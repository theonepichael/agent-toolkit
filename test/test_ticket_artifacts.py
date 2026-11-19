#!/usr/bin/env python3
"""ticket_artifacts.py: key resolution, destination reuse, classification,
and moves that never overwrite or delete.

Real subprocess here is bounded to `git` in tmp_path, which is why this file
carries the allow_real_subprocess marker.
"""

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "agent-scripts"))

import agent_toolkit_paths  # noqa: E402
import pytest  # noqa: E402
import ticket_artifacts as ta  # noqa: E402
import worktree_provenance  # noqa: E402

pytestmark = pytest.mark.allow_real_subprocess  # git in tmp_path only


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict:
    """A repo with one tracked file, a linked worktree on a ticket branch,
    an isolated toolkit home, and an empty destination root."""
    toolkit = tmp_path / "toolkit"
    monkeypatch.setenv("AGENT_TOOLKIT_HOME", str(toolkit))
    monkeypatch.delenv(ta.ENV_ROOT, raising=False)
    agent_toolkit_paths.DEFAULT_RESOLVER.invalidate()
    repo = tmp_path / "proj"
    repo.mkdir()
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "t@example.invalid", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    _git("config", "gc.auto", "0", cwd=repo)
    _git("config", "maintenance.auto", "false", cwd=repo)
    (repo / "tracked.md").write_text("x\n")
    (repo / ".gitignore").write_text("*.log\n")
    _git("add", ".", cwd=repo)
    _git("commit", "-qm", "init", cwd=repo)
    wt = tmp_path / "wt"
    _git("worktree", "add", "-q", str(wt), "-b", "PROJ-1234-Retry_Webhooks", cwd=repo)
    root = tmp_path / "tickets"
    root.mkdir()
    yield {"repo": repo, "wt": wt, "root": root, "toolkit": toolkit}
    agent_toolkit_paths.DEFAULT_RESOLVER.invalidate()


def _run(capsys: pytest.CaptureFixture, *argv: str) -> tuple[int, dict | None]:
    rc = ta.main(list(argv))
    out = capsys.readouterr().out
    return rc, json.loads(out) if out.strip() else None


def _scan(capsys: pytest.CaptureFixture, wt: Path, *extra: str) -> dict:
    rc, data = _run(capsys, "scan", "--worktree", str(wt), *extra)
    assert rc == 0
    return data


# ── ticket key resolution ────────────────────────────────────────────────────


def test_key_and_description_come_from_the_branch(env: dict, capsys) -> None:
    data = _scan(capsys, env["wt"], "--root", str(env["root"]))
    assert data["ticket_key"] == "PROJ-1234"
    assert data["key_source"] == "branch"
    assert data["description"] == "retry-webhooks"
    assert data["destination"] == str(env["root"] / "PROJ-1234-retry-webhooks")
    assert data["destination_exists"] is False
    assert data["error"] is None


def test_branch_that_is_only_the_key_needs_a_description(env: dict, capsys) -> None:
    _git("checkout", "-q", "-b", "PROJ-77", cwd=env["wt"])
    data = _scan(capsys, env["wt"], "--root", str(env["root"]))
    assert data["ticket_key"] == "PROJ-77"
    assert data["description"] is None
    assert data["destination"] is None
    assert data["error"] is not None
    data = _scan(
        capsys, env["wt"], "--root", str(env["root"]), "--description", "Fix Login!"
    )
    assert data["destination"] == str(env["root"] / "PROJ-77-fix-login")


def test_marker_slug_is_the_folder_name_when_branch_has_no_key(
    env: dict, capsys
) -> None:
    _git("checkout", "-q", "-b", "atk-some-item", cwd=env["wt"])
    assert worktree_provenance.write_marker(env["wt"], "atk-some-item")
    data = _scan(capsys, env["wt"], "--root", str(env["root"]))
    assert data["key_source"] == "marker"
    assert data["destination"] == str(env["root"] / "atk-some-item")


