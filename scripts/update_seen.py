"""将每日流程的评分 JSON 原子更新到 seen.txt。

本脚本不计算项目分数，只校验并保存 ``seen_updates``。
它不会添加新仓库；新仓库只能由 fetch_trending.py 首次登记。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SEEN_FILE = PROJECT_ROOT / "seen.txt"

ALLOWED_DECISIONS = {"analyzed", "skipped", "analysis_failed"}
ALLOWED_RUN_STATUSES = {
    "not_attempted",
    "succeeded",
    "run_failed",
    "manual_required",
    "blocked",
}
ALLOWED_NOTIFICATION_STATUSES = {
    "pending_decision",
    "pending",
    "notified",
    "notify_failed",
    "not_required",
}
ALLOWED_TRANSITIONS = {
    "pending_analysis": {"analyzed", "skipped", "analysis_failed"},
    "analysis_failed": {"analyzed", "skipped", "analysis_failed"},
    "analyzed": {"analyzed"},
    "skipped": {"skipped"},
}


class SeenUpdateError(ValueError):
    """输入数据或 seen.txt 状态不符合协议。"""


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
        raise SeenUpdateError(f"无效仓库名 {value!r}，必须是 owner/repo")
    return f"{parts[0]}/{parts[1]}".lower()


def expected_grade(score: int) -> str:
    if score >= 85:
        return "S"
    if score >= 75:
        return "A"
    if score >= 60:
        return "B"
    if score >= 40:
        return "C"
    return "D"


def require_string(update: dict[str, Any], field: str, repo: str) -> str:
    value = update.get(field)
    if not isinstance(value, str) or not value.strip():
        raise SeenUpdateError(f"{repo}: {field} 必须是非空字符串")
    return value.strip().lower()


def validate_score(update: dict[str, Any], repo: str, decision: str) -> int | None:
    score = update.get("score")
    if decision == "analysis_failed":
        if score not in (None, "", "-"):
            raise SeenUpdateError(f"{repo}: analysis_failed 时 score 必须为空或 '-' ")
        return None

    if isinstance(score, bool) or not isinstance(score, int):
        raise SeenUpdateError(f"{repo}: score 必须是 0–100 的整数")
    if not 0 <= score <= 100:
        raise SeenUpdateError(f"{repo}: score 超出 0–100 范围")

    grade = update.get("grade")
    if not isinstance(grade, str) or grade.strip().upper() != expected_grade(score):
        raise SeenUpdateError(
            f"{repo}: grade 与 score={score} 不匹配，应为 {expected_grade(score)}"
        )

    breakdown = update.get("score_breakdown")
    if breakdown is not None:
        if not isinstance(breakdown, dict) or not breakdown:
            raise SeenUpdateError(f"{repo}: score_breakdown 必须是非空对象")
        values = list(breakdown.values())
        if any(isinstance(value, bool) or not isinstance(value, int) for value in values):
            raise SeenUpdateError(f"{repo}: score_breakdown 的各项必须是整数")
        if sum(values) != score:
            raise SeenUpdateError(
                f"{repo}: score_breakdown 合计 {sum(values)}，不等于 score={score}"
            )
    return score


def validate_status_combination(
    repo: str,
    decision: str,
    run_status: str,
    notification_status: str,
) -> None:
    if decision not in ALLOWED_DECISIONS:
        raise SeenUpdateError(f"{repo}: 非法 analysis_decision={decision!r}")
    if run_status not in ALLOWED_RUN_STATUSES:
        raise SeenUpdateError(f"{repo}: 非法 run_status={run_status!r}")
    if notification_status not in ALLOWED_NOTIFICATION_STATUSES:
        raise SeenUpdateError(
            f"{repo}: 非法 notification_status={notification_status!r}"
        )

    if decision == "skipped" and (
        run_status != "not_attempted" or notification_status != "not_required"
    ):
        raise SeenUpdateError(
            f"{repo}: skipped 必须配合 not_attempted 和 not_required"
        )
    if decision == "analysis_failed" and (
        run_status != "not_attempted" or notification_status != "pending_decision"
    ):
        raise SeenUpdateError(
            f"{repo}: analysis_failed 必须配合 not_attempted 和 pending_decision"
        )
    if decision == "analyzed" and notification_status == "not_required":
        raise SeenUpdateError(f"{repo}: analyzed 项目不能使用 not_required")


def validate_update(raw_update: Any) -> dict[str, Any]:
    if not isinstance(raw_update, dict):
        raise SeenUpdateError("seen_updates 中每一项都必须是 JSON 对象")

    raw_repo = raw_update.get("repo")
    if not isinstance(raw_repo, str):
        raise SeenUpdateError("seen_updates 项目缺少字符串字段 repo")
    repo = normalize_repo(raw_repo)
    decision = require_string(raw_update, "analysis_decision", repo)
    run_status = require_string(raw_update, "run_status", repo)
    notification_status = require_string(raw_update, "notification_status", repo)
    score = validate_score(raw_update, repo, decision)
    validate_status_combination(repo, decision, run_status, notification_status)

    return {
        "repo": repo,
        "score": score,
        "decision": decision,
        "run_status": run_status,
        "notification_status": notification_status,
    }


def load_updates(path: Path | None, from_stdin: bool) -> list[dict[str, Any]]:
    try:
        if from_stdin:
            raw_text = sys.stdin.read()
        else:
            if path is None:
                raise SeenUpdateError("必须提供 --input 或 --stdin")
            if not path.is_file():
                raise SeenUpdateError(f"输入 JSON 不存在：{path}")
            raw_text = path.read_text(encoding="utf-8-sig")
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise SeenUpdateError(
            f"JSON 格式错误：第 {exc.lineno} 行第 {exc.colno} 列，{exc.msg}"
        ) from exc

    if isinstance(payload, dict):
        raw_updates = payload.get("seen_updates")
    else:
        raw_updates = payload
    if not isinstance(raw_updates, list):
        raise SeenUpdateError("输入必须是 seen_updates 数组，或包含该数组的 JSON 对象")
    if not raw_updates:
        return []

    updates = [validate_update(update) for update in raw_updates]
    repos = [update["repo"] for update in updates]
    duplicates = sorted({repo for repo in repos if repos.count(repo) > 1})
    if duplicates:
        raise SeenUpdateError("输入中存在重复仓库：" + ", ".join(duplicates))
    return updates


def parse_seen(content: str) -> tuple[list[str], dict[str, tuple[int, list[str]]]]:
    lines = content.splitlines()
    records: dict[str, tuple[int, list[str]]] = {}
    duplicate_records: set[str] = set()

    for index, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        columns = [column.strip() for column in line.split("|")]
        if len(columns) < 6:
            raise SeenUpdateError(
                f"seen.txt 第 {index + 1} 行字段不足 6 列：{raw_line!r}"
            )
        repo = normalize_repo(columns[0])
        if repo in records:
            duplicate_records.add(repo)
        records[repo] = (index, columns)

    if duplicate_records:
        raise SeenUpdateError(
            "seen.txt 中存在重复有效记录：" + ", ".join(sorted(duplicate_records))
        )
    return lines, records


def apply_updates(
    content: str,
    updates: list[dict[str, Any]],
) -> tuple[str, list[dict[str, str]]]:
    lines, records = parse_seen(content)
    requested_repos = {update["repo"] for update in updates}
    missing = sorted(requested_repos - records.keys())
    if missing:
        raise SeenUpdateError(
            "以下仓库尚未由 fetch_trending.py 登记，拒绝新增：" + ", ".join(missing)
        )

    changes: list[dict[str, str]] = []
    for update in updates:
        repo = update["repo"]
        index, columns = records[repo]
        old_decision = columns[3].lower()
        allowed_targets = ALLOWED_TRANSITIONS.get(old_decision, set())
        if update["decision"] not in allowed_targets:
            raise SeenUpdateError(
                f"{repo}: 不允许从 {old_decision!r} 转换到 {update['decision']!r}"
            )

        old_line = " | ".join(columns)
        score_text = "-" if update["score"] is None else str(update["score"])

        # 防止重复运行评分更新脚本时，把已经成功发送的项目退回 pending。
        notification_status = update["notification_status"]
        current_notification = columns[5].lower()
        if current_notification == "notified" and notification_status == "pending":
            notification_status = "notified"

        columns[0] = repo
        columns[2] = score_text
        columns[3] = update["decision"]
        columns[4] = update["run_status"]
        columns[5] = notification_status
        new_line = " | ".join(columns)
        lines[index] = new_line
        changes.append({"repo": repo, "before": old_line, "after": new_line})

    had_final_newline = content.endswith(("\n", "\r"))
    updated_content = "\n".join(lines)
    if had_final_newline:
        updated_content += "\n"
    return updated_content, changes


def write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="把每日流程的 seen_updates JSON 更新到 seen.txt"
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path, help="主 Agent 输出的 JSON 文件")
    source.add_argument("--stdin", action="store_true", help="从标准输入读取 JSON")
    parser.add_argument(
        "--seen-file",
        type=Path,
        default=DEFAULT_SEEN_FILE,
        help=f"seen.txt 路径，默认：{DEFAULT_SEEN_FILE}",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只校验并显示变更，不修改 seen.txt",
    )
    return parser.parse_args()


def main() -> int:
    configure_console_encoding()
    args = parse_args()
    input_path = args.input.expanduser().resolve() if args.input else None
    seen_path = args.seen_file.expanduser().resolve()

    try:
        updates = load_updates(input_path, args.stdin)
        if not seen_path.is_file():
            raise SeenUpdateError(f"seen.txt 不存在：{seen_path}")
        original = seen_path.read_text(encoding="utf-8-sig")
        updated, changes = apply_updates(original, updates)
    except (OSError, SeenUpdateError) as exc:
        print(f"更新失败：{exc}", file=sys.stderr)
        return 2

    if not changes:
        print("输入没有 seen_updates，seen.txt 无需修改。")
        return 0

    for change in changes:
        print(f"{change['repo']}")
        print(f"  原状态：{change['before']}")
        print(f"  新状态：{change['after']}")

    if args.dry_run:
        print(f"dry-run：已验证 {len(changes)} 条更新，没有修改 seen.txt。")
        return 0

    try:
        write_text_atomic(seen_path, updated)
    except OSError as exc:
        print(f"写入失败：{exc}", file=sys.stderr)
        return 3

    print(f"更新成功：已原子写入 {len(changes)} 条记录到 {seen_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
