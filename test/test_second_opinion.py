#!/usr/bin/env python3
"""Tests for second_opinion.py. Run with: python3 test_second_opinion.py

The generic backend-invocation engine (`_run_command`, `_kill_active_process`,
`run_backend_command`, `run_agy`/`run_copilot`/`run_opencode` argv-building,
the opencode JSON-event parsing helpers) lives in llm_backends.py now and is
covered by test_llm_backends.py. This file covers what's still
second_opinion-specific: prompt building, the adversary-agent `run_opencode`,
the CLI-level `cmd_review` fallback loop, and the thin wrappers that supply
second_opinion's own model/timeout/env-var choices to the shared engine.
"""

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent-scripts"))
import test_bootstrap  # noqa: E402
from pytest_shim import pytest
import test_layouts  # noqa: E402
import agent_toolkit_paths  # noqa: E402
import cli_common
import llm_backends  # noqa: E402
import second_opinion

# The eight model/pool variables that must be isolated from the host so tests
# never depend on a real SECOND_OPINION_* setting. Derived-independent of the
# implementation: enumerated here explicitly and asserted against the
# implementation's mapping so cleanup cannot silently hide a mapping omission.
EXPECTED_MODEL_ENV_VARS = [
    "SECOND_OPINION_CODEX_MODEL_POOL",
    "SECOND_OPINION_CODEX_MODEL",
    "SECOND_OPINION_AGY_MODEL_POOL",
    "SECOND_OPINION_AGY_MODEL",
    "SECOND_OPINION_OPENCODE_MODEL_POOL",
    "SECOND_OPINION_OPENCODE_MODEL",
    "SECOND_OPINION_PI_MODEL_POOL",
    "SECOND_OPINION_PI_MODEL",
    "SECOND_OPINION_COPILOT_MODEL_POOL",
    "SECOND_OPINION_COPILOT_MODEL",
]


_containment_patcher = None
_bind_preflight_patcher = None
_daemon_patcher = None


def setUpModule() -> None:
    """Clear every model/pool variable so the suite is host-independent, and
    stub the containment probe.

    Both halves matter and they must live in one function: defining a second
    setUpModule shadows the first, which silently restores exactly the host
    pool leakage the first half exists to prevent.

    The containment stubs are needed because the generic and per-backend
    probes shell out to `unshare` and `mount` — blocked by the repo-root
    conftest, and otherwise making these tests depend on the host kernel rather
    than on the behaviour under test.
    """
    global _containment_patcher, _bind_preflight_patcher
    import llm_backends

    for var in EXPECTED_MODEL_ENV_VARS:
        os.environ.pop(var, None)
    _containment_patcher = patch.object(
        llm_backends, "containment_available", lambda: True
    )
    _containment_patcher.start()
    _bind_preflight_patcher = patch.object(
        llm_backends,
        "containment_preflight",
        lambda backend, mode="text-only", target_dir=None: None,
    )
    _bind_preflight_patcher.start()
    # daemon_listening shells out to `ss` on every build; stubbed for the same
    # reason as the containment probe.
    global _daemon_patcher
    _daemon_patcher = patch.object(llm_backends, "daemon_listening", lambda b: False)
    _daemon_patcher.start()


def tearDownModule() -> None:
    if _containment_patcher is not None:
        _containment_patcher.stop()
    if _bind_preflight_patcher is not None:
        _bind_preflight_patcher.stop()
    if _daemon_patcher is not None:
        _daemon_patcher.stop()


def ns(**kwargs: object) -> argparse.Namespace:
    kwargs.setdefault("backend", None)
    kwargs.setdefault("focus_file", None)
    kwargs.setdefault("model_index", None)
    return argparse.Namespace(**kwargs)


def py(code: str) -> list[str]:
    """Build a command that runs `code` as a Python one-liner."""
    return [sys.executable, "-c", code]


class ModelEnvMappingTests(unittest.TestCase):
    """The _POOL_ENV_VARS table is the single source of truth for pool
    resolution, the validator's error variable names, and test cleanup."""

    def test_69_mapping_has_exactly_the_expected_pairs(self) -> None:
        self.assertEqual(
            second_opinion._POOL_ENV_VARS,
            {
                "codex": (
                    "SECOND_OPINION_CODEX_MODEL_POOL",
                    "SECOND_OPINION_CODEX_MODEL",
                ),
                "agy": (
                    "SECOND_OPINION_AGY_MODEL_POOL",
                    "SECOND_OPINION_AGY_MODEL",
                ),
                "opencode": (
                    "SECOND_OPINION_OPENCODE_MODEL_POOL",
                    "SECOND_OPINION_OPENCODE_MODEL",
                ),
                "pi": (
                    "SECOND_OPINION_PI_MODEL_POOL",
                    "SECOND_OPINION_PI_MODEL",
                ),
                "copilot": (
                    "SECOND_OPINION_COPILOT_MODEL_POOL",
                    "SECOND_OPINION_COPILOT_MODEL",
                ),
            },
        )

    def test_69b_mapping_yields_exactly_ten_expected_vars(self) -> None:
        flat = [v for pair in second_opinion._POOL_ENV_VARS.values() for v in pair]
        self.assertEqual(sorted(flat), sorted(EXPECTED_MODEL_ENV_VARS))
        self.assertEqual(len(flat), 10)

    def test_69c_backend_tables_share_keys_and_order(self) -> None:
        keys = ["codex", "agy", "pi", "opencode", "copilot"]
        self.assertEqual(list(second_opinion.BACKEND_PRIORITY), keys)
        self.assertEqual(list(second_opinion.BACKEND_RUNNERS.keys()), keys)
        self.assertEqual(list(second_opinion.BACKEND_LABELS.keys()), keys)


class ValidateModelIndexTests(unittest.TestCase):
    """_validate_model_index is pure: it reads an env mapping (a plain dict)
    and returns a config-error message or None. cmd_review die()s on a
    non-None return, before the runner, so an automatic selection does not
    silently skip to another backend."""

    def test_70_none_index_always_valid(self) -> None:
        for backend in ("agy", "opencode", "pi", "copilot"):
            self.assertIsNone(second_opinion._validate_model_index(backend, None, {}))

    def test_71_missing_pool_is_error_for_each_backend(self) -> None:
        for backend, pool_var in (
            ("agy", "SECOND_OPINION_AGY_MODEL_POOL"),
            ("opencode", "SECOND_OPINION_OPENCODE_MODEL_POOL"),
            ("pi", "SECOND_OPINION_PI_MODEL_POOL"),
            ("copilot", "SECOND_OPINION_COPILOT_MODEL_POOL"),
        ):
            msg = second_opinion._validate_model_index(backend, 0, {})
            self.assertIsNotNone(msg)
            self.assertIn(pool_var, msg)
            self.assertIn(backend, msg)

    def test_72_empty_pool_forms_all_error(self) -> None:
        for value in ("", "   ", ", , ,"):
            msg = second_opinion._validate_model_index(
                "opencode", 0, {"SECOND_OPINION_OPENCODE_MODEL_POOL": value}
            )
            self.assertIsNotNone(msg), f"value={value!r}"
            self.assertIn("SECOND_OPINION_OPENCODE_MODEL_POOL", msg)

    def test_73_single_override_does_not_satisfy_pool_requirement(self) -> None:
        msg = second_opinion._validate_model_index(
            "opencode",
            0,
            {
                "SECOND_OPINION_OPENCODE_MODEL_POOL": "",
                "SECOND_OPINION_OPENCODE_MODEL": "pinned",
            },
        )
        self.assertIsNotNone(msg)
        self.assertIn("SECOND_OPINION_OPENCODE_MODEL_POOL", msg)

    def test_74_out_of_range_is_error(self) -> None:
        msg = second_opinion._validate_model_index(
            "opencode",
            5,
            {"SECOND_OPINION_OPENCODE_MODEL_POOL": "a,b,c"},
        )
        self.assertIsNotNone(msg)
        self.assertIn("out of range", msg)
        self.assertIn("pool has 3 entries", msg)
        self.assertIn("valid indexes: 0-2", msg)

    def test_75_in_range_with_pool_is_valid(self) -> None:
        self.assertIsNone(
            second_opinion._validate_model_index(
                "opencode", 2, {"SECOND_OPINION_OPENCODE_MODEL_POOL": "a,b,c"}
            )
        )

    def test_76_one_entry_pool_accepts_index_zero(self) -> None:
        self.assertIsNone(
            second_opinion._validate_model_index(
                "copilot", 0, {"SECOND_OPINION_COPILOT_MODEL_POOL": "only"}
            )
        )

    def test_77_unknown_backend_never_errors(self) -> None:
        self.assertIsNone(second_opinion._validate_model_index("nope", 3, {}))


