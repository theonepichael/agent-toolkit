#!/usr/bin/env python3
"""Tests for migrate_path_transform.py: the reversible stored-path transform.

A sandbox HOME supplies the legacy and toolkit-home roots; a separate staging
tree holds the store copies the transform rewrites. Consumer tests (the real
``run_batch`` and ``approve_item``) each run on their own copy, because both
mutate the stores they read.
"""

import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "agent-scripts"))

import agent_toolkit_paths  # noqa: E402
import dev_status_mutation  # noqa: E402
import migrate_path_transform as mpt  # noqa: E402
import migrate_toolkit_home as mth  # noqa: E402
import migration_lock  # noqa: E402
import test_fault_injection as fi  # noqa: E402
import to_tickets_runner  # noqa: E402

pytestmark = pytest.mark.usefixtures("home")


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("XDG_STATE_HOME", str(h / ".local" / "state"))
    monkeypatch.delenv(agent_toolkit_paths.ENV_HOME, raising=False)
    migration_lock._reset_for_tests()
    yield h
    migration_lock._reset_for_tests()


def _legacy(home: Path, seg: str) -> str:
    return str(home / ".claude" / "data" / seg)


def _new(home: Path, seg: str) -> str:
    return str(home / ".agent-toolkit" / "data" / seg)


def _items_payload(items: list[dict]) -> str:
    return json.dumps({"schema_version": 2, "items": items}, indent=2)


def _in_review(slug: str, related: list[dict]) -> dict:
    item = {
        "id": slug,
        "summary": f"Item {slug}",
        "status": "in-review",
        "category": "chore",
        "context": "ctx",
        "next_steps": "next",
        "related_files": related,
        "blocked_by": [],
        "created": "2026-09-23",
        "updated": "2026-09-23",
    }
    item["review_content_hash"] = dev_status_mutation._content_hash(item)
    return item


def _stage(tmp_path: Path, home: Path) -> mpt.StoreFiles:
    """A staging tree whose stores reference the sandbox home's legacy roots."""
    root = tmp_path / "stage"
    backlog = root / "backlog"
    grill = root / "grill"
    batches = root / "to-tickets"
    standup = root / "standup"
    for d in (backlog, grill, grill / "vitals", batches / "sub", standup):
        d.mkdir(parents=True)
    plan = _legacy(home, "grill") + "/topic-plan.md"
    items = [
        _in_review("atk-one", [{"path": plan, "note": f"see {plan}"}]),
        {
            "id": "atk-two",
            "summary": "two",
            "status": "open",
            "category": "chore",
            "context": f"prose mentions {_legacy(home, 'grill')}/x.md",
            "next_steps": [f"read ~/.claude/data/grill/y.md"],
            "related_files": [
                {"path": "~/.claude/data/grill/tilde.md"},
                {"path": str(home / ".claude" / "scripts" / "dev_status.py")},
                {"path": _legacy(home, "draft-issues") + "/d.md"},
                {"path": "relative/path.md"},
                {"path": _legacy(home, "grill-old") + "/sib.md"},
                {"path": _legacy(home, "backlog-out-of-scope") + "/c.md"},
                {"path": _legacy(home, "grill") + "/../draft-issues/z.md"},
                {"path": _legacy(home, "backend_calls.jsonl") + "/child"},
                {"path": _legacy(home, "grill") + "/"},
                {"path": "/elsewhere/foreign.md"},
                "not-a-dict",
            ],
            "blocked_by": [],
        },
    ]
    (backlog / "items.json").write_text(_items_payload(items))
    pending = {
        "schema_version": 1,
        "items": [
            {
                "id": "p-one",
                "status": "waiting_for_reply",
                "description": "d",
                "source_ref": {"draft": _legacy(home, "draft-issues") + "/x.md"},
            }
        ],
    }
    (backlog / "pending_items.json").write_text(json.dumps(pending, indent=2))
    (backlog / "journal.jsonl").write_text('{"cmd": "add"}\n')
    session = {"schema_version": 1, "slug": "topic", "plan_path": plan, "decisions": []}
    (grill / "topic.json").write_text(json.dumps(session, indent=2))
    (grill / "topic-plan.md").write_text(f"# Plan\n\nSee {plan} and ~/.claude/data/grill/a.md\n")
    (grill / "vitals" / "_global.json").write_text("[]")
    batch = [
        {
            "id": "atk-t1",
            "summary": "t1",
            "category": "chore",
            "context": "",
            "next_steps": "",
            "related_files": [{"path": plan}],
            "blocked_by": [],
        }
    ]
    compact = json.dumps(batch, separators=(",", ":"))  # not indent=2: rollback must restore it
    (batches / "b.json").write_text(compact)
    (batches / "sub" / "nested.json").write_text(json.dumps(batch))
    (standup / "config.json").write_text(json.dumps({"git_repos": [str(home / "repo")]}))
    return mpt.StoreFiles(
        root=root,
        items=backlog / "items.json",
        pending=backlog / "pending_items.json",
        grill_dir=grill,
        batches_dir=batches,
        standup_config=standup / "config.json",
    )


