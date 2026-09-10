#!/usr/bin/env python3
"""refresh_guidance.py — audit-by-inspection for hand-authored, agent-facing docs.

Scans a repo's hand-authored prose documentation (every ``AGENTS.md``, plus a
per-repo fixed list such as ``README.md``/``STYLE.md``) for three things: (1)
mechanically-checkable claims in backtick/code-span citations that no longer
hold — a cited repo-relative path that's gone, or a cited command/flag the
named script no longer has, (2) progressive disclosure criteria — missing
paired ``CLAUDE.md`` symlinks, un-signposted child ``AGENTS.md`` files, and
root ``AGENTS.md`` line-budget violations, and (3) which ``##`` sections have
gone longest without a human-confirmed review, using a sidecar state file plus
a git-history fallback for sections that have never been marked reviewed.

This does not attempt semantic drift detection (verifying prose still
matches reality) — only what is mechanically verifiable, plus surfacing what
a human hasn't looked at recently. See ``mark-reviewed`` below for the only
way review state advances; a clean ``check`` run never bumps it on its own,
since a mechanical pass finding nothing broken does not establish the prose
is still semantically accurate.

Only backtick/code-span citations are ever considered a claim — a plain-
prose mention (an illustrative "e.g. some_script.py") is never flagged, by
construction, since these docs already use code-span formatting as their
convention for a real citation.

This is a shared engine, not a per-repo copy: which docs and script
directories to scan is config (a ``DocSetConfig``), so the same checking
logic runs against any repo. ``DOC_SETS`` holds only agent-toolkit's own,
self-describing entry — every other repo supplies its
own config by placing a ``refresh-guidance.toml`` at its own repo root,
auto-discovered by ``--repo-root`` (see ``load_external_doc_set``); this
module carries no hardcoded knowledge of any other repo's internal layout.
``MIGRATION.md`` is deliberately never in a doc-set's fixed list: it is
transient, self-obsoleting migration prose, not evergreen guidance.

Usage:
    refresh_guidance.py check --repo-root <path> [--doc-set agent-toolkit]
        scan and print a findings + staleness report (default subcommand)
    refresh_guidance.py mark-reviewed <doc> <heading> --repo-root <path> [--doc-set agent-toolkit]
        record human sign-off that one doc's `## <heading>` section is current
    refresh_guidance.py scaffold <directory> --repo-root <path> [--force]
        scaffold rubric-compliant AGENTS.md and paired CLAUDE.md symlink

Flags: --repo-root <path> (default: this checkout), --doc-set
{agent-toolkit} (no default — required unless <repo-root>/refresh-guidance.toml
exists, in which case that file is used instead and --doc-set must be
omitted), --commit <sha> and --date <YYYY-MM-DD> (mark-reviewed only;
default to current HEAD / today), --force/-f (scaffold only),
--quiet/-q, --verbose/-v.
Env vars: none.
Files read: every doc named by the active doc-set's config, every script
under its configured script directories (parsed with `ast`, never imported
or executed — these tools mutate live state, so auditing them must not run
them), its sidecar state file if present, and
`<repo-root>/refresh-guidance.toml` if present.
Files written: the active doc-set's sidecar state file
(`refresh-guidance-state.json` at the repo root, by default) by
`mark-reviewed`, and `<directory>/AGENTS.md` with its sibling `CLAUDE.md`
symlink by `scaffold`.
Exit codes: 0 success; 2 bad usage (no doc-set resolvable, a malformed
`refresh-guidance.toml`, `--doc-set` conflicting with one, missing repo
root, for `mark-reviewed` a doc/heading pair that doesn't exist, or for
`scaffold` a conflict or path traversal).

Requires Python 3.12+.
"""

import argparse
import ast
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import cli_common
import gen_interfaces

DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[1]

_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_ENV_ASSIGNMENT_RE = re.compile(r"[A-Z_][A-Z0-9_]*=\S*")
_PATH_LIKE_EXTENSIONS = (
    ".md",
    ".json",
    ".toml",
    ".lock",
    ".yml",
    ".yaml",
    ".ts",
    ".tsx",
    ".js",
    ".mjs",
    ".txt",
)
_SKIP_DIRS = frozenset(
    {".git", "__pycache__", "node_modules", ".venv", ".pytest_cache", ".ruff_cache"}
)

ROOT_AGENTS_BUDGET_CONTENT_LINES = 150

AGENTS_MD_TEMPLATE = """# AGENTS.md — {dir_name}

<!-- Paired with CLAUDE.md symlink in the same directory -->

## Responsibilities & Boundary

<!-- What this directory owns, key boundaries, and architectural roles -->

## Hazards & Signposts

<!-- Directory-specific hazards, non-obvious traps, gotchas, and prerequisite reads -->

## Local Conventions

<!-- Local conventions, commands, and verification rules for this subtree -->
"""


# ── per-repo configuration ───────────────────────────────────────────────────


