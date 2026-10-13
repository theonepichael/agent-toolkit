#!/usr/bin/env python3
"""Tests for agent-scripts/agent_toolkit_paths.py."""

from __future__ import annotations

import ast
import json
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
    TOOLKIT_HOME_POINTER_RELPATH,
    LayoutError,
    Resolver,
    StaleLayoutError,
    UnknownDomainError,
    path_for,
)


def _pointer_bytes(layout: str) -> bytes:
    """The canonical pointer bytes, matching the resolver's on-disk format."""
    return (json.dumps({"schema": POINTER_SCHEMA, "layout": layout}) + "\n").encode()


def _toolkit_pointer(home: Path) -> Path:
    return home / ".agent-toolkit" / TOOLKIT_HOME_POINTER_RELPATH


@pytest.fixture
def bare_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty HOME with no override; the default resolver is reset around it."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    agent_toolkit_paths.DEFAULT_RESOLVER.invalidate()
    return home


def test_path_for_resolves_to_toolkit_home(bare_home):
    root = bare_home / ".agent-toolkit"
    assert path_for("work-items") == root / "data" / "backlog"
    assert path_for("decisions") == root / "data" / "grill"
    assert path_for("guard-rail-log") == root / "data" / "guard_rails_audit.jsonl"


def test_path_for_honours_override(bare_home, monkeypatch):
    other = bare_home / "other-root"
    monkeypatch.setenv("AGENT_TOOLKIT_HOME", str(other))
    agent_toolkit_paths.DEFAULT_RESOLVER.invalidate()
    assert path_for("work-items") == other / "data" / "backlog"


def test_relative_override_raises(bare_home, monkeypatch):
    monkeypatch.setenv("AGENT_TOOLKIT_HOME", "relative/path")
    agent_toolkit_paths.DEFAULT_RESOLVER.invalidate()
    with pytest.raises(LayoutError):
        path_for("work-items")


def test_unknown_domain_lists_valid_domains():
    with pytest.raises(UnknownDomainError) as exc_info:
        path_for("not-a-domain")
    assert "not-a-domain" in str(exc_info.value)
    for domain in DOMAINS:
        assert domain in str(exc_info.value)


# ── check_not_stale: legacy paths are always stale ───────────────────────────


def _legacy(home: Path, name: str) -> Path:
    return home / ".claude" / "data" / name


def test_stale_legacy_path_is_refused_as_stale_layout_error(bare_home):
    with pytest.raises(StaleLayoutError):
        agent_toolkit_paths.check_not_stale(_legacy(bare_home, "grill") / "x.json")


def test_toolkit_home_path_is_not_stale(bare_home):
    agent_toolkit_paths.check_not_stale(
        bare_home / ".agent-toolkit" / "data" / "backlog" / "items.json"
    )


def test_path_outside_both_layouts_passes(bare_home, tmp_path):
    agent_toolkit_paths.check_not_stale(tmp_path / "custom" / "items.json")


# ── the toolkit-home pointer is the single source of truth ───────────────────


def test_toolkit_home_pointer_is_read(bare_home):
    ptr = _toolkit_pointer(bare_home)
    ptr.parent.mkdir(parents=True, exist_ok=True)
    ptr.write_bytes(_pointer_bytes("toolkit-home"))
    assert Resolver().layout() == "toolkit-home"


def test_absent_pointer_is_toolkit_home(bare_home):
    assert Resolver().layout() == "toolkit-home"


def test_legacy_pointer_value_is_rejected(bare_home):
    ptr = _toolkit_pointer(bare_home)
    ptr.parent.mkdir(parents=True, exist_ok=True)
    ptr.write_bytes(_pointer_bytes("legacy"))
    with pytest.raises(LayoutError):
        Resolver().layout()


def test_legacy_location_pointer_is_ignored(bare_home):
    # A leftover pointer at the retired legacy location is not read.
    ptr = bare_home / POINTER_RELPATH
    ptr.parent.mkdir(parents=True, exist_ok=True)
    ptr.write_bytes(_pointer_bytes("legacy"))
    assert Resolver().layout() == "toolkit-home"


def test_malformed_pointer_cases(bare_home):
    ptr = _toolkit_pointer(bare_home)
    ptr.parent.mkdir(parents=True, exist_ok=True)
    for content in (
        '{"layout": "toolkit-home"}',  # missing schema
        '{"schema": 2, "layout": "toolkit-home"}',  # bad schema
        '{"schema": true, "layout": "toolkit-home"}',  # boolean schema
        '{"schema": "1", "layout": "toolkit-home"}',  # string schema
        "not json",
    ):
        ptr.write_text(content)
        with pytest.raises(LayoutError):
            Resolver().layout()