class ResolvePlanTextTests(unittest.TestCase):
    def test_01_file_path_reads_contents(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            plan_path = Path(tmp) / "plan.md"
            plan_path.write_text("# Plan\n\nSome content.")
            self.assertEqual(
                second_opinion.resolve_plan_text(str(plan_path)),
                "# Plan\n\nSome content.",
            )

    def test_02_inline_text_passed_through(self) -> None:
        self.assertEqual(
            second_opinion.resolve_plan_text("not a real path, just plan text"),
            "not a real path, just plan text",
        )


class RunAgyWrapperTests(unittest.TestCase):
    """second_opinion.run_agy supplies a model (default or env-overridden) +
    BACKEND_TIMEOUT_SECONDS; llm_backends.run_agy's own argv-building is
    covered in test_llm_backends.py."""

    def test_03_supplies_default_model_and_timeout(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SECOND_OPINION_AGY_MODEL", None)
            os.environ.pop("SECOND_OPINION_AGY_TIMEOUT_SECONDS", None)
            with (
                patch.object(
                    second_opinion.llm_backends, "run_agy", return_value="critique"
                ) as mock_run,
                patch.object(second_opinion, "BACKEND_TIMEOUT_SECONDS", 300),
            ):
                result = second_opinion.run_agy("my prompt")
        self.assertEqual(result, "critique")
        mock_run.assert_called_once_with(
            "my prompt", model=second_opinion.DEFAULT_AGY_MODEL, timeout=300
        )

    def test_03b_forwards_env_var_model(self) -> None:
        with (
            patch.dict(
                os.environ, {"SECOND_OPINION_AGY_MODEL": "Gemini 3.5 Flash (Medium)"}
            ),
            patch.object(
                second_opinion.llm_backends, "run_agy", return_value="critique"
            ) as mock_run,
            patch.object(second_opinion, "BACKEND_TIMEOUT_SECONDS", 300),
        ):
            os.environ.pop("SECOND_OPINION_AGY_TIMEOUT_SECONDS", None)
            result = second_opinion.run_agy("my prompt")
        self.assertEqual(result, "critique")
        mock_run.assert_called_once_with(
            "my prompt", model="Gemini 3.5 Flash (Medium)", timeout=300
        )

    def test_03c_default_agy_model_value(self) -> None:
        self.assertEqual(second_opinion.DEFAULT_AGY_MODEL, "Gemini 3.8 Flash (High)")


class RunCopilotWrapperTests(unittest.TestCase):
    def test_04_forwards_env_var_model(self) -> None:
        with (
            patch.dict(
                os.environ, {"SECOND_OPINION_COPILOT_MODEL": "claude-sonnet-4.6"}
            ),
            patch.object(
                second_opinion.llm_backends, "run_copilot", return_value="critique"
            ) as mock_run,
            patch.object(second_opinion, "BACKEND_TIMEOUT_SECONDS", 300),
        ):
            os.environ.pop("SECOND_OPINION_COPILOT_TIMEOUT_SECONDS", None)
            result = second_opinion.run_copilot("my prompt")
        self.assertEqual(result, "critique")
        mock_run.assert_called_once_with(
            "my prompt", model="claude-sonnet-4.6", timeout=300
        )

    def test_05_no_env_var_passes_none(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SECOND_OPINION_COPILOT_MODEL", None)
            os.environ.pop("SECOND_OPINION_COPILOT_MODEL_POOL", None)
            os.environ.pop("SECOND_OPINION_COPILOT_TIMEOUT_SECONDS", None)
            with (
                patch.object(
                    second_opinion.llm_backends, "run_copilot", return_value="critique"
                ) as mock_run,
                patch.object(second_opinion, "BACKEND_TIMEOUT_SECONDS", 300),
            ):
                second_opinion.run_copilot("my prompt")
        mock_run.assert_called_once_with("my prompt", model=None, timeout=300)

    def test_05b_model_from_pool_at_index(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "SECOND_OPINION_COPILOT_MODEL_POOL": "gpt-4.1,gpt-5-mini",
                    "SECOND_OPINION_COPILOT_MODEL": "",
                },
            ),
            patch.object(
                second_opinion.llm_backends, "run_copilot", return_value="critique"
            ) as mock_run,
            patch.object(second_opinion, "BACKEND_TIMEOUT_SECONDS", 300),
        ):
            os.environ.pop("SECOND_OPINION_COPILOT_TIMEOUT_SECONDS", None)
            second_opinion.run_copilot("my prompt", model_index=1)
        mock_run.assert_called_once_with("my prompt", model="gpt-5-mini", timeout=300)


class ResolvePooledModelTests(unittest.TestCase):
    """Direct tests of _resolve_pooled_model -- the pure precedence/rotation
    logic shared by the runners and backend_label."""

    def _clear(self) -> None:
        for var in (
            "SECOND_OPINION_OPENCODE_MODEL",
            "SECOND_OPINION_OPENCODE_MODEL_POOL",
        ):
            os.environ.pop(var, None)

    def test_06a_single_override_wins_when_no_index(self) -> None:
        with patch.dict(
            os.environ,
            {
                "SECOND_OPINION_OPENCODE_MODEL": "pinned",
                "SECOND_OPINION_OPENCODE_MODEL_POOL": "a,b,c",
            },
        ):
            self.assertEqual(
                second_opinion._resolve_pooled_model(
                    "SECOND_OPINION_OPENCODE_MODEL_POOL",
                    "SECOND_OPINION_OPENCODE_MODEL",
                    None,
                ),
                "pinned",
            )

    def test_06b_whitespace_only_single_override_treated_as_unset(self) -> None:
        with patch.dict(
            os.environ,
            {
                "SECOND_OPINION_OPENCODE_MODEL": "   ",
                "SECOND_OPINION_OPENCODE_MODEL_POOL": "a,b,c",
            },
        ):
            self.assertEqual(
                second_opinion._resolve_pooled_model(
                    "SECOND_OPINION_OPENCODE_MODEL_POOL",
                    "SECOND_OPINION_OPENCODE_MODEL",
                    1,
                ),
                "b",
            )

    def test_06c_empty_pool_and_no_override_returns_none(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            self._clear()
            self.assertIsNone(
                second_opinion._resolve_pooled_model(
                    "SECOND_OPINION_OPENCODE_MODEL_POOL",
                    "SECOND_OPINION_OPENCODE_MODEL",
                    None,
                )
            )

    def test_06d_pool_of_commas_and_whitespace_treated_as_empty(self) -> None:
        with patch.dict(os.environ, {"SECOND_OPINION_OPENCODE_MODEL_POOL": " , , "}):
            self._clear_single_only()
            self.assertIsNone(
                second_opinion._resolve_pooled_model(
                    "SECOND_OPINION_OPENCODE_MODEL_POOL",
                    "SECOND_OPINION_OPENCODE_MODEL",
                    None,
                )
            )

    def _clear_single_only(self) -> None:
        os.environ.pop("SECOND_OPINION_OPENCODE_MODEL", None)

    def test_06e_no_index_defaults_to_zero(self) -> None:
        with patch.dict(os.environ, {"SECOND_OPINION_OPENCODE_MODEL_POOL": "a,b,c"}):
            self._clear_single_only()
            self.assertEqual(
                second_opinion._resolve_pooled_model(
                    "SECOND_OPINION_OPENCODE_MODEL_POOL",
                    "SECOND_OPINION_OPENCODE_MODEL",
                    None,
                ),
                "a",
            )

    def test_06f_explicit_index_selects_entry(self) -> None:
        with patch.dict(os.environ, {"SECOND_OPINION_OPENCODE_MODEL_POOL": "a,b,c"}):
            self._clear_single_only()
            self.assertEqual(
                second_opinion._resolve_pooled_model(
                    "SECOND_OPINION_OPENCODE_MODEL_POOL",
                    "SECOND_OPINION_OPENCODE_MODEL",
                    2,
                ),
                "c",
            )

    def test_06g_out_of_range_index_unresolved(self) -> None:
        # Out-of-range indexes are rejected by _validate_model_index before
        # the runner; if _resolve_pooled_model is ever reached with one
        # (validation bypassed), it must not silently wrap -- it returns None.
        with patch.dict(os.environ, {"SECOND_OPINION_OPENCODE_MODEL_POOL": "a,b,c"}):
            self._clear_single_only()
            self.assertIsNone(
                second_opinion._resolve_pooled_model(
                    "SECOND_OPINION_OPENCODE_MODEL_POOL",
                    "SECOND_OPINION_OPENCODE_MODEL",
                    5,
                )
            )

    def test_06h_pool_entries_stripped_of_whitespace(self) -> None:
        with patch.dict(
            os.environ, {"SECOND_OPINION_OPENCODE_MODEL_POOL": " a , b ,c "}
        ):
            self._clear_single_only()
            self.assertEqual(
                second_opinion._resolve_pooled_model(
                    "SECOND_OPINION_OPENCODE_MODEL_POOL",
                    "SECOND_OPINION_OPENCODE_MODEL",
                    1,
                ),
                "b",
            )


class BackendLabelTests(unittest.TestCase):
    def test_51_non_copilot_labels_unaffected_by_env_var(self) -> None:
        with patch.dict(
            os.environ, {"SECOND_OPINION_COPILOT_MODEL": "claude-sonnet-4.6"}
        ):
            os.environ.pop("SECOND_OPINION_OPENCODE_MODEL", None)
            self.assertEqual(
                second_opinion.backend_label("agy"),
                second_opinion.BACKEND_LABELS["agy"],
            )
            self.assertEqual(
                second_opinion.backend_label("opencode"),
                second_opinion.BACKEND_LABELS["opencode"],
            )

    def test_52_copilot_label_unchanged_when_env_var_unset(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SECOND_OPINION_COPILOT_MODEL", None)
            self.assertEqual(
                second_opinion.backend_label("copilot"),
                second_opinion.BACKEND_LABELS["copilot"],
            )

    def test_53_copilot_label_includes_model_when_env_var_set(self) -> None:
        with patch.dict(
            os.environ, {"SECOND_OPINION_COPILOT_MODEL": "claude-sonnet-4.6"}
        ):
            self.assertEqual(
                second_opinion.backend_label("copilot"),
                f"{second_opinion.BACKEND_LABELS['copilot']} (claude-sonnet-4.6)",
            )

    def test_54_agy_label_unchanged_when_env_var_unset(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SECOND_OPINION_AGY_MODEL", None)
            self.assertEqual(
                second_opinion.backend_label("agy"),
                second_opinion.BACKEND_LABELS["agy"],
            )

    def test_55_agy_label_reflects_overridden_model_when_env_var_set(self) -> None:
        with patch.dict(
            os.environ, {"SECOND_OPINION_AGY_MODEL": "Gemini 3.5 Flash (Medium)"}
        ):
            self.assertEqual(
                second_opinion.backend_label("agy"),
                "agy (Gemini 3.5 Flash (Medium))",
            )

    def test_56_agy_label_unchanged_when_env_var_set_to_default(self) -> None:
        with patch.dict(
            os.environ, {"SECOND_OPINION_AGY_MODEL": second_opinion.DEFAULT_AGY_MODEL}
        ):
            self.assertEqual(
                second_opinion.backend_label("agy"),
                second_opinion.BACKEND_LABELS["agy"],
            )

    def test_57_opencode_label_includes_model_when_env_var_set(self) -> None:
        with patch.dict(
            os.environ, {"SECOND_OPINION_OPENCODE_MODEL": "claude-sonnet-4.6"}
        ):
            self.assertEqual(
                second_opinion.backend_label("opencode"),
                f"{second_opinion.BACKEND_LABELS['opencode']} (claude-sonnet-4.6)",
            )

    def test_58_opencode_label_unchanged_when_env_var_unset(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SECOND_OPINION_OPENCODE_MODEL", None)
            self.assertEqual(
                second_opinion.backend_label("opencode"),
                second_opinion.BACKEND_LABELS["opencode"],
            )

    def test_59_opencode_label_unchanged_when_env_var_empty(self) -> None:
        with patch.dict(os.environ, {"SECOND_OPINION_OPENCODE_MODEL": ""}):
            self.assertEqual(
                second_opinion.backend_label("opencode"),
                second_opinion.BACKEND_LABELS["opencode"],
            )

    def test_60_opencode_var_does_not_affect_agy_or_copilot_labels(self) -> None:
        with patch.dict(
            os.environ, {"SECOND_OPINION_OPENCODE_MODEL": "claude-sonnet-4.6"}
        ):
            os.environ.pop("SECOND_OPINION_AGY_MODEL", None)
            os.environ.pop("SECOND_OPINION_COPILOT_MODEL", None)
            self.assertEqual(
                second_opinion.backend_label("agy"),
                second_opinion.BACKEND_LABELS["agy"],
            )
            self.assertEqual(
                second_opinion.backend_label("copilot"),
                second_opinion.BACKEND_LABELS["copilot"],
            )

    def test_61_opencode_label_reflects_pool_resolved_model(self) -> None:
        # Regression: backend_label used to read SECOND_OPINION_OPENCODE_MODEL
        # directly, independent of the runner's resolution -- once the runner
        # started resolving via the pool, the label would silently stop
        # showing a model at all unless it shares the same resolution.
        with patch.dict(
            os.environ, {"SECOND_OPINION_OPENCODE_MODEL_POOL": "model-a,model-b"}
        ):
            os.environ.pop("SECOND_OPINION_OPENCODE_MODEL", None)
            self.assertEqual(
                second_opinion.backend_label("opencode", model_index=1),
                f"{second_opinion.BACKEND_LABELS['opencode']} (model-b)",
            )

    def test_62_copilot_label_reflects_pool_resolved_model(self) -> None:
        with patch.dict(
            os.environ, {"SECOND_OPINION_COPILOT_MODEL_POOL": "gpt-4.1,gpt-5-mini"}
        ):
            os.environ.pop("SECOND_OPINION_COPILOT_MODEL", None)
            self.assertEqual(
                second_opinion.backend_label("copilot", model_index=0),
                f"{second_opinion.BACKEND_LABELS['copilot']} (gpt-4.1)",
            )

    def test_63_agy_label_takes_an_explicitly_passed_model(self) -> None:
        # A caller that knows which model was attempted passes it straight
        # through. None and the default both mean "agy's own default model",
        # so neither replaces the bare label; only a non-default model does.
        self.assertEqual(
            second_opinion.backend_label("agy", model=None),
            second_opinion.BACKEND_LABELS["agy"],
        )
        self.assertEqual(
            second_opinion.backend_label(
                "agy", model=second_opinion.DEFAULT_AGY_MODEL
            ),
            second_opinion.BACKEND_LABELS["agy"],
        )
        self.assertEqual(
            second_opinion.backend_label("agy", model="Gemini 3.5 Flash (Medium)"),
            "agy (Gemini 3.5 Flash (Medium))",
        )


class RunOpencodeTests(unittest.TestCase):
    @contextmanager
    def _run_command_returning(
        self, stdout: str, stderr: str = "", returncode: int = 0
    ) -> Iterator[None]:
        with patch.object(
            second_opinion, "_run_command", return_value=(returncode, stdout, stderr)
        ):
            yield

    def test_36_text_chunks_concatenated(self) -> None:
        stdout = (
            json.dumps({"type": "text", "part": {"text": "Hello "}})
            + "\n"
            + json.dumps({"type": "text", "part": {"text": "world"}})
            + "\n"
        )
        with self._run_command_returning(stdout):
            self.assertEqual(second_opinion.run_opencode("prompt"), "Hello world")

    def test_37_structured_error_event_raises_with_message(self) -> None:
        stdout = (
            json.dumps(
                {"type": "error", "error": {"data": {"message": "agent crashed"}}}
            )
            + "\n"
        )
        with (
            self._run_command_returning(stdout),
            self.assertRaises(second_opinion.BackendError) as cm,
        ):
            second_opinion.run_opencode("prompt")
        self.assertIn("agent crashed", str(cm.exception))

    def test_38_error_field_as_plain_string_does_not_crash(self) -> None:
        # Regression: `e.get("error", {}).get("data", {})` used to crash
        # with AttributeError when "error" was a plain string rather than
        # a nested object.
        stdout = json.dumps({"type": "error", "error": "flat string error"}) + "\n"
        with (
            self._run_command_returning(stdout),
            self.assertRaises(second_opinion.BackendError) as cm,
        ):
            second_opinion.run_opencode("prompt")
        self.assertIn("flat string error", str(cm.exception))

    def test_39_no_text_no_error_falls_back_to_raw_output(self) -> None:
        stdout = json.dumps({"type": "other"}) + "\n"
        with (
            self._run_command_returning(stdout, stderr="some stderr detail"),
            self.assertRaises(second_opinion.BackendError) as cm,
        ):
            second_opinion.run_opencode("prompt")
        self.assertIn("some stderr detail", str(cm.exception))

    def test_40_completely_unparseable_output_falls_back_to_stdout_snippet(
        self,
    ) -> None:
        with (
            self._run_command_returning("not json at all", stderr=""),
            self.assertRaises(second_opinion.BackendError) as cm,
        ):
            second_opinion.run_opencode("prompt")
        self.assertIn("not json at all", str(cm.exception))

    def _capture_cmd(
        self,
    ) -> tuple[Callable[..., tuple[int, str, str]], dict[str, list[str] | int]]:
        """Return a `_run_command` side-effect capturing its argv and timeout, plus the box."""
        box: dict[str, object] = {}

        def capture(
            cmd: list[str],
            timeout: int | None = None,
            *,
            retries: int = 0,
            stall_seconds: float | None = None,
        ) -> tuple[int, str, str]:
            box["cmd"] = cmd
            if timeout is not None:
                box["timeout"] = timeout
            box["retries"] = retries
            box["stall_seconds"] = stall_seconds
            return (
                0,
                json.dumps({"type": "text", "part": {"text": "critique"}}) + "\n",
                "",
            )

        return capture, box

    def test_40b_model_flag_added_when_env_var_set(self) -> None:
        capture, box = self._capture_cmd()
        with (
            patch.dict(
                os.environ, {"SECOND_OPINION_OPENCODE_MODEL": "claude-sonnet-4.6"}
            ),
            patch.object(second_opinion, "_run_command", side_effect=capture),
        ):
            result = second_opinion.run_opencode("my prompt")
        self.assertEqual(result, "critique")
        self.assertEqual(box["cmd"][-3:-1], ["-m", "claude-sonnet-4.6"])
        self.assertEqual(box["cmd"].count("-m"), 1)

    def test_40c_no_model_flag_when_env_var_unset(self) -> None:
        capture, box = self._capture_cmd()
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SECOND_OPINION_OPENCODE_MODEL", None)
            with patch.object(second_opinion, "_run_command", side_effect=capture):
                second_opinion.run_opencode("my prompt")
        self.assertNotIn("-m", box["cmd"])

    def test_40d_no_model_flag_when_env_var_empty(self) -> None:
        capture, box = self._capture_cmd()
        with (
            patch.dict(os.environ, {"SECOND_OPINION_OPENCODE_MODEL": ""}),
            patch.object(second_opinion, "_run_command", side_effect=capture),
        ):
            second_opinion.run_opencode("my prompt")
        self.assertNotIn("-m", box["cmd"])

    def test_40e_model_flag_from_pool_at_index(self) -> None:
        capture, box = self._capture_cmd()
        with (
            patch.dict(
                os.environ,
                {
                    "SECOND_OPINION_OPENCODE_MODEL_POOL": "model-a,model-b,model-c",
                    "SECOND_OPINION_OPENCODE_MODEL": "",
                },
            ),
            patch.object(second_opinion, "_run_command", side_effect=capture),
        ):
            second_opinion.run_opencode("my prompt", model_index=1)
        self.assertEqual(box["cmd"][-3:-1], ["-m", "model-b"])

    def test_40f_pool_index_upper_boundary(self) -> None:
        capture, box = self._capture_cmd()
        with (
            patch.dict(
                os.environ,
                {
                    "SECOND_OPINION_OPENCODE_MODEL_POOL": "model-a,model-b,model-c",
                    "SECOND_OPINION_OPENCODE_MODEL": "",
                },
            ),
            patch.object(second_opinion, "_run_command", side_effect=capture),
        ):
            second_opinion.run_opencode("my prompt", model_index=2)
        self.assertEqual(box["cmd"][-3:-1], ["-m", "model-c"])

    def test_40g_explicit_index_selects_pool_over_single_override(self) -> None:
        capture, box = self._capture_cmd()
        with (
            patch.dict(
                os.environ,
                {
                    "SECOND_OPINION_OPENCODE_MODEL_POOL": "model-a,model-b",
                    "SECOND_OPINION_OPENCODE_MODEL": "pinned-model",
                },
            ),
            patch.object(second_opinion, "_run_command", side_effect=capture),
        ):
            second_opinion.run_opencode("my prompt", model_index=1)
        self.assertEqual(box["cmd"][-3:-1], ["-m", "model-b"])

    def test_40h_requests_one_retry(self) -> None:
        # opencode's CLI intermittently stalls its event stream (see
        # llm_backends.run_opencode's docstring) -- the adversary agent
        # must ask for one retry, not the zero-retry default every other
        # backend uses.
        capture, box = self._capture_cmd()
        with patch.object(second_opinion, "_run_command", side_effect=capture):
            second_opinion.run_opencode("my prompt")
        # A stall (no output) is the recoverable failure mode, not a full
        # timeout: ask for one retry after a ~90s output stall, not a blind
        # retry-after-timeout (production retries succeeded 0 of 6).
        self.assertEqual(box["retries"], 1)
        self.assertEqual(box["stall_seconds"], 90)

    def test_60_tool_use_event_raises_backend_error(self) -> None:
        # Regression: the adversary agent must be stateless and text-only; a
        # tool_use event means a swapped-in model took a real shell/file
        # action instead of critiquing, so fail loud rather than swallow it.
        stdout = (
            json.dumps(
                {
                    "type": "tool_use",
                    "part": {"tool": "bash", "state": {"status": "completed"}},
                }
            )
            + "\n"
        )
        with (
            self._run_command_returning(stdout),
            self.assertRaises(second_opinion.BackendError) as cm,
        ):
            second_opinion.run_opencode("prompt")
        self.assertIn("bash", str(cm.exception))
        self.assertIn("instead of returning", str(cm.exception))

    def test_61_tool_use_raises_even_with_text_chunks(self) -> None:
        stdout = (
            json.dumps({"type": "text", "part": {"text": "the plan looks fine"}})
            + "\n"
            + json.dumps(
                {
                    "type": "tool_use",
                    "part": {"tool": "read", "state": {"status": "completed"}},
                }
            )
            + "\n"
        )
        with (
            self._run_command_returning(stdout),
            self.assertRaises(second_opinion.BackendError) as cm,
        ):
            second_opinion.run_opencode("prompt")
        self.assertIn("read", str(cm.exception))

    def test_62_emitted_tool_call_markup_text_raises(self) -> None:
        # Regression: a tool-hungry model denied every tool can emit its
        # attempted tool calls as literal <tool_calls> XML in a text event
        # (the captured post-fix failure), which would otherwise be returned
        # verbatim as the adversary critique.
        text = (
            "\n\n<tool_calls>\n"
            '<invoke name="bash">\n'
            '<parameter name="command" string="true">ls -la</parameter>\n'
            "</invoke>\n"
            "</tool_calls>"
        )
        stdout = json.dumps({"type": "text", "part": {"text": text}}) + "\n"
        with (
            self._run_command_returning(stdout),
            self.assertRaises(second_opinion.BackendError) as cm,
        ):
            second_opinion.run_opencode("prompt")
        self.assertIn("tool-call markup", str(cm.exception))

    def test_63_prose_merely_mentioning_tool_calls_passes(self) -> None:
        text = (
            "The plan's agent config will never emit <tool_calls> wrappers or "
            '<invoke name="bash"> markup since every tool is denied, so this '
            "risk is moot."
        )
        stdout = json.dumps({"type": "text", "part": {"text": text}}) + "\n"
        with self._run_command_returning(stdout):
            self.assertEqual(second_opinion.run_opencode("prompt"), text)

    def test_64_reordered_invoke_attributes_raises(self) -> None:
        # Regression: attribute order isn't guaranteed on a leaked <invoke>.
        text = '<invoke id="1" name="bash"><parameter name="command">ls</parameter></invoke>'
        stdout = json.dumps({"type": "text", "part": {"text": text}}) + "\n"
        with (
            self._run_command_returning(stdout),
            self.assertRaises(second_opinion.BackendError) as cm,
        ):
            second_opinion.run_opencode("prompt")
        self.assertIn("tool-call markup", str(cm.exception))

    def test_65_quoted_example_in_larger_critique_passes(self) -> None:
        # Regression: a real critique that quotes a fully-closed example of
        # this exact leak shape (plausible when reviewing this very feature)
        # must not itself be rejected as a leaked call.
        critique = (
            "This diff's leak detection is a good idea but underspecified. "
            "For instance, a leak shaped like "
            '<tool_calls><invoke name="bash"><parameter name="command">ls'
            "</parameter></invoke></tool_calls> would need every attribute "
            "in exactly that order to be caught, which is fragile. I'd "
            "recommend a broader set of shape patterns plus a check that "
            "the matched markup actually dominates the response, rather "
            "than merely appearing somewhere within it, so a critique that "
            "quotes an example like the one above during review doesn't "
            "get rejected as if it were the leak itself."
        )
        stdout = json.dumps({"type": "text", "part": {"text": critique}}) + "\n"
        with self._run_command_returning(stdout):
            self.assertEqual(second_opinion.run_opencode("prompt"), critique)

    def test_opencode_permission_denied_stderr_is_sanitized(self) -> None:
        # opencode reaches its "no text output" path with the vendor's
        # permission hint on stderr. The dangerous --dangerously-skip-permissions
        # hint must be stripped from the raised error (not passed through).
        stdout = json.dumps({"type": "other"}) + "\n"
        with self._run_command_returning(
            stdout,
            stderr=(
                "no output produced - a tool required the command permission; "
                "re-run with --dangerously-skip-permissions"
            ),
        ):
            with self.assertRaises(llm_backends.BackendToolPermissionDeniedError) as cm:
                second_opinion.run_opencode("prompt")
        msg = str(cm.exception)
        self.assertNotIn("--dangerously-skip-permissions", msg)
        self.assertIn("auto-denied", msg)


class CmdDetectTests(unittest.TestCase):
    def test_41_detect_reports_presence_and_eligibility(self) -> None:
        """detect reports both halves. Presence alone was misleading: an
        installed backend that cannot meet the isolation contract is never
        used, so reporting it as simply available pushed the discovery to the
        moment a critique was wanted."""
        out = io.StringIO()
        with (
            patch(
                "shutil.which",
                side_effect=lambda b: "/usr/bin/agy" if b == "agy" else None,
            ),
            patch.object(
                second_opinion.llm_backends, "containment_available", lambda: True
            ),
            patch("sys.stdout", out),
        ):
            second_opinion.cmd_detect(ns())
        report = json.loads(out.getvalue())
        self.assertEqual(sorted(report), ["agy", "codex", "copilot", "opencode", "pi"])
        self.assertTrue(report["agy"]["present"])
        self.assertTrue(report["agy"]["eligible"])
        for absent in ("codex", "opencode", "pi", "copilot"):
            self.assertFalse(report[absent]["present"], absent)
            self.assertFalse(report[absent]["eligible"], absent)
            self.assertEqual(report[absent]["reason"], "not installed", absent)

    def test_41b_detect_explains_an_installed_but_uncontainable_backend(self) -> None:
        """Installed and still ineligible is the case worth surfacing early:
        agy needs OS containment for every clause, so on a host without working
        namespaces it is present but unusable, and the reason must say so."""
        out = io.StringIO()
        with (
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.object(
                second_opinion.llm_backends, "containment_available", lambda: False
            ),
            patch("sys.stdout", out),
        ):
            second_opinion.cmd_detect(ns())
        report = json.loads(out.getvalue())
        self.assertTrue(report["agy"]["present"])
        self.assertFalse(report["agy"]["eligible"])
        self.assertIn("containment", report["agy"]["reason"])
        # pi isolates by flags alone, so it stays eligible on the same host.
        self.assertTrue(report["pi"]["eligible"])

    def test_detect_marks_backend_ineligible_when_a_required_bind_fails(self) -> None:
        out = io.StringIO()
        with (
            patch(
                "shutil.which",
                side_effect=lambda b: "/usr/bin/agy" if b == "agy" else None,
            ),
            patch.object(
                second_opinion.llm_backends, "containment_available", lambda: True
            ),
            patch.object(
                second_opinion.llm_backends,
                "containment_preflight",
                lambda backend, mode="text-only", target_dir=None: (
                    "bind preflight failed: source=/home/user/.local"
                    if backend == "agy"
                    else None
                ),
            ),
            patch("sys.stdout", out),
        ):
            second_opinion.cmd_detect(ns(mode="grounded"))
        report = json.loads(out.getvalue())
        self.assertFalse(report["agy"]["eligible"])
        self.assertIn("source=/home/user/.local", report["agy"]["reason"])

    def test_grounded_dispatch_skips_only_the_backend_with_a_failed_bind(self) -> None:
        calls: list[str] = []

        def run_agy(prompt: str, *, model_index: int | None = None) -> str:
            calls.append("agy")
            return "safe fallback critique"

        with (
            patch.dict(
                os.environ,
                {"SECOND_OPINION_AGY_MODEL": "Gemini 3.8 Flash (High)"},
            ),
            patch(
                "shutil.which",
                side_effect=lambda b: f"/usr/bin/{b}" if b in {"codex", "agy"} else None,
            ),
            patch.object(
                second_opinion.llm_backends, "containment_available", lambda: True
            ),
            patch.object(
                second_opinion.llm_backends,
                "containment_preflight",
                lambda backend, mode="text-only", target_dir=None: (
                    "bind preflight failed: source=/home/user/.local"
                    if backend == "codex"
                    else None
                ),
            ),
            patch.object(second_opinion, "BACKEND_RUNNERS", {"agy": run_agy}),
        ):
            result = second_opinion.review_plan(
                second_opinion.ReviewRequest(plan_text="my plan")
            )

        self.assertEqual(result.response_text, "safe fallback critique")
        self.assertEqual(calls, ["agy"])

    @pytest.mark.regression(
        "grounded-dispatch-preflights-target-bind",
        "AssertionError: ReviewError not raised",
    )
    def test_grounded_dispatch_preflights_target_bind(self) -> None:
        target = Path("/sandbox/worktree").resolve()
        calls: list[str] = []

        def run_agy(prompt: str, *, model_index: int | None = None) -> str:
            calls.append("agy")
            return "should not run"

        with (
            patch("shutil.which", side_effect=lambda backend: f"/usr/bin/{backend}"),
            patch.object(
                second_opinion.llm_backends, "containment_available", lambda: True
            ),
            patch.object(
                second_opinion.llm_backends,
                "containment_preflight",
                lambda backend, mode="text-only", target_dir=None: (
                    f"bind preflight failed: source={target_dir}"
                    if target_dir == target
                    else None
                ),
            ),
            patch.object(second_opinion, "BACKEND_RUNNERS", {"agy": run_agy}),
            self.assertRaises(second_opinion.ReviewError) as raised,
        ):
            second_opinion.review_plan(
                second_opinion.ReviewRequest(
                    plan_text="my plan", backend="agy", target_dir=target
                )
            )

        self.assertIn(str(target), str(raised.exception))
        self.assertEqual(calls, [])

    @pytest.mark.regression(
        "text-only-dispatch-skips-backend-with-failed-bind",
        "AssertionError: 'agy critique' != 'safe fallback critique'",
    )
    def test_text_only_dispatch_skips_backend_with_a_failed_bind(self) -> None:
        calls: list[str] = []

        def run_agy(prompt: str, *, model_index: int | None = None) -> str:
            calls.append("agy")
            return "agy critique"

        def run_opencode(prompt: str, *, model_index: int | None = None) -> str:
            calls.append("opencode")
            return "safe fallback critique"

        with (
            patch.object(
                second_opinion,
                "available_backends",
                return_value=["agy", "opencode"],
            ),
            patch(
                "shutil.which",
                side_effect=lambda backend: f"/usr/bin/{backend}"
                if backend in {"agy", "opencode"}
                else None,
            ),
            patch.object(
                second_opinion.llm_backends, "containment_available", lambda: True
            ),
            patch.object(
                second_opinion.llm_backends,
                "containment_preflight",
                lambda backend, mode="text-only", target_dir=None: (
                    "bind preflight failed: source=/home/user/.local"
                    if backend == "agy"
                    else None
                ),
            ),
            patch.object(
                second_opinion,
                "BACKEND_RUNNERS",
                {"agy": run_agy, "opencode": run_opencode},
            ),
        ):
            result = second_opinion.review_plan(
                second_opinion.ReviewRequest(plan_text="my plan", text_only=True)
            )

        self.assertEqual(result.response_text, "safe fallback critique")
        self.assertEqual(calls, ["opencode"])

    def test_text_only_dispatch_runs_when_containment_preflight_succeeds(self) -> None:
        calls: list[str] = []
        preflight_modes: list[str] = []

        def run_agy(prompt: str, *, model_index: int | None = None) -> str:
            calls.append("agy")
            return "agy critique"

        def preflight(
            backend: str, mode: str = "text-only", target_dir: Path | None = None
        ) -> str | None:
            preflight_modes.append(mode)
            return None

        with (
            patch.object(second_opinion, "available_backends", return_value=["agy"]),
            patch("shutil.which", return_value="/usr/bin/agy"),
            patch.object(
                second_opinion.llm_backends, "containment_available", lambda: True
            ),
            patch.object(
                second_opinion.llm_backends, "containment_preflight", preflight
            ),
            patch.object(second_opinion, "BACKEND_RUNNERS", {"agy": run_agy}),
        ):
            result = second_opinion.review_plan(
                second_opinion.ReviewRequest(plan_text="my plan", text_only=True)
            )

        self.assertEqual(result.response_text, "agy critique")
        self.assertEqual(calls, ["agy"])
        self.assertTrue(preflight_modes)
        self.assertEqual(set(preflight_modes), {"text-only"})


class CmdReviewTests(unittest.TestCase):
    def test_42_forced_backend_not_on_path_dies(self) -> None:
        err = io.StringIO()
        with (
            patch("shutil.which", return_value=None),
            self.assertRaises(SystemExit) as cm,
            patch("sys.stderr", err),
        ):
            second_opinion.cmd_review(ns(plan="text", backend="agy"))
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("agy not found on PATH", err.getvalue())

    def test_43_no_backend_available_dies(self) -> None:
        err = io.StringIO()
        with (
            patch("shutil.which", return_value=None),
            self.assertRaises(SystemExit) as cm,
            patch("sys.stderr", err),
        ):
            second_opinion.cmd_review(ns(plan="text"))
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("no backend available", err.getvalue())

    def test_44_first_backend_fails_second_succeeds(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with (
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.object(
                second_opinion,
                "BACKEND_RUNNERS",
                {
                    "agy": lambda p, model_index=None: (_ for _ in ()).throw(
                        second_opinion.BackendError("agy broke")
                    ),
                    "pi": lambda p, model_index=None: (_ for _ in ()).throw(
                        second_opinion.BackendError("pi broke")
                    ),
                    "opencode": lambda p, model_index=None: "opencode's critique",
                },
            ),
            patch("sys.stdout", out),
            patch("sys.stderr", err),
        ):
            second_opinion.cmd_review(ns(plan="my plan", verbose=True))
        self.assertIn("opencode's critique", out.getvalue())
        self.assertIn("agy broke", err.getvalue())

    def test_45_all_backends_fail_dies_with_combined_message(self) -> None:
        err = io.StringIO()
        with (
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.object(
                second_opinion,
                "BACKEND_RUNNERS",
                {
                    "agy": lambda p, model_index=None: (_ for _ in ()).throw(
                        second_opinion.BackendError("agy broke")
                    ),
                    "opencode": lambda p, model_index=None: (_ for _ in ()).throw(
                        second_opinion.BackendError("opencode broke")
                    ),
                    "pi": lambda p, model_index=None: (_ for _ in ()).throw(
                        second_opinion.BackendError("pi broke")
                    ),
                    "copilot": lambda p, model_index=None: (_ for _ in ()).throw(
                        second_opinion.BackendError("copilot broke")
                    ),
                },
            ),
            self.assertRaises(SystemExit) as cm,
            patch("sys.stderr", err),
        ):
            second_opinion.cmd_review(ns(plan="my plan"))
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("agy broke", err.getvalue())
        self.assertIn("opencode broke", err.getvalue())
        self.assertIn("pi broke", err.getvalue())
        self.assertIn("copilot broke", err.getvalue())
        self.assertIn("all backends failed — ", err.getvalue())

    def test_45b_all_backends_ruled_out_by_size(self) -> None:
        err = io.StringIO()
        with (
            patch(
                "shutil.which",
                side_effect=lambda b: f"/usr/bin/{b}" if b == "pi" else None,
            ),
            patch.object(
                second_opinion,
                "BACKEND_RUNNERS",
                {
                    "pi": lambda p, model_index=None: (_ for _ in ()).throw(
                        second_opinion.llm_backends.BackendPayloadSizeError(
                            "pi payload too large"
                        )
                    ),
                },
            ),
            self.assertRaises(SystemExit) as cm,
            patch("sys.stderr", err),
        ):
            second_opinion.cmd_review(ns(plan="my plan", backend="pi"))
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("all backends ruled out by payload size", err.getvalue())
        self.assertIn("pi: pi payload too large", err.getvalue())

    def test_45c_mixed_failure_reports_generic_message(self) -> None:
        err = io.StringIO()
        with (
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.object(
                second_opinion,
                "BACKEND_RUNNERS",
                {
                    "agy": lambda p, model_index=None: (_ for _ in ()).throw(
                        second_opinion.BackendError("agy broke")
                    ),
                    "pi": lambda p, model_index=None: (_ for _ in ()).throw(
                        second_opinion.llm_backends.BackendPayloadSizeError(
                            "pi payload too large"
                        )
                    ),
                    "opencode": lambda p, model_index=None: (_ for _ in ()).throw(
                        second_opinion.BackendError("opencode broke")
                    ),
                    "copilot": lambda p, model_index=None: (_ for _ in ()).throw(
                        second_opinion.BackendError("copilot broke")
                    ),
                },
            ),
            self.assertRaises(SystemExit) as cm,
            patch("sys.stderr", err),
        ):
            second_opinion.cmd_review(ns(plan="my plan"))
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("all backends failed — ", err.getvalue())
        self.assertNotIn("all backends ruled out by payload size", err.getvalue())

    def test_46_plan_file_contents_used_not_the_path(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            plan_path = Path(tmp) / "plan.md"
            plan_path.write_text("# The actual plan\ndetails here")

            captured_prompt = {}

            def fake_runner(prompt: str, model_index: int | None = None) -> str:
                captured_prompt["value"] = prompt
                return "critique"

            out = io.StringIO()
            with (
                patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
                patch.object(
                    second_opinion,
                    "BACKEND_RUNNERS",
                    {"agy": fake_runner, "opencode": fake_runner},
                ),
                patch("sys.stdout", out),
            ):
                second_opinion.cmd_review(ns(plan=str(plan_path)))
        self.assertIn("# The actual plan", captured_prompt["value"])
        self.assertIn("details here", captured_prompt["value"])

    def test_63_model_index_threaded_through_to_runner(self) -> None:
        # Not just that the runner tolerates model_index (the fakes updated
        # above for that) -- that cmd_review actually passes args.model_index
        # through, end to end, rather than e.g. a stale default.
        captured = {}

        def spy_runner(prompt: str, model_index: int | None = None) -> str:
            captured["model_index"] = model_index
            return "critique"

        out = io.StringIO()
        with (
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.object(second_opinion, "BACKEND_RUNNERS", {"agy": spy_runner}),
            patch.dict(os.environ, {"SECOND_OPINION_AGY_MODEL_POOL": "m0,m1,m2,m3"}),
            patch("sys.stdout", out),
        ):
            second_opinion.cmd_review(ns(plan="text", backend="agy", model_index=2))
        self.assertEqual(captured["model_index"], 2)

    def test_64_agy_backend_with_explicit_model_index_succeeds(self) -> None:
        # agy now honors model_index (shared indexed-pool contract) -- a valid
        # index into a configured pool must not crash.
        out = io.StringIO()
        with (
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.object(
                second_opinion,
                "BACKEND_RUNNERS",
                {"agy": lambda p, model_index=None: "agy critique"},
            ),
            patch.dict(os.environ, {"SECOND_OPINION_AGY_MODEL_POOL": "m0,m1,m2,m3"}),
            patch("sys.stdout", out),
        ):
            second_opinion.cmd_review(ns(plan="text", backend="agy", model_index=3))
        self.assertIn("agy critique", out.getvalue())

    def test_50a_forced_backend_index_missing_pool_dies(self) -> None:
        err = io.StringIO()
        with (
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.dict(os.environ, {}, clear=False),
            self.assertRaises(SystemExit) as cm,
            patch("sys.stderr", err),
        ):
            second_opinion.cmd_review(ns(plan="text", backend="agy", model_index=0))
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("SECOND_OPINION_AGY_MODEL_POOL", err.getvalue())
        self.assertIn("for backend agy", err.getvalue())

    def test_50b_forced_backend_index_out_of_range_dies(self) -> None:
        err = io.StringIO()
        with (
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.dict(os.environ, {"SECOND_OPINION_AGY_MODEL_POOL": "only"}),
            self.assertRaises(SystemExit) as cm,
            patch("sys.stderr", err),
        ):
            second_opinion.cmd_review(ns(plan="text", backend="agy", model_index=3))
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("out of range", err.getvalue())
        self.assertIn("pool has 1 entries", err.getvalue())
        self.assertIn("valid indexes: 0-0", err.getvalue())

    def test_50c_forced_backend_index_pool_used_over_single_override(self) -> None:
        capture, box = RunOpencodeTests()._capture_cmd()
        out = io.StringIO()
        with (
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.dict(
                os.environ,
                {
                    "SECOND_OPINION_OPENCODE_MODEL_POOL": "x,y",
                    "SECOND_OPINION_OPENCODE_MODEL": "pinned",
                },
            ),
            patch.object(second_opinion, "_run_command", side_effect=capture),
            patch("sys.stdout", out),
        ):
            second_opinion.cmd_review(
                ns(plan="text", backend="opencode", model_index=1)
            )
        self.assertEqual(box["cmd"][-3:-1], ["-m", "y"])

    def test_50d_automatic_selection_stops_on_first_candidate_config_error(
        self,
    ) -> None:
        # codex is first in priority; an empty codex pool with an explicit index
        # must stop selection (configuration error), NOT silently fall through
        # to opencode even though opencode is configured.
        err = io.StringIO()
        with (
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.dict(
                os.environ, {"SECOND_OPINION_OPENCODE_MODEL_POOL": "only-opencode"}
            ),
            self.assertRaises(SystemExit) as cm,
            patch("sys.stderr", err),
        ):
            second_opinion.cmd_review(ns(plan="text", model_index=0))
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("SECOND_OPINION_CODEX_MODEL_POOL", err.getvalue())
        self.assertIn("for backend codex", err.getvalue())

    def test_50e_agy_index_resolves_pool_model(self) -> None:
        captured: dict[str, object] = {}
        out = io.StringIO()

        def fake_agy(prompt: str, model: str, timeout: int) -> str:
            captured["model"] = model
            return "critique"

        with (
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.dict(os.environ, {"SECOND_OPINION_AGY_MODEL_POOL": "a,b"}),
            patch.object(second_opinion.llm_backends, "run_agy", side_effect=fake_agy),
            patch("sys.stdout", out),
        ):
            second_opinion.cmd_review(ns(plan="text", backend="agy", model_index=1))
        self.assertEqual(captured["model"], "b")

    def test_50f_copilot_index_resolves_pool_model(self) -> None:
        captured: dict[str, object] = {}
        out = io.StringIO()

        def fake_copilot(prompt: str, model: str | None, timeout: int) -> str:
            captured["model"] = model
            return "critique"

        with (
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.dict(
                os.environ, {"SECOND_OPINION_COPILOT_MODEL_POOL": "gpt-4,gpt-5"}
            ),
            patch.object(
                second_opinion.llm_backends, "run_copilot", side_effect=fake_copilot
            ),
            patch("sys.stdout", out),
        ):
            second_opinion.cmd_review(ns(plan="text", backend="copilot", model_index=0))
        self.assertEqual(captured["model"], "gpt-4")


    def test_cmd_review_defaults_are_grounded_mode_and_no_verbose_notices(self) -> None:
        # A Namespace without text_only/verbose must behave as the CLI's own
        # defaults do: grounded mode reaches the runner, and the skipped
        # candidate's notice stays uncollected (so cmd_review prints nothing
        # to stderr). quiet=True keeps the absent-pool notice out of the way.
        modes: list[str] = []

        def runner(prompt: str, *, model_index=None, mode="not-dispatched") -> str:
            modes.append(mode)
            return "critique"

        out, err = io.StringIO(), io.StringIO()
        with (
            patch(
                "shutil.which",
                side_effect=lambda b: "/usr/bin/pi" if b == "pi" else None,
            ),
            patch.dict(second_opinion.BACKEND_RUNNERS, {"pi": runner}),
            patch("sys.stdout", out),
            patch("sys.stderr", err),
        ):
            second_opinion.cmd_review(ns(plan="my plan", backend="agy,pi", quiet=True))
        self.assertEqual(modes, ["grounded"])
        self.assertIn("critique", out.getvalue())
        self.assertEqual(err.getvalue(), "")

    def test_round_cap_applies_when_allow_extra_round_is_no_attribute(self) -> None:
        tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmpdir, ignore_errors=True)
        test_layouts.activate_sandbox_home(Path(tmpdir), self.addCleanup)
        plan = Path(tmpdir) / "plan.md"
        plan.write_text("my plan")
        # No allow_extra_round attribute at all: argparse always sets it, so
        # its absence must still mean "no extra rounds".
        args = ns(plan=str(plan), backend="codex", run_id="no-allow-extra")

        def run_once(err: io.StringIO) -> None:
            with (
                patch("shutil.which", return_value="/usr/bin/codex"),
                patch.object(
                    second_opinion,
                    "BACKEND_RUNNERS",
                    {"codex": lambda p, model_index=None: "critique"},
                ),
                redirect_stdout(io.StringIO()),
                redirect_stderr(err),
            ):
                second_opinion.cmd_review(args)

        for _ in range(second_opinion.MAX_ROUNDS):
            run_once(io.StringIO())
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm:
            run_once(err)
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("round cap", err.getvalue())

    def test_prompt_only_sanitizes_plan_debris(self) -> None:
        # --prompt-only prints the prompt review_plan would send, so it strips
        # the same plan debris. The stripped token must be gone and the real
        # content must still be there.
        plan = "# Plan\n## Rejected Feedback\nOld note.\n## Step\nGo."
        out = io.StringIO()
        with patch("shutil.which", return_value=None), patch("sys.stdout", out):
            second_opinion.cmd_review(ns(plan=plan, prompt_only=True))
        printed = out.getvalue()
        self.assertNotIn("Old note.", printed)
        self.assertIn("Go.", printed)


class ReviewPlanTests(unittest.TestCase):
    """Facade-level tests for review_plan(): the programmatic entry point.

    The facade owns sanitize/prompt/size-preflight/candidate-fallback and
    returns structured results; every stderr-bound diagnostic is *collected*
    (ReviewResult.notices / AllBackendsFailedError.notices), never printed by
    the facade itself. cmd_review stays the only printing/exit layer.
    """

    def test_review_plan_success_returns_result_fields(self) -> None:
        captured: list[str] = []
        with (
            patch.dict(os.environ, {"SECOND_OPINION_AGY_MODEL": "Gemini 3.8 Flash (High)"}),
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.object(
                second_opinion,
                "BACKEND_RUNNERS",
                {
                    "agy": lambda p, model_index=None: (
                        captured.append(p) or "the critique"
                    )
                },
            ),
        ):
            result = second_opinion.review_plan(
                second_opinion.ReviewRequest(plan_text="my plan", backend="agy")
            )
        self.assertIsInstance(result, second_opinion.ReviewResult)
        self.assertEqual(result.response_text, "the critique")
        self.assertEqual(result.backend_label, "agy (Gemini 3.8 Flash (High))")
        self.assertEqual(result.bytes_saved, 0)
        self.assertEqual(result.notices, ())
        self.assertEqual(captured, [second_opinion.build_prompt("my plan", None)])

    def test_review_plan_falls_back_to_second_backend(self) -> None:
        with (
            patch.dict(
                os.environ,
                {
                    "SECOND_OPINION_AGY_MODEL": "Gemini 3.8 Flash (High)",
                    "SECOND_OPINION_PI_MODEL": "pi-model",
                },
            ),
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.object(second_opinion, "available_backends", return_value=["agy", "pi"]),
            patch.object(
                second_opinion,
                "BACKEND_RUNNERS",
                {
                    "agy": lambda p, model_index=None: (_ for _ in ()).throw(
                        second_opinion.BackendError("agy broke")
                    ),
                    "pi": lambda p, model_index=None: "pi's critique",
                },
            ),
        ):
            result = second_opinion.review_plan(
                second_opinion.ReviewRequest(plan_text="my plan"), verbose=True
            )
        self.assertEqual(result.response_text, "pi's critique")
        self.assertIn("agy broke", "".join(result.notices))

    def test_review_plan_all_fail_raises_all_backends_failed(self) -> None:
        agy_exc = second_opinion.BackendError("agy broke")
        pi_exc = second_opinion.BackendError("pi broke")
        with (
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.object(second_opinion, "available_backends", return_value=["agy", "pi"]),
            patch.object(
                second_opinion,
                "BACKEND_RUNNERS",
                {
                    "agy": lambda p, model_index=None: (_ for _ in ()).throw(agy_exc),
                    "pi": lambda p, model_index=None: (_ for _ in ()).throw(pi_exc),
                },
            ),
        ):
            with self.assertRaises(second_opinion.AllBackendsFailedError) as cm:
                second_opinion.review_plan(
                    second_opinion.ReviewRequest(plan_text="my plan")
                )
        exc = cm.exception
        # The exhaustion error IS a BackendError subclass, surfacing backend
        # failures through the facade unconverted (audit contract).
        self.assertIsInstance(exc, llm_backends.BackendError)
        self.assertIn("all backends failed — ", str(exc))
        self.assertIn("agy broke", str(exc))
        self.assertIn("pi broke", str(exc))
        self.assertEqual(exc.errors, (agy_exc, pi_exc))
        self.assertEqual(
            exc.failures,
            ("agy: agy broke", "pi: pi broke"),
        )
        self.assertFalse(exc.size_rule_out)

    def test_review_plan_surfaces_backend_error_subclass_single_backend(self) -> None:
        timeout_exc = llm_backends.BackendTimeoutError("pi timed out")
        with (
            patch("shutil.which", return_value="/usr/bin/pi"),
            patch.object(
                second_opinion,
                "BACKEND_RUNNERS",
                {
                    "pi": lambda p, model_index=None: (_ for _ in ()).throw(
                        timeout_exc
                    )
                },
            ),
        ):
            with self.assertRaises(llm_backends.BackendError) as cm:
                second_opinion.review_plan(
                    second_opinion.ReviewRequest(plan_text="my plan", backend="pi")
                )
        exc = cm.exception
        self.assertIsInstance(exc, second_opinion.AllBackendsFailedError)
        self.assertIn("pi timed out", str(exc))
        self.assertIn(
            "raise SECOND_OPINION_PI_TIMEOUT_SECONDS", "".join(exc.failures)
        )
        self.assertEqual(exc.errors, (timeout_exc,))

    def test_review_plan_emits_nothing_to_stderr(self) -> None:
        err = io.StringIO()
        with (
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.object(
                second_opinion,
                "BACKEND_RUNNERS",
                {"agy": lambda p, model_index=None: "ok"},
            ),
            patch("sys.stderr", err),
        ):
            second_opinion.review_plan(
                second_opinion.ReviewRequest(
                    plan_text="# Plan\n## Rejected Feedback\nold\n## Step\ngo"
                )
            )
        self.assertEqual(err.getvalue(), "")

    def test_review_plan_collects_debris_notice(self) -> None:
        with (
            patch.dict(os.environ, {"SECOND_OPINION_AGY_MODEL": "Gemini 3.8 Flash (High)"}),
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.object(
                second_opinion,
                "BACKEND_RUNNERS",
                {"agy": lambda p, model_index=None: "ok"},
            ),
        ):
            result = second_opinion.review_plan(
                second_opinion.ReviewRequest(
                    plan_text="# Plan\n## Rejected Feedback\nold\n## Step\ngo"
                )
            )
        self.assertEqual(len(result.notices), 1)
        self.assertIn("stripped inline review debris from plan", result.notices[0])
        self.assertIn("bytes saved", result.notices[0])
        self.assertGreater(result.bytes_saved, 0)

    def test_review_plan_quiet_suppresses_absent_config_notice(self) -> None:
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.object(
                second_opinion,
                "BACKEND_RUNNERS",
                {"agy": lambda p, model_index=None: "ok"},
            ),
        ):
            quiet_result = second_opinion.review_plan(
                second_opinion.ReviewRequest(plan_text="my plan"), quiet=True
            )
            loud_result = second_opinion.review_plan(
                second_opinion.ReviewRequest(plan_text="my plan")
            )
        self.assertEqual(quiet_result.notices, ())
        self.assertEqual(len(loud_result.notices), 1)
        self.assertIn("no model pool configured", loud_result.notices[0])

    def test_review_plan_threads_model_index_to_runner(self) -> None:
        seen: list[int | None] = []
        with (
            patch.dict(
                os.environ,
                {"SECOND_OPINION_AGY_MODEL_POOL": "model-a,model-b"},
            ),
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.object(
                second_opinion,
                "BACKEND_RUNNERS",
                {
                    "agy": lambda p, model_index=None: (
                        seen.append(model_index) or "ok"
                    )
                },
            ),
        ):
            result = second_opinion.review_plan(
                second_opinion.ReviewRequest(
                    plan_text="my plan", backend="agy", model_index=1
                )
            )
        self.assertEqual(seen, [1])
        self.assertIn("model-b", result.backend_label)

    def test_review_plan_no_backend_available_raises(self) -> None:
        with (
            patch("shutil.which", return_value=None),
            self.assertRaises(second_opinion.NoBackendAvailableError) as cm,
        ):
            second_opinion.review_plan(
                second_opinion.ReviewRequest(plan_text="my plan")
            )
        self.assertIn("no backend available", str(cm.exception))

    def test_review_plan_unknown_forced_backend_raises(self) -> None:
        with (
            patch("shutil.which", return_value=None),
            self.assertRaises(second_opinion.UnknownBackendError) as cm,
        ):
            second_opinion.review_plan(
                second_opinion.ReviewRequest(plan_text="my plan", backend="agy")
            )
        self.assertIn("agy not found on PATH", str(cm.exception))

    def test_review_plan_model_index_config_error_raises_before_runner(self) -> None:
        called: list[str] = []
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.object(
                second_opinion,
                "BACKEND_RUNNERS",
                {
                    "agy": lambda p, model_index=None: called.append("agy") or "ok",
                    "pi": lambda p, model_index=None: called.append("pi") or "ok",
                },
            ),
        ):
            with self.assertRaises(second_opinion.ModelIndexConfigError) as cm:
                second_opinion.review_plan(
                    second_opinion.ReviewRequest(plan_text="my plan", model_index=0)
                )
        self.assertIn("--model-index 0 requires", str(cm.exception))
        # Automatic selection must stop on the first config error, never
        # silently skipping to the next candidate.
        self.assertEqual(called, [])

    def test_review_plan_global_size_rule_out(self) -> None:
        with (
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.object(
                second_opinion,
                "BACKEND_RUNNERS",
                {"agy": lambda p, model_index=None: "never"},
            ),
        ):
            with self.assertRaises(second_opinion.AllBackendsFailedError) as cm:
                second_opinion.review_plan(
                    second_opinion.ReviewRequest(plan_text="p" * 105000)
                )
        exc = cm.exception
        self.assertTrue(exc.size_rule_out)
        self.assertIn("all backends ruled out by payload size", str(exc))
        self.assertNotIn("never", str(exc))

    def test_review_plan_verbose_collects_pi_preflight_skip_notice(self) -> None:
        pi_called: list[str] = []
        with (
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.object(
                second_opinion,
                "available_backends",
                return_value=["pi", "agy"],
            ),
            patch.object(
                second_opinion,
                "BACKEND_RUNNERS",
                {
                    "pi": lambda p, model_index=None: pi_called.append("pi") or "x",
                    "agy": lambda p, model_index=None: "agy critique",
                },
            ),
        ):
            result = second_opinion.review_plan(
                second_opinion.ReviewRequest(plan_text="p" * 15000), verbose=True
            )
        self.assertEqual(pi_called, [])
        self.assertEqual(result.response_text, "agy critique")
        skip_notices = [n for n in result.notices if "skipped:" in n]
        self.assertEqual(len(skip_notices), 1)
        self.assertIn("exceeds pi limit", skip_notices[0])

    def test_focus_hints_blank_variants_omit_section(self) -> None:
        captured: list[str] = []
        with (
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.object(
                second_opinion,
                "BACKEND_RUNNERS",
                {
                    "agy": lambda p, model_index=None: (
                        captured.append(p) or "ok"
                    )
                },
            ),
        ):
            prompts = []
            for hints in (None, "", "   ", "\n"):
                second_opinion.review_plan(
                    second_opinion.ReviewRequest(
                        plan_text="my plan", focus_hints=hints, backend="agy"
                    )
                )
                prompts.append(captured[-1])
        self.assertEqual(len(set(prompts)), 1)
        self.assertEqual(prompts[0], second_opinion.build_prompt("my plan", None))

    def test_default_call_keeps_notices_empty_on_a_skipped_candidate(self) -> None:
        # A list-mode entry missing on PATH is skipped (the installed one
        # answers), and with the default non-verbose call it produces no
        # notice at all — that default is what keeps an automated caller's
        # notice tuple empty.
        with (
            patch(
                "shutil.which",
                side_effect=lambda b: "/usr/bin/pi" if b == "pi" else None,
            ),
            patch.object(
                second_opinion,
                "BACKEND_RUNNERS",
                {"pi": lambda p, model_index=None: "critique"},
            ),
        ):
            result = second_opinion.review_plan(
                second_opinion.ReviewRequest(plan_text="my plan", backend="agy,pi"),
                quiet=True,
            )
        self.assertEqual(result.response_text, "critique")
        self.assertEqual(result.backend_label, second_opinion.BACKEND_LABELS["pi"])
        self.assertEqual(result.notices, ())

    def test_every_candidate_ruled_out_by_size_is_flagged_as_such(self) -> None:
        # The per-candidate exhaustion path (not the global CLI-limit path)
        # must report a size rule-out, not a generic outage.
        def runner(prompt: str, *, model_index=None) -> str:
            raise llm_backends.BackendPayloadSizeError("pi payload too large")

        with (
            patch("shutil.which", return_value="/usr/bin/pi"),
            patch.object(second_opinion, "BACKEND_RUNNERS", {"pi": runner}),
        ):
            with self.assertRaises(second_opinion.AllBackendsFailedError) as cm:
                second_opinion.review_plan(
                    second_opinion.ReviewRequest(plan_text="my plan", backend="pi")
                )
        self.assertIs(cm.exception.size_rule_out, True)


class DefaultFallbackNoticeTests(unittest.TestCase):
    """The absent-config contract (decided 2026-09-03): a review that falls
    back to a backend's default model announces it on stderr, names the pool
    var, where to set it, and a realistic example; --quiet suppresses it; any
    configured pool or single override keeps it silent."""

    def _run_review(
        self, backend: str, argv_env: dict[str, str], ns_kwargs: dict | None = None
    ) -> tuple[str, str]:
        out, err = io.StringIO(), io.StringIO()
        with (
            patch.dict(os.environ, argv_env, clear=True),
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.object(
                second_opinion,
                "BACKEND_RUNNERS",
                {backend: lambda p, model_index=None: "critique text"},
            ),
            patch("sys.stdout", out),
            patch("sys.stderr", err),
        ):
            second_opinion.cmd_review(
                ns(plan="my plan", backend=backend, **(ns_kwargs or {}))
            )
        return out.getvalue(), err.getvalue()

    def test_49a_default_fallback_announces_pool_var_where_and_example(self) -> None:
        _, err = self._run_review("agy", {})
        self.assertIn("SECOND_OPINION_AGY_MODEL_POOL", err)
        self.assertIn("default model", err)
        self.assertIn("export SECOND_OPINION_AGY_MODEL_POOL=", err)
        self.assertIn("~/.zshrc", err)

    def test_49b_configured_pool_is_silent(self) -> None:
        _, err = self._run_review(
            "agy", {"SECOND_OPINION_AGY_MODEL_POOL": "Gemini 3.8 Flash (High)"}
        )
        self.assertNotIn("no model pool configured", err)

    def test_49c_single_override_is_silent(self) -> None:
        _, err = self._run_review(
            "pi", {"SECOND_OPINION_PI_MODEL": "opencode-go/glm-5.2"}
        )
        self.assertNotIn("no model pool configured", err)

    def test_49d_quiet_suppresses_the_notice(self) -> None:
        _, err = self._run_review("pi", {}, {"quiet": True})
        self.assertNotIn("no model pool configured", err)

    def test_49e_copilot_notice_names_tier_requirement(self) -> None:
        _, err = self._run_review("copilot", {})
        self.assertIn("Pro or Enterprise", err)

    def test_49f_each_dispatched_backend_gets_its_own_notice(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.object(
                second_opinion,
                "BACKEND_RUNNERS",
                {
                    "agy": lambda p, model_index=None: (_ for _ in ()).throw(
                        second_opinion.BackendError("agy broke")
                    ),
                    "pi": lambda p, model_index=None: "critique text",
                },
            ),
            patch("sys.stdout", out),
            patch("sys.stderr", err),
        ):
            second_opinion.cmd_review(ns(plan="my plan"))
        err = err.getvalue()
        self.assertIn("SECOND_OPINION_AGY_MODEL_POOL", err)
        self.assertIn("SECOND_OPINION_PI_MODEL_POOL", err)

    def test_49g_absent_pool_config_helper_is_pure(self) -> None:
        env = {"SECOND_OPINION_PI_MODEL_POOL": "m1"}
        # The contract is a real bool, not merely something falsy: callers may
        # store or compare the value directly.
        self.assertIs(second_opinion._absent_pool_config("pi", env), False)
        self.assertIs(second_opinion._absent_pool_config("pi", {}), True)
        self.assertIs(second_opinion._absent_pool_config("copilot", {}), True)
        self.assertIs(second_opinion._absent_pool_config("not-a-backend", {}), False)


class DieTests(unittest.TestCase):
    def test_47_die_prints_prefixed_message_and_exits_1(self) -> None:
        err = io.StringIO()
        with self.assertRaises(SystemExit) as cm, patch("sys.stderr", err):
            second_opinion.die("something went wrong")
        self.assertEqual(cm.exception.code, 1)
        self.assertEqual(
            err.getvalue().strip(), "[second_opinion] something went wrong"
        )


class HandleTerminationTests(unittest.TestCase):
    def test_48_kills_active_process_and_exits_with_128_plus_signum(self) -> None:
        with (
            patch.object(second_opinion, "_kill_active_process") as mock_kill,
            self.assertRaises(SystemExit) as cm,
        ):
            second_opinion._handle_termination(15, None)
        mock_kill.assert_called_once()
        self.assertEqual(cm.exception.code, 128 + 15)


class ParserVerbosityTests(unittest.TestCase):
    def test_flags_parse_after_every_leaf_subcommand(self) -> None:
        # A leaf added later without an entry here silently loses coverage.
        cases = {
            "detect": ("cmd_detect", []),
            "review": ("cmd_review", ["dummy-plan"]),
        }
        for cmd, (target, extra) in cases.items():
            argv = ["second_opinion.py", cmd, *extra, "-q"]
            with (
                patch.object(second_opinion, target) as mock_cmd,
                patch.object(sys, "argv", argv),
            ):
                second_opinion.main()
            self.assertTrue(mock_cmd.call_args.args[0].quiet)


class ModelIndexParsingTests(unittest.TestCase):
    """--model-index validation, tested directly against the parser
    (build_parser) rather than only live -- see Verification steps."""

    def test_65_negative_index_rejected(self) -> None:
        parser = second_opinion.build_parser()
        err = io.StringIO()
        with patch("sys.stderr", err), self.assertRaises(SystemExit) as cm:
            parser.parse_args(["review", "plan.md", "--model-index", "-1"])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("--model-index", err.getvalue())

    def test_66_non_integer_index_rejected(self) -> None:
        parser = second_opinion.build_parser()
        err = io.StringIO()
        with patch("sys.stderr", err), self.assertRaises(SystemExit) as cm:
            parser.parse_args(["review", "plan.md", "--model-index", "abc"])
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("--model-index", err.getvalue())

    def test_67_non_negative_index_accepted(self) -> None:
        parser = second_opinion.build_parser()
        args = parser.parse_args(["review", "plan.md", "--model-index", "0"])
        self.assertEqual(args.model_index, 0)

    def test_68_index_omitted_defaults_to_none(self) -> None:
        parser = second_opinion.build_parser()
        args = parser.parse_args(["review", "plan.md"])
        self.assertIsNone(args.model_index)


class ParserDefaultsTests(unittest.TestCase):
    """The parser's declared boolean defaults and required flags are part of
    the CLI contract: a boolean flag defaults off, and a required flag refuses
    to parse without it."""

    def test_boolean_flags_default_off(self) -> None:
        parser = second_opinion.build_parser()
        review = parser.parse_args(["review", "plan.md"])
        self.assertIs(review.text_only, False)
        self.assertIs(review.allow_extra_round, False)
        panel = parser.parse_args(["panel", "plan.md", "--backends", "pi,agy"])
        self.assertIs(panel.text_only, False)
        self.assertIs(panel.allow_extra_round, False)
        worker = parser.parse_args(
            ["panel-worker", "--backend", "pi", "--prompt-file", "/tmp/p"]
        )
        self.assertIs(worker.text_only, False)

    def test_required_flags_still_refuse_an_incomplete_invocation(self) -> None:
        parser = second_opinion.build_parser()
        for argv in (
            ["panel", "plan.md"],
            ["panel-worker", "--backend", "pi"],
            ["panel-worker", "--prompt-file", "/tmp/p"],
        ):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as cm:
                parser.parse_args(argv)
            self.assertEqual(cm.exception.code, 2)


class ParsePositiveIntTests(unittest.TestCase):
    """Direct tests of _parse_positive_int -- the pure hardening/clamping
    logic shared by the global default and every per-backend override."""

    def test_70_blank_falls_back_to_default(self) -> None:
        self.assertEqual(second_opinion._parse_positive_int("", 120), 120)

    def test_71_whitespace_only_falls_back_to_default(self) -> None:
        self.assertEqual(second_opinion._parse_positive_int("   ", 120), 120)

    def test_72_non_integer_falls_back_to_default(self) -> None:
        self.assertEqual(second_opinion._parse_positive_int("abc", 120), 120)

    def test_73_zero_falls_back_to_default(self) -> None:
        self.assertEqual(second_opinion._parse_positive_int("0", 120), 120)

    def test_74_negative_falls_back_to_default(self) -> None:
        self.assertEqual(second_opinion._parse_positive_int("-5", 120), 120)

    def test_75_valid_value_below_cap_returned_as_is(self) -> None:
        self.assertEqual(second_opinion._parse_positive_int("90", 120), 90)

    def test_76_value_above_cap_clamped(self) -> None:
        self.assertEqual(
            second_opinion._parse_positive_int("999999", 120),
            second_opinion._MAX_BACKEND_TIMEOUT_SECONDS,
        )

    def test_77_surrounding_whitespace_stripped_then_parsed(self) -> None:
        self.assertEqual(second_opinion._parse_positive_int(" 200 ", 120), 200)

    def test_78_leading_plus_accepted(self) -> None:
        self.assertEqual(second_opinion._parse_positive_int("+120", 90), 120)

    def test_79_default_itself_clamped_to_max(self) -> None:
        self.assertEqual(
            second_opinion._parse_positive_int("", 999999),
            second_opinion._MAX_BACKEND_TIMEOUT_SECONDS,
        )


class ResolveTimeoutTests(unittest.TestCase):
    """Direct tests of _resolve_timeout -- reads its env var fresh per call,
    unlike the module-latched global BACKEND_TIMEOUT_SECONDS."""

    def test_80_unset_falls_back_to_global(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SECOND_OPINION_AGY_TIMEOUT_SECONDS", None)
            with patch.object(second_opinion, "BACKEND_TIMEOUT_SECONDS", 120):
                self.assertEqual(
                    second_opinion._resolve_timeout(
                        "SECOND_OPINION_AGY_TIMEOUT_SECONDS"
                    ),
                    120,
                )

    def test_81_valid_value_overrides_global(self) -> None:
        with (
            patch.dict(os.environ, {"SECOND_OPINION_AGY_TIMEOUT_SECONDS": "45"}),
            patch.object(second_opinion, "BACKEND_TIMEOUT_SECONDS", 120),
        ):
            self.assertEqual(
                second_opinion._resolve_timeout("SECOND_OPINION_AGY_TIMEOUT_SECONDS"),
                45,
            )

    def test_82_invalid_value_falls_back_to_global(self) -> None:
        with (
            patch.dict(os.environ, {"SECOND_OPINION_AGY_TIMEOUT_SECONDS": "-5"}),
            patch.object(second_opinion, "BACKEND_TIMEOUT_SECONDS", 120),
        ):
            self.assertEqual(
                second_opinion._resolve_timeout("SECOND_OPINION_AGY_TIMEOUT_SECONDS"),
                120,
            )


class OpencodeTimeoutFloorTests(unittest.TestCase):
    """opencode's per-backend timeout floor (2026-09-07): measured opencode
    critique latency is 200-270s per real plan regardless of payload size
    (see meta-agent-toolkit-reconcile-drift-spec-critique-notes.md), so the
    flat 120s default timed out on essentially every real critique and the
    failure read as a backend outage. The floor is a floor: a lower global
    default (explicit or not) must not drop opencode below it, an explicit
    per-backend override still wins, and everything stays clamped to the
    hard ceiling."""

    def _clear_timeout_vars(self) -> None:
        for var in (
            "SECOND_OPINION_TIMEOUT_SECONDS",
            "SECOND_OPINION_AGY_TIMEOUT_SECONDS",
            "SECOND_OPINION_OPENCODE_TIMEOUT_SECONDS",
            "SECOND_OPINION_PI_TIMEOUT_SECONDS",
            "SECOND_OPINION_COPILOT_TIMEOUT_SECONDS",
        ):
            os.environ.pop(var, None)

    def test_86_ceiling_is_600(self) -> None:
        self.assertEqual(second_opinion._MAX_BACKEND_TIMEOUT_SECONDS, 600)

    def test_87_opencode_unset_gets_450_floor_not_global_120(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            self._clear_timeout_vars()
            with patch.object(second_opinion, "BACKEND_TIMEOUT_SECONDS", 120):
                capture, box = RunOpencodeTests()._capture_cmd()
                with patch.object(second_opinion, "_run_command", side_effect=capture):
                    second_opinion.run_opencode("prompt")
                self.assertEqual(box["timeout"], 450)

    def test_88_floor_beats_lower_explicit_global(self) -> None:
        """A raised global default (e.g. 200s) still must not drop opencode
        below its measured-latency floor -- a 200s budget is the original
        bug for this backend."""
        with patch.dict(os.environ, {}, clear=False):
            self._clear_timeout_vars()
            with patch.object(second_opinion, "BACKEND_TIMEOUT_SECONDS", 200):
                capture, box = RunOpencodeTests()._capture_cmd()
                with patch.object(second_opinion, "_run_command", side_effect=capture):
                    second_opinion.run_opencode("prompt")
                self.assertEqual(box["timeout"], 450)

    def test_89_explicit_override_wins_over_floor(self) -> None:
        with patch.dict(os.environ, {"SECOND_OPINION_OPENCODE_TIMEOUT_SECONDS": "60"}):
            with patch.object(second_opinion, "BACKEND_TIMEOUT_SECONDS", 120):
                capture, box = RunOpencodeTests()._capture_cmd()
                with patch.object(second_opinion, "_run_command", side_effect=capture):
                    second_opinion.run_opencode("prompt")
                self.assertEqual(box["timeout"], 60)

    def test_89b_override_above_ceiling_clamped(self) -> None:
        with patch.dict(os.environ, {"SECOND_OPINION_OPENCODE_TIMEOUT_SECONDS": "700"}):
            capture, box = RunOpencodeTests()._capture_cmd()
            with patch.object(second_opinion, "_run_command", side_effect=capture):
                second_opinion.run_opencode("prompt")
            self.assertEqual(box["timeout"], 600)

    def test_89c_other_backends_keep_120_default(self) -> None:
        """The 120s fallback is text-only-only for agy/copilot (their grounded
        floor lives in the runner's explicit default, not in _resolve_timeout's
        own fallback), and every-mode for pi."""
        with patch.dict(os.environ, {}, clear=False):
            self._clear_timeout_vars()
            with patch.object(second_opinion, "BACKEND_TIMEOUT_SECONDS", 120):
                self.assertEqual(
                    second_opinion._resolve_timeout(
                        "SECOND_OPINION_AGY_TIMEOUT_SECONDS"
                    ),
                    120,
                )
                self.assertEqual(
                    second_opinion._resolve_timeout(
                        "SECOND_OPINION_PI_TIMEOUT_SECONDS"
                    ),
                    120,
                )
                self.assertEqual(
                    second_opinion._resolve_timeout(
                        "SECOND_OPINION_COPILOT_TIMEOUT_SECONDS"
                    ),
                    120,
                )

    def test_89d_resolve_timeout_default_param(self) -> None:
        """The new ``default`` parameter: used as the fallback when the env
        var is unset/invalid, clamped to the ceiling like every other path."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SECOND_OPINION_AGY_TIMEOUT_SECONDS", None)
            self.assertEqual(
                second_opinion._resolve_timeout(
                    "SECOND_OPINION_AGY_TIMEOUT_SECONDS", default=450
                ),
                450,
            )
        with patch.dict(os.environ, {"SECOND_OPINION_AGY_TIMEOUT_SECONDS": "700"}):
            self.assertEqual(
                second_opinion._resolve_timeout(
                    "SECOND_OPINION_AGY_TIMEOUT_SECONDS", default=450
                ),
                600,
            )


class CodexTimeoutFloorTests(unittest.TestCase):
    """codex's second-opinion success-latency FLOOR (E-05, 2026-09-23):

    backend_calls.jsonl (2026-08-31..09-24, n=42) measured p95 114s / p99
    119.6s against the flat 120s default -- a distribution truncated by the
    cap -- and 18 calls timed out at ~120s (2,161s). So codex's unset
    fallback must be max(global, 180), not the raw 120s global, exactly like
    opencode's 450s floor. An explicit override still wins, and a higher
    explicit global still applies."""

    def _capture_llm_backends_codex(
        self,
    ) -> tuple[Callable[..., str], dict[str, object]]:
        """Return a ``llm_backends.run_codex`` side-effect capturing its
        timeout kwarg, plus the box."""
        box: dict[str, object] = {}

        def fake_run_codex(
            prompt: str,
            *,
            model=None,
            timeout=120,
            mode="text-only",
            target_dir=None,
        ):
            box["prompt"] = prompt
            box["timeout"] = timeout
            box["model"] = model
            box["mode"] = mode
            box["target_dir"] = target_dir
            return "codex critique"

        return fake_run_codex, box

    def test_codex_unset_gets_180_floor_not_global_120(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SECOND_OPINION_CODEX_TIMEOUT_SECONDS", None)
            with patch.object(second_opinion, "BACKEND_TIMEOUT_SECONDS", 120):
                fake, box = self._capture_llm_backends_codex()
                with patch.object(second_opinion.llm_backends, "run_codex", side_effect=fake):
                    second_opinion.run_codex("prompt")
                self.assertEqual(box["timeout"], 180)

    def test_codex_floor_beats_lower_explicit_global(self) -> None:
        """A raised global default (e.g. 150s) still must not drop codex
        below its measured-latency floor -- a 150s budget would still sit
        under the 119.6s p99 tail without headroom."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SECOND_OPINION_CODEX_TIMEOUT_SECONDS", None)
            with patch.object(second_opinion, "BACKEND_TIMEOUT_SECONDS", 150):
                fake, box = self._capture_llm_backends_codex()
                with patch.object(second_opinion.llm_backends, "run_codex", side_effect=fake):
                    second_opinion.run_codex("prompt")
                self.assertEqual(box["timeout"], 180)

    def test_codex_explicit_override_wins_over_floor(self) -> None:
        with patch.dict(os.environ, {"SECOND_OPINION_CODEX_TIMEOUT_SECONDS": "90"}):
            with patch.object(second_opinion, "BACKEND_TIMEOUT_SECONDS", 120):
                fake, box = self._capture_llm_backends_codex()
                with patch.object(second_opinion.llm_backends, "run_codex", side_effect=fake):
                    second_opinion.run_codex("prompt")
                self.assertEqual(box["timeout"], 90)

    def test_codex_override_above_ceiling_clamped(self) -> None:
        with patch.dict(os.environ, {"SECOND_OPINION_CODEX_TIMEOUT_SECONDS": "700"}):
            fake, box = self._capture_llm_backends_codex()
            with patch.object(second_opinion.llm_backends, "run_codex", side_effect=fake):
                second_opinion.run_codex("prompt")
            self.assertEqual(box["timeout"], 600)


class GroundedTimeoutFloorTests(unittest.TestCase):
    """grounded-mode timeout FLOOR for agy/copilot (2026-10-08): a grounded
    review of a 57k spec timed out on both backends at the flat 120s default
    and only succeeded at 600. A grounded agy/copilot review with unset env
    vars must therefore fall back to max(global, 600), while text-only keeps
    the flat global and pi stays flat in every mode. An explicit override
    still wins, a higher global still applies, and everything clamps to the
    600 ceiling."""

    def _capture_agy(self) -> tuple[Callable[..., str], dict[str, object]]:
        box: dict[str, object] = {}

        def fake_run_agy(
            prompt: str,
            *,
            model: str,
            timeout: int = 120,
            mode: str = "text-only",
            target_dir=None,
        ) -> str:
            box["timeout"] = timeout
            box["mode"] = mode
            return "agy critique"

        return fake_run_agy, box

    def _capture_copilot(self) -> tuple[Callable[..., str], dict[str, object]]:
        box: dict[str, object] = {}

        def fake_run_copilot(
            prompt: str,
            *,
            model: str | None = None,
            timeout: int = 120,
            mode: str = "text-only",
            target_dir=None,
        ) -> str:
            box["timeout"] = timeout
            box["mode"] = mode
            return "copilot critique"

        return fake_run_copilot, box

    def _capture_pi(self) -> tuple[Callable[..., str], dict[str, object]]:
        box: dict[str, object] = {}

        def fake_run_pi(
            prompt: str,
            *,
            model: str | None = None,
            timeout: int = 120,
            mode: str = "text-only",
            target_dir=None,
        ) -> str:
            box["timeout"] = timeout
            return "pi critique"

        return fake_run_pi, box

    def test_grounded_agy_unset_gets_600(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SECOND_OPINION_AGY_TIMEOUT_SECONDS", None)
            with patch.object(second_opinion, "BACKEND_TIMEOUT_SECONDS", 120):
                fake, box = self._capture_agy()
                with patch.object(
                    second_opinion.llm_backends, "run_agy", side_effect=fake
                ):
                    second_opinion.run_agy("prompt", mode="grounded")
                self.assertEqual(box["timeout"], 600)

    def test_grounded_copilot_unset_gets_600(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SECOND_OPINION_COPILOT_TIMEOUT_SECONDS", None)
            with patch.object(second_opinion, "BACKEND_TIMEOUT_SECONDS", 120):
                fake, box = self._capture_copilot()
                with patch.object(
                    second_opinion.llm_backends, "run_copilot", side_effect=fake
                ):
                    second_opinion.run_copilot("prompt", mode="grounded")
                self.assertEqual(box["timeout"], 600)

    def test_text_only_agy_keeps_global(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SECOND_OPINION_AGY_TIMEOUT_SECONDS", None)
            with patch.object(second_opinion, "BACKEND_TIMEOUT_SECONDS", 120):
                fake, box = self._capture_agy()
                with patch.object(
                    second_opinion.llm_backends, "run_agy", side_effect=fake
                ):
                    second_opinion.run_agy("prompt", mode="text-only")
                self.assertEqual(box["timeout"], 120)

    def test_text_only_copilot_keeps_global(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SECOND_OPINION_COPILOT_TIMEOUT_SECONDS", None)
            with patch.object(second_opinion, "BACKEND_TIMEOUT_SECONDS", 120):
                fake, box = self._capture_copilot()
                with patch.object(
                    second_opinion.llm_backends, "run_copilot", side_effect=fake
                ):
                    second_opinion.run_copilot("prompt", mode="text-only")
                self.assertEqual(box["timeout"], 120)

    def test_pi_grounded_keeps_global(self) -> None:
        """pi is deliberately excluded: size-ruled-out for the large specs
        that motivated this, so its grounded reviews have no measured reason
        to change."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SECOND_OPINION_PI_TIMEOUT_SECONDS", None)
            with patch.object(second_opinion, "BACKEND_TIMEOUT_SECONDS", 120):
                fake, box = self._capture_pi()
                with patch.object(
                    second_opinion.llm_backends, "run_pi", side_effect=fake
                ):
                    second_opinion.run_pi("prompt", mode="grounded")
                self.assertEqual(box["timeout"], 120)

    def test_grounded_agy_explicit_override_wins(self) -> None:
        with patch.dict(os.environ, {"SECOND_OPINION_AGY_TIMEOUT_SECONDS": "60"}):
            with patch.object(second_opinion, "BACKEND_TIMEOUT_SECONDS", 120):
                fake, box = self._capture_agy()
                with patch.object(
                    second_opinion.llm_backends, "run_agy", side_effect=fake
                ):
                    second_opinion.run_agy("prompt", mode="grounded")
                self.assertEqual(box["timeout"], 60)

    def test_grounded_agy_override_above_ceiling_clamped(self) -> None:
        with patch.dict(os.environ, {"SECOND_OPINION_AGY_TIMEOUT_SECONDS": "700"}):
            fake, box = self._capture_agy()
            with patch.object(second_opinion.llm_backends, "run_agy", side_effect=fake):
                second_opinion.run_agy("prompt", mode="grounded")
            self.assertEqual(box["timeout"], 600)

    def test_grounded_floor_beats_lower_global(self) -> None:
        """A raised global (e.g. 300s) still must not drop grounded agy below
        its 600s floor."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SECOND_OPINION_AGY_TIMEOUT_SECONDS", None)
            with patch.object(second_opinion, "BACKEND_TIMEOUT_SECONDS", 300):
                fake, box = self._capture_agy()
                with patch.object(
                    second_opinion.llm_backends, "run_agy", side_effect=fake
                ):
                    second_opinion.run_agy("prompt", mode="grounded")
                self.assertEqual(box["timeout"], 600)


class TimeoutErrorHintTests(unittest.TestCase):
    """A timeout failure must name its escape hatch: which env var raises
    the budget and what the hard ceiling is -- the 2026-09-03 incident read
    as a backend outage because the message said only that attempts timed
    out."""

    def test_89e_timeout_failure_names_override_var_and_ceiling(self) -> None:
        err = io.StringIO()
        with (
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.dict(os.environ, {}, clear=False),
            self.assertRaises(SystemExit) as cm,
            patch("sys.stderr", err),
            patch.object(
                second_opinion.llm_backends,
                "run_agy",
                side_effect=llm_backends.BackendTimeoutError(
                    "timed out after 120s — killed"
                ),
            ),
        ):
            second_opinion.cmd_review(ns(plan="text", backend="agy", text_only=True))
        self.assertEqual(cm.exception.code, 1)
        combined = err.getvalue()
        self.assertIn("SECOND_OPINION_AGY_TIMEOUT_SECONDS", combined)
        self.assertIn("600", combined)


class GroundedTimeoutHintTests(unittest.TestCase):
    """The timeout-failure hint must describe the effective budget honestly:
    a grounded at-ceiling timeout names grounded mode and the text-only
    alternative, a below-ceiling timeout surfaces the effective value and the
    raise-var lever, a text-only at-ceiling timeout offers split/trim, and a
    stall gets a liveness hint rather than budget advice."""

    def _failures(
        self,
        backend: str,
        text_only: bool,
        exc: llm_backends.BackendTimeoutError,
        env: dict[str, str] | None = None,
        diff: bool = False,
    ) -> str:
        env = dict(env or {})
        with (
            patch("shutil.which", return_value=f"/usr/bin/{backend}"),
            patch.dict(os.environ, env, clear=False),
            patch.object(
                second_opinion,
                "BACKEND_RUNNERS",
                {
                    backend: lambda p, model_index=None, mode=None, target_dir=None: (
                        _ for _ in ()
                    ).throw(exc)
                },
            ),
        ):
            var = f"SECOND_OPINION_{backend.upper()}_TIMEOUT_SECONDS"
            if var not in env:
                os.environ.pop(var, None)
            with self.assertRaises(second_opinion.AllBackendsFailedError) as cm:
                second_opinion.review_plan(
                    second_opinion.ReviewRequest(
                        plan_text="my plan",
                        backend=backend,
                        text_only=text_only,
                        diff=diff,
                    )
                )
        return "".join(cm.exception.failures)

    def test_grounded_at_ceiling_names_grounded_and_text_only(self) -> None:
        text = self._failures(
            "agy",
            False,
            llm_backends.BackendTimeoutError("timed out after 600s — killed"),
        )
        self.assertIn("grounded reviews read the codebase", text)
        self.assertIn("--text-only", text)
        self.assertNotIn("SECOND_OPINION_AGY_TIMEOUT_SECONDS", text)

    def test_grounded_low_override_surfaces_effective_and_lever(self) -> None:
        text = self._failures(
            "agy",
            False,
            llm_backends.BackendTimeoutError("timed out after 60s — killed"),
            env={"SECOND_OPINION_AGY_TIMEOUT_SECONDS": "60"},
        )
        self.assertIn("currently 60s", text)
        self.assertIn("grounded, codebase-reading review", text)
        self.assertIn("SECOND_OPINION_AGY_TIMEOUT_SECONDS", text)
        self.assertNotIn("timed out at the 600s", text)

    def test_text_only_at_ceiling_advises_split(self) -> None:
        text = self._failures(
            "agy",
            True,
            llm_backends.BackendTimeoutError("timed out after 600s — killed"),
            env={"SECOND_OPINION_AGY_TIMEOUT_SECONDS": "600"},
        )
        self.assertIn("split or trimmed plan", text)
        self.assertNotIn("--text-only", text)

    def test_diff_at_ceiling_advises_narrow_not_text_only(self) -> None:
        """A grounded --diff review must not be told to drop to --text-only:
        that would unground the review and risk a false "No findings" from the
        diff text alone."""
        text = self._failures(
            "agy",
            False,
            llm_backends.BackendTimeoutError("timed out after 600s — killed"),
            diff=True,
        )
        self.assertIn("narrow the diff scope", text)
        self.assertIn("--diff-path", text)
        self.assertNotIn("retry with --text-only", text)

    def test_stall_gets_liveness_hint(self) -> None:
        text = self._failures(
            "opencode",
            False,
            llm_backends.BackendTimeoutError(
                "stalled after 90s with no output — killed", stalled=True
            ),
        )
        self.assertIn("silent output stall", text)
        self.assertNotIn("raise SECOND_OPINION", text)


class PerBackendTimeoutIsolationTests(unittest.TestCase):
    """Cross-contamination checks run through the real run_agy/run_opencode/
    run_copilot call sites -- the only place a wrong literal env-var-name
    string typo'd into one call site would actually be caught."""

    def _clear_all_timeout_vars(self) -> None:
        for var in (
            "SECOND_OPINION_TIMEOUT_SECONDS",
            "SECOND_OPINION_AGY_TIMEOUT_SECONDS",
            "SECOND_OPINION_OPENCODE_TIMEOUT_SECONDS",
            "SECOND_OPINION_PI_TIMEOUT_SECONDS",
            "SECOND_OPINION_COPILOT_TIMEOUT_SECONDS",
        ):
            os.environ.pop(var, None)

    def test_83_agy_override_does_not_leak_into_copilot_or_opencode(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            self._clear_all_timeout_vars()
            os.environ["SECOND_OPINION_AGY_TIMEOUT_SECONDS"] = "45"
            with patch.object(second_opinion, "BACKEND_TIMEOUT_SECONDS", 120):
                with patch.object(
                    second_opinion.llm_backends, "run_agy", return_value="c"
                ) as mock_agy:
                    second_opinion.run_agy("prompt")
                mock_agy.assert_called_once_with(
                    "prompt", model=second_opinion.DEFAULT_AGY_MODEL, timeout=45
                )

                with patch.object(
                    second_opinion.llm_backends, "run_copilot", return_value="c"
                ) as mock_copilot:
                    second_opinion.run_copilot("prompt")
                mock_copilot.assert_called_once_with("prompt", model=None, timeout=120)

                capture, box = RunOpencodeTests()._capture_cmd()
                with patch.object(second_opinion, "_run_command", side_effect=capture):
                    second_opinion.run_opencode("prompt")
                # 2026-09-07: opencode's unset fallback is its 450s
                # measured-latency floor, not the 120s global (see
                # OpencodeTimeoutFloorTests).
                self.assertEqual(box["timeout"], 450)

    def test_84_opencode_override_does_not_leak_into_agy_or_copilot(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            self._clear_all_timeout_vars()
            os.environ["SECOND_OPINION_OPENCODE_TIMEOUT_SECONDS"] = "60"
            with patch.object(second_opinion, "BACKEND_TIMEOUT_SECONDS", 120):
                capture, box = RunOpencodeTests()._capture_cmd()
                with patch.object(second_opinion, "_run_command", side_effect=capture):
                    second_opinion.run_opencode("prompt")
                self.assertEqual(box["timeout"], 60)

                with patch.object(
                    second_opinion.llm_backends, "run_agy", return_value="c"
                ) as mock_agy:
                    second_opinion.run_agy("prompt")
                mock_agy.assert_called_once_with(
                    "prompt", model=second_opinion.DEFAULT_AGY_MODEL, timeout=120
                )

                with patch.object(
                    second_opinion.llm_backends, "run_copilot", return_value="c"
                ) as mock_copilot:
                    second_opinion.run_copilot("prompt")
                mock_copilot.assert_called_once_with("prompt", model=None, timeout=120)

    def test_84b_pi_override_does_not_leak_into_agy_or_copilot(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            self._clear_all_timeout_vars()
            os.environ["SECOND_OPINION_PI_TIMEOUT_SECONDS"] = "90"
            with patch.object(second_opinion, "BACKEND_TIMEOUT_SECONDS", 120):
                with patch.object(
                    second_opinion.llm_backends, "run_pi", return_value="c"
                ) as mock_pi:
                    second_opinion.run_pi("prompt")
                mock_pi.assert_called_once_with("prompt", model=None, timeout=90)

                with patch.object(
                    second_opinion.llm_backends, "run_agy", return_value="c"
                ) as mock_agy:
                    second_opinion.run_agy("prompt")
                mock_agy.assert_called_once_with(
                    "prompt", model=second_opinion.DEFAULT_AGY_MODEL, timeout=120
                )

                with patch.object(
                    second_opinion.llm_backends, "run_copilot", return_value="c"
                ) as mock_copilot:
                    second_opinion.run_copilot("prompt")
                mock_copilot.assert_called_once_with("prompt", model=None, timeout=120)


class BackendTimeoutDefaultSubprocessTest(unittest.TestCase):
    """Regression-tests the literal default-argument value against a fresh
    interpreter, since BACKEND_TIMEOUT_SECONDS is latched at import time and
    pytest imports test modules at collection time -- before any fixture
    runs -- so in-process env patching can't cover this. Spawns only the
    Python interpreter itself; no backend CLI or production path involved."""

    @pytest.mark.allow_real_subprocess
    def test_85_default_is_120_with_no_env_override(self) -> None:
        env = dict(os.environ)
        env.pop("SECOND_OPINION_TIMEOUT_SECONDS", None)
        script_dir = str(Path(__file__).resolve().parent.parent / "agent-scripts")
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import second_opinion; print(second_opinion.BACKEND_TIMEOUT_SECONDS)",
            ],
            env={**env, "PYTHONPATH": script_dir},
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "120")


class SanitizePlanTextTests(unittest.TestCase):
    def test_90_strips_whitelisted_headings_and_content(self) -> None:
        plan = (
            "# Main Plan\n"
            "Here is the architecture.\n\n"
            "## Rejected Feedback\n"
            "We decided not to use SQLite.\n"
            "### Sub-note\n"
            "Details on why SQLite was rejected.\n\n"
            "## Implementation Steps\n"
            "1. Do the work.\n"
        )
        sanitized, bytes_saved = second_opinion.sanitize_plan_text(plan)
        self.assertNotIn("Rejected Feedback", sanitized)
        self.assertNotIn("SQLite", sanitized)
        self.assertIn("# Main Plan", sanitized)
        self.assertIn("## Implementation Steps", sanitized)
        self.assertIn("1. Do the work.", sanitized)
        self.assertGreater(bytes_saved, 0)
        self.assertEqual(
            bytes_saved, len(plan.encode("utf-8")) - len(sanitized.encode("utf-8"))
        )

    def test_91_preserves_code_fences_with_matching_strings(self) -> None:
        plan = (
            "# My Plan\n"
            "```markdown\n"
            "## Rejected Feedback\n"
            "This is documentation showing what not to do.\n"
            "```\n"
            "~~~python\n"
            "# Critique History\n"
            "def foo(): pass\n"
            "~~~\n"
            "## Next Steps\n"
            "Ship it.\n"
        )
        sanitized, bytes_saved = second_opinion.sanitize_plan_text(plan)
        self.assertIn("## Rejected Feedback", sanitized)
        self.assertIn("# Critique History", sanitized)
        self.assertIn("def foo(): pass", sanitized)
        self.assertEqual(bytes_saved, 0)

    def test_92_preserves_legitimate_architectural_sections(self) -> None:
        plan = (
            "# RFC\n"
            "## Alternatives Considered\n"
            "Alternative A and B were considered.\n\n"
            "## Rejected Alternatives\n"
            "Architecture reasons for rejecting X.\n\n"
            "## Changelog\n"
            "User-facing API changes in v2.\n"
        )
        sanitized, bytes_saved = second_opinion.sanitize_plan_text(plan)
        self.assertIn("## Alternatives Considered", sanitized)
        self.assertIn("## Rejected Alternatives", sanitized)
        self.assertIn("## Changelog", sanitized)
        self.assertEqual(bytes_saved, 0)
        self.assertEqual(sanitized, plan)

    def test_93_whitelisted_heading_at_eof(self) -> None:
        plan = "# Plan\nSpecs.\n## Critique History\nRound 1: fixed bug.\n"
        sanitized, bytes_saved = second_opinion.sanitize_plan_text(plan)
        self.assertNotIn("Critique History", sanitized)
        self.assertNotIn("Round 1", sanitized)
        self.assertIn("# Plan\nSpecs.", sanitized)
        self.assertGreater(bytes_saved, 0)

    def test_94_cmd_review_logs_stripped_bytes_notice(self) -> None:
        plan = "# Plan\n## Rejected Feedback\nOld note.\n## Step\nGo."
        err = io.StringIO()
        out = io.StringIO()
        with (
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.object(
                second_opinion,
                "BACKEND_RUNNERS",
                {"agy": lambda p, model_index=None: "ok"},
            ),
            patch("sys.stderr", err),
            patch("sys.stdout", out),
        ):
            second_opinion.cmd_review(ns(plan=plan, focus_file=None, backend=None))
        self.assertIn(
            "[second_opinion] stripped inline review debris from plan", err.getvalue()
        )
        self.assertIn("bytes saved", err.getvalue())


    def test_95_strips_a_heading_after_a_closed_fence(self) -> None:
        # A fence that closes must end the code block: a whitelisted heading
        # after it is plan prose again, not documentation.
        plan = (
            "# Plan\n"
            "```python\n"
            "print('x')\n"
            "```\n"
            "## Critique History\n"
            "Round 1 notes.\n"
        )
        sanitized, bytes_saved = second_opinion.sanitize_plan_text(plan)
        self.assertIn("print('x')", sanitized)
        self.assertNotIn("Round 1 notes.", sanitized)
        self.assertGreater(bytes_saved, 0)

    def test_96_preserves_a_numbered_line_that_is_not_a_heading(self) -> None:
        # "#1 ..." is a numbered item, not a level-1 heading (set("#1") is not
        # {"#"}), so its title must never be read as a process heading. The
        # real heading in the same run is the positive control.
        numbered = "#1 rejected feedback\n"
        sanitized, bytes_saved = second_opinion.sanitize_plan_text(
            "# Plan\n" + numbered
        )
        self.assertIn(numbered, sanitized)
        self.assertEqual(bytes_saved, 0)
        stripped, stripped_bytes = second_opinion.sanitize_plan_text(
            "## Rejected Feedback\nOld.\n"
        )
        self.assertNotIn("Old.", stripped)
        self.assertGreater(stripped_bytes, 0)


class DataDirSelfEnsureTests(unittest.TestCase):
    """second_opinion.py shares grill.py's artifact directory.

    It writes nothing there itself — it reads a --focus-file an agent wrote —
    but it is one of the two entry points that own the directory, so it
    creates it too. That is what lets the skill docs promise the directory
    exists and stop agents running `mkdir -p` defensively.
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        test_layouts.activate_sandbox_home(Path(self.tmpdir), self.addCleanup)
        self.data_dir = agent_toolkit_paths.path_for("decisions")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir)

    def test_ensure_data_dir_creates_missing_directory(self) -> None:
        self.assertFalse(self.data_dir.exists())
        second_opinion.ensure_data_dir()
        self.assertTrue(self.data_dir.is_dir())

    def test_ensure_data_dir_is_idempotent(self) -> None:
        second_opinion.ensure_data_dir()
        marker = self.data_dir / "focus.md"
        marker.write_text("focus")
        second_opinion.ensure_data_dir()
        self.assertEqual(marker.read_text(), "focus")

    def test_directory_created_while_waiting_for_the_lock_is_tolerated(self) -> None:
        # Two processes can both see no directory and serialize on the
        # migration lock; whichever wins creates it, and the other's mkdir
        # must tolerate finding it there instead of failing.
        events: list[str] = []
        data_dir = self.data_dir

        @contextmanager
        def racing_shared(site: str) -> Iterator[None]:
            events.append(f"enter:{site}")
            data_dir.mkdir(parents=True, exist_ok=True)
            yield
            events.append("exit")

        self.assertFalse(data_dir.exists())
        with patch.object(second_opinion.migration_lock, "shared", racing_shared):
            second_opinion.ensure_data_dir()
        self.assertEqual(events, ["enter:decisions", "exit"])
        self.assertTrue(data_dir.is_dir())
        # Positive control: with no competing writer, the same call must do the
        # creating itself — the fake lock above is what made it a no-op here.
        shutil.rmtree(data_dir)
        self.assertFalse(data_dir.exists())
        second_opinion.ensure_data_dir()
        self.assertTrue(data_dir.is_dir())

    def test_data_dir_matches_grills(self) -> None:
        """A second location would defeat the point — assert they are one dir."""
        sys.path.insert(0, str(Path(second_opinion.__file__).parent))
        import grill

        self.assertEqual(second_opinion._data_dir(), grill._data_dir())

    def test_detect_subcommand_creates_the_directory(self) -> None:
        self.assertFalse(self.data_dir.exists())
        with (
            patch.object(sys, "argv", ["second_opinion.py", "detect"]),
            patch("sys.stdout", io.StringIO()),
            patch("sys.stderr", io.StringIO()),
            patch.object(second_opinion, "cmd_detect"),
        ):
            second_opinion.main()
        self.assertTrue(self.data_dir.is_dir())


class OutputFileTamperTests(unittest.TestCase):
    """The output-file tamper guard, driven through `cmd_review` with a real
    regular file as stdout: a foreign write the tamper check *saw* is trimmed
    away, and one that lands after the check is left alone."""

    def _cmd_review(self, stream: object, runner: Callable[..., str]) -> str:
        out_err = io.StringIO()
        with (
            patch("shutil.which", return_value="/usr/bin/codex"),
            patch.dict(second_opinion.BACKEND_RUNNERS, {"codex": runner}),
            patch("sys.stdout", stream),
            patch("sys.stderr", out_err),
        ):
            second_opinion.cmd_review(
                ns(plan="my plan", backend="codex", quiet=True)
            )
        return out_err.getvalue()

    def test_tail_seen_before_the_check_is_trimmed(self) -> None:
        tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmpdir, ignore_errors=True)
        path = Path(tmpdir) / "out.txt"

        def runner(prompt: str, *, model_index=None) -> str:
            # A foreign writer reaches the file before the tamper check.
            with open(path, "a", encoding="utf-8") as foreign:
                foreign.write("FOREIGN-TAIL" * 20)
            return "critique"

        with open(path, "w", encoding="utf-8") as out_file:
            err = self._cmd_review(out_file, runner)
        text = path.read_text(encoding="utf-8")
        self.assertIn("critique", text)
        self.assertNotIn("FOREIGN-TAIL", text)
        self.assertIn("another process wrote", err)

    def test_tail_written_after_the_check_survives(self) -> None:
        tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmpdir, ignore_errors=True)
        path = Path(tmpdir) / "out.txt"

        class PostCheckWriter:
            """A regular-file stdout that flushes this run's bytes and only
            then lets a separate append-mode descriptor write behind it."""

            def __init__(self, target: Path) -> None:
                self._path = target
                self._file = open(target, "w", encoding="utf-8")

            def fileno(self) -> int:
                return self._file.fileno()

            def write(self, text: str) -> int:
                written = self._file.write(text)
                self._file.flush()
                with open(self._path, "a", encoding="utf-8") as foreign:
                    foreign.write("FOREIGN-TAIL")
                return written

            def flush(self) -> None:
                self._file.flush()

        stream = PostCheckWriter(path)
        self.addCleanup(stream._file.close)  # noqa: SLF001 — the test's own file
        err = self._cmd_review(stream, lambda p, model_index=None: "critique")
        text = path.read_text(encoding="utf-8")
        self.assertIn("critique", text)
        self.assertIn("FOREIGN-TAIL", text)
        self.assertEqual(err, "")


class EnsureRunsDirTests(unittest.TestCase):
    """`_ensure_runs_dir` creates the runs directory when missing and hands
    back the directory it created."""

    def setUp(self) -> None:
        self.tmpdir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.runs_dir = self.tmpdir / "nested" / "runs"
        patcher = patch.object(second_opinion, "_runs_dir", lambda: self.runs_dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_creates_missing_parents_and_returns_the_directory(self) -> None:
        returned = second_opinion._ensure_runs_dir()
        self.assertEqual(returned, self.runs_dir)
        self.assertTrue(self.runs_dir.is_dir())

    def test_directory_created_by_another_process_is_tolerated(self) -> None:
        # Simulate the interleaving the exist_ok guard exists for: another
        # process creates the directory between this call's is_dir() check and
        # its own mkdir, so that mkdir must tolerate finding it present.
        real_mkdir = Path.mkdir
        runs_dir = self.runs_dir
        raced: list[bool] = []

        def racing_mkdir(
            path_self: Path, *args: object, **kwargs: object
        ) -> None:
            if path_self == runs_dir and not raced:
                raced.append(not path_self.is_dir())
                real_mkdir(path_self, parents=True, exist_ok=True)
            return real_mkdir(path_self, *args, **kwargs)

        with patch.object(Path, "mkdir", racing_mkdir):
            returned = second_opinion._ensure_runs_dir()
        self.assertEqual(raced, [True])
        self.assertEqual(returned, runs_dir)
        self.assertTrue(runs_dir.is_dir())


class CritiqueNotesBindingTests(unittest.TestCase):
    """bind-notes / check-notes tie a critique-notes file to the artifact bytes.

    backlog-item's resume check used to accept the mere existence of the
    notes companion as proof the critique ran, so an edit to the plan made
    after the critique went unnoticed.
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        test_layouts.activate_sandbox_home(Path(self.tmpdir), self.addCleanup)
        self.data_dir = agent_toolkit_paths.path_for("decisions")
        self.work = Path(self.tmpdir) / "work"
        self.work.mkdir()
        self.artifact = self.work / "topic-spec.md"
        self.notes = self.work / "topic-spec-critique-notes.md"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir)

    def _main(self, *argv: str) -> tuple[str, str]:
        out, err = io.StringIO(), io.StringIO()
        with (
            patch.object(sys, "argv", ["second_opinion.py", *argv]),
            patch("sys.stdout", out),
            patch("sys.stderr", err),
        ):
            try:
                second_opinion.main()
            except SystemExit as exc:
                if exc.code not in (0, None):
                    raise
        return out.getvalue(), err.getvalue()

    def test_notes_path_strips_the_suffix(self) -> None:
        self.assertEqual(second_opinion.critique_notes_path(self.artifact), self.notes)
        bare = self.work / "plan"
        self.assertEqual(
            second_opinion.critique_notes_path(bare), self.work / "plan-critique-notes.md"
        )

    def test_status_missing_and_no_artifact(self) -> None:
        self.assertEqual(second_opinion.notes_status(self.artifact), "no-artifact")
        self.artifact.write_text("plan\n")
        self.assertEqual(second_opinion.notes_status(self.artifact), "missing")

    def test_unmarked_notes_are_unbound(self) -> None:
        self.artifact.write_text("plan\n")
        self.notes.write_text("# notes\nround 1\n")
        self.assertEqual(second_opinion.notes_status(self.artifact), "unbound")

    def test_bind_then_current_then_stale_after_edit(self) -> None:
        self.artifact.write_text("plan v1\n")
        self.notes.write_text("# notes\nround 1\n")
        self.assertEqual(second_opinion.bind_notes(self.artifact), self.notes)
        self.assertEqual(second_opinion.notes_status(self.artifact), "current")
        body = self.notes.read_text()
        self.assertEqual(
            body.splitlines()[0],
            f"<!-- critiqued-sha256: {second_opinion.artifact_digest(self.artifact)} -->",
        )
        self.assertTrue(body.endswith("# notes\nround 1\n"))
        self.artifact.write_text("plan v2\n")
        self.assertEqual(second_opinion.notes_status(self.artifact), "stale")

    def test_rebind_replaces_the_marker(self) -> None:
        self.artifact.write_text("v1\n")
        self.notes.write_text("# notes\n")
        second_opinion.bind_notes(self.artifact)
        self.artifact.write_text("v2\n")
        second_opinion.bind_notes(self.artifact)
        body = self.notes.read_text()
        self.assertEqual(body.count("critiqued-sha256"), 1)
        self.assertEqual(second_opinion.notes_status(self.artifact), "current")
        self.assertTrue(body.endswith("# notes\n"))

    def test_marker_not_on_first_line_or_malformed_is_unbound(self) -> None:
        self.artifact.write_text("v1\n")
        digest = second_opinion.artifact_digest(self.artifact)
        self.notes.write_text(f"# notes\n<!-- critiqued-sha256: {digest} -->\n")
        self.assertEqual(second_opinion.notes_status(self.artifact), "unbound")
        self.notes.write_text(f"<!-- critiqued-sha256: {digest[:10]} -->\n")
        self.assertEqual(second_opinion.notes_status(self.artifact), "unbound")

    def test_non_utf8_notes_still_get_a_status_and_survive_a_bind(self) -> None:
        self.artifact.write_text("v1\n")
        self.notes.write_bytes(b"# notes \xff\xfe\n")
        self.assertEqual(second_opinion.notes_status(self.artifact), "unbound")
        second_opinion.bind_notes(self.artifact)
        self.assertEqual(second_opinion.notes_status(self.artifact), "current")
        self.assertTrue(self.notes.read_bytes().endswith(b"# notes \xff\xfe\n"))

    def test_bind_refuses_without_notes_or_artifact(self) -> None:
        with self.assertRaises(FileNotFoundError):
            second_opinion.bind_notes(self.artifact)
        self.artifact.write_text("v1\n")
        with self.assertRaises(FileNotFoundError):
            second_opinion.bind_notes(self.artifact)

    def test_cli_check_prints_status_and_exits_zero_without_data_dir(self) -> None:
        out, _ = self._main("check-notes", str(self.artifact))
        self.assertEqual(out.strip(), "no-artifact")
        self.artifact.write_text("v1\n")
        self.notes.write_text("# notes\n")
        out, _ = self._main("check-notes", str(self.artifact))
        self.assertEqual(out.strip(), "unbound")
        self.assertFalse(self.data_dir.exists())

    def test_cli_bind_prints_notes_path_and_errors_when_missing(self) -> None:
        self.artifact.write_text("v1\n")
        with self.assertRaises(SystemExit) as ctx:
            self._main("bind-notes", str(self.artifact))
        self.assertEqual(ctx.exception.code, 1)
        self.notes.write_text("# notes\n")
        out, _ = self._main("bind-notes", str(self.artifact))
        self.assertEqual(out.strip(), str(self.notes))
        out, _ = self._main("check-notes", str(self.artifact))
        self.assertEqual(out.strip(), "current")
        self.assertFalse(self.data_dir.exists())

class FocusFileTruncationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir)

    def test_focus_file_exceeding_max_bytes_is_truncated_with_warning(self) -> None:
        focus_path = Path(self.tmpdir) / "huge-focus.md"
        focus_path.write_text("x" * 12000)
        captured_prompt = []
        err = io.StringIO()
        with (
            patch("shutil.which", return_value="/usr/bin/agy"),
            patch.object(
                second_opinion,
                "BACKEND_RUNNERS",
                {
                    "agy": lambda p, model_index=None: (
                        captured_prompt.append(p) or "critique"
                    )
                },
            ),
            patch("sys.stderr", err),
        ):
            second_opinion.cmd_review(
                ns(plan="plan text", focus_file=str(focus_path), backend="agy")
            )
        self.assertEqual(len(captured_prompt), 1)
        self.assertIn("warning: --focus-file", err.getvalue())
        self.assertIn("exceeds 8192 bytes; truncated to 8192 bytes", err.getvalue())
        # The hints embedded in prompt should have exactly 8192 'x's
        self.assertIn("x" * 8192, captured_prompt[0])
        self.assertNotIn("x" * 8193, captured_prompt[0])

    def test_focus_file_within_limit_is_not_truncated(self) -> None:
        focus_path = Path(self.tmpdir) / "normal-focus.md"
        focus_path.write_text("short hints")
        captured_prompt = []
        err = io.StringIO()
        with (
            patch("shutil.which", return_value="/usr/bin/agy"),
            patch.object(
                second_opinion,
                "BACKEND_RUNNERS",
                {
                    "agy": lambda p, model_index=None: (
                        captured_prompt.append(p) or "critique"
                    )
                },
            ),
            patch("sys.stderr", err),
        ):
            second_opinion.cmd_review(
                ns(plan="plan text", focus_file=str(focus_path), backend="agy")
            )
        self.assertEqual(len(captured_prompt), 1)
        self.assertNotIn("warning: --focus-file", err.getvalue())
        self.assertIn("short hints", captured_prompt[0])


class PayloadSizePreflightTests(unittest.TestCase):
    def test_preflight_skips_pi_when_prompt_exceeds_14000(self) -> None:
        plan = "p" * 15000
        pi_called = []
        agy_called = []
        with (
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.object(
                second_opinion,
                "available_backends",
                return_value=["pi", "agy"],
            ),
            patch.object(
                second_opinion,
                "BACKEND_RUNNERS",
                {
                    "pi": lambda p, model_index=None: (
                        pi_called.append(p) or "pi critique"
                    ),
                    "agy": lambda p, model_index=None: (
                        agy_called.append(p) or "agy critique"
                    ),
                },
            ),
            patch("sys.stdout", io.StringIO()),
        ):
            second_opinion.cmd_review(ns(plan=plan, backend=None))
        self.assertEqual(pi_called, [])
        self.assertEqual(len(agy_called), 1)

    def test_explicit_backend_pi_fails_when_prompt_exceeds_14000(self) -> None:
        plan = "p" * 15000
        err = io.StringIO()
        with (
            patch("shutil.which", return_value="/usr/bin/pi"),
            patch.object(
                second_opinion,
                "BACKEND_RUNNERS",
                {"pi": lambda p, model_index=None: "ok"},
            ),
            self.assertRaises(SystemExit) as cm,
            patch("sys.stderr", err),
        ):
            second_opinion.cmd_review(ns(plan=plan, backend="pi"))
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("exceeds pi limit (14000 bytes)", err.getvalue())

    def test_all_backends_ruled_out_when_prompt_exceeds_global_limit(self) -> None:
        plan = "p" * 105000
        err = io.StringIO()
        with (
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.object(
                second_opinion,
                "available_backends",
                return_value=["agy", "copilot"],
            ),
            self.assertRaises(SystemExit) as cm,
            patch("sys.stderr", err),
        ):
            second_opinion.cmd_review(ns(plan=plan, backend=None))
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("all backends ruled out by payload size", err.getvalue())


class GroundedReviewTests(unittest.TestCase):
    """Verifies grounded review is the default, --text-only opts out, and --dir works."""

    def test_review_plan_grounded_by_default(self) -> None:
        captured: dict[str, object] = {}

        def fake_runner(prompt: str, *, model_index=None, mode="grounded", target_dir=None):
            captured["prompt"] = prompt
            captured["mode"] = mode
            captured["target_dir"] = target_dir
            return "critique"

        with (
            patch("shutil.which", return_value="/usr/bin/codex"),
            patch.dict(second_opinion.BACKEND_RUNNERS, {"codex": fake_runner}),
        ):
            res = second_opinion.review_plan(
                second_opinion.ReviewRequest(plan_text="inspect code", backend="codex")
            )
        self.assertEqual(captured["mode"], "grounded")
        self.assertEqual(captured["target_dir"], Path.cwd().resolve())
        self.assertIn("read-only access to the codebase", str(captured["prompt"]))

    def test_review_plan_text_only_flag(self) -> None:
        captured: dict[str, object] = {}

        def fake_runner(prompt: str, *, model_index=None, mode="grounded", target_dir=None):
            captured["prompt"] = prompt
            captured["mode"] = mode
            captured["target_dir"] = target_dir
            return "critique"

        with (
            patch("shutil.which", return_value="/usr/bin/codex"),
            patch.dict(second_opinion.BACKEND_RUNNERS, {"codex": fake_runner}),
        ):
            res = second_opinion.review_plan(
                second_opinion.ReviewRequest(
                    plan_text="inspect code", backend="codex", text_only=True
                )
            )
        self.assertEqual(captured["mode"], "text-only")
        self.assertIn("you have no tools", str(captured["prompt"]))
        self.assertNotIn("read-only access to the codebase", str(captured["prompt"]))

    def test_cmd_review_dir_flag(self) -> None:
        captured: dict[str, object] = {}

        def fake_runner(prompt: str, *, model_index=None, mode="grounded", target_dir=None):
            captured["target_dir"] = target_dir
            return "critique"

        custom_dir = Path("/tmp/my-repo")
        with (
            patch("shutil.which", return_value="/usr/bin/codex"),
            patch.dict(second_opinion.BACKEND_RUNNERS, {"codex": fake_runner}),
            patch("sys.stdout", io.StringIO()),
        ):
            second_opinion.cmd_review(
                ns(plan="text", backend="codex", dir=custom_dir, text_only=False)
            )
        self.assertEqual(captured["target_dir"], custom_dir.resolve())

    def test_run_codex_wrapper(self) -> None:
        captured: dict[str, object] = {}

        def fake_run_codex(prompt: str, *, model=None, timeout=120, mode="text-only", target_dir=None):
            captured["prompt"] = prompt
            captured["model"] = model
            captured["mode"] = mode
            captured["target_dir"] = target_dir
            return "codex critique"

        with (
            patch.dict(os.environ, {"SECOND_OPINION_CODEX_MODEL_POOL": "gpt-5,o3"}),
            patch.object(second_opinion.llm_backends, "run_codex", side_effect=fake_run_codex),
        ):
            out = second_opinion.run_codex(
                "my prompt",
                model_index=1,
                mode="grounded",
                target_dir=Path("/repo"),
            )
        self.assertEqual(out, "codex critique")
        self.assertEqual(captured["model"], "o3")
        self.assertEqual(captured["mode"], "grounded")
        self.assertEqual(captured["target_dir"], Path("/repo"))


class RoundCapEnforcementTests(unittest.TestCase):
    """The round cap is enforced in code (cmd_review), keyed by --run-id or the
    resolved plan-file path. A run-id-less loop on the same plan file is still
    caught; inline-text reviews without a run-id stay unenforced.

    This is the fix for a worker that ignored the documented cap and looped 31
    critique rounds on one spec because nothing in the tooling enforced it.
    """

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        test_layouts.activate_sandbox_home(Path(self.tmpdir), self.addCleanup)
        self.data_dir = agent_toolkit_paths.path_for("decisions")
        self.runs_dir = self.data_dir / "second-opinion-runs"

    def tearDown(self) -> None:
        shutil.rmtree(self.tmpdir)

    def _plan_file(self, text: str = "my plan", name: str = "plan.md") -> Path:
        p = Path(self.tmpdir) / name
        p.write_text(text)
        return p

    def _run(
        self,
        plan: object,
        *,
        run_id: str | None = None,
        allow_extra: bool = False,
        expect_die: bool = False,
    ) -> tuple[int, str]:
        args = ns(
            plan=str(plan),
            backend="agy",
            run_id=run_id,
            allow_extra_round=allow_extra,
        )
        out, err = io.StringIO(), io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {}, clear=False))
            stack.enter_context(
                patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}")
            )
            stack.enter_context(
                patch.object(
                    second_opinion,
                    "BACKEND_RUNNERS",
                    {"agy": lambda p, model_index=None: "critique"},
                )
            )
            stack.enter_context(patch("sys.stdout", out))
            stack.enter_context(patch("sys.stderr", err))
            if expect_die:
                with self.assertRaises(SystemExit) as cm:
                    second_opinion.cmd_review(args)
                return cm.exception.code, err.getvalue()
            second_opinion.cmd_review(args)
        return 0, out.getvalue()

    def test_run_id_caps_at_three_then_refuses(self) -> None:
        plan = self._plan_file()
        for _ in range(3):
            code, out = self._run(plan, run_id="run-1")
            self.assertEqual(code, 0)
            self.assertIn("critique", out)
        code, msg = self._run(plan, run_id="run-1", expect_die=True)
        self.assertEqual(code, 1)
        self.assertIn("round cap (3)", msg)

    def test_refusal_message_omits_the_override(self) -> None:
        # A runaway loop must not be able to read the bypass out of the error.
        plan = self._plan_file()
        for _ in range(3):
            self._run(plan, run_id="run-2")
        _code, msg = self._run(plan, run_id="run-2", expect_die=True)
        self.assertNotIn("--allow-extra-round", msg)
        self.assertIn("Do not call review again for this plan", msg)

    def test_allow_extra_round_lifts_the_cap(self) -> None:
        plan = self._plan_file()
        for _ in range(5):
            code, out = self._run(plan, run_id="run-x", allow_extra=True)
            self.assertEqual(code, 0)
            self.assertIn("critique", out)

    def test_plan_path_key_enforces_without_run_id(self) -> None:
        # No run-id, but the same plan *file* every round: the resolved path is
        # the key, so a loop on the same plan file is still caught.
        plan = self._plan_file()
        for _ in range(3):
            code, out = self._run(plan)
            self.assertEqual(code, 0)
            self.assertIn("critique", out)
        code, msg = self._run(plan, expect_die=True)
        self.assertEqual(code, 1)
        self.assertIn("round cap (3)", msg)

    def test_plan_path_key_is_per_file(self) -> None:
        # A different plan file gets its own counter (no false refusal).
        plan_a = self._plan_file("plan A", name="plan_a.md")
        plan_b = self._plan_file("plan B", name="plan_b.md")
        # 3 rounds on A, then a 4th on B must NOT be refused.
        for _ in range(3):
            self._run(plan_a)
        code, out = self._run(plan_b)
        self.assertEqual(code, 0)
        self.assertIn("critique", out)

    def test_inline_text_without_run_id_not_enforced(self) -> None:
        # Inline text (not a file) and no run-id => no stable key => no cap.
        for _ in range(5):
            code, out = self._run("just some inline plan text")
            self.assertEqual(code, 0)
            self.assertIn("critique", out)
        # No state file should have been written for an unkeyed run.
        self.assertFalse(self.runs_dir.exists())

    def test_failed_review_does_not_consume_a_round(self) -> None:
        # A backend outage (AllBackendsFailedError) must not consume a round, so
        # a flaky backend can be retried without --allow-extra-round.
        plan = self._plan_file()
        for _ in range(3):
            args = ns(plan=str(plan), backend="agy", run_id="run-fail")
            err = io.StringIO()
            with (
                patch.dict(os.environ, {}, clear=False),
                patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
                patch.object(
                    second_opinion,
                    "BACKEND_RUNNERS",
                    {
                        "agy": lambda p, model_index=None: (_ for _ in ()).throw(
                            second_opinion.BackendError("agy broke")
                        )
                    },
                ),
                patch("sys.stderr", err),
                self.assertRaises(SystemExit),
            ):
                second_opinion.cmd_review(args)
        # Three failed calls left the counter at zero, so a successful call still
        # works (and is recorded as the first round).
        code, out = self._run(plan, run_id="run-fail")
        self.assertEqual(code, 0)
        self.assertIn("critique", out)
        files = list(self.runs_dir.glob("*.json"))
        self.assertEqual(len(files), 1)
        self.assertEqual(json.loads(files[0].read_text())["count"], 1)

    def test_state_file_written_under_data_dir(self) -> None:
        plan = self._plan_file()
        self._run(plan, run_id="run-9")
        self.assertTrue(self.runs_dir.is_dir())
        files = list(self.runs_dir.glob("*.json"))
        self.assertEqual(len(files), 1)
        data = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["key"], "run:run-9")

    def test_ttl_prunes_old_run_files(self) -> None:
        plan = self._plan_file()
        self._run(plan, run_id="old")
        # Force the one existing run file to be ancient.
        list(self.runs_dir.glob("*.json"))[0].write_text(
            json.dumps({"key": "run:old", "count": 1, "updated": 0.0})
        )
        # A fresh review triggers the prune; only the new run survives.
        self._run(plan, run_id="new")
        remaining = {
            json.loads(p.read_text(encoding="utf-8"))["key"]
            for p in self.runs_dir.glob("*.json")
        }
        self.assertNotIn("run:old", remaining)
        self.assertIn("run:new", remaining)

    def test_path_key_counter_resets_after_six_hours_idle(self) -> None:
        # A plan-path key (no --run-id) must not leak the cap across sessions: a
        # critique of the same file much later is a fresh run. Seed a counter at
        # the cap whose last update was 7h ago; it must be allowed (reset to 0).
        plan = self._plan_file()
        key = "plan:" + str(plan.resolve())
        second_opinion._ensure_runs_dir()
        path = second_opinion._run_state_path(key)
        now = 1_700_000_000.0
        path.write_text(
            json.dumps({"key": key, "count": 3, "updated": now - 7 * 3600})
        )
        with patch.object(second_opinion.time, "time", return_value=now):
            code, out = self._run(plan)
        self.assertEqual(code, 0)
        self.assertIn("critique", out)
        # The stale counter was reset to 0, then this review incremented it to 1.
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["count"], 1)

    def test_path_key_counter_kept_within_six_hours(self) -> None:
        # Within the 6h window the counter keeps accumulating, so a 4th call in a
        # single sitting is still refused.
        plan = self._plan_file()
        key = "plan:" + str(plan.resolve())
        second_opinion._ensure_runs_dir()
        path = second_opinion._run_state_path(key)
        now = 1_700_000_000.0
        path.write_text(
            json.dumps({"key": key, "count": 3, "updated": now - 60})
        )
        with patch.object(second_opinion.time, "time", return_value=now):
            code, msg = self._run(plan, expect_die=True)
        self.assertEqual(code, 1)
        self.assertIn("round cap (3)", msg)


class RoundCapHelperTests(unittest.TestCase):
    """Pure helpers behind the round-cap enforcement."""

    def test_round_count_for_defaults_to_zero(self) -> None:
        self.assertEqual(second_opinion.round_count_for({}), 0)
        self.assertEqual(second_opinion.round_count_for({"count": 2}), 2)

    def test_would_exceed_cap_boundaries(self) -> None:
        self.assertFalse(second_opinion.would_exceed_cap(0))
        self.assertFalse(second_opinion.would_exceed_cap(second_opinion.MAX_ROUNDS - 1))
        self.assertTrue(second_opinion.would_exceed_cap(second_opinion.MAX_ROUNDS))
        self.assertTrue(
            second_opinion.would_exceed_cap(second_opinion.MAX_ROUNDS + 1)
        )

    def test_record_review_increments_and_timestamps(self) -> None:
        state = {"key": "run:x", "count": 2, "updated": 0.0}
        new = second_opinion.record_review(state)
        self.assertEqual(new["count"], 3)
        self.assertGreater(new["updated"], 0.0)
        # Original is untouched (pure).
        self.assertEqual(state["count"], 2)

    def test_compute_round_key_run_id(self) -> None:
        self.assertEqual(
            second_opinion._compute_round_key(
                argparse.Namespace(run_id="abc", plan="p")
            ),
            "run:abc",
        )

    def test_compute_round_key_plan_path(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "plan.md"
            p.write_text("x")
            key = second_opinion._compute_round_key(
                argparse.Namespace(run_id=None, plan=str(p))
            )
            self.assertEqual(key, f"plan:{p.resolve()}")

    def test_compute_round_key_inline_text_is_none(self) -> None:
        self.assertIsNone(
            second_opinion._compute_round_key(
                argparse.Namespace(run_id=None, plan="not a file path")
            )
        )


if __name__ == "__main__":
    test_bootstrap.run_unittest_main(verbosity=1)


class BackendListFallbackTests(unittest.TestCase):
    """--backend accepts a comma-separated list; runtime failures rotate.

    First success wins. A single-name run keeps today's strict
    one-backend-only contract; a list entry that is not installed is
    skipped with a one-line notice. Tool-use transcripts quarantine the
    (backend, model) pair for the process; a fresh process starts clean.
    """

    ENV = {
        "SECOND_OPINION_AGY_MODEL": "Gemini 3.8 Flash (High)",
        "SECOND_OPINION_OPENCODE_MODEL_POOL": "model-a,model-b",
    }

    def setUp(self) -> None:
        # The quarantine set is process-lifetime by design; tests must not
        # leak entries into other tests in the same pytest worker process.
        self.addCleanup(second_opinion._UNFIT_MODELS.clear)

    def _plan(self, **kw: object) -> second_opinion.ReviewRequest:
        defaults: dict[str, object] = {"plan_text": "my plan"}
        defaults.update(kw)
        return second_opinion.ReviewRequest(**defaults)  # type: ignore[arg-type]

    @pytest.mark.regression(
        "explicit-backend-cli-list-normalization",
        "AttributeError: 'list' object has no attribute 'split'",
    )
    def test_cmd_review_normalizes_the_parsed_backend_list(self) -> None:
        args = second_opinion.build_parser().parse_args(
            ["review", "my plan", "--backend", "agy"]
        )
        captured: dict[str, object] = {}

        def fake_review(request, **_kwargs):
            captured["backend"] = request.backend
            return second_opinion.ReviewResult("agy", "critique", 0)

        with (
            patch.object(second_opinion, "review_plan", side_effect=fake_review),
            patch("sys.stdout", io.StringIO()),
        ):
            second_opinion.cmd_review(args)
        self.assertEqual(captured["backend"], "agy")

    def test_list_falls_back_to_second_entry_on_runtime_failure(self) -> None:
        with (
            patch.dict(os.environ, self.ENV),
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.object(
                second_opinion,
                "BACKEND_RUNNERS",
                {
                    "agy": lambda p, model_index=None: (_ for _ in ()).throw(
                        second_opinion.BackendError("agy broke")
                    ),
                    "opencode": lambda p, model_index=None: "opencode's critique",
                },
            ),
        ):
            result = second_opinion.review_plan(
                self._plan(backend="agy,opencode"), verbose=True
            )
        self.assertEqual(result.response_text, "opencode's critique")
        self.assertIn("agy broke", "".join(result.notices))

    def test_list_skips_entry_not_on_path_with_notice(self) -> None:
        err = io.StringIO()
        with (
            patch.dict(os.environ, self.ENV),
            patch(
                "shutil.which",
                side_effect=lambda b: f"/usr/bin/{b}" if b == "opencode" else None,
            ),
            patch.object(
                second_opinion,
                "BACKEND_RUNNERS",
                {"opencode": lambda p, model_index=None: "opencode's critique"},
            ),
            redirect_stderr(err),
        ):
            result = second_opinion.review_plan(
                self._plan(backend="agy,opencode"), verbose=True
            )
        self.assertEqual(result.response_text, "opencode's critique")
        self.assertIn("agy not found on PATH", "".join(result.notices))

    def test_list_all_missing_raises_all_backends_failed(self) -> None:
        with (
            patch.dict(os.environ, self.ENV),
            patch("shutil.which", side_effect=lambda b: None),
            patch.object(
                second_opinion,
                "BACKEND_RUNNERS",
                {"agy": lambda p, model_index=None: "never runs"},
            ),
        ):
            with self.assertRaises(second_opinion.AllBackendsFailedError) as cm:
                second_opinion.review_plan(self._plan(backend="agy,opencode"))
        self.assertIn("all backends failed", str(cm.exception))

    def test_single_name_failure_still_dies_without_fallback(self) -> None:
        with (
            patch.dict(os.environ, self.ENV),
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.object(
                second_opinion,
                "BACKEND_RUNNERS",
                {
                    "agy": lambda p, model_index=None: (_ for _ in ()).throw(
                        second_opinion.BackendError("agy broke")
                    ),
                    "opencode": lambda p, model_index=None: "never reached",
                },
            ),
        ):
            with self.assertRaises(second_opinion.AllBackendsFailedError) as cm:
                second_opinion.review_plan(self._plan(backend="agy"))
        self.assertIn("agy broke", str(cm.exception))
        self.assertNotIn("opencode", str(cm.exception))

    def test_permission_denied_empty_output_suggests_text_only(self) -> None:
        # A grounded run whose tool use was auto-denied surfaces an
        # AllBackendsFailedError that points the user at --text-only, never at
        # the backend's own --dangerously-skip-permissions hint (which would
        # break the critic's read-only isolation). The BackendError llm_backends
        # raises here is the permission-denied subtype it produces after
        # stripping that dangerous hint.
        with (
            patch.dict(os.environ, self.ENV),
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.object(
                second_opinion,
                "BACKEND_RUNNERS",
                {
                    "agy": lambda p, model_index=None: (_ for _ in ()).throw(
                        llm_backends.BackendToolPermissionDeniedError(
                            "exited 0 but produced no output: the backend's tool "
                            "use was auto-denied (no command permission), so it "
                            "produced no output"
                        )
                    ),
                },
            ),
        ):
            with self.assertRaises(second_opinion.AllBackendsFailedError) as cm:
                second_opinion.review_plan(self._plan(backend="agy"))
        msg = str(cm.exception)
        self.assertIn("--text-only", msg)
        self.assertNotIn("--dangerously-skip-permissions", msg)

    def test_permission_denied_last_in_backend_list_suggests_text_only(self) -> None:
        # The auto-denied backend is the LAST candidate in a comma-separated
        # --backend list, with a failing backend before it. The final
        # AllBackendsFailedError must still surface the --text-only hint and
        # never the backend's own --dangerously-skip-permissions hint, proving
        # the hint survives the real review_plan fallback path (not just a
        # single-backend run).
        with (
            patch.dict(os.environ, self.ENV),
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.object(
                second_opinion,
                "BACKEND_RUNNERS",
                {
                    "opencode": lambda p, model_index=None: (_ for _ in ()).throw(
                        second_opinion.BackendError("opencode broke")
                    ),
                    "agy": lambda p, model_index=None: (_ for _ in ()).throw(
                        llm_backends.BackendToolPermissionDeniedError(
                            "exited 0 but produced no output: the backend's tool "
                            "use was auto-denied (no command permission), so it "
                            "produced no output"
                        )
                    ),
                },
            ),
        ):
            with self.assertRaises(second_opinion.AllBackendsFailedError) as cm:
                second_opinion.review_plan(self._plan(backend="opencode,agy"))
        msg = str(cm.exception)
        self.assertIn("--text-only", msg)
        self.assertNotIn("--dangerously-skip-permissions", msg)

    def test_whitespace_only_critique_counts_as_failure(self) -> None:
        with (
            patch.dict(os.environ, self.ENV),
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.object(
                second_opinion,
                "BACKEND_RUNNERS",
                {
                    "agy": lambda p, model_index=None: "   \n  ",
                    "opencode": lambda p, model_index=None: "opencode's critique",
                },
            ),
        ):
            result = second_opinion.review_plan(
                self._plan(backend="agy,opencode"), verbose=True
            )
        self.assertEqual(result.response_text, "opencode's critique")
        self.assertIn("empty output", "".join(result.notices))

    def test_single_backend_whitespace_only_raises(self) -> None:
        with (
            patch.dict(os.environ, self.ENV),
            patch("shutil.which", return_value="/usr/bin/agy"),
            patch.object(
                second_opinion,
                "BACKEND_RUNNERS",
                {"agy": lambda p, model_index=None: "  \t\n"},
            ),
        ):
            with self.assertRaises(second_opinion.AllBackendsFailedError) as cm:
                second_opinion.review_plan(self._plan(backend="agy"))
        self.assertIn("empty output", "".join(cm.exception.failures))

    def test_tool_use_transcript_quarantines_pair_for_process(self) -> None:
        calls: list[int | None] = []

        def opencode_runner(
            prompt: str, *, model_index: int | None = None, **kw: object
        ) -> str:
            calls.append(model_index)
            if model_index in (None, 0):
                raise llm_backends.BackendToolUseError(
                    "adversary agent used tools instead of returning text"
                )
            return f"critique from pool index {model_index}"

        with (
            patch.dict(os.environ, self.ENV),
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.object(
                second_opinion, "BACKEND_RUNNERS", {"opencode": opencode_runner}
            ),
        ):
            first = second_opinion.review_plan(self._plan(backend="opencode"))
            self.assertEqual(first.response_text, "critique from pool index 1")
            second = second_opinion.review_plan(self._plan(backend="opencode"))
        self.assertEqual(second.response_text, "critique from pool index 1")
        self.assertEqual(calls, [0, 1, 1])


class GroundedEligibilityFilterTests(unittest.TestCase):
    """Grounded mode filters candidates by per-mode eligibility, not just
    presence. A backend whose grounded read tools are containment-dependent
    (codex/pi/copilot) is skipped (or refused, when forced) on a host without
    user namespaces, rather than escaping the attempt loop as IsolationError.
    """

    ENV = {
        "SECOND_OPINION_AGY_MODEL": "Gemini 3.8 Flash (High)",
        "SECOND_OPINION_OPENCODE_MODEL_POOL": "model-a,model-b",
    }

    def _plan(self, **kw: object) -> second_opinion.ReviewRequest:
        defaults: dict[str, object] = {"plan_text": "my plan"}
        defaults.update(kw)
        return second_opinion.ReviewRequest(**defaults)  # type: ignore[arg-type]

    def test_single_name_grounded_ineligible_raises_review_error(self) -> None:
        with (
            patch.dict(os.environ, self.ENV),
            patch(
                "shutil.which",
                side_effect=lambda b: "/usr/bin/codex" if b == "codex" else None,
            ),
            patch.object(
                second_opinion.llm_backends, "containment_available", lambda: False
            ),
        ):
            with self.assertRaises(second_opinion.ReviewError) as cm:
                second_opinion.review_plan(self._plan(backend="codex"))
        self.assertIn("not eligible for grounded review", str(cm.exception))
        self.assertIn("grounded", str(cm.exception))

    def test_auto_selection_raises_when_no_backend_is_grounded_eligible(
        self,
    ) -> None:
        # On a host without user namespaces, every grounded backend is
        # containment-dependent, so automatic grounded selection has nothing to
        # run: it must fail with a clear, per-backend reason and point at
        # --text-only, not fall through to an unisolated backend.
        with (
            patch.dict(os.environ, self.ENV),
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.object(
                second_opinion.llm_backends, "containment_available", lambda: False
            ),
        ):
            with self.assertRaises(second_opinion.NoBackendAvailableError) as cm:
                second_opinion.review_plan(self._plan())
        self.assertIn("no backend is eligible for grounded review", str(cm.exception))
        self.assertIn("--text-only", str(cm.exception))
        self.assertIn("codex", str(cm.exception))

    def test_text_only_forced_backend_bypasses_grounded_filter(self) -> None:
        # The grounded filter only applies in grounded mode; a text-only
        # single-name run proceeds even when containment is unavailable.
        with (
            patch.dict(os.environ, self.ENV),
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.object(
                second_opinion.llm_backends, "containment_available", lambda: False
            ),
            patch.object(
                second_opinion,
                "BACKEND_RUNNERS",
                {"codex": lambda p, model_index=None: "codex text critique"},
            ),
        ):
            result = second_opinion.review_plan(
                self._plan(backend="codex", text_only=True)
            )
        self.assertEqual(result.response_text, "codex text critique")

    def test_detect_mode_grounded_flags_unavailable(self) -> None:
        with (
            patch.object(
                second_opinion.llm_backends, "containment_available", lambda: False
            ),
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
        ):
            args = second_opinion.build_parser().parse_args(
                ["detect", "--mode", "grounded"]
            )
            buf = io.StringIO()
            with patch("sys.stdout", buf):
                second_opinion.cmd_detect(args)
        report = json.loads(buf.getvalue())
        self.assertFalse(report["codex"]["eligible"])
        self.assertIn("grounded", report["codex"]["reason"])


class PoolSkipRotationTests(unittest.TestCase):
    """Skip-to-next-model pool rotation.

    Two skip conditions inside a backend's model pool — a tool-call-instead-
    of-text reply (:class:`llm_backends.BackendToolUseError`) and opencode's
    "Model access is disabled" error — move the round to the next pool model
    instead of failing it, quarantine the (backend, model) pair for the
    process, and record the skip in backend-call telemetry. A single-model
    override stays strict: the caller named the model, so there is no silent
    replacement.
    """

    ENV = {
        "SECOND_OPINION_OPENCODE_MODEL_POOL": "dead-model,also-dead,healthy-model",
    }
    ACCESS_DISABLED = "Model access is disabled for this model id"

    def setUp(self) -> None:
        # Both are process-lifetime by design; tests must not leak state into
        # other tests in the same pytest worker process.
        self.addCleanup(second_opinion._UNFIT_MODELS.clear)
        self.addCleanup(setattr, llm_backends, "_pending_fallback_reason", None)

    def _plan(self, **kw: object) -> second_opinion.ReviewRequest:
        defaults: dict[str, object] = {"plan_text": "my plan"}
        defaults.update(kw)
        return second_opinion.ReviewRequest(**defaults)  # type: ignore[arg-type]

    @staticmethod
    def _runner(calls: list[int | None], *, bad: set[object], message: str):
        def run(prompt: str, *, model_index: int | None = None, **kw: object) -> str:
            calls.append(model_index)
            if model_index in bad:
                raise llm_backends.BackendError(message)
            return f"critique from pool index {model_index}"

        return run

    @pytest.mark.regression(
        "pool-skip-on-access-disabled",
        "a --model-index round died on the first dead pool entry instead of "
        "skipping to the next model",
    )
    def test_pinned_index_access_disabled_skips_to_next_entry(self) -> None:
        calls: list[int | None] = []
        with (
            patch.dict(os.environ, self.ENV),
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.object(
                second_opinion,
                "BACKEND_RUNNERS",
                {"opencode": self._runner(calls, bad={0}, message=self.ACCESS_DISABLED)},
            ),
        ):
            result = second_opinion.review_plan(
                self._plan(backend="opencode", model_index=0)
            )
        self.assertEqual(result.response_text, "critique from pool index 1")
        self.assertEqual(calls, [0, 1])
        self.assertIn(("opencode", "dead-model"), second_opinion._UNFIT_MODELS)

    @pytest.mark.regression(
        "pool-skip-on-tool-use-reply",
        "a pinned --model-index round failed outright when the model answered "
        "with a tool-use transcript",
    )
    def test_pinned_index_tool_use_reply_skips_to_next_entry(self) -> None:
        calls: list[int | None] = []
        tool_use = llm_backends.BackendToolUseError(
            "adversary agent used tools instead of returning text: invalid"
        )

        def run(prompt: str, *, model_index: int | None = None, **kw: object) -> str:
            calls.append(model_index)
            if model_index == 1:
                raise tool_use
            return f"critique from pool index {model_index}"

        with (
            patch.dict(os.environ, self.ENV),
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.object(second_opinion, "BACKEND_RUNNERS", {"opencode": run}),
        ):
            result = second_opinion.review_plan(
                self._plan(backend="opencode", model_index=1)
            )
        self.assertEqual(result.response_text, "critique from pool index 2")
        self.assertEqual(calls, [1, 2])
        self.assertIn(("opencode", "also-dead"), second_opinion._UNFIT_MODELS)

    def test_exhausted_pool_fails_with_skip_telemetry(self) -> None:
        calls: list[int | None] = []
        logged: list[tuple[tuple[object, ...], dict[str, object]]] = []
        with (
            patch.dict(os.environ, self.ENV),
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.object(
                second_opinion,
                "BACKEND_RUNNERS",
                {
                    "opencode": self._runner(
                        calls, bad={0, 1, 2}, message=self.ACCESS_DISABLED
                    )
                },
            ),
            patch.object(
                llm_backends,
                "_log_backend_call",
                side_effect=lambda *a, **kw: logged.append((a, kw)),
            ),
        ):
            with self.assertRaises(second_opinion.AllBackendsFailedError) as cm:
                second_opinion.review_plan(
                    self._plan(backend="opencode", model_index=0)
                )
        self.assertEqual(calls, [0, 1, 2])
        failures = "".join(cm.exception.failures)
        self.assertEqual(failures.count("skipped to next pool model"), 3)
        skipped = [a for a, _kw in logged if a[2] == "skipped"]
        self.assertEqual(len(skipped), 3)
        self.assertEqual(
            [a[1] for a in skipped], ["dead-model", "also-dead", "healthy-model"]
        )
        # No parked fallback reason may outlive the dead chain.
        self.assertIsNone(llm_backends._pending_fallback_reason)

    def test_single_override_pinned_run_fails_strictly(self) -> None:
        calls: list[int | None] = []
        with (
            patch.dict(
                os.environ,
                {**self.ENV, "SECOND_OPINION_OPENCODE_MODEL": "solo-model"},
            ),
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.object(
                second_opinion,
                "BACKEND_RUNNERS",
                {
                    "opencode": self._runner(
                        calls, bad={None}, message=self.ACCESS_DISABLED
                    )
                },
            ),
        ):
            with self.assertRaises(second_opinion.AllBackendsFailedError) as cm:
                second_opinion.review_plan(self._plan(backend="opencode"))
        self.assertEqual(calls, [None])
        self.assertNotIn("skipped to next pool model", str(cm.exception))
        self.assertEqual(second_opinion._UNFIT_MODELS, set())

    def test_default_model_attempt_fails_strictly(self) -> None:
        calls: list[int | None] = []
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.object(
                second_opinion,
                "BACKEND_RUNNERS",
                {
                    "opencode": self._runner(
                        calls, bad={None}, message=self.ACCESS_DISABLED
                    )
                },
            ),
        ):
            with self.assertRaises(second_opinion.AllBackendsFailedError):
                second_opinion.review_plan(
                    self._plan(backend="opencode"), quiet=True
                )
        self.assertEqual(calls, [None])
        self.assertEqual(second_opinion._UNFIT_MODELS, set())

    def test_unpinned_rotation_skips_access_disabled_entry(self) -> None:
        calls: list[int | None] = []
        with (
            patch.dict(os.environ, self.ENV),
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.object(
                second_opinion,
                "BACKEND_RUNNERS",
                {"opencode": self._runner(calls, bad={0}, message=self.ACCESS_DISABLED)},
            ),
        ):
            result = second_opinion.review_plan(self._plan(backend="opencode"))
        self.assertEqual(result.response_text, "critique from pool index 1")
        self.assertEqual(calls, [0, 1])

    def test_skip_appends_verbose_notice_and_telemetry(self) -> None:
        calls: list[int | None] = []
        parked_at_next: dict[str, object] = {}
        logged: list[tuple[tuple[object, ...], dict[str, object]]] = []

        def runner(
            prompt: str, *, model_index: int | None = None, **kw: object
        ) -> str:
            calls.append(model_index)
            if model_index == 0:
                raise llm_backends.BackendError(self.ACCESS_DISABLED)
            # The surviving attempt must see the parked skip reason at call
            # time — that is the value its own telemetry record will name as
            # fallback_reason.
            parked_at_next["value"] = llm_backends._pending_fallback_reason
            return f"critique from pool index {model_index}"

        with (
            patch.dict(os.environ, self.ENV),
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.object(
                second_opinion,
                "BACKEND_RUNNERS",
                {"opencode": runner},
            ),
            patch.object(
                llm_backends,
                "_log_backend_call",
                side_effect=lambda *a, **kw: logged.append((a, kw)),
            ),
        ):
            result = second_opinion.review_plan(
                self._plan(backend="opencode", model_index=0), verbose=True
            )
        self.assertEqual(result.response_text, "critique from pool index 1")
        skip_notices = [
            n for n in result.notices if "skipping to next pool model" in n
        ]
        self.assertEqual(len(skip_notices), 1)
        self.assertIn("dead-model", skip_notices[0])
        self.assertIn(self.ACCESS_DISABLED, skip_notices[0])
        skipped_records = [(a, kw) for a, kw in logged if a[2] == "skipped"]
        self.assertEqual(len(skipped_records), 1)
        args, kwargs = skipped_records[0]
        self.assertEqual(args[0], "opencode")
        self.assertEqual(args[1], "dead-model")
        self.assertGreater(args[4], 0)
        self.assertIn("Model access is disabled", kwargs["error_snippet"])
        # The surviving attempt's record must name why it follows a skip:
        # the parked reason is live at its call time, and nothing may leak
        # past the run.
        self.assertEqual(parked_at_next["value"], "pool_skip")
        self.assertIsNone(llm_backends._pending_fallback_reason)


class CmdProbeTests(unittest.TestCase):
    """The ``probe`` subcommand: per-pool-model health report as JSON.

    Every pool entry is probed independently with one trivial text-only
    request — probe never rotates and never raises on a dead model, it
    reports. Tests mock the backend runners; no test ever calls a real model.
    """

    ENV = {
        "SECOND_OPINION_OPENCODE_MODEL_POOL": "m-ok,m-bad",
    }

    def setUp(self) -> None:
        self.addCleanup(second_opinion._UNFIT_MODELS.clear)
        self.addCleanup(setattr, llm_backends, "_pending_fallback_reason", None)
        # `probe` now also reads each backend's `--version`. Fake the
        # subprocess here rather than per test: under pytest a real Popen hits
        # the sandbox guard (a RuntimeError, not a clean skip), and under a
        # direct unittest run it would execute /usr/bin/<backend> for real.
        self.version_proc = subprocess.CompletedProcess(
            [], 0, stdout="1.0.0\n", stderr=""
        )
        version = patch("subprocess.run", return_value=self.version_proc)
        version.start()
        self.addCleanup(version.stop)

    @staticmethod
    def _args(backend: list[str] | None) -> argparse.Namespace:
        return argparse.Namespace(
            cmd="probe", backend=backend, quiet=False, verbose=False
        )

    def _runner(self, calls: list[tuple[int | None, str | None]]):
        def run(
            prompt: str,
            *,
            model_index: int | None = None,
            mode: str | None = None,
        ) -> str:
            calls.append((model_index, mode))
            if model_index == 1:
                raise llm_backends.BackendToolUseError(
                    "adversary agent used tools instead of returning text: invalid"
                )
            return "ok"

        return run

    def test_probe_pool_reports_each_entry_independently(self) -> None:
        calls: list[tuple[int | None, str | None]] = []
        out = io.StringIO()
        with (
            patch.dict(os.environ, self.ENV),
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.object(
                second_opinion, "BACKEND_RUNNERS", {"opencode": self._runner(calls)}
            ),
            patch("sys.stdout", out),
            self.assertRaises(SystemExit) as cm,
        ):
            second_opinion.cmd_probe(self._args(["opencode"]))
        self.assertEqual(cm.exception.code, 1)
        # Text-only: the probe request must never hand the model tools.
        self.assertEqual({mode for _m, mode in calls}, {"text-only"})
        report = json.loads(out.getvalue())
        entries = report["probes"]
        self.assertEqual(len(entries), 2)
        first, second = entries
        self.assertEqual(first["index"], 0)
        self.assertEqual(first["model"], "m-ok")
        self.assertEqual(first["status"], "ok")
        self.assertIsNone(first["detail"])
        self.assertGreaterEqual(first["latency_seconds"], 0)
        self.assertEqual(first["config"], "pool")
        self.assertEqual(
            first["pool_var"], "SECOND_OPINION_OPENCODE_MODEL_POOL"
        )
        self.assertEqual(second["index"], 1)
        self.assertEqual(second["model"], "m-bad")
        self.assertEqual(second["status"], "unavailable")
        self.assertIn("used tools instead of returning text", second["detail"])

    def test_probe_all_healthy_exits_zero(self) -> None:
        calls: list[tuple[int | None, str | None]] = []
        out = io.StringIO()

        def run(
            prompt: str, *, model_index: int | None = None, mode: str | None = None
        ) -> str:
            calls.append((model_index, mode))
            return "ok"

        with (
            patch.dict(os.environ, self.ENV),
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.object(second_opinion, "BACKEND_RUNNERS", {"opencode": run}),
            patch("sys.stdout", out),
        ):
            second_opinion.cmd_probe(self._args(["opencode"]))
        self.assertEqual(len(calls), 2)
        self.assertEqual(json.loads(out.getvalue())["probes"][0]["status"], "ok")

    def test_probe_single_override_config(self) -> None:
        out = io.StringIO()
        with (
            # Single override only — a configured pool would take precedence
            # over it (see _probe_attempts), so the pool var stays unset here.
            patch.dict(
                os.environ,
                {"SECOND_OPINION_OPENCODE_MODEL": "solo-model"},
            ),
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.object(
                second_opinion,
                "BACKEND_RUNNERS",
                {"opencode": lambda p, model_index=None, mode=None: "ok"},
            ),
            patch("sys.stdout", out),
        ):
            second_opinion.cmd_probe(self._args(["opencode"]))
        entries = json.loads(out.getvalue())["probes"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["config"], "single")
        self.assertEqual(entries[0]["model"], "solo-model")
        self.assertIsNone(entries[0]["index"])
        self.assertEqual(entries[0]["status"], "ok")

    def test_probe_default_config_probes_backend_default(self) -> None:
        out = io.StringIO()
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.object(
                second_opinion,
                "BACKEND_RUNNERS",
                {"opencode": lambda p, model_index=None, mode=None: "ok"},
            ),
            patch("sys.stdout", out),
        ):
            second_opinion.cmd_probe(self._args(["opencode"]))
        entries = json.loads(out.getvalue())["probes"]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["config"], "default")
        self.assertIsNone(entries[0]["model"])
        self.assertIsNone(entries[0]["index"])

    def test_probe_not_installed_entry_and_default_backend_set(self) -> None:
        out = io.StringIO()
        with (
            patch.dict(os.environ, self.ENV),
            patch("shutil.which", return_value=None),
            patch.object(
                second_opinion,
                "BACKEND_RUNNERS",
                {"opencode": lambda p, model_index=None, mode=None: "never"},
            ),
            patch("sys.stdout", out),
        ):
            second_opinion.cmd_probe(self._args(["opencode"]))
        entries = json.loads(out.getvalue())["probes"]
        self.assertEqual(entries, [{"backend": "opencode", "status": "not_installed"}])

    def test_probe_default_backends_come_from_available_backends(self) -> None:
        out = io.StringIO()
        with (
            patch.dict(
                os.environ,
                {
                    "SECOND_OPINION_AGY_MODEL": "Gemini 3.8 Flash (High)",
                    **self.ENV,
                },
            ),
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.object(second_opinion, "available_backends", return_value=["agy"]),
            patch.object(
                second_opinion,
                "BACKEND_RUNNERS",
                {"agy": lambda p, model_index=None, mode=None: "ok"},
            ),
            patch("sys.stdout", out),
        ):
            second_opinion.cmd_probe(self._args(None))
        entries = json.loads(out.getvalue())["probes"]
        self.assertEqual([e["backend"] for e in entries], ["agy"])

    def test_probe_detail_is_redacted(self) -> None:
        # End to end against the real patterns: a secret-shaped value in a
        # backend's error must not reach the report. `sk-SECRET` would not
        # match (`sk-` needs 16+ chars), so the fixture uses a real shape.
        secret = "sk-" + "a1b2c3d4e5f6g7h8"
        out = io.StringIO()
        with (
            patch.dict(os.environ, self.ENV),
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.object(
                second_opinion,
                "BACKEND_RUNNERS",
                {
                    "opencode": lambda p, model_index=None, mode=None: (
                        (_ for _ in ()).throw(
                            llm_backends.BackendError(f"auth failed with {secret}")
                        )
                    )
                },
            ),
            patch("sys.stdout", out),
            self.assertRaises(SystemExit),
        ):
            second_opinion.cmd_probe(self._args(["opencode"]))
        detail = json.loads(out.getvalue())["probes"][0]["detail"]
        self.assertNotIn("a1b2c3d4e5f6g7h8", detail)
        self.assertIn("[REDACTED]", detail)

    def _probe_entry(self, out: io.StringIO) -> dict:
        return json.loads(out.getvalue())["probes"][0]

    def test_probe_reports_the_backend_cli_version(self) -> None:
        out = io.StringIO()
        self.version_proc.stdout = "codex-cli 0.162.1\n"
        with (
            patch.dict(os.environ, self.ENV),
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.object(
                second_opinion, "BACKEND_RUNNERS", {"opencode": self._runner([])}
            ),
            patch("sys.stdout", out),
            self.assertRaises(SystemExit),
        ):
            second_opinion.cmd_probe(self._args(["opencode"]))
        entries = json.loads(out.getvalue())["probes"]
        self.assertEqual([e["status"] for e in entries], ["ok", "unavailable"])
        self.assertEqual([e["cli_version"] for e in entries], ["0.162.1", "0.162.1"])

    def test_probe_unreadable_version_is_null_and_warned_not_failed(self) -> None:
        # A CLI too old / too broken to print a version must not be reported
        # as an unavailable *model*: the probe result is about the model.
        out, err = io.StringIO(), io.StringIO()
        self.version_proc.returncode = 3
        self.version_proc.stdout = ""
        self.version_proc.stderr = "unknown option --version"
        with (
            patch.dict(os.environ, self.ENV),
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.object(
                second_opinion,
                "BACKEND_RUNNERS",
                {"opencode": lambda p, model_index=None, mode=None: "ok"},
            ),
            patch("sys.stdout", out),
            patch("sys.stderr", err),
        ):
            second_opinion.cmd_probe(self._args(["opencode"]))
        entry = self._probe_entry(out)
        self.assertIsNone(entry["cli_version"])
        self.assertEqual(entry["status"], "ok")
        self.assertIn("unknown option --version", err.getvalue())

    def test_probe_empty_version_output_is_null_and_warned(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        self.version_proc.stdout = "\n"
        with (
            patch.dict(os.environ, self.ENV),
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.object(
                second_opinion,
                "BACKEND_RUNNERS",
                {"opencode": lambda p, model_index=None, mode=None: "ok"},
            ),
            patch("sys.stdout", out),
            patch("sys.stderr", err),
        ):
            second_opinion.cmd_probe(self._args(["opencode"]))
        self.assertIsNone(self._probe_entry(out)["cli_version"])
        self.assertEqual(len(err.getvalue().strip().splitlines()), 1)

    def test_probe_reads_the_version_once_per_distinct_backend(self) -> None:
        out = io.StringIO()
        with (
            patch.dict(os.environ, self.ENV),
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.object(
                second_opinion,
                "BACKEND_RUNNERS",
                {"opencode": lambda p, model_index=None, mode=None: "ok"},
            ),
            patch("sys.stdout", out),
        ):
            with patch("subprocess.run", return_value=self.version_proc) as run:
                second_opinion.cmd_probe(self._args(["opencode", "opencode"]))
        version_calls = [c for c in run.call_args_list if c.args[0][-1] == "--version"]
        self.assertEqual(len(version_calls), 1)

    def test_probe_resolves_each_distinct_binary_once(self) -> None:
        out = io.StringIO()
        with (
            patch.dict(os.environ, self.ENV),
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}") as which,
            patch.object(
                second_opinion,
                "BACKEND_RUNNERS",
                {"opencode": lambda p, model_index=None, mode=None: "ok"},
            ),
            patch("sys.stdout", out),
        ):
            second_opinion.cmd_probe(self._args(["opencode", "opencode"]))
        self.assertEqual(which.call_count, 1)

    def test_probe_detail_of_an_already_bounded_failure_is_not_recut(self) -> None:
        # A backend failure arrives from `_backend_failure` already masked and
        # excerpted; re-excerpting it would insert a second marker and drop
        # the exit-code prefix the reader needs.
        stderr = "".join(f"[codex] chunk {i}\n" for i in range(200)) + "fatal: 0.162.1"
        exc = llm_backends._backend_failure("codex exited 1", stderr=stderr)
        out = io.StringIO()

        def boom(prompt: str, *, model_index=None, mode=None) -> str:
            raise exc

        with (
            patch.dict(os.environ, self.ENV),
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.object(second_opinion, "BACKEND_RUNNERS", {"opencode": boom}),
            patch("sys.stdout", out),
            self.assertRaises(SystemExit),
        ):
            second_opinion.cmd_probe(self._args(["opencode"]))
        detail = json.loads(out.getvalue())["probes"][0]["detail"]
        # Byte-for-byte: a second excerpt would drop the head window and add
        # a second marker, which the looser assertions below would not catch.
        self.assertEqual(detail, str(exc))
        self.assertIn("codex exited 1", detail)
        self.assertIn("0.162.1", detail)
        self.assertEqual(detail.count(cli_common.EXCERPT_MARKER), 1)

    @pytest.mark.regression(
        "probe-detail-keeps-backend-output-tail",
        "AssertionError: '0.162.1' not found in '[codex] reading project doc "
        "chunk 0\\n[codex] reading project doc chunk 1",
    )
    def test_probe_detail_keeps_the_output_tail(self) -> None:
        preamble = "".join(
            f"[codex] reading project doc chunk {i}\n" for i in range(40)
        )
        fatal = "error: this Codex CLI build is 0.162.1, upgrade to >=0.170"
        out = io.StringIO()

        def boom(prompt: str, *, model_index=None, mode=None) -> str:
            raise llm_backends.BackendError(preamble + fatal)

        with (
            patch.dict(os.environ, self.ENV),
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.object(second_opinion, "BACKEND_RUNNERS", {"opencode": boom}),
            patch("sys.stdout", out),
            self.assertRaises(SystemExit),
        ):
            second_opinion.cmd_probe(self._args(["opencode"]))
        detail = json.loads(out.getvalue())["probes"][0]["detail"]
        self.assertIn("0.162.1", detail)
        # Exactly one excerpt: re-excerpting an already-bounded message would
        # insert a second marker and drop the first head window.
        self.assertEqual(detail.count(cli_common.EXCERPT_MARKER), 1)


