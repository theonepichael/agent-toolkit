#!/usr/bin/env python3
"""The toolkit-home migration: move the toolkit data domains, recoverably.

Run from the repository checkout through ``./install.sh``; never installed.

* ``--migrate-toolkit-home --harness=<list>`` moves the seven data domains
  from ``~/.claude/data`` to the toolkit home and repoints the runtime links.
* ``--rollback-toolkit-home-migration=<id>`` reverses one committed migration
  before it is finalized.
* ``--finalize-toolkit-home-migration=<id>`` deletes the journal-proven
  leftovers of a committed migration, or the retained transformed copy of a
  rolled-back or restored migration after checking it for changes.

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
the promoted domains move to ``.migration-<id>/restored/<domain>`` (kept
until an explicit finalize), every other step is undone newest-first, and the run ends
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
Finalize after restore checks non-telemetry files against the promotion
journal, reports discarded telemetry lines, and removes only this migration's
work-tree leftovers. Old journals without telemetry line counts report an
unknown count rather than guessing.

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
import contextlib
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
from collections.abc import Callable, Iterator, Sequence
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
    manual_edit_checklist: list[str] = field(default_factory=list)
    telemetry_lines_discarded: dict[str, int | None] = field(default_factory=dict)
    stale_references: list[dict[str, str]] = field(default_factory=list)
    recovered_stale_references: list[dict[str, str]] = field(default_factory=list)
    legacy_dirs_kept: list[str] = field(default_factory=list)
    unclassified_left: list[str] = field(default_factory=list)
    unfinished_migrations: list[str] = field(default_factory=list)

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
            "manual_edit_checklist": self.manual_edit_checklist,
            "telemetry_lines_discarded": self.telemetry_lines_discarded,
            "stale_references": self.stale_references,
            "recovered_stale_references": self.recovered_stale_references,
            "legacy_dirs_kept": self.legacy_dirs_kept,
            "unclassified_left": self.unclassified_left,
            "unfinished_migrations": self.unfinished_migrations,
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
_FINALIZE_STEPS = (
    "begun",
    "snapshot-removed",
    "staging-removed",
    "links-removed",
    "legacy-dir-removed",
)
_RESTORED_FINALIZE_STEPS = (
    "restored-begun",
    "restored-removed",
    "restored-leftovers-removed",
)

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
    "migrate.settings-rewrite.begun",
    "migrate.settings-rewrite.done",
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
    *(f"migrate.finalize.{s}" for s in _RESTORED_FINALIZE_STEPS),
    "migrate.finalize.restored-ended",
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
        "migrate.settings-rewrite.begun",
        "migrate.settings-rewrite.done",
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
            "resume-finalize",
            True,
            event(f"migrate.finalize.{step}"),
            True,
            True,
            "toolkit-home",
            FINALIZE_NAME,
        )
    # staging-removed and links-removed fire after the per-entry
    # snapshot-removed steps journal their done records, so the journal's
    # last event at those crash points is 'done'.
    rows["migrate.finalize.staging-removed"] = Expected(
        "resume-finalize", True, "done", True, True, "toolkit-home", FINALIZE_NAME
    )
    rows["migrate.finalize.links-removed"] = Expected(
        "resume-finalize", True, "done", True, True, "toolkit-home", FINALIZE_NAME
    )
    # legacy-dir-removed fires after the links cleanup, before any per-dir
    # removal, so the journal's last event there is also a 'done'.
    rows["migrate.finalize.legacy-dir-removed"] = Expected(
        "resume-finalize", True, "done", True, True, "toolkit-home", FINALIZE_NAME
    )
    for step in _RESTORED_FINALIZE_STEPS:
        rows[f"migrate.finalize.{step}"] = Expected(
            "resume-finalize", True, "begin", True, True, "legacy", FINALIZE_NAME
        )
    rows["migrate.finalize.restored-ended"] = Expected(
        "none", True, "end", True, True, "legacy", FINALIZE_NAME
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


def _lstat_no_symlink(root: Path) -> None:
    """Refuse a symlinked ``root`` or any of its ancestors, before any walk.

    ``is_dir()``/``iterdir()`` follow symlinks, so a symlinked legacy data
    root would make carry and snapshot operate on an external tree; the
    unlocked advisory pass runs before the lock, so this cheap lstat walk is
    what stops it, not the later locked refusal.
    """
    path = root
    while path.parent != path:
        if path.is_symlink():
            raise MigrationError(f"{path} is a symlink; refusing to traverse it")
        path = path.parent


def _shape(root: Path) -> tuple[str, ...]:
    """Sorted relative paths of every directory under ``root`` (``root`` itself excluded).

    ``_digest_tree`` records files and symlinks but not directories, so a
    shape record travels beside it wherever digest drift must also catch an
    added or removed directory.
    """
    dirs: list[str] = []
    stack = [root]
    while stack:
        path = stack.pop()
        for child in sorted(path.iterdir()):
            if child.is_dir() and not child.is_symlink():
                dirs.append(str(child.relative_to(root)))
                stack.append(child)
    return tuple(dirs)


def _tree_differences(
    root: Path, expected_files: Digests, expected_shape: Sequence[str]
) -> list[str]:
    """Digest drift AND directory-shape drift of ``root`` against the journal."""
    problems = _differences(root, expected_files, _digest_tree(root))
    if _lexists(root) and root.is_dir() and not root.is_symlink():
        current = _shape(root)
        for added in sorted(set(current) - set(expected_shape)):
            problems.append(f"{root / added} (directory added)")
        for removed in sorted(set(expected_shape) - set(current)):
            problems.append(f"{root / removed} (directory removed)")
    return problems


def _unclassified_entries(data_root: Path) -> tuple[list[Path], list[Path], list[Path]]:
    """Unclassified entries under the legacy data root, split by shape.

    Returns (carry candidates, empty directories, snapshot directories).
    Snapshot directories are never carry candidates; they belong to an
    older migration's finalize.
    """
    known = {_legacy(d).name for d in agent_toolkit_paths.DOMAINS}
    known.add(agent_toolkit_paths.POINTER_RELPATH.name)
    carries: list[Path] = []
    empties: list[Path] = []
    stale: list[Path] = []
    if not data_root.is_dir() or data_root.is_symlink():
        return carries, empties, stale
    for path in sorted(data_root.iterdir()):
        if path.name in known:
            continue
        if path.name.startswith(".toolkit-home-snapshot-"):
            stale.append(path)
            continue
        if path.is_dir() and not path.is_symlink():
            if any(path.iterdir()):
                carries.append(path)
            else:
                empties.append(path)
        else:
            carries.append(path)
    return carries, empties, stale


def _check_unclassified(ctx: MigrationContext) -> Finding:
    data_root = ctx.home / ".claude" / "data"
    if not data_root.is_dir():
        return Finding("unclassified", "ok", "no legacy data root")
    known = {_legacy(d).name for d in agent_toolkit_paths.DOMAINS}
    extra = [
        p
        for p in data_root.iterdir()
        if p.name not in known and p.name != agent_toolkit_paths.POINTER_RELPATH.name
    ]
    for entry in extra:
        if entry.is_symlink():
            return Finding(
                "unclassified",
                "refuse",
                "an unclassified legacy entry is a symlink; refusing to carry it",
                [str(entry)],
            )
        if entry.is_dir() and not entry.is_symlink():
            stack = [entry]
            while stack:
                current = stack.pop()
                for child in sorted(current.iterdir()):
                    if child.is_symlink():
                        return Finding(
                            "unclassified",
                            "refuse",
                            "a symlink inside an unclassified legacy tree; its "
                            "target may sit under a moved root, so the carry "
                            "refuses",
                            [str(child)],
                        )
                    if child.is_dir():
                        stack.append(child)
    stale = [p for p in extra if p.name.startswith(".toolkit-home-snapshot-")]
    if stale:
        return Finding(
            "unclassified",
            "refuse",
            "a toolkit-home snapshot directory still sits at the legacy data "
            "root; finalize the migration it belongs to first",
            sorted(str(p) for p in stale),
        )
    if not extra:
        return Finding("unclassified", "ok", "nothing unclassified")

    dest_data = _toolkit_root() / "data"
    collisions: list[str] = []
    pairs: list[str] = []
    for p in extra:
        dest = dest_data / p.name
        pairs.append(f"{p} -> {dest}")
        if _lexists(dest):
            collisions.append(str(dest))

    if collisions:
        return Finding(
            "unclassified",
            "refuse",
            "destination collision for unclassified data",
            sorted(collisions),
        )

    return Finding(
        "unclassified",
        "warn",
        f"will be carried to toolkit home: {dest_data}",
        pairs,
    )


def _check_path_env_vars(ctx: MigrationContext) -> Finding:
    flagged: list[str] = []

    guard_store = os.environ.get("GUARD_RAILS_STORE")
    if guard_store:
        try:
            resolved = Path(os.path.expanduser(guard_store)).resolve()
            legacy_data = (ctx.home / ".claude" / "data").resolve()
            if resolved.is_relative_to(legacy_data):
                flagged.append(f"GUARD_RAILS_STORE={guard_store}")
        except Exception:
            pass

    swarm_path = os.environ.get("COPILOT_SWARM_DEV_STATUS_PATH")
    if swarm_path:
        try:
            resolved = Path(os.path.expanduser(swarm_path)).resolve()
            legacy_scripts = (ctx.home / ".claude" / "scripts").resolve()
            if resolved.is_relative_to(legacy_scripts):
                flagged.append(f"COPILOT_SWARM_DEV_STATUS_PATH={swarm_path}")
        except Exception:
            pass

    if flagged:
        return Finding(
            "path-env-vars",
            "warn",
            "environment variable points to a path under a moved root; update by hand",
            flagged,
        )
    return Finding(
        "path-env-vars", "ok", "no path-valued environment variables under moved roots"
    )


def _check_swarm_state(ctx: MigrationContext) -> Finding:
    paths: list[str] = []
    pi_env = os.environ.get("PI_SWARM_STATE_DIR")
    pi_dir = (
        Path(os.path.expanduser(pi_env))
        if pi_env
        else ctx.home / ".pi" / "agent" / "state"
    )
    if pi_dir.is_dir():
        for p in pi_dir.glob("swarm-*.json"):
            paths.append(str(p))

    copilot_env = os.environ.get("COPILOT_SWARM_STATE_DIR")
    copilot_dir = (
        Path(os.path.expanduser(copilot_env))
        if copilot_env
        else ctx.home / ".copilot" / "state"
    )
    if copilot_dir.is_dir():
        for p in copilot_dir.glob("swarm-*.json"):
            paths.append(str(p))

    if paths:
        return Finding(
            "swarm-state",
            "warn",
            "toolkit workflow records retained under harness homes; not moved",
            sorted(paths),
        )
    return Finding("swarm-state", "ok", "no swarm run-state files found")


MANUAL_EDIT_FILES = (
    ".claude/CLAUDE.md",
    ".gemini/GEMINI.md",
    ".copilot/copilot-instructions.md",
    ".codex/AGENTS.md",
    ".pi/agent/AGENTS.md",
)

_DATA_ROOT_MARKERS = (
    ".claude/data/plans",
    ".claude/data/draft-issues",
    ".claude/data/analysis",
    ".claude/data/artifacts",
    ".claude/data/bug-reports",
)


def _check_manual_edits(ctx: MigrationContext) -> Finding:
    checklist: list[str] = []
    for rel in MANUAL_EDIT_FILES:
        path = ctx.home / rel
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except OSError:
            continue
        reasons: list[str] = []
        if ".claude/scripts" in content or "dev_status.py" in content:
            reasons.append("legacy script paths")
        if any(marker in content for marker in _DATA_ROOT_MARKERS) or (
            ".claude/data" in content and "toolkit_state.json" not in content
        ):
            reasons.append("legacy data-root references")
        if reasons:
            checklist.append(f"{path} ({' and '.join(reasons)})")

    settings_local = ctx.home / ".claude" / "settings.local.json"
    if settings_local.is_file():
        try:
            content = settings_local.read_text(encoding="utf-8")
            if ".claude/scripts" in content or "dev_status.py" in content:
                checklist.append(
                    f"{settings_local} (permission patterns naming legacy script paths)"
                )
        except OSError:
            pass

    if checklist:
        return Finding(
            "manual-edit-checklist",
            "warn",
            "external files reference legacy script paths and require manual update",
            checklist,
        )
    return Finding(
        "manual-edit-checklist", "ok", "no external files requiring manual edits found"
    )


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
        _check_path_env_vars(ctx),
        _check_swarm_state(ctx),
        _check_manual_edits(ctx),
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


def _jsonl_lines(path: Path, *, strict: bool) -> int:
    """Count complete JSONL records; optionally reject a torn or invalid line."""
    count = 0
    with path.open("rb") as handle:
        for line in handle:
            if not line.endswith(b"\n"):
                if strict:
                    raise MigrationError(f"{path} has an incomplete JSONL line")
                continue
            if strict and _parse_line(line[:-1]) is None:
                raise MigrationError(f"{path} has an invalid JSONL line")
            count += 1
    return count


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


def _record_only(_state: RunState, _record: StepRecord) -> dict[str, object]:
    """A stale-scan step only records; its apply never mutates."""
    return {}


def _no_undo(_state: RunState, _record: StepRecord) -> None:
    return None


def _always(_state: RunState, _record: StepRecord) -> bool:
    return True


def _data_root(ctx: MigrationContext) -> Path:
    return ctx.home / ".claude" / "data"


def _discover_carries(
    ctx: MigrationContext,
) -> tuple[list[dict[str, object]], list[Path]]:
    """Structured, journaled discovery of what this run carries.

    Taken under the migration lock; the authoritative record the carry
    scheduling, the RootMap carried roots and recovery all read. Refuses
    unsafe shapes (a symlink entry, a nested symlink, a non-regular file)
    instead of carrying them, and refuses a pre-existing
    ``.toolkit-home-snapshot-*`` directory — that is an older migration's
    finalize evidence, never a carry candidate. EMPTY directories are not
    carried: nothing to move, and an empty-to-empty replacement would make
    crash-window ownership undecidable.
    """
    data_root = _data_root(ctx)
    candidates, empties, _stale = _unclassified_entries(data_root)
    _lstat_no_symlink(data_root)
    dest_data = _toolkit_root() / "data"
    carries: list[dict[str, object]] = []
    for path in candidates:
        info = path.lstat()
        name = path.name
        if stat.S_ISLNK(info.st_mode):
            raise MigrationError(f"{path} is a symlink; refusing to carry it")
        if stat.S_ISDIR(info.st_mode):
            kind = "dir"
            inner = _nested_symlink(path)
            if inner is not None:
                raise MigrationError(
                    f"{inner} is a symlink inside {path}; its target may sit "
                    "under a moved root, so the carry refuses"
                )
        elif stat.S_ISREG(info.st_mode):
            kind = "file"
        else:
            raise MigrationError(f"refusing to carry a non-regular entry: {path}")
        dest = dest_data / name
        if _lexists(dest):
            raise MigrationError(
                f"{dest} is occupied; the carry destination must not exist, "
                "empty directories included"
            )
        carries.append(
            {
                "name": name,
                "kind": kind,
                "legacy": str(path),
                "dest": str(dest),
                "staging": _carry_staging_path(ctx, name),
                "absent": False,
                "files": _digest_tree(path),
                "shape": _shape(path) if kind == "dir" else (),
                "dest_existed": False,
            }
        )
    return carries, empties


def _preflight_apply(state: RunState, _record: StepRecord) -> dict[str, object]:
    if _lexists(state.work_dir):
        raise MigrationError(f"{state.work_dir} already exists")
    _lstat_no_symlink(_data_root(state.ctx))
    carries, empties = _discover_carries(state.ctx)
    _register_carry_oracle([str(e["name"]) for e in carries])
    dest_data = _toolkit_root() / "data"
    return {
        "warnings": state.warnings,
        "work_dir": str(state.work_dir),
        "snapshot_dir": str(state.snapshot_dir),
        "carry": carries,
        "carry_empties": [str(p) for p in empties],
        "dest_data_existed": dest_data.is_dir() and not dest_data.is_symlink(),
    }


def _inventory_apply(state: RunState, _record: StepRecord) -> dict[str, object]:
    inventory = build_inventory(state.ctx, strict=True)
    digest = write_inventory(state.require_journal().directory, inventory)
    state.inventory = inventory
    return {"sha256": digest}


def _journaled_carries(state: RunState) -> list[dict[str, object]]:
    """The carried list journaled by this run's preflight, or [] on older journals."""
    preflight = state.step("preflight")
    detail = _done(preflight) if preflight is not None else None
    if not isinstance(detail, dict):
        return []
    entries = detail.get("carry")
    return entries if isinstance(entries, list) else []


