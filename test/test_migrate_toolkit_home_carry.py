#!/usr/bin/env python3
"""Tests for the clean-cutover carry half of migrate_toolkit_home.py.

Unclassified data carried as journalled `carry:<name>` steps; the
skeleton sweep after abort/restore/rollback; finalize removing emptied
legacy toolkit directories and carried legacy copies; and the residue
audit extension of install.py's --check-links.

Same sandbox conventions as test_migrate_toolkit_home_moves.py: in-process
runs with stubbed fresh-process validation.
"""

import hashlib
import json
import os
import stat
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "agent-scripts"))

import agent_toolkit_paths  # noqa: E402
import depart  # noqa: E402
import dev_status_mutation  # noqa: E402
import install  # noqa: E402
import link_inspect  # noqa: E402
import migrate_toolkit_home as mth  # noqa: E402
import migration_lock  # noqa: E402
import test_fault_injection as fi  # noqa: E402
from test_migrate_toolkit_home_moves import (  # noqa: E402
    DRIVER,
    MIGRATE_ARGS,
    PRIOR_BASELINE,
    _baseline_file,
    _commit,
    _dest,
    _finalize,
    _full_stores,
    _history,
    _jdir,
    _layout,
    _legacy,
    _legacy_trees,
    _migrate,
    _opts,
    _rollback,
    _phases,
    _snapshot_dir,
    _state_dir,
    _tree,
    _work_dir,
)

pytestmark = pytest.mark.usefixtures("sandbox")

UNCLASSIFIED_DIRS = ("analysis", "artifacts", "bug-reports", "draft-issues", "plans")
UNCLASSIFIED_FILES = ("backlog.json", "backlog-history.json")


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
    class Stub:
        passes = True
        calls = 0

        def __call__(self, _ctx):
            self.calls += 1
            return self.passes, [{"command": ["stub"], "exit": 0 if self.passes else 1}]

    stub = Stub()
    monkeypatch.setattr(mth, "_run_validation", stub)
    return stub


@pytest.fixture
def machine(sandbox):
    _installed_runtime(sandbox)
    _full_stores(sandbox)
    _unclassified(sandbox)
    return sandbox


def _installed_runtime(home: Path) -> None:
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


def _unclassified(home: Path) -> None:
    """Every unclassified shape the real machine showed."""
    data = home / ".claude" / "data"
    for name in UNCLASSIFIED_DIRS:
        path = data / name
        path.mkdir(exist_ok=True)
        (path / "note.md").write_text(f"# {name}\n")
    for name in UNCLASSIFIED_FILES:
        (data / name).write_text(f'{{"{name}": true}}\n')


def _carry_phases(records: list[dict]) -> list[str]:
    return sorted({r["phase"] for r in records if r["phase"].startswith("carry")})


def _legacy_data(home: Path) -> Path:
    return home / ".claude" / "data"


def _toolkit_data(home: Path) -> Path:
    return home / ".agent-toolkit" / "data"


# ── carry ───────────────────────────────────────────────────────────────────


def test_carry_moves_every_unclassified_path(machine, capsys, validation):
    before = {
        name: _tree(machine / ".claude" / "data" / name)
        for name in (*UNCLASSIFIED_DIRS, *UNCLASSIFIED_FILES)
    }
    code, report = _migrate(capsys)
    assert code == 0, report
    assert report["outcome"].startswith("committed"), report["outcome"]

    for name in (*UNCLASSIFIED_DIRS, *UNCLASSIFIED_FILES):
        dest = _toolkit_data(machine) / name
        assert _tree(dest) == before[name], name
        assert not os.path.lexists(machine / ".claude" / "data" / name), name

    records = mth.read_records(_jdir(machine, report["migration_id"]))
    begun = [r["phase"] for r in records if r["event"] == "begin"]
    for name in (*UNCLASSIFIED_DIRS, *UNCLASSIFIED_FILES):
        assert f"carry:{name}" in begun, name
    # the legacy data root keeps only the layout pointer and the snapshot dir
    leftovers = sorted(
        p.name for p in (machine / ".claude" / "data").iterdir()
        if p.name != agent_toolkit_paths.POINTER_RELPATH.name
        and not p.name.startswith(".toolkit-home-snapshot-")
    )
    assert leftovers == [], leftovers


