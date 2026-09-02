#!/usr/bin/env python3
"""to_tickets_runner.py — create a linked batch of dev_status.py backlog
items from a confirmed vertical-slice/tracer-bullet ticket breakdown.

Driven by the ``to-tickets`` skill (one instance per harness), which drafts
the ticket breakdown and gets user confirmation before ever invoking this
script. This script owns only the mechanical part: computing a safe
execution order from each ticket's ``blocked_by`` edges, creating the
backlog items in that order, and making a partial run safely resumable.

Imports ``dev_status`` directly (see ``dev_status_sync.py`` for the same
precedent) rather than shelling out to its CLI — no subprocess, no shell
string, no CLI output to parse.

Usage
  to_tickets_runner.py run <batch-file.json>

Batch file schema
  A JSON array of ticket objects: ``{"id", "summary", "category",
  "context", "next_steps", "related_files", "blocked_by"}``. ``id`` and
  ``summary`` are required; the rest default to ``dev_status.py add``'s own
  defaults. ``blocked_by`` may name any slug — another ticket in this same
  batch, or an already-existing backlog item — regardless of where in the
  array it's drafted; this script computes its own execution order from the
  edges, it never trusts array position.

State file
  Written next to the batch file as ``<batch file>.state.json`` (i.e. the
  batch file's ``.json`` suffix replaced with ``.state.json``), recording
  a SHA-256 hash of the batch file's bytes plus which of its tickets have
  already been created. Re-running against the same, unmodified batch file
  resumes from where a prior, interrupted run left off; the state file is
  deleted on a fully successful run. A batch file whose content changed
  since the state file was written is refused, not silently resumed
  against.

Files read/written
  Reads the batch file and its state file. Writes/reads the same
  ``dev_status.py`` backlog store (``~/.claude/data/backlog/``) that
  ``dev_status.py`` itself and ``dev_status_sync.py`` use, via
  ``dev_status``'s own primitives (``backlog_lock``, ``load_items``,
  ``save_items``, ``append_journal_event``). Also creates
  ``~/.claude/data/to-tickets/`` on every invocation (see
  ``ensure_data_dir``) — the directory the skill has agents write their batch
  files into.

Exit codes
  0 success. 1 on any batch/schema/cycle/unknown-slug/stale-state/
  collision error (message on stderr).

Requires Python 3.12+, standard library only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import TypedDict, cast

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dev_status  # noqa: E402 — must follow the sys.path.insert above

DATA_DIR = Path.home() / ".claude" / "data" / "to-tickets"


def ensure_data_dir() -> None:
    """Create ``DATA_DIR`` if it is missing.

    Called once per invocation, before any subcommand runs. It is shared
    artifact storage: the ``to-tickets`` skill has the agent write its batch
    ``.json`` file there with its own file tools, not through this script, and
    agents used to run ``mkdir -p`` defensively first. Guaranteeing it here is
    what lets the skill docs drop that step. Deliberately its own directory,
    never ``grill.py``'s — that one is globbed as a private session store and
    cannot tolerate a non-session file landing in it.
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)


class Ticket(TypedDict):
    id: str
    summary: str
    category: str
    context: str
    next_steps: str
    related_files: list[dict[str, object]]
    blocked_by: list[str]


class BatchError(Exception):
    """A problem with the batch itself: bad schema, a cycle, an unknown slug."""


class SlugCollisionError(Exception):
    """A drafted slug collides with an unrelated, pre-existing item."""


