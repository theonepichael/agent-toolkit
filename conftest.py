import atexit
import builtins
import io
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

_REAL_HOME = Path(os.path.expanduser("~")).resolve()
_SANDBOX_HOME = tempfile.mkdtemp(prefix="dotfiles-pytest-home-")
os.environ["HOME"] = _SANDBOX_HOME
atexit.register(shutil.rmtree, _SANDBOX_HOME, ignore_errors=True)

import pytest  # noqa: E402 — HOME must be redirected before any other import

_ORIGINAL_POPEN_INIT = subprocess.Popen.__init__
_ORIGINAL_OPEN = io.open
_ORIGINAL_OS_OPEN = os.open
_WRITE_MODE_CHARS = frozenset("wax+")
_GUARDED_HOME_SUBDIRS = (_REAL_HOME / ".claude", _REAL_HOME / ".config")
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT

_CURRENT_ALLOW_SUBPROCESS = False
_IN_TEST_SUBPROCESS_GUARD = False
_CURRENT_ALLOW_PRODUCTION_PATHS = False
_IN_TEST_PATH_GUARD = False


def _is_write_mode(mode: str) -> bool:
    return any(c in _WRITE_MODE_CHARS for c in mode)


def _resolve_if_path(target: object) -> Path | None:
    if not isinstance(target, (str, bytes, os.PathLike)):
        return None
    s = os.fsdecode(target)
    if not (".claude" in s or ".config" in s or "~" in s):
        return None
    return Path(s).expanduser().resolve()


def _is_guarded_target(resolved: Path | None) -> bool:
    if resolved is None:
        return False
    return any(resolved.is_relative_to(d) for d in _GUARDED_HOME_SUBDIRS)


def _deny_production_path(resolved: Path, marker: str) -> None:
    raise RuntimeError(
        f"blocked real filesystem access to {resolved} under the real "
        f"HOME during a test — mark the test with @pytest.mark.{marker} "
        "to allow it"
    )


def _guarded_popen_init(
    self: subprocess.Popen, *args: object, **kwargs: object
) -> None:
    if _IN_TEST_SUBPROCESS_GUARD and not _CURRENT_ALLOW_SUBPROCESS:
        raise RuntimeError(
            "blocked a real subprocess.Popen/run/call/check_output call "
            "during a test — mark the test with "
            "@pytest.mark.allow_real_subprocess to allow it"
        )
    _ORIGINAL_POPEN_INIT(self, *args, **kwargs)


subprocess.Popen.__init__ = _guarded_popen_init


def _guarded_open(
    file: object, mode: str = "r", *args: object, **kwargs: object
) -> object:
    if (
        _IN_TEST_PATH_GUARD
        and not _CURRENT_ALLOW_PRODUCTION_PATHS
        and _is_write_mode(mode)
    ):
        resolved = _resolve_if_path(file)
        if _is_guarded_target(resolved):
            assert resolved is not None
            _deny_production_path(resolved, "allow_production_paths")
    return _ORIGINAL_OPEN(file, mode, *args, **kwargs)


builtins.open = _guarded_open
io.open = _guarded_open


def _make_guarded(original: object, *, path_args: tuple[int, ...] = (0,)) -> object:
    def guarded(*args: object, **kwargs: object) -> object:
        if _IN_TEST_PATH_GUARD and not _CURRENT_ALLOW_PRODUCTION_PATHS:
            for i in path_args:
                if i >= len(args):
                    continue
                resolved = _resolve_if_path(args[i])
                if _is_guarded_target(resolved):
                    assert resolved is not None
                    _deny_production_path(resolved, "allow_production_paths")
        return original(*args, **kwargs)

    return guarded


Path.write_text = _make_guarded(Path.write_text)
Path.write_bytes = _make_guarded(Path.write_bytes)
Path.unlink = _make_guarded(Path.unlink)
Path.rmdir = _make_guarded(Path.rmdir)
Path.mkdir = _make_guarded(Path.mkdir)
Path.rename = _make_guarded(Path.rename, path_args=(0, 1))
Path.replace = _make_guarded(Path.replace, path_args=(0, 1))

os.remove = _make_guarded(os.remove)
os.unlink = _make_guarded(os.unlink)
os.rmdir = _make_guarded(os.rmdir)
os.mkdir = _make_guarded(os.mkdir)
os.makedirs = _make_guarded(os.makedirs)
os.rename = _make_guarded(os.rename, path_args=(0, 1))
os.replace = _make_guarded(os.replace, path_args=(0, 1))
shutil.rmtree = _make_guarded(shutil.rmtree)


def _guarded_os_open(
    path: object, flags: int, *args: object, **kwargs: object
) -> object:
    if (
        _IN_TEST_PATH_GUARD
        and not _CURRENT_ALLOW_PRODUCTION_PATHS
        and (flags & _WRITE_FLAGS)
    ):
        resolved = _resolve_if_path(path)
        if _is_guarded_target(resolved):
            assert resolved is not None
            _deny_production_path(resolved, "allow_production_paths")
    return _ORIGINAL_OS_OPEN(path, flags, *args, **kwargs)


os.open = _guarded_os_open


@pytest.fixture(autouse=True)
def guard_real_subprocess(request: pytest.FixtureRequest) -> None:
    global _CURRENT_ALLOW_SUBPROCESS, _IN_TEST_SUBPROCESS_GUARD
    allowed = request.node.get_closest_marker("allow_real_subprocess") is not None
    prev_allow = _CURRENT_ALLOW_SUBPROCESS
    prev_guard = _IN_TEST_SUBPROCESS_GUARD
    _CURRENT_ALLOW_SUBPROCESS = allowed
    _IN_TEST_SUBPROCESS_GUARD = True
    try:
        yield
    finally:
        _CURRENT_ALLOW_SUBPROCESS = prev_allow
        _IN_TEST_SUBPROCESS_GUARD = prev_guard


@pytest.fixture(autouse=True)
def guard_production_paths(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> None:
    global _CURRENT_ALLOW_PRODUCTION_PATHS, _IN_TEST_PATH_GUARD
    allowed = request.node.get_closest_marker("allow_production_paths") is not None
    if allowed:
        monkeypatch.setenv("HOME", str(_REAL_HOME))
    prev_allow = _CURRENT_ALLOW_PRODUCTION_PATHS
    prev_guard = _IN_TEST_PATH_GUARD
    _CURRENT_ALLOW_PRODUCTION_PATHS = allowed
    _IN_TEST_PATH_GUARD = True
    try:
        yield
    finally:
        _CURRENT_ALLOW_PRODUCTION_PATHS = prev_allow
        _IN_TEST_PATH_GUARD = prev_guard
