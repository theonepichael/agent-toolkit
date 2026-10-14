# rename-transition: remove after the clean-break renames
"""Tests for agent-scripts/rename_transition.py, the clean-break rename groundwork.

Temp homes, temp stores and a monkeypatched environment throughout; the
migration lock file lives under an isolated XDG_STATE_HOME.
"""

import json
import os
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agent-scripts"))

import migration_lock  # noqa: E402
import rename_transition as rt  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    state = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    migration_lock._reset_for_tests()
    rt._reset_warnings_for_tests()
    yield state
    migration_lock._reset_for_tests()


ROWS = (
    rt.LegacyEnv("OLD_TOOL_HOME", "NEW_TOOL_HOME", "release-9"),
    *rt.family(
        "OLD_TOOL_{X}_MODEL", "NEW_TOOL_{X}_MODEL", ("CODEX", "PI"), "release-9"
    ),
)


# ── env() ─────────────────────────────────────────────────────────────────────


def test_env_prefers_new_name(capsys):
    environ = {"NEW_TOOL_HOME": "new", "OLD_TOOL_HOME": "old"}
    assert rt.env("NEW_TOOL_HOME", environ=environ, rows=ROWS) == "new"
    assert capsys.readouterr().err == ""


def test_env_falls_back_to_old_with_one_warning_per_process(capsys):
    environ = {"OLD_TOOL_HOME": "old"}
    assert rt.env("NEW_TOOL_HOME", environ=environ, rows=ROWS) == "old"
    assert rt.env("NEW_TOOL_HOME", environ=environ, rows=ROWS) == "old"
    err = capsys.readouterr().err
    assert err.count("OLD_TOOL_HOME is deprecated") == 1
    assert "rename it to NEW_TOOL_HOME" in err
    assert "release-9" in err


def test_env_empty_string_counts_as_set():
    assert rt.env("NEW_TOOL_HOME", "d", environ={"OLD_TOOL_HOME": ""}, rows=ROWS) == ""


def test_env_default_when_neither_set():
    assert rt.env("NEW_TOOL_HOME", "d", environ={}, rows=ROWS) == "d"


def test_env_unmapped_name_returns_default():
    assert rt.env("UNRELATED", "d", environ={"OLD_TOOL_HOME": "x"}, rows=ROWS) == "d"


def test_family_generates_explicit_rows():
    fam = rt.family("A_{X}_B", "C_{X}_D", ("P", "Q"), "release-1")
    assert [(r.old, r.new) for r in fam] == [("A_P_B", "C_P_D"), ("A_Q_B", "C_Q_D")]


def test_family_requires_placeholder_in_both_templates():
    with pytest.raises(ValueError):
        rt.family("A_B", "C_{X}_D", ("P",), "release-1")


def test_env_family_row_falls_back():
    environ = {"OLD_TOOL_PI_MODEL": "m"}
    assert rt.env("NEW_TOOL_PI_MODEL", environ=environ, rows=ROWS) == "m"


def test_shipped_table_validates():
    rt.validate_legacy_env()


@pytest.mark.parametrize(
    "rows",
    [
        (rt.LegacyEnv("A", "B", "r"), rt.LegacyEnv("C", "B", "r")),  # dup new
        (rt.LegacyEnv("A", "B", "r"), rt.LegacyEnv("A", "C", "r")),  # dup old
        (rt.LegacyEnv("A", "B", "r"), rt.LegacyEnv("B", "C", "r")),  # chain
        (rt.LegacyEnv("A", "A", "r"),),  # self-map
    ],
)
def test_validate_legacy_env_rejects_conflicts(rows):
    with pytest.raises(ValueError):
        rt.validate_legacy_env(rows)


# ── report ────────────────────────────────────────────────────────────────────


def test_legacy_env_report_lists_set_old_names_sorted():
    environ = {"OLD_TOOL_PI_MODEL": "x", "OLD_TOOL_HOME": "y", "NEW_TOOL_HOME": "z"}
    assert rt.legacy_env_report(environ, rows=ROWS) == [
        ("OLD_TOOL_HOME", "NEW_TOOL_HOME"),
        ("OLD_TOOL_PI_MODEL", "NEW_TOOL_PI_MODEL"),
    ]


