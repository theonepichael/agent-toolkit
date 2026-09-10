#!/usr/bin/env python3
"""Pure read-only facade over the dev_status backlog store.

This module is deliberately not a replacement for the ``dev_status.py`` CLI
read commands. ``cmd_show``, ``cmd_list`` and ``cmd_render`` perform a stale
claim sweep that may bump the revision and save the store; these helpers do
not. They expose the stored snapshot exactly as read by
``dev_status_storage.load_items()`` and never sweep, journal, bump revisions,
or save.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import dev_status_storage
from backlog_claim_lookup import BacklogClaimLookup, ClaimInfo
from dev_status_storage import BacklogItem


@dataclass(frozen=True)
class BacklogQuery:
    """Optional filters for facade item queries."""

    status: str | None = None
    prefix: str | None = None


@dataclass(frozen=True)
class BacklogSnapshot:
    """In-memory read-only view over one backlog item snapshot.

    Use this when several reads must observe the same item list. The standalone
    module functions below are convenience one-shot reads and intentionally load
    a fresh snapshot for each call.
    """

    items: Sequence[BacklogItem]

    @classmethod
    def from_path(cls, items_path: Path | None = None) -> BacklogSnapshot:
        """Load one snapshot through :func:`dev_status_storage.load_items`."""
        return cls(tuple(dev_status_storage.load_items(items_path)))

    def get_item(self, slug_or_id: str) -> BacklogItem | None:
        """Return the item whose stored ``id`` exactly matches ``slug_or_id``.

        Numeric dashboard positions are not resolved here; those are CLI display
        semantics that also depend on pending-item order.
        """
        for item in self.items:
            if item.get("id") == slug_or_id:
                return item
        return None

    def ready_items(self, query: BacklogQuery | None = None) -> list[BacklogItem]:
        """Return open items with no unresolved blockers in snapshot order.

        A blocker is resolved exactly when it names an existing backlog item with
        ``status == "done"``. Missing blockers and non-done blockers are
        unresolved, so cyclic blockers naturally keep every cycle member out of
        READY. ``query.status`` is an additional exact status filter; because
        READY items are open by definition, any non-``open`` status yields an
        empty result.
        """
        active_query = query or BacklogQuery()
        if active_query.status is not None and active_query.status != "open":
            return []

        index = _build_index(self.items)
        result: list[BacklogItem] = []
        for item in self.items:
            if item.get("status") != "open":
                continue
            if active_query.prefix is not None and not item.get("id", "").startswith(
                active_query.prefix
            ):
                continue
            if _effective_blockers(item, index):
                continue
            result.append(item)
        return result

    def in_progress_items(self) -> list[BacklogItem]:
        """Return in-progress items from this snapshot."""
        return [item for item in self.items if item.get("status") == "in-progress"]

    def item_status(self, slug_or_id: str) -> str | None:
        """Return an item's stored status, or ``None`` when unknown."""
        item = self.get_item(slug_or_id)
        if item is None:
            return None
        status = item.get("status")
        return status if isinstance(status, str) else None

    def claim_info(self, slug_or_id: str) -> ClaimInfo | None:
        """Return the item's stored claim coerced to :class:`ClaimInfo`."""
        item = self.get_item(slug_or_id)
        if item is None:
            return None
        return ClaimInfo.from_dict(item.get("claimed_by"))


def get_item(slug_or_id: str, *, items_path: Path | None = None) -> BacklogItem | None:
    """Load a fresh snapshot and return one exact stored-id match."""
    return BacklogSnapshot.from_path(items_path).get_item(slug_or_id)


def ready_items(
    query: BacklogQuery | None = None, *, items_path: Path | None = None
) -> list[BacklogItem]:
    """Load a fresh snapshot and return its transitive READY items."""
    return BacklogSnapshot.from_path(items_path).ready_items(query)


def in_progress_items(*, items_path: Path | None = None) -> list[BacklogItem]:
    """Load a fresh snapshot and return its in-progress items."""
    return BacklogSnapshot.from_path(items_path).in_progress_items()


def item_status(slug_or_id: str, *, items_path: Path | None = None) -> str | None:
    """Load a fresh snapshot and return one item's status."""
    return BacklogSnapshot.from_path(items_path).item_status(slug_or_id)


def claim_info(slug_or_id: str, *, items_path: Path | None = None) -> ClaimInfo | None:
    """Load a fresh snapshot and return one item's claim info."""
    return BacklogSnapshot.from_path(items_path).claim_info(slug_or_id)


class DevStatusClaimLookup:
    """``BacklogClaimLookup`` implementation over one dev_status snapshot."""

    def __init__(self, items_path: Path | None = None) -> None:
        self._snapshot = BacklogSnapshot.from_path(items_path)

    def ready_items(self, prefix: str | None = None) -> list[BacklogItem]:
        """Return transitive READY items, optionally prefix-filtered."""
        return self._snapshot.ready_items(BacklogQuery(prefix=prefix))

    def in_progress_items(self) -> list[BacklogItem]:
        """Return in-progress items from this instance's snapshot."""
        return self._snapshot.in_progress_items()

    def claim_info(self, slug: str) -> ClaimInfo | None:
        """Return one item's claim info from this instance's snapshot."""
        return self._snapshot.claim_info(slug)


# Static protocol check without instantiating against the real default store.
_claim_lookup_protocol_check: type[BacklogClaimLookup] = DevStatusClaimLookup


def _build_index(items: Sequence[BacklogItem]) -> dict[str, BacklogItem]:
    """Build a slug-to-item lookup for pure blocker checks."""
    return {item["id"]: item for item in items}


def _effective_blockers(item: BacklogItem, index: dict[str, BacklogItem]) -> list[str]:
    """Return blockers that keep ``item`` out of READY.

    Mirrors ``dev_status_impl.effective_blockers`` for READY semantics:
    existing DONE blockers are satisfied; missing and non-DONE blockers remain
    unresolved. A malformed non-list ``blocked_by`` is treated as empty, matching
    the dashboard helper's coercion without printing from this pure facade.
    """
    blocked_by = item.get("blocked_by", [])
    if not isinstance(blocked_by, list):
        return []

    result: list[str] = []
    for slug in blocked_by:
        dep = index.get(slug)
        if dep is None or dep.get("status") != "done":
            result.append(str(slug))
    return result
