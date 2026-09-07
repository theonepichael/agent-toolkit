#!/usr/bin/env python3
"""sync_from_dotfiles.py — replay dotfiles' harness changes onto this toolkit.

Not a harness-runtime script (no ``links.toml`` entry, never installed to a
harness config directory) — a repo-maintenance entrypoint run directly by
whoever maintains this toolkit, the same category as ``install.py`` /
``depart.py``. See the plan at
``~/.claude/data/grill/2026-09-03-atk-sync-script-promote-the-one-plan.md``
for the full design rationale (why ``BLOCKLIST`` and the conflict
classification rule live here as code, why this lives at ``scripts/`` and
not ``claude/scripts/``).

Same-path repo-specific docs (``NEVER_SYNCED_REPO_SPECIFIC``) are never
replayed in either direction — the toolkit's copy of each is intentionally
different from dotfiles' — and verify_invariants() additionally derives
any *other* same-path divergence from the sync lineage before any write,
so an unregistered divergence stops the run with guidance instead of a
blind copy clobbering toolkit-side content (or the incidental
BLOCKED_MODULES check halting with the wrong message).

Replays the range ``BASE..TIP`` of dotfiles' history onto this repo:

- ``BASE`` is read from ``scripts/.sync-state.json``'s
  ``last_synced_dotfiles_sha``, or supplied via ``--since`` on a first run
  or a recovery.
- ``TIP`` is dotfiles' current ``HEAD``, resolved once at the start of the
  run and never re-read — a commit landing in dotfiles mid-run cannot
  change what this run processes.

Phase 1 derives ``copy_set`` and ``conflict_set`` and checks invariants —
no writes happen in this phase, on any invocation. Phase 2 (only with
``--apply``) copies the plain, non-conflicting paths byte-identical from
dotfiles, skips paths classified as generated artifacts (the generator
sweep below regenerates those so they describe *this* repo), and applies
any conflict this tool has a registered handler for. Any conflict with
neither classification stops the whole run for hand resolution — a
repeatable tool must never guess. Copied/handled paths are ``git add``-ed
BEFORE the generator sweep runs: ``gen_interfaces.py`` enumerates
git-tracked files, so staging after the sweep silently drops brand-new
files from ``INTERFACES.md`` — this happened once during the one-off replay
this tool promotes into a permanent command.

This tool never commits on your behalf. Review the staged diff and commit
it yourself.

Flags: --apply, --since <sha>, --dotfiles-path <path>, --quiet/-q,
--verbose/-v.
Env vars: none.
Files read: <dotfiles>/** at BASE and TIP via ``git show``/``git diff``
(read-only — dotfiles is never written to); this repo's own git history;
``scripts/.sync-state.json``.
Files written (only with --apply): the copied paths under this repo, plus
whatever the generator sweep (``gen_interfaces.py``, ``gen_skills.py``,
``gen_second_opinion.py``, ``gen_skills_params.py``) rewrites, plus
``scripts/.sync-state.json``.
Exit codes: 0 clean (nothing to sync, or report/apply succeeded); 1 an
invariant failed, or a conflict has no registered classification (nothing
written in either case); 2 bad usage — no stored BASE and no --since given.
"""

import argparse
import fnmatch
import json
import subprocess
import sys
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent-scripts"))

import cli_common  # noqa: E402 — sibling dir inserted above

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DOTFILES_PATH = Path.home() / "dotfiles"

