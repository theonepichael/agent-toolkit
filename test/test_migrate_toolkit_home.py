#!/usr/bin/env python3
"""Tests for migrate_toolkit_home.py: preflight, dry run, write-ahead journal.

Every test builds a sandbox HOME (and XDG state dir) holding legacy toolkit
stores and a fake installed runtime, then runs the command in-process via
``migrate_toolkit_home.run`` (or install.py's parser for flag rules). The
fresh-process validation is stubbed to pass. The data phases, recovery by
the flip rule, and the crash tests live in test_migrate_toolkit_home_moves.py.
"""

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


@pytest.fixture(autouse=True)
def passing_validation(monkeypatch):
    """The sandbox has no installed runtime to validate against."""
    monkeypatch.setattr(mth, "_run_validation", lambda _ctx: (True, []))


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


def _legacy_stores(home: Path) -> Path:
    data = home / ".claude" / "data"
    backlog = data / "backlog"
    backlog.mkdir(parents=True)
    (backlog / "items.json").write_text(
        json.dumps({"schema_version": 2, "items": [{"id": "x-one"}]})
    )
    (backlog / "pending_items.json").write_text(
        json.dumps({"schema_version": 1, "items": []})
    )
    (backlog / "_meta.json").write_text(json.dumps({"rev": 3}))
    (backlog / "journal.jsonl").write_text('{"cmd": "add"}\n{"cmd": "done"}\n')
    (backlog / "_machine_id").write_text("0ee2ec8d")
    grill = data / "grill"
    grill.mkdir()
    (grill / "topic.json").write_text(json.dumps({"schema_version": 1, "slug": "topic"}))
    (grill / "topic-plan.md").write_text("# plan\n")
    (data / "backend_calls.jsonl").write_text('{"backend": "codex"}\n')
    (data / "unrelated-notes").mkdir()
    return data


@pytest.fixture
def machine(sandbox):
    _installed_runtime(sandbox)
    _legacy_stores(sandbox)
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


def _run(capsys, **kw: object) -> tuple[int, dict]:
    code = mth.run(_opts(**kw), repo_root=REPO)
    out = capsys.readouterr().out
    report = json.loads(out) if out.strip() else {}
    return code, report


def _tree(root: Path) -> dict[str, object]:
    """Every path under root with its type and content digest (for files)."""
    snap: dict[str, object] = {}
    if not root.exists():
        return snap
    for path in sorted(root.rglob("*")):
        info = path.lstat()
        rel = str(path.relative_to(root))
        if stat.S_ISLNK(info.st_mode):
            snap[rel] = ("link", os.readlink(path))
        elif stat.S_ISDIR(info.st_mode):
            snap[rel] = ("dir",)
        elif not stat.S_ISREG(info.st_mode):
            snap[rel] = ("special", stat.S_IFMT(info.st_mode))  # never open a FIFO
        else:
            snap[rel] = ("file", path.read_bytes(), stat.S_IMODE(info.st_mode))
    return snap


def _installer_state(home: Path) -> Path:
    return home / ".local" / "state" / "agent-toolkit"


def _journal_dirs(home: Path) -> list[Path]:
    root = _installer_state(home) / "migrations"
    return sorted(p for p in root.iterdir()) if root.is_dir() else []


def _records(journal_dir: Path) -> list[dict]:
    return mth.read_records(journal_dir)


def _findings(report: dict, status: str) -> set[str]:
    return {f["check"] for f in report["findings"] if f["status"] == status}


# ── flag rules (install.parse_args) ───────────────────────────────────────


def _parse_error(capsys, argv: list[str]) -> str:
    with pytest.raises(SystemExit) as exc:
        install.parse_args(argv)
    assert exc.value.code == 2
    return capsys.readouterr().err


@pytest.mark.regression(
    "migrate-toolkit-home-flag-missing",
    "ModuleNotFoundError: No module named 'migrate_toolkit_home'",
)
def test_migrate_flag_parses_with_its_modifiers():
    opts = install.parse_args(
        [
            "--migrate-toolkit-home",
            "--harness=claude,pi",
            "--dry-run",
            "--json",
            "--cross-filesystem",
            "--skip-reconciliation",
            "--migration-id=mig-20260923T120000Z-abcdef",
        ]
    )
    assert opts.migrate_toolkit_home
    assert opts.harnesses == ("claude", "pi")
    assert opts.dry_run and opts.json_report and opts.cross_filesystem
    assert opts.skip_reconciliation
    assert opts.migration_id == "mig-20260923T120000Z-abcdef"


