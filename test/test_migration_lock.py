#!/usr/bin/env python3
"""Tests for agent-scripts/migration_lock.py: the machine-wide migration lock."""

import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
AGENT_SCRIPTS = str(REPO / "agent-scripts")
sys.path.insert(0, AGENT_SCRIPTS)

import migration_lock  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setattr(migration_lock, "ENFORCE", False)
    migration_lock._reset_for_tests()
    yield state
    migration_lock._reset_for_tests()


def observations(state: Path) -> list[dict]:
    path = state / "agent-toolkit" / "migration-observations.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


HOLDER = textwrap.dedent(
    """
    import sys, time
    sys.path.insert(0, {scripts!r})
    import migration_lock
    from pathlib import Path
    with migration_lock.{mode}("test-holder"):
        Path(sys.argv[1]).write_text("held")
        time.sleep(float(sys.argv[2]))
    """
)


def spawn_holder(tmp_path: Path, mode: str, seconds: float = 30.0) -> subprocess.Popen:
    script = tmp_path / f"holder_{mode}.py"
    script.write_text(HOLDER.format(scripts=AGENT_SCRIPTS, mode=mode))
    ready = tmp_path / f"ready_{mode}"
    proc = subprocess.Popen(
        [sys.executable, str(script), str(ready), str(seconds)],
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
    )
    deadline = time.monotonic() + 10
    while not ready.exists():
        if time.monotonic() > deadline or proc.poll() is not None:
            proc.kill()
            raise AssertionError("holder never took the lock")
        time.sleep(0.01)
    return proc


def stop(proc: subprocess.Popen) -> None:
    proc.kill()
    proc.wait()


def test_lock_path_is_under_the_state_directory(isolated_state):
    assert migration_lock.lock_path() == isolated_state / "agent-toolkit" / "migration.lock"


# ── ordering ──────────────────────────────────────────────────────────────────


@pytest.mark.regression(
    "migration-lock-taken-inside-a-store-lock",
    "ModuleNotFoundError: No module named 'migration_lock'",
)
def test_taking_the_lock_inside_a_store_lock_is_an_ordering_error():
    migration_lock.note_store_lock_acquired()
    try:
        with pytest.raises(migration_lock.LockOrderError):
            with migration_lock.shared("late"):
                pass
    finally:
        migration_lock.note_store_lock_released()


def test_store_lock_inside_the_migration_scope_nests_freely():
    with migration_lock.shared("outer"):
        migration_lock.note_store_lock_acquired()
        try:
            with migration_lock.shared("nested"):
                pass
        finally:
            migration_lock.note_store_lock_released()


@pytest.mark.allow_real_subprocess  # an exclusive holder in a child process
def test_nested_writer_under_a_failed_outer_scope_is_not_an_ordering_error(tmp_path):
    holder = spawn_holder(tmp_path, "exclusive")
    try:
        with migration_lock.shared("outer"):  # would-block, admitted anyway
            migration_lock.note_store_lock_acquired()
            try:
                with migration_lock.shared("nested"):
                    pass
            finally:
                migration_lock.note_store_lock_released()
    finally:
        stop(holder)


# ── shared / exclusive across processes ──────────────────────────────────────


@pytest.mark.allow_real_subprocess  # a shared holder in a child process
def test_two_shared_holders_coexist(tmp_path, isolated_state):
    holder = spawn_holder(tmp_path, "shared")
    try:
        with migration_lock.shared("second"):
            pass
    finally:
        stop(holder)
    assert observations(isolated_state) == []


@pytest.mark.regression(
    "migration-lock-observe-mode-records-would-block",
    "ModuleNotFoundError: No module named 'migration_lock'",
)
@pytest.mark.allow_real_subprocess  # an exclusive holder in a child process
def test_observe_mode_records_would_block_and_proceeds(tmp_path, isolated_state):
    holder = spawn_holder(tmp_path, "exclusive")
    ran = False
    try:
        with migration_lock.shared("backlog"):
            ran = True
    finally:
        stop(holder)
    assert ran
    [obs] = observations(isolated_state)
    assert obs["event"] == "migration-lock-observe"
    assert (obs["site"], obs["outcome"]) == ("backlog", "would-block")
    assert obs["pid"] == os.getpid()


