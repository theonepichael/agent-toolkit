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

Environment: AGENT_TOOLKIT_TIMING=1 enables operational timing JSONL under
$XDG_STATE_HOME/agent-toolkit/timing.jsonl (default ~/.local/state).
Normal output and exit codes are unchanged; no prompts or argv are recorded.

Requires Python 3.12+.
"""

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import textwrap
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import NoReturn, NotRequired, TextIO, TypedDict, cast

import cli_common
import dev_status_formatting
import dev_status_storage
import llm_backends
from dev_status_mutation import (
    HARNESS_REPO as HARNESS_REPO,
)
from dev_status_mutation import (
    KNOWN_PROJECT_PREFIXES,
    REPO_PREFIXES,
    UNSET,
    BacklogMutationError,
    GatePassRequest,
    GateSetRequest,
    ItemUpdateRequest,
    MutationResult,
    NewItemRequest,
    PendingAddRequest,
    PendingUpdateRequest,
    RevisionConflictError,
    _claim_uses_ttl,
    _claim_within_ttl,
    _concat_order,
    _done_selection,
    _done_selection_stamp,
    _gate_blocks,
    _is_pid_alive,
    _pending_render_order,
    _purge_inbound_refs,
    _render_order,
    _run_state,
    _unified_order,
    add_item,
    add_pending_item,
    approve_item,
    block_item,
    build_index,
    done_item,
    effective_blockers,
    is_worker_safe,
    pass_gate,
    prefix_of,
    reject_item,
    remove_item,
    rename_item,
    require_kind,
    resolve_id,
    review_item,
    run_item,
    set_gate,
    start_item,
    today,
    unblock_item,
    update_item,
    update_pending_item,
    validate_slug,
)
from dev_status_mutation import (
    BacklogTransaction as BacklogTransaction,
)
from dev_status_mutation import (
    ClaimCollisionError as ClaimCollisionError,
)
from dev_status_mutation import (
    CycleError as CycleError,
)
from dev_status_mutation import (
    DuplicateSlugError as DuplicateSlugError,
)
from dev_status_mutation import (
    GateUnmetError as GateUnmetError,
)
from dev_status_mutation import (
    InvalidItemStateError as InvalidItemStateError,
)
from dev_status_mutation import (
    NotFoundError as NotFoundError,
)
from dev_status_mutation import (
    RunResult as RunResult,
)
from dev_status_mutation import (
    ValidationError as ValidationError,
)
from dev_status_mutation import (
    _argv0_basename as _argv0_basename,
)
from dev_status_mutation import (
    _check_claim_collision as _check_claim_collision,
)
from dev_status_mutation import (
    _claim_ttl_seconds as _claim_ttl_seconds,
)
from dev_status_mutation import (
    _detect_harness as _detect_harness,
)
from dev_status_mutation import (
    _find_owner_pid as _find_owner_pid,
)
from dev_status_mutation import (
    _gate_block_message as _gate_block_message,
)
from dev_status_mutation import (
    _gate_pass_refusal as _gate_pass_refusal,
)
from dev_status_mutation import (
    _is_ephemeral_shell as _is_ephemeral_shell,
)
from dev_status_mutation import (
    _is_unanchored_claim as _is_unanchored_claim,
)
from dev_status_mutation import (
    _make_claim as _make_claim,
)
from dev_status_mutation import (
    _pid_namespace_identity as _pid_namespace_identity,
)
from dev_status_mutation import (
    _priority_rank as _priority_rank,
)
from dev_status_mutation import (
    _proc_info as _proc_info,
)
from dev_status_mutation import (
    _repo_root_for_path as _repo_root_for_path,
)
from dev_status_mutation import (
    _validate_run_citation as _validate_run_citation,
)
from dev_status_mutation import (
    detect_cycle as detect_cycle,
)
from dev_status_mutation import (
    enforce_rev_guard as enforce_rev_guard,
)
from dev_status_mutation import (
    mutation_transaction as mutation_transaction,
)

DATA_DIR = dev_status_storage.DATA_DIR
ITEMS_FILE = dev_status_storage.ITEMS_FILE
PENDING_FILE = dev_status_storage.PENDING_FILE
META_FILE = dev_status_storage.META_FILE
LOCK_FILE = dev_status_storage.LOCK_FILE
JOURNAL_FILE = dev_status_storage.JOURNAL_FILE
RUNS_FILE = dev_status_storage.RUNS_FILE
MACHINE_ID_FILE = dev_status_storage.MACHINE_ID_FILE
RECAP_CACHE_FILE = dev_status_storage.RECAP_CACHE_FILE
RECAP_REGEN_LOCK_FILE = dev_status_storage.RECAP_REGEN_LOCK_FILE

OUT_OF_SCOPE_DIR = dev_status_storage.OUT_OF_SCOPE_DIR
OUT_OF_SCOPE_INDEX_FILE = dev_status_storage.OUT_OF_SCOPE_INDEX_FILE
OUT_OF_SCOPE_LOCK_FILE = dev_status_storage.OUT_OF_SCOPE_LOCK_FILE

# ── recap tuning knobs ──────────────────────────────────────────────────────
RECAP_TTL_SECONDS = 30 * 60
RECAP_STALE_MAX_HOURS = 24
RECAP_DISPATCH_WINDOW_HOURS = 48
RECAP_MAX_CHARS = 400
# Sentence-boundary truncation refuses to cut a boundary that would keep less
# than this much of the budget (a 3-char recap from "Hi." + a long run-on is a
# worse failure than a mid-sentence cut -- fall back to the hard cut instead).
RECAP_MIN_KEEP = RECAP_MAX_CHARS // 2
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


def _is_compact(args: argparse.Namespace | None = None) -> bool:
    """True when mutating commands should emit a single-line structured confirmation.

    Compact mode is active when DEVSTATUS_AGENT is truthy or --compact is
    explicitly supplied, unless overridden by --full / --no-compact.
    """
    if args is not None:
        if getattr(args, "full", False) or getattr(args, "no_compact", False):
            return False
        if getattr(args, "compact", False):
            return True
    return _agent_quiet()


def _sanitize_compact_detail(detail: str) -> str:
    """Collapse whitespace, truncate to 200 chars, and escape quotes."""
    cleaned = " ".join(str(detail).split())
    if len(cleaned) > 200:
        cleaned = cleaned[:197] + "..."
    return cleaned.replace('"', '\\"')


def format_compact_confirmation(
    cmd: str,
    slug: str,
    status: str,
    rev: int,
    ref: str | int | None = None,
    detail: str = "",
) -> str:
    """Format a single-line structured confirmation for mutating commands under compact mode."""
    ref_part = f' ref="{ref}"' if ref is not None and str(ref) != slug else ""
    sanitized_detail = _sanitize_compact_detail(detail)
    return (
        f"[{cmd}] slug={slug} status={status} rev={rev}{ref_part} "
        f'detail="{sanitized_detail}"'
    )


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
    "ready",
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
    "worktree",
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


def machine_id() -> str:
    """Return this machine's stable short id, creating it on first use."""
    return dev_status_storage.machine_id(MACHINE_ID_FILE, DATA_DIR)


