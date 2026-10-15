#!/usr/bin/env python3
r"""Launch supported agents in herdr tabs to work backlog items.

Owns the launch recipe so no session has to re-derive it. It launches only:
``swarm_spawn``/``swarm_poll`` in ``pi/extensions/swarm-tool.ts`` keep owning
spawn, poll, blocked-relay and amend, and this never reimplements them.

Three invariants, each a mistake that actually happened on 2026-09-03 while
doing this by hand:

* ``PI_AGENT_UNATTENDED=1`` goes on ``tab create``, never on ``agent start``.
  ``permission-gate.ts`` reads it at module load, before pi's first tool call,
  and fails closed on anything but exactly ``"1"``. Without it the agent stalls
  on a permission dialog while herdr still reports it as ``working``.
* ``--model`` reaches pi only after a bare ``--``. Handed to ``herdr agent
  start`` directly it is an unknown flag to herdr itself.
* An item whose prefix names the harness repo never goes to a worker, because a
  worker cannot safely edit the code it is running.

That last one is a prefix lookup rather than a per-item classifier: since
``meta-backlog-prefix-repo-alignment``, a prefix names the repo an item
targets, so the safety fact travels in the slug. ``NEVER_SWARMABLE`` derives
from ``dev_status`` rather than repeating it, so the two cannot drift.

Refusing here is a convenience for the human watching, not a guarantee --
``swarm_spawn`` re-reads the READY set on every spawn call, so enforcement
belongs there. That is a separate backlog item.

Known limitation this script cannot detect or route around: when the
calling session is a Claude Code session running in Auto Mode, every
``herdr agent start`` call this module makes -- any ``--kind`` -- can be
denied by Claude Code's own undocumented auto-mode classifier, a gate
independent of settings.json permission rules. See ``claude/commands/
swarm.md``'s "Known limitation" section (generated from
``templates/swarm.md.tmpl``) for the full reproduction and workaround.

A bare ``agent start`` timeout (herdr's own "timed out waiting for agent
startup") has been observed to be transient, so ``spawn_in_new_tab`` retries
it a bounded number of times -- each attempt against a brand new tab/pane,
never the one the timeout gave up on -- before treating it as a hard
failure. Any other failure still fails on the first attempt, unchanged.

``watch --cost-cap-agent``/``--cost-cap-total`` add spend caps measured from
pi's session JSONL -- pi's own price-table estimate, never provider billing.
With a cap set every report carries a ``cost`` object, and a report can fire
(exit 0) with empty ``attention`` and ``gone`` purely for a cost alert:
``over_cap``, or ``unmeasured``/``incomplete`` when a cost cannot be fully
read (fail closed). ``--ack-cost`` takes the alert's printed ``ack`` token,
which pins the reported snapshot; an ack snoozes one more ``--cost-step`` of
spend, or ``--cost-remind-s`` when the spend is not observable.

Usage:
    herdr_delegate.py plan
    herdr_delegate.py launch --slug <slug> [--model <model>]
                             [--kind {pi,copilot,agy,codex}] [--handoff]
    herdr_delegate.py launch --swarm <N> --prefix <prefix> [--model <model>]
                             [--kind {pi,copilot}]
    herdr_delegate.py launch --serial --prefix <prefix> [--model <model>]
                             [--kind {pi,copilot}]
    herdr_delegate.py watch <agent>... [--prefix <prefix>] --state <file>
                             [--ack <name>=<seq>]... [--ack-gone <name>]...
                             [--grace-s <n>] [--timeout-s <n>] [--poll-s <n>]
                             [--strict]
                             [--cost-cap-agent <usd>] [--cost-cap-total <usd>]
                             [--cost-step <usd>] [--cost-remind-s <n>]
                             [--ack-cost <token>]...
    herdr_delegate.py answer <agent> --expect-pane <pane>
                             (--option <label> | --index <n> | --other --text <text>)
                             [--settle-s <n>]
    herdr_delegate.py restart --swarm <N> --prefix <prefix> [--run-id <runId>]
                              [--model <model>] [--kind {pi,copilot}]
    herdr_delegate.py restart --serial --prefix <prefix> [--run-id <runId>]
                              [--model <model>] [--kind {pi,copilot}]
    herdr_delegate.py probe --kind {pi,copilot,agy,codex} [--model <model>]
                            [--cwd <cwd>]
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import datetime
from datetime import time as dt_time
from pathlib import Path

# Deliberately NOT .resolve()'d: dev_status.py and this script may live in
# different repos' checkouts, so resolving the symlink back to its own
# (dev_status.py-less) agent-scripts/ would break the import. Path(__file__).parent stays
# at the LIVE installed directory (~/.agent-toolkit/scripts/) instead, where both
# files are siblings regardless of which repo's checkout each one symlinks
# back to -- Python's import machinery follows a module's symlink itself,
# same as any other file open.
sys.path.insert(0, str(Path(__file__).parent))

import agent_toolkit_paths  # noqa: E402
import agy_workspace_trust  # noqa: E402
import analyze_sessions  # noqa: E402
import cli_common  # noqa: E402
import herdr_picker  # noqa: E402
from backlog_claim_lookup import (  # noqa: E402
    BacklogClaimLookup,
    LocalClaimLookup,
)
from dev_status import (  # noqa: E402
    HARNESS_REPO,
    REPO_PREFIXES,
    is_worker_safe,
    prefix_of,
)
from dev_status_impl import serial_safety  # noqa: E402
from dev_status_mutation import validate_slug  # noqa: E402
from dev_status_storage import BacklogItem  # noqa: E402
from worktree import (  # noqa: E402
    WorktreeError,
    resolve_backlog_item,
    resolve_worktree_config,
)

# What this module does with toolkit data (checked by scripts/check_toolkit_paths.py).
TOOLKIT_DATA = "reader"
TOOLKIT_DATA_VIA = ("dev_status_storage",)

UNATTENDED_ENV = "PI_AGENT_UNATTENDED=1"
"""Set on the tab so it is in pi's environment before pi starts."""


def foreman_capture_file(slug: str) -> Path:
    """The worker's digest-capture file for a handoff launch."""
    return (
        agent_toolkit_paths.path_for("work-items").parent
        / "foreman"
        / f"{slug}-digest.json"
    )


def foreman_capture_env(slug: str) -> str:
    """The env pair that points a handoff worker at its capture file."""
    return f"PI_SWARM_CAPTURE_FILE={foreman_capture_file(slug)}"


# `prefix_of` and `is_worker_safe` are imported, never redefined. The whole
# point of the prefix scheme is one source of truth for "may a worker take
# this?"; a second copy here would be the drift this design exists to avoid.

DEV_STATUS = Path(__file__).parent / "dev_status.py"
COPILOT_PLUGIN_DIR = str(
    Path(__file__).resolve().parent.parent / "copilot" / "extensions" / "swarm"
)
AGY_PROMPT_TIMEOUT_S = 8.0
AGY_PROMPT_POLL_INTERVAL_S = 0.25
PROBE_PROMPT = "Reply with exactly READY. Do not run any command or edit any file."


def is_inside_git_repo(path: Path) -> bool:
    """Check whether a path is inside any git repository."""
    cur = path.resolve()
    for candidate in (cur, *cur.parents):
        if (candidate / ".git").exists():
            return True
        if (
            (candidate / "HEAD").is_file()
            and (candidate / "refs").is_dir()
            and (candidate / "objects").is_dir()
        ):
            return True
    return False


