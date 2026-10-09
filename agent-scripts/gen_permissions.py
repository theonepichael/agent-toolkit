#!/usr/bin/env python3
"""gen_permissions.py — compile the shared bash-permission matrix into every seed.

Compiles agent-scripts/permission_matrix.py into each harness's global seed:
the ``permissions`` allow/ask/deny lists of claude/settings.json and
claude/settings.work.json, the ``permission.bash`` map of
opencode/opencode.jsonc, and the anchored region of
pi/extensions/permission-gate.ts. Only those keys/that region are rewritten;
every other key (hooks, external_directory, agent, ...) is left as it is.

Each emitter reproduces the matrix's neutral semantics in its harness's own
model: Claude's deny>ask>allow precedence, opencode's last-match-wins glob
map behind a ``"*": "ask"`` catch-all, and Pi's own classify(). Ask and deny
rules are emitted a second time behind a leading ``* `` so they still match
after a leading assignment or wrapper (``DEVSTATUS_AGENT=1 ...``,
``nice -n 5 ...``).

Never run this and gen_hooks.py concurrently: both rewrite
claude/settings*.json whole, and gen_hooks.py writes the snapshot it read.
Run serially, in either order, each leaves the other's --check green.

Usage:
    python3 agent-scripts/gen_permissions.py              rewrite every target
    python3 agent-scripts/gen_permissions.py --check      exit 1 if any target is stale
    python3 agent-scripts/gen_permissions.py --stdout     print the rendered targets,
                                                          write nothing
    python3 agent-scripts/gen_permissions.py --audit-live compare the live Claude and
                                                          opencode configs to the seeds

Flags: --check, --stdout, --audit-live, --home <path>, --repo-root <path>,
--quiet/-q, --verbose/-v.
Env vars: none.
Files read: agent-scripts/permission_matrix.py, claude/settings.json,
claude/settings.work.json, opencode/opencode.jsonc,
pi/extensions/permission-gate.ts; with --audit-live also ~/.claude/settings.json,
~/.config/opencode/opencode.jsonc and the profile marker
~/.local/state/agent-toolkit/profile (it never reads or writes
settings.local.json).
Files written: claude/settings.json, claude/settings.work.json,
opencode/opencode.jsonc, pi/extensions/permission-gate.ts (default mode only;
atomic replace). --check, --stdout and --audit-live write nothing.
Exit codes: 0 success / clean; 1 --check found stale output, or --audit-live
found live drift; 2 bad usage, an invalid matrix, or an unreadable target
(missing/duplicated anchor, unparseable JSON).

Requires Python 3.12+.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

import cli_common
import permission_matrix as pm
from permission_matrix import Tier

# Reads and rewrites tracked seeds; --audit-live reads live harness configs.
TOOLKIT_DATA = "harness-owned"

ANCHOR_BEGIN = "// <permission-matrix:begin>"
ANCHOR_END = "// <permission-matrix:end>"
CLAUDE_TARGETS: dict[str, str] = {
    "claude/settings.json": "claude",
    "claude/settings.work.json": "claude-work",
}
OPENCODE_SEED = "opencode/opencode.jsonc"
PI_GATE = "pi/extensions/permission-gate.ts"
CORPUS = "test/fixtures/permission_corpus.json"
PRETTIER_WIDTH = 100


class GenerationError(Exception):
    """A target cannot be rendered safely; nothing is written."""


# ── emitters ────────────────────────────────────────────────────────────────


def to_glob(pattern: str) -> str:
    """Neutral pattern -> glob for targets whose only wildcard is ``*``."""
    return pattern.replace(pm.DIR_TOKEN, "*")


def _gated(patterns: tuple[str, ...]) -> list[str]:
    """Ask/deny patterns as globs, each also behind a leading ``* ``."""
    out: list[str] = []
    for pattern in patterns:
        glob = to_glob(pattern)
        out.extend((glob, f"* {glob}"))
    return out


def emit_claude(rules: dict[Tier, tuple[str, ...]]) -> dict[str, list[str]]:
    """Claude ``permissions`` lists (precedence is deny>ask>allow by design)."""
    return {
        "allow": [f"Bash({to_glob(p)})" for p in rules[Tier.ALLOW]],
        "deny": [f"Bash({g})" for g in _gated(rules[Tier.DENY])],
        "ask": [f"Bash({g})" for g in _gated(rules[Tier.ASK])],
    }


def emit_opencode(rules: dict[Tier, tuple[str, ...]]) -> dict[str, str]:
    """opencode ``permission.bash``: last match wins, so allow < ask < deny."""
    bash: dict[str, str] = {"*": "ask"}
    for pattern in rules[Tier.ALLOW]:
        bash[to_glob(pattern)] = "allow"
    for glob in _gated(rules[Tier.ASK]):
        bash[glob] = "ask"
    for glob in _gated(rules[Tier.DENY]):
        bash[glob] = "deny"
    return bash


def _ts_array(name: str, patterns: tuple[str, ...]) -> str:
    items = [json.dumps(p, ensure_ascii=False) for p in patterns]
    head = f"export const {name}: string[] = ["
    one_line = head + ", ".join(items) + "];"
    if len(one_line) <= PRETTIER_WIDTH:
        return one_line
    return head + "\n" + "".join(f"  {item},\n" for item in items) + "];"


def emit_pi_region(rules: dict[Tier, tuple[str, ...]]) -> str:
    """The text between (and including) the permission-gate.ts anchors."""
    return "\n".join(
        (
            ANCHOR_BEGIN,
            "// Generated by agent-scripts/gen_permissions.py from",
            "// agent-scripts/permission_matrix.py. Do not edit by hand: edit the",
            "// matrix and regenerate.",
            _ts_array("ALLOW_PATTERNS", rules[Tier.ALLOW]),
            _ts_array("ASK_PATTERNS", rules[Tier.ASK]),
            _ts_array("DENY_PATTERNS", rules[Tier.DENY]),
            ANCHOR_END,
        )
    )


def splice_region(text: str, region: str, *, where: str) -> str:
    """Replace the single anchored region in ``text`` with ``region``."""
    if text.count(ANCHOR_BEGIN) != 1 or text.count(ANCHOR_END) != 1:
        raise GenerationError(
            f"{where}: expected exactly one {ANCHOR_BEGIN!r} and one {ANCHOR_END!r}"
        )
    start = text.index(ANCHOR_BEGIN)
    end = text.index(ANCHOR_END) + len(ANCHOR_END)
    if end < start:
        raise GenerationError(f"{where}: {ANCHOR_END!r} comes before {ANCHOR_BEGIN!r}")
    return text[:start] + region + text[end:]


def _load_json(path: Path, rel: str) -> dict[str, object]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise GenerationError(f"{rel}: cannot read as strict JSON ({exc})") from exc
    if not isinstance(data, dict):
        raise GenerationError(f"{rel}: top level is not an object")
    return data


def _dump(data: dict[str, object]) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def _render_claude(repo_root: Path, rel: str) -> str:
    data = _load_json(repo_root / rel, rel)
    existing = data.get("permissions")
    perms: dict[str, object] = dict(existing) if isinstance(existing, dict) else {}
    perms.update(emit_claude(pm.rules_for(CLAUDE_TARGETS[rel])))
    data["permissions"] = perms
    return _dump(data)


def _render_opencode(repo_root: Path) -> str:
    data = _load_json(repo_root / OPENCODE_SEED, OPENCODE_SEED)
    existing = data.get("permission")
    perm: dict[str, object] = dict(existing) if isinstance(existing, dict) else {}
    perm["bash"] = emit_opencode(pm.rules_for("opencode"))
    data["permission"] = perm
    return _dump(data)


def _render_pi(repo_root: Path) -> str:
    try:
        text = (repo_root / PI_GATE).read_text(encoding="utf-8")
    except OSError as exc:
        raise GenerationError(f"{PI_GATE}: cannot read ({exc})") from exc
    return splice_region(text, emit_pi_region(pm.rules_for("pi")), where=PI_GATE)


def _renderers(repo_root: Path) -> dict[Path, Callable[[], str]]:
    renderers: dict[Path, Callable[[], str]] = {
        repo_root / rel: (lambda rel=rel: _render_claude(repo_root, rel))
        for rel in CLAUDE_TARGETS
    }
    renderers[repo_root / OPENCODE_SEED] = lambda: _render_opencode(repo_root)
    renderers[repo_root / PI_GATE] = lambda: _render_pi(repo_root)
    return renderers


def compile_permissions(repo_root: Path) -> dict[Path, str]:
    """Render every target; raises GenerationError on an invalid matrix or target."""
    problems = pm.validate()
    if problems:
        raise GenerationError(
            "permission_matrix.py is invalid:\n  " + "\n  ".join(problems)
        )
    return {path: render() for path, render in _renderers(repo_root).items()}


def _atomic_write(path: Path, content: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        if path.exists():
            os.chmod(tmp, path.stat().st_mode & 0o7777)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


# ── matchers (models of each harness, used by tests and --audit-live) ───────


def scan_command(command: str) -> tuple[list[str], bool, bool]:
    """Quote-aware split into segments, as Pi's scanCommand does.

    Returns ``(segments, substitution, ambiguous)``. Splits on top-level
    ``;``, ``&&``, ``||``, ``|``, ``|&``, ``&`` and newline, never inside
    quotes or on a redirection's ``&`` (``2>&1``); reports command
    substitution and unbalanced quoting rather than resolving them.
    """
    segments: list[str] = []
    current: list[str] = []
    substitution = ambiguous = False
    state = "normal"
    i, n = 0, len(command)

    def end() -> None:
        text = "".join(current).strip()
        if text:
            segments.append(text)
        current.clear()

    while i < n:
        c = command[i]
        nxt = command[i + 1] if i + 1 < n else ""
        if state == "single":
            state = "normal" if c == "'" else state
            current.append(c)
            i += 1
            continue
        if state == "double":
            if c == '"':
                state = "normal"
            elif c == "\\" and nxt in ('"', "\\", "\n"):
                current.append(c + nxt)
                i += 2
                continue
            elif c == "`" or (c == "$" and nxt == "("):
                substitution = True
            current.append(c)
            i += 1
            continue
        if c == "\\":
            if not nxt:
                ambiguous = True
                break
            current.append(c + nxt)
            i += 2
            continue
        if c in "'\"":
            state = "single" if c == "'" else "double"
            current.append(c)
            i += 1
            continue
        if c == "`" or (c == "$" and nxt == "(") or (c in "<>" and nxt == "("):
            substitution = True
            current.append(c)
            i += 1
            continue
        if c == "#" and not "".join(current).strip():
            while i < n and command[i] != "\n":
                i += 1
            continue
        if c in ";\n":
            end()
            i += 1
            continue
        if c == "|":
            end()
            i += 2 if nxt in "|&" and nxt else 1
            continue
        if c == "&":
            prev = command[i - 1] if i else ""
            if nxt == ">" or prev in "<>" and prev:
                current.append(c)
                i += 1
                continue
            end()
            i += 2 if nxt == "&" else 1
            continue
        current.append(c)
        i += 1
    if state != "normal":
        ambiguous = True
    end()
    return segments, substitution, ambiguous


def _combine(command: str, segment_verdict: Callable[[str], str | None]) -> str:
    segments, substitution, ambiguous = scan_command(command.strip())
    if not segments:
        return "ask"
    verdicts = [segment_verdict(s) for s in segments]
    if "deny" in verdicts:
        return "deny"
    if substitution or ambiguous or "ask" in verdicts:
        return "ask"
    return "allow" if all(v == "allow" for v in verdicts) else "ask"


def claude_match(command: str, glob: str) -> bool:
    """Claude Code's documented Bash-rule glob (code.claude.com/docs/en/permissions).

    ``*`` matches any text including spaces; a trailing `` *`` that is the
    rule's only wildcard also matches the bare command.
    """
    if glob.endswith(" *") and glob.count("*") == 1:
        regex = re.escape(glob[:-2]) + "(?: .*)?"
    else:
        regex = ".*".join(re.escape(part) for part in glob.split("*"))
    return re.fullmatch(regex, command, re.DOTALL) is not None


def claude_verdict(perms: dict[str, object], command: str) -> str:
    """Rules-only Claude verdict: deny > ask > allow per segment, default ask.

    Claude's built-in read-only analyzer is deliberately not modeled.
    """

    def globs(tier: str) -> list[str]:
        rules = perms.get(tier, [])
        if not isinstance(rules, list):
            return []
        return [r[5:-1] for r in rules if isinstance(r, str) and r.startswith("Bash(")]

    deny, ask, allow = globs("deny"), globs("ask"), globs("allow")

    def segment(text: str) -> str | None:
        for tier, patterns in (("deny", deny), ("ask", ask), ("allow", allow)):
            if any(claude_match(text, g) for g in patterns):
                return tier
        return None

    return _combine(command, segment)


def opencode_match(command: str, pattern: str) -> bool:
    """Transcription of opencode's ``Wildcard.match`` (binary 0.0.0-dev-202609250015)."""
    command = command.replace("\\", "/")
    regex = re.sub(
        r"[.+^${}()|\[\]\\]", lambda m: "\\" + m.group(0), pattern.replace("\\", "/")
    )
    regex = regex.replace("*", ".*").replace("?", ".")
    if regex.endswith(" .*"):
        regex = regex[:-3] + "( .*)?"
    return re.fullmatch(regex, command, re.DOTALL) is not None


