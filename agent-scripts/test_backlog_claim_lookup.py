#!/usr/bin/env python3
"""Unit tests for backlog_claim_lookup.py -- ClaimInfo coercion from raw
store dicts, the snapshot semantics (one atomic read per lookup instance),
fail-open on unreadable stores, and protocol conformance. Git is not
touched here; the guard's end-to-end behaviour lives in the guard_rails
test files."""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from backlog_claim_lookup import (  # noqa: E402
    BacklogClaimLookup,
    ClaimInfo,
    LocalClaimLookup,
)


def _store(path: Path, items: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"items": items}))


def _item(slug: str, status: str = "in-progress", claim: dict | None = None) -> dict:
    return {"id": slug, "status": status, "claimed_by": claim}


class FromDictTests(unittest.TestCase):
    def test_full_claim_round_trips(self) -> None:
        claim = {
            "harness": "pi",
            "machine_id": "abc-123",
            "pid": 100,
            "owner_pid": 200,
            "pid_namespace": "ns",
            "ancestors": [],
            "claimed_at": "2026-01-01T00:00:00Z",
            "last_active": "2026-01-02T00:00:00Z",
        }
        info = ClaimInfo.from_dict(claim)
        assert info == ClaimInfo(
            machine_id="abc-123",
            owner_pid=200,
            pid=100,
            last_active="2026-01-02T00:00:00Z",
            claimed_at="2026-01-01T00:00:00Z",
        )

    def test_digit_string_pids_coerce(self) -> None:
        info = ClaimInfo.from_dict(
            {"machine_id": "m", "pid": "100", "owner_pid": "200"}
        )
        assert info is not None
        assert info.pid == 100
        assert info.owner_pid == 200

    def test_malformed_pid_fields_fall_back_to_zero(self) -> None:
        for bad in (None, "", "abc", ["1"], {"p": 1}, 1.5, True):
            with self.subTest(bad=bad):
                info = ClaimInfo.from_dict({"machine_id": "m", "owner_pid": bad})
                assert info is not None
                assert info.owner_pid == 0

    def test_missing_or_null_machine_id_is_empty_string(self) -> None:
        # Never str(None) -> "None": a claim without machine_id must read as
        # a mismatch against any real machine, exactly as the dict-level
        # str(claim.get("machine_id", "")) check did for the missing case.
        for claim in ({}, {"machine_id": None}, {"machine_id": 7}):
            with self.subTest(claim=claim):
                info = ClaimInfo.from_dict(claim)
                assert info is not None
                assert info.machine_id == ""

    def test_null_or_non_string_timestamps_are_none(self) -> None:
        info = ClaimInfo.from_dict(
            {"machine_id": "m", "last_active": None, "claimed_at": 5}
        )
        assert info == ClaimInfo(machine_id="m", owner_pid=0, pid=0)

    def test_empty_timestamp_strings_are_none(self) -> None:
        info = ClaimInfo.from_dict({"machine_id": "m", "last_active": ""})
        assert info is not None
        assert info.last_active is None

    def test_non_dict_claims_are_none(self) -> None:
        for claim in (None, "x", 5, ["a"], True):
            with self.subTest(claim=claim):
                assert ClaimInfo.from_dict(claim) is None


class LocalClaimLookupTests(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "items.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_in_progress_filter(self) -> None:
        _store(
            self.path,
            [
                _item("a"),
                _item("b", status="open"),
                _item("c", status="done"),
            ],
        )
        lookup = LocalClaimLookup(store_path=self.path)
        assert [i["id"] for i in lookup.in_progress_items()] == ["a"]

    def test_ready_items_filter_and_prefix(self) -> None:
        _store(
            self.path,
            [
                _item("atk-a", status="open"),
                _item("atk-b", status="open"),
                _item("meta-c", status="open"),
                _item("atk-d", status="in-progress"),
            ],
        )
        lookup = LocalClaimLookup(store_path=self.path)
        assert [i["id"] for i in lookup.ready_items()] == ["atk-a", "atk-b", "meta-c"]
        assert [i["id"] for i in lookup.ready_items(prefix="atk-")] == [
            "atk-a",
            "atk-b",
        ]

    def test_claim_info_hit_and_miss(self) -> None:
        _store(
            self.path,
            [
                _item("claimed", claim={"machine_id": "m", "owner_pid": 1}),
                _item("unclaimed", claim=None),
            ],
        )
        lookup = LocalClaimLookup(store_path=self.path)
        info = lookup.claim_info("claimed")
        assert info == ClaimInfo(machine_id="m", owner_pid=1, pid=0)
        assert lookup.claim_info("unclaimed") is None
        assert lookup.claim_info("absent") is None

    def test_unreadable_store_fails_open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not json")
        lookup = LocalClaimLookup(store_path=self.path)
        assert lookup.in_progress_items() == []
        assert lookup.ready_items() == []
        assert lookup.claim_info("a") is None

    def test_single_snapshot_survives_later_corruption(self) -> None:
        """TOCTOU regression: a store that goes bad after the first read
        must not flip a resolved claim into a deny-shaped None. Written to
        fail (red) against a per-method re-read implementation."""
        _store(
            self.path,
            [_item("a", claim={"machine_id": "m", "owner_pid": 1})],
        )
        lookup = LocalClaimLookup(store_path=self.path)
        assert lookup.in_progress_items()  # first read captures the snapshot
        self.path.write_text("{now corrupt")
        assert lookup.in_progress_items()  # still the good snapshot
        info = lookup.claim_info("a")
        assert info is not None
        assert info.owner_pid == 1

    def test_snapshot_covers_both_of_mains_calls(self) -> None:
        """Two evaluate() calls on one instance see one store state: a store
        rewritten between them must not change the second call's verdict
        inputs."""
        _store(self.path, [_item("a", claim={"machine_id": "m"})])
        lookup = LocalClaimLookup(store_path=self.path)
        first = lookup.in_progress_items()
        _store(self.path, [_item("a", status="done")])
        second = lookup.in_progress_items()
        assert first == second

    def test_constructor_injection_avoids_env(self) -> None:
        _store(self.path, [_item("a")])
        lookup = LocalClaimLookup(store_path=self.path)
        # No GUARD_RAILS_STORE involvement: the injected path is used as-is.
        assert [i["id"] for i in lookup.in_progress_items()] == ["a"]


class ProtocolConformanceTests(unittest.TestCase):
    def test_local_lookup_satisfies_protocol(self) -> None:
        assert isinstance(LocalClaimLookup(), BacklogClaimLookup)

    def test_protocol_members(self) -> None:
        for name in ("in_progress_items", "ready_items", "claim_info"):
            assert hasattr(BacklogClaimLookup, name), name


if __name__ == "__main__":
    unittest.main()
