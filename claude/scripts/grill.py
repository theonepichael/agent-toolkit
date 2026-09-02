#!/usr/bin/env python3
"""grill.py — grill-me session state CLI. All session mutations go through here.

The JSON session file is the capture mechanism: every decision point is recorded
the moment it's identified (open), then resolved (decided), then verified. An
unfinished session keeps its open questions, so it can resume in a later
conversation. The plan document itself is authored by the model as a separate
markdown artifact, informed by these decision points; the session stores only a
pointer to it (plan_path). `render` prints session status, not the plan.

Flags
  --quiet, -q    suppress non-essential output
  --verbose, -v  emit extra diagnostic messages to stderr

Requires Python 3.12+.
"""

import argparse
import fcntl
import json
import os
import re
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from datetime import date, datetime
from pathlib import Path
from typing import NoReturn, NotRequired, TypedDict, cast

import cli_common

DATA_DIR = Path.home() / ".claude" / "data" / "grill"
SCHEMA_VERSION = 1

VALID_SOURCES = {"user", "defaulted", "assumed", "tested"}
VALID_RESULTS = {"VERIFIED", "DISPUTED", "UNVERIFIABLE"}
EVIDENCE_REQUIRED = {"VERIFIED", "DISPUTED"}
REVISABLE_FIELDS = {"question", "decision", "reasoning", "source", "depends_on"}
ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
ID_MIN, ID_MAX = 2, 48


# ── data model ───────────────────────────────────────────────────────────────


class Verdict(TypedDict):
    """A recorded verification result for one decision."""

    result: str
    evidence: str
    date: str


class Decision(TypedDict):
    """One decision point within a grill session.

    ``decision`` and ``source`` are ``None`` while the point is still open
    (see :func:`is_open`). ``verdict`` is ``None`` until :func:`cmd_verdict`
    records one, and is reset to ``None`` whenever :func:`cmd_revise`
    changes the underlying text.
    """

    id: str
    question: str
    reasoning: str
    decision: str | None
    source: str | None
    verdict: Verdict | None
    depends_on: NotRequired[list[str]]


class Session(TypedDict):
    """A grill session as stored at ``DATA_DIR/<slug>.json``."""

    schema_version: int
    slug: str
    topic: str
    created: str
    updated: str
    plan_path: str | None
    pending_execution: bool
    backlog_slug: str | None
    decisions: list[Decision]


type DecisionList = list[Decision]


# ── helpers ───────────────────────────────────────────────────────────────────


def today() -> str:
    """Return today's date as an ISO-8601 string (``YYYY-MM-DD``)."""
    return date.today().isoformat()


def now() -> str:
    """Return the current local time as a full ISO-8601 timestamp.

    A full timestamp (not just a date) so same-day sessions never tie in
    :func:`_resolve_slug`'s latest-when-omitted rule. Date-only values in
    old files still sort correctly against these (prefix ordering), so no
    migration is needed.
    """
    return datetime.now().isoformat()


def die(context: str, msg: str) -> NoReturn:
    """Print an error to stderr and exit the process with status 1.

    Args:
        context: Command name to prefix onto the message.
        msg: The error message.
    """
    print(f"[{context}] {msg}", file=sys.stderr)
    sys.exit(1)


