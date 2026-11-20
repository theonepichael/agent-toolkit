#!/usr/bin/env python3
"""R3 (stale worktree base) compares against the worktree item's
integration branch, not always origin/main."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent-scripts"))
import test_bootstrap  # noqa: E402, F401

import guard_rails  # noqa: E402
from backlog_claim_lookup import LocalClaimLookup  # noqa: E402


class FakeLookup:
    """BacklogClaimLookup fake: in-progress items plus any-status items by slug."""

    def __init__(
        self, in_progress: list[dict] | None = None, others: list[dict] | None = None
    ) -> None:
        self._in_progress = in_progress or []
        self._all = {str(i["id"]): i for i in [*self._in_progress, *(others or [])]}

    def in_progress_items(self) -> list[dict]:
        return list(self._in_progress)

    def ready_items(self, prefix: str | None = None) -> list[dict]:
        return []

    def claim_info(self, slug: str) -> object:
        return None

    def get_item(self, slug: str) -> dict | None:
        return self._all.get(slug)


def _info(branch: str, marker: str | None = None, *, worktree: bool = True):
    return guard_rails.RepoInfo(
        toplevel="/wt",
        common_dir="/repo/.git",
        is_worktree=worktree,
        is_bare=False,
        branch=branch,
        marker_slug=marker,
    )


def _item(slug: str, status: str = "in-progress", **extra: object) -> dict:
    return {"id": slug, "status": status, "related_files": [], **extra}


def _ref(info: guard_rails.RepoInfo, lookup: FakeLookup) -> str:
    items = lookup.in_progress_items()
    return guard_rails._stale_base_ref(
        info, items, {str(i["id"]) for i in items}, lookup
    )


class TestStaleBaseRef:
    def test_in_progress_item_with_integration_branch(self) -> None:
        lookup = FakeLookup([_item("rel", integration_branch="release-1")])
        assert _ref(_info("rel"), lookup) == "origin/release-1"

    def test_item_without_integration_branch_is_main(self) -> None:
        assert _ref(_info("plain"), FakeLookup([_item("plain")])) == "origin/main"

    def test_no_attributed_item_is_main(self) -> None:
        assert _ref(_info("feature"), FakeLookup()) == "origin/main"

    def test_empty_or_non_string_integration_branch_is_main(self) -> None:
        for value in ("", 7, None):
            lookup = FakeLookup([_item("x", integration_branch=value)])
            assert _ref(_info("x"), lookup) == "origin/main"

    def test_live_marker_beats_branch_name(self) -> None:
        lookup = FakeLookup(
            [
                _item("marked", integration_branch="release-1"),
                _item("branchy", integration_branch="release-2"),
            ]
        )
        assert _ref(_info("branchy", marker="marked"), lookup) == "origin/release-1"

    def test_done_item_resolves_through_marker_even_on_custom_branch(self) -> None:
        lookup = FakeLookup(
            others=[_item("old", status="done", integration_branch="release-1")]
        )
        assert _ref(_info("feature-x", marker="old"), lookup) == "origin/release-1"

    def test_bare_branch_name_never_resolves_a_done_item(self) -> None:
        lookup = FakeLookup(
            others=[_item("hotfix", status="done", integration_branch="release-1")]
        )
        assert _ref(_info("hotfix"), lookup) == "origin/main"

    def test_protected_branch_ignores_the_marker(self) -> None:
        lookup = FakeLookup(
            others=[_item("old", status="done", integration_branch="release-1")]
        )
        assert _ref(_info("main", marker="old"), lookup) == "origin/main"

    def test_in_progress_item_on_new_branch_beats_stale_marker(self) -> None:
        lookup = FakeLookup(
            [_item("new-item")],
            others=[_item("old", status="done", integration_branch="release-1")],
        )
        assert _ref(_info("new-item", marker="old"), lookup) == "origin/main"


class TestLocalClaimLookupGetItem:
    def test_hit_any_status_and_miss(self, tmp_path: Path) -> None:
        store = tmp_path / "items.json"
        store.write_text(
            json.dumps({"items": [_item("done-one", status="done", integration_branch="r")]})
        )
        lookup = LocalClaimLookup(store_path=store)
        item = lookup.get_item("done-one")
        assert item is not None and item["integration_branch"] == "r"
        assert lookup.get_item("absent") is None

    def test_unreadable_store_is_none(self, tmp_path: Path) -> None:
        lookup = LocalClaimLookup(store_path=tmp_path / "missing.json")
        assert lookup.get_item("anything") is None


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name)
    _git(repo, "add", name)
    _git(repo, "commit", "-qm", name)
    return _git(repo, "rev-parse", "HEAD")


@pytest.mark.allow_real_subprocess  # git against a tmp_path repo
class TestStaleBaseEndToEnd:
    @pytest.fixture
    def worktree(self, tmp_path: Path) -> tuple[Path, Path]:
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(repo, "init", "-q", "-b", "main")
        _git(repo, "config", "user.name", "T")
        _git(repo, "config", "user.email", "t@example.com")
        _git(repo, "config", "gc.auto", "0")
        _git(repo, "config", "maintenance.auto", "false")
        base = _commit(repo, "a")
        _git(repo, "branch", "release-1")
        _git(repo, "update-ref", "refs/remotes/origin/main", base)
        _git(repo, "update-ref", "refs/remotes/origin/release-1", base)
        wt = tmp_path / "wt"
        _git(repo, "worktree", "add", "-q", "-b", "rel-item", str(wt), "release-1")
        # origin/main moves ahead of the item's base; release-1 does not.
        _commit(repo, "b")
        _git(repo, "update-ref", "refs/remotes/origin/main", _git(repo, "rev-parse", "main"))
        return repo, wt

    def _evaluate(self, wt: Path, lookup: FakeLookup) -> guard_rails.Verdict:
        with mock.patch.object(
            guard_rails, "_evaluate_claim", return_value=guard_rails.Verdict("allow")
        ):
            return guard_rails.evaluate(
                guard_rails.Request("write", str(wt), str(wt / "x.py")), lookup
            )

    def test_release_worktree_not_warned_when_only_main_moved(
        self, worktree: tuple[Path, Path]
    ) -> None:
        _repo, wt = worktree
        lookup = FakeLookup([_item("rel-item", integration_branch="release-1")])
        assert self._evaluate(wt, lookup).decision == "allow"

    def test_release_worktree_warned_naming_release_ref(
        self, worktree: tuple[Path, Path]
    ) -> None:
        repo, wt = worktree
        _git(repo, "checkout", "-q", "release-1")
        _git(repo, "update-ref", "refs/remotes/origin/release-1", _commit(repo, "c"))
        _git(repo, "checkout", "-q", "main")
        lookup = FakeLookup([_item("rel-item", integration_branch="release-1")])
        verdict = self._evaluate(wt, lookup)
        assert verdict.decision == "warn"
        assert verdict.rule == "stale-worktree-base"
        assert "origin/release-1" in verdict.reason

    def test_missing_remote_integration_ref_is_silent(
        self, worktree: tuple[Path, Path]
    ) -> None:
        repo, wt = worktree
        _git(repo, "update-ref", "-d", "refs/remotes/origin/release-1")
        lookup = FakeLookup([_item("rel-item", integration_branch="release-1")])
        assert self._evaluate(wt, lookup).decision == "allow"

    def test_plain_item_still_compared_to_origin_main(
        self, worktree: tuple[Path, Path]
    ) -> None:
        _repo, wt = worktree
        verdict = self._evaluate(wt, FakeLookup([_item("rel-item")]))
        assert verdict.decision == "warn"
        assert "origin/main" in verdict.reason


# ── the warn cursor: once per worktree per base commit ─────────────────────

GUARD_SCRIPT = Path(__file__).resolve().parent.parent / "agent-scripts/guard_rails.py"


def _behind_worktree(tmp_path: Path) -> tuple[Path, Path]:
    """A repo whose ``origin/main`` is one commit ahead of a linked worktree."""
    repo = tmp_path / "repo2"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "T")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "gc.auto", "0")
    _git(repo, "config", "maintenance.auto", "false")
    base = _commit(repo, "base")
    _git(repo, "worktree", "add", "-q", "-b", "item", str(tmp_path / "wt"), base)
    head = _commit(repo, "next")
    _git(repo, "update-ref", "refs/remotes/origin/main", head)
    return repo, tmp_path / "wt"


def _advance_upstream(repo: Path) -> str:
    """Move ``origin/main`` on by one commit; return the new base sha."""
    head = _commit(repo, f"more-{len(list(repo.iterdir()))}")
    _git(repo, "update-ref", "refs/remotes/origin/main", head)
    return head


def _run_guard(worktree: Path) -> dict[str, object]:
    """One real guard process for a write inside ``worktree``."""
    out = subprocess.run(
        [
            sys.executable,
            str(GUARD_SCRIPT),
            "--harness",
            "pi",
            "--tool",
            "write",
            "--cwd",
            str(worktree),
            "--path",
            str(worktree / "x.py"),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(out.stdout)


@pytest.fixture
def state_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``XDG_STATE_HOME`` at this test's directory; return the cursor."""
    state = tmp_path / "xdg-state"
    state.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    return state / "agent-toolkit" / "guard_rails_stale_base.json"


