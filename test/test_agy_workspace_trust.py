#!/usr/bin/env python3
"""Tests for agent-scripts/agy_workspace_trust.py.

Every test writes a temp settings.json copy; the real
~/.gemini/antigravity-cli/settings.json is never touched.
"""

import json
import threading
from pathlib import Path

import pytest

import agy_workspace_trust as agy


def _settings(tmp_path: Path, obj: object) -> Path:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps(obj), encoding="utf-8")
    return path


def test_ensure_trusted_adds_missing_path_and_preserves_other_keys(tmp_path):
    path = _settings(tmp_path, {"permissions": {"allow": ["command(git)"]}})
    assert agy.ensure_trusted("/tmp/new-dir", path=path) is True
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["trustedWorkspaces"] == ["/tmp/new-dir"]
    assert data["permissions"] == {"allow": ["command(git)"]}


def test_ensure_trusted_idempotent(tmp_path):
    path = _settings(tmp_path, {"trustedWorkspaces": ["/tmp/a"]})
    assert agy.ensure_trusted("/tmp/a", path=path) is False
    assert json.loads(path.read_text(encoding="utf-8"))["trustedWorkspaces"] == ["/tmp/a"]


def test_ensure_trusted_creates_missing_key(tmp_path):
    path = _settings(tmp_path, {})
    assert agy.ensure_trusted("/tmp/x", path=path) is True
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "trustedWorkspaces": ["/tmp/x"]
    }


def test_ensure_trusted_exact_spelling_idempotent(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    path = _settings(tmp_path, {"trustedWorkspaces": [str(real)]})
    assert agy.ensure_trusted(str(real), path=path) is False


def test_ensure_trusted_adds_real_path_when_only_symlink_trusted(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    path = _settings(tmp_path, {"trustedWorkspaces": [str(link)]})
    # agy matches verbatim, so the real spelling must be added even though the
    # symlink entry already resolves to the same directory.
    assert agy.ensure_trusted(str(real), path=path) is True
    assert json.loads(path.read_text(encoding="utf-8"))["trustedWorkspaces"] == [
        str(link),
        str(real),
    ]


def test_missing_file_refused(tmp_path):
    with pytest.raises(agy.TrustSettingsError, match="run `agy` once"):
        agy.ensure_trusted("/tmp/x", path=tmp_path / "settings.json")


def test_invalid_json_refused(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(agy.TrustSettingsError, match="not valid JSON"):
        agy.ensure_trusted("/tmp/x", path=path)


def test_invalid_utf8_refused(tmp_path):
    path = tmp_path / "settings.json"
    path.write_bytes(b"\xff\xfe\x00 not utf-8")
    with pytest.raises(agy.TrustSettingsError, match="cannot read"):
        agy.ensure_trusted("/tmp/x", path=path)


def test_non_object_root_refused(tmp_path):
    path = _settings(tmp_path, ["not", "an", "object"])
    with pytest.raises(agy.TrustSettingsError, match="non-object root"):
        agy.ensure_trusted("/tmp/x", path=path)


def test_non_list_trusted_refused(tmp_path):
    path = _settings(tmp_path, {"trustedWorkspaces": "nope"})
    with pytest.raises(agy.TrustSettingsError, match="not a list"):
        agy.ensure_trusted("/tmp/x", path=path)


def test_non_string_member_refused(tmp_path):
    path = _settings(tmp_path, {"trustedWorkspaces": [123]})
    with pytest.raises(agy.TrustSettingsError, match="non-string"):
        agy.ensure_trusted("/tmp/x", path=path)


def test_remove_trusted(tmp_path):
    path = _settings(tmp_path, {"trustedWorkspaces": ["/tmp/a", "/tmp/b"]})
    assert agy.remove_trusted("/tmp/a", path=path) is True
    assert json.loads(path.read_text(encoding="utf-8"))["trustedWorkspaces"] == [
        "/tmp/b"
    ]
    assert agy.remove_trusted("/tmp/a", path=path) is False


def test_prune_removes_only_missing_paths(tmp_path):
    keep = tmp_path / "keep"
    keep.mkdir()
    path = _settings(
        tmp_path, {"trustedWorkspaces": [str(keep), "/tmp/definitely-missing"]}
    )
    assert agy.prune_trusted(path=path) == ["/tmp/definitely-missing"]
    assert json.loads(path.read_text(encoding="utf-8"))["trustedWorkspaces"] == [
        str(keep)
    ]


def test_concurrent_writers_all_preserved(tmp_path):
    path = _settings(tmp_path, {})
    dirs = [f"/tmp/concurrent-{i}" for i in range(20)]

    def add(directory: str) -> None:
        agy.ensure_trusted(directory, path=path)

    threads = [threading.Thread(target=add, args=(d,)) for d in dirs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    data = json.loads(path.read_text(encoding="utf-8"))
    assert sorted(data["trustedWorkspaces"]) == sorted(dirs)


def test_cli_add_list_prune(tmp_path, capsys):
    path = _settings(tmp_path, {})
    assert agy.main(["add", "/tmp/cli-dir", "--settings", str(path)]) == 0
    assert agy.main(["list", "--settings", str(path)]) == 0
    assert "/tmp/cli-dir" in capsys.readouterr().out

    assert agy.main(["prune", "--settings", str(path)]) == 0
    assert "/tmp/cli-dir" in capsys.readouterr().out


def test_cli_refusal_exits_nonzero(tmp_path, capsys):
    path = _settings(tmp_path, {"trustedWorkspaces": "nope"})
    assert agy.main(["add", "/tmp/x", "--settings", str(path)]) == 1
    assert "agy-workspace-trust:" in capsys.readouterr().err


def test_symlinked_settings_writes_target_not_link(tmp_path):
    real = tmp_path / "real" / "settings.json"
    real.parent.mkdir()
    real.write_text(json.dumps({}), encoding="utf-8")
    link = tmp_path / "settings.json"
    link.symlink_to(real)

    assert agy.ensure_trusted("/tmp/x", path=link) is True
    # The symlink survives; the write landed at the target.
    assert link.is_symlink()
    assert json.loads(real.read_text(encoding="utf-8")) == {
        "trustedWorkspaces": ["/tmp/x"]
    }