def test_migrate_requires_harness(capsys):
    assert "no --harness specified" in _parse_error(capsys, ["--migrate-toolkit-home"])


@pytest.mark.parametrize(
    "extra",
    [
        "--rollback",
        "--wipe",
        "--force",
        "--reseed",
        "--adopt",
        "--force-harness",
    ],
)
def test_migrate_rejects_install_only_flags(capsys, extra):
    err = _parse_error(capsys, ["--migrate-toolkit-home", "--harness=claude", extra])
    assert "--migrate-toolkit-home cannot be combined" in err


def test_existing_messages_keep_precedence(capsys):
    err = _parse_error(capsys, ["--depart", "--migrate-toolkit-home"])
    assert "--depart must be used alone" in err
    err = _parse_error(capsys, ["--check-links", "--migrate-toolkit-home"])
    assert "--check-links must be used alone" in err
    err = _parse_error(capsys, ["--check-links", "--report-uninstalled", "--rollback"])
    assert "--check-links must be used alone" in err


def test_report_uninstalled_raw_flags_are_rejected_with_migrate(capsys):
    for flag in ("--report-uninstalled", "--no-report-uninstalled"):
        err = _parse_error(
            capsys, ["--migrate-toolkit-home", "--harness=claude", flag]
        )
        assert "--migrate-toolkit-home cannot be combined" in err


@pytest.mark.parametrize(
    "flag",
    ["--json", "--cross-filesystem", "--skip-reconciliation", "--migration-id=x"],
)
def test_migration_only_flags_need_the_command(capsys, flag):
    err = _parse_error(capsys, ["--harness=claude", flag])
    assert "can only be used with --migrate-toolkit-home" in err


def test_malformed_migration_id_is_an_argument_error(capsys):
    err = _parse_error(
        capsys, ["--migrate-toolkit-home", "--harness=claude", "--migration-id=bad"]
    )
    assert "invalid --migration-id" in err


def test_install_disables_bytecode_before_local_imports():
    source = (REPO / "install.py").read_text()
    guard = source.index("sys.dont_write_bytecode = True")
    first_local = source.index("import cli_common")
    assert guard < first_local


# ── dry run ─────────────────────────────────────────────────────────────────


@pytest.mark.regression("migrate-toolkit-home-command-missing", "ModuleNotFoundError: No module named 'migrate_toolkit_home'")
def test_dry_run_writes_nothing(machine, capsys, tmp_path, monkeypatch):
    xdg = tmp_path / "xdg-state"
    xdg.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg))
    before = (_tree(machine), _tree(xdg))
    code, report = _run(capsys, dry_run=True, skip_reconciliation=False)
    assert code == 0, report
    assert (_tree(machine), _tree(xdg)) == before
    assert report["dry_run"] is True
    assert "reconciliation" in _findings(report, "warn")
    assert "unclassified" in _findings(report, "warn")
    inv = report["inventory"]
    backlog = inv["domains"]["work-items"]
    assert {e["path"] for e in backlog["files"]} >= {"items.json", "journal.jsonl"}
    assert inv["domains"]["backend-log"]["files"][0]["path"] == "."


def test_dry_run_lock_probe_does_not_create_the_lock_file(machine, capsys):
    _run(capsys, dry_run=True)
    assert not migration_lock.lock_path().exists()


def test_dry_run_reports_a_busy_lock_as_warning(machine, capsys):
    path = migration_lock.lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_SH)
        code, report = _run(capsys, dry_run=True)
    finally:
        os.close(fd)
    assert code == 0
    assert "migration-lock" in _findings(report, "warn")


# ── real run ────────────────────────────────────────────────────────────────


def test_real_run_writes_journal_inventory_and_history(machine, capsys):
    code, report = _run(capsys)
    assert code == 0, report
    [jdir] = _journal_dirs(machine)
    assert jdir.name == report["migration_id"]
    assert sorted(p.name for p in jdir.iterdir()) == ["inventory.json", "journal.jsonl"]
    records = _records(jdir)
    assert records[-1]["event"] == "end"
    assert records[-1]["detail"]["outcome"] == "committed"
    inventory = jdir / "inventory.json"
    assert stat.S_IMODE(inventory.stat().st_mode) == 0o444
    assert json.loads(inventory.read_text())["migration_id"] == jdir.name
    history = (_installer_state(machine) / "history.jsonl").read_text().splitlines()
    entries = [json.loads(line) for line in history]
    migration = [e for e in entries if e.get("kind") == "migration"]
    assert [r["id"] for r in migration] == [jdir.name]


