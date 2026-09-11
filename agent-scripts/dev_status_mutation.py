#!/usr/bin/env python3
"""Typed mutation service and transaction manager for dev_status (Candidate 12).

This module extracts the business logic of all sixteen per-item mutating
operations from ``dev_status_impl.py`` into a typed service interface.
Service functions accept typed request dataclasses, enforce invariants,
perform synchronized mutations via ``dev_status_storage`` primitives, and
return structured ``MutationResult`` or ``RunResult`` snapshots.

Errors are communicated exclusively via typed subclasses of
``BacklogMutationError``; the module contains zero ``print`` or ``sys.exit``
calls.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Final, Protocol, cast

import dev_status_formatting
import dev_status_storage

# Re-exported storage types
BacklogItem = dev_status_storage.BacklogItem
PendingItem = dev_status_storage.PendingItem
Gate = dev_status_storage.Gate
RunRecord = dev_status_storage.RunRecord
BacklogIndex = dev_status_storage.BacklogIndex

RenderOrder = tuple[
    list[BacklogItem],
    list[BacklogItem],
    list[BacklogItem],
    list[BacklogItem],
    list[BacklogItem],
]

# ── Constants ─────────────────────────────────────────────────────────────────

VALID_STATUSES: frozenset[str] = frozenset({"open", "in-progress", "in-review", "done"})
VALID_PRIORITIES: frozenset[str] = frozenset({"high", "normal", "low"})
VALID_PENDING_STATUSES: frozenset[str] = frozenset(
    {"waiting_for_reply", "reply_received", "resolved"}
)
VALID_PENDING_KINDS: frozenset[str] = frozenset({"email", "chat", "approval"})

IMMUTABLE_FIELDS: frozenset[str] = frozenset({"id", "created", "completed_at"})
BACKLOG_MUTABLE_FIELDS: frozenset[str] = frozenset(
    {
        "summary",
        "category",
        "status",
        "blocked_by",
        "related_files",
        "context",
        "next_steps",
        "priority",
        "claimed_by",
    }
)
PENDING_MUTABLE_FIELDS: frozenset[str] = frozenset(
    {
        "description",
        "status",
        "context",
        "next_steps",
        "blocking",
        "source_ref",
        "outcome",
    }
)

SUBCOMMANDS: tuple[str, ...] = (
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
)
RESERVED_SLUGS: frozenset[str] = frozenset(
    set(SUBCOMMANDS) | {"pending", "out-of-scope", "all", "help", "new"}
)
SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)+$")
SLUG_MIN = 3
SLUG_MAX = 40

DEFAULT_CLAIM_TTL_SECONDS = 7200
DONE_MAX_ITEMS = 5
_PRIORITY_RANK = {"high": 0, "normal": 1, "low": 2}
KNOWN_PROJECT_PREFIXES = ("iron-lb", "ajhp", "meta", "work")

HARNESS_REPO = "dotfiles"
REPO_PREFIXES: dict[str, str] = {
    "iron-logbook": "iron-lb",
    "agent-toolkit": "atk",
    "dotfiles": "meta",
    "ai-job-hunter-pro": "ajhp",
}
WORKER_SAFE_PREFIXES: frozenset[str] = frozenset(
    prefix for repo, prefix in REPO_PREFIXES.items() if repo != HARNESS_REPO
)

_HASHED_CONTENT_FIELDS = ("summary", "context", "next_steps", "related_files")
_SHELL_BASENAMES = {"sh", "bash", "zsh", "dash", "ksh", "fish", "csh", "tcsh"}
_HARNESS_BASENAMES = {"agy", "claude", "opencode", "pi", "copilot"}
_DAEMON_BASENAMES = {
    "tmux",
    "screen",
    "herdr",
    "sshd",
    "systemd",
    "login",
    "ssh-agent",
    "containerd",
    "dockerd",
}
_OWNER_HOP_CAP = 10
_ANCESTOR_CMD_MAX = 120

# ── Sentinels & Result Types ──────────────────────────────────────────────────


class _Unset:
    def __repr__(self) -> str:
        return "UNSET"


UNSET: Final = _Unset()


@dataclass(frozen=True)
class MutationResult:
    """Carries post-mutation snapshot and metadata for the adapter to render."""

    cmd: str
    slug: str
    status: str
    rev: int
    ref: str | int | None
    detail: str
    item: Mapping[str, object]
    items: Sequence[BacklogItem]
    pending_items: Sequence[PendingItem]
    notices: tuple[str, ...] = ()


@dataclass(frozen=True)
class RunResult:
    """Distinct result shape for cmd_run execution and evidence recording."""

    run_id: str
    item: str
    command: str
    exit_code: int | None
    timed_out: bool
    started_at: str
    duration_s: float
    cwd: str
    appended: bool


# ── Typed Requests ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class NewItemRequest:
    id: str
    summary: str
    category: str = "feature"
    context: str = ""
    next_steps: str = ""
    related_files: tuple[Mapping[str, object], ...] = ()
    blocked_by: tuple[str, ...] = ()
    priority: str | None = None


@dataclass(frozen=True)
class ItemUpdateRequest:
    summary: str | _Unset | None = UNSET
    category: str | _Unset | None = UNSET
    context: str | _Unset | None = UNSET
    next_steps: str | _Unset | None = UNSET
    related_files: tuple[Mapping[str, object], ...] | _Unset = UNSET
    status: str | _Unset | None = UNSET
    priority: str | _Unset | None = UNSET
    claimed_by: Mapping[str, object] | _Unset | None = UNSET


@dataclass(frozen=True)
class GateSetRequest:
    required: bool
    criteria: tuple[str, ...]


@dataclass(frozen=True)
class GatePassRequest:
    coverage: Mapping[str, str] | None = None


@dataclass(frozen=True)
class PendingAddRequest:
    id: str
    description: str
    kind: str
    source_ref: Mapping[str, object] = field(default_factory=dict)
    context: str = ""
    next_steps: tuple[str, ...] = ()
    blocking: tuple[str, ...] = ()


@dataclass(frozen=True)
class PendingUpdateRequest:
    description: str | _Unset | None = UNSET
    context: str | _Unset | None = UNSET
    outcome: str | _Unset | None = UNSET
    status: str | _Unset | None = UNSET
    source_ref: Mapping[str, object] | _Unset | None = UNSET
    next_steps: tuple[str, ...] | _Unset = UNSET
    blocking: tuple[str, ...] | _Unset = UNSET


# ── Error Hierarchy ───────────────────────────────────────────────────────────


class BacklogMutationError(Exception):
    """Base class for all typed mutation refusals."""

    message: str
    exit_code: int = 1
    items: Sequence[BacklogItem] | None = None
    pending_items: Sequence[PendingItem] | None = None
    rev: int | None = None

    def __init__(
        self,
        message: str,
        *,
        exit_code: int = 1,
        items: Sequence[BacklogItem] | None = None,
        pending_items: Sequence[PendingItem] | None = None,
        rev: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code
        self.items = items
        self.pending_items = pending_items
        self.rev = rev


class RevisionConflictError(BacklogMutationError):
    """Refusal when --if-rev is missing or stale on numeric position mutations."""


class GateUnmetError(BacklogMutationError):
    """Refusal when completing an item whose verification gate is unmet."""


class NotFoundError(BacklogMutationError):
    """Refusal when a slug or numeric position cannot be found."""


class ClaimCollisionError(BacklogMutationError):
    """Refusal when start is attempted on an item actively claimed elsewhere."""


class DuplicateSlugError(BacklogMutationError):
    """Refusal when a proposed slug collides with an existing item or pool."""


class InvalidItemStateError(BacklogMutationError):
    """Refusal when an item's current state forbids the requested transition."""


class ValidationError(BacklogMutationError):
    """Refusal when request payload fails semantic validation rules."""


class CycleError(ValidationError):
    """Refusal when adding a dependency would create a cycle."""


# ── Pure Helpers ──────────────────────────────────────────────────────────────


def _resolve_impl_hook(name: str) -> object:
    impl = sys.modules.get("dev_status_impl") or sys.modules.get("dev_status")
    if impl is not None and hasattr(impl, name):
        target = getattr(impl, name)
        if target is not globals().get(name) and target is not getattr(
            dev_status_storage, name, None
        ):
            return target
    return None


def _bump_rev(meta_path: Path | None = None) -> int:
    if meta_path is None:
        hook = _resolve_impl_hook("bump_rev")
        if hook is not None:
            return hook()
    return dev_status_storage.bump_rev(meta_path)


def _save_items(items: list[BacklogItem], items_path: Path | None = None) -> None:
    if items_path is None:
        hook = _resolve_impl_hook("save_items")
        if hook is not None:
            return hook(items)
    return dev_status_storage.save_items(items, items_path)


def _save_pending(pending: list[PendingItem], pending_path: Path | None = None) -> None:
    if pending_path is None:
        hook = _resolve_impl_hook("save_pending")
        if hook is not None:
            return hook(pending)
    return dev_status_storage.save_pending(pending, pending_path)


def _append_journal_event(
    entry: dict[str, object],
    *,
    journal_file: Path | None = None,
    data_dir: Path | None = None,
    verbose: bool = False,
) -> None:
    with contextlib.suppress(OSError):
        dev_status_storage.append_journal_event(
            entry,
            journal_file=journal_file,
            data_dir=data_dir,
            verbose=verbose,
        )


def today() -> str:
    """Return today's date as an ISO-8601 string (``YYYY-MM-DD``)."""
    return date.today().isoformat()