@dataclass(frozen=True)
class DocSetConfig:
    """What to scan for one repo: fixed docs, plus where its scripts live.

    `AGENTS.md` files are never listed here — they are auto-discovered (see
    :func:`discover_agents_md`) since both repos follow the same
    `AGENTS.md` + `CLAUDE.md`-symlink convention identically.
    """

    fixed_docs: tuple[str, ...]
    script_dirs: tuple[str, ...]
    root_entrypoints: tuple[str, ...] = ("install.py", "depart.py")
    state_path: str = "refresh-guidance-state.json"
    cross_repo_scripts: bool = False
    """When True, script discovery also indexes `agent-scripts/*.py` under
    the resolved agent-toolkit root (see :data:`DEFAULT_AGENT_TOOLKIT_ROOT`),
    and path claim checks (both directory-qualified paths and bare-filename
    basename lookups) consult that checkout as well, matching the
    `AGENT_TOOLKIT_PATH` convention the bundle-transfer installer on the
    origin machine already uses.
    Real, evidence-based need: post-cutover, the origin repo's own docs legitimately
    cite `dev_status.py`/`second_opinion.py`/etc. bare, meaning "the shared
    tool, now hosted in agent-toolkit", as well as shared docs/templates
    like `MIGRATION.md`, `claude/commands/swarm.md`, or `agent-toolkit/README.md`
    -- without this, every one of those reads as a broken reference even though
    the doc is correct."""
    claim_exempt_docs: tuple[str, ...] = ("CHANGELOG.md",)
    """Docs still scanned for `##` sections and staleness, but never for
    mechanical path/command claims.

    `CHANGELOG.md` is a historical narrative -- each entry describes what
    was true *at that commit*, not what's true now, so "this path from a
    six-month-old entry no longer exists" isn't staleness, it's the entry
    doing its job (confirmed against a real CHANGELOG.md: the
    overwhelming majority of a first real run's findings were exactly this
    -- scripts correctly described as added, then later moved to
    agent-toolkit by a *later* entry).

    The `CLAUDE_CODE_PARITY.md` docs (added per doc-set below) are
    verification research notes *about other coding-agent tools* -- their
    own file layouts, npm packages, upstream doc URLs and GitHub repos,
    deliberately cited in the same backtick style this repo uses for real
    citations (confirmed by reading them: e.g. pi/CLAUDE_CODE_PARITY.md
    explicitly says `pi/keybindings.json` does *not* exist in this repo).
    Checking them as if every code span were repo-relative produced
    nothing but noise -- everything real about *this* repo's install
    surface in a parity doc is already covered by the command-claim check
    (which stays on), and by AGENTS.md/STYLE.md/README.md."""


DEFAULT_AGENT_TOOLKIT_ROOT = Path(
    os.environ.get(
        "AGENT_TOOLKIT_PATH", str(Path.home() / "Workspace" / "agent-toolkit")
    )
)


DOC_SETS: dict[str, DocSetConfig] = {
    "agent-toolkit": DocSetConfig(
        fixed_docs=(
            "README.md",
            "STYLE.md",
            "GLOSSARY.md",
            "copilot/CLAUDE_CODE_PARITY.md",
            "agy/CLAUDE_CODE_PARITY.md",
            "opencode/CLAUDE_CODE_PARITY.md",
            "pi/CLAUDE_CODE_PARITY.md",
        ),
        script_dirs=("agent-scripts", "scripts"),
        claim_exempt_docs=(
            "CHANGELOG.md",
            "copilot/CLAUDE_CODE_PARITY.md",
            "agy/CLAUDE_CODE_PARITY.md",
            "opencode/CLAUDE_CODE_PARITY.md",
            "pi/CLAUDE_CODE_PARITY.md",
        ),
    ),
}
"""The only built-in doc-set: agent-toolkit describing itself. Any other
repo supplies its own config via an external
`refresh-guidance.toml` at its own repo root (see
:func:`load_external_doc_set`) -- agent-toolkit's shared source carries no
hardcoded knowledge of any other repo's internal layout."""


EXTERNAL_CONFIG_FILENAME = "refresh-guidance.toml"

_DOC_SET_FIELD_KINDS: dict[str, str] = {
    "fixed_docs": "list_str",
    "script_dirs": "list_str",
    "root_entrypoints": "list_str",
    "state_path": "str",
    "cross_repo_scripts": "bool",
    "claim_exempt_docs": "list_str",
}
_REQUIRED_DOC_SET_KEYS = frozenset({"fixed_docs", "script_dirs"})


class ConfigError(Exception):
    """A refresh-guidance.toml or --doc-set resolution problem. Raised by
    :func:`load_external_doc_set`/:func:`resolve_doc_set`, never by a
    direct `sys.exit` -- only the CLI command functions translate this
    into an exit code, so a direct caller (a test, a future script) can
    catch it as a normal exception instead of capturing stderr and
    catching `SystemExit`."""


def load_external_doc_set(repo_root: Path) -> DocSetConfig | None:
    """Load `<repo_root>/refresh-guidance.toml` into a `DocSetConfig`, or
    `None` if the file doesn't exist. Validates every key before
    constructing anything: required keys present, no unrecognized keys,
    every value matching its field's expected shape (a `tuple[str, ...]`
    field must be a TOML array of strings, a `str` field a TOML string, a
    `bool` field a TOML boolean -- a quoted `"true"` is a string, not a
    boolean, and is rejected). Only after validation does it convert each
    validated array to a tuple -- a straight type coercion applied
    uniformly to every array field, never a per-field value translation.
    """
    path = repo_root / EXTERNAL_CONFIG_FILENAME
    if not path.is_file():
        return None

    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{EXTERNAL_CONFIG_FILENAME}: invalid TOML: {exc}") from exc

    missing = _REQUIRED_DOC_SET_KEYS - data.keys()
    if missing:
        raise ConfigError(
            f"{EXTERNAL_CONFIG_FILENAME}: missing required key(s): "
            f"{', '.join(sorted(missing))}"
        )
    unknown = data.keys() - _DOC_SET_FIELD_KINDS.keys()
    if unknown:
        raise ConfigError(
            f"{EXTERNAL_CONFIG_FILENAME}: unknown key(s): {', '.join(sorted(unknown))}"
        )

    kwargs: dict[str, object] = {}
    for key, value in data.items():
        kind = _DOC_SET_FIELD_KINDS[key]
        if kind == "list_str":
            if not isinstance(value, list):
                raise ConfigError(
                    f"{EXTERNAL_CONFIG_FILENAME}: {key} must be a list of "
                    f"strings, got {type(value).__name__}"
                )
            for index, element in enumerate(value):
                if not isinstance(element, str):
                    raise ConfigError(
                        f"{EXTERNAL_CONFIG_FILENAME}: {key}[{index}] must "
                        f"be a string, got {type(element).__name__}"
                    )
            kwargs[key] = tuple(value)
        elif kind == "bool":
            if not isinstance(value, bool):
                raise ConfigError(
                    f"{EXTERNAL_CONFIG_FILENAME}: {key} must be a bool, "
                    f"got {type(value).__name__}"
                )
            kwargs[key] = value
        else:  # "str"
            if not isinstance(value, str):
                raise ConfigError(
                    f"{EXTERNAL_CONFIG_FILENAME}: {key} must be a string, "
                    f"got {type(value).__name__}"
                )
            kwargs[key] = value

    return DocSetConfig(**kwargs)


