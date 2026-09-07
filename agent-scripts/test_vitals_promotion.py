#!/usr/bin/env python3
"""Tests for vitals_promotion.py. Run with: python3 test_vitals_promotion.py"""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import vitals_promotion as vp


def make_decision(
    decision_id: str,
    *,
    decision: str | None = "some decision text",
    reasoning: str = "some reasoning",
    source: str | None = "user",
    verdict: dict | None = None,
    question: str = "some question?",
) -> dict:
    return {
        "id": decision_id,
        "question": question,
        "reasoning": reasoning,
        "decision": decision,
        "source": source,
        "verdict": verdict,
    }


def make_session(
    slug: str, decisions: list[dict], backlog_slug: str | None = None
) -> dict:
    return {
        "schema_version": 1,
        "slug": slug,
        "topic": "topic",
        "created": "2026-08-01",
        "updated": "2026-08-01",
        "plan_path": None,
        "pending_execution": False,
        "backlog_slug": backlog_slug,
        "decisions": decisions,
    }


class VitalsPromotionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.data_dir = self.tmpdir / "grill"
        self.data_dir.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir)

    def write_session(self, session: dict) -> None:
        path = self.data_dir / f"{session['slug']}.json"
        path.write_text(json.dumps(session))

    def vitals_file(self, backlog_slug: str | None) -> Path:
        name = backlog_slug or "_global"
        return self.data_dir / "vitals" / f"{name}.json"


class ClassificationTests(VitalsPromotionTestCase):
    def test_open_decision_not_classified(self) -> None:
        d = make_decision("open-one", decision=None, source=None)
        self.assertTrue(vp.is_open(d))

    def test_user_verified_is_auto_promote(self) -> None:
        d = make_decision(
            "d1",
            source="user",
            verdict={"result": "VERIFIED", "evidence": "e", "date": "2026-08-01"},
        )
        self.assertEqual(vp.classify_decision(d), vp.AUTO_PROMOTE)

    def test_assumed_source_is_needs_review(self) -> None:
        d = make_decision("d2", source="assumed", verdict=None)
        self.assertEqual(vp.classify_decision(d), vp.NEEDS_REVIEW)
        self.assertEqual(vp.needs_review_reason(d), "assumed_defaulted")

    def test_defaulted_source_is_needs_review(self) -> None:
        d = make_decision("d3", source="defaulted", verdict=None)
        self.assertEqual(vp.classify_decision(d), vp.NEEDS_REVIEW)

    def test_disputed_verdict_is_needs_review(self) -> None:
        d = make_decision(
            "d4",
            source="user",
            verdict={"result": "DISPUTED", "evidence": "e", "date": "2026-08-01"},
        )
        self.assertEqual(vp.classify_decision(d), vp.NEEDS_REVIEW)
        self.assertEqual(vp.needs_review_reason(d), "disputed_unverifiable")

    def test_unverifiable_verdict_is_needs_review(self) -> None:
        d = make_decision(
            "d5",
            source="user",
            verdict={"result": "UNVERIFIABLE", "evidence": "e", "date": "2026-08-01"},
        )
        self.assertEqual(vp.classify_decision(d), vp.NEEDS_REVIEW)

    def test_tested_with_no_verdict_is_needs_review(self) -> None:
        d = make_decision("d6", source="tested", verdict=None)
        self.assertEqual(vp.classify_decision(d), vp.NEEDS_REVIEW)
        self.assertEqual(vp.needs_review_reason(d), "tested_no_verdict")

    def test_tested_with_verified_verdict_is_pending_verification(self) -> None:
        d = make_decision(
            "d7",
            source="tested",
            verdict={"result": "VERIFIED", "evidence": "e", "date": "2026-08-01"},
        )
        self.assertEqual(vp.classify_decision(d), vp.PENDING_VERIFICATION)

    def test_user_with_no_verdict_is_pending_verification(self) -> None:
        d = make_decision("d8", source="user", verdict=None)
        self.assertEqual(vp.classify_decision(d), vp.PENDING_VERIFICATION)

    def test_invalid_source_is_schema_anomaly(self) -> None:
        d = make_decision("d9", source="bogus", verdict=None)
        self.assertEqual(vp.classify_decision(d), vp.SCHEMA_ANOMALY)
        self.assertIn("invalid source", vp.anomaly_reason(d))

    def test_invalid_verdict_result_is_schema_anomaly(self) -> None:
        d = make_decision(
            "d10",
            source="user",
            verdict={"result": "BOGUS", "evidence": "e", "date": "2026-08-01"},
        )
        self.assertEqual(vp.classify_decision(d), vp.SCHEMA_ANOMALY)
        self.assertIn("invalid verdict.result", vp.anomaly_reason(d))