def _register_carry_oracle(names: list[str]) -> None:
    """Oracle rows for runtime-discovered carry checkpoints.

    ``_checkpoint`` asserts membership in the statically built oracle, so the
    journaled carried list generates rows for its names before any carry
    phase runs. A fresh process (crash recovery, resume) calls this from
    ``_load_state`` with the same journaled list, so the rows exist before
    any carry-phase recovery relies on them.
    """
    for name in names:
        for suffix in ("copied", "promoted"):
            RECOVERY_ORACLE.setdefault(
                f"migrate.carry.{name}.{suffix}",
                Expected("rollback", True, "begin", True, False),
            )
        RECOVERY_ORACLE.setdefault(
            f"migrate.carry.{name}.severed",
            Expected("roll-forward", True, "done", True, False, "toolkit-home"),
        )


def _carry_begin(state: RunState, phase: str) -> dict[str, object]:
    name = phase.partition(":")[2]
    for entry in _journaled_carries(state):
        if entry.get("name") == name:
            return dict(entry)
    raise MigrationError(
        f"{name} was not discovered at preflight; refusing to carry it"
    )


def _carry_apply(state: RunState, record: StepRecord) -> dict[str, object]:
    begin = _begin(record)
    if begin["absent"]:
        return {"absent": True}
    legacy, staging = Path(str(begin["legacy"])), Path(str(begin["staging"]))
    dest = Path(str(begin["dest"]))
    if _lexists(staging):
        raise MigrationError(f"{staging} is occupied; refusing to stage over it")
    _mkdir_durable(staging.parent)
    _lstat_no_symlink(dest)
    if _lexists(dest):
        raise MigrationError(
            f"{dest} is occupied; the carry destination must not exist, "
            "empty directories included"
        )
    info = legacy.lstat()
    if stat.S_ISDIR(info.st_mode):
        inner = _nested_symlink(legacy)
        if inner is not None:
            raise MigrationError(
                f"{inner} is a symlink inside the carried tree; its target may "
                "sit under a moved root, so the carry refuses"
            )
    elif not stat.S_ISREG(info.st_mode):
        raise MigrationError(f"refusing to carry non-regular entry: {legacy}")
    _copy_durable(legacy, staging)
    _fsync_dir(staging.parent)
    copied = _differences(
        staging,
        begin["files"],
        _digest_tree(staging),  # type: ignore[arg-type]
    )
    if copied:
        raise MigrationError(
            "a carried copy differs from its inventoried source: " + "; ".join(copied)
        )
    _checkpoint(f"migrate.carry.{_domain(record)}.copied")
    stale = _stale_entries(state, staging, legacy, dest)
    if stale:
        journal = state.require_journal()
        journal.begin(f"stale-scan:carry:{_domain(record)}", stale=stale)
        journal.done(f"stale-scan:carry:{_domain(record)}", stale=stale)
    _rename_durable(staging, dest)
    _checkpoint(f"migrate.carry.{_domain(record)}.promoted")
    return {
        "files": _digest_tree(dest),
        "shape": _shape(dest) if dest.is_dir() else (),
        "stale": stale,
    }


def _stale_entries(
    state: RunState, root: Path, legacy: Path, dest: Path
) -> list[dict[str, str]]:
    """Old-root mentions in the staged copy, journaled with BOTH paths.

    The record keeps the legacy source path AND the intended destination, so
    the reported file stays findable after a pre-flip abort or recovery: undo
    puts the copy back at its legacy source and removes the destination, and
    a destination-only record would point at a file that no longer exists.
    """
    import migrate_path_transform as mpt

    roots = _transform_roots(state)
    records: list[dict[str, str]] = []
    for record in mpt.mentions_in_tree(root, roots):
        rel = os.path.relpath(record["file"], root)
        if record.get("kind") == "unreadable":
            source = Path(legacy)
            target = Path(dest)
        else:
            source = Path(legacy) / rel
            target = Path(dest) / rel
        records.append(
            {
                "file": str(target),
                "source": str(source),
                "line": record["line"],
                "value": record["value"],
            }
        )
    return records


def _final_path(staged: str, staging: Path, dest: Path) -> str:
    """The file's FINAL destination path, not the temporary staging path."""
    rel = os.path.relpath(staged, staging)
    return str(dest / rel)


