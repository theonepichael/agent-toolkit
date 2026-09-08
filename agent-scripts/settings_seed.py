#!/usr/bin/env python3
"""Copy-once settings seeding, adoption, reseed, and drift detection.

Extracted from install.py (the ``── copy-once seeds and drift detection ──``
region) so the settings subsystem lives in one stdlib-only module instead of
inside the 5000-line installer. install.py keeps the orchestration: the four
harness entrypoints (``seed_claude_settings``, ``seed_pi_settings``,
``seed_opencode_config``, ``seed_vscode_settings``), the WSL/Windows platform
probes, and the summary consumption. This module holds everything those
entrypoints delegate to, moved verbatim.

Installer-context contract
--------------------------
The seed machinery consumes exactly this surface of install.Context, passed
in duck-typed (typed ``install.Context`` under ``typing.TYPE_CHECKING`` only,
so the module never imports install at runtime):

- ``ctx.opts.adopt`` / ``ctx.opts.dry_run`` / ``ctx.opts.reseed`` /
  ``ctx.opts.quiet`` — mode flags,
- ``ctx.display(path) -> str`` — path formatting for every printed line,
- ``ctx.reporter.skip(label, reason)`` — the skip-and-report channel,
- ``ctx.manifest.record_copy(dest)`` / ``ctx.manifest.has_backup(dest)`` /
  ``ctx.manifest.record_backup(dest, backup)`` — run-history bookkeeping,
- ``ctx.dotfiles`` — the repo root, for the git-cleanliness safeguard.

Subprocess access is injected, not imported: ``seed_file`` takes a required
keyword-only ``run_command`` (the installer's subprocess wrapper) so the
git-tracked-and-clean adoption safeguard stays steerable from install.py's
namespace — tests stub ``install.run_command`` and the entrypoints pass it
through at call time. Required, not defaulted, so a future caller cannot
silently skip the safeguard.

Drift helpers (``json_key_drift``, ``describe_*_drift``, ...) are pure and
print nothing — they are the seam ``settings_seed_drift_check.py``
eventually imports instead of its vendored copies. Printing in the seed
machinery goes through the sibling ``cli_common`` (``qprint``, the canonical
``PALETTE``, and ``preview``), exactly as it did inside install.py.
"""

import json
import os
import shutil
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import cli_common

if TYPE_CHECKING:
    import install

    Context = install.Context
else:
    Context = object  # duck-typed at runtime; see module docstring


class CommandOutcome(Protocol):
    """Duck-type of install.CommandResult, what an injected run_command returns."""

    ok: bool
    stdout: str


def json_key_drift(seed: dict[str, object], live: dict[str, object]) -> list[str]:
    """Return the top-level keys whose values differ between seed and live."""
    return sorted(k for k in set(seed) | set(live) if seed.get(k) != live.get(k))


_BYPASS_BASH_PATTERNS = (
    # Take an arbitrary command as their own argument (awk via
    # ``system()``), so their presence isn't "individually risky command
    # a profile could allow" — it defeats the allowlist entirely.
    "xargs *",
    "awk *",
    "sqlite3 *",  # .shell/.system dot-commands run arbitrary shell
    "nohup *",
    # Broaden an otherwise-narrow, already-approved command into a wider
    # category that can reach arbitrary code.
    "git --no-pager *",  # matches any git subcommand, incl. commit/push
    "uv *",  # broadens past the 4 named uv commands; `uv run` is arbitrary
    "python3 -m *",  # any installed module, incl. ones with side effects
    # Inline arbitrary code evaluation.
    "node -e *",
    "python3 -c *",
    "python3 - *",
    # Network-fetches and runs lifecycle hooks / arbitrary packages.
    "npm install*",
    "npm install",
    "npx *",
    # Delegates to a CLI with its own separate permission model, or the
    # same CLI redirected/auto-approved via specific flags.
    "opencode run*",  # --auto/--dir make this a real bypass
    "copilot *",
)


