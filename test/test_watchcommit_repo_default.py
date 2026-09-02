#!/usr/bin/env python3
"""watchcommit's default-repo fallback must derive from its own file
location, not a hardcoded ~/dotfiles path — see settings_seed_drift_check.py
and dotfiles_sync_check.py for the same convention."""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import watchcommit  # noqa: E402 — must follow sys.path.insert above


def test_default_repo_derives_from_file_location_not_home():
    assert watchcommit.DEFAULT_REPO == REPO_ROOT
