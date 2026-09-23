#!/usr/bin/env python3
"""The migration lock is adopted by every store lock and writer, outermost."""

import os
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
AGENT_SCRIPTS = str(REPO / "agent-scripts")
sys.path.insert(0, AGENT_SCRIPTS)

import dev_status_storage  # noqa: E402
import migration_lock  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(migration_lock, "ENFORCE", False)
    migration_lock._reset_for_tests()
    yield tmp_path
    migration_lock._reset_for_tests()


def assert_migration_outermost_and_held() -> None:
    """Called from inside a store lock: migration scope open, flock held, store noted."""
    assert migration_lock._depth() >= 1
    assert migration_lock._state.mode == "shared"
    assert migration_lock._state.fd != -1
    assert migration_lock._store_locks() >= 1


def assert_all_released() -> None:
    assert migration_lock._state.holders == 0
    assert migration_lock._state.fd == -1
    assert migration_lock._store_locks() == 0
    assert migration_lock._depth() == 0


def test_backlog_lock_takes_the_migration_scope_first(tmp_path):
    d = tmp_path / "backlog"
    (d).mkdir()
    (d / "_machine_id").write_text("0ee2ec8d")
    with dev_status_storage.backlog_lock(d, d / ".backlog.lock"):
        assert_migration_outermost_and_held()
        # a nested store lock and nested scope are legitimate
        with dev_status_storage.backlog_lock(d, d / ".backlog.lock"):
            assert_migration_outermost_and_held()
    assert_all_released()
    assert dev_status_storage._backlog_lock_count == 0


def test_out_of_scope_lock_takes_the_migration_scope_first(tmp_path):
    d = tmp_path / "oos"
    with dev_status_storage.out_of_scope_lock(d, d / ".lock"):
        assert_migration_outermost_and_held()
    assert_all_released()


def test_cross_store_nesting_is_not_an_ordering_error(tmp_path):
    d = tmp_path / "backlog"
    d.mkdir()
    (d / "_machine_id").write_text("0ee2ec8d")
    with dev_status_storage.backlog_lock(d, d / ".backlog.lock"):
        with dev_status_storage.out_of_scope_lock(tmp_path / "oos", tmp_path / "oos.lock"):
            assert migration_lock._store_locks() == 2
    assert_all_released()


def test_grill_session_locks_take_the_migration_scope_first(tmp_path):
    import grill

    with grill.session_lock("demo-session", data_dir=tmp_path / "grill"):
        assert_migration_outermost_and_held()
    assert_all_released()


def test_recap_regen_lock_takes_the_migration_scope_first(tmp_path, monkeypatch):
    import dev_status_impl

    monkeypatch.setattr(dev_status_impl, "DATA_DIR", tmp_path / "backlog")
    monkeypatch.setattr(dev_status_impl, "RECAP_REGEN_LOCK_FILE", tmp_path / "backlog" / "r.lock")
    with dev_status_impl._regen_lock(blocking=True) as acquired:
        assert acquired
        assert_migration_outermost_and_held()
    assert_all_released()


def test_machine_id_creation_takes_the_scope_but_a_read_does_not(tmp_path, monkeypatch):
    seen: list[str] = []
    real = migration_lock.shared

    def spy(site):
        seen.append(site)
        return real(site)

    monkeypatch.setattr(migration_lock, "shared", spy)
    d = tmp_path / "backlog"
    created = dev_status_storage.machine_id(d / "_machine_id", d)
    assert seen == ["machine-id"]
    seen.clear()
    assert dev_status_storage.machine_id(d / "_machine_id", d) == created
    assert seen == []


# ── partial-acquisition failures leave everything usable ─────────────────────


