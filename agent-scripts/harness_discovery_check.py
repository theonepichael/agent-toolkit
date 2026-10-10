#!/usr/bin/env python3
"""SessionStart hook + CLI: detect when a harness's instruction-file discovery
behavior changes, without pinning harness versions in the repo.

Why this exists
---------------
The ``AGENTS.md`` + ``CLAUDE.md``-symlink convention rests on which
instruction filenames Claude Code and opencode actually load. These are
external binaries on independent update schedules; the day one changes its
discovery logic, instruction loading breaks silently — exactly how the
opencode ``~/.claude/CLAUDE.md`` fallback defect sat undetected for months.

The real verifier is ``probe``: it builds a throwaway fixture repo whose
``AGENTS.md`` / ``CLAUDE.md`` / ``GEMINI.md`` files (at the root and in a
subdirectory) each carry a fresh random token, drives the harness
non-interactively, and checks which tokens it reports. The prompt names no
token, so a model cannot echo one it never loaded.

Mechanism
---------
Each machine keeps its own record of what it has verified, one file per
load-bearing harness under ``$XDG_STATE_HOME/agent-toolkit/``
(``harness-discovery-<name>.json``, default ``~/.local/state``). A record
names the binary it was measured on (resolved path, mtime, size), its
version, the probe schema (a hash of the expectations, the fixture layout
and :data:`PROBE_LOGIC_VERSION`), and the result: HOLD, BROKEN or ERROR.

``check --hook`` runs at session start with zero API calls and no
subprocess on its fast path. It only stats each binary and reads its record:

* a matching HOLD record: silent;
* no matching record (new install, upgrade, replaced build, changed probe
  schema): it launches ``probe --record`` in the background, detached in
  its own session so the hook's ``timeout`` cannot kill it, and prints
  nothing;
* a matching BROKEN record: one line naming the harness and what changed;
* a matching ERROR record: a background retry once its retry time has
  passed (24h, then 72h, then 7 days, then no more automatic retries), and
  one line from the third consecutive error on, saying verification failed.

A per-harness kernel lock (``flock`` on ``harness-discovery-<name>.lock``),
taken by ``check`` and handed to the probe process, keeps two sessions from
probing the same harness at once; the kernel releases it however the probe
exits. The probe sets ``HARNESS_DISCOVERY_PROBE=1`` for the harnesses it
drives, and ``check`` does nothing when that is set, so a probed harness's
own SessionStart hook cannot start another probe.

A recording probe confirms a token mismatch with a second run on a fresh
fixture before recording BROKEN; one mismatch followed by a HOLD is recorded
as an inconsistent-result ERROR. It records nothing if the binary changed
while it ran.

opencode has no SessionStart hook of its own: its upgrades are probed from
the next Claude Code or Copilot session (both run this hook). A machine that
only runs opencode verifies it with ``probe --record --harness opencode``.

Subcommands
-----------
    check   stat each load-bearing harness and compare it against its
            record. With ``--hook`` (the SessionStart hook): launch a
            background probe for an unverified binary and print only BROKEN
            or repeated-ERROR notes; it always exits 0. Without ``--hook``:
            print every harness that is not installed or not verified, and
            never launch anything.
    probe   rebuild the fixture, drive each requested harness, and print a
            per-row table (HOLD / BROKEN / ERROR) with a remediation next to
            any BROKEN row. ``--record`` also writes each harness's result
            to its record (this is what ``check --hook`` launches; run it by
            hand to verify a harness immediately).

Usage
-----
    harness_discovery_check.py check [--hook] [--strict]
    harness_discovery_check.py probe [--harness NAME] [--record]

Exits
-----
    check: 0 = clean or noted (always 0 with ``--hook`` unless ``--strict``),
           2 = under ``--strict``, an installed harness has no current HOLD
    probe: 0 = all rows HOLD, 1 = any BROKEN or ERROR, or ``--record`` while
           another probe of the same harness is running

Flags
  --quiet, -q    suppress non-essential output
  --verbose, -v  emit extra diagnostic messages to stderr
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import TextIO

import cli_common
import harness_spec

# What this module does with toolkit data (checked by scripts/check_toolkit_paths.py).
TOOLKIT_DATA = "none"

# Set in the environment of every harness the probe drives; ``check`` exits
# immediately when it is set, so a probed harness's SessionStart hook cannot
# launch a nested probe.
GUARD_ENV: str = "HARNESS_DISCOVERY_PROBE"

# Bump whenever the probe's logic changes in a way that could change its
# verdict (prompt, parsing, fixture). It is part of probe_schema(), so every
# recorded HOLD is re-verified after the change.
PROBE_LOGIC_VERSION: int = 2

# ── semantic expectations — which filenames each harness loads ──────────────
# These encode the measured behavioral facts, not version-specific offsets.
# They are the probe's expectations; changing them changes probe_schema(),
# which invalidates every recorded HOLD.
CLAUDE_CODE_EXPECTED_FILENAMES: frozenset[str] = harness_spec.HARNESSES[
    "claude"
].expected_filenames
OPENCODE_EXPECTED_FILENAMES: frozenset[str] = harness_spec.HARNESSES[
    "opencode"
].expected_filenames
PI_PREFERRED_FILENAMES: frozenset[str] = harness_spec.HARNESSES["pi"].expected_filenames
PI_FALLBACK_FILENAMES: frozenset[str] = frozenset({"CLAUDE.md"})
COPILOT_EXPECTED_FILENAMES: frozenset[str] = harness_spec.HARNESSES[
    "copilot"
].expected_filenames
AGY_EXPECTED_FILENAMES: frozenset[str] = harness_spec.HARNESSES[
    "agy"
].expected_filenames
# Codex reads project AGENTS.md natively (global ~/.codex/AGENTS.md too);
# extra project names only via its project_doc_fallback_filenames config,
# which this toolkit doesn't set. Verified against the 0.153.4 docs.
CODEX_EXPECTED_FILENAMES: frozenset[str] = harness_spec.HARNESSES[
    "codex"
].expected_filenames

# ── harness metadata ─────────────────────────────────────────────────────────

DISCOVERY_TARGETS: tuple[str, ...] = harness_spec.DISCOVERY_TARGETS

# Fallback paths tried before reporting UNVERIFIABLE (Linux/WSL only).
_FALLBACK_PATHS: dict[str, list[str]] = {
    name: list(spec.fallback_paths) for name, spec in harness_spec.HARNESSES.items()
}

_LOAD_BEARING: tuple[str, ...] = harness_spec.LOAD_BEARING_NAMES

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

# Automatic retry delays after the 1st, 2nd and 3rd consecutive ERROR on the
# same binary; after the 4th there are no more automatic retries until the
# binary or probe schema changes, or the user runs ``probe --record``.
_RETRY_DELAYS: tuple[int, ...] = (24 * 3600, 72 * 3600, 7 * 24 * 3600)
_MAX_AUTO_ERRORS: int = len(_RETRY_DELAYS) + 1
# From this many consecutive errors on, session start prints a note.
_NOTE_AFTER_ERRORS: int = 3

_STATE_DIRNAME: str = "agent-toolkit"
_DETAIL_MAX: int = 300


class HarnessCheckError(Exception):
    """Raised when a harness check can't proceed (subprocess failure, not a
    missing binary)."""


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
        try:
            p = Path(fallback).expanduser()
        except RuntimeError:
            continue
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
    # opencode: "1.18.32", or a dev build "0.0.0-dev-202609250015" (the
    #   prerelease suffix is kept: truncating it misnames the binary)
    # pi: "0.84.4"
    # copilot: "GitHub Copilot CLI 1.0.80."
    # agy: "1.1.22"
    m = re.search(r"(\d+(?:\.\d+)+(?:-[0-9A-Za-z.-]*[0-9A-Za-z])?)", line)
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


# ── binary identity ──────────────────────────────────────────────────────────


def _stat_key(binary: Path) -> tuple[str, int, int] | None:
    """Return the ``(resolved_path, mtime_ns, size)`` identity of
    ``binary``, or ``None`` when it cannot be stat'ed (uncacheable)."""
    try:
        st = binary.stat()
    except OSError:
        return None
    return (str(binary), st.st_mtime_ns, st.st_size)