def test_real_run_on_a_fresh_machine_has_an_empty_inventory(sandbox, capsys):
    _installed_runtime(sandbox)
    code, report = _run(capsys)
    assert code == 0, report
    assert all(not d["files"] for d in report["inventory"]["domains"].values())


def test_journal_follows_home_not_xdg(machine, capsys, tmp_path, monkeypatch):
    xdg = tmp_path / "xdg-state"
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg))
    code, _ = _run(capsys)
    assert code == 0
    assert _journal_dirs(machine)
    assert not (xdg / "agent-toolkit" / "migrations").exists()


def test_busy_lock_exits_75_and_writes_no_journal(machine, capsys):
    path = migration_lock.lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    stale = _installer_state(machine) / "migrations" / "mig-20260101T000000Z-aaaaaa"
    stale.mkdir(parents=True)
    try:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_SH)
        code, _ = _run(capsys)
    finally:
        os.close(fd)
    assert code == migration_lock.REFUSAL_EXIT_CODE
    assert _journal_dirs(machine) == [stale]
    assert list(stale.iterdir()) == []  # not abandoned while the lock was busy


def test_non_blocking_exclusive_raises_busy(sandbox):
    path = migration_lock.lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_SH)
        with pytest.raises(migration_lock.MigrationLockBusy):
            with migration_lock.exclusive("test", blocking=False):
                pytest.fail("must not acquire")
    finally:
        os.close(fd)
    with migration_lock.exclusive("test", blocking=False):
        pass


# ── refusals: exit 1, nothing written ─────────────────────────────────────


def _assert_initial_refusal(machine, capsys, check: str, **kw: object) -> None:
    before = (_tree(machine), _tree(_installer_state(machine)))
    code, report = _run(capsys, **kw)
    assert code == 1, report
    assert check in _findings(report, "refuse"), report["findings"]
    assert (_tree(machine), _tree(_installer_state(machine))) == before


def test_refuses_when_already_migrated(machine, capsys):
    agent_toolkit_paths.write_pointer(machine, "toolkit-home")
    _assert_initial_refusal(machine, capsys, "layout")


def test_refuses_invalid_json(machine, capsys):
    (machine / ".claude/data/backlog/items.json").write_text("{not json")
    _assert_initial_refusal(machine, capsys, "schemas")


def test_refuses_unsupported_schema(machine, capsys):
    (machine / ".claude/data/backlog/items.json").write_text(
        json.dumps({"schema_version": 9, "items": []})
    )
    _assert_initial_refusal(machine, capsys, "schemas")


def test_refuses_corrupt_middle_jsonl_line(machine, capsys):
    (machine / ".claude/data/backlog/journal.jsonl").write_text('{"a": 1}\nnope\n{"b": 2}\n')
    _assert_initial_refusal(machine, capsys, "schemas")


def test_torn_final_jsonl_line_only_warns(machine, capsys):
    (machine / ".claude/data/backlog/journal.jsonl").write_text('{"a": 1}\n{"b": ')
    code, report = _run(capsys, dry_run=True)
    assert code == 0
    assert "schemas" in _findings(report, "warn")


def test_refuses_destination_collision(machine, capsys):
    dest = machine / ".agent-toolkit" / "data" / "grill"
    dest.mkdir(parents=True)
    (dest / "stray.json").write_text("{}")
    _assert_initial_refusal(machine, capsys, "collisions")


def test_refuses_toolkit_home_override(machine, capsys, monkeypatch, tmp_path):
    monkeypatch.setenv(agent_toolkit_paths.ENV_HOME, str(tmp_path / "elsewhere"))
    _assert_initial_refusal(machine, capsys, "toolkit-home-override")


def test_refuses_cross_device_unless_allowed(machine, capsys, monkeypatch):
    real = mth._device_of

    def fake(path: Path) -> int:
        dev = real(path)
        return dev + 1 if ".agent-toolkit" in str(path) or path == machine else dev

    monkeypatch.setattr(mth, "_device_of", fake)
    _assert_initial_refusal(machine, capsys, "same-device")
    code, report = _run(capsys, dry_run=True, cross_filesystem=True)
    assert code == 0
    assert "same-device" in _findings(report, "warn")


