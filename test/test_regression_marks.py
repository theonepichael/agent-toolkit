#!/usr/bin/env python3
"""Tests for scripts/check_regressions.py."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CHECKER = REPO_ROOT / "scripts" / "check_regressions.py"


@pytest.mark.allow_real_subprocess
def test_checker_passes_on_repo():
    result = subprocess.run(
        [sys.executable, str(CHECKER), "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    data = json.loads(result.stdout)
    assert any(
        entry["item"] == "sandbox-guard-blocks-real-home-write"
        for entry in data.values()
    )


@pytest.mark.allow_real_subprocess
def test_checker_fails_on_zero_marks(tmp_path):
    empty_file = tmp_path / "test_empty.py"
    empty_file.write_text("def test_foo(): pass\n")
    result = subprocess.run(
        [sys.executable, str(CHECKER), str(empty_file)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "zero regression marks" in result.stderr


@pytest.mark.allow_real_subprocess
def test_checker_fails_on_missing_arg(tmp_path):
    f = tmp_path / "test_missing.py"
    f.write_text(
        "import pytest\n"
        "@pytest.mark.regression('foo-bar')\n"
        "def test_foo(): pass\n"
    )
    result = subprocess.run(
        [sys.executable, str(CHECKER), str(f)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "requires exactly 2 positional arguments" in result.stderr


@pytest.mark.allow_real_subprocess
def test_checker_fails_on_non_literal_arg(tmp_path):
    f = tmp_path / "test_non_literal.py"
    f.write_text(
        "import pytest\n"
        "slug = 'foo-bar'\n"
        "@pytest.mark.regression(label, 'red')\n"
        "def test_foo(): pass\n"
    )
    result = subprocess.run(
        [sys.executable, str(CHECKER), str(f)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "must be a string literal item slug" in result.stderr


@pytest.mark.allow_real_subprocess
def test_checker_fails_on_bad_slug(tmp_path):
    f = tmp_path / "test_bad_slug.py"
    f.write_text(
        "import pytest\n"
        "@pytest.mark.regression('BAD_SLUG!', 'red')\n"
        "def test_foo(): pass\n"
    )
    result = subprocess.run(
        [sys.executable, str(CHECKER), str(f)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "malformed label" in result.stderr


@pytest.mark.allow_real_subprocess
def test_checker_fails_on_aliased_mark_decorator(tmp_path):
    f = tmp_path / "test_aliased.py"
    f.write_text(
        "from pytest import mark\n"
        "@mark.regression('foo-bar', 'red')\n"
        "def test_foo(): pass\n"
    )
    result = subprocess.run(
        [sys.executable, str(CHECKER), str(f)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 1
    assert "bare @mark.regression" in result.stderr


@pytest.mark.allow_real_subprocess
def test_checker_exits_2_on_nonexistent_path(tmp_path):
    nonexistent = tmp_path / "nonexistent_dir"
    result = subprocess.run(
        [sys.executable, str(CHECKER), str(nonexistent)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 2
    assert "does not exist" in result.stderr
