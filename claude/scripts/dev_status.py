#!/usr/bin/env python3
"""dev_status.py v2 — slug IDs, structured dependency graph, pure render.

Backs the personal task/pending-item dashboard shared by Claude Code and
other harnesses that read and write the same on-disk JSON store. Every
mutating subcommand acquires an exclusive file lock, reads the current
state, applies its change, writes atomically, and bumps a monotonic
revision counter so numeric positional references (e.g. ``done 3``) can be
guarded against staleness with ``--if-rev``.

Flags
  --quiet, -q    suppress non-essential output
  --verbose, -v  emit extra diagnostic messages to stderr

Requires Python 3.12+.
"""

import argparse
import fcntl
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import NotRequired, TextIO, TypedDict, cast

import cli_common
import llm_backends

DATA_DIR = Path.home() / ".claude" / "data" / "backlog"
ITEMS_FILE = DATA_DIR / "items.json"
PENDING_FILE = DATA_DIR / "pending_items.json"
META_FILE = DATA_DIR / "_meta.json"
LOCK_FILE = DATA_DIR / ".backlog.lock"
JOURNAL_FILE = DATA_DIR / "journal.jsonl"
RUNS_FILE = DATA_DIR / "runs.jsonl"
MACHINE_ID_FILE = DATA_DIR / "_machine_id"
RECAP_CACHE_FILE = DATA_DIR / "recap-cache.json"
RECAP_REGEN_LOCK_FILE = DATA_DIR / "recap-regen.lock"

OUT_OF_SCOPE_DIR = Path.home() / ".claude" / "data" / "backlog-out-of-scope"
OUT_OF_SCOPE_INDEX_FILE = OUT_OF_SCOPE_DIR / "index.json"
OUT_OF_SCOPE_LOCK_FILE = OUT_OF_SCOPE_DIR / ".out-of-scope.lock"

# ── recap tuning knobs ──────────────────────────────────────────────────────
RECAP_TTL_SECONDS = 30 * 60
RECAP_STALE_MAX_HOURS = 24
RECAP_DISPATCH_WINDOW_HOURS = 48
RECAP_MAX_CHARS = 400
RECAP_TIMEOUT_SECONDS = float(os.environ.get("DEVSTATUS_RECAP_TIMEOUT_SECONDS", "60"))
RECAP_AGY_MODEL = os.environ.get("DEVSTATUS_RECAP_AGY_MODEL", "Gemini 3.6 Flash (High)")

VALID_STATUSES = {"open", "in-progress", "in-review", "done"}
VALID_PRIORITIES = {"high", "normal", "low"}


def _agent_quiet() -> bool:
    """True when the caller asked to suppress agent-only stderr noise.

    Read as a function rather than a module-level constant so it reflects
    the environment at call time (a short-lived CLI process reads it once
    per invocation either way; a function also lets tests toggle it
    per-test without needing to reload the module).
    """
    return bool(os.environ.get("DEVSTATUS_AGENT"))


DONE_MAX_ITEMS = 5
DONE_SELECTION_VERSION = 2

# Sort rank for priority (absence == normal). Lower sorts first.
_PRIORITY_RANK = {"high": 0, "normal": 1, "low": 2}

VALID_PENDING_STATUSES = {"waiting_for_reply", "reply_received", "resolved"}
VALID_PENDING_KINDS = {"email", "chat", "approval"}
PENDING_MUTABLE_FIELDS = {
    "status",
    "description",
    "context",
    "next_steps",
    "blocking",
    "outcome",
    "source_ref",
}
IMMUTABLE_FIELDS = {"id", "created", "completed_at"}
BACKLOG_MUTABLE_FIELDS = {
    "summary",
    "category",
    "blocked_by",
    "related_files",
    "context",
    "next_steps",
    "priority",
    "status",
    "claimed_by",
}
DEFAULT_CLAIM_TTL_SECONDS = 2 * 60 * 60  # 2 hours
# Subcommand names blocked from use as item slugs. Match is exact: only a
# slug equal to one of these bare verbs is refused — `remove-probe`,
# `add-feature`, etc. are accepted. Argparse never confuses a hyphenated
# slug with a subcommand (the subcommand is parsed from argv positionally),
# and the bare-verb reservation exists purely for dashboard clarity (no
# item literally named `remove`). Prefix-match refusal was considered and
# rejected: it would forbid natural slugs like `update-deps` for no real
# dispatch-safety gain (2026-07-25 decision).
SUBCOMMANDS = (
    "render",
    "list",
    "show",
    "add",
    "update",
    "start",
    "done",
    "review",
    "approve",
    "reject",
    "gate-set",
    "gate-pass",
    "run",
    "runs",
    "backfill-gate",
    "rename",
    "remove",
    "block",
    "unblock",
    "prune",
    "recap",
)
RESERVED_SLUGS = set(SUBCOMMANDS) | {"pending", "out-of-scope", "all", "help", "new"}
SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)+$")
SLUG_MIN, SLUG_MAX = 3, 40

CATEGORY_TAG = {"bug": "bug", "feature": "feat", "chore": "chore", "research": "rsrch"}
STALE_DAYS = 7
SECTION_WIDTH = 76
_LIST_SUMMARY_MAX = 46
_RESET = "\x1b[0m"
_COLORS = {
    "in_progress": "\x1b[33m",
    "ready": "\x1b[32m",
    "blocked": "\x1b[31m",
    "in_review": "\x1b[36m",
    "done": "\x1b[2m",
    "pending": "\x1b[35m",
    "warn": "\x1b[31m",
    "prio_high": "\x1b[1;31m",
    "prio_low": "\x1b[2m",
    "dim": "\x1b[2m",
}
KNOWN_PROJECT_PREFIXES = ("iron-lb", "ajhp", "meta", "work")


# ── data model ───────────────────────────────────────────────────────────────


class Gate(TypedDict):
    """A judgment-step verification checkpoint on a backlog item.

    Set once via ``gate-set`` when an item's plan/spec is classified
    (mechanical steps need no gate; a plan with judgment steps gets
    ``required: True`` and ``criteria`` drawn from those steps' acceptance
    criteria). ``passed_at`` stays ``None`` until ``gate-pass`` records one,
    and every ``gate-set`` call resets ``passed_at`` to ``None``
    — re-classifying (e.g. after a plan revision) invalidates any prior
    pass, mirroring grill.py's ``revise`` resetting a decision's verdict.

    ``gate-pass`` only records a pass when every criterion is covered by
    recorded run evidence (a command executed via the ``run`` subcommand)
    or an explicit per-criterion manual note — a bare claim is refused.
    ``set_at`` (full aware-UTC ISO datetime) stamps the current gate
    generation: run evidence with ``started_at`` older than ``set_at`` no
    longer counts, so re-classifying automatically orphans older evidence
    without deleting it. Gates classified before ``set_at`` existed carry
    no key, which gate-pass treats as "no lower bound". ``passed_via`` is
    ``run-evidence``/``manual``/``mixed``; ``coverage`` maps each criterion
    number to ``{"kind": "run", "run_id": ...}`` or
    ``{"kind": "manual", "note": ...}`` so ``show`` displays what
    satisfied what.
    """

    # `passed: bool | None` was dropped — redundant with `passed_at`
    # (non-None means passed); don't reintroduce it.
    required: bool
    criteria: list[str]
    passed_at: str | None
    set_at: NotRequired[str]
    passed_via: NotRequired[str]
    coverage: NotRequired[dict[str, dict[str, str]]]


class RunRecord(TypedDict):
    """One recorded command execution — a row of the ``runs.jsonl`` sidecar.

    Written by the ``run`` subcommand, which executes the command itself
    (never accepting a self-reported result) and appends one JSON line per
    run: ``{run_id, item, command, exit_code, timed_out, started_at,
    duration_s, cwd}``. ``exit_code`` is ``None`` exactly when
    ``timed_out`` is true. ``gate-pass`` cites rows from this file as
    per-criterion evidence; ``item`` holds the owning backlog slug.
    """

    run_id: str
    item: str
    command: str
    exit_code: int | None
    timed_out: bool
    started_at: str
    duration_s: float
    cwd: str


class BacklogItem(TypedDict):
    """A single backlog item as stored in ``items.json`` (schema v2).

    ``priority`` and ``completed_at`` are absent unless explicitly set —
    absence of ``priority`` is equivalent to ``"normal"`` (see
    :data:`_PRIORITY_RANK`), and ``completed_at`` only exists while
    ``status`` is ``"done"`` (stamped and cleared by
    :func:`_apply_status_transition`). ``gate`` is likewise absent on any
    item created before its introduction or never classified — absence is
    equivalent to an inert gate (see :func:`_gate_blocks`), the same
    absence-means-default convention ``priority`` already uses.
    """

    id: str
    created: str
    updated: str
    status: str
    summary: str
    category: str
    blocked_by: list[str]
    related_files: list[dict[str, object]]
    context: str
    next_steps: str
    priority: NotRequired[str]
    completed_at: NotRequired[str]
    review_feedback: NotRequired[str]
    review_content_hash: NotRequired[str]
    gate: NotRequired[Gate]


class PendingItem(TypedDict):
    """A single waiting-on-someone-else item as stored in ``pending_items.json``.

    ``resolved_at`` is absent unless ``status`` is ``"resolved"`` (stamped
    and cleared by :func:`_apply_status_transition`).
    """

    id: str
    created: str
    updated: str
    status: str
    description: str
    kind: str
    source_ref: dict[str, object]
    context: str
    next_steps: list[str]
    blocking: list[str]
    outcome: str | None
    resolved_at: NotRequired[str]


type BacklogIndex = dict[str, BacklogItem]
type RenderOrder = tuple[
    list[BacklogItem],
    list[BacklogItem],
    list[BacklogItem],
    list[BacklogItem],
    list[BacklogItem],
]


# ── helpers ───────────────────────────────────────────────────────────────────


def today() -> str:
    """Return today's date as an ISO-8601 string (``YYYY-MM-DD``)."""
    return date.today().isoformat()


_HASHED_CONTENT_FIELDS = ("summary", "context", "next_steps", "related_files")


