#!/usr/bin/env python3
"""Fast-tier tests for uninstall_state.py's package-transaction machinery (plan step 2).

No subprocesses, no real HOME — pure command builders, output parsers, and
the Transaction/interference/downgrade-ladder logic.

Requires Python 3.12+.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import uninstall_state  # noqa: E402 — must follow sys.path.insert above

# ── command builders ─────────────────────────────────────────────────────


def test_removal_commands_never_purge_or_autoremove():
    assert uninstall_state.apt_remove_command("eza") == [
        "sudo",
        "apt-get",
        "remove",
        "-y",
        "eza",
    ]
    assert uninstall_state.dnf_remove_command("eza") == ["sudo", "dnf", "remove", "-y", "eza"]
    for cmd in (uninstall_state.apt_remove_command("eza"), uninstall_state.dnf_remove_command("eza")):
        assert "purge" not in cmd
        assert "autoremove" not in cmd


def test_npm_and_uv_tool_removal_commands():
    assert uninstall_state.npm_uninstall_command("@github/copilot") == [
        "npm",
        "uninstall",
        "-g",
        "@github/copilot",
    ]
    assert uninstall_state.uv_tool_uninstall_command("ruff") == [
        "uv",
        "tool",
        "uninstall",
        "ruff",
    ]


@pytest.mark.regression(
    "apt-downgrade-command-missing-allow-downgrades",
    "At index 4 diff: 'eza=1.2.3-1' != '--allow-downgrades'",
)
def test_downgrade_commands():
    assert uninstall_state.apt_downgrade_command("eza", "1.2.3-1") == [
        "sudo",
        "apt-get",
        "install",
        "-y",
        "--allow-downgrades",
        "eza=1.2.3-1",
    ]
    assert uninstall_state.dnf_downgrade_command("eza", "1.2.3-1") == [
        "sudo",
        "dnf",
        "downgrade",
        "-y",
        "eza-1.2.3-1",
    ]


def test_rdepends_probe_commands():
    assert uninstall_state.apt_rdepends_command("eza") == [
        "apt-cache",
        "rdepends",
        "--installed",
        "eza",
    ]
    assert uninstall_state.dnf_whatrequires_command("eza") == [
        "dnf",
        "repoquery",
        "--whatrequires",
        "--installed",
        "eza",
    ]


# ── output parsers ────────────────────────────────────────────────────────


def test_parse_dpkg_query():
    output = "eza\t0.18.0-1\ntmux\t3.4-1\n"
    assert uninstall_state.parse_dpkg_query(output) == {"eza": "0.18.0-1", "tmux": "3.4-1"}


def test_parse_dpkg_query_ignores_blank_lines():
    assert uninstall_state.parse_dpkg_query("\n\n") == {}


def test_parse_rpm_qa_same_shape_as_dpkg():
    output = "neovim\t0.10.2-1.fc40\n"
    assert uninstall_state.parse_rpm_qa(output) == {"neovim": "0.10.2-1.fc40"}


def test_parse_npm_ls_global():
    output = """{
  "dependencies": {
    "@anthropic-ai/claude-code": {"version": "1.2.3"},
    "npm": {"version": "10.0.0"}
  }
}"""
    assert uninstall_state.parse_npm_ls_global(output) == {
        "@anthropic-ai/claude-code": "1.2.3",
        "npm": "10.0.0",
    }


def test_parse_npm_ls_global_unparseable_returns_empty():
    assert uninstall_state.parse_npm_ls_global("not json") == {}


def test_parse_npm_ls_global_missing_dependencies_key_returns_empty():
    assert uninstall_state.parse_npm_ls_global("{}") == {}


def test_parse_uv_tool_list():
    output = "ruff v0.5.0\n- ruff\nsomeother v1.0.0\n- someother\n"
    assert uninstall_state.parse_uv_tool_list(output) == {"ruff": "0.5.0", "someother": "1.0.0"}


def test_parse_uv_tool_list_empty():
    assert uninstall_state.parse_uv_tool_list("") == {}


# ── rdepends classification ──────────────────────────────────────────────


def test_parse_apt_rdepends_reads_the_header_apt_always_prints():
    """apt prints `<pkg>` + `Reverse Depends:` even with zero dependents."""
    assert uninstall_state.parse_apt_rdepends("sl\nReverse Depends:\n") == []
    assert uninstall_state.parse_apt_rdepends(
        "bash\nReverse Depends:\n  shfmt\n"
    ) == ["shfmt"]


def test_parse_apt_rdepends_counts_alternatives_and_versioned_rows():
    """One-space `| pkg` alternatives and version constraints are dependents."""
    assert uninstall_state.parse_apt_rdepends(
        "libgit2\nReverse Depends:\n |libgit2-dev\n  git (>= 1:2.0)\n"
    ) == ["|libgit2-dev", "git (>= 1:2.0)"]


def test_parse_apt_rdepends_rejects_unrecognized_output():
    """Only apt's documented shape may be read as an empty result."""
    for unrecognized in (
        "",
        "   \n",
        "Reverse Depends:\n",  # marker with no package heading
        "  other-pkg\n",  # dependent line before any marker
        "sl\nReverse Depends:\nother-pkg\n",  # dangling heading name
        "sl\nother-pkg\n",  # no marker at all
        "not a package name\nReverse Depends:\n",  # heading is not a name
        "sl\n\nReverse Depends:\n",  # blank line inside a heading block
    ):
        assert uninstall_state.parse_apt_rdepends(unrecognized) is None


