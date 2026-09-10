#!/usr/bin/env python3
"""SessionStart hook + CLI: flag when a managed symlink on this machine no
longer points where links.toml says it should.

Why this exists
---------------
install.py's ``--check-links`` already finds this drift, and finds it in
0.1s -- but nothing ever runs it. A link can sit wrong for days, and the
symptom when it finally bites is silence: a dangling symlink means the
harness simply never loads whatever it provided, with no error anywhere.

The recurring cause is a hand-repointed link. Live verification of an
extension change tempts you to aim the installed link at the worktree you
are working in; the worktree is then removed at cleanup and the link
dangles. This has happened three times -- ``custom-footer.ts``, then
``permission-gate.ts`` and ``swarm-tool.ts`` on 2026-09-02 -- each time
losing a pi extension silently, well after the change that caused it.

Prefer ``pi -e <path>`` over repointing a link: it loads an extension for
one session and leaves no state behind to forget to undo.

What it reports
---------------
The same audit ``--check-links`` runs, condensed to one line per bucket plus
a pointer at the full audit. The audit computation is shared with install.py
through ``link_inspect.audit_links`` -- this hook shells out to nothing and
parses no audit stdout, so the two can never disagree about what counts as
drift. Silent and exit 0 when the machine is clean, so it costs a session
nothing to have running. Also silent when the audit cannot run at all
(links.toml missing or malformed): a broken checker must not itself become
a session-start warning about the checker rather than the machine.

A fingerprint cache memoizes the findings keyed on the exact state of
links.toml, this file, install.py, link_inspect.py, the history manifest,
and all managed symlink targets, reducing a repeat run to a cache read.

Usage:
    link_drift_check.py check   print a line per drifted bucket (default)

Flags
  --quiet, -q    suppress non-essential output
  --verbose, -v  emit extra diagnostic messages to stderr
"""

import argparse
import contextlib
import hashlib
import json
import os
import tempfile
from pathlib import Path

import cli_common
import link_inspect

REPO = Path(__file__).resolve().parents[1]

# Audit cache configuration. Memoizing the audit result keyed on the exact
# state of links.toml, this file, install.py, link_inspect.py, the history
# manifest, and all managed symlink targets keeps a repeat invocation from
# re-walking every destination.
_CACHE_REPO_DIRNAME: str = "agent-toolkit"
_CACHE_FILENAME: str = "link-drift-check-cache.json"
# Bumped whenever the payload shape changes; an entry without a matching
# marker is a miss, never misread across schema edits.
_CACHE_SCHEMA: int = 2

# Repo-side files whose content the findings depend on. links.toml holds the
# rows themselves; the other three are the code that interprets them (the
# hook's own assembly included) -- an edit to any invalidates the cache.
_FINGERPRINT_FILES: tuple[str, ...] = (
    "links.toml",
    "install.py",
    "agent-scripts/link_inspect.py",
    "agent-scripts/link_drift_check.py",
)


def _cache_path() -> Path:
    """Resolve the cache file path at call time — honoring
    ``$XDG_CACHE_HOME`` (tests redirect it there), falling back to
    ``~/.cache``, under this repo's own directory name."""
    root = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(root) / _CACHE_REPO_DIRNAME / _CACHE_FILENAME


def _read_cache(path: Path) -> dict[str, object]:
    """Load the cache file, or an empty dict on any read/parse problem."""
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_cache(path: Path, entries: dict[str, object]) -> None:
    """Atomically replace the cache file (temp file + rename)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=path.parent, prefix=f"{path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(entries, handle)
            os.replace(tmp, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise
    except OSError:
        pass


def _file_records(repo: Path, rel: str, records: list[object]) -> bool:
    """Append ``rel``'s (path, mtime, size) to ``records``; False on OSError.

    A repo-side file the fingerprint depends on must exist for the cache to
    be trustworthy: a missing interpreter of the audit is a changed world,
    and returning False makes the fingerprint None (uncached runs).
    """
    path = repo / rel
    try:
        st = path.stat()
    except OSError:
        return False
    records.extend([rel, st.st_mtime_ns, st.st_size])
    return True


def _fingerprint(
    specs: list[link_inspect.LinkSpec],
    repo: Path,
    home: Path,
    manifest: Path,
) -> str | None:
    """Compute a cryptographic hash representing the state everything the
    audit's findings depend on: the repo-side files in _FINGERPRINT_FILES,
    the history manifest, each ``dir=true`` row's source root, and the live
    managed symlink destinations.

    Returns None if any fingerprinted repo file cannot be stat'd. Per-link
    problems are not fatal: an unreadable destination is recorded as a
    marker in the hash (and is exactly the drift this hook exists to
    report), while a vanished manifest is recorded as "missing" -- a fresh
    machine, not a broken one.
    """
    records: list[object] = []
    for rel in _FINGERPRINT_FILES:
        if not _file_records(repo, rel, records):
            return None
    try:
        st = manifest.stat()
        records.extend([str(manifest), st.st_mtime_ns, st.st_size])
    except OSError:
        records.append([str(manifest), "missing"])
    for spec in specs:
        if spec.dir:
            root = repo / spec.src
            try:
                st = root.stat()
            except OSError:
                records.append([spec.src, "dir-missing"])
            else:
                records.extend([[spec.src, "d"], st.st_mtime_ns, st.st_size])
        dest = link_inspect.expand_dest(spec.dest, home)
        try:
            target = os.readlink(dest)
            records.append([str(dest), "l", target])
        except OSError:
            try:
                st = dest.lstat()
                records.append([str(dest), "f", st.st_mtime_ns, st.st_size])
            except OSError:
                records.append([str(dest), "m"])
    return hashlib.sha256(json.dumps(records, sort_keys=True).encode()).hexdigest()


def _machine_facts() -> tuple[bool, bool, bool]:
    """Detect this machine's platform booleans: (is_mac, is_linux, is_wsl)."""
    system = os.uname().sysname if hasattr(os, "uname") else ""
    return (system == "Darwin", system == "Linux", link_inspect.detect_wsl(system))


