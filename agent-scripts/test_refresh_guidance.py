#!/usr/bin/env python3
"""Tests for refresh_guidance.py. Run with: python3 test_refresh_guidance.py"""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import refresh_guidance as rg

pytestmark = (
    pytest.mark.allow_real_subprocess
)  # creates/reads real git repos in temp dirs


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _init_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")


def _commit_all(root: Path, message: str) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)
    return _git(root, "rev-parse", "HEAD")


DOC_SET = rg.DocSetConfig(
    fixed_docs=("README.md", "STYLE.md"),
    script_dirs=("scripts",),
)


class ClaimExtractionTestCase(unittest.TestCase):
    """Covers verification step 1: the six extraction/checking scenarios."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.repo = Path(self.tmpdir)
        _init_repo(self.repo)
        (self.repo / "scripts").mkdir()
        (self.repo / "scripts" / "widget.py").write_text(
            "#!/usr/bin/env python3\n"
            '"""widget.py — a fake CLI for tests."""\n'
            "import argparse\n\n"
            "def build_parser():\n"
            "    parser = argparse.ArgumentParser()\n"
            "    parser.add_argument('--flag', action='store_true')\n"
            "    sub = parser.add_subparsers(dest='cmd')\n"
            "    sub.add_parser('run')\n"
            "    return parser\n\n"
            "if __name__ == '__main__':\n"
            "    build_parser().parse_args()\n"
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir)

    def _claims(self, text: str) -> list[rg.Claim]:
        known = set(rg.discover_scripts(self.repo, DOC_SET))
        _, claims = rg.parse_document("AGENTS.md", text, known)
        return claims

    def _findings(self, text: str) -> list[str]:
        known_scripts = rg.discover_scripts(self.repo, DOC_SET)
        basename_index = rg.discover_basename_index(self.repo)
        claims = self._claims(text)
        cli_cache: dict = {}
        details = []
        for claim in claims:
            detail = (
                rg.check_path_claim(claim, self.repo, basename_index)
                if claim.kind == "path"
                else rg.check_command_claim(
                    claim, known_scripts, cli_cache, self.repo, basename_index
                )
            )
            if detail:
                details.append(detail)
        return details

    def test_valid_code_span_path_not_flagged(self) -> None:
        (self.repo / "STYLE.md").write_text("house style\n")
        text = "## Standards\n\nSee `STYLE.md` for conventions.\n"
        self.assertEqual(self._findings(text), [])

    def test_broken_code_span_path_flagged(self) -> None:
        text = "## Standards\n\nSee `MISSING_DOC.md` for conventions.\n"
        findings = self._findings(text)
        self.assertEqual(len(findings), 1)
        self.assertIn("does not exist", findings[0])

    def test_plain_prose_mention_of_broken_path_not_flagged(self) -> None:
        text = "## Standards\n\nSee MISSING_DOC.md for conventions (no backticks).\n"
        self.assertEqual(self._findings(text), [])

    def test_valid_command_and_flag_not_flagged(self) -> None:
        text = "## Usage\n\nRun `python3 scripts/widget.py --flag`.\n"
        self.assertEqual(self._findings(text), [])

    def test_invalid_flag_flagged(self) -> None:
        text = "## Usage\n\nRun `python3 scripts/widget.py --nope`.\n"
        findings = self._findings(text)
        self.assertEqual(len(findings), 1)
        self.assertIn("unknown flag", findings[0])

    def test_unknown_script_name_flagged(self) -> None:
        text = "## Usage\n\nRun `python3 scripts/ghost.py --flag`.\n"
        findings = self._findings(text)
        self.assertEqual(len(findings), 1)
        self.assertIn("no such script", findings[0])

    def test_cross_reference_heading_resolves(self) -> None:
        (self.repo / "STYLE.md").write_text("intro\n\n## Formatting\n\nbody\n")
        text = "## Standards\n\nSee `STYLE.md#Formatting`.\n"
        self.assertEqual(self._findings(text), [])

    def test_cross_reference_heading_broken_flagged(self) -> None:
        (self.repo / "STYLE.md").write_text("intro\n\n## Formatting\n\nbody\n")
        text = "## Standards\n\nSee `STYLE.md#Nonexistent Heading`.\n"
        findings = self._findings(text)
        self.assertEqual(len(findings), 1)
        self.assertIn("heading not found", findings[0])

    def test_fenced_block_command_checked_too(self) -> None:
        text = "## Usage\n\n```bash\npython3 scripts/widget.py --nope\n```\n"
        findings = self._findings(text)
        self.assertEqual(len(findings), 1)
        self.assertIn("unknown flag", findings[0])

    def test_heading_inside_fence_not_treated_as_section_boundary(self) -> None:
        text = "## Real Section\n\n```\n## Not A Real Heading\n```\n"
        sections, _ = rg.parse_document("AGENTS.md", text, set())
        headings = [s.heading for s in sections]
        self.assertIn("Real Section", headings)
        self.assertNotIn("Not A Real Heading", headings)


class DiscoveryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.repo = Path(self.tmpdir)
        _init_repo(self.repo)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir)

    def test_discover_agents_md_finds_nested_and_skips_symlink(self) -> None:
        (self.repo / "AGENTS.md").write_text("# root\n")
        (self.repo / "sub").mkdir()
        (self.repo / "sub" / "AGENTS.md").write_text("# sub\n")
        (self.repo / "sub" / "CLAUDE.md").symlink_to("AGENTS.md")
        _commit_all(self.repo, "add docs")

        found = rg.discover_agents_md(self.repo)
        self.assertEqual(found, ["AGENTS.md", "sub/AGENTS.md"])

    def test_discovered_docs_skips_absent_fixed_doc(self) -> None:
        (self.repo / "README.md").write_text("readme\n")
        _commit_all(self.repo, "add readme")
        docs = rg.discovered_docs(self.repo, DOC_SET)
        self.assertIn("README.md", docs)
        self.assertNotIn("STYLE.md", docs)

    def test_discover_scripts_maps_basenames_and_skips_tests(self) -> None:
        (self.repo / "scripts").mkdir()
        (self.repo / "scripts" / "widget.py").write_text("# widget\n")
        (self.repo / "scripts" / "test_widget.py").write_text("# test\n")
        scripts = rg.discover_scripts(self.repo, DOC_SET)
        self.assertIn("widget.py", scripts)
        self.assertNotIn("test_widget.py", scripts)

    def _make_code_dir(self, name: str, count: int) -> None:
        (self.repo / name).mkdir()
        for i in range(count):
            (self.repo / name / f"module_{i}.py").write_text(f"# {i}\n")

    def test_flags_undocumented_code_directory_over_threshold(self) -> None:
        self._make_code_dir("bigdir", 5)
        _commit_all(self.repo, "add bigdir")
        found = rg.discover_undocumented_dirs(self.repo, threshold=5)
        self.assertEqual([d.directory for d in found], ["bigdir"])
        self.assertEqual(found[0].file_count, 5)

    def test_under_threshold_not_flagged(self) -> None:
        self._make_code_dir("smalldir", 4)
        _commit_all(self.repo, "add smalldir")
        found = rg.discover_undocumented_dirs(self.repo, threshold=5)
        self.assertEqual(found, [])

    def test_directory_with_agents_md_not_flagged(self) -> None:
        self._make_code_dir("docdir", 5)
        (self.repo / "docdir" / "AGENTS.md").write_text("# docdir\n")
        _commit_all(self.repo, "add docdir")
        found = rg.discover_undocumented_dirs(self.repo, threshold=5)
        self.assertEqual(found, [])

    def test_non_code_directory_not_flagged(self) -> None:
        (self.repo / "assets").mkdir()
        for i in range(6):
            (self.repo / "assets" / f"icon_{i}.png").write_text("fake")
        _commit_all(self.repo, "add assets")
        found = rg.discover_undocumented_dirs(self.repo, threshold=5)
        self.assertEqual(found, [])


class CrossRepoScriptsTestCase(unittest.TestCase):
    """A bare `dev_status.py`-style citation in a dotfiles-shaped repo must
    resolve against a sibling agent-toolkit checkout, not read as broken."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.agent_toolkit_root = Path(self.tmpdir) / "agent-toolkit"
        (self.agent_toolkit_root / "agent-scripts").mkdir(parents=True)
        (self.agent_toolkit_root / "agent-scripts" / "shared_tool.py").write_text(
            '#!/usr/bin/env python3\n"""shared_tool.py."""\nimport argparse\n\n'
            "def build_parser():\n"
            "    parser = argparse.ArgumentParser()\n"
            "    parser.add_argument('--flag', action='store_true')\n"
            "    return parser\n"
        )

        self.repo = Path(self.tmpdir) / "dotfiles"
        _init_repo(self.repo)
        _init_repo(self.agent_toolkit_root)
        _commit_all(self.agent_toolkit_root, "init toolkit")
        self.doc_set = rg.DocSetConfig(
            fixed_docs=("README.md",), script_dirs=("scripts",), cross_repo_scripts=True
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir)

    def test_bare_cross_repo_script_not_flagged(self) -> None:
        scripts = rg.discover_scripts(self.repo, self.doc_set, self.agent_toolkit_root)
        self.assertIn("shared_tool.py", scripts)

    def test_check_uses_agent_toolkit_root_for_command_claim(self) -> None:
        (self.repo / "README.md").write_text(
            "## Usage\n\nRun `python3 shared_tool.py --flag`.\n"
        )
        _commit_all(self.repo, "add readme")
        rg.DOC_SETS["custom-cross-repo"] = self.doc_set
        try:
            result = rg.run_check(
                self.repo, "custom-cross-repo", self.agent_toolkit_root
            )
        finally:
            del rg.DOC_SETS["custom-cross-repo"]
        self.assertEqual(result.findings, [])

    def test_without_agent_toolkit_root_it_is_flagged(self) -> None:
        (self.repo / "README.md").write_text(
            "## Usage\n\nRun `python3 shared_tool.py --flag`.\n"
        )
        _commit_all(self.repo, "add readme")
        rg.DOC_SETS["custom-cross-repo"] = self.doc_set
        try:
            result = rg.run_check(
                self.repo, "custom-cross-repo", Path(self.tmpdir) / "nope"
            )
        finally:
            del rg.DOC_SETS["custom-cross-repo"]
        self.assertEqual(len(result.findings), 1)
        self.assertIn("no such script", result.findings[0].detail)

    def test_cross_repo_path_claim_bare_and_qualified_not_flagged(self) -> None:
        (self.agent_toolkit_root / "MIGRATION.md").write_text("## Migration\n\nContent.\n")
        (self.agent_toolkit_root / "templates").mkdir()
        (self.agent_toolkit_root / "templates" / "swarm.md.tmpl").write_text("template")
        (self.agent_toolkit_root / "README.md").write_text("## Toolkit\n\nRoot readme.\n")
        _commit_all(self.agent_toolkit_root, "add cross repo files")

        (self.repo / "README.md").write_text(
            "## Docs\n\n"
            "See `MIGRATION.md` for migration guide.\n"
            "Template lives at `templates/swarm.md.tmpl`.\n"
            "Also refer to `agent-toolkit/README.md`.\n"
        )
        _commit_all(self.repo, "add readme")
        rg.DOC_SETS["custom-cross-repo"] = self.doc_set
        try:
            result = rg.run_check(
                self.repo, "custom-cross-repo", self.agent_toolkit_root
            )
        finally:
            del rg.DOC_SETS["custom-cross-repo"]
        self.assertEqual(result.findings, [])

    def test_cross_repo_path_claim_heading_fragment_verified(self) -> None:
        (self.agent_toolkit_root / "guide.md").write_text("## Real Heading\n\nBody.\n")
        _commit_all(self.agent_toolkit_root, "add guide")

        (self.repo / "README.md").write_text(
            "## Docs\n\n"
            "Valid: `guide.md#Real Heading`.\n"
            "Invalid: `guide.md#Ghost Heading`.\n"
        )
        _commit_all(self.repo, "add readme")
        rg.DOC_SETS["custom-cross-repo"] = self.doc_set
        try:
            result = rg.run_check(
                self.repo, "custom-cross-repo", self.agent_toolkit_root
            )
        finally:
            del rg.DOC_SETS["custom-cross-repo"]
        self.assertEqual(len(result.findings), 1)
        self.assertIn("heading not found in `guide.md`: 'Ghost Heading'", result.findings[0].detail)