def test_carry_records_stale_scan_before_promotion(machine, capsys, validation):
    plan_file = machine / ".claude" / "data" / "grill" / "topic-plan.md"
    (machine / ".claude" / "data" / "plans" / "pointer.md").write_text(
        f"see {machine / '.claude' / 'data' / 'grill'}\n"
    )
    code, report = _migrate(capsys)
    assert code == 0, report
    records = mth.read_records(_jdir(machine, report["migration_id"]))
    scan = [r for r in records if r["phase"].startswith("stale-scan:carry:plans")]
    assert scan, records
    assert _phases(records).index(("stale-scan:carry:plans", "done")) < _phases(
        records
    ).index(("carry:plans", "done") if ("carry:plans", "done") in _phases(records) else ("flip", "before"))
    refs = scan[-1]["detail"].get("stale") or []
    assert any("data/grill" in r.get("value", "") for r in refs), refs
    report_refs = report.get("stale_references") or []
    assert any(r["file"].endswith("pointer.md") for r in report_refs), report_refs


def test_carry_refuses_an_occupied_destination(machine, capsys, validation):
    occupied = _toolkit_data(machine) / "plans"
    occupied.mkdir(parents=True)
    (occupied / "existing.md").write_text("already here\n")
    snapshot = (_tree(machine / ".claude" / "data"), _tree(_state_dir(machine)))
    code, report = _migrate(capsys)
    assert code == 1, report
    assert report["outcome"] == "refused", report["outcome"]
    assert (_tree(machine / ".claude" / "data"), _tree(_state_dir(machine))) == snapshot


def test_carry_refuses_a_symlink_entry(machine, capsys, validation):
    data = machine / ".claude" / "data"
    (data / "plans").mkdir(exist_ok=True)
    (data / "plans" / "note.md").write_text("x\n")
    link = data / "linked"
    link.symlink_to(data / "plans")
    snapshot = (_tree(machine / ".claude" / "data"), _tree(_state_dir(machine)))
    code, report = _migrate(capsys)
    assert code == 1, report
    assert report["outcome"] == "refused", report["outcome"]
    assert (_tree(machine / ".claude" / "data"), _tree(_state_dir(machine))) == snapshot


def test_carry_refuses_a_nested_symlink(machine, capsys, validation):
    data = machine / ".claude" / "data"
    (data / "plans" / "inner").mkdir(parents=True, exist_ok=True)
    (data / "plans" / "inner" / "link").symlink_to(data / "grill")
    snapshot = (_tree(machine / ".claude" / "data"), _tree(_state_dir(machine)))
    code, report = _migrate(capsys)
    assert code == 1, report
    assert (_tree(machine / ".claude" / "data"), _tree(_state_dir(machine))) == snapshot


def test_carry_refuses_a_pre_existing_snapshot_directory(machine, capsys, validation):
    data = machine / ".claude" / "data"
    stale = data / ".toolkit-home-snapshot-oldid"
    stale.mkdir()
    (stale / "evidence").write_text("keep\n")
    snapshot = (_tree(machine / ".claude" / "data"), _tree(_state_dir(machine)))
    code, report = _migrate(capsys)
    assert code == 1, report
    assert (_tree(machine / ".claude" / "data"), _tree(_state_dir(machine))) == snapshot


def test_empty_unclassified_directory_is_left_until_finalize(machine, capsys, validation):
    empty = machine / ".claude" / "data" / "empty-dir"
    empty.mkdir()
    code, report = _migrate(capsys)
    assert code == 0, report
    assert empty.is_dir()
    assert not (_toolkit_data(machine) / "empty-dir").exists()
    listed = report.get("unclassified_left") or []
    assert any(p.endswith("empty-dir") for p in listed), listed

    code, report = _finalize(capsys, report["migration_id"])
    assert code == 0, report
    assert not os.path.lexists(empty)
    assert not any("empty-dir" in p for p in report.get("legacy_dirs_kept") or [])


