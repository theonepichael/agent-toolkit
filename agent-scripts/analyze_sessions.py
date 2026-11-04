#!/usr/bin/env python3
"""analyze_sessions.py — multi-harness session analysis tool.

Analyzes coding-agent sessions across pi, Claude Code, opencode, Copilot CLI,
agy, and Codex harnesses. Exposes subcommands for calculating token/USD costs,
reporting per-session time/spend to the first file write (milestones), listing
user prompts, and searching message transcripts.

Usage:
    python3 ~/.agent-toolkit/scripts/analyze_sessions.py cost [--by total|day|project|harness|model|session|item] [filters]
    python3 ~/.agent-toolkit/scripts/analyze_sessions.py milestones [--repo PATH] [filters]
    python3 ~/.agent-toolkit/scripts/analyze_sessions.py prompts [--format markdown|jsonl] [filters]
    python3 ~/.agent-toolkit/scripts/analyze_sessions.py search <query> [--regex] [--context N] [filters]

Flags:
  --harness        harness to analyze: all, pi, claude, opencode, copilot, agy, codex
                   (default: all; note: agy is opt-in and excluded from 'all')
  --since          filter sessions on/after date/time (ISO 8601 YYYY-MM-DD or
                   YYYY-MM-DDTHH:MM:SS, or relative e.g. 7d, today)
  --until          filter sessions on/before date/time (ISO 8601 YYYY-MM-DD or
                   YYYY-MM-DDTHH:MM:SS)
  --cwd            filter by working directory substring
  --repo           (milestones only) repo root whose file writes count;
                   defaults to each session's own cwd. Sessions with neither
                   root report status no-root, never a number. First-write
                   detection needs an explicit write/edit tool call carrying
                   a path (pi/claude/opencode logs); codex, copilot and agy
                   logs carry no such call and are reported as unsupported,
                   never as zero. No launch-time column exists: session logs
                   hold no real launch signal.
  --model          filter by model name substring
  --session        filter by session ID substring
  --limit          maximum number of records or results to return
  --grep, -g       filter message text by substring or regex
  --json           emit output as JSON
  --no-subagents   (cost only) exclude subagent / child sessions
  --include-subagents (prompts/search only) include subagent / child sessions
  --format         (prompts only) output format: markdown or jsonl
  --regex, -r      (search only) treat search query as regular expression
  --context, -C    (search only) number of context lines/messages around matches
  --quiet, -q      suppress non-essential output
  --verbose, -v    emit extra diagnostic messages to stderr

Notes:
  cost --by item groups sessions by the backlog slug parsed from each
  session's launch prompt (/backlog-item ... <slug>); sessions without a
  recognizable launch prompt group as 'unattributed' and an item's cost sums
  across relaunches.

Env vars: none.
Files read:
  ~/.pi/agent/sessions/**/*.jsonl
  ~/.claude/projects/**/*.jsonl
  ~/.local/share/opencode/opencode.db
  ~/.copilot/session-store.db
  ~/.gemini/antigravity-cli/brain/**/transcript_full.jsonl (or transcript.jsonl)
  ~/.codex/sessions/**/*.jsonl
Files written: none.
Exit codes: 0 success; 1 operational error; 2 bad usage.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import re
import sqlite3
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, TextIO

import cli_common

# What this module does with toolkit data (checked by scripts/check_toolkit_paths.py).
TOOLKIT_DATA = "harness-owned"

CLAUDE_PRICING: dict[str, tuple[float, float, float, float]] = {
    # per 1,000,000 tokens: (input, output, cache_read, cache_write[5m]).
    # Verified against platform.claude.com/docs/en/about-claude/pricing
    # (fetched 2026-09-01) -- re-check that page before editing this table.
    #
    # calculate_claude_cost() below matches by LONGEST substring hit, not
    # insertion order: a specific key always outranks a shorter generic one
    # regardless of where either sits in this dict. This is deliberate --
    # the previous first-match-in-order scheme let a brand new model id
    # (e.g. claude-opus-5, claude-haiku-4-5) silently match an unrelated,
    # long-stale generic fallback below just because no specific key existed
    # yet. Longest-match means a missing specific key degrades to the
    # generic fallback's rate rather than an arbitrary older one, and a
    # newly added specific key can never be shadowed by an existing one.
    #
    # The bare "claude-sonnet" / "claude-haiku" / "claude-opus" fallbacks
    # are kept pointed at each family's current-generation rate (not the
    # oldest one) so an unrecognized future model id degrades to a
    # reasonable estimate instead of a wildly wrong one.
    "claude-3-7-sonnet": (3.0, 15.0, 0.30, 3.75),
    "claude-3-5-sonnet": (3.0, 15.0, 0.30, 3.75),
    "claude-sonnet-4": (3.0, 15.0, 0.30, 3.75),  # bare 4, and 4.5/4.6 (same rate)
    "claude-sonnet-5": (2.0, 10.0, 0.20, 2.50),
    "claude-sonnet": (3.0, 15.0, 0.30, 3.75),  # generic fallback
    "claude-3-5-haiku": (0.80, 4.0, 0.08, 1.00),
    "claude-3-haiku": (0.25, 1.25, 0.03, 0.30),
    "claude-haiku-4-5": (1.0, 5.0, 0.10, 1.25),
    "claude-haiku": (1.0, 5.0, 0.10, 1.25),  # generic fallback
    "claude-3-opus": (15.0, 75.0, 1.50, 18.75),
    "claude-opus-4-1": (15.0, 75.0, 1.50, 18.75),  # retired
    "claude-opus-4": (15.0, 75.0, 1.50, 18.75),  # bare Opus 4, retired
    "claude-opus-4-5": (5.0, 25.0, 0.50, 6.25),
    "claude-opus-4-6": (5.0, 25.0, 0.50, 6.25),
    "claude-opus-4-7": (5.0, 25.0, 0.50, 6.25),
    "claude-opus-4-8": (5.0, 25.0, 0.50, 6.25),
    "claude-opus-5": (5.0, 25.0, 0.50, 6.25),
    "claude-opus-5-5": (4.0, 20.0, 0.20, 5.00),
    "claude-opus": (5.0, 25.0, 0.50, 6.25),  # generic fallback
    # Fable/Mythos 5.1 use a special 0.025x cache-hit multiplier (vs. the
    # standard 0.1x every other model above uses) -- Fable/Mythos 5 (no
    # ".1") predate it and still use the standard 0.1x. Mythos ids are a
    # best guess (limited-availability model, id not directly observed);
    # harmless if wrong since an unmatched key just never fires.
    "claude-mythos-5-1": (10.0, 50.0, 0.25, 12.50),
    "claude-fable-5-1": (10.0, 50.0, 0.25, 12.50),
    "claude-mythos-5": (10.0, 50.0, 1.0, 12.50),
    "claude-fable-5": (10.0, 50.0, 1.0, 12.50),
}


@dataclass
class SessionRecord:
    harness: str
    session_id: str
    cwd: str | None
    timestamp: str
    role: str
    text: str
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float | None = None
    cost_origin: str = "unavailable"
    is_subagent: bool = False

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )


def normalize_timestamp(ts: object) -> tuple[str, datetime | None]:
    """Convert string ISO timestamp or int/float epoch ms/s into ISO 8601 string and datetime."""
    if ts is None:
        return "", None
    if isinstance(ts, (int, float)):
        secs = ts / 1000.0 if ts > 1e11 else float(ts)
        dt = datetime.fromtimestamp(secs, tz=UTC)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ"), dt
    if isinstance(ts, str):
        s = ts.strip()
        if not s:
            return "", None
        s_clean = s.replace(" ", "T")
        if s_clean.endswith("Z"):
            s_clean = s_clean[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(s_clean)
            dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ"), dt
        except ValueError:
            return s, None
    return str(ts), None


def parse_date_boundary(val: str | None, *, is_until: bool = False) -> datetime | None:
    if not val:
        return None
    val_clean = val.strip().lower()
    now = datetime.now(UTC)
    if val_clean == "today":
        dt = datetime(now.year, now.month, now.day, tzinfo=UTC)
        if is_until:
            dt += timedelta(days=1) - timedelta(microseconds=1)
        return dt
    if val_clean == "yesterday":
        dt = datetime(now.year, now.month, now.day, tzinfo=UTC) - timedelta(days=1)
        if is_until:
            dt += timedelta(days=1) - timedelta(microseconds=1)
        return dt
    if re.match(r"^(\d+)d$", val_clean):
        days = int(val_clean[:-1])
        return now - timedelta(days=days)
    if re.match(r"^(\d+)w$", val_clean):
        weeks = int(val_clean[:-1])
        return now - timedelta(weeks=weeks)
    try:
        if len(val) == 10 and "-" in val:
            dt = datetime.strptime(val, "%Y-%m-%d").replace(tzinfo=UTC)
            if is_until:
                dt += timedelta(days=1) - timedelta(microseconds=1)
            return dt
        clean = val.replace("Z", "+00:00").replace("z", "+00:00").replace(" ", "T")
        dt = datetime.fromisoformat(clean)
        dt = dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    except ValueError:
        return None
    else:
        return dt


def calculate_claude_cost(
    model: str | None,
    in_tok: int,
    out_tok: int,
    cr_tok: int,
    cw_tok: int,
) -> tuple[float | None, str]:
    if not model:
        return None, "unavailable"
    m_lower = model.lower()
    # Longest matching key wins, not first-in-insertion-order -- see
    # CLAUDE_PRICING's comment for why order-independence matters here.
    best_key = max(
        (key for key in CLAUDE_PRICING if key in m_lower), key=len, default=None
    )
    if best_key is None:
        return None, "unavailable"
    in_rate, out_rate, cr_rate, cw_rate = CLAUDE_PRICING[best_key]
    cost = (
        in_tok * in_rate + out_tok * out_rate + cr_tok * cr_rate + cw_tok * cw_rate
    ) / 1_000_000.0
    return cost, "derived"


OPENAI_PRICING: dict[str, tuple[float, float, float, float]] = {
    # per 1,000,000 tokens: (input, output, cache_read, cache_write).
    # Verified against platform.openai.com/pricing (September 2026).
    # calculate_openai_cost() below matches by LONGEST substring hit.
    "gpt-6-astra": (10.0, 50.0, 1.0, 0.0),
    "gpt-6-sol": (2.0, 10.0, 0.20, 0.0),
    "gpt-6-luna": (0.10, 0.50, 0.01, 0.0),
    "gpt-6": (2.0, 10.0, 0.20, 0.0),  # generic fallback
    "gpt-5.6-sol": (4.0, 20.0, 0.40, 0.0),
    "gpt-5.6-terra": (2.0, 12.0, 0.20, 0.0),
    "gpt-5.6-luna": (0.20, 1.20, 0.02, 0.0),
    "gpt-5.6": (2.0, 12.0, 0.20, 0.0),  # generic fallback
    "gpt-5.5": (2.0, 12.0, 0.20, 0.0),
    "gpt-5.4-mini": (0.75, 4.50, 0.075, 0.0),
    "gpt-5-mini": (0.25, 2.0, 0.025, 0.0),
    "gpt-5-nano": (0.05, 0.40, 0.005, 0.0),
    "gpt-5": (1.25, 10.0, 0.125, 0.0),
    "gpt-4o-mini": (0.15, 0.60, 0.075, 0.0),
    "gpt-4o": (2.50, 10.0, 1.25, 0.0),
    "gpt-4.5": (75.0, 150.0, 37.50, 0.0),
    "gpt-4-turbo": (10.0, 30.0, 5.0, 0.0),
    "gpt-4": (30.0, 60.0, 0.0, 0.0),
    "gpt-3.5-turbo": (0.50, 1.50, 0.0, 0.0),
    "o3-mini": (1.10, 4.40, 0.55, 0.0),
    "o3": (2.0, 8.0, 0.50, 0.0),
    "o1-mini": (1.10, 4.40, 0.55, 0.0),
    "o1-preview": (15.0, 60.0, 7.50, 0.0),
    "o1": (15.0, 60.0, 7.50, 0.0),
    "codex": (2.0, 12.0, 0.20, 0.0),  # generic fallback
}


def calculate_openai_cost(
    model: str | None,
    in_tok: int,
    out_tok: int,
    cr_tok: int,
    cw_tok: int,
) -> tuple[float | None, str]:
    if not model:
        return None, "unavailable"
    m_lower = model.lower()
    best_key = max(
        (key for key in OPENAI_PRICING if key in m_lower), key=len, default=None
    )
    if best_key is None:
        return None, "unavailable"
    in_rate, out_rate, cr_rate, cw_rate = OPENAI_PRICING[best_key]
    cost = (
        in_tok * in_rate + out_tok * out_rate + cr_tok * cr_rate + cw_tok * cw_rate
    ) / 1_000_000.0
    return cost, "derived"


# ── Query service ─────────────────────────────────────────────────────────

# agy is opt-in everywhere in this tool, so "no explicit harnesses" means
# everything except agy — preserving the historical --harness all behavior.
_DEFAULT_HARNESSES: frozenset[str] = frozenset(
    {"pi", "claude", "opencode", "copilot", "codex"}
)
# Line-level decode diagnostics are capped per file; overflow collapses into
# one summary SkippedRecord. Parsing itself never stops early.
_DECODE_SKIP_CAP = 10
_VALID_ROLES = ("user", "assistant")


@dataclass(frozen=True)
class SessionQuery:
    """Typed, immutable filter bundle for query_sessions().

    All matches are record-level (a record failing a filter is dropped
    individually, never whole-session), exactly like the historical
    load_all_records() behavior.
    """

    harnesses: frozenset[str] | None = None
    # None means "everything except agy" (--harness all semantics).
    since: datetime | None = None
    # Must be tz-aware; a naive datetime raises ValueError at query time
    # (no silent UTC-vs-local interpretation).
    until: datetime | None = None
    # Must be tz-aware; same naive-datetime rule.
    cwd: str | None = None
    # Substring match, as --cwd today.
    model: str | None = None
    # Substring match (case-insensitive), as --model today.
    session_id: str | None = None
    # Substring match, as --session today.
    role: str | None = None
    # Exact role match ("user"/"assistant"); prompts passes "user".
    include_subagents: bool = False
    # Restrictive default: callers opt IN to subagent records. The cost
    # adapter passes True (unless --no-subagents); prompts/search pass
    # their --include-subagents flag.
    text: str | None = None
    # Record-text match, case-insensitive in every mode (both historical
    # paths are).
    text_regex: bool = False
    # text is a regex (re.IGNORECASE) when True, case-insensitive substring
    # when False.
    limit: int | None = None
    # Applied after all filtering; <= 0 disables limiting (historical
    # --limit behavior).
    keep: Literal["first", "last"] = "first"
    # Which END of the filtered, timestamp-ascending set the limit keeps:
    # "first" (search) or "last" (prompts). The returned list is ALWAYS
    # timestamp-ascending.


@dataclass(frozen=True)
class SkippedRecord:
    """A tolerated load failure, surfaced as a diagnostic instead of a raise.

    kind is one of "decode" (invalid JSON line), "io" (existing file that
    could not be read), or "db" (corrupt/unusable sqlite db). A path that
    simply does not exist is benign and produces NO diagnostic.
    """

    path: Path
    harness: str
    kind: str
    line_number: int | None = None
    # 1-based JSONL line for kind="decode" line-level entries, else None.
    reason: str = ""


@dataclass(frozen=True)
class CostSummary:
    """Aggregated cost/token rollup over a record list (pure builder output)."""

    sessions: int
    # Count of UNIQUE session_id values — exactly today's
    # len({rec.session_id ...}) semantics (cross-harness id collisions
    # dedupe today too; preserved, not fixed).
    messages: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    total_tokens: int
    cost_usd: float | None
    cost_origin: str


@dataclass(frozen=True)
class SessionQueryResult:
    records: list[SessionRecord]
    # Timestamp-ascending, tie-broken by (harness, session_id).
    skipped_records: list[SkippedRecord]
    # Load diagnostics; never raised.


def _content_text(content: object) -> str:
    """Join the text blocks of a message ``content`` value into one string.

    Accepts a plain string or a list of blocks (dicts carrying a ``text``
    string — pi ``text`` blocks, claude ``text`` blocks, codex
    ``input_text``/``output_text`` blocks — or bare strings). Tool-call
    blocks carry no ``text`` key and contribute nothing.
    """
    text_parts: list[str] = []
    if isinstance(content, str):
        text_parts.append(content)
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                if isinstance(block.get("text"), str):
                    text_parts.append(block["text"])
            elif isinstance(block, str):
                text_parts.append(block)
    return "\n".join(text_parts).strip()


def _iter_jsonl(
    path: Path, harness: str
) -> Iterator[dict[str, object] | SkippedRecord]:
    """Yield parsed JSON objects from a JSONL file, or SkippedRecords.

    Shared by the pi/claude/agy loaders so malformed-line tolerance and the
    per-file decode-diagnostic cap live in exactly one place. Tolerates and
    continues past bad lines (only the diagnostics are capped); a file that
    cannot be opened/read yields one kind="io" skip. Valid JSON that is not
    an object is treated as a decode skip (the historical code would have
    crashed on it with AttributeError).
    """

    bad = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for lineno, line in enumerate(f, 1):
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except json.JSONDecodeError:
                    bad += 1
                    if bad <= _DECODE_SKIP_CAP:
                        yield SkippedRecord(
                            path=path,
                            harness=harness,
                            kind="decode",
                            line_number=lineno,
                            reason="invalid JSON",
                        )
                    continue
                if isinstance(obj, dict):
                    yield obj
                else:
                    bad += 1
                    if bad <= _DECODE_SKIP_CAP:
                        yield SkippedRecord(
                            path=path,
                            harness=harness,
                            kind="decode",
                            line_number=lineno,
                            reason="invalid JSON",
                        )
    except OSError as exc:
        yield SkippedRecord(
            path=path,
            harness=harness,
            kind="io",
            line_number=None,
            reason=f"unreadable: {exc}",
        )
    if bad > _DECODE_SKIP_CAP:
        yield SkippedRecord(
            path=path,
            harness=harness,
            kind="decode",
            line_number=None,
            reason=f"{bad - _DECODE_SKIP_CAP} additional invalid JSON lines",
        )


def aggregate_cost(records: Sequence[SessionRecord]) -> CostSummary:
    """Pure builder: aggregate + cost-origin classification over a record list.

    Reproduces the historical cmd_cost group-row arithmetic exactly,
    including the cost-origin ladder. cost_usd stays None (never 0.0) when
    no record carries a cost, so the "-" rendering path is unchanged.
    """

    sessions = len({rec.session_id for rec in records})
    in_tok = sum(rec.input_tokens for rec in records)
    out_tok = sum(rec.output_tokens for rec in records)
    cr_tok = sum(rec.cache_read_tokens for rec in records)
    cw_tok = sum(rec.cache_write_tokens for rec in records)
    costs = [rec.cost_usd for rec in records if rec.cost_usd is not None]
    cost_sum = sum(costs) if costs else None

    cost_bearing_recs = [
        rec for rec in records if rec.total_tokens > 0 or rec.cost_usd is not None
    ]
    if not cost_bearing_recs:
        origin_str = "unavailable"
    else:
        origins = {rec.cost_origin for rec in cost_bearing_recs}
        if origins == {"native"}:
            origin_str = "native"
        elif origins == {"derived"}:
            origin_str = "derived"
        elif origins == {"unavailable"}:
            origin_str = "unavailable"
        elif origins == {"native", "derived"}:
            origin_str = "native+derived"
        elif "native" in origins or "derived" in origins:
            origin_str = "partial"
        else:
            origin_str = "unavailable"

    return CostSummary(
        sessions=sessions,
        messages=len(records),
        input_tokens=in_tok,
        output_tokens=out_tok,
        cache_read_tokens=cr_tok,
        cache_write_tokens=cw_tok,
        total_tokens=in_tok + out_tok + cr_tok + cw_tok,
        cost_usd=cost_sum,
        cost_origin=origin_str,
    )


def query_sessions(
    query: SessionQuery,
    *,
    roots: Mapping[str, Path | None] | None = None,
) -> SessionQueryResult:
    """Read-only query/report service over harness session stores.

    Effect boundary: filesystem/sqlite reads under caller-supplied roots
    (or loader defaults); no writes, no network, no subprocess. Malformed
    inputs are surfaced as SkippedRecord diagnostics, never raised.
    Timezone/boundary semantics: since/until must be tz-aware (naive
    ValueError); boundaries are inclusive on both ends; records with
    unparseable timestamps are never dropped by a boundary.
    """

    if query.role is not None and query.role not in _VALID_ROLES:
        raise ValueError(f"invalid role: {query.role!r}")
    if query.keep not in ("first", "last"):
        raise ValueError(f"invalid keep: {query.keep!r}")
    for name, dt in (("since", query.since), ("until", query.until)):
        if dt is not None and dt.tzinfo is None:
            raise ValueError(
                f"{name} must be timezone-aware; got naive datetime {dt!r}"
            )
    requested = query.harnesses if query.harnesses is not None else _DEFAULT_HARNESSES
    unknown = sorted(h for h in requested if h not in _DEFAULT_ROOT_GETTERS)
    if unknown:
        raise ValueError("unknown harness(es): " + ", ".join(unknown))
    if roots is not None:
        missing = sorted(h for h in requested if h not in roots)
        if missing:
            raise ValueError(
                "roots missing requested harness(es): " + ", ".join(missing)
            )

    def _root(harness: str) -> Path | None:
        if roots is None:
            return None
        return roots.get(harness)

    tasks: list[
        tuple[str, Callable[[], tuple[list[SessionRecord], list[SkippedRecord]]]]
    ] = []
    if "pi" in requested:
        tasks.append(
            (
                "pi",
                lambda: load_pi_records(_root("pi"), query.since, query.until),
            )
        )
    if "claude" in requested:
        tasks.append(
            (
                "claude",
                lambda: load_claude_records(_root("claude"), query.since, query.until),
            )
        )
    if "opencode" in requested:
        tasks.append(
            (
                "opencode",
                lambda: load_opencode_records(
                    _root("opencode"),
                    query.since,
                    query.until,
                    query.cwd,
                    query.session_id,
                ),
            )
        )
    if "copilot" in requested:
        tasks.append(
            (
                "copilot",
                lambda: load_copilot_records(
                    _root("copilot"),
                    query.since,
                    query.until,
                    query.cwd,
                    query.session_id,
                ),
            )
        )
    if "agy" in requested:
        tasks.append(
            (
                "agy",
                lambda: load_agy_records(
                    _root("agy"), query.since, query.until, query.session_id
                ),
            )
        )
    if "codex" in requested:
        tasks.append(
            (
                "codex",
                lambda: load_codex_records(
                    _root("codex"),
                    query.since,
                    query.until,
                    query.session_id,
                    query.cwd,
                ),
            )
        )

    records: list[SessionRecord] = []
    skipped: list[SkippedRecord] = []
    for _harness, task in tasks:
        recs, skips = task()
        records.extend(recs)
        skipped.extend(skips)

    if query.model:
        mf = query.model.lower()
        records = [r for r in records if r.model and mf in r.model.lower()]
    if query.cwd:
        records = [r for r in records if r.cwd and query.cwd in r.cwd]
    if query.session_id:
        records = [r for r in records if query.session_id in r.session_id]
    if query.role is not None:
        records = [r for r in records if r.role == query.role]
    if not query.include_subagents:
        records = [r for r in records if not r.is_subagent]
    if query.text is not None:
        if query.text_regex:
            pattern = re.compile(query.text, re.IGNORECASE)
            records = [r for r in records if pattern.search(r.text)]
        else:
            t_lower = query.text.lower()
            records = [r for r in records if t_lower in r.text.lower()]

    records.sort(key=lambda r: (r.timestamp, r.harness, r.session_id))
    limit = query.limit
    if limit is not None and limit > 0:
        records = records[-limit:] if query.keep == "last" else records[:limit]

    return SessionQueryResult(records=records, skipped_records=skipped)


# Default root resolvers, keyed by harness name — lambdas so Path.home()
# is evaluated per call (sandbox tests may change HOME after import).
_DEFAULT_ROOT_GETTERS: dict[str, Callable[[], Path]] = {
    "pi": lambda: Path.home() / ".pi" / "agent" / "sessions",
    "claude": lambda: Path.home() / ".claude" / "projects",
    "opencode": lambda: Path.home() / ".local" / "share" / "opencode" / "opencode.db",
    "copilot": lambda: Path.home() / ".copilot" / "session-store.db",
    "agy": lambda: Path.home() / ".gemini" / "antigravity-cli" / "brain",
    "codex": lambda: Path.home() / ".codex" / "sessions",
}


def _resolve_cli_roots() -> Mapping[str, Path | None] | None:
    """Roots for CLI invocations: None means every loader's default path."""

    return None