_machine_id = machine_id


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
                "Create a dedicated worktree first (`python3 ~/.claude/scripts/worktree.py <slug>`), "
                "or pass --allow-main to override.",
                file=sys.stderr,
            )
            sys.exit(1)
    except Exception:
        pass


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
    """Load all backlog items from :data:`ITEMS_FILE`."""
    return dev_status_storage.load_items(ITEMS_FILE)


def _atomic_write_json(path: Path, payload: str, prefix: str) -> None:
    """Write text to ``path`` via a temp file in its directory + ``os.replace``."""
    dev_status_storage.atomic_write_json(path, payload, prefix)


atomic_write_json = _atomic_write_json


def save_items(items: list[BacklogItem]) -> None:
    """Atomically persist ``items`` to :data:`ITEMS_FILE`."""
    dev_status_storage.save_items(items, ITEMS_FILE)


def load_pending() -> list[PendingItem]:
    """Load all pending items from :data:`PENDING_FILE`."""
    return dev_status_storage.load_pending(PENDING_FILE)


def save_pending(pending_items: list[PendingItem]) -> None:
    """Atomically persist ``pending_items`` to :data:`PENDING_FILE`."""
    dev_status_storage.save_pending(pending_items, PENDING_FILE)


@contextmanager
def backlog_lock() -> Iterator[None]:
    """Hold an exclusive lock over a mutating command's full read-modify-write cycle."""
    with dev_status_storage.backlog_lock(DATA_DIR, LOCK_FILE):
        yield


@contextmanager
def out_of_scope_lock() -> Iterator[None]:
    """Hold an exclusive lock over an out-of-scope command's read-modify-write cycle."""
    with dev_status_storage.out_of_scope_lock(OUT_OF_SCOPE_DIR, OUT_OF_SCOPE_LOCK_FILE):
        yield


def load_rev() -> int:
    """Read the current revision counter."""
    return dev_status_storage.load_rev(META_FILE)


def bump_rev() -> int:
    """Increment and persist the revision counter."""
    return dev_status_storage.bump_rev(META_FILE)


def _backup_before_bulk_delete(path: Path) -> None:
    """Snapshot a data file before a filter-based bulk deletion."""
    dev_status_storage.backup_before_bulk_delete(path)


backup_before_bulk_delete = _backup_before_bulk_delete


# ── graph helpers ─────────────────────────────────────────────────────────────


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
    return dev_status_formatting.section_top(title, width)


def _section_bottom(width: int = SECTION_WIDTH) -> str:
    """Render a section's bottom border."""
    return dev_status_formatting.section_bottom(width)


def _ellipsize(text: str, limit: int) -> str:
    """Truncate ``text`` to ``limit`` display chars with a trailing ``…``."""
    return dev_status_formatting.ellipsize(text, limit)


def _project_prefix(slug: str) -> str:
    """Extract canonical project prefix from a backlog item slug.

    Matches against :data:`KNOWN_PROJECT_PREFIXES` first (longest match),
    falling back to the first hyphen-separated segment if a hyphen exists,
    or the empty string for un-prefixed slugs.
    """
    return dev_status_formatting.project_prefix(slug, KNOWN_PROJECT_PREFIXES)


def _project_divider(
    project: str,
    count: int,
    width: int = SECTION_WIDTH,
    color: bool = False,
) -> str:
    """Render a horizontal divider row for a project group within a section."""
    line = dev_status_formatting.project_divider(project, count, width)
    return _colorize(line, _COLORS["dim"], color) if color else line


def _serial_repo_name_for_path(path: str) -> str | None:
    """Canonical repository name for one serial-runner related path.

    Unlike :func:`_repo_name_for_path`, this is an eligibility boundary rather
    than a best-effort reminder. It resolves symlinks (including the final
    component), accepts future leaves by walking to their nearest existing
    ancestor, verifies containment in the discovered worktree, and fails
    closed whenever Git cannot provide both roots unambiguously.
    """
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        return None
    try:
        canonical = candidate.resolve(strict=False)
    except (OSError, RuntimeError):
        return None

    start: Path | None = None
    if canonical.is_dir():
        start = canonical
    elif canonical.is_file():
        start = canonical.parent
    else:
        for ancestor in canonical.parents:
            if ancestor.is_dir():
                start = ancestor
                break
    if start is None:
        return None

    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(start),
                "rev-parse",
                "--path-format=absolute",
                "--show-toplevel",
                "--git-common-dir",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if len(lines) != 2:
        return None
    try:
        worktree_root = Path(lines[0]).resolve(strict=True)
        common_dir = Path(lines[1]).resolve(strict=False)
        canonical.relative_to(worktree_root)
    except (OSError, RuntimeError, ValueError):
        return None
    return common_dir.parent.name or None


def _is_serial_context_artifact(path: str) -> bool:
    """Whether a path is a mandated planning artifact, not a code target."""
    try:
        candidate = Path(path).resolve(strict=False)
        artifact_root = (Path.home() / ".claude" / "data" / "grill").resolve(
            strict=False
        )
        candidate.relative_to(artifact_root)
    except (OSError, RuntimeError, ValueError):
        return False
    return True


def serial_safety(item: BacklogItem) -> tuple[bool, str | None]:
    """Whether one READY item is safe for isolated serial delegation."""
    slug = str(item.get("id", ""))
    prefix = prefix_of(slug)
    repo_for_prefix = {value: key for key, value in REPO_PREFIXES.items()}
    expected_repo = repo_for_prefix.get(prefix)
    if expected_repo is None:
        return False, f"unknown prefix '{prefix}-' has no configured repository"

    related = item.get("related_files")
    if not isinstance(related, list) or not related:
        return False, "related_files must name at least one repository path"

    repositories: set[str] = set()
    for entry in related:
        if not isinstance(entry, dict):
            return False, "related_files contains a malformed entry"
        path = entry.get("path")
        if not isinstance(path, str) or not path or not Path(path).is_absolute():
            return False, "related_files paths must be non-empty absolute strings"
        if _is_serial_context_artifact(path):
            continue
        repo = _serial_repo_name_for_path(path)
        if repo is None:
            return False, f"cannot resolve related_files path to one repository: {path}"
        repositories.add(repo)

    if not repositories:
        return False, "related_files must name at least one target repository path"
    if len(repositories) != 1:
        return False, "related_files resolve to multiple repositories"
    actual_repo = next(iter(repositories))
    if actual_repo != expected_repo:
        return (
            False,
            f"prefix '{prefix}-' maps to {expected_repo}, but related_files resolve to {actual_repo}",
        )
    return True, None


