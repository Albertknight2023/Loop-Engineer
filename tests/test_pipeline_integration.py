"""不调用 Agnes、不发送微信的 Loop Engineer 逻辑连通性测试。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.fetch_trending import (  # noqa: E402
    append_new_repositories_to_seen,
    load_seen_repositories,
    parse_trending_html,
)
from scripts.send_wechat import (  # noqa: E402
    markdown_to_plain_text,
    split_message,
    update_notification_status,
    validate_repositories_for_notification,
)
from scripts.update_seen import (  # noqa: E402
    apply_updates,
    load_updates,
    write_text_atomic,
)


SAMPLE_HTML = """
<!doctype html>
<html><head><title>Trending repositories on GitHub today</title></head><body>
  <article class="Box-row">
    <h2><a href="/owner/already-seen">owner / already-seen</a></h2>
    <p>Previously processed AI tool.</p>
    <span itemprop="programmingLanguage">Python</span>
    <a href="/owner/already-seen/stargazers">1,000</a>
    <a href="/owner/already-seen/forks">100</a>
    <span class="d-inline-block float-sm-right">20 stars today</span>
  </article>
  <article class="Box-row">
    <h2><a href="/owner/high-project">owner / high-project</a></h2>
    <p>An innovative AI developer tool with a reproducible demo.</p>
    <span itemprop="programmingLanguage">Python</span>
    <a href="/owner/high-project/stargazers">8,000</a>
    <a href="/owner/high-project/forks">800</a>
    <span class="d-inline-block float-sm-right">300 stars today</span>
  </article>
  <article class="Box-row">
    <h2><a href="/owner/low-project">owner / low-project</a></h2>
    <p>A small unrelated demo with an unclear problem statement.</p>
    <span itemprop="programmingLanguage">JavaScript</span>
    <a href="/owner/low-project/stargazers">25,000</a>
    <a href="/owner/low-project/forks">20</a>
    <span class="d-inline-block float-sm-right">5,000 stars today</span>
  </article>
</body></html>
"""


class PipelineIntegrationTest(unittest.TestCase):
    def test_mocked_daily_pipeline(self) -> None:
        with tempfile.TemporaryDirectory(prefix="loop-engineer-test-") as directory:
            root = Path(directory)
            seen_path = root / "seen.txt"
            seen_path.write_text(
                "# integration fixture\n"
                "owner/already-seen | 2026-08-25 | 82 | analyzed | succeeded | notified\n",
                encoding="utf-8",
            )

            # 1. Python 抓取结果解析与 seen.txt 去重。
            seen_before = load_seen_repositories(seen_path)
            repositories = parse_trending_html(SAMPLE_HTML, seen_before)
            candidates = [repo for repo in repositories if not repo["is_seen"]]
            appended = append_new_repositories_to_seen(
                seen_path,
                candidates,
                "2026-08-26",
            )
            self.assertEqual(
                {repo["repo"] for repo in appended},
                {"owner/high-project", "owner/low-project"},
            )
            self.assertNotIn("owner/already-seen", {repo["repo"] for repo in appended})

            # 2. 模拟主 Agent 给所有新项目评分。
            analysis_payload = {
                "date": "2026-08-26",
                "seen_updates": [
                    {
                        "repo": "owner/high-project",
                        "score": 86,
                        "grade": "S",
                        "score_breakdown": {
                            "ai_development_relevance": 15,
                            "problem_value_and_readme": 18,
                            "innovation": 13,
                            "engineering_maturity": 17,
                            "installation_and_demo": 8,
                            "maintenance_and_star_credibility": 7,
                            "documentation_license_security": 4,
                            "short_video_value": 4,
                        },
                        "analysis_decision": "analyzed",
                        "run_status": "succeeded",
                        "notification_status": "pending",
                    },
                    {
                        "repo": "owner/low-project",
                        "score": 58,
                        "grade": "C",
                        "score_breakdown": {
                            "ai_development_relevance": 8,
                            "problem_value_and_readme": 13,
                            "innovation": 7,
                            "engineering_maturity": 10,
                            "installation_and_demo": 6,
                            "maintenance_and_star_credibility": 6,
                            "documentation_license_security": 4,
                            "short_video_value": 4,
                        },
                        "analysis_decision": "skipped",
                        "run_status": "not_attempted",
                        "notification_status": "not_required",
                    },
                ],
            }
            analysis_path = root / "analysis.json"
            analysis_path.write_text(
                json.dumps(analysis_payload, ensure_ascii=False),
                encoding="utf-8",
            )
            validated_updates = load_updates(analysis_path, from_stdin=False)
            current_seen = seen_path.read_text(encoding="utf-8")
            updated_seen, changes = apply_updates(current_seen, validated_updates)
            self.assertEqual(len(changes), 2)
            write_text_atomic(seen_path, updated_seen)

            # 3. 模拟子 Agent 高分项目报告片段和主 Agent 日期日报。
            staging_path = (
                root / "reports" / ".staging" / "2026-08-26" / "owner__high-project.md"
            )
            staging_path.parent.mkdir(parents=True)
            staging_path.write_text(
                "## owner/high-project\n\n"
                "- 评分：86 / S\n"
                "- 运行状态：succeeded\n"
                "- 短视频钩子：一分钟展示 AI 开发效率提升。\n",
                encoding="utf-8",
            )
            daily_report = root / "reports" / "2026-08-26.md"
            daily_report.write_text(
                "# GitHub Trending 日报｜2026-08-26\n\n"
                "## 今日 Trending 速览\n\n"
                "- owner/high-project：高分项目。\n"
                "- owner/low-project：低分项目，只做简要介绍。\n\n"
                + staging_path.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            report_text = daily_report.read_text(encoding="utf-8")
            self.assertIn("owner/high-project", report_text)
            self.assertIn("owner/low-project", report_text)

            # 4. 微信只做 dry-run 等价处理，不调用 wxauto；发送后模拟状态更新。
            plain_text = markdown_to_plain_text(report_text)
            chunks = split_message(plain_text, max_chars=1800)
            self.assertGreaterEqual(len(chunks), 1)
            validate_repositories_for_notification(
                seen_path,
                {"owner/high-project"},
            )
            update_notification_status(
                seen_path,
                {"owner/high-project"},
                "notified",
            )

            final_seen = seen_path.read_text(encoding="utf-8")
            self.assertIn(
                "owner/high-project | 2026-08-26 | 86 | analyzed | succeeded | notified",
                final_seen,
            )
            self.assertIn(
                "owner/low-project | 2026-08-26 | 58 | skipped | not_attempted | not_required",
                final_seen,
            )


if __name__ == "__main__":
    unittest.main()
