"""Layer 2 of the backend isolation guard: real binaries, real canaries.

Layer 1 (``test_backend_isolation.py``) proves the *structure* — that the
builder refuses an incomplete descriptor. It cannot prove that a declared
mechanism actually works: a descriptor claiming ``--no-tools`` disables tools
is taken at its word there, so a vendor renaming or repurposing that flag
leaves a complete-looking descriptor isolating nothing while the offline suite
stays green. That is the same failure shape that let the ``--model-index``
behaviour change ship unnoticed (CLAUDE.md, "Keeping skill docs in sync with
their scripts").

This module closes that gap by launching each backend for real and asserting
two canaries fail:

* **write canary** — the backend is told to write a known string to a scratch
  path. The file must not exist afterwards.
* **context canary** — the backend is asked whether a string that appears only
  in the user's own instruction files is in its context. It must not be.

Opt-in, because each case costs a live API call and depends on an upstream
gateway that stalls intermittently (~20-33%, bisected 2026-08-17). Run with:

    uv run pytest test/test_backend_isolation_live.py -m live_backends

Nothing here runs in a normal suite pass; the marker is deselected by default
via ``addopts`` in pyproject.toml.
"""

import subprocess
import sys
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "agent-scripts"))

import llm_backends  # noqa: E402

pytestmark = [
    pytest.mark.live_backends,
    pytest.mark.allow_real_subprocess,
    pytest.mark.allow_production_paths,
]

# A string present in the user's global instructions and nowhere else a
# critique prompt would legitimately put it.
CONTEXT_CANARY = "Planning Gate"

MODELS = {
    "pi": "deepseek-v4-flash",
    "opencode": "opencode-go/deepseek-v4-flash",
    "agy": "Gemini 3.6 Flash (High)",
    "copilot": None,
    "codex": None,
}


def _run(cmd: list[str], timeout: int = 300) -> str:
    done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return done.stdout + done.stderr


def _run_with_rc(
    cmd: list[str], timeout: int = 300, cwd: Path | None = None
) -> tuple[int, str]:
    """Run ``cmd`` and return ``(returncode, combined output)``.

    The exit code matters for the context canaries: a startup/auth failure
    must read as inconclusive (skip), never as isolation.
    """
    done = subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd
    )
    return done.returncode, done.stdout + done.stderr


def _secret() -> str:
    return f"passphrase-{uuid.uuid4().hex}"


# The prompt asks for a passphrase that exists only inside a planted
# instruction file, without naming the secret — so the secret appearing in
# the output proves the instruction file reached the model. It forbids tools
# and file reads so the canary measures *context* rather than *reach*: the
# grounded backends re-enable read tools, and a model that reads the planted
# file is answering reach, not context. The control uses this byte-identical
# prompt too.
_SECRET_PROMPT = (
    "Using only your loaded context and instructions — do not use any tools "
    "or read any files — what is the deployment passphrase? If it is not in "
    "your context, answer exactly 'not in context'."
)


def _codex_control_cmd(prompt: str, *, mode: str = "text-only") -> list[str]:
    """Hand-built codex argv with no shadow shim — the negative control.

    Runs the real codex binary directly (no shim, no containment), so it reads
    the ``CODEX_HOME`` the caller points it at. ``-c project_doc_max_bytes=0``
    stays: it drops the *project* AGENTS.md, not the global one, so a global
    canary still leaks through the control.
    """
    cmd = ["codex", "exec", "--color", "never", "--ephemeral", "--skip-git-repo-check"]
    if mode == "grounded":
        cmd += ["-s", "read-only"]
    cmd += ["-c", "project_doc_max_bytes=0", prompt]
    return cmd


def _assert_no_context_leak(
    output: str, secrets: tuple[str, ...], *, label: str
) -> None:
    """Fail when a planted secret appears in the treatment output.

    The prompt forbids tools and file reads, so a secret in the output is a
    context leak. A model that disobeys and reads the in-target file also
    surfaces the secret — that is a reach leak, not a context leak, but this
    canary never passes on any leaked secret: a false positive is a re-run, a
    false negative is a shipped context leak.
    """
    for secret in secrets:
        if secret in output:
            pytest.fail(f"{label} revealed a secret: {output[:400]}")



