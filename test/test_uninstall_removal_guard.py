#!/usr/bin/env python3
"""Class guard: shutil.rmtree may only be called from a small, named set of
functions across the allowlisted modules (install.py, uninstall_exec.py,
uninstall_state.py).

Every shutil.rmtree call on a tree-manifest-tracked directory goes through
uninstall_state.installed_tree_verdict first, either via uninstall_state.remove_manifest_tree
(the uninstall-time policy: remove only on TREE_UNCHANGED) or directly —
a deliberate unguarded sweep like _wipe_neovim_dirs, whose scope is
Neovim's own XDG data/state/cache dirs and never a vendor runtime tree
(see that function's docstring for why). Every other legitimate rmtree
call site is named explicitly in ALLOWED_RMTREE_FUNCTIONS below, with a
reason. Adding a new function that calls shutil.rmtree without updating
this allowlist fails the test here, forcing a deliberate choice (route
through uninstall_state.remove_manifest_tree, or add a reasoned allowlist entry)
instead of silently removing a user-modified tree without checking its
manifest.

The allowlist is an obligation list as well as a permission list: an entry
whose function stops calling shutil.rmtree fails
test_detector_sees_allowlisted_functions_in_real_files, so a stale entry is
removed rather than left matching nothing.
"""

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

ALLOWED_RMTREE_FUNCTIONS = {
    "install.py": {
        "_wipe_neovim_dirs",  # deliberate unguarded XDG state/cache sweep, distinct from the vendor runtime tree
    },
    "uninstall_exec.py": {
        "_execute_remove_directory",  # generic executor for .nvm + shared neovim dirs, never manifest-tracked
    },
    "uninstall_state.py": {
        "remove_manifest_tree",  # the blessed function itself
    },
}


class _ShutilImportResolver(ast.NodeVisitor):
    """Names in this module that resolve to the shutil module and to
    shutil.rmtree specifically, however they were imported."""

    def __init__(self) -> None:
        self.shutil_aliases: set[str] = set()
        self.rmtree_names: set[str] = set()

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name == "shutil":
                self.shutil_aliases.add(alias.asname or "shutil")

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module == "shutil":
            for alias in node.names:
                if alias.name == "rmtree":
                    self.rmtree_names.add(alias.asname or "rmtree")


class _RmtreeScopeVisitor(ast.NodeVisitor):
    """Attributes every shutil.rmtree call to its nearest enclosing function
    (or "<module>"), single-pass — correct for nested function defs, unlike
    two nested ast.walk() calls over the same tree."""

    def __init__(self, shutil_aliases: set[str], rmtree_names: set[str]) -> None:
        self._shutil_aliases = shutil_aliases
        self._rmtree_names = rmtree_names
        self._scope_stack: list[str] = ["<module>"]
        self.sites: list[tuple[str, int]] = []  # (enclosing function name, lineno)

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._scope_stack.append(node.name)
        self.generic_visit(node)
        self._scope_stack.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def visit_Call(self, node: ast.Call) -> None:
        if self._is_rmtree_call(node):
            self.sites.append((self._scope_stack[-1], node.lineno))
        self.generic_visit(node)

    def _is_rmtree_call(self, node: ast.Call) -> bool:
        func = node.func
        if isinstance(func, ast.Name):
            return func.id in self._rmtree_names
        if isinstance(func, ast.Attribute):
            return (
                func.attr == "rmtree"
                and isinstance(func.value, ast.Name)
                and func.value.id in self._shutil_aliases
            )
        return False


def _rmtree_sites(
    filename: str, source: str | None = None
) -> list[tuple[str, int]]:
    """Every shutil.rmtree call site in one module, as (scope, lineno).

    `source` overrides reading `filename` from disk, so a test can hand the
    scanner a literal instead of planting a file; `None` is the only value
    treated as "read the real file", so an empty injected source stays empty
    rather than silently falling back to disk.
    """
    text = (REPO_ROOT / filename).read_text() if source is None else source
    tree = ast.parse(text, filename=filename)
    imports = _ShutilImportResolver()
    imports.visit(tree)
    scoped = _RmtreeScopeVisitor(imports.shutil_aliases, imports.rmtree_names)
    scoped.visit(tree)
    return scoped.sites


def _unallowed_rmtree_sites(
    filename: str, source: str | None = None
) -> list[tuple[str, int]]:
    allowed = ALLOWED_RMTREE_FUNCTIONS[filename]
    return [
        (filename, lineno)
        for scope, lineno in _rmtree_sites(filename, source)
        if scope not in allowed
    ]


# The red string below was observed with a stray shutil.rmtree planted in
# uninstall_exec.py, a file the pre-change parametrization never scanned.
@pytest.mark.regression(
    "removal-guard-skips-allowlisted-file",
    "assert not [('uninstall_exec.py', 1406)]",
)
@pytest.mark.parametrize("filename", sorted(ALLOWED_RMTREE_FUNCTIONS))
def test_rmtree_only_called_from_allowed_functions(filename: str) -> None:
    violations = _unallowed_rmtree_sites(filename)
    assert not violations, (
        "shutil.rmtree called outside the allowed-function list "
        f"{sorted(ALLOWED_RMTREE_FUNCTIONS[filename])!r} — either this is a "
        "new tree-manifest-tracked removal that must go through "
        "uninstall_state.remove_manifest_tree instead of calling shutil.rmtree "
        "directly, or it's a new legitimate non-manifest wholesale removal "
        "that needs a reasoned entry added to ALLOWED_RMTREE_FUNCTIONS: "
        + ", ".join(f"{f}:{ln}" for f, ln in violations)
    )


