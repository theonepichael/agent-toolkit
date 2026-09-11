#!/usr/bin/env python3
"""Tests for vitals_promotion.py. Run with: python3 test_vitals_promotion.py"""

import copy
import io
import json
import os
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


def promoted(session_slug: str = "s1", decision_id: str = "d1", **overrides) -> dict:
    """A make_decision() that classifies as AUTO_PROMOTE."""
    base = dict(
        decision="do the thing",
        reasoning="because reasons",
        source="user",
        verdict={"result": "VERIFIED", "evidence": "e", "date": "2026-08-01"},
    )
    base.update(overrides)
    return make_session(
        session_slug, [make_decision(decision_id, **base)]
    )


class TempDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.data_dir = self.tmpdir / "grill"
        self.data_dir.mkdir()
        self.vitals_dir = self.data_dir / "vitals"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir)

    def write_session(self, session: dict) -> None:
        """Write a session file to the data dir (only for CLI-level tests)."""
        path = self.data_dir / f"{session['slug']}.json"
        path.write_text(json.dumps(session))

    def vitals_file(self, backlog_slug: str | None) -> Path:
        name = backlog_slug or "_global"
        return self.vitals_dir / f"{name}.json"

    def write_vitals(self, backlog_slug: str | None, records: list[dict]) -> None:
        path = self.vitals_file(backlog_slug)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(records))


# ── pure classification (unchanged policy, unchanged function) ──────────────


class ClassificationTests(TempDirTestCase):
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


# ── classify(): read-only pass ───────────────────────────────────────────────


class ClassifyTests(TempDirTestCase):
    def test_classify_writes_nothing(self) -> None:
        """The headline read-only guarantee: pending promotions AND supersedes
        previewed, nothing on disk — not even the vitals directory."""
        sessions = [promoted()]
        report = vp.classify(sessions, vitals_dir=self.vitals_dir)
        self.assertEqual(report["promoted_count"], 1)
        self.assertFalse(self.vitals_dir.exists())
        self.assertFalse((self.data_dir / "needs-review").exists())

    def test_open_decisions_excluded_from_totals(self) -> None:
        sessions = [
            make_session(
                "s1",
                [
                    make_decision("open1", decision=None, source=None, verdict=None),
                    make_decision("d1"),
                ],
            )
        ]
        report = vp.classify(sessions, vitals_dir=self.vitals_dir)
        self.assertEqual(report["total_decisions"], 2)
        self.assertEqual(report["open_decisions"], 1)
        self.assertEqual(sum(report["bucket_counts"].values()), 1)

    def test_classify_previews_pending_supersede_without_writing(self) -> None:
        """A promoted record whose decision later vanished must be *reported* as
        superseded by classify, while the on-disk record stays active."""
        self.write_vitals(
            None,
            [
                make_vitals_record(
                    text="gone", source_slug="s1", source_decision_id="d1"
                )
            ],
        )
        before = self.vitals_file(None).read_bytes()
        report = vp.classify([], vitals_dir=self.vitals_dir)
        self.assertEqual(report["superseded_count"], 1)
        self.assertEqual(self.vitals_file(None).read_bytes(), before)

    def test_anomaly_entries_reported(self) -> None:
        sessions = [make_session("s1", [make_decision("d1", source="bogus")])]
        report = vp.classify(sessions, vitals_dir=self.vitals_dir)
        self.assertEqual(len(report["anomaly_entries"]), 1)
        slug, decision_id, reason = report["anomaly_entries"][0]
        self.assertEqual((slug, decision_id), ("s1", "d1"))
        self.assertIn("invalid source", reason)

    def test_classify_does_not_mutate_input_sessions(self) -> None:
        sessions = [promoted()]
        snapshot = copy.deepcopy(sessions)
        vp.classify(sessions, vitals_dir=self.vitals_dir)
        self.assertEqual(sessions, snapshot)

    def test_vitals_dir_required_keyword_only(self) -> None:
        with self.assertRaises(TypeError):
            vp.classify([promoted()])  # type: ignore[call-arg]


