#!/usr/bin/env python3
"""The toolkit-home migration: move the toolkit data domains, recoverably.

Run from the repository checkout through ``./install.sh``; never installed.

* ``--migrate-toolkit-home --harness=<list>`` moves the seven data domains
  from ``~/.claude/data`` to the toolkit home and repoints the runtime links.
* ``--rollback-toolkit-home-migration=<id>`` reverses one committed migration
  before it is finalized.
* ``--finalize-toolkit-home-migration=<id>`` deletes the journal-proven
  leftovers of one committed migration: its legacy snapshot, its leftover
  staging, and the legacy links the new ``links.toml`` no longer produces.

Every preflight check runs read-only. ``--dry-run`` stops there and writes
nothing at all: no lock file, no directory, no journal, no history. A real
run takes the migration lock exclusively (never waiting for it), recovers
what earlier crashed runs left behind, repeats the checks under the lock, and
then runs the journalled phases. It does not share ``install.py``'s
best-effort ``Reporter`` path: the first failure aborts. It never enters
``run_install``, so orphan cleanup, settings seeding, and global Git
configuration cannot run during it.

Phases
------
``preflight``, ``inventory`` (an immutable ``inventory.json``), then
``stage:<domain>`` for each domain in the resolver's order (copy into
``<toolkit-root>/.migration-<id>/staging/<domain>`` without following links,
check schemas, rewrite stored paths on the copy), ``baseline`` (on Linux,
append one layer to the uninstall baseline recording the link destinations
about to be created as absent, so uninstall later removes them), ``links``
(create the
runtime links; record the legacy links the new ``links.toml`` no longer
produces, which stay until finalize), any phases later releases register to
run here, ``reverify`` (every staged file and every legacy source re-hashed),
``promote:<domain>`` (``rename(2)`` into place), ``flip`` (the layout pointer
becomes ``toolkit-home`` in one atomic replacement), ``snapshot:<domain>``
(each legacy path renamed into
``~/.claude/data/.toolkit-home-snapshot-<id>/``), and ``validate``
(``dev_status.py validate`` and ``install.sh --check-links`` in fresh
processes). The run ends ``committed``, and the lock is released only then.

A failure before the flip undoes every step newest-first and ends
``aborted``. A failure after it, including a failed validation, *restores*:
the pointer goes back to ``legacy`` first, the snapshots are renamed back,
the promoted domains move to ``.migration-<id>/restored/<domain>`` (never
deleted), every other step is undone newest-first, and the run ends
``restored``.

Each phase is a :class:`Step` in :data:`STEPS` with ``apply``, ``undo``
(idempotent, working only from what the journal recorded) and ``verify``
handlers. An unknown phase in a journal being undone is refused.

Journals
--------
``<installer state>/migrations/<id>/journal.jsonl``, where the installer state
directory is the one holding ``history.jsonl``
(``$HOME/.local/state/agent-toolkit``; it ignores ``XDG_STATE_HOME``, unlike
the migration lock), plus ``rollback.jsonl`` and ``finalize.jsonl`` beside it.
One JSON object per line: ``{seq, ts, id, phase, event, detail}``. Each
record is one ``write()`` on an append-only descriptor followed by ``fsync``,
before the action it describes (``begin``) and after it (``done``). ``end``
and ``abandoned`` are terminal and written at most once. A failed or short
write poisons the journal: it accepts no further records. Every file or
directory created is followed by an ``fsync`` of its parent directory.

Recovery (every real run and command, under the lock)
-----------------------------------------------------
* An unfinished journal with no data phase is **abandoned** (``unwind``).
* An unfinished journal without a completed ``flip`` is **rolled back**
  (``rollback``): every step undone newest-first, then ``abandoned``.
* An unfinished journal with a completed ``flip`` is **rolled forward**
  (``roll-forward``): missing snapshots finished, validation re-run. A failed
  validation restores (``restore``), guarded as below.
* A restore the crash interrupted is finished (``restore``).
* A terminal journal with no history record gets one (``retry``).
* An unfinished ``rollback.jsonl`` or ``finalize.jsonl`` is resumed to
  completion (``resume-rollback``, ``resume-finalize``).

Write guard: a restore that runs after the lock may have been released (roll
forward after a crash, or the narrow rollback) first re-hashes every promoted
domain against the digests journalled at promotion, and checks that no domain
absent at migration time has since appeared at its destination. Any
difference refuses with no mutation and names the files. The append-only
telemetry logs are exempt; restore keeps them aside like every other domain.

:data:`RECOVERY_ORACLE` states, for every fault checkpoint, what a crash there
leaves behind and which recovery action is correct.

Exit codes
  0 dry run passed, run committed, or a rollback/finalize completed (or had
  already completed); 1 a check refused, the run failed or restored; 75 the
  migration lock is busy.

Requires Python 3.12+. Standard library only.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

sys.path.insert(0, str(Path(__file__).resolve().parent / "agent-scripts"))

import agent_toolkit_paths  # noqa: E402 — sibling dir inserted above
import fault_checkpoint  # noqa: E402 — sibling dir inserted above
import link_inspect  # noqa: E402 — sibling dir inserted above
import migration_lock  # noqa: E402 — sibling dir inserted above

import depart  # noqa: E402 — sibling module next to this file
import depart_exec  # noqa: E402 — sibling module next to this file

MIGRATION_ID_RE = re.compile(r"^mig-\d{8}T\d{6}Z-[0-9a-f]{6}$")
OUTCOME_COMMITTED = "committed"
OUTCOME_RESTORED = "restored"
OUTCOME_ABORTED = "aborted"
OUTCOME_ROLLED_BACK = "rolled-back"
OUTCOME_FINALIZED = "finalized"
TERMINAL_EVENTS = frozenset({"end", "abandoned"})
LOCK_SITE = "toolkit-home-migration"
JOURNAL_NAME = "journal.jsonl"
ROLLBACK_NAME = "rollback.jsonl"
FINALIZE_NAME = "finalize.jsonl"
INVENTORY_NAME = "inventory.json"
INVENTORY_TMP_PREFIX = "inventory.json.tmp"
HASH_CHUNK = 1 << 20
VALIDATION_TIMEOUT = 600

Status = Literal["ok", "warn", "refuse"]
Action = Literal[
    "none",
    "retry",
    "unwind",
    "rollback",
    "roll-forward",
    "restore",
    "resume-rollback",
    "resume-finalize",
]


class MigrationError(RuntimeError):
    """A real run cannot continue; the message names the cause."""


class JournalError(MigrationError):
    """A journal write failed, or a record was refused (terminal / poisoned)."""


class JournalCorrupt(JournalError):
    """A journal has a malformed line that is not its final line."""


# ── options, findings, report ────────────────────────────────────────────────


@dataclass(frozen=True)
class MigrationOptions:
    harnesses: tuple[str, ...]
    profile: str = "personal"
    dry_run: bool = False
    json_report: bool = False
    cross_filesystem: bool = False
    skip_reconciliation: bool = False
    migration_id: str | None = None
    quiet: bool = False
    verbose: bool = False


@dataclass
class Finding:
    check: str
    status: Status
    detail: str
    paths: list[str] = field(default_factory=list)


@dataclass
class PreflightReport:
    migration_id: str
    dry_run: bool
    findings: list[Finding] = field(default_factory=list)
    inventory: dict[str, object] | None = None
    recovered: list[dict[str, str]] = field(default_factory=list)
    journal: str | None = None
    outcome: str = ""

    @property
    def refused(self) -> bool:
        return any(f.status == "refuse" for f in self.findings)

    def to_json(self) -> dict[str, object]:
        return {
            "migration_id": self.migration_id,
            "dry_run": self.dry_run,
            "outcome": self.outcome,
            "findings": [asdict(f) for f in self.findings],
            "inventory": self.inventory,
            "recovered": self.recovered,
            "journal": self.journal,
        }


@dataclass(frozen=True)
class MigrationContext:
    opts: MigrationOptions
    migration_id: str
    home: Path
    repo_root: Path
    installer_state: Path
    history: Path


def new_migration_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"mig-{stamp}-{secrets.token_hex(3)}"


# ── crash points and their oracle ────────────────────────────────────────────


@dataclass(frozen=True)
class Expected:
    """What a crash at one checkpoint leaves behind, and the right recovery.

    ``journal`` names the journal file ``last_event`` refers to; ``layout``
    is the layout recovery must leave the machine in.
    """

    action: Action
    journal_file: bool
    last_event: str | None
    inventory: bool
    history: bool
    layout: agent_toolkit_paths.Layout = "legacy"
    journal: str = JOURNAL_NAME


def _per_domain(template: str) -> tuple[str, ...]:
    return tuple(template.format(d) for d in agent_toolkit_paths.DOMAINS)


_RESTORE_STEPS = ("begun", "pointer", "promoted-aside", "undone")
_FINALIZE_STEPS = ("begun", "snapshot-removed", "staging-removed", "links-removed")

CHECKPOINTS: tuple[str, ...] = (
    "migrate.lock.taken",
    "migrate.journal.dir-created",
    "migrate.journal.opened",
    "migrate.preflight.begun",
    "migrate.preflight.done",
    "migrate.inventory.begun",
    "migrate.inventory.tmp-written",
    "migrate.inventory.written",
    "migrate.inventory.done",
    *(
        name
        for d in agent_toolkit_paths.DOMAINS
        for name in (
            f"migrate.stage.{d}.begun",
            f"migrate.stage.{d}.copied",
            f"migrate.stage.{d}.done",
        )
    ),
    "migrate.baseline.begun",
    "migrate.baseline.written",
    "migrate.baseline.done",
    "migrate.links.begun",
    "migrate.links.done",
    "migrate.reverify.begun",
    "migrate.reverify.done",
    *(
        name
        for d in agent_toolkit_paths.DOMAINS
        for name in (f"migrate.promote.{d}.before", f"migrate.promote.{d}.done")
    ),
    "migrate.flip.before",
    "migrate.flip.done",
    *(
        name
        for d in agent_toolkit_paths.DOMAINS
        for name in (f"migrate.snapshot.{d}.before", f"migrate.snapshot.{d}.done")
    ),
    "migrate.validate.begun",
    "migrate.validate.done",
    "migrate.journal.ended",
    "migrate.history.written",
    *(f"migrate.restore.{s}" for s in _RESTORE_STEPS),
    "migrate.restore.ended",
    *(f"migrate.rollback.{s}" for s in _RESTORE_STEPS),
    "migrate.rollback.ended",
    *(f"migrate.finalize.{s}" for s in _FINALIZE_STEPS),
    "migrate.finalize.ended",
)


def _oracle() -> dict[str, Expected]:
    """One row per checkpoint. The flip is the boundary between the rules.

    Before any data phase, an unfinished journal is abandoned (``unwind``).
    From the first ``stage`` record until ``flip`` is done it is rolled back
    to legacy (``rollback``). From ``flip`` done on it is rolled forward to
    toolkit-home (``roll-forward``). Restore, rollback and finalize
    checkpoints are reached in their own scenarios: a failed validation, the
    narrow rollback of a committed run, and finalize of a committed run.
    """

    def event(name: str) -> str:
        return "done" if name.endswith(".done") else "begin"

    rows: dict[str, Expected] = {
        "migrate.lock.taken": Expected("none", False, None, False, False),
        "migrate.journal.dir-created": Expected("unwind", False, None, False, False),
        "migrate.journal.opened": Expected("unwind", True, None, False, False),
        "migrate.preflight.begun": Expected("unwind", True, "begin", False, False),
        "migrate.preflight.done": Expected("unwind", True, "done", False, False),
        "migrate.inventory.begun": Expected("unwind", True, "begin", False, False),
        "migrate.inventory.tmp-written": Expected(
            "unwind", True, "begin", False, False
        ),
        "migrate.inventory.written": Expected("unwind", True, "begin", True, False),
        "migrate.inventory.done": Expected("unwind", True, "done", True, False),
    }
    before_flip = [
        *(n for n in CHECKPOINTS if n.startswith("migrate.stage.")),
        "migrate.baseline.begun",
        "migrate.baseline.written",
        "migrate.baseline.done",
        "migrate.links.begun",
        "migrate.links.done",
        "migrate.reverify.begun",
        "migrate.reverify.done",
        *(n for n in CHECKPOINTS if n.startswith("migrate.promote.")),
        "migrate.flip.before",
    ]
    for name in before_flip:
        rows[name] = Expected("rollback", True, event(name), True, False)
    after_flip = [
        "migrate.flip.done",
        *(n for n in CHECKPOINTS if n.startswith("migrate.snapshot.")),
        "migrate.validate.begun",
        "migrate.validate.done",
    ]
    for name in after_flip:
        rows[name] = Expected(
            "roll-forward", True, event(name), True, False, "toolkit-home"
        )
    rows["migrate.journal.ended"] = Expected(
        "retry", True, "end", True, False, "toolkit-home"
    )
    rows["migrate.history.written"] = Expected(
        "none", True, "end", True, True, "toolkit-home"
    )
    for step in _RESTORE_STEPS:
        rows[f"migrate.restore.{step}"] = Expected(
            "restore", True, "begin", True, False
        )
        rows[f"migrate.rollback.{step}"] = Expected(
            "resume-rollback", True, "begin", True, True, "legacy", ROLLBACK_NAME
        )
    rows["migrate.restore.ended"] = Expected("retry", True, "end", True, False)
    rows["migrate.rollback.ended"] = Expected(
        "none", True, "end", True, True, "legacy", ROLLBACK_NAME
    )
    for step in _FINALIZE_STEPS:
        rows[f"migrate.finalize.{step}"] = Expected(
            "resume-finalize", True, "begin", True, True, "toolkit-home", FINALIZE_NAME
        )
    rows["migrate.finalize.ended"] = Expected(
        "none", True, "end", True, True, "toolkit-home", FINALIZE_NAME
    )
    return rows


RECOVERY_ORACLE: dict[str, Expected] = _oracle()


def _checkpoint(name: str) -> None:
    assert name in RECOVERY_ORACLE, name
    fault_checkpoint.checkpoint(name)


# ── durable filesystem helpers ───────────────────────────────────────────────


def _fsync_dir(directory: Path) -> None:
    fd = os.open(str(directory), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _mkdir_durable(path: Path) -> None:
    """Create ``path`` and any missing ancestors, fsyncing each new entry's parent."""
    missing: list[Path] = []
    probe = path
    while not probe.exists():
        missing.append(probe)
        probe = probe.parent
    for directory in reversed(missing):
        directory.mkdir()
        _fsync_dir(directory.parent)


