#!/usr/bin/env python3
"""Tests for harness_discovery_check.py. Run with: python3 test_harness_discovery_check.py

Covers the session-start ``check`` tier (per-machine probe records, the
background probe launch, its lock and recursion guard) and the ``probe``
tier (random-token fixture, confirmation runs, record writing).

No real harness binaries are invoked — the subprocess layer is fully
mocked, and every test redirects ``XDG_STATE_HOME`` to a temp dir (the test
bootstrap redirects ``HOME`` but not ``XDG_STATE_HOME``).
"""

import fcntl
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Callable, Sequence
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent-scripts"))
import test_bootstrap  # noqa: E402
import harness_discovery_check as hdc
from pytest_shim import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

# Which fixture files each fake harness "loads" — the measured behavior the
# real probe expects (harness_spec.probe_expected_root).
LOADS_OK: dict[str, list[str]] = {
    "claude": ["CLAUDE.md"],
    "opencode": ["AGENTS.md"],
    "pi": ["AGENTS.md"],
    "copilot": ["CLAUDE.md", "GEMINI.md", "AGENTS.md"],
    "agy": [],
    "codex": ["AGENTS.md"],
}


def _make_result(
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
) -> object:
    """Return a fake subprocess.CompletedProcess-like object."""

    class _Result:
        def __init__(self) -> None:
            self.stdout = stdout
            self.stderr = stderr
            self.returncode = returncode

    return _Result()


def fixture_run_factory(
    loads: dict[str, object] | None = None,
    *,
    version: str = "1.0.0",
    fail_probe: Sequence[str] = (),
    timeout_probe: Sequence[str] = (),
    echo_prompt: Sequence[str] = (),
    log: list[dict[str, object]] | None = None,
) -> Callable[..., object]:
    """Return a fake ``subprocess.run``.

    A probe call answers with the token lines of the fixture files the fake
    harness "loads" (read from the call's ``cwd``), so it can only report a
    token that is really in the fixture. ``loads[name]`` may be a callable
    returning the file list, for answers that change between calls.
    """
    loads = LOADS_OK if loads is None else loads

    def fake_run(cmd: Sequence[str], **kwargs: object) -> object:
        name = Path(cmd[0]).name
        if name == "git":
            return _make_result()
        if "--version" in cmd:
            return _make_result(stdout=f"{version}\n")
        if log is not None:
            log.append(
                {
                    "name": name,
                    "prompt": cmd[-1],
                    "env": kwargs.get("env"),
                    "cwd": kwargs.get("cwd"),
                }
            )
        if name in timeout_probe:
            raise TimeoutError("timed out")
        if name in fail_probe:
            return _make_result(stderr="auth failed", returncode=1)
        if name in echo_prompt:
            return _make_result(stdout=str(cmd[-1]))
        spec = loads.get(name, [])
        files = spec() if callable(spec) else spec
        cwd = Path(str(kwargs["cwd"]))
        found = [(cwd / rel).read_text().strip() for rel in files]  # type: ignore[union-attr]
        return _make_result(stdout=("Markers: " + ", ".join(found)) if found else "none")

    return fake_run


def fake_resolve_binary(name: str) -> Path | None:
    """Return a deterministic fake path so ``Path(binary).name`` yields the
    harness name itself."""
    return Path(f"/fake/bin/{name}")


