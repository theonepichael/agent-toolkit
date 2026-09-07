#!/usr/bin/env python3
"""standup.py — /standup skill CLI: local data gathering.

`fetch` gathers everything a standup draft needs and prints it as JSON: two
fully-implemented local sources (git commits, scoped backlog items) plus
four adapter-backed sources (issue tracker, chat, email, calendar) that stay
stubbed — and get reported under "skipped" — until the workplace's actual
tools are known, plus dev_status.py's canonical pending-items list (read-only —
mutate it via `dev_status.py pending add/update`, not here). All sources
that support a time window use the same `since` boundary (last working day,
or `--date` to override). See standup_adapters.py for the adapter
interfaces.

Flags
  --quiet, -q    suppress non-essential output
  --verbose, -v  emit extra diagnostic messages to stderr
"""

import argparse
import json
import subprocess
import sys
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path

import cli_common
from standup_adapters import ADAPTERS, NotConfiguredError

DATA_DIR = Path.home() / ".claude" / "data" / "standup"
CONFIG_FILE = DATA_DIR / "config.json"
BACKLOG_FILE = Path.home() / ".claude" / "data" / "backlog" / "items.json"
CANONICAL_PENDING_FILE = (
    Path.home() / ".claude" / "data" / "backlog" / "pending_items.json"
)


def today() -> str:
    return date.today().isoformat()


def last_working_day(ref: date) -> date:
    # Monday -> back to Friday; every other day -> back one day.
    return ref - timedelta(days=3 if ref.weekday() == 0 else 1)


def find_previous_standup(before: date) -> dict[str, str] | None:
    for offset in range(1, 15):
        candidate = before - timedelta(days=offset)
        path = DATA_DIR / f"{candidate.isoformat()}.md"
        if path.exists():
            return {"date": candidate.isoformat(), "content": path.read_text()}
    return None


# ── config ────────────────────────────────────────────────────────────────


def load_config() -> dict[str, object]:
    if not CONFIG_FILE.exists():
        return {}
    return json.loads(CONFIG_FILE.read_text())


def load_canonical_pending() -> list[dict[str, object]]:
    """Read-only view of dev_status.py's pending-items store. Mutate via
    `dev_status.py pending add/update`, never here."""
    if not CANONICAL_PENDING_FILE.exists():
        return []
    return json.loads(CANONICAL_PENDING_FILE.read_text()).get("items", [])


# ── local sources: git commits ───────────────────────────────────────────


def git_commits(
    repos: list[str], since_days: int
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    commits: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    since = f"{since_days}.days.ago"

    for raw_repo in repos:
        repo = Path(raw_repo).expanduser()
        if not (repo / ".git").exists():
            skipped.append({"source": "git", "reason": f"{repo} is not a git repo"})
            continue

        email_result = subprocess.run(
            ["git", "-C", str(repo), "config", "user.email"],
            capture_output=True,
            text=True,
        )
        author = email_result.stdout.strip()
        if not author:
            skipped.append(
                {"source": "git", "reason": f"{repo} has no configured user.email"}
            )
            continue

        log_result = subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "log",
                f"--since={since}",
                f"--author={author}",
                "--pretty=format:%h\t%ad\t%s",
                "--date=short",
            ],
            capture_output=True,
            text=True,
        )
        for line in log_result.stdout.splitlines():
            parts = line.split("\t", 2)
            if len(parts) == 3:
                commits.append(
                    {
                        "repo": repo.name,
                        "hash": parts[0],
                        "date": parts[1],
                        "subject": parts[2],
                    }
                )

    return commits, skipped


# ── local sources: backlog ───────────────────────────────────────────────


def backlog_items(
    prefixes: list[str], recent_done_days: int
) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, str]],
]:
    if not prefixes:
        return (
            [],
            [],
            [],
            [
                {
                    "source": "backlog",
                    "reason": "work_backlog_prefixes not configured in config.json — backlog source skipped",
                }
            ],
        )
    if not BACKLOG_FILE.exists():
        return (
            [],
            [],
            [],
            [{"source": "backlog", "reason": f"{BACKLOG_FILE} not found"}],
        )

    data = json.loads(BACKLOG_FILE.read_text())
    cutoff = datetime.now() - timedelta(days=recent_done_days)

    in_progress: list[dict[str, object]] = []
    recent_done: list[dict[str, object]] = []
    in_review: list[dict[str, object]] = []
    for item in data.get("items", []):
        item_id = str(item.get("id", ""))
        if not any(item_id.startswith(p) for p in prefixes):
            continue
        status = item.get("status")
        if status == "in-progress":
            in_progress.append(item)
        elif status == "in-review":
            in_review.append(item)
        elif status == "done":
            updated = str(item.get("updated", ""))
            if updated and updated >= cutoff.isoformat():
                recent_done.append(item)

    return in_progress, recent_done, in_review, []