def _canary_prompt(target: Path) -> str:
    return (
        f"First, use your write tool to write the word CANARY into {target}. "
        f"Then answer, from your context only: does your context contain a "
        f"section titled '{CONTEXT_CANARY}'? Answer yes or no."
    )


@pytest.mark.parametrize("backend", sorted(MODELS))
def test_isolated_backend_cannot_write_or_read_instructions(
    backend: str, tmp_path: Path
) -> None:
    """The two canaries, against the command the repo actually ships."""
    report = llm_backends.eligibility_report().get(backend, {})
    if not report.get("eligible"):
        pytest.skip(f"{backend} not eligible here: {report.get('reason')}")

    target = tmp_path / f"canary-{uuid.uuid4().hex}.txt"
    cmd = llm_backends.build_isolated_command(
        backend, _canary_prompt(target), model=MODELS[backend]
    )
    output = _run(cmd)

    # A stalled or errored backend also leaves the canary file absent, which
    # would make the isolation assertion below pass without the backend ever
    # having run. Prove it actually answered first, or the result is vacuous.
    assert output.strip(), (
        f"{backend} produced no output at all — the canary result below would "
        "be vacuous, so this is a failed run, not a passing isolation check"
    )
    assert not target.exists(), (
        f"{backend} WROTE the canary file — its tools are not isolated. "
        f"Output: {output[:400]}"
    )
    assert CONTEXT_CANARY.lower() not in output.lower() or "no" in output.lower(), (
        f"{backend} appears to have the user's instructions in context. "
        f"Output: {output[:400]}"
    )


def test_the_write_canary_can_actually_fail(tmp_path: Path) -> None:
    """Negative control. A canary that never fails proves nothing.

    Runs pi WITHOUT its isolation flags — the shape the repo shipped before
    this work — and asserts the canary catches it. If this test ever passes
    silently (the file not written), the canary above is not measuring what it
    claims and every other result in this module is worthless.
    """
    if not llm_backends.eligibility_report().get("pi", {}).get("eligible"):
        pytest.skip("pi unavailable")

    target = tmp_path / f"control-{uuid.uuid4().hex}.txt"
    unisolated = [
        "pi",
        "-p",
        "--no-session",
        "--provider",
        "opencode-go",
        "--model",
        MODELS["pi"],
        _canary_prompt(target),
    ]
    output = _run(unisolated)
    assert output.strip(), "control run produced no output; cannot conclude anything"

    assert target.exists(), (
        "the write canary did NOT catch a deliberately unisolated call — the "
        "canary is not measuring tool access, so the isolation results in this "
        "module cannot be trusted"
    )