def opencode_bypass_drift(
    seed: dict[str, object], live: dict[str, object]
) -> list[str]:
    """Return allowlist-bypass bash patterns present live but not in the seed.

    This is a curated, fixed set — not a generalized "any key live has
    that seed doesn't" diff. A generalized version would flag a live-only
    key that's merely narrower than, but already behaviorally covered by,
    an existing seed glob (e.g. a one-off interactively-approved
    ``git log --all`` against seed's ``git log*``) as false-positive drift. Every pattern here instead shares one of two properties that
    makes a legitimate interactive approval unlikely to ever collide with
    it: it takes an arbitrary command as its own argument (``xargs``,
    ``awk``, ``sqlite3``'s ``.shell``/``.system``, ``nohup``), or it
    broadens an otherwise-narrow, already-approved command into a wider
    category, evaluates code inline, fetches and runs external code, or
    delegates to a separate CLI/permission model entirely.

    This check is diff-gated (only runs when a caller already detected
    seed≠live) and deliberately doesn't attempt full policy compliance —
    only this bypass-shaped subset. It's also a snapshot of known bypass
    shapes, not a taxonomy: a future bypass-shaped tool not in this tuple
    (e.g. ``perl -e *``) isn't automatically caught here or by the seed's
    own policy-compliance test — a policy review has to catch that, same
    as any other undocumented addition. Full policy compliance for the
    *seed* itself (not just this bypass subset, and unconditional on any
    diff existing) is a separate, CI-only pytest check — see
    ``test/test_install.py``'s ``_APPROVED_BASH_PATTERNS``.
    """
    seed_bash = _bash_permissions(seed)
    live_bash = _bash_permissions(live)
    return [k for k in _BYPASS_BASH_PATTERNS if k in live_bash and k not in seed_bash]


def _bash_permissions(config: dict[str, object]) -> dict[str, object]:
    """Return ``permission.bash`` from an opencode config, or ``{}``."""
    permission = config.get("permission")
    if not isinstance(permission, dict):
        return {}
    bash = permission.get("bash")
    return bash if isinstance(bash, dict) else {}


def _load_json_pair_text(
    seed_text: str, live_text: str
) -> tuple[dict[str, object], dict[str, object]] | None:
    """Load a JSON object pair from already-read text."""
    try:
        seed_data = json.loads(seed_text)
        live_data = json.loads(live_text)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(seed_data, dict) or not isinstance(live_data, dict):
        return None
    return seed_data, live_data


def _describe_settings_text(seed_text: str, live_text: str) -> str:
    """Describe settings drift without rereading either side."""
    if seed_text == live_text:
        return ""
    pair = _load_json_pair_text(seed_text, live_text)
    if pair is None:
        return "content differs from the repo copy (unreadable or invalid JSON)"
    return ", ".join(json_key_drift(*pair))


def _describe_opencode_text(
    seed_text: str, live_text: str, *, adopt: bool = False
) -> str:
    """Describe opencode drift without rereading either side."""
    if seed_text == live_text:
        return ""
    pair = _load_json_pair_text(seed_text, live_text)
    if pair is None:
        return "content differs from the repo copy (unreadable or invalid JSON)"
    bypasses = opencode_bypass_drift(*pair)
    if bypasses:
        action = (
            "resolve manually before adopting"
            if adopt
            else "re-run with --reseed to fix"
        )
        return (
            f"SECURITY: {', '.join(bypasses)} still allowed in your live "
            f"opencode.jsonc (allowlist bypass) — {action}"
        )
    return ", ".join(json_key_drift(*pair))


def _describe_vscode_text(seed_text: str, live_text: str) -> str:
    """Describe VS Code JSON/JSONC drift without rereading either side."""
    if seed_text == live_text:
        return ""
    try:
        seed_data: object = json.loads(seed_text)
    except json.JSONDecodeError:
        seed_data = None
    try:
        live_data: object = json.loads(live_text)
    except json.JSONDecodeError:
        live_data = None
    if isinstance(seed_data, dict) and isinstance(live_data, dict):
        return ", ".join(json_key_drift(seed_data, live_data))
    if isinstance(seed_data, list) and isinstance(live_data, list):
        if len(seed_data) != len(live_data):
            return f"{len(live_data)} bindings live vs {len(seed_data)} in seed"
        return f"binding definitions differ ({len(live_data)} bindings)"
    return "content differs from the repo copy"


def describe_settings_drift(seed: Path, live: Path) -> str:
    """Describe how a live settings.json diverged from its seed.

    Text equality is checked first, before any JSON parsing is attempted —
    see ``describe_vscode_drift``'s docstring for why (this mirrors its
    exact shape). Only once text has already proven to differ does an
    unparseable live file get its own non-empty fallback, so a corrupted
    live settings.json is no longer invisible to drift reporting.
    """
    if not seed.is_file() or not live.is_file():
        return ""
    return _describe_settings_text(
        seed.read_text(encoding="utf-8"), live.read_text(encoding="utf-8")
    )


