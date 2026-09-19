#!/usr/bin/env python3
"""Tests for dev_status_formatting.py. Run with: python3 test_dev_status_formatting.py

Direct unit coverage for a pure module that previously had none of its own
-- every function here was only exercised indirectly through
test_dev_status.py's CLI-output assertions. Added 2026-09-18 alongside
test_dev_status_storage.py's split, closing the gap flagged when that
split landed.
"""

import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent-scripts"))
import test_bootstrap  # noqa: E402

import dev_status_formatting as fmt  # noqa: E402


class SectionRenderingTestCase(unittest.TestCase):
    def test_section_top_pads_to_width(self):
        line = fmt.section_top("RECAP", 20)
        self.assertTrue(line.startswith("┌─ RECAP "))
        self.assertEqual(len(line), 20)

    def test_section_top_floors_at_three_dashes_when_title_overflows_width(self):
        line = fmt.section_top("A very long title that overflows", 10)
        self.assertTrue(line.endswith("───"))

    def test_section_bottom(self):
        self.assertEqual(fmt.section_bottom(5), "└────")

    def test_ellipsize_passes_through_text_at_or_under_limit(self):
        self.assertEqual(fmt.ellipsize("short", 10), "short")
        self.assertEqual(fmt.ellipsize("exact", 5), "exact")

    def test_ellipsize_truncates_with_trailing_ellipsis(self):
        self.assertEqual(fmt.ellipsize("a long string", 5), "a lo…")

    def test_ellipsize_limit_of_one_keeps_one_char(self):
        self.assertEqual(fmt.ellipsize("hello", 1), "h…")

    def test_project_prefix_exact_match(self):
        self.assertEqual(fmt.project_prefix("atk", ["atk", "pi"]), "atk")

    def test_project_prefix_dash_suffixed_match(self):
        self.assertEqual(fmt.project_prefix("atk-123", ["atk", "pi"]), "atk")

    def test_project_prefix_unknown_but_hyphenated_falls_back_to_generic_split(self):
        self.assertEqual(fmt.project_prefix("meta-9", ["atk", "pi"]), "meta")

    def test_project_prefix_unknown_and_unhyphenated_is_empty(self):
        self.assertEqual(fmt.project_prefix("standalone", ["atk", "pi"]), "")

    def test_project_divider_singular_item(self):
        line = fmt.project_divider("atk", 1, 30)
        self.assertIn("(1 item)", line)

    def test_project_divider_plural_items(self):
        line = fmt.project_divider("atk", 3, 30)
        self.assertIn("(3 items)", line)

    def test_project_divider_floors_at_three_dashes(self):
        line = fmt.project_divider("a-very-long-project-name", 3, 10)
        self.assertTrue(line.endswith("───"))

    def test_format_age_under_a_minute_floors_to_one_minute(self):
        self.assertEqual(fmt.format_age(5), "1m")

    def test_format_age_minutes(self):
        self.assertEqual(fmt.format_age(45 * 60), "45m")

    def test_format_age_hours(self):
        self.assertEqual(fmt.format_age(3 * 3600), "3h")

    def test_format_age_just_under_an_hour_is_still_minutes(self):
        self.assertEqual(fmt.format_age(3599), "59m")


class ChangelogAndFactsTestCase(unittest.TestCase):
    @staticmethod
    def _parse(ts):
        return datetime(2026, 1, 1, 12, 30) if ts else None

    def test_render_changelog_skips_diagnostic_entries(self):
        entries = [{"diagnostic": True, "cmd": "lock-wait", "ts": "x"}]
        self.assertEqual(fmt.render_changelog(entries, self._parse), "")

    def test_render_changelog_skips_completion_transitions(self):
        entries = [{"to_status": "done", "cmd": "done", "ts": "x"}]
        self.assertEqual(fmt.render_changelog(entries, self._parse), "")

    def test_render_changelog_renders_slug_when_no_summary(self):
        entries = [{"cmd": "start", "slug": "atk-1", "ts": "x"}]
        self.assertEqual(fmt.render_changelog(entries, self._parse), "[12:30] start atk-1")

    def test_render_changelog_prefers_summary_over_slug(self):
        entries = [
            {"cmd": "start", "slug": "atk-1", "summary": "Do the thing", "ts": "x"}
        ]
        line = fmt.render_changelog(entries, self._parse)
        self.assertEqual(line, "[12:30] start — Do the thing")

    def test_render_changelog_renders_status_transition_detail(self):
        entries = [
            {"cmd": "review", "from_status": "in-progress", "to_status": "review", "ts": "x"}
        ]
        line = fmt.render_changelog(entries, self._parse)
        self.assertIn("(in-progress→review)", line)

    def test_render_changelog_renders_fields_feedback_and_count(self):
        entries = [
            {
                "cmd": "update",
                "fields": ["priority", "context"],
                "feedback": "needs work",
                "count": 2,
                "ts": "x",
            }
        ]
        line = fmt.render_changelog(entries, self._parse)
        self.assertIn("changed: priority, context", line)
        self.assertIn("feedback: needs work", line)
        self.assertIn("2 item(s)", line)

    def test_render_changelog_missing_timestamp_renders_placeholder(self):
        entries = [{"cmd": "start", "slug": "atk-1"}]
        line = fmt.render_changelog(entries, self._parse)
        self.assertTrue(line.startswith("[??:??]"))

    def test_render_changelog_joins_multiple_entries_with_newlines(self):
        entries = [
            {"cmd": "start", "slug": "atk-1", "ts": "x"},
            {"cmd": "block", "slug": "atk-2", "ts": "x"},
        ]
        lines = fmt.render_changelog(entries, self._parse).splitlines()
        self.assertEqual(len(lines), 2)

    def test_render_done_facts_with_stamp(self):
        stamp = datetime(2026, 3, 4)
        items = [{"summary": "Ship it"}]
        line = fmt.render_done_facts(items, lambda _item: stamp)
        self.assertEqual(line, "- Ship it (completed 2026-03-04)")

    def test_render_done_facts_without_stamp(self):
        items = [{"summary": "Ship it"}]
        line = fmt.render_done_facts(items, lambda _item: None)
        self.assertEqual(line, "- Ship it (completed unknown date)")

    def test_render_done_facts_missing_summary_renders_empty(self):
        items = [{}]
        line = fmt.render_done_facts(items, lambda _item: None)
        self.assertEqual(line, "-  (completed unknown date)")

    def test_bucket_summary_format(self):
        summary = fmt.bucket_summary(
            in_progress=1, ready=2, blocked=3, in_review=4, done=5, pending=6
        )
        self.assertEqual(
            summary,
            "in progress: 1, ready: 2, blocked: 3, in review: 4, "
            "done (latest 5 max): 5, pending: 6",
        )


