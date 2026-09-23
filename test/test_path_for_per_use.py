#!/usr/bin/env python3
"""Toolkit data paths are resolved at each use, never frozen at import.

Every module here was imported (by conftest) before any test home existed,
which is exactly the situation of a long-lived process that started before a
layout flip. Each test activates a two-layout sandbox home, flips the layout
pointer inside the same process, and checks that reads and writes follow the
pointer — and that a path captured before the flip is refused instead of
quietly recreating the legacy store.
"""

import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent-scripts"))

import agent_toolkit_paths  # noqa: E402
import backlog_claim_lookup  # noqa: E402
import dev_status  # noqa: E402
import dev_status_storage  # noqa: E402
import grill  # noqa: E402
import guard_rails  # noqa: E402
import llm_backends  # noqa: E402
import migration_lock  # noqa: E402
import second_opinion  # noqa: E402
import standup  # noqa: E402
import test_layouts  # noqa: E402
import to_tickets_runner  # noqa: E402
import vitals_promotion  # noqa: E402


def _stale_error() -> type[Exception]:
    return agent_toolkit_paths.StaleLayoutError


@pytest.fixture
def local(tmp_path: Path) -> Iterator[test_layouts.SandboxHome]:
    homes = test_layouts.build_two_layout_homes(tmp_path)
    with homes.local.activated():
        yield homes.local


def _legacy(home: test_layouts.SandboxHome, name: str) -> Path:
    return home.legacy_data / name


def _toolkit(home: test_layouts.SandboxHome, name: str) -> Path:
    return home.toolkit_home / "data" / name


def _retire_legacy(home: test_layouts.SandboxHome, name: str) -> None:
    """Rename a legacy entry away, the way migration commit does."""
    src = _legacy(home, name)
    src.rename(src.with_name(f"{name}.snapshot"))


def _item(slug: str) -> dict[str, object]:
    return {"id": slug, "summary": slug, "status": "open", "category": "chore"}


def _save_one_item(slug: str) -> None:
    with dev_status.backlog_lock():
        dev_status.save_items([_item(slug)])
        dev_status.bump_rev()


def _flip_on_first_scope(
    monkeypatch: pytest.MonkeyPatch,
    home: test_layouts.SandboxHome,
    retire: tuple[str, ...] = (),
) -> None:
    """Flip (and retire legacy entries) just before the first migration scope.

    ``migration_lock.shared`` is non-blocking, so the real race window is
    between a caller starting and its scope being entered. This puts the
    whole migration inside that window.
    """
    real = migration_lock.shared
    fired: list[bool] = []

    @contextmanager
    def shared(site: str, **kwargs: object) -> Iterator[None]:
        if not fired:
            fired.append(True)
            home.flip("toolkit-home")
            for name in retire:
                _retire_legacy(home, name)
        with real(site, **kwargs):
            yield

    monkeypatch.setattr(migration_lock, "shared", shared)


# ── backlog store: regression ────────────────────────────────────────────────


@pytest.mark.regression(
    "store-write-ignores-layout-flip",
    "FileNotFoundError: [Errno 2] No such file or directory",
)
def test_a_write_after_flip_lands_in_toolkit_home(local):
    local.flip("toolkit-home")
    _save_one_item("after-flip")
    items = json.loads((_toolkit(local, "backlog") / "items.json").read_text())
    assert [i["id"] for i in items["items"]] == ["after-flip"]
    assert not (_legacy(local, "backlog") / "items.json").exists()


@pytest.mark.regression(
    "store-write-recreates-retired-legacy-store",
    "AssertionError: assert False",
)
def test_b_write_after_flip_does_not_recreate_retired_legacy_store(local):
    local.flip("toolkit-home")
    _retire_legacy(local, "backlog")
    _save_one_item("after-retire")
    assert not _legacy(local, "backlog").exists()
    assert (_toolkit(local, "backlog") / "items.json").exists()


@pytest.mark.regression(
    "backlog-lock-resolves-before-migration-scope",
    "AssertionError: assert False",
)
def test_c_flip_between_start_and_scope_entry_backlog_lock(local, monkeypatch):
    _flip_on_first_scope(monkeypatch, local, retire=("backlog",))
    _save_one_item("raced")
    assert not _legacy(local, "backlog").exists()
    assert (_toolkit(local, "backlog") / "items.json").exists()
    assert (_toolkit(local, "backlog") / "_machine_id").exists()


