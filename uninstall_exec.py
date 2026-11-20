#!/usr/bin/env python3
"""uninstall_exec.py — --uninstall execution: preflight, phases, confirmation, cleanup.

Extracted interface-preserving from ``install.py`` (which keeps thin
binding wrappers so its CLI, stdout/stderr, exit codes, and imported API
are unchanged). This module owns the uninstall *execution* half — baseline
capture, preflight classification reporting, the confirmation contract,
the per-category execution phases, the uninstall-ledger retry loop, and
state finalization. The pure data model stays in ``uninstall_state.py``;
install-proper keeps its own plumbing (command running, link-spec
application, service/gitconfig capture) which is injected here via
:class:`Deps` so the dependency direction stays one-way:
``install -> uninstall_exec -> uninstall``. This module must never import
``install`` — test/test_uninstall_exec_layering.py enforces that.

Repo-maintenance entrypoint category, same as ``install.py`` /
``uninstall_state.py``: no ``links.toml`` row, never installed to a harness config
directory.

Requires Python 3.12+.
"""

import contextlib
import shutil
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

sys.path.insert(0, str(Path(__file__).resolve().parent / "agent-scripts"))

import cli_common  # noqa: E402 — sibling dir inserted above

import uninstall_state  # noqa: E402 — sibling dir on path above


class UninstallOptionsLike(Protocol):
    """The install options uninstall execution actually reads."""

    dry_run: bool
    quiet: bool
    yes: bool


class ManifestLike(Protocol):
    """The install manifest surface uninstall execution actually reads."""

    path: Path


class ManagedServiceLike(Protocol):
    """Structural stand-in for install.ManagedService (name/unit only)."""

    name: str
    unit: str


class LinkSpecLike(Protocol):
    """Structural stand-in for link_inspect.LinkSpec (dest only, here)."""

    dest: str


class CommandResultLike(Protocol):
    """Structural stand-in for install.CommandResult."""

    ok: bool


class UninstallContext(Protocol):
    """Structural subset of install.Context used by uninstall execution."""

    state_dir: Path
    home: Path
    profile_marker: Path
    is_linux: bool
    uninstall_baseline: uninstall_state.Baseline | None
    opts: UninstallOptionsLike
    manifest: ManifestLike

    def has_harness(self, name: str) -> bool: ...


@dataclass(frozen=True)
class Deps:
    """Execution dependencies injected from install.py (resolved at call time).

    Fields are bound from install.py's *current* module globals on every
    uninstall entrypoint invocation, so test monkeypatching and the
    runtime-populated ``managed_services`` list behave exactly as they did
    when this code lived in install.py.
    """

    run_command: Callable[[Sequence[str] | str], CommandResultLike]
    have: Callable[[str], bool]
    link_applies: Callable[[LinkSpecLike, UninstallContext], bool]
    expand_dest: Callable[[str, Path], Path]
    capture_live_service: Callable[
        [UninstallContext, ManagedServiceLike], dict[str, object]
    ]
    capture_package_snapshot: Callable[[str], dict[str, str] | None]
    current_user: Callable[[], str]
    global_git_hooks_path: Callable[[], str | None]
    header: Callable[..., None]
    managed_git_hooks_path: Callable[[UninstallContext], str]
    vscode_wsl_user_dir: Callable[[], Path | None]
    palette: object
    managed_services: list[ManagedServiceLike]
    GLOBAL_GIT_HOOKS_PATH_KEY: str


# ── uninstall baseline capture ──────────────────────────────────────────────


def _is_state_dir_or_its_ancestor(
    deps: Deps, path: Path, ctx: UninstallContext
) -> bool:
    """Whether ``path`` is the state directory itself, or one of its own ancestors.

    The state directory holds this feature's own ``baseline.json``/
    ``history.jsonl``/``uninstall.jsonl``/lock — its removal (and any of
    its ancestors that become empty as a result) is entirely
    ``_finalize_uninstall_state``'s job, run *after* the generic directory
    phase. Letting the generic phase track and act on these paths would
    make it try to rmdir them while they (or the state directory nested
    inside them) still hold files that haven't been cleared yet — an
    unbreakable "not empty" that would permanently block a clean uninstall.
    """
    return path == ctx.state_dir or path in ctx.state_dir.parents


def _uninstall_owned_destinations(
    deps: Deps, ctx: UninstallContext, specs: Sequence[LinkSpecLike]
) -> list[Path]:
    """Every links.toml/seed destination this run's options make applicable.

    These are the only categories whose ``file:``/``symlink:`` keys can ever
    need content restored (rc files are handled separately) — see
    :func:`capture_uninstall_baseline`'s blob-writing rule.
    """
    destinations: list[Path] = []
    for spec in specs:
        if deps.link_applies(spec, ctx):
            destinations.append(deps.expand_dest(spec.dest, ctx.home))
    if ctx.has_harness("claude"):
        destinations.append(ctx.home / ".claude" / "settings.json")
    if ctx.has_harness("opencode"):
        destinations.append(ctx.home / ".config" / "opencode" / "opencode.jsonc")
    if ctx.has_harness("pi"):
        destinations.append(ctx.home / ".pi" / "agent" / "settings.json")
    return destinations


def capture_destination_records(
    dests: Sequence[Path], *, home: Path, state_dir: Path, blob_dir: Path | None
) -> dict[str, dict[str, object]]:
    """Baseline records for link destinations: each file, link, backup and parent.

    For every destination: ``file:<dest>`` (content saved under ``blob_dir``
    when present and ``blob_dir`` is given), ``symlink:<dest>``,
    ``file:<dest>.bak``, and a ``directory:`` record for every ancestor up to
    ``home`` except ``state_dir`` and its own ancestors (see
    :func:`_is_state_dir_or_its_ancestor`). Shared by
    :func:`capture_uninstall_baseline` and the toolkit-home migration, which
    records the destinations it is about to create before creating them.
    """
    records: dict[str, dict[str, object]] = {}
    for dest in dests:
        records[uninstall_state.file_key(dest)] = uninstall_state.capture_file(
            dest, blob_dir=blob_dir
        )
        records[uninstall_state.symlink_key(dest)] = uninstall_state.capture_symlink(
            dest
        )
        bak = dest.with_name(dest.name + ".bak")
        records[uninstall_state.file_key(bak)] = uninstall_state.capture_file(bak)
        for ancestor in uninstall_state.ancestor_directories(dest, home):
            if ancestor == state_dir or ancestor in state_dir.parents:
                continue
            key = uninstall_state.directory_key(ancestor)
            if key not in records:
                records[key] = uninstall_state.capture_directory(ancestor)
    return records


