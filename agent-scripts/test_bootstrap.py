#!/usr/bin/env python3
"""Shared test-run sandbox: one source of truth for pytest and direct runs.

This module owns every safety mechanism the repo's two test tiers share:

- ``bootstrap()`` redirects ``HOME`` to a throwaway sandbox directory and
  installs write-denial guards on the mutating filesystem APIs listed in
  :data:`GUARDED_MUTATION_APIS`, plus the real-subprocess guard on
  ``subprocess.Popen``.
- Guards are *installed* by ``bootstrap()`` but *inactive* until something
  activates them: under pytest that is ``conftest.py``'s autouse fixtures
  (per test, honoring the ``allow_real_subprocess`` /
  ``allow_production_paths`` markers); under a direct ``python3 test_X.py``
  run it is :func:`run_unittest_main` (path guard on, subprocess guard off —
  see its docstring for why plain unittest cannot honor pytest markers).

Why this module exists
----------------------
Before it, the safety lived entirely in ``conftest.py`` and therefore applied
only when pytest collected a test. The unittest-style tier (living in
``test/`` since the 2026-09-18 consolidation; see ``test/AGENTS.md``) is
runnable via plain ``python3 test_X.py``, and on that path nothing was
sandboxed at all.

Import-order contract
---------------------
Toolkit data paths resolve at each use, but a production script may still
compute a ``Path.home()``-rooted value at import (e.g.
``bundle_drift_check.STATE_DIR``). ``bootstrap()`` must therefore
run **before** any script under test is imported — a direct-run test file
calls it at the top of the file, not from its ``__main__`` block.

Not a test module
-----------------
Despite the ``test_`` filename (which here is the repo's links.toml- and
INTERFACES.md-exemption convention for in-repo support files, see
``agent-scripts/AGENTS.md``), this module contains no test cases; pytest
collects zero tests from it. It must stay standard-library-only so direct
runs work without a synced venv, and must never import ``pytest``.
``importlib.reload()`` of this module is unsupported: it would re-execute
the module, resetting the install sentinel and stacking wrappers.
"""

from __future__ import annotations

import atexit
import builtins
import io
import os
import shutil
import subprocess
import tempfile
import unittest
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn

REAL_HOME = Path(os.path.expanduser("~")).resolve()
"""The untouched real home, captured before any sandboxing."""

GUARDED_HOME_SUBDIRS: list[Path] = [
    REAL_HOME / ".claude",
    REAL_HOME / ".config",
    REAL_HOME / ".local" / "state" / "agent-toolkit",
    REAL_HOME / ".agent-toolkit",
]
"""Real-home subtrees a guarded write may never touch.

Every path-like argument of every guarded API is resolved — relative and
``..``-laden paths against the call's ``dir_fd`` anchor when one is given
(via the platform's fd link), otherwise against the process cwd — and a
resolved landing inside any of these subtrees is denied. A symlink alias
is followed by resolution, so an alias with no ``~``/marker-shaped name
is caught the same way.

A mutable module attribute on purpose: the suite's probe test swaps in a
``tmp_path``-rooted copy to exercise every guarded API without aiming at the
developer's real state.
"""

_WRITE_MODE_CHARS = frozenset("wax+")
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT
_FD_LINK_BASES = ("/proc/self/fd", "/dev/fd")
"""Platform dirs whose ``N`` entries link an open fd's real path.

Used to resolve a ``dir_fd=``-anchored relative path against the fd's
directory. Neither existing (an exotic platform) leaves such calls
unchecked — documented in the module docstring, not a silent narrowing of
this tuple.
"""

_ACTIVE = {"subprocess": False, "paths": False}
_INSTALLED = False
_SANDBOX_HOME: Path | None = None
"""The temp HOME installed by :func:`install_sandbox_home`, if any."""


def _is_write_mode(mode: str) -> bool:
    return any(c in _WRITE_MODE_CHARS for c in mode)


def _fd_link_base() -> str | None:
    for base in _FD_LINK_BASES:
        if os.path.isdir(base):
            return base
    return None


