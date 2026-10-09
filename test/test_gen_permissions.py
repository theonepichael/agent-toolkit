#!/usr/bin/env python3
"""Tests for gen_permissions.py — compiling the permission matrix into seeds.

Run with: python3 test/test_gen_permissions.py or uv run pytest test/test_gen_permissions.py
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent-scripts"))
import test_bootstrap  # noqa: E402,F401
from pytest_shim import pytest  # noqa: E402
import gen_hooks  # noqa: E402
import gen_permissions as gp  # noqa: E402
import permission_matrix as pm  # noqa: E402
import settings_seed  # noqa: E402
from permission_matrix import Tier  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
CORPUS = json.loads(
    (REPO_ROOT / "test" / "fixtures" / "permission_corpus.json").read_text(
        encoding="utf-8"
    )
)["cases"]
TARGET_FILES = (
    "claude/settings.json",
    "claude/settings.work.json",
    "opencode/opencode.jsonc",
    "pi/extensions/permission-gate.ts",
)
# gen_hooks.compile_hooks reads these too, so a scratch root needs them.
HOOK_FILES = (
    "copilot/hooks/pre-tool-use.json",
    "copilot/hooks/post-tool-use.json",
    "copilot/hooks/session-start.json",
    "copilot/hooks/agent-stop.json",
    "agy/hooks.json",
)


def _scratch_root() -> Path:
    root = Path(tempfile.mkdtemp(prefix="gen-permissions-"))
    for rel in TARGET_FILES + HOOK_FILES:
        dest = root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / rel, dest)
    return root


def _run(argv: list[str]) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = gp.main(argv)
    return code, out.getvalue(), err.getvalue()


def _compiled_json(rel: str) -> dict[str, object]:
    return json.loads(gp.compile_permissions(REPO_ROOT)[REPO_ROOT / rel])


class CompileTests(unittest.TestCase):
    def test_compiles_all_four_targets(self) -> None:
        outputs = gp.compile_permissions(REPO_ROOT)
        self.assertEqual(
            {p.relative_to(REPO_ROOT).as_posix() for p in outputs}, set(TARGET_FILES)
        )

    def test_committed_tree_is_current(self) -> None:
        for path, content in gp.compile_permissions(REPO_ROOT).items():
            self.assertEqual(path.read_text(encoding="utf-8"), content, path)

    def test_claude_rules_match_matrix(self) -> None:
        for rel, target in gp.CLAUDE_TARGETS.items():
            perms = _compiled_json(rel)["permissions"]
            neutral = pm.rules_for(target)
            self.assertEqual(
                perms["allow"], [f"Bash({p})" for p in neutral[Tier.ALLOW]], rel
            )
            self.assertEqual(len(perms["allow"]), 74)
            self.assertEqual(len(perms["ask"]), 18)
            self.assertEqual(len(perms["deny"]), 2)
            self.assertIn("Bash(git commit*)", perms["ask"])
            self.assertIn("Bash(git -C * commit*)", perms["ask"])
            self.assertIn("Bash(git difftool*)", perms["ask"])
            self.assertIn("Bash(sort -o *)", perms["ask"])
            self.assertFalse([r for r in perms["allow"] if "-C *" in r], rel)
            self.assertFalse([r for r in perms["allow"] if ":*" in r], rel)

    def test_opencode_bash_order_and_counts(self) -> None:
        bash = _compiled_json("opencode/opencode.jsonc")["permission"]["bash"]
        items = list(bash.items())
        self.assertEqual(items[0], ("*", "ask"))
        verdicts = [v for _, v in items[1:]]
        # allows, then asks, then denies, so last-match-wins gives deny>ask>allow
        self.assertEqual(verdicts, sorted(verdicts, key=["allow", "ask", "deny"].index))
        self.assertEqual(verdicts.count("allow"), 74)
        self.assertEqual(verdicts.count("ask"), 18)
        self.assertEqual(verdicts.count("deny"), 2)
        self.assertEqual(bash["git commit*"], "ask")
        self.assertEqual(bash["git -C * commit*"], "ask")
        self.assertEqual(bash["git difftool*"], "ask")
        self.assertEqual(bash["sort -o *"], "ask")
        self.assertFalse([k for k, v in bash.items() if v == "allow" and "-C *" in k])

    def test_non_permission_keys_untouched(self) -> None:
        for rel in ("claude/settings.json", "claude/settings.work.json"):
            before = json.loads((REPO_ROOT / rel).read_text(encoding="utf-8"))
            after = _compiled_json(rel)
            self.assertEqual(list(before), list(after), rel)
            for key in before:
                if key != "permissions":
                    self.assertEqual(before[key], after[key], f"{rel}:{key}")
        before = json.loads((REPO_ROOT / "opencode/opencode.jsonc").read_text("utf-8"))
        after = _compiled_json("opencode/opencode.jsonc")
        self.assertEqual(
            before["permission"]["external_directory"],
            after["permission"]["external_directory"],
        )
        for key in before:
            if key != "permission":
                self.assertEqual(before[key], after[key], key)

    def test_pi_region_holds_matrix_lists(self) -> None:
        text = (REPO_ROOT / "pi/extensions/permission-gate.ts").read_text("utf-8")
        region = text.split(gp.ANCHOR_BEGIN, 1)[1].split(gp.ANCHOR_END, 1)[0]
        neutral = pm.rules_for("pi")
        for pattern in neutral[Tier.ALLOW] + neutral[Tier.ASK] + neutral[Tier.DENY]:
            self.assertIn(json.dumps(pattern), region)
        self.assertNotIn("dev_status.py *", region)

    def test_bypass_patterns_never_allowed(self) -> None:
        bash = _compiled_json("opencode/opencode.jsonc")["permission"]["bash"]
        allowed = {k for k, v in bash.items() if v == "allow"}
        self.assertFalse(allowed & set(settings_seed._BYPASS_BASH_PATTERNS))
        for pattern in ("DEVSTATUS_AGENT=1 python3 *", "sed -n *", "find *"):
            self.assertIn(pattern, settings_seed._BYPASS_BASH_PATTERNS)


class CorpusTests(unittest.TestCase):
    """The same corpus pi/test/permission-gate.test.ts runs through classify()."""

    def test_claude_rules_verdicts(self) -> None:
        for rel in gp.CLAUDE_TARGETS:
            perms = _compiled_json(rel)["permissions"]
            for case in CORPUS:
                with self.subTest(target=rel, command=case["command"]):
                    self.assertEqual(
                        gp.claude_verdict(perms, case["command"]), case["claude"]
                    )

    def test_opencode_verdicts(self) -> None:
        bash = _compiled_json("opencode/opencode.jsonc")["permission"]["bash"]
        for case in CORPUS:
            with self.subTest(command=case["command"]):
                self.assertEqual(
                    gp.opencode_verdict(bash, case["command"]), case["opencode"]
                )

    def test_opencode_matcher_transcription(self) -> None:
        """Wildcard.match as read from the opencode binary on 2026-09-30."""
        self.assertTrue(gp.opencode_match("ls", "ls *"))
        self.assertTrue(gp.opencode_match("ls -la", "ls *"))
        self.assertFalse(gp.opencode_match("lsof", "ls *"))
        self.assertTrue(gp.opencode_match("lsof", "ls*"))
        self.assertTrue(gp.opencode_match("lx", "l?"))
        self.assertTrue(gp.opencode_match("a\nb", "a*"))

    def test_claude_matcher_trailing_space_star(self) -> None:
        """Docs: a trailing ' *' matches the bare command only as the sole wildcard."""
        self.assertTrue(gp.claude_match("ls", "ls *"))
        self.assertFalse(gp.claude_match("lsof", "ls *"))
        self.assertTrue(gp.claude_match("lsof", "ls*"))
        self.assertTrue(gp.claude_match("npm --help x", "* --help *"))
        self.assertFalse(gp.claude_match("npm --help", "* --help *"))


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = _scratch_root()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_check_exits_0_on_current_tree(self) -> None:
        code, _, err = _run(["--check", "--repo-root", str(REPO_ROOT)])
        self.assertEqual(code, 0, err)

    def test_check_exits_1_on_each_stale_target(self) -> None:
        for rel in TARGET_FILES:
            with self.subTest(rel=rel):
                path = self.root / rel
                original = path.read_text(encoding="utf-8")
                if rel.endswith(".ts"):
                    stale = original.replace('"pwd",', '"pwdx",', 1)
                else:
                    stale = original.replace('"ask"', '"allow"', 1).replace(
                        "git commit*", "git commitx*", 1
                    )
                self.assertNotEqual(stale, original)
                path.write_text(stale, encoding="utf-8")
                code, _, err = _run(["--check", "--repo-root", str(self.root)])
                self.assertEqual(code, 1)
                self.assertIn(rel, err)
                path.write_text(original, encoding="utf-8")

    def test_write_then_check_is_idempotent(self) -> None:
        path = self.root / "claude/settings.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["permissions"]["allow"] = []
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        self.assertEqual(_run(["--repo-root", str(self.root)])[0], 0)
        self.assertEqual(_run(["--check", "--repo-root", str(self.root)])[0], 0)
        self.assertEqual(_run(["--repo-root", str(self.root)])[0], 0)
        self.assertEqual(_run(["--check", "--repo-root", str(self.root)])[0], 0)

    def test_composes_with_gen_hooks_in_both_orders(self) -> None:
        claude = [self.root / rel for rel in gp.CLAUDE_TARGETS]
        # gen_permissions first, then gen_hooks output must be unchanged
        _run(["--repo-root", str(self.root)])
        for path, content in gen_hooks.compile_hooks(self.root).items():
            if path in claude:
                self.assertEqual(path.read_text(encoding="utf-8"), content, path)
        # gen_hooks writing first must leave gen_permissions --check green
        for path, content in gen_hooks.compile_hooks(self.root).items():
            path.write_text(content, encoding="utf-8")
        self.assertEqual(_run(["--check", "--repo-root", str(self.root)])[0], 0)

    def test_missing_anchor_exits_2_without_writing(self) -> None:
        pi = self.root / "pi/extensions/permission-gate.ts"
        pi.write_text(
            pi.read_text(encoding="utf-8").replace(gp.ANCHOR_END, "// gone"),
            encoding="utf-8",
        )
        settings = self.root / "claude/settings.json"
        settings.write_text(
            settings.read_text(encoding="utf-8").replace("git commit*", "x"),
            encoding="utf-8",
        )
        before = settings.read_text(encoding="utf-8")
        code, _, err = _run(["--repo-root", str(self.root)])
        self.assertEqual(code, 2)
        self.assertIn("permission-gate.ts", err)
        self.assertEqual(settings.read_text(encoding="utf-8"), before)

    def test_duplicate_anchor_exits_2(self) -> None:
        pi = self.root / "pi/extensions/permission-gate.ts"
        pi.write_text(
            pi.read_text(encoding="utf-8") + "\n" + gp.ANCHOR_BEGIN + "\n",
            encoding="utf-8",
        )
        self.assertEqual(_run(["--check", "--repo-root", str(self.root)])[0], 2)

    def test_jsonc_comment_exits_2(self) -> None:
        oc = self.root / "opencode/opencode.jsonc"
        oc.write_text("// note\n" + oc.read_text(encoding="utf-8"), encoding="utf-8")
        code, _, err = _run(["--repo-root", str(self.root)])
        self.assertEqual(code, 2)
        self.assertIn("opencode.jsonc", err)

    def test_stdout_writes_nothing(self) -> None:
        settings = self.root / "claude/settings.json"
        settings.write_text("{}\n", encoding="utf-8")
        code, out, _ = _run(["--stdout", "--repo-root", str(self.root)])
        self.assertEqual(code, 0)
        self.assertIn("claude/settings.json", out)
        self.assertEqual(settings.read_text(encoding="utf-8"), "{}\n")


class AuditLiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp(prefix="gen-permissions-home-"))
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        outputs = gp.compile_permissions(REPO_ROOT)
        self.claude_live = self.home / ".claude" / "settings.json"
        self.opencode_live = self.home / ".config" / "opencode" / "opencode.jsonc"
        self.claude_live.parent.mkdir(parents=True)
        self.opencode_live.parent.mkdir(parents=True)
        self.claude_live.write_text(outputs[REPO_ROOT / "claude/settings.json"])
        self.opencode_live.write_text(outputs[REPO_ROOT / "opencode/opencode.jsonc"])
        self.local = self.home / ".claude" / "settings.local.json"
        self.local.write_text('{"permissions": {"allow": ["Bash(rm *)"]}}\n')

    def _audit(self) -> tuple[int, str]:
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(out):
            code = gp.main(
                ["--audit-live", "--repo-root", str(REPO_ROOT), "--home", str(self.home)]
            )
        return code, out.getvalue()

    def test_matching_live_files_exit_0(self) -> None:
        code, out = self._audit()
        self.assertEqual(code, 0, out)

    def test_live_only_allow_is_reported(self) -> None:
        data = json.loads(self.opencode_live.read_text())
        data["permission"]["bash"]["DEVSTATUS_AGENT=1 python3 *"] = "allow"
        self.opencode_live.write_text(json.dumps(data, indent=2))
        code, out = self._audit()
        self.assertEqual(code, 1)
        self.assertIn("DEVSTATUS_AGENT=1 python3 *", out)

    def test_wrong_order_is_reported(self) -> None:
        data = json.loads(self.opencode_live.read_text())
        bash = data["permission"]["bash"]
        commit = bash.pop("git commit*")
        data["permission"]["bash"] = {"git commit*": commit, **bash}
        self.opencode_live.write_text(json.dumps(data, indent=2))
        code, out = self._audit()
        self.assertEqual(code, 1)
        self.assertIn("order", out)

    def test_claude_extra_and_missing_reported(self) -> None:
        data = json.loads(self.claude_live.read_text())
        data["permissions"]["allow"].append("Bash(git push *)")
        data["permissions"]["deny"] = []
        self.claude_live.write_text(json.dumps(data, indent=2))
        code, out = self._audit()
        self.assertEqual(code, 1)
        self.assertIn("Bash(git push *)", out)
        self.assertIn("prune", out)

    @pytest.mark.regression(
        "audit-live-reports-clean-on-wrong-typed-permissions",
        "AssertionError: 0 != 1",
    )
    def test_wrong_typed_permission_sections_are_drift(self) -> None:
        data = json.loads(self.opencode_live.read_text())
        data["permission"]["bash"] = []
        self.opencode_live.write_text(json.dumps(data, indent=2))
        code, out = self._audit()
        self.assertEqual(code, 1, out)
        self.opencode_live.write_text(
            gp.compile_permissions(REPO_ROOT)[REPO_ROOT / "opencode/opencode.jsonc"]
        )
        data = json.loads(self.claude_live.read_text())
        data["permissions"] = ["Bash(rm *)"]
        self.claude_live.write_text(json.dumps(data, indent=2))
        code, out = self._audit()
        self.assertEqual(code, 1, out)

    @pytest.mark.regression(
        "audit-live-crashes-on-non-string-tier-entries",
        "TypeError: cannot use 'dict' as a set element (unhashable type: 'dict')",
    )
    def test_malformed_tier_entries_are_drift_not_crash(self) -> None:
        # A wrongly-typed element *inside* a tier list (dict instead of
        # string) must be reported as drift, not blow up the read-only
        # audit with an unhashable-type TypeError.
        data = json.loads(self.claude_live.read_text())
        data["permissions"]["allow"] = [{"rule": "Bash(ls *)"}]
        self.claude_live.write_text(json.dumps(data, indent=2))
        code, out = self._audit()
        self.assertEqual(code, 1, out)
        self.assertIn("non-string", out)

    def test_work_profile_compares_against_work_seed(self) -> None:
        marker = self.home / ".local" / "state" / "agent-toolkit" / "profile"
        marker.parent.mkdir(parents=True)
        marker.write_text("work\n")
        code, out = self._audit()
        self.assertEqual(code, 0, out)
        self.assertIn("claude-work", out)

    def test_missing_live_file_is_skipped(self) -> None:
        self.opencode_live.unlink()
        code, out = self._audit()
        self.assertEqual(code, 0, out)
        self.assertIn("skipped", out)

    def test_audit_never_writes_and_ignores_settings_local(self) -> None:
        stamps = {
            p: (p.read_bytes(), p.stat().st_mtime_ns)
            for p in (self.claude_live, self.opencode_live, self.local)
        }
        self._audit()
        for path, stamp in stamps.items():
            self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), stamp)


if __name__ == "__main__":
    test_bootstrap.run_unittest_main(verbosity=2)