class RunDryRunTests(VitalsPromotionTestCase):
    def test_dry_run_writes_nothing(self) -> None:
        self.write_session(
            make_session(
                "s1",
                [
                    make_decision(
                        "d1",
                        source="user",
                        verdict={
                            "result": "VERIFIED",
                            "evidence": "e",
                            "date": "2026-08-01",
                        },
                    )
                ],
            )
        )
        report = vp.run(self.data_dir, apply=False)
        self.assertEqual(report["promoted_count"], 1)
        self.assertFalse((self.data_dir / "vitals").exists())
        self.assertFalse((self.data_dir / "needs-review").exists())

    def test_open_decisions_excluded_from_totals(self) -> None:
        self.write_session(
            make_session(
                "s1",
                [
                    make_decision("open1", decision=None, source=None, verdict=None),
                    make_decision(
                        "d1",
                        source="user",
                        verdict={
                            "result": "VERIFIED",
                            "evidence": "e",
                            "date": "2026-08-01",
                        },
                    ),
                ],
            )
        )
        report = vp.run(self.data_dir, apply=False)
        self.assertEqual(report["total_decisions"], 2)
        self.assertEqual(report["open_decisions"], 1)
        self.assertEqual(sum(report["bucket_counts"].values()), 1)


class RunApplyTests(VitalsPromotionTestCase):
    def test_apply_writes_vitals_record(self) -> None:
        self.write_session(
            make_session(
                "s1",
                [
                    make_decision(
                        "d1",
                        decision="do the thing",
                        reasoning="because reasons",
                        source="user",
                        verdict={
                            "result": "VERIFIED",
                            "evidence": "e",
                            "date": "2026-08-01",
                        },
                    )
                ],
                backlog_slug="proj-x",
            )
        )
        report = vp.run(self.data_dir, apply=True)
        self.assertEqual(report["promoted_count"], 1)

        records = json.loads(self.vitals_file("proj-x").read_text())
        self.assertEqual(len(records), 1)
        rec = records[0]
        self.assertEqual(rec["text"], "do the thing")
        self.assertEqual(rec["reasoning"], "because reasons")
        self.assertEqual(rec["source_slug"], "s1")
        self.assertEqual(rec["source_decision_id"], "d1")
        self.assertEqual(rec["backlog_slug"], "proj-x")
        self.assertEqual(rec["confidence"], "verified")
        self.assertEqual(rec["status"], "active")
        self.assertIn("promoted_at", rec)

    def test_global_backlog_slug_uses_underscore_global_file(self) -> None:
        self.write_session(
            make_session(
                "s1",
                [
                    make_decision(
                        "d1",
                        source="user",
                        verdict={
                            "result": "VERIFIED",
                            "evidence": "e",
                            "date": "2026-08-01",
                        },
                    )
                ],
                backlog_slug=None,
            )
        )
        vp.run(self.data_dir, apply=True)
        self.assertTrue(self.vitals_file(None).exists())

    def test_apply_writes_needs_review_file(self) -> None:
        self.write_session(
            make_session("s1", [make_decision("d1", source="assumed", verdict=None)])
        )
        report = vp.run(self.data_dir, apply=True)
        self.assertTrue(report["needs_review_path"].exists())
        entries = json.loads(report["needs_review_path"].read_text())
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["source_decision_id"], "d1")
        self.assertEqual(entries[0]["flag_reason"], "assumed_defaulted")

    def test_needs_review_does_not_touch_vitals(self) -> None:
        self.write_session(
            make_session("s1", [make_decision("d1", source="assumed", verdict=None)])
        )
        vp.run(self.data_dir, apply=True)
        self.assertFalse((self.data_dir / "vitals").exists())

    def test_rerun_is_idempotent_no_duplicate_promotion(self) -> None:
        self.write_session(
            make_session(
                "s1",
                [
                    make_decision(
                        "d1",
                        source="user",
                        verdict={
                            "result": "VERIFIED",
                            "evidence": "e",
                            "date": "2026-08-01",
                        },
                    )
                ],
            )
        )
        first = vp.run(self.data_dir, apply=True)
        second = vp.run(self.data_dir, apply=True)
        self.assertEqual(first["promoted_count"], 1)
        self.assertEqual(second["promoted_count"], 0)
        records = json.loads(self.vitals_file(None).read_text())
        self.assertEqual(len(records), 1)


