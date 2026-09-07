#!/usr/bin/env python3
"""llm_backends.py — shared subprocess plumbing for CLI-agent backends
(agy, opencode, pi, copilot). Extracted from second_opinion.py so
dev_status.py's recap generation can reuse the same process-lifecycle
handling (timeouts, process-group kills, opencode JSON-event parsing) with
its own timeout and model choices, without duplicating it.

Requires Python 3.12+.
"""

import functools
import json
import os
import re
import shutil
import signal
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path

BACKEND_PRIORITY = ["agy", "pi", "opencode", "copilot"]

# ── isolation contract ────────────────────────────────────────────────────
#
# Every backend invocation in this repo — adversarial critique and plain
# completion alike — must satisfy all of these. A backend meets every clause
# or it is not eligible; there is no partially-isolated tier, because a
# weaker tier becomes the working default once its warning is normalised.
#
# Why this exists: measured 2026-08-31, a critique call could write files and,
# through pi's dev_status tool, mutate the real backlog store, while every
# backend was handed the user's own instruction files — so the "outside"
# reviewer held the user's rulebook. _raise_on_emitted_tool_call below is not
# a guard against that: it catches tool-call markup leaking through as *text*
# and cannot see a real tool action.
#
# tools_execution and tools_reach are deliberately separate, because the two
# available mechanisms deliver different things and conflating them would let
# the contract claim more than it enforces. A vendor flag can remove the tools
# outright; OS containment cannot -- it leaves them running with nothing of
# yours in reach. Both clauses are mandatory either way, so this is still one
# bar: a backend satisfies each clause by one mechanism or the other, and
# containment satisfies tools_execution only in the sense that there is
# nothing left to execute against.
ISOLATION_CLAUSES: tuple[str, ...] = (
    "tools_execution",
    "tools_reach",
    "context",
    "templates",
    "skills",
    "mcp",
    "session",
)

# Sentinel: this clause has no vendor mechanism and is satisfied only by
# running the backend under OS containment. Never treat it as "no flags
# needed" — a containment path that silently no-ops leaves a call that
# believes it is isolated and is not.
OS_CONTAINED = object()

# Sentinel: this clause is moot for this backend because another clause
# already removed the capability -- a backend with no tools at all has no
# reach to constrain. Distinct from an empty flag list, which means "declared,
# and satisfied by the base command".
NOT_APPLICABLE = object()

# Per-backend capability descriptor: for each contract clause, the concrete
# mechanism that satisfies it. Coverage lives in data rather than in branches
# so a backend added without a complete descriptor is unbuildable, instead of
# depending on whoever adds it remembering to handle every clause.
#
# "_base" is the command prefix; the builder appends the prompt last unless
# the descriptor sets _prompt_follows_base (a prompt flag that consumes the
# next token as its value -- see copilot/agy).
# Every mechanism below was verified by running it — see the plan artifact at
# ~/.claude/data/grill/2026-08-31-meta-second-opinion-backend-isol-plan.md.
BACKEND_ISOLATION: dict[str, dict[str, object]] = {
    # Verified: --no-tools reports "no tools are available in this session";
    # -nc/-np leave no instruction text in context. Skills and MCP servers
    # reach pi as tools, so --no-tools covers them too.
    "pi": {
        "_base": ["pi", "-p", "--no-session", "--provider", "opencode-go"],
        "tools_execution": ["--no-tools"],
        "tools_reach": NOT_APPLICABLE,
        "skills": ["--no-tools"],
        "mcp": ["--no-tools"],
        "context": ["--no-context-files"],
        "templates": ["--no-prompt-templates"],
        "session": ["--no-session"],
    },
    # Verified: --no-custom-instructions drops the instruction files, and
    # --deny-tool blocks the tools explicitly. Headless copilot also happens
    # to deny writes on its own, but "happens to" is not a mechanism — the
    # contract requires a declared one.
    "copilot": {
        # copilot's -p/--prompt takes the next argument as its prompt value,
        # so the prompt binds to the flag rather than trailing the command —
        # -p must be last in _base, and the isolation flags follow the
        # prompt. The old shape ["copilot", "-p", "--silent"] made -p
        # swallow --silent and left the prompt as a bare positional copilot
        # rejects. Verified against copilot --help: flags after the -p value
        # are parsed normally.
        "_base": ["copilot", "--silent", "-p"],
        "_prompt_follows_base": True,
        "tools_execution": ["--deny-tool=write", "--deny-tool=shell"],
        "tools_reach": NOT_APPLICABLE,
        "skills": ["--deny-tool=write", "--deny-tool=shell"],
        "mcp": ["--deny-tool=write", "--deny-tool=shell"],
        "context": ["--no-custom-instructions"],
        "templates": ["--no-custom-instructions"],
        "session": [],
    },
    # The adversary agent's "permission": "deny" genuinely blocks tools —
    # verified, it refused a canary write. But opencode reads
    # ~/.claude/CLAUDE.md as a global instruction fallback with no flag to
    # stop it (bisected 2026-08-31 by shadow-HOME removal), so the context
    # clause needs containment.
    "opencode": {
        "_base": [
            "opencode",
            "run",
            "--auto",
            "--format",
            "json",
            "--agent",
            "adversary",
        ],
        "_model_flag": "-m",
        "_contain": {
            "expose": [".config", ".local", ".cache", ".opencode", ".bun"],
            "shadow": {},
        },
        "tools_execution": ["--agent", "adversary"],
        "tools_reach": OS_CONTAINED,
        "skills": ["--agent", "adversary"],
        "mcp": ["--agent", "adversary"],
        "context": OS_CONTAINED,
        "templates": OS_CONTAINED,
        "session": [],
    },
    # agy has no qualifying vendor mechanism for anything. Verified defeated:
    # --sandbox (confines writes to agy's artifacts dir but arbitrary file
    # read survives), a permissions {allow: [], deny: ["*"]} block, and a
    # top-level disabledTools list naming all 17 of its tools. It exposes no
    # tool-disable flag, so containment is its only qualifying path.
    "agy": {
        "_base": ["agy", "--print"],
        # agy's --print takes the next argument as its prompt value, so the
        # prompt binds to the flag rather than trailing the command. It says
        # so itself: "--print took --model as its prompt".
        "_prompt_follows_base": True,
        "_contain": {
            "expose": [".local", ".cache"],
            "shadow": {".gemini": ["GEMINI.md"]},
        },
        "tools_execution": OS_CONTAINED,
        "tools_reach": OS_CONTAINED,
        "skills": OS_CONTAINED,
        "mcp": OS_CONTAINED,
        "context": OS_CONTAINED,
        "templates": OS_CONTAINED,
        "session": OS_CONTAINED,
    },
}