def opencode_verdict(bash: dict[str, object], command: str) -> str:
    """opencode verdict: the last matching ``permission.bash`` entry, per segment."""
    items = [(k, v) for k, v in bash.items() if isinstance(v, str)]

    def segment(text: str) -> str | None:
        found = None
        for pattern, verdict in items:
            if opencode_match(text, pattern):
                found = verdict
        return found

    return _combine(command, segment)


# ── --audit-live ────────────────────────────────────────────────────────────


def _profile(home: Path) -> str:
    marker = home / ".local" / "state" / "agent-toolkit" / "profile"
    try:
        return marker.read_text(encoding="utf-8").strip() or "personal"
    except OSError:
        return "personal"


def _corpus_diffs(
    repo_root: Path, verdict: Callable[[str], str], seed: Callable[[str], str]
) -> list[str]:
    try:
        cases = json.loads((repo_root / CORPUS).read_text(encoding="utf-8"))["cases"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        return []
    out = []
    for case in cases:
        command = case["command"]
        live, expected = verdict(command), seed(command)
        if live != expected:
            out.append(
                f"    corpus: {command!r} is {live} live, {expected} in the seed"
            )
    return out


def audit_live(repo_root: Path, home: Path) -> tuple[int, list[str]]:
    """Compare live Claude/opencode permission rules to the generated seeds."""
    lines: list[str] = []
    drift = False
    outputs = compile_permissions(repo_root)

    target = "claude-work" if _profile(home) == "work" else "claude"
    rel = next(r for r, t in CLAUDE_TARGETS.items() if t == target)
    seed_perms = json.loads(outputs[repo_root / rel])["permissions"]
    live_path = home / ".claude" / "settings.json"
    lines.append(f"Claude ({target} seed {rel}) vs {live_path}:")
    if not live_path.is_file():
        lines.append("  not present — skipped")
    else:
        try:
            live = json.loads(live_path.read_text(encoding="utf-8"))
            live_perms = live.get("permissions", {}) if isinstance(live, dict) else None
        except json.JSONDecodeError as exc:
            live_perms, drift = None, True
            lines.append(f"  unreadable JSON: {exc}")
        else:
            if not isinstance(live_perms, dict):
                live_perms, drift = None, True
                lines.append("  `permissions` is not an object")
        if isinstance(live_perms, dict):
            for tier in ("allow", "ask", "deny"):
                raw = live_perms.get(tier, [])
                if not isinstance(raw, list):
                    lines.append(f"  `permissions.{tier}` is not a list")
                    raw = []
                malformed = [v for v in raw if not isinstance(v, str)]
                if malformed:
                    lines.append(
                        f"  `permissions.{tier}` has non-string entries: "
                        + ", ".join(repr(v) for v in malformed)
                    )
                    drift = True
                have = {v for v in raw if isinstance(v, str)}
                want = set(seed_perms[tier])
                for extra in sorted(have - want):
                    lines.append(f"  live-only {tier}: {extra}")
                for missing in sorted(want - have):
                    lines.append(f"  missing {tier}: {missing}")
                drift |= have != want
            lines.extend(
                _corpus_diffs(
                    repo_root,
                    lambda c: claude_verdict(live_perms, c),
                    lambda c: claude_verdict(seed_perms, c),
                )
            )

    seed_bash = json.loads(outputs[repo_root / OPENCODE_SEED])["permission"]["bash"]
    live_path = home / ".config" / "opencode" / "opencode.jsonc"
    lines.append(f"opencode ({OPENCODE_SEED}) vs {live_path}:")
    if not live_path.is_file():
        lines.append("  not present — skipped")
    else:
        try:
            live = json.loads(live_path.read_text(encoding="utf-8"))
            perm = live.get("permission", {}) if isinstance(live, dict) else None
            live_bash = perm.get("bash", {}) if isinstance(perm, dict) else None
        except json.JSONDecodeError as exc:
            live_bash, drift = None, True
            lines.append(f"  unreadable JSON: {exc}")
        else:
            if not isinstance(live_bash, dict):
                live_bash, drift = None, True
                lines.append("  `permission.bash` is not an object")
        if isinstance(live_bash, dict):
            for key in live_bash:
                if key not in seed_bash:
                    lines.append(f"  live-only: {key!r}: {live_bash[key]!r}")
            for key, verdict in seed_bash.items():
                if key not in live_bash:
                    lines.append(f"  missing: {key!r}: {verdict!r}")
                elif live_bash[key] != verdict:
                    lines.append(
                        f"  verdict differs: {key!r} is {live_bash[key]!r} live, "
                        f"{verdict!r} in the seed"
                    )
            if live_bash != seed_bash:
                drift = True
            elif list(live_bash.items()) != list(seed_bash.items()):
                drift = True
                lines.append("  same rules, different order (last match wins)")
            lines.extend(
                _corpus_diffs(
                    repo_root,
                    lambda c: opencode_verdict(live_bash, c),
                    lambda c: opencode_verdict(seed_bash, c),
                )
            )

    lines.append(
        "Drift found: prune live entries by hand (or install.py --reseed, which "
        "discards live approvals), then re-run."
        if drift
        else "Live permission rules match the generated seeds."
    )
    return (1 if drift else 0), lines


# ── CLI ─────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compile permission_matrix.py into the Claude, opencode and Pi seeds."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if any target differs from compiled output",
    )
    mode.add_argument(
        "--stdout", action="store_true", help="print rendered targets and write nothing"
    )
    mode.add_argument(
        "--audit-live",
        action="store_true",
        help="read-only: compare live Claude/opencode permission rules to the seeds",
    )
    parser.add_argument(
        "--home",
        type=Path,
        default=None,
        help="home directory whose live configs --audit-live reads (default: $HOME)",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="repository root directory (default: parent of agent-scripts/)",
    )
    cli_common.add_verbosity_args(parser)
    args = parser.parse_args(argv)
    repo_root = (
        args.repo_root.resolve()
        if args.repo_root
        else Path(__file__).resolve().parent.parent
    )

    try:
        if args.audit_live:
            code, lines = audit_live(repo_root, args.home or Path.home())
            print("\n".join(lines))
            return code
        outputs = compile_permissions(repo_root)
    except GenerationError as exc:
        print(f"gen_permissions.py: {exc}", file=sys.stderr)
        return 2

    if args.stdout:
        for path, content in outputs.items():
            print(f"=== {path.relative_to(repo_root)} ===")
            print(content)
        return 0

    if args.check:
        stale = 0
        for path, compiled in outputs.items():
            current = path.read_text(encoding="utf-8")
            if current != compiled:
                stale += 1
                rel = path.relative_to(repo_root)
                sys.stderr.writelines(
                    difflib.unified_diff(
                        current.splitlines(keepends=True),
                        compiled.splitlines(keepends=True),
                        fromfile=f"a/{rel}",
                        tofile=f"b/{rel}",
                    )
                )
        if stale:
            cli_common.qprint(
                f"gen_permissions.py --check: {stale} file(s) stale.", quiet=args.quiet
            )
            return 1
        return 0

    # Re-render each target from a fresh read immediately before writing it.
    try:
        for path, render in _renderers(repo_root).items():
            content = render()
            if path.read_text(encoding="utf-8") != content:
                _atomic_write(path, content)
                cli_common.vprint(
                    f"Wrote {path.relative_to(repo_root)}", verbose=args.verbose
                )
    except GenerationError as exc:
        print(f"gen_permissions.py: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
