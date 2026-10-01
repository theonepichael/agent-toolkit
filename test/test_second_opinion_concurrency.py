#!/usr/bin/env python3
"""Concurrent second_opinion.py review runs must not splice each other's output.

Observed failure: parallel agents (sub-agents sharing one scratch directory)
each ran ``second_opinion.py review ... > r1.txt 2>&1; cat r1.txt``. A ``>``
redirect truncates at open and every process then writes at its own offset;
the script prints its whole critique at the end, at offset 0, so the last run
to finish overwrote the head of the file and an earlier, longer critique left
its tail behind — each caller read its own critique followed by mid-word
fragments of another plan's critique.

The subprocess tests here reproduce that with the real script and a fake
``codex`` on PATH (no real backend, an isolated HOME). Each child opens the
shared path itself, the way two separate shell redirects do: ``flock`` does not
conflict within one inherited open file description.
"""

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent-scripts"))
import test_bootstrap  # noqa: E402,F401
import pytest  # noqa: E402
import second_opinion  # noqa: E402

SCRIPT = Path(__file__).resolve().parent.parent / "agent-scripts" / "second_opinion.py"

# The fake backend: the long critique returns fast, the short one slowly, so
# without a guard the short run finishes last and overwrites only the head of
# the long run's bytes. Every invocation is logged so a refused run can be
# shown never to have reached the backend.
FAKE_CODEX = """#!/usr/bin/env python3
import sys, time, os
prompt = sys.argv[-1]
with open(os.environ["FAKE_CODEX_LOG"], "a") as log:
    log.write("PLAN_LONG\\n" if "PLAN_LONG" in prompt else "PLAN_SHORT\\n")
if "PLAN_LONG" in prompt:
    time.sleep(1.5)
    print("LONG-CRITIQUE " * 400)
else:
    time.sleep(3.0)
    print("short-critique " * 20)
"""


def _env(tmp: Path) -> dict[str, str]:
    bin_dir = tmp / "bin"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / "codex"
    fake.write_text(FAKE_CODEX)
    fake.chmod(0o755)
    home = tmp / "home"
    home.mkdir(exist_ok=True)
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("SECOND_OPINION_") and k != "AGENT_TOOLKIT_HOME"
    }
    env.update(
        HOME=str(home),
        XDG_STATE_HOME=str(home / ".local" / "state"),
        PATH=f"{bin_dir}{os.pathsep}{env.get('PATH', '')}",
        FAKE_CODEX_LOG=str(tmp / "codex.log"),
    )
    return env


def _start(
    tmp: Path, env: dict[str, str], plan: str, out: Path, err: Path | None
) -> subprocess.Popen[bytes]:
    plan_file = tmp / f"{plan}.md"
    plan_file.write_text(f"{plan}: do one small thing\n")
    # Each child opens the shared path itself, truncating it, like `> file`.
    stdout = open(out, "wb")  # noqa: SIM115 — handed to the child, closed below
    stderr = open(err, "wb") if err is not None else subprocess.STDOUT  # noqa: SIM115
    try:
        return subprocess.Popen(
            [
                sys.executable,
                str(SCRIPT),
                "review",
                str(plan_file),
                "--backend",
                "codex",
                "--text-only",
                "--quiet",
                "--run-id",
                f"concurrency-{plan}",
            ],
            stdout=stdout,
            stderr=stderr,
            env=env,
            cwd=tmp,
        )
    finally:
        stdout.close()
        if err is not None:
            stderr.close()  # type: ignore[union-attr]