@pytest.mark.regression(
    "machine-id-resolves-before-migration-scope",
    "AssertionError: assert not True",
)
def test_c_flip_between_start_and_scope_entry_machine_id(local, monkeypatch):
    _flip_on_first_scope(monkeypatch, local, retire=("backlog",))
    created = dev_status.machine_id()
    assert not _legacy(local, "backlog").exists()
    stored = (_toolkit(local, "backlog") / "_machine_id").read_text().strip()
    assert stored == created


@pytest.mark.regression(
    "machine-id-repair-resolves-before-migration-scope",
    "AssertionError: assert False",
)
def test_c_flip_between_start_and_scope_entry_machine_id_repair(
    local, monkeypatch, capsys
):
    _flip_on_first_scope(monkeypatch, local, retire=("backlog",))
    monkeypatch.setattr(sys, "argv", ["dev_status.py", "machine-id", "--repair"])
    dev_status.main()
    assert not _legacy(local, "backlog").exists()
    assert (_toolkit(local, "backlog") / "_machine_id").exists()


@pytest.mark.regression(
    "store-read-frozen-at-import",
    "AssertionError: assert [{'id': 'race...ry': 'chore'}] == []",
)
def test_d_reader_sees_new_store_on_next_call_after_flip(local):
    assert dev_status.load_items() == []
    target = _toolkit(local, "backlog") / "items.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"schema_version": 2, "items": [_item("new")]}))
    local.flip("toolkit-home")
    assert [i["id"] for i in dev_status.load_items()] == ["new"]


@pytest.mark.regression(
    "held-legacy-store-path-accepted-after-flip",
    "AttributeError: module 'agent_toolkit_paths' has no attribute 'StaleLayoutError'",
)
@pytest.mark.parametrize("retired", [False, True], ids=["present", "retired"])
def test_e_held_legacy_path_fails_loudly(local, retired):
    held = _legacy(local, "backlog")
    local.flip("toolkit-home")
    if retired:
        _retire_legacy(local, "backlog")
    with pytest.raises(_stale_error()):
        with dev_status_storage.backlog_lock(held, held / ".backlog.lock"):
            pytest.fail("a held legacy path must not be locked")
    assert _legacy(local, "backlog").exists() is not retired
    assert not (held / ".backlog.lock").exists()


@pytest.mark.regression(
    "store-path-attribute-frozen-at-import",
    "AssertionError: assert PosixPath('/tmp/agent-toolkit-test-home-w9ctr0cf/.claude/data/backlog') == PosixPath(",
)
def test_f_compat_attribute_follows_the_layout(local):
    assert dev_status.DATA_DIR == agent_toolkit_paths.path_for("work-items")
    local.flip("toolkit-home")
    assert dev_status.DATA_DIR == _toolkit(local, "backlog")
    assert dev_status.ITEMS_FILE == _toolkit(local, "backlog") / "items.json"


# ── backlog store: preservation ──────────────────────────────────────────────


def test_e2_custom_dir_outside_both_layouts_is_still_created(local, tmp_path):
    custom = tmp_path / "elsewhere" / "backlog"
    local.flip("toolkit-home")
    created = dev_status_storage.machine_id(custom / "_machine_id", custom)
    assert (custom / "_machine_id").read_text().strip() == created


@pytest.mark.regression(
    "store-path-ignores-home-change",
    "AssertionError: assert False",
)
def test_e3_first_run_on_fresh_home_creates_legacy_store(tmp_path):
    fresh = test_layouts.SandboxHome(tmp_path / "fresh", agent_toolkit_paths.write_pointer)
    fresh.home.mkdir()
    with fresh.activated():
        _save_one_item("first")
    assert (fresh.legacy_data / "backlog" / "items.json").exists()


@pytest.mark.regression(
    "store-path-ignores-toolkit-home-override",
    "AssertionError: assert False",
)
def test_e3_first_run_under_override_creates_toolkit_store(tmp_path, monkeypatch):
    fresh = test_layouts.SandboxHome(tmp_path / "fresh", agent_toolkit_paths.write_pointer)
    fresh.home.mkdir()
    override = tmp_path / "override"
    with fresh.activated():
        fresh.flip("toolkit-home")
        monkeypatch.setenv("AGENT_TOOLKIT_HOME", str(override))
        _save_one_item("first")
    assert (override / "data" / "backlog" / "items.json").exists()
    assert not (fresh.legacy_data / "backlog").exists()


# ── other modules: regression ────────────────────────────────────────────────


