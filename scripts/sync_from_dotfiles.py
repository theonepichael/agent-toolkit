#!/usr/bin/env python3
"""sync_from_dotfiles.py — keep claude/CORE_INSTRUCTIONS.md current with dotfiles.

Not a harness-runtime script (no ``links.toml`` entry, never installed to a
harness config directory) — a repo-maintenance entrypoint run directly by
whoever maintains this toolkit, the same category as ``install.py`` /
``depart.py``.

Since the 2026-09-07 cutover (see ``MIGRATION.md``), the permanent upstream
contract is a single file: ``claude/CORE_INSTRUCTIONS.md`` is authored in
dotfiles and flows downstream into this toolkit — everything else here is
authored in this repo. This tool implements exactly that contract, replacing
the pre-cutover whole-repo diff reconciler (deleted; recoverable from git
history if a second upstream file ever appears):

1. read ``dotfiles@HEAD:claude/CORE_INSTRUCTIONS.md`` (read-only — dotfiles
   is never written to; HEAD is resolved once per run),
2. apply TRANSFORM — the one registered mechanical rewrite this toolkit
   needs (the backticked ``claude/scripts/`` token, which names dotfiles'
   own script directory, becomes ``agent-scripts/``, which names this
   repo's). Nothing else is rewritten; every ``~/.claude/scripts/``
   occurrence in command examples passes through untouched. The run counts
   substitutions and stops loudly on any count other than the designed
   cases — a repeatable tool never guesses,
3. compare with this repo's copy: identical → up to date, a true no-op
   (``--apply`` included: no sweep, no state write, tree untouched);
   different and not ``--apply`` → summarize the pending change and stop;
4. with ``--apply`` on differing content: write the transformed file, run
   the generator sweep so generated artifacts describe *this* repo, write
   provenance state, and stage exactly two paths — the contract file and
   the state file. Swept outputs (INTERFACES.md, generated skill docs, …)
   are deliberately left unstaged: the user reviews the full working-tree
   diff and commits it themselves — this tool never commits.

Provenance (``scripts/.sync-state.json``) is write-only bookkeeping: it
records the sha of the last dotfiles commit that touched the contract file
(not dotfiles HEAD — unrelated upstream commits never churn toolkit state)
and is never read back to gate anything. The sync decision is made purely
by comparing file content, so a stale, missing, or corrupt state file can
at worst cause one harmless state rewrite — never a wrong copy or a
skipped sync. The rewritten docstring above IS the user documentation:
``argparse --help`` derives from it and ``agent-scripts/gen_interfaces.py``
generates INTERFACES.md from it.

Flags: --apply, --dotfiles-path <path>, --quiet/-q, --verbose/-v.
Env vars: none.
Files read: dotfiles' ``claude/CORE_INSTRUCTIONS.md`` at HEAD via ``git
show`` (read-only); this repo's own ``claude/CORE_INSTRUCTIONS.md`` and
``scripts/.sync-state.json``.
Files written (only with --apply): this repo's
``claude/CORE_INSTRUCTIONS.md``, whatever the generator sweep
(``agent-scripts/gen_interfaces.py``, ``gen_skills.py``,
``gen_second_opinion.py``, ``gen_skills_params.py``) rewrites, and
``scripts/.sync-state.json``.
Exit codes: 0 clean (up to date, or report/apply succeeded); 1 contract
file missing from dotfiles@HEAD, the transform guard tripped, a generator
failed, or the dotfiles path is not a usable git checkout (nothing staged
and state not advanced in any of these cases); 2 bad usage.
"""

import argparse
import difflib
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent-scripts"))

import cli_common  # noqa: E402 — sibling dir inserted above

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DOTFILES_PATH = Path.home() / "dotfiles"

# The permanent post-cutover contract (MIGRATION.md): the one file authored
# upstream in dotfiles and synced into this toolkit.
CONTRACT_FILE = "claude/CORE_INSTRUCTIONS.md"

# The registered mechanical transform. The backtick delimiters are the
# anchor: they match the prose token naming dotfiles' own script directory
# and never the ``~/.claude/scripts/`` occurrences inside command examples,
# which name the deployed symlink farm and must pass through untouched.
TRANSFORM_OLD = "`claude/scripts/`"
TRANSFORM_NEW = "`agent-scripts/`"

# This repo's generators: run after any content sync so generated artifacts
# (INTERFACES.md, skill docs) describe *this* repo, never dotfiles'.
GENERATOR_SWEEP: tuple[str, ...] = (
    "agent-scripts/gen_interfaces.py",
    "agent-scripts/gen_skills.py",
    "agent-scripts/gen_second_opinion.py",
    "agent-scripts/gen_skills_params.py",
)


