#!/usr/bin/env python3
"""Tests for agent-scripts/agent_toolkit_paths.py."""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent-scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import agent_toolkit_paths  # noqa: E402
from agent_toolkit_paths import (  # noqa: E402
    DOMAINS,
    POINTER_RELPATH,
    POINTER_SCHEMA,
    LayoutError,
    Resolver,
    UnknownDomainError,
    path_for,
    write_pointer,
)


def test_legacy_paths_match_old_literals(two_layout_homes):
    """Every converted constant resolves to its pre-resolver literal path."""
    local = two_layout_homes.local
    with local.activated():
        assert path_for("work-items") == Path.home() / ".claude" / "data" / "backlog"
        assert (
            path_for("out-of-scope")
            == Path.home() / ".claude" / "data" / "backlog-out-of-scope"
        )
        assert path_for("decisions") == Path.home() / ".claude" / "data" / "grill"
        assert (
            path_for("ticket-batches")
            == Path.home() / ".claude" / "data" / "to-tickets"
        )
        assert path_for("standups") == Path.home() / ".claude" / "data" / "standup"
        assert (
            path_for("guard-rail-log")
            == Path.home() / ".claude" / "data" / "guard_rails_audit.jsonl"
        )
        assert (
            path_for("backend-log")
            == Path.home() / ".claude" / "data" / "backend_calls.jsonl"
        )


def test_toolkit_home_paths_when_flipped(two_layout_homes):
    local = two_layout_homes.local
    with local.activated():
        local.flip("toolkit-home")
        root = Path.home() / ".agent-toolkit"
        assert path_for("work-items") == root / "data" / "backlog"
        assert path_for("decisions") == root / "data" / "grill"


def test_no_pointer_means_legacy(two_layout_homes):
    with two_layout_homes.peer.activated():
        assert agent_toolkit_paths.current_layout() == "legacy"


def test_default_resolver_sees_flip_with_no_reload_or_patch(two_layout_homes):
    local = two_layout_homes.local
    with local.activated():
        assert path_for("decisions") == Path.home() / ".claude" / "data" / "grill"
        local.flip("toolkit-home")
        assert path_for("decisions") == Path.home() / ".agent-toolkit" / "data" / "grill"
        local.flip("legacy")
        assert path_for("decisions") == Path.home() / ".claude" / "data" / "grill"


def test_manual_resolver_sees_flip_with_no_reload_or_patch(two_layout_homes):
    local = two_layout_homes.local
    resolver = Resolver()
    with local.activated():
        assert resolver.path_for("decisions") == Path.home() / ".claude" / "data" / "grill"
        local.flip("toolkit-home")
        assert (
            resolver.path_for("decisions")
            == Path.home() / ".agent-toolkit" / "data" / "grill"
        )
        local.flip("legacy")
        assert resolver.path_for("decisions") == Path.home() / ".claude" / "data" / "grill"


def test_cache_invalidation_on_same_size_rewrite(two_layout_homes):
    local = two_layout_homes.local
    with local.activated():
        local.flip("legacy")
        first = path_for("decisions")
        write_pointer(local.home, "legacy")
        second = path_for("decisions")
        assert second == first


def test_write_pointer_changes_inode(tmp_path):
    home = tmp_path / "home"
    write_pointer(home, "legacy")
    pointer = home / POINTER_RELPATH
    first = pointer.stat().st_ino
    write_pointer(home, "legacy")
    second = pointer.stat().st_ino
    assert second != first


def test_malformed_pointer_cases(two_layout_homes):
    local = two_layout_homes.local
    pointer = local.home / POINTER_RELPATH
    cases = [
        ("not json", "not json"),
        ('{"layout": "legacy"}', "missing schema"),
        ('{"schema": 2, "layout": "legacy"}', "bad schema"),
        ('{"schema": 1}', "missing layout"),
        ('{"schema": 1, "layout": "unknown"}', "unknown layout"),
        ("[]", "not an object"),
        ('{"schema": true, "layout": "legacy"}', "boolean schema"),
        ('{"schema": "1", "layout": "legacy"}', "string schema"),
    ]
    for content, desc in cases:
        pointer.parent.mkdir(parents=True, exist_ok=True)
        pointer.write_text(content)
        with local.activated():
            try:
                result = agent_toolkit_paths.current_layout()
            except LayoutError as exc:
                assert str(pointer) in str(exc)
            else:
                raise AssertionError(f"case {desc!r} did not raise (got {result!r})")
        pointer.unlink()


def test_unreadable_pointer_raises_not_silent_legacy(two_layout_homes):
    local = two_layout_homes.local
    pointer = local.home / POINTER_RELPATH
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text('{"schema": 1, "layout": "toolkit-home"}')
    pointer.chmod(0o000)
    try:
        with local.activated():
            with pytest.raises(LayoutError) as exc_info:
                agent_toolkit_paths.current_layout()
            assert str(pointer) in str(exc_info.value)
            assert "cannot read" in str(exc_info.value)
    finally:
        pointer.chmod(0o644)


def test_invalid_utf8_pointer_raises_layout_error(two_layout_homes):
    local = two_layout_homes.local
    pointer = local.home / POINTER_RELPATH
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_bytes(b'\xff\xfe')
    with local.activated():
        with pytest.raises(LayoutError) as exc_info:
            agent_toolkit_paths.current_layout()
        assert str(pointer) in str(exc_info.value)


