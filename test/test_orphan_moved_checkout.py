"""Orphan cleanup when the checkout moved after a link was recorded.

The install history keeps each link's ``src`` as it was at creation time. A
checkout that later moved (the flat ``~/Workspace/agent-toolkit`` to the
nested ``~/Workspace/agent-toolkit/agent-toolkit``) leaves history srcs under
the old path while reinstalls repoint the live links into the new one. The
exact-equality ownership guard then treated agent-toolkit's own orphaned
links as someone else's and never removed them.
"""

import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agent-scripts"))
sys.path.insert(0, str(REPO))

import install  # noqa: E402
import link_inspect  # noqa: E402


def _toolkit(root: Path) -> Path:
    """A minimal agent-toolkit checkout (markers plus agent-scripts/)."""
    (root / "agent-scripts").mkdir(parents=True)
    (root / "links.toml").write_text("")
    (root / "install.py").write_text("")
    (root / "agent-scripts" / "link_inspect.py").write_text("")
    return root


def _sibling_repo(root: Path) -> Path:
    """A sibling repo's checkout: the shared markers, no agent-scripts/link_inspect.py."""
    (root / "agent-scripts").mkdir(parents=True)
    (root / "links.toml").write_text("")
    (root / "install.py").write_text("")
    return root


def _record(dest: Path, src: Path) -> dict[str, object]:
    return {"kind": "symlink-created", "dest": str(dest), "src": str(src)}


def _orphans(entries: list[dict[str, object]]) -> list[Path]:
    return link_inspect.find_orphaned_links([], manifest_entries=entries)


@pytest.fixture
def moved(tmp_path):
    """The real incident: flat container dir, nested checkout, dangling link."""
    container = tmp_path / "Workspace" / "agent-toolkit"
    nested = _toolkit(container / "agent-toolkit")
    dest_dir = tmp_path / "home" / ".agent-toolkit" / "scripts"
    dest_dir.mkdir(parents=True)
    dest = dest_dir / "gone.py"
    dest.symlink_to(nested / "agent-scripts" / "gone.py")  # source deleted
    old_src = container / "agent-scripts" / "gone.py"  # pre-move flat path
    return tmp_path, nested, dest, old_src


@pytest.mark.regression(
    "orphan-cleanup-misses-links-recorded-under-a-moved-checkout",
    "assert [] == [PosixPath(",
)
def test_moved_checkout_link_is_an_orphan(moved):
    _tmp, _nested, dest, old_src = moved
    assert _orphans([_record(dest, old_src)]) == [dest]


def test_exact_src_match_still_an_orphan(moved):
    _tmp, nested, dest, _old = moved
    assert _orphans([_record(dest, nested / "agent-scripts" / "gone.py")]) == [dest]


def test_target_in_sibling_repo_checkout_is_not_ours(tmp_path):
    sib = _sibling_repo(tmp_path / "sibling")
    dest = tmp_path / "link.py"
    dest.symlink_to(sib / "agent-scripts" / "gone.py")
    old_src = tmp_path / "old" / "agent-scripts" / "gone.py"
    assert _orphans([_record(dest, old_src)]) == []


def test_target_outside_any_checkout_is_not_ours(tmp_path):
    (tmp_path / "loose" / "agent-scripts").mkdir(parents=True)
    dest = tmp_path / "link.py"
    dest.symlink_to(tmp_path / "loose" / "agent-scripts" / "gone.py")
    old_src = tmp_path / "old" / "agent-scripts" / "gone.py"
    assert _orphans([_record(dest, old_src)]) == []


def test_single_component_tail_is_not_enough(tmp_path):
    toolkit = _toolkit(tmp_path / "toolkit")
    dest = tmp_path / "link"
    dest.symlink_to(toolkit / "gone.py")  # tail is just "gone.py"
    assert _orphans([_record(dest, tmp_path / "old" / "gone.py")]) == []


