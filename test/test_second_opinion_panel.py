#!/usr/bin/env python3
"""Panel-mode tests for second_opinion.py's ``panel`` subcommand.

stdlib unittest, dependency-free, same conventions as test_second_opinion.py.
The panel runs each backend call as a child ``_panel-worker`` process; these
tests mock that boundary — ``_spawn_worker`` (which wraps ``subprocess.Popen``)
and ``_collect_children`` (which drains the child pipes) — plus
``llm_backends.eligibility_report``, so no real subprocess ever runs and no
test calls a real backend.
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent-scripts"))

import llm_backends  # noqa: E402
import second_opinion  # noqa: E402


class _FakeProc:
    """Hashable stand-in for a spawned Popen child."""

    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode

    def poll(self) -> int:
        """A collected child reports done (returncode), never ``None``."""
        return self.returncode


def _eligible_report(mode=None, target_dir=None):
    return {
        b: {"present": True, "eligible": True, "reason": None}
        for b in llm_backends.BACKEND_PRIORITY
    }


def _run_panel(
    backends,
    *,
    round1=None,
    round1_fail=None,
    round2=None,
    round2_fail=None,
    plan_text="my plan",
):
    """Run run_panel with mocked workers.

    ``round1`` maps backend -> round-1 answer text; ``round1_fail`` maps
    backend -> stderr (a round-1 failure). ``round2`` maps backend -> {role:
    text}; ``round2_fail`` maps backend -> {role: stderr}. Returns
    ``(summary, failures, transcript_text, spawned)`` where ``spawned`` is the
    list of ``(backend, prompt)`` in spawn order.
    """
    round1 = round1 or {}
    round1_fail = round1_fail or {}
    round2 = round2 or {}
    round2_fail = round2_fail or {}
    spawned = []

    def fake_spawn(backend, prompt, *, text_only, target_dir, prompt_dir):
        spawned.append((backend, prompt))
        return _FakeProc(0)

    def fake_collect(pending):
        for proc, (backend, role) in list(pending.items()):
            if role == "position":
                if backend in round1_fail:
                    proc.returncode = 1
                    yield proc, (backend, role), "", round1_fail[backend]
                else:
                    yield proc, (backend, role), round1[backend], ""
            else:
                fail = round2_fail.get(backend, {})
                if role in fail:
                    proc.returncode = 1
                    yield proc, (backend, role), "", fail[role]
                else:
                    yield proc, (backend, role), round2[backend][role], ""

    with tempfile.TemporaryDirectory() as d:
        transcript = Path(d) / "transcript.md"
        with (
            patch.object(
                llm_backends, "eligibility_report", side_effect=_eligible_report
            ),
            patch.object(second_opinion, "_spawn_worker", side_effect=fake_spawn),
            patch.object(second_opinion, "_collect_children", side_effect=fake_collect),
        ):
            summary, failures = second_opinion.run_panel(
                plan_text,
                list(backends),
                focus_hints=None,
                transcript_path=transcript,
                target_dir=Path.cwd(),
                text_only=True,
            )
        text = transcript.read_text(encoding="utf-8")
    return summary, failures, text, spawned


class PanelOrchestrationTests(unittest.TestCase):
    """Round structure, parallel dispatch, anonymisation, failure handling."""

    def test_two_rounds_with_summary_and_cross_exam(self) -> None:
        summary, failures, _, spawned = _run_panel(
            backends=("codex", "agy", "pi"),
            round1={"codex": "c1", "agy": "a1", "pi": "p1"},
            round2={
                "codex": {"synthesis": "**Agreed** … **Disputed** … **Open**"},
                "agy": {"cross-examination": "cross a"},
                "pi": {"cross-examination": "cross p"},
            },
        )
        self.assertIn("Agreed", summary)
        self.assertEqual(failures, [])
        round1_spawns = [p for b, p in spawned if "independent reviewers" in p]
        self.assertEqual(len(round1_spawns), 3)
        syn_prompt = next(p for b, p in spawned if "synthesizer of a panel" in p)
        self.assertEqual(syn_prompt.count("### Reviewer "), 3)
        cross_prompts = [
            p for b, p in spawned if "your own round-1 position" in p
        ]
        self.assertEqual(len(cross_prompts), 2)
        for p in cross_prompts:
            self.assertIn("Your round-1 position", p)
            self.assertEqual(p.count("### Reviewer "), 2)

    def test_round1_spawns_all_before_collecting(self) -> None:
        order = []

        def fake_spawn(backend, prompt, *, text_only, target_dir, prompt_dir):
            order.append(("spawn", backend))
            return _FakeProc(0)

        def fake_collect(pending):
            order.append(("collect", len(pending)))
            for proc, (backend, role) in list(pending.items()):
                if role == "position":
                    yield proc, (backend, role), "pos", ""
                else:
                    yield proc, (backend, role), "x", ""

        with tempfile.TemporaryDirectory() as d:
            transcript = Path(d) / "t.md"
            with (
                patch.object(
                    llm_backends,
                    "eligibility_report",
                    side_effect=_eligible_report,
                ),
                patch.object(second_opinion, "_spawn_worker", side_effect=fake_spawn),
                patch.object(
                    second_opinion, "_collect_children", side_effect=fake_collect
                ),
            ):
                second_opinion.run_panel(
                    "plan",
                    ["codex", "agy", "pi"],
                    focus_hints=None,
                    transcript_path=transcript,
                    target_dir=Path.cwd(),
                    text_only=True,
                )
        # All three round-1 workers are spawned before the first collect.
        first_collect = order.index(("collect", 3))
        self.assertEqual(order[:first_collect], [
            ("spawn", "codex"),
            ("spawn", "agy"),
            ("spawn", "pi"),
        ])

    def test_anonymisation_redacts_backend_names_in_round2_prompts(self) -> None:
        _, _, _, spawned = _run_panel(
            backends=("codex", "agy"),
            round1={"codex": "mentions codex and agy", "agy": "mentions pi"},
            round2={
                "codex": {"synthesis": "summary"},
                "agy": {"cross-examination": "cross"},
            },
        )
        round2_prompts = [
            p
            for b, p in spawned
            if "synthesizer of a panel" in p or "your own round-1 position" in p
        ]
        self.assertEqual(len(round2_prompts), 2)
        for p in round2_prompts:
            for name in ("codex", "agy", "pi", "opencode", "copilot"):
                self.assertNotRegex(p, rf"\b{name}\b")

    def test_failed_backend_continues_and_is_recorded(self) -> None:
        summary, failures, text, _ = _run_panel(
            backends=("codex", "agy", "pi"),
            round1={"codex": "c1", "pi": "p1"},
            round1_fail={"agy": "boom"},
            round2={
                "codex": {"synthesis": "summary"},
                "pi": {"cross-examination": "cross"},
            },
        )
        self.assertEqual(summary, "summary")
        self.assertEqual(len(failures), 1)
        self.assertIn("agy — round 1", failures[0])
        self.assertIn("boom", failures[0])
        self.assertIn("boom", text)

    def test_fewer_than_two_successful_round1_is_an_error(self) -> None:
        with self.assertRaises(second_opinion.PanelError):
            _run_panel(
                backends=("codex", "agy"),
                round1={"codex": "c1"},
                round1_fail={"agy": "down"},
                round2={"codex": {"synthesis": "summary"}},
            )

    def test_synthesis_failure_exits_with_error(self) -> None:
        with self.assertRaises(second_opinion.PanelError):
            _run_panel(
                backends=("codex", "agy"),
                round1={"codex": "c1", "agy": "a1"},
                round2={"agy": {"cross-examination": "cross"}},
                round2_fail={"codex": {"synthesis": "synthesis boom"}},
            )

    def test_transcript_records_replies_and_failures(self) -> None:
        _, _, text, _ = _run_panel(
            backends=("codex", "agy", "pi"),
            round1={"codex": "c1", "pi": "p1"},
            round1_fail={"agy": "boom"},
            round2={
                "codex": {"synthesis": "summary"},
                "pi": {"cross-examination": "cross"},
            },
        )
        self.assertIn("Panelists:", text)
        self.assertIn("A = codex", text)
        self.assertIn("## Round 1: independent positions", text)
        self.assertIn("## Round 2: cross-examination", text)
        self.assertIn("### Final summary", text)
        self.assertIn("boom", text)
        self.assertIn("## Failures", text)
        self.assertIn("(tool-written)", text)

    def test_spawn_failure_terminates_already_spawned_children(self) -> None:
        count = {"n": 0}
        running = Mock()
        running.pid = 1234
        running.poll.return_value = None  # still running

        def fake_spawn(backend, prompt, *, text_only, target_dir, prompt_dir):
            if count["n"] >= 1:
                raise OSError("spawn exhausted")
            count["n"] += 1
            return running

        killed = []
        with tempfile.TemporaryDirectory() as d:
            transcript = Path(d) / "t.md"
            with (
                patch.object(
                    llm_backends,
                    "eligibility_report",
                    side_effect=_eligible_report,
                ),
                patch.object(
                    second_opinion, "_spawn_worker", side_effect=fake_spawn
                ),
                patch.object(second_opinion.os, "getpgid", side_effect=lambda p: p),
                patch.object(
                    second_opinion.os,
                    "killpg",
                    side_effect=lambda p, s: killed.append(p),
                ),
            ):
                with self.assertRaises(OSError):
                    second_opinion.run_panel(
                        "plan",
                        ["codex", "agy", "pi"],
                        focus_hints=None,
                        transcript_path=transcript,
                        target_dir=Path.cwd(),
                        text_only=True,
                    )
        self.assertEqual(killed, [1234])


class CmdPanelValidationTests(unittest.TestCase):
    """Arg parsing / validation: dedup and the fewer-than-2-distinct guard."""

    def _cmd(self, argv):
        parser = second_opinion.build_parser()
        return parser.parse_args(["panel", *argv])

    def test_duplicate_backends_fail(self) -> None:
        args = self._cmd(["--backends", "codex,codex", "plan.md"])
        with self.assertRaises(SystemExit):
            second_opinion.cmd_panel(args)

    def test_unknown_backend_fails(self) -> None:
        # --backends is validated at parse time (ArgumentTypeError -> exit 2).
        with self.assertRaises(SystemExit):
            self._cmd(["--backends", "codex,nope", "plan.md"])

    def test_missing_plan_fails(self) -> None:
        args = self._cmd(["--backends", "codex,agy"])
        with self.assertRaises(SystemExit):
            second_opinion.cmd_panel(args)


if __name__ == "__main__":
    unittest.main()
