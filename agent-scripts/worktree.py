#!/usr/bin/env python3
"""worktree.py — automated worktree creation and dependency bootstrapping.

Provides a typed API and CLI tool to create or reuse git worktrees and
bootstrap project dependencies in a single step. Resolves repositories from
backlog item related_files, explicit CLI flags (--repo and --branch), or
the current working directory.

Usage:
    python3 ~/.claude/scripts/worktree.py <slug|N> [flags]
    python3 ~/.claude/scripts/worktree.py --repo <path> --branch <name> [flags]

Flags:
    --repo              Path to the git repository
    --branch            Branch name to create or attach
    --dest              Explicit target directory for the worktree
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

import cli_common


class WorktreeError(Exception):
    """Raised when worktree resolution, creation, or bootstrapping fails."""


@dataclass(frozen=True)
class WorktreeConfig:
    """Configuration for worktree creation and bootstrapping."""

    repo: Path
    branch: str
    worktree_path: Path
    skip_bootstrap: bool = False
    force: bool = False
    quiet: bool = False


@dataclass(frozen=True)
class WorktreeResult:
    """Outcome of a worktree creation or reuse operation."""

    worktree_path: Path
    branch: str
    reused: bool
    bootstrap_executed: bool
    bootstrap_command: list[str] | None = None
    diagnostics: tuple[str, ...] = ()


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
    skip_bootstrap: bool = False,
    force: bool = False,
    quiet: bool = False,
    items_path: Path | None = None,
) -> WorktreeConfig:
    """Resolve worktree target repository, branch name, and destination path.

    Resolves via explicit repository and branch, from backlog item related_files,
    or from the current working directory.
    """
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
        matched_item = resolve_backlog_item(slug_or_id, items_path=items_path)
        if matched_item is not None:
            slug = str(matched_item["id"])
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

    return WorktreeConfig(
        repo=repo_root,
        branch=branch_name,
        worktree_path=worktree_path,
        skip_bootstrap=skip_bootstrap,
        force=force,
        quiet=quiet,
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


def create_and_bootstrap_worktree(config: WorktreeConfig) -> WorktreeResult:
    """Create or reuse a git worktree and bootstrap dependencies."""
    reused = False
    diagnostics: list[str] = []

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
        else:
            cmd.extend([str(config.worktree_path), "-b", config.branch])

        res_add = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res_add.returncode != 0:
            error_detail = res_add.stderr.strip() or res_add.stdout.strip()
            raise WorktreeError(f"git worktree add failed: {error_detail}")

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
                skip_bootstrap=args.skip_bootstrap,
                force=args.force,
                quiet=args.quiet,
            )
            result = create_and_bootstrap_worktree(config)

            if not args.quiet:
                for diag in result.diagnostics:
                    print(f"[worktree] {diag}", file=sys.stderr)

            if args.json:
                payload = {
                    "worktree_path": str(result.worktree_path),
                    "branch": result.branch,
                    "reused": result.reused,
                    "bootstrap_executed": result.bootstrap_executed,
                    "bootstrap_command": result.bootstrap_command,
                    "diagnostics": list(result.diagnostics),
                }
                print(json.dumps(payload, indent=2))
            else:
                print(str(result.worktree_path))
        except WorktreeError as err:
            print(f"[worktree] error: {err}", file=sys.stderr)
            sys.exit(1)


if __name__ == "__main__":
    main()