class StateTestCase(unittest.TestCase):
    """Real temp-dir binaries (stat-able) and a throwaway XDG_STATE_HOME."""

    NOW = 1_800_000_000.0

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.state_root = self.root / "state"
        env = patch.dict(
            os.environ,
            {
                "XDG_STATE_HOME": str(self.state_root),
                "XDG_CACHE_HOME": str(self.root / "cache"),
            },
        )
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop(hdc.GUARD_ENV, None)
        self.missing: set[str] = set()
        resolve = patch.object(hdc, "resolve_binary", self._fake_binary)
        resolve.start()
        self.addCleanup(resolve.stop)
        self.launches: list[tuple[str, tuple[str, int, int]]] = []
        self.launch_result: str | None = None

    def _fake_binary(self, name: str) -> Path | None:
        if name in self.missing:
            return None
        path = self.bin_dir / name
        if not path.exists():
            path.write_text(f"#!/bin/sh\necho {name}\n")
        return path

    def identity(self, name: str) -> tuple[str, int, int]:
        binary = self._fake_binary(name)
        assert binary is not None
        key = hdc._stat_key(binary)
        assert key is not None
        return key

    def write(self, name: str, status: str, **overrides: object) -> None:
        path, mtime_ns, size = self.identity(name)
        record: dict[str, object] = {
            "identity": {"path": path, "mtime_ns": mtime_ns, "size": size},
            "version": "1.0.0",
            "probe_schema": hdc.probe_schema(),
            "status": status,
            "detail": None if status == "HOLD" else f"{name} detail",
            "checked_at": self.NOW - 60,
            "error_count": 0,
            "next_retry_at": None,
        }
        record.update(overrides)
        hdc._record_path(name).parent.mkdir(parents=True, exist_ok=True)
        hdc._record_path(name).write_text(json.dumps(record))

    def hold_all(self) -> None:
        for name in hdc._LOAD_BEARING:
            self.write(name, "HOLD")

    def fake_launch(self, name: str, identity: tuple[str, int, int]) -> str | None:
        self.launches.append((name, identity))
        return self.launch_result

    def run_check(
        self,
        *,
        hook: bool = True,
        strict: bool = False,
        now: float | None = None,
    ) -> tuple[int, str]:
        out = io.StringIO()
        err = io.StringIO()

        def no_subprocess(*_a: object, **_k: object) -> object:
            raise AssertionError("check must not spawn a subprocess")

        with (
            patch("sys.stdout", out),
            patch("sys.stderr", err),
            patch.object(hdc.subprocess, "run", no_subprocess),
        ):
            code = hdc.cmd_check(
                hook=hook,
                strict=strict,
                now=self.NOW if now is None else now,
                launch=self.fake_launch,
            )
        return code, out.getvalue() + err.getvalue()


