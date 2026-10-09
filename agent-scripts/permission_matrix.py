#!/usr/bin/env python3
"""Declarative bash-permission matrix shared by every harness's global seed.

The single hand-edited source of allow / ask / deny policy for bash commands.
``gen_permissions.py`` compiles it into ``claude/settings.json``,
``claude/settings.work.json``, ``opencode/opencode.jsonc`` (``permission.bash``)
and the generated region of ``pi/extensions/permission-gate.ts``. Edit this
file, never those copies, then run ``python3 agent-scripts/gen_permissions.py``.

Neutral pattern grammar (every emitter must reproduce it):

- literal text;
- ``*`` matches any text, spaces included;
- a trailing `` *`` (space-star, the pattern's only wildcard) also matches the
  bare command, so ``ls *`` matches ``ls``;
- ``<dir>`` matches exactly one whitespace-free argument, and is legal only
  directly after a leading ``git -C ``.

Forbidden: ``?`` (a wildcard in opencode), the legacy ``:*`` suffix, leading or
trailing whitespace, and a bare rule already covered by its own trailing
`` *`` rule.

Neutral semantics: a command is judged per segment; deny beats ask beats allow;
the default is ask. Allow rules match from the start of a segment only. Ask and
deny rules also match after any whitespace inside a segment, a deliberate
superset of "matches past leading assignments and wrappers" that over-matches
only in the safe direction.

Exceptions only ever remove ALLOW rules from one target, each with a written
capability reason, so no exception can weaken an ask or a deny.

Standard library only, like every other script in this directory.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

import harness_spec

# What this module does with toolkit data (checked by scripts/check_toolkit_paths.py).
TOOLKIT_DATA = "none"

DIR_TOKEN = "<dir>"
TOKENS: tuple[str, ...] = (DIR_TOKEN,)
MIN_REASON_LEN = 20

# Emitted targets -> the harness (harness_spec.HARNESSES key) each belongs to.
TARGETS: dict[str, str] = {
    "claude": "claude",
    "claude-work": "claude",
    "opencode": "opencode",
    "pi": "pi",
}

# Targets whose rule syntax can express each grammar token literally. A target
# missing from a token's set must carry a TokenException for it.
TOKEN_SUPPORT: dict[str, frozenset[str]] = {
    DIR_TOKEN: frozenset({"pi"}),
}


class Tier(StrEnum):
    """Permission tier of a rule."""

    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


@dataclass(frozen=True)
class Rule:
    """One neutral permission rule."""

    pattern: str
    tier: Tier
    group: str
    note: str = ""


@dataclass(frozen=True)
class PatternException:
    """Remove one named ALLOW pattern from one target."""

    target: str
    pattern: str
    reason: str


@dataclass(frozen=True)
class TokenException:
    """A target's syntax cannot express ``token``: drop its ALLOW rules there."""

    target: str
    token: str
    reason: str


_SCRIPTS = "~/.agent-toolkit/scripts"
_DEVSTATUS = f"python3 {_SCRIPTS}/dev_status.py *"


def _allow(group: str, *patterns: str, note: str = "") -> tuple[Rule, ...]:
    return tuple(Rule(p, Tier.ALLOW, group, note) for p in patterns)


_GIT_PLAIN: tuple[str, ...] = (
    "git log*",
    "git status*",
    "git diff*",
    "git show*",
    "git ls-files*",
    "git check-ignore*",
    "git add*",
    "git rev-parse*",
    # Listing forms only. `-a` and `-v` are exact: `git branch -v -D x`
    # deletes and `git branch -a -m x y` renames (probed 2026-09-30), while
    # `--list` and `--show-current` refuse every mutating combination.
    "git branch",
    "git branch --show-current",
    "git branch --list *",
    "git branch -a",
    "git branch -v",
    "git branch -vv",
    "git branch -av",
    "git branch -avv",
    # Creation forms. Their destructive flags are fenced by ASK rules below.
    "git checkout -b *",
    "git worktree list *",
    "git worktree add *",
)


