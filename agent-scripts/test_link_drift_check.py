#!/usr/bin/env python3
"""Tests for link_drift_check.py. Run with: python3 test_link_drift_check.py

Deliberately dependency-free stdlib unittest, like its siblings in this
directory, so the tool stays testable on a machine that has never run
`uv sync`. The hook audits real fixture state (a temp repo + temp home with
real symlinks) through the direct link_inspect import -- the audit logic
itself is exercised, never faked; only the machine's identity (paths,
platform) is injected.
"""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import link_drift_check as ldc
import link_inspect as li

# The hook reads these repo files to fingerprint the audit; a fixture repo
# therefore carries empty stand-ins for them so cache hits can happen.
_FINGERPRINT_STUBS = (
    "install.py",
    "agent-scripts/link_inspect.py",
    "agent-scripts/link_drift_check.py",
)


class Fixture:
    """A temp repo + temp home with real links and a temp manifest."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.repo = root / "repo"
        self.home = root / "home"
        self.repo.mkdir(parents=True)
        self.home.mkdir(parents=True)
        for rel in _FINGERPRINT_STUBS:
            path = self.repo / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("")
        self.manifest = self.repo_home_state() / "history.jsonl"

    def repo_home_state(self) -> Path:
        return self.home / ".local" / "state" / "agent-toolkit"

    def write_links(self, text: str) -> None:
        (self.repo / "links.toml").write_text(text)

    def write_manifest(self, entries: list[dict[str, object]]) -> None:
        self.manifest.parent.mkdir(parents=True, exist_ok=True)
        self.manifest.write_text("".join(json.dumps(entry) + "\n" for entry in entries))

    def source(self, rel: str, content: str = "x\n") -> Path:
        path = self.repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def link(self, src_rel: str, dest: str) -> Path:
        """Create a live symlink dest -> repo file, expanding ~ against home."""
        target = self.repo / src_rel
        path = self.expand(dest)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(target)
        return path

    def link_to(self, dest: str, target: Path) -> Path:
        path = self.expand(dest)
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_symlink() or path.exists():
            path.unlink()
        path.symlink_to(target)
        return path

    def expand(self, dest: str) -> Path:
        if dest == "~":
            return self.home
        if dest.startswith("~/"):
            return self.home / dest[2:]
        return Path(dest)

    def check(self, **kwargs: object) -> str:
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            ldc.cmd_check(
                repo_root=self.repo,
                home=self.home,
                machine=(False, True, False),
                **kwargs,
            )
        return buffer.getvalue()

    def pointer(self) -> str:
        return f"`python3 {self.repo}/install.py --check-links`"


BASE_LINKS = """\
[[link]]
src = "claude/global-instructions.md"
dest = "~/.claude/global-instructions.md"
"""


def counting_audit() -> tuple[object, list[int]]:
    """Patch link_inspect.collect_link_findings with a wrapper that records
    how many times the real audit ran (the fixture state stays live
    underneath) — the seam the hook actually consumes."""

    real = li.collect_link_findings
    calls: list[int] = []

    def wrapper(*args: object, **kwargs: object) -> object:
        calls.append(1)
        return real(*args, **kwargs)  # type: ignore[arg-type]

    return wrapper, calls


class CheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="test-link-drift-check-"))
        self._env_patch = patch.dict(os.environ, {"XDG_CACHE_HOME": str(self.tmpdir)})
        self._env_patch.start()

    def tearDown(self) -> None:
        self._env_patch.stop()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def fixture(self) -> Fixture:
        fx = Fixture(self.tmpdir / "fx")
        fx.write_links(BASE_LINKS)
        fx.source("claude/global-instructions.md")
        fx.link("claude/global-instructions.md", "~/.claude/global-instructions.md")
        return fx

    def test_clean_machine_prints_nothing(self) -> None:
        self.assertEqual(self.fixture().check(), "")

    def test_drift_names_the_bucket_and_the_full_audit_command(self) -> None:
        fx = self.fixture()
        # The failure this hook exists for: a live link left pointing into a
        # worktree checkout of the same file after a hand-repoint for live
        # verification -- .git as a *file* is what marks a worktree, so this
        # is real drift, not the excusable foreign-main-checkout note.
        other = fx.root / "repo-wt"
        (other / "claude").mkdir(parents=True)
        (other / "claude" / "global-instructions.md").write_text("wt\n")
        (other / "links.toml").write_text("")
        (other / "install.py").write_text("")
        (other / ".git").write_text("gitdir: somewhere\n")
        fx.link_to(
            "~/.claude/global-instructions.md",
            other / "claude" / "global-instructions.md",
        )
        out = fx.check()
        self.assertEqual(
            out,
            f"links: wrong-target (1) — run {fx.pointer()}\n",
        )

    def test_several_buckets_are_all_named_in_canonical_order(self) -> None:
        fx = self.fixture()
        fx.link_to(
            "~/.claude/global-instructions.md", fx.repo / "claude" / "elsewhere.md"
        )
        # A second row whose repo source is gone: broken-source.
        fx.source("claude/gone.md")
        fx.link("claude/gone.md", "~/.claude/gone.md")
        fx.write_links(
            BASE_LINKS
            + """
[[link]]
src = "claude/gone.md"
dest = "~/.claude/gone.md"
"""
        )
        (fx.repo / "claude" / "gone.md").unlink()
        out = fx.check()
        self.assertEqual(
            out,
            f"links: broken-source (1); wrong-target (1) — run {fx.pointer()}\n",
        )

    def test_never_installed_bucket_is_reported(self) -> None:
        fx = self.fixture()
        # A declared link whose source exists but destination was never installed:
        fx.source("claude/new.md")
        fx.write_links(
            BASE_LINKS
            + """