def _resolve_path_arg(target: object, *, dir_fd: int | None = None) -> Path | None:
    """Resolve a path argument the way the kernel would, best-effort.

    Returns ``None`` for a non-path argument and for anything unresolvable
    (no fd-link platform, a symlink chain with an unreachable or looping
    component): resolution failure counts as "not denied" so the guard
    never turns an allowed call into a crash of its own.
    """
    if not isinstance(target, (str, bytes, os.PathLike)):
        return None
    s = os.fsdecode(target)
    try:
        if dir_fd is not None and not os.path.isabs(s):
            base = _fd_link_base()
            if base is None:
                return None
            return Path(os.path.join(f"{base}/{dir_fd}", s)).resolve()
        return Path(s).expanduser().resolve()
    except (OSError, RuntimeError):
        return None


def _is_guarded_target(resolved: Path | None) -> bool:
    if resolved is None:
        return False
    return any(resolved.is_relative_to(d) for d in GUARDED_HOME_SUBDIRS)


def _deny_production_path(resolved: Path) -> None:
    raise RuntimeError(
        f"blocked real filesystem access to {resolved} under the real "
        f"HOME during a test — mark the test with "
        "@pytest.mark.allow_production_paths to allow it (direct "
        "unittest runs have no opt-out; run under pytest)"
    )


@dataclass(frozen=True)
class ApiSpec:
    """One guarded mutating API: identity, which args are paths, how to probe it.

    ``args`` lists positional indices that carry paths (index 0 is ``self``
    for unbound ``Path`` methods — same semantics as the wrappers).
    ``names`` is index-aligned with ``args``: the keyword name each path
    argument accepts, or ``None`` where the argument cannot be passed by
    keyword (the ``Path`` instance itself). ``fd_kwargs`` maps a path-arg
    index to the dir-fd keyword anchoring it — one entry per fd keyword,
    since the pairing is per-API and asymmetric (``os.symlink``'s single
    ``dir_fd`` anchors *dst* only; its ``src`` is a stored link target and
    is resolved without the fd).
    ``mode_arg``/``flags_arg`` mark the extra write-intent pre-filters for
    ``open``/``os.open``, which only guard calls that actually open for
    writing. ``probe`` invokes the API with the guarded path(es) under
    ``root`` — used by the suite's registry-introspection test; it may aim
    at nonexistent paths, the guard raises before anything is touched.
    """

    qualname: str
    args: tuple[int, ...] = (0,)
    names: tuple[str | None, ...] = ()
    fd_kwargs: tuple[tuple[int, str], ...] = ()
    mode_arg: int | None = None
    flags_arg: int | None = None
    probe: Callable[[Path], object] = field(default=lambda root: None)


def _probe_open(root: Path) -> object:
    return open(root / ".claude" / "probe.txt", "w")


def _probe_os_open(root: Path) -> object:
    return os.open(str(root / ".claude" / "probe.txt"), os.O_WRONLY | os.O_CREAT)


def _probe_path_write_text(root: Path) -> object:
    return (root / ".claude" / "probe.txt").write_text("x")


def _probe_path_write_bytes(root: Path) -> object:
    return (root / ".claude" / "probe.txt").write_bytes(b"x")


def _probe_path_read(root: Path) -> object:  # pragma: no cover - never reached
    return (root / ".claude" / "probe.txt").read_text()


def _probe_path_unlink(root: Path) -> object:
    return (root / ".claude" / "probe.txt").unlink()


def _probe_path_rmdir(root: Path) -> object:
    return (root / ".claude" / "empty").rmdir()


def _probe_path_mkdir(root: Path) -> object:
    return (root / ".claude" / "sub").mkdir()


def _probe_path_rename(root: Path) -> object:
    return (root / ".claude" / "a.txt").rename(root / ".claude" / "b.txt")


def _probe_path_replace(root: Path) -> object:
    return (root / ".claude" / "a.txt").replace(root / ".claude" / "b.txt")


def _probe_path_touch(root: Path) -> object:
    return (root / ".claude" / "probe.txt").touch()


def _probe_path_chmod(root: Path) -> object:
    return (root / ".claude" / "probe.txt").chmod(0o600)


def _probe_path_symlink_to(root: Path) -> object:
    return (root / ".claude" / "link").symlink_to(root / ".claude" / "probe.txt")


def _probe_path_hardlink_to(root: Path) -> object:
    return (root / ".claude" / "link").hardlink_to(root / ".claude" / "probe.txt")