def test_parse_apt_rdepends_accepts_a_second_heading_block():
    """Multi-architecture output repeats the heading block."""
    assert uninstall_state.parse_apt_rdepends(
        "libgit2:amd64\nReverse Depends:\n  git\nlibgit2:i386\nReverse Depends:\n"
    ) == ["git"]


def test_classify_rdepends_removable_on_success_empty_output():
    """dnf's probe prints bare names, so an empty result really is empty."""
    assert uninstall_state.classify_rdepends_result(True, "", "dnf") == "removable"
    assert uninstall_state.classify_rdepends_result(True, "  \n", "dnf") == "removable"


def test_classify_rdepends_in_use_on_a_named_dependent():
    assert (
        uninstall_state.classify_rdepends_result(
            True, "bash\nReverse Depends:\n  shfmt\n", "apt"
        )
        == "in-use"
    )
    assert uninstall_state.classify_rdepends_result(True, "other-pkg\n", "dnf") == (
        "in-use"
    )


def test_classify_rdepends_unknown_on_failure_even_if_output_empty():
    """A failed probe must never be treated as proof of removability."""
    # Recognized, header-only output — so it is the ``ok`` guard that makes
    # this unproven, not the shape.
    assert (
        uninstall_state.classify_rdepends_result(
            False, "sl\nReverse Depends:\n", "apt"
        )
        == "unknown"
    )
    assert uninstall_state.classify_rdepends_result(False, "", "dnf") == "unknown"


def test_classify_rdepends_unknown_for_an_unrecognized_or_unknown_manager():
    """Unprovable output is never removability, whichever manager asked."""
    assert uninstall_state.classify_rdepends_result(True, "", "apt") == "unknown"
    assert uninstall_state.classify_rdepends_result(True, "x\n", "pip") == "unknown"


# ── Transaction: introduced dependencies ─────────────────────────────────


def test_introduced_excludes_the_requested_package_itself():
    txn = uninstall_state.Transaction(
        manager="apt",
        requested=["eza"],
        before={"tmux": "3.4-1"},
        after={"tmux": "3.4-1", "eza": "0.18.0-1", "libgit2": "1.7-1"},
        comparand_source="epoch",
        interference=[],
        captured_at="t1",
    )
    assert txn.introduced() == ["libgit2"]


