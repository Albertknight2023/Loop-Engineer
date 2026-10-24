"""Agnes OpenAI-compatible text API client.

This module only sends prompts and parses JSON.  It deliberately does not run
shell commands, clone repositories, update seen.txt, or control WeChat.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"


class AgnesError(RuntimeError):
    """A safe-to-display Agnes configuration, transport, or response error."""


def configure_console_encoding() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8")


def _positive_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise AgnesError(f"{name} 必须是整数，当前为 {raw!r}") from exc
    if value <= 0:
        raise AgnesError(f"{name} 必须大于 0")
    return value


def _nonnegative_int(name: str, default: int) -> int:
    raw = os.getenv(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise AgnesError(f"{name} 必须是整数，当前为 {raw!r}") from exc
    if value < 0:
        raise AgnesError(f"{name} 不能小于 0")
    return value


def _temperature() -> float:
    raw = os.getenv("LLM_TEMPERATURE", "0.2").strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise AgnesError(f"LLM_TEMPERATURE 必须是数字，当前为 {raw!r}") from exc
    if not 0 <= value <= 2:
        raise AgnesError("LLM_TEMPERATURE 必须在 0 到 2 之间")
    return value


@dataclass(frozen=True)
class AgnesConfig:
    api_key: str
    base_url: str
    model: str
    timeout_seconds: int
    max_tokens: int
    temperature: float
    max_retries: int

    @classmethod
    def from_env(cls, env_file: Path = DEFAULT_ENV_FILE) -> "AgnesConfig":
        load_dotenv(env_file, override=False)
        api_key = os.getenv("AGNES_API_KEY", "").strip()
        if not api_key or api_key.lower().startswith("your_"):
            raise AgnesError(
                f"没有可用的 AGNES_API_KEY。请在 {env_file} 中填写密钥，"
                "不要把密钥写进 PROMPT.md 或命令行参数。"
            )

        base_url = os.getenv(
            "AGNES_BASE_URL", "https://apihub.agnes-ai.com/v1"
        ).strip().rstrip("/")
        if not base_url.startswith("https://"):
            raise AgnesError("AGNES_BASE_URL 必须使用 https://")

        model = os.getenv("AGNES_MODEL", "agnes-2.0-flash").strip()
        if not model:
            raise AgnesError("AGNES_MODEL 不能为空")

        return cls(
            api_key=api_key,
            base_url=base_url,
            model=model,
            timeout_seconds=_positive_int("LLM_TIMEOUT_SECONDS", 120),
            max_tokens=_positive_int("LLM_MAX_TOKENS", 8000),
            temperature=_temperature(),
            # Zero keeps API usage predictable. Workflow-level recovery can
            # retry deliberately instead of silently paying for duplicate calls.
            max_retries=_nonnegative_int("LLM_MAX_RETRIES", 0),
        )


def extract_json_object(text: str) -> dict[str, Any]:
    """Parse a JSON object, tolerating a short preface or a Markdown fence."""

    candidate = text.strip()
    if candidate.startswith("```"):
        first_newline = candidate.find("\n")
        last_fence = candidate.rfind("```")
        if first_newline != -1 and last_fence > first_newline:
            candidate = candidate[first_newline + 1 : last_fence].strip()

    decoder = json.JSONDecoder()
    for index, character in enumerate(candidate):
        if character != "{":
            continue
        try:
            value, _ = decoder.raw_decode(candidate[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise AgnesError("Agnes 没有返回可解析的 JSON 对象")


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            else:
                text = getattr(item, "text", None)
                if isinstance(text, str):
                    parts.append(text)
        if parts:
            return "\n".join(parts)
    raise AgnesError("Agnes 响应中没有文本 content")


class AgnesClient:
    def __init__(self, config: AgnesConfig | None = None) -> None:
        self.config = config or AgnesConfig.from_env()
        try:
            self._client = OpenAI(
                api_key=self.config.api_key,
                base_url=self.config.base_url,
                timeout=self.config.timeout_seconds,
                max_retries=self.config.max_retries,
            )
        except ImportError as exc:
            if "socks" in str(exc).lower():
                raise AgnesError(
                    "检测到 SOCKS 代理，但缺少 socksio。请用当前 Python 运行："
                    'python -m pip install "httpx[socks]"'
                ) from exc
            raise AgnesError(f"Agnes 客户端依赖加载失败：{exc}") from exc

    def complete_json(self, *, system_prompt: str, user_prompt: str) -> dict[str, Any]:
        """Return one JSON object from a streaming chat completion.

        JSON mode is attempted first. Some OpenAI-compatible gateways reject
        ``response_format`` even when normal chat completions work, so a 400/422
        response is retried once without that optional parameter. Streaming is
        used because complex Agnes prompts may take long enough that proxies
        close an otherwise idle non-streaming connection.
        """

        common: dict[str, Any] = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "stream": True,
        }

        def request_and_collect(*, json_mode: bool) -> str:
            kwargs = dict(common)
            if json_mode:
                kwargs["response_format"] = {"type": "json_object"}
            stream = self._client.chat.completions.create(**kwargs)
            parts: list[str] = []
            for chunk in stream:
                for choice in getattr(chunk, "choices", []):
                    delta = getattr(choice, "delta", None)
                    content = getattr(delta, "content", None)
                    if isinstance(content, str):
                        parts.append(content)
            if not parts:
                raise AgnesError("Agnes 流式响应中没有文本 content")
            return "".join(parts)

        try:
            try:
                content = request_and_collect(json_mode=True)
            except APIStatusError as exc:
                if exc.status_code not in {400, 422}:
                    raise
                content = request_and_collect(json_mode=False)
        except APITimeoutError as exc:
            raise AgnesError(
                f"Agnes 请求超过 {self.config.timeout_seconds} 秒"
            ) from exc
        except APIConnectionError as exc:
            raise AgnesError("无法连接 Agnes API，请检查网络和 AGNES_BASE_URL") from exc
        except APIStatusError as exc:
            request_id = getattr(exc, "request_id", None)
            suffix = f"，request_id={request_id}" if request_id else ""
            raise AgnesError(f"Agnes API 返回 HTTP {exc.status_code}{suffix}") from exc
        except Exception as exc:
            raise AgnesError(f"Agnes 调用失败：{type(exc).__name__}") from exc

        return extract_json_object(content)

    def check_connection(self) -> dict[str, Any]:
        return self.complete_json(
            system_prompt="只返回严格 JSON，不要 Markdown。",
            user_prompt='返回 {"ok": true, "message": "agnes connected"}',
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="测试 Agnes 文本模型 API")
    parser.add_argument(
        "--check",
        action="store_true",
        help="发送最小 JSON 请求验证密钥、地址和模型",
    )
    return parser.parse_args()


def main() -> int:
    configure_console_encoding()
    args = parse_args()
    if not args.check:
        print("请使用 --check 做最小连接测试；本脚本不会显示 API 密钥。")
        return 0
    try:
        client = AgnesClient()
        result = client.check_connection()
    except AgnesError as exc:
        print(f"连接测试失败：{exc}", file=sys.stderr)
        return 1
    print("Agnes 连接成功。")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
