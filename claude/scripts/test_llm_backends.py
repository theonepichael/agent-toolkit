#!/usr/bin/env python3
"""Tests for llm_backends.py. Run with: python3 test_llm_backends.py

Uses real subprocesses (via `sys.executable -c ...`) for `_run_command`/
`_kill_active_process` rather than mocking `subprocess.Popen` — those two
functions' entire job is process lifecycle management (timeouts, process
groups, kill signals), which a mock would just assert away instead of
exercising. Higher-level logic (argv-building, opencode's event parsing)
mocks `_run_command`/`run_backend_command` instead, since real
`agy`/`opencode`/`pi`/`copilot` binaries aren't available in CI.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import AbstractContextManager
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import llm_backends


def py(code: str) -> list[str]:
    """Build a command that runs `code` as a Python one-liner."""
    return [sys.executable, "-c", code]


class SafeGetTests(unittest.TestCase):
    def test_01_nested_success(self) -> None:
        obj = {"a": {"b": {"c": "value"}}}
        self.assertEqual(llm_backends._safe_get(obj, "a", "b", "c"), "value")

    def test_02_missing_key_returns_none(self) -> None:
        self.assertIsNone(llm_backends._safe_get({"a": {}}, "a", "b", "c"))

    def test_03_non_dict_intermediate_returns_none_not_crash(self) -> None:
        obj = {"a": {"b": "just a string"}}
        self.assertIsNone(llm_backends._safe_get(obj, "a", "b", "c"))

    def test_04_non_dict_root_returns_none(self) -> None:
        self.assertIsNone(llm_backends._safe_get("not a dict", "a"))

    def test_05_empty_keys_returns_obj_itself(self) -> None:
        obj = {"a": 1}
        self.assertEqual(llm_backends._safe_get(obj), obj)


class OpencodeJsonEventsTests(unittest.TestCase):
    def test_06_valid_lines_parsed(self) -> None:
        raw = '{"type": "text", "part": {"text": "hi"}}\n{"type": "done"}\n'
        events = llm_backends._opencode_json_events(raw)
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["type"], "text")

    def test_07_invalid_json_lines_skipped(self) -> None:
        raw = 'not json\n{"type": "text", "part": {"text": "hi"}}\ngarbage{{{\n'
        events = llm_backends._opencode_json_events(raw)
        self.assertEqual(len(events), 1)

    def test_08_blank_lines_skipped(self) -> None:
        raw = '\n  \n{"type": "done"}\n\n'
        events = llm_backends._opencode_json_events(raw)
        self.assertEqual(len(events), 1)

    def test_09_non_dict_json_values_skipped_not_crash(self) -> None:
        raw = '[1, 2, 3]\n"just a string"\n42\n{"type": "done"}\n'
        events = llm_backends._opencode_json_events(raw)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "done")


class OpencodeTextChunksTests(unittest.TestCase):
    def test_10_normal_chunks_extracted(self) -> None:
        events: list[dict[str, object]] = [
            {"type": "text", "part": {"text": "Hello "}},
            {"type": "text", "part": {"text": "world"}},
            {"type": "done"},
        ]
        self.assertEqual(
            llm_backends._opencode_text_chunks(events), ["Hello ", "world"]
        )

    def test_11_missing_part_skipped_not_crash(self) -> None:
        events: list[dict[str, object]] = [{"type": "text"}]
        self.assertEqual(llm_backends._opencode_text_chunks(events), [])

    def test_12_part_not_a_dict_skipped_not_crash(self) -> None:
        events: list[dict[str, object]] = [{"type": "text", "part": "not a dict"}]
        self.assertEqual(llm_backends._opencode_text_chunks(events), [])

    def test_13_non_text_type_ignored(self) -> None:
        events: list[dict[str, object]] = [
            {"type": "other", "part": {"text": "should not appear"}}
        ]
        self.assertEqual(llm_backends._opencode_text_chunks(events), [])

    def test_14_empty_text_not_included(self) -> None:
        events: list[dict[str, object]] = [{"type": "text", "part": {"text": ""}}]
        self.assertEqual(llm_backends._opencode_text_chunks(events), [])


class AvailableBackendsTests(unittest.TestCase):
    def test_15_priority_order_preserved(self) -> None:
        with patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"):
            self.assertEqual(
                llm_backends.available_backends(),
                ["agy", "pi", "opencode", "copilot"],
            )

    def test_16_only_installed_backends_returned(self) -> None:
        with patch(
            "shutil.which",
            side_effect=lambda b: "/usr/bin/opencode" if b == "opencode" else None,
        ):
            self.assertEqual(llm_backends.available_backends(), ["opencode"])

    def test_17_none_installed_returns_empty(self) -> None:
        with patch("shutil.which", return_value=None):
            self.assertEqual(llm_backends.available_backends(), [])

    def test_18_resolve_backend_picks_first_priority(self) -> None:
        """resolve_backend now filters on contract eligibility, not merely on
        presence -- an installed backend that cannot be isolated must never be
        selected."""
        with (
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.object(llm_backends, "containment_available", lambda: True),
        ):
            self.assertEqual(llm_backends.resolve_backend(), "agy")

    def test_18b_resolve_backend_skips_a_backend_it_cannot_isolate(self) -> None:
        """On a host with no working namespaces, agy and opencode are
        ineligible (both need containment) and resolution falls to pi, which
        isolates by flags alone."""
        with (
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.object(llm_backends, "containment_available", lambda: False),
        ):
            self.assertEqual(llm_backends.resolve_backend(), "pi")

    def test_19_resolve_backend_none_when_unavailable(self) -> None:
        with (
            patch("shutil.which", return_value=None),
            patch.object(llm_backends, "containment_available", lambda: False),
        ):
            self.assertIsNone(llm_backends.resolve_backend())


class RunCommandRealSubprocessTests(unittest.TestCase):
    """Exercises `_run_command`/`_kill_active_process` with real child processes."""

    @pytest.mark.allow_real_subprocess
    def test_20_successful_command(self) -> None:
        returncode, stdout, _stderr = llm_backends._run_command(
            py("print('hi')"), timeout=30
        )
        self.assertEqual(returncode, 0)
        self.assertEqual(stdout.strip(), "hi")

    @pytest.mark.allow_real_subprocess
    def test_21_nonzero_exit_captured(self) -> None:
        returncode, _stdout, _stderr = llm_backends._run_command(
            py("import sys; sys.exit(3)"), timeout=30
        )
        self.assertEqual(returncode, 3)

    @pytest.mark.allow_real_subprocess
    def test_22_stderr_captured(self) -> None:
        _returncode, _stdout, stderr = llm_backends._run_command(
            py("import sys; sys.stderr.write('oops')"), timeout=30
        )
        self.assertIn("oops", stderr)

    @pytest.mark.allow_real_subprocess
    def test_23_missing_executable_wrapped_as_backend_error(self) -> None:
        # Regression: this used to propagate a raw OSError/FileNotFoundError
        # instead of BackendError, crashing the caller's fallback loop.
        with self.assertRaises(llm_backends.BackendError) as cm:
            llm_backends._run_command(["/no/such/executable-xyz"], timeout=30)
        self.assertIn("failed to start", str(cm.exception))

    @pytest.mark.allow_real_subprocess
    def test_24_missing_executable_does_not_leave_active_process_set(self) -> None:
        with self.assertRaises(llm_backends.BackendError):
            llm_backends._run_command(["/no/such/executable-xyz"], timeout=30)
        self.assertIsNone(llm_backends._active_process)

    @pytest.mark.allow_real_subprocess
    def test_25_active_process_cleared_after_success(self) -> None:
        llm_backends._run_command(py("print('hi')"), timeout=30)
        self.assertIsNone(llm_backends._active_process)

    @pytest.mark.allow_real_subprocess
    def test_26_timeout_kills_process_and_raises(self) -> None:
        start = time.monotonic()
        with self.assertRaises(llm_backends.BackendError) as cm:
            llm_backends._run_command(py("import time; time.sleep(30)"), timeout=0.3)
        elapsed = time.monotonic() - start
        self.assertIn("timed out after 0.3s", str(cm.exception))
        # killed promptly, not left running the full 30s sleep
        self.assertLess(elapsed, 5)
        self.assertIsNone(llm_backends._active_process)

    @pytest.mark.allow_real_subprocess
    def test_62_child_does_not_inherit_an_open_never_eof_stdin(self) -> None:
        # Regression for anomalyco/opencode#38723: a child that reads stdin
        # at all blocks forever if it inherits a descriptor that never
        # reaches EOF (e.g. a pipe whose write end is still open) -- which is
        # exactly what our own stdin can look like when this process is
        # itself spawned by a long-lived parent. Simulates that by replacing
        # our own fd 0 with the read end of a pipe whose write end is
        # deliberately kept open for the duration of the call. Pre-fix (no
        # stdin= on Popen), the child inherits this and `sys.stdin.read()`
        # never returns, so `_run_command` would raise on the 5s timeout
        # instead of returning quickly.
        read_fd, write_fd = os.pipe()
        saved_stdin_fd = os.dup(0)
        try:
            os.dup2(read_fd, 0)
            os.close(read_fd)
            start = time.monotonic()
            returncode, stdout, _stderr = llm_backends._run_command(
                py("import sys; data = sys.stdin.read(); print(len(data))"),
                timeout=5,
            )
            elapsed = time.monotonic() - start
        finally:
            os.dup2(saved_stdin_fd, 0)
            os.close(saved_stdin_fd)
            os.close(write_fd)
        self.assertEqual(returncode, 0)
        self.assertEqual(stdout.strip(), "0")
        self.assertLess(elapsed, 3)

    @pytest.mark.allow_real_subprocess
    def test_59_retries_zero_by_default_message_unchanged(self) -> None:
        # Default (no retries kwarg) must behave exactly like pre-retry
        # code: a single attempt, no "(all N attempts...)" suffix.
        with self.assertRaises(llm_backends.BackendError) as cm:
            llm_backends._run_command(py("import time; time.sleep(30)"), timeout=0.3)
        self.assertEqual(str(cm.exception), "timed out after 0.3s — killed")

    @pytest.mark.allow_real_subprocess
    def test_60_retry_recovers_after_one_timeout(self) -> None:
        # First attempt: no marker file yet -> creates it, then sleeps past
        # the timeout (gets killed). Second attempt (the retry): marker
        # exists -> prints immediately and exits 0. Proves a genuine retry
        # (a fresh subprocess) happens, not just a longer single wait.
        marker = Path(tempfile.mkdtemp()) / "attempt-marker"
        script = (
            "import pathlib, time\n"
            f"marker = pathlib.Path({str(marker)!r})\n"
            "if marker.exists():\n"
            "    print('recovered')\n"
            "else:\n"
            "    marker.touch()\n"
            "    time.sleep(30)\n"
        )
        returncode, stdout, _stderr = llm_backends._run_command(
            py(script), timeout=0.3, retries=1
        )
        self.assertEqual(returncode, 0)
        self.assertEqual(stdout.strip(), "recovered")

    @pytest.mark.allow_real_subprocess
    def test_61_retry_exhausted_raises_with_attempt_count(self) -> None:
        start = time.monotonic()
        with self.assertRaises(llm_backends.BackendError) as cm:
            llm_backends._run_command(
                py("import time; time.sleep(30)"), timeout=0.3, retries=1
            )
        elapsed = time.monotonic() - start
        self.assertIn("timed out after 0.3s — killed", str(cm.exception))
        self.assertIn("all 2 attempts timed out", str(cm.exception))
        # bounded: roughly 2x timeout (both attempts killed promptly), not
        # anywhere near the 30s sleep either attempt would otherwise run
        self.assertLess(elapsed, 5)
        self.assertIsNone(llm_backends._active_process)

    @pytest.mark.allow_real_subprocess
    def test_27_kill_active_process_terminates_real_child(self) -> None:
        proc = subprocess.Popen(
            py("import time; time.sleep(30)"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        llm_backends._active_process = proc
        try:
            llm_backends._kill_active_process()
            proc.wait(timeout=5)
            self.assertIsNotNone(proc.poll())
        finally:
            llm_backends._active_process = None
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            if proc.stdout:
                proc.stdout.close()
            if proc.stderr:
                proc.stderr.close()

    def test_28_kill_active_process_noop_when_none(self) -> None:
        llm_backends._active_process = None
        llm_backends._kill_active_process()  # must not raise

    @pytest.mark.allow_real_subprocess
    def test_29_kill_active_process_noop_when_already_exited(self) -> None:
        proc = subprocess.Popen(py("pass"), text=True)
        proc.wait()
        llm_backends._active_process = proc
        try:
            llm_backends._kill_active_process()  # must not raise
        finally:
            llm_backends._active_process = None


@pytest.mark.allow_real_subprocess
class RunBackendCommandTests(unittest.TestCase):
    def test_30_success_returns_stripped_stdout(self) -> None:
        result = llm_backends.run_backend_command(
            py("print('  output text  ')"), timeout=30
        )
        self.assertEqual(result, "output text")

    def test_31_nonzero_exit_raises_with_stderr(self) -> None:
        with self.assertRaises(llm_backends.BackendError) as cm:
            llm_backends.run_backend_command(
                py("import sys; sys.stderr.write('boom'); sys.exit(1)"), timeout=30
            )
        self.assertIn("exited 1", str(cm.exception))
        self.assertIn("boom", str(cm.exception))

    def test_32_exit_zero_empty_stdout_is_still_a_failure(self) -> None:
        with self.assertRaises(llm_backends.BackendError) as cm:
            llm_backends.run_backend_command(py("pass"), timeout=30)
        self.assertIn("produced no output", str(cm.exception))


# Command prefixes the isolation contract produces. Kept as constants so a
# deliberate contract change updates one place, and so these argv assertions
# read as "base plus this call's arguments" rather than as opaque lists.
PI_ISOLATED = [
    "pi",
    "-p",
    "--no-session",
    "--provider",
    "opencode-go",
    "--no-tools",
    "--no-context-files",
    "--no-prompt-templates",
]
OPENCODE_ISOLATED = [
    "unshare",
    "-Urm",
    "--map-root-user",
    "opencode",
    "run",
    "--auto",
    "--format",
    "json",
    "--agent",
    "adversary",
]


class _ContainmentStubbed(unittest.TestCase):
    """Base for suites whose backends consult containment_available().

    That probe shells out to `unshare`, which the repo-root conftest blocks
    and which would make these command-shape assertions depend on the host
    kernel. Stubbed True so the assertions stay about argv.
    """

    def setUp(self) -> None:
        super().setUp()
        patcher = patch.object(llm_backends, "containment_available", lambda: True)
        patcher.start()
        self.addCleanup(patcher.stop)
        daemon = patch.object(llm_backends, "daemon_listening", lambda b: False)
        daemon.start()
        self.addCleanup(daemon.stop)


class RunAgyTests(_ContainmentStubbed):
    def test_33_builds_expected_argv(self) -> None:
        """agy runs under the containment wrapper, with its prompt bound to
        --print and its model flag after it.

        Asserted as properties rather than a literal argv: the wrapper carries
        an inline shell script whose text changes whenever a mount is added,
        and a literal comparison would fail on changes that do not affect the
        contract.
        """
        with patch.object(
            llm_backends, "run_backend_command", return_value="text"
        ) as mock_run:
            result = llm_backends.run_agy(
                "my prompt", model="Gemini 3.6 Flash (High)", timeout=60
            )
        self.assertEqual(result, "text")
        argv, timeout = mock_run.call_args[0]
        self.assertEqual(timeout, 60)
        self.assertEqual(argv[:3], ["unshare", "-Urm", "--map-root-user"])
        self.assertIn("agy", argv)
        # --print takes the next argument as its prompt value.
        self.assertEqual(argv[argv.index("--print") + 1], "my prompt")
        self.assertEqual(argv[argv.index("--model") + 1], "Gemini 3.6 Flash (High)")

    def test_33b_agy_containment_hides_home_and_tmp(self) -> None:
        """The wrapper must actually blank things. A namespace that mounts
        nothing contains nothing, and the descriptor would then be claiming an
        isolation mechanism that does not work."""
        with patch.object(
            llm_backends, "run_backend_command", return_value="t"
        ) as mock_run:
            llm_backends.run_agy("p", model="m", timeout=60)
        script = " ".join(mock_run.call_args[0][0])
        self.assertIn('mount -t tmpfs tmpfs "$H"', script)
        self.assertIn("mount -t tmpfs tmpfs /tmp", script)
        self.assertIn("SB_SHADOW_OMIT=GEMINI.md", script)


class RunCopilotTests(unittest.TestCase):
    # copilot's -p/--prompt takes the next argument as its prompt VALUE, so
    # the prompt must sit right after -p (last in _base) with the isolation
    # flags following it -- see llm_backends.BACKEND_ISOLATION["copilot"].

    def test_34_no_model_omits_flag(self) -> None:
        with patch.object(
            llm_backends, "run_backend_command", return_value="text"
        ) as mock_run:
            result = llm_backends.run_copilot("my prompt", model=None, timeout=60)
        self.assertEqual(result, "text")
        mock_run.assert_called_once_with(
            [
                "copilot",
                "--silent",
                "-p",
                "my prompt",
                "--deny-tool=write",
                "--deny-tool=shell",
                "--no-custom-instructions",
            ],
            60,
        )

    def test_35_empty_string_model_omits_flag(self) -> None:
        with patch.object(
            llm_backends, "run_backend_command", return_value="text"
        ) as mock_run:
            llm_backends.run_copilot("my prompt", model="", timeout=60)
        mock_run.assert_called_once_with(
            [
                "copilot",
                "--silent",
                "-p",
                "my prompt",
                "--deny-tool=write",
                "--deny-tool=shell",
                "--no-custom-instructions",
            ],
            60,
        )

    def test_36_model_appends_flag(self) -> None:
        with patch.object(
            llm_backends, "run_backend_command", return_value="text"
        ) as mock_run:
            llm_backends.run_copilot("my prompt", model="claude-sonnet-4.6", timeout=60)
        mock_run.assert_called_once_with(
            [
                "copilot",
                "--silent",
                "-p",
                "my prompt",
                "--deny-tool=write",
                "--deny-tool=shell",
                "--no-custom-instructions",
                "--model",
                "claude-sonnet-4.6",
            ],
            60,
        )

    def test_36b_entitlement_rejection_raises_policy_error(self) -> None:
        """Regression: a copilot --model rejection is an entitlement failure
        (the flag needs Copilot Pro/Enterprise), but the vendor words it as a
        bad-model-id failure ('not available'), which sent two diagnoses down
        the wrong path on 2026-09-03. The re-raised error must state the real
        cause and the env var to unset, not just relay vendor text."""
        vendor = 'Error: Model "opencode-go/hy3" from --model flag is not available.'
        with (
            patch.object(
                llm_backends,
                "run_backend_command",
                side_effect=llm_backends.BackendError(f"exited 1: {vendor}"),
            ),
            self.assertRaises(llm_backends.BackendModelPolicyError) as cm,
        ):
            llm_backends.run_copilot("prompt", model="opencode-go/hy3", timeout=60)
        msg = str(cm.exception)
        # The true cause, in the code's own words -- never only vendor text.
        self.assertIn("Pro or Enterprise", msg)
        # What to unset on a free-tier machine.
        self.assertIn("SECOND_OPINION_COPILOT_MODEL_POOL", msg)
        # Vendor wording kept verbatim for the record; assert only the
        # stable fragment, not the whole sentence (they may reword).
        self.assertIn("from --model flag is not available", msg)
        # Strict-subclass contract: generic handlers keep working.
        self.assertIsInstance(cm.exception, llm_backends.BackendError)

    def test_36c_non_entitlement_failure_stays_plain_backend_error(self) -> None:
        with (
            patch.object(
                llm_backends,
                "run_backend_command",
                side_effect=llm_backends.BackendError("exited 1: quota exceeded"),
            ),
            self.assertRaises(llm_backends.BackendError) as cm,
        ):
            llm_backends.run_copilot("prompt", model="claude-sonnet-4.6", timeout=60)
        self.assertNotIsInstance(cm.exception, llm_backends.BackendModelPolicyError)
        self.assertIn("quota exceeded", str(cm.exception))


class RunPiTests(unittest.TestCase):
    def test_63_no_model_argv(self) -> None:
        with patch.object(
            llm_backends, "run_backend_command", return_value="text"
        ) as mock_run:
            result = llm_backends.run_pi("my prompt", model=None, timeout=60)
        self.assertEqual(result, "text")
        mock_run.assert_called_once_with(
            PI_ISOLATED + ["my prompt"],
            60,
        )

    def test_64_model_appends_flag(self) -> None:
        with patch.object(
            llm_backends, "run_backend_command", return_value="text"
        ) as mock_run:
            llm_backends.run_pi("my prompt", model="kimi-k2.6", timeout=60)
        mock_run.assert_called_once_with(
            PI_ISOLATED + ["--model", "kimi-k2.6", "my prompt"],
            60,
        )

    def test_65_leaked_tool_call_markup_raises(self) -> None:
        text = (
            '<tool_calls><invoke name="bash">'
            '<parameter name="command">ls -la</parameter></invoke></tool_calls>'
        )
        with (
            patch.object(llm_backends, "run_backend_command", return_value=text),
            self.assertRaises(llm_backends.BackendError) as cm,
        ):
            llm_backends.run_pi("prompt", model=None, timeout=60)
        self.assertIn("tool-call markup", str(cm.exception))

    def test_66_oversized_prompt_raises_without_spawning_command(self) -> None:
        oversized = "a" * (llm_backends.PI_MAX_PROMPT_BYTES + 1)
        with (
            patch.object(llm_backends, "build_isolated_command") as mock_build,
            patch.object(llm_backends, "run_backend_command") as mock_run,
            self.assertRaises(llm_backends.BackendPayloadSizeError) as cm,
        ):
            llm_backends.run_pi(oversized, model=None, timeout=60)
        mock_build.assert_not_called()
        mock_run.assert_not_called()
        err_msg = str(cm.exception)
        self.assertIn(str(len(oversized.encode())), err_msg)
        self.assertIn(str(llm_backends.PI_MAX_PROMPT_BYTES), err_msg)
        self.assertIn("opencode-go gateway", err_msg)

    def test_67_exact_limit_prompt_proceeds(self) -> None:
        exact = "a" * llm_backends.PI_MAX_PROMPT_BYTES
        with patch.object(
            llm_backends, "run_backend_command", return_value="ok"
        ) as mock_run:
            result = llm_backends.run_pi(exact, model=None, timeout=60)
        self.assertEqual(result, "ok")
        mock_run.assert_called_once()


class RunOpencodeTests(_ContainmentStubbed):
    def _run_command_returning(
        self, stdout: str, stderr: str = "", returncode: int = 0
    ) -> AbstractContextManager[object]:
        return patch.object(
            llm_backends, "_run_command", return_value=(returncode, stdout, stderr)
        )

    def test_37_text_chunks_concatenated(self) -> None:
        stdout = (
            json.dumps({"type": "text", "part": {"text": "Hello "}})
            + "\n"
            + json.dumps({"type": "text", "part": {"text": "world"}})
            + "\n"
        )
        with self._run_command_returning(stdout):
            self.assertEqual(
                llm_backends.run_opencode("prompt", model=None, timeout=60),
                "Hello world",
            )

    def test_38_model_flag_passed_through(self) -> None:
        with patch.object(
            llm_backends,
            "_run_command",
            return_value=(0, '{"type": "text", "part": {"text": "hi"}}\n', ""),
        ) as mock_run:
            llm_backends.run_opencode("prompt", model="claude-sonnet-4.6", timeout=60)
        argv = mock_run.call_args[0][0]
        self.assertEqual(argv[argv.index("-m") + 1], "claude-sonnet-4.6")
        self.assertEqual(argv[-1], "prompt")

    def test_39_no_model_argv(self) -> None:
        """opencode always runs as the adversary agent -- that is its declared
        tools mechanism -- and under containment, since it reads
        ~/.claude/CLAUDE.md globally with no flag to stop it."""
        with patch.object(
            llm_backends,
            "_run_command",
            return_value=(0, '{"type": "text", "part": {"text": "hi"}}\n', ""),
        ) as mock_run:
            llm_backends.run_opencode("prompt", model=None, timeout=60)
        argv, timeout = mock_run.call_args[0]
        self.assertEqual(timeout, 60)
        self.assertEqual(mock_run.call_args[1], {"retries": 1})
        self.assertEqual(argv[:3], ["unshare", "-Urm", "--map-root-user"])
        self.assertEqual(argv[argv.index("--agent") + 1], "adversary")
        self.assertNotIn("-m", argv)
        self.assertEqual(argv[-1], "prompt")

    def test_40_structured_error_event_raises_with_message(self) -> None:
        stdout = (
            json.dumps(
                {"type": "error", "error": {"data": {"message": "backend crashed"}}}
            )
            + "\n"
        )
        with (
            self._run_command_returning(stdout),
            self.assertRaises(llm_backends.BackendError) as cm,
        ):
            llm_backends.run_opencode("prompt", model=None, timeout=60)
        self.assertIn("backend crashed", str(cm.exception))

    def test_41_error_field_as_plain_string_does_not_crash(self) -> None:
        stdout = json.dumps({"type": "error", "error": "flat string error"}) + "\n"
        with (
            self._run_command_returning(stdout),
            self.assertRaises(llm_backends.BackendError) as cm,
        ):
            llm_backends.run_opencode("prompt", model=None, timeout=60)
        self.assertIn("flat string error", str(cm.exception))

    def test_42_no_text_no_error_falls_back_to_stderr(self) -> None:
        stdout = json.dumps({"type": "other"}) + "\n"
        with (
            self._run_command_returning(stdout, stderr="some stderr detail"),
            self.assertRaises(llm_backends.BackendError) as cm,
        ):
            llm_backends.run_opencode("prompt", model=None, timeout=60)
        self.assertIn("some stderr detail", str(cm.exception))

    def test_43_completely_unparseable_output_falls_back_to_stdout_snippet(
        self,
    ) -> None:
        with (
            self._run_command_returning("not json at all", stderr=""),
            self.assertRaises(llm_backends.BackendError) as cm,
        ):
            llm_backends.run_opencode("prompt", model=None, timeout=60)
        self.assertIn("not json at all", str(cm.exception))

    def test_44_tool_use_event_raises_backend_error(self) -> None:
        stdout = (
            json.dumps(
                {
                    "type": "tool_use",
                    "part": {"tool": "bash", "state": {"status": "completed"}},
                }
            )
            + "\n"
        )
        with (
            self._run_command_returning(stdout),
            self.assertRaises(llm_backends.BackendError) as cm,
        ):
            llm_backends.run_opencode("prompt", model=None, timeout=60)
        self.assertIn("bash", str(cm.exception))
        self.assertIn("tools instead of returning text", str(cm.exception))

    def test_45_tool_use_raises_even_with_text_chunks(self) -> None:
        # Regression: a text-only caller (a recap) must fail loud if the run
        # also performed real tool actions, not silently return the prose.
        stdout = (
            json.dumps({"type": "text", "part": {"text": "a recap"}})
            + "\n"
            + json.dumps(
                {
                    "type": "tool_use",
                    "part": {"tool": "read", "state": {"status": "completed"}},
                }
            )
            + "\n"
        )
        with (
            self._run_command_returning(stdout),
            self.assertRaises(llm_backends.BackendError) as cm,
        ):
            llm_backends.run_opencode("prompt", model=None, timeout=60)
        self.assertIn("read", str(cm.exception))

    def test_48_emitted_tool_call_markup_text_raises(self) -> None:
        # Regression: when every tool is denied (e.g. the adversary agent's
        # "permission": "deny"), a tool-hungry model can emit its attempted
        # tool calls as literal <tool_calls> XML inside a text event rather
        # than a real tool_use event. That has no `_raise_on_tool_use` match
        # and would otherwise be returned verbatim as the critique.
        text = (
            "\n\n<tool_calls>\n"
            '<invoke name="bash">\n'
            '<parameter name="command" string="true">ls -la</parameter>\n'
            "</invoke>\n"
            "</tool_calls>"
        )
        stdout = json.dumps({"type": "text", "part": {"text": text}}) + "\n"
        with (
            self._run_command_returning(stdout),
            self.assertRaises(llm_backends.BackendError) as cm,
        ):
            llm_backends.run_opencode("prompt", model=None, timeout=60)
        self.assertIn("tool-call markup", str(cm.exception))

    def test_49_prose_merely_mentioning_tool_calls_passes(self) -> None:
        text = (
            "The plan's agent config will never emit <tool_calls> wrappers or "
            '<invoke name="bash"> markup since every tool is denied, so this '
            "risk is moot."
        )
        stdout = json.dumps({"type": "text", "part": {"text": text}}) + "\n"
        with self._run_command_returning(stdout):
            self.assertEqual(
                llm_backends.run_opencode("prompt", model=None, timeout=60), text
            )


class ToolCallLeakDetectionTests(unittest.TestCase):
    """Direct tests of _raise_on_emitted_tool_call's shape coverage and the
    dominance-ratio false-positive guard, bypassing the subprocess/event
    plumbing that OpencodeCommandTests exercises above."""

    def _assert_leak(self, text: str) -> None:
        with self.assertRaises(llm_backends.BackendError) as cm:
            llm_backends._raise_on_emitted_tool_call(text, context="ctx")
        self.assertIn("tool-call markup", str(cm.exception))

    def _assert_no_leak(self, text: str) -> None:
        llm_backends._raise_on_emitted_tool_call(text, context="ctx")  # no raise

    def test_50_reordered_invoke_attributes_raises(self) -> None:
        # Regression: attribute order isn't guaranteed — `name` need not be
        # the first attribute on <invoke>.
        self._assert_leak(
            '<invoke id="1" name="bash"><parameter name="command">ls</parameter></invoke>'
        )

    def test_51_zero_argument_invoke_raises(self) -> None:
        # Regression: a no-argument tool call has no <parameter> tag at all.
        self._assert_leak('<invoke name="list_files"></invoke>')

    def test_52_invoke_without_tool_calls_wrapper_raises(self) -> None:
        # Regression: a swapped-in model can emit the inner <invoke> block
        # without ever wrapping it in <tool_calls>.
        self._assert_leak(
            '<invoke name="bash"><parameter name="command">ls</parameter></invoke>'
        )

    def test_53_json_tool_calls_array_raises(self) -> None:
        self._assert_leak('{"tool_calls": [{"name": "bash", "arguments": {}}]}')

    def test_54_json_tool_use_type_raises(self) -> None:
        self._assert_leak('{"type": "tool_use", "name": "bash", "input": {}}')

    def test_55_fenced_tool_call_block_raises(self) -> None:
        # Regression: wrapping the leaked markup in a code fence must not
        # hide it from detection.
        self._assert_leak(
            '```xml\n<tool_calls><invoke name="bash">'
            '<parameter name="command">ls</parameter></invoke></tool_calls>\n```'
        )

    def test_56_unclosed_tag_mention_passes(self) -> None:
        # No closing marker anywhere — must not fall back to matching to
        # end-of-string, or any prose mentioning an opening tag would leak-flag.
        self._assert_no_leak(
            "If a tool would help here, note that <tool_calls> is unavailable."
        )

    def test_57_quoted_example_in_larger_critique_passes(self) -> None:
        # Regression: a real, multi-paragraph critique that illustrates this
        # exact failure mode with one fully-closed example snippet must not
        # be rejected as if the whole response were the leaked call — the
        # snippet is a small fraction of the total response.
        critique = (
            "The plan's regex backstop for the adversary agent has a real gap: "
            "several tool-call leak shapes still pass through undetected. For "
            "example, catching leaks shaped like "
            '<tool_calls><invoke name="bash"><parameter name="command">ls -la'
            "</parameter></invoke></tool_calls> is good, but the plan's regex "
            "requires that exact attribute order and tag sequence, so a "
            "reordered or zero-argument invoke slips through silently. "
            "I'd also flag that the plan never accounts for JSON-shaped "
            'tool-call blocks like {"tool_calls": [...]}, which some backends '
            "emit instead of XML. Recommend broadening the pattern set and "
            "adding a dominance check so a quoted example like the one above, "
            "embedded in an otherwise-substantive critique, isn't itself "
            "mistaken for a leaked call. Overall the core permission-deny "
            "approach is sound; it's specifically the text-based backstop "
            "that needs the extra coverage described above before this is "
            "ready to rely on for every model that might get swapped in."
        )
        self._assert_no_leak(critique)

    def test_58_scan_is_bounded_on_repetitive_input(self) -> None:
        # Regression: unbounded scanning measured 15-42s on 500KB-2MB of
        # repeated markup-like text (a realistic LLM repetition-loop output).
        # The scan window must keep this well under a second regardless of
        # total input size.
        huge = '<tool_calls><invoke name="x">' * 200_000  # ~5.8MB, never closed
        start = time.monotonic()
        self._assert_no_leak(huge)  # never closes -> no match, must return fast
        self.assertLess(time.monotonic() - start, 2.0)


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


class _LogPathRedirected(unittest.TestCase):
    """Base for suites that exercise real logging: redirects
    _backend_call_log_path to a tmp file so nothing touches the sandboxed
    HOME's ~/.claude/data/ directly."""

    def setUp(self) -> None:
        super().setUp()
        self.log_path = Path(tempfile.mkdtemp()) / "backend_calls.jsonl"
        patcher = patch.object(
            llm_backends, "_backend_call_log_path", lambda: self.log_path
        )
        patcher.start()
        self.addCleanup(patcher.stop)


