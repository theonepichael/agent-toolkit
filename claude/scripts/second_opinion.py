#!/usr/bin/env python3
"""second_opinion.py — one-shot adversarial critique of a plan from a non-Claude
backend. Single-round by design: the multi-round loop, plan revision, and
convergence judgment all require LLM reasoning and live in prose instructions,
not here.

Described by: templates/second_opinion.md.tmpl, the canonical body template
gen_second_opinion.py generates the five second-opinion skill/command copies
from (claude/commands/second-opinion.md and its four siblings). Changing this
script's flags, env vars, or their behavior? Re-read
templates/second_opinion.md.tmpl and this file's docstring together before
committing — INTERFACES.md's entry for this module is generated from the
docstring below, and gen_second_opinion.py --check partially guards the
template against it (see that script's own docstring), but a behavior change
with no corresponding flag/name change isn't caught structurally. See
CLAUDE.md's "Keeping skill docs in sync with their scripts".

Flags
  --quiet, -q      suppress non-essential output
  --verbose, -v    emit extra diagnostic messages to stderr
   --model-index N  (review only) 0-based index into the backend model pool
                    (SECOND_OPINION_<BACKEND>_MODEL_POOL) for this call, e.g.
                    round 1 of a rotation is index 0, round 2 is index 1.
                    Supported for agy, pi, opencode, and copilot. An explicit
                    index selects the pool even when a single-model override
                    is set, and is a hard error if the selected backend has no
                    configured pool or the index is out of range — it no
                    longer silently falls back. Must be non-negative.

Env vars
  SECOND_OPINION_AGY_MODEL          force the agy model (default "Gemini 3.7 Flash (High)")
  SECOND_OPINION_AGY_MODEL_POOL     comma-separated agy model pool for
                                    --model-index rotation (default: unset,
                                    no rotation). SECOND_OPINION_AGY_MODEL, if
                                    set without --model-index, wins outright
                                    over the pool; an explicit --model-index
                                    selects the pool regardless.
  SECOND_OPINION_PI_MODEL           force the Pi model (default: unset, Pi's
                                    own default for the opencode-go provider)
  SECOND_OPINION_PI_MODEL_POOL      comma-separated Pi model pool for
                                    --model-index rotation (default: unset,
                                    no rotation). SECOND_OPINION_PI_MODEL, if
                                    set without --model-index, wins outright
                                    over the pool; an explicit --model-index
                                    selects the pool regardless.
  SECOND_OPINION_COPILOT_MODEL      force the Copilot model (default: unset)
  SECOND_OPINION_OPENCODE_MODEL     force the opencode adversary-agent model (default: live config)
  SECOND_OPINION_OPENCODE_MODEL_POOL  comma-separated opencode model pool for
                                      --model-index rotation (default: unset,
                                      no rotation). SECOND_OPINION_OPENCODE_MODEL,
                                      if set without --model-index, wins
                                      outright over the pool; an explicit
                                      --model-index selects the pool
                                      regardless.
  SECOND_OPINION_COPILOT_MODEL_POOL   same, for the copilot backend and
                                      SECOND_OPINION_COPILOT_MODEL.
  SECOND_OPINION_TIMEOUT_SECONDS    default per-backend timeout in seconds (default 120)
  SECOND_OPINION_AGY_TIMEOUT_SECONDS      override the timeout for agy calls only
  SECOND_OPINION_PI_TIMEOUT_SECONDS       override the timeout for pi calls only
  SECOND_OPINION_OPENCODE_TIMEOUT_SECONDS override the timeout for opencode calls only
  SECOND_OPINION_COPILOT_TIMEOUT_SECONDS  override the timeout for copilot calls only
                                     Unset/blank/non-integer/zero/negative
                                     falls back to SECOND_OPINION_TIMEOUT_SECONDS.
                                     A value above 300 is clamped to 300 —
                                     the previous global default is now a
                                     hard ceiling on every timeout, default
                                     or overridden.

Files read: <plan-file-or-text> (if a path), --focus-file. Nothing written.

Exit codes: 0 success; 1 any failure (no backend available, all backends failed,
bad --focus-file, unknown subcommand).

Requires Python 3.12+.
"""

import argparse
import json
import os
import shutil
import signal
import sys
from collections.abc import Mapping
from pathlib import Path
from types import FrameType
from typing import NoReturn