def _report_skips(skipped: list[SkippedRecord], *, verbose: bool) -> None:
    """Print skip diagnostics to stderr, but only under --verbose."""

    if not verbose:
        return
    for s in skipped:
        print(f"skipped {s.harness} {s.path}: {s.reason}", file=sys.stderr)


# ── Adapters ──────────────────────────────────────────────────────────────


def load_pi_records(
    base_dir: Path | None = None,
    since_dt: datetime | None = None,
    until_dt: datetime | None = None,
) -> tuple[list[SessionRecord], list[SkippedRecord]]:
    if base_dir is None:
        base_dir = Path.home() / ".pi" / "agent" / "sessions"
    records: list[SessionRecord] = []
    skipped: list[SkippedRecord] = []
    if not base_dir.exists():
        return records, skipped
    for p in sorted(base_dir.rglob("*.jsonl")):
        is_subagent_path = "subagent" in str(p).lower()
        session_id = p.stem
        session_cwd: str | None = None
        current_model: str | None = None

        for item in _iter_jsonl(p, "pi"):
            if isinstance(item, SkippedRecord):
                skipped.append(item)
                continue
            obj = item

            t = obj.get("type")
            if t == "session":
                session_id = str(obj.get("id") or session_id)
                session_cwd = obj.get("cwd")
                continue
            if t == "model_change":
                current_model = obj.get("modelId") or current_model
                continue
            if t == "message":
                msg = obj.get("message", {})
                role = msg.get("role")
                if role not in ("user", "assistant"):
                    continue

                ts_val = obj.get("timestamp") or msg.get("timestamp")
                ts_str, dt = normalize_timestamp(ts_val)
                if since_dt and dt and dt < since_dt:
                    continue
                if until_dt and dt and dt > until_dt:
                    continue

                msg_model = obj.get("model") or msg.get("model") or current_model
                if msg_model:
                    current_model = msg_model

                text = _content_text(msg.get("content"))

                usage = obj.get("usage") or msg.get("usage") or {}
                in_tok = int(usage.get("input", 0) or 0)
                out_tok = int(usage.get("output", 0) or 0)
                cr_tok = int(usage.get("cacheRead", 0) or 0)
                cw_tok = int(usage.get("cacheWrite", 0) or 0)

                cost_usd: float | None = None
                cost_origin = "unavailable"
                cost_data = usage.get("cost")
                if isinstance(cost_data, (int, float)):
                    cost_usd = float(cost_data)
                    cost_origin = "native"
                elif isinstance(cost_data, dict):
                    total = cost_data.get("total")
                    if total is not None:
                        cost_usd = float(total)
                        cost_origin = "native"

                records.append(
                    SessionRecord(
                        harness="pi",
                        session_id=session_id,
                        cwd=session_cwd,
                        timestamp=ts_str,
                        role=role,
                        text=text,
                        model=current_model,
                        input_tokens=in_tok,
                        output_tokens=out_tok,
                        cache_read_tokens=cr_tok,
                        cache_write_tokens=cw_tok,
                        cost_usd=cost_usd,
                        cost_origin=cost_origin,
                        is_subagent=is_subagent_path,
                    )
                )
    return records, skipped


