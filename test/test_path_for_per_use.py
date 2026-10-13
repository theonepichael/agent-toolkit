#!/usr/bin/env python3
"""Toolkit data paths are resolved at each use, never frozen at import.

Every module here was imported (by conftest) before any test home existed,
which is exactly the situation of a long-lived process. Each test activates a
two-layout sandbox home (legacy + toolkit-home directory trees) and checks
that writers and readers resolve to the toolkit-home store, and that a path
under the retired legacy layout is refused instead of quietly recreating it.
"""

import json
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent-scripts"))

import agent_toolkit_paths  # noqa: E402
import dev_status  # noqa: E402
import dev_status_storage  # noqa: E402
import grill  # noqa: E402
import migration_lock  # noqa: E402
import test_layouts  # noqa: E402
import to_tickets_runner  # noqa: E402
import vitals_promotion  # noqa: E402


def _stale_error() -> type[Exception]:
    return agent_toolkit_paths.StaleLayoutError


@pytest.fixture
def local(tmp_path: Path):
    homes = test_layouts.build_two_layout_homes(tmp_path)
    with homes.local.activated():
        yield homes.local


def _legacy(home: test_layouts.SandboxHome, name: str) -> Path:
    return home.legacy_data / name


def _toolkit(home: test_layouts.SandboxHome, name: str) -> Path:
    return home.toolkit_home / "data" / name


def _item(slug: str) -> dict[str, object]:
    return {"id": slug, "summary": slug, "status": "open", "category": "chore"}


def _save_one_item(slug: str) -> None:
    with dev_status.backlog_lock():
        dev_status.save_items([_item(slug)])
        dev_status.bump_rev()


# ── store location ───────────────────────────────────────────────────────────


def test_e2_custom_dir_outside_both_layouts_is_still_created(local, tmp_path):
    custom = tmp_path / "elsewhere" / "backlog"
    created = dev_status_storage.machine_id(custom / "_machine_id", custom)
    assert (custom / "_machine_id").read_text().strip() == created


@pytest.mark.regression(
    "store-path-ignores-home-change",
    "AssertionError: assert False",
)
def test_e3_first_run_on_fresh_home_creates_toolkit_home_store(tmp_path):
    fresh = test_layouts.SandboxHome(tmp_path / "fresh")
    fresh.home.mkdir()
    with fresh.activated():
        _save_one_item("first")
    assert (fresh.toolkit_home / "data" / "backlog" / "items.json").exists()
    assert not fresh.legacy_data.exists()


@pytest.mark.regression(
    "store-path-ignores-toolkit-home-override",
    "AssertionError: assert False",
)
def test_e3_first_run_under_override_creates_toolkit_store(tmp_path, monkeypatch):
    fresh = test_layouts.SandboxHome(tmp_path / "fresh")
    fresh.home.mkdir()
    override = tmp_path / "override"
    with fresh.activated():
        monkeypatch.setenv("AGENT_TOOLKIT_HOME", str(override))
        _save_one_item("first")
    assert (override / "data" / "backlog" / "items.json").exists()
    assert not (fresh.legacy_data / "backlog").exists()


# ── held legacy paths are refused by every entry point ───────────────────────


@pytest.mark.regression(
    "grill-accepts-held-legacy-store",
    "AttributeError: module 'agent_toolkit_paths' has no attribute 'StaleLayoutError'",
)
def test_h_grill_session_lock_refuses_held_legacy_dir(local):
    held = _legacy(local, "grill")
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
    with pytest.raises(_stale_error()):
        write(held)
    assert sorted(p.name for p in held.iterdir()) == before


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
