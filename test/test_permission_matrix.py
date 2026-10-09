#!/usr/bin/env python3
"""Tests for permission_matrix.py — the shared bash-permission policy.

Run with: python3 test/test_permission_matrix.py or uv run pytest test/test_permission_matrix.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent-scripts"))
import test_bootstrap  # noqa: E402,F401
import permission_matrix as pm  # noqa: E402
from permission_matrix import (  # noqa: E402
    PatternException,
    Rule,
    Tier,
    TokenException,
)

GOOD_REASON = "a real capability difference, written out in full"


def _rules(*patterns: str, tier: Tier = Tier.ALLOW) -> tuple[Rule, ...]:
    return tuple(Rule(p, tier, "g") for p in patterns)


class MatrixIsSoundTests(unittest.TestCase):
    def test_shipped_matrix_validates_clean(self) -> None:
        self.assertEqual(pm.validate(), [])

    def test_targets_map_to_registered_harnesses(self) -> None:
        import harness_spec

        self.assertLessEqual(set(pm.TARGETS.values()), set(harness_spec.HARNESSES))


class GrammarTests(unittest.TestCase):
    def test_question_mark_is_rejected(self) -> None:
        self.assertTrue(pm.validate(_rules("ls ?"), ()))

    def test_legacy_colon_star_is_rejected(self) -> None:
        self.assertTrue(pm.validate(_rules("git log:*"), ()))

    def test_surrounding_whitespace_is_rejected(self) -> None:
        self.assertTrue(pm.validate(_rules(" ls *"), ()))
        self.assertTrue(pm.validate(_rules("ls * "), ()))

    def test_dir_token_only_after_leading_git_c(self) -> None:
        self.assertEqual(pm.pattern_violations("git -C <dir> log*"), [])
        self.assertTrue(pm.pattern_violations("ls <dir>"))
        self.assertTrue(pm.pattern_violations("git log <dir>"))
        self.assertTrue(pm.pattern_violations("git -C <dir> -C <dir> log*"))

    def test_trailing_space_star_must_be_sole_wildcard(self) -> None:
        self.assertTrue(pm.pattern_violations("git * main *"))
        self.assertEqual(pm.pattern_violations("git -C <dir> branch --list *"), [])

    def test_redundant_bare_rule_is_rejected(self) -> None:
        problems = pm.validate(_rules("head *", "head"), ())
        self.assertTrue(any("redundant bare" in p for p in problems), problems)

    def test_duplicate_rule_is_rejected(self) -> None:
        problems = pm.validate(_rules("ls*", "ls*"), ())
        self.assertTrue(any("duplicate" in p for p in problems), problems)


class ExceptionTests(unittest.TestCase):
    def test_exception_without_reason_fails(self) -> None:
        for reason in ("", "   ", "too short"):
            problems = pm.validate(
                _rules("ls*"), (PatternException("pi", "ls*", reason),)
            )
            self.assertTrue(any("written reason" in p for p in problems), problems)

    def test_shipped_exceptions_all_carry_reasons(self) -> None:
        for exc in pm.EXCEPTIONS:
            self.assertGreaterEqual(len(exc.reason.strip()), pm.MIN_REASON_LEN, exc)

    def test_exception_on_ask_or_deny_is_rejected(self) -> None:
        rules = _rules("git commit*", tier=Tier.ASK) + _rules(
            "rm *", tier=Tier.DENY
        )
        for pattern in ("git commit*", "rm *"):
            problems = pm.validate(
                rules, (PatternException("pi", pattern, GOOD_REASON),)
            )
            self.assertTrue(any("only remove allows" in p for p in problems), problems)

    def test_unknown_target_is_rejected(self) -> None:
        problems = pm.validate(
            _rules("ls*"), (PatternException("emacs", "ls*", GOOD_REASON),)
        )
        self.assertTrue(any("unknown target" in p for p in problems), problems)

    def test_unknown_token_is_rejected(self) -> None:
        problems = pm.validate(_rules("ls*"), (TokenException("pi", "<x>", GOOD_REASON),))
        self.assertTrue(any("unknown token" in p for p in problems), problems)

    def test_duplicate_exception_is_rejected(self) -> None:
        exc = PatternException("pi", "ls*", GOOD_REASON)
        problems = pm.validate(_rules("ls*"), (exc, exc))
        self.assertTrue(any("duplicate" in p for p in problems), problems)

    def test_dir_allow_needs_token_exception_on_glob_targets(self) -> None:
        problems = pm.validate(_rules("git -C <dir> log*"), ())
        for target in ("claude", "claude-work", "opencode"):
            self.assertTrue(any(repr(target) in p for p in problems), problems)
        self.assertFalse(any("'pi'" in p for p in problems), problems)


class RulesForTests(unittest.TestCase):
    def test_exceptions_only_remove_allows(self) -> None:
        for target in pm.TARGETS:
            got = pm.rules_for(target)
            all_ask = tuple(r.pattern for r in pm.RULES if r.tier is Tier.ASK)
            all_deny = tuple(r.pattern for r in pm.RULES if r.tier is Tier.DENY)
            self.assertEqual(got[Tier.ASK], all_ask, target)
            self.assertEqual(got[Tier.DENY], all_deny, target)
            self.assertLessEqual(set(got[Tier.ALLOW]), pm.shared_allow())

    def test_git_commit_is_ask_everywhere(self) -> None:
        for target in pm.TARGETS:
            ask = pm.rules_for(target)[Tier.ASK]
            self.assertIn("git commit*", ask, target)
            self.assertIn("git -C <dir> commit*", ask, target)

    def test_glob_targets_get_no_dir_allows(self) -> None:
        for target in ("claude", "claude-work", "opencode"):
            allow = pm.rules_for(target)[Tier.ALLOW]
            self.assertFalse([p for p in allow if "<dir>" in p], target)
        self.assertTrue([p for p in pm.rules_for("pi")[Tier.ALLOW] if "<dir>" in p])

    def test_pi_omits_native_tool_scripts(self) -> None:
        allow = pm.rules_for("pi")[Tier.ALLOW]
        for script in ("dev_status.py", "grill.py", "second_opinion.py", "vitals_promotion.py"):
            self.assertFalse([p for p in allow if script in p], script)

    def test_per_target_counts(self) -> None:
        counts = {
            t: tuple(len(pm.rules_for(t)[tier]) for tier in Tier) for t in pm.TARGETS
        }
        self.assertEqual(len(pm.shared_allow()), 93)
        self.assertEqual(counts["claude"], (74, 9, 1))
        self.assertEqual(counts["claude-work"], (74, 9, 1))
        self.assertEqual(counts["opencode"], (74, 9, 1))
        self.assertEqual(counts["pi"], (86, 9, 1))

    def test_work_profile_matches_personal(self) -> None:
        self.assertEqual(pm.rules_for("claude"), pm.rules_for("claude-work"))

    def test_dropped_bypass_shapes_stay_dropped(self) -> None:
        shared = pm.shared_allow()
        for pattern in (
            "find *",
            "sed -n *",
            "DEVSTATUS_AGENT=1 python3 *",
            "git checkout*",
            "git branch*",
            "git worktree*",
        ):
            self.assertNotIn(pattern, shared)


if __name__ == "__main__":
    test_bootstrap.run_unittest_main(verbosity=2)
