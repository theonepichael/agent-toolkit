#!/usr/bin/env python3
"""Launch pi agents in herdr tabs to work backlog items.

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

Usage:
    herdr_delegate.py plan
    herdr_delegate.py launch --slug <slug> [--model <model>]
    herdr_delegate.py launch --swarm <N> --prefix <prefix> [--model <model>]
    herdr_delegate.py restart --swarm <N> --prefix <prefix> [--run-id <runId>]
                              [--model <model>]
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

# Deliberately NOT .resolve()'d: post meta-agent-toolkit-migration-cutover,
# dev_status.py lives in agent-toolkit's checkout while this script stays in
# dotfiles', so resolving the symlink back to dotfiles' own (now dev_status.py
# -less) agent-scripts/ would break the import. Path(__file__).parent stays
# at the LIVE installed directory (~/.claude/scripts/) instead, where both
# files are siblings regardless of which repo's checkout each one symlinks
# back to -- Python's import machinery follows a module's symlink itself,
# same as any other file open.
sys.path.insert(0, str(Path(__file__).parent))

from dev_status import (  # noqa: E402
    HARNESS_REPO,
    REPO_PREFIXES,
    is_worker_safe,
    prefix_of,
)

UNATTENDED_ENV = "PI_AGENT_UNATTENDED=1"
"""Set on the tab so it is in pi's environment before pi starts."""

# `prefix_of` and `is_worker_safe` are imported, never redefined. The whole
# point of the prefix scheme is one source of truth for "may a worker take
# this?"; a second copy here would be the drift this design exists to avoid.

DEV_STATUS = Path(__file__).parent / "dev_status.py"


class RefusedError(RuntimeError):
    """A launch that must not proceed, with a reason fit to show the user."""


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
    if not is_worker_safe(target):
        known = ", ".join(f"{p}-" for p in sorted(REPO_PREFIXES.values()))
        raise RefusedError(
            f"'{target}-' is not a worker-safe prefix. Either it names the "
            f"harness repo ({HARNESS_REPO}), so a worker would be editing the "
            "code it is running, or it is unrecognised -- unknown prefixes are "
            f"refused rather than assumed safe. Known prefixes: {known}. Work "
            "these in a normal session instead."
        )


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


def build_tab_argv(*, cwd: str, label: str) -> list[str]:
    """`herdr tab create` argv. The env pair is what makes the worker unattended."""
    return [
        "tab",
        "create",
        "--cwd",
        cwd,
        "--label",
        label,
        "--env",
        UNATTENDED_ENV,
        "--no-focus",
    ]


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


def build_agent_start_argv(*, name: str, pane: str, model: str | None) -> list[str]:
    """`herdr agent start` argv, with any model passed through after a bare ``--``."""
    argv = ["agent", "start", name, "--kind", "pi", "--pane", pane]
    if model:
        argv += ["--", "--model", model]
    return argv


def worker_prompt(slug: str) -> str:
    """One worker, one item, unattended."""
    return f"/backlog-item --auto {slug}"


def orchestrator_prompt(concurrency: int) -> str:
    """One orchestrator; `swarm_spawn` owns the fan-out from here."""
    return f"/backlog-item --swarm={concurrency}"


def orchestrator_resume_prompt(concurrency: int, run_id: str, prefix: str) -> str:
    """One orchestrator, resuming an interrupted run.

    Single line, deliberately: pi's prompt templates take arguments from the
    command line itself, and extra prose after a slash command is not a
    documented mechanism. Everything `resume` means lives in
    pi/prompts/backlog-item.md's --swarm section; this only has to match what
    that parser accepts.
    """
    return f"/backlog-item --swarm={concurrency} resume {run_id} --prefix {prefix}"


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


def swarm_state_dir() -> Path:
    """Where swarm-tool.ts persists per-runId state (same override, same default)."""
    override = os.environ.get("PI_SWARM_STATE_DIR")
    if override:
        return Path(override)
    return Path.home() / ".pi" / "agent" / "state"


def state_matches_prefix(state: object, prefix: str) -> bool:
    """Whether one parsed state file belongs to a run scoped to ``prefix``.

    Exact field first -- swarm-tool.ts stamps ``prefix`` on fresh state --
    with a slug-scan fallback for files written before the field existed. A
    file with a DIFFERENT prefix field never matches by slug luck: the field
    is the deliberate answer, the scan is only for legacy files that lack it.
    """
    if not isinstance(state, dict):
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