class TestStaleBaseWarnCursor:
    @pytest.mark.allow_real_subprocess  # spawns the guard CLI against a tmp git repo
    @pytest.mark.regression(
        "stale-base-warn-repeats-on-every-write",
        "AssertionError: assert ['warn', 'warn', 'warn'] == ['warn', 'allow', 'allow']",
    )
    def test_repeated_writes_warn_once(
        self, tmp_path: Path, state_home: Path
    ) -> None:
        _repo, wt = _behind_worktree(tmp_path)
        decisions = [_run_guard(wt)["decision"] for _ in range(3)]
        assert decisions == ["warn", "allow", "allow"]

    @pytest.mark.allow_real_subprocess  # spawns the guard CLI against a tmp git repo
    def test_warns_again_when_the_base_moves(
        self, tmp_path: Path, state_home: Path
    ) -> None:
        repo, wt = _behind_worktree(tmp_path)
        first = _run_guard(wt)
        assert first["decision"] == "warn"
        assert "origin/main" in str(first["reason"])
        assert _run_guard(wt)["decision"] == "allow"

        _advance_upstream(repo)

        moved = _run_guard(wt)
        assert moved["decision"] == "warn"
        assert "origin/main" in str(moved["reason"])
        assert _run_guard(wt)["decision"] == "allow"

    @pytest.mark.allow_real_subprocess  # spawns the guard CLI against a tmp git repo
    def test_audit_records_each_call(
        self, tmp_path: Path, state_home: Path
    ) -> None:
        _repo, wt = _behind_worktree(tmp_path)
        _run_guard(wt)
        _run_guard(wt)

        import agent_toolkit_paths

        log = agent_toolkit_paths.path_for("guard-rail-log")
        records = [json.loads(line) for line in log.read_text().splitlines()]
        mine = [
            record
            for record in records
            if record.get("target") == str(wt / "x.py")
        ]
        assert [record["rule"] for record in mine] == [
            "stale-worktree-base",
            "stale-worktree-base-repeat",
        ]
        assert [record["decision"] for record in mine] == ["allow", "allow"]

    @pytest.mark.allow_real_subprocess  # spawns the guard CLI against a tmp git repo
    def test_corrupt_state_warns_then_rewrites(
        self, tmp_path: Path, state_home: Path
    ) -> None:
        repo, wt = _behind_worktree(tmp_path)
        path = state_home
        path.parent.mkdir(parents=True)
        path.write_text("{not json")

        assert _run_guard(wt)["decision"] == "warn"

        expected = _git(repo, "rev-parse", "origin/main")
        assert json.loads(path.read_text())[str(wt)] == expected
        assert _run_guard(wt)["decision"] == "allow"

    @pytest.mark.allow_real_subprocess  # spawns the guard CLI against a tmp git repo
    def test_state_lands_in_the_sandboxed_state_dir(
        self, tmp_path: Path, state_home: Path
    ) -> None:
        _repo, wt = _behind_worktree(tmp_path)
        _run_guard(wt)

        assert state_home.is_file()
        json.loads(state_home.read_text())  # a complete, parseable cursor

