#!/usr/bin/env python3
"""Provenance-root allowlist behaviour of guard_rails.py against REAL git
repositories.

A repo may declare gitignored provenance roots (``.guard-rails.json``); the
guard permits a write into a *busy* main checkout only when the target lies
under one of those roots and git actually ignores it. Everything else — a
tracked path, an undeclared ignored path, a ``..`` or symlink escape — stays
refused. Like test_guard_rails_topology.py, these deliberately do not mock
git: the rule turns on how git reports ``check-ignore`` and walks up to a
repo, so mocked output would encode the very assumptions under test.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "agent-scripts"))

import guard_rails  # noqa: E402

pytestmark = pytest.mark.allow_real_subprocess


class _FakeLookup:
    """Minimal BacklogClaimLookup fake: items in, no claims (a None claim is
    exactly what the unclaimed-item deny path needs)."""

    def __init__(self, items: list[dict] | None = None) -> None:
        self._items = items or []

    def in_progress_items(self) -> list[dict]:
        return list(self._items)

    def ready_items(self, prefix: str | None = None) -> list[dict]:
        return []

    def claim_info(self, slug: str) -> None:
        return None


def _fake_lookup(items: list[dict] | None = None) -> _FakeLookup:
    return _FakeLookup(items)


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def _init_repo(path: Path, branch: str = "main") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git("init", "-q", "-b", branch, ".", cwd=path)
    _git("config", "user.email", "t@example.invalid", cwd=path)
    _git("config", "user.name", "t", cwd=path)
    _git("config", "gc.auto", "0", cwd=path)
    _git("config", "maintenance.auto", "false", cwd=path)
    (path / "f.txt").write_text("a\n")
    _git("add", "f.txt", cwd=path)
    _git("commit", "-q", "-m", "init", cwd=path)
    return path


def _write_marker(repo: Path, roots: object) -> None:
    (repo / ".guard-rails.json").write_text(
        json.dumps({"provenance_roots": roots}), encoding="utf-8"
    )


def _gitignore(repo: Path, pattern: str) -> None:
    with (repo / ".gitignore").open("a", encoding="utf-8") as fh:
        fh.write(f"{pattern}\n")


def _busy_item(main_checkout: Path) -> list[dict]:
    return [
        {
            "id": "demo-slug",
            "status": "in-progress",
            "related_files": [{"path": str(main_checkout / "f.txt")}],
        }
    ]


def _evaluate_write(main_checkout: Path, target: Path, items: list[dict]):
    return guard_rails.evaluate(
        guard_rails.Request("write", str(main_checkout), str(target)),
        _fake_lookup(items),
    )


@pytest.fixture
def main_checkout(tmp_path: Path) -> Path:
    return _init_repo(tmp_path / "proj")


def test_declared_ignored_provenance_write_is_allowed(main_checkout: Path) -> None:
    """The regression: a busy main checkout must allow a write under a
    declared, gitignored provenance root."""
    _gitignore(main_checkout, "reports/local/")
    _write_marker(main_checkout, ["reports/local/"])
    verdict = _evaluate_write(
        main_checkout,
        main_checkout / "reports/local/x.py",
        _busy_item(main_checkout),
    )
    assert verdict.decision == "allow", verdict.reason


def test_tracked_file_under_declared_root_is_refused(main_checkout: Path) -> None:
    """A tracked file under the declared root is not ignored, so it stays
    refused even though it sits under a provenance root."""
    _gitignore(main_checkout, "reports/local/")
    _write_marker(main_checkout, ["reports/local/"])
    (main_checkout / "reports/local").mkdir(parents=True)
    (main_checkout / "reports/local/tracked.txt").write_text("tracked\n")
    _git("add", "-f", "reports/local/tracked.txt", cwd=main_checkout)
    _git("commit", "-q", "-m", "tracked provenance file", cwd=main_checkout)

    verdict = _evaluate_write(
        main_checkout,
        main_checkout / "reports/local/tracked.txt",
        _busy_item(main_checkout),
    )
    assert verdict.decision == "deny"
    assert verdict.rule == "main-checkout-write"


def test_undeclared_ignored_path_is_refused(main_checkout: Path) -> None:
    """A gitignored path outside any declared root is still refused."""
    _gitignore(main_checkout, "other/")
    _write_marker(main_checkout, ["reports/local/"])
    verdict = _evaluate_write(
        main_checkout, main_checkout / "other/y.py", _busy_item(main_checkout)
    )
    assert verdict.decision == "deny"
    assert verdict.rule == "main-checkout-write"


def test_dotdot_escape_is_refused(main_checkout: Path) -> None:
    """A ``..`` path that lexically leaves the declared root is refused."""
    _gitignore(main_checkout, "reports/local/")
    _write_marker(main_checkout, ["reports/local/"])
    target = main_checkout / "reports/local/../../escape.txt"
    verdict = _evaluate_write(main_checkout, target, _busy_item(main_checkout))
    assert verdict.decision == "deny"


def test_symlink_escape_is_refused(main_checkout: Path, tmp_path: Path) -> None:
    """A symlink inside the declared root pointing outside it is refused."""
    outside = tmp_path / "outside"
    outside.mkdir()
    _gitignore(main_checkout, "reports/local/")
    _write_marker(main_checkout, ["reports/local/"])
    (main_checkout / "reports/local").mkdir(parents=True)
    os.symlink(outside, main_checkout / "reports/local/esc")

    verdict = _evaluate_write(
        main_checkout,
        main_checkout / "reports/local/esc/x.py",
        _busy_item(main_checkout),
    )
    assert verdict.decision == "deny"


def test_ignored_symlink_to_tracked_file_is_refused(main_checkout: Path) -> None:
    """The combination the two simpler cases miss: an ignored symlink whose
    target is a tracked file inside the root. The lexical path reads as
    ignored (the symlink itself), but the write lands on a tracked file, so
    the resolved path's gitignore status must decide -- and refuse."""
    _gitignore(main_checkout, "reports/local/")
    _write_marker(main_checkout, ["reports/local/"])
    (main_checkout / "reports/local").mkdir(parents=True)
    tracked = main_checkout / "reports/local/tracked.txt"
    tracked.write_text("tracked\n")
    _git("add", "-f", "reports/local/tracked.txt", cwd=main_checkout)
    _git("commit", "-q", "-m", "tracked provenance file", cwd=main_checkout)
    os.symlink("tracked.txt", main_checkout / "reports/local/alias.txt")

    verdict = _evaluate_write(
        main_checkout,
        main_checkout / "reports/local/alias.txt",
        _busy_item(main_checkout),
    )
    assert verdict.decision == "deny"