# ── promote(): the only write path ──────────────────────────────────────────


class PromoteTests(TempDirTestCase):
    def test_promote_writes_vitals_record(self) -> None:
        sessions = [
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
        ]
        report = vp.promote(sessions, vitals_dir=self.vitals_dir)
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

    def test_nothing_dirty_writes_no_directory(self) -> None:
        """A NEEDS_REVIEW decision touches nothing at all."""
        sessions = [make_session("s1", [make_decision("d1", source="assumed")])]
        vp.promote(sessions, vitals_dir=self.vitals_dir)
        self.assertFalse(self.vitals_dir.exists())
        self.assertFalse((self.data_dir / "needs-review").exists())

    def test_global_backlog_slug_uses_underscore_global_file(self) -> None:
        sessions = [promoted()]
        vp.promote(sessions, vitals_dir=self.vitals_dir)
        self.assertTrue(self.vitals_file(None).exists())

    def test_rerun_is_idempotent_no_duplicate_promotion(self) -> None:
        sessions = [promoted()]
        first = vp.promote(sessions, vitals_dir=self.vitals_dir)
        second = vp.promote(sessions, vitals_dir=self.vitals_dir)
        self.assertEqual(first["promoted_count"], 1)
        self.assertEqual(second["promoted_count"], 0)
        records = json.loads(self.vitals_file(None).read_text())
        self.assertEqual(len(records), 1)

    def test_classify_then_promote_matches_promote_alone(self) -> None:
        """The no-caller-visible-mutation contract: a dry run followed by the
        write on the *same list object* reports exactly what the write alone
        reports."""
        sessions = [promoted()]
        dry = vp.classify(sessions, vitals_dir=self.vitals_dir)
        applied_after_dry = vp.promote(sessions, vitals_dir=self.vitals_dir)
        fresh_dir = self.tmpdir / "grill2" / "vitals"
        applied_fresh = vp.promote([promoted()], vitals_dir=fresh_dir)
        for key in ("promoted_count", "superseded_count", "total_decisions"):
            self.assertEqual(dry[key], applied_fresh[key])
            self.assertEqual(applied_after_dry[key], applied_fresh[key])

    def test_vitals_dir_required_keyword_only(self) -> None:
        with self.assertRaises(TypeError):
            vp.promote([promoted()])  # type: ignore[call-arg]


# ── supersede pass (via promote) ─────────────────────────────────────────────