def _probe_os_remove(root: Path) -> object:
    return os.remove(root / ".claude" / "probe.txt")


def _probe_os_rmdir(root: Path) -> object:
    return os.rmdir(root / ".claude" / "empty")


def _probe_os_mkdir(root: Path) -> object:
    return os.mkdir(root / ".claude" / "sub")


def _probe_os_makedirs(root: Path) -> object:
    return os.makedirs(root / ".claude" / "sub" / "deep")


def _probe_os_rename(root: Path) -> object:
    return os.rename(root / ".claude" / "a.txt", root / ".claude" / "b.txt")


def _probe_os_replace(root: Path) -> object:
    return os.replace(root / ".claude" / "a.txt", root / ".claude" / "b.txt")


def _probe_os_link(root: Path) -> object:
    return os.link(root / ".claude" / "probe.txt", root / ".claude" / "link.txt")


def _probe_os_symlink(root: Path) -> object:
    return os.symlink(root / ".claude" / "probe.txt", root / ".claude" / "link.txt")


def _probe_os_chmod(root: Path) -> object:
    return os.chmod(root / ".claude" / "probe.txt", 0o600)


def _probe_os_utime(root: Path) -> object:
    return os.utime(root / ".claude" / "probe.txt", (0, 0))


def _probe_os_truncate(root: Path) -> object:
    return os.truncate(root / ".claude" / "probe.txt", 0)


def _probe_shutil_rmtree(root: Path) -> object:
    return shutil.rmtree(root / ".claude" / "tree")


def _probe_shutil_move(root: Path) -> object:
    return shutil.move(str(root / ".claude" / "a.txt"), str(root / ".claude" / "b.txt"))


def _probe_shutil_copy(root: Path) -> object:
    return shutil.copy(root / ".claude" / "a.txt", root / ".claude" / "b.txt")


def _probe_shutil_copyfile(root: Path) -> object:
    return shutil.copyfile(root / ".claude" / "a.txt", root / ".claude" / "b.txt")


def _probe_shutil_copytree(root: Path) -> object:
    return shutil.copytree(root / ".claude" / "tree", root / ".claude" / "tree2")


