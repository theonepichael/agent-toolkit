#!/usr/bin/env python3
"""Read-only snapshot lookup over the backlog store for guard consumers.

`guard_rails.py` used to own its own item-reading logic (`load_in_progress()`
parsing the JSON directly) on top of its `GUARD_RAILS_STORE`-overridable path
resolution, and resolved per-item claims straight off the raw `claimed_by`
dicts. This module is the one place that read lives now:

- `ClaimInfo` — the claim fields the guard actually reads, mapped from
  `dev_status_impl._make_claim()`'s claim dict (`harness`, `pid_namespace`,
  `ancestors` and friends stay in the store, out of the protocol).
- `BacklogClaimLookup` — the read-only protocol `guard_rails.evaluate()`
  depends on. Candidate 10 (`herdr_delegate`'s queue facade) reuses this
  module rather than the adapter, so the three-method shape is fixed here
  even though `evaluate()` only calls two of them.
- `LocalClaimLookup` — the adapter `main()` constructs. It captures one
  atomic snapshot of the store on its first read and serves every method
  from it: a single guard evaluation can never observe half of a store
  write, and a transient unreadable store can never flip an allow into a
  deny mid-evaluation. A fresh view is a fresh instance; the guard process
  is short-lived, so nothing caches across runs.

Environment
  GUARD_RAILS_STORE    path to an alternate backlog store — the single
                       source of truth for this contract (guard_rails.py's
                       docstring references it, it lives here). Only
                       reachable from the environment the harness was
                       launched in, like GUARD_RAILS_OFF.

Nothing here mutates the store, and nothing here imports `guard_rails` or
`dev_status_impl` — the dependency direction is strictly
guard_rails → this module → (types only) dev_status_storage.

Usage:
    from backlog_claim_lookup import BacklogClaimLookup, LocalClaimLookup

    claims: BacklogClaimLookup = LocalClaimLookup()
    items = claims.in_progress_items()
    info = claims.claim_info("my-slug")
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from dev_status_storage import BacklogItem

DEFAULT_BACKLOG_ITEMS = Path.home() / ".claude" / "data" / "backlog" / "items.json"


def backlog_items_path() -> Path:
    """Where the backlog store lives. ``GUARD_RAILS_STORE`` overrides it so
    the guard can be exercised end-to-end against a throwaway store instead
    of the real one. Like ``GUARD_RAILS_OFF``, this is only reachable from
    the environment the harness was launched in -- an agent's own shell
    cannot reach the hook's environment."""
    override = os.environ.get("GUARD_RAILS_STORE")
    return Path(override) if override else DEFAULT_BACKLOG_ITEMS


@dataclass(frozen=True)
class ClaimInfo:
    """The fields of a claim record the guard's verdict logic reads.

    Mapped from ``dev_status_impl._make_claim()``'s claim dict -- which also
    carries ``harness``, ``pid_namespace`` and ``ancestors``; those stay in
    the store, out of this protocol. ``owner_pid`` is the durable claim
    holder found by the ancestor walk; ``pid`` is the short-lived invoking
    process, distinct from it."""

    machine_id: str
    owner_pid: int
    pid: int
    last_active: str | None = None
    claimed_at: str | None = None

    @classmethod
    def from_dict(cls, claim: object) -> ClaimInfo | None:
        """Coerce a store's ``claimed_by`` value into a ClaimInfo.

        Every rule is verdict-equivalent to guard_rails' former dict-level
        checks on the same input: a non-dict (or None) claim is not a claim;
        machine_id must genuinely be a str (a JSON null never becomes the
        string ``"None"``); pid fields coerce via the same digit test the
        old code used, anything else falling back to 0; timestamps must be
        non-empty strings.
        """
        if not isinstance(claim, dict):
            return None

        def _int(value: object) -> int:
            return int(value) if str(value or "").isdigit() else 0

        def _stamp(value: object) -> str | None:
            return value if isinstance(value, str) and value else None

        machine = claim.get("machine_id")
        return cls(
            machine_id=machine if isinstance(machine, str) else "",
            owner_pid=_int(claim.get("owner_pid")),
            pid=_int(claim.get("pid")),
            last_active=_stamp(claim.get("last_active")),
            claimed_at=_stamp(claim.get("claimed_at")),
        )


@runtime_checkable
class BacklogClaimLookup(Protocol):
    """Read-only view of the backlog store, as guard consumers need it.

    ``in_progress_items`` and ``claim_info`` back ``guard_rails.evaluate()``;
    ``ready_items`` is candidate 10's surface (the herdr queue facade) and
    has no consumer yet. Lookup by ``claim_info`` is by slug only -- the
    item's ``id`` field; numeric dashboard positions are a dev_status.py CLI
    concern and are not resolvable here."""

    def in_progress_items(self) -> list[BacklogItem]: ...

    def ready_items(self, prefix: str | None = None) -> list[BacklogItem]: ...

    def claim_info(self, slug: str) -> ClaimInfo | None: ...


class LocalClaimLookup:
    """Read-only snapshot view over the backlog store: the one item-reading
    implementation (moved here from guard_rails.py) until candidate 6 lands
    as the shared read facade.

    The first read captures the snapshot; every method serves it for the
    instance's lifetime, so one ``evaluate()`` call -- or both of
    ``guard_rails.main()``'s calls on the same instance -- sees a single
    atomic backlog state and costs exactly one store read. A fresh view is
    a fresh ``LocalClaimLookup``; the guard process is short-lived, so
    nothing caches across runs. Consumers wanting fresh data construct a
    new instance.

    ``ready_items`` is deliberately NOT the transitive READY walk
    (``dev_status_impl._render_order``): it returns open-status items only,
    blocker-blind. No current consumer calls it; the blocker-graph
    semantics arrive with the queue-facade candidate, and the walk lives in
    dev_status.py -- do not build readiness decisions on this method.
    """

    def __init__(self, store_path: Path | None = None) -> None:
        self._store_path = store_path
        self._snapshot: list[dict] | None = None
        self._loaded = False

    def _items(self) -> list[dict]:
        """The instance's snapshot, loading it on first use.

        An unreadable store snapshots as the empty list: the guard's
        fail-open posture (unreadable storage allows) expressed as a list,
        so no later method can re-decide and flip the verdict."""
        if not self._loaded:
            path = (
                self._store_path
                if self._store_path is not None
                else backlog_items_path()
            )
            self._snapshot = self._read_store(path)
            self._loaded = True
        return self._snapshot or []

    @staticmethod
    def _read_store(path: Path) -> list[dict] | None:
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        items = data.get("items", []) if isinstance(data, dict) else data
        if not isinstance(items, list):
            return None
        return [i for i in items if isinstance(i, dict)]

    def in_progress_items(self) -> list[dict]:
        """In-progress items from the snapshot; [] when unreadable."""
        return [i for i in self._items() if i.get("status") == "in-progress"]

    def ready_items(self, prefix: str | None = None) -> list[dict]:
        """Open-status items from the snapshot (blocker-blind; see class
        docstring), prefix-filtered when a prefix is given."""
        items = [i for i in self._items() if i.get("status") == "open"]
        if prefix is not None:
            items = [i for i in items if str(i.get("id", "")).startswith(prefix)]
        return items

    def claim_info(self, slug: str) -> ClaimInfo | None:
        """The pointed item's claim, from the snapshot."""
        for item in self._items():
            if item.get("id") == slug:
                return ClaimInfo.from_dict(item.get("claimed_by"))
        return None