def resolve_doc_set(
    repo_root: Path, doc_set_name: str | None
) -> tuple[DocSetConfig, str]:
    """Resolve the doc-set to use for `repo_root`, given the (possibly
    absent) `--doc-set` value.

    `doc_set_name` is `None` exactly when `--doc-set` was omitted --
    argparse's default is `None`, not `"agent-toolkit"` (see
    `_add_doc_set_args`), so this function can tell "no opinion given"
    apart from "user explicitly chose agent-toolkit". Every branch either
    resolves from one explicit source or raises :class:`ConfigError` --
    there is no silent fallback, since a silent fallback to
    `DOC_SETS["agent-toolkit"]` here is exactly how this module used to
    leak agent-toolkit's own doc-set onto a repo that isn't agent-toolkit.
    """
    external = load_external_doc_set(repo_root)
    if external is not None:
        if doc_set_name is not None:
            raise ConfigError(
                f"{EXTERNAL_CONFIG_FILENAME} and --doc-set were both "
                "given; pick one (remove the flag, or delete/move the file)"
            )
        return external, f"{repo_root.name} (external config)"

    if doc_set_name is None:
        raise ConfigError(
            "no doc-set specified: pass --doc-set agent-toolkit, or add "
            f"{repo_root}/{EXTERNAL_CONFIG_FILENAME}"
        )
    if doc_set_name not in DOC_SETS:
        raise ConfigError(
            f"unknown doc-set {doc_set_name!r}; choices: {sorted(DOC_SETS)}"
        )
    return DOC_SETS[doc_set_name], doc_set_name


# ── discovery ─────────────────────────────────────────────────────────────────