@pytest.mark.allow_real_subprocess  # an exclusive holder in a child process
def test_enforce_mode_refuses_and_names_the_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(migration_lock, "ENFORCE", True)
    holder = spawn_holder(tmp_path, "exclusive")
    try:
        with pytest.raises(migration_lock.MigrationLockBusy) as ctx:
            with migration_lock.shared("backlog"):
                pytest.fail("must not run")
    finally:
        stop(holder)
    assert str(migration_lock.lock_path()) in str(ctx.value)


@pytest.mark.allow_real_subprocess  # SIGKILLs an exclusive holder (fault harness)
def test_a_killed_exclusive_holder_releases_the_lock(tmp_path, isolated_state):
    import test_fault_injection as fi

    home = tmp_path / "home"
    home.mkdir()
    script = tmp_path / "hold_and_die.py"
    script.write_text(
        textwrap.dedent(
            f"""
            import sys
            sys.path.insert(0, {AGENT_SCRIPTS!r})
            import migration_lock, fault_checkpoint
            with migration_lock.exclusive("dying"):
                fault_checkpoint.checkpoint("lock.held")
            """
        )
    )
    fi.run_killed_at(
        "lock.held",
        script,
        home=home,
        declared=["lock.held"],
        extra_env={"PYTHONPATH": AGENT_SCRIPTS, "XDG_STATE_HOME": str(isolated_state)},
    )
    with migration_lock.shared("after"):
        pass
    assert observations(isolated_state) == []


def test_no_descriptor_is_held_between_operations_or_at_import():
    assert migration_lock._state.fd == -1
    with migration_lock.shared("op"):
        assert migration_lock._state.fd != -1
    assert migration_lock._state.fd == -1
    assert migration_lock._state.holders == 0


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_unwritable_lock_directory_records_lock_error_and_proceeds(isolated_state):
    isolated_state.mkdir(parents=True)
    lock_dir = isolated_state / "agent-toolkit"
    lock_dir.mkdir()
    lock_dir.chmod(0o555)
    try:
        ran = False
        with migration_lock.shared("backlog"):
            ran = True
    finally:
        lock_dir.chmod(0o755)
    assert ran
    # the observation file lives in the same (unwritable) directory, so the
    # last-resort stderr path is what records it; nothing raised.


# ── mixed modes and threads ──────────────────────────────────────────────────


def test_shared_inside_own_exclusive_is_allowed():
    with migration_lock.exclusive("migrator"):
        with migration_lock.shared("migrator-writes"):
            pass


def test_exclusive_inside_shared_is_refused():
    with migration_lock.shared("writer"):
        with pytest.raises(migration_lock.LockModeError):
            with migration_lock.exclusive("upgrade"):
                pass


def run_in_thread(fn) -> BaseException | None:
    box: list[BaseException | None] = [None]

    def target() -> None:
        try:
            fn()
        except BaseException as exc:  # noqa: BLE001
            box[0] = exc

    t = threading.Thread(target=target)
    t.start()
    t.join(timeout=5)
    assert not t.is_alive(), "thread hung"
    return box[0]


def test_other_thread_shared_during_exclusive_is_refused_immediately():
    def other() -> None:
        with migration_lock.shared("other-thread"):
            pass

    with migration_lock.exclusive("migrator"):
        assert isinstance(run_in_thread(other), migration_lock.LockModeError)


def test_exclusive_while_another_thread_holds_shared_is_refused_immediately():
    def other() -> None:
        with migration_lock.exclusive("other-thread"):
            pass

    with migration_lock.shared("writer"):
        assert isinstance(run_in_thread(other), migration_lock.LockModeError)


