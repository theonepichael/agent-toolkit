#!/usr/bin/env python3
"""SessionStart hook + CLI: detect (and optionally fix) drift between the
live ``~/.claude/settings.json`` / ``~/.config/opencode/opencode.jsonc`` /
(under WSL) the Windows-side VS Code ``settings.json`` and
``keybindings.json`` and their seeds in the dotfiles repo.

Why this exists
---------------
install.py's seed_file contract is copy-once: settings.json and
opencode.jsonc are written the first time install.py runs on a machine,
then never overwritten — because Claude Code and opencode both rewrite
these files in place as permissions get approved live, which would replace
a symlink with a plain file and silently detach the seed. install.py
already has helpers (``describe_settings_drift``, ``describe_opencode_drift``,
``opencode_bypass_drift``, ``json_key_drift``, ``_load_json_pair``) to report
divergence — but they only run when install.py is manually re-executed.
Nothing runs them automatically, so a live settings.json can silently drift
from the repo seed for days (this happened 2026-07-24 -> 2026-07-30). This
script closes that gap: a SessionStart hook prints a one-liner per drifted
file at the start of every Claude Code session.

What counts as drift
--------------------
A *denylist* of cosmetic keys is silently allowed to differ (per-machine
preferences). Everything else is reported as drift by default, so a
future security-relevant key Claude Code ships (``autoCompact``, ``sandbox``,
the already-shipped ``skipDangerousModePermissionPrompt``) is caught on the
first session after the schema change rather than silently ignored —
forcing a human to either silence it (add to cosmetics) or investigate.

    settings.json   cosmetic: theme, model, voice, voiceEnabled, agent,
                    tui, statusLine, autoMode
                    reported: everything else (permissions, hooks, and any
                    unknown future top-level key)
    opencode.jsonc  cosmetic: $schema, agent
                    reported: everything else (permission, and any unknown
                    future top-level key); an ``xargs *`` / ``awk *``
                    bypass under ``permission.bash`` is surfaced verbatim
                    as a ``SECURITY:`` line, exactly as install.py prints
                    it.
    VS Code         no cosmetic split — every setting/keybinding is a
    settings.json / legitimate user edit, not a security-relevant key.
    keybindings.json Drift is a plain whole-file text comparison (not a
                    JSON key diff): these are legal JSONC (comments,
                    trailing commas), which ``json.loads`` can't always
                    parse, so text equality is the only signal that can't
                    misreport a commented file as drift-free. Only checked
                    under WSL, when a Windows-side ``code`` CLI is found on
                    PATH (see ``_vscode_wsl_user_dir``).

Why the helpers are vendored in-script, not imported from install.py
-------------------------------------------------------------------
A SessionStart hook is leaf infrastructure; importing a 1800-line
installer to reuse ~30 lines of pure drift helpers trades trivial
one-time duplication for permanent coupling — and an import failure
(any ``ImportError`` propagating from a future edit to install.py)
would silently no-op the whole hook, disconnecting the smoke detector
from its battery. The helpers below are vendored from install.py
(see the ``# vendored from install.py`` headers); keep them in sync if
install.py's drifts.

Subcommands
-----------
    check   print a one-liner per drifted file (default; the SessionStart
            hook uses this). Silent when there's nothing to report.
            Loud-fails (prints to stdout AND exits nonzero) when a file
            should be parseable but isn't — e.g. opencode.jsonc grew a
            ``//`` comment that ``json.loads`` can't read. Never pretends
            "no drift" on a parse failure. VS Code drift never loud-fails
            this way (there's no strict JSON loader for it — see above);
            its message is direction-neutral — ``check`` can't know
            algorithmically which side of a whole-file VS Code diff is
            "correct", so it names both ``push-vscode`` (repo->live) and
            ``sync-to-seed`` (live->repo) rather than recommending one by
            default.
    fix     additively merge drifted keys from the seed into the live
            file, preserving all live-only entries (which are almost
            certainly live approvals). Does NOT cover VS Code — VS Code
            drift is a whole-file text diff (see "What counts as drift"
            above), not a per-key JSON merge, so it isn't a fit for this
            additive-merge model in either direction. See ``push-vscode``
            below for the repo->live direction and ``sync-to-seed`` for
            live->repo:

            * ``permissions.allow``: union — append seed entries not in
              live; never remove live entries.
            * ``permissions.deny`` / ``permissions.ask``: overwrite from
              seed only when live is a *weakened subset* (live has fewer
              entries than the seed — a security regression); otherwise
              preserve live.
            * ``permission.bash`` (opencode): union of seed patterns into
              live; never remove live patterns — **except** the
              ``xargs *`` / ``awk *`` allowlist bypasses, which are
              stripped from live even under additive policy.
            * ``hooks``: wholesale-overwrite from seed (hooks are not
              live-modified by approval flows, so any divergence is
              genuine drift).
            * Any key that exists in both but differs in a way that isn't
              one of the above is *flag-and-skip*: printed, not touched.

            Backs up the prior live file to
            ``<path>.bak.YYYYmmdd-HHMMSS`` first; writes atomically
            (tmp + ``os.replace`` in the same dir, defense in depth
            against torn reads). Refuses to run while any Claude Code
            session is active — Claude Code holds settings in memory
            for the session's lifetime and serializes its in-memory state
            on every approval, so a mid-session fix gets silently
            clobbered on the next approval and SessionStart never re-fires
            (the session never restarted). Close your Claude Code
            sessions, then run ``fix``.
    sync-to-seed
            the reverse direction: mirror live settings back into the
            dotfiles seed. For a live-first edit (e.g. testing a
            SessionStart hook fix immediately in
            ``~/.claude/settings.json``) that needs forwarding into git.

            * ``hooks``: wholesale-mirrored live->seed (same rationale as
              fix()'s seed->live direction — hooks aren't live-modified by
              approval flows, so any divergence is genuine drift).
            * ``permissions`` / opencode's ``permission.*``: never
              written, only reported — live approvals are often
              profile-specific, so a live-only entry isn't necessarily
              drift to repair.
            * VS Code's ``settings.json`` / ``keybindings.json``: the
              live->repo direction of VS Code drift repair (``push-vscode``
              below is the repo->live direction; ``fix`` covers neither —
              see above). Whole-file text copy, not a JSON round-trip, so
              it can't fail on native JSONC and can't silently reformat
              the user's file.

            No active-session guard needed (unlike ``fix``) — this only
            writes the seed file, never the live files Claude Code/
            opencode hold in memory. Same backup-then-atomic-write pattern
            as ``fix``. Accepts ``--dotfiles-root PATH`` to target a
            worktree instead of the live checkout (the default, resolved
            from this script's own location) — point it at a fresh worktree
            per the dotfiles-first git policy rather than writing directly
            into the main checkout.
    push-vscode
            the repo->live direction for VS Code: push the dotfiles seed's
            ``settings.json`` / ``keybindings.json`` out to the live
            Windows user directory. WSL-only (see ``_vscode_wsl_user_dir``);
            refuses with an explicit error off WSL rather than a silent
            no-op, since this is a single-purpose command, not a multi-file
            "check everything" command like ``sync-to-seed`` where skipping
            VS Code off-WSL is normal.

            For each file with a diff, prints it and asks for confirmation
            (``--yes``/``-y`` to skip the prompt; refuses on non-interactive
            stdin without ``--yes``). Confirmations for both files are
            collected before anything is written. Only once something is
            confirmed does it check whether native Windows VS Code
            (``Code.exe``) is currently running, via ``tasklist.exe`` —
            checked as late as possible, immediately before the write, to
            keep the human-response pause during confirmation out of the
            TOCTOU window. If VS Code is detected, refuses the whole push
            (no partial writes — safe because nothing was written before
            this check). The process check is best-effort (assumes "not
            running" if ``tasklist.exe`` is missing, times out, or errors)
            and backup-protected, not a guarantee: each live file gets a
            timestamped ``.bak`` before being overwritten, same convention
            as ``fix``/``sync-to-seed``. Accepts ``--dotfiles-root PATH``,
            same as ``sync-to-seed``.

Usage
-----
    settings_seed_drift_check.py           # check (default)
    settings_seed_drift_check.py check
    settings_seed_drift_check.py fix
    settings_seed_drift_check.py sync-to-seed [--dotfiles-root PATH]
    settings_seed_drift_check.py push-vscode [--dotfiles-root PATH] [--yes]

Exits 0 from ``check`` when there is no drift and no parse failure;
exits nonzero from ``check`` on a parse failure (so the user notices),
but never blocks the session otherwise. ``fix`` exits 1 when it refused
to run (active sessions) or hit a parse failure, 0 otherwise.
``sync-to-seed`` exits 1 on a parse failure or a ``--dotfiles-root`` that
doesn't exist, 0 otherwise. ``push-vscode`` exits 1 when it refused to run
(off WSL, VS Code detected as running, or confirmation required on
non-interactive stdin without ``--yes``), 0 otherwise (including "nothing
to push").

Flags
  --quiet, -q    suppress non-essential output
  --verbose, -v  emit extra diagnostic messages to stderr
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from contextlib import suppress
from datetime import datetime
from pathlib import Path

import cli_common

HOME = Path.home()
DOTFILES = Path(__file__).resolve().parents[2]

# Cosmetic keys — per-machine preferences explicitly allowed to drift
# silently. Anything NOT in this set is reported as drift by default, so a
# future security-relevant key Claude Code ships is caught on the first
# session after the schema change rather than silently ignored. (The prior
# allowlist design silently missed ``skipDangerousModePermissionPrompt``,
# which is security-relevant and shipped today.)
SETTINGS_COSMETIC_KEYS: frozenset[str] = frozenset(
    {
        "theme",
        "model",
        "voice",
        "voiceEnabled",
        "agent",
        "tui",
        "statusLine",
        "agentPushNotifEnabled",
        "effortLevel",
        "enabledPlugins",
        "autoMode",
    }
)
OPENCODE_COSMETIC_KEYS: frozenset[str] = frozenset({"$schema", "agent"})

# opencode allowlist-bypass patterns — these invoke an arbitrary other
# command as their own argument (awk via system()), so their presence
# defeats the allowlist entirely. They're stripped from live even under
# the otherwise-additive fix policy: a security regression takes priority
# over preserving live-only entries.
OPENCODE_BYPASS_PATTERNS: tuple[str, ...] = ("xargs *", "awk *")

PROFILE_MARKER = HOME / ".local" / "state" / "dotfiles" / "profile"


class DriftCheckError(Exception):
    """Raised when drift checking can't proceed (parse failure, not a
    missing file). Missing files are not errors — there is simply nothing
    to compare against."""


# ── vendored from install.py — keep in sync if it drifts ─────────────────────
# These are lifted from install.py:1173-1226 so this leaf SessionStart hook
# doesn't need to import an 1800-line installer (an import failure there
# would silently no-op the whole hook). They are pure functions with no
# install side effects.


def json_key_drift(seed: dict[str, object], live: dict[str, object]) -> list[str]:
    """Return the top-level keys whose values differ between seed and live."""
    return sorted(k for k in set(seed) | set(live) if seed.get(k) != live.get(k))


def _bash_permissions(config: dict[str, object]) -> dict[str, object]:
    """Return ``permission.bash`` from an opencode config, or ``{}``."""
    permission = config.get("permission")
    if not isinstance(permission, dict):
        return {}
    bash = permission.get("bash")
    return bash if isinstance(bash, dict) else {}


def opencode_bypass_drift(
    seed: dict[str, object], live: dict[str, object]
) -> list[str]:
    """Return allowlist-bypass bash patterns present live but not in the seed.

    ``xargs`` and ``awk`` each invoke an arbitrary other command as their
    own argument (awk via ``system()``), so their presence isn't
    "individually risky command a profile could allow" — it defeats the
    allowlist entirely. They're called out separately from the generic
    top-level key diff because they live nested under ``permission.bash``,
    where a top-level diff would only say "permission" changed without
    saying which pattern came back.
    """
    seed_bash = _bash_permissions(seed)
    live_bash = _bash_permissions(live)
    return [
        k for k in OPENCODE_BYPASS_PATTERNS if k in live_bash and k not in seed_bash
    ]


def _vscode_wsl_user_dir() -> Path | None:
    """Locate the Windows-side VS Code user directory from WSL.

    Under WSL, VS Code is normally driven from the Windows GUI via the
    Remote-WSL extension, so the real user settings.json lives in the
    Windows user profile, not the WSL filesystem. The profile directory is
    derived from the Windows-side ``code`` shim's own path (inherited onto
    PATH via WSL interop) rather than hardcoding a username.

    Returns:
        The ``.../AppData/Roaming/Code/User`` directory, or None if no
        Windows-side ``code`` CLI is on PATH.
    """
    code_bin = shutil.which("code")
    if not code_bin:
        return None
    parts = Path(code_bin).parts
    if "AppData" not in parts or not code_bin.startswith("/mnt/"):
        return None
    win_user_dir = Path(*parts[: parts.index("AppData")])
    if "Users" not in parts:
        return None
    return win_user_dir / "AppData" / "Roaming" / "Code" / "User"


# ── end vendored block ──────────────────────────────────────────────────────


# ── JSON loading: loud-fail on parse failure, silent on missing file ────────


def _load_json_strict(path: Path) -> dict[str, object]:
    """Read and parse a JSON object from ``path``.

    Raise :class:`DriftCheckError` on a parse failure (the file exists but
    isn't valid JSON — e.g. opencode.jsonc grew a ``//`` comment that
    ``json.loads`` can't read) or on any non-missing read error. Re-raise
    :class:`FileNotFoundError` unchanged so callers can tell "file is
    missing, nothing to compare" apart from "file is present but broken".
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise DriftCheckError(f"cannot read {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise DriftCheckError(
            f"cannot parse {path} as JSON — {exc}. If this is opencode.jsonc "
            "and has // comments, json.loads can't read JSONC; remove the "
            "comments or install a JSONC parser."
        ) from exc
    if not isinstance(data, dict):
        raise DriftCheckError(
            f"{path} parsed to {type(data).__name__}, expected a JSON object"
        )
    return data


def _load_json_pair(
    seed: Path, live: Path
) -> tuple[dict[str, object], dict[str, object]] | None:
    """Load a (seed, live) JSON pair.

    Return ``None`` if either file is *missing* (there is simply nothing
    to compare against — the caller stays silent). Raise
    :class:`DriftCheckError` if either file is present but unparseable —
    a smoke detector that silently disconnects its own battery on a parse
    failure is the failure mode to avoid here.
    """
    try:
        seed_data = _load_json_strict(seed)
    except FileNotFoundError:
        return None
    try:
        live_data = _load_json_strict(live)
    except FileNotFoundError:
        return None
    return seed_data, live_data


# ── profile resolution, seed paths ───────────────────────────────────────────


def resolve_profile() -> str:
    """Return "work" if this machine is work-provisioned, else "personal"."""
    try:
        return "work" if PROFILE_MARKER.read_text().strip() == "work" else "personal"
    except OSError:
        return "personal"


def settings_seed_path(root: Path | None = None) -> Path:
    """Return the seed settings.json path for this machine's profile,
    under ``root`` (default the ``DOTFILES`` module constant — resolved at
    call time, not bound at import, so callers that don't pass ``root``
    still pick up a patched/overridden ``DOTFILES``)."""
    name = "settings.work.json" if resolve_profile() == "work" else "settings.json"
    return (root if root is not None else DOTFILES) / "claude" / name


def opencode_seed_path(root: Path | None = None) -> Path | None:
    """Return the opencode.jsonc seed path under ``root``, or None on a
    work machine.

    opencode is never installed on a work machine (install.py rejects
    --profile=work --harness=opencode outright), so there's no seed to
    compare against there.
    """
    if resolve_profile() == "work":
        return None
    return (root if root is not None else DOTFILES) / "opencode" / "opencode.jsonc"


def vscode_seed_path(name: str, root: Path | None = None) -> Path:
    """Return the seed path for a VS Code file (``settings.json`` or
    ``keybindings.json``) under ``root``.

    No work/personal split like ``settings_seed_path`` — VS Code settings
    aren't profile-specific the way Claude Code's are.
    """
    return (root if root is not None else DOTFILES) / "vscode" / name


# ── drift detection ──────────────────────────────────────────────────────────


def _non_cosmetic_drift(
    seed: dict[str, object], live: dict[str, object], cosmetics: frozenset[str]
) -> list[str]:
    """Return non-cosmetic top-level keys whose values differ between
    seed and live. Everything in ``cosmetics`` is silently permitted to
    drift; everything else is reported."""
    keys = (set(seed) | set(live)) - cosmetics
    return sorted(k for k in keys if seed.get(k) != live.get(k))


def settings_drift(seed: Path, live: Path) -> list[str]:
    """Return the non-cosmetic settings.json keys that diverged, or [] if
    either file is missing. Raise on a parse failure."""
    pair = _load_json_pair(seed, live)
    if pair is None:
        return []
    return _non_cosmetic_drift(*pair, SETTINGS_COSMETIC_KEYS)


def opencode_drift(seed: Path, live: Path) -> str:
    """Return a drift description for opencode.jsonc non-cosmetic keys, or "".

    A SECURITY bypass (xargs/awk) outranks a plain permission-key diff and
    is returned verbatim with the ``SECURITY:`` prefix, matching
    install.describe_opencode_drift. Raise on a parse failure.
    """
    pair = _load_json_pair(seed, live)
    if pair is None:
        return ""
    bypasses = opencode_bypass_drift(*pair)
    if bypasses:
        return (
            f"SECURITY: {', '.join(bypasses)} still allowed in your live "
            "opencode.jsonc (allowlist bypass) — run "
            "`python3 ~/.claude/scripts/settings_seed_drift_check.py fix` to repair"
        )
    drifted = _non_cosmetic_drift(*pair, OPENCODE_COSMETIC_KEYS)
    return ", ".join(drifted)


def _try_parse_json(path: Path) -> object | None:
    """Best-effort JSON parse of ``path``. Returns None on any read or
    parse failure — never raises. Used only to enrich a drift message,
    never to decide whether drift exists, so a commented (JSONC) VS Code
    file doesn't loud-fail the way ``_load_json_strict`` would."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def vscode_drift(seed: Path, live: Path) -> str:
    """Describe how a live VS Code settings.json/keybindings.json diverged
    from its seed, or "" if there's nothing to compare or nothing drifted.

    Text equality is the definitive drift signal, not JSON equality — same
    rationale as install.describe_vscode_drift: VS Code's live files are
    legal JSONC (comments, trailing commas) that ``json.loads`` can't
    parse, so a JSON-first check would miss real drift on a merely
    commented file. ``_try_parse_json`` only runs after text drift is
    already confirmed, purely to enrich the message. Never raises — there
    is no loud-fail path for VS Code drift (see module docstring).
    """
    if not seed.is_file() or not live.is_file():
        return ""
    seed_text = seed.read_text(encoding="utf-8")
    live_text = live.read_text(encoding="utf-8")
    if seed_text == live_text:
        return ""

    seed_data = _try_parse_json(seed)
    live_data = _try_parse_json(live)
    if isinstance(seed_data, dict) and isinstance(live_data, dict):
        return ", ".join(json_key_drift(seed_data, live_data))
    if isinstance(seed_data, list) and isinstance(live_data, list):
        if len(seed_data) != len(live_data):
            return f"{len(live_data)} bindings live vs {len(seed_data)} in seed"
        return f"binding definitions differ ({len(live_data)} bindings)"
    return "content differs from the repo copy"


def _print_loud(msg: str) -> None:
    """Print to both stdout and stderr so the message survives both the
    SessionStart hook's ``2>/dev/null`` (which only suppresses stderr) and
    manual piping of stdout elsewhere."""
    print(msg)
    print(msg, file=sys.stderr)


def cmd_check(quiet: bool = False) -> int:
    """Print a one-liner per drifted file; silent when nothing drifted.
    Loud-fail (nonzero exit) on a parse failure."""
    messages: list[str] = []
    exit_code = 0

    settings_live = HOME / ".claude" / "settings.json"
    settings_seed = settings_seed_path()
    try:
        drifted = settings_drift(settings_seed, settings_live)
    except DriftCheckError as exc:
        messages.append(
            f"[settings_seed_drift_check] cannot check settings drift: {exc}"
        )
        exit_code = 1
    else:
        if drifted:
            messages.append(
                f"settings.json drifted from seed ({settings_seed.name}) on: "
                f"{', '.join(drifted)} — "
                "run `python3 ~/.claude/scripts/settings_seed_drift_check.py fix`"
            )

    oc_seed = opencode_seed_path()
    if oc_seed is not None:
        oc_live = HOME / ".config" / "opencode" / "opencode.jsonc"
        try:
            oc_drifted = opencode_drift(oc_seed, oc_live)
        except DriftCheckError as exc:
            messages.append(
                f"[settings_seed_drift_check] cannot check opencode drift: {exc}"
            )
            exit_code = 1
        else:
            if oc_drifted:
                messages.append(oc_drifted)

    vscode_user_dir = _vscode_wsl_user_dir()
    if vscode_user_dir is not None:
        for name in ("settings.json", "keybindings.json"):
            seed = vscode_seed_path(name)
            live = vscode_user_dir / name
            drift = vscode_drift(seed, live)
            if drift:
                messages.append(
                    f"{name} (VS Code) drifted from seed ({seed.name}): {drift} — "
                    "run `python3 ~/.claude/scripts/settings_seed_drift_check.py "
                    "push-vscode` to push the repo's version to Windows, or "
                    "`sync-to-seed` to pull the Windows version into the repo, "
                    "depending on which side has the change you want to keep"
                )

    if messages:
        cli_common.qprint("\n".join(messages), quiet=quiet)
    return exit_code


# ── fix ───────────────────────────────────────────────────────────────────────


def _merge_permissions_additive(
    seed_perm: dict[str, object], live_perm: dict[str, object]
) -> tuple[dict[str, object], list[str], list[str]]:
    """Additively merge seed permissions into live.

    Return ``(new_perm, applied, skipped)`` where ``applied`` lists the
    changes that will be written and ``skipped`` lists the differing keys
    that were intentionally left untouched (flag-and-skip).

    * ``allow``: union — append seed entries not in live; never remove.
    * ``deny`` / ``ask``: overwrite from seed only when live is a
      *weakened subset* (live has fewer entries than seed — a security
      regression); otherwise preserve live.
    * Other ``permissions`` sub-keys: append from seed if missing in live;
      flag-and-skip if present in both but differ.
    """
    new: dict[str, object] = dict(live_perm)
    applied: list[str] = []
    skipped: list[str] = []

    # allow: additive union
    seed_allow = seed_perm.get("allow", [])
    live_allow = new.get("allow", [])
    if isinstance(seed_allow, list) and isinstance(live_allow, list):
        missing = [e for e in seed_allow if e not in live_allow]
        if missing:
            new["allow"] = list(live_allow) + missing
            applied.append(
                "permissions.allow +[" + ", ".join(repr(e) for e in missing) + "]"
            )
    elif "allow" in seed_perm and seed_perm.get("allow") != live_allow:
        skipped.append("permissions.allow differs — leaving live")

    # deny, ask: wholesale overwrite only when live is a weakened subset
    for sub in ("deny", "ask"):
        seed_v = seed_perm.get(sub, [])
        live_v = new.get(sub, [])
        if not isinstance(seed_v, list) or not isinstance(live_v, list):
            if sub in seed_perm and seed_perm.get(sub) != live_v:
                skipped.append(f"permissions.{sub} differs — leaving live")
            continue
        if set(live_v) < set(seed_v):
            # live is a strict subset of seed → live has fewer entries →
            # weakenend (e.g. live deny=[] but seed deny=["Bash(rm *)"]).
            new[sub] = list(seed_v)
            applied.append(
                f"permissions.{sub} restored from seed (live was weakened subset)"
            )
        elif set(live_v) != set(seed_v):
            skipped.append(
                f"permissions.{sub} differs (live={live_v!r}, seed={seed_v!r}) — leaving live"
            )

    # other permission sub-keys
    for key, value in seed_perm.items():
        if key in ("allow", "deny", "ask"):
            continue
        if key not in new:
            new[key] = value
            applied.append(f"permissions.{key} added from seed")
        elif new.get(key) != value:
            skipped.append(f"permissions.{key} differs — leaving live")

    return new, applied, skipped


def _hooks_fix(
    seed: dict[str, object], live: dict[str, object]
) -> tuple[object | None, str]:
    """Decide the wholesale-overwrite action for ``hooks``.

    Return ``(new_value, description)`` where ``new_value`` is ``None`` if
    no change is needed. A ``new_value`` of ``{}`` means "drop live's
    hooks" (seed has no hooks key or an empty hooks dict); any other dict
    means "overwrite live's hooks with this from seed".

    hooks are wholesale (not additive) because they are not live-modified
    by approval flows — any divergence is genuine drift.
    """
    seed_has_hooks = "hooks" in seed
    seed_hooks = seed.get("hooks")
    if not seed_has_hooks:
        if "hooks" in live:
            return {}, "hooks dropped (seed has none) — wholesale overwrite"
        return None, ""
    if live.get("hooks") != seed_hooks:
        return seed_hooks, "hooks overwritten from seed"
    return None, ""


def _hooks_reverse_sync(
    live: dict[str, object], seed: dict[str, object]
) -> tuple[object | None, str]:
    """Decide the wholesale live -> seed action for hooks (sync-to-seed's
    counterpart to :func:`_hooks_fix`).

    Reuses ``_hooks_fix``'s value computation by calling it with live and
    seed swapped — that part is genuinely symmetric (first arg is "source
    of truth", second is "target to overwrite"). But ``_hooks_fix``'s
    description strings are hardcoded for the seed->live direction
    ("hooks overwritten from seed"); printing those verbatim here would
    say "from seed" when the value actually came from live. This wrapper
    keeps the value logic but supplies its own live->seed-worded
    description.
    """
    new_value, _ = _hooks_fix(live, seed)
    if new_value is None:
        return None, ""
    if new_value == {}:
        return new_value, "hooks dropped from seed (live has none)"
    return new_value, "hooks mirrored from live"


def _settings_permissions_live_only(
    seed_perm: dict[str, object], live_perm: dict[str, object]
) -> list[str]:
    """Return description fragments like "allow +[...]" for permissions
    sub-keys (allow/deny/ask) present live but not in seed. These are
    plain string lists (patterns), so presence is the only signal — there
    is no per-entry value to diverge. Report-only — never used to write
    anything."""
    fragments = []
    for sub in ("allow", "deny", "ask"):
        seed_v = seed_perm.get(sub, [])
        live_v = live_perm.get(sub, [])
        if not isinstance(seed_v, list) or not isinstance(live_v, list):
            continue
        missing = [e for e in live_v if e not in seed_v]
        if missing:
            fragments.append(f"{sub} +{missing!r}")
    return fragments


def _opencode_permission_live_only(
    seed_perm: dict[str, object], live_perm: dict[str, object]
) -> list[str]:
    """Same idea for opencode's ``permission.bash`` and
    ``permission.external_directory`` — both dicts of pattern/path ->
    verdict. Unlike settings.json's lists, a shared key can carry a
    *different* verdict live vs seed (e.g. "ask" -> "allow"), which is
    real drift even though the key exists in both — so both presence and
    value mismatches are reported."""
    fragments = []
    for sub in ("bash", "external_directory"):
        seed_d = seed_perm.get(sub, {})
        live_d = live_perm.get(sub, {})
        if not isinstance(seed_d, dict) or not isinstance(live_d, dict):
            continue
        parts = []
        missing = [k for k in live_d if k not in seed_d]
        if missing:
            parts.append(f"+{missing!r}")
        differing = [k for k in live_d if k in seed_d and live_d[k] != seed_d[k]]
        if differing:
            detail = ", ".join(
                f"{k!r} live={live_d[k]!r} seed={seed_d[k]!r}" for k in differing
            )
            parts.append(f"differs[{detail}]")
        if parts:
            fragments.append(f"{sub} " + " ".join(parts))
    return fragments


def _merge_opencode_permission(
    seed_perm: dict[str, object], live_perm: dict[str, object]
) -> tuple[dict[str, object], list[str], list[str]]:
    """Additively merge seed ``permission`` into live for opencode.jsonc.

    The args are the ``permission`` sub-dicts (NOT whole top-level
    configs), so ``bash`` is read directly rather than via
    :func:`_bash_permissions` (which expects a top-level config and looks
    up ``config["permission"]["bash"]``).

    * ``permission.bash``: union — append seed patterns not in live;
      differing verdicts on shared patterns are flag-and-skip; bypass
      patterns (``xargs *`` / ``awk *``) present live-but-not-seed are
      stripped (security regression takes priority over preserving
      live-only entries).
    * ``permission.external_directory``: union of path keys (append seed
      paths not in live; differing verdicts flag-and-skip).
    * Other ``permission`` sub-keys: append from seed if missing;
      flag-and-skip if both but differ.
    """
    new: dict[str, object] = dict(live_perm)
    applied: list[str] = []
    skipped: list[str] = []

    # bash patterns: additive union + bypass removal + flag-and-skip differing
    seed_bash = seed_perm.get("bash", {})
    live_bash = new.get("bash", {})
    if isinstance(seed_bash, dict) or isinstance(live_bash, dict):
        seed_bash_d = seed_bash if isinstance(seed_bash, dict) else {}
        live_bash_d = live_bash if isinstance(live_bash, dict) else {}
        merged_bash: dict[str, object] = dict(live_bash_d)
        # append seed patterns not in live
        for pat, verdict in seed_bash_d.items():
            if pat not in merged_bash:
                merged_bash[pat] = verdict
                applied.append(f"permission.bash +[{pat!r}]")
            elif merged_bash.get(pat) != verdict:
                skipped.append(
                    f"permission.bash[{pat!r}] verdict differs "
                    f"(live={merged_bash.get(pat)!r}, seed={verdict!r}) — leaving live"
                )
        # strip bypass patterns present live-but-not-seed (security regression)
        stripped = [
            pat
            for pat in OPENCODE_BYPASS_PATTERNS
            if pat in merged_bash and pat not in seed_bash_d
        ]
        for pat in stripped:
            del merged_bash[pat]
            applied.append(f"permission.bash -[{pat!r}] (allowlist bypass removed)")
        # write back if changed (only when live actually had a bash dict or
        # the merge produced one)
        if merged_bash != live_bash_d or not isinstance(live_bash, dict):
            new["bash"] = merged_bash

    # external_directory: additive union of path keys
    for sub in ("external_directory",):
        seed_sub = seed_perm.get(sub)
        live_sub = new.get(sub)
        if isinstance(seed_sub, dict) and isinstance(live_sub, dict):
            merged: dict[str, object] = dict(live_sub)
            for key, verdict in seed_sub.items():
                if key not in merged:
                    merged[key] = verdict
                    applied.append(f"permission.{sub} +[{key!r}]")
                elif merged.get(key) != verdict:
                    skipped.append(
                        f"permission.{sub}[{key!r}] verdict differs "
                        f"(live={merged.get(key)!r}, seed={verdict!r}) — leaving live"
                    )
            if merged != live_sub:
                new[sub] = merged
        elif isinstance(seed_sub, dict) and sub not in new:
            merged = dict(seed_sub)
            new[sub] = merged
            applied.append(f"permission.{sub} added from seed")

    # other permission sub-keys
    for key, value in seed_perm.items():
        if key in ("bash", "external_directory"):
            continue
        if key not in new:
            new[key] = value
            applied.append(f"permission.{key} added from seed")
        elif new.get(key) != value:
            skipped.append(f"permission.{key} differs — leaving live")

    return new, applied, skipped


def _atomic_write(path: Path, data: dict[str, object]) -> None:
    """Write ``data`` as JSON to ``path`` atomically (tmp in the same dir +
    ``os.replace``). Defense in depth against torn reads — does NOT solve
    the semantic race with a live Claude Code session (see cmd_fix's
    refusal gate)."""
    text = json.dumps(data, indent=2) + "\n"
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmppath = tempfile.mkstemp(prefix=path.name + ".", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmppath, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmppath)
        raise


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` verbatim to ``path`` atomically (tmp in the same dir
    + ``os.replace``), same pattern as ``_atomic_write`` but for a raw
    string instead of a JSON-serialized dict. Used for VS Code's
    settings/keybindings files, which must be copied byte-for-byte — a
    JSON parse-and-reserialize round-trip would break on native JSONC
    (comments, trailing commas) and silently reformat the user's file even
    when it didn't break."""
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmppath = tempfile.mkstemp(prefix=path.name + ".", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmppath, path)
    except BaseException:
        with suppress(OSError):
            os.unlink(tmppath)
        raise


def _fix_settings_file(live_path: Path, seed_path: Path, quiet: bool = False) -> int:
    """Additively fix settings.json drift. Return 0 on success or
    no-action, 1 on parse failure."""
    try:
        live = _load_json_strict(live_path)
    except FileNotFoundError:
        return 0  # no live file → nothing to fix
    except DriftCheckError as exc:
        _print_loud(f"[settings_seed_drift_check] cannot fix {live_path}: {exc}")
        return 1
    try:
        seed = _load_json_strict(seed_path)
    except FileNotFoundError:
        return 0  # no seed → can't fix
    except DriftCheckError as exc:
        _print_loud(
            f"[settings_seed_drift_check] cannot fix — seed {seed_path} unparseable: {exc}"
        )
        return 1

    applied: list[str] = []
    skipped: list[str] = []

    # permissions: additive
    if "permissions" in seed and isinstance(seed["permissions"], dict):
        live_perm = live.get("permissions", {})
        if not isinstance(live_perm, dict):
            skipped.append("permissions: live value not an object — skipping")
        else:
            new_perm, ap, sk = _merge_permissions_additive(
                seed["permissions"], live_perm
            )
            applied.extend(ap)
            skipped.extend(sk)
            if new_perm != live_perm:
                live["permissions"] = new_perm

    # hooks: wholesale
    new_hooks, hook_desc = _hooks_fix(seed, live)
    if new_hooks is not None:
        if new_hooks == {}:
            live.pop("hooks", None)
        else:
            live["hooks"] = new_hooks  # type: ignore[assignment]
        applied.append(hook_desc)

    # Other non-cosmetic top-level keys are NOT auto-fixed — only
    # permissions and hooks are critical-and-repairable. Anything else
    # (an unknown future security key) is reported by `check` but left
    # alone by `fix`; a human decides whether to add it to the seed or
    # silence it in cosmetics.

    if not applied:
        cli_common.qprint(
            f"settings_seed_drift_check: {live_path.name} — no auto-repairable drift.",
            quiet=quiet,
        )
        for s in skipped:
            cli_common.qprint(f"  skipped: {s}", quiet=quiet)
        return 0

    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    backup = live_path.with_suffix(live_path.suffix + f".bak.{stamp}")
    shutil.copy2(live_path, backup)
    _atomic_write(live_path, live)
    cli_common.qprint(
        f"settings_seed_drift_check: repaired {live_path} "
        f"(applied: {', '.join(applied)}) — backup at {backup}",
        quiet=quiet,
    )
    for s in skipped:
        cli_common.qprint(f"  skipped: {s}", quiet=quiet)
    return 0


def _fix_opencode_file(live_path: Path, seed_path: Path, quiet: bool = False) -> int:
    """Additively fix opencode.jsonc drift. Return 0 on success or
    no-action, 1 on parse failure."""
    try:
        live = _load_json_strict(live_path)
    except FileNotFoundError:
        return 0
    except DriftCheckError as exc:
        _print_loud(f"[settings_seed_drift_check] cannot fix {live_path}: {exc}")
        return 1
    try:
        seed = _load_json_strict(seed_path)
    except FileNotFoundError:
        return 0
    except DriftCheckError as exc:
        _print_loud(
            f"[settings_seed_drift_check] cannot fix — seed {seed_path} unparseable: {exc}"
        )
        return 1

    applied: list[str] = []
    skipped: list[str] = []

    if "permission" in seed and isinstance(seed["permission"], dict):
        live_perm = live.get("permission", {})
        if not isinstance(live_perm, dict):
            skipped.append("permission: live value not an object — skipping")
        else:
            new_perm, ap, sk = _merge_opencode_permission(seed["permission"], live_perm)
            applied.extend(ap)
            skipped.extend(sk)
            if new_perm != live_perm:
                live["permission"] = new_perm

    if not applied:
        cli_common.qprint(
            f"settings_seed_drift_check: {live_path.name} — no auto-repairable drift.",
            quiet=quiet,
        )
        for s in skipped:
            cli_common.qprint(f"  skipped: {s}", quiet=quiet)
        return 0

    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
    backup = live_path.with_suffix(live_path.suffix + f".bak.{stamp}")
    shutil.copy2(live_path, backup)
    _atomic_write(live_path, live)
    cli_common.qprint(
        f"settings_seed_drift_check: repaired {live_path} "
        f"(applied: {', '.join(applied)}) — backup at {backup}",
        quiet=quiet,
    )
    for s in skipped:
        cli_common.qprint(f"  skipped: {s}", quiet=quiet)
    return 0


# ── sync-to-seed: reverse direction (live -> seed) ─────────────────────────


def _sync_settings_to_seed(
    live_path: Path, seed_path: Path, quiet: bool = False
) -> int:
    """Mirror live settings.json back into the seed. Return 0 on success
    or no-action, 1 on parse failure.

    hooks are wholesale-mirrored live->seed (they're not live-modified by
    approval flows, so any divergence is genuine drift, same rationale as
    fix()'s seed->live direction). permissions are never written here —
    live approvals are often profile-specific — only reported.
    """
    try:
        live = _load_json_strict(live_path)
    except FileNotFoundError:
        return 0  # no live file → nothing to sync
    except DriftCheckError as exc:
        _print_loud(f"[settings_seed_drift_check] cannot sync {live_path}: {exc}")
        return 1
    try:
        seed = _load_json_strict(seed_path)
    except FileNotFoundError:
        return 0  # no seed → silent no-op
    except DriftCheckError as exc:
        _print_loud(
            f"[settings_seed_drift_check] cannot sync — seed {seed_path} unparseable: {exc}"
        )
        return 1

    new_hooks, hook_desc = _hooks_reverse_sync(live, seed)

    report_lines: list[str] = []
    live_perm = live.get("permissions", {})
    seed_perm = seed.get("permissions", {})
    if isinstance(live_perm, dict) and isinstance(seed_perm, dict):
        fragments = _settings_permissions_live_only(seed_perm, live_perm)
        if fragments:
            report_lines.append(
                f"settings.json permissions live-only (not synced): {', '.join(fragments)}"
            )

    if new_hooks is not None:
        if new_hooks == {}:
            seed.pop("hooks", None)
        else:
            seed["hooks"] = new_hooks  # type: ignore[assignment]
        stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        backup = seed_path.with_suffix(seed_path.suffix + f".bak.{stamp}")
        shutil.copy2(seed_path, backup)
        _atomic_write(seed_path, seed)
        cli_common.qprint(
            f"settings_seed_drift_check: synced {seed_path} "
            f"({hook_desc}) — backup at {backup}",
            quiet=quiet,
        )

    for line in report_lines:
        cli_common.qprint(line, quiet=quiet)
    return 0


def _sync_opencode_to_seed(
    live_path: Path, seed_path: Path, quiet: bool = False
) -> int:
    """Mirror live opencode.jsonc permission drift back into the seed as a
    report only — there is no hooks-equivalent key in opencode.jsonc, so
    nothing is ever wholesale-written for this file. Return 0 on success,
    1 on parse failure."""
    try:
        live = _load_json_strict(live_path)
    except FileNotFoundError:
        return 0
    except DriftCheckError as exc:
        _print_loud(f"[settings_seed_drift_check] cannot sync {live_path}: {exc}")
        return 1
    try:
        seed = _load_json_strict(seed_path)
    except FileNotFoundError:
        return 0
    except DriftCheckError as exc:
        _print_loud(
            f"[settings_seed_drift_check] cannot sync — seed {seed_path} unparseable: {exc}"
        )
        return 1

    live_perm = live.get("permission", {})
    seed_perm = seed.get("permission", {})
    if isinstance(live_perm, dict) and isinstance(seed_perm, dict):
        fragments = _opencode_permission_live_only(seed_perm, live_perm)
        if fragments:
            cli_common.qprint(
                f"opencode.jsonc permission live-only (not synced): {', '.join(fragments)}",
                quiet=quiet,
            )
    return 0


def _sync_vscode_to_seed(live_path: Path, seed_path: Path, quiet: bool = False) -> int:
    """Mirror a live VS Code settings.json/keybindings.json file back into
    its seed. Return 0 always — there's no active-session guard needed
    (this only writes the seed) and no parse-failure path to fail on,
    since this never parses JSON.

    Whole-file copy, raw text not a JSON round-trip (see
    ``_atomic_write_text``): VS Code's live files are legal JSONC
    (comments, trailing commas) that a parse-and-reserialize round-trip
    would break on, or silently reformat even when it didn't break.
    """
    if not live_path.is_file():
        return 0  # nothing live to sync
    live_text = live_path.read_text(encoding="utf-8")
    try:
        seed_text: str | None = seed_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        seed_text = None
    if seed_text == live_text:
        return 0

    backup_msg = ""
    if seed_path.is_file():
        stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
        backup = seed_path.with_suffix(seed_path.suffix + f".bak.{stamp}")
        shutil.copy2(seed_path, backup)
        backup_msg = f" — backup at {backup}"
    _atomic_write_text(seed_path, live_text)
    cli_common.qprint(
        f"settings_seed_drift_check: synced {seed_path} (from {live_path}){backup_msg}",
        quiet=quiet,
    )
    return 0


# ── push-vscode: repo -> live (guarded) ──────────────────────────────────────


def _vscode_process_running() -> bool | None:
    """Best-effort check whether native Windows VS Code (``Code.exe``) is
    currently running, via ``tasklist.exe`` from WSL.

    Matches the image name directly with a word-boundary regex, not a
    substring of the "no tasks found" message — that informational string
    is localized per-language on Windows, so substring matching on it
    would be unreliable across locales.

    Returns ``True`` if confirmed running, ``False`` if confirmed not
    running, or ``None`` if ``tasklist.exe`` isn't found, the call times
    out, or it errors — undetermined is distinct from confirmed-not-running
    so callers can decide how to treat "couldn't check" for themselves.
    """
    try:
        result = subprocess.run(
            ["tasklist.exe", "/FI", "IMAGENAME eq Code.exe"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return bool(re.search(r"\bCode\.exe\b", result.stdout, re.IGNORECASE))


def _push_vscode_to_live(seed_path: Path, live_path: Path) -> tuple[bool, str] | None:
    """Pure diff-and-plan logic for pushing one VS Code file from the repo
    seed to the live Windows path. No process check, no prompt, no write —
    those live in :func:`cmd_push_vscode`, so the same confirm-then-check-
    then-write sequence covers both files exactly once.

    ``seed_path`` must exist (it's the dotfiles repo's own seed). A missing
    ``live_path`` is treated as empty content — meaning this push will
    create the file.

    Return ``None`` if ``seed_path`` and ``live_path`` already have
    identical content (nothing to do for this file). Python's
    ``Path.read_text()`` universal-newline translation already normalizes a
    Windows-saved CRLF to LF on read, so this equality check isn't
    dominated by line-ending noise. Otherwise return ``(live_exists,
    diff)``: ``live_exists`` tells the caller whether a ``.bak`` backup
    applies; ``diff`` is a unified diff from live to seed.
    """
    seed_text = seed_path.read_text(encoding="utf-8")
    live_exists = live_path.is_file()
    live_text = live_path.read_text(encoding="utf-8") if live_exists else ""
    if seed_text == live_text:
        return None
    diff = "".join(
        difflib.unified_diff(
            live_text.splitlines(keepends=True),
            seed_text.splitlines(keepends=True),
            fromfile=str(live_path),
            tofile=str(seed_path),
        )
    )
    return live_exists, diff


# ── active-session detection ─────────────────────────────────────────────────


def _roster_sessions_active() -> int:
    """Return count of active Claude Code sessions reported by the daemon
    roster (``~/.claude/daemon/roster.json``), or 0 if the roster is
    absent or reports no live supervisor / empty workers."""
    roster_path = HOME / ".claude" / "daemon" / "roster.json"
    try:
        data = json.loads(roster_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if not isinstance(data, dict):
        return 0
    supervisor_pid = data.get("supervisorPid")
    workers = data.get("workers", {})
    if not isinstance(supervisor_pid, int) or not isinstance(workers, dict):
        return 0
    if not workers:
        return 0
    # supervisor must be alive for the workers to be considered active
    if not _pid_alive(supervisor_pid):
        return 0
    return len(workers)


def _pid_alive(pid: int) -> bool:
    """Return True if ``pid`` is a live process owned by this user."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # not our process, but it does exist
        return True
    except OSError:
        return False
    return True


def _proc_scan_sessions_active() -> int:
    """Return count of running processes whose executable basename is
    ``claude``. Linux-only (scans ``/proc``); on other platforms falls
    back to ``pgrep -x claude``."""
    if os.path.exists("/proc"):
        count = 0
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                exe = os.readlink(f"/proc/{entry}/exe")
            except OSError:
                continue
            if os.path.basename(exe) == "claude":
                count += 1
        return count
    # non-Linux fallback
    try:
        result = subprocess.run(
            ["pgrep", "-x", "claude"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired, FileNotFoundError):
        return 0
    if result.returncode != 0:
        return 0
    return len([ln for ln in result.stdout.splitlines() if ln.strip()])


def _sessions_active() -> int:
    """Return the number of active Claude Code sessions detected.

    Combines two signals (either indicates active sessions): the daemon
    roster's live supervisor with non-empty workers, and a /proc scan for
    processes whose executable basename is ``claude``. Max of the two —
    one signal catching what the other misses.
    """
    return max(_roster_sessions_active(), _proc_scan_sessions_active())


# ── subcommand entry points ──────────────────────────────────────────────────


def cmd_fix(quiet: bool = False) -> int:
    """Additively repair drifted critical keys in live files from seeds.
    Refuse to run while any Claude Code session is active — Claude Code
    holds settings in memory for the session's lifetime and serializes its
    in-memory state (not a fresh read-merge-write) on every approval, so a
    mid-session fix gets silently clobbered on the next approval and
    SessionStart never re-fires."""
    n = _sessions_active()
    if n > 0:
        _print_loud(
            f"[settings_seed_drift_check] refusing to fix: {n} Claude Code "
            "session(s) active — close them first to avoid losing the fix "
            "on next approval."
        )
        return 1

    exit_code = 0
    settings_live = HOME / ".claude" / "settings.json"
    settings_seed = settings_seed_path()
    if settings_live.is_file() and settings_seed.is_file():
        exit_code = (
            _fix_settings_file(settings_live, settings_seed, quiet=quiet) or exit_code
        )

    oc_seed = opencode_seed_path()
    if oc_seed is not None and oc_seed.is_file():
        oc_live = HOME / ".config" / "opencode" / "opencode.jsonc"
        if oc_live.is_file():
            exit_code = _fix_opencode_file(oc_live, oc_seed, quiet=quiet) or exit_code

    return exit_code


def cmd_sync_to_seed(dotfiles_root: Path, quiet: bool = False) -> int:
    """Mirror live settings back into the dotfiles seed (reverse of
    fix()). No active-session guard needed — this only writes the seed
    file in ``dotfiles_root``, never the live files Claude Code/opencode
    hold in memory for a session's lifetime."""
    if not dotfiles_root.is_dir():
        _print_loud(
            f"[settings_seed_drift_check] --dotfiles-root {dotfiles_root} "
            "does not exist or is not a directory."
        )
        return 1

    exit_code = 0
    settings_live = HOME / ".claude" / "settings.json"
    settings_seed = settings_seed_path(dotfiles_root)
    exit_code = (
        _sync_settings_to_seed(settings_live, settings_seed, quiet=quiet) or exit_code
    )

    oc_seed = opencode_seed_path(dotfiles_root)
    if oc_seed is not None:
        oc_live = HOME / ".config" / "opencode" / "opencode.jsonc"
        exit_code = _sync_opencode_to_seed(oc_live, oc_seed, quiet=quiet) or exit_code

    vscode_user_dir = _vscode_wsl_user_dir()
    if vscode_user_dir is not None:
        for name in ("settings.json", "keybindings.json"):
            vscode_live = vscode_user_dir / name
            vscode_seed = vscode_seed_path(name, dotfiles_root)
            exit_code = (
                _sync_vscode_to_seed(vscode_live, vscode_seed, quiet=quiet) or exit_code
            )

    return exit_code


def _diff_summary(diff: str) -> str:
    """Return a one-line "N lines added, M removed" summary of a unified
    diff string, for orienting a large diff. Not required for correctness
    — the diff itself, printed alongside this, is the actual record."""
    added = sum(
        1
        for line in diff.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    removed = sum(
        1
        for line in diff.splitlines()
        if line.startswith("-") and not line.startswith("---")
    )
    return f"{added} lines added, {removed} removed"


def cmd_push_vscode(dotfiles_root: Path, quiet: bool = False, yes: bool = False) -> int:
    """Push the dotfiles seed's VS Code settings.json/keybindings.json out
    to the live Windows user directory (the repo->live direction; see the
    module docstring's ``push-vscode`` section for the full guard
    rationale).

    Confirms each file's diff before writing, then checks for a running
    native Windows VS Code process immediately before writing — deliberately
    last, not first, so the (unbounded) human-response pause during
    confirmation isn't inside the TOCTOU window. No partial writes: nothing
    is written until every file needing confirmation has been confirmed
    and the process check has passed.
    """
    vscode_user_dir = _vscode_wsl_user_dir()
    if vscode_user_dir is None:
        _print_loud(
            "[settings_seed_drift_check] push-vscode only applies under WSL "
            "with a Windows-side `code` CLI on PATH."
        )
        return 1

    to_write: list[tuple[Path, Path, bool]] = []  # (seed, live, live_exists)
    for name in ("settings.json", "keybindings.json"):
        seed = vscode_seed_path(name, dotfiles_root)
        live = vscode_user_dir / name
        result = _push_vscode_to_live(seed, live)
        if result is None:
            continue
        live_exists, diff = result
        cli_common.qprint(f"{name}: {_diff_summary(diff)}", quiet=quiet)
        print(diff)  # always printed regardless of --quiet — the record of
        # what a destructive command is about to overwrite isn't chatter.

        if yes:
            confirmed = True
        elif not sys.stdin.isatty():
            _print_loud(
                "[settings_seed_drift_check] refusing: confirmation required, "
                "stdin is not interactive — pass --yes"
            )
            return 1
        else:
            answer = input(f"Push {name} to the live Windows VS Code config? [y/N] ")
            confirmed = answer.strip().lower() == "y"

        if confirmed:
            to_write.append((seed, live, live_exists))

    if not to_write:
        cli_common.qprint("settings_seed_drift_check: nothing to push.", quiet=quiet)
        return 0

    if _vscode_process_running() is not False:
        _print_loud(
            "[settings_seed_drift_check] refusing to push: VS Code appears to "
            "be running on Windows, or its status could not be verified — "
            "close it first, then re-run push-vscode (it could clobber this "
            "write on its next save)."
        )
        return 1

    pushed: list[str] = []
    for seed, live, live_exists in to_write:
        if live_exists:
            stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")
            backup = live.with_suffix(live.suffix + f".bak.{stamp}")
            shutil.copy2(live, backup)
        _atomic_write_text(live, seed.read_text(encoding="utf-8"))
        pushed.append(live.name)

    cli_common.qprint(
        f"settings_seed_drift_check: pushed {', '.join(pushed)} to {vscode_user_dir}",
        quiet=quiet,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="settings_seed_drift_check")
    # --quiet/-v are defined once, on every leaf subcommand parser only
    # (via this shared `parents=` parser) -- never on `parser` itself. See
    # dev_status.py's build_parser() for the full rationale.
    verbosity_parent = argparse.ArgumentParser(add_help=False)
    cli_common.add_verbosity_args(verbosity_parent)
    subparsers = parser.add_subparsers(dest="subcommand")
    subparsers.add_parser("check", parents=[verbosity_parent])
    subparsers.add_parser("fix", parents=[verbosity_parent])
    sync_parser = subparsers.add_parser("sync-to-seed", parents=[verbosity_parent])
    sync_parser.add_argument(
        "--dotfiles-root", type=Path, default=DOTFILES, dest="dotfiles_root"
    )
    push_parser = subparsers.add_parser("push-vscode", parents=[verbosity_parent])
    push_parser.add_argument(
        "--dotfiles-root", type=Path, default=DOTFILES, dest="dotfiles_root"
    )
    push_parser.add_argument("--yes", "-y", action="store_true")

    args = parser.parse_args(argv)
    quiet = getattr(args, "quiet", False)
    subcommand = args.subcommand or "check"
    if subcommand == "check":
        return cmd_check(quiet=quiet)
    if subcommand == "fix":
        return cmd_fix(quiet=quiet)
    if subcommand == "push-vscode":
        return cmd_push_vscode(args.dotfiles_root, quiet=quiet, yes=args.yes)
    return cmd_sync_to_seed(args.dotfiles_root, quiet=quiet)


if __name__ == "__main__":
    sys.exit(main())