class RefusedError(RuntimeError):
    """A launch that must not proceed, with a reason fit to show the user."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        """herdr's own ``error.code`` for this failure, when parseable (see
        ``_parse_herdr_error_code``). ``None`` for a non-JSON or
        differently-shaped stderr -- deliberately not passed to
        ``super().__init__`` so ``.args``/``str()`` stay exactly the message,
        unaffected by this attribute."""


def _parse_herdr_error_code(stderr: str) -> str | None:
    """``error.code`` from a herdr JSON error envelope, or ``None``.

    ``None`` for anything that isn't exactly ``{"error": {"code": "<str>", ...}}``
    -- non-JSON stderr, JSON missing the ``error`` key, a non-dict ``error``,
    or a non-string ``code``. All of these are treated identically by the
    caller (an unrecognized failure, never retried).
    """
    try:
        parsed = json.loads(stderr)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    error = parsed.get("error")
    if not isinstance(error, dict):
        return None
    code = error.get("code")
    return code if isinstance(code, str) else None


def require_herdr_env(env: dict[str, str] | os._Environ[str]) -> None:
    """Refuse unless this process is inside a herdr-managed pane.

    Exact-match on ``"1"``, matching herdr's own documented check and
    ``permission-gate.ts``'s fail-closed read. Outside herdr there is no session
    to launch into, and targeting the UI-focused pane could hit another client.
    """
    if env.get("HERDR_ENV") != "1":
        raise RefusedError(
            "not running inside herdr (HERDR_ENV is not exactly '1'), so there "
            "is no session to launch into"
        )


def check_launchable(*, slug: str | None = None, prefix: str | None = None) -> None:
    """Refuse a launch that targets the harness's own repo.

    Accepts either form the caller might use -- a single item or a whole
    prefix -- so neither route can slip past.
    """
    target = prefix if prefix is not None else prefix_of(slug or "")
    if slug is not None:
        item = resolve_backlog_item(slug)
        if item is not None and item.get("no_worker"):
            reason = item.get("no_worker_reason")
            detail = (
                str(reason)
                if isinstance(reason, str) and reason.strip()
                else "item opted out of worker delegation"
            )
            raise RefusedError(
                f"'{slug}' opts out of worker delegation (no_worker): {detail}. "
                "Work it in a normal session instead."
            )
    if not is_worker_safe(target):
        known = ", ".join(f"{p}-" for p in sorted(REPO_PREFIXES.values()))
        raise RefusedError(
            f"'{target}-' is not a worker-safe prefix. Either it names the "
            f"harness repo ({HARNESS_REPO}), so a worker would be editing the "
            "code it is running, or it is unrecognised -- unknown prefixes are "
            f"refused rather than assumed safe. Known prefixes: {known}. Work "
            "these in a normal session instead."
        )


def canonical_prefix(prefix: str) -> str:
    """Slug-head form used in prompts, labels, state and comparisons."""
    return prefix.removesuffix("-")


def check_serial_prefix(prefix: str) -> str:
    """Return a known canonical prefix; item-level serial safety is separate."""
    canonical = canonical_prefix(prefix)
    if canonical not in REPO_PREFIXES.values():
        known = ", ".join(f"{p}-" for p in sorted(REPO_PREFIXES.values()))
        raise RefusedError(
            f"'{canonical}-' is not a known serial queue prefix. Known prefixes: {known}."
        )
    return canonical


def group_by_prefix(slugs: list[str]) -> list[dict[str, object]]:
    """Group slugs by prefix, worker-safe prefixes first, then largest first.

    Order is behaviour, not cosmetics: the skill recommends the first row.
    """
    counts: dict[str, int] = {}
    for slug in slugs:
        counts[prefix_of(slug)] = counts.get(prefix_of(slug), 0) + 1
    rows = [
        {"prefix": p, "count": n, "worker_safe": is_worker_safe(p)}
        for p, n in counts.items()
    ]
    rows.sort(key=lambda r: (not r["worker_safe"], -int(r["count"]), r["prefix"]))
    return rows


def build_tab_argv(
    *, cwd: str, label: str, kind: str = "pi", handoff_slug: str | None = None
) -> list[str]:
    """`herdr tab create` argv. For pi, the env pair is what makes the worker unattended;
    a handoff launch also points the worker at its capture file via the same env-var
    mechanism the swarm uses."""
    argv = ["tab", "create", "--cwd", cwd, "--label", label]
    if kind == "pi":
        argv += ["--env", UNATTENDED_ENV]
        if handoff_slug:
            argv += ["--env", foreman_capture_env(handoff_slug)]
    argv += ["--no-focus"]
    return argv


def agent_name_for(label: str) -> str:
    """The herdr agent name derived from a tab label.

    ONE helper on purpose: launch's start, restart's deregistration poll and
    restart's relaunch all claim the same name, and deriving it three ways
    would let a future sanitization change move one of them and not the
    others -- the poll would wait on a name nothing claims, or relaunch into
    a name it just certified free.
    """
    return label.replace("_", "-")[:32]


def build_tab_list_argv() -> list[str]:
    """`herdr tab list` argv."""
    return ["tab", "list"]


def build_agent_list_argv() -> list[str]:
    """`herdr agent list` argv."""
    return ["agent", "list"]


def build_agent_start_argv(
    *,
    name: str,
    pane: str,
    model: str | None,
    kind: str = "pi",
    session_id: str | None = None,
    allow_all_tools: bool = True,
    plugin_dir: str | None = None,
    writable_roots: list[str] | None = None,
) -> list[str]:
    """`herdr agent start` argv, with flags passed through after a bare ``--``."""
    argv = ["agent", "start", name, "--kind", kind, "--pane", pane]
    if kind == "copilot":
        session_args: list[str] = []
        if session_id:
            session_args += ["--session-id", session_id]
        if allow_all_tools:
            session_args += ["--allow-all-tools"]
        if plugin_dir:
            session_args += ["--plugin-dir", plugin_dir]
        if model:
            session_args += ["--model", model]
        if session_args:
            argv += ["--", *session_args]
        return argv

    if kind == "agy":
        session_args = ["--mode", "accept-edits"]
        if model:
            session_args += ["--model", model]
        return [*argv, "--", *session_args]

    if kind == "codex":
        session_args = [
            "-c",
            "check_for_update_on_startup=false",
            "--approve-for-me",
        ]
        for root in writable_roots or []:
            session_args += ["--add-dir", root]
        if model:
            session_args += ["--model", model]
        return [*argv, "--", *session_args]

    if model:
        argv += ["--", "--model", model]
    return argv


def worker_prompt(slug: str, kind: str = "pi", handoff: bool = False) -> str:
    """One worker, one item, unattended. ``handoff`` makes the worker stop after
    its commit gate and print the FOREMAN-HANDOFF line, leaving the landing to
    the foreman."""
    invoke = "$backlog-item" if kind == "codex" else "/backlog-item"
    suffix = " --handoff" if handoff else ""
    return f"{invoke} --auto {slug}{suffix}"


def _scope_prompt(
    base: str, *, items: list[str] | None, exclude: list[str] | None
) -> str:
    """Append the optional --items/--exclude scope to an orchestrator prompt."""
    if items:
        return f"{base} --items {' '.join(items)}"
    if exclude:
        return f"{base} --exclude {' '.join(exclude)}"
    return base


def orchestrator_prompt(
    concurrency: int,
    prefix: str,
    kind: str = "pi",
    items: list[str] | None = None,
    exclude: list[str] | None = None,
) -> str:
    """One orchestrator; `swarm_spawn` owns the fan-out from here."""
    return _scope_prompt(
        f"/backlog-item --swarm={concurrency} --prefix {prefix}",
        items=items,
        exclude=exclude,
    )


def orchestrator_resume_prompt(
    concurrency: int, run_id: str, prefix: str, kind: str = "pi"
) -> str:
    """One orchestrator, resuming an interrupted run.

    Single line, deliberately: prompt templates take arguments from the
    command line itself, and extra prose after a slash command is not a
    documented mechanism. Everything `resume` means lives in
    the harness's --swarm section; this only has to match what
    that parser accepts.
    """
    return f"/backlog-item --swarm={concurrency} resume {run_id} --prefix {prefix}"


def serial_orchestrator_prompt(
    prefix: str,
    items: list[str] | None = None,
    exclude: list[str] | None = None,
) -> str:
    """One orchestrator running the shared scheduler with a single worker."""
    return _scope_prompt(
        f"/backlog-item --serial --prefix {canonical_prefix(prefix)}",
        items=items,
        exclude=exclude,
    )


def serial_orchestrator_resume_prompt(run_id: str, prefix: str) -> str:
    """Resume one serial orchestrator without changing its run identity."""
    return f"/backlog-item --serial resume {run_id} --prefix {canonical_prefix(prefix)}"


RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9._-]+")
RUN_ID_MAX_LEN = 64


def validate_run_id(run_id: str) -> str:
    """Refuse a runId the delegate cannot safely pass through.

    swarm-tool.ts's ``statePath`` joins the runId into a state filename
    unsanitized, so a crafted value is stopped here rather than trusted
    downstream.
    """
    if (
        not run_id
        or len(run_id) > RUN_ID_MAX_LEN
        or not RUN_ID_PATTERN.fullmatch(run_id)
    ):
        raise RefusedError(
            f"--run-id must be non-empty, at most {RUN_ID_MAX_LEN} chars, and "
            f"only [A-Za-z0-9._-] (got {run_id!r})"
        )
    return run_id


def swarm_state_dir(kind: str = "pi") -> Path:
    """Where swarm state is persisted for kind (same override, same default)."""
    if kind == "copilot":
        override = os.environ.get("COPILOT_SWARM_STATE_DIR")
        if override:
            return Path(override)
        return Path.home() / ".copilot" / "state"
    override = os.environ.get("PI_SWARM_STATE_DIR")
    if override:
        return Path(override)
    return Path.home() / ".pi" / "agent" / "state"


def state_matches_prefix(state: object, prefix: str, mode: str = "concurrent") -> bool:
    """Whether one parsed state file belongs to a run scoped to ``prefix``.

    Exact field first -- swarm-tool stamps ``prefix`` on fresh state --
    with a slug-scan fallback for files written before the field existed. A
    file with a DIFFERENT prefix field never matches by slug luck: the field
    is the deliberate answer, the scan is only for legacy files that lack it.
    """
    if not isinstance(state, dict):
        return False
    recorded_mode = state.get("mode", "concurrent")
    if recorded_mode != mode:
        return False
    recorded = state.get("prefix")
    if recorded is not None:
        return recorded == prefix
    slugs: list[object] = []
    workers = state.get("workers")
    if isinstance(workers, list):
        slugs += [w.get("slug") for w in workers if isinstance(w, dict)]
    attempted = state.get("attempted")
    if isinstance(attempted, list):
        slugs += attempted
    return any(isinstance(s, str) and s.startswith(f"{prefix}-") for s in slugs)


def discover_run_id(prefix: str, kind: str = "pi", mode: str = "concurrent") -> str:
    """The runId of the newest state file belonging to ``prefix``.

    Refuses rather than falling back to a fresh run: a restart that cannot
    name the run it is resuming must not silently launch a new one -- the
    previous run's workers are deliberately still alive, and a fresh runId
    would neither adopt nor reconcile them. The refusal names the two
    deliberate exits instead (explicit --run-id, or launch for a conscious
    fresh start).
    """
    state_dir = swarm_state_dir(kind=kind)
    if not state_dir.is_dir():
        raise RefusedError(
            f"no swarm state dir at {state_dir}, so there is no run to resume. "
            "Pass --run-id explicitly, or use launch for a deliberate fresh start."
        )
    files = sorted(
        state_dir.glob("swarm-*.json"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    skipped: list[str] = []
    matches: list[tuple[str, bool]] = []
    for path in files:
        try:
            state: object = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError, ValueError):
            skipped.append(path.name)
            continue
        if state_matches_prefix(state, prefix, mode):
            run_id = ""
            if isinstance(state, dict):
                candidate = state.get("runId")
                if isinstance(candidate, str):
                    run_id = candidate
            if not run_id:
                run_id = path.stem.removeprefix("swarm-")
            workers = state.get("workers") if isinstance(state, dict) else None
            matches.append((run_id, isinstance(workers, list) and bool(workers)))
    active = [run_id for run_id, has_workers in matches if has_workers]
    if len(active) > 1:
        raise RefusedError(
            f"multiple {mode} runs for prefix '{prefix}' still contain worker "
            f"records ({', '.join(active)}); pass --run-id explicitly"
        )
    if active:
        return active[0]
    if matches:
        return matches[0][0]
    detail = f" ({len(skipped)} unparseable, e.g. {skipped[0]})" if skipped else ""
    raise RefusedError(
        f"no swarm state file matches prefix '{prefix}'{detail}, so there is no "
        "run to resume. Pass --run-id explicitly, or use launch for a deliberate "
        "fresh start."
    )


def resolve_resume_run_id(
    prefix: str,
    run_id: str | None,
    kind: str = "pi",
    mode: str = "concurrent",
) -> str:
    """The runId a restart will resume. Explicit always wins; discovery next."""
    if run_id is not None:
        return validate_run_id(run_id)
    return discover_run_id(prefix, kind=kind, mode=mode)


def parse_tab_list(listing: dict[str, object]) -> list[dict[str, object]]:
    """Tabs out of a `herdr tab list` envelope; [] on anything unexpected."""
    result = listing.get("result") if isinstance(listing, dict) else None
    tabs = result.get("tabs") if isinstance(result, dict) else None
    if not isinstance(tabs, list):
        return []
    return [t for t in tabs if isinstance(t, dict)]


def live_tab_ids_with_label(label: str) -> list[str]:
    """Ids of every live tab carrying exactly ``label``.

    Exact equality, never a prefix match: launch created the label verbatim,
    so the contract is exact, and a prefix match could swallow a human's
    differently-suffixed tab.
    """
    listing = herdr(build_tab_list_argv())
    return [
        str(t["tab_id"])
        for t in parse_tab_list(listing)
        if t.get("label") == label and isinstance(t.get("tab_id"), str)
    ]


def live_queue_orchestrators(prefix: str) -> list[tuple[str, str]]:
    """Live serial or concurrent orchestrator tabs for one canonical prefix."""
    labels = {f"serial-{prefix}", f"swarm-{prefix}"}
    listing = herdr(build_tab_list_argv())
    return [
        (str(tab["label"]), str(tab["tab_id"]))
        for tab in parse_tab_list(listing)
        if tab.get("label") in labels and isinstance(tab.get("tab_id"), str)
    ]


def parse_agent_names(listing: dict[str, object]) -> list[str]:
    """Agent names out of a `herdr agent list` envelope; [] on anything unexpected."""
    result = listing.get("result") if isinstance(listing, dict) else None
    agents = result.get("agents") if isinstance(result, dict) else None
    if not isinstance(agents, list):
        return []
    return [
        a["name"]
        for a in agents
        if isinstance(a, dict) and isinstance(a.get("name"), str)
    ]


def parse_agent_status(listing: dict[str, object], name: str) -> str | None:
    """Agent status for ``name`` out of a `herdr agent list` envelope; None if absent."""
    result = listing.get("result") if isinstance(listing, dict) else None
    agents = result.get("agents") if isinstance(result, dict) else None
    if not isinstance(agents, list):
        return None
    for a in agents:
        if isinstance(a, dict) and a.get("name") == name:
            status = a.get("agent_status")
            return status if isinstance(status, str) else None
    return None


# ── foreman `watch` ─────────────────────────────────────────────────────────

WATCH_ATTENTION_STATUSES = ("blocked", "done", "idle")
WATCH_DEFAULT_GRACE_S = 60
WATCH_DEFAULT_POLL_S = 15
WATCH_TIMEOUT_EXIT = 124


def parse_agent_listing_strict(
    listing: dict[str, object],
) -> list[dict[str, object]]:
    """Agent records out of `herdr agent list`, strict; refuse a malformed envelope.

    Unlike :func:`parse_agent_names` -- which returns ``[]`` on a malformed
    envelope, silently reading garbage as "no agents" -- this raises
    :class:`RefusedError` (exit 2) on anything that is not exactly
    ``{"result": {"agents": [...]}}`` with well-formed records. Unnamed records
    (foreign interactive agents) are skipped: they cannot be watched by name.
    A named record missing a string ``agent_status`` or an integer
    ``state_change_seq`` is malformed.
    """
    if not isinstance(listing, dict):
        raise RefusedError("malformed agent listing: not a JSON object")
    result = listing.get("result")
    if not isinstance(result, dict):
        raise RefusedError("malformed agent listing: missing 'result' object")
    agents = result.get("agents")
    if not isinstance(agents, list):
        raise RefusedError("malformed agent listing: 'result.agents' is not a list")
    records: list[dict[str, object]] = []
    for raw in agents:
        if not isinstance(raw, dict):
            raise RefusedError("malformed agent listing: agent record is not an object")
        name = raw.get("name")
        if name is None:
            continue  # unnamed/foreign agent: not watchable by name
        status = raw.get("agent_status")
        seq = raw.get("state_change_seq")
        if (
            not isinstance(name, str)
            or not isinstance(status, str)
            or not isinstance(seq, int)
            or isinstance(seq, bool)
        ):
            raise RefusedError(
                f"malformed agent listing: record {name!r} lacks a string name, "
                "string agent_status, or integer state_change_seq"
            )
        records.append(
            {
                "name": name,
                "status": status,
                "seq": seq,
                "session_path": _pi_session_path(raw.get("agent_session")),
            }
        )
    return records


def _pi_session_path(session: object) -> str | None:
    """A pi agent's session-file path from its ``agent_session``; else None.

    Anything other than ``{"agent": "pi", "kind": "path", "value": <str>}`` is
    not a pi session file -- never a malformed listing.
    """
    if (
        isinstance(session, dict)
        and session.get("agent") == "pi"
        and session.get("kind") == "path"
        and isinstance(session.get("value"), str)
    ):
        return str(session["value"])
    return None


def _empty_watch_state() -> dict[str, object]:
    return {"agents": {}, "membership": [], "pending_gone": []}


def _validate_watch_state(state: object) -> dict[str, object]:
    """Type/key-check a parsed watch state; refuse anything that isn't the real shape."""
    if not isinstance(state, dict):
        raise RefusedError("corrupt watch state: not a JSON object")
    agents = state.get("agents")
    if not isinstance(agents, dict):
        raise RefusedError("corrupt watch state: 'agents' is not an object")
    for name, rec in agents.items():
        if not isinstance(name, str) or not isinstance(rec, dict):
            raise RefusedError("corrupt watch state: invalid agent entry")
        acked = rec.get("acked_seq")
        first = rec.get("first_seen_at")
        ever = rec.get("ever_working")
        if (
            not isinstance(acked, int)
            or isinstance(acked, bool)
            or not isinstance(first, (int, float))
            or isinstance(first, bool)
            or not isinstance(ever, bool)
        ):
            raise RefusedError(
                "corrupt watch state: agent entry missing acked_seq/"
                "first_seen_at/ever_working"
            )
    membership = state.get("membership")
    pending_gone = state.get("pending_gone")
    if not isinstance(membership, list) or not all(
        isinstance(m, str) for m in membership
    ):
        raise RefusedError("corrupt watch state: 'membership' is not a list of strings")
    if not isinstance(pending_gone, list) or not all(
        isinstance(m, str) for m in pending_gone
    ):
        raise RefusedError(
            "corrupt watch state: 'pending_gone' is not a list of strings"
        )
    validated: dict[str, object] = {
        "agents": agents,
        "membership": list(membership),
        "pending_gone": list(pending_gone),
    }
    if "cost_acks" in state:
        validated["cost_acks"] = _validate_cost_acks(state["cost_acks"])
    return validated