def _transform_roots(state: RunState) -> object:
    """RootMap for this run: the domain roots plus every carried root."""
    import migrate_path_transform as mpt

    carries = _journaled_carries(state)
    carried_dirs = [
        (Path(str(e["legacy"])), Path(str(e["dest"])))
        for e in carries
        if e.get("kind") == "dir"
    ]
    carried_files = [
        (Path(str(e["legacy"])), Path(str(e["dest"])))
        for e in carries
        if e.get("kind") == "file"
    ]
    return mpt.RootMap.for_home(
        state.ctx.home, carried_dirs=carried_dirs, carried_files=carried_files
    )


def _nested_symlink(root: Path) -> Path | None:
    stack = [root]
    while stack:
        path = stack.pop()
        for child in sorted(path.iterdir()):
            if child.is_symlink():
                return child
            if child.is_dir():
                stack.append(child)
    return None


def _carry_undo(state: RunState, record: StepRecord) -> None:
    begin = _begin(record)
    if begin.get("absent"):
        return
    staging = Path(str(begin["staging"]))
    dest = Path(str(begin["dest"]))
    legacy = Path(str(begin["legacy"]))
    if _lexists(staging):
        _remove_path(staging, within=state.work_dir)
    if _lexists(dest) and _lexists(legacy):
        # the pre-flip case: the copy at dest is the run's, the legacy
        # original is intact, so the copy is redundant and the journal's
        # digests prove it is this carry's
        problems = _tree_differences(dest, begin["files"], begin.get("shape", ()))
        problems += _tree_differences(legacy, begin["files"], begin.get("shape", ()))
        if problems:
            raise MigrationError(
                "the carried copy or its legacy source changed, so nothing was "
                "deleted: " + "; ".join(problems)
            )
        _remove_path(dest, within=dest.parent)
    elif _lexists(dest) and not _lexists(legacy):
        raise MigrationError(
            f"{dest} exists but its legacy original is gone; resolve it by hand"
        )


def _carry_verify(state: RunState, record: StepRecord) -> bool:
    begin = _begin(record)
    if begin.get("absent"):
        return True
    staging = Path(str(begin["staging"]))
    dest = Path(str(begin["dest"]))
    return _lexists(staging) or _lexists(dest)


def _carry_snapshot_begin(state: RunState, phase: str) -> dict[str, object]:
    name = phase.partition(":")[2]
    carry = state.step(f"carry:{name}")
    if carry is None or _done(carry) is None or _absent(carry):
        raise MigrationError(f"{name} was never carried")
    legacy = Path(str(_begin(carry)["legacy"]))
    return {
        "legacy": str(legacy),
        "snapshot": str(state.snapshot_dir / name),
        "absent": False,
    }


def _carry_snapshot_apply(_state: RunState, record: StepRecord) -> dict[str, object]:
    begin = _begin(record)
    legacy, snapshot = Path(str(begin["legacy"])), Path(str(begin["snapshot"]))
    if _lexists(snapshot):
        raise MigrationError(f"{snapshot} already exists")
    carry = _state_step(_state, f"carry:{_domain(record)}")
    done = _done(carry) if carry is not None else None
    if done is None:
        raise MigrationError(f"{legacy} was never carried")
    if _lexists(legacy):
        problems = _tree_differences(legacy, done["files"], done.get("shape", ()))
        if problems:
            raise MigrationError(
                f"{legacy} no longer matches the journal, so nothing was "
                "severed: " + "; ".join(problems)
            )
    _rename_durable(legacy, snapshot)
    return {}


def _carry_snapshot_undo(state: RunState, record: StepRecord) -> None:
    begin = _begin(record)
    legacy, snapshot = Path(str(begin["legacy"])), Path(str(begin["snapshot"]))
    if _lexists(snapshot):
        if _lexists(legacy):
            raise MigrationError(
                f"{legacy} was recreated while its original is still in "
                f"{snapshot}; resolve it by hand"
            )
        _rename_durable(snapshot, legacy)
    parent = snapshot.parent
    if parent.is_dir() and not parent.is_symlink() and not any(parent.iterdir()):
        parent.rmdir()
        _fsync_dir(parent.parent)


def _carry_snapshot_verify(state: RunState, record: StepRecord) -> bool:
    begin = _begin(record)
    legacy, snapshot = Path(str(begin["legacy"])), Path(str(begin["snapshot"]))
    return _lexists(snapshot) and not _lexists(legacy)


def _state_step(state: RunState, phase: str) -> StepRecord | None:
    return state.step(phase)


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

    dest = Path(str(state.domain_entry(domain)["destination"]))
    legacy = Path(str(state.domain_entry(domain)["legacy"]))

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
        plan = mpt.plan_transform(stores(), _transform_roots(state))
        if plan.stale:
            journal = state.require_journal()
            record = [
                {
                    "file": _final_path(r.file, staging, dest),
                    "line": r.pointer,
                    "value": r.value,
                }
                for r in plan.stale
            ]
            journal.begin(f"stale-scan:{domain}", stale=record, source=str(legacy))
            journal.done(f"stale-scan:{domain}", stale=record, source=str(legacy))
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
    return {"created": len(_planned_links(record)), "retained": _retained(state)}


# The toolkit-home directories whose links.toml rows replace a legacy
# ~/.claude path of the same relative name.
_MAPPED_DIRS = ("scripts", "hooks", "icons")


def _retained(state: RunState) -> list[dict[str, str]]:
    """The legacy links this migration keeps until finalize removes them.

    Two sources, deduped by destination. The mapping: for each applicable
    row under ``~/.agent-toolkit/{scripts,hooks,icons}``, the ``~/.claude``
    link at the same relative path, when it points into an agent-toolkit
    checkout. And the manifest: links an earlier install recorded that no
    row produces any more. The history alone misses links it never recorded
    or recorded with a since-moved source, so it cannot be the only source.
    """
    ctx = state.ctx
    gathered = _gathered_links(state)
    known = {dest for _src, dest, _rel, _applicable in gathered}
    toolkit, legacy = ctx.home / ".agent-toolkit", ctx.home / ".claude"
    candidates: list[Path] = []
    for _src, dest, _rel, applicable in gathered:
        if not applicable or not dest.is_relative_to(toolkit):
            continue
        relative = dest.relative_to(toolkit)
        if relative.parts[0] not in _MAPPED_DIRS:
            continue
        old = legacy / relative
        if old.is_symlink() and old not in known and _points_into_toolkit(old):
            candidates.append(old)
    candidates += [
        dest
        for dest in link_inspect.find_orphaned_links(
            gathered, manifest_entries=link_inspect.read_manifest_entries(ctx.history)
        )
        if dest.is_symlink()
    ]
    retained: dict[Path, dict[str, str]] = {}
    for dest in candidates:
        retained.setdefault(dest, {"dest": str(dest), "target": os.readlink(dest)})
    return list(retained.values())


def _points_into_toolkit(link: Path) -> bool:
    """Whether ``link``'s target lies inside an agent-toolkit checkout.

    The target is joined to the link's directory and normalized without
    resolving, so a dangling link into a checkout still counts. A checkout
    has links.toml, install.py and agent-scripts/; the origin repo has
    the first two only, so its links stay foreign.
    """
    target = Path(os.path.normpath(link.parent / os.readlink(link)))
    return any(
        (root / "links.toml").is_file()
        and (root / "install.py").is_file()
        and (root / "agent-scripts").is_dir()
        for root in target.parents
    )


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


# ── settings and global-hook rewrite (release 1) ─────────────────────────────────

# Live settings files, by harness, that can hold a manifest-owned command path.
_SETTINGS_FILES: dict[str, tuple[str, ...]] = {
    "claude": (".claude/settings.json",),
    "opencode": (".config/opencode/opencode.jsonc",),
    "pi": (".pi/agent/settings.json",),
    "agy": (".gemini/antigravity-cli/settings.json",),
}


def _manifest_path_map(repo_root: Path) -> dict[str, str]:
    """Old manifest path -> new manifest path, read from links.toml.

    Every links.toml destination under the toolkit home has a legacy
    equivalent under the harness home (~/.claude); the rewrite replaces a live
    value that is exactly the old path, or a command that invokes it, with the
    new path. No heuristics or substring matching. Both the ``~/``-relative
    and the expanded ``/home/<user>/`` form of each old path are mapped (live
    agy settings use both), each rewritten to the same form it was found in.
    """
    mapping: dict[str, str] = {}
    home = str(Path.home())
    for spec in link_inspect.load_links(repo_root / "links.toml"):
        dest = spec.dest
        if not dest.startswith("~/.agent-toolkit"):
            continue
        old = "~/.claude" + dest[len("~/.agent-toolkit") :]
        mapping[old] = dest
        mapping[home + old[1:]] = home + dest[1:]
    return mapping


def _rewrite_value(value: str, mapping: dict[str, str]) -> tuple[str, bool, list[str]]:
    """Rewrite one string against the manifest map.

    Returns (new_value, matched, unmatched_list). An exact match replaces the
    whole value; a whitespace token that is an old path is replaced in place —
    only that token's span changes, so every other character, including
    irregular whitespace, survives; a value that merely contains an old path
    without a clean token is reported as unmatched and left alone. A
    parenthesised value is always unmatched: permission patterns (Bash(...:*)
    and agy's command(...)) are structural syntax around the path, not plain
    invocations of it, and stay on the manual-review checklist.
    """
    if value in mapping:
        return mapping[value], True, []
    parts = _WS_SPLIT.split(value)
    hit = False
    for i, part in enumerate(parts):
        if part and not part.isspace() and part in mapping:
            parts[i] = mapping[part]
            hit = True
    if hit and "(" not in value and ")" not in value:
        # a parenthesised wrapper (a permission pattern like Bash(...:*) or
        # agy's command(...)) is structural syntax around the path, not a
        # plain invocation of it: report it, leave the bytes alone
        return "".join(parts), True, []
    if any(k in value for k in mapping):
        return value, False, [value]
    return value, False, []


def _skip_string(text: str, i: int) -> int:
    """The index just past the JSON string literal opening at ``i``."""
    i += 1
    n = len(text)
    while i < n:
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == '"':
            return i + 1
        i += 1
    return n


def _strip_jsonc(text: str) -> str:
    """Comment-stripped copy of a JSONC text, string-aware.

    For structural validation and post-rewrite re-checks only; discovery and
    rewriting always work on the original text.
    """
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = _skip_string(text, i)
            out.append(text[i:j])
            i = j
        elif c == "/" and text[i + 1 : i + 2] == "/":
            j = text.find("\n", i)
            i = n if j == -1 else j  # keep the newline itself
        elif c == "/" and text[i + 1 : i + 2] == "*":
            j = text.find("*/", i + 2)
            i = n if j == -1 else j + 2
            out.append(" ")
        else:
            out.append(c)
            i += 1
    return "".join(out)