class DiffReviewTests(unittest.TestCase):
    """`review --diff`: the staged diff is the input, with a bug-hunting prompt."""

    def _git_ok(self, stdout: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["git"], returncode=0, stdout=stdout, stderr=""
        )

    def test_build_prompt_diff_grounded_uses_diff_review_prompt(self) -> None:
        prompt = second_opinion.build_prompt("+x = 1\n", None, diff=True)
        self.assertIn("staged diff", prompt)
        self.assertIn("read-only access to the codebase", prompt)
        self.assertIn("No findings", prompt)
        self.assertIn("+x = 1", prompt)
        self.assertNotIn("reviewing a plan", prompt)

    def test_build_prompt_diff_asks_for_weak_tests(self) -> None:
        # The diff reviewer looks for tests that cannot fail; a plan review
        # has no tests to judge, so the item stays diff-only.
        diff_prompt = second_opinion.build_prompt("+x = 1\n", None, diff=True)
        plan_prompt = second_opinion.build_prompt("plan text\n", None)
        self.assertIn("- weak tests:", diff_prompt)
        self.assertNotIn("weak tests", plan_prompt)

    def test_build_prompt_diff_includes_simplifications_instruction(self) -> None:
        prompt = second_opinion.build_prompt("+x = 1\n", None, diff=True)
        self.assertIn('headed "Simplifications:"', prompt)
        self.assertIn("then give the Simplifications section", prompt)
        self.assertNotIn('answer exactly "No findings." and stop', prompt)
        self.assertNotIn("If you find nothing real", prompt)

    def test_build_prompt_diff_simplifications_present_with_focus_hints(
        self,
    ) -> None:
        prompt = second_opinion.build_prompt("+x\n", "- the lock", diff=True)
        self.assertIn('headed "Simplifications:"', prompt)
        self.assertIn("then give the Simplifications section", prompt)
        self.assertIn("- the lock", prompt)

    def test_build_prompt_diff_text_only_says_no_tools(self) -> None:
        prompt = second_opinion.build_prompt(
            "+x = 1\n", None, grounded=False, diff=True
        )
        self.assertIn("staged diff", prompt)
        self.assertIn("you have no tools", prompt)
        self.assertNotIn("read-only access to the codebase", prompt)

    def test_build_prompt_diff_focus_section_names_the_change(self) -> None:
        prompt = second_opinion.build_prompt("+x\n", "- the lock", diff=True)
        self.assertIn("- the lock", prompt)
        self.assertNotIn("plan's author", prompt)

    def test_build_prompt_diff_spec_adds_a_separate_spec_section(self) -> None:
        prompt = second_opinion.build_prompt(
            "+x\n", None, diff=True, spec_text="Export writes CSV.\n"
        )
        self.assertIn('headed "Spec:"', prompt)
        self.assertIn("Export writes CSV.", prompt)
        self.assertIn("missing or partial", prompt)
        self.assertIn("not asked for", prompt)
        self.assertIn("implemented wrongly", prompt)
        self.assertIn('"Spec: no findings."', prompt)
        # The diff stays last, after the spec, as the material under review.
        self.assertLess(prompt.index("Export writes CSV."), prompt.index("+x\n"))

    def test_build_prompt_diff_without_spec_has_no_spec_section(self) -> None:
        prompt = second_opinion.build_prompt("+x\n", None, diff=True)
        self.assertNotIn('headed "Spec:"', prompt)

    def test_cmd_review_spec_reaches_the_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            spec = Path(tmpdir) / "spec.md"
            spec.write_text("Export writes CSV.\n", encoding="utf-8")
            args = second_opinion.build_parser().parse_args(
                ["review", "--diff", "--spec", str(spec), "--prompt-only"]
            )
            with (
                patch.object(second_opinion, "read_staged_diff", return_value="+x\n"),
                patch("sys.stdout", io.StringIO()) as out,
            ):
                second_opinion.cmd_review(args)
        self.assertEqual(
            out.getvalue(),
            second_opinion.build_prompt(
                "+x\n", None, diff=True, spec_text="Export writes CSV.\n"
            ),
        )

    def test_cmd_review_spec_without_diff_is_an_error(self) -> None:
        args = second_opinion.build_parser().parse_args(
            ["review", "plan text", "--spec", "spec.md"]
        )
        with (
            redirect_stderr(io.StringIO()) as err,
            self.assertRaises(SystemExit) as cm,
        ):
            second_opinion.cmd_review(args)
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("--spec only applies with --diff", err.getvalue())

    def _spec_error(self, content: bytes) -> str:
        with tempfile.TemporaryDirectory() as tmpdir:
            spec = Path(tmpdir) / "spec.md"
            spec.write_bytes(content)
            args = second_opinion.build_parser().parse_args(
                ["review", "--diff", "--spec", str(spec), "--prompt-only"]
            )
            with (
                patch.object(second_opinion, "read_staged_diff", return_value="+x\n"),
                patch("sys.stdout", io.StringIO()),
                redirect_stderr(io.StringIO()) as err,
                self.assertRaises(SystemExit) as cm,
            ):
                second_opinion.cmd_review(args)
        self.assertEqual(cm.exception.code, 1)
        return err.getvalue()

    def test_cmd_review_blank_spec_file_is_an_error(self) -> None:
        # A blank spec would otherwise drop the Spec: section silently while
        # the report still names the spec path as checked.
        self.assertIn("--spec is empty", self._spec_error(b"  \n\n"))

    def test_cmd_review_empty_spec_argument_is_an_error(self) -> None:
        args = second_opinion.build_parser().parse_args(
            ["review", "--diff", "--spec", "", "--prompt-only"]
        )
        with (
            patch.object(second_opinion, "read_staged_diff", return_value="+x\n"),
            patch("sys.stdout", io.StringIO()),
            redirect_stderr(io.StringIO()) as err,
            self.assertRaises(SystemExit) as cm,
        ):
            second_opinion.cmd_review(args)
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("--spec not found", err.getvalue())

    def test_cmd_review_undecodable_spec_file_is_an_error(self) -> None:
        self.assertIn("--spec unreadable", self._spec_error(b"\xff\xfe bad"))

    def test_cmd_review_spec_reaches_the_backend_prompt(self) -> None:
        captured: dict[str, str] = {}

        def fake_runner(prompt: str, **_kwargs: object) -> str:
            captured["prompt"] = prompt
            return "No findings."

        with tempfile.TemporaryDirectory() as tmpdir:
            spec = Path(tmpdir) / "spec.md"
            spec.write_text("Export writes CSV.\n", encoding="utf-8")
            args = second_opinion.build_parser().parse_args(
                ["review", "--diff", "--spec", str(spec), "--backend", "codex"]
            )
            with (
                patch("shutil.which", return_value="/usr/bin/codex"),
                patch.dict(second_opinion.BACKEND_RUNNERS, {"codex": fake_runner}),
                patch.object(second_opinion, "read_staged_diff", return_value="+x\n"),
                patch("sys.stdout", io.StringIO()),
            ):
                second_opinion.cmd_review(args)
        self.assertIn('headed "Spec:"', captured["prompt"])
        self.assertIn("Export writes CSV.", captured["prompt"])

    def test_cmd_review_missing_spec_file_is_an_error(self) -> None:
        args = second_opinion.build_parser().parse_args(
            ["review", "--diff", "--spec", "/nonexistent/spec.md"]
        )
        with (
            patch.object(second_opinion, "read_staged_diff", return_value="+x\n"),
            redirect_stderr(io.StringIO()) as err,
            self.assertRaises(SystemExit) as cm,
        ):
            second_opinion.cmd_review(args)
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("--spec not found", err.getvalue())

    def test_read_staged_diff_runs_git_in_dir(self) -> None:
        calls: list[list[str]] = []

        def fake_run(argv: list[str], **kwargs: object):
            calls.append(argv)
            return self._git_ok("diff --git a/x b/x\n+y\n")

        with patch.object(second_opinion.subprocess, "run", side_effect=fake_run):
            text = second_opinion.read_staged_diff(Path("/tmp/repo"))
        self.assertEqual(text, "diff --git a/x b/x\n+y\n")
        self.assertEqual(
            calls, [["git", "-C", "/tmp/repo", "diff", "--cached", "--no-color"]]
        )

    def test_read_staged_diff_empty_is_an_error(self) -> None:
        with (
            patch.object(
                second_opinion.subprocess, "run", return_value=self._git_ok("  \n")
            ),
            redirect_stderr(io.StringIO()) as err,
            self.assertRaises(SystemExit) as cm,
        ):
            second_opinion.read_staged_diff(Path("/tmp/repo"))
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("staged diff is empty", err.getvalue())

    def test_read_staged_diff_git_failure_is_an_error(self) -> None:
        failed = subprocess.CompletedProcess(
            args=["git"], returncode=128, stdout="", stderr="not a git repository"
        )
        with (
            patch.object(second_opinion.subprocess, "run", return_value=failed),
            redirect_stderr(io.StringIO()) as err,
            self.assertRaises(SystemExit),
        ):
            second_opinion.read_staged_diff(Path("/tmp/repo"))
        self.assertIn("not a git repository", err.getvalue())

    def test_cmd_review_diff_sends_staged_diff_with_diff_prompt(self) -> None:
        captured: dict[str, object] = {}

        def fake_runner(prompt: str, *, model_index=None, mode="grounded", target_dir=None):
            captured["prompt"] = prompt
            captured["target_dir"] = target_dir
            return "1. bug"

        diff_text = "diff --git a/a.py b/a.py\n+## Critique notes\n"
        repo = Path("/tmp/my-repo")
        with (
            patch("shutil.which", return_value="/usr/bin/codex"),
            patch.dict(second_opinion.BACKEND_RUNNERS, {"codex": fake_runner}),
            patch.object(
                second_opinion.subprocess,
                "run",
                return_value=self._git_ok(diff_text),
            ) as run,
            patch("sys.stdout", io.StringIO()) as out,
        ):
            second_opinion.cmd_review(
                ns(plan=None, diff=True, backend="codex", dir=repo, text_only=False)
            )
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0][:3], ["git", "-C", str(repo.resolve())])
        self.assertEqual(
            captured["prompt"], second_opinion.build_prompt(diff_text, None, diff=True)
        )
        self.assertEqual(captured["target_dir"], repo.resolve())
        self.assertIn("1. bug", out.getvalue())

    def test_parser_requires_exactly_one_of_plan_or_diff(self) -> None:
        parser = second_opinion.build_parser()
        args = parser.parse_args(["review", "--diff"])
        self.assertTrue(args.diff)
        self.assertIsNone(args.plan)
        for argv in (["review"], ["review", "plan.md", "--diff"]):
            with (
                redirect_stderr(io.StringIO()),
                self.assertRaises(SystemExit) as cm,
            ):
                second_opinion.cmd_review(parser.parse_args(argv))
            self.assertEqual(cm.exception.code, 1)

    def test_cmd_review_prompt_only_prints_prompt_and_calls_no_backend(self) -> None:
        diff_text = "diff --git a/a.py b/a.py\n+x\n"
        with (
            patch("shutil.which", return_value=None),
            patch.dict(second_opinion.BACKEND_RUNNERS, {}, clear=True),
            patch.object(
                second_opinion.subprocess, "run", return_value=self._git_ok(diff_text)
            ),
            patch("sys.stdout", io.StringIO()) as out,
        ):
            second_opinion.cmd_review(
                ns(plan=None, diff=True, dir=Path("/tmp/r"), prompt_only=True)
            )
        self.assertEqual(
            out.getvalue(), second_opinion.build_prompt(diff_text, None, diff=True)
        )

    @pytest.mark.allow_real_subprocess  # a real git in a throwaway repo, so the flags' effects (not the flags) are what is asserted
    def test_read_staged_diff_reads_a_real_staged_diff(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir)
            subprocess.run(
                ["git", "init", "-q", str(repo)], check=True, capture_output=True
            )
            (repo / "a.txt").write_text("hello\n")
            subprocess.run(
                ["git", "-C", str(repo), "add", "a.txt"],
                check=True,
                capture_output=True,
            )
            text = second_opinion.read_staged_diff(repo)
        self.assertIsInstance(text, str)
        self.assertIn("+hello", text)

    @pytest.mark.allow_real_subprocess  # a real git failing outside a repository
    def test_read_staged_diff_real_git_failure_exits_one(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            err = io.StringIO()
            with redirect_stderr(err), self.assertRaises(SystemExit) as cm:
                second_opinion.read_staged_diff(Path(tmpdir))
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("git diff --cached failed", err.getvalue())

    def test_read_staged_diff_passes_pathspecs(self) -> None:
        with patch.object(
            second_opinion.subprocess, "run", return_value=self._git_ok("+y\n")
        ) as run:
            second_opinion.read_staged_diff(Path("/r"), ["agent-scripts", ":!*.md"])
        self.assertEqual(
            run.call_args.args[0],
            ["git", "-C", "/r", "diff", "--cached", "--no-color", "--",
             "agent-scripts", ":!*.md"],
        )

    def test_cmd_review_diff_path_flag_reaches_git(self) -> None:
        parser = second_opinion.build_parser()
        args = parser.parse_args(
            ["review", "--diff", "--diff-path", "a", "--diff-path", ":!b", "--prompt-only"]
        )
        with (
            patch.object(
                second_opinion.subprocess, "run", return_value=self._git_ok("+y\n")
            ) as run,
            patch("sys.stdout", io.StringIO()),
        ):
            second_opinion.cmd_review(args)
        self.assertEqual(run.call_args.args[0][-3:], ["--", "a", ":!b"])

    def test_diff_path_without_diff_is_an_error(self) -> None:
        args = second_opinion.build_parser().parse_args(
            ["review", "plan.md", "--diff-path", "a"]
        )
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            second_opinion.cmd_review(args)

    def test_exclude_backend_skips_the_author_harness(self) -> None:
        used: list[str] = []

        def runner(name: str):
            def run(prompt: str, *, model_index=None, mode="grounded", target_dir=None):
                used.append(name)
                return "No findings."
            return run

        with (
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.dict(
                second_opinion.BACKEND_RUNNERS,
                {"codex": runner("codex"), "agy": runner("agy")},
                clear=True,
            ),
            patch.object(
                second_opinion, "available_backends", return_value=["codex", "agy"]
            ),
        ):
            second_opinion.review_plan(
                second_opinion.ReviewRequest(
                    plan_text="+x\n", diff=True, exclude_backends=("codex",)
                )
            )
        self.assertEqual(used, ["agy"])

    def test_exclude_backend_leaving_none_is_no_backend(self) -> None:
        with (
            patch.object(second_opinion, "available_backends", return_value=["codex"]),
            patch.dict(second_opinion.BACKEND_RUNNERS, {"codex": lambda p, **k: "x"}),
            self.assertRaises(second_opinion.NoBackendAvailableError),
        ):
            second_opinion.review_plan(
                second_opinion.ReviewRequest(plan_text="p", exclude_backends=("codex",))
            )

    def test_exclude_backend_flag_parses_and_conflicts_with_backend(self) -> None:
        parser = second_opinion.build_parser()
        args = parser.parse_args(
            ["review", "p", "--exclude-backend", "codex", "--exclude-backend", "pi"]
        )
        self.assertEqual(args.exclude_backend, ["codex", "pi"])
        args = parser.parse_args(
            ["review", "p", "--backend", "agy", "--exclude-backend", "codex"]
        )
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            second_opinion.cmd_review(args)

    def test_exclude_backend_rejects_unknown_name(self) -> None:
        args = second_opinion.build_parser().parse_args(
            ["review", "p", "--exclude-backend", "CODEX"]
        )
        with redirect_stderr(io.StringIO()) as err, self.assertRaises(SystemExit):
            second_opinion.cmd_review(args)
        self.assertIn("unknown backend", err.getvalue())


class ImageReviewFacadeTests(unittest.TestCase):
    def setUp(self) -> None:
        super().setUp()
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        self.addCleanup(self._tmpdir.cleanup)
        patcher = patch.object(second_opinion.llm_backends, "containment_available", lambda: True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _png(self, name: str = "a.png") -> Path:
        p = self.tmp / name
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 20)
        return p

    def test_forced_agy_with_images_raises_image_support_error(self) -> None:
        png = str(self._png())
        with self.assertRaises(second_opinion.ImageSupportError) as raised:
            second_opinion.review_plan(
                second_opinion.ReviewRequest(
                    plan_text="rubric", backend="agy", images=(png,)
                )
            )
        self.assertIn("agy", str(raised.exception))
        self.assertIn("no headless image flag", str(raised.exception))

    def test_priority_selection_only_agy_raises_explicit(self) -> None:
        png = str(self._png())
        with (
            patch.object(second_opinion, "available_backends", return_value=["agy"]),
            patch("shutil.which", side_effect=lambda b: "/usr/bin/agy" if b == "agy" else None),
        ):
            with self.assertRaises(second_opinion.NoBackendAvailableError) as raised:
                second_opinion.review_plan(
                    second_opinion.ReviewRequest(plan_text="rubric", images=(png,))
                )
        self.assertIn("agy", str(raised.exception))
        self.assertIn("no headless image flag", str(raised.exception))

    def test_validation_error_translated_to_image_input_error(self) -> None:
        with self.assertRaises(second_opinion.ImageInputError):
            second_opinion.review_plan(
                second_opinion.ReviewRequest(
                    plan_text="rubric", images=("/nonexistent/x.png",)
                )
            )

    def test_images_threaded_to_runner(self) -> None:
        png = str(self._png())
        captured: dict[str, object] = {}

        def fake_runner(
            prompt,
            *,
            model_index=None,
            mode=None,
            target_dir=None,
            images=None,
        ):
            captured["images"] = images
            return "critique"

        with (
            patch.object(second_opinion, "available_backends", return_value=["codex"]),
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.object(second_opinion, "BACKEND_RUNNERS", {"codex": fake_runner}),
        ):
            result = second_opinion.review_plan(
                second_opinion.ReviewRequest(plan_text="rubric", images=(png,))
            )
        self.assertEqual(result.response_text, "critique")
        self.assertEqual(captured["images"], [png])

    def test_forced_list_filters_non_capable_with_notice(self) -> None:
        png = str(self._png())
        captured: dict[str, object] = {}

        def fake_runner(
            prompt,
            *,
            model_index=None,
            mode=None,
            target_dir=None,
            images=None,
        ):
            captured["images"] = images
            return "critique"

        with (
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}" if b == "codex" else None),
            patch.object(second_opinion, "BACKEND_RUNNERS", {"codex": fake_runner}),
        ):
            result = second_opinion.review_plan(
                second_opinion.ReviewRequest(
                    plan_text="rubric", backend="agy,codex", images=(png,)
                )
            )
        self.assertEqual(result.response_text, "critique")
        self.assertEqual(captured["images"], [png])

    def test_image_prompt_is_used(self) -> None:
        prompt = second_opinion.build_prompt("rubric", None, images=("a.png", "b.png"))
        self.assertIn("2 attached image(s)", prompt)
        self.assertIn("rubric", prompt)

    def test_diff_and_images_are_rejected(self) -> None:
        png = str(self._png())
        with self.assertRaises(second_opinion.ReviewError) as raised:
            second_opinion.review_plan(
                second_opinion.ReviewRequest(
                    plan_text="staged diff text", diff=True, images=(png,)
                )
            )
        self.assertIn("mutually exclusive", str(raised.exception))

    def test_prompt_only_diff_image_conflict_rejected(self) -> None:
        parser = second_opinion.build_parser()
        args = parser.parse_args(
            ["review", "--diff", "--image", "a.png", "--prompt-only"]
        )
        with (
            patch.object(
                second_opinion, "read_staged_diff", return_value="fake diff"
            ),
            redirect_stderr(io.StringIO()) as err,
            self.assertRaises(SystemExit),
        ):
            second_opinion.cmd_review(args)
        self.assertIn("do not combine", err.getvalue())

    def test_opencode_staging_failure_is_a_backend_error(self) -> None:
        with patch.object(
            second_opinion.llm_backends,
            "stage_images",
            side_effect=PermissionError("read-only home"),
        ):
            with self.assertRaises(second_opinion.BackendError) as raised:
                second_opinion.run_opencode("rubric", images=["a.png"])
        self.assertIn("staging failed", str(raised.exception))

    def test_staging_failure_falls_through_to_next_backend(self) -> None:
        png = str(self._png())
        calls: list[str] = []

        def run_copilot(
            prompt,
            *,
            model_index=None,
            mode=None,
            target_dir=None,
            images=None,
        ):
            calls.append("copilot")
            return "copilot critique"

        with (
            patch.object(
                second_opinion.llm_backends,
                "stage_images",
                side_effect=PermissionError("read-only home"),
            ),
            patch.object(
                second_opinion.llm_backends, "containment_preflight", lambda *a, **k: None
            ),
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.object(
                second_opinion,
                "BACKEND_RUNNERS",
                {"opencode": second_opinion.run_opencode, "copilot": run_copilot},
            ),
        ):
            result = second_opinion.review_plan(
                second_opinion.ReviewRequest(
                    plan_text="rubric", backend="opencode,copilot", images=(png,)
                )
            )
        self.assertEqual(result.response_text, "copilot critique")
        self.assertEqual(calls, ["copilot"])



