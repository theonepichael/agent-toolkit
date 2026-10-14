#!/usr/bin/env python3
"""Toolkit data paths are resolved at each use, never frozen at import.

Every module here was imported (by conftest) before any test home existed,
which is exactly the situation of a long-lived process. Each test activates a
sandbox home and checks that writers and readers resolve to the toolkit-home
store.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent-scripts"))

import dev_status  # noqa: E402
import dev_status_storage  # noqa: E402
import migration_lock  # noqa: E402
import test_layouts  # noqa: E402


def _item(slug: str) -> dict[str, object]:
    return {"id": slug, "summary": slug, "status": "open", "category": "chore"}


def _save_one_item(slug: str) -> None:
    with dev_status.backlog_lock():
        dev_status.save_items([_item(slug)])
        dev_status.bump_rev()


# ── store location ───────────────────────────────────────────────────────────


def test_e2_custom_dir_outside_the_store_is_still_created(tmp_path):
    home = test_layouts.SandboxHome(tmp_path / "home")
    home.home.mkdir()
    with home.activated():
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


# ── read paths take no migration scope ──────────────────────────────────────


def test_i_machine_id_read_takes_no_scope(monkeypatch):
    created = dev_status.machine_id()
    seen: list[str] = []
    real = migration_lock.shared

    def spy(site: str, **kwargs: object):
        seen.append(site)
        return real(site, **kwargs)

    monkeypatch.setattr(migration_lock, "shared", spy)
    assert dev_status.machine_id() == created
    assert seen == []
