"""Offline tests for the Agnes JSON boundary and daily state machine helpers."""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from run_daily import (  # noqa: E402
    DailyRunError,
    build_daily_report,
    build_seen_updates,
    pending_repositories_for_date,
    score_with_isolation,
    validate_evaluation,
)
from scripts.agnes_client import AgnesError, extract_json_object  # noqa: E402


def evaluation(repo: str, *, high: bool = True) -> dict:
    if high:
        breakdown = {
            "ai_development_relevance": 15,
            "problem_value_and_readme": 18,
            "innovation": 13,
            "engineering_maturity": 17,
            "installation_and_demo": 8,
            "maintenance_and_star_credibility": 7,
            "documentation_license_security": 4,
            "short_video_value": 4,
        }
    else:
        breakdown = {
            "ai_development_relevance": 8,
            "problem_value_and_readme": 13,
            "innovation": 7,
            "engineering_maturity": 10,
            "installation_and_demo": 6,
            "maintenance_and_star_credibility": 6,
            "documentation_license_security": 4,
            "short_video_value": 4,
        }
    score = sum(breakdown.values())
    return {
        "repo": repo,
        "score": score,
        "grade": "S" if score >= 85 else "C",
        "score_breakdown": breakdown,
        "readme_problem_clarity": "clear" if high else "partial",
        "star_credibility": "normal" if high else "unknown",
        "star_anomaly_signals": [],
        "short_video_suitability": "high" if high else "low",
        "short_video_hook": "一分钟演示" if high else "",
        "demo_idea": "录制前后对比" if high else "",
        "selection_reasons": ["达到门槛"] if high else ["创新性不足"],
        "light_analysis": "轻量分析",
        "blocked_security_risk": False,
        "is_high_score": high,
    }


class FakeAgnesClient:
    def complete_json(self, *, system_prompt: str, user_prompt: str) -> dict:
        repositories = json.loads(user_prompt)["repositories"]
        # Simulate a malformed multi-repository response. The runner should
        # isolate it into single-repository retries instead of losing all rows.
        if len(repositories) > 1:
            raise AgnesError("mock batch failure")
        repo = repositories[0]["repo"]
        return {"evaluations": [evaluation(repo, high=repo.endswith("high"))]}


class NoCallAgnesClient:
    def complete_json(self, *, system_prompt: str, user_prompt: str) -> dict:
        raise AssertionError("validated score cache should prevent another API call")


class RepairingAgnesClient:
    def __init__(self) -> None:
        self.calls = 0

    def complete_json(self, *, system_prompt: str, user_prompt: str) -> dict:
        self.calls += 1
        payload = json.loads(user_prompt)
        repo = payload["repositories"][0]["repo"]
        if self.calls == 1:
            malformed = evaluation(repo)
            malformed["score_breakdown"].pop("engineering_maturity")
            return {"evaluations": [malformed]}
        self.assert_repair_instruction(payload)
        return {"evaluations": [evaluation(repo)]}

    @staticmethod
    def assert_repair_instruction(payload: dict) -> None:
        if "校验错误" not in payload.get("instruction", ""):
            raise AssertionError("repair request should include validation feedback")