class RotationOrderTests(unittest.TestCase):
    """`rotation_order`: the pure backend-rotation policy.

    Never-served candidates first in their incoming (priority) order, then the
    served ones ordered by their most recent appearance in history, oldest
    first. Same elements as the input, always.
    """

    def test_empty_history_keeps_priority_order(self) -> None:
        self.assertEqual(
            second_opinion.rotation_order(["codex", "agy", "pi"], ()),
            ["codex", "agy", "pi"],
        )

    def test_served_backends_move_behind_unserved_ones(self) -> None:
        self.assertEqual(
            second_opinion.rotation_order(["codex", "agy", "pi"], ["codex"]),
            ["agy", "pi", "codex"],
        )

    def test_all_served_least_recent_first(self) -> None:
        self.assertEqual(
            second_opinion.rotation_order(
                ["codex", "agy", "pi"], ["codex", "agy", "pi", "codex"]
            ),
            ["agy", "pi", "codex"],
        )

    def test_history_names_outside_candidates_are_ignored(self) -> None:
        self.assertEqual(
            second_opinion.rotation_order(["agy", "pi"], ["codex", "agy"]),
            ["pi", "agy"],
        )

    def test_single_candidate(self) -> None:
        self.assertEqual(second_opinion.rotation_order(["agy"], ["agy"]), ["agy"])