class RecapPromptTestCase(unittest.TestCase):
    def test_build_recap_prompt_substitutes_all_three_facts(self):
        prompt = fmt.build_recap_prompt("changelog line", "bucket line", "done line")
        self.assertIn("changelog line", prompt)
        self.assertIn("bucket line", prompt)
        self.assertIn("done line", prompt)

    def test_build_recap_prompt_empty_changelog_and_completed_use_none_placeholder(self):
        prompt = fmt.build_recap_prompt("", "bucket line", "")
        self.assertIn("(none)", prompt)

    def test_build_recap_prompt_honors_custom_template(self):
        prompt = fmt.build_recap_prompt(
            "c", "b", "d", template="{changelog}|{buckets}|{completed}"
        )
        self.assertEqual(prompt, "c|b|d")


class RecapTextNormalizationTestCase(unittest.TestCase):
    def test_abbrev_boundary_matches_known_abbreviation(self):
        text = "See e.g. the docs"
        # the dot after "g" -- the one after "e" belongs to a shorter,
        # non-matching token ("e") since the walk-back stops at the space.
        self.assertTrue(fmt.recap_is_abbrev_boundary(text, text.index("g.") + 1))

    def test_abbrev_boundary_matches_single_uppercase_initial(self):
        text = "Talk to J. Smith today"
        dot = text.index("J.") + 1
        self.assertTrue(fmt.recap_is_abbrev_boundary(text, dot))

    def test_abbrev_boundary_false_for_ordinary_sentence_end(self):
        text = "This is done. Next sentence"
        dot = text.index(".")
        self.assertFalse(fmt.recap_is_abbrev_boundary(text, dot))

    def test_last_sentence_cut_finds_boundary_within_budget(self):
        text = "First sentence. Second sentence."
        cut = fmt.recap_last_sentence_cut(text, budget=15, min_keep=5)
        self.assertEqual(text[:cut], "First sentence.")

    def test_last_sentence_cut_returns_the_last_boundary_within_budget(self):
        text = "First sentence. Second sentence. Third sentence."
        cut = fmt.recap_last_sentence_cut(text, budget=34, min_keep=5)
        self.assertEqual(text[:cut], "First sentence. Second sentence.")

    def test_last_sentence_cut_skips_abbreviation_dots(self):
        text = "See e.g. this. More text follows here."
        cut = fmt.recap_last_sentence_cut(text, budget=len(text), min_keep=5)
        self.assertEqual(text[:cut], "See e.g. this.")

    def test_last_sentence_cut_respects_min_keep_floor(self):
        text = "Hi. More text after that goes on for a while."
        cut = fmt.recap_last_sentence_cut(text, budget=len(text), min_keep=20)
        self.assertIsNone(cut)

    def test_last_sentence_cut_none_when_no_boundary_in_budget(self):
        text = "No punctuation anywhere in this string at all"
        cut = fmt.recap_last_sentence_cut(text, budget=len(text), min_keep=1)
        self.assertIsNone(cut)

    def test_normalize_recap_text_strips_emoji_and_markdown(self):
        raw = "**Great work** today \U0001f389 done!"
        normalized = fmt.normalize_recap_text(raw, max_chars=100, min_keep=1)
        self.assertNotIn("*", normalized)
        self.assertNotIn("\U0001f389", normalized)

    def test_normalize_recap_text_collapses_whitespace(self):
        raw = "Line one\n\n\nLine   two"
        normalized = fmt.normalize_recap_text(raw, max_chars=100, min_keep=1)
        self.assertEqual(normalized, "Line one Line two")

    def test_normalize_recap_text_short_text_passes_through(self):
        raw = "Short and sweet."
        self.assertEqual(fmt.normalize_recap_text(raw, max_chars=100, min_keep=1), raw)

    def test_normalize_recap_text_cuts_at_sentence_boundary_when_over_budget(self):
        raw = "First sentence here. Second sentence that pushes well past budget."
        normalized = fmt.normalize_recap_text(raw, max_chars=25, min_keep=5)
        self.assertEqual(normalized, "First sentence here.")

    def test_normalize_recap_text_falls_back_to_hard_truncate_with_ellipsis(self):
        raw = "onelongwordwithnosentenceboundaryatallwhatsoever"
        normalized = fmt.normalize_recap_text(raw, max_chars=10, min_keep=1)
        self.assertEqual(normalized, raw[:10].rstrip() + "…")


if __name__ == "__main__":
    test_bootstrap.run_unittest_main()