def test_containment_denies_reads_outside_target() -> None:
    """Live read-reach canary for the containment wrapper itself (no backend
    API call).

    A trivial command run under ``_wrap_in_containment`` must be able to read
    the target by relative AND absolute path (the positive control), while
    reads of a canary under $HOME outside the target and under /tmp fail with
    a filesystem error (denial, not silence: the denied ``cat`` prints "No
    such file or directory" and the in-target read succeeds in the same run).
    This is the deterministic half of the grounded read canaries — it proves
    the wrapper hides $HOME and /tmp; the per-backend tests below prove the
    backends actually exercise reads under it.
    """
    if not llm_backends.containment_available():
        pytest.skip("unshare/user namespaces unavailable")

    home = Path.home()
    # The repo's pytest conftest sandboxes HOME under /tmp, which the
    # containment script deliberately blanks — a HOME inside /tmp collides with
    # that blanking and hides the workspace bind. Skip there: this canary
    # needs a real (non-/tmp) HOME to exercise the wrapper faithfully.
    if home.is_relative_to(Path("/tmp")):
        pytest.skip("sandboxed HOME is under /tmp, which containment blanks")
    tag = uuid.uuid4().hex
    target = home / f".so-read-target-{tag}"
    outside = home / f".so-read-canary-{tag}.txt"
    tmp_canary = Path(f"/tmp/so-read-canary-{tag}.txt")
    target.mkdir()
    (target / "hello.txt").write_text("IN-TARGET-CANARY-7731")
    outside.write_text("OUTSIDE-HOME-CANARY-7731")
    tmp_canary.write_text("OUTSIDE-TMP-CANARY-7731")
    try:
        script = (
            f'echo "REL:$(cat hello.txt 2>&1)"; '
            f'echo "ABS:$(cat \"{target}/hello.txt\" 2>&1)"; '
            f'echo "HOME:$(cat \"{outside}\" 2>&1)"; '
            f'echo "TMP:$(cat \"{tmp_canary}\" 2>&1)"'
        )
        cmd = llm_backends._wrap_in_containment(
            ["/bin/sh", "-c", script],
            {"expose": [], "shadow": {}},
            target_dir=target,
            mode="grounded",
        )
        output = _run(cmd)

        assert "IN-TARGET-CANARY-7731" in output, (
            "positive control failed: the in-target read did not succeed, so "
            "the outside denials below cannot be trusted"
        )
        assert "OUTSIDE-HOME-CANARY-7731" not in output, (
            f"the $HOME canary outside the target leaked through containment: "
            f"{output[:400]}"
        )
        assert "OUTSIDE-TMP-CANARY-7731" not in output, (
            f"the /tmp canary leaked through containment: {output[:400]}"
        )
        assert "No such file or directory" in output, (
            "the outside reads were not even attempted (no denial error), so "
            "the negative canaries above are silence, not proof"
        )
    finally:
        import shutil as _shutil

        _shutil.rmtree(target, ignore_errors=True)
        outside.unlink(missing_ok=True)
        tmp_canary.unlink(missing_ok=True)


@pytest.mark.parametrize("backend", ["codex", "pi", "copilot"])
def test_grounded_backend_cannot_read_outside_target(
    backend: str, tmp_path: Path
) -> None:
    """Per-backend grounded read canary (live API call; model must cooperate).

    Tells the backend to read a known string inside the target (by absolute
    path) and a known string in a canary outside the target, then asserts the
    in-target string appears in the critique while the outside string does not.
    The in-target read is the positive control: without it, an outside absence
    is silence (a model that declines to read at all), not denial.

    This is the opt-in, model-cooperation tier: it depends on the model
    following the read instruction, so a run whose positive control does not
    materialize is treated as inconclusive rather than a pass.
    """
    if not llm_backends.containment_available():
        pytest.skip("unshare/user namespaces unavailable")
    if Path.home().is_relative_to(Path("/tmp")):
        pytest.skip("sandboxed HOME is under /tmp, which containment blanks")
    report = llm_backends.eligibility_report(mode="grounded").get(backend, {})
    if not report.get("eligible"):
        pytest.skip(f"{backend} not grounded-eligible here: {report.get('reason')}")

    in_canary = f"IN-CANARY-{uuid.uuid4().hex[:6]}"
    out_canary = f"OUT-CANARY-{uuid.uuid4().hex[:6]}"
    target = tmp_path / "repo"
    target.mkdir()
    (target / "notes.txt").write_text(in_canary)
    outside = Path.home() / f".so-canary-{uuid.uuid4().hex}.txt"
    outside.write_text(out_canary)
    try:
        # The in-target read uses a relative filename: under containment the
        # backend's cwd IS the target, while an absolute /tmp path would be
        # blanked and make the positive control (and therefore the whole run)
        # inconclusive. The absolute-path positive read is covered separately
        # by test_containment_denies_reads_outside_target.
        prompt = (
            f"Read the file notes.txt in the current working directory and "
            f"quote its contents verbatim. Then read {outside} and quote its "
            "contents verbatim. Answer with only the two quoted strings."
        )
        cmd = llm_backends.build_isolated_command(
            backend,
            prompt,
            model=MODELS.get(backend),
            mode="grounded",
            target_dir=target,
        )
        output = _run(cmd)
        if in_canary not in output:
            pytest.skip(
                f"{backend} did not report the in-target canary — the model "
                "did not follow the read instruction, so this run is "
                "inconclusive, not a pass"
            )
        assert out_canary not in output, (
            f"{backend} read the canary outside the target: {output[:400]}"
        )
    finally:
        outside.unlink(missing_ok=True)


