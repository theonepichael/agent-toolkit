"""Pure text-formatting helpers shared by the backlog dashboard and recap.

This module deliberately owns no paths, environment reads, locks, IO, cache
state, or backend dispatch.  Its callers supply the small pieces of domain
normalization needed for journal timestamps and completion stamps, keeping the
formatting functions deterministic and independently reusable.
"""

import re
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

# What this module does with toolkit data (checked by scripts/check_toolkit_paths.py).
TOOLKIT_DATA = "none"

RECAP_PROMPT = """\
You are writing a short "welcome back" recap for a personal task dashboard, \
in the style of an away-summary: second person, warm, plain text, no emoji, \
1-3 sentences. Use ONLY the facts below -- never invent items, people, or \
events not listed. Refer to items by what they are (a short plain-language \
description), never by an internal id or slug, even if one appears in the \
facts below. Stay short, but be comprehensive within that space: name what \
was actually done rather than only a count or a vague gesture at it.

Latest completions (not necessarily within the last 48h):
{completed}

Other recent activity (last 48h, excluding completion transitions):
{changelog}

Current board: {buckets}
"""

_RECAP_ABBREV_TOKENS = frozenset(
    {
        "e.g",
        "i.e",
        "etc",
        "vs",
        "dr",
        "mr",
        "mrs",
        "ms",
        "prof",
        "sr",
        "jr",
        "st",
        "inc",
        "ltd",
        "fig",
        "no",
        "approx",
    }
)
_RECAP_MARKDOWN_RE = re.compile(r"[*_`#>~]")
_RECAP_EMOJI_RE = re.compile(
    "[\\U0001f300-\\U0001faff\\U00002600-\\U000027bf\\U0001f1e6-\\U0001f1ff]+"
)


def section_top(title: str, width: int) -> str:
    """Render a section's top border with an embedded title."""
    prefix = f"┌─ {title} "
    return prefix + ("─" * max(width - len(prefix), 3))


def section_bottom(width: int) -> str:
    """Render a section's bottom border."""
    return "└" + ("─" * (width - 1))


def ellipsize(text: str, limit: int) -> str:
    """Truncate ``text`` to ``limit`` display chars with a trailing ``…``."""
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 1)] + "…"


def project_prefix(slug: str, known_prefixes: Sequence[str]) -> str:
    """Extract a canonical project prefix, with a generic slug fallback."""
    for prefix in known_prefixes:
        if slug == prefix or slug.startswith(f"{prefix}-"):
            return prefix
    if "-" in slug:
        return slug.split("-", 1)[0]
    return ""


def project_divider(project: str, count: int, width: int) -> str:
    """Render an uncolored divider row for a project group."""
    unit = "item" if count == 1 else "items"
    label = f"─── {project} ({count} {unit}) "
    return f"│  {label}" + ("─" * max(width - len(f"│  {label}"), 3))


def format_age(seconds: float) -> str:
    """Render an age in seconds as a short marker: ``45m`` or ``3h``."""
    hours = seconds / 3600
    if hours < 1:
        return f"{max(1, int(seconds / 60))}m"
    return f"{int(hours)}h"


# Sentinel for ordering groups whose events carry no parseable timestamp.
# Aware (UTC) so it sorts against real parsed timestamps without a TypeError.
_EPOCH = datetime(1, 1, 1, tzinfo=UTC)


def render_changelog(
    entries: list[dict[str, object]],
    parse_timestamp: Callable[[object], datetime | None],
    max_lines: int = 60,
) -> str:
    """Pre-render non-completion journal entries into dense, per-item prompt facts.

    Journal entries that share an item (keyed by ``slug``, falling back to
    ``summary``) are coalesced into a single line: the item's latest summary,
    its first→last status arrow (omitted when the status never changed), an
    edit count (the number of ``update`` events), the union of changed field
    names, any ``reject`` feedback, and a compact list of other actions
    (``block``, ``unblock``, ``gate-set``, ``gate-pass``, ``rename``, ``add``,
    ``remove``) that touched the item. This collapses the ~400-line, ~50 KB
    per-event changelog (E-02) into at most ``max_lines`` item lines so the
    recap prompt spends tokens on facts rather than repetition.

    Board-wide events that carry neither a slug nor a summary (e.g.
    ``prune``, ``backfill-gate``) are kept as their own lines, keyed by
    command, so their counts stay visible. Diagnostic entries and completion
    (``done``) transitions are dropped before any grouping, as before.
    """
    groups: dict[object, list[dict[str, object]]] = {}
    group_last_ts: dict[object, datetime] = {}
    for entry in entries:
        if entry.get("diagnostic"):
            # Low-level lock-contention/stale-sweep/claim-theft telemetry is
            # post-mortem detail, never a workflow event a recap reader
            # should see — skip it before any rendering runs.
            continue
        if entry.get("to_status") == "done":
            continue
        slug = entry.get("slug")
        summary = entry.get("summary")
        if slug:
            key: object = slug
        elif summary:
            key = summary
        else:
            # Board-wide event (no item identity) — keep it distinct by command
            # so unrelated events don't merge and lose their count.
            key = f"__event__:{entry.get('cmd', '?')}"
        groups.setdefault(key, []).append(entry)
        ts = parse_timestamp(entry.get("ts"))
        if ts is not None:
            group_last_ts[key] = ts

    lines = []
    for key in sorted(groups, key=lambda k: group_last_ts.get(k, _EPOCH)):
        line = _render_changelog_group(groups[key], parse_timestamp, key)
        if line:
            lines.append(line)

    if len(lines) > max_lines:
        dropped = len(lines) - max_lines
        lines = lines[-max_lines:]
        lines.insert(0, f"(+{dropped} earlier items)")
    return "\n".join(lines)