GUARDED_MUTATION_APIS: dict[str, ApiSpec] = {
    spec.qualname: spec
    for spec in (
        ApiSpec("builtins.open", mode_arg=1, probe=_probe_open),
        ApiSpec("io.open", mode_arg=1, probe=_probe_open),
        ApiSpec("os.open", flags_arg=1, probe=_probe_os_open),
        ApiSpec("pathlib.Path.write_text", probe=_probe_path_write_text),
        ApiSpec("pathlib.Path.write_bytes", probe=_probe_path_write_bytes),
        ApiSpec("pathlib.Path.unlink", probe=_probe_path_unlink),
        ApiSpec("pathlib.Path.rmdir", probe=_probe_path_rmdir),
        ApiSpec("pathlib.Path.mkdir", probe=_probe_path_mkdir),
        ApiSpec("pathlib.Path.touch", probe=_probe_path_touch),
        ApiSpec("pathlib.Path.chmod", probe=_probe_path_chmod),
        ApiSpec("pathlib.Path.rename", args=(0, 1), names=(None, "target"),
                probe=_probe_path_rename),
        ApiSpec("pathlib.Path.replace", args=(0, 1), names=(None, "target"),
                probe=_probe_path_replace),
        ApiSpec("pathlib.Path.symlink_to", args=(0, 1), names=(None, "target"),
                probe=_probe_path_symlink_to),
        ApiSpec(
            "pathlib.Path.hardlink_to", args=(0, 1), names=(None, "target"),
            probe=_probe_path_hardlink_to,
        ),
        ApiSpec("os.remove", names=("path",), fd_kwargs=((0, "dir_fd"),),
                probe=_probe_os_remove),
        ApiSpec("os.unlink", names=("path",), fd_kwargs=((0, "dir_fd"),),
                probe=_probe_os_remove),
        ApiSpec("os.rmdir", names=("path",), fd_kwargs=((0, "dir_fd"),),
                probe=_probe_os_rmdir),
        ApiSpec("os.mkdir", names=("path",), fd_kwargs=((0, "dir_fd"),),
                probe=_probe_os_mkdir),
        ApiSpec("os.makedirs", names=("name",), probe=_probe_os_makedirs),
        ApiSpec("os.rename", args=(0, 1), names=("src", "dst"),
                fd_kwargs=((0, "src_dir_fd"), (1, "dst_dir_fd")),
                probe=_probe_os_rename),
        ApiSpec("os.replace", args=(0, 1), names=("src", "dst"),
                fd_kwargs=((0, "src_dir_fd"), (1, "dst_dir_fd")),
                probe=_probe_os_replace),
        ApiSpec("os.link", args=(0, 1), names=("src", "dst"),
                fd_kwargs=((0, "src_dir_fd"), (1, "dst_dir_fd")),
                probe=_probe_os_link),
        ApiSpec("os.symlink", args=(0, 1), names=("src", "dst"),
                fd_kwargs=((1, "dir_fd"),), probe=_probe_os_symlink),
        ApiSpec("os.chmod", names=("path",), fd_kwargs=((0, "dir_fd"),),
                probe=_probe_os_chmod),
        ApiSpec("os.utime", names=("path",), fd_kwargs=((0, "dir_fd"),),
                probe=_probe_os_utime),
        ApiSpec("os.truncate", names=("path",), probe=_probe_os_truncate),
        ApiSpec("shutil.rmtree", names=("path",), probe=_probe_shutil_rmtree),
        ApiSpec("shutil.move", args=(0, 1), names=("src", "dst"),
                probe=_probe_shutil_move),
        ApiSpec("shutil.copy", args=(0, 1), names=("src", "dst"),
                probe=_probe_shutil_copy),
        ApiSpec("shutil.copyfile", args=(0, 1), names=("src", "dst"),
                probe=_probe_shutil_copyfile),
        ApiSpec("shutil.copytree", args=(0, 1), names=("src", "dst"),
                probe=_probe_shutil_copytree),
    )
}
"""Every mutating API the sandbox denies against guarded real-home subtrees.

Introspection target for the suite's coverage test; adding an entry here is
the repo's registry for "what a test may not do to the real filesystem".
"""


def _check_path_args(
    args: tuple[object, ...], kwargs: dict[str, object], path_args: ApiSpec
) -> None:
    fd_names = dict(path_args.fd_kwargs)
    for i in path_args.args:
        candidates = [args[i]] if i < len(args) else []
        name = path_args.names[i] if i < len(path_args.names) else None
        if name is not None and name in kwargs:
            candidates.append(kwargs[name])
        fd_name = fd_names.get(i)
        dir_fd = kwargs.get(fd_name) if fd_name is not None else None
        for candidate in candidates:
            resolved = _resolve_path_arg(candidate, dir_fd=dir_fd)
            if _is_guarded_target(resolved):
                assert resolved is not None
                _deny_production_path(resolved)


def _guarded(
    original: Callable[..., object], *, path_args: ApiSpec
) -> Callable[..., object]:
    def guarded(*args: object, **kwargs: object) -> object:
        if _ACTIVE["paths"]:
            _check_path_args(args, kwargs, path_args)
        return original(*args, **kwargs)

    return guarded


def _guarded_open(
    original: Callable[..., object],
) -> Callable[..., object]:
    def guarded(
        file: object, mode: str = "r", *args: object, **kwargs: object
    ) -> object:
        if (
            _ACTIVE["paths"]
            and isinstance(mode, str)
            and _is_write_mode(mode)
        ):
            resolved = _resolve_path_arg(file)
            if _is_guarded_target(resolved):
                assert resolved is not None
                _deny_production_path(resolved)
        return original(file, mode, *args, **kwargs)

    return guarded


def _guarded_os_open(
    original: Callable[..., object],
) -> Callable[..., object]:
    def guarded(path: object, flags: int, *args: object, **kwargs: object) -> object:
        if _ACTIVE["paths"] and isinstance(flags, int) and (flags & _WRITE_FLAGS):
            dir_fd = kwargs.get("dir_fd")
            for candidate in (path, kwargs.get("path")):
                resolved = _resolve_path_arg(candidate, dir_fd=dir_fd)
                if _is_guarded_target(resolved):
                    assert resolved is not None
                    _deny_production_path(resolved)
        return original(path, flags, *args, **kwargs)

    return guarded