import cli_common
import llm_backends
from llm_backends import (
    BackendError,
    _finalize_text_response,
    _opencode_json_events,
    _opencode_text_chunks,
    _raise_on_tool_use,
    _safe_get,
    available_backends,
)

BACKEND_PRIORITY = llm_backends.BACKEND_PRIORITY

# Shared artifact storage, same directory grill.py owns. Kept in sync with
# grill.py's DATA_DIR by test_second_opinion.py's DataDirSelfEnsureTests.
DATA_DIR = Path.home() / ".claude" / "data" / "grill"

_MAX_BACKEND_TIMEOUT_SECONDS = 300  # the previous global default, now a hard ceiling


def _parse_positive_int(value: str, default: int) -> int:
    """Parse ``value`` as a positive int, clamped to the max, or fall back to ``default``.

    Blank (after stripping whitespace), non-integer, zero, and negative
    values all fall back to ``default``, itself clamped to the max --
    callers must not rely on an un-clamped ``default`` bypassing the
    ceiling. A parsed value above :data:`_MAX_BACKEND_TIMEOUT_SECONDS` is
    clamped down to it, not treated as invalid -- a large override is
    honored, capped. A leading ``+`` (e.g. ``"+120"``) is accepted, matching
    Python's own ``int()`` semantics for a positive-int string -- not
    special-cased to reject.
    """
    value = value.strip()
    if not value:
        return min(default, _MAX_BACKEND_TIMEOUT_SECONDS)
    try:
        parsed = int(value)
    except ValueError:
        return min(default, _MAX_BACKEND_TIMEOUT_SECONDS)
    if parsed <= 0:
        return min(default, _MAX_BACKEND_TIMEOUT_SECONDS)
    return min(parsed, _MAX_BACKEND_TIMEOUT_SECONDS)


BACKEND_TIMEOUT_SECONDS = _parse_positive_int(
    os.environ.get("SECOND_OPINION_TIMEOUT_SECONDS", ""), 120
)


def _resolve_timeout(env_var: str) -> int:
    """Return ``env_var``'s value as a per-backend timeout override.

    Falls back to :data:`BACKEND_TIMEOUT_SECONDS` if ``env_var`` is unset,
    blank, non-integer, zero, or negative. Read fresh from the environment
    on every call (unlike the module-latched global) -- matches how
    :func:`_resolve_pooled_model`'s single-override env var is already read
    fresh per call, not latched.
    """
    return _parse_positive_int(os.environ.get(env_var, ""), BACKEND_TIMEOUT_SECONDS)


# backend -> (pool env var, single-override env var). The single source of
# truth for pool resolution, the validator's error variable names, and test
# cleanup. agy joins opencode/pi/copilot under the same indexed-pool contract;
# its single override is SECOND_OPINION_AGY_MODEL and its pool is
# SECOND_OPINION_AGY_MODEL_POOL.
_POOL_ENV_VARS = {
    "agy": ("SECOND_OPINION_AGY_MODEL_POOL", "SECOND_OPINION_AGY_MODEL"),
    "pi": ("SECOND_OPINION_PI_MODEL_POOL", "SECOND_OPINION_PI_MODEL"),
    "opencode": ("SECOND_OPINION_OPENCODE_MODEL_POOL", "SECOND_OPINION_OPENCODE_MODEL"),
    "copilot": ("SECOND_OPINION_COPILOT_MODEL_POOL", "SECOND_OPINION_COPILOT_MODEL"),
}


def _env_stripped(var: str, env: Mapping[str, str] | None = None) -> str:
    """Return ``var``'s value stripped of whitespace, or ``""`` if unset/blank.

    Reads from ``env`` when given (a plain dict in tests) and ``os.environ``
    otherwise, so the pool parser and this helper stay pure and testable
    without host variables leaking in.
    """
    value = os.environ.get(var) if env is None else env.get(var)
    return (value or "").strip()


def _parse_pool(var: str, env: Mapping[str, str] | None = None) -> list[str]:
    """Split ``var``'s comma-separated value into stripped, non-empty entries.

    Reads from ``env`` when given (a plain dict in tests) and ``os.environ``
    otherwise; unset/blank/comma-only values yield ``[]``.
    """
    raw = os.environ.get(var, "") if env is None else env.get(var, "")
    return [m.strip() for m in raw.split(",") if m.strip()]


