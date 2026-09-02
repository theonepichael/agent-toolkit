#!/usr/bin/env python3
"""vitals-promotion.py — mechanical vitals-promotion pass over grill session data.

Reads every grill session under DATA_DIR, classifies each closed decision
into AUTO_PROMOTE / NEEDS_REVIEW / PENDING_VERIFICATION / SCHEMA_ANOMALY, and
(when --apply is passed) writes AUTO_PROMOTE decisions out as vitals records
and NEEDS_REVIEW decisions out as a dated review pile. A supersede pass keeps
previously-promoted vitals records honest against the session data's current
state: a promoted decision that got removed, reopened, revised, or that no
longer classifies as AUTO_PROMOTE has its vitals record flipped to
"superseded" (never deleted) so a fresh promotion can replace it.

This is a mechanical, rerunnable pass — no cross-session contradiction
detection, no curation. Dry-run by default; pass --apply to write.

Flags
  --quiet, -q    suppress non-essential output
  --verbose, -v  emit extra diagnostic messages to stderr

Requires Python 3.12+.
"""

import argparse
import json
import os
import tempfile
from collections import Counter
from contextlib import suppress
from datetime import date, datetime
from pathlib import Path
from typing import TypedDict, cast

import cli_common

DATA_DIR = Path.home() / ".claude" / "data" / "grill"
VITALS_DIR = DATA_DIR / "vitals"
NEEDS_REVIEW_DIR = DATA_DIR / "needs-review"

VALID_SOURCES = {"user", "defaulted", "assumed", "tested"}
VALID_RESULTS = {"VERIFIED", "DISPUTED", "UNVERIFIABLE"}

AUTO_PROMOTE = "AUTO_PROMOTE"
NEEDS_REVIEW = "NEEDS_REVIEW"
PENDING_VERIFICATION = "PENDING_VERIFICATION"
SCHEMA_ANOMALY = "SCHEMA_ANOMALY"
BUCKETS = (AUTO_PROMOTE, NEEDS_REVIEW, PENDING_VERIFICATION, SCHEMA_ANOMALY)


# ── data model (subset of grill.py's schema; read-only consumer here) ────────


class Verdict(TypedDict):
    result: str
    evidence: str
    date: str


class Decision(TypedDict):
    id: str
    question: str
    reasoning: str
    decision: str | None
    source: str | None
    verdict: Verdict | None


class Session(TypedDict):
    schema_version: int
    slug: str
    topic: str
    created: str
    updated: str
    plan_path: str | None
    pending_execution: bool
    backlog_slug: str | None
    decisions: list[Decision]


class VitalsRecord(TypedDict, total=False):
    text: str
    reasoning: str
    source_slug: str
    source_decision_id: str
    backlog_slug: str | None
    confidence: str
    promoted_at: str
    status: str
    superseded_at: str
    reason: str


class NeedsReviewEntry(TypedDict):
    source_slug: str
    source_decision_id: str
    backlog_slug: str | None
    question: str
    decision: str
    reasoning: str
    source: str | None
    verdict: Verdict | None
    flag_reason: str


DecisionKey = tuple[str, str]


# ── helpers ───────────────────────────────────────────────────────────────────


def now_iso() -> str:
    return datetime.now().isoformat()


def is_open(decision: Decision) -> bool:
    return decision.get("decision") is None


def load_all_sessions(data_dir: Path) -> list[Session]:
    if not data_dir.exists():
        return []
    sessions: list[Session] = []
    for path in sorted(data_dir.glob("*.json")):
        sessions.append(cast(Session, json.loads(path.read_text())))
    return sessions


def build_decision_lookup(sessions: list[Session]) -> dict[DecisionKey, Decision]:
    lookup: dict[DecisionKey, Decision] = {}
    for session in sessions:
        for decision in session["decisions"]:
            lookup[(session["slug"], decision["id"])] = decision
    return lookup


def atomic_write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, prefix=".vitals_tmp_")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        with suppress(OSError):
            os.unlink(tmp_path)
        raise


def load_vitals_file(path: Path) -> list[VitalsRecord]:
    if not path.exists():
        return []
    return cast(list[VitalsRecord], json.loads(path.read_text()))


def vitals_path(vitals_dir: Path, backlog_slug: str | None) -> Path:
    return vitals_dir / f"{backlog_slug or '_global'}.json"


def latest_needs_review_file(needs_review_dir: Path) -> Path | None:
    """Return the most recently dated needs-review file, or None if none exist.

    Each run overwrites a full snapshot (not a delta) to a same-day file and
    a fresh file on a new day — only the newest file is non-redundant.
    """
    if not needs_review_dir.exists():
        return None
    files = sorted(needs_review_dir.glob("*-needs-review.json"))
    return files[-1] if files else None


def summarize_needs_review(entries: list[NeedsReviewEntry]) -> str:
    """One-line summary: count plus the earliest-dated entry, by source_slug prefix."""
    if not entries:
        return "needs-review: 0 entries"
    oldest = min(entries, key=lambda e: e["source_slug"][:10])
    question = oldest["question"]
    snippet = question if len(question) <= 80 else question[:77] + "..."
    return (
        f"needs-review: {len(entries)} entries (oldest: "
        f"{oldest['source_slug'][:10]} {oldest['source_decision_id']} — {snippet!r})"
    )