class IsolationError(RuntimeError):
    """A backend cannot be invoked because it does not meet the contract.

    Distinct from BackendError, which means an eligible backend was tried and
    failed. This one means it is never tried at all: a capability failure, not
    a liveness failure, and never a fallback target.
    """


@functools.cache
def containment_available() -> bool:
    """Whether OS containment can actually be established on this host.

    Checked by doing it, not by inspecting the platform: `unshare` exists on
    a host whose kernel may still refuse unprivileged user namespaces, and a
    containment path that silently no-ops is worse than no containment path.

    Cached: the answer cannot change within a process, and probing on every
    backend call would put a subprocess spawn in front of every critique.
    """
    if shutil.which("unshare") is None:
        return False
    try:
        probe = subprocess.run(
            ["unshare", "-Urm", "--map-root-user", "true"],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0


# A vendor daemon already listening holds session state that neither a CLI
# flag nor a $HOME tmpfs can reach: the flags configure the client, and the
# state lives in a process nobody in this repo started. Raised during the
# 2026-08-31 critique as a leak path both isolation mechanisms miss.
#
# Detected by looking for a listening socket owned by the backend's own
# binary, not by probing a fixed port: `opencode serve --port` defaults to 0,
# i.e. the kernel picks an ephemeral port, so any hardcoded port number would
# be wrong nearly always and would report "no daemon" while one is running.
_DAEMON_BACKENDS: frozenset[str] = frozenset({"opencode"})


def daemon_listening(backend: str) -> bool:
    """Whether a daemon belonging to ``backend`` currently holds a listening
    socket.

    Returns False when the socket tooling is unavailable rather than guessing:
    a false "no daemon" here is caught by the session-clause refusal only if
    the daemon is real, so the honest failure mode is to under-report and let
    the live canary tier catch it, not to fabricate certainty.
    """
    if backend not in _DAEMON_BACKENDS:
        return False
    if shutil.which("ss") is None:
        return False
    try:
        listing = subprocess.run(
            ["ss", "-lptn"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return f'"{backend}"' in listing.stdout


def _uncovered_clauses(spec: dict[str, object]) -> list[str]:
    return [clause for clause in ISOLATION_CLAUSES if clause not in spec]


def build_isolated_command(
    backend: str, prompt: str, *, model: str | None
) -> list[str]:
    """Build the only command any caller may run for ``backend``.

    Refuses rather than degrading: an incomplete descriptor, an unknown
    backend, or a containment-dependent backend on a host that cannot contain
    all raise IsolationError.

    Raises:
        IsolationError: naming the backend and the specific unmet clause.
    """
    spec = BACKEND_ISOLATION.get(backend)
    if spec is None:
        raise IsolationError(
            f"{backend}: no isolation descriptor — every backend must declare "
            f"a mechanism for each of {', '.join(ISOLATION_CLAUSES)} before it "
            "can be invoked"
        )

    uncovered = _uncovered_clauses(spec)
    if uncovered:
        raise IsolationError(
            f"{backend}: isolation descriptor does not cover "
            f"{', '.join(uncovered)} — refusing to build an unisolated command"
        )

    if daemon_listening(backend):
        raise IsolationError(
            f"{backend}: a vendor daemon is already listening, which can hold "
            "session state that neither the isolation flags nor the $HOME "
            "sandbox can reach — refusing the `session` clause. Stop it and "
            "retry."
        )

    contained = [c for c in ISOLATION_CLAUSES if spec[c] is OS_CONTAINED]
    if contained and not containment_available():
        raise IsolationError(
            f"{backend}: clause(s) {', '.join(contained)} can only be satisfied "
            "by OS containment, which is unavailable on this host (no unshare, "
            "or unprivileged user namespaces are disabled)"
        )

    cmd: list[str] = list(spec["_base"])  # type: ignore[arg-type]
    if spec.get("_prompt_follows_base"):
        cmd.append(prompt)
    for clause in ISOLATION_CLAUSES:
        mechanism = spec[clause]
        if mechanism is OS_CONTAINED or mechanism is NOT_APPLICABLE:
            continue
        for flag in mechanism:  # type: ignore[union-attr]
            if flag not in cmd:
                cmd.append(flag)
    if model:
        cmd += [str(spec.get("_model_flag", "--model")), model]
    if not spec.get("_prompt_follows_base"):
        cmd.append(prompt)

    if contained:
        cmd = _wrap_in_containment(cmd, spec.get("_contain", {}))  # type: ignore[arg-type]
    return cmd


_CONTAINMENT_SCRIPT = r"""set -eu
H="$SB_HOME"
STAGE="$(mktemp -d)"

# Stage every bind source BEFORE the tmpfs goes over $HOME. Mount order is
# load-bearing: a tmpfs mounted first hides the very directories the later
# binds read from, which presents as the backend binary vanishing rather
# than as a mount error.
i=0
for src in $SB_EXPOSE; do
    if [ -e "$H/$src" ]; then
        mkdir -p "$STAGE/e$i"
        mount --bind "$H/$src" "$STAGE/e$i"
    fi
    i=$((i + 1))
done
if [ -n "$SB_SHADOW_DIR" ] && [ -d "$H/$SB_SHADOW_DIR" ]; then
    mkdir -p "$STAGE/real"
    mount --bind "$H/$SB_SHADOW_DIR" "$STAGE/real"
fi

mount -t tmpfs tmpfs "$H"

i=0
for src in $SB_EXPOSE; do
    if [ -e "$STAGE/e$i" ]; then
        mkdir -p "$H/$src"
        mount --bind "$STAGE/e$i" "$H/$src"
    fi
    i=$((i + 1))
done

# Rebuild the shadowed directory entry by entry, omitting the named ones.
# Bind each entry rather than symlinking it: a symlink pointing back into
# $H/<dir> would resolve to this very directory once it is mounted, and the
# backend fails with ELOOP instead of anything legible.
if [ -d "$STAGE/real" ]; then
    mkdir -p "$H/$SB_SHADOW_DIR"
    for entry in $(ls -A "$STAGE/real"); do
        skip=0
        for omit in $SB_SHADOW_OMIT; do
            [ "$entry" = "$omit" ] && skip=1
        done
        [ "$skip" = "1" ] && continue
        if [ -d "$STAGE/real/$entry" ]; then
            mkdir -p "$H/$SB_SHADOW_DIR/$entry"
        else
            : > "$H/$SB_SHADOW_DIR/$entry"
        fi
        mount --bind "$STAGE/real/$entry" "$H/$SB_SHADOW_DIR/$entry"
    done
fi

# Blank /tmp and land in an empty cwd. $HOME alone is not the user's data:
# scratch files, worktrees and anything the caller happens to be sitting in
# are all reachable otherwise, and a reviewer that can read the tree it is
# reviewing is not an outside opinion.
mount -t tmpfs tmpfs /tmp
mkdir -p /tmp/cwd
cd /tmp/cwd

exec "$@"
"""


def _wrap_in_containment(cmd: list[str], contain: dict[str, object]) -> list[str]:
    """Wrap ``cmd`` so it runs with the user's home blanked.

    Everything under $HOME disappears except what the backend needs to
    function: its own binary and its credentials. What the sandbox hides is a
    design input, not a property of unshare — a wrapper that establishes a
    namespace but mounts nothing contains nothing.

    ``shadow`` handles the awkward case where credentials and the instruction
    file live in the *same* directory: agy's OAuth token and its GEMINI.md are
    both under ~/.gemini, so hiding the directory wholesale drops it into an
    interactive Google OAuth flow. That directory is rebuilt entry by entry
    with the named files omitted.
    """
    home = os.path.expanduser("~")
    expose = list(contain.get("expose", []))  # type: ignore[arg-type]
    shadow_spec: dict[str, list[str]] = contain.get("shadow", {})  # type: ignore[assignment]
    shadow_dir = next(iter(shadow_spec), "")
    omit = shadow_spec.get(shadow_dir, []) if shadow_dir else []

    return [
        "unshare",
        "-Urm",
        "--map-root-user",
        "env",
        f"SB_HOME={home}",
        f"SB_EXPOSE={' '.join(expose)}",
        f"SB_SHADOW_DIR={shadow_dir}",
        f"SB_SHADOW_OMIT={' '.join(omit)}",
        "/bin/sh",
        "-c",
        _CONTAINMENT_SCRIPT,
        "sh",
        *cmd,
    ]


def eligibility_report() -> dict[str, dict[str, object]]:
    """Per-backend presence and contract eligibility, with a reason when not.

    Surfaces an ineligible or unavailable backend before a critique is needed
    rather than at the moment one is wanted.
    """
    can_contain = containment_available()
    report: dict[str, dict[str, object]] = {}
    for name in BACKEND_PRIORITY:
        present = shutil.which(name) is not None
        spec = BACKEND_ISOLATION.get(name)
        if spec is None:
            report[name] = {
                "present": present,
                "eligible": False,
                "reason": "no isolation descriptor",
            }
            continue
        uncovered = _uncovered_clauses(spec)
        if uncovered:
            reason = f"descriptor does not cover {', '.join(uncovered)}"
        elif not present:
            reason = "not installed"
        elif (
            any(spec[c] is OS_CONTAINED for c in ISOLATION_CLAUSES) and not can_contain
        ):
            reason = "requires OS containment, unavailable on this host"
        else:
            reason = ""
        report[name] = {
            "present": present,
            "eligible": not reason,
            "reason": reason,
        }
    return report


# Each pattern captures one shape a tool-hungry model can emit as literal
# text when every tool is denied (opencode's "permission": "deny") or a
# swapped-in model is otherwise tool-starved: XML with the <tool_calls>
# wrapper, bare XML without it, or a JSON-shaped tool-call block — attribute
# order and the presence of a wrapper/parameters aren't guaranteed, so each
# form is matched independently. Every pattern requires an actual *closing*
# marker (</tool_calls>, </invoke>, a <parameter ...name=, or a closing `]`/
# `}`); none fall back to matching to end-of-string on an unclosed tag, since
# that would also swallow prose that merely *mentions* an opening tag without
# ever completing the structure (e.g. "will never emit <tool_calls>
# wrappers").
_TOOL_CALL_LEAK_PATTERNS = (
    re.compile(r"<tool_calls\b.*?</tool_calls>", re.IGNORECASE | re.DOTALL),
    re.compile(
        r"<invoke\b[^>]*\bname\s*=.*?(?:<parameter\b[^>]*\bname\s*=|</invoke>)",
        re.IGNORECASE | re.DOTALL,
    ),
    re.compile(r'"tool_calls"\s*:\s*\[.*?\]', re.IGNORECASE | re.DOTALL),
    re.compile(r'"type"\s*:\s*"tool_use".*?\}', re.IGNORECASE | re.DOTALL),
)

# Caps how much of a response the leak check scans. Bounds worst-case regex
# cost on a degenerate repetition-loop response (measured 15-42s unbounded at
# 500KB-2MB of repeated markup-like text) while covering any realistic
# critique length with room to spare — a genuine leak is the model's entire
# turn, so it always appears well within this window.
_TOOL_CALL_LEAK_SCAN_LIMIT = 20_000

# Fraction of the scanned text that must fall inside a matched block before
# it's treated as a leak rather than prose that quotes/discusses an example.
# A genuine leaked tool call largely *is* the response; a critique that
# illustrates the failure mode with a quoted snippet has that snippet
# embedded in much more surrounding analysis, so its ratio stays low.
_TOOL_CALL_LEAK_DOMINANCE_RATIO = 0.4

_active_process: subprocess.Popen[str] | None = None


class BackendError(Exception):
    """A backend was invoked but failed (timeout or nonzero exit)."""


class BackendTimeoutError(BackendError):
    """A backend call failed because every attempt (initial + retries) timed
    out -- the specific silent-stall failure mode instrumentation exists to
    measure, distinct from a normal nonzero-exit or empty-output failure.

    A strict BackendError subclass: every existing ``except BackendError``
    call site keeps catching this unchanged.
    """


class BackendPayloadSizeError(BackendError):
    """A backend call was rejected before invocation because the payload
    exceeds the maximum size known to work reliably for that backend.

    A strict BackendError subclass: callers treating all failures generically
    catch this unchanged, while callers reporting rule-outs distinguish it.
    """


class BackendModelPolicyError(BackendError):
    """A backend rejected a model because the ACCOUNT cannot select models
    through the model flag -- an entitlement failure, not a bad model id.

    Concretely (copilot): GitHub Copilot's ``--model`` flag requires a Pro
    or Enterprise plan. On a free tier EVERY id is rejected with vendor
    wording ('Model X from --model flag is not available') that describes an
    entitlement problem in the language of a bad-id problem -- which is why
    it read as a wrong pool and cost two misdiagnoses (2026-09-03). The
    re-raised message states the real cause and, for copilot, names the
    per-machine pool variable to unset.

    A strict BackendError subclass: every existing ``except BackendError``
    call site keeps catching this unchanged, while callers reporting
    rule-outs distinguish it.
    """


# The minimal stable fragment of copilot's entitlement-rejection wording.
# Deliberately not the vendor's full sentence: they can reword the rest, and
# a reworded message must degrade to a plain BackendError, never
# mis-classify -- which is also why there is no retry-without-model fallback
# keyed on this text.
_COPILOT_MODEL_POLICY_MARKER = "from --model flag is not available"


PI_MAX_PROMPT_BYTES = 14000


def available_backends() -> list[str]:
    """Return the backends in :data:`BACKEND_PRIORITY` that are on ``PATH``."""
    return [b for b in BACKEND_PRIORITY if shutil.which(b)]


def eligible_backends() -> list[str]:
    """Backends that are installed AND meet the isolation contract, in priority
    order.

    Capability and liveness are different failures and are handled in different
    places. This is the capability filter: a backend that cannot meet the
    contract is never returned, so it is never tried and never used as a
    fallback. A backend that is eligible but hangs is a liveness failure, and
    that is what :func:`run_with_fallback` handles.
    """
    report = eligibility_report()
    return [b for b in BACKEND_PRIORITY if report.get(b, {}).get("eligible")]


def resolve_backend() -> str | None:
    """Return the highest-priority eligible backend, or ``None`` if none is."""
    backends = eligible_backends()
    return backends[0] if backends else None


def run_with_fallback(
    runner: "Callable[[str], str]",
    *,
    backends: list[str] | None = None,
) -> tuple[str, str]:
    """Try each eligible backend in turn; return ``(backend, output)``.

    A backend that raises :class:`BackendError` — a stall, a timeout, an empty
    stream — is a liveness failure, and the next eligible backend is tried.
    This is not a safety compromise: every candidate has already passed the
    capability filter, so falling through never lands on an unisolated one.

    Why this exists: on 2026-08-31 the opencode-go gateway stalled four
    consecutive calls (a known intermittent fault bisected in this repo on
    2026-08-17 at a 20-33% rate), which with no fallback took the whole
    critique feature down even though every backend was contract-eligible.

    Raises:
        IsolationError: if no backend meets the contract at all — a capability
            failure, deliberately distinct from every backend having been tried
            and failed.
        BackendError: if every eligible backend was tried and each failed.
    """
    candidates = backends if backends is not None else eligible_backends()
    if not candidates:
        report = eligibility_report()
        detail = "; ".join(
            f"{name}: {entry['reason']}"
            for name, entry in report.items()
            if entry.get("reason")
        )
        raise IsolationError(
            f"no backend meets the isolation contract — {detail or 'none installed'}"
        )

    failures: list[str] = []
    for backend in candidates:
        try:
            return backend, runner(backend)
        except BackendError as exc:
            failures.append(f"{backend}: {exc}")
    raise BackendError("all eligible backends failed — " + "; ".join(failures))


def _safe_get(obj: object, *keys: str) -> object | None:
    """Walk nested dict keys, returning ``None`` at the first missing or non-dict step.

    Backend subprocess output is external, loosely-specified JSON — a field
    documented as "usually a nested object" can still show up as a bare
    string or be absent entirely on a given event. A chained
    ``d.get("a", {}).get("b")`` crashes with ``AttributeError`` the moment
    any intermediate value isn't actually a dict; this never does.

    Args:
        obj: The value to walk, expected to be a dict (or nested dicts).
        *keys: Keys to look up in sequence.

    Returns:
        The value at the end of the key path, or ``None`` if ``obj`` (or
        any intermediate value) isn't a dict, or a key is missing.
    """
    current = obj
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _kill_active_process() -> None:
    """Kill the currently-running backend subprocess's entire process group, if any."""
    proc = _active_process
    if proc is None or proc.poll() is not None:
        return
    with suppress(ProcessLookupError):
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    proc.wait()


def _run_command(
    cmd: list[str], timeout: float, *, retries: int = 0
) -> tuple[int, str, str]:
    """Run ``cmd`` as a subprocess, capturing its output.

    Tracks the running process in :data:`_active_process` so a termination
    signal or a timeout can kill it (and its whole process group).

    ``stdin`` is always :data:`subprocess.DEVNULL`, never inherited: every
    backend here is a one-shot, non-interactive prompt, so a child never
    needs to read from our stdin. Without this, a child that reads stdin at
    all (opencode's CLI does, unconditionally, per
    ``anomalyco/opencode#38723``) inherits our own process's stdin file
    descriptor by default and blocks forever if that descriptor never
    reaches EOF (e.g. an open pipe with no writer, which is exactly what a
    long-lived parent process like this one hands its children) — this
    reproduced reliably in this environment and is a confirmed root cause of
    "opencode intermittently hangs" reports upstream, not merely a
    hypothesis.

    Args:
        cmd: The command and arguments to execute.
        timeout: Seconds to wait before killing the process.
        retries: Extra attempts after an initial timeout, before raising —
            each retry re-runs ``cmd`` from scratch (a fresh subprocess),
            waiting up to ``timeout`` again, so worst-case wall time is
            ``timeout * (1 + retries)``. Only a *timeout* triggers a retry;
            a nonzero exit or a failed-to-start error still raises/returns
            on the first attempt (those aren't the observed opencode
            failure mode this exists for — see ``run_opencode``). Default
            0 preserves the original no-retry behavior for every other
            caller (agy, copilot).

    Returns:
        ``(returncode, stdout, stderr)``.

    Raises:
        BackendError: If ``cmd``'s executable can't be started.
        BackendTimeoutError: If every attempt (the initial one plus
            ``retries``) times out. A strict ``BackendError`` subclass, so
            existing ``except BackendError`` call sites still catch it.
    """
    global _active_process
    attempt = 0
    while True:
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except OSError as e:
            # e.g. the backend vanished from PATH between `shutil.which` and
            # here — without this, an unhandled OSError would crash the
            # whole program instead of letting the caller's per-backend
            # fallback run.
            raise BackendError(f"failed to start {cmd[0]}: {e}") from e
        _active_process = proc
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_active_process()
            # `_kill_active_process` only reaps the process (`wait()`); the
            # stdout/stderr pipes opened by Popen(..., stdout=PIPE, stderr=PIPE)
            # are still open at this point. A second `communicate()` on the
            # now-dead process drains and closes them — without it, the fds
            # leak until the Popen object happens to get garbage-collected.
            proc.communicate()
            if attempt < retries:
                attempt += 1
                continue
            suffix = f" (all {retries + 1} attempts timed out)" if retries else ""
            raise BackendTimeoutError(f"timed out after {timeout}s — killed{suffix}")
        finally:
            _active_process = None
        return proc.returncode, stdout, stderr


def run_backend_command(cmd: list[str], timeout: float) -> str:
    """Run a backend CLI command and return its critique/prose text.

    Args:
        cmd: The command and arguments to execute.
        timeout: Seconds to wait before killing the process.

    Returns:
        The backend's stripped stdout.

    Raises:
        BackendError: If the process exits nonzero, or exits 0 with empty
            stdout (still a failure — see inline comment).
    """
    returncode, stdout, stderr = _run_command(cmd, timeout)
    if returncode != 0:
        raise BackendError(f"exited {returncode}: {stderr.strip()}")
    result = stdout.strip()
    if not result:
        # Exit 0 with empty stdout is still a failure — e.g. agy in headless
        # mode has its tool calls auto-denied, prints "no output produced" to
        # stderr, and exits 0. Treating that as success would silently pass
        # empty output through and skip the caller's priority fallback.
        detail = stderr.strip() or "(no stderr)"
        raise BackendError(f"exited 0 but produced no output: {detail}")
    return result


def _backend_call_log_path() -> Path:
    """Where each run_* call's outcome record is appended.

    Computed fresh on every call rather than cached at import time, so the
    repo-root pytest sandbox's HOME redirection (test/AGENTS.md) always
    takes effect — a module-level constant would freeze whatever HOME was
    set at import.
    """
    return Path.home() / ".claude" / "data" / "backend_calls.jsonl"


def _log_backend_call(
    backend: str,
    model: str | None,
    outcome: str,
    wall_seconds: float,
    prompt_bytes: int,
) -> None:
    """Best-effort: append one JSONL record of a backend-call attempt.

    Never raises. Measures the real opencode-go stall rate from actual
    usage instead of anecdote — see the 2026-08-17 bisection referenced in
    :func:`run_opencode`. Logging a call is not part of any caller's
    contract, so a failure here (bad data, disk full, permissions) must
    never surface as a call failure.
    """
    try:
        record = {
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "backend": backend,
            "model": model,
            "outcome": outcome,  # "success" | "timeout" | "error"
            "wall_seconds": round(wall_seconds, 2),
            "prompt_bytes": prompt_bytes,
        }
        line = (json.dumps(record) + "\n").encode()
        path = _backend_call_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # Unbuffered, single write() of the whole line: guarantees exactly
        # one write(2) syscall for a line well under PIPE_BUF (4096 on
        # Linux), which is what makes O_APPEND interleaving-free across
        # concurrent processes. A buffered text-mode open(path, "a") does
        # not carry that guarantee.
        with path.open("ab", buffering=0) as f:
            f.write(line)
    except Exception:
        pass


@contextmanager
def _track_backend_call(backend: str, model: str | None, prompt: str) -> Iterator[None]:
    """Time and log one backend-call attempt via :func:`_log_backend_call`.

    Call only after :func:`build_isolated_command` has already succeeded —
    an IsolationError raised before entering this context is a capability
    failure, not an attempt, and must never be logged.

    The final ``except Exception`` branch is deliberately broader than
    ``BackendError``: it never masks anything (the original exception and
    its traceback still propagate via ``raise``), it only guarantees that
    every failure mode — including one this module doesn't raise today —
    gets one logged "error" record instead of silently escaping uncounted.
    """
    start = time.monotonic()
    prompt_bytes = len(prompt.encode())
    outcome = "success"
    try:
        yield
    except BackendTimeoutError:
        outcome = "timeout"
        raise
    except Exception:
        outcome = "error"
        raise
    finally:
        _log_backend_call(
            backend, model, outcome, time.monotonic() - start, prompt_bytes
        )


def run_agy(prompt: str, *, model: str, timeout: float) -> str:
    """Run the ``agy`` backend with the given model and return its text output.

    Raises IsolationError before running anything if this host cannot contain
    agy — it has no vendor mechanism for any contract clause, so containment
    is its only qualifying path (see BACKEND_ISOLATION). Instrumented via
    :func:`_track_backend_call`, entered only after the isolated command is
    built, so an IsolationError is never logged as an attempt.
    """
    cmd = build_isolated_command("agy", prompt, model=model)
    with _track_backend_call("agy", model, prompt):
        return run_backend_command(cmd, timeout)


def run_copilot(prompt: str, *, model: str | None, timeout: float) -> str:
    """Run the ``copilot`` backend and return its text output.

    No tool-permission flags are passed. Copilot CLI's permission system
    (``copilot help permissions``) only gates ``shell``, ``write``, ``url``,
    and MCP-server tools — its built-in file-read tool isn't gated at all.
    An adversarial critique or a recap prompt never needs to write or run
    shell commands, so there's nothing to allow.

    ``model``, when truthy, forces ``--model``. Copilot CLI's ``--model``
    flag is gated by a per-account "model picker" policy — on a
    policy-disabled account every explicit model is rejected, and only the
    implicit default routing (no ``--model`` flag at all) works. Callers
    should leave ``model`` unset/empty unless their account is confirmed to
    allow explicit model selection.

    A failure whose text carries the vendor's entitlement fragment
    (:data:`_COPILOT_MODEL_POLICY_MARKER`) is re-raised as
    :class:`BackendModelPolicyError` stating the true cause -- the flag
    needs a Pro or Enterprise plan, so no model id works -- and the pool
    variables to unset on a free-tier machine. No retry without the model:
    keying a fallback on vendor error text they can reword would turn a
    working backend into a silent regression.
    """
    cmd = build_isolated_command("copilot", prompt, model=model)
    with _track_backend_call("copilot", model, prompt):
        try:
            return run_backend_command(cmd, timeout)
        except BackendError as exc:
            if _COPILOT_MODEL_POLICY_MARKER in str(exc):
                raise BackendModelPolicyError(
                    "copilot cannot select a model: the --model flag requires "
                    "a GitHub Copilot Pro or Enterprise plan, so no model id "
                    "works on this account's tier -- the rejection is an "
                    "entitlement failure, not a bad model id. Vendor wording: "
                    f"{exc}. On a free-tier machine, unset "
                    "SECOND_OPINION_COPILOT_MODEL_POOL (and "
                    "SECOND_OPINION_COPILOT_MODEL) to let copilot use its "
                    "implicit default routing."
                ) from exc
            raise


_DEFAULT_PI_PROVIDER = "opencode-go"


def run_pi(prompt: str, *, model: str | None, timeout: float) -> str:
    """Run Pi's headless mode and return its text output.

    Generic invocation for callers that just want a plain completion (e.g.
    dev_status.py's recap prose). Always targets the ``opencode-go`` gateway
    provider — the only provider confirmed authenticated on this machine
    (``pi auth check --provider opencode-go --json`` -> ``ready``) and the
    one every configured model pool entry resolves through; no alternative
    provider is configured in ``~/.pi/agent/models.json`` on this machine.

    Unlike opencode's ``adversary`` agent (which sets ``permission: deny``
    so a swapped-in model can only return prose), no equivalent
    restricted-permission invocation for Pi is verified yet — this call
    passes no tool-restriction flag. :func:`_raise_on_emitted_tool_call`
    below is the only backstop against a model leaking an attempted tool
    call as text; it cannot catch Pi actually taking a real tool action
    instead of returning a critique.

    No retry on timeout (unlike :func:`run_opencode`): telemetry
    (2026-08-31/09-01, ``~/.claude/data/backend_calls.jsonl``) showed a 3/3
    failure rate on realistic review prompts (>14KB) through this same
    ``opencode-go`` gateway — a deterministic payload-size stall (~14-20KB
    band), not opencode's intermittent 20-33% flake. A retry there has real
    odds of succeeding on the second attempt; here it would only double the
    wasted wall time (120s -> 240s) before falling through to the next
    backend, with no expected gain in success rate. Small prompts (<1KB)
    succeed in ~3s, so this only bites realistic-sized review prompts.
    Prompts over :data:`PI_MAX_PROMPT_BYTES` (14000 bytes) are refused
    immediately to avoid waiting for deterministic timeouts.

    Raises:
        BackendPayloadSizeError: If ``prompt`` exceeds :data:`PI_MAX_PROMPT_BYTES`.
        BackendError: If the process exits nonzero or produces no output
            (via :func:`run_backend_command`), or the output is dominated by
            leaked tool-call markup (via :func:`_raise_on_emitted_tool_call`).
    """
    prompt_bytes = len(prompt.encode())
    if prompt_bytes > PI_MAX_PROMPT_BYTES:
        raise BackendPayloadSizeError(
            f"prompt size ({prompt_bytes} bytes) exceeds pi limit "
            f"({PI_MAX_PROMPT_BYTES} bytes); pi's opencode-go gateway "
            f"deterministically stalls on larger payloads"
        )
    cmd = build_isolated_command("pi", prompt, model=model)
    with _track_backend_call("pi", model, prompt):
        text = run_backend_command(cmd, timeout)
        _raise_on_emitted_tool_call(text, context="pi")
        return text


def _opencode_json_events(raw_output: str) -> list[dict[str, object]]:
    """Parse opencode's ``--format json`` stdout into a list of event objects.

    Each non-blank line is expected to be one JSON object. Lines that
    aren't valid JSON, or that decode to something other than a JSON
    object (e.g. a stray log line, or a bare string/array), are silently
    skipped rather than raising — opencode's output stream isn't
    guaranteed to be pure JSON-lines.
    """
    events = []
    for line in raw_output.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            events.append(parsed)
    return events


def _opencode_text_chunks(events: list[dict[str, object]]) -> list[str]:
    """Extract text chunks from opencode's parsed event stream."""
    chunks = []
    for e in events:
        if e.get("type") != "text":
            continue
        text = _safe_get(e, "part", "text")
        if isinstance(text, str) and text:
            chunks.append(text)
    return chunks


def _opencode_tool_use_events(
    events: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Return the subset of opencode events that represent a tool invocation.

    opencode's ``--format json`` stream emits one ``tool_use`` event per
    completed or failed tool call. A text-only caller (an adversarial
    critique, a recap) must never see one: its presence means the agent took
    a real shell/file action instead of replying with prose.
    """
    return [e for e in events if e.get("type") == "tool_use"]


def _raise_on_tool_use(events: list[dict[str, object]], *, context: str) -> None:
    """Raise :class:`BackendError` if any opencode event is a tool invocation.

    Defense in depth on top of per-agent permission config: a future agent
    or config could reintroduce the gap a permission deny closes today, and
    silently ignoring a tool_use event (the pre-fix behavior) is exactly how
    a swapped-in model's real shell/file actions went unnoticed.

    Args:
        events: Parsed opencode ``--format json`` events.
        context: Label for the failing caller, used in the error message.

    Raises:
        BackendError: If any event in ``events`` has ``type == "tool_use"``,
            naming the tools that were invoked.
    """
    tool_uses = _opencode_tool_use_events(events)
    if not tool_uses:
        return
    tools = sorted(
        {str(tool) for e in tool_uses if (tool := _safe_get(e, "part", "tool"))}
    )
    names = ", ".join(tools) if tools else "unknown tools"
    raise BackendError(f"{context} used tools instead of returning text: {names}")


def _raise_on_emitted_tool_call(text: str, *, context: str) -> None:
    """Raise :class:`BackendError` if ``text`` looks like a leaked tool call.

    A text-only or tool-starved caller must never return a "critique" that is
    actually a model's rejected tool invocation. Some tool-hungry models
    respond to having no usable tool by emitting their attempted call as
    literal markup (XML or JSON-shaped) inside a text event instead of a real
    ``tool_use`` event — which :func:`_raise_on_tool_use` can't see. This is
    the second backstop for that leak.

    Detection combines two checks against :data:`_TOOL_CALL_LEAK_PATTERNS`,
    scanned up to :data:`_TOOL_CALL_LEAK_SCAN_LIMIT`:

    - At least one pattern must match a *closed* block (never a bare opening
      tag), so prose that only mentions tool-call syntax in passing never
      matches at all.
    - The matched block(s) must make up at least
      :data:`_TOOL_CALL_LEAK_DOMINANCE_RATIO` of the scanned text, so a
      critique that quotes/discusses a complete example snippet as one small
      part of much larger prose isn't treated the same as a response that
      *is* the leaked call.

    Args:
        text: The joined text chunks to inspect.
        context: Label for the failing caller, used in the error message.

    Raises:
        BackendError: If ``text`` is dominated by a leaked tool-call block.
    """
    scanned = text[:_TOOL_CALL_LEAK_SCAN_LIMIT]
    spans = sorted(
        (m.start(), m.end())
        for pattern in _TOOL_CALL_LEAK_PATTERNS
        for m in pattern.finditer(scanned)
    )
    if not spans:
        return
    merged: list[list[int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    matched_chars = sum(end - start for start, end in merged)
    if matched_chars / len(scanned) < _TOOL_CALL_LEAK_DOMINANCE_RATIO:
        return
    raise BackendError(f"{context} returned tool-call markup instead of prose")


def _finalize_text_response(chunks: list[str], *, context: str) -> str:
    """Join text chunks, apply the leaked-tool-call backstop, and return the result.

    Shared by both ``run_opencode`` variants (the generic one in this module
    and second_opinion.py's adversary-agent one) so the two call sites can't
    drift out of sync with each other.

    Raises:
        BackendError: Via :func:`_raise_on_emitted_tool_call`.
    """
    text = "".join(chunks).strip()
    _raise_on_emitted_tool_call(text, context=context)
    return text


def run_opencode(prompt: str, *, model: str | None, timeout: float) -> str:
    """Run opencode's default agent (no ``--agent`` override) and return its text output.

    Generic invocation for callers that just want a plain completion (e.g.
    dev_status.py's recap prose). second_opinion.py's adversarial critique
    uses its own ``run_opencode`` with ``--agent adversary``, built on the
    same ``_run_command``/event-parsing helpers this module exports.

    Raises:
        BackendError: If the event stream contains a ``tool_use`` event (the
            agent took a real shell/file action instead of returning text),
            the returned text is dominated by leaked tool-call markup (an
            attempted tool call leaking through as text instead of a real
            ``tool_use`` event), or if it has no text chunks — either because
            an explicit error event was emitted, or because nothing
            recognizable was produced at all.

    Retries once on a timeout (``retries=1``): confirmed via direct
    bisection (2026-08-17) that opencode's CLI intermittently stalls its
    event stream (emits ``step_start`` and then nothing, no ``text``/
    ``step_finish``/error — a genuine stall, not merely slow) at roughly a
    20-33% rate on prompts around 20KB, independent of exact byte count,
    the model-pool index, and whether the prompt is passed inline or via
    ``-f``/``--file`` — no fix is available at this layer, so one retry is
    the practical mitigation (drops the practical failure rate to roughly
    4-10%).
    """
    cmd = build_isolated_command("opencode", prompt, model=model)
    with _track_backend_call("opencode", model, prompt):
        _, stdout, stderr = _run_command(cmd, timeout, retries=1)
        events = _opencode_json_events(stdout)
        _raise_on_tool_use(events, context="opencode")
        chunks = _opencode_text_chunks(events)
        if chunks:
            return _finalize_text_response(chunks, context="opencode")
        for e in events:
            if e.get("type") == "error":
                message = _safe_get(e, "error", "data", "message")
                raise BackendError(f"error: {message or e.get('error')}")
        raise BackendError(f"no text output: {stderr.strip() or stdout.strip()[:200]}")
