#!/usr/bin/env python3
"""SessionStart hook: flag when the dotfiles repo has drifted from the last
commit bundled over to a GitHub-blocked work machine.

Usage:
    dotfiles_sync_check.py check       print a drift note if HEAD is ahead of the marker (default)
    dotfiles_sync_check.py mark [sha]  record the given (or current HEAD) commit as last-bundled

Flags
  --quiet, -q    suppress non-essential output
  --verbose, -v  emit extra diagnostic messages to stderr
"""

import argparse
import subprocess
import sys
from pathlib import Path

import cli_common

REPO = Path(__file__).resolve().parents[2]
STATE_DIR = Path.home() / ".local" / "state" / "dotfiles"
MARKER = STATE_DIR / "last-bundled-commit"


def git(*args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(REPO), *args],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def cmd_check(quiet: bool = False) -> None:
    if not REPO.is_dir() or not MARKER.exists():
        return
    marker_sha = MARKER.read_text().strip()
    if not marker_sha:
        return
    head = git("rev-parse", "HEAD")
    if head is None or head == marker_sha:
        return
    count = git("rev-list", "--count", f"{marker_sha}..{head}")
    if not count or count == "0":
        return
    cli_common.qprint(
        f"dotfiles: {count} commit(s) ahead of last bundled transfer ({marker_sha[:7]})",
        quiet=quiet,
    )


def cmd_mark(sha: str | None, quiet: bool = False) -> None:
    target = sha or git("rev-parse", "HEAD")
    if not target:
        print("dotfiles_sync_check: could not resolve HEAD", file=sys.stderr)
        sys.exit(1)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    MARKER.write_text(target + "\n")
    cli_common.qprint(
        f"dotfiles: marked {target[:7]} as last-bundled commit",
        quiet=quiet,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Flag when the dotfiles repo has drifted from the last "
        "commit bundled over to a GitHub-blocked work machine."
    )
    # --quiet/-v are defined once, on every leaf subcommand parser only
    # (via this shared `parents=` parser) -- never on `parser` itself. See
    # dev_status.py's build_parser() for the full rationale.
    verbosity_parent = argparse.ArgumentParser(add_help=False)
    cli_common.add_verbosity_args(verbosity_parent)
    subparsers = parser.add_subparsers(dest="subcommand")
    subparsers.add_parser(
        "check",
        help="print a drift note if HEAD is ahead of the marker (default)",
        parents=[verbosity_parent],
    )
    mark_parser = subparsers.add_parser(
        "mark",
        help="record the given (or current HEAD) commit as last-bundled",
        parents=[verbosity_parent],
    )
    mark_parser.add_argument(
        "sha", nargs="?", default=None, help="commit to record (defaults to HEAD)"
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    subcommand = args.subcommand or "check"
    quiet = getattr(args, "quiet", False)
    if subcommand == "check":
        cmd_check(quiet=quiet)
    else:
        cmd_mark(args.sha, quiet=quiet)


if __name__ == "__main__":
    main()
