#!/usr/bin/env python3
"""Tests for ``install.sh --move-layout-pointer`` (Release 2 pointer move)."""

from __future__ import annotations

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
import install  # noqa: E402
import migrate_toolkit_home as mth  # noqa: E402
import migration_lock  # noqa: E402
import test_fault_injection as fi  # noqa: E402

DRIVER = Path(__file__).resolve().parent / "_migrate_driver.py"
CANON = agent_toolkit_paths.pointer_payload("toolkit-home")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A finished Release 1 machine: deployed Release 2 resolver, no pointer yet."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local" / "state"))
    monkeypatch.delenv(agent_toolkit_paths.ENV_HOME, raising=False)
    migration_lock._reset_for_tests()
    agent_toolkit_paths.DEFAULT_RESOLVER.invalidate()
    scripts = home / ".agent-toolkit" / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "agent_toolkit_paths.py").symlink_to(
        REPO / "agent-scripts" / "agent_toolkit_paths.py"
    )
    yield home
    migration_lock._reset_for_tests()
    agent_toolkit_paths.DEFAULT_RESOLVER.invalidate()


@pytest.fixture
def states(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """The Release 1 status rows; one finalized migration unless a test edits it."""
    rows: list[dict[str, object]] = [
        {"id": "mig-20260101T000000Z-aaaaaa", "state": "finalized"}
    ]
    monkeypatch.setattr(mth, "_migration_states", lambda _state: rows)
    return rows


def _old(home: Path) -> Path:
    return home / agent_toolkit_paths.POINTER_RELPATH


def _new(home: Path) -> Path:
    return home / ".agent-toolkit" / "data" / "toolkit_state.json"


def _write_old(home: Path, layout: str = "toolkit-home") -> None:
    agent_toolkit_paths.write_pointer(home, layout, toolkit_root=None)  # type: ignore[arg-type]


def _write_new(home: Path, layout: str = "toolkit-home") -> None:
    agent_toolkit_paths.write_pointer(
        home,
        layout,  # type: ignore[arg-type]
        toolkit_root=home / ".agent-toolkit",
    )


def _run(capsys: pytest.CaptureFixture[str]) -> tuple[int, dict[str, object]]:
    code = mth.move_layout_pointer_command(
        mth.MigrationOptions(harnesses=(), json_report=True), repo_root=REPO
    )
    out = capsys.readouterr().out
    return code, json.loads(out) if out.strip() else {}


def _assert_moved(home: Path) -> None:
    assert _new(home).read_bytes() == CANON
    assert not os.path.lexists(_old(home))
    assert not os.path.lexists(home / ".claude" / "data")
    agent_toolkit_paths.DEFAULT_RESOLVER.invalidate()
    assert agent_toolkit_paths.current_layout() == "toolkit-home"


def _snapshot(home: Path) -> dict[str, object]:
    """Every entry under ``home`` except the lock infrastructure."""
    lock = migration_lock.lock_path()
    snap: dict[str, object] = {}
    for path in sorted(home.rglob("*")):
        # The lock file and the directories created to hold it.
        if lock.is_relative_to(path) or lock.parent in path.parents:
            continue
        info = path.lstat()
        rel = str(path.relative_to(home))
        if stat.S_ISLNK(info.st_mode):
            snap[rel] = ("link", os.readlink(path))
        elif stat.S_ISREG(info.st_mode):
            snap[rel] = ("file", path.read_bytes())
        else:
            snap[rel] = (stat.S_IFMT(info.st_mode),)
    return snap


# ── success paths ────────────────────────────────────────────────────────────


def test_moves_the_old_pointer_and_removes_the_empty_data_dir(home, states, capsys):
    _write_old(home)
    code, report = _run(capsys)
    assert code == 0, report
    assert report["outcome"] == "moved"
    _assert_moved(home)
    assert (home / ".claude").is_dir()


def test_rerun_after_success_reports_already_moved_and_changes_nothing(
    home, states, capsys
):
    _write_old(home)
    assert _run(capsys)[0] == 0
    before = _snapshot(home)
    code, report = _run(capsys)
    assert code == 0, report
    assert report["outcome"] == "already-moved"
    assert _snapshot(home) == before


@pytest.mark.parametrize("start", ["both", "new-with-empty-data-dir", "new-only"])
def test_every_interrupted_state_converges(home, states, capsys, start):
    if start == "both":
        _write_old(home)
        _write_new(home)
    elif start == "new-with-empty-data-dir":
        _write_new(home)
        (home / ".claude" / "data").mkdir(parents=True)
    else:
        _write_new(home)
    code, report = _run(capsys)
    assert code == 0, report
    _assert_moved(home)


def test_non_empty_data_dir_is_left_and_reported(home, states, capsys):
    _write_old(home)
    foreign = home / ".claude" / "data" / "notes.txt"
    foreign.write_text("not toolkit data")
    code, report = _run(capsys)
    assert code == 0, report
    assert foreign.read_text() == "not toolkit data"
    assert not os.path.lexists(_old(home))
    assert report["data_dir_kept"] == [str(foreign)]


def test_greenfield_machine_proceeds_without_a_finalized_row(home, states, capsys):
    states.clear()
    _write_old(home)
    code, report = _run(capsys)
    assert code == 0, report
    _assert_moved(home)


def test_abandoned_rows_do_not_block_the_move(home, states, capsys):
    states.append({"id": "mig-20260102T000000Z-bbbbbb", "state": "abandoned"})
    _write_old(home)
    assert _run(capsys)[0] == 0
    _assert_moved(home)


# ── crash and re-run ─────────────────────────────────────────────────────────


@pytest.mark.allow_real_subprocess  # the command runs in a child killed by SIGKILL
@pytest.mark.parametrize("checkpoint", mth.POINTER_MOVE_CHECKPOINTS)
def test_kill_at_each_checkpoint_then_rerun_converges(home, states, capsys, checkpoint):
    _write_old(home)
    fi.run_killed_at(
        checkpoint,
        DRIVER,
        ["install", "--move-layout-pointer", "--json", "--quiet"],
        home=home,
        declared=mth.POINTER_MOVE_CHECKPOINTS,
        extra_env={"MIGRATE_DRIVER_STATES": "finalized"},
        timeout=60,
    )
    code, report = _run(capsys)
    assert code == 0, report
    _assert_moved(home)


# ── refusals ─────────────────────────────────────────────────────────────────


def _setup_refusal(home: Path, states: list, monkeypatch, case: str) -> None:
    data = home / ".claude" / "data"
    if case == "old-says-legacy":
        _write_old(home, "legacy")
    elif case == "new-says-legacy":
        _write_old(home)
        _write_new(home, "legacy")
    elif case == "old-not-canonical-bytes":
        data.mkdir(parents=True)
        _old(home).write_text('{"layout": "toolkit-home", "schema": 1}\n')
    elif case == "symlinked-data-dir":
        elsewhere = home / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "toolkit_state.json").write_bytes(CANON)
        (home / ".claude").mkdir()
        data.symlink_to(elsewhere)
    elif case == "symlinked-old-leaf":
        data.mkdir(parents=True)
        target = home / "real-pointer"
        target.write_bytes(CANON)
        _old(home).symlink_to(target)
    elif case == "dangling-old-leaf":
        data.mkdir(parents=True)
        _old(home).symlink_to(home / "missing")
        _write_new(home)
    elif case == "fifo-old-leaf":
        data.mkdir(parents=True)
        os.mkfifo(_old(home))
    elif case == "symlinked-toolkit-data-dir":
        _write_old(home)
        real = home / "real-data"
        real.mkdir()
        (home / ".agent-toolkit" / "data").symlink_to(real)
    elif case == "root-beneath-claude":
        _write_old(home)
        monkeypatch.setenv(agent_toolkit_paths.ENV_HOME, str(home / ".claude" / "tk"))
        scripts = home / ".claude" / "tk" / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "agent_toolkit_paths.py").symlink_to(
            REPO / "agent-scripts" / "agent_toolkit_paths.py"
        )
    elif case == "layout-is-legacy":
        (data / "backlog").mkdir(parents=True)
    elif case == "not-finalized-and-not-greenfield":
        states.clear()
        _write_old(home)
        (data / "backlog").mkdir(parents=True)
    elif case == "old-resolver-deployed":
        _write_old(home)
        deployed = home / ".agent-toolkit" / "scripts" / "agent_toolkit_paths.py"
        deployed.unlink()
        deployed.write_text("# a Release 1 resolver\nPOINTER_RELPATH = None\n")
    elif case == "no-resolver-deployed":
        _write_old(home)
        (home / ".agent-toolkit" / "scripts" / "agent_toolkit_paths.py").unlink()
    else:
        states.append({"id": "mig-20260102T000000Z-bbbbbb", "state": case})
        _write_old(home)


