#!/usr/bin/env python3
"""Parity between migrate_toolkit_home's journal reader and retained_legacy_links.

The drift hook delegates its retained-link lookup to
agent-scripts/retained_legacy_links so it need not import the migration
toolchain. That module re-reads the same append-only journals with its own
copy of the parser primitives (read_records / _split_valid / _steps_in /
_outcome), because the migrator keeps its own reader for its other uses and
the two must not be deduped while a parallel change lands. Two parsers of one
format drift silently, so this test pins them together: for a corpus of
journal shapes, migrate_toolkit_home's reader and the new module must produce
the same records, steps, outcome, and retained set. The pre-change inline
retained_legacy_links logic is reconstructed here (not imported) so a change
to either reader breaks this test instead of the machine's session-start
check.
"""

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent-scripts"))
import test_bootstrap  # noqa: E402

import migrate_toolkit_home as mth  # noqa: E402
import retained_legacy_links as rll  # noqa: E402

DEST = "/x/legacy-link.py"
TARGET = "/y/new-link.py"


def _rec(seq: int, phase: str, event: str, detail: object, mid: str = "mig-p") -> dict[str, object]:
    return {"seq": seq, "ts": "t", "id": mid, "phase": phase, "event": event, "detail": detail}


def _links_with_retained(mid: str = "mig-p") -> list[dict[str, object]]:
    return [
        _rec(1, "links", "begin", {"planned": [{"dest": DEST, "target": TARGET}]}, mid),
        _rec(
            2,
            "links",
            "done",
            {"created": 1, "retained": [{"dest": DEST, "target": TARGET}]},
            mid,
        ),
        _rec(3, "run", "end", {"outcome": "committed"}, mid),
    ]


def _finalize(outcome: str, *, mode: str | None = None, mid: str = "mig-p") -> list[dict[str, object]]:
    return [
        _rec(1, "finalize", "begin", ({"mode": mode} if mode else {}), mid),
        _rec(2, "finalize", "done", {}, mid),
        _rec(3, "run", "end", {"outcome": outcome}, mid),
    ]


class JournalParityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="parity-"))
        self.state = self.tmp / "state"
        self.state.mkdir()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _mig_dir(self, mid: str) -> Path:
        directory = self.state / "migrations" / mid
        directory.mkdir(parents=True)
        return directory

    def _write(self, directory: Path, name: str, content: object) -> None:
        text = (
            content
            if isinstance(content, str)
            else "".join(json.dumps(record) + "\n" for record in content)
        )
        (directory / name).write_text(text)

    def _old_retained(self, installer_state: Path) -> set[Path]:
        # Reconstructed from the pre-change inline retained_legacy_links in
        # migrate_toolkit_home.py (release-1), so it tracks the migrator's
        # reader, not the new module.
        kept: set[Path] = set()
        for directory in mth._journal_dirs(installer_state):
            finalized = mth.read_records(directory, mth.FINALIZE_NAME)
            if mth._outcome(finalized) == mth.OUTCOME_FINALIZED:
                begin = next(
                    (
                        r.get("detail")
                        for r in finalized
                        if r.get("phase") == "finalize" and r.get("event") == "begin"
                    ),
                    None,
                )
                if not isinstance(begin, dict) or begin.get("mode") != "restored-copy":
                    continue
            for record in mth._steps_in(mth.read_records(directory)):
                done = mth._done(record)
                if record["phase"] != "links" or done is None:
                    continue
                for link in done.get("retained", []):  # type: ignore[attr-defined]
                    kept.add(Path(str(link["dest"])))
        return kept

    def _assert_parse_parity(self, directory: Path, name: str) -> None:
        try:
            mth.read_records(directory, name)
        except mth.JournalError:
            mth_raised = True
        else:
            mth_raised = False
        try:
            rll._read_records(directory, name)
        except rll.RetainedLinksError:
            rll_raised = True
        else:
            rll_raised = False
        self.assertEqual(mth_raised, rll_raised, f"raise mismatch on {name}")
        if not mth_raised:
            self.assertEqual(
                mth.read_records(directory, name),
                rll._read_records(directory, name),
                f"records differ on {name}",
            )

    def _assert_steps_parity(self, directory: Path) -> None:
        self.assertEqual(
            mth._steps_in(mth.read_records(directory)),
            rll._steps_in(rll._read_records(directory)),
        )

    def _assert_outcome_parity(self, directory: Path) -> None:
        self.assertEqual(
            mth._outcome(mth.read_records(directory, mth.FINALIZE_NAME)),
            rll._outcome(rll._read_records(directory, rll.FINALIZE_NAME)),
        )

    def _check_shape(
        self,
        mid: str,
        *,
        journal: object,
        finalize: object = None,
        expect_raise: bool = False,
    ) -> None:
        directory = self._mig_dir(mid)
        self._write(directory, "journal.jsonl", journal)
        if finalize is not None:
            self._write(directory, "finalize.jsonl", finalize)
        self._assert_parse_parity(directory, "journal.jsonl")
        self._assert_parse_parity(directory, "finalize.jsonl")
        if not expect_raise:
            self._assert_steps_parity(directory)
            self._assert_outcome_parity(directory)
        # Retained-set parity: old inline logic vs new module.
        if expect_raise:
            with self.assertRaises(mth.JournalError):
                self._old_retained(self.state)
            with self.assertRaises(rll.RetainedLinksError):
                rll.retained_legacy_links(self.state)
        else:
            self.assertEqual(
                self._old_retained(self.state),
                rll.retained_legacy_links(self.state),
                f"retained set mismatch for {mid}",
            )

    def test_clean_with_retained(self) -> None:
        self._check_shape("mig-clean", journal=_links_with_retained())

    def test_clean_without_retained(self) -> None:
        journal = [
            _rec(1, "links", "begin", {"planned": []}),
            _rec(2, "links", "done", {"created": 0, "retained": []}),
            _rec(3, "run", "end", {"outcome": "committed"}),
        ]
        self._check_shape("mig-clean2", journal=journal)

    def test_torn_final_line(self) -> None:
        text = (
            json.dumps(
                _rec(1, "links", "begin", {"planned": [{"dest": DEST, "target": TARGET}]})
            )
            + "\n"
            + json.dumps(
                _rec(
                    2,
                    "links",
                    "done",
                    {"created": 1, "retained": [{"dest": DEST, "target": TARGET}]},
                )
            )
            + "\n"
            + json.dumps(_rec(3, "run", "end", {"outcome": "committed"}))
            + "\ngarbage-on-the-last-line\n"
        )
        self._check_shape("mig-torn", journal=text)

    def test_corrupt_earlier_line(self) -> None:
        text = (
            "garbage-in-the-middle\n"
            + json.dumps(
                _rec(
                    2,
                    "links",
                    "done",
                    {"created": 1, "retained": [{"dest": DEST, "target": TARGET}]},
                )
            )
            + "\n"
            + json.dumps(_rec(3, "run", "end", {"outcome": "committed"}))
            + "\n"
        )
        self._check_shape("mig-corrupt", journal=text, expect_raise=True)

    def test_blank_lines(self) -> None:
        # A blank line anywhere but the final torn tail is a malformed journal
        # line to both readers: they must agree on rejecting it.
        text = "\n" + "\n".join(json.dumps(r) for r in _links_with_retained()) + "\n"
        self._check_shape("mig-blank", journal=text, expect_raise=True)

    def test_finalize_normal_mode(self) -> None:
        # A normal finalize removes the retained links: not exempted.
        self._check_shape(
            "mig-fn", journal=_links_with_retained(), finalize=_finalize("finalized")
        )

    def test_finalize_restored_copy_mode(self) -> None:
        # A restored-copy finalize keeps them: still exempted.
        self._check_shape(
            "mig-fr",
            journal=_links_with_retained(),
            finalize=_finalize("finalized", mode="restored-copy"),
        )

    def test_rollback(self) -> None:
        journal = _links_with_retained() + [
            _rec(4, "recover", "begin", {}),
            _rec(5, "rollback", "begin", {}),
            _rec(6, "rollback", "done", {}),
            _rec(7, "run", "end", {"outcome": "rolled-back"}),
        ]
        self._check_shape("mig-rb", journal=journal)

    def test_abandoned(self) -> None:
        journal = _links_with_retained() + [
            _rec(4, "recover", "begin", {}),
            _rec(5, "recover", "abandoned", {}),
        ]
        self._check_shape("mig-ab", journal=journal)


if __name__ == "__main__":
    test_bootstrap.run_unittest_main()
