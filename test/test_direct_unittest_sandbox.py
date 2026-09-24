#!/usr/bin/env python3
"""Tests proving direct python3 test_X.py entrypoints are sandboxed."""

import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

import pytest

import test_bootstrap

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.allow_real_subprocess
def test_direct_run_redirects_home(tmp_path):
    driver = tmp_path / "driver_home.py"
    driver.write_text(
        "import os, sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(REPO_ROOT / 'agent-scripts')!r})\n"
        "import test_bootstrap\n"
        "assert Path.home() != test_bootstrap.REAL_HOME\n"
        "assert os.environ['HOME'] != str(test_bootstrap.REAL_HOME)\n"
    )
    env = os.environ.copy()
    env["HOME"] = str(test_bootstrap.REAL_HOME)
    result = subprocess.run(
        [sys.executable, str(driver)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"stdout: {result.stdout}, stderr: {result.stderr}"


@pytest.mark.allow_real_subprocess
def test_direct_run_path_guard_blocks_real_home_write(tmp_path):
    driver = tmp_path / "driver_write.py"
    probe_target = test_bootstrap.REAL_HOME / ".claude" / f"sandbox-probe-{os.getpid()}.txt"
    driver.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(REPO_ROOT / 'agent-scripts')!r})\n"
        "import test_bootstrap\n"
        "test_bootstrap.activate(subprocess=False, paths=True)\n"
        f"open({str(probe_target)!r}, 'w').close()\n"
    )
    env = os.environ.copy()
    env["HOME"] = str(test_bootstrap.REAL_HOME)
    result = subprocess.run(
        [sys.executable, str(driver)],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "allow_production_paths" in result.stderr
    assert not probe_target.exists()


@pytest.mark.allow_real_subprocess
def test_direct_run_sample_entrypoints():
    # 1. Pytest-free entrypoint: test_analyze_sessions.py
    target1 = REPO_ROOT / "test" / "test_analyze_sessions.py"
    result1 = subprocess.run(
        [sys.executable, str(target1)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result1.returncode == 0, f"target1 failed: {result1.stderr}"

    # 2. Heavy real entrypoint: test_dev_status.py, one store-touching
    #    test selected through unittest's argv passthrough -- proves the same
    #    bootstrap/sandbox path without re-running the whole file serially.
    target2 = REPO_ROOT / "test" / "test_dev_status.py"
    result2 = subprocess.run(
        [
            sys.executable,
            str(target2),
            "BacklogTestCase.test_backlog_lock_reentrant_same_thread_does_not_deadlock",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result2.returncode == 0, f"target2 failed: {result2.stderr}"
    assert "Ran 1 test" in result2.stderr, (
        f"selection did not run exactly one test: {result2.stderr}"
    )


NEW_GUARDED_APIS = [
    "pathlib.Path.touch",
    "pathlib.Path.chmod",
    "pathlib.Path.symlink_to",
    "pathlib.Path.hardlink_to",
    "os.link",
    "os.symlink",
    "os.chmod",
    "os.utime",
    "os.truncate",
    "shutil.move",
    "shutil.copy",
    "shutil.copyfile",
    "shutil.copytree",
]


@pytest.mark.regression(
    "sandbox-guards-new-mutation-apis",
    "Failed: DID NOT RAISE RuntimeError",
)
@pytest.mark.parametrize("api_name", NEW_GUARDED_APIS)
def test_newly_covered_mutation_apis_raise_guard_error(tmp_path, api_name):
    fake_home = tmp_path / "fake_home"
    fake_claude = fake_home / ".claude"
    fake_claude.mkdir(parents=True)

    orig_subdirs = list(test_bootstrap.GUARDED_HOME_SUBDIRS)
    test_bootstrap.GUARDED_HOME_SUBDIRS[:] = [fake_claude]
    try:
        with test_bootstrap.guards(paths=True):
            spec = test_bootstrap.GUARDED_MUTATION_APIS[api_name]
            with pytest.raises(RuntimeError, match="allow_production_paths"):
                spec.probe(fake_home)
    finally:
        test_bootstrap.GUARDED_HOME_SUBDIRS[:] = orig_subdirs


def test_guarded_mutation_apis_probe_delegation_when_inactive(tmp_path):
    fake_home = tmp_path / "fake_home"
    fake_claude = fake_home / ".claude"
    fake_claude.mkdir(parents=True)

    # Setup files/directories so probes don't crash when underlying function is called
    (fake_claude / "probe.txt").write_text("probe")
    (fake_claude / "a.txt").write_text("a")
    (fake_claude / "empty").mkdir()
    (fake_claude / "tree").mkdir()

    orig_subdirs = list(test_bootstrap.GUARDED_HOME_SUBDIRS)
    test_bootstrap.GUARDED_HOME_SUBDIRS[:] = [fake_claude]
    try:
        with test_bootstrap.guards(paths=False):
            for name, spec in test_bootstrap.GUARDED_MUTATION_APIS.items():
                # Clean up between probes
                (fake_claude / "probe.txt").touch()
                (fake_claude / "a.txt").touch()
                (fake_claude / "link").unlink(missing_ok=True)
                (fake_claude / "link.txt").unlink(missing_ok=True)
                (fake_claude / "b.txt").unlink(missing_ok=True)
                shutil.rmtree(fake_claude / "tree2", ignore_errors=True)
                if not (fake_claude / "empty").exists():
                    (fake_claude / "empty").mkdir()
                if not (fake_claude / "tree").exists():
                    (fake_claude / "tree").mkdir()

                try:
                    spec.probe(fake_home)
                except RuntimeError as e:
                    if "allow_production_paths" in str(e):
                        pytest.fail(f"API {name} raised guard RuntimeError under paths=False")
                except Exception:
                    # Non-guard exceptions (e.g. underlying OS errors if unsupported) are fine
                    pass
    finally:
        test_bootstrap.GUARDED_HOME_SUBDIRS[:] = orig_subdirs


def test_toolkit_home_is_in_the_guarded_set():
    assert test_bootstrap.REAL_HOME / ".agent-toolkit" in test_bootstrap.GUARDED_HOME_SUBDIRS


@pytest.mark.regression(
    "sandbox-guard-blocks-toolkit-home-write",
    "Failed: DID NOT RAISE RuntimeError",
)
def test_toolkit_home_write_blocked_by_resolution():
    # A fake guarded root, never the real one, so a red run cannot touch
    # real state. The fake root's name carries no marker-substring, and the
    # probe path avoids any marker-shaped component, so the block can only
    # come from resolution landing inside the guarded root itself.
    root = Path(tempfile.mkdtemp(prefix="guard-probe-"))
    fake_toolkit = root / ".agent-toolkit"
    fake_toolkit.mkdir()
    target = fake_toolkit / "probe.txt"
    assert not any(m in str(target) for m in (".claude", ".config", ".local/state")), target

    orig_subdirs = list(test_bootstrap.GUARDED_HOME_SUBDIRS)
    test_bootstrap.GUARDED_HOME_SUBDIRS[:] = [fake_toolkit.resolve()]
    try:
        with test_bootstrap.guards(paths=True):
            with pytest.raises(RuntimeError, match="allow_production_paths"):
                target.write_text("should never land on disk")
        assert not target.exists()
    finally:
        test_bootstrap.GUARDED_HOME_SUBDIRS[:] = orig_subdirs
        shutil.rmtree(root, ignore_errors=True)


# --- Path-resolution gap probes (fake guarded roots, never the real one) ---
# Every fake guarded root below is rooted at a name with no marker substring
# (".claude", ".config", ".local/state"), and every probe path avoids
# marker-shaped components: a denial can only come from the guard resolving
# the argument into the guarded root, not from a raw-string marker.


def _swap_guarded_root(guarded: Path) -> list[Path]:
    orig = list(test_bootstrap.GUARDED_HOME_SUBDIRS)
    test_bootstrap.GUARDED_HOME_SUBDIRS[:] = [guarded.resolve()]
    return orig


@pytest.mark.regression(
    "sandbox-guard-blocks-relative-into-guarded-root",
    "Failed: DID NOT RAISE RuntimeError",
)
@pytest.mark.parametrize("encode", [lambda s: s, os.fsencode], ids=["str", "bytes"])
def test_relative_path_into_guarded_root_denied(
    tmp_path, monkeypatch, encode: Callable[[str], object]
):
    guarded = tmp_path / "guarded"
    guarded.mkdir()
    monkeypatch.chdir(guarded)

    orig = _swap_guarded_root(guarded)
    try:
        with test_bootstrap.guards(paths=True):
            with pytest.raises(RuntimeError, match="allow_production_paths"):
                open(encode("probe.txt"), "w")
        assert not (guarded / "probe.txt").exists()
    finally:
        test_bootstrap.GUARDED_HOME_SUBDIRS[:] = orig


@pytest.mark.regression(
    "sandbox-guard-blocks-dotdot-into-guarded-root",
    "Failed: DID NOT RAISE RuntimeError",
)
def test_dotdot_relative_path_into_guarded_root_denied(tmp_path, monkeypatch):
    guarded = tmp_path / "guarded"
    (guarded / "sub").mkdir(parents=True)
    monkeypatch.chdir(guarded / "sub")

    orig = _swap_guarded_root(guarded)
    try:
        with test_bootstrap.guards(paths=True):
            with pytest.raises(RuntimeError, match="allow_production_paths"):
                open("a/../probe.txt", "w")
        assert not (guarded / "probe.txt").exists()
    finally:
        test_bootstrap.GUARDED_HOME_SUBDIRS[:] = orig


@pytest.mark.regression(
    "sandbox-guard-blocks-symlink-alias-into-guarded-root",
    "Failed: DID NOT RAISE RuntimeError",
)
def test_symlink_alias_into_guarded_root_denied(tmp_path):
    guarded = tmp_path / "guarded"
    guarded.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(guarded)

    orig = _swap_guarded_root(guarded)
    try:
        with test_bootstrap.guards(paths=True):
            with pytest.raises(RuntimeError, match="allow_production_paths"):
                open(str(alias / "probe.txt"), "w")
        assert not (guarded / "probe.txt").exists()
    finally:
        test_bootstrap.GUARDED_HOME_SUBDIRS[:] = orig


_FD_LINK_BASE = "/proc/self/fd" if os.path.isdir("/proc/self/fd") else (
    "/dev/fd" if os.path.isdir("/dev/fd") else None
)


@pytest.mark.skipif(
    _FD_LINK_BASE is None, reason="no platform fd link to resolve a dir_fd through"
)
@pytest.mark.regression(
    "sandbox-guard-blocks-dir-fd-into-guarded-root",
    "Failed: DID NOT RAISE RuntimeError",
)
def test_dir_fd_write_into_guarded_root_denied(tmp_path):
    guarded = tmp_path / "guarded"
    guarded.mkdir()
    fd = os.open(guarded, os.O_RDONLY)
    try:
        orig = _swap_guarded_root(guarded)
        try:
            with test_bootstrap.guards(paths=True):
                with pytest.raises(RuntimeError, match="allow_production_paths"):
                    os.open("probe.txt", os.O_WRONLY | os.O_CREAT, dir_fd=fd)
            assert not (guarded / "probe.txt").exists()
        finally:
            test_bootstrap.GUARDED_HOME_SUBDIRS[:] = orig
    finally:
        os.close(fd)


def _keyword_probe(guarded: Path, qualname: str) -> None:
    """Invoke one generic-wrapper API with its path args by keyword.

    Source files the underlying call needs are pre-created inside the fake
    guarded root so a red run exercises the real call (and lands in tmp,
    never real state).
    """
    (guarded / "probe.txt").write_text("probe")
    (guarded / "a.txt").write_text("a")
    (guarded / "empty").mkdir()
    (guarded / "tree").mkdir()
    probes = {
        "os.remove": lambda: os.remove(path=str(guarded / "probe.txt")),
        "os.unlink": lambda: os.unlink(path=str(guarded / "probe.txt")),
        "os.rmdir": lambda: os.rmdir(path=str(guarded / "empty")),
        "os.mkdir": lambda: os.mkdir(path=str(guarded / "sub")),
        "os.makedirs": lambda: os.makedirs(name=str(guarded / "deep")),
        "os.rename": lambda: os.rename(
            src=str(guarded / "a.txt"), dst=str(guarded / "b.txt")
        ),
        "os.replace": lambda: os.replace(
            src=str(guarded / "a.txt"), dst=str(guarded / "b2.txt")
        ),
        "os.link": lambda: os.link(
            src=str(guarded / "probe.txt"), dst=str(guarded / "link.txt")
        ),
        "os.symlink": lambda: os.symlink(
            src=str(guarded / "probe.txt"), dst=str(guarded / "sym.txt")
        ),
        "os.chmod": lambda: os.chmod(path=str(guarded / "probe.txt"), mode=0o600),
        "os.utime": lambda: os.utime(path=str(guarded / "probe.txt")),
        "os.truncate": lambda: os.truncate(
            path=str(guarded / "probe.txt"), length=0
        ),
        "shutil.rmtree": lambda: shutil.rmtree(path=str(guarded / "tree")),
        "shutil.move": lambda: shutil.move(
            src=str(guarded / "a.txt"), dst=str(guarded / "mv.txt")
        ),
        "shutil.copy": lambda: shutil.copy(
            src=str(guarded / "a.txt"), dst=str(guarded / "cp.txt")
        ),
        "shutil.copyfile": lambda: shutil.copyfile(
            src=str(guarded / "a.txt"), dst=str(guarded / "cf.txt")
        ),
        "shutil.copytree": lambda: shutil.copytree(
            src=str(guarded / "tree"), dst=str(guarded / "tree2")
        ),
    }
    probes[qualname]()


_KEYWORD_APIS = tuple(
    name
    for name in test_bootstrap.GUARDED_MUTATION_APIS
    if name in {
        "os.remove", "os.unlink", "os.rmdir", "os.mkdir", "os.makedirs",
        "os.rename", "os.replace", "os.link", "os.symlink", "os.chmod",
        "os.utime", "os.truncate", "shutil.rmtree", "shutil.move",
        "shutil.copy", "shutil.copyfile", "shutil.copytree",
    }
)


@pytest.mark.regression(
    "sandbox-guard-blocks-keyword-path-args",
    "Failed: DID NOT RAISE RuntimeError",
)
@pytest.mark.parametrize("api_name", _KEYWORD_APIS)
def test_keyword_path_args_denied(tmp_path, api_name):
    guarded = tmp_path / "guarded"
    guarded.mkdir()

    orig = _swap_guarded_root(guarded)
    try:
        with test_bootstrap.guards(paths=True):
            with pytest.raises(RuntimeError, match="allow_production_paths"):
                _keyword_probe(guarded, api_name)
    finally:
        test_bootstrap.GUARDED_HOME_SUBDIRS[:] = orig


@pytest.mark.regression(
    "sandbox-guard-blocks-keyword-target-args",
    "Failed: DID NOT RAISE RuntimeError",
)
@pytest.mark.parametrize(
    "api_name",
    [
        "pathlib.Path.rename",
        "pathlib.Path.replace",
        "pathlib.Path.symlink_to",
        "pathlib.Path.hardlink_to",
    ],
)
def test_path_keyword_target_args_denied(tmp_path, api_name):
    guarded = tmp_path / "guarded"
    guarded.mkdir()
    (guarded / "probe.txt").write_text("probe")
    self_path = tmp_path / "outside.txt"
    self_path.write_text("self")
    spec = test_bootstrap.GUARDED_MUTATION_APIS[api_name]
    target = guarded / "target.txt"

    orig = _swap_guarded_root(guarded)
    try:
        with test_bootstrap.guards(paths=True):
            with pytest.raises(RuntimeError, match="allow_production_paths"):
                getattr(self_path, spec.qualname.split(".")[-1])(target=target)
    finally:
        test_bootstrap.GUARDED_HOME_SUBDIRS[:] = orig