def _validate_cost_acks(acks: object) -> dict[str, dict[str, object]]:
    """Type-check the optional ``cost_acks`` map (absent in older state files)."""
    if not isinstance(acks, dict):
        raise RefusedError("corrupt watch state: 'cost_acks' is not an object")
    for subject, ack in acks.items():
        ok = (
            isinstance(subject, str)
            and isinstance(ack, dict)
            and ack.get("reason") in COST_REASONS
            and _is_number_or_none(ack.get("usd"))
            and isinstance(ack.get("session_hash"), str)
            and isinstance(ack.get("problems_hash"), str)
            and isinstance(ack.get("day"), str)
            and _is_number_or_none(ack.get("acked_at"))
            and ack.get("acked_at") is not None
        )
        if not ok:
            raise RefusedError(
                f"corrupt watch state: invalid cost_acks entry {subject!r}"
            )
    return acks


def _is_number_or_none(value: object) -> bool:
    return value is None or (
        isinstance(value, (int, float)) and not isinstance(value, bool)
    )


def _load_watch_state(path: Path) -> dict[str, object]:
    if not path.exists():
        return _empty_watch_state()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RefusedError(f"cannot read watch state {path}: {exc}") from exc
    try:
        state = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RefusedError(f"corrupt watch state {path}: not valid JSON") from exc
    return _validate_watch_state(state)