# Source-of-truth verdict (2026-09-04): this repo is authoritative for
# claude/commands/swarm.md (generated from templates/swarm.md.tmpl via
# gen_skills.py SWARM_PARAMS) and claude/scripts/herdr_delegate.py; the
# dotfiles-side originals are deleted under this verdict, so any
# dotfiles-side change to either path is an error to route back to this
# repo, never a delta to merge. One-transition note for the sync run whose
# BASE..TIP first contains dotfiles' deletion commit: verify_invariants()
# flags three paths with the "in copy_set but missing from dotfiles@TIP"
# invariant below — the two files above plus dotfiles' own
# test/test_herdr_delegate.py — and the correct fix at that moment is to
# add all three to BLOCKLIST for exactly that sync; once the run succeeds
# and .sync-state.json's BASE is at or past the deletion commit, remove
# the entries again -- the stale-entry guard checks existence at
# dotfiles@BASE, so leaving them in makes the NEXT run fail. Adding them
# pre-emptively (while dotfiles still has the files at today's BASE) would
# trip that same guard, which is why BLOCKLIST carries no entry for them
# yet.
#
# Category-B: deletions made deliberately when the toolkit snapshot was cut.
# Hand-maintained, not derived — which deletions were *deliberate* is intent,
# and a diff cannot express intent. verify_invariants() is the guard against
# an entry going stale (typo, or dotfiles resurrecting one of these itself).
BLOCKLIST: tuple[str, ...] = (
    "claude/scripts/watchcommit_activity.py",
    "claude/scripts/test_watchcommit.py",
    "claude/scripts/test_wc_guard.py",
    "claude/scripts/test_opencode_skills_sync.py",
    "claude/scripts/test_opencode_skills_sync_activity.py",
    "claude/scripts/opencode_skills_sync_activity.py",
    "shell/.poshtheme.omp.json",
    "test/test_shell_decoupling.py",
    "test/test_watchcommit_repo_default.py",
    # Removed deliberately in cdd9d54 together with dev_status_sync.py
    # itself (the sync of dev_status into the toolkit was retired); the
    # toolkit-side removal is intentional, so dotfiles' continued edits
    # to its own copy must never be replayed here.
    "claude/scripts/test_dev_status_sync.py",
)

# Modules/files a copied path must not reference at TIP — catches a copy
# that would silently resurrect a dependency on a pruned module.
BLOCKED_MODULES: tuple[str, ...] = (
    "watchcommit_activity",
    "opencode_skills_sync_activity",
    "test_wc_guard",
    "test_watchcommit",
    "test_shell_decoupling",
    ".poshtheme",
)

# dotfiles-only paths this toolkit deliberately never carries.
EXCLUDE: tuple[str, ...] = (
    "CHANGELOG.md",
    # This machine's personal-policy overlay and the dotfiles-only generator
    # that composes it with claude/CORE_INSTRUCTIONS.md into
    # claude/global-instructions.md -- agent-toolkit has no overlay to
    # compose against and symlinks CORE_INSTRUCTIONS.md directly instead.
    "claude/personal-overlay.md",
    "claude/scripts/gen_core_instructions.py",
    "claude/scripts/test_gen_core_instructions.py",
    # watchcommit itself and its githooks-global test -- dotfiles-side files
    # first added post-cutover (11c827b) that this toolkit never carries.
    # (The companion githooks fix in the same commit,
    # githooks-global/lib/no-commit-on-main.sh, IS shared and syncs.)
    "scripts/watchcommit.py",
    "test/test_no_commit_on_main.py",
    # Tests dotfiles' own links.toml coverage for claude/commands/ --
    # never in this toolkit's history; the toolkit covers its own command
    # links in its own suite. Dotfiles-only by construction, not a
    # toolkit-side deletion.
    "test/test_claude_command_links.py",
)

# Paths that exist at the same relative path in BOTH checkouts with
# intentionally different content -- AGENTS.md-style directory docs, each
# repo's own README/STYLE/config/installer/tests (the toolkit's tiny diffs
# in CORE_INSTRUCTIONS/test/AGENTS/STYLE are its claude/scripts/ →
# agent-scripts/ rename propagated through prose). A dotfiles change to one
# is never replayed onto this toolkit: port a wanted change by hand, as its
# own decision. fnmatch patterns over the whole repo-relative path (`*`
# crosses `/`, same mechanism as GENERATED_ARTIFACT_PATTERNS), so they match
# whole names, never substrings. Distinct from EXCLUDE, which holds
# dotfiles-only paths absent from this toolkit -- do not merge the two sets.
# Classification is not left to memory alone: verify_invariants() derives
# same-path divergence from the sync lineage before any write, and
# scripts/test_sync_from_dotfiles.py's RealCheckoutDerivationTests derives
# it against the real checkouts at test time.
NEVER_SYNCED_REPO_SPECIFIC: tuple[str, ...] = (
    "AGENTS.md",
    "*/AGENTS.md",
    "README.md",
    "STYLE.md",
    "claude/CORE_INSTRUCTIONS.md",
    "claude/settings.json",
    "githooks/pre-commit",
    "install.py",
    "links.toml",
    "opencode/opencode.jsonc",
    "pyproject.toml",
    "test/test_install.py",
    "test/test_lint.py",
    "uv.lock",
)