RULES: tuple[Rule, ...] = (
    *_allow(
        "toolkit-scripts",
        f"python3 {_SCRIPTS}/dev_status.py *",
        f"python3 {_SCRIPTS}/grill.py *",
        f"python3 {_SCRIPTS}/second_opinion.py *",
        f"python3 {_SCRIPTS}/settings_seed_drift_check.py *",
        f"python3 {_SCRIPTS}/bundle_drift_check.py *",
        f"python3 {_SCRIPTS}/vitals_promotion.py *",
        "python3 agent-scripts/vitals_promotion.py *",
        f"python3 {_SCRIPTS}/worktree.py *",
        # Narrowed from `DEVSTATUS_AGENT=1 python3 *`, which allowed any script.
        f"DEVSTATUS_AGENT=1 {_DEVSTATUS}",
        f"env DEVSTATUS_AGENT=1 {_DEVSTATUS}",
    ),
    *_allow(
        "repo-scripts",
        "./scripts/bootstrap-worktree.sh*",
        "scripts/bootstrap-worktree.sh*",
        "./scripts/build-copilot-swarm.sh*",
        "scripts/build-copilot-swarm.sh*",
    ),
    *_allow(
        "npm",
        "npm install*",
        note=(
            "Deliberate allow (user-decided 2026-09-30): runs package lifecycle "
            "scripts, but the bootstrap and worktree flows depend on it. Kept off "
            "settings_seed._BYPASS_BASH_PATTERNS for that reason."
        ),
    ),
    *_allow(
        "npm",
        "npm test*",
        "npm run test*",
        "npm run lint*",
        "npm run typecheck*",
        "npm run format*",
    ),
    *_allow("git", *_GIT_PLAIN),
    *_allow("git-dir", *(f"git -C {DIR_TOKEN} {p[4:]}" for p in _GIT_PLAIN)),
    *_allow(
        "uv",
        "uv sync*",
        "uv run pytest*",
        "uv run ruff check*",
        "uv run ruff format*",
    ),
    *_allow(
        "read-only",
        "lsof *",
        "ps *",
        "ls*",
        "pwd",
        "which *",
        "head *",
        "tail *",
        "wc *",
        "sort *",
        "uniq *",
        "grep *",
        "rg *",
        "file *",
        "stat *",
        "du *",
        "df *",
        "date*",
        "whoami*",
        "env",
        "printenv*",
        "cat *",
        "strings *",
        "readlink *",
        "jq *",
        "diff *",
        "echo *",
        "pgrep *",
        "ss *",
        "systemctl status*",
        "systemctl is-active*",
        "systemctl is-enabled*",
    ),
    Rule("git commit*", Tier.ASK, "git-gates", "commit always needs approval"),
    Rule(f"git -C {DIR_TOKEN} commit*", Tier.ASK, "git-gates"),
    Rule(
        "git checkout -b * -*f*",
        Tier.ASK,
        "git-gates",
        "`checkout -b x -f` discards local changes; also catches --force/-qf/--forc",
    ),
    Rule(f"git -C {DIR_TOKEN} checkout -b * -*f*", Tier.ASK, "git-gates"),
    Rule("git worktree add*-B*", Tier.ASK, "git-gates", "-B resets an existing branch"),
    Rule(f"git -C {DIR_TOKEN} worktree add*-B*", Tier.ASK, "git-gates"),
    # difftool fences. `git diff*` above also matches
    # `git difftool -y -x 'sh -c …'`, which executes the supplied command;
    # fence the whole subcommand rather than trust the flag space to stay
    # inert.
    Rule(
        "git difftool*",
        Tier.ASK,
        "git-gates",
        "-x/--extcmd runs the supplied command; external diff tools execute",
    ),
    Rule(f"git -C {DIR_TOKEN} difftool*", Tier.ASK, "git-gates"),
    # sort -o writes its output into a file, overwriting it.
    Rule("sort -o *", Tier.ASK, "read-only", "`sort -o FILE` overwrites FILE"),
    Rule(f"python3 {_SCRIPTS}/dev_status.py prune*", Tier.DENY, "toolkit-scripts"),
)


_PI_NATIVE_TOOL_REASON = (
    "Pi ships a native tool for this script (pi/extensions/*-tool.ts); a bash "
    "allow would let the model bypass the tool silently, so a direct bash call "
    "stays at ask (and blocks headless)."
)
_GLOB_DIR_REASON = (
    "This target's rule syntax has only `*`, which matches any text including "
    "spaces, so it cannot constrain <dir> to one argument: `git -C * log*` would "
    "allow `git -C r reset --hard logfile`."
)

EXCEPTIONS: tuple[PatternException | TokenException, ...] = (
    *(
        PatternException("pi", p, _PI_NATIVE_TOOL_REASON)
        for p in (
            f"python3 {_SCRIPTS}/dev_status.py *",
            f"python3 {_SCRIPTS}/grill.py *",
            f"python3 {_SCRIPTS}/second_opinion.py *",
            f"python3 {_SCRIPTS}/vitals_promotion.py *",
            "python3 agent-scripts/vitals_promotion.py *",
            f"DEVSTATUS_AGENT=1 {_DEVSTATUS}",
            f"env DEVSTATUS_AGENT=1 {_DEVSTATUS}",
        )
    ),
    *(
        TokenException(t, DIR_TOKEN, _GLOB_DIR_REASON)
        for t in ("claude", "claude-work", "opencode")
    ),
)


