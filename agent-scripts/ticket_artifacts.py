#!/usr/bin/env python3
"""ticket_artifacts.py — find and file away untracked files in a ticket worktree.

Personal, never-committed files (runbooks, demo decks, screenshots) collect
as untracked files in ticket worktrees. Left there, they block
``git worktree remove`` and dev_status's review/approve dirty check, or get
lost to ``--force``. This script finds them, proposes a label for each, and
moves the ones the caller names into a per-ticket folder outside the repo.
The shunt-artifacts skill owns the conversation; this script owns
classification and moves.

scan    Print JSON: the worktree's ticket key and its source, the
        destination folder, and every untracked file (git's own ignore
        rules apply) with a proposed label: ``code`` (should probably be
        committed), ``artifact`` (move it out), or ``unknown``.
move    Move the named untracked files into the destination, keeping
        their paths relative to the worktree root. Prints one JSON result
        per file: ``moved``, ``skipped-collision`` (the destination already
        has that path; nothing is overwritten), or ``error``.

Nothing here deletes a file. A move hard-links the target, or copies into
an exclusively created file where linking is impossible (across devices, or
a ``/mnt/c`` OneDrive root under WSL), then unlinks the source. Either way an
existing target is never replaced, even one created mid-move. The ticket key
must be a single folder name, and the root may not sit inside the worktree.

Ticket key, first match wins: ``--key``; ``branch_key_regex`` matched
against the branch (``PROJ-1234-retry-webhooks`` gives key ``PROJ-1234``,
description ``retry-webhooks``); the worktree's backlog provenance marker
(the slug is the whole folder name). Otherwise ``key_source`` is ``none``.

Destination: ``<root>/<KEY>-<description>``. An existing ``<KEY>`` or
``<KEY>-*`` folder under the root is reused whatever its description; more
than one is an error. A branch that is only the key has no description, so
a new folder needs ``--description``.

Usage
  ticket_artifacts.py scan [--worktree PATH] [--root DIR] [--key KEY]
                           [--description SLUG]
  ticket_artifacts.py move [--worktree PATH] [--root DIR] [--key KEY]
                           [--description SLUG] FILE...

Environment
  TICKET_ARTIFACTS_ROOT   destination root; --root overrides it, and it
                          overrides the config file's ``root``.

Files read: <toolkit-root>/data/artifacts/config.json (optional) —
  ``{"root": DIR, "branch_key_regex": RE, "artifact_globs": [...],
  "code_globs": [...]}``; every key is optional. Globs match the file name,
  or the whole relative path when the glob contains ``/``. ``code_globs``
  is checked first.
Files written: only the moved files, under the destination root.

Exit codes
  0 scan succeeded / every file moved; 1 move skipped or failed at least
  one file; 2 usage or setup error (not a git worktree, bad config, no
  root, no key, no description, ambiguous destination).
"""

from __future__ import annotations

import argparse
import errno
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import agent_toolkit_paths
import cli_common
import worktree_provenance

# What this module does with toolkit data (checked by scripts/check_toolkit_paths.py).
TOOLKIT_DATA = "reader"

ENV_ROOT = "TICKET_ARTIFACTS_ROOT"
GIT_TIMEOUT = 10.0
DEFAULT_BRANCH_KEY_REGEX = r"^[A-Z][A-Z0-9]+-\d+"
DEFAULT_ARTIFACT_GLOBS = (
    "*.md",
    "*.pptx",
    "*.docx",
    "*.pdf",
    "*.png",
    "*.jpg",
    "*.gif",
    "*.mp4",
    "screenshots/**",
)
DEFAULT_CODE_GLOBS = (
    "*.py",
    "*.ts",
    "*.tsx",
    "*.js",
    "*.go",
    "*.java",
    "*.cs",
    "*.sh",
    "*.sql",
    "*.json",
    "*.toml",
    "*.yaml",
    "*.yml",
)


class SetupError(Exception):
    """A usage or environment problem that stops the command (exit 2)."""


@dataclass
class Config:
    root: Path | None = None
    branch_key_regex: str = DEFAULT_BRANCH_KEY_REGEX
    artifact_globs: tuple[str, ...] = DEFAULT_ARTIFACT_GLOBS
    code_globs: tuple[str, ...] = DEFAULT_CODE_GLOBS


@dataclass
class Plan:
    """Everything ``scan`` reports and ``move`` acts on."""

    worktree: Path
    branch: str | None
    ticket_key: str | None
    key_source: str
    description: str | None
    root: Path | None
    destination: Path | None
    error: str | None
    files: list[dict[str, str]] = field(default_factory=list)


def config_path() -> Path:
    return agent_toolkit_paths.path_for("ticket-artifacts") / "config.json"


def _str_tuple(
    data: dict[str, object], key: str, default: tuple[str, ...]
) -> tuple[str, ...]:
    value = data.get(key, default)
    if not isinstance(value, list | tuple) or not all(
        isinstance(item, str) for item in value
    ):
        raise SetupError(f"config {key} must be a list of strings")
    return tuple(value)