def _content_hash(item: BacklogItem) -> str:
    """SHA-256 over the reviewer-visible content fields, stable serialization.

    Missing fields hash as ``None`` via ``.get(k)`` (not ``.get(k, "")``) —
    a field appearing later where it was previously absent counts as
    content drift, same as any other change. ``related_files`` list order
    is significant: reordering entries is a content change. Nested dict key
    order is not (``sort_keys=True`` recurses).
    """
    payload = json.dumps(
        {k: cast(dict[str, object], item).get(k) for k in _HASHED_CONTENT_FIELDS},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _detect_harness(explicit: str | None = None) -> str:
    """Detect current agent/environment harness name.

    Priority:
    1. explicit argument / --claimed-by flag
    2. DEVSTATUS_HARNESS environment variable
    3. PI_SESSION / PI_CODING_AGENT -> 'pi'
    4. CLAUDE_CODE / ANTHROPIC_CLI -> 'claude'
    5. ANTIGRAVITY / AGY_SESSION -> 'agy'
    6. OPENCODE_GATEWAY / OPENCODE -> 'opencode'
    7. GITHUB_COPILOT / COPILOT -> 'copilot'
    8. Fallback: 'cli'
    """
    if explicit:
        return explicit.strip()
    if env := os.environ.get("DEVSTATUS_HARNESS"):
        return env.strip()
    if os.environ.get("PI_SESSION") or os.environ.get("PI_CODING_AGENT"):
        return "pi"
    if os.environ.get("CLAUDE_CODE") or os.environ.get("ANTHROPIC_CLI"):
        return "claude"
    if os.environ.get("ANTIGRAVITY") or os.environ.get("AGY_SESSION"):
        return "agy"
    if os.environ.get("OPENCODE_GATEWAY") or os.environ.get("OPENCODE"):
        return "opencode"
    if os.environ.get("GITHUB_COPILOT") or os.environ.get("COPILOT"):
        return "copilot"
    return "cli"


def _claim_ttl_seconds() -> float:
    """Return configured claim TTL in seconds."""
    try:
        return float(
            os.environ.get(
                "DEVSTATUS_CLAIM_TTL_SECONDS", str(DEFAULT_CLAIM_TTL_SECONDS)
            )
        )
    except (ValueError, TypeError):
        return float(DEFAULT_CLAIM_TTL_SECONDS)


def _is_pid_alive(pid: int) -> bool:
    """Check whether a process with `pid` is currently alive on the local machine."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        # Process exists but belongs to another user
        return True
    except ProcessLookupError:
        # ESRCH: no such process
        return False
    except OSError as e:
        import errno

        if e.errno == errno.EPERM:
            return True
        if e.errno == errno.ESRCH:
            return False
        return False
    else:
        return True


def machine_id() -> str:
    """Return this machine's stable short id, creating it on first use.

    Not hostname — hostnames change/collide across machines this store is
    shared between. Matches the ``_meta.json``/``_sync-base.json`` aux-file
    convention: a small file alongside the data files, not part of the
    schema itself.
    """
    try:
        existing = MACHINE_ID_FILE.read_text().strip()
        if existing:
            return existing
    except OSError:
        pass
    new_id = secrets.token_hex(4)
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        MACHINE_ID_FILE.write_text(new_id)
    except OSError:
        pass
    return new_id


_machine_id = machine_id


def _make_claim(harness: str | None = None) -> dict[str, object]:
    """Create a fresh claim dictionary for the current session."""
    h = _detect_harness(harness)
    now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "harness": h,
        "machine_id": machine_id(),
        "pid": os.getpid(),
        "claimed_at": now_iso,
        "last_active": now_iso,
    }


def _check_worktree_guard(allow_main: bool = False, quiet: bool = False) -> None:
    """Check that start is not being executed from the main branch root of a git repository."""
    if allow_main:
        return
    try:
        res = subprocess.run(
            [
                "git",
                "rev-parse",
                "--git-common-dir",
                "--git-dir",
                "--abbrev-ref",
                "HEAD",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if getattr(res, "returncode", 1) != 0:
            return
        stdout = getattr(res, "stdout", "")
        if not isinstance(stdout, str):
            return
        lines = [line.strip() for line in stdout.strip().splitlines() if line.strip()]
        if len(lines) < 3:
            return
        git_common_dir, git_dir, branch = lines[0], lines[1], lines[2]
        common_path = Path(git_common_dir).resolve()
        dir_path = Path(git_dir).resolve()
        is_main_worktree = common_path == dir_path
        if is_main_worktree and branch in ("main", "master"):
            print(
                "[start] Refusing to start item on main/master checkout in a git repository.\n"
                "Create a dedicated worktree first (`git worktree add ../<repo>-<slug> -b <slug>`), "
                "or pass --allow-main to override.",
                file=sys.stderr,
            )
            sys.exit(1)
    except Exception:
        pass


def _check_claim_collision(
    item: BacklogItem,
    current_harness: str,
    current_machine: str,
    current_pid: int,
    force: bool = False,
    quiet: bool = False,
) -> None:
    """Refuse start if item is already actively claimed by another session."""
    claim = cast(dict[str, object], item.get("claimed_by"))
    if not isinstance(claim, dict):
        return
    claim_harness = str(claim.get("harness", "unknown"))
    claim_machine = str(claim.get("machine_id", ""))
    claim_pid = int(claim.get("pid") or 0) if str(claim.get("pid", "")).isdigit() else 0
    claim_last_active = str(claim.get("last_active") or claim.get("claimed_at") or "")

    if force:
        return

    if (
        claim_machine == current_machine
        and claim_pid == current_pid
        and current_pid > 0
    ):
        return

    if claim_machine == current_machine and claim_pid > 0:
        if _is_pid_alive(claim_pid):
            print(
                f"[start] {item.get('id', '?')} is actively claimed by {claim_harness} "
                f"(PID {claim_pid} on this machine). Use --force to take over the claim.",
                file=sys.stderr,
            )
            sys.exit(1)
        else:
            if not _agent_quiet() and not quiet:
                print(
                    f"[start] Previous claim by {claim_harness} (PID {claim_pid}) is dead. "
                    "Taking over claim.",
                    file=sys.stderr,
                )
            return

    ttl = _claim_ttl_seconds()
    if claim_last_active:
        try:
            ts_str = claim_last_active.replace("Z", "+00:00")
            dt = datetime.fromisoformat(ts_str)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            now = datetime.now(UTC)
            elapsed = (now - dt).total_seconds()
            if elapsed < ttl:
                rem_mins = int((ttl - elapsed) / 60)
                print(
                    f"[start] {item.get('id', '?')} was claimed by {claim_harness} on machine {claim_machine[:6]} "
                    f"{int(elapsed / 60)}m ago (active for {rem_mins}m more). Use --force to take over.",
                    file=sys.stderr,
                )
                sys.exit(1)
            else:
                if not _agent_quiet() and not quiet:
                    print(
                        f"[start] Previous claim by {claim_harness} expired (idle {int(elapsed / 60)}m). "
                        "Taking over claim.",
                        file=sys.stderr,
                    )
        except (ValueError, TypeError):
            pass


def _apply_status_transition(
    item: dict[str, object], new_status: str, stamp_field: str, done_value: str
) -> None:
    """Stamp or clear a completion timestamp as an item's status changes.

    Stamps ``stamp_field`` with today's date when ``new_status`` enters
    ``done_value``, and clears it when the item's status leaves
    ``done_value``. Called from every path that can change status, so the
    stamp can't be bypassed or left stale. Also clears ``claimed_by``
    when transitioning away from ``in-progress``.

    Args:
        item: The backlog or pending item being mutated, in place.
        new_status: The status value about to be applied.
        stamp_field: Name of the field to stamp/clear (e.g.
            ``"completed_at"`` for backlog items, ``"resolved_at"`` for
            pending items).
        done_value: The status value that triggers stamping (e.g.
            ``"done"`` or ``"resolved"``).
    """
    old_status = item.get("status")
    if new_status == done_value and old_status != done_value:
        item[stamp_field] = today()
    elif old_status == done_value and new_status != done_value:
        item.pop(stamp_field, None)
    if new_status != "in-progress":
        item.pop("claimed_by", None)


def _gate_blocks(gate: Mapping[str, object] | None) -> bool:
    """Return True if `gate` blocks a done-transition (required, unpassed)."""
    return bool(gate) and bool(gate.get("required")) and gate.get("passed_at") is None


def _gate_block_message(cmd: str, item: BacklogItem) -> str | None:
    """Return a refusal message if ``item``'s gate blocks a done-transition.

    ``None`` means the gate doesn't block: absent, not required, or already
    passed. Called from both :func:`cmd_done` and :func:`cmd_approve` —
    either command can complete an item, so both need the same check.

    Args:
        cmd: Command name to prefix onto the message.
        item: The item being transitioned to done.
    """
    gate = item.get("gate")
    if not _gate_blocks(gate):
        return None
    n = len(gate.get("criteria", []))
    return (
        f"[{cmd}] {item.get('id', '?')} has an unmet gate ({n} criterion/criteria "
        "unconfirmed) -- record evidence with 'run <id> -- <command>' then pass "
        "'gate-pass <id>' with a coverage payload; 'show <id>' to "
        "review the criteria."
    )


def _check_gate_or_exit(cmd: str, item: BacklogItem) -> None:
    """Print a refusal and exit(1) if item's gate blocks `cmd`'s transition."""
    gate_msg = _gate_block_message(cmd, item)
    if gate_msg:
        print(gate_msg, file=sys.stderr)
        sys.exit(1)


def _category_tag(category: str) -> str:
    """Render a category as a bracketed line prefix, e.g. ``"[bug] "``.

    Unknown categories are truncated to 5 characters rather than rejected,
    since the tag is cosmetic only.

    Args:
        category: The item's category, or ``""`` for none.

    Returns:
        A bracketed, space-suffixed tag, or ``""`` if ``category`` is falsy.
    """
    if not category:
        return ""
    tag = CATEGORY_TAG.get(category, category[:5])
    return f"[{tag}] "


def _age_days(updated_str: str) -> int | None:
    """Return the number of days between today and an ISO date string.

    Args:
        updated_str: An ISO-8601 date string, or any invalid/empty value.

    Returns:
        Whole days elapsed since ``updated_str``, or ``None`` if it isn't a
        valid ISO date.
    """
    try:
        d = date.fromisoformat(updated_str)
    except (TypeError, ValueError):
        return None
    return (date.today() - d).days


def _normalize_done_stamp(raw: object) -> datetime | None:
    """Normalize a non-empty ISO completion stamp to an aware UTC datetime."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        dt = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _done_selection_stamp(item: BacklogItem) -> datetime | None:
    """Return the normalized completion key for a done item.

    ``completed_at`` takes precedence when it contains a non-empty value.
    Empty values are treated as absent for legacy data and fall back to
    ``updated``; a non-empty invalid value remains ineligible.
    """
    completed_at = item.get("completed_at")
    if completed_at is None or (
        isinstance(completed_at, str) and not completed_at.strip()
    ):
        completed_at = item.get("updated")
    return _normalize_done_stamp(completed_at)


def _done_selection(items: list[BacklogItem]) -> list[BacklogItem]:
    """Return the five newest eligible done items with deterministic ties."""
    candidates = [
        (item, stamp)
        for item in items
        if item.get("status") == "done"
        and (stamp := _done_selection_stamp(item)) is not None
    ]
    candidates.sort(key=lambda pair: pair[0]["id"])
    candidates.sort(key=lambda pair: pair[1], reverse=True)
    return [item for item, _stamp in candidates[:DONE_MAX_ITEMS]]


def _use_color(out: TextIO) -> bool:
    """Return whether ANSI color codes should be written to ``out``."""
    return hasattr(out, "isatty") and out.isatty()


def _colorize(text: str, color_code: str, enabled: bool) -> str:
    """Wrap ``text`` in an ANSI color code, or return it unchanged.

    Args:
        text: The text to colorize.
        color_code: An ANSI escape sequence (e.g. from :data:`_COLORS`).
        enabled: Whether coloring is active (typically from
            :func:`_use_color`).
    """
    return f"{color_code}{text}{_RESET}" if enabled else text


def validate_slug(slug: str, context: str = "") -> str | None:
    """Validate a candidate item slug.

    Args:
        slug: The candidate slug.
        context: Optional command name to prefix onto the error message.

    Returns:
        ``None`` if ``slug`` is valid, otherwise a human-readable error
        message describing which rule it violates.
    """
    prefix = f"[{context}] " if context else ""
    if slug in RESERVED_SLUGS:
        return f"{prefix}slug '{slug}' is a reserved word"
    if not SLUG_RE.match(slug):
        return (
            f"{prefix}invalid slug '{slug}' — must match "
            r"^[a-z0-9]+(-[a-z0-9]+)+$ (lowercase, hyphen-separated segments)"
        )
    if not (SLUG_MIN <= len(slug) <= SLUG_MAX):
        return f"{prefix}slug '{slug}' length {len(slug)} out of range [{SLUG_MIN},{SLUG_MAX}]"
    return None


def _parse_json_arg(raw: str, context: str) -> dict[str, object]:
    """Parse a CLI argument as a JSON object.

    Args:
        raw: The raw argument text.
        context: Command name to prefix onto any error message.

    Returns:
        The decoded JSON object.

    Raises:
        SystemExit: If ``raw`` isn't valid JSON. Exits with status 1 after
            printing to stderr.
    """
    try:
        return cast(dict[str, object], json.loads(raw))
    except json.JSONDecodeError as e:
        print(f"[{context}] invalid JSON: {e}", file=sys.stderr)
        sys.exit(1)


def _str_field(patch: dict[str, object], key: str, default: str = "") -> str:
    """Extract a string field from a JSON patch.

    Treats a missing key and an explicit JSON ``null`` identically, both
    collapsing to ``default``. Without this, code like
    ``cast(str, patch.get("summary", "")).strip()`` crashes with
    ``AttributeError`` on an explicit ``summary: null`` — the default only
    applies when the key is absent, not when it's present with value
    ``None`` — and every ``required`` check built on that pattern is
    bypassed the same way.

    Args:
        patch: The decoded JSON patch.
        key: The field to extract.
        default: Value to use when the field is missing or ``null``.
    """
    value = patch.get(key)
    return default if value is None else str(value)


def _list_field(patch: dict[str, object], key: str) -> list[object]:
    """Extract a list field from a JSON patch, treating null/missing as ``[]``.

    Without this, an explicit ``null`` for e.g. ``blocked_by`` passes
    ``.get(key, [])``'s default straight through as ``None`` (the default
    only applies when the key is absent), and the next ``for dep in
    blocked_by`` crashes with ``TypeError: 'NoneType' object is not
    iterable``.

    A non-null, non-list value (e.g. a string) is rejected with a
    :class:`SystemExit`. Previously it was passed through via ``cast`` and
    the next iteration walked the string one character at a time, corrupting
    stored state silently on update.
    """
    value = patch.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        print(
            f"field '{key}' must be a list, got {type(value).__name__}",
            file=sys.stderr,
        )
        sys.exit(1)
    return cast(list[object], value)


def _dict_field(patch: dict[str, object], key: str) -> dict[str, object]:
    """Extract a dict field from a JSON patch, treating null/missing as ``{}``."""
    value = patch.get(key)
    return {} if value is None else cast(dict[str, object], value)


def _reject_null_fields(
    cmd: str, patch: dict[str, object], fields: tuple[str, ...]
) -> None:
    """Refuse a patch that sets any of ``fields`` to explicit JSON ``null``.

    Used before a raw ``dict.update(patch)`` merge (``update``, ``pending
    update``) for fields that have no "unset" state in the schema — unlike
    ``priority``, they're always present with a meaningful default, never
    absent — so null has no defined meaning there. Without this check, the
    merge writes the null straight into a supposedly-never-null field:
    some call sites crash on the next read (e.g. ``blocked_by: null``
    breaks the next ``for dep in blocked_by``), others just quietly
    corrupt the stored record with a value that violates the schema until
    it's read again.

    Args:
        cmd: Command name to prefix onto the error message.
        patch: The JSON patch about to be merged.
        fields: Field names that must not be explicit null in ``patch``.

    Raises:
        SystemExit: If any of ``fields`` is present in ``patch`` with
            value ``None``. Exits with status 1 after printing to stderr.
    """
    nulled = sorted(f for f in fields if f in patch and patch[f] is None)
    if nulled:
        print(
            f"[{cmd}] field(s) cannot be null: {', '.join(nulled)} "
            "— omit the field to leave it unchanged",
            file=sys.stderr,
        )
        sys.exit(1)


# ── I/O ───────────────────────────────────────────────────────────────────────


def load_items() -> list[BacklogItem]:
    """Load all backlog items from :data:`ITEMS_FILE`.

    Returns:
        The stored items, or ``[]`` if the file doesn't exist yet.

    Raises:
        SystemExit: If the file contains invalid JSON or an unrecognized
            schema version. The process exits with status 1 after printing
            a diagnostic to stderr.
    """
    if not ITEMS_FILE.exists():
        return []
    try:
        data = json.loads(ITEMS_FILE.read_text())
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
        print(
            f"backlog file corrupted at {ITEMS_FILE}; restore from backup. ({e})",
            file=sys.stderr,
        )
        sys.exit(1)
    if not isinstance(data, dict) or data.get("schema_version") != 2:
        print(
            f"backlog file at {ITEMS_FILE} is not schema_version 2; "
            "check file or run migration.",
            file=sys.stderr,
        )
        sys.exit(1)
    return cast(list[BacklogItem], data.get("items", []))


def _atomic_write_json(path: Path, payload: str, prefix: str) -> None:
    """Write text to ``path`` via a temp file in its directory + ``os.replace``.

    Shared by every writer of the backlog data files (and, despite the
    name, any other text this module writes atomically — the payload need
    not be JSON). Cleans up the temp file on failure so a crash mid-write
    never leaves debris behind or corrupts the destination.

    Ensures the temp file is fsynced before rename and the containing
    directory is fsynced after the rename so the replace is durable.

    Args:
        path: Destination file path.
        payload: Already-serialized text to write.
        prefix: Prefix for the temporary file created alongside ``path``.
    """
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=prefix)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())

        os.replace(tmp_path, path)

        dir_fd = None
        try:
            dir_fd = os.open(
                str(directory), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            os.fsync(dir_fd)
        finally:
            if dir_fd is not None:
                os.close(dir_fd)
    except Exception:
        with suppress(OSError):
            os.unlink(tmp_path)
        raise


def save_items(items: list[BacklogItem]) -> None:
    """Atomically persist ``items`` to :data:`ITEMS_FILE`."""
    payload = json.dumps({"schema_version": 2, "items": items}, indent=2)
    _atomic_write_json(ITEMS_FILE, payload, ".items_tmp_")


def load_pending() -> list[PendingItem]:
    """Load all pending items from :data:`PENDING_FILE`.

    Returns:
        The stored pending items, or ``[]`` if the file doesn't exist yet.

    Raises:
        SystemExit: If the file contains invalid JSON or an unrecognized
            schema version. The process exits with status 1 after printing
            a diagnostic to stderr.
    """
    if not PENDING_FILE.exists():
        return []
    try:
        data = json.loads(PENDING_FILE.read_text())
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
        print(
            f"pending-items file corrupted at {PENDING_FILE}; restore from backup. ({e})",
            file=sys.stderr,
        )
        sys.exit(1)
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        print(
            f"pending-items file at {PENDING_FILE} is not schema_version 1; "
            "check file or run migration.",
            file=sys.stderr,
        )
        sys.exit(1)
    return cast(list[PendingItem], data.get("items", []))


def save_pending(pending_items: list[PendingItem]) -> None:
    """Atomically persist ``pending_items`` to :data:`PENDING_FILE`."""
    payload = json.dumps({"schema_version": 1, "items": pending_items}, indent=2)
    _atomic_write_json(PENDING_FILE, payload, ".pending_tmp_")


# flock(2) locks are tied to the open file description, not the process, so a
# naive nested ``with backlog_lock()`` deadlocks: the inner ``open`` gets a
# distinct descriptor and its ``flock`` blocks forever waiting on the outer
# lock held by the same process. A ``threading.RLock`` makes same-thread
# re-entry a no-op (no second ``flock``) while still blocking other threads,
# and the single ``flock`` taken on the outermost entry still serializes
# distinct *processes* as before.
_backlog_lock_rlock = threading.RLock()
_backlog_lock_fd: int = -1
_backlog_lock_count: int = 0


@contextmanager
def backlog_lock() -> Iterator[None]:
    """Hold an exclusive lock over a mutating command's full read-modify-write cycle.

    Held across items.json + pending_items.json + _meta.json so two
    concurrent writers (e.g. Claude Code and opencode sharing this store)
    serialize instead of racing.

    Safe to re-enter from the same thread (a nested ``with`` does not
    deadlock): the underlying ``flock`` is taken once on the outermost entry
    and released on the innermost exit. Distinct threads/processes still
    serialize behind the lock.
    """
    global _backlog_lock_fd, _backlog_lock_count
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _backlog_lock_rlock:
        _backlog_lock_count += 1
        if _backlog_lock_count == 1:
            _backlog_lock_fd = os.open(str(LOCK_FILE), os.O_WRONLY | os.O_CREAT, 0o644)
            fcntl.flock(_backlog_lock_fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            _backlog_lock_count -= 1
            if _backlog_lock_count == 0:
                fcntl.flock(_backlog_lock_fd, fcntl.LOCK_UN)
                os.close(_backlog_lock_fd)
                _backlog_lock_fd = -1


_out_of_scope_lock_rlock = threading.RLock()
_out_of_scope_lock_fd: int = -1
_out_of_scope_lock_count: int = 0


@contextmanager
def out_of_scope_lock() -> Iterator[None]:
    """Hold an exclusive lock over an out-of-scope command's read-modify-write cycle.

    Mirrors :func:`backlog_lock` exactly (same reentrant-per-thread,
    serialize-across-processes shape) but over its own lock file, scoped to
    ``~/.claude/data/backlog-out-of-scope/`` -- unrelated to
    ``LOCK_FILE``/``backlog_lock()``, which guards the separate
    items.json/pending_items.json store.
    """
    global _out_of_scope_lock_fd, _out_of_scope_lock_count
    OUT_OF_SCOPE_DIR.mkdir(parents=True, exist_ok=True)
    with _out_of_scope_lock_rlock:
        _out_of_scope_lock_count += 1
        if _out_of_scope_lock_count == 1:
            _out_of_scope_lock_fd = os.open(
                str(OUT_OF_SCOPE_LOCK_FILE), os.O_WRONLY | os.O_CREAT, 0o644
            )
            fcntl.flock(_out_of_scope_lock_fd, fcntl.LOCK_EX)
        try:
            yield
        finally:
            _out_of_scope_lock_count -= 1
            if _out_of_scope_lock_count == 0:
                fcntl.flock(_out_of_scope_lock_fd, fcntl.LOCK_UN)
                os.close(_out_of_scope_lock_fd)
                _out_of_scope_lock_fd = -1


def load_rev() -> int:
    """Read the current revision counter.

    Returns:
        The stored revision, or ``0`` if :data:`META_FILE` is missing or
        unreadable (lazy auto-init, no migration needed). A structurally
        valid non-dict JSON value (e.g. ``[]``) or a non-int ``rev`` field
        also falls back to ``0`` — the previous narrow ``JSONDecodeError``
        catch alone would let a non-dict traceback with
        ``AttributeError`` on the ``.get`` and a non-int rev silently brick
        every numeric ``--if-rev`` mutation.
    """
    if not META_FILE.exists():
        return 0
    try:
        data = json.loads(META_FILE.read_text())
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return 0
    if not isinstance(data, dict):
        return 0
    rev = data.get("rev", 0)
    if not isinstance(rev, int) or isinstance(rev, bool):
        return 0
    return rev


def bump_rev() -> int:
    """Increment and persist the revision counter.

    Must be called while holding :func:`backlog_lock`.

    Returns:
        The new revision value.
    """
    rev = load_rev() + 1
    payload = json.dumps({"rev": rev})
    _atomic_write_json(META_FILE, payload, ".meta_tmp_")
    return rev


def _backup_before_bulk_delete(path: Path) -> None:
    """Snapshot a data file before a filter-based bulk deletion.

    Covers the one class of mutation that isn't trivially reversible by
    re-running a single command: records removed by a computed filter
    rather than by explicit id.

    Args:
        path: The data file to snapshot. No-op if it doesn't exist.
    """
    if not path.exists():
        return
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    backup_path = path.with_name(f"{path.stem}.bak-{stamp}{path.suffix}")
    backup_path.write_bytes(path.read_bytes())


# ── graph helpers ─────────────────────────────────────────────────────────────


def build_index(items: list[BacklogItem]) -> BacklogIndex:
    """Build a slug → item lookup for ``items``."""
    return {i["id"]: i for i in items}


def effective_blockers(item: BacklogItem, index: BacklogIndex) -> list[str]:
    """Return ``item``'s ``blocked_by`` slugs whose referent isn't done.

    A blocker slug that no longer resolves in ``index`` still counts as
    unresolved — it's returned as-is. ``index`` is a *backlog* index, so
    this fallback is also what makes a pending-item blocker (``blocked_by``
    can hold a pending slug — see :func:`cmd_block`) correctly keep an item
    out of READY, with no separate pending-aware branch needed here.
    Otherwise (a genuinely dangling slug) this only fires for legacy or
    hand-edited data now that :func:`cmd_remove`/:func:`cmd_prune` purge
    inbound ``blocked_by`` references on delete.

    A stored ``blocked_by`` that isn't a list (legacy corruption — a
    string value would previously be iterated character-by-character) is
    coerced to ``[]`` with a stderr warning so :func:`render` doesn't
    walk a string one character at a time and emit a slot per character.

    Args:
        item: The item to check.
        index: Slug → item lookup, as built by :func:`build_index`.
    """
    bb = item.get("blocked_by", [])
    if not isinstance(bb, list):
        print(
            f"[effective_blockers] {item.get('id', '?')}.blocked_by is "
            f"{type(bb).__name__}, not list — coercing to []",
            file=sys.stderr,
        )
        return []
    result = []
    for s in bb:
        dep = index.get(s)
        if dep is None or dep.get("status") != "done":
            result.append(s)
    return result


def detect_cycle(start: str, new_dep: str, index: BacklogIndex) -> bool:
    """Check whether adding ``new_dep`` as a blocker of ``start`` would cycle.

    Args:
        start: Slug of the item that would gain ``new_dep`` as a blocker.
        new_dep: Slug of the proposed blocker.
        index: Slug → item lookup, as built by :func:`build_index`.

    Returns:
        ``True`` if ``new_dep`` (transitively, via its own blockers)
        already depends on ``start``.
    """
    visited: set[str] = set()
    stack = [new_dep]
    while stack:
        node = stack.pop()
        if node == start:
            return True
        if node in visited:
            continue
        visited.add(node)
        dep = index.get(node)
        if dep:
            stack.extend(dep.get("blocked_by", []))
    return False


def _purge_inbound_refs(
    removed_slugs: set[str],
    items: list[BacklogItem],
    pending_items: list[PendingItem],
) -> None:
    """Strip every reference to ``removed_slugs`` from surviving records.

    Purges ``removed_slugs`` from backlog items' ``blocked_by`` lists and
    pending items' ``blocking`` lists. Without this, a deleted blocker
    would remain in its dependents' ``blocked_by`` and
    :func:`effective_blockers` would treat the missing slug as unresolved,
    retroactively flipping dependents from READY into BLOCKED.

    Mutates the passed records in place.
    """
    if not removed_slugs:
        return
    for item in items:
        if item.get("id") in removed_slugs:
            continue
        bb = item.get("blocked_by") or []
        if any(s in removed_slugs for s in bb):
            item["blocked_by"] = [s for s in bb if s not in removed_slugs]
    for p in pending_items:
        if p.get("id") in removed_slugs:
            continue
        blocking = p.get("blocking") or []
        if any(s in removed_slugs for s in blocking):
            p["blocking"] = [s for s in blocking if s not in removed_slugs]


# ── render order ──────────────────────────────────────────────────────────────


def _priority_rank(item: BacklogItem) -> int:
    """Sort rank for ``item``'s priority; lower sorts first.

    Absence and any unrecognized value both collapse to ``"normal"``'s rank.
    """
    return _PRIORITY_RANK.get(item.get("priority", "normal"), 1)


def _priority_glyph(item: BacklogItem, color: bool) -> str:
    """Render the leading 2-char priority gutter for one dashboard line.

    Bold-red up-triangle for high, dim down-triangle for low, dim middle
    dot for normal/absent — keeps every line's tag column aligned and
    gives every row a mark instead of a blank hole.
    """
    p = item.get("priority")
    if p == "high":
        return _colorize("▲", _COLORS["prio_high"], color) + " "
    if p == "low":
        return _colorize("▽", _COLORS["prio_low"], color) + " "
    return _colorize("·", _COLORS["prio_low"], color) + " "


def _section_top(title: str, width: int = SECTION_WIDTH) -> str:
    """Render a section's top border with an embedded title."""
    prefix = f"┌─ {title} "
    fill = max(width - len(prefix), 3)
    return prefix + ("─" * fill)


def _section_bottom(width: int = SECTION_WIDTH) -> str:
    """Render a section's bottom border."""
    return "└" + ("─" * (width - 1))


def _ellipsize(text: str, limit: int) -> str:
    """Truncate ``text`` to ``limit`` display chars with a trailing ``…``."""
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 1)] + "…"


def _project_prefix(slug: str) -> str:
    """Extract canonical project prefix from a backlog item slug.

    Matches against :data:`KNOWN_PROJECT_PREFIXES` first (longest match),
    falling back to the first hyphen-separated segment if a hyphen exists,
    or the empty string for un-prefixed slugs.
    """
    for prefix in KNOWN_PROJECT_PREFIXES:
        if slug == prefix or slug.startswith(f"{prefix}-"):
            return prefix
    if "-" in slug:
        return slug.split("-", 1)[0]
    return ""


def _project_divider(
    project: str,
    count: int,
    width: int = SECTION_WIDTH,
    color: bool = False,
) -> str:
    """Render a horizontal divider row for a project group within a section."""
    unit = "item" if count == 1 else "items"
    label = f"─── {project} ({count} {unit}) "
    fill = max(width - len(f"│  {label}"), 3)
    line = f"│  {label}" + ("─" * fill)
    return _colorize(line, _COLORS["dim"], color) if color else line