def _render_changelog_group(
    evs: list[dict[str, object]],
    parse_timestamp: Callable[[object], datetime | None],
    key: object,
) -> str:
    """Render one coalesced changelog line for a group of item/event entries."""
    # Label: latest summary, else the slug, else (board-wide) the command.
    label = ""
    for entry in reversed(evs):
        if entry.get("summary"):
            label = str(entry["summary"])
            break
        if entry.get("slug"):
            label = str(entry["slug"])
            break
    if not label:
        label = str(evs[0].get("cmd", "?"))

    ts = parse_timestamp(evs[-1].get("ts"))
    time_str = ts.strftime("%H:%M") if ts else "??:??"

    edits = 0
    fields: list[str] = []
    other_actions: set[str] = set()
    feedbacks: list[str] = []
    transitions: list[tuple[object, object]] = []
    for entry in evs:
        cmd = entry.get("cmd")
        if cmd == "update":
            edits += 1
            for fld in entry.get("fields") or []:
                if fld not in fields:
                    fields.append(str(fld))
        elif entry.get("from_status") or entry.get("to_status"):
            transitions.append((entry.get("from_status"), entry.get("to_status")))
        elif not str(key).startswith("__event__:"):
            # A concrete action on the item that is neither an edit nor a
            # status transition (block, unblock, gate-set, gate-pass, rename,
            # add, remove) — keep it visible, collapsed to a set. Board-wide
            # events are keyed by command and already labelled with it, so
            # they are skipped here to avoid repeating the command.
            if cmd is not None:
                other_actions.add(str(cmd))
        fb = entry.get("feedback")
        if fb:
            feedbacks.append(str(fb))

    start_status = end_status = None
    if transitions:
        first = transitions[0]
        last = transitions[-1]
        start_status = first[0] or first[1]
        end_status = last[1] or last[0]

    clauses: list[str] = []
    if (
        start_status is not None
        and end_status is not None
        and start_status != end_status
    ):
        clauses.append(f"{start_status}→{end_status}")
    if edits:
        clauses.append(f"{edits} edit{'s' if edits != 1 else ''}")

    body = ""
    if clauses:
        body = " — " + "; ".join(clauses)
    if fields:
        body += f" (fields: {', '.join(fields)})"
    if other_actions:
        body += "; " + ", ".join(sorted(other_actions))
    # Feedback is prose that matters to a recap; bound it so a long rejection
    # can't defeat the line cap meant to keep the prompt small.
    if feedbacks:
        joined = "; ".join(ellipsize(fb, 200) for fb in feedbacks)
        body += f"; feedback: {joined}"

    # Board-wide events (no item identity) keep their count, as before.
    if str(key).startswith("__event__:"):
        count = evs[0].get("count")
        if count is not None:
            body += f" ({count} item(s))"

    return f"[{time_str}] {label}{body}"


def render_done_facts(
    items: list[dict[str, object]],
    done_stamp: Callable[[dict[str, object]], datetime | None],
) -> str:
    """Render selected completed items as dated, slug-free prompt facts."""
    lines = []
    for item in items:
        stamp = done_stamp(item)
        completed = stamp.date().isoformat() if stamp else "unknown date"
        lines.append(f"- {item.get('summary', '')} (completed {completed})")
    return "\n".join(lines)


def bucket_summary(
    in_progress: int, ready: int, blocked: int, in_review: int, done: int, pending: int
) -> str:
    """Render bucket section counts as a compact recap-prompt fact."""
    return ", ".join(
        [
            f"in progress: {in_progress}",
            f"ready: {ready}",
            f"blocked: {blocked}",
            f"in review: {in_review}",
            f"done (latest 5 max): {done}",
            f"pending: {pending}",
        ]
    )


def build_recap_prompt(
    changelog: str, buckets: str, completed: str, template: str = RECAP_PROMPT
) -> str:
    """Build the recap prompt from activity, selected completions, and counts."""
    return template.format(
        changelog=changelog or "(none)",
        completed=completed or "(none)",
        buckets=buckets,
    )


def recap_is_abbrev_boundary(text: str, dot: int) -> bool:
    """True when the dot at ``dot`` is an abbreviation or initial."""
    start = dot
    while start > 0 and not text[start - 1].isspace():
        start -= 1
    token = text[start:dot]
    return token.lower() in _RECAP_ABBREV_TOKENS or (
        len(token) == 1 and token.isalpha() and token.isupper()
    )


def recap_last_sentence_cut(
    text: str,
    budget: int,
    min_keep: int,
    is_abbrev_boundary: Callable[[str, int], bool] = recap_is_abbrev_boundary,
) -> int | None:
    """Return the last acceptable sentence boundary within ``budget``."""
    last: int | None = None
    for index, char in enumerate(text[:budget]):
        if char not in ".!?" or index + 1 >= len(text) or not text[index + 1].isspace():
            continue
        if char == "." and is_abbrev_boundary(text, index):
            continue
        if index + 1 >= min_keep:
            last = index + 1
    return last


def normalize_recap_text(
    raw: str,
    max_chars: int,
    min_keep: int,
    last_sentence_cut: Callable[[str, int, int], int | None] = recap_last_sentence_cut,
) -> str:
    """Strip presentation noise and fit backend recap prose within a budget."""
    text = _RECAP_EMOJI_RE.sub("", raw)
    text = _RECAP_MARKDOWN_RE.sub("", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_chars:
        return text
    cut = last_sentence_cut(text, max_chars, min_keep)
    if cut is not None:
        return text[:cut]
    return text[:max_chars].rstrip() + "…"
