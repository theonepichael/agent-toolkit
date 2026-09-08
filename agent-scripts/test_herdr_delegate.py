#!/usr/bin/env python3
"""Tests for herdr_delegate.py. Run with: python3 test_herdr_delegate.py

The script's source of truth is this repo (dotfiles' copy was retired in the
cutover; the live ~/.claude/scripts symlink points here). Everything is
tested in process: herdr is faked at the module's herdr() boundary -- the
only function that shells out -- so nothing here reaches the real herdr
socket or spawns a subprocess, and the conftest sandbox needs no marking.

Covered: the CLI contract (help, usage errors), the launch-path failure
cleanup, and the `restart` subcommand's whole decision tree --
HERDR_ENV/prefix refusals, resolution-before-close ordering,
close-exactly-one-by-label, the deregistration poll, zero-match/multi-match
handling, runId discovery (explicit wins; persisted `prefix` field first,
legacy slug-scan fallback; refusal rather than a fresh launch), and
prompt-contract exact-match.
"""

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))
import herdr_delegate


class FakeHerdr:
    """Stands in for the herdr binary: records argv, answers canned JSON."""

    def __init__(
        self,
        tabs: list[dict[str, str]] | None = None,
        agents: list[dict[str, str]] | None = None,
        agent_list_script: list[list[dict[str, str]]] | None = None,
    ) -> None:
        self.tabs = list(tabs or [])
        self.agents = list(agents or [])
        # Consumed in order by `agent list` before falling back to self.agents.
        self.agent_list_script = list(agent_list_script or [])
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> dict[str, object]:
        self.calls.append(list(argv))
        head = (argv[0], argv[1])
        if head == ("tab", "list"):
            return {"result": {"tabs": list(self.tabs)}}
        if head == ("tab", "close"):
            tab_id = argv[2]
            self.tabs = [t for t in self.tabs if t["tab_id"] != tab_id]
            return {"result": {}}
        if head == ("agent", "list"):
            if self.agent_list_script:
                return {"result": {"agents": self.agent_list_script.pop(0)}}
            return {"result": {"agents": list(self.agents)}}
        if head == ("tab", "create"):
            return {
                "result": {
                    "root_pane": {"pane_id": "w9:pN"},
                    "tab": {"tab_id": "w9:tN"},
                }
            }
        if head in (("agent", "start"), ("agent", "prompt")):
            if head == ("agent", "start"):
                self.agents = [*self.agents, {"name": argv[2]}]
            return {"result": {}}
        return {"result": {}}

    def named(self, command: str, subcommand: str) -> list[list[str]]:
        return [c for c in self.calls if c[0] == command and c[1] == subcommand]


def write_state(
    state_dir: Path, name: str, body: str, mtime_offset_s: float = 0.0
) -> Path:
    path = state_dir / name
    path.write_text(body)
    if mtime_offset_s:
        import time

        stamp = time.time() + mtime_offset_s
        os.utime(path, (stamp, stamp))
    return path


