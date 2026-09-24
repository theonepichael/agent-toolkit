#!/usr/bin/env python3
"""Read which legacy symlinks an unfinalized toolkit-home migration still keeps.

A committed migration keeps the legacy links its new links.toml no longer
produces until its finalize deletes them: the migration's ``links`` step
records them as ``retained``. Between migrate and finalize, every other tool
that audits symlinks -- install.py's ``--check-links`` and the SessionStart
drift hook (agent-scripts/link_drift_check.py) -- would otherwise report
those links as orphaned drift. The callers pass the set this returns to
``link_inspect.collect_link_findings`` as ``retained=`` so those links are
exempted instead of flagged.

This module is deliberately stdlib-only and self-contained. The drift hook
runs at every session start and must not import the large migrate_toolkit_home
module (which pulls in the rest of the migration toolchain and is meant to run
once, from the installer). It re-reads the same append-only journals directly,
so the hook and the installer cannot disagree about which links a live
migration still owns.

The journals live under the installer state directory -- the one holding
history.jsonl (``$HOME/.local/state/agent-toolkit``; it ignores XDG_STATE_HOME,
unlike the migration lock). Callers pass that directory in: install.py
resolves it from the manifest path's parent, and the drift hook does the same
through ``link_inspect.manifest_path``.
"""

from __future__ import annotations

import json
from pathlib import Path

# What this module does with toolkit data (checked by scripts/check_toolkit_paths.py).
TOOLKIT_DATA = "none"

JOURNAL_NAME = "journal.jsonl"
FINALIZE_NAME = "finalize.jsonl"
OUTCOME_FINALIZED = "finalized"
TERMINAL_EVENTS = frozenset({"end", "abandoned"})
CONTROL_PHASES = frozenset({"run", "recover", "restore", "finalize"})


class RetainedLinksError(OSError):
    """A migration journal could not be read (unreadable directory or file, or a
    corrupt journal line). A subclass of OSError so callers that catch the
    latter -- install.py exempting nothing, the drift hook staying silent --
    catch it too."""


def retained_legacy_links(installer_state: Path) -> set[Path]:
    """Legacy link destinations a committed-but-unfinalized migration keeps.

    Reads every migration journal under ``installer_state/migrations``. A
    migration whose finalize completed in ``restored-copy`` mode has already
    given those links back to the legacy layout, so its retained set is no
    longer "needed" -- it is skipped. Every other committed migration's
    ``links`` step records the legacy links it kept; their destinations are
    returned.

    Raises only :class:`RetainedLinksError` (a subclass of OSError) when a
    journal directory or file cannot be read, or a journal line is corrupt,
    so callers can choose their own failure policy. Mirrors the reader
    migrate_toolkit_home.retained_legacy_links used to be, before it delegated
    here.
    """
    kept: set[Path] = set()
    for directory in _journal_dirs(installer_state):
        if _outcome(_read_records(directory, FINALIZE_NAME)) == OUTCOME_FINALIZED:
            finalized = _read_records(directory, FINALIZE_NAME)
            begin = next(
                (
                    r.get("detail")
                    for r in finalized
                    if r.get("phase") == "finalize" and r.get("event") == "begin"
                ),
                None,
            )
            if not isinstance(begin, dict) or begin.get("mode") != "restored-copy":
                continue
        for record in _steps_in(_read_records(directory)):
            done = record["done"]
            if record["phase"] != "links" or done is None:
                continue
            for link in done.get("retained", []):  # type: ignore[attr-defined]
                kept.add(Path(str(link["dest"])))
    return kept


# ── journal reading (stdlib-only; mirrors migrate_toolkit_home's primitives) ──


def _journal_dirs(installer_state: Path) -> list[Path]:
    """Migration journal directories under ``installer_state/migrations``."""
    root = installer_state / "migrations"
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir() if p.is_dir() and not p.is_symlink())


def _read_records(directory: Path, name: str = JOURNAL_NAME) -> list[dict[str, object]]:
    """Every valid record in one journal file; a torn final line is skipped.

    A missing file is not an error (no journal of that kind exists yet) and
    yields an empty list. Any other read failure, or a corrupt earlier line,
    raises :class:`RetainedLinksError`.
    """
    path = directory / name
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return []
    return _split_valid(raw)[0]


def _split_valid(raw: bytes) -> tuple[list[dict[str, object]], int, bool]:
    """Parse journal bytes: (records, byte length of the valid prefix, torn tail?).

    A malformed final line is a torn tail and is skipped; a malformed earlier
    line raises :class:`RetainedLinksError`.
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
            raise RetainedLinksError(f"malformed journal line {index + 1}")
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


def _outcome(records: list[dict[str, object]]) -> str | None:
    """The ``end`` outcome of a finished journal, else None."""
    if not records or records[-1].get("event") != "end":
        return None
    detail = records[-1].get("detail")
    return str(detail.get("outcome")) if isinstance(detail, dict) else None


def _steps_in(records: list[dict[str, object]]) -> list[dict[str, object]]:
    """Build ``{"phase", "begin", "done"}`` step records from journal records.

    Control phases (run/recover/restore/finalize) are skipped; a step is
    present once its ``begin`` has been seen, and its ``done`` is filled in
    when the matching ``done`` record arrives.
    """
    steps: list[dict[str, object]] = []
    latest: dict[str, dict[str, object]] = {}
    for record in records:
        phase = str(record.get("phase", ""))
        if phase in CONTROL_PHASES:
            continue
        detail = record.get("detail")
        detail = detail if isinstance(detail, dict) else {}
        if record.get("event") == "begin":
            step: dict[str, object] = {"phase": phase, "begin": detail, "done": None}
            steps.append(step)
            latest[phase] = step
        elif record.get("event") == "done" and phase in latest:
            latest[phase]["done"] = detail
    return steps
