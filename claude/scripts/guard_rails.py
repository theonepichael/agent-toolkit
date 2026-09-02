#!/usr/bin/env python3
"""Pre-tool guard shared by every harness: refuse a write into a repository's
main checkout while a backlog item for that repository is in progress, warn
when the current worktree's base has fallen behind ``origin/main``, and (Bash,
Claude Code only) deny the git-native ways to defeat the no-commit-on-main
git hook (``githooks/pre-commit`` / ``githooks-global/pre-commit``).

Claude Code, agy and Copilot pipe their native hook payload in on stdin and
get their native verdict back on stdout. Pi and opencode already hold the
parsed values, so they pass them as flags and read a neutral JSON verdict.

Write-family tool calls get the full R2/R3 check above. Bash calls get a
narrower check: blocking ``git commit`` itself by parsing a shell string was
shown to be bypassable (``bash -c``, a heredoc fed to ``sh``, a mid-word
backslash) and is not attempted here -- that job now belongs to the git hook,
which sees the fully shell-resolved state. What Bash *is* checked for is the
git-native ways to defeat that hook on a protected branch: ``--no-verify``,
overriding or redirecting ``core.hooksPath`` (``-c``, ``GIT_CONFIG_*`` env
vars, a plain ``git config`` mutation), and a direct write to
``.git/config``. agy and Copilot get no bash-family wiring (best-effort
tier); their Bash calls fall through unrecognized, same as before.

Usage:
    guard_rails.py --harness claude|agy|copilot        read stdin, write that harness's verdict
    guard_rails.py --tool T --cwd D --path P           neutral write-family form for Pi/opencode
    guard_rails.py --tool bash --cwd D --command C     neutral bash-family form for Pi/opencode

Flags
  --harness NAME  read the named harness's payload on stdin and answer in its shape
  --tool NAME     tool name, for the neutral form
  --cwd DIR       session working directory, for the neutral form
  --path PATH     target file path, for the neutral write-family form
  --command CMD   shell command, for the neutral bash-family form
  --quiet, -q     suppress non-essential output
  --verbose, -v   emit extra diagnostic messages to stderr

Environment
  GUARD_RAILS_OFF=1    disable every rule
  GUARD_RAILS_STORE    path to an alternate backlog store, for exercising the
                       guard against a throwaway store

Set ``GUARD_RAILS_OFF=1`` in the environment the harness is launched from to
disable every rule. It is intentionally not reachable from an agent's own
shell: hooks are spawned by the host process, so a tool call's ``export``
never reaches this script.
"""

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import cli_common

DEFAULT_BACKLOG_ITEMS = Path.home() / ".claude" / "data" / "backlog" / "items.json"
PROTECTED_BRANCHES = {"main", "master"}
GIT_TIMEOUT = 2.0

WRITE_TOOLS = {
    "write",
    "edit",
    "multiedit",
    "notebookedit",
    "write_to_file",
    "replace_file_content",
    "create",
    "str_replace_editor",
}

# Shell control operators a value-token scan must stop at -- past one of
# these, whatever follows belongs to a different command, not an argument to
# the git-config invocation being scanned (e.g. `git config core.hooksPath ||
# echo unset` must read as a plain read, not a write with value `||`).
_CONTROL_OPERATORS = {"&&", "||", "|", ";", ">", ">>", "<", "\n"}


@dataclass(frozen=True)
class Request:
    """A normalized tool call: what family, from where, against which path
    (write-family) or command (bash-family)."""

    tool: str
    cwd: str
    path: str
    command: str = ""


@dataclass(frozen=True)
class Verdict:
    decision: str  # allow | deny | warn
    reason: str = ""


@dataclass(frozen=True)
class RepoInfo:
    toplevel: str
    common_dir: str
    is_worktree: bool
    is_bare: bool
    branch: str


def tool_family(name: object) -> str:
    """Collapse a harness's tool name to a family. Harnesses disagree on
    spelling -- Claude Code says ``Edit``, agy says ``replace_file_content``,
    opencode and Pi say ``edit`` -- so nothing matches a literal name."""
    if not isinstance(name, str):
        return ""
    lowered = name.strip().lower()
    if lowered in WRITE_TOOLS:
        return "write"
    if lowered == "bash":
        return "bash"
    return ""


def git(*args: str, cwd: str | None = None) -> str | None:
    """Run git, returning stripped stdout, or None on any failure. Every call
    is bounded: a guard that hangs is a guard that silently permits."""
    try:
        result = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
            cwd=cwd,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _absolutize(value: str, base: str) -> str:
    """``git -C <dir> rev-parse --git-common-dir`` prints relative to <dir>,
    not to this process's cwd, so a bare realpath() would resolve ``.git``
    against wherever the guard happens to be running and produce a path that
    does not exist."""
    if not os.path.isabs(value):
        value = os.path.join(base, value)
    return os.path.realpath(value)


