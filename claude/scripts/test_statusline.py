#!/usr/bin/env python3
"""Tests for statusline.py. Run with: python3 test_statusline.py

statusline.py is a Claude Code status line script: reads a JSON session
payload on stdin and renders one ANSI-color-coded line. These tests cover
the pure render helper with payload dicts, the 5-hour quota countdown
segment, the right-alignment combine step, and one subprocess test per
end-to-end contract (stdin -> stdout, with and without ``COLUMNS``).
main() is intentionally never called directly — it ends in sys.exit(0)
for gen_interfaces' AST extraction, so calling it in-process would raise
SystemExit.
"""

import json
import os
import re
import subprocess
import sys
import time
import unittest
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import statusline  # noqa: E402

GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
RESET = "\033[0m"


def payload(pct: float | None = None, model: str | None = "Opus") -> dict:
    return {
        "model": {"display_name": model},
        "context_window": {"used_percentage": pct},
    }


def rate_limit_payload(used_percentage: object, resets_at: object) -> dict:
    return {
        "model": {"display_name": "Opus"},
        "context_window": {"used_percentage": 50.0},
        "rate_limits": {
            "five_hour": {
                "used_percentage": used_percentage,
                "resets_at": resets_at,
            },
        },
    }


_NOW = 1_000_000.0
_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


class RenderTestCase(unittest.TestCase):
    """Exact-string rendering, thresholds, and prefix/bar behavior."""

    def test_42_percent_green_four_blocks(self) -> None:
        self.assertEqual(
            statusline._render(payload(42.0)),
            f"[Opus] {GREEN}▓▓▓▓░░░░░░{RESET} 42%",
        )

    def test_85_percent_yellow_eight_blocks(self) -> None:
        self.assertEqual(
            statusline._render(payload(85.0)),
            f"[Opus] {YELLOW}▓▓▓▓▓▓▓▓░░{RESET} 85%",
        )

    def test_95_percent_red_nine_blocks(self) -> None:
        self.assertEqual(
            statusline._render(payload(95.0)),
            f"[Opus] {RED}▓▓▓▓▓▓▓▓▓░{RESET} 95%",
        )

    def test_70_0_is_yellow(self) -> None:
        self.assertIn(YELLOW, statusline._render(payload(70.0)))

    def test_90_0_is_red(self) -> None:
        self.assertIn(RED, statusline._render(payload(90.0)))

    def test_zero_percent_green_empty_bar(self) -> None:
        self.assertEqual(
            statusline._render(payload(0.0)),
            f"[Opus] {GREEN}░░░░░░░░░░{RESET} 0%",
        )

    def test_100_percent_red_full_bar(self) -> None:
        self.assertEqual(
            statusline._render(payload(100.0)),
            f"[Opus] {RED}▓▓▓▓▓▓▓▓▓▓{RESET} 100%",
        )

    def test_no_full_bar_before_100_percent(self) -> None:
        """99% renders a 9-block bar (capped), never the full 10 — the bar
        never reads as 100% before the account actually is. round-with-cap
        keeps the label ``99%`` and the fill at 9 cells."""
        self.assertEqual(
            statusline._render(payload(99.0)),
            f"[Opus] {RED}▓▓▓▓▓▓▓▓▓░{RESET} 99%",
        )

    def test_rounding_display(self) -> None:
        self.assertEqual(
            statusline._render(payload(42.3)),
            f"[Opus] {GREEN}▓▓▓▓░░░░░░{RESET} 42%",
        )

    def test_69_6_rounds_up_to_yellow_seven_blocks(self) -> None:
        """69.6 displays ``70%`` and must color yellow to match that label,
        with a 7-cell fill agreeing with the rounded number, not the green
        color and 6-cell fill the old raw-float threshold produced."""
        self.assertEqual(
            statusline._render(payload(69.6)),
            f"[Opus] {YELLOW}▓▓▓▓▓▓▓░░░{RESET} 70%",
        )

    def test_89_6_rounds_up_to_red_nine_blocks(self) -> None:
        self.assertEqual(
            statusline._render(payload(89.6)),
            f"[Opus] {RED}▓▓▓▓▓▓▓▓▓░{RESET} 90%",
        )

    def test_no_model_prefix_when_name_absent(self) -> None:
        self.assertEqual(
            statusline._render(payload(50.0, model=None)),
            f"{GREEN}▓▓▓▓▓░░░░░{RESET} 50%",
        )

    def test_model_blank_string_drops_prefix(self) -> None:
        self.assertEqual(
            statusline._render(payload(50.0, model="  ")),
            f"{GREEN}▓▓▓▓▓░░░░░{RESET} 50%",
        )

    def test_model_non_string_drops_prefix(self) -> None:
        for bad in (42, ["Opus"], {"name": "Opus"}, True):
            self.assertEqual(
                statusline._render(payload(50.0, model=bad)),
                f"{GREEN}▓▓▓▓▓░░░░░{RESET} 50%",
            )


