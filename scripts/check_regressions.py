#!/usr/bin/env python3
"""Check @pytest.mark.regression marks across repo test suites.

Validates that every regression marker conforms to the required AST shape:
`@pytest.mark.regression("<label>", "<observed pre-fix failure, verbatim>")`
where <label> is a kebab-case name describing the regression itself (not a
ticket id — ticket linkage lives in the backlog store, not in git history).
Enforces the minimum-marks invariant (at least 1 valid mark must be present).
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

_SLUG_RE = re.compile(r"^[a-z]+(?:-[a-z0-9]+)+$")


def _is_pytest_mark_regression(node: ast.AST) -> bool:
    if isinstance(node, ast.Attribute) and node.attr == "regression":
        val = node.value
        if isinstance(val, ast.Attribute) and val.attr == "mark":
            return isinstance(val.value, ast.Name) and val.value.id == "pytest"
    return False


def _is_bare_mark_regression(node: ast.AST) -> bool:
    if isinstance(node, ast.Attribute) and node.attr == "regression":
        return isinstance(node.value, ast.Name) and node.value.id == "mark"
    return False


def check_file(path: Path, marks: dict[str, dict[str, str]], errors: list[str]) -> None:
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as err:
        errors.append(f"{path}: could not read file: {err}")
        return

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError as err:
        errors.append(f"{path}:{err.lineno}: syntax error: {err.msg}")
        return

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call):
                if _is_pytest_mark_regression(dec.func):
                    # Validate arguments
                    if len(dec.args) != 2 or dec.keywords:
                        errors.append(
                            f"{path}:{dec.lineno}: @pytest.mark.regression requires "
                            f"exactly 2 positional arguments (item-slug, red-failure), "
                            f"got {len(dec.args)} args and {len(dec.keywords)} kwargs"
                        )
                        continue
                    item_arg, red_arg = dec.args[0], dec.args[1]
                    if not (
                        isinstance(item_arg, ast.Constant)
                        and isinstance(item_arg.value, str)
                    ):
                        errors.append(
                            f"{path}:{item_arg.lineno}: @pytest.mark.regression first "
                            "argument must be a string literal item slug"
                        )
                        continue
                    if not (
                        isinstance(red_arg, ast.Constant)
                        and isinstance(red_arg.value, str)
                    ):
                        errors.append(
                            f"{path}:{red_arg.lineno}: @pytest.mark.regression second "
                            "argument must be a string literal expected failure"
                        )
                        continue
                    slug = item_arg.value
                    if not _SLUG_RE.match(slug):
                        errors.append(
                            f"{path}:{item_arg.lineno}: malformed label {slug!r} "
                            "(expected kebab-case, e.g. sandbox-guard-blocks-real-home-write)"
                        )
                        continue
                    key = f"{path}:{dec.lineno}"
                    marks[key] = {"item": slug, "red": red_arg.value}
                elif _is_bare_mark_regression(dec.func):
                    errors.append(
                        f"{path}:{dec.lineno}: bare @mark.regression via aliased import "
                        "is forbidden; use @pytest.mark.regression"
                    )
            elif _is_pytest_mark_regression(dec) or _is_bare_mark_regression(dec):
                errors.append(
                    f"{path}:{dec.lineno}: @pytest.mark.regression must be called with "
                    "arguments (item-slug, red-failure)"
                )


def scan_paths(paths: list[Path]) -> tuple[dict[str, dict[str, str]], list[str], bool]:
    marks: dict[str, dict[str, str]] = {}
    errors: list[str] = []
    has_path_error = False

    files_to_check: list[Path] = []
    for p in paths:
        if not p.exists():
            errors.append(f"path does not exist: {p}")
            has_path_error = True
            continue
        if p.is_file():
            if p.suffix == ".py":
                files_to_check.append(p)
        else:
            files_to_check.extend(sorted(p.rglob("*.py")))

    for f in sorted(files_to_check):
        check_file(f, marks, errors)

    return marks, errors, has_path_error


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify @pytest.mark.regression usage across tests."
    )
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="Directories or files to scan (default: test/)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output findings as JSON",
    )
    args = parser.parse_args(argv)

    if not args.paths:
        repo_root = Path(__file__).resolve().parent.parent
        target_paths = [repo_root / "test"]
    else:
        target_paths = [p.resolve() for p in args.paths]

    marks, errors, has_path_error = scan_paths(target_paths)

    if has_path_error:
        for err in errors:
            print(f"ERROR: {err}", file=sys.stderr)
        return 2

    if errors:
        for err in errors:
            print(f"ERROR: {err}", file=sys.stderr)
        return 1

    if not marks:
        print("ERROR: zero regression marks found in scanned paths", file=sys.stderr)
        return 1

    if args.json_output:
        print(json.dumps(marks, indent=2))
    else:
        print(f"OK: found {len(marks)} valid regression mark(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