def _state_for(batch: Path, added: dict) -> None:
    to_tickets_runner.write_state(
        batch, {"batch_hash": to_tickets_runner._batch_hash(batch), "added": added}
    )


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.is_symlink()
    }


def _items(stores: mpt.StoreFiles) -> dict[str, dict]:
    data = json.loads(stores.items.read_text())
    return {i["id"]: i for i in data["items"]}


# ── roots and classification ───────────────────────────────────────────────


@pytest.mark.regression(
    "stored-path-transform-missing",
    "ModuleNotFoundError: No module named 'migrate_path_transform'",
)
def test_root_map_uses_the_supplied_home_not_process_home(tmp_path):
    other = tmp_path / "other-home"
    roots = mpt.RootMap.for_home(other)
    assert (Path(_legacy(other, "grill")), Path(_new(other, "grill"))) in roots.dirs
    assert (
        Path(_legacy(other, "backend_calls.jsonl")),
        Path(_new(other, "backend_calls.jsonl")),
    ) in roots.files


def test_root_map_honours_an_override_outside_home(tmp_path, monkeypatch):
    override = tmp_path / "outside"
    monkeypatch.setenv(agent_toolkit_paths.ENV_HOME, str(override))
    roots = mpt.RootMap.for_home(tmp_path / "h")
    assert (Path(_legacy(tmp_path / "h", "grill")), override / "data" / "grill") in roots.dirs
    got = roots.classify("~/.claude/data/grill/a.md")
    assert got.kind == "rewrite" and got.new == str(override / "data" / "grill" / "a.md")


@pytest.mark.parametrize(
    ("value", "kind", "expected"),
    [
        ("{L}/grill/a.md", "rewrite", "{N}/grill/a.md"),
        ("{L}/grill", "rewrite", "{N}/grill"),
        ("{L}/grill/", "rewrite", "{N}/grill/"),
        ("~/.claude/data/grill/a.md", "rewrite", "~/.agent-toolkit/data/grill/a.md"),
        ("{L}/backlog-out-of-scope/c.md", "rewrite", "{N}/backlog-out-of-scope/c.md"),
        ("{L}/backend_calls.jsonl", "rewrite", "{N}/backend_calls.jsonl"),
        ("{L}/backend_calls.jsonl/child", "unmatched-structured", None),
        ("{L}/backend_calls.jsonl/", "unmatched-structured", None),
        ("{L}/grill/../draft-issues/z.md", "ambiguous", None),
        ("{L}/grill/./a.md", "ambiguous", None),
        ("{L}/grill//a.md", "ambiguous", None),
        ("{L}/grill-old/sib.md", "untouched", None),
        ("{L}/draft-issues/d.md", "untouched", None),
        ("{L}", "untouched", None),
        ("{H}/.claude/scripts/dev_status.py", "untouched", None),
        ("relative/grill/a.md", "untouched", None),
        ("~other/.claude/data/grill/a.md", "untouched", None),
        ("/elsewhere/foreign.md", "untouched", None),
    ],
)
def test_classify(home, value, kind, expected):
    fill = {"L": str(home / ".claude/data"), "N": str(home / ".agent-toolkit/data"), "H": str(home)}
    roots = mpt.RootMap.for_home(home)
    got = roots.classify(value.format(**fill))
    assert got.kind == kind
    assert got.new == (expected.format(**fill) if expected else None)