def describe_opencode_drift(seed: Path, live: Path) -> str:
    """Describe how a live opencode.jsonc diverged from its seed.

    A returned allowlist bypass outranks (and replaces) the generic key
    list: it's a security regression, not config drift to skim past. Text
    equality is checked before any JSON parsing, same as
    ``describe_settings_drift`` — this is what keeps a byte-identical
    ``opencode.jsonc`` containing a ``//`` comment from being misreported as
    drifted just because ``json.loads`` can't parse it.
    """
    if not seed.is_file() or not live.is_file():
        return ""
    return _describe_opencode_text(
        seed.read_text(encoding="utf-8"), live.read_text(encoding="utf-8")
    )


def describe_vscode_drift(seed: Path, live: Path) -> str:
    """Describe how a live VS Code settings/keybindings file diverged from its seed.

    Text equality is the definitive drift signal, not JSON equality: VS
    Code's live files are legal JSONC (``//`` comments, trailing commas),
    which ``json.loads`` can't parse, so a JSON-first check would miss real
    drift whenever the live file merely contains a comment. JSON parsing
    below only runs after text drift is already confirmed, purely to
    enrich the message. A missing seed or live file means there's nothing
    to compare — not a difference to report.
    """
    if not seed.is_file() or not live.is_file():
        return ""
    seed_text = seed.read_text(encoding="utf-8")
    live_text = live.read_text(encoding="utf-8")
    if seed_text == live_text:
        return ""

    return _describe_vscode_text(seed_text, live_text)


def _load_json_pair(
    seed: Path, live: Path
) -> tuple[dict[str, object], dict[str, object]] | None:
    """Load a (seed, live) JSON pair, or None if either can't be read."""
    try:
        seed_text = seed.read_text(encoding="utf-8")
        live_text = live.read_text(encoding="utf-8")
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return _load_json_pair_text(seed_text, live_text)


def seed_file(
    ctx: Context,
    seed: Path,
    dest: Path,
    *,
    skip_label: str,
    drift: Callable[[Path, Path], str],
    adopt_drift: Callable[[str, str], str] | None = None,
    adopt_blocker: Callable[[Context, Path, Path, str, str], str | None] | None = None,
    run_command: Callable[..., CommandOutcome],
) -> str:
    """Copy ``seed`` to ``dest`` once, or report drift if it's already there.

    These files (Claude Code's settings.json, opencode's opencode.jsonc,
    Pi's settings.json) are copied rather than symlinked because each tool
    rewrites its own copy in place live (permissions approved, settings
    edited, etc.), which would replace a symlink with a plain file and
    silently detach it from the repo. So the repo copy is a *seed*: written
    once, never overwritten, with divergence reported instead.

    Args:
        ctx: The run context (see the module docstring for the consumed
            Context surface).
        seed: Repo-side seed file (already profile-resolved by the caller).
        dest: Live destination path.
        skip_label: Step name used if the copy fails.
        drift: Callback describing divergence when ``dest`` already exists.
        run_command: The installer's subprocess wrapper, injected by the
            caller so the adoption git-cleanliness check stays steerable
            from install.py's namespace (tests stub it there). Required —
            never defaulted — so a caller can't silently skip the
            safeguard.

    Returns:
        A drift description for the end-of-run summary, or ``""``.
    """
    if ctx.opts.adopt:
        return _adopt_seed(
            ctx,
            seed,
            dest,
            skip_label=skip_label,
            drift=adopt_drift,
            blocker=adopt_blocker,
            run_command=run_command,
        )

    if not dest.is_file():
        if ctx.opts.dry_run:
            cli_common.preview(
                f"would copy {ctx.display(dest)} (from {seed.name})",
                quiet=ctx.opts.quiet,
            )
            return ""
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(seed, dest)
        except OSError:
            ctx.reporter.skip(skip_label, "copy failed")
            return ""
        ctx.manifest.record_copy(dest)
        cli_common.qprint(
            cli_common.PALETTE.ok(f"  copied {ctx.display(dest)} (from {seed.name})"),
            quiet=ctx.opts.quiet,
        )
        return ""

    drift_desc = drift(seed, dest)
    if not drift_desc or not ctx.opts.reseed:
        return drift_desc
    return _reseed_file(ctx, seed, dest, skip_label=skip_label, drift_desc=drift_desc)


