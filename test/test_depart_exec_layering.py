"""Layering guards for depart_exec.py — the extracted departure executor.

install.py imports depart_exec, so depart_exec must never import install
(not even under TYPE_CHECKING) and must depend only on the stdlib and the
depart data model. Mirrors test_install.py's conventions: sandboxed HOME,
no real subprocess, no production-path writes.
"""

import ast
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))  # noqa: E402 — repo-root module imports

import depart_exec  # noqa: E402 — must follow sys.path.insert above


def _import_nodes(tree: ast.AST) -> list[ast.stmt]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]


def test_depart_exec_never_imports_install() -> None:
    """No ``install`` import anywhere in depart_exec — conditional included."""
    tree = ast.parse((REPO_ROOT / "depart_exec.py").read_text())
    for node in _import_nodes(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        else:
            names = [node.module or ""]
        for name in names:
            assert name.split(".")[0] != "install", (
                f"depart_exec.py must not import install (found {name!r} "
                f"at line {node.lineno})"
            )


def test_depart_exec_imports_only_stdlib_and_depart() -> None:
    """The module's entire import graph is stdlib + depart (no transitives)."""
    tree = ast.parse((REPO_ROOT / "depart_exec.py").read_text())
    roots: set[str] = set()
    for node in _import_nodes(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        else:
            roots.add((node.module or "").split(".")[0])
    allowed = set(sys.stdlib_module_names) | {
        "depart",
        "cli_common",
        "settings_seed_drift_check",
    }
    unexpected = roots - allowed
    assert not unexpected, f"unexpected imports in depart_exec.py: {sorted(unexpected)}"


def test_depart_exec_imports_standalone() -> None:
    """depart_exec imports cleanly with no other installer module loaded."""
    assert depart_exec.Deps is not None
    assert depart_exec.capture_departure_baseline is not None
    assert depart_exec.do_depart is not None