class BackendRotationCmdReviewTests(unittest.TestCase):
    """A shared run key rotates priority-order selection across backends.

    Each round prefers the eligible backend that served least recently, so a
    three-round loop gets three backends (model families) instead of three
    entries from one backend's pool. --backend and --model-index opt out.
    """

    BACKENDS = ["codex", "agy", "opencode"]
    ENV = {
        "SECOND_OPINION_CODEX_MODEL": "gpt-x",
        "SECOND_OPINION_AGY_MODEL": "gemini-x",
        "SECOND_OPINION_OPENCODE_MODEL": "glm-x",
    }

    def setUp(self) -> None:
        self.tmpdir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmpdir)
        test_layouts.activate_sandbox_home(Path(self.tmpdir), self.addCleanup)
        self.addCleanup(second_opinion._UNFIT_MODELS.clear)
        self.addCleanup(setattr, llm_backends, "_pending_fallback_reason", None)
        self.broken: set[str] = set()
        self.plan = Path(self.tmpdir) / "plan.md"
        self.plan.write_text("my plan")

    def _runner(self, name: str) -> Callable[..., str]:
        def run(prompt: str, model_index: int | None = None) -> str:
            if name in self.broken:
                raise second_opinion.BackendError(f"{name} broke")
            return f"{name} critique"

        return run

    def _run(
        self, *, expect_die: bool = False, env: dict[str, str] | None = None, **kw: object
    ) -> tuple[str, str]:
        kw.setdefault("plan", str(self.plan))
        kw.setdefault("run_id", "rot-1")
        kw.setdefault("allow_extra_round", False)
        args = ns(**kw)
        out, err = io.StringIO(), io.StringIO()
        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, env or self.ENV))
            stack.enter_context(
                patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}")
            )
            stack.enter_context(
                patch.object(
                    second_opinion, "available_backends", return_value=self.BACKENDS
                )
            )
            stack.enter_context(
                patch.object(
                    second_opinion,
                    "BACKEND_RUNNERS",
                    {b: self._runner(b) for b in self.BACKENDS},
                )
            )
            stack.enter_context(patch("sys.stdout", out))
            stack.enter_context(patch("sys.stderr", err))
            if expect_die:
                with self.assertRaises(SystemExit):
                    second_opinion.cmd_review(args)
            else:
                second_opinion.cmd_review(args)
        return out.getvalue(), err.getvalue()

    @staticmethod
    def _served(out: str) -> str:
        return out.split(" critique", 1)[0].rsplit("\n", 1)[-1]

    def _state(self, key: str = "run:rot-1") -> dict[str, object]:
        path = second_opinion._run_state_path(key)
        return json.loads(path.read_text(encoding="utf-8"))

    def test_three_rounds_use_three_backends(self) -> None:
        served = [self._served(self._run()[0]) for _ in range(3)]
        self.assertEqual(served, ["codex", "agy", "opencode"])
        self.assertEqual(self._state()["served"], ["codex", "agy", "opencode"])
        self.assertEqual(self._state()["count"], 3)

    def test_failed_preferred_backend_is_preferred_again_next_round(self) -> None:
        self.broken = {"codex"}
        self.assertEqual(self._served(self._run()[0]), "agy")
        self.broken = set()
        self.assertEqual(self._served(self._run()[0]), "codex")

    def test_fallback_is_reported_without_verbose(self) -> None:
        self.broken = {"codex"}
        _out, err = self._run(verbose=False, quiet=False)
        notice = [ln for ln in err.splitlines() if "rotation preferred" in ln]
        self.assertEqual(len(notice), 1)
        self.assertTrue(notice[0].startswith("[second_opinion] "))
        self.assertIn("preferred codex;", notice[0])
        self.assertIn("served by agy", notice[0])
        self.assertIn("codex: codex broke", notice[0])

    def test_fallback_report_is_silenced_by_quiet(self) -> None:
        self.broken = {"codex"}
        out, err = self._run(verbose=False, quiet=True)
        self.assertEqual(self._served(out), "agy")
        self.assertNotIn("rotation preferred", err)

    def test_explicit_backend_does_not_rotate(self) -> None:
        served = [self._served(self._run(backend="codex")[0]) for _ in range(3)]
        self.assertEqual(served, ["codex", "codex", "codex"])

    def test_model_index_does_not_rotate(self) -> None:
        env = {**self.ENV, "SECOND_OPINION_CODEX_MODEL_POOL": "gpt-a,gpt-b"}
        served = [
            self._served(self._run(model_index=0, env=env)[0]) for _ in range(2)
        ]
        self.assertEqual(served, ["codex", "codex"])

    def test_unkeyed_inline_review_does_not_rotate(self) -> None:
        served = [
            self._served(self._run(plan="inline plan text", run_id=None)[0])
            for _ in range(2)
        ]
        self.assertEqual(served, ["codex", "codex"])

    def test_excluded_backend_never_rotates_in(self) -> None:
        served = [
            self._served(self._run(exclude_backend=["agy"])[0]) for _ in range(3)
        ]
        self.assertEqual(served, ["codex", "opencode", "codex"])

    def test_failed_round_records_nothing(self) -> None:
        self._run()
        self.broken = set(self.BACKENDS)
        self._run(expect_die=True)
        self.assertEqual(self._state()["served"], ["codex"])
        self.assertEqual(self._state()["count"], 1)

    def test_tampered_round_records_nothing(self) -> None:
        self._run()
        with patch.object(second_opinion, "_warn_if_output_tampered", return_value=True):
            self._run()
        self.assertEqual(self._state()["served"], ["codex"])
        self.assertEqual(self._state()["count"], 1)

    def test_fresh_allow_extra_round_records_history_not_count(self) -> None:
        runs_dir = second_opinion._runs_dir()
        self.assertFalse(runs_dir.exists())
        out, _err = self._run(allow_extra_round=True)
        self.assertEqual(self._served(out), "codex")
        self.assertEqual(self._state()["served"], ["codex"])
        self.assertEqual(self._state()["count"], 0)
        self.assertEqual(self._served(self._run(allow_extra_round=True)[0]), "agy")

    def test_malformed_served_is_normalized(self) -> None:
        second_opinion._ensure_runs_dir()
        path = second_opinion._run_state_path("run:rot-1")
        path.write_text(
            json.dumps(
                {"key": "run:rot-1", "count": 1, "updated": 0.0,
                 "served": [["codex"], {"a": 1}, None, "bogus", "codex"]}
            )
        )
        state = second_opinion._load_run_state(path, "run:rot-1")
        self.assertEqual(state["served"], ["codex"])
        self.assertEqual(state["count"], 1)
        path.write_text(json.dumps({"key": "run:rot-1", "count": 2, "served": "codex"}))
        self.assertEqual(second_opinion._load_run_state(path, "run:rot-1")["served"], [])
        # And a round still rotates off the normalized history.
        self.assertEqual(self._served(self._run()[0]), "codex")

    def test_recording_onto_malformed_history_writes_a_clean_list(self) -> None:
        second_opinion._ensure_runs_dir()
        path = second_opinion._run_state_path("run:rot-1")
        path.write_text(
            json.dumps({"key": "run:rot-1", "count": 1, "served": [None, "agy", ["x"]]})
        )
        second_opinion._record_successful_review("run:rot-1", "codex")
        self.assertEqual(self._state()["served"], ["agy", "codex"])

    def test_plan_key_ttl_reset_clears_history(self) -> None:
        key = "plan:" + str(self.plan.resolve())
        second_opinion._ensure_runs_dir()
        path = second_opinion._run_state_path(key)
        path.write_text(
            json.dumps({"key": key, "count": 2, "updated": 1.0, "served": ["codex"]})
        )
        state = second_opinion._load_state_for_key(path, key)
        self.assertEqual(state["count"], 0)
        self.assertEqual(state["served"], [])

    def test_unwritable_history_still_prints_the_critique(self) -> None:
        # A critique that already came back must reach the caller even when
        # the run state cannot be written; the failure is a stderr warning.
        with patch.object(
            second_opinion, "_save_run_state", side_effect=OSError("read-only")
        ):
            out, err = self._run(allow_extra_round=True)
        self.assertEqual(self._served(out), "codex")
        self.assertIn("read-only", err)

    def test_plan_path_key_rotates_without_run_id(self) -> None:
        served = [self._served(self._run(run_id=None)[0]) for _ in range(2)]
        self.assertEqual(served, ["codex", "agy"])

    def test_panel_style_recorder_call_still_counts(self) -> None:
        second_opinion._record_successful_review("run:panel")
        state = self._state("run:panel")
        self.assertEqual(state["count"], 1)
        self.assertEqual(state.get("served", []), [])


