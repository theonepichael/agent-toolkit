#!/usr/bin/env python3
"""vitals-promotion.py — mechanical vitals-promotion pass over grill session data.

Classifies each closed decision in a set of grill sessions into AUTO_PROMOTE /
NEEDS_REVIEW / PENDING_VERIFICATION / SCHEMA_ANOMALY, and — on the write path —
promotes AUTO_PROMOTE decisions out as vitals records. A supersede pass keeps
previously-promoted vitals records honest against the session data's current
state: a promoted decision that got removed, reopened, revised, or that no
longer classifies as AUTO_PROMOTE has its vitals record flipped to "superseded"
(never deleted) so a fresh promotion can replace it.

This is a mechanical, rerunnable pass — no cross-session contradiction
detection, no curation. The CLI is dry-run by default; pass --apply to write.

Also supports --search <query> to look up already-promoted vitals records by
keyword without loading the whole store, so a caller (e.g. grill-me's own
pre-step) can check for already-settled facts cheaply.

Flags
  --quiet, -q             suppress non-essential output
  --verbose, -v           emit extra diagnostic messages to stderr
  --search <query>        search vitals records for QUERY and exit
  --backlog-slug <slug>   with --search, also search <slug>.json
  --include-superseded    with --search, also match superseded records
  --json                  with --search, emit JSON instead of plain text

Service API
  Beyond argv, the module splits its effects by function name — there is no
  ``apply`` flag below the CLI, and none may be added to ``promote``:

    ``search_vitals(vitals_dir, keywords, include_superseded, backlog_slug=None)``
      read-only lookup over ``_global.json`` plus at most one backlog file.
    ``classify(sessions, *, vitals_dir)``
      read-only pass; previews pending promotions *and* supersedes and never
      writes — it has no write call, not a disabled one.
    ``promote(sessions, *, vitals_dir)``
      the only write path; always writes exactly the files the pass dirtied.

  ``vitals_dir`` is required on both because a defaulted write target would
  silently mean the live ``~/.claude/data/grill/vitals`` store. Both take a
  caller-supplied ``list[Session]`` (``grill.Session`` — this module reads
  grill's schema, it does not own it) and never mutate it: each call parses the
  store afresh, so a dry run followed by a write on the same list reports the
  same numbers as the write alone.

  Session completeness is the CLI adapter's job: ``main()`` loads sessions via
  ``grill.all_sessions()`` — which skips an unreadable session with a warning
  rather than failing — and refuses to run at all when the count of loaded
  sessions is below the count of session files on disk, because a skipped
  session is indistinguishable from a deleted one to the supersede pass. A
  library caller passes its own list and owns that guarantee: hand it a partial
  list and you can supersede vitals records whose source sessions are fine.

Requires Python 3.12+.
"""

import argparse
import json
import os
import sys
import tempfile
from collections import Counter
from contextlib import suppress
from datetime import datetime
from pathlib import Path
from typing import TextIO, TypedDict, cast

import cli_common
import grill
from grill import Decision, Session, is_open

DATA_DIR = Path.home() / ".claude" / "data" / "grill"
VITALS_DIR = DATA_DIR / "vitals"

VALID_SOURCES = {"user", "defaulted", "assumed", "tested"}
VALID_RESULTS = {"VERIFIED", "DISPUTED", "UNVERIFIABLE"}

AUTO_PROMOTE = "AUTO_PROMOTE"
NEEDS_REVIEW = "NEEDS_REVIEW"
PENDING_VERIFICATION = "PENDING_VERIFICATION"
SCHEMA_ANOMALY = "SCHEMA_ANOMALY"
BUCKETS = (AUTO_PROMOTE, NEEDS_REVIEW, PENDING_VERIFICATION, SCHEMA_ANOMALY)


# ── data model ───────────────────────────────────────────────────────────────
#
# Grill's session shape is imported above rather than restated here:
# ``grill.Session`` is the schema, and loading it through
# ``grill.all_sessions()`` is what keeps this consumer from breaking when that
# schema evolves. ``VitalsRecord`` stays defined here because this module owns
# the vitals store format.


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


DecisionKey = tuple[str, str]


# ── helpers ───────────────────────────────────────────────────────────────────


def now_iso() -> str:
    return datetime.now().isoformat()


def build_decision_lookup(sessions: list[Session]) -> dict[DecisionKey, Decision]:
    lookup: dict[DecisionKey, Decision] = {}
    for session in sessions:
        for decision in session["decisions"]:
            lookup[(session["slug"], decision["id"])] = decision
    return lookup