def make_fake_herdr(
    *,
    start_error: str | None = None,
    start_code: str | None = None,
    start_error_script: list[tuple[str, str | None] | None] | None = None,
    agent_list_script: list[list[dict[str, str]]] | None = None,
    prompt_error: str | None = None,
    close_error: str | None = None,
) -> tuple[Callable[[list[str]], dict[str, object]], list[list[str]]]:
    """Replace herdr_delegate.herdr with a scripted fake.

    Returns (fake, argvs): ``fake`` answers ``tab create`` with a fresh
    ``w:t<n>``/``w:p<n>`` pair each call (so a caller retrying gets a
    distinguishable tab per attempt), ``agent list`` from
    ``agent_list_script`` (consumed in order, empty list once exhausted),
    and ``agent start`` / ``agent prompt`` / ``tab close`` from the given
    error strings (None = success). ``start_error_script``, if given, is
    consumed in order for successive ``agent start`` calls (``None`` entry
    = success that attempt, ``(message, code)`` = a raised
    ``RefusedError``); once exhausted, further calls succeed. Without a
    script, every ``agent start`` call uses the single ``start_error``/
    ``start_code`` pair. ``argvs`` records every argv it was given.
    """

    argvs: list[list[str]] = []
    tab_seq = {"n": 0}
    start_script = list(start_error_script) if start_error_script is not None else None
    agent_list_steps = list(agent_list_script or [])

    def fake(argv: list[str]) -> dict[str, object]:
        argvs.append(argv)
        if argv[0] == "tab" and argv[1] == "create":
            tab_seq["n"] += 1
            n = tab_seq["n"]
            return {
                "result": {
                    "root_pane": {"pane_id": f"w:p{n}"},
                    "tab": {"tab_id": f"w:t{n}"},
                }
            }
        if argv[0] == "agent" and argv[1] == "start":
            if start_script is not None and start_script:
                entry = start_script.pop(0)
                if entry is not None:
                    message, code = entry
                    raise herdr_delegate.RefusedError(message, code=code)
            elif start_error:
                raise herdr_delegate.RefusedError(start_error, code=start_code)
            return {"result": {"type": "ok"}}
        if argv[0] == "agent" and argv[1] == "prompt" and prompt_error:
            raise herdr_delegate.RefusedError(prompt_error)
        if argv[0] == "tab" and argv[1] == "close" and close_error:
            raise herdr_delegate.RefusedError(close_error)
        if argv[0] == "agent" and argv[1] == "list":
            if agent_list_steps:
                return {"result": {"agents": agent_list_steps.pop(0)}}
            return {"result": {"agents": []}}
        return {"result": {"type": "ok"}}

    return fake, argvs


def run_launch(fake: Callable[[list[str]], dict[str, object]]) -> int:
    """Run ``launch --slug atk-example`` with herdr faked; return exit code."""
    with (
        mock.patch.object(herdr_delegate, "herdr", fake),
        mock.patch.object(herdr_delegate.time, "sleep", lambda _s: None),
        mock.patch.dict(os.environ, {"HERDR_ENV": "1"}),
    ):
        argv = ["launch", "--slug", "atk-example", "--cwd", "/tmp"]
        sys.argv = ["herdr_delegate.py", *argv]
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                herdr_delegate.main()
        except SystemExit as e:
            return int(e.code or 0)
        return 0


class LaunchFailureCleanupTests(unittest.TestCase):
    """The tab create -> agent start window.

    A failed ``agent start`` (e.g. agent_name_taken) must not leave the
    freshly created tab behind: the caller created it solely for this agent,
    and herdr leaves pane cleanup to the caller on that error, so nobody else
    will close it -- the contentless unknown-status tab seen after a failed
    2026-09-07 swarm launch.
    """

    def test_agent_start_failure_closes_the_created_tab(self) -> None:
        fake, argvs = make_fake_herdr(start_error="agent_name_taken: nope")
        code = run_launch(fake)
        self.assertEqual(code, 1)
        self.assertIn(["tab", "close", "w:t1"], argvs)

    def test_failed_cleanup_still_reports_the_launch_error(self) -> None:
        fake, argvs = make_fake_herdr(
            start_error="agent_name_taken: nope", close_error="tab close failed"
        )
        code = run_launch(fake)
        self.assertEqual(code, 1)
        # The close was attempted, but the launch error is what surfaces.
        self.assertIn(["tab", "close", "w:t1"], argvs)

    def test_agent_prompt_failure_leaves_the_tab(self) -> None:
        # A failed prompt means the agent DID start; the tab holds a live
        # agent, and closing it would kill it.
        fake, argvs = make_fake_herdr(prompt_error="agent_prompt_stalled")
        code = run_launch(fake)
        self.assertEqual(code, 1)
        self.assertNotIn(["tab", "close", "w:t1"], argvs)

    def test_success_path_never_closes_the_tab(self) -> None:
        fake, argvs = make_fake_herdr()
        code = run_launch(fake)
        self.assertEqual(code, 0)
        self.assertNotIn(["tab", "close", "w:t1"], argvs)