def test_no_branch_key_and_no_marker_reports_none(env: dict, capsys) -> None:
    _git("checkout", "-q", "-b", "scratch", cwd=env["wt"])
    data = _scan(capsys, env["wt"], "--root", str(env["root"]))
    assert data["key_source"] == "none"
    assert data["ticket_key"] is None
    assert data["destination"] is None
    data = _scan(capsys, env["wt"], "--root", str(env["root"]), "--key", "OPS-9",
                 "--description", "cleanup")
    assert data["key_source"] == "flag"
    assert data["destination"] == str(env["root"] / "OPS-9-cleanup")


def test_config_regex_overrides_the_default(env: dict, capsys) -> None:
    cfg = env["toolkit"] / "data" / "artifacts" / "config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"branch_key_regex": r"(?<=feature/)[a-z]+-\d+"}))
    _git("checkout", "-q", "-b", "feature/abc-12-thing", cwd=env["wt"])
    data = _scan(capsys, env["wt"], "--root", str(env["root"]))
    assert data["ticket_key"] == "abc-12"
    assert data["description"] == "thing"


# ── destination folder reuse ─────────────────────────────────────────────────


def test_existing_key_folder_is_reused_whatever_its_description(
    env: dict, capsys
) -> None:
    (env["root"] / "PROJ-1234-older-name").mkdir()
    (env["root"] / "PROJ-12345-other-ticket").mkdir()
    data = _scan(capsys, env["wt"], "--root", str(env["root"]))
    assert data["destination"] == str(env["root"] / "PROJ-1234-older-name")
    assert data["destination_exists"] is True


def test_more_than_one_key_folder_is_an_error(env: dict, capsys) -> None:
    (env["root"] / "PROJ-1234").mkdir()
    (env["root"] / "PROJ-1234-a").mkdir()
    data = _scan(capsys, env["wt"], "--root", str(env["root"]))
    assert data["destination"] is None
    assert data["error"] is not None
    (env["wt"] / "notes.md").write_text("n\n")
    rc, out = _run(capsys, "move", "--worktree", str(env["wt"]),
                   "--root", str(env["root"]), "notes.md")
    assert rc == 2
    assert (env["wt"] / "notes.md").exists()


# ── classification ───────────────────────────────────────────────────────────


def test_untracked_files_are_labeled_and_ignored_files_excluded(
    env: dict, capsys
) -> None:
    wt = env["wt"]
    (wt / "runbook.md").write_text("r\n")
    (wt / "fix.py").write_text("p\n")
    (wt / "screenshots" / "deep").mkdir(parents=True)
    (wt / "screenshots" / "deep" / "a.bin").write_text("b\n")
    (wt / "notes.txt").write_text("t\n")
    (wt / "debug.log").write_text("ignored\n")
    data = _scan(capsys, wt)
    labels = {f["path"]: f["label"] for f in data["files"]}
    assert labels == {
        "fix.py": "code",
        "notes.txt": "unknown",
        "runbook.md": "artifact",
        "screenshots/deep/a.bin": "artifact",
    }