def atomic_write_json(path: Path, payload: object) -> None:
    """Write ``payload`` atomically, creating the parent directory if needed.

    This ``mkdir`` is the module's only one, and this is reached only from
    ``promote()``'s write loop -- so a dry run cannot create the store.
    """
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
    """Parse one vitals file. Returns brand-new dicts on every call (json.loads),
    which is what keeps the supersede pass's in-place edits invisible to
    callers and to any other pass in the same process."""
    if not path.exists():
        return []
    return cast(list[VitalsRecord], json.loads(path.read_text()))


def vitals_path(vitals_dir: Path, backlog_slug: str | None) -> Path:
    return vitals_dir / f"{backlog_slug or '_global'}.json"


# ── search ────────────────────────────────────────────────────────────────────


def matches_query(record: VitalsRecord, keywords: list[str]) -> bool:
    """True iff every keyword is a case-insensitive substring of text or reasoning.

    No length filtering: substring matching can't distinguish a short
    stopword ("of", "is") from a short domain term ("ci", "cd", "ui", "db"),
    so every keyword is used as given.
    """
    if not keywords:
        raise ValueError("keywords must not be empty")
    haystack = f"{record.get('text', '')} {record.get('reasoning', '')}".lower()
    return all(keyword.lower() in haystack for keyword in keywords)


def search_vitals(
    vitals_dir: Path,
    keywords: list[str],
    include_superseded: bool,
    backlog_slug: str | None = None,
) -> list[VitalsRecord]:
    """Search _global.json (plus <backlog_slug>.json if given) for matches.

    Deliberately not a glob over every *.json in vitals_dir: that would let
    a search from one project's session surface unrelated projects'
    decisions once other backlog-scoped files start existing. Global
    matches come first, then backlog-scoped matches, each in on-disk record
    order. `backlog_slug == "_global"` is a no-op, not a second load of the
    same file.

    The mirror-image decision lives in ``_plan()``'s supersede pass, which
    *does* glob the whole store -- it has to see every record to keep it
    honest. Those two loaders stay separate on purpose; sharing one would
    either make search leak across projects or make the supersede pass blind
    to backlog-scoped files.
    """
    if not keywords:
        raise ValueError("keywords must not be empty")
    paths = [vitals_dir / "_global.json"]
    if backlog_slug and backlog_slug != "_global":
        paths.append(vitals_dir / f"{backlog_slug}.json")

    results: list[VitalsRecord] = []
    for path in paths:
        for record in load_vitals_file(path):
            if not include_superseded and record.get("status") != "active":
                continue
            if matches_query(record, keywords):
                results.append(record)
    return results


def print_search_results(
    results: list[VitalsRecord],
    as_json: bool,
    quiet: bool = False,
    file: TextIO | None = None,
) -> None:
    if as_json:
        cli_common.qprint(json.dumps(results, indent=2), quiet=quiet, file=file)
        return
    if not results:
        cli_common.qprint("no matching vitals records", quiet=quiet, file=file)
        return
    blocks = []
    for record in results:
        backlog_slug = record.get("backlog_slug") or "global"
        blocks.append(
            f"[{record.get('source_slug')}] {record.get('source_decision_id')}  "
            f"(status: {record.get('status')}, backlog_slug: {backlog_slug})\n"
            f"text: {record.get('text', '')}\n"
            f"reasoning: {record.get('reasoning', '')}"
        )
    cli_common.qprint("\n\n".join(blocks), quiet=quiet, file=file)


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
    dirty_vitals_paths: list[Path]


Store = dict[Path, list[VitalsRecord]]


def _plan(sessions: list[Session], vitals_dir: Path) -> tuple[Report, Store]:
    """Compute the whole promotion pass without writing anything.

    Returns the report plus the full post-pass store (every file the pass read
    or would write, keyed by path) so the caller can persist it. Reads are
    complete before this returns, so a malformed vitals file aborts here --
    before any caller gets a chance to write a half-applied store.
    """
    lookup = build_decision_lookup(sessions)

    # supersede pass — load every existing vitals file and re-check each
    # active record against current session state. This globs the whole store
    # on purpose (see search_vitals' opposite choice).
    existing_paths = sorted(vitals_dir.glob("*.json")) if vitals_dir.exists() else []
    store: Store = {p: load_vitals_file(p) for p in existing_paths}
    superseded_count = 0
    dirty_paths: set[Path] = set()
    for path, records in store.items():
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
            elif bucket == SCHEMA_ANOMALY:
                anomaly_entries.append(
                    (session["slug"], decision["id"], anomaly_reason(decision))
                )

    # promote pass — append a fresh vitals record for each AUTO_PROMOTE
    # decision that doesn't already have a matching active record.
    promoted_count = 0
    for session, decision in auto_promote_items:
        path = vitals_path(vitals_dir, session.get("backlog_slug"))
        records = store.setdefault(path, [])
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

    report: Report = {
        "sessions_scanned": len(sessions),
        "total_decisions": total_decisions,
        "open_decisions": open_decisions,
        "bucket_counts": {b: bucket_counts.get(b, 0) for b in BUCKETS},
        "promoted_count": promoted_count,
        "superseded_count": superseded_count,
        "anomaly_entries": anomaly_entries,
        "dirty_vitals_paths": sorted(dirty_paths),
    }
    return report, store