class AgnesBoundaryTest(unittest.TestCase):
    def test_extract_json_from_fenced_response(self) -> None:
        self.assertEqual(
            extract_json_object('说明\n```json\n{"ok": true}\n```'),
            {"ok": True},
        )

    def test_validate_high_score_is_derived_from_a_grade(self) -> None:
        with patch.dict(os.environ, {"HIGH_SCORE_THRESHOLD": "75"}):
            checked = validate_evaluation(evaluation("owner/high"), "owner/high")
        self.assertTrue(checked["is_high_score"])
        broken = evaluation("owner/high")
        broken["is_high_score"] = False
        checked = validate_evaluation(broken, "owner/high")
        self.assertTrue(checked["is_high_score"])

    def test_non_ai_a_grade_project_is_high_score(self) -> None:
        raw = evaluation("owner/general-tool")
        raw["score_breakdown"] = {
            "ai_development_relevance": 0,
            "problem_value_and_readme": 20,
            "innovation": 10,
            "engineering_maturity": 20,
            "installation_and_demo": 8,
            "maintenance_and_star_credibility": 8,
            "documentation_license_security": 5,
            "short_video_value": 4,
        }
        raw["score"] = 75
        raw["grade"] = "A"
        raw["is_high_score"] = False
        checked = validate_evaluation(raw, "owner/general-tool")
        self.assertTrue(checked["is_high_score"])

    def test_string_list_fields_are_normalized_without_changing_scores(self) -> None:
        raw = evaluation("owner/high")
        raw["star_anomaly_signals"] = "证据不足"
        raw["selection_reasons"] = '["达到门槛"]'
        checked = validate_evaluation(raw, "owner/high")
        self.assertEqual(checked["star_anomaly_signals"], ["证据不足"])
        self.assertEqual(checked["selection_reasons"], ["达到门槛"])
        self.assertEqual(checked["score"], raw["score"])

    def test_batch_failure_isolated_to_individual_repositories(self) -> None:
        repositories = [{"repo": "owner/high"}, {"repo": "owner/low"}]
        scores, failures = score_with_isolation(
            FakeAgnesClient(), repositories, batch_size=8
        )
        self.assertEqual([item["repo"] for item in scores], ["owner/high", "owner/low"])
        self.assertEqual(failures, [])

    def test_invalid_single_score_gets_one_feedback_repair(self) -> None:
        client = RepairingAgnesClient()
        scores, failures = score_with_isolation(
            client,
            [{"repo": "owner/high"}],
            batch_size=1,
        )
        self.assertEqual(client.calls, 2)
        self.assertEqual([item["repo"] for item in scores], ["owner/high"])
        self.assertEqual(failures, [])

    def test_validated_score_cache_prevents_duplicate_api_calls(self) -> None:
        import tempfile

        repositories = [{"repo": "owner/high"}, {"repo": "owner/low"}]
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "score_cache.json"
            first, failures = score_with_isolation(
                FakeAgnesClient(), repositories, batch_size=1, cache_path=cache
            )
            self.assertEqual(len(first), 2)
            self.assertEqual(failures, [])
            second, failures = score_with_isolation(
                NoCallAgnesClient(), repositories, batch_size=1, cache_path=cache
            )
            self.assertEqual(len(second), 2)
            self.assertEqual(failures, [])


class DailyStateTest(unittest.TestCase):
    def test_recover_only_same_day_pending_rows(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            seen = Path(directory) / "seen.txt"
            seen.write_text(
                "owner/today | 2026-08-26 | - | pending_analysis | not_attempted | pending_decision\n"
                "owner/old | 2026-08-25 | - | pending_analysis | not_attempted | pending_decision\n"
                "owner/done | 2026-08-26 | 80 | analyzed | succeeded | notified\n",
                encoding="utf-8",
            )
            self.assertEqual(
                pending_repositories_for_date(seen, "2026-08-26"),
                {"owner/today"},
            )

    def test_seen_updates_derive_status_without_recalculating_scores(self) -> None:
        high = validate_evaluation(evaluation("owner/high"), "owner/high")
        low = validate_evaluation(evaluation("owner/low", high=False), "owner/low")
        state = {
            "evaluations": [high, low],
            "failures": [{"repo": "owner/failed", "error": "timeout"}],
        }
        updates = build_seen_updates(
            state,
            {"owner/high": {"run_status": "succeeded"}},
        )
        by_repo = {item["repo"]: item for item in updates}
        self.assertEqual(by_repo["owner/high"]["analysis_decision"], "analyzed")
        self.assertEqual(by_repo["owner/high"]["run_status"], "succeeded")
        self.assertEqual(by_repo["owner/low"]["analysis_decision"], "skipped")
        self.assertEqual(by_repo["owner/failed"]["analysis_decision"], "analysis_failed")

    def test_empty_high_score_report(self) -> None:
        low = validate_evaluation(evaluation("owner/low", high=False), "owner/low")
        report = build_daily_report(
            {
                "date": "2026-08-26",
                "total_trending_count": 10,
                "input_count": 1,
                "evaluations": [low],
                "failures": [],
            },
            {},
        )
        self.assertIn("没有达到 A 级推荐门槛", report)
        self.assertIn("今日 Trending 速览", report)
        self.assertIn("owner/low", report)
        self.assertIn("没有 Clone、下载、安装或运行", report)


if __name__ == "__main__":
    unittest.main()