class SupersedeTests(TempDirTestCase):
    def _promote_one(self) -> None:
        vp.promote([promoted()], vitals_dir=self.vitals_dir)

    def test_removed_decision_supersedes_record(self) -> None:
        self._promote_one()
        report = vp.promote([make_session("s1", [])], vitals_dir=self.vitals_dir)
        self.assertEqual(report["superseded_count"], 1)
        records = json.loads(self.vitals_file(None).read_text())
        self.assertEqual(records[0]["status"], "superseded")
        self.assertIn("no longer exists", records[0]["reason"])

    def test_reopened_decision_supersedes_record(self) -> None:
        self._promote_one()
        sessions = [
            make_session(
                "s1", [make_decision("d1", decision=None, source=None, verdict=None)]
            )
        ]
        report = vp.promote(sessions, vitals_dir=self.vitals_dir)
        self.assertEqual(report["superseded_count"], 1)
        records = json.loads(self.vitals_file(None).read_text())
        self.assertEqual(records[0]["status"], "superseded")
        self.assertIn("open again", records[0]["reason"])

    def test_revised_text_supersedes_and_repromotes(self) -> None:
        self._promote_one()
        sessions = [
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
        ]
        report = vp.promote(sessions, vitals_dir=self.vitals_dir)
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
        sessions = [
            make_session(
                "s1",
                [
                    make_decision(
                        "d1",
                        decision="do the thing",
                        source="user",
                        verdict={
                            "result": "DISPUTED",
                            "evidence": "e2",
                            "date": "2026-08-02",
                        },
                    )
                ],
            )
        ]
        report = vp.promote(sessions, vitals_dir=self.vitals_dir)
        self.assertEqual(report["superseded_count"], 1)
        self.assertEqual(report["promoted_count"], 0)
        records = json.loads(self.vitals_file(None).read_text())
        self.assertEqual(records[0]["status"], "superseded")
        self.assertIn("no longer meets auto-promote criteria", records[0]["reason"])

    def test_verdict_reset_to_null_supersedes(self) -> None:
        self._promote_one()
        sessions = [
            make_session(
                "s1", [make_decision("d1", decision="do the thing", verdict=None)]
            )
        ]
        report = vp.promote(sessions, vitals_dir=self.vitals_dir)
        self.assertEqual(report["superseded_count"], 1)
        records = json.loads(self.vitals_file(None).read_text())
        self.assertEqual(records[0]["status"], "superseded")

    def test_unchanged_decision_not_superseded(self) -> None:
        self._promote_one()
        report = vp.promote([promoted()], vitals_dir=self.vitals_dir)
        self.assertEqual(report["superseded_count"], 0)
        self.assertEqual(report["promoted_count"], 0)

    def test_supersede_pass_acts_on_every_vitals_file_in_the_store(self) -> None:
        """The store-wide glob reaches backlog-scoped files search() never loads."""
        self.write_vitals(
            "proj-other",
            [
                make_vitals_record(
                    text="other fact", source_slug="s1", source_decision_id="d1"
                )
            ],
        )
        report = vp.promote([], vitals_dir=self.vitals_dir)
        self.assertEqual(report["superseded_count"], 1)
        records = json.loads(self.vitals_file("proj-other").read_text())
        self.assertEqual(records[0]["status"], "superseded")

    def test_malformed_vitals_file_writes_nothing(self) -> None:
        """All loads precede all writes: an unparsable store file aborts the
        write pass with zero files rewritten."""
        self.write_vitals("proj-x", [make_vitals_record(text="good file")])
        self.vitals_dir.joinpath("broken.json").write_text("{not json")
        sessions = [promoted()]
        with self.assertRaises(json.JSONDecodeError):
            vp.promote(sessions, vitals_dir=self.vitals_dir)
        # the untouched sibling file proves no partial application happened
        records = json.loads(self.vitals_file("proj-x").read_text())
        self.assertEqual(records[0]["status"], "active")


# ── search side (pure, unchanged) ────────────────────────────────────────────


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


