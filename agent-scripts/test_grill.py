#!/usr/bin/env python3
"""Tests for grill.py. Run with: python3 test_grill.py"""

import argparse
import io
import json
import shutil
import sys
import tempfile
import unittest
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import grill


def ns(**kwargs: object) -> argparse.Namespace:
    kwargs.setdefault("session", None)
    kwargs.setdefault("backlog_slug", None)
    return argparse.Namespace(**kwargs)


class _GrillDataDirFixture:
    """Shared fixture: sandboxed ``grill.DATA_DIR`` plus CLI helpers."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.data_dir = Path(self.tmpdir) / "grill"
        self._patch = patch.object(grill, "DATA_DIR", self.data_dir)
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        shutil.rmtree(self.tmpdir)

    def new_session(self, topic: str = "Auth token design") -> str:
        out = io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", io.StringIO()):
            grill.cmd_new(ns(json=json.dumps({"topic": topic})))
        return out.getvalue().strip()

    def decide(self, slug: str | None = None, **fields: object) -> None:
        payload = {
            "id": "token-storage",
            "question": "Where do tokens live?",
            "decision": "httpOnly cookie",
            **fields,
        }
        with patch("sys.stderr", io.StringIO()):
            grill.cmd_decide(ns(json=json.dumps(payload), session=slug))


class GrillTestCase(_GrillDataDirFixture, unittest.TestCase):

    # ── new ──────────────────────────────────────────────────────────────────

    def test_01_new_creates_session_with_derived_slug(self) -> None:
        slug = self.new_session("Auth token design!")
        self.assertTrue(slug.endswith("-auth-token-design"))
        session = grill.load_session(slug)
        self.assertEqual(session["topic"], "Auth token design!")
        self.assertEqual(session["decisions"], [])

    def test_02_new_duplicate_topic_auto_suffixes(self) -> None:
        first = self.new_session()
        second = self.new_session()
        self.assertNotEqual(first, second)
        self.assertEqual(second, f"{first}-2")

    def test_03_new_missing_topic_rejected(self) -> None:
        err = io.StringIO()
        with patch("sys.stderr", err), self.assertRaises(SystemExit):
            grill.cmd_new(ns(json="{}"))
        self.assertIn("'topic' is required", err.getvalue())

    # ── ask / decide lifecycle ───────────────────────────────────────────────

    def test_04_decide_one_shot_appends_with_null_verdict(self) -> None:
        slug = self.new_session()
        self.decide(slug)
        session = grill.load_session(slug)
        decision = session["decisions"][0]
        self.assertEqual(decision["id"], "token-storage")
        self.assertEqual(decision["source"], "user")
        self.assertIsNone(decision["verdict"])

    def test_05_decide_on_already_decided_rejected(self) -> None:
        slug = self.new_session()
        self.decide(slug)
        err = io.StringIO()
        with patch("sys.stderr", err), self.assertRaises(SystemExit):
            grill.cmd_decide(
                ns(
                    json=json.dumps({"id": "token-storage", "decision": "again"}),
                    session=slug,
                )
            )
        self.assertIn("already decided", err.getvalue())

    def test_05b_ask_registers_open_then_decide_resolves(self) -> None:
        slug = self.new_session()
        with patch("sys.stderr", io.StringIO()):
            grill.cmd_ask(
                ns(
                    json=json.dumps({"id": "token-storage", "question": "Where?"}),
                    session=slug,
                )
            )
        decision = grill.load_session(slug)["decisions"][0]
        self.assertIsNone(decision["decision"])
        self.assertIsNone(decision["source"])
        with patch("sys.stderr", io.StringIO()):
            grill.cmd_decide(
                ns(
                    json=json.dumps(
                        {
                            "id": "token-storage",
                            "decision": "httpOnly cookie",
                            "source": "defaulted",
                        }
                    ),
                    session=slug,
                )
            )
        decision = grill.load_session(slug)["decisions"][0]
        self.assertEqual(decision["decision"], "httpOnly cookie")
        self.assertEqual(decision["source"], "defaulted")

    def test_05c_next_prints_first_open_question(self) -> None:
        slug = self.new_session()
        with patch("sys.stderr", io.StringIO()):
            grill.cmd_ask(
                ns(
                    json=json.dumps({"id": "token-storage", "question": "Where?"}),
                    session=slug,
                )
            )
        out = io.StringIO()
        with patch("sys.stdout", out):
            grill.cmd_next(ns(session=slug))
        self.assertIn("token-storage: Where?", out.getvalue())

    def test_06_decide_invalid_source_rejected(self) -> None:
        slug = self.new_session()
        with self.assertRaises(SystemExit):
            self.decide(slug, source="vibes")

    # ── verdict ──────────────────────────────────────────────────────────────

    def test_07_verified_without_evidence_rejected(self) -> None:
        slug = self.new_session()
        self.decide(slug)
        err = io.StringIO()
        with patch("sys.stderr", err), self.assertRaises(SystemExit):
            grill.cmd_verdict(
                ns(
                    decision_id="token-storage",
                    json=json.dumps({"result": "VERIFIED"}),
                    session=slug,
                )
            )
        self.assertIn("requires 'evidence'", err.getvalue())

    def test_08_verdict_recorded_structured(self) -> None:
        slug = self.new_session()
        self.decide(slug)
        with patch("sys.stderr", io.StringIO()):
            grill.cmd_verdict(
                ns(
                    decision_id="token-storage",
                    json=json.dumps(
                        {"result": "VERIFIED", "evidence": "ran auth tests"}
                    ),
                    session=slug,
                )
            )
        verdict = grill.load_session(slug)["decisions"][0]["verdict"]
        assert verdict is not None
        self.assertEqual(verdict["result"], "VERIFIED")
        self.assertEqual(verdict["evidence"], "ran auth tests")

    def test_09_unverifiable_allowed_without_evidence(self) -> None:
        slug = self.new_session()
        self.decide(slug)
        with patch("sys.stderr", io.StringIO()):
            grill.cmd_verdict(
                ns(
                    decision_id="token-storage",
                    json=json.dumps({"result": "UNVERIFIABLE"}),
                    session=slug,
                )
            )
        verdict = grill.load_session(slug)["decisions"][0]["verdict"]
        assert verdict is not None
        self.assertEqual(verdict["result"], "UNVERIFIABLE")

    def test_09b_verdict_on_open_decision_rejected(self) -> None:
        slug = self.new_session()
        with patch("sys.stderr", io.StringIO()):
            grill.cmd_ask(
                ns(
                    json=json.dumps({"id": "token-storage", "question": "Where?"}),
                    session=slug,
                )
            )
        err = io.StringIO()
        with patch("sys.stderr", err), self.assertRaises(SystemExit):
            grill.cmd_verdict(
                ns(
                    decision_id="token-storage",
                    json=json.dumps({"result": "UNVERIFIABLE"}),
                    session=slug,
                )
            )
        self.assertIn("still open", err.getvalue())

    # ── revise ───────────────────────────────────────────────────────────────

    def test_10_revise_resets_verdict(self) -> None:
        slug = self.new_session()
        self.decide(slug)
        with patch("sys.stderr", io.StringIO()):
            grill.cmd_verdict(
                ns(
                    decision_id="token-storage",
                    json=json.dumps(
                        {"result": "DISPUTED", "evidence": "cookie blocked"}
                    ),
                    session=slug,
                )
            )
            grill.cmd_revise(
                ns(
                    decision_id="token-storage",
                    patch=json.dumps({"decision": "session storage + short TTL"}),
                    session=slug,
                )
            )
        decision = grill.load_session(slug)["decisions"][0]
        self.assertEqual(decision["decision"], "session storage + short TTL")
        self.assertIsNone(decision["verdict"])

    def test_11_revise_unknown_field_rejected(self) -> None:
        slug = self.new_session()
        self.decide(slug)
        err = io.StringIO()
        with patch("sys.stderr", err), self.assertRaises(SystemExit):
            grill.cmd_revise(
                ns(
                    decision_id="token-storage",
                    patch=json.dumps({"verdict": {"result": "VERIFIED"}}),
                    session=slug,
                )
            )
        self.assertIn("cannot revise", err.getvalue())

    # ── session resolution ───────────────────────────────────────────────────

    def test_12_omitted_session_resolves_to_most_recent(self) -> None:
        self.new_session("First topic")
        second = self.new_session("Second topic")
        # both created today; ties break by slug — force distinct updated dates
        session = grill.load_session(second)
        session["updated"] = "2099-01-01"
        grill.save_session(session)
        resolved = grill.resolve_session(None, "test")
        self.assertEqual(resolved["slug"], second)

    def test_12b_same_day_sessions_resolve_to_newest_not_alphabetical(self) -> None:
        # regression: day-granular 'updated' tied for same-day sessions and the
        # alphabetically-later slug won; timestamps must break the tie by recency
        self.new_session("Zeta topic")
        second = self.new_session("Alpha topic")
        resolved = grill.resolve_session(None, "test")
        self.assertEqual(resolved["slug"], second)

    def test_12c_timestamp_sorts_after_legacy_date_only_value(self) -> None:
        first = self.new_session("Legacy session")
        session = grill.load_session(first)
        session["updated"] = "2099-01-01"  # date-only, as pre-timestamp files have
        grill.save_session(session)
        second = self.new_session("New session")
        newer = grill.load_session(second)
        newer["updated"] = "2099-01-01T08:00:00"
        grill.save_session(newer)
        resolved = grill.resolve_session(None, "test")
        self.assertEqual(resolved["slug"], second)

    def test_13_substring_match_unique_and_ambiguous(self) -> None:
        self.new_session("Alpha plan")
        self.new_session("Beta plan")
        resolved = grill.resolve_session("alpha", "test")
        self.assertIn("alpha-plan", resolved["slug"])
        with patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            grill.resolve_session("plan", "test")

    # ── rm ───────────────────────────────────────────────────────────────────

    def test_13b_rm_removes_decision_point(self) -> None:
        slug = self.new_session()
        with patch("sys.stderr", io.StringIO()):
            grill.cmd_ask(
                ns(
                    json=json.dumps({"id": "token-storage", "question": "Where?"}),
                    session=slug,
                )
            )
        err = io.StringIO()
        with patch("sys.stderr", err):
            grill.cmd_rm(ns(decision_id="token-storage", session=slug, verbose=True))
        self.assertIn("removed token-storage (open)", err.getvalue())
        self.assertEqual(grill.load_session(slug)["decisions"], [])

    def test_13c_rm_unknown_id_rejected(self) -> None:
        slug = self.new_session()
        err = io.StringIO()
        with patch("sys.stderr", err), self.assertRaises(SystemExit):
            grill.cmd_rm(ns(decision_id="nope", session=slug))
        self.assertIn("no decision 'nope'", err.getvalue())

    # ── render / plan artifact ───────────────────────────────────────────────

    def test_14_render_status_table_open_questions_and_escaping(self) -> None:
        slug = self.new_session()
        self.decide(slug, decision="cookie | not localStorage")
        with patch("sys.stderr", io.StringIO()):
            grill.cmd_ask(
                ns(
                    json=json.dumps(
                        {"id": "ttl-length", "question": "How long a TTL?"}
                    ),
                    session=slug,
                )
            )
        md = grill.render_markdown(grill.load_session(slug))
        self.assertIn("# Grill status:", md)
        self.assertIn("1/2 decided · 0 verified", md)
        self.assertIn("cookie \\| not localStorage", md)
        self.assertIn("## Open questions", md)
        self.assertIn("**ttl-length**", md)
        self.assertIn("plan: not written yet", md)

    def test_15_plan_records_existing_artifact_path(self) -> None:
        slug = self.new_session()
        artifact = Path(self.tmpdir) / "auth-plan.md"
        err = io.StringIO()
        with patch("sys.stderr", err), self.assertRaises(SystemExit):
            grill.cmd_plan(ns(path=str(artifact), session=slug))
        self.assertIn("not found", err.getvalue())

        artifact.write_text("# Auth plan\n")
        with patch("sys.stderr", io.StringIO()):
            grill.cmd_plan(ns(path=str(artifact), session=slug))
        session = grill.load_session(slug)
        self.assertEqual(session["plan_path"], str(artifact))
        self.assertIn(str(artifact), grill.render_markdown(session))

    # ── corruption ───────────────────────────────────────────────────────────

    def test_16_corrupted_session_fails_loudly(self) -> None:
        slug = self.new_session()
        grill.session_path(slug).write_text("{not json")
        err = io.StringIO()
        with patch("sys.stderr", err), self.assertRaises(SystemExit):
            grill.load_session(slug)
        self.assertIn("corrupted", err.getvalue())

    # ── list ─────────────────────────────────────────────────────────────────

    def test_17_list_prints_one_line_per_session(self) -> None:
        slug = self.new_session("Alpha topic")
        self.decide(slug)
        out = io.StringIO()
        with patch("sys.stdout", out):
            grill.cmd_list(ns())
        line = out.getvalue().strip()
        self.assertTrue(line.startswith(slug))
        self.assertIn("1/1 decided", line)
        self.assertIn("0 verified", line)
        self.assertIn("Alpha topic", line)

    def test_17b_list_empty_prints_nothing(self) -> None:
        out = io.StringIO()
        with patch("sys.stdout", out):
            grill.cmd_list(ns())
        self.assertEqual(out.getvalue(), "")

    # ── explicit JSON null vs. missing field ────────────────────────────────
    # Regression coverage for a real bug: `str(patch.get(key, default))`
    # turns an explicit JSON `null` into the four-character string "None",
    # which then passes any `if not field:` required-field check instead of
    # being rejected. `_text()` must treat null and "missing" identically.

    def test_18_new_explicit_null_topic_rejected_like_missing(self) -> None:
        err = io.StringIO()
        with patch("sys.stderr", err), self.assertRaises(SystemExit):
            grill.cmd_new(ns(json=json.dumps({"topic": None})))
        self.assertIn("'topic' is required", err.getvalue())
        self.assertEqual(grill.all_session_slugs(), [])

    def test_18b_ask_explicit_null_question_rejected_like_missing(self) -> None:
        slug = self.new_session()
        err = io.StringIO()
        with patch("sys.stderr", err), self.assertRaises(SystemExit):
            grill.cmd_ask(
                ns(
                    json=json.dumps({"id": "token-storage", "question": None}),
                    session=slug,
                )
            )
        self.assertIn("'question' is required", err.getvalue())
        self.assertEqual(grill.load_session(slug)["decisions"], [])

    def test_18c_decide_explicit_null_source_falls_back_to_default(self) -> None:
        slug = self.new_session()
        with patch("sys.stderr", io.StringIO()):
            grill.cmd_decide(
                ns(
                    json=json.dumps(
                        {
                            "id": "token-storage",
                            "question": "Where?",
                            "decision": "httpOnly cookie",
                            "source": None,
                        }
                    ),
                    session=slug,
                )
            )
        decision = grill.load_session(slug)["decisions"][0]
        self.assertEqual(decision["source"], "user")

    # ── revise can't blank/reopen a decided item ────────────────────────────
    # Regression coverage for a real bug: `revise <id> '{"decision": null}'`
    # used to pass straight through to `decision.update(patch)`, setting
    # `decision["decision"] = None` — which is exactly what `is_open()`
    # checks for, silently reopening an already-decided item outside of
    # `cmd_decide`'s own validation.

    def test_19_revise_null_decision_rejected(self) -> None:
        slug = self.new_session()
        self.decide(slug)
        err = io.StringIO()
        with patch("sys.stderr", err), self.assertRaises(SystemExit):
            grill.cmd_revise(
                ns(
                    decision_id="token-storage",
                    patch=json.dumps({"decision": None}),
                    session=slug,
                )
            )
        self.assertIn("cannot be blank", err.getvalue())
        decision = grill.load_session(slug)["decisions"][0]
        self.assertFalse(grill.is_open(decision))
        self.assertEqual(decision["decision"], "httpOnly cookie")

    def test_19b_revise_blank_string_decision_rejected(self) -> None:
        slug = self.new_session()
        self.decide(slug)
        err = io.StringIO()
        with patch("sys.stderr", err), self.assertRaises(SystemExit):
            grill.cmd_revise(
                ns(
                    decision_id="token-storage",
                    patch=json.dumps({"decision": "   "}),
                    session=slug,
                )
            )
        self.assertIn("cannot be blank", err.getvalue())

    # ── concurrency: locking prevents lost updates ──────────────────────────

    def test_20_concurrent_mutators_of_same_session_serialize(self) -> None:
        import threading

        slug = self.new_session()
        with patch("sys.stderr", io.StringIO()):
            grill.cmd_ask(
                ns(json=json.dumps({"id": "item-a", "question": "A?"}), session=slug)
            )
            grill.cmd_ask(
                ns(json=json.dumps({"id": "item-b", "question": "B?"}), session=slug)
            )

        def decide_a() -> None:
            with patch("sys.stderr", io.StringIO()):
                grill.cmd_decide(
                    ns(
                        json=json.dumps({"id": "item-a", "decision": "answer A"}),
                        session=slug,
                    )
                )

        def decide_b() -> None:
            with patch("sys.stderr", io.StringIO()):
                grill.cmd_decide(
                    ns(
                        json=json.dumps({"id": "item-b", "decision": "answer B"}),
                        session=slug,
                    )
                )

        with grill.session_lock(slug):
            t1 = threading.Thread(target=decide_a)
            t2 = threading.Thread(target=decide_b)
            t1.start()
            t2.start()
            import time

            time.sleep(0.1)
            self.assertTrue(t1.is_alive())
            self.assertTrue(t2.is_alive())
        t1.join(timeout=5)
        t2.join(timeout=5)
        self.assertFalse(t1.is_alive())
        self.assertFalse(t2.is_alive())

        decisions = {
            d["id"]: d["decision"] for d in grill.load_session(slug)["decisions"]
        }
        self.assertEqual(decisions["item-a"], "answer A")
        self.assertEqual(decisions["item-b"], "answer B")

    def test_20b_concurrent_new_with_same_topic_gets_distinct_slugs(self) -> None:
        import threading

        results: dict[str, str] = {}

        def create(name: str) -> None:
            out = io.StringIO()
            with patch("sys.stdout", out), patch("sys.stderr", io.StringIO()):
                grill.cmd_new(ns(json=json.dumps({"topic": "Same topic"})))
            results[name] = out.getvalue().strip()

        with grill._new_session_lock():
            t1 = threading.Thread(target=create, args=("one",))
            t2 = threading.Thread(target=create, args=("two",))
            t1.start()
            t2.start()
            import time

            time.sleep(0.1)
            self.assertTrue(t1.is_alive())
            self.assertTrue(t2.is_alive())
        t1.join(timeout=5)
        t2.join(timeout=5)

        self.assertNotEqual(results["one"], results["two"])
        self.assertEqual(len(grill.all_session_slugs()), 2)

    # ── pending-execution (clear-and-go) ────────────────────────────────────

    def test_21_pending_plan_silent_when_nothing_pending(self) -> None:
        self.new_session()
        out = io.StringIO()
        with patch("sys.stdout", out):
            grill.cmd_pending_plan(ns(consume=False))
        self.assertEqual(out.getvalue(), "")

    def test_21b_mark_pending_execution_sets_flag(self) -> None:
        slug = self.new_session()
        with patch("sys.stderr", io.StringIO()):
            grill.cmd_mark_pending_execution(ns(session=slug))
        self.assertTrue(grill.load_session(slug)["pending_execution"])

    def test_21c_pending_plan_prints_and_consumes(self) -> None:
        slug = self.new_session()
        artifact = Path(self.tmpdir) / "plan.md"
        artifact.write_text("# Plan\n")
        with patch("sys.stderr", io.StringIO()):
            grill.cmd_plan(ns(path=str(artifact), session=slug))
            grill.cmd_mark_pending_execution(ns(session=slug))

        out = io.StringIO()
        with patch("sys.stdout", out):
            grill.cmd_pending_plan(ns(consume=True))
        self.assertIn(slug, out.getvalue())
        self.assertIn(str(artifact), out.getvalue())
        self.assertFalse(grill.load_session(slug)["pending_execution"])

    def test_21d_pending_plan_without_consume_leaves_flag_set(self) -> None:
        slug = self.new_session()
        with patch("sys.stderr", io.StringIO()):
            grill.cmd_mark_pending_execution(ns(session=slug))

        out = io.StringIO()
        with patch("sys.stdout", out):
            grill.cmd_pending_plan(ns(consume=False))
        self.assertIn(slug, out.getvalue())
        self.assertTrue(grill.load_session(slug)["pending_execution"])

    def test_21e_pending_plan_picks_most_recently_updated(self) -> None:
        first = self.new_session("First topic")
        second = self.new_session("Second topic")
        with patch("sys.stderr", io.StringIO()):
            grill.cmd_mark_pending_execution(ns(session=first))
            grill.cmd_mark_pending_execution(ns(session=second))
        session = grill.load_session(second)
        session["updated"] = "2099-01-01T00:00:00"
        grill.save_session(session)

        out = io.StringIO()
        with patch("sys.stdout", out):
            grill.cmd_pending_plan(ns(consume=False))
        self.assertIn(second, out.getvalue())
        self.assertNotIn(first, out.getvalue())

    def test_21f_mark_pending_execution_records_backlog_slug(self) -> None:
        slug = self.new_session()
        with patch("sys.stderr", io.StringIO()):
            grill.cmd_mark_pending_execution(
                ns(session=slug, backlog_slug="iron-lb-example")
            )
        self.assertEqual(grill.load_session(slug)["backlog_slug"], "iron-lb-example")

    def test_21g_mark_pending_execution_rejects_invalid_backlog_slug(self) -> None:
        slug = self.new_session()
        with patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            grill.cmd_mark_pending_execution(
                ns(session=slug, backlog_slug="Not Kebab Case")
            )

    def test_21h_pending_plan_prints_backlog_item_resume_line(self) -> None:
        slug = self.new_session()
        with patch("sys.stderr", io.StringIO()):
            grill.cmd_mark_pending_execution(
                ns(session=slug, backlog_slug="iron-lb-example")
            )

        out = io.StringIO()
        with patch("sys.stdout", out):
            grill.cmd_pending_plan(ns(consume=False))
        self.assertIn("/backlog-item iron-lb-example", out.getvalue())

    def test_21i_pending_plan_omits_backlog_item_line_when_absent(self) -> None:
        slug = self.new_session()
        with patch("sys.stderr", io.StringIO()):
            grill.cmd_mark_pending_execution(ns(session=slug))

        out = io.StringIO()
        with patch("sys.stdout", out):
            grill.cmd_pending_plan(ns(consume=False))
        self.assertNotIn("/backlog-item", out.getvalue())

    # ── depends_on / frontier ────────────────────────────────────────────────

    def ask(self, slug: str | None = None, **fields: object) -> None:
        payload = {
            "id": "token-storage",
            "question": "Where do tokens live?",
            **fields,
        }
        with patch("sys.stderr", io.StringIO()):
            grill.cmd_ask(ns(json=json.dumps(payload), session=slug))

    def test_22_ask_accepts_and_stores_depends_on(self) -> None:
        slug = self.new_session()
        self.ask(slug, id="token-storage", question="Where?")
        self.ask(
            slug,
            id="ttl-length",
            question="How long?",
            depends_on=["token-storage"],
        )
        decision = grill.find_decision(grill.load_session(slug), "ttl-length", "test")
        self.assertEqual(decision["depends_on"], ["token-storage"])

    def test_22b_ask_defaults_depends_on_to_empty_list(self) -> None:
        slug = self.new_session()
        self.ask(slug)
        decision = grill.find_decision(
            grill.load_session(slug), "token-storage", "test"
        )
        self.assertEqual(decision["depends_on"], [])

    def test_22c_decide_create_path_accepts_depends_on(self) -> None:
        slug = self.new_session()
        self.ask(slug, id="token-storage", question="Where?")
        self.decide(
            slug, id="ttl-length", question="How long?", depends_on=["token-storage"]
        )
        decision = grill.find_decision(grill.load_session(slug), "ttl-length", "test")
        self.assertEqual(decision["depends_on"], ["token-storage"])

    def test_23_depends_on_rejects_unknown_id(self) -> None:
        slug = self.new_session()
        err = io.StringIO()
        with patch("sys.stderr", err), self.assertRaises(SystemExit):
            grill.cmd_ask(
                ns(
                    json=json.dumps(
                        {
                            "id": "token-storage",
                            "question": "Where?",
                            "depends_on": ["nonexistent"],
                        }
                    ),
                    session=slug,
                )
            )
        self.assertIn("unknown decision id", err.getvalue())
        self.assertEqual(grill.load_session(slug)["decisions"], [])

    def test_23b_depends_on_rejects_self_reference(self) -> None:
        slug = self.new_session()
        err = io.StringIO()
        with patch("sys.stderr", err), self.assertRaises(SystemExit):
            grill.cmd_ask(
                ns(
                    json=json.dumps(
                        {
                            "id": "token-storage",
                            "question": "Where?",
                            "depends_on": ["token-storage"],
                        }
                    ),
                    session=slug,
                )
            )
        self.assertIn("own id", err.getvalue())

    def test_23c_depends_on_bare_string_rejected(self) -> None:
        slug = self.new_session()
        err = io.StringIO()
        with patch("sys.stderr", err), self.assertRaises(SystemExit):
            grill.cmd_ask(
                ns(
                    json=json.dumps(
                        {
                            "id": "token-storage",
                            "question": "Where?",
                            "depends_on": "token-storage",
                        }
                    ),
                    session=slug,
                )
            )
        self.assertIn("must be a list of strings", err.getvalue())

    def test_23d_depends_on_deduped_order_preserving(self) -> None:
        slug = self.new_session()
        self.ask(slug, id="token-storage", question="Where?")
        self.ask(slug, id="second", question="Second?")
        self.ask(
            slug,
            id="third",
            question="Third?",
            depends_on=["token-storage", "second", "token-storage"],
        )
        decision = grill.find_decision(grill.load_session(slug), "third", "test")
        self.assertEqual(decision["depends_on"], ["token-storage", "second"])

    def test_24_decide_existing_updates_depends_on(self) -> None:
        slug = self.new_session()
        self.ask(slug, id="token-storage", question="Where?")
        self.ask(slug, id="ttl-length", question="How long?")
        self.decide(slug, id="ttl-length", depends_on=["token-storage"])
        decision = grill.find_decision(grill.load_session(slug), "ttl-length", "test")
        self.assertEqual(decision["depends_on"], ["token-storage"])

    def test_24b_decide_existing_omitted_depends_on_preserved(self) -> None:
        slug = self.new_session()
        self.ask(slug, id="token-storage", question="Where?")
        self.ask(
            slug, id="ttl-length", question="How long?", depends_on=["token-storage"]
        )
        self.decide(slug, id="ttl-length")
        decision = grill.find_decision(grill.load_session(slug), "ttl-length", "test")
        self.assertEqual(decision["depends_on"], ["token-storage"])

    def test_25_decide_existing_rejects_cycle(self) -> None:
        slug = self.new_session()
        self.ask(slug, id="decision-a", question="A?")
        self.ask(slug, id="decision-b", question="B?", depends_on=["decision-a"])
        err = io.StringIO()
        with patch("sys.stderr", err), self.assertRaises(SystemExit):
            grill.cmd_decide(
                ns(
                    json=json.dumps(
                        {
                            "id": "decision-a",
                            "decision": "chosen",
                            "depends_on": ["decision-b"],
                        }
                    ),
                    session=slug,
                )
            )
        self.assertIn("cycle", err.getvalue())

    def test_26_revise_sets_depends_on(self) -> None:
        slug = self.new_session()
        self.ask(slug, id="token-storage", question="Where?")
        self.decide(slug, id="token-storage")
        self.ask(slug, id="ttl-length", question="How long?")
        self.decide(slug, id="ttl-length")
        with patch("sys.stderr", io.StringIO()):
            grill.cmd_revise(
                ns(
                    decision_id="ttl-length",
                    patch=json.dumps({"depends_on": ["token-storage"]}),
                    session=slug,
                )
            )
        decision = grill.find_decision(grill.load_session(slug), "ttl-length", "test")
        self.assertEqual(decision["depends_on"], ["token-storage"])

    def test_26b_revise_rejects_cycle(self) -> None:
        slug = self.new_session()
        self.ask(slug, id="decision-a", question="A?")
        self.decide(slug, id="decision-a")
        self.ask(slug, id="decision-b", question="B?", depends_on=["decision-a"])
        self.decide(slug, id="decision-b")
        err = io.StringIO()
        with patch("sys.stderr", err), self.assertRaises(SystemExit):
            grill.cmd_revise(
                ns(
                    decision_id="decision-a",
                    patch=json.dumps({"depends_on": ["decision-b"]}),
                    session=slug,
                )
            )
        self.assertIn("cycle", err.getvalue())

    def test_26c_revise_depends_on_does_not_reset_verdict(self) -> None:
        slug = self.new_session()
        self.ask(slug, id="token-storage", question="Where?")
        self.decide(slug, id="token-storage")
        self.ask(slug, id="ttl-length", question="How long?")
        self.decide(slug, id="ttl-length")
        with patch("sys.stderr", io.StringIO()):
            grill.cmd_verdict(
                ns(
                    decision_id="ttl-length",
                    json=json.dumps({"result": "VERIFIED", "evidence": "checked"}),
                    session=slug,
                )
            )
            grill.cmd_revise(
                ns(
                    decision_id="ttl-length",
                    patch=json.dumps({"depends_on": ["token-storage"]}),
                    session=slug,
                )
            )
        decision = grill.find_decision(grill.load_session(slug), "ttl-length", "test")
        self.assertIsNotNone(decision["verdict"])

    def test_26d_revise_unrelated_field_with_stored_dangling_dep_succeeds(
        self,
    ) -> None:
        slug = self.new_session()
        self.ask(slug, id="token-storage", question="Where?")
        self.ask(
            slug, id="ttl-length", question="How long?", depends_on=["token-storage"]
        )
        with patch("sys.stderr", io.StringIO()):
            grill.cmd_rm(ns(decision_id="token-storage", session=slug, force=True))
        with patch("sys.stderr", io.StringIO()):
            grill.cmd_revise(
                ns(
                    decision_id="ttl-length",
                    patch=json.dumps({"question": "How long exactly?"}),
                    session=slug,
                )
            )
        decision = grill.find_decision(grill.load_session(slug), "ttl-length", "test")
        self.assertEqual(decision["question"], "How long exactly?")

    def test_27_session_without_depends_on_key_behaves_as_empty(self) -> None:
        slug = self.new_session()
        session = grill.load_session(slug)
        session["decisions"].append(
            {
                "id": "legacy",
                "question": "Legacy?",
                "reasoning": "",
                "decision": None,
                "source": None,
                "verdict": None,
            }
        )
        grill.save_session(session)
        session = grill.load_session(slug)
        decision = grill.find_decision(session, "legacy", "test")
        self.assertEqual(decision.get("depends_on", []), [])
        ready = grill.frontier(session)
        self.assertIn("legacy", [d["id"] for d in ready])

    def test_28_ask_bad_id_among_good_aborts_atomically(self) -> None:
        slug = self.new_session()
        self.ask(slug, id="token-storage", question="Where?")
        err = io.StringIO()
        with patch("sys.stderr", err), self.assertRaises(SystemExit):
            self.ask(
                slug,
                id="ttl-length",
                question="How long?",
                depends_on=["token-storage", "nonexistent"],
            )
        session = grill.load_session(slug)
        self.assertEqual(len(session["decisions"]), 1)

    def test_29_rm_rejects_when_referenced(self) -> None:
        slug = self.new_session()
        self.ask(slug, id="token-storage", question="Where?")
        self.ask(
            slug, id="ttl-length", question="How long?", depends_on=["token-storage"]
        )
        err = io.StringIO()
        with patch("sys.stderr", err), self.assertRaises(SystemExit):
            grill.cmd_rm(ns(decision_id="token-storage", session=slug, force=False))
        self.assertIn("still depended on by", err.getvalue())
        self.assertIn("ttl-length", err.getvalue())

    def test_29b_rm_succeeds_when_unreferenced(self) -> None:
        slug = self.new_session()
        self.ask(slug, id="token-storage", question="Where?")
        with patch("sys.stderr", io.StringIO()):
            grill.cmd_rm(ns(decision_id="token-storage", session=slug, force=False))
        self.assertEqual(grill.load_session(slug)["decisions"], [])

    def test_29c_rm_force_bypasses_and_dangles(self) -> None:
        slug = self.new_session()
        self.ask(slug, id="decision-c", question="C?")
        self.ask(slug, id="decision-b", question="B?", depends_on=["decision-c"])
        self.ask(slug, id="decision-a", question="A?", depends_on=["decision-b"])
        err = io.StringIO()
        with patch("sys.stderr", err), self.assertRaises(SystemExit):
            grill.cmd_rm(ns(decision_id="decision-c", session=slug, force=False))
        self.assertIn("decision-b", err.getvalue())
        with patch("sys.stderr", io.StringIO()):
            grill.cmd_rm(ns(decision_id="decision-c", session=slug, force=True))
        session = grill.load_session(slug)
        self.assertNotIn("decision-c", [d["id"] for d in session["decisions"]])
        b = grill.find_decision(session, "decision-b", "test")
        self.assertEqual(b["depends_on"], ["decision-c"])

    def test_30_frontier_3_chain_direct_only_check(self) -> None:
        slug = self.new_session()
        self.ask(slug, id="decision-a", question="A?")
        self.ask(slug, id="decision-b", question="B?", depends_on=["decision-a"])
        self.ask(slug, id="decision-c", question="C?", depends_on=["decision-b"])
        session = grill.load_session(slug)
        self.assertEqual([d["id"] for d in grill.frontier(session)], ["decision-a"])

        self.decide(slug, id="decision-a")
        session = grill.load_session(slug)
        self.assertEqual([d["id"] for d in grill.frontier(session)], ["decision-b"])

        self.decide(slug, id="decision-b")
        session = grill.load_session(slug)
        self.assertEqual([d["id"] for d in grill.frontier(session)], ["decision-c"])

    def test_31_revise_cycle_3_node_transitive(self) -> None:
        slug = self.new_session()
        self.ask(slug, id="decision-a", question="A?")
        self.decide(slug, id="decision-a")
        self.ask(slug, id="decision-b", question="B?", depends_on=["decision-a"])
        self.decide(slug, id="decision-b")
        self.ask(slug, id="decision-c", question="C?", depends_on=["decision-b"])
        self.decide(slug, id="decision-c")
        err = io.StringIO()
        with patch("sys.stderr", err), self.assertRaises(SystemExit):
            grill.cmd_revise(
                ns(
                    decision_id="decision-a",
                    patch=json.dumps({"depends_on": ["decision-c"]}),
                    session=slug,
                )
            )
        self.assertIn("cycle", err.getvalue())

    def test_31b_revise_unrelated_pre_existing_cycle_terminates(self) -> None:
        slug = self.new_session()
        self.ask(slug, id="decision-a", question="A?")
        self.decide(slug, id="decision-a")
        self.ask(slug, id="decision-x", question="X?")
        self.decide(slug, id="decision-x")
        self.ask(slug, id="decision-y", question="Y?")
        self.decide(slug, id="decision-y")
        session = grill.load_session(slug)
        x = grill.find_decision(session, "decision-x", "test")
        y = grill.find_decision(session, "decision-y", "test")
        x["depends_on"] = ["decision-y"]
        y["depends_on"] = ["decision-x"]
        grill.save_session(session)

        with patch("sys.stderr", io.StringIO()):
            grill.cmd_revise(
                ns(
                    decision_id="decision-a",
                    patch=json.dumps({"depends_on": ["decision-x"]}),
                    session=slug,
                )
            )
        session = grill.load_session(slug)
        a = grill.find_decision(session, "decision-a", "test")
        self.assertEqual(a["depends_on"], ["decision-x"])

    def test_32_dangling_dependency_note_via_frontier_and_next(self) -> None:
        slug = self.new_session()
        self.ask(slug, id="decision-a", question="A?")
        self.ask(slug, id="decision-b", question="B?", depends_on=["decision-a"])
        with patch("sys.stderr", io.StringIO()):
            grill.cmd_rm(ns(decision_id="decision-a", session=slug, force=True))

        session = grill.load_session(slug)
        b = grill.find_decision(session, "decision-b", "test")
        self.assertEqual(grill._dangling_deps(b, session), ["decision-a"])

        out = io.StringIO()
        err = io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", err):
            grill.cmd_frontier(ns(session=slug, verbose=True))
        self.assertIn("decision-b", out.getvalue())
        self.assertIn("no longer exists", err.getvalue())

        out2 = io.StringIO()
        err2 = io.StringIO()
        with patch("sys.stdout", out2), patch("sys.stderr", err2):
            grill.cmd_next(ns(session=slug, verbose=True))
        self.assertIn("decision-b", out2.getvalue())
        self.assertIn("no longer exists", err2.getvalue())

    def test_33_frontier_returns_no_deps_and_all_decided(self) -> None:
        slug = self.new_session()
        self.ask(slug, id="decision-a", question="A?")
        self.decide(slug, id="decision-a")
        self.ask(slug, id="decision-b", question="B?", depends_on=["decision-a"])
        self.ask(slug, id="decision-c", question="C?")
        session = grill.load_session(slug)
        ready = {d["id"] for d in grill.frontier(session)}
        self.assertEqual(ready, {"decision-b", "decision-c"})

    def test_33b_frontier_excludes_open_dependency(self) -> None:
        slug = self.new_session()
        self.ask(slug, id="decision-a", question="A?")
        self.ask(slug, id="decision-b", question="B?", depends_on=["decision-a"])
        session = grill.load_session(slug)
        ready = {d["id"] for d in grill.frontier(session)}
        self.assertEqual(ready, {"decision-a"})

    def test_33c_frontier_includes_dangling_dependency(self) -> None:
        slug = self.new_session()
        session = grill.load_session(slug)
        session["decisions"].append(
            {
                "id": "decision-b",
                "question": "B?",
                "reasoning": "",
                "decision": None,
                "source": None,
                "verdict": None,
                "depends_on": ["ghost"],
            }
        )
        grill.save_session(session)
        session = grill.load_session(slug)
        ready = {d["id"] for d in grill.frontier(session)}
        self.assertEqual(ready, {"decision-b"})

    def test_33d_frontier_prints_no_open_questions(self) -> None:
        slug = self.new_session()
        out = io.StringIO()
        with patch("sys.stdout", out):
            grill.cmd_frontier(ns(session=slug, verbose=False))
        self.assertIn("(no open questions)", out.getvalue())

    def test_33e_frontier_prints_all_blocked_distinct_message(self) -> None:
        slug = self.new_session()
        session = grill.load_session(slug)
        session["decisions"] = [
            {
                "id": "decision-a",
                "question": "A?",
                "reasoning": "",
                "decision": None,
                "source": None,
                "verdict": None,
                "depends_on": ["decision-b"],
            },
            {
                "id": "decision-b",
                "question": "B?",
                "reasoning": "",
                "decision": None,
                "source": None,
                "verdict": None,
                "depends_on": ["decision-a"],
            },
        ]
        grill.save_session(session)
        out = io.StringIO()
        with patch("sys.stdout", out):
            grill.cmd_frontier(ns(session=slug, verbose=False))
        self.assertIn("(2 open, all blocked)", out.getvalue())

    def test_34_next_returns_first_frontier_item_not_first_open(self) -> None:
        slug = self.new_session()
        self.ask(slug, id="decision-a", question="A?")
        self.ask(slug, id="decision-b", question="B?", depends_on=["decision-a"])
        session = grill.load_session(slug)
        by_id = {d["id"]: d for d in session["decisions"]}
        # reorder so the blocked decision (b) precedes the frontier-ready one (a)
        session["decisions"] = [by_id["decision-b"], by_id["decision-a"]]
        grill.save_session(session)

        out = io.StringIO()
        with patch("sys.stdout", out):
            grill.cmd_next(ns(session=slug, verbose=False))
        self.assertIn("decision-a", out.getvalue())
        self.assertNotIn("decision-b:", out.getvalue())


class ServiceApiTests(_GrillDataDirFixture, unittest.TestCase):
    """Direct service-API tests — call the functions, never argv.

    Covers the typed error classes, cycle detection, stale-snapshot safety,
    cross-thread lock serialization, and data_dir injection.
    """

    def _err(self, fn: Callable[[], object]) -> tuple[type[Exception], str]:
        with self.assertRaises(Exception) as cm:
            fn()
        return type(cm.exception), str(cm.exception)

    def test_service_open_roundtrip_and_all_sessions(self) -> None:
        slug = self.new_session()
        session = grill.open_session(slug, data_dir=self.data_dir)
        self.assertEqual(session["topic"], "Auth token design")
        self.assertEqual(
            [s["slug"] for s in grill.all_sessions(data_dir=self.data_dir)], [slug]
        )

    def test_service_open_missing_slug_raises_session_not_found(self) -> None:
        err_type, msg = self._err(
            lambda: grill.open_session("no-such-session", data_dir=self.data_dir)
        )
        self.assertIs(err_type, grill.SessionNotFoundError)
        self.assertIn("no-such-session", msg)

    def test_service_open_corrupt_file_raises_session_file_error(self) -> None:
        slug = self.new_session()
        (self.data_dir / f"{slug}.json").write_text("{not json")
        err_type, _ = self._err(
            lambda: grill.open_session(slug, data_dir=self.data_dir)
        )
        self.assertIs(err_type, grill.SessionFileError)

    def test_service_open_invalid_slug_format_raises(self) -> None:
        err_type, _ = self._err(
            lambda: grill.open_session("../evil", data_dir=self.data_dir)
        )
        self.assertIs(err_type, grill.SessionNotFoundError)

    def test_service_ask_duplicate_id_raises_validation_error(self) -> None:
        slug = self.new_session()
        grill.ask_decision(
            slug, "q1", {"question": "One?"}, data_dir=self.data_dir
        )
        err_type, msg = self._err(
            lambda: grill.ask_decision(
                slug, "q1", {"question": "Again?"}, data_dir=self.data_dir
            )
        )
        self.assertIs(err_type, grill.ValidationError)
        self.assertIn("duplicate decision id: q1", msg)

    def test_service_ask_depends_on_unknown_id_raises(self) -> None:
        slug = self.new_session()
        err_type, msg = self._err(
            lambda: grill.ask_decision(
                slug, "q1", {"question": "One?", "depends_on": ["ghost"]},
                data_dir=self.data_dir,
            )
        )
        self.assertIs(err_type, grill.ValidationError)
        self.assertIn("unknown decision id(s): ghost", msg)

    def test_service_decide_create_and_resolve_paths_match_cli(self) -> None:
        slug = self.new_session()
        d = grill.record_decision(
            slug,
            "one-shot",
            {"question": "Q?", "decision": "D", "source": "tested"},
            data_dir=self.data_dir,
        )
        self.assertEqual(d["decision"], "D")
        self.assertEqual(d["source"], "tested")
        self.assertIsNone(d["verdict"])
        # resolve path on an ask-registered open decision
        grill.ask_decision(slug, "open-q", {"question": "Open?"},
                           data_dir=self.data_dir)
        d2 = grill.record_decision(
            slug, "open-q", {"decision": "Now D"}, data_dir=self.data_dir
        )
        self.assertEqual(d2["decision"], "Now D")
        self.assertEqual(grill.load_session(slug)["decisions"][1]["question"], "Open?")

    def test_service_decide_already_decided_raises(self) -> None:
        slug = self.new_session()
        grill.record_decision(
            slug, "d1", {"question": "Q?", "decision": "D"},
            data_dir=self.data_dir,
        )
        err_type, msg = self._err(
            lambda: grill.record_decision(
                slug, "d1", {"decision": "D2"}, data_dir=self.data_dir
            )
        )
        self.assertIs(err_type, grill.ValidationError)
        self.assertIn("already decided", msg)

    def test_service_revise_resets_verdict_and_rejects_unknown_field(self) -> None:
        slug = self.new_session()
        grill.record_decision(
            slug, "d1", {"question": "Q?", "decision": "D"},
            data_dir=self.data_dir,
        )
        grill.record_verdict(
            slug, "d1", {"result": "UNVERIFIABLE", "evidence": "n/a"},
            data_dir=self.data_dir,
        )
        d = grill.revise_decision(
            slug, "d1", {"decision": "D-revised"}, data_dir=self.data_dir
        )
        self.assertIsNone(d["verdict"])
        err_type, msg = self._err(
            lambda: grill.revise_decision(
                slug, "d1", {"bogus": "x"}, data_dir=self.data_dir
            )
        )
        self.assertIs(err_type, grill.ValidationError)
        self.assertIn("cannot revise field(s): bogus", msg)
        err_type, _ = self._err(
            lambda: grill.revise_decision(
                slug, "d1", {"id": "rename"}, data_dir=self.data_dir
            )
        )
        self.assertIs(err_type, grill.ValidationError)

    def test_service_revise_cycle_raises_cycle_error(self) -> None:
        slug = self.new_session()
        grill.ask_decision(slug, "alpha", {"question": "A?"}, data_dir=self.data_dir)
        grill.ask_decision(slug, "beta", {"question": "B?", "depends_on": ["alpha"]},
                           data_dir=self.data_dir)
        err_type, msg = self._err(
            lambda: grill.revise_decision(
                slug, "alpha", {"depends_on": ["beta"]}, data_dir=self.data_dir
            )
        )
        self.assertIs(err_type, grill.CycleError)
        self.assertIn("would introduce a cycle", msg)

    def test_service_revise_3node_transitive_cycle_raises(self) -> None:
        slug = self.new_session()
        grill.ask_decision(slug, "alpha", {"question": "A?"}, data_dir=self.data_dir)
        grill.ask_decision(slug, "beta", {"question": "B?", "depends_on": ["alpha"]},
                           data_dir=self.data_dir)
        grill.ask_decision(slug, "gamma", {"question": "C?", "depends_on": ["beta"]},
                           data_dir=self.data_dir)
        err_type, _ = self._err(
            lambda: grill.revise_decision(
                slug, "alpha", {"depends_on": ["gamma"]}, data_dir=self.data_dir
            )
        )
        self.assertIs(err_type, grill.CycleError)

    def test_service_revise_preexisting_dangling_dep_succeeds(self) -> None:
        slug = self.new_session()
        grill.ask_decision(slug, "alpha", {"question": "A?"}, data_dir=self.data_dir)
        grill.ask_decision(slug, "beta", {"question": "B?", "depends_on": ["alpha"]},
                           data_dir=self.data_dir)
        grill.record_decision(
            slug, "alpha", {"decision": "A decided"}, data_dir=self.data_dir
        )
        # --force removal leaves beta's depends_on dangling, as cmd_rm --force does
        with patch("sys.stderr", io.StringIO()):
            grill.cmd_rm(ns(decision_id="alpha", session=slug, force=True))
        # beta is still revisable — the dangling dep is never re-validated
        d = grill.revise_decision(
            slug, "beta", {"question": "B revised?"}, data_dir=self.data_dir
        )
        self.assertEqual(d["question"], "B revised?")

    def test_service_verdict_requires_evidence_for_verified(self) -> None:
        slug = self.new_session()
        grill.record_decision(
            slug, "d1", {"question": "Q?", "decision": "D"},
            data_dir=self.data_dir,
        )
        err_type, msg = self._err(
            lambda: grill.record_verdict(
                slug, "d1", {"result": "VERIFIED", "evidence": ""},
                data_dir=self.data_dir,
            )
        )
        self.assertIs(err_type, grill.ValidationError)
        self.assertIn("requires 'evidence'", msg)

    def test_service_verdict_on_open_decision_raises(self) -> None:
        slug = self.new_session()
        grill.ask_decision(slug, "q1", {"question": "Q?"}, data_dir=self.data_dir)
        err_type, msg = self._err(
            lambda: grill.record_verdict(
                slug, "q1", {"result": "VERIFIED", "evidence": "e"},
                data_dir=self.data_dir,
            )
        )
        self.assertIs(err_type, grill.ValidationError)
        self.assertIn("still open", msg)

    def test_service_verdict_date_always_stamped(self) -> None:
        slug = self.new_session()
        grill.record_decision(
            slug, "d1", {"question": "Q?", "decision": "D"},
            data_dir=self.data_dir,
        )
        d = grill.record_verdict(
            slug, "d1",
            {"result": "VERIFIED", "evidence": "e", "date": "1999-01-01"},
            data_dir=self.data_dir,
        )
        self.assertEqual(d["verdict"]["date"], grill.today())

    def test_service_mutation_sees_concurrent_writers_change(self) -> None:
        slug = self.new_session()
        grill.ask_decision(slug, "q1", {"question": "Q?"}, data_dir=self.data_dir)
        stale = grill.open_session(slug, data_dir=self.data_dir)  # pre-mutation read
        grill.record_decision(
            slug, "q1", {"decision": "Other writer decided"},
            data_dir=self.data_dir,
        )
        # A verdict on q1 must succeed: the mutation reloads under the lock,
        # so the pre-mutation "still open" snapshot is never consulted.
        d = grill.record_verdict(
            slug, "q1", {"result": "UNVERIFIABLE", "evidence": "n/a"},
            data_dir=self.data_dir,
        )
        self.assertEqual(d["verdict"]["result"], "UNVERIFIABLE")
        session = grill.open_session(slug, data_dir=self.data_dir)
        target = grill._get_decision(session, "q1")
        self.assertEqual(target["decision"], "Other writer decided")
        del stale

    def test_service_mutations_serialize_across_threads(self) -> None:
        import threading

        slug = self.new_session()
        grill.ask_decision(slug, "q1", {"question": "Q?"}, data_dir=self.data_dir)
        errors: list[Exception] = []

        def worker(k: int) -> None:
            try:
                grill.record_decision(
                    slug, f"w{k}",
                    {"question": f"W{k}?", "decision": f"D{k}"},
                    data_dir=self.data_dir,
                )
            except Exception as e:  # noqa: BLE001 — recorded and asserted below
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(k,)) for k in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        session = grill.open_session(slug, data_dir=self.data_dir)
        self.assertEqual(len(session["decisions"]), 6)  # q1 + w0..w4, none lost

    def test_service_all_sessions_skips_corrupt_with_warning(self) -> None:
        slug = self.new_session()
        (self.data_dir / "broken.json").write_text("{bad")
        err = io.StringIO()
        with patch("sys.stderr", err):
            sessions = grill.all_sessions(data_dir=self.data_dir)
        self.assertEqual([s["slug"] for s in sessions], [slug])
        self.assertIn("skipping unreadable grill session", err.getvalue())

    def test_service_frontier_of_matches_frontier_alias(self) -> None:
        slug = self.new_session()
        grill.ask_decision(slug, "alpha", {"question": "A?"}, data_dir=self.data_dir)
        grill.ask_decision(slug, "beta", {"question": "B?", "depends_on": ["alpha"]},
                           data_dir=self.data_dir)
        session = grill.open_session(slug, data_dir=self.data_dir)
        self.assertEqual(
            [d["id"] for d in grill.frontier_of(session)], ["alpha"]
        )
        self.assertEqual(grill.frontier_of(session), grill.frontier(session))


class ServiceDataDirInjectionTests(unittest.TestCase):
    """Injected data_dir wins over the module global — never the reverse.

    Deliberately NOT under GrillTestCase's DATA_DIR patch: with the global
    patched, a fallback to grill.DATA_DIR would be indistinguishable from
    correct injection. Here grill.DATA_DIR is whatever the environment says
    (the sandboxed HOME under pytest), and the session must land only in
    the injected tempdir.
    """

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.data_dir = self.tmpdir / "grill"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir)

    def test_service_data_dir_injection_never_touches_real_data_dir(self) -> None:
        session: grill.Session = {
            "schema_version": grill.SCHEMA_VERSION,
            "slug": "injection-check",
            "topic": "injection",
            "created": grill.now(),
            "updated": grill.now(),
            "plan_path": None,
            "pending_execution": False,
            "backlog_slug": None,
            "decisions": [],
        }
        grill.save_session(session, data_dir=self.data_dir)
        loaded = grill.open_session("injection-check", data_dir=self.data_dir)
        self.assertEqual(loaded["topic"], "injection")
        grill.ask_decision(
            "injection-check", "q1", {"question": "Q?"}, data_dir=self.data_dir
        )
        self.assertTrue((self.data_dir / "injection-check.json").exists())
        self.assertFalse((grill.DATA_DIR / "injection-check.json").exists())


class ParserVerbosityTests(unittest.TestCase):
    def test_flags_parse_after_every_leaf_subcommand(self) -> None:
        # A leaf added later without an entry here silently loses coverage.
        cases = {
            "new": ("cmd_new", ["{}"]),
            "ask": ("cmd_ask", ["{}"]),
            "decide": ("cmd_decide", ["{}"]),
            "revise": ("cmd_revise", ["dummy-id", "{}"]),
            "rm": ("cmd_rm", ["dummy-id"]),
            "verdict": ("cmd_verdict", ["dummy-id", "{}"]),
            "plan": ("cmd_plan", ["dummy-path"]),
            "mark-pending-execution": ("cmd_mark_pending_execution", []),
            "pending-plan": ("cmd_pending_plan", []),
            "next": ("cmd_next", []),
            "frontier": ("cmd_frontier", []),
            "render": ("cmd_render", []),
            "list": ("cmd_list", []),
            "show": ("cmd_show", []),
        }
        for cmd, (target, extra) in cases.items():
            argv = ["grill.py", cmd, *extra, "-q"]
            with (
                patch.object(grill, target) as mock_cmd,
                patch.object(sys, "argv", argv),
            ):
                grill.main()
            self.assertTrue(mock_cmd.call_args.args[0].quiet)


class DataDirSelfEnsureTests(GrillTestCase):
    """DATA_DIR is shared artifact storage, so grill.py owns creating it.

    Agents write plan/spec .md files into the same directory with their own
    file tools, and were running `mkdir -p` defensively first. Every grill.py
    invocation ensures the directory, so that reflex has nothing left to
    guard against.
    """

    def test_ensure_data_dir_creates_missing_directory(self) -> None:
        self.assertFalse(self.data_dir.exists())
        grill.ensure_data_dir()
        self.assertTrue(self.data_dir.is_dir())

    def test_ensure_data_dir_is_idempotent(self) -> None:
        grill.ensure_data_dir()
        marker = self.data_dir / "keep.md"
        marker.write_text("artifact")
        grill.ensure_data_dir()
        self.assertEqual(marker.read_text(), "artifact")

    def test_read_only_subcommand_still_creates_the_directory(self) -> None:
        """`list` never writes a session, but must still leave DATA_DIR there."""
        self.assertFalse(self.data_dir.exists())
        with (
            patch.object(sys, "argv", ["grill.py", "list"]),
            patch("sys.stdout", io.StringIO()),
            patch("sys.stderr", io.StringIO()),
        ):
            grill.main()
        self.assertTrue(self.data_dir.is_dir())


if __name__ == "__main__":
    unittest.main(verbosity=1)
