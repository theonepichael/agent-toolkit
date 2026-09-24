#!/usr/bin/env python3
"""dev_status run: the default cwd is the item's own tree, or the run refuses.

A run with no ``--cwd`` used to execute in the related repository's main
checkout, so a gate could pass on evidence from a tree that never held the
item's work. These tests build real repositories and worktrees in tmp_path
and drive ``run_item`` end to end: the command either runs in the checkout
attributed to the item, or it is refused before running and nothing is
recorded. Real subprocess is bounded to ``git`` and the probe command, both
inside tmp_path, which is why the file carries the allow_real_subprocess
marker.
"""

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "agent-scripts"))

import pytest  # noqa: E402
import dev_status_mutation as dsm  # noqa: E402
import worktree_provenance as wp  # noqa: E402

pytestmark = pytest.mark.allow_real_subprocess  # git and a probe in tmp_path only

SLUG = "item-a"


def _git(*args: str, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=path)
    _git("config", "user.email", "t@example.invalid", cwd=path)
    _git("config", "user.name", "t", cwd=path)
    _git("config", "gc.auto", "0", cwd=path)
    _git("config", "maintenance.auto", "false", cwd=path)
    (path / "tracked.txt").write_text("x\n")
    _git("add", "tracked.txt", cwd=path)
    _git("commit", "-qm", "init", cwd=path)
    return path


def _commit(path: Path, name: str) -> None:
    (path / name).write_text(f"{name}\n")
    _git("add", name, cwd=path)
    _git("commit", "-qm", name, cwd=path)


def _item(repo: Path, *, status: str = "in-progress", **extra: object) -> dict:
    item = {
        "id": SLUG,
        "created": "2026-01-01",
        "updated": "2026-01-01",
        "status": status,
        "summary": "s",
        "category": "bug",
        "blocked_by": [],
        "related_files": [{"path": str(repo / "tracked.txt"), "note": "n"}],
        "context": "c",
        "next_steps": "n",
    }
    item.update(extra)
    return item


class Store:
    def __init__(self, root: Path) -> None:
        self.dir = root / "store"
        self.dir.mkdir()
        self.items = self.dir / "items.json"
        self.runs = self.dir / "runs.jsonl"

    def write(self, *items: dict) -> None:
        self.items.write_text(json.dumps({"schema_version": 2, "items": list(items)}))

    def rows(self) -> list[dict]:
        if not self.runs.exists():
            return []
        return [json.loads(line) for line in self.runs.read_text().splitlines() if line]


@pytest.fixture()
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Store:
    monkeypatch.chdir(tmp_path)
    return Store(tmp_path)


def _run(store: Store, **kw: object) -> dsm.RunResult:
    probe = [sys.executable, "-c", "import os; open('ran', 'w').write(os.getcwd())"]
    return dsm.run_item(SLUG, probe, items_path=store.items, timeout=30, **kw)


def _refused(store: Store, *, match: str, **kw: object) -> None:
    with pytest.raises(dsm.ValidationError, match=match):
        _run(store, **kw)
    assert store.rows() == []


def _ran_in(store: Store, path: Path) -> dict:
    res = _run(store)
    assert Path(res.cwd) == path.resolve()
    assert (path / "ran").read_text() == str(path.resolve())
    row = store.rows()[-1]
    assert row["cwd"] == str(path.resolve())
    assert row["head"] == _git("rev-parse", "HEAD", cwd=path)
    return row


# ── an attributed worktree wins over the main checkout ────────────────────


def test_marker_worktree_is_chosen_over_the_main_checkout(store, tmp_path):
    repo = _init_repo(tmp_path / "proj")
    wt = tmp_path / "proj-wt"
    _git("worktree", "add", "-q", str(wt), "-b", "renamed", cwd=repo)
    wp.write_marker(wt, SLUG)
    store.write(_item(repo))
    _ran_in(store, wt)


def test_slug_branch_worktree_is_chosen(store, tmp_path):
    repo = _init_repo(tmp_path / "proj")
    wt = tmp_path / "proj-wt"
    _git("worktree", "add", "-q", str(wt), "-b", SLUG, cwd=repo)
    store.write(_item(repo))
    _ran_in(store, wt)


def test_an_item_in_review_still_finds_its_marker_worktree(store, tmp_path):
    repo = _init_repo(tmp_path / "proj")
    wt = tmp_path / "proj-wt"
    _git("worktree", "add", "-q", str(wt), "-b", "renamed", cwd=repo)
    wp.write_marker(wt, SLUG)
    store.write(_item(repo, status="in-review"))
    _ran_in(store, wt)


def test_stale_marker_falls_back_to_the_branch(store, tmp_path):
    repo = _init_repo(tmp_path / "proj")
    wt = tmp_path / "proj-wt"
    _git("worktree", "add", "-q", str(wt), "-b", SLUG, cwd=repo)
    wp.write_marker(wt, "long-done")
    store.write(_item(repo))
    _ran_in(store, wt)


def test_another_live_items_marker_wins_over_the_branch(store, tmp_path):
    repo = _init_repo(tmp_path / "proj")
    wt = tmp_path / "proj-wt"
    _git("worktree", "add", "-q", str(wt), "-b", SLUG, cwd=repo)
    _commit(wt, "work.txt")
    wp.write_marker(wt, "other")
    other = _item(repo)
    other["id"] = "other"
    store.write(_item(repo), other)
    # Not attributed to this item, and the main checkout is on main with no
    # merged item branch, so the fallback refuses.
    _refused(store, match="not merged")