# ── per-machine records ───────────────────────────────────────────────────────


def _state_root() -> Path:
    return cli_common.state_dir() / _STATE_DIRNAME


def _record_path(name: str) -> Path:
    """The record file for ``name``: one file per harness, so two harnesses
    recording at once never overwrite each other."""
    return _state_root() / f"harness-discovery-{name}.json"


def _lock_path(name: str) -> Path:
    return _state_root() / f"harness-discovery-{name}.lock"


def probe_schema() -> str:
    """Hash of everything a recorded verdict depends on besides the binary:
    each harness's expected tokens, the fixture's token labels and layout,
    and :data:`PROBE_LOGIC_VERSION`. A record with another schema is stale."""
    payload = {
        "logic": PROBE_LOGIC_VERSION,
        "labels": list(_ALL_TOKENS),
        "layout": [path for path, _label in _FIXTURE_FILES],
        "expected": {
            name: sorted(spec.probe_expected_root)
            for name, spec in sorted(harness_spec.HARNESSES.items())
        },
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode())
    return digest.hexdigest()[:16]


def _identity_dict(identity: tuple[str, int, int]) -> dict[str, object]:
    path, mtime_ns, size = identity
    return {"path": path, "mtime_ns": mtime_ns, "size": size}


def _format_identity(identity: tuple[str, int, int]) -> str:
    """``path:mtime_ns:size`` — the ``--expect-identity`` argument form."""
    path, mtime_ns, size = identity
    return f"{path}:{mtime_ns}:{size}"


