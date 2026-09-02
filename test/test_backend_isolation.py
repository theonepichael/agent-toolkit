"""Layer 1 of the backend isolation guard: offline, runs on every suite pass.

The contract every backend invocation must meet is defined in
``llm_backends.ISOLATION_CLAUSES``. This module proves the *structure* holds:
the command builder refuses to emit a command for a backend whose capability
descriptor does not cover every clause, so a backend added without a complete
descriptor cannot be invoked at all.

What this file deliberately does NOT prove: that a declared mechanism actually
works. A descriptor claiming ``"tools_execution": ["--no-tools"]`` is taken at its word
here. If a vendor renames or repurposes that flag, the descriptor still looks
complete and these tests still pass -- which is exactly the marker-check
weakness that let the ``--model-index`` behaviour change ship unnoticed. Proving
the mechanism is Layer 2's job (``test_backend_isolation_live.py``), which runs
real canaries against the real binaries behind an opt-in marker.

Measured evidence behind the shipped descriptors is recorded in
``~/.claude/data/grill/2026-08-31-meta-second-opinion-backend-isol-plan.md``.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "claude" / "scripts"))

import llm_backends  # noqa: E402

# ── the contract itself ───────────────────────────────────────────────────


def test_contract_clauses_are_the_agreed_set() -> None:
    """The clause list is the contract. Changing it is a deliberate act, not a
    refactor, so it is pinned here rather than derived from the descriptors."""
    assert llm_backends.ISOLATION_CLAUSES == (
        "tools_execution",
        "tools_reach",
        "context",
        "templates",
        "skills",
        "mcp",
        "session",
    )


def test_every_shipped_backend_has_a_complete_descriptor() -> None:
    incomplete = {
        name: sorted(set(llm_backends.ISOLATION_CLAUSES) - set(spec))
        for name, spec in llm_backends.BACKEND_ISOLATION.items()
        if set(llm_backends.ISOLATION_CLAUSES) - set(spec)
    }
    assert not incomplete, f"backends with uncovered clauses: {incomplete}"


def test_every_backend_priority_entry_has_a_descriptor() -> None:
    """A backend cannot be reachable through resolution without a descriptor."""
    missing = [
        b
        for b in llm_backends.BACKEND_PRIORITY
        if b not in llm_backends.BACKEND_ISOLATION
    ]
    assert not missing, f"in BACKEND_PRIORITY but undeclared: {missing}"


# ── the builder fails closed ──────────────────────────────────────────────


@pytest.mark.parametrize("dropped", llm_backends.ISOLATION_CLAUSES)
def test_builder_refuses_a_descriptor_missing_any_single_clause(
    dropped: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One parametrised case per clause: dropping any one of them must raise.

    This is the whole point of the descriptor design -- coverage lives in data,
    so an incomplete backend is unbuildable rather than silently unisolated.
    """
    spec = {c: ["--flag"] for c in llm_backends.ISOLATION_CLAUSES if c != dropped}
    monkeypatch.setitem(llm_backends.BACKEND_ISOLATION, "fake", spec)

    with pytest.raises(llm_backends.IsolationError) as excinfo:
        llm_backends.build_isolated_command("fake", "prompt", model=None)

    message = str(excinfo.value)
    assert "fake" in message
    assert dropped in message, "the error must name the unmet clause, not just fail"


def test_builder_refuses_an_unknown_backend() -> None:
    with pytest.raises(llm_backends.IsolationError) as excinfo:
        llm_backends.build_isolated_command("no-such-backend", "prompt", model=None)
    assert "no-such-backend" in str(excinfo.value)


def test_builder_emits_every_declared_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = {c: [f"--{c}"] for c in llm_backends.ISOLATION_CLAUSES}
    spec["_base"] = ["fakebin", "-p"]
    monkeypatch.setitem(llm_backends.BACKEND_ISOLATION, "fake", spec)

    cmd = llm_backends.build_isolated_command("fake", "the prompt", model=None)

    for clause in llm_backends.ISOLATION_CLAUSES:
        assert f"--{clause}" in cmd, f"clause {clause} flag missing from command"
    assert cmd[-1] == "the prompt"


def test_builder_deduplicates_a_flag_covering_several_clauses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pi's --no-tools covers tools_execution and skills; it must appear once."""
    spec = {c: ["--all-in-one"] for c in llm_backends.ISOLATION_CLAUSES}
    spec["_base"] = ["fakebin"]
    monkeypatch.setitem(llm_backends.BACKEND_ISOLATION, "fake", spec)

    cmd = llm_backends.build_isolated_command("fake", "p", model=None)

    assert cmd.count("--all-in-one") == 1


# ── OS containment is never assumed ───────────────────────────────────────