class DegradationTestCase(unittest.TestCase):
    """Defensive input handling: absent/null/wrong-typed fields."""

    def test_missing_context_window_zero_percent(self) -> None:
        self.assertEqual(
            statusline._render({"model": {"display_name": "Opus"}}),
            f"[Opus] {GREEN}░░░░░░░░░░{RESET} 0%",
        )

    def test_null_used_percentage_zero_percent(self) -> None:
        self.assertEqual(
            statusline._render(payload(None)),
            f"[Opus] {GREEN}░░░░░░░░░░{RESET} 0%",
        )

    def test_context_window_non_dict(self) -> None:
        for bad in (None, "ctx", [], 42):
            self.assertEqual(
                statusline._render(
                    {"model": {"display_name": "Opus"}, "context_window": bad}
                ),
                f"[Opus] {GREEN}░░░░░░░░░░{RESET} 0%",
            )

    def test_model_non_dict(self) -> None:
        for bad in (None, "Opus", [], 42):
            self.assertEqual(
                statusline._render(
                    {"model": bad, "context_window": {"used_percentage": 50.0}}
                ),
                f"{GREEN}▓▓▓▓▓░░░░░{RESET} 50%",
            )

    def test_nan_treated_as_zero(self) -> None:
        self.assertEqual(
            statusline._render(payload(float("nan"))),
            f"[Opus] {GREEN}░░░░░░░░░░{RESET} 0%",
        )

    def test_inf_treated_as_zero(self) -> None:
        self.assertEqual(
            statusline._render(payload(float("inf"))),
            f"[Opus] {GREEN}░░░░░░░░░░{RESET} 0%",
        )

    def test_negative_clamped_to_zero(self) -> None:
        self.assertEqual(
            statusline._render(payload(-5.0)),
            f"[Opus] {GREEN}░░░░░░░░░░{RESET} 0%",
        )

    def test_over_100_clamped(self) -> None:
        self.assertEqual(
            statusline._render(payload(250.0)),
            f"[Opus] {RED}▓▓▓▓▓▓▓▓▓▓{RESET} 100%",
        )

    def test_numeric_string_accepted(self) -> None:
        self.assertEqual(
            statusline._render(payload("42")),
            f"[Opus] {GREEN}▓▓▓▓░░░░░░{RESET} 42%",
        )

    def test_bool_rejected_as_zero(self) -> None:
        for bad in (True, False):
            self.assertEqual(
                statusline._render(payload(bad)),
                f"[Opus] {GREEN}░░░░░░░░░░{RESET} 0%",
            )

    def test_non_numeric_string_rejected(self) -> None:
        for bad in ("", "abc"):
            self.assertEqual(
                statusline._render(payload(bad)),
                f"[Opus] {GREEN}░░░░░░░░░░{RESET} 0%",
            )

    def test_non_numeric_other_types_rejected(self) -> None:
        for bad in ([], {}, None):
            self.assertEqual(
                statusline._render(payload(bad)),
                f"[Opus] {GREEN}░░░░░░░░░░{RESET} 0%",
            )


