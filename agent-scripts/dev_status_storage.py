"""Backlog persistence, lock coordination, and journal primitives.

This module encapsulates atomic file writes, locking (flock + re-entrant
thread lock), revision counter operations, run evidence records, and
journal append/read primitives.  Functions accept explicit paths or fall
back dynamically to module defaults, respecting call-time patches to
dev_status / dev_status_impl globals for complete backwards compatibility.
"""

from __future__ import annotations

import fcntl
import json
import os
import secrets
import sys
import tempfile
import threading
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import NotRequired, TypedDict, cast

import cli_common

DATA_DIR = Path.home() / ".claude" / "data" / "backlog"
ITEMS_FILE = DATA_DIR / "items.json"
PENDING_FILE = DATA_DIR / "pending_items.json"
META_FILE = DATA_DIR / "_meta.json"
LOCK_FILE = DATA_DIR / ".backlog.lock"
JOURNAL_FILE = DATA_DIR / "journal.jsonl"
RUNS_FILE = DATA_DIR / "runs.jsonl"
MACHINE_ID_FILE = DATA_DIR / "_machine_id"
RECAP_CACHE_FILE = DATA_DIR / "recap-cache.json"
RECAP_REGEN_LOCK_FILE = DATA_DIR / "recap-regen.lock"

OUT_OF_SCOPE_DIR = Path.home() / ".claude" / "data" / "backlog-out-of-scope"
OUT_OF_SCOPE_INDEX_FILE = OUT_OF_SCOPE_DIR / "index.json"
OUT_OF_SCOPE_LOCK_FILE = OUT_OF_SCOPE_DIR / ".out-of-scope.lock"


def _resolve_path(var_name: str, fallback: Path) -> Path:
    """Resolve a path dynamically from dev_status_impl if present, else fallback."""
    impl = sys.modules.get("dev_status_impl")
    if impl is not None and hasattr(impl, var_name):
        val = getattr(impl, var_name)
        if isinstance(val, (Path, str)):
            return Path(val)
    curr = globals().get(var_name)
    if isinstance(curr, (Path, str)):
        return Path(curr)
    return fallback


# ── data model ───────────────────────────────────────────────────────────────


class Gate(TypedDict):
    """A judgment-step verification checkpoint on a backlog item."""

    required: bool
    criteria: list[str]
    passed_at: str | None
    set_at: NotRequired[str]
    passed_via: NotRequired[str]
    coverage: NotRequired[dict[str, dict[str, str]]]


class RunRecord(TypedDict):
    """One recorded command execution — a row of the runs.jsonl sidecar."""

    run_id: str
    item: str
    command: str
    exit_code: int | None
    timed_out: bool
    started_at: str
    duration_s: float
    cwd: str


class BacklogItem(TypedDict):
    """A single backlog item as stored in items.json (schema v2)."""

    id: str
    created: str
    updated: str
    status: str
    summary: str
    category: str
    blocked_by: list[str]
    related_files: list[dict[str, object]]
    context: str
    next_steps: str
    priority: NotRequired[str]
    completed_at: NotRequired[str]
    review_feedback: NotRequired[str]
    review_content_hash: NotRequired[str]
    gate: NotRequired[Gate]


class PendingItem(TypedDict):
    """A single waiting-on-someone-else item as stored in pending_items.json."""

    id: str
    created: str
    updated: str
    status: str
    description: str
    kind: str
    source_ref: dict[str, object]
    context: str
    next_steps: list[str]
    blocking: list[str]
    outcome: str | None
    resolved_at: NotRequired[str]


type BacklogIndex = dict[str, BacklogItem]


# ── machine id ───────────────────────────────────────────────────────────────


def machine_id(
    machine_id_file: Path | None = None, data_dir: Path | None = None
) -> str:
    """Return this machine's stable short id, creating it on first use."""
    mid_file = machine_id_file or _resolve_path("MACHINE_ID_FILE", MACHINE_ID_FILE)
    d_dir = data_dir or _resolve_path("DATA_DIR", DATA_DIR)
    try:
        existing = mid_file.read_text().strip()
        if existing:
            return existing
    except OSError:
        pass
    new_id = secrets.token_hex(4)
    try:
        d_dir.mkdir(parents=True, exist_ok=True)
        mid_file.write_text(new_id)
    except OSError:
        pass
    return new_id