def test_session_cwd_worktree_of_an_unlisted_repo_is_chosen(
    store, tmp_path, monkeypatch
):
    listed = _init_repo(tmp_path / "listed")
    other = _init_repo(tmp_path / "other")
    wt = tmp_path / "other-wt"
    _git("worktree", "add", "-q", str(wt), "-b", SLUG, cwd=other)
    (wt / "sub").mkdir()
    monkeypatch.chdir(wt / "sub")
    store.write(_item(listed))
    _ran_in(store, wt)


def test_two_attributed_worktrees_refuse(store, tmp_path):
    repo = _init_repo(tmp_path / "proj")
    for name in ("wt1", "wt2"):
        wt = tmp_path / name
        _git("worktree", "add", "-q", str(wt), "-b", name, cwd=repo)
        wp.write_marker(wt, SLUG)
    store.write(_item(repo))
    _refused(store, match="ambiguous")


def test_an_invalid_marker_refuses(store, tmp_path):
    repo = _init_repo(tmp_path / "proj")
    wt = tmp_path / "proj-wt"
    _git("worktree", "add", "-q", str(wt), "-b", SLUG, cwd=repo)
    git_dir = Path(_git("rev-parse", "--absolute-git-dir", cwd=wt))
    wp.marker_path(git_dir).write_text("one\ntwo\n")
    store.write(_item(repo))
    _refused(store, match="marker")


# ── no worktree: the main checkout only when it holds the work ───────────


def test_merged_work_runs_in_the_main_checkout_on_main(store, tmp_path):
    repo = _init_repo(tmp_path / "proj")
    _git("branch", SLUG, cwd=repo)
    store.write(_item(repo))
    _ran_in(store, repo)


def test_squash_merged_item_with_no_branch_runs_in_the_main_checkout(
    store, tmp_path
):
    repo = _init_repo(tmp_path / "proj")
    store.write(_item(repo))
    _ran_in(store, repo)


def test_unmerged_item_branch_refuses(store, tmp_path):
    repo = _init_repo(tmp_path / "proj")
    _git("checkout", "-q", "-b", SLUG, cwd=repo)
    _commit(repo, "work.txt")
    _git("checkout", "-q", "main", cwd=repo)
    store.write(_item(repo))
    _refused(store, match="not merged")


def test_integration_branch_work_refuses_on_main(store, tmp_path):
    repo = _init_repo(tmp_path / "proj")
    _git("branch", "release-1", cwd=repo)
    store.write(_item(repo, integration_branch="release-1"))
    _refused(store, match="release-1")


def test_integration_branch_checkout_runs(store, tmp_path):
    repo = _init_repo(tmp_path / "proj")
    _git("checkout", "-q", "-b", "release-1", cwd=repo)
    store.write(_item(repo, integration_branch="release-1"))
    _ran_in(store, repo)


def test_missing_integration_branch_refuses(store, tmp_path):
    repo = _init_repo(tmp_path / "proj")
    store.write(_item(repo, integration_branch="release-9"))
    _refused(store, match="release-9")


def test_detached_main_checkout_refuses(store, tmp_path):
    repo = _init_repo(tmp_path / "proj")
    _git("checkout", "-q", "--detach", cwd=repo)
    store.write(_item(repo))
    _refused(store, match="detached")


def test_one_refusing_repo_refuses_the_run(store, tmp_path):
    good = _init_repo(tmp_path / "good")
    bad = _init_repo(tmp_path / "bad")
    _git("checkout", "-q", "--detach", cwd=bad)
    item = _item(good)
    item["related_files"].append({"path": str(bad / "tracked.txt"), "note": "n"})
    store.write(item)
    _refused(store, match="detached")


def test_a_relative_related_path_refuses(store, tmp_path):
    repo = _init_repo(tmp_path / "proj")
    item = _item(repo)
    item["related_files"].append({"path": "relative/file.py", "note": "n"})
    store.write(item)
    _refused(store, match="relative")


# ── no repository, explicit --cwd, and HEAD ───────────────────────────────


def test_no_repository_runs_in_the_session_cwd(store, tmp_path):
    notes = tmp_path / "notes"
    notes.mkdir()
    item = _item(tmp_path)
    item["related_files"] = [{"path": str(notes / "spec.md"), "note": "n"}]
    store.write(item)
    res = _run(store)
    assert Path(res.cwd) == tmp_path.resolve()
    assert store.rows()[-1]["head"] is None


def test_explicit_cwd_outside_git_runs_with_no_head(store, tmp_path):
    repo = _init_repo(tmp_path / "proj")
    plain = tmp_path / "plain"
    plain.mkdir()
    store.write(_item(repo))
    res = _run(store, cwd=str(plain))
    assert Path(res.cwd) == plain.resolve()
    assert store.rows()[-1]["head"] is None


def test_unreadable_head_refuses_even_with_explicit_cwd(store, tmp_path):
    repo = _init_repo(tmp_path / "proj")
    unborn = tmp_path / "unborn"
    unborn.mkdir()
    _git("init", "-q", "-b", "main", cwd=unborn)
    store.write(_item(repo))
    _refused(store, match="HEAD", cwd=str(unborn))
    assert not (unborn / "ran").exists()


def test_runs_listing_shows_head_and_cwd(store, tmp_path, capsys):
    import dev_status_impl

    repo = _init_repo(tmp_path / "proj")
    wt = tmp_path / "proj-wt"
    _git("worktree", "add", "-q", str(wt), "-b", SLUG, cwd=repo)
    store.write(_item(repo))
    row = _ran_in(store, wt)
    old = {k: v for k, v in row.items() if k != "head"}
    with store.runs.open("a") as fh:
        fh.write(json.dumps({**old, "run_id": "0" * 32}) + "\n")
    lines = dev_status_impl.format_run_rows(store.rows())
    assert row["head"][:12] in lines[0] and str(wt.resolve()) in lines[0]
    assert " - " in lines[1]
