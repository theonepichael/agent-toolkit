#!/usr/bin/env python3
"""Tests for fault_checkpoint.py and the test_fault_injection harness.

A toy two-phase writer stands in for the real migrator: it journals intent
and completion records around each action, with a checkpoint at every
boundary, including the ambiguous one (action done, completion not yet
recorded). Its toy recovery decides from disk alone.
"""

import json
import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent-scripts"))

import fault_checkpoint  # noqa: E402
import test_fault_injection as fi  # noqa: E402

AGENT_SCRIPTS = str(Path(__file__).resolve().parent.parent / "agent-scripts")
PYPATH = {"PYTHONPATH": AGENT_SCRIPTS}

TOY_WRITER = textwrap.dedent(
    """
    import atexit, json, os, sys
    from pathlib import Path
    from fault_checkpoint import checkpoint

    root = Path(sys.argv[1])
    journal = root / "journal.jsonl"

    def record(entry):
        with journal.open("a") as f:
            f.write(json.dumps(entry) + "\\n")
            f.flush()
            os.fsync(f.fileno())

    atexit.register(lambda: (root / "atexit.txt").write_text("ran"))
    try:
        checkpoint("toy.start")
        record({"step": "a", "phase": "intent"})
        (root / "a.txt").write_text("a")
        checkpoint("toy.a.acted")
        record({"step": "a", "phase": "done"})
        checkpoint("toy.a.done")
        record({"step": "b", "phase": "intent"})
        checkpoint("toy.b.intent")
        (root / "b.txt").write_text("b")
        record({"step": "b", "phase": "done"})
        record({"step": "commit"})
        checkpoint("toy.committed")
    finally:
        (root / "cleanup.txt").write_text("ran")
    """
)

TOY_RECOVER = textwrap.dedent(
    """
    import json, sys
    from pathlib import Path

    root = Path(sys.argv[1])
    lie = len(sys.argv) > 2 and sys.argv[2] == "--wrong"
    journal = root / "journal.jsonl"
    entries = []
    if journal.exists():
        entries = [json.loads(l) for l in journal.read_text().splitlines() if l]
    done = {e["step"] for e in entries if e.get("phase") == "done"}
    if not entries or {"step": "commit"} in entries:
        decision = "none"
    elif "a" not in done:
        # Intent without completion: the action may or may not have run.
        (root / "a.txt").unlink(missing_ok=True)
        journal.unlink()
        decision = "unwind"
    else:
        (root / "b.txt").write_text("b")
        with journal.open("a") as f:
            f.write(json.dumps({"step": "b", "phase": "done"}) + "\\n")
            f.write(json.dumps({"step": "commit"}) + "\\n")
        decision = "retry"
    if lie:
        decision = "retry" if decision != "retry" else "unwind"
    print(decision)
    """
)

DECLARED = ["toy.start", "toy.a.acted", "toy.a.done", "toy.b.intent", "toy.committed"]


def _nothing(root: Path) -> None:
    assert not (root / "a.txt").exists()
    assert not (root / "b.txt").exists()


def _both(root: Path) -> None:
    assert (root / "a.txt").read_text() == "a"
    assert (root / "b.txt").read_text() == "b"
    lines = (root / "journal.jsonl").read_text().splitlines()
    assert json.loads(lines[-1]) == {"step": "commit"}


ORACLE = [
    fi.OracleRow("toy.start", "none", _nothing),
    fi.OracleRow("toy.a.acted", "unwind", _nothing),
    fi.OracleRow("toy.a.done", "retry", _both),
    fi.OracleRow("toy.b.intent", "retry", _both),
    fi.OracleRow("toy.committed", "none", _both),
]


@pytest.fixture
def toy(tmp_path: Path) -> dict[str, Path]:
    home = tmp_path / "home"
    root = tmp_path / "root"
    home.mkdir()
    root.mkdir()
    writer = tmp_path / "toy_writer.py"
    recover = tmp_path / "toy_recover.py"
    writer.write_text(TOY_WRITER)
    recover.write_text(TOY_RECOVER)
    return {"home": home, "root": root, "writer": writer, "recover": recover}