def test_unreadable_pointer_raises(bare_home):
    ptr = _toolkit_pointer(bare_home)
    ptr.parent.mkdir(parents=True, exist_ok=True)
    ptr.mkdir()
    with pytest.raises(LayoutError):
        Resolver().layout()


def test_invalid_utf8_pointer_raises(bare_home):
    ptr = _toolkit_pointer(bare_home)
    ptr.parent.mkdir(parents=True, exist_ok=True)
    ptr.write_bytes(b'{"schema": 1, "layout": "\xff\xfe"}\n')
    with pytest.raises(LayoutError):
        Resolver().layout()


def test_override_aliasing_the_legacy_pointer_raises(bare_home, monkeypatch):
    monkeypatch.setenv("AGENT_TOOLKIT_HOME", str(bare_home / ".claude"))
    with pytest.raises(LayoutError):
        Resolver().layout()


def test_cache_follows_the_pointer_appearing_and_vanishing(bare_home):
    resolver = Resolver()
    assert resolver.layout() == "toolkit-home"
    ptr = _toolkit_pointer(bare_home)
    ptr.parent.mkdir(parents=True, exist_ok=True)
    ptr.write_bytes(_pointer_bytes("toolkit-home"))
    assert resolver.layout() == "toolkit-home"
    ptr.unlink()
    assert resolver.layout() == "toolkit-home"


def test_cache_follows_a_toolkit_root_change(bare_home, monkeypatch):
    resolver = Resolver()
    assert resolver.layout() == "toolkit-home"
    other = bare_home / "other-root"
    monkeypatch.setenv("AGENT_TOOLKIT_HOME", str(other))
    assert resolver.layout() == "toolkit-home"


def test_cache_rereads_a_malformed_pointer_after_a_miss(bare_home):
    # The first read (pointer absent) caches toolkit-home. A malformed pointer
    # written afterward must be read, not masked by the cache -- a resolver
    # that failed to invalidate would keep returning toolkit-home.
    resolver = Resolver()
    assert resolver.layout() == "toolkit-home"
    ptr = _toolkit_pointer(bare_home)
    ptr.parent.mkdir(parents=True, exist_ok=True)
    ptr.write_text("not json")
    with pytest.raises(LayoutError):
        resolver.layout()


def test_cache_rereads_after_the_toolkit_root_gains_a_malformed_pointer(
    bare_home, monkeypatch
):
    # Switching AGENT_TOOLKIT_HOME changes the cache key, so the resolver must
    # re-read the new root's pointer (malformed) rather than replay the old
    # toolkit-home answer.
    resolver = Resolver()
    assert resolver.layout() == "toolkit-home"
    other = bare_home / "other-root"
    ptr = other / TOOLKIT_HOME_POINTER_RELPATH
    ptr.parent.mkdir(parents=True, exist_ok=True)
    ptr.write_text("not json")
    monkeypatch.setenv("AGENT_TOOLKIT_HOME", str(other))
    with pytest.raises(LayoutError):
        resolver.layout()


def test_path_for_does_not_create_directories(bare_home):
    path = path_for("work-items")
    assert path == bare_home / ".agent-toolkit" / "data" / "backlog"
    assert not path.exists()


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
    """True if ``tree`` builds a ``.claude/data`` path in code (not in prose)."""
    import check_toolkit_paths

    return any(
        segs[:1] == ("data",)
        for _line, segs in check_toolkit_paths.python_references(tree)
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


def test_generated_text_root_matches_the_default_toolkit_root(tmp_path, monkeypatch):
    """harness_spec's display root is the resolver's default root, spelled with ~."""
    import harness_spec

    monkeypatch.delenv(agent_toolkit_paths.ENV_HOME, raising=False)
    root = harness_spec.TOOLKIT_DISPLAY_ROOT
    assert root.startswith("~/")
    home_root = tmp_path / root.removeprefix("~/")
    assert Resolver()._toolkit_root(tmp_path) == home_root
    assert Resolver()._path_in(tmp_path, "decisions", "toolkit-home") == (
        tmp_path
        / harness_spec.TOOLKIT_PATH_TOKENS["TOOLKIT_DATA"].removeprefix("~/")
        / "grill"
    )