def test_backlog_lock_is_usable_after_the_migration_scope_is_refused(tmp_path, monkeypatch):
    d = tmp_path / "backlog"
    d.mkdir()
    (d / "_machine_id").write_text("0ee2ec8d")

    def refuse(site):
        raise migration_lock.MigrationLockBusy("refused for test")

    with monkeypatch.context() as m:
        m.setattr(migration_lock, "shared", refuse)
        with pytest.raises(migration_lock.MigrationLockBusy):
            with dev_status_storage.backlog_lock(d, d / ".backlog.lock"):
                pytest.fail("must not run")
    assert dev_status_storage._backlog_lock_count == 0
    assert dev_status_storage._backlog_lock_fd == -1
    with dev_status_storage.backlog_lock(d, d / ".backlog.lock"):
        pass
    assert_all_released()


def test_backlog_lock_is_usable_after_the_store_flock_fails(tmp_path, monkeypatch):
    d = tmp_path / "backlog"
    d.mkdir()
    (d / "_machine_id").write_text("0ee2ec8d")
    real_flock = dev_status_storage.fcntl.flock
    calls = {"n": 0}

    def failing_flock(fd, op):
        # fail only the store's exclusive flock, not the migration lock's
        if op == dev_status_storage.fcntl.LOCK_EX and calls["n"] == 0:
            calls["n"] += 1
            raise OSError("flock failed for test")
        return real_flock(fd, op)

    with monkeypatch.context() as m:
        m.setattr(dev_status_storage.fcntl, "flock", failing_flock)
        with pytest.raises(OSError):
            with dev_status_storage.backlog_lock(d, d / ".backlog.lock"):
                pytest.fail("must not run")
    assert_all_released()
    assert dev_status_storage._backlog_lock_count == 0
    with dev_status_storage.backlog_lock(d, d / ".backlog.lock"):
        assert_migration_outermost_and_held()


def test_out_of_scope_lock_is_usable_after_the_migration_scope_is_refused(tmp_path, monkeypatch):
    def refuse(site):
        raise migration_lock.MigrationLockBusy("refused for test")

    with monkeypatch.context() as m:
        m.setattr(migration_lock, "shared", refuse)
        with pytest.raises(migration_lock.MigrationLockBusy):
            with dev_status_storage.out_of_scope_lock(tmp_path / "oos", tmp_path / "oos.lock"):
                pass
    assert dev_status_storage._out_of_scope_lock_count == 0
    with dev_status_storage.out_of_scope_lock(tmp_path / "oos", tmp_path / "oos.lock"):
        assert_migration_outermost_and_held()
    assert_all_released()


# ── cross-process: the lock is really held during a write ────────────────────

PROBE = textwrap.dedent(
    """
    import fcntl, os, sys
    fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("busy")
    else:
        print("free")
    """
)


@pytest.mark.allow_real_subprocess  # probes the lock from a second process
def test_a_backlog_write_holds_the_shared_lock_against_an_exclusive_probe(tmp_path):
    probe = tmp_path / "probe.py"
    probe.write_text(PROBE)
    lock = str(migration_lock.lock_path())

    def ask() -> str:
        return subprocess.run(
            [sys.executable, str(probe), lock], capture_output=True, text=True, timeout=30
        ).stdout.strip()

    d = tmp_path / "backlog"
    d.mkdir()
    (d / "_machine_id").write_text("0ee2ec8d")
    with dev_status_storage.backlog_lock(d, d / ".backlog.lock"):
        assert ask() == "busy"
    assert ask() == "free"


HOLD = textwrap.dedent(
    """
    import sys, time
    sys.path.insert(0, {scripts!r})
    import migration_lock
    from pathlib import Path
    with migration_lock.exclusive("migrator"):
        Path(sys.argv[1]).write_text("held")
        time.sleep(30)
    """
)


@pytest.mark.allow_real_subprocess  # an exclusive holder in a child process
def test_nested_store_writes_under_a_would_block_scope_proceed(tmp_path):
    script = tmp_path / "hold.py"
    script.write_text(HOLD.format(scripts=AGENT_SCRIPTS))
    ready = tmp_path / "ready"
    proc = subprocess.Popen(
        [sys.executable, str(script), str(ready)],
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
    )
    try:
        deadline = time.monotonic() + 10
        while not ready.exists():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        d = tmp_path / "backlog"
        d.mkdir()
        (d / "_machine_id").write_text("0ee2ec8d")
        with dev_status_storage.backlog_lock(d, d / ".backlog.lock"):
            with dev_status_storage.out_of_scope_lock(tmp_path / "oos", tmp_path / "oos.lock"):
                assert migration_lock._state.mode == "shared-unlocked"
    finally:
        proc.kill()
        proc.wait()
    assert_all_released()