# Generator outputs: never copy these from dotfiles even when they conflict.
# The toolkit's own generator sweep (GENERATOR_SWEEP, below) is authoritative
# for what they contain in *this* repo — copying would ship a doc describing
# dotfiles' repo instead. Glob patterns, not a fixed file list, so a new
# skill's generated doc is classified correctly without an edit here.
GENERATED_ARTIFACT_PATTERNS: tuple[str, ...] = (
    "INTERFACES.md",
    "claude/scripts/contract_fingerprints.json",
    "claude/commands/*.md",
    "opencode/command/*.md",
    "opencode/skills/*/SKILL.md",
    "copilot/skills/*/SKILL.md",
    "agy/skills/*/SKILL.md",
    "pi/prompts/*.md",
    "pi/skills/*/SKILL.md",
    "templates/*.tmpl",
)

GENERATOR_SWEEP: tuple[str, ...] = (
    "claude/scripts/gen_interfaces.py",
    "claude/scripts/gen_skills.py",
    "claude/scripts/gen_second_opinion.py",
    "claude/scripts/gen_skills_params.py",
)

# Conflict paths with a verified, mechanical delta-apply rule (classification
# categories 2 and 3 — "toolkit's version is a deliberate subset" / "the
# dotfiles-side change is a fact also true of the toolkit"). Empty until a
# recurring conflict earns one: "apply only the semantically valid delta" is
# a judgment call a diff cannot express, so a handler is added here only
# after a maintainer has actually made that judgment once, by hand. Until
# then every conflict without a GENERATED_ARTIFACT_PATTERNS match is
# category 4 — stop and hand-resolve, the safe default.
ConflictHandler = Callable[[Path, Path, str, str], None]
CONFLICT_HANDLERS: dict[str, ConflictHandler] = {}


def state_path(repo_root: Path) -> Path:
    """Return the path to the committed sync-state marker."""
    return repo_root / "scripts" / ".sync-state.json"