class LogBackendCallTests(_LogPathRedirected):
    def test_66_appends_one_line_with_expected_fields(self) -> None:
        llm_backends._log_backend_call("pi", "kimi-k2.6", "success", 1.234, 512)
        lines = _read_jsonl(self.log_path)
        self.assertEqual(len(lines), 1)
        record = lines[0]
        self.assertEqual(record["backend"], "pi")
        self.assertEqual(record["model"], "kimi-k2.6")
        self.assertEqual(record["outcome"], "success")
        self.assertEqual(record["wall_seconds"], 1.23)
        self.assertEqual(record["prompt_bytes"], 512)
        self.assertIn("ts", record)

    def test_67_model_none_serializes_to_null(self) -> None:
        llm_backends._log_backend_call("opencode", None, "success", 0.5, 10)
        record = _read_jsonl(self.log_path)[0]
        self.assertIsNone(record["model"])

    def test_68_creates_parent_directory(self) -> None:
        nested = self.log_path.parent / "not-yet-created" / "backend_calls.jsonl"
        with patch.object(llm_backends, "_backend_call_log_path", lambda: nested):
            self.assertFalse(nested.parent.exists())
            llm_backends._log_backend_call("agy", "m", "success", 0.1, 1)
            self.assertTrue(nested.exists())

    def test_69_write_failure_swallowed_not_raised(self) -> None:
        with patch.object(Path, "open", side_effect=OSError("disk full")):
            llm_backends._log_backend_call("agy", "m", "error", 0.1, 1)  # no raise

    def test_70_two_calls_append_two_separate_lines(self) -> None:
        llm_backends._log_backend_call("agy", "m1", "success", 0.1, 1)
        llm_backends._log_backend_call("copilot", "m2", "timeout", 5.0, 999)
        lines = _read_jsonl(self.log_path)
        self.assertEqual(len(lines), 2)
        self.assertEqual([r["backend"] for r in lines], ["agy", "copilot"])