def _wait_for_backend_call(log: Path, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if log.exists() and log.read_text():
            return
        time.sleep(0.05)
    raise AssertionError("the first run never reached the backend")


def _run_pair(tmp: Path, *, separate_stderr: bool) -> tuple[int, int, str, str]:
    env = _env(tmp)
    shared = tmp / "shared.txt"
    err_a = tmp / "a.err" if separate_stderr else None
    err_b = tmp / "b.err" if separate_stderr else None
    first = _start(tmp, env, "PLAN_LONG", shared, err_a)
    # The second run starts only once the first is mid-flight, so the two
    # lifetimes overlap the way the parallel agents' runs did.
    _wait_for_backend_call(tmp / "codex.log")
    second = _start(tmp, env, "PLAN_SHORT", shared, err_b)
    rc_a = first.wait(timeout=60)
    rc_b = second.wait(timeout=60)
    calls = (tmp / "codex.log").read_text()
    b_err = err_b.read_text() if err_b is not None else ""
    return rc_a, rc_b, calls, shared.read_text(errors="replace") + "\0" + b_err


@pytest.mark.allow_real_subprocess  # runs the script with a fake codex on PATH
@pytest.mark.regression(
    "concurrent-review-redirects-splice-critiques",
    "AssertionError: Second opinion via Codex CLI:",
)
def test_concurrent_runs_sharing_one_output_file_never_splice(tmp_path: Path) -> None:
    rc_a, rc_b, calls, combined = _run_pair(tmp_path, separate_stderr=False)
    shared, _ = combined.split("\0", 1)
    # The second run must refuse before reaching the backend...
    assert rc_b == 1, shared
    assert calls.splitlines() == ["PLAN_LONG"], calls
    assert rc_a == 0, shared
    # ...and the file must hold no foreign critique bytes.
    assert "short-critique" not in shared, shared
    assert "LONG-CRITIQUE" in shared
    # The holder saw its file disturbed by the refusal and said so.
    assert "another process wrote to this run's output file" in shared


@pytest.mark.allow_real_subprocess  # runs the script with a fake codex on PATH
@pytest.mark.regression(
    "concurrent-review-stdout-only-collision-not-refused",
    "assert 0 == 1",
)
def test_stdout_only_collision_refuses_and_leaves_holder_clean(tmp_path: Path) -> None:
    rc_a, rc_b, calls, combined = _run_pair(tmp_path, separate_stderr=True)
    shared, b_err = combined.split("\0", 1)
    assert rc_b == 1
    assert "another second_opinion.py run is writing" in b_err, b_err
    assert calls.splitlines() == ["PLAN_LONG"], calls
    assert rc_a == 0
    assert shared.startswith("Second opinion via"), shared[:200]
    assert "short-critique" not in shared


@pytest.mark.allow_real_subprocess  # runs the script with a fake codex on PATH
def test_concurrent_runs_to_separate_files_both_succeed(tmp_path: Path) -> None:
    env = _env(tmp_path)
    a = _start(tmp_path, env, "PLAN_LONG", tmp_path / "a.txt", None)
    b = _start(tmp_path, env, "PLAN_SHORT", tmp_path / "b.txt", None)
    assert a.wait(timeout=60) == 0
    assert b.wait(timeout=60) == 0
    out_a = (tmp_path / "a.txt").read_text()
    out_b = (tmp_path / "b.txt").read_text()
    assert "LONG-CRITIQUE" in out_a and "short-critique" not in out_a
    assert "short-critique" in out_b and "LONG-CRITIQUE" not in out_b


def test_lock_skips_streams_that_are_not_regular_files(tmp_path: Path) -> None:
    import io

    assert second_opinion._lock_output_file(io.StringIO()) is None
    with open(os.devnull, "w") as devnull:
        assert second_opinion._lock_output_file(devnull) is None
    r, w = os.pipe()
    try:
        with os.fdopen(w, "w") as pipe_w:
            assert second_opinion._lock_output_file(pipe_w) is None
    finally:
        os.close(r)
    with open(tmp_path / "out.txt", "w") as regular:
        assert second_opinion._lock_output_file(regular) is not None


@pytest.mark.regression(
    "run-state-fixed-temp-name-rename-race",
    "AssertionError: assert [FileNotFound...r directory')] == []",
)
def test_concurrent_same_key_state_saves_never_collide(tmp_path: Path) -> None:
    """Both temp writes land before either rename: a fixed temp name makes the
    second rename find its source already moved away."""
    target = tmp_path / "state.json"
    barrier = threading.Barrier(2, timeout=5)
    real_replace = os.replace
    errors: list[BaseException] = []

    def gated_replace(src: str | Path, dst: str | Path) -> None:
        barrier.wait()
        real_replace(src, dst)

    def save(n: int) -> None:
        try:
            second_opinion._save_run_state(target, {"count": n})
        except BaseException as exc:  # noqa: BLE001 — collected for the assert
            errors.append(exc)

    with patch.object(second_opinion.os, "replace", gated_replace):
        threads = [threading.Thread(target=save, args=(n,)) for n in (1, 2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
    assert errors == []
    assert target.is_file()
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


@pytest.mark.allow_real_subprocess  # runs the script with a fake codex on PATH
@pytest.mark.regression(
    "refusal-tail-survives-short-holder-critique",
    "AssertionError: [second_opinion] warning: another process wrote to this run's output file while it ran",
)
def test_short_holder_critique_leaves_no_refusal_tail(tmp_path: Path) -> None:
    """The refused run's `2>&1` refusal lands in the shared file first; a holder
    whose own output is shorter must not leave the refusal's tail behind."""
    env = _env(tmp_path)
    fake = tmp_path / "bin" / "codex"
    fake.write_text(
        FAKE_CODEX.replace('print("LONG-CRITIQUE " * 400)', 'print("LONG-CRITIQUE")')
    )
    shared = tmp_path / "shared.txt"
    first = _start(tmp_path, env, "PLAN_LONG", shared, None)
    _wait_for_backend_call(tmp_path / "codex.log")
    second = _start(tmp_path, env, "PLAN_SHORT", shared, None)
    assert second.wait(timeout=60) == 1
    assert first.wait(timeout=60) == 0
    text = shared.read_text()
    assert text.rstrip().endswith("LONG-CRITIQUE"), text
    assert "never a fixed name" not in text, text


@pytest.mark.regression(
    "append-mode-output-false-tamper-warning",
    "AssertionError: assert 'another process wrote' not in '[second_opi...e (mktemp)\\n'",
)
def test_append_mode_output_is_not_reported_as_tampered(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A shell `>>` hands over an O_APPEND fd still at offset 0 on a non-empty
    file (unlike Python's own "a" mode, which seeks to the end)."""
    out = tmp_path / "log.txt"
    out.write_text("an earlier round's output\n")
    fd = os.open(out, os.O_WRONLY | os.O_APPEND)
    try:
        assert os.lseek(fd, 0, os.SEEK_CUR) == 0
        second_opinion._warn_if_output_tampered(fd)
    finally:
        os.close(fd)
    assert "another process wrote" not in capsys.readouterr().err


@pytest.mark.regression(
    "disturbed-review-consumes-round",
    "AssertionError: Expected '_record_successful_review' to not have been called. Called 1 times.",
)
def test_disturbed_review_does_not_consume_a_round(tmp_path: Path) -> None:
    """The tamper warning tells the caller to rerun the round, so the disturbed
    run must not spend one of the run's capped rounds."""
    plan = tmp_path / "plan.md"
    plan.write_text("a plan\n")
    args = second_opinion.build_parser().parse_args(
        ["review", str(plan), "--run-id", "disturbed", "--backend", "codex"]
    )
    result = second_opinion.ReviewResult(
        backend_label="Codex CLI", response_text="critique", bytes_saved=0, notices=()
    )
    with (
        patch.object(second_opinion, "review_plan", return_value=result),
        patch.object(second_opinion, "_refuse_if_cap_reached"),
        patch.object(second_opinion, "_lock_output_file", return_value=None),
        patch.object(second_opinion, "_warn_if_output_tampered", return_value=True),
        patch.object(second_opinion, "_record_successful_review") as record,
    ):
        second_opinion.cmd_review(args)
    record.assert_not_called()