class SupersedeTests(VitalsPromotionTestCase):
    def _promote_one(self) -> None:
        self.write_session(
            make_session(
                "s1",
                [
                    make_decision(
                        "d1",
                        decision="original text",
                        source="user",
                        verdict={
                            "result": "VERIFIED",
                            "evidence": "e",
                            "date": "2026-08-01",
                        },
                    )
                ],
            )
        )
        vp.run(self.data_dir, apply=True)

    def test_removed_decision_supersedes_record(self) -> None:
        self._promote_one()
        self.write_session(make_session("s1", []))  # decision rm'd
        report = vp.run(self.data_dir, apply=True)
        self.assertEqual(report["superseded_count"], 1)
        records = json.loads(self.vitals_file(None).read_text())
        self.assertEqual(records[0]["status"], "superseded")
        self.assertIn("no longer exists", records[0]["reason"])

    def test_reopened_decision_supersedes_record(self) -> None:
        self._promote_one()
        self.write_session(
            make_session(
                "s1", [make_decision("d1", decision=None, source=None, verdict=None)]
            )
        )
        report = vp.run(self.data_dir, apply=True)
        self.assertEqual(report["superseded_count"], 1)
        records = json.loads(self.vitals_file(None).read_text())
        self.assertEqual(records[0]["status"], "superseded")
        self.assertIn("open again", records[0]["reason"])

    def test_revised_text_supersedes_and_repromotes(self) -> None:
        self._promote_one()
        self.write_session(
            make_session(
                "s1",
                [
                    make_decision(
                        "d1",
                        decision="revised text",
                        source="user",
                        verdict={
                            "result": "VERIFIED",
                            "evidence": "e2",
                            "date": "2026-08-02",
                        },
                    )
                ],
            )
        )
        report = vp.run(self.data_dir, apply=True)
        self.assertEqual(report["superseded_count"], 1)
        self.assertEqual(report["promoted_count"], 1)
        records = json.loads(self.vitals_file(None).read_text())
        self.assertEqual(len(records), 2)
        statuses = {r["status"] for r in records}
        self.assertEqual(statuses, {"superseded", "active"})
        active = next(r for r in records if r["status"] == "active")
        self.assertEqual(active["text"], "revised text")

    def test_verdict_flip_without_text_change_supersedes(self) -> None:
        self._promote_one()
        self.write_session(
            make_session(
                "s1",
                [
                    make_decision(
                        "d1",
                        decision="original text",
                        source="user",
                        verdict={
                            "result": "DISPUTED",
                            "evidence": "e2",
                            "date": "2026-08-02",
                        },
                    )
                ],
            )
        )
        report = vp.run(self.data_dir, apply=True)
        self.assertEqual(report["superseded_count"], 1)
        self.assertEqual(report["promoted_count"], 0)
        records = json.loads(self.vitals_file(None).read_text())
        self.assertEqual(records[0]["status"], "superseded")
        self.assertIn("no longer meets auto-promote criteria", records[0]["reason"])

    def test_verdict_reset_to_null_supersedes(self) -> None:
        self._promote_one()
        self.write_session(
            make_session(
                "s1",
                [
                    make_decision(
                        "d1", decision="original text", source="user", verdict=None
                    )
                ],
            )
        )
        report = vp.run(self.data_dir, apply=True)
        self.assertEqual(report["superseded_count"], 1)
        records = json.loads(self.vitals_file(None).read_text())
        self.assertEqual(records[0]["status"], "superseded")

    def test_unchanged_decision_not_superseded(self) -> None:
        self._promote_one()
        report = vp.run(self.data_dir, apply=True)
        self.assertEqual(report["superseded_count"], 0)
        self.assertEqual(report["promoted_count"], 0)