def capture_uninstall_baseline(
    deps: Deps, ctx: UninstallContext, specs: Sequence[LinkSpecLike]
) -> None:
    """Capture this run's uninstall baseline layer before any install step runs.

    Linux/WSL and Fedora only (Implementation Sequence step 6 — this feature
    does not apply on macOS) and a no-op under ``--dry-run`` (step 1: a
    dry-run install writes no ``baseline.json`` and creates no immutable
    first layer). Must run before any step that could mutate state this
    baseline is meant to describe — notably the runtime setup steps that
    follow it (``install_node``/NVM), which can mutate rc files themselves.
    """
    if not ctx.is_linux or ctx.opts.dry_run:
        return

    state_dir = ctx.state_dir
    baseline = uninstall_state.load_baseline(state_dir) or uninstall_state.Baseline()
    records: dict[str, dict[str, object]] = {}
    seen_dirs: set[Path] = set()

    def _track_ancestors(path: Path) -> None:
        for ancestor in uninstall_state.ancestor_directories(path, ctx.home):
            if ancestor in seen_dirs or _is_state_dir_or_its_ancestor(
                deps, ancestor, ctx
            ):
                continue
            seen_dirs.add(ancestor)
            records[uninstall_state.directory_key(ancestor)] = (
                uninstall_state.capture_directory(ancestor)
            )

    for rc_name in uninstall_state.RC_FILENAMES:
        rc_path = ctx.home / rc_name
        records[uninstall_state.file_key(rc_path)] = uninstall_state.capture_file(
            rc_path, blob_dir=state_dir
        )

    dests = _uninstall_owned_destinations(deps, ctx, specs)
    for record_key, record in capture_destination_records(
        dests, home=ctx.home, state_dir=state_dir, blob_dir=state_dir
    ).items():
        records[record_key] = record
    for dest in dests:
        seen_dirs.update(uninstall_state.ancestor_directories(dest, ctx.home))

    for path in (
        ctx.home / ".local" / "bin" / "uv",
        ctx.home / ".local" / "bin" / "oh-my-posh",
        ctx.home / ".vim" / "autoload" / "plug.vim",
    ):
        records[uninstall_state.file_key(path)] = uninstall_state.capture_file(path)
        _track_ancestors(path)

    vscode_user_dir = deps.vscode_wsl_user_dir()
    if vscode_user_dir is not None:
        for name in ("settings.json", "keybindings.json"):
            path = vscode_user_dir / name
            bak = path.with_name(path.name + ".bak")
            for guarded_path in (path, bak):
                record = uninstall_state.capture_file(guarded_path)
                record["needs_vscode_guard"] = True
                records[uninstall_state.file_key(guarded_path)] = record

    records[uninstall_state.file_key(ctx.profile_marker)] = (
        uninstall_state.capture_file(ctx.profile_marker)
    )
    _track_ancestors(ctx.profile_marker)

    for path in (
        ctx.home / ".local" / "bin" / "bat",
        ctx.home / ".local" / "bin" / "fd",
    ):
        records[uninstall_state.symlink_key(path)] = uninstall_state.capture_symlink(
            path
        )
        _track_ancestors(path)

    font_dir = ctx.home / ".local" / "share" / "fonts" / "JetBrainsMonoNerdFont"
    records[uninstall_state.directory_key(font_dir)] = (
        uninstall_state.capture_tree_manifest(font_dir)
    )
    _track_ancestors(font_dir)

    neovim_prefix = ctx.home / ".local" / "opt" / "neovim"
    records[uninstall_state.directory_key(neovim_prefix)] = (
        uninstall_state.capture_tree_manifest(neovim_prefix)
    )
    _track_ancestors(neovim_prefix)

    for parts in uninstall_state.SHARED_NEOVIM_DIRS:
        shared_dir = ctx.home.joinpath(*parts)
        records[uninstall_state.directory_key(shared_dir)] = (
            uninstall_state.capture_directory(shared_dir)
        )

    records[uninstall_state.runtime_key(ctx.home / ".nvm")] = (
        uninstall_state.capture_runtime_nvm(ctx.home)
    )

    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    baseline.add_layer(stamp, records)
    uninstall_state.save_baseline(state_dir, baseline)
    ctx.uninstall_baseline = baseline


def _uninstall_state_paths(deps: Deps, state_dir: Path) -> list[Path]:
    """This feature's own state files, if present — never anything else.

    Snapshot naming is pinned (``baseline.json`` plus
    ``baseline-snapshot-<sha256>.blob``, flat in the state directory), so a
    glob is always exactly correct here — regardless of whether
    ``baseline.json`` itself is missing, empty, or unparseable at rollback
    time. Never includes ``history.jsonl``, the profile marker, or anything
    else this feature doesn't own.
    """
    if not state_dir.is_dir():
        return []
    paths = [uninstall_state.baseline_path(state_dir)]
    paths.extend(sorted(state_dir.glob("baseline-snapshot-*.blob")))
    paths.append(state_dir / "uninstall.lock")
    paths.append(state_dir / "uninstall.jsonl")
    return paths


def _delete_uninstall_state(deps: Deps, ctx: UninstallContext) -> None:
    """Delete this feature's own state files during a real (non-dry-run) rollback.

    The lock file is not unlinked by path: a live run's lock is not this
    sweep's to remove, so it goes through
    :func:`uninstall_state.clear_stale_uninstall_lock`, which removes the file
    only when the recorded holder is dead.
    """
    lock_path = uninstall_state.uninstall_lock_path(ctx.state_dir)
    for path in _uninstall_state_paths(deps, ctx.state_dir):
        if path == lock_path:
            continue
        path.unlink(missing_ok=True)
    uninstall_state.clear_stale_uninstall_lock(ctx.state_dir)


# ── uninstall preflight and CLI ─────────────────────────────────────────────


def _tree_manifest_directories(ctx: UninstallContext) -> set[Path]:
    """Directories tracked as full tree manifests, not plain directory: keys."""
    return {
        ctx.home / ".local" / "share" / "fonts" / "JetBrainsMonoNerdFont",
        ctx.home / ".local" / "opt" / "neovim",
    }


