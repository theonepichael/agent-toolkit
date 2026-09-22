#!/usr/bin/env python3
"""Test support: kill a child process at a named checkpoint, then judge recovery.

Pairs with the runtime hook in ``fault_checkpoint.py``. Exception injection
runs cleanup code that a killed process never runs, and some installer
fixtures stub ``os.fsync``; neither can show what a crash really leaves on
disk. This harness runs the code under test in a fresh interpreter, arms one
checkpoint, and requires the child to die there by SIGKILL.

A kill proves only what a crashed process fails to clean up. It does not
prove durability across power loss: the page cache survives a process kill.

Mandatory caller sequence, per checkpoint
-----------------------------------------
1. :func:`run_killed_at`.
2. Run recovery in a fresh process (or call) WITHOUT the fault variables;
   :func:`sandbox_env` builds that environment.
3. Get the decision recovery made (``retry``, ``unwind``, or ``none`` for a
   crash before the first journal record) from the on-disk state alone. A
   test adapter may read it from recovery's journal or output. Never pass the
   oracle row's ``expected`` into recovery or the adapter: that makes the
   check circular.
4. :func:`assert_recovery`.

:func:`check_oracle_complete` makes sure every declared checkpoint has
exactly one oracle row.

Child processes do not inherit this test sandbox's in-process write guards,
so every child runs under :func:`sandbox_env`: ``HOME``, the XDG directories,
and ``cwd`` all point into the given sandbox home, and bytecode writes are
off.

Standard library only, and never imports pytest, so direct ``python3``
test runs can use it. Callers under pytest need
``@pytest.mark.allow_real_subprocess``.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from fault_checkpoint import ENV_ARM, ENV_MARKER

Recovery = Literal["retry", "unwind", "none"]


@dataclass(frozen=True)
class OracleRow:
    """The one right recovered state for a crash at ``checkpoint``."""

    checkpoint: str
    expected: Recovery
    check: Callable[[Path], None]
    """Asserts the recovered state under a root; raises AssertionError."""


@dataclass(frozen=True)
class KillResult:
    checkpoint: str
    returncode: int
    reached: bool


def check_oracle_complete(declared: Sequence[str], table: Sequence[OracleRow]) -> None:
    """Raise AssertionError unless ``table`` has exactly one row per declared name."""
    problems: list[str] = []
    dup_declared = sorted(n for n, c in Counter(declared).items() if c > 1)
    if dup_declared:
        problems.append(f"duplicate declared checkpoints: {dup_declared}")
    rows = Counter(row.checkpoint for row in table)
    dup_rows = sorted(n for n, c in rows.items() if c > 1)
    if dup_rows:
        problems.append(f"duplicate oracle rows: {dup_rows}")
    missing = sorted(set(declared) - set(rows))
    if missing:
        problems.append(f"checkpoints with no oracle row: {missing}")
    extra = sorted(set(rows) - set(declared))
    if extra:
        problems.append(f"oracle rows for undeclared checkpoints: {extra}")
    if problems:
        raise AssertionError("; ".join(problems))


def assert_recovery(row: OracleRow, observed: Recovery, root: Path) -> None:
    """Check recovery's own decision against the row, then the recovered state."""
    if observed != row.expected:
        raise AssertionError(
            f"checkpoint {row.checkpoint}: recovery decided {observed!r}, "
            f"oracle expects {row.expected!r}"
        )
    row.check(root)


def sandbox_env(home: Path, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """A new child environment confined to ``home``, with no fault variables."""
    env = {k: v for k, v in os.environ.items() if k not in (ENV_ARM, ENV_MARKER)}
    env["HOME"] = str(home)
    env["XDG_STATE_HOME"] = str(home / ".local" / "state")
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    env["XDG_DATA_HOME"] = str(home / ".local" / "share")
    env["XDG_CACHE_HOME"] = str(home / ".cache")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if extra:
        env.update(extra)
    return env


def _kill_group(pgid: int) -> None:
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pgid, signal.SIGKILL)


def _tail(path: Path) -> str:
    try:
        return path.read_text(errors="replace")[-2000:]
    except OSError:
        return ""


def run_killed_at(
    checkpoint: str,
    script: Path,
    args: Sequence[str] = (),
    *,
    home: Path,
    declared: Sequence[str],
    extra_env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    timeout: float = 30.0,
) -> KillResult:
    """Run ``script`` with ``checkpoint`` armed and require it to die there.

    The harness builds argv itself (``sys.executable`` plus ``script``), so
    the checkpoint always runs in the direct child: the arm value's parent
    PID check and the SIGKILL return code both rely on that. The child gets
    its own session; every exit path kills the whole process group, so no
    descendant outlives the call.
    """
    if checkpoint not in declared:
        raise ValueError(f"checkpoint {checkpoint!r} is not declared: {list(declared)}")
    work = Path(tempfile.mkdtemp(prefix="fault-run-"))
    try:
        marker = work / "marker"
        out_path = work / "output"
        env = sandbox_env(home, extra_env)
        env[ENV_ARM] = f"{checkpoint}@{os.getpid()}"
        env[ENV_MARKER] = str(marker)
        with out_path.open("wb") as out:
            proc = subprocess.Popen(
                [sys.executable, str(script), *args],
                env=env,
                cwd=str(cwd if cwd is not None else home),
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                returncode = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                _kill_group(proc.pid)
                proc.wait()
                raise TimeoutError(
                    f"child timed out at checkpoint {checkpoint} after {timeout}s"
                ) from None
            finally:
                _kill_group(proc.pid)
                if proc.returncode is None:
                    proc.wait()
        output = _tail(out_path)
        if returncode == 0:
            raise AssertionError(
                f"armed checkpoint never reached: {checkpoint}\n{output}"
            )
        if returncode != -signal.SIGKILL:
            raise AssertionError(
                f"child exited {returncode}, not by SIGKILL at {checkpoint}\n{output}"
            )
        reached = marker.exists() and marker.read_text() == checkpoint
        if not reached:
            raise AssertionError(
                f"child was killed but left no marker for {checkpoint}\n{output}"
            )
        return KillResult(checkpoint=checkpoint, returncode=returncode, reached=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)