def _tracked_files(repo_root: Path) -> list[str] | None:
    """Every `git ls-files` entry under ``repo_root``, or None if unavailable."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.splitlines() if result.returncode == 0 else None


def discover_agents_md(repo_root: Path) -> list[str]:
    """Return every tracked `AGENTS.md`'s repo-relative path, sorted.

    Filters tracked files by exact basename, the same approach
    `test/test_agents_md_links.py` already uses — so a `CLAUDE.md` symlink
    beside one is never picked up as a second doc. Falls back to a
    filesystem walk when git is unavailable (or this isn't a checkout).
    """
    tracked = _tracked_files(repo_root)
    if tracked is not None:
        return sorted(p for p in tracked if Path(p).name == "AGENTS.md")

    found: list[str] = []
    for path in repo_root.rglob("AGENTS.md"):
        rel = path.relative_to(repo_root)
        if any(part in _SKIP_DIRS for part in rel.parts):
            continue
        if path.is_file():
            found.append(rel.as_posix())
    return sorted(found)


UNDOCUMENTED_DIR_THRESHOLD = 5
_CODE_EXTENSIONS = (".py", ".ts", ".sh")


@dataclass
class UndocumentedDir:
    directory: str
    file_count: int


def discover_undocumented_dirs(
    repo_root: Path, threshold: int = UNDOCUMENTED_DIR_THRESHOLD
) -> list[UndocumentedDir]:
    """Flag a top-level directory that looks complex enough to warrant its
    own `AGENTS.md` but doesn't have one -- the inverse signal from the rest
    of this module: not "this existing doc is stale" but "no one has
    written a doc for this yet." Purely a suggestion for a human to weigh;
    never creates anything.

    Scoped to top-level directories only (repo_root's immediate children),
    matching how this repo's own `AGENTS.md` files are actually placed
    (`test/`, `agent-scripts/`, `pi/` -- never a nested subdirectory) rather
    than recursing into every directory in the tree. A directory qualifies
    when it holds at least ``threshold`` tracked files (recursively) and at
    least one is a code file (`.py`/`.ts`/`.sh`) -- a directory of pure
    assets or generated output isn't the kind of thing that accumulates
    conventions worth documenting.
    """
    tracked = _tracked_files(repo_root) or []
    tracked_set = set(tracked)
    by_top: dict[str, list[str]] = {}
    for relpath in tracked:
        parts = Path(relpath).parts
        if len(parts) < 2:
            continue  # a repo-root file, not inside any directory
        by_top.setdefault(parts[0], []).append(relpath)

    results: list[UndocumentedDir] = []
    for top, files in sorted(by_top.items()):
        if top in _SKIP_DIRS or f"{top}/AGENTS.md" in tracked_set:
            continue
        if len(files) < threshold:
            continue
        if not any(f.endswith(_CODE_EXTENSIONS) for f in files):
            continue
        results.append(UndocumentedDir(directory=top, file_count=len(files)))
    return results


def discover_basename_index(
    repo_root: Path, agent_toolkit_root: Path | None = None
) -> dict[str, list[str]]:
    """Map every tracked file's basename to its repo-relative path(s).

    The fallback :func:`check_path_claim` uses for a bare filename citation
    (no directory component) that doesn't exist exactly where cited --
    existence anywhere in the repo is enough to call the claim valid.
    Empty (not None) when git is unavailable, so callers need no special
    case: a bare-filename fallback just never matches.

    When ``agent_toolkit_root`` is provided, also indexes its tracked files
    after ``repo_root``'s, so local repo files take precedence on collision.
    """
    index: dict[str, list[str]] = {}
    for relpath in _tracked_files(repo_root) or []:
        index.setdefault(Path(relpath).name, []).append(relpath)
    if agent_toolkit_root is not None:
        for relpath in _tracked_files(agent_toolkit_root) or []:
            index.setdefault(Path(relpath).name, []).append(relpath)
    return index


def discovered_docs(repo_root: Path, doc_set: DocSetConfig) -> list[str]:
    """Union of the doc-set's fixed docs (only those present) and every
    auto-discovered `AGENTS.md` -- a fixed doc absent from this repo (e.g. a
    `CHANGELOG.md` one repo has and the other doesn't) is silently skipped
    rather than reported as missing."""
    docs = {d for d in doc_set.fixed_docs if (repo_root / d).is_file()}
    docs.update(discover_agents_md(repo_root))
    return sorted(docs)


def discover_scripts(
    repo_root: Path,
    doc_set: DocSetConfig,
    agent_toolkit_root: Path | None = None,
) -> dict[str, Path]:
    """Map every script basename under the doc-set's script directories (plus
    its root entrypoints) to its path -- the universe of scripts a command
    citation can legitimately name.

    When ``doc_set.cross_repo_scripts`` is set, also indexes
    ``agent_toolkit_root / "agent-scripts"`` -- a repo-root-local script
    always wins on a basename collision (checked first, `setdefault`).
    """
    scripts: dict[str, Path] = {}
    for rel_dir in doc_set.script_dirs:
        directory = repo_root / rel_dir
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.py")):
            if path.name.startswith("test_"):
                continue
            scripts.setdefault(path.name, path)
    for rel_entry in doc_set.root_entrypoints:
        path = repo_root / rel_entry
        if path.is_file():
            scripts.setdefault(path.name, path)
    if doc_set.cross_repo_scripts and agent_toolkit_root is not None:
        directory = agent_toolkit_root / "agent-scripts"
        if directory.is_dir():
            for path in sorted(directory.glob("*.py")):
                if path.name.startswith("test_"):
                    continue
                scripts.setdefault(path.name, path)
    return scripts


# ── sections + claim extraction ──────────────────────────────────────────────


@dataclass
class Section:
    """One `##` heading's line range. `heading == ""` is the doc's preamble
    (before its first `##`) -- never tracked in the staleness table, but its
    code-span claims are still checked."""

    heading: str
    start_line: int
    end_line: int


@dataclass
class Claim:
    doc: str
    section: str
    line: int
    kind: str  # "path" | "command"
    raw: str
    tokens: list[str] = field(default_factory=list)
    basename: str | None = None
    attempted: str | None = None


_BARE_EXTENSION_RE = re.compile(r"\.[A-Za-z0-9.]+")


def _looks_like_placeholder_stem(stem: str) -> bool:
    """Report whether a `snake_case` stem mixes a real word with a bare
    single-letter/all-caps segment (``test_X``, ``do_THING``) -- this repo's
    convention for "put a real name here" in prose, not a literal
    filename."""
    parts = stem.split("_")
    if len(parts) < 2:
        return False
    return any(p.islower() for p in parts) and any(
        p.isalpha() and p.isupper() for p in parts
    )


def _looks_like_path(candidate: str) -> bool:
    """Report whether a bare (no-whitespace) code-span substring is shaped
    like a repo-relative path claim (never `.py`/`.sh` -- those are always
    routed through the command-claim path instead, whether or not they
    resolve)."""
    if not candidate or candidate.startswith(("-", "~", "/", "<", "{", "$")):
        return False
    if "+" in candidate:
        return False  # keybinding notation ("Ctrl+Option+Left/Right"), not a path
    if "/" in candidate:
        return True
    return candidate.endswith(_PATH_LIKE_EXTENSIONS)


def _find_command_token(
    tokens: list[str], known: set[str]
) -> tuple[int, str] | tuple[None, str] | None:
    """Walk tokens left to right for a script invocation, mirroring
    `gen_interfaces.invocation_tokens`'s "script name must be effectively
    first" rule but for any known script rather than one specific target.

    Returns ``(index, basename)`` for a recognized script, ``(None, token)``
    -- the full original token, directory prefix included -- for a token
    that looks like a script invocation (ends `.py`/`.sh`) of a name not in
    ``known`` -- this is what lets a doc citing a script that no longer
    exists get flagged rather than silently skipped, while a script that's
    merely outside the configured CLI-checkable directories (a `test/`
    helper, `install.sh`) still gets checked for plain existence by
    :func:`check_command_claim` -- or ``None`` when no invocation-shaped
    token is found at all.
    """
    for index, token in enumerate(tokens):
        name = Path(token).name
        if name in known:
            return index, name
        if (
            token in gen_interfaces.SHELL_PROMPT_TOKENS
            or token in gen_interfaces.INTERPRETER_PREFIXES
        ):
            continue
        if _ENV_ASSIGNMENT_RE.fullmatch(token):
            continue
        if name.endswith((".py", ".sh")):
            if _looks_like_placeholder_stem(Path(name).stem):
                return None
            return None, token
        return None
    return None


def classify_span(
    text: str, known_scripts: set[str]
) -> tuple[str, str, list[str], str | None, str | None] | None:
    """Classify one code-span's text as a path or command claim, or None if
    it isn't claim-shaped at all (a bare flag, an env var name, a glob
    pattern, a generic shell command with no known script in it, ...).

    Returns ``(kind, raw, tokens, basename, attempted)``.
    """
    stripped = text.strip()
    if not stripped or any(ch in stripped for ch in "*?{}"):
        return None  # glob/wildcard/brace-expansion patterns, not one citation
    if _BARE_EXTENSION_RE.fullmatch(stripped):
        return None  # "`.py` files" -- an extension mention, not a citation
    tokens = gen_interfaces.tokenize_invocation_line(stripped)
    if tokens:
        match = _find_command_token(tokens, known_scripts)
        if match is not None:
            index, basename = match
            if index is None:
                return "command", stripped, [], None, basename
            return "command", stripped, tokens[index:], basename, None
    path_part, sep, _fragment = stripped.partition("#")
    candidate = path_part if sep else stripped
    if " " not in candidate and "\t" not in candidate and _looks_like_path(candidate):
        return "path", stripped, [], None, None
    return None


def parse_document(
    doc_relpath: str, text: str, known_scripts: set[str]
) -> tuple[list[Section], list[Claim]]:
    """One pass over a doc's lines: track `##` headings and fenced-code
    state together, so a `## `-looking line inside a fenced example is never
    mistaken for a real section boundary."""
    lines = text.splitlines()
    sections: list[Section] = []
    claims: list[Claim] = []
    heading = ""
    start = 1
    in_fence = False

    def add_claim(
        lineno: int, shape: tuple[str, str, list[str], str | None, str | None]
    ) -> None:
        kind, raw, tokens, basename, attempted = shape
        claims.append(
            Claim(
                doc=doc_relpath,
                section=heading,
                line=lineno,
                kind=kind,
                raw=raw,
                tokens=tokens,
                basename=basename,
                attempted=attempted,
            )
        )

    for lineno, raw_line in enumerate(lines, start=1):
        if raw_line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence and raw_line.startswith("## "):
            sections.append(Section(heading, start, lineno - 1))
            heading = raw_line[3:].strip().rstrip("#").strip()
            start = lineno
            continue
        if in_fence:
            line = raw_line.split(" #", 1)[0]
            shape = classify_span(line, known_scripts)
            if shape is not None:
                add_claim(lineno, shape)
            continue
        for match in _INLINE_CODE_RE.finditer(raw_line):
            shape = classify_span(match.group(1), known_scripts)
            if shape is not None:
                add_claim(lineno, shape)

    sections.append(Section(heading, start, len(lines) or 1))
    return sections, claims


# ── claim checking ───────────────────────────────────────────────────────────


def check_path_claim(
    claim: Claim,
    repo_root: Path,
    basename_index: dict[str, list[str]],
    agent_toolkit_root: Path | None = None,
) -> str | None:
    """Verify a path claim's file/dir exists, and -- when it carries a
    `#fragment` -- that a matching `##` heading exists in the target doc.

    A directory-qualified path (contains `/`) must resolve exactly as
    given -- a wrong directory in an otherwise-real filename is itself a
    bug worth flagging. A bare filename with no directory falls back to
    ``basename_index`` (every tracked file in the repo, indexed by
    basename) before being declared broken, so a doc that names a real file
    without spelling out its full path (`` `question-tool.ts` `` rather
    than `` `pi/extensions/question-tool.ts` ``) isn't a false positive.

    When ``agent_toolkit_root`` is provided, paths that do not exist under
    ``repo_root`` are checked against ``agent_toolkit_root`` (either directly,
    with an ``agent-toolkit/`` or checkout-basename prefix stripped, or
    under ``agent_toolkit_root.parent``) before being declared broken.
    """
    path_part, _, fragment = claim.raw.partition("#")
    if not path_part:
        return None
    target = repo_root / path_part
    if not target.exists() and agent_toolkit_root is not None:
        if (agent_toolkit_root / path_part).exists():
            target = agent_toolkit_root / path_part
        elif (
            path_part.startswith(f"{agent_toolkit_root.name}/")
            and (
                agent_toolkit_root
                / path_part.removeprefix(f"{agent_toolkit_root.name}/")
            ).exists()
        ):
            target = agent_toolkit_root / path_part.removeprefix(
                f"{agent_toolkit_root.name}/"
            )
        elif (
            path_part.startswith("agent-toolkit/")
            and (agent_toolkit_root / path_part.removeprefix("agent-toolkit/")).exists()
        ):
            target = agent_toolkit_root / path_part.removeprefix("agent-toolkit/")
        elif (agent_toolkit_root.parent / path_part).exists():
            target = agent_toolkit_root.parent / path_part
    if not target.exists():
        if "/" not in path_part and path_part in basename_index:
            return None
        return f"path does not exist: `{path_part}`"
    if fragment and target.is_file() and target.suffix == ".md":
        text = target.read_text(encoding="utf-8", errors="replace")
        sections, _ = parse_document(path_part, text, set())
        headings = {section.heading for section in sections}
        normalized = " ".join(fragment.split())
        if normalized not in headings:
            return f"heading not found in `{path_part}`: {fragment!r}"
    return None


def check_command_claim(
    claim: Claim,
    scripts: dict[str, Path],
    cli_cache: dict[Path, gen_interfaces.CliSpec | None],
    repo_root: Path,
    basename_index: dict[str, list[str]],
) -> str | None:
    """Verify a command claim's script exists and its cited flags/subcommands
    are real, by reusing `gen_interfaces`'s own argparse extraction and
    invocation validator -- the same machinery it uses to keep INTERFACES.md
    honest, rather than a second implementation of argparse introspection.

    A script outside the configured CLI-checkable directories (`test/`
    helpers, `install.sh`, a bare `scenarios.sh` cited without its `test/`
    prefix, ...) can still be a perfectly valid citation -- checked for
    existence (exactly as given, then -- for a bare, directory-less token --
    by basename anywhere in the repo, the same fallback
    :func:`check_path_claim` uses) rather than flagged outright, since
    :func:`_find_command_token` only knows it isn't one of the *known*
    scripts, not that it doesn't exist at all.
    """
    if claim.basename is None:
        if (repo_root / claim.attempted).exists():
            return None
        if "/" not in claim.attempted and claim.attempted in basename_index:
            return None
        return f"no such script: `{claim.attempted}`"
    path = scripts[claim.basename]
    if path not in cli_cache:
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(path))
        except (OSError, SyntaxError):
            cli_cache[path] = None
        else:
            module_doc = ast.get_docstring(tree) or ""
            cli_cache[path] = gen_interfaces.extract_cli(
                tree, gen_interfaces.first_paragraph(module_doc)
            )
    cli = cli_cache[path]
    if cli is None:
        return None
    problems = gen_interfaces.validate_invocation(cli, claim.tokens)
    return "; ".join(problems) if problems else None


# ── git staleness fallback ───────────────────────────────────────────────────


def git_blame_range(
    repo_root: Path, doc: str, start_line: int, end_line: int
) -> tuple[str | None, str | None]:
    """Last commit (short sha, date) that touched a section's line range, via
    `git log -L` -- the secondary staleness signal for a section with no
    review-state entry yet."""
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repo_root),
                "log",
                "-1",
                "--format=%h|%ad",
                "--date=short",
                "-L",
                f"{start_line},{end_line}:{doc}",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    if result.returncode != 0:
        return None, None
    first_line = next((line for line in result.stdout.splitlines() if line.strip()), "")
    sha, _, date = first_line.partition("|")
    return (sha or None, date or None)


# ── review-tracking sidecar state ────────────────────────────────────────────


def _state_path(repo_root: Path, doc_set: DocSetConfig) -> Path:
    return repo_root / doc_set.state_path


def load_state(repo_root: Path, doc_set: DocSetConfig) -> dict[str, dict[str, str]]:
    path = _state_path(repo_root, doc_set)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(
    repo_root: Path, doc_set: DocSetConfig, state: dict[str, dict[str, str]]
) -> None:
    path = _state_path(repo_root, doc_set)
    path.write_text(
        json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _git_head_short(repo_root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    return result.stdout.strip() or "unknown"


# ── check ─────────────────────────────────────────────────────────────────────


@dataclass
class Finding:
    doc: str
    section: str
    line: int
    kind: str
    raw: str
    detail: str


@dataclass
class SectionStatus:
    doc: str
    heading: str
    reviewed: bool
    last_reviewed_commit: str | None
    last_reviewed_date: str | None
    fallback_commit: str | None
    fallback_date: str | None


@dataclass
class CheckResult:
    findings: list[Finding]
    sections: list[SectionStatus]
    undocumented_dirs: list[UndocumentedDir]


def run_check(
    repo_root: Path,
    doc_set_name: str | None,
    agent_toolkit_root: Path = DEFAULT_AGENT_TOOLKIT_ROOT,
) -> CheckResult:
    doc_set, _label = resolve_doc_set(repo_root, doc_set_name)
    docs = discovered_docs(repo_root, doc_set)
    scripts = discover_scripts(repo_root, doc_set, agent_toolkit_root)
    known_basenames = set(scripts)
    cross_repo_root = agent_toolkit_root if doc_set.cross_repo_scripts else None
    basename_index = discover_basename_index(repo_root, cross_repo_root)
    state = load_state(repo_root, doc_set)
    cli_cache: dict[Path, gen_interfaces.CliSpec | None] = {}
    findings: list[Finding] = []
    sections: list[SectionStatus] = []

    for doc in docs:
        text = (repo_root / doc).read_text(encoding="utf-8", errors="replace")
        doc_sections, claims = parse_document(doc, text, known_basenames)
        if doc in doc_set.claim_exempt_docs:
            claims = []

        for claim in claims:
            detail = (
                check_path_claim(claim, repo_root, basename_index, cross_repo_root)
                if claim.kind == "path"
                else check_command_claim(
                    claim, scripts, cli_cache, repo_root, basename_index
                )
            )
            if detail:
                findings.append(
                    Finding(
                        claim.doc,
                        claim.section,
                        claim.line,
                        claim.kind,
                        claim.raw,
                        detail,
                    )
                )

        for section in doc_sections:
            if not section.heading:
                continue
            entry = state.get(f"{doc}#{section.heading}")
            fallback_commit, fallback_date = (None, None)
            if entry is None:
                fallback_commit, fallback_date = git_blame_range(
                    repo_root, doc, section.start_line, section.end_line
                )
            sections.append(
                SectionStatus(
                    doc=doc,
                    heading=section.heading,
                    reviewed=entry is not None,
                    last_reviewed_commit=entry.get("last_reviewed_commit")
                    if entry
                    else None,
                    last_reviewed_date=entry.get("last_reviewed_date")
                    if entry
                    else None,
                    fallback_commit=fallback_commit,
                    fallback_date=fallback_date,
                )
            )

    # ── progressive disclosure checks ─────────────────────────────────────────
    agents_docs = discover_agents_md(repo_root)
    root_agents_present = "AGENTS.md" in agents_docs
    if agents_docs and not root_agents_present:
        findings.append(
            Finding(
                doc="AGENTS.md",
                section="",
                line=1,
                kind="signpost",
                raw="AGENTS.md",
                detail="missing root AGENTS.md instructions file",
            )
        )
    elif root_agents_present:
        root_text = (repo_root / "AGENTS.md").read_text(
            encoding="utf-8", errors="replace"
        )
        content_lines = 0
        in_comment = False
        for raw_line in root_text.splitlines():
            stripped = raw_line.strip()
            if not stripped:
                continue
            if stripped.startswith("<!--") and stripped.endswith("-->"):
                continue
            if stripped.startswith("<!--"):
                in_comment = True
                continue
            if in_comment:
                if "-->" in stripped:
                    in_comment = False
                continue
            content_lines += 1
        if content_lines > ROOT_AGENTS_BUDGET_CONTENT_LINES:
            findings.append(
                Finding(
                    doc="AGENTS.md",
                    section="",
                    line=1,
                    kind="budget",
                    raw="AGENTS.md",
                    detail=(
                        f"root AGENTS.md content line count ({content_lines}) "
                        f"exceeds budget ({ROOT_AGENTS_BUDGET_CONTENT_LINES})"
                    ),
                )
            )

        for doc in agents_docs:
            if doc == "AGENTS.md":
                continue
            top_dir = Path(doc).parts[0]
            pattern = re.compile(
                rf"(?:`{re.escape(top_dir)}/`|`{re.escape(top_dir)}/AGENTS\.md`|`{re.escape(doc)}`|\[[^\]]+\]\((?:\./)?{re.escape(top_dir)}/)"
            )
            if not pattern.search(root_text):
                findings.append(
                    Finding(
                        doc=doc,
                        section="",
                        line=1,
                        kind="signpost",
                        raw=doc,
                        detail=f"directory '{top_dir}' not signposted in root AGENTS.md",
                    )
                )

    for doc in agents_docs:
        doc_path = repo_root / doc
        claude_path = doc_path.parent / "CLAUDE.md"
        if not claude_path.exists() and not claude_path.is_symlink():
            findings.append(
                Finding(
                    doc=doc,
                    section="",
                    line=1,
                    kind="symlink",
                    raw="CLAUDE.md",
                    detail=f"missing paired symlink beside {doc}",
                )
            )
        elif not claude_path.is_symlink():
            findings.append(
                Finding(
                    doc=doc,
                    section="",
                    line=1,
                    kind="symlink",
                    raw="CLAUDE.md",
                    detail="regular file, expected symlink pointing to AGENTS.md",
                )
            )
        else:
            try:
                target = os.readlink(claude_path)
            except OSError:
                target = ""
            if target != "AGENTS.md":
                findings.append(
                    Finding(
                        doc=doc,
                        section="",
                        line=1,
                        kind="symlink",
                        raw="CLAUDE.md",
                        detail=f"symlink target is '{target}', expected 'AGENTS.md'",
                    )
                )

    undocumented_dirs = discover_undocumented_dirs(repo_root)
    return CheckResult(
        findings=findings, sections=sections, undocumented_dirs=undocumented_dirs
    )


def render_report(result: CheckResult, doc_set_label: str) -> str:
    lines: list[str] = [f"refresh-guidance report — doc-set: {doc_set_label}", ""]

    lines.append(f"Findings ({len(result.findings)}):")
    if not result.findings:
        lines.append("  none")
    else:
        for finding in sorted(result.findings, key=lambda f: (f.doc, f.line)):
            lines.append(
                f"  {finding.doc}:{finding.line} [{finding.kind}] `{finding.raw}` — {finding.detail}"
            )

    lines.append("")
    lines.append(f"Sections ({len(result.sections)}):")
    for section in sorted(result.sections, key=lambda s: (s.doc, s.heading)):
        key = f"{section.doc}#{section.heading}"
        if section.reviewed:
            lines.append(
                f"  {key}: reviewed {section.last_reviewed_date} ({section.last_reviewed_commit})"
            )
        elif section.fallback_commit:
            lines.append(
                f"  {key}: never reviewed — last changed {section.fallback_date} "
                f"({section.fallback_commit})"
            )
        else:
            lines.append(f"  {key}: never reviewed — no git history available")

    lines.append("")
    lines.append(f"Undocumented directories ({len(result.undocumented_dirs)}):")
    if not result.undocumented_dirs:
        lines.append("  none")
    else:
        for entry in sorted(result.undocumented_dirs, key=lambda d: -d.file_count):
            lines.append(
                f"  {entry.directory}/ — {entry.file_count} tracked files, has code, no AGENTS.md"
            )

    return "\n".join(lines) + "\n"


def cmd_check(
    repo_root: Path,
    doc_set_name: str | None,
    agent_toolkit_root: Path = DEFAULT_AGENT_TOOLKIT_ROOT,
    quiet: bool = False,
) -> None:
    if not repo_root.is_dir():
        print(f"refresh_guidance: no such repo root {repo_root}", file=sys.stderr)
        sys.exit(2)
    try:
        doc_set, label = resolve_doc_set(repo_root, doc_set_name)
    except ConfigError as exc:
        print(f"refresh_guidance: {exc}", file=sys.stderr)
        sys.exit(2)
    result = run_check(repo_root, doc_set_name, agent_toolkit_root)
    if not quiet:
        print(render_report(result, label), end="")


def cmd_mark_reviewed(
    repo_root: Path,
    doc_set_name: str | None,
    doc: str,
    heading: str,
    commit: str | None,
    date: str | None,
    quiet: bool = False,
) -> None:
    try:
        doc_set, _label = resolve_doc_set(repo_root, doc_set_name)
    except ConfigError as exc:
        print(f"refresh_guidance: {exc}", file=sys.stderr)
        sys.exit(2)
    full_path = repo_root / doc
    if not full_path.is_file():
        print(
            f"refresh_guidance: no such doc {doc!r} under {repo_root}", file=sys.stderr
        )
        sys.exit(2)

    text = full_path.read_text(encoding="utf-8", errors="replace")
    sections, _ = parse_document(doc, text, set())
    known_headings = {section.heading for section in sections if section.heading}
    if heading not in known_headings:
        print(
            f"refresh_guidance: no '## {heading}' heading found in {doc}",
            file=sys.stderr,
        )
        sys.exit(2)

    resolved_commit = commit or _git_head_short(repo_root)
    resolved_date = date or dt.date.today().isoformat()
    state = load_state(repo_root, doc_set)
    key = f"{doc}#{heading}"
    state[key] = {
        "last_reviewed_commit": resolved_commit,
        "last_reviewed_date": resolved_date,
        "reviewed_by": "human-confirmed",
    }
    save_state(repo_root, doc_set, state)
    cli_common.qprint(
        f"refresh_guidance: marked {key!r} reviewed at {resolved_commit} ({resolved_date})",
        quiet=quiet,
    )


def cmd_scaffold(
    repo_root: Path,
    directory: str,
    force: bool = False,
    quiet: bool = False,
) -> None:
    """Scaffold a rubric-compliant `AGENTS.md` and paired `CLAUDE.md` symlink
    under `<repo_root>/<directory>`.
    """
    if not directory or directory.strip() in (".", "./", "/"):
        print(
            "refresh_guidance scaffold: cannot scaffold root repository",
            file=sys.stderr,
        )
        sys.exit(2)

    dir_path = Path(directory)
    if dir_path.is_absolute() or (dir_path.parts and dir_path.parts[0] == ".."):
        print(
            f"refresh_guidance scaffold: path outside repo root: {directory!r}",
            file=sys.stderr,
        )
        sys.exit(2)

    target_dir = (repo_root / dir_path).resolve()
    try:
        target_dir.relative_to(repo_root.resolve())
    except ValueError:
        print(
            f"refresh_guidance scaffold: path outside repo root: {directory!r}",
            file=sys.stderr,
        )
        sys.exit(2)

    if target_dir == repo_root.resolve():
        print(
            "refresh_guidance scaffold: cannot scaffold root repository",
            file=sys.stderr,
        )
        sys.exit(2)

    target_dir.mkdir(parents=True, exist_ok=True)
    agents_path = target_dir / "AGENTS.md"
    claude_path = target_dir / "CLAUDE.md"

    if not force:
        if agents_path.exists() and (claude_path.exists() or claude_path.is_symlink()):
            if claude_path.is_symlink() and os.readlink(claude_path) == "AGENTS.md":
                cli_common.qprint(
                    f"refresh_guidance scaffold: {directory} already has compliant AGENTS.md and CLAUDE.md",
                    quiet=quiet,
                )
                return
            print(
                f"refresh_guidance scaffold: {claude_path} already exists (pass --force to overwrite)",
                file=sys.stderr,
            )
            sys.exit(2)
        elif not agents_path.exists() and (
            claude_path.exists() or claude_path.is_symlink()
        ):
            print(
                f"refresh_guidance scaffold: {claude_path} already exists (pass --force to overwrite)",
                file=sys.stderr,
            )
            sys.exit(2)
        elif (
            agents_path.exists()
            and not claude_path.exists()
            and not claude_path.is_symlink()
        ):
            os.symlink("AGENTS.md", claude_path)
            cli_common.qprint(
                f"refresh_guidance scaffold: created missing symlink {claude_path} -> AGENTS.md",
                quiet=quiet,
            )
            return

    if force and (claude_path.is_symlink() or claude_path.exists()):
        claude_path.unlink()

    content = AGENTS_MD_TEMPLATE.format(dir_name=dir_path.name)
    agents_path.write_text(content, encoding="utf-8")
    if not claude_path.is_symlink() and not claude_path.exists():
        os.symlink("AGENTS.md", claude_path)

    cli_common.qprint(
        f"refresh_guidance scaffold: scaffolded {agents_path} and {claude_path}",
        quiet=quiet,
    )


# ── CLI ───────────────────────────────────────────────────────────────────────


def _add_doc_set_args(parser: argparse.ArgumentParser) -> None:
    """Add `--repo-root`/`--doc-set`/`--agent-toolkit-root` to ``parser``.

    Called once per leaf subcommand rather than shared via `parents=`, so
    `gen_interfaces.py`'s static extraction attaches these to each
    subcommand's own argument list (the same `_add_id_arg`-style helper
    pattern `dev_status_impl.py` uses) -- a `parents=`-shared parser's
    `add_argument` calls land on `CliSpec.options` (top-level) instead,
    which `validate_invocation` never consults once a subcommand path has
    matched, so a doc example combining a subcommand with one of these
    flags would read as using an "unknown flag" that's actually real.
    """
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=DEFAULT_REPO_ROOT,
        help="repo root to scan (default: this checkout)",
    )
    parser.add_argument(
        "--doc-set",
        choices=sorted(DOC_SETS),
        default=None,
        help="which built-in doc-set config to use. No default -- either "
        "pass this, or let <repo-root>/refresh-guidance.toml be "
        "auto-discovered (never both).",
    )
    parser.add_argument(
        "--agent-toolkit-root",
        type=Path,
        default=DEFAULT_AGENT_TOOLKIT_ROOT,
        help="agent-toolkit checkout used to resolve cross-repo script "
        "citations for doc-sets with cross_repo_scripts set (default: "
        "$AGENT_TOOLKIT_PATH or ~/Workspace/agent-toolkit)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit hand-authored, agent-facing docs for mechanically-"
        "checkable stale references and per-section human-review staleness."
    )
    # --quiet/-v and --repo-root/--doc-set are defined once, on every leaf
    # subcommand parser only (via this shared `parents=` parser) -- never on
    # `parser` itself. See dev_status_impl.py's build_parser() for the full
    # rationale.
    verbosity_parent = argparse.ArgumentParser(add_help=False)
    cli_common.add_verbosity_args(verbosity_parent)

    subparsers = parser.add_subparsers(dest="subcommand")

    check_parser = subparsers.add_parser(
        "check",
        help="scan the configured doc-set and print a findings + staleness report (default)",
        parents=[verbosity_parent],
    )
    _add_doc_set_args(check_parser)

    mark_parser = subparsers.add_parser(
        "mark-reviewed",
        help="record human sign-off that one doc's `## <heading>` section is current",
        parents=[verbosity_parent],
    )
    _add_doc_set_args(mark_parser)
    mark_parser.add_argument("doc", help="repo-relative doc path, e.g. AGENTS.md")
    mark_parser.add_argument("heading", help="exact `## <heading>` text")
    mark_parser.add_argument(
        "--commit", default=None, help="commit sha to record (default: current HEAD)"
    )
    mark_parser.add_argument(
        "--date", default=None, help="YYYY-MM-DD to record (default: today)"
    )

    scaffold_parser = subparsers.add_parser(
        "scaffold",
        help="scaffold a rubric-compliant AGENTS.md and paired CLAUDE.md symlink in a directory",
        parents=[verbosity_parent],
    )
    _add_doc_set_args(scaffold_parser)
    scaffold_parser.add_argument(
        "directory", help="repo-relative directory path to scaffold"
    )
    scaffold_parser.add_argument(
        "--force",
        "-f",
        action="store_true",
        help="overwrite existing AGENTS.md or CLAUDE.md",
    )

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    subcommand = args.subcommand or "check"
    quiet = getattr(args, "quiet", False)
    repo_root = getattr(args, "repo_root", DEFAULT_REPO_ROOT).resolve()
    doc_set_name = getattr(args, "doc_set", None)
    agent_toolkit_root = getattr(args, "agent_toolkit_root", DEFAULT_AGENT_TOOLKIT_ROOT)

    if subcommand == "check":
        cmd_check(repo_root, doc_set_name, agent_toolkit_root, quiet=quiet)
    elif subcommand == "mark-reviewed":
        cmd_mark_reviewed(
            repo_root,
            doc_set_name,
            args.doc,
            args.heading,
            args.commit,
            args.date,
            quiet=quiet,
        )
    elif subcommand == "scaffold":
        cmd_scaffold(
            repo_root,
            args.directory,
            force=getattr(args, "force", False),
            quiet=quiet,
        )


if __name__ == "__main__":
    main()