def state_path(repo_root: Path) -> Path:
    """Return the path to the committed sync-state marker."""
    return repo_root / "scripts" / ".sync-state.json"


def load_state(repo_root: Path) -> dict[str, object] | None:
    """Load the sync-state marker, or None if absent or corrupt.

    Provenance-only: a corrupt file is indistinguishable from no history —
    never a crash, never a gate (the sync decision compares content).
    """
    import json

    path = state_path(repo_root)
    if not path.is_file():
        return None
    try:
        return dict[str, object](json.loads(path.read_text(encoding="utf-8")))
    except (ValueError, OSError):
        return None


def write_state(repo_root: Path, *, dotfiles_sha: str) -> None:
    """Record provenance for a successful content sync.

    ``dotfiles_sha`` is the last dotfiles commit that touched
    ``CONTRACT_FILE`` (not dotfiles HEAD) — unrelated upstream commits must
    never churn this toolkit's state.
    """
    import json

    payload = {
        "last_synced_dotfiles_sha": dotfiles_sha,
        "synced_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    path = state_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


# ── git plumbing ─────────────────────────────────────────────────────────────


def run_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a git command in ``repo``, capturing output as text."""
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )


def resolve_head(repo: Path) -> str:
    """Return the current HEAD commit of ``repo``."""
    result = run_git(repo, "rev-parse", "HEAD")
    if result.returncode != 0:
        raise RuntimeError(f"git rev-parse HEAD failed in {repo}: {result.stderr}")
    return result.stdout.strip()


def path_exists_at(repo: Path, ref: str, path: str) -> bool:
    """Return whether ``path`` exists in ``repo`` at ``ref``."""
    return run_git(repo, "cat-file", "-e", f"{ref}:{path}").returncode == 0


def read_at(repo: Path, ref: str, path: str) -> bytes:
    """Return the raw bytes of ``path`` in ``repo`` at ``ref``."""
    result = subprocess.run(
        ["git", "-C", str(repo), "show", f"{ref}:{path}"],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode(errors="replace")
        raise RuntimeError(f"git show {ref}:{path} failed in {repo}: {stderr}")
    return result.stdout


def last_commit_touching(repo: Path, ref: str, path: str) -> str:
    """The sha of the last commit in ``repo`` (at ``ref``) touching ``path``.

    Falls back to ``ref`` when history answers empty (e.g. a shallow
    clone) — provenance is best-effort bookkeeping, never gating.
    """
    result = run_git(repo, "log", "-1", "--format=%H", ref, "--", path)
    sha = result.stdout.strip() if result.returncode == 0 else ""
    return sha or ref


def git_add(repo_root: Path, paths: list[str]) -> None:
    """Stage the given repo-relative paths."""
    result = run_git(repo_root, "add", "--", *paths)
    if result.returncode != 0:
        raise RuntimeError(f"git add failed in {repo_root}: {result.stderr}")


# ── transform and sweep ──────────────────────────────────────────────────────


def apply_transform(text: str) -> tuple[str, int]:
    """Apply the registered transform; return (new text, substitution count)."""
    count = text.count(TRANSFORM_OLD)
    return text.replace(TRANSFORM_OLD, TRANSFORM_NEW), count


def run_generator_sweep(
    repo_root: Path, sweep: tuple[str, ...], *, quiet: bool, verbose: bool
) -> None:
    """Run every generator in ``sweep`` so generated artifacts describe this repo."""
    for relpath in sweep:
        cli_common.vprint(f"running {relpath}", verbose=verbose)
        result = subprocess.run(
            [sys.executable, relpath],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"{relpath} failed (exit {result.returncode}):\n"
                f"{result.stdout}\n{result.stderr}"
            )
        cli_common.qprint(f"  {relpath}: ok", quiet=quiet)


# ── cli ──────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="sync_from_dotfiles",
        description=(
            "keep claude/CORE_INSTRUCTIONS.md current with dotfiles@HEAD "
            "(the one permanent post-cutover upstream relationship), then "
            "run the generator sweep; see the module docstring for the full "
            "contract"
        ),
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="apply the sync (default: report/diff only)",
    )
    parser.add_argument(
        "--dotfiles-path",
        metavar="<path>",
        default=None,
        help="path to the dotfiles checkout (default: ~/dotfiles)",
    )
    cli_common.add_verbosity_args(parser)
    return parser


def main(
    repo_root: Path = REPO_ROOT,
    argv: list[str] | None = None,
    *,
    do_exit: bool = True,
    generator_sweep: tuple[str, ...] = GENERATOR_SWEEP,
) -> int:
    """Parse argv, run the single-contract sync, and report or apply it.

    Returns the process exit code (0 clean; 1 failure; argparse raises
    ``SystemExit(2)`` itself on bad usage). With ``do_exit`` True (the CLI
    entrypoint), the code is also passed to ``sys.exit``.
    """

    def fail(message: str) -> int:
        print(f"[sync_from_dotfiles] ERROR: {message}", file=sys.stderr)
        if do_exit:
            raise SystemExit(1)
        return 1

    parser = build_parser()
    args = parser.parse_args(argv)

    dotfiles_path = (
        Path(args.dotfiles_path).expanduser()
        if args.dotfiles_path
        else DEFAULT_DOTFILES_PATH
    )

    try:
        tip = resolve_head(dotfiles_path)
    except RuntimeError as exc:
        return fail(f"{dotfiles_path} is not a usable git checkout: {exc}")

    if not path_exists_at(dotfiles_path, tip, CONTRACT_FILE):
        return fail(
            f"{CONTRACT_FILE} is missing from dotfiles@{tip[:12]} — the "
            "upstream contract is broken; nothing written"
        )
    try:
        upstream_text = read_at(dotfiles_path, tip, CONTRACT_FILE).decode("utf-8")
    except UnicodeDecodeError as exc:
        return fail(f"{CONTRACT_FILE} in dotfiles@{tip[:12]} is not valid UTF-8: {exc}")

    transformed, count = apply_transform(upstream_text)

    toolkit_file = repo_root / CONTRACT_FILE
    toolkit_text = (
        toolkit_file.read_text(encoding="utf-8") if toolkit_file.is_file() else None
    )

    if toolkit_text == transformed:
        cli_common.qprint(
            f"[sync_from_dotfiles] up to date with dotfiles@{tip[:12]}",
            quiet=args.quiet,
        )
        if do_exit:
            raise SystemExit(0)
        return 0

    if count == 0:
        return fail(
            "the copies differ but the transform made 0 substitutions — "
            "upstream likely reworded away the backticked `claude/scripts/` "
            "token; review the upstream change and port it by hand (or "
            "adjust TRANSFORM) rather than blind-copying it"
        )
    if count > 1:
        return fail(
            f"the transform matched {count} occurrences of {TRANSFORM_OLD!r} "
            "(expected 1) — upstream prose changed in a way the transform "
            "was not designed for; review before syncing"
        )

    if not args.apply:
        diff = list(
            difflib.unified_diff(
                (toolkit_text or "").splitlines(),
                transformed.splitlines(),
                fromfile=f"toolkit:{CONTRACT_FILE}",
                tofile=f"dotfiles@{tip[:12]}:{CONTRACT_FILE}",
                lineterm="",
            )
        )
        cli_common.qprint(
            f"[sync_from_dotfiles] BASE(toolkit) → TIP(dotfiles@{tip[:12]}); "
            f"transform substitutions: {count}",
            quiet=args.quiet,
        )
        for line in diff[:40]:
            cli_common.qprint(f"  {line}", quiet=args.quiet)
        if len(diff) > 40:
            cli_common.qprint(f"  … {len(diff) - 40} more diff lines", quiet=args.quiet)
        cli_common.qprint(
            "[sync_from_dotfiles] report only — pass --apply to write",
            quiet=args.quiet,
        )
        if do_exit:
            raise SystemExit(0)
        return 0

    toolkit_file.write_text(transformed, encoding="utf-8")
    cli_common.qprint(
        f"[sync_from_dotfiles] synced {CONTRACT_FILE} from dotfiles@{tip[:12]} "
        f"(transform substitutions: {count})",
        quiet=args.quiet,
    )

    try:
        run_generator_sweep(
            repo_root, generator_sweep, quiet=args.quiet, verbose=args.verbose
        )
    except RuntimeError as exc:
        return fail(
            "the generator sweep failed partway; nothing staged, state NOT "
            "advanced — fix the generator and re-run. Generator output:\n"
            f"{exc}"
        )

    touching = last_commit_touching(dotfiles_path, tip, CONTRACT_FILE)
    if touching == tip:
        cli_common.vprint(
            "provenance: no dedicated upstream commit found for "
            f"{CONTRACT_FILE}; recording TIP {tip[:12]} as best effort",
            verbose=args.verbose,
        )
    write_state(repo_root, dotfiles_sha=touching)
    git_add(
        repo_root, [CONTRACT_FILE, str(state_path(repo_root).relative_to(repo_root))]
    )

    cli_common.qprint(
        f"[sync_from_dotfiles] applied; staged {CONTRACT_FILE} and "
        f"{state_path(repo_root).relative_to(repo_root)} (provenance: upstream "
        f"{touching[:12]}) — review the staged diff (swept outputs are "
        "deliberately unstaged) and commit it yourself; this tool does not "
        "commit on your behalf",
        quiet=args.quiet,
    )
    if do_exit:
        raise SystemExit(0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
