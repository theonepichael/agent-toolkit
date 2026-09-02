#!/usr/bin/env python3
"""Fast-tier tests for depart.py's ownership-key and baseline-layer primitives.

No subprocesses, no real HOME — everything runs against a throwaway
``tmp_path``. Mirrors test_install.py's conventions.

Requires Python 3.12+.
"""

import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import depart  # noqa: E402 — must follow sys.path.insert above

# ── ownership-key string rendering ──────────────────────────────────────────


def test_key_helpers_render_type_prefixed_strings(tmp_path):
    assert depart.file_key(tmp_path / "x") == f"file:{tmp_path / 'x'}"
    assert depart.symlink_key(tmp_path / "x") == f"symlink:{tmp_path / 'x'}"
    assert depart.directory_key(tmp_path / "x") == f"directory:{tmp_path / 'x'}"
    assert depart.package_key("apt", "eza") == "package:apt/eza"
    assert depart.package_key("uv-tool", "ruff") == "package:uv-tool/ruff"
    assert depart.service_key("systemd", "watchcommit") == "service:systemd/watchcommit"
    assert depart.runtime_key(tmp_path / ".nvm") == f"runtime:{tmp_path / '.nvm'}"


def test_file_and_symlink_keys_are_distinct_for_the_same_path(tmp_path):
    dest = tmp_path / ".zshrc"
    assert depart.file_key(dest) != depart.symlink_key(dest)


def test_key_type_extracts_the_type_prefix():
    assert depart.key_type("file:/home/user/.zshrc") == "file"
    assert depart.key_type("package:uv-tool/ruff") == "package"


# ── capture_file ─────────────────────────────────────────────────────────


def test_capture_file_absent_when_missing(tmp_path):
    record = depart.capture_file(tmp_path / "nope")
    assert record == {"state": "absent"}


def test_capture_file_present_records_size_and_sha256(tmp_path):
    path = tmp_path / "f.txt"
    path.write_bytes(b"hello")
    record = depart.capture_file(path)
    assert record["state"] == "present"
    assert record["size"] == 5
    import hashlib

    assert record["sha256"] == hashlib.sha256(b"hello").hexdigest()


def test_capture_file_with_blob_dir_writes_blob_and_records_digest(tmp_path):
    state_dir = tmp_path / "state"
    path = tmp_path / "f.txt"
    path.write_bytes(b"hello")
    record = depart.capture_file(path, blob_dir=state_dir)
    assert record["blob"] == depart.write_blob(state_dir, b"hello")
    assert depart.read_blob(state_dir, record["blob"]) == b"hello"


def test_capture_file_without_blob_dir_has_no_blob_key(tmp_path):
    path = tmp_path / "f.txt"
    path.write_bytes(b"hello")
    record = depart.capture_file(path)
    assert "blob" not in record


def test_capture_file_absent_with_blob_dir_writes_no_blob(tmp_path):
    state_dir = tmp_path / "state"
    record = depart.capture_file(tmp_path / "nope", blob_dir=state_dir)
    assert record == {"state": "absent"}
    assert not state_dir.exists()


def test_capture_file_treats_a_symlink_as_absent(tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("x")
    link = tmp_path / "link.txt"
    link.symlink_to(target)
    assert depart.capture_file(link) == {"state": "absent"}


def test_capture_file_unknown_on_permission_error(tmp_path, monkeypatch):
    path = tmp_path / "f.txt"
    path.write_bytes(b"x")

    def boom(self, *a, **k):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "read_bytes", boom)
    assert depart.capture_file(path) == {"state": "unknown"}


# ── capture_symlink ──────────────────────────────────────────────────────


def test_capture_symlink_absent_for_a_plain_file(tmp_path):
    path = tmp_path / "f.txt"
    path.write_text("x")
    assert depart.capture_symlink(path) == {"state": "absent"}


def test_capture_symlink_present_records_target_text(tmp_path):
    link = tmp_path / "link"
    link.symlink_to("/some/target")
    record = depart.capture_symlink(link)
    assert record == {"state": "present", "target": "/some/target"}


# ── capture_directory ────────────────────────────────────────────────────


