#!/usr/bin/env python3
"""Tests for the data-moving half of migrate_toolkit_home.py.

Stage, links, reverify, promote, flip, snapshot, validate; the restore after
a failed validation; recovery by the flip rule; the narrow rollback and
finalize commands; the step registry; and install.py's broad ``--rollback``
refusal and orphan-cleanup exemption.

In-process tests stub the fresh-process validation (``mth._run_validation``),
because the sandbox HOME has no installed runtime to validate. The crash
tests run ``_migrate_driver.py`` in a child killed at a named checkpoint.
"""

import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "agent-scripts"))

import agent_toolkit_paths  # noqa: E402
import depart  # noqa: E402
import depart_exec  # noqa: E402
import dev_status_mutation  # noqa: E402
import install  # noqa: E402
import link_inspect  # noqa: E402
import migrate_toolkit_home as mth  # noqa: E402
import migration_lock  # noqa: E402
import test_fault_injection as fi  # noqa: E402

DRIVER = Path(__file__).resolve().parent / "_migrate_driver.py"

pytestmark = pytest.mark.usefixtures("sandbox")


# ── fixtures ────────────────────────────────────────────────────────────────


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local" / "state"))
    monkeypatch.delenv(agent_toolkit_paths.ENV_HOME, raising=False)
    migration_lock._reset_for_tests()
    yield home
    migration_lock._reset_for_tests()


@pytest.fixture
def validation(monkeypatch):
    """The stubbed fresh-process validation; set ``.passes`` to steer it."""

    class Stub:
        passes = True
        calls = 0

        def __call__(self, _ctx):
            self.calls += 1
            return self.passes, [{"command": ["stub"], "exit": 0 if self.passes else 1}]

    stub = Stub()
    monkeypatch.setattr(mth, "_run_validation", stub)
    return stub


def _installed_runtime(home: Path, *, enforce: bool = True) -> None:
    """A fake installed runtime, linked into place the way install.py links it."""
    real = home / "fake-runtime"
    real.mkdir(exist_ok=True)
    (real / "migration_lock.py").write_text(f"ENFORCE: bool = {enforce}\n")
    (real / "agent_toolkit_paths.py").write_text("# installed\n")
    scripts = home / ".claude" / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    for module in ("migration_lock.py", "agent_toolkit_paths.py"):
        link = scripts / module
        if link.is_symlink():
            link.unlink()
        link.symlink_to(real / module)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _full_stores(home: Path, *, skip: tuple[str, ...] = ()) -> None:
    """Every domain populated, with stored paths the transform must rewrite."""
    data = home / ".claude" / "data"
    plan = data / "grill" / "topic-plan.md"
    if "work-items" not in skip:
        backlog = data / "backlog"
        backlog.mkdir(parents=True)
        item = {
            "id": "x-one",
            "summary": "s",
            "context": "c",
            "next_steps": "n",
            "related_files": [{"path": str(plan), "note": "plan"}],
        }
        item["review_content_hash"] = dev_status_mutation._content_hash(item)
        (backlog / "items.json").write_text(
            json.dumps({"schema_version": 2, "items": [item]})
        )
        (backlog / "pending_items.json").write_text(
            json.dumps({"schema_version": 1, "items": []})
        )
        (backlog / "_meta.json").write_text(json.dumps({"rev": 3}))
        (backlog / "journal.jsonl").write_text('{"cmd": "add"}\n')
        (backlog / "_machine_id").write_text("0ee2ec8d")
    if "out-of-scope" not in skip:
        oos = data / "backlog-out-of-scope"
        oos.mkdir(parents=True)
        (oos / "index.json").write_text("{}")
    if "decisions" not in skip:
        grill = data / "grill"
        grill.mkdir(parents=True)
        (grill / "topic.json").write_text(
            json.dumps({"schema_version": 1, "slug": "topic", "plan_path": str(plan)})
        )
        plan.write_text("# plan\n")
        (grill / "nested").mkdir()
        (grill / "nested" / "notes.md").write_text("nested\n")
    if "ticket-batches" not in skip:
        batches = data / "to-tickets"
        batches.mkdir(parents=True)
        batch = json.dumps([{"id": "t1", "related_files": [{"path": str(plan)}]}])
        (batches / "batch.json").write_text(batch)
        (batches / "batch.state.json").write_text(
            json.dumps({"batch_hash": _sha(batch.encode())})
        )
    if "standups" not in skip:
        standup = data / "standup"
        standup.mkdir(parents=True)
        (standup / "config.json").write_text(json.dumps({"git_repos": []}))
    if "guard-rail-log" not in skip:
        (data / "guard_rails_audit.jsonl").write_text('{"tool": "Bash"}\n')
    if "backend-log" not in skip:
        (data / "backend_calls.jsonl").write_text('{"backend": "codex"}\n')
    notes = data / "unrelated-notes"
    notes.mkdir(parents=True, exist_ok=True)
    (notes / "keep.txt").write_text("not toolkit data\n")


# A hand-formatted uninstall baseline (indent 4, one earlier layer), so byte-exact
# restores are distinguishable from a re-serialization.
PRIOR_BASELINE = (
    json.dumps(
        {
            "version": 1,
            "layers": [
                {
                    "captured_at": "2026-01-01T00:00:00+00:00",
                    "records": {"file:/nowhere/.bashrc": {"state": "absent"}},
                }
            ],
            "transactions": [],
            "installed_trees": {},
        },
        indent=4,
    )
    + "\n"
).encode()


def _baseline_file(home: Path) -> Path:
    return home / ".local" / "state" / "agent-toolkit" / "baseline.json"


@pytest.fixture
def machine(sandbox):
    _installed_runtime(sandbox)
    _full_stores(sandbox)
    baseline = _baseline_file(sandbox)
    baseline.parent.mkdir(parents=True, exist_ok=True)
    baseline.write_bytes(PRIOR_BASELINE)
    return sandbox


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


def _report(capsys) -> dict:
    out = capsys.readouterr().out
    return json.loads(out) if out.strip() else {}


def _migrate(capsys, repo: Path = REPO, **kw: object) -> tuple[int, dict]:
    code = mth.run(_opts(**kw), repo_root=repo)
    return code, _report(capsys)


def _rollback(capsys, mid: str, repo: Path = REPO) -> tuple[int, dict]:
    code = mth.rollback_command(mid, _opts(), repo_root=repo)
    return code, _report(capsys)


def _finalize(capsys, mid: str, repo: Path = REPO) -> tuple[int, dict]:
    code = mth.finalize_command(mid, _opts(), repo_root=repo)
    return code, _report(capsys)


def _tree(root: Path) -> dict[str, object]:
    snap: dict[str, object] = {}
    if not os.path.lexists(root):
        return snap
    if not root.is_dir() or root.is_symlink():
        return {".": ("file", root.read_bytes(), stat.S_IMODE(root.lstat().st_mode))}
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        rel = str(path.relative_to(root))
        if stat.S_ISLNK(info.st_mode):
            snap[rel] = ("link", os.readlink(path))
        elif stat.S_ISDIR(info.st_mode):
            snap[rel] = ("dir",)
        else:
            snap[rel] = ("file", path.read_bytes(), stat.S_IMODE(info.st_mode))
    return snap