def _repo_name_for_path(path: str) -> str | None:
    """Resolve a file path to the directory name of the git repo containing it.

    Uses ``--git-common-dir`` rather than ``--show-toplevel`` on purpose. Inside
    a worktree, ``--show-toplevel`` returns the *worktree* root, whose basename
    is ``<repo>-<slug>`` and matches nothing in :data:`REPO_PREFIXES`; since
    every item is worked in a worktree per the repo's Git policy, that would
    fail on essentially every add. ``--git-common-dir`` returns the main repo's
    ``.git``, whose parent is the real repo (verified live 2026-09-03).

    Returns ``None`` whenever the answer is not certain -- no such directory,
    git missing or failing, empty output. Callers treat ``None`` as "say
    nothing", never as a finding.
    """
    start: Path | None = None
    candidate = Path(path)
    if candidate.is_dir():
        start = candidate
    else:
        for ancestor in candidate.parents:
            if ancestor.is_dir():
                start = ancestor
                break
    if start is None:
        return None
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(start),
                "rev-parse",
                "--path-format=absolute",
                "--git-common-dir",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    common = result.stdout.strip()
    if not common:
        return None
    return Path(common).parent.name or None


def _prefix_check_reminder(
    item: BacklogItem,
    *,
    cmd: str,
    err: TextIO | None = None,
) -> None:
    """Warn when an item's prefix disagrees with the repo its files live in.

    Advisory only: it never blocks the add and never changes an exit code. It
    speaks only when certain -- exactly one repo resolved, that repo mapped,
    and the prefix genuinely different. Silence is the default, so a printed
    line is always a positive finding.

    Deliberately takes no ``quiet`` parameter, unlike
    :func:`_blocker_check_reminder` and :func:`_out_of_scope_check_reminder`
    beside it. Agents always pass ``DEVSTATUS_AGENT=1``, and an agent is the
    only caller that ever adds an item, so a reminder silenced by quiet mode
    would be invisible to its entire audience. It does reach the agent:
    ``pi/extensions/dev-status-tool.ts`` folds stderr into the tool result on
    success and throws only on a nonzero exit.
    """
    stream = err if err is not None else sys.stderr
    slug = str(item.get("id", ""))
    if not slug:
        return

    names: set[str] = set()
    for entry in item.get("related_files", []):
        if not isinstance(entry, Mapping):
            continue
        raw = str(entry.get("path", "")).strip()
        if not raw:
            continue
        resolved = _repo_name_for_path(raw)
        if resolved:
            names.add(resolved)
    if len(names) != 1:
        return

    repo = names.pop()
    expected = REPO_PREFIXES.get(repo)
    if expected is None or slug.startswith(f"{expected}-"):
        return

    # Longest-first: `iron-lb-x` must report `iron-lb-`, not the `iron-` that a
    # split on the first dash would produce.
    actual = prefix_of(slug)
    print(
        f"[{cmd}] {slug} carries the prefix '{actual}-', but its related_files "
        f"are in {repo}, whose prefix is '{expected}-'. A prefix names the "
        f"target repo so a swarm can scope itself safely. Rename with: "
        f"dev_status.py rename {slug} {expected}-<rest-of-slug>",
        file=stream,
    )


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
    return dev_status_storage.load_out_of_scope_index(OUT_OF_SCOPE_INDEX_FILE)


load_out_of_scope_index = _load_out_of_scope_index


def _save_out_of_scope_index(index: dict[str, dict[str, object]]) -> None:
    """Atomically persist the out-of-scope concept index."""
    dev_status_storage.save_out_of_scope_index(index, OUT_OF_SCOPE_INDEX_FILE)


save_out_of_scope_index = _save_out_of_scope_index


def _out_of_scope_md_path(slug: str) -> Path:
    """Path to a concept's freeform-reason markdown file."""
    return dev_status_storage.out_of_scope_md_path(slug, OUT_OF_SCOPE_DIR)


out_of_scope_md_path = _out_of_scope_md_path


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
    wait_seconds: float | None = None,
    detail: str | None = None,
    diagnostic: bool | None = None,
) -> dict[str, object]:
    """Build one journal entry: a fixed envelope plus structured optionals."""
    return dev_status_storage.journal_entry(
        cmd,
        kind,
        rev,
        slug=slug,
        summary=summary,
        from_status=from_status,
        to_status=to_status,
        fields=fields,
        feedback=feedback,
        count=count,
        wait_seconds=wait_seconds,
        detail=detail,
        diagnostic=diagnostic,
    )


journal_entry = _journal_entry


def append_journal_event(entry: dict[str, object], *, verbose: bool = False) -> None:
    """Append one event to the journal, best-effort."""
    dev_status_storage.append_journal_event(
        entry, journal_file=JOURNAL_FILE, data_dir=DATA_DIR, verbose=verbose
    )


def _parse_journal_ts(raw: object) -> datetime | None:
    """Parse a journal entry's ``ts`` field into an aware UTC ``datetime``."""
    return dev_status_storage.parse_journal_ts(raw)


parse_journal_ts = _parse_journal_ts


# ── run evidence (runs.jsonl sidecar) ──────────────────────────────────────


def load_runs(item: str | None = None) -> list[RunRecord]:
    """Load run-evidence rows from :data:`RUNS_FILE`, optionally for one item."""
    return dev_status_storage.load_runs(item, runs_file=RUNS_FILE)


def write_runs_file(runs: Sequence[RunRecord]) -> None:
    """Atomically rewrite :data:`RUNS_FILE` with ``runs`` (one JSON line each)."""
    dev_status_storage.write_runs_file(runs, runs_file=RUNS_FILE)


def append_run_record(record: RunRecord) -> bool:
    """Append one run-evidence row to :data:`RUNS_FILE` (best-effort)."""
    return dev_status_storage.append_run_record(
        record, runs_file=RUNS_FILE, data_dir=DATA_DIR
    )


def read_journal_entries(
    within_hours: float | None = None, *, verbose: bool = False
) -> list[dict[str, object]]:
    """Read journal entries, optionally filtered to the last ``within_hours``."""
    return dev_status_storage.read_journal_entries(
        within_hours, journal_file=JOURNAL_FILE, verbose=verbose
    )


def _journal_last_entry_within(hours: float) -> bool:
    """Cheap pre-spawn check: does the journal's last entry fall within ``hours``?"""
    return dev_status_storage.journal_last_entry_within(
        hours, journal_file=JOURNAL_FILE
    )


journal_last_entry_within = _journal_last_entry_within


# ── recap: cache + dispatch ─────────────────────────────────────────────────


def _recap_disabled() -> bool:
    """Kill-switch: checked both before dispatching a regen and at display time."""
    return os.environ.get("DEVSTATUS_RECAP_DISABLE") == "1"


def _load_recap_cache() -> dict[str, object] | None:
    """Load ``recap-cache.json``, or ``None`` if missing/corrupt/malformed."""
    return dev_status_storage.load_recap_cache(RECAP_CACHE_FILE)


load_recap_cache = _load_recap_cache


def _save_recap_cache(backend: str, text: str, board_fingerprint: str) -> None:
    """Atomically persist a recap result."""
    dev_status_storage.save_recap_cache(
        backend, text, board_fingerprint, RECAP_CACHE_FILE
    )