def _resolve_pooled_model(
    pool_env_var: str,
    single_env_var: str,
    model_index: int | None,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Pick a model for one backend call (opencode/pi/copilot).

    Pure function, no side effects. An explicit ``model_index`` selects the
    matching ``pool_env_var`` entry for that call — even when
    ``single_env_var`` is also set (the non-interactive way to choose the pool
    over a single-model override); :func:`_validate_model_index` has already
    rejected an out-of-range or missing-pool index before this is reached, so
    the bounds guard is only a defensive no-op here. With no ``model_index``,
    ``single_env_var`` wins if set (whitespace-only counts as unset), else the
    pool's first entry, else ``None`` (the "let the backend pick" default).

    Reads from ``env`` when given (tests) and ``os.environ`` otherwise. agy
    uses :func:`_resolve_agy_model` instead, which preserves its pre-pool
    no-index default.
    """
    pool = _parse_pool(pool_env_var, env)
    if model_index is not None:
        if pool and 0 <= model_index < len(pool):
            return pool[model_index]
        return None
    single = _env_stripped(single_env_var, env)
    if single:
        return single
    if pool:
        return pool[0]
    return None


def _validate_model_index(
    backend: str, model_index: int | None, env: Mapping[str, str] | None = None
) -> str | None:
    """Return a config-error message for ``backend``'s ``model_index``, else ``None``.

    Pure: reads ``env`` (a plain dict in tests) or ``os.environ`` and never
    prints or exits — the caller ``die``s on a non-``None`` return. An
    out-of-range or missing-pool index is a configuration error, not a
    fallback trigger: it must be caught before the runner so an automatic
    selection does not silently skip to another backend.

    Empty-pool is checked before the range, so a missing pool never produces a
    ``0--1`` valid-range error. No ``model_index`` (``None``) is always valid.
    """
    if model_index is None:
        return None
    if backend not in _POOL_ENV_VARS:
        return None
    pool_var, _ = _POOL_ENV_VARS[backend]
    pool = _parse_pool(pool_var, env)
    if not pool:
        return (
            f"--model-index {model_index} requires {pool_var} for backend "
            f"{backend}; set it for pool rotation, use --backend "
            f"<configured-backend> to bypass priority, or omit --model-index"
        )
    if not (0 <= model_index < len(pool)):
        return (
            f"--model-index {model_index} out of range for {pool_var}; pool "
            f"has {len(pool)} entries (valid indexes: 0-{len(pool) - 1})"
        )
    return None


def _resolve_agy_model(model_index: int | None) -> str:
    """Pick the agy model for one call.

    No ``model_index``: ``SECOND_OPINION_AGY_MODEL`` if set, else
    :data:`DEFAULT_AGY_MODEL` — preserves the pre-pool default even when an agy
    pool is configured (an indexed pool rotation is opt-in per call). With
    ``model_index``: the entry at that index from
    ``SECOND_OPINION_AGY_MODEL_POOL``; :func:`_validate_model_index` has already
    died on a missing-pool or out-of-range index, so the guard here is only
    reached if validation is bypassed. Falls back to the single override or
    default when no pool is configured.
    """
    single = _env_stripped("SECOND_OPINION_AGY_MODEL")
    if model_index is None:
        return single or DEFAULT_AGY_MODEL
    pool = _parse_pool("SECOND_OPINION_AGY_MODEL_POOL")
    if pool:
        idx = model_index if 0 <= model_index < len(pool) else 0
        return pool[idx]
    return single or DEFAULT_AGY_MODEL


def _vprint_pool_choice(
    backend: str, model_index: int | None, *, verbose: bool
) -> None:
    """Emit the one and only --verbose notice about pool/index resolution for ``backend``.

    A manual/exploratory-CLI and --verbose-debugging convenience, not a
    safety net against a caller forgetting --model-index: it only fires
    under --verbose, so a non-verbose automated caller never sees it.
    Automated multi-round callers are documented to always pass
    --model-index every round regardless of pool state (see SKILL.md /
    second-opinion.md) rather than relying on this to catch a missed flag.
    """
    if backend not in _POOL_ENV_VARS or not verbose:
        return
    pool_var, single_var = _POOL_ENV_VARS[backend]
    if model_index is None and _env_stripped(single_var):
        return
    pool = _parse_pool(pool_var)
    if not pool:
        return
    index = model_index if model_index is not None else 0
    if not (0 <= index < len(pool)):
        return
    chosen = pool[index]
    cli_common.vprint(
        f"[second_opinion] {pool_var} -> {chosen} (index {index})", verbose=verbose
    )


CRITIQUE_PROMPT = """\
You are reviewing a plan written by another AI assistant.
Your job is to find problems, not to summarize or agree.

Respond in plain prose only: you have no tools, so never emit tool-call
markup (XML like <tool_calls>, JSON tool-call blocks, or fenced tool
invocations) — if you could not check something, say so in the critique.

Be specific and concrete:
- What could go wrong or is underspecified?
- What did the author miss or assume without justification?
- Where do you disagree, and why?
- Is there a simpler approach?
{focus_section}
If the plan is genuinely solid, say so briefly — but don't pad
agreement with praise. Skip preamble.

---
{plan_text}
"""

FOCUS_SECTION = """
The plan's author flagged these as this plan's specific risk points —
scrutinize them closely, but don't let them limit the rest of your review:
{hints}
"""


def build_prompt(plan_text: str, focus_hints: str | None) -> str:
    """Build the critique prompt, optionally inserting plan-specific focus hints.

    Args:
        plan_text: The plan to review.
        focus_hints: Bullet-point text naming risk areas specific to this
            plan, or ``None``/blank to omit the section entirely.

    Returns:
        The fully-formatted critique prompt.
    """
    focus_section = (
        FOCUS_SECTION.format(hints=focus_hints.strip())
        if focus_hints and focus_hints.strip()
        else ""
    )
    return CRITIQUE_PROMPT.format(plan_text=plan_text, focus_section=focus_section)


def die(msg: str) -> NoReturn:
    """Print an error to stderr, prefixed for this script, and exit with status 1."""
    print(f"[second_opinion] {msg}", file=sys.stderr)
    sys.exit(1)


def resolve_plan_text(arg: str) -> str:
    """Resolve a CLI argument to plan text: a file's contents, or the arg itself.

    Args:
        arg: A filesystem path or inline plan text.

    Returns:
        The file's contents if ``arg`` names an existing file, otherwise
        ``arg`` unchanged (treated as inline text).
    """
    try:
        path = Path(arg).expanduser()
        if path.is_file():
            return path.read_text()
    except OSError:
        pass  # arg is inline text too long/invalid to be a filesystem path
    return arg


def _kill_active_process() -> None:
    """Kill the currently-running backend subprocess's entire process group, if any."""
    llm_backends._kill_active_process()


def _handle_termination(signum: int, frame: FrameType | None) -> NoReturn:
    """Signal handler: kill any active backend subprocess, then exit.

    Registered for SIGTERM/SIGINT so an interrupted `review` doesn't leave
    an orphaned backend process running in the background.
    """
    _kill_active_process()
    sys.exit(128 + signum)


def _run_command(
    cmd: list[str], timeout: int | None = None, *, retries: int = 0
) -> tuple[int, str, str]:
    """Run ``cmd`` as a subprocess, capturing its output.

    Thin wrapper around :func:`llm_backends._run_command` — kept local (rather
    than a bare re-export) so :data:`BACKEND_TIMEOUT_SECONDS` is read from
    *this* module's global at call time, matching pre-extraction behavior for
    anything that patches it. ``timeout`` defaults to
    :data:`BACKEND_TIMEOUT_SECONDS` when not given (``None``); callers with a
    resolved per-backend override pass their own value. ``retries`` is
    passed straight through to :func:`llm_backends._run_command` — see its
    docstring.
    """
    return llm_backends._run_command(
        cmd,
        timeout if timeout is not None else BACKEND_TIMEOUT_SECONDS,
        retries=retries,
    )


DEFAULT_AGY_MODEL = "Gemini 3.7 Flash (High)"


def run_agy(prompt: str, *, model_index: int | None = None) -> str:
    """Run the ``agy`` backend and return its critique text.

    The model is resolved via :func:`_resolve_agy_model`: without
    ``--model-index``, ``SECOND_OPINION_AGY_MODEL`` if set, else
    :data:`DEFAULT_AGY_MODEL` (a configured agy pool does not change the
    default); with ``--model-index``, the indexed entry from
    ``SECOND_OPINION_AGY_MODEL_POOL`` (validated by
    :func:`_validate_model_index`, which dies on a missing pool or out-of-range
    index). ``model_index`` is no longer ignored — agy now shares the
    indexed-pool contract.
    """
    model = _resolve_agy_model(model_index)
    return llm_backends.run_agy(
        prompt,
        model=model,
        timeout=_resolve_timeout("SECOND_OPINION_AGY_TIMEOUT_SECONDS"),
    )


def run_opencode(prompt: str, *, model_index: int | None = None) -> str:
    """Run the ``opencode`` backend's adversary agent and return its critique text.

    Not routed through :func:`llm_backends.run_opencode` (the generic
    variant): the adversary agent is second_opinion-specific (``--agent
    adversary``), so this builds its own command but reuses the shared
    event-parsing helpers.

    The model is resolved via :func:`_resolve_pooled_model`:
    ``SECOND_OPINION_OPENCODE_MODEL``, if set, wins outright; otherwise
    ``model_index`` picks an entry from ``SECOND_OPINION_OPENCODE_MODEL_POOL``
    if that's set; otherwise unset/empty means the live opencode config's
    model.

    Raises:
        BackendError: If the event stream contains a ``tool_use`` event — the
            adversary agent must be stateless and text-only, so a swapped-in
            model taking real shell/file actions is a hard failure, not a
            silently swallowed event — if the returned text is dominated by
            leaked tool-call markup (a tool-starved model's denied tool
            invocation leaking through as text instead of a real ``tool_use``
            event), or if it has no text chunks: either an explicit error
            event was emitted, or nothing recognizable was produced at all.

            Some models (e.g. gpt-5.6-luna, as of 2026-08) fail this last way
            unconditionally under this agent's ``permission: deny`` — reliably
            reproduced even with a prompt that never attempts a tool call, and
            reproduced identically under a bare-string ``deny`` and a
            granular per-capability ``deny`` object, while the same model
            works fine on the default (unrestricted) agent. That points to an
            opencode/provider-side bug specific to restricted permission on
            this model, not a "tool-hungry model" trait — but the practical
            fix is the same either way: pin a model that tolerates this
            agent's restriction via ``SECOND_OPINION_OPENCODE_MODEL``.

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
    model = _resolve_pooled_model(
        "SECOND_OPINION_OPENCODE_MODEL_POOL",
        "SECOND_OPINION_OPENCODE_MODEL",
        model_index,
    )
    # Built by llm_backends, not here. This function used to assemble its own
    # argv, which is how it ended up as the only invocation path in the repo
    # carrying the adversary agent while every other one ran unisolated.
    cmd = llm_backends.build_isolated_command("opencode", prompt, model=model)
    _, stdout, stderr = _run_command(
        cmd,
        timeout=_resolve_timeout("SECOND_OPINION_OPENCODE_TIMEOUT_SECONDS"),
        retries=1,
    )
    events = _opencode_json_events(stdout)
    _raise_on_tool_use(events, context="adversary agent")
    chunks = _opencode_text_chunks(events)
    if chunks:
        return _finalize_text_response(chunks, context="adversary agent")
    for e in events:
        if e.get("type") == "error":
            message = _safe_get(e, "error", "data", "message")
            raise BackendError(f"adversary agent error: {message or e.get('error')}")
    raise BackendError(f"no text output: {stderr.strip() or stdout.strip()[:200]}")


def run_copilot(prompt: str, *, model_index: int | None = None) -> str:
    """Run the ``copilot`` backend and return its critique text.

    The model is resolved via :func:`_resolve_pooled_model`:
    ``SECOND_OPINION_COPILOT_MODEL``, if set, wins outright; otherwise
    ``model_index`` picks an entry from ``SECOND_OPINION_COPILOT_MODEL_POOL``
    if that's set; otherwise ``None`` (no ``--model`` flag — see
    :func:`llm_backends.run_copilot` for why that's the safe default).
    """
    return llm_backends.run_copilot(
        prompt,
        model=_resolve_pooled_model(
            "SECOND_OPINION_COPILOT_MODEL_POOL",
            "SECOND_OPINION_COPILOT_MODEL",
            model_index,
        ),
        timeout=_resolve_timeout("SECOND_OPINION_COPILOT_TIMEOUT_SECONDS"),
    )


def run_pi(prompt: str, *, model_index: int | None = None) -> str:
    """Run the ``pi`` backend and return its critique text.

    The model is resolved via :func:`_resolve_pooled_model`:
    ``SECOND_OPINION_PI_MODEL``, if set, wins outright; otherwise
    ``model_index`` picks an entry from ``SECOND_OPINION_PI_MODEL_POOL`` if
    that's set; otherwise ``None`` (Pi's own default model choice for the
    ``opencode-go`` provider — see :func:`llm_backends.run_pi`).
    """
    return llm_backends.run_pi(
        prompt,
        model=_resolve_pooled_model(
            "SECOND_OPINION_PI_MODEL_POOL", "SECOND_OPINION_PI_MODEL", model_index
        ),
        timeout=_resolve_timeout("SECOND_OPINION_PI_TIMEOUT_SECONDS"),
    )


BACKEND_RUNNERS = {
    "agy": run_agy,
    "pi": run_pi,
    "opencode": run_opencode,
    "copilot": run_copilot,
}
BACKEND_LABELS = {
    "agy": f"agy ({DEFAULT_AGY_MODEL})",
    "pi": "pi (opencode-go gateway)",
    "opencode": "opencode (adversary agent)",
    "copilot": "GitHub Copilot CLI",
}


def backend_label(backend: str, *, model_index: int | None = None) -> str:
    """Return ``backend``'s display label, appending the resolved model if any.

    For ``agy``, resolution goes through :func:`_resolve_agy_model`; for
    ``opencode``/``pi``/``copilot`` it goes through the same
    :func:`_resolve_pooled_model` the runner uses (single-override env var
    when no index, else pool + ``model_index``) — one source of truth, so
    the printed label always matches the model the runner actually used,
    pool or not.
    """
    label = BACKEND_LABELS[backend]
    if backend == "agy":
        model = _resolve_agy_model(model_index)
        if model != DEFAULT_AGY_MODEL:
            return f"agy ({model})"
        return label
    if backend == "copilot":
        model = _resolve_pooled_model(
            "SECOND_OPINION_COPILOT_MODEL_POOL",
            "SECOND_OPINION_COPILOT_MODEL",
            model_index,
        )
        if model:
            return f"{label} ({model})"
    if backend == "opencode":
        model = _resolve_pooled_model(
            "SECOND_OPINION_OPENCODE_MODEL_POOL",
            "SECOND_OPINION_OPENCODE_MODEL",
            model_index,
        )
        if model:
            return f"{label} ({model})"
    if backend == "pi":
        model = _resolve_pooled_model(
            "SECOND_OPINION_PI_MODEL_POOL", "SECOND_OPINION_PI_MODEL", model_index
        )
        if model:
            return f"{label} ({model})"
    return label


def cmd_detect(args: argparse.Namespace) -> None:
    """Handle ``detect``: print backend presence AND contract eligibility.

    Presence alone was misleading: an installed backend that cannot meet the
    isolation contract is never used, so reporting it as simply available
    invited the discovery to happen at the moment a critique was wanted rather
    than before. Each entry carries ``present``, ``eligible`` and, when
    ineligible, the ``reason``.

    The legacy boolean shape is preserved under each backend's ``present``
    key so existing callers keep a one-line migration.
    """
    print(json.dumps(llm_backends.eligibility_report(), indent=2))


def cmd_review(args: argparse.Namespace) -> None:
    """Handle ``review``: get one critique from the priority-selected backend.

    Tries each candidate backend in priority order (or just the forced
    ``--backend``, if given) until one succeeds; prints the first
    successful critique and returns. Exits nonzero only if every candidate
    fails.
    """
    if args.backend:
        if not shutil.which(args.backend):
            die(f"{args.backend} not found on PATH")
        candidates = [args.backend]
    else:
        candidates = available_backends()
        if not candidates:
            die("no backend available — install one of: " + ", ".join(BACKEND_PRIORITY))

    plan_text = resolve_plan_text(args.plan)
    focus_hints = None
    if args.focus_file:
        focus_path = Path(args.focus_file).expanduser()
        if not focus_path.is_file():
            die(f"--focus-file not found: {focus_path}")
        focus_hints = focus_path.read_text()
    prompt = build_prompt(plan_text, focus_hints)

    model_index = getattr(args, "model_index", None)
    verbose = getattr(args, "verbose", False)
    failures = []
    for backend in candidates:
        _vprint_pool_choice(backend, model_index, verbose=verbose)
        if model_index is not None:
            err = _validate_model_index(backend, model_index, os.environ)
            if err:
                die(err)
        try:
            critique = BACKEND_RUNNERS[backend](prompt, model_index=model_index)
        except BackendError as exc:
            cli_common.vprint(
                f"[second_opinion] {backend_label(backend, model_index=model_index)} "
                f"failed: {exc}",
                verbose=verbose,
            )
            failures.append(f"{backend}: {exc}")
            continue
        print(f"Second opinion via {backend_label(backend, model_index=model_index)}:")
        print(critique)
        return

    die("all backends failed — " + "; ".join(failures))


def _non_negative_int(value: str) -> int:
    """argparse ``type=`` for ``--model-index``: reject negatives with a clean error.

    A plain ``type=int`` would accept ``-1`` (this parser has no option
    strings that look like negative numbers, so argparse's own heuristic
    lets ``-1`` through as a value rather than an unrecognized flag) and
    hand it to :func:`_resolve_pooled_model`'s modulo, silently wrapping
    instead of rejecting. There's no legitimate rotation use case for a
    negative index, so reject it here where argparse reports it cleanly.
    """
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError(f"must be non-negative, got {parsed}")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    """Build the full argument parser for every subcommand.

    Extracted from :func:`main` so tests can exercise parsing (e.g.
    --model-index validation) without going through sys.argv.
    """
    parser = argparse.ArgumentParser(
        description="one-shot adversarial critique of a plan from a non-Claude backend",
    )
    # --quiet/-v are defined once, on every leaf subcommand parser only
    # (via this shared `parents=` parser) -- never on `parser` itself. See
    # dev_status.py's build_parser() for the full rationale.
    verbosity_parent = argparse.ArgumentParser(add_help=False)
    cli_common.add_verbosity_args(verbosity_parent)
    sub = parser.add_subparsers(dest="cmd", metavar="{detect,review}")

    sub.add_parser(
        "detect", help="list available backends as JSON", parents=[verbosity_parent]
    )

    p = sub.add_parser(
        "review",
        help="get one critique from the priority-selected backend",
        parents=[verbosity_parent],
    )
    p.add_argument("plan", metavar="<plan-file-or-text>")
    p.add_argument(
        "--backend",
        choices=BACKEND_PRIORITY,
        default=None,
        help="force this backend instead of priority-order fallback",
    )
    p.add_argument(
        "--focus-file",
        default=None,
        help="path to a file of plan-specific risk hints, appended to the "
        "critique prompt as areas to scrutinize (supplements, not replaces, "
        "the generic adversarial mandate)",
    )
    p.add_argument(
        "--model-index",
        type=_non_negative_int,
        default=None,
        metavar="N",
        help="0-based index into the backend model pool "
        "(SECOND_OPINION_{AGY,PI,OPENCODE,COPILOT}_MODEL_POOL) for this call "
        "-- round 1 of a rotation is index 0, round 2 is index 1, etc. "
        "Supported for agy/pi/opencode/copilot; an explicit index selects the "
        "pool even when a single-model override is set, and is a hard error "
        "if the pool is unset/empty or the index is out of range (was "
        "previously a silent no-op/fallback).",
    )

    return parser


def ensure_data_dir() -> None:
    """Create ``DATA_DIR`` if it is missing.

    This script only reads from the directory — a ``--focus-file`` an agent
    wrote. It creates it anyway because it is one of the two entry points
    that own the directory, so agents can rely on it existing after either
    one runs instead of running ``mkdir -p`` first.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def main() -> None:
    """Register termination handlers, parse argv, and dispatch to a subcommand."""
    ensure_data_dir()
    signal.signal(signal.SIGTERM, _handle_termination)
    signal.signal(signal.SIGINT, _handle_termination)

    parser = build_parser()
    args = parser.parse_args()

    dispatch = {"detect": cmd_detect, "review": cmd_review}
    if args.cmd in dispatch:
        dispatch[args.cmd](args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