def discover_run_id(prefix: str) -> str:
    """The runId of the newest state file belonging to ``prefix``.

    Refuses rather than falling back to a fresh run: a restart that cannot
    name the run it is resuming must not silently launch a new one -- the
    previous run's workers are deliberately still alive, and a fresh runId
    would neither adopt nor reconcile them. The refusal names the two
    deliberate exits instead (explicit --run-id, or launch for a conscious
    fresh start).
    """
    state_dir = swarm_state_dir()
    if not state_dir.is_dir():
        raise RefusedError(
            f"no swarm state dir at {state_dir}, so there is no run to resume. "
            "Pass --run-id explicitly, or use launch for a deliberate fresh start."
        )
    files = sorted(
        state_dir.glob("swarm-*.json"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    skipped: list[str] = []
    for path in files:
        try:
            state: object = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError, ValueError):
            skipped.append(path.name)
            continue
        if state_matches_prefix(state, prefix):
            run_id = ""
            if isinstance(state, dict):
                candidate = state.get("runId")
                if isinstance(candidate, str):
                    run_id = candidate
            if not run_id:
                run_id = path.stem.removeprefix("swarm-")
            return run_id
    detail = f" ({len(skipped)} unparseable, e.g. {skipped[0]})" if skipped else ""
    raise RefusedError(
        f"no swarm state file matches prefix '{prefix}'{detail}, so there is no "
        "run to resume. Pass --run-id explicitly, or use launch for a deliberate "
        "fresh start."
    )


def resolve_resume_run_id(prefix: str, run_id: str | None) -> str:
    """The runId a restart will resume. Explicit always wins; discovery next."""
    if run_id is not None:
        return validate_run_id(run_id)
    return discover_run_id(prefix)


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


RESTART_DEREGISTER_POLLS = 20
RESTART_DEREGISTER_INTERVAL_S = 0.25


def wait_agent_deregistered(name: str) -> None:
    """Poll until no live agent carries ``name``, bounded; refuse if it persists.

    A just-closed tab's agent may take a moment to unregister, and relaunching
    into a still-registered name fails with agent_name_taken. Exhaustion is a
    refusal naming the recovery -- retry restart (idempotent: it finds zero
    tabs and relaunches once the name frees) or attach manually -- not a
    silent relaunch attempt.
    """
    for _ in range(RESTART_DEREGISTER_POLLS):
        listing = herdr(build_agent_list_argv())
        if name not in parse_agent_names(listing):
            return
        time.sleep(RESTART_DEREGISTER_INTERVAL_S)
    raise RefusedError(
        f"agent '{name}' is still registered after its tab closed. Retry "
        f"`restart` (it relaunches once the name frees) or attach manually: "
        f"herdr agent attach {name}"
    )


def spawn_in_new_tab(
    *, cwd: str, label: str, prompt: str, model: str | None
) -> dict[str, object]:
    """Create a tab, start pi in it, and hand it its prompt.

    The one launch sequence, shared by `launch` and `restart` so the two
    cannot drift apart.
    """
    created = herdr(build_tab_argv(cwd=cwd, label=label))
    result = created["result"]
    pane = result["root_pane"]["pane_id"]  # type: ignore[index]
    tab = result["tab"]["tab_id"]  # type: ignore[index]

    name = agent_name_for(label)
    try:
        herdr(build_agent_start_argv(name=name, pane=pane, model=model))
    except RefusedError:
        # No agent is running in the freshly created tab, and herdr leaves
        # pane cleanup to the caller on this failure -- so if we do nothing,
        # the tab leaks as a contentless, unknown-status pane (seen after a
        # failed 2026-09-07 swarm launch). Close it best-effort and surface
        # the original launch error either way. A failed `agent prompt` is
        # NOT this case: there the agent did start, the tab holds a live
        # agent, and closing the tab would kill it.
        with contextlib.suppress(RefusedError):
            herdr(["tab", "close", tab])
        raise
    herdr(["agent", "prompt", name, prompt])
    return {"tab": tab, "pane": pane, "agent": name, "prompt": prompt}


def ready_slugs() -> list[str]:
    """Slugs currently in READY, straight from ``dev_status.py ready``."""
    result = subprocess.run(
        [sys.executable, str(DEV_STATUS), "ready"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RefusedError(f"dev_status.py ready failed: {result.stderr.strip()}")
    text = result.stdout
    items = json.loads(text[text.index("[") :])
    return [str(item["id"]) for item in items]


def herdr(argv: list[str]) -> dict[str, object]:
    """Run a herdr command and return its parsed JSON result."""
    result = subprocess.run(
        ["herdr", *argv], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise RefusedError(f"herdr {' '.join(argv)} failed: {result.stderr.strip()}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RefusedError(
            f"herdr {' '.join(argv)} returned unparseable output: {exc}"
        ) from exc


def cmd_plan(_args: argparse.Namespace) -> None:
    """Print the READY queue grouped by prefix, as JSON. No side effects."""
    print(json.dumps({"prefixes": group_by_prefix(ready_slugs())}, indent=2))


def cmd_launch(args: argparse.Namespace) -> None:
    """Create a tab, start pi in it, and hand it its prompt."""
    require_herdr_env(os.environ)
    if args.slug:
        check_launchable(slug=args.slug)
        label, prompt = args.slug, worker_prompt(args.slug)
    else:
        check_launchable(prefix=args.prefix)
        label = f"swarm-{args.prefix}"
        prompt = orchestrator_prompt(args.swarm)

    print(
        json.dumps(
            spawn_in_new_tab(cwd=args.cwd, label=label, prompt=prompt, model=args.model)
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
    check_launchable(prefix=args.prefix)
    label = f"swarm-{args.prefix}"

    run_id = resolve_resume_run_id(args.prefix, args.run_id)

    matches = live_tab_ids_with_label(label)
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

    prompt = orchestrator_resume_prompt(args.swarm, run_id, args.prefix)
    summary = spawn_in_new_tab(
        cwd=args.cwd, label=label, prompt=prompt, model=args.model
    )
    summary["closed_tab"] = closed_tab
    summary["resumed"] = run_id
    print(json.dumps(summary))


def main() -> None:
    """Parse arguments and dispatch."""
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    plan = sub.add_parser("plan", help="READY queue grouped by prefix, as JSON")
    plan.set_defaults(func=cmd_plan)

    launch = sub.add_parser("launch", help="start a pi worker or orchestrator")
    # Flat rather than a mutually exclusive group: gen_interfaces.py extracts a
    # subcommand's flags from add_argument calls on the subparser itself, so a
    # group's members are invisible to it and every doc example citing them
    # reads as an unknown flag. Exclusivity is enforced below instead.
    launch.add_argument("--slug", help="single item for one unattended worker")
    launch.add_argument("--swarm", type=int, help="fan out across N workers")
    launch.add_argument("--prefix", help="queue scope, required with --swarm")
    launch.add_argument("--model", help="model passed through to pi after a bare --")
    launch.add_argument("--cwd", default=os.getcwd(), help="working directory")
    launch.set_defaults(func=cmd_launch)

    restart = sub.add_parser(
        "restart",
        help="close a live swarm orchestrator's tab, relaunch it, resume the same run",
    )
    restart.add_argument("--swarm", type=int, help="fan out across N workers")
    restart.add_argument("--prefix", help="queue scope, required with --swarm")
    restart.add_argument(
        "--run-id", help="runId to resume; discovered from persisted state when omitted"
    )
    restart.add_argument("--model", help="model passed through to pi after a bare --")
    restart.add_argument("--cwd", default=os.getcwd(), help="working directory")
    restart.set_defaults(func=cmd_restart)

    args = parser.parse_args()
    if args.command == "launch":
        if bool(args.slug) == bool(args.swarm):
            parser.error("pass exactly one of --slug or --swarm")
        if args.swarm and not args.prefix:
            parser.error("--swarm requires --prefix; an unscoped queue mixes projects")
    if args.command == "restart":
        if not args.swarm:
            parser.error("--swarm is required for restart")
        if not args.prefix:
            parser.error("--swarm requires --prefix; an unscoped queue mixes projects")
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
