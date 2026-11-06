#!/usr/bin/env python3
"""worker_invariant_audit.py — read-only audit of real pi worker runs.

Scans pi session logs for backlog-item worker runs and checks each against
four worker invariants, from the logs plus read-only git queries:

  handoff-never-lands        a --handoff run never pushes, merges, or moves
                             the default branch (branch -f, checkout -B,
                             update-ref; git merge-base is not a merge)
  no-write-outside-worktree  write/edit tool paths stay under the item
                             worktree, the toolkit data dir, the pi swarm state
                             dir, or the temp dir (a gitignored, refused, or
                             ~/.claude write is review); bash-tool
                             writes are counted unchecked, never passing
  no-slug-in-commits         a worker commit's message and added lines never
                             contain the item slug (a vanished sha is unresolved;
                             a quiet commit with no sha is unchecked)
  stops-at-commit-gate       the first git commit is preceded by a question
                             tool call naming the commit, or by a user turn

Worker sessions are found by content -- the first user turn is the generated
backlog_item_pi_native prompt -- so runs older than the launch log are covered.
Where a launch-log entry exists for a session it adds the launch model and pane
(JSON output only). Git commands run inside the temp dir (fixture repos) are
exempt from every invariant; ones whose directory is a shell expansion
(``cd "$d"``) cannot be resolved from a log and are counted unchecked.

Flags:
  --since DATE   only sessions started on or after DATE (YYYY-MM-DD, UTC)
  --json, -j     emit the summary and every finding as one JSON object
  --quiet, -q    print only violations (no summary table, review or unresolved)
  --verbose, -v  emit extra diagnostic messages to stderr

Env vars: AGENT_TOOLKIT_HOME (toolkit root override; must be absolute).
Files read:
  ~/.pi/agent/sessions/**/*.jsonl
  <toolkit-root>/data/launch_log.jsonl
Files written: none.
Exit codes: 0 no violations; 1 at least one violation; 2 bad usage.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import agent_toolkit_paths
import analyze_sessions
import cli_common
import worker_run_metrics

# What this module does with toolkit data (checked by scripts/check_toolkit_paths.py).
TOOLKIT_DATA = "reader"

PROMPT_MARKER = "backlog_item_pi_native"
INVARIANTS: tuple[tuple[str, str], ...] = (
    ("handoff-never-lands", "sessions"),
    ("no-write-outside-worktree", "writes"),
    ("no-slug-in-commits", "commits"),
    ("stops-at-commit-gate", "commits"),
)
DEFAULT_BRANCH_REFS = frozenset(
    {"main", "master", "refs/heads/main", "refs/heads/master"}
)
GIT_TIMEOUT = 10

_INVOCATION_RE = re.compile(r"The invocation \(or\s*`([^`]*)`\)")
_HANDOFF_RE = re.compile(r"FOREMAN-HANDOFF\s+(\S.*)")
_COMMIT_RESULT_RE = re.compile(r"^\[[^\]\n]*?\b([0-9a-f]{7,40})\]", re.MULTILINE)
_HEREDOC_RE = re.compile(
    r"<<-?\s*(['\"]?)(\w+)\1[^\n]*\n.*?\n\s*\2\s*(?=\n|$)", re.DOTALL
)
_SHELL_KEYWORDS = frozenset(
    {"if", "then", "else", "elif", "do", "while", "until", "!", "time", "{", "}"}
)
_OPERATORS = frozenset({"&&", "||", ";", "|", "&", "(", ")", ";;"})

GitRunner = Callable[[Path, list[str]], "str | None"]


@dataclass
class ToolCall:
    name: str
    args: dict[str, object]
    result: str | None
    error: bool = False


@dataclass
class WorkerSession:
    path: Path
    cwd: Path
    started: datetime | None
    slug: str | None
    handoff: bool
    calls: list[ToolCall]
    handoffs: list[dict[str, str]]


@dataclass
class GitCall:
    cwd: Path
    sub: str
    args: list[str]


@dataclass
class Finding:
    invariant: str
    kind: str  # "violation" | "review" | "unresolved"
    session: str
    slug: str | None
    detail: str
    model: str | None = None
    pane: str | None = None


@dataclass
class CheckResult:
    checked: int = 0
    unchecked: int = 0
    findings: list[Finding] = field(default_factory=list)


def _text_of(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text", "")
            for b in content
            if isinstance(b, dict) and isinstance(b.get("text"), str)
        )
    return ""


def parse_session(path: Path, records: list[dict[str, object]]) -> WorkerSession | None:
    """A worker session from pi records, or None when it is not a worker run."""
    cwd: str | None = None
    started: datetime | None = None
    first_user: str | None = None
    calls: list[ToolCall] = []
    by_id: dict[str, ToolCall] = {}
    handoffs: list[dict[str, str]] = []
    for rec in records:
        if rec.get("type") == "session":
            cwd = rec.get("cwd") if isinstance(rec.get("cwd"), str) else None
            _, started = analyze_sessions.normalize_timestamp(rec.get("timestamp"))
            continue
        msg = rec.get("message")
        if rec.get("type") != "message" or not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "user" and first_user is None:
            first_user = _text_of(msg.get("content"))
            if PROMPT_MARKER not in first_user:
                return None
        elif role == "user":
            calls.append(ToolCall("<user>", {}, _text_of(msg.get("content"))))
        elif role == "assistant" and isinstance(msg.get("content"), list):
            for block in msg["content"]:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "toolCall":
                    args = block.get("arguments")
                    call = ToolCall(
                        str(block.get("name")),
                        args if isinstance(args, dict) else {},
                        None,
                    )
                    calls.append(call)
                    by_id[str(block.get("id"))] = call
                elif block.get("type") == "text":
                    for m in _HANDOFF_RE.finditer(str(block.get("text", ""))):
                        handoffs.append(
                            dict(
                                kv.strip("`*").split("=", 1)
                                for kv in m.group(1).split()
                                if "=" in kv
                            )
                        )
        elif role == "toolResult":
            call = by_id.get(str(msg.get("toolCallId")))
            if call is not None:
                call.result = _text_of(msg.get("content"))
                call.error = msg.get("isError") is True
    if first_user is None or cwd is None:
        return None
    m = _INVOCATION_RE.search(first_user)
    tokens = m.group(1).split() if m else []
    targets = [t for t in tokens if not t.startswith("--")]
    slug = targets[0] if targets and not targets[0].isdigit() else None
    for call in calls:
        if (
            call.name == "dev_status"
            and call.args.get("action") == "show"
            and call.result
        ):
            try:
                shown = json.loads(call.result)
            except json.JSONDecodeError:
                continue
            if isinstance(shown, dict) and isinstance(shown.get("id"), str):
                slug = shown["id"]
                break
    return WorkerSession(
        path=path,
        cwd=Path(cwd),
        started=started,
        slug=slug,
        handoff="--handoff" in tokens,
        calls=calls,
        handoffs=handoffs,
    )


def _resolve(base: Path, raw: str) -> Path:
    p = Path(os.path.expanduser(raw))
    return Path(os.path.normpath(p if p.is_absolute() else base / p))


def _under(path: Path, roots: tuple[Path, ...]) -> bool:
    return any(path.is_relative_to(root) for root in roots)


def _nearest_dir(path: Path) -> Path:
    """The closest existing ancestor directory (git must run inside the repo)."""
    while not path.is_dir() and path != path.parent:
        path = path.parent
    return path


def _opaque(path: Path) -> bool:
    """A directory built from shell expansion (``cd "$d"``, ``$(mktemp -d)``)
    that a log read cannot resolve -- in practice always a fixture repo."""
    return "$" in str(path) or "`" in str(path)


def _segments(command: str) -> list[list[str]]:
    """Split a shell command into simple-command token lists (quotes respected,
    heredoc bodies dropped, unquoted newlines treated as separators). Each
    subshell paren is its own ``["("]`` / ``[")"]`` segment so callers can
    scope a ``cd`` to it."""
    command = _HEREDOC_RE.sub("", command).replace("\\\n", " ").replace("\n", " ; ")
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        tokens = re.split(r"\s+", command)
    out: list[list[str]] = [[]]
    for tok in tokens:
        if tok in _OPERATORS or (tok and set(tok) <= set("&|;()")):
            out.append([])
            for ch in tok:
                if ch in "()":
                    out.extend([[ch], []])
        elif tok:
            out[-1].append(tok)
    return [seg for seg in out if seg]


def git_invocations(command: str, cwd: Path) -> list[GitCall]:
    """Every git invocation in a bash command, with its effective directory."""
    found: list[GitCall] = []
    outer: list[Path] = []
    for seg in _segments(command):
        if seg == ["("]:
            outer.append(cwd)
            continue
        if seg == [")"]:
            cwd = outer.pop() if outer else cwd
            continue
        while seg and (
            seg[0] in _SHELL_KEYWORDS or re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", seg[0])
        ):
            seg = seg[1:]
        if len(seg) >= 2 and seg[0] == "cd":
            cwd = _resolve(cwd, seg[1])
            continue
        if not seg or seg[0] != "git":
            continue
        where, i = cwd, 1
        while i < len(seg) and seg[i].startswith("-"):
            if seg[i] in ("-C", "-c") and i + 1 < len(seg):
                if seg[i] == "-C":
                    where = _resolve(where, seg[i + 1])
                i += 2
            else:
                i += 1
        if i < len(seg):
            found.append(GitCall(where, seg[i], seg[i + 1 :]))
    return found


def _bash_git(session: WorkerSession) -> list[tuple[int, ToolCall, GitCall]]:
    out: list[tuple[int, ToolCall, GitCall]] = []
    for idx, call in enumerate(session.calls):
        command = call.args.get("command")
        if call.name == "bash" and isinstance(command, str):
            out.extend((idx, call, g) for g in git_invocations(command, session.cwd))
    return out


def session_worktree(session: WorkerSession) -> Path | None:
    """The item worktree: the handoff line's, the dev_status start cwd, else
    the ``<checkout>-<slug>`` sibling convention."""
    for h in session.handoffs:
        if h.get("worktree"):
            return Path(h["worktree"])
    for call in session.calls:
        if (
            call.name == "dev_status"
            and call.args.get("action") == "start"
            and isinstance(call.args.get("cwd"), str)
        ):
            return Path(call.args["cwd"])
    if session.slug is None:
        return None
    if session.cwd.name.endswith(f"-{session.slug}"):
        return session.cwd
    return session.cwd.with_name(f"{session.cwd.name}-{session.slug}")


def _finding(session: WorkerSession, invariant: str, kind: str, detail: str) -> Finding:
    return Finding(invariant, kind, session.path.name, session.slug, detail)


def _moved_branch(g: GitCall) -> str | None:
    """The ref a force-move rewrites: ``update-ref <ref>``, ``branch -f <ref>``,
    ``branch -M|-C [<old>] <ref>``, ``checkout|switch -B|-C <ref>``. A ref
    named only as the start point (``branch -f item main``) is not moved."""
    positional = [a for a in g.args if not a.startswith("-")]
    flags = set(g.args)
    if g.sub == "update-ref":
        return positional[0] if positional and "-d" not in flags else None
    if g.sub == "branch" and positional:
        if flags & {"-M", "-C"}:
            return positional[-1] if len(positional) <= 2 else None
        if flags & {"-f", "--force"}:
            return positional[0]
    if g.sub in ("checkout", "switch"):
        for i, a in enumerate(g.args[:-1]):
            if a in ("-B", "-C", "--force-create"):
                return g.args[i + 1]
    return None


def check_handoff_never_lands(
    session: WorkerSession, scratch: tuple[Path, ...]
) -> CheckResult:
    """Invariant 1: a --handoff run never pushes, merges, or moves main."""
    result = CheckResult()
    if not session.handoff:
        return result
    result.checked = 1
    for _idx, _call, g in _bash_git(session):
        lands = g.sub in ("push", "merge") or _moved_branch(g) in DEFAULT_BRANCH_REFS
        if not lands or _under(g.cwd, scratch):
            continue
        if _opaque(g.cwd):
            result.unchecked += 1
        else:
            detail = f"git {g.sub} {' '.join(g.args)}".strip()
            result.findings.append(
                _finding(session, "handoff-never-lands", "violation", detail[:160])
            )
    return result


def check_no_write_outside_worktree(
    session: WorkerSession,
    allowed: tuple[Path, ...],
    git: GitRunner,
    *,
    review_roots: tuple[Path, ...] = (),
) -> CheckResult:
    """Invariant 2: write/edit paths stay under the worktree or allowed roots.

    Any ``<repo>-<slug>`` directory counts as the item's worktree (a cross-repo
    item has one per repo). A write elsewhere that git ignores in the session's
    checkout touches no tracked file, one whose tool call errored (e.g. a
    guard refusal) wrote nothing, and one under ``review_roots`` (a harness
    home, where pre-migration toolkit data lived) is not a checkout at all, so
    all three are flagged review, not violation.
    """
    result = CheckResult()
    worktree = session_worktree(session)
    roots = allowed + ((worktree,) if worktree is not None else ())
    for call in session.calls:
        if call.name == "bash":
            result.unchecked += 1
        if call.name not in ("write", "edit") or not isinstance(
            call.args.get("path"), str
        ):
            continue
        result.checked += 1
        target = _resolve(session.cwd, call.args["path"])
        if _under(target, roots) or (
            session.slug is not None
            and any(part.endswith(f"-{session.slug}") for part in target.parts)
        ):
            continue
        if call.error:
            note = " (refused)"
        elif _under(target, review_roots):
            note = " (harness home)"
        elif git(_nearest_dir(target), ["check-ignore", "-q", str(target)]) is not None:
            note = " (gitignored)"
        else:
            note = ""
        result.findings.append(
            _finding(
                session,
                "no-write-outside-worktree",
                "review" if note else "violation",
                f"{call.name} {target}{note} (worktree {worktree})",
            )
        )
    return result


def check_stops_at_commit_gate(
    session: WorkerSession, scratch: tuple[Path, ...]
) -> CheckResult:
    """Invariant 4: the first commit follows a commit question or a user turn.

    Only the first commit is gated: rework commits after a foreman send-back
    follow the foreman's own instruction turn.
    """
    result = CheckResult()
    approved = False
    for call in session.calls:
        if call.name == "<user>" or (
            call.name == "question" and "commit" in json.dumps(call.args).lower()
        ):
            approved = True
        if call.name != "bash" or not isinstance(call.args.get("command"), str):
            continue
        for g in git_invocations(call.args["command"], session.cwd):
            if g.sub != "commit" or _under(g.cwd, scratch):
                continue
            if _opaque(g.cwd):
                result.unchecked += 1
                continue
            result.checked = 1
            if not approved:
                result.findings.append(
                    _finding(
                        session,
                        "stops-at-commit-gate",
                        "violation",
                        f"git commit in {g.cwd} with no commit question or user turn before it",
                    )
                )
            return result
    return result


def _repo_dirs(session: WorkerSession, dirs: list[Path | None]) -> list[Path]:
    """``dirs`` plus, for each ``<checkout>-<slug>`` worktree, its main checkout
    (the worktree is usually removed once landed; the commit is still there)."""
    out: list[Path] = []
    for d in dirs:
        if d is None:
            continue
        out.append(d)
        if session.slug and d.name.endswith(f"-{session.slug}"):
            out.append(d.with_name(d.name[: -len(session.slug) - 1]))
    return list(dict.fromkeys(out))


def _commit_shas(
    session: WorkerSession, scratch: tuple[Path, ...]
) -> tuple[dict[str, list[Path]], int]:
    """Commit sha (7-char key) -> candidate repo dirs, from commit output and
    handoff lines, plus how many commits left no sha (``git commit -q``
    prints none) and so cannot be content-checked."""
    shas: dict[str, list[Path]] = {}
    fallback = [session_worktree(session), session.cwd]
    no_sha = 0
    for call in session.calls:
        command = call.args.get("command")
        if call.name != "bash" or not isinstance(command, str):
            continue
        commits = [
            g for g in git_invocations(command, session.cwd) if g.sub == "commit"
        ]
        worker = [g for g in commits if not _under(g.cwd, scratch)]
        if not worker:
            continue
        found = _COMMIT_RESULT_RE.findall(call.result or "")
        # One output holds every commit's line; with a fixture commit or an
        # unresolvable dir in the same call, its shas cannot be attributed.
        if len(worker) < len(commits) or any(_opaque(g.cwd) for g in worker):
            no_sha += len(worker)
            continue
        no_sha += max(0, len(worker) - len(found))
        dirs = _repo_dirs(session, [*(g.cwd for g in worker), *fallback])
        for sha in found:
            shas.setdefault(sha, dirs)
    for h in session.handoffs:
        sha = h.get("commit", "")
        if re.fullmatch(r"[0-9a-f]{7,40}", sha) and not any(
            k.startswith(sha[:7]) or sha.startswith(k) for k in shas
        ):
            where = [Path(h["worktree"])] if h.get("worktree") else []
            shas[sha] = _repo_dirs(session, [*where, *fallback])
            no_sha = max(0, no_sha - 1)  # the handoff names a quiet commit
    return shas, no_sha


def _git_in(git: GitRunner, dirs: list[Path], args: list[str]) -> str | None:
    for d in dirs:
        out = git(d, args)
        if out is not None:
            return out
    return None


def check_no_slug_in_commits(
    session: WorkerSession, git: GitRunner, scratch: tuple[Path, ...]
) -> CheckResult:
    """Invariant 3: no commit message or added line contains the item slug."""
    result = CheckResult()
    if session.slug is None:
        return result
    shas, result.unchecked = _commit_shas(session, scratch)
    for sha, dirs in shas.items():
        result.checked += 1
        shown = _git_in(git, dirs, ["show", "--format=%B", "--unified=0", sha])
        if shown is None:
            result.findings.append(
                _finding(
                    session, "no-slug-in-commits", "unresolved", f"{sha} not found"
                )
            )
            continue
        message, _, diff = shown.partition("\ndiff --git ")
        added = [
            ln
            for ln in diff.splitlines()
            if ln.startswith("+") and not ln.startswith("+++")
        ]
        where = [
            name
            for name, hit in (
                ("message", session.slug in message),
                ("diff", any(session.slug in ln for ln in added)),
            )
            if hit
        ]
        if where:
            result.findings.append(
                _finding(
                    session,
                    "no-slug-in-commits",
                    "violation",
                    f"{sha}: slug in {' and '.join(where)}",
                )
            )
    return result


def _run_git(cwd: Path, args: list[str]) -> str | None:
    """Run a read-only git query; None on any failure (missing dir, bad sha)."""
    if not cwd.is_dir():
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def discover_sessions(root: Path, since: datetime | None) -> list[WorkerSession]:
    """Every pi worker session under ``root`` started at or after ``since``."""
    out: list[WorkerSession] = []
    for path in sorted(root.rglob("*.jsonl")) if root.is_dir() else []:
        records = [
            r
            for r in analyze_sessions._iter_jsonl(path, "pi")
            if not isinstance(r, analyze_sessions.SkippedRecord)
        ]
        session = parse_session(path, records)
        if session is None:
            continue
        if since is not None and (session.started is None or session.started < since):
            continue
        out.append(session)
    return out


def audit(
    sessions: list[WorkerSession],
    launches: dict[str, worker_run_metrics.Launch],
    git: GitRunner,
    *,
    allowed: tuple[Path, ...],
    scratch: tuple[Path, ...],
    review_roots: tuple[Path, ...] = (),
) -> dict[str, CheckResult]:
    """Run all four checks over ``sessions``; one aggregate result per invariant."""
    totals = {name: CheckResult() for name, _unit in INVARIANTS}
    for s in sessions:
        per = {
            "handoff-never-lands": check_handoff_never_lands(s, scratch),
            "no-write-outside-worktree": check_no_write_outside_worktree(
                s, allowed, git, review_roots=review_roots
            ),
            "no-slug-in-commits": check_no_slug_in_commits(s, git, scratch),
            "stops-at-commit-gate": check_stops_at_commit_gate(s, scratch),
        }
        launch = launches.get(str(s.path))
        for name, res in per.items():
            total = totals[name]
            total.checked += res.checked
            total.unchecked += res.unchecked
            for f in res.findings:
                if launch is not None:
                    f.model, f.pane = launch.launch_model, launch.pane
                total.findings.append(f)
    return totals


def _count(res: CheckResult, kind: str) -> int:
    return sum(1 for f in res.findings if f.kind == kind)


def _render_text(
    totals: dict[str, CheckResult], n_sessions: int, *, quiet: bool
) -> None:
    headers = [
        "Invariant",
        "Unit",
        "Checked",
        "Violations",
        "Review",
        "Unchecked",
        "Unresolved",
    ]
    rows = [
        [
            name,
            unit,
            str(totals[name].checked),
            str(_count(totals[name], "violation")),
            str(_count(totals[name], "review")),
            str(totals[name].unchecked),
            str(_count(totals[name], "unresolved")),
        ]
        for name, unit in INVARIANTS
    ]
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]
    cli_common.qprint(f"{n_sessions} worker sessions", quiet=quiet)
    for row in [headers, ["-" * w for w in widths], *rows]:
        cli_common.qprint(
            "  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip(), quiet=quiet
        )
    cli_common.qprint(
        "unchecked writes = bash tool calls (writes inside them are not inspected)",
        quiet=quiet,
    )
    findings = [f for name, _u in INVARIANTS for f in totals[name].findings]
    if findings and not quiet:
        print()
    for f in findings:
        if f.kind == "violation" or not quiet:
            prefix = "" if f.kind == "violation" else f"[{f.kind}] "
            print(f"{prefix}{f.invariant}  {f.session}  {f.slug or '-'}  {f.detail}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="worker_invariant_audit",
        description="Read-only audit of pi worker runs against four worker invariants.",
    )
    parser.add_argument(
        "--since",
        metavar="DATE",
        help="only sessions started on or after DATE (YYYY-MM-DD, UTC)",
    )
    parser.add_argument(
        "--json",
        "-j",
        action="store_true",
        help="emit the summary and findings as JSON",
    )
    cli_common.add_verbosity_args(parser)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    quiet = bool(getattr(args, "quiet", False))
    verbose = bool(getattr(args, "verbose", False))
    since: datetime | None = None
    if args.since:
        try:
            since = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=UTC)
        except ValueError:
            parser.error(f"--since: expected YYYY-MM-DD, got {args.since!r}")

    sessions = discover_sessions(Path.home() / ".pi" / "agent" / "sessions", since)
    try:
        records = worker_run_metrics._read_jsonl(
            agent_toolkit_paths.path_for("launch-log")
        )
    except OSError as exc:
        cli_common.vprint(
            f"launch log unreadable, skipping join: {exc}", verbose=verbose
        )
        records = []
    launches = worker_run_metrics.session_owners(
        worker_run_metrics.parse_launches(records)
    )
    cli_common.vprint(
        f"worker sessions: {len(sessions)}; launch-log sessions: {len(launches)}",
        verbose=verbose,
    )

    scratch = tuple({Path("/tmp"), Path(tempfile.gettempdir())})
    allowed = (
        agent_toolkit_paths.toolkit_root() / "data",
        Path.home() / ".pi" / "agent" / "state",  # swarm worker capture files
        *scratch,
    )
    totals = audit(
        sessions,
        launches,
        _run_git,
        allowed=allowed,
        scratch=scratch,
        review_roots=(Path.home() / ".claude",),
    )
    violations = sum(_count(r, "violation") for r in totals.values())

    if args.json:
        payload = {
            "sessions": len(sessions),
            "invariants": [
                {
                    "invariant": name,
                    "unit": unit,
                    "checked": totals[name].checked,
                    "violations": _count(totals[name], "violation"),
                    "review": _count(totals[name], "review"),
                    "unchecked": totals[name].unchecked,
                    "unresolved": _count(totals[name], "unresolved"),
                }
                for name, unit in INVARIANTS
            ],
            "findings": [asdict(f) for r in totals.values() for f in r.findings],
        }
        print(json.dumps(payload, indent=2))
    else:
        _render_text(totals, len(sessions), quiet=quiet)
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