# ── pi cost measurement for spend caps ──────────────────────────────────────
#
# Stricter than load_pi_records on purpose: a spend cap must fail closed, so
# anything that could hide a charge is reported as a problem instead of being
# skipped or coerced. load_pi_records keeps its tolerant reporting semantics.


@dataclass(frozen=True)
class PiCost:
    """A pi cost measurement: pi's own price-table estimate, not provider billing.

    ``usd`` is None when the sum overflowed. ``complete`` is True only when the
    source was fully readable and every charge-bearing record yielded a cost.
    """

    usd: float | None
    complete: bool
    problems: tuple[str, ...]


def _pi_usage_cost(usage: object) -> float | None:
    """Strict pi ``usage`` cost: a finite, non-negative number, or ``cost.total``."""
    if not isinstance(usage, dict):
        return None
    cost = usage.get("cost")
    if isinstance(cost, dict):
        cost = cost.get("total")
    if isinstance(cost, bool) or not isinstance(cost, (int, float)):
        return None
    try:
        value = float(cost)
    except OverflowError:
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return value


def _scan_pi_file(path: Path, since: datetime | None) -> PiCost:
    """Sum assistant costs in one pi session file; never raises.

    An unterminated final line is an in-progress write and is dropped. With
    ``since``, only assistant messages timestamped at or after it count, and a
    cost-bearing message without a parseable timestamp is a problem.
    """
    problems: list[str] = []
    try:
        data = path.read_bytes()
    except OSError as exc:
        return PiCost(0.0, False, (f"unreadable: {exc}",))
    lines = data.split(b"\n")
    if not data.endswith(b"\n"):
        lines = lines[:-1]
    total = 0.0
    for lineno, raw in enumerate(lines, 1):
        if not raw.strip():
            continue
        try:
            obj = json.loads(raw)
            if not isinstance(obj, dict):
                problems.append(f"line {lineno}: not a JSON object")
                continue
            if obj.get("type") != "message":
                continue
            msg = obj.get("message")
            if not isinstance(msg, dict):
                problems.append(f"line {lineno}: message is not an object")
                continue
            if msg.get("role") != "assistant":
                continue
            dt: datetime | None = None
            if since is not None:
                # Window first: an out-of-window record cannot spoil today's total.
                _, dt = normalize_timestamp(
                    obj.get("timestamp") or msg.get("timestamp")
                )
                if dt is not None and dt < since:
                    continue
            cost = _pi_usage_cost(msg.get("usage", obj.get("usage")))
            if cost is None:
                problems.append(f"line {lineno}: assistant message without a cost")
                continue
            if since is not None and dt is None:
                problems.append(f"line {lineno}: cost without a timestamp")
                continue
            total += cost
        except Exception as exc:  # noqa: BLE001 — a cap must never crash the watcher
            problems.append(f"line {lineno}: {type(exc).__name__}")
    if not math.isfinite(total):
        problems.append("overflow")
        return PiCost(None, False, tuple(problems))
    return PiCost(total, not problems, tuple(problems))


def pi_session_cost(path: Path) -> PiCost:
    """Total pi-estimated cost of one session file; never raises."""
    return _scan_pi_file(path, None)


def pi_cost_since(since: datetime, base_dir: Path | None = None) -> PiCost:
    """Total pi-estimated cost of every pi session since ``since``; never raises.

    Files whose mtime predates ``since`` are not opened (a file restored with an
    older mtime than its newest message is undercounted). Subagent sessions are
    included. Problems are prefixed with the file name.
    """
    if base_dir is None:
        base_dir = Path.home() / ".pi" / "agent" / "sessions"
    try:
        base_dir.stat()
    except FileNotFoundError:
        return PiCost(0.0, True, ())
    except OSError as exc:
        return PiCost(0.0, False, (f"sessions dir: {exc}",))
    problems: list[str] = []

    def onerror(exc: OSError) -> None:
        if not isinstance(exc, FileNotFoundError):
            problems.append(f"walk: {exc}")

    total = 0.0
    overflow = False
    cutoff = since.timestamp()
    for dirpath, _dirs, files in os.walk(base_dir, onerror=onerror):
        for name in sorted(files):
            if not name.endswith(".jsonl"):
                continue
            path = Path(dirpath) / name
            try:
                if path.stat().st_mtime < cutoff:
                    continue
            except FileNotFoundError:
                continue
            except OSError as exc:
                problems.append(f"{name}: {exc}")
                continue
            got = _scan_pi_file(path, since)
            problems.extend(f"{name}: {p}" for p in got.problems)
            if got.usd is None:
                overflow = True
            else:
                total += got.usd
    if overflow or not math.isfinite(total):
        if "overflow" not in problems:
            problems.append("overflow")
        return PiCost(None, False, tuple(problems))
    return PiCost(total, not problems, tuple(problems))


def load_claude_records(
    base_dir: Path | None = None,
    since_dt: datetime | None = None,
    until_dt: datetime | None = None,
) -> tuple[list[SessionRecord], list[SkippedRecord]]:
    if base_dir is None:
        base_dir = Path.home() / ".claude" / "projects"
    records: list[SessionRecord] = []
    skipped: list[SkippedRecord] = []
    if not base_dir.exists():
        return records, skipped
    for p in sorted(base_dir.rglob("*.jsonl")):
        session_id = p.stem
        session_cwd: str | None = None
        for item in _iter_jsonl(p, "claude"):
            if isinstance(item, SkippedRecord):
                skipped.append(item)
                continue
            obj = item

            if "sessionId" in obj:
                session_id = str(obj["sessionId"])
            if "cwd" in obj and session_cwd is None:
                session_cwd = str(obj["cwd"])

            t = obj.get("type")
            if t not in ("user", "assistant"):
                continue

            is_sidechain = bool(obj.get("isSidechain", False))
            ts_str, dt = normalize_timestamp(obj.get("timestamp"))
            if since_dt and dt and dt < since_dt:
                continue
            if until_dt and dt and dt > until_dt:
                continue

            role = t
            msg = obj.get("message", {})
            if isinstance(msg, dict) and msg.get("role"):
                role = msg.get("role")

            text = _content_text(
                msg.get("content") if isinstance(msg, dict) else obj.get("content")
            )

            model: str | None = None
            in_tok, out_tok, cr_tok, cw_tok = 0, 0, 0, 0
            cost_usd: float | None = None
            cost_origin = "unavailable"

            if isinstance(msg, dict):
                model = msg.get("model")
                usage = msg.get("usage", {})
                if isinstance(usage, dict):
                    in_tok = int(usage.get("input_tokens", 0) or 0)
                    out_tok = int(usage.get("output_tokens", 0) or 0)
                    cr_tok = int(usage.get("cache_read_input_tokens", 0) or 0)
                    cw_tok = int(usage.get("cache_creation_input_tokens", 0) or 0)

            if in_tok or out_tok or cr_tok or cw_tok:
                cost_usd, cost_origin = calculate_claude_cost(
                    model, in_tok, out_tok, cr_tok, cw_tok
                )

            records.append(
                SessionRecord(
                    harness="claude",
                    session_id=session_id,
                    cwd=session_cwd,
                    timestamp=ts_str,
                    role=role,
                    text=text,
                    model=model,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    cache_read_tokens=cr_tok,
                    cache_write_tokens=cw_tok,
                    cost_usd=cost_usd,
                    cost_origin=cost_origin,
                    is_subagent=is_sidechain,
                )
            )
    return records, skipped