class RefusedErrorCodeTests(unittest.TestCase):
    """The optional ``code`` attribute must never change message/str behavior."""

    def test_code_defaults_to_none(self) -> None:
        exc = herdr_delegate.RefusedError("boom")
        self.assertIsNone(exc.code)
        self.assertEqual(str(exc), "boom")

    def test_code_is_stored_without_altering_str(self) -> None:
        exc = herdr_delegate.RefusedError("boom", code="timeout")
        self.assertEqual(exc.code, "timeout")
        self.assertEqual(str(exc), "boom")
        self.assertEqual(exc.args, ("boom",))


class ParseHerdrErrorCodeTests(unittest.TestCase):
    """``_parse_herdr_error_code`` is a pure function: no side effects to fake."""

    def test_extracts_code_from_envelope(self) -> None:
        stderr = json.dumps({"error": {"code": "timeout", "message": "x"}, "id": "y"})
        self.assertEqual(herdr_delegate._parse_herdr_error_code(stderr), "timeout")

    def test_empty_string_returns_none(self) -> None:
        self.assertIsNone(herdr_delegate._parse_herdr_error_code(""))

    def test_non_json_returns_none(self) -> None:
        self.assertIsNone(herdr_delegate._parse_herdr_error_code("not json at all"))

    def test_json_without_error_key_returns_none(self) -> None:
        self.assertIsNone(
            herdr_delegate._parse_herdr_error_code(json.dumps({"ok": True}))
        )

    def test_error_not_a_dict_returns_none(self) -> None:
        self.assertIsNone(
            herdr_delegate._parse_herdr_error_code(json.dumps({"error": "boom"}))
        )

    def test_code_not_a_string_returns_none(self) -> None:
        self.assertIsNone(
            herdr_delegate._parse_herdr_error_code(json.dumps({"error": {"code": 5}}))
        )


class HerdrSubprocessTests(unittest.TestCase):
    """``herdr()`` itself, at the real subprocess boundary -- the only way to
    exercise its JSON-error-code parsing, since every other test in this file
    fakes ``herdr_delegate.herdr`` wholesale and never reaches this code."""

    def _completed(self, *, stderr: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["herdr", "agent", "start"], returncode=1, stdout="", stderr=stderr
        )

    def test_nonzero_exit_attaches_parsed_code(self) -> None:
        stderr = json.dumps(
            {
                "error": {"code": "timeout", "message": "timed out"},
                "id": "cli:agent:start",
            }
        )
        with (
            mock.patch.object(
                herdr_delegate.subprocess,
                "run",
                return_value=self._completed(stderr=stderr),
            ),
            self.assertRaises(herdr_delegate.RefusedError) as ctx,
        ):
            herdr_delegate.herdr(["agent", "start", "x"])
        self.assertEqual(ctx.exception.code, "timeout")

    def test_nonzero_exit_with_unrecognized_json_warns_on_stderr(self) -> None:
        stderr = json.dumps({"weird": "shape"})
        err = io.StringIO()
        with (
            mock.patch.object(
                herdr_delegate.subprocess,
                "run",
                return_value=self._completed(stderr=stderr),
            ),
            redirect_stderr(err),
            self.assertRaises(herdr_delegate.RefusedError) as ctx,
        ):
            herdr_delegate.herdr(["agent", "start", "x"])
        self.assertIsNone(ctx.exception.code)
        self.assertIn("no recognized 'code' field", err.getvalue())

    def test_nonzero_exit_with_plain_text_stderr_has_no_warning(self) -> None:
        err = io.StringIO()
        with (
            mock.patch.object(
                herdr_delegate.subprocess,
                "run",
                return_value=self._completed(stderr="boom, not json"),
            ),
            redirect_stderr(err),
            self.assertRaises(herdr_delegate.RefusedError) as ctx,
        ):
            herdr_delegate.herdr(["agent", "start", "x"])
        self.assertIsNone(ctx.exception.code)
        self.assertEqual(err.getvalue(), "")


