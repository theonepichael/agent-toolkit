"""Pytest wiring for the shared test sandbox in ``agent-scripts/test_bootstrap.py``.

The safety machinery (sandboxed ``HOME``, the guarded mutation-API patches,
the activation flags) lives in ``test_bootstrap`` so direct
``python3 test_X.py`` runs share it. This module only wires pytest to it:
bootstrap at import (before any test module computes ``Path.home()``-rooted
constants), then per-test guard activation honoring the
``allow_real_subprocess`` / ``allow_production_paths`` markers.
"""

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent
if str(_REPO_ROOT / "agent-scripts") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "agent-scripts"))

import test_bootstrap  # noqa: E402 — HOME must be redirected before other imports

test_bootstrap.bootstrap()

# Kept for test/test_conftest_guards.py, which imports `conftest._REAL_HOME`.
_REAL_HOME = test_bootstrap.REAL_HOME

import pytest  # noqa: E402
import test_layouts  # noqa: E402


@pytest.fixture(autouse=True)
def guard_real_subprocess(request: pytest.FixtureRequest) -> None:
    allowed = request.node.get_closest_marker("allow_real_subprocess") is not None
    with test_bootstrap.guards(subprocess=not allowed):
        yield


@pytest.fixture(autouse=True)
def guard_production_paths(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    allowed = request.node.get_closest_marker("allow_production_paths") is not None
    if allowed:
        monkeypatch.setenv("HOME", str(test_bootstrap.REAL_HOME))
    with test_bootstrap.guards(paths=not allowed):
        yield


@pytest.fixture
def two_layout_homes(tmp_path: Path) -> test_layouts.TwoLayoutHomes:
    """A home holding both layouts plus a legacy-only fake peer home.

    See ``agent-scripts/test_layouts.py`` for what this does and does not
    prove.
    """
    return test_layouts.build_two_layout_homes(tmp_path)
