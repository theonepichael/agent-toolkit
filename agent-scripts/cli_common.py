"""Shared CLI helpers used across dotfiles scripts.

Flags
  --quiet, -q    suppress non-essential stdout output
  --verbose, -v  emit extra diagnostic messages to stderr
"""

import argparse
import sys
from typing import TextIO


def add_verbosity_args(parser: argparse.ArgumentParser) -> None:
    """Add mutually-exclusive --quiet/-q and --verbose/-v flags to a parser."""
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="suppress non-essential output",
    )
    group.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="emit extra diagnostic messages to stderr",
    )


def vprint(msg: str, *, verbose: bool, file: TextIO | None = None) -> None:
    """Print a diagnostic message when verbose mode is enabled."""
    if verbose:
        if file is None:
            file = sys.stderr
        print(msg, file=file)


def qprint(msg: str, *, quiet: bool, file: TextIO | None = None) -> None:
    """Print a message unless quiet mode is enabled."""
    if not quiet:
        if file is None:
            file = sys.stdout
        print(msg, file=file)