# ── classification ───────────────────────────────────────────────────────────


def anomaly_reason(decision: Decision) -> str:
    reasons: list[str] = []
    source = decision.get("source")
    if source not in VALID_SOURCES:
        reasons.append(f"invalid source {source!r}")
    verdict = decision.get("verdict")
    if verdict is not None and verdict.get("result") not in VALID_RESULTS:
        reasons.append(f"invalid verdict.result {verdict.get('result')!r}")
    return "; ".join(reasons) or "unspecified anomaly"


def classify_decision(decision: Decision) -> str:
    """Classify one closed decision. Caller must have already filtered out open ones."""
    source = decision.get("source")
    verdict = decision.get("verdict")
    if source not in VALID_SOURCES:
        return SCHEMA_ANOMALY
    if verdict is not None and verdict.get("result") not in VALID_RESULTS:
        return SCHEMA_ANOMALY

    vresult = verdict.get("result") if verdict else None
    if source == "user" and vresult == "VERIFIED":
        return AUTO_PROMOTE
    if (
        source in ("assumed", "defaulted")
        or vresult in ("DISPUTED", "UNVERIFIABLE")
        or (source == "tested" and verdict is None)
    ):
        return NEEDS_REVIEW
    return PENDING_VERIFICATION


def needs_review_reason(decision: Decision) -> str:
    """Which NEEDS_REVIEW sub-condition fired, in priority order (for the breakdown)."""
    source = decision.get("source")
    verdict = decision.get("verdict")
    vresult = verdict.get("result") if verdict else None
    if vresult in ("DISPUTED", "UNVERIFIABLE"):
        return "disputed_unverifiable"
    if source in ("assumed", "defaulted"):
        return "assumed_defaulted"
    if source == "tested" and verdict is None:
        return "tested_no_verdict"
    return "unknown"  # unreachable given classify_decision's NEEDS_REVIEW condition


# ── supersede pass ───────────────────────────────────────────────────────────


def supersede_reason(
    record: VitalsRecord, lookup: dict[DecisionKey, Decision]
) -> str | None:
    """Return why ``record`` should be superseded, or None if it's still valid."""
    key = (record.get("source_slug", ""), record.get("source_decision_id", ""))
    current = lookup.get(key)
    if current is None:
        return "source decision no longer exists in session (removed)"
    if is_open(current):
        return "source decision is open again"
    bucket = classify_decision(current)
    if bucket != AUTO_PROMOTE:
        return f"source decision no longer meets auto-promote criteria (now {bucket})"
    if current.get("decision") != record.get("text") or current.get(
        "reasoning", ""
    ) != record.get("reasoning", ""):
        return "source decision text/reasoning revised post-promotion"
    return None


# ── report ───────────────────────────────────────────────────────────────────


class Report(TypedDict):
    sessions_scanned: int
    total_decisions: int
    open_decisions: int
    bucket_counts: dict[str, int]
    promoted_count: int
    superseded_count: int
    anomaly_entries: list[tuple[str, str, str]]  # (session_slug, decision_id, reason)
    needs_review_entries: list[NeedsReviewEntry]
    needs_review_path: Path
    dirty_vitals_paths: list[Path]