def load_opencode_records(
    db_path: Path | None = None,
    since_dt: datetime | None = None,
    until_dt: datetime | None = None,
    cwd_filter: str | None = None,
    session_filter: str | None = None,
) -> tuple[list[SessionRecord], list[SkippedRecord]]:
    if db_path is None:
        db_path = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
    records: list[SessionRecord] = []
    skipped: list[SkippedRecord] = []
    if not db_path.exists():
        return records, skipped

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        try:
            conn = sqlite3.connect(str(db_path))
        except sqlite3.Error as exc:
            skipped.append(
                SkippedRecord(
                    path=db_path,
                    harness="opencode",
                    kind="db",
                    line_number=None,
                    reason=f"corrupt db: {exc}",
                )
            )
            return records, skipped

    try:
        cursor = conn.cursor()
        where_clauses: list[str] = []
        params: list[object] = []

        if since_dt:
            where_clauses.append("m.time_created >= ?")
            params.append(int(since_dt.timestamp() * 1000))
        if until_dt:
            where_clauses.append("m.time_created <= ?")
            params.append(int(until_dt.timestamp() * 1000))
        if session_filter:
            where_clauses.append("s.id LIKE ?")
            params.append(f"%{session_filter}%")
        if cwd_filter:
            where_clauses.append("s.directory LIKE ?")
            params.append(f"%{cwd_filter}%")

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        query = f"""
            SELECT s.id, s.parent_id, s.directory, m.id, m.time_created, m.data
            FROM message m
            JOIN session s ON m.session_id = s.id
            {where_sql}
            ORDER BY m.time_created ASC
        """
        cursor.execute(query, params)
        rows = cursor.fetchall()

        if not rows:
            return records, skipped

        msg_ids = [r[3] for r in rows]
        part_texts: dict[str, list[str]] = {}
        for i in range(0, len(msg_ids), 500):
            chunk = msg_ids[i : i + 500]
            placeholders = ",".join(["?"] * len(chunk))
            cursor.execute(
                f"SELECT message_id, data FROM part WHERE message_id IN ({placeholders}) ORDER BY time_created ASC",
                chunk,
            )
            for m_id, p_data in cursor.fetchall():
                try:
                    p_obj = json.loads(p_data)
                    if p_obj.get("type") == "text" and "text" in p_obj:
                        part_texts.setdefault(m_id, []).append(str(p_obj["text"]))
                except json.JSONDecodeError:
                    continue

        for s_id, s_parent_id, s_dir, m_id, m_time, m_data in rows:
            is_subagent = bool(s_parent_id and str(s_parent_id).strip())
            ts_str, _ = normalize_timestamp(m_time)

            try:
                m_obj = json.loads(m_data)
            except json.JSONDecodeError:
                m_obj = {}

            role = m_obj.get("role", "user")
            model_id = m_obj.get("modelID")
            if not model_id and isinstance(m_obj.get("model"), dict):
                model_id = m_obj["model"].get("modelID")

            tokens_obj = m_obj.get("tokens", {})
            in_tok = int(tokens_obj.get("input", 0) or 0)
            out_tok = int(tokens_obj.get("output", 0) or 0)
            cache_obj = (
                tokens_obj.get("cache", {}) if isinstance(tokens_obj, dict) else {}
            )
            cr_tok = int(cache_obj.get("read", 0) or 0)
            cw_tok = int(cache_obj.get("write", 0) or 0)

            cost = m_obj.get("cost")
            cost_usd: float | None = float(cost) if cost is not None else None
            cost_origin = "native" if cost_usd is not None else "unavailable"

            texts = part_texts.get(m_id, [])
            text = "\n".join(texts).strip()

            records.append(
                SessionRecord(
                    harness="opencode",
                    session_id=s_id,
                    cwd=s_dir,
                    timestamp=ts_str,
                    role=role,
                    text=text,
                    model=model_id,
                    input_tokens=in_tok,
                    output_tokens=out_tok,
                    cache_read_tokens=cr_tok,
                    cache_write_tokens=cw_tok,
                    cost_usd=cost_usd,
                    cost_origin=cost_origin,
                    is_subagent=is_subagent,
                )
            )
    except sqlite3.Error as exc:
        skipped.append(
            SkippedRecord(
                path=db_path,
                harness="opencode",
                kind="db",
                line_number=None,
                reason=f"corrupt db: {exc}",
            )
        )
    finally:
        conn.close()
    return records, skipped


def load_copilot_records(
    db_path: Path | None = None,
    since_dt: datetime | None = None,
    until_dt: datetime | None = None,
    cwd_filter: str | None = None,
    session_filter: str | None = None,
) -> tuple[list[SessionRecord], list[SkippedRecord]]:
    if db_path is None:
        db_path = Path.home() / ".copilot" / "session-store.db"
    records: list[SessionRecord] = []
    skipped: list[SkippedRecord] = []
    if not db_path.exists():
        return records, skipped

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        try:
            conn = sqlite3.connect(str(db_path))
        except sqlite3.Error as exc:
            skipped.append(
                SkippedRecord(
                    path=db_path,
                    harness="copilot",
                    kind="db",
                    line_number=None,
                    reason=f"corrupt db: {exc}",
                )
            )
            return records, skipped

    try:
        cursor = conn.cursor()
        where_clauses: list[str] = []
        params: list[object] = []

        if since_dt:
            where_clauses.append("t.timestamp >= ?")
            params.append(since_dt.strftime("%Y-%m-%d %H:%M:%S"))
        if until_dt:
            where_clauses.append("t.timestamp <= ?")
            params.append(until_dt.strftime("%Y-%m-%d %H:%M:%S"))
        if session_filter:
            where_clauses.append("s.id LIKE ?")
            params.append(f"%{session_filter}%")
        if cwd_filter:
            where_clauses.append("s.cwd LIKE ?")
            params.append(f"%{cwd_filter}%")

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

        query = f"""
            SELECT s.id, s.cwd, t.turn_index, t.user_message, t.assistant_response, t.timestamp
            FROM turns t
            JOIN sessions s ON t.session_id = s.id
            {where_sql}
            ORDER BY t.session_id, t.turn_index ASC
        """
        cursor.execute(query, params)
        turn_rows = cursor.fetchall()

        usage_query = """
            SELECT session_id, turn_index, model,
                   SUM(COALESCE(input_tokens, 0)),
                   SUM(COALESCE(output_tokens, 0)),
                   SUM(COALESCE(cache_read_tokens, 0)),
                   SUM(COALESCE(cache_write_tokens, 0))
            FROM assistant_usage_events
            GROUP BY session_id, turn_index
        """
        try:
            cursor.execute(usage_query)
            usage_map: dict[tuple[str, int], tuple[str | None, int, int, int, int]] = {}
            for row in cursor.fetchall():
                sess_id, turn_idx, model, in_tok, out_tok, cr_tok, cw_tok = row
                t_idx = int(turn_idx) if turn_idx is not None else 0
                usage_map[(sess_id, t_idx)] = (
                    model,
                    int(in_tok),
                    int(out_tok),
                    int(cr_tok),
                    int(cw_tok),
                )
        except sqlite3.Error:
            usage_map = {}

        for sess_id, cwd, turn_idx, u_msg, a_resp, ts in turn_rows:
            ts_str, _ = normalize_timestamp(ts)
            t_idx = int(turn_idx) if turn_idx is not None else 0

            if u_msg:
                records.append(
                    SessionRecord(
                        harness="copilot",
                        session_id=sess_id,
                        cwd=cwd,
                        timestamp=ts_str,
                        role="user",
                        text=u_msg or "",
                        model=None,
                        input_tokens=0,
                        output_tokens=0,
                        cache_read_tokens=0,
                        cache_write_tokens=0,
                        cost_usd=None,
                        cost_origin="unavailable",
                        is_subagent=False,
                    )
                )

            u_info = usage_map.get((sess_id, t_idx), (None, 0, 0, 0, 0))
            model, in_tok, out_tok, cr_tok, cw_tok = u_info
            if a_resp or in_tok or out_tok:
                cost_usd: float | None = None
                cost_origin = "unavailable"
                if model and (in_tok or out_tok or cr_tok or cw_tok):
                    cost_usd, cost_origin = calculate_claude_cost(
                        model, in_tok, out_tok, cr_tok, cw_tok
                    )
                    if cost_origin == "unavailable":
                        cost_usd, cost_origin = calculate_openai_cost(
                            model, in_tok, out_tok, cr_tok, cw_tok
                        )

                records.append(
                    SessionRecord(
                        harness="copilot",
                        session_id=sess_id,
                        cwd=cwd,
                        timestamp=ts_str,
                        role="assistant",
                        text=a_resp or "",
                        model=model,
                        input_tokens=in_tok,
                        output_tokens=out_tok,
                        cache_read_tokens=cr_tok,
                        cache_write_tokens=cw_tok,
                        cost_usd=cost_usd,
                        cost_origin=cost_origin,
                        is_subagent=False,
                    )
                )
    except sqlite3.Error as exc:
        skipped.append(
            SkippedRecord(
                path=db_path,
                harness="copilot",
                kind="db",
                line_number=None,
                reason=f"corrupt db: {exc}",
            )
        )
    finally:
        conn.close()
    return records, skipped


def load_agy_records(
    base_dir: Path | None = None,
    since_dt: datetime | None = None,
    until_dt: datetime | None = None,
    session_filter: str | None = None,
) -> tuple[list[SessionRecord], list[SkippedRecord]]:
    if base_dir is None:
        base_dir = Path.home() / ".gemini" / "antigravity-cli" / "brain"
    records: list[SessionRecord] = []
    skipped: list[SkippedRecord] = []
    if not base_dir.exists():
        return records, skipped

    for sess_dir in sorted(p for p in base_dir.iterdir() if p.is_dir()):
        session_id = sess_dir.name
        if session_filter and session_filter not in session_id:
            continue

        log_dir = sess_dir / ".system_generated" / "logs"
        transcript_file = log_dir / "transcript_full.jsonl"
        if not transcript_file.exists():
            transcript_file = log_dir / "transcript.jsonl"
        if not transcript_file.exists():
            continue

        for item in _iter_jsonl(transcript_file, "agy"):
            if isinstance(item, SkippedRecord):
                skipped.append(item)
                continue
            obj = item

            t = obj.get("type")
            if t == "USER_INPUT":
                role = "user"
            elif t == "PLANNER_RESPONSE":
                role = "assistant"
            else:
                continue

            content = obj.get("content") or ""
            ts_str, dt = normalize_timestamp(obj.get("created_at"))
            if since_dt and dt and dt < since_dt:
                continue
            if until_dt and dt and dt > until_dt:
                continue

            records.append(
                SessionRecord(
                    harness="agy",
                    session_id=session_id,
                    cwd=None,
                    timestamp=ts_str,
                    role=role,
                    text=content if isinstance(content, str) else str(content),
                    model=None,
                    input_tokens=0,
                    output_tokens=0,
                    cache_read_tokens=0,
                    cache_write_tokens=0,
                    cost_usd=None,
                    cost_origin="unavailable",
                    is_subagent=False,
                )
            )
    return records, skipped