def common_dir_of(directory: str) -> str | None:
    """Canonical git common directory for a path, or None if it is not in a
    repo. Two paths share a repository exactly when this value matches -- a
    linked worktree and its main checkout have different toplevels but the
    same common dir, which is why toplevel comparison cannot be used."""
    if not directory:
        return None
    out = git(
        "-C", directory, "rev-parse", "--path-format=absolute", "--git-common-dir"
    )
    if out:
        return os.path.realpath(out)
    out = git("-C", directory, "rev-parse", "--git-common-dir")
    if not out:
        return None
    return _absolutize(out, directory)


def repo_info(directory: str) -> RepoInfo | None:
    """Classify a directory: which repo, worktree or main checkout, bare or
    not, and on which branch. None when it is not a repository at all, or
    when git is too old to answer (``--git-common-dir`` needs git >= 2.5) --
    in which case the guard says so on stderr rather than guessing."""
    if not directory:
        return None
    out = git(
        "-C",
        directory,
        "rev-parse",
        "--git-dir",
        "--git-common-dir",
        "--is-bare-repository",
    )
    if not out:
        return None
    lines = out.splitlines()
    if len(lines) < 3:
        print(
            "[guard-rails] git did not report --git-common-dir; R2 is inert here",
            file=sys.stderr,
        )
        return None
    git_dir = _absolutize(lines[0], directory)
    common_dir = _absolutize(lines[1], directory)
    is_bare = lines[2].strip() == "true"
    toplevel = git("-C", directory, "rev-parse", "--show-toplevel") or ""
    branch = git("-C", directory, "branch", "--show-current") or ""
    return RepoInfo(
        toplevel=toplevel,
        common_dir=common_dir,
        is_worktree=git_dir != common_dir,
        is_bare=is_bare,
        branch=branch,
    )


def backlog_items_path() -> Path:
    """Where the backlog store lives. ``GUARD_RAILS_STORE`` overrides it so
    the guard can be exercised end-to-end against a throwaway store instead
    of the real one. Like ``GUARD_RAILS_OFF``, this is only reachable from
    the environment the harness was launched in -- an agent's own shell
    cannot reach the hook's environment."""
    override = os.environ.get("GUARD_RAILS_STORE")
    return Path(override) if override else DEFAULT_BACKLOG_ITEMS


def load_in_progress() -> list[dict] | None:
    """In-progress backlog items, or None when the store cannot be read."""
    try:
        data = json.loads(backlog_items_path().read_text())
    except (OSError, ValueError):
        return None
    items = data.get("items", []) if isinstance(data, dict) else data
    if not isinstance(items, list):
        return None
    return [
        i for i in items if isinstance(i, dict) and i.get("status") == "in-progress"
    ]


def _item_directories(item: dict) -> list[str]:
    """Deduplicated parent directories of an item's related_files. An item
    routinely lists several files in one repo; without dedup each one costs
    its own git call and its own timeout."""
    seen: dict[str, None] = {}
    for entry in item.get("related_files") or []:
        path = entry.get("path") if isinstance(entry, dict) else entry
        if not isinstance(path, str) or not path:
            continue
        seen.setdefault(os.path.dirname(path) or path, None)
    return list(seen)


def _busy_item(common_dir: str, items: list[dict]) -> str | None:
    """Slug of an in-progress item belonging to this repository, if any."""
    for item in items:
        for directory in _item_directories(item):
            if common_dir_of(directory) == common_dir:
                return str(item.get("id") or "?")
    return None


def _behind_origin_main(directory: str) -> bool:
    """Whether this worktree's base is behind the already-fetched
    origin/main. Never fetches -- a guard must not need the network."""
    if git("-C", directory, "rev-parse", "--verify", "--quiet", "origin/main") is None:
        return False
    counts = git(
        "-C", directory, "rev-list", "--left-right", "--count", "origin/main...HEAD"
    )
    if not counts:
        return False
    try:
        behind = int(counts.split()[0])
    except (ValueError, IndexError):
        return False
    return behind > 0