class SearchVitalsTests(TempDirTestCase):
    def test_raises_on_empty_keywords(self) -> None:
        self.write_vitals(None, [make_vitals_record()])
        with self.assertRaises(ValueError):
            vp.search_vitals(self.vitals_dir, [], include_superseded=False)

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
            self.vitals_dir, ["vitals", "query"], include_superseded=False
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
            self.vitals_dir,
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
            self.vitals_dir,
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
            self.vitals_dir, ["vitals", "query"], include_superseded=False
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
            self.vitals_dir, ["vitals", "query"], include_superseded=True
        )
        self.assertEqual(len(results), 2)

    def test_missing_backlog_file_returns_global_only(self) -> None:
        self.write_vitals(None, [make_vitals_record(text="global vitals query fact")])
        results = vp.search_vitals(
            self.vitals_dir,
            ["vitals", "query"],
            include_superseded=False,
            backlog_slug="nonexistent",
        )
        self.assertEqual(len(results), 1)

    def test_no_matches_returns_empty_list(self) -> None:
        self.write_vitals(None, [make_vitals_record(text="unrelated fact")])
        results = vp.search_vitals(self.vitals_dir, ["zzznomatch"], include_superseded=False)
        self.assertEqual(results, [])

    def test_search_does_not_see_unrelated_backlog_files(self) -> None:
        """Counterpart to the supersede-pass store-wide glob: search loads only
        _global.json plus the one named backlog file."""
        self.write_vitals(
            "proj-secret",
            [
                make_vitals_record(
                    text="vitables query fact from another project",
                    backlog_slug="proj-secret",
                )
            ],
        )
        results = vp.search_vitals(
            self.vitals_dir, ["vitables", "query"], include_superseded=False
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


# ── CLI adapter: session loading, strict-input guard, dispatch order ────────


@pytest.mark.allow_real_subprocess  # invokes the real vitals_promotion.py CLI end to end
class CliPassTests(TempDirTestCase):
    """Disk-level coverage of main(): sessions arrive through
    grill.all_sessions(), and the strict-input guard is CLI-only behavior. The
    service functions above are tested as a library; these keep the adapter
    itself under real integration test."""

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

    def test_dry_run_then_apply_end_to_end(self) -> None:
        self.write_session(promoted())
        dry = self.run_cli()
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn("DRY RUN", dry.stdout)
        self.assertIn("promoted this run:   1", dry.stdout)
        self.assertFalse(self.vitals_dir.exists())

        applied = self.run_cli("--apply")
        self.assertEqual(applied.returncode, 0, applied.stderr)
        self.assertIn("APPLIED", applied.stdout)
        self.assertTrue(self.vitals_file(None).exists())

        second = self.run_cli("--apply")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("promoted this run:   0", second.stdout)

    def test_guard_refuses_unreadable_session_on_both_modes(self) -> None:
        """A corrupt session file must not be silently treated as a deleted one:
        the supersede pass would mark its records 'removed' and persist that."""
        self.write_session(promoted())
        (self.data_dir / "broken.json").write_text("{not json")
        for args in ((), ("--apply",)):
            result = self.run_cli(*args)
            self.assertEqual(result.returncode, 1, f"{args} should be refused")
            self.assertIn("could not be read", result.stderr)
            self.assertFalse(
                self.vitals_dir.exists(), f"{args} must not create the store"
            )

    def test_guard_message_names_the_skipped_count(self) -> None:
        self.write_session(promoted())
        (self.data_dir / "broken.json").write_text("{not json")
        result = self.run_cli()
        self.assertIn("1 of 2", result.stderr)

    def test_guard_passes_once_the_corrupt_file_is_removed(self) -> None:
        self.write_session(promoted())
        broken = self.data_dir / "broken.json"
        broken.write_text("{not json")
        self.assertEqual(self.run_cli("--apply").returncode, 1)
        broken.unlink()
        result = self.run_cli("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.vitals_file(None).exists())

    def test_help_description_matches_docstring_first_line(self) -> None:
        """The literal argparse description must stay identical to the docstring's
        first line -- it is the only thing keeping the two from drifting, since
        gen_interfaces.py can't resolve a derived expression."""
        env = {**os.environ, "COLUMNS": "200"}
        result = subprocess.run(
            [sys.executable, str(Path(__file__).parent / "vitals_promotion.py"), "--help"],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        first_line = vp.__doc__.strip().splitlines()[0]
        self.assertIn(first_line, result.stdout)
        # Flags / Service API sections stay out of user-facing help.
        self.assertNotIn("Service API", result.stdout)
        self.assertNotIn("classify(sessions, *, vitals_dir)", result.stdout)

    def test_search_dispatches_before_session_load(self) -> None:
        """--search reads only the store; a broken session file must never block
        a cheap lookup."""
        self.write_vitals(None, [make_vitals_record(text="the vitals query design")])
        self.write_session(promoted())
        (self.data_dir / "broken.json").write_text("{not json")
        result = self.run_cli("--search", "vitals query")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("the vitals query design", result.stdout)


if __name__ == "__main__":
    unittest.main()
