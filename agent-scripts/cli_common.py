"""Shared CLI helpers used across agent-toolkit scripts.

Environment
  AGENT_TOOLKIT_TIMING=1 enables timing_span records under
  $XDG_STATE_HOME/agent-toolkit/timing.jsonl (default ~/.local/state).
  No prompts or argv are recorded. Logging failure never changes exit behavior.

Flags
  --quiet, -q    suppress non-essential stdout output
  --verbose, -v  emit extra diagnostic messages to stderr
"""

import argparse
import json
import logging
import os
import re
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, TextIO

# What this module does with toolkit data (checked by scripts/check_toolkit_paths.py).
TOOLKIT_DATA = "infrastructure"

_SUPPRESSED_LEVEL = logging.CRITICAL + 1
_MODULE_LOGGER_NAME = "cli_common"


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


class Palette:
    """ANSI colorizer that no-ops when color isn't appropriate.

    Raw escape codes rather than a third-party library: this runs on
    machines that may not have been provisioned yet, so only the standard
    library is depended on.
    """

    RESET = "\x1b[0m"

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def _wrap(self, text: str, code: str) -> str:
        return f"{code}{text}{self.RESET}" if self.enabled else text

    def header(self, text: str) -> str:
        """Section header (``==>`` lines) and the summary banner."""
        return self._wrap(text, "\x1b[1;36m")

    def ok(self, text: str) -> str:
        """A mutation that succeeded."""
        return self._wrap(text, "\x1b[32m")

    def warn(self, text: str) -> str:
        """A skipped step or a drift report — not fatal, but read it."""
        return self._wrap(text, "\x1b[33m")

    def error(self, text: str) -> str:
        """A hard error (argument errors, blocked run)."""
        return self._wrap(text, "\x1b[31m")

    def dim(self, text: str) -> str:
        """Dry-run previews and other informational asides."""
        return self._wrap(text, "\x1b[2m")


def color_enabled(stream: object) -> bool:
    """Return whether ANSI codes should be emitted to ``stream``.

    Honors the ``NO_COLOR`` convention (any non-empty value disables color)
    and ``TERM=dumb``, and otherwise only colorizes an interactive terminal
    so piped/redirected output stays clean for grep.
    """
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    isatty = getattr(stream, "isatty", None)
    return bool(isatty and isatty())


# Color state is set once at entrypoint startup (install.py's main()) by
# mutating ``enabled`` on this single canonical object — never by rebinding
# the name — so every importer observes the same state regardless of import
# style. Default-off so any import-time or test-time use is plain text.
PALETTE = Palette(False)


def preview(message: str, *, quiet: bool = False) -> None:
    """Print a dry-run preview line."""
    qprint(PALETTE.dim(f"  [dry-run] {message}"), quiet=quiet)


def get_logger(
    name: str, *, verbose: bool = False, quiet: bool = False
) -> logging.Logger:
    """Return a stderr-only diagnostic logger, a structured complement to vprint.

    Attaches only an in-memory StreamHandler(sys.stderr) — never a file
    handler, never stdout, and no parameter can select either. Performs no
    I/O at call time (no directory creation, nothing opened), so it is safe
    on every invocation of a latency-sensitive script.

    Idempotent but not static: a repeat call with the same name reuses the
    existing handler (no duplicate handlers/log lines) but still resets the
    level, so later calls with different verbose/quiet flags take effect.
    Level gating matches vprint/qprint: DEBUG if verbose, fully suppressed
    if quiet, WARNING otherwise. propagate=False keeps records from leaking
    onto a root-logger handler configured elsewhere in the process.

    Raises ValueError on an empty name — guards against attaching a handler
    to the root logger by accident.
    """
    if not name:
        raise ValueError("logger name must be a non-empty string")
    logger = logging.getLogger(name)
    handler = None
    for existing in logger.handlers:
        if (
            isinstance(existing, logging.StreamHandler)
            and existing.stream is sys.stderr
        ):
            handler = existing
            break
    if handler is None:
        handler = logging.StreamHandler(sys.stderr)
        formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
        formatter.converter = time.gmtime
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    if quiet:
        logger.setLevel(_SUPPRESSED_LEVEL)
    elif verbose:
        logger.setLevel(logging.DEBUG)
    else:
        logger.setLevel(logging.WARNING)
    logger.propagate = False
    return logger


class JsonlWriteError(Exception):
    """Raised by append_jsonl(..., on_error="raise") when the write fails.

    Not an OSError subclass: it also wraps serialisation failures (a record
    json.dumps cannot encode is never an OSError). The original error is the
    __cause__; ``path`` names the log file the write targeted.
    """

    path: Path


OnError = Literal["log", "silent", "raise"]


