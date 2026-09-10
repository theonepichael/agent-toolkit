#!/usr/bin/env python3
"""Tests for link_inspect.py. Run with: python3 test_link_inspect.py

Deliberately dependency-free stdlib unittest, like its siblings in this
directory, so the tool stays testable on a machine that has never run
`uv sync`. Every path lives under a throwaway temp directory — nothing
here touches real user state or shells out.

Covers three layers:

* the extracted pure classifiers and drift-finding functions, against the
  narrowed (Context-free) signatures;
* the isolation contract: link_inspect must import in a fresh interpreter
  with only its own directory on sys.path — no repo root, no install
  module (no circular or hidden dependency);
* the alias contract: every name install.py must keep re-exporting so
  existing tests and callers (``install._implied_repo_root`` etc.)
  resolve unchanged.
"""

import shutil
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

import link_inspect as li  # noqa: E402 — path insert above


def format_path(path: Path) -> str:
    """Test stand-in for Context.display: absolute, no ~-shortening."""
    return str(path)


class ClassifierTests(unittest.TestCase):
    """The pure path-classification helpers, on tempdir fixtures."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)

    def test_is_symlink_and_path_exists_tolerate_unreadable(self) -> None:
        missing = self.home / "missing"
        self.assertFalse(li.is_symlink(missing))
        self.assertFalse(li.path_exists(missing))
        real = self.home / "real"
        real.write_text("x")
        self.assertTrue(li.path_exists(real))
        self.assertFalse(li.is_symlink(real))

    def test_link_target_resolves_relative_against_link_dir(self) -> None:
        dest = self.home / "link"
        dest.symlink_to("relative-target")
        self.assertEqual(li.link_target(dest), self.home / "relative-target")
        absolute = self.home / "abs"
        absolute.symlink_to(self.home / "real")
        self.assertEqual(li.link_target(absolute), self.home / "real")

    def test_same_path_falls_back_to_resolve(self) -> None:
        a = self.home / "a"
        self.assertTrue(li.same_path(a, self.home / "a"))
        self.assertFalse(li.same_path(a, self.home / "b"))

    def test_implied_repo_root_requires_full_tail_match(self) -> None:
        self.assertEqual(
            li.implied_repo_root(Path("/a/b/zsh/.zshrc"), "zsh/.zshrc"),
            Path("/a/b"),
        )
        self.assertIsNone(li.implied_repo_root(Path("/a/b/other/.zshrc"), "zsh/.zshrc"))
        self.assertEqual(
            li.implied_repo_root(Path("/zsh/.zshrc"), "zsh/.zshrc"), Path("/")
        )
        self.assertIsNone(li.implied_repo_root(Path("/zsh"), "zsh/.zshrc"))

    def test_checkout_shape_checks(self) -> None:
        repo = self.home / "repo"
        repo.mkdir()
        self.assertFalse(li.is_repo_checkout(repo))
        (repo / "links.toml").write_text("")
        (repo / "install.py").write_text("")
        self.assertTrue(li.is_repo_checkout(repo))
        self.assertFalse(li.is_main_checkout(repo))  # .git absent: not primary
        (repo / ".git").mkdir()
        self.assertTrue(li.is_main_checkout(repo))


class ExpandDestTests(unittest.TestCase):
    def test_expands_against_explicit_home_only(self) -> None:
        home = Path("/sandbox/home")
        self.assertEqual(li.expand_dest("~", home), home)
        self.assertEqual(li.expand_dest("~/.claude/x", home), home / ".claude" / "x")
        self.assertEqual(li.expand_dest("/abs/path", home), Path("/abs/path"))


class CheckApplicableLinksTests(unittest.TestCase):
    """Drift-finding buckets on the narrowed signature."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        (self.repo / "claude").mkdir(parents=True)
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()

    def link(self, src_rel: str, target: str) -> tuple[Path, Path, str, bool]:
        src = self.repo / src_rel
        src.write_text("content")
        dest = self.home / ".claude" / Path(src_rel).name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.symlink_to(target)
        return (src, dest, src_rel, True)

    def test_healthy_link_is_silent(self) -> None:
        triple = self.link("claude/x", str(self.repo / "claude" / "x"))
        findings, foreign = li.check_applicable_links(
            [triple], repo_root=self.repo, format_path=format_path
        )
        self.assertEqual(foreign, {})
        self.assertEqual({k: v for k, v in findings.items() if v}, {})

    def test_wrong_target_and_dangling_source(self) -> None:
        wrong = self.link("claude/wrong", str(self.home / "elsewhere"))
        # A source deleted from the repo: the link still points at it.
        dangling = self.link("claude/gone", str(self.repo / "claude" / "gone"))
        (self.repo / "claude" / "gone").unlink()
        findings, _ = li.check_applicable_links(
            [wrong, dangling], repo_root=self.repo, format_path=format_path
        )
        self.assertEqual(len(findings[li.CHECK_BUCKET_WRONG_TARGET]), 1)
        self.assertEqual(len(findings[li.CHECK_BUCKET_BROKEN_SOURCE]), 1)

    def test_not_a_symlink_bucket(self) -> None:
        src = self.repo / "claude" / "realfile"
        src.write_text("content")
        dest = self.home / ".claude" / "realfile"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("a real file")
        findings, _ = li.check_applicable_links(
            [(src, dest, "claude/realfile", True)],
            repo_root=self.repo,
            format_path=format_path,
        )
        self.assertEqual(len(findings[li.CHECK_BUCKET_NOT_A_SYMLINK]), 1)

    def test_inapplicable_rows_are_skipped(self) -> None:
        triple = self.link("claude/x", str(self.home / "nowhere"))
        triple = (triple[0], triple[1], triple[2], False)
        findings, _ = li.check_applicable_links(
            [triple], repo_root=self.repo, format_path=format_path
        )
        self.assertEqual({k: v for k, v in findings.items() if v}, {})


class FindOrphanedLinksTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir()
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()

    def test_manifest_recorded_dest_not_in_links_is_orphan(self) -> None:
        stale = self.home / "stale"
        stale.symlink_to(self.repo / "old")
        entries = [
            {
                "kind": "symlink-created",
                "dest": str(stale),
                "src": str(self.repo / "old"),
            }
        ]
        orphans = li.find_orphaned_links([], manifest_entries=entries)
        self.assertEqual(orphans, [stale])

    def test_live_repointed_dest_is_claimed_not_orphaned(self) -> None:
        claimed = self.home / "claimed"
        claimed.symlink_to(self.home / "other-tool")
        entries = [
            {
                "kind": "symlink-created",
                "dest": str(claimed),
                "src": str(self.repo / "mine"),
            }
        ]
        self.assertEqual(li.find_orphaned_links([], manifest_entries=entries), [])

    def test_gone_dest_needs_no_report(self) -> None:
        entries = [
            {
                "kind": "symlink-created",
                "dest": str(self.home / "vanished"),
                "src": str(self.repo / "old"),
            }
        ]
        self.assertEqual(li.find_orphaned_links([], manifest_entries=entries), [])


class CheckUnmanagedFilesTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.repo = Path(self._tmp.name) / "repo"
        self.repo.mkdir()
        self.home = Path(self._tmp.name) / "home"
        self.managed = self.home / ".claude" / "scripts"
        self.managed.mkdir(parents=True)

    def test_foreign_file_reported_managed_and_hidden_skipped(self) -> None:
        (self.managed / "foreign.py").write_text("x")
        (self.managed / ".hidden").write_text("x")
        (self.managed / "keep.py~").write_text("x")
        linked = self.managed / "linked.py"
        linked.symlink_to(self.repo / "linked.py")
        dir_spec = li.ManagedDirSpec(dest="~/.claude/scripts")
        findings: dict[str, list[str]] = {b: [] for b in li.CHECK_BUCKETS}
        audited = li.check_unmanaged_files(
            [dir_spec],
            links=[(self.repo / "linked.py", linked, "linked.py", True)],
            home=self.home,
            format_path=format_path,
            dir_applies=lambda _spec: True,
            findings=findings,
        )
        self.assertEqual(audited, 1)
        self.assertEqual(len(findings[li.CHECK_BUCKET_UNMANAGED]), 1)
        self.assertIn("foreign.py", findings[li.CHECK_BUCKET_UNMANAGED][0])

    def test_out_of_scope_directory_not_audited(self) -> None:
        dir_spec = li.ManagedDirSpec(dest="~/.claude/scripts")
        audited = li.check_unmanaged_files(
            [dir_spec],
            links=[],
            home=self.home,
            format_path=format_path,
            dir_applies=lambda _spec: False,
            findings={b: [] for b in li.CHECK_BUCKETS},
        )
        self.assertEqual(audited, 0)


