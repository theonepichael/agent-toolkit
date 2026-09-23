#!/usr/bin/env python3
"""The ``--migrate-toolkit-home`` command: preflight, dry run, write-ahead journal.

Run through ``./install.sh --migrate-toolkit-home --harness=<list>`` from the
repository checkout; never installed. This release builds the command's
safety frame only and moves no data:

* Every preflight check runs read-only. ``--dry-run`` stops there and writes
  nothing at all: no lock file, no directory, no journal, no history.
* A real run takes the migration lock exclusively (never waiting for it),
  recovers any journal an earlier crashed run left behind, repeats the
  checks under the lock, then writes a write-ahead journal and an immutable
  inventory, records one ``history.jsonl`` line, and stops with
  ``stopped:stages-not-built``.

It does not share ``install.py``'s best-effort ``Reporter`` path: the first
failure aborts. It never enters ``run_install``, so orphan cleanup, settings
seeding, and global Git configuration cannot run during it.

Journal
-------
``<installer state>/migrations/<id>/journal.jsonl``, where the installer state
directory is the one holding ``history.jsonl``
(``$HOME/.local/state/agent-toolkit``; it ignores ``XDG_STATE_HOME``, unlike
the migration lock). One JSON object per line:
``{seq, ts, id, phase, event, detail}``. Each record is one ``write()`` on an
append-only descriptor followed by ``fsync``, before the action it describes
(``begin``) and after it (``done``). ``end`` and ``abandoned`` are terminal and
written at most once. A failed or short write poisons the journal: it accepts
no further records and is left for the next run to abandon. Every file or
directory created is followed by an ``fsync`` of its parent directory.

Recovery (next real run, under the lock)
----------------------------------------
* A journal with no terminal record is **abandoned** (``unwind``): stray temp
  inventories are removed, a torn final line is truncated away, one
  ``abandoned`` record is appended, then its history record is added.
* A terminal journal with no history record gets one (``retry``).
* Otherwise nothing is done (``none``).

:data:`RECOVERY_ORACLE` states, for every fault checkpoint, what a crash there
leaves behind and which recovery action is correct.

Exit codes
  0 dry run passed, or real run stopped cleanly; 1 a check refused or the run
  failed; 75 the migration lock is busy.

Requires Python 3.12+. Standard library only.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import secrets
import stat
import sys
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

sys.path.insert(0, str(Path(__file__).resolve().parent / "agent-scripts"))

import agent_toolkit_paths  # noqa: E402 — sibling dir inserted above
import fault_checkpoint  # noqa: E402 — sibling dir inserted above
import link_inspect  # noqa: E402 — sibling dir inserted above
import migration_lock  # noqa: E402 — sibling dir inserted above

MIGRATION_ID_RE = re.compile(r"^mig-\d{8}T\d{6}Z-[0-9a-f]{6}$")
OUTCOME_STOPPED = "stopped:stages-not-built"
TERMINAL_EVENTS = frozenset({"end", "abandoned"})
LOCK_SITE = "toolkit-home-migration"
JOURNAL_NAME = "journal.jsonl"
INVENTORY_NAME = "inventory.json"
INVENTORY_TMP_PREFIX = "inventory.json.tmp"
HASH_CHUNK = 1 << 20

Status = Literal["ok", "warn", "refuse"]
Action = Literal["none", "retry", "unwind"]


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
    """What a crash at one checkpoint leaves behind, and the right recovery."""

    action: Action
    journal_file: bool
    last_event: str | None
    inventory: bool
    history: bool


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
    "migrate.journal.ended",
    "migrate.history.written",
)

RECOVERY_ORACLE: dict[str, Expected] = {
    "migrate.lock.taken": Expected("none", False, None, False, False),
    "migrate.journal.dir-created": Expected("unwind", False, None, False, False),
    "migrate.journal.opened": Expected("unwind", True, None, False, False),
    "migrate.preflight.begun": Expected("unwind", True, "begin", False, False),
    "migrate.preflight.done": Expected("unwind", True, "done", False, False),
    "migrate.inventory.begun": Expected("unwind", True, "begin", False, False),
    "migrate.inventory.tmp-written": Expected("unwind", True, "begin", False, False),
    "migrate.inventory.written": Expected("unwind", True, "begin", True, False),
    "migrate.inventory.done": Expected("unwind", True, "done", True, False),
    "migrate.journal.ended": Expected("retry", True, "end", True, False),
    "migrate.history.written": Expected("none", True, "end", True, True),
}


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


def read_records(directory: Path) -> list[dict[str, object]]:
    """Every valid record in ``directory``'s journal; a torn final line is skipped."""
    path = directory / JOURNAL_NAME
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return []
    return _split_valid(raw)[0]