class HookCheckTestCase(StateTestCase):
    @pytest.mark.regression(
        "harness-upgrade-nags-until-repo-repin",
        "AttributeError: module 'harness_discovery_check' has no attribute 'GUARD_ENV'",
    )
    def test_unverified_binary_launches_probe_silently(self) -> None:
        code, output = self.run_check()
        self.assertEqual(code, 0)
        self.assertEqual(output, "")
        self.assertEqual(
            [name for name, _ in self.launches], list(hdc._LOAD_BEARING)
        )
        self.assertEqual(self.launches[0][1], self.identity(hdc._LOAD_BEARING[0]))

    def test_hold_record_is_silent_and_launches_nothing(self) -> None:
        self.hold_all()
        code, output = self.run_check()
        self.assertEqual((code, output, self.launches), (0, "", []))

    def test_broken_record_prints_note(self) -> None:
        self.hold_all()
        self.write("claude", "BROKEN", detail="missing ['FIXTURE_TOKEN_CLAUDE_ROOT']")
        code, output = self.run_check()
        self.assertEqual(code, 0)
        self.assertIn("claude", output)
        self.assertIn("FIXTURE_TOKEN_CLAUDE_ROOT", output)
        self.assertIn("probe --harness claude", output)
        self.assertEqual(self.launches, [])

    def test_changed_identity_counts_as_missing(self) -> None:
        self.hold_all()
        (self.bin_dir / "claude").write_text("#!/bin/sh\necho upgraded build\n")
        code, output = self.run_check()
        self.assertEqual((code, output), (0, ""))
        self.assertEqual([n for n, _ in self.launches], ["claude"])

    def test_changed_probe_schema_counts_as_missing(self) -> None:
        self.hold_all()
        self.write("opencode", "HOLD", probe_schema="stale")
        self.run_check()
        self.assertEqual([n for n, _ in self.launches], ["opencode"])

    def test_corrupt_record_triggers_reprobe(self) -> None:
        self.hold_all()
        hdc._record_path("claude").write_text("{not json")
        code, output = self.run_check()
        self.assertEqual((code, output), (0, ""))
        self.assertEqual([n for n, _ in self.launches], ["claude"])

    def test_error_record_waits_for_retry_time(self) -> None:
        self.hold_all()
        self.write("claude", "ERROR", error_count=1, next_retry_at=self.NOW + 10)
        code, output = self.run_check()
        self.assertEqual((code, output, self.launches), (0, "", []))
        self.run_check(now=self.NOW + 11)
        self.assertEqual([n for n, _ in self.launches], ["claude"])

    def test_third_error_prints_could_not_verify(self) -> None:
        self.hold_all()
        self.write(
            "claude",
            "ERROR",
            version="2.1.290",
            detail="claude: probe exited nonzero: auth failed",
            error_count=3,
            next_retry_at=self.NOW + 100,
        )
        code, output = self.run_check()
        self.assertEqual(code, 0)
        self.assertIn("could not verify claude 2.1.290", output)
        self.assertIn("auth failed", output)
        self.assertNotIn("changed", output)

    def test_fourth_error_stops_automatic_retries(self) -> None:
        self.hold_all()
        self.write("claude", "ERROR", error_count=4, next_retry_at=None)
        self.run_check(now=self.NOW + 10**8)
        self.assertEqual(self.launches, [])

    def test_missing_binary_is_silent(self) -> None:
        self.missing = {"claude", "opencode"}
        code, output = self.run_check(strict=True)
        self.assertEqual((code, output, self.launches), (0, "", []))

    def test_strict_exit_codes(self) -> None:
        code, _ = self.run_check(strict=True)
        self.assertEqual(code, 2, "pending")
        self.hold_all()
        code, _ = self.run_check(strict=True)
        self.assertEqual(code, 0, "all HOLD")
        for status in ("BROKEN", "ERROR"):
            with self.subTest(status=status):
                self.write("claude", status, next_retry_at=self.NOW + 100)
                code, _ = self.run_check(strict=True)
                self.assertEqual(code, 2)

    def test_guard_env_makes_check_a_noop(self) -> None:
        with patch.dict(os.environ, {hdc.GUARD_ENV: "1"}):
            code, output = self.run_check(strict=True)
        self.assertEqual((code, output, self.launches), (0, "", []))

    def test_launch_failure_records_error_silently(self) -> None:
        self.launch_result = "Popen failed: boom"
        code, output = self.run_check()
        self.assertEqual((code, output), (0, ""))
        record = hdc.read_record("claude")
        assert record is not None
        self.assertEqual(record["status"], "ERROR")
        self.assertEqual(record["error_count"], 1)
        self.assertIn("boom", str(record["detail"]))
        self.assertEqual(record["next_retry_at"], self.NOW + hdc._RETRY_DELAYS[0])

    def test_launch_failure_without_writable_state_prints_one_line(self) -> None:
        self.launch_result = "lock file not openable"
        self.state_root.write_text("not a directory")
        code, output = self.run_check()
        self.assertEqual(code, 0)
        self.assertIn("cannot verify claude", output)
        self.assertEqual(len(output.strip().splitlines()), len(hdc._LOAD_BEARING))

    def test_manual_check_reports_missing_and_pending(self) -> None:
        self.missing = {"opencode"}
        code, output = self.run_check(hook=False)
        self.assertEqual(code, 0)
        self.assertIn("opencode", output)
        self.assertIn("not installed", output)
        self.assertIn("claude", output)
        self.assertIn("not verified", output)
        self.assertEqual(self.launches, [], "a manual check never launches")