class FiveHourSegmentTestCase(unittest.TestCase):
    """The right-side 5-hour quota countdown segment: color, countdown,
    and round()-from-displayed thresholds, all driven by an injected
    ``now`` so countdown assertions are exact."""

    def test_mid_range_green_with_hours_minutes_countdown(self) -> None:
        resets_at = _NOW + 7980.0
        self.assertEqual(
            statusline._render_rate_limit(
                rate_limit_payload(23.0, resets_at), now=_NOW
            ),
            f"5h: {GREEN}23%{RESET} (resets in 2h13m)",
        )

    def test_mid_range_green_with_minutes_only_countdown(self) -> None:
        resets_at = _NOW + 600.0
        self.assertEqual(
            statusline._render_rate_limit(
                rate_limit_payload(23.0, resets_at), now=_NOW
            ),
            f"5h: {GREEN}23%{RESET} (resets in 10m)",
        )

    def test_75_percent_yellow(self) -> None:
        resets_at = _NOW + 7980.0
        self.assertEqual(
            statusline._render_rate_limit(
                rate_limit_payload(75.0, resets_at), now=_NOW
            ),
            f"5h: {YELLOW}75%{RESET} (resets in 2h13m)",
        )

    def test_95_percent_red(self) -> None:
        resets_at = _NOW + 7980.0
        self.assertEqual(
            statusline._render_rate_limit(
                rate_limit_payload(95.0, resets_at), now=_NOW
            ),
            f"5h: {RED}95%{RESET} (resets in 2h13m)",
        )

    def test_69_5_rounds_to_yellow(self) -> None:
        """round(69.5) == 70 (banker's: 70 is even) -> yellow, ``70%``."""
        resets_at = _NOW + 7980.0
        self.assertEqual(
            statusline._render_rate_limit(
                rate_limit_payload(69.5, resets_at), now=_NOW
            ),
            f"5h: {YELLOW}70%{RESET} (resets in 2h13m)",
        )

    def test_89_5_rounds_to_red(self) -> None:
        """round(89.5) == 90 (banker's: 90 is even) -> red, ``90%``."""
        resets_at = _NOW + 7980.0
        self.assertEqual(
            statusline._render_rate_limit(
                rate_limit_payload(89.5, resets_at), now=_NOW
            ),
            f"5h: {RED}90%{RESET} (resets in 2h13m)",
        )

    def test_resets_at_in_past_renders_zero_minutes(self) -> None:
        resets_at = _NOW - 5.0
        self.assertEqual(
            statusline._render_rate_limit(
                rate_limit_payload(23.0, resets_at), now=_NOW
            ),
            f"5h: {GREEN}23%{RESET} (resets in 0m)",
        )


class CountdownFormatTestCase(unittest.TestCase):
    """_format_countdown: Hh/Mm formatting with floor semantics — never
    rounds a minute up, never goes negative."""

    def test_two_hours_thirteen_minutes(self) -> None:
        self.assertEqual(statusline._format_countdown(7980.0), "2h13m")

    def test_just_under_an_hour_renders_minutes(self) -> None:
        self.assertEqual(statusline._format_countdown(3599.9), "59m")

    def test_exactly_one_hour(self) -> None:
        self.assertEqual(statusline._format_countdown(3600.0), "1h0m")

    def test_zero_seconds(self) -> None:
        self.assertEqual(statusline._format_countdown(0.0), "0m")

    def test_negative_floors_to_zero(self) -> None:
        self.assertEqual(statusline._format_countdown(-5.0), "0m")


