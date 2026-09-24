#!/usr/bin/env python3
"""Tests for the release-1 settings and global-hook rewrite migration step.

The step (`settings-rewrite`, an ``in_run`` phase) rewrites only values that
are exactly a manifest-owned command path or that invoke one, leaving every
user edit intact; snapshots the originals and re-verifies them before any
restore; and exposes a ``guard`` that runs in ``_write_guard`` *before* the
layout pointer is flipped back, so a third-party edit refuses restore with no
half-restored machine.
"""

import json
import subprocess

import pytest

import agent_toolkit_paths  # noqa: E402
import link_inspect  # noqa: E402
import migrate_toolkit_home as mth  # noqa: E402
import migration_lock  # noqa: E402
from pathlib import Path  # noqa: E402

REPO = Path(__file__).resolve().parent.parent

pytestmark = [
    pytest.mark.usefixtures("sandbox"),
    pytest.mark.allow_real_subprocess,
]


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local" / "state"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv(agent_toolkit_paths.ENV_HOME, raising=False)
    migration_lock._reset_for_tests()
    yield home
    migration_lock._reset_for_tests()


@pytest.fixture(autouse=True)
def passing_validation(monkeypatch):
    monkeypatch.setattr(mth, "_run_validation", lambda _ctx: (True, []))


@pytest.fixture
def machine(sandbox):
    _installed_runtime(sandbox)
    _legacy_stores(sandbox)
    return sandbox


def _installed_runtime(home: object) -> None:
    """A fake installed runtime, linked into place the way install.py links it."""
    real = home / "fake-runtime"
    real.mkdir(exist_ok=True)
    (real / "migration_lock.py").write_text("ENFORCE: bool = True\n")
    (real / "agent_toolkit_paths.py").write_text("# installed\n")
    scripts = home / ".claude" / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    for module in ("migration_lock.py", "agent_toolkit_paths.py"):
        link = scripts / module
        if link.is_symlink():
            link.unlink()
        link.symlink_to(real / module)


def _legacy_stores(home: object) -> None:
    data = home / ".claude" / "data"
    backlog = data / "backlog"
    backlog.mkdir(parents=True)
    (backlog / "items.json").write_text(
        json.dumps({"schema_version": 2, "items": [{"id": "x-one"}]})
    )
    (backlog / "pending_items.json").write_text(
        json.dumps({"schema_version": 1, "items": []})
    )
    (backlog / "_meta.json").write_text(json.dumps({"rev": 3}))
    (backlog / "journal.jsonl").write_text('{"cmd": "add"}\n{"cmd": "done"}\n')
    (backlog / "_machine_id").write_text("0ee2ec8d")
    grill = data / "grill"
    grill.mkdir()
    (grill / "topic.json").write_text(
        json.dumps({"schema_version": 1, "slug": "topic"})
    )
    (grill / "topic-plan.md").write_text("# plan\n")
    (data / "backend_calls.jsonl").write_text('{"backend": "codex"}\n')
    (data / "unrelated-notes").mkdir()


def _opts(**kw: object) -> mth.MigrationOptions:
    base: dict[str, object] = {
        "harnesses": ("claude",),
        "profile": "personal",
        "dry_run": False,
        "json_report": True,
        "cross_filesystem": False,
        "skip_reconciliation": True,
        "migration_id": None,
        "quiet": True,
        "verbose": False,
    }
    base.update(kw)
    return mth.MigrationOptions(**base)


def _run(capsys, **kw: object) -> tuple[int, dict]:
    code = mth.run(_opts(**kw), repo_root=REPO)
    out = capsys.readouterr().out
    report = json.loads(out) if out.strip() else {}
    return code, report


def _installer_state(home: object) -> object:
    return home / ".local" / "state" / "agent-toolkit"


def _journal_dirs(home: object) -> list:
    root = _installer_state(home) / "migrations"
    return sorted(p for p in root.iterdir()) if root.is_dir() else []


def _records(journal_dir: object) -> list:
    return mth.read_records(journal_dir)


def _settings_path(home: object) -> object:
    return home / ".claude" / "settings.json"


# ── unit: manifest map and rewrite helpers ───────────────────────────────────


def test_manifest_path_map_derives_old_to_new():
    mapping = mth._manifest_path_map(REPO)
    assert mapping["~/.claude/scripts/notify.py"] == "~/.agent-toolkit/scripts/notify.py"
    assert mapping["~/.claude/hooks/agy-elapsed.js"] == (
        "~/.agent-toolkit/hooks/agy-elapsed.js"
    )


