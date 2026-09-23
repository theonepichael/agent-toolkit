#!/usr/bin/env python3
"""worktree.py — automated worktree creation and dependency bootstrapping.

Provides a typed API and CLI tool to create or reuse git worktrees and
bootstrap project dependencies in a single step. Resolves repositories from
backlog item related_files, explicit CLI flags (--repo and --branch), or
the current working directory.

Usage:
    python3 ~/.agent-toolkit/scripts/worktree.py <slug|N> [flags]
    python3 ~/.agent-toolkit/scripts/worktree.py --repo <path> --branch <name> [flags]

Flags:
    --repo              Path to the git repository
    --branch            Branch name to create or attach
    --dest              Explicit target directory for the worktree
    --base              Start point for a newly created branch (default: the
                        item's integration_branch, else HEAD); must resolve
                        locally. Ignored, with a warning, when the branch
                        already exists
    --skip-bootstrap    Skip automatic dependency installation
    --force, -f         Pass --force to git worktree add
    --json              Emit structured result as JSON
    --quiet, -q         Suppress non-essential output
    --verbose, -v       Emit extra diagnostic messages to stderr

Exit codes:
    0: Success (worktree path printed to stdout)
    1: Worktree creation, resolution, or bootstrap failed
    2: Command-line syntax or argument error
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import cli_common
import worktree_provenance

# What this module does with toolkit data (checked by scripts/check_toolkit_paths.py).
TOOLKIT_DATA = "reader"
TOOLKIT_DATA_VIA = ("dev_status_storage",)


class WorktreeError(Exception):
    """Raised when worktree resolution, creation, or bootstrapping fails."""


BaseSource = Literal["--base", "integration_branch", "HEAD"]


@dataclass(frozen=True)
class WorktreeConfig:
    """Configuration for worktree creation and bootstrapping."""

    repo: Path
    branch: str
    worktree_path: Path
    skip_bootstrap: bool = False
    force: bool = False
    quiet: bool = False
    # Backlog item slug this worktree was created for, when a real item was
    # resolved (independent of how the repo was selected: `worktree.py <item>
    # --repo <repo>` is the documented multi-repo invocation and must still
    # stamp). None for ad-hoc --repo/--branch work.
    slug: str | None = None
    # Start point for a newly created branch: an explicit --base, else the
    # item's integration_branch, else None (git's implicit HEAD). Items that
    # land on an integration branch (e.g. release-1) must not fork from main.
    base: str | None = None
    base_source: BaseSource = "HEAD"


@dataclass(frozen=True)
class WorktreeResult:
    """Outcome of a worktree creation or reuse operation."""

    worktree_path: Path
    branch: str
    reused: bool
    bootstrap_executed: bool
    bootstrap_command: list[str] | None = None
    diagnostics: tuple[str, ...] = ()
    # Set only when this call created the branch; None on attach/reuse.
    base: str | None = None
    base_source: BaseSource | None = None
    base_commit: str | None = None
    warnings: tuple[str, ...] = ()


def result_payload(result: WorktreeResult) -> dict[str, object]:
    """JSON payload for a worktree result, shared by both CLIs."""
    return {
        "worktree_path": str(result.worktree_path),
        "branch": result.branch,
        "reused": result.reused,
        "bootstrap_executed": result.bootstrap_executed,
        "bootstrap_command": result.bootstrap_command,
        "diagnostics": list(result.diagnostics),
        "base": result.base,
        "base_source": result.base_source,
        "base_commit": result.base_commit,
        "warnings": list(result.warnings),
    }


def find_repo_for_path(path: Path) -> Path | None:
    """Find the enclosing git repository root for a given path."""
    p = path.resolve() if path.is_absolute() else (Path.cwd() / path).resolve()
    while not p.exists() and p != p.parent:
        p = p.parent
    if not p.exists():
        return None
    dir_to_check = p if p.is_dir() else p.parent
    try:
        res = subprocess.run(
            ["git", "-C", str(dir_to_check), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode == 0 and res.stdout.strip():
            return Path(res.stdout.strip()).resolve()
    except Exception:
        pass
    return None


def resolve_backlog_item(
    slug_or_id: str, items_path: Path | None = None
) -> dict[str, object] | None:
    """Look up a backlog item by slug or numeric position using dev_status_storage."""
    try:
        import dev_status_storage

        items = dev_status_storage.load_items(items_path)
    except Exception:
        return None

    for item in items:
        if item.get("id") == slug_or_id:
            return item

    if slug_or_id.isdigit():
        try:
            import dev_status_impl

            pending = dev_status_storage.load_pending()
            kind, resolved_slug = dev_status_impl.resolve_id(slug_or_id, items, pending)
            if kind == "backlog":
                for item in items:
                    if item.get("id") == resolved_slug:
                        return item
        except Exception:
            pass

    return None


def resolve_worktree_config(
    slug_or_id: str | None = None,
    *,
    repo: Path | str | None = None,
    branch: str | None = None,
    dest: Path | str | None = None,
    base: str | None = None,
    skip_bootstrap: bool = False,
    force: bool = False,
    quiet: bool = False,
    items_path: Path | None = None,
) -> WorktreeConfig:
    """Resolve worktree target repository, branch name, and destination path.

    Resolves via explicit repository and branch, from backlog item related_files,
    or from the current working directory. The new-branch start point is
    ``base`` when given, else the item's ``integration_branch``, else HEAD.
    """
    # Resolve the backlog item once, up front: the slug matters for the
    # provenance marker even when --repo selects the repository explicitly.
    matched_item = (
        resolve_backlog_item(slug_or_id, items_path=items_path)
        if slug_or_id is not None
        else None
    )
    item_slug = str(matched_item["id"]) if matched_item is not None else None

    repo_root: Path | None = None
    branch_name: str | None = None

    if repo is not None:
        repo_root = find_repo_for_path(Path(repo))
        if repo_root is None:
            raise WorktreeError(
                f"Specified path is not inside a git repository: {repo}"
            )
        branch_name = branch or slug_or_id
        if not branch_name:
            raise WorktreeError(
                "Branch name must be specified via --branch or item argument."
            )
    elif slug_or_id is not None:
        if matched_item is not None:
            slug = item_slug or str(matched_item["id"])
            branch_name = branch or slug
            repos: set[Path] = set()
            for rf in matched_item.get("related_files", []):
                if isinstance(rf, dict) and "path" in rf:
                    r = find_repo_for_path(Path(str(rf["path"])))
                    if r is not None:
                        repos.add(r)
            if len(repos) == 1:
                repo_root = next(iter(repos))
            elif len(repos) > 1:
                repo_list = ", ".join(str(r) for r in sorted(repos))
                raise WorktreeError(
                    f"Multiple project repos found in related_files: {repo_list}. "
                    "Pass --repo to specify which repository to use."
                )
            else:
                cwd_repo = find_repo_for_path(Path.cwd())
                if cwd_repo is not None:
                    repo_root = cwd_repo
                else:
                    raise WorktreeError(
                        f"No git repository found in related_files for item '{slug}', "
                        "and current directory is not a git repository. Pass --repo."
                    )
        else:
            cwd_repo = find_repo_for_path(Path.cwd())
            if cwd_repo is not None:
                repo_root = cwd_repo
                branch_name = branch or slug_or_id
            else:
                raise WorktreeError(
                    f"No backlog item matches '{slug_or_id}', and current directory "
                    "is not a git repository. Specify --repo and --branch."
                )
    else:
        raise WorktreeError(
            "Either a backlog item slug/id or --repo and --branch must be provided."
        )

    assert repo_root is not None
    assert branch_name is not None

    if dest is not None:
        worktree_path = Path(dest).resolve()
    else:
        repo_name = repo_root.name
        worktree_path = (repo_root.parent / f"{repo_name}-{branch_name}").resolve()

    base_source: BaseSource = "HEAD"
    integration_branch = (
        matched_item.get("integration_branch") if matched_item is not None else None
    )
    if base:
        base_source = "--base"
    elif isinstance(integration_branch, str) and integration_branch:
        base = integration_branch
        base_source = "integration_branch"
    else:
        base = None

    return WorktreeConfig(
        repo=repo_root,
        branch=branch_name,
        worktree_path=worktree_path,
        skip_bootstrap=skip_bootstrap,
        force=force,
        quiet=quiet,
        slug=item_slug,
        base=base,
        base_source=base_source,
    )


def bootstrap_worktree(
    worktree_path: Path, *, quiet: bool = False
) -> tuple[bool, list[str] | None, list[str]]:
    """Execute dependency bootstrapping for the target worktree.

    Tier 1: scripts/bootstrap-worktree.sh or .config/bootstrap.sh
    Tier 2: Lockfile / manifest heuristics (uv, bun, pnpm, npm, cargo, go)

    Returns (executed, command, diagnostics).
    Raises WorktreeError if an executed command exits non-zero.
    """
    diagnostics: list[str] = []

    # Tier 1: Repo bootstrap script
    tier1_script: Path | None = None
    candidate1 = worktree_path / "scripts" / "bootstrap-worktree.sh"
    candidate2 = worktree_path / ".config" / "bootstrap.sh"
    if candidate1.is_file():
        tier1_script = candidate1
    elif candidate2.is_file():
        tier1_script = candidate2

    if tier1_script is not None:
        cmd = ["bash", str(tier1_script)]
        try:
            rel_display = tier1_script.relative_to(worktree_path)
        except ValueError:
            rel_display = tier1_script
        diagnostics.append(f"Running repo bootstrap script: {rel_display}")
        out_dest = subprocess.DEVNULL if quiet else sys.stderr
        res = subprocess.run(
            cmd,
            cwd=str(worktree_path),
            stdout=out_dest,
            stderr=out_dest,
            check=False,
        )
        if res.returncode != 0:
            raise WorktreeError(
                f"Bootstrap script '{rel_display}' failed with exit code {res.returncode}"
            )
        return True, cmd, diagnostics

    # Tier 2: Lockfile heuristics
    tier2_commands: list[list[str]] = []
    # Python
    if (worktree_path / "uv.lock").exists() or (
        worktree_path / "pyproject.toml"
    ).exists():
        tier2_commands.append(["uv", "sync"])
    # JS / Node
    if (worktree_path / "bun.lockb").exists() or (worktree_path / "bun.lock").exists():
        tier2_commands.append(["bun", "install"])
    elif (worktree_path / "pnpm-lock.yaml").exists():
        tier2_commands.append(["pnpm", "install"])
    elif (worktree_path / "package-lock.json").exists():
        tier2_commands.append(["npm", "install"])
    # Rust
    if (worktree_path / "Cargo.lock").exists() or (
        worktree_path / "Cargo.toml"
    ).exists():
        tier2_commands.append(["cargo", "check"])
    # Go
    if (worktree_path / "go.mod").exists():
        tier2_commands.append(["go", "mod", "download"])

    if not tier2_commands:
        return False, None, diagnostics

    last_cmd = None
    for cmd in tier2_commands:
        tool = cmd[0]
        if shutil.which(tool) is None:
            diagnostics.append(
                f"Warning: '{tool}' not found on PATH; skipping '{' '.join(cmd)}'"
            )
            continue
        diagnostics.append(f"Running bootstrap command: {' '.join(cmd)}")
        out_dest = subprocess.DEVNULL if quiet else sys.stderr
        res = subprocess.run(
            cmd,
            cwd=str(worktree_path),
            stdout=out_dest,
            stderr=out_dest,
            check=False,
        )
        if res.returncode != 0:
            raise WorktreeError(
                f"Bootstrap command '{' '.join(cmd)}' failed with exit code {res.returncode}"
            )
        last_cmd = cmd

    return (last_cmd is not None), last_cmd, diagnostics


def _git_common_dir(target_dir: Path) -> Path | None:
    """Resolve git common dir for target_dir as an absolute Path."""
    res = subprocess.run(
        ["git", "-C", str(target_dir), "rev-parse", "--git-common-dir"],
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        return None
    raw = res.stdout.strip()
    if not raw:
        return None
    p = Path(raw)
    if not p.is_absolute():
        p = target_dir / p
    return p.resolve()


def _resolve_commit(repo: Path, ref: str) -> str | None:
    """Resolve ref to a commit SHA in repo, locally (never fetches)."""
    res = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "rev-parse",
            "--verify",
            "--quiet",
            f"{ref}^{{commit}}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    sha = res.stdout.strip()
    return sha if res.returncode == 0 and sha else None


def create_and_bootstrap_worktree(config: WorktreeConfig) -> WorktreeResult:
    """Create or reuse a git worktree and bootstrap dependencies."""
    reused = False
    diagnostics: list[str] = []
    warnings: list[str] = []
    base_used: str | None = None
    base_source_used: BaseSource | None = None
    base_commit: str | None = None

    if config.worktree_path.exists():
        # Validate that it is an existing worktree for config.repo on config.branch
        res_toplevel = subprocess.run(
            ["git", "-C", str(config.worktree_path), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
        )
        if (
            res_toplevel.returncode != 0
            or Path(res_toplevel.stdout.strip()).resolve()
            != config.worktree_path.resolve()
        ):
            raise WorktreeError(
                f"Target directory '{config.worktree_path}' exists but is not a git worktree."
            )

        # Check git common dir
        repo_common_path = _git_common_dir(config.repo)
        wt_common_path = _git_common_dir(config.worktree_path)
        if (
            repo_common_path is None
            or wt_common_path is None
            or repo_common_path != wt_common_path
        ):
            raise WorktreeError(
                f"Target directory '{config.worktree_path}' belongs to a different git repository."
            )

        # Check branch
        res_branch = subprocess.run(
            ["git", "-C", str(config.worktree_path), "branch", "--show-current"],
            capture_output=True,
            text=True,
            check=False,
        )
        current_branch = res_branch.stdout.strip()
        if current_branch != config.branch:
            raise WorktreeError(
                f"Target worktree '{config.worktree_path}' is on branch '{current_branch}', not '{config.branch}'."
            )

        reused = True
        diagnostics.append(f"Reusing existing worktree at {config.worktree_path}")
    else:
        # Branch existence check
        res_branch_check = subprocess.run(
            [
                "git",
                "-C",
                str(config.repo),
                "rev-parse",
                "--verify",
                "--quiet",
                f"refs/heads/{config.branch}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        branch_exists = res_branch_check.returncode == 0

        cmd = ["git", "-C", str(config.repo), "worktree", "add"]
        if config.force:
            cmd.append("--force")

        if branch_exists:
            cmd.extend([str(config.worktree_path), config.branch])
            if config.base is not None:
                note = (
                    f"branch '{config.branch}' already exists; "
                    f"{config.base_source} '{config.base}' not applied"
                )
                # An explicit --base asked for something that didn't happen;
                # an implied integration_branch persists on the item, so
                # warning on every resume would be noise.
                if config.base_source == "--base":
                    warnings.append(note)
                else:
                    diagnostics.append(note)
        else:
            base_ref = config.base if config.base is not None else "HEAD"
            base_commit = _resolve_commit(config.repo, base_ref)
            if base_commit is None:
                if config.base is None:
                    raise WorktreeError(
                        f"Cannot resolve HEAD in '{config.repo}' to branch from."
                    )
                raise WorktreeError(
                    f"Base '{config.base}' ({config.base_source}) does not resolve "
                    f"to a local commit in '{config.repo}'. If it exists only on a "
                    f"remote, create it locally first: git branch {config.base} "
                    f"origin/{config.base}"
                )
            cmd.extend(["-b", config.branch, str(config.worktree_path)])
            # Pass the resolved SHA, not the ref: validation and use can't
            # diverge, and git sets no upstream for the new item branch.
            if config.base is not None:
                cmd.append(base_commit)
            base_used = base_ref
            base_source_used = config.base_source

        res_add = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res_add.returncode != 0:
            error_detail = res_add.stderr.strip() or res_add.stdout.strip()
            raise WorktreeError(f"git worktree add failed: {error_detail}")
        if base_used is not None:
            origin = "default" if config.base is None else base_source_used
            diagnostics.append(
                f"Created branch '{config.branch}' from '{base_used}' ({origin})"
            )

    if config.slug:
        try:
            stamped = worktree_provenance.write_marker(
                config.worktree_path, config.slug
            )
        except OSError as err:
            raise WorktreeError(f"Failed to write provenance marker: {err}") from err
        if not stamped:
            raise WorktreeError(
                f"Failed to write provenance marker for '{config.slug}' in '{config.worktree_path}'."
            )

    bootstrap_executed = False
    bootstrap_cmd = None
    if not config.skip_bootstrap:
        bootstrap_executed, bootstrap_cmd, boot_diags = bootstrap_worktree(
            config.worktree_path, quiet=config.quiet
        )
        diagnostics.extend(boot_diags)

    return WorktreeResult(
        worktree_path=config.worktree_path.resolve(),
        branch=config.branch,
        reused=reused,
        bootstrap_executed=bootstrap_executed,
        bootstrap_command=bootstrap_cmd,
        diagnostics=tuple(diagnostics),
        base=base_used,
        base_source=base_source_used,
        base_commit=base_commit,
        warnings=tuple(warnings),
    )


def build_parser() -> argparse.ArgumentParser:
    """Build command-line parser for worktree.py."""
    parser = argparse.ArgumentParser(
        description="Automate worktree creation and dependency bootstrapping.",
    )
    cli_common.add_verbosity_args(parser)
    parser.add_argument(
        "item",
        nargs="?",
        default=None,
        help="Backlog item slug, numeric position, or branch name",
    )
    parser.add_argument(
        "--repo",
        default=None,
        help="Path to git repository",
    )
    parser.add_argument(
        "--branch",
        default=None,
        help="Branch name for worktree",
    )
    parser.add_argument(
        "--dest",
        default=None,
        help="Explicit destination path for the worktree",
    )
    parser.add_argument(
        "--base",
        default=None,
        help=(
            "Start point for a newly created branch "
            "(default: the item's integration_branch, else HEAD)"
        ),
    )
    parser.add_argument(
        "--skip-bootstrap",
        action="store_true",
        default=False,
        help="Skip dependency bootstrapping",
    )
    parser.add_argument(
        "--force",
        "-f",
        action="store_true",
        default=False,
        help="Pass --force to git worktree add",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        help="Emit structured result as JSON",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    """CLI entrypoint for worktree.py."""
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.item and not args.repo:
        parser.error("Either a backlog item slug/id or --repo must be provided.")

    with cli_common.timing_span("script", script="worktree"):
        try:
            config = resolve_worktree_config(
                slug_or_id=args.item,
                repo=args.repo,
                branch=args.branch,
                dest=args.dest,
                base=args.base,
                skip_bootstrap=args.skip_bootstrap,
                force=args.force,
                quiet=args.quiet,
            )
            result = create_and_bootstrap_worktree(config)

            if not args.quiet:
                for diag in result.diagnostics:
                    print(f"[worktree] {diag}", file=sys.stderr)
            for warning in result.warnings:
                print(f"[worktree] warning: {warning}", file=sys.stderr)

            if args.json:
                print(json.dumps(result_payload(result), indent=2))
            else:
                print(str(result.worktree_path))
        except WorktreeError as err:
            print(f"[worktree] error: {err}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