_machine_id = machine_id


# ── atomic file operations ───────────────────────────────────────────────────


def atomic_write_json(path: Path, payload: str, prefix: str) -> None:
    """Write text to ``path`` via a temp file in its directory + ``os.replace``.

    Cleans up the temp file on failure so a crash mid-write never leaves
    debris behind or corrupts the destination. Ensures the temp file is
    fsynced before rename and the containing directory is fsynced after.
    """
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=prefix)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())

        os.replace(tmp_path, path)

        dir_fd = None
        try:
            dir_fd = os.open(
                str(directory), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            os.fsync(dir_fd)
        finally:
            if dir_fd is not None:
                os.close(dir_fd)
    except Exception:
        with suppress(OSError):
            os.unlink(tmp_path)
        raise


_atomic_write_json = atomic_write_json


def backup_before_bulk_delete(path: Path) -> None:
    """Snapshot a data file before a filter-based bulk deletion."""
    if not path.exists():
        return
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    backup_path = path.with_name(f"{path.stem}.bak-{stamp}{path.suffix}")
    backup_path.write_bytes(path.read_bytes())


_backup_before_bulk_delete = backup_before_bulk_delete


# ── item & pending persistence ──────────────────────────────────────────────


def load_items(path: Path | None = None) -> list[BacklogItem]:
    """Load all backlog items from ``path`` (defaults to :data:`ITEMS_FILE`)."""
    target = path or _resolve_path("ITEMS_FILE", ITEMS_FILE)
    if not target.exists():
        return []
    try:
        data = json.loads(target.read_text())
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
        print(
            f"backlog file corrupted at {target}; restore from backup. ({e})",
            file=sys.stderr,
        )
        sys.exit(1)
    if not isinstance(data, dict) or data.get("schema_version") != 2:
        print(
            f"backlog file at {target} is not schema_version 2; "
            "check file or run migration.",
            file=sys.stderr,
        )
        sys.exit(1)
    return cast(list[BacklogItem], data.get("items", []))


def save_items(items: list[BacklogItem], path: Path | None = None) -> None:
    """Atomically persist ``items`` to ``path`` (defaults to :data:`ITEMS_FILE`)."""
    target = path or _resolve_path("ITEMS_FILE", ITEMS_FILE)
    payload = json.dumps({"schema_version": 2, "items": items}, indent=2)
    atomic_write_json(target, payload, ".items_tmp_")


def load_pending(path: Path | None = None) -> list[PendingItem]:
    """Load all pending items from ``path`` (defaults to :data:`PENDING_FILE`)."""
    target = path or _resolve_path("PENDING_FILE", PENDING_FILE)
    if not target.exists():
        return []
    try:
        data = json.loads(target.read_text())
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
        print(
            f"pending-items file corrupted at {target}; restore from backup. ({e})",
            file=sys.stderr,
        )
        sys.exit(1)
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        print(
            f"pending-items file at {target} is not schema_version 1; "
            "check file or run migration.",
            file=sys.stderr,
        )
        sys.exit(1)
    return cast(list[PendingItem], data.get("items", []))


def save_pending(pending_items: list[PendingItem], path: Path | None = None) -> None:
    """Atomically persist ``pending_items`` to ``path`` (defaults to :data:`PENDING_FILE`)."""
    target = path or _resolve_path("PENDING_FILE", PENDING_FILE)
    payload = json.dumps({"schema_version": 1, "items": pending_items}, indent=2)
    atomic_write_json(target, payload, ".pending_tmp_")


# ── revision management ──────────────────────────────────────────────────────


def load_rev(meta_file: Path | None = None) -> int:
    """Read the current revision counter."""
    m_file = meta_file or _resolve_path("META_FILE", META_FILE)
    if not m_file.exists():
        return 0
    try:
        data = json.loads(m_file.read_text())
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return 0
    if not isinstance(data, dict):
        return 0
    rev = data.get("rev", 0)
    if not isinstance(rev, int) or isinstance(rev, bool):
        return 0
    return rev


def bump_rev(meta_file: Path | None = None) -> int:
    """Increment and persist the revision counter.

    Must be called while holding :func:`backlog_lock`.
    """
    m_file = meta_file or _resolve_path("META_FILE", META_FILE)
    rev = load_rev(m_file) + 1
    payload = json.dumps({"rev": rev})
    atomic_write_json(m_file, payload, ".meta_tmp_")
    return rev


# ── locking ──────────────────────────────────────────────────────────────────


_backlog_lock_rlock = threading.RLock()
_backlog_lock_fd: int = -1
_backlog_lock_count: int = 0

_BACKLOG_LOCK_WAIT_JOURNAL_THRESHOLD_SECONDS = 0.5


@contextmanager
def backlog_lock(
    data_dir: Path | None = None,
    lock_file: Path | None = None,
) -> Iterator[None]:
    """Hold an exclusive lock over a mutating command's full read-modify-write cycle.

    Safe to re-enter from the same thread: the flock is taken once on the
    outermost entry and released on innermost exit.
    """
    global _backlog_lock_fd, _backlog_lock_count
    d_dir = data_dir or _resolve_path("DATA_DIR", DATA_DIR)
    l_file = lock_file or _resolve_path("LOCK_FILE", LOCK_FILE)
    d_dir.mkdir(parents=True, exist_ok=True)
    with _backlog_lock_rlock:
        _backlog_lock_count += 1
        if _backlog_lock_count == 1:
            _backlog_lock_fd = os.open(str(l_file), os.O_WRONLY | os.O_CREAT, 0o644)
            wait_start = time.monotonic()
            fcntl.flock(_backlog_lock_fd, fcntl.LOCK_EX)
            wait_seconds = time.monotonic() - wait_start
            if wait_seconds > _BACKLOG_LOCK_WAIT_JOURNAL_THRESHOLD_SECONDS:
                append_journal_event(
                    journal_entry(
                        "lock-wait",
                        "backlog",
                        load_rev(),
                        wait_seconds=round(wait_seconds, 3),
                        diagnostic=True,
                    )
                )
        try:
            yield
        finally:
            _backlog_lock_count -= 1
            if _backlog_lock_count == 0:
                fcntl.flock(_backlog_lock_fd, fcntl.LOCK_UN)
                os.close(_backlog_lock_fd)
                _backlog_lock_fd = -1


_out_of_scope_lock_rlock = threading.RLock()
_out_of_scope_lock_fd: int = -1
_out_of_scope_lock_count: int = 0


@contextmanager
def out_of_scope_lock(
    out_of_scope_dir: Path | None = None,
    lock_file: Path | None = None,
) -> Iterator[None]:
    """Hold an exclusive lock over an out-of-scope command's cycle."""
    global _out_of_scope_lock_fd, _out_of_scope_lock_count
    oos_dir = out_of_scope_dir or _resolve_path("OUT_OF_SCOPE_DIR", OUT_OF_SCOPE_DIR)
    l_file = lock_file or _resolve_path(
        "OUT_OF_SCOPE_LOCK_FILE", OUT_OF_SCOPE_LOCK_FILE
    )
    oos_dir.mkdir(parents=True, exist_ok=True)
    with _out_of_scope_lock_rlock:
        _out_of_scope_lock_count += 1
        if _out_of_scope_lock_count == 1:
            _out_of_scope_lock_fd = os.open(
                str(l_file), os.O_WRONLY | os.O_CREAT, 0o644
            )
            fcntl.flock(_out_of_scope_lock_fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            _out_of_scope_lock_count -= 1
            if _out_of_scope_lock_count == 0:
                fcntl.flock(_out_of_scope_lock_fd, fcntl.LOCK_UN)
                os.close(_out_of_scope_lock_fd)
                _out_of_scope_lock_fd = -1


# ── out-of-scope storage ─────────────────────────────────────────────────────


def load_out_of_scope_index(path: Path | None = None) -> dict[str, dict[str, object]]:
    """Load the out-of-scope concept index, or ``{}`` if it doesn't exist yet."""
    target = path or _resolve_path("OUT_OF_SCOPE_INDEX_FILE", OUT_OF_SCOPE_INDEX_FILE)
    if not target.exists():
        return {}
    return cast(dict[str, dict[str, object]], json.loads(target.read_text()))


_load_out_of_scope_index = load_out_of_scope_index


def save_out_of_scope_index(
    index: dict[str, dict[str, object]], path: Path | None = None
) -> None:
    """Atomically persist the out-of-scope concept index."""
    target = path or _resolve_path("OUT_OF_SCOPE_INDEX_FILE", OUT_OF_SCOPE_INDEX_FILE)
    payload = json.dumps(index, indent=2)
    atomic_write_json(target, payload, ".oos_index_tmp_")


_save_out_of_scope_index = save_out_of_scope_index


def out_of_scope_md_path(slug: str, out_of_scope_dir: Path | None = None) -> Path:
    """Path to a concept's freeform-reason markdown file."""
    target_dir = out_of_scope_dir or _resolve_path("OUT_OF_SCOPE_DIR", OUT_OF_SCOPE_DIR)
    return target_dir / f"{slug}.md"


_out_of_scope_md_path = out_of_scope_md_path


# ── event journal ────────────────────────────────────────────────────────────


def journal_entry(
    cmd: str,
    kind: str,
    rev: int,
    *,
    slug: str | None = None,
    summary: str | None = None,
    from_status: str | None = None,
    to_status: str | None = None,
    fields: list[str] | None = None,
    feedback: str | None = None,
    count: int | None = None,
    wait_seconds: float | None = None,
    detail: str | None = None,
    diagnostic: bool | None = None,
) -> dict[str, object]:
    """Build one journal entry: a fixed envelope plus structured optionals."""
    entry: dict[str, object] = {
        "ts": datetime.now(UTC).isoformat(),
        "rev": rev,
        "machine": machine_id(),
        "cmd": cmd,
        "kind": kind,
    }
    if slug is not None:
        entry["slug"] = slug
    if summary is not None:
        entry["summary"] = summary
    if from_status is not None:
        entry["from_status"] = from_status
    if to_status is not None:
        entry["to_status"] = to_status
    if fields is not None:
        entry["fields"] = fields
    if feedback is not None:
        entry["feedback"] = feedback
    if count is not None:
        entry["count"] = count
    if wait_seconds is not None:
        entry["wait_seconds"] = wait_seconds
    if detail is not None:
        entry["detail"] = detail
    if diagnostic is not None:
        entry["diagnostic"] = diagnostic
    return entry


_journal_entry = journal_entry


def append_journal_event(
    entry: dict[str, object],
    *,
    journal_file: Path | None = None,
    data_dir: Path | None = None,
    verbose: bool = False,
) -> None:
    """Append one event to the journal, best-effort."""
    j_file = journal_file or _resolve_path("JOURNAL_FILE", JOURNAL_FILE)
    d_dir = data_dir or _resolve_path("DATA_DIR", DATA_DIR)
    line = json.dumps(entry, sort_keys=True)
    try:
        d_dir.mkdir(parents=True, exist_ok=True)
        with open(j_file, "a") as f:
            f.write(line + "\n")
    except OSError as e:
        cli_common.vprint(
            f"[journal] append failed (non-fatal): {e}",
            verbose=verbose,
            file=sys.stderr,
        )


def parse_journal_ts(raw: object) -> datetime | None:
    """Parse a journal entry's ``ts`` field into an aware UTC ``datetime``."""
    if not isinstance(raw, str):
        return None
    try:
        ts = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts


_parse_journal_ts = parse_journal_ts


def read_journal_entries(
    within_hours: float | None = None,
    *,
    journal_file: Path | None = None,
    verbose: bool = False,
) -> list[dict[str, object]]:
    """Read journal entries, optionally filtered to the last ``within_hours``."""
    j_file = journal_file or _resolve_path("JOURNAL_FILE", JOURNAL_FILE)
    if not j_file.exists():
        return []
    try:
        raw_lines = j_file.read_text().splitlines()
    except OSError:
        return []

    non_blank = [line for line in raw_lines if line.strip()]
    cutoff = (
        datetime.now(UTC) - timedelta(hours=within_hours)
        if within_hours is not None
        else None
    )

    entries: list[dict[str, object]] = []
    last_index = len(non_blank) - 1
    for i, line in enumerate(non_blank):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            if i != last_index:
                cli_common.vprint(
                    f"[journal] corrupt line {i + 1} in {j_file}",
                    verbose=verbose,
                    file=sys.stderr,
                )
            continue
        if not isinstance(entry, dict):
            continue
        if cutoff is not None:
            ts = parse_journal_ts(entry.get("ts"))
            if ts is None or ts < cutoff:
                continue
        entries.append(entry)
    return entries


def journal_last_entry_within(
    hours: float, *, journal_file: Path | None = None
) -> bool:
    """Cheap pre-spawn check: does the journal's last entry fall within ``hours``?"""
    j_file = journal_file or _resolve_path("JOURNAL_FILE", JOURNAL_FILE)
    if not j_file.exists():
        return False
    try:
        non_blank = [line for line in j_file.read_text().splitlines() if line.strip()]
    except OSError:
        return False
    for line in reversed(non_blank):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = parse_journal_ts(entry.get("ts") if isinstance(entry, dict) else None)
        if ts is None:
            return False
        return (datetime.now(UTC) - ts) <= timedelta(hours=hours)
    return False


_journal_last_entry_within = journal_last_entry_within


# ── run evidence ─────────────────────────────────────────────────────────────


def load_runs(
    item: str | None = None, *, runs_file: Path | None = None
) -> list[RunRecord]:
    """Load run-evidence rows from :data:`RUNS_FILE`, optionally for one item."""
    r_file = runs_file or _resolve_path("RUNS_FILE", RUNS_FILE)
    if not r_file.exists():
        return []
    try:
        raw_lines = r_file.read_text().splitlines()
    except OSError:
        return []
    non_blank = [line for line in raw_lines if line.strip()]
    runs: list[RunRecord] = []
    for line_no, line in enumerate(non_blank, start=1):
        try:
            run = json.loads(line)
        except json.JSONDecodeError:
            if line_no == len(non_blank):
                break
            print(
                f"runs file corrupted at {r_file}; ignoring malformed line {line_no}",
                file=sys.stderr,
            )
            continue
        if not isinstance(run, dict):
            continue
        if item is None or run.get("item") == item:
            runs.append(cast(RunRecord, run))
    return runs


def write_runs_file(
    runs: Sequence[RunRecord], *, runs_file: Path | None = None
) -> None:
    """Atomically rewrite :data:`RUNS_FILE` with ``runs``."""
    r_file = runs_file or _resolve_path("RUNS_FILE", RUNS_FILE)
    payload = "".join(json.dumps(run, sort_keys=True) + "\n" for run in runs)
    atomic_write_json(r_file, payload, ".runs_tmp_")


def append_run_record(
    record: RunRecord,
    *,
    runs_file: Path | None = None,
    data_dir: Path | None = None,
) -> bool:
    """Append one run-evidence row to :data:`RUNS_FILE` (best-effort)."""
    r_file = runs_file or _resolve_path("RUNS_FILE", RUNS_FILE)
    d_dir = data_dir or _resolve_path("DATA_DIR", DATA_DIR)
    line = json.dumps(record, sort_keys=True)
    try:
        d_dir.mkdir(parents=True, exist_ok=True)
        with open(r_file, "a") as f:
            f.write(line + "\n")
    except OSError as e:
        print(f"[runs] append failed (non-fatal): {e}", file=sys.stderr)
        return False
    return True


# ── recap cache ──────────────────────────────────────────────────────────────


def load_recap_cache(path: Path | None = None) -> dict[str, object] | None:
    """Load ``recap-cache.json``, or ``None`` if missing/corrupt/malformed."""
    target = path or _resolve_path("RECAP_CACHE_FILE", RECAP_CACHE_FILE)
    if not target.exists():
        return None
    try:
        data = json.loads(target.read_text())
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    return data


_load_recap_cache = load_recap_cache


def save_recap_cache(
    backend: str,
    text: str,
    board_fingerprint: str,
    path: Path | None = None,
) -> None:
    """Atomically persist a recap result."""
    target = path or _resolve_path("RECAP_CACHE_FILE", RECAP_CACHE_FILE)
    payload = json.dumps(
        {
            "generated_at": datetime.now(UTC).isoformat(),
            "backend": backend,
            "text": text,
            "board_fingerprint": board_fingerprint,
        }
    )
    atomic_write_json(target, payload, ".recap_tmp_")


_save_recap_cache = save_recap_cache