def _render_order(items: list[BacklogItem]) -> RenderOrder:
    """Bucket and sort backlog items into dashboard render order.

    Returns:
        A 5-tuple of ``(in_progress, ready, blocked, in_review, done)``,
        where ``done`` contains at most :data:`DONE_MAX_ITEMS` eligible done
        items, sorted most-recently-completed first. Completion ordering uses
        normalized ``completed_at`` values, falling back to ``updated`` for
        legacy items, with ascending id ties. The dashboard omits the DONE
        section entirely when this is empty.

    Note:
        DONE-section membership changes only when the backlog data changes,
        aside from malformed data being repaired outside this renderer.
    """
    index = build_index(items)

    in_progress = sorted(
        [i for i in items if i.get("status") == "in-progress"],
        key=lambda i: i.get("updated", ""),
        reverse=True,
    )
    in_progress = sorted(in_progress, key=_priority_rank)  # stable
    open_items = [i for i in items if i.get("status") == "open"]
    ready = sorted(
        [i for i in open_items if not effective_blockers(i, index)],
        key=lambda i: i.get("created", ""),
    )
    ready = sorted(ready, key=_priority_rank)  # stable
    ready = sorted(ready, key=lambda i: _project_prefix(i.get("id", "")))  # stable
    blocked = sorted(
        [i for i in open_items if effective_blockers(i, index)],
        key=lambda i: (len(effective_blockers(i, index)), i.get("updated", "")),
    )
    blocked = sorted(blocked, key=_priority_rank)  # stable
    blocked = sorted(blocked, key=lambda i: _project_prefix(i.get("id", "")))  # stable
    in_review = sorted(
        [i for i in items if i.get("status") == "in-review"],
        key=lambda i: i.get("updated", ""),
    )
    in_review = sorted(in_review, key=_priority_rank)  # stable
    done = _done_selection(items)

    return in_progress, ready, blocked, in_review, done


def _blocker_check_reminder(
    items: list[BacklogItem],
    exclude_slug: str | None,
    *,
    cmd: str,
    err: TextIO | None = None,
    quiet: bool = False,
) -> None:
    """Print a one-line stderr reminder to check for blocker relationships.

    Fires after a successful ``add``/``pending add`` when other READY or
    IN PROGRESS items exist — ``render()`` already printed the list above
    this, so this only adds the imperative, not a re-print. No matching
    heuristic; the caller (human or agent) makes the judgment call.

    Args:
        items: The current backlog items.
        exclude_slug: Slug to omit from the candidate count (typically the
            item that was just added), or ``None`` if nothing to exclude.
        cmd: Command name to prefix onto the reminder.
        err: Stream to print to; defaults to ``sys.stderr``.
        quiet: Suppress the reminder (combined with :func:`_agent_quiet`).
    """
    if err is None:
        err = sys.stderr
    in_progress, ready, _, _, _ = _render_order(items)
    candidates = [i for i in in_progress + ready if i["id"] != exclude_slug]
    if not candidates:
        return
    cli_common.qprint(
        f"[{cmd}] check the READY/IN PROGRESS items above for blocker relationships",
        quiet=(quiet or _agent_quiet()),
        file=err,
    )


def _load_out_of_scope_index() -> dict[str, dict[str, object]]:
    """Load the out-of-scope concept index, or ``{}`` if it doesn't exist yet."""
    if not OUT_OF_SCOPE_INDEX_FILE.exists():
        return {}
    return cast(
        dict[str, dict[str, object]], json.loads(OUT_OF_SCOPE_INDEX_FILE.read_text())
    )


def _save_out_of_scope_index(index: dict[str, dict[str, object]]) -> None:
    """Atomically persist the out-of-scope concept index."""
    payload = json.dumps(index, indent=2)
    _atomic_write_json(OUT_OF_SCOPE_INDEX_FILE, payload, ".oos_index_tmp_")


def _out_of_scope_md_path(slug: str) -> Path:
    """Path to a concept's freeform-reason markdown file."""
    return OUT_OF_SCOPE_DIR / f"{slug}.md"


def _out_of_scope_check_reminder(
    cmd: str, *, err: TextIO | None = None, quiet: bool = False
) -> None:
    """Print a one-line stderr reminder to check the out-of-scope knowledge base.

    Fires after a successful ``add``/``pending add`` when at least one
    rejected concept is on file. No matching heuristic, same as
    :func:`_blocker_check_reminder` -- the caller (human or agent) makes the
    judgment call.

    Args:
        cmd: Command name to prefix onto the reminder.
        err: Stream to print to; defaults to ``sys.stderr``.
        quiet: Suppress the reminder (combined with :func:`_agent_quiet`).
    """
    if err is None:
        err = sys.stderr
    index = _load_out_of_scope_index()
    if not index:
        return
    cli_common.qprint(
        f"[{cmd}] {len(index)} rejected concept(s) on file — check "
        "~/.claude/data/backlog-out-of-scope/ (or 'out-of-scope list') for "
        "a match before proceeding",
        quiet=(quiet or _agent_quiet()),
        file=err,
    )


def _pending_render_order(pending_items: list[PendingItem]) -> list[PendingItem]:
    """Order unresolved pending items: reply_received group first, each newest-first."""
    unresolved = [p for p in pending_items if p.get("status") != "resolved"]
    by_recency = sorted(unresolved, key=lambda p: p.get("updated", ""), reverse=True)
    return sorted(
        by_recency, key=lambda p: 0 if p.get("status") == "reply_received" else 1
    )


# ── number resolution ─────────────────────────────────────────────────────────


def _concat_order(
    pending_ordered: list[PendingItem], buckets: RenderOrder
) -> list[BacklogItem | PendingItem]:
    """Concatenate pending + all five backlog buckets into one flat render order."""
    in_progress, ready, blocked, in_review, done = buckets
    return [*pending_ordered, *in_progress, *ready, *blocked, *in_review, *done]


def _unified_order(
    items: list[BacklogItem], pending_items: list[PendingItem]
) -> list[BacklogItem | PendingItem]:
    """Return the full cross-section render order: pending first, then backlog."""
    return _concat_order(_pending_render_order(pending_items), _render_order(items))


def resolve_id(
    arg: str, items: list[BacklogItem], pending_items: list[PendingItem]
) -> tuple[str, str]:
    """Resolve a display number or slug to a ``(kind, slug)`` pair.

    Args:
        arg: A 1-based display number (as printed by ``render``) or a slug.
        items: The current backlog items.
        pending_items: The current pending items.

    Returns:
        ``(kind, slug)`` where ``kind`` is ``"backlog"`` or ``"pending"``.
        For a non-numeric ``arg``, ``kind`` is inferred by checking which
        pool the slug belongs to.

    Raises:
        SystemExit: If ``arg`` is numeric but out of range for the current
            render order, or if a non-numeric ``arg`` matches no item in
            either pool. Exits with status 1 after printing to stderr.
    """
    try:
        n = int(arg)
    except ValueError:
        pending_ids = {p["id"] for p in pending_items}
        if arg in pending_ids:
            return "pending", arg
        backlog_ids = {i["id"] for i in items}
        if arg in backlog_ids:
            return "backlog", arg
        # Unknown slug — surface not-found here, before require_kind gets
        # a chance to mis-resolve it as "wrong kind" (the previous
        # `("backlog", arg)` default made `pending update typo-slug ...`
        # claim the slug was "a backlog item" when no such item existed).
        print(f"[resolve] not found: {arg}", file=sys.stderr)
        sys.exit(1)

    ordered = _unified_order(items, pending_items)
    if not (1 <= n <= len(ordered)):
        print(f"[resolve] no item at position {n}", file=sys.stderr)
        sys.exit(1)
    resolved = ordered[n - 1]
    pending_ids = {p["id"] for p in pending_items}
    kind = "pending" if resolved["id"] in pending_ids else "backlog"
    return kind, resolved["id"]


def require_kind(cmd: str, arg: str, kind: str, expected: str) -> None:
    """Exit with a helpful message if ``kind`` doesn't match ``expected``.

    Args:
        cmd: Command name to prefix onto the error message.
        arg: The original id argument, echoed back to the user.
        kind: The kind actually resolved (``"backlog"`` or ``"pending"``).
        expected: The kind this command requires.

    Raises:
        SystemExit: If ``kind != expected``. Exits with status 1.
    """
    if kind != expected:
        other = (
            "pending update/list"
            if expected == "backlog"
            else "update/start/done/block/unblock"
        )
        print(
            f"[{cmd}] position {arg} is a {kind} item — use '{other}' instead",
            file=sys.stderr,
        )
        sys.exit(1)


def enforce_rev_guard(
    cmd: str,
    id_arg: str,
    if_rev_arg: int | None,
    current_rev: int,
    items: list[BacklogItem],
    pending_items: list[PendingItem],
) -> None:
    """Refuse a numeric-id mutation that lacks a fresh ``--if-rev``.

    Slug id args are exempt — slug identity never goes stale, only a
    numeric position can point at the wrong item after a concurrent
    change.

    Args:
        cmd: Command name to prefix onto any error message.
        id_arg: The raw id argument as given on the command line.
        if_rev_arg: The ``--if-rev`` value supplied, or ``None``.
        current_rev: The revision currently on disk.
        items: The current backlog items (used to re-render on refusal).
        pending_items: The current pending items (used to re-render on
            refusal).

    Raises:
        SystemExit: If ``id_arg`` is numeric and ``if_rev_arg`` is missing
            or doesn't match ``current_rev``. Exits with status 1 after
            re-rendering the dashboard so the caller can retry with a
            fresh revision.
    """
    try:
        int(id_arg)
    except (ValueError, TypeError):
        return  # slug-based call, no guard needed

    if if_rev_arg is None:
        print(
            f"[{cmd}] numeric id '{id_arg}' requires --if-rev <N> to guard "
            f"against a stale position — refusing (no write).",
            file=sys.stderr,
        )
        print(
            f"[{cmd}] current rev is {current_rev}. Re-confirm your target "
            f"below, then retry with --if-rev {current_rev}.",
            file=sys.stderr,
        )
        render(items, pending_items, rev=current_rev)
        sys.exit(1)

    if if_rev_arg != current_rev:
        print(
            f"[{cmd}] stale rev: --if-rev {if_rev_arg} given, current is "
            f"{current_rev} — the backlog changed since you last read it. "
            f"Refusing (no write).",
            file=sys.stderr,
        )
        render(items, pending_items, rev=current_rev)
        sys.exit(1)


# ── render ────────────────────────────────────────────────────────────────────


def _pending_suffix(item: dict[str, object], color: bool) -> str:
    """Render a pending item's line suffix: reply marker and waiting-age."""
    marker = ""
    if item.get("status") == "reply_received":
        marker = " " + _colorize("reply received", _COLORS["pending"], color)
    age = _age_days(cast(str, item.get("created", "")))
    since = f" (waiting {age}d)" if age is not None else ""
    return marker + since


def _gate_suffix(item: dict[str, object], color: bool) -> str:
    """Render a trailing marker for an item whose gate blocks completion.

    Without this, an item refused by :func:`_gate_block_message` looks
    identical to any other item on the dashboard until someone actually
    runs ``approve``/``done`` and hits the refusal.
    """
    gate = cast(dict[str, object] | None, item.get("gate"))
    if not _gate_blocks(gate):
        return ""
    return " " + _colorize("\U0001f512 gate", _COLORS["warn"], color)


def _in_progress_suffix(item: dict[str, object], color: bool) -> str:
    """Render gate suffix and claimed harness tag for in-progress items."""
    res = _gate_suffix(item, color)
    claim = cast(dict[str, object] | None, item.get("claimed_by"))
    if isinstance(claim, dict):
        harness = claim.get("harness")
        if harness and str(harness).strip():
            res += f" [{str(harness).strip()}]"
    return res


def render(
    items: list[BacklogItem] | None = None,
    pending_items: list[PendingItem] | None = None,
    *,
    out: TextIO | None = None,
    err: TextIO | None = None,
    rev: int | None = None,
    dispatch: bool = False,
) -> None:
    """Render the full dashboard: pending items, then the five backlog sections.

    Pure render — no writes, no other side effects — with one exception:
    when ``dispatch`` is true, a recap-regen child may be spawned (fully
    detached; never blocks) after everything has printed. Loads current
    state from disk for any of ``items``/``pending_items``/``rev`` left as
    ``None``, so callers that already hold the data in memory (e.g. inside
    a lock) can pass it through instead of re-reading.

    Args:
        items: Backlog items to render, or ``None`` to load from disk.
        pending_items: Pending items to render, or ``None`` to load from
            disk.
        out: Stream for the dashboard body; defaults to ``sys.stdout``.
        err: Stream for the trailing ``item-map:`` line; defaults to
            ``sys.stderr``.
        rev: Revision to report in the ``item-map:`` line, or ``None`` to
            load the current one from disk.
        dispatch: Whether to check for and spawn a detached recap-regen
            child after rendering. Callers still holding :func:`backlog_lock`
            must leave this ``False`` (the default) and run the check
            themselves after releasing it — see :func:`_maybe_dispatch_recap_regen`.
    """
    if out is None:
        out = sys.stdout
    if err is None:
        err = sys.stderr

    if items is None:
        items = load_items()
    if pending_items is None:
        pending_items = load_pending()
    if rev is None:
        rev = load_rev()

    index = build_index(items)
    pending_index = {p["id"]: p for p in pending_items}
    buckets = _render_order(items)
    in_progress, ready, blocked, in_review, done = buckets
    current_fingerprint = _board_fingerprint(items, pending_items)
    pending_ordered = _pending_render_order(pending_items)
    ordered: list[BacklogItem | PendingItem] = _concat_order(pending_ordered, buckets)

    if not ordered:
        print("(backlog is empty)", file=out)
        for line in _recap_section_lines(_use_color(out), current_fingerprint) or []:
            print(line, file=out)
        if not _agent_quiet():
            print(f"item-map: rev={rev}", file=err)
        if dispatch:
            _maybe_dispatch_recap_regen()
        return

    color = _use_color(out)
    pending_id_set = {p["id"] for p in pending_items}

    # Pre-assign all numbers so blocked-by annotations can reference any item
    slug_to_num = {item["id"]: n + 1 for n, item in enumerate(ordered)}
    item_map = {
        n + 1: (
            f"pending:{item['id']}"
            if item["id"] in pending_id_set
            else f"backlog:{item['id']}"
        )
        for n, item in enumerate(ordered)
    }

    sections: list[list[str]] = []

    def add_section(
        title: str,
        section_items: Sequence[BacklogItem | PendingItem],
        show_blockers: bool = False,
        show_age: bool = False,
        color_code: str | None = None,
        summary_key: str = "summary",
        show_category: bool = True,
        line_suffix: Callable[[dict[str, object], bool], str] | None = None,
        show_priority: bool = False,
        group_by_project: bool = False,
    ) -> None:
        """Append one rendered section (with its border) to ``sections``."""
        if not section_items:
            return
        frame_code = _COLORS.get(color_code) if color_code else None
        top = (
            _colorize(_section_top(title), frame_code, color)
            if frame_code
            else _section_top(title)
        )
        bottom = (
            _colorize(_section_bottom(), frame_code, color)
            if frame_code
            else _section_bottom()
        )
        lines = [top]
        current_project: str | None = None
        project_counts: dict[str, int] = {}
        if group_by_project:
            for item in section_items:
                p = _project_prefix(item["id"])
                project_counts[p] = project_counts.get(p, 0) + 1

        for item in section_items:
            if group_by_project:
                proj = _project_prefix(item["id"])
                if proj != current_project:
                    current_project = proj
                    if proj:
                        lines.append(
                            _project_divider(proj, project_counts[proj], color=color)
                        )

            item_d = cast(dict[str, object], item)
            n = slug_to_num[item["id"]]
            badge = (
                _priority_glyph(cast(BacklogItem, item), color) if show_priority else ""
            )
            tag = (
                _category_tag(cast(str, item_d.get("category", "")))
                if show_category
                else ""
            )
            line = f"│  {n:2}  {badge}{tag}{item_d.get(summary_key, '')}"
            if line_suffix:
                line += line_suffix(item_d, color)
            if show_age:
                age = _age_days(cast(str, item_d.get("updated", "")))
                if age is not None:
                    line += f" · {age}d"
                    if age > STALE_DAYS:
                        line += " " + _colorize("⚠️", _COLORS["warn"], color)
            lines.append(line)
            if show_blockers:
                eff = effective_blockers(cast(BacklogItem, item), index)
                if eff:
                    parts = []
                    for i, slug in enumerate(eff):
                        dep = index.get(slug)
                        dep_summary_key = "summary"
                        if dep is None:
                            dep = pending_index.get(slug)
                            dep_summary_key = "description"
                        dep_n = slug_to_num.get(slug)
                        ref = f"#{dep_n}" if dep_n else slug
                        if i == 0 and dep:
                            hint = dep.get(dep_summary_key, "")[:55]
                            parts.append(f"{ref} ({hint})")
                        else:
                            parts.append(ref)
                    lines.append(f"│      ↳ blocked by: {', '.join(parts)}")
        lines.append(bottom)
        sections.append(lines)

    add_section(
        "PENDING",
        pending_ordered,
        color_code="pending",
        summary_key="description",
        show_category=False,
        line_suffix=_pending_suffix,
    )
    add_section(
        "IN PROGRESS",
        in_progress,
        show_blockers=True,
        show_age=True,
        show_priority=True,
        color_code="in_progress",
        line_suffix=_in_progress_suffix,
    )
    add_section(
        "READY",
        ready,
        show_priority=True,
        color_code="ready",
        line_suffix=_gate_suffix,
        group_by_project=True,
    )
    add_section(
        "BLOCKED",
        blocked,
        show_blockers=True,
        show_age=True,
        show_priority=True,
        color_code="blocked",
        line_suffix=_gate_suffix,
        group_by_project=True,
    )
    add_section(
        "IN REVIEW",
        in_review,
        show_age=True,
        show_priority=True,
        color_code="in_review",
        line_suffix=_gate_suffix,
    )
    add_section("DONE", done, color_code="done")

    recap_lines = _recap_section_lines(color, current_fingerprint)
    if recap_lines:
        sections.append(recap_lines)

    for i, section_lines in enumerate(sections):
        if i > 0:
            print(file=out)
        for line in section_lines:
            print(line, file=out)

    if not _agent_quiet():
        map_str = ",".join(f"{n}={tag}" for n, tag in item_map.items())
        print(f"item-map: rev={rev} {map_str}", file=err)

    if dispatch:
        _maybe_dispatch_recap_regen()


# ── recap: event journal ────────────────────────────────────────────────────


def _journal_entry(
    cmd: str,
    kind: str,
    rev: int,
    *,
    slug: str | None = None,
    summary: str | None = None,
    from_status: str | None = None,
    to_status: str | None = None,
    fields: list[str] | None = None,
    feedback: str | None = None,
    count: int | None = None,
) -> dict[str, object]:
    """Build one journal entry: a fixed envelope plus structured optionals.

    Optional fields are omitted entirely (not written as ``null``) when not
    given, keeping entries compact and letting :func:`_render_changelog`
    branch on plain ``dict.get`` presence checks.
    """
    entry: dict[str, object] = {
        "ts": datetime.now(UTC).isoformat(),
        "rev": rev,
        "machine": machine_id(),
        "cmd": cmd,
        "kind": kind,
    }
    if slug is not None:
        entry["slug"] = slug
    if summary is not None:
        entry["summary"] = summary
    if from_status is not None:
        entry["from_status"] = from_status
    if to_status is not None:
        entry["to_status"] = to_status
    if fields is not None:
        entry["fields"] = fields
    if feedback is not None:
        entry["feedback"] = feedback
    if count is not None:
        entry["count"] = count
    return entry


def append_journal_event(entry: dict[str, object], *, verbose: bool = False) -> None:
    """Append one event to the journal, best-effort.

    The JSON store (``items.json``/``pending_items.json``) is authoritative
    and has already been written by the time this is called — a failed
    journal append (disk full, permissions) must never fail or crash a
    mutation that already succeeded, so failures are swallowed with a
    one-line stderr warning instead of raised.
    """
    line = json.dumps(entry, sort_keys=True)
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(JOURNAL_FILE, "a") as f:
            f.write(line + "\n")
    except OSError as e:
        cli_common.vprint(
            f"[journal] append failed (non-fatal): {e}",
            verbose=verbose,
            file=sys.stderr,
        )


def _parse_journal_ts(raw: object) -> datetime | None:
    """Parse a journal entry's ``ts`` field into an aware UTC ``datetime``."""
    if not isinstance(raw, str):
        return None
    try:
        ts = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts


# ── run evidence (runs.jsonl sidecar) ──────────────────────────────────────


def load_runs(item: str | None = None) -> list[RunRecord]:
    """Load run-evidence rows from :data:`RUNS_FILE`, optionally for one item.

    Read without holding :func:`backlog_lock` — writers append single lines
    under the lock, but a concurrent append can still expose a trailing
    partial line on read. A ``JSONDecodeError`` on that last line is
    silently skipped (the writer just hasn't finished); the same failure on
    any earlier line is real corruption and surfaces as a stderr warning
    rather than being silently dropped (same policy as the journal reader).

    Args:
        item: Restrict to rows whose ``item`` field equals this slug, or
            ``None`` for every row.
    """
    if not RUNS_FILE.exists():
        return []
    try:
        raw_lines = RUNS_FILE.read_text().splitlines()
    except OSError:
        return []
    non_blank = [line for line in raw_lines if line.strip()]
    runs: list[RunRecord] = []
    for line_no, line in enumerate(non_blank, start=1):
        try:
            run = json.loads(line)
        except json.JSONDecodeError:
            if line_no == len(non_blank):
                break  # trailing partial line from a concurrent append
            print(
                f"runs file corrupted at {RUNS_FILE}; ignoring malformed "
                f"line {line_no}",
                file=sys.stderr,
            )
            continue
        if not isinstance(run, dict):
            continue
        if item is None or run.get("item") == item:
            runs.append(cast(RunRecord, run))
    return runs