def test_refuses_runtime_that_is_not_lock_aware(machine, capsys):
    _installed_runtime(machine, enforce=False)
    _assert_initial_refusal(machine, capsys, "runtime-lock-aware")


def test_refuses_missing_installed_runtime(sandbox, capsys):
    _legacy_stores(sandbox)
    _assert_initial_refusal(sandbox, capsys, "runtime-lock-aware")


def test_personal_real_run_refuses_without_reconciliation_override(machine, capsys):
    _assert_initial_refusal(machine, capsys, "reconciliation", skip_reconciliation=False)


def test_work_profile_skips_reconciliation(machine, capsys):
    code, report = _run(capsys, profile="work", skip_reconciliation=False)
    assert code == 0, report
    assert "reconciliation" in _findings(report, "ok")


def test_refuses_fifo_without_opening_it(machine, capsys):
    os.mkfifo(machine / ".claude/data/backlog/pending_items.json.fifo")
    _assert_initial_refusal(machine, capsys, "inventory-types")


def test_fifo_named_like_a_store_does_not_block(machine, capsys):
    items = machine / ".claude/data/backlog/items.json"
    items.unlink()
    os.mkfifo(items)
    _assert_initial_refusal(machine, capsys, "inventory-types")


def test_refuses_symlinked_domain_root(machine, capsys, tmp_path):
    real = tmp_path / "real-standup"
    real.mkdir()
    (machine / ".claude/data/standup").symlink_to(real)
    _assert_initial_refusal(machine, capsys, "legacy-roots")


def test_refuses_file_blocking_destination(machine, capsys):
    (machine / ".agent-toolkit").mkdir()
    (machine / ".agent-toolkit" / "data").write_text("oops")
    _assert_initial_refusal(machine, capsys, "destination-ancestors")


def test_refuses_existing_migration_id(machine, capsys):
    mid = "mig-20260923T120000Z-abcdef"
    (_installer_state(machine) / "migrations" / mid).mkdir(parents=True)
    (_installer_state(machine) / "migrations" / mid / "journal.jsonl").write_text(
        json.dumps({"seq": 1, "event": "end", "detail": {"outcome": "x"}}) + "\n"
    )
    _assert_initial_refusal(machine, capsys, "migration-id", migration_id=mid)


def test_symlink_inside_a_domain_is_recorded_not_followed(machine, capsys, tmp_path):
    target = tmp_path / "outside.md"
    target.write_text("secret")
    (machine / ".claude/data/grill/link.md").symlink_to(target)
    code, report = _run(capsys, dry_run=True)
    assert code == 0
    entries = {e["path"]: e for e in report["inventory"]["domains"]["decisions"]["files"]}
    assert entries["link.md"] == {"path": "link.md", "symlink": str(target)}


# ── recovery ────────────────────────────────────────────────────────────────


def _crashed_journal(machine: Path, name: str, raw: bytes | None) -> Path:
    jdir = _installer_state(machine) / "migrations" / name
    jdir.mkdir(parents=True)
    if raw is not None:
        (jdir / "journal.jsonl").write_bytes(raw)
    return jdir


BEGIN = json.dumps({"seq": 1, "id": "x", "phase": "preflight", "event": "begin", "detail": {}})


@pytest.mark.parametrize(
    "tail",
    [b'{"seq": 2, "ev', '{"seq": 2, "detail": "café'.encode()[:-1]],
    ids=["torn-json", "cut-utf8"],
)
def test_torn_journal_tail_is_truncated_then_abandoned(machine, capsys, tail):
    jdir = _crashed_journal(
        machine, "mig-20260101T000000Z-aaaaaa", BEGIN.encode() + b"\n" + tail
    )
    code, report = _run(capsys)
    assert code == 0, report
    records = _records(jdir)
    assert [r["event"] for r in records] == ["begin", "abandoned"]
    assert (jdir / "journal.jsonl").read_bytes().endswith(b"\n")
    assert {"id": jdir.name, "action": "unwind"} in report["recovered"]


def test_empty_journal_dir_is_abandoned(machine, capsys):
    jdir = _crashed_journal(machine, "mig-20260101T000000Z-bbbbbb", None)
    code, report = _run(capsys)
    assert code == 0
    assert [r["event"] for r in _records(jdir)] == ["abandoned"]