def _guarded_popen_init(
    self: subprocess.Popen[object], *args: object, **kwargs: object
) -> None:
    if _ACTIVE["subprocess"]:
        raise RuntimeError(
            "blocked a real subprocess.Popen/run/call/check_output call "
            "during a test — mark the test with "
            "@pytest.mark.allow_real_subprocess to allow it (direct "
            "unittest runs leave subprocess unguarded)"
        )
    _ORIGINAL_POPEN_INIT(self, *args, **kwargs)


_ORIGINAL_POPEN_INIT = subprocess.Popen.__init__

_SPECIAL_WRAPPERS: dict[str, Callable[[Callable[..., object]], Callable[..., object]]] = {
    "builtins.open": _guarded_open,
    "io.open": _guarded_open,
    "os.open": _guarded_os_open,
}

# qualname -> (container object, attribute name). Static on purpose: an
# import-time resolution failure should be a loud crash, not a silent gap.
_CONTAINERS: dict[str, tuple[object, str]] = {
    "builtins.open": (builtins, "open"),
    "io.open": (io, "open"),
    "os.open": (os, "open"),
    "pathlib.Path.write_text": (Path, "write_text"),
    "pathlib.Path.write_bytes": (Path, "write_bytes"),
    "pathlib.Path.unlink": (Path, "unlink"),
    "pathlib.Path.rmdir": (Path, "rmdir"),
    "pathlib.Path.mkdir": (Path, "mkdir"),
    "pathlib.Path.rename": (Path, "rename"),
    "pathlib.Path.replace": (Path, "replace"),
    "pathlib.Path.touch": (Path, "touch"),
    "pathlib.Path.chmod": (Path, "chmod"),
    "pathlib.Path.symlink_to": (Path, "symlink_to"),
    "pathlib.Path.hardlink_to": (Path, "hardlink_to"),
    "os.remove": (os, "remove"),
    "os.unlink": (os, "unlink"),
    "os.rmdir": (os, "rmdir"),
    "os.mkdir": (os, "mkdir"),
    "os.makedirs": (os, "makedirs"),
    "os.rename": (os, "rename"),
    "os.replace": (os, "replace"),
    "os.link": (os, "link"),
    "os.symlink": (os, "symlink"),
    "os.chmod": (os, "chmod"),
    "os.utime": (os, "utime"),
    "os.truncate": (os, "truncate"),
    "shutil.rmtree": (shutil, "rmtree"),
    "shutil.move": (shutil, "move"),
    "shutil.copy": (shutil, "copy"),
    "shutil.copyfile": (shutil, "copyfile"),
    "shutil.copytree": (shutil, "copytree"),
}


def scrub_git_config_env() -> None:
    """Clear harness-injected GIT_CONFIG_* env vars from os.environ.

    Harnesses like Copilot CLI inject GIT_CONFIG_COUNT, GIT_CONFIG_KEY_*,
    GIT_CONFIG_VALUE_*, and GIT_CONFIG_PARAMETERS into the environment
    (e.g. safe.bareRepository=explicit, credential.interactive=never,
    core.fsmonitor=). The test sandbox isolates HOME but inherits these,
    causing git queries on bare repos or submodules to fail.
    """
    for key in list(os.environ):
        if key in ("GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS") or key.startswith(
            ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
        ):
            os.environ.pop(key, None)


def install_sandbox_home() -> Path:
    """Redirect ``HOME`` to a throwaway directory for this process.

    Idempotent: a second call is a no-op returning the existing sandbox.
    Cleanup is registered with :func:`atexit`.
    """
    global _SANDBOX_HOME
    if _SANDBOX_HOME is not None:
        return _SANDBOX_HOME
    sandbox = Path(tempfile.mkdtemp(prefix="agent-toolkit-test-home-"))
    os.environ["HOME"] = str(sandbox)
    os.environ.pop("AGENT_TOOLKIT_HOME", None)
    scrub_git_config_env()
    atexit.register(shutil.rmtree, str(sandbox), ignore_errors=True)
    _SANDBOX_HOME = sandbox
    return sandbox