def pattern_violations(pattern: str) -> list[str]:
    """Return every grammar violation in one neutral pattern."""
    problems: list[str] = []
    if pattern != pattern.strip():
        problems.append(f"{pattern!r}: leading or trailing whitespace")
    if not pattern.strip():
        problems.append(f"{pattern!r}: empty pattern")
    if "?" in pattern:
        problems.append(f"{pattern!r}: '?' is a wildcard in opencode")
    if ":*" in pattern:
        problems.append(f"{pattern!r}: legacy ':*' suffix; use ' *'")
    if pattern.endswith(" *") and pattern.count("*") > 1:
        # Claude makes a trailing " *" match the bare command only when it is
        # the sole wildcard; opencode always does. Forbid the case they differ.
        problems.append(f"{pattern!r}: a trailing ' *' must be the only '*'")
    count = pattern.count(DIR_TOKEN)
    if count and (count > 1 or not pattern.startswith(f"git -C {DIR_TOKEN} ")):
        problems.append(
            f"{pattern!r}: {DIR_TOKEN} is legal only after a leading 'git -C '"
        )
    return problems


def validate(
    rules: tuple[Rule, ...] = RULES,
    exceptions: tuple[PatternException | TokenException, ...] = EXCEPTIONS,
) -> list[str]:
    """Return every violation in the matrix; ``[]`` when it is sound."""
    problems: list[str] = []
    seen: set[str] = set()
    by_tier: dict[Tier, set[str]] = {tier: set() for tier in Tier}
    for rule in rules:
        problems.extend(pattern_violations(rule.pattern))
        if rule.pattern in seen:
            problems.append(f"{rule.pattern!r}: duplicate rule")
        seen.add(rule.pattern)
        by_tier[rule.tier].add(rule.pattern)
    for tier, patterns in by_tier.items():
        for pattern in patterns:
            if "*" not in pattern and f"{pattern} *" in patterns:
                problems.append(
                    f"{pattern!r}: redundant bare {tier} rule ({pattern + ' *'!r} "
                    "already matches the bare command)"
                )

    unknown_harnesses = set(TARGETS.values()) - set(harness_spec.HARNESSES)
    for name in sorted(unknown_harnesses):
        problems.append(f"target harness {name!r} is not in harness_spec.HARNESSES")

    keys: set[tuple[str, str, str]] = set()
    for exc in exceptions:
        kind = "pattern" if isinstance(exc, PatternException) else "token"
        subject = exc.pattern if isinstance(exc, PatternException) else exc.token
        key = (kind, exc.target, subject)
        if key in keys:
            problems.append(f"duplicate {kind} exception {exc.target}/{subject!r}")
        keys.add(key)
        if exc.target not in TARGETS:
            problems.append(f"exception for unknown target {exc.target!r}")
        if len(exc.reason.strip()) < MIN_REASON_LEN:
            problems.append(
                f"{kind} exception {exc.target}/{subject!r} needs a written reason "
                f"(at least {MIN_REASON_LEN} characters)"
            )
        if isinstance(exc, PatternException):
            if exc.pattern not in by_tier[Tier.ALLOW]:
                problems.append(
                    f"pattern exception {exc.target}/{exc.pattern!r} names no ALLOW "
                    "rule (exceptions may only remove allows)"
                )
        elif exc.token not in TOKENS:
            problems.append(f"token exception names unknown token {exc.token!r}")

    for token, supported in TOKEN_SUPPORT.items():
        if not any(token in p for p in by_tier[Tier.ALLOW]):
            continue
        for target in TARGETS:
            if target in supported:
                continue
            if ("token", target, token) not in keys:
                problems.append(
                    f"target {target!r} cannot express {token} but has no "
                    "TokenException for it"
                )
    return problems


def rules_for(
    target: str,
    rules: tuple[Rule, ...] = RULES,
    exceptions: tuple[PatternException | TokenException, ...] = EXCEPTIONS,
) -> dict[Tier, tuple[str, ...]]:
    """Neutral patterns per tier for ``target``, in matrix order, exceptions applied."""
    if target not in TARGETS:
        raise KeyError(f"unknown permission target {target!r}")
    removed = {
        e.pattern
        for e in exceptions
        if isinstance(e, PatternException) and e.target == target
    }
    tokens = {
        e.token
        for e in exceptions
        if isinstance(e, TokenException) and e.target == target
    }
    out: dict[Tier, list[str]] = {tier: [] for tier in Tier}
    for rule in rules:
        if rule.tier is Tier.ALLOW and (
            rule.pattern in removed or any(t in rule.pattern for t in tokens)
        ):
            continue
        out[rule.tier].append(rule.pattern)
    return {tier: tuple(patterns) for tier, patterns in out.items()}


def shared_allow(rules: tuple[Rule, ...] = RULES) -> frozenset[str]:
    """Every ALLOW pattern in the matrix, before any exception."""
    return frozenset(r.pattern for r in rules if r.tier is Tier.ALLOW)