# ── enforce paths (dormant in stage 0a; ENFORCE patched True here) ───────────


@pytest.fixture()
def exclusive_holder(tmp_path):
    script = tmp_path / "hold_ex.py"
    script.write_text(HOLD.format(scripts=AGENT_SCRIPTS))
    ready = tmp_path / "ready_ex"
    proc = subprocess.Popen(
        [sys.executable, str(script), str(ready)],
        env=dict(os.environ, PYTHONDONTWRITEBYTECODE="1"),
    )
    deadline = time.monotonic() + 10
    while not ready.exists():
        assert time.monotonic() < deadline and proc.poll() is None
        time.sleep(0.01)
    yield proc
    proc.kill()
    proc.wait()


def observations_file() -> Path:
    return migration_lock.observations_path()


def outcomes() -> list[tuple[str, str]]:
    import json

    p = observations_file()
    if not p.exists():
        return []
    return [(r["site"], r["outcome"]) for r in map(json.loads, p.read_text().splitlines())]


@pytest.mark.allow_real_subprocess  # exclusive holder child
def test_enforce_dev_status_mutation_exits_75(exclusive_holder, monkeypatch, capsys):
    import dev_status_impl

    monkeypatch.setattr(migration_lock, "ENFORCE", True)
    monkeypatch.setattr(sys, "argv", ["dev_status.py", "add", '{"id": "blocked-item", "summary": "x"}'])
    with pytest.raises(SystemExit) as ctx:
        dev_status_impl.main()
    assert ctx.value.code == 75
    err = capsys.readouterr().err
    assert str(migration_lock.lock_path()) in err
    assert "Traceback" not in err


@pytest.mark.allow_real_subprocess  # exclusive holder child
def test_enforce_grill_write_exits_75(exclusive_holder, monkeypatch, capsys):
    import grill

    monkeypatch.setattr(migration_lock, "ENFORCE", True)
    monkeypatch.setattr(sys, "argv", ["grill.py", "new", '{"topic": "blocked"}'])
    with pytest.raises(SystemExit) as ctx:
        grill.main()
    assert ctx.value.code == 75
    assert "[grill]" in capsys.readouterr().err


@pytest.mark.allow_real_subprocess  # exclusive holder child
def test_enforce_vitals_apply_exits_75_but_dry_run_works(exclusive_holder, monkeypatch, tmp_path):
    import vitals_promotion

    data = tmp_path / "grill"
    data.mkdir()
    monkeypatch.setattr(migration_lock, "ENFORCE", True)
    monkeypatch.setattr(sys, "argv", ["vitals_promotion.py", "--data-dir", str(data), "--apply"])
    with pytest.raises(SystemExit) as ctx:
        vitals_promotion.main()
    assert ctx.value.code == 75
    monkeypatch.setattr(sys, "argv", ["vitals_promotion.py", "--data-dir", str(data)])
    vitals_promotion.main()  # dry run takes no scope, so it still runs


@pytest.mark.allow_real_subprocess  # exclusive holder child
def test_enforce_ticket_runner_exits_75(exclusive_holder, monkeypatch, tmp_path):
    import json

    import to_tickets_runner

    batch = tmp_path / "b.json"
    batch.write_text(json.dumps([{"id": "never-made", "summary": "x"}]))
    monkeypatch.setattr(migration_lock, "ENFORCE", True)
    monkeypatch.setattr(sys, "argv", ["to_tickets_runner.py", "run", str(batch)])
    with pytest.raises(SystemExit) as ctx:
        to_tickets_runner.main()
    assert ctx.value.code == 75