def test_introduced_empty_when_nothing_new_appeared():
    txn = uninstall_state.Transaction(
        manager="apt",
        requested=["eza"],
        before={"eza": "0.18.0-1"},
        after={"eza": "0.19.0-1"},
        comparand_source="epoch",
        interference=[],
        captured_at="t1",
    )
    assert txn.introduced() == []


def test_transaction_dict_roundtrip():
    txn = uninstall_state.Transaction(
        manager="uv-tool",
        requested=["ruff"],
        before={},
        after={"ruff": "0.5.0"},
        comparand_source="none",
        interference=[],
        captured_at="t1",
    )
    restored = uninstall_state.transaction_from_dict(uninstall_state.transaction_to_dict(txn))
    assert restored == txn


# ── record_transaction: comparand selection and interference ────────────


def test_first_transaction_uses_epoch_as_comparand():
    baseline = uninstall_state.Baseline()
    epoch = {"tmux": "3.4-1"}
    txn = uninstall_state.record_transaction(
        baseline,
        manager="apt",
        requested=["eza"],
        before={"tmux": "3.4-1"},
        after={"tmux": "3.4-1", "eza": "0.18.0-1"},
        captured_at="t1",
        epoch=epoch,
    )
    assert txn.comparand_source == "epoch"
    assert txn.interference == []  # before matches epoch exactly


def test_first_transaction_without_epoch_has_no_comparand():
    baseline = uninstall_state.Baseline()
    txn = uninstall_state.record_transaction(
        baseline,
        manager="apt",
        requested=["eza"],
        before={"tmux": "3.4-1"},
        after={"tmux": "3.4-1", "eza": "0.18.0-1"},
        captured_at="t1",
    )
    assert txn.comparand_source == "none"
    assert txn.interference == []


def test_subsequent_transaction_compares_against_previous_same_manager_after():
    baseline = uninstall_state.Baseline()
    uninstall_state.record_transaction(
        baseline,
        manager="apt",
        requested=["eza"],
        before={},
        after={"eza": "0.18.0-1"},
        captured_at="t1",
        epoch={},
    )
    # A second apt transaction's before matches transaction 1's after exactly.
    txn2 = uninstall_state.record_transaction(
        baseline,
        manager="apt",
        requested=["tmux"],
        before={"eza": "0.18.0-1"},
        after={"eza": "0.18.0-1", "tmux": "3.4-1"},
        captured_at="t2",
    )
    assert txn2.comparand_source == "previous"
    assert txn2.interference == []


def test_cross_manager_transactions_never_compared_to_each_other():
    """uv-tool's first transaction must not be compared against apt's history."""
    baseline = uninstall_state.Baseline()
    uninstall_state.record_transaction(
        baseline,
        manager="apt",
        requested=["eza"],
        before={},
        after={"eza": "0.18.0-1"},
        captured_at="t1",
        epoch={},
    )
    txn = uninstall_state.record_transaction(
        baseline,
        manager="uv-tool",
        requested=["ruff"],
        before={},
        after={"ruff": "0.5.0"},
        captured_at="t2",
        epoch={},
    )
    # If apt's history had leaked in as the comparand, "eza" would show up
    # as interference (present in apt's after, absent from uv-tool's before).
    assert txn.comparand_source == "epoch"
    assert txn.interference == []


def test_interference_detected_when_unrelated_package_changed_between_transactions():
    baseline = uninstall_state.Baseline()
    uninstall_state.record_transaction(
        baseline,
        manager="apt",
        requested=["eza"],
        before={"tmux": "3.4-1"},
        after={"tmux": "3.4-1", "eza": "0.18.0-1"},
        captured_at="t1",
        epoch={"tmux": "3.4-1"},
    )
    # Something else (e.g. unattended-upgrades) bumped tmux mid-run.
    txn2 = uninstall_state.record_transaction(
        baseline,
        manager="apt",
        requested=["ripgrep"],
        before={"tmux": "3.4-2", "eza": "0.18.0-1"},
        after={"tmux": "3.4-2", "eza": "0.18.0-1", "ripgrep": "14.0-1"},
        captured_at="t2",
    )
    assert txn2.interference == ["tmux"]
    # Interference is informational only — it doesn't change what "introduced" means.
    assert txn2.introduced() == []


