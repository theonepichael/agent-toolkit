#!/usr/bin/env python3
"""Tests for agent-scripts/guard_rails_report.py."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent-scripts"))
import test_bootstrap  # noqa: E402

import agent_toolkit_paths  # noqa: E402
import guard_rails_report  # noqa: E402


def _record(**fields: object) -> str:
    """One audit line, defaulting every field to a benign allow."""
    base: dict[str, object] = {
        "ts": "2026-01-01T00:00:00+00:00",
        "harness": "claude",
        "tool": "bash",
        "target": "git status",
        "rule": "default-allow",
        "decision": "allow",
    }
    base.update(fields)
    return json.dumps(base)


class ReportTestCase(unittest.TestCase):
    """Base with a throwaway toolkit home and a helper to write the audit log."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "toolkit"
        env = patch.dict(os.environ, {"AGENT_TOOLKIT_HOME": str(self.root)})
        env.start()
        self.addCleanup(env.stop)
        agent_toolkit_paths.DEFAULT_RESOLVER.invalidate()
        self.addCleanup(agent_toolkit_paths.DEFAULT_RESOLVER.invalidate)
        self.log = self.root / "data" / "guard_rails_audit.jsonl"

    def write(self, lines: list[str]) -> None:
        self.log.parent.mkdir(parents=True, exist_ok=True)
        self.log.write_text("".join(line + "\n" for line in lines))

    def report(self, *argv: str) -> dict:
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            code = guard_rails_report.main(["--json", *argv])
        self.assertEqual(code, 0)
        return json.loads(out.getvalue())


class CountsTests(ReportTestCase):
    def test_counts_by_harness_verdict_and_rule(self) -> None:
        self.write(
            [
                _record(),
                _record(),
                _record(decision="deny", rule="no-verify"),
                _record(harness="pi", rule="hookspath-mutation", decision="deny"),
                _record(harness=""),
            ]
        )
        report = self.report()
        self.assertEqual(report["records_read"], 5)
        self.assertEqual(report["malformed"], 0)
        self.assertEqual(report["decisions"], {"allow": 3, "deny": 2})
        self.assertEqual(
            report["harnesses"],
            [
                {"harness": "claude", "total": 3},
                {"harness": "", "total": 1},
                {"harness": "pi", "total": 1},
            ],
        )
        self.assertEqual(
            report["rules"],
            [
                {"rule": "default-allow", "total": 3},
                {"rule": "hookspath-mutation", "total": 1},
                {"rule": "no-verify", "total": 1},
            ],
        )
        # Text mode is the default output, and must render the same tree.
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            self.assertEqual(guard_rails_report.main([]), 0)
        self.assertIn("(unattributed)=1", out.getvalue())

    def test_malformed_lines_are_skipped(self) -> None:
        self.write([_record(), "not json", "[1, 2, 3]"])
        report = self.report()
        self.assertEqual(report["records_read"], 1)
        self.assertEqual(report["malformed"], 2)
        self.assertEqual(report["decisions"], {"allow": 1, "deny": 0})


class StemTests(ReportTestCase):
    def test_stems_drop_assignments_and_wrappers(self) -> None:
        self.write(
            [
                _record(
                    decision="deny", rule="no-verify", target="FOO=1 sudo git status"
                ),
                _record(
                    decision="deny",
                    rule="hookspath-shell-substitution",
                    target="env python3 script.py",
                ),
                _record(decision="deny", rule="rm-rf", target="echo hi"),
                _record(tool="write", target="/tmp/file.py", decision="deny"),
                _record(decision="allow", target="cat file"),
            ]
        )
        report = self.report()
        self.assertEqual(
            report["denied_stems"],
            [{"stem": "echo", "denials": 1}, {"stem": "git", "denials": 1},
             {"stem": "python3", "denials": 1}],
        )


class CandidateTests(ReportTestCase):
    def test_threshold_controls_candidacy(self) -> None:
        self.write(
            [
                _record(decision="deny", rule="no-verify", target="git status"),
                _record(decision="deny", rule="no-verify", target="git diff"),
                _record(decision="deny", rule="no-verify", target="echo hi"),
            ]
        )
        at_two = self.report("--threshold", "2")
        self.assertEqual(at_two["candidates"], [{"stem": "git", "denials": 2}])
        at_three = self.report("--threshold", "3")
        self.assertEqual(at_three["candidates"], [])
        # The stem table is unconditional; only candidacy is threshold-gated.
        self.assertEqual(
            [row["stem"] for row in at_three["denied_stems"]], ["git", "echo"]
        )


class ReadOnlyTests(ReportTestCase):
    def test_report_does_not_modify_the_log(self) -> None:
        self.write([_record(), _record(decision="deny", rule="rm-rf")])
        before = self.log.read_bytes()
        before_mtime = self.log.stat().st_mtime_ns
        self.report()
        self.assertEqual(self.log.read_bytes(), before)
        self.assertEqual(self.log.stat().st_mtime_ns, before_mtime)


class MissingLogTests(ReportTestCase):
    def test_missing_log_is_an_empty_report(self) -> None:
        report = self.report()
        self.assertEqual(report["records_read"], 0)
        self.assertEqual(report["decisions"], {"allow": 0, "deny": 0})
        self.assertEqual(report["denied_stems"], [])


if __name__ == "__main__":
    test_bootstrap.run_unittest_main()
