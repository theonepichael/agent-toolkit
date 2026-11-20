#!/usr/bin/env python3
"""guard_rails_report.py — read-only report over the guard-rails audit log.

Summarises ``guard_rails_audit.jsonl`` (one JSON line per guarded tool call,
written by ``guard_rails._audit_verdict``) without modifying it: counts by
harness, verdict and rule, the top denied command stems, and the stems denied
often enough to be worth reviewing as permission-list candidates.

Deliberately small: a line that does not parse is skipped and counted, and a
command that will not tokenize contributes no stem. Stems are best-effort —
the audit target is the redacted first 200 characters of the command, and the
stem is the first token after any leading ``VAR=value`` assignments and wrapper
words, so a compound command reports its first command only.

Only the newest ``--max-lines`` lines are kept, so a growing telemetry log costs
bounded memory.

Flags:
  --json, -j       emit the report as one JSON object
  --limit N        display rows per text section (default 10)
  --threshold N    denial count that flags a stem as a candidate (default 3)
  --max-lines N    only the newest N lines of the log are read (default 50000)
  --quiet, -q      suppress the text report (--json is unaffected)
  --verbose, -v    emit extra diagnostic messages to stderr

Env vars: AGENT_TOOLKIT_HOME (toolkit root override; must be absolute).
Files read: <toolkit-root>/data/guard_rails_audit.jsonl
Files written: none.
Exit codes: 0 success (including a missing log); 1 the layout cannot be
resolved or the log path cannot be read; 2 bad usage.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from collections import Counter, deque
from pathlib import Path

import agent_toolkit_paths
import cli_common

# What this module does with toolkit data (checked by scripts/check_toolkit_paths.py).
TOOLKIT_DATA = "reader"

# Leading words whose own arguments are not the command a stem should report:
# `sudo git status` and `env python3 x.py` are about `git` and `python3`.
_WRAPPERS = frozenset({"sudo", "env", "timeout", "nice"})


def _ranked(counts: Counter[str]) -> list[tuple[str, int]]:
    """Descending count, then ascending key — stable across runs."""
    return sorted(counts.items(), key=lambda row: (-row[1], row[0]))


def _stem(command: str) -> str | None:
    """The command stem of an audit ``target``, or None if it has none."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    while tokens:
        name, sep, _ = tokens[0].partition("=")
        if (sep and name.isidentifier()) or tokens[0] in _WRAPPERS:
            tokens = tokens[1:]
            continue
        break
    return Path(tokens[0]).name if tokens and tokens[0] else None


def build_report(path: Path, *, threshold: int, max_lines: int) -> dict[str, object]:
    """Aggregate the audit log's newest ``max_lines`` lines into one report."""
    decisions: Counter[str] = Counter({"allow": 0, "deny": 0})
    harnesses: Counter[str] = Counter()
    rules: Counter[str] = Counter()
    stems: Counter[str] = Counter()
    records = malformed = 0

    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            window = deque(handle, maxlen=max_lines)
    except FileNotFoundError:
        window = deque()

    for line in window:
        try:
            record = json.loads(line)
        except ValueError:
            malformed += 1
            continue
        if not isinstance(record, dict):
            malformed += 1
            continue
        records += 1
        harnesses[record.get("harness") or ""] += 1
        rules[record.get("rule") or ""] += 1
        decisions[record.get("decision") or ""] += 1
        if record.get("decision") == "deny" and record.get("tool") == "bash":
            stem = _stem(record.get("target") or "")
            if stem:
                stems[stem] += 1

    denied_stems = [{"stem": s, "denials": n} for s, n in _ranked(stems)]
    return {
        "path": str(path),
        "records_read": records,
        "malformed": malformed,
        "decisions": dict(_ranked(decisions)),
        "harnesses": [
            {"harness": name, "total": total} for name, total in _ranked(harnesses)
        ],
        "rules": [{"rule": name, "total": total} for name, total in _ranked(rules)],
        "denied_stems": denied_stems,
        "candidates": [row for row in denied_stems if row["denials"] >= threshold],
    }


def _stem_rows(rows: list[dict[str, object]], limit: int) -> list[str]:
    kept = [f"  {row['stem']}  {row['denials']}" for row in rows[:limit]]
    return kept or ["  (none)"]


def render_text(report: dict[str, object], *, limit: int, threshold: int) -> str:
    """Render ``report`` as the human-readable text form."""

    def cells(section: str, field: str, empty: str) -> str:
        rows = report[section][:limit]
        return "  ".join(f"{r[field] or empty}={r['total']}" for r in rows) or "(none)"

    decisions = " ".join(
        f"{name or '(none)'}={count}" for name, count in report["decisions"].items()
    )
    rows = [
        f"guard-rails audit report: {report['path']}",
        f"records read: {report['records_read']}  malformed skipped: {report['malformed']}",
        f"decisions: {decisions}",
        f"harnesses: {cells('harnesses', 'harness', '(unattributed)')}",
        f"rules: {cells('rules', 'rule', '(none)')}",
        "top denied command stems:",
        *_stem_rows(report["denied_stems"], limit),
        f"permission-list review candidates (denied >= {threshold} times):",
        *_stem_rows(report["candidates"], limit),
    ]
    return "\n".join(rows)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="read-only report over the guard-rails audit log",
    )
    parser.add_argument("--json", "-j", action="store_true", help="emit JSON")
    parser.add_argument(
        "--limit", type=int, default=10, metavar="N", help="rows per text section"
    )
    parser.add_argument(
        "--threshold", type=int, default=3, metavar="N", help="candidate denials"
    )
    parser.add_argument(
        "--max-lines", type=int, default=50000, metavar="N", help="newest N lines"
    )
    cli_common.add_verbosity_args(parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        path = agent_toolkit_paths.path_for("guard-rail-log")
    except agent_toolkit_paths.LayoutError as exc:
        print(f"guard_rails_report: {exc}", file=sys.stderr)
        return 1
    try:
        report = build_report(path, threshold=args.threshold, max_lines=args.max_lines)
    except OSError as exc:
        print(f"guard_rails_report: cannot read {path}: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        cli_common.qprint(
            render_text(report, limit=args.limit, threshold=args.threshold),
            quiet=args.quiet,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