def _strip_trailing_commas(text: str) -> str:
    """Drop commas before a closing brace or bracket, string-aware."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = _skip_string(text, i)
            out.append(text[i:j])
            i = j
            continue
        if c == ",":
            k = i + 1
            while k < n and text[k] in " \t\r\n":
                k += 1
            if k < n and text[k] in "}]":
                i += 1
                continue
        out.append(c)
        i += 1
    return "".join(out)


def _json_string_spans(text: str) -> list[tuple[int, int]]:
    """Spans of every JSON string literal in ``text``, comments skipped.

    Walks the raw text so discovery can run on JSONC; each span includes its
    quotes.
    """
    spans: list[tuple[int, int]] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            j = _skip_string(text, i)
            spans.append((i, j))
            i = j
        elif c == "/" and text[i + 1 : i + 2] == "/":
            j = text.find("\n", i)
            i = n if j == -1 else j
        elif c == "/" and text[i + 1 : i + 2] == "*":
            j = text.find("*/", i + 2)
            i = n if j == -1 else j + 2
        else:
            i += 1
    return spans


def _json_valid(text: str) -> bool:
    """Whether ``text`` parses as JSON or comment/trailing-comma JSONC."""
    try:
        json.loads(_strip_trailing_commas(_strip_jsonc(text)))
    except ValueError:
        return False
    return True


def _rewrite_json_text(
    text: str, mapping: dict[str, str]
) -> tuple[str, list[dict[str, str]], list[str]]:
    """Rewrite matched string literals in place on the raw text.

    Discovery parses (JSONC-tolerant), but substitution happens on the
    original bytes: each matched value's exact literal is replaced by the
    JSON encoding of its new value, and every other byte — comments,
    indentation, whitespace inside values — is left identical. Keys are never
    rewritten. Raises ValueError when ``text`` does not parse.
    """
    if not _json_valid(text):
        raise ValueError("not valid JSON/JSONC")
    edits: list[tuple[int, int, str]] = []
    matches: list[dict[str, str]] = []
    unmatched: list[str] = []
    n = len(text)
    for start, end in _json_string_spans(text):
        try:
            value = json.loads(text[start:end])
        except json.JSONDecodeError:
            continue  # a malformed literal in a file _json_valid accepted: skip
        if not isinstance(value, str):
            continue
        k = end
        while k < n and text[k] in " \t\r\n":
            k += 1
        if k < n and text[k] == ":":
            # a key, not a value: keys name permission rules or config
            # entries and are never rewritten — but a key that names an old
            # manifest path is exactly the drift the manual-review report
            # exists for, so list it
            _new, matched, um = _rewrite_value(value, mapping)
            unmatched.extend(um)
            if matched:
                unmatched.append(value)
            continue
        new, matched, um = _rewrite_value(value, mapping)
        if matched:
            matches.append({"old": value, "new": new})
            edits.append((start, end, json.dumps(new)))
        unmatched.extend(um)
    out = text
    for start, end, literal in reversed(edits):
        out = out[:start] + literal + out[end:]
    return out, matches, unmatched


_WS_SPLIT = re.compile(r"(\s+)")


def _rewrite_shell_text(
    text: str, mapping: dict[str, str]
) -> tuple[str, list[dict[str, str]], list[str]]:
    """Rewrite the emitted command paths in a shell hook file (token-based)."""
    matches: list[dict[str, str]] = []
    unmatched: list[str] = []
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        parts = _WS_SPLIT.split(line)
        for i, part in enumerate(parts):
            if part and not part.isspace():
                new, matched, um = _rewrite_value(part, mapping)
                if matched:
                    parts[i] = new
                    matches.append({"old": part, "new": new})
                unmatched.extend(um)
        out.append("".join(parts))
    return "".join(out), matches, unmatched


def _githook_file(hooks_path: Path) -> Path | None:
    """The no-commit-on-main hook file under a global core.hooksPath, if present."""
    for candidate in (
        hooks_path / "lib" / "no-commit-on-main.sh",
        hooks_path / "no-commit-on-main.sh",
    ):
        if _lexists(candidate) and candidate.is_file():
            return candidate
    return None


def _git_global_entries() -> list[tuple[str, str, str]]:
    """(section, key, value) entries from the global git config files.

    Parses the files directly (no subprocess) so migrations run hermetically
    under the test harness, which blocks real subprocess calls. Same files,
    in git's order: ``$XDG_CONFIG_HOME/git/config`` (or
    ``~/.config/git/config``), then ``~/.gitconfig``. Include and includeIf
    sections are surfaced as entries too, so callers can detect that the
    parsed values may not be final.
    """
    entries: list[tuple[str, str, str]] = []
    candidates: list[Path] = []
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        candidates.append(Path(xdg) / "git" / "config")
    else:
        candidates.append(Path.home() / ".config" / "git" / "config")
    candidates.append(Path.home() / ".gitconfig")
    for path in candidates:
        if not _lexists(path) or not path.is_file():
            continue
        cur: str | None = None
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for raw in lines:
            line = raw.strip()
            if not line or line.startswith(("#", ";")):
                continue
            if line.startswith("[") and line.endswith("]"):
                cur = line[1:-1].split(None, 1)[0]
                continue
            if cur is not None and "=" in line:
                k, _, v = line.partition("=")
                entries.append((cur, k.strip(), v.strip()))
    return entries


def _git_global_get(key: str) -> str | None:
    """Read a global git config value by parsing its file (no subprocess)."""
    section, _, var = key.partition(".")
    for sec, k, v in _git_global_entries():
        if sec == section and k == var:
            return v
    return None


def _git_global_has_includes() -> bool:
    """Whether any global git config file has include or includeIf sections."""
    return any(
        sec == "include" or sec.startswith("includeIf")
        for sec, _k, _v in _git_global_entries()
    )


def _git_global_set(key: str, value: str) -> None:
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(
            ["git", "config", "--global", key, value],
            capture_output=True,
            text=True,
            check=False,
        )


def _settings_targets(ctx: MigrationContext) -> list[dict[str, object]]:
    """The live settings files and the global git hook to consider."""
    targets: list[dict[str, object]] = []
    for harness in ctx.opts.harnesses:
        for rel in _SETTINGS_FILES.get(harness, ()):
            targets.append(
                {"kind": "file", "path": str(ctx.home / rel), "harness": harness}
            )
    targets.append({"kind": "githook", "path": "", "harness": None})
    return targets


def _settings_snapshot_dir(state: RunState) -> Path:
    return state.work_dir / "settings-rewrite"


def _settings_rewrite_begin(state: RunState, _phase: str) -> dict[str, object]:
    """Plan only — no filesystem writes here.

    ``_run_phase`` calls a step's begin handler BEFORE journalling begin, so a
    handler that created the snapshot directory and files would leave an
    unjournaled tree a crash could not recover: this computes ONLY the planned
    paths and digests. The snapshots are written by the apply, after the begin
    record exists and before any settings write.
    """
    ctx = state.ctx
    targets = _settings_targets(ctx)
    plan: list[dict[str, object]] = []
    index = 0
    for t in targets:
        entry: dict[str, object] = {
            "kind": t["kind"],
            "path": t["path"],
            "harness": t.get("harness"),
            "present": False,
            "snapshot_rel": None,
            "original_digest": None,
            "config_key": None,
            "needs_manual": None,
            "unparseable": False,
        }
        if t["kind"] == "githook":
            config = _git_global_get("core.hooksPath")
            entry["config_key"] = config or None
            if config and _git_global_has_includes():
                entry["needs_manual"] = (
                    "core.hooksPath is set but the global git config has "
                    "include/includeIf sections, so it may be overridden "
                    "there; review by hand"
                )
            elif config:
                hook_file = _githook_file(Path(config))
                if hook_file is not None:
                    entry["path"] = str(hook_file)
                    entry["present"] = True
        else:
            if Path(str(t["path"])).is_file():
                entry["present"] = True
        if entry["present"]:
            path = Path(str(entry["path"]))
            entry["snapshot_rel"] = str(index)
            entry["original_digest"] = _sha256(path)
            index += 1
        plan.append(entry)
    return {"targets": plan}


def _settings_rewrite_apply(state: RunState, record: StepRecord) -> dict[str, object]:
    begin = _begin(record)
    targets = list(begin["targets"])  # type: ignore[arg-type]
    snapshot_dir = _settings_snapshot_dir(state)
    # COPY-BEFORE-WRITE invariant: every original is durably snapshotted and
    # verified BEFORE the first settings write, so a crash can never leave a
    # rewritten file whose original snapshot was never completed.
    _mkdir_durable(snapshot_dir)
    saved: list[dict[str, object]] = []
    for entry in targets:
        if not entry["present"]:
            continue
        path = Path(str(entry["path"]))
        snap = snapshot_dir / str(entry["snapshot_rel"])
        _write_durable(snap, path.read_bytes(), 0o444)
        _fsync_file(snap)
        if _sha256(path) != entry["original_digest"]:
            raise MigrationError(
                f"{path} changed between the begin record and its snapshot"
            )
        saved.append(entry)
    _fsync_dir(snapshot_dir)
    mapping = _manifest_path_map(state.ctx.repo_root)
    done_targets: list[dict[str, object]] = []
    for entry in targets:
        new_entry: dict[str, object] = dict(entry)
        new_entry["rewritten_digest"] = None
        new_entry["matches"] = []
        new_entry["unmatched"] = []
        new_entry["rewritten"] = False
        if not entry["present"]:
            done_targets.append(new_entry)
            continue
        path = Path(str(entry["path"]))
        try:
            raw = path.read_bytes()
            text = raw.decode("utf-8")
        except (OSError, UnicodeDecodeError):
            new_entry["unparseable"] = True
            done_targets.append(new_entry)
            continue
        if entry["kind"] == "githook":
            new_text, matches, unmatched = _rewrite_shell_text(text, mapping)
        else:
            try:
                new_text, matches, unmatched = _rewrite_json_text(text, mapping)
            except ValueError:
                new_entry["unparseable"] = True
                done_targets.append(new_entry)
                continue
        if matches:
            mode = path.stat().st_mode & 0o777
            _write_durable(path, new_text.encode("utf-8"), mode)
            _fsync_file(path)
            new_entry["rewritten"] = True
        new_entry["matches"] = matches
        new_entry["unmatched"] = unmatched
        new_entry["rewritten_digest"] = _sha256(path)
        done_targets.append(new_entry)
    return {"targets": done_targets}


def _settings_rewrite_undo(state: RunState, record: StepRecord) -> None:
    begin = _begin(record)
    done = _done(record)
    entries = list((done if done is not None else begin).get("targets", []))  # type: ignore[arg-type]
    snapshot_dir = _settings_snapshot_dir(state)
    for entry in entries:
        if (
            entry["kind"] == "githook"
            and entry.get("config_key") is not None
            and not entry.get("needs_manual")
        ):
            current = _git_global_get("core.hooksPath")
            if current != entry["config_key"]:
                _git_global_set("core.hooksPath", str(entry["config_key"]))
        snap_rel = entry.get("snapshot_rel")
        if not entry.get("present") or not snap_rel:
            continue
        snap = snapshot_dir / str(snap_rel)
        if not _lexists(snap):
            continue
        original = snap.read_bytes()
        if hashlib.sha256(original).hexdigest() != entry.get("original_digest"):
            raise MigrationError(
                f"{snap} does not match the digest journalled for {entry['path']}; "
                "restore it by hand"
            )
        path = Path(str(entry["path"]))
        mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
        _write_durable(path, original, mode)
        _fsync_file(path)
    # the snapshot tree is journal-proven migration-owned bookkeeping: without
    # removing it, the skeleton sweep could never rmdir a work_dir that saved
    # settings snapshots.
    shutil.rmtree(snapshot_dir, ignore_errors=True)


def _settings_rewrite_verify(state: RunState, record: StepRecord) -> bool:
    done = _done(record)
    if done is None:
        return True
    for entry in list(done.get("targets", [])):  # type: ignore[arg-type]
        if not entry.get("present"):
            continue
        path = Path(str(entry["path"]))
        if not _lexists(path) or not path.is_file():
            return False
        if _sha256(path) != entry.get("rewritten_digest"):
            return False
        if (
            entry["kind"] == "githook"
            and entry.get("config_key") is not None
            and _git_global_get("core.hooksPath") != entry["config_key"]
        ):
            return False
    return True


def _settings_rewrite_guard(state: RunState, record: StepRecord) -> list[str]:
    """Refuse restore if a live setting or the hook path changed since the run.

    Called from _write_guard, which runs before the pointer is flipped back to
    legacy, so a refusal leaves the machine fully migrated rather than
    half-restored.
    """
    done = _done(record)
    problems: list[str] = []
    if done is None:
        return problems
    for entry in list(done.get("targets", [])):  # type: ignore[arg-type]
        if not entry.get("present"):
            continue
        path = Path(str(entry["path"]))
        if not _lexists(path) or not path.is_file():
            problems.append(f"{path} (changed since the migration; not restoring)")
            continue
        if _sha256(path) != entry.get("rewritten_digest"):
            problems.append(f"{path} (changed since the migration; not restoring)")
        if (
            entry["kind"] == "githook"
            and entry.get("config_key") is not None
            and _git_global_get("core.hooksPath") != entry["config_key"]
        ):
            problems.append(
                "core.hooksPath (changed since the migration; not restoring)"
            )
    return problems


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
    for record in _steps_in(state.records):
        if not str(record["phase"]).startswith("carry:"):
            continue
        done = _done(record)
        if done is None or _absent(record):
            continue
        begin = _begin(record)
        problems += _tree_differences(
            Path(str(begin["dest"])), done["files"], done.get("shape", ())
        )
        problems += _tree_differences(
            Path(str(begin["legacy"])), begin["files"], begin.get("shape", ())
        )
        checked += len(done["files"])  # type: ignore[arg-type]
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
        result: dict[str, object] = {"absent": True}
        if _domain(record) in TELEMETRY_DOMAINS:
            result["telemetry_lines"] = 0
        return result
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
    result = {"files": _done(stage)["files"]}  # type: ignore[index]
    if _domain(record) in TELEMETRY_DOMAINS:
        result["telemetry_lines"] = _jsonl_lines(dest, strict=False)
    return result


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
    absent = stage is None or _absent(stage)
    return {
        "legacy": str(legacy),
        "snapshot": str(state.snapshot_dir / legacy.name),
        "absent": absent,
        "shape": ()
        if absent or not (legacy.is_dir() and not legacy.is_symlink())
        else _shape(legacy),
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
            "--during-migration",
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
register_step(
    "settings-rewrite",
    Step(
        _settings_rewrite_apply,
        _settings_rewrite_undo,
        _settings_rewrite_verify,
        _settings_rewrite_begin,
        guard=_settings_rewrite_guard,
    ),
    in_run=True,
)
register_step("carry:", Step(_carry_apply, _carry_undo, _carry_verify, _carry_begin))
register_step(
    "carry-snapshot:",
    Step(
        _carry_snapshot_apply,
        _carry_snapshot_undo,
        _carry_snapshot_verify,
        _carry_snapshot_begin,
    ),
)
register_step("stale-scan:", Step(_record_only, _no_undo, _always))


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
    for entry in _journaled_carries(state):
        if entry.get("absent"):
            continue
        _run_phase(state, f"carry:{entry['name']}")
    _prune_empty_tree(state.work_dir / "staging")
    _run_phase(
        state, "baseline", begun="migrate.baseline.begun", done="migrate.baseline.done"
    )
    _run_phase(state, "links", begun="migrate.links.begun", done="migrate.links.done")
    for phase in RUNTIME_PHASES:
        begun = (
            f"migrate.{phase}.begun"
            if f"migrate.{phase}.begun" in RECOVERY_ORACLE
            else None
        )
        done = (
            f"migrate.{phase}.done"
            if f"migrate.{phase}.done" in RECOVERY_ORACLE
            else None
        )
        _run_phase(state, phase, begun=begun, done=done)
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
    for entry in _journaled_carries(state):
        if entry.get("absent"):
            continue
        name = str(entry["name"])
        phase = f"carry-snapshot:{name}"
        existing = state.step(phase)
        if existing is None:
            _run_phase(state, phase, done=f"migrate.carry.{name}.severed")
        elif _done(existing) is None:
            step = step_for(phase)
            detail = {} if step.verify(state, existing) else step.apply(state, existing)
            journal.done(phase, **detail)
            _checkpoint(f"migrate.carry.{name}.severed")


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


def _prune_empty_tree(root: Path) -> None:
    """Remove every empty directory under ``root``, deepest first, idempotently."""
    if not root.is_dir() or root.is_symlink():
        return
    for child in sorted(root.iterdir()):
        if child.is_dir() and not child.is_symlink():
            _prune_empty_tree(child)
            if not any(child.iterdir()):
                child.rmdir()


def _sweep_skeletons(state: RunState) -> None:
    """Remove the migration's empty skeletons; never a dir holding anything.

    Runs on the abort, restore, narrow-rollback and pre-flip crash-recovery
    paths, never inside finalize's apply. ``work_dir/settings-rewrite`` is
    removed by ``_settings_rewrite_undo`` (before this sweep, newest-first),
    ``restored/`` holding set-aside data is never swept — it belongs to the
    restored-cleanup finalize. The created-empty toolkit-home ``data`` dir is
    pruned only when the journal proves the run created it (the preflight
    detail records whether the parent pre-existed), and the toolkit root
    itself only when it is empty too.
    """
    work = state.work_dir
    if work.is_dir() and not work.is_symlink():
        for name in ("staging", "transform", "restored"):
            root = work / name
            _prune_empty_tree(root)
        for name in ("staging", "transform"):
            root = work / name
            if root.is_dir() and not root.is_symlink() and not any(root.iterdir()):
                root.rmdir()
                _fsync_dir(work)
        if not any(work.iterdir()):
            work.rmdir()
            _fsync_dir(work.parent)
    preflight = state.step("preflight")
    detail = _done(preflight) if preflight is not None else None
    if not isinstance(detail, dict) or detail.get("dest_data_existed"):
        return
    root = _toolkit_root()
    data = root / "data"
    if data.is_dir() and not data.is_symlink() and not any(data.iterdir()):
        data.rmdir()
        _fsync_dir(root)
        if root.is_dir() and not root.is_symlink() and not any(root.iterdir()):
            root.rmdir()
            _fsync_dir(root.parent)


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
        elif phase.startswith("carry:"):
            dest = Path(str(_begin(record)["dest"]))
            if _absent(record):
                if _lexists(dest):
                    problems.append(f"{dest} (created after the migration)")
            else:
                problems += _tree_differences(
                    dest, done["files"], done.get("shape", ())
                )
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
    _check_restore_collisions(state, steps, resume=resume)
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
        elif phase.startswith("carry:") and not _absent(record):
            _set_aside(Path(str(_begin(record)["dest"])), aside / "carry" / domain)
    _checkpoint(f"migrate.{label}.promoted-aside")
    _undo_steps(state, steps)
    _sweep_skeletons(state)
    _checkpoint(f"migrate.{label}.undone")
    journal.done("restore")


def _check_restore_collisions(
    state: RunState, steps: list[StepRecord], *, resume: bool
) -> None:
    """A legacy path recreated while the migration still holds its data refuses.

    Runs BEFORE any restore mutation, on the ``guarded=False`` path too — the
    path validation failure takes while the lock is still held and skips the
    guard entirely. The check is per-snapshot: for a restore RESUMING after an
    interrupted undo, originals already returned to legacy are recognized by
    the snapshot being ABSENT (``_snapshot_undo`` journals nothing when it
    returns one), so "snapshot absent, legacy present" is not a collision;
    "snapshot AND legacy both present" is. A recreated legacy path is refused
    up front instead of letting ``_snapshot_undo`` discover the collision only
    after the pointer already flipped.
    """
    collisions: list[str] = []
    for record in steps:
        phase = str(record["phase"])
        if _done(record) is None or _absent(record):
            continue
        if phase.startswith("snapshot:"):
            snapshot = Path(str(_begin(record)["snapshot"]))
            legacy = _domain_legacy(state, _domain(record))
            if legacy is not None and _lexists(snapshot) and _lexists(legacy):
                collisions.append(
                    f"{legacy} (recreated while {snapshot} holds its original)"
                )
        elif phase.startswith("carry:"):
            legacy = Path(str(_begin(record)["legacy"]))
            dest = Path(str(_begin(record)["dest"]))
            if _lexists(legacy) and _lexists(dest):
                collisions.append(
                    f"{legacy} (recreated while {dest} holds the carried copy)"
                )
    if collisions:
        raise RestoreRefused(
            "a legacy path was recreated while the migration still holds its "
            "original or copy; resolve it by hand: " + "; ".join(collisions)
        )


def _domain_legacy(state: RunState, domain: str) -> Path | None:
    try:
        return Path(str(state.domain_entry(domain)["legacy"]))
    except KeyError:
        return None


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
    carry = preflight.get("carry")
    if isinstance(carry, list):
        _register_carry_oracle(
            [str(e["name"]) for e in carry if isinstance(e, dict) and e.get("name")]
        )
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
            _sweep_skeletons(state)
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
        if (
            (directory / ROLLBACK_NAME).exists()
            and (directory / FINALIZE_NAME).exists()
            and _outcome(read_records(directory, ROLLBACK_NAME)) != OUTCOME_ROLLED_BACK
        ):
            raise MigrationError(
                f"{directory.name} has conflicting rollback and finalize journals"
            )
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
    snapshots: list[dict[str, object]] = []
    for record in _steps_in(state.records):
        if not str(record["phase"]).startswith("snapshot:") or _absent(record):
            continue
        snapshot = Path(str(_begin(record)["snapshot"]))
        expected_names.add(snapshot.name)
        expected = _inventory_digests(state, _domain(record))
        problems += _differences(snapshot, expected, _digest_tree(snapshot))
        snapshots.append(
            {
                "name": snapshot.name,
                "files": expected,
                "shape": list(_begin(record).get("shape", ())),
            }
        )
    for record in _steps_in(state.records):
        if not str(record["phase"]).startswith("carry-snapshot:") or _absent(record):
            continue
        name = _domain(record)
        snapshot = Path(str(_begin(record)["snapshot"]))
        expected_names.add(snapshot.name)
        carry = state.step(f"carry:{name}")
        done = _done(carry) if carry is not None else None
        if done is None:
            raise MigrationError(f"{name} was carried without a done record")
        problems += _tree_differences(snapshot, done["files"], done.get("shape", ()))
        snapshots.append(
            {
                "name": snapshot.name,
                "files": done["files"],
                "shape": list(done.get("shape", ())),
            }
        )
        legacy = Path(str(_begin(record)["legacy"]))
        if _lexists(legacy):
            problems.append(
                f"{legacy} (the carried legacy copy was recreated before finalize)"
            )
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
    begin = _begin(links) if links is not None else None
    legacy_dirs = _legacy_dir_candidates_from_links(state.ctx.home, begin)
    return {
        "mode": "committed",
        "snapshot_dir": str(state.snapshot_dir),
        "work_dir": str(state.work_dir),
        "retained": (done or {}).get("retained", []),
        "snapshots": snapshots,
        "legacy_dirs": [str(p) for p in legacy_dirs],
    }


def _legacy_dir_candidates_from_links(home: Path, links_detail: object) -> list[Path]:
    """The begin-recorded legacy dirs, or the mapped three on older journals."""
    mapped = [home / ".claude" / name for name in _MAPPED_DIRS]
    if not isinstance(links_detail, dict):
        return sorted(set(mapped))
    extra: set[Path] = set()
    for link in links_detail.get("planned", []):
        if not isinstance(link, dict):
            continue
        dest = Path(str(link.get("dest", "")))
        if not dest.is_relative_to(home / ".claude"):
            continue
        parent = dest.parent
        if parent != home / ".claude":
            extra.add(parent)
    return sorted(set(mapped) | extra)


def _restored_manifest(work_dir: Path) -> dict[str, dict[str, str]]:
    """Record every entry under the three disposable trees, including dirs."""
    entries: dict[str, dict[str, str]] = {}
    for name in ("restored", "staging", "transform"):
        root = work_dir / name
        if not _lexists(root):
            continue
        stack = [root]
        while stack:
            path = stack.pop()
            rel = str(path.relative_to(work_dir))
            info = path.lstat()
            if stat.S_ISDIR(info.st_mode):
                entries[rel] = {"type": "dir"}
                stack.extend(sorted(path.iterdir(), reverse=True))
            elif stat.S_ISREG(info.st_mode):
                entries[rel] = {"type": "file", "sha256": _sha256(path)}
            elif stat.S_ISLNK(info.st_mode) and path != root:
                entries[rel] = {"type": "symlink", "target": os.readlink(path)}
            else:
                raise MigrationError(f"{path} is not a safe file or directory")
    return entries


def _check_restored_manifest(
    work_dir: Path, expected: dict[str, dict[str, str]], *, resume: bool
) -> None:
    current = _restored_manifest(work_dir)
    problems: list[str] = []
    for rel in sorted(set(expected) | set(current)):
        path = work_dir / rel
        if rel not in expected:
            problems.append(f"{path} (added)")
        elif rel not in current:
            if not resume:
                problems.append(f"{path} (removed)")
        elif expected[rel] != current[rel]:
            problems.append(f"{path} (changed)")
    if problems:
        raise MigrationError("restored cleanup changed: " + "; ".join(problems))


def _restored_finalize_plan(state: RunState) -> dict[str, object]:
    work_dir = state.work_dir
    expected_work = _toolkit_root() / f".migration-{state.ctx.migration_id}"
    if work_dir != expected_work or work_dir.is_symlink():
        raise MigrationError(f"unsafe migration work directory: {work_dir}")
    if _toolkit_root().is_symlink():
        raise MigrationError(f"unsafe migration toolkit root: {_toolkit_root()}")
    if not work_dir.is_dir():
        # a MISSING work directory is the valid already-empty state: the
        # skeleton sweep removed it after a restore with nothing to set
        # aside, and there is nothing left to clean — an empty manifest.
        return {
            "mode": "restored-copy",
            "work_dir": str(work_dir),
            "restored": str(work_dir / "restored"),
            "staging": str(work_dir / "staging"),
            "transform": str(work_dir / "transform"),
            "manifest": {},
            "telemetry_lines_discarded": {},
        }
    restored = work_dir / "restored"
    problems: list[str] = []
    expected_domains: set[str] = set()
    carry_expected: set[str] = set()
    counts: dict[str, int | None] = {}
    for record in _steps_in(state.records):
        if not str(record["phase"]).startswith("carry:"):
            continue
        done = _done(record)
        if done is None or _absent(record):
            continue
        name = _domain(record)
        path = restored / "carry" / name
        carry_expected.add(name)
        problems += _tree_differences(path, done["files"], done.get("shape", ()))
        if not _lexists(path):
            problems.append(f"{path} (removed)")
    for record in _steps_in(state.records):
        if not str(record["phase"]).startswith("promote:"):
            continue
        domain = _domain(record)
        done = _done(record)
        if done is None:
            continue
        path = restored / domain
        if not _absent(record):
            expected_domains.add(domain)
            if domain not in TELEMETRY_DOMAINS:
                problems += _differences(path, done["files"], _digest_tree(path))  # type: ignore[arg-type]
        if domain in TELEMETRY_DOMAINS and _lexists(path):
            if not path.is_file() or path.is_symlink():
                problems.append(f"{path} (not a regular telemetry file)")
            else:
                baseline = done.get("telemetry_lines")
                if _absent(record):
                    baseline = 0
                counts[domain] = (
                    _jsonl_lines(path, strict=True) - baseline
                    if isinstance(baseline, int)
                    else None
                )
    if restored.is_dir():
        for path in restored.iterdir():
            if path.name == "carry":
                continue
            if path.name not in expected_domains and not (
                path.name in TELEMETRY_DOMAINS
                and state.step(f"promote:{path.name}") is not None
            ):
                problems.append(f"{path} (not a promoted domain)")
        carry_dir = restored / "carry"
        if carry_dir.is_dir():
            for path in carry_dir.iterdir():
                if path.name not in carry_expected:
                    problems.append(f"{path} (not a promoted domain)")
    for domain in expected_domains:
        if not _lexists(restored / domain):
            problems.append(f"{restored / domain} (removed)")
    for name in ("staging", "transform"):
        root = work_dir / name
        if root.is_dir():
            for child in root.iterdir():
                problems.append(f"{child} (leftover content is not journal-proven)")
    if problems:
        raise MigrationError(
            "restored copy no longer matches the journal, so nothing was deleted: "
            + "; ".join(problems)
        )
    return {
        "mode": "restored-copy",
        "work_dir": str(work_dir),
        "restored": str(restored),
        "staging": str(work_dir / "staging"),
        "transform": str(work_dir / "transform"),
        "manifest": _restored_manifest(work_dir),
        "telemetry_lines_discarded": counts,
    }


def _finalize_apply(state: RunState, plan: dict[str, object]) -> None:
    """Per-entry deletion, journalled, safe to repeat on resume.

    The removal decision runs AFTER the retained links are removed (a
    directory containing only a retained link looks non-empty before that
    cleanup), never in the plan: the plan journals the candidate set, the
    empty check happens when the removal runs, and each directory's OBSERVED
    result is persisted as a finalize JOURNAL record (phase
    ``legacy-dir:<dir>``, begin/done like any step — a ``_checkpoint`` call
    asserts oracle membership only and stores nothing), so a resumed finalize
    and the report reconstruct what actually happened.

    Snapshot entries are removed one by one, each verified against its
    journal digests AND directory shape immediately before its own removal,
    on the fresh path AND on resume (which replays the saved plan directly):
    a snapshot entry written after the plan was journaled refuses that
    entry's deletion with the files named, while unchanged entries still
    proceed. Each entry's removal is journaled as ``snapshot-removed:<name>`` —
    its begin event BEFORE the deletion starts and its done event after — so
    every crash window is covered: on resume, an ABSENT entry is expected
    when its removal begin record exists (this finalize deleted it, caught
    between begin and done) or when its done record exists; an absent entry
    with NEITHER record is a deletion by another process and refuses with the
    path named. A crash INSIDE an entry's recursive removal has its own
    recovery rule: with the entry's removal begin record present, the
    REMAINING content must be a subset of the journal-verified content —
    every remaining file matches its journal digest and shape — and removal
    continues; a remaining file that matches nothing is a foreign write and
    refuses. The ownership limit is the same as the carry destination's and
    is stated for snapshots too: digest-and-shape matching cannot
    distinguish a recreated file with identical bytes from the journal's
    copy; the exclusive migration lock is the documented ownership
    assumption for the snapshot directory, and the guarantee protects writes
    PRESENT at the check.
    """
    journal = state.require_journal()
    snapshot_dir = Path(str(plan["snapshot_dir"]))
    work_dir = Path(str(plan["work_dir"]))
    prior_done = {
        str(r.get("phase")).partition(":")[2]
        for r in journal.records
        if r.get("phase", "").startswith("snapshot-removed:")
        and r.get("event") == "done"
    }
    prior_begins = {
        str(r.get("phase")).partition(":")[2]
        for r in journal.records
        if r.get("phase", "").startswith("snapshot-removed:")
        and r.get("event") == "begin"
    }
    fired = False
    for entry in plan.get("snapshots", []):  # type: ignore[attr-defined]
        name = str(entry["name"])
        if name in prior_done:
            continue
        path = snapshot_dir / name
        journal.begin(f"snapshot-removed:{name}", snapshot=str(path))
        if not fired:
            _checkpoint("migrate.finalize.snapshot-removed")
            fired = True
        if not _lexists(path):
            if name not in prior_begins:
                raise MigrationError(
                    f"{path} is absent without a removal record; something "
                    "else deleted it"
                )
        else:
            problems = _tree_differences(path, entry["files"], entry.get("shape", ()))
            if problems:
                raise MigrationError(
                    f"{path} changed before its deletion: " + "; ".join(problems)
                )
            _remove_path(path, within=snapshot_dir)
        journal.done(f"snapshot-removed:{name}")
    if snapshot_dir.is_dir():
        if any(snapshot_dir.iterdir()):
            _checkpoint("migrate.finalize.snapshot-removed")
        else:
            snapshot_dir.rmdir()
            _fsync_dir(snapshot_dir.parent)
            _checkpoint("migrate.finalize.snapshot-removed")
    for leftover in ("staging", "transform"):
        _remove_path(work_dir / leftover, within=work_dir)
    if work_dir.is_dir() and not any(work_dir.iterdir()):
        work_dir.rmdir()
        _fsync_dir(work_dir.parent)
    _checkpoint("migrate.finalize.staging-removed")
    # A link finalize removed, or found already gone, leaves every history
    # entry for its destination dead, whatever source it recorded. A link
    # someone changed stays; only the entry naming the journaled target goes.
    gone: set[str] = set()
    changed: set[tuple[str, str]] = set()
    for link in plan["retained"]:  # type: ignore[attr-defined]
        dest, target = Path(str(link["dest"])), str(link["target"])
        if dest.is_symlink() and os.readlink(dest) == target:
            dest.unlink()
            _fsync_dir(dest.parent)
        if not _lexists(dest):
            gone.add(str(dest))
        elif not (dest.is_symlink() and os.readlink(dest) == target):
            changed.add((str(dest), target))
    _drop_history_entries(
        state.ctx.history,
        lambda e: (
            e.get("kind") == "symlink-created"
            and (e.get("dest") in gone or (e.get("dest"), e.get("src")) in changed)
        ),
    )
    _checkpoint("migrate.finalize.links-removed")
    _checkpoint("migrate.finalize.legacy-dir-removed")
    kept: list[str] = []
    for dir_path in plan.get("legacy_dirs", []):  # type: ignore[attr-defined]
        directory = Path(str(dir_path))
        if not _lexists(directory):
            continue
        if any(directory.iterdir()):
            kept.append(str(directory))
            journal.begin(f"legacy-dir:{directory.name}", path=str(directory))
            journal.done(
                f"legacy-dir:{directory.name}", observed="left (holds other content)"
            )
            continue
        journal.begin(f"legacy-dir:{directory.name}", path=str(directory))
        directory.rmdir()
        _fsync_dir(directory.parent)
        journal.done(f"legacy-dir:{directory.name}", observed="removed")
    plan["legacy_dirs_kept"] = kept


def _remove_fd_tree(parent_fd: int, name: str) -> None:
    """Remove a child without following a swapped symlink during traversal."""
    try:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(info.st_mode):
        child_fd = os.open(
            name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd
        )
        try:
            for child in os.listdir(child_fd):
                _remove_fd_tree(child_fd, child)
            os.rmdir(name, dir_fd=parent_fd)
            os.fsync(parent_fd)
        finally:
            os.close(child_fd)
    elif stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
        os.unlink(name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    else:
        raise MigrationError(f"refusing to delete non-regular entry: {name}")


def _finalize_restored_apply(state: RunState, plan: dict[str, object]) -> None:
    work_dir = state.work_dir
    if plan.get("work_dir") != str(work_dir) or work_dir != (
        _toolkit_root() / f".migration-{state.ctx.migration_id}"
    ):
        raise MigrationError("finalize plan names an unexpected work directory")
    for name in ("restored", "staging", "transform"):
        if plan.get(name) != str(work_dir / name):
            raise MigrationError(f"finalize plan names an unexpected {name} path")
    manifest = plan.get("manifest")
    if not isinstance(manifest, dict):
        raise MigrationError("finalize plan has no cleanup manifest")
    root_fd = os.open(_toolkit_root(), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try:
            work_fd = os.open(
                work_dir.name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=root_fd,
            )
        except FileNotFoundError:
            work_fd = None
        if work_fd is not None:
            try:
                _check_restored_manifest(work_dir, manifest, resume=True)
                _remove_fd_tree(work_fd, "restored")
                _checkpoint("migrate.finalize.restored-removed")
                for name in ("staging", "transform"):
                    _remove_fd_tree(work_fd, name)
                _checkpoint("migrate.finalize.restored-leftovers-removed")
                if not os.listdir(work_fd):
                    os.rmdir(work_dir.name, dir_fd=root_fd)
                    os.fsync(root_fd)
            finally:
                os.close(work_fd)
        else:
            _checkpoint("migrate.finalize.restored-removed")
            _checkpoint("migrate.finalize.restored-leftovers-removed")
    finally:
        os.close(root_fd)


def _finalize(
    state: RunState, *, restored_copy: bool, journal: Journal | None = None
) -> dict[str, object]:
    plan = _restored_finalize_plan(state) if restored_copy else _finalize_plan(state)
    shutil.rmtree(state.work_dir / "settings-rewrite", ignore_errors=True)
    directory = state.ctx.installer_state / "migrations" / state.ctx.migration_id
    state.journal = journal or Journal.create(directory, FINALIZE_NAME)
    try:
        state.journal.begin("finalize", **plan)
        _checkpoint(
            "migrate.finalize.restored-begun"
            if restored_copy
            else "migrate.finalize.begun"
        )
        if restored_copy:
            _finalize_restored_apply(state, plan)
        else:
            _finalize_apply(state, plan)
        state.journal.done("finalize")
        state.journal.end(OUTCOME_FINALIZED)
        _checkpoint(
            "migrate.finalize.restored-ended"
            if restored_copy
            else "migrate.finalize.ended"
        )
    finally:
        state.journal.close()
    return plan


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
        restored_copy = _finalize_mode(directory, read_records(directory))
        _finalize(state, restored_copy=restored_copy, journal=journal)
        return
    try:
        if ("finalize", "done") not in events:
            mode = begin.get("mode")
            if mode == "restored-copy":
                _finalize_restored_apply(state, begin)
            elif mode == "committed" or (
                mode is None
                and {"snapshot_dir", "retained", "work_dir"} <= begin.keys()
            ):
                _finalize_apply(state, begin)
            else:
                raise MigrationError(f"{directory.name} has an unknown finalize mode")
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


def _finalize_mode(directory: Path, records: list[dict[str, object]]) -> bool:
    """Whether finalize removes a restored copy; reject unsupported states."""
    outcome = _outcome(records)
    rollback = directory / ROLLBACK_NAME
    if outcome == OUTCOME_RESTORED:
        if rollback.exists():
            raise MigrationError(f"{directory.name} has an unexpected rollback journal")
        return True
    if outcome != OUTCOME_COMMITTED:
        raise MigrationError(
            f"{directory.name} is not a committed or restored migration"
        )
    if rollback.exists():
        if _outcome(read_records(directory, ROLLBACK_NAME)) != OUTCOME_ROLLED_BACK:
            raise MigrationError(f"{directory.name} has an unfinished rollback")
        return True
    return False


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
            if kind == "rollback":
                records = _open_committed(directory, kind, other)
                restored_copy = False
            else:
                records = read_records(directory)
                restored_copy = _finalize_mode(directory, records)
            report.journal = str(directory / name)
            if (directory / name).exists():
                report.outcome = f"{kind}: already done"
                if kind == "finalize":
                    begin = next(
                        (
                            r.get("detail")
                            for r in read_records(directory, name)
                            if r.get("phase") == "finalize"
                            and r.get("event") == "begin"
                        ),
                        None,
                    )
                    if isinstance(begin, dict):
                        report.telemetry_lines_discarded = begin.get(
                            "telemetry_lines_discarded", {}
                        )
            else:
                state = _load_state(directory, None, records, repo_root, history)
                if kind == "rollback":
                    _rollback(state)
                    report.outcome = (
                        f"{OUTCOME_ROLLED_BACK}: the layout is legacy again"
                    )
                else:
                    plan = _finalize(state, restored_copy=restored_copy)
                    if restored_copy:
                        report.telemetry_lines_discarded = plan[
                            "telemetry_lines_discarded"
                        ]  # type: ignore[assignment]
                        discarded = ", ".join(
                            f"{domain}={count if count is not None else 'unknown'}"
                            for domain, count in sorted(
                                report.telemetry_lines_discarded.items()
                            )
                        )
                        report.outcome = (
                            f"{OUTCOME_FINALIZED}: restored copy removed; "
                            f"telemetry lines discarded: {discarded or 'none'}"
                        )
                    else:
                        report.outcome = (
                            f"{OUTCOME_FINALIZED}: the legacy snapshot is gone"
                        )
                    report.legacy_dirs_kept = [
                        str(p)
                        for p in plan.get("legacy_dirs_kept", [])  # type: ignore[arg-type]
                    ]
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


def _strict_journal_dirs(installer_state: Path) -> list[Path]:
    """Migration journal directories, refusing anything the lenient scan skips.

    The strict evidence check the residue audit uses: a SYMLINKED
    ``migrations`` root, a symlinked journal directory, a non-directory entry
    under ``migrations/``, a missing MAIN journal file, a malformed final
    (torn) line and a corrupt earlier line all refuse — a strict scan must
    not report a clean audit while expected journals went uninspected, and
    incomplete evidence is not a pass. The lenient reader
    (``agent-scripts/retained_legacy_links``) keeps its existing behavior for
    the session-start drift hook.
    """
    root = installer_state / "migrations"
    if root.is_symlink():
        raise MigrationError(f"{root} is a symlink; refusing to audit it")
    if not root.is_dir():
        return []
    dirs: list[Path] = []
    for entry in sorted(root.iterdir()):
        if entry.is_symlink():
            raise MigrationError(f"{entry} is a symlink; refusing to audit it")
        if not entry.is_dir():
            raise MigrationError(
                f"{entry} is not a directory; refusing to audit past it"
            )
        if not (entry / JOURNAL_NAME).is_file():
            raise MigrationError(
                f"{entry} has no {JOURNAL_NAME}; refusing to audit past it"
            )
        dirs.append(entry)
    return dirs


def _strict_records(directory: Path) -> list[dict[str, object]]:
    """Journal records with a torn-tail refusal (the lenient reader skips it)."""
    raw = (directory / JOURNAL_NAME).read_bytes()
    records, _offset, torn = _split_valid(raw)
    if torn:
        raise MigrationError(
            f"{directory / JOURNAL_NAME} ends with a malformed final line"
        )
    return records


def _mapped_dirs(home: Path) -> set[Path]:
    """The mapped legacy toolkit directories (``_MAPPED_DIRS``)."""
    return {home / ".claude" / name for name in _MAPPED_DIRS}


def _carry_staging_path(ctx: MigrationContext, name: str) -> str:
    return str(
        _toolkit_root() / f".migration-{ctx.migration_id}" / "staging" / "carry" / name
    )


def _migration_states(installer_state: Path) -> list[dict[str, object]]:
    """One per-journal state row for the residue audit's ownership rules."""
    rows: list[dict[str, object]] = []
    for directory in _strict_journal_dirs(installer_state):
        records = _strict_records(directory)
        outcome = _outcome([dict(r) for r in records]) or ""
        finalized = (
            _outcome([dict(r) for r in read_records(directory, FINALIZE_NAME)])
            == OUTCOME_FINALIZED
        )
        rolled_back = (
            _outcome([dict(r) for r in read_records(directory, ROLLBACK_NAME)])
            == OUTCOME_ROLLED_BACK
        )
        if not outcome:
            state = "live"
        elif outcome == OUTCOME_COMMITTED:
            state = (
                "rolled-back"
                if rolled_back
                else ("finalized" if finalized else "committed-unfinalized")
            )
        elif outcome == OUTCOME_RESTORED:
            state = "finalized-restored" if finalized else "restored-unfinalized"
        else:
            state = "abandoned"
        rows.append(
            {
                "id": directory.name,
                "state": state,
                "carried": _carried_names(records),
                "legacy_dirs": _legacy_dirs_from_records(directory, records),
            }
        )
    return rows


