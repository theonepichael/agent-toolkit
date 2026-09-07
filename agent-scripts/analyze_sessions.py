#!/usr/bin/env python3
"""analyze_sessions.py — multi-harness session analysis tool.

Analyzes coding-agent sessions across pi, Claude Code, opencode, Copilot CLI,
and agy harnesses. Exposes subcommands for calculating token/USD costs, listing
user prompts, and searching message transcripts.

Usage:
    python3 ~/.claude/scripts/analyze_sessions.py cost [--by total|day|project|harness|model|session] [filters]
    python3 ~/.claude/scripts/analyze_sessions.py prompts [--format markdown|jsonl] [filters]
    python3 ~/.claude/scripts/analyze_sessions.py search <query> [--regex] [--context N] [filters]

Flags:
  --harness        harness to analyze: all, pi, claude, opencode, copilot, agy
                   (default: all; note: agy is opt-in and excluded from 'all')
  --since          filter sessions on/after date/time (ISO 8601 YYYY-MM-DD or
                   YYYY-MM-DDTHH:MM:SS, or relative e.g. 7d, today)
  --until          filter sessions on/before date/time (ISO 8601 YYYY-MM-DD or
                   YYYY-MM-DDTHH:MM:SS)
  --cwd            filter by working directory substring
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

Env vars: none.
Files read:
  ~/.pi/agent/sessions/**/*.jsonl
  ~/.claude/projects/**/*.jsonl
  ~/.local/share/opencode/opencode.db
  ~/.copilot/session-store.db
  ~/.gemini/antigravity-cli/brain/**/transcript_full.jsonl (or transcript.jsonl)
Files written: none.
Exit codes: 0 success; 1 operational error; 2 bad usage.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TextIO

import cli_common

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


# ── Adapters ──────────────────────────────────────────────────────────────


def load_pi_records(
    base_dir: Path | None = None,
    since_dt: datetime | None = None,
    until_dt: datetime | None = None,
) -> list[SessionRecord]:
    if base_dir is None:
        base_dir = Path.home() / ".pi" / "agent" / "sessions"
    if not base_dir.exists():
        return []
    records: list[SessionRecord] = []
    for p in base_dir.rglob("*.jsonl"):
        is_subagent_path = "subagent" in str(p).lower()
        session_id = p.stem
        session_cwd: str | None = None
        current_model: str | None = None

        try:
            with p.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue

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

                        msg_model = (
                            obj.get("model") or msg.get("model") or current_model
                        )
                        if msg_model:
                            current_model = msg_model

                        content = msg.get("content")
                        text_parts: list[str] = []
                        if isinstance(content, str):
                            text_parts.append(content)
                        elif isinstance(content, list):
                            for block in content:
                                if isinstance(block, dict):
                                    if block.get("type") == "text" and "text" in block:
                                        text_parts.append(str(block["text"]))
                                elif isinstance(block, str):
                                    text_parts.append(block)
                        text = "\n".join(text_parts).strip()

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
        except OSError:
            continue
    return records


def load_claude_records(
    base_dir: Path | None = None,
    since_dt: datetime | None = None,
    until_dt: datetime | None = None,
) -> list[SessionRecord]:
    if base_dir is None:
        base_dir = Path.home() / ".claude" / "projects"
    if not base_dir.exists():
        return []
    records: list[SessionRecord] = []
    for p in base_dir.rglob("*.jsonl"):
        session_id = p.stem
        session_cwd: str | None = None
        try:
            with p.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue

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

                    text_parts: list[str] = []
                    content = (
                        msg.get("content")
                        if isinstance(msg, dict)
                        else obj.get("content")
                    )
                    if isinstance(content, str):
                        text_parts.append(content)
                    elif isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict):
                                if block.get("type") == "text" and "text" in block:
                                    text_parts.append(str(block["text"]))
                            elif isinstance(block, str):
                                text_parts.append(block)
                    text = "\n".join(text_parts).strip()

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
                            cw_tok = int(
                                usage.get("cache_creation_input_tokens", 0) or 0
                            )

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
        except OSError:
            continue
    return records


def load_opencode_records(
    db_path: Path | None = None,
    since_dt: datetime | None = None,
    until_dt: datetime | None = None,
    cwd_filter: str | None = None,
    session_filter: str | None = None,
) -> list[SessionRecord]:
    if db_path is None:
        db_path = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
    if not db_path.exists():
        return []

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        try:
            conn = sqlite3.connect(str(db_path))
        except sqlite3.Error:
            return []

    records: list[SessionRecord] = []
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
            return []

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
    finally:
        conn.close()
    return records


def load_copilot_records(
    db_path: Path | None = None,
    since_dt: datetime | None = None,
    until_dt: datetime | None = None,
    cwd_filter: str | None = None,
    session_filter: str | None = None,
) -> list[SessionRecord]:
    if db_path is None:
        db_path = Path.home() / ".copilot" / "session-store.db"
    if not db_path.exists():
        return []

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        try:
            conn = sqlite3.connect(str(db_path))
        except sqlite3.Error:
            return []

    records: list[SessionRecord] = []
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
                        cost_usd=None,
                        cost_origin="unavailable",
                        is_subagent=False,
                    )
                )
    finally:
        conn.close()
    return records


def load_agy_records(
    base_dir: Path | None = None,
    since_dt: datetime | None = None,
    until_dt: datetime | None = None,
    session_filter: str | None = None,
) -> list[SessionRecord]:
    if base_dir is None:
        base_dir = Path.home() / ".gemini" / "antigravity-cli" / "brain"
    if not base_dir.exists():
        return []
    records: list[SessionRecord] = []

    for sess_dir in base_dir.iterdir():
        if not sess_dir.is_dir():
            continue
        session_id = sess_dir.name
        if session_filter and session_filter not in session_id:
            continue

        log_dir = sess_dir / ".system_generated" / "logs"
        transcript_file = log_dir / "transcript_full.jsonl"
        if not transcript_file.exists():
            transcript_file = log_dir / "transcript.jsonl"
        if not transcript_file.exists():
            continue

        try:
            with transcript_file.open("r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue

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
        except OSError:
            continue
    return records


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
) -> list[SessionRecord]:
    records: list[SessionRecord] = []

    if harness in ("all", "pi"):
        records.extend(load_pi_records(pi_dir, since_dt, until_dt))
    if harness in ("all", "claude"):
        records.extend(load_claude_records(claude_dir, since_dt, until_dt))
    if harness in ("all", "opencode"):
        records.extend(
            load_opencode_records(
                opencode_db, since_dt, until_dt, cwd_filter, session_filter
            )
        )
    if harness in ("all", "copilot"):
        records.extend(
            load_copilot_records(
                copilot_db, since_dt, until_dt, cwd_filter, session_filter
            )
        )
    if harness == "agy":
        records.extend(load_agy_records(agy_dir, since_dt, until_dt, session_filter))

    if model_filter:
        mf = model_filter.lower()
        records = [r for r in records if r.model and mf in r.model.lower()]

    if cwd_filter:
        records = [r for r in records if r.cwd and cwd_filter in r.cwd]

    if session_filter:
        records = [r for r in records if session_filter in r.session_id]

    if not include_subagents:
        records = [r for r in records if not r.is_subagent]

    records.sort(key=lambda r: r.timestamp)
    return records


# ── Subcommand Handlers ───────────────────────────────────────────────────


def cmd_cost(
    args: argparse.Namespace,
    records: list[SessionRecord],
    *,
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
        else:
            key = "total"
        groups.setdefault(key, []).append(r)

    rows: list[dict[str, object]] = []
    for g_name, g_recs in sorted(groups.items()):
        sessions_count = len({rec.session_id for rec in g_recs})
        msgs_count = len(g_recs)
        in_tok = sum(rec.input_tokens for rec in g_recs)
        out_tok = sum(rec.output_tokens for rec in g_recs)
        cr_tok = sum(rec.cache_read_tokens for rec in g_recs)
        cw_tok = sum(rec.cache_write_tokens for rec in g_recs)
        total_tok = in_tok + out_tok + cr_tok + cw_tok

        costs = [rec.cost_usd for rec in g_recs if rec.cost_usd is not None]
        cost_sum = sum(costs) if costs else None

        cost_bearing_recs = [
            rec for rec in g_recs if rec.total_tokens > 0 or rec.cost_usd is not None
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

        rows.append(
            {
                "group": g_name,
                "sessions": sessions_count,
                "messages": msgs_count,
                "input_tokens": in_tok,
                "output_tokens": out_tok,
                "cache_read_tokens": cr_tok,
                "cache_write_tokens": cw_tok,
                "total_tokens": total_tok,
                "cost_usd": round(cost_sum, 4) if cost_sum is not None else None,
                "cost_origin": origin_str,
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
        description="Multi-harness session analysis tool across pi, Claude Code, opencode, Copilot CLI, and agy.",
    )
    cli_common.add_verbosity_args(parser)

    subparsers = parser.add_subparsers(dest="subcommand", required=True)

    def add_common_filters(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--harness",
            choices=["all", "pi", "claude", "opencode", "copilot", "agy"],
            default="all",
            help="harness to analyze: all, pi, claude, opencode, copilot, agy (default: all; note: agy is opt-in and excluded from 'all')",
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
        choices=["total", "day", "project", "harness", "model", "session"],
        default="total",
        help="rollup grouping: total, day, project, harness, model, session (default: total)",
    )
    p_cost.add_argument(
        "--no-subagents",
        action="store_true",
        help="exclude subagent / child sessions (included by default)",
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

    include_subagents = True
    if args.subcommand == "cost":
        include_subagents = not args.no_subagents
    elif args.subcommand in ("prompts", "search"):
        include_subagents = args.include_subagents

    records = load_all_records(
        harness=args.harness,
        since_dt=since_dt,
        until_dt=until_dt,
        cwd_filter=args.cwd,
        model_filter=args.model,
        session_filter=args.session,
        include_subagents=include_subagents,
    )

    if args.subcommand == "cost":
        return cmd_cost(args, records)
    if args.subcommand == "prompts":
        return cmd_prompts(args, records)
    if args.subcommand == "search":
        return cmd_search(args, records)

    return 0


if __name__ == "__main__":
    sys.exit(main())