def _save_watch_state(path: Path, state: dict[str, object]) -> None:
    """Atomic write: temp file + ``os.replace``, so an interrupted write cannot
    leave a half-written state (the previous state survives)."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


@contextlib.contextmanager
def _watch_lock(state_path: Path) -> Iterator[None]:
    """Hold an exclusive ``fcntl.flock`` for the watcher's lifetime.

    The lock lives on a stable ``<state>.lock`` sibling (never ``os.replace``d,
    unlike the state file), so a second watcher on the same state path fails
    to acquire it and exits 2 immediately.
    """
    lock_path = state_path.with_name(state_path.name + ".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RefusedError(
                f"watch state {state_path} is locked by another watcher"
            ) from exc
        yield
    finally:
        os.close(fd)


# ── foreman `watch` cost caps ───────────────────────────────────────────────
#
# Spend is pi's own price-table estimate read from its session JSONL (never the
# pane status line). Every alert is a *condition* on a *subject* (`agent:<name>`
# or `total`); an ack stores the snapshot the report printed, so a late ack can
# never swallow spend that happened after the report. An ack snoozes at most one
# more `--cost-step` of measured spend, or `--cost-remind-s` when the spend is
# not observable (an incomplete measurement or an overflow).

COST_REASONS = ("over_cap", "unmeasured", "incomplete")
COST_DEFAULT_STEP = 1.0
COST_DEFAULT_REMIND_S = 900.0
COST_ESTIMATE_NOTE = "pi native cost from its price table, not provider billing"
COST_PROBLEMS_SHOWN = 20
COST_TOTAL_SUBJECT = "total"


def _watch_now() -> datetime:
    """The local-aware current time (patched in tests)."""
    return datetime.now().astimezone()


def _local_midnight(now: datetime) -> datetime:
    """Local midnight of ``now``'s date, using the zone's rules for that date.

    Truncating ``now`` would keep *today's* UTC offset, which is wrong on a DST
    transition day; a naive midnight resolved by ``astimezone`` is not.
    """
    return datetime.combine(now.astimezone().date(), dt_time()).astimezone()


def _positive_finite(raw: str) -> float:
    """argparse type: a finite number > 0 (NaN, infinity and <= 0 refused)."""
    try:
        value = float(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{raw!r} is not a number") from exc
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError(f"{raw!r} must be finite and > 0")
    return value


def _short_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _session_hash(session: str | None) -> str:
    return "-" if session is None else _short_hash(session)


def _problems_hash(problems: tuple[str, ...]) -> str:
    return "-" if not problems else _short_hash("\n".join(sorted(problems)))


def _cost_token(subject: str, snap: dict[str, object], day: str) -> str:
    usd = snap["usd"]
    usd_s = "overflow" if usd is None else repr(usd)
    return ":".join(
        [
            "cost",
            subject,
            str(snap["reason"]),
            usd_s,
            str(snap["session_hash"]),
            str(snap["problems_hash"]),
            day,
        ]
    )


def _parse_cost_token(token: str) -> tuple[str, dict[str, object]]:
    """Decode an ``--ack-cost`` token into (subject, stored snapshot); refuse junk."""
    head, sep, rest = token.partition(":")
    parts = rest.rsplit(":", 5) if sep else []
    if head != "cost" or len(parts) != 6:
        raise RefusedError(f"--ack-cost {token!r} is not a cost alert token")
    subject, reason, usd_s, session_hash, problems_hash, day = parts
    if not (subject == COST_TOTAL_SUBJECT or subject.startswith("agent:")):
        raise RefusedError(f"--ack-cost {token!r}: unknown subject {subject!r}")
    if reason not in COST_REASONS:
        raise RefusedError(f"--ack-cost {token!r}: unknown reason {reason!r}")
    usd: float | None = None
    if usd_s != "overflow":
        try:
            usd = float(usd_s)
        except ValueError as exc:
            raise RefusedError(f"--ack-cost {token!r}: bad usd {usd_s!r}") from exc
        if not math.isfinite(usd):
            raise RefusedError(f"--ack-cost {token!r}: bad usd {usd_s!r}")
    return subject, {
        "reason": reason,
        "usd": usd,
        "session_hash": session_hash,
        "problems_hash": problems_hash,
        "day": day,
    }


def _cost_condition(
    measured: analyze_sessions.PiCost, cap: float, incomplete_reason: str
) -> str | None:
    """``over_cap`` first (a known overspend is never hidden), then incompleteness."""
    if measured.usd is None or measured.usd >= cap:
        return "over_cap"
    if not measured.complete:
        return incomplete_reason
    return None


def _cost_ack_silences(
    ack: dict[str, object] | None,
    snap: dict[str, object],
    *,
    subject: str,
    day: str,
    step: float,
    remind_s: float,
    now_ts: float,
) -> bool:
    """True while a stored ack still covers this snapshot (see the section rule)."""
    if not ack:
        return False
    if (
        ack["reason"] != snap["reason"]
        or ack["session_hash"] != snap["session_hash"]
        or ack["problems_hash"] != snap["problems_hash"]
        or (subject == COST_TOTAL_SUBJECT and ack["day"] != day)
    ):
        return False
    usd, acked_usd = snap["usd"], ack["usd"]
    if (
        isinstance(usd, float)
        and isinstance(acked_usd, (int, float))
        and usd >= float(acked_usd) + step
    ):
        return False
    # Spend is observable only on a complete measurement (no problems) without
    # overflow; anything else is snoozed by time, never indefinitely.
    observable = (
        snap["reason"] == "over_cap"
        and usd is not None
        and acked_usd is not None
        and snap["problems_hash"] == "-"
    )
    return observable or now_ts - float(ack["acked_at"]) < remind_s


def _measure_costs(
    args: argparse.Namespace,
    agents: dict[str, str | None],
    acks: dict[str, dict[str, object]],
) -> dict[str, object]:
    """Measure the capped subjects and evaluate their alerts against ``acks``."""
    now = _watch_now()
    day = now.date().isoformat()
    now_ts = now.timestamp()
    snaps: dict[str, tuple[analyze_sessions.PiCost, str | None, str | None]] = {}
    agents_out: dict[str, object] = {}
    total_out: dict[str, object] | None = None

    if args.cost_cap_agent is not None:
        for name, session in sorted(agents.items()):
            if session is None:
                measured = analyze_sessions.PiCost(0.0, False, ("no pi session path",))
            else:
                measured = analyze_sessions.pi_session_cost(Path(session))
            reason = _cost_condition(measured, args.cost_cap_agent, "unmeasured")
            snaps[f"agent:{name}"] = (measured, session, reason)
            agents_out[name] = {"session": session, **_cost_json(measured)}
    if args.cost_cap_total is not None:
        measured = analyze_sessions.pi_cost_since(_local_midnight(now))
        reason = _cost_condition(measured, args.cost_cap_total, "incomplete")
        snaps[COST_TOTAL_SUBJECT] = (measured, None, reason)
        total_out = _cost_json(measured)

    alerts: dict[str, object] = {}
    for subject, (measured, session, reason) in snaps.items():
        if reason is None:
            continue
        snap: dict[str, object] = {
            "reason": reason,
            "usd": measured.usd,
            "session_hash": _session_hash(session),
            "problems_hash": _problems_hash(measured.problems),
        }
        if _cost_ack_silences(
            acks.get(subject),
            snap,
            subject=subject,
            day=day,
            step=args.cost_step,
            remind_s=args.cost_remind_s,
            now_ts=now_ts,
        ):
            continue
        alerts[subject] = {
            "reason": reason,
            "usd": measured.usd,
            "ack": _cost_token(subject, snap, day),
        }
    return {
        "day": day,
        "estimate": COST_ESTIMATE_NOTE,
        "cap_agent": args.cost_cap_agent,
        "cap_total": args.cost_cap_total,
        "step": args.cost_step,
        "total": total_out,
        "agents": agents_out,
        "alerts": alerts,
    }


def _cost_json(measured: analyze_sessions.PiCost) -> dict[str, object]:
    problems = list(measured.problems[:COST_PROBLEMS_SHOWN])
    hidden = len(measured.problems) - len(problems)
    if hidden > 0:
        problems.append(f"... {hidden} more")
    return {"usd": measured.usd, "complete": measured.complete, "problems": problems}


def _apply_cost_acks(args: argparse.Namespace, state: dict[str, object]) -> None:
    """Store each ``--ack-cost`` token's reported snapshot.

    A token whose cap is not set is refused, so an ack is never recorded
    against a cap nobody is watching.
    """
    if not args.ack_cost:
        return
    acks = state.setdefault("cost_acks", {})
    now_ts = _watch_now().timestamp()
    for token in args.ack_cost:
        subject, snap = _parse_cost_token(token)
        if subject == COST_TOTAL_SUBJECT and args.cost_cap_total is None:
            raise RefusedError(f"--ack-cost {token!r} needs --cost-cap-total")
        if subject != COST_TOTAL_SUBJECT and args.cost_cap_agent is None:
            raise RefusedError(f"--ack-cost {token!r} needs --cost-cap-agent")
        acks[subject] = {**snap, "acked_at": now_ts}


def _cmd_watch(args: argparse.Namespace) -> None:
    require_herdr_env(os.environ)
    state_path = Path(args.state)
    prefix = canonical_prefix(args.prefix) if args.prefix else None
    names = list(args.agents)
    grace = args.grace_s
    timeout = args.timeout_s
    poll = args.poll_s

    with _watch_lock(state_path):
        state = _load_watch_state(state_path)
        now = time.time()

        # Acknowledgements are applied before any poll, so a crash after a
        # report but before the foreman acts re-reports the same event.
        for ack in args.ack or []:
            name, sep, seq_s = ack.rpartition("=")
            if not sep or not name or not seq_s.isdigit():
                raise RefusedError(f"--ack {ack!r} is not NAME=SEQ")
            rec = state["agents"].setdefault(
                name, {"acked_seq": 0, "first_seen_at": now, "ever_working": False}
            )
            rec["acked_seq"] = int(seq_s)
        for name in args.ack_gone or []:
            state["pending_gone"] = [n for n in state["pending_gone"] if n != name]
            state["membership"] = [n for n in state["membership"] if n != name]
            state["agents"].pop(name, None)
        _apply_cost_acks(args, state)
        capped = args.cost_cap_agent is not None or args.cost_cap_total is not None

        strict_checked = False
        deadline = time.monotonic() + timeout
        while True:
            listing = herdr(build_agent_list_argv())
            records = parse_agent_listing_strict(listing)
            by_name = {str(r["name"]): r for r in records}
            present = set(by_name)
            now = time.time()

            if args.strict and not strict_checked:
                strict_checked = True
                missing = sorted(n for n in names if n not in present)
                if missing:
                    _save_watch_state(state_path, state)
                    raise RefusedError(
                        f"--strict: {' '.join(missing)} absent from the first listing"
                    )

            targets = set(names)
            if prefix:
                targets |= {n for n in present if n.startswith(f"{prefix}-")}

            for name in sorted(present & targets):
                if name not in state["membership"]:
                    state["membership"].append(name)
                rec = state["agents"].setdefault(
                    name, {"acked_seq": 0, "first_seen_at": now, "ever_working": False}
                )
                if by_name[name]["status"] == "working":
                    rec["ever_working"] = True

            attention: dict[str, dict[str, object]] = {}
            for name in sorted(present & targets):
                rec = by_name[name]
                agent = state["agents"][name]
                if rec["status"] not in WATCH_ATTENTION_STATUSES:
                    continue
                if rec["seq"] == agent["acked_seq"]:
                    continue
                if rec["status"] == "idle" and (
                    now - agent["first_seen_at"] < grace and not agent["ever_working"]
                ):
                    continue
                attention[name] = {"status": rec["status"], "seq": rec["seq"]}

            for name in state["membership"]:
                if name in present:
                    continue
                is_target = name in names or (
                    prefix is not None and name.startswith(f"{prefix}-")
                )
                if is_target and name not in state["pending_gone"]:
                    state["pending_gone"].append(name)

            gone = sorted(state["pending_gone"])
            all_records = {
                name: {"status": by_name[name]["status"], "seq": by_name[name]["seq"]}
                for name in sorted(present & targets)
            }
            report: dict[str, object] = {
                "attention": attention,
                "gone": gone,
                "all": all_records,
            }
            if capped:
                report["cost"] = _measure_costs(
                    args,
                    {n: by_name[n]["session_path"] for n in sorted(present & targets)},
                    state.get("cost_acks", {}),
                )

            if attention or gone or (capped and report["cost"]["alerts"]):
                _save_watch_state(state_path, state)
                print(json.dumps(report, indent=2))
                return

            _save_watch_state(state_path, state)
            if time.monotonic() >= deadline:
                print(json.dumps({**report, "timeout": True}, indent=2))
                raise SystemExit(WATCH_TIMEOUT_EXIT)
            time.sleep(poll)


def cmd_watch(args: argparse.Namespace) -> None:
    """Report the first watched agent that needs attention; exit 124 on timeout."""
    try:
        _cmd_watch(args)
    except RefusedError as exc:
        print(f"[herdr_delegate] {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


# ── foreman `answer` ────────────────────────────────────────────────────────

ANSWER_TEXT_MAX = 120
ANSWER_TEXT_PATTERN = re.compile(r"[!-~]+(?: [!-~]+)*")


def build_agent_get_argv(name: str) -> list[str]:
    """`herdr agent get` argv; the single-agent payload nests under result.agent."""
    return ["agent", "get", name]


def build_pane_send_keys_argv(pane: str, keys: list[str]) -> list[str]:
    """`herdr pane send-keys` argv, one key per trailing element."""
    return ["pane", "send-keys", pane, *keys]


def build_pane_send_text_argv(pane: str, text: str) -> list[str]:
    """`herdr pane send-text` argv."""
    return ["pane", "send-text", pane, text]


def parse_agent_get(listing: dict[str, object]) -> dict[str, str] | None:
    """Status, pane and tab out of a `herdr agent get` envelope; None if malformed.

    ``tab`` is additive: a missing or non-string ``tab_id`` reads as ``""`` and
    never makes the parse fail, so `answer`'s status/pane checks are unchanged.
    """
    result = listing.get("result")
    agent = result.get("agent") if isinstance(result, dict) else None
    if not isinstance(agent, dict):
        return None
    status = agent.get("agent_status")
    if not isinstance(status, str):
        return None
    pane = agent.get("pane_id")
    tab = agent.get("tab_id")
    return {
        "status": status,
        "pane": pane if isinstance(pane, str) else "",
        "tab": tab if isinstance(tab, str) else "",
    }


def validate_answer_text(text: str) -> None:
    """Refuse --text outside the accepted contract (printable ASCII, single
    spaces, no leading/trailing space, at most 120 chars, not blank)."""
    if len(text) > ANSWER_TEXT_MAX:
        raise RefusedError(f"--text must be at most {ANSWER_TEXT_MAX} characters")
    if not ANSWER_TEXT_PATTERN.fullmatch(text):
        raise RefusedError(
            "--text must be printable ASCII with single spaces only, no leading "
            "or trailing space, and not blank"
        )


class AnswerOutcome(Exception):
    """A needs_manual / submitted_unconfirmed outcome, carrying its exit code."""

    def __init__(self, code: int, result: str, input_sent: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.result = result
        self.input_sent = input_sent
        self.reason = reason


def _answer_outcome(code: int, result: str, input_sent: str, reason: str) -> None:
    print(json.dumps({"result": result, "input_sent": input_sent, "reason": reason}))
    raise SystemExit(code)


def _verify_blocked(agent: str, pane: str, identity: list[str], input_sent: str) -> str:
    """Re-check status/pane/picker identity before a mutation; return visible text.

    ``input_sent`` is what this invocation has already delivered, so a failure
    after navigation or text is reported as exit 4 (not safe to retry blindly),
    not exit 3 (safe to retry).
    """
    listing = herdr(build_agent_get_argv(agent))
    info = parse_agent_get(listing)
    code = 4 if input_sent != "none" else 3
    if info is None:
        raise AnswerOutcome(
            code,
            "needs_manual",
            input_sent,
            f"agent get for {agent!r} returned no recognizable status",
        )
    if info["status"] != "blocked":
        raise AnswerOutcome(
            code,
            "needs_manual",
            input_sent,
            f"agent {agent!r} is {info['status']!r}, not blocked",
        )
    if info["pane"] != pane:
        raise AnswerOutcome(
            code,
            "needs_manual",
            input_sent,
            f"pane is {info['pane']!r}, expected {pane!r}",
        )
    content = read_pane_text(pane, source="visible")
    parsed = herdr_picker.parse_picker(content)
    if not parsed.options:
        raise AnswerOutcome(
            code,
            "needs_manual",
            input_sent,
            "no question-tool picker in the visible pane",
        )
    if [o.label for o in parsed.options] != identity:
        raise AnswerOutcome(
            code,
            "needs_manual",
            input_sent,
            "picker identity changed since the last read",
        )
    return content


def _poll_until(
    predicate: Callable[[], bool], timeout_s: float, poll_interval: float = 0.2
) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(poll_interval)
    return predicate()


def _send(argv: list[str], input_sent: str) -> None:
    """Run a mutation; a herdr failure after input was sent is exit 4, never 2."""
    try:
        herdr(argv)
    except RefusedError as exc:
        if input_sent != "none":
            raise AnswerOutcome(4, "needs_manual", input_sent, str(exc)) from exc
        raise


def _visible_editor_text(pane: str) -> str:
    parsed = herdr_picker.parse_edit_mode(read_pane_text(pane, source="visible"))
    return "".join(parsed.editor_text_lines) if parsed else ""


def _run_answer(args: argparse.Namespace) -> None:
    require_herdr_env(os.environ)
    agent = args.agent
    pane = args.expect_pane
    settle_s = args.settle_s

    if args.other:
        validate_answer_text(args.text)
        text = args.text

    input_sent = "none"

    listing = herdr(build_agent_get_argv(agent))
    info = parse_agent_get(listing)
    if info is None:
        raise RefusedError(f"agent get for {agent!r} returned no recognizable status")
    if info["status"] != "blocked":
        _answer_outcome(
            3,
            "needs_manual",
            "none",
            f"agent {agent!r} is {info['status']!r}, not blocked",
        )
    if info["pane"] != pane:
        _answer_outcome(
            3, "needs_manual", "none", f"pane is {info['pane']!r}, expected {pane!r}"
        )

    content = read_pane_text(pane, source="visible")
    picker = herdr_picker.parse_picker(content)
    edit = herdr_picker.parse_edit_mode(content)

    # A previous answer attempt may have left the worker already in edit mode
    # with text in the editor. Drive it directly rather than re-navigating.
    if edit is not None:
        if not args.other:
            _answer_outcome(
                3,
                "needs_manual",
                "none",
                "worker is already in free-text edit mode; use --other --text, or "
                "drive selection mode from a fresh picker",
            )
        identity = [o.label for o in edit.options]
        if not edit.complete:
            _answer_outcome(
                4, "needs_manual", "none", "editor is scrolled or not fully visible"
            )
        _verify_blocked(agent, pane, identity, input_sent)
        _send(build_pane_send_text_argv(pane, text), input_sent)
        input_sent = "text"
        text_settled = _poll_until(
            lambda: (
                re.sub(r"\s+", "", _visible_editor_text(pane))
                == re.sub(r"\s+", "", text)
            ),
            settle_s,
        )
        if not text_settled:
            _answer_outcome(
                4, "needs_manual", "text", "editor text never matched --text"
            )
        _verify_blocked(agent, pane, identity, input_sent)
        _send(build_pane_send_keys_argv(pane, ["enter"]), input_sent)
        input_sent = "submitted"
        _finish_answer(pane, input_sent, settle_s)
        return

    if not picker.options:
        _answer_outcome(
            3, "needs_manual", "none", "no question-tool picker in the visible pane"
        )
    identity = [o.label for o in picker.options]
    selected = picker.selected_index
    if selected is None:
        _answer_outcome(3, "needs_manual", "none", "picker has no highlighted option")

    if args.other:
        target = next(
            (o for o in picker.options if o.label == herdr_picker.OTHER_OPTION_LABEL),
            None,
        )
        if target is None:
            _answer_outcome(
                3, "needs_manual", "none", "free-text escape option not present"
            )
    elif args.option is not None:
        target = herdr_picker.match_option(args.option, picker.options)
        if target is None:
            _answer_outcome(
                3,
                "needs_manual",
                "none",
                f"--option {args.option!r} matched no listed option (or was ambiguous)",
            )
        if target.label == herdr_picker.OTHER_OPTION_LABEL:
            _answer_outcome(
                3, "needs_manual", "none", "select the free-text escape via --other"
            )
    else:
        target = next((o for o in picker.options if o.index == args.index), None)
        if target is None:
            _answer_outcome(
                3, "needs_manual", "none", f"--index {args.index} is out of range"
            )
        if target.label == herdr_picker.OTHER_OPTION_LABEL:
            _answer_outcome(
                3, "needs_manual", "none", "select the free-text escape via --other"
            )

    # Navigate to the target (arrows only, no Enter).
    arrows = herdr_picker.navigation_keys(selected, target.index)
    if arrows:
        _verify_blocked(agent, pane, identity, input_sent)
        _send(build_pane_send_keys_argv(pane, arrows), input_sent)
        input_sent = "navigation"
        settled = _poll_until(
            lambda: (
                herdr_picker.parse_picker(
                    read_pane_text(pane, source="visible")
                ).selected_index
                == target.index
            ),
            settle_s,
        )
        if not settled:
            _answer_outcome(
                4, "needs_manual", "navigation", "highlight never settled on the target"
            )

    if args.other:
        # Enter edit mode, then verify completeness before sending text.
        _verify_blocked(agent, pane, identity, input_sent)
        _send(build_pane_send_keys_argv(pane, ["enter"]), input_sent)
        input_sent = "navigation"
        in_edit = _poll_until(
            lambda: (
                herdr_picker.parse_edit_mode(read_pane_text(pane, source="visible"))
                is not None
            ),
            settle_s,
        )
        if not in_edit:
            _answer_outcome(
                4, "needs_manual", "navigation", "picker never entered edit mode"
            )
        edit = herdr_picker.parse_edit_mode(read_pane_text(pane, source="visible"))
        if edit is None or not edit.complete:
            _answer_outcome(
                4,
                "needs_manual",
                "navigation",
                "editor is scrolled or not fully visible",
            )
        _verify_blocked(agent, pane, identity, input_sent)
        _send(build_pane_send_text_argv(pane, text), input_sent)
        input_sent = "text"
        text_settled = _poll_until(
            lambda: (
                re.sub(r"\s+", "", _visible_editor_text(pane))
                == re.sub(r"\s+", "", text)
            ),
            settle_s,
        )
        if not text_settled:
            _answer_outcome(
                4, "needs_manual", "text", "editor text never matched --text"
            )
        _verify_blocked(agent, pane, identity, input_sent)
        _send(build_pane_send_keys_argv(pane, ["enter"]), input_sent)
        input_sent = "submitted"
    else:
        _verify_blocked(agent, pane, identity, input_sent)
        _send(build_pane_send_keys_argv(pane, ["enter"]), input_sent)
        input_sent = "submitted"

    _finish_answer(pane, input_sent, settle_s)


def _finish_answer(pane: str, input_sent: str, settle_s: float) -> None:
    """Post-submit check: the picker identity must be gone; then report ok."""
    cleared = _poll_until(
        lambda: (
            not herdr_picker.parse_picker(
                read_pane_text(pane, source="visible")
            ).options
        ),
        settle_s,
    )
    if not cleared:
        _answer_outcome(
            5, "submitted_unconfirmed", "submitted", "picker still visible after submit"
        )
    _answer_outcome(0, "ok", input_sent, "submitted")


def cmd_answer(args: argparse.Namespace) -> None:
    """Drive a live worker's question-tool picker by option, or free text via --other."""
    try:
        _run_answer(args)
    except AnswerOutcome as oc:
        print(
            json.dumps(
                {"result": oc.result, "input_sent": oc.input_sent, "reason": oc.reason}
            )
        )
        raise SystemExit(oc.code) from oc
    except RefusedError as exc:
        print(f"[herdr_delegate] {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


CLOSABLE_AGENT_STATUSES = frozenset({"idle", "done"})


def load_backlog_items() -> list[dict[str, object]]:
    """Every backlog item, read fresh from the store (the test seam for `close`)."""
    import dev_status_storage

    return [dict(item) for item in dev_status_storage.load_items()]


def _items_for_agent(agent: str) -> list[dict[str, object]]:
    """Every backlog item whose launched agent name would be ``agent``.

    A full scan, not an id lookup: `agent_name_for` truncates to 32 characters,
    so a long slug's agent name is not its id, and two long slugs can collide.
    """
    return [
        item
        for item in load_backlog_items()
        if isinstance(item.get("id"), str) and agent_name_for(str(item["id"])) == agent
    ]


def parse_tab_get(listing: dict[str, object]) -> dict[str, object] | None:
    """Label and pane count out of a `herdr tab get` envelope; None if malformed."""
    result = listing.get("result")
    tab = result.get("tab") if isinstance(result, dict) else None
    if not isinstance(tab, dict):
        return None
    label = tab.get("label")
    pane_count = tab.get("pane_count")
    if not isinstance(label, str) or not isinstance(pane_count, int):
        return None
    return {"label": label, "pane_count": pane_count}


def _close_snapshot(
    agent: str, expect_pane: str, *, force: bool
) -> tuple[dict[str, str], dict[str, object], dict[str, object] | None]:
    """One read of everything `close` checks; refuses on the first failed check.

    Returns the agent record, the tab record and the resolved item (``None``
    only under ``--force`` when no single item matches the agent name).
    """
    try:
        info = parse_agent_get(herdr(build_agent_get_argv(agent)))
    except RefusedError as exc:
        raise RefusedError(f"no live agent {agent!r} ({exc}); nothing closed") from exc
    if info is None:
        raise RefusedError(f"agent get for {agent!r} returned no recognizable status")
    if not info["pane"] or info["pane"] != expect_pane:
        raise RefusedError(
            f"agent {agent!r} is in pane {info['pane']!r}, expected {expect_pane!r}; "
            "nothing closed"
        )
    if not info["tab"]:
        raise RefusedError(f"agent {agent!r} reports no tab id; nothing closed")

    matches = _items_for_agent(agent)
    item = matches[0] if len(matches) == 1 else None
    if item is None and not force:
        what = "no backlog item" if not matches else f"{len(matches)} backlog items"
        raise RefusedError(
            f"{what} map to agent {agent!r}; refusing to guess (pass --force to "
            "close anyway)"
        )
    if not force:
        status = item.get("status") if item else None
        if status != "done":
            raise RefusedError(
                f"item {item['id']!r} is {status!r}, not done -- land and approve it "  # type: ignore[index]
                "before closing its worker"
            )
        if info["status"] not in CLOSABLE_AGENT_STATUSES:
            raise RefusedError(
                f"agent {agent!r} is {info['status']!r}; only an idle or done "
                "worker's tab is closed"
            )

    try:
        tab = parse_tab_get(herdr(["tab", "get", info["tab"]]))
    except RefusedError as exc:
        raise RefusedError(
            f"tab {info['tab']!r} is gone ({exc}); nothing closed"
        ) from exc
    if tab is None:
        raise RefusedError(f"tab get for {info['tab']!r} returned no recognizable tab")
    if tab["pane_count"] != 1:
        raise RefusedError(
            f"tab {info['tab']!r} holds {tab['pane_count']} panes; closing it would "
            "take another pane with it"
        )
    if item is not None and tab["label"] != item["id"]:
        raise RefusedError(
            f"tab {info['tab']!r} is labelled {tab['label']!r}, not {item['id']!r}; "
            "it is not this worker's tab"
        )
    return info, tab, item


def cmd_close(args: argparse.Namespace) -> None:
    """Close a finished worker's tab: its item done, the agent idle or done.

    Everything is read twice and must agree before the one mutation, which
    closes exactly the tab the first read verified. herdr has no conditional
    close, and the item re-read takes no backlog lock, so a change landing
    between the second read and the close is a residual race; the foreman's
    landing order (close only after ``approve``) is what keeps it theoretical.
    """
    require_herdr_env(os.environ)
    first = _close_snapshot(args.agent, args.expect_pane, force=args.force)
    second = _close_snapshot(args.agent, args.expect_pane, force=args.force)
    info, tab, item = first
    again_info, again_tab, again_item = second
    item_id = item.get("id") if item else None
    again_id = again_item.get("id") if again_item else None
    identity_changed = again_info["tab"] != info["tab"] or again_tab != tab
    # --force waives the item and status checks, so it waives their
    # revalidation too; pane, tab and exclusivity are never waived.
    state_changed = not args.force and (
        again_info["status"] != info["status"]
        or again_id != item_id
        or (again_item or {}).get("status") != (item or {}).get("status")
    )
    if identity_changed or state_changed:
        raise RefusedError(
            f"agent {args.agent!r} changed between checks; nothing closed"
        )
    herdr(["tab", "close", info["tab"]])
    print(
        json.dumps(
            {
                "agent": args.agent,
                "closed_tab": info["tab"],
                "item": item_id,
                "item_status": item.get("status") if item else None,
                "forced": bool(args.force),
            }
        )
    )


RESTART_DEREGISTER_POLLS = 20
RESTART_DEREGISTER_INTERVAL_S = 0.25


def wait_agent_deregistered(
    name: str,
    *,
    retry_advice: str = "Retry `restart` (it relaunches once the name frees)",
) -> None:
    """Poll until no live agent carries ``name``, bounded; refuse if it persists.

    A just-closed tab's agent may take a moment to unregister, and relaunching
    into a still-registered name fails with agent_name_taken. Exhaustion is a
    refusal naming the recovery -- not a silent relaunch attempt.
    ``retry_advice`` lets a different caller (the agent-start retry loop in
    ``spawn_in_new_tab``, which is already retrying on its own) name its own
    recovery instead of `restart`'s, which would be the wrong advice there.
    """
    for _ in range(RESTART_DEREGISTER_POLLS):
        listing = herdr(build_agent_list_argv())
        if name not in parse_agent_names(listing):
            return
        time.sleep(RESTART_DEREGISTER_INTERVAL_S)
    raise RefusedError(
        f"agent '{name}' is still registered after its tab closed. "
        f"{retry_advice} or attach manually: herdr agent attach {name}"
    )


AGENT_START_TIMEOUT_RETRIES = 2
"""Retries after the first `agent start` attempt, for herdr's own `timeout`
error only. Fixed and unconfigurable, matching RESTART_DEREGISTER_POLLS'
style -- this is a narrow, low-frequency failure path, not a tuning knob."""

AGENT_START_RETRY_BACKOFF_BASE_S = 3.0
"""Backoff before retry n (0-indexed) is this times (n + 1): 3s, 6s, ...
A formula rather than a parallel list-of-durations, so raising
AGENT_START_TIMEOUT_RETRIES can never desync from a fixed-length backoff
list and index out of range."""


def prepare_worker_worktree(slug: str) -> str:
    """Validate one item and bootstrap its exact worktree before agent startup."""
    item = resolve_backlog_item(slug)
    if item is None:
        raise RefusedError(f"no backlog item matches {slug!r}")
    safe, reason = serial_safety(item)
    if not safe:
        raise RefusedError(f"item {slug!r} is not safe for delegation: {reason}")
    try:
        config = resolve_worktree_config(slug)
    except WorktreeError as exc:
        raise RefusedError(str(exc)) from exc
    result = subprocess.run(
        [sys.executable, str(DEV_STATUS), "worktree", slug, "--json"],
        cwd=str(config.repo),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RefusedError(
            f"worktree setup failed for {slug!r}: {result.stderr.strip()}"
        )
    try:
        payload = json.loads(result.stdout)
        actual = Path(payload["worktree_path"]).resolve()
    except (ValueError, KeyError, TypeError) as exc:
        raise RefusedError("worktree setup returned no valid path") from exc
    if actual != config.worktree_path:
        raise RefusedError(
            f"worktree setup returned {actual}, expected {config.worktree_path}"
        )
    return str(actual)


def codex_writable_roots() -> list[str]:
    """Runtime paths a backlog worker writes outside its worktree."""
    cache = subprocess.run(
        ["uv", "cache", "dir"], capture_output=True, text=True, check=False
    )
    if cache.returncode != 0 or not cache.stdout.strip():
        raise RefusedError(f"cannot resolve uv cache: {cache.stderr.strip()}")
    return [
        str(agent_toolkit_paths.path_for("work-items").parent),
        str(cli_common.state_dir() / "agent-toolkit"),
        str(Path(cache.stdout.strip()).resolve()),
    ]


def spawn_in_new_tab(
    *,
    cwd: str,
    label: str,
    prompt: str,
    model: str | None,
    kind: str = "pi",
    session_id: str | None = None,
    allow_all_tools: bool = True,
    plugin_dir: str | None = None,
    writable_roots: list[str] | None = None,
    handoff_slug: str | None = None,
) -> dict[str, object]:
    """Create a tab, start an agent in it, and hand it its prompt.

    The one launch sequence, shared by `launch` and `restart` so the two
    cannot drift apart. A `timeout`-coded `agent start` failure is retried
    against a brand new tab/pane (never the one the timeout gave up on --
    herdr's own contract leaves that pane in unspecified, caller-owned state);
    every other failure, and a `timeout` once retries are exhausted, behaves
    exactly as before this existed. Only `agent start` is inside the retried
    section: `tab create` and `agent prompt` failures are different failure
    classes (a live-agent tab must never be closed, and a creation failure has
    no tab to clean up) and must never be retried here.
    """
    name = agent_name_for(label)
    for attempt in range(AGENT_START_TIMEOUT_RETRIES + 1):
        created = herdr(
            build_tab_argv(cwd=cwd, label=label, kind=kind, handoff_slug=handoff_slug)
        )
        result = created["result"]
        pane = result["root_pane"]["pane_id"]  # type: ignore[index]
        tab = result["tab"]["tab_id"]  # type: ignore[index]

        try:
            herdr(
                build_agent_start_argv(
                    name=name,
                    pane=pane,
                    model=model,
                    kind=kind,
                    session_id=session_id,
                    allow_all_tools=allow_all_tools,
                    plugin_dir=plugin_dir,
                    writable_roots=writable_roots,
                )
            )
        except RefusedError as exc:
            refusal = trust_prompt_refusal(pane=pane, kind=kind, exc=exc)
            # No agent is running in the freshly created tab, and herdr leaves
            # pane cleanup to the caller on this failure -- so if we do nothing,
            # the tab leaks as a contentless, unknown-status pane (seen after a
            # failed 2026-09-07 swarm launch). Close it best-effort and surface
            # the (possibly upgraded) launch error either way.
            with contextlib.suppress(RefusedError):
                herdr(["tab", "close", tab])
            if refusal.code != "timeout" or attempt == AGENT_START_TIMEOUT_RETRIES:
                if refusal.code == "timeout":
                    raise RefusedError(
                        f"gave up after {attempt + 1} attempt(s) waiting for "
                        f"agent startup across fresh tabs (name={name!r}); "
                        "check `herdr status`",
                        code=refusal.code,
                    ) from refusal
                raise refusal
            print(
                f"[herdr_delegate] agent start timed out on attempt "
                f"{attempt + 1}; retrying after backoff",
                file=sys.stderr,
            )
            wait_agent_deregistered(
                name, retry_advice="This retry loop will try again shortly"
            )
            time.sleep(AGENT_START_RETRY_BACKOFF_BASE_S * (attempt + 1))
            continue
        # Only reached after a successful `agent start`. A failed `agent
        # prompt` here means the agent DID start -- the tab holds a live
        # agent, closing it would kill it, and it must never be retried.
        herdr(["agent", "prompt", name, prompt])
        if kind == "agy":
            confirm_prompt_receipt(pane=pane, tab=tab, name=name, prompt=prompt)
        summary: dict[str, object] = {
            "tab": tab,
            "pane": pane,
            "agent": name,
            "prompt": prompt,
        }
        if session_id:
            summary["session_id"] = session_id
        return summary
    raise AssertionError("unreachable: loop always returns or raises")


def select_ready(
    prefix: str | None = None,
    claims: BacklogClaimLookup | None = None,
) -> list[BacklogItem]:
    """Select open backlog items matching an optional prefix using claims lookup."""
    if claims is None:
        claims = LocalClaimLookup()
    return claims.ready_items(prefix)


def build_launch_plan(
    items: list[BacklogItem],
    *,
    kind: str = "pi",
    cwd: str = ".",
) -> list[list[str]]:
    """Construct herdr tab-creation argvs for a list of backlog items.

    Pure/read-only: computes the argv lists without executing them or
    spawning any tabs. Actual execution is left to spawn_in_new_tab / herdr.
    """
    return [
        build_tab_argv(cwd=cwd, label=str(item["id"]), kind=kind)
        for item in items
        if "id" in item
    ]


def ready_slugs(claims: BacklogClaimLookup | None = None) -> list[str]:
    """Slugs currently in READY, sourced via select_ready()."""
    return [str(item["id"]) for item in select_ready(claims=claims) if "id" in item]


def herdr(argv: list[str]) -> dict[str, object]:
    """Run a herdr command and return its parsed JSON result."""
    result = subprocess.run(
        ["herdr", *argv], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        code = _parse_herdr_error_code(result.stderr)
        if code is None and _looks_like_json_object(result.stderr):
            # Valid JSON, but not the {"error": {"code": ...}} shape this
            # file knows how to read -- herdr's error envelope may have
            # drifted. Purely descriptive: this function has no notion of
            # what, if anything, a caller would have retried.
            print(
                f"[herdr_delegate] herdr {argv[0]} {argv[1]} returned a JSON "
                f"error with no recognized 'code' field: {result.stderr.strip()}",
                file=sys.stderr,
            )
        raise RefusedError(
            f"herdr {' '.join(argv)} failed: {result.stderr.strip()}", code=code
        )
    if len(argv) >= 2 and argv[0] == "pane" and argv[1] == "read":
        return {"result": {"text": result.stdout}}
    if (
        len(argv) >= 2
        and argv[0] == "pane"
        and argv[1] in ("send-keys", "send-text")
        and not result.stdout.strip()
    ):
        # These commands print nothing on success (herdr 0.x); an empty stdout
        # with exit 0 is success, never an unparseable-output failure.
        return {"result": {}}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RefusedError(
            f"herdr {' '.join(argv)} returned unparseable output: {exc}"
        ) from exc


def read_pane_text(pane: str, *, source: str = "visible") -> str:
    """Read a pane's terminal snapshot via `herdr pane read`."""
    res = herdr(["pane", "read", pane, "--source", source])
    result = res.get("result")
    if isinstance(result, dict) and isinstance(result.get("text"), str):
        return result["text"]
    return ""


def trust_prompt_refusal(*, pane: str, kind: str, exc: RefusedError) -> RefusedError:
    """Upgrade an agy ``agent_not_ready`` refusal to a trust-prompt message.

    Reads the pane *before* the caller closes the tab (the pane still shows
    agy's startup text at that point) and, if it carries the workspace-trust
    dialog, returns a distinct refusal so a future agy change surfaces as
    "trust prompt" rather than a generic blocked startup. Best-effort: a
    failed pane read falls through to ``exc`` unchanged and never masks it.
    """
    if kind != "agy" or exc.code != "agent_not_ready":
        return exc
    try:
        text = read_pane_text(pane)
    except RefusedError:
        return exc
    if agy_workspace_trust.TRUST_PROMPT_TEXT in text:
        return RefusedError(
            "agy is blocked on its workspace-trust prompt "
            f"({agy_workspace_trust.TRUST_PROMPT_TEXT!r}); auto-trust did not "
            "take effect -- agy may have changed how it stores trust. Check "
            "~/.gemini/antigravity-cli/settings.json trustedWorkspaces.",
            code=exc.code,
        )
    return exc


def trust_agy_dir(dir_path: str) -> None:
    """Ensure agy's trustedWorkspaces lists ``dir_path`` before an agy launch.

    Translates the trust module's own error into a :class:`RefusedError` so it
    surfaces as the usual ``[herdr_delegate] ...`` refusal (``main`` only catches
    ``RefusedError``), not a traceback.
    """
    try:
        agy_workspace_trust.ensure_trusted(dir_path)
    except agy_workspace_trust.TrustSettingsError as exc:
        raise RefusedError(str(exc)) from exc


def prune_agy_trust_entries() -> None:
    """Best-effort prune of vanished agy trusted-workspace entries.

    Runs before every agy launch/probe so entries for since-removed worktrees
    and probe dirs don't accumulate. Never blocks the launch: a failure warns
    on stderr and returns.
    """
    try:
        agy_workspace_trust.prune_trusted()
    except agy_workspace_trust.TrustSettingsError as exc:
        print(
            f"[herdr_delegate] warning: cannot prune agy trusted workspaces: {exc}",
            file=sys.stderr,
        )


def confirm_prompt_receipt(
    *,
    pane: str,
    tab: str,
    name: str,
    prompt: str,
    timeout_s: float = AGY_PROMPT_TIMEOUT_S,
    poll_interval_s: float = AGY_PROMPT_POLL_INTERVAL_S,
    max_resends: int = 1,
) -> None:
    """Poll pane for the echoed prompt line; resend if absent, fail loudly if still missing."""
    target = prompt.strip()
    resends_left = max_resends
    while True:
        deadline = time.monotonic() + timeout_s
        while True:
            text = read_pane_text(pane)
            if target in text:
                return
            if time.monotonic() >= deadline:
                break
            time.sleep(poll_interval_s)
            if time.monotonic() >= deadline:
                break

        if resends_left > 0:
            resends_left -= 1
            listing = herdr(build_agent_list_argv())
            if parse_agent_status(listing, name) == "working":
                return
            herdr(["agent", "prompt", name, prompt])
            continue

        raise RefusedError(
            f"prompt was not received in tab {tab} (pane {pane}); check `herdr pane read {pane}`"
        )


def _looks_like_json_object(text: str) -> bool:
    """Whether ``text`` parses as a JSON object (used only to decide whether
    a missing/unrecognized ``error.code`` is worth a drift warning)."""
    try:
        return isinstance(json.loads(text), dict)
    except (json.JSONDecodeError, ValueError):
        return False


def cmd_plan(_args: argparse.Namespace) -> None:
    """Print the READY queue grouped by prefix, as JSON. No side effects."""
    print(json.dumps({"prefixes": group_by_prefix(ready_slugs())}, indent=2))


def cmd_launch(args: argparse.Namespace) -> None:
    """Create a tab, start a worker or orchestrator, and hand it its prompt."""
    require_herdr_env(os.environ)
    kind = getattr(args, "kind", "pi")
    plugin_dir = COPILOT_PLUGIN_DIR if kind == "copilot" else None
    # copilot only: --session-id belongs on every copilot `agent start`
    # (see copilot/skills/swarm/SKILL.md's "key invariants") so this launch
    # is nameable for a later --resume=<id>, the same as a swarm_spawn worker.
    session_id = str(uuid.uuid4()) if kind == "copilot" else None
    handoff_slug = args.slug if args.handoff else None
    if args.slug:
        check_launchable(slug=args.slug)
        label, prompt = (
            args.slug,
            worker_prompt(args.slug, kind=kind, handoff=args.handoff),
        )
    elif args.serial:
        prefix = check_serial_prefix(args.prefix)
        label = f"serial-{prefix}"
        prompt = serial_orchestrator_prompt(prefix, args.items, args.exclude)
    else:
        prefix = canonical_prefix(args.prefix)
        check_launchable(prefix=prefix)
        label = f"swarm-{prefix}"
        prompt = orchestrator_prompt(
            args.swarm, prefix, kind=kind, items=args.items, exclude=args.exclude
        )

    if not args.slug:
        conflicts = live_queue_orchestrators(prefix)
        if conflicts:
            rendered = ", ".join(f"{other} in {tab}" for other, tab in conflicts)
            raise RefusedError(
                f"queue orchestrator already live for '{prefix}-': {rendered}; "
                "resume or restart that run instead"
            )

    cwd = args.cwd or os.getcwd()
    writable_roots: list[str] | None = None
    if kind in ("agy", "codex"):
        cwd = prepare_worker_worktree(args.slug)
        if kind == "agy":
            prune_agy_trust_entries()
            trust_agy_dir(cwd)
        elif kind == "codex":
            writable_roots = codex_writable_roots()

    print(
        json.dumps(
            spawn_in_new_tab(
                cwd=cwd,
                label=label,
                prompt=prompt,
                model=args.model,
                kind=kind,
                session_id=session_id,
                plugin_dir=plugin_dir,
                writable_roots=writable_roots,
                handoff_slug=handoff_slug,
            )
        )
    )


def cmd_restart(args: argparse.Namespace) -> None:
    """Close a live swarm orchestrator's tab, relaunch it, and prompt it to resume.

    Ordered so nothing destructive happens before everything checkable has
    passed: the runId is resolved and validated first, so a failure there
    leaves the running orchestrator untouched. Workers are never touched --
    only the orchestrator's own tab is a close candidate.
    """
    require_herdr_env(os.environ)
    mode = "serial" if args.serial else "concurrent"
    prefix = canonical_prefix(args.prefix)
    if args.serial:
        prefix = check_serial_prefix(prefix)
    else:
        check_launchable(prefix=prefix)
    kind = getattr(args, "kind", "pi")
    plugin_dir = COPILOT_PLUGIN_DIR if kind == "copilot" else None
    # See cmd_launch: the relaunched orchestrator is a brand new copilot
    # process (its own tab was just closed below), so it gets its own fresh
    # session-id the same way, not the closed tab's.
    session_id = str(uuid.uuid4()) if kind == "copilot" else None
    label = f"serial-{prefix}" if args.serial else f"swarm-{prefix}"

    run_id = resolve_resume_run_id(prefix, args.run_id, kind=kind, mode=mode)

    orchestrators = live_queue_orchestrators(prefix)
    conflicting = [(other, tab) for other, tab in orchestrators if other != label]
    if conflicting:
        rendered = ", ".join(f"{other} in {tab}" for other, tab in conflicting)
        raise RefusedError(
            f"another queue mode is already live for '{prefix}-': {rendered}; "
            "stop it before restarting this run"
        )
    matches = [tab for other, tab in orchestrators if other == label]
    if len(matches) > 1:
        raise RefusedError(
            f"{len(matches)} live tabs carry the label '{label}' ({', '.join(matches)}); "
            "refusing to guess which one is the orchestrator. Close all but one by "
            "hand, then retry."
        )
    closed_tab: str | None = None
    if matches:
        closed_tab = matches[0]
        herdr(["tab", "close", closed_tab])
        wait_agent_deregistered(agent_name_for(label))

    prompt = (
        serial_orchestrator_resume_prompt(run_id, prefix)
        if args.serial
        else orchestrator_resume_prompt(args.swarm, run_id, prefix, kind=kind)
    )
    summary = spawn_in_new_tab(
        cwd=args.cwd,
        label=label,
        prompt=prompt,
        model=args.model,
        kind=kind,
        session_id=session_id,
        plugin_dir=plugin_dir,
    )
    summary["closed_tab"] = closed_tab
    summary["resumed"] = run_id
    print(json.dumps(summary))


def cmd_probe(args: argparse.Namespace) -> None:
    """Launch an agent in a scratch tab to confirm readiness and prompt receipt."""
    require_herdr_env(os.environ)
    scratch_dir: str | None = None
    if args.cwd:
        explicit_cwd = Path(args.cwd).resolve()
        if is_inside_git_repo(explicit_cwd):
            raise RefusedError(
                f"probe --cwd cannot be inside a git repository: {args.cwd}"
            )
        cwd = str(explicit_cwd)
    else:
        scratch_dir = tempfile.mkdtemp(prefix="herdr-probe-")
        cwd = scratch_dir

    kind = args.kind
    label = f"probe-{kind}-{uuid.uuid4().hex[:6]}"
    name = agent_name_for(label)
    plugin_dir = COPILOT_PLUGIN_DIR if kind == "copilot" else None
    session_id = str(uuid.uuid4()) if kind == "copilot" else None
    writable_roots = codex_writable_roots() if kind == "codex" else None
    trivial_prompt = PROBE_PROMPT

    tab_id: str | None = None
    try:
        if kind == "agy":
            prune_agy_trust_entries()
            agy_workspace_trust.ensure_trusted(cwd)
        created = herdr(build_tab_argv(cwd=cwd, label=label, kind=kind))
        result = created["result"]
        pane = result["root_pane"]["pane_id"]  # type: ignore[index]
        tab_id = result["tab"]["tab_id"]  # type: ignore[index]

        try:
            herdr(
                build_agent_start_argv(
                    name=name,
                    pane=pane,
                    model=args.model,
                    kind=kind,
                    session_id=session_id,
                    plugin_dir=plugin_dir,
                    writable_roots=writable_roots,
                )
            )
        except RefusedError as exc:
            # Same upgrade as spawn_in_new_tab: an agy startup blocked on the
            # trust dialog surfaces as a distinct reason rather than a generic
            # startup failure.
            raise trust_prompt_refusal(pane=pane, kind=kind, exc=exc)
        herdr(["agent", "prompt", name, trivial_prompt])
        confirm_prompt_receipt(
            pane=pane,
            tab=tab_id,
            name=name,
            prompt=trivial_prompt,
        )
        print(f"probe {kind}: PASS")
    except Exception as exc:
        print(f"probe {kind}: FAIL ({exc})", file=sys.stderr)
        raise SystemExit(1) from exc
    finally:
        if tab_id:
            with contextlib.suppress(RefusedError):
                herdr(["tab", "close", tab_id])
        if scratch_dir is not None:
            shutil.rmtree(scratch_dir, ignore_errors=True)
            if kind == "agy":
                try:
                    agy_workspace_trust.remove_trusted(scratch_dir)
                except Exception as exc:
                    print(
                        f"[herdr_delegate] warning: failed to untrust probe dir "
                        f"{scratch_dir}: {exc}",
                        file=sys.stderr,
                    )


def main() -> None:
    """Parse arguments and dispatch."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="READY queue grouped by prefix, as JSON")
    plan.set_defaults(func=cmd_plan)

    launch = sub.add_parser(
        "launch", help="start a worker, or a pi/copilot queue orchestrator"
    )
    # Flat rather than a mutually exclusive group: gen_interfaces.py extracts a
    # subcommand's flags from add_argument calls on the subparser itself, so a
    # group's members are invisible to it and every doc example citing them
    # reads as an unknown flag. Exclusivity is enforced below instead.
    launch.add_argument("--slug", help="single item for one unattended worker")
    launch.add_argument("--swarm", type=int, help="fan out across N workers")
    launch.add_argument(
        "--serial", action="store_true", help="run a prefix queue one worker at a time"
    )
    launch.add_argument(
        "--prefix", help="queue scope, required with --swarm or --serial"
    )
    launch.add_argument(
        "--items",
        nargs="+",
        help="explicit slugs for the queue (with --swarm or --serial)",
    )
    launch.add_argument(
        "--exclude",
        nargs="+",
        help="slugs to skip in the prefix queue (with --swarm or --serial)",
    )
    launch.add_argument(
        "--model", help="model passed through to harness after a bare --"
    )
    launch.add_argument("--cwd", help="working directory (pi/copilot only)")
    launch.add_argument(
        "--kind",
        choices=["pi", "copilot", "agy", "codex"],
        default="pi",
        help="agent harness (agy/codex support --slug only; default: pi)",
    )
    launch.add_argument(
        "--handoff",
        action="store_true",
        help="worker stops after its commit gate and prints the FOREMAN-HANDOFF "
        "line, leaving the landing to the foreman (--slug --kind pi only)",
    )
    launch.set_defaults(func=cmd_launch)

    watch = sub.add_parser(
        "watch", help="report the first of N agents that needs attention"
    )
    watch.add_argument("agents", nargs="+", help="agent names to watch")
    watch.add_argument(
        "--prefix", help="also watch agents whose name has this slug-head prefix"
    )
    watch.add_argument("--state", required=True, help="watch state file")
    watch.add_argument(
        "--ack",
        action="append",
        metavar="NAME=SEQ",
        help="acknowledge NAME at SEQ before polling (repeatable)",
    )
    watch.add_argument(
        "--ack-gone",
        action="append",
        metavar="NAME",
        help="acknowledge a gone agent before polling (repeatable)",
    )
    watch.add_argument(
        "--grace-s",
        type=float,
        default=WATCH_DEFAULT_GRACE_S,
        help="suppress idle reports for this long after first sight (default 60)",
    )
    watch.add_argument(
        "--timeout-s",
        type=float,
        required=True,
        help="exit 124 after this long with nothing to report",
    )
    watch.add_argument(
        "--poll-s",
        type=float,
        default=WATCH_DEFAULT_POLL_S,
        help="seconds between polls (default 15)",
    )
    watch.add_argument(
        "--strict",
        action="store_true",
        help="reject positional names absent from the first listing",
    )
    watch.add_argument(
        "--cost-cap-agent",
        type=_positive_finite,
        metavar="USD",
        help="alert when a watched pi agent's session cost reaches USD "
        "(pi's price-table estimate, not provider billing)",
    )
    watch.add_argument(
        "--cost-cap-total",
        type=_positive_finite,
        metavar="USD",
        help="alert when every pi session's cost since local midnight reaches USD",
    )
    watch.add_argument(
        "--cost-step",
        type=_positive_finite,
        default=COST_DEFAULT_STEP,
        metavar="USD",
        help="an acked cost alert re-fires after this much more spend (default 1.0)",
    )
    watch.add_argument(
        "--cost-remind-s",
        type=_positive_finite,
        default=COST_DEFAULT_REMIND_S,
        metavar="N",
        help="an acked alert whose spend is not observable (unmeasured, "
        "incomplete, overflow) re-fires after N seconds (default 900)",
    )
    watch.add_argument(
        "--ack-cost",
        action="append",
        metavar="TOKEN",
        help="acknowledge a cost alert by the 'ack' token its report printed "
        "(repeatable; needs the cap that produced it)",
    )
    watch.set_defaults(func=cmd_watch)

    answer = sub.add_parser(
        "answer",
        help="drive a live worker's question-tool picker by option, or free text via --other",
    )
    answer.add_argument("agent", help="the worker's herdr agent name")
    answer.add_argument(
        "--expect-pane", required=True, help="the pane this worker is parked in"
    )
    answer.add_argument("--option", help="match an option by label")
    answer.add_argument("--index", type=int, help="select by 1-based option index")
    answer.add_argument(
        "--other", action="store_true", help="select the free-text escape option"
    )
    answer.add_argument("--text", help="free-text to send (only with --other)")
    answer.add_argument(
        "--settle-s",
        type=float,
        default=5.0,
        help="seconds to wait for the highlight/editor to settle (default 5)",
    )
    answer.set_defaults(func=cmd_answer)

    close = sub.add_parser(
        "close",
        help="close a finished worker's tab (its item done, the agent idle or done)",
    )
    close.add_argument("agent", help="the worker's herdr agent name")
    close.add_argument(
        "--expect-pane", required=True, help="the pane this worker runs in"
    )
    close.add_argument(
        "--force",
        action="store_true",
        help="operator override: skip the item-done and agent-status checks "
        "(never the pane, tab or exclusivity checks)",
    )
    close.set_defaults(func=cmd_close)

    restart = sub.add_parser(
        "restart",
        help="close a live swarm orchestrator's tab, relaunch it, resume the same run",
    )
    restart.add_argument("--swarm", type=int, help="fan out across N workers")
    restart.add_argument(
        "--serial", action="store_true", help="resume a one-worker serial queue"
    )
    restart.add_argument(
        "--prefix", help="queue scope, required with --swarm or --serial"
    )
    restart.add_argument(
        "--run-id", help="runId to resume; discovered from persisted state when omitted"
    )
    restart.add_argument(
        "--model", help="model passed through to harness after a bare --"
    )
    restart.add_argument("--cwd", default=os.getcwd(), help="working directory")
    restart.add_argument(
        "--kind",
        choices=["pi", "copilot"],
        default="pi",
        help="agent harness (pi or copilot; default: pi)",
    )
    restart.set_defaults(func=cmd_restart)

    probe = sub.add_parser(
        "probe",
        help="verify agent readiness and prompt receipt in a scratch tab",
    )
    probe.add_argument(
        "--kind",
        choices=["pi", "copilot", "agy", "codex"],
        required=True,
        help="agent harness to probe",
    )
    probe.add_argument(
        "--model", help="model passed through to harness after a bare --"
    )
    probe.add_argument("--cwd", help="scratch tab working directory")
    probe.set_defaults(func=cmd_probe)

    args = parser.parse_args()
    if args.command == "launch":
        selectors = (
            int(bool(args.slug)) + int(args.swarm is not None) + int(args.serial)
        )
        if selectors != 1:
            parser.error("pass exactly one of --slug, --swarm, or --serial")
        if (args.swarm is not None or args.serial) and not args.prefix:
            parser.error(
                "--swarm and --serial require --prefix; an unscoped queue mixes projects"
            )
        if args.items and args.exclude:
            parser.error("--items and --exclude are mutually exclusive")
        if (args.items or args.exclude) and args.slug:
            parser.error(
                "--items and --exclude are only valid with --swarm or --serial"
            )
        for slug in args.items or []:
            err = validate_slug(slug, "--items")
            if err:
                parser.error(err)
        for slug in args.exclude or []:
            err = validate_slug(slug, "--exclude")
            if err:
                parser.error(err)
        if args.kind in ("agy", "codex"):
            if not args.slug:
                parser.error(f"--kind {args.kind} supports single-item --slug only")
            if args.cwd is not None:
                parser.error(f"--kind {args.kind} resolves the worktree; omit --cwd")
        if args.handoff:
            if not args.slug:
                parser.error("--handoff requires a single --slug target")
            if args.kind != "pi":
                parser.error(
                    "--handoff is pi-only (other kinds do not run the pi FOREMAN-HANDOFF path)"
                )
    if args.command == "answer":
        selectors = (
            int(args.option is not None) + int(args.index is not None) + int(args.other)
        )
        if selectors != 1:
            parser.error("pass exactly one of --option, --index, or --other")
        if args.other and not args.text:
            parser.error("--other requires --text")
        if args.text and not args.other:
            parser.error("--text is only valid with --other")
    if args.command == "restart":
        selectors = int(args.swarm is not None) + int(args.serial)
        if selectors != 1:
            parser.error("pass exactly one of --swarm or --serial for restart")
        if not args.prefix:
            parser.error(
                "--swarm and --serial require --prefix; an unscoped queue mixes projects"
            )
        if args.run_id is not None:
            try:
                args.run_id = validate_run_id(args.run_id)
            except RefusedError as exc:
                parser.error(str(exc))
    try:
        args.func(args)
    except RefusedError as exc:
        print(f"[herdr_delegate] {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
