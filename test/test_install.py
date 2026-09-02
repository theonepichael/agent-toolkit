#!/usr/bin/env python3
"""Fast-tier tests for install.py — no packages, no subprocesses, no real HOME.

Run with: ``uv run --with pytest pytest test/test_install.py`` (or
``pytest test/test_install.py`` in an environment that already has pytest).

Everything here runs against a throwaway home directory under ``tmp_path``
and a stubbed :func:`install.run_command`, so nothing touches the machine.
The container-based lifecycle suite (``test/run.sh`` → ``scenarios.sh``)
still owns the slow tier: real package managers, real service enablement.

Requires Python 3.12+.
"""

import io
import json
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "claude" / "scripts"))


import settings_seed_drift_check  # noqa: E402 — must follow sys.path.insert above

import depart  # noqa: E402 — must follow sys.path.insert above
import install  # noqa: E402 — must follow sys.path.insert above

# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def no_subprocesses(monkeypatch):
    """Fail loudly if any test path tries to shell out for real."""

    def _forbidden(*args, **kwargs):
        raise AssertionError(f"unexpected subprocess call: {args!r}")

    monkeypatch.setattr(install, "run_command", _forbidden)


@pytest.fixture
def home(tmp_path):
    """A throwaway home directory, handed to Context instead of the real one.

    Context carries its home explicitly (rather than calling Path.home()
    per use), so pointing it here is enough to keep every test off the real
    machine — no HOME monkeypatching needed.
    """
    path = tmp_path / "home"
    path.mkdir()
    return path


def make_ctx(
    home,
    *,
    harnesses=("claude",),
    profile="personal",
    dry_run=False,
    force=False,
    wipe=False,
    no_nvim_pin=False,
    reseed=False,
    adopt=False,
    depart=False,
    yes=False,
    check_links=False,
    report_uninstalled=False,
    system="Linux",
    is_wsl=False,
    dotfiles=REPO_ROOT,
):
    """Build a Context pointed at a throwaway home and the real repo."""
    opts = install.Options(
        harnesses=tuple(harnesses),
        profile=profile,
        dry_run=dry_run,
        force=force,
        wipe=wipe,
        no_nvim_pin=no_nvim_pin,
        reseed=reseed,
        adopt=adopt,
        depart=depart,
        yes=yes,
        check_links=check_links,
        report_uninstalled=report_uninstalled,
    )
    state_dir = home / ".local" / "state" / "dotfiles"
    return install.Context(
        dotfiles=dotfiles,
        home=home,
        opts=opts,
        manifest=install.Manifest(state_dir / "history.jsonl", dry_run=dry_run),
        reporter=install.Reporter(),
        system=system,
        is_wsl=is_wsl,
    )


def history(ctx):
    """Return the recorded history entries, oldest first."""
    if not ctx.manifest.path.is_file():
        return []
    return ctx.manifest.entries()


def kinds(ctx, kind):
    """Return every history entry of one kind."""
    return [e for e in history(ctx) if e["kind"] == kind]


@pytest.fixture
def offline_install(monkeypatch):
    """Neuter every step that would install software or touch a service.

    Leaves the file-level steps (symlinks, seeds, marker, history) real, so
    a full ``run_install`` can be exercised end to end.
    """
    for name in (
        "install_mac_packages",
        "install_linux_packages",
        "install_node",
        "capture_service_baseline",
        "enable_managed_services",
        "capture_git_hooks_path_baseline",
        "install_global_git_hooks_path",
        "load_watchcommit_agent",
        "import_rectangle_prefs",
        "set_caps_lock_to_escape",
        "install_vim_plug",
        "bootstrap_neovim",
    ):
        monkeypatch.setattr(install, name, lambda *a, **k: None)
    monkeypatch.setattr(install, "install_npm_harness", lambda *a, **k: None)


@pytest.fixture
def links():
    """The repo's real links.toml, parsed."""
    return install.load_links(REPO_ROOT / "links.toml")


# ── CLI validation ────────────────────────────────────────────────────────────


def parse_error(argv, capsys):
    """Run parse_args expecting a refusal; return (exit code, stderr)."""
    with pytest.raises(SystemExit) as excinfo:
        install.parse_args(argv)
    return excinfo.value.code, capsys.readouterr().err


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["--harness="], "empty value"),
        (["--harness=claude,"], "empty value"),
        (["--harness=bogus"], "unknown harness: bogus"),
        ([], "no --harness specified"),
        (["--bogus"], "unknown argument: --bogus"),
        # The pre-harness flags were a hard cutover with no back-compat
        # shims, so they fall through to the generic unknown-argument path.
        (["--work"], "unknown argument: --work"),
        (["--copilot"], "unknown argument: --copilot"),
        (["--profile=nope", "--harness=claude"], "invalid --profile: nope"),
        (
            ["--profile=work", "--harness=opencode"],
            "--harness=opencode is not allowed with --profile=work",
        ),
        (
            ["--profile=work", "--harness=copilot,opencode"],
            "--harness=opencode is not allowed with --profile=work",
        ),
        (["--rollback", "--harness=claude"], "must be used alone"),
        (["--rollback", "--profile=work"], "must be used alone"),
        (["--rollback", "--force"], "must be used alone"),
        (["--rollback", "--reseed"], "must be used alone"),
        (["--rollback", "--adopt"], "must be used alone"),
        (["--adopt", "--reseed", "--harness=claude"], "cannot be used together"),
        (["--reseed"], "no --harness specified"),
        # No --harness alongside --wipe, deliberately: exercises the
        # parse_args ordering fix (the --wipe-without-rollback check must
        # fire before the missing-harness check, not after).
        (["--wipe"], "--wipe can only be used with --rollback"),
        (["--depart", "--harness=claude"], "--depart must be used alone"),
        (["--depart", "--rollback"], "--depart must be used alone"),
        (["--depart", "--wipe"], "--depart must be used alone"),
        (["--depart", "--profile=work"], "--depart must be used alone"),
        (["--depart", "--force"], "--depart must be used alone"),
        (["--depart", "--reseed"], "--depart must be used alone"),
        (["--depart", "--adopt"], "--depart must be used alone"),
        (["--check-links", "--adopt"], "--check-links must be used alone"),
        (
            ["--report-uninstalled"],
            "--report-uninstalled can only be used with --check-links",
        ),
        (
            ["--report-uninstalled", "--harness=claude"],
            "--report-uninstalled can only be used with --check-links",
        ),
        (["--depart", "--no-nvim-pin"], "--depart must be used alone"),
        # --depart --rollback names the --depart conflict, not rollback's —
        # the --depart-alone check runs first.
        (["--rollback", "--depart"], "--depart must be used alone"),
        (["--yes"], "--yes can only be used with --depart"),
        (["--yes", "--harness=claude"], "--yes can only be used with --depart"),
    ],
)
def test_argument_errors_exit_2(argv, expected, capsys):
    code, err = parse_error(argv, capsys)
    assert code == 2
    assert expected in err


def test_help_exits_zero_with_usage(capsys):
    with pytest.raises(SystemExit) as excinfo:
        install.parse_args(["--help"])
    assert excinfo.value.code == 0
    assert capsys.readouterr().out.startswith("usage:")


def test_unknown_argument_reported_before_bad_value(capsys):
    """A typo'd flag names the typo, not the downstream validation failure."""
    _, err = parse_error(["--bogus", "--profile=nope"], capsys)
    assert "unknown argument: --bogus" in err
    assert "invalid --profile" not in err


def test_repeated_harness_flags_accumulate():
    opts = install.parse_args(["--harness=claude", "--harness=copilot"])
    assert opts.harnesses == ("claude", "copilot")


def test_comma_separated_harnesses():
    opts = install.parse_args(["--harness=claude,opencode,agy"])
    assert opts.harnesses == ("claude", "opencode", "agy")


def test_depart_alone_parses():
    opts = install.parse_args(["--depart"])
    assert opts.depart is True
    assert opts.yes is False


def test_depart_with_yes_and_dry_run_parses():
    opts = install.parse_args(["--depart", "--yes", "--dry-run"])
    assert opts.depart is True
    assert opts.yes is True
    assert opts.dry_run is True


def test_usage_documents_depart(capsys):
    with pytest.raises(SystemExit):
        install.parse_args(["--help"])
    assert "--depart" in capsys.readouterr().out


def test_duplicate_harness_is_collapsed():
    opts = install.parse_args(["--harness=claude,claude", "--harness=claude"])
    assert opts.harnesses == ("claude",)


def test_rollback_allows_only_dry_run():
    opts = install.parse_args(["--rollback", "--dry-run"])
    assert opts.rollback and opts.dry_run and opts.harnesses == ()


def test_rollback_wipe_parses():
    opts = install.parse_args(["--rollback", "--wipe"])
    assert opts.rollback and opts.wipe and not opts.dry_run


def test_rollback_wipe_dry_run_parses():
    opts = install.parse_args(["--rollback", "--wipe", "--dry-run"])
    assert opts.rollback and opts.wipe and opts.dry_run


def test_defaults():
    opts = install.parse_args(["--harness=claude"])
    assert opts.profile == "personal"
    assert not (opts.rollback or opts.force or opts.dry_run or opts.no_nvim_pin)


def test_no_nvim_pin_flag_parses():
    opts = install.parse_args(["--harness=claude", "--no-nvim-pin"])
    assert opts.no_nvim_pin


def test_reseed_flag_parses():
    opts = install.parse_args(["--harness=claude", "--reseed"])
    assert opts.reseed


def test_adopt_flag_parses():
    opts = install.parse_args(["--harness=claude", "--adopt"])
    assert opts.adopt


# ── links.toml ────────────────────────────────────────────────────────────────


def test_links_table_parses_and_sources_exist(links):
    assert links, "links.toml should not be empty"
    for spec in links:
        assert (REPO_ROOT / spec.src).exists(), f"missing repo source: {spec.src}"
        assert spec.dest.startswith("~/")


def test_every_claude_script_has_a_links_entry(links):
    """Every production (non-test) script in claude/scripts/ must have a
    links.toml entry, or ~/.claude/scripts/<name> silently never exists at
    install time -- caught live twice already (test_dev_status.py's
    pre-existing manual symlink papering over a missing entry, then
    vitals_promotion.py shipping with no entry at all, found via a live
    grill-me spot-check). Test files are excluded: they're always run
    in-repo (``python3 test_X.py`` from claude/scripts/), never invoked via
    the deployed ~/.claude/scripts/ path by any skill or production script.
    """
    linked_srcs = {spec.src for spec in links}
    scripts = sorted((REPO_ROOT / "claude" / "scripts").glob("*.py"))
    production_scripts = [p for p in scripts if not p.name.startswith("test_")]
    missing = [
        p.name
        for p in production_scripts
        if f"claude/scripts/{p.name}" not in linked_srcs
    ]
    assert not missing, f"claude/scripts scripts missing a links.toml entry: {missing}"


def test_no_test_file_has_a_links_entry(links):
    """The inverse of the coverage rule above: three test files
    (test_dev_status.py, test_dev_status_sync.py, test_vitals_promotion.py)
    had accumulated links.toml entries with no functional reason -- nothing
    in the repo ever invokes a test via the deployed ~/.claude/scripts/
    path, only via ``python3 test_X.py`` inside the checkout. Removed as
    unnecessary state; this guards against the pattern creeping back."""
    linked_test_files = [
        spec.src
        for spec in links
        if spec.src.startswith("claude/scripts/test_") and spec.src.endswith(".py")
    ]
    assert not linked_test_files, (
        f"unnecessary test-file links.toml entries: {linked_test_files}"
    )


def test_claude_script_links_use_correct_dest(links):
    """A typo'd dest is as broken as a missing entry -- the src still
    exists so test_links_table_parses_and_sources_exist won't catch it, but
    ~/.claude/scripts/<name> never gets the right symlink."""
    for spec in links:
        if spec.src.startswith("claude/scripts/") and spec.src.endswith(".py"):
            name = Path(spec.src).name
            assert spec.dest == f"~/.claude/scripts/{name}", spec.src


def test_links_reject_unknown_key(tmp_path):
    bad = tmp_path / "links.toml"
    bad.write_text('[[link]]\nsrc = "a"\ndest = "~/a"\nharnes = "claude"\n')
    with pytest.raises(ValueError, match="unknown key"):
        install.load_links(bad)


def test_links_reject_unknown_harness(tmp_path):
    bad = tmp_path / "links.toml"
    bad.write_text('[[link]]\nsrc = "a"\ndest = "~/a"\nharness = "nope"\n')
    with pytest.raises(ValueError, match="unknown harness"):
        install.load_links(bad)


def test_harness_gate(home, links):
    claude_only = make_ctx(home, harnesses=("claude",))
    dests = {s.dest for s in links if install.link_applies(s, claude_only)}
    assert "~/.claude/CLAUDE.md" in dests
    assert "~/.copilot/copilot-instructions.md" not in dests
    assert "~/.gemini/GEMINI.md" not in dests
    # Shared scripts stay linked no matter which harness was picked.
    assert "~/.claude/scripts/dev_status.py" in dests


def test_platform_and_profile_gates(home, links):
    linux_personal = make_ctx(home, system="Linux")
    dests = {s.dest for s in links if install.link_applies(s, linux_personal)}
    assert "~/.config/systemd/user/watchcommit.service" in dests
    assert "~/.claude/scripts/watchcommit_activity.py" in dests
    assert "~/.zprofile" not in dests
    assert "~/.config/Code/User/settings.json" in dests

    linux_work = make_ctx(home, system="Linux", profile="work")
    work_dests = {s.dest for s in links if install.link_applies(s, linux_work)}
    assert "~/.local/bin/watchcommit" not in work_dests
    assert "~/.config/systemd/user/watchcommit.service" not in work_dests
    assert "~/.claude/scripts/watchcommit_activity.py" not in work_dests

    mac = make_ctx(home, system="Darwin")
    mac_dests = {s.dest for s in links if install.link_applies(s, mac)}
    assert "~/.zprofile" in mac_dests
    assert "~/Library/LaunchAgents/com.user.watchcommit.plist" in mac_dests
    assert "~/.config/systemd/user/watchcommit.service" not in mac_dests

    wsl = make_ctx(home, system="Linux", is_wsl=True)
    wsl_dests = {s.dest for s in links if install.link_applies(s, wsl)}
    assert "~/.config/Code/User/settings.json" not in wsl_dests


def test_copilot_hook_platform_split(home, links):
    """The Copilot sessionStart hook is bash-only, so links.toml carries two
    same-destination entries gated mac/linux (the VS Code mutually-exclusive
    platform pattern) instead of one unqualified entry: on any given machine
    exactly one applies, and nothing installs the hook where the bash
    handler could not dispatch."""
    hook = "~/.copilot/hooks/session-start.json"
    entries = [s for s in links if s.dest == hook]
    assert len(entries) == 2
    assert {s.platform for s in entries} == {"mac", "linux"}
    assert all(s.harness == "copilot" for s in entries)

    for system in ("Linux", "Darwin"):
        ctx = make_ctx(home, harnesses=("copilot",), system=system)
        applying = [s for s in entries if install.link_applies(s, ctx)]
        assert len(applying) == 1, system
    # No copilot in --harness: neither entry links, on either platform.
    for system in ("Linux", "Darwin"):
        ctx = make_ctx(home, harnesses=("claude",), system=system)
        assert not any(install.link_applies(s, ctx) for s in entries)


def test_expand_dest(home):
    assert install.expand_dest("~/.vimrc", home) == home / ".vimrc"
    assert install.expand_dest("/etc/hosts", home) == Path("/etc/hosts")


# ── symlink engine ────────────────────────────────────────────────────────────


def test_symlink_creates_and_records(home):
    ctx = make_ctx(home)
    src = REPO_ROOT / "vim" / ".vimrc"
    dest = home / ".vimrc"

    assert install.symlink(ctx, src, dest)

    assert dest.is_symlink()
    assert Path(str(dest.resolve())) == src.resolve()
    assert kinds(ctx, "symlink-created") == [
        {"kind": "symlink-created", "dest": str(dest), "src": str(src)}
    ]


def test_symlink_backs_up_existing_file(home):
    ctx = make_ctx(home)
    src = REPO_ROOT / "vim" / ".vimrc"
    dest = home / ".vimrc"
    dest.write_text("sentinel-content\n")

    install.symlink(ctx, src, dest)

    assert dest.is_symlink()
    backup = home / ".vimrc.bak"
    assert backup.read_text() == "sentinel-content\n"
    assert kinds(ctx, "file-backed-up") == [
        {"kind": "file-backed-up", "dest": str(dest), "backup": str(backup)}
    ]
    assert len(kinds(ctx, "symlink-created")) == 1


def test_symlink_rerun_is_a_noop_and_not_rerecorded(home):
    ctx = make_ctx(home)
    src = REPO_ROOT / "vim" / ".vimrc"
    dest = home / ".vimrc"

    install.symlink(ctx, src, dest)
    install.symlink(ctx, src, dest)

    assert len(kinds(ctx, "symlink-created")) == 1


def test_symlink_replaces_a_link_pointing_elsewhere(home):
    ctx = make_ctx(home)
    src = REPO_ROOT / "vim" / ".vimrc"
    dest = home / ".vimrc"
    dest.symlink_to("/etc/hostname")

    install.symlink(ctx, src, dest)

    assert Path(str(dest.readlink())) == src
    # It was already a symlink, so the run that first created it owns the
    # history entry — this one must not add a second.
    assert kinds(ctx, "symlink-created") == []


def test_symlink_dry_run_changes_nothing(home, capsys):
    ctx = make_ctx(home, dry_run=True)
    src = REPO_ROOT / "vim" / ".vimrc"
    dest = home / ".vimrc"

    install.symlink(ctx, src, dest)

    assert not dest.exists()
    assert not ctx.manifest.path.exists()
    assert "would link" in capsys.readouterr().out


def test_symlink_directory_source(home):
    """A symlinked directory re-links in place, never nesting inside itself."""
    ctx = make_ctx(home)
    src = REPO_ROOT / "nvim"
    dest = home / ".config" / "nvim"

    install.symlink(ctx, src, dest)
    install.symlink(ctx, src, dest)

    assert dest.is_symlink()
    assert not (src / "nvim").exists()


# ── copy-once seeds and drift ─────────────────────────────────────────────────


def test_settings_seed_copied_once_then_reports_drift(home):
    ctx = make_ctx(home, harnesses=("claude",))
    seed_name, drift = install.seed_claude_settings(ctx)

    dest = home / ".claude" / "settings.json"
    assert seed_name == "settings.json"
    assert drift == ""
    assert dest.is_file() and not dest.is_symlink()
    assert dest.read_text() == (REPO_ROOT / "claude" / "settings.json").read_text()
    assert kinds(ctx, "file-copied") == [{"kind": "file-copied", "dest": str(dest)}]

    # Second run: the live file is edited, and must be reported, not replaced.
    live = json.loads(dest.read_text())
    live["model"] = "some-other-model"
    dest.write_text(json.dumps(live))

    ctx2 = make_ctx(home, harnesses=("claude",))
    _, drift2 = install.seed_claude_settings(ctx2)
    assert "model" in drift2
    assert json.loads(dest.read_text())["model"] == "some-other-model"
    # Still only run 1's copy on record — the second run reported, not rewrote.
    assert len(kinds(ctx2, "file-copied")) == 1


def test_work_profile_uses_the_work_seed(home):
    ctx = make_ctx(home, harnesses=("claude",), profile="work")
    seed_name, _ = install.seed_claude_settings(ctx)
    assert seed_name == "settings.work.json"
    assert (home / ".claude" / "settings.json").read_text() == (
        REPO_ROOT / "claude" / "settings.work.json"
    ).read_text()


def test_settings_seed_skipped_when_claude_not_selected(home):
    ctx = make_ctx(home, harnesses=("opencode",))
    assert install.seed_claude_settings(ctx) == ("", "")
    assert not (home / ".claude" / "settings.json").exists()


def test_opencode_seed_has_no_work_variant(home):
    """opencode is excluded from --profile=work at parse_args (see the CLI
    validation test) — this documents that seed_opencode_config itself has
    no separate work variant to accidentally seed either, even if a
    work-profile opencode Context were ever constructed directly."""
    personal = make_ctx(home, harnesses=("opencode",))
    name, drift = install.seed_opencode_config(personal)
    dest = home / ".config" / "opencode" / "opencode.jsonc"
    assert (name, drift) == ("opencode.jsonc", "")
    assert dest.read_text() == (REPO_ROOT / "opencode" / "opencode.jsonc").read_text()

    other_home = home.parent / "home2"
    other_home.mkdir()
    work = make_ctx(other_home, harnesses=("opencode",), profile="work")
    name, _ = install.seed_opencode_config(work)
    assert name == "opencode.jsonc"


def test_opencode_bypass_check_fires_specifically(home):
    """xargs/awk reappearing live is a SECURITY line, not generic drift."""
    ctx = make_ctx(home, harnesses=("opencode",))
    install.seed_opencode_config(ctx)

    dest = home / ".config" / "opencode" / "opencode.jsonc"
    live = json.loads(dest.read_text())
    live["permission"]["bash"]["xargs *"] = "allow"
    live["permission"]["bash"]["awk *"] = "allow"
    dest.write_text(json.dumps(live))

    _, drift = install.seed_opencode_config(make_ctx(home, harnesses=("opencode",)))
    assert drift.startswith("SECURITY: xargs *, awk *")
    assert "allowlist bypass" in drift


def test_opencode_generic_drift_when_no_bypass(home):
    ctx = make_ctx(home, harnesses=("opencode",))
    install.seed_opencode_config(ctx)

    dest = home / ".config" / "opencode" / "opencode.jsonc"
    live = json.loads(dest.read_text())
    live["theme"] = "custom"
    dest.write_text(json.dumps(live))

    _, drift = install.seed_opencode_config(make_ctx(home, harnesses=("opencode",)))
    assert drift == "theme"


def test_drift_helpers_on_raw_dicts():
    assert install.json_key_drift({"a": 1, "b": 2}, {"a": 1, "b": 3}) == ["b"]
    assert install.opencode_bypass_drift(
        {"permission": {"bash": {}}},
        {"permission": {"bash": {"awk *": "allow"}}},
    ) == ["awk *"]
    assert install.opencode_bypass_drift({}, {}) == []


def test_opencode_bypass_drift_covers_all_extended_patterns():
    """Each of the 13 newly-classified patterns triggers, not just xargs/awk."""
    new_patterns = [
        "git --no-pager *",
        "uv *",
        "node -e *",
        "python3 -c *",
        "python3 -m *",
        "python3 - *",
        "npm install*",
        "npm install",
        "npx *",
        "sqlite3 *",
        "opencode run*",
        "copilot *",
        "nohup *",
    ]
    for pattern in new_patterns:
        result = install.opencode_bypass_drift(
            {"permission": {"bash": {}}},
            {"permission": {"bash": {pattern: "allow"}}},
        )
        assert result == [pattern], f"{pattern!r} should trigger bypass drift"


def test_opencode_bypass_drift_does_not_catch_unnamed_patterns():
    """The curated set is a snapshot, not a generalized diff — an unnamed
    bypass-shaped pattern (perl -e *, not one of the 15) is not flagged.
    Full-policy compliance is the pytest allowlist-equality test's job."""
    assert (
        install.opencode_bypass_drift(
            {"permission": {"bash": {}}},
            {"permission": {"bash": {"perl -e *": "allow"}}},
        )
        == []
    )


# ── seed policy compliance (allowlist equality) ─────────────────────────────

# Hand-authored from this audit's classification (see
# ~/.claude/data/grill/meta-opencode-seed-bypass-audit-spec.md) — NOT
# derived at runtime from the seed file, since that would make the
# equality assertion below vacuous. Any future legitimate
# permission.bash change (new script, new KEEP) must update this
# literal in the same commit, or the test below goes red.
#
# Three members are known, frozen bypass-shaped gaps, deliberately left
# out of this audit's scope (they predate it): `find *` (-delete/-exec
# run arbitrary commands), `sed -n *` (GNU sed's `e` command executes
# shell), and `env` (`env FOO=bar <command>` runs an arbitrary trailing
# command) — same shape as xargs/awk, just not touched here.
_APPROVED_BASH_PATTERNS = frozenset(
    {
        # 5 named shared workflow scripts
        "python3 ~/.claude/scripts/dev_status.py *",
        "python3 ~/.claude/scripts/grill.py *",
        "python3 ~/.claude/scripts/second_opinion.py *",
        "python3 ~/.claude/scripts/settings_seed_drift_check.py *",
        "python3 ~/.claude/scripts/dotfiles_sync_check.py *",
        # read-only git inspection (6 forms x plain/-C *)
        "git log*",
        "git status*",
        "git diff*",
        "git show*",
        "git ls-files*",
        "git check-ignore*",
        "git -C * log*",
        "git -C * status*",
        "git -C * diff*",
        "git -C * show*",
        "git -C * ls-files*",
        "git -C * check-ignore*",
        # 4 named uv commands
        "uv sync*",
        "uv run pytest*",
        "uv run ruff check*",
        "uv run ruff format*",
        # pre-existing generic read-only-utility tier (never documented
        # in README before this audit)
        "ls*",
        "pwd",
        "which *",
        "head *",
        "head",
        "tail *",
        "tail",
        "wc *",
        "wc",
        "sort *",
        "sort",
        "uniq *",
        "uniq",
        "grep *",
        "rg *",
        "find *",
        "file *",
        "stat *",
        "du *",
        "df *",
        "date*",
        "whoami*",
        "env",
        "printenv*",
        "cat *",
        "cat",
        "sed -n *",
        "strings *",
        "readlink *",
        "jq *",
        "diff *",
        "diff",
        "echo *",
        "echo",
        "systemctl status*",
        "systemctl is-active*",
        "systemctl is-enabled*",
        # newly-recognized read-only system/process inspection
        "lsof *",
        "ps *",
        "pgrep *",
        "ss *",
    }
)