def load_codex_records(
    base_dir: Path | None = None,
    since_dt: datetime | None = None,
    until_dt: datetime | None = None,
    session_filter: str | None = None,
    cwd_filter: str | None = None,
) -> tuple[list[SessionRecord], list[SkippedRecord]]:
    if base_dir is None:
        base_dir = Path.home() / ".codex" / "sessions"
    records: list[SessionRecord] = []
    skipped: list[SkippedRecord] = []
    if not base_dir.exists():
        return records, skipped

    search_dir = base_dir / "sessions" if (base_dir / "sessions").is_dir() else base_dir
    for p in sorted(search_dir.rglob("*.jsonl")):
        if p.name in ("session_index.jsonl", "history.jsonl"):
            continue
        is_subagent_path = "subagent" in str(p).lower()
        session_id = p.stem
        session_cwd: str | None = None

        items: list[dict[str, object]] = []
        for item in _iter_jsonl(p, "codex"):
            if isinstance(item, SkippedRecord):
                skipped.append(item)
            else:
                items.append(item)

        for obj in items:
            if obj.get("type") == "session_meta":
                payload = obj.get("payload") or {}
                if isinstance(payload, dict):
                    session_id = str(
                        payload.get("id") or payload.get("session_id") or session_id
                    )
                    if payload.get("cwd"):
                        session_cwd = str(payload["cwd"])
                    if (
                        payload.get("is_subagent")
                        or payload.get("source") == "subagent"
                        or payload.get("originator") == "subagent"
                    ):
                        is_subagent_path = True
                break

        if (
            session_filter
            and session_filter not in session_id
            and session_filter not in p.name
        ):
            continue

        turn_usage: dict[str, dict[str, int]] = {}
        for obj in items:
            if obj.get("type") == "token_usage_record":
                payload = obj.get("payload") or {}
                if isinstance(payload, dict):
                    tid = payload.get("turn_id")
                    usage = payload.get("usage") or {}
                    if tid and isinstance(usage, dict):
                        in_tok = int(usage.get("input_tokens", 0) or 0)
                        cr_tok = int(usage.get("cached_input_tokens", 0) or 0)
                        cw_tok = int(usage.get("cache_write_input_tokens", 0) or 0)
                        out_tok = int(usage.get("output_tokens", 0) or 0)
                        net_in = max(0, in_tok - cr_tok)
                        if tid not in turn_usage:
                            turn_usage[tid] = {
                                "input": 0,
                                "output": 0,
                                "cache_read": 0,
                                "cache_write": 0,
                            }
                        turn_usage[tid]["input"] += net_in
                        turn_usage[tid]["output"] += out_tok
                        turn_usage[tid]["cache_read"] += cr_tok
                        turn_usage[tid]["cache_write"] += cw_tok

        last_assistant_idx: dict[str, int] = {}
        for i, obj in enumerate(items):
            if obj.get("type") == "response_item":
                payload = obj.get("payload") or {}
                if (
                    isinstance(payload, dict)
                    and payload.get("type") == "message"
                    and payload.get("role") == "assistant"
                ):
                    passthrough = payload.get(
                        "internal_chat_message_metadata_passthrough"
                    )
                    tid = (
                        passthrough.get("turn_id")
                        if isinstance(passthrough, dict)
                        else None
                    )
                    if tid:
                        last_assistant_idx[tid] = i

        current_cwd = session_cwd
        current_model: str | None = None
        last_ts_str = ""

        for i, obj in enumerate(items):
            t = obj.get("type")
            payload = obj.get("payload") or {}
            if not isinstance(payload, dict):
                continue

            if t == "turn_context":
                if payload.get("cwd"):
                    current_cwd = str(payload["cwd"])
                if payload.get("model"):
                    current_model = str(payload["model"])
                continue

            if t == "event_msg" and payload.get("type") == "thread_settings_applied":
                ts = payload.get("thread_settings") or {}
                if isinstance(ts, dict) and ts.get("model"):
                    current_model = str(ts["model"])
                continue

            if t == "response_item" and payload.get("type") == "message":
                role = payload.get("role")
                if role not in ("user", "assistant"):
                    continue

                ts_val = obj.get("timestamp") or payload.get("timestamp")
                ts_str, dt = normalize_timestamp(ts_val)
                if ts_str:
                    last_ts_str = ts_str
                if since_dt and dt and dt < since_dt:
                    continue
                if until_dt and dt and dt > until_dt:
                    continue
                if cwd_filter and current_cwd and cwd_filter not in current_cwd:
                    continue

                content = payload.get("content")
                text = _content_text(content)

                in_tok, out_tok, cr_tok, cw_tok = 0, 0, 0, 0
                cost_usd: float | None = None
                cost_origin = "unavailable"
                rec_model = current_model

                if role == "assistant":
                    inline_usage = payload.get("usage") or obj.get("usage")
                    if isinstance(inline_usage, dict):
                        raw_in = int(
                            inline_usage.get("input_tokens", 0)
                            or inline_usage.get("input", 0)
                            or 0
                        )
                        cr_tok = int(
                            inline_usage.get("cached_input_tokens", 0)
                            or inline_usage.get("cache_read_tokens", 0)
                            or inline_usage.get("cacheRead", 0)
                            or 0
                        )
                        cw_tok = int(
                            inline_usage.get("cache_write_input_tokens", 0)
                            or inline_usage.get("cache_write_tokens", 0)
                            or inline_usage.get("cacheWrite", 0)
                            or 0
                        )
                        out_tok = int(
                            inline_usage.get("output_tokens", 0)
                            or inline_usage.get("output", 0)
                            or 0
                        )
                        in_tok = max(0, raw_in - cr_tok)
                        cost_val = inline_usage.get("cost") or payload.get("cost")
                        if cost_val is not None:
                            try:
                                cost_usd = float(cost_val)
                                cost_origin = "native"
                            except (ValueError, TypeError):
                                pass
                    else:
                        passthrough = payload.get(
                            "internal_chat_message_metadata_passthrough"
                        )
                        tid = (
                            passthrough.get("turn_id")
                            if isinstance(passthrough, dict)
                            else None
                        )
                        if (
                            tid
                            and last_assistant_idx.get(tid) == i
                            and tid in turn_usage
                        ):
                            u = turn_usage.pop(tid)
                            in_tok = u["input"]
                            out_tok = u["output"]
                            cr_tok = u["cache_read"]
                            cw_tok = u["cache_write"]

                    if cost_usd is None and (in_tok or out_tok or cr_tok or cw_tok):
                        cost_usd, cost_origin = calculate_openai_cost(
                            rec_model, in_tok, out_tok, cr_tok, cw_tok
                        )

                records.append(
                    SessionRecord(
                        harness="codex",
                        session_id=session_id,
                        cwd=current_cwd,
                        timestamp=ts_str,
                        role=role,
                        text=text,
                        model=rec_model,
                        input_tokens=in_tok,
                        output_tokens=out_tok,
                        cache_read_tokens=cr_tok,
                        cache_write_tokens=cw_tok,
                        cost_usd=cost_usd,
                        cost_origin=cost_origin,
                        is_subagent=is_subagent_path,
                    )
                )

        for _tid, u in turn_usage.items():
            cost_usd, cost_origin = calculate_openai_cost(
                current_model,
                u["input"],
                u["output"],
                u["cache_read"],
                u["cache_write"],
            )
            records.append(
                SessionRecord(
                    harness="codex",
                    session_id=session_id,
                    cwd=current_cwd,
                    timestamp=last_ts_str,
                    role="assistant",
                    text="",
                    model=current_model,
                    input_tokens=u["input"],
                    output_tokens=u["output"],
                    cache_read_tokens=u["cache_read"],
                    cache_write_tokens=u["cache_write"],
                    cost_usd=cost_usd,
                    cost_origin=cost_origin,
                    is_subagent=is_subagent_path,
                )
            )

    return records, skipped


def load_all_records(
    harness: str = "all",
    since_dt: datetime | None = None,
    until_dt: datetime | None = None,
    cwd_filter: str | None = None,
    model_filter: str | None = None,
    session_filter: str | None = None,
    *,
    include_subagents: bool = True,
    pi_dir: Path | None = None,
    claude_dir: Path | None = None,
    opencode_db: Path | None = None,
    copilot_db: Path | None = None,
    agy_dir: Path | None = None,
    codex_dir: Path | None = None,
) -> list[SessionRecord]:
    """Legacy shim: same positional contract and bare-list return as always.

    Delegates to query_sessions(); skip diagnostics are available only via
    query_sessions() directly (this shim's historical return type has no
    place to carry them).
    """

    roots: dict[str, Path | None] = {
        "pi": pi_dir,
        "claude": claude_dir,
        "opencode": opencode_db,
        "copilot": copilot_db,
        "agy": agy_dir,
        "codex": codex_dir,
    }
    query = SessionQuery(
        harnesses=None if harness == "all" else frozenset({harness}),
        since=since_dt,
        until=until_dt,
        cwd=cwd_filter,
        model=model_filter,
        session_id=session_filter,
        include_subagents=include_subagents,
    )
    return query_sessions(query, roots=roots).records


# ── Subcommand Handlers ───────────────────────────────────────────────────


# ── launch-prompt attribution (cost --by item) ───────────────────────────
#
# A worker session's backlog slug is parsed from its launch prompt: the raw
# `/backlog-item --auto <slug> [--handoff]` invocation a worker launch sends
# (or the `$backlog-item` form on codex), or the expanded skill prompt pi /
# Claude Code record in its place (``The invocation (or `<args>`)`` anchor,
# ``slug: "<args>"`` fallback). Template doc mentions of `/backlog-item
# <slug>` are placeholders and never parse as a target.

_RAW_INVOCATION = re.compile(r"^`?(?:/|\$)backlog-item\b(.*)$", re.DOTALL)
_RAW_LINE = re.compile(r"^(?:/|\$)backlog-item\b(.*)$", re.MULTILINE)
_EXPANDED_ANCHOR = re.compile(r"The invocation \(or\s*`([^`]*)`", re.DOTALL)
_SLUG_FALLBACK = re.compile(r'slug: "([^"]*)"')


def _invocation_target(invocation: str) -> str | None:
    """The item slug or position named by a ``/backlog-item`` invocation.

    Follows the documented grammar: boolean flags first, then the target; a
    ``resume <runId>`` tail is stepped over; queue invocations
    (``--swarm``/``--serial``) name no item; unresolved ``<...>``
    placeholders are rejected. Returns None when no target is named.
    """
    tokens = invocation.split()
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("--auto", "--handoff"):
            i += 1
            continue
        if tok == "--swarm" or tok.startswith("--swarm=") or tok == "--serial":
            return None
        if tok == "resume":
            i += 2
            continue
        if tok.startswith("-"):
            return None
        if "<" in tok or ">" in tok:
            return None
        return tok
    return None


def launch_slug_from_prompt(text: str) -> str | None:
    """The backlog slug named by a session's launch prompt, else None.

    Recognizes the raw invocation starting the message (herdr worker
    prompts, codex's `$backlog-item` form), the expanded pi-template anchor
    (``The invocation (or `<args>`)``), a ``slug: "<args>"`` instruction
    fallback, and — last — a raw invocation on its own line, for harnesses
    that log the typed line alongside an expansion that never embeds it.
    Placeholder tokens (``<slug>`` and friends) never parse as a target, so
    the templates' own backticked doc examples are inert.
    """
    raw = _RAW_INVOCATION.match(text.lstrip())
    if raw:
        return _invocation_target(raw.group(1))
    anchor = _EXPANDED_ANCHOR.search(text)
    if anchor:
        target = _invocation_target(anchor.group(1))
        if target:
            return target
    for m in _SLUG_FALLBACK.finditer(text):
        target = _invocation_target(m.group(1))
        if target:
            return target
    for m in _RAW_LINE.finditer(text):
        target = _invocation_target(m.group(1))
        if target:
            return target
    return None


_ATTRIBUTION_DEFAULT_ROOTS: dict[str, tuple[str, ...]] = {
    "pi": (".pi", "agent", "sessions"),
    "claude": (".claude", "projects"),
    "opencode": (".local", "share", "opencode", "opencode.db"),
    "copilot": (".copilot", "session-store.db"),
    "agy": (".gemini", "antigravity-cli", "brain"),
    "codex": (".codex", "sessions"),
}


