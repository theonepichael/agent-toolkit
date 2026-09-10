#!/usr/bin/env python3
"""standup.py — /standup skill CLI and read-only fetch service.

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
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import cli_common
from standup_adapters import (
    ADAPTERS,
    CalendarAdapter,
    CalEvent,
    ChatAdapter,
    EmailAdapter,
    IssueTrackerAdapter,
    Item,
    Message,
    NotConfiguredError,
)

DATA_DIR = Path.home() / ".claude" / "data" / "standup"
CONFIG_FILE = DATA_DIR / "config.json"
BACKLOG_FILE = Path.home() / ".claude" / "data" / "backlog" / "items.json"
CANONICAL_PENDING_FILE = (
    Path.home() / ".claude" / "data" / "backlog" / "pending_items.json"
)


class StandupConfigError(Exception):
    """Raised when caller-supplied standup configuration is invalid."""


@dataclass(frozen=True)
class StandupConfig:
    git_repos: tuple[str, ...] = ()
    work_backlog_prefixes: tuple[str, ...] = ()
    commit_days: int = 1
    recent_done_days: int = 2


@dataclass(frozen=True)
class StandupPaths:
    standup_data_dir: Path
    backlog_file: Path
    pending_file: Path


@dataclass(frozen=True)
class SkippedSource:
    source: str
    reason: str


@dataclass(frozen=True)
class StandupSources:
    issue_tracker: IssueTrackerAdapter | None = None
    chat: ChatAdapter | None = None
    email: EmailAdapter | None = None
    calendar: CalendarAdapter | None = None


@dataclass(frozen=True)
class StandupReport:
    date: date
    since: date
    git_commits: list[dict[str, str]]
    backlog_in_progress: list[dict[str, object]]
    backlog_recent_done: list[dict[str, object]]
    backlog_in_review: list[dict[str, object]]
    assigned_items: list[Item]
    messages: list[Message]
    chat_thread_updates: list[Message]
    email_correspondence: list[Message]
    email_thread_updates: list[Message]
    calendar_events: list[CalEvent]
    pending_items_open: list[dict[str, object]]
    previous_standup: dict[str, str] | None
    skipped_sources: list[SkippedSource]

    def to_json_dict(self) -> dict[str, object]:
        return {
            "date": self.date.isoformat(),
            "since": self.since.isoformat(),
            "git_commits": self.git_commits,
            "backlog_in_progress": self.backlog_in_progress,
            "backlog_recent_done": self.backlog_recent_done,
            "backlog_in_review": self.backlog_in_review,
            "assigned_items": [asdict(i) for i in self.assigned_items],
            "messages": [asdict(m) for m in self.messages],
            "chat_thread_updates": [asdict(m) for m in self.chat_thread_updates],
            "email_correspondence": [asdict(m) for m in self.email_correspondence],
            "email_thread_updates": [asdict(m) for m in self.email_thread_updates],
            "calendar_events": [asdict(e) for e in self.calendar_events],
            "pending_items_open": self.pending_items_open,
            "previous_standup": self.previous_standup,
            "skipped": [asdict(s) for s in self.skipped_sources],
        }


def today() -> str:
    return date.today().isoformat()


def last_working_day(ref: date) -> date:
    # Monday -> back to Friday; every other day -> back one day.
    return ref - timedelta(days=3 if ref.weekday() == 0 else 1)


def find_previous_standup(
    before: date, standup_data_dir: Path = DATA_DIR
) -> dict[str, str] | None:
    for offset in range(1, 15):
        candidate = before - timedelta(days=offset)
        path = standup_data_dir / f"{candidate.isoformat()}.md"
        if path.exists():
            return {"date": candidate.isoformat(), "content": path.read_text()}
    return None


# ── config ────────────────────────────────────────────────────────────────


def load_config(config_file: Path = CONFIG_FILE) -> dict[str, object]:
    if not config_file.exists():
        return {}
    return json.loads(config_file.read_text())


def _config_from_mapping(config: dict[str, object]) -> StandupConfig:
    return StandupConfig(
        git_repos=tuple(str(r) for r in config.get("git_repos", [])),
        work_backlog_prefixes=tuple(
            str(p) for p in config.get("work_backlog_prefixes", [])
        ),
        commit_days=int(config.get("commit_days", 1)),
        recent_done_days=int(config.get("recent_done_days", 2)),
    )


def load_canonical_pending(
    pending_file: Path = CANONICAL_PENDING_FILE,
) -> list[dict[str, object]]:
    """Read-only view of dev_status.py's pending-items store. Mutate via
    `dev_status.py pending add/update`, never here."""
    if not pending_file.exists():
        return []
    data = json.loads(pending_file.read_text())
    items = data.get("items", [])
    if not isinstance(items, list):
        raise StandupConfigError(f"{pending_file} has non-list items")
    return [i for i in items if isinstance(i, dict)]


# ── local sources: git commits ───────────────────────────────────────────


def git_commits(
    repos: list[str], since_days: int
) -> tuple[list[dict[str, str]], list[SkippedSource]]:
    commits: list[dict[str, str]] = []
    skipped: list[SkippedSource] = []
    since = f"{since_days}.days.ago"

    for raw_repo in repos:
        repo = Path(raw_repo).expanduser()
        if not (repo / ".git").exists():
            skipped.append(SkippedSource("git", f"{repo} is not a git repo"))
            continue

        email_result = subprocess.run(
            ["git", "-C", str(repo), "config", "user.email"],
            capture_output=True,
            text=True,
        )
        author = email_result.stdout.strip()
        if not author:
            skipped.append(SkippedSource("git", f"{repo} has no configured user.email"))
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
    prefixes: list[str], recent_done_days: int, backlog_file: Path = BACKLOG_FILE
) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    list[dict[str, object]],
    list[SkippedSource],
]:
    if not prefixes:
        return (
            [],
            [],
            [],
            [
                SkippedSource(
                    "backlog",
                    "work_backlog_prefixes not configured in config.json — backlog source skipped",
                )
            ],
        )
    if not backlog_file.exists():
        return (
            [],
            [],
            [],
            [SkippedSource("backlog", f"{backlog_file} not found")],
        )

    data = json.loads(backlog_file.read_text())
    cutoff = datetime.now() - timedelta(days=recent_done_days)

    in_progress: list[dict[str, object]] = []
    recent_done: list[dict[str, object]] = []
    in_review: list[dict[str, object]] = []
    for item in data.get("items", []):
        if not isinstance(item, dict):
            continue
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


def fetch_standup(
    config: StandupConfig,
    sources: StandupSources,
    *,
    paths: StandupPaths,
    reference_date: date | None = None,
) -> StandupReport:
    skipped: list[SkippedSource] = []
    ref_date = reference_date or date.today()
    since = last_working_day(ref_date)

    commits, git_skips = git_commits(list(config.git_repos), config.commit_days)
    skipped.extend(git_skips)

    in_progress, recent_done, in_review, backlog_skips = backlog_items(
        list(config.work_backlog_prefixes), config.recent_done_days, paths.backlog_file
    )
    skipped.extend(backlog_skips)

    pending_items = load_canonical_pending(paths.pending_file)
    pending_open = [i for i in pending_items if i.get("status") != "resolved"]
    pending_chat = [i for i in pending_open if i.get("kind") == "chat"]
    pending_email = [i for i in pending_open if i.get("kind") == "email"]

    assigned_items: list[Item] = []
    if sources.issue_tracker is not None:
        try:
            assigned_items = sources.issue_tracker.get_assigned_items()
            assigned_items.sort(key=lambda i: i.updated_at, reverse=True)
        except NotConfiguredError as e:
            skipped.append(SkippedSource("issue_tracker", str(e)))

    messages: list[Message] = []
    chat_thread_updates: list[Message] = []
    if sources.chat is not None:
        try:
            messages = sources.chat.get_relevant_messages(since)
            if pending_chat:
                chat_thread_updates = sources.chat.get_thread_updates(pending_chat)
        except NotConfiguredError as e:
            skipped.append(SkippedSource("chat", str(e)))

    email_correspondence: list[Message] = []
    email_thread_updates: list[Message] = []
    if sources.email is not None:
        try:
            email_correspondence = sources.email.get_correspondence(since)
            if pending_email:
                email_thread_updates = sources.email.get_thread_updates(pending_email)
        except NotConfiguredError as e:
            skipped.append(SkippedSource("email", str(e)))

    calendar_events: list[CalEvent] = []
    if sources.calendar is not None:
        try:
            calendar_events = [
                e
                for e in sources.calendar.get_calendar_events([since, ref_date])
                if not e.is_recurring
            ]
        except NotConfiguredError as e:
            skipped.append(SkippedSource("calendar", str(e)))

    return StandupReport(
        date=ref_date,
        since=since,
        git_commits=commits,
        backlog_in_progress=in_progress,
        backlog_recent_done=recent_done,
        backlog_in_review=in_review,
        assigned_items=assigned_items,
        messages=messages,
        chat_thread_updates=chat_thread_updates,
        email_correspondence=email_correspondence,
        email_thread_updates=email_thread_updates,
        calendar_events=calendar_events,
        pending_items_open=pending_open,
        previous_standup=find_previous_standup(ref_date, paths.standup_data_dir),
        skipped_sources=skipped,
    )


def _default_sources() -> StandupSources:
    issue_tracker = ADAPTERS["issue_tracker"]
    chat = ADAPTERS["chat"]
    email = ADAPTERS["email"]
    calendar = ADAPTERS["calendar"]
    return StandupSources(
        issue_tracker=issue_tracker
        if isinstance(issue_tracker, IssueTrackerAdapter)
        else None,
        chat=chat if isinstance(chat, ChatAdapter) else None,
        email=email if isinstance(email, EmailAdapter) else None,
        calendar=calendar if isinstance(calendar, CalendarAdapter) else None,
    )


def cmd_fetch(args: argparse.Namespace) -> None:
    config = _config_from_mapping(load_config())
    ref_date = date.fromisoformat(args.date) if args.date else date.today()
    report = fetch_standup(
        config,
        _default_sources(),
        reference_date=ref_date,
        paths=StandupPaths(
            standup_data_dir=DATA_DIR,
            backlog_file=BACKLOG_FILE,
            pending_file=CANONICAL_PENDING_FILE,
        ),
    )
    print(json.dumps(report.to_json_dict(), indent=2))


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