class TrackBackendCallTests(_LogPathRedirected):
    def test_71_success_logs_success_outcome(self) -> None:
        with llm_backends._track_backend_call("pi", "m", "a prompt"):
            pass
        record = _read_jsonl(self.log_path)[0]
        self.assertEqual(record["outcome"], "success")
        self.assertEqual(record["prompt_bytes"], len(b"a prompt"))

    def test_72_backend_timeout_error_logs_timeout_and_reraises(self) -> None:
        with (
            self.assertRaises(llm_backends.BackendTimeoutError),
            llm_backends._track_backend_call("opencode", "m", "p"),
        ):
            raise llm_backends.BackendTimeoutError("timed out after 60s — killed")
        record = _read_jsonl(self.log_path)[0]
        self.assertEqual(record["outcome"], "timeout")

    def test_73_backend_error_logs_error_and_reraises(self) -> None:
        with (
            self.assertRaises(llm_backends.BackendError),
            llm_backends._track_backend_call("copilot", "m", "p"),
        ):
            raise llm_backends.BackendError("exited 1: boom")
        record = _read_jsonl(self.log_path)[0]
        self.assertEqual(record["outcome"], "error")

    def test_74_unanticipated_exception_logs_error_and_reraises(self) -> None:
        # Not a BackendError at all -- proves the broad except in
        # _track_backend_call still logs one "error" line and still
        # re-raises the original exception unchanged, rather than either
        # silently dropping the log line or masking the real error.
        with (
            self.assertRaises(ValueError),
            llm_backends._track_backend_call("agy", "m", "p"),
        ):
            raise ValueError("something else broke")
        record = _read_jsonl(self.log_path)[0]
        self.assertEqual(record["outcome"], "error")