def load_state(repo_root: Path) -> dict[str, object] | None:
    """Load the sync-state marker, or None if this repo has never synced."""
    path = state_path(repo_root)
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def write_state(
    repo_root: Path, *, dotfiles_sha: str, toolkit_commit: str | None
) -> None:
    """Record a successful sync as the new BASE for the next run.

    ``toolkit_commit`` is deliberately left for the caller to fill in after
    committing this run's changes — this tool never commits on your behalf,
    so it cannot know its own post-commit HEAD. Until something patches it
    in, resolve_toolkit_anchor() falls back to the toolkit's root commit.
    """
    payload = {
        "last_synced_dotfiles_sha": dotfiles_sha,
        "synced_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "toolkit_commit": toolkit_commit,
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


def changed_paths(repo: Path, base: str, tip: str) -> frozenset[str]:
    """Return every path git reports as changed between two refs."""
    result = run_git(repo, "diff", "--name-only", base, tip)
    if result.returncode != 0:
        raise RuntimeError(f"git diff {base} {tip} failed in {repo}: {result.stderr}")
    return frozenset(line for line in result.stdout.splitlines() if line)


def path_exists_at(repo: Path, ref: str, path: str) -> bool:
    """Return whether ``path`` exists in ``repo`` at ``ref``."""
    result = run_git(repo, "cat-file", "-e", f"{ref}:{path}")
    return result.returncode == 0


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


def resolve_head(repo: Path) -> str:
    """Return the current HEAD commit of ``repo``."""
    result = run_git(repo, "rev-parse", "HEAD")
    if result.returncode != 0:
        raise RuntimeError(f"git rev-parse HEAD failed in {repo}: {result.stderr}")
    return result.stdout.strip()


def root_commit(repo: Path) -> str:
    """Return the earliest commit reachable from HEAD in ``repo``."""
    result = run_git(repo, "rev-list", "--max-parents=0", "--reverse", "HEAD")
    if result.returncode != 0:
        raise RuntimeError(f"git rev-list failed in {repo}: {result.stderr}")
    lines = [line for line in result.stdout.splitlines() if line]
    if not lines:
        raise RuntimeError(f"no root commit found in {repo}")
    return lines[0]


def resolve_toolkit_anchor(repo_root: Path, state: dict[str, object] | None) -> str:
    """Toolkit-side anchor for conflict-set math.

    The last commit recorded as already synced, i.e. ``toolkit_commit`` from
    a prior successful ``--apply``. Falls back to the toolkit's own root
    commit — the maximally conservative choice, since it treats the whole
    toolkit history as "possibly conflicting" rather than silently narrowing
    the window — when no recorded anchor exists yet: a genuine first run, or
    a state file whose ``toolkit_commit`` no wrapping caller has filled in
    after committing (see write_state()).
    """
    anchor = state.get("toolkit_commit") if state else None
    if isinstance(anchor, str) and anchor:
        return anchor
    return root_commit(repo_root)


def git_add(repo_root: Path, paths: Sequence[str]) -> None:
    """Stage the given repo-relative paths."""
    if not paths:
        return
    result = run_git(repo_root, "add", "--", *paths)
    if result.returncode != 0:
        raise RuntimeError(f"git add failed in {repo_root}: {result.stderr}")


def write_copies(
    repo_root: Path, dotfiles_path: Path, tip: str, paths: Sequence[str]
) -> None:
    """Write each path's byte-identical content from dotfiles@tip into the repo."""
    for relpath in paths:
        content = read_at(dotfiles_path, tip, relpath)
        dest = repo_root / relpath
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)


def run_generator_sweep(
    repo_root: Path, sweep: Sequence[str], *, quiet: bool, verbose: bool
) -> None:
    """Run every generator in ``sweep`` so copied artifacts describe this repo."""
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


def apply_sync(
    repo_root: Path,
    dotfiles_path: Path,
    tip: str,
    plain_copies: Sequence[str],
    handled: Sequence[str],
    base: str,
    *,
    generator_sweep: Sequence[str] = GENERATOR_SWEEP,
    quiet: bool = False,
    verbose: bool = False,
) -> None:
    """Write copies, apply handled conflicts, sweep, then record new state.

    Staging (``git add``) happens BEFORE the generator sweep runs — this
    ordering is load-bearing, not incidental. ``gen_interfaces.py`` (and any
    generator like it) enumerates git-*tracked* files, so a brand-new copied
    file left unstaged is invisible to it; staging first is what makes the
    sweep see it. See the module docstring and
    ``test_apply_stages_new_files_before_generator_sweep_sees_them``.
    """
    write_copies(repo_root, dotfiles_path, tip, plain_copies)
    for path in handled:
        CONFLICT_HANDLERS[path](dotfiles_path, repo_root, base, tip)

    git_add(repo_root, [*plain_copies, *handled])
    run_generator_sweep(repo_root, generator_sweep, quiet=quiet, verbose=verbose)

    write_state(repo_root, dotfiles_sha=tip, toolkit_commit=None)
    git_add(repo_root, [str(state_path(repo_root).relative_to(repo_root))])


# ── pure set math and classification ────────────────────────────────────────


def match_never_synced(path: str) -> bool:
    """Whether ``path`` is never synced between the two checkouts."""
    return any(fnmatch.fnmatch(path, pattern) for pattern in NEVER_SYNCED_REPO_SPECIFIC)