def _recapture_live_value(
    deps: Deps, ctx: UninstallContext, key: str
) -> dict[str, object]:
    """Fresh, read-only live value for one *already-recorded* ownership key.

    Dispatches purely on the key itself — deliberately never re-derives
    "is this destination applicable" from links.toml + the current
    invocation's ``--harness`` selection. ``--uninstall`` is standalone
    (``parse_args`` rejects ``--harness`` alongside it), so at uninstall
    time ``ctx.opts.harnesses`` is always empty; re-deriving applicability
    from it would make every harness-gated links.toml entry (``~/.claude/
    CLAUDE.md``, its commands, the copy-once seed files, ...) silently
    invisible to preflight — the real bug this replaced (caught via a real
    container run, not the fast unit-test suite, since every fast test
    happened to capture and recapture with the same harness selection).
    The baseline itself is the only source of truth for what was ever
    installer-tracked; this function only ever answers "what's live at
    this exact key's path right now."
    """
    type_, path_str = key.split(":", 1)
    path = Path(path_str)
    if type_ == "file":
        return uninstall_state.capture_file(path)
    if type_ == "symlink":
        return uninstall_state.capture_symlink(path)
    if type_ == "directory":
        if path in _tree_manifest_directories(ctx):
            return uninstall_state.capture_tree_manifest(path)
        return uninstall_state.capture_directory(path)
    if type_ == "runtime":
        return uninstall_state.capture_runtime_nvm(path.parent)
    return {"state": uninstall_state.STATE_UNKNOWN}


def _recapture_uninstall_live_state(
    deps: Deps, ctx: UninstallContext, baseline: uninstall_state.Baseline
) -> dict[str, dict[str, object]]:
    """Re-capture every tracked ownership key's *current* value, read-only.

    Driven entirely by ``baseline.all_keys()`` — see
    :func:`_recapture_live_value`'s docstring for why that's load-bearing,
    not incidental. Never writes a blob or persists anything; this only
    builds the "live" half of a preflight comparison.
    """
    return {
        key: _recapture_live_value(deps, ctx, key)
        for key in baseline.all_keys()
        if uninstall_state.key_type(key) not in ("service", "gitconfig")
    }


def _apply_rc_file_reclassification(
    deps: Deps,
    ctx: UninstallContext,
    baseline: uninstall_state.Baseline,
    report: dict[str, uninstall_state.Classification],
) -> None:
    """Override the generic result for each rc file with the append-aware rule."""
    for rc_name in uninstall_state.RC_FILENAMES:
        rc_path = ctx.home / rc_name
        key = uninstall_state.file_key(rc_path)
        recorded = baseline.value_for(key)
        if recorded is None or recorded.get("state") != uninstall_state.STATE_PRESENT:
            continue
        blob_digest = recorded.get("blob")
        baseline_content = (
            uninstall_state.read_blob(ctx.state_dir, str(blob_digest))
            if isinstance(blob_digest, str)
            else None
        )
        try:
            live_content: bytes | None = rc_path.read_bytes()
        except OSError:
            live_content = None
        override = uninstall_state.reclassify_rc_file(
            recorded, baseline_content, live_content
        )
        if override is not None:
            report[key] = override


def _apply_symlink_pair_reclassification(
    deps: Deps,
    baseline: uninstall_state.Baseline,
    live: dict[str, dict[str, object]],
    report: dict[str, uninstall_state.Classification],
) -> None:
    """Override the generic per-key results for each backed-up-then-symlinked pair.

    Candidate paths come from the report's own keys (i.e. the baseline),
    never re-derived from links.toml — same reasoning as
    :func:`_recapture_live_value`.
    """
    file_paths = {
        Path(k.split(":", 1)[1])
        for k in report
        if uninstall_state.key_type(k) == "file"
    }
    symlink_paths = {
        Path(k.split(":", 1)[1])
        for k in report
        if uninstall_state.key_type(k) == "symlink"
    }
    for path in file_paths & symlink_paths:
        file_key = uninstall_state.file_key(path)
        symlink_key = uninstall_state.symlink_key(path)
        override = uninstall_state.reclassify_symlink_destination_pair(
            baseline.value_for(file_key),
            live.get(file_key, {"state": uninstall_state.STATE_UNKNOWN}),
            baseline.value_for(symlink_key),
            live.get(symlink_key, {"state": uninstall_state.STATE_UNKNOWN}),
        )
        if override is not None:
            report[file_key], report[symlink_key] = override


def build_preflight_report(
    deps: Deps, ctx: UninstallContext
) -> dict[str, uninstall_state.Classification] | None:
    """Classify every tracked ownership key, or None if there's no baseline."""
    baseline = uninstall_state.load_baseline(ctx.state_dir)
    if baseline is None:
        return None
    live = _recapture_uninstall_live_state(deps, ctx, baseline)
    report: dict[str, uninstall_state.Classification] = {}
    for key in sorted(baseline.all_keys()):
        # service:/gitconfig: keys use their own dedicated classifier (their
        # record shapes don't fit the tri-state present/absent model the
        # generic classifier expects — gitconfig's "owned" case in
        # particular needs to compare live against a *managed value*, which
        # classify_ownership_key has no parameter for).
        if uninstall_state.key_type(key) in ("service", "gitconfig"):
            continue
        recorded = baseline.value_for(key)
        live_value = live.get(key, {"state": uninstall_state.STATE_UNKNOWN})
        report[key] = uninstall_state.classify_ownership_key(key, recorded, live_value)

    _apply_rc_file_reclassification(deps, ctx, baseline, report)
    _apply_symlink_pair_reclassification(deps, baseline, live, report)

    for service in deps.managed_services:
        service_key = uninstall_state.service_key("systemd", service.name)
        if service_key in baseline.all_keys():
            report[service_key] = uninstall_state.classify_service(
                baseline.value_for(service_key), deps.capture_live_service(ctx, service)
            )

    hooks_key = uninstall_state.gitconfig_key(deps.GLOBAL_GIT_HOOKS_PATH_KEY)
    if hooks_key in baseline.all_keys():
        report[hooks_key] = uninstall_state.classify_gitconfig(
            baseline.value_for(hooks_key),
            uninstall_state.build_gitconfig_record(deps.global_git_hooks_path()),
            deps.managed_git_hooks_path(ctx),
        )
    return report


def build_package_preflight(
    deps: Deps, ctx: UninstallContext
) -> list[uninstall_state.PackageClassification] | None:
    """Classify every requested/introduced package, or None if there's no baseline."""
    baseline = uninstall_state.load_baseline(ctx.state_dir)
    if baseline is None:
        return None
    return uninstall_state.classify_package_transactions(
        baseline, live_package_snapshots(deps, baseline)
    )