def test_epoch_comparand_only_used_for_a_managers_very_first_transaction():
    """Even if `epoch` is passed again, an existing prior transaction wins."""
    baseline = uninstall_state.Baseline()
    uninstall_state.record_transaction(
        baseline,
        manager="apt",
        requested=["eza"],
        before={},
        after={"eza": "0.18.0-1"},
        captured_at="t1",
        epoch={},
    )
    txn2 = uninstall_state.record_transaction(
        baseline,
        manager="apt",
        requested=["tmux"],
        before={"eza": "0.18.0-1"},
        after={"eza": "0.18.0-1", "tmux": "3.4-1"},
        captured_at="t2",
        epoch={"wrong": "should-not-be-used"},
    )
    assert txn2.comparand_source == "previous"
    assert txn2.interference == []


def test_record_transaction_persists_into_baseline_transactions():
    baseline = uninstall_state.Baseline()
    uninstall_state.record_transaction(
        baseline,
        manager="apt",
        requested=["eza"],
        before={},
        after={"eza": "0.18.0-1"},
        captured_at="t1",
        epoch={},
    )
    assert len(baseline.transactions) == 1
    assert baseline.transactions[0]["manager"] == "apt"


# ── downgrade ladder ──────────────────────────────────────────────────────


def test_earliest_recorded_version_is_the_first_before_value():
    baseline = uninstall_state.Baseline()
    uninstall_state.record_transaction(
        baseline,
        manager="apt",
        requested=["neovim"],
        before={"neovim": "0.9.0-1"},
        after={"neovim": "0.10.0-1"},
        captured_at="t1",
        epoch={"neovim": "0.9.0-1"},
    )
    assert uninstall_state.earliest_recorded_version(baseline, "apt", "neovim") == "0.9.0-1"


def test_earliest_recorded_version_none_when_never_recorded():
    baseline = uninstall_state.Baseline()
    assert uninstall_state.earliest_recorded_version(baseline, "apt", "neovim") is None


def test_downgrade_candidates_earliest_first_then_intermediates_reverse_chronological():
    baseline = uninstall_state.Baseline()
    # neovim starts at 0.9.0, gets bumped across three separate runs/transactions.
    uninstall_state.record_transaction(
        baseline,
        manager="apt",
        requested=["neovim"],
        before={"neovim": "0.9.0-1"},
        after={"neovim": "0.10.0-1"},
        captured_at="t1",
        epoch={"neovim": "0.9.0-1"},
    )
    uninstall_state.record_transaction(
        baseline,
        manager="apt",
        requested=["neovim"],
        before={"neovim": "0.10.0-1"},
        after={"neovim": "0.11.0-1"},
        captured_at="t2",
    )
    uninstall_state.record_transaction(
        baseline,
        manager="apt",
        requested=["neovim"],
        before={"neovim": "0.11.0-1"},
        after={"neovim": "0.12.0-1"},
        captured_at="t3",
    )
    assert uninstall_state.downgrade_candidates(baseline, "apt", "neovim") == [
        "0.9.0-1",
        "0.12.0-1",
        "0.11.0-1",
        "0.10.0-1",
    ]


def test_downgrade_candidates_empty_when_never_recorded():
    baseline = uninstall_state.Baseline()
    assert uninstall_state.downgrade_candidates(baseline, "apt", "neovim") == []


def test_downgrade_candidates_deduplicates_repeated_versions():
    baseline = uninstall_state.Baseline()
    uninstall_state.record_transaction(
        baseline,
        manager="apt",
        requested=["neovim"],
        before={"neovim": "0.9.0-1"},
        after={"neovim": "0.9.0-1"},  # a no-op reinstall transaction
        captured_at="t1",
        epoch={"neovim": "0.9.0-1"},
    )
    assert uninstall_state.downgrade_candidates(baseline, "apt", "neovim") == ["0.9.0-1"]