class BackendRotationFacadeTests(unittest.TestCase):
    """review_plan's side of rotation: ordering, reasons, and the opt-outs."""

    def setUp(self) -> None:
        self.addCleanup(second_opinion._UNFIT_MODELS.clear)
        self.addCleanup(setattr, llm_backends, "_pending_fallback_reason", None)

    def _review(
        self,
        runners: dict[str, Callable[..., str]],
        env: dict[str, str],
        **kw: object,
    ) -> second_opinion.ReviewResult:
        with (
            patch.dict(os.environ, env),
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.object(
                second_opinion, "available_backends", return_value=list(runners)
            ),
            patch.object(second_opinion, "BACKEND_RUNNERS", runners),
        ):
            return second_opinion.review_plan(
                second_opinion.ReviewRequest(plan_text="my plan", **kw)  # type: ignore[arg-type]
            )

    def test_result_names_the_bare_backend(self) -> None:
        result = self._review(
            {"agy": lambda p, model_index=None: "c"},
            {"SECOND_OPINION_AGY_MODEL": "gemini-x"},
        )
        self.assertEqual(result.backend, "agy")

    def test_history_reorders_candidates(self) -> None:
        result = self._review(
            {
                "codex": lambda p, model_index=None: "codex c",
                "agy": lambda p, model_index=None: "agy c",
            },
            {"SECOND_OPINION_AGY_MODEL": "gemini-x", "SECOND_OPINION_CODEX_MODEL": "g"},
            rotation_history=("codex",),
        )
        self.assertEqual(result.backend, "agy")

    def test_explicit_backend_list_ignores_history(self) -> None:
        result = self._review(
            {
                "codex": lambda p, model_index=None: "codex c",
                "agy": lambda p, model_index=None: "agy c",
            },
            {"SECOND_OPINION_AGY_MODEL": "gemini-x", "SECOND_OPINION_CODEX_MODEL": "g"},
            backend="codex,agy",
            rotation_history=("codex",),
        )
        self.assertEqual(result.backend, "codex")

    def test_quarantined_preferred_backend_is_a_fallback_reason(self) -> None:
        second_opinion._UNFIT_MODELS.update({("opencode", "m-a"), ("opencode", "m-b")})
        result = self._review(
            {
                "opencode": lambda p, model_index=None: "never",
                "agy": lambda p, model_index=None: "agy c",
            },
            {
                "SECOND_OPINION_OPENCODE_MODEL_POOL": "m-a,m-b",
                "SECOND_OPINION_AGY_MODEL": "gemini-x",
            },
            rotation_history=(),
        )
        self.assertEqual(result.backend, "agy")
        notice = [n for n in result.notices if "rotation preferred" in n]
        self.assertEqual(len(notice), 1)
        self.assertIn("opencode: every pool model quarantined", notice[0])

    def test_rotation_with_model_index_is_a_request_error(self) -> None:
        with self.assertRaises(second_opinion.ReviewError):
            self._review(
                {"agy": lambda p, model_index=None: "c"},
                {"SECOND_OPINION_AGY_MODEL_POOL": "a,b"},
                rotation_history=(),
                model_index=0,
            )

    def test_pool_exhausted_by_skips_is_the_fallback_reason(self) -> None:
        def tool_hungry(p: str, model_index: int | None = None) -> str:
            raise llm_backends.BackendToolUseError("used tools instead of text")

        result = self._review(
            {"opencode": tool_hungry, "agy": lambda p, model_index=None: "agy c"},
            {
                "SECOND_OPINION_OPENCODE_MODEL_POOL": "m-a,m-b",
                "SECOND_OPINION_AGY_MODEL": "gemini-x",
            },
            rotation_history=(),
        )
        self.assertEqual(result.backend, "agy")
        notice = [n for n in result.notices if "rotation preferred" in n]
        self.assertEqual(len(notice), 1)
        self.assertIn("preferred opencode;", notice[0])
        self.assertIn("served by agy", notice[0])
        self.assertIn("opencode: used tools instead of text", notice[0])

    def test_pi_size_skip_is_a_fallback_reason(self) -> None:
        big = "x" * (llm_backends.PI_MAX_PROMPT_BYTES + 10)
        with (
            patch.dict(os.environ, {"SECOND_OPINION_AGY_MODEL": "gemini-x"}),
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.object(second_opinion, "available_backends", return_value=["pi", "agy"]),
            patch.object(
                second_opinion,
                "BACKEND_RUNNERS",
                {
                    "pi": lambda p, model_index=None: "pi c",
                    "agy": lambda p, model_index=None: "agy c",
                },
            ),
        ):
            result = second_opinion.review_plan(
                second_opinion.ReviewRequest(plan_text=big, rotation_history=())
            )
        self.assertEqual(result.backend, "agy")
        notice = [n for n in result.notices if "rotation preferred" in n]
        self.assertEqual(len(notice), 1)
        self.assertIn("preferred pi;", notice[0])
        self.assertIn("served by agy", notice[0])
        self.assertIn("pi: prompt size", notice[0])

    def test_fully_quarantined_pool_is_named_in_failures(self) -> None:
        second_opinion._UNFIT_MODELS.update({("opencode", "m-a"), ("opencode", "m-b")})
        with self.assertRaises(second_opinion.AllBackendsFailedError) as cm:
            self._review(
                {"opencode": lambda p, model_index=None: "never"},
                {"SECOND_OPINION_OPENCODE_MODEL_POOL": "m-a,m-b"},
            )
        self.assertTrue(
            any("opencode" in f and "quarantined" in f for f in cm.exception.failures)
        )