def test_report_cli_default_mode(capsys):
    assert rt.report_main(session=False, environ={}, rows=ROWS) == 0
    assert "no legacy environment variables set" in capsys.readouterr().out
    assert rt.report_main(session=False, environ={"OLD_TOOL_HOME": "1"}, rows=ROWS) == 1
    assert "OLD_TOOL_HOME -> NEW_TOOL_HOME" in capsys.readouterr().out


def test_report_cli_session_mode_is_silent_or_one_line(capsys):
    assert rt.report_main(session=True, environ={}, rows=ROWS) == 0
    assert capsys.readouterr().out == ""
    environ = {"OLD_TOOL_HOME": "1", "OLD_TOOL_CODEX_MODEL": "2"}
    assert rt.report_main(session=True, environ=environ, rows=ROWS) == 0
    out = capsys.readouterr().out
    assert out.count("\n") == 1
    assert out.startswith("legacy env vars set: ")
    assert "OLD_TOOL_CODEX_MODEL -> NEW_TOOL_CODEX_MODEL" in out


# ── store-transform framework ────────────────────────────────────────────────


class JsonAdapter:
    """Test adapter: one JSON file per item plus a rotated .1 sibling.

    Version 1 is the unmarked shape (a bare list); later versions are
    ``{"v": n, "items": [...]}``.
    """

    name = "teststore"
    current_version = 3
    supported_versions = frozenset({1, 2})

    def discover(self, home: Path) -> list[Path]:
        root = home / "store"
        if not root.is_dir():
            return []
        return sorted(p for p in root.iterdir() if p.is_file())

    def version_of(self, data: bytes) -> int:
        obj = json.loads(data)
        if isinstance(obj, list):
            return 1
        if isinstance(obj, dict) and isinstance(obj.get("v"), int):
            return obj["v"]
        raise rt.StoreFormatError("unrecognised teststore shape")

    def load(self, data: bytes) -> object:
        obj = json.loads(data)
        return obj if isinstance(obj, list) else obj["items"]

    def dump(self, obj: object, version: int) -> bytes:
        if version == 1:
            return json.dumps(obj).encode()
        return json.dumps({"v": version, "items": obj}).encode()


def _upper(items: object) -> object:
    return [str(i).upper() for i in items]  # type: ignore[union-attr]


def _suffix(items: object) -> object:
    return [f"{i}!" for i in items]  # type: ignore[union-attr]


ADAPTERS = (JsonAdapter(),)
TRANSFORMS = (
    rt.Transform("teststore", 1, 2, _upper, "uppercase items"),
    rt.Transform("teststore", 2, 3, _suffix, "suffix items"),
)


def _write(home: Path, name: str, obj: object, mode: int = 0o600) -> Path:
    path = home / "store" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj))
    path.chmod(mode)
    return path


def _run(home: Path, *, dry_run: bool = False, **kw) -> list[str]:
    return rt.run_transforms(
        home,
        dry_run=dry_run,
        adapters=kw.get("adapters", ADAPTERS),
        transforms=kw.get("transforms", TRANSFORMS),
    )


def test_shipped_registry_is_empty_and_valid():
    assert rt.ADAPTERS == ()
    assert rt.TRANSFORMS == ()
    rt.validate_registry()


def test_empty_registry_is_a_noop(tmp_path):
    assert rt.run_transforms(tmp_path, dry_run=False) == []
    assert rt.pending_transforms(tmp_path) == []


def test_run_transforms_brings_every_file_to_current(tmp_path):
    a = _write(tmp_path, "a.json", ["x", "y"])
    b = _write(tmp_path, "a.json.1", {"v": 2, "items": ["Q"]}, mode=0o640)
    lines = _run(tmp_path)
    assert json.loads(a.read_text()) == {"v": 3, "items": ["X!", "Y!"]}
    assert json.loads(b.read_text()) == {"v": 3, "items": ["Q!"]}
    assert stat.S_IMODE(a.stat().st_mode) == 0o600
    assert stat.S_IMODE(b.stat().st_mode) == 0o640
    assert any("a.json" in line for line in lines)