def classify(sessions: list[Session], *, vitals_dir: Path) -> Report:
    """Run the whole pass read-only: report what it *would* promote and supersede.

    Read-only by construction, not by a flag -- there is no write call here, and
    it cannot create ``vitals_dir`` (the only ``mkdir`` is inside
    ``atomic_write_json``, reached only from ``promote()``). Pass a complete
    session list: an incomplete one previews a bogus set of supersedes (the CLI
    guards this; see the Service API section of the module docstring).
    """
    report, _store = _plan(sessions, vitals_dir)
    return report


def promote(sessions: list[Session], *, vitals_dir: Path) -> Report:
    """Run the pass and write every file it dirtied. Always writes.

    The module's only mutating entry point. ``vitals_dir`` is required so no
    call can write the live store by omission; a run with nothing dirty writes
    nothing, and the store directory is not created.
    """
    report, store = _plan(sessions, vitals_dir)
    for path in report["dirty_vitals_paths"]:
        atomic_write_json(path, store[path])
    return report


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
        for path in report["dirty_vitals_paths"]:
            cli_common.qprint(f"vitals written: {path}", quiet=quiet)


def main() -> None:
    # Literal, not ``__doc__``: argparse's description is also the text
    # ``gen_interfaces.py`` extracts for INTERFACES.md's ``CLI (argparse)`` line,
    # and its resolver reads only a string literal or a bare ``__doc__`` name --
    # an expression like ``__doc__.splitlines()[0]`` extracts as "no
    # ``description=`` set", silently dropping the description from the
    # inventory. A bare ``__doc__`` would work but dumps the whole docstring
    # (Flags and Service API sections included) into --help. Same tradeoff
    # grill.py already made. ``CliPassTests`` pins this string to the
    # docstring's first line so the two cannot drift.
    parser = argparse.ArgumentParser(
        description="vitals-promotion.py — mechanical vitals-promotion pass over grill session data."
    )
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
        help="write vitals files (default: dry-run, prints only)",
    )
    parser.add_argument(
        "--search",
        metavar="QUERY",
        help="search vitals records for QUERY (space-separated keywords, "
        "AND-combined) and exit",
    )
    parser.add_argument(
        "--backlog-slug",
        metavar="SLUG",
        help="with --search, also search <SLUG>.json (default: _global.json only)",
    )
    parser.add_argument(
        "--include-superseded",
        action="store_true",
        help="with --search, also match superseded records",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="with --search, emit matching records as a JSON list instead of "
        "plain text",
    )
    args = parser.parse_args()

    # --search reads only the vitals store, so it dispatches before sessions are
    # loaded: the strict-input guard below must never block a cheap lookup.
    if args.search is not None:
        keywords = args.search.split()
        if not keywords:
            print(
                "Error: --search query must contain at least one keyword.",
                file=sys.stderr,
            )
            sys.exit(1)
        results = search_vitals(
            args.data_dir / "vitals",
            keywords,
            args.include_superseded,
            args.backlog_slug,
        )
        print_search_results(results, args.json, quiet=getattr(args, "quiet", False))
        return

    # grill.all_sessions() is tolerant by design (a corrupt or wrong-version
    # session is skipped with a warning), which is right for a read API and
    # wrong for this pass unguarded: a skipped session looks exactly like a
    # deleted one to the supersede pass, so --apply would flip still-valid
    # records to "superseded" off a partial read. all_session_slugs() globs the
    # same `*.json` in the same call (and the vitals store is a subdirectory,
    # which that glob does not recurse into), so a count difference can only
    # mean a file failed to open. Refuse both modes rather than preview a
    # misleading report.
    slugs = grill.all_session_slugs(args.data_dir)
    sessions = grill.all_sessions(data_dir=args.data_dir)
    if len(sessions) != len(slugs):
        print(
            f"Error: {len(slugs) - len(sessions)} of {len(slugs)} grill session "
            f"file(s) in {args.data_dir} could not be read; refusing to run.",
            file=sys.stderr,
        )
        sys.exit(1)

    vitals_dir = args.data_dir / "vitals"
    if args.apply:
        report = promote(sessions, vitals_dir=vitals_dir)
    else:
        report = classify(sessions, vitals_dir=vitals_dir)
    print_report(report, args.apply, quiet=getattr(args, "quiet", False))


if __name__ == "__main__":
    main()