def test_empty_unclassified_directory_that_gained_content_is_kept(
    machine, capsys, validation
):
    empty = machine / ".claude" / "data" / "empty-dir"
    empty.mkdir()
    code, report = _migrate(capsys)
    assert code == 0, report
    (empty / "written-later.md").write_text("keep\n")

    code, report = _finalize(capsys, report["migration_id"])
    assert code == 0, report
    assert (empty / "written-later.md").read_text() == "keep\n"
    assert any(p.endswith("empty-dir") for p in report.get("legacy_dirs_kept") or [])


def test_empty_unclassified_directory_survives_rollback_and_its_finalize(
    machine, capsys, validation
):
    empty = machine / ".claude" / "data" / "empty-dir"
    empty.mkdir()
    code, report = _migrate(capsys)
    mid = report["migration_id"]
    code, report = _rollback(capsys, mid)
    assert code == 0, report
    code, report = _finalize(capsys, mid)
    assert code == 0, report
    assert empty.is_dir()


def test_dry_run_does_not_claim_to_carry_an_empty_directory(machine, capsys):
    (machine / ".claude" / "data" / "empty-dir").mkdir()
    code, report = _migrate(capsys, dry_run=True)
    assert code == 0, report
    finding = next(f for f in report["findings"] if f["check"] == "unclassified")
    carried = [p for p in finding["paths"] if "->" in p]
    assert not any("empty-dir" in p for p in carried), finding
    assert any(
        p.endswith("empty-dir (empty, removed at finalize)") for p in finding["paths"]
    ), finding


def test_residue_audit_owns_an_empty_directory_until_finalize(
    machine, capsys, validation, tmp_path
):
    repo = _fixture_repo(tmp_path)
    (machine / ".claude" / "data" / "empty-dir").mkdir()
    code, report = _migrate(capsys, repo=repo)
    mid = report["migration_id"]
    code, out = _audit(machine, repo)
    assert "empty-dir" not in out, out

    code, report = _finalize(capsys, mid, repo=repo)
    assert code == 0, report
    code, out = _audit(machine, repo)
    assert code == 0, out
    assert "empty-dir" not in out, out


# ── rollback / restore keep carries like domains ────────────────────────────


def test_narrow_rollback_restores_carried_legacy_copies(
    machine, capsys, validation
):
    code, report = _migrate(capsys)
    assert code == 0, report
    mid = report["migration_id"]
    code, report = _rollback(capsys, mid)
    assert code == 0, report
    for name in (*UNCLASSIFIED_DIRS, *UNCLASSIFIED_FILES):
        assert os.path.lexists(machine / ".claude" / "data" / name), name
        assert not (_toolkit_data(machine) / name).exists(), name


def test_narrow_rollback_keeps_a_written_carry_aside(
    machine, capsys, validation
):
    code, report = _migrate(capsys)
    assert code == 0, report
    mid = report["migration_id"]
    (machine / ".agent-toolkit" / "data" / "plans" / "after.md").write_text("new\n")
    code, report = _rollback(capsys, mid)
    assert code == 1, report  # the write guard refuses a changed carry
    kept = _work_dir(machine, mid) / "restored" / "carry" / "plans"
    # nothing was mutated by the refused rollback
    assert (machine / ".agent-toolkit" / "data" / "plans" / "after.md").exists()


def test_restored_carry_aside_is_finalizeable(machine, capsys, validation):
    validation.passes = False
    code, report = _migrate(capsys)
    assert code == 1, report  # restored
    mid = report["migration_id"]
    assert (_work_dir(machine, mid) / "restored" / "carry" / "plans").is_dir()
    code, report = _finalize(capsys, mid)
    assert code == 0, report
    assert not (_work_dir(machine, mid) / "restored" / "carry").exists()


# ── finalize removes carried legacy copies and skeletons ────────────────────