def _carried_names(records: list[dict[str, object]]) -> list[str]:
    """The carried entry names journaled by this migration's preflight."""
    for record in records:
        if record.get("phase") == "preflight" and record.get("event") == "done":
            detail = record.get("detail")
            if isinstance(detail, dict):
                carry = detail.get("carry")
                if isinstance(carry, list):
                    return [
                        str(e.get("name"))
                        for e in carry
                        if isinstance(e, dict) and e.get("name")
                    ]
    return []


def _legacy_dirs_from_records(
    directory: Path, records: list[dict[str, object]]
) -> set[Path]:
    """The legacy toolkit dirs this migration's links step recorded.

    Older journals predate the begin field: their fallback is the mapped
    three plus their own planned and retained destinations' legacy parents.
    """
    home = Path.home()
    mapped = {home / ".claude" / name for name in _MAPPED_DIRS}
    extra: set[Path] = set()
    destinations: set[str] = set()
    for record in records:
        if record.get("phase") != "links":
            continue
        detail = record.get("detail")
        if not isinstance(detail, dict):
            continue
        for key in ("planned", "retained"):
            for link in detail.get(key, []) or []:
                if isinstance(link, dict) and link.get("dest"):
                    destinations.add(str(link["dest"]))
    for dest in destinations:
        path = Path(dest)
        if path.is_relative_to(home / ".claude") and path.parent != home / ".claude":
            extra.add(path.parent)
    return mapped | extra


