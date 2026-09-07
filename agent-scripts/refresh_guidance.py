#!/usr/bin/env python3
"""refresh_guidance.py — audit-by-inspection for hand-authored, agent-facing docs.

Scans a repo's hand-authored prose documentation (every ``AGENTS.md``, plus a
per-repo fixed list such as ``README.md``/``STYLE.md``) for two things: (1)
mechanically-checkable claims in backtick/code-span citations that no longer
hold — a cited repo-relative path that's gone, or a cited command/flag the
named script no longer has — and (2) which ``##`` sections have gone longest
without a human-confirmed review, using a sidecar state file plus a
git-history fallback for sections that have never been marked reviewed.

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
directories to scan is config (``DOC_SETS``), keyed by ``--doc-set``, so the
same checking logic runs against agent-toolkit and dotfiles alike — only the
per-repo doc/script-directory list differs. ``MIGRATION.md`` is deliberately
never in a doc-set's fixed list: it is transient, self-obsoleting migration
prose, not evergreen guidance.

Usage:
    refresh_guidance.py check --repo-root <path> --doc-set <name>
        scan and print a findings + staleness report (default subcommand)
    refresh_guidance.py mark-reviewed <doc> <heading> --repo-root <path> --doc-set <name>
        record human sign-off that one doc's `## <heading>` section is current

Flags: --repo-root <path> (default: this checkout), --doc-set
{agent-toolkit,dotfiles} (default: agent-toolkit), --commit <sha> and --date
<YYYY-MM-DD> (mark-reviewed only; default to current HEAD / today),
--quiet/-q, --verbose/-v.
Env vars: none.
Files read: every doc named by the active doc-set's config, every script
under its configured script directories (parsed with `ast`, never imported
or executed — these tools mutate live state, so auditing them must not run
them), and its sidecar state file if present.
Files written: the active doc-set's sidecar state file
(`refresh-guidance-state.json` at the repo root, by default), only by
`mark-reviewed`.
Exit codes: 0 success; 2 bad usage (unknown doc-set, missing repo root, or
— for `mark-reviewed` — a doc/heading pair that doesn't exist).

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
    the same `AGENT_TOOLKIT_PATH` convention `scripts/install-with-agent-
    toolkit.sh` and `claude/scripts/test_dev_status_sync.py` already use.
    Real, evidence-based need: post-cutover, dotfiles' own docs legitimately
    cite `dev_status.py`/`second_opinion.py`/etc. bare, meaning "the shared
    tool, now hosted in agent-toolkit" -- without this, every one of those
    reads as a broken reference even though the doc is correct."""
    claim_exempt_docs: tuple[str, ...] = ("CHANGELOG.md",)
    """Docs still scanned for `##` sections and staleness, but never for
    mechanical path/command claims.

    `CHANGELOG.md` is a historical narrative -- each entry describes what
    was true *at that commit*, not what's true now, so "this path from a
    six-month-old entry no longer exists" isn't staleness, it's the entry
    doing its job (confirmed against dotfiles' real CHANGELOG.md: the
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
            "CHANGELOG.md",
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
    "dotfiles": DocSetConfig(
        fixed_docs=("README.md", "STYLE.md", "CHANGELOG.md"),
        script_dirs=("claude/scripts", "scripts"),
        cross_repo_scripts=True,
    ),
}


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


def discover_basename_index(repo_root: Path) -> dict[str, list[str]]:
    """Map every tracked file's basename to its repo-relative path(s).

    The fallback :func:`check_path_claim` uses for a bare filename citation
    (no directory component) that doesn't exist exactly where cited --
    existence anywhere in the repo is enough to call the claim valid.
    Empty (not None) when git is unavailable, so callers need no special
    case: a bare-filename fallback just never matches.
    """
    index: dict[str, list[str]] = {}
    for relpath in _tracked_files(repo_root) or []:
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
    claim: Claim, repo_root: Path, basename_index: dict[str, list[str]]
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
    """
    path_part, _, fragment = claim.raw.partition("#")
    if not path_part:
        return None
    target = repo_root / path_part
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


def run_check(
    repo_root: Path,
    doc_set_name: str,
    agent_toolkit_root: Path = DEFAULT_AGENT_TOOLKIT_ROOT,
) -> CheckResult:
    doc_set = DOC_SETS[doc_set_name]
    docs = discovered_docs(repo_root, doc_set)
    scripts = discover_scripts(repo_root, doc_set, agent_toolkit_root)
    known_basenames = set(scripts)
    basename_index = discover_basename_index(repo_root)
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
                check_path_claim(claim, repo_root, basename_index)
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

    return CheckResult(findings=findings, sections=sections)