def test_classify_carried_roots_rewrite(home):
    legacy = home / ".claude/data"
    carried = mpt.RootMap.for_home(
        home,
        carried_dirs=[(legacy / "draft-issues", home / ".agent-toolkit/data/draft-issues")],
        carried_files=[(legacy / "backlog.json", home / ".agent-toolkit/data/backlog.json")],
    )
    got = carried.classify(str(home / ".claude/data/draft-issues/x.md"))
    assert got.kind == "rewrite"
    assert got.new == str(home / ".agent-toolkit/data/draft-issues/x.md")
    exact = carried.classify(str(legacy / "backlog.json"))
    assert exact.kind == "rewrite"
    assert exact.new == str(home / ".agent-toolkit/data/backlog.json")
    # carried FILE roots still rewrite only an exact match
    inner = carried.classify(str(home / ".claude/data/backlog.json/child"))
    assert inner.kind == "unmatched-structured"


# ── plan ────────────────────────────────────────────────────────────────────


def test_plan_rewrites_only_named_fields(tmp_path, home):
    stores = _stage(tmp_path, home)
    before = _tree_bytes(stores.root)
    plan = mpt.plan_transform(stores, mpt.RootMap.for_home(home))
    assert _tree_bytes(stores.root) == before  # planning writes nothing
    changed = {c.path for c in plan.changes}
    assert changed == {
        stores.items,
        stores.grill_dir / "topic.json",
        stores.batches_dir / "b.json",
        stores.batches_dir / "sub" / "nested.json",
    }
    new_items = {
        i["id"]: i for i in json.loads(next(c.after for c in plan.changes if c.path == stores.items))["items"]
    }
    one = new_items["atk-one"]["related_files"][0]
    assert one["path"] == _new(home, "grill") + "/topic-plan.md"
    assert one["note"] == f"see {_legacy(home, 'grill')}/topic-plan.md"  # prose untouched
    two = [rf["path"] if isinstance(rf, dict) else rf for rf in new_items["atk-two"]["related_files"]]
    assert two[0] == "~/.agent-toolkit/data/grill/tilde.md"
    assert two[1:8] == [
        str(home / ".claude" / "scripts" / "dev_status.py"),
        _legacy(home, "draft-issues") + "/d.md",
        "relative/path.md",
        _legacy(home, "grill-old") + "/sib.md",
        _new(home, "backlog-out-of-scope") + "/c.md",
        _legacy(home, "grill") + "/../draft-issues/z.md",
        _legacy(home, "backend_calls.jsonl") + "/child",
    ]
    assert two[8] == _new(home, "grill") + "/"
    assert two[9:] == ["/elsewhere/foreign.md", "not-a-dict"]
    reasons = {s.reason for s in plan.stale}
    assert {"prose", "markdown", "ambiguous", "unmatched-structured"} <= reasons


def test_untouched_files_stay_byte_identical(tmp_path, home):
    stores = _stage(tmp_path, home)
    before = _tree_bytes(stores.root)
    plan = mpt.plan_transform(stores, mpt.RootMap.for_home(home))
    plan_dir, digest = mpt.save_plan(plan, tmp_path / "work")
    mpt.apply_saved(plan_dir, digest)
    after = _tree_bytes(stores.root)
    changed = {str(c.path.relative_to(stores.root)) for c in plan.changes}
    for name, data in before.items():
        if name not in changed:
            assert after[name] == data, name
    assert after["grill/topic-plan.md"] == before["grill/topic-plan.md"]


def test_items_are_serialised_like_the_store(tmp_path, home):
    stores = _stage(tmp_path, home)
    plan = mpt.plan_transform(stores, mpt.RootMap.for_home(home))
    after = next(c.after for c in plan.changes if c.path == stores.items).decode()
    data = json.loads(after)
    assert after == json.dumps({"schema_version": 2, "items": data["items"]}, indent=2)


def test_extra_envelope_key_is_refused(tmp_path, home):
    stores = _stage(tmp_path, home)
    data = json.loads(stores.items.read_text())
    data["extra"] = 1
    stores.items.write_text(json.dumps(data))
    with pytest.raises(mpt.TransformError):
        mpt.plan_transform(stores, mpt.RootMap.for_home(home))


def test_invalid_json_is_refused(tmp_path, home):
    stores = _stage(tmp_path, home)
    (stores.grill_dir / "topic.json").write_text("{nope")
    with pytest.raises(mpt.TransformError):
        mpt.plan_transform(stores, mpt.RootMap.for_home(home))