class RunFunctionsLoggingTests(_ContainmentStubbed, _LogPathRedirected):
    """Integration: each run_* function's actual logging wiring, not just
    _track_backend_call in isolation."""

    def test_75_run_pi_success_appends_line(self) -> None:
        with patch.object(llm_backends, "run_backend_command", return_value="text"):
            result = llm_backends.run_pi("my prompt", model="kimi-k2.6", timeout=60)
        self.assertEqual(result, "text")
        record = _read_jsonl(self.log_path)[0]
        self.assertEqual(record, record | {"backend": "pi", "outcome": "success"})

    def test_76_run_opencode_success_appends_line_no_extraction_break(self) -> None:
        stdout = json.dumps({"type": "text", "part": {"text": "hi"}}) + "\n"
        with patch.object(llm_backends, "_run_command", return_value=(0, stdout, "")):
            result = llm_backends.run_opencode("prompt", model=None, timeout=60)
        self.assertEqual(result, "hi")
        record = _read_jsonl(self.log_path)[0]
        self.assertEqual(record["backend"], "opencode")
        self.assertEqual(record["outcome"], "success")

    def test_77_run_opencode_backend_error_appends_error_line(self) -> None:
        with (
            patch.object(
                llm_backends, "_run_command", return_value=(0, "not json", "")
            ),
            self.assertRaises(llm_backends.BackendError),
        ):
            llm_backends.run_opencode("prompt", model=None, timeout=60)
        record = _read_jsonl(self.log_path)[0]
        self.assertEqual(record["outcome"], "error")

    def test_78_isolation_error_before_call_is_never_logged(self) -> None:
        with (
            patch.object(llm_backends, "containment_available", lambda: False),
            self.assertRaises(llm_backends.IsolationError),
        ):
            llm_backends.run_agy("p", model="m", timeout=60)
        self.assertEqual(_read_jsonl(self.log_path), [])


class OpencodeToolUseEventsTests(unittest.TestCase):
    def test_46_filters_only_tool_use_events(self) -> None:
        events: list[dict[str, object]] = [
            {"type": "text", "part": {"text": "hi"}},
            {"type": "tool_use", "part": {"tool": "bash"}},
            {"type": "step_start"},
            {"type": "tool_use", "part": {"tool": "read"}},
        ]
        self.assertEqual(
            [
                e["part"]["tool"]  # type: ignore[index]
                for e in llm_backends._opencode_tool_use_events(events)
            ],
            ["bash", "read"],
        )

    def test_47_no_tool_use_returns_empty(self) -> None:
        events: list[dict[str, object]] = [
            {"type": "text", "part": {"text": "hi"}},
            {"type": "step_finish"},
        ]
        self.assertEqual(llm_backends._opencode_tool_use_events(events), [])


if __name__ == "__main__":
    unittest.main(verbosity=1)