def test_seed_permission_bash_matches_approved_patterns():
    """The repo seed's permission.bash allow-set equals the documented
    policy exactly — this is the unconditional guard against e2363f73's
    failure mode (a bad rule ported straight into the seed), since it
    doesn't depend on any seed/live diff existing."""
    seed = json.loads((REPO_ROOT / "opencode" / "opencode.jsonc").read_text())
    bash = seed["permission"]["bash"]
    allow_keys = {k for k, v in bash.items() if v == "allow"}
    assert allow_keys == _APPROVED_BASH_PATTERNS


def test_seed_permission_bash_catch_all_is_ask():
    """Locks the catch-all's value explicitly — a regression to "deny"
    would pass the equality test above silently (it's excluded from the
    literal either way), so this pins it directly."""
    seed = json.loads((REPO_ROOT / "opencode" / "opencode.jsonc").read_text())
    assert seed["permission"]["bash"]["*"] == "ask"


# ── --reseed ─────────────────────────────────────────────────────────────────


def _raise_oserror(*args, **kwargs):
    raise OSError("simulated failure")


def _stub_clean_git(monkeypatch, *, status=""):
    """Stub the two Git probes used by --adopt."""

    def _run(cmd, *, capture=False, shell=False):
        assert cmd[0] == "git"
        if "ls-files" in cmd:
            return install.CommandResult(True, f"{cmd[-1]}\n")
        if "status" in cmd:
            return install.CommandResult(True, status)
        raise AssertionError(f"unexpected command: {cmd!r}")

    monkeypatch.setattr(install, "run_command", _run)


def _adopt_repo(tmp_path, *, name, content):
    """Create a minimal fake checkout containing one tracked seed."""
    repo = tmp_path / "repo"
    seed = repo / "claude" / name
    seed.parent.mkdir(parents=True)
    seed.write_text(content)
    return repo, seed


def test_adopt_copies_live_claude_text_without_history_or_backup(
    tmp_path, home, monkeypatch
):
    repo, seed = _adopt_repo(
        tmp_path, name="settings.json", content='{"model":"repo"}\n'
    )
    dest = home / ".claude" / "settings.json"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b'{"model":"live"}\r\n')
    seed.chmod(0o640)
    _stub_clean_git(monkeypatch)

    ctx = make_ctx(home, harnesses=("claude",), adopt=True, dotfiles=repo)
    _, drift = install.seed_claude_settings(ctx)

    assert drift == ""
    assert seed.read_bytes() == b'{"model":"live"}\n'
    assert seed.stat().st_mode & 0o777 == 0o640
    assert not ctx.manifest.path.exists()
    assert not (home / ".claude" / "settings.json.bak").exists()


def test_adopt_crlf_only_difference_is_a_noop(tmp_path, home, monkeypatch):
    repo, seed = _adopt_repo(
        tmp_path, name="settings.json", content='{"model":"same"}\n'
    )
    dest = home / ".claude" / "settings.json"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b'{"model":"same"}\r\n')
    _stub_clean_git(monkeypatch)

    ctx = make_ctx(home, harnesses=("claude",), adopt=True, dotfiles=repo)
    _, drift = install.seed_claude_settings(ctx)

    assert drift == ""
    assert seed.read_text() == '{"model":"same"}\n'
    assert not ctx.reporter.skipped


def test_adopt_copies_both_vscode_files_when_wsl_selected(tmp_path, home, monkeypatch):
    repo = tmp_path / "repo"
    vscode = repo / "vscode"
    vscode.mkdir(parents=True)
    (vscode / "settings.json").write_text('{"theme":"repo"}\n')
    (vscode / "keybindings.json").write_text('[{"key":"ctrl+a"}]\n')
    live_dir = home / "windows" / "Code" / "User"
    live_dir.mkdir(parents=True)
    (live_dir / "settings.json").write_text('{"theme":"live"}\n')
    (live_dir / "keybindings.json").write_text('[{"key":"ctrl+a"},{"key":"ctrl+b"}]\n')
    monkeypatch.setattr(install, "_vscode_wsl_user_dir", lambda: live_dir)
    _stub_clean_git(monkeypatch)

    ctx = make_ctx(home, is_wsl=True, adopt=True, dotfiles=repo)
    results = install.seed_vscode_settings(ctx)

    assert all(drift == "" for _, (_, drift) in results)
    assert (vscode / "settings.json").read_text() == '{"theme":"live"}\n'
    assert (vscode / "keybindings.json").read_text() == (
        '[{"key":"ctrl+a"},{"key":"ctrl+b"}]\n'
    )
    assert not ctx.manifest.path.exists()


def test_adopt_refuses_malformed_live_opencode_with_live_path(
    tmp_path, home, monkeypatch, capsys
):
    repo, seed = _adopt_repo(tmp_path, name="unused", content="unused\n")
    seed = repo / "opencode" / "opencode.jsonc"
    seed.parent.mkdir()
    seed.write_text('{"theme":"repo"}\n')
    dest = home / ".config" / "opencode" / "opencode.jsonc"
    dest.parent.mkdir(parents=True)
    dest.write_text('{"theme":}\n')
    _stub_clean_git(monkeypatch)

    ctx = make_ctx(home, harnesses=("opencode",), adopt=True, dotfiles=repo)
    _, drift = install.seed_opencode_config(ctx)
    out = capsys.readouterr().out

    assert drift != ""
    assert "live opencode file ~/.config/opencode/opencode.jsonc" in out
    assert seed.read_text() == '{"theme":"repo"}\n'


def test_adopt_missing_live_file_is_a_silent_noop(tmp_path, home, monkeypatch):
    repo, seed = _adopt_repo(
        tmp_path, name="settings.json", content='{"model":"repo"}\n'
    )
    _stub_clean_git(monkeypatch)

    ctx = make_ctx(home, harnesses=("claude",), adopt=True, dotfiles=repo)
    assert install.seed_claude_settings(ctx) == ("settings.json", "")
    assert not (home / ".claude" / "settings.json").exists()
    assert not ctx.reporter.skipped
    assert seed.read_text() == '{"model":"repo"}\n'


def test_adopt_dry_run_does_not_change_seed(tmp_path, home, monkeypatch, capsys):
    repo, seed = _adopt_repo(
        tmp_path, name="settings.json", content='{"model":"repo"}\n'
    )
    dest = home / ".claude" / "settings.json"
    dest.parent.mkdir(parents=True)
    dest.write_text('{"model":"live"}\n')
    _stub_clean_git(monkeypatch)

    ctx = make_ctx(home, harnesses=("claude",), adopt=True, dry_run=True, dotfiles=repo)
    _, drift = install.seed_claude_settings(ctx)

    assert drift == ""
    assert seed.read_text() == '{"model":"repo"}\n'
    assert "would adopt" in capsys.readouterr().out
    assert not ctx.manifest.path.exists()


def test_adopt_refuses_dirty_repo_seed(tmp_path, home, monkeypatch, capsys):
    repo, seed = _adopt_repo(
        tmp_path, name="settings.json", content='{"model":"repo"}\n'
    )
    dest = home / ".claude" / "settings.json"
    dest.parent.mkdir(parents=True)
    dest.write_text('{"model":"live"}\n')
    _stub_clean_git(monkeypatch, status=" M claude/settings.json\n")

    ctx = make_ctx(home, harnesses=("claude",), adopt=True, dotfiles=repo)
    _, drift = install.seed_claude_settings(ctx)

    assert drift == "model"
    assert seed.read_text() == '{"model":"repo"}\n'
    assert "commit or stash" in capsys.readouterr().out
    assert not ctx.manifest.path.exists()


def test_adopt_refuses_untracked_repo_seed(tmp_path, home, monkeypatch, capsys):
    repo, seed = _adopt_repo(
        tmp_path, name="settings.json", content='{"model":"repo"}\n'
    )
    dest = home / ".claude" / "settings.json"
    dest.parent.mkdir(parents=True)
    dest.write_text('{"model":"live"}\n')

    def _untracked(cmd, *, capture=False, shell=False):
        assert "ls-files" in cmd
        return install.CommandResult(False)

    monkeypatch.setattr(install, "run_command", _untracked)
    ctx = make_ctx(home, harnesses=("claude",), adopt=True, dotfiles=repo)
    _, drift = install.seed_claude_settings(ctx)

    assert drift == "model"
    assert seed.read_text() == '{"model":"repo"}\n'
    assert "untracked" in capsys.readouterr().out


def test_adopt_refuses_symlinked_repo_seed_without_replacing_link(
    tmp_path, home, monkeypatch, capsys
):
    repo, seed = _adopt_repo(
        tmp_path, name="settings.json", content='{"model":"repo"}\n'
    )
    target = repo / "target.json"
    target.write_text('{"model":"target"}\n')
    seed.unlink()
    seed.symlink_to(target)
    dest = home / ".claude" / "settings.json"
    dest.parent.mkdir(parents=True)
    dest.write_text('{"model":"live"}\n')

    ctx = make_ctx(home, harnesses=("claude",), adopt=True, dotfiles=repo)
    _, drift = install.seed_claude_settings(ctx)

    assert drift
    assert seed.is_symlink()
    assert "repo/claude/settings.json" in capsys.readouterr().out


def test_adopt_opencode_blocks_live_bypass(tmp_path, home, monkeypatch, capsys):
    seed_text = '{"permission":{"bash":{}}}\n'
    repo, seed = _adopt_repo(tmp_path, name="unused", content="unused\n")
    seed = repo / "opencode" / "opencode.jsonc"
    seed.parent.mkdir()
    seed.write_text(seed_text)
    dest = home / ".config" / "opencode" / "opencode.jsonc"
    dest.parent.mkdir(parents=True)
    dest.write_text('{"permission":{"bash":{"xargs *":"allow"}}}\n')
    _stub_clean_git(monkeypatch)

    ctx = make_ctx(home, harnesses=("opencode",), adopt=True, dotfiles=repo)
    _, drift = install.seed_opencode_config(ctx)
    out = capsys.readouterr().out

    assert drift.startswith("SECURITY: xargs *")
    assert "SECURITY" in out
    assert "live ~/.config/opencode/opencode.jsonc" in out
    assert seed.read_text() == seed_text


def test_adopt_opencode_repairs_malformed_repo_seed(tmp_path, home, monkeypatch):
    repo, seed = _adopt_repo(tmp_path, name="unused", content="unused\n")
    seed = repo / "opencode" / "opencode.jsonc"
    seed.parent.mkdir()
    seed.write_text("{not json\n")
    dest = home / ".config" / "opencode" / "opencode.jsonc"
    dest.parent.mkdir(parents=True)
    dest.write_text('{"theme":"custom"}\n')
    _stub_clean_git(monkeypatch)

    ctx = make_ctx(home, harnesses=("opencode",), adopt=True, dotfiles=repo)
    _, drift = install.seed_opencode_config(ctx)

    assert drift == ""
    assert seed.read_text() == '{"theme":"custom"}\n'


def test_adopt_opencode_writes_the_validated_snapshot(tmp_path, home, monkeypatch):
    repo, seed = _adopt_repo(tmp_path, name="unused", content="unused\n")
    seed = repo / "opencode" / "opencode.jsonc"
    seed.parent.mkdir()
    seed.write_text('{"theme":"repo"}\n')
    dest = home / ".config" / "opencode" / "opencode.jsonc"
    dest.parent.mkdir(parents=True)
    original = '{"theme":"validated"}\n'
    dest.write_text(original)
    _stub_clean_git(monkeypatch)
    blocker = install._opencode_adopt_blocker

    def mutate_after_validation(ctx, seed_path, dest_path, seed_text, live_text):
        result = blocker(ctx, seed_path, dest_path, seed_text, live_text)
        dest_path.write_text('{"theme":"changed-after-check"}\n')
        return result

    monkeypatch.setattr(install, "_opencode_adopt_blocker", mutate_after_validation)
    ctx = make_ctx(home, harnesses=("opencode",), adopt=True, dotfiles=repo)
    _, drift = install.seed_opencode_config(ctx)

    assert drift == ""
    assert seed.read_text() == original


def test_adopt_empty_live_file_is_reported(tmp_path, home, monkeypatch, capsys):
    repo, _ = _adopt_repo(tmp_path, name="settings.json", content='{"model":"repo"}\n')
    dest = home / ".claude" / "settings.json"
    dest.parent.mkdir(parents=True)
    dest.write_text("")
    _stub_clean_git(monkeypatch)

    ctx = make_ctx(home, harnesses=("claude",), adopt=True, dotfiles=repo)
    _, drift = install.seed_claude_settings(ctx)

    assert drift != ""
    assert "empty" in capsys.readouterr().out
    assert ctx.reporter.skipped


def test_adopt_atomic_failure_preserves_seed(tmp_path, home, monkeypatch, capsys):
    repo, seed = _adopt_repo(
        tmp_path, name="settings.json", content='{"model":"repo"}\n'
    )
    dest = home / ".claude" / "settings.json"
    dest.parent.mkdir(parents=True)
    dest.write_text('{"model":"live"}\n')
    _stub_clean_git(monkeypatch)
    monkeypatch.setattr(install.os, "replace", _raise_oserror)

    ctx = make_ctx(home, harnesses=("claude",), adopt=True, dotfiles=repo)
    _, drift = install.seed_claude_settings(ctx)

    assert drift == "model"
    assert seed.read_text() == '{"model":"repo"}\n'
    assert "atomically write" in capsys.readouterr().out
    assert not list(seed.parent.glob(f".{seed.name}.adopt-*"))


def test_manifest_entries_and_has_backup_with_no_history_file(home):
    """Eval criterion 13: a fresh machine has no history.jsonl yet."""
    ctx = make_ctx(home, harnesses=("claude",), reseed=True)
    assert not ctx.manifest.path.exists()
    assert ctx.manifest.entries() == []
    assert ctx.manifest.has_backup(home / ".claude" / "settings.json") is False


def test_reseed_backs_up_and_overwrites_drifted_file(home):
    """Eval criterion 1."""
    dest = home / ".claude" / "settings.json"
    dest.parent.mkdir(parents=True)
    live = json.loads((REPO_ROOT / "claude" / "settings.json").read_text())
    live["model"] = "drifted-value"
    dest.write_text(json.dumps(live))

    ctx = make_ctx(home, harnesses=("claude",), reseed=True)
    _, drift = install.seed_claude_settings(ctx)

    backup = home / ".claude" / "settings.json.bak"
    assert drift == ""
    assert json.loads(backup.read_text())["model"] == "drifted-value"
    assert dest.read_text() == (REPO_ROOT / "claude" / "settings.json").read_text()
    assert kinds(ctx, "file-backed-up") == [
        {"kind": "file-backed-up", "dest": str(dest), "backup": str(backup)}
    ]
    assert kinds(ctx, "file-copied") == [{"kind": "file-copied", "dest": str(dest)}]


def test_reseed_when_dest_missing_is_a_plain_copy(home):
    """Eval criterion 2: identical to today's behavior, no new code path."""
    ctx = make_ctx(home, harnesses=("claude",), reseed=True)
    _, drift = install.seed_claude_settings(ctx)

    dest = home / ".claude" / "settings.json"
    assert drift == ""
    assert dest.is_file()
    assert not (home / ".claude" / "settings.json.bak").exists()
    assert kinds(ctx, "file-backed-up") == []
    assert kinds(ctx, "file-copied") == [{"kind": "file-copied", "dest": str(dest)}]


def test_reseed_noop_when_drift_is_empty(home):
    """Eval criterion 3: real text drift but no *value* drift is still a no-op."""
    ctx = make_ctx(home, harnesses=("claude",))
    install.seed_claude_settings(ctx)

    dest = home / ".claude" / "settings.json"
    seed_path = REPO_ROOT / "claude" / "settings.json"
    values = json.loads(seed_path.read_text())
    reformatted = json.dumps(values, indent=4, sort_keys=True)
    dest.write_text(reformatted)
    assert dest.read_text() != seed_path.read_text()  # real text drift
    assert install.describe_settings_drift(seed_path, dest) == ""  # no value drift

    reseed_ctx = make_ctx(home, harnesses=("claude",), reseed=True)
    _, drift = install.seed_claude_settings(reseed_ctx)

    assert drift == ""
    assert not (home / ".claude" / "settings.json.bak").exists()
    assert dest.read_text() == reformatted
    assert kinds(reseed_ctx, "file-backed-up") == []
    assert len(kinds(reseed_ctx, "file-copied")) == 1  # only the initial seed


def test_reseed_dry_run_previews_fresh_backup_and_changes_nothing(home, capsys):
    """Eval criterion 4, fresh-backup variant."""
    dest = home / ".claude" / "settings.json"
    dest.parent.mkdir(parents=True)
    dest.write_text(json.dumps({"model": "drifted-value"}))
    before = dest.read_text()

    ctx = make_ctx(home, harnesses=("claude",), reseed=True, dry_run=True)
    _, drift = install.seed_claude_settings(ctx)
    out = capsys.readouterr().out

    assert drift == ""
    assert "would back up" in out
    assert "then reseed from settings.json" in out
    assert dest.read_text() == before
    assert not (home / ".claude" / "settings.json.bak").exists()
    assert not ctx.manifest.path.exists()


def test_reseed_dry_run_previews_already_backed_up(home, capsys):
    """Eval criterion 4, already-backed-up variant."""
    dest = home / ".claude" / "settings.json"
    dest.parent.mkdir(parents=True)
    backup = home / ".claude" / "settings.json.bak"
    dest.write_text(json.dumps({"model": "drifted-value"}))
    backup.write_text("true-original\n")
    make_ctx(home, harnesses=("claude",)).manifest.record_backup(dest, backup)

    ctx = make_ctx(home, harnesses=("claude",), reseed=True, dry_run=True)
    _, drift = install.seed_claude_settings(ctx)
    out = capsys.readouterr().out

    assert drift == ""
    assert "already backed up" in out
    assert backup.read_text() == "true-original\n"
    assert dest.read_text() == json.dumps({"model": "drifted-value"})


def test_reseed_dry_run_previews_foreign_backup_blocks_it(home, capsys):
    """Eval criterion 4, foreign-backup-blocks-it variant."""
    dest = home / ".claude" / "settings.json"
    dest.parent.mkdir(parents=True)
    backup = home / ".claude" / "settings.json.bak"
    dest.write_text(json.dumps({"model": "drifted-value"}))
    backup.write_text("foreign\n")

    ctx = make_ctx(home, harnesses=("claude",), reseed=True, dry_run=True)
    _, drift = install.seed_claude_settings(ctx)
    out = capsys.readouterr().out

    assert drift != ""
    assert "would skip reseeding" in out
    assert "resolve manually" in out
    assert backup.read_text() == "foreign\n"
    assert dest.read_text() == json.dumps({"model": "drifted-value"})


def test_reseed_foreign_unrecorded_backup_blocks_reseed(home, capsys):
    """Eval criterion 9 (real run, not dry-run)."""
    dest = home / ".claude" / "settings.json"
    dest.parent.mkdir(parents=True)
    backup = home / ".claude" / "settings.json.bak"
    dest.write_text(json.dumps({"model": "drifted-value"}))
    backup.write_text("foreign\n")

    ctx = make_ctx(home, harnesses=("claude",), reseed=True)
    _, drift = install.seed_claude_settings(ctx)
    out = capsys.readouterr().out

    assert drift != ""
    assert "SKIPPED" in out
    assert "resolve manually" in out
    assert backup.read_text() == "foreign\n"
    assert dest.read_text() == json.dumps({"model": "drifted-value"})
    assert kinds(ctx, "file-backed-up") == []
    assert kinds(ctx, "file-copied") == []


def test_reseed_backup_move_failure_is_skipped_not_raised(home, monkeypatch, capsys):
    """Eval criterion 12: a move failure never falls through to a copy attempt."""
    dest = home / ".claude" / "settings.json"
    dest.parent.mkdir(parents=True)
    live = json.loads((REPO_ROOT / "claude" / "settings.json").read_text())
    live["model"] = "drifted-value"
    dest.write_text(json.dumps(live))
    monkeypatch.setattr(install.shutil, "move", _raise_oserror)

    ctx = make_ctx(home, harnesses=("claude",), reseed=True)
    _, drift = install.seed_claude_settings(ctx)
    out = capsys.readouterr().out

    assert drift == ""
    assert "SKIPPED" in out
    assert "reseed backup failed" in out
    assert dest.read_text() == json.dumps(live)
    assert kinds(ctx, "file-backed-up") == []
    assert kinds(ctx, "file-copied") == []


def test_reseed_copy_failure_restores_dest_from_backup(home, monkeypatch, capsys):
    """Eval criterion 12: a copy failure after a successful move restores
    dest from backup, rather than leaving the live application with no
    config file at all."""
    dest = home / ".claude" / "settings.json"
    dest.parent.mkdir(parents=True)
    live = json.loads((REPO_ROOT / "claude" / "settings.json").read_text())
    live["model"] = "drifted-value"
    dest.write_text(json.dumps(live))
    monkeypatch.setattr(install.shutil, "copy", _raise_oserror)

    ctx = make_ctx(home, harnesses=("claude",), reseed=True)
    _, drift = install.seed_claude_settings(ctx)
    out = capsys.readouterr().out

    assert drift == ""
    assert "SKIPPED" in out
    assert "copy failed" in out
    assert dest.read_text() == json.dumps(live)
    assert not (home / ".claude" / "settings.json.bak").exists()
    # The backup entry is still recorded (the physical move did succeed);
    # only the copy step failed and its recovery moved the file back.
    assert len(kinds(ctx, "file-backed-up")) == 1
    assert kinds(ctx, "file-copied") == []


def test_reseed_recorded_backup_deleted_from_disk_takes_fresh_backup(home):
    """Eval criterion 15: a recorded-but-deleted .bak is treated as state 1,
    not silently skipped."""
    dest = home / ".claude" / "settings.json"
    dest.parent.mkdir(parents=True)
    stale_backup = home / ".claude" / "settings.json.bak"
    live = json.loads((REPO_ROOT / "claude" / "settings.json").read_text())
    live["model"] = "drifted-value"
    dest.write_text(json.dumps(live))
    # A backup was recorded once, but the .bak file itself no longer exists.
    make_ctx(home, harnesses=("claude",)).manifest.record_backup(dest, stale_backup)

    ctx = make_ctx(home, harnesses=("claude",), reseed=True)
    _, drift = install.seed_claude_settings(ctx)

    assert drift == ""
    assert json.loads(stale_backup.read_text())["model"] == "drifted-value"
    assert dest.read_text() == (REPO_ROOT / "claude" / "settings.json").read_text()
    assert len(kinds(ctx, "file-backed-up")) == 2
    assert len(kinds(ctx, "file-copied")) == 1


def test_two_consecutive_reseeds_then_rollback_restores_true_original(home):
    """Eval criterion 7."""
    dest = home / ".claude" / "settings.json"
    dest.parent.mkdir(parents=True)
    dest.write_text(json.dumps({"model": "true-original"}))

    ctx1 = make_ctx(home, harnesses=("claude",), reseed=True)
    install.seed_claude_settings(ctx1)
    assert dest.read_text() == (REPO_ROOT / "claude" / "settings.json").read_text()

    live = json.loads(dest.read_text())
    live["model"] = "intermediate-drift"
    dest.write_text(json.dumps(live))

    ctx2 = make_ctx(home, harnesses=("claude",), reseed=True)
    install.seed_claude_settings(ctx2)
    assert dest.read_text() == (REPO_ROOT / "claude" / "settings.json").read_text()

    assert len(kinds(ctx2, "file-backed-up")) == 1
    assert len(kinds(ctx2, "file-copied")) == 2

    assert install.do_rollback(make_ctx(home)) == 0
    assert json.loads(dest.read_text()) == {"model": "true-original"}


def test_three_consecutive_reseeds_then_rollback_restores_true_original(home):
    """Eval criterion 8: the invariant generalizes beyond the two-run case."""
    dest = home / ".claude" / "settings.json"
    dest.parent.mkdir(parents=True)
    dest.write_text(json.dumps({"model": "true-original"}))

    for i in range(3):
        ctx = make_ctx(home, harnesses=("claude",), reseed=True)
        install.seed_claude_settings(ctx)
        assert dest.read_text() == (REPO_ROOT / "claude" / "settings.json").read_text()
        live = json.loads(dest.read_text())
        live["model"] = f"intermediate-drift-{i}"
        dest.write_text(json.dumps(live))

    final_ctx = make_ctx(home, harnesses=("claude",), reseed=True)
    install.seed_claude_settings(final_ctx)
    assert len(kinds(final_ctx, "file-backed-up")) == 1
    assert len(kinds(final_ctx, "file-copied")) == 4

    assert install.do_rollback(make_ctx(home)) == 0
    assert json.loads(dest.read_text()) == {"model": "true-original"}


def test_bootstrap_then_single_reseed_then_rollback_restores_true_original(home):
    """Eval criterion 14: the realistic sequence every real seed goes
    through — a plain bootstrap copy, live edits, one --reseed, then
    --rollback — not just the back-to-back-reseeds cases above. Regression
    test for the do_rollback/_rollback_copy double-delete fix: without
    ``restored_dests``, the original bootstrap's file-copied entry deletes
    the just-restored true original a second time."""
    dest = home / ".claude" / "settings.json"

    bootstrap_ctx = make_ctx(home, harnesses=("claude",))
    install.seed_claude_settings(bootstrap_ctx)
    assert kinds(bootstrap_ctx, "file-copied") == [
        {"kind": "file-copied", "dest": str(dest)}
    ]

    live = json.loads(dest.read_text())
    live["model"] = "live-edited-value"
    dest.write_text(json.dumps(live))
    true_original_content = dest.read_text()

    reseed_ctx = make_ctx(home, harnesses=("claude",), reseed=True)
    install.seed_claude_settings(reseed_ctx)
    assert dest.read_text() == (REPO_ROOT / "claude" / "settings.json").read_text()
    assert len(kinds(reseed_ctx, "file-copied")) == 2
    assert len(kinds(reseed_ctx, "file-backed-up")) == 1

    assert install.do_rollback(make_ctx(home)) == 0
    assert dest.read_text() == true_original_content