[[link]]
src = "claude/new.md"
dest = "~/.claude/new.md"
"""
        )
        out = fx.check()
        self.assertEqual(
            out,
            f"links: never-installed (1) — run {fx.pointer()}\n",
        )

    def test_quiet_suppresses_the_note(self) -> None:
        fx = self.fixture()
        fx.link_to(
            "~/.claude/global-instructions.md", fx.repo / "claude" / "elsewhere.md"
        )
        self.assertEqual(fx.check(quiet=True), "")

    def test_malformed_links_toml_stays_silent(self) -> None:
        """A broken checker must not itself become a session-start warning."""
        fx = Fixture(self.tmpdir / "fx")
        fx.write_links("[[link]]\nsrc = \n")
        self.assertEqual(fx.check(), "")

    def test_missing_links_toml_stays_silent(self) -> None:
        fx = Fixture(self.tmpdir / "fx")
        self.assertEqual(fx.check(), "")

    def test_platform_gated_row_is_skipped(self) -> None:
        fx = Fixture(self.tmpdir / "fx")
        fx.write_links(
            """\
[[link]]
src = "claude/global-instructions.md"
dest = "~/.claude/global-instructions.md"
platform = "mac"
"""
        )
        fx.source("claude/global-instructions.md")
        fx.link_to(
            "~/.claude/global-instructions.md", fx.repo / "claude" / "elsewhere.md"
        )
        self.assertEqual(fx.check(), "")

    def test_profile_excluded_row_is_skipped(self) -> None:
        fx = Fixture(self.tmpdir / "fx")
        fx.write_links(
            """\
[[link]]
src = "claude/global-instructions.md"
dest = "~/.claude/global-instructions.md"
profile_exclude = ["personal"]
"""
        )
        fx.source("claude/global-instructions.md")
        fx.link_to(
            "~/.claude/global-instructions.md", fx.repo / "claude" / "elsewhere.md"
        )
        self.assertEqual(fx.check(), "")

    def test_orphaned_manifest_link_is_reported(self) -> None:
        fx = self.fixture()
        stale = fx.expand("~/.claude/stale-link.py")
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.symlink_to(fx.repo / "claude" / "global-instructions.md")
        fx.write_manifest(
            [
                {
                    "kind": "symlink-created",
                    "dest": str(stale),
                    "src": str(fx.repo / "claude" / "global-instructions.md"),
                }
            ]
        )
        out = fx.check()
        self.assertEqual(
            out,
            f"links: orphaned (1) — run {fx.pointer()}\n",
        )

    def test_missing_manifest_reads_as_empty_history(self) -> None:
        """No install has ever run: no orphan findings, no error."""
        fx = self.fixture()
        self.assertEqual(fx.check(), "")


class CacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="test-link-drift-cache-"))
        self.cache_dir = self.tmpdir / "cache"
        self.cache_file = (
            self.cache_dir / "agent-toolkit" / "link-drift-check-cache.json"
        )
        self._env_patch = patch.dict(
            os.environ, {"XDG_CACHE_HOME": str(self.cache_dir)}
        )
        self._env_patch.start()
        self.fx = Fixture(self.tmpdir / "fx")
        self.fx.write_links(BASE_LINKS)
        self.fx.source("claude/global-instructions.md")
        self.fx.link(
            "claude/global-instructions.md", "~/.claude/global-instructions.md"
        )

    def tearDown(self) -> None:
        self._env_patch.stop()
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_cache_roundtrip_avoids_second_audit(self) -> None:
        wrapper, calls = counting_audit()
        with patch.object(ldc.link_inspect, "collect_link_findings", wrapper):
            self.assertEqual(self.fx.check(), "")
            self.assertEqual(self.fx.check(), "")
        self.assertEqual(len(calls), 1)

    def test_cached_drift_replayed_byte_identically(self) -> None:
        self.fx.link_to(
            "~/.claude/global-instructions.md",
            self.fx.repo / "claude" / "elsewhere.md",
        )
        wrapper, calls = counting_audit()
        with patch.object(ldc.link_inspect, "collect_link_findings", wrapper):
            fresh = self.fx.check()
            replayed = self.fx.check()
        self.assertEqual(len(calls), 1)
        self.assertEqual(replayed, fresh)
        self.assertIn("wrong-target (1)", replayed)

    def test_corrupted_cache_falls_back_gracefully(self) -> None:
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        self.cache_file.write_text("not json!!!")
        self.assertEqual(self.fx.check(), "")

    def test_pre_schema2_cache_is_treated_as_a_miss(self) -> None:
        """A cache written by the old stdout-parsing hook must never be
        misread as findings data."""
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        old_shape = {
            "audit": {
                "fingerprint": "x",
                "returncode": 1,
                "stdout": "wrong-target (1):\n",
            }
        }
        self.cache_file.write_text(json.dumps(old_shape))
        self.assertEqual(self.fx.check(), "")
        # Recomputed and rewritten in the new shape.
        data = json.loads(self.cache_file.read_text())
        self.assertEqual(data["schema"], 2)


class ParserTests(unittest.TestCase):
    def test_check_is_the_default_subcommand(self) -> None:
        args = ldc.build_parser().parse_args([])
        self.assertIsNone(args.subcommand)

    def test_check_accepts_verbosity_flags(self) -> None:
        args = ldc.build_parser().parse_args(["check", "--quiet"])
        self.assertTrue(args.quiet)


if __name__ == "__main__":
    unittest.main()