class LaunchTestCase(StateTestCase):
    def test_launch_detaches_and_hands_over_the_lock(self) -> None:
        seen: dict[str, object] = {}
        identity = self.identity("claude")

        def fake_popen(argv: Sequence[str], **kwargs: object) -> object:
            seen["argv"] = list(argv)
            seen["kwargs"] = kwargs
            # While the probe is being started the lock must already be held.
            with open(hdc._lock_path("claude"), "a") as handle:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return object()

        self.assertIsNone(hdc._launch_probe("claude", identity, popen=fake_popen))
        argv = seen["argv"]
        kwargs = seen["kwargs"]
        assert isinstance(argv, list) and isinstance(kwargs, dict)
        self.assertIn("probe", argv)
        self.assertIn("--record", argv)
        self.assertEqual(argv[argv.index("--harness") + 1], "claude")
        self.assertEqual(
            argv[argv.index("--expect-identity") + 1],
            hdc._format_identity(identity),
        )
        lock_fd = int(argv[argv.index("--lock-fd") + 1])
        self.assertTrue(kwargs["start_new_session"])
        self.assertEqual(kwargs["pass_fds"], (lock_fd,))
        for stream in ("stdin", "stdout", "stderr"):
            self.assertIs(kwargs[stream], subprocess.DEVNULL)

    def test_held_lock_blocks_a_second_launch(self) -> None:
        calls: list[object] = []
        path = hdc._lock_path("claude")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = hdc._launch_probe(
                "claude", self.identity("claude"), popen=lambda *a, **k: calls.append(a)
            )
        self.assertIsNone(result)
        self.assertEqual(calls, [])

    def test_popen_failure_is_reported_and_releases_the_lock(self) -> None:
        def failing_popen(*_a: object, **_k: object) -> object:
            raise OSError("no exec")

        result = hdc._launch_probe("claude", self.identity("claude"), popen=failing_popen)
        self.assertIsNotNone(result)
        self.assertIn("no exec", str(result))
        with open(hdc._lock_path("claude"), "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)


class RecordProbeTestCase(StateTestCase):
    def run_probe(
        self,
        fake_run: Callable[..., object],
        *,
        harness: str = "claude",
        record: bool = True,
        expect_identity: str | None = None,
        now: float | None = None,
    ) -> tuple[int, str]:
        out = io.StringIO()
        with patch("sys.stdout", out):
            code = hdc.cmd_probe(
                harness=harness,
                record=record,
                expect_identity=expect_identity,
                run_command=fake_run,
                now=self.NOW if now is None else now,
            )
        return code, out.getvalue()

    def test_hold_is_recorded_with_identity_schema_and_version(self) -> None:
        code, _ = self.run_probe(fixture_run_factory(version="2.1.290 (Claude Code)"))
        self.assertEqual(code, 0)
        record = hdc.read_record("claude")
        assert record is not None
        self.assertEqual(record["status"], "HOLD")
        self.assertEqual(record["version"], "2.1.290")
        self.assertEqual(record["probe_schema"], hdc.probe_schema())
        self.assertEqual(record["error_count"], 0)
        path, mtime_ns, size = self.identity("claude")
        self.assertEqual(
            record["identity"], {"path": path, "mtime_ns": mtime_ns, "size": size}
        )

    def test_single_mismatch_then_hold_records_inconsistent_error(self) -> None:
        answers = iter([["AGENTS.md"], ["CLAUDE.md"]])
        fake = fixture_run_factory({"claude": lambda: next(answers)})
        self.run_probe(fake)
        record = hdc.read_record("claude")
        assert record is not None
        self.assertEqual(record["status"], "ERROR")
        self.assertIn("inconsistent", str(record["detail"]))
        self.assertEqual(record["error_count"], 1)

    def test_two_mismatches_record_broken(self) -> None:
        log: list[dict[str, object]] = []
        fake = fixture_run_factory({"claude": ["AGENTS.md"]}, log=log)
        code, output = self.run_probe(fake)
        self.assertEqual(code, 1)
        self.assertIn("BROKEN", output)
        record = hdc.read_record("claude")
        assert record is not None
        self.assertEqual(record["status"], "BROKEN")
        self.assertIn("FIXTURE_TOKEN_CLAUDE_ROOT", str(record["detail"]))
        self.assertEqual(len(log), 2, "exactly one confirming re-run")
        self.assertNotEqual(log[0]["cwd"], log[1]["cwd"], "fresh fixture per run")

    def test_identity_change_mid_probe_records_nothing(self) -> None:
        stale = hdc._format_identity(self.identity("claude"))
        (self.bin_dir / "claude").write_text("#!/bin/sh\necho replaced\n")
        self.run_probe(fixture_run_factory(), expect_identity=stale)
        self.assertIsNone(hdc.read_record("claude"))

    def test_error_count_and_retry_schedule(self) -> None:
        fake = fixture_run_factory(fail_probe=("claude",))
        with patch.object(hdc, "_PROBE_ATTEMPTS", 1):
            expected_delays = list(hdc._RETRY_DELAYS) + [None]
            for count, delay in enumerate(expected_delays, start=1):
                with self.subTest(count=count):
                    self.run_probe(fake)
                    record = hdc.read_record("claude")
                    assert record is not None
                    self.assertEqual(record["error_count"], count)
                    self.assertEqual(
                        record["next_retry_at"],
                        None if delay is None else self.NOW + delay,
                    )
                    self.assertIn("auth failed", str(record["detail"]))
        self.run_probe(fixture_run_factory())
        record = hdc.read_record("claude")
        assert record is not None
        self.assertEqual((record["status"], record["error_count"]), ("HOLD", 0))

    def test_error_count_resets_on_new_identity(self) -> None:
        self.write("claude", "ERROR", error_count=2)
        (self.bin_dir / "claude").write_text("#!/bin/sh\necho new build\n")
        with patch.object(hdc, "_PROBE_ATTEMPTS", 1):
            self.run_probe(fixture_run_factory(fail_probe=("claude",)))
        record = hdc.read_record("claude")
        assert record is not None
        self.assertEqual(record["error_count"], 1)

    def test_plain_probe_writes_nothing(self) -> None:
        self.run_probe(fixture_run_factory(), record=False)
        self.assertIsNone(hdc.read_record("claude"))

    def test_two_harnesses_keep_both_records(self) -> None:
        self.run_probe(fixture_run_factory(), harness="claude")
        self.run_probe(fixture_run_factory(), harness="opencode")
        self.assertIsNotNone(hdc.read_record("claude"))
        self.assertIsNotNone(hdc.read_record("opencode"))

    def test_children_get_the_recursion_guard_and_pwd(self) -> None:
        log: list[dict[str, object]] = []
        self.run_probe(fixture_run_factory(log=log))
        env = log[0]["env"]
        assert isinstance(env, dict)
        self.assertEqual(env.get(hdc.GUARD_ENV), "1")
        self.assertEqual(env.get("PWD"), str(log[0]["cwd"]))

    def test_manual_record_refuses_while_another_probe_holds_the_lock(self) -> None:
        path = hdc._lock_path("claude")
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            code, output = self.run_probe(fixture_run_factory())
        self.assertEqual(code, 1)
        self.assertIn("already running", output)
        self.assertIsNone(hdc.read_record("claude"))


class ProbeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        resolve = patch.object(hdc, "resolve_binary", fake_resolve_binary)
        resolve.start()
        self.addCleanup(resolve.stop)

    def run_probe(
        self,
        *,
        harness: str | None = None,
        fake_run: Callable[..., object] | None = None,
    ) -> tuple[int, str]:
        out = io.StringIO()
        with patch("sys.stdout", out):
            code = hdc.cmd_probe(harness=harness, run_command=fake_run)
        return code, out.getvalue()

    def test_all_hold(self) -> None:
        code, output = self.run_probe(fake_run=fixture_run_factory())
        self.assertEqual(code, 0)
        self.assertIn("HOLD", output)
        self.assertNotIn("BROKEN", output)
        self.assertNotIn("ERROR", output)

    def test_broken_row(self) -> None:
        fake = fixture_run_factory({"claude": ["AGENTS.md"]})
        code, output = self.run_probe(harness="claude", fake_run=fake)
        self.assertEqual(code, 1)
        self.assertIn("BROKEN", output)
        self.assertIn("unexpected", output)
        self.assertIn("missing", output)
        self.assertIn("harness_spec.py", output)

    def test_error_on_timeout(self) -> None:
        fake = fixture_run_factory(timeout_probe=("opencode",))
        code, output = self.run_probe(harness="opencode", fake_run=fake)
        self.assertEqual(code, 1)
        self.assertIn("ERROR", output)
        self.assertIn("timed out", output)

    def test_error_on_nonzero_exit(self) -> None:
        fake = fixture_run_factory(fail_probe=("pi",))
        code, output = self.run_probe(harness="pi", fake_run=fake)
        self.assertEqual(code, 1)
        self.assertIn("auth failed", output)

    def test_retry_on_empty_response(self) -> None:
        answers = iter([[], ["AGENTS.md"]])
        fake = fixture_run_factory({"opencode": lambda: next(answers)})
        code, output = self.run_probe(harness="opencode", fake_run=fake)
        self.assertEqual(code, 0)
        self.assertIn("HOLD", output)

    def test_echoing_the_prompt_yields_no_tokens(self) -> None:
        """The prompt names no token, so an answer that merely repeats it
        can never count as a loaded file."""
        fake = fixture_run_factory(echo_prompt=("claude",))
        with patch.object(hdc, "_PROBE_ATTEMPTS", 1):
            code, output = self.run_probe(harness="claude", fake_run=fake)
        self.assertEqual(code, 1)
        self.assertIn("ERROR", output)
        self.assertNotIn("BROKEN", output)

    def test_tokens_are_random_per_run_and_absent_from_prompt(self) -> None:
        log: list[dict[str, object]] = []
        fake = fixture_run_factory({"claude": ["AGENTS.md"]}, log=log)
        self.run_probe(harness="claude", fake_run=fake)
        self.run_probe(harness="claude", fake_run=fake)
        tokens = []
        for entry in log:
            fixture = Path(str(entry["cwd"]))
            tokens.append((fixture / "AGENTS.md").read_text().strip()) if fixture.exists() else None
        prompts = {str(entry["prompt"]) for entry in log}
        self.assertEqual(len(prompts), 1, "the prompt is fixed; only the fixture changes")
        prompt = prompts.pop()
        for label in hdc._ALL_TOKENS:
            self.assertNotIn(label, prompt)

    def test_fixture_tokens_differ_between_runs(self) -> None:
        first = hdc._new_tokens()
        second = hdc._new_tokens()
        self.assertEqual(set(first), set(hdc._ALL_TOKENS))
        self.assertTrue(set(first.values()).isdisjoint(second.values()))
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            hdc._build_fixture(repo, first, run_command=lambda cmd, **kw: _make_result())
            self.assertIn(first[hdc._TOKEN_AGENTS_ROOT], (repo / "AGENTS.md").read_text())
            self.assertIn(first[hdc._TOKEN_GEMINI_SUB], (repo / "sub" / "GEMINI.md").read_text())

    def test_parse_tokens_maps_values_back_to_labels(self) -> None:
        tokens = hdc._new_tokens()
        text = f"I see {tokens[hdc._TOKEN_AGENTS_ROOT]} and also {hdc._TOKEN_CLAUDE_ROOT}."
        self.assertEqual(hdc._parse_tokens(text, tokens), {hdc._TOKEN_AGENTS_ROOT})
        self.assertEqual(hdc._parse_tokens("none", tokens), set())

    def test_single_harness_probe(self) -> None:
        code, output = self.run_probe(harness="pi", fake_run=fixture_run_factory())
        self.assertEqual(code, 0)
        self.assertIn("pi", output)
        self.assertNotIn("claude", output)

    def test_probe_runs_the_resolved_binary(self) -> None:
        """The probe must run the file whose identity is recorded — also a
        fallback-path binary that is not on ``PATH``."""
        log: list[dict[str, object]] = []
        seen: list[Sequence[str]] = []
        inner = fixture_run_factory(log=log)

        def fake_run(cmd: Sequence[str], **kwargs: object) -> object:
            seen.append(cmd)
            return inner(cmd, **kwargs)

        self.run_probe(harness="opencode", fake_run=fake_run)
        probe_calls = [cmd for cmd in seen if "--version" not in cmd and Path(cmd[0]).name != "git"]
        self.assertEqual(probe_calls[0][0], "/fake/bin/opencode")


class RemovedPinsTestCase(unittest.TestCase):
    def test_no_version_pin_constants_remain(self) -> None:
        for name in (
            "CLAUDE_CODE_PINNED_VERSION",
            "OPENCODE_PINNED_VERSION",
            "PI_PINNED_VERSION",
            "COPILOT_PINNED_VERSION",
            "AGY_PINNED_VERSION",
            "CODEX_PINNED_VERSION",
            "pinned_version",
        ):
            with self.subTest(name=name):
                self.assertFalse(hasattr(hdc, name))

    def test_probe_schema_tracks_expectations(self) -> None:
        import harness_spec

        before = hdc.probe_schema()
        spec = harness_spec.HARNESSES["claude"]
        altered = type(spec)(**{**spec.__dict__, "probe_expected_root": frozenset({"X"})})
        with patch.dict(harness_spec.HARNESSES, {"claude": altered}):
            self.assertNotEqual(hdc.probe_schema(), before)
        with patch.object(hdc, "PROBE_LOGIC_VERSION", hdc.PROBE_LOGIC_VERSION + 1):
            self.assertNotEqual(hdc.probe_schema(), before)


class VersionExtractionTestCase(unittest.TestCase):
    def test_claude(self) -> None:
        self.assertEqual(
            hdc._extract_version("claude", "2.1.252 (Claude Code)"), "2.1.252"
        )

    def test_opencode(self) -> None:
        self.assertEqual(hdc._extract_version("opencode", "1.18.25"), "1.18.25")

    def test_pi(self) -> None:
        self.assertEqual(hdc._extract_version("pi", "0.84.4"), "0.84.4")

    def test_copilot(self) -> None:
        self.assertEqual(
            hdc._extract_version("copilot", "GitHub Copilot CLI 1.0.80."),
            "1.0.80",
        )

    def test_agy(self) -> None:
        self.assertEqual(hdc._extract_version("agy", "1.1.22"), "1.1.22")

    def test_opencode_dev_build_is_not_truncated(self) -> None:
        """A dev build's version must be reported whole; truncating
        ``0.0.0-dev-202609250015`` to ``0.0.0`` misnamed the installed
        binary in the SessionStart drift note."""
        self.assertEqual(
            hdc._extract_version("opencode", "0.0.0-dev-202609250015"),
            "0.0.0-dev-202609250015",
        )

    def test_prerelease_kept_build_metadata_dropped(self) -> None:
        self.assertEqual(hdc._extract_version("opencode", "v1.19.0-rc.1"), "1.19.0-rc.1")
        self.assertEqual(hdc._extract_version("opencode", "1.18.32+build.5"), "1.18.32")

    def test_unparseable_returns_raw(self) -> None:
        self.assertEqual(hdc._extract_version("claude", "nightly"), "nightly")


class ResolveBinaryTestCase(unittest.TestCase):
    def test_shutil_which_found(self) -> None:
        with patch("shutil.which", return_value="/usr/bin/claude"):
            p = hdc.resolve_binary("claude")
        self.assertIsNotNone(p)
        assert p is not None
        self.assertEqual(p.name, "claude")

    def test_missing_returns_none(self) -> None:
        with patch("shutil.which", return_value=None):
            with patch.object(hdc, "_FALLBACK_PATHS", {"claude": []}):
                p = hdc.resolve_binary("claude")
        self.assertIsNone(p)

    def test_fallback_used(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_bin = Path(tmpdir) / "claude"
            fake_bin.write_text("#!/bin/sh\necho fake")
            fake_bin.chmod(0o755)
            with (
                patch("shutil.which", return_value=None),
                patch.object(
                    hdc,
                    "_FALLBACK_PATHS",
                    {"claude": [str(fake_bin)]},
                ),
            ):
                p = hdc.resolve_binary("claude")
            self.assertIsNotNone(p)
            assert p is not None
            self.assertTrue(p.exists())


class IntegrationSanityTestCase(unittest.TestCase):
    def test_parser_defaults_to_check(self) -> None:
        args = hdc.build_parser().parse_args([])
        self.assertIsNone(args.subcommand)

    def test_parser_probe_record(self) -> None:
        args = hdc.build_parser().parse_args(
            ["probe", "--harness", "pi", "--record", "--expect-identity", "/x:1:2"]
        )
        self.assertEqual(args.harness, "pi")
        self.assertTrue(args.record)
        self.assertEqual(args.expect_identity, "/x:1:2")

    def test_parser_check_strict(self) -> None:
        args = hdc.build_parser().parse_args(["check", "--strict"])
        self.assertTrue(args.strict)

    def test_verbosity_flags_present(self) -> None:
        for cmd in ("check", "probe"):
            args = hdc.build_parser().parse_args([cmd, "-q"])
            self.assertTrue(args.quiet)
            self.assertFalse(getattr(args, "verbose", True))


if __name__ == "__main__":
    test_bootstrap.run_unittest_main()