# ── vscode WSL seeding ───────────────────────────────────────────────────────


def test_seed_vscode_settings_noop_off_wsl(home):
    ctx = make_ctx(home, is_wsl=False)
    assert install.seed_vscode_settings(ctx) == []
    assert not ctx.reporter.skipped


def test_seed_vscode_settings_skips_without_windows_code_cli(home, monkeypatch):
    monkeypatch.setattr(install, "_vscode_wsl_user_dir", lambda: None)
    ctx = make_ctx(home, is_wsl=True)
    assert install.seed_vscode_settings(ctx) == []
    assert ctx.reporter.skipped == [
        "VS Code settings — WSL detected but no Windows-side 'code' CLI found on PATH"
    ]


def test_seed_vscode_settings_fresh_copy_records_manifest(home, monkeypatch):
    vscode_dir = home / "winappdata"
    vscode_dir.mkdir()
    monkeypatch.setattr(install, "_vscode_wsl_user_dir", lambda: vscode_dir)
    ctx = make_ctx(home, is_wsl=True)

    results = install.seed_vscode_settings(ctx)

    names = {name for _, (name, _) in results}
    assert names == {"settings.json", "keybindings.json"}
    for name in ("settings.json", "keybindings.json"):
        dest = vscode_dir / name
        assert dest.is_file() and not dest.is_symlink()
        assert dest.read_text() == (REPO_ROOT / "vscode" / name).read_text()
    assert {e["dest"] for e in kinds(ctx, "file-copied")} == {
        str(vscode_dir / "settings.json"),
        str(vscode_dir / "keybindings.json"),
    }
    assert all(drift == "" for _, (_, drift) in results)


def test_seed_vscode_settings_migrates_stale_symlink(home, monkeypatch):
    vscode_dir = home / "winappdata"
    vscode_dir.mkdir()
    monkeypatch.setattr(install, "_vscode_wsl_user_dir", lambda: vscode_dir)
    ctx = make_ctx(home, is_wsl=True)

    old_target = home / "old_settings.json"
    old_target.write_text('{"old": true}')
    dest = vscode_dir / "settings.json"
    dest.symlink_to(old_target)
    assert dest.is_symlink()

    install.seed_vscode_settings(ctx)

    assert dest.is_file() and not dest.is_symlink()
    assert dest.read_text() == (REPO_ROOT / "vscode" / "settings.json").read_text()


def test_seed_vscode_settings_migration_dry_run_previews_and_changes_nothing(
    home, monkeypatch, capsys
):
    vscode_dir = home / "winappdata"
    vscode_dir.mkdir()
    monkeypatch.setattr(install, "_vscode_wsl_user_dir", lambda: vscode_dir)
    ctx = make_ctx(home, is_wsl=True, dry_run=True)

    old_target = home / "old_settings.json"
    old_target.write_text('{"old": true}')
    dest = vscode_dir / "settings.json"
    dest.symlink_to(old_target)
    # Keep keybindings.json out of the picture — a plain not-yet-seeded
    # file there would legitimately hit seed_file's own "would copy"
    # preview branch, which isn't what this test is about.
    (vscode_dir / "keybindings.json").write_text(
        (REPO_ROOT / "vscode" / "keybindings.json").read_text()
    )

    install.seed_vscode_settings(ctx)

    out = capsys.readouterr().out
    assert "would remove stale WSL symlink" in out
    assert "would copy" not in out
    assert dest.is_symlink()
    assert dest.resolve() == old_target.resolve()


def test_seed_vscode_settings_reports_drift_when_content_differs(home, monkeypatch):
    vscode_dir = home / "winappdata"
    vscode_dir.mkdir()
    monkeypatch.setattr(install, "_vscode_wsl_user_dir", lambda: vscode_dir)
    ctx = make_ctx(home, is_wsl=True)
    install.seed_vscode_settings(ctx)

    dest = vscode_dir / "settings.json"
    dest.write_text(dest.read_text() + "\n// live edit\n")

    results = install.seed_vscode_settings(make_ctx(home, is_wsl=True))
    drift_by_name = {name: drift for _, (name, drift) in results}
    assert drift_by_name["settings.json"] != ""
    assert drift_by_name["keybindings.json"] == ""


def test_describe_vscode_drift_identical_content_is_no_drift(tmp_path):
    seed = tmp_path / "seed.json"
    live = tmp_path / "live.json"
    seed.write_text('{"a": 1}')
    live.write_text('{"a": 1}')
    assert install.describe_vscode_drift(seed, live) == ""


def test_describe_vscode_drift_dict_key_diff(tmp_path):
    seed = tmp_path / "seed.json"
    live = tmp_path / "live.json"
    seed.write_text(json.dumps({"a": 1, "b": 2}))
    live.write_text(json.dumps({"a": 1, "b": 3}))
    assert install.describe_vscode_drift(seed, live) == "b"


def test_describe_vscode_drift_list_length_diff(tmp_path):
    seed = tmp_path / "seed.json"
    live = tmp_path / "live.json"
    seed.write_text(json.dumps([{"key": "a"}]))
    live.write_text(json.dumps([{"key": "a"}, {"key": "b"}]))
    assert install.describe_vscode_drift(seed, live) == "2 bindings live vs 1 in seed"


def test_describe_vscode_drift_list_same_length_content_differs(tmp_path):
    seed = tmp_path / "seed.json"
    live = tmp_path / "live.json"
    seed.write_text(json.dumps([{"key": "a"}]))
    live.write_text(json.dumps([{"key": "b"}]))
    assert (
        install.describe_vscode_drift(seed, live)
        == "binding definitions differ (1 bindings)"
    )


def test_describe_vscode_drift_jsonc_comment_still_reports_generic_fallback(tmp_path):
    seed = tmp_path / "seed.json"
    live = tmp_path / "live.json"
    seed.write_text(json.dumps({"a": 1}))
    live.write_text('// a comment\n{"a": 1, "b": 2}')
    drift = install.describe_vscode_drift(seed, live)
    assert drift == "content differs from the repo copy"


def test_describe_vscode_drift_missing_file_is_nothing_to_compare(tmp_path):
    seed = tmp_path / "seed.json"
    live = tmp_path / "live.json"
    seed.write_text('{"a": 1}')
    assert install.describe_vscode_drift(seed, live) == ""


# ── settings/opencode drift: text-first check + corrupted-JSON fix ─────────────


def test_describe_settings_drift_corrupted_json_now_reports_non_empty(tmp_path):
    """Eval criterion 10: unparseable live JSON used to be invisible to drift."""
    seed = tmp_path / "seed.json"
    live = tmp_path / "live.json"
    seed.write_text(json.dumps({"a": 1}))
    live.write_text("{not valid json")
    drift = install.describe_settings_drift(seed, live)
    assert drift != ""
    assert "unreadable or invalid JSON" in drift


def test_describe_opencode_drift_corrupted_json_now_reports_non_empty(tmp_path):
    seed = tmp_path / "seed.json"
    live = tmp_path / "live.json"
    seed.write_text(json.dumps({"a": 1}))
    live.write_text("{not valid json")
    drift = install.describe_opencode_drift(seed, live)
    assert drift != ""
    assert "unreadable or invalid JSON" in drift


def test_describe_opencode_drift_identical_jsonc_with_comment_is_no_drift(tmp_path):
    """Eval criterion 11: the text-equality check must fire before JSON
    parsing, so a byte-identical commented JSONC file is never misreported
    as corrupted/drifted just because json.loads can't parse the comment."""
    seed = tmp_path / "seed.jsonc"
    live = tmp_path / "live.jsonc"
    content = '// a comment\n{"a": 1}'
    seed.write_text(content)
    live.write_text(content)
    assert install.describe_opencode_drift(seed, live) == ""


def test_describe_settings_drift_identical_is_no_drift(tmp_path):
    seed = tmp_path / "seed.json"
    live = tmp_path / "live.json"
    seed.write_text(json.dumps({"a": 1}))
    live.write_text(json.dumps({"a": 1}))
    assert install.describe_settings_drift(seed, live) == ""


# ── history + rollback ────────────────────────────────────────────────────────


def test_history_accumulates_across_runs(home, links, offline_install):
    ctx_a = make_ctx(home, harnesses=("claude",))
    install.run_install(ctx_a, links)
    ctx_b = make_ctx(home, harnesses=("opencode",))
    install.run_install(ctx_b, links)

    entries = history(ctx_b)
    assert sum(1 for e in entries if e["kind"] == "run") == 2
    dests = {e.get("dest") for e in entries}
    assert str(home / ".claude" / "CLAUDE.md") in dests
    assert str(home / ".config" / "opencode" / "opencode.jsonc") in dests


def test_rollback_undoes_every_past_run(home, links, offline_install):
    install.run_install(make_ctx(home, harnesses=("claude",)), links)
    install.run_install(make_ctx(home, harnesses=("opencode",)), links)

    assert (home / ".claude" / "CLAUDE.md").is_symlink()
    assert (home / ".config" / "opencode" / "opencode.jsonc").is_file()

    rollback = make_ctx(home)
    assert install.do_rollback(rollback) == 0

    # Run A's files, not just run B's.
    assert not (home / ".claude" / "CLAUDE.md").exists()
    assert not (home / ".vimrc").exists()
    assert not (home / ".config" / "opencode" / "opencode.jsonc").exists()
    assert not rollback.manifest.path.exists()


def test_rollback_restores_backed_up_file(home, links, offline_install):
    (home / ".vimrc").write_text("sentinel-content\n")
    install.run_install(make_ctx(home, harnesses=("claude",)), links)
    assert (home / ".vimrc").is_symlink()

    assert install.do_rollback(make_ctx(home)) == 0

    assert (home / ".vimrc").read_text() == "sentinel-content\n"
    assert not (home / ".vimrc.bak").exists()


def test_rollback_leaves_a_reclaimed_symlink_alone(
    home, links, offline_install, capsys
):
    install.run_install(make_ctx(home, harnesses=("claude",)), links)

    claimed = home / ".claude" / "CLAUDE.md"
    claimed.unlink()
    claimed.symlink_to("/etc/hostname")

    code = install.do_rollback(make_ctx(home))
    out = capsys.readouterr().out

    assert code == 1
    assert claimed.readlink() == Path("/etc/hostname")
    assert "something else has claimed this path" in out
    assert "rollback step(s) did not apply cleanly" in out


def test_rollback_reports_a_missing_backup(home, links, offline_install, capsys):
    (home / ".vimrc").write_text("sentinel-content\n")
    install.run_install(make_ctx(home, harnesses=("claude",)), links)
    (home / ".vimrc.bak").unlink()

    code = install.do_rollback(make_ctx(home))
    out = capsys.readouterr().out

    assert code == 1
    assert "not found — already restored, or removed outside install.sh" in out


def test_duplicate_backup_entry_is_not_reported_twice(home, capsys):
    """A repeat backup/rollback/reinstall cycle leaves two entries for one path.

    The second (older) one is already satisfied by the first restore, so it
    must not be reported as a missing-backup anomaly.
    """
    ctx = make_ctx(home)
    dest = home / ".vimrc"
    backup = home / ".vimrc.bak"
    dest.write_text("original\n")
    dest.rename(backup)
    ctx.manifest.record_backup(dest, backup)
    ctx.manifest.record_backup(dest, backup)

    code = install.do_rollback(make_ctx(home))
    out = capsys.readouterr().out

    assert code == 0
    assert dest.read_text() == "original\n"
    assert "SKIPPED" not in out


def test_rollback_never_uninstalls_packages(home, capsys):
    ctx = make_ctx(home)
    ctx.manifest.init_run("personal")
    ctx.manifest.record_package("ripgrep")

    assert install.do_rollback(make_ctx(home)) == 0
    assert "package left installed (profile-independent): ripgrep" in (
        capsys.readouterr().out
    )


def test_rollback_dry_run_changes_nothing(home, links, offline_install, capsys):
    install.run_install(make_ctx(home, harnesses=("claude",)), links)

    preview = make_ctx(home, dry_run=True)
    assert install.do_rollback(preview) == 0
    out = capsys.readouterr().out

    assert "Dry run complete" in out
    assert (home / ".claude" / "CLAUDE.md").is_symlink()
    assert preview.manifest.path.is_file()


def test_rollback_without_history_exits_1(home, capsys):
    assert install.do_rollback(make_ctx(home)) == 1
    assert "nothing to roll back" in capsys.readouterr().err


# ── --rollback --wipe ────────────────────────────────────────────────────────


def _disable_systemctl(monkeypatch):
    """Make the watchcommit-wipe gate see systemd --user as unavailable.

    Only safe for tests with no unit symlink on disk at all (the no-manifest
    cases below, which never call ``run_install``): ``_wipe_service``
    checks ``unit_path.is_symlink()`` before ever calling ``have()``, so
    with no symlink this never fires and never touches ``run_command``.
    Tests that *do* run a real install first (``links.toml`` records real
    watchcommit.service/opencode-skills-sync.service symlinks for
    personal+linux, and ``offline_install`` only stubs the install-time
    ``enable_managed_services`` call, not the symlinks themselves) must use
    ``_watchcommit_available`` instead — forcing
    systemd "unavailable" against a real unit symlink is a genuine anomaly
    (SKIPPED, exit 1) by design, not a quiet no-op, so it would wrongly
    fail unrelated assertions about backup deletion or nvim-dir sweeping.
    """
    monkeypatch.setattr(install, "have", lambda exe: exe != "systemctl")


def _watchcommit_available(monkeypatch, *, probe_ok=True, disable_ok=True):
    """Stub systemd as available and record the calls _wipe_managed_services makes.

    Recognizes a disable call for any unit in ``install.MANAGED_SERVICES``
    (not just watchcommit's) — stays strict otherwise: a disable call naming
    an unrecognized unit still raises, same as any other unexpected command.
    """
    calls: list[list[str]] = []
    units = {service.unit for service in install.MANAGED_SERVICES}

    def _stub(cmd, *, shell=False, capture=False):
        argv = list(cmd)
        calls.append(argv)
        if argv == ["systemctl", "--user", "show-environment"]:
            return install.CommandResult(probe_ok)
        if (
            len(argv) == 5
            and argv[:4] == ["systemctl", "--user", "disable", "--now"]
            and argv[4] in units
        ):
            return install.CommandResult(disable_ok)
        raise AssertionError(f"unexpected command: {argv!r}")

    monkeypatch.setattr(install, "have", lambda exe: exe == "systemctl")
    monkeypatch.setattr(install, "run_command", _stub)
    return calls


def test_wipe_deletes_backup_instead_of_restoring(
    home, links, offline_install, monkeypatch
):
    (home / ".vimrc").write_text("sentinel-content\n")
    install.run_install(make_ctx(home, harnesses=("claude",)), links)
    assert (home / ".vimrc").is_symlink()

    _watchcommit_available(monkeypatch)
    assert install.do_rollback(make_ctx(home, wipe=True)) == 0

    assert not (home / ".vimrc").exists()
    assert not (home / ".vimrc.bak").exists()


def test_wipe_dry_run_does_not_delete_backup(home, links, offline_install, monkeypatch):
    (home / ".vimrc").write_text("sentinel-content\n")
    install.run_install(make_ctx(home, harnesses=("claude",)), links)

    _watchcommit_available(monkeypatch)
    assert install.do_rollback(make_ctx(home, wipe=True, dry_run=True)) == 0

    assert (home / ".vimrc").is_symlink()
    assert (home / ".vimrc.bak").is_file()


def test_wipe_missing_backup_reports_wipe_wording(
    home, links, offline_install, capsys, monkeypatch
):
    (home / ".vimrc").write_text("sentinel-content\n")
    install.run_install(make_ctx(home, harnesses=("claude",)), links)
    (home / ".vimrc.bak").unlink()

    _watchcommit_available(monkeypatch)
    code = install.do_rollback(make_ctx(home, wipe=True))
    out = capsys.readouterr().out

    assert code == 1
    assert "not found — already wiped, or removed outside install.sh" in out
    assert "already restored" not in out


def test_wipe_sweeps_nvim_dirs(home, links, offline_install, monkeypatch):
    install.run_install(make_ctx(home, harnesses=("claude",)), links)
    nvim_dirs = [
        home / ".local" / "share" / "nvim",
        home / ".local" / "state" / "nvim",
        home / ".cache" / "nvim",
    ]
    for path in nvim_dirs:
        path.mkdir(parents=True)
        (path / "sentinel").write_text("x\n")

    _watchcommit_available(monkeypatch)
    assert install.do_rollback(make_ctx(home, wipe=True)) == 0

    for path in nvim_dirs:
        assert not path.exists()


def test_rollback_without_wipe_leaves_nvim_dirs_alone(home, links, offline_install):
    install.run_install(make_ctx(home, harnesses=("claude",)), links)
    nvim_dir = home / ".local" / "share" / "nvim"
    nvim_dir.mkdir(parents=True)
    (nvim_dir / "sentinel").write_text("x\n")

    assert install.do_rollback(make_ctx(home)) == 0

    assert nvim_dir.is_dir()


def test_wipe_dry_run_leaves_nvim_dirs_alone(home, links, offline_install, monkeypatch):
    install.run_install(make_ctx(home, harnesses=("claude",)), links)
    nvim_dir = home / ".local" / "share" / "nvim"
    nvim_dir.mkdir(parents=True)
    (nvim_dir / "sentinel").write_text("x\n")

    _watchcommit_available(monkeypatch)
    assert install.do_rollback(make_ctx(home, wipe=True, dry_run=True)) == 0

    assert nvim_dir.is_dir()
    assert (nvim_dir / "sentinel").is_file()


def test_wipe_disables_watchcommit_service(home, links, offline_install, monkeypatch):
    install.run_install(make_ctx(home, harnesses=("claude",)), links)
    unit = home / ".config" / "systemd" / "user" / "watchcommit.service"
    assert unit.is_symlink()

    calls = _watchcommit_available(monkeypatch)

    assert install.do_rollback(make_ctx(home, wipe=True)) == 0
    assert ["systemctl", "--user", "disable", "--now", "watchcommit.service"] in calls


def test_wipe_disables_opencode_skills_sync_service(
    home, links, offline_install, monkeypatch
):
    install.run_install(make_ctx(home, harnesses=("claude",)), links)
    unit = home / ".config" / "systemd" / "user" / "opencode-skills-sync.service"
    assert unit.is_symlink()

    calls = _watchcommit_available(monkeypatch)

    assert install.do_rollback(make_ctx(home, wipe=True)) == 0
    assert [
        "systemctl",
        "--user",
        "disable",
        "--now",
        "opencode-skills-sync.service",
    ] in calls


def test_wipe_dry_run_previews_watchcommit_disable(
    home, links, offline_install, monkeypatch, capsys
):
    install.run_install(make_ctx(home, harnesses=("claude",)), links)

    calls = _watchcommit_available(monkeypatch)

    assert install.do_rollback(make_ctx(home, wipe=True, dry_run=True)) == 0
    out = capsys.readouterr().out

    assert "would disable+stop the watchcommit systemd user service" in out
    assert [
        "systemctl",
        "--user",
        "disable",
        "--now",
        "watchcommit.service",
    ] not in calls


def test_plain_rollback_does_not_touch_watchcommit(
    home, links, offline_install, monkeypatch
):
    install.run_install(make_ctx(home, harnesses=("claude",)), links)

    calls = _watchcommit_available(monkeypatch)

    assert install.do_rollback(make_ctx(home)) == 0
    assert calls == []


def test_wipe_watchcommit_noop_when_never_installed(
    home, links, offline_install, monkeypatch
):
    install.run_install(make_ctx(home, harnesses=("claude",), profile="work"), links)
    unit = home / ".config" / "systemd" / "user" / "watchcommit.service"
    assert not unit.exists()

    calls = _watchcommit_available(monkeypatch)

    assert install.do_rollback(make_ctx(home, wipe=True, profile="work")) == 0
    assert calls == []


def test_wipe_watchcommit_anomaly_when_systemd_unavailable(
    home, links, offline_install, monkeypatch, capsys
):
    install.run_install(make_ctx(home, harnesses=("claude",)), links)
    unit = home / ".config" / "systemd" / "user" / "watchcommit.service"
    assert unit.is_symlink()

    _watchcommit_available(monkeypatch, probe_ok=False)

    code = install.do_rollback(make_ctx(home, wipe=True))
    out = capsys.readouterr().out

    assert code == 1
    assert "SKIPPED" in out


def test_wipe_watchcommit_anomaly_counts_as_swept_with_no_manifest(
    home, monkeypatch, capsys
):
    """A probe failure is a real anomaly, not a silent no-op, even with
    nothing else to roll back — it must still push the exit code to 1
    instead of falling through to the plain "nothing to roll back" error."""
    unit = home / ".config" / "systemd" / "user" / "watchcommit.service"
    unit.parent.mkdir(parents=True)
    fake_src = home / ".fake-unit-source"
    fake_src.write_text("x\n")
    unit.symlink_to(fake_src)

    _watchcommit_available(monkeypatch, probe_ok=False)

    code = install.do_rollback(make_ctx(home, wipe=True))
    captured = capsys.readouterr()

    assert code == 1
    assert "SKIPPED" in captured.out
    assert "nothing to roll back" not in captured.out
    assert "nothing to roll back" not in captured.err


def test_wipe_no_manifest_sweeps_nvim_dirs(home, monkeypatch):
    nvim_dir = home / ".local" / "share" / "nvim"
    nvim_dir.mkdir(parents=True)
    (nvim_dir / "sentinel").write_text("x\n")

    _disable_systemctl(monkeypatch)
    code = install.do_rollback(make_ctx(home, wipe=True))

    assert code == 0
    assert not nvim_dir.exists()


def test_wipe_no_manifest_dry_run_sweeps_preview(home, monkeypatch, capsys):
    nvim_dir = home / ".local" / "share" / "nvim"
    nvim_dir.mkdir(parents=True)
    (nvim_dir / "sentinel").write_text("x\n")

    _disable_systemctl(monkeypatch)
    code = install.do_rollback(make_ctx(home, wipe=True, dry_run=True))
    out = capsys.readouterr().out

    assert code == 0
    assert nvim_dir.is_dir()
    assert "would remove" in out
    assert "nothing to roll back" not in out


def test_wipe_no_manifest_and_nothing_to_sweep_still_errors(home, monkeypatch, capsys):
    _disable_systemctl(monkeypatch)
    code = install.do_rollback(make_ctx(home, wipe=True))
    err = capsys.readouterr().err

    assert code == 1
    assert "nothing to roll back" in err


def test_wipe_no_manifest_dry_run_and_nothing_to_sweep_still_errors(
    home, monkeypatch, capsys
):
    _disable_systemctl(monkeypatch)
    code = install.do_rollback(make_ctx(home, wipe=True, dry_run=True))
    err = capsys.readouterr().err

    assert code == 1
    assert "nothing to roll back" in err


def test_wipe_removes_empty_state_dir(home, links, offline_install, monkeypatch):
    install.run_install(make_ctx(home, harnesses=("claude",)), links)
    state_dir = home / ".local" / "state" / "dotfiles"
    assert state_dir.is_dir()

    _watchcommit_available(monkeypatch)
    assert install.do_rollback(make_ctx(home, wipe=True)) == 0

    assert not state_dir.exists()


def test_wipe_dry_run_previews_empty_state_dir_removal(
    home, links, offline_install, monkeypatch, capsys
):
    install.run_install(make_ctx(home, harnesses=("claude",)), links)
    state_dir = home / ".local" / "state" / "dotfiles"

    _watchcommit_available(monkeypatch)
    assert install.do_rollback(make_ctx(home, wipe=True, dry_run=True)) == 0
    out = capsys.readouterr().out

    assert state_dir.is_dir()
    assert f"would remove empty state directory {state_dir}" in out


def test_plain_rollback_leaves_state_dir_even_if_empty(home, links, offline_install):
    install.run_install(make_ctx(home, harnesses=("claude",)), links)
    state_dir = home / ".local" / "state" / "dotfiles"

    assert install.do_rollback(make_ctx(home)) == 0

    assert state_dir.is_dir()


def test_history_survives_a_malformed_line(home):
    ctx = make_ctx(home)
    ctx.manifest.init_run("personal")
    with open(ctx.manifest.path, "a", encoding="utf-8") as handle:
        handle.write("not json at all\n")
    ctx.manifest.record_package("tmux")

    entries = ctx.manifest.entries()
    assert [e["kind"] for e in entries] == ["run", "package-installed"]


# ── work-profile guard and marker ─────────────────────────────────────────────


def test_work_run_writes_marker_and_rollback_resets_it(home, links, offline_install):
    ctx = make_ctx(home, harnesses=("claude",), profile="work")
    install.run_install(ctx, links)

    assert ctx.profile_marker.read_text().strip() == "work"
    assert install.work_guard_blocks(make_ctx(home, profile="personal"))
    assert not install.work_guard_blocks(make_ctx(home, profile="personal", force=True))
    assert not install.work_guard_blocks(make_ctx(home, profile="work"))

    install.do_rollback(make_ctx(home))
    assert not ctx.profile_marker.exists()
    assert not install.work_guard_blocks(make_ctx(home, profile="personal"))


def test_guard_is_inert_without_a_marker(home):
    assert not install.work_guard_blocks(make_ctx(home, profile="personal"))


def test_marker_not_written_on_personal_run(home, links, offline_install):
    ctx = make_ctx(home, harnesses=("claude",))
    install.run_install(ctx, links)
    assert not ctx.profile_marker.exists()


# ── whole-run behavior ────────────────────────────────────────────────────────


