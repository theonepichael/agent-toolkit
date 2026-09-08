"""Shared CLI helpers used across dotfiles scripts.

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
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO

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


def append_jsonl(path: Path, record: dict[str, object]) -> None:
    """Best-effort: append one JSON record to `path` as a single JSONL line.

    Never raises to the caller: a write failure (bad data, disk full,
    permissions) is swallowed and debug-logged via get_logger so a --verbose
    operator can still see it. Logging a record is not part of any caller's
    contract.

    Concurrency-safe across processes by construction: lazy parent mkdir,
    then one unbuffered, single write() of the whole line under O_APPEND
    ("ab", buffering=0) — exactly one write(2) syscall for a line well under
    PIPE_BUF, which makes concurrent writers' lines interleaving-free. A
    buffered text-mode open(path, "a") does not carry that guarantee.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = (json.dumps(record) + "\n").encode()
        with path.open("ab", buffering=0) as f:
            f.write(line)
    except Exception as exc:
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
        try:
            state = os.environ.get("XDG_STATE_HOME")
            base = (
                Path(state)
                if state and Path(state).is_absolute()
                else Path.home() / ".local/state"
            )
            path = base / "agent-toolkit/timing.jsonl"
            path.parent.mkdir(parents=True, exist_ok=True)
            data = (json.dumps(record) + "\n").encode()
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "ab", buffering=0) as stream:
                stream.write(data)
        except Exception:
            pass