class PoolSkipClassificationTests(unittest.TestCase):
    """A pool-skip marker must classify from the full backend output, not from
    whatever the message excerpt happened to keep."""

    @pytest.mark.regression(
        "opencode-access-disabled-classified-beyond-stdout-head",
        "AssertionError: False is not true",
    )
    def test_marker_past_the_stdout_cut_still_skips(self) -> None:
        vendor = "Model access is disabled for this model id"
        exc = llm_backends._backend_failure(
            "opencode exited 1",
            stderr="",
            stdout=("event line\n" * 60) + vendor,
        )
        self.assertTrue(second_opinion._pool_skip_condition("opencode", exc))
        # Positive control: the same marker with no excerpting involved.
        short = llm_backends._backend_failure(
            "opencode exited 1", stderr="", stdout=vendor
        )
        self.assertTrue(second_opinion._pool_skip_condition("opencode", short))

    def test_marker_in_the_omitted_middle_of_stderr_still_skips(self) -> None:
        # The excerpt drops the middle; classification must not consult it.
        stderr = (
            "[opencode] starting\n" * 100
            + "Model access is disabled\n"
            + "[opencode] trailing chatter\n" * 100
        )
        exc = llm_backends._backend_failure("opencode exited 1", stderr=stderr)
        self.assertNotIn("Model access is disabled", str(exc))
        self.assertTrue(second_opinion._pool_skip_condition("opencode", exc))