def residue_findings(home: Path, installer_state: Path) -> dict[str, list[str]]:
    """Toolkit-owned residue at legacy locations, for --check-links.

    ONE rule, per journal, no global suppression: the layout must be
    toolkit-home and residue checking ALWAYS runs, exempting only what a
    journal legitimately owns — its snapshot directory (live or awaiting its
    finalize), its retained links (while retention is live), its carried
    legacy entries (while the journal is live), its restored copies, and its
    journaled legacy toolkit directories until its finalize completes.
    Everything else fails, including residue left by a committed-finalized
    journal and an overlapping name any closed journal should have removed.
    Each unfinalized restored journal is also reported as cleanup still
    owed. Fails closed: raises on unreadable journal state (never a silent
    pass). After all journals have finalized, ANY non-empty unclassified
    legacy entry that shows no toolkit attribution is a classify-by-hand
    listing — older journals never recorded carry names, so the audit does
    not rely on carried-name attribution for them.
    """
    if agent_toolkit_paths.current_layout() != "toolkit-home":
        rows = _migration_states(installer_state)
        return {
            "residue": [],
            "manual": [],
            "owed": [
                f"{row['id']} — run --finalize-toolkit-home-migration {row['id']}"
                for row in rows
                if row["state"] == "restored-unfinalized"
            ],
        }
    rows = _migration_states(installer_state)
    owned_snapshots: set[str] = set()
    owned_carried: set[str] = set()
    owned_dirs: set[Path] = set()
    for row in rows:
        if row["state"] in ("live", "committed-unfinalized", "restored-unfinalized"):
            owned_snapshots.add(f".toolkit-home-snapshot-{row['id']}")
        if row["state"] in ("live", "committed-unfinalized"):
            owned_carried |= set(row["carried"])
        owned_dirs |= set(row["legacy_dirs"])
    carried_by_any: set[str] = set()
    for row in rows:
        carried_by_any |= set(row["carried"])

    residue: list[str] = []
    manual: list[str] = []
    owed: list[str] = []
    data_root = home / ".claude" / "data"
    domain_names = {_legacy(d).name for d in agent_toolkit_paths.DOMAINS}
    pointer = agent_toolkit_paths.POINTER_RELPATH.name
    if data_root.is_dir() and not data_root.is_symlink():
        for entry in sorted(data_root.iterdir()):
            if entry.name == pointer:
                continue
            if entry.name.startswith(".toolkit-home-snapshot-"):
                if entry.name not in owned_snapshots:
                    residue.append(str(entry))
                continue
            if entry.name in domain_names:
                residue.append(str(entry))
                continue
            if entry.name in owned_carried:
                continue
            if entry.name in carried_by_any:
                residue.append(str(entry))
                continue
            manual.append(str(entry))

    for directory in sorted(_mapped_dirs(home) | owned_dirs):
        if not _lexists(directory) or not directory.is_dir() or directory.is_symlink():
            continue
        if directory in owned_dirs:
            continue
        holds_toolkit = False
        for child in directory.iterdir():
            if child.is_symlink():
                if os.readlink(child).startswith(str(home / ".agent-toolkit")):
                    holds_toolkit = True
                    break
            elif child.is_relative_to(home / ".agent-toolkit"):
                holds_toolkit = True
                break
        if holds_toolkit:
            residue.append(f"{directory} (holds a toolkit-owned link)")
        elif not any(directory.iterdir()):
            residue.append(f"{directory} (empty and still present after finalize)")
        else:
            manual.append(f"{directory} (holds content matching no toolkit-owned rule)")

    owed = [
        f"{row['id']} — run --finalize-toolkit-home-migration {row['id']}"
        for row in rows
        if row["state"] == "restored-unfinalized"
    ]
    return {
        "residue": sorted(set(residue)),
        "manual": sorted(set(manual)),
        "owed": owed,
    }