def slugify(text: str) -> str:
    """Lowercase ``text`` and collapse runs of non-alphanumerics to single hyphens."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def validate_decision_id(decision_id: str, context: str) -> None:
    """Validate a decision id's format and length.

    Args:
        decision_id: The candidate id.
        context: Command name to prefix onto any error message.

    Raises:
        SystemExit: If ``decision_id`` isn't lowercase kebab-case or is
            outside ``[ID_MIN, ID_MAX]`` characters.
    """
    if not ID_RE.match(decision_id):
        die(context, f"invalid decision id '{decision_id}' — lowercase kebab-case")
    if not (ID_MIN <= len(decision_id) <= ID_MAX):
        die(
            context,
            f"decision id '{decision_id}' length {len(decision_id)} "
            f"out of range [{ID_MIN},{ID_MAX}]",
        )


def parse_json_arg(raw: str, context: str) -> dict[str, object]:
    """Parse a CLI argument as a JSON object.

    Args:
        raw: The raw argument text.
        context: Command name to prefix onto any error message.

    Returns:
        The decoded JSON object.

    Raises:
        SystemExit: If ``raw`` isn't valid JSON, or decodes to something
            other than a JSON object. Exits with status 1 after printing
            to stderr.
    """
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        die(context, f"invalid JSON: {e}")
    if not isinstance(parsed, dict):
        die(context, "expected a JSON object")
    return cast(dict[str, object], parsed)


def _text(patch: dict[str, object], key: str, default: str = "") -> str:
    """Extract a string field from a JSON patch.

    Treats a missing key and an explicit JSON ``null`` identically, both
    collapsing to ``default``. Without this, ``str(None)`` would silently
    turn an explicit ``null`` into the four-character string ``"None"``,
    which then passes any truthiness check meant to catch a missing
    required field (and, rendered back out, reads as a real value).

    Args:
        patch: The decoded JSON patch.
        key: The field to extract.
        default: Value to use when the field is missing or ``null``.
    """
    value = patch.get(key)
    if value is None:
        return default
    return str(value)


# ── I/O ───────────────────────────────────────────────────────────────────────


def session_path(slug: str) -> Path:
    """Return the on-disk path for the session identified by ``slug``."""
    return DATA_DIR / f"{slug}.json"


def load_session(slug: str) -> Session:
    """Load one session by slug.

    Args:
        slug: The session's slug.

    Returns:
        The decoded session.

    Raises:
        SystemExit: If the file is missing, contains invalid JSON, isn't a
            JSON object, or has an unrecognized schema version. Exits with
            status 1 after printing a diagnostic to stderr.
    """
    path = session_path(slug)
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        print(
            f"session file corrupted at {path}; fix or restore from backup. ({e})",
            file=sys.stderr,
        )
        sys.exit(1)
    if not isinstance(data, dict):
        print(
            f"session file at {path} is not a JSON object (found {type(data).__name__}); "
            "check file or remove it from the sessions directory.",
            file=sys.stderr,
        )
        sys.exit(1)
    if data.get("schema_version") != SCHEMA_VERSION:
        print(
            f"session file at {path} is not schema_version {SCHEMA_VERSION}; "
            "check file or run migration.",
            file=sys.stderr,
        )
        sys.exit(1)
    return cast(Session, data)


def ensure_data_dir() -> None:
    """Create ``DATA_DIR`` if it is missing.

    Called once per invocation, before any subcommand runs, so the directory
    is present even for read-only commands. It is shared artifact storage:
    agents write plan and spec ``.md`` files there with their own file tools,
    not through this script, and used to run ``mkdir -p`` defensively first.
    Guaranteeing it here is what lets the skill docs drop that step.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def save_session(session: Session) -> None:
    """Atomically persist ``session`` to its slug-derived path."""
    ensure_data_dir()
    payload = json.dumps(session, indent=2)
    fd, tmp_path = tempfile.mkstemp(dir=DATA_DIR, prefix=".session_tmp_")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(payload)
        os.replace(tmp_path, session_path(session["slug"]))
    except Exception:
        with suppress(OSError):
            os.unlink(tmp_path)
        raise


def all_session_slugs() -> list[str]:
    """Return every session slug on disk, sorted, or ``[]`` if none exist."""
    if not DATA_DIR.exists():
        return []
    return sorted(p.stem for p in DATA_DIR.glob("*.json"))