def load_launch_slugs(
    roots: Mapping[str, Path | None] | None = None,
) -> dict[tuple[str, str], str]:
    """Map ``(harness, session_id)`` to the slug named by its launch prompt.

    Derived from each session's FIRST user message before any date
    filtering, so ``cost --by item`` attribution survives the same filters
    that drop old records. Sessions without a recognizable launch prompt
    (including subagent/child sessions) are simply absent and group as
    ``unattributed`` at the call site. Scans only the harnesses named in
    ``roots`` (all of them when it is None). Best-effort: a store that
    cannot be read contributes nothing.
    """
    out: dict[tuple[str, str], str] = {}

    def _record(harness: str, session_id: str, text: object) -> None:
        if isinstance(text, str):
            slug = launch_slug_from_prompt(text)
            if slug:
                out.setdefault((harness, session_id), slug)

    def _root(harness: str) -> Path:
        if roots is not None and roots.get(harness) is not None:
            return roots[harness]  # type: ignore[return-value]
        return Path.home().joinpath(*_ATTRIBUTION_DEFAULT_ROOTS[harness])

    wanted = tuple(roots) if roots is not None else tuple(_ATTRIBUTION_DEFAULT_ROOTS)

    if "pi" in wanted and _root("pi").exists():
        for p in sorted(_root("pi").rglob("*.jsonl")):
            sid = p.stem
            for item in _iter_jsonl(p, "pi"):
                if isinstance(item, SkippedRecord):
                    continue
                if item.get("type") == "session":
                    sid = str(item.get("id") or sid)
                elif item.get("type") == "message":
                    msg = item.get("message") or {}
                    if msg.get("role") == "user":
                        _record("pi", sid, _content_text(msg.get("content")))
                        break

    if "claude" in wanted and _root("claude").exists():
        for p in sorted(_root("claude").rglob("*.jsonl")):
            sid = p.stem
            for item in _iter_jsonl(p, "claude"):
                if isinstance(item, SkippedRecord):
                    continue
                if "sessionId" in item:
                    sid = str(item["sessionId"])
                if item.get("type") == "user" and not item.get("isSidechain"):
                    msg = item.get("message")
                    c = (
                        msg.get("content")
                        if isinstance(msg, dict)
                        else item.get("content")
                    )
                    _record("claude", sid, _content_text(c))
                    break

    if "opencode" in wanted:
        conn = _open_sqlite_ro(_root("opencode"))
        if conn is not None:
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT m.session_id, p.data FROM part p "
                    "JOIN message m ON p.message_id = m.id "
                    "JOIN session s ON m.session_id = s.id "
                    "WHERE json_extract(m.data, '$.role') = 'user' "
                    "ORDER BY m.time_created ASC, p.time_created ASC"
                )
                for sess_id, p_data in cur.fetchall():
                    if ("opencode", sess_id) not in out:
                        with contextlib.suppress(json.JSONDecodeError):
                            _record(
                                "opencode",
                                sess_id,
                                _content_text([json.loads(p_data)]),
                            )
            except sqlite3.Error:
                pass
            finally:
                conn.close()

    if "copilot" in wanted:
        conn = _open_sqlite_ro(_root("copilot"))
        if conn is not None:
            try:
                cur = conn.cursor()
                cur.execute(
                    "SELECT t.session_id, t.user_message FROM turns t "
                    "JOIN sessions s ON t.session_id = s.id "
                    "ORDER BY t.session_id, t.turn_index ASC"
                )
                for sess_id, user_message in cur.fetchall():
                    _record("copilot", sess_id, user_message)
            except sqlite3.Error:
                pass
            finally:
                conn.close()

    if "agy" in wanted and _root("agy").exists():
        for sess_dir in sorted(p for p in _root("agy").iterdir() if p.is_dir()):
            log_dir = sess_dir / ".system_generated" / "logs"
            for tname in ("transcript_full.jsonl", "transcript.jsonl"):
                tp = log_dir / tname
                if tp.exists():
                    for item in _iter_jsonl(tp, "agy"):
                        if (
                            not isinstance(item, SkippedRecord)
                            and item.get("type") == "USER_INPUT"
                        ):
                            _record("agy", sess_dir.name, item.get("content"))
                            break
                    break

    if "codex" in wanted and _root("codex").exists():
        base = _root("codex")
        sdir = base / "sessions" if (base / "sessions").is_dir() else base
        for p in sorted(sdir.rglob("*.jsonl")):
            if p.name in ("session_index.jsonl", "history.jsonl"):
                continue
            sid = p.stem
            for item in _iter_jsonl(p, "codex"):
                if isinstance(item, SkippedRecord):
                    continue
                pl = item.get("payload") or {}
                if isinstance(pl, dict):
                    if item.get("type") == "session_meta":
                        sid = str(pl.get("id") or pl.get("session_id") or sid)
                    elif (
                        item.get("type") == "response_item"
                        and pl.get("type") == "message"
                        and pl.get("role") == "user"
                    ):
                        _record("codex", sid, _content_text(pl.get("content")))
                        break

    return out


def _open_sqlite_ro(db_path: Path) -> sqlite3.Connection | None:
    """Open a sqlite store read-only, falling back to a plain open; else None."""
    if not db_path.exists():
        return None
    try:
        return sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        try:
            return sqlite3.connect(str(db_path))
        except sqlite3.Error:
            return None


# ── milestones (time and spend to the first file write) ──────────────────
#
# First-write detection needs an explicit write/edit tool call carrying a
# file path — the only non-heuristic write signal session logs offer. It is
# measured where the logs have it: pi (toolCall write/edit +
# arguments.path), claude (tool_use Write/Edit/MultiEdit/NotebookEdit +
# input.file_path), opencode (tool part edit/write/patch +
# state.input.filePath). codex, copilot and agy logs carry no such call
# (writes hide in exec shell text or prose), so those harnesses are
# reported as unsupported — never as a fabricated zero.

_MILESTONE_WRITE_TOOLS: dict[str, frozenset[str]] = {
    "pi": frozenset({"write", "edit"}),
    "claude": frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"}),
    "opencode": frozenset({"edit", "write", "patch"}),
}

MILESTONE_HARNESSES: tuple[str, ...] = ("pi", "claude", "opencode")

UNSUPPORTED_MILESTONE_HARNESSES: dict[str, str] = {
    "codex": "no explicit write tool call in codex logs (writes hide in exec shell text)",
    "copilot": "no explicit write tool call in copilot's session store (prose turns)",
    "agy": "no explicit write tool call in agy transcripts (prose renderings)",
}


@dataclass(frozen=True)
class SessionMilestone:
    """Time and spend from session start to the first in-repo file write."""

    harness: str
    session_id: str
    cwd: str | None
    model: str | None
    # Starting model: the earliest model-bearing record's model, else None
    # (rendered as "unknown"). A later model change never redefines it.
    start: datetime | None
    minutes_to_first_write: float | None
    spend_to_first_write: float | None
    first_write_cost_origin: str
    # "native" / "derived" / "partial" (amount is a floor) / "unavailable".
    first_write_path: str | None
    total_minutes: float | None
    total_spend: float | None
    total_cost_origin: str
    status: str
    # "ok" / "no-write" / "no-root" (no containment root: unmeasurable).


@dataclass(frozen=True)
class _WriteEvent:
    """One message's contribution to a session's milestone timeline."""

    ts: datetime | None
    charge: bool
    # The message carries tokens or cost (the records a spend sum counts).
    cost: float | None
    write_path: str | None


def _milestone_finish(
    *,
    harness: str,
    session_id: str,
    cwd: str | None,
    model: str | None,
    start: datetime | None,
    events: list[_WriteEvent],
    repo: str | None,
) -> SessionMilestone:
    """Fold one session's events into its milestone row."""
    base_origin = "derived" if harness == "claude" else "native"
    root = repo or cwd

    total_minutes: float | None = None
    if start is not None and events:
        last_ts = next((e.ts for e in reversed(events) if e.ts is not None), None)
        if last_ts is not None:
            total_minutes = (last_ts - start).total_seconds() / 60.0

    def _window(end: _WriteEvent | None) -> tuple[float | None, str]:
        charges = [
            e
            for e in events
            if e.charge and (end is None or (e.ts is not None and e.ts <= end.ts))
        ]
        have = [e.cost for e in charges if e.cost is not None]
        if not have:
            return None, "unavailable"
        return sum(have), ("partial" if len(have) < len(charges) else base_origin)

    first_write: _WriteEvent | None = None
    if root is not None:
        root_path = Path(os.path.normpath(root))
        for ev in events:
            if not ev.write_path:
                continue
            p = Path(ev.write_path)
            if not p.is_absolute():
                if cwd is None:
                    continue
                p = Path(cwd) / p
            if Path(os.path.normpath(str(p))).is_relative_to(root_path):
                first_write = ev
                break

    status = "no-root" if root is None else ("ok" if first_write else "no-write")
    minutes = (
        (first_write.ts - start).total_seconds() / 60.0
        if first_write and start and first_write.ts
        else None
    )
    first_spend, first_origin = (
        _window(first_write) if first_write else (None, "unavailable")
    )
    total_spend, total_origin = _window(None)

    return SessionMilestone(
        harness=harness,
        session_id=session_id,
        cwd=cwd,
        model=model or "unknown",
        start=start,
        minutes_to_first_write=minutes,
        spend_to_first_write=first_spend,
        first_write_cost_origin=first_origin,
        first_write_path=first_write.write_path if first_write else None,
        total_minutes=total_minutes,
        total_spend=total_spend,
        total_cost_origin=total_origin,
        status=status,
    )


def _milestone_rows_pi(
    base_dir: Path, repo: str | None
) -> tuple[list[SessionMilestone], list[SkippedRecord]]:
    rows: list[SessionMilestone] = []
    skipped: list[SkippedRecord] = []
    if not base_dir.exists():
        return rows, skipped
    for p in sorted(base_dir.rglob("*.jsonl")):
        if "subagent" in str(p).lower():
            continue
        session_id = p.stem
        session_cwd: str | None = None
        model: str | None = None
        start: datetime | None = None
        events: list[_WriteEvent] = []
        for item in _iter_jsonl(p, "pi"):
            if isinstance(item, SkippedRecord):
                skipped.append(item)
                continue
            obj = item
            t = obj.get("type")
            ts_val = obj.get("timestamp")
            msg: dict[str, object] = {}
            if t == "message":
                raw_msg = obj.get("message") or {}
                msg = raw_msg if isinstance(raw_msg, dict) else {}
                ts_val = ts_val or msg.get("timestamp")
            _, dt = normalize_timestamp(ts_val)
            if start is None and dt is not None:
                start = dt
            if t == "session":
                session_id = str(obj.get("id") or session_id)
                if obj.get("cwd"):
                    session_cwd = str(obj["cwd"])
                continue
            if t == "model_change":
                if model is None and obj.get("modelId"):
                    model = str(obj["modelId"])
                continue
            if t != "message" or msg.get("role") != "assistant":
                continue
            if model is None:
                m_model = obj.get("model") or msg.get("model")
                if m_model:
                    model = str(m_model)
            usage = obj.get("usage") or msg.get("usage") or {}
            cost = _pi_usage_cost(usage) if isinstance(usage, dict) else None
            charge = bool(
                cost is not None
                or isinstance(usage, dict)
                and any(
                    usage.get(k) for k in ("input", "output", "cacheRead", "cacheWrite")
                )
            )
            write_path: str | None = None
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if (
                        isinstance(block, dict)
                        and block.get("type") == "toolCall"
                        and block.get("name") in _MILESTONE_WRITE_TOOLS["pi"]
                    ):
                        args = block.get("arguments") or {}
                        if isinstance(args, dict) and args.get("path"):
                            write_path = str(args["path"])
                            break
            events.append(_WriteEvent(dt, charge, cost, write_path))
        rows.append(
            _milestone_finish(
                harness="pi",
                session_id=session_id,
                cwd=session_cwd,
                model=model,
                start=start,
                events=events,
                repo=repo,
            )
        )
    return rows, skipped


def _milestone_rows_claude(
    base_dir: Path, repo: str | None
) -> tuple[list[SessionMilestone], list[SkippedRecord]]:
    rows: list[SessionMilestone] = []
    skipped: list[SkippedRecord] = []
    if not base_dir.exists():
        return rows, skipped
    for p in sorted(base_dir.rglob("*.jsonl")):
        session_id = p.stem
        session_cwd: str | None = None
        model: str | None = None
        start: datetime | None = None
        events: list[_WriteEvent] = []
        for item in _iter_jsonl(p, "claude"):
            if isinstance(item, SkippedRecord):
                skipped.append(item)
                continue
            obj = item
            if "sessionId" in obj:
                session_id = str(obj["sessionId"])
            if "cwd" in obj and session_cwd is None:
                session_cwd = str(obj["cwd"])
            _, dt = normalize_timestamp(obj.get("timestamp"))
            if start is None and dt is not None:
                start = dt
            t = obj.get("type")
            if t not in ("user", "assistant") or obj.get("isSidechain"):
                continue
            msg = obj.get("message", {})
            msg = msg if isinstance(msg, dict) else {}
            if t != "assistant":
                continue
            if model is None and msg.get("model"):
                model = str(msg["model"])
            usage = msg.get("usage", {})
            usage = usage if isinstance(usage, dict) else {}
            in_tok = int(usage.get("input_tokens", 0) or 0)
            out_tok = int(usage.get("output_tokens", 0) or 0)
            cr_tok = int(usage.get("cache_read_input_tokens", 0) or 0)
            cw_tok = int(usage.get("cache_creation_input_tokens", 0) or 0)
            cost: float | None = None
            charge = bool(in_tok or out_tok or cr_tok or cw_tok)
            if charge:
                cost, _origin = calculate_claude_cost(
                    msg.get("model"), in_tok, out_tok, cr_tok, cw_tok
                )
            write_path: str | None = None
            content = msg.get("content")
            if isinstance(content, list):
                for block in content:
                    if (
                        isinstance(block, dict)
                        and block.get("type") == "tool_use"
                        and block.get("name") in _MILESTONE_WRITE_TOOLS["claude"]
                    ):
                        inp = block.get("input") or {}
                        if isinstance(inp, dict) and inp.get("file_path"):
                            write_path = str(inp["file_path"])
                            break
            events.append(_WriteEvent(dt, charge, cost, write_path))
        rows.append(
            _milestone_finish(
                harness="claude",
                session_id=session_id,
                cwd=session_cwd,
                model=model,
                start=start,
                events=events,
                repo=repo,
            )
        )
    return rows, skipped