# ── package classification ────────────────────────────────────────────────


def _baseline_with(*transactions):
    baseline = uninstall_state.Baseline()
    for kwargs in transactions:
        uninstall_state.record_transaction(baseline, **kwargs)
    return baseline


def test_classify_fresh_install_is_owned_remove():
    baseline = _baseline_with(
        {
            "manager": "apt",
            "requested": ["eza"],
            "before": {},
            "after": {"eza": "0.18.0-1"},
            "captured_at": "t1",
            "epoch": {},
        }
    )
    results = uninstall_state.classify_package_transactions(
        baseline, {"apt": {"eza": "0.18.0-1"}}
    )
    assert len(results) == 1
    c = results[0]
    assert c.bucket == "owned"
    assert c.action == "remove"
    assert c.name == "eza"


def test_classify_upgraded_preexisting_package_is_owned_downgrade():
    baseline = _baseline_with(
        {
            "manager": "apt",
            "requested": ["neovim"],
            "before": {"neovim": "0.9.0-1"},
            "after": {"neovim": "0.10.0-1"},
            "captured_at": "t1",
            "epoch": {"neovim": "0.9.0-1"},
        }
    )
    results = uninstall_state.classify_package_transactions(
        baseline, {"apt": {"neovim": "0.10.0-1"}}
    )
    assert len(results) == 1
    assert results[0].bucket == "owned"
    assert results[0].action == "downgrade"


def test_classify_already_installed_unchanged_is_preserved_not_downgrade():
    """apt reporting '0 upgraded' (already the newest version) must not be
    treated as something to downgrade — nothing this transaction did."""
    baseline = _baseline_with(
        {
            "manager": "apt",
            "requested": ["tmux"],
            "before": {"tmux": "3.4-1"},
            "after": {"tmux": "3.4-1"},
            "captured_at": "t1",
            "epoch": {"tmux": "3.4-1"},
        }
    )
    results = uninstall_state.classify_package_transactions(baseline, {"apt": {"tmux": "3.4-1"}})
    assert len(results) == 1
    assert results[0].bucket == "preserved"
    assert results[0].action is None


def test_classify_introduced_dependency_is_owned_remove():
    baseline = _baseline_with(
        {
            "manager": "apt",
            "requested": ["eza"],
            "before": {},
            "after": {"eza": "0.18.0-1", "libgit2": "1.7-1"},
            "captured_at": "t1",
            "epoch": {},
        }
    )
    results = uninstall_state.classify_package_transactions(
        baseline, {"apt": {"eza": "0.18.0-1", "libgit2": "1.7-1"}}
    )
    names = {c.name: c for c in results}
    assert names["libgit2"].bucket == "owned"
    assert names["libgit2"].action == "remove"
    assert names["libgit2"].reason == "introduced as a dependency by this transaction"


def test_classify_already_removed_package_is_preserved():
    baseline = _baseline_with(
        {
            "manager": "apt",
            "requested": ["eza"],
            "before": {},
            "after": {"eza": "0.18.0-1"},
            "captured_at": "t1",
            "epoch": {},
        }
    )
    results = uninstall_state.classify_package_transactions(baseline, {"apt": {}})
    assert results[0].bucket == "preserved"


def test_classify_failed_probe_is_unresolved():
    baseline = _baseline_with(
        {
            "manager": "apt",
            "requested": ["eza"],
            "before": {},
            "after": {"eza": "0.18.0-1"},
            "captured_at": "t1",
            "epoch": {},
        }
    )
    results = uninstall_state.classify_package_transactions(baseline, {"apt": None})
    assert results[0].bucket == "unresolved"


