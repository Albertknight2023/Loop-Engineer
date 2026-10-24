"""抓取 GitHub Daily Trending，用 seen.txt 去重并登记首次出现的仓库。

脚本负责抓取、解析、本地去重，并把新仓库立即追加到 seen.txt；不会调用 AI。
默认输出：data/raw/YYYY-MM-DD/trending.json 和 trending.html。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


GITHUB_BASE_URL = "https://github.com"
TRENDING_URL = f"{GITHUB_BASE_URL}/trending"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SEEN_FILE = PROJECT_ROOT / "seen.txt"
DEFAULT_SEEN_TEMPLATE = PROJECT_ROOT / "templates" / "seen.example.txt"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0 Safari/537.36 loop-engineer/1.0"
)


def china_now() -> datetime:
    """返回北京时间，不依赖 Windows 是否安装 IANA 时区数据。"""

    return datetime.now(timezone(timedelta(hours=8)))


def normalize_repo(value: str) -> str:
    """将仓库路径规范化为小写 owner/repo。"""

    value = value.strip().strip("/")
    if value.lower().endswith(".git"):
        value = value[:-4]
    parts = [part.strip() for part in value.split("/") if part.strip()]
    if len(parts) != 2:
        return ""
    return f"{parts[0]}/{parts[1]}".lower()


def load_seen_repositories(path: Path) -> set[str]:
    """读取 seen.txt 第一列；任何已经登记的仓库都不再作为新项目。"""

    if not path.exists():
        return set()

    repositories: set[str] = set()
    with path.open("r", encoding="utf-8-sig") as seen_file:
        for raw_line in seen_file:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            repo = normalize_repo(line.split("|", maxsplit=1)[0])
            if repo:
                repositories.add(repo)
    return repositories


def parse_number(text: str | None) -> int | None:
    """解析 1,234、1.2k、3m 等 GitHub 数量文本。"""

    if not text:
        return None
    match = re.search(r"([0-9][0-9,.]*)\s*([kKmM]?)", text)
    if not match:
        return None

    number_text = match.group(1).replace(",", "")
    try:
        number = float(number_text)
    except ValueError:
        return None

    multiplier = {"": 1, "k": 1_000, "m": 1_000_000}[match.group(2).lower()]
    return int(number * multiplier)


def clean_text(element: Any) -> str | None:
    if element is None:
        return None
    text = " ".join(element.get_text(" ", strip=True).split())
    return text or None


def build_session() -> requests.Session:
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount("https://", adapter)
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
    )
    return session


def fetch_daily_trending(
    session: requests.Session,
    *,
    language: str | None,
    spoken_language_code: str | None,
    timeout: float,
) -> tuple[str, str]:
    url = TRENDING_URL
    if language:
        if not re.fullmatch(r"[A-Za-z0-9+#._-]+", language):
            raise ValueError(f"无效的语言路径：{language!r}")
        url = f"{TRENDING_URL}/{language}"

    params = {"since": "daily"}
    if spoken_language_code:
        params["spoken_language_code"] = spoken_language_code

    response = session.get(url, params=params, timeout=timeout)
    response.raise_for_status()
    return response.text, response.url


def parse_trending_html(html: str, seen: set[str]) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    articles = soup.select("article.Box-row")
    repositories: list[dict[str, Any]] = []

    for rank, article in enumerate(articles, start=1):
        repo_link = article.select_one("h2 a[href]")
        if repo_link is None:
            continue

        repo_path = repo_link.get("href", "")
        repo_key = normalize_repo(repo_path)
        if not repo_key:
            continue
        owner, name = repo_key.split("/", maxsplit=1)

        stars_link = article.select_one('a[href$="/stargazers"]')
        forks_link = article.select_one('a[href$="/forks"]')
        language_element = article.select_one('[itemprop="programmingLanguage"]')

        stars_today_text = None
        for element in article.select("span"):
            candidate = clean_text(element)
            if candidate and re.search(r"\bstars?\s+today\b", candidate, re.IGNORECASE):
                stars_today_text = candidate
                break

        built_by: list[str] = []
        for user_link in article.select('a[data-hovercard-type="user"][href]'):
            login = str(user_link.get("href", "")).strip("/")
            if login and "/" not in login and login not in built_by:
                built_by.append(login)

        repositories.append(
            {
                "rank": rank,
                "repo": repo_key,
                "owner": owner,
                "name": name,
                "url": urljoin(GITHUB_BASE_URL, repo_path),
                "description": clean_text(article.select_one("p")),
                "language": clean_text(language_element),
                "stars": parse_number(clean_text(stars_link)),
                "forks": parse_number(clean_text(forks_link)),
                "stars_today": parse_number(stars_today_text),
                "built_by": built_by,
                "is_seen": repo_key in seen,
            }
        )

    if not repositories:
        page_title = clean_text(soup.select_one("title")) or "未知页面"
        raise RuntimeError(
            "没有解析到 Trending 仓库；GitHub 页面结构可能已经改变，"
            f"或返回了验证页面。页面标题：{page_title}"
        )
    return repositories


def write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(path)


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    content = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    write_text_atomic(path, content)


def append_new_repositories_to_seen(
    path: Path,
    repositories: list[dict[str, Any]],
    discovered_date: str,
) -> list[dict[str, Any]]:
    """把真正首次出现的仓库原子追加为 pending_analysis 记录。

    写入前重新读取一次 seen.txt，避免同一仓库被并发或重复追加。
    返回实际成功追加的仓库列表。
    """

    current_seen = load_seen_repositories(path)
    actually_new: list[dict[str, Any]] = []
    for repository in repositories:
        repo_key = repository["repo"]
        if repo_key not in current_seen:
            actually_new.append(repository)
            current_seen.add(repo_key)

    if not actually_new:
        return []

    existing_content = ""
    if path.exists():
        existing_content = path.read_text(encoding="utf-8-sig")
    elif DEFAULT_SEEN_TEMPLATE.exists():
        existing_content = DEFAULT_SEEN_TEMPLATE.read_text(encoding="utf-8-sig")
    if existing_content and not existing_content.endswith(("\n", "\r")):
        existing_content += "\n"

    new_lines = [
        (
            f"{repository['repo']} | {discovered_date} | - | "
            "pending_analysis | not_attempted | pending_decision"
        )
        for repository in actually_new
    ]
    updated_content = existing_content + "\n".join(new_lines) + "\n"
    write_text_atomic(path, updated_content)
    return actually_new


def parse_args() -> argparse.Namespace:
    today = china_now().date().isoformat()
    default_output = PROJECT_ROOT / "data" / "raw" / today / "trending.json"

    parser = argparse.ArgumentParser(description="抓取 GitHub Daily Trending 项目")
    parser.add_argument("--language", help="可选编程语言，例如 python、javascript、c++")
    parser.add_argument("--spoken-language-code", help="可选自然语言代码，例如 zh、en")
    parser.add_argument(
        "--seen-file",
        type=Path,
        default=DEFAULT_SEEN_FILE,
        help=f"去重索引路径，默认：{DEFAULT_SEEN_FILE}",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=default_output,
        help=f"JSON 输出路径，默认：{default_output}",
    )
    parser.add_argument("--timeout", type=float, default=30.0, help="请求超时秒数，默认 30")
    parser.add_argument("--no-save-html", action="store_true", help="不保存原始 HTML")
    parser.add_argument("--print-json", action="store_true", help="同时将完整 JSON 输出到终端")
    return parser.parse_args()


def main() -> int:
    # 让 Windows 定时任务和重定向日志稳定输出中文 UTF-8。
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(encoding="utf-8")

    args = parse_args()
    seen_path = args.seen_file.expanduser().resolve()
    output_path = args.output.expanduser().resolve()

    try:
        seen = load_seen_repositories(seen_path)
        with build_session() as session:
            html, source_url = fetch_daily_trending(
                session,
                language=args.language,
                spoken_language_code=args.spoken_language_code,
                timeout=args.timeout,
            )
        repositories = parse_trending_html(html, seen)
    except (OSError, ValueError, RuntimeError, requests.RequestException) as exc:
        print(f"抓取失败：{exc}", file=sys.stderr)
        return 1

    new_candidates = [repo for repo in repositories if not repo["is_seen"]]
    discovered_date = china_now().date().isoformat()
    try:
        new_repositories = append_new_repositories_to_seen(
            seen_path,
            new_candidates,
            discovered_date,
        )
    except OSError as exc:
        print(f"更新 seen.txt 失败：{exc}", file=sys.stderr)
        return 1

    # 如果写入前发生过并发更新，以实际成功追加的结果作为 AI 唯一输入。
    new_repo_keys = {repo["repo"] for repo in new_repositories}
    for repository in repositories:
        repository["is_new"] = repository["repo"] in new_repo_keys

    fetched_at = china_now().isoformat(timespec="seconds")
    payload = {
        "period": "daily",
        "fetched_at": fetched_at,
        "source_url": source_url,
        "filters": {
            "language": args.language,
            "spoken_language_code": args.spoken_language_code,
        },
        "seen_file": str(seen_path),
        "total_count": len(repositories),
        "seen_count": len(repositories) - len(new_repositories),
        "new_count": len(new_repositories),
        "repositories": repositories,
        "new_repositories": new_repositories,
    }

    try:
        write_json_atomic(output_path, payload)
        if not args.no_save_html:
            write_text_atomic(output_path.with_name("trending.html"), html)
    except OSError as exc:
        print(f"保存失败：{exc}", file=sys.stderr)
        return 1

    print(
        f"抓取完成：共 {payload['total_count']} 个，"
        f"已见 {payload['seen_count']} 个，新项目 {payload['new_count']} 个。"
    )
    print(f"新项目已登记到：{seen_path}")
    print(f"JSON：{output_path}")
    if not args.no_save_html:
        print(f"HTML：{output_path.with_name('trending.html')}")
    if args.print_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