def _validate_batch_schema(data: object) -> list[Ticket]:
    """Validate and normalize the raw JSON payload into a list of tickets.

    Raises:
        BatchError: If the payload isn't a well-formed batch (wrong shape,
            missing/mistyped required fields, or a duplicate id).
    """
    if not isinstance(data, list) or not data:
        raise BatchError("batch file must contain a non-empty JSON array")

    tickets: list[Ticket] = []
    seen: set[str] = set()
    for i, entry in enumerate(data):
        if not isinstance(entry, dict):
            raise BatchError(f"batch entry {i} must be a JSON object")

        raw_id = entry.get("id")
        if not isinstance(raw_id, str) or not raw_id:
            raise BatchError(f"batch entry {i} missing required string field 'id'")
        slug_err = dev_status.validate_slug(raw_id, "to-tickets")
        if slug_err:
            raise BatchError(slug_err)
        if raw_id in seen:
            raise BatchError(f"duplicate id '{raw_id}' within batch")
        seen.add(raw_id)

        summary = entry.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise BatchError(f"ticket '{raw_id}' missing required field 'summary'")

        blocked_by = entry.get("blocked_by", [])
        if not isinstance(blocked_by, list) or not all(
            isinstance(b, str) for b in blocked_by
        ):
            raise BatchError(
                f"ticket '{raw_id}' field 'blocked_by' must be a list of strings"
            )

        related_files = entry.get("related_files", [])
        if not isinstance(related_files, list):
            raise BatchError(f"ticket '{raw_id}' field 'related_files' must be a list")

        tickets.append(
            {
                "id": raw_id,
                "summary": summary,
                "category": cast(str, entry.get("category", "feature")),
                "context": cast(str, entry.get("context", "")),
                "next_steps": cast(str, entry.get("next_steps", "")),
                "related_files": cast(list[dict[str, object]], related_files),
                "blocked_by": cast(list[str], blocked_by),
            }
        )
    return tickets


def load_batch(path: Path) -> list[Ticket]:
    """Load and validate the batch file at ``path``.

    Raises:
        BatchError: If the file is missing, isn't valid JSON, or fails
            schema validation.
    """
    if not path.exists():
        raise BatchError(f"batch file not found: {path}")
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
        raise BatchError(f"batch file is not valid JSON: {path} ({e})") from e
    return _validate_batch_schema(data)


def compute_order(tickets: list[Ticket], index: dev_status.BacklogIndex) -> list[str]:
    """Compute a safe creation order for ``tickets`` from their ``blocked_by`` edges.

    A dependency naming an existing (non-batch) slug is checked against
    ``index`` and otherwise ignored for ordering purposes — it already
    exists, so it can never be part of a cycle with these tickets (an
    existing item's own ``blocked_by`` can only name slugs that already
    existed when *it* was added).

    Raises:
        BatchError: If a ``blocked_by`` entry names a slug that is neither
            in this batch nor in ``index``, or if the batch's own entries
            contain a dependency cycle.
    """
    batch_ids = {t["id"] for t in tickets}
    in_degree: dict[str, int] = dict.fromkeys(batch_ids, 0)
    dependents: dict[str, list[str]] = {tid: [] for tid in batch_ids}

    for ticket in tickets:
        tid = ticket["id"]
        for dep in ticket["blocked_by"]:
            if dep in batch_ids:
                dependents[dep].append(tid)
                in_degree[tid] += 1
            elif dep not in index:
                raise BatchError(
                    f"ticket '{tid}' depends on unknown slug '{dep}' — not in "
                    "this batch and not an existing backlog item"
                )

    queue = sorted(tid for tid in batch_ids if in_degree[tid] == 0)
    order: list[str] = []
    while queue:
        node = queue.pop(0)
        order.append(node)
        newly_ready = []
        for dependent in dependents[node]:
            in_degree[dependent] -= 1
            if in_degree[dependent] == 0:
                newly_ready.append(dependent)
        queue = sorted(queue + newly_ready)

    if len(order) != len(batch_ids):
        remaining = sorted(batch_ids - set(order))
        raise BatchError(f"batch contains a dependency cycle among: {remaining}")
    return order


def _state_path(batch_path: Path) -> Path:
    return batch_path.with_suffix(".state.json")


def _batch_hash(batch_path: Path) -> str:
    return hashlib.sha256(batch_path.read_bytes()).hexdigest()


def load_state(batch_path: Path) -> dict[str, object] | None:
    """Load the state file for ``batch_path``, or ``None`` if absent/unreadable."""
    state_path = _state_path(batch_path)
    if not state_path.exists():
        return None
    try:
        return cast(dict[str, object], json.loads(state_path.read_text()))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return None


