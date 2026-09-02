#!/usr/bin/env python3
"""SessionStart hook + CLI: detect when a harness's instruction-file discovery
behavior may have drifted from the version-pinned facts in README.md.

Why this exists
---------------
README.md records, version-pinned, which instruction filenames each of the
five installed harnesses actually loads (measured live 2026-08-30). The
``AGENTS.md`` + ``CLAUDE.md``-symlink convention rests on the opencode and
Claude Code rows. These are external binaries on independent update schedules;
the day one changes its discovery logic, instruction loading breaks silently —
exactly how the opencode ``~/.claude/CLAUDE.md`` fallback defect sat
undetected for months.

This script provides two tiers:

* ``check`` — cheap, stateless version-pin comparison against the two
  load-bearing harnesses (opencode, Claude Code). Runs as a SessionStart
  hook. Zero API calls. Prints a drift note when the installed version
  differs from the README pin, recommending a live probe.
* ``probe`` — on-demand semantic verification. Rebuilds the audit fixture
  (throwaway git repo with ``AGENTS.md``, ``CLAUDE.md``, ``GEMINI.md`` at
  root and in a subdirectory, each holding a distinct token), drives each
  harness non-interactively, and asserts the measured behavior against
  README expectations. ~10–15 single-turn API calls on cheap/free models.

Mechanism
---------
``check`` is *stateless version-pin comparison*: discovery behavior can only
change when the binary changes, and the README table is version-pinned, so
"installed version ≠ pinned version" is the exact, cheap signal that a row
is no longer verified. ``probe`` is the semantic verifier: it rebuilds the
fixture and asks each harness which tokens are in its context.

No state files anywhere — the README pin itself is the state. This removes
cache corruption, write races, and notification-suppression traps in one
move.

Subcommands
-----------
    check   resolve the load-bearing harness binaries, run ``--version``,
            and compare against the pinned constants. Silent when versions
            match. Prints a one-line note naming harness, installed
            version, pinned version, and the affected row when they differ.
            The note prints every run while the mismatch stands —
            deliberate nag, not one-shot suppression.
    probe   rebuild the audit fixture in a temp directory, drive each
            harness non-interactively, extract token names from the
            response, and compare against expectation constants. Prints a
            per-row table (HOLD / BROKEN / ERROR) with a suggested
            remediation next to any BROKEN row.

Usage
-----
    harness_discovery_check.py check [--hook] [--strict]
    harness_discovery_check.py probe [--harness NAME]

Exits
-----
    check: 0 = clean or noted mismatch, 1 = internal error,
           2 = attention needed under ``--strict``
    probe: 0 = all rows HOLD, nonzero = any BROKEN or ERROR

Flags
  --quiet, -q    suppress non-essential output
  --verbose, -v  emit extra diagnostic messages to stderr
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TextIO

import cli_common

# ── version pins — keep in lockstep with README.md's harness table ──────────
# README.md "Harness instruction-file discovery" table, 2026-08-30
CLAUDE_CODE_PINNED_VERSION: str = "2.1.252"
OPENCODE_PINNED_VERSION: str = "1.18.25"
PI_PINNED_VERSION: str = "0.84.4"
COPILOT_PINNED_VERSION: str = "1.0.80"
AGY_PINNED_VERSION: str = "1.1.22"

# ── semantic expectations — which filenames each harness loads ──────────────
# These encode the measured behavioral facts, not version-specific offsets.
# See README.md "Harness instruction-file discovery" for provenance.
CLAUDE_CODE_EXPECTED_FILENAMES: frozenset[str] = frozenset({"CLAUDE.md"})
OPENCODE_EXPECTED_FILENAMES: frozenset[str] = frozenset({"AGENTS.md"})
PI_PREFERRED_FILENAMES: frozenset[str] = frozenset({"AGENTS.md"})
PI_FALLBACK_FILENAMES: frozenset[str] = frozenset({"CLAUDE.md"})
COPILOT_EXPECTED_FILENAMES: frozenset[str] = frozenset(
    {"CLAUDE.md", "GEMINI.md", "AGENTS.md"}
)
AGY_EXPECTED_FILENAMES: frozenset[str] = frozenset()

# ── harness metadata ─────────────────────────────────────────────────────────

# Fallback paths tried before reporting UNVERIFIABLE (Linux/WSL only).
_FALLBACK_PATHS: dict[str, list[str]] = {
    "claude": ["/home/yanil/.local/bin/claude"],
    "opencode": ["/home/yanil/.opencode/bin/opencode"],
    "pi": ["/home/yanil/.npm-global/bin/pi"],
    "copilot": ["/home/yanil/.npm-global/bin/copilot"],
    "agy": ["/home/yanil/.local/bin/agy"],
}

_LOAD_BEARING: tuple[str, ...] = ("claude", "opencode")

# Token names placed in fixture files so the probe can detect which were
# loaded. Each must be a single substring unlikely to appear in model prose.
_TOKEN_AGENTS_ROOT: str = "FIXTURE_TOKEN_AGENTS_ROOT"
_TOKEN_CLAUDE_ROOT: str = "FIXTURE_TOKEN_CLAUDE_ROOT"
_TOKEN_GEMINI_ROOT: str = "FIXTURE_TOKEN_GEMINI_ROOT"
_TOKEN_AGENTS_SUB: str = "FIXTURE_TOKEN_AGENTS_SUB"
_TOKEN_CLAUDE_SUB: str = "FIXTURE_TOKEN_CLAUDE_SUB"
_TOKEN_GEMINI_SUB: str = "FIXTURE_TOKEN_GEMINI_SUB"

_ALL_TOKENS: tuple[str, ...] = (
    _TOKEN_AGENTS_ROOT,
    _TOKEN_CLAUDE_ROOT,
    _TOKEN_GEMINI_ROOT,
    _TOKEN_AGENTS_SUB,
    _TOKEN_CLAUDE_SUB,
    _TOKEN_GEMINI_SUB,
)

_PROBE_ATTEMPTS: int = 3


class HarnessCheckError(Exception):
    """Raised when a harness check can't proceed (subprocess failure, not a
    missing binary). Missing binaries degrade to UNVERIFIABLE."""


def _vprint(msg: str, *, verbose: bool, file: TextIO | None = None) -> None:
    cli_common.vprint(msg, verbose=verbose, file=file)


def _qprint(msg: str, *, quiet: bool, file: TextIO | None = None) -> None:
    cli_common.qprint(msg, quiet=quiet, file=file)


# ── binary resolution ────────────────────────────────────────────────────────


def resolve_binary(name: str) -> Path | None:
    """Resolve a harness binary via ``shutil.which`` then ``Path.resolve()``.

    If ``which`` returns nothing, try the small fallback-path list for this
    harness (Linux/WSL only). Returns ``None`` if the binary cannot be found.
    """
    found = shutil.which(name)
    if found:
        return Path(found).resolve()
    for fallback in _FALLBACK_PATHS.get(name, []):
        p = Path(fallback)
        if p.is_file():
            return p.resolve()
    return None


# ── version extraction ───────────────────────────────────────────────────────


def _extract_version(name: str, first_line: str) -> str:
    """Extract a ``X.Y.Z`` version string from the first line of
    ``--version`` output.

    Each harness has its own format; this function knows the common ones.
    Returns the raw first line if no semver-like pattern is found — the
    caller treats a mismatch-class note as the degradation path.
    """
    line = first_line.strip()
    # claude: "2.1.252 (Claude Code)"
    # opencode: "1.18.25"
    # pi: "0.84.4"
    # copilot: "GitHub Copilot CLI 1.0.80."
    # agy: "1.1.22"
    m = re.search(r"(\d+(?:\.\d+)+)", line)
    return m.group(1) if m else line


def run_version(
    name: str,
    binary: Path,
    run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> str:
    """Run ``<binary> --version`` and return the extracted version string.

    Raise :class:`HarnessCheckError` on a nonzero exit or timeout.
    """
    try:
        result = run_command(
            [str(binary), "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise HarnessCheckError(f"{name}: --version failed: {exc}") from exc
    if result.returncode != 0:
        raise HarnessCheckError(
            f"{name}: --version exited {result.returncode}: {result.stderr.strip() or result.stdout.strip()}"
        )
    first = (result.stdout or "").splitlines()[0] if result.stdout else ""
    return _extract_version(name, first)


# ── check (hook tier) ────────────────────────────────────────────────────────


def _check_one(
    name: str,
    pinned: str,
    quiet: bool = False,
    verbose: bool = False,
    run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[str | None, bool]:
    """Check one harness. Return ``(note_or_none, is_error)``.

    ``note_or_none`` is a plain-language one-line note when the installed
    version differs from the pin, or ``None`` when they match (or the binary
    is missing). ``is_error`` is ``True`` only when ``--version`` itself
    failed (not a mismatch).
    """
    binary = resolve_binary(name)
    if binary is None:
        _vprint(f"{name}: binary not found — UNVERIFIABLE", verbose=verbose)
        return None, False
    try:
        installed = run_version(name, binary, run_command=run_command)
    except HarnessCheckError as exc:
        return f"[{name}] {exc}", True
    if installed == pinned:
        _vprint(f"{name}: {installed} matches pinned {pinned}", verbose=verbose)
        return None, False
    note = (
        f"[{name}] installed {installed} ≠ pinned {pinned} — "
        f"instruction-file discovery row is unverified. "
        f"Run `python3 ~/.claude/scripts/harness_discovery_check.py probe --harness {name}`"
    )
    return note, False


def cmd_check(
    *,
    hook: bool = False,
    strict: bool = False,
    quiet: bool = False,
    verbose: bool = False,
    run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> int:
    """Stateless version-pin comparison for the load-bearing harnesses.

    Silent when versions match. Prints a note on mismatch. Returns 0 for
    clean/noted, 1 for internal error, 2 for attention under ``--strict``.
    """
    notes: list[str] = []
    errors: list[str] = []
    for name in _LOAD_BEARING:
        pinned = {
            "claude": CLAUDE_CODE_PINNED_VERSION,
            "opencode": OPENCODE_PINNED_VERSION,
        }[name]
        note, is_error = _check_one(
            name, pinned, quiet=quiet, verbose=verbose, run_command=run_command
        )
        if is_error:
            errors.append(note or f"[{name}] internal error")
        elif note:
            notes.append(note)

    if errors:
        # In --hook mode, print a crash note so a broken checker is
        # distinguishable from a stale pin.
        crash = "[harness-discovery] checker failed — run it manually"
        if hook:
            print(crash)
        else:
            for e in errors:
                _qprint(e, quiet=quiet, file=sys.stderr)
            _qprint(crash, quiet=quiet, file=sys.stderr)
        return 1

    if notes:
        for note in notes:
            _qprint(note, quiet=quiet)
        return 2 if strict else 0

    return 0


# ── probe (live tier) ────────────────────────────────────────────────────────


def _build_fixture(
    repo: Path,
    run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    """Create the audit fixture in ``repo`` (already a git repo or plain
    directory). Writes the six token-bearing files and commits if git is
    present."""
    (repo / "AGENTS.md").write_text(f"{_TOKEN_AGENTS_ROOT}\n")
    (repo / "CLAUDE.md").write_text(f"{_TOKEN_CLAUDE_ROOT}\n")
    (repo / "GEMINI.md").write_text(f"{_TOKEN_GEMINI_ROOT}\n")

    sub = repo / "sub"
    sub.mkdir(exist_ok=True)
    (sub / "AGENTS.md").write_text(f"{_TOKEN_AGENTS_SUB}\n")
    (sub / "CLAUDE.md").write_text(f"{_TOKEN_CLAUDE_SUB}\n")
    (sub / "GEMINI.md").write_text(f"{_TOKEN_GEMINI_SUB}\n")

    # Initialise as a git repo so harnesses that use git root discovery
    # (e.g. Copilot) see a real repository.
    if not (repo / ".git").exists():
        run_command(["git", "init", "-q"], cwd=repo, check=False)
    run_command(["git", "add", "."], cwd=repo, check=False)
    run_command(["git", "commit", "-q", "-m", "init"], cwd=repo, check=False)


def _probe_prompt(tokens: Sequence[str]) -> str:
    """Return the prompt shape used for every harness probe.

    The model is instructed not to use tools and to list only the tokens it
    recognises from its loaded instructions/context.
    """
    token_list = ", ".join(tokens)
    return (
        "You are being probed for instruction-file loading behavior. "
        "Do NOT use any tools. Based ONLY on the instructions and context "
        "you have been loaded with, which of these tokens appear in your "
        f"context: {token_list}? "
        "List only the matching tokens, separated by commas. If none, say 'none'."
    )


def _parse_tokens(response: str) -> set[str]:
    """Extract fixture token names from a probe response.

    Cheap models wrap answers in prose/markdown, so we search for each
    known token name as a substring rather than expecting an exact match.
    """
    found: set[str] = set()
    for token in _ALL_TOKENS:
        if token in response:
            found.add(token)
    return found


def _harness_probe_command(name: str, prompt: str) -> list[str]:
    """Return the non-interactive invocation for ``name`` with ``prompt``.

    These invocations are reconstructed from the 2026-08-30 audit; they
    drive each harness in print/prompt mode with a cheap or free model
    where configurable.
    """
    if name == "claude":
        return ["claude", "-p", "--model", "haiku", prompt]
    if name == "opencode":
        # Free-tier model; override via OPENCODE_PROBE_MODEL env var.
        model = os.environ.get("OPENCODE_PROBE_MODEL", "opencode-go/glm-5.2")
        return ["opencode", "run", "-m", model, prompt]
    if name == "pi":
        return ["pi", "-p", "--no-tools", prompt]
    if name == "copilot":
        return ["copilot", "-p", prompt]
    if name == "agy":
        return ["agy", "-p", prompt]
    raise HarnessCheckError(f"unknown harness: {name}")


def _run_probe(
    name: str,
    cwd: Path,
    run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[set[str], str | None]:
    """Run one probe attempt for ``name`` from ``cwd``.

    Returns ``(tokens_found, error_or_none)``. ``error_or_none`` is set
    when the harness invocation itself fails (transport, auth, timeout),
    in which case ``tokens_found`` is empty.
    """
    binary = resolve_binary(name)
    if binary is None:
        return set(), f"{name}: binary not found"
    prompt = _probe_prompt(_ALL_TOKENS)
    cmd = _harness_probe_command(name, prompt)
    try:
        result = run_command(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            cwd=cwd,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return set(), f"{name}: probe invocation failed: {exc}"
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        detail = stderr or stdout or f"exit {result.returncode}"
        return set(), f"{name}: probe exited nonzero: {detail}"
    response = result.stdout or ""
    return _parse_tokens(response), None


def _probe_harness(
    name: str,
    expected_tokens: set[str],
    cwd: Path,
    run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[str, str | None]:
    """Probe a harness up to ``_PROBE_ATTEMPTS`` times.

    Returns ``(status, detail)`` where ``status`` is one of
    ``"HOLD"``, ``"BROKEN"``, ``"ERROR"``, and ``detail`` is a
    human-readable explanation (nonempty for BROKEN and ERROR).
    """
    for attempt in range(1, _PROBE_ATTEMPTS + 1):
        found, error = _run_probe(name, cwd, run_command=run_command)
        if error:
            _vprint(f"{name}: attempt {attempt} ERROR: {error}", verbose=True)
            if attempt == _PROBE_ATTEMPTS:
                return "ERROR", error
            continue
        # A completely empty token set is suspicious; retry when possible.
        # After all attempts, no tokens means we cannot measure (ERROR) unless
        # nothing was expected in the first place (agy → HOLD).
        if not found:
            if attempt < _PROBE_ATTEMPTS:
                _vprint(
                    f"{name}: attempt {attempt} found no tokens, retrying",
                    verbose=True,
                )
                continue
            if expected_tokens:
                return (
                    "ERROR",
                    f"{name}: no tokens extracted after {_PROBE_ATTEMPTS} attempts",
                )
            return "HOLD", None
        # Validate: every expected token must be present, and no unexpected
        # token may be present (within the fixture set).
        missing = expected_tokens - found
        extra = found - expected_tokens
        if missing or extra:
            parts: list[str] = []
            if missing:
                parts.append(f"missing {sorted(missing)}")
            if extra:
                parts.append(f"unexpected {sorted(extra)}")
            return "BROKEN", "; ".join(parts)
        return "HOLD", None
    # unreachable, but satisfies type checker
    return "ERROR", f"{name}: exhausted all attempts"


def _remediation(name: str, status: str, detail: str | None) -> str:
    """Return a suggested remediation for a BROKEN row."""
    if status != "BROKEN" or not detail:
        return ""
    return (
        f"  → {name} discovery behavior changed. Re-measure with the audit "
        f"fixture, update README.md's version pin and expectation table, "
        f"and adjust the convention if needed. Detail: {detail}"
    )


def cmd_probe(
    *,
    harness: str | None = None,
    quiet: bool = False,
    verbose: bool = False,
    run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> int:
    """On-demand live semantic verification.

    Rebuilds the audit fixture, probes the requested harnesses, and prints
    a per-row table. Exits nonzero if any row is BROKEN or ERROR.
    """
    targets: list[str] = (
        [harness]
        if harness
        else [
            "claude",
            "opencode",
            "pi",
            "copilot",
            "agy",
        ]
    )

    # Expected root tokens per harness (semantic fact, not version-specific).
    expected_root: dict[str, set[str]] = {
        "claude": {_TOKEN_CLAUDE_ROOT},
        "opencode": {_TOKEN_AGENTS_ROOT},
        "pi": {_TOKEN_AGENTS_ROOT},  # prefers AGENTS.md
        "copilot": {_TOKEN_CLAUDE_ROOT, _TOKEN_GEMINI_ROOT, _TOKEN_AGENTS_ROOT},
        "agy": set(),  # none at project level
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir) / "fixture"
        repo.mkdir()
        _build_fixture(repo, run_command=run_command)
        root_cwd = repo

        rows: list[tuple[str, str, str | None]] = []
        for name in targets:
            _vprint(f"probing {name} ...", verbose=verbose)
            status, detail = _probe_harness(
                name, expected_root[name], root_cwd, run_command=run_command
            )
            rows.append((name, status, detail))

    # Print table
    _qprint("Harness | Status | Detail", quiet=quiet)
    _qprint("-" * 50, quiet=quiet)
    any_bad = False
    for name, status, detail in rows:
        detail_str = detail or "-"
        _qprint(f"{name:8} | {status:6} | {detail_str}", quiet=quiet)
        if status in ("BROKEN", "ERROR"):
            any_bad = True
            rem = _remediation(name, status, detail)
            if rem:
                _qprint(rem, quiet=quiet)

    return 1 if any_bad else 0


# ── CLI ──────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Detect harness instruction-file discovery drift against "
        "README.md's version-pinned facts."
    )
    verbosity_parent = argparse.ArgumentParser(add_help=False)
    cli_common.add_verbosity_args(verbosity_parent)

    subparsers = parser.add_subparsers(dest="subcommand")

    check_parser = subparsers.add_parser(
        "check",
        help="stateless version-pin comparison for load-bearing harnesses (default)",
        parents=[verbosity_parent],
    )
    check_parser.add_argument(
        "--hook",
        action="store_true",
        help="format output for SessionStart hook consumption",
    )
    check_parser.add_argument(
        "--strict",
        action="store_true",
        help="exit 2 when a version mismatch is noted (default: exit 0)",
    )

    probe_parser = subparsers.add_parser(
        "probe",
        help="on-demand live semantic verification (~10-15 API calls)",
        parents=[verbosity_parent],
    )
    probe_parser.add_argument(
        "--harness",
        choices=["claude", "opencode", "pi", "copilot", "agy"],
        help="probe a single harness instead of all five",
    )

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    subcommand = args.subcommand or "check"
    quiet = getattr(args, "quiet", False)
    verbose = getattr(args, "verbose", False)

    if subcommand == "check":
        sys.exit(
            cmd_check(hook=args.hook, strict=args.strict, quiet=quiet, verbose=verbose)
        )
    else:
        sys.exit(cmd_probe(harness=args.harness, quiet=quiet, verbose=verbose))


if __name__ == "__main__":
    main()