def _collect_stale_references(installer_state: Path) -> list[dict[str, str]]:
    """Stale references recorded in the journals of finished migrations.

    recover() returns only migration id and action summaries, and run() emits
    a NEW report for its new migration — so a crash after staging leaves the
    promised review checklist only in an old journal. This reads the journals
    directly (the stale-scan step records survive every exit path, including
    a carry that fails after its scan but before its done record) so the
    earlier run's references reach the report the rollout operator actually
    reads.
    """
    records: list[dict[str, str]] = []
    for directory in _journal_dirs(installer_state):
        for record in read_records(directory):
            if not str(record.get("phase", "")).startswith("stale-scan:"):
                continue
            if record.get("event") != "done":
                continue
            detail = record.get("detail")
            if not isinstance(detail, dict):
                continue
            for ref in detail.get("stale", []) or []:
                if isinstance(ref, dict):
                    records.append({k: str(v) for k, v in ref.items()})
    return records


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
    """Legacy links a toolkit-home migration still needs; cleanup and the audit skip them.

    Delegates to the stdlib-only reader in agent-scripts/retained_legacy_links
    so the SessionStart drift hook can import just that module instead of this
    whole migration toolchain; see that module for the journal-reading logic
    and the exact skip rules. Its signature and contract are identical here.
    """
    from retained_legacy_links import retained_legacy_links as _read_kept

    return _read_kept(installer_state)


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
    for item in report.unfinished_migrations:
        print(
            f"  unfinished migration {item}: finalize or roll it back before "
            "re-running; its journal holds the stale-reference checklist",
            file=stream,
        )
    for ref in [*report.recovered_stale_references, *report.stale_references]:
        where = ref.get("source") or ref.get("file")
        print(
            f"  stale reference: {where} ({ref.get('file')}) "
            f"line {ref['line']}: {ref['value']}",
            file=stream,
        )
    for path in report.legacy_dirs_kept:
        print(
            f"  legacy directory kept (holds other content): {path}",
            file=stream,
        )
    for path in report.unclassified_left:
        print(
            f"  empty unclassified legacy directory left in place, classify by "
            f"hand: {path}",
            file=stream,
        )
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
    report.manual_edit_checklist = next(
        (f.paths for f in report.findings if f.check == "manual-edit-checklist"), []
    )

    report.unfinished_migrations = committed_unfinalized(history.parent)
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
            report.recovered_stale_references = _collect_stale_references(
                ctx.installer_state
            )
            report.findings = run_checks(ctx)
            report.manual_edit_checklist = next(
                (
                    f.paths
                    for f in report.findings
                    if f.check == "manual-edit-checklist"
                ),
                [],
            )
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
                report.stale_references = _collect_stale_references(ctx.installer_state)
                for record in read_records(journal.directory):
                    if (
                        record.get("phase") == "preflight"
                        and record.get("event") == "done"
                    ):
                        detail = record.get("detail")
                        if isinstance(detail, dict):
                            report.unclassified_left = [
                                str(p) for p in detail.get("carry_empties", []) or []
                            ]
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
            _sweep_skeletons(state)
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