class RateLimitDegradationTestCase(unittest.TestCase):
    """5h segment degrades to an empty string (so the line is left-only)
    when any required field is absent, wrong-typed, or non-finite."""

    def test_rate_limits_absent(self) -> None:
        p = {
            "model": {"display_name": "Opus"},
            "context_window": {"used_percentage": 50.0},
        }
        self.assertEqual(statusline._render_rate_limit(p, now=_NOW), "")

    def test_rate_limits_non_dict(self) -> None:
        for bad in (None, "rl", [], 42):
            p = rate_limit_payload(23.0, _NOW + 7980.0)
            p["rate_limits"] = bad
            self.assertEqual(statusline._render_rate_limit(p, now=_NOW), "")

    def test_five_hour_absent(self) -> None:
        p = rate_limit_payload(23.0, _NOW + 7980.0)
        p["rate_limits"] = {}
        self.assertEqual(statusline._render_rate_limit(p, now=_NOW), "")

    def test_five_hour_non_dict(self) -> None:
        for bad in (None, "fh", [], 42):
            p = rate_limit_payload(23.0, _NOW + 7980.0)
            p["rate_limits"]["five_hour"] = bad
            self.assertEqual(statusline._render_rate_limit(p, now=_NOW), "")

    def test_used_percentage_missing(self) -> None:
        p = {
            "model": {"display_name": "Opus"},
            "context_window": {"used_percentage": 50.0},
            "rate_limits": {"five_hour": {"resets_at": _NOW + 7980.0}},
        }
        self.assertEqual(statusline._render_rate_limit(p, now=_NOW), "")

    def test_used_percentage_null(self) -> None:
        self.assertEqual(
            statusline._render_rate_limit(
                rate_limit_payload(None, _NOW + 7980.0), now=_NOW
            ),
            "",
        )

    def test_used_percentage_non_numeric(self) -> None:
        for bad in ("abc", [], {}):
            self.assertEqual(
                statusline._render_rate_limit(
                    rate_limit_payload(bad, _NOW + 7980.0), now=_NOW
                ),
                "",
            )

    def test_used_percentage_bool_rejected(self) -> None:
        for bad in (True, False):
            self.assertEqual(
                statusline._render_rate_limit(
                    rate_limit_payload(bad, _NOW + 7980.0), now=_NOW
                ),
                "",
            )

    def test_used_percentage_inf_rejected(self) -> None:
        self.assertEqual(
            statusline._render_rate_limit(
                rate_limit_payload(float("inf"), _NOW + 7980.0), now=_NOW
            ),
            "",
        )

    def test_resets_at_missing(self) -> None:
        p = {
            "model": {"display_name": "Opus"},
            "context_window": {"used_percentage": 50.0},
            "rate_limits": {"five_hour": {"used_percentage": 23.0}},
        }
        self.assertEqual(statusline._render_rate_limit(p, now=_NOW), "")

    def test_resets_at_null(self) -> None:
        self.assertEqual(
            statusline._render_rate_limit(rate_limit_payload(23.0, None), now=_NOW),
            "",
        )

    def test_resets_at_non_numeric(self) -> None:
        for bad in ("abc", [], {}):
            self.assertEqual(
                statusline._render_rate_limit(rate_limit_payload(23.0, bad), now=_NOW),
                "",
            )

    def test_resets_at_bool_rejected(self) -> None:
        self.assertEqual(
            statusline._render_rate_limit(rate_limit_payload(23.0, True), now=_NOW),
            "",
        )

    def test_resets_at_inf_rejected(self) -> None:
        self.assertEqual(
            statusline._render_rate_limit(rate_limit_payload(23.0, 1e999), now=_NOW),
            "",
        )

    def test_resets_at_nan_rejected(self) -> None:
        self.assertEqual(
            statusline._render_rate_limit(
                rate_limit_payload(23.0, float("nan")), now=_NOW
            ),
            "",
        )