@pytest.mark.regression(
    "backend-log-resolves-before-migration-scope",
    "AssertionError: assert not True",
)
def test_g_backend_log_follows_a_flip_in_the_scope_gap(local, monkeypatch):
    _flip_on_first_scope(monkeypatch, local, retire=("backend_calls.jsonl",))
    llm_backends._log_backend_call("codex", None, "success", 1.0, 10)
    assert not _legacy(local, "backend_calls.jsonl").exists()
    assert _toolkit(local, "backend_calls.jsonl").read_text().strip()


@pytest.mark.regression(
    "guard-layout-error-frozen-at-import",
    "assert None is not None",
)
def test_g_guard_rails_denies_when_pointer_breaks_after_import(local):
    assert guard_rails._layout_error_reason() is None
    (local.home / agent_toolkit_paths.POINTER_RELPATH).write_text("{not json")
    assert guard_rails._layout_error_reason() is not None


@pytest.mark.regression(
    "guard-audit-log-frozen-at-import",
    "FileNotFoundError: [Errno 2] No such file or directory",
)
def test_g_guard_rails_audit_log_follows_the_layout(local):
    local.flip("toolkit-home")
    verdict = guard_rails.Verdict("allow", "")
    guard_rails._audit_verdict("claude", None, verdict)
    assert _toolkit(local, "guard_rails_audit.jsonl").read_text().strip()


@pytest.mark.regression(
    "claim-lookup-store-frozen-at-import",
    "AssertionError: assert PosixPath('/tmp/agent-toolkit-test-home-w9ctr0cf/.claude/data/backlog/items.json') == PosixPath(",
)
def test_g_claim_lookup_follows_the_layout(local):
    local.flip("toolkit-home")
    expected = _toolkit(local, "backlog") / "items.json"
    assert backlog_claim_lookup.backlog_items_path() == expected


@pytest.mark.regression(
    "grill-store-frozen-at-import",
    "AssertionError: assert [] == ['only-new']",
)
def test_g_grill_reads_the_new_store(local):
    _toolkit(local, "grill").mkdir(parents=True, exist_ok=True)
    (_toolkit(local, "grill") / "only-new.json").write_text("{}")
    local.flip("toolkit-home")
    assert grill.all_session_slugs() == ["only-new"]


@pytest.mark.regression(
    "grill-new-session-lock-frozen-at-import",
    "AssertionError: assert False",
)
def test_g_grill_new_session_lock_follows_the_layout(local):
    local.flip("toolkit-home")
    _retire_legacy(local, "grill")
    with grill._new_session_lock():
        pass
    assert not _legacy(local, "grill").exists()
    assert (_toolkit(local, "grill") / "._new_session.lock").exists()


@pytest.mark.regression(
    "second-opinion-store-frozen-at-import",
    "AssertionError: assert False",
)
def test_g_second_opinion_creates_the_new_store(local):
    local.flip("toolkit-home")
    second_opinion.ensure_data_dir()
    assert _toolkit(local, "grill").is_dir()


@pytest.mark.regression(
    "ticket-batch-store-frozen-at-import",
    "AssertionError: assert False",
)
def test_g_to_tickets_creates_the_new_store(local):
    local.flip("toolkit-home")
    to_tickets_runner.ensure_data_dir()
    assert _toolkit(local, "to-tickets").is_dir()


@pytest.mark.regression(
    "standup-paths-frozen-at-import",
    "AssertionError: assert {} == {'commit_days': 7}",
)
def test_g_standup_reads_the_new_store(local):
    new = _toolkit(local, "standup")
    new.mkdir(parents=True, exist_ok=True)
    (new / "config.json").write_text(json.dumps({"commit_days": 7}))
    (new / "2026-09-22.md").write_text("new standup")
    local.flip("toolkit-home")
    assert standup.load_config() == {"commit_days": 7}
    previous = standup.find_previous_standup(date(2026, 9, 23))
    assert previous is not None and previous["content"] == "new standup"


@pytest.mark.regression(
    "vitals-default-dir-frozen-at-import",
    "AssertionError: assert [PosixPath('/...rill/vitals')] == [PosixPath('/...rill/vitals')]",
)
def test_g_vitals_default_data_dir_follows_the_layout(local, monkeypatch):
    seen: list[Path] = []

    def fake_search(vitals_dir: Path, *args: object) -> list[object]:
        seen.append(vitals_dir)
        return []

    monkeypatch.setattr(vitals_promotion, "search_vitals", fake_search)
    monkeypatch.setattr(sys, "argv", ["vitals_promotion.py", "--search", "x"])
    local.flip("toolkit-home")
    vitals_promotion._main()
    assert seen == [_toolkit(local, "grill") / "vitals"]


# ── held legacy paths through other entry points ─────────────────────────────


