#!/usr/bin/env python3
"""worker_run_metrics.py — read-only worker-run metrics report.

Joins the launch ledger to worker sessions and the backlog store and prints,
per model and harness, how worker runs actually behaved: items launched, items
landed, send-back count (counted from ``sendback`` journal events, rendered
``unmeasured`` only when the journal is unreadable),
median time and median spend to first write, and total spend per landed item.

Spend units differ by harness and stay native — never converted or summed
across harnesses: pi is the session's recorded native USD cost (``usage.cost
.total``), agy is distinct model calls, Copilot is AI Credits (unmeasurable
today). Every spend column is labelled with its unit; an unmeasurable value
keeps its native unit with a completeness flag, and a harness whose unit is
unassignable renders ``unmeasured`` — never 0 and never USD.

Model labels use one rule: a pi session's label is its full
``provider/modelId:level`` form (from its model_change and thinking_level_change
records), so a worker launched with ``--model opencode-go/mimo-v2.6-pro:high``
and one launched without a flag share a row; the ``:level`` suffix is kept
because the mimo experiment compares thinking levels. The launch-time ``model``
is used only when the session has no model info (e.g. agy).

Flags:
  --json, -j     emit output as JSON (a list of row objects; [] when empty)
  --quiet, -q    suppress non-essential output
  --verbose, -v  emit extra diagnostic messages to stderr

Env vars: AGENT_TOOLKIT_HOME (toolkit root override; must be absolute).
Files read:
  <toolkit-root>/data/launch_log.jsonl
  <toolkit-root>/data/backlog/items.json
  <toolkit-root>/data/backlog/journal.jsonl
  ~/.pi/agent/sessions/**/*.jsonl        (pi worker sessions)
  ~/.gemini/antigravity-cli/conversations/<id>.db   (agy worker sessions)
Files written: none.
Exit codes: 0 success; 1 operational error (launch log unreadable); 2 bad usage.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TextIO

import agent_toolkit_paths
import analyze_sessions
import cli_common

# What this module does with toolkit data (checked by scripts/check_toolkit_paths.py).
TOOLKIT_DATA = "reader"

_AGY_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)

# Native spend unit per worker harness. Copilot's AIC is reserved but not yet
# reachable: copilot workers are not recorded with a session id, so they
# surface as an unknown harness with no assignable unit.
_SPEND_UNITS: dict[str, str] = {
    "pi": "USD",
    "agy": "calls",
}


@dataclass
class Launch:
    """One resolved launch-log worker record."""

    order: int
    ts: datetime | None
    slug: str | None
    harness: str
    session_id: str | None
    launch_model: str | None
    agy_db: str | None = None


@dataclass
class SessionInfo:
    """Measured data for one joined worker session."""

    harness: str
    session_model: str | None
    minutes_to_first_write: float | None
    spend_to_first_write: float | None
    first_write_complete: bool
    total_spend: float | None
    total_spend_complete: bool
    readable: bool


def _classify_session(worker_session: object) -> tuple[str, str | None, str | None]:
    """Resolve ``(harness, canonical_session_id, agy_db_path)`` from a ``worker_session``.

    pi sessions are recorded as their JSONL path; agy sessions as a
    conversation UUID or a ``.db`` path. One agy conversation recorded once as
    a UUID and once as a ``.db`` path resolves to the same canonical id. The
    ``agy_db_path`` is the exact database file to open (the supplied ``.db``
    path, or the default conversations dir for a bare UUID) -- never re-derived
    by appending ``.db`` to a path. Anything else is an unknown harness with no
    joinable session.
    """
    if not isinstance(worker_session, str) or not worker_session.strip():
        return "unknown", None, None
    value = worker_session.strip()
    if value.endswith(".jsonl") and value.startswith("/"):
        return "pi", value, None
    if value.endswith(".db"):
        stem = Path(value).stem
        canonical = stem if _AGY_UUID_RE.match(stem) else value
        return "agy", canonical, value
    if _AGY_UUID_RE.match(value):
        db = analyze_sessions._agy_conversations_dir() / f"{value}.db"
        return "agy", value, str(db)
    return "unknown", None, None


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    """Read JSONL objects, skipping malformed lines.

    A missing file yields ``[]``. Any other ``OSError`` (file exists but cannot
    be read) propagates to the caller, which decides whether that is fatal.
    """
    out: list[dict[str, object]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
    except FileNotFoundError:
        return []
    return out


def _read_items(path: Path) -> dict[str, dict[str, object]]:
    """Read the backlog store's ``items.json`` as ``slug -> item``; {} on failure."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    items = raw.get("items")
    if not isinstance(items, list):
        return {}
    out: dict[str, dict[str, object]] = {}
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            out[item["id"]] = item
    return out