class IsolationTests(unittest.TestCase):
    """link_inspect must import with only its own directory on sys.path."""

    @pytest.mark.allow_real_subprocess  # spawns one `python -c` child that
    # only imports link_inspect and prints a constant — no network, no real
    # ~/.claude access; a fresh-interpreter import is the isolation property
    # under test, which an in-process import cannot prove.
    def test_fresh_interpreter_import_without_repo_root(self) -> None:
        code = (
            f"import sys; sys.path = [p for p in sys.path if p != {str(REPO_ROOT)!r}]; "
            f"sys.path.insert(0, {str(HERE)!r}); import link_inspect; "
            "print(link_inspect.CHECK_BUCKETS[0])"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            check=False,
            cwd=HERE,  # keep cwd-resolved \'\' sys.path entries inside agent-scripts
        )
        self.assertEqual(
            proc.returncode,
            0,
            f"isolation import failed:\n{proc.stdout}\n{proc.stderr}",
        )
        self.assertEqual(proc.stdout.strip(), li.CHECK_BUCKET_BROKEN_SOURCE)


class LoadLinksTests(unittest.TestCase):
    """The moved TOML parsers (verbatim from install.py) keep their validation."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="test-link-inspect-load-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def write(self, text: str) -> Path:
        path = self.tmpdir / "links.toml"
        path.write_text(text)
        return path

    def test_load_links_parses_rows_in_file_order(self) -> None:
        path = self.write(
            '[[link]]\nsrc = "a/b"\ndest = "~/.a-b"\n\n'
            '[[link]]\nsrc = "c/d"\ndest = "~/.c-d"\nharness = "pi"\ndir = true\n'
        )
        specs = li.load_links(path)
        self.assertEqual([s.src for s in specs], ["a/b", "c/d"])
        self.assertEqual(specs[1].harness, "pi")
        self.assertTrue(specs[1].dir)

    def test_load_links_rejects_unknown_keys_and_bad_values(self) -> None:
        for bad in (
            '[[link]]\nsrc = "a"\ndest = "b"\nharnes = "claude"\n',
            '[[link]]\nsrc = "a"\ndest = "~/.a"\nharness = "nope"\n',
            '[[link]]\nsrc = "a"\ndest = "~/.a"\nplatform = "beos"\n',
            '[[link]]\nsrc = "a"\ndest = "~/.a"\nprofile_exclude = ["side"]\n',
            "[[link]]\nsrc = \n",
        ):
            with self.assertRaises((ValueError, TypeError)):
                li.load_links(self.write(bad))

    def test_load_managed_dirs_parse_and_reject(self) -> None:
        path = self.write('[[managed_dir]]\ndest = "~/.managed"\nignore = ["*.tmp"]\n')
        specs = li.load_managed_dirs(path)
        self.assertEqual(specs[0].dest, "~/.managed")
        self.assertEqual(specs[0].ignore, ("*.tmp",))
        with self.assertRaises((ValueError, TypeError)):
            li.load_managed_dirs(self.write("[[managed_dir]]\nnope = 1\n"))


class ManifestAndScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="test-link-inspect-scope-"))

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_manifest_path_is_the_shared_state_constant(self) -> None:
        home = self.tmpdir / "home"
        self.assertEqual(
            li.manifest_path(home),
            home / ".local" / "state" / "agent-toolkit" / "history.jsonl",
        )

    def test_read_manifest_entries_missing_file_and_bad_lines(self) -> None:
        self.assertEqual(li.read_manifest_entries(self.tmpdir / "nope.jsonl"), [])
        path = self.tmpdir / "history.jsonl"
        path.write_text(
            '{"kind": "symlink-created", "dest": "/x"}\n'
            "garbage line\n\n"
            '{"kind": "file-copied"}\n'
        )
        entries = li.read_manifest_entries(path)
        self.assertEqual(
            [e["kind"] for e in entries], ["symlink-created", "file-copied"]
        )

    def test_link_applies_gates_on_each_field(self) -> None:
        spec = li.LinkSpec(src="a", dest="~/.a", harness="pi", platform="mac")
        kwargs = dict(
            harnesses=("claude",),
            is_mac=False,
            is_linux=True,
            is_wsl=False,
            profile="personal",
        )
        self.assertFalse(li.link_applies(spec, **kwargs))  # harness gate
        self.assertFalse(
            li.link_applies(
                spec,
                harnesses=("pi",),
                **{k: v for k, v in kwargs.items() if k != "harnesses"},
            )
        )  # platform gate
        plain = li.LinkSpec(src="a", dest="~/.a")
        self.assertTrue(li.link_applies(plain, **kwargs))

    def test_format_path_shortens_home(self) -> None:
        home = self.tmpdir / "home"
        self.assertEqual(li.format_path(home / ".claude" / "x", home), "~/.claude/x")
        self.assertEqual(li.format_path(Path("/etc/hosts"), home), "/etc/hosts")


class AuditLinksTests(unittest.TestCase):
    """The consolidated entry point: assembly, scoping, and findings in one."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp(prefix="test-link-inspect-audit-"))
        self.repo = self.tmpdir / "repo"
        self.home = self.tmpdir / "home"
        self.repo.mkdir()
        self.home.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_audit_links_reports_wrong_target_as_data(self) -> None:
        (self.repo / "claude").mkdir()
        (self.repo / "claude" / "g.md").write_text("x\n")
        (self.repo / "links.toml").write_text(
            '[[link]]\nsrc = "claude/g.md"\ndest = "~/.claude/g.md"\n'
        )
        (self.home / ".claude").mkdir()
        (self.home / ".claude" / "g.md").symlink_to(
            self.repo / "claude" / "elsewhere.md"
        )
        findings, foreign, dirs_audited = li.audit_links(
            repo_root=self.repo,
            home=self.home,
            harnesses=li.VALID_HARNESSES,
            is_mac=False,
            is_linux=True,
            is_wsl=False,
            profile=li.DEFAULT_PROFILE,
            manifest_file=li.manifest_path(self.home),
            format_path=lambda path: li.format_path(path, self.home),
        )
        self.assertEqual(len(findings[li.CHECK_BUCKET_WRONG_TARGET]), 1)
        self.assertEqual(foreign, {})
        self.assertEqual(dirs_audited, 0)

    def test_audit_links_pre_parsed_specs_skip_second_parse(self) -> None:
        (self.repo / "claude").mkdir()
        (self.repo / "claude" / "g.md").write_text("x\n")
        (self.repo / "links.toml").write_text(
            '[[link]]\nsrc = "claude/g.md"\ndest = "~/.claude/g.md"\n'
        )
        (self.home / ".claude").mkdir()
        (self.home / ".claude" / "g.md").symlink_to(self.repo / "claude" / "g.md")
        specs = li.load_links(self.repo / "links.toml")
        findings, _foreign, _dirs = li.audit_links(
            repo_root=self.repo,
            home=self.home,
            harnesses=li.VALID_HARNESSES,
            is_mac=False,
            is_linux=True,
            is_wsl=False,
            profile=li.DEFAULT_PROFILE,
            manifest_file=li.manifest_path(self.home),
            format_path=lambda path: li.format_path(path, self.home),
            specs=specs,
            managed_dirs=[],
        )
        self.assertEqual(findings, {bucket: [] for bucket in li.CHECK_BUCKETS})