@pytest.mark.regression(
    "grill-accepts-held-legacy-store",
    "AttributeError: module 'agent_toolkit_paths' has no attribute 'StaleLayoutError'",
)
def test_h_grill_session_lock_refuses_held_legacy_dir(local):
    held = _legacy(local, "grill")
    local.flip("toolkit-home")
    with pytest.raises(_stale_error()):
        with grill.session_lock("x", data_dir=held):
            pytest.fail("must not lock a stale store")
    assert not (held / ".x.lock").exists()


@pytest.mark.regression(
    "ticket-batch-accepts-held-legacy-path",
    "AttributeError: module 'agent_toolkit_paths' has no attribute 'StaleLayoutError'",
)
def test_h_to_tickets_refuses_held_legacy_batch(local):
    batch = _legacy(local, "to-tickets") / "batch.json"
    batch.write_text(json.dumps({"tickets": []}))
    local.flip("toolkit-home")
    with pytest.raises(_stale_error()):
        to_tickets_runner.run_batch(batch)
    assert not batch.with_suffix(".state.json").exists()


@pytest.mark.regression(
    "vitals-accepts-held-legacy-store",
    "AttributeError: module 'agent_toolkit_paths' has no attribute 'StaleLayoutError'",
)
def test_h_vitals_refuses_held_legacy_data_dir(local, monkeypatch):
    held = _legacy(local, "grill")
    monkeypatch.setattr(
        sys, "argv", ["vitals_promotion.py", "--data-dir", str(held), "--apply"]
    )
    local.flip("toolkit-home")
    with pytest.raises(_stale_error()):
        vitals_promotion._main()
    assert not (held / "vitals").exists()


@pytest.mark.regression(
    "store-writers-accept-held-legacy-paths",
    "AttributeError: module 'agent_toolkit_paths' has no attribute 'StaleLayoutError'",
)
@pytest.mark.parametrize(
    "write",
    [
        pytest.param(
            lambda p: dev_status._atomic_write_json(p / "items.json", "{}", ".t_"),
            id="atomic_write_json",
        ),
        pytest.param(
            lambda p: dev_status._backup_before_bulk_delete(p / "items.json"),
            id="backup_before_bulk_delete",
        ),
        pytest.param(
            lambda p: dev_status_storage.append_journal_event(
                {"cmd": "x"}, journal_file=p / "journal.jsonl", data_dir=p
            ),
            id="append_journal_event",
        ),
        pytest.param(
            lambda p: dev_status_storage.append_run_record(
                {"item": "x"}, runs_file=p / "runs.jsonl", data_dir=p
            ),
            id="append_run_record",
        ),
    ],
)
def test_h_low_level_writers_refuse_held_legacy_paths(local, write):
    held = _legacy(local, "backlog")
    (held / "items.json").write_text("{}")
    before = sorted(p.name for p in held.iterdir())
    local.flip("toolkit-home")
    with pytest.raises(_stale_error()):
        write(held)
    assert sorted(p.name for p in held.iterdir()) == before


# ── check_not_stale: new API ─────────────────────────────────────────────────


def test_i_overlapping_roots_never_flag_a_live_path(local, monkeypatch):
    monkeypatch.setenv("AGENT_TOOLKIT_HOME", str(local.home / ".claude"))
    local.flip("toolkit-home")
    live = agent_toolkit_paths.path_for("work-items")
    assert live == local.legacy_data / "backlog"
    agent_toolkit_paths.check_not_stale(live / "items.json")


def test_i_relative_override_is_ignored_under_legacy(local, monkeypatch):
    monkeypatch.setenv("AGENT_TOOLKIT_HOME", "relative/root")
    agent_toolkit_paths.check_not_stale(_legacy(local, "backlog") / "items.json")


def test_i_path_outside_both_layouts_passes(local, tmp_path):
    local.flip("toolkit-home")
    agent_toolkit_paths.check_not_stale(tmp_path / "custom" / "items.json")


def test_i_toolkit_path_is_stale_after_flip_back(local):
    local.flip("toolkit-home")
    live = agent_toolkit_paths.path_for("decisions")
    local.flip("legacy")
    with pytest.raises(_stale_error()):
        agent_toolkit_paths.check_not_stale(live / "x.json")


def test_i_machine_id_read_takes_no_scope(local, monkeypatch):
    created = dev_status.machine_id()
    seen: list[str] = []
    real = migration_lock.shared

    def spy(site: str, **kwargs: object):
        seen.append(site)
        return real(site, **kwargs)

    monkeypatch.setattr(migration_lock, "shared", spy)
    assert dev_status.machine_id() == created
    assert seen == []