def test_full_run_wires_only_the_selected_harness(home, links, offline_install):
    ctx = make_ctx(home, harnesses=("claude",))
    assert install.run_install(ctx, links) == 0

    assert (home / ".claude" / "CLAUDE.md").is_symlink()
    assert (home / ".claude" / "scripts" / "dev_status.py").is_symlink()
    assert not (home / ".copilot" / "copilot-instructions.md").exists()
    assert not (home / ".config" / "opencode" / "opencode.jsonc").exists()
    assert not (home / ".gemini" / "GEMINI.md").exists()


def test_full_run_is_additive_across_narrowing_selections(home, links, offline_install):
    install.run_install(make_ctx(home, harnesses=("claude", "copilot")), links)
    install.run_install(make_ctx(home, harnesses=("claude",)), links)

    # Narrowing --harness on a later run never uninstalls what came before.
    assert (home / ".copilot" / "copilot-instructions.md").is_symlink()


def test_full_dry_run_touches_nothing(home, links, offline_install):
    ctx = make_ctx(home, harnesses=("claude", "opencode"), dry_run=True)
    install.run_install(ctx, links)

    assert not (home / ".vimrc").exists()
    assert not (home / ".claude").exists()
    assert not ctx.manifest.path.exists()


def test_exit_status_is_1_when_a_step_was_skipped(home, links, offline_install):
    ctx = make_ctx(home, harnesses=("claude",))
    ctx.reporter.skip("something", "because")
    assert install.run_install(ctx, links) == 1


# ── small helpers ─────────────────────────────────────────────────────────────


def test_parse_neovim_version():
    assert install.parse_neovim_version("NVIM v0.11.2\nBuild type: Release") == (0, 11)
    assert install.parse_neovim_version("NVIM v0.9.5") == (0, 9)
    assert install.parse_neovim_version("nvim: command not found") is None
    assert install.parse_neovim_version("") is None


def _stub_nvim(monkeypatch, *, version_output="NVIM v0.12.4", runtime_ok=True):
    """Stub nvim --version and the headless runtime probe."""

    def _stub(cmd, *, shell=False, capture=False):
        argv = list(cmd)
        if argv == ["nvim", "--version"]:
            return install.CommandResult(True, version_output)
        if argv[:2] == ["nvim", "--headless"]:
            return install.CommandResult(runtime_ok)
        raise AssertionError(f"unexpected command: {argv!r}")

    monkeypatch.setattr(install, "have", lambda exe: exe == "nvim")
    monkeypatch.setattr(install, "run_command", _stub)


def test_neovim_runtime_ok_reflects_probe(monkeypatch):
    _stub_nvim(monkeypatch, runtime_ok=True)
    assert install.neovim_runtime_ok() is True


def test_neovim_runtime_ok_reflects_probe_failure(monkeypatch):
    _stub_nvim(monkeypatch, runtime_ok=False)
    assert install.neovim_runtime_ok() is False


def test_neovim_status_missing_binary(monkeypatch):
    monkeypatch.setattr(install, "have", lambda exe: False)
    assert install._neovim_status() == (None, False)


def test_neovim_status_unparseable_version_skips_runtime_probe(monkeypatch):
    calls = []

    def _stub(cmd, *, shell=False, capture=False):
        calls.append(list(cmd))
        return install.CommandResult(True, "nvim: command not found")

    monkeypatch.setattr(install, "have", lambda exe: True)
    monkeypatch.setattr(install, "run_command", _stub)
    assert install._neovim_status() == (None, False)
    assert calls == [["nvim", "--version"]]


def test_neovim_status_ok(monkeypatch):
    _stub_nvim(monkeypatch, version_output="NVIM v0.12.4", runtime_ok=True)
    assert install._neovim_status() == ((0, 12), True)


def test_neovim_status_broken_runtime(monkeypatch):
    _stub_nvim(monkeypatch, version_output="NVIM v0.12.4", runtime_ok=False)
    assert install._neovim_status() == ((0, 12), False)


def test_bootstrap_neovim_skips_when_not_installed(home, monkeypatch, capsys):
    ctx = make_ctx(home)
    monkeypatch.setattr(install, "have", lambda exe: False)
    install.bootstrap_neovim(ctx)
    assert "Neovim not installed" in capsys.readouterr().out


def test_bootstrap_neovim_skips_when_version_too_old(home, monkeypatch, capsys):
    ctx = make_ctx(home)
    _stub_nvim(monkeypatch, version_output="NVIM v0.9.5")
    install.bootstrap_neovim(ctx)
    out = capsys.readouterr().out
    assert "Neovim 0.9 found, config needs >=0.11" in out


def test_bootstrap_neovim_old_version_includes_fallback_failure(
    home, monkeypatch, capsys
):
    ctx = make_ctx(home)
    ctx.neovim_fallback_failure = (
        "download/extract failed, or archive layout unexpected"
    )
    _stub_nvim(monkeypatch, version_output="NVIM v0.9.5")
    install.bootstrap_neovim(ctx)
    out = capsys.readouterr().out
    assert "fallback install also failed" in out
    assert "download/extract failed, or archive layout unexpected" in out


def test_bootstrap_neovim_skips_when_runtime_broken(home, monkeypatch, capsys):
    ctx = make_ctx(home)
    _stub_nvim(monkeypatch, version_output="NVIM v0.12.4", runtime_ok=False)
    install.bootstrap_neovim(ctx)
    out = capsys.readouterr().out
    assert "runtime doesn't resolve" in out
    assert "vim.uri" in out


def test_bootstrap_neovim_proceeds_when_runtime_ok(home, monkeypatch, capsys):
    ctx = make_ctx(home, dry_run=True)
    _stub_nvim(monkeypatch, version_output="NVIM v0.12.4", runtime_ok=True)
    install.bootstrap_neovim(ctx)
    out = capsys.readouterr().out
    assert "would run: nvim --headless" in out
    assert "SKIPPED" not in out


# ── neovim fallback install (Linux) ────────────────────────────────────────────


def _stub_neovim_fallback(
    monkeypatch,
    *,
    have_nvim=False,
    version_output="",
    runtime_ok=False,
    download_ok=True,
    extract_ok=True,
    asset="nvim-linux-x86_64.tar.gz",
    machine="x86_64",
):
    """Stub the whole external surface _install_neovim_fallback touches.

    The ``tar`` stub fabricates the extracted directory tree a real
    extraction would produce (bin/nvim, share/nvim/runtime), since nothing
    here actually shells out.
    """
    calls: list[list[str]] = []

    def _stub(cmd, *, shell=False, capture=False):
        argv = list(cmd)
        calls.append(argv)
        if argv == ["nvim", "--version"]:
            return install.CommandResult(True, version_output)
        if argv[:2] == ["nvim", "--headless"]:
            return install.CommandResult(runtime_ok)
        if argv[0] == "curl":
            return install.CommandResult(download_ok)
        if argv[0] == "tar":
            if extract_ok:
                tmp_dir = Path(argv[-1])
                extracted = tmp_dir / asset.removesuffix(".tar.gz")
                (extracted / "bin").mkdir(parents=True)
                (extracted / "bin" / "nvim").write_text("fake nvim binary\n")
                (extracted / "share" / "nvim" / "runtime").mkdir(parents=True)
            return install.CommandResult(extract_ok)
        raise AssertionError(f"unexpected command: {argv!r}")

    monkeypatch.setattr(install, "have", lambda exe: exe == "nvim" and have_nvim)
    monkeypatch.setattr(install, "run_command", _stub)
    monkeypatch.setattr(install.platform, "machine", lambda: machine)
    return calls


def test_install_neovim_fallback_no_pin_noop_when_already_good(home, monkeypatch):
    """--no-nvim-pin restores the old rescue-only behavior: a system nvim
    that's already good enough (>=0.11, working runtime) is left alone,
    even though it isn't our managed pin."""
    ctx = make_ctx(home, no_nvim_pin=True)
    calls = _stub_neovim_fallback(
        monkeypatch, have_nvim=True, version_output="NVIM v0.11.6", runtime_ok=True
    )
    install._install_neovim_fallback(ctx)
    assert kinds(ctx, "package-installed") == []
    assert kinds(ctx, "symlink-created") == []
    assert not any(argv[0] in ("curl", "tar") for argv in calls)


def test_install_neovim_fallback_pins_by_default_even_when_system_nvim_good(
    home, monkeypatch
):
    """Default behavior (no --no-nvim-pin): a system nvim that's already
    good enough still gets overridden by the pinned build, since it isn't
    our managed symlink into ~/.local/opt/neovim."""
    ctx = make_ctx(home)
    _stub_neovim_fallback(
        monkeypatch, have_nvim=True, version_output="NVIM v0.11.6", runtime_ok=True
    )
    install._install_neovim_fallback(ctx)

    prefix = home / ".local" / "opt" / "neovim"
    shim = home / ".local" / "bin" / "nvim"
    assert (prefix / "bin" / "nvim").read_text() == "fake nvim binary\n"
    assert shim.is_symlink()

    packages = kinds(ctx, "package-installed")
    assert len(packages) == 1
    assert install.NEOVIM_FALLBACK_VERSION in packages[0]["name"]


def test_install_neovim_fallback_noop_when_already_pinned(home, monkeypatch):
    """Idempotent: once ~/.local/bin/nvim already resolves to our own
    correctly-pinned install, re-running does nothing — no network calls,
    no reinstall — under default (pin-enforcing) behavior."""
    ctx = make_ctx(home)
    prefix = home / ".local" / "opt" / "neovim"
    shim = home / ".local" / "bin" / "nvim"
    nvim_bin = prefix / "bin" / "nvim"
    nvim_bin.parent.mkdir(parents=True)
    nvim_bin.write_text("real fallback binary\n")
    shim.parent.mkdir(parents=True)
    shim.symlink_to(nvim_bin)

    calls: list[list[str]] = []

    def _stub(cmd, *, shell=False, capture=False):
        argv = list(cmd)
        calls.append(argv)
        if argv == [str(shim), "--version"]:
            return install.CommandResult(
                True, f"NVIM v{install.NEOVIM_FALLBACK_VERSION}"
            )
        raise AssertionError(f"unexpected command: {argv!r}")

    monkeypatch.setattr(install, "run_command", _stub)
    install._install_neovim_fallback(ctx)

    assert calls == [[str(shim), "--version"]]
    assert kinds(ctx, "package-installed") == []
    assert kinds(ctx, "symlink-created") == []


def test_install_neovim_fallback_no_pin_still_rescues_when_broken(home, monkeypatch):
    """--no-nvim-pin doesn't suppress genuine rescues: a missing/broken
    system nvim still gets the fallback installed."""
    ctx = make_ctx(home, no_nvim_pin=True)
    _stub_neovim_fallback(
        monkeypatch, have_nvim=True, version_output="NVIM v0.9.5", runtime_ok=False
    )
    install._install_neovim_fallback(ctx)

    packages = kinds(ctx, "package-installed")
    assert len(packages) == 1
    assert install.NEOVIM_FALLBACK_VERSION in packages[0]["name"]


def test_install_neovim_fallback_installs_when_too_old(home, monkeypatch):
    ctx = make_ctx(home)
    _stub_neovim_fallback(
        monkeypatch, have_nvim=True, version_output="NVIM v0.9.5", runtime_ok=False
    )
    install._install_neovim_fallback(ctx)

    prefix = home / ".local" / "opt" / "neovim"
    shim = home / ".local" / "bin" / "nvim"
    assert (prefix / "bin" / "nvim").read_text() == "fake nvim binary\n"
    assert shim.is_symlink()
    assert shim.resolve() == (prefix / "bin" / "nvim").resolve()

    packages = kinds(ctx, "package-installed")
    assert len(packages) == 1
    assert install.NEOVIM_FALLBACK_VERSION in packages[0]["name"]

    symlinks = kinds(ctx, "symlink-created")
    assert len(symlinks) == 1
    assert symlinks[0]["dest"] == str(shim)


def test_install_neovim_fallback_download_failure_skips(home, monkeypatch, capsys):
    ctx = make_ctx(home)
    _stub_neovim_fallback(monkeypatch, have_nvim=False, download_ok=False)
    install._install_neovim_fallback(ctx)

    out = capsys.readouterr().out
    assert "download/extract failed" in out
    assert ctx.neovim_fallback_failure is not None
    assert kinds(ctx, "package-installed") == []
    assert kinds(ctx, "symlink-created") == []
    assert not (home / ".local" / "opt" / "neovim").exists()


def test_install_neovim_fallback_unsupported_arch_skips(home, monkeypatch, capsys):
    ctx = make_ctx(home)
    calls = _stub_neovim_fallback(monkeypatch, have_nvim=False, machine="riscv64")
    install._install_neovim_fallback(ctx)

    out = capsys.readouterr().out
    assert "unsupported architecture" in out
    assert "riscv64" in out
    assert ctx.neovim_fallback_failure is not None
    assert calls == []


def test_install_neovim_fallback_replaces_stale_prefix(home, monkeypatch):
    """An unrecorded stale prefix self-heals: TREE_UNRECORDED still proceeds
    (see meta-guard-nvim-fallback-clobber's asymmetric verdict design)."""
    ctx = make_ctx(home)
    ctx.departure_baseline = depart.Baseline()
    prefix = home / ".local" / "opt" / "neovim"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "bin" / "nvim").write_text("stale old binary\n")
    (prefix / "STALE_MARKER").write_text("x\n")

    _stub_neovim_fallback(monkeypatch, have_nvim=False)
    install._install_neovim_fallback(ctx)

    assert (prefix / "bin" / "nvim").read_text() == "fake nvim binary\n"
    assert not (prefix / "STALE_MARKER").exists()


def test_install_neovim_fallback_replaces_incomplete_prefix(home, monkeypatch):
    ctx = make_ctx(home)
    ctx.departure_baseline = depart.Baseline()
    prefix = home / ".local" / "opt" / "neovim"
    (prefix / "bin").mkdir(parents=True)  # interrupted install: no nvim binary

    _stub_neovim_fallback(monkeypatch, have_nvim=False)
    install._install_neovim_fallback(ctx)

    assert (prefix / "bin" / "nvim").read_text() == "fake nvim binary\n"


def test_install_neovim_fallback_replaces_unchanged_recorded_prefix(home, monkeypatch):
    """TREE_UNCHANGED: a prefix that still matches its recorded manifest is
    removed and replaced, same as an unrecorded one — the common reinstall
    case."""
    ctx = make_ctx(home)
    ctx.departure_baseline = depart.Baseline()
    prefix = home / ".local" / "opt" / "neovim"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "bin" / "nvim").write_text("previous fallback binary\n")
    depart.record_installed_tree(ctx.departure_baseline, prefix)

    _stub_neovim_fallback(monkeypatch, have_nvim=False)
    install._install_neovim_fallback(ctx)

    assert (prefix / "bin" / "nvim").read_text() == "fake nvim binary\n"


def test_install_neovim_fallback_blocks_on_modified_recorded_prefix(
    home, monkeypatch, capsys
):
    """TREE_MODIFIED: a recorded prefix that no longer matches (the user
    added something) is left untouched, not silently destroyed — the fix
    for meta-guard-nvim-fallback-clobber's incident class."""
    ctx = make_ctx(home)
    ctx.departure_baseline = depart.Baseline()
    prefix = home / ".local" / "opt" / "neovim"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "bin" / "nvim").write_text("previous fallback binary\n")
    depart.record_installed_tree(ctx.departure_baseline, prefix)
    (prefix / "my-own-plugin.lua").write_text("-- mine\n")

    calls = _stub_neovim_fallback(monkeypatch, have_nvim=False)
    install._install_neovim_fallback(ctx)

    out = capsys.readouterr().out
    assert "may contain changes you made after installing" in out
    assert ctx.neovim_fallback_failure is not None
    assert (prefix / "bin" / "nvim").read_text() == "previous fallback binary\n"
    assert (prefix / "my-own-plugin.lua").read_text() == "-- mine\n"
    assert kinds(ctx, "package-installed") == []
    # tmp_dir cleanup still runs even though the install is skipped
    assert any(argv[0] == "curl" for argv in calls)
    assert any(argv[0] == "tar" for argv in calls)


def test_install_neovim_fallback_blocks_on_modified_recorded_symlink(
    home, monkeypatch, capsys
):
    """A symlinked prefix goes through the same verdict check as a
    directory -- TREE_MODIFIED blocks it too, not just the directory case."""
    ctx = make_ctx(home)
    ctx.departure_baseline = depart.Baseline()
    prefix = home / ".local" / "opt" / "neovim"
    real_dir = home / "custom-neovim-build"
    (real_dir / "bin").mkdir(parents=True)
    (real_dir / "bin" / "nvim").write_text("custom build\n")
    depart.record_installed_tree(ctx.departure_baseline, prefix)  # recorded as a dir
    prefix.parent.mkdir(parents=True)
    prefix.symlink_to(real_dir)  # now a symlink -- mismatches the recording

    _stub_neovim_fallback(monkeypatch, have_nvim=False)
    install._install_neovim_fallback(ctx)

    out = capsys.readouterr().out
    assert "may contain changes you made after installing" in out
    assert prefix.is_symlink()
    assert prefix.resolve() == real_dir.resolve()


def test_install_neovim_fallback_replaces_unrecorded_symlink(home, monkeypatch):
    """An unrecorded symlink self-heals (unlinked, not blocked) -- proves
    the non-directory branch's TREE_UNRECORDED path doesn't hit a
    shutil.rmtree-on-non-directory error."""
    ctx = make_ctx(home)
    ctx.departure_baseline = depart.Baseline()
    prefix = home / ".local" / "opt" / "neovim"
    real_dir = home / "elsewhere"
    real_dir.mkdir(parents=True)
    prefix.parent.mkdir(parents=True)
    prefix.symlink_to(real_dir)

    _stub_neovim_fallback(monkeypatch, have_nvim=False)
    install._install_neovim_fallback(ctx)

    assert not prefix.is_symlink()
    assert (prefix / "bin" / "nvim").read_text() == "fake nvim binary\n"


def test_install_neovim_fallback_no_baseline_skips_when_prefix_exists(
    home, monkeypatch, capsys
):
    """ctx.departure_baseline is None with an existing prefix fails safe
    (skips) rather than falling through to an unguarded clobber -- this
    branch is unreachable via a real run_install (see the module-level
    comment at its call site) but must still fail safe if ever hit
    directly, e.g. by a test or a future refactor."""
    ctx = make_ctx(home)
    assert ctx.departure_baseline is None
    prefix = home / ".local" / "opt" / "neovim"
    (prefix / "bin").mkdir(parents=True)
    (prefix / "bin" / "nvim").write_text("existing binary\n")

    calls = _stub_neovim_fallback(monkeypatch, have_nvim=False)
    install._install_neovim_fallback(ctx)

    out = capsys.readouterr().out
    assert "no departure baseline available" in out
    assert ctx.neovim_fallback_failure is not None
    assert (prefix / "bin" / "nvim").read_text() == "existing binary\n"
    assert any(argv[0] == "curl" for argv in calls)  # tmp_dir cleanup still ran


def test_install_neovim_fallback_no_baseline_proceeds_when_prefix_absent(
    home, monkeypatch
):
    """The no-baseline check must never fire when there's nothing at prefix
    to protect -- a clean install proceeds regardless of
    ctx.departure_baseline. This is the case an earlier draft of this fix
    got wrong (gated the whole function on the baseline instead of just
    the removal decision)."""
    ctx = make_ctx(home)
    assert ctx.departure_baseline is None
    prefix = home / ".local" / "opt" / "neovim"
    assert not prefix.exists()

    _stub_neovim_fallback(monkeypatch, have_nvim=False)
    install._install_neovim_fallback(ctx)

    assert (prefix / "bin" / "nvim").read_text() == "fake nvim binary\n"
    assert ctx.neovim_fallback_failure is None


def test_install_neovim_fallback_dry_run(home, monkeypatch, capsys):
    ctx = make_ctx(home, dry_run=True)
    calls = _stub_neovim_fallback(monkeypatch, have_nvim=False)
    install._install_neovim_fallback(ctx)

    out = capsys.readouterr().out
    assert "[dry-run]" in out
    assert install.NEOVIM_FALLBACK_VERSION in out
    assert calls == []
    assert not (home / ".local" / "opt" / "neovim").exists()
    assert kinds(ctx, "package-installed") == []


def test_color_is_off_for_non_tty(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)

    class _NotATty:
        def isatty(self):
            return False

    class _Tty:
        def isatty(self):
            return True

    monkeypatch.setenv("TERM", "xterm-256color")
    assert not install.color_enabled(_NotATty())
    assert install.color_enabled(_Tty())

    monkeypatch.setenv("NO_COLOR", "1")
    assert not install.color_enabled(_Tty())
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setenv("TERM", "dumb")
    assert not install.color_enabled(_Tty())


def test_palette_wraps_only_when_enabled():
    assert install.Palette(False).ok("hi") == "hi"
    assert install.Palette(True).ok("hi") == "\x1b[32mhi\x1b[0m"


def test_context_display_shortens_home(home):
    ctx = make_ctx(home)
    assert ctx.display(home / ".claude" / "settings.json") == "~/.claude/settings.json"
    assert ctx.display(Path("/etc/hosts")) == "/etc/hosts"


# ── departure baseline capture ──────────────────────────────────────────────


def test_capture_departure_baseline_noop_on_mac(home, links):
    ctx = make_ctx(home, system="Darwin")
    install.capture_departure_baseline(ctx, links)
    assert not depart.baseline_path(ctx.state_dir).exists()


def test_capture_departure_baseline_noop_on_dry_run(home, links):
    ctx = make_ctx(home, dry_run=True)
    install.capture_departure_baseline(ctx, links)
    assert not depart.baseline_path(ctx.state_dir).exists()


def test_capture_departure_baseline_captures_rc_files_with_blobs(home, links):
    (home / ".zshrc").write_text("original zshrc\n")
    ctx = make_ctx(home)
    install.capture_departure_baseline(ctx, links)

    baseline = depart.load_baseline(ctx.state_dir)
    assert baseline is not None
    record = baseline.value_for(depart.file_key(home / ".zshrc"))
    assert record["state"] == "present"
    assert depart.read_blob(ctx.state_dir, record["blob"]) == b"original zshrc\n"

    assert baseline.value_for(depart.file_key(home / ".bashrc")) == {"state": "absent"}


def test_capture_departure_baseline_captures_link_destination_and_bak(home, links):
    ctx = make_ctx(home, harnesses=("claude",))
    install.capture_departure_baseline(ctx, links)
    baseline = depart.load_baseline(ctx.state_dir)

    dest = home / ".vimrc"
    assert baseline.value_for(depart.file_key(dest)) == {"state": "absent"}
    assert baseline.value_for(depart.symlink_key(dest)) == {"state": "absent"}
    assert baseline.value_for(depart.file_key(dest.with_name(dest.name + ".bak"))) == {
        "state": "absent"
    }


def test_capture_departure_baseline_captures_ancestor_directories(home, links):
    ctx = make_ctx(home, harnesses=("claude",))
    install.capture_departure_baseline(ctx, links)
    baseline = depart.load_baseline(ctx.state_dir)
    # ~/.local/bin is an ancestor of the uv/oh-my-posh/shim destinations.
    assert baseline.value_for(depart.directory_key(home / ".local" / "bin")) == {
        "state": "absent"
    }


def test_capture_departure_baseline_captures_seed_destination_when_harness_selected(
    home, links
):
    ctx = make_ctx(home, harnesses=("claude",))
    install.capture_departure_baseline(ctx, links)
    baseline = depart.load_baseline(ctx.state_dir)
    dest = home / ".claude" / "settings.json"
    assert baseline.value_for(depart.file_key(dest)) is not None


def test_capture_departure_baseline_skips_seed_destination_when_harness_not_selected(
    home, links
):
    ctx = make_ctx(home, harnesses=("opencode",))
    install.capture_departure_baseline(ctx, links)
    baseline = depart.load_baseline(ctx.state_dir)
    dest = home / ".claude" / "settings.json"
    assert baseline.value_for(depart.file_key(dest)) is None


def test_capture_departure_baseline_captures_vscode_files_when_discoverable(
    home, links, monkeypatch
):
    vscode_dir = home / "winappdata"
    vscode_dir.mkdir()
    monkeypatch.setattr(install, "_vscode_wsl_user_dir", lambda: vscode_dir)
    ctx = make_ctx(home, is_wsl=True)
    install.capture_departure_baseline(ctx, links)
    baseline = depart.load_baseline(ctx.state_dir)

    for name in (
        "settings.json",
        "keybindings.json",
        "settings.json.bak",
        "keybindings.json.bak",
    ):
        record = baseline.value_for(depart.file_key(vscode_dir / name))
        assert record == {"state": "absent", "needs_vscode_guard": True}


def test_capture_departure_baseline_skips_vscode_files_without_discovery(
    home, links, monkeypatch
):
    monkeypatch.setattr(install, "_vscode_wsl_user_dir", lambda: None)
    ctx = make_ctx(home, is_wsl=True)
    install.capture_departure_baseline(ctx, links)
    baseline = depart.load_baseline(ctx.state_dir)

    for name in ("settings.json", "keybindings.json"):
        assert baseline.value_for(depart.file_key(home / name)) is None


def test_capture_departure_baseline_tree_manifests_font_and_neovim_prefix(home, links):
    ctx = make_ctx(home)
    install.capture_departure_baseline(ctx, links)
    baseline = depart.load_baseline(ctx.state_dir)
    font_dir = home / ".local" / "share" / "fonts" / "JetBrainsMonoNerdFont"
    neovim_prefix = home / ".local" / "opt" / "neovim"
    assert baseline.value_for(depart.directory_key(font_dir)) == {"state": "absent"}
    assert baseline.value_for(depart.directory_key(neovim_prefix)) == {
        "state": "absent"
    }


def test_capture_departure_baseline_captures_shared_nvim_dirs(home, links):
    ctx = make_ctx(home)
    install.capture_departure_baseline(ctx, links)
    baseline = depart.load_baseline(ctx.state_dir)
    for parts in depart.SHARED_NEOVIM_DIRS:
        key = depart.directory_key(home.joinpath(*parts))
        assert baseline.value_for(key) == {"state": "absent"}