def render_report(result: CheckResult, doc_set_name: str) -> str:
    lines: list[str] = [f"refresh-guidance report — doc-set: {doc_set_name}", ""]

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

    return "\n".join(lines) + "\n"


def cmd_check(
    repo_root: Path,
    doc_set_name: str,
    agent_toolkit_root: Path = DEFAULT_AGENT_TOOLKIT_ROOT,
    quiet: bool = False,
) -> None:
    if doc_set_name not in DOC_SETS:
        print(
            f"refresh_guidance: unknown doc-set {doc_set_name!r}; choices: {sorted(DOC_SETS)}",
            file=sys.stderr,
        )
        sys.exit(2)
    if not repo_root.is_dir():
        print(f"refresh_guidance: no such repo root {repo_root}", file=sys.stderr)
        sys.exit(2)
    result = run_check(repo_root, doc_set_name, agent_toolkit_root)
    if not quiet:
        print(render_report(result, doc_set_name), end="")


def cmd_mark_reviewed(
    repo_root: Path,
    doc_set_name: str,
    doc: str,
    heading: str,
    commit: str | None,
    date: str | None,
    quiet: bool = False,
) -> None:
    if doc_set_name not in DOC_SETS:
        print(
            f"refresh_guidance: unknown doc-set {doc_set_name!r}; choices: {sorted(DOC_SETS)}",
            file=sys.stderr,
        )
        sys.exit(2)
    doc_set = DOC_SETS[doc_set_name]
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


# ── CLI ───────────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Audit hand-authored, agent-facing docs for mechanically-"
        "checkable stale references and per-section human-review staleness."
    )
    # --quiet/-v and --repo-root/--doc-set are defined once, on every leaf
    # subcommand parser only (via these shared `parents=` parsers) -- never
    # on `parser` itself. See dev_status_impl.py's build_parser() for the
    # full rationale.
    verbosity_parent = argparse.ArgumentParser(add_help=False)
    cli_common.add_verbosity_args(verbosity_parent)

    doc_set_parent = argparse.ArgumentParser(add_help=False)
    doc_set_parent.add_argument(
        "--repo-root",
        type=Path,
        default=DEFAULT_REPO_ROOT,
        help="repo root to scan (default: this checkout)",
    )
    doc_set_parent.add_argument(
        "--doc-set",
        choices=sorted(DOC_SETS),
        default="agent-toolkit",
        help="which per-repo doc-set config to use (default: agent-toolkit)",
    )
    doc_set_parent.add_argument(
        "--agent-toolkit-root",
        type=Path,
        default=DEFAULT_AGENT_TOOLKIT_ROOT,
        help="agent-toolkit checkout used to resolve cross-repo script "
        "citations for doc-sets with cross_repo_scripts set (default: "
        "$AGENT_TOOLKIT_PATH or ~/Workspace/agent-toolkit)",
    )

    subparsers = parser.add_subparsers(dest="subcommand")

    subparsers.add_parser(
        "check",
        help="scan the configured doc-set and print a findings + staleness report (default)",
        parents=[verbosity_parent, doc_set_parent],
    )

    mark_parser = subparsers.add_parser(
        "mark-reviewed",
        help="record human sign-off that one doc's `## <heading>` section is current",
        parents=[verbosity_parent, doc_set_parent],
    )
    mark_parser.add_argument("doc", help="repo-relative doc path, e.g. AGENTS.md")
    mark_parser.add_argument("heading", help="exact `## <heading>` text")
    mark_parser.add_argument(
        "--commit", default=None, help="commit sha to record (default: current HEAD)"
    )
    mark_parser.add_argument(
        "--date", default=None, help="YYYY-MM-DD to record (default: today)"
    )

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    subcommand = args.subcommand or "check"
    quiet = getattr(args, "quiet", False)
    repo_root = getattr(args, "repo_root", DEFAULT_REPO_ROOT).resolve()
    doc_set_name = getattr(args, "doc_set", "agent-toolkit")
    agent_toolkit_root = getattr(args, "agent_toolkit_root", DEFAULT_AGENT_TOOLKIT_ROOT)

    if subcommand == "check":
        cmd_check(repo_root, doc_set_name, agent_toolkit_root, quiet=quiet)
    else:
        cmd_mark_reviewed(
            repo_root,
            doc_set_name,
            args.doc,
            args.heading,
            args.commit,
            args.date,
            quiet=quiet,
        )


if __name__ == "__main__":
    main()