@contextmanager
def _flock(path: Path) -> Iterator[None]:
    """Hold an exclusive advisory lock on ``path`` for the block's duration."""
    ensure_data_dir()
    with open(path, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


@contextmanager
def session_lock(slug: str) -> Iterator[None]:
    """Hold an exclusive lock over one session's read-modify-write cycle.

    Serializes concurrent mutators of the same session file — e.g. two
    agents in different terminals racing a ``decide`` and a ``verdict``
    against the same slug — so a read-modify-write cycle can't interleave
    and silently lose one side's write. Every mutating subcommand
    (``ask``/``decide``/``revise``/``rm``/``verdict``/``plan``) resolves
    its target slug, acquires this lock, then reloads the session fresh
    before mutating — never operating on a copy read before the lock was
    held.
    """
    with _flock(DATA_DIR / f".{slug}.lock"):
        yield


@contextmanager
def _new_session_lock() -> Iterator[None]:
    """Hold an exclusive lock over `cmd_new`'s free-slug scan and create.

    Without this, two concurrent ``new`` calls that both compute the same
    free slug would race: the second `save_session` would silently
    overwrite the first session file instead of picking a different slug.
    """
    with _flock(DATA_DIR / "._new_session.lock"):
        yield


def _resolve_slug(arg: str | None, context: str) -> str:
    """Resolve ``--session`` to a slug: exact match, unique substring, or latest.

    Args:
        arg: The ``--session`` value (exact slug or substring), or ``None``
            to resolve to the most recently updated session.
        context: Command name to prefix onto any error message.

    Returns:
        The resolved slug.

    Raises:
        SystemExit: If no sessions exist, ``arg`` matches none, or ``arg``
            matches more than one slug ambiguously.
    """
    slugs = all_session_slugs()
    if not slugs:
        die(context, "no grill sessions exist — create one with 'new'")

    if arg is None:
        sessions = [load_session(s) for s in slugs]
        latest = max(sessions, key=lambda s: (s["updated"], s["slug"]))
        return latest["slug"]

    if arg in slugs:
        return arg

    matches = [s for s in slugs if arg in s]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        die(context, f"no session matches '{arg}' — have: {', '.join(slugs)}")
    die(context, f"'{arg}' is ambiguous: {', '.join(matches)}")


def resolve_session(arg: str | None, context: str) -> Session:
    """Resolve ``--session`` (see :func:`_resolve_slug`) and load it."""
    return load_session(_resolve_slug(arg, context))


def find_decision(session: Session, decision_id: str, context: str) -> Decision:
    """Find a decision by id within a session.

    Args:
        session: The session to search.
        decision_id: The decision's id.
        context: Command name to prefix onto any error message.

    Returns:
        The matching decision.

    Raises:
        SystemExit: If no decision with that id exists in the session.
    """
    for decision in session["decisions"]:
        if decision["id"] == decision_id:
            return decision
    die(context, f"no decision '{decision_id}' in session {session['slug']}")


def is_open(decision: Decision) -> bool:
    """Return whether ``decision`` has not yet been decided."""
    return decision.get("decision") is None


def _validate_depends_on(
    session: Session, owner_id: str, raw: object, context: str
) -> list[str]:
    """Validate and normalize a ``depends_on`` array from a JSON patch.

    Type-checks (must be a list of strings), dedupes (order-preserving),
    rejects self-reference, format-validates each id, and rejects any id
    not already present as a decision in ``session``. All-or-nothing: exits
    via :func:`die` before returning on any failure, so the caller must
    finish this call before mutating ``session["decisions"]``.

    Args:
        session: The session to check referenced ids against.
        owner_id: The id of the decision ``raw`` will be attached to.
        raw: The decoded ``depends_on`` value from a JSON patch.
        context: Command name to prefix onto any error message.
    """
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        die(context, "'depends_on' must be a list of strings")
    raw_ids = cast(list[str], raw)

    deduped: list[str] = []
    for dep_id in raw_ids:
        if dep_id not in deduped:
            deduped.append(dep_id)

    if owner_id in deduped:
        die(context, f"'depends_on' cannot reference its own id '{owner_id}'")

    for dep_id in deduped:
        validate_decision_id(dep_id, context)

    existing_ids = {d["id"] for d in session["decisions"]}
    bad = [dep_id for dep_id in deduped if dep_id not in existing_ids]
    if bad:
        die(
            context,
            f"'depends_on' references unknown decision id(s): {', '.join(bad)}",
        )

    return deduped


def _would_cycle(session: Session, owner_id: str, new_depends_on: list[str]) -> bool:
    """Return whether setting ``owner_id``'s ``depends_on`` to ``new_depends_on``
    would introduce a cycle.

    DFS from each id in ``new_depends_on``, following each visited
    decision's already-stored ``depends_on`` edges, checking whether
    ``owner_id`` is reachable. Keeps a ``seen`` set of visited nodes so a
    cycle elsewhere in the graph — reachable during the walk but not
    involving ``owner_id`` — terminates the walk instead of looping
    forever. A dangling id (absent from the session) has no outgoing edges
    and is simply a dead end.
    """
    by_id = {d["id"]: d for d in session["decisions"]}
    seen: set[str] = set()
    stack = list(new_depends_on)
    while stack:
        current = stack.pop()
        if current == owner_id:
            return True
        if current in seen:
            continue
        seen.add(current)
        dep = by_id.get(current)
        if dep is not None:
            stack.extend(dep.get("depends_on", []))
    return False


def confirm(context: str, session: Session, detail: str, verbose: bool = False) -> None:
    """Echo a mutating command's outcome to stderr."""
    cli_common.vprint(f"[{context}] {session['slug']}: {detail}", verbose=verbose)


def touch(session: Session) -> None:
    """Stamp ``session['updated']`` with the current timestamp, in place."""
    session["updated"] = now()


def frontier(session: Session) -> DecisionList:
    """Return every open decision whose dependencies are all resolved.

    A dependency is resolved when it points to a decided decision, or to an
    id absent from the session entirely (dangling — e.g. from
    ``rm --force``). Preserves the order decisions already appear in
    ``session["decisions"]``.

    This direct-only check (no explicit multi-hop graph walk) is
    transitively correct by construction: :func:`is_open` already reflects
    a decision's full resolved state, so a decision two hops back stays
    correctly gating until every hop between it and the one being checked
    is itself decided.
    """
    by_id = {d["id"]: d for d in session["decisions"]}
    result: DecisionList = []
    for d in session["decisions"]:
        if not is_open(d):
            continue
        blocked = any(
            (dep := by_id.get(dep_id)) is not None and is_open(dep)
            for dep_id in d.get("depends_on", [])
        )
        if not blocked:
            result.append(d)
    return result


def _dangling_deps(decision: Decision, session: Session) -> list[str]:
    """Return which of ``decision``'s ``depends_on`` ids are absent from ``session``."""
    existing_ids = {d["id"] for d in session["decisions"]}
    return [
        dep_id
        for dep_id in decision.get("depends_on", [])
        if dep_id not in existing_ids
    ]


# ── render ────────────────────────────────────────────────────────────────────


def _cell(text: str) -> str:
    """Escape ``text`` for safe embedding in a Markdown table cell."""
    return text.replace("|", "\\|").replace("\n", " ")


def render_markdown(session: Session) -> str:
    """Render a session's status as a Markdown document.

    Includes a summary line, any open questions, a decision table, and
    verification evidence for decisions that have a recorded verdict.
    """
    decisions = session["decisions"]
    open_qs = [d for d in decisions if is_open(d)]
    decided = [d for d in decisions if not is_open(d)]
    verdicts = [d for d in decided if d.get("verdict")]

    plan_path = session.get("plan_path")
    lines = [
        f"# Grill status: {session['topic']}",
        "",
        (
            f"_{session['created'][:10]} · updated {session['updated'][:10]} · "
            f"{len(decided)}/{len(decisions)} decided · {len(verdicts)} verified_"
        ),
        "",
        f"_plan: {plan_path}_" if plan_path else "_plan: not written yet_",
    ]

    if open_qs:
        lines += ["", "## Open questions", ""]
        for d in open_qs:
            lines.append(f"- **{d['id']}** — {d['question']}")

    if decided:
        lines += [
            "",
            "## Decisions",
            "",
            "| Decision | What we decided | Source | Verified |",
            "|----------|-----------------|--------|----------|",
        ]
        for d in decided:
            verdict = d.get("verdict")
            result = verdict["result"] if verdict else ""
            lines.append(
                f"| {_cell(d['id'])} | {_cell(str(d['decision']))} "
                f"| {d['source']} | {result} |"
            )

    if verdicts:
        lines += ["", "## Verification evidence", ""]
        for d in verdicts:
            v = d["verdict"]
            assert v is not None  # filtered into `verdicts` above
            lines.append(
                f"- **{d['id']}** — {v['result']} ({v['date']}): {v['evidence']}"
            )

    return "\n".join(lines) + "\n"


# ── subcommand handlers ───────────────────────────────────────────────────────


def cmd_new(args: argparse.Namespace) -> None:
    """Handle ``new``: create a session and print its slug on stdout."""
    patch = parse_json_arg(args.json, "new")
    topic = _text(patch, "topic").strip()
    if not topic:
        die("new", "'topic' is required")

    base = (
        _text(patch, "slug").strip() or f"{today()}-{slugify(topic)[:32].rstrip('-')}"
    )
    if not ID_RE.match(base):
        die("new", f"invalid slug '{base}' — lowercase kebab-case")

    with _new_session_lock():
        slug = base
        existing = set(all_session_slugs())
        for n in range(2, 100):
            if slug not in existing:
                break
            slug = f"{base}-{n}"
        else:
            die("new", f"could not find a free slug for '{base}'")

        session: Session = {
            "schema_version": SCHEMA_VERSION,
            "slug": slug,
            "topic": topic,
            "created": now(),
            "updated": now(),
            "plan_path": None,
            "pending_execution": False,
            "backlog_slug": None,
            "decisions": [],
        }
        save_session(session)
    confirm("new", session, topic, verbose=getattr(args, "verbose", False))
    print(slug)


def cmd_ask(args: argparse.Namespace) -> None:
    """Handle ``ask``: register an open decision point."""
    slug = _resolve_slug(args.session, "ask")
    patch = parse_json_arg(args.json, "ask")

    decision_id = _text(patch, "id").strip()
    if not decision_id:
        die("ask", "'id' is required")
    validate_decision_id(decision_id, "ask")

    question = _text(patch, "question").strip()
    if not question:
        die("ask", "'question' is required")

    reasoning = _text(patch, "reasoning").strip()

    with session_lock(slug):
        session = load_session(slug)
        decisions = session["decisions"]
        if any(d["id"] == decision_id for d in decisions):
            die("ask", f"duplicate decision id: {decision_id}")

        depends_on: list[str] = []
        if "depends_on" in patch:
            depends_on = _validate_depends_on(
                session, decision_id, patch["depends_on"], "ask"
            )

        decisions.append(
            {
                "id": decision_id,
                "question": question,
                "reasoning": reasoning,
                "decision": None,
                "source": None,
                "verdict": None,
                "depends_on": depends_on,
            }
        )
        touch(session)
        save_session(session)
    confirm(
        "ask",
        session,
        f"? {decision_id} — {question[:60]}",
        verbose=getattr(args, "verbose", False),
    )


def cmd_decide(args: argparse.Namespace) -> None:
    """Handle ``decide``: resolve an open decision point, or add+decide in one shot."""
    slug = _resolve_slug(args.session, "decide")
    patch = parse_json_arg(args.json, "decide")

    decision_id = _text(patch, "id").strip()
    if not decision_id:
        die("decide", "'id' is required")
    validate_decision_id(decision_id, "decide")

    question = _text(patch, "question").strip()
    decision_text = _text(patch, "decision").strip()
    if not decision_text:
        die("decide", "'decision' is required")

    source = _text(patch, "source", "user")
    if source not in VALID_SOURCES:
        die(
            "decide",
            f"invalid source '{source}' — one of: {', '.join(sorted(VALID_SOURCES))}",
        )

    reasoning_given = "reasoning" in patch
    reasoning = _text(patch, "reasoning").strip()
    depends_on_given = "depends_on" in patch

    with session_lock(slug):
        session = load_session(slug)
        decisions = session["decisions"]
        existing = next((d for d in decisions if d["id"] == decision_id), None)

        if existing is not None:
            if not is_open(existing):
                die("decide", f"'{decision_id}' is already decided — use revise")
            if depends_on_given:
                validated = _validate_depends_on(
                    session, decision_id, patch["depends_on"], "decide"
                )
                if _would_cycle(session, decision_id, validated):
                    die(
                        "decide",
                        "'depends_on' would introduce a cycle involving "
                        f"'{decision_id}'",
                    )
                existing["depends_on"] = validated
            existing["decision"] = decision_text
            existing["source"] = source
            if question:
                existing["question"] = question
            if reasoning_given:
                existing["reasoning"] = reasoning
        else:
            if not question:
                die(
                    "decide",
                    "'question' is required for a decision point not registered via ask",
                )
            depends_on: list[str] = []
            if depends_on_given:
                depends_on = _validate_depends_on(
                    session, decision_id, patch["depends_on"], "decide"
                )
            decisions.append(
                {
                    "id": decision_id,
                    "question": question,
                    "reasoning": reasoning,
                    "decision": decision_text,
                    "source": source,
                    "verdict": None,
                    "depends_on": depends_on,
                }
            )

        touch(session)
        save_session(session)
    confirm(
        "decide",
        session,
        f"{decision_id} ({source}) — {decision_text[:60]}",
        verbose=getattr(args, "verbose", False),
    )


def cmd_revise(args: argparse.Namespace) -> None:
    """Handle ``revise``: amend a decision's text (resets its verdict).

    Rejects blanking out ``question``/``decision`` via revise (whether by
    explicit ``null`` or an empty string) — that would silently walk an
    already-decided item back toward "open" without going through
    :func:`cmd_decide`'s own validation, and without :func:`is_open`
    (which only checks for ``None``) ever flagging it.
    """
    slug = _resolve_slug(args.session, "revise")
    patch = parse_json_arg(args.patch, "revise")

    bad = set(patch) - REVISABLE_FIELDS
    if bad:
        die("revise", f"cannot revise field(s): {', '.join(sorted(bad))}")

    normalized: dict[str, str] = {}
    for field in ("question", "decision", "reasoning"):
        if field in patch:
            text = _text(patch, field)
            if field in ("question", "decision") and not text.strip():
                die("revise", f"'{field}' cannot be blank")
            normalized[field] = text
    if "source" in patch:
        source = _text(patch, "source")
        if source not in VALID_SOURCES:
            die("revise", f"invalid source '{source}'")
        normalized["source"] = source

    with session_lock(slug):
        session = load_session(slug)
        decision = find_decision(session, args.decision_id, "revise")
        if is_open(decision) and ({"decision", "source"} & set(normalized)):
            die(
                "revise", f"'{args.decision_id}' is still open — resolve it with decide"
            )

        new_depends_on: list[str] | None = None
        if "depends_on" in patch:
            new_depends_on = _validate_depends_on(
                session, args.decision_id, patch["depends_on"], "revise"
            )
            if _would_cycle(session, args.decision_id, new_depends_on):
                die(
                    "revise",
                    "'depends_on' would introduce a cycle involving "
                    f"'{args.decision_id}'",
                )

        cast(dict[str, object], decision).update(normalized)
        if new_depends_on is not None:
            decision["depends_on"] = new_depends_on
        note = ""
        if decision.get("verdict") and normalized:
            decision["verdict"] = None
            note = " (verdict reset — re-verify)"
        touch(session)
        save_session(session)
    confirm(
        "revise",
        session,
        f"{args.decision_id} updated{note}",
        verbose=getattr(args, "verbose", False),
    )


def cmd_rm(args: argparse.Namespace) -> None:
    """Handle ``rm``: remove a decision point from a session.

    Refuses to remove a decision still named in another decision's
    ``depends_on`` (referential integrity), unless ``--force`` is passed —
    in which case the referencing decision's ``depends_on`` entry goes
    dangling on purpose, which :func:`frontier` already treats as resolved.
    """
    slug = _resolve_slug(args.session, "rm")
    with session_lock(slug):
        session = load_session(slug)
        decision = find_decision(session, args.decision_id, "rm")
        if not getattr(args, "force", False):
            referencing = [
                d["id"]
                for d in session["decisions"]
                if args.decision_id in d.get("depends_on", [])
            ]
            if referencing:
                die(
                    "rm",
                    f"'{args.decision_id}' is still depended on by: "
                    f"{', '.join(referencing)} — use --force to remove anyway",
                )
        session["decisions"].remove(decision)
        state = "open" if is_open(decision) else "decided"
        touch(session)
        save_session(session)
    confirm(
        "rm",
        session,
        f"removed {args.decision_id} ({state})",
        verbose=getattr(args, "verbose", False),
    )


def cmd_verdict(args: argparse.Namespace) -> None:
    """Handle ``verdict``: record a verification result for a decided item."""
    slug = _resolve_slug(args.session, "verdict")
    patch = parse_json_arg(args.json, "verdict")

    result = _text(patch, "result")
    if result not in VALID_RESULTS:
        die(
            "verdict",
            f"invalid result '{result}' — one of: {', '.join(sorted(VALID_RESULTS))}",
        )

    evidence = _text(patch, "evidence").strip()
    if result in EVIDENCE_REQUIRED and not evidence:
        die(
            "verdict",
            f"{result} requires 'evidence' — what experiment was run, what happened",
        )

    with session_lock(slug):
        session = load_session(slug)
        decision = find_decision(session, args.decision_id, "verdict")
        if is_open(decision):
            die(
                "verdict",
                f"'{args.decision_id}' is still open — decide it before verifying",
            )
        decision["verdict"] = {"result": result, "evidence": evidence, "date": today()}
        touch(session)
        save_session(session)
    confirm(
        "verdict",
        session,
        f"{args.decision_id}: {result}",
        verbose=getattr(args, "verbose", False),
    )


def cmd_plan(args: argparse.Namespace) -> None:
    """Handle ``plan``: record the path of the model-authored plan artifact."""
    slug = _resolve_slug(args.session, "plan")
    path = Path(args.path).expanduser()
    if not path.exists():
        die(
            "plan",
            f"plan artifact not found at {path} — write it first, then record it",
        )
    with session_lock(slug):
        session = load_session(slug)
        session["plan_path"] = str(path)
        touch(session)
        save_session(session)
    confirm(
        "plan",
        session,
        f"plan artifact recorded: {path}",
        verbose=getattr(args, "verbose", False),
    )


def cmd_mark_pending_execution(args: argparse.Namespace) -> None:
    """Handle ``mark-pending-execution``: flag a session's plan as clear-and-go ready.

    ``--backlog-slug`` records which ``dev_status.py`` item this plan belongs
    to, when known — e.g. `/backlog-item`'s handoff step, which already has
    the item's slug in scope. `pending-plan` uses it to point the resumed
    session at `/backlog-item <slug>` instead of the plan file directly, so
    the item's own state/gates aren't bypassed.
    """
    backlog_slug = getattr(args, "backlog_slug", None)
    if backlog_slug is not None and not ID_RE.match(backlog_slug):
        die(
            "mark-pending-execution",
            f"invalid backlog slug '{backlog_slug}' — lowercase kebab-case",
        )
    slug = _resolve_slug(args.session, "mark-pending-execution")
    with session_lock(slug):
        session = load_session(slug)
        session["pending_execution"] = True
        if backlog_slug is not None:
            session["backlog_slug"] = backlog_slug
        touch(session)
        save_session(session)
    confirm(
        "mark-pending-execution",
        session,
        "pending_execution set",
        verbose=getattr(args, "verbose", False),
    )


def cmd_pending_plan(args: argparse.Namespace) -> None:
    """Handle ``pending-plan``: print (and optionally consume) the most recent
    pending-execution plan.

    Called from the SessionStart hook with ``--consume`` so a plan flagged via
    ``mark-pending-execution`` in a prior conversation surfaces automatically at
    the next session's start. Silent (no output) when nothing is pending, which
    is the common case — this must stay side-effect-free noise on every other
    session start. A single unreadable or non-session file in ``DATA_DIR``
    must not block the scan: ``load_session`` prints its own diagnostic and
    exits, so that's caught per-slug and skipped here rather than propagated.
    """
    pending = []
    for slug in all_session_slugs():
        try:
            session = load_session(slug)
        except SystemExit:
            continue
        if session.get("pending_execution"):
            pending.append(session)
    if not pending:
        return

    pending.sort(key=lambda s: str(s.get("updated", "")), reverse=True)
    slug = pending[0]["slug"]
    plan_path = pending[0].get("plan_path")
    backlog_slug = pending[0].get("backlog_slug")

    if args.consume:
        with session_lock(slug):
            session = load_session(slug)
            if session.get("pending_execution"):
                session["pending_execution"] = False
                touch(session)
                save_session(session)

    print(f"\U0001f4cb Grill plan ready to execute: {slug}")
    if plan_path:
        print(f"   Plan: {plan_path}")
    if backlog_slug:
        print(f"   Resume via: /backlog-item {backlog_slug}")
        print("   (If the user says go/continue, run that command — it resumes")
        print("    through the backlog item's own state and gates instead of")
        print("    implementing the plan file directly. If skip/no, take no")
        print("    further action — this flag is already cleared.)")
    else:
        print("   (If the user says go/continue, read the plan file and start")
        print("    implementing it directly. If skip/no, take no further action —")
        print("    this flag is already cleared.)")


def _print_frontier_item(d: Decision, session: Session, verbose: bool) -> None:
    """Print one decision in ``next``/``frontier``'s shared one-line format."""
    print(f"{d['id']}: {d['question']}")
    if d.get("reasoning"):
        print(f"  context: {d['reasoning']}")
    for dep_id in _dangling_deps(d, session):
        cli_common.vprint(
            f"note: '{d['id']}' depends_on '{dep_id}', which no longer exists "
            "— treating as resolved",
            verbose=verbose,
        )


def cmd_next(args: argparse.Namespace) -> None:
    """Handle ``next``: print the first frontier-ready open decision, if any."""
    session = resolve_session(args.session, "next")
    open_count = sum(1 for d in session["decisions"] if is_open(d))
    if open_count == 0:
        print("(no open questions)")
        return
    ready = frontier(session)
    if not ready:
        print(f"({open_count} open, all blocked)")
        return
    _print_frontier_item(ready[0], session, getattr(args, "verbose", False))


def cmd_frontier(args: argparse.Namespace) -> None:
    """Handle ``frontier``: print the batch of currently-askable open decisions.

    A decision is "currently-askable" once every id it names in
    ``depends_on`` is either decided or absent from the session (see
    :func:`frontier`) — this is what lets `grill-me`'s Q&A loop batch a
    deterministic, script-computed set of questions per round instead of
    trusting the model's own informal judgment of dependency order.
    """
    session = resolve_session(args.session, "frontier")
    verbose = getattr(args, "verbose", False)
    open_count = sum(1 for d in session["decisions"] if is_open(d))
    if open_count == 0:
        print("(no open questions)")
        return
    ready = frontier(session)
    if not ready:
        print(f"({open_count} open, all blocked)")
        return
    for d in ready:
        _print_frontier_item(d, session, verbose)


def cmd_render(args: argparse.Namespace) -> None:
    """Handle ``render``: print session status as Markdown."""
    session = resolve_session(args.session, "render")
    print(render_markdown(session), end="")


def cmd_list(args: argparse.Namespace) -> None:
    """Handle ``list``: print one summary line per session, tab-separated."""
    for slug in all_session_slugs():
        session = load_session(slug)
        decisions = session["decisions"]
        decided = [d for d in decisions if not is_open(d)]
        verified = sum(1 for d in decided if d.get("verdict"))
        print(
            f"{slug}\t{session.get('updated', '')[:10]}\t"
            f"{len(decided)}/{len(decisions)} decided\t{verified} verified\t"
            f"{session.get('topic', '')}"
        )


def cmd_show(args: argparse.Namespace) -> None:
    """Handle ``show``: print a session (or one decision within it) as JSON."""
    session = resolve_session(args.session, "show")
    if args.decision_id:
        print(json.dumps(find_decision(session, args.decision_id, "show"), indent=2))
    else:
        print(json.dumps(session, indent=2))


# ── main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    """Parse argv and dispatch to the matching subcommand handler."""
    ensure_data_dir()
    parser = argparse.ArgumentParser(
        description="grill-me session state CLI (all mutations go through here)",
    )
    # --quiet/-v are defined once, on every leaf subcommand parser only
    # (via this shared `parents=` parser) -- never on `parser` itself. See
    # dev_status.py's build_parser() for the full rationale.
    verbosity_parent = argparse.ArgumentParser(add_help=False)
    cli_common.add_verbosity_args(verbosity_parent)
    sub = parser.add_subparsers(
        dest="cmd",
        metavar=(
            "{new,ask,decide,revise,rm,verdict,plan,mark-pending-execution,"
            "pending-plan,next,frontier,render,list,show}"
        ),
    )

    def add_session_flag(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--session",
            "-s",
            default=None,
            help="session slug or unique substring (default: most recent)",
        )

    p = sub.add_parser("new", help="create a session", parents=[verbosity_parent])
    p.add_argument("json", metavar='\'{"topic": "..."}\'')

    p = sub.add_parser(
        "ask", help="register an open decision point", parents=[verbosity_parent]
    )
    p.add_argument(
        "json", metavar='\'{"id", "question", ["reasoning"], ["depends_on"]}\''
    )
    add_session_flag(p)

    p = sub.add_parser(
        "decide",
        help="resolve an open decision point (or add+decide in one shot)",
        parents=[verbosity_parent],
    )
    p.add_argument(
        "json",
        metavar=(
            '\'{"id", "decision", ["question"], ["reasoning"], ["source"], '
            '["depends_on"]}\''
        ),
    )
    add_session_flag(p)

    p = sub.add_parser(
        "revise",
        help="amend a decision (resets its verdict)",
        parents=[verbosity_parent],
    )
    p.add_argument("decision_id")
    p.add_argument("patch", metavar='\'{"decision": "...", ["depends_on"]}\'')
    add_session_flag(p)

    p = sub.add_parser(
        "rm",
        help="remove a decision point from a session",
        parents=[verbosity_parent],
    )
    p.add_argument("decision_id")
    p.add_argument(
        "--force",
        action="store_true",
        help="bypass the referential-integrity check (dangling depends_on allowed)",
    )
    add_session_flag(p)

    p = sub.add_parser(
        "verdict", help="record a verification verdict", parents=[verbosity_parent]
    )
    p.add_argument("decision_id")
    p.add_argument(
        "json",
        metavar='\'{"result": "VERIFIED|DISPUTED|UNVERIFIABLE", "evidence": "..."}\'',
    )
    add_session_flag(p)

    p = sub.add_parser(
        "plan",
        help="record the path of the model-authored plan artifact",
        parents=[verbosity_parent],
    )
    p.add_argument("path")
    add_session_flag(p)

    p = sub.add_parser(
        "mark-pending-execution",
        help="flag a session's plan as ready for clear-and-go resume",
        parents=[verbosity_parent],
    )
    p.add_argument(
        "--backlog-slug",
        default=None,
        help="dev_status.py item this plan belongs to, if any",
    )
    add_session_flag(p)

    p = sub.add_parser(
        "pending-plan",
        help="print (and optionally consume) the most recent pending-execution plan",
        parents=[verbosity_parent],
    )
    p.add_argument(
        "--consume",
        action="store_true",
        help="clear pending_execution on the printed session (one-shot)",
    )

    p = sub.add_parser(
        "next",
        help="print the first frontier-ready open decision point",
        parents=[verbosity_parent],
    )
    add_session_flag(p)

    p = sub.add_parser(
        "frontier",
        help="print the batch of currently-askable (dependency-resolved) open decisions",
        parents=[verbosity_parent],
    )
    add_session_flag(p)

    p = sub.add_parser(
        "render",
        help="print session status as markdown",
        parents=[verbosity_parent],
    )
    add_session_flag(p)

    sub.add_parser("list", help="list sessions", parents=[verbosity_parent])

    p = sub.add_parser(
        "show",
        help="print session (or one decision) as JSON",
        parents=[verbosity_parent],
    )
    p.add_argument("decision_id", nargs="?", default=None)
    add_session_flag(p)

    args = parser.parse_args()

    dispatch: dict[str, Callable[[argparse.Namespace], None]] = {
        "new": cmd_new,
        "ask": cmd_ask,
        "decide": cmd_decide,
        "revise": cmd_revise,
        "rm": cmd_rm,
        "verdict": cmd_verdict,
        "plan": cmd_plan,
        "mark-pending-execution": cmd_mark_pending_execution,
        "pending-plan": cmd_pending_plan,
        "next": cmd_next,
        "frontier": cmd_frontier,
        "render": cmd_render,
        "list": cmd_list,
        "show": cmd_show,
    }

    if args.cmd in dispatch:
        dispatch[args.cmd](args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