def test_capture_departure_baseline_captures_runtime_nvm(home, links):
    ctx = make_ctx(home)
    install.capture_departure_baseline(ctx, links)
    baseline = depart.load_baseline(ctx.state_dir)
    assert baseline.value_for(depart.runtime_key(home / ".nvm")) == {"state": "absent"}


def test_capture_departure_baseline_first_layer_is_immutable_across_runs(home, links):
    ctx = make_ctx(home)
    install.capture_departure_baseline(ctx, links)  # first run: rc absent

    (home / ".zshrc").write_text("added after baseline was captured")
    install.capture_departure_baseline(ctx, links)  # second run

    baseline = depart.load_baseline(ctx.state_dir)
    # Still recorded as absent from the first (immutable) layer — a
    # later-observed present value must never overwrite it.
    assert baseline.value_for(depart.file_key(home / ".zshrc")) == {"state": "absent"}
    assert len(baseline.layers) == 1  # nothing genuinely unrecorded to add


def test_capture_departure_baseline_runs_via_run_install(home, links, offline_install):
    ctx = make_ctx(home, harnesses=("claude",))
    install.run_install(ctx, links)
    assert depart.baseline_path(ctx.state_dir).is_file()


def test_preflight_sees_harness_gated_files_despite_depart_having_no_harness(
    home, links, offline_install
):
    """Regression test for a real bug caught only by a container run: --depart
    is standalone (parse_args rejects --harness alongside it), so
    ctx.opts.harnesses is always empty at departure time. build_preflight_report
    must never re-derive "applicable" links.toml destinations from that empty
    selection — every fast test happened to capture and recapture with the
    same harness, so this was invisible until a real Ubuntu container showed
    every claude-harness-gated file (~/.claude/CLAUDE.md, its commands,
    settings.json) silently falling out of the preflight report entirely."""
    ctx = make_ctx(home, harnesses=("claude",))
    install.run_install(ctx, links)

    # The real CLI shape: a --depart Options has no harnesses at all.
    depart_ctx = make_ctx(home, harnesses=())
    assert depart_ctx.opts.harnesses == ()

    report = install.build_preflight_report(depart_ctx)
    claude_md_key = depart.symlink_key(home / ".claude" / "CLAUDE.md")
    settings_key = depart.file_key(home / ".claude" / "settings.json")

    assert claude_md_key in report
    assert report[claude_md_key].bucket == "owned"
    assert report[claude_md_key].action == "remove"
    assert settings_key in report
    assert report[settings_key].bucket == "owned"
    assert report[settings_key].action == "remove"


# ── build_preflight_report: the two named restore reclassifications ────────


def test_preflight_reclassifies_appended_rc_file_as_owned_restore(
    home, links, offline_install
):
    (home / ".zshrc").write_text("original zshrc content\n")
    ctx = make_ctx(home, harnesses=("claude",))
    install.run_install(ctx, links)  # captures baseline with original content

    # links.toml itself symlinks ~/.zshrc into the repo, so writing through
    # the live symlink would edit the repo's own tracked file — unlink it
    # first and replace it with a plain file simulating NVM/_install_uv/
    # oh-my-posh appending to the rc file, without touching the checkout.
    zshrc = home / ".zshrc"
    assert zshrc.is_symlink()
    zshrc.unlink()
    zshrc.write_text("original zshrc content\nexport NVM_DIR=...\n")

    report = install.build_preflight_report(make_ctx(home))
    c = report[depart.file_key(zshrc)]
    assert c.bucket == "owned"
    assert c.action == "restore"


def test_preflight_leaves_edited_rc_file_unresolved(home, links, offline_install):
    (home / ".zshrc").write_text("original zshrc content\n")
    ctx = make_ctx(home, harnesses=("claude",))
    install.run_install(ctx, links)

    # links.toml itself symlinks ~/.zshrc into the repo, so writing through
    # the live symlink would edit the repo's own tracked file — unlink it
    # first and replace it with a plain file to simulate an edited/replaced
    # rc file without touching the checkout.
    zshrc = home / ".zshrc"
    assert zshrc.is_symlink()
    zshrc.unlink()
    zshrc.write_text("completely different content\n")

    report = install.build_preflight_report(make_ctx(home))
    c = report[depart.file_key(zshrc)]
    assert c.bucket == "unresolved"


def test_preflight_reclassifies_backed_up_then_symlinked_pair(
    home, links, offline_install
):
    dest = home / ".vimrc"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("my own vimrc\n")
    ctx = make_ctx(home, harnesses=("claude",))
    install.run_install(ctx, links)  # backs dest up to .vimrc.bak, symlinks over it

    assert dest.is_symlink()
    backup = dest.with_name(dest.name + ".bak")
    assert backup.read_text() == "my own vimrc\n"

    report = install.build_preflight_report(make_ctx(home))
    file_c = report[depart.file_key(dest)]
    symlink_c = report[depart.symlink_key(dest)]
    assert file_c.bucket == "owned"
    assert file_c.action == "restore"
    assert symlink_c.bucket == "owned"
    assert symlink_c.action == "remove"


# ── rollback deletes departure state ────────────────────────────────────────


def test_real_rollback_deletes_baseline_and_blobs(home, links, offline_install):
    (home / ".zshrc").write_text("x")
    ctx = make_ctx(home, harnesses=("claude",))
    install.run_install(ctx, links)
    assert depart.baseline_path(ctx.state_dir).is_file()
    assert list(ctx.state_dir.glob("baseline-snapshot-*.blob"))

    install.do_rollback(make_ctx(home))

    assert not depart.baseline_path(ctx.state_dir).exists()
    assert not list(ctx.state_dir.glob("baseline-snapshot-*.blob"))


def test_real_rollback_deletes_blobs_even_with_unparseable_baseline_json(
    home, links, offline_install
):
    """The pinned glob fallback: baseline.json missing/empty/unparseable at
    rollback time still deletes exactly baseline.json + baseline-snapshot-*.blob
    — nothing else, since blob filenames are content-addressed and never
    created by anything but this feature."""
    (home / ".zshrc").write_text("x")
    ctx = make_ctx(home, harnesses=("claude",))
    install.run_install(ctx, links)
    assert list(ctx.state_dir.glob("baseline-snapshot-*.blob"))

    depart.baseline_path(ctx.state_dir).write_text("not valid json{")
    assert depart.load_baseline(ctx.state_dir) is None  # confirms it's unparseable

    install.do_rollback(make_ctx(home))

    assert not depart.baseline_path(ctx.state_dir).exists()
    assert not list(ctx.state_dir.glob("baseline-snapshot-*.blob"))


def test_rollback_wipe_dry_run_preview_excludes_departure_state(
    home, links, offline_install, monkeypatch, capsys
):
    (home / ".zshrc").write_text("x")
    ctx = make_ctx(home, harnesses=("claude",))
    install.run_install(ctx, links)

    monkeypatch.setattr(
        install,
        "run_command",
        lambda cmd, **k: install.CommandResult(True, ""),
    )
    code = install.do_rollback(make_ctx(home, wipe=True, dry_run=True))
    out = capsys.readouterr().out

    assert code == 0
    assert f"would remove empty state directory {ctx.state_dir}" in out


# ── execute_file_symlink_phase: VS Code process-running guard ──────────────


def _vscode_guard_baseline_and_report(guarded_paths, unguarded_path):
    """A baseline + preflight report with guarded and unguarded owned/remove
    file: keys, for exercising execute_file_symlink_phase's guard directly
    without a full install/capture pass."""
    baseline = depart.Baseline()
    records = {}
    for p in (*guarded_paths, unguarded_path):
        records[depart.file_key(p)] = {"state": "absent"}
    for p in guarded_paths:
        records[depart.file_key(p)]["needs_vscode_guard"] = True
    baseline.add_layer("2024-01-01T00:00:00+00:00", records)

    report = {
        depart.file_key(p): depart.Classification(
            depart.BUCKET_OWNED, depart.ACTION_REMOVE, "absent at baseline, now present"
        )
        for p in (*guarded_paths, unguarded_path)
    }
    return baseline, report


def test_execute_file_symlink_phase_removes_guarded_key_when_not_running(
    home, monkeypatch
):
    guarded = home / "settings.json"
    guarded.write_text("x")
    unguarded = home / ".vimrc"
    unguarded.write_text("y")
    baseline, report = _vscode_guard_baseline_and_report([guarded], unguarded)
    ledger = depart.DepartureLedger(depart.departure_ledger_path(home / "state"))

    monkeypatch.setattr(
        settings_seed_drift_check, "_vscode_process_running", lambda: False
    )
    install.execute_file_symlink_phase(make_ctx(home), baseline, report, ledger)

    assert not guarded.exists()
    assert not unguarded.exists()
    outcomes = {e["key"]: e["outcome"] for e in ledger.entries()}
    assert outcomes[depart.file_key(guarded)] == "ok"


@pytest.mark.parametrize("running_value", [True, None])
def test_execute_file_symlink_phase_leaves_guarded_key_unresolved_when_running_or_unknown(
    home, monkeypatch, running_value
):
    guarded = home / "settings.json"
    guarded.write_text("x")
    unguarded = home / ".vimrc"
    unguarded.write_text("y")
    baseline, report = _vscode_guard_baseline_and_report([guarded], unguarded)
    ledger = depart.DepartureLedger(depart.departure_ledger_path(home / "state"))

    monkeypatch.setattr(
        settings_seed_drift_check, "_vscode_process_running", lambda: running_value
    )
    install.execute_file_symlink_phase(make_ctx(home), baseline, report, ledger)

    assert guarded.exists()  # left alone
    assert not unguarded.exists()  # unaffected — still removed
    outcomes = {e["key"]: e["outcome"] for e in ledger.entries()}
    assert outcomes[depart.file_key(guarded)].startswith(
        install._VSCODE_GUARD_UNRESOLVED_PREFIX
    )
    assert "VS Code" in outcomes[depart.file_key(guarded)]
    assert outcomes[depart.file_key(unguarded)] == "ok"


def test_execute_file_symlink_phase_checks_process_running_exactly_once(
    home, monkeypatch
):
    guarded_paths = [
        home / n
        for n in (
            "settings.json",
            "keybindings.json",
            "settings.json.bak",
            "keybindings.json.bak",
        )
    ]
    for p in guarded_paths:
        p.write_text("x")
    unguarded = home / ".vimrc"
    unguarded.write_text("y")
    baseline, report = _vscode_guard_baseline_and_report(guarded_paths, unguarded)
    ledger = depart.DepartureLedger(depart.departure_ledger_path(home / "state"))

    calls = []

    def _tracking_check():
        calls.append(1)
        return False

    monkeypatch.setattr(
        settings_seed_drift_check, "_vscode_process_running", _tracking_check
    )
    install.execute_file_symlink_phase(make_ctx(home), baseline, report, ledger)

    assert len(calls) == 1


def test_execute_file_symlink_phase_never_checks_process_running_when_no_guarded_keys(
    home, monkeypatch
):
    unguarded = home / ".vimrc"
    unguarded.write_text("y")
    baseline, report = _vscode_guard_baseline_and_report([], unguarded)
    ledger = depart.DepartureLedger(depart.departure_ledger_path(home / "state"))

    def _forbidden():
        raise AssertionError("process check should not run without guarded keys")

    monkeypatch.setattr(
        settings_seed_drift_check, "_vscode_process_running", _forbidden
    )
    install.execute_file_symlink_phase(make_ctx(home), baseline, report, ledger)
    assert not unguarded.exists()


def _vscode_guard_solo_baseline_and_report(guarded):
    """A baseline + report for a single guarded owned/remove file: key,
    for the retry-boundary tests below -- no unguarded companion key."""
    baseline = depart.Baseline()
    baseline.add_layer(
        "2024-01-01T00:00:00+00:00",
        {depart.file_key(guarded): {"state": "absent", "needs_vscode_guard": True}},
    )
    report = {
        depart.file_key(guarded): depart.Classification(
            depart.BUCKET_OWNED, depart.ACTION_REMOVE, "absent at baseline, now present"
        )
    }
    return baseline, report


def test_execute_file_symlink_phase_retries_guarded_key_after_close(home, monkeypatch):
    guarded = home / "settings.json"
    guarded.write_text("x")
    baseline, report = _vscode_guard_solo_baseline_and_report(guarded)
    ledger = depart.DepartureLedger(depart.departure_ledger_path(home / "state"))
    ctx = make_ctx(home)
    calls: list[bool | None] = []

    def _running_then_closed():
        result = True if not calls else False
        calls.append(result)
        return result

    monkeypatch.setattr(
        settings_seed_drift_check, "_vscode_process_running", _running_then_closed
    )
    install.execute_file_symlink_phase(ctx, baseline, report, ledger)
    assert guarded.exists()
    assert depart.file_key(guarded) in ledger.completed_keys()

    install.execute_file_symlink_phase(ctx, baseline, report, ledger)
    assert not guarded.exists()
    assert calls == [True, False]  # proves the second call genuinely re-checked


def test_execute_file_symlink_phase_does_not_retry_guarded_key_after_unrelated_failure(
    home, monkeypatch
):
    guarded = home / "settings.json"
    guarded.write_text("x")
    baseline, report = _vscode_guard_solo_baseline_and_report(guarded)
    ledger = depart.DepartureLedger(depart.departure_ledger_path(home / "state"))
    # Simulate a prior attempt that failed for a reason that has nothing to
    # do with the VS Code guard (matches install._execute_remove_file's own
    # OSError-outcome shape), not the guard's message.
    ledger.record(
        depart.file_key(guarded),
        depart.ACTION_REMOVE,
        "unresolved: could not remove file (mock)",
    )

    monkeypatch.setattr(
        settings_seed_drift_check, "_vscode_process_running", lambda: False
    )
    install.execute_file_symlink_phase(make_ctx(home), baseline, report, ledger)
    assert guarded.exists()  # never retried -- not a guard-block outcome


def test_execute_file_symlink_phase_stays_retryable_after_guard_then_unrelated_failure(
    home, monkeypatch
):
    guarded = home / "settings.json"
    guarded.write_text("x")
    baseline, report = _vscode_guard_solo_baseline_and_report(guarded)
    ledger = depart.DepartureLedger(depart.departure_ledger_path(home / "state"))
    # A guard block, then a later unrelated failure -- both recorded,
    # oldest first, matching the ledger's append-only history.
    ledger.record(
        depart.file_key(guarded),
        depart.ACTION_REMOVE,
        "unresolved [vscode-guard-blocked]: Windows VS Code is running (or "
        "could not be verified) — close it first, then re-run --depart",
    )
    ledger.record(
        depart.file_key(guarded),
        depart.ACTION_REMOVE,
        "unresolved: could not remove file (mock)",
    )

    monkeypatch.setattr(
        settings_seed_drift_check, "_vscode_process_running", lambda: False
    )
    install.execute_file_symlink_phase(make_ctx(home), baseline, report, ledger)
    assert not guarded.exists()  # still retryable -- it was guard-blocked at least once


# ── build_preflight_report: display-only VS Code guard annotation ──────────


def test_vscode_guard_preflight_annotation_present_when_running_or_unknown(
    home, monkeypatch
):
    guarded = home / "settings.json"
    ctx = make_ctx(home)
    baseline = depart.Baseline()
    baseline.add_layer(
        "2024-01-01T00:00:00+00:00",
        {depart.file_key(guarded): {"state": "absent", "needs_vscode_guard": True}},
    )
    depart.save_baseline(ctx.state_dir, baseline)
    report = {
        depart.file_key(guarded): depart.Classification(
            depart.BUCKET_OWNED, depart.ACTION_REMOVE, "absent at baseline, now present"
        )
    }

    monkeypatch.setattr(
        settings_seed_drift_check, "_vscode_process_running", lambda: True
    )
    annotations = install._vscode_guard_preflight_annotations(ctx, report)

    assert depart.file_key(guarded) in annotations
    # Purely display-only: the stored Classification is untouched.
    assert report[depart.file_key(guarded)] == depart.Classification(
        depart.BUCKET_OWNED, depart.ACTION_REMOVE, "absent at baseline, now present"
    )


def test_vscode_guard_preflight_annotation_absent_when_not_running(home, monkeypatch):
    guarded = home / "settings.json"
    ctx = make_ctx(home)
    baseline = depart.Baseline()
    baseline.add_layer(
        "2024-01-01T00:00:00+00:00",
        {depart.file_key(guarded): {"state": "absent", "needs_vscode_guard": True}},
    )
    depart.save_baseline(ctx.state_dir, baseline)
    report = {
        depart.file_key(guarded): depart.Classification(
            depart.BUCKET_OWNED, depart.ACTION_REMOVE, "absent at baseline, now present"
        )
    }

    monkeypatch.setattr(
        settings_seed_drift_check, "_vscode_process_running", lambda: False
    )
    annotations = install._vscode_guard_preflight_annotations(ctx, report)

    assert annotations == {}


# ── do_depart: zero-evidence refusal, preflight, confirmation ──────────────


class _FakeStdin:
    """A minimal stand-in for sys.stdin: isatty() plus one readline()."""

    def __init__(self, isatty, text=""):
        self._isatty = isatty
        self._lines = io.StringIO(text)

    def isatty(self):
        return self._isatty

    def readline(self):
        return self._lines.readline()


def test_do_depart_zero_evidence_refusal_exits_2(home, capsys):
    code = install.do_depart(make_ctx(home))
    assert code == 2
    assert "no baseline" in capsys.readouterr().err


def test_do_depart_dry_run_prints_preflight_and_exits_0(
    home, links, offline_install, capsys
):
    ctx = make_ctx(home, harnesses=("claude",))
    install.run_install(ctx, links)

    code = install.do_depart(make_ctx(home, dry_run=True))
    out = capsys.readouterr().out

    assert code == 0
    assert "Departure preflight" in out
    assert "owned" in out


def test_do_depart_dry_run_never_touches_stdin(
    home, links, offline_install, monkeypatch
):
    """--dry-run always exits 0 regardless of TTY status and never prompts."""
    ctx = make_ctx(home, harnesses=("claude",))
    install.run_install(ctx, links)

    def _boom():
        raise AssertionError("dry-run must not check stdin")

    monkeypatch.setattr(sys, "stdin", _FakeStdin(False))
    monkeypatch.setattr(sys.stdin, "isatty", _boom)
    code = install.do_depart(make_ctx(home, dry_run=True))
    assert code == 0


def test_do_depart_non_tty_real_run_without_yes_exits_2(
    home, links, offline_install, monkeypatch, capsys
):
    ctx = make_ctx(home, harnesses=("claude",))
    install.run_install(ctx, links)

    monkeypatch.setattr(sys, "stdin", _FakeStdin(False))
    code = install.do_depart(make_ctx(home))
    assert code == 2
    assert "non-interactive" in capsys.readouterr().err


def test_do_depart_non_tty_real_run_without_yes_skips_preflight_dump(
    home, links, offline_install, monkeypatch, capsys
):
    """A guaranteed-refusal run shouldn't bury the error under a preflight
    report the user can't act on anyway -- the error must be the only
    output, not the last line of a long dump."""
    ctx = make_ctx(home, harnesses=("claude",))
    install.run_install(ctx, links)

    monkeypatch.setattr(sys, "stdin", _FakeStdin(False))
    code = install.do_depart(make_ctx(home))
    assert code == 2
    out = capsys.readouterr().out
    assert "owned (" not in out
    assert "preserved (" not in out


def test_do_depart_yes_bypasses_prompt_entirely(
    home, links, offline_install, monkeypatch, capsys
):
    ctx = make_ctx(home, harnesses=("claude",))
    install.run_install(ctx, links)

    def _boom():
        raise AssertionError("--yes must skip the confirmation prompt")

    monkeypatch.setattr(sys, "stdin", _FakeStdin(False))
    monkeypatch.setattr(sys.stdin, "isatty", _boom)
    code = install.do_depart(make_ctx(home, yes=True))
    out = capsys.readouterr().out
    # Everything this fresh install created is absent-at-baseline, so a
    # full departure succeeds and leaves no trace.
    assert code == 0
    assert "Departure complete" in out
    assert not any(home.iterdir())


def test_do_depart_correct_token_proceeds_past_confirmation(
    home, links, offline_install, monkeypatch, capsys
):
    ctx = make_ctx(home, harnesses=("claude",))
    install.run_install(ctx, links)

    monkeypatch.setattr(sys, "stdin", _FakeStdin(True, "DEPART\n"))
    code = install.do_depart(make_ctx(home))
    out = capsys.readouterr().out
    assert code == 0
    assert "Departure complete" in out
    assert not any(home.iterdir())


@pytest.mark.parametrize("token", ["nope\n", " DEPART \n", "depart\n", ""])
def test_do_depart_bad_token_exits_2(
    home, links, offline_install, monkeypatch, capsys, token
):
    """Wrong token, surrounding whitespace, wrong case, or EOF all refuse."""
    ctx = make_ctx(home, harnesses=("claude",))
    install.run_install(ctx, links)

    monkeypatch.setattr(sys, "stdin", _FakeStdin(True, token))
    code = install.do_depart(make_ctx(home))
    assert code == 2
    assert "confirmation not received" in capsys.readouterr().err


def test_do_depart_token_without_trailing_newline_still_matches(
    home, links, offline_install, monkeypatch, capsys
):
    """Nothing to strip when input has no trailing LF at all — still a match."""
    ctx = make_ctx(home, harnesses=("claude",))
    install.run_install(ctx, links)

    monkeypatch.setattr(sys, "stdin", _FakeStdin(True, "DEPART"))
    code = install.do_depart(make_ctx(home))
    assert code == 0


# ── do_depart execution: the two named restore cases, end to end ──────────


def test_depart_restores_backed_up_then_symlinked_destination(
    home, links, offline_install
):
    dest = home / ".vimrc"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("my own vimrc\n")
    ctx = make_ctx(home, harnesses=("claude",))
    install.run_install(ctx, links)
    assert dest.is_symlink()

    code = install.do_depart(make_ctx(home, yes=True))

    assert code == 0
    assert dest.is_file() and not dest.is_symlink()
    assert dest.read_text() == "my own vimrc\n"
    assert not dest.with_name(dest.name + ".bak").exists()


def test_depart_restores_appended_rc_file(home, links, offline_install):
    zshrc = home / ".zshrc"
    zshrc.write_text("original zshrc content\n")
    ctx = make_ctx(home, harnesses=("claude",))
    install.run_install(ctx, links)  # baseline blob captures the original content

    # links.toml symlinks ~/.zshrc into the repo; unlink first so writing
    # the "appended" simulation doesn't touch the checkout (see the
    # preflight reclassification tests' identical caveat).
    assert zshrc.is_symlink()
    zshrc.unlink()
    zshrc.write_text("original zshrc content\nexport NVM_DIR=...\n")

    code = install.do_depart(make_ctx(home, yes=True))

    assert code == 0
    assert zshrc.read_text() == "original zshrc content\n"


def test_depart_leaves_unrelated_files_untouched(home, links, offline_install):
    unrelated = home / "my-notes.txt"
    unrelated.write_text("keep me\n")
    ctx = make_ctx(home, harnesses=("claude",))
    install.run_install(ctx, links)

    code = install.do_depart(make_ctx(home, yes=True))

    assert code == 0
    assert unrelated.read_text() == "keep me\n"


# ── wholesale tree removal is gated on the post-install manifest ──────────


def _seed_installed_font_tree(home, *, record: bool):
    """Put an installer-shaped font tree in place, optionally snapshotting it.

    Mirrors what _install_nerd_font leaves behind, without downloading
    anything. ``record=False`` reproduces a baseline captured before
    post-install snapshotting existed.
    """
    font_dir = home / ".local" / "share" / "fonts" / "JetBrainsMonoNerdFont"
    font_dir.mkdir(parents=True)
    (font_dir / "JetBrainsMono-Regular.ttf").write_bytes(b"installer font")
    (font_dir / ".nerd-fonts-version").write_text("3.4.0\n")

    baseline = depart.load_baseline(home / ".local" / "state" / "dotfiles") or (
        depart.Baseline()
    )
    if record:
        depart.record_installed_tree(baseline, font_dir)
    depart.save_baseline(home / ".local" / "state" / "dotfiles", baseline)
    return font_dir


def test_depart_removes_an_untouched_font_tree_wholesale(home, links, offline_install):
    ctx = make_ctx(home, harnesses=("claude",))
    install.run_install(ctx, links)
    font_dir = _seed_installed_font_tree(home, record=True)

    code = install.do_depart(make_ctx(home, yes=True))

    assert code == 0
    assert not font_dir.exists()


def test_depart_preserves_a_font_tree_the_user_added_to(
    home, links, offline_install, capsys
):
    """The data-loss regression: a font added after install must survive."""
    ctx = make_ctx(home, harnesses=("claude",))
    install.run_install(ctx, links)
    font_dir = _seed_installed_font_tree(home, record=True)

    mine = font_dir / "MyOwnFont.ttf"
    mine.write_bytes(b"do not delete me")

    code = install.do_depart(make_ctx(home, yes=True))
    out = capsys.readouterr().out

    assert code == 1
    assert font_dir.exists()
    assert mine.read_bytes() == b"do not delete me"
    assert "tree changed since install" in out


def test_depart_preserves_a_font_tree_with_no_recorded_manifest(
    home, links, offline_install, capsys
):
    """An older baseline cannot prove the tree is untouched, so keep it."""
    ctx = make_ctx(home, harnesses=("claude",))
    install.run_install(ctx, links)
    font_dir = _seed_installed_font_tree(home, record=False)

    code = install.do_depart(make_ctx(home, yes=True))
    out = capsys.readouterr().out

    assert code == 1
    assert font_dir.exists()
    assert "no post-install manifest" in out


