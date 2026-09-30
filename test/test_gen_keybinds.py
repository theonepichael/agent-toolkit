#!/usr/bin/env python3
"""Tests for agent-scripts/gen_keybinds.py.

Every fixture is a synthetic byte blob shaped like the minified keybind table in
an opencode binary. Nothing here runs the real opencode binary or npm: every
subprocess call is replaced, and every file lives under ``tmp_path``.

The shapes are taken from two real builds. The published release minifies the
keybind helper as ``H`` and a development build as ``g``, which is why the
extractor must find the helper by what it does, not by what it is called.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent-scripts"))
import pytest  # noqa: E402

import gen_keybinds as gk  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "python-quality.yml"

# Leader letters in the synthetic table, with the keybind names that own them.
LEADER_ROWS = {
    "a": "agent_list",
    "b": "sidebar_toggle",
    "c": "session_compact",
    "e": "editor_open",
    "l": "session_list",
    "m": "model_list",
    "n": "session_new",
    "s": "status_view",
    "t": "theme_list",
    "u": "messages_undo",
    "y": "messages_copy",
}


def _entries(helper: str, *, filler: int = 60, extra: str = "") -> str:
    parts = [
        f'leader:{helper}(kl,"Leader key for keybind combinations")',
        f'app_exit:{helper}("ctrl+c,ctrl+d,<leader>q","Exit the application")',
    ]
    for letter, name in LEADER_ROWS.items():
        parts.append(f'{name}:{helper}("<leader>{letter}","Do {name}")')
    parts.append(f'tips_toggle:{helper}("<leader>q","Toggle tips")')
    for index in range(filler):
        parts.append(f'filler_{index}:{helper}("none","Filler {index}")')
    if extra:
        parts.append(extra)
    return ",".join(parts)


def _table(helper: str = "H", **kwargs: object) -> str:
    return (
        f"{helper}=(_,J)=>({{default:_,description:J}}),"
        f"x_={{{_entries(helper, **kwargs)}}}"  # type: ignore[arg-type]
    )


def _expected() -> dict[str, list[str]]:
    rows = {letter: [name] for letter, name in LEADER_ROWS.items()}
    rows["q"] = ["app_exit", "tips_toggle"]
    return dict(sorted(rows.items()))


def _binary(tmp_path: Path, body: str, name: str = "opencode") -> Path:
    path = tmp_path / name
    path.write_bytes(b"\x7fELF junk " + body.encode() + b" trailing junk")
    return path


# ── extraction ────────────────────────────────────────────────────────────────


@pytest.mark.regression(
    "keybind-extractor-hardcodes-minified-helper-name",
    "gen_keybinds.KeybindExtractionError: 'leader:g(' not found in",
)
def test_extracts_table_whatever_the_helper_is_called(tmp_path: Path) -> None:
    for helper in ("H", "g", "W4"):
        binary = _binary(tmp_path, _table(helper), f"opencode-{helper}")
        assert gk.leader_chords(binary) == _expected(), helper


def test_ignores_a_leader_object_built_by_a_different_helper(tmp_path: Path) -> None:
    # The dev build also has ``{leader:os(e.api,"leader"),...}``, a command map.
    decoy = 'os=(e,t)=>St(e,t),cm={leader:os(e.api,"leader"),messagesCopy:os(e.api,"x")}'
    binary = _binary(tmp_path, decoy + ";" + _table("H"))
    assert gk.leader_chords(binary) == _expected()


def test_rejects_a_lone_decoy_without_the_identity_keys(tmp_path: Path) -> None:
    body = _table("H").replace("app_exit:", "zz_exit:").replace(
        "messages_copy:", "zz_copy:"
    )
    binary = _binary(tmp_path, body)
    with pytest.raises(gk.KeybindExtractionError, match="identity"):
        gk.leader_chords(binary)


def test_rejects_two_candidate_tables(tmp_path: Path) -> None:
    binary = _binary(tmp_path, _table("H") + ";" + _table("Q"))
    with pytest.raises(gk.KeybindExtractionError, match="2 candidate"):
        gk.leader_chords(binary)


def test_rejects_missing_table(tmp_path: Path) -> None:
    binary = _binary(tmp_path, "nothing to see here")
    with pytest.raises(gk.KeybindExtractionError):
        gk.leader_chords(binary)


def test_rejects_a_table_below_the_keybind_floor(tmp_path: Path) -> None:
    binary = _binary(tmp_path, _table("H", filler=5))
    with pytest.raises(gk.KeybindExtractionError, match="floor"):
        gk.leader_chords(binary)


def test_quoted_braces_do_not_move_the_object_bounds(tmp_path: Path) -> None:
    # A quoted "{" just before the object would fool a backward brace search,
    # and a quoted "}" inside a description would end a naive forward scan.
    extra = 'weird_braces:H("none","Close } then { open")'
    body = 'z="{",' + _table("H", extra=extra)
    assert gk.leader_chords(_binary(tmp_path, body)) == _expected()


def test_rejects_a_scan_truncated_after_the_floors(tmp_path: Path) -> None:
    # An unterminated string mid-object swallows the rest, including the close
    # brace. Floors and identity keys were already met before the break, so only
    # whole-object accounting can catch this.
    extra = 'broken:H("none","unterminated)'
    body = _table("H", extra=extra) + ',later:H("<leader>z","after")}'
    with pytest.raises(gk.KeybindExtractionError):
        gk.leader_chords(_binary(tmp_path, body))


def test_counts_quoted_keys_and_object_defaults(tmp_path: Path) -> None:
    # Both real builds have ``"dialog.select.prev":H("up,ctrl+p",...)`` and
    # ``input_paste:H({key:"ctrl+v",preventDefault:!1},...)``. A name-regex parser
    # skipped the quoted-key entries entirely, so a leader chord added to one of
    # them would have been invisible.
    extra = (
        '"dialog.x.open":H("<leader>d","Quoted key")'
        ',input_paste:H({key:"<leader>p",preventDefault:!1},"Object default")'
    )
    chords = gk.leader_chords(_binary(tmp_path, _table("H", extra=extra)))
    assert chords["d"] == ["dialog.x.open"]
    assert chords["p"] == ["input_paste"]


@pytest.mark.regression(
    "keybind-identifier-default-hides-leader-chord",
    "Failed: DID NOT RAISE KeybindExtractionError",
)
@pytest.mark.parametrize(
    "extra",
    [
        'some_action:H(K,"Default held in a variable")',
        'some_action:H({key:K,preventDefault:!1},"Object default via a variable")',
    ],
)
def test_rejects_a_default_it_cannot_read(tmp_path: Path, extra: str) -> None:
    # ``K="<leader>d"`` elsewhere would make this a chord the table silently
    # omits. Only ``leader`` itself (the leader key, never a chord) may default
    # to a variable.
    with pytest.raises(gk.KeybindExtractionError, match="entry"):
        gk.leader_chords(_binary(tmp_path, _table("H", extra=extra)))


def test_rejects_an_entry_that_is_not_a_helper_call(tmp_path: Path) -> None:
    body = _table("H", extra="sneaky:someOtherThing(1,2)")
    with pytest.raises(gk.KeybindExtractionError, match="entry"):
        gk.leader_chords(_binary(tmp_path, body))


# ── version and pin ───────────────────────────────────────────────────────────


def _fake_run(stdout: str = "", returncode: int = 0) -> object:
    def run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, returncode, stdout, "")

    return run


@pytest.mark.parametrize("line", ["1.18.32", "0.0.0-dev-202609250015"])
def test_version_accepts_release_and_dev_builds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, line: str
) -> None:
    monkeypatch.setattr(gk.subprocess, "run", _fake_run(line + "\n"))
    assert gk.opencode_version(tmp_path / "opencode") == line


@pytest.mark.parametrize(
    ("line", "expected"),
    [("v1.18.32", "1.18.32"), ("1.18.32+build.5", "1.18.32+build.5")],
)
def test_version_accepts_the_forms_the_sdk_pin_check_accepts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, line: str, expected: str
) -> None:
    """Same version-line grammar as test_opencode_ts_checks.VERSION_LINE: an
    optional leading ``v`` (stripped) and optional ``+build`` metadata."""
    monkeypatch.setattr(gk.subprocess, "run", _fake_run(line + "\n"))
    assert gk.opencode_version(tmp_path / "opencode") == expected


@pytest.mark.parametrize(
    ("stdout", "code"), [("1.18.32\n", 1), ("", 0), ("opencode rocks\n", 0)]
)
def test_version_failure_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stdout: str, code: int
) -> None:
    monkeypatch.setattr(gk.subprocess, "run", _fake_run(stdout, code))
    with pytest.raises(gk.KeybindExtractionError):
        gk.opencode_version(tmp_path / "opencode")


def test_pinned_version_reads_the_sdk_pin(tmp_path: Path) -> None:
    pkg = tmp_path / "package.json"
    pkg.write_text(json.dumps({"devDependencies": {"@opencode-ai/plugin": "1.2.3"}}))
    assert gk.pinned_version(pkg) == "1.2.3"
    pkg.write_text(json.dumps({"devDependencies": {}}))
    with pytest.raises(gk.KeybindExtractionError):
        gk.pinned_version(pkg)


# ── fetching the pinned binary ────────────────────────────────────────────────


def _tgz(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _fake_npm(members: dict[str, bytes], calls: list[list[str]], code: int = 0):
    def run(cmd: list[str], **_: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        dest = Path(cmd[cmd.index("--pack-destination") + 1])
        (dest / "opencode-linux-x64-1.18.32.tgz").write_bytes(_tgz(members))
        return subprocess.CompletedProcess(
            cmd, code, "opencode-linux-x64-1.18.32.tgz\n", "boom" if code else ""
        )

    return run


def _linux_x64(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gk.platform, "system", lambda: "Linux")
    monkeypatch.setattr(gk.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(gk.shutil, "which", lambda name: f"/usr/bin/{name}")


def test_fetch_packs_the_pinned_platform_package(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _linux_x64(monkeypatch)
    calls: list[list[str]] = []
    monkeypatch.setattr(
        gk.subprocess, "run", _fake_npm({"package/bin/opencode": b"BIN"}, calls)
    )
    path = gk.fetch_pinned_binary("1.18.32", tmp_path)
    assert path.read_bytes() == b"BIN"
    assert path.is_relative_to(tmp_path)
    assert "opencode-linux-x64@1.18.32" in calls[0]
    assert calls[0][:2] == ["/usr/bin/npm", "pack"]


def test_fetch_failure_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _linux_x64(monkeypatch)
    monkeypatch.setattr(
        gk.subprocess, "run", _fake_npm({"package/bin/opencode": b"x"}, [], code=1)
    )
    with pytest.raises(gk.KeybindExtractionError, match="npm pack"):
        gk.fetch_pinned_binary("1.18.32", tmp_path)


def test_fetch_missing_member_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _linux_x64(monkeypatch)
    monkeypatch.setattr(
        gk.subprocess, "run", _fake_npm({"package/README.md": b"x"}, [])
    )
    with pytest.raises(gk.KeybindExtractionError, match="package/bin/opencode"):
        gk.fetch_pinned_binary("1.18.32", tmp_path)


def test_fetch_unsupported_platform_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(gk.platform, "system", lambda: "Windows")
    monkeypatch.setattr(gk.platform, "machine", lambda: "AMD64")
    with pytest.raises(gk.KeybindExtractionError, match="platform"):
        gk.fetch_pinned_binary("1.18.32", tmp_path)


# ── main(): exit-code contract ────────────────────────────────────────────────

PIN = "1.18.32"


@pytest.fixture
def repo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """A parity doc and package.json under tmp_path, wired into the module."""
    doc = tmp_path / "PARITY.md"
    body = gk.render_block(_expected())
    doc.write_text(
        "# hand-authored\n\n"
        f"{gk.BEGIN_ANCHOR}\n\n{body}\n{gk.END_ANCHOR}\n\ntrailing prose\n"
    )
    pkg = tmp_path / "package.json"
    pkg.write_text(json.dumps({"devDependencies": {"@opencode-ai/plugin": PIN}}))
    monkeypatch.setattr(gk, "PARITY_DOC", doc)
    monkeypatch.setattr(gk, "PACKAGE_JSON", pkg)
    return tmp_path


def _installed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, version: str, body: str = ""
) -> Path:
    binary = _binary(tmp_path, body or _table("H"), "installed-opencode")
    monkeypatch.setattr(gk.shutil, "which", lambda _name: str(binary))
    monkeypatch.setattr(gk, "opencode_version", lambda _b: version)
    return binary


def test_check_fresh_against_pinned_binary(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    _installed(monkeypatch, repo, PIN)
    assert gk.main(["--check"]) == 0


def test_check_stale_exits_1(
    monkeypatch: pytest.MonkeyPatch, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    extra = 'new_thing:H("<leader>d","New")'
    _installed(monkeypatch, repo, PIN, _table("H", extra=extra))
    assert gk.main(["--check"]) == 1
    assert "STALE" in capsys.readouterr().err


def test_check_unpinned_without_allow_exits_2(
    monkeypatch: pytest.MonkeyPatch, repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _installed(monkeypatch, repo, "0.0.0-dev-1")
    assert gk.main(["--check"]) == 2
    assert "not the pinned" in capsys.readouterr().err


def test_check_unpinned_with_allow_really_compares(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    _installed(monkeypatch, repo, "0.0.0-dev-1")
    assert gk.main(["--check", "--allow-unpinned"]) == 0
    extra = 'new_thing:H("<leader>d","New")'
    _installed(monkeypatch, repo, "0.0.0-dev-1", _table("H", extra=extra))
    assert gk.main(["--check", "--allow-unpinned"]) == 1


def test_check_without_any_binary_exits_2(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    monkeypatch.setattr(gk.shutil, "which", lambda _name: None)
    assert gk.main(["--check"]) == 2


def test_explicit_binary_is_used(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    binary = _binary(repo, _table("g"), "explicit")
    monkeypatch.setattr(gk.shutil, "which", lambda _name: None)
    monkeypatch.setattr(gk, "opencode_version", lambda _b: PIN)
    assert gk.main(["--check", "--binary", str(binary)]) == 0


def test_unreadable_binary_exits_2(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    monkeypatch.setattr(gk, "opencode_version", lambda _b: PIN)
    assert gk.main(["--check", "--binary", str(repo / "does-not-exist")]) == 2


def test_fetch_pinned_path(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    fetched = _binary(repo, _table("H"), "fetched")
    seen: list[str] = []

    def fetch(version: str, dest: Path) -> Path:
        seen.append(version)
        return fetched

    monkeypatch.setattr(gk, "fetch_pinned_binary", fetch)
    monkeypatch.setattr(gk, "opencode_version", lambda _b: PIN)
    assert gk.main(["--check", "--fetch-pinned"]) == 0
    assert seen == [PIN]


def test_fetched_binary_with_wrong_version_exits_2(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    fetched = _binary(repo, _table("H"), "fetched")
    monkeypatch.setattr(gk, "fetch_pinned_binary", lambda _v, _d: fetched)
    monkeypatch.setattr(gk, "opencode_version", lambda _b: "9.9.9")
    assert gk.main(["--check", "--fetch-pinned", "--allow-unpinned"]) == 2


def test_fetch_error_exits_2(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    def fetch(_v: str, _d: Path) -> Path:
        raise gk.KeybindExtractionError("npm pack failed")

    monkeypatch.setattr(gk, "fetch_pinned_binary", fetch)
    assert gk.main(["--check", "--fetch-pinned"]) == 2


def test_missing_pin_exits_2(monkeypatch: pytest.MonkeyPatch, repo: Path) -> None:
    (repo / "package.json").write_text("{not json")
    _installed(monkeypatch, repo, PIN)
    assert gk.main(["--check"]) == 2


def test_write_from_unpinned_binary_exits_2(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    before = (repo / "PARITY.md").read_text()
    _installed(monkeypatch, repo, "0.0.0-dev-1")
    assert gk.main([]) == 2
    assert (repo / "PARITY.md").read_text() == before


@pytest.mark.parametrize(
    "argv", [["--allow-unpinned"], ["--check", "--binary", "x", "--fetch-pinned"]]
)
def test_usage_errors_exit_2(repo: Path, argv: list[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        gk.main(argv)
    assert excinfo.value.code == 2


def test_write_rewrites_only_the_anchored_block(
    monkeypatch: pytest.MonkeyPatch, repo: Path
) -> None:
    extra = 'new_thing:H("<leader>d","New")'
    _installed(monkeypatch, repo, PIN, _table("H", extra=extra))
    assert gk.main([]) == 0
    text = (repo / "PARITY.md").read_text()
    assert text.startswith("# hand-authored\n\n")
    assert text.endswith("\n\ntrailing prose\n")
    assert "| `<leader>d` | `new_thing` |" in text
    assert gk.main(["--check"]) == 0


# ── CI wiring ─────────────────────────────────────────────────────────────────


def test_ci_runs_the_pinned_check_and_cannot_skip_it() -> None:
    """CI has no opencode on PATH, so the pytest staleness test skips there.

    This step is the only thing that compares the committed table with the pinned
    release in CI. Losing ``--fetch-pinned`` would make it exit 2 (no binary),
    and ``continue-on-error`` would make any failure green, so pin both.
    """
    text = WORKFLOW.read_text()
    test_job = text.split("\n  test:\n", 1)[1].split("\n  test-3-12:\n", 1)[0]
    steps = test_job.split("\n      - ")
    matching = [s for s in steps if "gen_keybinds.py" in s]
    assert len(matching) == 1, "python-quality `test` job must run gen_keybinds.py"
    step = matching[0]
    assert "--check" in step and "--fetch-pinned" in step, step
    assert "continue-on-error" not in step, step