def _vscode_guard_preflight_annotations(
    deps: Deps, ctx: UninstallContext, report: dict[str, uninstall_state.Classification]
) -> dict[str, str]:
    """Display-only: flag guarded VS Code keys headed for removal when
    Windows VS Code is running or its status couldn't be verified.

    Purely advisory — never touches the stored ``Classification`` objects
    in ``report`` and never gates anything. The real gate is
    ``execute_file_symlink_phase``'s own, independent, execution-time check
    (see its call site for the TOCTOU rationale): this probe only keeps
    ``--uninstall --dry-run`` from printing a removal it already knows won't
    happen, it does not replace that later check.
    """
    baseline = uninstall_state.load_baseline(ctx.state_dir)
    if baseline is None:
        return {}
    guarded_removals = [
        key
        for key, c in report.items()
        if c.bucket == uninstall_state.BUCKET_OWNED
        and c.action == uninstall_state.ACTION_REMOVE
        and (baseline.value_for(key) or {}).get("needs_vscode_guard")
    ]
    if not guarded_removals:
        return {}
    from settings_seed_drift_check import _vscode_process_running

    if _vscode_process_running() is False:
        return {}
    note = (
        "Windows VS Code is running (or could not be verified) — this will "
        "land as unresolved, not removed"
    )
    return dict.fromkeys(guarded_removals, note)


def _print_preflight_report(
    deps: Deps,
    report: dict[str, uninstall_state.Classification],
    package_report: Sequence[uninstall_state.PackageClassification] = (),
    quiet: bool = False,
    guard_annotations: dict[str, str] | None = None,
) -> None:
    """Print the full uninstall preflight, grouped by bucket."""
    guard_annotations = guard_annotations or {}
    deps.header("==> Uninstall preflight", quiet=quiet)
    for bucket in (
        uninstall_state.BUCKET_OWNED,
        uninstall_state.BUCKET_DRIFTED,
        uninstall_state.BUCKET_UNRESOLVED,
        uninstall_state.BUCKET_PRESERVED,
    ):
        keys = sorted(k for k, c in report.items() if c.bucket == bucket)
        package_lines = [c for c in package_report if c.bucket == bucket]
        if not keys and not package_lines:
            continue
        print(deps.palette.header(f"  {bucket} ({len(keys) + len(package_lines)}):"))
        warn = bucket in (
            uninstall_state.BUCKET_UNRESOLVED,
            uninstall_state.BUCKET_DRIFTED,
        )
        for key in keys:
            c = report[key]
            action = f" [{c.action}]" if c.action else ""
            line = f"    {key}{action} — {c.reason}"
            line_warn = warn
            if key in guard_annotations:
                line += f" ({guard_annotations[key]})"
                line_warn = True
            print(deps.palette.warn(line) if line_warn else line)
        for pc in sorted(package_lines, key=lambda c: c.key):
            action = f" [{pc.action}]" if pc.action else ""
            line = f"    {pc.key}{action} — {pc.reason}"
            print(deps.palette.warn(line) if warn else line)


def _read_confirmation_token(
    deps: Deps,
) -> str:
    """Read one line from stdin, stripping exactly one trailing LF/CRLF.

    Surrounding spaces/tabs are deliberately left in place — the caller
    compares for an exact ``"UNINSTALL"`` match, so ``" UNINSTALL"`` or an EOF
    (empty string) both correctly fail to match.
    """
    line = sys.stdin.readline()
    if line.endswith("\r\n"):
        return line[:-2]
    if line.endswith("\n"):
        return line[:-1]
    return line


def _restore_target_still_occupied(deps: Deps, dest: Path) -> bool:
    return dest.exists() or dest.is_symlink()


def _execute_restore(
    deps: Deps,
    ctx: UninstallContext,
    dest: Path,
    recorded: dict[str, object],
    *,
    expect_absent: bool,
) -> str:
    """Restore ``dest``'s content from its recorded blob.

    ``expect_absent`` is set only for the backed-up-then-symlinked pair
    case, where the paired symlink was just removed in the prior phase —
    if ``dest`` is unexpectedly occupied afterward, something else has
    claimed the path and the restore aborts rather than overwriting it.
    For a plain in-place restore (an appended-to rc file), ``dest`` is
    expected to already exist and gets overwritten with the recorded
    content directly.
    """
    digest = recorded.get("blob")
    if not isinstance(digest, str):
        return "unresolved: no blob recorded for restore"
    content = uninstall_state.read_blob(ctx.state_dir, digest)
    if content is None:
        return "unresolved: recorded blob is missing or unreadable"
    if expect_absent and _restore_target_still_occupied(deps, dest):
        return "unresolved: destination still occupied after symlink removal"
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
    except OSError as exc:
        return f"unresolved: restore failed ({exc})"
    return "ok"


def _execute_remove_symlink(deps: Deps, path: Path) -> str:
    if not path.is_symlink():
        return "ok: already absent"
    try:
        path.unlink()
    except OSError as exc:
        return f"unresolved: could not remove symlink ({exc})"
    return "ok"


def _execute_remove_file(deps: Deps, path: Path) -> str:
    if not path.is_file() or path.is_symlink():
        return "ok: already absent"
    try:
        path.unlink()
    except OSError as exc:
        return f"unresolved: could not remove file ({exc})"
    return "ok"


def _maybe_consume_bak(
    deps: Deps, dest: Path, baseline: uninstall_state.Baseline
) -> None:
    """Delete ``dest``'s ``.bak`` once a clean restore succeeds, if it's
    provably uninstall-owned — never touch a ``.bak`` this feature can't
    prove it created.

    Only ever called after :func:`_execute_restore` has already returned
    ``"ok"`` — its authoritative source is the content blob, so a
    qualifying ``.bak`` is now redundant leftover, not a second restore
    source. See uninstall_state.reclassify_symlink_destination_pair's docstring and
    Implementation Sequence step 4's ``.bak`` provenance rule.
    """
    bak = dest.with_name(dest.name + ".bak")
    file_recorded = baseline.value_for(uninstall_state.file_key(dest))
    bak_recorded = baseline.value_for(uninstall_state.file_key(bak))
    if not (
        file_recorded is not None
        and file_recorded.get("state") == uninstall_state.STATE_PRESENT
        and bak_recorded is not None
        and bak_recorded.get("state") == uninstall_state.STATE_ABSENT
    ):
        return
    try:
        if bak.is_file() and not bak.is_symlink():
            bak.unlink()
    except OSError:
        pass  # best-effort cleanup — never fails the restore itself


def _other_enabled_user_units(deps: Deps, exclude: frozenset[str]) -> list[str] | None:
    """Every enabled systemd ``--user`` unit other than those in ``exclude``.

    None if the listing probe itself failed/is unavailable — callers must
    treat that as "can't prove it's safe," never as an empty list.
    """
    if not deps.have("systemctl"):
        return None
    result = deps.run_command(
        ["systemctl", "--user", "list-unit-files", "--state=enabled", "--no-legend"],
        capture=True,
    )
    if not result.ok:
        return None
    units = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if parts and parts[0] not in exclude:
            units.append(parts[0])
    return units