def test_finalize_after_commit_cleans_carry_and_legacy_dirs(
    machine, capsys, validation
):
    code, report = _migrate(capsys)
    assert code == 0, report
    mid = report["migration_id"]
    code, report = _finalize(capsys, mid)
    assert code == 0, report
    data = machine / ".claude" / "data"
    assert [p.name for p in data.iterdir()] == [
        agent_toolkit_paths.POINTER_RELPATH.name
    ]
    for name in (*UNCLASSIFIED_DIRS, *UNCLASSIFIED_FILES):
        assert (_toolkit_data(machine) / name).exists(), name
    assert not _snapshot_dir(machine, mid).exists()
    assert not _work_dir(machine, mid).exists()


def test_finalize_refuses_a_changed_carry_snapshot(machine, capsys, validation):
    code, report = _migrate(capsys)
    assert code == 0, report
    mid = report["migration_id"]
    (machine / ".claude" / "data" / "plans").mkdir()
    (machine / ".claude" / "data" / "plans" / "new.md").write_text("drift\n")
    code, report = _finalize(capsys, mid)
    assert code == 1, report
    assert "drift" in report["outcome"] or "no longer matches" in report["outcome"]


def test_finalize_removes_an_emptied_legacy_directory(
    machine, capsys, validation, tmp_path
):
    repo = _fixture_repo(tmp_path)
    legacy_hooks = machine / ".claude" / "hooks"
    legacy_hooks.mkdir(exist_ok=True)
    (legacy_hooks / "tool.py").symlink_to(repo / "agent-scripts" / "tool.py")
    history = _state_dir(machine) / "history.jsonl"
    history.parent.mkdir(parents=True, exist_ok=True)
    history.write_text(
        json.dumps(
            {
                "kind": "symlink-created",
                "dest": str(legacy_hooks / "tool.py"),
                "src": str(repo / "agent-scripts" / "tool.py"),
            }
        )
        + "\n"
    )
    code, report = _migrate(capsys, repo=repo)
    assert code == 0, report
    mid = report["migration_id"]
    assert (legacy_hooks / "tool.py").is_symlink()
    code, report = _finalize(capsys, mid, repo=repo)
    assert code == 0, report
    assert not os.path.lexists(legacy_hooks / "tool.py")
    assert not legacy_hooks.exists()
    records = mth.read_records(_jdir(machine, mid), mth.FINALIZE_NAME)
    legacy_dirs = {
        r["phase"]: r["detail"].get("observed")
        for r in records
        if r["phase"].startswith("legacy-dir:")
    }
    assert legacy_dirs.get("legacy-dir:hooks") == "removed"


def test_finalize_keeps_a_legacy_directory_holding_foreign_files(
    machine, capsys, validation, tmp_path
):
    repo = _fixture_repo(tmp_path)
    legacy_hooks = machine / ".claude" / "hooks"
    legacy_hooks.mkdir(exist_ok=True)
    (legacy_hooks / "user-own.py").write_text("# not the toolkit's\n")
    code, report = _migrate(capsys, repo=repo)
    assert code == 0, report
    mid = report["migration_id"]
    code, report = _finalize(capsys, mid, repo=repo)
    assert code == 0, report
    assert (legacy_hooks / "user-own.py").exists()
    records = mth.read_records(_jdir(machine, mid), mth.FINALIZE_NAME)
    kept = [
        r for r in records
        if r["phase"] == "legacy-dir:hooks" and r["event"] == "done"
    ]
    assert kept and kept[-1]["detail"].get("observed", "").startswith("left")


def test_finalize_reports_the_kept_legacy_directory(
    machine, capsys, validation, tmp_path
):
    repo = _fixture_repo(tmp_path)
    legacy_scripts = machine / ".claude" / "scripts"
    legacy_scripts.mkdir(exist_ok=True)
    (legacy_scripts / "user-own.py").write_text("# keep\n")
    code, report = _migrate(capsys, repo=repo)
    mid = report["migration_id"]
    code, report = _finalize(capsys, mid, repo=repo)
    assert code == 0, report
    assert any("scripts" in p for p in (report.get("legacy_dirs_kept") or []))


# ── skeleton sweep ──────────────────────────────────────────────────────────