def test_install_records_the_font_tree_manifest(home, links, monkeypatch):
    """_install_nerd_font must snapshot what it produced."""
    ctx = make_ctx(home)
    ctx.departure_baseline = depart.Baseline()
    font_dir = home / ".local" / "share" / "fonts" / "JetBrainsMonoNerdFont"

    def fake_run(cmd, **kwargs):
        if cmd and cmd[0] == "curl":
            return install.CommandResult(True)
        if cmd and cmd[0] == "unzip":
            font_dir.mkdir(parents=True, exist_ok=True)
            (font_dir / "JetBrainsMono-Regular.ttf").write_bytes(b"font")
            return install.CommandResult(True)
        return install.CommandResult(True)

    monkeypatch.setattr(install, "run_command", fake_run)
    install._install_nerd_font(ctx)

    assert str(font_dir) in ctx.departure_baseline.installed_trees
    assert depart.installed_tree_verdict(ctx.departure_baseline, font_dir) == (
        depart.TREE_UNCHANGED
    )


def test_depart_retry_skips_already_ledger_completed_actions(
    home, links, offline_install, monkeypatch
):
    ctx = make_ctx(home, harnesses=("claude",))
    install.run_install(ctx, links)

    baseline = depart.load_baseline(ctx.state_dir)
    report = install.build_preflight_report(make_ctx(home))
    ledger = depart.DepartureLedger(depart.departure_ledger_path(ctx.state_dir))
    install.execute_file_symlink_phase(make_ctx(home), baseline, report, ledger)
    completed_after_first_pass = ledger.completed_keys()
    assert completed_after_first_pass  # the fresh install created plenty to remove

    calls = []
    real_remove = install._execute_remove_file

    def _tracking_remove(path):
        calls.append(path)
        return real_remove(path)

    monkeypatch.setattr(install, "_execute_remove_file", _tracking_remove)
    install.execute_file_symlink_phase(make_ctx(home), baseline, report, ledger)

    assert calls == []  # nothing already in the ledger was re-attempted


def test_depart_second_lock_acquisition_refuses_while_first_is_live(
    home, links, offline_install, monkeypatch
):
    ctx = make_ctx(home, harnesses=("claude",))
    install.run_install(ctx, links)

    acquired, _ = depart.acquire_departure_lock(
        ctx.state_dir, pid=os.getpid(), start_time_fn=depart.process_start_time
    )
    assert acquired is True
    try:
        code = install.do_depart(make_ctx(home, yes=True))
        assert code == 2
    finally:
        depart.release_departure_lock(ctx.state_dir, pid=os.getpid())


# ── package transaction recording (step 2 live wiring) ─────────────────────


def test_install_linux_packages_one_by_one_records_transactions(home, monkeypatch):
    ctx = make_ctx(home)
    ctx.departure_baseline = depart.Baseline()
    live_versions: dict[str, str] = {}

    def run(cmd, **kwargs):
        if cmd[0] == "dpkg-query":
            output = "".join(f"{n}\t{v}\n" for n, v in live_versions.items())
            return install.CommandResult(True, output)
        if cmd[:3] == ["sudo", "apt-get", "install"]:
            live_versions[cmd[-1]] = "1.0-1"
            return install.CommandResult(True)
        raise AssertionError(f"unexpected command: {cmd!r}")

    monkeypatch.setattr(install, "run_command", run)
    install._install_linux_packages_one_by_one(ctx, "apt", epoch={})

    txns = ctx.departure_baseline.transactions
    assert len(txns) == len(install.LINUX_PACKAGES)
    first = txns[0]
    assert first["manager"] == "apt"
    assert first["requested"] == [install.LINUX_PACKAGES[0]]
    assert first["after"][install.LINUX_PACKAGES[0]] == "1.0-1"
    assert first["comparand_source"] == "epoch"
    # Every recorded package actually landed in the manifest too.
    assert kinds(ctx, "package-installed") == [
        {"kind": "package-installed", "name": pkg} for pkg in install.LINUX_PACKAGES
    ]


def test_install_linux_packages_dry_run_records_no_transactions(home, monkeypatch):
    ctx = make_ctx(home, dry_run=True)
    ctx.departure_baseline = depart.Baseline()

    def run(cmd, **kwargs):
        raise AssertionError("dry-run must never shell out")

    monkeypatch.setattr(install, "run_command", run)
    install._install_linux_packages_one_by_one(ctx, "apt", epoch={})

    assert ctx.departure_baseline.transactions == []


def test_install_linux_packages_no_departure_baseline_skips_probes(home, monkeypatch):
    """When not tracking (e.g. macOS, or dry-run upstream), no probe calls happen."""
    ctx = make_ctx(home)
    assert ctx.departure_baseline is None

    def run(cmd, **kwargs):
        if cmd[:3] == ["sudo", "apt-get", "install"]:
            return install.CommandResult(True)
        raise AssertionError(f"unexpected probe call when not tracking: {cmd!r}")

    monkeypatch.setattr(install, "run_command", run)
    install._install_linux_packages_one_by_one(ctx, "apt", epoch=None)


def test_install_npm_harness_records_transaction(home, monkeypatch):
    ctx = make_ctx(home, harnesses=("claude",))
    ctx.departure_baseline = depart.Baseline()
    live = {"npm": "10.0.0"}

    def run(cmd, **kwargs):
        if cmd[:2] == ["npm", "ls"]:
            deps = {n: {"version": v} for n, v in live.items()}
            return install.CommandResult(True, json.dumps({"dependencies": deps}))
        if cmd[:3] == ["npm", "install", "-g"]:
            live[cmd[3]] = "1.2.3"
            return install.CommandResult(True)
        raise AssertionError(f"unexpected command: {cmd!r}")

    monkeypatch.setattr(install, "have", lambda name: name == "npm")
    monkeypatch.setattr(install, "run_command", run)
    install.install_npm_harness(
        ctx, "claude", "Claude Code", "@anthropic-ai/claude-code"
    )

    txns = ctx.departure_baseline.transactions
    assert len(txns) == 1
    assert txns[0]["manager"] == "npm"
    assert txns[0]["requested"] == ["@anthropic-ai/claude-code"]
    assert txns[0]["after"]["@anthropic-ai/claude-code"] == "1.2.3"


def test_install_ruff_uv_tool_records_transaction(home, monkeypatch):
    ctx = make_ctx(home)
    ctx.departure_baseline = depart.Baseline()
    live: dict[str, str] = {}

    def run(cmd, **kwargs):
        if cmd == ["uv", "tool", "list"]:
            output = "".join(f"{n} v{v}\n" for n, v in live.items())
            return install.CommandResult(True, output)
        if cmd == ["uv", "tool", "install", "ruff"]:
            live["ruff"] = "0.5.0"
            return install.CommandResult(True)
        raise AssertionError(f"unexpected command: {cmd!r}")

    monkeypatch.setattr(install, "have", lambda name: name == "uv")
    monkeypatch.setattr(install, "run_command", run)
    install._install_ruff_uv_tool(ctx)

    txns = ctx.departure_baseline.transactions
    assert len(txns) == 1
    assert txns[0]["manager"] == "uv-tool"
    assert txns[0]["requested"] == ["ruff"]
    assert txns[0]["after"]["ruff"] == "0.5.0"
    assert kinds(ctx, "package-installed") == [
        {"kind": "package-installed", "name": "ruff"}
    ]


# ── package removal execution ───────────────────────────────────────────────


def _package_baseline(*transactions):
    baseline = depart.Baseline()
    for kwargs in transactions:
        depart.record_transaction(baseline, **kwargs)
    return baseline


def test_execute_package_phase_removes_freshly_installed_package(home, monkeypatch):
    baseline = _package_baseline(
        {
            "manager": "apt",
            "requested": ["eza"],
            "before": {},
            "after": {"eza": "0.18.0-1"},
            "captured_at": "t1",
            "epoch": {},
        }
    )
    ledger = depart.DepartureLedger(depart.departure_ledger_path(home / "state"))
    removed = []

    def run(cmd, **kwargs):
        if cmd[0] == "dpkg-query":
            return install.CommandResult(True, "eza\t0.18.0-1\n")
        if cmd == ["sudo", "apt-get", "remove", "-y", "eza"]:
            removed.append("eza")
            return install.CommandResult(True)
        raise AssertionError(f"unexpected command: {cmd!r}")

    monkeypatch.setattr(install, "run_command", run)
    result = install.execute_package_phase(make_ctx(home), baseline, ledger)

    assert result is True
    assert removed == ["eza"]
    assert ledger.completed_keys() == {"package:apt/eza"}


def test_execute_package_phase_never_touches_already_absent_package(home, monkeypatch):
    """Already-absent (preserved) packages aren't ledger-tracked at all — nothing
    to retry, since preflight already showed nothing needed doing."""
    baseline = _package_baseline(
        {
            "manager": "apt",
            "requested": ["eza"],
            "before": {},
            "after": {"eza": "0.18.0-1"},
            "captured_at": "t1",
            "epoch": {},
        }
    )
    ledger = depart.DepartureLedger(depart.departure_ledger_path(home / "state"))

    def run(cmd, **kwargs):
        if cmd[0] == "dpkg-query":
            return install.CommandResult(True, "")  # eza already removed by the user
        raise AssertionError(f"unexpected command: {cmd!r} — nothing to remove")

    monkeypatch.setattr(install, "run_command", run)
    install.execute_package_phase(make_ctx(home), baseline, ledger)
    assert ledger.entries() == []


def test_execute_package_phase_removes_introduced_dependency_when_probe_empty(
    home, monkeypatch
):
    baseline = _package_baseline(
        {
            "manager": "apt",
            "requested": ["eza"],
            "before": {},
            "after": {"eza": "0.18.0-1", "libgit2": "1.7-1"},
            "captured_at": "t1",
            "epoch": {},
        }
    )
    ledger = depart.DepartureLedger(depart.departure_ledger_path(home / "state"))
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0] == "dpkg-query":
            return install.CommandResult(True, "eza\t0.18.0-1\nlibgit2\t1.7-1\n")
        if cmd[:3] == ["apt-cache", "rdepends", "--installed"]:
            return install.CommandResult(True, "")  # nothing depends on it
        if cmd[0] == "sudo":
            return install.CommandResult(True)
        raise AssertionError(f"unexpected command: {cmd!r}")

    monkeypatch.setattr(install, "run_command", run)
    install.execute_package_phase(make_ctx(home), baseline, ledger)

    assert depart.package_key("apt", "libgit2") in ledger.completed_keys()
    libgit2_entry = next(
        e for e in ledger.entries() if e["key"] == "package:apt/libgit2"
    )
    assert libgit2_entry["outcome"] == "ok"
    assert ["apt-cache", "rdepends", "--installed", "libgit2"] in calls


def test_execute_package_phase_blocks_introduced_dependency_when_still_needed(
    home, monkeypatch
):
    baseline = _package_baseline(
        {
            "manager": "apt",
            "requested": ["eza"],
            "before": {},
            "after": {"eza": "0.18.0-1", "libgit2": "1.7-1"},
            "captured_at": "t1",
            "epoch": {},
        }
    )
    ledger = depart.DepartureLedger(depart.departure_ledger_path(home / "state"))

    def run(cmd, **kwargs):
        if cmd[0] == "dpkg-query":
            return install.CommandResult(True, "eza\t0.18.0-1\nlibgit2\t1.7-1\n")
        if cmd[:3] == ["apt-cache", "rdepends", "--installed"]:
            return install.CommandResult(True, "Reverse Depends:\n  other-pkg")
        if cmd == ["sudo", "apt-get", "remove", "-y", "eza"]:
            return install.CommandResult(True)
        raise AssertionError(f"unexpected command: {cmd!r}")

    monkeypatch.setattr(install, "run_command", run)
    install.execute_package_phase(make_ctx(home), baseline, ledger)

    libgit2_entry = next(
        e for e in ledger.entries() if e["key"] == "package:apt/libgit2"
    )
    assert libgit2_entry["outcome"].startswith("unresolved")


def test_execute_package_phase_blocks_dependency_removal_on_failed_probe(
    home, monkeypatch
):
    baseline = _package_baseline(
        {
            "manager": "apt",
            "requested": ["eza"],
            "before": {},
            "after": {"eza": "0.18.0-1", "libgit2": "1.7-1"},
            "captured_at": "t1",
            "epoch": {},
        }
    )
    ledger = depart.DepartureLedger(depart.departure_ledger_path(home / "state"))

    def run(cmd, **kwargs):
        if cmd[0] == "dpkg-query":
            return install.CommandResult(True, "eza\t0.18.0-1\nlibgit2\t1.7-1\n")
        if cmd[:3] == ["apt-cache", "rdepends", "--installed"]:
            return install.CommandResult(False, "")  # probe unavailable/failed
        if cmd == ["sudo", "apt-get", "remove", "-y", "eza"]:
            return install.CommandResult(True)
        raise AssertionError(f"unexpected command: {cmd!r}")

    monkeypatch.setattr(install, "run_command", run)
    install.execute_package_phase(make_ctx(home), baseline, ledger)

    libgit2_entry = next(
        e for e in ledger.entries() if e["key"] == "package:apt/libgit2"
    )
    assert libgit2_entry["outcome"].startswith("unresolved")


def test_execute_package_phase_downgrades_an_upgraded_package(home, monkeypatch):
    baseline = _package_baseline(
        {
            "manager": "apt",
            "requested": ["neovim"],
            "before": {"neovim": "0.9.0-1"},
            "after": {"neovim": "0.10.0-1"},
            "captured_at": "t1",
            "epoch": {"neovim": "0.9.0-1"},
        }
    )
    ledger = depart.DepartureLedger(depart.departure_ledger_path(home / "state"))
    live = {"neovim": "0.10.0-1"}

    def run(cmd, **kwargs):
        if cmd[0] == "dpkg-query":
            return install.CommandResult(True, f"neovim\t{live['neovim']}\n")
        if cmd[:4] == ["sudo", "apt-get", "install", "-y"]:
            live["neovim"] = "0.9.0-1"
            return install.CommandResult(True)
        raise AssertionError(f"unexpected command: {cmd!r}")

    monkeypatch.setattr(install, "run_command", run)
    result = install.execute_package_phase(make_ctx(home), baseline, ledger)

    assert result is True
    entry = next(e for e in ledger.entries() if e["key"] == "package:apt/neovim")
    assert entry["outcome"] == "ok"
    assert entry["action"] == "downgrade"


def test_execute_package_phase_halts_on_changed_state_then_failed_downgrade(
    home, monkeypatch
):
    """tmux (t1, earlier) precedes neovim (t2, later); reverse order means
    neovim is processed *first* and its halt must stop tmux from ever
    being reached."""
    baseline = _package_baseline(
        {
            "manager": "apt",
            "requested": ["tmux"],
            "before": {},
            "after": {"tmux": "3.4-1"},
            "captured_at": "t1",
            "epoch": {},
        },
        {
            "manager": "apt",
            "requested": ["neovim"],
            "before": {"neovim": "0.9.0-1"},
            "after": {"neovim": "0.10.0-1"},
            "captured_at": "t2",
        },
    )
    ledger = depart.DepartureLedger(depart.departure_ledger_path(home / "state"))
    live = {"neovim": "0.10.0-1", "tmux": "3.4-1"}

    def run(cmd, **kwargs):
        if cmd[0] == "dpkg-query":
            output = "".join(f"{n}\t{v}\n" for n, v in live.items())
            return install.CommandResult(True, output)
        if cmd[:4] == ["sudo", "apt-get", "install", "-y"]:
            # The downgrade command "partially" changes live state, then fails.
            live["neovim"] = "0.9.5-1"
            return install.CommandResult(False)
        raise AssertionError(
            f"unexpected command: {cmd!r} — tmux should never be reached"
        )

    monkeypatch.setattr(install, "run_command", run)
    result = install.execute_package_phase(make_ctx(home), baseline, ledger)

    assert result is False  # halted -> caller must skip the runtime phase too
    entry = next(e for e in ledger.entries() if e["key"] == "package:apt/neovim")
    assert entry["outcome"] == "halt: changed-state-then-failed downgrade"
    # tmux (the earlier transaction, processed after neovim in reverse
    # order) was never reached because the phase halted first.
    assert not any(e["key"] == "package:apt/tmux" for e in ledger.entries())


def test_execute_package_phase_skips_already_ledger_completed(home, monkeypatch):
    baseline = _package_baseline(
        {
            "manager": "apt",
            "requested": ["eza"],
            "before": {},
            "after": {"eza": "0.18.0-1"},
            "captured_at": "t1",
            "epoch": {},
        }
    )
    ledger = depart.DepartureLedger(depart.departure_ledger_path(home / "state"))
    ledger.record("package:apt/eza", "remove", "ok")

    def run(cmd, **kwargs):
        raise AssertionError("a ledger-complete package must never be re-probed")

    monkeypatch.setattr(install, "run_command", run)
    install.execute_package_phase(make_ctx(home), baseline, ledger)


def test_execute_departure_skips_runtime_phase_after_package_halt(home, monkeypatch):
    baseline = _package_baseline(
        {
            "manager": "apt",
            "requested": ["neovim"],
            "before": {"neovim": "0.9.0-1"},
            "after": {"neovim": "0.10.0-1"},
            "captured_at": "t1",
            "epoch": {"neovim": "0.9.0-1"},
        }
    )
    ctx = make_ctx(home)
    runtime_key = depart.runtime_key(ctx.home / ".nvm")
    report = {
        runtime_key: depart.Classification(
            depart.BUCKET_OWNED, depart.ACTION_REMOVE, "absent at baseline, now present"
        )
    }

    live = {"neovim": "0.10.0-1"}

    def run(cmd, **kwargs):
        if cmd[0] == "dpkg-query":
            return install.CommandResult(True, f"neovim\t{live['neovim']}\n")
        if cmd[:4] == ["sudo", "apt-get", "install", "-y"]:
            live["neovim"] = "0.9.5-1"  # partially changed, then fails
            return install.CommandResult(False)
        raise AssertionError(
            f"unexpected command: {cmd!r} — runtime phase must be skipped"
        )

    monkeypatch.setattr(install, "run_command", run)
    ledger = install.execute_departure(ctx, baseline, report)

    assert runtime_key not in ledger.completed_keys()


# ── service/linger execution, end to end through do_depart ─────────────────


def _fresh_managed_services_live(*, linger=False):
    """Initial per-unit + shared-linger state for _watchcommit_run_command.

    Linger is one shared, top-level value (a single machine-wide toggle,
    not owned by any one service) while enabled/active are tracked
    per-unit under ``"units"``.
    """
    return {
        "units": {
            service.unit: {"enabled": False, "active": False}
            for service in install.MANAGED_SERVICES
        },
        "linger": linger,
    }


def _watchcommit_run_command(live, *, other_enabled_units=""):
    """A run_command fake covering every MANAGED_SERVICES unit's full
    enable/disable + linger cycle. ``live`` is the shape returned by
    :func:`_fresh_managed_services_live` — routes each command to its own
    unit's state (never a value shared across units) so a bug that
    hardcodes one unit inside the generalized install.py loop surfaces as
    a test failure instead of being masked by the other unit's state.
    """
    units = live["units"]

    def run(cmd, **kwargs):
        if cmd == ["systemctl", "--user", "show-environment"]:
            return install.CommandResult(True, "")
        if cmd == ["systemctl", "--user", "daemon-reload"]:
            return install.CommandResult(True)
        if cmd == ["loginctl", "show-user", "testuser", "--property=Linger"]:
            return install.CommandResult(
                True, "Linger=yes" if live["linger"] else "Linger=no"
            )
        if cmd == ["loginctl", "enable-linger", "testuser"]:
            live["linger"] = True
            return install.CommandResult(True)
        if cmd == ["loginctl", "disable-linger", "testuser"]:
            live["linger"] = False
            return install.CommandResult(True)
        if cmd == [
            "systemctl",
            "--user",
            "list-unit-files",
            "--state=enabled",
            "--no-legend",
        ]:
            return install.CommandResult(True, other_enabled_units)
        if len(cmd) == 4 and cmd[:3] == ["systemctl", "--user", "is-enabled"]:
            unit = cmd[3]
            if unit not in units:
                raise AssertionError(f"unexpected command: {cmd!r}")
            enabled = units[unit]["enabled"]
            return install.CommandResult(enabled, "enabled" if enabled else "disabled")
        if len(cmd) == 4 and cmd[:3] == ["systemctl", "--user", "is-active"]:
            unit = cmd[3]
            if unit not in units:
                raise AssertionError(f"unexpected command: {cmd!r}")
            active = units[unit]["active"]
            return install.CommandResult(active, "active" if active else "inactive")
        if len(cmd) == 5 and cmd[:4] == ["systemctl", "--user", "enable", "--now"]:
            unit = cmd[4]
            if unit not in units:
                raise AssertionError(f"unexpected command: {cmd!r}")
            units[unit]["enabled"] = True
            units[unit]["active"] = True
            return install.CommandResult(True)
        if len(cmd) == 5 and cmd[:4] == ["systemctl", "--user", "disable", "--now"]:
            unit = cmd[4]
            if unit not in units:
                raise AssertionError(f"unexpected command: {cmd!r}")
            units[unit]["enabled"] = False
            units[unit]["active"] = False
            return install.CommandResult(True)
        raise AssertionError(f"unexpected command: {cmd!r}")

    return run


def test_depart_disables_both_managed_services_and_restores_linger(
    home, links, monkeypatch, capsys
):
    for name in (
        "install_mac_packages",
        "install_linux_packages",
        "install_node",
        "load_watchcommit_agent",
        "import_rectangle_prefs",
        "set_caps_lock_to_escape",
        "install_vim_plug",
        "bootstrap_neovim",
        "capture_git_hooks_path_baseline",
        "install_global_git_hooks_path",
    ):
        monkeypatch.setattr(install, name, lambda *a, **k: None)
    monkeypatch.setattr(install, "install_npm_harness", lambda *a, **k: None)

    live = _fresh_managed_services_live()
    monkeypatch.setattr(install, "run_command", _watchcommit_run_command(live))
    monkeypatch.setattr(install, "have", lambda name: name in ("systemctl", "loginctl"))
    monkeypatch.setattr(install, "_current_user", lambda: "testuser")

    ctx = make_ctx(home, harnesses=("claude",))
    install.run_install(ctx, links)
    assert all(unit["enabled"] and unit["active"] for unit in live["units"].values())
    assert live["linger"] is True

    calls: list[list[str]] = []
    real_run = install.run_command

    def counting_run(cmd, **kwargs):
        calls.append(list(cmd))
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(install, "run_command", counting_run)
    code = install.do_depart(make_ctx(home, yes=True))

    assert code == 0
    assert all(
        not unit["enabled"] and not unit["active"] for unit in live["units"].values()
    )
    assert live["linger"] is False
    assert calls.count(["loginctl", "disable-linger", "testuser"]) == 1

    ledger = depart.DepartureLedger(depart.departure_ledger_path(ctx.state_dir))
    assert not any(
        str(e.get("outcome", "")).startswith("unresolved") for e in ledger.entries()
    )


def test_depart_preserves_linger_when_other_units_depend_on_it(
    home, links, monkeypatch, capsys
):
    for name in (
        "install_mac_packages",
        "install_linux_packages",
        "install_node",
        "load_watchcommit_agent",
        "import_rectangle_prefs",
        "set_caps_lock_to_escape",
        "install_vim_plug",
        "bootstrap_neovim",
        "capture_git_hooks_path_baseline",
        "install_global_git_hooks_path",
    ):
        monkeypatch.setattr(install, name, lambda *a, **k: None)
    monkeypatch.setattr(install, "install_npm_harness", lambda *a, **k: None)

    live = _fresh_managed_services_live()
    run = _watchcommit_run_command(
        live, other_enabled_units="some-other-timer.timer enabled\n"
    )
    monkeypatch.setattr(install, "run_command", run)
    monkeypatch.setattr(install, "have", lambda name: name in ("systemctl", "loginctl"))
    monkeypatch.setattr(install, "_current_user", lambda: "testuser")

    ctx = make_ctx(home, harnesses=("claude",))
    install.run_install(ctx, links)

    code = install.do_depart(make_ctx(home, yes=True))

    # Both units are still fully departed (linger is a separate, decoupled
    # concern — see install-multi-service-depart-adopt-spec.md's Design
    # decision): the "other unit depends on linger" case is advisory-only
    # now, not a ledger-tainting failure, so depart still reports success.
    assert code == 0
    assert all(
        not unit["enabled"] and not unit["active"] for unit in live["units"].values()
    )
    assert live["linger"] is True  # linger correctly preserved

    ledger = depart.DepartureLedger(depart.departure_ledger_path(ctx.state_dir))
    assert not any(
        str(e.get("outcome", "")).startswith("unresolved") for e in ledger.entries()
    )


