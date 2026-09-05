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
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

# Deliberately NOT .resolve()'d: post meta-agent-toolkit-migration-cutover,
# dev_status.py lives in agent-toolkit's checkout while this script stays in
# dotfiles', so resolving the symlink back to dotfiles' own (now dev_status.py
# -less) claude/scripts/ would break the import. Path(__file__).parent stays
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

    created = herdr(build_tab_argv(cwd=args.cwd, label=label))
    result = created["result"]
    pane = result["root_pane"]["pane_id"]  # type: ignore[index]
    tab = result["tab"]["tab_id"]  # type: ignore[index]

    name = label.replace("_", "-")[:32]
    herdr(build_agent_start_argv(name=name, pane=pane, model=args.model))
    herdr(["agent", "prompt", name, prompt])
    print(json.dumps({"tab": tab, "pane": pane, "agent": name, "prompt": prompt}))


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

    args = parser.parse_args()
    if args.command == "launch":
        if bool(args.slug) == bool(args.swarm):
            parser.error("pass exactly one of --slug or --swarm")
        if args.swarm and not args.prefix:
            parser.error("--swarm requires --prefix; an unscoped queue mixes projects")
    try:
        args.func(args)
    except RefusedError as exc:
        print(f"[herdr_delegate] {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