@pytest.mark.allow_real_subprocess  # exclusive holder child
def test_enforce_telemetry_skips_and_observes_without_breaking_callers(
    exclusive_holder, monkeypatch, tmp_path, capsys
):
    import cli_common
    import guard_rails
    import llm_backends

    monkeypatch.setattr(migration_lock, "ENFORCE", True)
    audit = tmp_path / "audit.jsonl"
    monkeypatch.setattr(guard_rails, "GUARD_RAILS_LOG_PATH", audit)
    guard_rails._audit_verdict("claude", None, guard_rails.Verdict("allow"))
    assert not audit.exists()

    backend = tmp_path / "backend.jsonl"
    monkeypatch.setattr(llm_backends, "_backend_call_log_path", lambda: backend)
    llm_backends._log_backend_call("codex", None, "ok", 0.1, 10)
    assert not backend.exists()

    cli_common._append_timing_record({"x": 1})
    assert ("guard-rail-log", "refused-telemetry") in outcomes()
    assert ("backend-log", "refused-telemetry") in outcomes()
    assert ("timing-log", "refused-telemetry") in outcomes()
    assert capsys.readouterr().err == ""


@pytest.mark.allow_real_subprocess  # exclusive holder child
def test_observe_mode_machine_id_creation_under_a_migration_records_and_proceeds(
    exclusive_holder, tmp_path
):
    d = tmp_path / "backlog"
    created = dev_status_storage.machine_id(d / "_machine_id", d)
    assert (d / "_machine_id").read_text() == created
    assert ("machine-id", "would-block") in outcomes()


# ── scope boundaries ─────────────────────────────────────────────────────────


def test_vitals_apply_reads_inside_the_scope_and_dry_run_outside(monkeypatch, tmp_path):
    import grill
    import vitals_promotion

    depths: list[int] = []
    real = grill.all_sessions

    def spy(*a, **k):
        depths.append(migration_lock._depth())
        return real(*a, **k)

    monkeypatch.setattr(grill, "all_sessions", spy)
    data = tmp_path / "grill"
    data.mkdir()
    monkeypatch.setattr(sys, "argv", ["vitals_promotion.py", "--data-dir", str(data), "--apply"])
    vitals_promotion.main()
    monkeypatch.setattr(sys, "argv", ["vitals_promotion.py", "--data-dir", str(data)])
    vitals_promotion.main()
    assert depths == [1, 0]


def test_ticket_batch_holds_one_scope_through_state_deletion(monkeypatch, tmp_path):
    import json

    import to_tickets_runner

    seen: list[int] = []
    real = to_tickets_runner.delete_state

    def spy(*a, **k):
        seen.append(migration_lock._depth())
        return real(*a, **k)

    monkeypatch.setattr(to_tickets_runner, "delete_state", spy)
    batch = tmp_path / "b.json"
    batch.write_text(json.dumps([{"id": "one-ticket", "summary": "x"}]))
    import dev_status

    d = tmp_path / "backlog"
    for name, value in {
        "DATA_DIR": d,
        "ITEMS_FILE": d / "items.json",
        "PENDING_FILE": d / "pending_items.json",
        "META_FILE": d / "_meta.json",
        "LOCK_FILE": d / ".backlog.lock",
        "JOURNAL_FILE": d / "journal.jsonl",
        "MACHINE_ID_FILE": d / "_machine_id",
    }.items():
        monkeypatch.setattr(dev_status, name, value)
    to_tickets_runner.run_batch(batch)
    assert seen and all(depth >= 1 for depth in seen)
    assert_all_released()


def test_grill_and_second_opinion_directory_creation_take_a_scope(monkeypatch, tmp_path):
    import grill
    import second_opinion

    sites: list[str] = []
    real = migration_lock.shared

    def spy(site, **kw):
        sites.append(site)
        return real(site, **kw)

    monkeypatch.setattr(migration_lock, "shared", spy)
    grill.ensure_data_dir(tmp_path / "g")
    monkeypatch.setattr(second_opinion, "DATA_DIR", tmp_path / "so")
    second_opinion.ensure_data_dir()
    assert sites == ["grill", "decisions"]