class AgentStartRetryTests(unittest.TestCase):
    """The bounded fresh-tab retry for a ``timeout``-coded ``agent start``.

    Every retry must get its own tab/pane (round 1's finding: retrying
    against the same pane a timeout already gave up on is unsafe), and only
    the ``timeout`` code may ever retry -- everything else (including no
    parseable code at all) must behave exactly as before this feature
    existed.
    """

    def test_timeout_retried_then_succeeds(self) -> None:
        fake, argvs = make_fake_herdr(
            start_error_script=[
                ("timed out", "timeout"),
                ("timed out", "timeout"),
                None,
            ]
        )
        code = run_launch(fake)
        self.assertEqual(code, 0)
        self.assertEqual(len(_named(argvs, "agent", "start")), 3)
        self.assertEqual(len(_named(argvs, "tab", "create")), 3)
        self.assertEqual(len(_named(argvs, "tab", "close")), 2)
        self.assertEqual(
            _named(argvs, "agent", "prompt"),
            [["agent", "prompt", "atk-example", "/backlog-item --auto atk-example"]],
        )

    def test_timeout_exhausts_after_configured_retries(self) -> None:
        fake, argvs = make_fake_herdr(
            start_error_script=[("timed out", "timeout")] * 10
        )
        code = run_launch(fake)
        self.assertEqual(code, 1)
        attempts = herdr_delegate.AGENT_START_TIMEOUT_RETRIES + 1
        self.assertEqual(len(_named(argvs, "agent", "start")), attempts)
        self.assertEqual(len(_named(argvs, "tab", "create")), attempts)
        self.assertEqual(len(_named(argvs, "tab", "close")), attempts)

    def test_exhaustion_message_names_attempts_and_herdr_status(self) -> None:
        fake, _ = make_fake_herdr(start_error_script=[("timed out", "timeout")] * 10)
        _, err = self._run_main_capturing(fake, expect_exit=True)
        attempts = herdr_delegate.AGENT_START_TIMEOUT_RETRIES + 1
        self.assertIn(f"{attempts} attempt", err)
        self.assertIn("herdr status", err)

    def test_non_timeout_failure_never_retries(self) -> None:
        fake, argvs = make_fake_herdr(start_error="agent_name_taken: nope")
        code = run_launch(fake)
        self.assertEqual(code, 1)
        self.assertEqual(len(_named(argvs, "agent", "start")), 1)
        self.assertEqual(len(_named(argvs, "tab", "create")), 1)

    def test_uncoded_failure_never_retries(self) -> None:
        # code=None (e.g. non-JSON stderr) is exactly as non-retryable as
        # any other unrecognized failure -- a single attempt, no change.
        fake, argvs = make_fake_herdr(start_error="weird failure", start_code=None)
        code = run_launch(fake)
        self.assertEqual(code, 1)
        self.assertEqual(len(_named(argvs, "agent", "start")), 1)

    def test_retry_progress_notice_emitted_to_stderr(self) -> None:
        fake, _ = make_fake_herdr(start_error_script=[("timed out", "timeout"), None])
        _, err = self._run_main_capturing(fake, expect_exit=False)
        self.assertIn("retrying after backoff", err)

    def test_deregistration_wait_between_retries(self) -> None:
        fake, argvs = make_fake_herdr(
            start_error_script=[("timed out", "timeout"), None],
            agent_list_script=[[{"name": "atk-example"}], []],
        )
        code = run_launch(fake)
        self.assertEqual(code, 0)
        self.assertEqual(len(_named(argvs, "agent", "list")), 2)

    def test_deregistration_exhaustion_uses_retry_loop_advice_not_restart(self) -> None:
        fake, _ = make_fake_herdr(
            start_error_script=[("timed out", "timeout"), None],
            agent_list_script=[[{"name": "atk-example"}]] * 100,
        )
        _, err = self._run_main_capturing(fake, expect_exit=True)
        self.assertNotIn("`restart`", err)
        self.assertIn("retry loop", err)

    def test_tab_create_failure_propagates_unretried(self) -> None:
        def fake(argv: list[str]) -> dict[str, object]:
            if argv[0] == "tab" and argv[1] == "create":
                raise herdr_delegate.RefusedError("boom: cannot create tab")
            return {"result": {"type": "ok"}}

        code = run_launch(fake)
        self.assertEqual(code, 1)

    def test_prompt_failure_after_success_is_not_retried_and_leaves_tab(self) -> None:
        fake, argvs = make_fake_herdr(prompt_error="agent_prompt_stalled")
        code = run_launch(fake)
        self.assertEqual(code, 1)
        self.assertEqual(len(_named(argvs, "agent", "start")), 1)
        self.assertNotIn(["tab", "close", "w:t1"], argvs)

    def _run_main_capturing(
        self, fake: Callable[[list[str]], dict[str, object]], *, expect_exit: bool
    ) -> tuple[str, str]:
        with (
            mock.patch.object(herdr_delegate, "herdr", fake),
            mock.patch.object(herdr_delegate.time, "sleep", lambda _s: None),
            mock.patch.dict(os.environ, {"HERDR_ENV": "1"}),
        ):
            argv = ["launch", "--slug", "atk-example", "--cwd", "/tmp"]
            sys.argv = ["herdr_delegate.py", *argv]
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                if expect_exit:
                    with self.assertRaises(SystemExit):
                        herdr_delegate.main()
                else:
                    herdr_delegate.main()
            return out.getvalue(), err.getvalue()