def test_containment_backend_refuses_when_platform_cannot_contain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A containment-dependent backend on a host without working namespaces must
    refuse. A containment path that silently no-ops is worse than none: the call
    believes it is contained and is not."""
    spec = {c: llm_backends.OS_CONTAINED for c in llm_backends.ISOLATION_CLAUSES}
    spec["_base"] = ["fakebin"]
    monkeypatch.setitem(llm_backends.BACKEND_ISOLATION, "fake", spec)
    monkeypatch.setattr(llm_backends, "containment_available", lambda: False)

    with pytest.raises(llm_backends.IsolationError) as excinfo:
        llm_backends.build_isolated_command("fake", "p", model=None)
    assert "contain" in str(excinfo.value).lower()


def test_containment_backend_builds_a_wrapped_command_when_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = {c: llm_backends.OS_CONTAINED for c in llm_backends.ISOLATION_CLAUSES}
    spec["_base"] = ["fakebin"]
    monkeypatch.setitem(llm_backends.BACKEND_ISOLATION, "fake", spec)
    monkeypatch.setattr(llm_backends, "containment_available", lambda: True)

    cmd = llm_backends.build_isolated_command("fake", "p", model=None)

    assert cmd[0] == "unshare", "a contained backend must run under the wrapper"


def test_agy_is_declared_containment_dependent() -> None:
    """Measured 2026-08-31: agy defeats --sandbox, a permissions deny-all, and a
    top-level disabledTools list -- arbitrary file read survives all three, and
    it exposes no tool-disable flag. Containment is its only qualifying path."""
    agy = llm_backends.BACKEND_ISOLATION["agy"]
    assert agy["tools_execution"] is llm_backends.OS_CONTAINED
    assert agy["tools_reach"] is llm_backends.OS_CONTAINED
    assert agy["context"] is llm_backends.OS_CONTAINED


def test_opencode_context_is_containment_dependent() -> None:
    """opencode denies tools via the adversary agent, but reads
    ~/.claude/CLAUDE.md globally with no flag to stop it (bisected 2026-08-31)."""
    assert (
        llm_backends.BACKEND_ISOLATION["opencode"]["context"]
        is llm_backends.OS_CONTAINED
    )


# ── eligibility reporting ─────────────────────────────────────────────────


def test_eligibility_reports_reason_for_each_ineligible_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(llm_backends, "containment_available", lambda: False)

    report = llm_backends.eligibility_report()

    for name, entry in report.items():
        assert "eligible" in entry, name
        if not entry["eligible"]:
            assert entry.get("reason"), f"{name} ineligible with no reason given"


def test_flagged_backends_declare_reach_moot_not_contained() -> None:
    """pi and copilot remove the tools outright, so there is no reach left to
    constrain. That must read as NOT_APPLICABLE, never as OS_CONTAINED --
    otherwise the builder would wrap them in a sandbox they do not need, and
    the descriptor would misdescribe why they are safe."""
    for name in ("pi", "copilot"):
        spec = llm_backends.BACKEND_ISOLATION[name]
        assert spec["tools_reach"] is llm_backends.NOT_APPLICABLE, name
        assert spec["tools_execution"] is not llm_backends.OS_CONTAINED, name


def test_not_applicable_does_not_trigger_containment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A NOT_APPLICABLE clause must not be mistaken for a containment
    requirement: a backend that needs no sandbox must build without one even on
    a host that cannot provide it."""
    spec = {c: llm_backends.NOT_APPLICABLE for c in llm_backends.ISOLATION_CLAUSES}
    spec["_base"] = ["fakebin"]
    monkeypatch.setitem(llm_backends.BACKEND_ISOLATION, "fake", spec)
    monkeypatch.setattr(llm_backends, "containment_available", lambda: False)

    cmd = llm_backends.build_isolated_command("fake", "p", model=None)
    assert cmd[0] == "fakebin"


# ── liveness is not capability ────────────────────────────────────────────


def test_fallback_moves_to_the_next_eligible_backend_on_a_hang(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stall must not take the feature down. Exercised with a stubbed hanging
    backend rather than a real one: the opencode-go gateway stalls
    intermittently (~20-33%), so a live test of this would be flaky in exactly
    the way the fallback exists to absorb."""
    monkeypatch.setattr(llm_backends, "eligible_backends", lambda: ["alpha", "beta"])
    tried: list[str] = []

    def runner(backend: str) -> str:
        tried.append(backend)
        if backend == "alpha":
            raise llm_backends.BackendError("timed out after 120s — killed")
        return "critique text"

    backend, out = llm_backends.run_with_fallback(runner)

    assert tried == ["alpha", "beta"], "must try the next one, in priority order"
    assert (backend, out) == ("beta", "critique text")


def test_fallback_never_targets_an_ineligible_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of separating the two failures: falling back must never
    reach a backend that failed the capability filter."""
    monkeypatch.setattr(llm_backends, "eligible_backends", lambda: ["alpha"])

    def runner(backend: str) -> str:
        raise llm_backends.BackendError("stalled")

    with pytest.raises(llm_backends.BackendError) as excinfo:
        llm_backends.run_with_fallback(runner)
    assert "all eligible backends failed" in str(excinfo.value)
    assert "alpha: stalled" in str(excinfo.value)


def test_no_eligible_backend_is_an_isolation_error_not_a_backend_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing eligible is a capability failure and must be reported as one --
    conflating it with 'everything was tried and failed' would hide that the
    cause is an unmet contract, not a flaky gateway."""
    monkeypatch.setattr(llm_backends, "eligible_backends", lambda: [])
    # eligibility_report() is consulted to build the reason string, and it
    # probes containment with a real subprocess the conftest guard blocks.
    monkeypatch.setattr(llm_backends, "containment_available", lambda: False)

    with pytest.raises(llm_backends.IsolationError):
        llm_backends.run_with_fallback(lambda b: "unreachable")