def _summary(buckets: dict[str, list[str]]) -> str:
    """Render the bucket summary exactly as the audit's stdout would show it.

    Buckets in CHECK_BUCKETS order as ``name (count)``, ``; ``-joined; the
    pointer at the full audit stands in when nothing parseable was found.
    """
    named = [
        f"{bucket} ({len(buckets[bucket])})"
        for bucket in link_inspect.CHECK_BUCKETS
        if buckets.get(bucket)
    ]
    return "; ".join(named) if named else "see the full audit"


def _report(findings: dict[str, list[str]], quiet: bool, repo: Path) -> None:
    summary = _summary(findings)
    cli_common.qprint(
        f"links: {summary} — run `python3 {repo}/install.py --check-links`",
        quiet=quiet,
    )


def _audit(
    specs: list[link_inspect.LinkSpec],
    managed_dirs: list[link_inspect.ManagedDirSpec],
    repo: Path,
    home: Path,
    machine: tuple[bool, bool, bool],
) -> dict[str, list[str]] | None:
    """Run the shared audit in-process, or None if it cannot run.

    Data-shaped failures (malformed links.toml — ValueError/TypeError, the
    same classification install.py's --check-links entrypoint catches) and
    environmental ones (OSError) both map to None: the hook never reports
    problems with itself. Unexpected internal errors are deliberately not
    caught — in a dev repo a bug must be visible.
    """
    is_mac, is_linux, is_wsl = machine
    try:
        findings, _foreign, _dirs = link_inspect.audit_links(
            repo_root=repo,
            home=home,
            harnesses=link_inspect.VALID_HARNESSES,
            is_mac=is_mac,
            is_linux=is_linux,
            is_wsl=is_wsl,
            profile=link_inspect.DEFAULT_PROFILE,
            manifest_file=link_inspect.manifest_path(home),
            format_path=lambda path: link_inspect.format_path(path, home),
            report_uninstalled=False,
            specs=specs,
            managed_dirs=managed_dirs,
        )
    except (OSError, ValueError, TypeError):
        return None
    return findings


def cmd_check(
    quiet: bool = False,
    *,
    repo_root: Path | None = None,
    home: Path | None = None,
    machine: tuple[bool, bool, bool] | None = None,
) -> None:
    """Print one summary line per run of drifted buckets, or nothing.

    ``repo_root``/``home``/``machine`` are injectable so tests can point the
    hook at fixture state instead of this machine; production leaves them
    all defaulted.
    """
    repo = repo_root or REPO
    home = home or Path.home()

    try:
        specs = link_inspect.load_links(repo / "links.toml")
        managed_dirs = link_inspect.load_managed_dirs(repo / "links.toml")
    except (OSError, ValueError, TypeError):
        return

    cache_path = _cache_path()
    cache = _read_cache(cache_path)
    cached_entry = cache.get("audit") if isinstance(cache, dict) else None
    fp: str | None = None
    if isinstance(cached_entry, dict) and cache.get("schema") == _CACHE_SCHEMA:
        fp = _fingerprint(specs, repo, home, link_inspect.manifest_path(home))
        if (
            fp is not None
            and cached_entry.get("fingerprint") == fp
            and isinstance(cached_entry.get("buckets"), dict)
            and isinstance(cached_entry.get("exit"), int)
        ):
            if cached_entry["exit"] == 0:
                return
            _report(cached_entry["buckets"], quiet, repo)  # type: ignore[arg-type]
            return

    findings = _audit(specs, managed_dirs, repo, home, machine or _machine_facts())
    if findings is None:
        return

    exit_code = (
        1 if any(findings[bucket] for bucket in link_inspect.CHECK_BUCKETS) else 0
    )
    if fp is None:
        fp = _fingerprint(specs, repo, home, link_inspect.manifest_path(home))
    if fp is not None:
        _write_cache(
            cache_path,
            {
                "schema": _CACHE_SCHEMA,
                "audit": {
                    "fingerprint": fp,
                    "buckets": findings,
                    "exit": exit_code,
                },
            },
        )

    if exit_code == 0:
        return
    _report(findings, quiet, repo)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Flag managed symlinks that no longer point where "
        "links.toml says they should."
    )
    # --quiet/-v are defined once, on every leaf subcommand parser only (via
    # this shared `parents=` parser) -- never on `parser` itself. See
    # dev_status.py's build_parser() for the full rationale.
    verbosity_parent = argparse.ArgumentParser(add_help=False)
    cli_common.add_verbosity_args(verbosity_parent)
    subparsers = parser.add_subparsers(dest="subcommand")
    subparsers.add_parser(
        "check",
        help="print a line per drifted bucket (default)",
        parents=[verbosity_parent],
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    cmd_check(quiet=getattr(args, "quiet", False))


if __name__ == "__main__":
    main()
