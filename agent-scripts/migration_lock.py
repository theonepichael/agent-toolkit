#!/usr/bin/env python3
"""Machine-wide migration lock: writers share it, the toolkit-home migrator owns it.

Every Python write (or directory creation) inside a migrating data domain
enters ``shared(site)`` for the length of its operation. The release-1
migration takes ``exclusive(site)``. It is one ``flock`` file, so the kernel
releases it when a holder dies; there is no lease or expiry.

Stage 0a (this release): ``ENFORCE`` is False. A writer that would have been
blocked, or that cannot open or lock the file, records an observation and
proceeds without the lock; nothing is ever refused. Stage 0b flips
``ENFORCE``, after which the same cases raise ``MigrationLockBusy`` (CLIs
exit 75) and telemetry writers skip their append and record a
``refused-telemetry`` observation instead.

Ordering: the migration lock is always outermost. Store locks call
``note_store_lock_acquired()`` / ``note_store_lock_released()``; entering
``shared()`` or ``exclusive()`` while this thread holds a store lock and has
no admitted migration scope raises ``LockOrderError`` in every mode.

Scopes are re-entrant. Depth is per thread; the descriptor is process-wide,
reference-counted, and released when the last scope exits. ``exclusive()``
nested in ``shared()`` and any cross-thread mix of the two raise
``LockModeError`` immediately (the migrator is single-threaded by contract).
Not fork-safe: a child inherits the descriptor.

Files
  $XDG_STATE_HOME/agent-toolkit/migration.lock (default ~/.local/state):
    the lock file. Never inside a data root, never migrated.
  $XDG_STATE_HOME/agent-toolkit/migration-observations.jsonl: one JSONL
    record per observation {ts, event, site, outcome, detail, pid}, where
    outcome is would-block, lock-error, or refused-telemetry. If it cannot be
    written, one line goes to stderr instead.

Commands
  migration_lock.py status                 lock path, mode, exclusive holder, observation count
  migration_lock.py hold [--seconds N]     hold the lock exclusively for N seconds (default 60, max 600)
  migration_lock.py observations [--since ISO]   print recorded observations

Exit codes
  0 success; 1 usage error.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import cli_common

# What this module does with toolkit data (checked by scripts/check_toolkit_paths.py).
TOOLKIT_DATA = "infrastructure"

LOCK_RELPATH = Path("agent-toolkit") / "migration.lock"
OBSERVATIONS_RELPATH = Path("agent-toolkit") / "migration-observations.jsonl"
ENFORCE: bool = False
"""Stage 0a: observe only. Stage 0b flips this one constant."""
REFUSAL_EXIT_CODE = 75
"""EX_TEMPFAIL: what a CLI exits with when a write is refused (ENFORCE only)."""
MAX_HOLD_SECONDS = 600


class MigrationLockBusy(RuntimeError):
    """A write was refused: the migration lock is held or unusable (ENFORCE only)."""


class LockOrderError(RuntimeError):
    """The migration lock was requested inside a store lock (a code bug)."""


class LockModeError(RuntimeError):
    """An exclusive/shared mix this lock does not support."""


@dataclass
class _State:
    fd: int = -1
    mode: str | None = (
        None  # None | shared | shared-unlocked | acquiring-exclusive | exclusive
    )
    holders: int = 0  # admitted scopes across all threads
    owner: int | None = None  # thread ident holding or reserving exclusive


_mutex = threading.Lock()
_state = _State()
_tls = threading.local()


def _depth() -> int:
    return getattr(_tls, "depth", 0)


def _store_locks() -> int:
    return getattr(_tls, "store_locks", 0)


def _reset_for_tests() -> None:
    """Drop all bookkeeping (tests only; never call while a scope is open)."""
    global _state
    with _mutex:
        if _state.fd != -1:
            with suppress(OSError):
                os.close(_state.fd)
        _state = _State()
    _tls.depth = 0
    _tls.store_locks = 0


def lock_path() -> Path:
    return cli_common.state_dir() / LOCK_RELPATH


def observations_path() -> Path:
    return cli_common.state_dir() / OBSERVATIONS_RELPATH


def observe(site: str, outcome: str, detail: str, *, quiet: bool = False) -> None:
    """Record one observation; never raises and never takes the lock.

    If the observation cannot be written, one line goes to stderr, unless
    ``quiet`` (telemetry writers, which must never print).
    """
    try:
        record = {
            "ts": datetime.now(UTC).isoformat(),
            "event": "migration-lock-observe",
            "site": site,
            "outcome": outcome,
            "detail": detail,
            "pid": os.getpid(),
        }
        cli_common.append_jsonl(observations_path(), record, on_error="raise")
    except Exception as exc:
        if quiet:
            return
        with suppress(Exception):
            sys.stderr.write(
                f"[migration-lock] migration-lock observation lost ({site} "
                f"{outcome}: {detail}): {exc}\n"
            )


def note_store_lock_acquired() -> None:
    _tls.store_locks = _store_locks() + 1


def note_store_lock_released() -> None:
    _tls.store_locks = max(0, _store_locks() - 1)


def _check_order(site: str) -> None:
    if _store_locks() > 0 and _depth() == 0:
        raise LockOrderError(
            f"migration lock requested for {site!r} while a store lock is held; "
            "the migration lock must be taken first"
        )


def _release_locked() -> None:
    """Drop one admitted scope. Caller holds _mutex."""
    _state.holders -= 1
    if _state.holders == 0:
        if _state.fd != -1:
            with suppress(OSError):
                fcntl.flock(_state.fd, fcntl.LOCK_UN)
            with suppress(OSError):
                os.close(_state.fd)
        _state.fd = -1
        _state.mode = None
        _state.owner = None


def _open_lock_file() -> int:
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    return os.open(str(path), os.O_RDWR | os.O_CREAT, 0o644)


@contextmanager
def shared(site: str, *, quiet: bool = False) -> Iterator[None]:
    """Admit one writer scope for ``site``; see the module docstring.

    ``quiet`` suppresses the stderr fallback when an observation cannot be
    written (for telemetry writers, which must never print).
    """
    _check_order(site)
    me = threading.get_ident()
    with _mutex:
        if _state.mode in ("exclusive", "acquiring-exclusive"):
            if _state.owner != me:
                raise LockModeError(
                    f"shared scope for {site!r} requested from another thread while "
                    "this process holds or is acquiring the exclusive lock"
                )
        elif _state.holders == 0:
            fd = -1
            try:
                fd = _open_lock_file()
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                if fd != -1:
                    os.close(fd)
                if ENFORCE:
                    raise MigrationLockBusy(
                        f"{site}: the migration lock {lock_path()} is held "
                        "exclusively (a migration is running); retry when it finishes"
                    ) from None
                observe(
                    site,
                    "would-block",
                    "held exclusively by another process",
                    quiet=quiet,
                )
                _state.mode = "shared-unlocked"
            except OSError as exc:
                if fd != -1:
                    with suppress(OSError):
                        os.close(fd)
                if ENFORCE:
                    raise MigrationLockBusy(
                        f"{site}: cannot use the migration lock {lock_path()}: {exc}"
                    ) from exc
                observe(site, "lock-error", str(exc), quiet=quiet)
                _state.mode = "shared-unlocked"
            else:
                _state.fd = fd
                _state.mode = "shared"
        _state.holders += 1
    _tls.depth = _depth() + 1
    try:
        yield
    finally:
        _tls.depth = _depth() - 1
        with _mutex:
            _release_locked()


@contextmanager
def exclusive(site: str) -> Iterator[None]:
    """Hold the lock exclusively (the migrator). Blocks until all writers leave."""
    _check_order(site)
    me = threading.get_ident()
    reentrant = False
    with _mutex:
        if _state.mode in ("exclusive", "acquiring-exclusive") and _state.owner == me:
            _state.holders += 1
            reentrant = True
        elif _state.holders > 0:
            raise LockModeError(
                f"exclusive lock for {site!r} requested while a shared scope or "
                "another thread's exclusive lock is active in this process"
            )
        else:
            _state.mode = "acquiring-exclusive"
            _state.owner = me
            _state.holders = 1
    if not reentrant:
        fd = -1
        try:
            fd = _open_lock_file()
            fcntl.flock(fd, fcntl.LOCK_EX)
        except BaseException:
            if fd != -1:
                with suppress(OSError):
                    os.close(fd)
            with _mutex:
                _release_locked()
            raise
        with _mutex:
            _state.fd = fd
            _state.mode = "exclusive"
    _tls.depth = _depth() + 1
    try:
        yield
    finally:
        _tls.depth = _depth() - 1
        with _mutex:
            _release_locked()


# ── CLI ──────────────────────────────────────────────────────────────────────


def _clamp_hold_seconds(seconds: float) -> float:
    return max(0.0, min(float(seconds), float(MAX_HOLD_SECONDS)))


def _exclusive_holder_present() -> bool:
    try:
        fd = _open_lock_file()
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _load_observations() -> list[dict[str, object]]:
    path = observations_path()
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return []
    out: list[dict[str, object]] = []
    for line in lines:
        with suppress(json.JSONDecodeError):
            record = json.loads(line)
            if isinstance(record, dict):
                out.append(record)
    return out


def cmd_status(args: argparse.Namespace) -> int:
    print(f"lock: {lock_path()}")
    print(f"mode: {'enforce' if ENFORCE else 'observe'}")
    print(f"exclusive holder: {'yes' if _exclusive_holder_present() else 'none'}")
    print(f"observations: {len(_load_observations())} ({observations_path()})")
    return 0


def cmd_hold(args: argparse.Namespace) -> int:
    seconds = _clamp_hold_seconds(args.seconds)
    with exclusive("manual-hold"):
        print(f"holding {lock_path()} exclusively for {seconds:g}s", flush=True)
        time.sleep(seconds)
    print("released", flush=True)
    return 0


def cmd_observations(args: argparse.Namespace) -> int:
    for record in _load_observations():
        if args.since and str(record.get("ts", "")) < args.since:
            continue
        print(
            f"{record.get('ts')}  {record.get('outcome')}  {record.get('site')}  "
            f"pid={record.get('pid')}  {record.get('detail')}"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="migration_lock.py",
        description="Inspect or hold the machine-wide migration lock.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="show lock path, mode, holder, observation count")
    p = sub.add_parser(
        "hold", help="hold the lock exclusively for N seconds (soak testing)"
    )
    p.add_argument(
        "--seconds", type=float, default=60.0, help="seconds to hold (max 600)"
    )
    p = sub.add_parser("observations", help="print recorded observations")
    p.add_argument("--since", help="only observations at or after this ISO timestamp")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {
        "status": cmd_status,
        "hold": cmd_hold,
        "observations": cmd_observations,
    }
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