class InstallAliasTests(unittest.TestCase):
    """Every name install.py must keep re-exporting after the extraction."""

    ALIASES = (
        "JUNK_SUFFIXES",
        "CHECK_BUCKET_BROKEN_SOURCE",
        "CHECK_BUCKET_WRONG_TARGET",
        "CHECK_BUCKET_NOT_A_SYMLINK",
        "CHECK_BUCKET_ORPHANED",
        "CHECK_BUCKET_UNMANAGED",
        "CHECK_BUCKET_NEVER_INSTALLED",
        "CHECK_BUCKETS",
        "LinkSpec",
        "ManagedDirSpec",
        "expand_dest",
        "_JUNK_SUFFIXES",
        "_is_symlink",
        "_path_exists",
        "_link_target",
        "_same_path",
        "_implied_repo_root",
        "_is_repo_checkout",
        "_is_main_checkout",
        "_check_applicable_links",
        "_find_orphaned_links",
        "_check_orphaned_links",
        "_live_backup_paths",
        "_dir_applies",
        "_check_unmanaged_files",
        "VALID_HARNESSES",
        "VALID_PROFILES",
        "DEFAULT_PROFILE",
        "load_links",
        "load_managed_dirs",
        "detect_wsl",
        "manifest_path",
        "read_manifest_entries",
        "format_path",
        "audit_links",
        "link_applies",
        "iter_concrete_links",
        "gather_links",
        "dir_applies",
    )

    def test_install_module_reexports_every_moved_name(self) -> None:
        import install  # noqa: PLC0415 — needs repo root on sys.path

        missing = [name for name in self.ALIASES if not hasattr(install, name)]
        self.assertEqual(missing, [])

    def test_alias_bodies_identical_to_module_functions(self) -> None:
        import install  # noqa: PLC0415 — needs repo root on sys.path

        pairs = {
            "_is_symlink": "is_symlink",
            "_path_exists": "path_exists",
            "_link_target": "link_target",
            "_same_path": "same_path",
            "_implied_repo_root": "implied_repo_root",
            "_is_repo_checkout": "is_repo_checkout",
            "_is_main_checkout": "is_main_checkout",
            "expand_dest": "expand_dest",
        }
        for install_name, module_name in pairs.items():
            self.assertIs(
                getattr(install, install_name),
                getattr(li, module_name),
                f"install.{install_name} is not the link_inspect implementation",
            )


if __name__ == "__main__":
    unittest.main()