def test_symlinked_store_is_refused(tmp_path, home):
    stores = _stage(tmp_path, home)
    real = tmp_path / "live-items.json"
    shutil.copy(stores.items, real)
    stores.items.unlink()
    stores.items.symlink_to(real)
    with pytest.raises(mpt.TransformError):
        mpt.plan_transform(stores, mpt.RootMap.for_home(home))


def test_symlink_met_in_discovery_is_skipped(tmp_path, home):
    stores = _stage(tmp_path, home)
    outside = tmp_path / "outside.json"
    outside.write_text("[]")
    (stores.batches_dir / "link.json").symlink_to(outside)
    plan = mpt.plan_transform(stores, mpt.RootMap.for_home(home))
    assert any(s.reason == "symlink" for s in plan.skipped)
    assert all(c.path.name != "link.json" for c in plan.changes)


# ── hashes ──────────────────────────────────────────────────────────────────


def test_review_hash_is_recomputed_when_it_was_valid(tmp_path, home):
    stores = _stage(tmp_path, home)
    plan = mpt.plan_transform(stores, mpt.RootMap.for_home(home))
    new_items = json.loads(next(c.after for c in plan.changes if c.path == stores.items))["items"]
    one = next(i for i in new_items if i["id"] == "atk-one")
    assert one["review_content_hash"] == dev_status_mutation._content_hash(one)
    assert [h.kind for h in plan.hashes].count("review_content_hash") == 1


def test_stale_review_hash_is_left_and_reported(tmp_path, home):
    stores = _stage(tmp_path, home)
    data = json.loads(stores.items.read_text())
    data["items"][0]["summary"] = "edited after review"
    stores.items.write_text(_items_payload(data["items"]))
    plan = mpt.plan_transform(stores, mpt.RootMap.for_home(home))
    new_items = json.loads(next(c.after for c in plan.changes if c.path == stores.items))["items"]
    assert new_items[0]["review_content_hash"] == data["items"][0]["review_content_hash"]
    assert any(s.kind == "review_content_hash" for s in plan.skipped)


def test_stale_review_hash_that_would_become_valid_is_refused(tmp_path, home):
    stores = _stage(tmp_path, home)
    data = json.loads(stores.items.read_text())
    item = data["items"][0]
    moved = json.loads(json.dumps(item))
    moved["related_files"][0]["path"] = _new(home, "grill") + "/topic-plan.md"
    item["review_content_hash"] = dev_status_mutation._content_hash(moved)
    stores.items.write_text(_items_payload(data["items"]))
    with pytest.raises(mpt.TransformError):
        mpt.plan_transform(stores, mpt.RootMap.for_home(home))


def test_batch_hash_is_recomputed_when_it_was_valid(tmp_path, home):
    stores = _stage(tmp_path, home)
    batch = stores.batches_dir / "b.json"
    _state_for(batch, {})
    plan = mpt.plan_transform(stores, mpt.RootMap.for_home(home))
    new_batch = next(c.after for c in plan.changes if c.path == batch)
    new_state = json.loads(next(c.after for c in plan.changes if c.path == batch.with_suffix(".state.json")))
    assert new_state["batch_hash"] == hashlib.sha256(new_batch).hexdigest()
    state_bytes = next(c.after for c in plan.changes if c.path == batch.with_suffix(".state.json"))
    assert state_bytes == json.dumps(new_state, indent=2).encode()  # write_state format


def test_stale_batch_hash_is_left_and_reported(tmp_path, home):
    stores = _stage(tmp_path, home)
    batch = stores.batches_dir / "b.json"
    batch.with_suffix(".state.json").write_text(json.dumps({"batch_hash": "0" * 64, "added": {}}))
    plan = mpt.plan_transform(stores, mpt.RootMap.for_home(home))
    assert all(c.path != batch.with_suffix(".state.json") for c in plan.changes)
    assert any(s.kind == "batch_hash" for s in plan.skipped)


# ── save / apply / rollback ────────────────────────────────────────────────


def _applied(tmp_path, home):
    stores = _stage(tmp_path, home)
    _state_for(stores.batches_dir / "b.json", {})
    before = _tree_bytes(stores.root)
    plan = mpt.plan_transform(stores, mpt.RootMap.for_home(home))
    plan_dir, digest = mpt.save_plan(plan, tmp_path / "work")
    mpt.apply_saved(plan_dir, digest)
    return stores, before, plan, plan_dir, digest