def _execute_service_disable(deps: Deps, service: ManagedServiceLike) -> str:
    """Disable+stop one managed service's systemd --user unit.

    Unit-level only — linger is a separate, machine-wide concern handled by
    :func:`_reconcile_linger` outside the uninstall ledger (see
    install-multi-service-depart-adopt-spec.md's Design decision: bundling
    linger into a single service's outcome breaks once more than one
    service can be uninstalled in the same run).
    """
    if not deps.have("systemctl"):
        return "unresolved: systemd --user unavailable"
    if not deps.run_command(
        ["systemctl", "--user", "disable", "--now", service.unit]
    ).ok:
        return "unresolved: systemctl --user disable --now failed"
    return "ok"


def _reconcile_linger(
    deps: Deps, ctx: UninstallContext, baseline: uninstall_state.Baseline
) -> None:
    """Best-effort, not ledger-tracked: restore linger once nothing managed
    still needs it.

    Runs unconditionally at the end of every :func:`execute_service_phase`
    call. Eligibility is recomputed from live state every call (not from
    "did this run's loop just disable something"), so a failed attempt
    self-heals on a later ``--uninstall`` invocation with no persisted flag
    needed. Never raises, never returns an outcome, never affects
    ``do_uninstall``'s exit code — a failure here is advisory only, matching
    the existing install-time ``loginctl enable-linger`` failure precedent
    in :func:`_enable_service`.
    """
    if not ctx.is_linux:
        return
    eligible_off = frozenset(
        service.unit
        for service in deps.managed_services
        if (
            recorded := baseline.value_for(
                uninstall_state.service_key("systemd", service.name)
            )
        )
        is not None
        and recorded.get("linger") is False
        and deps.capture_live_service(ctx, service).get("enabled") is False
    )
    if not eligible_off:
        return

    others = _other_enabled_user_units(deps, exclude=eligible_off)
    if others is None:
        cli_common.qprint(
            "  note: could not check for other enabled systemd --user units — "
            "linger left as-is",
            quiet=ctx.opts.quiet,
        )
        return
    if others:
        cli_common.qprint(
            "  note: linger left enabled — other systemd --user units depend "
            f"on it ({', '.join(others)})",
            quiet=ctx.opts.quiet,
        )
        return
    if not deps.run_command(
        ["loginctl", "disable-linger", deps.current_user()], capture=True
    ).ok:
        cli_common.qprint(
            "  note: loginctl disable-linger failed", quiet=ctx.opts.quiet
        )


def execute_service_phase(
    deps: Deps,
    ctx: UninstallContext,
    baseline: uninstall_state.Baseline,
    ledger: uninstall_state.UninstallLedger,
) -> None:
    """Disable+stop every owned managed service, then reconcile linger once."""
    for service in deps.managed_services:
        key = uninstall_state.service_key("systemd", service.name)
        if key in ledger.completed_keys():
            continue
        recorded = baseline.value_for(key)
        if recorded is None:
            continue  # never captured (e.g. a work-profile install) — nothing to check
        c = uninstall_state.classify_service(
            recorded, deps.capture_live_service(ctx, service)
        )
        if c.bucket != uninstall_state.BUCKET_OWNED:
            continue
        ledger.record(
            key,
            c.action or uninstall_state.ACTION_DISABLE,
            _execute_service_disable(deps, service),
        )

    _reconcile_linger(deps, ctx, baseline)


def _execute_gitconfig_restore(deps: Deps, recorded: dict[str, object]) -> str:
    """Restore the global core.hooksPath value baseline recorded: unset if
    it was absent before the installer set it, otherwise set it back."""
    if recorded.get("state") == uninstall_state.STATE_ABSENT:
        ok = deps.run_command(
            ["git", "config", "--global", "--unset", deps.GLOBAL_GIT_HOOKS_PATH_KEY]
        ).ok
    else:
        value = recorded.get("value")
        ok = (
            isinstance(value, str)
            and deps.run_command(
                ["git", "config", "--global", deps.GLOBAL_GIT_HOOKS_PATH_KEY, value]
            ).ok
        )
    return "ok" if ok else "unresolved: git config --global restore failed"


def execute_gitconfig_phase(
    deps: Deps,
    ctx: UninstallContext,
    baseline: uninstall_state.Baseline,
    ledger: uninstall_state.UninstallLedger,
) -> None:
    """Restore the pre-install global core.hooksPath value, if this
    installer owns the current value."""
    key = uninstall_state.gitconfig_key(deps.GLOBAL_GIT_HOOKS_PATH_KEY)
    if key in ledger.completed_keys():
        return
    recorded = baseline.value_for(key)
    if recorded is None:
        return  # never captured (e.g. a work-profile install) — nothing to check
    live = uninstall_state.build_gitconfig_record(deps.global_git_hooks_path())
    c = uninstall_state.classify_gitconfig(
        recorded, live, deps.managed_git_hooks_path(ctx)
    )
    if c.bucket != uninstall_state.BUCKET_OWNED:
        return
    ledger.record(
        key,
        c.action or uninstall_state.ACTION_RESTORE,
        _execute_gitconfig_restore(deps, recorded),
    )


_VSCODE_GUARD_UNRESOLVED_PREFIX = "unresolved [vscode-guard-blocked]:"


