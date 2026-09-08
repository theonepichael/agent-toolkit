#!/usr/bin/env python3
"""Tests for settings_seed.py. Run with: python3 test_settings_seed.py

Deliberately dependency-free stdlib unittest, like its siblings in this
directory, so the tool stays testable on a machine that has never run
`uv sync`. Every path lives under a throwaway temp directory — nothing
here touches real user state or shells out to git for real.

Covers four layers:

* the extracted seed machinery (copy-once, drift reporting, reseed
  safeguards, adoption refusals) against a minimal fake Context,
  pinning the module's explicit installer-context contract;
* the run_command injection contract: the git-cleanliness safeguard is
  invoked through the callable install.py's entrypoints pass at call
  time, so the existing ``monkeypatch.setattr(install, "run_command",
  ...)`` stub seam keeps working with zero test edits;
* the isolation contract: settings_seed must import in a fresh
  interpreter with only its own directory on sys.path — no repo root,
  no install module (no circular or hidden dependency);
* the alias and palette contracts: every name install.py must keep
  re-exporting, and the single canonical Palette object shared by
  install.py and this module (state-mutated, never rebound).
"""

import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import pytest  # repo conftest guard marker only

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPO_ROOT))

import cli_common  # noqa: E402 — path insert above
import settings_seed as ss  # noqa: E402 — path insert above

# ── fake Context: the module's declared installer-context contract ──────────


class FakeOpts:
    def __init__(
        self,
        *,
        adopt: bool = False,
        dry_run: bool = False,
        reseed: bool = False,
        quiet: bool = False,
        profile: str = "personal",
    ) -> None:
        self.adopt = adopt
        self.dry_run = dry_run
        self.reseed = reseed
        self.quiet = quiet
        self.profile = profile


class FakeReporter:
    def __init__(self) -> None:
        self.skips: list[tuple[str, str]] = []

    def skip(self, label: str, reason: str) -> None:
        self.skips.append((label, reason))


class FakeManifest:
    def __init__(self) -> None:
        self.backups: dict[Path, Path] = {}
        self.copies: list[Path] = []

    def record_copy(self, dest: Path) -> None:
        self.copies.append(dest)

    def has_backup(self, dest: Path) -> bool:
        return dest in self.backups

    def record_backup(self, dest: Path, backup: Path) -> None:
        self.backups[dest] = backup


class FakeContext:
    """Duck-typed stand-in for install.Context's settings-seed surface."""

    def __init__(self, home: Path, dotfiles: Path, **opts: bool | str) -> None:
        self.home = home
        self.dotfiles = dotfiles
        self.opts = FakeOpts(**opts)  # type: ignore[arg-type]
        self.reporter = FakeReporter()
        self.manifest = FakeManifest()

    def display(self, path: Path) -> str:
        return str(path)

    def has_harness(self, name: str) -> bool:
        return name == "claude"


def recording_run_command():
    """A run_command double that records argv and answers like `git` would."""
    calls: list[object] = []

    def run(cmd: object, **_kwargs: bool) -> object:
        calls.append(cmd)
        argv = " ".join(str(a) for a in cmd)  # type: ignore[union-attr,operator]
        if "ls-files" in argv:
            return type("R", (), {"ok": True, "stdout": "seed.json\n"})()
        return type("R", (), {"ok": True, "stdout": ""})()  # status: clean

    run.calls = calls  # type: ignore[attr-defined]
    return run


# ── seed_file: copy-once semantics ───────────────────────────────────────────


class SeedFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir()
        self.seed = self.repo / "settings.json"
        self.seed.write_text('{"a": 1}\n', encoding="utf-8")
        self.dest = self.home / ".claude" / "settings.json"
        self.run = recording_run_command()

    def seed_file(self, ctx: FakeContext) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = ss.seed_file(
                ctx,
                self.seed,
                self.dest,
                skip_label="settings.json seed",
                drift=ss.describe_settings_drift,
                adopt_drift=ss._describe_settings_text,
                run_command=self.run,  # type: ignore[arg-type]
            )
        self.captured = out.getvalue()
        return result

    def test_first_run_copies_once_and_records_manifest(self) -> None:
        ctx = FakeContext(self.home, self.repo)
        self.assertEqual(self.seed_file(ctx), "")
        self.assertEqual(self.dest.read_text(encoding="utf-8"), '{"a": 1}\n')
        self.assertEqual(ctx.manifest.copies, [self.dest])
        self.assertIn("copied", self.captured)
        # The repo seed is untouched: copy-once, never overwritten.
        self.assertEqual(self.seed.read_text(encoding="utf-8"), '{"a": 1}\n')

    def test_second_run_reports_drift_without_overwriting(self) -> None:
        ctx = FakeContext(self.home, self.repo)
        self.seed_file(ctx)
        self.dest.write_text('{"a": 2}\n', encoding="utf-8")
        drift = self.seed_file(ctx)
        self.assertEqual(drift, "a")
        self.assertEqual(self.dest.read_text(encoding="utf-8"), '{"a": 2}\n')

    def test_dry_run_previews_and_copies_nothing(self) -> None:
        ctx = FakeContext(self.home, self.repo, dry_run=True)
        self.assertEqual(self.seed_file(ctx), "")
        self.assertFalse(self.dest.exists())
        self.assertIn("would copy", self.captured)

    def test_run_command_is_required(self) -> None:
        ctx = FakeContext(self.home, self.repo)
        with self.assertRaises(TypeError):
            ss.seed_file(  # type: ignore[call-arg]
                ctx,
                self.seed,
                self.dest,
                skip_label="settings.json seed",
                drift=ss.describe_settings_drift,
            )

    def test_reseed_backs_up_then_copies_seed(self) -> None:
        ctx = FakeContext(self.home, self.repo, reseed=True)
        self.seed_file(ctx)
        self.dest.write_text('{"a": 2}\n', encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = ss.seed_file(
                ctx,
                self.seed,
                self.dest,
                skip_label="settings.json seed",
                drift=ss.describe_settings_drift,
                run_command=self.run,  # type: ignore[arg-type]
            )
        self.assertEqual(result, "")
        self.assertEqual(self.dest.read_text(encoding="utf-8"), '{"a": 1}\n')
        backup = self.dest.with_name(self.dest.name + ".bak")
        self.assertEqual(backup.read_text(encoding="utf-8"), '{"a": 2}\n')
        self.assertEqual(ctx.manifest.backups, {self.dest: backup})

    def test_reseed_refuses_foreign_unrecorded_bak(self) -> None:
        ctx = FakeContext(self.home, self.repo, reseed=True)
        self.seed_file(ctx)
        self.dest.write_text('{"a": 2}\n', encoding="utf-8")
        # A .bak this tool never recorded: don't touch either file.
        foreign = self.dest.with_name(self.dest.name + ".bak")
        foreign.write_text("someone else's backup\n", encoding="utf-8")
        result = self.seed_file(ctx)
        self.assertEqual(result, "a")
        self.assertEqual(self.dest.read_text(encoding="utf-8"), '{"a": 2}\n')
        self.assertTrue(
            any("isn't a recorded backup" in reason for _, reason in ctx.reporter.skips)
        )


# ── adoption: refusal-to-weaken-safeguards paths ─────────────────────────────


class AdoptSeedTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir()
        self.seed = self.repo / "settings.json"
        self.seed.write_text('{"a": 1}\n', encoding="utf-8")
        self.dest = self.home / ".claude" / "settings.json"
        self.dest.parent.mkdir(parents=True)
        self.run = recording_run_command()

    def adopt(self, ctx: FakeContext) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = ss.seed_file(
                ctx,
                self.seed,
                self.dest,
                skip_label="settings.json seed",
                drift=ss.describe_settings_drift,
                adopt_drift=ss._describe_settings_text,
                run_command=self.run,  # type: ignore[arg-type]
            )
        self.captured = out.getvalue()
        return result

    def test_adopt_writes_normalized_live_text_into_clean_seed(self) -> None:
        ctx = FakeContext(self.home, self.repo, adopt=True)
        self.dest.write_text('{"a": 2}\r\n', encoding="utf-8")
        self.assertEqual(self.adopt(ctx), "")
        self.assertEqual(self.seed.read_text(encoding="utf-8"), '{"a": 2}\n')
        self.assertIn("adopted", self.captured)

    def test_adopt_refuses_symlinked_seed(self) -> None:
        ctx = FakeContext(self.home, self.repo, adopt=True)
        real = self.repo / "real-seed.json"
        real.write_text('{"a": 1}\n', encoding="utf-8")
        self.seed.unlink()
        self.seed.symlink_to(real)
        self.dest.write_text('{"a": 2}\n', encoding="utf-8")
        self.assertEqual(self.adopt(ctx), "content differs from the repo copy")
        self.assertTrue(any("symlink" in reason for _, reason in ctx.reporter.skips))

    def test_adopt_refuses_dirty_seed_via_injected_run_command(self) -> None:
        ctx = FakeContext(self.home, self.repo, adopt=True)
        self.dest.write_text('{"a": 2}\n', encoding="utf-8")

        def dirty_git(cmd: object, **_kwargs: bool) -> object:
            argv = " ".join(str(a) for a in cmd)  # type: ignore[union-attr,operator]
            if "status" in argv:
                return type("R", (), {"ok": True, "stdout": " M settings.json\n"})()
            return type("R", (), {"ok": True, "stdout": "settings.json\n"})()

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            result = ss.seed_file(
                ctx,
                self.seed,
                self.dest,
                skip_label="settings.json seed",
                drift=ss.describe_settings_drift,
                adopt_drift=ss._describe_settings_text,
                run_command=dirty_git,  # type: ignore[arg-type]
            )
        self.assertEqual(result, "a")
        # The live file must NOT have been adopted into the dirty seed.
        self.assertEqual(self.seed.read_text(encoding="utf-8"), '{"a": 1}\n')
        self.assertTrue(any("dirty" in reason for _, reason in ctx.reporter.skips))

    def test_opencode_adopt_blocker_refuses_new_bypass(self) -> None:
        seed = self.repo / "opencode.jsonc"
        seed.write_text('{"permission": {"bash": {"git log*": "allow"}}}\n')
        blocker = ss._opencode_adopt_blocker
        reason = blocker(  # type: ignore[arg-type]
            FakeContext(self.home, self.repo),
            seed,
            self.dest,
            seed.read_text(encoding="utf-8"),
            '{"permission": {"bash": {"git log*": "allow", "node -e *": "allow"}}}',
        )
        assert reason is not None
        self.assertIn("SECURITY:", reason)
        self.assertIn("node -e *", reason)

    def test_opencode_adopt_blocker_allows_no_bypass(self) -> None:
        seed = self.repo / "opencode.jsonc"
        seed.write_text('{"permission": {"bash": {"git log*": "allow"}}}\n')
        self.assertIsNone(
            ss._opencode_adopt_blocker(  # type: ignore[arg-type]
                FakeContext(self.home, self.repo),
                seed,
                self.dest,
                seed.read_text(encoding="utf-8"),
                '{"permission": {"bash": {"git log*": "allow"}}}',
            )
        )


# ── run_command seam: install entrypoints late-bind the stub ────────────────


def test_install_entrypoint_routes_stubbed_run_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """monkeypatched install.run_command must reach _adopt_git_reason."""
    import install  # noqa: PLC0415 — needs repo root on sys.path

    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        (home / ".claude").mkdir(parents=True)
        repo = Path(tmp) / "repo"
        (repo / "claude").mkdir(parents=True)
        (repo / "claude" / "settings.json").write_text('{"a": 1}\n')
        (home / ".claude" / "settings.json").write_text('{"a": 2}\n')

        calls: list[object] = []

        def stub(cmd: object, **_kwargs: bool) -> object:
            calls.append(cmd)
            argv = " ".join(str(a) for a in cmd)  # type: ignore[union-attr,operator]
            if "ls-files" in argv:
                return type("R", (), {"ok": True, "stdout": "claude/settings.json\n"})()
            return type("R", (), {"ok": True, "stdout": ""})()  # status: clean

        monkeypatch.setattr(install, "run_command", stub)
        ctx = FakeContext(home, repo, adopt=True)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            _name, drift = install.seed_claude_settings(ctx)  # type: ignore[arg-type]
        assert calls, "seed_claude_settings never consulted run_command"
        assert any("ls-files" in str(c) for c in calls)
        # The stub answers tracked-and-clean, so adoption proceeds.
        assert drift == ""
        assert (repo / "claude" / "settings.json").read_text(
            encoding="utf-8"
        ) == '{"a": 2}\n'


def test_install_entrypoint_copy_once_via_fake_ctx() -> None:
    import install  # noqa: PLC0415 — needs repo root on sys.path

    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        (home / ".claude").mkdir(parents=True)
        repo = Path(tmp) / "repo"
        (repo / "claude").mkdir(parents=True)
        (repo / "claude" / "settings.json").write_text('{"a": 1}\n')
        ctx = FakeContext(home, repo)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            name, drift = install.seed_claude_settings(ctx)  # type: ignore[arg-type]
        assert (name, drift) == ("settings.json", "")
        assert ctx.manifest.copies == [home / ".claude" / "settings.json"]


# ── palette: one canonical object, mutated not rebound ──────────────────────


class PaletteParityTests(unittest.TestCase):
    def test_install_and_module_share_one_palette(self) -> None:
        import install  # noqa: PLC0415 — needs repo root on sys.path

        self.assertIs(install.PALETTE, cli_common.PALETTE)

    def test_state_mutation_reaches_both(self) -> None:
        import install  # noqa: PLC0415 — needs repo root on sys.path

        original = cli_common.PALETTE.enabled
        try:
            cli_common.PALETTE.enabled = True
            self.assertTrue(install.PALETTE.enabled)
            self.assertIn("\x1b[", cli_common.PALETTE.ok("x"))
        finally:
            cli_common.PALETTE.enabled = original

    def test_default_is_disabled_plain_text(self) -> None:
        self.assertFalse(cli_common.PALETTE.enabled)
        self.assertEqual(cli_common.PALETTE.ok("x"), "x")

    def test_preview_uses_canonical_palette(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli_common.preview("would do x", quiet=False)
        self.assertIn("[dry-run] would do x", out.getvalue())


# ── drift helpers: module-level re-exports behave identically ───────────────


class DriftHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def test_describe_settings_drift_reports_key_diff(self) -> None:
        seed = self.base / "seed.json"
        live = self.base / "live.json"
        seed.write_text('{"a": 1, "b": 2}')
        live.write_text('{"a": 1, "b": 3}')
        self.assertEqual(ss.describe_settings_drift(seed, live), "b")

    def test_text_equal_jsonc_is_not_drift(self) -> None:
        seed = self.base / "seed.jsonc"
        live = self.base / "live.jsonc"
        text = '{"permission": {"bash": {}}} // comment\n'
        seed.write_text(text)
        live.write_text(text)
        self.assertEqual(ss.describe_opencode_drift(seed, live), "")

    def test_vscode_bindings_count_diff(self) -> None:
        seed = self.base / "keybindings.json"
        live = self.base / "live.json"
        seed.write_text("[]")
        live.write_text('[{"key": "ctrl+s"}]')
        self.assertIn("1 bindings live vs 0", ss.describe_vscode_drift(seed, live))

    def test_missing_files_are_silent(self) -> None:
        self.assertEqual(
            ss.describe_settings_drift(self.base / "a", self.base / "b"), ""
        )

    def test_json_key_drift_sorted_union(self) -> None:
        self.assertEqual(ss.json_key_drift({"a": 1, "b": 2}, {"a": 1, "b": 3}), ["b"])

    def test_normalize_seed_text(self) -> None:
        self.assertEqual(ss._normalize_seed_text("a\r\nb\r\n"), "a\nb\n")


# ── isolation and alias contracts ───────────────────────────────────────────


class IsolationTests(unittest.TestCase):
    """settings_seed must import with only its own directory on sys.path."""

    @pytest.mark.allow_real_subprocess  # spawns one `python -c` child that
    # only imports settings_seed and prints a constant — no network, no real
    # ~/.claude access; a fresh-interpreter import is the isolation property
    # under test, which an in-process import cannot prove.
    def test_fresh_interpreter_import_without_repo_root(self) -> None:
        code = (
            f"import sys; sys.path = [p for p in sys.path if p != {str(REPO_ROOT)!r}]; "
            f"sys.path.insert(0, {str(HERE)!r}); import settings_seed; "
            "print(settings_seed.json_key_drift({'a': 1}, {'a': 2})[0])"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=False,
            cwd=HERE,  # keep cwd-resolved sys.path entries inside agent-scripts
        )
        self.assertEqual(
            proc.returncode,
            0,
            f"isolation import failed:\n{proc.stdout}\n{proc.stderr}",
        )
        self.assertEqual(proc.stdout.strip(), "a")


class InstallAliasTests(unittest.TestCase):
    """Every name install.py must keep re-exporting after the extraction."""

    ALIASES = (
        "json_key_drift",
        "_BYPASS_BASH_PATTERNS",
        "opencode_bypass_drift",
        "_bash_permissions",
        "_load_json_pair_text",
        "_describe_settings_text",
        "_describe_opencode_text",
        "_describe_vscode_text",
        "describe_settings_drift",
        "describe_opencode_drift",
        "describe_vscode_drift",
        "_load_json_pair",
        "seed_file",
        "_adopt_seed",
        "_normalize_seed_text",
        "_adopt_git_reason",
        "_adopt_file",
        "_opencode_adopt_blocker",
        "_reseed_file",
    )

    def test_install_module_reexports_every_moved_name(self) -> None:
        import install  # noqa: PLC0415 — needs repo root on sys.path

        missing = [name for name in self.ALIASES if not hasattr(install, name)]
        self.assertEqual(missing, [])

    def test_alias_bodies_identical_to_module_functions(self) -> None:
        import install  # noqa: PLC0415 — needs repo root on sys.path

        for name in self.ALIASES:
            self.assertIs(getattr(install, name), getattr(ss, name), name)

    def test_moved_names_absent_from_install_source(self) -> None:
        """The definitions themselves must live in the module, not install.py."""
        source = (REPO_ROOT / "install.py").read_text(encoding="utf-8")
        for name in self.ALIASES:
            self.assertNotIn(f"def {name}(", source, name)


if __name__ == "__main__":
    unittest.main()
