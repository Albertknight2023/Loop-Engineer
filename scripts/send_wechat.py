"""通过 Windows PC 微信把日报发送给“文件传输助手”。

此脚本固定发送目标，不允许从命令行指定其他联系人。它使用 wxauto 控制已经
登录的 PC 微信客户端，因此必须在用户已登录的交互式 Windows 会话中运行。

默认读取 reports/latest.md。可以用 --dry-run 预览，不触碰微信，也不修改
seen.txt。通过 --repo 指定本次报告包含的高分仓库后，脚本会在全部消息发送
成功时把对应项目的通知状态更新为 notified；发送异常时更新为 notify_failed。
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path
from typing import Iterable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPORT = PROJECT_ROOT / "reports" / "latest.md"
DEFAULT_SEEN_FILE = PROJECT_ROOT / "seen.txt"
WECHAT_TARGET = "文件传输助手"


def configure_console_encoding() -> None:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8")


def normalize_repo(value: str) -> str:
    value = value.strip().strip("/")
    if value.lower().endswith(".git"):
        value = value[:-4]
    parts = [part.strip() for part in value.split("/") if part.strip()]
    if len(parts) != 2:
        raise ValueError(f"无效的仓库名：{value!r}，应为 owner/repo")
    return f"{parts[0]}/{parts[1]}".lower()


def markdown_to_plain_text(markdown: str) -> str:
    """把日报中的常用 Markdown 转成适合微信阅读的纯文本。"""

    text = markdown.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"```[^\n]*\n?", "", text)
    text = re.sub(r"\[([^\]]+)]\((https?://[^)]+)\)", r"\1：\2", text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*>\s?", "", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"__([^_]+)__", r"\1", text)
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def split_message(text: str, max_chars: int) -> list[str]:
    """尽量按段落和行切分，避免截断项目说明。"""

    if max_chars < 200:
        raise ValueError("--max-chars 不能小于 200")
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    current = ""

    def flush() -> None:
        nonlocal current
        if current.strip():
            chunks.append(current.strip())
        current = ""

    for paragraph in re.split(r"\n\s*\n", text):
        paragraph = paragraph.strip()
        if not paragraph:
            continue

        pieces: list[str] = []
        if len(paragraph) <= max_chars:
            pieces.append(paragraph)
        else:
            for line in paragraph.splitlines():
                line = line.strip()
                if not line:
                    continue
                pieces.extend(
                    line[start : start + max_chars]
                    for start in range(0, len(line), max_chars)
                )

        for piece in pieces:
            separator = "\n\n" if current else ""
            if len(current) + len(separator) + len(piece) <= max_chars:
                current += separator + piece
            else:
                flush()
                current = piece
    flush()
    return chunks


def read_message(args: argparse.Namespace) -> str:
    if args.message is not None:
        raw = args.message
    elif args.stdin:
        raw = sys.stdin.read()
    else:
        report_path = args.message_file.expanduser().resolve()
        if not report_path.is_file():
            raise FileNotFoundError(f"报告不存在：{report_path}")
        raw = report_path.read_text(encoding="utf-8-sig")

    if not raw.strip():
        raise ValueError("消息内容为空，拒绝发送")
    return raw if args.raw_markdown else markdown_to_plain_text(raw)


def parse_seen_lines(content: str) -> tuple[list[str], dict[str, int]]:
    lines = content.splitlines()
    positions: dict[str, int] = {}
    for index, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        columns = [column.strip() for column in line.split("|")]
        if not columns:
            continue
        try:
            repo = normalize_repo(columns[0])
        except ValueError:
            continue
        positions[repo] = index
    return lines, positions


def validate_repositories_for_notification(path: Path, repositories: set[str]) -> None:
    """发送前确认仓库存在，且确实属于已深度分析的项目。"""

    if not repositories:
        return
    if not path.is_file():
        raise FileNotFoundError(f"seen 文件不存在：{path}")

    lines, positions = parse_seen_lines(path.read_text(encoding="utf-8-sig"))
    missing = sorted(repositories - positions.keys())
    if missing:
        raise ValueError(f"以下仓库不在 seen.txt 中：{', '.join(missing)}")

    invalid: list[str] = []
    for repo in sorted(repositories):
        columns = [column.strip() for column in lines[positions[repo]].split("|")]
        decision = columns[3].lower() if len(columns) >= 4 else ""
        if decision != "analyzed":
            invalid.append(f"{repo}({decision or '缺少状态'})")
    if invalid:
        raise ValueError(
            "只有 analyzed 项目才能标记微信通知：" + ", ".join(invalid)
        )


def write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def update_notification_status(
    path: Path,
    repositories: set[str],
    status: str,
) -> None:
    """原子更新指定仓库的通知状态，不改变分数和其他状态。"""

    if not repositories:
        return
    content = path.read_text(encoding="utf-8-sig")
    had_final_newline = content.endswith(("\n", "\r"))
    lines, positions = parse_seen_lines(content)

    for repo in repositories:
        index = positions[repo]
        columns = [column.strip() for column in lines[index].split("|")]
        if len(columns) < 6:
            raise ValueError(f"seen.txt 中 {repo} 的字段不足 6 列")
        columns[5] = status
        lines[index] = " | ".join(columns)

    updated = "\n".join(lines)
    if had_final_newline:
        updated += "\n"
    write_text_atomic(path, updated)


def send_chunks(chunks: Iterable[str], delay_seconds: float) -> int:
    """发送全部分段；返回已成功调用 SendMsg 的分段数量。"""

    try:
        from wxauto import WeChat
        import wxauto.wxauto as wxauto_implementation
    except ModuleNotFoundError as exc:
        missing_module = exc.name or "未知模块"
        raise RuntimeError(
            f"微信自动化组件导入失败，缺少模块 {missing_module!r}。"
            "请运行：python -m pip install -r requirements.txt"
        ) from exc
    except ImportError as exc:
        raise RuntimeError(
            f"微信自动化组件导入失败：{exc}。"
            "请确认当前 Python 与依赖兼容。"
        ) from exc

    # weixin-auto 3.9.11.17 的 SendMsg 会在每次发送前调用
    # UpdateWnd.Close()，其实现通过发送 Esc 关闭升级弹窗。在微信 3.9.12.x
    # 上，UpdateWnd 的 UIA 识别可能误中主窗口，导致微信主窗口被 Esc 隐藏。
    # 这里只在当前脚本进程内禁用该动作，不修改 site-packages。
    def keep_wechat_window_open(_update_window: object) -> None:
        return None

    wxauto_implementation.UpdateWnd.Close = keep_wechat_window_open

    # 同一版本的 WeChat.SendMsg 在发送成功后还会显式执行
    # self.UiaAPI.SendKeys('{Ctrl}{Alt}w')。该组合键会切换微信主窗口的
    # 显示/隐藏状态，因此必须在 wxauto 自己的按键入口处拦截。只拦截这一
    # 个精确组合，不影响搜索、粘贴、回车等实际发送操作。
    original_uia_send_keys = wxauto_implementation.uia.SendKeys

    def safe_uia_send_keys(
        text: str,
        interval: float = 0.01,
        waitTime: float = 0.5,
        charMode: bool = True,
        debug: bool = False,
    ) -> None:
        normalized = re.sub(r"\s+", "", text).casefold()
        if normalized == "{ctrl}{alt}w":
            return None
        original_uia_send_keys(text, interval, waitTime, charMode, debug)

    wxauto_implementation.uia.SendKeys = safe_uia_send_keys

    try:
        wechat = WeChat()
    except Exception as exc:
        raise RuntimeError(
            "无法连接已登录的 PC 微信。请确认微信 3.9 客户端已登录、主界面可用，"
            "并且脚本运行在同一个交互式 Windows 用户会话中。"
        ) from exc

    sent_count = 0
    for chunk in chunks:
        try:
            wechat.SendMsg(chunk, who=WECHAT_TARGET)
        except Exception as exc:
            raise RuntimeError(
                f"第 {sent_count + 1} 段发送失败；前面可能已有 {sent_count} 段发送成功。"
                "为避免重复，脚本不会自动重试。"
            ) from exc
        sent_count += 1
        if delay_seconds > 0:
            time.sleep(delay_seconds)
    return sent_count


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="把 GitHub Trending 日报发送到微信文件传输助手"
    )
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--message", help="直接发送的文本")
    source.add_argument("--stdin", action="store_true", help="从标准输入读取文本")
    source.add_argument(
        "--message-file",
        type=Path,
        default=DEFAULT_REPORT,
        help=f"消息文件，默认：{DEFAULT_REPORT}",
    )
    parser.add_argument(
        "--repo",
        action="append",
        default=[],
        help="本次报告包含的高分仓库 owner/repo；可重复指定",
    )
    parser.add_argument(
        "--seen-file",
        type=Path,
        default=DEFAULT_SEEN_FILE,
        help=f"通知状态文件，默认：{DEFAULT_SEEN_FILE}",
    )
    parser.add_argument("--max-chars", type=int, default=1800, help="每段最大字符数")
    parser.add_argument("--max-chunks", type=int, default=6, help="最多发送段数")
    parser.add_argument("--delay", type=float, default=0.6, help="分段发送间隔秒数")
    parser.add_argument("--raw-markdown", action="store_true", help="不转换 Markdown")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只显示最终文本，不控制微信、不修改 seen.txt",
    )
    return parser.parse_args()


def main() -> int:
    configure_console_encoding()
    args = parse_args()

    if args.max_chunks < 1:
        print("发送失败：--max-chunks 必须大于 0", file=sys.stderr)
        return 2
    if args.delay < 0:
        print("发送失败：--delay 不能小于 0", file=sys.stderr)
        return 2

    try:
        message = read_message(args)
        chunks = split_message(message, args.max_chars)
        if len(chunks) > args.max_chunks:
            raise ValueError(
                f"报告需要发送 {len(chunks)} 段，超过 --max-chunks={args.max_chunks}。"
                "请精简报告或显式提高限制，脚本不会静默截断。"
            )
        repositories = {normalize_repo(repo) for repo in args.repo}
        seen_path = args.seen_file.expanduser().resolve()
        validate_repositories_for_notification(seen_path, repositories)
    except (OSError, ValueError) as exc:
        print(f"发送失败：{exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        print(f"目标：{WECHAT_TARGET}")
        print(f"分段：{len(chunks)}")
        for index, chunk in enumerate(chunks, start=1):
            print(f"\n--- 第 {index}/{len(chunks)} 段 ---\n{chunk}")
        print("\ndry-run：没有控制微信，也没有修改 seen.txt。")
        return 0

    try:
        sent_count = send_chunks(chunks, args.delay)
    except RuntimeError as exc:
        print(f"发送失败：{exc}", file=sys.stderr)
        try:
            update_notification_status(seen_path, repositories, "notify_failed")
        except (OSError, ValueError) as status_exc:
            print(f"同时无法更新通知状态：{status_exc}", file=sys.stderr)
        return 1

    try:
        update_notification_status(seen_path, repositories, "notified")
    except (OSError, ValueError) as exc:
        print(
            f"微信已发送 {sent_count} 段，但更新 seen.txt 失败：{exc}。"
            "请勿直接重发，先手工核对通知状态。",
            file=sys.stderr,
        )
        return 3

    print(f"发送成功：已向{WECHAT_TARGET}发送 {sent_count} 段消息。")
    if repositories:
        print(f"已标记 notified：{', '.join(sorted(repositories))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