def run(data_dir: Path, apply: bool) -> Report:
    vitals_dir = data_dir / "vitals"
    needs_review_dir = data_dir / "needs-review"

    sessions = load_all_sessions(data_dir)
    lookup = build_decision_lookup(sessions)

    # supersede pass — load every existing vitals file and re-check each
    # active record against current session state.
    existing_paths = sorted(vitals_dir.glob("*.json")) if vitals_dir.exists() else []
    vitals_by_path: dict[Path, list[VitalsRecord]] = {
        p: load_vitals_file(p) for p in existing_paths
    }
    superseded_count = 0
    dirty_paths: set[Path] = set()
    for path, records in vitals_by_path.items():
        for record in records:
            if record.get("status") != "active":
                continue
            reason = supersede_reason(record, lookup)
            if reason is not None:
                record["status"] = "superseded"
                record["superseded_at"] = now_iso()
                record["reason"] = reason
                superseded_count += 1
                dirty_paths.add(path)

    # classify every closed decision
    bucket_counts: Counter[str] = Counter()
    open_decisions = 0
    total_decisions = 0
    auto_promote_items: list[tuple[Session, Decision]] = []
    needs_review_entries: list[NeedsReviewEntry] = []
    anomaly_entries: list[tuple[str, str, str]] = []

    for session in sessions:
        for decision in session["decisions"]:
            total_decisions += 1
            if is_open(decision):
                open_decisions += 1
                continue
            bucket = classify_decision(decision)
            bucket_counts[bucket] += 1
            if bucket == AUTO_PROMOTE:
                auto_promote_items.append((session, decision))
            elif bucket == NEEDS_REVIEW:
                verdict = decision.get("verdict")
                needs_review_entries.append(
                    {
                        "source_slug": session["slug"],
                        "source_decision_id": decision["id"],
                        "backlog_slug": session.get("backlog_slug"),
                        "question": decision.get("question", ""),
                        "decision": decision.get("decision") or "",
                        "reasoning": decision.get("reasoning", ""),
                        "source": decision.get("source"),
                        "verdict": verdict,
                        "flag_reason": needs_review_reason(decision),
                    }
                )
            elif bucket == SCHEMA_ANOMALY:
                anomaly_entries.append(
                    (session["slug"], decision["id"], anomaly_reason(decision))
                )

    # promote pass — append a fresh vitals record for each AUTO_PROMOTE
    # decision that doesn't already have a matching active record.
    promoted_count = 0
    for session, decision in auto_promote_items:
        path = vitals_path(vitals_dir, session.get("backlog_slug"))
        records = vitals_by_path.setdefault(path, [])
        already_promoted = any(
            r.get("status") == "active"
            and r.get("source_slug") == session["slug"]
            and r.get("source_decision_id") == decision["id"]
            and r.get("text") == decision["decision"]
            and r.get("reasoning", "") == decision.get("reasoning", "")
            for r in records
        )
        if already_promoted:
            continue
        records.append(
            {
                "text": cast(str, decision["decision"]),
                "reasoning": decision.get("reasoning", ""),
                "source_slug": session["slug"],
                "source_decision_id": decision["id"],
                "backlog_slug": session.get("backlog_slug"),
                "confidence": "verified",
                "promoted_at": now_iso(),
                "status": "active",
            }
        )
        promoted_count += 1
        dirty_paths.add(path)

    needs_review_path = (
        needs_review_dir / f"{date.today().isoformat()}-needs-review.json"
    )

    if apply:
        for path in dirty_paths:
            atomic_write_json(path, vitals_by_path[path])
        atomic_write_json(needs_review_path, needs_review_entries)

    return {
        "sessions_scanned": len(sessions),
        "total_decisions": total_decisions,
        "open_decisions": open_decisions,
        "bucket_counts": {b: bucket_counts.get(b, 0) for b in BUCKETS},
        "promoted_count": promoted_count,
        "superseded_count": superseded_count,
        "anomaly_entries": anomaly_entries,
        "needs_review_entries": needs_review_entries,
        "needs_review_path": needs_review_path,
        "dirty_vitals_paths": sorted(dirty_paths),
    }


def print_report(report: Report, apply: bool, quiet: bool = False) -> None:
    mode = "APPLIED" if apply else "DRY RUN (pass --apply to write)"
    cli_common.qprint(f"── vitals-promotion: {mode} ──", quiet=quiet)
    cli_common.qprint(f"sessions scanned:    {report['sessions_scanned']}", quiet=quiet)
    cli_common.qprint(f"total decisions:     {report['total_decisions']}", quiet=quiet)
    cli_common.qprint(f"open (skipped):      {report['open_decisions']}", quiet=quiet)
    cli_common.qprint("bucket counts (closed decisions):", quiet=quiet)
    for bucket in BUCKETS:
        cli_common.qprint(
            f"  {bucket:<20} {report['bucket_counts'][bucket]}", quiet=quiet
        )
    cli_common.qprint(f"promoted this run:   {report['promoted_count']}", quiet=quiet)
    cli_common.qprint(f"superseded this run: {report['superseded_count']}", quiet=quiet)
    cli_common.qprint(
        f"schema anomalies:    {len(report['anomaly_entries'])}", quiet=quiet
    )
    if report["anomaly_entries"]:
        for slug, decision_id, reason in report["anomaly_entries"]:
            cli_common.qprint(f"  ANOMALY {slug}:{decision_id} — {reason}", quiet=quiet)
    if apply:
        cli_common.qprint(
            f"needs-review written to: {report['needs_review_path']}", quiet=quiet
        )
        for path in report["dirty_vitals_paths"]:
            cli_common.qprint(f"vitals written: {path}", quiet=quiet)
    else:
        cli_common.qprint(
            f"needs-review would be written to: {report['needs_review_path']}",
            quiet=quiet,
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    cli_common.add_verbosity_args(parser)
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DATA_DIR,
        help="grill session data directory (default: ~/.claude/data/grill)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write vitals/needs-review files (default: dry-run, prints only)",
    )
    parser.add_argument(
        "--needs-review-summary",
        action="store_true",
        help="print a one-line summary of the latest needs-review file and exit",
    )
    args = parser.parse_args()

    if args.needs_review_summary:
        path = latest_needs_review_file(args.data_dir / "needs-review")
        if path is None:
            cli_common.qprint(
                "needs-review: 0 entries", quiet=getattr(args, "quiet", False)
            )
        else:
            entries = cast(list[NeedsReviewEntry], json.loads(path.read_text()))
            cli_common.qprint(
                summarize_needs_review(entries),
                quiet=getattr(args, "quiet", False),
            )
        return

    report = run(args.data_dir, args.apply)
    print_report(report, args.apply, quiet=getattr(args, "quiet", False))


if __name__ == "__main__":
    main()