def load_config(path: Path) -> Config:
    """Read the config file; a missing file means all defaults."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return Config()
    except OSError as exc:
        raise SetupError(f"cannot read {path}: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SetupError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise SetupError(f"{path} must hold a JSON object")
    root = data.get("root")
    if root is not None and (not isinstance(root, str) or not root):
        raise SetupError("config root must be a non-empty string")
    regex = data.get("branch_key_regex", DEFAULT_BRANCH_KEY_REGEX)
    if not isinstance(regex, str):
        raise SetupError("config branch_key_regex must be a string")
    try:
        re.compile(regex)
    except re.error as exc:
        raise SetupError(f"config branch_key_regex is invalid: {exc}") from exc
    return Config(
        root=Path(root).expanduser() if root else None,
        branch_key_regex=regex,
        artifact_globs=_str_tuple(data, "artifact_globs", DEFAULT_ARTIFACT_GLOBS),
        code_globs=_str_tuple(data, "code_globs", DEFAULT_CODE_GLOBS),
    )


def resolve_root(flag: str | None, config: Config) -> Path | None:
    """--root > TICKET_ARTIFACTS_ROOT > config ``root`` > unset."""
    if flag:
        return Path(flag).expanduser()
    env = os.environ.get(ENV_ROOT)
    if env:
        return Path(env).expanduser()
    return config.root


def _git(worktree: Path, *args: str) -> str | None:
    """Run git in the worktree; stdout, or None on any failure."""
    try:
        result = subprocess.run(
            ["git", "-C", str(worktree), *args],
            capture_output=True,
            timeout=GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    # fsdecode, not text mode: a filename may hold any byte (a CR, invalid
    # UTF-8), and it must round-trip back to the same path on disk.
    return os.fsdecode(result.stdout)


def worktree_root(path: Path) -> Path:
    out = _git(path, "rev-parse", "--show-toplevel")
    # Only the trailing newline: a directory name may end in a space.
    top = out.removesuffix("\n") if out else ""
    if not top:
        raise SetupError(f"{path} is not inside a git worktree")
    return Path(top).resolve()


def current_branch(worktree: Path) -> str | None:
    out = _git(worktree, "symbolic-ref", "--short", "-q", "HEAD")
    return out.strip() or None if out else None


def untracked_files(worktree: Path) -> list[str]:
    """Every untracked, non-ignored file, relative to the worktree root.

    ``ls-files --others`` lists each file inside an untracked directory
    rather than the directory itself.
    """
    out = _git(worktree, "ls-files", "--others", "--exclude-standard", "-z")
    if out is None:
        raise SetupError(f"git ls-files failed in {worktree}")
    return sorted(entry for entry in out.split("\0") if entry)


def classify(path: str, config: Config) -> str:
    """``code`` before ``artifact``; everything else is ``unknown``."""

    def matches(globs: tuple[str, ...]) -> bool:
        name = path.rsplit("/", 1)[-1]
        return any(
            fnmatch.fnmatchcase(path if "/" in glob else name, glob) for glob in globs
        )

    if matches(config.code_globs):
        return "code"
    if matches(config.artifact_globs):
        return "artifact"
    return "unknown"


def kebab(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def resolve_key(
    worktree: Path, branch: str | None, key_flag: str | None, config: Config
) -> tuple[str | None, str, str | None]:
    """(key, key_source, description from the branch)."""
    if key_flag:
        return key_flag, "flag", None
    if branch:
        match = re.search(config.branch_key_regex, branch)
        if match and match.group(0):
            rest = kebab(branch[match.end() :])
            return match.group(0), "branch", rest or None
    slug = worktree_provenance.read_marker_for_worktree(worktree)
    if slug:
        return slug, "marker", None
    return None, "none", None


def _is_folder_name(name: str) -> bool:
    """One path component: a key or slug can never climb out of the root."""
    return name not in ("", ".", "..") and not any(c in name for c in "/\\\0")


def resolve_destination(
    root: Path, key: str, key_source: str, description: str | None
) -> tuple[Path | None, str | None]:
    """(destination, error). A marker slug is the whole folder name; a
    ticket key reuses any existing ``<KEY>``/``<KEY>-*`` folder."""
    if key_source == "marker":
        return root / key, None
    existing = (
        sorted(
            child
            for child in root.iterdir()
            if child.is_dir()
            and (child.name == key or child.name.startswith(f"{key}-"))
        )
        if root.is_dir()
        else []
    )
    if len(existing) > 1:
        names = ", ".join(child.name for child in existing)
        return None, f"more than one folder for {key} under {root}: {names}"
    if existing:
        return existing[0], None
    if not description:
        return None, f"no description for {key}; pass --description"
    return root / f"{key}-{description}", None


def build_plan(args: argparse.Namespace) -> Plan:
    config = load_config(config_path())
    worktree = worktree_root(Path(args.worktree))
    branch = current_branch(worktree)
    key, key_source, description = resolve_key(worktree, branch, args.key, config)
    if args.description:
        description = kebab(args.description)
        if not description:
            raise SetupError("--description has no letters or digits")
    root = resolve_root(args.root, config)
    destination: Path | None = None
    error: str | None = None
    if root is None:
        error = (
            f"no destination root: set {ENV_ROOT}, --root, or root in {config_path()}"
        )
    elif root.resolve().is_relative_to(worktree):
        error = f"destination root {root} is inside the worktree"
    elif key is None:
        error = "no ticket key from the branch or a backlog marker; pass --key"
    elif not _is_folder_name(key):
        error = f"ticket key {key!r} is not a single folder name"
    else:
        destination, error = resolve_destination(root, key, key_source, description)
    files = [
        {"path": path, "label": classify(path, config)}
        for path in untracked_files(worktree)
    ]
    return Plan(
        worktree=worktree,
        branch=branch,
        ticket_key=key,
        key_source=key_source,
        description=description,
        root=root,
        destination=destination,
        error=error,
        files=files,
    )


def cmd_scan(args: argparse.Namespace) -> int:
    plan = build_plan(args)
    print(
        json.dumps(
            {
                "worktree": str(plan.worktree),
                "branch": plan.branch,
                "ticket_key": plan.ticket_key,
                "key_source": plan.key_source,
                "description": plan.description,
                "root": str(plan.root) if plan.root else None,
                "destination": str(plan.destination) if plan.destination else None,
                "destination_exists": bool(
                    plan.destination and plan.destination.is_dir()
                ),
                "error": plan.error,
                "files": plan.files,
            },
            indent=2,
        )
    )
    return 0


# A hard link is impossible across filesystems, or on some (e.g. a WSL
# /mnt/c drive); copy into an exclusively created file instead.
_NO_LINK_ERRNOS = frozenset(
    {errno.EXDEV, errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EMLINK}
)


def place_exclusive(source: Path, target: Path) -> None:
    """Move ``source`` to ``target`` without ever replacing ``target``.

    Raises FileExistsError when ``target`` exists, even if it appeared after
    any earlier check: both the link and the ``xb`` open refuse atomically.
    """
    try:
        os.link(source, target, follow_symlinks=False)
    except OSError as exc:
        if exc.errno not in _NO_LINK_ERRNOS:
            raise
        if source.is_symlink():
            os.symlink(os.readlink(source), target)
        else:
            dst = target.open("xb")
            try:
                with source.open("rb") as src, dst:
                    shutil.copyfileobj(src, dst)
                shutil.copystat(source, target)
            except BaseException:
                # The target is ours (exclusively created): a partial copy
                # would turn every retry into a false collision.
                target.unlink(missing_ok=True)
                raise
    source.unlink()


def move_one(worktree: Path, destination: Path, rel: str) -> dict[str, str]:
    target = destination / rel
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        # A symlinked folder under the destination could lead anywhere,
        # including back into the worktree.
        if not target.parent.resolve().is_relative_to(destination.resolve()):
            return {
                "path": rel,
                "result": "error",
                "detail": f"{target.parent} resolves outside {destination}",
            }
        place_exclusive(worktree / rel, target)
    except FileExistsError:
        return {"path": rel, "result": "skipped-collision", "target": str(target)}
    except OSError as exc:
        return {"path": rel, "result": "error", "detail": str(exc)}
    return {"path": rel, "result": "moved", "target": str(target)}


def cmd_move(args: argparse.Namespace) -> int:
    plan = build_plan(args)
    if plan.error or plan.destination is None:
        raise SetupError(plan.error or "no destination")
    untracked = {entry["path"] for entry in plan.files}
    results = []
    for raw in args.files:
        rel = Path(os.path.normpath(raw)).as_posix()
        if rel not in untracked:
            results.append(
                {"path": raw, "result": "error", "detail": "not an untracked file"}
            )
            continue
        results.append(move_one(plan.worktree, plan.destination, rel))
    print(
        json.dumps({"destination": str(plan.destination), "results": results}, indent=2)
    )
    return 0 if all(r["result"] == "moved" for r in results) else 1


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--worktree", default=".", help="worktree to inspect (default: cwd)"
    )
    parser.add_argument(
        "--root", help=f"destination root (overrides {ENV_ROOT} and config)"
    )
    parser.add_argument(
        "--key", help="ticket key to use instead of the branch or backlog marker"
    )
    parser.add_argument(
        "--description",
        help="folder description for a new <KEY>-<description> folder (kebab-cased)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Find untracked files in a ticket worktree and move "
        "the ones you name into a per-ticket folder outside the repo."
    )
    cli_common.add_verbosity_args(parser)
    sub = parser.add_subparsers(dest="command", required=True)
    scan = sub.add_parser(
        "scan",
        help="list untracked files with proposed labels and the destination",
    )
    _add_common_args(scan)
    move = sub.add_parser(
        "move",
        help="move the named untracked files into the destination",
    )
    _add_common_args(move)
    move.add_argument(
        "files", nargs="+", help="untracked file paths, relative to the worktree root"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "scan":
            return cmd_scan(args)
        return cmd_move(args)
    except SetupError as exc:
        print(f"ticket_artifacts: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