def _write_all(fd: int, data: bytes) -> None:
    written = os.write(fd, data)
    if written != len(data):
        raise JournalError(f"short write: {written} of {len(data)} bytes")


def _lexists(path: Path) -> bool:
    try:
        path.lstat()
    except (FileNotFoundError, NotADirectoryError):
        return False
    return True


def _device_of(path: Path) -> int:
    return path.lstat().st_dev


def _nearest_existing(path: Path) -> Path:
    probe = path
    while not _lexists(probe):
        probe = probe.parent
    return probe


# ── journal ──────────────────────────────────────────────────────────────────


def _split_valid(raw: bytes) -> tuple[list[dict[str, object]], int, bool]:
    """Parse journal bytes: (records, byte length of the valid prefix, torn tail?).

    A malformed final line is a torn tail and is skipped; a malformed earlier
    line raises :class:`JournalCorrupt`.
    """
    records: list[dict[str, object]] = []
    lines = raw.split(b"\n")
    # With a trailing newline the last element is b"" and is not a line.
    complete = lines[:-1]
    tail = lines[-1]
    offset = 0
    for index, line in enumerate(complete):
        record = _parse_line(line)
        if record is None:
            last_nonempty = index == len(complete) - 1 and not tail
            if last_nonempty:
                return records, offset, True
            raise JournalCorrupt(f"malformed journal line {index + 1}")
        records.append(record)
        offset += len(line) + 1
    if tail:
        record = _parse_line(tail)
        if record is None:
            return records, offset, True
        records.append(record)
        offset += len(tail)
    return records, offset, False


def _parse_line(line: bytes) -> dict[str, object] | None:
    if not line.strip():
        return None
    try:
        parsed = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def read_records(directory: Path, name: str = JOURNAL_NAME) -> list[dict[str, object]]:
    """Every valid record in one journal file; a torn final line is skipped."""
    path = directory / name
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return []
    return _split_valid(raw)[0]


def is_terminal(directory: Path, name: str = JOURNAL_NAME) -> bool:
    records = read_records(directory, name)
    return bool(records) and records[-1].get("event") in TERMINAL_EVENTS


def _outcome(records: list[dict[str, object]]) -> str | None:
    """The ``end`` outcome of a finished journal, else None."""
    if not records or records[-1].get("event") != "end":
        return None
    detail = records[-1].get("detail")
    return str(detail.get("outcome")) if isinstance(detail, dict) else None


def _record(
    migration_id: str, seq: int, phase: str, event: str, detail: dict[str, object]
) -> bytes:
    entry = {
        "seq": seq,
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "id": migration_id,
        "phase": phase,
        "event": event,
        "detail": detail,
    }
    return (json.dumps(entry, sort_keys=True) + "\n").encode()


class Journal:
    """One write-ahead journal file: the migration run's, or a rollback's or finalize's.

    ``records`` holds every record in the file, including those written
    through this object, so a run can read back what it journalled.
    """

    def __init__(
        self,
        directory: Path,
        migration_id: str,
        fd: int,
        name: str = JOURNAL_NAME,
        records: list[dict[str, object]] | None = None,
    ) -> None:
        self.directory = directory
        self.migration_id = migration_id
        self.name = name
        self._fd = fd
        self.records: list[dict[str, object]] = records if records is not None else []
        last = self.records[-1].get("seq", 0) if self.records else 0
        self._seq = last if isinstance(last, int) else 0
        self.poisoned = False
        self.terminal = bool(self.records) and (
            self.records[-1].get("event") in TERMINAL_EVENTS
        )

    @classmethod
    def open(cls, installer_state: Path, migration_id: str) -> Journal:
        root = installer_state / "migrations"
        _mkdir_durable(root)
        directory = root / migration_id
        try:
            directory.mkdir()
        except FileExistsError:
            raise JournalError(f"a journal already exists for {migration_id}") from None
        _fsync_dir(root)
        _checkpoint("migrate.journal.dir-created")
        journal = cls.create(directory, JOURNAL_NAME)
        _checkpoint("migrate.journal.opened")
        return journal

    @classmethod
    def create(cls, directory: Path, name: str) -> Journal:
        """Create a new, empty journal file in an existing migration directory."""
        fd = os.open(
            directory / name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND,
            0o644,
        )
        os.fsync(fd)
        _fsync_dir(directory)
        return cls(directory, directory.name, fd, name)

    @classmethod
    def resume(cls, directory: Path, name: str = JOURNAL_NAME) -> Journal:
        """Reopen a journal a crashed run left, to append to it.

        A torn final line is truncated away first, and a missing final newline
        is restored, so the next record starts on its own line. The file is
        created if the crash came before it existed.
        """
        path = directory / name
        created = not path.exists()
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            raw = b""
            os.lseek(fd, 0, os.SEEK_SET)
            while chunk := os.read(fd, HASH_CHUNK):
                raw += chunk
            records, valid, _torn = _split_valid(raw)
            if valid != len(raw):
                os.ftruncate(fd, valid)
                os.fsync(fd)
            if valid and not raw[:valid].endswith(b"\n"):
                _write_all(fd, b"\n")
                os.fsync(fd)
        except BaseException:
            os.close(fd)
            raise
        if created:
            _fsync_dir(directory)
        return cls(directory, directory.name, fd, name, records)

    def _append(self, phase: str, event: str, detail: dict[str, object]) -> None:
        if self.poisoned:
            raise JournalError(
                f"journal {self.directory / self.name} is poisoned by a failed write"
            )
        if self.terminal:
            raise JournalError(
                f"journal {self.directory / self.name} already has a terminal record"
            )
        self._seq += 1
        data = _record(self.migration_id, self._seq, phase, event, detail)
        try:
            _write_all(self._fd, data)
            os.fsync(self._fd)
        except (OSError, JournalError) as exc:
            self.poisoned = True
            raise JournalError(f"journal write failed: {exc}") from exc
        self.records.append(json.loads(data))
        if event in TERMINAL_EVENTS:
            self.terminal = True

    def begin(self, phase: str, **detail: object) -> None:
        self._append(phase, "begin", detail)

    def done(self, phase: str, **detail: object) -> None:
        self._append(phase, "done", detail)

    def end(self, outcome: str, **detail: object) -> None:
        self._append("run", "end", {"outcome": outcome, **detail})

    def abandon(self, reason: str) -> None:
        self._append("recover", "abandoned", {"reason": reason})

    def close(self) -> None:
        if self._fd != -1:
            os.close(self._fd)
            self._fd = -1


def _journal_dirs(installer_state: Path) -> list[Path]:
    root = installer_state / "migrations"
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and not p.is_symlink())


def find_unfinished(installer_state: Path) -> list[Path]:
    return [d for d in _journal_dirs(installer_state) if not is_terminal(d)]


def abandon(directory: Path, reason: str) -> None:
    """Close out a crashed run's journal. Safe to repeat after a crash of its own."""
    for stray in directory.glob(f"{INVENTORY_TMP_PREFIX}*"):
        stray.unlink()
    _fsync_dir(directory)
    journal = Journal.resume(directory)
    try:
        journal.abandon(reason)
    finally:
        journal.close()


# ── history ──────────────────────────────────────────────────────────────────