def _shell_tokens(command: str) -> list[str]:
    """Tokenize a shell command, keeping control operators (``&&``, ``||``,
    ``|``, ``;``, ``>``, ``>>``, ``<``) as their own tokens rather than
    folding them into neighbouring words. This is what lets the scans below
    stop a value-token search at the right place instead of misreading `git
    config core.hooksPath || echo unset` as a write with value `||`."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    try:
        return list(lexer)
    except ValueError:
        # Unbalanced quote or similar -- fall back to a plain split so the
        # scans below still see *something* rather than raising.
        return command.split()


def _has_config_override_flag(tokens: list[str]) -> bool:
    """Whether a ``-c``/``--config core.hooksPath=...`` override appears
    anywhere in the command. Scans every occurrence, not just the first --
    the override can ride alongside an unrelated ``-c`` in the same
    invocation (`git -c core.hooksPath=x -c other=y config ...`)."""
    n = len(tokens)
    for i, tok in enumerate(tokens):
        if tok in ("-c", "--config") and i + 1 < n:
            if tokens[i + 1].startswith("core.hooksPath="):
                return True
        elif tok.startswith(("--config=core.hooksPath=", "-ccore.hooksPath=")):
            return True
    return False


def _find_hookspath_mutation(tokens: list[str]) -> bool:
    """Whether the tokenized command contains a ``git config`` mutation of
    ``core.hooksPath``.

    Old syntax: ``git config [scope] core.hooksPath [value]`` -- a write iff
    a value token follows before a control operator, or an explicit
    ``--unset``/``--unset-all``/``--replace-all`` flag appears in the same
    invocation. New syntax (git >= 2.46): ``git config set|unset
    core.hooksPath ...`` -- ``set``/``unset`` subcommands are always a
    write; ``get``/``--get`` is always a read. ``--edit``/``-e`` opens an
    interactive editor on the whole file with no key argument to check, so
    it is treated as an unconditional mutation risk.
    """
    n = len(tokens)
    for i, tok in enumerate(tokens):
        if tok != "git":
            continue
        j = i + 1
        config_idx = None
        while j < n and tokens[j] not in _CONTROL_OPERATORS:
            if tokens[j] == "config":
                config_idx = j
                break
            j += 1
        if config_idx is None:
            continue

        k = config_idx + 1
        explicit_write_flag = False
        explicit_subcommand: str | None = None
        saw_hookspath = False
        saw_value_after_hookspath = False
        while k < n and tokens[k] not in _CONTROL_OPERATORS:
            t = tokens[k]
            if t in ("--edit", "-e"):
                return True
            if t in ("--unset", "--unset-all", "--replace-all"):
                explicit_write_flag = True
            elif t in ("--get", "--get-all", "--get-regexp"):
                explicit_subcommand = explicit_subcommand or "get"
            elif t in ("set", "unset", "get") and not saw_hookspath:
                explicit_subcommand = explicit_subcommand or t
            elif t == "core.hooksPath":
                saw_hookspath = True
                if explicit_write_flag or explicit_subcommand in ("set", "unset"):
                    return True
            elif saw_hookspath and not t.startswith("-"):
                saw_value_after_hookspath = True
            k += 1
        if saw_hookspath and saw_value_after_hookspath and explicit_subcommand != "get":
            return True
    return False


def _is_git_config_path(token: str) -> bool:
    """Whether ``token`` names a path whose basename is ``config`` inside a
    ``.git`` directory -- narrow on purpose, see :func:`_writes_git_config_file`."""
    normalized = token.strip("\"'").replace("\\", "/")
    parts = [p for p in normalized.split("/") if p]
    return len(parts) >= 2 and parts[-1] == "config" and parts[-2] == ".git"


def _writes_git_config_file(tokens: list[str]) -> bool:
    """Whether the command redirects, ``sed -i``s, or ``tee``s onto a
    ``.git/config`` path. Deliberately narrow: a bare reference with no
    write syntax (``cat .git/config``, ``grep hooksPath .git/config``) must
    stay allowed -- those are ordinary diagnostic commands. Not exhaustive:
    ``cp``/``mv``/``install``/an inline interpreter writing the file, or a
    wrapper script written elsewhere and executed, are accepted gaps (would
    require executing the shell to resolve, defeating a fast PreToolUse
    check)."""
    n = len(tokens)
    for i, tok in enumerate(tokens):
        if tok in (">", ">>"):
            if i + 1 < n and _is_git_config_path(tokens[i + 1]):
                return True
        elif tok == "sed":
            j = i + 1
            saw_inplace = False
            while j < n and tokens[j] not in _CONTROL_OPERATORS:
                if tokens[j] == "-i" or (
                    tokens[j].startswith("-i") and not tokens[j].startswith("--")
                ):
                    saw_inplace = True
                if saw_inplace and _is_git_config_path(tokens[j]):
                    return True
                j += 1
        elif tok == "tee":
            j = i + 1
            while j < n and tokens[j] not in _CONTROL_OPERATORS:
                if _is_git_config_path(tokens[j]):
                    return True
                j += 1
    return False


def evaluate_bash_override(command: str, cwd: str) -> Verdict:
    """Deny the git-native ways to defeat the no-commit-on-main git hook, on
    a protected branch only -- see the module docstring. Fails open (allow)
    when the branch can't be determined, same posture as every other check
    here: a guard that cannot answer must not block the loop.

    Known, accepted gaps (see the spec this implements): ``git commit -n``
    (the short ``--no-verify`` alias) is not checked -- too generic a
    single-letter flag to deny without real false-positive risk. This
    covers only agent-mediated Bash tool calls, never arbitrary manual
    shell use outside the harness.
    """
    info = repo_info(cwd)
    if info is None or info.branch not in PROTECTED_BRANCHES:
        return Verdict("allow")

    if "core.hooksPath" in command and ("$" in command or "`" in command):
        return Verdict(
            "deny",
            "Referencing core.hooksPath through a shell variable or command "
            "substitution is blocked on a protected branch -- this could "
            "supply an override value a static check can't otherwise see.",
        )

    tokens = _shell_tokens(command)

    if "git" in tokens and "--no-verify" in tokens:
        return Verdict(
            "deny",
            "git --no-verify is blocked on a protected branch -- it would "
            "skip the no-commit-on-main hook entirely.",
        )

    if _has_config_override_flag(tokens):
        return Verdict(
            "deny",
            "git -c/--config core.hooksPath=... is blocked on a protected "
            "branch -- it would override the no-commit-on-main hook for "
            "this invocation.",
        )

    if re.search(r"\bGIT_CONFIG_KEY_\d+\s*=\s*[\"']?core\.hooksPath", command):
        return Verdict(
            "deny",
            "GIT_CONFIG_KEY_N=core.hooksPath is blocked on a protected "
            "branch -- it redirects core.hooksPath via git's positional "
            "config-override env vars.",
        )
    if re.search(r"\bGIT_CONFIG_(GLOBAL|SYSTEM)=", command):
        return Verdict(
            "deny",
            "GIT_CONFIG_GLOBAL=/GIT_CONFIG_SYSTEM= is blocked on a "
            "protected branch -- it redirects which config file git reads, "
            "which can hide core.hooksPath the same way overriding it directly would.",
        )
    if re.search(r"\bGIT_CONFIG_PARAMETERS=", command):
        return Verdict(
            "deny",
            "GIT_CONFIG_PARAMETERS= is blocked on a protected branch -- "
            "it is git's env-var form of -c-style config overrides.",
        )

    if _find_hookspath_mutation(tokens):
        return Verdict(
            "deny",
            "Setting or unsetting core.hooksPath is blocked on a protected "
            "branch -- it would disable the no-commit-on-main hook.",
        )

    if _writes_git_config_file(tokens):
        return Verdict(
            "deny",
            "Writing directly to .git/config is blocked on a protected "
            "branch -- it can rewrite core.hooksPath outside git's own "
            "config-mutation commands.",
        )

    return Verdict("allow")


def evaluate(req: Request) -> Verdict:
    """Apply R2 then R3 to write-family calls, and the bash-family override
    check to Bash calls. Fails open on anything it cannot answer."""
    if os.environ.get("GUARD_RAILS_OFF") == "1":
        return Verdict("allow")
    if req.tool == "bash":
        return evaluate_bash_override(req.command, req.cwd)
    if req.tool != "write":
        return Verdict("allow")

    directory = os.path.dirname(req.path) or req.cwd
    info = repo_info(directory)
    if info is None or info.is_bare:
        return Verdict("allow")

    if info.is_worktree:
        # R3: only a worktree can be behind its own base.
        if _behind_origin_main(directory):
            return Verdict(
                "warn",
                "This worktree's base is behind origin/main. Pull before "
                "continuing, or the work will be built on a stale tree.",
            )
        return Verdict("allow")

    if info.branch not in PROTECTED_BRANCHES:
        return Verdict("allow")

    items = load_in_progress()
    if not items:
        return Verdict("allow")

    slug = _busy_item(info.common_dir, items)
    if slug is None:
        return Verdict("allow")
    return Verdict(
        "deny",
        f"Refusing to write into the main checkout of {info.toplevel} on "
        f"'{info.branch}' while backlog item '{slug}' is in progress there. "
        f"Do this work in a worktree: "
        f"git -C {info.toplevel} worktree add ../<repo>-<slug> -b <slug>",
    )


def parse_payload(harness: str, payload: object) -> Request | None:
    """Normalize a harness's native hook payload. Returns None when the
    payload cannot be understood -- the caller then allows, because a script
    that cannot identify the tool must not deny every tool."""
    if not isinstance(payload, dict):
        return None
    try:
        if harness == "claude":
            args = payload.get("tool_input") or {}
            name = payload.get("tool_name")
            path = args.get("file_path") or args.get("path") or ""
            command = args.get("command") or ""
            cwd = payload.get("cwd") or ""
        elif harness == "agy":
            call = payload.get("toolCall") or {}
            args = call.get("args") or {}
            name = call.get("name")
            path = args.get("TargetFile") or args.get("path") or ""
            command = ""
            cwd = payload.get("cwd") or ""
        elif harness == "copilot":
            name = payload.get("toolName")
            raw = payload.get("toolArgs")
            # Copilot nests its arguments as a JSON *string*.
            args = json.loads(raw) if isinstance(raw, str) else (raw or {})
            path = args.get("path") or args.get("file_path") or ""
            command = ""
            cwd = payload.get("cwd") or ""
        else:
            return None
    except (AttributeError, ValueError):
        return None
    if not isinstance(args, dict) or not isinstance(path, str):
        return None
    family = tool_family(name)
    # Bash-family wiring is Claude Code only (best-effort tier: agy and
    # Copilot get none, regardless of what their own tool names happen to
    # be) -- see the module docstring.
    if family == "bash" and harness != "claude":
        family = ""
    if not family:
        return None
    return Request(tool=family, cwd=cwd, path=path, command=command)


def render(harness: str | None, verdict: Verdict) -> tuple[str, int]:
    """Shape a verdict into the harness's own reply. Always exit 0 -- exit 2
    would also block on Claude Code, but a JSON verdict carries the reason."""
    if harness == "claude":
        block: dict[str, object] = {"hookEventName": "PreToolUse"}
        if verdict.decision == "deny":
            block["permissionDecision"] = "deny"
            block["permissionDecisionReason"] = verdict.reason
        elif verdict.decision == "warn":
            block["additionalContext"] = verdict.reason
        return json.dumps({"hookSpecificOutput": block}), 0
    if harness == "agy":
        out: dict[str, object] = {
            "decision": "deny" if verdict.decision == "deny" else "allow"
        }
        if verdict.reason:
            out["reason"] = verdict.reason
        return json.dumps(out), 0
    if harness == "copilot":
        # Probed 2026-09-01: Copilot honours the EXIT CODE only. A JSON
        # {"decision":"deny"} on exit 0 was ignored and the write went
        # through. It also surfaces neither stdout nor stderr to the agent --
        # the model sees only "Denied by preToolUse hook: hook exited with
        # code 2" -- so the reason is emitted on the chance a later release
        # starts showing it, not because it is read today.
        out = {"decision": "deny" if verdict.decision == "deny" else "allow"}
        if verdict.reason:
            out["reason"] = verdict.reason
        return json.dumps(out), (2 if verdict.decision == "deny" else 0)
    return json.dumps({"decision": verdict.decision, "reason": verdict.reason}), 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="pre-tool guard shared by every harness",
    )
    parser.add_argument(
        "--harness",
        choices=["claude", "agy", "copilot"],
        help="read this harness's payload on stdin and answer in its shape",
    )
    parser.add_argument("--tool", help="tool name, for the neutral form")
    parser.add_argument("--cwd", help="session working directory, for the neutral form")
    parser.add_argument(
        "--path", help="target file path, for the neutral write-family form"
    )
    parser.add_argument(
        "--command", help="shell command, for the neutral bash-family form"
    )
    cli_common.add_verbosity_args(parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.harness:
        try:
            payload = json.loads(sys.stdin.read() or "{}")
        except ValueError:
            payload = None
        req = parse_payload(args.harness, payload) if payload is not None else None
        if req is None:
            out, code = render(args.harness, Verdict("allow"))
            print(out)
            return code
        verdict = evaluate(req)
        cli_common.vprint(
            f"[guard-rails] {verdict.decision}: {verdict.reason}",
            verbose=args.verbose,
        )
        out, code = render(args.harness, verdict)
        if args.harness == "copilot" and verdict.reason:
            print(verdict.reason, file=sys.stderr)
        print(out)
        return code

    req = Request(
        tool=tool_family(args.tool),
        cwd=args.cwd or "",
        path=args.path or "",
        command=args.command or "",
    )
    verdict = evaluate(req)
    out, code = render(None, verdict)
    print(out)
    return code


if __name__ == "__main__":
    sys.exit(main())