def test_nested_sibling_repo_inside_toolkit_is_not_ours(tmp_path):
    toolkit = _toolkit(tmp_path / "toolkit")
    sib = _sibling_repo(toolkit / "vendor" / "sibling")
    dest = tmp_path / "link.py"
    dest.symlink_to(sib / "agent-scripts" / "gone.py")
    old_src = tmp_path / "old" / "vendor" / "sibling" / "agent-scripts" / "gone.py"
    assert _orphans([_record(dest, old_src)]) == []


def test_symlinked_dir_escaping_the_checkout_is_not_ours(tmp_path):
    toolkit = _toolkit(tmp_path / "toolkit")
    outside = tmp_path / "outside" / "agent-scripts"
    outside.mkdir(parents=True)
    (toolkit / "escape").symlink_to(tmp_path / "outside")
    dest = tmp_path / "link.py"
    dest.symlink_to(toolkit / "escape" / "agent-scripts" / "gone.py")
    old_src = tmp_path / "old" / "agent-scripts" / "gone.py"
    assert _orphans([_record(dest, old_src)]) == []


def test_final_component_symlink_escaping_is_not_ours(tmp_path):
    toolkit = _toolkit(tmp_path / "toolkit")
    sib = _sibling_repo(tmp_path / "sibling")
    (sib / "agent-scripts" / "x.py").write_text("")
    (toolkit / "agent-scripts" / "x.py").symlink_to(sib / "agent-scripts" / "x.py")
    dest = tmp_path / "link.py"
    dest.symlink_to(toolkit / "agent-scripts" / "x.py")
    old_src = tmp_path / "old" / "agent-scripts" / "x.py"
    assert _orphans([_record(dest, old_src)]) == []


def test_missing_parent_is_not_ours(tmp_path):
    toolkit = _toolkit(tmp_path / "toolkit")
    dest = tmp_path / "link.py"
    dest.symlink_to(toolkit / "gone-dir" / "sub" / "gone.py")
    old_src = tmp_path / "old" / "gone-dir" / "sub" / "gone.py"
    assert _orphans([_record(dest, old_src)]) == []


def test_recorded_src_with_extra_components_inside_a_checkout_is_not_ours(tmp_path):
    toolkit = _toolkit(tmp_path / "toolkit")
    old_toolkit = _toolkit(tmp_path / "old-toolkit")
    dest = tmp_path / "link.py"
    dest.symlink_to(toolkit / "agent-scripts" / "gone.py")
    old_src = old_toolkit / "vendor" / "agent-scripts" / "gone.py"
    assert _orphans([_record(dest, old_src)]) == []


def test_recorded_root_behind_an_alias_into_a_checkout_is_not_ours(tmp_path):
    toolkit = _toolkit(tmp_path / "toolkit")
    old_toolkit = _toolkit(tmp_path / "old-toolkit")
    (old_toolkit / "vendor").mkdir()
    (tmp_path / "alias").symlink_to(old_toolkit / "vendor")
    dest = tmp_path / "link.py"
    dest.symlink_to(toolkit / "agent-scripts" / "gone.py")
    old_src = tmp_path / "alias" / "agent-scripts" / "gone.py"
    assert _orphans([_record(dest, old_src)]) == []


def test_relative_live_target_is_recognised(moved):
    tmp, nested, dest, old_src = moved
    dest.unlink()
    dest.symlink_to(os.path.relpath(nested / "agent-scripts" / "gone.py", dest.parent))
    assert _orphans([_record(dest, old_src)]) == [dest]


def test_dotdot_live_target_is_recognised(moved):
    _tmp, nested, dest, old_src = moved
    dest.unlink()
    dest.symlink_to(nested / "agent-scripts" / ".." / "agent-scripts" / "gone.py")
    assert _orphans([_record(dest, old_src)]) == [dest]


def test_symlinked_parent_resolving_into_the_checkout_is_recognised(moved):
    tmp, nested, dest, old_src = moved
    (tmp / "shortcut").symlink_to(nested)
    dest.unlink()
    dest.symlink_to(tmp / "shortcut" / "agent-scripts" / "gone.py")
    assert _orphans([_record(dest, old_src)]) == [dest]