@pytest.mark.allow_real_subprocess
def test_guard_rails_with_invalid_utf8_pointer_emits_deny_not_crash(two_layout_homes):
    local = two_layout_homes.local
    pointer = local.home / POINTER_RELPATH
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_bytes(b'\xff\xfe')
    repo_root = Path(__file__).resolve().parents[1]
    script = repo_root / "agent-scripts" / "guard_rails.py"
    payload = json.dumps({"cwd": "/repo", "tool_name": "Edit", "tool_input": {"file_path": "/repo/a.py"}})
    result = subprocess.run(
        [sys.executable, str(script), "--harness", "claude"],
        input=payload,
        capture_output=True,
        text=True,
        env={**os.environ, "HOME": str(local.home)},
    )
    assert result.returncode == 0, result.stderr
    parsed = json.loads(result.stdout)
    assert parsed["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_relative_agent_toolkit_home_raises(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    write_pointer(home, "toolkit-home")
    monkeypatch.setenv("AGENT_TOOLKIT_HOME", "relative/path")
    monkeypatch.setenv("HOME", str(home))
    agent_toolkit_paths.DEFAULT_RESOLVER.invalidate()
    try:
        with pytest.raises(LayoutError) as exc_info:
            path_for("work-items")
        assert "absolute" in str(exc_info.value)
    finally:
        agent_toolkit_paths.DEFAULT_RESOLVER.invalidate()


def test_absolute_agent_toolkit_home_moves_only_toolkit_paths(two_layout_homes, monkeypatch):
    local = two_layout_homes.local
    override = local.home / "toolkit-override"
    override.mkdir()
    write_pointer(local.home, "toolkit-home")
    monkeypatch.setenv("HOME", str(local.home))
    monkeypatch.setenv("AGENT_TOOLKIT_HOME", str(override))
    agent_toolkit_paths.DEFAULT_RESOLVER.invalidate()
    try:
        assert path_for("work-items") == override / "data" / "backlog"
        local.flip("legacy")
        assert path_for("work-items") == Path.home() / ".claude" / "data" / "backlog"
    finally:
        agent_toolkit_paths.DEFAULT_RESOLVER.invalidate()


def test_unknown_domain_lists_valid_domains():
    with pytest.raises(UnknownDomainError) as exc_info:
        path_for("not-a-domain")
    assert "not-a-domain" in str(exc_info.value)
    for domain in DOMAINS:
        assert domain in str(exc_info.value)


def test_pointer_format_and_schema(two_layout_homes):
    pointer = two_layout_homes.local.home / POINTER_RELPATH
    data = json.loads(pointer.read_text())
    assert data == {"schema": POINTER_SCHEMA, "layout": "legacy"}


def test_resolver_path_for_does_not_create_directories(two_layout_homes, tmp_path, monkeypatch):
    home = tmp_path / "fresh"
    write_pointer(home, "legacy")
    resolver = Resolver()
    monkeypatch.setenv("HOME", str(home))
    agent_toolkit_paths.DEFAULT_RESOLVER.invalidate()
    try:
        path = resolver.path_for("work-items")
        assert path == home / ".claude" / "data" / "backlog"
        assert not path.exists()
    finally:
        agent_toolkit_paths.DEFAULT_RESOLVER.invalidate()


def test_no_module_other_than_resolver_constructs_toolkit_data_path():
    """AST guard: only agent_toolkit_paths.py builds ``~/.claude/data/...``."""
    repo_root = Path(__file__).resolve().parents[1]
    scripts_dir = repo_root / "agent-scripts"
    offenders: list[str] = []
    for source in scripts_dir.glob("*.py"):
        if source.name == "agent_toolkit_paths.py" or source.name.startswith("test_"):
            continue
        tree = ast.parse(source.read_text())
        if _constructs_toolkit_data_path(tree):
            offenders.append(source.name)
    assert not offenders, f"modules constructing legacy data paths: {offenders}"


def _constructs_toolkit_data_path(tree: ast.AST) -> bool:
    """True if ``tree`` builds a ``.claude/data`` path in code (not in prose).

    Uses the repository check's own extractor, so this guard and
    scripts/check_toolkit_paths.py can never disagree about what counts.
    """
    import check_toolkit_paths

    return any(
        segs[:1] == ("data",) for _line, segs in check_toolkit_paths.python_references(tree)
    )

@pytest.mark.regression(
    "ast-guard-never-matches-single-quoted-unparse",
    "AssertionError: assert False",
)
@pytest.mark.parametrize(
    "source",
    [
        'Path.home() / ".claude" / "data" / "x"',
        "Path.home() / '.claude' / 'data' / 'grill'",
        'Path("~/.claude/data/backlog").expanduser()',
        'BASE = Path.home() / ".claude"\nD = BASE / "data" / "x"',
    ],
)
def test_ast_guard_catches_real_constructions(source):
    assert _constructs_toolkit_data_path(ast.parse(source))


@pytest.mark.parametrize(
    "source",
    [
        'Path.home() / ".claude" / "projects"',
        '"see ~/.claude/data/grill in the docs"',
        'agent_toolkit_paths.path_for("decisions")',
    ],
)
def test_ast_guard_ignores_non_constructions(source):
    assert not _constructs_toolkit_data_path(ast.parse(source))