def validate_slug(slug: str, context: str = "") -> str | None:
    """Validate a candidate item slug."""
    prefix = f"[{context}] " if context else ""
    if slug in RESERVED_SLUGS:
        return f"{prefix}slug '{slug}' is a reserved word"
    if not SLUG_RE.match(slug):
        return (
            f"{prefix}invalid slug '{slug}' — must match "
            r"^[a-z0-9]+(-[a-z0-9]+)+$ (lowercase, hyphen-separated segments)"
        )
    if not (SLUG_MIN <= len(slug) <= SLUG_MAX):
        return (
            f"{prefix}slug '{slug}' length {len(slug)} out of range "
            f"[{SLUG_MIN},{SLUG_MAX}]"
        )
    return None


def build_index(items: list[BacklogItem]) -> BacklogIndex:
    """Build a slug -> item lookup for ``items``."""
    return {i["id"]: i for i in items}


def effective_blockers(item: BacklogItem, index: BacklogIndex) -> list[str]:
    """Return ``item``'s ``blocked_by`` slugs whose referent isn't done."""
    bb = item.get("blocked_by", [])
    if not isinstance(bb, list):
        sys.stderr.write(
            f"[effective_blockers] {item.get('id', '?')}.blocked_by is "
            f"{type(bb).__name__}, not list — coercing to []\n"
        )
        return []
    result: list[str] = []
    for s in bb:
        dep = index.get(s)
        if dep is None or dep.get("status") != "done":
            result.append(s)
    return result


def detect_cycle(start: str, new_dep: str, index: BacklogIndex) -> bool:
    """Check whether adding ``new_dep`` as a blocker of ``start`` would cycle."""
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


def prefix_of(slug: str) -> str:
    """The slug's prefix, preferring the longest known one."""
    for known in sorted(REPO_PREFIXES.values(), key=len, reverse=True):
        if slug.startswith(f"{known}-"):
            return known
    return slug.split("-")[0]


def is_worker_safe(prefix: str) -> bool:
    """Whether a swarm worker may be handed items under this prefix."""
    return prefix in WORKER_SAFE_PREFIXES


def _project_prefix(slug: str) -> str:
    """Extract canonical project prefix from a backlog item slug."""
    return dev_status_formatting.project_prefix(slug, KNOWN_PROJECT_PREFIXES)


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
    """Return the normalized completion key for a done item."""
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


def _priority_rank(item: BacklogItem) -> int:
    """Sort rank for ``item``'s priority; lower sorts first."""
    return _PRIORITY_RANK.get(item.get("priority", "normal"), 1)


def _render_order(items: list[BacklogItem]) -> RenderOrder:
    """Bucket and sort backlog items into dashboard render order."""
    index = build_index(items)
    in_progress = sorted(
        [i for i in items if i.get("status") == "in-progress"],
        key=lambda i: i.get("updated", ""),
        reverse=True,
    )
    in_progress = sorted(in_progress, key=_priority_rank)
    open_items = [i for i in items if i.get("status") == "open"]
    ready = sorted(
        [i for i in open_items if not effective_blockers(i, index)],
        key=lambda i: i.get("created", ""),
    )
    ready = sorted(ready, key=_priority_rank)
    ready = sorted(ready, key=lambda i: _project_prefix(i.get("id", "")))
    blocked = sorted(
        [i for i in open_items if effective_blockers(i, index)],
        key=lambda i: (len(effective_blockers(i, index)), i.get("updated", "")),
    )
    blocked = sorted(blocked, key=_priority_rank)
    blocked = sorted(blocked, key=lambda i: _project_prefix(i.get("id", "")))
    in_review = sorted(
        [i for i in items if i.get("status") == "in-review"],
        key=lambda i: i.get("updated", ""),
    )
    in_review = sorted(in_review, key=_priority_rank)
    done = _done_selection(items)
    return in_progress, ready, blocked, in_review, done


def _pending_render_order(
    pending_items: list[PendingItem],
) -> list[PendingItem]:
    """Order unresolved pending items: reply_received group first, each newest-first."""
    unresolved = [p for p in pending_items if p.get("status") != "resolved"]
    by_recency = sorted(unresolved, key=lambda p: p.get("updated", ""), reverse=True)
    return sorted(
        by_recency,
        key=lambda p: 0 if p.get("status") == "reply_received" else 1,
    )


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
    """Resolve a display number or slug to a ``(kind, slug)`` pair."""
    try:
        n = int(arg)
    except ValueError:
        pending_ids = {p["id"] for p in pending_items}
        if arg in pending_ids:
            return "pending", arg
        backlog_ids = {i["id"] for i in items}
        if arg in backlog_ids:
            return "backlog", arg
        raise NotFoundError(f"[resolve] not found: {arg}") from None

    ordered = _unified_order(items, pending_items)
    if not (1 <= n <= len(ordered)):
        raise NotFoundError(f"[resolve] no item at position {n}")
    resolved = ordered[n - 1]
    pending_ids = {p["id"] for p in pending_items}
    kind = "pending" if resolved["id"] in pending_ids else "backlog"
    return kind, resolved["id"]


def require_kind(cmd: str, arg: str, kind: str, expected: str) -> None:
    """Raise ValidationError if ``kind`` doesn't match ``expected``."""
    if kind != expected:
        other = (
            "pending update/list"
            if expected == "backlog"
            else "update/start/done/block/unblock"
        )
        raise ValidationError(
            f"[{cmd}] position {arg} is a {kind} item — use '{other}' instead"
        )


def _apply_status_transition(
    item: dict[str, object], new_status: str, stamp_field: str, done_value: str
) -> None:
    """Stamp or clear a completion timestamp as an item's status changes."""
    old_status = item.get("status")
    if new_status == done_value and old_status != done_value:
        item[stamp_field] = today()
    elif old_status == done_value and new_status != done_value:
        item.pop(stamp_field, None)
    if new_status != "in-progress":
        item.pop("claimed_by", None)


def _content_hash(item: BacklogItem) -> str:
    """SHA-256 over the reviewer-visible content fields, stable serialization."""
    payload = json.dumps(
        {k: cast(dict[str, object], item).get(k) for k in _HASHED_CONTENT_FIELDS},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _gate_blocks(gate: Mapping[str, object] | None) -> bool:
    """Return True if `gate` blocks a done-transition (required, unpassed)."""
    return bool(gate) and bool(gate.get("required")) and gate.get("passed_at") is None


def _gate_block_message(cmd: str, item: BacklogItem) -> str | None:
    """Return a refusal message if ``item``'s gate blocks a done-transition."""
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


def _run_state(run: RunRecord) -> str:
    """Render a run's outcome as one short token (``exit=N``/``timeout``)."""
    if run.get("timed_out"):
        return "timeout"
    return f"exit={run.get('exit_code')}"


def _gate_pass_refusal(slug: str, problems: list[str], runs: list[RunRecord]) -> str:
    """Build a copy-pasteable refusal listing every coverage problem."""
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
    criterion: str,
    run_id: str,
    runs: list[RunRecord],
    gate: Mapping[str, object],
) -> tuple[str | None, dict[str, str] | None]:
    """Validate one ``run:<run_id>`` citation against recorded evidence."""
    run = next((r for r in runs if r.get("run_id") == run_id), None)
    if run is None:
        return (
            (
                f"criterion {criterion}: unknown run id '{run_id}' "
                "(no such run recorded for this item)"
            ),
            None,
        )
    if run.get("timed_out") or run.get("exit_code") != 0:
        return (
            (
                f"criterion {criterion}: cited run {run_id} is failed or "
                f"timed out (exit={run.get('exit_code')}, "
                f"timed_out={run.get('timed_out')})"
            ),
            None,
        )
    started_at = dev_status_storage.parse_journal_ts(run.get("started_at"))
    set_at = dev_status_storage.parse_journal_ts(gate.get("set_at"))
    if started_at is None:
        return (
            f"criterion {criterion}: cited run {run_id} has no parseable started_at",
            None,
        )
    if set_at is not None and started_at < set_at:
        return (
            (
                f"criterion {criterion}: cited run {run_id} is stale "
                f"(started_at {run.get('started_at')} pre-dates gate set_at "
                f"{gate.get('set_at')})"
            ),
            None,
        )
    return None, {"kind": "run", "run_id": run_id}


def _purge_inbound_refs(
    removed_slugs: set[str],
    items: list[BacklogItem],
    pending_items: list[PendingItem],
) -> None:
    """Strip every reference to ``removed_slugs`` from surviving records."""
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


# ── Claim Machinery ───────────────────────────────────────────────────────────


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


def _claim_within_ttl(claim_last_active: str) -> bool:
    """True if a claim's activity stamp is parseable and inside the claim TTL."""
    if not claim_last_active:
        return False
    try:
        ts_str = claim_last_active.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts_str)
    except (ValueError, TypeError):
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    elapsed = (datetime.now(UTC) - dt).total_seconds()
    return elapsed < _claim_ttl_seconds()


def _is_unanchored_claim(claim: dict[str, object]) -> bool:
    """Return whether owner discovery fell back to the invoking child PID."""
    pid = int(claim.get("pid") or 0) if str(claim.get("pid", "")).isdigit() else 0
    owner_pid = (
        int(claim.get("owner_pid")) if str(claim.get("owner_pid", "")).isdigit() else 0
    )
    return pid > 0 and owner_pid == pid


def _pid_namespace_identity() -> str | None:
    """Return the current Linux PID-namespace identity, if observable."""
    hook = _resolve_impl_hook("_pid_namespace_identity")
    if hook is not None:
        return hook()
    try:
        return os.readlink("/proc/self/ns/pid")
    except OSError:
        return None