def _parse_identity(text: str) -> tuple[str, int, int] | None:
    path, sep1, rest = text.rpartition(":")
    head, sep2, mtime = path.rpartition(":")
    if not (sep1 and sep2):
        return None
    try:
        return (head, int(mtime), int(rest))
    except ValueError:
        return None


def read_record(name: str) -> dict[str, object] | None:
    """The record for ``name``, or ``None`` when absent, unreadable or not a
    JSON object (a corrupt record is treated as missing and re-probed)."""
    try:
        data = json.loads(_record_path(name).read_text())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_record(name: str, record: dict[str, object]) -> bool:
    """Atomically replace ``name``'s record (temp file + rename). Return
    ``False`` instead of raising when the state dir is not writable."""
    path = _record_path(name)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=path.parent, prefix=f"{path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(record, handle)
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
    except OSError:
        return False
    return True


def _record_matches(
    record: dict[str, object] | None, identity: tuple[str, int, int]
) -> bool:
    return (
        record is not None
        and record.get("identity") == _identity_dict(identity)
        and record.get("probe_schema") == probe_schema()
    )


def _truncate(detail: str | None) -> str | None:
    if detail is None:
        return None
    detail = " ".join(detail.split())
    return detail if len(detail) <= _DETAIL_MAX else detail[: _DETAIL_MAX - 1] + "…"


def _build_record(
    name: str,
    identity: tuple[str, int, int],
    version: str | None,
    status: str,
    detail: str | None,
    now: float,
) -> dict[str, object]:
    """A new record for ``name``. ERROR counts on from a previous matching
    ERROR record and schedules the next automatic retry; HOLD and BROKEN
    reset the count."""
    error_count = 0
    next_retry_at: float | None = None
    if status == "ERROR":
        previous = read_record(name)
        prior = 0
        if _record_matches(previous, identity) and previous is not None:
            if previous.get("status") == "ERROR" and isinstance(
                previous.get("error_count"), int
            ):
                prior = int(previous["error_count"])  # type: ignore[arg-type]
            if version is None and isinstance(previous.get("version"), str):
                version = str(previous["version"])
        error_count = prior + 1
        if error_count <= len(_RETRY_DELAYS):
            next_retry_at = now + _RETRY_DELAYS[error_count - 1]
    return {
        "identity": _identity_dict(identity),
        "version": version,
        "probe_schema": probe_schema(),
        "status": status,
        "detail": _truncate(detail),
        "checked_at": now,
        "error_count": error_count,
        "next_retry_at": next_retry_at,
    }


# ── check (session-start tier) ───────────────────────────────────────────────

_PROBE_HINT = "python3 ~/.agent-toolkit/scripts/harness_discovery_check.py probe"