def test_nonexistent_descendant_beneath_symlink_is_refused(
    main_checkout: Path, tmp_path: Path
) -> None:
    """A not-yet-created parent under an escape symlink still resolves to the
    main checkout (walk-up discovery) and is refused, not fail-open."""
    outside = tmp_path / "outside2"
    outside.mkdir()
    _gitignore(main_checkout, "reports/local/")
    _write_marker(main_checkout, ["reports/local/"])
    (main_checkout / "reports/local").mkdir(parents=True)
    os.symlink(outside, main_checkout / "reports/local/esc")

    verdict = _evaluate_write(
        main_checkout,
        main_checkout / "reports/local/esc/nonexistent/x.py",
        _busy_item(main_checkout),
    )
    assert verdict.decision == "deny"


def test_declared_root_symlinked_outside_repo_is_refused(
    main_checkout: Path, tmp_path: Path
) -> None:
    """A declared root that is itself a symlink out of the repo is dropped, so
    the write stays refused."""
    outside = tmp_path / "outside3"
    outside.mkdir()
    (main_checkout / "reports").mkdir()
    os.symlink(outside, main_checkout / "reports/local")
    _write_marker(main_checkout, ["reports/local/"])

    verdict = _evaluate_write(
        main_checkout,
        main_checkout / "reports/local/x.py",
        _busy_item(main_checkout),
    )
    assert verdict.decision == "deny"


def test_declaration_of_repo_root_is_dropped(main_checkout: Path) -> None:
    """Declaring ``.`` would widen the exception to every ignored path; it is
    dropped and the write is refused."""
    _gitignore(main_checkout, "build/")
    _write_marker(main_checkout, ["."])
    verdict = _evaluate_write(
        main_checkout, main_checkout / "build/x.py", _busy_item(main_checkout)
    )
    assert verdict.decision == "deny"


def test_sibling_prefix_does_not_match(main_checkout: Path) -> None:
    """``reports/local2/`` must not match a declared ``reports/local/`` (the
    containment check is component-aware, not a string prefix)."""
    _gitignore(main_checkout, "reports/local2/")
    _write_marker(main_checkout, ["reports/local/"])
    verdict = _evaluate_write(
        main_checkout,
        main_checkout / "reports/local2/x.py",
        _busy_item(main_checkout),
    )
    assert verdict.decision == "deny"


def test_related_file_still_requires_claim(main_checkout: Path) -> None:
    """The allowlist must not bypass R4: a declared, ignored target that is
    also an in-progress item's related_file is still denied without a claim."""
    _gitignore(main_checkout, "reports/local/")
    _write_marker(main_checkout, ["reports/local/"])
    (main_checkout / "reports/local").mkdir(parents=True)
    related = main_checkout / "reports/local/foo.py"
    related.write_text("x\n")
    items = [
        {
            "id": "demo-slug",
            "status": "in-progress",
            "related_files": [{"path": str(related)}],
        }
    ]
    verdict = _evaluate_write(main_checkout, related, items)
    assert verdict.decision == "deny"
    assert verdict.rule == "unclaimed-item-write"


def test_missing_marker_still_refuses(main_checkout: Path) -> None:
    _gitignore(main_checkout, "reports/local/")
    verdict = _evaluate_write(
        main_checkout,
        main_checkout / "reports/local/x.py",
        _busy_item(main_checkout),
    )
    assert verdict.decision == "deny"
    assert verdict.rule == "main-checkout-write"


def test_malformed_marker_still_refuses(main_checkout: Path) -> None:
    _gitignore(main_checkout, "reports/local/")
    (main_checkout / ".guard-rails.json").write_text("{not json", encoding="utf-8")
    verdict = _evaluate_write(
        main_checkout,
        main_checkout / "reports/local/x.py",
        _busy_item(main_checkout),
    )
    assert verdict.decision == "deny"


def test_marker_with_invalid_entries_still_refuses(main_checkout: Path) -> None:
    _gitignore(main_checkout, "reports/local/")
    _write_marker(main_checkout, [None, 123, "", "/abs", "..", {"a": 1}, "reports/local/"])
    # Only "reports/local/" survives; the rest are dropped. But this target is
    # under the surviving root and ignored, so it is allowed — the point is
    # that invalid entries are skipped without aborting the read. To pin the
    # refusal side, also assert a path under no surviving bad entry is refused.
    allowed = _evaluate_write(
        main_checkout,
        main_checkout / "reports/local/x.py",
        _busy_item(main_checkout),
    )
    assert allowed.decision == "allow"

    verdict = _evaluate_write(
        main_checkout,
        main_checkout / "unrelated/z.py",
        _busy_item(main_checkout),
    )
    assert verdict.decision == "deny"