def _symlinks(root: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    for current, dirs, files in os.walk(root):
        for name in dirs + files:
            path = Path(current) / name
            if path.is_symlink():
                found[str(path)] = os.readlink(path)
    return found


def _legacy(domain: str) -> Path:
    return agent_toolkit_paths.path_for_layout(domain, "legacy")


def _dest(domain: str) -> Path:
    return agent_toolkit_paths.path_for_layout(domain, "toolkit-home")


def _legacy_trees() -> dict[str, dict[str, object]]:
    return {d: _tree(_legacy(d)) for d in agent_toolkit_paths.DOMAINS}


def _state_dir(home: Path) -> Path:
    return home / ".local" / "state" / "agent-toolkit"


def _jdir(home: Path, mid: str) -> Path:
    return _state_dir(home) / "migrations" / mid


def _journal_dirs(home: Path) -> list[Path]:
    root = _state_dir(home) / "migrations"
    return sorted(root.iterdir()) if root.is_dir() else []


def _work_dir(home: Path, mid: str) -> Path:
    return home / ".agent-toolkit" / f".migration-{mid}"


def _snapshot_dir(home: Path, mid: str) -> Path:
    return home / ".claude" / "data" / f".toolkit-home-snapshot-{mid}"


def _layout() -> str:
    agent_toolkit_paths.DEFAULT_RESOLVER.invalidate()
    return agent_toolkit_paths.current_layout()


def _history(home: Path) -> list[dict]:
    path = _state_dir(home) / "history.jsonl"
    return [e for e in link_inspect.read_manifest_entries(path)]


def _phases(records: list[dict]) -> list[tuple[str, str]]:
    return [(r["phase"], r["event"]) for r in records]


# ── full run ────────────────────────────────────────────────────────────────


def _expected_phase_order() -> list[str]:
    domains = agent_toolkit_paths.DOMAINS
    return [
        "preflight",
        "inventory",
        *(f"stage:{d}" for d in domains),
        "carry:unrelated-notes",
        "baseline",
        "links",
        "settings-rewrite",
        "reverify",
        *(f"promote:{d}" for d in domains),
        "flip",
        *(f"snapshot:{d}" for d in domains),
        "carry-snapshot:unrelated-notes",
        "validate",
    ]


def test_full_run_moves_every_domain_flips_and_snapshots(machine, capsys, validation):
    before = _legacy_trees()
    unrelated = _tree(machine / ".claude" / "data" / "unrelated-notes")
    code, report = _migrate(capsys)
    assert code == 0, report
    mid = report["migration_id"]
    assert report["outcome"].startswith("committed"), report["outcome"]
    assert _layout() == "toolkit-home"

    for domain in agent_toolkit_paths.DOMAINS:
        assert not os.path.lexists(_legacy(domain)), domain
        assert os.path.lexists(_dest(domain)), domain
        snap = _snapshot_dir(machine, mid) / _legacy(domain).name
        assert _tree(snap) == before[domain], domain

    new_plan = str(_dest("decisions") / "topic-plan.md")
    items = json.loads((_dest("work-items") / "items.json").read_text())["items"]
    assert items[0]["related_files"][0]["path"] == new_plan
    assert items[0]["review_content_hash"] == dev_status_mutation._content_hash(items[0])
    session = json.loads((_dest("decisions") / "topic.json").read_text())
    assert session["plan_path"] == new_plan
    batch = (_dest("ticket-batches") / "batch.json").read_bytes()
    assert json.loads(batch)[0]["related_files"][0]["path"] == new_plan
    state = json.loads((_dest("ticket-batches") / "batch.state.json").read_text())
    assert state["batch_hash"] == _sha(batch)
    assert (_dest("decisions") / "nested" / "notes.md").read_text() == "nested\n"

    assert _tree(machine / ".agent-toolkit" / "data" / "unrelated-notes") == unrelated
    assert not (_work_dir(machine, mid) / "staging").exists() or not any(
        (_work_dir(machine, mid) / "staging").iterdir()
    )

    records = mth.read_records(_jdir(machine, mid))
    begun = [r["phase"] for r in records if r["event"] == "begin"]
    assert begun == _expected_phase_order()
    assert all(
        any(r["phase"] == p and r["event"] == "done" for r in records) for p in begun
    )
    assert records[-1]["event"] == "end"
    assert records[-1]["detail"]["outcome"] == "committed"
    migrations = [e for e in _history(machine) if e.get("kind") == "migration"]
    assert [(e["id"], e["outcome"]) for e in migrations] == [(mid, "committed")]
    assert validation.calls == 1


def test_resolver_serves_legacy_until_flip_and_toolkit_home_after(
    machine, capsys, validation, monkeypatch
):
    present = [d for d in agent_toolkit_paths.DOMAINS if os.path.lexists(_legacy(d))]
    seen: list[tuple[str, str]] = []
    real = mth._checkpoint

    def spy(name: str) -> None:
        real(name)
        layout = _layout()
        seen.append((name, layout))
        for domain in present:
            path = agent_toolkit_paths.path_for(domain)
            assert os.path.lexists(path), (name, layout, path)

    monkeypatch.setattr(mth, "_checkpoint", spy)
    code, report = _migrate(capsys)
    assert code == 0, report
    names = [n for n, _ in seen]
    flip = names.index("migrate.flip.done")
    assert {layout for _, layout in seen[:flip]} == {"legacy"}
    assert {layout for _, layout in seen[flip:]} == {"toolkit-home"}


def test_absent_domain_is_recorded_and_skipped(sandbox, capsys, validation):
    _installed_runtime(sandbox)
    _full_stores(sandbox, skip=("standups", "backend-log"))
    code, report = _migrate(capsys)
    assert code == 0, report
    assert not os.path.lexists(_dest("standups"))
    assert not os.path.lexists(_dest("backend-log"))
    records = mth.read_records(_jdir(sandbox, report["migration_id"]))
    done = {r["phase"]: r["detail"] for r in records if r["event"] == "done"}
    assert done["stage:standups"].get("absent") is True
    assert done["promote:standups"].get("absent") is True
    assert done["snapshot:standups"].get("absent") is True


def test_fresh_machine_commits_with_nothing_to_move(sandbox, capsys, validation):
    _installed_runtime(sandbox)
    code, report = _migrate(capsys)
    assert code == 0, report
    assert _layout() == "toolkit-home"


# ── pre-flip failures roll back ─────────────────────────────────────────────


def _at(monkeypatch, checkpoint: str, action) -> None:
    real = mth._checkpoint

    def spy(name: str) -> None:
        real(name)
        if name == checkpoint:
            action()

    monkeypatch.setattr(mth, "_checkpoint", spy)


def _assert_rolled_back(home: Path, before: dict, mid: str) -> None:
    assert _layout() == "legacy"
    assert _legacy_trees() == before
    for domain in agent_toolkit_paths.DOMAINS:
        assert not os.path.lexists(_dest(domain)), domain
    staging = _work_dir(home, mid) / "staging"
    assert not staging.exists() or not any(staging.iterdir())
    records = mth.read_records(_jdir(home, mid))
    assert records[-1]["event"] == "end"
    assert records[-1]["detail"]["outcome"] == "aborted"


def test_reverify_names_a_staged_file_edited_before_promotion(
    machine, capsys, validation, monkeypatch
):
    before = _legacy_trees()
    mid = "mig-20260923T120000Z-aaaaaa"
    edited = _work_dir(machine, mid) / "staging" / "decisions" / "topic-plan.md"
    _at(monkeypatch, "migrate.links.done", lambda: edited.write_text("tampered\n"))
    code, report = _migrate(capsys, migration_id=mid)
    assert code == 1, report
    assert str(edited) in report["outcome"]
    _assert_rolled_back(machine, before, mid)
    assert validation.calls == 0


def test_reverify_names_a_legacy_source_edited_after_staging(
    machine, capsys, validation, monkeypatch
):
    mid = "mig-20260923T120000Z-bbbbbb"
    source = _legacy("decisions") / "topic-plan.md"
    _at(monkeypatch, "migrate.links.done", lambda: source.write_text("late edit\n"))
    code, report = _migrate(capsys, migration_id=mid)
    assert code == 1, report
    assert str(source) in report["outcome"]
    assert source.read_text() == "late edit\n"
    assert _layout() == "legacy"


def test_promotion_refuses_a_destination_it_did_not_create(
    machine, capsys, validation, monkeypatch
):
    before = _legacy_trees()
    mid = "mig-20260923T120000Z-cccccc"
    foreign = _dest("decisions") / "stray.md"

    def occupy() -> None:
        foreign.parent.mkdir(parents=True)
        foreign.write_text("someone else\n")

    _at(monkeypatch, "migrate.reverify.done", occupy)
    code, report = _migrate(capsys, migration_id=mid)
    assert code == 1, report
    assert str(_dest("decisions")) in report["outcome"]
    assert foreign.read_text() == "someone else\n"
    assert _layout() == "legacy"
    assert _legacy_trees() == before
    assert not os.path.lexists(_dest("work-items"))


def test_cross_filesystem_run_commits(machine, capsys, validation, monkeypatch):
    real = mth._device_of

    def fake(path: Path) -> int:
        dev = real(path)
        return dev + 1 if ".agent-toolkit" in str(path) or path == machine else dev

    monkeypatch.setattr(mth, "_device_of", fake)
    code, report = _migrate(capsys)
    assert code == 1  # refused without the flag
    code, report = _migrate(capsys, cross_filesystem=True)
    assert code == 0, report
    assert _layout() == "toolkit-home"


def test_a_copy_that_differs_from_its_source_aborts(
    machine, capsys, validation, monkeypatch
):
    before = _legacy_trees()
    mid = "mig-20260923T120000Z-dddddd"
    import shutil

    real_copy = shutil.copy2

    def corrupting(src, dst, *, follow_symlinks=True):
        result = real_copy(src, dst, follow_symlinks=follow_symlinks)
        if Path(src).name == "_meta.json":
            Path(dst).write_text('{"rev": 999}')
        return result

    monkeypatch.setattr(shutil, "copy2", corrupting)
    code, report = _migrate(capsys, migration_id=mid, cross_filesystem=True)
    assert code == 1, report
    assert "_meta.json" in report["outcome"]
    _assert_rolled_back(machine, before, mid)


# ── post-flip validation failure restores ──────────────────────────────────


def test_failed_validation_restores_a_byte_identical_legacy_layout(
    machine, capsys, validation
):
    before = _legacy_trees()
    links_before = _symlinks(machine)
    validation.passes = False
    code, report = _migrate(capsys)
    assert code == 1, report
    mid = report["migration_id"]
    assert report["outcome"].startswith("restored"), report["outcome"]
    assert _layout() == "legacy"
    assert _legacy_trees() == before
    for domain in agent_toolkit_paths.DOMAINS:
        assert not os.path.lexists(_dest(domain)), domain
        kept = _work_dir(machine, mid) / "restored" / domain
        assert os.path.lexists(kept), domain
    items = json.loads(
        (_work_dir(machine, mid) / "restored" / "work-items" / "items.json").read_text()
    )
    assert items["items"][0]["id"] == "x-one"
    assert _symlinks(machine) == links_before
    records = mth.read_records(_jdir(machine, mid))
    assert records[-1]["detail"]["outcome"] == "restored"
    assert not any(
        e.get("kind") == "symlink-created" and e.get("migration") == mid
        for e in _history(machine)
    )
    assert not _snapshot_dir(machine, mid).exists()


# ── narrow rollback ─────────────────────────────────────────────────────────


def _commit(capsys) -> str:
    code, report = _migrate(capsys)
    assert code == 0, report
    return report["migration_id"]


def test_narrow_rollback_returns_to_legacy_and_is_idempotent(
    machine, capsys, validation
):
    before = _legacy_trees()
    links_before = _symlinks(machine)
    mid = _commit(capsys)
    code, report = _rollback(capsys, mid)
    assert code == 0, report
    assert _layout() == "legacy"
    assert _legacy_trees() == before
    assert _symlinks(machine) == links_before
    for domain in agent_toolkit_paths.DOMAINS:
        assert not os.path.lexists(_dest(domain))
        assert os.path.lexists(_work_dir(machine, mid) / "restored" / domain)
    records = mth.read_records(_jdir(machine, mid), mth.ROLLBACK_NAME)
    assert records[-1]["detail"]["outcome"] == "rolled-back"
    assert mth.read_records(_jdir(machine, mid))[-1]["detail"]["outcome"] == "committed"

    snapshot = (_tree(machine), _tree(_state_dir(machine)))
    code, _ = _rollback(capsys, mid)
    assert code == 0
    assert (_tree(machine), _tree(_state_dir(machine))) == snapshot
    code, report = _finalize(capsys, mid)
    assert code == 0, report
    assert not _work_dir(machine, mid).exists()
    assert _legacy_trees() == before
    assert _symlinks(machine) == links_before
    code, _ = _finalize(capsys, mid)
    assert code == 0


def test_narrow_rollback_refuses_after_a_write_and_mutates_nothing(
    machine, capsys, validation
):
    mid = _commit(capsys)
    written = _dest("work-items") / "_meta.json"
    written.write_text('{"rev": 4}')
    snapshot = (_tree(machine), _tree(_state_dir(machine)))
    code, report = _rollback(capsys, mid)
    assert code == 1, report
    assert str(written) in report["outcome"]
    assert (_tree(machine), _tree(_state_dir(machine))) == snapshot
    assert _layout() == "toolkit-home"


def test_narrow_rollback_refuses_a_domain_created_after_commit(
    sandbox, capsys, validation
):
    _installed_runtime(sandbox)
    _full_stores(sandbox, skip=("standups",))
    mid = _commit(capsys)
    _dest("standups").mkdir()
    (_dest("standups") / "config.json").write_text("{}")
    code, report = _rollback(capsys, mid)
    assert code == 1, report
    assert str(_dest("standups")) in report["outcome"]
    assert _layout() == "toolkit-home"


def test_appended_telemetry_does_not_block_rollback_and_is_kept(
    machine, capsys, validation
):
    mid = _commit(capsys)
    with _dest("guard-rail-log").open("a") as handle:
        handle.write('{"tool": "Edit"}\n')
    code, report = _rollback(capsys, mid)
    assert code == 0, report
    kept = _work_dir(machine, mid) / "restored" / "guard-rail-log"
    assert kept.read_text().endswith('{"tool": "Edit"}\n')


def test_rollback_requires_a_committed_migration(machine, capsys, validation):
    code, report = _rollback(capsys, "mig-20260923T120000Z-eeeeee")
    assert code == 1
    assert "not a committed migration" in report["outcome"]


# ── finalize ────────────────────────────────────────────────────────────────


@pytest.mark.regression("finalize-restored-copy", "is not a committed migration")
def test_finalize_after_failed_validation_removes_restored_copy(
    machine, capsys, validation
):
    before = _legacy_trees()
    validation.passes = False
    code, report = _migrate(capsys)
    assert code == 1, report
    mid = report["migration_id"]
    code, report = _finalize(capsys, mid)
    assert code == 0, report
    assert not _work_dir(machine, mid).exists()
    assert _legacy_trees() == before
    assert _layout() == "legacy"


@pytest.mark.regression("finalize-restored-mismatch", "rolled back")
def test_finalize_refuses_changed_restored_data(machine, capsys, validation):
    mid = _commit(capsys)
    _rollback(capsys, mid)
    changed = _work_dir(machine, mid) / "restored" / "work-items" / "items.json"
    changed.write_text("changed\n")
    code, report = _finalize(capsys, mid)
    assert code == 1, report
    assert str(changed) in report["outcome"]
    assert not (_jdir(machine, mid) / mth.FINALIZE_NAME).exists()


@pytest.mark.parametrize("change", ["added", "removed"])
def test_finalize_refuses_restored_tree_drift(machine, capsys, validation, change):
    mid = _commit(capsys)
    _rollback(capsys, mid)
    path = _work_dir(machine, mid) / "restored" / "work-items" / "extra.txt"
    if change == "added":
        path.write_text("new\n")
    else:
        path = path.with_name("items.json")
        path.unlink()
    code, report = _finalize(capsys, mid)
    assert code == 1, report
    assert str(path) in report["outcome"]
    assert not (_jdir(machine, mid) / mth.FINALIZE_NAME).exists()


def test_finalize_reports_appended_telemetry_lines(machine, capsys, validation):
    mid = _commit(capsys)
    with _dest("guard-rail-log").open("a") as handle:
        handle.write('{"tool": "Edit"}\n')
    _rollback(capsys, mid)
    code, report = _finalize(capsys, mid)
    assert code == 0, report
    assert report["telemetry_lines_discarded"]["guard-rail-log"] == 1
    assert report["telemetry_lines_discarded"]["backend-log"] == 0


def test_finalize_counts_telemetry_created_after_promotion(
    sandbox, capsys, validation
):
    _installed_runtime(sandbox)
    _full_stores(sandbox, skip=("backend-log",))
    mid = _commit(capsys)
    _dest("backend-log").write_text('{"backend": "new"}\n')
    _rollback(capsys, mid)
    code, report = _finalize(capsys, mid)
    assert code == 0, report
    assert report["telemetry_lines_discarded"]["backend-log"] == 1


def test_finalize_reports_unknown_for_old_telemetry_journal(
    machine, capsys, validation
):
    mid = _commit(capsys)
    _rollback(capsys, mid)
    journal = _jdir(machine, mid) / mth.JOURNAL_NAME
    rows = [json.loads(line) for line in journal.read_text().splitlines()]
    for row in rows:
        if row["phase"] == "promote:backend-log" and row["event"] == "done":
            row["detail"].pop("telemetry_lines")
    journal.write_text("".join(json.dumps(row) + "\n" for row in rows))
    code, report = _finalize(capsys, mid)
    assert code == 0, report
    assert report["telemetry_lines_discarded"]["backend-log"] is None


def test_finalize_refuses_malformed_telemetry(machine, capsys, validation):
    mid = _commit(capsys)
    _rollback(capsys, mid)
    path = _work_dir(machine, mid) / "restored" / "backend-log"
    with path.open("a") as handle:
        handle.write("torn")
    code, report = _finalize(capsys, mid)
    assert code == 1, report
    assert str(path) in report["outcome"]
    assert path.exists()


def test_empty_finalize_journal_replans_after_rollback(machine, capsys, validation):
    mid = _commit(capsys)
    _rollback(capsys, mid)
    (_jdir(machine, mid) / mth.FINALIZE_NAME).touch()
    code, report = _finalize(capsys, mid)
    assert code == 0, report
    assert not _work_dir(machine, mid).exists()
    assert mth.is_terminal(_jdir(machine, mid), mth.FINALIZE_NAME)


def test_resume_refuses_write_after_finalize_begin(machine, capsys, validation):
    mid = _commit(capsys)
    _rollback(capsys, mid)
    directory = _jdir(machine, mid)
    history = _state_dir(machine) / "history.jsonl"
    state = mth._load_state(directory, None, mth.read_records(directory), REPO, history)
    plan = mth._restored_finalize_plan(state)
    journal = mth.Journal.create(directory, mth.FINALIZE_NAME)
    journal.begin("finalize", **plan)
    journal.close()
    changed = _work_dir(machine, mid) / "restored" / "work-items" / "items.json"
    changed.write_text("post-crash write\n")
    code, report = _finalize(capsys, mid)
    assert code == 1, report
    assert str(changed) in report["outcome"]
    assert changed.read_text() == "post-crash write\n"


def test_finalize_refuses_symlinked_work_directory(machine, capsys, validation):
    mid = _commit(capsys)
    _rollback(capsys, mid)
    work = _work_dir(machine, mid)
    parked = work.with_name(work.name + ".parked")
    work.rename(parked)
    work.symlink_to(parked, target_is_directory=True)
    code, report = _finalize(capsys, mid)
    assert code == 1, report
    assert "unsafe migration work directory" in report["outcome"]
    assert (parked / "restored").exists()


def test_finalize_refuses_unjournalled_transform_leftover(
    machine, capsys, validation
):
    mid = _commit(capsys)
    _rollback(capsys, mid)
    extra = _work_dir(machine, mid) / "transform" / "work-items" / "new.txt"
    extra.parent.mkdir(parents=True, exist_ok=True)
    extra.write_text("new\n")
    code, report = _finalize(capsys, mid)
    assert code == 1, report
    assert str(extra.parent) in report["outcome"]
    assert extra.exists()


def test_finalize_deletes_only_journal_proven_paths(machine, capsys, validation):
    mid = _commit(capsys)
    unrelated = _tree(machine / ".agent-toolkit" / "data" / "unrelated-notes")
    data_after = {d: _tree(_dest(d)) for d in agent_toolkit_paths.DOMAINS}
    code, report = _finalize(capsys, mid)
    assert code == 0, report
    assert not _snapshot_dir(machine, mid).exists()
    assert not _work_dir(machine, mid).exists()
    assert _tree(machine / ".agent-toolkit" / "data" / "unrelated-notes") == unrelated
    assert {d: _tree(_dest(d)) for d in agent_toolkit_paths.DOMAINS} == data_after
    assert _layout() == "toolkit-home"
    records = mth.read_records(_jdir(machine, mid), mth.FINALIZE_NAME)
    assert records[-1]["detail"]["outcome"] == "finalized"

    snapshot = (_tree(machine), _tree(_state_dir(machine)))
    code, _ = _finalize(capsys, mid)
    assert code == 0
    assert (_tree(machine), _tree(_state_dir(machine))) == snapshot
    code, report = _rollback(capsys, mid)
    assert code == 1
    assert "finalized" in report["outcome"]


def test_finalize_refuses_after_a_manual_edit_inside_the_snapshot(
    machine, capsys, validation
):
    mid = _commit(capsys)
    edited = _snapshot_dir(machine, mid) / "grill" / "topic-plan.md"
    edited.write_text("edited\n")
    snapshot = (_tree(machine), _tree(_state_dir(machine)))
    code, report = _finalize(capsys, mid)
    assert code == 1, report
    assert str(edited) in report["outcome"]
    assert (_tree(machine), _tree(_state_dir(machine))) == snapshot


# ── install.py: broad rollback, orphan cleanup ──────────────────────────────


def test_broad_rollback_refuses_while_a_migration_is_unfinalized(
    machine, capsys, validation
):
    mid = _commit(capsys)
    code = install.main(["--rollback"])
    err = capsys.readouterr().err
    assert code == 2
    assert f"--rollback-toolkit-home-migration={mid}" in err
    _finalize(capsys, mid)
    assert mth.committed_unfinalized(_state_dir(machine)) == []


def test_broad_rollback_is_allowed_after_a_narrow_rollback(machine, capsys, validation):
    mid = _commit(capsys)
    _rollback(capsys, mid)
    assert mth.committed_unfinalized(_state_dir(machine)) == []


def _fixture_repo(tmp_path: Path) -> Path:
    """A release-1-style checkout: one runtime link moved to a new destination."""
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts" / "tool.py").write_text("# tool\n")
    (repo / "links.toml").write_text(
        '[[link]]\nsrc = "scripts/tool.py"\ndest = "~/.agent-toolkit/scripts/tool.py"\n'
    )
    return repo


def test_retained_legacy_links_survive_orphan_cleanup_until_finalize(
    machine, capsys, validation, tmp_path
):
    repo = _fixture_repo(tmp_path)
    old = machine / ".claude" / "scripts" / "tool.py"
    old.symlink_to(repo / "scripts" / "tool.py")
    history = _state_dir(machine) / "history.jsonl"
    history.parent.mkdir(parents=True, exist_ok=True)
    history.write_text(
        json.dumps({"kind": "symlink-created", "dest": str(old), "src": str(repo / "scripts" / "tool.py")})
        + "\n"
    )
    code, report = _migrate(capsys, repo=repo)
    assert code == 0, report
    mid = report["migration_id"]
    new = machine / ".agent-toolkit" / "scripts" / "tool.py"
    assert os.readlink(new) == str(repo / "scripts" / "tool.py")
    assert old.is_symlink()

    ctx = install.build_context(install.parse_args(["--harness=claude"]), repo_root=repo)
    links = install.gather_links(ctx, link_inspect.load_links(repo / "links.toml"))
    assert old not in install._find_orphaned_links(ctx, links)
    assert old in mth.retained_legacy_links(_state_dir(machine))

    code, report = _finalize(capsys, mid, repo=repo)
    assert code == 0, report
    assert not os.path.lexists(old)
    assert new.is_symlink()
    assert not any(e.get("dest") == str(old) for e in _history(machine))
    assert old not in mth.retained_legacy_links(_state_dir(machine))


def _toolkit_repo(tmp_path: Path) -> Path:
    """An agent-toolkit-shaped checkout whose shared runtime moved to the toolkit home.

    It carries the three ownership markers (links.toml, install.py,
    agent-scripts/). ``gated.py`` only applies to pi.
    """
    repo = tmp_path / "repo"
    scripts = repo / "agent-scripts"
    scripts.mkdir(parents=True)
    (repo / "install.py").write_text("# installer\n")
    (repo / "claude" / "icons").mkdir(parents=True)
    rows = []
    for name in ("tool", "stale", "dang", "rel", "gated", "foreign", "other"):
        (scripts / f"{name}.py").write_text(f"# {name}\n")
        harness = '\nharness = "pi"' if name == "gated" else ""
        rows.append(
            f'[[link]]\nsrc = "agent-scripts/{name}.py"\n'
            f'dest = "~/.agent-toolkit/scripts/{name}.py"{harness}\n'
        )
    rows.append('[[link]]\nsrc = "claude/icons"\ndest = "~/.agent-toolkit/icons"\n')
    (repo / "links.toml").write_text("\n".join(rows))
    return repo


def _legacy_links(home: Path, repo: Path, tmp_path: Path) -> dict[str, Path]:
    """Legacy ~/.claude links in every shape the desktop showed."""
    legacy = home / ".claude"
    scripts = legacy / "scripts"
    origin = tmp_path / "origin"
    (origin / "claude" / "scripts").mkdir(parents=True)
    (origin / "links.toml").write_text("")
    (origin / "install.py").write_text("")
    (origin / "claude" / "scripts" / "foreign.py").write_text("# origin\n")
    links = {
        "tool": (scripts / "tool.py", repo / "agent-scripts" / "tool.py"),
        "stale": (scripts / "stale.py", repo / "agent-scripts" / "stale.py"),
        "dang": (scripts / "dang.py", repo / "claude" / "scripts" / "dang.py"),
        "rel": (
            scripts / "rel.py",
            Path(os.path.relpath(repo / "agent-scripts" / "rel.py", scripts)),
        ),
        "icons": (legacy / "icons", repo / "claude" / "icons"),
        "gated": (scripts / "gated.py", repo / "agent-scripts" / "gated.py"),
        "foreign": (scripts / "foreign.py", origin / "claude" / "scripts" / "foreign.py"),
    }
    for dest, target in links.values():
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.symlink_to(target)
    (scripts / "other.py").write_text("a real file\n")
    history = _state_dir(home) / "history.jsonl"
    history.parent.mkdir(parents=True, exist_ok=True)
    with history.open("a") as fh:
        fh.write(
            json.dumps(
                {
                    "kind": "symlink-created",
                    "dest": str(scripts / "stale.py"),
                    "src": str(repo / "claude" / "scripts" / "stale.py"),
                }
            )
            + "\n"
        )
    return {name: dest for name, (dest, _target) in links.items()}


def test_links_retain_toolkit_legacy_links_by_the_mapping(
    machine, capsys, validation, tmp_path
):
    repo = _toolkit_repo(tmp_path)
    legacy = _legacy_links(machine, repo, tmp_path)
    code, report = _migrate(capsys, repo=repo)
    assert code == 0, report
    retained = mth.retained_legacy_links(_state_dir(machine))
    toolkit = {legacy[n] for n in ("tool", "stale", "dang", "rel", "icons")}
    assert retained == toolkit

    code, report = _finalize(capsys, report["migration_id"], repo=repo)
    assert code == 0, report
    for dest in toolkit:
        assert not os.path.lexists(dest), dest
    assert legacy["gated"].is_symlink()
    assert legacy["foreign"].is_symlink()
    assert (machine / ".claude" / "scripts" / "other.py").read_text() == "a real file\n"
    dests = {e.get("dest") for e in _history(machine)}
    assert not dests & {str(d) for d in toolkit}


def test_finalize_leaves_a_repointed_retained_link_and_its_other_history(
    machine, capsys, validation, tmp_path
):
    repo = _toolkit_repo(tmp_path)
    legacy = _legacy_links(machine, repo, tmp_path)
    code, report = _migrate(capsys, repo=repo)
    assert code == 0, report
    tool = legacy["tool"]
    journaled = str(repo / "agent-scripts" / "tool.py")
    elsewhere = tmp_path / "elsewhere.py"
    tool.unlink()
    tool.symlink_to(elsewhere)
    real = legacy["rel"]
    real.unlink()
    real.write_text("replaced by hand\n")
    history = _state_dir(machine) / "history.jsonl"
    with history.open("a") as fh:
        for src in (journaled, str(elsewhere)):
            fh.write(json.dumps({"kind": "symlink-created", "dest": str(tool), "src": src}) + "\n")

    code, report = _finalize(capsys, report["migration_id"], repo=repo)
    assert code == 0, report
    assert os.readlink(tool) == str(elsewhere)
    assert real.read_text() == "replaced by hand\n"
    kept = [(e["dest"], e["src"]) for e in _history(machine) if e.get("dest") == str(tool)]
    assert kept == [(str(tool), str(elsewhere))]


def _check_ctx(repo: Path) -> install.Context:
    return install.build_context(
        install.parse_args(["--check-links", "--harness=claude"]), repo_root=repo
    )


def _recorded_legacy_link(home: Path, repo: Path) -> Path:
    """A manifest-recorded legacy link the fixture repo no longer produces."""
    old = home / ".claude" / "scripts" / "tool.py"
    old.symlink_to(repo / "scripts" / "tool.py")
    history = _state_dir(home) / "history.jsonl"
    history.parent.mkdir(parents=True, exist_ok=True)
    history.write_text(
        json.dumps(
            {"kind": "symlink-created", "dest": str(old), "src": str(repo / "scripts" / "tool.py")}
        )
        + "\n"
    )
    return old


def test_migration_validation_passes_check_links_with_retained_links(
    machine, capsys, monkeypatch, tmp_path
):
    """The real check-links, run where validation runs, sees the links journal."""
    repo = _fixture_repo(tmp_path)
    _recorded_legacy_link(machine, repo)
    outputs: list[str] = []

    def in_process_check(_ctx: object) -> tuple[bool, list[dict[str, object]]]:
        code = install.do_check_links(_check_ctx(repo))
        outputs.append(capsys.readouterr().out)
        return code == 0, [{"command": ["check-links"], "exit": code}]

    monkeypatch.setattr(mth, "_run_validation", in_process_check)
    code, report = _migrate(capsys, repo=repo)
    assert code == 0, (report, outputs)
    assert "1 legacy link(s) kept for a toolkit-home migration" in outputs[0]


def test_check_links_notes_nothing_for_a_retained_link_that_is_no_orphan(
    machine, capsys, validation, tmp_path
):
    """Mapping-retained links the manifest never recorded are no finding at all."""
    repo = _toolkit_repo(tmp_path)
    _legacy_links(machine, repo, tmp_path)
    code, report = _migrate(capsys, repo=repo)
    assert code == 0, report
    assert mth.retained_legacy_links(_state_dir(machine))
    install.do_check_links(_check_ctx(repo))
    out = capsys.readouterr().out
    assert "orphaned" not in out, out
    assert "legacy link(s) kept" not in out, out


def test_check_links_fails_loud_when_a_migration_journal_is_unreadable(
    machine, capsys, validation, tmp_path
):
    repo = _fixture_repo(tmp_path)
    old = _recorded_legacy_link(machine, repo)
    code, report = _migrate(capsys, repo=repo)
    assert code == 0, report
    journal = _jdir(machine, report["migration_id"]) / mth.JOURNAL_NAME
    journal.chmod(0)
    try:
        ctx = _check_ctx(repo)
        assert install.do_check_links(ctx) == 1
        out = capsys.readouterr()
        assert "journal" in (out.out + out.err) and "unreadable" in (out.out + out.err)
        links = install.gather_links(ctx, link_inspect.load_links(repo / "links.toml"))
        assert install._find_orphaned_links(ctx, links) == []
        assert old.is_symlink()
    finally:
        journal.chmod(0o600)


def test_legacy_layout_migrations_keep_their_links_protected(
    machine, capsys, validation, tmp_path
):
    """While the legacy layout is live again its links stay exempt.

    A restored or rolled-back migration returns the machine to the legacy
    layout, whose settings still invoke the legacy links, so neither cleanup
    nor the audit may treat them as orphans. Only a normal finalize ends that.
    """
    repo = _fixture_repo(tmp_path)
    old = _recorded_legacy_link(machine, repo)
    validation.passes = False
    code, report = _migrate(capsys, repo=repo)
    assert code != 0, report
    assert old in mth.retained_legacy_links(_state_dir(machine))

    validation.passes = True
    code, report = _migrate(capsys, repo=repo)
    assert code == 0, report
    code, _ = _rollback(capsys, report["migration_id"], repo=repo)
    assert code == 0
    assert old in mth.retained_legacy_links(_state_dir(machine))
    ctx = install.build_context(install.parse_args(["--harness=claude"]), repo_root=repo)
    links = install.gather_links(ctx, link_inspect.load_links(repo / "links.toml"))
    assert old not in install._find_orphaned_links(ctx, links)


def test_restored_finalize_keeps_legacy_links_protected(
    machine, capsys, validation, tmp_path
):
    repo = _fixture_repo(tmp_path)
    old = _recorded_legacy_link(machine, repo)
    code, report = _migrate(capsys, repo=repo)
    assert code == 0, report
    mid = report["migration_id"]
    code, report = _rollback(capsys, mid, repo=repo)
    assert code == 0, report
    code, report = _finalize(capsys, mid, repo=repo)
    assert code == 0, report
    assert old.is_symlink()
    ctx = install.build_context(install.parse_args(["--harness=claude"]), repo_root=repo)
    links = install.gather_links(ctx, link_inspect.load_links(repo / "links.toml"))
    assert old not in install._find_orphaned_links(ctx, links)


# ── step registry ───────────────────────────────────────────────────────────


@pytest.fixture
def fake_steps():
    calls: list[str] = []

    def make(name: str) -> mth.Step:
        return mth.Step(
            apply=lambda state, rec: calls.append(f"apply:{name}") or {"n": name},
            undo=lambda state, rec: calls.append(f"undo:{name}"),
            verify=lambda state, rec: True,
        )

    mth.register_step("test-fake-a", make("a"), in_run=True)
    mth.register_step("test-fake-b", make("b"), in_run=True)
    yield calls
    for phase in ("test-fake-a", "test-fake-b"):
        mth.STEPS.pop(phase, None)
        if phase in mth.RUNTIME_PHASES:
            mth.RUNTIME_PHASES.remove(phase)


def test_registered_steps_apply_in_order_and_undo_newest_first(
    machine, capsys, validation, fake_steps
):
    validation.passes = False
    code, report = _migrate(capsys)
    assert code == 1, report
    assert fake_steps == ["apply:a", "apply:b", "undo:b", "undo:a"]
    records = mth.read_records(_jdir(machine, report["migration_id"]))
    begun = [r["phase"] for r in records if r["event"] == "begin"]
    assert begun.index("links") < begun.index("test-fake-a") < begun.index("reverify")


def test_registering_a_phase_twice_is_refused(fake_steps):
    with pytest.raises(ValueError):
        mth.register_step("test-fake-a", mth.STEPS["test-fake-a"])


def test_unknown_phase_in_a_journal_makes_rollback_refuse(machine, capsys, validation):
    mid = _commit(capsys)
    journal = _jdir(machine, mid) / mth.JOURNAL_NAME
    lines = journal.read_text().splitlines()
    mystery = json.dumps({"seq": 0, "id": mid, "phase": "mystery", "event": "begin", "detail": {}})
    journal.write_text("\n".join([*lines[:-1], mystery, lines[-1]]) + "\n")
    snapshot = (_tree(machine), _tree(_state_dir(machine)))
    code, report = _rollback(capsys, mid)
    assert code == 1, report
    assert "mystery" in report["outcome"]
    assert (_tree(machine), _tree(_state_dir(machine))) == snapshot


# ── install.py flags ────────────────────────────────────────────────────────


def _parse_error(capsys, argv: list[str]) -> str:
    with pytest.raises(SystemExit) as exc:
        install.parse_args(argv)
    assert exc.value.code == 2
    return capsys.readouterr().err


def test_post_commit_flags_parse():
    mid = "mig-20260923T120000Z-abcdef"
    opts = install.parse_args([f"--rollback-toolkit-home-migration={mid}", "--json"])
    assert opts.rollback_migration == mid and opts.json_report
    opts = install.parse_args(["--finalize-toolkit-home-migration", mid, "--quiet"])
    assert opts.finalize_migration == mid and opts.quiet


@pytest.mark.parametrize(
    "extra",
    ["--harness=claude", "--rollback", "--dry-run", "--migrate-toolkit-home",
     "--finalize-toolkit-home-migration=mig-20260923T120000Z-abcdef"],
)
def test_post_commit_flags_stand_alone(capsys, extra):
    err = _parse_error(
        capsys, ["--rollback-toolkit-home-migration=mig-20260923T120000Z-abcdef", extra]
    )
    assert "must be used alone" in err


def test_post_commit_flags_validate_the_id(capsys):
    err = _parse_error(capsys, ["--finalize-toolkit-home-migration=bad"])
    assert "invalid --finalize-toolkit-home-migration" in err


# ── crash at every checkpoint ───────────────────────────────────────────────


def _scenario(checkpoint: str) -> str:
    for prefix in ("rollback", "finalize", "restore"):
        if checkpoint.startswith(f"migrate.{prefix}."):
            return prefix
    return "migrate"


MIGRATE_ARGS = ["--migrate-toolkit-home", "--harness=claude", "--skip-reconciliation", "--json", "--quiet"]


def _driver(home: Path, *args: str, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(DRIVER), *args],
        env=fi.sandbox_env(home, extra_env),
        cwd=str(home),
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def _pointer(root: Path) -> str:
    pointer = root / agent_toolkit_paths.POINTER_RELPATH
    if not pointer.exists():
        return "legacy"
    return json.loads(pointer.read_text())["layout"]


def _check_recovered(root: Path, expected: mth.Expected) -> None:
    assert _pointer(root) == expected.layout
    baseline = _baseline_file(root)
    if expected.layout == "legacy":
        assert baseline.read_bytes() == PRIOR_BASELINE
    else:
        loaded = depart.load_baseline(baseline.parent)
        assert loaded is not None and len(loaded.layers) == 2
    legacy_items = root / ".claude" / "data" / "backlog" / "items.json"
    dest_items = root / ".agent-toolkit" / "data" / "backlog" / "items.json"
    if expected.layout == "legacy":
        assert json.loads(legacy_items.read_text())["items"][0]["id"] == "x-one"
        assert not dest_items.exists()
        for work in (root / ".agent-toolkit").glob(".migration-*"):
            staging = work / "staging"
            assert not staging.exists() or not any(staging.iterdir()), staging
    else:
        assert json.loads(dest_items.read_text())["items"][0]["id"] == "x-one"
        assert not legacy_items.exists()


def _oracle_rows() -> list[fi.OracleRow]:
    return [
        fi.OracleRow(
            checkpoint=name,
            expected=expected.action,  # type: ignore[arg-type]
            check=lambda root, _exp=expected: _check_recovered(root, _exp),
        )
        for name, expected in mth.RECOVERY_ORACLE.items()
    ]


def _assert_crash_state(home: Path, expected: mth.Expected) -> None:
    dirs = _journal_dirs(home)
    if expected.action == "none" and not expected.journal_file:
        assert dirs == []
        return
    jdir = dirs[-1]
    journal = jdir / expected.journal
    assert journal.exists() is expected.journal_file
    records = mth.read_records(jdir, expected.journal)
    last = records[-1]["event"] if records else None
    assert last == expected.last_event
    assert (jdir / mth.INVENTORY_NAME).exists() is expected.inventory
    ids = mth.history_ids(_state_dir(home) / "history.jsonl")
    assert (jdir.name in ids) is expected.history


def test_oracle_covers_every_checkpoint():
    # runtime-registered carry rows (migrate.carry.<name>.* generated from a
    # run's journaled carried list) are legitimately undeclared: they appear
    # only after a preflight discovers unclassified entries.
    fi.check_oracle_complete(
        mth.CHECKPOINTS,
        [r for r in _oracle_rows() if not r.checkpoint.startswith("migrate.carry.")],
    )


@pytest.mark.allow_real_subprocess  # the migrator runs in a child killed by SIGKILL
@pytest.mark.parametrize("checkpoint", mth.CHECKPOINTS)
def test_crash_at_checkpoint_recovers_by_the_oracle(machine, checkpoint):
    scenario = _scenario(checkpoint)
    args = ["install", *MIGRATE_ARGS]
    extra_env: dict[str, str] = {}
    if scenario in ("rollback", "finalize"):
        setup = _driver(machine, "install", *MIGRATE_ARGS)
        assert setup.returncode == 0, setup.stdout + setup.stderr
        mid = json.loads(setup.stdout)["migration_id"]
        if checkpoint.startswith("migrate.finalize.restored-"):
            rolled = _driver(
                machine,
                "install",
                f"--rollback-toolkit-home-migration={mid}",
                "--json",
                "--quiet",
            )
            assert rolled.returncode == 0, rolled.stdout + rolled.stderr
        args = ["install", f"--{scenario}-toolkit-home-migration={mid}", "--json", "--quiet"]
    if scenario == "restore":
        extra_env["MIGRATE_DRIVER_VALIDATION"] = "fail"

    fi.run_killed_at(
        checkpoint,
        DRIVER,
        args,
        home=machine,
        declared=mth.CHECKPOINTS,
        extra_env=extra_env,
        timeout=120,
    )
    expected = mth.RECOVERY_ORACLE[checkpoint]
    _assert_crash_state(machine, expected)

    result = _driver(machine, "recover")
    assert result.returncode == 0, result.stdout + result.stderr
    actions = {r["action"] for r in json.loads(result.stdout)["recovered"]}
    observed = actions.pop() if actions else "none"
    assert not actions, result.stdout
    row = next(r for r in _oracle_rows() if r.checkpoint == checkpoint)
    fi.assert_recovery(row, observed, machine)  # type: ignore[arg-type]
    for jdir in _journal_dirs(machine):
        for name in (mth.JOURNAL_NAME, mth.ROLLBACK_NAME, mth.FINALIZE_NAME):
            if (jdir / name).exists():
                assert mth.is_terminal(jdir, name), (jdir, name)


# ── uninstall baseline ──────────────────────────────────────────────────────


def _layer_tag(mid: str) -> str:
    return f"toolkit-home-migration {mid}"


def _planned_dests(records: list[dict]) -> list[str]:
    [links] = [r for r in records if r["phase"] == "links" and r["event"] == "begin"]
    return [str(link["dest"]) for link in links["detail"]["planned"]]


def test_baseline_layer_records_planned_destinations_before_links(
    machine, capsys, validation
):
    first = next(iter(link_inspect.load_links(REPO / "links.toml")))
    already = f"symlink:{link_inspect.expand_dest(first.dest, machine)}"
    prior = depart.load_baseline(_baseline_file(machine).parent)
    prior.layers[0].records[already] = {"state": "absent"}
    _baseline_file(machine).write_text(
        json.dumps(depart.baseline_to_dict(prior), indent=2, sort_keys=True) + "\n"
    )
    code, report = _migrate(capsys)
    assert code == 0, report
    mid = report["migration_id"]
    records = mth.read_records(_jdir(machine, mid))
    begun = [r["phase"] for r in records if r["event"] == "begin"]
    assert begun.index("baseline") < begun.index("links")
    assert begun.index("stage:backend-log") < begun.index("baseline")

    loaded = depart.load_baseline(_baseline_file(machine).parent)
    assert [layer.captured_at for layer in loaded.layers][-1] == _layer_tag(mid)
    ours = loaded.layers[-1].records
    assert ours, "the migration recorded nothing"
    assert already not in ours
    link_keys = [k for k in ours if k.startswith(("file:", "symlink:"))]
    assert link_keys and all(ours[k] == {"state": "absent"} for k in link_keys)
    dests = _planned_dests(records)
    sample = next(d for d in dests if f"symlink:{d}" != already)
    for key in (f"file:{sample}", f"symlink:{sample}", f"file:{sample}.bak"):
        assert key in ours, key
    assert f"directory:{machine / '.agent-toolkit'}" in ours
    assert not any(k.startswith("directory:") and "/.local/state" in k for k in ours)
    assert loaded.layers[0].records == prior.layers[0].records


def test_next_install_records_nothing_new_and_departure_owns_the_links(
    machine, capsys, validation
):
    code, report = _migrate(capsys)
    assert code == 0, report
    records = mth.read_records(_jdir(machine, report["migration_id"]))
    dests = [Path(d) for d in _planned_dests(records)]
    state_dir = _baseline_file(machine).parent
    loaded = depart.load_baseline(state_dir)
    before = len(loaded.layers)
    live = depart_exec.capture_destination_records(
        dests, home=machine, state_dir=state_dir, blob_dir=state_dir
    )
    loaded.add_layer("next-install", live)
    assert len(loaded.layers) == before
    key = f"symlink:{dests[0]}"
    verdict = depart.classify_ownership_key(key, loaded.value_for(key), live[key])
    assert (verdict.bucket, verdict.action) == (depart.BUCKET_OWNED, depart.ACTION_REMOVE)


def test_failed_validation_restores_the_baseline_bytes(machine, capsys, validation):
    validation.passes = False
    code, report = _migrate(capsys)
    assert code == 1, report
    assert _baseline_file(machine).read_bytes() == PRIOR_BASELINE


def test_a_baseline_the_migration_created_is_deleted_on_restore(
    machine, capsys, validation
):
    _baseline_file(machine).unlink()
    validation.passes = False
    code, report = _migrate(capsys)
    assert code == 1, report
    assert not _baseline_file(machine).exists()


def test_narrow_rollback_restores_the_baseline_bytes(machine, capsys, validation):
    mid = _commit(capsys)
    code, report = _rollback(capsys, mid)
    assert code == 0, report
    assert _baseline_file(machine).read_bytes() == PRIOR_BASELINE


def test_narrow_rollback_keeps_a_later_install_layer(machine, capsys, validation):
    mid = _commit(capsys)
    state_dir = _baseline_file(machine).parent
    loaded = depart.load_baseline(state_dir)
    loaded.add_layer("later-install", {"file:/nowhere/.zshrc": {"state": "absent"}})
    depart.save_baseline(state_dir, loaded)
    code, report = _rollback(capsys, mid)
    assert code == 0, report
    after = depart.load_baseline(state_dir)
    assert [layer.captured_at for layer in after.layers] == [
        "2026-01-01T00:00:00+00:00",
        "later-install",
    ]


@pytest.mark.parametrize("damage", ["edit-layer", "drop-layer", "delete", "garble"])
def test_narrow_rollback_refuses_a_changed_baseline(
    machine, capsys, validation, damage
):
    mid = _commit(capsys)
    path = _baseline_file(machine)
    if damage in ("edit-layer", "drop-layer"):
        data = json.loads(path.read_text())
        ours = next(l for l in data["layers"] if l["captured_at"] == _layer_tag(mid))
        if damage == "edit-layer":
            key = next(iter(ours["records"]))
            ours["records"][key] = {"state": "tampered"}
        else:
            data["layers"].remove(ours)
        path.write_text(json.dumps(data))
    elif damage == "delete":
        path.unlink()
    else:
        path.write_text("{not json")
    snapshot = (_tree(machine), _tree(_state_dir(machine)))
    code, report = _rollback(capsys, mid)
    assert code == 1, report
    assert "baseline.json" in report["outcome"]
    assert (_tree(machine), _tree(_state_dir(machine))) == snapshot


def test_macos_skips_the_baseline(machine, capsys, validation, monkeypatch):
    monkeypatch.setattr(mth, "_is_linux", lambda: False)
    code, report = _migrate(capsys)
    assert code == 0, report
    assert _baseline_file(machine).read_bytes() == PRIOR_BASELINE
    records = mth.read_records(_jdir(machine, report["migration_id"]))
    [begin] = [r for r in records if r["phase"] == "baseline" and r["event"] == "begin"]
    assert begin["detail"]["skipped"] == "not linux"


def test_an_unparseable_baseline_refuses_before_any_write(machine, capsys, validation):
    _baseline_file(machine).write_text("{not json")
    before = _legacy_trees()
    code, report = _migrate(capsys)
    assert code == 1, report
    assert "baseline.json" in report["outcome"]
    assert _baseline_file(machine).read_text() == "{not json"
    assert _legacy_trees() == before
    assert _layout() == "legacy"


def test_a_regular_file_at_a_planned_destination_refuses_before_the_baseline(
    machine, capsys, validation
):
    first = next(iter(link_inspect.load_links(REPO / "links.toml")))
    blocker = link_inspect.expand_dest(first.dest, machine)
    blocker.parent.mkdir(parents=True, exist_ok=True)
    blocker.write_text("user file\n")
    code, report = _migrate(capsys)
    assert code == 1, report
    assert str(blocker) in report["outcome"]
    assert _baseline_file(machine).read_bytes() == PRIOR_BASELINE
    assert blocker.read_text() == "user file\n"