def _claim_uses_ttl(claim: dict[str, object]) -> bool:
    """Return whether a claim's PIDs cannot safely be checked by this process."""
    if _is_unanchored_claim(claim):
        return True
    if "pid_namespace" not in claim:
        return False
    claim_namespace = claim.get("pid_namespace")
    current_namespace = _pid_namespace_identity()
    return (
        not isinstance(claim_namespace, str)
        or not claim_namespace
        or current_namespace is None
        or claim_namespace != current_namespace
    )


def _is_pid_alive(pid: int) -> bool:
    """Check whether a process with `pid` is currently alive on the local machine."""
    hook = _resolve_impl_hook("_is_pid_alive")
    if hook is not None:
        return hook(pid)
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except ProcessLookupError:
        return False
    except OSError as e:
        import errno

        return e.errno == errno.EPERM
    else:
        return True


def _proc_info(pid: int) -> tuple[int, str] | None:
    """Return (ppid, space-joined cmdline) for pid, or None if unreadable."""
    hook = _resolve_impl_hook("_proc_info")
    if hook is not None:
        return hook(pid)
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        fields = stat[stat.rfind(")") + 2 :].split()
        ppid = int(fields[1])
        cmdline = (
            Path(f"/proc/{pid}/cmdline")
            .read_bytes()
            .replace(b"\x00", b" ")
            .decode("utf-8", "replace")
            .strip()
        )
    except (OSError, ValueError, IndexError):
        pass
    else:
        return (ppid, cmdline)
    try:
        res = subprocess.run(
            ["ps", "-o", "ppid=", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if res.returncode != 0:
            return None
        parts = res.stdout.strip().split(None, 1)
        if len(parts) != 2:
            return None
        return (int(parts[0]), parts[1].strip())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _argv0_basename(cmd: str) -> str:
    """Basename of a cmdline's first token, stripping a leading login dash."""
    argv0 = cmd.split(" ", 1)[0].strip()
    argv0 = argv0.removeprefix("-")
    return os.path.basename(argv0)


def _is_ephemeral_shell(cmd: str) -> bool:
    """Check whether a shell command invocation is an ephemeral subshell/script."""
    tokens = cmd.split()
    if len(tokens) <= 1:
        return False
    for arg in tokens[1:]:
        if arg == "--":
            continue
        if arg.startswith("-") and not arg.startswith("--"):
            if "c" in arg:
                return True
        elif arg.startswith("--command") or not arg.startswith("-"):
            return True
    return False


def _find_owner_pid(
    start_pid: int | None = None,
) -> tuple[int, list[dict[str, object]]]:
    """Find the session-owner PID for a claim, walking the ancestor chain."""
    pid = start_pid if start_pid is not None else os.getpid()
    seen: list[tuple[int, str]] = []
    candidates: list[int] = []
    owner: int | None = None
    cur = pid
    for _ in range(_OWNER_HOP_CAP):
        info = _proc_info(cur)
        if info is None:
            break
        ppid, cmd = info
        seen.append((cur, cmd))
        at_boundary = ppid <= 1
        if cmd:
            base = _argv0_basename(cmd)
            if base in _DAEMON_BASENAMES:
                break
            if base in _HARNESS_BASENAMES and cur != pid:
                owner = cur
                break
            if base in _SHELL_BASENAMES:
                if _is_ephemeral_shell(cmd):
                    if at_boundary:
                        break
                    cur = ppid
                    continue
                if not candidates:
                    owner = cur
                break
            if cur != pid:
                candidates.append(cur)
        if at_boundary:
            break
        cur = ppid
    if owner is None:
        owner = candidates[0] if candidates else pid
    owner_cmd = next((c for p, c in seen if p == owner), "")
    kept = seen[:4]
    ancestors = [{"pid": p, "cmd": c[:_ANCESTOR_CMD_MAX]} for p, c in kept]
    if owner != pid and all(a["pid"] != owner for a in ancestors):
        ancestors.append({"pid": owner, "cmd": owner_cmd[:_ANCESTOR_CMD_MAX]})
    return owner, ancestors


def _detect_harness(explicit: str | None = None) -> str:
    """Detect current agent/environment harness name."""
    if explicit:
        return explicit.strip()
    if env := os.environ.get("DEVSTATUS_HARNESS"):
        return env.strip()
    if os.environ.get("PI_SESSION") or os.environ.get("PI_CODING_AGENT"):
        return "pi"
    if os.environ.get("CLAUDE_CODE") or os.environ.get("ANTHROPIC_CLI"):
        return "claude"
    if (
        os.environ.get("ANTIGRAVITY")
        or os.environ.get("AGY_SESSION")
        or os.environ.get("ANTIGRAVITY_AGENT")
        or os.environ.get("ANTIGRAVITY_CONVERSATION_ID")
        or os.environ.get("AI_AGENT") == "antigravity"
    ):
        return "agy"
    if os.environ.get("OPENCODE_GATEWAY") or os.environ.get("OPENCODE"):
        return "opencode"
    if os.environ.get("GITHUB_COPILOT") or os.environ.get("COPILOT"):
        return "copilot"
    return "cli"


def _make_claim(
    harness: str | None = None,
    *,
    data_dir: Path | None = None,
    machine_id_file: Path | None = None,
) -> dict[str, object]:
    """Create a fresh claim dictionary for the current session."""
    h = _detect_harness(harness)
    now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    owner_pid, ancestors = _find_owner_pid()
    return {
        "harness": h,
        "machine_id": dev_status_storage.machine_id(machine_id_file, data_dir),
        "pid": os.getpid(),
        "owner_pid": owner_pid,
        "pid_namespace": _pid_namespace_identity(),
        "ancestors": ancestors,
        "claimed_at": now_iso,
        "last_active": now_iso,
    }


def _check_claim_collision(
    item: BacklogItem,
    current_harness: str,
    current_machine: str,
    current_pid: int,
    force: bool = False,
    quiet: bool = False,
    current_owner_pid: int = 0,
    *,
    current_rev: int = 0,
    journal_path: Path | None = None,
    verbose: bool = False,
) -> str | None:
    """Check claim collision, raising ClaimCollisionError or returning takeover notice."""
    claim = cast(dict[str, object], item.get("claimed_by"))
    if not isinstance(claim, dict):
        return None
    claim_harness = str(claim.get("harness", "unknown"))
    claim_machine = str(claim.get("machine_id", ""))
    claim_pid = int(claim.get("pid") or 0) if str(claim.get("pid", "")).isdigit() else 0
    claim_owner_pid = (
        int(claim.get("owner_pid")) if str(claim.get("owner_pid", "")).isdigit() else 0
    )
    claim_last_active = str(claim.get("last_active") or claim.get("claimed_at") or "")
    ttl_only = _claim_uses_ttl(claim)

    if (
        not ttl_only
        and claim_machine == current_machine
        and claim_pid == current_pid
        and current_pid > 0
    ):
        return None

    if (
        not ttl_only
        and claim_machine == current_machine
        and claim_owner_pid > 0
        and claim_owner_pid == current_owner_pid
        and current_owner_pid > 0
    ):
        return None

    if force:
        anchor_pid = claim_owner_pid if claim_owner_pid > 0 else claim_pid
        if claim_machine == current_machine:
            stolen_live = (
                _claim_within_ttl(claim_last_active)
                if ttl_only
                else anchor_pid > 0 and _is_pid_alive(anchor_pid)
            )
        else:
            stolen_live = _claim_within_ttl(claim_last_active)
        if stolen_live:
            _append_journal_event(
                dev_status_storage.journal_entry(
                    "claim-theft",
                    "backlog",
                    current_rev,
                    slug=str(item.get("id", "?")),
                    detail=(
                        f"--force took over live {claim_harness} claim "
                        f"(PID {anchor_pid}, machine {claim_machine[:6]})"
                    ),
                    diagnostic=True,
                ),
                journal_file=journal_path,
                verbose=verbose,
            )
        return None

    if claim_machine == current_machine and not ttl_only and claim_owner_pid > 0:
        if _is_pid_alive(claim_owner_pid):
            raise ClaimCollisionError(
                f"[start] {item.get('id', '?')} is actively claimed by {claim_harness} "
                f"(PID {claim_owner_pid} on this machine). Use --force to take over the claim."
            )
        return (
            f"[start] Previous claim by {claim_harness} (PID {claim_owner_pid}) is dead. "
            "Taking over claim."
        )

    if claim_machine == current_machine and not ttl_only and claim_pid > 0:
        if _is_pid_alive(claim_pid):
            raise ClaimCollisionError(
                f"[start] {item.get('id', '?')} is actively claimed by {claim_harness} "
                f"(PID {claim_pid} on this machine). Use --force to take over the claim."
            )
        return (
            f"[start] Previous claim by {claim_harness} (PID {claim_pid}) is dead. "
            "Taking over claim."
        )

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
                raise ClaimCollisionError(
                    f"[start] {item.get('id', '?')} was claimed by {claim_harness} on machine {claim_machine[:6]} "
                    f"{int(elapsed / 60)}m ago (active for {rem_mins}m more). Use --force to take over."
                )
            return (
                f"[start] Previous claim by {claim_harness} expired (idle {int(elapsed / 60)}m). "
                "Taking over claim."
            )
        except (ValueError, TypeError):
            pass
    return None


# ── Path & Execution Helpers ──────────────────────────────────────────────────


def _repo_root_for_path(path: str) -> Path | None:
    """Resolve a path to its enclosing git worktree root."""
    candidate = Path(path).expanduser()
    start: Path | None = candidate if candidate.is_dir() else None
    if start is None:
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
                "--show-toplevel",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode != 0:
            return None
        top = result.stdout.strip()
        if not top:
            return None
        p = Path(top)
        return p if p.is_dir() else None
    except (OSError, subprocess.SubprocessError):
        return None


def _derive_run_cwd(item: BacklogItem | None, explicit_cwd: str | None) -> Path:
    """Determine working directory for dev_status run."""
    if explicit_cwd is not None:
        p = Path(explicit_cwd).expanduser().resolve()
        if not p.is_dir():
            raise ValidationError(
                f"[run] specified cwd '{explicit_cwd}' is not a directory"
            )
        return p

    if item is not None:
        for entry in item.get("related_files", []):
            if not isinstance(entry, Mapping):
                continue
            raw = str(entry.get("path", "")).strip()
            if not raw:
                continue
            repo_root = _repo_root_for_path(raw)
            if repo_root is not None and repo_root.is_dir():
                return repo_root

    return Path.cwd()


def enforce_rev_guard(
    cmd: str,
    id_arg: str,
    if_rev_arg: int | None,
    current_rev: int,
    items: list[BacklogItem],
    pending_items: list[PendingItem],
) -> None:
    """Refuse a numeric-id mutation that lacks a fresh --if-rev."""
    try:
        int(id_arg)
    except (ValueError, TypeError):
        return

    if if_rev_arg is None:
        msg = (
            f"[{cmd}] numeric id '{id_arg}' requires --if-rev <N> to guard "
            f"against a stale position — refusing (no write).\n"
            f"[{cmd}] current rev is {current_rev}. Re-confirm your target "
            f"below, then retry with --if-rev {current_rev}."
        )
        raise RevisionConflictError(
            msg,
            items=items,
            pending_items=pending_items,
            rev=current_rev,
        )

    if if_rev_arg != current_rev:
        msg = (
            f"[{cmd}] stale rev: --if-rev {if_rev_arg} given, current is "
            f"{current_rev} — the backlog changed since you last read it. "
            f"Refusing (no write)."
        )
        raise RevisionConflictError(
            msg,
            items=items,
            pending_items=pending_items,
            rev=current_rev,
        )


def _storage_bundle(items_path: Path | None) -> dict[str, Path | None]:
    """Derive explicit file paths when items_path is given, else None defaults."""
    if items_path is None:
        return {
            "items_path": None,
            "pending_path": None,
            "meta_path": None,
            "lock_file": None,
            "journal_path": None,
            "runs_path": None,
            "data_dir": None,
            "machine_id_file": None,
        }
    data_dir = items_path.parent
    return {
        "items_path": items_path,
        "pending_path": data_dir / "pending_items.json",
        "meta_path": data_dir / "_meta.json",
        "lock_file": data_dir / ".backlog.lock",
        "journal_path": data_dir / "journal.jsonl",
        "runs_path": data_dir / "runs.jsonl",
        "data_dir": data_dir,
        "machine_id_file": data_dir / "_machine_id",
    }


# ── The 16 Service Mutations ──────────────────────────────────────────────────


def add_item(
    request: NewItemRequest,
    *,
    verbose: bool = False,
    items_path: Path | None = None,
) -> MutationResult:
    """Append a new backlog item."""
    slug = request.id.strip()
    if not slug:
        raise ValidationError("[add] 'id' is required")
    err = validate_slug(slug, "add")
    if err:
        raise ValidationError(err)
    if not request.summary.strip():
        raise ValidationError("[add] 'summary' is required")
    if request.priority is not None and request.priority not in VALID_PRIORITIES:
        raise ValidationError(
            f"[add] invalid priority '{request.priority}' — must be one of: "
            f"{', '.join(sorted(VALID_PRIORITIES))}"
        )

    paths = _storage_bundle(items_path)
    with dev_status_storage.backlog_lock(paths["data_dir"], paths["lock_file"]):
        items = dev_status_storage.load_items(paths["items_path"])
        pending_items = dev_status_storage.load_pending(paths["pending_path"])
        index = build_index(items)

        if slug in index:
            raise DuplicateSlugError(f"[add] duplicate slug: {slug}")
        if any(p["id"] == slug for p in pending_items):
            raise DuplicateSlugError(
                f"[add] slug '{slug}' already exists as a pending item — "
                "remove or rename the pending item first"
            )

        pending_ids = {p["id"] for p in pending_items}
        for dep in request.blocked_by:
            if dep not in index and dep not in pending_ids:
                raise ValidationError(
                    f"[add] blocked_by references unknown slug: {dep}"
                )

        item: BacklogItem = {
            "id": slug,
            "created": today(),
            "updated": today(),
            "status": "open",
            "summary": request.summary.strip(),
            "category": request.category,
            "blocked_by": list(request.blocked_by),
            "related_files": [dict(rf) for rf in request.related_files],
            "context": request.context,
            "next_steps": request.next_steps,
        }
        if request.priority is not None:
            item["priority"] = request.priority

        items.append(item)
        new_rev = _bump_rev(paths["meta_path"])
        _save_items(items, paths["items_path"])
        _append_journal_event(
            dev_status_storage.journal_entry(
                "add", "backlog", new_rev, slug=slug, summary=item["summary"]
            ),
            journal_file=paths["journal_path"],
            verbose=verbose,
        )

        return MutationResult(
            cmd="add",
            slug=slug,
            status="open",
            rev=new_rev,
            ref=None,
            detail=item["summary"],
            item=item,
            items=items,
            pending_items=pending_items,
        )


def update_item(
    slug_or_id: str,
    request: ItemUpdateRequest,
    *,
    if_rev: int | None = None,
    verbose: bool = False,
    items_path: Path | None = None,
) -> MutationResult:
    """Merge an update request into a backlog item."""
    # Pre-lock defensive validations
    if (
        request.status is not UNSET
        and request.status is not None
        and request.status not in VALID_STATUSES
    ):
        raise ValidationError(
            f"[update] invalid status '{request.status}' — must be one of: "
            f"{', '.join(sorted(VALID_STATUSES))}"
        )
    if (
        request.priority is not UNSET
        and request.priority is not None
        and request.priority not in VALID_PRIORITIES
    ):
        raise ValidationError(
            f"[update] invalid priority '{request.priority}' — must be one of: "
            f"{', '.join(sorted(VALID_PRIORITIES))}"
        )

    nulled = [
        f
        for f, val in (
            ("summary", request.summary),
            ("category", request.category),
            ("related_files", request.related_files),
            ("context", request.context),
            ("next_steps", request.next_steps),
        )
        if val is None
    ]
    if nulled:
        raise ValidationError(
            f"[update] field(s) cannot be null: {', '.join(sorted(nulled))} — "
            "omit the field to leave it unchanged"
        )

    paths = _storage_bundle(items_path)
    with dev_status_storage.backlog_lock(paths["data_dir"], paths["lock_file"]):
        items = dev_status_storage.load_items(paths["items_path"])
        pending_items = dev_status_storage.load_pending(paths["pending_path"])
        current_rev = dev_status_storage.load_rev(paths["meta_path"])
        enforce_rev_guard(
            "update", slug_or_id, if_rev, current_rev, items, pending_items
        )

        kind, slug = resolve_id(slug_or_id, items, pending_items)
        require_kind("update", slug_or_id, kind, "backlog")

        index = build_index(items)
        item = index.get(slug)
        if item is None:
            raise NotFoundError(f"[update] not found: {slug}")

        old_status = item.get("status")
        fields: list[str] = []

        if request.summary is not UNSET and request.summary is not None:
            item["summary"] = request.summary
            fields.append("summary")
        if request.category is not UNSET and request.category is not None:
            item["category"] = request.category
            fields.append("category")
        if request.context is not UNSET and request.context is not None:
            item["context"] = request.context
            fields.append("context")
        if request.next_steps is not UNSET and request.next_steps is not None:
            item["next_steps"] = request.next_steps
            fields.append("next_steps")
        if request.related_files is not UNSET:
            item["related_files"] = (
                [dict(rf) for rf in request.related_files]
                if request.related_files is not None
                else []
            )
            fields.append("related_files")
        if request.priority is not UNSET:
            fields.append("priority")
            if request.priority is None:
                item.pop("priority", None)
            else:
                item["priority"] = request.priority
        if request.claimed_by is not UNSET:
            fields.append("claimed_by")
            if request.claimed_by is None:
                item.pop("claimed_by", None)
            else:
                item["claimed_by"] = dict(request.claimed_by)
        if request.status is not UNSET and request.status is not None:
            fields.append("status")
            new_st = request.status
            _apply_status_transition(item, new_st, "completed_at", "done")
            if new_st != "in-progress":
                item.pop("claimed_by", None)
            elif (
                new_st == "in-progress"
                and request.claimed_by is UNSET
                and "claimed_by" not in item
            ):
                item["claimed_by"] = _make_claim(
                    data_dir=paths["data_dir"],
                    machine_id_file=paths["machine_id_file"],
                )
            item["status"] = new_st

        item["updated"] = today()
        new_status = item.get("status")
        new_rev = _bump_rev(paths["meta_path"])
        _save_items(items, paths["items_path"])

        _append_journal_event(
            dev_status_storage.journal_entry(
                "update",
                "backlog",
                new_rev,
                slug=slug,
                summary=cast(str, item.get("summary", "")),
                from_status=old_status if old_status != new_status else None,
                to_status=new_status if old_status != new_status else None,
                fields=sorted(fields),
            ),
            journal_file=paths["journal_path"],
            verbose=verbose,
        )

        detail = f"updated {', '.join(sorted(fields))}: {item.get('summary', '')}"
        return MutationResult(
            cmd="update",
            slug=slug,
            status=cast(str, item.get("status", "open")),
            rev=new_rev,
            ref=slug_or_id,
            detail=detail,
            item=item,
            items=items,
            pending_items=pending_items,
        )


def start_item(
    slug_or_id: str,
    *,
    if_rev: int | None = None,
    claimed_by: str | None = None,
    force: bool = False,
    verbose: bool = False,
    items_path: Path | None = None,
) -> MutationResult:
    """Mark a backlog item in-progress."""
    paths = _storage_bundle(items_path)
    with dev_status_storage.backlog_lock(paths["data_dir"], paths["lock_file"]):
        items = dev_status_storage.load_items(paths["items_path"])
        pending_items = dev_status_storage.load_pending(paths["pending_path"])
        current_rev = dev_status_storage.load_rev(paths["meta_path"])
        enforce_rev_guard(
            "start", slug_or_id, if_rev, current_rev, items, pending_items
        )

        kind, slug = resolve_id(slug_or_id, items, pending_items)
        require_kind("start", slug_or_id, kind, "backlog")

        index = build_index(items)
        item = index.get(slug)
        if item is None:
            raise NotFoundError(f"[start] not found: {slug}")

        if item.get("status") == "in-review":
            raise InvalidItemStateError(
                f"[start] {slug} is in-review -- use 'approve <id>' to "
                "accept it or 'reject <id> <feedback>' to send it back to "
                "in-progress."
            )

        mid = dev_status_storage.machine_id(paths["machine_id_file"], paths["data_dir"])
        notice = _check_claim_collision(
            item,
            current_harness=_detect_harness(claimed_by),
            current_machine=mid,
            current_pid=os.getpid(),
            current_owner_pid=_find_owner_pid()[0],
            force=force,
            current_rev=current_rev,
            journal_path=paths["journal_path"],
            verbose=verbose,
        )

        old_status = item.get("status")
        _apply_status_transition(item, "in-progress", "completed_at", "done")
        item["status"] = "in-progress"
        item["claimed_by"] = _make_claim(
            claimed_by,
            data_dir=paths["data_dir"],
            machine_id_file=paths["machine_id_file"],
        )
        item["updated"] = today()

        new_rev = _bump_rev(paths["meta_path"])
        _save_items(items, paths["items_path"])

        _append_journal_event(
            dev_status_storage.journal_entry(
                "start",
                "backlog",
                new_rev,
                slug=slug,
                summary=cast(str, item.get("summary", "")),
                from_status=old_status if old_status != "in-progress" else None,
                to_status="in-progress" if old_status != "in-progress" else None,
            ),
            journal_file=paths["journal_path"],
            verbose=verbose,
        )

        notices = (notice,) if notice else ()
        return MutationResult(
            cmd="start",
            slug=slug,
            status="in-progress",
            rev=new_rev,
            ref=slug_or_id,
            detail=cast(str, item.get("summary", "")),
            item=item,
            items=items,
            pending_items=pending_items,
            notices=notices,
        )


def done_item(
    slug_or_id: str,
    *,
    if_rev: int | None = None,
    verbose: bool = False,
    items_path: Path | None = None,
) -> MutationResult:
    """Mark a backlog item done."""
    paths = _storage_bundle(items_path)
    with dev_status_storage.backlog_lock(paths["data_dir"], paths["lock_file"]):
        items = dev_status_storage.load_items(paths["items_path"])
        pending_items = dev_status_storage.load_pending(paths["pending_path"])
        current_rev = dev_status_storage.load_rev(paths["meta_path"])
        enforce_rev_guard("done", slug_or_id, if_rev, current_rev, items, pending_items)

        kind, slug = resolve_id(slug_or_id, items, pending_items)
        require_kind("done", slug_or_id, kind, "backlog")

        index = build_index(items)
        item = index.get(slug)
        if item is None:
            raise NotFoundError(f"[done] not found: {slug}")

        if item.get("status") == "in-review":
            raise InvalidItemStateError(
                f"[done] {slug} is in-review -- use 'approve <id>' to "
                "complete it or 'reject <id> <feedback>' to send it back to "
                "in-progress."
            )

        gate_msg = _gate_block_message("done", item)
        if gate_msg:
            raise GateUnmetError(gate_msg)

        old_status = item.get("status")
        _apply_status_transition(item, "done", "completed_at", "done")
        item["status"] = "done"
        item["updated"] = today()

        new_rev = _bump_rev(paths["meta_path"])
        _save_items(items, paths["items_path"])

        _append_journal_event(
            dev_status_storage.journal_entry(
                "done",
                "backlog",
                new_rev,
                slug=slug,
                summary=cast(str, item.get("summary", "")),
                from_status=old_status if old_status != "done" else None,
                to_status="done" if old_status != "done" else None,
            ),
            journal_file=paths["journal_path"],
            verbose=verbose,
        )

        return MutationResult(
            cmd="done",
            slug=slug,
            status="done",
            rev=new_rev,
            ref=slug_or_id,
            detail=cast(str, item.get("summary", "")),
            item=item,
            items=items,
            pending_items=pending_items,
        )


def review_item(
    slug_or_id: str,
    *,
    if_rev: int | None = None,
    verbose: bool = False,
    items_path: Path | None = None,
) -> MutationResult:
    """Submit (or re-submit) an item for review."""
    paths = _storage_bundle(items_path)
    with dev_status_storage.backlog_lock(paths["data_dir"], paths["lock_file"]):
        items = dev_status_storage.load_items(paths["items_path"])
        pending_items = dev_status_storage.load_pending(paths["pending_path"])
        current_rev = dev_status_storage.load_rev(paths["meta_path"])
        enforce_rev_guard(
            "review", slug_or_id, if_rev, current_rev, items, pending_items
        )

        kind, slug = resolve_id(slug_or_id, items, pending_items)
        require_kind("review", slug_or_id, kind, "backlog")

        index = build_index(items)
        item = index.get(slug)
        if item is None:
            raise NotFoundError(f"[review] not found: {slug}")

        if item.get("status") not in ("in-progress", "in-review"):
            raise InvalidItemStateError(
                f"[review] {slug} is '{item.get('status')}' -- only an "
                "in-progress (or already in-review) item can be submitted "
                "for review."
            )

        old_status = item.get("status")
        _apply_status_transition(item, "in-review", "completed_at", "done")
        item["status"] = "in-review"
        item["review_content_hash"] = _content_hash(item)
        item.pop("review_feedback", None)
        item["updated"] = today()

        new_rev = _bump_rev(paths["meta_path"])
        _save_items(items, paths["items_path"])

        _append_journal_event(
            dev_status_storage.journal_entry(
                "review",
                "backlog",
                new_rev,
                slug=slug,
                summary=cast(str, item.get("summary", "")),
                from_status=old_status if old_status != "in-review" else None,
                to_status="in-review" if old_status != "in-review" else None,
            ),
            journal_file=paths["journal_path"],
            verbose=verbose,
        )

        return MutationResult(
            cmd="review",
            slug=slug,
            status="in-review",
            rev=new_rev,
            ref=slug_or_id,
            detail=cast(str, item.get("summary", "")),
            item=item,
            items=items,
            pending_items=pending_items,
        )


def approve_item(
    slug_or_id: str,
    *,
    if_rev: int | None = None,
    verbose: bool = False,
    items_path: Path | None = None,
) -> MutationResult:
    """Accept an in-review item, marking it done."""
    paths = _storage_bundle(items_path)
    with dev_status_storage.backlog_lock(paths["data_dir"], paths["lock_file"]):
        items = dev_status_storage.load_items(paths["items_path"])
        pending_items = dev_status_storage.load_pending(paths["pending_path"])
        current_rev = dev_status_storage.load_rev(paths["meta_path"])
        enforce_rev_guard(
            "approve", slug_or_id, if_rev, current_rev, items, pending_items
        )

        kind, slug = resolve_id(slug_or_id, items, pending_items)
        require_kind("approve", slug_or_id, kind, "backlog")

        index = build_index(items)
        item = index.get(slug)
        if item is None:
            raise NotFoundError(f"[approve] not found: {slug}")

        if item.get("status") != "in-review":
            raise InvalidItemStateError(
                f"[approve] {slug} is '{item.get('status')}', not "
                "in-review -- nothing to approve."
            )

        if item.get("review_content_hash") != _content_hash(item):
            raise InvalidItemStateError(
                f"[approve] {slug}'s content changed since it was "
                "submitted for review -- run 'review <id>' again to re-pin "
                "the current content before approving."
            )

        gate_msg = _gate_block_message("approve", item)
        if gate_msg:
            raise GateUnmetError(gate_msg)

        old_status = item.get("status")
        _apply_status_transition(item, "done", "completed_at", "done")
        item["status"] = "done"
        item["updated"] = today()

        new_rev = _bump_rev(paths["meta_path"])
        _save_items(items, paths["items_path"])

        _append_journal_event(
            dev_status_storage.journal_entry(
                "approve",
                "backlog",
                new_rev,
                slug=slug,
                summary=cast(str, item.get("summary", "")),
                from_status=old_status if old_status != "done" else None,
                to_status="done" if old_status != "done" else None,
            ),
            journal_file=paths["journal_path"],
            verbose=verbose,
        )

        return MutationResult(
            cmd="approve",
            slug=slug,
            status="done",
            rev=new_rev,
            ref=slug_or_id,
            detail=cast(str, item.get("summary", "")),
            item=item,
            items=items,
            pending_items=pending_items,
        )


def reject_item(
    slug_or_id: str,
    feedback: str,
    *,
    if_rev: int | None = None,
    verbose: bool = False,
    items_path: Path | None = None,
) -> MutationResult:
    """Send an in-review item back to in-progress with feedback."""
    clean_fb = feedback.strip()
    if not clean_fb:
        raise ValidationError("[reject] feedback is required and cannot be empty")

    paths = _storage_bundle(items_path)
    with dev_status_storage.backlog_lock(paths["data_dir"], paths["lock_file"]):
        items = dev_status_storage.load_items(paths["items_path"])
        pending_items = dev_status_storage.load_pending(paths["pending_path"])
        current_rev = dev_status_storage.load_rev(paths["meta_path"])
        enforce_rev_guard(
            "reject", slug_or_id, if_rev, current_rev, items, pending_items
        )

        kind, slug = resolve_id(slug_or_id, items, pending_items)
        require_kind("reject", slug_or_id, kind, "backlog")

        index = build_index(items)
        item = index.get(slug)
        if item is None:
            raise NotFoundError(f"[reject] not found: {slug}")

        if item.get("status") != "in-review":
            raise InvalidItemStateError(
                f"[reject] {slug} is '{item.get('status')}', not "
                "in-review -- nothing to reject."
            )

        if item.get("review_content_hash") != _content_hash(item):
            raise InvalidItemStateError(
                f"[reject] {slug}'s content changed since it was "
                "submitted for review -- run 'review <id>' again to re-pin "
                "the current content, then reject with feedback that "
                "applies to what's actually there."
            )

        old_status = item.get("status")
        _apply_status_transition(item, "in-progress", "completed_at", "done")
        item["status"] = "in-progress"
        item["review_feedback"] = clean_fb
        item.pop("review_content_hash", None)
        item["updated"] = today()

        new_rev = _bump_rev(paths["meta_path"])
        _save_items(items, paths["items_path"])

        _append_journal_event(
            dev_status_storage.journal_entry(
                "reject",
                "backlog",
                new_rev,
                slug=slug,
                summary=cast(str, item.get("summary", "")),
                from_status=old_status if old_status != "in-progress" else None,
                to_status="in-progress" if old_status != "in-progress" else None,
                feedback=clean_fb,
            ),
            journal_file=paths["journal_path"],
            verbose=verbose,
        )

        return MutationResult(
            cmd="reject",
            slug=slug,
            status="in-progress",
            rev=new_rev,
            ref=slug_or_id,
            detail=cast(str, item.get("summary", "")),
            item=item,
            items=items,
            pending_items=pending_items,
        )


def block_item(
    slug_or_id: str,
    blocker_slug_or_id: str,
    *,
    if_rev: int | None = None,
    verbose: bool = False,
    items_path: Path | None = None,
) -> MutationResult:
    """Add a blocker to a backlog item."""
    blocker = blocker_slug_or_id
    paths = _storage_bundle(items_path)
    with dev_status_storage.backlog_lock(paths["data_dir"], paths["lock_file"]):
        items = dev_status_storage.load_items(paths["items_path"])
        pending_items = dev_status_storage.load_pending(paths["pending_path"])
        current_rev = dev_status_storage.load_rev(paths["meta_path"])
        enforce_rev_guard(
            "block", slug_or_id, if_rev, current_rev, items, pending_items
        )

        kind, slug = resolve_id(slug_or_id, items, pending_items)
        require_kind("block", slug_or_id, kind, "backlog")

        index = build_index(items)
        item = index.get(slug)
        if item is None:
            raise NotFoundError(f"[block] not found: {slug}")

        pending_ids = {p["id"] for p in pending_items}
        if blocker not in index and blocker not in pending_ids:
            raise NotFoundError(f"[block] blocker not found: {blocker}")

        if blocker in item.get("blocked_by", []):
            raise ValidationError(f"[block] {slug} already blocked by {blocker}")

        if detect_cycle(slug, blocker, index):
            raise CycleError(
                f"[block] would create a cycle: {blocker} already depends on {slug}"
            )

        item.setdefault("blocked_by", []).append(blocker)
        item["updated"] = today()

        new_rev = _bump_rev(paths["meta_path"])
        _save_items(items, paths["items_path"])

        _append_journal_event(
            dev_status_storage.journal_entry(
                "block",
                "backlog",
                new_rev,
                slug=slug,
                summary=cast(str, item.get("summary", "")),
            ),
            journal_file=paths["journal_path"],
            verbose=verbose,
        )

        return MutationResult(
            cmd="block",
            slug=slug,
            status=cast(str, item.get("status", "open")),
            rev=new_rev,
            ref=slug_or_id,
            detail=f"blocked by {blocker}",
            item=item,
            items=items,
            pending_items=pending_items,
        )


def unblock_item(
    slug_or_id: str,
    blocker_slug_or_id: str,
    *,
    if_rev: int | None = None,
    verbose: bool = False,
    items_path: Path | None = None,
) -> MutationResult:
    """Remove a blocker from a backlog item."""
    blocker = blocker_slug_or_id
    paths = _storage_bundle(items_path)
    with dev_status_storage.backlog_lock(paths["data_dir"], paths["lock_file"]):
        items = dev_status_storage.load_items(paths["items_path"])
        pending_items = dev_status_storage.load_pending(paths["pending_path"])
        current_rev = dev_status_storage.load_rev(paths["meta_path"])
        enforce_rev_guard(
            "unblock", slug_or_id, if_rev, current_rev, items, pending_items
        )

        kind, slug = resolve_id(slug_or_id, items, pending_items)
        require_kind("unblock", slug_or_id, kind, "backlog")

        index = build_index(items)
        item = index.get(slug)
        if item is None:
            raise NotFoundError(f"[unblock] not found: {slug}")

        if blocker not in item.get("blocked_by", []):
            raise ValidationError(f"[unblock] {slug} is not blocked by {blocker}")

        item["blocked_by"] = [s for s in item.get("blocked_by", []) if s != blocker]
        item["updated"] = today()

        new_rev = _bump_rev(paths["meta_path"])
        _save_items(items, paths["items_path"])

        _append_journal_event(
            dev_status_storage.journal_entry(
                "unblock",
                "backlog",
                new_rev,
                slug=slug,
                summary=cast(str, item.get("summary", "")),
            ),
            journal_file=paths["journal_path"],
            verbose=verbose,
        )

        return MutationResult(
            cmd="unblock",
            slug=slug,
            status=cast(str, item.get("status", "open")),
            rev=new_rev,
            ref=slug_or_id,
            detail=f"unblocked from {blocker}",
            item=item,
            items=items,
            pending_items=pending_items,
        )


def set_gate(
    slug_or_id: str,
    request: GateSetRequest,
    *,
    if_rev: int | None = None,
    verbose: bool = False,
    items_path: Path | None = None,
) -> MutationResult:
    """Classify an item's judgment-step verification gate."""
    if not isinstance(request.required, bool):
        raise ValidationError("[gate-set] 'required' (bool) is required")
    if not all(isinstance(c, str) and c.strip() for c in request.criteria):
        raise ValidationError(
            "[gate-set] 'criteria' must be a list of non-empty strings"
        )
    if request.required and not request.criteria:
        raise ValidationError(
            "[gate-set] 'criteria' cannot be empty when required=true"
        )

    paths = _storage_bundle(items_path)
    with dev_status_storage.backlog_lock(paths["data_dir"], paths["lock_file"]):
        items = dev_status_storage.load_items(paths["items_path"])
        pending_items = dev_status_storage.load_pending(paths["pending_path"])
        current_rev = dev_status_storage.load_rev(paths["meta_path"])
        enforce_rev_guard(
            "gate-set", slug_or_id, if_rev, current_rev, items, pending_items
        )

        kind, slug = resolve_id(slug_or_id, items, pending_items)
        require_kind("gate-set", slug_or_id, kind, "backlog")

        index = build_index(items)
        item = index.get(slug)
        if item is None:
            raise NotFoundError(f"[gate-set] not found: {slug}")

        item["gate"] = {
            "required": request.required,
            "criteria": list(request.criteria),
            "passed_at": None,
            "set_at": datetime.now(UTC).isoformat(),
        }
        item["updated"] = today()

        new_rev = _bump_rev(paths["meta_path"])
        _save_items(items, paths["items_path"])

        _append_journal_event(
            dev_status_storage.journal_entry(
                "gate-set",
                "backlog",
                new_rev,
                slug=slug,
                summary=cast(str, item.get("summary", "")),
            ),
            journal_file=paths["journal_path"],
            verbose=verbose,
        )

        detail = f"gate set ({len(request.criteria)} criteria, required={str(request.required).lower()})"
        return MutationResult(
            cmd="gate-set",
            slug=slug,
            status=cast(str, item.get("status", "open")),
            rev=new_rev,
            ref=slug_or_id,
            detail=detail,
            item=item,
            items=items,
            pending_items=pending_items,
        )


def pass_gate(
    slug_or_id: str,
    request: GatePassRequest,
    *,
    if_rev: int | None = None,
    verbose: bool = False,
    items_path: Path | None = None,
) -> MutationResult:
    """Record that an item's gate criteria are satisfied."""
    paths = _storage_bundle(items_path)
    with dev_status_storage.backlog_lock(paths["data_dir"], paths["lock_file"]):
        items = dev_status_storage.load_items(paths["items_path"])
        pending_items = dev_status_storage.load_pending(paths["pending_path"])
        current_rev = dev_status_storage.load_rev(paths["meta_path"])
        enforce_rev_guard(
            "gate-pass", slug_or_id, if_rev, current_rev, items, pending_items
        )

        kind, slug = resolve_id(slug_or_id, items, pending_items)
        require_kind("gate-pass", slug_or_id, kind, "backlog")

        index = build_index(items)
        item = index.get(slug)
        if item is None:
            raise NotFoundError(f"[gate-pass] not found: {slug}")

        gate = item.get("gate")
        if not gate or not gate.get("required"):
            raise InvalidItemStateError(
                f"[gate-pass] {slug} has no required gate -- nothing to pass"
            )

        if request.coverage is None or not isinstance(request.coverage, Mapping):
            raise ValidationError(
                "[gate-pass] a coverage payload is required: "
                '{"coverage": {"<criterion#>": "run:<run_id>" or "manual:<note>"}} — '
                "every criterion needs recorded run evidence ('dev_status run <id> -- <command...>') or a manual note"
            )

        criteria = cast(list[str], gate.get("criteria", []))
        n = len(criteria)
        runs = dev_status_storage.load_runs(slug, runs_file=paths["runs_path"])
        problems: list[str] = []
        covered: dict[str, dict[str, str]] = {}

        for key, value in request.coverage.items():
            if not isinstance(key, str) or not key.isdigit():
                problems.append(
                    f"coverage key {key!r}: not a criterion number (criteria are 1..{n})"
                )
                continue
            if not 1 <= int(key) <= n:
                problems.append(
                    f"coverage key '{key}': criterion number out of range (criteria are 1..{n})"
                )
                continue
            canonical_key = str(int(key))
            if not isinstance(value, str):
                problems.append(
                    f"criterion {canonical_key}: value must be a string ('run:<run_id>' or 'manual:<note>')"
                )
                continue
            if value.startswith("run:"):
                problem, entry = _validate_run_citation(
                    canonical_key, value[len("run:") :].strip(), runs, gate
                )
                if problem:
                    problems.append(problem)
                else:
                    covered[canonical_key] = cast(dict[str, str], entry)
            elif value.startswith("manual:"):
                note = value[len("manual:") :].strip()
                if not note:
                    problems.append(f"criterion {canonical_key}: empty manual note")
                else:
                    covered[canonical_key] = {"kind": "manual", "note": note}
            else:
                problems.append(
                    f"criterion {canonical_key}: value must be 'run:<run_id>' or 'manual:<note>'"
                )

        for i in range(1, n + 1):
            if str(i) not in covered and not any(
                p.startswith(f"criterion {i}:") for p in problems
            ):
                problems.append(f"criterion {i}: not covered")

        if problems:
            raise ValidationError(_gate_pass_refusal(slug, problems, runs))

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
        item["updated"] = today()

        new_rev = _bump_rev(paths["meta_path"])
        _save_items(items, paths["items_path"])

        _append_journal_event(
            dev_status_storage.journal_entry(
                "gate-pass",
                "backlog",
                new_rev,
                slug=slug,
                summary=cast(str, item.get("summary", "")),
            ),
            journal_file=paths["journal_path"],
            verbose=verbose,
        )

        return MutationResult(
            cmd="gate-pass",
            slug=slug,
            status=cast(str, item.get("status", "open")),
            rev=new_rev,
            ref=slug_or_id,
            detail=f"gate passed via {gate['passed_via']}",
            item=item,
            items=items,
            pending_items=pending_items,
        )


def rename_item(
    old_slug_or_id: str,
    new_slug: str,
    *,
    if_rev: int | None = None,
    verbose: bool = False,
    items_path: Path | None = None,
) -> MutationResult:
    """Rename a slug and rewrite every reference to it across both pools."""
    err = validate_slug(new_slug, "rename")
    if err:
        raise ValidationError(err)

    paths = _storage_bundle(items_path)
    with dev_status_storage.backlog_lock(paths["data_dir"], paths["lock_file"]):
        items = dev_status_storage.load_items(paths["items_path"])
        pending_items = dev_status_storage.load_pending(paths["pending_path"])
        current_rev = dev_status_storage.load_rev(paths["meta_path"])
        enforce_rev_guard(
            "rename", old_slug_or_id, if_rev, current_rev, items, pending_items
        )

        kind, old_slug = resolve_id(old_slug_or_id, items, pending_items)
        require_kind("rename", old_slug_or_id, kind, "backlog")

        index = build_index(items)
        pending_index = {p["id"]: p for p in pending_items}

        if new_slug in index:
            raise DuplicateSlugError(f"[rename] collision: '{new_slug}' already exists")
        if new_slug in pending_index:
            raise DuplicateSlugError(
                f"[rename] collision: '{new_slug}' already exists as a pending item"
            )

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
            p_fields = cast(dict[str, str], p)
            for field in ("description", "context"):
                p_fields[field] = _rewrite_prose(p_fields.get(field, ""))
            for step_idx, step in enumerate(p.get("next_steps", [])):
                if isinstance(step, str):
                    p["next_steps"][step_idx] = _rewrite_prose(step)
            for rf in p.get("related_files", []):
                note = rf.get("note", "") if isinstance(rf, dict) else ""
                if isinstance(note, str) and note:
                    rf["note"] = _rewrite_prose(note)

        new_rev = _bump_rev(paths["meta_path"])
        _save_items(items, paths["items_path"])
        _save_pending(pending_items, paths["pending_path"])

        runs = dev_status_storage.load_runs(runs_file=paths["runs_path"])
        renamed_runs = 0
        for run in runs:
            if run.get("item") == old_slug:
                run["item"] = new_slug
                renamed_runs += 1
        if renamed_runs:
            dev_status_storage.write_runs_file(runs, runs_file=paths["runs_path"])

        renamed_summary = cast(BacklogItem, renamed_item)["summary"]
        _append_journal_event(
            dev_status_storage.journal_entry(
                "rename",
                "backlog",
                new_rev,
                slug=new_slug,
                summary=f"renamed an item ({renamed_summary})",
            ),
            journal_file=paths["journal_path"],
            verbose=verbose,
        )

        return MutationResult(
            cmd="rename",
            slug=new_slug,
            status=cast(str, cast(BacklogItem, renamed_item).get("status", "open")),
            rev=new_rev,
            ref=old_slug,
            detail=f"renamed from {old_slug}",
            item=cast(BacklogItem, renamed_item),
            items=items,
            pending_items=pending_items,
        )


def remove_item(
    slug_or_id: str,
    *,
    if_rev: int | None = None,
    verbose: bool = False,
    items_path: Path | None = None,
) -> MutationResult:
    """Permanently delete one backlog item."""
    paths = _storage_bundle(items_path)
    with dev_status_storage.backlog_lock(paths["data_dir"], paths["lock_file"]):
        items = dev_status_storage.load_items(paths["items_path"])
        pending_items = dev_status_storage.load_pending(paths["pending_path"])
        current_rev = dev_status_storage.load_rev(paths["meta_path"])
        enforce_rev_guard(
            "remove", slug_or_id, if_rev, current_rev, items, pending_items
        )

        kind, slug = resolve_id(slug_or_id, items, pending_items)
        require_kind("remove", slug_or_id, kind, "backlog")

        index = build_index(items)
        item = index.get(slug)
        if item is None:
            raise NotFoundError(f"[remove] not found: {slug}")

        remaining_items = [i for i in items if i["id"] != slug]
        _purge_inbound_refs({slug}, remaining_items, pending_items)
        new_rev = _bump_rev(paths["meta_path"])
        _save_items(remaining_items, paths["items_path"])
        _save_pending(pending_items, paths["pending_path"])

        _append_journal_event(
            dev_status_storage.journal_entry(
                "remove",
                "backlog",
                new_rev,
                slug=slug,
                summary=cast(str, item.get("summary", "")),
            ),
            journal_file=paths["journal_path"],
            verbose=verbose,
        )

        return MutationResult(
            cmd="remove",
            slug=slug,
            status="removed",
            rev=new_rev,
            ref=slug_or_id,
            detail=cast(str, item.get("summary", "")),
            item=item,
            items=remaining_items,
            pending_items=pending_items,
        )


def run_item(
    slug_or_id: str,
    command: Sequence[str],
    *,
    if_rev: int | None = None,
    timeout: float | None = None,
    cwd: str | None = None,
    items_path: Path | None = None,
) -> RunResult:
    """Execute a command and record it as run evidence."""
    cmd_list = list(command)
    if not cmd_list:
        raise ValidationError(
            "[run] no command given -- usage: dev_status run <id> -- <command...>"
        )

    paths = _storage_bundle(items_path)
    # Brief lock 1: resolve + rev guard + item lookup
    with dev_status_storage.backlog_lock(paths["data_dir"], paths["lock_file"]):
        items = dev_status_storage.load_items(paths["items_path"])
        pending_items = dev_status_storage.load_pending(paths["pending_path"])
        current_rev = dev_status_storage.load_rev(paths["meta_path"])
        enforce_rev_guard("run", slug_or_id, if_rev, current_rev, items, pending_items)

        kind, slug = resolve_id(slug_or_id, items, pending_items)
        require_kind("run", slug_or_id, kind, "backlog")

        item = build_index(items).get(slug)

    run_cwd = _derive_run_cwd(item, cwd)
    child_env = {k: v for k, v in os.environ.items() if k != "DEVSTATUS_AGENT"}
    started_at = datetime.now(UTC).isoformat()
    start_mono = time.monotonic()

    # Subprocess execution outside the lock
    try:
        proc = subprocess.run(
            cmd_list,
            timeout=timeout,
            env=child_env,
            cwd=str(run_cwd),
        )
        exit_code: int | None = proc.returncode
        timed_out = False
    except subprocess.TimeoutExpired:
        exit_code, timed_out = None, True

    duration_s = round(time.monotonic() - start_mono, 3)

    record: RunRecord = {
        "run_id": uuid.uuid4().hex,
        "item": slug,
        "command": " ".join(cmd_list),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "started_at": started_at,
        "duration_s": duration_s,
        "cwd": str(run_cwd),
    }

    # Brief lock 2: append evidence row
    with dev_status_storage.backlog_lock(paths["data_dir"], paths["lock_file"]):
        appended = dev_status_storage.append_run_record(
            record, runs_file=paths["runs_path"]
        )

    return RunResult(
        run_id=record["run_id"],
        item=slug,
        command=record["command"],
        exit_code=exit_code,
        timed_out=timed_out,
        started_at=started_at,
        duration_s=duration_s,
        cwd=str(run_cwd),
        appended=appended,
    )


def add_pending_item(
    request: PendingAddRequest,
    *,
    verbose: bool = False,
    items_path: Path | None = None,
) -> MutationResult:
    """Track a new waiting-on-someone-else item."""
    slug = request.id.strip()
    if not slug:
        raise ValidationError("[pending add] 'id' is required")
    err = validate_slug(slug, "pending add")
    if err:
        raise ValidationError(err)

    desc = request.description.strip()
    if not desc:
        raise ValidationError("[pending add] 'description' is required")

    if request.kind not in VALID_PENDING_KINDS:
        raise ValidationError(
            f"[pending add] invalid kind '{request.kind}' — one of: "
            f"{', '.join(sorted(VALID_PENDING_KINDS))}"
        )

    paths = _storage_bundle(items_path)
    with dev_status_storage.backlog_lock(paths["data_dir"], paths["lock_file"]):
        pending_items = dev_status_storage.load_pending(paths["pending_path"])
        if any(p["id"] == slug for p in pending_items):
            raise DuplicateSlugError(f"[pending add] duplicate id: {slug}")

        backlog_items = dev_status_storage.load_items(paths["items_path"])
        index = build_index(backlog_items)
        if slug in index:
            raise DuplicateSlugError(
                f"[pending add] slug '{slug}' already exists as a backlog item — "
                "remove or rename the backlog item first"
            )

        for dep in request.blocking:
            if dep not in index:
                raise ValidationError(
                    f"[pending add] blocking references unknown slug: {dep}"
                )

        item: PendingItem = {
            "id": slug,
            "created": today(),
            "updated": today(),
            "status": "waiting_for_reply",
            "description": desc,
            "kind": request.kind,
            "source_ref": dict(request.source_ref),
            "context": request.context,
            "next_steps": list(request.next_steps),
            "blocking": list(request.blocking),
            "outcome": None,
        }

        pending_items.append(item)
        new_rev = _bump_rev(paths["meta_path"])
        _save_pending(pending_items, paths["pending_path"])

        _append_journal_event(
            dev_status_storage.journal_entry(
                "add", "pending", new_rev, slug=slug, summary=desc
            ),
            journal_file=paths["journal_path"],
            verbose=verbose,
        )

        return MutationResult(
            cmd="pending add",
            slug=slug,
            status="waiting_for_reply",
            rev=new_rev,
            ref=None,
            detail=desc,
            item=item,
            items=backlog_items,
            pending_items=pending_items,
        )


def update_pending_item(
    slug_or_id: str,
    request: PendingUpdateRequest,
    *,
    if_rev: int | None = None,
    verbose: bool = False,
    items_path: Path | None = None,
) -> MutationResult:
    """Merge an update request into a pending item."""
    if (
        request.status is not UNSET
        and request.status is not None
        and request.status not in VALID_PENDING_STATUSES
    ):
        raise ValidationError(
            f"[pending update] invalid status '{request.status}' — one of: "
            f"{', '.join(sorted(VALID_PENDING_STATUSES))}"
        )

    nulled = [
        f
        for f, val in (
            ("description", request.description),
            ("context", request.context),
            ("next_steps", request.next_steps),
            ("blocking", request.blocking),
            ("source_ref", request.source_ref),
        )
        if val is None
    ]
    if nulled:
        raise ValidationError(
            f"[pending update] field(s) cannot be null: {', '.join(sorted(nulled))} — "
            "omit the field to leave it unchanged"
        )

    paths = _storage_bundle(items_path)
    with dev_status_storage.backlog_lock(paths["data_dir"], paths["lock_file"]):
        items = dev_status_storage.load_items(paths["items_path"])
        pending_items = dev_status_storage.load_pending(paths["pending_path"])
        current_rev = dev_status_storage.load_rev(paths["meta_path"])
        enforce_rev_guard(
            "pending update",
            slug_or_id,
            if_rev,
            current_rev,
            items,
            pending_items,
        )

        kind, slug = resolve_id(slug_or_id, items, pending_items)
        require_kind("pending update", slug_or_id, kind, "pending")

        index = {p["id"]: p for p in pending_items}
        item = index.get(slug)
        if item is None:
            raise NotFoundError(f"[pending update] not found: {slug}")

        backlog_index = build_index(items)
        if request.blocking is not UNSET and request.blocking is not None:
            for dep in request.blocking:
                if dep not in backlog_index:
                    raise ValidationError(
                        f"[pending update] blocking references unknown slug: {dep}"
                    )

        old_status = item.get("status")
        fields: list[str] = []

        if request.description is not UNSET and request.description is not None:
            item["description"] = request.description
            fields.append("description")
        if request.context is not UNSET and request.context is not None:
            item["context"] = request.context
            fields.append("context")
        if request.outcome is not UNSET:
            item["outcome"] = request.outcome
            fields.append("outcome")
        if request.source_ref is not UNSET and request.source_ref is not None:
            item["source_ref"] = dict(request.source_ref)
            fields.append("source_ref")
        if request.next_steps is not UNSET and request.next_steps is not None:
            item["next_steps"] = list(request.next_steps)
            fields.append("next_steps")
        if request.blocking is not UNSET and request.blocking is not None:
            item["blocking"] = list(request.blocking)
            fields.append("blocking")
        if request.status is not UNSET and request.status is not None:
            fields.append("status")
            _apply_status_transition(
                cast(dict[str, object], item),
                request.status,
                "resolved_at",
                "resolved",
            )
            item["status"] = request.status

        item["updated"] = today()
        new_status = item.get("status")
        new_rev = _bump_rev(paths["meta_path"])
        _save_pending(pending_items, paths["pending_path"])

        _append_journal_event(
            dev_status_storage.journal_entry(
                "update",
                "pending",
                new_rev,
                slug=slug,
                summary=cast(str, item.get("description", "")),
                from_status=old_status if old_status != new_status else None,
                to_status=new_status if old_status != new_status else None,
                fields=sorted(fields),
            ),
            journal_file=paths["journal_path"],
            verbose=verbose,
        )

        return MutationResult(
            cmd="pending update",
            slug=slug,
            status=cast(str, item.get("status", "")),
            rev=new_rev,
            ref=slug_or_id,
            detail=cast(str, item.get("description", "")),
            item=item,
            items=items,
            pending_items=pending_items,
        )


# ── Candidate 13 Batch Primitive: mutation_transaction ────────────────────────


class BacklogTransaction(Protocol):
    """Transaction interface providing single-lock fail-fast batch adds."""

    def index(self) -> BacklogIndex: ...

    def pending_items(self) -> list[PendingItem]: ...

    def add_item(self, request: NewItemRequest) -> MutationResult: ...


class _BacklogTransactionImpl:
    def __init__(
        self,
        items: list[BacklogItem],
        pending_items: list[PendingItem],
        index: BacklogIndex,
        paths: dict[str, Path | None],
        verbose: bool = False,
    ) -> None:
        self._items = items
        self._pending_items = pending_items
        self._index = index
        self._paths = paths
        self._verbose = verbose

    def index(self) -> BacklogIndex:
        return self._index

    def pending_items(self) -> list[PendingItem]:
        return self._pending_items

    def add_item(self, request: NewItemRequest) -> MutationResult:
        slug = request.id.strip()
        if not slug:
            raise ValidationError("[add] 'id' is required")
        err = validate_slug(slug, "add")
        if err:
            raise ValidationError(err)
        if not request.summary.strip():
            raise ValidationError("[add] 'summary' is required")
        if request.priority is not None and request.priority not in VALID_PRIORITIES:
            raise ValidationError(
                f"[add] invalid priority '{request.priority}' — must be one of: "
                f"{', '.join(sorted(VALID_PRIORITIES))}"
            )

        if slug in self._index:
            raise DuplicateSlugError(f"[add] duplicate slug: {slug}")
        if any(p["id"] == slug for p in self._pending_items):
            raise DuplicateSlugError(
                f"[add] slug '{slug}' already exists as a pending item — "
                "remove or rename the pending item first"
            )

        item: BacklogItem = {
            "id": slug,
            "created": today(),
            "updated": today(),
            "status": "open",
            "summary": request.summary.strip(),
            "category": request.category,
            "blocked_by": list(request.blocked_by),
            "related_files": [dict(rf) for rf in request.related_files],
            "context": request.context,
            "next_steps": request.next_steps,
        }
        if request.priority is not None:
            item["priority"] = request.priority

        self._items.append(item)
        self._index[slug] = item

        new_rev = _bump_rev(self._paths["meta_path"])
        _save_items(self._items, self._paths["items_path"])
        _append_journal_event(
            dev_status_storage.journal_entry(
                "add", "backlog", new_rev, slug=slug, summary=item["summary"]
            ),
            journal_file=self._paths["journal_path"],
            verbose=self._verbose,
        )

        return MutationResult(
            cmd="add",
            slug=slug,
            status="open",
            rev=new_rev,
            ref=None,
            detail=item["summary"],
            item=item,
            items=self._items,
            pending_items=self._pending_items,
        )


@contextmanager
def mutation_transaction(
    *, items_path: Path | None = None, verbose: bool = False
) -> Iterator[BacklogTransaction]:
    """Hold backlog_lock once for batch operations; yields BacklogTransaction."""
    paths = _storage_bundle(items_path)
    with dev_status_storage.backlog_lock(paths["data_dir"], paths["lock_file"]):
        items = dev_status_storage.load_items(paths["items_path"])
        pending_items = dev_status_storage.load_pending(paths["pending_path"])
        index = build_index(items)
        tx = _BacklogTransactionImpl(
            items, pending_items, index, paths, verbose=verbose
        )
        yield tx
