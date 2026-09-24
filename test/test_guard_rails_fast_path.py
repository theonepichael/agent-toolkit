"""guard_rails.py's fast path: it may only answer calls whose verdict is a
plain allow, and its answer must be indistinguishable from the full path's.

The identity tests compare both the fast path and the forced full path
(GUARD_RAILS_NO_FAST_PATH=1) against test/fixtures/guard_fast_path_baseline.json,
recorded from the script as it was before the fast path existed.
"""

from __future__ import annotations

import ast
import io
import json
import sys
from pathlib import Path
from unittest import mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent-scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import guard_fast_path_matrix as matrix  # noqa: E402
import guard_rails  # noqa: E402

# ── the trigger classifier ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "command",
    [
        "ls",
        "git status",
        "git commit -m 'fix the thing'",
        "uv run pytest -q",
        "cat README.md",
        "",
    ],
)
def test_ordinary_commands_are_trigger_free(command: str) -> None:
    assert guard_rails.bash_trigger_free(command)


# One command per deny branch of evaluate_bash_override, each with the quote-
# and escape-splicing variants a shell would still resolve to the same thing.
DENY_BRANCH_EXAMPLES = {
    "hookspath-shell-substitution": "git config core.hooksPath $X",
    "no-verify": "git commit --no-verify -m x",
    "hookspath-config-override": "git -c core.hooksPath=/tmp/h commit -m x",
    "hookspath-env-config-key": "GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_COUNT=1 git commit",
    "hookspath-env-config-global": "GIT_CONFIG_GLOBAL=/tmp/g git commit -m x",
    "hookspath-env-config-parameters": "GIT_CONFIG_PARAMETERS=x git commit -m x",
    "hookspath-mutation": "git config core.hooksPath /tmp/h",
    "hookspath-mutation-unbalanced-quote": "git config core.hooksPath /tmp/h 'oops",
    "git-config-file-write": "echo x >> .git/config",
    "git-config-file-sed": "sed -i s/a/b/ .git/config",
    "git-config-file-tee": "echo x | tee .git/config",
}


def _splices(command: str) -> list[str]:
    out = [command]
    for word in ("config", "--no-verify", "hooksPath"):
        if word in command:
            mid = len(word) // 2
            out.append(command.replace(word, word[:mid] + '""' + word[mid:]))
            out.append(command.replace(word, word[:mid] + "\\" + word[mid:]))
            out.append(command.replace(word, word[:mid] + "''" + word[mid:]))
            out.append(command.replace(word, word.upper()))
    return out


def _protected_branch_verdict(command: str) -> guard_rails.Verdict:
    info = guard_rails.RepoInfo(
        toplevel="/r", common_dir="/r/.git", branch="main", is_worktree=False, is_bare=False
    )
    with mock.patch.object(guard_rails, "repo_info", return_value=info):
        return guard_rails.evaluate_bash_override(command, "/r")


@pytest.mark.parametrize("branch", sorted(DENY_BRANCH_EXAMPLES))
def test_every_deny_branch_example_denies_and_is_a_trigger(branch: str) -> None:
    command = DENY_BRANCH_EXAMPLES[branch]
    assert _protected_branch_verdict(command).decision == "deny", command
    for variant in _splices(command):
        assert not guard_rails.bash_trigger_free(variant), variant