def test_second_run_is_noop_and_takes_no_lock(tmp_path, isolated_state):
    _write(tmp_path, "a.json", ["x"])
    _run(tmp_path)
    lock = migration_lock.lock_path()
    if lock.exists():
        lock.unlink()
    assert _run(tmp_path) == []
    assert not lock.exists()


def test_dry_run_writes_nothing_and_creates_no_lock(tmp_path):
    a = _write(tmp_path, "a.json", ["x"])
    before = a.read_bytes()
    lines = _run(tmp_path, dry_run=True)
    assert a.read_bytes() == before
    assert lines and all(line.startswith("would apply") for line in lines)
    assert not migration_lock.lock_path().exists()


def test_newer_version_errors_even_when_nothing_pending(tmp_path):
    _write(tmp_path, "a.json", {"v": 9, "items": []})
    with pytest.raises(rt.TransformError, match="a.json"):
        _run(tmp_path)


def test_unsupported_old_version_errors(tmp_path):
    _write(tmp_path, "a.json", {"v": 0, "items": []})
    with pytest.raises(rt.TransformError, match="a.json"):
        _run(tmp_path)


def test_corrupt_file_errors_and_names_it(tmp_path):
    path = tmp_path / "store" / "bad.json"
    path.parent.mkdir(parents=True)
    path.write_text("{not json")
    with pytest.raises(rt.TransformError, match="bad.json"):
        _run(tmp_path)


def test_interrupted_run_is_finished_by_rerun(tmp_path):
    a = _write(tmp_path, "a.json", ["x"])
    b = _write(tmp_path, "b.json", ["y"])
    calls = {"n": 0}

    def flaky(items: object) -> object:
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("boom")
        return _upper(items)

    transforms = (rt.Transform("teststore", 1, 2, flaky, "flaky"), TRANSFORMS[1])
    with pytest.raises(rt.TransformError):
        _run(tmp_path, transforms=transforms)
    assert json.loads(a.read_text())["v"] == 3
    assert json.loads(b.read_text()) == ["y"]
    _run(tmp_path)
    assert json.loads(b.read_text()) == {"v": 3, "items": ["Y!"]}


def test_dump_that_drops_the_version_is_rejected_and_writes_nothing(tmp_path):
    class Lossy(JsonAdapter):
        def dump(self, obj: object, version: int) -> bytes:
            return json.dumps(obj).encode()  # forgets to mark the version

    a = _write(tmp_path, "a.json", ["x"])
    before = a.read_bytes()
    with pytest.raises(rt.TransformError, match="version"):
        _run(tmp_path, adapters=(Lossy(),))
    assert a.read_bytes() == before


def test_vanished_file_is_skipped(tmp_path, monkeypatch):
    _write(tmp_path, "a.json", ["x"])
    ghost = tmp_path / "store" / "ghost.json"
    adapter = JsonAdapter()
    original = adapter.discover
    monkeypatch.setattr(adapter, "discover", lambda home: [*original(home), ghost])
    _run(tmp_path, adapters=(adapter,))


@pytest.mark.parametrize(
    "transforms",
    [
        (rt.Transform("teststore", 1, 2, _upper, "a"),),  # gap 2->3
        (*TRANSFORMS, rt.Transform("teststore", 2, 3, _upper, "dup")),
        (*TRANSFORMS, rt.Transform("nostore", 1, 2, _upper, "x")),
        (rt.Transform("teststore", 1, 3, _upper, "skip"), TRANSFORMS[1]),
    ],
)
def test_validate_registry_rejects_bad_chains(transforms):
    with pytest.raises(ValueError):
        rt.validate_registry(ADAPTERS, transforms)


def test_validate_registry_accepts_good_chain():
    rt.validate_registry(ADAPTERS, TRANSFORMS)