def test_classify_walks_transactions_in_reverse_order():
    baseline = _baseline_with(
        {
            "manager": "apt",
            "requested": ["eza"],
            "before": {},
            "after": {"eza": "0.18.0-1"},
            "captured_at": "t1",
            "epoch": {},
        },
        {
            "manager": "apt",
            "requested": ["tmux"],
            "before": {"eza": "0.18.0-1"},
            "after": {"eza": "0.18.0-1", "tmux": "3.4-1"},
            "captured_at": "t2",
        },
    )
    results = uninstall_state.classify_package_transactions(
        baseline, {"apt": {"eza": "0.18.0-1", "tmux": "3.4-1"}}
    )
    assert [c.name for c in results] == ["tmux", "eza"]


def test_classify_deduplicates_a_package_touched_by_multiple_transactions():
    """One verdict per package, from its whole recorded history."""
    baseline = _baseline_with(
        {
            "manager": "apt",
            "requested": ["neovim"],
            "before": {"neovim": "0.9.0-1"},
            "after": {"neovim": "0.10.0-1"},
            "captured_at": "t1",
            "epoch": {"neovim": "0.9.0-1"},
        },
        {
            "manager": "apt",
            "requested": ["neovim"],
            "before": {"neovim": "0.10.0-1"},
            "after": {"neovim": "0.11.0-1"},
            "captured_at": "t2",
        },
    )
    results = uninstall_state.classify_package_transactions(
        baseline, {"apt": {"neovim": "0.11.0-1"}}
    )
    assert len(results) == 1
    # The package pre-existed the installer's first transaction (0.9.0-1 in
    # its `before`), so it can never be removed — and a later transaction
    # changed its version, so it is restored, not left alone.
    assert results[0].action == "downgrade"


@pytest.mark.regression(
    "repeat-install-keeps-package-ownership",
    "AssertionError: assert 'preserved' == 'owned'",
)
def test_classify_keeps_ownership_through_a_later_noop_reinstall_transaction():
    """A second install run records a no-op transaction; ownership must survive."""
    baseline = _baseline_with(
        {
            "manager": "apt",
            "requested": ["eza"],
            "before": {},
            "after": {"eza": "0.18.0-1"},
            "captured_at": "t1",
            "epoch": {},
        },
        {
            "manager": "apt",
            "requested": ["eza"],
            "before": {"eza": "0.18.0-1"},
            "after": {"eza": "0.18.0-1"},
            "captured_at": "t2",
        },
    )
    results = uninstall_state.classify_package_transactions(
        baseline, {"apt": {"eza": "0.18.0-1"}}
    )
    assert len(results) == 1
    assert results[0].bucket == "owned"
    assert results[0].action == "remove"


def test_classify_removes_a_fresh_install_that_a_later_run_upgraded():
    """No pre-install version exists, so restoring one is never an option."""
    baseline = _baseline_with(
        {
            "manager": "apt",
            "requested": ["eza"],
            "before": {},
            "after": {"eza": "0.18.0-1"},
            "captured_at": "t1",
            "epoch": {},
        },
        {
            "manager": "apt",
            "requested": ["eza"],
            "before": {"eza": "0.18.0-1"},
            "after": {"eza": "0.19.0-1"},
            "captured_at": "t2",
        },
    )
    results = uninstall_state.classify_package_transactions(
        baseline, {"apt": {"eza": "0.19.0-1"}}
    )
    assert results[0].bucket == "owned"
    assert results[0].action == "remove"


def test_classify_keeps_the_dependency_probe_for_a_later_explicit_request():
    """A no-op re-request must not turn a dependency into an unprobed removal."""
    baseline = _baseline_with(
        {
            "manager": "apt",
            "requested": ["eza"],
            "before": {},
            "after": {"eza": "0.18.0-1", "libgit2": "1.7-1"},
            "captured_at": "t1",
            "epoch": {},
        },
        {
            "manager": "apt",
            "requested": ["libgit2"],
            "before": {"eza": "0.18.0-1", "libgit2": "1.7-1"},
            "after": {"eza": "0.18.0-1", "libgit2": "1.7-1"},
            "captured_at": "t2",
        },
    )
    results = uninstall_state.classify_package_transactions(
        baseline, {"apt": {"eza": "0.18.0-1", "libgit2": "1.7-1"}}
    )
    by_name = {c.name: c for c in results}
    assert by_name["libgit2"].bucket == "owned"
    assert by_name["libgit2"].action == "remove"
    # Which removal path that action takes (probe-gated vs direct) is asserted
    # behaviourally in test_install.py's package-phase tests.