def append_jsonl(
    path: Path,
    record: dict[str, object],
    *,
    on_error: OnError = "log",
    mode: int = 0o666,
    max_bytes: int | None = None,
) -> None:
    """Append one JSON record to ``path`` as a single JSONL line, opt-in failure reporting.

    ``max_bytes`` opt-in size cap for telemetry logs that grow without bound.
    When set and ``path`` already exists and exceeds it, ``path`` is renamed to
    ``<stem><suffix>.1`` (e.g. ``timing.jsonl`` → ``timing.jsonl.1``) via
    ``os.replace`` — keeping exactly one generation — before the new line is
    written to a fresh ``path``. The check is ``>`` (strict), so a file that
    sits exactly at the cap is not rotated. ``max_bytes`` is off by default
    (``None``); data files that must stay single-generation (journal.jsonl,
    runs.jsonl) never pass it. A race between two appenders can at most drop
    the other's single line into the rotated file, which is acceptable for
    telemetry; the rotation is best-effort and never raises — if the rename
    fails, the append proceeds as today (no rotation).

    The default ``on_error="log"`` preserves today's best-effort contract: a
    write failure (serialisation, bad parent, permissions, disk full, a short
    or failed write, a close error) is swallowed and logged as one line on
    stderr (always: the logger is forced to DEBUG level, whatever the
    caller's verbosity), and the function never raises to the caller. ``on_error="silent"`` swallows the same failures with no output at
    all. ``on_error="raise"`` raises JsonlWriteError naming ``path`` and
    chaining the original error, so a caller that must honour a refusal (e.g. a
    locked write) can. No caller in this repo is switched to ``raise`` by this
    module; the choice is the caller's.

    The record is serialised to bytes BEFORE anything is opened, so a
    serialisation failure leaves no partial line behind. The line is written
    with ONE os.write on a descriptor opened O_WRONLY | O_CREAT | O_APPEND with
    the given ``mode`` (applied only when the file is created, masked by the
    umask as usual — the default 0o666 reproduces today's creation permissions).
    A short write (fewer bytes written than the line length) is itself a
    failure: os.write does not raise for it, so the function builds its own
    OSError and treats it like any other failure. If the write fails and the
    close then also fails, the close error is suppressed so the original write
    error is what surfaces; a close failure after a successful write is itself
    the failure. The descriptor is always closed.

    Concurrency: one unbuffered write under O_APPEND keeps appends to a regular
    file from interleaving on the local filesystem; the concurrency test covers
    the property on the tested filesystem but it is not claimed as an
    unconditional cross-platform guarantee. A short write may already have
    appended a partial line before the error is reported — raising callers must
    not retry the append automatically, since a retry would concatenate onto
    the fragment.
    """
    try:
        line_bytes = (json.dumps(record) + "\n").encode()
    except Exception as exc:
        _report_jsonl_failure(exc, path, on_error)
        return
    # Size-capped rotation (opt-in, telemetry only): if the existing file
    # already exceeds max_bytes, rename it to its .1 sibling (one generation)
    # before the new line is written. The whole check runs inside one try so a
    # concurrent rotation can't break the never-raises contract: a second
    # writer may have already renamed the file away, so stat() raises
    # FileNotFoundError, which is harmless — the append below just creates a
    # fresh file. Any other OSError is logged at debug and skipped; rotation is
    # best-effort and never honours on_error (so on_error="raise" cannot be
    # triggered by a rotation failure), and the append always proceeds.
    if max_bytes is not None:
        try:
            if path.stat().st_size > max_bytes:
                os.replace(path, path.with_suffix(path.suffix + ".1"))
        except FileNotFoundError:
            pass  # rotated away by a concurrent writer; fresh file created below
        except OSError as exc:  # rotation hiccup: never block the append
            get_logger(_MODULE_LOGGER_NAME, verbose=True).debug(
                "append_jsonl rotation skipped for %s: %s", path, exc
            )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, mode)
    except Exception as exc:
        _report_jsonl_failure(exc, path, on_error)
        return
    write_err: Exception | None = None
    try:
        written = os.write(fd, line_bytes)
        if written < len(line_bytes):
            write_err = OSError(
                f"short write: wrote {written} of {len(line_bytes)} bytes"
            )
    except Exception as exc:  # os.write failure
        write_err = exc
    # Explicit close: a plain ``try/finally: os.close(fd)`` would let a close
    # error mask a pending write error, so close independently and only treat a
    # close failure as the failure when the write already succeeded.
    try:
        os.close(fd)
    except Exception as close_exc:
        if write_err is not None:
            # Write already failed: report it, not the close error that
            # followed it.
            _report_jsonl_failure(write_err, path, on_error)
            return
        _report_jsonl_failure(close_exc, path, on_error)
        return
    if write_err is not None:
        _report_jsonl_failure(write_err, path, on_error)


def _report_jsonl_failure(exc: Exception, path: Path, on_error: OnError) -> None:
    """Surface (or swallow) an append_jsonl failure per ``on_error``."""
    if on_error == "raise":
        err = JsonlWriteError(f"append_jsonl failed for {path}: {exc}")
        err.path = path
        raise err from exc
    if on_error == "silent":
        return
    get_logger(_MODULE_LOGGER_NAME, verbose=True).debug(
        "append_jsonl failed for %s: %s", path, exc
    )