def execute_file_symlink_phase(
    deps: Deps,
    ctx: UninstallContext,
    baseline: uninstall_state.Baseline,
    report: dict[str, uninstall_state.Classification],
    ledger: uninstall_state.UninstallLedger,
) -> None:
    """Execute every owned ``file:``/``symlink:`` action, in pinned order.

    Symlink removals run before same-path file restores — the identical
    problem ``do_rollback``'s ``restored_dests`` ordering already solves —
    so a paired restore never finds its own soon-to-be-removed symlink
    still occupying the path.

    A key whose ledger history shows it was blocked by the VS Code guard
    at least once stays retryable across ``done``'s otherwise-permanent
    exclusion — without this, a guard-blocked key would never be retried
    even after VS Code closes, contradicting the guard's own "close it
    first, then re-run --uninstall" message. Every other outcome (including a
    non-guard failure on the same guarded key) keeps the ledger's normal
    never-re-attempted contract.
    """
    done = ledger.completed_keys()
    guard_retryable = ledger.keys_with_outcome_prefix(_VSCODE_GUARD_UNRESOLVED_PREFIX)
    owned = {
        key: c
        for key, c in report.items()
        if c.bucket == uninstall_state.BUCKET_OWNED
        and uninstall_state.key_type(key) in ("file", "symlink")
        and (key not in done or key in guard_retryable)
    }

    for key in sorted(owned):
        c = owned[key]
        if (
            uninstall_state.key_type(key) != "symlink"
            or c.action != uninstall_state.ACTION_REMOVE
        ):
            continue
        path = Path(key.partition(":")[2])
        ledger.record(key, c.action, _execute_remove_symlink(deps, path))

    vscode_running: bool | None = None
    vscode_checked = False
    for key in sorted(owned):
        c = owned[key]
        if uninstall_state.key_type(key) != "file":
            continue
        path = Path(key.partition(":")[2])
        if c.action == uninstall_state.ACTION_REMOVE:
            recorded = baseline.value_for(key) or {}
            if recorded.get("needs_vscode_guard"):
                if not vscode_checked:
                    from settings_seed_drift_check import _vscode_process_running

                    vscode_running = _vscode_process_running()
                    vscode_checked = True
                if vscode_running is not False:
                    ledger.record(
                        key,
                        c.action,
                        f"{_VSCODE_GUARD_UNRESOLVED_PREFIX} Windows VS Code "
                        "is running (or could not be verified) — close it "
                        "first, then re-run --uninstall",
                    )
                    continue
            ledger.record(key, c.action, _execute_remove_file(deps, path))
            continue
        recorded = baseline.value_for(key) or {}
        paired_symlink = report.get(uninstall_state.symlink_key(path))
        expect_absent = (
            paired_symlink is not None
            and paired_symlink.bucket == uninstall_state.BUCKET_OWNED
            and paired_symlink.action == uninstall_state.ACTION_REMOVE
        )
        outcome = _execute_restore(
            deps, ctx, path, recorded, expect_absent=expect_absent
        )
        if outcome == "ok":
            _maybe_consume_bak(deps, path, baseline)
        ledger.record(key, c.action or "restore", outcome)


def _wholesale_removal_directories(deps: Deps, ctx: UninstallContext) -> set[Path]:
    """Directories removed wholesale (bypassing the empty-only rule) when owned.

    The Neovim fallback prefix and Nerd Font directory (tree-manifest
    artifacts) plus the three shared Neovim state/cache dirs — matching
    Implementation Sequence step 4's named exceptions to the generic
    empty-only ``directory:`` removal rule.
    """
    wholesale = {
        ctx.home / ".local" / "share" / "fonts" / "JetBrainsMonoNerdFont",
        ctx.home / ".local" / "opt" / "neovim",
    }
    wholesale.update(
        ctx.home.joinpath(*parts) for parts in uninstall_state.SHARED_NEOVIM_DIRS
    )
    return wholesale


def _execute_remove_directory(deps: Deps, path: Path, *, wholesale: bool) -> str:
    try:
        if path.is_symlink() or not path.is_dir():
            return "ok: already absent"
        if wholesale:
            shutil.rmtree(path)
            return "ok"
        if any(path.iterdir()):
            return "unresolved: directory not empty"
        path.rmdir()
    except OSError as exc:
        return f"unresolved: {exc}"
    return "ok"


def _remove_tree_manifest_directory(
    deps: Deps, baseline: uninstall_state.Baseline, path: Path
) -> str:
    """Remove a wholly installer-owned tree, but only if it is untouched.

    The Nerd Font directory and the pinned Neovim prefix are the two trees
    uninstall deletes outright rather than emptying, so they are the two
    where a wholesale ``rmtree`` could destroy something the user added
    after installing. Gated on the post-install manifest: an exact match is
    the only proof that everything inside is the installer's own.
    """
    try:
        if path.is_symlink() or not path.is_dir():
            return "ok: already absent"
    except OSError as exc:
        return f"unresolved: {exc}"

    try:
        verdict = uninstall_state.remove_manifest_tree(baseline, path)
    except OSError as exc:
        return f"unresolved: {exc}"
    if verdict == uninstall_state.TREE_MODIFIED:
        return (
            "unresolved: tree changed since install — something was added or "
            "edited inside it, so it was left in place rather than removed "
            "wholesale; remove it by hand if you are sure"
        )
    if verdict == uninstall_state.TREE_UNRECORDED:
        return (
            "unresolved: no post-install manifest recorded for this tree, so "
            "it cannot be proven unmodified — left in place; remove it by "
            "hand, or re-run install.sh to record one"
        )
    return "ok"


def execute_directory_phase(
    deps: Deps,
    ctx: UninstallContext,
    baseline: uninstall_state.Baseline,
    report: dict[str, uninstall_state.Classification],
    ledger: uninstall_state.UninstallLedger,
) -> None:
    """Execute every owned ``directory:`` action, deepest-path-first.

    Deepest-first so a parent directory is only empty-checked after its own
    contents have already been processed this same run.
    """
    done = ledger.completed_keys()
    wholesale_dirs = _wholesale_removal_directories(deps, ctx)
    manifest_dirs = _tree_manifest_directories(ctx)
    owned_dirs = [
        key
        for key, c in report.items()
        if c.bucket == uninstall_state.BUCKET_OWNED
        and uninstall_state.key_type(key) == "directory"
        and c.action == uninstall_state.ACTION_REMOVE
        and key not in done
    ]

    def _depth(key: str) -> int:
        return len(Path(key.partition(":")[2]).parts)

    for key in sorted(owned_dirs, key=_depth, reverse=True):
        path = Path(key.partition(":")[2])
        if path in manifest_dirs:
            outcome = _remove_tree_manifest_directory(deps, baseline, path)
        else:
            outcome = _execute_remove_directory(
                deps, path, wholesale=path in wholesale_dirs
            )
        ledger.record(key, uninstall_state.ACTION_REMOVE, outcome)


def execute_runtime_phase(
    deps: Deps,
    ctx: UninstallContext,
    report: dict[str, uninstall_state.Classification],
    ledger: uninstall_state.UninstallLedger,
) -> None:
    """Remove the NVM root wholesale, if owned and not already done."""
    key = uninstall_state.runtime_key(ctx.home / ".nvm")
    if key in ledger.completed_keys():
        return
    c = report.get(key)
    if (
        c is None
        or c.bucket != uninstall_state.BUCKET_OWNED
        or c.action != uninstall_state.ACTION_REMOVE
    ):
        return
    outcome = _execute_remove_directory(deps, ctx.home / ".nvm", wholesale=True)
    ledger.record(key, uninstall_state.ACTION_REMOVE, outcome)