def write_runs_file(runs: Sequence[RunRecord]) -> None:
    """Atomically rewrite :data:`RUNS_FILE` with ``runs`` (one JSON line each)."""
    payload = "".join(json.dumps(run, sort_keys=True) + "\n" for run in runs)
    _atomic_write_json(RUNS_FILE, payload, ".runs_tmp_")


def append_run_record(record: RunRecord) -> bool:
    """Append one run-evidence row to :data:`RUNS_FILE` (best-effort).

    Callers must hold :func:`backlog_lock` (appends are single-line
    O_APPEND writes, but the lock serializes concurrent harness sessions).
    The subprocess whose evidence this is has already run by the time this
    is called — a failed append (disk full, permissions) must never fail or
    crash the ``run`` command itself, so failures print a one-line stderr
    warning instead of raised. Unconditional (not verbose-gated): silently
    losing evidence would defeat the whole point of recording it. Returns
    whether the append actually landed, so ``cmd_run`` doesn't tell the
    caller evidence was recorded when it wasn't.
    """
    line = json.dumps(record, sort_keys=True)
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(RUNS_FILE, "a") as f:
            f.write(line + "\n")
    except OSError as e:
        print(f"[runs] append failed (non-fatal): {e}", file=sys.stderr)
        return False
    return True


def _run_state(run: RunRecord) -> str:
    """Render a run's outcome as one short token (``exit=N``/``timeout``)."""
    if run.get("timed_out"):
        return "timeout"
    return f"exit={run.get('exit_code')}"


def read_journal_entries(
    within_hours: float | None = None, *, verbose: bool = False
) -> list[dict[str, object]]:
    """Read journal entries, optionally filtered to the last ``within_hours``.

    Read without holding :func:`backlog_lock` — appends are serialized
    elsewhere (under the lock, or under a briefly-acquired one from
    ``dev_status_sync.py``), but a concurrent append can still expose a
    trailing partial line on the final read. A ``JSONDecodeError`` on that
    last line is silently skipped (the writer just hasn't finished); the
    same failure on any earlier line is real corruption and is surfaced as
    a stderr warning rather than silently dropped.
    """
    if not JOURNAL_FILE.exists():
        return []
    try:
        raw_lines = JOURNAL_FILE.read_text().splitlines()
    except OSError:
        return []

    non_blank = [line for line in raw_lines if line.strip()]
    cutoff = (
        datetime.now(UTC) - timedelta(hours=within_hours)
        if within_hours is not None
        else None
    )

    entries: list[dict[str, object]] = []
    last_index = len(non_blank) - 1
    for i, line in enumerate(non_blank):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            if i != last_index:
                cli_common.vprint(
                    f"[journal] corrupt line {i + 1} in {JOURNAL_FILE}",
                    verbose=verbose,
                    file=sys.stderr,
                )
            continue
        if not isinstance(entry, dict):
            continue
        if cutoff is not None:
            ts = _parse_journal_ts(entry.get("ts"))
            if ts is None or ts < cutoff:
                continue
        entries.append(entry)
    return entries


def _journal_last_entry_within(hours: float) -> bool:
    """Cheap pre-spawn check: does the journal's last entry fall within ``hours``?

    A bare "file non-empty" check would spawn a regen forever on a journal
    that's gone quiet (the child caches an empty result, it ages out in
    :data:`RECAP_TTL_SECONDS`, the next render spawns again) — this looks at
    the actual last timestamp instead.
    """
    if not JOURNAL_FILE.exists():
        return False
    try:
        non_blank = [
            line for line in JOURNAL_FILE.read_text().splitlines() if line.strip()
        ]
    except OSError:
        return False
    for line in reversed(non_blank):
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue  # trailing partial line (or, for an earlier one, corrupt) — skip
        ts = _parse_journal_ts(entry.get("ts") if isinstance(entry, dict) else None)
        if ts is None:
            return False
        return (datetime.now(UTC) - ts) <= timedelta(hours=hours)
    return False


# ── recap: cache + dispatch ─────────────────────────────────────────────────


def _recap_disabled() -> bool:
    """Kill-switch: checked both before dispatching a regen and at display time."""
    return os.environ.get("DEVSTATUS_RECAP_DISABLE") == "1"


def _load_recap_cache() -> dict[str, object] | None:
    """Load ``recap-cache.json``, or ``None`` if missing/corrupt/malformed."""
    if not RECAP_CACHE_FILE.exists():
        return None
    try:
        data = json.loads(RECAP_CACHE_FILE.read_text())
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _save_recap_cache(backend: str, text: str, board_fingerprint: str) -> None:
    """Atomically persist a recap result. ``text`` may legitimately be empty.

    Caching an empty result (with a fresh timestamp) is deliberate: it's
    what bounds retries to :data:`RECAP_TTL_SECONDS` instead of re-triggering
    a backend call on every render forever (see module plan, Part 3).

    ``board_fingerprint`` is the board's identity fingerprint (see
    :func:`_board_fingerprint`) as of generation time -- persisted so a
    later render can detect the board has moved since this text was
    generated (see :func:`_recap_section_lines`), independent of how much
    wall-clock time has passed. Deliberately not a counts-only summary: two
    different real board states (e.g. one item moving ready->done while
    another moves blocked->ready) can share identical counts while the
    prose's specific claims are already wrong about *which* item changed.
    """
    payload = json.dumps(
        {
            "generated_at": datetime.now(UTC).isoformat(),
            "backend": backend,
            "text": text,
            "board_fingerprint": board_fingerprint,
        }
    )
    _atomic_write_json(RECAP_CACHE_FILE, payload, ".recap_tmp_")


def _recap_cache_age_seconds(cache: dict[str, object]) -> float | None:
    """Seconds since ``cache`` was generated, or ``None`` if its timestamp is unusable."""
    ts = _parse_journal_ts(cache.get("generated_at"))
    if ts is None:
        return None
    return (datetime.now(UTC) - ts).total_seconds()


def _format_age(seconds: float) -> str:
    """Render an age in seconds as a short marker: ``"45m"`` or ``"3h"``."""
    hours = seconds / 3600
    if hours < 1:
        return f"{max(1, int(seconds / 60))}m"
    return f"{int(hours)}h"


@contextmanager
def _regen_lock(*, blocking: bool) -> Iterator[bool]:
    """Hold :data:`RECAP_REGEN_LOCK_FILE`, yielding whether it was acquired.

    Non-blocking mode (used by the detached regen child) yields ``False``
    immediately if another regen is already in flight instead of waiting —
    flock releases on process death, so a killed regen never leaves a stuck
    lock. Blocking mode (used by the synchronous ``recap`` subcommand) waits
    for that in-flight regen to finish and always yields ``True``.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(RECAP_REGEN_LOCK_FILE, "w") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _maybe_dispatch_recap_regen() -> None:
    """Spawn a detached recap-regen child if it looks worth refreshing.

    Never blocks and never calls a backend itself — this only decides
    whether to spawn ``_internal-regen`` as a fully detached child process.
    Must be called *after* releasing :func:`backlog_lock`; a 60s backend
    call made while holding it would stall every concurrent mutation and
    the agent ``render``-before-``--if-rev`` flow.

    A cache within :data:`RECAP_TTL_SECONDS` is only skipped if its board
    fingerprint still matches the live board -- a mutation that lands
    mid-TTL (e.g. a ``start``/``done`` right after a regen) must not have
    to wait out the rest of the window before a refresh is even considered.

    The quiet-journal check runs first, before touching the fingerprint:
    it's a cheap last-line-of-the-journal read, versus the fingerprint's
    full load-items-and-hash, and a mismatch during a quiet journal window
    (e.g. a cross-machine sync landed a cache from elsewhere with no local
    journal activity) would otherwise pay that cost only to bail out anyway
    on the very next check.
    """
    if _recap_disabled():
        return
    if not _journal_last_entry_within(RECAP_DISPATCH_WINDOW_HOURS):
        return  # quiet journal -- a spawn now would just cache emptiness
    cache = _load_recap_cache()
    if cache is not None:
        age = _recap_cache_age_seconds(cache)
        fingerprint_matches = (
            cache.get("board_fingerprint") == _current_board_fingerprint()
        )
        if age is not None and age <= RECAP_TTL_SECONDS and fingerprint_matches:
            return  # fresh and still accurate -- nothing to refresh
    subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "_internal-regen"],
        start_new_session=True,
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _recap_section_lines(color: bool, current_fingerprint: str) -> list[str] | None:
    """Build the dim RECAP frame's lines for :func:`render`, or ``None`` to omit it.

    Display rules, checked in order: kill-switch -> no cache/empty text ->
    board has moved since generation (fingerprint mismatch -- omitted
    regardless of age; a pre-migration cache with no ``"board_fingerprint"``
    key compares unequal to any real fingerprint and is treated the same
    way) -> fresh (< :data:`RECAP_TTL_SECONDS`, shown plain) -> stale (<=
    :data:`RECAP_STALE_MAX_HOURS`, shown with an age marker) -> older still
    (omitted).
    """
    if _recap_disabled():
        return None
    cache = _load_recap_cache()
    if cache is None:
        return None
    text = cache.get("text")
    if not isinstance(text, str) or not text:
        return None
    if cache.get("board_fingerprint") != current_fingerprint:
        return None
    age = _recap_cache_age_seconds(cache)
    if age is None:
        return None
    if age <= RECAP_TTL_SECONDS:
        marker = ""
    elif age <= RECAP_STALE_MAX_HOURS * 3600:
        marker = f" (recap from {_format_age(age)} ago)"
    else:
        return None

    frame_code = _COLORS.get("done")
    top = _colorize(_section_top("RECAP 👋"), frame_code, color)
    bottom = _colorize(_section_bottom(), frame_code, color)
    wrapped = textwrap.wrap(text, width=SECTION_WIDTH - 3) or [text]
    lines = [top]
    for i, wline in enumerate(wrapped):
        suffix = marker if i == len(wrapped) - 1 else ""
        lines.append(f"│  {wline}{suffix}")
    lines.append(bottom)
    return lines


# ── recap: prompt + normalization ───────────────────────────────────────────

RECAP_PROMPT = """\
You are writing a short "welcome back" recap for a personal task dashboard, \
in the style of an away-summary: second person, warm, plain text, no emoji, \
1-3 sentences. Use ONLY the facts below -- never invent items, people, or \
events not listed. Refer to items by what they are (a short plain-language \
description), never by an internal id or slug, even if one appears in the \
facts below. Stay short, but be comprehensive within that space: name what \
was actually done rather than only a count or a vague gesture at it.

Latest completions (not necessarily within the last 48h):
{completed}

Other recent activity (last 48h, excluding completion transitions):
{changelog}