def test_code_globs_win_over_artifact_globs(env: dict, capsys) -> None:
    cfg = env["toolkit"] / "data" / "artifacts" / "config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"code_globs": ["docs/*.md"]}))
    (env["wt"] / "docs").mkdir()
    (env["wt"] / "docs" / "api.md").write_text("a\n")
    (env["wt"] / "loose.md").write_text("l\n")
    labels = {f["path"]: f["label"] for f in _scan(capsys, env["wt"])["files"]}
    assert labels == {"docs/api.md": "code", "loose.md": "artifact"}


# ── root precedence ──────────────────────────────────────────────────────────


def test_root_precedence_flag_then_env_then_config(
    env: dict, capsys, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = env["toolkit"] / "data" / "artifacts" / "config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"root": str(tmp_path / "from-config")}))
    assert _scan(capsys, env["wt"])["root"] == str(tmp_path / "from-config")
    monkeypatch.setenv(ta.ENV_ROOT, str(tmp_path / "from-env"))
    assert _scan(capsys, env["wt"])["root"] == str(tmp_path / "from-env")
    data = _scan(capsys, env["wt"], "--root", str(tmp_path / "from-flag"))
    assert data["root"] == str(tmp_path / "from-flag")


def test_unset_root_scans_but_move_refuses(env: dict, capsys) -> None:
    (env["wt"] / "runbook.md").write_text("r\n")
    data = _scan(capsys, env["wt"])
    assert data["root"] is None
    assert data["destination"] is None
    assert [f["path"] for f in data["files"]] == ["runbook.md"]
    rc, out = _run(capsys, "move", "--worktree", str(env["wt"]), "runbook.md")
    assert rc == 2
    assert out is None
    assert (env["wt"] / "runbook.md").exists()


def test_malformed_config_is_a_setup_error(env: dict, capsys) -> None:
    cfg = env["toolkit"] / "data" / "artifacts" / "config.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("{not json")
    assert ta.main(["scan", "--worktree", str(env["wt"])]) == 2


# ── moves ────────────────────────────────────────────────────────────────────


def test_move_preserves_relative_paths_and_leaves_the_worktree_clean(
    env: dict, capsys
) -> None:
    wt, root = env["wt"], env["root"]
    (wt / "runbook.md").write_text("r\n")
    (wt / "screenshots").mkdir()
    (wt / "screenshots" / "a.png").write_bytes(b"png")
    rc, out = _run(capsys, "move", "--worktree", str(wt), "--root", str(root),
                   "runbook.md", "screenshots/a.png")
    assert rc == 0
    dest = root / "PROJ-1234-retry-webhooks"
    assert [r["result"] for r in out["results"]] == ["moved", "moved"]
    assert (dest / "runbook.md").read_text() == "r\n"
    assert (dest / "screenshots" / "a.png").read_bytes() == b"png"
    assert _scan(capsys, wt)["files"] == []


def test_collision_is_skipped_and_neither_file_is_touched(env: dict, capsys) -> None:
    wt, root = env["wt"], env["root"]
    dest = root / "PROJ-1234-retry-webhooks"
    dest.mkdir()
    (dest / "runbook.md").write_text("old\n")
    (wt / "runbook.md").write_text("new\n")
    (wt / "deck.pptx").write_text("d\n")
    rc, out = _run(capsys, "move", "--worktree", str(wt), "--root", str(root),
                   "runbook.md", "deck.pptx")
    assert rc == 1
    results = {r["path"]: r["result"] for r in out["results"]}
    assert results == {"runbook.md": "skipped-collision", "deck.pptx": "moved"}
    assert (dest / "runbook.md").read_text() == "old\n"
    assert (wt / "runbook.md").read_text() == "new\n"


def test_move_refuses_tracked_ignored_and_unlisted_files(env: dict, capsys) -> None:
    wt, root = env["wt"], env["root"]
    (wt / "debug.log").write_text("ignored\n")
    (wt / "junk.txt").write_text("j\n")
    rc, out = _run(capsys, "move", "--worktree", str(wt), "--root", str(root),
                   "tracked.md", "debug.log", "../outside.md")
    assert rc == 1
    assert [r["result"] for r in out["results"]] == ["error", "error", "error"]
    assert (wt / "tracked.md").exists()
    assert (wt / "debug.log").exists()
    # Junk nobody named is left exactly where it was.
    assert (wt / "junk.txt").read_text() == "j\n"


def test_not_a_git_worktree_is_a_setup_error(tmp_path: Path, env: dict) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    assert ta.main(["scan", "--worktree", str(plain)]) == 2


# ── review regressions ───────────────────────────────────────────────────────


@pytest.mark.regression(
    "move-overwrites-target-created-after-check",
    "AssertionError: assert 'new\\n' == 'old\\n'",
)
def test_target_appearing_after_any_check_is_never_overwritten(
    env: dict, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Another shunt for the same ticket creates the target between a
    # check and the move: simulate by hiding the target from exists().
    wt, root = env["wt"], env["root"]
    dest = root / "PROJ-1234-retry-webhooks"
    dest.mkdir()
    (dest / "runbook.md").write_text("old\n")
    (wt / "runbook.md").write_text("new\n")
    real_exists = Path.exists
    monkeypatch.setattr(
        Path, "exists", lambda self, **kw: False if self.name == "runbook.md" else real_exists(self, **kw)
    )
    monkeypatch.setattr(Path, "is_symlink", lambda self: False)
    rc, out = _run(capsys, "move", "--worktree", str(wt), "--root", str(root),
                   "runbook.md")
    assert (dest / "runbook.md").read_text() == "old\n"
    assert (wt / "runbook.md").read_text() == "new\n"
    assert rc == 1
    assert out["results"][0]["result"] == "skipped-collision"


@pytest.mark.regression(
    "key-escapes-destination-root",
    "assert 0 == 2",
)
def test_key_cannot_escape_the_root(env: dict, capsys) -> None:
    (env["wt"] / "runbook.md").write_text("r\n")
    rc, _ = _run(capsys, "move", "--worktree", str(env["wt"]), "--root",
                 str(env["root"]), "--key", "../other", "--description", "demo",
                 "runbook.md")
    assert rc == 2
    assert not (env["root"].parent / "other-demo").exists()
    assert (env["wt"] / "runbook.md").exists()


@pytest.mark.regression(
    "root-inside-worktree-accepted",
    "assert 0 == 2",
)
def test_root_inside_the_worktree_is_refused(env: dict, capsys) -> None:
    (env["wt"] / "runbook.md").write_text("r\n")
    rc, _ = _run(capsys, "move", "--worktree", str(env["wt"]), "--root",
                 str(env["wt"] / "stash"), "runbook.md")
    assert rc == 2
    assert (env["wt"] / "runbook.md").exists()


@pytest.mark.regression(
    "staged-rename-source-read-as-untracked",
    "AssertionError: assert ['notes.md'] == []",
)
def test_staged_rename_is_not_reported_as_untracked(env: dict, capsys) -> None:
    wt = env["wt"]
    (wt / "?? notes.md").write_text("t\n")
    _git("add", "?? notes.md", cwd=wt)
    _git("commit", "-qm", "odd name", cwd=wt)
    _git("mv", "?? notes.md", "renamed.md", cwd=wt)
    assert [f["path"] for f in _scan(capsys, wt)["files"]] == []


@pytest.mark.regression(
    "carriage-return-filename-unmovable",
    "UnicodeDecodeError: 'utf-8' codec can't decode byte 0xe9",
)
def test_odd_byte_filenames_round_trip_through_scan_and_move(
    env: dict, capsys
) -> None:
    wt, root = env["wt"], env["root"]
    names = ["cr\rname.md", "latin\udce9.md"]  # \udce9 = raw byte 0xE9
    for name in names:
        (wt / name).write_text("x\n")
    listed = [f["path"] for f in _scan(capsys, wt)["files"]]
    assert sorted(listed) == sorted(names)
    rc, out = _run(capsys, "move", "--worktree", str(wt), "--root", str(root),
                   *listed)
    assert [r["result"] for r in out["results"]] == ["moved", "moved"]
    assert rc == 0


def test_cross_device_move_copies_and_still_never_overwrites(
    env: dict, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    import errno
    import os

    def no_link(*a: object, **kw: object) -> None:
        raise OSError(errno.EXDEV, "cross-device link")

    monkeypatch.setattr(ta.os, "link", no_link)
    wt, root = env["wt"], env["root"]
    dest = root / "PROJ-1234-retry-webhooks"
    dest.mkdir()
    (dest / "taken.md").write_text("old\n")
    (wt / "taken.md").write_text("new\n")
    (wt / "deck.pptx").write_bytes(b"deck")
    os.symlink("deck.pptx", wt / "link.md")
    rc, out = _run(capsys, "move", "--worktree", str(wt), "--root", str(root),
                   "taken.md", "deck.pptx", "link.md")
    results = {r["path"]: r["result"] for r in out["results"]}
    assert results == {"taken.md": "skipped-collision", "deck.pptx": "moved",
                       "link.md": "moved"}
    assert rc == 1
    assert (dest / "taken.md").read_text() == "old\n"
    assert (wt / "taken.md").read_text() == "new\n"
    assert (dest / "deck.pptx").read_bytes() == b"deck"
    assert not (wt / "deck.pptx").exists()
    assert os.readlink(dest / "link.md") == "deck.pptx"


@pytest.mark.regression(
    "move-follows-destination-symlink-back-into-worktree",
    "AssertionError: assert 'moved' == 'error'",
)
def test_symlinked_folder_in_destination_is_not_followed(env: dict, capsys) -> None:
    import os

    wt, root = env["wt"], env["root"]
    dest = root / "PROJ-1234-retry-webhooks"
    dest.mkdir()
    (wt / "elsewhere").mkdir()
    os.symlink(wt / "elsewhere", dest / "screenshots")
    (wt / "screenshots").mkdir()
    (wt / "screenshots" / "a.png").write_bytes(b"png")
    rc, out = _run(capsys, "move", "--worktree", str(wt), "--root", str(root),
                   "screenshots/a.png")
    assert out["results"][0]["result"] == "error"
    assert rc == 1
    assert (wt / "screenshots" / "a.png").read_bytes() == b"png"
    assert not (wt / "elsewhere" / "a.png").exists()


@pytest.mark.regression(
    "failed-copy-leaves-partial-target",
    "AssertionError: assert not True",
)
def test_failed_cross_device_copy_leaves_no_partial_target(
    env: dict, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    import errno

    def no_link(*a: object, **kw: object) -> None:
        raise OSError(errno.EXDEV, "cross-device link")

    def broken_copystat(*a: object, **kw: object) -> None:
        raise OSError(errno.ENOSPC, "no space")

    monkeypatch.setattr(ta.os, "link", no_link)
    monkeypatch.setattr(ta.shutil, "copystat", broken_copystat)
    wt, root = env["wt"], env["root"]
    (wt / "runbook.md").write_text("r\n")
    rc, out = _run(capsys, "move", "--worktree", str(wt), "--root", str(root),
                   "runbook.md")
    assert out["results"][0]["result"] == "error"
    assert rc == 1
    assert not (root / "PROJ-1234-retry-webhooks" / "runbook.md").exists()
    assert (wt / "runbook.md").read_text() == "r\n"


@pytest.mark.regression(
    "worktree-path-trailing-space-stripped",
    "assert 2 == 0",
)
def test_worktree_path_with_trailing_space(env: dict, capsys, tmp_path: Path) -> None:
    spaced = tmp_path / "ticket "
    _git("worktree", "add", "-q", str(spaced), "-b", "PROJ-9-spaced", cwd=env["repo"])
    (spaced / "runbook.md").write_text("r\n")
    rc, data = _run(capsys, "scan", "--worktree", str(spaced))
    assert rc == 0
    assert data["worktree"] == str(spaced)
    assert [f["path"] for f in data["files"]] == ["runbook.md"]


def test_leading_dash_filename_moves_after_double_dash(env: dict, capsys) -> None:
    wt, root = env["wt"], env["root"]
    (wt / "-draft.md").write_text("d\n")
    rc, out = _run(capsys, "move", "--worktree", str(wt), "--root", str(root),
                   "--", "-draft.md")
    assert rc == 0
    assert out["results"][0]["result"] == "moved"