_REMOVAL_COMMANDS: dict[str, Callable[[str], list[str]]] = {
    "apt": uninstall_state.apt_remove_command,
    "dnf": uninstall_state.dnf_remove_command,
    "npm": uninstall_state.npm_uninstall_command,
    "uv-tool": uninstall_state.uv_tool_uninstall_command,
}
_RDEPENDS_COMMANDS: dict[str, Callable[[str], list[str]]] = {
    "apt": uninstall_state.apt_rdepends_command,
    "dnf": uninstall_state.dnf_whatrequires_command,
}
_DOWNGRADE_COMMANDS: dict[str, Callable[[str, str], list[str]]] = {
    "apt": uninstall_state.apt_downgrade_command,
    "dnf": uninstall_state.dnf_downgrade_command,
}


def live_package_snapshots(
    deps: Deps,
    baseline: uninstall_state.Baseline,
) -> dict[str, dict[str, str] | None]:
    """Fresh probe results for every manager appearing in recorded transactions."""
    managers = {t.get("manager") for t in baseline.transactions if t.get("manager")}
    return {str(m): deps.capture_package_snapshot(str(m)) for m in managers}


def _execute_package_removal(deps: Deps, manager: str, name: str) -> str:
    builder = _REMOVAL_COMMANDS.get(manager)
    if builder is None:
        return f"unresolved: no removal command for manager {manager!r}"
    if deps.run_command(builder(name)).ok:
        return "ok"
    return f"unresolved: {manager} removal failed for {name}"


def _execute_dependency_removal(deps: Deps, manager: str, name: str) -> str:
    """Remove an introduced dependency, gated on an explicitly-empty rdepends probe.

    Never a broad autoremove — only ever this one named package, and only
    once its own probe proves nothing else installed still depends on it.
    """
    probe_builder = _RDEPENDS_COMMANDS.get(manager)
    if probe_builder is None:
        return f"unresolved: no reverse-dependency probe for manager {manager!r}"
    result = deps.run_command(probe_builder(name), capture=True)
    verdict = uninstall_state.classify_rdepends_result(
        result.ok, result.stdout, manager
    )
    if verdict != "removable":
        return f"unresolved: reverse-dependency probe {verdict}"
    return _execute_package_removal(deps, manager, name)


def _execute_downgrade(
    deps: Deps, baseline: uninstall_state.Baseline, manager: str, name: str
) -> str:
    """Try each downgrade candidate in order (earliest first, per the ladder).

    Returns ``"halt: ..."`` only for the one named exception to this
    installer's general no-abort convention: a downgrade command that ran
    and left the package manager's own reported state different from both
    the pre-attempt and target versions — state left genuinely uncertain
    mid-operation, per the plan's "changed-state-then-failed" rule.
    """
    candidates = uninstall_state.downgrade_candidates(baseline, manager, name)
    builder = _DOWNGRADE_COMMANDS.get(manager)
    if not candidates or builder is None:
        return "unresolved: no recorded downgrade target for this package"
    pre = deps.capture_package_snapshot(manager)
    for version in candidates:
        if deps.run_command(builder(name, version)).ok:
            return "ok"
        post = deps.capture_package_snapshot(manager)
        if (
            pre is not None
            and post is not None
            and post.get(name) != pre.get(name)
            and post.get(name) != version
        ):
            return "halt: changed-state-then-failed downgrade"
        pre = post
    return "unresolved: downgrade ladder exhausted, no safe version installed"


def execute_package_phase(
    deps: Deps,
    ctx: UninstallContext,
    baseline: uninstall_state.Baseline,
    ledger: uninstall_state.UninstallLedger,
) -> bool:
    """Remove/downgrade owned packages, reverse transactions order.

    Returns False only when a changed-state-then-failed downgrade halted
    the phase — callers must skip the subsequent runtime/shared-state
    phase too when this happens, per the plan's explicit exception to the
    general no-abort/report-skips convention.
    """
    done = ledger.completed_keys()
    pending_managers = {
        uninstall_state.transaction_from_dict(t).manager
        for t in baseline.transactions
        if any(
            uninstall_state.package_key(t.get("manager", ""), name) not in done
            for name in (
                *uninstall_state.transaction_from_dict(t).requested,
                *uninstall_state.transaction_from_dict(t).introduced(),
            )
        )
    }
    snapshots = {m: deps.capture_package_snapshot(m) for m in pending_managers}
    for c in uninstall_state.classify_package_transactions(baseline, snapshots):
        if c.key in done or c.bucket != uninstall_state.BUCKET_OWNED:
            continue
        if c.action == uninstall_state.ACTION_DOWNGRADE:
            outcome = _execute_downgrade(deps, baseline, c.manager, c.name)
            ledger.record(c.key, c.action, outcome)
            if outcome.startswith("halt:"):
                return False
        elif c.reason == "introduced as a dependency by this transaction":
            ledger.record(
                c.key, c.action, _execute_dependency_removal(deps, c.manager, c.name)
            )
        else:
            ledger.record(
                c.key, c.action, _execute_package_removal(deps, c.manager, c.name)
            )
    return True


def execute_uninstall(
    deps: Deps,
    ctx: UninstallContext,
    baseline: uninstall_state.Baseline,
    report: dict[str, uninstall_state.Classification],
) -> uninstall_state.UninstallLedger:
    """Perform every safe ``owned`` action, retry-safe via the uninstall ledger.

    Order: services stop/disable first, then the global git hooksPath
    restore, then file/symlink restore-or-remove, then directories
    deepest-first, then packages in reverse transaction order, then the NVM
    runtime last. A changed-state-then-failed downgrade halts the package
    phase and skips the runtime phase too.
    """
    ledger = uninstall_state.UninstallLedger(
        uninstall_state.uninstall_ledger_path(ctx.state_dir)
    )
    execute_service_phase(deps, ctx, baseline, ledger)
    execute_gitconfig_phase(deps, ctx, baseline, ledger)
    execute_file_symlink_phase(deps, ctx, baseline, report, ledger)
    execute_directory_phase(deps, ctx, baseline, report, ledger)
    if execute_package_phase(deps, ctx, baseline, ledger):
        execute_runtime_phase(deps, ctx, report, ledger)
    return ledger