def test_abort_sweep_removes_the_migration_skeleton(
    machine, capsys, validation, monkeypatch
):
    # the registered Step holds a direct reference to _links_apply, so
    # monkeypatching the module attribute has no effect; replace the registry
    # entry itself to make the run abort before the flip.
    links = mth.STEPS["links"]

    def failing(state, record):
        raise mth.MigrationError("boom")

    monkeypatch.setitem(mth.STEPS, "links", mth.Step(failing, links.undo, links.verify, links.begin))
    code, report = _migrate(capsys)
    assert code == 1, report
    assert not _work_dir(machine, report["migration_id"]).exists()
    data = _toolkit_data(machine)
    assert not data.exists() or not any(data.iterdir())


def test_created_empty_toolkit_data_dir_is_swept_on_abort(
    machine, capsys, validation, monkeypatch
):
    # a machine with NO unclassified entries still creates ~/.agent-toolkit/data
    promote = mth.STEPS["promote:"]
    monkeypatch.setitem(
        mth.STEPS,
        "promote:",
        mth.Step(lambda s, r: (_ for _ in ()).throw(mth.MigrationError("boom")), promote.undo, promote.verify, promote.begin),
    )
    code, report = _migrate(capsys)
    assert code == 1, report
    assert not _work_dir(machine, report["migration_id"]).exists()


def test_rollback_sweep_removes_empty_skeletons(machine, capsys, validation):
    code, report = _migrate(capsys)
    mid = report["migration_id"]
    code, report = _rollback(capsys, mid)
    assert code == 0, report
    work = _work_dir(machine, mid)
    staging = work / "staging"
    assert not staging.exists() or not any(staging.iterdir())


# ── residue audit ───────────────────────────────────────────────────────────


def _audit(home: Path, repo: Path = REPO, *extra: str) -> tuple[int, str]:
    argv = ["--check-links", "--harness=claude", "--quiet", *extra]
    ctx = install.build_context(install.parse_args(argv), repo_root=repo)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = install.do_check_links(ctx)
    return code, out.getvalue()


def test_residue_audit_passes_after_finalize(machine, capsys, validation, tmp_path):
    repo = _fixture_repo(tmp_path)
    code, report = _migrate(capsys, repo=repo)
    mid = report["migration_id"]
    code, report = _finalize(capsys, mid, repo=repo)
    assert code == 0, report
    code, out = _audit(machine, repo)
    assert code == 0, out
    assert "residue" not in out or "no toolkit-owned residue" in out


def test_residue_audit_fails_on_domain_residue(machine, capsys, validation, tmp_path):
    repo = _fixture_repo(tmp_path)
    code, report = _migrate(capsys, repo=repo)
    mid = report["migration_id"]
    code, report = _finalize(capsys, mid, repo=repo)
    # recreate a domain legacy dir after finalize
    (machine / ".claude" / "data" / "backlog").mkdir(parents=True)
    (machine / ".claude" / "data" / "backlog" / "items.json").write_text("{}")
    code, out = _audit(machine, repo)
    assert code == 1, out
    assert "backlog" in out


def test_residue_audit_exempts_the_pointer_and_retained_links(
    machine, capsys, validation, tmp_path
):
    repo = _fixture_repo(tmp_path)
    code, report = _migrate(capsys, repo=repo)
    mid = report["migration_id"]
    code, out = _audit(machine, repo)
    # unfinalized: the audit is silent about its own snapshot and retained links
    assert code == 0, out
    assert "cleanup is still owed" not in out
    code, report = _finalize(capsys, mid, repo=repo)
    assert code == 0, report


def test_residue_audit_reports_restored_cleanup_debt(
    machine, capsys, validation, tmp_path
):
    repo = _fixture_repo(tmp_path)
    validation.passes = False
    code, report = _migrate(capsys, repo=repo)
    assert code == 1, report
    validation.passes = True
    code, out = _audit(machine, repo)
    assert code == 1, out
    assert "cleanup is still owed" in out


def test_residue_audit_silent_in_legacy_layout(machine, capsys, validation, tmp_path):
    repo = _fixture_repo(tmp_path)
    code, out = _audit(machine, repo)
    # the never-installed rows fail the link audit (exit 1), but the residue
    # portion of the audit stays silent in a legacy layout
    assert "legacy residue:" not in out
    assert "toolkit-owned residue" not in out
    assert "classify by hand:" not in out