def test_rollback_restores_every_file_byte_for_byte(tmp_path, home):
    stores, before, _plan, plan_dir, digest = _applied(tmp_path, home)
    assert _tree_bytes(stores.root) != before
    mpt.rollback_saved(plan_dir, digest)
    assert _tree_bytes(stores.root) == before


def test_apply_and_rollback_are_idempotent(tmp_path, home):
    stores, before, _plan, plan_dir, digest = _applied(tmp_path, home)
    after = _tree_bytes(stores.root)
    mpt.apply_saved(plan_dir, digest)
    assert _tree_bytes(stores.root) == after
    mpt.rollback_saved(plan_dir, digest)
    mpt.rollback_saved(plan_dir, digest)
    assert _tree_bytes(stores.root) == before


def test_unrelated_edit_blocks_apply_and_rollback(tmp_path, home):
    stores, _before, _plan, plan_dir, digest = _applied(tmp_path, home)
    data = json.loads(stores.items.read_text())
    data["items"][0]["summary"] = "someone edited this"
    stores.items.write_text(_items_payload(data["items"]))
    snapshot = _tree_bytes(stores.root)
    with pytest.raises(mpt.TransformError):
        mpt.rollback_saved(plan_dir, digest)
    with pytest.raises(mpt.TransformError):
        mpt.apply_saved(plan_dir, digest)
    assert _tree_bytes(stores.root) == snapshot


@pytest.mark.parametrize("damage", ["manifest", "before", "after"])
def test_damaged_saved_plan_is_refused(tmp_path, home, damage):
    stores = _stage(tmp_path, home)
    plan = mpt.plan_transform(stores, mpt.RootMap.for_home(home))
    plan_dir, digest = mpt.save_plan(plan, tmp_path / "work")
    target = {
        "manifest": plan_dir / "manifest.json",
        "before": plan_dir / "before" / "0",
        "after": plan_dir / "after" / "0",
    }[damage]
    os.chmod(target, 0o644)
    target.write_bytes(target.read_bytes() + b" ")
    snapshot = _tree_bytes(stores.root)
    with pytest.raises(mpt.TransformError):
        mpt.apply_saved(plan_dir, digest)
    assert _tree_bytes(stores.root) == snapshot


def test_save_plan_refuses_an_existing_plan(tmp_path, home):
    stores = _stage(tmp_path, home)
    plan = mpt.plan_transform(stores, mpt.RootMap.for_home(home))
    mpt.save_plan(plan, tmp_path / "work")
    with pytest.raises(mpt.TransformError):
        mpt.save_plan(plan, tmp_path / "work")


def test_discard_plan_without_and_with_manifest(tmp_path, home):
    stores = _stage(tmp_path, home)
    plan = mpt.plan_transform(stores, mpt.RootMap.for_home(home))
    plan_dir, _ = mpt.save_plan(plan, tmp_path / "work")
    mpt.discard_plan(plan_dir)
    assert not plan_dir.exists()
    plan_dir, _ = mpt.save_plan(plan, tmp_path / "work")
    (plan_dir / "manifest.json").unlink()
    mpt.discard_plan(plan_dir)
    assert not plan_dir.exists()


def test_discard_plan_refuses_once_applied(tmp_path, home):
    _stores, _before, _plan, plan_dir, _digest = _applied(tmp_path, home)
    with pytest.raises(mpt.TransformError):
        mpt.discard_plan(plan_dir)


def test_symlinked_target_blocks_apply(tmp_path, home):
    stores = _stage(tmp_path, home)
    plan = mpt.plan_transform(stores, mpt.RootMap.for_home(home))
    plan_dir, digest = mpt.save_plan(plan, tmp_path / "work")
    real = tmp_path / "moved-grill"
    stores.grill_dir.rename(real)
    stores.grill_dir.symlink_to(real)
    with pytest.raises(mpt.TransformError):
        mpt.apply_saved(plan_dir, digest)


# ── real consumers ──────────────────────────────────────────────────────────


def _flip(home: Path, layout: str) -> None:
    agent_toolkit_paths.write_pointer(home, layout)


@pytest.mark.allow_real_subprocess  # approve_item's lifecycle check may run git
def test_migrated_in_review_item_approves(tmp_path, home):
    stores, _before, _plan, _dir, _digest = _applied(tmp_path, home)
    result = dev_status_mutation.approve_item("atk-one", items_path=stores.items)
    assert _items(stores)["atk-one"]["status"] == "done", result