def test_classify_does_not_treat_an_unrelated_snapshot_package_as_a_dependency():
    """Whole-manager snapshots list every package; only ``requested`` decides."""
    baseline = _baseline_with(
        {
            "manager": "apt",
            "requested": ["eza"],
            "before": {"tmux": "3.4-1"},
            "after": {"tmux": "3.4-1", "eza": "0.18.0-1"},
            "captured_at": "t1",
            "epoch": {"tmux": "3.4-1"},
        },
        {
            "manager": "apt",
            "requested": ["tmux"],
            "before": {"tmux": "3.4-1", "eza": "0.18.0-1"},
            "after": {"tmux": "3.4-1", "eza": "0.18.0-1"},
            "captured_at": "t2",
        },
    )
    results = uninstall_state.classify_package_transactions(
        baseline, {"apt": {"tmux": "3.4-1", "eza": "0.18.0-1"}}
    )
    by_name = {c.name: c for c in results}
    assert by_name["tmux"].bucket == "preserved"
    assert by_name["tmux"].action is None
    assert by_name["eza"].bucket == "owned"
    assert by_name["eza"].action == "remove"


def test_classify_does_not_claim_a_package_no_transaction_recorded():
    """Nothing in the history proves the installer put it there."""
    baseline = _baseline_with(
        {
            "manager": "apt",
            "requested": ["eza"],
            "before": {},
            "after": {},
            "captured_at": "t1",
            "epoch": {},
        }
    )
    results = uninstall_state.classify_package_transactions(
        baseline, {"apt": {"eza": "0.18.0-1"}}
    )
    assert results[0].bucket == "preserved"
    assert results[0].action is None


def test_classify_pre_existing_package_upgraded_after_a_noop_is_downgraded():
    """A no-op first transaction must not hide a later installer upgrade."""
    baseline = _baseline_with(
        {
            "manager": "apt",
            "requested": ["neovim"],
            "before": {"neovim": "0.9.0-1"},
            "after": {"neovim": "0.9.0-1"},
            "captured_at": "t1",
            "epoch": {"neovim": "0.9.0-1"},
        },
        {
            "manager": "apt",
            "requested": ["neovim"],
            "before": {"neovim": "0.9.0-1"},
            "after": {"neovim": "0.10.0-1"},
            "captured_at": "t2",
        },
        {
            "manager": "apt",
            "requested": ["neovim"],
            "before": {"neovim": "0.10.0-1"},
            "after": {"neovim": "0.10.0-1"},
            "captured_at": "t3",
        },
    )
    results = uninstall_state.classify_package_transactions(
        baseline, {"apt": {"neovim": "0.10.0-1"}}
    )
    assert results[0].bucket == "owned"
    assert results[0].action == "downgrade"


def test_classify_does_not_claim_a_package_a_transaction_never_recorded():
    """A requested-but-unrecorded package proves nothing; the user installed it."""
    baseline = _baseline_with(
        {
            "manager": "apt",
            "requested": ["eza"],
            "before": {},
            "after": {},
            "captured_at": "t1",
            "epoch": {},
        },
        {
            "manager": "apt",
            "requested": ["eza"],
            "before": {"eza": "0.18.0-1"},
            "after": {"eza": "0.18.0-1"},
            "captured_at": "t2",
        },
    )
    results = uninstall_state.classify_package_transactions(
        baseline, {"apt": {"eza": "0.18.0-1"}}
    )
    assert results[0].bucket == "preserved"
    assert results[0].action is None