class CombineSegmentsTestCase(unittest.TestCase):
    """_combine_segments: always a compact ``" | "``-joined line, never
    padded to any terminal width. Right-alignment via COLUMNS was removed
    after a production report: Claude Code reports the true pty COLUMNS
    (confirmed 285 in a real tmux/Windows Terminal session) but its own
    fullscreen TUI renders the status-line row narrower than that when a
    session-name badge is showing, silently truncating a padded line with
    an ellipsis. COLUMNS is not a reliable proxy for the actually
    renderable width, so this never pads."""

    def setUp(self) -> None:
        self.left = f"[Opus] {GREEN}▓▓▓▓░░░░░░{RESET} 42%"
        self.right = f"5h: {GREEN}23%{RESET} (resets in 2h13m)"

    def test_always_joins_with_separator(self) -> None:
        self.assertEqual(
            statusline._combine_segments(self.left, self.right),
            f"{self.left} | {self.right}",
        )

    def test_empty_right_returns_left_unchanged(self) -> None:
        self.assertEqual(statusline._combine_segments(self.left, ""), self.left)


@pytest.mark.allow_real_subprocess
class ProcessTestCase(unittest.TestCase):
    """Full stdin -> stdout contract through a real subprocess."""

    def test_pipe_payload_renders_line(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).parent / "statusline.py")],
            input=json.dumps(payload(42.0)),
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, f"[Opus] {GREEN}▓▓▓▓░░░░░░{RESET} 42%\n")
        self.assertEqual(proc.stderr, "")

    def test_non_json_stdin_empty_output_exit_zero(self) -> None:
        for raw in ("", "not json", "42", "null", "[]", "true"):
            proc = subprocess.run(
                [sys.executable, str(Path(__file__).parent / "statusline.py")],
                input=raw,
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout, "")
            self.assertEqual(proc.stderr, "")

    def test_non_dict_payload_empty_output_exit_zero(self) -> None:
        for raw in ("null", "[]", "true", "123"):
            proc = subprocess.run(
                [sys.executable, str(Path(__file__).parent / "statusline.py")],
                input=raw,
                capture_output=True,
                text=True,
            )
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stdout, "")
            self.assertEqual(proc.stderr, "")

    @staticmethod
    def _five_hour_stdin() -> str:
        return json.dumps(
            {
                "model": {"display_name": "Opus"},
                "context_window": {"used_percentage": 42.0},
                "rate_limits": {
                    "five_hour": {
                        "used_percentage": 23.0,
                        "resets_at": time.time() + 86400,
                    },
                },
            }
        )

    def test_columns_has_no_effect_on_output(self) -> None:
        """Regression test for the production truncation report: a wide,
        narrow, unset, or garbage COLUMNS value must all produce the exact
        same compact output — COLUMNS is read from the environment (it's
        still a real, documented field) but never used to pad or align."""
        outputs = {}
        for label, columns in (
            ("wide", "285"),
            ("narrow", "20"),
            ("garbage", "abc"),
            ("zero", "0"),
        ):
            proc = subprocess.run(
                [sys.executable, str(Path(__file__).parent / "statusline.py")],
                input=self._five_hour_stdin(),
                capture_output=True,
                text=True,
                env={**os.environ, "COLUMNS": columns},
            )
            self.assertEqual(proc.returncode, 0)
            self.assertEqual(proc.stderr, "")
            outputs[label] = proc.stdout

        env_unset = {**os.environ}
        env_unset.pop("COLUMNS", None)
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).parent / "statusline.py")],
            input=self._five_hour_stdin(),
            capture_output=True,
            text=True,
            env=env_unset,
        )
        outputs["unset"] = proc.stdout

        self.assertEqual(len(set(outputs.values())), 1, outputs)
        self.assertIn(" | ", outputs["wide"])
        self.assertRegex(strip_ansi(outputs["wide"]), r"5h: .*% \(resets in \d+h\d+m\)")

    def test_default_now_path_renders_well_formed_segment(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).parent / "statusline.py")],
            input=self._five_hour_stdin(),
            capture_output=True,
            text=True,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, "")
        self.assertRegex(strip_ansi(proc.stdout), r"5h: .*% \(resets in \d+h\d+m\)")


if __name__ == "__main__":
    unittest.main()