def write_state(batch_path: Path, state: dict[str, object]) -> None:
    """Atomically write ``state`` to ``batch_path``'s state file."""
    state_path = _state_path(batch_path)
    tmp_path = state_path.with_name(state_path.name + ".tmp")
    tmp_path.write_text(json.dumps(state, indent=2))
    tmp_path.replace(state_path)


def delete_state(batch_path: Path) -> None:
    """Remove ``batch_path``'s state file, if any."""
    _state_path(batch_path).unlink(missing_ok=True)


def run(batch_path: Path) -> list[str]:
    """Create every ticket in ``batch_path``'s batch, resuming if interrupted before.

    Returns:
        The created (or already-created, on resume) ticket slugs, in the
        order they were/are created.

    Raises:
        BatchError: Bad schema, unknown external slug, a dependency cycle,
            or a stale state file (batch content changed since it was
            written).
        SlugCollisionError: A ticket's slug already exists in the backlog
            (or pending) store, and the state file does not already record
            it as created by a prior run of this exact batch.
    """
    tickets = load_batch(batch_path)
    by_id = {t["id"]: t for t in tickets}
    batch_hash = _batch_hash(batch_path)

    state = load_state(batch_path)
    if state is not None and state.get("batch_hash") != batch_hash:
        raise BatchError(
            f"stale state file for {batch_path}: the batch file has changed "
            "since this state was recorded — restore the original batch "
            f"file, or delete {_state_path(batch_path)} to start over"
        )
    added: dict[str, bool] = cast(
        dict[str, bool], (state or {}).get("added", {}) if state else {}
    )

    created: list[str] = []
    with dev_status.backlog_lock():
        items = dev_status.load_items()
        pending = dev_status.load_pending()
        index = dev_status.build_index(items)

        order = compute_order(tickets, index)

        for slug in order:
            if added.get(slug):
                created.append(slug)
                continue

            if slug in index or any(p["id"] == slug for p in pending):
                raise SlugCollisionError(
                    f"slug '{slug}' already exists in the backlog store, but "
                    "this batch's state file does not record it as created "
                    "by this run — likely a naming collision with an "
                    "unrelated item, aborting rather than silently linking "
                    "later tickets to it"
                )

            ticket = by_id[slug]
            new_item: dev_status.BacklogItem = {
                "id": slug,
                "created": dev_status.today(),
                "updated": dev_status.today(),
                "status": "open",
                "summary": ticket["summary"],
                "category": ticket["category"],
                "blocked_by": ticket["blocked_by"],
                "related_files": ticket["related_files"],
                "context": ticket["context"],
                "next_steps": ticket["next_steps"],
            }
            items.append(new_item)
            index[slug] = new_item

            new_rev = dev_status.bump_rev()
            dev_status.save_items(items)
            dev_status.append_journal_event(
                dev_status._journal_entry(
                    "add", "backlog", new_rev, slug=slug, summary=ticket["summary"]
                )
            )

            added[slug] = True
            created.append(slug)
            write_state(batch_path, {"batch_hash": batch_hash, "added": added})

    delete_state(batch_path)
    return created


def cmd_run(args: argparse.Namespace) -> None:
    batch_path = Path(args.batch_file).expanduser()
    try:
        created = run(batch_path)
    except (BatchError, SlugCollisionError) as e:
        print(f"[to-tickets] {e}", file=sys.stderr)
        sys.exit(1)

    print(f"Created {len(created)} ticket(s):")
    for slug in created:
        print(f"  {slug}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="to_tickets_runner.py",
        description="Create a linked batch of dev_status.py backlog items "
        "from a confirmed ticket breakdown.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("run", help="create every ticket in a batch file")
    p.add_argument("batch_file", help="path to the batch JSON file")
    p.set_defaults(func=cmd_run)
    return parser


def main() -> None:
    ensure_data_dir()
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