def test_rewrite_value_exact_and_token_and_unmatched():
    mapping = mth._manifest_path_map(REPO)
    # exact whole-value match
    assert mth._rewrite_value("~/.claude/scripts/notify.py", mapping) == (
        "~/.agent-toolkit/scripts/notify.py",
        True,
        [],
    )
    # clean token invocation (command + flags)
    new, matched, um = mth._rewrite_value(
        "python3 ~/.claude/scripts/notify.py --verbose", mapping
    )
    assert matched and "--verbose" in new and "~/.agent-toolkit" in new
    # substring but not a clean token -> unmatched, left alone
    new, matched, um = mth._rewrite_value(
        "x ~/.claude/scripts/notify.py; rm -rf /", mapping
    )
    assert not matched and um


def test_rewrite_json_text_preserves_drift():
    mapping = mth._manifest_path_map(REPO)
    data = {
        "permissions": {"allow": ["Bash(ls:*)", "~/.claude/scripts/notify.py"]},
        "hooks": {
            "PostToolUse": [{"command": "python3 ~/.claude/scripts/guard_rails.py"}]
        },
        "statusLine": {"command": "node ~/.claude/hooks/agy-elapsed.js"},
        "myCustomKey": "my drift value",
    }
    raw = json.dumps(data, indent=2)
    new_text, matches, unmatched = mth._rewrite_json_text(raw, mapping)
    assert unmatched == []
    out = json.loads(new_text)
    assert out["permissions"]["allow"][1] == "~/.agent-toolkit/scripts/notify.py"
    assert "python3 ~/.agent-toolkit/scripts/guard_rails.py" in (
        out["hooks"]["PostToolUse"][0]["command"]
    )
    assert "node ~/.agent-toolkit/hooks/agy-elapsed.js" in out["statusLine"]["command"]
    # drift untouched
    assert out["myCustomKey"] == "my drift value"
    assert len(matches) == 3


def test_rewrite_value_preserves_whitespace_exactly():
    """Only the matched token's span changes; every other character survives.

    A whitespace-collapsing rebuild would silently reformat the value
    (bash -c "a  b" becoming bash -c "a b"), which is a user edit.
    """
    mapping = mth._manifest_path_map(REPO)
    value = 'bash -c "echo  a"  python3 ~/.claude/scripts/notify.py\t--flag'
    new, matched, um = mth._rewrite_value(value, mapping)
    assert matched and not um
    assert new == (
        'bash -c "echo  a"  python3 ~/.agent-toolkit/scripts/notify.py\t--flag'
    )


def test_permission_pattern_reported_not_rewritten():
    """Permission-pattern wrappers (Bash(...) and agy's command(...)) are
    structural syntax, not plain invocations: reported, never rewritten."""
    mapping = mth._manifest_path_map(REPO)
    for value in (
        "Bash(python3 ~/.claude/scripts/dev_status.py prune:*)",
        "Bash(python3 ~/.claude/scripts/grill.py:*)",
        "command(python3 ~/.claude/scripts/dev_status.py render)",
        "command(node ~/.claude/hooks/agy-elapsed.js)",
    ):
        new, matched, um = mth._rewrite_value(value, mapping)
        assert not matched, value
        assert new == value, value
        assert um == [value], value


def test_rewrite_json_text_in_place_preserves_comments_and_bytes():
    """Discovery parses (JSONC-tolerant), but rewriting happens on the raw text.

    Every byte outside the substituted literals — comments, indentation,
    whitespace inside values — must survive exactly; the result must still
    parse.
    """
    mapping = mth._manifest_path_map(REPO)
    raw = "\n".join(
        [
            "{",
            "  // keep me",
            '  "statusLine": { "command": "python3  ~/.claude/scripts/notify.py", },',
            "  /* block */",
            '  "drift": "keep  me"',
            "}",
            "",
        ]
    )
    new_text, matches, unmatched = mth._rewrite_json_text(raw, mapping)
    assert unmatched == [] and len(matches) == 1
    expected = raw.replace(
        '"python3  ~/.claude/scripts/notify.py"',
        json.dumps("python3  ~/.agent-toolkit/scripts/notify.py"),
    )
    assert new_text == expected
    assert "// keep me" in new_text and "/* block */" in new_text
    assert '  "drift": "keep  me"' in new_text
    data = json.loads(mth._strip_trailing_commas(mth._strip_jsonc(new_text)))
    assert data["statusLine"]["command"] == (
        "python3  ~/.agent-toolkit/scripts/notify.py"
    )
    assert data["drift"] == "keep  me"


def test_rewrite_json_text_keys_never_rewritten():
    mapping = mth._manifest_path_map(REPO)
    raw = json.dumps({"~/.claude/scripts/notify.py": "value"}, indent=2)
    new_text, matches, unmatched = mth._rewrite_json_text(raw, mapping)
    # never rewritten, but reported: a key naming an old path is drift
    assert matches == []
    assert unmatched == ["~/.claude/scripts/notify.py"]
    assert new_text == raw