save_recap_cache = _save_recap_cache


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

RECAP_PROMPT = dev_status_formatting.RECAP_PROMPT


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
    return dev_status_formatting.render_changelog(entries, _parse_journal_ts)


def _render_done_facts(items: list[BacklogItem]) -> str:
    """Render selected completed items as dated, slug-free prompt facts."""
    return dev_status_formatting.render_done_facts(items, _done_selection_stamp)


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
    return dev_status_formatting.bucket_summary(
        in_progress, ready, blocked, in_review, done, pending
    )


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
    return dev_status_formatting.build_recap_prompt(
        changelog, buckets, completed, RECAP_PROMPT
    )


# Tokens whose trailing '.' does not end a sentence: common abbreviations and
# initials. Heuristic by design -- when in doubt, we do not cut.
def _recap_is_abbrev_boundary(text: str, dot: int) -> bool:
    """True when the '.' at *dot* is abbreviation/initial, not a sentence end."""
    return dev_status_formatting.recap_is_abbrev_boundary(text, dot)


def _recap_last_sentence_cut(text: str, budget: int, min_keep: int) -> int | None:
    """Index just past the last acceptable sentence boundary within *budget*.

    A boundary is one of ``.!?`` followed by whitespace. A ``.`` whose
    preceding token is an abbreviation or a single leading-uppercase initial
    (see :func:`_recap_is_abbrev_boundary`) is not a boundary; ``!`` and ``?``
    are never blocklist-guarded. A boundary that would keep fewer than
    *min_keep* characters is rejected as degenerate. Returns ``None`` when no
    acceptable boundary exists (CJK, run-on text, all-degenerate boundaries).
    """
    return dev_status_formatting.recap_last_sentence_cut(
        text, budget, min_keep, _recap_is_abbrev_boundary
    )


def _normalize_recap_text(raw: str) -> str:
    """Defensively normalize a backend's raw recap output.

    No rejection path -- strips markdown markers and emoji, collapses
    whitespace, and truncates to :data:`RECAP_MAX_CHARS`. An over-budget text
    is cut at the last sentence boundary within the budget (see
    :func:`_recap_last_sentence_cut`); when no acceptable boundary exists it
    falls back to a hard cut plus an ellipsis. An empty result after
    normalization is a legitimate outcome, cached by the caller (see
    :func:`_save_recap_cache`), not an error.
    """
    return dev_status_formatting.normalize_recap_text(
        raw, RECAP_MAX_CHARS, RECAP_MIN_KEEP, _recap_last_sentence_cut
    )


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


def cmd_worktree(args: argparse.Namespace) -> None:
    """Handle ``worktree``: create or reuse a git worktree and bootstrap dependencies."""
    import worktree

    if not args.item and not args.repo:
        print(
            "[dev_status] error: either a backlog item slug/id or --repo must be provided",
            file=sys.stderr,
        )
        sys.exit(2)

    try:
        config = worktree.resolve_worktree_config(
            slug_or_id=args.item,
            repo=args.repo,
            branch=args.branch,
            dest=args.dest,
            skip_bootstrap=args.skip_bootstrap,
            force=args.force,
            quiet=getattr(args, "quiet", False),
        )
        result = worktree.create_and_bootstrap_worktree(config)

        if not getattr(args, "quiet", False):
            for diag in result.diagnostics:
                print(f"[worktree] {diag}", file=sys.stderr)

        if getattr(args, "json", False):
            payload = {
                "worktree_path": str(result.worktree_path),
                "branch": result.branch,
                "reused": result.reused,
                "bootstrap_executed": result.bootstrap_executed,
                "bootstrap_command": result.bootstrap_command,
                "diagnostics": list(result.diagnostics),
            }
            print(json.dumps(payload, indent=2))
        else:
            print(str(result.worktree_path))
    except worktree.WorktreeError as err:
        print(f"[worktree] error: {err}", file=sys.stderr)
        sys.exit(1)


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


def _handle_mutation_error(cmd: str, err: BacklogMutationError) -> NoReturn:
    if (
        isinstance(err, RevisionConflictError)
        and err.items is not None
        and err.pending_items is not None
    ):
        print(err.message, file=sys.stderr)
        render(list(err.items), list(err.pending_items), rev=err.rev)
        sys.exit(err.exit_code)
    print(err.message, file=sys.stderr)
    sys.exit(err.exit_code)


def _render_mutation_result(
    res: MutationResult,
    *,
    announce: bool = False,
    id_arg: str | None = None,
    quiet: bool = False,
    compact: bool | None = None,
) -> None:
    is_compact_run = _agent_quiet() if compact is None else compact
    if is_compact_run:
        line = format_compact_confirmation(
            cmd=res.cmd,
            slug=res.slug,
            status=res.status,
            rev=res.rev,
            ref=res.ref,
            detail=res.detail,
        )
        print(line)
    else:
        for notice in res.notices:
            print(notice, file=sys.stderr)
        if announce:
            confirm_resolution(
                res.cmd,
                id_arg or str(res.ref or res.slug),
                dict(res.item),
                quiet=quiet,
            )
        render(list(res.items), list(res.pending_items), rev=res.rev)
    _maybe_dispatch_recap_regen()


# ── subcommand handlers ───────────────────────────────────────────────────────