def _latest_done_ts(path: Path) -> dict[str, str]:
    """Latest journal ``approve``/``done`` event timestamp per slug; {} on failure.

    The journal is append-only, so the last matching line wins.
    """
    out: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}
    for line in lines:
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get("to_status") != "done":
            continue
        if entry.get("cmd") not in ("approve", "done"):
            continue
        slug, ts = entry.get("slug"), entry.get("ts")
        if isinstance(slug, str) and isinstance(ts, str):
            out[slug] = ts
    return out


def _sendback_events(path: Path) -> list[tuple[str, str]] | None:
    """Every ``sendback`` journal event as ``(slug, ts)``; ``None`` if unreadable.

    Each send-back is an independent event, so all are returned in order
    (unlike :func:`_latest_done_ts`, which keeps only the last match).
    ``None`` -- not an empty list -- distinguishes an unreadable journal from
    a readable one with no send-backs, so the report renders ``unmeasured``
    for the former and ``0`` for the latter.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    out: list[tuple[str, str]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get("cmd") != "sendback":
            continue
        slug, ts = entry.get("slug"), entry.get("ts")
        if isinstance(slug, str) and isinstance(ts, str):
            out.append((slug, ts))
    return out


def _parse_ts(raw: object) -> datetime | None:
    if not isinstance(raw, str):
        return None
    _, dt = analyze_sessions.normalize_timestamp(raw)
    return dt


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    if n % 2:
        return ordered[n // 2]
    return (ordered[n // 2 - 1] + ordered[n // 2]) / 2.0


def _pi_model_label(path: Path) -> str | None:
    """The session's full model label ``provider/modelId:level``, else None.

    Built from the session's ``model_change`` (provider + modelId) and
    ``thinking_level_change`` (thinkingLevel) records, so a worker launched
    with ``--model opencode-go/mimo-v2.6-pro:high`` and one launched without a
    flag (whose session records the same provider/model/level) share one
    label -- the launch flag is not a separate row. The ``:level`` suffix is
    kept because the mimo experiment compares thinking levels.
    """
    provider: str | None = None
    model_id: str | None = None
    level: str | None = None
    for item in analyze_sessions._iter_jsonl(path, "pi"):
        if isinstance(item, analyze_sessions.SkippedRecord):
            continue
        t = item.get("type")
        if t == "model_change":
            if isinstance(item.get("provider"), str) and item["provider"].strip():
                provider = item["provider"].strip()
            if isinstance(item.get("modelId"), str) and item["modelId"].strip():
                model_id = item["modelId"].strip()
        elif t == "thinking_level_change":
            if (
                isinstance(item.get("thinkingLevel"), str)
                and item["thinkingLevel"].strip()
            ):
                level = item["thinkingLevel"].strip()
    if not model_id:
        return None
    label = f"{provider}/{model_id}" if provider else model_id
    if level:
        label = f"{label}:{level}"
    return label


def _pi_session_info(path: Path) -> SessionInfo:
    """Measure one pi session via the shared milestone scanner (one scan)."""
    session_id, _cwd, model, _start, events, skipped = (
        analyze_sessions._scan_pi_milestone(path)
    )
    io_skips = [s for s in skipped if getattr(s, "kind", None) == "io"]
    if io_skips:
        return SessionInfo(
            harness="pi",
            session_model=None,
            minutes_to_first_write=None,
            spend_to_first_write=None,
            first_write_complete=False,
            total_spend=None,
            total_spend_complete=False,
            readable=False,
        )
    ms = analyze_sessions._milestone_finish(
        harness="pi",
        session_id=session_id,
        cwd=_cwd,
        model=model,
        start=_start,
        events=events,
        repo="/",
    )
    first_spend = ms.spend_to_first_write
    total_spend = ms.total_spend
    if first_spend is not None and not math.isfinite(first_spend):
        first_spend = None
    if total_spend is not None and not math.isfinite(total_spend):
        total_spend = None
    complete = not skipped
    return SessionInfo(
        harness="pi",
        session_model=_pi_model_label(path),
        minutes_to_first_write=ms.minutes_to_first_write,
        spend_to_first_write=first_spend,
        first_write_complete=complete
        and (ms.status != "ok" or ms.first_write_cost_origin == "native"),
        total_spend=total_spend,
        total_spend_complete=complete
        and ms.total_cost_origin not in ("partial", "unavailable"),
        readable=True,
    )


def _agy_session_info(db_path: str) -> SessionInfo:
    """Measure one agy conversation's distinct model calls, read-only."""
    measured = analyze_sessions.read_agy_session_calls(
        Path(db_path), strict_read_only=True
    )
    readable = not measured.unreadable
    calls = measured.calls if measured.complete else None
    return SessionInfo(
        harness="agy",
        session_model=None,
        minutes_to_first_write=None,
        spend_to_first_write=None,
        first_write_complete=True,
        total_spend=calls,
        total_spend_complete=measured.complete,
        readable=readable,
    )