def append_history(path: Path, record: dict[str, object]) -> None:
    """Append one record to ``history.jsonl`` so a torn tail never swallows it."""
    _mkdir_durable(path.parent)
    created = not path.exists()
    needs_newline = False
    if not created and path.stat().st_size:
        with path.open("rb") as handle:
            handle.seek(-1, os.SEEK_END)
            needs_newline = handle.read(1) != b"\n"
    data = (("\n" if needs_newline else "") + json.dumps(record) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        _write_all(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    if created:
        _fsync_dir(path.parent)


def _drop_history_entries(
    path: Path, drop: Callable[[dict[str, object]], bool]
) -> None:
    """Rewrite ``history.jsonl`` without the entries ``drop`` selects.

    Unparseable lines are kept byte for byte. Atomic: temp file, fsync,
    ``os.replace``, parent fsync. Nothing is written when nothing matches.
    """
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return
    kept: list[bytes] = []
    dropped = False
    for line in raw.splitlines(keepends=True):
        entry = _parse_line(line.rstrip(b"\n"))
        if entry is not None and drop(entry):
            dropped = True
            continue
        kept.append(line if line.endswith(b"\n") else line + b"\n")
    if not dropped:
        return
    tmp = path.with_name(f".{path.name}.migration-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        _write_all(fd, b"".join(kept))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def history_ids(path: Path) -> set[str]:
    ids: set[str] = set()
    for entry in link_inspect.read_manifest_entries(path):
        if entry.get("kind") == "migration" and isinstance(entry.get("id"), str):
            ids.add(entry["id"])  # type: ignore[arg-type]
    return ids


# ── preflight checks ─────────────────────────────────────────────────────────

# Recognized store files and how to validate them. Names not listed are only
# inventoried, never parsed.
_VERSIONED: dict[str, dict[str, int]] = {
    "work-items": {"items.json": 2, "pending_items.json": 1},
}
_JSON: dict[str, tuple[str, ...]] = {
    "work-items": ("_meta.json", "recap-cache.json"),
    "out-of-scope": ("index.json",),
}
_JSONL: dict[str, tuple[str, ...]] = {
    "work-items": ("journal.jsonl", "runs.jsonl"),
}
_SINGLE_FILE_JSONL = frozenset({"guard-rail-log", "backend-log"})
GRILL_SCHEMA_VERSION = 1


def _legacy(domain: str) -> Path:
    return agent_toolkit_paths.path_for_layout(domain, "legacy")


def _destination(domain: str) -> Path | None:
    try:
        return agent_toolkit_paths.path_for_layout(domain, "toolkit-home")
    except agent_toolkit_paths.LayoutError:
        return None


def _walk(root: Path) -> Iterator[tuple[str, Path, os.stat_result]]:
    """Yield (relative path, path, lstat) for every entry, never following links.

    A single-file domain yields itself as ``"."``. Directories are descended
    only when ``lstat`` says they are real directories.
    """
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode):
        yield ".", root, info
        return
    stack = [root]
    while stack:
        directory = stack.pop()
        for child in sorted(directory.iterdir()):
            child_info = child.lstat()
            rel = str(child.relative_to(root))
            if stat.S_ISDIR(child_info.st_mode):
                stack.append(child)
                continue
            yield rel, child, child_info


def _schema_rule(domain: str, rel: str) -> tuple[str, int | None] | None:
    """How to validate one file: ("versioned", n) / ("json", None) / ("jsonl", None)."""
    if domain in _SINGLE_FILE_JSONL:
        return ("jsonl", None)
    if rel in _VERSIONED.get(domain, {}):
        return ("versioned", _VERSIONED[domain][rel])
    if rel in _JSON.get(domain, ()):
        return ("json", None)
    if rel in _JSONL.get(domain, ()):
        return ("jsonl", None)
    if domain == "decisions" and rel.endswith(".json"):
        if "/" not in rel:
            return ("versioned", GRILL_SCHEMA_VERSION)
        return ("json", None)
    if domain == "ticket-batches" and rel.endswith(".json"):
        return ("json", None)
    return None


def _check_jsonl(path: Path) -> Finding | None:
    """Stream a JSONL file; a bad final line warns, a bad earlier one refuses."""
    bad: int | None = None
    count = 0
    with path.open("rb") as handle:
        for count, line in enumerate(handle, start=1):  # noqa: B007
            if bad is not None:
                return Finding(
                    "schemas", "refuse", f"corrupt line {bad} in {path}", [str(path)]
                )
            if line.strip() and _parse_line(line.rstrip(b"\n")) is None:
                bad = count
    if bad is not None:
        return Finding(
            "schemas", "warn", f"torn final line {bad} in {path}", [str(path)]
        )
    return None


def _check_json(path: Path, rule: str, version: int | None) -> Finding | None:
    try:
        parsed = json.loads(path.read_bytes().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return Finding(
            "schemas", "refuse", f"invalid JSON in {path}: {exc}", [str(path)]
        )
    if rule != "versioned":
        return None
    found = parsed.get("schema_version") if isinstance(parsed, dict) else None
    if found != version:
        return Finding(
            "schemas",
            "refuse",
            f"unsupported schema_version {found!r} in {path} (expected {version})",
            [str(path)],
        )
    return None


def _check_domain_contents(domain: str, root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for rel, path, info in _walk(root):
        if stat.S_ISLNK(info.st_mode):
            continue
        if not stat.S_ISREG(info.st_mode):
            findings.append(
                Finding(
                    "inventory-types",
                    "refuse",
                    f"{path} is not a regular file, directory, or symlink",
                    [str(path)],
                )
            )
            continue
        rule = _schema_rule(domain, rel)
        if rule is None:
            continue
        kind, version = rule
        finding = (
            _check_jsonl(path) if kind == "jsonl" else _check_json(path, kind, version)
        )
        if finding is not None:
            findings.append(finding)
    return findings


def _check_checkout(ctx: MigrationContext) -> Finding:
    info = link_inspect.detect_wsl
    system = os.uname().sysname
    missing: list[str] = []
    for spec in link_inspect.load_links(ctx.repo_root / "links.toml"):
        if not link_inspect.link_applies(
            spec,
            harnesses=ctx.opts.harnesses,
            is_mac=system == "Darwin",
            is_linux=system == "Linux",
            is_wsl=info(system),
            profile=ctx.opts.profile,
        ):
            continue
        if any(ch in spec.src for ch in "*?["):
            continue
        if not (ctx.repo_root / spec.src).exists():
            missing.append(spec.src)
    if missing:
        return Finding(
            "checkout", "refuse", "declared runtime sources are missing", missing
        )
    return Finding("checkout", "ok", "every declared runtime source is present")


def _check_runtime(ctx: MigrationContext) -> Finding:
    scripts = ctx.home / ".claude" / "scripts"
    lock_module = scripts / "migration_lock.py"
    resolver = scripts / "agent_toolkit_paths.py"
    missing = [str(p) for p in (lock_module, resolver) if not p.is_file()]
    if missing:
        return Finding(
            "runtime-lock-aware",
            "refuse",
            "the installed runtime is missing; run ./install.sh first",
            missing,
        )
    enforce: object = None
    try:
        tree = ast.parse(lock_module.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError) as exc:
        return Finding(
            "runtime-lock-aware", "refuse", f"cannot read {lock_module}: {exc}"
        )
    for node in tree.body:
        # ``ENFORCE = True`` or the annotated ``ENFORCE: bool = True``.
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target, value = node.target, node.value
        else:
            continue
        if (
            isinstance(target, ast.Name)
            and target.id == "ENFORCE"
            and isinstance(value, ast.Constant)
        ):
            enforce = value.value
    if enforce is not True:
        return Finding(
            "runtime-lock-aware",
            "refuse",
            f"the installed migration lock does not enforce (ENFORCE = {enforce!r}); "
            "install the current release first",
            [str(lock_module)],
        )
    return Finding(
        "runtime-lock-aware", "ok", "the installed runtime enforces the lock"
    )


def _check_layout() -> Finding:
    try:
        layout = agent_toolkit_paths.current_layout()
    except agent_toolkit_paths.LayoutError as exc:
        return Finding("layout", "refuse", str(exc))
    if layout != "legacy":
        return Finding("layout", "refuse", f"the layout is already {layout!r}")
    return Finding("layout", "ok", "legacy layout")


def _check_override(ctx: MigrationContext) -> Finding:
    value = os.environ.get(agent_toolkit_paths.ENV_HOME)
    if value is None:
        return Finding("toolkit-home-override", "ok", "not set")
    if not Path(value).is_absolute():
        return Finding(
            "toolkit-home-override",
            "refuse",
            f"{agent_toolkit_paths.ENV_HOME} is relative: {value!r}",
        )
    if ctx.opts.cross_filesystem:
        return Finding(
            "toolkit-home-override", "warn", f"toolkit home overridden to {value}"
        )
    return Finding(
        "toolkit-home-override",
        "refuse",
        f"{agent_toolkit_paths.ENV_HOME}={value} points elsewhere; unset it or pass "
        "--cross-filesystem",
    )


def _check_domains(ctx: MigrationContext) -> list[Finding]:
    findings: list[Finding] = []
    symlinked: list[str] = []
    collisions: list[str] = []
    blocked: list[str] = []
    devices: list[str] = []
    for domain in agent_toolkit_paths.DOMAINS:
        legacy = _legacy(domain)
        dest = _destination(domain)
        if _lexists(legacy):
            if stat.S_ISLNK(legacy.lstat().st_mode):
                symlinked.append(str(legacy))
            else:
                findings.extend(_check_domain_contents(domain, legacy))
        if dest is None:
            continue
        for ancestor in [dest, *dest.parents]:
            if ancestor == ctx.home or not ancestor.is_relative_to(ctx.home):
                break
            if not _lexists(ancestor):
                continue
            mode = ancestor.lstat().st_mode
            if stat.S_ISLNK(mode) or (ancestor != dest and not stat.S_ISDIR(mode)):
                blocked.append(str(ancestor))
                break
        if (
            _lexists(dest)
            and str(dest) not in blocked
            and (not dest.is_dir() or any(dest.iterdir()))
        ):
            collisions.append(str(dest))
        if _device_of(_nearest_existing(legacy)) != _device_of(_nearest_existing(dest)):
            devices.append(domain)
    findings.append(
        Finding(
            "legacy-roots", "refuse", "a legacy domain root is a symlink", symlinked
        )
        if symlinked
        else Finding("legacy-roots", "ok", "legacy roots are real paths")
    )
    findings.append(
        Finding(
            "destination-ancestors",
            "refuse",
            "a destination is blocked",
            sorted(set(blocked)),
        )
        if blocked
        else Finding("destination-ancestors", "ok", "destinations are reachable")
    )
    findings.append(
        Finding("collisions", "refuse", "destinations already hold data", collisions)
        if collisions
        else Finding("collisions", "ok", "no destination holds data")
    )
    if devices:
        status: Status = "warn" if ctx.opts.cross_filesystem else "refuse"
        detail = (
            "cross-filesystem mode: later stages copy and verify"
            if ctx.opts.cross_filesystem
            else "legacy and toolkit-home paths are on different devices; pass "
            "--cross-filesystem to allow copy-and-verify"
        )
        findings.append(Finding("same-device", status, detail, devices))
    else:
        findings.append(Finding("same-device", "ok", "same device"))
    if not any(f.check in ("schemas", "inventory-types") for f in findings):
        findings.append(Finding("schemas", "ok", "every recognized store parses"))
    return findings


def _check_unclassified(ctx: MigrationContext) -> Finding:
    data_root = ctx.home / ".claude" / "data"
    known = {_legacy(d).name for d in agent_toolkit_paths.DOMAINS}
    known.add(agent_toolkit_paths.POINTER_RELPATH.name)
    if not data_root.is_dir():
        return Finding("unclassified", "ok", "no legacy data root")
    extra = sorted(str(p) for p in data_root.iterdir() if p.name not in known)
    if extra:
        return Finding(
            "unclassified",
            "warn",
            "not toolkit data; left in place, never moved",
            extra,
        )
    return Finding("unclassified", "ok", "nothing unclassified")


def _check_lock_probe() -> Finding:
    """Dry run: is the migration lock free? Opens without creating it."""
    import fcntl

    path = migration_lock.lock_path()
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return Finding("migration-lock", "ok", "free (no lock file yet)")
    except OSError as exc:
        return Finding("migration-lock", "refuse", f"cannot read lock {path}: {exc}")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return Finding("migration-lock", "warn", "held by a writer or migration now")
    except OSError as exc:
        return Finding("migration-lock", "refuse", f"cannot lock {path}: {exc}")
    finally:
        os.close(fd)
    return Finding("migration-lock", "ok", "free")


def _check_journals(ctx: MigrationContext) -> list[Finding]:
    findings: list[Finding] = []
    try:
        unfinished = find_unfinished(ctx.installer_state)
    except JournalCorrupt as exc:
        return [Finding("unfinished-journals", "refuse", str(exc))]
    if unfinished:
        findings.append(
            Finding(
                "unfinished-journals",
                "warn",
                "earlier runs died; a real run abandons these first",
                [str(p) for p in unfinished],
            )
        )
    else:
        findings.append(Finding("unfinished-journals", "ok", "none"))
    target = ctx.installer_state / "migrations" / ctx.migration_id
    if _lexists(target):
        findings.append(
            Finding(
                "migration-id",
                "refuse",
                f"{ctx.migration_id} is already used",
                [str(target)],
            )
        )
    else:
        findings.append(Finding("migration-id", "ok", ctx.migration_id))
    return findings


def _check_reconciliation(ctx: MigrationContext) -> Finding:
    if ctx.opts.profile == "work":
        return Finding("reconciliation", "ok", "skipped: no peer on a work machine")
    advice = (
        "run `python3 ~/.claude/scripts/dev_status_sync.py status` and resolve any "
        "divergence with the other machine first"
    )
    if ctx.opts.skip_reconciliation:
        return Finding(
            "reconciliation", "warn", f"skipped by --skip-reconciliation; {advice}"
        )
    if ctx.opts.dry_run:
        return Finding("reconciliation", "warn", f"not checked automatically; {advice}")
    return Finding(
        "reconciliation",
        "refuse",
        f"not checked automatically; {advice}, then re-run with --skip-reconciliation",
    )


def _check_legacy_links(ctx: MigrationContext) -> tuple[Finding, list[dict[str, str]]]:
    links: list[dict[str, str]] = []
    dangling: list[str] = []
    for entry in link_inspect.read_manifest_entries(ctx.history):
        if entry.get("kind") != "symlink-created":
            continue
        dest = Path(str(entry.get("dest", "")))
        if not _lexists(dest) or not dest.is_symlink():
            continue
        links.append({"dest": str(dest), "target": os.readlink(dest)})
        if not dest.exists():
            dangling.append(str(dest))
    if dangling:
        return Finding(
            "legacy-links", "warn", "installed links point nowhere", dangling
        ), links
    return Finding(
        "legacy-links", "ok", f"{len(links)} installed links recorded"
    ), links


def run_checks(ctx: MigrationContext) -> list[Finding]:
    """Every structural check, read-only and without hashing."""
    findings = [
        _check_checkout(ctx),
        _check_runtime(ctx),
        _check_layout(),
        _check_override(ctx),
        *_check_domains(ctx),
        _check_unclassified(ctx),
        *_check_journals(ctx),
        _check_reconciliation(ctx),
        _check_legacy_links(ctx)[0],
        Finding("harness-selection", "ok", ",".join(ctx.opts.harnesses)),
    ]
    if ctx.opts.dry_run:
        findings.append(_check_lock_probe())
    return findings


# ── inventory ────────────────────────────────────────────────────────────────


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(HASH_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _schema_version(domain: str, rel: str, path: Path) -> int | None:
    rule = _schema_rule(domain, rel)
    if rule is None or rule[0] != "versioned":
        return None
    try:
        parsed = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return None
    version = parsed.get("schema_version") if isinstance(parsed, dict) else None
    return version if isinstance(version, int) else None


def build_inventory(ctx: MigrationContext, *, strict: bool) -> dict[str, object]:
    """Hash every legacy store file once.

    ``strict`` (under the lock): a file that vanishes or changes type is a
    failure. Otherwise (dry-run snapshot) it is skipped.
    """
    domains: dict[str, object] = {}
    for domain in agent_toolkit_paths.DOMAINS:
        legacy = _legacy(domain)
        dest = _destination(domain)
        files: list[dict[str, object]] = []
        if _lexists(legacy) and not legacy.is_symlink():
            for rel, path, info in _walk(legacy):
                try:
                    if stat.S_ISLNK(info.st_mode):
                        files.append({"path": rel, "symlink": os.readlink(path)})
                        continue
                    if not stat.S_ISREG(info.st_mode):
                        raise MigrationError(f"{path} is not a regular file")
                    entry: dict[str, object] = {
                        "path": rel,
                        "size": info.st_size,
                        "sha256": _sha256(path),
                    }
                    version = _schema_version(domain, rel, path)
                    if version is not None:
                        entry["schema_version"] = version
                    files.append(entry)
                except FileNotFoundError:
                    if strict:
                        raise MigrationError(
                            f"{path} vanished while the lock was held"
                        ) from None
        domains[domain] = {
            "legacy": str(legacy),
            "destination": str(dest) if dest is not None else None,
            "files": files,
        }
    unclassified = _check_unclassified(ctx)
    return {
        "migration_id": ctx.migration_id,
        "created": datetime.now(UTC).isoformat(timespec="seconds"),
        "profile": ctx.opts.profile,
        "harnesses": list(ctx.opts.harnesses),
        "layout": "legacy",
        "domains": domains,
        "unclassified": unclassified.paths,
        "legacy_links": _check_legacy_links(ctx)[1],
    }


def write_inventory(directory: Path, inventory: dict[str, object]) -> str:
    """Write ``inventory.json`` atomically and read-only; return its sha256."""
    payload = (json.dumps(inventory, indent=2, sort_keys=True) + "\n").encode()
    tmp = directory / f"{INVENTORY_TMP_PREFIX}{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    try:
        _write_all(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
    _checkpoint("migrate.inventory.tmp-written")
    os.replace(tmp, directory / INVENTORY_NAME)
    _fsync_dir(directory)
    _checkpoint("migrate.inventory.written")
    return hashlib.sha256(payload).hexdigest()


# ── run state and the step registry ──────────────────────────────────────────

StepRecord = dict[str, object]
"""One journalled step as handlers see it: ``{"phase", "begin", "done"}``.

``begin`` is the detail of the step's ``begin`` record, ``done`` the detail of
its ``done`` record, or None when the step was begun and never finished.
"""

Digests = dict[str, object]
"""Relative path → sha256, or ``{"symlink": target}``; ``"."`` for a file root."""

TELEMETRY_DOMAINS = frozenset({"guard-rail-log", "backend-log"})


class RestoreRefused(MigrationError):
    """The toolkit home changed after the migration; a restore would lose it."""


@dataclass
class RunState:
    """What the step handlers of one migration work from.

    ``records`` is the migration journal: live (the run's own journal) during
    a run and its recovery, a read-only copy during a rollback or finalize,
    whose own records go to ``journal``.
    """

    ctx: MigrationContext
    journal: Journal | None
    records: list[dict[str, object]]
    work_dir: Path
    snapshot_dir: Path
    inventory: dict[str, object] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def step(self, phase: str) -> StepRecord | None:
        """The latest record of ``phase``, or None."""
        for record in reversed(_steps_in(self.records)):
            if record["phase"] == phase:
                return record
        return None

    def require_journal(self) -> Journal:
        if self.journal is None:
            raise MigrationError("no journal is open for this operation")
        return self.journal

    def domain_entry(self, domain: str) -> dict[str, object]:
        return self.inventory["domains"][domain]  # type: ignore[index,return-value]


@dataclass(frozen=True)
class Step:
    """How one journalled phase is applied, undone, and checked.

    ``apply`` performs the phase and returns the detail of its ``done`` record.
    ``undo`` reverses it from what the step record holds, never from
    re-derived state; it is idempotent and must handle a step that was begun
    and never finished. ``verify`` reports whether ``apply``'s effect is
    present. ``begin``, when given, computes the ``begin`` record's detail
    before anything changes, so ``undo`` can work from it after a crash.
    ``guard``, when given, lists what changed since the step ran that would
    make undoing it unsafe; a restore after the lock may have been released,
    and the narrow rollback, refuse before any change when it reports
    anything.
    """

    apply: Callable[[RunState, StepRecord], dict[str, object]]
    undo: Callable[[RunState, StepRecord], None]
    verify: Callable[[RunState, StepRecord], bool]
    begin: Callable[[RunState, str], dict[str, object]] | None = None
    guard: Callable[[RunState, StepRecord], list[str]] | None = None


STEPS: dict[str, Step] = {}
RUNTIME_PHASES: list[str] = []
CONTROL_PHASES = frozenset({"run", "recover", "restore", "finalize"})


def register_step(phase: str, step: Step, *, in_run: bool = False) -> None:
    """Register ``step`` for ``phase``: an exact name, or ``"name:"`` for ``name:<x>``.

    ``in_run`` also schedules the phase in every migration run, after
    ``links`` and before ``reverify``, in registration order. Restore and the
    narrow rollback undo it through the registry like any built-in phase.
    Registering a phase twice raises ValueError.
    """
    if phase in STEPS or phase in CONTROL_PHASES:
        raise ValueError(f"migration phase {phase!r} is already registered")
    STEPS[phase] = step
    if in_run:
        RUNTIME_PHASES.append(phase)


def step_for(phase: str) -> Step:
    """The registered step for ``phase``; an unknown phase fails closed."""
    if phase in STEPS:
        return STEPS[phase]
    name, sep, _ = phase.partition(":")
    if sep and f"{name}:" in STEPS:
        return STEPS[f"{name}:"]
    raise MigrationError(
        f"unknown migration phase {phase!r} in the journal; refusing to undo it"
    )


def _steps_in(records: list[dict[str, object]]) -> list[StepRecord]:
    steps: list[StepRecord] = []
    latest: dict[str, StepRecord] = {}
    for record in records:
        phase = str(record.get("phase", ""))
        if phase in CONTROL_PHASES:
            continue
        detail = record.get("detail")
        detail = detail if isinstance(detail, dict) else {}
        if record.get("event") == "begin":
            step: StepRecord = {"phase": phase, "begin": detail, "done": None}
            steps.append(step)
            latest[phase] = step
        elif record.get("event") == "done" and phase in latest:
            latest[phase]["done"] = detail
    return steps


def _domain(record: StepRecord) -> str:
    return str(record["phase"]).partition(":")[2]


def _begin(record: StepRecord) -> dict[str, object]:
    return record["begin"]  # type: ignore[return-value]


def _done(record: StepRecord) -> dict[str, object] | None:
    return record["done"]  # type: ignore[return-value]


def _absent(record: StepRecord) -> bool:
    return bool(_begin(record).get("absent"))


def _undo_steps(state: RunState, steps: list[StepRecord]) -> None:
    """Undo ``steps`` newest-first; every phase is resolved before anything changes."""
    resolved = [(s, step_for(str(s["phase"]))) for s in steps]
    for record, step in reversed(resolved):
        step.undo(state, record)


def _run_phase(
    state: RunState, phase: str, *, begun: str | None = None, done: str | None = None
) -> dict[str, object]:
    step = step_for(phase)
    journal = state.require_journal()
    detail = step.begin(state, phase) if step.begin is not None else {}
    journal.begin(phase, **detail)
    if begun:
        _checkpoint(begun)
    result = step.apply(state, {"phase": phase, "begin": detail, "done": None})
    journal.done(phase, **result)
    if done:
        _checkpoint(done)
    return result


# ── filesystem helpers for the data phases ───────────────────────────────────


def _toolkit_root() -> Path:
    dest = _destination(agent_toolkit_paths.DOMAINS[0])
    if dest is None:
        raise MigrationError(
            "cannot resolve the toolkit home; check AGENT_TOOLKIT_HOME"
        )
    return dest.parent.parent


def _fsync_file(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _copy_durable(src: Path, dst: Path) -> None:
    """Copy ``src`` to ``dst`` without following links, fsyncing as it goes.

    Links are recreated as links. Each directory is fsynced after its
    entries exist; the caller fsyncs the parent of ``dst``.
    """
    info = src.lstat()
    if stat.S_ISLNK(info.st_mode):
        os.symlink(os.readlink(src), dst)
    elif stat.S_ISDIR(info.st_mode):
        dst.mkdir()
        for child in sorted(src.iterdir()):
            _copy_durable(child, dst / child.name)
        shutil.copystat(src, dst, follow_symlinks=False)
        _fsync_dir(dst)
    elif stat.S_ISREG(info.st_mode):
        shutil.copy2(src, dst, follow_symlinks=False)
        _fsync_file(dst)
    else:
        raise MigrationError(f"{src} is not a regular file, directory, or symlink")


def _rename_durable(src: Path, dst: Path) -> None:
    _mkdir_durable(dst.parent)
    os.rename(src, dst)
    _fsync_dir(dst.parent)
    if src.parent != dst.parent:
        _fsync_dir(src.parent)


def _remove_path(path: Path, *, within: Path) -> None:
    """Delete ``path`` (a tree or a file), which must be ``within`` or under it."""
    if path != within and not path.is_relative_to(within):
        raise MigrationError(f"refusing to delete {path}: outside {within}")
    if not _lexists(path):
        return
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink()
    _fsync_dir(path.parent)


def _place_link(dest: Path, target: str) -> None:
    """Point ``dest`` at ``target`` atomically: a temp link renamed over it."""
    _mkdir_durable(dest.parent)
    tmp = dest.parent / f".{dest.name}.migration-{os.getpid()}"
    if _lexists(tmp):
        tmp.unlink()
    os.symlink(target, tmp)
    os.replace(tmp, dest)
    _fsync_dir(dest.parent)


def _write_pointer(home: Path, layout: agent_toolkit_paths.Layout) -> None:
    agent_toolkit_paths.write_pointer(home, layout)
    agent_toolkit_paths.DEFAULT_RESOLVER.invalidate()


def _digest_tree(root: Path) -> Digests:
    """Digest every non-directory entry under ``root``, never following links."""
    if not _lexists(root):
        return {}
    digests: Digests = {}
    for rel, path, info in _walk(root):
        if stat.S_ISLNK(info.st_mode):
            digests[rel] = {"symlink": os.readlink(path)}
        elif stat.S_ISREG(info.st_mode):
            digests[rel] = _sha256(path)
        else:
            raise MigrationError(f"{path} is not a regular file, directory, or symlink")
    return digests


def _inventory_digests(state: RunState, domain: str) -> Digests:
    files = state.domain_entry(domain)["files"]
    return {
        str(f["path"]): f["sha256"] if "sha256" in f else {"symlink": f["symlink"]}
        for f in files  # type: ignore[union-attr]
    }


def _differences(root: Path, expected: Digests, current: Digests) -> list[str]:
    found: list[str] = []
    for rel in sorted(set(expected) | set(current)):
        path = root if rel == "." else root / rel
        if rel not in current:
            found.append(f"{path} (removed)")
        elif rel not in expected:
            found.append(f"{path} (added)")
        elif expected[rel] != current[rel]:
            found.append(f"{path} (changed)")
    return found


# ── the built-in steps ───────────────────────────────────────────────────────


def _no_undo(_state: RunState, _record: StepRecord) -> None:
    return None


def _always(_state: RunState, _record: StepRecord) -> bool:
    return True


def _preflight_apply(state: RunState, _record: StepRecord) -> dict[str, object]:
    if _lexists(state.work_dir):
        raise MigrationError(f"{state.work_dir} already exists")
    return {
        "warnings": state.warnings,
        "work_dir": str(state.work_dir),
        "snapshot_dir": str(state.snapshot_dir),
    }


def _inventory_apply(state: RunState, _record: StepRecord) -> dict[str, object]:
    inventory = build_inventory(state.ctx, strict=True)
    digest = write_inventory(state.require_journal().directory, inventory)
    state.inventory = inventory
    return {"sha256": digest}


def _stage_begin(state: RunState, phase: str) -> dict[str, object]:
    domain = phase.partition(":")[2]
    legacy = Path(str(state.domain_entry(domain)["legacy"]))
    return {
        "legacy": str(legacy),
        "staging": str(state.work_dir / "staging" / domain),
        "transform_dir": str(state.work_dir / "transform" / domain),
        "absent": not _lexists(legacy),
    }


def _transform_staged(
    state: RunState, domain: str, staging: Path, work: Path
) -> dict[str, object] | None:
    """Rewrite stored toolkit-data paths in one staged domain; None if nothing to do."""
    # Imported here: migrate_path_transform imports this module at load time.
    import migrate_path_transform as mpt

    def present(name: str) -> Path | None:
        path = staging / name
        return path if path.exists() else None

    stores = {
        "work-items": lambda: mpt.StoreFiles(
            root=staging,
            items=present("items.json"),
            pending=present("pending_items.json"),
        ),
        "decisions": lambda: mpt.StoreFiles(root=staging, grill_dir=staging),
        "ticket-batches": lambda: mpt.StoreFiles(root=staging, batches_dir=staging),
        "standups": lambda: mpt.StoreFiles(
            root=staging, standup_config=present("config.json")
        ),
    }.get(domain)
    if stores is None:
        return None
    try:
        plan = mpt.plan_transform(stores(), mpt.RootMap.for_home(state.ctx.home))
        if not plan.changes:
            return None
        plan_dir, manifest_sha256 = mpt.save_plan(plan, work)
        mpt.apply_saved(plan_dir, manifest_sha256)
    except mpt.TransformError as exc:
        raise MigrationError(f"path transform for {domain}: {exc}") from exc
    return {
        "plan_dir": str(plan_dir),
        "manifest_sha256": manifest_sha256,
        "paths": len(plan.paths),
        "hashes": len(plan.hashes),
        "stale": len(plan.stale),
    }


def _stage_apply(state: RunState, record: StepRecord) -> dict[str, object]:
    begin = _begin(record)
    if begin["absent"]:
        return {"absent": True}
    domain = _domain(record)
    legacy, staging = Path(str(begin["legacy"])), Path(str(begin["staging"]))
    if _lexists(staging):
        raise MigrationError(f"{staging} is occupied; refusing to stage over it")
    _mkdir_durable(staging.parent)
    _copy_durable(legacy, staging)
    _fsync_dir(staging.parent)
    _checkpoint(f"migrate.stage.{domain}.copied")
    copied = _differences(
        staging, _inventory_digests(state, domain), _digest_tree(staging)
    )
    if copied:
        raise MigrationError(
            "a staged copy differs from its inventoried source: " + "; ".join(copied)
        )
    refusals = [
        f.detail
        for f in _check_domain_contents(domain, staging)
        if f.status == "refuse"
    ]
    if refusals:
        raise MigrationError("; ".join(refusals))
    transform = _transform_staged(
        state, domain, staging, Path(str(begin["transform_dir"]))
    )
    return {"files": _digest_tree(staging), "transform": transform}


def _stage_undo(state: RunState, record: StepRecord) -> None:
    begin = _begin(record)
    if begin.get("absent"):
        return
    for key in ("staging", "transform_dir"):
        _remove_path(Path(str(begin[key])), within=state.work_dir)


def _stage_verify(_state: RunState, record: StepRecord) -> bool:
    return _absent(record) or _lexists(Path(str(_begin(record)["staging"])))


def _gathered_links(state: RunState) -> list[tuple[Path, Path, str, bool]]:
    system = os.uname().sysname
    ctx = state.ctx
    return link_inspect.gather_links(
        link_inspect.load_links(ctx.repo_root / "links.toml"),
        repo_root=ctx.repo_root,
        home=ctx.home,
        harnesses=ctx.opts.harnesses,
        is_mac=system == "Darwin",
        is_linux=system == "Linux",
        is_wsl=link_inspect.detect_wsl(system),
        profile=ctx.opts.profile,
    )


def _links_begin(state: RunState, _phase: str) -> dict[str, object]:
    """Every link to create: ``(dest, prior target or None, new target)``."""
    return {"planned": _planned_links_for(state)}


def _planned_links_for(state: RunState) -> list[dict[str, object]]:
    """The links the ``links`` phase will create or repoint; refuses a real file."""
    planned: dict[str, dict[str, object]] = {}
    for src, dest, _rel, applicable in _gathered_links(state):
        if not applicable:
            continue
        prior: str | None = None
        if dest.is_symlink():
            prior = os.readlink(dest)
            if prior == str(src):
                continue
        elif _lexists(dest):
            raise MigrationError(
                f"{dest} exists and is not a link; run ./install.sh first"
            )
        known = planned.get(str(dest))
        if known is not None and known["target"] != str(src):
            raise MigrationError(f"{dest} is claimed by two links.toml sources")
        planned[str(dest)] = {"dest": str(dest), "prior": prior, "target": str(src)}
    return list(planned.values())


def _planned_links(record: StepRecord) -> list[dict[str, object]]:
    return list(_begin(record).get("planned", []))  # type: ignore[call-overload]


def _links_apply(state: RunState, record: StepRecord) -> dict[str, object]:
    ctx = state.ctx
    for link in _planned_links(record):
        dest, target = Path(str(link["dest"])), str(link["target"])
        _place_link(dest, target)
        append_history(
            ctx.history,
            {
                "kind": "symlink-created",
                "dest": str(dest),
                "src": target,
                "migration": ctx.migration_id,
            },
        )
    retained = [
        {"dest": str(dest), "target": os.readlink(dest)}
        for dest in link_inspect.find_orphaned_links(
            _gathered_links(state),
            manifest_entries=link_inspect.read_manifest_entries(ctx.history),
        )
        if dest.is_symlink()
    ]
    return {"created": len(_planned_links(record)), "retained": retained}


def _links_undo(state: RunState, record: StepRecord) -> None:
    undone: set[str] = set()
    for link in _planned_links(record):
        dest, target = Path(str(link["dest"])), str(link["target"])
        undone.add(str(dest))
        if not dest.is_symlink() or os.readlink(dest) != target:
            continue
        prior = link.get("prior")
        if prior is None:
            dest.unlink()
            _fsync_dir(dest.parent)
        else:
            _place_link(dest, str(prior))
    migration_id = state.ctx.migration_id
    _drop_history_entries(
        state.ctx.history,
        lambda e: (
            e.get("kind") == "symlink-created"
            and e.get("migration") == migration_id
            and e.get("dest") in undone
        ),
    )


def _links_verify(_state: RunState, record: StepRecord) -> bool:
    return all(
        Path(str(link["dest"])).is_symlink()
        and os.readlink(str(link["dest"])) == link["target"]
        for link in _planned_links(record)
    )


# The uninstall baseline (depart.py) is append-only and first-layer-wins, so the
# migration never rewrites a recorded key. It appends one layer, before the
# links phase creates anything, recording each destination it is about to
# link as it is now (absent). Without it the next install would record those
# links as already present, and uninstall would leave them behind.

BASELINE_SNAPSHOT = "baseline.before.json"


def _is_linux() -> bool:
    return os.uname().sysname == "Linux"


def _baseline_tag(state: RunState) -> str:
    return f"toolkit-home-migration {state.ctx.migration_id}"


def _journal_dir(state: RunState) -> Path:
    return state.ctx.installer_state / "migrations" / state.ctx.migration_id


def _write_durable(path: Path, data: bytes, mode: int = 0o644) -> None:
    tmp = path.with_name(f".{path.name}.migration-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    try:
        _write_all(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def _baseline_bytes(baseline: depart.Baseline) -> bytes:
    """The bytes ``depart.save_baseline`` would write for ``baseline``."""
    payload = json.dumps(depart.baseline_to_dict(baseline), indent=2, sort_keys=True)
    return (payload + "\n").encode()


def _parse_baseline(path: Path) -> depart.Baseline | None:
    """The baseline at ``path``; None when missing; MigrationError when unreadable."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MigrationError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise MigrationError(f"{path} is not a baseline object")
    return depart.baseline_from_dict(data)


def _baseline_begin(state: RunState, _phase: str) -> dict[str, object]:
    path = depart.baseline_path(state.ctx.installer_state)
    detail: dict[str, object] = {
        "path": str(path),
        "existed": False,
        "sha256": None,
        "snapshot": None,
        "dests": [],
        "skipped": None,
    }
    if not _is_linux():
        detail["skipped"] = "not linux"
        return detail
    detail["dests"] = [str(link["dest"]) for link in _planned_links_for(state)]
    if _parse_baseline(path) is not None:
        raw = path.read_bytes()
        snapshot = _journal_dir(state) / BASELINE_SNAPSHOT
        _write_durable(snapshot, raw, 0o444)
        detail.update(
            existed=True,
            sha256=hashlib.sha256(raw).hexdigest(),
            snapshot=str(snapshot),
        )
    return detail


def _baseline_apply(state: RunState, record: StepRecord) -> dict[str, object]:
    begin = _begin(record)
    if begin["skipped"]:
        return {"layer": None, "sha256": None}
    path = Path(str(begin["path"]))
    baseline = _parse_baseline(path) or depart.Baseline()
    before = len(baseline.layers)
    records = depart_exec.capture_destination_records(
        [Path(str(d)) for d in begin["dests"]],  # type: ignore[attr-defined]
        home=state.ctx.home,
        state_dir=state.ctx.installer_state,
        blob_dir=None,
    )
    baseline.add_layer(_baseline_tag(state), records)
    if len(baseline.layers) == before:
        return {"layer": None, "sha256": begin["sha256"]}
    _mkdir_durable(path.parent)
    data = _baseline_bytes(baseline)
    _write_durable(path, data)
    _checkpoint("migrate.baseline.written")
    layer = baseline.layers[-1]
    return {
        "layer": {"captured_at": layer.captured_at, "records": layer.records},
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def _baseline_undo(state: RunState, record: StepRecord) -> None:
    begin = _begin(record)
    if begin.get("skipped"):
        return
    path = Path(str(begin["path"]))
    baseline = _parse_baseline(path)
    tag = _baseline_tag(state)
    if baseline is None or not any(lyr.captured_at == tag for lyr in baseline.layers):
        return
    baseline.layers = [lyr for lyr in baseline.layers if lyr.captured_at != tag]
    empty = not (baseline.layers or baseline.transactions or baseline.installed_trees)
    if not begin["existed"]:
        if empty:
            path.unlink()
            _fsync_dir(path.parent)
            return
        _write_durable(path, _baseline_bytes(baseline))
        return
    snapshot = Path(str(begin["snapshot"]))
    try:
        raw = snapshot.read_bytes()
    except OSError as exc:
        raise MigrationError(
            f"cannot read the baseline snapshot {snapshot}: {exc}"
        ) from exc
    if hashlib.sha256(raw).hexdigest() != begin["sha256"]:
        raise MigrationError(
            f"{snapshot} does not match the digest journalled for {path}; "
            "restore it by hand"
        )
    original = _parse_baseline(snapshot)
    if original is not None and depart.baseline_to_dict(
        original
    ) == depart.baseline_to_dict(baseline):
        _write_durable(path, raw)
    else:
        _write_durable(path, _baseline_bytes(baseline))


def _baseline_guard(state: RunState, record: StepRecord) -> list[str]:
    done = _done(record)
    layer = done.get("layer") if done else None
    if not isinstance(layer, dict):
        return []
    path = Path(str(_begin(record)["path"]))
    try:
        baseline = _parse_baseline(path)
    except MigrationError as exc:
        return [str(exc)]
    if baseline is None:
        return [f"{path} (deleted since the migration recorded its layer)"]
    ours = [lyr for lyr in baseline.layers if lyr.captured_at == layer["captured_at"]]
    if not ours:
        return [f"{path} (the migration's layer was removed)"]
    if ours[0].records != layer["records"]:
        return [f"{path} (the migration's layer was edited)"]
    return []


def _baseline_verify(state: RunState, record: StepRecord) -> bool:
    done = _done(record)
    if _begin(record).get("skipped") or (done is not None and done["layer"] is None):
        return True
    return not _baseline_guard(state, record) and done is not None


def _reverify_apply(state: RunState, _record: StepRecord) -> dict[str, object]:
    problems: list[str] = []
    checked = 0
    for record in _steps_in(state.records):
        done = _done(record)
        if not str(record["phase"]).startswith("stage:") or done is None:
            continue
        if _absent(record):
            continue
        staging = Path(str(_begin(record)["staging"]))
        expected: Digests = done["files"]  # type: ignore[assignment]
        problems += _differences(staging, expected, _digest_tree(staging))
        checked += len(expected)
    for domain in agent_toolkit_paths.DOMAINS:
        legacy = Path(str(state.domain_entry(domain)["legacy"]))
        expected = _inventory_digests(state, domain)
        problems += _differences(legacy, expected, _digest_tree(legacy))
        checked += len(expected)
    if problems:
        raise MigrationError(
            "changed between staging and promotion, so nothing was promoted: "
            + "; ".join(problems)
        )
    return {"checked": checked}


def _promote_begin(state: RunState, phase: str) -> dict[str, object]:
    domain = phase.partition(":")[2]
    stage = state.step(f"stage:{domain}")
    if stage is None or _done(stage) is None:
        raise MigrationError(f"{domain} was never staged")
    return {
        "staging": _begin(stage)["staging"],
        "destination": state.domain_entry(domain)["destination"],
        "absent": _absent(stage),
    }


def _promote_apply(state: RunState, record: StepRecord) -> dict[str, object]:
    begin = _begin(record)
    if begin["absent"]:
        return {"absent": True}
    staging, dest = Path(str(begin["staging"])), Path(str(begin["destination"]))
    if _lexists(dest):
        empty_dir = (
            dest.is_dir()
            and not dest.is_symlink()
            and staging.is_dir()
            and not any(dest.iterdir())
        )
        if not empty_dir:
            raise MigrationError(
                f"{dest} is occupied by something this migration did not create"
            )
    _rename_durable(staging, dest)
    stage = state.step(f"stage:{_domain(record)}")
    return {"files": _done(stage)["files"]}  # type: ignore[index]


def _promote_undo(_state: RunState, record: StepRecord) -> None:
    begin = _begin(record)
    if begin.get("absent"):
        return
    staging, dest = Path(str(begin["staging"])), Path(str(begin["destination"]))
    if _lexists(dest) and not _lexists(staging):
        _rename_durable(dest, staging)


def _promote_verify(_state: RunState, record: StepRecord) -> bool:
    begin = _begin(record)
    if begin.get("absent"):
        return True
    staging, dest = Path(str(begin["staging"])), Path(str(begin["destination"]))
    return _lexists(dest) and not _lexists(staging)


def _flip_begin(state: RunState, _phase: str) -> dict[str, object]:
    return {"pointer": str(state.ctx.home / agent_toolkit_paths.POINTER_RELPATH)}


def _flip_apply(state: RunState, _record: StepRecord) -> dict[str, object]:
    _write_pointer(state.ctx.home, "toolkit-home")
    return {"layout": "toolkit-home"}


def _flip_undo(state: RunState, _record: StepRecord) -> None:
    _write_pointer(state.ctx.home, "legacy")


def _flip_verify(_state: RunState, _record: StepRecord) -> bool:
    agent_toolkit_paths.DEFAULT_RESOLVER.invalidate()
    return agent_toolkit_paths.current_layout() == "toolkit-home"


def _snapshot_begin(state: RunState, phase: str) -> dict[str, object]:
    domain = phase.partition(":")[2]
    stage = state.step(f"stage:{domain}")
    legacy = Path(str(state.domain_entry(domain)["legacy"]))
    return {
        "legacy": str(legacy),
        "snapshot": str(state.snapshot_dir / legacy.name),
        "absent": stage is None or _absent(stage),
    }


def _snapshot_apply(_state: RunState, record: StepRecord) -> dict[str, object]:
    begin = _begin(record)
    if begin["absent"]:
        return {"absent": True}
    legacy, snapshot = Path(str(begin["legacy"])), Path(str(begin["snapshot"]))
    if _lexists(snapshot):
        raise MigrationError(f"{snapshot} already exists")
    _rename_durable(legacy, snapshot)
    return {}


def _snapshot_undo(_state: RunState, record: StepRecord) -> None:
    begin = _begin(record)
    if begin.get("absent"):
        return
    legacy, snapshot = Path(str(begin["legacy"])), Path(str(begin["snapshot"]))
    if _lexists(snapshot):
        if _lexists(legacy):
            raise MigrationError(
                f"{legacy} was recreated while its original is still in {snapshot}; "
                "resolve it by hand"
            )
        _rename_durable(snapshot, legacy)
    parent = snapshot.parent
    if parent.is_dir() and not parent.is_symlink() and not any(parent.iterdir()):
        parent.rmdir()
        _fsync_dir(parent.parent)


def _snapshot_verify(_state: RunState, record: StepRecord) -> bool:
    begin = _begin(record)
    if begin.get("absent"):
        return True
    legacy, snapshot = Path(str(begin["legacy"])), Path(str(begin["snapshot"]))
    return _lexists(snapshot) and not _lexists(legacy)


def _validation_commands(ctx: MigrationContext) -> list[list[str]]:
    return [
        [
            sys.executable,
            str(ctx.repo_root / "agent-scripts" / "dev_status.py"),
            "validate",
        ],
        [
            str(ctx.repo_root / "install.sh"),
            "--check-links",
            f"--harness={','.join(ctx.opts.harnesses)}",
            f"--profile={ctx.opts.profile}",
        ],
    ]


def _run_validation(ctx: MigrationContext) -> tuple[bool, list[dict[str, object]]]:
    """Validate the new layout in fresh processes. Any failure to run is a failure."""
    results: list[dict[str, object]] = []
    passed = True
    for command in _validation_commands(ctx):
        try:
            proc = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=VALIDATION_TIMEOUT,
                check=False,
            )
            code: int | None = proc.returncode
            output = (proc.stdout + proc.stderr)[-4000:]
        except Exception as exc:  # noqa: BLE001 — a validator that cannot run fails
            code, output = None, f"{type(exc).__name__}: {exc}"
        results.append({"command": command, "exit": code, "output": output})
        passed = passed and code == 0
    return passed, results


def _validate_apply(state: RunState, _record: StepRecord) -> dict[str, object]:
    passed, results = _run_validation(state.ctx)
    return {"passed": passed, "results": results}


def _validate_verify(_state: RunState, record: StepRecord) -> bool:
    done = _done(record)
    return done is not None and bool(done.get("passed"))


register_step("preflight", Step(_preflight_apply, _no_undo, _always))
register_step("inventory", Step(_inventory_apply, _no_undo, _always))
register_step("stage:", Step(_stage_apply, _stage_undo, _stage_verify, _stage_begin))
register_step(
    "baseline",
    Step(
        _baseline_apply,
        _baseline_undo,
        _baseline_verify,
        _baseline_begin,
        _baseline_guard,
    ),
)
register_step("links", Step(_links_apply, _links_undo, _links_verify, _links_begin))
register_step("reverify", Step(_reverify_apply, _no_undo, _always))
register_step(
    "promote:", Step(_promote_apply, _promote_undo, _promote_verify, _promote_begin)
)
register_step("flip", Step(_flip_apply, _flip_undo, _flip_verify, _flip_begin))
register_step(
    "snapshot:",
    Step(_snapshot_apply, _snapshot_undo, _snapshot_verify, _snapshot_begin),
)
register_step("validate", Step(_validate_apply, _no_undo, _validate_verify))


# ── run, restore, recover ────────────────────────────────────────────────────


def _before_flip(state: RunState) -> None:
    domains = agent_toolkit_paths.DOMAINS
    _run_phase(
        state,
        "preflight",
        begun="migrate.preflight.begun",
        done="migrate.preflight.done",
    )
    _run_phase(
        state,
        "inventory",
        begun="migrate.inventory.begun",
        done="migrate.inventory.done",
    )
    for d in domains:
        _run_phase(
            state,
            f"stage:{d}",
            begun=f"migrate.stage.{d}.begun",
            done=f"migrate.stage.{d}.done",
        )
    _run_phase(
        state, "baseline", begun="migrate.baseline.begun", done="migrate.baseline.done"
    )
    _run_phase(state, "links", begun="migrate.links.begun", done="migrate.links.done")
    for phase in RUNTIME_PHASES:
        _run_phase(state, phase)
    _run_phase(
        state, "reverify", begun="migrate.reverify.begun", done="migrate.reverify.done"
    )
    for d in domains:
        _run_phase(
            state,
            f"promote:{d}",
            begun=f"migrate.promote.{d}.before",
            done=f"migrate.promote.{d}.done",
        )
    _run_phase(state, "flip", begun="migrate.flip.before", done="migrate.flip.done")


def _finish_snapshots(state: RunState) -> None:
    """Snapshot every domain not yet snapshotted; a begun one is verified first."""
    journal = state.require_journal()
    for d in agent_toolkit_paths.DOMAINS:
        phase = f"snapshot:{d}"
        existing = state.step(phase)
        if existing is None:
            _run_phase(
                state,
                phase,
                begun=f"migrate.snapshot.{d}.before",
                done=f"migrate.snapshot.{d}.done",
            )
        elif _done(existing) is None:
            step = step_for(phase)
            detail = {} if step.verify(state, existing) else step.apply(state, existing)
            journal.done(phase, **detail)


def _validated(state: RunState) -> bool:
    """Validate, unless a completed validation already passed."""
    validated = state.step("validate")
    if validated is not None and _validate_verify(state, validated):
        return True
    result = _run_phase(
        state, "validate", begun="migrate.validate.begun", done="migrate.validate.done"
    )
    return bool(result["passed"])


def _after_flip(state: RunState, *, lock_held: bool) -> tuple[str, str]:
    """Finish snapshots and validation, then commit or restore. (outcome, reason)."""
    journal = state.require_journal()
    try:
        _finish_snapshots(state)
        passed = _validated(state)
    except JournalError:
        raise
    except Exception as exc:  # noqa: BLE001 — any failure after the flip restores
        reason = str(exc)
    else:
        if passed:
            journal.end(OUTCOME_COMMITTED)
            _checkpoint("migrate.journal.ended")
            return OUTCOME_COMMITTED, "every domain moved; the layout is toolkit-home"
        reason = "validation of the new layout failed"
    restore(state, guarded=not lock_held, label="restore")
    journal.end(OUTCOME_RESTORED, reason=reason)
    _checkpoint("migrate.restore.ended")
    return OUTCOME_RESTORED, reason


def _write_guard(state: RunState) -> list[str]:
    """What changed in the toolkit home since promotion (telemetry exempt)."""
    problems: list[str] = []
    for record in _steps_in(state.records):
        domain = _domain(record)
        done = _done(record)
        if domain in TELEMETRY_DOMAINS or done is None:
            continue
        phase = str(record["phase"])
        if phase.startswith("promote:") and not _absent(record):
            dest = Path(str(_begin(record)["destination"]))
            problems += _differences(dest, done["files"], _digest_tree(dest))  # type: ignore[arg-type]
        elif phase.startswith("stage:") and _absent(record):
            dest = Path(str(state.domain_entry(domain)["destination"]))
            if _lexists(dest):
                problems.append(f"{dest} (created after the migration)")
    for record in _steps_in(state.records):
        guard = step_for(str(record["phase"])).guard
        if guard is not None:
            problems += guard(state, record)
    return problems


def _set_aside(path: Path, target: Path) -> None:
    if not _lexists(path):
        return
    if _lexists(target):
        raise MigrationError(f"{target} already exists; cannot set {path} aside")
    _rename_durable(path, target)


def restore(
    state: RunState, *, guarded: bool, label: str, resume: bool = False
) -> None:
    """Return to the legacy layout, keeping every promoted domain aside.

    Pointer first, then the promoted domains move to ``restored/``, then every
    step is undone newest-first (the snapshots go back to their legacy paths
    and the new links are undone). ``guarded`` runs the write guard first
    and refuses with no change; ``resume`` finishes a restore already begun.
    """
    journal = state.require_journal()
    steps = _steps_in(state.records)
    for record in steps:
        step_for(str(record["phase"]))
    if not resume:
        if guarded:
            problems = _write_guard(state)
            if problems:
                raise RestoreRefused(
                    "the toolkit home changed after the migration, so nothing was "
                    "restored; decide by hand: " + "; ".join(problems)
                )
        journal.begin("restore", guarded=guarded)
    _checkpoint(f"migrate.{label}.begun")
    _write_pointer(state.ctx.home, "legacy")
    _checkpoint(f"migrate.{label}.pointer")
    aside = state.work_dir / "restored"
    for record in steps:
        domain = _domain(record)
        if _done(record) is None:
            continue
        phase = str(record["phase"])
        if phase.startswith("promote:") and not _absent(record):
            _set_aside(Path(str(_begin(record)["destination"])), aside / domain)
        elif (
            phase.startswith("stage:")
            and _absent(record)
            and domain in TELEMETRY_DOMAINS
        ):
            dest = Path(str(state.domain_entry(domain)["destination"]))
            _set_aside(dest, aside / domain)
    _checkpoint(f"migrate.{label}.promoted-aside")
    _undo_steps(state, steps)
    _checkpoint(f"migrate.{label}.undone")
    journal.done("restore")


def _load_state(
    directory: Path,
    journal: Journal | None,
    records: list[dict[str, object]],
    repo_root: Path,
    history: Path,
) -> RunState:
    """Rebuild a run's state from its journal directory alone."""
    inventory = json.loads((directory / INVENTORY_NAME).read_text(encoding="utf-8"))
    preflight = next(
        (
            r["detail"]
            for r in records
            if r.get("phase") == "preflight" and r.get("event") == "done"
        ),
        None,
    )
    if not isinstance(preflight, dict) or "work_dir" not in preflight:
        raise MigrationError(f"{directory} records no migration paths")
    ctx = MigrationContext(
        opts=MigrationOptions(
            harnesses=tuple(inventory["harnesses"]),
            profile=str(inventory["profile"]),
        ),
        migration_id=directory.name,
        home=Path.home(),
        repo_root=repo_root,
        installer_state=history.parent,
        history=history,
    )
    return RunState(
        ctx=ctx,
        journal=journal,
        records=records,
        work_dir=Path(str(preflight["work_dir"])),
        snapshot_dir=Path(str(preflight["snapshot_dir"])),
        inventory=inventory,
    )


def _has_data_phases(records: list[dict[str, object]]) -> bool:
    return any(
        r.get("phase") not in ("preflight", "inventory", *CONTROL_PHASES)
        for r in records
    )


def _recover_data_run(
    directory: Path, repo_root: Path, history: Path
) -> tuple[str, Action]:
    """Finish a crashed run by the flip rule. Returns (outcome, action)."""
    journal = Journal.resume(directory)
    try:
        state = _load_state(directory, journal, journal.records, repo_root, history)
        restoring = [
            r.get("event") for r in journal.records if r.get("phase") == "restore"
        ]
        if "begin" in restoring:
            if "done" not in restoring:
                restore(state, guarded=False, label="restore", resume=True)
            journal.end(OUTCOME_RESTORED, reason="finished an interrupted restore")
            return OUTCOME_RESTORED, "restore"
        flip = state.step("flip")
        if flip is not None and _done(flip) is None and _flip_verify(state, flip):
            journal.done("flip", layout="toolkit-home", recovered=True)
            flip = state.step("flip")
        if flip is None or _done(flip) is None:
            _undo_steps(state, _steps_in(journal.records))
            journal.abandon("rolled back: the run died before the layout flip")
            return "abandoned", "rollback"
        outcome, _reason = _after_flip(state, lock_held=False)
        return outcome, "roll-forward" if outcome == OUTCOME_COMMITTED else "restore"
    finally:
        journal.close()


def recover(
    installer_state: Path, history: Path, *, repo_root: Path | None = None
) -> list[dict[str, str]]:
    """Finish or undo whatever crashed runs and commands left; repair history."""
    root = repo_root or Path(__file__).resolve().parent
    actions: list[dict[str, str]] = []
    known = history_ids(history)
    for directory in _journal_dirs(installer_state):
        records = read_records(directory)
        outcome: str | None = None
        action: Action = "none"
        if not is_terminal(directory):
            if _has_data_phases(records):
                outcome, action = _recover_data_run(directory, root, history)
            else:
                abandon(
                    directory, "the run that wrote this journal died before finishing"
                )
                outcome, action = "abandoned", "unwind"
        elif directory.name not in known:
            outcome = _outcome(records) or str(records[-1].get("event"))
            action = "retry"
        if outcome is not None:
            append_history(
                history,
                {
                    "kind": "migration",
                    "id": directory.name,
                    "outcome": outcome,
                    "journal": str(directory / JOURNAL_NAME),
                },
            )
            actions.append({"id": directory.name, "action": action})
        for name, label in (
            (ROLLBACK_NAME, "resume-rollback"),
            (FINALIZE_NAME, "resume-finalize"),
        ):
            if (directory / name).exists() and not is_terminal(directory, name):
                _resume_post_commit(directory, name, root, history)
                actions.append({"id": directory.name, "action": label})
    return actions


# ── rollback and finalize of a committed migration ───────────────────────────


def _rollback(state: RunState, journal: Journal | None = None) -> None:
    for record in _steps_in(state.records):
        step_for(str(record["phase"]))
    problems = _write_guard(state)
    if problems:
        raise RestoreRefused(
            "the toolkit home changed after the migration, so nothing was rolled "
            "back; decide by hand: " + "; ".join(problems)
        )
    directory = state.ctx.installer_state / "migrations" / state.ctx.migration_id
    state.journal = journal or Journal.create(directory, ROLLBACK_NAME)
    try:
        restore(state, guarded=False, label="rollback")
        state.journal.end(OUTCOME_ROLLED_BACK)
        _checkpoint("migrate.rollback.ended")
    finally:
        state.journal.close()


def _finalize_plan(state: RunState) -> dict[str, object]:
    """What finalize deletes; refuses, changing nothing, if the snapshot drifted."""
    problems: list[str] = []
    expected_names: set[str] = set()
    for record in _steps_in(state.records):
        if not str(record["phase"]).startswith("snapshot:") or _absent(record):
            continue
        snapshot = Path(str(_begin(record)["snapshot"]))
        expected_names.add(snapshot.name)
        expected = _inventory_digests(state, _domain(record))
        problems += _differences(snapshot, expected, _digest_tree(snapshot))
    if state.snapshot_dir.is_dir():
        for child in sorted(state.snapshot_dir.iterdir()):
            if child.name not in expected_names:
                problems.append(f"{child} (not part of the snapshot)")
    if problems:
        raise MigrationError(
            "the snapshot no longer matches the journal, so nothing was deleted: "
            + "; ".join(problems)
        )
    links = state.step("links")
    done = _done(links) if links is not None else None
    return {
        "snapshot_dir": str(state.snapshot_dir),
        "work_dir": str(state.work_dir),
        "retained": (done or {}).get("retained", []),
    }


def _finalize_apply(state: RunState, plan: dict[str, object]) -> None:
    snapshot_dir = Path(str(plan["snapshot_dir"]))
    work_dir = Path(str(plan["work_dir"]))
    _remove_path(snapshot_dir, within=snapshot_dir)
    _checkpoint("migrate.finalize.snapshot-removed")
    for leftover in ("staging", "transform"):
        _remove_path(work_dir / leftover, within=work_dir)
    if work_dir.is_dir() and not any(work_dir.iterdir()):
        work_dir.rmdir()
        _fsync_dir(work_dir.parent)
    _checkpoint("migrate.finalize.staging-removed")
    gone: set[tuple[str, str]] = set()
    for link in plan["retained"]:  # type: ignore[attr-defined]
        dest, target = Path(str(link["dest"])), str(link["target"])
        if dest.is_symlink() and os.readlink(dest) == target:
            dest.unlink()
            _fsync_dir(dest.parent)
        if not (dest.is_symlink() and os.readlink(dest) == target):
            gone.add((str(dest), target))
    _drop_history_entries(
        state.ctx.history,
        lambda e: (
            e.get("kind") == "symlink-created" and (e.get("dest"), e.get("src")) in gone
        ),
    )
    _checkpoint("migrate.finalize.links-removed")


def _finalize(state: RunState, journal: Journal | None = None) -> None:
    plan = _finalize_plan(state)
    directory = state.ctx.installer_state / "migrations" / state.ctx.migration_id
    state.journal = journal or Journal.create(directory, FINALIZE_NAME)
    try:
        state.journal.begin("finalize", **plan)
        _checkpoint("migrate.finalize.begun")
        _finalize_apply(state, plan)
        state.journal.done("finalize")
        state.journal.end(OUTCOME_FINALIZED)
        _checkpoint("migrate.finalize.ended")
    finally:
        state.journal.close()


def _resume_post_commit(
    directory: Path, name: str, repo_root: Path, history: Path
) -> None:
    """Finish a rollback or finalize that a crash interrupted."""
    journal = Journal.resume(directory, name)
    state = _load_state(directory, journal, read_records(directory), repo_root, history)
    events = {
        (r.get("phase"), r.get("event"))
        for r in journal.records
        if r.get("phase") in ("restore", "finalize")
    }
    if name == ROLLBACK_NAME:
        if ("restore", "begin") not in events:
            _rollback(state, journal)
            return
        try:
            if ("restore", "done") not in events:
                restore(state, guarded=False, label="rollback", resume=True)
            journal.end(OUTCOME_ROLLED_BACK)
        finally:
            journal.close()
        return
    begin = next(
        (
            r["detail"]
            for r in journal.records
            if r.get("phase") == "finalize" and r.get("event") == "begin"
        ),
        None,
    )
    if not isinstance(begin, dict):
        _finalize(state, journal)
        return
    try:
        if ("finalize", "done") not in events:
            _finalize_apply(state, begin)
            journal.done("finalize")
        journal.end(OUTCOME_FINALIZED)
    finally:
        journal.close()


def _open_committed(directory: Path, kind: str, other: str) -> list[dict[str, object]]:
    """The migration journal of a committed run the other command has not closed."""
    records = read_records(directory)
    if _outcome(records) != OUTCOME_COMMITTED:
        raise MigrationError(f"{directory.name} is not a committed migration")
    if (directory / other).exists():
        was = "finalized" if other == FINALIZE_NAME else "rolled back"
        raise MigrationError(
            f"{directory.name} was already {was}; it cannot be "
            f"{'rolled back' if kind == 'rollback' else 'finalized'}"
        )
    return records


def _post_commit(
    kind: Literal["rollback", "finalize"],
    migration_id: str,
    opts: MigrationOptions,
    *,
    repo_root: Path,
) -> int:
    history = link_inspect.manifest_path(Path.home())
    directory = history.parent / "migrations" / migration_id
    name, other = (
        (ROLLBACK_NAME, FINALIZE_NAME)
        if kind == "rollback"
        else (FINALIZE_NAME, ROLLBACK_NAME)
    )
    report = PreflightReport(migration_id=migration_id, dry_run=False)
    try:
        with migration_lock.exclusive(LOCK_SITE, blocking=False):
            report.recovered = recover(history.parent, history, repo_root=repo_root)
            records = _open_committed(directory, kind, other)
            report.journal = str(directory / name)
            if (directory / name).exists():
                report.outcome = f"{kind}: already done"
            else:
                state = _load_state(directory, None, records, repo_root, history)
                if kind == "rollback":
                    _rollback(state)
                    report.outcome = (
                        f"{OUTCOME_ROLLED_BACK}: the layout is legacy again"
                    )
                else:
                    _finalize(state)
                    report.outcome = f"{OUTCOME_FINALIZED}: the legacy snapshot is gone"
    except migration_lock.MigrationLockBusy as exc:
        report.outcome = f"lock-busy: {exc}"
        _emit(report, opts)
        return migration_lock.REFUSAL_EXIT_CODE
    except (MigrationError, OSError) as exc:
        report.outcome = f"failed: {exc}"
        _emit(report, opts)
        return 1
    _emit(report, opts)
    return 0


def rollback_command(
    migration_id: str, opts: MigrationOptions, *, repo_root: Path
) -> int:
    """``--rollback-toolkit-home-migration``: reverse one committed migration."""
    return _post_commit("rollback", migration_id, opts, repo_root=repo_root)


def finalize_command(
    migration_id: str, opts: MigrationOptions, *, repo_root: Path
) -> int:
    """``--finalize-toolkit-home-migration``: delete one migration's leftovers."""
    return _post_commit("finalize", migration_id, opts, repo_root=repo_root)


def committed_unfinalized(installer_state: Path) -> list[str]:
    """Ids of committed migrations neither finalized nor rolled back.

    A journal that cannot be read counts as one: the broad rollback this
    guards must not proceed on a guess.
    """
    ids: list[str] = []
    for directory in _journal_dirs(installer_state):
        try:
            if _outcome(read_records(directory)) != OUTCOME_COMMITTED:
                continue
            closed = (
                _outcome(read_records(directory, FINALIZE_NAME)) == OUTCOME_FINALIZED
                or _outcome(read_records(directory, ROLLBACK_NAME))
                == OUTCOME_ROLLED_BACK
            )
        except JournalCorrupt:
            closed = False
        if not closed:
            ids.append(directory.name)
    return ids


def retained_legacy_links(installer_state: Path) -> set[Path]:
    """Legacy links kept for unfinalized migrations; orphan cleanup must skip them."""
    kept: set[Path] = set()
    for directory in _journal_dirs(installer_state):
        if _outcome(read_records(directory, FINALIZE_NAME)) == OUTCOME_FINALIZED:
            continue
        for record in _steps_in(read_records(directory)):
            done = _done(record)
            if record["phase"] != "links" or done is None:
                continue
            for link in done.get("retained", []):  # type: ignore[attr-defined]
                kept.add(Path(str(link["dest"])))
    return kept


# ── executor ─────────────────────────────────────────────────────────────────


def _emit(report: PreflightReport, opts: MigrationOptions) -> None:
    if opts.json_report:
        print(json.dumps(report.to_json(), indent=2, sort_keys=True))
        return
    stream = sys.stdout
    if not opts.quiet or report.refused:
        for finding in report.findings:
            if finding.status == "ok" and not opts.verbose:
                continue
            print(
                f"  [{finding.status}] {finding.check}: {finding.detail}", file=stream
            )
            for path in finding.paths:
                print(f"      {path}", file=stream)
    for item in report.recovered:
        print(f"  recovered {item['id']}: {item['action']}", file=stream)
    print(f"{report.outcome}  (migration {report.migration_id})", file=stream)
    if report.journal:
        print(f"journal: {report.journal}", file=stream)


def _inventory_summary(inventory: dict[str, object]) -> dict[str, int]:
    files = [f for d in inventory["domains"].values() for f in d["files"]]  # type: ignore[union-attr]
    return {
        "files": len(files),
        "bytes": sum(int(f.get("size", 0)) for f in files),
    }


def run(opts: MigrationOptions, *, repo_root: Path) -> int:
    """Run the command. Returns the process exit code."""
    home = Path.home()
    history = link_inspect.manifest_path(home)
    ctx = MigrationContext(
        opts=opts,
        migration_id=opts.migration_id or new_migration_id(),
        home=home,
        repo_root=repo_root,
        installer_state=history.parent,
        history=history,
    )
    report = PreflightReport(migration_id=ctx.migration_id, dry_run=opts.dry_run)
    report.findings = run_checks(ctx)

    if opts.dry_run:
        if not any(
            f.check == "inventory-types" and f.status == "refuse"
            for f in report.findings
        ):
            report.inventory = build_inventory(ctx, strict=False)
        report.outcome = "dry-run:refused" if report.refused else "dry-run:ok"
        if report.inventory is not None and not opts.json_report and not opts.quiet:
            summary = _inventory_summary(report.inventory)
            print(f"inventory: {summary['files']} files, {summary['bytes']} bytes")
        _emit(report, opts)
        return 1 if report.refused else 0

    if report.refused:
        report.outcome = "refused"
        _emit(report, opts)
        return 1

    try:
        with migration_lock.exclusive(LOCK_SITE, blocking=False):
            _checkpoint("migrate.lock.taken")
            report.recovered = recover(
                ctx.installer_state, ctx.history, repo_root=ctx.repo_root
            )
            report.findings = run_checks(ctx)
            if report.refused:
                report.outcome = "refused"
                _emit(report, opts)
                return 1
            journal = Journal.open(ctx.installer_state, ctx.migration_id)
            report.journal = str(journal.directory / JOURNAL_NAME)
            try:
                outcome, reason = _run_journalled(ctx, journal, report)
            finally:
                journal.close()
            append_history(
                ctx.history,
                {
                    "kind": "migration",
                    "id": ctx.migration_id,
                    "outcome": outcome,
                    "journal": report.journal,
                },
            )
            _checkpoint("migrate.history.written")
    except migration_lock.MigrationLockBusy as exc:
        report.outcome = f"lock-busy: {exc}"
        _emit(report, opts)
        return migration_lock.REFUSAL_EXIT_CODE
    except (MigrationError, OSError) as exc:
        report.outcome = f"failed: {exc}"
        _emit(report, opts)
        return 1
    report.outcome = f"{outcome}: {reason}"
    _emit(report, opts)
    return 0 if outcome == OUTCOME_COMMITTED else 1


def _run_journalled(
    ctx: MigrationContext, journal: Journal, report: PreflightReport
) -> tuple[str, str]:
    """Every phase in order. Returns (outcome, reason); raises after an abort."""
    state = RunState(
        ctx=ctx,
        journal=journal,
        records=journal.records,
        work_dir=_toolkit_root() / f".migration-{ctx.migration_id}",
        snapshot_dir=ctx.home
        / ".claude"
        / "data"
        / f".toolkit-home-snapshot-{ctx.migration_id}",
        warnings=sorted({f.check for f in report.findings if f.status == "warn"}),
    )
    try:
        _before_flip(state)
    except JournalError:
        raise
    except Exception as exc:
        report.inventory = state.inventory or None
        try:
            _undo_steps(state, _steps_in(state.records))
        except Exception as undo_exc:
            raise MigrationError(
                f"{exc}; undoing the run also failed, so the next run will retry: "
                f"{undo_exc}"
            ) from undo_exc
        if not journal.terminal and not journal.poisoned:
            journal.end(OUTCOME_ABORTED, error=str(exc))
        if isinstance(exc, (MigrationError, OSError)):
            raise
        raise MigrationError(f"{type(exc).__name__}: {exc}") from exc
    report.inventory = state.inventory
    return _after_flip(state, lock_held=True)