def compute_copy_set(dotfiles_changed: frozenset[str]) -> frozenset[str]:
    """Paths to replay onto the toolkit: everything dotfiles changed, minus
    the deliberate-deletion blocklist, the dotfiles-only exclude list, and
    the never-synced repo-specific paths (same relative path in both
    checkouts, intentionally different content — copying one over the other
    would clobber)."""
    return (
        dotfiles_changed
        - frozenset(BLOCKLIST)
        - frozenset(EXCLUDE)
        - {p for p in dotfiles_changed if match_never_synced(p)}
    )


def compute_conflict_set(
    dotfiles_changed: frozenset[str], toolkit_changed: frozenset[str]
) -> frozenset[str]:
    """Paths both sides changed since the last sync — never assumed, always
    derived. Never-synced repo-specific paths are subtracted first: they
    replay in neither direction, so both sides touching one is not a
    conflict to hand-resolve."""
    return (dotfiles_changed & toolkit_changed) - {
        p for p in dotfiles_changed if match_never_synced(p)
    }


def find_unclassified_divergences(
    common_paths: Iterable[str],
    diverged_paths: Iterable[str],
    diverged: Callable[[str], bool],
) -> list[str]:
    """Same-path divergences between the two checkouts with no classification.

    ``common_paths`` are the relative paths present in both trees;
    ``diverged_paths`` are the candidates to test; ``diverged(path)``
    reports whether that path's content actually differs between the two
    repos' compared states (the derivation decides which states those are
    — see verify_invariants() for the write-time lineage rule and
    RealCheckoutDerivationTests for the settled-anchor rule). A path that
    diverges but is neither in NEVER_SYNCED_REPO_SPECIFIC, nor matched by
    GENERATED_ARTIFACT_PATTERNS, nor registered in CONFLICT_HANDLERS is
    returned — the caller surfaces it with guidance instead of letting a
    future sync blind-copy over it."""
    return [
        path
        for path in sorted(set(common_paths) & set(diverged_paths))
        if diverged(path)
        and not (
            match_never_synced(path)
            or any(
                fnmatch.fnmatch(path, pattern)
                for pattern in GENERATED_ARTIFACT_PATTERNS
            )
            or path in CONFLICT_HANDLERS
        )
    ]


def classify_conflict(path: str) -> str:
    """Classify one conflict path: "generated_artifact", "handled", or
    "unclassified" (category 4 — stop and hand-resolve, the safe default)."""
    if any(fnmatch.fnmatch(path, pattern) for pattern in GENERATED_ARTIFACT_PATTERNS):
        return "generated_artifact"
    if path in CONFLICT_HANDLERS:
        return "handled"
    return "unclassified"