def _landing_ts(
    item: dict[str, object], done_ts: dict[str, str], slug: str
) -> datetime | None:
    for raw in (item.get("completed_at"), done_ts.get(slug)):
        dt = _parse_ts(raw)
        if dt is not None:
            return dt
    return None


@dataclass
class Row:
    """Aggregate metrics for one (model, harness) group."""

    harness: str
    model: str
    launched: int = 0
    landed: int = 0
    sent_back: int = 0
    unjoined: int = 0
    first_write_minutes: list[float] = field(default_factory=list)
    first_write_spend: list[float] = field(default_factory=list)
    first_write_spend_complete: bool = True


def compute_report(
    launch_records: list[dict[str, object]],
    items: dict[str, dict[str, object]],
    done_ts: dict[str, str],
    sendbacks: list[tuple[str, str]] | None = None,
) -> list[dict[str, object]]:
    """Pure aggregation: launch records + store data -> per-(model, harness) rows."""
    launches: list[Launch] = []
    for order, rec in enumerate(launch_records):
        if rec.get("kind") != "worker":
            continue
        slug = rec.get("slug")
        slug = slug if isinstance(slug, str) and slug.strip() else None
        model = rec.get("model")
        launch_model = model if isinstance(model, str) and model.strip() else None
        harness, session_id, agy_db = _classify_session(rec.get("worker_session"))
        _, ts = analyze_sessions.normalize_timestamp(rec.get("ts"))
        launches.append(
            Launch(
                order=order,
                ts=ts,
                slug=slug,
                harness=harness,
                session_id=session_id,
                launch_model=launch_model,
                agy_db=agy_db,
            )
        )

    # Session ownership: earliest referencing launch owns the session.
    owner: dict[str, Launch] = {}
    for launch in launches:
        if launch.session_id is None:
            continue
        existing = owner.get(launch.session_id)
        if existing is None:
            owner[launch.session_id] = launch
            continue
        if launch.ts is not None and (existing.ts is None or launch.ts < existing.ts):
            owner[launch.session_id] = launch

    # Session measurements, keyed by canonical session id.
    session_info: dict[str, SessionInfo] = {}
    for session_id in owner:
        if owner[session_id].harness == "pi":
            session_info[session_id] = _pi_session_info(Path(session_id))
        elif owner[session_id].harness == "agy":
            session_info[session_id] = _agy_session_info(owner[session_id].agy_db or "")

    def launch_model_key(launch: Launch) -> str:
        # One rule, documented in the module docstring: the session's full
        # label (provider/modelId:level) wins for pi so a flagged launch and
        # an unflagged launch of the same model share a row; the launch flag
        # is only a fallback for sessions with no model info (e.g. agy).
        info = session_info.get(launch.session_id or "")
        if info is not None and info.session_model:
            return info.session_model
        if launch.launch_model:
            return launch.launch_model
        return "unknown"

    # Landing launch per landed slug: most recent launch at-or-before the
    # landing timestamp (falling back to the most recent launch).
    by_slug: dict[str, list[Launch]] = defaultdict(list)
    for launch in launches:
        if launch.slug:
            by_slug[launch.slug].append(launch)

    landing_launch: dict[str, Launch] = {}
    for slug, slug_launches in by_slug.items():
        item = items.get(slug)
        if item is None or item.get("status") != "done":
            continue
        land_ts = _landing_ts(item, done_ts, slug)
        eligible = [
            ln
            for ln in slug_launches
            if land_ts is None or ln.ts is None or ln.ts <= land_ts
        ]
        landing_launch[slug] = max(
            eligible or slug_launches,
            key=lambda ln: (ln.ts is not None, ln.ts or datetime.min, -ln.order),
        )

    rows_by_group: dict[tuple[str, str], Row] = {}

    def row_for(harness: str, model: str) -> Row:
        key = (harness, model)
        row = rows_by_group.get(key)
        if row is None:
            row = Row(harness=harness, model=model)
            rows_by_group[key] = row
        return row

    # launched (distinct slugs) + unjoined, per launch record's own group.
    launched_slugs: dict[tuple[str, str], set[str]] = defaultdict(set)
    for launch in launches:
        key = (launch.harness, launch_model_key(launch))
        if launch.slug:
            launched_slugs[key].add(launch.slug)
        joined = (
            launch.session_id is not None and session_info[launch.session_id].readable
        )
        item_ok = launch.slug is not None and launch.slug in items
        if not joined or not item_ok:
            row_for(*key).unjoined += 1

    for key, slugs in launched_slugs.items():
        row_for(*key).launched = len(slugs)

    # first-write + landed-spend, attributed to the session's owning group.
    # Per-item session accounting so completeness requires EVERY session of a
    # landed item to be measured (one measured + one missing is incomplete).
    item_account: dict[tuple[str, str], dict[str, list[tuple[float | None, bool]]]] = (
        defaultdict(lambda: defaultdict(list))
    )
    for session_id, info in session_info.items():
        own = owner[session_id]
        key = (own.harness, launch_model_key(own))
        row = row_for(*key)
        if info.readable:
            if info.minutes_to_first_write is not None:
                row.first_write_minutes.append(info.minutes_to_first_write)
            if info.spend_to_first_write is not None:
                row.first_write_spend.append(info.spend_to_first_write)
            if not info.first_write_complete:
                row.first_write_spend_complete = False
        else:
            row.first_write_spend_complete = False  # a skipped session
        if own.slug is not None:
            landed = landing_launch.get(own.slug)
            if (
                landed is not None
                and landed.harness == own.harness
                and (launch_model_key(landed) == launch_model_key(own))
                and (own.ts is None or landed.ts is None or own.ts <= landed.ts)
            ):
                if info.readable:
                    item_account[key][own.slug].append(
                        (info.total_spend, info.total_spend_complete)
                    )
                else:
                    item_account[key][own.slug].append((None, False))

    # landed credit.
    for slug, landed in landing_launch.items():
        row = row_for(landed.harness, launch_model_key(landed))
        row.landed += 1

    # send-back credit: each event goes to the launch active when it was
    # received (the most recent launch for the slug at-or-before the event).
    # An event with no such launch is left unattributed, never credited to a
    # future worker. ``sendbacks is None`` (unreadable journal) leaves the
    # counts unknown.
    if sendbacks is not None:
        for slug, ts_str in sendbacks:
            ts = _parse_ts(ts_str)
            if ts is None:
                continue  # invalid event timestamp -> unattributed
            slug_launches = by_slug.get(slug)
            if not slug_launches:
                continue
            eligible = [ln for ln in slug_launches if ln.ts is not None and ln.ts <= ts]
            if not eligible:
                continue
            chosen = max(eligible, key=lambda ln: (ln.ts, -ln.order))
            row_for(chosen.harness, launch_model_key(chosen)).sent_back += 1

    # assemble output rows
    out: list[dict[str, object]] = []
    for (harness, model), row in sorted(rows_by_group.items()):
        unit = _SPEND_UNITS.get(harness, "unmeasured")
        landed = row.landed
        per_item = item_account.get((harness, model), {})
        measured_sum = 0.0
        covered_items = 0
        all_complete = True
        for sessions in per_item.values():
            item_measured = [s for s, _c in sessions if s is not None]
            if item_measured:
                covered_items += 1
            for value, complete in sessions:
                if value is None:
                    all_complete = False
                else:
                    measured_sum += value
                    if not complete:
                        all_complete = False
        spend_per_landed: dict[str, object] = {
            "value": None,
            "unit": unit,
            "complete": landed > 0 and all_complete and covered_items == landed,
            "covered": covered_items,
            "of": landed,
        }
        if covered_items and math.isfinite(measured_sum):
            spend_per_landed["value"] = measured_sum / covered_items
        median_minutes = _median(row.first_write_minutes)
        median_spend = _median(row.first_write_spend)
        out.append(
            {
                "harness": harness,
                "model": model,
                "launched": row.launched,
                "landed": landed,
                "sent_back": None if sendbacks is None else row.sent_back,
                "unjoined": row.unjoined,
                "first_write": {
                    "median_minutes": median_minutes,
                    "minutes_measured_sessions": len(row.first_write_minutes),
                    "median_spend": median_spend,
                    "spend_unit": unit,
                    "spend_measured_sessions": len(row.first_write_spend),
                    "spend_complete": row.first_write_spend_complete,
                },
                "spend_per_landed": spend_per_landed,
            }
        )
    return out