Current board: {buckets}
"""


def _render_changelog(entries: list[dict[str, object]]) -> str:
    """Pre-render journal entries into dense changelog lines for the prompt.

    Raw JSON envelopes waste tokens on structural syntax and degrade a
    fast/cheap model's comprehension -- this does the structuring in Python
    instead so the prompt only spends tokens on the facts. The slug is only
    included when there's no summary to fall back on -- when both are
    present the summary alone should describe the item, and the id-shaped
    slug token invites a model to echo it back as if it were a name.
    Audited against every ``_journal_entry`` call site: every one that
    passes ``slug=`` also passes a non-empty ``summary=`` (backed by
    ``cmd_add``'s/``pending add``'s own non-empty-summary/description
    validation, and `cmd_rename`'s summary now being sourced from the
    renamed item's own title) -- so the ``slug``-without-``summary``
    fallback is defensive, not a live path in current practice. Kept
    rather than removed, for legacy/hand-edited journal data (the same
    defensive posture :func:`effective_blockers` takes for a missing
    ``blocked_by`` referent).

    Known remaining slug-shaped-text vectors, not guarded against here:
    a `reject`'s freeform `feedback` string (may name another item by
    slug) and, more speculatively, the `fields` detail list (only ever
    fixed, non-slug-shaped Python identifiers like "summary"/"priority"
    today, but nothing stops a future field name from being slug-shaped).
    Both are lower-value to guard against than the two fixed here
    (unconditional slug-append, `rename`'s synthetic summary): freeform
    text can't be safely scrubbed without risking mangled prose, and field
    names are code-controlled, not user data.
    """
    lines = []
    for e in entries:
        if e.get("to_status") == "done":
            continue
        ts = _parse_journal_ts(e.get("ts"))
        time_str = ts.strftime("%H:%M") if ts else "??:??"
        line = f"[{time_str}] {e.get('cmd', '?')}"
        slug = e.get("slug")
        summary = e.get("summary")
        if slug and not summary:
            line += f" {slug}"
        if summary:
            line += f" — {summary}"
        detail = []
        if e.get("from_status") and e.get("to_status"):
            detail.append(f"{e['from_status']}→{e['to_status']}")
        if e.get("fields"):
            detail.append(f"changed: {', '.join(cast(list[str], e['fields']))}")
        if e.get("feedback"):
            detail.append(f"feedback: {e['feedback']}")
        if e.get("count") is not None:
            detail.append(f"{e['count']} item(s)")
        if detail:
            line += f" ({'; '.join(detail)})"
        lines.append(line)
    return "\n".join(lines)


def _render_done_facts(items: list[BacklogItem]) -> str:
    """Render selected completed items as dated, slug-free prompt facts."""
    lines = []
    for item in items:
        stamp = _done_selection_stamp(item)
        completed = stamp.date().isoformat() if stamp else "unknown date"
        lines.append(f"- {item.get('summary', '')} (completed {completed})")
    return "\n".join(lines)


def _bucket_summary(
    in_progress: int, ready: int, blocked: int, in_review: int, done: int, pending: int
) -> str:
    """Render bucket section counts as a compact summary string for the recap prompt.

    Purely descriptive -- this is what the backend reads as "the facts"; it
    is not used to detect staleness (see :func:`_board_fingerprint` for
    that). Two different real board states can share the same counts (an
    item moving ready->done while another moves blocked->ready leaves
    every count unchanged), so counts alone would be too weak a staleness
    signal even though they're the right level of detail for the prompt.
    """
    parts = [
        f"in progress: {in_progress}",
        f"ready: {ready}",
        f"blocked: {blocked}",
        f"in review: {in_review}",
        f"done (latest 5 max): {done}",
        f"pending: {pending}",
    ]
    return ", ".join(parts)


def _current_bucket_summary() -> str:
    """Summarize the current board's section counts for the recap prompt."""
    items = load_items()
    pending_items = load_pending()
    in_progress, ready, blocked, in_review, done = _render_order(items)
    return _bucket_summary(
        len(in_progress),
        len(ready),
        len(blocked),
        len(in_review),
        len(done),
        len(pending_items),
    )


def _board_fingerprint(
    items: list[BacklogItem], pending_items: list[PendingItem]
) -> str:
    """Hash every item's identity + prose-bearing inputs for staleness checks.

    Unlike :func:`_bucket_summary`'s counts, this changes on *any* status
    move, ``blocked_by`` change, ``summary``/``description`` edit, or
    membership change (an item added/removed, or moving between buckets
    while total counts happen to stay put) -- exactly the cases a cached
    recap's specific claims could be contradicted by, which a counts-only
    comparison would silently miss.

    ``blocked_by`` is included, not just ``status``: an open item's
    ready-vs-blocked bucket is a function of :func:`effective_blockers`
    (itself derived from every item's id/status/``blocked_by``), so two
    items swapping which one blocks the other changes bucket membership
    while leaving both items' own ``status`` untouched -- (id, status)
    alone would miss it. A phantom ``blocked_by`` entry (a slug that no
    longer resolves) is hashed as-is even though :func:`effective_blockers`
    filters it out of bucket membership -- a harmless, accepted
    over-invalidation (an extra regen dispatch, not a wrong display) rather
    than a correctness problem worth the extra lookup to avoid.

    ``summary``/``description`` is included because the recap prompt's
    changelog lines *are* each item's summary/description (see
    :func:`_render_changelog`) -- a title edit doesn't move an item between
    buckets, but it does change what "you completed X" should now say,
    which is exactly the class of contradiction this fingerprint exists to
    catch, not just count/membership drift.

    Deliberately excludes ``priority``/sort-order fields (they reorder a
    bucket's rows but never move an item between buckets or change what a
    "ready: N" claim means, nor do they appear in any recap prose). The DONE
    selection version and normalized selection inputs are included separately
    so a change to the fixed selection rule or to a legacy fallback stamp
    cannot leave cached recap facts looking current.
    """
    pairs = sorted(
        (
            item["id"],
            item["status"],
            item.get("summary", ""),
            tuple(sorted(item.get("blocked_by", []))),
        )
        for item in items
    )
    pending_pairs = sorted(
        (f"pending:{p['id']}", p["status"], p.get("description", ""), ())
        for p in pending_items
    )
    done_selection_pairs = sorted(
        (
            item["id"],
            (
                stamp.isoformat()
                if (stamp := _done_selection_stamp(item)) is not None
                else None
            ),
            item.get("summary", ""),
        )
        for item in items
        if item.get("status") == "done"
    )
    payload = {
        "selection_version": DONE_SELECTION_VERSION,
        "board": pairs + pending_pairs,
        "done_selection": done_selection_pairs,
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    return digest[:16]


def _current_board_fingerprint() -> str:
    """Fingerprint of the current on-disk board, for staleness checks.

    Inherits whatever :func:`load_items`/:func:`load_pending` do with a
    missing or corrupt data file (defaults/empty, per their own contract)
    -- no special-casing here, same as :func:`_current_bucket_summary`.
    """
    return _board_fingerprint(load_items(), load_pending())


def _build_recap_prompt(changelog: str, buckets: str, completed: str) -> str:
    """Build the recap prompt from activity, selected completions, and counts."""
    return RECAP_PROMPT.format(
        changelog=changelog or "(none)",
        completed=completed or "(none)",
        buckets=buckets,
    )


_RECAP_MARKDOWN_RE = re.compile(r"[*_`#>~]")
_RECAP_EMOJI_RE = re.compile(
    "[\U0001f300-\U0001faff\U00002600-\U000027bf\U0001f1e6-\U0001f1ff]+"
)


def _normalize_recap_text(raw: str) -> str:
    """Defensively normalize a backend's raw recap output.

    No rejection path -- strips markdown markers and emoji, collapses
    whitespace, and truncates to :data:`RECAP_MAX_CHARS`. An empty result
    after normalization is a legitimate outcome, cached by the caller (see
    :func:`_save_recap_cache`), not an error.
    """
    text = _RECAP_EMOJI_RE.sub("", raw)
    text = _RECAP_MARKDOWN_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > RECAP_MAX_CHARS:
        text = text[:RECAP_MAX_CHARS].rstrip() + "…"
    return text


# ── recap: generation + subcommands ─────────────────────────────────────────


def _run_recap_regen(
    backend_override: str | None = None, *, verbose: bool = False
) -> tuple[str, str]:
    """Generate fresh recap prose and cache it.

    Returns ``(backend, text)`` on success -- including a successful call
    that normalizes to an empty string, which is still cached (see
    :func:`_save_recap_cache`). Returns ``("", "")`` without touching the
    cache if there's nothing to summarize, or every candidate backend
    fails/times out -- the prior cache (if any) is left exactly as it was.

    The board is loaded once for the prompt's counts (a board mutation
    landing mid-call only affects what the backend was *asked about*, not
    what gets cached as fresh -- that's correct, it's what the backend
    actually saw). The cache's fingerprint is captured separately, in a
    fresh read taken right before the write below rather than reused from
    that same pre-call snapshot: this narrows the "board moved during
    generation" race from the full, up-to-:data:`RECAP_TIMEOUT_SECONDS`-long
    backend call down to the instant between that read and the write. Any
    mutation landing in that narrow window (or after) is still caught: the
    fingerprint is re-checked against the live board on every display (see
    :func:`_recap_section_lines`) and every dispatch decision (see
    :func:`_maybe_dispatch_recap_regen`) -- bounding a stale cache's
    lifetime to at most one more regen cycle, self-triggered by the very
    next command or render.
    """
    entries = read_journal_entries(
        within_hours=RECAP_DISPATCH_WINDOW_HOURS, verbose=verbose
    )
    if not entries:
        return "", ""

    items = load_items()
    pending_items = load_pending()
    in_progress, ready, blocked, in_review, done = _render_order(items)
    buckets = _bucket_summary(
        len(in_progress),
        len(ready),
        len(blocked),
        len(in_review),
        len(done),
        len(pending_items),
    )
    prompt = _build_recap_prompt(
        _render_changelog(entries), buckets, _render_done_facts(done)
    )

    candidates = (
        [backend_override] if backend_override else llm_backends.available_backends()
    )
    for backend in candidates:
        try:
            if backend == "agy":
                raw = llm_backends.run_agy(
                    prompt, model=RECAP_AGY_MODEL, timeout=RECAP_TIMEOUT_SECONDS
                )
            elif backend == "opencode":
                raw = llm_backends.run_opencode(
                    prompt, model=None, timeout=RECAP_TIMEOUT_SECONDS
                )
            elif backend == "pi":
                raw = llm_backends.run_pi(
                    prompt, model=None, timeout=RECAP_TIMEOUT_SECONDS
                )
            elif backend == "copilot":
                raw = llm_backends.run_copilot(
                    prompt, model=None, timeout=RECAP_TIMEOUT_SECONDS
                )
            else:
                continue
        except llm_backends.BackendError:
            continue
        text = _normalize_recap_text(raw)
        # Re-read the fingerprint post-call rather than reusing the
        # pre-call snapshot: narrows the mid-call mutation race (see
        # docstring) from "anywhere during the up-to-RECAP_TIMEOUT_SECONDS
        # backend call" down to "the instant between this read and the
        # write below" -- the prompt itself still reflects the pre-call
        # board, which is correct (that's what the backend was actually
        # asked about).
        _save_recap_cache(cast(str, backend), text, _current_board_fingerprint())
        return cast(str, backend), text
    return "", ""


def cmd_internal_regen() -> None:
    """Hidden re-exec entrypoint spawned by :func:`_maybe_dispatch_recap_regen`.

    Not registered as a normal subcommand (not in :data:`SUBCOMMANDS`) --
    ``main`` dispatches to this directly off a raw argv check, before
    argparse, so it never appears in ``--help``. Exits immediately without
    calling any backend if another regen is already in flight.
    """
    with _regen_lock(blocking=False) as acquired:
        if acquired:
            _run_recap_regen()


def _print_recap(cache: dict[str, object]) -> None:
    """Print a cached recap's text, or a "nothing available" note if empty."""
    text = cache.get("text")
    if text:
        print(cast(str, text))
    else:
        print("[recap] no recap available", file=sys.stderr)


def cmd_recap(args: argparse.Namespace) -> None:
    """Handle ``recap``: the only synchronous regen path (explicit user intent).

    Bare: prints the cache if fresh *and* its board fingerprint still
    matches the live board, otherwise blocks on the regen lock and
    regenerates (double-checking freshness after acquiring, in case an
    in-flight detached child just refreshed it). ``--refresh`` bypasses the
    freshness check entirely and always regenerates.
    """

    current_fingerprint = _current_board_fingerprint()

    def _fresh_enough(cache: dict[str, object] | None) -> bool:
        if cache is None:
            return False
        if cache.get("board_fingerprint") != current_fingerprint:
            return False
        age = _recap_cache_age_seconds(cache)
        return age is not None and age <= RECAP_TTL_SECONDS

    cache = _load_recap_cache()
    if not args.refresh and _fresh_enough(cache):
        _print_recap(cast(dict[str, object], cache))
        return

    text = ""
    with _regen_lock(blocking=True):
        # Double-checked: an in-flight detached child may have just
        # refreshed the cache while this call waited on the lock -- reuse
        # it instead of burning a redundant backend call.
        cache = _load_recap_cache()
        if not args.refresh and _fresh_enough(cache):
            _print_recap(cast(dict[str, object], cache))
            return
        _backend, text = _run_recap_regen(
            backend_override=args.backend, verbose=args.verbose
        )

    if text:
        print(text)
    else:
        print("[recap] no recap available", file=sys.stderr)


# ── mutation infrastructure ───────────────────────────────────────────────────


def confirm_resolution(
    cmd: str,
    arg: str | int,
    item: BacklogItem | PendingItem,
    summary_key: str = "summary",
    *,
    quiet: bool = False,
) -> None:
    """Echo what a mutating command resolved to, so misresolution is visible."""
    ref = f"{arg} → " if str(arg) != item["id"] else ""
    summary = cast(dict[str, object], item).get(summary_key, "")
    cli_common.qprint(
        f"[{cmd}] {ref}{item['id']}: {summary}",
        quiet=(quiet or _agent_quiet()),
        file=sys.stderr,
    )


@dataclass
class _MutationResult:
    """Working set threaded through a :func:`_backlog_mutation` block.

    ``item``, ``items``, ``pending_items``, ``slug``, and ``new_rev`` are
    all populated before the caller's block runs — ``new_rev`` is bumped
    *before* yielding, ahead of any write, so a crash between the bump and
    the eventual write(s) never leaves changed data under a stale rev (see
    :func:`_backlog_mutation`'s docstring). It's still named ``new_rev``
    rather than folded into the constructor call above for API stability —
    existing callers read it post-``with`` via ``m.new_rev``.

    ``pending_items`` is exposed so mutators can render with the in-memory
    pending set instead of re-reading it unlocked post-lock (mixed-snapshot
    fix — the render call now belongs inside the lock).

    ``journal_extra`` lets a caller's block stuff command-specific journal
    fields (e.g. ``update``'s patched ``fields``, ``reject``'s ``feedback``)
    that :func:`_backlog_mutation`'s generic cleanup can't otherwise infer —
    it only auto-detects ``from_status``/``to_status`` by diffing status
    before/after the block runs.
    """

    item: BacklogItem
    items: list[BacklogItem]
    pending_items: list[PendingItem]
    slug: str
    new_rev: int | None = None
    journal_extra: dict[str, object] = field(default_factory=dict)


@contextmanager
def _backlog_mutation(
    cmd: str,
    id_arg: str,
    if_rev_arg: int | None,
    announce: bool = False,
    *,
    quiet: bool = False,
    verbose: bool = False,
) -> Iterator[_MutationResult]:
    """Run the shared skeleton for update/start/done/block/unblock/remove
    (also review/approve/reject/gate-set/gate-pass — every command sharing
    this shape).

    Acquires the lock, enforces the rev guard, resolves the target id, and
    refuses if it isn't a backlog item. Yields a :class:`_MutationResult`
    for the caller to mutate in place (or, for ``remove``, to reassign
    ``items`` to a filtered list). After the caller's block completes,
    saves ``items`` while still holding the lock, appends one journal event
    (status transition auto-detected; command-specific extras come from
    ``result.journal_extra``), then renders — matching the
    bump-then-write-then-journal-then-optionally-announce-then-render order
    every extracted command used before this helper existed. ``announce=True``
    calls :func:`confirm_resolution` (still inside the lock) for the
    update/start/done shape; ``block``/``unblock`` stay silent and
    ``remove`` announces its own message inside the caller's block, before
    this cleanup runs, using the pre-removal item. Once the lock is
    released, :func:`_maybe_dispatch_recap_regen` runs once for every
    caller of this helper — the single hook site covers all of them.

    The rev is bumped *before* yielding — ahead of every write, including
    ones a caller's block makes itself (e.g. ``remove``'s extra
    ``save_pending`` call) — not after the caller's block completes. A
    crash between the bump and any of those writes only burns a rev number
    (harmless: the guard just sees a numbering gap), rather than the
    reverse order's hazard, where a crash between a write and the bump
    left changed data sitting under a stale rev — silently letting a
    genuinely-stale numeric ``--if-rev`` call pass the guard, since the rev
    it was checked against hadn't advanced to reflect the change. One
    accepted side effect: a caller's block that refuses mid-way (e.g.
    ``block``'s cycle-detection check) now also burns a rev number even
    though nothing was written — harmless for the same reason, and
    preferable to threading a "did we already bump" flag through every
    caller just to preserve a no-op call leaving rev untouched, which
    nothing downstream relies on.

    The render call lives here — after the write(s) — rather than in each
    caller's block body: a caller-side ``render(m.items, rev=m.new_rev)``
    would run *before* this cleanup (a ``with`` block's body always
    finishes before the code after ``yield``), so it would render against
    ``result.items`` before the caller's own in-block writes (like
    ``remove``'s ``save_pending``) even though ``m.new_rev`` itself is
    already set by then.

    Args:
        cmd: Command name, used in error messages and announcements.
        id_arg: The raw id argument (slug or numeric position).
        if_rev_arg: The ``--if-rev`` value supplied, or ``None``.
        announce: Whether to call :func:`confirm_resolution` on success.
        quiet: Threaded to :func:`confirm_resolution` and
            :func:`append_journal_event`.
        verbose: Threaded to :func:`append_journal_event`.

    Yields:
        A :class:`_MutationResult` for the caller to mutate.

    Raises:
        SystemExit: Via :func:`enforce_rev_guard`, :func:`require_kind`, or
            directly, if the target can't be resolved.
    """
    with backlog_lock():
        items = load_items()
        pending_items = load_pending()
        current_rev = load_rev()
        enforce_rev_guard(cmd, id_arg, if_rev_arg, current_rev, items, pending_items)

        kind, slug = resolve_id(id_arg, items, pending_items)
        require_kind(cmd, id_arg, kind, "backlog")

        index = build_index(items)
        item = index.get(slug)
        if item is None:
            print(f"[{cmd}] not found: {slug}", file=sys.stderr)
            sys.exit(1)

        old_status = item.get("status")
        new_rev = bump_rev()
        result = _MutationResult(
            item=item,
            items=items,
            pending_items=pending_items,
            slug=slug,
            new_rev=new_rev,
        )

        yield result

        save_items(result.items)
        new_status = result.item.get("status")
        append_journal_event(
            _journal_entry(
                cmd,
                "backlog",
                cast(int, result.new_rev),
                slug=result.slug,
                summary=cast(str, result.item.get("summary", "")),
                from_status=old_status if old_status != new_status else None,
                to_status=new_status if old_status != new_status else None,
                fields=cast(list[str] | None, result.journal_extra.get("fields")),
                feedback=cast(str | None, result.journal_extra.get("feedback")),
                count=cast(int | None, result.journal_extra.get("count")),
            ),
            verbose=verbose,
        )
        if announce:
            confirm_resolution(cmd, id_arg, result.item, quiet=quiet)
        render(result.items, result.pending_items, rev=result.new_rev)
    _maybe_dispatch_recap_regen()


# ── subcommand handlers ───────────────────────────────────────────────────────


def cmd_render(args: argparse.Namespace) -> None:
    """Handle ``render``: print the dashboard with no other side effects.

    Reads items + pending + rev atomically under :func:`backlog_lock` so the
    printed (items, rev) pair is self-consistent — a writer committing between
    the item read and the rev read would otherwise pair a stale item-map with a
    fresh rev, defeating a downstream numeric ``--if-rev`` guard. The actual
    ``render`` call (and its recap dispatch check) happens after the lock is
    released — agents run a bare ``render`` before every numeric mutation to
    fetch the rev for ``--if-rev``, so this path must stay instant and never
    hold the lock across a possible recap-regen spawn.
    """
    with backlog_lock():
        items = load_items()
        pending_items = load_pending()
        rev = load_rev()
    render(items, pending_items, rev=rev, dispatch=True)


def cmd_list(args: argparse.Namespace) -> None:
    """Handle ``list``: print grouped, aligned backlog sections by default.

    Default output mirrors ``render``'s mental model: non-empty status
    sections (IN PROGRESS / READY / BLOCKED / IN REVIEW / DONE), with each
    row numbered to match ``render``'s display numbering so a number copied
    from ``list`` resolves identically in every mutation command via
    :func:`resolve_id`. The DONE section uses the same capped selection as
    ``render`` (:func:`_done_selection`) precisely because only those items
    appear in :func:`_unified_order` — an uncapped row would have no
    display number that any numeric-id command could resolve; older done
    items remain reachable by slug. ``--raw`` preserves the historical
    machine-readable TSV byte-for-byte.

    Reads items + pending + rev under :func:`backlog_lock` (same rationale
    as :func:`cmd_render`; pending is needed only for stable numbering).
    """
    with backlog_lock():
        items = load_items()
        pending_items = load_pending()
        rev = load_rev()
    print(f"# rev={rev}", file=sys.stderr)

    if args.raw:
        for item in items:
            if args.status is None or item.get("status") == args.status:
                print(
                    f"{item['id']}\t{item.get('status', '')}\t{item.get('summary', '')}"
                )
        return

    matched_slugs = {
        item["id"]
        for item in items
        if args.status is None or item.get("status") == args.status
    }
    if not matched_slugs:
        print("(backlog is empty)" if args.status is None else "(no matching items)")
        return

    ordered = _unified_order(items, pending_items)
    backlog_ids = {item["id"] for item in items}
    slug_to_num = {
        item["id"]: n + 1 for n, item in enumerate(ordered) if item["id"] in backlog_ids
    }

    in_progress, ready, blocked, in_review, _done_capped = _render_order(items)
    sections: list[tuple[str, Sequence[BacklogItem]]] = [
        ("IN PROGRESS", in_progress),
        ("READY", ready),
        ("BLOCKED", blocked),
        ("IN REVIEW", in_review),
        ("DONE", _done_selection(items)),
    ]

    built: list[tuple[str, list[tuple[int, str, str, str]]]] = []
    for title, bucket in sections:
        rows = []
        for item in bucket:
            if item["id"] not in matched_slugs:
                continue
            rows.append(
                (
                    slug_to_num[item["id"]],
                    _category_tag(cast(str, item.get("category", ""))),
                    cast(str, item.get("summary", "")),
                    item["id"],
                )
            )
        if rows:
            built.append((title, rows))

    num_w = len(str(max(n for _, rws in built for n, *_ in rws)))
    tag_w = max(len(tag) for _, rws in built for _, tag, _, _ in rws)
    sum_w = max(
        len(_ellipsize(summary, _LIST_SUMMARY_MAX))
        for _, rws in built
        for _, _, summary, _ in rws
    )

    out_lines: list[str] = []
    for i, (title, rows) in enumerate(built):
        if i > 0:
            out_lines.append("")
        out_lines.append(_section_top(title))
        for n, tag, summary, slug in rows:
            out_lines.append(
                f"│ {n:>{num_w}} {tag:<{tag_w}}"
                f"{_ellipsize(summary, _LIST_SUMMARY_MAX):<{sum_w}}  {slug}"
            )
        out_lines.append(_section_bottom())
    print("\n".join(out_lines))


def cmd_show(args: argparse.Namespace) -> None:
    """Handle ``show``: print the full JSON record for one item.

    Reads items + pending + rev under :func:`backlog_lock` (same rationale
    as :func:`cmd_render`).
    """
    with backlog_lock():
        items = load_items()
        pending_items = load_pending()
        kind, slug = resolve_id(args.id, items, pending_items)
        index: dict[str, object] = (
            cast(dict[str, object], {p["id"]: p for p in pending_items})
            if kind == "pending"
            else cast(dict[str, object], build_index(items))
        )
        item = index.get(slug)
        if item is None:
            print(f"[show] not found: {slug}", file=sys.stderr)
            sys.exit(1)
        print(f"# rev={load_rev()}", file=sys.stderr)
        if kind == "backlog":
            runs = load_runs(slug)
            if runs:
                last = runs[-1]
                print(
                    f"# runs: {len(runs)} recorded; most recent "
                    f"{last.get('run_id', '?')} {_run_state(last)} "
                    f"at {last.get('started_at', '?')}",
                    file=sys.stderr,
                )
        print(json.dumps(item, indent=2))


def cmd_add(args: argparse.Namespace) -> None:
    """Handle ``add``: append a new backlog item.

    ``args.json`` must decode to an object with at least ``id`` and
    ``summary``; see :data:`BacklogItem` for the full field set.
    """
    patch = _parse_json_arg(args.json, "add")

    slug = _str_field(patch, "id").strip()
    if not slug:
        summary = _str_field(patch, "summary")
        suggestion = re.sub(r"[^a-z0-9]+", "-", summary.lower()).strip("-")[:38]
        if not suggestion:
            suggestion = "my-item"
        if "-" not in suggestion:
            suggestion = f"item-{suggestion}"
        print(
            f"[add] 'id' is required — suggested slug: {suggestion}",
            file=sys.stderr,
        )
        sys.exit(1)

    err = validate_slug(slug, "add")
    if err:
        print(err, file=sys.stderr)
        sys.exit(1)

    if not _str_field(patch, "summary").strip():
        print("[add] 'summary' is required", file=sys.stderr)
        sys.exit(1)

    if "priority" in patch and patch["priority"] not in VALID_PRIORITIES:
        print(
            f"[add] invalid priority '{patch['priority']}' — must be one of: "
            f"{', '.join(sorted(VALID_PRIORITIES))}",
            file=sys.stderr,
        )
        sys.exit(1)

    with backlog_lock():
        items = load_items()
        pending_items = load_pending()
        index = build_index(items)

        if slug in index:
            print(f"[add] duplicate slug: {slug}", file=sys.stderr)
            sys.exit(1)
        # Cross-pool uniqueness: a pending item with the same id would
        # otherwise make resolve_id treat the backlog record as pending,
        # permanently orphaning it (it gets no number in the item-map at all).
        if any(p["id"] == slug for p in pending_items):
            print(
                f"[add] slug '{slug}' already exists as a pending item — "
                "remove or rename the pending item first",
                file=sys.stderr,
            )
            sys.exit(1)

        blocked_by = cast(list[str], _list_field(patch, "blocked_by"))
        pending_ids = {p["id"] for p in pending_items}
        for dep in blocked_by:
            if dep not in index and dep not in pending_ids:
                print(
                    f"[add] blocked_by references unknown slug: {dep}", file=sys.stderr
                )
                sys.exit(1)

        item: BacklogItem = {
            "id": slug,
            "created": today(),
            "updated": today(),
            "status": "open",
            "summary": _str_field(patch, "summary").strip(),
            "category": _str_field(patch, "category", "feature"),
            "blocked_by": blocked_by,
            "related_files": cast(
                list[dict[str, object]], _list_field(patch, "related_files")
            ),
            "context": _str_field(patch, "context"),
            "next_steps": _str_field(patch, "next_steps"),
        }
        if "priority" in patch:
            item["priority"] = cast(str, patch["priority"])

        items.append(item)
        # Bump the revision before writing data: a crash between this and
        # the write below only burns a rev number (harmless — the guard
        # just sees a gap), rather than leaving changed data under a stale
        # rev, which would let a genuinely-stale numeric --if-rev call
        # silently pass the guard.
        new_rev = bump_rev()
        save_items(items)
        append_journal_event(
            _journal_entry(
                "add", "backlog", new_rev, slug=slug, summary=item["summary"]
            ),
            verbose=args.verbose,
        )
        render(items, pending_items, rev=new_rev)
    _maybe_dispatch_recap_regen()
    _blocker_check_reminder(items, slug, cmd="add", quiet=args.quiet)
    _out_of_scope_check_reminder("add", quiet=args.quiet)


def cmd_update(args: argparse.Namespace) -> None:
    """Handle ``update``: merge a JSON patch into a backlog item."""
    patch = _parse_json_arg(args.patch, "update")

    bad = set(patch) & IMMUTABLE_FIELDS
    if bad:
        print(
            f"[update] cannot modify immutable field(s): {', '.join(sorted(bad))}",
            file=sys.stderr,
        )
        sys.exit(1)

    review_only_fields = {"review_feedback", "review_content_hash"} & set(patch)
    if review_only_fields:
        print(
            f"[update] cannot modify {', '.join(sorted(review_only_fields))} "
            "directly -- use 'review <id>' to submit for review, "
            "'approve <id>' to accept, or 'reject <id> <feedback>' to send "
            "back with feedback.",
            file=sys.stderr,
        )
        sys.exit(1)

    if "gate" in patch:
        print(
            "[update] cannot modify 'gate' directly -- use 'gate-set <id> "
            '\'{"required": true, "criteria": [...]}\'\' to classify or '
            "'gate-pass <id>' to record a pass.",
            file=sys.stderr,
        )
        sys.exit(1)

    unknown = set(patch) - BACKLOG_MUTABLE_FIELDS - IMMUTABLE_FIELDS
    if unknown:
        print(
            f"[update] unrecognized field(s): {', '.join(sorted(unknown))}",
            file=sys.stderr,
        )
        sys.exit(1)

    if "status" in patch and patch["status"] not in VALID_STATUSES:
        print(
            f"[update] invalid status '{patch['status']}' — must be one of: "
            f"{', '.join(sorted(VALID_STATUSES))}",
            file=sys.stderr,
        )
        sys.exit(1)

    # `priority: null` means "unset" — revert to the no-priority/normal
    # state cmd_add allows by omission — not a rejected value.
    unset_priority = "priority" in patch and patch["priority"] is None
    if (
        "priority" in patch
        and not unset_priority
        and patch["priority"] not in VALID_PRIORITIES
    ):
        print(
            f"[update] invalid priority '{patch['priority']}' — must be one of: "
            f"{', '.join(sorted(VALID_PRIORITIES))}",
            file=sys.stderr,
        )
        sys.exit(1)

    # `blocked_by` is rejected on `update` outright, even though it's listed in
    # BACKLOG_MUTABLE_FIELDS — the raw `dict.update(patch)` merge bypasses every
    # guard `block`/`unblock` enforces (existence, self-block, cycle detection),
    # and CLAUDE.md already routes blocked_by edits through `block`/`unblock`.
    # Refusing here removes the bypass path by construction instead of trying
    # to mirror block's validation into update and keep both code paths in
    # sync. `unblock` cannot be done via update either (removing a slug from
    # `blocked_by` via raw replacement would skip its "is the slug actually
    # present" check), so the same error redirects both directions.
    if "blocked_by" in patch:
        print(
            "[update] cannot modify 'blocked_by' directly — use "
            "'block <id> <blocker>' to add or 'unblock <id> <blocker>' to "
            "remove. update's raw merge bypasses block/unblock's existence, "
            "self-block, and cycle checks.",
            file=sys.stderr,
        )
        sys.exit(1)

    _reject_null_fields(
        "update",
        patch,
        ("summary", "category", "related_files", "context", "next_steps"),
    )

    with _backlog_mutation(
        "update",
        args.id,
        args.if_rev,
        announce=True,
        quiet=args.quiet,
        verbose=args.verbose,
    ) as m:
        m.journal_extra["fields"] = sorted(patch)
        if "status" in patch:
            _apply_status_transition(
                cast(dict[str, object], m.item),
                cast(str, patch["status"]),
                "completed_at",
                "done",
            )
        if unset_priority:
            m.item.pop("priority", None)
            del patch["priority"]
        if "status" in patch:
            new_st = patch["status"]
            if new_st != "in-progress":
                m.item.pop("claimed_by", None)
            elif (
                new_st == "in-progress"
                and "claimed_by" not in patch
                and "claimed_by" not in m.item
            ):
                m.item["claimed_by"] = _make_claim()
        cast(dict[str, object], m.item).update(patch)
        m.item["updated"] = today()


def cmd_start(args: argparse.Namespace) -> None:
    """Handle ``start``: mark a backlog item in-progress."""
    _check_worktree_guard(
        allow_main=bool(
            getattr(args, "allow_main", False)
            or getattr(args, "no_worktree_check", False)
        ),
        quiet=args.quiet,
    )
    with _backlog_mutation(
        "start",
        args.id,
        args.if_rev,
        announce=True,
        quiet=args.quiet,
        verbose=args.verbose,
    ) as m:
        if m.item.get("status") == "in-review":
            print(
                f"[start] {m.slug} is in-review -- use 'approve <id>' to "
                "accept it or 'reject <id> <feedback>' to send it back to "
                "in-progress.",
                file=sys.stderr,
            )
            sys.exit(1)
        _check_claim_collision(
            m.item,
            current_harness=_detect_harness(getattr(args, "claimed_by", None)),
            current_machine=_machine_id(),
            current_pid=os.getpid(),
            force=bool(getattr(args, "force", False)),
            quiet=args.quiet,
        )
        # Use the shared transition helper so completed_at is cleared when
        # moving an item off "done", mirroring cmd_done's behavior.
        _apply_status_transition(
            cast(dict[str, object], m.item), "in-progress", "completed_at", "done"
        )
        m.item["status"] = "in-progress"
        m.item["claimed_by"] = _make_claim(getattr(args, "claimed_by", None))
        m.item["updated"] = today()


def cmd_done(args: argparse.Namespace) -> None:
    """Handle ``done``: mark a backlog item done."""
    with _backlog_mutation(
        "done",
        args.id,
        args.if_rev,
        announce=True,
        quiet=args.quiet,
        verbose=args.verbose,
    ) as m:
        if m.item.get("status") == "in-review":
            print(
                f"[done] {m.slug} is in-review -- use 'approve <id>' to "
                "complete it or 'reject <id> <feedback>' to send it back to "
                "in-progress.",
                file=sys.stderr,
            )
            sys.exit(1)
        _check_gate_or_exit("done", m.item)
        _apply_status_transition(
            cast(dict[str, object], m.item), "done", "completed_at", "done"
        )
        m.item["status"] = "done"
        m.item["updated"] = today()


def cmd_review(args: argparse.Namespace) -> None:
    """Handle ``review``: submit (or re-submit) an item for review.

    Valid from ``in-progress`` (normal submission) or from ``in-review``
    itself (re-pins the content hash after a drift refusal, clearing any
    stale feedback, without changing status).
    """
    with _backlog_mutation(
        "review",
        args.id,
        args.if_rev,
        announce=True,
        quiet=args.quiet,
        verbose=args.verbose,
    ) as m:
        if m.item.get("status") not in ("in-progress", "in-review"):
            print(
                f"[review] {m.slug} is '{m.item.get('status')}' -- only an "
                "in-progress (or already in-review) item can be submitted "
                "for review.",
                file=sys.stderr,
            )
            sys.exit(1)
        _apply_status_transition(
            cast(dict[str, object], m.item), "in-review", "completed_at", "done"
        )
        m.item["status"] = "in-review"
        m.item["review_content_hash"] = _content_hash(m.item)
        m.item.pop("review_feedback", None)
        m.item["updated"] = today()


def cmd_approve(args: argparse.Namespace) -> None:
    """Handle ``approve``: accept an in-review item, marking it done."""
    with _backlog_mutation(
        "approve",
        args.id,
        args.if_rev,
        announce=True,
        quiet=args.quiet,
        verbose=args.verbose,
    ) as m:
        if m.item.get("status") != "in-review":
            print(
                f"[approve] {m.slug} is '{m.item.get('status')}', not "
                "in-review -- nothing to approve.",
                file=sys.stderr,
            )
            sys.exit(1)
        if m.item.get("review_content_hash") != _content_hash(m.item):
            print(
                f"[approve] {m.slug}'s content changed since it was "
                "submitted for review -- run 'review <id>' again to re-pin "
                "the current content before approving.",
                file=sys.stderr,
            )
            sys.exit(1)
        _check_gate_or_exit("approve", m.item)
        _apply_status_transition(
            cast(dict[str, object], m.item), "done", "completed_at", "done"
        )
        m.item["status"] = "done"
        m.item["updated"] = today()


def cmd_reject(args: argparse.Namespace) -> None:
    """Handle ``reject``: send an in-review item back to in-progress with feedback."""
    feedback = args.feedback.strip()
    if not feedback:
        print("[reject] feedback is required and cannot be empty", file=sys.stderr)
        sys.exit(1)
    with _backlog_mutation(
        "reject",
        args.id,
        args.if_rev,
        announce=True,
        quiet=args.quiet,
        verbose=args.verbose,
    ) as m:
        if m.item.get("status") != "in-review":
            print(
                f"[reject] {m.slug} is '{m.item.get('status')}', not "
                "in-review -- nothing to reject.",
                file=sys.stderr,
            )
            sys.exit(1)
        if m.item.get("review_content_hash") != _content_hash(m.item):
            print(
                f"[reject] {m.slug}'s content changed since it was "
                "submitted for review -- run 'review <id>' again to re-pin "
                "the current content, then reject with feedback that "
                "applies to what's actually there.",
                file=sys.stderr,
            )
            sys.exit(1)
        m.journal_extra["feedback"] = feedback
        _apply_status_transition(
            cast(dict[str, object], m.item), "in-progress", "completed_at", "done"
        )
        m.item["status"] = "in-progress"
        m.item["review_feedback"] = feedback
        m.item.pop("review_content_hash", None)
        m.item["updated"] = today()


def cmd_gate_set(args: argparse.Namespace) -> None:
    """Handle ``gate-set``: classify an item's judgment-step verification gate.

    Always resets ``passed_at`` to ``None`` — re-classifying a
    gate invalidates any prior pass (see :class:`Gate`'s docstring).
    """
    patch = _parse_json_arg(args.json, "gate-set")

    if "required" not in patch or not isinstance(patch["required"], bool):
        print("[gate-set] 'required' (bool) is required", file=sys.stderr)
        sys.exit(1)
    required = cast(bool, patch["required"])

    criteria_raw = _list_field(patch, "criteria")
    if not all(isinstance(c, str) and c.strip() for c in criteria_raw):
        print(
            "[gate-set] 'criteria' must be a list of non-empty strings",
            file=sys.stderr,
        )
        sys.exit(1)
    criteria = cast(list[str], criteria_raw)

    if required and not criteria:
        print(
            "[gate-set] 'criteria' cannot be empty when required=true",
            file=sys.stderr,
        )
        sys.exit(1)

    with _backlog_mutation(
        "gate-set",
        args.id,
        args.if_rev,
        announce=True,
        quiet=args.quiet,
        verbose=args.verbose,
    ) as m:
        m.item["gate"] = {
            "required": required,
            "criteria": criteria,
            "passed_at": None,
            # Full aware-UTC timestamp, not a date: gate-pass compares run
            # evidence's started_at against this, so a date-only stamp would
            # let same-day runs recorded *before* this re-classification
            # count as evidence. Starting a new generation shifts set_at,
            # automatically orphaning older runs (the audit trail in
            # runs.jsonl is never deleted).
            "set_at": datetime.now(UTC).isoformat(),
        }
        m.item["updated"] = today()


def _gate_pass_refusal(slug: str, problems: list[str], runs: list[RunRecord]) -> str:
    """Build a copy-pasteable refusal listing every coverage problem.

    Names each uncovered/invalid criterion with its reason and appends the
    item's recent runs (newest last) so the caller can pick a valid
    ``run:<run_id>`` citation without a second lookup.
    """
    lines = [f"[gate-pass] coverage invalid for {slug}:"]
    lines.extend(f"  {problem}" for problem in problems)
    if runs:
        lines.append(f"recent runs for {slug} (cite as run:<run_id>):")
        for run in runs[-5:]:
            lines.append(
                f"  {run.get('run_id', '?')}  {_run_state(run)}  "
                f"{run.get('started_at', '?')}  {run.get('command', '?')}"
            )
    else:
        lines.append(
            f"no runs recorded for {slug} -- record one with: "
            "dev_status run <id> -- <command...>"
        )
    return "\n".join(lines)


def _validate_run_citation(
    criterion: str, run_id: str, runs: list[RunRecord], gate: Mapping[str, object]
) -> tuple[str | None, dict[str, str] | None]:
    """Validate one ``run:<run_id>`` citation against the recorded evidence.

    Returns ``(problem, coverage_entry)`` — exactly one is ``None``. A run
    counts only when it exists for this item, exited 0, didn't time out,
    and started at or after the gate's ``set_at`` (gates without ``set_at``
    — classified before the stamp existed — impose no lower bound).
    """
    run = next((r for r in runs if r.get("run_id") == run_id), None)
    if run is None:
        message = (
            f"criterion {criterion}: unknown run id '{run_id}' "
            "(no such run recorded for this item)"
        )
        return (message, None)
    if run.get("timed_out") or run.get("exit_code") != 0:
        message = (
            f"criterion {criterion}: cited run {run_id} is failed or "
            f"timed out (exit={run.get('exit_code')}, "
            f"timed_out={run.get('timed_out')})"
        )
        return (message, None)
    started_at = _parse_journal_ts(run.get("started_at"))
    set_at = _parse_journal_ts(gate.get("set_at"))
    if started_at is None:
        return (
            f"criterion {criterion}: cited run {run_id} has no parseable started_at",
            None,
        )
    if set_at is not None and started_at < set_at:
        message = (
            f"criterion {criterion}: cited run {run_id} is stale "
            f"(started_at {run.get('started_at')} pre-dates gate set_at "
            f"{gate.get('set_at')})"
        )
        return (message, None)
    return None, {"kind": "run", "run_id": run_id}


def cmd_gate_pass(args: argparse.Namespace) -> None:
    """Handle ``gate-pass``: record that an item's gate criteria are satisfied.

    Requires an explicit per-criterion coverage payload — every numbered
    criterion (as ``show`` displays them) must cite either a recorded run
    (``run:<run_id>``: exit 0, not timed out, ``started_at`` at/after the
    gate's ``set_at``; one run may cover several criteria) or a non-empty
    manual note (``manual:<note>``) for genuinely-manual criteria such as
    visual checks. On success stores ``passed_at``, ``passed_via``
    (``run-evidence``/``manual``/``mixed``), and the normalized coverage
    map on the gate, so ``show`` displays which run/note satisfied what.
    """
    patch = (
        _parse_json_arg(args.json, "gate-pass") if getattr(args, "json", None) else {}
    )

    with _backlog_mutation(
        "gate-pass",
        args.id,
        args.if_rev,
        announce=True,
        quiet=args.quiet,
        verbose=args.verbose,
    ) as m:
        gate = m.item.get("gate")
        if not gate or not gate.get("required"):
            print(
                f"[gate-pass] {m.slug} has no required gate -- nothing to pass",
                file=sys.stderr,
            )
            sys.exit(1)
        coverage_raw = patch.get("coverage")
        if not isinstance(coverage_raw, dict):
            print(
                "[gate-pass] a coverage payload is required: "
                '{"coverage": {"<criterion#>": "run:<run_id>" or '
                '"manual:<note>"}} — every criterion needs recorded run '
                "evidence ('dev_status run <id> -- <command...>') or a manual note",
                file=sys.stderr,
            )
            sys.exit(1)
        criteria = cast(list[str], gate.get("criteria", []))
        n = len(criteria)
        runs = load_runs(m.slug)
        problems: list[str] = []
        covered: dict[str, dict[str, str]] = {}
        for key, value in coverage_raw.items():
            if not isinstance(key, str) or not key.isdigit():
                problems.append(
                    f"coverage key {key!r}: not a criterion number "
                    f"(criteria are 1..{n})"
                )
                continue
            if not 1 <= int(key) <= n:
                problems.append(
                    f"coverage key '{key}': criterion number out of range "
                    f"(criteria are 1..{n})"
                )
                continue
            # Canonicalize before using the key as a dict key or in the
            # "not covered" membership check below -- otherwise a
            # non-canonical but validly-in-range key like "01" satisfies
            # every check above yet never matches str(i)'s canonical form,
            # so genuinely-covered criterion 1 still reports "not covered".
            key = str(int(key))
            if not isinstance(value, str):
                problems.append(
                    f"criterion {key}: value must be a string "
                    "('run:<run_id>' or 'manual:<note>')"
                )
                continue
            if value.startswith("run:"):
                problem, entry = _validate_run_citation(
                    key, value[len("run:") :].strip(), runs, gate
                )
                if problem:
                    problems.append(problem)
                else:
                    covered[key] = cast(dict[str, str], entry)
            elif value.startswith("manual:"):
                note = value[len("manual:") :].strip()
                if not note:
                    problems.append(f"criterion {key}: empty manual note")
                else:
                    covered[key] = {"kind": "manual", "note": note}
            else:
                problems.append(
                    f"criterion {key}: value must be 'run:<run_id>' or 'manual:<note>'"
                )
        for i in range(1, n + 1):
            if str(i) not in covered and not any(
                p.startswith(f"criterion {i}:") for p in problems
            ):
                problems.append(f"criterion {i}: not covered")
        if problems:
            print(_gate_pass_refusal(m.slug, problems, runs), file=sys.stderr)
            sys.exit(1)
        kinds = {entry["kind"] for entry in covered.values()}
        gate["passed_at"] = today()
        gate["passed_via"] = (
            "manual"
            if kinds == {"manual"}
            else "run-evidence"
            if kinds == {"run"}
            else "mixed"
        )
        gate["coverage"] = covered
        m.item["updated"] = today()


def cmd_run(args: argparse.Namespace) -> None:
    """Handle ``run``: execute a command and record it as run evidence.

    Executes ``args.command`` via :func:`subprocess.run` — no shell, output
    inherited (visible live; a 30-minute suite must not look like a hung
    prompt) — then appends one truthful row (exit code, duration, timeout
    flag) to the ``runs.jsonl`` sidecar for ``gate-pass`` to cite.

    Lock scope: the item-existence check and the append each take
    :func:`backlog_lock` briefly; the subprocess itself runs **outside** the
    lock — holding it across a long test run would freeze every other
    harness session sharing the store.

    A numeric ``id`` is resolved against the full pending+backlog pool
    (matching ``render``'s own numbering) and then checked with
    :func:`require_kind` — resolving against the backlog pool alone would
    silently misnumber every position once any pending item exists, since
    pending items always come first in the dashboard's numbering. Like
    every other numeric-id command, a stale position is caught by
    :func:`enforce_rev_guard` rather than silently attributing this run's
    evidence to the wrong item.
    """
    with backlog_lock():
        items = load_items()
        pending_items = load_pending()
        current_rev = load_rev()
        enforce_rev_guard(
            "run", args.id, args.if_rev, current_rev, items, pending_items
        )
        kind, slug = resolve_id(args.id, items, pending_items)
        require_kind("run", args.id, kind, "backlog")
    # argparse's native "--" handling (see the parser's nargs="*" comment)
    # already strips a leading "--" separator, so args.command is the
    # command as typed -- no manual stripping here, which would otherwise
    # eat a second, user-supplied "--" as the command's own first argument.
    command = list(args.command or [])
    if not command:
        print(
            "[run] no command given -- usage: dev_status run <id> -- <command...>",
            file=sys.stderr,
        )
        sys.exit(1)

    # DEVSTATUS_AGENT scrubbed from the child's environment: CLAUDE.md's own
    # convention has agents prefix it on dev_status.py mutating calls
    # (suppresses a stderr echo), and `run` -- needing --if-rev like every
    # other numeric-id mutation -- reasonably looks like one of those calls.
    # But `run`'s "mutating call" is really the *recorded command*, which
    # can itself invoke dev_status.py (e.g. this repo's own test suite);
    # left unscrubbed, that inherited DEVSTATUS_AGENT=1 changes the command's
    # own behavior and produces false evidence -- reproduced live: an
    # unrelated `dev_status.py run ... -- uv run pytest -q` command run this
    # way spuriously failed 45 dev_status.py tests that assert on the exact
    # stderr echo DEVSTATUS_AGENT suppresses.
    child_env = {k: v for k, v in os.environ.items() if k != "DEVSTATUS_AGENT"}
    started_at = datetime.now(UTC).isoformat()
    start_mono = time.monotonic()
    try:
        proc = subprocess.run(command, timeout=args.timeout, env=child_env)
        exit_code: int | None = proc.returncode
        timed_out = False
    except subprocess.TimeoutExpired:
        print(
            f"[run] command timed out after {args.timeout}s",
            file=sys.stderr,
        )
        exit_code, timed_out = None, True
    duration_s = round(time.monotonic() - start_mono, 3)

    record: RunRecord = {
        "run_id": uuid.uuid4().hex,
        "item": slug,
        "command": " ".join(command),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "started_at": started_at,
        "duration_s": duration_s,
        "cwd": os.getcwd(),
    }
    with backlog_lock():
        appended = append_run_record(record)
    state = "timed out" if timed_out else f"exit {exit_code}"
    if appended:
        cli_common.qprint(
            f"[run] recorded {record['run_id']} for {slug}: {state} after {duration_s}s",
            quiet=args.quiet,
        )
    else:
        # append_run_record already printed the failure reason -- this just
        # makes sure stdout never claims evidence was saved when it wasn't,
        # since a later gate-pass citing run_id would otherwise fail with a
        # confusing "unknown run id" and no obvious link back to this line.
        print(
            f"[run] ran {slug}: {state} after {duration_s}s -- NOT recorded, "
            "see the error above",
            file=sys.stderr,
        )


def cmd_runs(args: argparse.Namespace) -> None:
    """Handle ``runs``: list an item's recorded run evidence."""
    with backlog_lock():
        items = load_items()
        pending_items = load_pending()
        kind, slug = resolve_id(args.id, items, pending_items)
        require_kind("runs", args.id, kind, "backlog")
        runs = load_runs(slug)
    if not runs:
        cli_common.qprint(f"[runs] {slug}: no runs recorded", quiet=args.quiet)
        return
    cli_common.qprint(f"[runs] {slug}: {len(runs)} recorded run(s)", quiet=args.quiet)
    for run in runs:
        cli_common.qprint(
            f"  {run.get('run_id', '?')}  {_run_state(run)}  "
            f"{run.get('started_at', '?')}  {run.get('duration_s', '?')}s  "
            f"{run.get('command', '?')}",
            quiet=args.quiet,
        )


def cmd_backfill_gate(args: argparse.Namespace) -> None:
    """Handle ``backfill-gate``: stamp an explicit inert gate on legacy items.

    Items created before ``gate`` existed have no ``gate`` key at all —
    code already treats that as inert (see :func:`_gate_blocks`). Leaving
    it implicit means only ``show <id>``'s raw JSON can distinguish "not
    yet classified" from "classified, not required" — the dashboard
    intentionally renders both the same way (see ``_gate_suffix``, and
    ``test_41p``). This makes the distinction explicit in storage, for
    tooling/audits that read the JSON directly. There's no reliable
    signal in the existing schema (no per-item verdict data) to infer a
    *required* gate from, so this never sets ``required: true`` -- that
    only ever happens via a deliberate ``gate-set`` call. Dry run by
    default; ``--apply`` writes.
    """
    with backlog_lock():
        items = load_items()
        missing = [i for i in items if "gate" not in i]
        if not missing:
            print("[backfill-gate] nothing to do -- every item already has a gate")
            return
        # The listing is payload in dry-run mode (nothing else follows) but
        # pre-mutation chatter in --apply mode (render() follows below) --
        # gate it only in the latter case, never in the former.
        listing_quiet = args.quiet if args.apply else False
        cli_common.qprint(
            f"[backfill-gate] {len(missing)} item(s) missing 'gate':",
            quiet=listing_quiet,
        )
        for i in missing:
            cli_common.qprint(
                f"  {i['id']}: {i.get('summary', '')}", quiet=listing_quiet
            )
        if not args.apply:
            print(
                f"[backfill-gate] dry run -- re-run with --apply to stamp "
                f"{len(missing)} item(s) with an inert gate"
            )
            return
        for i in missing:
            i["gate"] = {
                "required": False,
                "criteria": [],
                "passed_at": None,
            }
        new_rev = bump_rev()
        save_items(items)
        append_journal_event(
            _journal_entry("backfill-gate", "backlog", new_rev, count=len(missing)),
            verbose=args.verbose,
        )
        cli_common.qprint(
            f"[backfill-gate] stamped {len(missing)} item(s)", quiet=args.quiet
        )
        render(items, load_pending(), rev=new_rev)
    _maybe_dispatch_recap_regen()


def cmd_rename(args: argparse.Namespace) -> None:
    """Handle ``rename``: rename a slug and rewrite every reference to it.

    Rewrites references across both backlog and pending items: backlog
    ``blocked_by`` lists and prose fields (``summary``/``context``/
    ``next_steps``), pending ``blocking`` lists and ``related_files[].note``
    prose. Prose substitution uses a boundary anchored on the slug alphabet
    ``[a-z0-9-]`` (negative lookbehind/lookahead) — ``\\b`` would over-match
    because ``-`` is a non-word char, so ``\\bfoo-bar\\b`` matches the
    ``foo-bar`` prefix of the unrelated sibling slug ``foo-bar-baz``.
    """
    old_slug_arg = args.old_slug
    new_slug = args.new_slug

    err = validate_slug(new_slug, "rename")
    if err:
        print(err, file=sys.stderr)
        sys.exit(1)

    with backlog_lock():
        items = load_items()
        pending_items = load_pending()
        current_rev = load_rev()
        enforce_rev_guard(
            "rename", old_slug_arg, args.if_rev, current_rev, items, pending_items
        )

        kind, old_slug = resolve_id(old_slug_arg, items, pending_items)
        require_kind("rename", old_slug_arg, kind, "backlog")

        index = build_index(items)
        pending_index = {p["id"]: p for p in pending_items}

        # Cross-pool collision check: new_slug must not exist in either pool
        # (old_slug is already guaranteed to be a backlog item, not a
        # pending one, by require_kind above).
        if new_slug in index:
            print(f"[rename] collision: '{new_slug}' already exists", file=sys.stderr)
            sys.exit(1)
        if new_slug in pending_index:
            print(
                f"[rename] collision: '{new_slug}' already exists as a pending item",
                file=sys.stderr,
            )
            sys.exit(1)

        # Boundary anchored on the slug alphabet (not \w) so `foo-bar` does
        # not match the prefix of `foo-bar-baz`.
        word_re = re.compile(r"(?<![a-z0-9-])" + re.escape(old_slug) + r"(?![a-z0-9-])")

        def _rewrite_prose(text: str) -> str:
            if old_slug in text:
                return word_re.sub(new_slug, text)
            return text

        renamed_item: BacklogItem | None = None
        for item in items:
            if item["id"] == old_slug:
                item["id"] = new_slug
                renamed_item = item
            item["blocked_by"] = [
                new_slug if s == old_slug else s for s in item.get("blocked_by", [])
            ]
            fields = cast(dict[str, str], item)
            for field in ("summary", "context", "next_steps"):
                fields[field] = _rewrite_prose(fields.get(field, ""))
            for rf in item.get("related_files", []):
                note = rf.get("note", "") if isinstance(rf, dict) else ""
                if isinstance(note, str) and note:
                    rf["note"] = _rewrite_prose(note)

        for p in pending_items:
            p["blocking"] = [
                new_slug if s == old_slug else s for s in p.get("blocking", [])
            ]
            fields = cast(dict[str, str], p)
            for field in ("description", "context"):
                fields[field] = _rewrite_prose(fields.get(field, ""))
            for step_idx, step in enumerate(p.get("next_steps", [])):
                if isinstance(step, str):
                    p["next_steps"][step_idx] = _rewrite_prose(step)
            for rf in p.get("related_files", []):
                note = rf.get("note", "") if isinstance(rf, dict) else ""
                if isinstance(note, str) and note:
                    rf["note"] = _rewrite_prose(note)

        # Bump before either write (see _backlog_mutation's docstring for
        # why this order matters): a crash mid-way here burns a rev number
        # rather than leaving one or both files changed under a stale rev.
        new_rev = bump_rev()
        save_items(items)
        save_pending(pending_items)
        # runs.jsonl rows reference the old slug as their `item` field —
        # rewrite them in the same pass (same policy as blocked_by), so
        # recorded evidence survives a rename instead of dangling.
        runs = load_runs()
        renamed_runs = 0
        for run in runs:
            if run.get("item") == old_slug:
                run["item"] = new_slug
                renamed_runs += 1
        if renamed_runs:
            write_runs_file(runs)
        # renamed_item is guaranteed set: resolve_id/require_kind above
        # already confirmed old_slug names a backlog item in `items`.
        renamed_summary = cast(BacklogItem, renamed_item)["summary"]
        append_journal_event(
            _journal_entry(
                "rename",
                "backlog",
                new_rev,
                slug=new_slug,
                summary=f"renamed an item ({renamed_summary})",
            ),
            verbose=args.verbose,
        )
        cli_common.qprint(
            f"[rename] {old_slug} → {new_slug}",
            quiet=(args.quiet or _agent_quiet()),
            file=sys.stderr,
        )
        render(items, pending_items, rev=new_rev)
    _maybe_dispatch_recap_regen()


def cmd_block(args: argparse.Namespace) -> None:
    """Handle ``block``: add a blocker to a backlog item.

    ``blocker`` may be a backlog slug or a pending item's slug — a backlog
    item can be genuinely blocked on an external wait, not just on another
    backlog item. ``effective_blockers`` already treats any slug missing
    from the backlog index as unresolved, so a pending blocker correctly
    keeps the item out of READY with no further change there; this
    function only needed to stop rejecting it at the door. Cycle detection
    stays backlog-index-only deliberately: a pending item carries no
    ``blocked_by`` of its own, so it can never participate in a cycle.

    Refuses duplicates and cycle-creating blockers.
    """
    blocker = args.blocker
    with _backlog_mutation(
        "block", args.id, args.if_rev, quiet=args.quiet, verbose=args.verbose
    ) as m:
        index = build_index(m.items)
        pending_ids = {p["id"] for p in m.pending_items}
        if blocker not in index and blocker not in pending_ids:
            print(f"[block] blocker not found: {blocker}", file=sys.stderr)
            sys.exit(1)
        if blocker in m.item.get("blocked_by", []):
            print(f"[block] {m.slug} already blocked by {blocker}", file=sys.stderr)
            sys.exit(1)
        if detect_cycle(m.slug, blocker, index):
            print(
                f"[block] would create a cycle: {blocker} already depends on {m.slug}",
                file=sys.stderr,
            )
            sys.exit(1)

        m.item.setdefault("blocked_by", []).append(blocker)
        m.item["updated"] = today()


def cmd_unblock(args: argparse.Namespace) -> None:
    """Handle ``unblock``: remove a blocker from a backlog item."""
    blocker = args.blocker
    with _backlog_mutation(
        "unblock", args.id, args.if_rev, quiet=args.quiet, verbose=args.verbose
    ) as m:
        if blocker not in m.item.get("blocked_by", []):
            print(f"[unblock] {m.slug} is not blocked by {blocker}", file=sys.stderr)
            sys.exit(1)

        m.item["blocked_by"] = [s for s in m.item["blocked_by"] if s != blocker]
        m.item["updated"] = today()


def _read_reason_file(path_str: str) -> str:
    """Read and validate a ``--reason-file`` argument's content.

    Exits with status 1 after a distinct stderr message for each of:
    missing path, a directory, an unreadable file, or empty/whitespace-only
    content.
    """
    path = Path(path_str).expanduser()
    if not path.exists():
        print(f"[out-of-scope] reason file not found: {path}", file=sys.stderr)
        sys.exit(1)
    if not path.is_file():
        print(
            f"[out-of-scope] reason file is not a regular file: {path}", file=sys.stderr
        )
        sys.exit(1)
    try:
        text = path.read_text()
    except OSError as e:
        print(f"[out-of-scope] reason file unreadable: {path} ({e})", file=sys.stderr)
        sys.exit(1)
    if not text.strip():
        print(f"[out-of-scope] reason file is empty: {path}", file=sys.stderr)
        sys.exit(1)
    return text


def cmd_out_of_scope_add(args: argparse.Namespace) -> None:
    """Handle ``out-of-scope add``: record a rejected concept."""
    slug = args.concept_slug
    err = validate_slug(slug, "out-of-scope add")
    if err:
        print(err, file=sys.stderr)
        sys.exit(1)

    reason = _read_reason_file(args.reason_file)
    related_items = list(args.related_item or [])

    with out_of_scope_lock():
        index = _load_out_of_scope_index()
        md_path = _out_of_scope_md_path(slug)
        if slug in index or md_path.exists():
            print(
                f"[out-of-scope add] '{slug}' already exists — use "
                "'out-of-scope link' to add a referencing item, or "
                "'out-of-scope remove' first to start over",
                file=sys.stderr,
            )
            sys.exit(1)

        if related_items:
            item_index = build_index(load_items())
            for related in related_items:
                if related not in item_index:
                    print(
                        f"[out-of-scope add] related-item not found: {related}",
                        file=sys.stderr,
                    )
                    sys.exit(1)

        rejected = today()
        content = f"# {slug}\n\nRejected: {rejected}\n\n{reason}"
        _atomic_write_json(md_path, content, f".oos_{slug}_tmp_")
        index[slug] = {"rejected": rejected, "related_items": related_items}
        _save_out_of_scope_index(index)


def cmd_out_of_scope_link(args: argparse.Namespace) -> None:
    """Handle ``out-of-scope link``: reference a backlog item from a rejected concept."""
    slug = args.concept_slug
    backlog_slug = args.backlog_slug
    with out_of_scope_lock():
        index = _load_out_of_scope_index()
        if slug not in index:
            print(f"[out-of-scope link] not found: {slug}", file=sys.stderr)
            sys.exit(1)
        if backlog_slug not in build_index(load_items()):
            print(
                f"[out-of-scope link] not a current backlog slug: {backlog_slug}",
                file=sys.stderr,
            )
            sys.exit(1)

        related = cast(list[str], index[slug].setdefault("related_items", []))
        if backlog_slug not in related:
            related.append(backlog_slug)
            _save_out_of_scope_index(index)


def cmd_out_of_scope_unlink(args: argparse.Namespace) -> None:
    """Handle ``out-of-scope unlink``: remove a backlog-item reference."""
    slug = args.concept_slug
    backlog_slug = args.backlog_slug
    with out_of_scope_lock():
        index = _load_out_of_scope_index()
        if slug not in index:
            print(f"[out-of-scope unlink] not found: {slug}", file=sys.stderr)
            sys.exit(1)

        related = cast(list[str], index[slug].setdefault("related_items", []))
        if backlog_slug in related:
            related.remove(backlog_slug)
            _save_out_of_scope_index(index)


def cmd_out_of_scope_remove(args: argparse.Namespace) -> None:
    """Handle ``out-of-scope remove``: delete a rejected concept's record."""
    slug = args.concept_slug
    with out_of_scope_lock():
        index = _load_out_of_scope_index()
        md_path = _out_of_scope_md_path(slug)
        had_entry = slug in index
        had_md = md_path.exists()
        if not had_entry and not had_md:
            print(f"[out-of-scope remove] not found: {slug}", file=sys.stderr)
            sys.exit(1)

        if had_md:
            md_path.unlink(missing_ok=True)
        if had_entry:
            index.pop(slug, None)
            _save_out_of_scope_index(index)


def cmd_out_of_scope_list(args: argparse.Namespace) -> None:
    """Handle ``out-of-scope list``: list rejected concepts, newest-first."""
    index = _load_out_of_scope_index()
    if not index:
        print("[out-of-scope] no rejected concepts on file", file=sys.stderr)
        return
    entries = sorted(
        index.items(), key=lambda kv: kv[1].get("rejected", ""), reverse=True
    )
    for slug, entry in entries:
        rejected = entry.get("rejected", "?")
        count = len(cast(list[str], entry.get("related_items", [])))
        print(f"{slug}  rejected {rejected}  related_items={count}")


def cmd_out_of_scope_show(args: argparse.Namespace) -> None:
    """Handle ``out-of-scope show``: print a rejected concept's full record."""
    slug = args.concept_slug
    index = _load_out_of_scope_index()
    md_path = _out_of_scope_md_path(slug)
    entry = index.get(slug)
    if entry is None and not md_path.exists():
        print(f"[out-of-scope show] not found: {slug}", file=sys.stderr)
        sys.exit(1)

    if md_path.exists():
        print(md_path.read_text())
    else:
        print(f"[out-of-scope show] warning: '{slug}.md' is missing", file=sys.stderr)

    if entry is not None:
        related = cast(list[str], entry.get("related_items", []))
        print(f"related_items: {', '.join(related) if related else '(none)'}")
    else:
        print(
            f"[out-of-scope show] warning: '{slug}' has no index.json entry",
            file=sys.stderr,
        )


def cmd_pending_add(args: argparse.Namespace) -> None:
    """Handle ``pending add``: track a new waiting-on-someone-else item.

    ``args.json`` must decode to an object with at least ``id``,
    ``description``, and ``kind``; see :data:`PendingItem` for the full
    field set.
    """
    patch = _parse_json_arg(args.json, "pending add")

    slug = _str_field(patch, "id").strip()
    if not slug:
        print("[pending add] 'id' is required", file=sys.stderr)
        sys.exit(1)
    err = validate_slug(slug, "pending add")
    if err:
        print(err, file=sys.stderr)
        sys.exit(1)

    description = _str_field(patch, "description").strip()
    if not description:
        print("[pending add] 'description' is required", file=sys.stderr)
        sys.exit(1)

    kind = _str_field(patch, "kind")
    if kind not in VALID_PENDING_KINDS:
        print(
            f"[pending add] invalid kind '{kind}' — one of: "
            f"{', '.join(sorted(VALID_PENDING_KINDS))}",
            file=sys.stderr,
        )
        sys.exit(1)

    with backlog_lock():
        pending_items = load_pending()
        if any(p["id"] == slug for p in pending_items):
            print(f"[pending add] duplicate id: {slug}", file=sys.stderr)
            sys.exit(1)

        backlog_items = load_items()
        index = build_index(backlog_items)
        # Cross-pool uniqueness: a backlog item with the same id would
        # otherwise make resolve_id treat the pending record as backlog
        # (resolve_id looks at the pending pool first for non-numeric args),
        # permanently orphaning the pending item.
        if slug in index:
            print(
                f"[pending add] slug '{slug}' already exists as a backlog item — "
                "remove or rename the backlog item first",
                file=sys.stderr,
            )
            sys.exit(1)

        blocking = cast(list[str], _list_field(patch, "blocking"))
        for dep in blocking:
            if dep not in index:
                print(
                    f"[pending add] blocking references unknown slug: {dep}",
                    file=sys.stderr,
                )
                sys.exit(1)

        pending_items.append(
            {
                "id": slug,
                "created": today(),
                "updated": today(),
                "status": "waiting_for_reply",
                "description": description,
                "kind": kind,
                "source_ref": _dict_field(patch, "source_ref"),
                "context": _str_field(patch, "context"),
                "next_steps": cast(list[str], _list_field(patch, "next_steps")),
                "blocking": blocking,
                "outcome": None,
            }
        )
        new_rev = bump_rev()
        save_pending(pending_items)
        append_journal_event(
            _journal_entry("add", "pending", new_rev, slug=slug, summary=description),
            verbose=args.verbose,
        )
        cli_common.qprint(
            f"[pending add] {slug} — {description[:60]}",
            quiet=(args.quiet or _agent_quiet()),
            file=sys.stderr,
        )
        render(backlog_items, pending_items, rev=new_rev)
    _maybe_dispatch_recap_regen()
    _blocker_check_reminder(backlog_items, None, cmd="pending add", quiet=args.quiet)
    _out_of_scope_check_reminder("pending add", quiet=args.quiet)


def cmd_pending_update(args: argparse.Namespace) -> None:
    """Handle ``pending update``: merge a JSON patch into a pending item."""
    patch = _parse_json_arg(args.patch, "pending update")

    bad = set(patch) - PENDING_MUTABLE_FIELDS
    if bad:
        print(
            f"[pending update] cannot update field(s): {', '.join(sorted(bad))}",
            file=sys.stderr,
        )
        sys.exit(1)
    if "status" in patch and patch["status"] not in VALID_PENDING_STATUSES:
        print(
            f"[pending update] invalid status '{patch['status']}' — one of: "
            f"{', '.join(sorted(VALID_PENDING_STATUSES))}",
            file=sys.stderr,
        )
        sys.exit(1)

    # `outcome: null` is legitimate (that's its state until resolution), so
    # it's deliberately excluded here — everything else in this allowlist
    # is always-present/never-null per PendingItem's schema.
    _reject_null_fields(
        "pending update",
        patch,
        ("description", "context", "next_steps", "blocking", "source_ref"),
    )

    with backlog_lock():
        items = load_items()
        pending_items = load_pending()
        current_rev = load_rev()
        enforce_rev_guard(
            "pending update", args.id, args.if_rev, current_rev, items, pending_items
        )

        kind, slug = resolve_id(args.id, items, pending_items)
        require_kind("pending update", args.id, kind, "pending")

        index = {p["id"]: p for p in pending_items}
        item = index.get(slug)
        if item is None:
            print(f"[pending update] not found: {slug}", file=sys.stderr)
            sys.exit(1)
        # Validate `blocking` slugs against the backlog index, mirroring
        # `pending add`'s validation — without this, a typo'd slug slips in
        # here and CLAUDE.md routes post-add blocker edits through this path.
        if "blocking" in patch:
            backlog_index = build_index(items)
            blocking_patch = cast(list[str], _list_field(patch, "blocking"))
            for dep in blocking_patch:
                if dep not in backlog_index:
                    print(
                        f"[pending update] blocking references unknown slug: {dep}",
                        file=sys.stderr,
                    )
                    sys.exit(1)
        old_status = item.get("status")
        if "status" in patch:
            _apply_status_transition(
                cast(dict[str, object], item),
                cast(str, patch["status"]),
                "resolved_at",
                "resolved",
            )
        cast(dict[str, object], item).update(patch)
        item["updated"] = today()
        new_status = item.get("status")
        new_rev = bump_rev()
        save_pending(pending_items)
        append_journal_event(
            _journal_entry(
                "update",
                "pending",
                new_rev,
                slug=slug,
                summary=cast(str, item.get("description", "")),
                from_status=old_status if old_status != new_status else None,
                to_status=new_status if old_status != new_status else None,
                fields=sorted(patch),
            ),
            verbose=args.verbose,
        )
        confirm_resolution(
            "pending update",
            args.id,
            item,
            summary_key="description",
            quiet=args.quiet,
        )
        render(items, pending_items, rev=new_rev)
    _maybe_dispatch_recap_regen()


def cmd_pending_list(args: argparse.Namespace) -> None:
    """Handle ``pending list``: print each pending item as one JSON line."""
    for item in load_pending():
        print(json.dumps(item))


def cmd_remove(args: argparse.Namespace) -> None:
    """Handle ``remove``: permanently delete one backlog item.

    Purges the removed slug from every surviving item's ``blocked_by``
    list and every pending item's ``blocking`` list, so deleting a
    completed blocker doesn't retroactively flip its dependents from READY
    into BLOCKED via :func:`effective_blockers`'s missing-slug-is-unresolved
    fallback. The pending-side purge is saved explicitly here —
    :func:`_backlog_mutation`'s cleanup only persists ``items``, so a
    ``blocking``-list edit made only in memory would otherwise vanish the
    moment this process exits, leaving the stale slug on disk for the next
    command to reload.
    """
    with _backlog_mutation("remove", args.id, args.if_rev, verbose=args.verbose) as m:
        m.items = [i for i in m.items if i["id"] != m.slug]
        _purge_inbound_refs({m.slug}, m.items, m.pending_items)
        save_pending(m.pending_items)
        confirm_resolution("remove", args.id, m.item, quiet=args.quiet)


def cmd_prune(args: argparse.Namespace) -> None:
    """Handle ``prune``: permanently remove done/resolved items older than 14 days.

    Backs up each data file before any deletion occurs (see
    :func:`_backup_before_bulk_delete`), purges inbound ``blocked_by``/
    ``blocking`` references to the pruned slugs (same rationale as
    :func:`cmd_remove`), and only bumps the revision if something was
    actually removed.

    Computes both keep-sets and purges inbound refs entirely in memory
    before any file is touched, then bumps the rev and writes each
    changed file exactly once — previously ``items``/``pending_items``
    could each be written twice in one call (once with the raw filtered
    set, again after ref-purging), and the rev was bumped only after all
    of that, so a crash anywhere in the sequence could leave any subset of
    the writes applied under a stale rev. Bumping first means a crash now
    only ever burns a rev number (see :func:`_backlog_mutation`'s
    docstring for the same reasoning applied there).
    """
    cutoff_days = 14

    with backlog_lock():
        items = load_items()
        keep: list[BacklogItem] = []
        pruned_slugs: set[str] = set()
        for item in items:
            if item.get("status") == "done":
                age = _age_days(item.get("completed_at") or item.get("updated", ""))
                if age is None:
                    print(
                        f"[prune] skipping {item.get('id', '?')}: "
                        "no valid completed_at/updated date",
                        file=sys.stderr,
                    )
                elif age >= cutoff_days:
                    pruned_slugs.add(item["id"])
                    continue
            keep.append(item)

        pending_items = load_pending()
        pending_keep: list[PendingItem] = []
        pending_pruned_slugs: set[str] = set()
        for pending_item in pending_items:
            if pending_item.get("status") == "resolved":
                age = _age_days(
                    pending_item.get("resolved_at") or pending_item.get("updated", "")
                )
                if age is None:
                    print(
                        f"[prune] skipping {pending_item.get('id', '?')}: "
                        "no valid resolved_at/updated date",
                        file=sys.stderr,
                    )
                elif age >= cutoff_days:
                    pending_pruned_slugs.add(pending_item["id"])
                    continue
            pending_keep.append(pending_item)

        total_removed = len(pruned_slugs) + len(pending_pruned_slugs)
        if total_removed:
            if pruned_slugs:
                _backup_before_bulk_delete(ITEMS_FILE)
            if pending_pruned_slugs:
                _backup_before_bulk_delete(PENDING_FILE)

            # Purge inbound refs from the surviving records before either
            # write. `blocked_by` values can be backlog slugs *or* pending
            # slugs (cmd_block/cmd_add accept both), so pruning a pending
            # item alone can still leave a stale reference in a surviving
            # backlog item's `blocked_by` — `keep` must be written whenever
            # either set is non-empty, not just when a backlog item itself
            # was pruned. Symmetrically, pending `blocking` lists reference
            # backlog slugs, so pruning a *backlog* item alone can leave a
            # stale reference in a surviving pending item — pending_keep
            # must be written whenever either set is non-empty too (an
            # earlier version's asymmetric check silently dropped this
            # case: `_purge_inbound_refs` would strip the reference in
            # memory but the file on disk kept the stale slug since
            # save_pending was never called for a backlog-only prune).
            _purge_inbound_refs(pruned_slugs | pending_pruned_slugs, keep, pending_keep)

            new_rev = bump_rev()
            if pruned_slugs or pending_pruned_slugs:
                save_items(keep)
                save_pending(pending_keep)
            append_journal_event(
                _journal_entry("prune", "backlog", new_rev, count=total_removed),
                verbose=args.verbose,
            )
            cli_common.qprint(
                f"[prune] removed {len(pruned_slugs)} backlog item(s), "
                f"{len(pending_pruned_slugs)} pending item(s) — "
                f"backup written to {DATA_DIR}",
                quiet=args.quiet,
            )
            # Match every other mutator: render prints the dashboard and the
            # item-map line so a caller's rev stays fresh.
            render(keep, pending_keep, rev=new_rev)
        else:
            print("[prune] nothing to prune")
    _maybe_dispatch_recap_regen()


# ── main ──────────────────────────────────────────────────────────────────────


def _add_id_arg(parser: argparse.ArgumentParser, name: str = "id") -> None:
    """Add the shared ``<slug|N>`` positional id argument to a subcommand parser."""
    parser.add_argument(name, metavar="<slug|N>")


def _add_if_rev_arg(parser: argparse.ArgumentParser, id_name: str = "id") -> None:
    """Add the shared ``--if-rev`` staleness guard to a mutating subparser."""
    parser.add_argument(
        "--if-rev",
        type=int,
        default=None,
        metavar="<N>",
        help=f"required when <{id_name}> is numeric; get the current value from "
        "render/list/show immediately before this call",
    )


dispatch: dict[str, Callable[[argparse.Namespace], None]] = {
    "render": cmd_render,
    "list": cmd_list,
    "show": cmd_show,
    "add": cmd_add,
    "update": cmd_update,
    "start": cmd_start,
    "done": cmd_done,
    "review": cmd_review,
    "approve": cmd_approve,
    "reject": cmd_reject,
    "gate-set": cmd_gate_set,
    "gate-pass": cmd_gate_pass,
    "run": cmd_run,
    "runs": cmd_runs,
    "backfill-gate": cmd_backfill_gate,
    "rename": cmd_rename,
    "remove": cmd_remove,
    "block": cmd_block,
    "unblock": cmd_unblock,
    "prune": cmd_prune,
    "recap": cmd_recap,
}

if __name__ == "__main__" and set(dispatch) != set(SUBCOMMANDS):
    print(
        f"[dev_status] dispatch/SUBCOMMANDS drift: {set(dispatch) ^ set(SUBCOMMANDS)}",
        file=sys.stderr,
    )
    sys.exit(1)


class _CommandSeparatorParser(argparse.ArgumentParser):
    """Subparser class that treats the first bare ``--`` as a hard split.

    Tokens after the first ``--`` belong to the subparser's ``command``
    positional verbatim -- a second literal ``--`` included -- while tokens
    before it parse normally, so flags like ``--timeout``/``-q``/``--if-rev``
    work on either side of the id. Neither stock behavior fits ``run``:
    ``nargs=REMAINDER`` swallows every token the instant it starts matching
    (so ``run <id> --timeout N -- <cmd>`` -- the documented order, and what
    ``pi/extensions/dev-status-tool.ts`` generates -- folded ``--timeout``
    into the command and left it at its default), and ``nargs="*"`` cannot
    re-attach positionals once an optional has matched (the tokens after
    ``--`` came back as extras) while argparse's native ``--`` handling
    strips *every* separator, not just the first. Splitting argv here gives
    both behaviors at once.

    Installed as the subparsers' ``parser_class`` so every leaf parser gets
    it, but it only deviates from stock parsing when the leaf actually
    defines a ``command`` positional -- every other subcommand parses
    exactly as before.
    """

    def parse_known_args(
        self,
        args: list[str] | str | None = None,
        namespace: argparse.Namespace | None = None,
    ) -> tuple[argparse.Namespace, list[str]]:
        if args is None:
            args = sys.argv[1:]
        args = list(args)  # type: ignore[arg-type]
        command = None
        if any(a.dest == "command" for a in self._actions) and "--" in args:
            sep = args.index("--")
            command = args[sep + 1 :]
            args = args[:sep]
        namespace, extras = super().parse_known_args(args, namespace)
        if command is not None:
            namespace.command = command
        return namespace, extras


def build_parser() -> argparse.ArgumentParser:
    """Build the full argument parser for every subcommand.

    Extracted from :func:`main` so tests can exercise parsing (flag
    placement, mutual exclusion) without going through ``sys.argv``.
    """
    parser = argparse.ArgumentParser(
        description="deterministic backlog dashboard v2",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # --quiet/-v are defined once, on every leaf subcommand parser only
    # (via this shared `parents=` parser) -- never on `parser` itself, and
    # never on the intermediate `pending` subparser below. Argparse's
    # subparser dispatch parses the remaining argv into a fresh
    # sub-namespace and unconditionally copies every attribute (including
    # an unset flag's default) back onto the outer namespace, so defining
    # the flags in more than one place along a single dispatch path lets an
    # outer-set value get silently reset by the inner parse.
    verbosity_parent = argparse.ArgumentParser(add_help=False)
    cli_common.add_verbosity_args(verbosity_parent)
    sub = parser.add_subparsers(
        dest="cmd",
        metavar="{" + ",".join(SUBCOMMANDS) + "}",
        parser_class=_CommandSeparatorParser,
    )

    sub.add_parser(
        "render",
        help="render dashboard (pure — no side effects)",
        parents=[verbosity_parent],
    )
    p = sub.add_parser(
        "list",
        help="grouped backlog table (--raw for tab-separated output)",
        parents=[verbosity_parent],
    )
    p.add_argument(
        "--status",
        choices=sorted(VALID_STATUSES),
        default=None,
        help="only show items with this status",
    )
    p.add_argument(
        "--raw",
        action="store_true",
        help="machine-readable TSV (id\\tstatus\\tsummary) instead of the table",
    )

    p = sub.add_parser(
        "show", help="print full JSON for an item", parents=[verbosity_parent]
    )
    _add_id_arg(p)

    p = sub.add_parser(
        "add",
        help="append a new item (id required in JSON)",
        parents=[verbosity_parent],
    )
    p.add_argument(
        "json",
        metavar='\'{"id": "my-slug", "summary": "...", "priority": "high"}\'',
    )

    p = sub.add_parser(
        "update", help="merge JSON patch into an item", parents=[verbosity_parent]
    )
    _add_id_arg(p)
    p.add_argument("patch", metavar='\'{"field": "value", "priority": "high"}\'')
    _add_if_rev_arg(p)

    p = sub.add_parser(
        "start", help="mark item in-progress", parents=[verbosity_parent]
    )
    _add_id_arg(p)
    _add_if_rev_arg(p)
    p.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="force start even if item is actively claimed by another session",
    )
    p.add_argument(
        "--allow-main",
        "--no-worktree-check",
        dest="allow_main",
        action="store_true",
        help="allow starting item from the main/master repository checkout",
    )
    p.add_argument(
        "--claimed-by",
        metavar="HARNESS",
        default=None,
        help="override claimed harness/session identifier",
    )

    p = sub.add_parser("done", help="mark item done", parents=[verbosity_parent])
    _add_id_arg(p)
    _add_if_rev_arg(p)

    p = sub.add_parser(
        "review",
        help="submit (or re-submit) an item for review",
        parents=[verbosity_parent],
    )
    _add_id_arg(p)
    _add_if_rev_arg(p)

    p = sub.add_parser(
        "approve",
        help="approve an in-review item, marking it done",
        parents=[verbosity_parent],
    )
    _add_id_arg(p)
    _add_if_rev_arg(p)

    p = sub.add_parser(
        "reject",
        help="reject an in-review item, sending it back to in-progress",
        parents=[verbosity_parent],
    )
    _add_id_arg(p)
    p.add_argument("feedback", metavar="<feedback>")
    _add_if_rev_arg(p)

    p = sub.add_parser(
        "gate-set",
        help="classify an item's judgment-verification gate",
        parents=[verbosity_parent],
    )
    _add_id_arg(p)
    p.add_argument("json", metavar='\'{"required": true, "criteria": ["..."]}\'')
    _add_if_rev_arg(p)

    p = sub.add_parser(
        "gate-pass",
        help="record that an item's gate criteria are satisfied",
        parents=[verbosity_parent],
    )
    _add_id_arg(p)
    p.add_argument(
        "json",
        metavar='\'{"coverage": {"1": "run:<run_id>" | "manual:<note>"}}\'',
        nargs="?",
        default=None,
        help="per-criterion coverage: run evidence or a manual note per criterion",
    )
    _add_if_rev_arg(p)

    p = sub.add_parser(
        "run",
        help="execute a command and record it as run evidence for an item",
        parents=[verbosity_parent],
    )
    _add_id_arg(p)
    _add_if_rev_arg(p)
    p.add_argument(
        "--timeout",
        type=float,
        default=1800.0,
        metavar="SECONDS",
        help="kill the command after this many seconds (default: 1800)",
    )
    p.add_argument(
        "command",
        # nargs="*" plus the _CommandSeparatorParser dispatch (see its
        # docstring): REMAINDER grabs every remaining token the instant it
        # starts matching -- folding a documented-order `run <id>
        # --timeout N -- <cmd>` into the command itself -- while "*" alone
        # cannot re-attach tokens after an optional and strips every "--"
        # in the stream, not just the first. The parser class owns the
        # split: everything after the first bare "--" lands here verbatim.
        nargs="*",
        metavar="-- <command...>",
        help="command to execute and record (everything after --; no shell)",
    )

    p = sub.add_parser(
        "runs",
        help="list recorded run evidence for an item",
        parents=[verbosity_parent],
    )
    _add_id_arg(p)

    p = sub.add_parser(
        "backfill-gate",
        help="stamp an explicit inert gate on legacy items",
        parents=[verbosity_parent],
    )
    p.add_argument(
        "--apply", action="store_true", help="write changes (default: dry run)"
    )

    p = sub.add_parser(
        "rename",
        help="rename slug (rewrites all references)",
        parents=[verbosity_parent],
    )
    _add_id_arg(p, "old_slug")
    p.add_argument("new_slug")
    _add_if_rev_arg(p, "old_slug")

    p = sub.add_parser(
        "remove",
        help="permanently remove one item by slug or number",
        parents=[verbosity_parent],
    )
    _add_id_arg(p)
    _add_if_rev_arg(p)

    p = sub.add_parser(
        "block", help="add a blocker to an item", parents=[verbosity_parent]
    )
    _add_id_arg(p)
    p.add_argument("blocker", metavar="<blocker-slug>")
    _add_if_rev_arg(p)

    p = sub.add_parser(
        "unblock", help="remove a blocker from an item", parents=[verbosity_parent]
    )
    _add_id_arg(p)
    p.add_argument("blocker", metavar="<blocker-slug>")
    _add_if_rev_arg(p)

    p = sub.add_parser(
        "prune",
        help="permanently remove done/resolved items older than 14 days",
        parents=[verbosity_parent],
    )
    p.add_argument(
        "--force",
        action="store_true",
        required=True,
        help="required to prevent accidental prune",
    )

    p = sub.add_parser(
        "recap",
        help="print a friendly prose recap of recent activity",
        parents=[verbosity_parent],
    )
    p.add_argument(
        "--refresh",
        action="store_true",
        help="bypass the freshness cache and regenerate the recap now",
    )
    p.add_argument(
        "--backend",
        choices=llm_backends.BACKEND_PRIORITY,
        default=None,
        help="force this backend instead of priority-order fallback",
    )

    # No `parents=[verbosity_parent]` here: `pending` has its own nested
    # subparsers below, and attaching the flags at this intermediate level
    # too would reintroduce the same namespace-merge bug one level down.
    pending = sub.add_parser("pending", help="manage pending (waiting-on-reply) items")
    pending_sub = pending.add_subparsers(dest="pending_cmd")

    p = pending_sub.add_parser(
        "add", help="track a new pending item", parents=[verbosity_parent]
    )
    p.add_argument(
        "json",
        metavar='\'{"id", "description", "kind", ["source_ref"], ["context"], '
        '["next_steps"], ["blocking"]}\'',
    )

    p = pending_sub.add_parser(
        "update",
        help="merge a JSON patch into an existing pending item",
        parents=[verbosity_parent],
    )
    _add_id_arg(p)
    p.add_argument("patch", metavar='\'{"status": "reply_received", ...}\'')
    _add_if_rev_arg(p)

    pending_sub.add_parser(
        "list", help="list pending items as JSON lines", parents=[verbosity_parent]
    )

    # Stashed for `main`'s no-pending-subcommand help path -- argparse has
    # no public lookup from a parser back to one of its own subparsers by
    # name.
    parser.pending_parser = pending  # type: ignore[attr-defined]

    # Same nested-subparser shape as `pending` above, and the same reason
    # for omitting `parents=[verbosity_parent]` at this intermediate level.
    out_of_scope = sub.add_parser(
        "out-of-scope",
        help=(
            "record/browse rejected feature concepts (distinct from "
            "'reject', which sends an in-review item back for rework)"
        ),
    )
    oos_sub = out_of_scope.add_subparsers(dest="oos_cmd")

    oos_add = oos_sub.add_parser(
        "add", help="record a rejected concept", parents=[verbosity_parent]
    )
    oos_add.add_argument("concept_slug", metavar="<concept-slug>")
    oos_add.add_argument(
        "--reason-file", dest="reason_file", required=True, metavar="<path>"
    )
    oos_add.add_argument(
        "--related-item",
        dest="related_item",
        action="append",
        metavar="<backlog-slug>",
    )

    oos_link = oos_sub.add_parser(
        "link",
        help="reference a backlog item from a rejected concept",
        parents=[verbosity_parent],
    )
    oos_link.add_argument("concept_slug", metavar="<concept-slug>")
    oos_link.add_argument("backlog_slug", metavar="<backlog-slug>")

    oos_unlink = oos_sub.add_parser(
        "unlink",
        help="remove a backlog-item reference from a rejected concept",
        parents=[verbosity_parent],
    )
    oos_unlink.add_argument("concept_slug", metavar="<concept-slug>")
    oos_unlink.add_argument("backlog_slug", metavar="<backlog-slug>")

    oos_remove = oos_sub.add_parser(
        "remove", help="delete a rejected concept's record", parents=[verbosity_parent]
    )
    oos_remove.add_argument("concept_slug", metavar="<concept-slug>")

    oos_sub.add_parser(
        "list", help="list rejected concepts, newest-first", parents=[verbosity_parent]
    )

    oos_show = oos_sub.add_parser(
        "show",
        help="print a rejected concept's full record",
        parents=[verbosity_parent],
    )
    oos_show.add_argument("concept_slug", metavar="<concept-slug>")

    parser.out_of_scope_parser = out_of_scope  # type: ignore[attr-defined]

    return parser


def main() -> None:
    """Parse argv and dispatch to the matching subcommand handler.

    ``_internal-regen`` is handled off a raw argv check before argparse ever
    runs — it's the hidden re-exec target :func:`_maybe_dispatch_recap_regen`
    spawns for itself, deliberately not a real subcommand (not in
    :data:`SUBCOMMANDS`/``dispatch``, never shown in ``--help``).
    """
    if len(sys.argv) > 1 and sys.argv[1] == "_internal-regen":
        cmd_internal_regen()
        return

    parser = build_parser()
    args = parser.parse_args()

    if args.cmd == "pending":
        pending_dispatch: dict[str, Callable[[argparse.Namespace], None]] = {
            "add": cmd_pending_add,
            "update": cmd_pending_update,
            "list": cmd_pending_list,
        }
        if args.pending_cmd in pending_dispatch:
            pending_dispatch[args.pending_cmd](args)
        else:
            parser.pending_parser.print_help()  # type: ignore[attr-defined]
            sys.exit(1)
    elif args.cmd == "out-of-scope":
        oos_dispatch: dict[str, Callable[[argparse.Namespace], None]] = {
            "add": cmd_out_of_scope_add,
            "link": cmd_out_of_scope_link,
            "unlink": cmd_out_of_scope_unlink,
            "remove": cmd_out_of_scope_remove,
            "list": cmd_out_of_scope_list,
            "show": cmd_out_of_scope_show,
        }
        if args.oos_cmd in oos_dispatch:
            oos_dispatch[args.oos_cmd](args)
        else:
            parser.out_of_scope_parser.print_help()  # type: ignore[attr-defined]
            sys.exit(1)
    elif args.cmd in dispatch:
        dispatch[args.cmd](args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