def _adopt_seed(
    ctx: Context,
    seed: Path,
    dest: Path,
    *,
    skip_label: str,
    drift: Callable[[str, str], str] | None,
    blocker: Callable[[Context, Path, Path, str, str], str | None] | None,
    run_command: Callable[..., CommandOutcome],
) -> str:
    """Validate one live snapshot, then adopt it into a clean repo seed."""
    if not dest.exists() and not dest.is_symlink():
        return ""
    if seed.is_symlink():
        ctx.reporter.skip(
            skip_label,
            f"repo seed {ctx.display(seed)} is a symlink — resolve manually",
        )
        return "content differs from the repo copy"

    try:
        live_text = dest.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeDecodeError):
        ctx.reporter.skip(
            skip_label,
            f"could not read live file {ctx.display(dest)} — repair it before rerunning",
        )
        return "content differs from the repo copy"
    if not live_text:
        ctx.reporter.skip(
            skip_label,
            f"live file {ctx.display(dest)} is empty — repair it before rerunning",
        )
        return "content differs from the repo copy"

    try:
        seed_text = seed.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        ctx.reporter.skip(
            skip_label,
            f"could not read repo seed {ctx.display(seed)} — repair it before rerunning",
        )
        return "content differs from the repo copy"

    normalized_seed = _normalize_seed_text(seed_text)
    normalized_live = _normalize_seed_text(live_text)
    if normalized_seed == normalized_live:
        return ""

    drift_desc = (
        drift(normalized_seed, normalized_live)
        if drift is not None
        else "content differs from the repo copy"
    )
    reasons: list[str] = []
    if blocker is not None:
        reason = blocker(ctx, seed, dest, normalized_seed, normalized_live)
        if reason:
            reasons.append(reason)
    git_reason = _adopt_git_reason(ctx, seed, run_command=run_command)
    if git_reason:
        reasons.append(git_reason)
    if reasons:
        ctx.reporter.skip(skip_label, "; ".join(reasons))
        return drift_desc or "content differs from the repo copy"

    if ctx.opts.dry_run:
        cli_common.preview(
            f"would adopt {ctx.display(dest)} → {ctx.display(seed)} "
            "(repo seed will become dirty)",
            quiet=ctx.opts.quiet,
        )
        return ""

    if not _adopt_file(ctx, seed, normalized_live, skip_label=skip_label):
        return drift_desc or "content differs from the repo copy"
    cli_common.qprint(
        cli_common.PALETTE.ok(
            f"  adopted {ctx.display(dest)} → {ctx.display(seed)} "
            "(commit the repo seed before adopting another edit)"
        ),
        quiet=ctx.opts.quiet,
    )
    return ""


def _normalize_seed_text(text: str) -> str:
    """Normalize Windows CRLF text without changing other content."""
    return text.replace("\r\n", "\n")


def _adopt_git_reason(
    ctx: Context, seed: Path, *, run_command: Callable[..., CommandOutcome]
) -> str:
    """Return a refusal reason unless Git proves the seed is tracked and clean."""
    try:
        relative = seed.relative_to(ctx.dotfiles).as_posix()
    except ValueError:
        return "repo seed is outside the Git checkout — repair the path manually"
    prefix = ["git", "-C", str(ctx.dotfiles)]
    tracked = run_command(
        [*prefix, "ls-files", "--error-unmatch", "--", relative], capture=True
    )
    if not tracked.ok or not tracked.stdout.strip():
        return (
            f"repo seed {ctx.display(seed)} is untracked or Git is unavailable "
            "— track it and repair Git access before rerunning"
        )
    status = run_command(
        [*prefix, "status", "--porcelain", "--", relative], capture=True
    )
    if not status.ok:
        return (
            f"Git could not inspect {ctx.display(seed)} — repair Git access "
            "before rerunning"
        )
    if status.stdout:
        return (
            f"repo seed {ctx.display(seed)} is dirty — commit or stash it "
            "before rerunning"
        )
    return ""


def _adopt_file(ctx: Context, seed: Path, live_text: str, *, skip_label: str) -> bool:
    """Atomically write normalized live text while preserving the seed mode."""
    if seed.is_symlink():
        ctx.reporter.skip(
            skip_label,
            f"repo seed {ctx.display(seed)} is a symlink — resolve manually",
        )
        return False
    temp_path: Path | None = None
    try:
        mode = stat.S_IMODE(seed.stat().st_mode)
        fd, temp_name = tempfile.mkstemp(prefix=f".{seed.name}.adopt-", dir=seed.parent)
        temp_path = Path(temp_name)
        os.close(fd)
        temp_path.write_text(
            _normalize_seed_text(live_text), encoding="utf-8", newline=""
        )
        os.chmod(temp_path, mode)
        os.replace(temp_path, seed)
    except OSError:
        ctx.reporter.skip(
            skip_label,
            f"could not atomically write {ctx.display(seed)} — "
            "repair the writable directory or disk before rerunning",
        )
        return False
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
    return True