class SharedFamilyWarningTests(unittest.TestCase):
    """probe's no-shared-family check over the models a rotated review uses."""

    def setUp(self) -> None:
        # `probe` also reads each backend's `--version`; fake the subprocess so
        # no test here can reach a real Popen (the sandbox guard aborts those).
        version = patch(
            "subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, stdout="1.0.0\n", stderr=""),
        )
        version.start()
        self.addCleanup(version.stop)

    def test_family_token(self) -> None:
        cases = {
            "opencode-go/kimi-k2.7-code": "kimi",
            "gpt-5.6-luna": "gpt",
            "Gemini 3.8 Flash (High)": "gemini",
            "qwen3.8-flash": "qwen",
            "o3": "o",
            "3x-model": None,
        }
        for model, token in cases.items():
            self.assertEqual(second_opinion._family_token(model), token, model)

    def test_shared_pool_family_warns(self) -> None:
        env = {
            "SECOND_OPINION_PI_MODEL_POOL": "glm-5.3-flash,kimi-k2.7-code",
            "SECOND_OPINION_OPENCODE_MODEL_POOL": "opencode-go/qwen3.8,opencode-go/kimi-k2.7-code",
            "SECOND_OPINION_AGY_MODEL": "gemini-x",
        }
        warnings = second_opinion._shared_family_warnings(env)
        self.assertEqual(len(warnings), 1)
        self.assertIn("kimi", warnings[0])
        self.assertIn("pi", warnings[0])
        self.assertIn("opencode", warnings[0])

    def test_disjoint_pools_are_quiet(self) -> None:
        env = {
            "SECOND_OPINION_PI_MODEL_POOL": "glm-5.3-flash,glm-5.2",
            "SECOND_OPINION_OPENCODE_MODEL_POOL": "opencode-go/qwen3.8",
            "SECOND_OPINION_AGY_MODEL": "gemini-x",
        }
        self.assertEqual(second_opinion._shared_family_warnings(env), [])

    def test_single_override_hides_its_pool(self) -> None:
        env = {
            "SECOND_OPINION_PI_MODEL": "glm-5.2",
            "SECOND_OPINION_PI_MODEL_POOL": "kimi-k2.7-code",
            "SECOND_OPINION_OPENCODE_MODEL_POOL": "opencode-go/kimi-k2.7-code",
            "SECOND_OPINION_AGY_MODEL": "gemini-x",
        }
        self.assertEqual(second_opinion._shared_family_warnings(env), [])

    def test_agy_builtin_default_counts(self) -> None:
        env = {"SECOND_OPINION_COPILOT_MODEL_POOL": "gemini-3-pro"}
        warnings = second_opinion._shared_family_warnings(env)
        self.assertEqual(len(warnings), 1)
        self.assertIn("gemini", warnings[0])

    def _probe(self, quiet: bool) -> tuple[str, str]:
        env = {
            "SECOND_OPINION_PI_MODEL_POOL": "kimi-a",
            "SECOND_OPINION_OPENCODE_MODEL_POOL": "opencode-go/kimi-b",
        }
        out, err = io.StringIO(), io.StringIO()
        with (
            patch.dict(os.environ, env),
            patch("shutil.which", side_effect=lambda b: f"/usr/bin/{b}"),
            patch.object(
                second_opinion,
                "BACKEND_RUNNERS",
                {"opencode": lambda p, model_index=None, mode=None: "ok"},
            ),
            patch("sys.stdout", out),
            patch("sys.stderr", err),
        ):
            second_opinion.cmd_probe(
                argparse.Namespace(
                    cmd="probe", backend=["opencode"], quiet=quiet, verbose=False
                )
            )
        return out.getvalue(), err.getvalue()

    def test_probe_prints_warning_on_stderr_only(self) -> None:
        out, err = self._probe(quiet=False)
        self.assertIn("[second_opinion] warning:", err)
        self.assertIn("kimi", err)
        self.assertNotIn("warning", out)
        json.loads(out)

    def test_probe_warning_silenced_by_quiet(self) -> None:
        out, err = self._probe(quiet=True)
        self.assertEqual(len(json.loads(out)["probes"]), 1)
        self.assertNotIn("warning", err)