# Named, high-confidence, structural secret patterns only. Deliberately no
# generic length/entropy-based catch-all: a blunt "any 20+ char
# alnum/base64-looking run" would mask git SHAs, UUIDs, branch names and
# ordinary identifiers — exactly what audit-log target fields are made of.
_REDACT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\b(?:ghp|gho)_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?:\b|(?<=_))(?i:key|token|password)=[^\s&]+"),
)


def redact_secrets(text: str, *, max_length: int = 200) -> str:
    """Mask secret-shaped substrings, then truncate to max_length.

    Clamps the input to max_length + 200 first, bounding the regex pass's
    input size on a hot path. Truncation alone is never a sanitization step:
    redaction always runs before the final truncation, so a secret
    straddling the truncation boundary is masked, not partially leaked.
    """
    clamped = text[: max_length + 200]
    for pattern in _REDACT_PATTERNS:
        clamped = pattern.sub("[REDACTED]", clamped)
    return clamped[:max_length]


def state_dir() -> Path:
    """The XDG state base directory: $XDG_STATE_HOME, default ~/.local/state.

    A relative $XDG_STATE_HOME is ignored (the XDG spec requires absolute
    paths). Shared by every writer under the installer state directory, which
    the toolkit-home migration never moves.
    """
    state = os.environ.get("XDG_STATE_HOME")
    return (
        Path(state)
        if state and Path(state).is_absolute()
        else Path.home() / ".local/state"
    )


def timing_log_path() -> Path:
    """The timing log path: $XDG_STATE_HOME/agent-toolkit/timing.jsonl.

    Mirrors the rule timing_span has always used, extracted so the timing
    writer can resolve it inside its own exception boundary — path resolution
    can fail before append_jsonl is entered, and that failure must stay silent
    (never raise, never change exit behaviour), exactly like a write failure.

    Size-capped rotation (phase A): when this file exceeds 20 MiB it is renamed
    to timing.jsonl.1, keeping one generation. Anyone mining the log by hand
    should read both files; the .1 holds the prior generation only.
    """
    return state_dir() / "agent-toolkit/timing.jsonl"


def _append_timing_record(record: dict[str, object]) -> None:
    """Append one timing record under the migration scope; never raises.

    The migration appends installer history in this directory, so timing
    writes take the shared migration scope too. A refusal skips the line and
    is recorded as an observation; timing never changes a command's result.
    """
    import migration_lock  # lazy: migration_lock itself imports this module

    try:
        with migration_lock.shared("timing-log", quiet=True):
            append_jsonl(
                timing_log_path(),
                record,
                on_error="silent",
                mode=0o600,
                max_bytes=_TIMING_LOG_MAX_BYTES,
            )
    except migration_lock.MigrationLockBusy as exc:
        migration_lock.observe("timing-log", "refused-telemetry", str(exc), quiet=True)


# Phase A cap (telemetry only): timing.jsonl is not a migrated data domain, so a
# .1 sibling is safe here. The guard/backend logs stay uncapped until the
# toolkit-home migration's inventory knows about <file>.1 (phase B).
_TIMING_LOG_MAX_BYTES = 20 * 1024 * 1024


_TIMING_PARENT: ContextVar[tuple[str, str] | None] = ContextVar(
    "timing_parent", default=None
)


@contextmanager
def timing_span(name: str, **fields: str | int) -> Iterator[dict[str, object]]:
    """Opt-in nested timings; callers must supply only fixed operational labels.

    AGENT_TOOLKIT_TIMING=1 appends JSONL to
    $XDG_STATE_HOME/agent-toolkit/timing.jsonl (default ~/.local/state).
    Records include UTC start, monotonic duration, PID, trace/span/parent IDs,
    outcome and caller-supplied metadata. Never pass argv, prompts or errors.
    Disabled mode performs no file I/O; recording failures stay silent.
    """
    record: dict[str, object] = {}
    if os.environ.get("AGENT_TOOLKIT_TIMING") != "1":
        yield record
        return
    parent = _TIMING_PARENT.get()
    span_id = uuid.uuid4().hex
    trace_id = parent[0] if parent else uuid.uuid4().hex
    token = _TIMING_PARENT.set((trace_id, span_id))
    record.update(fields)
    record.update(
        name=name,
        trace_id=trace_id,
        span_id=span_id,
        parent_id=parent[1] if parent else None,
        pid=os.getpid(),
        started_at=datetime.now(UTC).isoformat(),
        outcome="success",
    )
    start = time.monotonic()
    try:
        yield record
    except BaseException as exc:
        if record.get("outcome") == "success":
            record["outcome"] = "error"
        if isinstance(exc, SystemExit):
            record["outcome"] = "success" if exc.code in (None, 0) else "error"
        elif isinstance(exc, (KeyboardInterrupt, GeneratorExit)):
            record["outcome"] = "cancelled"
        raise
    finally:
        record["duration_seconds"] = round(time.monotonic() - start, 6)
        _TIMING_PARENT.reset(token)
        with suppress(Exception):
            _append_timing_record(record)