def verify_invariants(
    dotfiles_path: Path,
    base: str,
    tip: str,
    copy_set: frozenset[str],
    plain_copies: Sequence[str],
    toolkit_root: Path = REPO_ROOT,
) -> list[str]:
    """Check the invariants that must hold before any write. Empty means clean.

    The deletion check runs over the full ``copy_set``: any path dotfiles
    changed and this tool would otherwise touch (plain copy, generated
    artifact, or a registered conflict handler) must still exist at TIP, or
    it looks like an undeclared deletion. The ``BLOCKED_MODULES`` content
    check is narrower, over ``plain_copies`` only — the paths this tool
    blind-copies verbatim with no further review. A conflict path (e.g.
    ``INTERFACES.md``, regenerated by the sweep, or ``links.toml``, always
    routed to hand-resolution) is never blind-copied, so dotfiles' own
    legitimate self-references to a blocklisted module inside those files
    (its own generated docs describing its own personal-only scripts) must
    not block a run that will never actually copy that content in.

    The same-path divergence check is also over ``plain_copies`` only: a
    plain copy is safe to blind-copy iff the toolkit's copy is exactly
    dotfiles@BASE's (clean delta replay — the copy replays the pending
    dotfiles delta) or is a genuinely new file (absent from the toolkit
    AND from dotfiles@BASE). Anything else is pre-anchor divergence —
    toolkit-side content a ``toolkit_changed`` diff can never see — or an
    unclassified deletion (the toolkit deleted the path while dotfiles
    edited it); both stop the run with guidance instead of silently
    clobbering or resurrecting. Generated artifacts are skipped
    silently: they diverge by construction (each repo's sweep emits its
    own) and are regenerated after any copy.
    """
    problems: list[str] = []
    for entry in BLOCKLIST:
        if not path_exists_at(dotfiles_path, base, entry):
            problems.append(
                f"BLOCKLIST entry {entry!r} does not exist in dotfiles@{base} "
                "— stale entry, fix BLOCKLIST"
            )
    for path in sorted(copy_set):
        if not path_exists_at(dotfiles_path, tip, path):
            problems.append(
                f"{path!r} is in copy_set but missing from dotfiles@{tip} — "
                "looks like a deletion; add it to BLOCKLIST or EXCLUDE if deliberate"
            )
    toolkit_head = resolve_head(toolkit_root)
    for path in sorted(plain_copies):
        if not path_exists_at(dotfiles_path, tip, path):
            continue  # already reported as a deletion above
        content = read_at(dotfiles_path, tip, path)
        text = content.decode("utf-8", errors="ignore")
        for module in BLOCKED_MODULES:
            if module in text:
                problems.append(
                    f"{path!r} references blocklisted module {module!r} "
                    f"at dotfiles@{tip}"
                )
        if any(
            fnmatch.fnmatch(path, pattern) for pattern in GENERATED_ARTIFACT_PATTERNS
        ):
            continue  # regenerated by the sweep; diverges by construction
        in_dotfiles_base = path_exists_at(dotfiles_path, base, path)
        in_toolkit_head = path_exists_at(toolkit_root, toolkit_head, path)
        if not in_toolkit_head:
            if in_dotfiles_base:
                problems.append(
                    f"{path!r} is missing from the toolkit but exists in "
                    f"dotfiles@{base} — an unclassified deletion; add it to "
                    "BLOCKLIST if deliberate — never blind-copy it back"
                )
            continue  # genuinely new file in dotfiles: clean copy
        if not in_dotfiles_base or read_at(dotfiles_path, base, path) != read_at(
            toolkit_root, toolkit_head, path
        ):
            problems.append(
                f"{path!r} diverged from the sync lineage: the toolkit's copy "
                f"differs from dotfiles@{base}, so blind-copying dotfiles@{tip} "
                "over it would clobber toolkit-side content — add it to "
                "NEVER_SYNCED_REPO_SPECIFIC if the divergence is intentional, "
                "register a CONFLICT_HANDLERS rule, or hand-resolve"
            )
    return problems


# ── cli ──────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    parser = argparse.ArgumentParser(
        prog="sync_from_dotfiles",
        description="replay dotfiles' harness changes onto this toolkit",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="derive, verify, and apply the sync (default: report/diff only)",
    )
    parser.add_argument(
        "--since",
        metavar="<sha>",
        default=None,
        help="override the stored dotfiles BASE (first run, or recovery)",
    )
    parser.add_argument(
        "--dotfiles-path",
        metavar="<path>",
        default=None,
        help="path to the dotfiles checkout (default: ~/dotfiles)",
    )
    cli_common.add_verbosity_args(parser)
    return parser