def _launch_probe(
    name: str,
    identity: tuple[str, int, int],
    popen: Callable[..., object] = subprocess.Popen,
) -> str | None:
    """Launch ``probe --record`` for ``name`` in the background.

    Takes the harness's lock with a non-blocking ``flock`` and hands the
    locked descriptor to the child (``pass_fds``), so the lock stays held
    without a gap until the probe process exits, however it exits. Returns
    ``None`` when the probe was launched or another probe already holds the
    lock, or a short reason when it could not be launched.
    """
    lock_path = _lock_path(name)
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        return f"lock file not openable: {exc}"
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return None  # a probe of this harness is already running
        except OSError as exc:
            return f"lock not acquirable: {exc}"
        argv = [
            sys.executable,
            str(Path(__file__).resolve()),
            "probe",
            "--record",
            "--harness",
            name,
            "--expect-identity",
            _format_identity(identity),
            "--lock-fd",
            str(fd),
        ]
        try:
            popen(
                argv,
                start_new_session=True,
                close_fds=True,
                pass_fds=(fd,),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            return f"probe launch failed: {exc}"
        return None
    finally:
        os.close(fd)


def _error_note(name: str, record: dict[str, object]) -> str:
    version = record.get("version") or "?"
    count = record.get("error_count")
    detail = record.get("detail") or "no detail"
    return (
        f"[harness-discovery] could not verify {name} {version} "
        f"({count} attempts: {detail}) — run `{_PROBE_HINT} --record --harness {name}`"
    )


def _broken_note(name: str, record: dict[str, object]) -> str:
    version = record.get("version") or "?"
    detail = record.get("detail") or "no detail"
    return (
        f"[harness-discovery] {name} {version} instruction-file discovery changed: "
        f"{detail}. Confirm with `{_PROBE_HINT} --harness {name}`, then update "
        f"harness_spec.py's expectations and the AGENTS.md/CLAUDE.md convention."
    )


def _hook_check_one(
    name: str,
    now: float,
    launch: Callable[[str, tuple[str, int, int]], str | None],
) -> tuple[str | None, bool]:
    """Check one harness in hook mode. Return ``(note_or_none, verified)``.

    ``verified`` is True when the binary is absent (nothing to verify) or has
    a current HOLD record; ``--strict`` keys off it.
    """
    binary = resolve_binary(name)
    if binary is None:
        return None, True
    identity = _stat_key(binary)
    if identity is None:
        return None, True
    record = read_record(name)
    if _record_matches(record, identity) and record is not None:
        status = record.get("status")
        if status == "HOLD":
            return None, True
        if status == "BROKEN":
            return _broken_note(name, record), False
        count = record.get("error_count")
        count = count if isinstance(count, int) else 0
        retry_at = record.get("next_retry_at")
        due = isinstance(retry_at, (int, float)) and now >= retry_at
        note = _error_note(name, record) if count >= _NOTE_AFTER_ERRORS else None
        if not due or count >= _MAX_AUTO_ERRORS:
            return note, False
        failure = launch(name, identity)
        return (note or _launch_failure(name, identity, failure, now)), False
    failure = launch(name, identity)
    return _launch_failure(name, identity, failure, now), False


def _launch_failure(
    name: str,
    identity: tuple[str, int, int],
    failure: str | None,
    now: float,
) -> str | None:
    """Turn a failed launch into an ERROR record, or a one-line note when
    even that cannot be written."""
    if failure is None:
        return None
    record = _build_record(name, identity, None, "ERROR", failure, now)
    if write_record(name, record):
        return None
    return f"[harness-discovery] cannot verify {name}: {failure}"


def cmd_check(
    *,
    hook: bool = False,
    strict: bool = False,
    quiet: bool = False,
    verbose: bool = False,
    now: float | None = None,
    launch: Callable[[str, tuple[str, int, int]], str | None] | None = None,
) -> int:
    """Compare each load-bearing harness's binary against its record.

    Hook mode launches background probes and prints only BROKEN and
    repeated-ERROR notes; manual mode prints every unverified or missing
    harness and launches nothing. Returns 0, or 2 under ``--strict`` when an
    installed harness has no current HOLD record.
    """
    if os.environ.get(GUARD_ENV) == "1":
        return 0
    now = time.time() if now is None else now
    launch = _launch_probe if launch is None else launch
    notes: list[str] = []
    all_verified = True
    for name in _LOAD_BEARING:
        if hook:
            note, verified = _hook_check_one(name, now, launch)
        else:
            note, verified = _manual_check_one(name)
        all_verified = all_verified and verified
        if note:
            notes.append(note)
    for note in notes:
        _qprint(note, quiet=quiet)
    _vprint(f"check: {len(notes)} note(s)", verbose=verbose)
    return 2 if strict and not all_verified else 0


def _manual_check_one(name: str) -> tuple[str | None, bool]:
    binary = resolve_binary(name)
    if binary is None:
        return f"[{name}] not installed", True
    identity = _stat_key(binary)
    if identity is None:
        return f"[{name}] {binary} cannot be stat'ed", True
    record = read_record(name)
    if not (_record_matches(record, identity) and record is not None):
        return (
            f"[{name}] not verified on this machine yet — "
            f"run `{_PROBE_HINT} --record --harness {name}`"
        ), False
    status = record.get("status")
    if status == "HOLD":
        return None, True
    if status == "BROKEN":
        return _broken_note(name, record), False
    return _error_note(name, record), False


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
    if name == "codex":
        # `codex exec` is the verified non-interactive mode (positional
        # PROMPT). Model override via env var, mirroring opencode's
        # pattern -- unset means codex's own configured default model.
        model = os.environ.get("CODEX_PROBE_MODEL")
        cmd = ["codex", "exec"]
        if model:
            cmd += ["-m", model]
        cmd.append(prompt)
        return cmd
    raise HarnessCheckError(f"unknown harness: {name}")


# ── probe (live tier) ────────────────────────────────────────────────────────

# (relative path, token label) for each fixture file.
_FIXTURE_FILES: tuple[tuple[str, str], ...] = (
    ("AGENTS.md", _TOKEN_AGENTS_ROOT),
    ("CLAUDE.md", _TOKEN_CLAUDE_ROOT),
    ("GEMINI.md", _TOKEN_GEMINI_ROOT),
    ("sub/AGENTS.md", _TOKEN_AGENTS_SUB),
    ("sub/CLAUDE.md", _TOKEN_CLAUDE_SUB),
    ("sub/GEMINI.md", _TOKEN_GEMINI_SUB),
)

_MARKER_PREFIX: str = "MARKER-"


def _new_tokens() -> dict[str, str]:
    """A fresh random token per fixture label. Random and never named in the
    prompt, so a model cannot report a token it did not load."""
    return {
        label: f"{_MARKER_PREFIX}{secrets.token_hex(8).upper()}"
        for label in _ALL_TOKENS
    }


def _build_fixture(
    repo: Path,
    tokens: dict[str, str],
    run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> None:
    """Create the audit fixture in ``repo``: six instruction files, each
    holding its label's random token, committed to a git repo so harnesses
    that use git root discovery (e.g. Copilot) see a real repository."""
    for rel, label in _FIXTURE_FILES:
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{tokens[label]}\n")
    if not (repo / ".git").exists():
        run_command(["git", "init", "-q"], cwd=repo, check=False)
    run_command(["git", "add", "."], cwd=repo, check=False)
    run_command(["git", "commit", "-q", "-m", "init"], cwd=repo, check=False)


def _probe_prompt() -> str:
    """The prompt for every harness probe. It names the marker prefix but
    no token, so only a loaded file can supply one."""
    return (
        "You are being probed for instruction-file loading behavior. "
        "Do NOT use any tools. Your loaded instructions or context may contain "
        f"marker lines starting with {_MARKER_PREFIX!r}. Repeat every such marker "
        "exactly as written, separated by commas. If you were given none, say 'none'."
    )


def _parse_tokens(response: str, tokens: dict[str, str]) -> set[str]:
    """Map the random tokens found in ``response`` back to their labels.

    Cheap models wrap answers in prose/markdown, so each token is searched
    as a substring rather than expecting an exact match."""
    return {label for label, value in tokens.items() if value in response}


def _run_probe(
    name: str,
    cwd: Path,
    tokens: dict[str, str],
    run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[set[str], str | None]:
    """Run one probe attempt for ``name`` from ``cwd``.

    Returns ``(labels_found, error_or_none)``. ``error_or_none`` is set
    when the harness invocation itself fails (transport, auth, timeout),
    in which case ``labels_found`` is empty.
    """
    binary = resolve_binary(name)
    if binary is None:
        return set(), f"{name}: binary not found"
    # Run the resolved file, not the bare command name: the probe must
    # exercise the same binary whose identity is recorded — including a
    # fallback-path install that is not on PATH.
    cmd = [str(binary), *_harness_probe_command(name, _probe_prompt())[1:]]
    # `cwd=` alone chdir()s the child but leaves the inherited `PWD` env var
    # stale at the caller's own directory. opencode resolves its project
    # root from `$PWD`, not the real working directory, so a stale PWD here
    # makes it silently probe the caller's own repo instead of the fixture.
    env = dict(os.environ)
    env["PWD"] = str(cwd)
    env[GUARD_ENV] = "1"
    try:
        result = run_command(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            cwd=cwd,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return set(), f"{name}: probe invocation failed: {exc}"
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        detail = stderr or stdout or f"exit {result.returncode}"
        return set(), f"{name}: probe exited nonzero: {detail}"
    return _parse_tokens(result.stdout or "", tokens), None


def _probe_harness(
    name: str,
    expected_tokens: set[str],
    cwd: Path,
    tokens: dict[str, str],
    run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[str, str | None]:
    """Probe a harness up to ``_PROBE_ATTEMPTS`` times against one fixture.

    Returns ``(status, detail)`` where ``status`` is one of
    ``"HOLD"``, ``"BROKEN"``, ``"ERROR"``, and ``detail`` is a
    human-readable explanation (nonempty for BROKEN and ERROR).
    """
    for attempt in range(1, _PROBE_ATTEMPTS + 1):
        found, error = _run_probe(name, cwd, tokens, run_command=run_command)
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
                    f"{name}: attempt {attempt} found no tokens, retrying", verbose=True
                )
                continue
            if expected_tokens:
                return (
                    "ERROR",
                    f"{name}: no tokens extracted after {_PROBE_ATTEMPTS} attempts",
                )
            return "HOLD", None
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
    return "ERROR", f"{name}: exhausted all attempts"


def _probe_fresh(
    name: str,
    expected_tokens: set[str],
    run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[str, str | None]:
    """Probe ``name`` once against a newly built fixture with new tokens."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir) / "fixture"
        repo.mkdir()
        tokens = _new_tokens()
        _build_fixture(repo, tokens, run_command=run_command)
        return _probe_harness(
            name, expected_tokens, repo, tokens, run_command=run_command
        )


def _remediation(name: str, status: str, detail: str | None) -> str:
    """Return a suggested remediation for a BROKEN row."""
    if status != "BROKEN" or not detail:
        return ""
    return (
        f"  → {name} discovery behavior changed. Re-run the probe to confirm, "
        f"then update the expectations in harness_spec.py and adjust the "
        f"AGENTS.md/CLAUDE.md convention if needed. Detail: {detail}"
    )


def _acquire_own_lock(name: str) -> int | None:
    """Take ``name``'s probe lock for a manual ``probe --record``. Return the
    descriptor, or ``None`` when another probe holds it."""
    path = _lock_path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def _record_probe(
    name: str,
    expected: set[str],
    expect_identity: tuple[str, int, int] | None,
    now: float,
    run_command: Callable[..., subprocess.CompletedProcess[str]],
) -> tuple[str, str | None]:
    """Probe ``name`` with confirmation, and record the verdict."""
    binary = resolve_binary(name)
    if binary is None:
        return "ERROR", f"{name}: binary not found"
    identity = _stat_key(binary)
    if identity is None:
        return "ERROR", f"{name}: {binary} cannot be stat'ed"
    if expect_identity is not None and identity != expect_identity:
        return "ERROR", f"{name}: binary changed since the probe was launched"
    try:
        version: str | None = run_version(name, binary, run_command=run_command)
    except HarnessCheckError as exc:
        version = None
        status, detail = "ERROR", str(exc)
    else:
        status, detail = _probe_fresh(name, expected, run_command=run_command)
        if status == "BROKEN":
            # Confirm on an independent fixture before recording BROKEN.
            second, second_detail = _probe_fresh(
                name, expected, run_command=run_command
            )
            if second == "HOLD":
                status, detail = (
                    "ERROR",
                    f"{name}: inconsistent result (BROKEN then HOLD): {detail}",
                )
            elif second == "ERROR":
                status, detail = "ERROR", second_detail
    if _stat_key(binary) != identity:
        _vprint(f"{name}: binary changed during the probe; not recorded", verbose=True)
        return status, detail
    write_record(name, _build_record(name, identity, version, status, detail, now))
    return status, detail


def cmd_probe(
    *,
    harness: str | None = None,
    record: bool = False,
    expect_identity: str | None = None,
    lock_fd: int | None = None,
    quiet: bool = False,
    verbose: bool = False,
    run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    now: float | None = None,
) -> int:
    """Live semantic verification.

    Probes the requested harnesses against a fresh random-token fixture and
    prints a per-row table. With ``record``, confirms any mismatch on a
    second fixture and writes each harness's verdict to its record. Exits
    nonzero if any row is BROKEN or ERROR.
    """
    targets: list[str] = [harness] if harness else list(DISCOVERY_TARGETS)
    now = time.time() if now is None else now
    expected_root: dict[str, set[str]] = {
        name: set(spec.probe_expected_root)
        for name, spec in harness_spec.HARNESSES.items()
    }
    wanted_identity = _parse_identity(expect_identity) if expect_identity else None

    rows: list[tuple[str, str, str | None]] = []
    for name in targets:
        _vprint(f"probing {name} ...", verbose=verbose)
        if not record:
            status, detail = _probe_fresh(
                name, expected_root[name], run_command=run_command
            )
            rows.append((name, status, detail))
            continue
        own_fd: int | None = None
        if lock_fd is None:
            try:
                own_fd = _acquire_own_lock(name)
            except OSError as exc:
                rows.append((name, "ERROR", f"{name}: lock not acquirable: {exc}"))
                continue
            if own_fd is None:
                rows.append((name, "ERROR", f"{name}: a probe is already running"))
                continue
        try:
            status, detail = _record_probe(
                name, expected_root[name], wanted_identity, now, run_command
            )
        finally:
            if own_fd is not None:
                os.close(own_fd)
        rows.append((name, status, detail))

    _qprint("Harness | Status | Detail", quiet=quiet)
    _qprint("-" * 50, quiet=quiet)
    any_bad = False
    for name, status, detail in rows:
        _qprint(f"{name:8} | {status:6} | {detail or '-'}", quiet=quiet)
        if status in ("BROKEN", "ERROR"):
            any_bad = True
            rem = _remediation(name, status, detail)
            if rem:
                _qprint(rem, quiet=quiet)
    return 1 if any_bad else 0


# ── CLI ──────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Detect harness instruction-file discovery changes by "
        "probing each installed harness once per binary."
    )
    verbosity_parent = argparse.ArgumentParser(add_help=False)
    cli_common.add_verbosity_args(verbosity_parent)

    subparsers = parser.add_subparsers(dest="subcommand")

    check_parser = subparsers.add_parser(
        "check",
        help="compare installed harnesses against this machine's probe records (default)",
        parents=[verbosity_parent],
    )
    check_parser.add_argument(
        "--hook",
        action="store_true",
        help="SessionStart mode: launch background probes, print only problems, exit 0",
    )
    check_parser.add_argument(
        "--strict",
        action="store_true",
        help="exit 2 when an installed harness has no current HOLD record",
    )

    probe_parser = subparsers.add_parser(
        "probe",
        help="live semantic verification (~10-15 cheap API calls)",
        parents=[verbosity_parent],
    )
    probe_parser.add_argument(
        "--harness",
        choices=["claude", "opencode", "pi", "copilot", "agy", "codex"],
        help="probe a single harness instead of all supported ones",
    )
    probe_parser.add_argument(
        "--record",
        action="store_true",
        help="confirm any mismatch and write each verdict to this machine's record",
    )
    probe_parser.add_argument(
        "--expect-identity",
        help="(used by check --hook) path:mtime_ns:size the probe was launched for",
    )
    probe_parser.add_argument(
        "--lock-fd",
        type=int,
        help="(used by check --hook) inherited descriptor holding the probe lock",
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
            cmd_check(
                hook=getattr(args, "hook", False),
                strict=getattr(args, "strict", False),
                quiet=quiet,
                verbose=verbose,
            )
        )
    sys.exit(
        cmd_probe(
            harness=args.harness,
            record=args.record,
            expect_identity=args.expect_identity,
            lock_fd=args.lock_fd,
            quiet=quiet,
            verbose=verbose,
        )
    )


if __name__ == "__main__":
    main()