def test_capture_directory_present_and_absent(tmp_path):
    d = tmp_path / "d"
    assert depart.capture_directory(d) == {"state": "absent"}
    d.mkdir()
    assert depart.capture_directory(d) == {"state": "present"}


def test_capture_directory_treats_a_symlinked_dir_as_absent(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    assert depart.capture_directory(link) == {"state": "absent"}


# ── ancestor directories (mkdir(parents=True) coverage) ─────────────────


def test_ancestor_directories_walks_up_to_but_excluding_home(tmp_path):
    home = tmp_path / "home"
    path = home / ".local" / "bin" / "nvim"
    assert depart.ancestor_directories(path, home) == [
        home / ".local" / "bin",
        home / ".local",
    ]


def test_ancestor_directories_direct_child_of_home(tmp_path):
    home = tmp_path / "home"
    path = home / ".zshrc"
    assert depart.ancestor_directories(path, home) == []


def test_ancestor_directories_outside_home_is_empty(tmp_path):
    home = tmp_path / "home"
    other = tmp_path / "elsewhere" / "deep" / "file"
    assert depart.ancestor_directories(other, home) == []


# ── tree manifest (Neovim prefix / Nerd Font) ───────────────────────────


def test_tree_manifest_absent_for_missing_root(tmp_path):
    assert depart.capture_tree_manifest(tmp_path / "nope") == {"state": "absent"}


def test_tree_manifest_captures_files_symlinks_and_empty_dirs(tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    (root / "bin").mkdir()
    (root / "bin" / "nvim").write_bytes(b"binary")
    (root / "empty").mkdir()
    (root / "link").symlink_to(root / "bin" / "nvim")

    manifest = depart.capture_tree_manifest(root)
    assert manifest["state"] == "present"
    entries = manifest["entries"]
    assert "dir:bin" in entries
    assert "dir:empty" in entries
    assert "file:bin/nvim" in entries
    assert entries["file:bin/nvim"]["size"] == 6
    assert entries["symlink:link"] == str(root / "bin" / "nvim")


def test_tree_manifest_matches_true_for_identical_tree(tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    (root / "f").write_bytes(b"x")
    recorded = depart.capture_tree_manifest(root)
    assert depart.tree_manifest_matches(root, recorded) is True


def test_tree_manifest_matches_false_after_any_change(tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    (root / "f").write_bytes(b"x")
    recorded = depart.capture_tree_manifest(root)
    (root / "f").write_bytes(b"changed")
    assert depart.tree_manifest_matches(root, recorded) is False


def test_tree_manifest_matches_false_when_recorded_state_is_not_present(tmp_path):
    root = tmp_path / "tree"
    root.mkdir()
    recorded = {"state": "unknown"}
    assert depart.tree_manifest_matches(root, recorded) is False


# ── post-install tree snapshots (what gates wholesale removal) ───────────


def _installed_tree(tmp_path):
    root = tmp_path / "fonts"
    root.mkdir()
    (root / "a.ttf").write_bytes(b"font-a")
    (root / "sub").mkdir()
    (root / "sub" / "b.ttf").write_bytes(b"font-b")
    return root


def test_verdict_unchanged_for_an_untouched_tree(tmp_path):
    root = _installed_tree(tmp_path)
    baseline = depart.Baseline()
    depart.record_installed_tree(baseline, root)
    assert depart.installed_tree_verdict(baseline, root) == depart.TREE_UNCHANGED


def test_verdict_modified_when_a_file_is_added(tmp_path):
    """The data-loss case: a font the user dropped in after installing."""
    root = _installed_tree(tmp_path)
    baseline = depart.Baseline()
    depart.record_installed_tree(baseline, root)

    (root / "my-own.ttf").write_bytes(b"mine")
    assert depart.installed_tree_verdict(baseline, root) == depart.TREE_MODIFIED


def test_verdict_modified_when_an_existing_file_is_edited(tmp_path):
    root = _installed_tree(tmp_path)
    baseline = depart.Baseline()
    depart.record_installed_tree(baseline, root)

    (root / "sub" / "b.ttf").write_bytes(b"edited")
    assert depart.installed_tree_verdict(baseline, root) == depart.TREE_MODIFIED


def test_verdict_modified_when_a_file_is_removed(tmp_path):
    root = _installed_tree(tmp_path)
    baseline = depart.Baseline()
    depart.record_installed_tree(baseline, root)

    (root / "a.ttf").unlink()
    assert depart.installed_tree_verdict(baseline, root) == depart.TREE_MODIFIED


def test_verdict_unrecorded_when_no_snapshot_was_ever_taken(tmp_path):
    """Baselines captured before snapshotting existed must not be removable."""
    root = _installed_tree(tmp_path)
    assert depart.installed_tree_verdict(depart.Baseline(), root) == (
        depart.TREE_UNRECORDED
    )


def test_record_installed_tree_overwrites_on_reinstall(tmp_path):
    """The most recent install is what departure should expect to find."""
    root = _installed_tree(tmp_path)
    baseline = depart.Baseline()
    depart.record_installed_tree(baseline, root)

    (root / "c.ttf").write_bytes(b"added by a later install")
    depart.record_installed_tree(baseline, root)
    assert depart.installed_tree_verdict(baseline, root) == depart.TREE_UNCHANGED


def test_remove_manifest_tree_removes_and_returns_unchanged(tmp_path):
    root = _installed_tree(tmp_path)
    baseline = depart.Baseline()
    depart.record_installed_tree(baseline, root)

    verdict = depart.remove_manifest_tree(baseline, root)
    assert verdict == depart.TREE_UNCHANGED
    assert not root.exists()


def test_remove_manifest_tree_leaves_modified_tree_in_place(tmp_path):
    root = _installed_tree(tmp_path)
    baseline = depart.Baseline()
    depart.record_installed_tree(baseline, root)
    (root / "my-own.ttf").write_bytes(b"mine")

    verdict = depart.remove_manifest_tree(baseline, root)
    assert verdict == depart.TREE_MODIFIED
    assert root.exists()
    assert (root / "my-own.ttf").exists()


def test_remove_manifest_tree_leaves_unrecorded_tree_in_place(tmp_path):
    root = _installed_tree(tmp_path)
    baseline = depart.Baseline()

    verdict = depart.remove_manifest_tree(baseline, root)
    assert verdict == depart.TREE_UNRECORDED
    assert root.exists()


def test_installed_trees_survive_a_save_load_roundtrip(tmp_path):
    root = _installed_tree(tmp_path)
    baseline = depart.Baseline()
    depart.record_installed_tree(baseline, root)

    depart.save_baseline(tmp_path / "state", baseline)
    loaded = depart.load_baseline(tmp_path / "state")

    assert loaded is not None
    assert depart.installed_tree_verdict(loaded, root) == depart.TREE_UNCHANGED
    (root / "later.ttf").write_bytes(b"x")
    assert depart.installed_tree_verdict(loaded, root) == depart.TREE_MODIFIED


def test_baseline_without_installed_trees_key_loads_clean(tmp_path):
    """An older baseline.json predating this field must still parse."""
    state = tmp_path / "state"
    state.mkdir()
    depart.baseline_path(state).write_text(
        json.dumps({"version": 1, "layers": [], "transactions": []}),
        encoding="utf-8",
    )
    loaded = depart.load_baseline(state)
    assert loaded is not None
    assert loaded.installed_trees == {}


# ── runtime:<root> (NVM) — lightweight, top-level-only divergence ───────


def test_capture_runtime_nvm_absent_when_no_nvm_dir(tmp_path):
    assert depart.capture_runtime_nvm(tmp_path) == {"state": "absent"}


def test_capture_runtime_nvm_present_with_installed_versions(tmp_path):
    versions = tmp_path / ".nvm" / "versions" / "node"
    versions.mkdir(parents=True)
    (versions / "v20.0.0").mkdir()
    (versions / "v18.0.0").mkdir()
    record = depart.capture_runtime_nvm(tmp_path)
    assert record == {"state": "present", "versions": ["v18.0.0", "v20.0.0"]}


def test_runtime_nvm_not_diverged_when_extra_version_added(tmp_path):
    """Normal `nvm install`/`npm install -g` use must not count as divergence."""
    versions = tmp_path / ".nvm" / "versions" / "node"
    versions.mkdir(parents=True)
    (versions / "v20.0.0").mkdir()
    recorded = depart.capture_runtime_nvm(tmp_path)

    (versions / "v22.0.0").mkdir()
    assert depart.runtime_nvm_diverged(tmp_path, recorded) is False


def test_runtime_nvm_diverged_when_installed_version_removed(tmp_path):
    versions = tmp_path / ".nvm" / "versions" / "node"
    versions.mkdir(parents=True)
    (versions / "v20.0.0").mkdir()
    recorded = depart.capture_runtime_nvm(tmp_path)

    import shutil

    shutil.rmtree(versions / "v20.0.0")
    assert depart.runtime_nvm_diverged(tmp_path, recorded) is True


def test_runtime_nvm_diverged_when_root_removed_entirely(tmp_path):
    versions = tmp_path / ".nvm" / "versions" / "node"
    versions.mkdir(parents=True)
    recorded = depart.capture_runtime_nvm(tmp_path)

    import shutil

    shutil.rmtree(tmp_path / ".nvm")
    assert depart.runtime_nvm_diverged(tmp_path, recorded) is True


def test_runtime_nvm_not_diverged_when_recorded_absent(tmp_path):
    recorded = {"state": "absent"}
    assert depart.runtime_nvm_diverged(tmp_path, recorded) is False


# ── content blobs ────────────────────────────────────────────────────────


def test_write_blob_then_read_blob_roundtrips(tmp_path):
    digest = depart.write_blob(tmp_path, b"hello world")
    assert depart.read_blob(tmp_path, digest) == b"hello world"


def test_write_blob_is_idempotent_for_identical_content(tmp_path):
    digest1 = depart.write_blob(tmp_path, b"same")
    digest2 = depart.write_blob(tmp_path, b"same")
    assert digest1 == digest2
    assert len(list(tmp_path.glob("baseline-snapshot-*.blob"))) == 1


def test_read_blob_missing_returns_none(tmp_path):
    assert depart.read_blob(tmp_path, "deadbeef" * 8) is None


# ── Baseline layering: unrecorded vs absent ─────────────────────────────


def test_first_layer_is_created_from_an_empty_baseline():
    baseline = depart.Baseline()
    baseline.add_layer("2026-08-13T00:00:00", {"file:/a": {"state": "present"}})
    assert len(baseline.layers) == 1
    assert baseline.value_for("file:/a") == {"state": "present"}


def test_absent_recorded_key_is_not_unrecorded():
    """A key captured as absent in layer 1 must not be eligible for re-capture."""
    baseline = depart.Baseline()
    baseline.add_layer("t1", {"file:/a": {"state": "absent"}})
    assert baseline.is_unrecorded("file:/a") is False


def test_supplementary_layer_only_adds_genuinely_unrecorded_keys():
    baseline = depart.Baseline()
    baseline.add_layer("t1", {"file:/a": {"state": "absent"}})
    # A later run captures /a fresh (now present) and a brand-new /b.
    baseline.add_layer(
        "t2",
        {
            "file:/a": {"state": "present", "size": 1, "sha256": "x"},
            "file:/b": {"state": "present", "size": 1, "sha256": "y"},
        },
    )
    # /a's value is still the first layer's absent — never overwritten.
    assert baseline.value_for("file:/a") == {"state": "absent"}
    assert baseline.value_for("file:/b") == {
        "state": "present",
        "size": 1,
        "sha256": "y",
    }
    assert len(baseline.layers) == 2
    assert baseline.layers[1].records == {
        "file:/b": {"state": "present", "size": 1, "sha256": "y"}
    }


def test_add_layer_with_nothing_new_does_not_append_an_empty_layer():
    baseline = depart.Baseline()
    baseline.add_layer("t1", {"file:/a": {"state": "absent"}})
    baseline.add_layer("t2", {"file:/a": {"state": "present"}})
    assert len(baseline.layers) == 1


def test_first_layer_is_immutable_even_when_value_changes_to_unknown():
    baseline = depart.Baseline()
    baseline.add_layer(
        "t1", {"file:/a": {"state": "present", "size": 1, "sha256": "x"}}
    )
    baseline.add_layer("t2", {"file:/a": {"state": "unknown"}})
    assert baseline.value_for("file:/a") == {
        "state": "present",
        "size": 1,
        "sha256": "x",
    }


def test_all_keys_unions_across_layers():
    baseline = depart.Baseline()
    baseline.add_layer("t1", {"file:/a": {"state": "absent"}})
    baseline.add_layer("t2", {"file:/b": {"state": "present"}})
    assert baseline.all_keys() == {"file:/a", "file:/b"}


def test_value_for_missing_key_is_none():
    baseline = depart.Baseline()
    assert baseline.value_for("file:/nope") is None
    assert baseline.is_unrecorded("file:/nope") is True


# ── serialization ────────────────────────────────────────────────────────


def test_save_and_load_baseline_roundtrips(tmp_path):
    baseline = depart.Baseline()
    baseline.add_layer(
        "t1", {"file:/a": {"state": "present", "size": 1, "sha256": "x"}}
    )
    baseline.transactions.append({"manager": "apt", "packages": ["eza"]})

    depart.save_baseline(tmp_path, baseline)
    loaded = depart.load_baseline(tmp_path)

    assert loaded is not None
    assert loaded.value_for("file:/a") == {"state": "present", "size": 1, "sha256": "x"}
    assert loaded.transactions == [{"manager": "apt", "packages": ["eza"]}]


def test_load_baseline_missing_file_returns_none(tmp_path):
    assert depart.load_baseline(tmp_path) is None


def test_load_baseline_empty_file_returns_none(tmp_path):
    depart.baseline_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    depart.baseline_path(tmp_path).write_text("")
    assert depart.load_baseline(tmp_path) is None


def test_load_baseline_unparseable_file_returns_none(tmp_path):
    depart.baseline_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
    depart.baseline_path(tmp_path).write_text("not json{")
    assert depart.load_baseline(tmp_path) is None


def test_save_baseline_creates_state_dir_if_missing(tmp_path):
    state_dir = tmp_path / "does" / "not" / "exist"
    depart.save_baseline(state_dir, depart.Baseline())
    assert depart.baseline_path(state_dir).is_file()


# ── preflight classification ─────────────────────────────────────────────

ABSENT = {"state": "absent"}
UNKNOWN = {"state": "unknown"}


def _present_file(sha256="x", size=1):
    return {"state": "present", "sha256": sha256, "size": size}


def test_classify_no_baseline_record_is_unresolved():
    c = depart.classify_ownership_key("file:/a", None, ABSENT)
    assert c.bucket == "unresolved"
    assert c.action is None


def test_classify_unknown_baseline_is_unresolved():
    c = depart.classify_ownership_key("file:/a", UNKNOWN, ABSENT)
    assert c.bucket == "unresolved"


def test_classify_unknown_live_is_unresolved():
    c = depart.classify_ownership_key("file:/a", ABSENT, UNKNOWN)
    assert c.bucket == "unresolved"


def test_classify_absent_baseline_still_absent_is_preserved():
    c = depart.classify_ownership_key("file:/a", ABSENT, ABSENT)
    assert c.bucket == "preserved"
    assert c.action is None


def test_classify_absent_baseline_now_present_is_owned_remove():
    c = depart.classify_ownership_key("file:/a", ABSENT, _present_file())
    assert c.bucket == "owned"
    assert c.action == "remove"


# ── needs_vscode_guard marker: must never change generic classification ──


def test_classify_vscode_guard_marked_absent_baseline_still_absent_is_preserved():
    """A needs_vscode_guard-tagged key captured absent and recaptured absent
    still classifies PRESERVED, not UNRESOLVED — a permanent regression
    guard for the property that the marker rides on the same dict
    capture_file returns and is never seen by any whole-dict comparison
    (see the meta-depart-vscode-restore grill plan, decision 7)."""
    recorded = {"state": "absent", "needs_vscode_guard": True}
    c = depart.classify_ownership_key("file:/a", recorded, ABSENT)
    assert c.bucket == "preserved"
    assert c.action is None


def test_classify_vscode_guard_marked_absent_baseline_now_present_is_owned_remove():
    recorded = {"state": "absent", "needs_vscode_guard": True}
    c = depart.classify_ownership_key("file:/a", recorded, _present_file())
    assert c.bucket == "owned"
    assert c.action == "remove"


def test_classify_present_baseline_unchanged_is_preserved():
    recorded = _present_file()
    live = _present_file()
    c = depart.classify_ownership_key("file:/a", recorded, live)
    assert c.bucket == "preserved"
    assert c.action is None


def test_classify_present_baseline_now_absent_is_drifted():
    c = depart.classify_ownership_key("file:/a", _present_file(), ABSENT)
    assert c.bucket == "drifted"
    assert c.action is None


def test_classify_present_baseline_content_changed_is_unresolved():
    """Deliberately conservative: content changes aren't guessed at here (step 4's job)."""
    recorded = _present_file(sha256="original")
    live = _present_file(sha256="changed")
    c = depart.classify_ownership_key("file:/a", recorded, live)
    assert c.bucket == "unresolved"
    assert c.action is None


def test_classify_symlink_present_unchanged_is_preserved():
    recorded = {"state": "present", "target": "/src/a"}
    live = {"state": "present", "target": "/src/a"}
    c = depart.classify_ownership_key("symlink:/a", recorded, live)
    assert c.bucket == "preserved"


def test_classify_symlink_retargeted_is_unresolved():
    recorded = {"state": "present", "target": "/src/a"}
    live = {"state": "present", "target": "/somewhere/else"}
    c = depart.classify_ownership_key("symlink:/a", recorded, live)
    assert c.bucket == "unresolved"


def test_classify_tree_manifest_matches_is_preserved():
    entries = {"file:bin/nvim": {"size": 1, "sha256": "x"}}
    recorded = {"state": "present", "entries": entries}
    live = {"state": "present", "entries": dict(entries)}
    c = depart.classify_ownership_key("directory:/prefix", recorded, live)
    assert c.bucket == "preserved"


def test_classify_tree_manifest_absent_baseline_now_present_is_owned_remove():
    entries = {"file:bin/nvim": {"size": 1, "sha256": "x"}}
    live = {"state": "present", "entries": entries}
    c = depart.classify_ownership_key("directory:/prefix", ABSENT, live)
    assert c.bucket == "owned"
    assert c.action == "remove"


def test_classify_runtime_versions_order_independent():
    recorded = {"state": "present", "versions": ["v18.0.0", "v20.0.0"]}
    live = {"state": "present", "versions": ["v20.0.0", "v18.0.0"]}
    c = depart.classify_ownership_key("runtime:/home/.nvm", recorded, live)
    assert c.bucket == "preserved"


# ── reclassify_rc_file: the append-only restore case ─────────────────────


def test_reclassify_rc_file_pure_append_is_owned_restore():
    recorded = _present_file()
    c = depart.reclassify_rc_file(
        recorded, b"export PATH=x\n", b"export PATH=x\nexport NVM_DIR=y\n"
    )
    assert c.bucket == "owned"
    assert c.action == "restore"


def test_reclassify_rc_file_unchanged_defers_to_generic_classifier():
    recorded = _present_file()
    assert depart.reclassify_rc_file(recorded, b"same\n", b"same\n") is None


def test_reclassify_rc_file_edited_in_place_is_unresolved():
    """Not a pure append (content inserted/changed mid-file) — must not guess."""
    recorded = _present_file()
    c = depart.reclassify_rc_file(
        recorded, b"line1\nline2\n", b"line1\nEDITED\nline2\n"
    )
    assert c.bucket == "unresolved"
    assert c.action is None


def test_reclassify_rc_file_missing_blob_is_unresolved():
    recorded = _present_file()
    c = depart.reclassify_rc_file(recorded, None, b"anything")
    assert c.bucket == "unresolved"


def test_reclassify_rc_file_live_missing_defers_to_generic_classifier():
    recorded = _present_file()
    assert depart.reclassify_rc_file(recorded, b"x", None) is None


def test_reclassify_rc_file_baseline_absent_defers():
    assert depart.reclassify_rc_file(ABSENT, None, b"new content") is None


def test_reclassify_rc_file_no_baseline_record_defers():
    assert depart.reclassify_rc_file(None, None, b"x") is None


# ── reclassify_symlink_destination_pair: backed-up-then-symlinked ───────


def test_reclassify_symlink_pair_matches_the_named_edge_case():
    file_recorded = _present_file()
    file_live = ABSENT
    symlink_recorded = ABSENT
    symlink_live = {"state": "present", "target": "/repo/zshrc"}
    result = depart.reclassify_symlink_destination_pair(
        file_recorded, file_live, symlink_recorded, symlink_live
    )
    assert result is not None
    file_c, symlink_c = result
    assert file_c.bucket == "owned"
    assert file_c.action == "restore"
    assert symlink_c.bucket == "owned"
    assert symlink_c.action == "remove"


def test_reclassify_symlink_pair_no_match_when_file_still_present():
    result = depart.reclassify_symlink_destination_pair(
        _present_file(), _present_file(), ABSENT, {"state": "present", "target": "/x"}
    )
    assert result is None


def test_reclassify_symlink_pair_no_match_when_symlink_was_already_baseline_present():
    result = depart.reclassify_symlink_destination_pair(
        _present_file(),
        ABSENT,
        {"state": "present", "target": "/original"},
        {"state": "present", "target": "/repo/zshrc"},
    )
    assert result is None


def test_reclassify_symlink_pair_no_match_when_nothing_changed():
    result = depart.reclassify_symlink_destination_pair(ABSENT, ABSENT, ABSENT, ABSENT)
    assert result is None


# ── departure ledger ──────────────────────────────────────────────────────


def test_ledger_records_and_reads_back_entries(tmp_path):
    ledger = depart.DepartureLedger(depart.departure_ledger_path(tmp_path))
    ledger.record("file:/a", "remove", "ok")
    ledger.record("symlink:/b", "restore", "ok")
    assert ledger.completed_keys() == {"file:/a", "symlink:/b"}


def test_ledger_empty_when_no_file(tmp_path):
    ledger = depart.DepartureLedger(depart.departure_ledger_path(tmp_path))
    assert ledger.entries() == []
    assert ledger.completed_keys() == set()


def test_ledger_clear_removes_the_file(tmp_path):
    ledger = depart.DepartureLedger(depart.departure_ledger_path(tmp_path))
    ledger.record("file:/a", "remove", "ok")
    ledger.clear()
    assert not ledger.path.exists()


def test_ledger_ignores_unparseable_trailing_line(tmp_path):
    path = depart.departure_ledger_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '{"key": "file:/a", "action": "remove", "outcome": "ok"}\ntruncated{'
    )
    ledger = depart.DepartureLedger(path)
    assert ledger.completed_keys() == {"file:/a"}


# ── advisory lock ─────────────────────────────────────────────────────────


def test_acquire_lock_succeeds_when_no_lock_exists(tmp_path):
    acquired, stale = depart.acquire_departure_lock(
        tmp_path, pid=1234, start_time_fn=lambda pid: 100
    )
    assert acquired is True
    assert stale is None
    assert depart.read_lock(depart.departure_lock_path(tmp_path)) == depart.LockInfo(
        pid=1234, start_time=100
    )


def test_acquire_lock_refuses_when_holder_is_alive(tmp_path):
    depart.acquire_departure_lock(tmp_path, pid=1234, start_time_fn=lambda pid: 100)

    def start_time_fn(pid):
        return 100 if pid == 1234 else 999

    acquired, _ = depart.acquire_departure_lock(
        tmp_path, pid=5678, start_time_fn=start_time_fn
    )
    assert acquired is False


def test_acquire_lock_reclaims_when_holder_pid_is_dead(tmp_path):
    depart.acquire_departure_lock(tmp_path, pid=1234, start_time_fn=lambda pid: 100)

    def start_time_fn(pid):
        return None if pid == 1234 else 200  # dead

    acquired, stale = depart.acquire_departure_lock(
        tmp_path, pid=5678, start_time_fn=start_time_fn
    )
    assert acquired is True
    assert stale == depart.LockInfo(pid=1234, start_time=100)
    assert depart.read_lock(depart.departure_lock_path(tmp_path)) == depart.LockInfo(
        pid=5678, start_time=200
    )


def test_acquire_lock_reclaims_on_pid_reuse_start_time_mismatch(tmp_path):
    """A live PID whose start time no longer matches is stale, not alive."""
    depart.acquire_departure_lock(tmp_path, pid=1234, start_time_fn=lambda pid: 100)

    def start_time_fn(pid):
        return 555 if pid == 1234 else 200  # same pid, different process now

    acquired, stale = depart.acquire_departure_lock(
        tmp_path, pid=5678, start_time_fn=start_time_fn
    )
    assert acquired is True
    assert stale == depart.LockInfo(pid=1234, start_time=100)


def test_is_lock_holder_alive_permission_error_means_alive():
    def boom(pid):
        raise PermissionError

    assert depart.is_lock_holder_alive(1, 100, start_time_fn=boom) is True


def test_release_lock_only_when_it_is_ours(tmp_path):
    depart.acquire_departure_lock(tmp_path, pid=1234, start_time_fn=lambda pid: 100)
    depart.release_departure_lock(tmp_path, pid=9999)  # not ours — no-op
    assert depart.read_lock(depart.departure_lock_path(tmp_path)) is not None

    depart.release_departure_lock(tmp_path, pid=1234)
    assert depart.read_lock(depart.departure_lock_path(tmp_path)) is None


def test_process_start_time_missing_pid_returns_none(tmp_path):
    assert depart.process_start_time(999999999) is None


def test_process_start_time_reads_own_pid():
    assert depart.process_start_time(os.getpid()) is not None


# ── service/linger (watchcommit) ─────────────────────────────────────────


def test_build_service_record_present_when_all_probes_answer():
    record = depart.build_service_record(enabled=False, active=False, linger=False)
    assert record == {
        "state": "present",
        "enabled": False,
        "active": False,
        "linger": False,
    }


def test_build_service_record_unknown_when_any_probe_failed():
    assert depart.build_service_record(enabled=None, active=False, linger=False) == {
        "state": "unknown"
    }


def test_classify_service_no_baseline_is_unresolved():
    live = depart.build_service_record(enabled=True, active=True, linger=True)
    c = depart.classify_service(None, live)
    assert c.bucket == "unresolved"


def test_classify_service_unknown_baseline_is_unresolved():
    live = depart.build_service_record(enabled=True, active=True, linger=True)
    c = depart.classify_service({"state": "unknown"}, live)
    assert c.bucket == "unresolved"


def test_classify_service_unknown_live_is_unresolved():
    recorded = depart.build_service_record(enabled=False, active=False, linger=False)
    c = depart.classify_service(recorded, {"state": "unknown"})
    assert c.bucket == "unresolved"


def test_classify_service_unchanged_is_preserved():
    recorded = depart.build_service_record(enabled=True, active=True, linger=True)
    live = depart.build_service_record(enabled=True, active=True, linger=True)
    c = depart.classify_service(recorded, live)
    assert c.bucket == "preserved"


def test_classify_service_enabled_by_installer_is_owned_disable():
    recorded = depart.build_service_record(enabled=False, active=False, linger=False)
    live = depart.build_service_record(enabled=True, active=True, linger=True)
    c = depart.classify_service(recorded, live)
    assert c.bucket == "owned"
    assert c.action == "disable"


def test_classify_service_unexpected_change_is_drifted():
    """Baseline already enabled, live now disabled: not this installer's doing."""
    recorded = depart.build_service_record(enabled=True, active=True, linger=True)
    live = depart.build_service_record(enabled=False, active=False, linger=True)
    c = depart.classify_service(recorded, live)
    assert c.bucket == "drifted"