def _opencode_adopt_blocker(
    ctx: Context,
    seed: Path,
    dest: Path,
    seed_text: str,
    live_text: str,
) -> str | None:
    """Refuse live opencode parses that fail or introduce an allowlist bypass."""
    try:
        live_data = json.loads(live_text)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return (
            f"live opencode file {ctx.display(dest)} is not a JSON object "
            "(comments, trailing commas, or invalid JSON are unsupported) — "
            "resolve it manually before adopting"
        )
    if not isinstance(live_data, dict):
        return (
            f"live opencode file {ctx.display(dest)} is not a JSON object — "
            "resolve it manually before adopting"
        )

    seed_data: object
    try:
        seed_data = json.loads(seed_text)
    except (json.JSONDecodeError, UnicodeDecodeError):
        seed_data = {}
    if not isinstance(seed_data, dict):
        seed_data = {}

    bypasses = opencode_bypass_drift(seed_data, live_data)
    if not bypasses:
        return None
    return (
        f"SECURITY: {', '.join(bypasses)} present in live {ctx.display(dest)} "
        "(allowlist bypass) — resolve the security change manually before adopting"
    )


def _reseed_file(
    ctx: Context, seed: Path, dest: Path, *, skip_label: str, drift_desc: str
) -> str:
    """Back up and overwrite a drifted copy-once seed with the repo's version.

    Only called once ``seed_file`` has already confirmed real drift and
    ``--reseed`` is set. ``dest``'s true original is preserved exactly
    once, tracked via the manifest (not merely a ``<name>.bak``'s presence
    on disk — see ``Manifest.has_backup``): a foreign, unrecorded ``.bak``
    blocks the reseed entirely rather than risking either file, and a
    recorded backup whose ``.bak`` was since deleted is treated as if no
    backup had ever been taken.
    """
    backup = dest.with_name(dest.name + ".bak")
    has_backup = ctx.manifest.has_backup(dest)
    backup_exists = backup.exists()

    # A .bak this dotfiles tool never recorded — don't touch either file.
    if not has_backup and backup_exists:
        if ctx.opts.dry_run:
            cli_common.preview(
                f"would skip reseeding {ctx.display(dest)} — {backup.name} exists "
                "but isn't a recorded backup, resolve manually",
                quiet=ctx.opts.quiet,
            )
            return drift_desc
        ctx.reporter.skip(
            skip_label,
            f"{backup} exists but isn't a recorded backup — resolve manually",
        )
        return drift_desc

    # True original not (or no longer) safely preserved anywhere.
    needs_backup = not has_backup or not backup_exists

    if ctx.opts.dry_run:
        if needs_backup:
            cli_common.preview(
                f"would back up {ctx.display(dest)} → {dest.name}.bak, "
                f"then reseed from {seed.name}",
                quiet=ctx.opts.quiet,
            )
        else:
            cli_common.preview(
                f"would reseed {ctx.display(dest)} from {seed.name} (already backed up)",
                quiet=ctx.opts.quiet,
            )
        return ""

    if needs_backup:
        try:
            shutil.move(str(dest), str(backup))
        except (OSError, shutil.Error):
            ctx.reporter.skip(skip_label, "reseed backup failed")
            return ""
        ctx.manifest.record_backup(dest, backup)
        cli_common.qprint(f"  Backing up {dest} → {backup}", quiet=ctx.opts.quiet)

    try:
        shutil.copy(seed, dest)
    except OSError:
        ctx.reporter.skip(skip_label, "copy failed")
        if needs_backup:
            try:
                shutil.move(str(backup), str(dest))
            except (OSError, shutil.Error):
                ctx.reporter.skip(
                    skip_label,
                    f"could not restore {dest} from {backup} after failed "
                    f"reseed — {dest.name} is missing; restore manually",
                )
        return ""

    ctx.manifest.record_copy(dest)
    cli_common.qprint(
        cli_common.PALETTE.ok(f"  reseeded {ctx.display(dest)} (from {seed.name})"),
        quiet=ctx.opts.quiet,
    )
    return ""