@pytest.mark.allow_real_subprocess  # approve_item's lifecycle check may run git
def test_stale_review_stays_refused_after_transform(tmp_path, home):
    stores = _stage(tmp_path, home)
    data = json.loads(stores.items.read_text())
    data["items"][0]["context"] = "edited after review"
    stores.items.write_text(_items_payload(data["items"]))
    plan = mpt.plan_transform(stores, mpt.RootMap.for_home(home))
    plan_dir, digest = mpt.save_plan(plan, tmp_path / "work")
    mpt.apply_saved(plan_dir, digest)
    with pytest.raises(Exception) as refused:  # noqa: B017 — any refusal type
        dev_status_mutation.approve_item("atk-one", items_path=stores.items)
    assert "changed since" in str(refused.value) or "hash" in str(refused.value), refused.value
    assert _items(stores)["atk-one"]["status"] == "in-review"


def _batch_home(home: Path, layout: str, batch: list[dict]) -> Path:
    """Place a batch in the resolved ticket-batches dir for ``layout``."""
    _flip(home, layout)
    directory = agent_toolkit_paths.path_for("ticket-batches")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "b.json"
    path.write_text(json.dumps(batch, separators=(",", ":")))
    return path


def _ticket(tid: str, path: str) -> dict:
    return {
        "id": tid,
        "summary": tid,
        "category": "chore",
        "context": "",
        "next_steps": "",
        "related_files": [{"path": path}],
        "blocked_by": [],
    }


def _seed_backlog(existing: list[str]) -> None:
    store = agent_toolkit_paths.path_for("work-items")
    store.mkdir(parents=True, exist_ok=True)
    items = [
        {"id": s, "summary": s, "status": "open", "category": "chore", "related_files": [], "blocked_by": []}
        for s in existing
    ]
    (store / "items.json").write_text(_items_payload(items))


def _stores_for_batch(batch: Path, root: Path) -> mpt.StoreFiles:
    return mpt.StoreFiles(root=root, batches_dir=batch.parent)


def test_migrated_partial_batch_resumes_in_toolkit_home(tmp_path, home):
    plan_path = _legacy(home, "grill") + "/p.md"
    tickets = [_ticket("atk-t1", plan_path), _ticket("atk-t2", plan_path)]
    # Build the legacy-era batch + state, then move the bytes into the
    # toolkit-home batch dir the way promotion will, and transform there.
    legacy_batch = _batch_home(home, "legacy", tickets)
    _state_for(legacy_batch, {"atk-t1": True})
    new_dir = Path(_new(home, "to-tickets"))
    new_dir.mkdir(parents=True)
    for name in ("b.json", "b.state.json"):
        shutil.copy(legacy_batch.parent / name, new_dir / name)
    migrated = new_dir / "b.json"
    plan = mpt.plan_transform(_stores_for_batch(migrated, home), mpt.RootMap.for_home(home))
    plan_dir, digest = mpt.save_plan(plan, tmp_path / "work")
    mpt.apply_saved(plan_dir, digest)
    _flip(home, "toolkit-home")
    _seed_backlog(["atk-t1"])
    created = to_tickets_runner.run_batch(migrated)
    assert created == ["atk-t2"] or "atk-t2" in created
    items = json.loads((agent_toolkit_paths.path_for("work-items") / "items.json").read_text())["items"]
    by_id = {i["id"]: i for i in items}
    assert by_id["atk-t1"]["summary"] == "atk-t1"  # untouched
    assert by_id["atk-t2"]["related_files"][0]["path"] == _new(home, "grill") + "/p.md"
    with pytest.raises(agent_toolkit_paths.StaleLayoutError):
        to_tickets_runner.run_batch(legacy_batch)


def test_rolled_back_batch_resumes_in_legacy(tmp_path, home):
    plan_path = _legacy(home, "grill") + "/p.md"
    batch = _batch_home(home, "legacy", [_ticket("atk-t1", plan_path), _ticket("atk-t2", plan_path)])
    _state_for(batch, {"atk-t1": True})
    plan = mpt.plan_transform(_stores_for_batch(batch, home), mpt.RootMap.for_home(home))
    plan_dir, digest = mpt.save_plan(plan, tmp_path / "work")
    mpt.apply_saved(plan_dir, digest)
    mpt.rollback_saved(plan_dir, digest)
    _seed_backlog(["atk-t1"])
    created = to_tickets_runner.run_batch(batch)
    assert "atk-t2" in created


