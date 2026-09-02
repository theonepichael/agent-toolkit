#!/usr/bin/env python3
"""SessionStart hook + CLI: flag when a managed symlink on this machine no
longer points where links.toml says it should.

Why this exists
---------------
install.py's ``--check-links`` already finds this drift, and finds it in
0.1s -- but nothing ever runs it. A link can sit wrong for days, and the
symptom when it finally bites is silence: a dangling symlink means the
harness simply never loads whatever it provided, with no error anywhere.

The recurring cause is a hand-repointed link. Live verification of an
extension change tempts you to aim the installed link at the worktree you
are working in; the worktree is then removed at cleanup and the link
dangles. This has happened three times -- ``custom-footer.ts``, then
``permission-gate.ts`` and ``swarm-tool.ts`` on 2026-09-02 -- each time
losing a pi extension silently, well after the change that caused it.

Prefer ``pi -e <path>`` over repointing a link: it loads an extension for
one session and leaves no state behind to forget to undo.

What it reports
---------------
Whatever ``--check-links`` reports, condensed to one line per bucket plus a
pointer at the full audit. Silent and exit 0 when the machine is clean, so
it costs a session nothing to have running.

Usage:
    link_drift_check.py check   print a line per drifted bucket (default)

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
AUDIT_TIMEOUT_SECONDS = 15


def _audit(
    run_command: object = subprocess.run,
) -> subprocess.CompletedProcess[str] | None:
    """Run install.py's read-only link audit, or None if it cannot run."""
    installer = REPO / "install.py"
    if not installer.is_file():
        return None
    try:
        return run_command(  # type: ignore[operator]
            [sys.executable, str(installer), "--check-links"],
            capture_output=True,
            text=True,
            timeout=AUDIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _drift_lines(stdout: str) -> list[str]:
    """Pull the audit's bucket headers ("wrong-target (2):") out of its report.

    Parsing the headers rather than reimplementing the audit keeps links.toml
    the single source of truth: a bucket added to install.py shows up here
    with no change, and the two can never disagree about what counts as drift.
    """
    lines = []
    for raw in stdout.splitlines():
        stripped = raw.strip()
        if stripped.endswith(":") and "(" in stripped and stripped[0].isalpha():
            lines.append(stripped.rstrip(":"))
    return lines


def cmd_check(quiet: bool = False, run_command: object = subprocess.run) -> None:
    result = _audit(run_command)
    # No installer, an unreadable one, or a crashed audit is not this hook's
    # problem to report -- staying silent beats a session-start warning about
    # the checker rather than the machine.
    if result is None or result.returncode == 0:
        return
    buckets = _drift_lines(result.stdout)
    summary = "; ".join(buckets) if buckets else "see the full audit"
    cli_common.qprint(
        f"links: {summary} — run `python3 {REPO}/install.py --check-links`",
        quiet=quiet,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Flag managed symlinks that no longer point where "
        "links.toml says they should."
    )
    # --quiet/-v are defined once, on every leaf subcommand parser only (via
    # this shared `parents=` parser) -- never on `parser` itself. See
    # dev_status.py's build_parser() for the full rationale.
    verbosity_parent = argparse.ArgumentParser(add_help=False)
    cli_common.add_verbosity_args(verbosity_parent)
    subparsers = parser.add_subparsers(dest="subcommand")
    subparsers.add_parser(
        "check",
        help="print a line per drifted bucket (default)",
        parents=[verbosity_parent],
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    cmd_check(quiet=getattr(args, "quiet", False))


if __name__ == "__main__":
    main()
