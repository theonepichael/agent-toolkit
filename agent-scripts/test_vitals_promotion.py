#!/usr/bin/env python3
"""Tests for vitals_promotion.py. Run with: python3 test_vitals_promotion.py"""

import io
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

    def test_apply_does_not_write_needs_review_snapshot(self) -> None:
        self.write_session(
            make_session("s1", [make_decision("d1", source="assumed", verdict=None)])
        )
        vp.run(self.data_dir, apply=True)
        self.assertFalse((self.data_dir / "needs-review").exists())

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


def make_vitals_record(**overrides: object) -> dict:
    base: dict = {
        "text": "some settled fact about the topic",
        "reasoning": "because reasons that mention the topic too",
        "source_slug": "2026-09-09-some-session",
        "source_decision_id": "d1",
        "backlog_slug": None,
        "confidence": "verified",
        "promoted_at": "2026-09-09T00:00:00",
        "status": "active",
    }
    base.update(overrides)
    return base


class MatchesQueryTests(unittest.TestCase):
    def test_and_combination_requires_every_keyword(self) -> None:
        rec = make_vitals_record(text="fix the vitals query interface", reasoning="")
        self.assertTrue(vp.matches_query(rec, ["vitals", "query"]))
        self.assertFalse(vp.matches_query(rec, ["vitals", "missing"]))

    def test_case_insensitive(self) -> None:
        rec = make_vitals_record(text="Fix CLI Output", reasoning="")
        self.assertTrue(vp.matches_query(rec, ["cli"]))

    def test_matches_reasoning_field_too(self) -> None:
        rec = make_vitals_record(text="unrelated", reasoning="mentions ci pipeline")
        self.assertTrue(vp.matches_query(rec, ["ci"]))

    def test_short_domain_term_keywords_are_not_filtered(self) -> None:
        rec = make_vitals_record(text="set up ci and cd pipelines", reasoning="")
        self.assertTrue(vp.matches_query(rec, ["ci", "cd"]))

    def test_raises_on_empty_keywords(self) -> None:
        rec = make_vitals_record()
        with self.assertRaises(ValueError):
            vp.matches_query(rec, [])


class SearchVitalsTests(VitalsPromotionTestCase):
    def write_vitals(self, backlog_slug: str | None, records: list[dict]) -> None:
        path = self.vitals_file(backlog_slug)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(records))

    def test_raises_on_empty_keywords(self) -> None:
        self.write_vitals(None, [make_vitals_record()])
        with self.assertRaises(ValueError):
            vp.search_vitals(self.data_dir / "vitals", [], include_superseded=False)

    def test_defaults_to_global_only(self) -> None:
        self.write_vitals(None, [make_vitals_record(text="global vitals query fact")])
        self.write_vitals(
            "proj-x",
            [
                make_vitals_record(
                    text="proj-x vitals query fact", backlog_slug="proj-x"
                )
            ],
        )
        results = vp.search_vitals(
            self.data_dir / "vitals", ["vitals", "query"], include_superseded=False
        )
        self.assertEqual(len(results), 1)
        self.assertIsNone(results[0]["backlog_slug"])

    def test_backlog_slug_includes_both_files_global_first(self) -> None:
        self.write_vitals(None, [make_vitals_record(text="global vitals query fact")])
        self.write_vitals(
            "proj-x",
            [
                make_vitals_record(
                    text="proj-x vitals query fact", backlog_slug="proj-x"
                )
            ],
        )
        results = vp.search_vitals(
            self.data_dir / "vitals",
            ["vitals", "query"],
            include_superseded=False,
            backlog_slug="proj-x",
        )
        self.assertEqual(len(results), 2)
        self.assertIsNone(results[0]["backlog_slug"])
        self.assertEqual(results[1]["backlog_slug"], "proj-x")

    def test_backlog_slug_global_is_not_a_duplicate_load(self) -> None:
        self.write_vitals(None, [make_vitals_record(text="global vitals query fact")])
        results = vp.search_vitals(
            self.data_dir / "vitals",
            ["vitals", "query"],
            include_superseded=False,
            backlog_slug="_global",
        )
        self.assertEqual(len(results), 1)

    def test_excludes_superseded_by_default(self) -> None:
        self.write_vitals(
            None,
            [
                make_vitals_record(text="active vitals query fact", status="active"),
                make_vitals_record(
                    text="superseded vitals query fact", status="superseded"
                ),
            ],
        )
        results = vp.search_vitals(
            self.data_dir / "vitals", ["vitals", "query"], include_superseded=False
        )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "active")

    def test_include_superseded(self) -> None:
        self.write_vitals(
            None,
            [
                make_vitals_record(text="active vitals query fact", status="active"),
                make_vitals_record(
                    text="superseded vitals query fact", status="superseded"
                ),
            ],
        )
        results = vp.search_vitals(
            self.data_dir / "vitals", ["vitals", "query"], include_superseded=True
        )
        self.assertEqual(len(results), 2)

    def test_missing_backlog_file_returns_global_only(self) -> None:
        self.write_vitals(None, [make_vitals_record(text="global vitals query fact")])
        results = vp.search_vitals(
            self.data_dir / "vitals",
            ["vitals", "query"],
            include_superseded=False,
            backlog_slug="nonexistent",
        )
        self.assertEqual(len(results), 1)

    def test_no_matches_returns_empty_list(self) -> None:
        self.write_vitals(None, [make_vitals_record(text="unrelated fact")])
        results = vp.search_vitals(
            self.data_dir / "vitals", ["zzznomatch"], include_superseded=False
        )
        self.assertEqual(results, [])