def is_terminal(directory: Path) -> bool:
    records = read_records(directory)
    return bool(records) and records[-1].get("event") in TERMINAL_EVENTS


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
    """The write-ahead journal of one migration run."""

    def __init__(self, directory: Path, migration_id: str, fd: int) -> None:
        self.directory = directory
        self.migration_id = migration_id
        self._fd = fd
        self._seq = 0
        self.poisoned = False
        self.terminal = False

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
        fd = os.open(
            directory / JOURNAL_NAME,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND,
            0o644,
        )
        os.fsync(fd)
        _fsync_dir(directory)
        _checkpoint("migrate.journal.opened")
        return cls(directory, migration_id, fd)

    def _append(self, phase: str, event: str, detail: dict[str, object]) -> None:
        if self.poisoned:
            raise JournalError(
                f"journal {self.directory} is poisoned by a failed write"
            )
        if self.terminal:
            raise JournalError(
                f"journal {self.directory} already has a terminal record"
            )
        self._seq += 1
        data = _record(self.migration_id, self._seq, phase, event, detail)
        try:
            _write_all(self._fd, data)
            os.fsync(self._fd)
        except (OSError, JournalError) as exc:
            self.poisoned = True
            raise JournalError(f"journal write failed: {exc}") from exc
        if event in TERMINAL_EVENTS:
            self.terminal = True

    def begin(self, phase: str, **detail: object) -> None:
        self._append(phase, "begin", detail)

    def done(self, phase: str, **detail: object) -> None:
        self._append(phase, "done", detail)

    def end(self, outcome: str, **detail: object) -> None:
        self._append("run", "end", {"outcome": outcome, **detail})

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
    path = directory / JOURNAL_NAME
    created = not path.exists()
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        raw = b""
        while chunk := os.read(fd, HASH_CHUNK):
            raw += chunk
        records, valid, _torn = _split_valid(raw)
        if valid != len(raw):
            os.ftruncate(fd, valid)
            os.fsync(fd)
        prefix = b"\n" if valid and not raw[:valid].endswith(b"\n") else b""
        seq = int(records[-1].get("seq", 0)) + 1 if records else 1  # type: ignore[call-overload]
        data = prefix + _record(
            directory.name, seq, "recover", "abandoned", {"reason": reason}
        )
        os.lseek(fd, 0, os.SEEK_END)
        _write_all(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    if created:
        _fsync_dir(directory)


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


def history_ids(path: Path) -> set[str]:
    ids: set[str] = set()
    for entry in link_inspect.read_manifest_entries(path):
        if entry.get("kind") == "migration" and isinstance(entry.get("id"), str):
            ids.add(entry["id"])  # type: ignore[arg-type]
    return ids


def recover(installer_state: Path, history: Path) -> list[dict[str, str]]:
    """Abandon unfinished journals and repair missing history records."""
    actions: list[dict[str, str]] = []
    known = history_ids(history)
    for directory in _journal_dirs(installer_state):
        if not is_terminal(directory):
            abandon(directory, "the run that wrote this journal died before finishing")
            outcome, action = "abandoned", "unwind"
        elif directory.name not in known:
            outcome = str(
                read_records(directory)[-1].get("detail", {}).get("outcome", "")
            )  # type: ignore[union-attr]
            action = "retry"
        else:
            continue
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
    return actions


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
            report.recovered = recover(ctx.installer_state, ctx.history)
            report.findings = run_checks(ctx)
            if report.refused:
                report.outcome = "refused"
                _emit(report, opts)
                return 1
            journal = Journal.open(ctx.installer_state, ctx.migration_id)
            report.journal = str(journal.directory / JOURNAL_NAME)
            try:
                _run_journalled(ctx, journal, report)
            finally:
                journal.close()
            append_history(
                ctx.history,
                {
                    "kind": "migration",
                    "id": ctx.migration_id,
                    "outcome": OUTCOME_STOPPED,
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
    report.outcome = f"{OUTCOME_STOPPED}: preflight passed, nothing moved"
    _emit(report, opts)
    return 0


def _run_journalled(
    ctx: MigrationContext, journal: Journal, report: PreflightReport
) -> None:
    try:
        journal.begin("preflight")
        _checkpoint("migrate.preflight.begun")
        journal.done(
            "preflight",
            warnings=sorted({f.check for f in report.findings if f.status == "warn"}),
        )
        _checkpoint("migrate.preflight.done")
        journal.begin("inventory")
        _checkpoint("migrate.inventory.begun")
        inventory = build_inventory(ctx, strict=True)
        digest = write_inventory(journal.directory, inventory)
        report.inventory = inventory
        journal.done("inventory", sha256=digest)
        _checkpoint("migrate.inventory.done")
        journal.end(OUTCOME_STOPPED)
        _checkpoint("migrate.journal.ended")
    except JournalError:
        raise
    except Exception as exc:
        if not journal.terminal and not journal.poisoned:
            journal.end("aborted", error=str(exc))
        raise
