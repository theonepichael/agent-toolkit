#!/usr/bin/env python3
"""Tests for harness_discovery_check.py. Run with: python3 test_harness_discovery_check.py

Covers the stateless ``check`` tier (version-pin comparison) and the
``probe`` tier (fixture-build + token extraction + retry logic).

No real harness binaries are invoked — the subprocess layer is fully
mocked. Tests that genuinely shell out carry
``@pytest.mark.allow_real_subprocess`` per ``test/AGENTS.md``.
"""

import io
import sys
import tempfile
import unittest
from collections.abc import Sequence
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent))
import harness_discovery_check as hdc

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _make_result(
    stdout: str = "",
    stderr: str = "",
    returncode: int = 0,
    exc: Exception | None = None,
) -> object:
    """Return a fake subprocess.CompletedProcess-like object."""

    class _Result:
        def __init__(self) -> None:
            self.stdout = stdout
            self.stderr = stderr
            self.returncode = returncode
            self._exc = exc

    return _Result()


def fake_run_factory(
    versions: dict[str, str] | None = None,
    probes: dict[str, str] | None = None,
    fail_version: Sequence[str] = (),
    fail_probe: Sequence[str] = (),
    timeout_version: Sequence[str] = (),
    timeout_probe: Sequence[str] = (),
) -> object:
    """Return a fake ``subprocess.run`` callable."""
    versions = versions or {}
    probes = probes or {}

    def fake_run(cmd: Sequence[str], **kwargs: object) -> object:
        binary = cmd[0]
        name = Path(binary).name
        # Version call
        if "--version" in cmd:
            if name in timeout_version:
                raise TimeoutError("timed out")
            if name in fail_version:
                return _make_result(stderr="error", returncode=1)
            return _make_result(stdout=versions.get(name, "0.0.0") + "\n")
        # Probe call
        if name in timeout_probe:
            raise TimeoutError("timed out")
        if name in fail_probe:
            return _make_result(stderr="auth failed", returncode=1)
        return _make_result(stdout=probes.get(name, ""))

    return fake_run


def fake_resolve_binary(name: str) -> Path | None:
    """Return a deterministic fake path so ``Path(binary).name`` yields the
    harness name itself."""
    return Path(f"/fake/bin/{name}")


class CheckTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._patches = [
            patch.object(hdc, "resolve_binary", fake_resolve_binary),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()

    def run_check(
        self,
        *,
        hook: bool = False,
        strict: bool = False,
        fake_run=None,
        quiet: bool = False,
    ) -> tuple[int, str]:
        out = io.StringIO()
        err = io.StringIO()
        with patch("sys.stdout", out), patch("sys.stderr", err):
            code = hdc.cmd_check(
                hook=hook, strict=strict, quiet=quiet, run_command=fake_run
            )
        return code, out.getvalue() + err.getvalue()

    def test_matching_versions_exit_clean(self) -> None:
        fake = fake_run_factory(
            versions={
                "claude": hdc.CLAUDE_CODE_PINNED_VERSION,
                "opencode": hdc.OPENCODE_PINNED_VERSION,
            }
        )
        code, output = self.run_check(fake_run=fake)
        self.assertEqual(code, 0)
        self.assertEqual(output.strip(), "")

    def test_mismatch_prints_note(self) -> None:
        fake = fake_run_factory(
            versions={
                "claude": "2.1.999",
                "opencode": hdc.OPENCODE_PINNED_VERSION,
            }
        )
        code, output = self.run_check(fake_run=fake)
        self.assertEqual(code, 0)
        self.assertIn("claude", output)
        self.assertIn("2.1.999", output)
        self.assertIn(hdc.CLAUDE_CODE_PINNED_VERSION, output)
        self.assertIn("probe --harness claude", output)

    def test_mismatch_strict_exits_2(self) -> None:
        fake = fake_run_factory(
            versions={
                "claude": "2.1.999",
                "opencode": hdc.OPENCODE_PINNED_VERSION,
            }
        )
        code, _ = self.run_check(fake_run=fake, strict=True)
        self.assertEqual(code, 2)

    def test_missing_binary_unverifiable_exit_0(self) -> None:
        fake = fake_run_factory(versions={})
        with patch.object(hdc, "resolve_binary", lambda _name: None):
            code, output = self.run_check(fake_run=fake)
        self.assertEqual(code, 0)
        self.assertEqual(output.strip(), "")

    def test_version_failure_is_error(self) -> None:
        fake = fake_run_factory(
            versions={"claude": "2.1.251"}, fail_version=("opencode",)
        )
        code, output = self.run_check(fake_run=fake)
        self.assertEqual(code, 1)
        self.assertIn("opencode", output)
        self.assertIn("ERROR", output.upper())

    def test_hook_mode_crash_note_on_error(self) -> None:
        fake = fake_run_factory(fail_version=("claude",))
        code, output = self.run_check(fake_run=fake, hook=True)
        self.assertEqual(code, 1)
        self.assertIn("checker failed", output)

    def test_hook_mode_empty_on_clean(self) -> None:
        fake = fake_run_factory(
            versions={
                "claude": hdc.CLAUDE_CODE_PINNED_VERSION,
                "opencode": hdc.OPENCODE_PINNED_VERSION,
            }
        )
        code, output = self.run_check(fake_run=fake, hook=True)
        self.assertEqual(code, 0)
        self.assertEqual(output.strip(), "")

    def test_both_mismatch_notes_printed(self) -> None:
        fake = fake_run_factory(versions={"claude": "2.1.999", "opencode": "1.19.0"})
        code, output = self.run_check(fake_run=fake)
        self.assertEqual(code, 0)
        self.assertIn("claude", output)
        self.assertIn("opencode", output)

    def test_verbosity_flags_present(self) -> None:
        for cmd in ("check", "probe"):
            args = hdc.build_parser().parse_args([cmd, "-q"])
            self.assertTrue(args.quiet)
            self.assertFalse(getattr(args, "verbose", True))


class ProbeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._patches = [
            patch.object(hdc, "resolve_binary", fake_resolve_binary),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()

    def run_probe(
        self,
        *,
        harness: str | None = None,
        fake_run=None,
        quiet: bool = False,
    ) -> tuple[int, str]:
        out = io.StringIO()
        with patch("sys.stdout", out):
            code = hdc.cmd_probe(harness=harness, quiet=quiet, run_command=fake_run)
        return code, out.getvalue()

    def test_all_hold(self) -> None:
        fake = fake_run_factory(
            probes={
                "claude": hdc._TOKEN_CLAUDE_ROOT,
                "opencode": hdc._TOKEN_AGENTS_ROOT,
                "pi": hdc._TOKEN_AGENTS_ROOT,
                "copilot": f"{hdc._TOKEN_CLAUDE_ROOT}, {hdc._TOKEN_GEMINI_ROOT}, {hdc._TOKEN_AGENTS_ROOT}",
                "agy": "none",
            }
        )
        code, output = self.run_probe(fake_run=fake)
        self.assertEqual(code, 0)
        self.assertIn("HOLD", output)
        self.assertNotIn("BROKEN", output)
        self.assertNotIn("ERROR", output)

    def test_broken_row(self) -> None:
        fake = fake_run_factory(
            probes={
                "claude": hdc._TOKEN_AGENTS_ROOT,  # wrong — should be CLAUDE only
            }
        )
        code, output = self.run_probe(harness="claude", fake_run=fake)
        self.assertEqual(code, 1)
        self.assertIn("BROKEN", output)
        self.assertIn("unexpected", output)
        self.assertIn("missing", output)
        self.assertIn("re-measure", output.lower())

    def test_error_on_timeout(self) -> None:
        fake = fake_run_factory(timeout_probe=("opencode",))
        code, output = self.run_probe(harness="opencode", fake_run=fake)
        self.assertEqual(code, 1)
        self.assertIn("ERROR", output)
        self.assertIn("timed out", output)

    def test_error_on_nonzero_exit(self) -> None:
        fake = fake_run_factory(fail_probe=("pi",))
        code, output = self.run_probe(harness="pi", fake_run=fake)
        self.assertEqual(code, 1)
        self.assertIn("ERROR", output)
        self.assertIn("auth failed", output)

    def test_retry_on_empty_response(self) -> None:
        """Empty responses should be retried before accepting ERROR."""
        call_count = 0

        def counting_run(cmd, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count < hdc._PROBE_ATTEMPTS:
                return _make_result(stdout="some prose without tokens")
            return _make_result(stdout=hdc._TOKEN_AGENTS_ROOT)

        code, output = self.run_probe(harness="opencode", fake_run=counting_run)
        self.assertEqual(code, 0)
        self.assertIn("HOLD", output)
        self.assertGreaterEqual(call_count, 2)

    def test_retry_then_error(self) -> None:
        """After _PROBE_ATTEMPTS empty/invalid responses, yield ERROR."""
        fake = fake_run_factory(probes={"claude": "just prose no tokens"})
        with patch.object(hdc, "_PROBE_ATTEMPTS", 2):
            code, output = self.run_probe(harness="claude", fake_run=fake)
        self.assertEqual(code, 1)
        self.assertIn("ERROR", output)

    def test_prose_wrapped_extraction(self) -> None:
        """Tokens buried in markdown/prose must still be extracted."""
        response = (
            "Sure! Here are the tokens I found:\n\n"
            f"- {hdc._TOKEN_CLAUDE_ROOT}\n"
            "- some other text\n"
        )
        fake = fake_run_factory(probes={"claude": response})
        code, output = self.run_probe(harness="claude", fake_run=fake)
        self.assertEqual(code, 0)
        self.assertIn("HOLD", output)

    def test_fixture_built(self) -> None:
        """The temp fixture repo must contain all six token files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir) / "fixture"
            repo.mkdir()
            hdc._build_fixture(repo, run_command=lambda cmd, **kw: _make_result())
            self.assertTrue((repo / "AGENTS.md").is_file())
            self.assertTrue((repo / "CLAUDE.md").is_file())
            self.assertTrue((repo / "GEMINI.md").is_file())
            self.assertTrue((repo / "sub" / "AGENTS.md").is_file())
            self.assertTrue((repo / "sub" / "CLAUDE.md").is_file())
            self.assertTrue((repo / "sub" / "GEMINI.md").is_file())
            self.assertIn(
                hdc._TOKEN_AGENTS_ROOT,
                (repo / "AGENTS.md").read_text(),
            )

    def test_single_harness_probe(self) -> None:
        fake = fake_run_factory(probes={"pi": hdc._TOKEN_AGENTS_ROOT})
        code, output = self.run_probe(harness="pi", fake_run=fake)
        self.assertEqual(code, 0)
        self.assertIn("pi", output)
        self.assertNotIn("claude", output)


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


class ParseTokensTestCase(unittest.TestCase):
    def test_exact(self) -> None:
        self.assertEqual(
            hdc._parse_tokens(hdc._TOKEN_CLAUDE_ROOT),
            {hdc._TOKEN_CLAUDE_ROOT},
        )

    def test_prose_wrapped(self) -> None:
        text = f"I see {hdc._TOKEN_AGENTS_ROOT} and {hdc._TOKEN_CLAUDE_ROOT}."
        self.assertEqual(
            hdc._parse_tokens(text),
            {hdc._TOKEN_AGENTS_ROOT, hdc._TOKEN_CLAUDE_ROOT},
        )

    def test_none(self) -> None:
        self.assertEqual(hdc._parse_tokens("none"), set())


class IntegrationSanityTestCase(unittest.TestCase):
    """Lightweight end-to-end tests that exercise the real parser and
    fixture builder without invoking real harness binaries."""

    def test_parser_defaults_to_check(self) -> None:
        args = hdc.build_parser().parse_args([])
        self.assertIsNone(args.subcommand)

    def test_parser_probe_harness(self) -> None:
        args = hdc.build_parser().parse_args(["probe", "--harness", "pi"])
        self.assertEqual(args.harness, "pi")

    def test_parser_check_strict(self) -> None:
        args = hdc.build_parser().parse_args(["check", "--strict"])
        self.assertTrue(args.strict)


if __name__ == "__main__":
    unittest.main()