# qualname -> original callable, captured at this module's import (before
# any patching). Frozen here so install_patches never wraps a wrapper.
_ORIGINALS: dict[str, Callable[..., object]] = {
    "builtins.open": builtins.open,

    "io.open": io.open,
    "os.open": os.open,
    "pathlib.Path.write_text": Path.write_text,
    "pathlib.Path.write_bytes": Path.write_bytes,
    "pathlib.Path.unlink": Path.unlink,
    "pathlib.Path.rmdir": Path.rmdir,
    "pathlib.Path.mkdir": Path.mkdir,
    "pathlib.Path.rename": Path.rename,
    "pathlib.Path.replace": Path.replace,
    "pathlib.Path.touch": Path.touch,
    "pathlib.Path.chmod": Path.chmod,
    "pathlib.Path.symlink_to": Path.symlink_to,
    "pathlib.Path.hardlink_to": Path.hardlink_to,
    "os.remove": os.remove,
    "os.unlink": os.unlink,
    "os.rmdir": os.rmdir,
    "os.mkdir": os.mkdir,
    "os.makedirs": os.makedirs,
    "os.rename": os.rename,
    "os.replace": os.replace,
    "os.link": os.link,
    "os.symlink": os.symlink,
    "os.chmod": os.chmod,
    "os.utime": os.utime,
    "os.truncate": os.truncate,
    "shutil.rmtree": shutil.rmtree,
    "shutil.move": shutil.move,
    "shutil.copy": shutil.copy,
    "shutil.copyfile": shutil.copyfile,
    "shutil.copytree": shutil.copytree,
}


def install_patches() -> None:
    """Wrap every API in :data:`GUARDED_MUTATION_APIS` plus ``Popen``.

    Installed wrappers check the :data:`_ACTIVE` flags at call time, so this
    is safe to run early; enforcement begins only once :func:`activate` (or
    a fixture via :func:`guards`) turns a flag on. Idempotent within the
    process; ``importlib.reload`` of this module is unsupported (see the
    module docstring) because it would stack wrappers.
    """
    global _INSTALLED
    if _INSTALLED:
        return
    subprocess.Popen.__init__ = _guarded_popen_init  # type: ignore[method-assign]
    for qualname, spec in GUARDED_MUTATION_APIS.items():
        container, attr = _CONTAINERS[qualname]
        original = _ORIGINALS[qualname]
        special = _SPECIAL_WRAPPERS.get(qualname)
        if special is not None:
            wrapped = special(original)
        else:
            wrapped = _guarded(original, path_args=spec)
        setattr(container, attr, wrapped)
    _INSTALLED = True


def bootstrap() -> None:
    """One call per test process: sandbox HOME + install guards (still off)."""
    install_sandbox_home()
    install_patches()


def activate(*, subprocess: bool, paths: bool) -> None:  # noqa: A002
    """Set both guard flags directly (the direct-unittest entrypoint's model:
    one run, one policy)."""
    _ACTIVE["subprocess"] = subprocess
    _ACTIVE["paths"] = paths


@contextmanager
def guards(**flags: bool) -> Iterator[None]:
    """Temporarily set the given guard flags, restoring their previous values.

    Accepts any subset of ``subprocess=`` / ``paths=``; untouched flags keep
    their current value. This subset semantics is load-bearing: pytest's two
    autouse fixtures each manage one flag independently, and a test marked
    with one allow-marker must not thereby lose the other guard.
    """
    bad = set(flags) - set(_ACTIVE)
    if bad:
        raise TypeError(f"unknown guard flags: {sorted(bad)}")
    prev = {key: _ACTIVE[key] for key in flags}
    _ACTIVE.update(flags)
    try:
        yield
    finally:
        _ACTIVE.update(prev)


def run_unittest_main(**kwargs: object) -> NoReturn:
    """The direct-entrypoint hook: ``python3 test_X.py`` ends with this.

    Bootstraps (idempotent — a no-op if ``conftest.py`` already did it under
    pytest), activates the guards for the whole run — path guard ON, real
    subprocess allowed (plain unittest has no marker machinery; see the
    module docstring) — then hands everything through to
    :func:`unittest.main`, so ``verbosity=`` and argv passthrough keep
    working verbatim.
    """
    bootstrap()
    activate(subprocess=False, paths=True)
    unittest.main(**kwargs)


bootstrap()