# ── journal integration and crashes ─────────────────────────────────────────


def test_journal_integration(tmp_path, home):
    stores = _stage(tmp_path, home)
    journal = mth.Journal.open(tmp_path / "state", mth.new_migration_id())
    plan = mpt.plan_transform(stores, mpt.RootMap.for_home(home))
    work = journal.directory
    journal.begin("transform-plan", plan_dir=str(work / "transform"))
    plan_dir, digest = mpt.save_plan(plan, work)
    journal.done("transform-plan", manifest_sha256=digest)
    journal.begin("transform-apply")
    mpt.apply_saved(plan_dir, digest)
    journal.done("transform-apply", files=len(plan.changes))
    journal.close()
    events = [(r["phase"], r["event"]) for r in mth.read_records(work)]
    assert events == [
        ("transform-plan", "begin"),
        ("transform-plan", "done"),
        ("transform-apply", "begin"),
        ("transform-apply", "done"),
    ]


CRASH_SCRIPT = """
import json, sys
from pathlib import Path
sys.path.insert(0, {repo!r}); sys.path.insert(0, {repo!r} + "/agent-scripts")
import migrate_path_transform as mpt
mode, plan_dir, digest = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
(mpt.apply_saved if mode == "apply" else mpt.rollback_saved)(plan_dir, digest)
"""


def _crash_points(n: int, mode: str) -> tuple[str, ...]:
    return tuple(f"transform.{mode}.{i}" for i in range(n))


@pytest.mark.allow_real_subprocess  # the transform runs in a child killed by SIGKILL
@pytest.mark.parametrize("mode", ["apply", "rollback"])
def test_crash_at_every_replacement_recovers_both_ways(tmp_path, home, mode):
    script = tmp_path / "crash.py"
    script.write_text(CRASH_SCRIPT.format(repo=str(REPO)))
    probe = _stage(tmp_path / "probe", home)
    n = len(mpt.plan_transform(probe, mpt.RootMap.for_home(home)).changes)
    for index, point in enumerate(_crash_points(n, mode)):
        run = tmp_path / f"{mode}-{index}"
        stores = _stage(run, home)
        before = _tree_bytes(stores.root)
        plan = mpt.plan_transform(stores, mpt.RootMap.for_home(home))
        plan_dir, digest = mpt.save_plan(plan, run / "work")
        if mode == "rollback":
            mpt.apply_saved(plan_dir, digest)
        after_apply = None
        fi.run_killed_at(
            point,
            script,
            [mode, str(plan_dir), digest],
            home=home,
            declared=_crash_points(n, mode),
        )
        # Either direction finishes from disk alone, with paths and hashes in step.
        mpt.apply_saved(plan_dir, digest)
        after_apply = _tree_bytes(stores.root)
        one = _items(stores)["atk-one"]
        assert one["review_content_hash"] == dev_status_mutation._content_hash(one)
        mpt.rollback_saved(plan_dir, digest)
        assert _tree_bytes(stores.root) == before
        assert after_apply != before


@pytest.mark.allow_real_subprocess  # save_plan runs in a child killed by SIGKILL
def test_crash_inside_save_plan_is_discardable(tmp_path, home):
    script = tmp_path / "save.py"
    script.write_text(
        f"""
import sys
from pathlib import Path
sys.path.insert(0, {str(REPO)!r}); sys.path.insert(0, {str(REPO)!r} + "/agent-scripts")
import migrate_path_transform as mpt
stores = mpt.StoreFiles(root=Path(sys.argv[1]), items=Path(sys.argv[1]) / "backlog" / "items.json")
plan = mpt.plan_transform(stores, mpt.RootMap.for_home(Path(sys.argv[2])))
mpt.save_plan(plan, Path(sys.argv[3]))
"""
    )
    stores = _stage(tmp_path, home)
    before = _tree_bytes(stores.root)
    fi.run_killed_at(
        "transform.save.snapshots-written",
        script,
        [str(stores.root), str(home), str(tmp_path / "work")],
        home=home,
        declared=("transform.save.snapshots-written",),
    )
    plan_dir = tmp_path / "work" / "transform"
    assert plan_dir.exists() and not (plan_dir / "manifest.json").exists()
    mpt.discard_plan(plan_dir)
    assert not plan_dir.exists()
    assert _tree_bytes(stores.root) == before