@pytest.mark.allow_real_subprocess
def test_transform_holds_exclusive_lock_and_writer_is_refused(tmp_path, isolated_state):
    """A toolkit writer running mid-transform is refused with exit 75."""
    _write(tmp_path, "a.json", ["x"])
    entered = threading.Event()
    release = threading.Event()

    def blocking(items: object) -> object:
        entered.set()
        assert release.wait(30)
        return _upper(items)

    transforms = (rt.Transform("teststore", 1, 2, blocking, "block"), TRANSFORMS[1])
    errors: list[BaseException] = []

    def runner() -> None:
        try:
            _run(tmp_path, transforms=transforms)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    thread = threading.Thread(target=runner)
    thread.start()
    try:
        assert entered.wait(30)
        writer = _writer_subprocess(tmp_path, isolated_state)
    finally:
        release.set()
        thread.join(30)
    assert not errors
    assert writer.returncode == 75, writer.stdout + writer.stderr


def _writer_subprocess(tmp_path: Path, state: Path) -> subprocess.CompletedProcess:
    home = tmp_path / "writer-home"
    home.mkdir(exist_ok=True)
    env = {
        **os.environ,
        "HOME": str(home),
        "XDG_STATE_HOME": str(state),
        "DEVSTATUS_AGENT": "1",
    }
    payload = json.dumps(
        {"id": "atk-lock-probe", "summary": "probe", "category": "chore"}
    )
    return subprocess.run(
        [sys.executable, str(REPO / "agent-scripts" / "dev_status.py"), "add", payload],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


# ── checklist ─────────────────────────────────────────────────────────────────


def test_checklist_lists_ledger_items_and_hits(tmp_path):
    f = tmp_path / "CLAUDE.md"
    f.write_text("intro\nrun dev_status.py show\n")
    missing = tmp_path / "absent.md"
    out = rt.checklist("dev_status", "work_tracker", "release-9", files=[f, missing])
    assert "- [ ] Defining source file renamed" in out
    assert f"{f}:2" in out
    assert "dev_status.py show" in out
    assert f"{missing}: not present" in out


# ── removal tag ───────────────────────────────────────────────────────────────


def test_every_file_touching_the_transition_carries_the_removal_tag():
    tracked = _repo_files()
    offenders = []
    for rel in tracked:
        path = REPO / rel
        if path.suffix not in {".py", ".sh", ".md", ".toml", ".ts", ".json"}:
            continue
        if rel in {"INTERFACES.md", "CHANGELOG.md"} or rel.startswith("docs/"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "rename_transition" in text and rt.REMOVAL_TAG not in text:
            offenders.append(rel)
    assert offenders == []


def _repo_files() -> list[str]:
    """Every repo file outside VCS, venv and cache directories."""
    out = []
    for path in REPO.rglob("*"):
        rel = path.relative_to(REPO).as_posix()
        if any(
            part in {".git", ".venv", "node_modules", "__pycache__"}
            for part in path.parts
        ):
            continue
        if path.is_file():
            out.append(rel)
    return out


def test_module_carries_the_removal_tag():
    text = (REPO / "agent-scripts" / "rename_transition.py").read_text()
    assert rt.REMOVAL_TAG in text


def test_adapter_load_error_becomes_transform_error(tmp_path):
    class BadLoad(JsonAdapter):
        def load(self, data: bytes) -> object:
            raise KeyError("items")

    _write(tmp_path, "a.json", ["x"])
    with pytest.raises(rt.TransformError, match="a.json"):
        _run(tmp_path, adapters=(BadLoad(),))


def test_adapter_dump_error_becomes_transform_error(tmp_path):
    class BadDump(JsonAdapter):
        def dump(self, obj: object, version: int) -> bytes:
            raise TypeError("not serialisable")

    _write(tmp_path, "a.json", ["x"])
    with pytest.raises(rt.TransformError, match="a.json"):
        _run(tmp_path, adapters=(BadDump(),))


def test_adapter_discover_error_becomes_transform_error(tmp_path):
    class BadDiscover(JsonAdapter):
        def discover(self, home: Path) -> list[Path]:
            raise PermissionError("no access")

    with pytest.raises(rt.TransformError, match="teststore"):
        _run(tmp_path, adapters=(BadDiscover(),))


def test_write_failure_becomes_transform_error(tmp_path, monkeypatch):
    _write(tmp_path, "a.json", ["x"])

    def fail(path: Path, data: bytes) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(rt, "atomic_write_bytes", fail)
    with pytest.raises(rt.TransformError, match="disk full"):
        _run(tmp_path)