def test_manifest_path_map_includes_absolute_home_forms():
    """Live agy settings use /home/<user>/... as well as ~/... forms."""
    mapping = mth._manifest_path_map(REPO)
    home = str(Path.home())
    assert mapping[home + "/.claude/scripts/notify.py"] == (
        home + "/.agent-toolkit/scripts/notify.py"
    )
    assert mapping[home + "/.claude/hooks/agy-elapsed.js"] == (
        home + "/.agent-toolkit/hooks/agy-elapsed.js"
    )


def test_rewrite_shell_text_token_based():
    mapping = mth._manifest_path_map(REPO)
    text = 'echo "  python3 ~/.claude/scripts/worktree.py <slug>"\n'
    new_text, matches, unmatched = mth._rewrite_shell_text(text, mapping)
    assert unmatched == []
    assert "python3 ~/.agent-toolkit/scripts/worktree.py" in new_text
    # surrounding whitespace preserved
    assert new_text.startswith('echo "  ') and new_text.endswith('>"\n')


# ── integration: a live migration rewrites settings in place ────────────────


def test_migration_rewrites_live_settings(machine, capsys):
    settings = _settings_path(machine)
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(
        json.dumps(
            {
                "permissions": {
                    "allow": ["Bash(ls:*)", "~/.claude/scripts/notify.py"]
                },
                "hooks": {
                    "PostToolUse": [
                        {"command": "python3 ~/.claude/scripts/guard_rails.py"}
                    ]
                },
                "statusLine": {"command": "node ~/.claude/hooks/agy-elapsed.js"},
                "myCustomKey": "my drift value",
                "theme": "dark",
            },
            indent=2,
        )
    )
    code, report = _run(capsys)
    assert code == 0, report

    data = json.loads(settings.read_text())
    # manifest paths rewritten
    assert data["permissions"]["allow"][1] == "~/.agent-toolkit/scripts/notify.py"
    assert "python3 ~/.agent-toolkit/scripts/guard_rails.py" in (
        data["hooks"]["PostToolUse"][0]["command"]
    )
    assert "node ~/.agent-toolkit/hooks/agy-elapsed.js" in data["statusLine"]["command"]
    # user drift preserved
    assert data["myCustomKey"] == "my drift value"
    assert data["theme"] == "dark"

    # the step ran and journalled a snapshot of the original
    jdir = _journal_dirs(machine)[0]
    phases = [r["phase"] for r in _records(jdir) if r["event"] == "begin"]
    assert "settings-rewrite" in phases
    work_dir = machine / ".agent-toolkit" / f".migration-{report['migration_id']}"
    assert (work_dir / "settings-rewrite").is_dir()


def test_unmatched_value_reported_and_left_alone(machine, capsys):
    settings = _settings_path(machine)
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(
        json.dumps(
            {
                "command": "python3 ~/.claude/scripts/notify.py; rm -rf /",
                "clean": "python3 ~/.claude/scripts/notify.py",
                "drift": "keep me",
            },
            indent=2,
        )
    )
    original = settings.read_text()
    code, report = _run(capsys)
    assert code == 0, report

    data = json.loads(settings.read_text())
    # a clean token invocation is rewritten
    assert data["clean"] == "python3 ~/.agent-toolkit/scripts/notify.py"
    # an unmatched wrapper is reported and left alone
    assert data["command"] == "python3 ~/.claude/scripts/notify.py; rm -rf /"
    assert data["drift"] == "keep me"

    jdir = _journal_dirs(machine)[0]
    rec = next(
        r
        for r in _records(jdir)
        if r["phase"] == "settings-rewrite" and r["event"] == "done"
    )
    file_target = next(t for t in rec["detail"]["targets"] if t["kind"] == "file")
    assert "python3 ~/.claude/scripts/notify.py; rm -rf /" in file_target["unmatched"]


# ── restore and the third-party-edit guard ──────────────────────────────────


def _rebuild_state(machine, migration_id):
    directory = _installer_state(machine) / "migrations" / migration_id
    records = mth.read_records(directory)
    return mth._load_state(
        directory, None, records, REPO, link_inspect.manifest_path(machine)
    )


def _write_claude_settings(machine, body: str) -> object:
    settings = _settings_path(machine)
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(body)
    return settings


def test_byte_for_byte_restore_on_rollback(machine, capsys):
    settings = _write_claude_settings(
        machine,
        json.dumps(
            {
                "permissions": {"allow": ["~/.claude/scripts/notify.py"]},
                "drift": "untouched",
            },
            indent=2,
        ),
    )
    original = settings.read_text()
    code, report = _run(capsys)
    assert code == 0, report

    # no third-party edit: a narrow rollback restores the original bytes exactly
    mth._rollback(_rebuild_state(machine, report["migration_id"]))
    assert settings.read_text() == original
    assert agent_toolkit_paths.current_layout() == "legacy"