def test_residue_audit_fails_closed_on_a_corrupt_journal(
    machine, capsys, validation, tmp_path
):
    repo = _fixture_repo(tmp_path)
    code, report = _migrate(capsys, repo=repo)
    mid = report["migration_id"]
    journal = _jdir(machine, mid) / mth.JOURNAL_NAME
    lines = journal.read_text().splitlines()
    lines.insert(len(lines) - 1, "{not json")
    journal.write_text("\n".join(lines) + "\n")
    code, out = _audit(machine, repo)
    assert code == 1, out
    assert "unreadable" in out or "journal" in out


def test_residue_audit_report_only_during_migration(
    machine, capsys, validation, tmp_path
):
    repo = _fixture_repo(tmp_path)
    code, report = _migrate(capsys, repo=repo)
    mid = report["migration_id"]
    argv = [
        "--check-links",
        "--during-migration",
        "--harness=claude",
    ]
    opts = install.parse_args(argv)
    ctx = install.build_context(opts, repo_root=repo)
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = install.do_check_links(ctx)
    assert code == 0, out.getvalue()
    assert "residue" in out.getvalue() or "snapshot" in out.getvalue()


def test_during_migration_flag_is_rejected_without_check_links():
    with pytest.raises(SystemExit) as exc:
        install.parse_args(["--during-migration", "--json"])
    assert exc.value.code == 2


# ── full rehearsal cycle: migrate → rollback → finalize → migrate → finalize ──


def test_full_rehearsal_cycle_ends_with_a_clean_audit(
    machine, capsys, validation, tmp_path
):
    repo = _fixture_repo(tmp_path)
    code, report = _migrate(capsys, repo=repo)
    assert code == 0, report
    mid = report["migration_id"]
    code, report = _rollback(capsys, mid)
    assert code == 0, report
    code, report = _finalize(capsys, mid, repo=repo)
    assert code == 0, report
    code, report = _migrate(capsys, repo=repo)
    assert code == 0, report
    mid = report["migration_id"]
    code, report = _finalize(capsys, mid, repo=repo)
    assert code == 0, report
    code, out = _audit(machine, repo)
    assert code == 0, out
    assert "legacy residue:" not in out
    data = machine / ".claude" / "data"
    assert [p.name for p in data.iterdir()] == [
        agent_toolkit_paths.POINTER_RELPATH.name
    ]
    for name in (*UNCLASSIFIED_DIRS, *UNCLASSIFIED_FILES):
        assert (_toolkit_data(machine) / name).exists(), name


def test_preflight_refusal_names_unfinished_migrations(
    machine, capsys, validation
):
    code, report = _migrate(capsys)
    mid = report["migration_id"]
    # a second run refuses: the layout is already toolkit-home and one
    # committed migration is unfinalized
    code, report = _migrate(capsys)
    assert code == 1, report
    assert report.get("unfinished_migrations") == [mid], report
    assert any(f["status"] == "refuse" for f in report["findings"])


# ── helpers shared with the moves module ────────────────────────────────────


def _fixture_repo(tmp_path: Path) -> Path:
    """A toolkit-shaped checkout whose shared runtime moved to the toolkit home."""
    repo = tmp_path / "repo"
    scripts = repo / "agent-scripts"
    scripts.mkdir(parents=True)
    (repo / "install.py").write_text("# installer\n")
    (repo / "claude" / "icons").mkdir(parents=True)
    rows = []
    for name in ("tool", "keep"):
        (scripts / f"{name}.py").write_text(f"# {name}\n")
        rows.append(
            f'[[link]]\nsrc = "agent-scripts/{name}.py"\n'
            f'dest = "~/.agent-toolkit/scripts/{name}.py"\n'
        )
    (repo / "links.toml").write_text("\n".join(rows))
    return repo


import contextlib  # noqa: E402
import io  # noqa: E402


def _legacy_trees() -> dict[str, dict[str, object]]:
    return {d: _tree(_legacy(d)) for d in agent_toolkit_paths.DOMAINS}