def test_probe_oserror_fails_closed(moved, monkeypatch):
    _tmp, _nested, dest, old_src = moved

    def boom(root: Path) -> bool:
        raise PermissionError("denied")

    monkeypatch.setattr(link_inspect, "is_repo_checkout", boom)
    assert _orphans([_record(dest, old_src)]) == []


def test_unreadable_destination_is_skipped(moved, monkeypatch):
    _tmp, _nested, dest, old_src = moved
    real = os.readlink

    def flaky(path, *a, **k):
        if Path(path) == dest:
            raise OSError("cannot read")
        return real(path, *a, **k)

    monkeypatch.setattr(link_inspect.os, "readlink", flaky)
    assert _orphans([_record(dest, old_src)]) == []


def test_plain_install_cleanup_removes_the_moved_checkout_orphan(moved):
    tmp, nested, dest, old_src = moved
    home = tmp / "home"
    ctx = install.Context(
        repo_root=nested,
        home=home,
        opts=install.Options(harnesses=("claude",)),
        manifest=install.Manifest(
            home / ".local" / "state" / "agent-toolkit" / "history.jsonl",
            dry_run=False,
        ),
        reporter=install.Reporter(),
        system="Linux",
        is_wsl=False,
    )
    ctx.manifest.record_symlink(dest, old_src)
    assert install.do_check_links(ctx) == 1
    install._cleanup_orphaned_links(ctx, [])
    assert not dest.is_symlink()
    assert install.do_check_links(ctx) == 0


def test_cleanup_rechecks_ownership_before_unlinking(moved, monkeypatch):
    """A destination repointed after detection is left alone."""
    tmp, nested, dest, old_src = moved
    home = tmp / "home"
    ctx = install.Context(
        repo_root=nested,
        home=home,
        opts=install.Options(harnesses=("claude",)),
        manifest=install.Manifest(
            home / ".local" / "state" / "agent-toolkit" / "history.jsonl",
            dry_run=False,
        ),
        reporter=install.Reporter(),
        system="Linux",
        is_wsl=False,
    )
    ctx.manifest.record_symlink(dest, old_src)
    sib = _sibling_repo(tmp / "sibling")
    monkeypatch.setattr(install, "_find_orphaned_links", lambda ctx, links: [dest])
    dest.unlink()
    dest.symlink_to(sib / "agent-scripts" / "gone.py")  # someone else's now
    install._cleanup_orphaned_links(ctx, [])
    assert dest.is_symlink()


def test_any_recorded_src_for_a_dest_counts_in_detection_and_cleanup(moved):
    """Two history entries for one dest: matching either recorded src is ownership."""
    tmp, nested, dest, old_src = moved
    other = tmp / "elsewhere" / "unrelated.py"  # cannot match by tail
    entries = [_record(dest, other), _record(dest, old_src)]
    assert _orphans(entries) == [dest]
    home = tmp / "home"
    ctx = install.Context(
        repo_root=nested,
        home=home,
        opts=install.Options(harnesses=("claude",)),
        manifest=install.Manifest(
            home / ".local" / "state" / "agent-toolkit" / "history.jsonl",
            dry_run=False,
        ),
        reporter=install.Reporter(),
        system="Linux",
        is_wsl=False,
    )
    ctx.manifest.record_symlink(dest, old_src)
    ctx.manifest.record_symlink(dest, other)  # newest entry no longer matches
    install._cleanup_orphaned_links(ctx, [])
    assert not dest.is_symlink()


def test_link_vanishing_between_reads_fails_closed(moved, monkeypatch):
    _tmp, _nested, dest, old_src = moved
    real = os.readlink
    calls = {"n": 0}

    def once_then_gone(path, *a, **k):
        if Path(path) == dest:
            calls["n"] += 1
            if calls["n"] > 1:
                raise FileNotFoundError("gone")
        return real(path, *a, **k)

    monkeypatch.setattr(link_inspect.os, "readlink", once_then_gone)
    assert _orphans([_record(dest, old_src)]) in ([], [dest])