def _sweep_dead_claims(items: list[BacklogItem]) -> list[str]:
    """Revert in-progress claims whose owning process is confirmed dead.

    The claim-liveness check in :func:`_check_claim_collision` runs only
    when a *new* ``start`` is attempted on the exact claimed item, so a dead
    session's claim otherwise sits dashboard-visible as IN PROGRESS until a
    fresh start attempt or the full claim TTL elapses. The read paths
    (``render``/``list``/``show``) call this sweep so the reversion that
    used to need a manual ``update <slug> '{"status": "open"}'` happens
    proactively.

    Scope, mirroring :func:`_check_claim_collision`'s own ordering:

    - Only ``status == "in-progress"`` items with a dict ``claimed_by``.
    - Only same-machine claims (``machine_id`` matches); cross-machine
      claims cannot be PID-checked and stay TTL-governed.
    - Durable owner anchors and legacy ``pid``-only claims are PID-checked
      only in the same PID namespace. An equal positive ``owner_pid`` and
      ``pid``, a namespace mismatch, or unavailable namespace identity uses
      the claim activity TTL instead.

    Mutates ``items`` in place for every reverted claim and returns one
    human-readable notice string per reversion (empty list = nothing
    changed); the caller persists and bumps the rev only when notices came
    back. Notices carry the claim's forensics (harness, dead PID,
    ``claimed_at``) since dropping ``claimed_by`` loses that context.
    """
    current_machine = machine_id()
    notices: list[str] = []
    for item in items:
        if item.get("status") != "in-progress":
            continue
        claim = item.get("claimed_by")
        if not isinstance(claim, dict):
            continue
        if str(claim.get("machine_id", "")) != current_machine:
            continue
        claim_harness = str(claim.get("harness", "unknown"))
        claim_pid = (
            int(claim.get("pid") or 0) if str(claim.get("pid", "")).isdigit() else 0
        )
        claim_owner_pid = (
            int(claim.get("owner_pid"))
            if str(claim.get("owner_pid", "")).isdigit()
            else 0
        )
        claim_last_active = str(
            claim.get("last_active") or claim.get("claimed_at") or ""
        )
        ttl_only = _claim_uses_ttl(claim)
        owner_pid = claim_owner_pid if claim_owner_pid > 0 else claim_pid
        if ttl_only:
            if _claim_within_ttl(claim_last_active):
                continue
        elif owner_pid <= 0 or _is_pid_alive(owner_pid):
            continue
        claimed_at = str(claim.get("claimed_at") or "")
        notice = (
            f"[sweep] {item.get('id', '?')} claim by {claim_harness} "
            f"(PID {owner_pid}, claimed {claimed_at}) is dead; reverted to open."
        )
        notices.append(notice)
        item["status"] = "open"
        item.pop("claimed_by", None)
        # Journal write happens here, inside the same ``fcntl.flock`` critical
        # section as the claim mutation — callers all hold :func:`backlog_lock`
        # — so two racing processes can never both sweep and both journal the
        # same stale claim.
        append_journal_event(
            _journal_entry(
                "stale-pid-sweep",
                "backlog",
                load_rev(),
                slug=str(item.get("id", "?")),
                detail=(
                    f"dead-claim sweep reverted {claim_harness} claim "
                    f"(PID {owner_pid}, claimed {claimed_at}) to open"
                ),
                diagnostic=True,
            )
        )
    return notices


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

    One exception to "no other side effects": the claim-liveness sweep
    (:func:`_sweep_dead_claims`) runs under the lock and, when a claim is
    actually reverted, persists the items and bumps the rev before it is
    read — so the printed rev stays self-consistent with the mutated store.
    Nothing is written when every claim is alive.
    """
    with backlog_lock():
        items = load_items()
        pending_items = load_pending()
        notices = _sweep_dead_claims(items)
        if notices:
            bump_rev()
            save_items(items)
        rev = load_rev()
    for notice in notices:
        print(notice, file=sys.stderr)
    render(items, pending_items, rev=rev, dispatch=True)


def cmd_ready(args: argparse.Namespace) -> None:
    """Handle ``ready``: print the READY bucket as a JSON array of full records.

    READY is *computed*, never stored — an item is ready when it is open and
    :func:`effective_blockers` returns nothing for it, which walks the blocker
    graph transitively on every call. A caller that wants that set has to
    either ask this script or reimplement the walk, and a second
    implementation would drift the moment the graph rules changed. So this
    exposes the bucket :func:`_render_order` already builds for the dashboard,
    rather than leaving each consumer to derive it.

    Full records, not ids: the caller this exists for (``swarm_spawn``'s
    scheduler, via ``pi/extensions/swarm-tool.ts``) needs ``related_files`` to
    tell whether two items would edit the same file, and a second round-trip
    per item to fetch that would be both slower and racier.

    Pure — same contract as :func:`cmd_render`, no writes, safe to call on a
    loop while workers are running.
    """
    with backlog_lock():
        items = load_items()
        rev = load_rev()
    print(f"# rev={rev}", file=sys.stderr)

    _in_progress, ready, _blocked, _in_review, _done = _render_order(items)
    if args.prefix:
        ready = [item for item in ready if item["id"].startswith(args.prefix)]
    # Stamped here rather than derived by each consumer: swarm_spawn lives in
    # TypeScript and cannot import this module, and a second copy of the prefix
    # scheme there would drift the moment REPO_PREFIXES changed. Every item
    # carries it, because the consumer fails closed on a missing field.
    stamped: list[dict[str, object]] = []
    for item in ready:
        serial_safe, serial_reason = serial_safety(item)
        record: dict[str, object] = {
            **item,
            "worker_safe": is_worker_safe(prefix_of(str(item["id"]))),
            "serial_safe": serial_safe,
        }
        if serial_reason is not None:
            record["serial_safety_reason"] = serial_reason
        stamped.append(record)
    print(json.dumps(stamped, indent=2))


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
    The claim-liveness sweep runs under the same lock, before the rev is
    read — see :func:`_sweep_dead_claims`.
    """
    with backlog_lock():
        items = load_items()
        pending_items = load_pending()
        notices = _sweep_dead_claims(items)
        if notices:
            bump_rev()
            save_items(items)
        rev = load_rev()
    for notice in notices:
        print(notice, file=sys.stderr)
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
    as :func:`cmd_render`). The claim-liveness sweep runs under the same
    lock, before the rev is read — see :func:`_sweep_dead_claims`.
    """
    with backlog_lock():
        items = load_items()
        notices = _sweep_dead_claims(items)
        if notices:
            bump_rev()
            save_items(items)
        pending_items = load_pending()
        try:
            kind, slug = resolve_id(args.id, items, pending_items)
        except BacklogMutationError as exc:
            _handle_mutation_error("show", exc)
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
        for notice in notices:
            print(notice, file=sys.stderr)
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

    req = NewItemRequest(
        id=slug,
        summary=_str_field(patch, "summary").strip(),
        category=_str_field(patch, "category", "feature"),
        context=_str_field(patch, "context"),
        next_steps=_str_field(patch, "next_steps"),
        related_files=tuple(
            cast(dict[str, object], rf)
            for rf in _list_field(patch, "related_files")
            if isinstance(rf, dict)
        ),
        blocked_by=tuple(str(dep) for dep in _list_field(patch, "blocked_by")),
        priority=cast(str, patch["priority"]) if "priority" in patch else None,
    )
    try:
        res = add_item(req, verbose=getattr(args, "verbose", False))
    except BacklogMutationError as exc:
        _handle_mutation_error("add", exc)

    _render_mutation_result(res, compact=_is_compact(args))
    _blocker_check_reminder(
        list(res.items), res.slug, cmd="add", quiet=getattr(args, "quiet", False)
    )
    _out_of_scope_check_reminder("add", quiet=getattr(args, "quiet", False))
    _prefix_check_reminder(dict(res.item), cmd="add")


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

    unset_priority = "priority" in patch and patch["priority"] is None

    req = ItemUpdateRequest(
        summary=cast(str, patch["summary"]) if "summary" in patch else UNSET,
        category=cast(str, patch["category"]) if "category" in patch else UNSET,
        context=cast(str, patch["context"]) if "context" in patch else UNSET,
        next_steps=cast(str, patch["next_steps"]) if "next_steps" in patch else UNSET,
        related_files=(
            tuple(
                cast(dict[str, object], rf)
                for rf in _list_field(patch, "related_files")
            )
            if "related_files" in patch
            else UNSET
        ),
        status=cast(str, patch["status"]) if "status" in patch else UNSET,
        priority=(
            None
            if unset_priority
            else (cast(str, patch["priority"]) if "priority" in patch else UNSET)
        ),
        claimed_by=(
            cast(dict[str, object], patch["claimed_by"])
            if "claimed_by" in patch
            else UNSET
        ),
    )
    try:
        res = update_item(
            args.id,
            req,
            if_rev=args.if_rev,
            verbose=getattr(args, "verbose", False),
        )
    except BacklogMutationError as exc:
        _handle_mutation_error("update", exc)

    _render_mutation_result(
        res,
        announce=True,
        id_arg=args.id,
        quiet=getattr(args, "quiet", False),
        compact=_is_compact(args),
    )


def cmd_start(args: argparse.Namespace) -> None:
    """Handle ``start``: mark a backlog item in-progress."""
    _check_worktree_guard(
        allow_main=bool(
            getattr(args, "allow_main", False)
            or getattr(args, "no_worktree_check", False)
        ),
        quiet=getattr(args, "quiet", False),
    )
    try:
        res = start_item(
            args.id,
            if_rev=args.if_rev,
            claimed_by=getattr(args, "claimed_by", None),
            force=bool(getattr(args, "force", False)),
            verbose=getattr(args, "verbose", False),
        )
    except BacklogMutationError as exc:
        _handle_mutation_error("start", exc)

    _render_mutation_result(
        res,
        announce=True,
        id_arg=args.id,
        quiet=getattr(args, "quiet", False),
        compact=_is_compact(args),
    )


def cmd_done(args: argparse.Namespace) -> None:
    """Handle ``done``: mark a backlog item done."""
    try:
        res = done_item(
            args.id,
            if_rev=args.if_rev,
            verbose=getattr(args, "verbose", False),
        )
    except BacklogMutationError as exc:
        _handle_mutation_error("done", exc)

    _render_mutation_result(
        res,
        announce=True,
        id_arg=args.id,
        quiet=getattr(args, "quiet", False),
        compact=_is_compact(args),
    )


def cmd_review(args: argparse.Namespace) -> None:
    """Handle ``review``: submit (or re-submit) an item for review.

    Valid from ``in-progress`` (normal submission) or from ``in-review``
    itself (re-pins the content hash after a drift refusal, clearing any
    stale feedback, without changing status).
    """
    try:
        res = review_item(
            args.id,
            if_rev=args.if_rev,
            verbose=getattr(args, "verbose", False),
        )
    except BacklogMutationError as exc:
        _handle_mutation_error("review", exc)

    _render_mutation_result(
        res,
        announce=True,
        id_arg=args.id,
        quiet=getattr(args, "quiet", False),
        compact=_is_compact(args),
    )


def cmd_approve(args: argparse.Namespace) -> None:
    """Handle ``approve``: accept an in-review item, marking it done."""
    try:
        res = approve_item(
            args.id,
            if_rev=args.if_rev,
            verbose=getattr(args, "verbose", False),
        )
    except BacklogMutationError as exc:
        _handle_mutation_error("approve", exc)

    _render_mutation_result(
        res,
        announce=True,
        id_arg=args.id,
        quiet=getattr(args, "quiet", False),
        compact=_is_compact(args),
    )


def cmd_reject(args: argparse.Namespace) -> None:
    """Handle ``reject``: send an in-review item back to in-progress with feedback."""
    feedback = args.feedback.strip()
    if not feedback:
        print("[reject] feedback is required and cannot be empty", file=sys.stderr)
        sys.exit(1)
    try:
        res = reject_item(
            args.id,
            feedback=feedback,
            if_rev=args.if_rev,
            verbose=getattr(args, "verbose", False),
        )
    except BacklogMutationError as exc:
        _handle_mutation_error("reject", exc)

    _render_mutation_result(
        res,
        announce=True,
        id_arg=args.id,
        quiet=getattr(args, "quiet", False),
        compact=_is_compact(args),
    )


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
    criteria = tuple(cast(list[str], criteria_raw))

    if required and not criteria:
        print(
            "[gate-set] 'criteria' cannot be empty when required=true",
            file=sys.stderr,
        )
        sys.exit(1)

    req = GateSetRequest(required=required, criteria=criteria)
    try:
        res = set_gate(
            args.id,
            req,
            if_rev=args.if_rev,
            verbose=getattr(args, "verbose", False),
        )
    except BacklogMutationError as exc:
        _handle_mutation_error("gate-set", exc)

    _render_mutation_result(
        res,
        announce=True,
        id_arg=args.id,
        quiet=getattr(args, "quiet", False),
        compact=_is_compact(args),
    )


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
    coverage_raw = patch.get("coverage") if isinstance(patch, dict) else None
    coverage = (
        {
            str(k): str(v) if isinstance(v, str) else cast(str, v)
            for k, v in coverage_raw.items()
        }
        if isinstance(coverage_raw, dict)
        else None
    )
    req = GatePassRequest(coverage=coverage)
    try:
        res = pass_gate(
            args.id,
            req,
            if_rev=args.if_rev,
            verbose=getattr(args, "verbose", False),
        )
    except BacklogMutationError as exc:
        _handle_mutation_error("gate-pass", exc)

    _render_mutation_result(
        res,
        announce=True,
        id_arg=args.id,
        quiet=getattr(args, "quiet", False),
        compact=_is_compact(args),
    )


def cmd_run(args: argparse.Namespace) -> None:
    """Handle ``run``: execute a command and record it as run evidence.

    Executes ``args.command`` via :func:`subprocess.run` — no shell, output
    inherited (visible live; a 30-minute suite must not look like a hung
    prompt) — then appends one truthful row (exit code, duration, timeout
    flag) to the ``runs.jsonl`` sidecar for ``gate-pass`` to cite.

    Lock scope: the item-existence check and the append each take
    :func:`backlog_lock` briefly; the subprocess itself runs **outside** the
    lock — holding it across a long test run would freeze every other
    harness session sharing the store. A numeric ``id`` is resolved against
    the full pending+backlog pool (matching ``render``'s own numbering) and
    then checked with :func:`require_kind` — resolving against the backlog
    pool alone would silently misnumber every position once any pending item
    exists, since pending items always come first in the dashboard's
    numbering. Like every other numeric-id command, a stale position is caught
    by :func:`enforce_rev_guard` rather than silently attributing this run's
    evidence to the wrong item.
    """
    command = list(args.command or [])
    if not command:
        print(
            "[run] no command given -- usage: dev_status run <id> -- <command...>",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        res = run_item(
            args.id,
            command,
            if_rev=args.if_rev,
            timeout=args.timeout,
            cwd=getattr(args, "cwd", None),
        )
    except BacklogMutationError as exc:
        _handle_mutation_error("run", exc)

    if res.timed_out:
        print(
            f"[run] command timed out after {args.timeout}s",
            file=sys.stderr,
        )
    state = "timed out" if res.timed_out else f"exit {res.exit_code}"
    if res.appended:
        cli_common.qprint(
            f"[run] recorded {res.run_id} for {res.item}: {state} after {res.duration_s}s",
            quiet=args.quiet,
        )
    else:
        print(
            f"[run] ran {res.item}: {state} after {res.duration_s}s -- NOT recorded, "
            "see the error above",
            file=sys.stderr,
        )


def cmd_runs(args: argparse.Namespace) -> None:
    """Handle ``runs``: list an item's recorded run evidence."""
    with backlog_lock():
        items = load_items()
        pending_items = load_pending()
        try:
            kind, slug = resolve_id(args.id, items, pending_items)
            require_kind("runs", args.id, kind, "backlog")
        except BacklogMutationError as exc:
            _handle_mutation_error("runs", exc)
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
    try:
        res = rename_item(
            args.old_slug,
            args.new_slug,
            if_rev=args.if_rev,
            verbose=getattr(args, "verbose", False),
        )
    except BacklogMutationError as exc:
        _handle_mutation_error("rename", exc)

    is_compact_run = _is_compact(args)
    if is_compact_run:
        line = format_compact_confirmation(
            cmd="rename",
            slug=res.slug,
            status=res.status,
            rev=res.rev,
            ref=res.ref,
            detail=res.detail,
        )
        print(line)
    else:
        cli_common.qprint(
            f"[rename] {res.ref} → {res.slug}",
            quiet=(getattr(args, "quiet", False) or _agent_quiet()),
            file=sys.stderr,
        )
        render(list(res.items), list(res.pending_items), rev=res.rev)
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
    try:
        res = block_item(
            args.id,
            args.blocker,
            if_rev=args.if_rev,
            verbose=getattr(args, "verbose", False),
        )
    except BacklogMutationError as exc:
        _handle_mutation_error("block", exc)

    _render_mutation_result(
        res,
        announce=False,
        id_arg=args.id,
        quiet=getattr(args, "quiet", False),
        compact=_is_compact(args),
    )


def cmd_unblock(args: argparse.Namespace) -> None:
    """Handle ``unblock``: remove a blocker from a backlog item."""
    try:
        res = unblock_item(
            args.id,
            args.blocker,
            if_rev=args.if_rev,
            verbose=getattr(args, "verbose", False),
        )
    except BacklogMutationError as exc:
        _handle_mutation_error("unblock", exc)

    _render_mutation_result(
        res,
        announce=False,
        id_arg=args.id,
        quiet=getattr(args, "quiet", False),
        compact=_is_compact(args),
    )


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

    req = PendingAddRequest(
        id=slug,
        description=description,
        kind=kind,
        source_ref=_dict_field(patch, "source_ref"),
        context=_str_field(patch, "context"),
        next_steps=tuple(str(s) for s in _list_field(patch, "next_steps")),
        blocking=tuple(str(dep) for dep in _list_field(patch, "blocking")),
    )
    try:
        res = add_pending_item(req, verbose=getattr(args, "verbose", False))
    except BacklogMutationError as exc:
        _handle_mutation_error("pending add", exc)

    if _is_compact(args):
        line = format_compact_confirmation(
            cmd="pending add",
            slug=res.slug,
            status=res.status,
            rev=res.rev,
            ref=None,
            detail=res.detail,
        )
        print(line)
    else:
        cli_common.qprint(
            f"[pending add] {res.slug} — {res.detail[:60]}",
            quiet=(getattr(args, "quiet", False) or _agent_quiet()),
            file=sys.stderr,
        )
        render(list(res.items), list(res.pending_items), rev=res.rev)
    _maybe_dispatch_recap_regen()
    _blocker_check_reminder(
        list(res.items), None, cmd="pending add", quiet=getattr(args, "quiet", False)
    )
    _out_of_scope_check_reminder("pending add", quiet=getattr(args, "quiet", False))


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

    _reject_null_fields(
        "pending update",
        patch,
        ("description", "context", "next_steps", "blocking", "source_ref"),
    )

    req = PendingUpdateRequest(
        description=(
            cast(str, patch["description"]) if "description" in patch else UNSET
        ),
        context=cast(str, patch["context"]) if "context" in patch else UNSET,
        outcome=cast(str | None, patch["outcome"]) if "outcome" in patch else UNSET,
        status=cast(str, patch["status"]) if "status" in patch else UNSET,
        source_ref=(
            cast(dict[str, object], patch["source_ref"])
            if "source_ref" in patch
            else UNSET
        ),
        next_steps=(
            tuple(str(s) for s in _list_field(patch, "next_steps"))
            if "next_steps" in patch
            else UNSET
        ),
        blocking=(
            tuple(str(dep) for dep in _list_field(patch, "blocking"))
            if "blocking" in patch
            else UNSET
        ),
    )
    try:
        res = update_pending_item(
            args.id,
            req,
            if_rev=args.if_rev,
            verbose=getattr(args, "verbose", False),
        )
    except BacklogMutationError as exc:
        _handle_mutation_error("pending update", exc)

    if _is_compact(args):
        line = format_compact_confirmation(
            cmd="pending update",
            slug=res.slug,
            status=res.status,
            rev=res.rev,
            ref=args.id,
            detail=res.detail,
        )
        print(line)
    else:
        confirm_resolution(
            "pending update",
            args.id,
            dict(res.item),
            summary_key="description",
            quiet=getattr(args, "quiet", False),
        )
        render(list(res.items), list(res.pending_items), rev=res.rev)
    _maybe_dispatch_recap_regen()


def cmd_pending_list(args: argparse.Namespace) -> None:
    """Handle ``pending list``: print each pending item as one JSON line."""
    for item in load_pending():
        print(json.dumps(item))


def cmd_remove(args: argparse.Namespace) -> None:
    """Handle ``remove``: permanently delete one backlog item.

    Purges the removed slug from every surviving item's ``blocked_by`` list
    and every pending item's ``blocking`` list, so deleting a completed
    blocker doesn't retroactively flip its dependents from READY into
    BLOCKED via :func:`effective_blockers`'s missing-slug-is-unresolved
    fallback. The pending-side purge is saved explicitly here —
    :func:`_backlog_mutation`'s cleanup only persists ``items``, so a
    ``blocking``-list edit made only in memory would otherwise vanish the
    moment this process exits, leaving the stale slug on disk for the next
    command to reload.
    """
    try:
        res = remove_item(
            args.id,
            if_rev=args.if_rev,
            verbose=getattr(args, "verbose", False),
        )
    except BacklogMutationError as exc:
        _handle_mutation_error("remove", exc)

    if _is_compact(args):
        line = format_compact_confirmation(
            cmd="remove",
            slug=res.slug,
            status=res.status,
            rev=res.rev,
            ref=res.ref,
            detail=res.detail,
        )
        print(line)
    else:
        confirm_resolution(
            "remove", args.id, dict(res.item), quiet=getattr(args, "quiet", False)
        )
        render(list(res.items), list(res.pending_items), rev=res.rev)
    _maybe_dispatch_recap_regen()


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
    "ready": cmd_ready,
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
    "worktree": cmd_worktree,
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
    compact_parent = argparse.ArgumentParser(add_help=False)
    compact_group = compact_parent.add_mutually_exclusive_group()
    compact_group.add_argument(
        "--compact",
        action="store_true",
        default=False,
        help="single-line structured confirmation on stdout instead of full dashboard",
    )
    compact_group.add_argument(
        "--full",
        "--no-compact",
        dest="full",
        action="store_true",
        default=False,
        help="force full dashboard render even under DEVSTATUS_AGENT=1",
    )
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
        "ready",
        help="print the READY bucket as JSON (open, unblocked, full records)",
        parents=[verbosity_parent],
    )
    p.add_argument(
        "--prefix",
        default=None,
        help="only items whose slug starts with this (e.g. meta-)",
    )

    p = sub.add_parser(
        "show", help="print full JSON for an item", parents=[verbosity_parent]
    )
    _add_id_arg(p)

    p = sub.add_parser(
        "add",
        help="append a new item (id required in JSON)",
        parents=[verbosity_parent, compact_parent],
    )
    p.add_argument(
        "json",
        metavar='\'{"id": "my-slug", "summary": "...", "priority": "high"}\'',
    )

    p = sub.add_parser(
        "update",
        help="merge JSON patch into an item",
        parents=[verbosity_parent, compact_parent],
    )
    _add_id_arg(p)
    p.add_argument("patch", metavar='\'{"field": "value", "priority": "high"}\'')
    _add_if_rev_arg(p)

    p = sub.add_parser(
        "start",
        help="mark item in-progress",
        parents=[verbosity_parent, compact_parent],
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

    p = sub.add_parser(
        "done",
        help="mark item done",
        parents=[verbosity_parent, compact_parent],
    )
    _add_id_arg(p)
    _add_if_rev_arg(p)

    p = sub.add_parser(
        "review",
        help="submit (or re-submit) an item for review",
        parents=[verbosity_parent, compact_parent],
    )
    _add_id_arg(p)
    _add_if_rev_arg(p)

    p = sub.add_parser(
        "approve",
        help="approve an in-review item, marking it done",
        parents=[verbosity_parent, compact_parent],
    )
    _add_id_arg(p)
    _add_if_rev_arg(p)

    p = sub.add_parser(
        "reject",
        help="reject an in-review item, sending it back to in-progress",
        parents=[verbosity_parent, compact_parent],
    )
    _add_id_arg(p)
    p.add_argument("feedback", metavar="<feedback>")
    _add_if_rev_arg(p)

    p = sub.add_parser(
        "gate-set",
        help="classify an item's judgment-verification gate",
        parents=[verbosity_parent, compact_parent],
    )
    _add_id_arg(p)
    p.add_argument("json", metavar='\'{"required": true, "criteria": ["..."]}\'')
    _add_if_rev_arg(p)

    p = sub.add_parser(
        "gate-pass",
        help="record that an item's gate criteria are satisfied",
        parents=[verbosity_parent, compact_parent],
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
        "--cwd",
        type=str,
        default=None,
        metavar="PATH",
        help="working directory for command execution (defaults to repo root of item's related_files, or session cwd)",
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
        parents=[verbosity_parent, compact_parent],
    )
    _add_id_arg(p, "old_slug")
    p.add_argument("new_slug")
    _add_if_rev_arg(p, "old_slug")

    p = sub.add_parser(
        "remove",
        help="permanently remove one item by slug or number",
        parents=[verbosity_parent, compact_parent],
    )
    _add_id_arg(p)
    _add_if_rev_arg(p)

    p = sub.add_parser(
        "block",
        help="add a blocker to an item",
        parents=[verbosity_parent, compact_parent],
    )
    _add_id_arg(p)
    p.add_argument("blocker", metavar="<blocker-slug>")
    _add_if_rev_arg(p)

    p = sub.add_parser(
        "unblock",
        help="remove a blocker from an item",
        parents=[verbosity_parent, compact_parent],
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

    p = sub.add_parser(
        "worktree",
        help="create or reuse a git worktree and bootstrap dependencies",
        parents=[verbosity_parent],
    )
    p.add_argument(
        "item",
        nargs="?",
        default=None,
        help="Backlog item slug, numeric position, or branch name",
    )
    p.add_argument(
        "--repo",
        default=None,
        help="Path to git repository",
    )
    p.add_argument(
        "--branch",
        default=None,
        help="Branch name for worktree",
    )
    p.add_argument(
        "--dest",
        default=None,
        help="Explicit destination path for the worktree",
    )
    p.add_argument(
        "--skip-bootstrap",
        action="store_true",
        default=False,
        help="Skip dependency bootstrapping",
    )
    p.add_argument(
        "--force",
        "-f",
        action="store_true",
        default=False,
        help="Pass --force to git worktree add",
    )
    p.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Emit structured result as JSON",
    )

    # No `parents=[verbosity_parent]` here: `pending` has its own nested
    # subparsers below, and attaching the flags at this intermediate level
    # too would reintroduce the same namespace-merge bug one level down.
    pending = sub.add_parser("pending", help="manage pending (waiting-on-reply) items")
    pending_sub = pending.add_subparsers(dest="pending_cmd")

    p = pending_sub.add_parser(
        "add",
        help="track a new pending item",
        parents=[verbosity_parent, compact_parent],
    )
    p.add_argument(
        "json",
        metavar='\'{"id", "description", "kind", ["source_ref"], ["context"], '
        '["next_steps"], ["blocking"]}\'',
    )

    p = pending_sub.add_parser(
        "update",
        help="merge a JSON patch into an existing pending item",
        parents=[verbosity_parent, compact_parent],
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


@cli_common.timing_span("script", script="dev_status")
def main() -> None:
    """Parse argv and dispatch to the matching subcommand handler.

    ``_internal-regen`` is handled off a raw argv check before argparse ever
    runs — it's the hidden re-exec target :func:`_maybe_dispatch_recap_regen`
    spawns for itself, deliberately not a real subcommand (not in
    :data:`SUBCOMMANDS`/``dispatch``, never shown in ``--help``).
    """
    if len(sys.argv) > 1 and sys.argv[1] == "_internal-regen":
        with cli_common.timing_span(
            "command", script="dev_status", command="_internal-regen"
        ):
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
            with cli_common.timing_span(
                "command",
                script="dev_status",
                command=args.cmd,
                subcommand=args.pending_cmd,
            ):
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
            with cli_common.timing_span(
                "command",
                script="dev_status",
                command=args.cmd,
                subcommand=args.oos_cmd,
            ):
                oos_dispatch[args.oos_cmd](args)
        else:
            parser.out_of_scope_parser.print_help()  # type: ignore[attr-defined]
            sys.exit(1)
    elif args.cmd in dispatch:
        with cli_common.timing_span("command", script="dev_status", command=args.cmd):
            dispatch[args.cmd](args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