def test_stray_temp_inventory_is_removed_before_abandon(machine, capsys):
    jdir = _crashed_journal(machine, "mig-20260101T000000Z-cccccc", BEGIN.encode() + b"\n")
    (jdir / "inventory.json.tmp123").write_text("{partial")
    code, _ = _run(capsys)
    assert code == 0
    assert not list(jdir.glob("inventory.json.tmp*"))
    assert _records(jdir)[-1]["event"] == "abandoned"


def test_terminal_journal_without_history_is_repaired_once(machine, capsys):
    end = json.dumps({"seq": 2, "id": "x", "phase": "run", "event": "end", "detail": {"outcome": "committed"}})
    jdir = _crashed_journal(
        machine, "mig-20260101T000000Z-dddddd", (BEGIN + "\n" + end + "\n").encode()
    )
    history = _installer_state(machine) / "history.jsonl"
    history.write_text('{"kind": "run", "timestamp": "t"}\n{"kind": "migr')  # torn tail
    code, report = _run(capsys)
    assert code == 0, report
    assert {"id": jdir.name, "action": "retry"} in report["recovered"]
    parsed = []
    for line in history.read_text().splitlines():
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    ids = [e["id"] for e in parsed if e.get("kind") == "migration"]
    assert ids.count(jdir.name) == 1
    _run(capsys)  # refused: the first run left the layout at toolkit-home
    parsed2 = [json.loads(line) for line in history.read_text().splitlines() if line.startswith("{\"kind\": \"migration\"") or '"kind": "migration"' in line and line.endswith("}")]
    assert [e["id"] for e in parsed2].count(jdir.name) == 1


def test_journal_refuses_a_second_terminal_record(sandbox):
    journal = mth.Journal.open(_installer_state(sandbox), mth.new_migration_id())
    journal.end("committed")
    with pytest.raises(mth.JournalError):
        journal.end("aborted")


def test_poisoned_journal_accepts_no_more_records(sandbox, monkeypatch):
    journal = mth.Journal.open(_installer_state(sandbox), mth.new_migration_id())
    real_write = os.write
    monkeypatch.setattr(os, "write", lambda fd, data: real_write(fd, data[:3]))
    with pytest.raises(mth.JournalError):
        journal.begin("preflight")
    monkeypatch.setattr(os, "write", real_write)
    with pytest.raises(mth.JournalError):
        journal.end("aborted")


# ── durability ordering ─────────────────────────────────────────────────────


def test_fsync_order_record_before_action(machine, capsys, monkeypatch):
    events: list[str] = []
    real_fsync = os.fsync

    def spy(fd: int) -> None:
        events.append("fsync:" + os.readlink(f"/proc/self/fd/{fd}"))
        real_fsync(fd)

    real_replace = os.replace

    def replace_spy(src, dst):
        events.append(f"replace:{dst}")
        real_replace(src, dst)

    monkeypatch.setattr(os, "fsync", spy)
    monkeypatch.setattr(os, "replace", replace_spy)
    code, report = _run(capsys)
    assert code == 0, report
    jdir = _journal_dirs(machine)[0]
    journal = str(jdir / "journal.jsonl")
    inventory = str(jdir / "inventory.json")
    first_journal_sync = events.index(f"fsync:{journal}")
    rename = events.index(f"replace:{inventory}")
    assert first_journal_sync < rename  # inventory "begin" is durable first
    after = events[rename + 1 :]
    assert f"fsync:{jdir}" in after  # dir fsynced after the rename
    assert f"fsync:{journal}" in after  # "done" recorded after the action
    assert f"fsync:{jdir.parent}" in events[:first_journal_sync]


@pytest.mark.regression(
    "lock-aware-check-misses-annotated-enforce",
    "the installed migration lock does not enforce (ENFORCE = None)",
)
def test_real_installed_lock_module_counts_as_lock_aware(sandbox, capsys):
    scripts = sandbox / ".claude" / "scripts"
    scripts.mkdir(parents=True)
    for name in ("migration_lock.py", "agent_toolkit_paths.py"):
        (scripts / name).symlink_to(REPO / "agent-scripts" / name)
    code, report = _run(capsys, dry_run=True)
    assert "runtime-lock-aware" in _findings(report, "ok"), report["findings"]
    assert code == 0
