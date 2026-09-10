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

Service API
  Beyond argv, this module exposes a directly-callable session service for
  the decision lifecycle: ``open_session``/``all_sessions`` (unlocked
  reads), ``ask_decision``/``record_decision``/``revise_decision``/
  ``record_verdict`` (mutations — each takes the session slug, acquires
  ``session_lock``, and reloads from disk inside the lock, never trusting a
  caller-supplied snapshot), and ``frontier_of`` (pure). The service raises
  ``GrillSessionError`` subclasses (``SessionNotFoundError``,
  ``SessionFileError``, ``DecisionNotFoundError``, ``CycleError``,
  ``ValidationError``); only the ``cmd_*`` CLI adapters call ``die()``/exit.
  Partial updates use ``DecisionPatch``, a ``TypedDict(total=False)``
  mirroring ``Decision``'s mutable fields — a documented, narrow exception
  to the no-raw-dict-payload convention that keeps the CLI's flexible
  partial-field JSON patches typed.

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


# ── session service errors ───────────────────────────────────────────────────


class GrillSessionError(Exception):
    """Base class for every typed session-service failure."""


class SessionNotFoundError(GrillSessionError):
    """No readable session exists for the requested slug."""


class SessionFileError(GrillSessionError):
    """A session file exists but is corrupt, not a JSON object, or has an
    unrecognized schema version."""


class DecisionNotFoundError(GrillSessionError):
    """The named decision id does not exist within the session."""


class CycleError(GrillSessionError):
    """A ``depends_on`` change would introduce a dependency cycle."""


class ValidationError(GrillSessionError):
    """A patch field value, state transition, or ``depends_on`` payload is
    invalid."""


class DecisionPatch(TypedDict, total=False):
    """Partial-update payload for the decision-mutation service functions.

    Mirrors ``Decision``'s mutable fields; absent keys preserve the stored
    value. Text fields are typed ``str`` — the CLI adapter collapses an
    explicit JSON ``null`` to ``""`` before calling the service (and the
    service defensively treats a direct caller's ``None`` the same way), so
    a non-string never reaches a session file.
    """

    question: str
    reasoning: str
    decision: str
    source: str
    depends_on: list[str]


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
    """Validate a decision id's format and length (CLI contract: exits).

    Delegates the checks to the service-layer
    :func:`_validate_service_decision_id` and maps its ``ValidationError``
    to ``die()`` — the checks are defined once, not duplicated.

    Args:
        decision_id: The candidate id.
        context: Command name to prefix onto any error message.

    Raises:
        SystemExit: If ``decision_id`` isn't lowercase kebab-case or is
            outside ``[ID_MIN, ID_MAX]`` characters.
    """
    try:
        _validate_service_decision_id(decision_id)
    except ValidationError as e:
        die(context, str(e))


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


def session_path(slug: str, data_dir: Path | None = None) -> Path:
    """Return the on-disk path for the session identified by ``slug``."""
    return (data_dir if data_dir is not None else DATA_DIR) / f"{slug}.json"


def open_session(slug: str, *, data_dir: Path | None = None) -> Session:
    """Load one session by exact slug as a typed service operation.

    This is the service-layer read; :func:`load_session` is its CLI-facing
    wrapper (prints and exits). Takes an exact slug — no substring or
    latest-session resolution, which stay CLI-side in ``_resolve_slug``.

    Args:
        slug: The session's exact slug (lowercase kebab-case).
        data_dir: Directory holding the session files; ``None`` uses the
            module-global ``DATA_DIR``.

    Returns:
        The decoded session.

    Raises:
        SessionNotFoundError: If ``slug`` is malformed or has no file.
        SessionFileError: If the file contains invalid JSON, isn't a JSON
            object, or has an unrecognized schema version.
    """
    if not ID_RE.match(slug):
        raise SessionNotFoundError(f"invalid session slug '{slug}'")
    path = session_path(slug, data_dir)
    try:
        raw = path.read_text()
    except FileNotFoundError as e:
        raise SessionNotFoundError(
            f"no grill session '{slug}' at {path} — create one with 'new'"
        ) from e
    except OSError as e:
        raise SessionFileError(f"session file at {path} is unreadable ({e})") from e
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SessionFileError(
            f"session file corrupted at {path}; fix or restore from backup. ({e})"
        ) from e
    if not isinstance(data, dict):
        raise SessionFileError(
            f"session file at {path} is not a JSON object "
            f"(found {type(data).__name__}); check file or remove it from the "
            "sessions directory."
        )
    if data.get("schema_version") != SCHEMA_VERSION:
        raise SessionFileError(
            f"session file at {path} is not schema_version {SCHEMA_VERSION}; "
            "check file or run migration."
        )
    return cast(Session, data)