def test_reconcile_linger_self_heals_on_a_later_depart_after_a_transient_failure(
    home, links, monkeypatch, capsys
):
    """A failed loginctl disable-linger on one --depart run must not
    permanently strand linger enabled: _reconcile_linger recomputes its
    eligibility from live state on every call, not from what this run's
    per-service loop did, so a later --depart run with nothing new to
    disable (both units' ledger keys already complete) still retries and
    succeeds once the underlying problem clears.

    A departure with *nothing else* left unresolved finalizes and deletes
    the baseline/ledger entirely (do_depart's _finalize_departure_state) —
    correct behavior, not a bug — which would leave no state for a second
    --depart call to retry against at all. This test pre-seeds one
    unrelated, permanently-unresolved ledger entry (standing in for any
    real unresolved item — a font-tree conflict, a guarded VS Code file,
    etc.) purely to keep the baseline alive across two --depart
    invocations, so the *services'* own self-healing retry behavior can
    actually be observed independent of that unrelated item's fate.
    """
    for name in (
        "install_mac_packages",
        "install_linux_packages",
        "install_node",
        "load_watchcommit_agent",
        "import_rectangle_prefs",
        "set_caps_lock_to_escape",
        "install_vim_plug",
        "bootstrap_neovim",
        "capture_git_hooks_path_baseline",
        "install_global_git_hooks_path",
    ):
        monkeypatch.setattr(install, name, lambda *a, **k: None)
    monkeypatch.setattr(install, "install_npm_harness", lambda *a, **k: None)

    live = _fresh_managed_services_live()
    base_run = _watchcommit_run_command(live)
    disable_linger_calls = {"count": 0}

    def flaky_run(cmd, **kwargs):
        if cmd == ["loginctl", "disable-linger", "testuser"]:
            disable_linger_calls["count"] += 1
            if disable_linger_calls["count"] == 1:
                return install.CommandResult(False)  # transient failure, once
        return base_run(cmd, **kwargs)

    monkeypatch.setattr(install, "run_command", flaky_run)
    monkeypatch.setattr(install, "have", lambda name: name in ("systemctl", "loginctl"))
    monkeypatch.setattr(install, "_current_user", lambda: "testuser")

    ctx = make_ctx(home, harnesses=("claude",))
    install.run_install(ctx, links)

    ledger = depart.DepartureLedger(depart.departure_ledger_path(ctx.state_dir))
    ledger.record("contrived:unrelated", "noop", "unresolved: kept alive for this test")

    first_code = install.do_depart(make_ctx(home, yes=True))
    assert first_code == 1  # the contrived unrelated item keeps this incomplete
    assert all(
        not unit["enabled"] and not unit["active"] for unit in live["units"].values()
    )
    assert live["linger"] is True  # disable-linger failed — still on
    assert not any(
        str(e.get("outcome", "")).startswith("unresolved")
        for e in ledger.entries()
        if e.get("key") != "contrived:unrelated"
    )  # both services still recorded "ok" despite the linger failure

    second_code = install.do_depart(make_ctx(home, yes=True))
    assert second_code == 1  # the contrived item is permanently stuck, by design
    assert live["linger"] is False  # but linger self-healed: retried and succeeded
    assert disable_linger_calls["count"] == 2


# ── --check-links: read-only links.toml audit ──────────────────────────────


CHECK_LINKS_TOML = """\
[[link]]
src = "zsh/.zshrc"
dest = "~/.zshrc"

[[link]]
src = "claude/CLAUDE.md"
dest = "~/.claude/CLAUDE.md"
harness = "claude"

[[link]]
src = "copilot/instructions.md"
dest = "~/.copilot/instructions.md"
harness = "copilot"

[[link]]
src = "karabiner/karabiner.json"
dest = "~/.config/karabiner/karabiner.json"
platform = "mac"
"""


@pytest.fixture
def fake_repo(tmp_path):
    """A throwaway dotfiles repo with its own links.toml.

    Deliberately not the real REPO_ROOT: these tests delete and rename
    sources to provoke findings, which must never touch tracked files.
    """
    repo = tmp_path / "repo"
    for relative in (
        "zsh/.zshrc",
        "claude/CLAUDE.md",
        "copilot/instructions.md",
        "karabiner/karabiner.json",
    ):
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{relative}\n")
    (repo / "links.toml").write_text(CHECK_LINKS_TOML)
    # The audit now asks whether a checkout is the primary one or a worktree,
    # so the fixtures have to carry the git signature that answers it: a
    # directory here, a `gitdir:` file in other_checkout below.
    (repo / ".git").mkdir()
    return repo


def wire_check_links(ctx, *pairs):
    """Symlink the given (src, dest) relative pairs, recording them as an install."""
    for src, dest in pairs:
        install.symlink(ctx, ctx.dotfiles / src, install.expand_dest(dest, ctx.home))


def snapshot_tree(root):
    """Record every path under ``root`` with enough detail to detect a mutation."""
    state = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            state[str(path)] = ("symlink", os.readlink(path))
        elif path.is_dir():
            state[str(path)] = ("dir", "")
        else:
            state[str(path)] = ("file", path.read_text())
    return state


def check_links_ctx(home, fake_repo, **kwargs):
    kwargs.setdefault("harnesses", ("claude", "copilot"))
    return make_ctx(home, dotfiles=fake_repo, **kwargs)


def test_check_links_clean_state_reports_nothing(home, fake_repo, capsys):
    ctx = check_links_ctx(home, fake_repo)
    wire_check_links(
        ctx,
        ("zsh/.zshrc", "~/.zshrc"),
        ("claude/CLAUDE.md", "~/.claude/CLAUDE.md"),
        ("copilot/instructions.md", "~/.copilot/instructions.md"),
    )
    capsys.readouterr()

    code = install.do_check_links(ctx)

    out = capsys.readouterr().out
    assert code == 0
    assert "every applicable link is" in out
    for bucket in install.CHECK_BUCKETS:
        assert f"{bucket} (" not in out


def test_check_links_detects_broken_source(home, fake_repo, capsys):
    ctx = check_links_ctx(home, fake_repo)
    wire_check_links(ctx, ("zsh/.zshrc", "~/.zshrc"))
    (fake_repo / "zsh" / ".zshrc").unlink()
    capsys.readouterr()

    code = install.do_check_links(ctx)

    out = capsys.readouterr().out
    assert code == 1
    assert "broken-source (1)" in out
    assert "~/.zshrc" in out
    assert "no longer exists in the repo" in out


def test_check_links_detects_orphaned_link(home, fake_repo, capsys):
    """A dest the manifest recorded that links.toml no longer produces."""
    ctx = check_links_ctx(home, fake_repo)
    orphan = home / ".oldrc"
    orphan.symlink_to(fake_repo / "zsh" / ".zshrc")
    ctx.manifest.record_symlink(orphan, fake_repo / "zsh" / ".zshrc")
    capsys.readouterr()

    code = install.do_check_links(ctx)

    out = capsys.readouterr().out
    assert code == 1
    assert "orphaned (1)" in out
    assert "~/.oldrc" in out
    assert "no links.toml entry produces it anymore" in out


def test_check_links_ignores_orphan_already_removed(home, fake_repo, capsys):
    """A recorded dest that a past rollback already deleted is not a finding."""
    ctx = check_links_ctx(home, fake_repo)
    ctx.manifest.record_symlink(home / ".oldrc", fake_repo / "zsh" / ".zshrc")
    capsys.readouterr()

    assert install.do_check_links(ctx) == 0


def test_check_links_ignores_gated_off_entry_as_orphan(home, fake_repo, capsys):
    """A mac-only entry seen from Linux is gated off, not removed from links.toml."""
    ctx = check_links_ctx(home, fake_repo, system="Linux")
    karabiner = home / ".config" / "karabiner" / "karabiner.json"
    karabiner.parent.mkdir(parents=True)
    karabiner.symlink_to(fake_repo / "karabiner" / "karabiner.json")
    ctx.manifest.record_symlink(karabiner, fake_repo / "karabiner" / "karabiner.json")
    capsys.readouterr()

    assert install.do_check_links(ctx) == 0


def test_check_links_detects_wrong_target(home, fake_repo, capsys):
    ctx = check_links_ctx(home, fake_repo)
    dest = home / ".zshrc"
    dest.symlink_to(fake_repo / "claude" / "CLAUDE.md")
    capsys.readouterr()

    code = install.do_check_links(ctx)

    out = capsys.readouterr().out
    assert code == 1
    assert "wrong-target (1)" in out
    assert f"but links.toml says {fake_repo / 'zsh' / '.zshrc'}" in out


def make_checkout(root, *, main):
    """Build a second dotfiles checkout that _is_dotfiles_checkout() accepts.

    `main` picks the git signature that tells the two apart on disk: a primary
    checkout carries `.git` as a directory, a `git worktree add` checkout
    carries it as a file holding a `gitdir:` pointer. That is the whole
    discriminator, and it needs no subprocess -- which matters here, since the
    suite blocks real subprocess calls.
    """
    for relative in ("zsh/.zshrc", "claude/CLAUDE.md"):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{relative}\n")
    (root / "links.toml").write_text(CHECK_LINKS_TOML)
    (root / "install.py").write_text("# stand-in\n")
    if main:
        (root / ".git").mkdir()
    else:
        (root / ".git").write_text("gitdir: /somewhere/.git/worktrees/wt\n")
    return root


# THE HAZARD, pinned. Three times now a live link has been hand-pointed into a
# worktree for a live verification and left there; when the worktree was
# removed the link dangled and the extension silently stopped loading
# (custom-footer.ts, then permission-gate.ts and swarm-tool.ts on 2026-09-02).
# The audit excused every one of them: it only asked "a different checkout?",
# never "which one is canonical?", so it filed them as a benign note and
# exited 0 while quietly dropping them from the audited count.
def test_check_links_reports_a_link_pointing_into_a_worktree(home, tmp_path, capsys):
    main = make_checkout(tmp_path / "repo", main=True)
    worktree = make_checkout(tmp_path / "repo-wt", main=False)
    ctx = check_links_ctx(home, main)
    dest = home / ".zshrc"
    dest.symlink_to(worktree / "zsh" / ".zshrc")
    capsys.readouterr()

    code = install.do_check_links(ctx)

    out = capsys.readouterr().out
    assert code == 1
    assert "wrong-target (1)" in out
    assert str(worktree / "zsh" / ".zshrc") in out


# The inverse direction is normal and must stay silent: this repo mandates
# worktree-first development, so auditing from a worktree while the machine's
# links point at the main checkout is the expected state, not drift.
def test_check_links_stays_quiet_auditing_from_a_worktree(home, tmp_path, capsys):
    main = make_checkout(tmp_path / "repo", main=True)
    worktree = make_checkout(tmp_path / "repo-wt", main=False)
    ctx = check_links_ctx(home, worktree)
    dest = home / ".zshrc"
    dest.symlink_to(main / "zsh" / ".zshrc")
    capsys.readouterr()

    code = install.do_check_links(ctx)

    out = capsys.readouterr().out
    assert code == 0
    assert "wrong-target" not in out


def test_check_links_detects_real_file_where_a_link_belongs(home, fake_repo, capsys):
    ctx = check_links_ctx(home, fake_repo)
    (home / ".zshrc").write_text("hand-written\n")
    capsys.readouterr()

    code = install.do_check_links(ctx)

    out = capsys.readouterr().out
    assert code == 1
    assert "not-a-symlink (1)" in out
    assert (
        f"a real file sits where a symlink to {fake_repo / 'zsh' / '.zshrc'} "
        "belongs" in out
    )


def test_check_links_ignores_unrelated_files(home, fake_repo, capsys):
    """Files and symlinks the installer never owned are none of its business."""
    ctx = check_links_ctx(home, fake_repo)
    wire_check_links(ctx, ("zsh/.zshrc", "~/.zshrc"))
    (home / ".bashrc").write_text("not ours\n")
    (home / ".dangling").symlink_to(home / "nowhere")
    (home / "notes").mkdir()
    capsys.readouterr()

    assert install.do_check_links(ctx) == 0


def test_check_links_ignores_uninstalled_entries(home, fake_repo, capsys):
    """A dest that simply was never installed is not yet a problem."""
    ctx = check_links_ctx(home, fake_repo)
    capsys.readouterr()

    assert install.do_check_links(ctx) == 0


def test_check_links_without_harness_widens_to_every_harness(home, fake_repo, capsys):
    """No --harness audits all harnesses, so a copilot link is still checked."""
    wiring = check_links_ctx(home, fake_repo)
    wire_check_links(wiring, ("copilot/instructions.md", "~/.copilot/instructions.md"))
    (fake_repo / "copilot" / "instructions.md").unlink()
    capsys.readouterr()

    code = install.do_check_links(check_links_ctx(home, fake_repo, harnesses=()))

    out = capsys.readouterr().out
    assert code == 1
    assert "broken-source (1)" in out
    assert "~/.copilot/instructions.md" in out


def test_check_links_reports_several_buckets_at_once(home, fake_repo, capsys):
    ctx = check_links_ctx(home, fake_repo)
    wire_check_links(
        ctx,
        ("zsh/.zshrc", "~/.zshrc"),
        ("claude/CLAUDE.md", "~/.claude/CLAUDE.md"),
    )
    (fake_repo / "zsh" / ".zshrc").unlink()
    orphan = home / ".oldrc"
    orphan.symlink_to(fake_repo / "claude" / "CLAUDE.md")
    ctx.manifest.record_symlink(orphan, fake_repo / "claude" / "CLAUDE.md")
    capsys.readouterr()

    code = install.do_check_links(ctx)

    out = capsys.readouterr().out
    assert code == 1
    assert "broken-source (1)" in out
    assert "orphaned (1)" in out
    assert "2 link problem(s) found" in out


def test_check_links_changes_nothing_on_disk(home, fake_repo, capsys):
    """The whole point: an audit that finds problems still fixes none of them."""
    ctx = check_links_ctx(home, fake_repo)
    wire_check_links(
        ctx,
        ("zsh/.zshrc", "~/.zshrc"),
        ("claude/CLAUDE.md", "~/.claude/CLAUDE.md"),
    )
    (fake_repo / "zsh" / ".zshrc").unlink()
    (home / ".copilot").mkdir()
    (home / ".copilot" / "instructions.md").write_text("hand-written\n")
    orphan = home / ".oldrc"
    orphan.symlink_to(fake_repo / "claude" / "CLAUDE.md")
    ctx.manifest.record_symlink(orphan, fake_repo / "claude" / "CLAUDE.md")

    before_home = snapshot_tree(home)
    before_repo = snapshot_tree(fake_repo)

    assert install.do_check_links(ctx) == 1

    assert snapshot_tree(home) == before_home
    assert snapshot_tree(fake_repo) == before_repo


def test_check_links_missing_links_toml_exits_2(home, tmp_path, monkeypatch, capsys):
    empty_repo = tmp_path / "empty"
    empty_repo.mkdir()
    ctx = make_ctx(home, dotfiles=empty_repo)
    monkeypatch.setattr(install, "build_context", lambda opts: ctx)

    code = install.main(["--check-links"])

    assert code == 2
    assert "could not read the symlink table" in capsys.readouterr().err


def test_main_dispatches_check_links_without_installing(
    home, fake_repo, monkeypatch, capsys
):
    ctx = check_links_ctx(home, fake_repo)
    monkeypatch.setattr(install, "build_context", lambda opts: ctx)
    monkeypatch.setattr(
        install,
        "run_install",
        lambda *a, **k: pytest.fail("--check-links must not run the installer"),
    )

    assert install.main(["--check-links"]) == 0
    assert "links.toml audit (read-only)" in capsys.readouterr().out


def test_check_links_alone_parses():
    opts = install.parse_args(["--check-links"])
    assert opts.check_links is True
    assert opts.harnesses == ()


def test_check_links_accepts_harness_and_profile():
    opts = install.parse_args(["--check-links", "--harness=claude", "--profile=work"])
    assert opts.check_links is True
    assert opts.harnesses == ("claude",)
    assert opts.profile == "work"


def test_check_links_accepts_report_uninstalled():
    opts = install.parse_args(["--check-links", "--report-uninstalled"])
    assert opts.check_links is True
    assert opts.report_uninstalled is True


@pytest.mark.parametrize(
    "argv",
    [
        ["--check-links", "--rollback"],
        ["--check-links", "--wipe"],
        ["--check-links", "--force"],
        ["--check-links", "--reseed"],
        ["--check-links", "--no-nvim-pin"],
        ["--check-links", "--dry-run"],
    ],
)
def test_check_links_rejects_mutating_flags(argv, capsys):
    code, err = parse_error(argv, capsys)
    assert code == 2
    assert "--check-links must be used alone, apart from --harness and --profile" in err


def test_depart_takes_precedence_over_check_links(capsys):
    """--depart --check-links names the --depart conflict, matching --rollback."""
    code, err = parse_error(["--depart", "--check-links"], capsys)
    assert code == 2
    assert "--depart must be used alone" in err


def test_usage_documents_check_links(capsys):
    with pytest.raises(SystemExit):
        install.parse_args(["--help"])
    assert "--check-links" in capsys.readouterr().out


def test_usage_mentions_every_check_links_bucket(capsys):
    """CHECK_BUCKETS has grown without the hand-written --help text keeping
    pace before (twice: unmanaged, then never-installed) and nothing caught
    it either time. This makes a future miss a hard test failure instead of
    something a human has to notice by hand."""
    with pytest.raises(SystemExit):
        install.parse_args(["--help"])
    out = capsys.readouterr().out
    for bucket in install.CHECK_BUCKETS:
        assert bucket in out, f"--help text is missing the {bucket!r} bucket"


# ── --check-links --report-uninstalled: never-installed detection ──────────


def test_check_links_never_installed_fires_with_flag(home, fake_repo, capsys):
    """Applicable, src exists, dest missing, no manifest record: a real gap."""
    ctx = check_links_ctx(home, fake_repo, report_uninstalled=True)
    capsys.readouterr()

    code = install.do_check_links(ctx)

    out = capsys.readouterr().out
    assert code == 1
    assert "never-installed" in out


def test_check_links_never_installed_silent_without_flag(home, fake_repo, capsys):
    """Same gap, but the opt-in flag was never passed: default stays silent."""
    ctx = check_links_ctx(home, fake_repo)
    capsys.readouterr()

    code = install.do_check_links(ctx)

    out = capsys.readouterr().out
    assert code == 0
    assert "never-installed" not in out


def test_check_links_never_installed_silent_when_manifest_recorded(
    home, fake_repo, capsys
):
    """A dest with prior manifest history is a legitimate removal, not a gap.

    The claude and copilot rows are also wired so they cannot fire and mask
    the assertion about zsh specifically.
    """
    ctx = check_links_ctx(home, fake_repo, report_uninstalled=True)
    wire_check_links(
        ctx,
        ("zsh/.zshrc", "~/.zshrc"),
        ("claude/CLAUDE.md", "~/.claude/CLAUDE.md"),
        ("copilot/instructions.md", "~/.copilot/instructions.md"),
    )
    (home / ".zshrc").unlink()
    capsys.readouterr()

    code = install.do_check_links(ctx)

    out = capsys.readouterr().out
    assert code == 0
    assert "never-installed" not in out


def test_check_links_never_installed_silent_for_gated_off_row(home, fake_repo, capsys):
    """A row never applicable on this machine never counts as never-installed.

    Scoped to just ``claude`` so the ``copilot`` row is not applicable, while
    the zsh and claude rows are wired so they cannot also fire and mask the
    assertion.
    """
    ctx = check_links_ctx(
        home, fake_repo, harnesses=("claude",), report_uninstalled=True
    )
    wire_check_links(
        ctx,
        ("zsh/.zshrc", "~/.zshrc"),
        ("claude/CLAUDE.md", "~/.claude/CLAUDE.md"),
    )
    capsys.readouterr()

    code = install.do_check_links(ctx)

    out = capsys.readouterr().out
    assert code == 0
    assert "never-installed" not in out


def test_check_links_never_installed_silent_for_dual_fault(home, fake_repo, capsys):
    """No dest and no src: a different, pre-existing, out-of-scope gap.

    The claude and copilot rows are also wired so they cannot fire and mask
    the assertion about zsh specifically.
    """
    ctx = check_links_ctx(home, fake_repo, report_uninstalled=True)
    wire_check_links(
        ctx,
        ("claude/CLAUDE.md", "~/.claude/CLAUDE.md"),
        ("copilot/instructions.md", "~/.copilot/instructions.md"),
    )
    (fake_repo / "zsh" / ".zshrc").unlink()
    capsys.readouterr()

    code = install.do_check_links(ctx)

    out = capsys.readouterr().out
    assert code == 0
    assert "never-installed" not in out


def test_check_links_never_installed_ignores_dangling_symlink(home, fake_repo, capsys):
    """A dangling symlink is broken-source, never never-installed, flag or not.

    claude and copilot are also wired so they cannot fire never-installed
    and mask the assertion about zsh's dangling symlink.
    """
    ctx = check_links_ctx(home, fake_repo, report_uninstalled=True)
    wire_check_links(
        ctx,
        ("zsh/.zshrc", "~/.zshrc"),
        ("claude/CLAUDE.md", "~/.claude/CLAUDE.md"),
        ("copilot/instructions.md", "~/.copilot/instructions.md"),
    )
    (fake_repo / "zsh" / ".zshrc").unlink()
    capsys.readouterr()

    code = install.do_check_links(ctx)

    out = capsys.readouterr().out
    assert code == 1
    assert "broken-source (1)" in out
    assert "never-installed" not in out


def test_check_links_never_installed_ignores_real_file_in_the_way(
    home, fake_repo, capsys
):
    """A real file where a symlink belongs is not-a-symlink, never never-installed.

    claude and copilot are also wired so they cannot fire never-installed
    and mask the assertion about zsh's real-file collision.
    """
    ctx = check_links_ctx(home, fake_repo, report_uninstalled=True)
    wire_check_links(
        ctx,
        ("claude/CLAUDE.md", "~/.claude/CLAUDE.md"),
        ("copilot/instructions.md", "~/.copilot/instructions.md"),
    )
    (home / ".zshrc").write_text("hand-written\n")
    capsys.readouterr()

    code = install.do_check_links(ctx)

    out = capsys.readouterr().out
    assert code == 1
    assert "not-a-symlink (1)" in out
    assert "never-installed" not in out


# ── --check-links: unmanaged files in declared-exclusive directories ────────

# One declared directory is pi-only (every row inside it carries
# harness = "pi"), the other carries no harness at all, so the two exercise
# both sides of the inherited-scoping rule.
MANAGED_DIR_TOML = """\
[[link]]
src = "ext/one.ts"
dest = "~/.pi/agent/extensions/one.ts"
harness = "pi"

[[link]]
src = "scripts/one.py"
dest = "~/.claude/scripts/one.py"

[[managed_dir]]
dest = "~/.pi/agent/extensions"

[[managed_dir]]
dest = "~/.claude/scripts"
"""


@pytest.fixture
def managed_repo(tmp_path):
    """A fake repo whose links.toml declares two exclusive directories."""
    repo = tmp_path / "repo"
    for relative in ("ext/one.ts", "scripts/one.py"):
        path = repo / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{relative}\n")
    (repo / "links.toml").write_text(MANAGED_DIR_TOML)
    return repo


def test_check_links_declared_dirs_clean_report_nothing(home, managed_repo, capsys):
    """A declared directory holding only what links.toml produces stays quiet."""
    ctx = check_links_ctx(home, managed_repo, harnesses=("pi", "claude", "copilot"))
    wire_check_links(
        ctx,
        ("ext/one.ts", "~/.pi/agent/extensions/one.ts"),
        ("scripts/one.py", "~/.claude/scripts/one.py"),
    )
    capsys.readouterr()

    assert install.do_check_links(ctx) == 0

    assert "unmanaged (" not in capsys.readouterr().out


def test_check_links_reports_foreign_file_in_declared_dir(home, managed_repo, capsys):
    ctx = check_links_ctx(home, managed_repo, harnesses=("pi",))
    wire_check_links(ctx, ("ext/one.ts", "~/.pi/agent/extensions/one.ts"))
    (home / ".pi/agent/extensions/stray.ts").write_text("not ours\n")
    capsys.readouterr()

    code = install.do_check_links(ctx)

    out = capsys.readouterr().out
    assert code == 1
    assert "unmanaged (1)" in out
    assert "~/.pi/agent/extensions/stray.ts" in out
    assert "no links.toml entry produces it" in out


def test_check_links_skips_ignored_files_in_declared_dir(home, tmp_path, capsys):
    """Foreign files matching a declared directory's ignore list are skipped."""
    repo = tmp_path / "repo"
    (repo / "ext").mkdir(parents=True)
    (repo / "ext/one.ts").write_text("ext/one.ts\n")
    (repo / "links.toml").write_text(
        "[[link]]\n"
        'src = "ext/one.ts"\n'
        'dest = "~/.pi/agent/extensions/one.ts"\n'
        'harness = "pi"\n'
        "\n"
        "[[managed_dir]]\n"
        'dest = "~/.pi/agent/extensions"\n'
        'ignore = ["herdr-*.ts", "special.json"]\n'
    )
    ctx = check_links_ctx(home, repo, harnesses=("pi",))
    wire_check_links(ctx, ("ext/one.ts", "~/.pi/agent/extensions/one.ts"))
    (home / ".pi/agent/extensions/herdr-agent-state.ts").write_text("external\n")
    (home / ".pi/agent/extensions/special.json").write_text("external\n")
    capsys.readouterr()

    assert install.do_check_links(ctx) == 0
    assert "unmanaged (" not in capsys.readouterr().out

    # Non-ignored foreign files are still caught
    (home / ".pi/agent/extensions/stray.ts").write_text("stray\n")
    capsys.readouterr()

    assert install.do_check_links(ctx) == 1
    out = capsys.readouterr().out
    assert "unmanaged (1)" in out
    assert "~/.pi/agent/extensions/stray.ts" in out
    assert "herdr-agent-state.ts" not in out


def test_check_links_skips_manifest_recorded_backup(home, managed_repo, capsys):
    """A backup whose destination is still present is live --rollback payload.

    Telling the user to delete it would destroy the only copy of their
    pre-dotfiles original, so it must never read as a foreign file.
    """
    ctx = check_links_ctx(home, managed_repo, harnesses=("claude",))
    target = home / ".claude/scripts/one.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("the user's original\n")

    install.symlink(ctx, managed_repo / "scripts/one.py", target)

    assert target.is_symlink()
    assert (home / ".claude/scripts/one.py.bak").exists()
    capsys.readouterr()

    assert install.do_check_links(ctx) == 0

    assert "unmanaged (" not in capsys.readouterr().out


def test_check_links_reports_unrecorded_bak_in_declared_dir(home, managed_repo, capsys):
    """A .bak the manifest never recorded is a stray, not rollback payload."""
    ctx = check_links_ctx(home, managed_repo, harnesses=("claude",))
    wire_check_links(ctx, ("scripts/one.py", "~/.claude/scripts/one.py"))
    (home / ".claude/scripts/one.py.bak").write_text("hand-made\n")
    capsys.readouterr()

    assert install.do_check_links(ctx) == 1

    assert "one.py.bak" in capsys.readouterr().out


