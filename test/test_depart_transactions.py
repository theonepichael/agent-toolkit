#!/usr/bin/env python3
"""Fast-tier tests for depart.py's package-transaction machinery (plan step 2).

No subprocesses, no real HOME — pure command builders, output parsers, and
the Transaction/interference/downgrade-ladder logic.

Requires Python 3.12+.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import depart  # noqa: E402 — must follow sys.path.insert above

# ── command builders ─────────────────────────────────────────────────────


def test_removal_commands_never_purge_or_autoremove():
    assert depart.apt_remove_command("eza") == [
        "sudo",
        "apt-get",
        "remove",
        "-y",
        "eza",
    ]
    assert depart.dnf_remove_command("eza") == ["sudo", "dnf", "remove", "-y", "eza"]
    for cmd in (depart.apt_remove_command("eza"), depart.dnf_remove_command("eza")):
        assert "purge" not in cmd
        assert "autoremove" not in cmd


def test_npm_and_uv_tool_removal_commands():
    assert depart.npm_uninstall_command("@github/copilot") == [
        "npm",
        "uninstall",
        "-g",
        "@github/copilot",
    ]
    assert depart.uv_tool_uninstall_command("ruff") == [
        "uv",
        "tool",
        "uninstall",
        "ruff",
    ]


def test_downgrade_commands():
    assert depart.apt_downgrade_command("eza", "1.2.3-1") == [
        "sudo",
        "apt-get",
        "install",
        "-y",
        "eza=1.2.3-1",
    ]
    assert depart.dnf_downgrade_command("eza", "1.2.3-1") == [
        "sudo",
        "dnf",
        "downgrade",
        "-y",
        "eza-1.2.3-1",
    ]


def test_rdepends_probe_commands():
    assert depart.apt_rdepends_command("eza") == [
        "apt-cache",
        "rdepends",
        "--installed",
        "eza",
    ]
    assert depart.dnf_whatrequires_command("eza") == [
        "dnf",
        "repoquery",
        "--whatrequires",
        "--installed",
        "eza",
    ]


# ── output parsers ────────────────────────────────────────────────────────


def test_parse_dpkg_query():
    output = "eza\t0.18.0-1\ntmux\t3.4-1\n"
    assert depart.parse_dpkg_query(output) == {"eza": "0.18.0-1", "tmux": "3.4-1"}


def test_parse_dpkg_query_ignores_blank_lines():
    assert depart.parse_dpkg_query("\n\n") == {}


def test_parse_rpm_qa_same_shape_as_dpkg():
    output = "neovim\t0.10.2-1.fc40\n"
    assert depart.parse_rpm_qa(output) == {"neovim": "0.10.2-1.fc40"}


def test_parse_npm_ls_global():
    output = """{
  "dependencies": {
    "@anthropic-ai/claude-code": {"version": "1.2.3"},
    "npm": {"version": "10.0.0"}
  }
}"""
    assert depart.parse_npm_ls_global(output) == {
        "@anthropic-ai/claude-code": "1.2.3",
        "npm": "10.0.0",
    }


def test_parse_npm_ls_global_unparseable_returns_empty():
    assert depart.parse_npm_ls_global("not json") == {}


def test_parse_npm_ls_global_missing_dependencies_key_returns_empty():
    assert depart.parse_npm_ls_global("{}") == {}


def test_parse_uv_tool_list():
    output = "ruff v0.5.0\n- ruff\nsomeother v1.0.0\n- someother\n"
    assert depart.parse_uv_tool_list(output) == {"ruff": "0.5.0", "someother": "1.0.0"}


def test_parse_uv_tool_list_empty():
    assert depart.parse_uv_tool_list("") == {}


# ── rdepends classification ──────────────────────────────────────────────


def test_classify_rdepends_removable_on_success_empty_output():
    assert depart.classify_rdepends_result(True, "") == "removable"
    assert depart.classify_rdepends_result(True, "   \n") == "removable"


def test_classify_rdepends_in_use_on_nonempty_output():
    assert (
        depart.classify_rdepends_result(True, "Reverse Depends:\n  other-pkg")
        == "in-use"
    )


def test_classify_rdepends_unknown_on_failure_even_if_output_empty():
    """A failed probe must never be treated as proof of removability."""
    assert depart.classify_rdepends_result(False, "") == "unknown"


# ── Transaction: introduced dependencies ─────────────────────────────────


def test_introduced_excludes_the_requested_package_itself():
    txn = depart.Transaction(
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
    txn = depart.Transaction(
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
    txn = depart.Transaction(
        manager="uv-tool",
        requested=["ruff"],
        before={},
        after={"ruff": "0.5.0"},
        comparand_source="none",
        interference=[],
        captured_at="t1",
    )
    restored = depart.transaction_from_dict(depart.transaction_to_dict(txn))
    assert restored == txn


# ── record_transaction: comparand selection and interference ────────────


def test_first_transaction_uses_epoch_as_comparand():
    baseline = depart.Baseline()
    epoch = {"tmux": "3.4-1"}
    txn = depart.record_transaction(
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
    baseline = depart.Baseline()
    txn = depart.record_transaction(
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
    baseline = depart.Baseline()
    depart.record_transaction(
        baseline,
        manager="apt",
        requested=["eza"],
        before={},
        after={"eza": "0.18.0-1"},
        captured_at="t1",
        epoch={},
    )
    # A second apt transaction's before matches transaction 1's after exactly.
    txn2 = depart.record_transaction(
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
    baseline = depart.Baseline()
    depart.record_transaction(
        baseline,
        manager="apt",
        requested=["eza"],
        before={},
        after={"eza": "0.18.0-1"},
        captured_at="t1",
        epoch={},
    )
    txn = depart.record_transaction(
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
    baseline = depart.Baseline()
    depart.record_transaction(
        baseline,
        manager="apt",
        requested=["eza"],
        before={"tmux": "3.4-1"},
        after={"tmux": "3.4-1", "eza": "0.18.0-1"},
        captured_at="t1",
        epoch={"tmux": "3.4-1"},
    )
    # Something else (e.g. unattended-upgrades) bumped tmux mid-run.
    txn2 = depart.record_transaction(
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
    baseline = depart.Baseline()
    depart.record_transaction(
        baseline,
        manager="apt",
        requested=["eza"],
        before={},
        after={"eza": "0.18.0-1"},
        captured_at="t1",
        epoch={},
    )
    txn2 = depart.record_transaction(
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
    baseline = depart.Baseline()
    depart.record_transaction(
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
    baseline = depart.Baseline()
    depart.record_transaction(
        baseline,
        manager="apt",
        requested=["neovim"],
        before={"neovim": "0.9.0-1"},
        after={"neovim": "0.10.0-1"},
        captured_at="t1",
        epoch={"neovim": "0.9.0-1"},
    )
    assert depart.earliest_recorded_version(baseline, "apt", "neovim") == "0.9.0-1"


def test_earliest_recorded_version_none_when_never_recorded():
    baseline = depart.Baseline()
    assert depart.earliest_recorded_version(baseline, "apt", "neovim") is None


def test_downgrade_candidates_earliest_first_then_intermediates_reverse_chronological():
    baseline = depart.Baseline()
    # neovim starts at 0.9.0, gets bumped across three separate runs/transactions.
    depart.record_transaction(
        baseline,
        manager="apt",
        requested=["neovim"],
        before={"neovim": "0.9.0-1"},
        after={"neovim": "0.10.0-1"},
        captured_at="t1",
        epoch={"neovim": "0.9.0-1"},
    )
    depart.record_transaction(
        baseline,
        manager="apt",
        requested=["neovim"],
        before={"neovim": "0.10.0-1"},
        after={"neovim": "0.11.0-1"},
        captured_at="t2",
    )
    depart.record_transaction(
        baseline,
        manager="apt",
        requested=["neovim"],
        before={"neovim": "0.11.0-1"},
        after={"neovim": "0.12.0-1"},
        captured_at="t3",
    )
    assert depart.downgrade_candidates(baseline, "apt", "neovim") == [
        "0.9.0-1",
        "0.12.0-1",
        "0.11.0-1",
        "0.10.0-1",
    ]


def test_downgrade_candidates_empty_when_never_recorded():
    baseline = depart.Baseline()
    assert depart.downgrade_candidates(baseline, "apt", "neovim") == []


def test_downgrade_candidates_deduplicates_repeated_versions():
    baseline = depart.Baseline()
    depart.record_transaction(
        baseline,
        manager="apt",
        requested=["neovim"],
        before={"neovim": "0.9.0-1"},
        after={"neovim": "0.9.0-1"},  # a no-op reinstall transaction
        captured_at="t1",
        epoch={"neovim": "0.9.0-1"},
    )
    assert depart.downgrade_candidates(baseline, "apt", "neovim") == ["0.9.0-1"]


# ── package classification ────────────────────────────────────────────────


def _baseline_with(*transactions):
    baseline = depart.Baseline()
    for kwargs in transactions:
        depart.record_transaction(baseline, **kwargs)
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
    results = depart.classify_package_transactions(
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
    results = depart.classify_package_transactions(
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
    results = depart.classify_package_transactions(baseline, {"apt": {"tmux": "3.4-1"}})
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
    results = depart.classify_package_transactions(
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
    results = depart.classify_package_transactions(baseline, {"apt": {}})
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
    results = depart.classify_package_transactions(baseline, {"apt": None})
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
    results = depart.classify_package_transactions(
        baseline, {"apt": {"eza": "0.18.0-1", "tmux": "3.4-1"}}
    )
    assert [c.name for c in results] == ["tmux", "eza"]


def test_classify_deduplicates_a_package_touched_by_multiple_transactions():
    """Classified from its most-recently-touched transaction only."""
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
    results = depart.classify_package_transactions(
        baseline, {"apt": {"neovim": "0.11.0-1"}}
    )
    assert len(results) == 1
    # Most recent transaction's `before` (0.10.0-1) was already installed,
    # so this is still classified as an upgrade — downgrade, not remove.
    assert results[0].action == "downgrade"