def load_session(slug: str) -> Session:
    """Load one session by slug, rendering service errors for the CLI.

    CLI-facing wrapper over :func:`open_session` used by the non-adapter
    commands and ``cmd_pending_plan``: every ``GrillSessionError`` prints
    its message (no ``[context]`` prefix, matching today's corrupt-file
    output) and exits with status 1.

    Args:
        slug: The session's slug.

    Returns:
        The decoded session.

    Raises:
        SystemExit: If the file is missing, unreadable, contains invalid
            JSON, isn't a JSON object, or has an unrecognized schema
            version. Exits with status 1 after printing a diagnostic to
            stderr.
    """
    try:
        return open_session(slug)
    except GrillSessionError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)


def ensure_data_dir(data_dir: Path | None = None) -> None:
    """Create the grill data directory if it is missing.

    Called once per invocation, before any subcommand runs, so the directory
    is present even for read-only commands. It is shared artifact storage:
    agents write plan and spec ``.md`` files there with their own file tools,
    not through this script, and used to run ``mkdir -p`` defensively first.
    Guaranteeing it here is what lets the skill docs drop that step.
    """
    (data_dir if data_dir is not None else DATA_DIR).mkdir(parents=True, exist_ok=True)


def save_session(session: Session, data_dir: Path | None = None) -> None:
    """Atomically persist ``session`` to its slug-derived path."""
    base = data_dir if data_dir is not None else DATA_DIR
    ensure_data_dir(base)
    payload = json.dumps(session, indent=2)
    fd, tmp_path = tempfile.mkstemp(dir=base, prefix=".session_tmp_")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(payload)
        os.replace(tmp_path, session_path(session["slug"], base))
    except Exception:
        with suppress(OSError):
            os.unlink(tmp_path)
        raise


def all_session_slugs(data_dir: Path | None = None) -> list[str]:
    """Return every session slug on disk, sorted, or ``[]`` if none exist."""
    base = data_dir if data_dir is not None else DATA_DIR
    if not base.exists():
        return []
    return sorted(p.stem for p in base.glob("*.json"))


def all_sessions(*, data_dir: Path | None = None) -> list[Session]:
    """Bulk-load every readable session, sorted by slug.

    The formalized read API other modules consume (candidate 8's
    ``vitals_promotion`` loads its input through this instead of re-reading
    grill's on-disk JSON directly). Tolerant by design, matching
    ``cmd_pending_plan``'s bulk scan: a file that raises
    ``GrillSessionError`` (corrupt, wrong schema version, unreadable) is
    skipped with a one-line stderr warning so corruption is visible rather
    than silent — never fatal to the batch.

    Args:
        data_dir: Directory holding the session files; ``None`` uses the
            module-global ``DATA_DIR``.

    Returns:
        Every readable session, sorted by slug; ``[]`` when the directory
        does not exist.
    """
    base = data_dir if data_dir is not None else DATA_DIR
    if not base.exists():
        return []
    sessions: list[Session] = []
    for path in sorted(base.glob("*.json")):
        try:
            sessions.append(open_session(path.stem, data_dir=base))
        except GrillSessionError as e:
            print(f"skipping unreadable grill session: {path} ({e})", file=sys.stderr)
    return sessions