class ClaimExemptDocsTestCase(unittest.TestCase):
    """A CHANGELOG.md-style historical doc keeps its sections tracked for
    staleness, but a broken path in it is never reported as a finding."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.repo = Path(self.tmpdir)
        _init_repo(self.repo)
        (self.repo / "CHANGELOG.md").write_text(
            "## 2026-01-01\n\nAdded `gone_now.py`, since removed.\n"
        )
        (self.repo / "README.md").write_text(
            "## Current\n\nSee `also_gone.py` for details.\n"
        )
        _commit_all(self.repo, "add docs")
        self.doc_set = rg.DocSetConfig(
            fixed_docs=("README.md", "CHANGELOG.md"),
            script_dirs=("scripts",),
            claim_exempt_docs=("CHANGELOG.md",),
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir)

    def test_exempt_doc_claim_not_flagged_but_section_still_tracked(self) -> None:
        rg.DOC_SETS["custom-exempt"] = self.doc_set
        try:
            result = rg.run_check(self.repo, "custom-exempt")
        finally:
            del rg.DOC_SETS["custom-exempt"]

        docs_with_findings = {f.doc for f in result.findings}
        self.assertNotIn("CHANGELOG.md", docs_with_findings)
        self.assertIn("README.md", docs_with_findings)

        headings = {(s.doc, s.heading) for s in result.sections}
        self.assertIn(("CHANGELOG.md", "2026-01-01"), headings)


class StateRoundTripTestCase(unittest.TestCase):
    """Covers verification step 4: mark reviewed -> re-run -> not re-flagged."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.repo = Path(self.tmpdir)
        _init_repo(self.repo)
        (self.repo / "README.md").write_text(
            "intro\n\n## First Section\n\nbody one\n\n## Second Section\n\nbody two\n"
        )
        _commit_all(self.repo, "add readme")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir)

    def test_unreviewed_section_falls_back_to_git_blame_age(self) -> None:
        rg.DOC_SETS["custom"] = DOC_SET
        try:
            result = rg.run_check(self.repo, "custom")
        finally:
            del rg.DOC_SETS["custom"]
        statuses = {s.heading: s for s in result.sections}
        self.assertIn("First Section", statuses)
        self.assertFalse(statuses["First Section"].reviewed)
        self.assertIsNotNone(statuses["First Section"].fallback_commit)

    def test_mark_reviewed_then_recheck_not_flagged_as_unreviewed(self) -> None:
        rg.DOC_SETS["custom"] = DOC_SET
        try:
            rg.cmd_mark_reviewed(
                self.repo,
                "custom",
                "README.md",
                "First Section",
                commit="abc1234",
                date="2026-09-07",
                quiet=True,
            )
            result = rg.run_check(self.repo, "custom")
        finally:
            del rg.DOC_SETS["custom"]

        statuses = {s.heading: s for s in result.sections}
        self.assertTrue(statuses["First Section"].reviewed)
        self.assertEqual(statuses["First Section"].last_reviewed_commit, "abc1234")
        self.assertFalse(statuses["Second Section"].reviewed)

    def test_mark_reviewed_rejects_unknown_heading(self) -> None:
        rg.DOC_SETS["custom"] = DOC_SET
        try:
            with self.assertRaises(SystemExit) as ctx:
                rg.cmd_mark_reviewed(
                    self.repo,
                    "custom",
                    "README.md",
                    "No Such Section",
                    None,
                    None,
                    quiet=True,
                )
            self.assertEqual(ctx.exception.code, 2)
        finally:
            del rg.DOC_SETS["custom"]

    def test_mark_reviewed_rejects_unknown_doc(self) -> None:
        rg.DOC_SETS["custom"] = DOC_SET
        try:
            with self.assertRaises(SystemExit) as ctx:
                rg.cmd_mark_reviewed(
                    self.repo,
                    "custom",
                    "NOPE.md",
                    "First Section",
                    None,
                    None,
                    quiet=True,
                )
            self.assertEqual(ctx.exception.code, 2)
        finally:
            del rg.DOC_SETS["custom"]