def _string_constants(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def test_no_command_the_rules_deny_is_trigger_free() -> None:
    """Superset check over every string in the bash-rule tests, and every
    splice variant of each: whatever evaluate_bash_override denies on a
    protected branch, the fast path must never call trigger-free."""
    candidates: set[str] = set(DENY_BRANCH_EXAMPLES.values())
    for name in ("test_guard_rails.py", "test_guard_rails_topology.py"):
        candidates |= _string_constants(Path(__file__).parent / name)
    denied = 0
    for base in sorted(candidates):
        for command in _splices(base):
            if _protected_branch_verdict(command).decision == "deny":
                denied += 1
                assert not guard_rails.bash_trigger_free(command), command
    assert denied >= len(DENY_BRANCH_EXAMPLES)


# ── end-to-end identity against the pre-fast-path baseline ─────────────────

_BASELINE = json.loads(matrix.BASELINE.read_text())


# Runs the real guard_rails.py in a subprocess under a throwaway HOME (the
# matrix module builds the sandbox and a git repo there): the fast path runs
# before any import a test could mock, so only a real run reaches it.
@pytest.mark.allow_real_subprocess
@pytest.mark.parametrize("mode", ["fast", "forced-slow"])
@pytest.mark.parametrize("case", matrix.CASES, ids=[c[0] for c in matrix.CASES])
def test_matches_the_pre_fast_path_baseline(case: tuple, mode: str) -> None:
    extra = {"GUARD_RAILS_NO_FAST_PATH": "1"} if mode == "forced-slow" else {}
    assert matrix.run_case(case, extra) == _BASELINE[case[0]]


def test_baseline_covers_every_case() -> None:
    assert sorted(_BASELINE) == sorted(c[0] for c in matrix.CASES)


# ── which calls the fast path actually claims ──────────────────────────────

FAST_CASES = {
    "claude-bash-ls",
    "claude-bash-padded-name",
    "claude-bash-lower-name",
    "claude-bash-secret",
    "claude-bash-empty-command",
    "claude-read",
    "claude-glob",
    "claude-nonstr-name",
    "claude-empty-stdin",
    "agy-view",
    "agy-bash",
    "copilot-view",
    "copilot-bad-args",
    "neutral-bash-ls",
    "neutral-bash-empty",
    "off-claude-bash-ls",
    "off-claude-read",
    "off-copilot-view",
    "off-neutral-bash-ls",
    "off-broken-claude-bash-ls",
}


def _decide(case: tuple) -> object:
    _, argv, payload, env_over, pointer = case
    argv = matrix._subst(argv, "/repo")
    if argv[0] == "--harness":
        raw = payload if isinstance(payload, str) else json.dumps(
            matrix._subst(payload, "/repo")
        )
    else:
        raw = None
    layout = mock.patch.object(
        guard_rails,
        "_layout_error_reason",
        return_value="broken" if pointer is not None else None,
    )
    with mock.patch.dict("os.environ", env_over), layout:
        try:
            return guard_rails._fast_decide(argv, raw)
        except Exception:
            return None


def _claimable(case: tuple) -> bool:
    argv = case[1]
    return (len(argv) == 2 and argv[0] == "--harness") or (
        len(argv) == 6 and argv[0::2] == ["--tool", "--cwd", "--command"]
    )


@pytest.mark.parametrize("case", matrix.CASES, ids=[c[0] for c in matrix.CASES])
def test_fast_path_claims_exactly_the_expected_calls(case: tuple) -> None:
    claimed = _claimable(case) and _decide(case) is not None
    assert claimed == (case[0] in FAST_CASES)


# ── fall-through only before output, always with stdin restored ────────────

_PAYLOAD = json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": "/"})


def _run_fast(argv: list[str], stdin_text: str) -> tuple[bool, str, str]:
    out = io.StringIO()
    with (
        mock.patch.object(sys, "stdin", io.StringIO(stdin_text)),
        mock.patch.object(sys, "stdout", out),
        mock.patch.object(guard_rails, "_append_audit_record"),
    ):
        handled = guard_rails._fast_path(argv)
        rest = sys.stdin.read()
    return handled, out.getvalue(), rest


@pytest.mark.parametrize(
    "stage", ["bash_trigger_free", "_layout_error_reason", "_audit_record", "tool_family"]
)
def test_an_exception_in_any_decide_stage_falls_through_with_stdin_restored(
    stage: str,
) -> None:
    with mock.patch.object(guard_rails, stage, side_effect=RuntimeError("boom")):
        handled, out, rest = _run_fast(["--harness", "claude"], _PAYLOAD)
    assert (handled, out, rest) == (False, "", _PAYLOAD)


def test_a_layout_error_falls_through_with_stdin_restored() -> None:
    with mock.patch.object(guard_rails, "_layout_error_reason", return_value="broken"):
        handled, out, rest = _run_fast(["--harness", "claude"], _PAYLOAD)
    assert (handled, out, rest) == (False, "", _PAYLOAD)


def test_a_handled_call_writes_one_verdict() -> None:
    with mock.patch.object(guard_rails, "_layout_error_reason", return_value=None):
        handled, out, _ = _run_fast(["--harness", "claude"], _PAYLOAD)
    assert handled
    assert out == json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse"}}) + "\n"


def test_an_audit_failure_after_output_never_falls_through() -> None:
    """Once the verdict is on stdout, a failure must not send the call on to
    main(), which would write a second verdict."""
    out = io.StringIO()
    with (
        mock.patch.object(sys, "stdin", io.StringIO(_PAYLOAD)),
        mock.patch.object(sys, "stdout", out),
        mock.patch.object(guard_rails, "_layout_error_reason", return_value=None),
        mock.patch.object(
            guard_rails, "_append_audit_record", side_effect=RuntimeError("disk")
        ),
        pytest.raises(RuntimeError),
    ):
        guard_rails._fast_path(["--harness", "claude"])
    assert out.getvalue().count("\n") == 1


def test_the_kill_switch_skips_the_fast_path_without_reading_stdin() -> None:
    with mock.patch.dict("os.environ", {"GUARD_RAILS_NO_FAST_PATH": "1"}):
        handled, out, rest = _run_fast(["--harness", "claude"], _PAYLOAD)
    assert (handled, out, rest) == (False, "", _PAYLOAD)


@pytest.mark.parametrize(
    "argv",
    [
        ["--harness", "claude", "-v"],
        ["--harness", "claude", "-q"],
        ["--tool", "bash", "--cwd", "/", "--command", "ls", "-v"],
        ["--cwd", "/", "--tool", "bash", "--command", "ls"],
        ["--tool", "edit", "--cwd", "/", "--command", "ls"],
        [],
    ],
)
def test_other_argv_shapes_are_never_claimed(argv: list[str]) -> None:
    handled, out, rest = _run_fast(argv, _PAYLOAD)
    assert (handled, out, rest) == (False, "", _PAYLOAD)