def _fmt_number(value: object, unit: str, *, per_landed: bool = False) -> str:
    if value is None:
        return f"unmeasured ({unit})" if unit != "unmeasured" else "unmeasured"
    if unit == "USD":
        return f"${float(value):.4f}" + ("/landed" if per_landed else "")
    return f"{float(value):g} {unit}" + ("/landed" if per_landed else "")


def _render_text(rows: list[dict[str, object]], *, quiet: bool, file: TextIO) -> None:
    if not rows:
        cli_common.qprint("no launch records", quiet=quiet, file=file)
        return
    headers = [
        "Harness",
        "Model",
        "Launched",
        "Landed",
        "SentBack",
        "Unjoined",
        "MedTTFW",
        "MedSpendTTFW",
        "SpendPerLanded",
    ]
    table: list[list[str]] = []
    any_incomplete = False
    for r in rows:
        fw = r["first_write"]
        sp = r["spend_per_landed"]
        med_ttfw = (
            f"{float(fw['median_minutes']):.1f}m"
            if fw["median_minutes"] is not None
            else "-"
        )
        med_spend = _fmt_number(fw["median_spend"], fw["spend_unit"])
        per_landed = _fmt_number(sp["value"], sp["unit"], per_landed=True)
        incomplete = not (fw["spend_complete"] and sp["complete"])
        any_incomplete = any_incomplete or incomplete
        if incomplete:
            per_landed += "*"
        table.append(
            [
                str(r["harness"]),
                str(r["model"]),
                str(r["launched"]),
                str(r["landed"]),
                "unmeasured" if r["sent_back"] is None else str(r["sent_back"]),
                str(r["unjoined"]),
                med_ttfw,
                med_spend,
                per_landed,
            ]
        )
    left_align = {0, 1, 2, 3, 4, 5}
    widths = [
        max(len(h), *(len(row[i]) for row in table)) for i, h in enumerate(headers)
    ]
    fmt = lambda row: "  ".join(  # noqa: E731
        f"{row[i]:<{widths[i]}}" if i in left_align else f"{row[i]:>{widths[i]}}"
        for i in range(len(row))
    )
    cli_common.qprint(fmt(headers), quiet=quiet, file=file)
    cli_common.qprint("  ".join("-" * w for w in widths), quiet=quiet, file=file)
    for row in table:
        cli_common.qprint(fmt(row), quiet=quiet, file=file)
    if any_incomplete:
        cli_common.qprint(
            "* spend/first-write coverage incomplete (a session was skipped, "
            "partial, or unmeasurable)",
            quiet=quiet,
            file=file,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="worker_run_metrics",
        description="Read-only per-model/harness worker-run metrics report.",
    )
    parser.add_argument(
        "--json",
        "-j",
        action="store_true",
        help="emit output as JSON (a list of row objects)",
    )
    cli_common.add_verbosity_args(parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    quiet = bool(getattr(args, "quiet", False))
    verbose = bool(getattr(args, "verbose", False))

    launch_path = agent_toolkit_paths.path_for("launch-log")
    work = agent_toolkit_paths.path_for("work-items")

    try:
        launch_records = _read_jsonl(launch_path)
    except OSError as exc:
        print(f"worker_run_metrics: cannot read {launch_path}: {exc}", file=sys.stderr)
        return 1

    items = _read_items(work / "items.json")
    done_ts = _latest_done_ts(work / "journal.jsonl")
    sendbacks = _sendback_events(work / "journal.jsonl")

    if verbose:
        cli_common.vprint(
            f"launch records: {len(launch_records)}; items: {len(items)}",
            verbose=verbose,
        )

    rows = compute_report(launch_records, items, done_ts, sendbacks)

    if args.json:
        print(json.dumps(rows, indent=2))
        return 0

    _render_text(rows, quiet=quiet, file=sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