def _recover(toy: dict[str, Path], *extra: str) -> str:
    result = subprocess.run(
        [sys.executable, str(toy["recover"]), str(toy["root"]), *extra],
        env=fi.sandbox_env(toy["home"], PYPATH),
        cwd=toy["home"],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _process_gone(pid: int) -> bool:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return True
    return stat.rsplit(")", 1)[1].split()[0] == "Z"


# ── fault_checkpoint.checkpoint ───────────────────────────────────────────────


@pytest.mark.parametrize("bad", ["", "Upper", "-lead", "has space", "a/b"])
def test_checkpoint_rejects_malformed_name_even_unarmed(monkeypatch, bad):
    monkeypatch.delenv(fault_checkpoint.ENV_ARM, raising=False)
    with pytest.raises(ValueError, match="invalid checkpoint name"):
        fault_checkpoint.checkpoint(bad)


@pytest.mark.parametrize(
    "arm",
    [
        None,
        "other.name@{ppid}",
        "toy.x@1",  # wrong parent pid
        "toy.x",  # malformed: no pid
        "toy.x@notapid",
    ],
)
def test_checkpoint_does_not_fire_unless_armed_for_this_parent(monkeypatch, arm):
    if arm is None:
        monkeypatch.delenv(fault_checkpoint.ENV_ARM, raising=False)
    else:
        monkeypatch.setenv(fault_checkpoint.ENV_ARM, arm.format(ppid=os.getppid()))
    killed = []
    monkeypatch.setattr(os, "kill", lambda *a: killed.append(a))
    fault_checkpoint.checkpoint("toy.x")
    assert killed == []


def test_checkpoint_fires_when_armed_for_this_parent(monkeypatch, tmp_path):
    marker = tmp_path / "marker"
    monkeypatch.setenv(fault_checkpoint.ENV_ARM, f"toy.x@{os.getppid()}")
    monkeypatch.setenv(fault_checkpoint.ENV_MARKER, str(marker))
    killed = []
    monkeypatch.setattr(os, "kill", lambda *a: killed.append(a))
    fault_checkpoint.checkpoint("toy.x")
    assert killed == [(os.getpid(), signal.SIGKILL)]
    assert marker.read_text() == "toy.x"


def test_checkpoint_still_kills_when_marker_write_fails(monkeypatch, tmp_path):
    monkeypatch.setenv(fault_checkpoint.ENV_ARM, f"toy.x@{os.getppid()}")
    monkeypatch.setenv(fault_checkpoint.ENV_MARKER, str(tmp_path / "no" / "dir" / "m"))
    killed = []
    monkeypatch.setattr(os, "kill", lambda *a: killed.append(a))
    fault_checkpoint.checkpoint("toy.x")
    assert killed == [(os.getpid(), signal.SIGKILL)]


# ── oracle table ──────────────────────────────────────────────────────────────


def test_oracle_for_toy_writer_is_complete():
    fi.check_oracle_complete(DECLARED, ORACLE)


@pytest.mark.parametrize(
    ("declared", "table", "message"),
    [
        (DECLARED, ORACLE[1:], "no oracle row: ['toy.start']"),
        (DECLARED[1:], ORACLE, "undeclared checkpoints: ['toy.start']"),
        (DECLARED, [*ORACLE, ORACLE[0]], "duplicate oracle rows: ['toy.start']"),
        ([*DECLARED, "toy.start"], ORACLE, "duplicate declared checkpoints"),
    ],
)
def test_oracle_completeness_rejects_bad_tables(declared, table, message):
    with pytest.raises(AssertionError, match=message.replace("[", r"\[")):
        fi.check_oracle_complete(declared, table)


# ── harness, end to end ───────────────────────────────────────────────────────


@pytest.mark.allow_real_subprocess
@pytest.mark.parametrize("row", ORACLE, ids=lambda r: r.checkpoint)
def test_crash_at_each_checkpoint_recovers_to_the_oracle_state(toy, row):
    result = fi.run_killed_at(
        row.checkpoint,
        toy["writer"],
        [str(toy["root"])],
        home=toy["home"],
        declared=DECLARED,
        extra_env=PYPATH,
    )
    assert result.returncode == -signal.SIGKILL
    assert result.reached
    # No unwinding ran in the killed child.
    assert not (toy["root"] / "cleanup.txt").exists()
    assert not (toy["root"] / "atexit.txt").exists()
    fi.assert_recovery(row, _recover(toy), toy["root"])


@pytest.mark.allow_real_subprocess
def test_wrong_recovery_decision_fails_the_oracle(toy):
    row = ORACLE[1]  # toy.a.acted -> unwind
    fi.run_killed_at(
        row.checkpoint,
        toy["writer"],
        [str(toy["root"])],
        home=toy["home"],
        declared=DECLARED,
        extra_env=PYPATH,
    )
    with pytest.raises(AssertionError, match="recovery decided 'retry'"):
        fi.assert_recovery(row, _recover(toy, "--wrong"), toy["root"])


def test_undeclared_checkpoint_is_refused_before_spawning(toy):
    with pytest.raises(ValueError, match="not declared"):
        fi.run_killed_at(
            "toy.nope", toy["writer"], home=toy["home"], declared=DECLARED
        )


@pytest.mark.allow_real_subprocess
def test_unreached_checkpoint_fails_loudly(toy, tmp_path):
    script = tmp_path / "never.py"
    script.write_text("print('done')\n")
    with pytest.raises(AssertionError, match="never reached: toy.start"):
        fi.run_killed_at("toy.start", script, home=toy["home"], declared=DECLARED)


@pytest.mark.allow_real_subprocess
def test_other_exit_fails_loudly(toy, tmp_path):
    script = tmp_path / "boom.py"
    script.write_text("raise SystemExit(3)\n")
    with pytest.raises(AssertionError, match="exited 3, not by SIGKILL"):
        fi.run_killed_at("toy.start", script, home=toy["home"], declared=DECLARED)


GRANDCHILD = textwrap.dedent(
    """
    import subprocess, sys, time
    from pathlib import Path
    from fault_checkpoint import checkpoint

    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    Path(sys.argv[1]).write_text(str(p.pid))
    if sys.argv[2] == "hang":
        time.sleep(60)
    checkpoint("toy.start")
    """
)


@pytest.mark.allow_real_subprocess
@pytest.mark.parametrize("mode", ["kill", "hang"])
def test_no_descendant_survives(toy, tmp_path, mode):
    script = tmp_path / "grandchild.py"
    script.write_text(GRANDCHILD)
    pidfile = tmp_path / "grandchild.pid"
    call = lambda: fi.run_killed_at(  # noqa: E731
        "toy.start",
        script,
        [str(pidfile), mode],
        home=toy["home"],
        declared=DECLARED,
        extra_env=PYPATH,
        timeout=3.0,
    )
    started = time.monotonic()
    if mode == "hang":
        with pytest.raises(TimeoutError, match="timed out at checkpoint toy.start"):
            call()
    else:
        call()
    # The harness must kill, not wait out, a stalled child (it sleeps 60s).
    assert time.monotonic() - started < 20
    pid = int(pidfile.read_text())
    deadline = time.monotonic() + 5
    while not _process_gone(pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _process_gone(pid)


ENV_PROBE = textwrap.dedent(
    """
    import json, os, sys
    from pathlib import Path
    from fault_checkpoint import checkpoint

    keys = ["HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
            "XDG_CACHE_HOME", "PYTHONDONTWRITEBYTECODE"]
    data = {k: os.environ.get(k) for k in keys}
    data["cwd"] = os.getcwd()
    Path(sys.argv[1]).write_text(json.dumps(data))
    checkpoint("toy.start")
    """
)


@pytest.mark.allow_real_subprocess
def test_child_runs_confined_to_the_sandbox_home(toy, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", "/somewhere/real")
    script = tmp_path / "probe.py"
    script.write_text(ENV_PROBE)
    out = tmp_path / "env.json"
    caller_env = dict(PYPATH)
    fi.run_killed_at(
        "toy.start",
        script,
        [str(out)],
        home=toy["home"],
        declared=DECLARED,
        extra_env=caller_env,
    )
    seen = json.loads(out.read_text())
    home = str(toy["home"])
    assert seen["HOME"] == home
    assert seen["cwd"] == str(toy["home"].resolve())
    for key in ("XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        assert seen[key].startswith(home + os.sep), key
    assert seen["PYTHONDONTWRITEBYTECODE"] == "1"
    # The caller's mapping and this process's env are untouched.
    assert caller_env == PYPATH
    assert fault_checkpoint.ENV_ARM not in os.environ


def test_sandbox_env_drops_inherited_fault_vars(monkeypatch, tmp_path):
    monkeypatch.setenv(fault_checkpoint.ENV_ARM, "toy.start@1")
    monkeypatch.setenv(fault_checkpoint.ENV_MARKER, "/stale")
    env = fi.sandbox_env(tmp_path)
    assert fault_checkpoint.ENV_ARM not in env
    assert fault_checkpoint.ENV_MARKER not in env