class PrintSearchResultsTests(unittest.TestCase):
    def test_plain_text_zero_matches(self) -> None:
        buf = io.StringIO()
        vp.print_search_results([], as_json=False, file=buf)
        self.assertIn("no matching vitals records", buf.getvalue())

    def test_json_zero_matches_prints_empty_list(self) -> None:
        buf = io.StringIO()
        vp.print_search_results([], as_json=True, file=buf)
        self.assertEqual(json.loads(buf.getvalue()), [])

    def test_plain_text_includes_backlog_slug_and_citation_fields(self) -> None:
        rec = make_vitals_record(
            text="the fact", reasoning="the reasoning", backlog_slug="proj-x"
        )
        buf = io.StringIO()
        vp.print_search_results([rec], as_json=False, file=buf)
        out = buf.getvalue()
        self.assertIn("2026-09-09-some-session", out)
        self.assertIn("d1", out)
        self.assertIn("backlog_slug: proj-x", out)
        self.assertIn("the fact", out)
        self.assertIn("the reasoning", out)

    def test_plain_text_global_record_shown_as_global(self) -> None:
        rec = make_vitals_record(backlog_slug=None)
        buf = io.StringIO()
        vp.print_search_results([rec], as_json=False, file=buf)
        self.assertIn("backlog_slug: global", buf.getvalue())

    def test_json_output_round_trips_records(self) -> None:
        rec = make_vitals_record()
        buf = io.StringIO()
        vp.print_search_results([rec], as_json=True, file=buf)
        self.assertEqual(json.loads(buf.getvalue()), [rec])


@pytest.mark.allow_real_subprocess  # invokes the real vitals_promotion.py CLI end to end
class SearchCliTests(VitalsPromotionTestCase):
    def write_vitals(self, backlog_slug: str | None, records: list[dict]) -> None:
        path = self.vitals_file(backlog_slug)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(records))

    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(Path(__file__).parent / "vitals_promotion.py"),
                "--data-dir",
                str(self.data_dir),
                *args,
            ],
            capture_output=True,
            text=True,
        )

    def test_search_prints_matches(self) -> None:
        self.write_vitals(None, [make_vitals_record(text="the vitals query design")])
        result = self.run_cli("--search", "vitals query")
        self.assertEqual(result.returncode, 0)
        self.assertIn("the vitals query design", result.stdout)

    def test_empty_query_is_rejected(self) -> None:
        self.write_vitals(None, [make_vitals_record()])
        result = self.run_cli("--search", "   ")
        self.assertNotEqual(result.returncode, 0)

    def test_search_does_not_run_apply(self) -> None:
        self.write_vitals(None, [make_vitals_record(text="the vitals query design")])
        result = self.run_cli("--search", "vitals query")
        self.assertEqual(result.returncode, 0)
        # nothing to promote since no grill sessions exist in data_dir
        self.assertFalse((self.data_dir / "needs-review").exists())


if __name__ == "__main__":
    unittest.main()
