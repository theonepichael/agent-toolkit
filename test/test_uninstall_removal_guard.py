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


def _rmtree_sites(filename: str) -> list[tuple[str, int]]:
    tree = ast.parse((REPO_ROOT / filename).read_text(), filename=filename)
    imports = _ShutilImportResolver()
    imports.visit(tree)
    scoped = _RmtreeScopeVisitor(imports.shutil_aliases, imports.rmtree_names)
    scoped.visit(tree)
    return scoped.sites


def _unallowed_rmtree_sites(filename: str) -> list[tuple[str, int]]:
    allowed = ALLOWED_RMTREE_FUNCTIONS[filename]
    return [
        (filename, lineno)
        for scope, lineno in _rmtree_sites(filename)
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
