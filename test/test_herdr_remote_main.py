#!/usr/bin/env python3
"""Tests for herdr_remote.__main__: the version marker used by /api/health.

Requires Python 3.12+.
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import pytest  # noqa: E402

from herdr_remote import __main__ as herdr_main  # noqa: E402

SHORT_SHA = re.compile(r"^[0-9a-f]{4,40}$")


@pytest.mark.allow_real_subprocess  # reads this real repo's git sha, no writes
def test_repo_version_returns_a_real_short_sha():
    version = herdr_main._repo_version()
    assert SHORT_SHA.match(version), version


@pytest.mark.allow_real_subprocess  # attempts a real (failing) git invocation
def test_repo_version_degrades_to_unknown_outside_a_repo(tmp_path, monkeypatch):
    monkeypatch.setattr(herdr_main, "REPO_ROOT", tmp_path)
    assert herdr_main._repo_version() == "unknown"