@pytest.mark.parametrize(
    "case",
    [
        "old-says-legacy",
        "new-says-legacy",
        "old-not-canonical-bytes",
        "symlinked-data-dir",
        "symlinked-old-leaf",
        "dangling-old-leaf",
        "fifo-old-leaf",
        "symlinked-toolkit-data-dir",
        "root-beneath-claude",
        "layout-is-legacy",
        "not-finalized-and-not-greenfield",
        "old-resolver-deployed",
        "no-resolver-deployed",
        "live",
        "committed-unfinalized",
        "restored-unfinalized",
        "rolled-back",
        "finalized-restored",
    ],
)
def test_refusals_change_nothing(home, states, capsys, monkeypatch, case):
    _setup_refusal(home, states, monkeypatch, case)
    agent_toolkit_paths.DEFAULT_RESOLVER.invalidate()
    before = _snapshot(home)
    code, report = _run(capsys)
    assert code == 1, report
    assert str(report["outcome"]).startswith("refused:"), report
    assert _snapshot(home) == before


def test_busy_lock_refuses_with_the_lock_exit_code(home, states, capsys):
    _write_old(home)
    path = migration_lock.lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_SH)
        code, _ = _run(capsys)
    finally:
        os.close(fd)
    assert code == migration_lock.REFUSAL_EXIT_CODE
    assert _old(home).read_bytes() == CANON
    assert not _new(home).exists()