@contextmanager
def _flock(path: Path, data_dir: Path | None = None) -> Iterator[None]:
    """Hold an exclusive advisory lock on ``path`` for the block's duration."""
    ensure_data_dir(data_dir)
    with open(path, "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


@contextmanager
def session_lock(slug: str, *, data_dir: Path | None = None) -> Iterator[None]:
    """Hold an exclusive lock over one session's read-modify-write cycle.

    Serializes concurrent mutators of the same session file — e.g. two
    agents in different terminals racing a ``decide`` and a ``verdict``
    against the same slug — so a read-modify-write cycle can't interleave
    and silently lose one side's write. Every mutating subcommand
    (``ask``/``decide``/``revise``/``rm``/``verdict``/``plan``) resolves
    its target slug, acquires this lock, then reloads the session fresh
    before mutating — never operating on a copy read before the lock was
    held.

    This is also the documented public composition primitive for direct
    service callers: a caller needing a consistent post-mutation snapshot
    re-loads under ``session_lock(slug)`` itself. Never call a mutation
    function while already holding this lock in the same process —
    ``fcntl.flock`` self-deadlocks across open file descriptions — and no
    service function ever nests locks internally.
    """
    base = data_dir if data_dir is not None else DATA_DIR
    with _flock(base / f".{slug}.lock", base):
        yield


@contextmanager
def _new_session_lock(data_dir: Path | None = None) -> Iterator[None]:
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
    """Find a decision by id within a session (CLI contract: exits on miss).

    Delegates the lookup to the service-layer :func:`_get_decision` and
    maps its ``DecisionNotFoundError`` to ``die()``.

    Args:
        session: The session to search.
        decision_id: The decision's id.
        context: Command name to prefix onto any error message.

    Returns:
        The matching decision.

    Raises:
        SystemExit: If no decision with that id exists in the session.
    """
    try:
        return _get_decision(session, decision_id)
    except DecisionNotFoundError as e:
        die(context, str(e))


def is_open(decision: Decision) -> bool:
    """Return whether ``decision`` has not yet been decided."""
    return decision.get("decision") is None


def _validate_depends_on(
    session: Session, owner_id: str, raw: object, context: str
) -> list[str]:
    """Validate and normalize a ``depends_on`` array from a JSON patch.

    CLI wrapper over the service-layer :func:`_svc_validate_depends_on`:
    maps its ``ValidationError`` to ``die()`` with the given command-name
    prefix. Kept only for the non-adapter mutating commands; the four
    service-adapted paths validate via the service directly.

    Args:
        session: The session to check referenced ids against.
        owner_id: The id of the decision ``raw`` will be attached to.
        raw: The decoded ``depends_on`` value from a JSON patch.
        context: Command name to prefix onto any error message.
    """
    try:
        return _svc_validate_depends_on(session, owner_id, raw)
    except ValidationError as e:
        die(context, str(e))


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


def confirm(context: str, slug: str, detail: str, verbose: bool = False) -> None:
    """Echo a mutating command's outcome to stderr."""
    cli_common.vprint(f"[{context}] {slug}: {detail}", verbose=verbose)


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


# ── session service ─────────────────────────────────────────────────────────


def _validate_service_decision_id(decision_id: str) -> None:
    """Validate a decision id's format and length, raising typed errors.

    Service-layer counterpart of :func:`validate_decision_id` (which keeps
    its die() contract for ``cmd_new`` and delegates here). Message bodies
    are byte-identical to the CLI's.

    Raises:
        ValidationError: If ``decision_id`` isn't lowercase kebab-case or
            is outside ``[ID_MIN, ID_MAX]`` characters.
    """
    if not ID_RE.match(decision_id):
        raise ValidationError(
            f"invalid decision id '{decision_id}' — lowercase kebab-case"
        )
    if not (ID_MIN <= len(decision_id) <= ID_MAX):
        raise ValidationError(
            f"decision id '{decision_id}' length {len(decision_id)} "
            f"out of range [{ID_MIN},{ID_MAX}]"
        )


def _svc_text(value: object, default: str = "") -> str:
    """Service-side collapse of an explicit ``None`` to ``default``.

    Direct callers bypass the CLI adapter's ``_text`` collapse, so a ``None``
    in a text patch field is defensively treated the same as a missing key
    rather than stored as a non-string.
    """
    if value is None:
        return default
    return str(value)


def _svc_validate_depends_on(session: Session, owner_id: str, raw: object) -> list[str]:
    """Service-layer ``depends_on`` validation, raising typed errors.

    Same checks and normalization as the CLI's ``_validate_depends_on``
    (type, order-preserving dedupe, self-reference, id format, unknown
    ids), with ``ValidationError`` instead of ``die()``. Message bodies are
    byte-identical. All-or-nothing: raises before returning on any failure.
    """
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise ValidationError("'depends_on' must be a list of strings")
    raw_ids = cast(list[str], raw)

    deduped: list[str] = []
    for dep_id in raw_ids:
        if dep_id not in deduped:
            deduped.append(dep_id)

    if owner_id in deduped:
        raise ValidationError(f"'depends_on' cannot reference its own id '{owner_id}'")

    for dep_id in deduped:
        _validate_service_decision_id(dep_id)

    existing_ids = {d["id"] for d in session["decisions"]}
    bad = [dep_id for dep_id in deduped if dep_id not in existing_ids]
    if bad:
        raise ValidationError(
            f"'depends_on' references unknown decision id(s): {', '.join(bad)}"
        )

    return deduped


def _get_decision(session: Session, decision_id: str) -> Decision:
    """Find a decision by id within a session, raising a typed error.

    Pure, in-memory lookup — no I/O, no lock. The CLI-facing
    :func:`find_decision` wraps this with its die() contract.

    Raises:
        DecisionNotFoundError: If no decision with that id exists.
    """
    for decision in session["decisions"]:
        if decision["id"] == decision_id:
            return decision
    raise DecisionNotFoundError(
        f"no decision '{decision_id}' in session {session['slug']}"
    )


def _mutate(
    slug: str,
    context: str,
    mutate_fn: Callable[[Session], Decision],
    *,
    data_dir: Path | None = None,
) -> Decision:
    """Run one decision mutation under ``session_lock`` with lock-then-reload.

    Acquires the session's exclusive lock, reloads the session fresh from
    disk inside it (never trusting a caller-supplied snapshot), applies
    ``mutate_fn`` (which validates state-dependently, mutates, stamps
    ``touch`` and saves), and returns the mutated decision. The lock is
    released on return.
    """
    with session_lock(slug, data_dir=data_dir):
        session = open_session(slug, data_dir=data_dir)
        decision = mutate_fn(session)
        touch(session)
        save_session(session, data_dir)
        return decision


def ask_decision(
    slug: str, decision_id: str, patch: DecisionPatch, *, data_dir: Path | None = None
) -> Decision:
    """Register a new open decision point in the session.

    Service path of ``cmd_ask``. Validates format, required fields and
    ``depends_on`` pre-lock; the duplicate-id check runs inside the lock on
    the freshly reloaded session, as in today's CLI.

    Args:
        slug: The session's exact slug.
        decision_id: The new decision's id (lowercase kebab-case).
        patch: ``question`` (required), ``reasoning`` (optional),
            ``depends_on`` (optional).
        data_dir: Directory holding the session files; ``None`` uses the
            module-global ``DATA_DIR``.

    Returns:
        The newly appended open decision.

    Raises:
        ValidationError: On a bad id, missing/blank question, duplicate id,
            or invalid ``depends_on``.
        SessionNotFoundError / SessionFileError: Via the reload.
    """
    if not decision_id:
        raise ValidationError("'id' is required")
    _validate_service_decision_id(decision_id)
    question = _svc_text(patch.get("question")).strip()
    if not question:
        raise ValidationError("'question' is required")
    reasoning = _svc_text(patch.get("reasoning")).strip()
    depends_on_given = "depends_on" in patch

    def mutate_fn(session: Session) -> Decision:
        if any(d["id"] == decision_id for d in session["decisions"]):
            raise ValidationError(f"duplicate decision id: {decision_id}")
        deps: list[str] = []
        if depends_on_given:
            deps = _svc_validate_depends_on(session, decision_id, patch["depends_on"])
        decision: Decision = {
            "id": decision_id,
            "question": question,
            "reasoning": reasoning,
            "decision": None,
            "source": None,
            "verdict": None,
            "depends_on": deps,
        }
        session["decisions"].append(decision)
        return decision

    return _mutate(slug, "ask", mutate_fn, data_dir=data_dir)


def record_decision(
    slug: str, decision_id: str, patch: DecisionPatch, *, data_dir: Path | None = None
) -> Decision:
    """Resolve an open decision point, or add-and-decide in one shot.

    Service path of ``cmd_decide``. Mirrors its three paths exactly: an
    unknown id creates a decided decision (requiring ``question``); an open
    id is resolved (updating ``question``/``reasoning``/``depends_on`` only
    when given); an already-decided id is refused.

    Args:
        slug: The session's exact slug.
        decision_id: The decision's id.
        patch: ``decision`` (required), ``question`` (required only on the
            create path), ``source`` (defaults to ``user``), ``reasoning``
            and ``depends_on`` (optional; presence-gated updates).
        data_dir: Directory holding the session files; ``None`` uses the
            module-global ``DATA_DIR``.

    Returns:
        The mutated decision.

    Raises:
        ValidationError: On a bad id, missing/blank required field, invalid
            source, already-decided target, or invalid ``depends_on``.
        CycleError: If a ``depends_on`` change would introduce a cycle.
        SessionNotFoundError / SessionFileError: Via the reload.
    """
    if not decision_id:
        raise ValidationError("'id' is required")
    _validate_service_decision_id(decision_id)
    decision_text = _svc_text(patch.get("decision")).strip()
    if not decision_text:
        raise ValidationError("'decision' is required")
    source = _svc_text(patch.get("source"), "user")
    if source not in VALID_SOURCES:
        raise ValidationError(
            f"invalid source '{source}' — one of: {', '.join(sorted(VALID_SOURCES))}"
        )
    question = _svc_text(patch.get("question")).strip()
    reasoning_given = "reasoning" in patch
    reasoning = _svc_text(patch.get("reasoning")).strip()
    depends_on_given = "depends_on" in patch

    def mutate_fn(session: Session) -> Decision:
        decisions = session["decisions"]
        existing = next((d for d in decisions if d["id"] == decision_id), None)

        if existing is not None:
            if not is_open(existing):
                raise ValidationError(
                    f"'{decision_id}' is already decided — use revise"
                )
            if depends_on_given:
                validated = _svc_validate_depends_on(
                    session, decision_id, patch["depends_on"]
                )
                if _would_cycle(session, decision_id, validated):
                    raise CycleError(
                        "'depends_on' would introduce a cycle involving "
                        f"'{decision_id}'"
                    )
                existing["depends_on"] = validated
            existing["decision"] = decision_text
            existing["source"] = source
            if question:
                existing["question"] = question
            if reasoning_given:
                existing["reasoning"] = reasoning
            return existing

        if not question:
            raise ValidationError(
                "'question' is required for a decision point not registered via ask"
            )
        deps = []
        if depends_on_given:
            deps = _svc_validate_depends_on(session, decision_id, patch["depends_on"])
        decision = {
            "id": decision_id,
            "question": question,
            "reasoning": reasoning,
            "decision": decision_text,
            "source": source,
            "verdict": None,
            "depends_on": deps,
        }
        decisions.append(decision)
        return decision

    return _mutate(slug, "decide", mutate_fn, data_dir=data_dir)


def revise_decision(
    slug: str, decision_id: str, patch: DecisionPatch, *, data_dir: Path | None = None
) -> Decision:
    """Amend a decided decision's text, resetting its verdict.

    Service path of ``cmd_revise``. Only ``REVISABLE_FIELDS`` keys are
    accepted (an ``id`` key inside the patch is rejected, not a rename);
    ``question``/``decision`` cannot be blanked; an open decision cannot be
    given ``decision``/``source``; a ``depends_on``-only change does not
    reset an existing verdict.

    Args:
        slug: The session's exact slug.
        decision_id: The decision's id.
        patch: Any of ``question``, ``decision``, ``reasoning``, ``source``,
            ``depends_on``.
        data_dir: Directory holding the session files; ``None`` uses the
            module-global ``DATA_DIR``.

    Returns:
        The mutated decision.

    Raises:
        ValidationError: On a foreign patch key, blank required field,
            invalid source, open-target transition, or invalid
            ``depends_on``.
        CycleError: If a ``depends_on`` change would introduce a cycle.
        DecisionNotFoundError: If ``decision_id`` has no decision.
        SessionNotFoundError / SessionFileError: Via the reload.
    """
    bad = set(patch) - REVISABLE_FIELDS
    if bad:
        raise ValidationError(f"cannot revise field(s): {', '.join(sorted(bad))}")

    normalized: dict[str, str] = {}
    for field in ("question", "decision", "reasoning"):
        if field in patch:
            text = _svc_text(patch[field])
            if field in ("question", "decision") and not text.strip():
                raise ValidationError(f"'{field}' cannot be blank")
            normalized[field] = text
    if "source" in patch:
        source = _svc_text(patch["source"])
        if source not in VALID_SOURCES:
            raise ValidationError(f"invalid source '{source}'")
        normalized["source"] = source

    depends_on_given = "depends_on" in patch

    def mutate_fn(session: Session) -> Decision:
        decision = _get_decision(session, decision_id)
        if is_open(decision) and ({"decision", "source"} & set(normalized)):
            raise ValidationError(
                f"'{decision_id}' is still open — resolve it with decide"
            )

        new_depends_on: list[str] | None = None
        if depends_on_given:
            new_depends_on = _svc_validate_depends_on(
                session, decision_id, patch["depends_on"]
            )
            if _would_cycle(session, decision_id, new_depends_on):
                raise CycleError(
                    f"'depends_on' would introduce a cycle involving '{decision_id}'"
                )

        cast(dict[str, object], decision).update(normalized)
        if new_depends_on is not None:
            decision["depends_on"] = new_depends_on
        if decision.get("verdict") and normalized:
            decision["verdict"] = None
        return decision

    return _mutate(slug, "revise", mutate_fn, data_dir=data_dir)


def record_verdict(
    slug: str, decision_id: str, verdict: Verdict, *, data_dir: Path | None = None
) -> Decision:
    """Record a verification result for a decided decision.

    Service path of ``cmd_verdict``. ``date`` is always stamped with
    today's ISO date — a caller-supplied ``date`` is overwritten (the CLI
    never passes one). Extra keys in ``verdict`` are ignored; the stored
    verdict contains exactly ``result``, ``evidence`` and ``date``.

    Args:
        slug: The session's exact slug.
        decision_id: The decision's id.
        verdict: ``result`` (one of ``VALID_RESULTS``) and ``evidence``
            (required for ``VERIFIED``/``DISPUTED``).
        data_dir: Directory holding the session files; ``None`` uses the
            module-global ``DATA_DIR``.

    Returns:
        The mutated decision with its new verdict.

    Raises:
        ValidationError: On an invalid result, missing evidence, or an
            open target decision.
        DecisionNotFoundError: If ``decision_id`` has no decision.
        SessionNotFoundError / SessionFileError: Via the reload.
    """
    result = _svc_text(verdict.get("result"))
    if result not in VALID_RESULTS:
        raise ValidationError(
            f"invalid result '{result}' — one of: {', '.join(sorted(VALID_RESULTS))}"
        )
    evidence = _svc_text(verdict.get("evidence")).strip()
    if result in EVIDENCE_REQUIRED and not evidence:
        raise ValidationError(
            f"{result} requires 'evidence' — what experiment was run, what happened"
        )

    def mutate_fn(session: Session) -> Decision:
        decision = _get_decision(session, decision_id)
        if is_open(decision):
            raise ValidationError(
                f"'{decision_id}' is still open — decide it before verifying"
            )
        decision["verdict"] = {
            "result": result,
            "evidence": evidence,
            "date": today(),
        }
        return decision

    return _mutate(slug, "verdict", mutate_fn, data_dir=data_dir)


def frontier_of(session: Session) -> DecisionList:
    """Service-API name for :func:`frontier` — every open decision whose
    dependencies are all resolved.

    Pure: takes an in-memory ``Session``, performs no I/O and takes no
    lock. Slug-keyed callers compose it as ``frontier_of(open_session(
    slug))``.
    """
    return frontier(session)


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
    confirm("new", session["slug"], topic, verbose=getattr(args, "verbose", False))
    print(slug)


def cmd_ask(args: argparse.Namespace) -> None:
    """Handle ``ask``: register an open decision point (service adapter)."""
    slug = _resolve_slug(args.session, "ask")
    patch = parse_json_arg(args.json, "ask")

    svc_patch: DecisionPatch = {
        "question": _text(patch, "question").strip(),
        "reasoning": _text(patch, "reasoning").strip(),
    }
    if "depends_on" in patch:
        svc_patch["depends_on"] = patch["depends_on"]  # type: ignore[assignment]

    try:
        decision = ask_decision(slug, _text(patch, "id").strip(), svc_patch)
    except GrillSessionError as e:
        die("ask", str(e))
    confirm(
        "ask",
        slug,
        f"? {decision['id']} — {decision['question'][:60]}",
        verbose=getattr(args, "verbose", False),
    )


def cmd_decide(args: argparse.Namespace) -> None:
    """Handle ``decide``: resolve an open decision point, or add+decide in one
    shot (service adapter).
    """
    slug = _resolve_slug(args.session, "decide")
    patch = parse_json_arg(args.json, "decide")

    svc_patch: DecisionPatch = {
        "question": _text(patch, "question").strip(),
        "decision": _text(patch, "decision").strip(),
        "source": _text(patch, "source", "user"),
        "reasoning": _text(patch, "reasoning").strip(),
    }
    if "depends_on" in patch:
        svc_patch["depends_on"] = patch["depends_on"]  # type: ignore[assignment]

    try:
        decision = record_decision(slug, _text(patch, "id").strip(), svc_patch)
    except GrillSessionError as e:
        die("decide", str(e))
    confirm(
        "decide",
        slug,
        f"{decision['id']} ({decision['source']}) — {decision['decision'][:60]}",
        verbose=getattr(args, "verbose", False),
    )


def cmd_revise(args: argparse.Namespace) -> None:
    """Handle ``revise``: amend a decision's text (resets its verdict).

    Service adapter: the revision logic lives in :func:`revise_decision`;
    this wrapper only parses argv, renders the outcome, and maps typed
    errors to ``die()``. The pre-call unlocked read exists only to render
    the "verdict reset" suffix, which depends on pre-mutation state the
    returned decision cannot reveal (a reset verdict and a never-recorded
    verdict are both ``None`` afterwards).
    """
    slug = _resolve_slug(args.session, "revise")
    patch = parse_json_arg(args.patch, "revise")

    # Pass the parsed patch through as-is (unknown keys included — the
    # service rejects them); explicit nulls are collapsed by the service's
    # _svc_text, matching the CLI's _text semantics.
    svc_patch = cast(DecisionPatch, dict(patch))

    try:
        pre = open_session(slug)
        had_verdict = _get_decision(pre, args.decision_id).get("verdict") is not None
    except GrillSessionError:
        had_verdict = False  # the service call below raises the real error

    try:
        revise_decision(slug, args.decision_id, svc_patch)
    except GrillSessionError as e:
        die("revise", str(e))
    normalized_touched = any(
        field in svc_patch for field in ("question", "decision", "reasoning", "source")
    )
    note = " (verdict reset — re-verify)" if had_verdict and normalized_touched else ""
    confirm(
        "revise",
        slug,
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
        slug,
        f"removed {args.decision_id} ({state})",
        verbose=getattr(args, "verbose", False),
    )


def cmd_verdict(args: argparse.Namespace) -> None:
    """Handle ``verdict``: record a verification result for a decided item
    (service adapter).
    """
    slug = _resolve_slug(args.session, "verdict")
    patch = parse_json_arg(args.json, "verdict")

    verdict: Verdict = {
        "result": _text(patch, "result"),
        "evidence": _text(patch, "evidence").strip(),
        "date": today(),
    }
    try:
        decision = record_verdict(slug, args.decision_id, verdict)
    except GrillSessionError as e:
        die("verdict", str(e))
    confirm(
        "verdict",
        slug,
        f"{args.decision_id}: {decision['verdict']['result']}",
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
        slug,
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
        slug,
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