@pytest.mark.regression(
    "removal-guard-silently-misses-forbidden-rmtree",
    "assert [] == [('_execute_remove_directory', 4), ('_sweep_runtime_tree', 7)]",
)
def test_detector_reports_forbidden_rmtree_in_injected_source() -> None:
    # Red string below was captured with _rmtree_sites stubbed to return []
    # (the negative control); against the pre-change HEAD the call raises
    # TypeError for the extra argument instead, so the AssertionError is the
    # evidence that matters.
    source = (
        "import shutil\n"
        "\n"
        "def _execute_remove_directory(path):\n"
        "    shutil.rmtree(path)\n"
        "\n"
        "def _sweep_runtime_tree(path):\n"
        "    shutil.rmtree(path)\n"
    )
    # Both calls are attributed to distinct scopes before filtering, so
    # "line 4 removed by the allowlist" cannot be confused with "line 4 never
    # seen at all".
    assert _rmtree_sites("uninstall_exec.py", source) == [
        ("_execute_remove_directory", 4),
        ("_sweep_runtime_tree", 7),
    ]
    # The allowlist drops the allowed scope and keeps the forbidden one.
    assert _unallowed_rmtree_sites("uninstall_exec.py", source=source) == [
        ("uninstall_exec.py", 7)
    ]
    # An empty injected source must not fall back to the real file: the real
    # uninstall_exec.py does contain a call, so a falsy check would leak it in.
    assert _rmtree_sites("uninstall_exec.py", "") == []


# The three tests below pin the three import forms _ShutilImportResolver's
# visitors claim to handle: an aliased module import (visit_Import's
# `asname or "shutil"`), an aliased function import, and an unaliased function
# import (visit_ImportFrom's `asname or "rmtree"`). All three are otherwise
# unexercised — every allowlisted file uses plain `import shutil` with
# qualified calls — so without them a mutation of any one branch leaves the
# whole suite green. Their red strings were captured by mutating the branch
# each one covers; see each test's comment for which mutation, since this
# change adds tests and touches no resolver code.
@pytest.mark.regression(
    "removal-guard-misses-aliased-shutil-module",
    "assert [] == [('_sweep_runtime_tree', 4)]",
)
def test_detector_resolves_aliased_shutil_module_import() -> None:
    # Red captured with visit_Import's entry mutated to add only "shutil" (no
    # `asname or`), so `sh` never lands in shutil_aliases and the attribute
    # call on it is invisible to the scope visitor.
    source = (
        "import shutil as sh\n"
        "\n"
        "def _sweep_runtime_tree(path):\n"
        "    sh.rmtree(path)\n"
    )
    assert _rmtree_sites("uninstall_exec.py", source) == [
        ("_sweep_runtime_tree", 4),
    ]
    # _sweep_runtime_tree is not allowlisted for uninstall_exec.py, so the
    # alias-resolved site must survive into the violation list.
    assert _unallowed_rmtree_sites("uninstall_exec.py", source=source) == [
        ("uninstall_exec.py", 4)
    ]


@pytest.mark.regression(
    "removal-guard-misses-aliased-rmtree-import",
    "assert [] == [('_sweep_runtime_tree', 4)]",
)
def test_detector_resolves_aliased_rmtree_function_import() -> None:
    # Red captured with visit_ImportFrom's entry mutated to add only "rmtree"
    # (no `asname or`), so `rm` never lands in rmtree_names and the bare call
    # is invisible to the scope visitor.
    source = (
        "from shutil import rmtree as rm\n"
        "\n"
        "def _sweep_runtime_tree(path):\n"
        "    rm(path)\n"
    )
    assert _rmtree_sites("uninstall_exec.py", source) == [
        ("_sweep_runtime_tree", 4),
    ]
    assert _unallowed_rmtree_sites("uninstall_exec.py", source=source) == [
        ("uninstall_exec.py", 4)
    ]


@pytest.mark.regression(
    "removal-guard-misses-unaliased-rmtree-import",
    "assert [] == [('_sweep_runtime_tree', 4)]",
)
def test_detector_resolves_unaliased_rmtree_function_import() -> None:
    # Red captured with visit_ImportFrom's entry narrowed to
    # `if alias.asname:` — the `or "rmtree"` fallback dropped — which is the
    # only thing making an unaliased `from shutil import rmtree` resolve.
    source = (
        "from shutil import rmtree\n"
        "\n"
        "def _sweep_runtime_tree(path):\n"
        "    rmtree(path)\n"
    )
    assert _rmtree_sites("uninstall_exec.py", source) == [
        ("_sweep_runtime_tree", 4),
    ]
    assert _unallowed_rmtree_sites("uninstall_exec.py", source=source) == [
        ("uninstall_exec.py", 4)
    ]


@pytest.mark.parametrize("filename", sorted(ALLOWED_RMTREE_FUNCTIONS))
def test_detector_sees_allowlisted_functions_in_real_files(filename: str) -> None:
    scopes = {scope for scope, _ in _rmtree_sites(filename)}
    assert ALLOWED_RMTREE_FUNCTIONS[filename] <= scopes


def test_allowed_functions_still_exist() -> None:
    """Catches the allowlist going stale (renamed/removed functions leaving
    a name entry that silently matches nothing, making the ban vacuous for
    that entry)."""
    for filename, names in ALLOWED_RMTREE_FUNCTIONS.items():
        tree = ast.parse((REPO_ROOT / filename).read_text(), filename=filename)
        defined = {
            n.name
            for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)
        }
        missing = names - defined
        assert not missing, (
            f"{filename}: allowlisted function(s) no longer exist: {missing}"
        )