def test_text_report_names_both_pointers_and_restart_advice(home, states, capsys):
    _write_old(home)
    code = mth.move_layout_pointer_command(
        mth.MigrationOptions(harnesses=()), repo_root=REPO
    )
    out = capsys.readouterr().out
    assert code == 0
    assert str(_new(home)) in out
    assert "restart" in out.lower()


# ── install.py wiring ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "extra", [[], ["--json"], ["--quiet"], ["--verbose"], ["--json", "--quiet"]]
)
def test_parse_accepts_the_flag_alone_or_with_output_flags(extra):
    opts = install.parse_args(["--move-layout-pointer", *extra])
    assert opts.move_layout_pointer
    assert opts.harnesses == ()


@pytest.mark.parametrize(
    "extra",
    [
        ["--harness=claude"],
        ["--dry-run"],
        ["--rollback"],
        ["--check-links"],
        ["--depart"],
        ["--migrate-toolkit-home"],
        ["--finalize-toolkit-home-migration=mig-20260101T000000Z-aaaaaa"],
        ["--profile=work"],
        ["--skip-reconciliation"],
    ],
)
def test_parse_rejects_other_actions_and_modifiers(extra, capsys):
    with pytest.raises(SystemExit) as exc_info:
        install.parse_args(["--move-layout-pointer", *extra])
    assert exc_info.value.code == 2


def test_main_dispatches_to_the_move_command(monkeypatch):
    seen: list[mth.MigrationOptions] = []

    def fake(opts: mth.MigrationOptions, *, repo_root: Path) -> int:
        seen.append(opts)
        return 0

    monkeypatch.setattr(mth, "move_layout_pointer_command", fake)
    assert install.main(["--move-layout-pointer", "--json"]) == 0
    assert len(seen) == 1 and seen[0].json_report