def _finalize_uninstall_state(deps: Deps, ctx: UninstallContext) -> None:
    """After a fully successful uninstall: release the lock and delete state.

    Deletes baseline snapshots, ``baseline.json``, ``history.jsonl``,
    ``uninstall.jsonl``, the profile marker, and the state directory itself
    if it's now empty. Only called when zero unresolved/drifted items
    remain — a partial uninstall retains everything for a retry.

    The lock is held for the whole of that sweep and released last: releasing
    first would let a second run take the lock and load a baseline this call
    is in the middle of deleting. The lock file itself is never unlinked here
    — ``release_uninstall_lock`` owns it, and it removes it only while
    holding the lock — so a lock a second run takes after that point survives.

    Also makes a best-effort (non-ledger, never-blocking) sweep of the
    state directory's own now-possibly-empty ancestors — ``~/.local/state``
    and ``~/.local`` — since the generic directory phase deliberately never
    touches them (see ``_is_state_dir_or_its_ancestor``) precisely because
    their emptiness could only ever be known *after* this cleanup runs.
    """
    lock_path = uninstall_state.uninstall_lock_path(ctx.state_dir)
    for path in _uninstall_state_paths(deps, ctx.state_dir):
        if path == lock_path:
            continue
        path.unlink(missing_ok=True)
    ctx.manifest.path.unlink(missing_ok=True)
    ctx.profile_marker.unlink(missing_ok=True)
    uninstall_state.release_uninstall_lock(ctx.state_dir)
    if ctx.state_dir.is_dir() and not any(ctx.state_dir.iterdir()):
        # A second run can take the lock between the check and this call; the
        # directory is then legitimately in use again.
        with contextlib.suppress(OSError):
            ctx.state_dir.rmdir()

    ancestor = ctx.state_dir.parent
    while ancestor != ctx.home and ctx.home in ancestor.parents:
        try:
            if not ancestor.is_dir() or any(ancestor.iterdir()):
                break
            ancestor.rmdir()
        except OSError:
            break
        ancestor = ancestor.parent


def do_uninstall(deps: Deps, ctx: UninstallContext) -> int:
    """Preview and execute a pristine-state uninstall.

    Implements the zero-evidence refusal, the four-bucket preflight
    report, the confirmation/exit-code contract, retryable execution via
    the uninstall ledger, and advisory-lock acquisition/release from
    Implementation Sequence steps 3 and 4. The classifier is deliberately
    conservative (see ``uninstall_state.classify_ownership_key`` and its two named
    reclassification overrides) — anything it can't classify with
    confidence lands in ``unresolved`` rather than being guessed at, so
    this only ever mutates what preflight already reported as ``owned``.
    Package removal and service/linger handling are not implemented yet
    (see ``execute_uninstall``'s docstring).
    """
    baseline_file = uninstall_state.baseline_path(ctx.state_dir)
    if not baseline_file.is_file():
        print(
            deps.palette.error(
                f"no baseline at {baseline_file} — nothing to uninstall from"
            ),
            file=sys.stderr,
        )
        print(
            deps.palette.error(
                "for a guaranteed pristine reset, see the WSL unregister/recreate "
                "instructions in README.md"
            ),
            file=sys.stderr,
        )
        return 2

    # A real (non-dry-run) run with no --yes and no keyboard to confirm on
    # is a guaranteed refusal no matter what the preflight report says, so
    # check for it before printing that report — otherwise the error that
    # actually matters ends up buried under a 100+ line dump the user can't
    # act on anyway.
    if not ctx.opts.dry_run and not ctx.opts.yes and not sys.stdin.isatty():
        print(
            deps.palette.error("refusing a non-interactive real run without --yes"),
            file=sys.stderr,
        )
        return 2

    report = build_preflight_report(deps, ctx)
    if report is None:
        print(
            deps.palette.error(
                f"no baseline at {baseline_file} — nothing to uninstall from"
            ),
            file=sys.stderr,
        )
        return 2
    package_report = build_package_preflight(deps, ctx) or []
    guard_annotations = _vscode_guard_preflight_annotations(deps, ctx, report)

    _print_preflight_report(
        deps,
        report,
        package_report,
        quiet=ctx.opts.quiet,
        guard_annotations=guard_annotations,
    )

    if ctx.opts.dry_run:
        print(deps.palette.header("Dry run complete — nothing was changed."))
        return 0

    if not ctx.opts.yes:
        print()
        print("Type UNINSTALL to proceed: ", end="", flush=True)
        if (
            _read_confirmation_token(
                deps,
            )
            != "UNINSTALL"
        ):
            print(
                deps.palette.error("confirmation not received — aborting"),
                file=sys.stderr,
            )
            return 2

    acquired, stale = uninstall_state.acquire_uninstall_lock(ctx.state_dir)
    if not acquired:
        print(
            deps.palette.error(
                "another --uninstall is already running on this machine"
            ),
            file=sys.stderr,
        )
        return 2
    if stale is not None:
        print(
            deps.palette.warn(
                f"reclaimed a stale uninstall lock (was held by pid {stale.pid})"
            )
        )

    try:
        baseline = uninstall_state.load_baseline(ctx.state_dir)
        if baseline is None:
            print(
                deps.palette.error(
                    f"no baseline at {baseline_file} — nothing to uninstall from"
                ),
                file=sys.stderr,
            )
            return 2

        ledger = execute_uninstall(deps, ctx, baseline, report)
        failed = [
            e
            for e in ledger.entries()
            if str(e.get("outcome", "")).startswith(("unresolved", "halt"))
        ]
        unresolved_keys = [
            key
            for key, c in report.items()
            if c.bucket
            in (uninstall_state.BUCKET_UNRESOLVED, uninstall_state.BUCKET_DRIFTED)
        ] + [
            c.key
            for c in package_report
            if c.bucket
            in (uninstall_state.BUCKET_UNRESOLVED, uninstall_state.BUCKET_DRIFTED)
        ]

        if not failed and not unresolved_keys:
            _finalize_uninstall_state(deps, ctx)
            print(
                deps.palette.header(
                    "Uninstall complete — no installer footprint remains."
                )
            )
            return 0

        # Preflight explains why something was never *attempted*; these are
        # the ones that were attempted and did not complete. Their reasons
        # only ever reached uninstall.jsonl, so a run that deliberately
        # preserved something — a tree the user added to, a package still
        # depended on — looked identical to an unexplained failure.
        if failed:
            print(deps.palette.warn(f"  attempted but not completed ({len(failed)}):"))
            for entry in failed:
                reason = str(entry.get("outcome", "")).partition(": ")[2]
                print(deps.palette.warn(f"    {entry.get('key', '')} — {reason}"))

        print(
            deps.palette.warn(
                f"⚠ uninstall incomplete — {len(failed) + len(unresolved_keys)} "
                "item(s) remain unresolved (see the preflight report above); "
                "re-run --uninstall to retry"
            )
        )
        return 1
    finally:
        uninstall_state.release_uninstall_lock(ctx.state_dir)