def test_check_links_absent_declared_dir_is_silent(home, managed_repo, capsys):
    """A directory that was never provisioned here has nothing to audit."""
    ctx = check_links_ctx(home, managed_repo, harnesses=("pi",))
    capsys.readouterr()

    assert install.do_check_links(ctx) == 0

    assert "unmanaged (" not in capsys.readouterr().out


def test_check_links_declared_dir_inherits_harness_from_its_rows(
    home, managed_repo, capsys
):
    """Scoping the run to claude must not audit a pi-only directory."""
    wire_ctx = check_links_ctx(home, managed_repo, harnesses=("pi",))
    wire_check_links(wire_ctx, ("ext/one.ts", "~/.pi/agent/extensions/one.ts"))
    (home / ".pi/agent/extensions/stray.ts").write_text("not ours\n")

    claude_only = check_links_ctx(home, managed_repo, harnesses=("claude",))
    capsys.readouterr()
    assert install.do_check_links(claude_only) == 0
    assert "unmanaged (" not in capsys.readouterr().out

    with_pi = check_links_ctx(home, managed_repo, harnesses=("claude", "pi"))
    capsys.readouterr()
    assert install.do_check_links(with_pi) == 1
    assert "unmanaged (1)" in capsys.readouterr().out


def test_check_links_unharnessed_declared_dir_audited_under_any_harness(
    home, managed_repo, capsys
):
    """A declared directory whose rows carry no harness is always in scope."""
    ctx = check_links_ctx(home, managed_repo, harnesses=("copilot",))
    wire_check_links(ctx, ("scripts/one.py", "~/.claude/scripts/one.py"))
    (home / ".claude/scripts/stray.py").write_text("not ours\n")
    capsys.readouterr()

    assert install.do_check_links(ctx) == 1

    assert ".claude/scripts/stray.py" in capsys.readouterr().out


def test_check_links_skips_directories_and_junk_in_declared_dir(
    home, managed_repo, capsys
):
    ctx = check_links_ctx(home, managed_repo, harnesses=("claude",))
    wire_check_links(ctx, ("scripts/one.py", "~/.claude/scripts/one.py"))
    scripts = home / ".claude/scripts"
    (scripts / ".ruff_cache").mkdir()
    (scripts / "__pycache__").mkdir()
    (scripts / "notes.py~").write_text("editor swap\n")
    (scripts / ".hidden").write_text("hidden\n")
    capsys.readouterr()

    assert install.do_check_links(ctx) == 0

    assert "unmanaged (" not in capsys.readouterr().out


def test_check_links_reports_unreadable_declared_dir(home, managed_repo, capsys):
    """A declared directory that cannot be read was not verified, so it fails."""
    if os.geteuid() == 0:
        pytest.skip("root can read a 000 directory, so the fault cannot occur")

    ctx = check_links_ctx(home, managed_repo, harnesses=("claude",))
    wire_check_links(ctx, ("scripts/one.py", "~/.claude/scripts/one.py"))
    scripts = home / ".claude/scripts"
    mode = scripts.stat().st_mode
    capsys.readouterr()
    try:
        scripts.chmod(0o000)
        code = install.do_check_links(ctx)
        out = capsys.readouterr().out
    finally:
        scripts.chmod(mode)

    assert code == 1
    assert "could not be audited" in out


def test_load_managed_dirs_parses_and_rejects(tmp_path):
    path = tmp_path / "links.toml"

    path.write_text(
        "[[managed_dir]]\n"
        'dest = "~/.claude/scripts"\n'
        "\n"
        "[[managed_dir]]\n"
        'dest = "~/.pi/agent/extensions"\n'
        'ignore = ["herdr-agent-state.ts", "*.tmp"]\n'
    )
    specs = install.load_managed_dirs(path)
    assert [spec.dest for spec in specs] == [
        "~/.claude/scripts",
        "~/.pi/agent/extensions",
    ]
    assert specs[0].ignore == ()
    assert specs[1].ignore == ("herdr-agent-state.ts", "*.tmp")

    path.write_text("")
    assert install.load_managed_dirs(path) == []


@pytest.mark.parametrize(
    "bad",
    [
        '[[managed_dir]]\ndest = "~/.x"\nharness = "pi"\n',
        "[[managed_dir]]\n",
        '[[managed_dir]]\ndest = ""\n',
        '[[managed_dir]] = "not a table"\n',
        '[[managed_dir]]\ndest = "~/.x"\nignore = "not-a-list"\n',
        '[[managed_dir]]\ndest = "~/.x"\nignore = [123]\n',
        '[[managed_dir]]\ndest = "~/.x"\nignore = [""]\n',
    ],
)
def test_load_managed_dirs_rejects_invalid_rows(tmp_path, bad):
    path = tmp_path / "links.toml"
    path.write_text(bad)

    with pytest.raises((ValueError, TypeError)):
        install.load_managed_dirs(path)


# Measured 2026-08-31: inferring managed directories from every link's
# dest.parent surfaces 230 unmanaged entries, 68 of them in $HOME alone. That
# is why exclusivity is opt-in, and why widening it has to be a deliberate act.
_FORBIDDEN_MANAGED_DIRS = frozenset({"~", "~/.pi/agent", "~/.claude/output-styles"})


def test_declared_managed_dirs_stay_narrow():
    """Pin the declared set itself, not the state of any one machine.

    CI runs against a fake home and so cannot catch a too-wide declaration;
    without this, adding dest = "~" would pass CI and then fail on every real
    machine. This reads the repo's own links.toml, not user state.
    """
    home = Path.home()

    for spec in install.load_managed_dirs(REPO_ROOT / "links.toml"):
        assert spec.dest not in _FORBIDDEN_MANAGED_DIRS, spec.dest
        expanded = install.expand_dest(spec.dest, home)
        assert expanded != home, f"{spec.dest} declares $HOME itself"
        assert expanded.is_relative_to(home), f"{spec.dest} escapes $HOME"


# ── --check-links: message units and the worktree/second-checkout case ──────


@pytest.fixture
def other_checkout(tmp_path, fake_repo):
    """A second checkout of the same repo, the way a git worktree looks.

    The audit's fake_repo fixtures all share one root, which is exactly why
    a real smoke test from a worktree — where every live link points at the
    main checkout — surfaced 28 bogus wrong-target findings that no unit
    test could have caught.
    """
    other = tmp_path / "repo-worktree"
    for path in sorted(fake_repo.rglob("*")):
        if path.is_dir():
            continue
        target = other / path.relative_to(fake_repo)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(path.read_text())
    # `git worktree add` leaves `.git` as a pointer file, not a directory --
    # what tells this checkout apart from the primary one.
    (other / ".git").unlink(missing_ok=True)
    (other / ".git").write_text("gitdir: /somewhere/.git/worktrees/wt\n")
    (other / "install.py").write_text("# stub\n")
    (fake_repo / "install.py").write_text("# stub\n")
    return other


def test_check_links_messages_print_absolute_expected_paths(home, fake_repo, capsys):
    """A finding must show the path the comparison actually used, not spec.src.

    Printing the raw relative src next to a resolved absolute target makes a
    correct finding read like a tool bug, and hides the real cause.
    """
    ctx = check_links_ctx(home, fake_repo)
    (home / ".zshrc").symlink_to(fake_repo / "claude" / "CLAUDE.md")
    (home / ".claude").mkdir()
    (home / ".claude" / "CLAUDE.md").write_text("hand-written\n")
    capsys.readouterr()

    assert install.do_check_links(ctx) == 1

    out = capsys.readouterr().out
    assert f"but links.toml says {fake_repo / 'zsh' / '.zshrc'}" in out
    assert f"a symlink to {fake_repo / 'claude' / 'CLAUDE.md'} belongs" in out
    # The bare relative form must not appear as the expected value anywhere.
    assert "says zsh/.zshrc" not in out
    assert "symlink to claude/CLAUDE.md belongs" not in out


def test_check_links_broken_source_message_is_absolute(home, fake_repo, capsys):
    ctx = check_links_ctx(home, fake_repo)
    wire_check_links(ctx, ("zsh/.zshrc", "~/.zshrc"))
    (fake_repo / "zsh" / ".zshrc").unlink()
    capsys.readouterr()

    assert install.do_check_links(ctx) == 1
    assert f"links to {fake_repo / 'zsh' / '.zshrc'}" in capsys.readouterr().out


def test_check_links_from_a_worktree_collapses_to_one_note(
    home, fake_repo, other_checkout, capsys
):
    """Auditing from a worktree must not drown the report in wrong-target lines."""
    wiring = check_links_ctx(home, fake_repo)
    wire_check_links(
        wiring,
        ("zsh/.zshrc", "~/.zshrc"),
        ("claude/CLAUDE.md", "~/.claude/CLAUDE.md"),
        ("copilot/instructions.md", "~/.copilot/instructions.md"),
    )
    capsys.readouterr()

    code = install.do_check_links(check_links_ctx(home, other_checkout))

    out = capsys.readouterr().out
    assert code == 0, "a worktree audit of a healthy machine is not a failure"
    assert "wrong-target" not in out
    assert f"note: 3 link(s) point into {fake_repo}" in out
    assert f"rather than this checkout ({other_checkout})" in out
    assert "running from a worktree" in out


def test_check_links_worktree_note_reports_entries_not_audited(
    home, fake_repo, other_checkout, capsys
):
    wiring = check_links_ctx(home, fake_repo)
    wire_check_links(wiring, ("zsh/.zshrc", "~/.zshrc"))
    capsys.readouterr()

    assert install.do_check_links(check_links_ctx(home, other_checkout)) == 0
    # 4 entries in links.toml, 1 of them excluded as pointing at the other
    # checkout — the count must show what was actually looked at.
    assert "3 of 4 entries checked" in capsys.readouterr().out


def test_check_links_dangling_link_into_another_checkout_still_reported(
    home, fake_repo, other_checkout, capsys
):
    """A dead link is a real problem whichever checkout it aims at."""
    wiring = check_links_ctx(home, fake_repo)
    wire_check_links(wiring, ("zsh/.zshrc", "~/.zshrc"))
    (fake_repo / "zsh" / ".zshrc").unlink()
    capsys.readouterr()

    code = install.do_check_links(check_links_ctx(home, other_checkout))

    out = capsys.readouterr().out
    assert code == 1
    assert "broken-source (1)" in out
    assert f"links to {fake_repo / 'zsh' / '.zshrc'}" in out
    assert "dangling symlink" in out


def test_check_links_worktree_note_does_not_mask_real_findings(
    home, fake_repo, other_checkout, capsys
):
    """The note is informational; genuine findings still drive exit 1."""
    wiring = check_links_ctx(home, fake_repo)
    wire_check_links(wiring, ("zsh/.zshrc", "~/.zshrc"))
    orphan = home / ".oldrc"
    orphan.symlink_to(fake_repo / "zsh" / ".zshrc")
    wiring.manifest.record_symlink(orphan, fake_repo / "zsh" / ".zshrc")
    capsys.readouterr()

    code = install.do_check_links(check_links_ctx(home, other_checkout))

    out = capsys.readouterr().out
    assert code == 1
    assert "note: 1 link(s) point into" in out
    assert "orphaned (1)" in out
    assert "1 link problem(s) found" in out


def test_check_links_unrelated_target_is_still_wrong_target(
    home, fake_repo, tmp_path, capsys
):
    """Only *another checkout of this repo* gets the benefit of the doubt."""
    elsewhere = tmp_path / "elsewhere"
    (elsewhere / "zsh").mkdir(parents=True)
    (elsewhere / "zsh" / ".zshrc").write_text("someone else's\n")
    ctx = check_links_ctx(home, fake_repo)
    (home / ".zshrc").symlink_to(elsewhere / "zsh" / ".zshrc")
    capsys.readouterr()

    code = install.do_check_links(ctx)

    out = capsys.readouterr().out
    assert code == 1, "a non-checkout directory is not a worktree, it is a bad link"
    assert "wrong-target (1)" in out


def test_implied_repo_root_requires_a_full_tail_match():
    assert install._implied_repo_root(Path("/a/b/zsh/.zshrc"), "zsh/.zshrc") == Path(
        "/a/b"
    )
    assert install._implied_repo_root(Path("/a/b/other/.zshrc"), "zsh/.zshrc") is None
    assert install._implied_repo_root(Path("/zsh/.zshrc"), "zsh/.zshrc") == Path("/")
    assert install._implied_repo_root(Path("/zsh"), "zsh/.zshrc") is None


# ── dir=true directory-glob rows (Fidelity local-skill-fork mechanism) ─────────


def test_load_links_accepts_dir_true(tmp_path):
    toml = tmp_path / "links.toml"
    toml.write_text('[[link]]\nsrc = "local/x"\ndest = "~/x"\ndir = true\n')

    specs = install.load_links(toml)

    assert specs == [install.LinkSpec(src="local/x", dest="~/x", dir=True)]


def test_load_links_rejects_non_bool_dir(tmp_path):
    toml = tmp_path / "links.toml"
    toml.write_text('[[link]]\nsrc = "local/x"\ndest = "~/x"\ndir = "yes"\n')

    with pytest.raises(TypeError, match="dir"):
        install.load_links(toml)


@pytest.fixture
def dir_repo(tmp_path):
    """A throwaway dotfiles repo with a work-only ``dir=true`` row.

    Mirrors the real design: the row is scoped to profile="work" so it's a
    no-op on personal, and its source directory starts empty — individual
    tests populate it.
    """
    repo = tmp_path / "dir-repo"
    (repo / "local" / "claude" / "commands").mkdir(parents=True)
    (repo / "install.py").write_text("# stub\n")
    (repo / "links.toml").write_text(
        '[[link]]\nsrc = "local/claude/commands"\ndir = true\n'
        'dest = "~/.claude/commands"\nprofile_exclude = ["personal"]\n'
    )
    # Primary checkout: `.git` is a directory (see fake_repo).
    (repo / ".git").mkdir()
    return repo


def dir_repo_ctx(home, dir_repo, *, profile="work", **kwargs):
    return make_ctx(home, dotfiles=dir_repo, profile=profile, **kwargs)


def dir_true_spec(**overrides):
    fields = {
        "src": "local/claude/commands",
        "dest": "~/.claude/commands",
        "dir": True,
        "profile_exclude": ("personal",),
    }
    fields.update(overrides)
    return install.LinkSpec(**fields)


def test_dir_true_row_personal_profile_skips_entirely(home, dir_repo, offline_install):
    (dir_repo / "local" / "claude" / "commands" / "foo.md").write_text("foo\n")
    ctx = dir_repo_ctx(home, dir_repo, profile="personal")

    install.run_install(ctx, [dir_true_spec()])

    assert not (home / ".claude" / "commands").exists()
    assert history(ctx) == [] or kinds(ctx, "symlink-created") == []


def test_dir_true_row_creates_symlink_per_file_including_nested(
    home, dir_repo, offline_install
):
    root = dir_repo / "local" / "claude" / "commands"
    (root / "foo.md").write_text("foo\n")
    (root / "sub").mkdir()
    (root / "sub" / "bar.md").write_text("bar\n")
    ctx = dir_repo_ctx(home, dir_repo)

    install.run_install(ctx, [dir_true_spec()])

    dest_root = home / ".claude" / "commands"
    assert (dest_root / "foo.md").resolve() == (root / "foo.md").resolve()
    assert (dest_root / "sub" / "bar.md").resolve() == (
        root / "sub" / "bar.md"
    ).resolve()
    assert len(kinds(ctx, "symlink-created")) == 2


def test_dir_true_row_missing_source_dir_no_error_zero_links(
    home, dir_repo, offline_install
):
    import shutil as _shutil

    _shutil.rmtree(dir_repo / "local" / "claude" / "commands")
    ctx = dir_repo_ctx(home, dir_repo)

    install.run_install(ctx, [dir_true_spec()])

    assert not (home / ".claude" / "commands").exists()
    assert kinds(ctx, "symlink-created") == []


def test_dir_true_row_rerun_no_duplicate_manifest_entries(
    home, dir_repo, offline_install
):
    (dir_repo / "local" / "claude" / "commands" / "foo.md").write_text("foo\n")
    ctx = dir_repo_ctx(home, dir_repo)

    install.run_install(ctx, [dir_true_spec()])
    install.run_install(ctx, [dir_true_spec()])

    assert len(kinds(ctx, "symlink-created")) == 1


def test_dir_true_row_filters_hidden_and_junk_files(home, dir_repo, offline_install):
    root = dir_repo / "local" / "claude" / "commands"
    (root / "foo.md").write_text("foo\n")
    (root / ".DS_Store").write_text("junk\n")
    (root / "foo.md.swp").write_text("junk\n")
    ctx = dir_repo_ctx(home, dir_repo)

    install.run_install(ctx, [dir_true_spec()])

    dest_root = home / ".claude" / "commands"
    assert {p.name for p in dest_root.iterdir()} == {"foo.md"}


def test_dir_true_row_full_subdirectory_deletion_cleans_up_all_symlinks(
    home, dir_repo, offline_install, capsys
):
    root = dir_repo / "local" / "claude" / "commands"
    (root / "foo.md").write_text("foo\n")
    (root / "bar.md").write_text("bar\n")
    ctx = dir_repo_ctx(home, dir_repo)
    install.run_install(ctx, [dir_true_spec()])
    assert len(kinds(ctx, "symlink-created")) == 2

    import shutil as _shutil

    _shutil.rmtree(root)
    capsys.readouterr()

    install.run_install(ctx, [dir_true_spec()])

    out = capsys.readouterr().out
    dest_root = home / ".claude" / "commands"
    assert not (dest_root / "foo.md").exists()
    assert not (dest_root / "bar.md").exists()
    assert not dest_root.exists(), "now-empty parent directory should be pruned"
    assert kinds(ctx, "symlink-created") == []
    assert out.count("removed orphaned symlink") == 2


def test_plain_install_auto_removes_orphaned_symlink_and_prints_it(
    home, dir_repo, offline_install, capsys
):
    root = dir_repo / "local" / "claude" / "commands"
    (root / "foo.md").write_text("foo\n")
    ctx = dir_repo_ctx(home, dir_repo)
    install.run_install(ctx, [dir_true_spec()])
    dest = home / ".claude" / "commands" / "foo.md"
    assert dest.is_symlink()

    (root / "foo.md").unlink()
    capsys.readouterr()
    install.run_install(ctx, [dir_true_spec()])

    out = capsys.readouterr().out
    assert not dest.exists()
    assert kinds(ctx, "symlink-created") == []
    assert "removed orphaned symlink" in out
    assert ctx.display(dest) in out


def test_auto_cleanup_leaves_broken_source_entries_alone(
    home, dir_repo, offline_install
):
    """The orphan-only cleanup pass must never touch a still-known destination."""
    root = dir_repo / "local" / "claude" / "commands"
    (root / "orphan.md").write_text("gone-soon\n")
    other_src = dir_repo / "local" / "other.md"
    other_src.write_text("kept\n")
    other_dest = "~/.claude/other.md"
    specs = [dir_true_spec(), install.LinkSpec(src="local/other.md", dest=other_dest)]
    ctx = dir_repo_ctx(home, dir_repo)
    install.run_install(ctx, specs)

    (root / "orphan.md").unlink()  # becomes orphaned: row no longer produces it
    other_src.unlink()  # becomes broken-source: row still produces this dest

    install.run_install(ctx, specs)

    orphan_dest = home / ".claude" / "commands" / "orphan.md"
    broken_dest = home / ".claude" / "other.md"
    assert not orphan_dest.exists(), "orphaned entry should have been cleaned up"
    assert broken_dest.is_symlink(), "still-known (broken-source) entry must survive"
    assert kinds(ctx, "symlink-created") == [
        e for e in kinds(ctx, "symlink-created") if e["dest"] == str(broken_dest)
    ]


def test_auto_cleanup_survives_a_different_harness_selected(
    home, dir_repo, offline_install
):
    """Proves the all-harness view: an unselected harness's row is not orphaned."""
    root = dir_repo / "local" / "claude" / "commands"
    (root / "foo.md").write_text("foo\n")
    specs = [dir_true_spec(harness="claude")]
    ctx = dir_repo_ctx(home, dir_repo, harnesses=("claude",))
    install.run_install(ctx, specs)
    dest = home / ".claude" / "commands" / "foo.md"
    assert dest.is_symlink()

    other_harness_ctx = dir_repo_ctx(home, dir_repo, harnesses=("opencode",))
    install.run_install(other_harness_ctx, specs)

    assert dest.is_symlink(), "a claude-only row must survive an opencode-only run"
    assert len(kinds(ctx, "symlink-created")) == 1


def test_auto_cleanup_dry_run_previews_and_changes_nothing(
    home, dir_repo, offline_install, capsys
):
    root = dir_repo / "local" / "claude" / "commands"
    (root / "foo.md").write_text("foo\n")
    ctx = dir_repo_ctx(home, dir_repo)
    install.run_install(ctx, [dir_true_spec()])
    dest = home / ".claude" / "commands" / "foo.md"
    (root / "foo.md").unlink()
    capsys.readouterr()

    dry_ctx = dir_repo_ctx(home, dir_repo, dry_run=True)
    install.run_install(dry_ctx, [dir_true_spec()])

    out = capsys.readouterr().out
    assert "[dry-run]" in out
    assert "would remove orphaned symlink" in out
    assert dest.is_symlink(), "dry-run must not actually remove the orphan"
    assert len(kinds(ctx, "symlink-created")) == 1


def test_rollback_reverts_dir_true_expanded_symlinks_individually(
    home, dir_repo, offline_install
):
    root = dir_repo / "local" / "claude" / "commands"
    (root / "foo.md").write_text("foo\n")
    (root / "bar.md").write_text("bar\n")
    ctx = dir_repo_ctx(home, dir_repo)
    install.run_install(ctx, [dir_true_spec()])
    dest_root = home / ".claude" / "commands"
    assert (dest_root / "foo.md").is_symlink()
    assert (dest_root / "bar.md").is_symlink()

    install.do_rollback(ctx)

    assert not (dest_root / "foo.md").exists()
    assert not (dest_root / "bar.md").exists()


def test_collision_normal_row_and_dir_true_row_same_dest_aborts(
    home, dir_repo, offline_install, capsys
):
    root = dir_repo / "local" / "claude" / "commands"
    (root / "shared.md").write_text("from-dir\n")
    normal_src = dir_repo / "normal-shared.md"
    normal_src.write_text("from-normal\n")
    specs = [
        install.LinkSpec(src="normal-shared.md", dest="~/.claude/commands/shared.md"),
        dir_true_spec(),
    ]
    ctx = dir_repo_ctx(home, dir_repo)
    capsys.readouterr()

    code = install.run_install(ctx, specs)

    out = capsys.readouterr().err
    assert code == 2
    assert "normal-shared.md" in out
    assert "local/claude/commands/shared.md" in out
    assert not (home / ".claude" / "commands" / "shared.md").exists()
    assert kinds(ctx, "symlink-created") == []


def test_collision_two_dir_true_rows_same_dest_aborts(
    home, dir_repo, offline_install, capsys
):
    root_a = dir_repo / "local" / "claude" / "commands"
    root_b = dir_repo / "local" / "claude" / "commands-b"
    root_b.mkdir()
    (root_a / "shared.md").write_text("from-a\n")
    (root_b / "shared.md").write_text("from-b\n")
    specs = [
        dir_true_spec(),
        dir_true_spec(src="local/claude/commands-b"),
    ]
    ctx = dir_repo_ctx(home, dir_repo)
    capsys.readouterr()

    code = install.run_install(ctx, specs)

    out = capsys.readouterr().err
    assert code == 2
    assert "commands/shared.md" in out
    assert "commands-b/shared.md" in out
    assert kinds(ctx, "symlink-created") == []


def test_check_links_dir_true_clean_state_reports_nothing(home, dir_repo, capsys):
    root = dir_repo / "local" / "claude" / "commands"
    (root / "foo.md").write_text("foo\n")
    ctx = dir_repo_ctx(home, dir_repo)
    install.symlink(
        ctx, root / "foo.md", install.expand_dest("~/.claude/commands/foo.md", home)
    )
    capsys.readouterr()

    code = install.do_check_links(ctx)

    out = capsys.readouterr().out
    assert code == 0
    assert "orphaned" not in out


def test_check_links_dir_true_detects_orphan_after_delete(home, dir_repo, capsys):
    root = dir_repo / "local" / "claude" / "commands"
    (root / "foo.md").write_text("foo\n")
    ctx = dir_repo_ctx(home, dir_repo)
    dest = install.expand_dest("~/.claude/commands/foo.md", home)
    install.symlink(ctx, root / "foo.md", dest)
    (root / "foo.md").unlink()
    capsys.readouterr()

    code = install.do_check_links(ctx)

    out = capsys.readouterr().out
    assert code == 1
    assert "orphaned (1)" in out


def test_check_links_dir_true_cross_checkout_uses_relative_path(
    home, dir_repo, tmp_path, capsys
):
    """A dir=true expansion's cross-checkout check must use the per-file path."""
    root = dir_repo / "local" / "claude" / "commands"
    (root / "foo.md").write_text("foo\n")
    ctx = dir_repo_ctx(home, dir_repo)
    dest = install.expand_dest("~/.claude/commands/foo.md", home)
    install.symlink(ctx, root / "foo.md", dest)

    other = tmp_path / "dir-repo-worktree"
    for path in sorted(dir_repo.rglob("*")):
        if path.is_dir():
            continue
        target = other / path.relative_to(dir_repo)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(path.read_text())
    (other / ".git").write_text("gitdir: /somewhere/.git/worktrees/wt\n")
    capsys.readouterr()

    other_ctx = dir_repo_ctx(home, other)
    code = install.do_check_links(other_ctx)

    out = capsys.readouterr().out
    assert code == 0, f"expected the cross-checkout note, not a false finding:\n{out}"
    assert "wrong-target" not in out
    assert f"note: 1 link(s) point into {dir_repo}" in out