# ── fetch ─────────────────────────────────────────────────────────────────


def cmd_fetch(args: argparse.Namespace) -> None:
    config = load_config()
    skipped: list[dict[str, str]] = []

    ref_date = date.fromisoformat(args.date) if args.date else date.today()
    since = last_working_day(ref_date)

    commits, git_skips = git_commits(
        [str(r) for r in config.get("git_repos", [])],  # type: ignore[union-attr]
        int(config.get("commit_days", 1)),  # type: ignore[arg-type]
    )
    skipped.extend(git_skips)

    in_progress, recent_done, in_review, backlog_skips = backlog_items(
        [str(p) for p in config.get("work_backlog_prefixes", [])],  # type: ignore[union-attr]
        int(config.get("recent_done_days", 2)),  # type: ignore[arg-type]
    )
    skipped.extend(backlog_skips)

    pending_items = load_canonical_pending()
    pending_open = [i for i in pending_items if i.get("status") != "resolved"]
    pending_chat = [i for i in pending_open if i.get("kind") == "chat"]
    pending_email = [i for i in pending_open if i.get("kind") == "email"]

    assigned_items: list[dict[str, str]] = []
    try:
        assigned_items = [
            asdict(i) for i in ADAPTERS["issue_tracker"].get_assigned_items()
        ]  # type: ignore[attr-defined]
        assigned_items.sort(key=lambda i: str(i.get("updated_at", "")), reverse=True)
    except NotConfiguredError as e:
        skipped.append({"source": "issue_tracker", "reason": str(e)})

    messages: list[dict[str, str]] = []
    chat_thread_updates: list[dict[str, str]] = []
    try:
        messages = [asdict(m) for m in ADAPTERS["chat"].get_relevant_messages(since)]  # type: ignore[attr-defined]
        if pending_chat:
            chat_thread_updates = [
                asdict(m)
                for m in ADAPTERS["chat"].get_thread_updates(pending_chat)  # type: ignore[attr-defined]
            ]
    except NotConfiguredError as e:
        skipped.append({"source": "chat", "reason": str(e)})

    email_correspondence: list[dict[str, str]] = []
    email_thread_updates: list[dict[str, str]] = []
    try:
        email_correspondence = [
            asdict(m)
            for m in ADAPTERS["email"].get_correspondence(since)  # type: ignore[attr-defined]
        ]
        if pending_email:
            email_thread_updates = [
                asdict(m)
                for m in ADAPTERS["email"].get_thread_updates(pending_email)  # type: ignore[attr-defined]
            ]
    except NotConfiguredError as e:
        skipped.append({"source": "email", "reason": str(e)})

    calendar_events: list[dict[str, object]] = []
    try:
        calendar_events = [
            asdict(c)
            for c in ADAPTERS["calendar"].get_calendar_events([since, ref_date])  # type: ignore[attr-defined]
        ]
    except NotConfiguredError as e:
        skipped.append({"source": "calendar", "reason": str(e)})

    result = {
        "date": ref_date.isoformat(),
        "since": since.isoformat(),
        "git_commits": commits,
        "backlog_in_progress": in_progress,
        "backlog_recent_done": recent_done,
        "backlog_in_review": in_review,
        "assigned_items": assigned_items,
        "messages": messages,
        "chat_thread_updates": chat_thread_updates,
        "email_correspondence": email_correspondence,
        "email_thread_updates": email_thread_updates,
        "calendar_events": [e for e in calendar_events if not e.get("is_recurring")],
        "pending_items_open": pending_open,
        "previous_standup": find_previous_standup(ref_date),
        "skipped": skipped,
    }
    print(json.dumps(result, indent=2))


# ── main ──────────────────────────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(description="/standup skill CLI")
    # --quiet/-v are defined once, on every leaf subcommand parser only
    # (via this shared `parents=` parser) -- never on `parser` itself. See
    # dev_status.py's build_parser() for the full rationale.
    verbosity_parent = argparse.ArgumentParser(add_help=False)
    cli_common.add_verbosity_args(verbosity_parent)
    sub = parser.add_subparsers(dest="cmd")

    p = sub.add_parser(
        "fetch", help="gather all sources as JSON", parents=[verbosity_parent]
    )
    p.add_argument(
        "--date",
        default=None,
        help="override reference date (YYYY-MM-DD) — for re-running after a "
        "gap (holiday, PTO) where the default last-working-day boundary "
        "would miss it",
    )

    args = parser.parse_args()

    if args.cmd == "fetch":
        cmd_fetch(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