def _named(argvs: list[list[str]], command: str, subcommand: str) -> list[list[str]]:
    return [a for a in argvs if a[0] == command and a[1] == subcommand]


class DelegateTests(unittest.TestCase):
    def run_main(self, argv: list[str]) -> tuple[int, str, str]:
        """Run main() with the given argv, returning (exit_code, stdout, stderr)."""
        sys.argv = ["herdr_delegate.py", *argv]
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                herdr_delegate.main()
        except SystemExit as e:
            return int(e.code or 0), out.getvalue(), err.getvalue()
        return 0, out.getvalue(), err.getvalue()


class SmokeTests(DelegateTests):
    def test_help_exits_zero_and_names_all_subcommands(self) -> None:
        code, out, _ = self.run_main(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("plan", out)
        self.assertIn("launch", out)
        self.assertIn("restart", out)

    def test_bad_usage_exits_two(self) -> None:
        code, _, _ = self.run_main(["--no-such-flag"])
        self.assertEqual(code, 2)

    def test_launch_requires_a_selector(self) -> None:
        code, _, err = self.run_main(["launch"])
        self.assertEqual(code, 2)
        self.assertIn("--slug", err)

    def test_launch_requires_prefix_with_swarm(self) -> None:
        code, _, err = self.run_main(["launch", "--swarm", "3"])
        self.assertEqual(code, 2)
        self.assertIn("--prefix", err)

    def test_restart_requires_prefix_with_swarm(self) -> None:
        code, _, err = self.run_main(["restart", "--swarm", "3"])
        self.assertEqual(code, 2)
        self.assertIn("--prefix", err)


class PromptContractTests(unittest.TestCase):
    def test_worker_prompt(self) -> None:
        self.assertEqual(
            herdr_delegate.worker_prompt("atk-example"),
            "/backlog-item --auto atk-example",
        )

    def test_orchestrator_prompt(self) -> None:
        self.assertEqual(
            herdr_delegate.orchestrator_prompt(3, "atk"),
            "/backlog-item --swarm=3 --prefix atk",
        )

    def test_orchestrator_resume_prompt(self) -> None:
        self.assertEqual(
            herdr_delegate.orchestrator_resume_prompt(3, "r1", "atk"),
            "/backlog-item --swarm=3 resume r1 --prefix atk",
        )


@mock.patch.dict(os.environ, {"HERDR_ENV": "1"})
class LaunchTests(DelegateTests):
    def setUp(self) -> None:
        self.fake = FakeHerdr()
        patcher = mock.patch.object(herdr_delegate, "herdr", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        sleep_patcher = mock.patch.object(herdr_delegate.time, "sleep", lambda _s: None)
        sleep_patcher.start()
        self.addCleanup(sleep_patcher.stop)

    def test_swarm_launch_prompts_with_prefix(self) -> None:
        code, out, _ = self.run_main(
            ["launch", "--swarm", "3", "--prefix", "atk", "--cwd", "/tmp"]
        )
        self.assertEqual(code, 0)
        prompts = self.fake.named("agent", "prompt")
        self.assertEqual(len(prompts), 1)
        self.assertEqual(prompts[0][2], "swarm-atk")
        self.assertEqual(prompts[0][3], "/backlog-item --swarm=3 --prefix atk")


@mock.patch.dict(os.environ, {"HERDR_ENV": "1"})
class RestartTests(DelegateTests):
    """The restart decision tree, against the in-process fake."""

    def setUp(self) -> None:
        self.fake = FakeHerdr()
        patcher = mock.patch.object(herdr_delegate, "herdr", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        sleep_patcher = mock.patch.object(herdr_delegate.time, "sleep", lambda _s: None)
        sleep_patcher.start()
        self.addCleanup(sleep_patcher.stop)
        self.state_dir = Path(tempfile.mkdtemp(prefix="swarm-state-"))
        env_patcher = mock.patch.dict(
            os.environ, {"PI_SWARM_STATE_DIR": str(self.state_dir)}
        )
        env_patcher.start()
        self.addCleanup(env_patcher.stop)
        self.addCleanup(
            lambda: __import__("shutil").rmtree(self.state_dir, ignore_errors=True)
        )

    def restart(self, *extra: str) -> tuple[int, str, str]:
        return self.run_main(["restart", "--swarm", "3", "--prefix", "atk", *extra])

    # -- gating ---------------------------------------------------------------

    def test_refuses_outside_herdr(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HERDR_ENV", None)
            code, _, err = self.restart()
        self.assertEqual(code, 1)
        self.assertIn("HERDR_ENV", err)

    def test_refuses_unsafe_prefix(self) -> None:
        code, _, err = self.run_main(["restart", "--swarm", "3", "--prefix", "meta"])
        self.assertEqual(code, 1)
        self.assertIn("not a worker-safe", err)

    def test_refuses_unknown_prefix(self) -> None:
        code, _, err = self.run_main(["restart", "--swarm", "3", "--prefix", "nope"])
        self.assertEqual(code, 1)
        self.assertIn("not a worker-safe", err)

    # -- run-id validation (before anything closes) ---------------------------

    def test_run_id_with_path_separator_exits_two(self) -> None:
        code, _, _ = self.restart("--run-id", "a/b")
        self.assertEqual(code, 2)

    def test_run_id_empty_exits_two(self) -> None:
        code, _, _ = self.restart("--run-id", "")
        self.assertEqual(code, 2)

    def test_run_id_overlong_exits_two(self) -> None:
        code, _, _ = self.restart("--run-id", "r" * 65)
        self.assertEqual(code, 2)

    # -- discovery ------------------------------------------------------------

    def test_explicit_run_id_wins_without_reading_state(self) -> None:
        # A corrupt state dir must not matter when the runId is explicit.
        (self.state_dir / "swarm-broken.json").write_text("{not json")
        code, out, _ = self.restart("--run-id", "r1")
        self.assertEqual(code, 0)
        summary = json.loads(out)
        self.assertEqual(summary["resumed"], "r1")
        prompt = self.fake.named("agent", "prompt")[0]
        self.assertEqual(prompt[3], "/backlog-item --swarm=3 resume r1 --prefix atk")

    def test_discovery_prefers_persisted_prefix_field(self) -> None:
        # A legacy file (slug-scan match) older than the exact-field one: the
        # newest exact-field match wins.
        write_state(
            self.state_dir,
            "swarm-legacy.json",
            json.dumps({"runId": "legacy", "workers": [], "attempted": ["atk-old"]}),
            mtime_offset_s=-100,
        )
        write_state(
            self.state_dir,
            "swarm-current.json",
            json.dumps({"runId": "current", "prefix": "atk", "workers": []}),
        )
        code, out, _ = self.restart()
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["resumed"], "current")

    def test_discovery_falls_back_to_legacy_slug_scan(self) -> None:
        write_state(
            self.state_dir,
            "swarm-legacy.json",
            json.dumps(
                {
                    "runId": "legacy",
                    "workers": [{"agent": "w1", "slug": "atk-foo"}],
                }
            ),
        )
        code, out, _ = self.restart()
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["resumed"], "legacy")

    def test_prefix_field_mismatch_never_matches(self) -> None:
        # Exact field beats slug-scan even when it does NOT match: a run
        # scoped to another prefix must not be adopted by slug luck.
        write_state(
            self.state_dir,
            "swarm-other.json",
            json.dumps({"runId": "other", "prefix": "iron-lb", "workers": []}),
            mtime_offset_s=-5,
        )
        write_state(
            self.state_dir,
            "swarm-newer.json",
            json.dumps({"runId": "newer", "prefix": "iron-lb", "workers": []}),
        )
        code, _, err = self.restart()
        self.assertEqual(code, 1)
        self.assertIn("--run-id", err)
        self.assertIn("launch", err)

    def test_discovery_failure_refuses_without_closing_anything(self) -> None:
        # An empty state dir and a live orchestrator tab: the refusal must
        # leave the tab untouched (resolution-before-close ordering).
        self.fake.tabs = [{"tab_id": "w1:tO", "label": "swarm-atk"}]
        code, _, err = self.restart()
        self.assertEqual(code, 1)
        self.assertIn("--run-id", err)
        self.assertEqual(self.fake.named("tab", "close"), [])

    def test_no_state_dir_refuses(self) -> None:
        import shutil

        shutil.rmtree(self.state_dir)
        code, _, err = self.restart()
        self.assertEqual(code, 1)
        self.assertIn("state", err.lower())

    def test_corrupt_state_files_are_skipped_and_named(self) -> None:
        (self.state_dir / "swarm-broken.json").write_text("{not json")
        write_state(
            self.state_dir,
            "swarm-unrelated.json",
            json.dumps({"runId": "u", "prefix": "iron-lb", "workers": []}),
        )
        code, _, err = self.restart()
        self.assertEqual(code, 1)
        self.assertIn("swarm-broken.json", err)

    # -- tab selection ---------------------------------------------------------

    def test_closes_exactly_one_matching_tab_and_relaunches(self) -> None:
        self.fake.tabs = [
            {"tab_id": "w1:tO", "label": "swarm-atk"},
            {"tab_id": "w2:tX", "label": "swarm-iron-lb"},
            {"tab_id": "w3:tW", "label": "some worker tab"},
        ]
        write_state(
            self.state_dir,
            "swarm-r1.json",
            json.dumps({"runId": "r1", "prefix": "atk", "workers": []}),
        )
        code, out, _ = self.restart()
        self.assertEqual(code, 0)
        closes = self.fake.named("tab", "close")
        self.assertEqual(closes, [["tab", "close", "w1:tO"]])
        summary = json.loads(out)
        self.assertEqual(summary["closed_tab"], "w1:tO")
        self.assertEqual(summary["resumed"], "r1")
        # Relaunch uses launch's exact builders: env on the tab, model after
        # the separator only when given (not given here).
        create = self.fake.named("tab", "create")[0]
        self.assertIn("--label", create)
        self.assertEqual(create[create.index("--label") + 1], "swarm-atk")
        self.assertIn("PI_AGENT_UNATTENDED=1", create)
        start = self.fake.named("agent", "start")[0]
        self.assertEqual(start[2], "swarm-atk")
        prompt = self.fake.named("agent", "prompt")[0]
        self.assertEqual(prompt[2], "swarm-atk")
        self.assertEqual(prompt[3], "/backlog-item --swarm=3 resume r1 --prefix atk")

    def test_zero_matching_tabs_still_relaunches(self) -> None:
        write_state(
            self.state_dir,
            "swarm-r1.json",
            json.dumps({"runId": "r1", "prefix": "atk", "workers": []}),
        )
        code, out, _ = self.restart()
        self.assertEqual(code, 0)
        self.assertEqual(self.fake.named("tab", "close"), [])
        self.assertEqual(json.loads(out)["closed_tab"], None)

    def test_multiple_matching_tabs_refuse_without_closing(self) -> None:
        self.fake.tabs = [
            {"tab_id": "w1:tA", "label": "swarm-atk"},
            {"tab_id": "w2:tB", "label": "swarm-atk"},
        ]
        write_state(
            self.state_dir,
            "swarm-r1.json",
            json.dumps({"runId": "r1", "prefix": "atk", "workers": []}),
        )
        code, _, err = self.restart()
        self.assertEqual(code, 1)
        self.assertIn("swarm-atk", err)
        self.assertEqual(self.fake.named("tab", "close"), [])
        self.assertEqual(self.fake.named("tab", "create"), [])

    # -- deregistration poll ---------------------------------------------------

    def test_deregistration_poll_waits_for_name_to_clear(self) -> None:
        self.fake.tabs = [{"tab_id": "w1:tO", "label": "swarm-atk"}]
        # The name is still registered for two polls, then clears.
        self.fake.agent_list_script = [
            [{"name": "swarm-atk"}],
            [{"name": "swarm-atk"}],
            [],
        ]
        write_state(
            self.state_dir,
            "swarm-r1.json",
            json.dumps({"runId": "r1", "prefix": "atk", "workers": []}),
        )
        code, out, _ = self.restart()
        self.assertEqual(code, 0)
        # Three agent list calls happened between close and relaunch.
        lists = self.fake.named("agent", "list")
        close_idx = self.fake.calls.index(["tab", "close", "w1:tO"])
        self.assertTrue(all(self.fake.calls.index(c) > close_idx for c in lists))
        self.assertEqual(json.loads(out)["closed_tab"], "w1:tO")

    def test_deregistration_poll_exhaustion_refuses(self) -> None:
        self.fake.tabs = [{"tab_id": "w1:tO", "label": "swarm-atk"}]
        write_state(
            self.state_dir,
            "swarm-r1.json",
            json.dumps({"runId": "r1", "prefix": "atk", "workers": []}),
        )
        # Every poll still shows the agent live.
        self.fake.agent_list_script = [[{"name": "swarm-atk"}]] * 100
        code, _, err = self.restart()
        self.assertEqual(code, 1)
        self.assertIn("swarm-atk", err)
        self.assertIn("restart", err)
        # Nothing was relaunched into the contested name.
        self.assertEqual(self.fake.named("agent", "start"), [])

    def test_model_passthrough_reaches_the_separator(self) -> None:
        write_state(
            self.state_dir,
            "swarm-r1.json",
            json.dumps({"runId": "r1", "prefix": "atk", "workers": []}),
        )
        code, _, _ = self.restart("--model", "opencode-go/glm-5.2")
        self.assertEqual(code, 0)
        start = self.fake.named("agent", "start")[0]
        sep = start.index("--")
        self.assertEqual(start[sep:], ["--", "--model", "opencode-go/glm-5.2"])


if __name__ == "__main__":
    unittest.main()
