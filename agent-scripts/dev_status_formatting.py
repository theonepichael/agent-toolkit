"""Pure text-formatting helpers shared by the backlog dashboard and recap.

This module deliberately owns no paths, environment reads, locks, IO, cache
state, or backend dispatch.  Its callers supply the small pieces of domain
normalization needed for journal timestamps and completion stamps, keeping the
formatting functions deterministic and independently reusable.
"""

import re
from collections.abc import Callable, Sequence
from datetime import datetime

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


def render_changelog(
    entries: list[dict[str, object]],
    parse_timestamp: Callable[[object], datetime | None],
) -> str:
    """Pre-render non-completion journal entries into dense prompt facts."""
    lines = []
    for entry in entries:
        if entry.get("diagnostic"):
            # Low-level lock-contention/stale-sweep/claim-theft telemetry is
            # post-mortem detail, never a workflow event a recap reader
            # should see — skip it before any rendering runs.
            continue
        if entry.get("to_status") == "done":
            continue
        timestamp = parse_timestamp(entry.get("ts"))
        time_str = timestamp.strftime("%H:%M") if timestamp else "??:??"
        line = f"[{time_str}] {entry.get('cmd', '?')}"
        slug = entry.get("slug")
        summary = entry.get("summary")
        if slug and not summary:
            line += f" {slug}"
        if summary:
            line += f" — {summary}"
        detail = []
        if entry.get("from_status") and entry.get("to_status"):
            detail.append(f"{entry['from_status']}→{entry['to_status']}")
        if entry.get("fields"):
            detail.append(f"changed: {', '.join(entry['fields'])}")
        if entry.get("feedback"):
            detail.append(f"feedback: {entry['feedback']}")
        if entry.get("count") is not None:
            detail.append(f"{entry['count']} item(s)")
        if detail:
            line += f" ({'; '.join(detail)})"
        lines.append(line)
    return "\n".join(lines)


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