def make_needs_review_entry(
    source_slug: str,
    decision_id: str,
    *,
    question: str = "some question?",
    flag_reason: str = "assumed_defaulted",
) -> dict:
    return {
        "source_slug": source_slug,
        "source_decision_id": decision_id,
        "backlog_slug": None,
        "question": question,
        "decision": "some answer",
        "reasoning": "some reasoning",
        "source": "assumed",
        "verdict": None,
        "flag_reason": flag_reason,
    }


class LatestNeedsReviewFileTests(VitalsPromotionTestCase):
    def test_returns_none_on_missing_directory(self) -> None:
        self.assertIsNone(vp.latest_needs_review_file(self.data_dir / "needs-review"))

    def test_returns_none_on_empty_directory(self) -> None:
        needs_review_dir = self.data_dir / "needs-review"
        needs_review_dir.mkdir()
        self.assertIsNone(vp.latest_needs_review_file(needs_review_dir))

    def test_returns_lexicographically_last_file(self) -> None:
        needs_review_dir = self.data_dir / "needs-review"
        needs_review_dir.mkdir()
        (needs_review_dir / "2026-08-01-needs-review.json").write_text("[]")
        (needs_review_dir / "2026-08-11-needs-review.json").write_text("[]")
        (needs_review_dir / "2026-08-05-needs-review.json").write_text("[]")
        result = vp.latest_needs_review_file(needs_review_dir)
        self.assertEqual(result, needs_review_dir / "2026-08-11-needs-review.json")


class SummarizeNeedsReviewTests(unittest.TestCase):
    def test_empty_list(self) -> None:
        self.assertEqual(vp.summarize_needs_review([]), "needs-review: 0 entries")

    def test_mixed_list_picks_earliest_source_slug(self) -> None:
        entries = [
            make_needs_review_entry("2026-08-11-later-topic", "d2", question="q2"),
            make_needs_review_entry("2026-08-01-earlier-topic", "d1", question="q1"),
        ]
        summary = vp.summarize_needs_review(entries)
        self.assertIn("2 entries", summary)
        self.assertIn("2026-08-01", summary)
        self.assertIn("d1", summary)
        self.assertIn("q1", summary)

    def test_long_question_is_truncated(self) -> None:
        long_question = "x" * 200
        entries = [
            make_needs_review_entry("2026-08-01-slug", "d1", question=long_question)
        ]
        summary = vp.summarize_needs_review(entries)
        self.assertNotIn(long_question, summary)
        self.assertIn("...", summary)


@pytest.mark.allow_real_subprocess
class NeedsReviewSummaryCliTests(VitalsPromotionTestCase):
    def test_flag_prints_zero_entries_on_missing_dir(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).parent / "vitals_promotion.py"),
                "--data-dir",
                str(self.data_dir),
                "--needs-review-summary",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("needs-review: 0 entries", result.stdout)

    def test_flag_prints_summary_and_does_not_run_apply(self) -> None:
        needs_review_dir = self.data_dir / "needs-review"
        needs_review_dir.mkdir()
        entries = [make_needs_review_entry("2026-08-01-slug", "d1", question="q1")]
        (needs_review_dir / "2026-08-01-needs-review.json").write_text(
            json.dumps(entries)
        )
        result = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).parent / "vitals_promotion.py"),
                "--data-dir",
                str(self.data_dir),
                "--needs-review-summary",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0)
        self.assertIn("1 entries", result.stdout)
        self.assertIn("d1", result.stdout)
        self.assertFalse((self.data_dir / "vitals").exists())


if __name__ == "__main__":
    unittest.main()