def test_third_party_edit_refuses_restore(machine, capsys):
    """The design-gap fix: a guard in _write_guard refuses before the flip.

    A third-party edit after the migration must refuse restore with no change,
    leaving the machine fully migrated (pointer still toolkit-home) rather than
    half-restored (pointer legacy with the settings only partially reverted).
    """
    settings = _write_claude_settings(
        machine,
        json.dumps(
            {"permissions": {"allow": ["~/.claude/scripts/notify.py"]}},
            indent=2,
        ),
    )
    code, report = _run(capsys)
    assert code == 0, report

    # a third party edits the live file after the migration (target the
    # rewritten path, which is what actually lives in the file now)
    edited = settings.read_text().replace(
        "~/.agent-toolkit/scripts/notify.py", "EVIL-PATH"
    )
    assert "EVIL-PATH" in edited
    settings.write_text(edited)

    with pytest.raises(mth.RestoreRefused):
        mth._rollback(_rebuild_state(machine, report["migration_id"]))

    # the layout pointer was NOT flipped back — no half-restored machine
    assert agent_toolkit_paths.current_layout() == "toolkit-home"
    # the settings file is unchanged (still the third-party edit, not reverted)
    assert "EVIL-PATH" in settings.read_text()


# ── global git hook ─────────────────────────────────────────────────────────


def test_githook_rewrite_and_restore(machine, capsys):
    if subprocess.run(["git", "--version"], capture_output=True).returncode != 0:
        pytest.skip("git not available")
    hooks = machine / ".git-hooks"
    hook = hooks / "lib" / "no-commit-on-main.sh"
    hook.parent.mkdir(parents=True)
    hook.write_text('#!/bin/sh\necho "python3 ~/.claude/scripts/worktree.py --do"\n')
    subprocess.run(
        ["git", "config", "--global", "core.hooksPath", str(hooks)],
        check=True,
        capture_output=True,
    )
    try:
        code, report = _run(capsys)
        assert code == 0, report

        # the emitted command path was rewritten in the hook file
        assert "python3 ~/.agent-toolkit/scripts/worktree.py" in hook.read_text()

        # a narrow rollback restores the original hook content
        mth._rollback(_rebuild_state(machine, report["migration_id"]))
        assert "python3 ~/.claude/scripts/worktree.py" in hook.read_text()
    finally:
        subprocess.run(
            ["git", "config", "--global", "--unset", "core.hooksPath"],
            check=False,
            capture_output=True,
        )


def test_gitconfig_include_reports_manual_review(machine, capsys):
    """An include/includeIf section means core.hooksPath may be overridden —
    report it for manual review instead of assuming the parsed value is final."""
    hooks = machine / ".git-hooks"
    hook = hooks / "no-commit-on-main.sh"
    hook.parent.mkdir(parents=True)
    hook.write_text("python3 ~/.claude/scripts/worktree.py --do\n")
    (machine / ".gitconfig").write_text(
        "[core]\n\thooksPath = " + str(hooks) + "\n"
        "[include]\n\tpath = ~/other.gitconfig\n"
    )
    code, report = _run(capsys)
    assert code == 0, report

    # not rewritten, left for a human
    assert "python3 ~/.claude/scripts/worktree.py" in hook.read_text()
    jdir = _journal_dirs(machine)[0]
    rec = next(
        r
        for r in _records(jdir)
        if r["phase"] == "settings-rewrite" and r["event"] == "done"
    )
    gh = next(t for t in rec["detail"]["targets"] if t["kind"] == "githook")
    assert "include" in str(gh.get("needs_manual", ""))


def test_gitconfig_includeif_reports_manual_review(machine, capsys):
    hooks = machine / ".git-hooks"
    hook = hooks / "no-commit-on-main.sh"
    hook.parent.mkdir(parents=True)
    hook.write_text("python3 ~/.claude/scripts/worktree.py --do\n")
    (machine / ".gitconfig").write_text(
        "[core]\n\thooksPath = " + str(hooks) + "\n"
        '[includeIf "gitdir:~/work/"]\n\tpath = ~/work.gitconfig\n'
    )
    code, report = _run(capsys)
    assert code == 0, report
    assert "python3 ~/.claude/scripts/worktree.py" in hook.read_text()
    jdir = _journal_dirs(machine)[0]
    rec = next(
        r
        for r in _records(jdir)
        if r["phase"] == "settings-rewrite" and r["event"] == "done"
    )
    gh = next(t for t in rec["detail"]["targets"] if t["kind"] == "githook")
    assert "include" in str(gh.get("needs_manual", ""))