def _milestone_rows_opencode(
    base_dir: Path, repo: str | None
) -> tuple[list[SessionMilestone], list[SkippedRecord]]:
    rows: list[SessionMilestone] = []
    skipped: list[SkippedRecord] = []
    conn = _open_sqlite_ro(base_dir)
    if conn is None:
        return rows, skipped
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, parent_id, directory, time_created FROM session "
            "ORDER BY time_created ASC"
        )
        sessions = {
            sid: (parent, directory, created)
            for sid, parent, directory, created in cur.fetchall()
            if not parent
        }
        cur.execute("SELECT message_id, data FROM part ORDER BY time_created ASC")
        write_paths: dict[str, str] = {}
        for m_id, p_data in cur.fetchall():
            if m_id in write_paths:
                continue
            try:
                part = json.loads(p_data)
            except json.JSONDecodeError:
                continue
            if (
                isinstance(part, dict)
                and part.get("type") == "tool"
                and part.get("tool") in _MILESTONE_WRITE_TOOLS["opencode"]
            ):
                state = part.get("state") or {}
                inp = state.get("input") if isinstance(state, dict) else {}
                if isinstance(inp, dict) and inp.get("filePath"):
                    write_paths[m_id] = str(inp["filePath"])

        cur.execute(
            "SELECT id, session_id, time_created, data FROM message "
            "ORDER BY time_created ASC"
        )
        events_by_sess: dict[str, list[_WriteEvent]] = {}
        model_by_sess: dict[str, str] = {}
        for m_id, sess_id, ts_ms, m_data in cur.fetchall():
            if sess_id not in sessions:
                continue
            try:
                data = json.loads(m_data)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict) or data.get("role") != "assistant":
                continue
            _, dt = normalize_timestamp(ts_ms)
            if sess_id not in model_by_sess and data.get("modelID"):
                model_by_sess[sess_id] = str(data["modelID"])
            raw_cost = data.get("cost")
            cost = (
                float(raw_cost)
                if isinstance(raw_cost, (int, float)) and not isinstance(raw_cost, bool)
                else None
            )
            tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else {}
            cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
            charge = bool(
                cost is not None
                or any(
                    tokens.get(k) or cache.get(k2)
                    for k, k2 in (("input", "read"), ("output", "write"))
                )
            )
            events_by_sess.setdefault(sess_id, []).append(
                _WriteEvent(dt, charge, cost, write_paths.get(m_id))
            )
        for sess_id, (_, directory, created) in sessions.items():
            _, start_dt = normalize_timestamp(created)
            rows.append(
                _milestone_finish(
                    harness="opencode",
                    session_id=sess_id,
                    cwd=str(directory) if directory else None,
                    model=model_by_sess.get(sess_id),
                    start=start_dt,
                    events=events_by_sess.get(sess_id, []),
                    repo=repo,
                )
            )
    except sqlite3.Error as exc:
        skipped.append(
            SkippedRecord(
                path=base_dir,
                harness="opencode",
                kind="db",
                reason=f"corrupt db: {exc}",
            )
        )
    finally:
        conn.close()
    return rows, skipped


_MILESTONE_SCANNERS = {
    "pi": _milestone_rows_pi,
    "claude": _milestone_rows_claude,
    "opencode": _milestone_rows_opencode,
}

_MILESTONE_DEFAULT_ROOTS: dict[str, tuple[str, ...]] = {
    "pi": (".pi", "agent", "sessions"),
    "claude": (".claude", "projects"),
    "opencode": (".local", "share", "opencode", "opencode.db"),
}


def session_milestones(
    harness: str,
    base_dir: Path | None = None,
    *,
    repo: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    model: str | None = None,
    session: str | None = None,
    cwd: str | None = None,
) -> tuple[list[SessionMilestone], list[SkippedRecord]]:
    """Per-session first-file-write milestones for one measurable harness.

    Whole-session scans: filters select or drop a session as a unit (since/
    until by session START time, model by the session's starting model), so
    a filter can never turn a later write into "first write" or undercount
    spend. ``repo`` is the containment root for writes (default: each
    session's own cwd; neither root means the session is unmeasurable).
    """
    scanner = _MILESTONE_SCANNERS.get(harness)
    if scanner is None:
        raise ValueError(f"harness does not support milestones: {harness!r}")
    if base_dir is None:
        base_dir = Path.home().joinpath(*_MILESTONE_DEFAULT_ROOTS[harness])
    rows, skipped = scanner(base_dir, repo)
    out: list[SessionMilestone] = []
    for r in rows:
        if since is not None and (r.start is None or r.start < since):
            continue
        if until is not None and (r.start is None or r.start > until):
            continue
        if model is not None and model.lower() not in (r.model or "unknown").lower():
            continue
        if session is not None and session not in r.session_id:
            continue
        if cwd is not None and cwd not in (r.cwd or ""):
            continue
        out.append(r)
    return out, skipped


def cmd_cost(
    args: argparse.Namespace,
    records: list[SessionRecord],
    *,
    launch_slugs: Mapping[tuple[str, str], str] | None = None,
    file: TextIO | None = None,
) -> int:
    group_by = args.by

    groups: dict[str, list[SessionRecord]] = {}
    for r in records:
        if group_by == "total":
            key = "total"
        elif group_by == "day":
            key = r.timestamp[:10] if len(r.timestamp) >= 10 else "unknown"
        elif group_by == "project":
            key = r.cwd or "unknown"
        elif group_by == "harness":
            key = r.harness
        elif group_by == "model":
            key = r.model or "unknown"
        elif group_by == "session":
            key = f"{r.harness}:{r.session_id}"
        elif group_by == "item":
            key = (launch_slugs or {}).get((r.harness, r.session_id)) or "unattributed"
        else:
            key = "total"
        groups.setdefault(key, []).append(r)

    rows: list[dict[str, object]] = []
    for g_name, g_recs in sorted(groups.items()):
        cs = aggregate_cost(g_recs)
        rows.append(
            {
                "group": g_name,
                "sessions": cs.sessions,
                "messages": cs.messages,
                "input_tokens": cs.input_tokens,
                "output_tokens": cs.output_tokens,
                "cache_read_tokens": cs.cache_read_tokens,
                "cache_write_tokens": cs.cache_write_tokens,
                "total_tokens": cs.total_tokens,
                "cost_usd": round(cs.cost_usd, 4) if cs.cost_usd is not None else None,
                "cost_origin": cs.cost_origin,
            }
        )

    if args.json:
        print(json.dumps(rows, indent=2), file=file or sys.stdout)
        return 0

    if not rows:
        cli_common.qprint(
            "No session records found matching filters.", quiet=args.quiet, file=file
        )
        return 0

    col_group = "Group" if group_by != "total" else "Rollup"
    headers = [
        col_group,
        "Sess",
        "Msgs",
        "In",
        "Out",
        "Cache Read",
        "Cache Write",
        "Total Tok",
        "Cost USD",
        "Origin",
    ]

    table_data: list[list[str]] = []
    for r in rows:
        cost_display = f"${r['cost_usd']:.4f}" if r["cost_usd"] is not None else "-"
        table_data.append(
            [
                str(r["group"]),
                str(r["sessions"]),
                str(r["messages"]),
                f"{int(r['input_tokens']):,}",
                f"{int(r['output_tokens']):,}",
                f"{int(r['cache_read_tokens']):,}",
                f"{int(r['cache_write_tokens']):,}",
                f"{int(r['total_tokens']):,}",
                cost_display,
                str(r["cost_origin"]),
            ]
        )

    widths = [len(h) for h in headers]
    for row in table_data:
        for i, val in enumerate(row):
            widths[i] = max(widths[i], len(val))

    header_line = "  ".join(
        f"{h:<{widths[i]}}" if i == 0 or i == 9 else f"{h:>{widths[i]}}"
        for i, h in enumerate(headers)
    )
    sep_line = "  ".join("-" * widths[i] for i in range(len(headers)))

    cli_common.qprint(header_line, quiet=args.quiet, file=file)
    cli_common.qprint(sep_line, quiet=args.quiet, file=file)
    for row in table_data:
        line = "  ".join(
            f"{row[i]:<{widths[i]}}" if i == 0 or i == 9 else f"{row[i]:>{widths[i]}}"
            for i in range(len(row))
        )
        cli_common.qprint(line, quiet=args.quiet, file=file)

    if len(rows) > 1:
        cli_common.qprint(sep_line, quiet=args.quiet, file=file)
        total_sessions = len({rec.session_id for rec in records})
        total_msgs = len(records)
        tot_in = sum(int(r["input_tokens"]) for r in rows)
        tot_out = sum(int(r["output_tokens"]) for r in rows)
        tot_cr = sum(int(r["cache_read_tokens"]) for r in rows)
        tot_cw = sum(int(r["cache_write_tokens"]) for r in rows)
        tot_tok = sum(int(r["total_tokens"]) for r in rows)
        all_costs = [r["cost_usd"] for r in rows if r["cost_usd"] is not None]
        tot_cost = f"${sum(all_costs):.4f}" if all_costs else "-"
        tot_origins = {str(r["cost_origin"]) for r in rows}
        if tot_origins == {"native"}:
            tot_origin = "native"
        elif tot_origins == {"derived"}:
            tot_origin = "derived"
        elif tot_origins == {"unavailable"}:
            tot_origin = "unavailable"
        elif tot_origins == {"native", "derived"}:
            tot_origin = "native+derived"
        elif "native" in tot_origins or "derived" in tot_origins:
            tot_origin = "partial"
        else:
            tot_origin = "unavailable"

        total_row = [
            "TOTAL",
            str(total_sessions),
            str(total_msgs),
            f"{tot_in:,}",
            f"{tot_out:,}",
            f"{tot_cr:,}",
            f"{tot_cw:,}",
            f"{tot_tok:,}",
            tot_cost,
            tot_origin,
        ]
        tot_line = "  ".join(
            f"{total_row[i]:<{widths[i]}}"
            if i == 0 or i == 9
            else f"{total_row[i]:>{widths[i]}}"
            for i in range(len(total_row))
        )
        cli_common.qprint(tot_line, quiet=args.quiet, file=file)

    return 0


def _milestone_row(m: SessionMilestone) -> dict[str, object]:
    """One milestones JSON row from a session milestone."""
    return {
        "harness": m.harness,
        "session_id": m.session_id,
        "model": m.model,
        "cwd": m.cwd,
        "start": m.start.strftime("%Y-%m-%dT%H:%M:%SZ") if m.start else None,
        "minutes_to_first_write": (
            round(m.minutes_to_first_write, 2)
            if m.minutes_to_first_write is not None
            else None
        ),
        "spend_to_first_write": (
            round(m.spend_to_first_write, 6)
            if m.spend_to_first_write is not None
            else None
        ),
        "first_write_cost_origin": m.first_write_cost_origin,
        "first_write_path": m.first_write_path,
        "total_minutes": (
            round(m.total_minutes, 2) if m.total_minutes is not None else None
        ),
        "total_spend": (round(m.total_spend, 6) if m.total_spend is not None else None),
        "total_cost_origin": m.total_cost_origin,
        "status": m.status,
    }