@pytest.mark.allow_real_subprocess  # another process holds shared, so exclusive waits
def test_shared_while_exclusive_is_waiting_is_refused_immediately(tmp_path):
    holder = spawn_holder(tmp_path, "shared")
    waiting = threading.Event()
    done = threading.Event()

    def acquire_exclusive() -> None:
        waiting.set()
        with migration_lock.exclusive("migrator"):
            done.set()

    t = threading.Thread(target=acquire_exclusive, daemon=True)
    t.start()
    waiting.wait(5)
    deadline = time.monotonic() + 5
    while migration_lock._state.mode != "acquiring-exclusive" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert migration_lock._state.mode == "acquiring-exclusive"
    try:
        with pytest.raises(migration_lock.LockModeError):
            with migration_lock.shared("writer"):
                pass
    finally:
        stop(holder)
        t.join(timeout=10)
    assert done.is_set()


def test_threads_share_one_descriptor_and_the_last_one_out_releases():
    inside = threading.Event()
    release = threading.Event()

    def worker() -> None:
        with migration_lock.shared("worker"):
            inside.set()
            release.wait(5)

    t = threading.Thread(target=worker)
    t.start()
    inside.wait(5)
    with migration_lock.shared("main"):
        assert migration_lock._state.holders == 2
    assert migration_lock._state.fd != -1  # worker still inside
    release.set()
    t.join(5)
    assert migration_lock._state.fd == -1


# ── observations ─────────────────────────────────────────────────────────────


def test_observation_write_failure_falls_back_to_stderr(monkeypatch, capsys):
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(migration_lock.cli_common, "append_jsonl", boom)
    migration_lock.observe("site", "lock-error", "detail")  # must not raise
    assert "migration-lock observation lost" in capsys.readouterr().err


# ── CLI ──────────────────────────────────────────────────────────────────────


def cli(*args: str, state: Path, timeout: float = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(REPO / "agent-scripts" / "migration_lock.py"), *args],
        env=dict(os.environ, XDG_STATE_HOME=str(state), PYTHONDONTWRITEBYTECODE="1"),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


@pytest.mark.allow_real_subprocess  # runs the CLI
def test_cli_status_reports_mode_and_holder(tmp_path, isolated_state):
    r = cli("status", state=isolated_state)
    assert r.returncode == 0, r.stderr
    assert "mode: observe" in r.stdout
    assert "exclusive holder: none" in r.stdout
    holder = spawn_holder(tmp_path, "exclusive")
    try:
        r = cli("status", state=isolated_state)
    finally:
        stop(holder)
    assert "exclusive holder: yes" in r.stdout


@pytest.mark.allow_real_subprocess  # runs the CLI
def test_cli_hold_then_observations(tmp_path, isolated_state):
    proc = subprocess.Popen(
        [sys.executable, str(REPO / "agent-scripts" / "migration_lock.py"), "hold", "--seconds", "3"],
        env=dict(os.environ, XDG_STATE_HOME=str(isolated_state), PYTHONDONTWRITEBYTECODE="1"),
        stdout=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 10
    while "exclusive holder: yes" not in cli("status", state=isolated_state).stdout:
        assert time.monotonic() < deadline, "hold never took the lock"
        time.sleep(0.05)
    with migration_lock.shared("soak-writer"):
        pass
    proc.wait(timeout=10)
    assert "exclusive holder: none" in cli("status", state=isolated_state).stdout
    r = cli("observations", state=isolated_state)
    assert "soak-writer" in r.stdout and "would-block" in r.stdout
    assert "observations: 1" in cli("status", state=isolated_state).stdout


def test_hold_seconds_are_capped():
    assert migration_lock._clamp_hold_seconds(10_000) == 600
    assert migration_lock._clamp_hold_seconds(-5) == 0


def test_a_scope_is_cheap_enough_for_every_tool_call():
    # guard_rails takes one shared scope per tool call: open, non-blocking
    # flock, close. Generous bound; typical cost is well under a millisecond.
    start = time.monotonic()
    for _ in range(500):
        with migration_lock.shared("guard-rail-log", quiet=True):
            pass
    assert (time.monotonic() - start) / 500 < 0.005