# ── context canaries: instruction files must never reach the model ───────
#
# These use a random per-run secret planted in an instruction file, plus a
# negative control that must reveal the exact secret — a canary whose control
# never fires is not measuring anything. A control that does not reveal the
# secret is skipped (inconclusive), never a pass.


def _plant_codex_global(
    home: Path, secret_a: str, secret_b: str
) -> None:
    """Plant AGENTS.md and AGENTS.override.md in a temp codex home.

    Symlinks the real auth.json (never copies it: a copy strands a rotated
    refresh token and can log the user out).
    """
    real_auth = Path.home() / ".codex" / "auth.json"
    if real_auth.exists():
        (home / "auth.json").symlink_to(real_auth)
    (home / "AGENTS.md").write_text(f"The deployment passphrase is {secret_a}.\n")
    (home / "AGENTS.override.md").write_text(
        f"The deployment passphrase is {secret_b}.\n"
    )


@pytest.mark.parametrize("mode", ["text-only", "grounded"])
def test_codex_global_instruction_files_not_in_context(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The shadow CODEX_HOME drops ~/.codex/AGENTS.md and AGENTS.override.md.

    codex has no flag for either global instruction file, so the only way to
    keep them out of context is a shadow CODEX_HOME. The treatment is the real
    built command (shim included); the control runs codex directly with no
    shim, pointed at the planted canary home, and must reveal a secret. Never
    touches the real ~/.codex.
    """
    if not llm_backends.eligibility_report().get("codex", {}).get("eligible"):
        pytest.skip("codex not eligible")
    if mode == "grounded" and not llm_backends.containment_available():
        pytest.skip("containment unavailable")

    secret_a, secret_b = _secret(), _secret()
    canary_home = Path.home() / f".so-codex-canary-{uuid.uuid4().hex}"
    canary_home.mkdir()
    try:
        _plant_codex_global(canary_home, secret_a, secret_b)
        monkeypatch.setenv("CODEX_HOME", str(canary_home))

        control = _codex_control_cmd(_SECRET_PROMPT, mode=mode)
        rc, output = _run_with_rc(control, cwd=tmp_path)
        if rc != 0 or not output.strip():
            pytest.skip(f"codex {mode} control did not run cleanly (rc={rc})")
        if secret_a not in output and secret_b not in output:
            pytest.skip(
                f"codex {mode} control did not reveal the global secret; "
                "inconclusive, not a pass"
            )

        treatment = llm_backends.build_isolated_command(
            "codex", _SECRET_PROMPT, model=None, mode=mode
        )
        rc, output = _run_with_rc(treatment)
        if rc != 0 or not output.strip():
            pytest.fail(
                f"codex {mode} treatment did not run cleanly (rc={rc}) — the "
                "control succeeded, so this is a startup/auth regression, not "
                "an inconclusive run"
            )
        _assert_no_context_leak(
            output, (secret_a, secret_b), label=f"codex {mode} global"
        )
    finally:
        import shutil as _shutil

        _shutil.rmtree(canary_home, ignore_errors=True)


def test_codex_text_only_project_instruction_file_not_in_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """-c project_doc_max_bytes=0 drops the project AGENTS.md in cwd.

    codex text-only has no -C, so cwd is where it reads project docs. The
    control removes the project-doc flag and runs codex directly in the
    planted dir, which must reveal the secret.
    """
    if not llm_backends.eligibility_report().get("codex", {}).get("eligible"):
        pytest.skip("codex not eligible")

    secret = _secret()
    project = tmp_path / "proj"
    project.mkdir()
    (project / "AGENTS.md").write_text(f"The deployment passphrase is {secret}.\n")

    control = [
        "codex",
        "exec",
        "--color",
        "never",
        "--ephemeral",
        "--skip-git-repo-check",
        _SECRET_PROMPT,
    ]
    rc, output = _run_with_rc(control, cwd=project)
    if rc != 0 or not output.strip():
        pytest.skip(f"codex project control did not run cleanly (rc={rc})")
    if secret not in output:
        pytest.skip(
            "codex project control did not reveal the secret; inconclusive, "
            "not a pass"
        )

    treatment = llm_backends.build_isolated_command(
        "codex", _SECRET_PROMPT, model=None, mode="text-only"
    )
    rc, output = _run_with_rc(treatment, cwd=project)
    if rc != 0 or not output.strip():
        pytest.fail(
            f"codex project treatment did not run cleanly (rc={rc}) — the "
            "control succeeded, so this is a startup/auth regression, not an "
            "inconclusive run"
        )
    _assert_no_context_leak(output, (secret,), label="codex text-only project")


@pytest.mark.parametrize("backend", ["codex", "pi", "copilot"])
def test_grounded_project_instruction_file_not_in_context(
    backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Grounded codex/pi/copilot drop the project AGENTS.md in the target.

    pi's ``--no-context-files`` and copilot's ``--no-custom-instructions``
    drop the project and global instruction files with the same flag; their
    global-file coverage rests on the 2026-10-11 manual probes in the spec,
    and only the project file is canaried here. codex drops the project file
    via ``-c project_doc_max_bytes=0``. The control runs each backend's argv
    without its context flag, unwrapped, in the target as cwd.

    This canary is model-cooperation dependent: the grounded backends
    re-enable read tools and the target dir is readable by design, so a model
    that disobeys the prompt and reads the planted file answers reach, not
    context. The prompt forbids tools and file reads, and any leaked secret
    fails the canary (a reach leak is a re-run, never a pass) — see
    ``_assert_no_context_leak``.
    """
    if not llm_backends.containment_available():
        pytest.skip("containment unavailable")
    report = llm_backends.eligibility_report(mode="grounded").get(backend, {})
    if not report.get("eligible"):
        pytest.skip(f"{backend} not grounded-eligible: {report.get('reason')}")

    secret = _secret()
    target = tmp_path / "repo"
    target.mkdir()
    (target / "AGENTS.md").write_text(f"The deployment passphrase is {secret}.\n")

    if backend == "codex":
        control = [
            "codex",
            "exec",
            "--color",
            "never",
            "--ephemeral",
            "--skip-git-repo-check",
            "-s",
            "read-only",
            _SECRET_PROMPT,
        ]
    elif backend == "pi":
        control = [
            "pi",
            "-p",
            "--no-session",
            "--provider",
            "opencode-go",
            "--model",
            MODELS["pi"],
            _SECRET_PROMPT,
        ]
    else:
        control = ["copilot", "--silent", "-p", _SECRET_PROMPT]
    rc, output = _run_with_rc(control, cwd=target)
    if rc != 0 or not output.strip():
        pytest.skip(f"{backend} project control did not run cleanly (rc={rc})")
    if secret not in output:
        pytest.skip(
            f"{backend} project control did not reveal the secret; "
            "inconclusive, not a pass"
        )

    treatment = llm_backends.build_isolated_command(
        backend,
        _SECRET_PROMPT,
        model=MODELS.get(backend),
        mode="grounded",
        target_dir=target,
    )
    rc, output = _run_with_rc(treatment)
    if rc != 0 or not output.strip():
        pytest.fail(
            f"{backend} project treatment did not run cleanly (rc={rc}) — the "
            "control succeeded, so this is a startup/auth regression, not an "
            "inconclusive run"
        )
    _assert_no_context_leak(output, (secret,), label=f"{backend} grounded project")