def main() -> None:
    """Parse argv, derive the sync plan, and report or apply it."""
    parser = build_parser()
    args = parser.parse_args()

    dotfiles_path = (
        Path(args.dotfiles_path).expanduser()
        if args.dotfiles_path
        else DEFAULT_DOTFILES_PATH
    )

    state = load_state(REPO_ROOT)
    base = args.since or (state.get("last_synced_dotfiles_sha") if state else None)
    if not base or not isinstance(base, str):
        print(
            "[sync_from_dotfiles] no stored BASE and no --since given — "
            "pass --since <sha> on a first run",
            file=sys.stderr,
        )
        sys.exit(2)

    tip = resolve_head(dotfiles_path)
    toolkit_head = resolve_head(REPO_ROOT)
    toolkit_anchor = resolve_toolkit_anchor(REPO_ROOT, state)

    dotfiles_changed = changed_paths(dotfiles_path, base, tip)
    toolkit_changed = changed_paths(REPO_ROOT, toolkit_anchor, toolkit_head)

    copy_set = compute_copy_set(dotfiles_changed)
    conflict_set = compute_conflict_set(dotfiles_changed, toolkit_changed)
    plain_copies = sorted(copy_set - conflict_set)

    problems = verify_invariants(dotfiles_path, base, tip, copy_set, plain_copies)
    if problems:
        for problem in problems:
            print(
                f"[sync_from_dotfiles] INVARIANT VIOLATION: {problem}", file=sys.stderr
            )
        sys.exit(1)

    classifications = {path: classify_conflict(path) for path in sorted(conflict_set)}
    unclassified = [p for p, c in classifications.items() if c == "unclassified"]
    if unclassified:
        for path in unclassified:
            print(
                f"[sync_from_dotfiles] UNCLASSIFIED CONFLICT: {path}", file=sys.stderr
            )
            print(
                f"  dotfiles diff: git -C {dotfiles_path} diff {base} {tip} -- {path}",
                file=sys.stderr,
            )
            print(
                f"  toolkit diff:  git -C {REPO_ROOT} diff {toolkit_anchor} "
                f"{toolkit_head} -- {path}",
                file=sys.stderr,
            )
        print(
            "[sync_from_dotfiles] hand-resolve the path(s) above, then either "
            "register a CONFLICT_HANDLERS entry or blocklist/exclude them",
            file=sys.stderr,
        )
        sys.exit(1)

    generated = sorted(
        p for p, c in classifications.items() if c == "generated_artifact"
    )
    handled = sorted(p for p, c in classifications.items() if c == "handled")
    never_synced = sorted(p for p in dotfiles_changed if match_never_synced(p))

    cli_common.qprint(f"[sync_from_dotfiles] BASE={base} TIP={tip}", quiet=args.quiet)
    if never_synced:
        cli_common.qprint(
            "[sync_from_dotfiles] repo-specific, never synced: this toolkit's "
            "copy of each path below is intentionally different from dotfiles'; "
            "a wanted dotfiles-side change is ported by hand, not replayed",
            quiet=args.quiet,
        )
        for path in never_synced:
            cli_common.qprint(
                f"  skip (repo-specific, never synced): {path}", quiet=args.quiet
            )
    cli_common.qprint(
        f"[sync_from_dotfiles] copy_set: {len(copy_set)} path(s) "
        f"({len(plain_copies)} plain, {len(generated)} generated-artifact, "
        f"{len(handled)} handled by a registered rule)",
        quiet=args.quiet,
    )
    for path in plain_copies:
        cli_common.vprint(f"  copy   {path}", verbose=args.verbose)
    for path in generated:
        cli_common.qprint(
            f"  skip (generated artifact, regenerated by the sweep): {path}",
            quiet=args.quiet,
        )
    for path in handled:
        cli_common.qprint(f"  apply (registered handler): {path}", quiet=args.quiet)

    if not args.apply:
        cli_common.qprint(
            "[sync_from_dotfiles] report only — pass --apply to write", quiet=args.quiet
        )
        return

    apply_sync(
        REPO_ROOT,
        dotfiles_path,
        tip,
        plain_copies,
        handled,
        base,
        quiet=args.quiet,
        verbose=args.verbose,
    )

    cli_common.qprint(
        f"[sync_from_dotfiles] applied {len(plain_copies) + len(handled)} path(s); "
        f"wrote {state_path(REPO_ROOT).relative_to(REPO_ROOT)}",
        quiet=args.quiet,
    )
    cli_common.qprint(
        "[sync_from_dotfiles] review the staged diff and commit it yourself — "
        "this tool does not commit on your behalf",
        quiet=args.quiet,
    )


if __name__ == "__main__":
    main()