class ExternalDocSetConfigTestCase(unittest.TestCase):
    """Covers loading <repo_root>/refresh-guidance.toml into a DocSetConfig
    — the mechanism that lets a calling repo (dotfiles, or any future
    third repo) supply its own doc-set config without agent-toolkit
    hardcoding that repo's internal layout."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.repo = Path(self.tmpdir)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir)

    def _write_toml(self, text: str) -> None:
        (self.repo / rg.EXTERNAL_CONFIG_FILENAME).write_text(text)

    def test_no_file_returns_none(self) -> None:
        self.assertIsNone(rg.load_external_doc_set(self.repo))

    def test_valid_file_produces_expected_config_with_tuples(self) -> None:
        self._write_toml(
            'fixed_docs = ["README.md", "STYLE.md"]\n'
            'script_dirs = ["tools", "scripts"]\n'
            "cross_repo_scripts = true\n"
        )
        doc_set = rg.load_external_doc_set(self.repo)
        assert doc_set is not None
        self.assertEqual(doc_set.fixed_docs, ("README.md", "STYLE.md"))
        self.assertIsInstance(doc_set.fixed_docs, tuple)
        self.assertEqual(doc_set.script_dirs, ("tools", "scripts"))
        self.assertTrue(doc_set.cross_repo_scripts)
        # Optional fields not given fall back to DocSetConfig's own defaults.
        self.assertEqual(doc_set.claim_exempt_docs, ("CHANGELOG.md",))

    def test_missing_required_key_raises_config_error(self) -> None:
        self._write_toml('fixed_docs = ["README.md"]\n')  # no script_dirs
        with self.assertRaises(rg.ConfigError) as ctx:
            rg.load_external_doc_set(self.repo)
        self.assertIn("script_dirs", str(ctx.exception))

    def test_unknown_key_raises_config_error(self) -> None:
        self._write_toml(
            'fixed_docs = ["README.md"]\n'
            'script_dirs = ["scripts"]\n'
            'bogus_key = "oops"\n'
        )
        with self.assertRaises(rg.ConfigError) as ctx:
            rg.load_external_doc_set(self.repo)
        self.assertIn("bogus_key", str(ctx.exception))

    def test_invalid_toml_raises_config_error(self) -> None:
        self._write_toml("this is not [valid toml\n")
        with self.assertRaises(rg.ConfigError):
            rg.load_external_doc_set(self.repo)

    def test_non_string_array_element_raises_config_error(self) -> None:
        self._write_toml('fixed_docs = ["README.md"]\nscript_dirs = ["scripts", 123]\n')
        with self.assertRaises(rg.ConfigError) as ctx:
            rg.load_external_doc_set(self.repo)
        message = str(ctx.exception)
        self.assertIn("script_dirs", message)
        self.assertIn("1", message)

    def test_wrong_type_scalar_raises_config_error(self) -> None:
        self._write_toml(
            'fixed_docs = ["README.md"]\n'
            'script_dirs = ["scripts"]\n'
            'cross_repo_scripts = "true"\n'
        )
        with self.assertRaises(rg.ConfigError) as ctx:
            rg.load_external_doc_set(self.repo)
        self.assertIn("cross_repo_scripts", str(ctx.exception))


class DocSetResolutionTestCase(unittest.TestCase):
    """Covers resolve_doc_set's precedence rules -- no path through it may
    silently apply agent-toolkit's own doc-set to a repo that isn't
    agent-toolkit."""

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.repo = Path(self.tmpdir)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir)

    def test_external_file_used_when_doc_set_name_is_none(self) -> None:
        (self.repo / rg.EXTERNAL_CONFIG_FILENAME).write_text(
            'fixed_docs = ["README.md"]\nscript_dirs = ["scripts"]\n'
        )
        doc_set, label = rg.resolve_doc_set(self.repo, None)
        self.assertEqual(doc_set.fixed_docs, ("README.md",))
        self.assertIn("external config", label)

    def test_explicit_doc_set_with_external_file_conflicts(self) -> None:
        (self.repo / rg.EXTERNAL_CONFIG_FILENAME).write_text(
            'fixed_docs = ["README.md"]\nscript_dirs = ["scripts"]\n'
        )
        with self.assertRaises(rg.ConfigError) as ctx:
            rg.resolve_doc_set(self.repo, "agent-toolkit")
        self.assertIn("refresh-guidance.toml", str(ctx.exception))

    def test_no_external_file_and_no_doc_set_name_errors(self) -> None:
        with self.assertRaises(rg.ConfigError) as ctx:
            rg.resolve_doc_set(self.repo, None)
        self.assertIn("no doc-set specified", str(ctx.exception))

    def test_no_external_file_explicit_known_name_resolves(self) -> None:
        doc_set, label = rg.resolve_doc_set(self.repo, "agent-toolkit")
        self.assertIs(doc_set, rg.DOC_SETS["agent-toolkit"])
        self.assertEqual(label, "agent-toolkit")

    def test_no_external_file_unknown_name_errors(self) -> None:
        with self.assertRaises(rg.ConfigError):
            rg.resolve_doc_set(self.repo, "nonexistent-doc-set")

    def test_dotfiles_leak_is_gone(self) -> None:
        """The leak this item exists to remove: DOC_SETS must carry no
        repo-specific config for anything other than agent-toolkit."""
        self.assertEqual(list(rg.DOC_SETS.keys()), ["agent-toolkit"])


class RealRepoSmokeTestCase(unittest.TestCase):
    """Covers verification step 2: run against agent-toolkit's actual docs.

    One of the known findings below is a genuine stale reference
    (`.github/SECRET_CHECK.md` is cited but doesn't exist); the rest are
    accepted false positives -- a bare filename that names
    something outside the repo (a runtime data file, another tool's own
    doc) is mechanically indistinguishable from a genuinely-broken
    repo-relative citation, and `pi/node_modules` is an uncommitted
    dependency directory that exists only after `bun install` runs in
    `pi/`, so a fresh worktree legitimately lacks it. The known set is
    subtractive (`actual - known`), so an entry for a finding that never
    occurs (deps installed) masks nothing: the checker only flags paths
    that don't exist, so an existing `pi/node_modules` can never produce
    this finding. This asserts no *new* finding appears (a
    regression in the checking logic) without being brittle to line
    numbers, and doesn't fail if one of the known findings gets fixed.
    """

    _KNOWN_FINDINGS = {
        ("README.md", "backlog.json"),
        ("STYLE.md", ".github/SECRET_CHECK.md"),
        ("pi/AGENTS.md", "docs/skills.md"),
        ("pi/AGENTS.md", "docs/prompt-templates.md"),
        ("pi/AGENTS.md", "pi/node_modules"),
        ("pi/AGENTS.md", "swarm-picker-copilot.ts"),
        ("README.md", "pi/node_modules"),
    }

    def test_every_pi_node_modules_citation_is_a_known_finding(self) -> None:
        """Regression guard for the fresh-worktree variant: any scanned,
        claim-checked doc that cites `pi/node_modules` must have a matching
        `_KNOWN_FINDINGS` entry, because a fresh worktree legitimately lacks
        the untracked directory and the checker then flags the citation as
        stale (the smoke test above only catches this when it happens to run
        in a worktree before `bootstrap-worktree.sh` installed the deps).
        """
        repo_root = rg.DEFAULT_REPO_ROOT
        doc_set, _label = rg.resolve_doc_set(repo_root, "agent-toolkit")
        for doc in rg.discovered_docs(repo_root, doc_set):
            if doc in doc_set.claim_exempt_docs:
                continue
            text = (repo_root / doc).read_text(encoding="utf-8", errors="replace")
            _, claims = rg.parse_document(doc, text, set())
            cited = {
                (claim.doc, claim.raw)
                for claim in claims
                if claim.kind == "path" and claim.raw == "pi/node_modules"
            }
            self.assertEqual(
                cited - self._KNOWN_FINDINGS,
                set(),
                f"{doc} cites `pi/node_modules` without a _KNOWN_FINDINGS "
                "entry; a fresh worktree without it flags the citation stale",
            )

    def test_no_unexpected_findings_against_this_checkout(self) -> None:
        result = rg.run_check(rg.DEFAULT_REPO_ROOT, "agent-toolkit")
        actual = {(f.doc, f.raw) for f in result.findings}
        unexpected = actual - self._KNOWN_FINDINGS
        self.assertEqual(
            unexpected, set(), f"new/unexpected stale claim(s): {unexpected}"
        )
        self.assertGreater(len(result.sections), 0)


class CliParsingTestCase(unittest.TestCase):
    def test_verbosity_and_repo_root_flags_parse_after_every_leaf_subcommand(
        self,
    ) -> None:
        for cmd, extra in (
            ("check", []),
            ("mark-reviewed", ["AGENTS.md", "Some Heading"]),
        ):
            args = rg.build_parser().parse_args(
                [cmd, *extra, "-q", "--doc-set", "agent-toolkit"]
            )
            self.assertTrue(args.quiet)
            self.assertEqual(args.doc_set, "agent-toolkit")

    def test_doc_set_omitted_parses_to_none(self) -> None:
        args = rg.build_parser().parse_args(["check"])
        self.assertIsNone(args.doc_set)

    def test_bare_invocation_defaults_to_check(self) -> None:
        args = rg.build_parser().parse_args([])
        self.assertIsNone(args.subcommand)


if __name__ == "__main__":
    unittest.main()