def cmd_milestones(
    args: argparse.Namespace,
    *,
    roots: Mapping[str, Path | None] | None = None,
    file: TextIO | None = None,
) -> int:
    """Report per-session time and spend from start to the first file write.

    Session-level selection: --since/--until filter on session start,
    --model on the session's starting model, --session/--cwd on substrings.
    Harnesses whose logs carry no explicit write tool call are reported as
    one 'unsupported' row each — never as a zero measurement. No launch-time
    column exists: session logs hold no real launch signal.
    """
    since_dt = parse_date_boundary(args.since, is_until=False)
    until_dt = parse_date_boundary(args.until, is_until=True)
    if args.harness == "all":
        requested: tuple[str, ...] = (*MILESTONE_HARNESSES, "codex", "copilot")
    else:
        requested = (args.harness,)

    rows: list[dict[str, object]] = []
    for harness in requested:
        if harness in UNSUPPORTED_MILESTONE_HARNESSES:
            rows.append(
                {
                    "harness": harness,
                    "session_id": None,
                    "model": None,
                    "cwd": None,
                    "start": None,
                    "minutes_to_first_write": None,
                    "spend_to_first_write": None,
                    "first_write_cost_origin": "unavailable",
                    "first_write_path": None,
                    "total_minutes": None,
                    "total_spend": None,
                    "total_cost_origin": "unavailable",
                    "status": "unsupported",
                    "reason": UNSUPPORTED_MILESTONE_HARNESSES[harness],
                }
            )
            continue
        root = roots.get(harness) if roots is not None else None
        milestones, skipped = session_milestones(
            harness,
            root,
            repo=args.repo,
            since=since_dt,
            until=until_dt,
            model=args.model,
            session=args.session,
            cwd=args.cwd,
        )
        _report_skips(skipped, verbose=bool(getattr(args, "verbose", False)))
        rows.extend(_milestone_row(m) for m in milestones)

    if args.json:
        print(json.dumps(rows, indent=2), file=file or sys.stdout)
        return 0

    if not rows:
        cli_common.qprint(
            "No session records found matching filters.", quiet=args.quiet, file=file
        )
        return 0

    headers = [
        "Harness",
        "Session",
        "Model",
        "Start",
        "FirstWrite",
        "FirstSpend",
        "Path",
        "Total",
        "TotalSpend",
        "Origin",
        "Status",
    ]

    def _fmt_min(v: object) -> str:
        return f"{float(v):.0f}m" if isinstance(v, (int, float)) else "-"

    def _fmt_usd(v: object) -> str:
        return f"${float(v):.4f}" if isinstance(v, (int, float)) else "-"

    table: list[list[str]] = []
    for r in rows:
        if r.get("status") == "unsupported":
            table.append([str(r["harness"])] + ["-"] * 9 + ["unsupported"])
            continue
        table.append(
            [
                str(r["harness"]),
                str(r["session_id"]),
                str(r["model"]),
                str(r["start"] or "-")[:16],
                _fmt_min(r["minutes_to_first_write"]),
                _fmt_usd(r["spend_to_first_write"]),
                str(r["first_write_path"] or "-"),
                _fmt_min(r["total_minutes"]),
                _fmt_usd(r["total_spend"]),
                f"{r['first_write_cost_origin']}/{r['total_cost_origin']}",
                str(r["status"]),
            ]
        )

    left_align = {0, 1, 2, 3, 6, 9, 10}
    widths = [
        max(len(h), *(len(row[i]) for row in table)) for i, h in enumerate(headers)
    ]
    fmt = lambda row: "  ".join(  # noqa: E731
        f"{row[i]:<{widths[i]}}" if i in left_align else f"{row[i]:>{widths[i]}}"
        for i in range(len(row))
    )
    cli_common.qprint(fmt(headers), quiet=args.quiet, file=file)
    cli_common.qprint("  ".join("-" * w for w in widths), quiet=args.quiet, file=file)
    for row in table:
        cli_common.qprint(fmt(row), quiet=args.quiet, file=file)
    return 0


def cmd_prompts(
    args: argparse.Namespace,
    records: list[SessionRecord],
    *,
    file: TextIO | None = None,
) -> int:
    prompt_records = [r for r in records if r.role == "user" and r.text.strip()]

    if args.grep:
        try:
            pattern = re.compile(args.grep, re.IGNORECASE)
            prompt_records = [r for r in prompt_records if pattern.search(r.text)]
        except re.error as e:
            print(f"Error: invalid regex '{args.grep}': {e}", file=sys.stderr)
            return 1

    if args.limit and args.limit > 0:
        prompt_records = prompt_records[-args.limit :]

    is_jsonl = getattr(args, "format", "markdown") == "jsonl" or getattr(
        args, "json", False
    )

    if is_jsonl:
        for r in prompt_records:
            obj = {
                "timestamp": r.timestamp,
                "harness": r.harness,
                "session_id": r.session_id,
                "cwd": r.cwd,
                "model": r.model,
                "is_subagent": r.is_subagent,
                "prompt": r.text,
            }
            print(json.dumps(obj), file=file or sys.stdout)
        return 0

    if not prompt_records:
        cli_common.qprint(
            "No prompt records found matching filters.", quiet=args.quiet, file=file
        )
        return 0

    for r in prompt_records:
        loc = f" | cwd: {r.cwd}" if r.cwd else ""
        header = f"### [{r.harness}] {r.timestamp} | session: {r.session_id}{loc}"
        cli_common.qprint(header, quiet=args.quiet, file=file)
        cli_common.qprint(r.text, quiet=args.quiet, file=file)
        cli_common.qprint("", quiet=args.quiet, file=file)

    return 0


def cmd_search(
    args: argparse.Namespace,
    records: list[SessionRecord],
    *,
    file: TextIO | None = None,
) -> int:
    query = args.query or args.grep
    if not query:
        print("Error: search query required.", file=sys.stderr)
        return 1

    is_regex = bool(args.regex)
    if is_regex:
        try:
            pattern = re.compile(query, re.IGNORECASE)
            matches = [r for r in records if pattern.search(r.text)]
        except re.error as e:
            print(f"Error: invalid regex '{query}': {e}", file=sys.stderr)
            return 1
    else:
        q_lower = query.lower()
        matches = [r for r in records if q_lower in r.text.lower()]

    if args.limit and args.limit > 0:
        matches = matches[: args.limit]

    if args.json:
        out = [
            {
                "harness": r.harness,
                "session_id": r.session_id,
                "cwd": r.cwd,
                "timestamp": r.timestamp,
                "role": r.role,
                "model": r.model,
                "is_subagent": r.is_subagent,
                "text": r.text,
            }
            for r in matches
        ]
        print(json.dumps(out, indent=2), file=file or sys.stdout)
        return 0

    if not matches:
        cli_common.qprint(
            f"No matches found for '{query}'.", quiet=args.quiet, file=file
        )
        return 0

    for r in matches:
        loc = f" | cwd: {r.cwd}" if r.cwd else ""
        if r.harness == "copilot":
            header = f"[{r.harness}] {r.timestamp} | session: {r.session_id}{loc} (turn-level digest — no intra-turn context)"
        else:
            header = f"[{r.harness}] {r.timestamp} | session: {r.session_id}{loc} | role: {r.role}"

        cli_common.qprint(header, quiet=args.quiet, file=file)
        cli_common.qprint(r.text, quiet=args.quiet, file=file)
        cli_common.qprint("-" * 60, quiet=args.quiet, file=file)

    return 0


# ── CLI parser ────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="analyze_sessions.py",
        description="Multi-harness session analysis tool across pi, Claude Code, opencode, Copilot CLI, agy, and Codex.",
    )
    cli_common.add_verbosity_args(parser)

    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    def add_common_filters(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--harness",
            choices=["all", "pi", "claude", "opencode", "copilot", "agy", "codex"],
            default="all",
            help="harness to analyze: all, pi, claude, opencode, copilot, agy, codex (default: all; note: agy is opt-in and excluded from 'all')",
        )
        p.add_argument(
            "--since",
            help="filter sessions on/after date/time (ISO 8601 YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS, or relative e.g. 7d, today)",
        )
        p.add_argument(
            "--until",
            help="filter sessions on/before date/time (ISO 8601 YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)",
        )
        p.add_argument("--cwd", help="filter by working directory substring")
        p.add_argument("--model", help="filter by model name substring")
        p.add_argument("--session", help="filter by session ID substring")
        p.add_argument(
            "--limit", type=int, default=None, help="limit number of results"
        )
        p.add_argument("--grep", "-g", help="filter message text by substring or regex")
        p.add_argument("--json", action="store_true", help="emit output as JSON")

    # cost subcommand
    p_cost = subparsers.add_parser(
        "cost", help="token and USD cost analysis and rollups"
    )
    add_common_filters(p_cost)
    cli_common.add_verbosity_args(p_cost)
    p_cost.add_argument(
        "--by",
        choices=["total", "day", "project", "harness", "model", "session", "item"],
        default="total",
        help="rollup grouping: total, day, project, harness, model, session, item (default: total; item = backlog slug from each session's launch prompt, unattributed otherwise)",
    )
    p_cost.add_argument(
        "--no-subagents",
        action="store_true",
        help="exclude subagent / child sessions (included by default)",
    )

    # milestones subcommand
    p_milestones = subparsers.add_parser(
        "milestones",
        help="per-session time and spend from session start to the first file write",
    )
    add_common_filters(p_milestones)
    cli_common.add_verbosity_args(p_milestones)
    p_milestones.add_argument(
        "--repo",
        help="repo root whose file writes count (default: each session's own cwd; sessions with neither root report no-root, never a number)",
    )

    # prompts subcommand
    p_prompts = subparsers.add_parser("prompts", help="list user prompts")
    add_common_filters(p_prompts)
    cli_common.add_verbosity_args(p_prompts)
    p_prompts.add_argument(
        "--format",
        choices=["markdown", "jsonl"],
        default="markdown",
        help="output format (default: markdown)",
    )
    p_prompts.add_argument(
        "--include-subagents",
        action="store_true",
        help="include subagent / child sessions (excluded by default)",
    )

    # search subcommand
    p_search = subparsers.add_parser("search", help="search message transcripts")
    p_search.add_argument("query", nargs="?", default=None, help="search query text")
    add_common_filters(p_search)
    cli_common.add_verbosity_args(p_search)
    p_search.add_argument(
        "--regex",
        "-r",
        action="store_true",
        help="treat query as regular expression",
    )
    p_search.add_argument(
        "--context",
        "-C",
        type=int,
        default=0,
        help="context lines around match (default: 0)",
    )
    p_search.add_argument(
        "--include-subagents",
        action="store_true",
        help="include subagent / child sessions (excluded by default)",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    since_dt = parse_date_boundary(args.since, is_until=False)
    until_dt = parse_date_boundary(args.until, is_until=True)

    if args.subcommand == "milestones":
        return cmd_milestones(args, roots=_resolve_cli_roots())

    include_subagents = True
    if args.subcommand == "cost":
        include_subagents = not args.no_subagents
    elif args.subcommand in ("prompts", "search"):
        include_subagents = args.include_subagents

    # Per-command query shape: prompts narrows to user prompts (grep is
    # always a case-insensitive regex there, limit keeps the LAST N);
    # search matches any role (substring unless --regex, limit keeps the
    # FIRST N). cost honors only its structural filters (--grep/--limit are
    # historically ignored there and stay ignored).
    text: str | None = None
    text_regex = False
    limit: int | None = None
    keep: Literal["first", "last"] = "first"
    role: str | None = None
    if args.subcommand == "prompts":
        role = "user"
        text = args.grep
        text_regex = True
        limit = args.limit
        keep = "last"
    elif args.subcommand == "search":
        text = args.query or args.grep
        text_regex = bool(args.regex)
        limit = args.limit
        keep = "first"

    query = SessionQuery(
        harnesses=None if args.harness == "all" else frozenset({args.harness}),
        since=since_dt,
        until=until_dt,
        cwd=args.cwd,
        model=args.model,
        session_id=args.session,
        role=role,
        include_subagents=include_subagents,
        text=text,
        text_regex=text_regex,
        limit=limit,
        keep=keep,
    )
    try:
        result = query_sessions(query, roots=_resolve_cli_roots())
    except re.error as e:
        print(f"Error: invalid regex '{text}': {e}", file=sys.stderr)
        return 1

    _report_skips(result.skipped_records, verbose=bool(args.verbose))

    if args.subcommand == "cost":
        launch_slugs = None
        if args.by == "item":
            launch_slugs = load_launch_slugs(_resolve_cli_roots())
        return cmd_cost(args, result.records, launch_slugs=launch_slugs)
    if args.subcommand == "prompts":
        return cmd_prompts(args, result.records)
    if args.subcommand == "search":
        return cmd_search(args, result.records)

    return 0


if __name__ == "__main__":
    sys.exit(main())
