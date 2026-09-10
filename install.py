#!/usr/bin/env python3
"""install.py — agent-toolkit + AI-harness provisioner for macOS and Linux/WSL.

Ported from the zsh ``install.sh`` this repo used through mid-2026; the
shell script is now a thin bootstrap that locates a Python 3.12+ and execs
this file.

Two properties from the shell version are load-bearing and preserved here:

* **Nothing aborts the run.** There is no ``set -e`` equivalent and no
  bare ``raise`` in the install path — a blocked installer or an offline
  package mirror must not stop the steps after it. Every failure is
  collected by :class:`Reporter` and printed loudly in the end-of-run
  summary; the exit code is 1 if anything was skipped, 0 otherwise.
* **Every file mutation is recorded** to an append-only history log
  (``~/.local/state/agent-toolkit/history.jsonl`` -- a directory of its own,
  distinct from the origin repo's own state directory, so this repo's
  orphan-cleanup never treats that repo's still-wanted symlinks as its own
  stale entries) that never gets truncated, so ``--rollback`` reverses
  *every* run ever recorded, not just the most recent one. Packages are
  reported but never uninstalled.

The dotfile symlink table itself lives in ``links.toml`` next to this file,
not in code — see that file's header for the per-entry schema.

Flags
  --quiet, -q    suppress non-essential output
  --verbose, -v  emit extra diagnostic messages to stderr

Requires Python 3.12+.
"""

import argparse
import contextlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import NoReturn
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent / "agent-scripts"))

import cli_common  # noqa: E402 — sibling dir inserted above
import link_inspect  # noqa: E402 — sibling dir inserted above
import settings_seed  # noqa: E402 — sibling dir inserted above

import depart  # noqa: E402 — sibling dir on path above
import depart_exec  # noqa: E402 — sibling dir on path above

# Re-exports from link_inspect (the extracted links.toml-audit module in
# agent-scripts/): every moved name keeps resolving through install for
# existing tests and callers. Assignment style, so ruff's F401 has nothing
# to flag — these are the interface, not unused accidents.
JUNK_SUFFIXES = link_inspect.JUNK_SUFFIXES
_JUNK_SUFFIXES = link_inspect.JUNK_SUFFIXES
CHECK_BUCKET_BROKEN_SOURCE = link_inspect.CHECK_BUCKET_BROKEN_SOURCE
CHECK_BUCKET_WRONG_TARGET = link_inspect.CHECK_BUCKET_WRONG_TARGET
CHECK_BUCKET_NOT_A_SYMLINK = link_inspect.CHECK_BUCKET_NOT_A_SYMLINK
CHECK_BUCKET_ORPHANED = link_inspect.CHECK_BUCKET_ORPHANED
CHECK_BUCKET_UNMANAGED = link_inspect.CHECK_BUCKET_UNMANAGED
CHECK_BUCKET_NEVER_INSTALLED = link_inspect.CHECK_BUCKET_NEVER_INSTALLED
CHECK_BUCKETS = link_inspect.CHECK_BUCKETS
LinkSpec = link_inspect.LinkSpec
ManagedDirSpec = link_inspect.ManagedDirSpec
expand_dest = link_inspect.expand_dest
_is_symlink = link_inspect.is_symlink
_path_exists = link_inspect.path_exists
_link_target = link_inspect.link_target
_same_path = link_inspect.same_path
_implied_repo_root = link_inspect.implied_repo_root
_is_repo_checkout = link_inspect.is_repo_checkout
_is_main_checkout = link_inspect.is_main_checkout

# Re-exports from settings_seed (the extracted copy-once-settings module in
# agent-scripts/): every moved name keeps resolving through install for
# existing tests and callers — same assignment style as the link_inspect
# block above. Palette/color_enabled/PALETTE moved to cli_common, which both
# install.py and settings_seed.py import; main() mutates PALETTE.enabled on
# that single canonical object rather than rebinding the name.
PALETTE = cli_common.PALETTE
Palette = cli_common.Palette
color_enabled = cli_common.color_enabled
json_key_drift = settings_seed.json_key_drift
_BYPASS_BASH_PATTERNS = settings_seed._BYPASS_BASH_PATTERNS
opencode_bypass_drift = settings_seed.opencode_bypass_drift
_bash_permissions = settings_seed._bash_permissions
_load_json_pair_text = settings_seed._load_json_pair_text
_describe_settings_text = settings_seed._describe_settings_text
_describe_opencode_text = settings_seed._describe_opencode_text
_describe_vscode_text = settings_seed._describe_vscode_text
describe_settings_drift = settings_seed.describe_settings_drift
describe_opencode_drift = settings_seed.describe_opencode_drift
describe_vscode_drift = settings_seed.describe_vscode_drift
_load_json_pair = settings_seed._load_json_pair
seed_file = settings_seed.seed_file
_adopt_seed = settings_seed._adopt_seed
_normalize_seed_text = settings_seed._normalize_seed_text
_adopt_git_reason = settings_seed._adopt_git_reason
_adopt_file = settings_seed._adopt_file
_opencode_adopt_blocker = settings_seed._opencode_adopt_blocker
_reseed_file = settings_seed._reseed_file

# VALID_HARNESSES/VALID_PROFILES re-export from link_inspect rather than a
# second literal here: install.py used to keep its own copy alongside
# link_inspect's, and that duplication is exactly what let link_inspect's
# copy go stale (missing "codex") while this one was updated for the Codex
# harness landing — the same class of drift atk-settings-unvendor-drift-
# helpers fixed for the settings hook. link_inspect.VALID_HARNESSES now
# includes "codex" too, so there is one source of truth again.
VALID_HARNESSES = link_inspect.VALID_HARNESSES
VALID_PROFILES = link_inspect.VALID_PROFILES
DEFAULT_PROFILE = link_inspect.DEFAULT_PROFILE
load_links = link_inspect.load_links
load_managed_dirs = link_inspect.load_managed_dirs
detect_wsl = link_inspect.detect_wsl
manifest_path = link_inspect.manifest_path
read_manifest_entries = link_inspect.read_manifest_entries
format_path = link_inspect.format_path
audit_links = link_inspect.audit_links
dir_applies = link_inspect.dir_applies

USAGE = """\
usage: ./install.sh --harness=<claude,copilot,opencode,agy,pi,codex>[,...] [--profile=personal|work] [--rollback] [--wipe] [--force] [--dry-run] [--reseed | --adopt] [--quiet | --verbose]
       ./install.sh --depart [--yes] [--dry-run] [--quiet | --verbose]
       ./install.sh --check-links [--harness=...] [--profile=personal|work] [--quiet | --verbose]

  --quiet, -q   suppress non-essential output
  --verbose, -v emit extra diagnostic messages to stderr
  --harness   required for an install run; not needed by the undo/audit
              actions (--rollback, --depart, --check-links), though
              --check-links accepts it to scope which entries apply.
              Comma-separated, at least one of:
              claude, copilot, opencode, agy, pi, codex. No default — every run must
              state its intent explicitly. Purely additive: omitting a harness
              you previously selected does NOT uninstall or clean it up,
              it just skips re-provisioning it this run. Removal is a
              --rollback concern (reverses every run recorded in the
              history file, not just the most recent one) or manual cleanup.
  --profile   personal (default) or work. Controls machine-level concerns:
              excludes personal API-key setup, seeds
              tightened settings where a profile-specific variant exists
              (settings.work.json), and excludes opencode entirely — it is
              never installed on a work machine, regardless of --harness.
              Otherwise never restricts which harness(es) you can choose —
              --profile=work --harness=claude is honored as stated.
  --rollback  reverse every file mutation (symlinks, copies, backups) ever
              recorded across all past runs, using the history file, then
              exit. Not limited to the most recent run — running install.sh
              several times over weeks and then rolling back undoes all of
              it in one shot, oldest run included. Packages are reported
              but never uninstalled. Must be used alone (no --harness,
              --profile, or --force) — except --dry-run and --wipe, see
              below.
  --wipe      modifier for --rollback: instead of restoring the original
              pre-install files from their .bak backups, deletes the
              backups outright, so nothing installer-related is left behind.
              Also sweeps untracked state the installer creates but never
              records in its history — Neovim's XDG state dirs
              (~/.local/share/nvim, ~/.local/state/nvim, ~/.cache/nvim) and,
              on Linux, every managed systemd --user service (disabled
              and stopped). These are NOT where a Neovim binary itself
              belongs — a self-contained Neovim install (its share/nvim/
              runtime tree) must live outside ~/.local/share/nvim (a
              self-contained prefix such as ~/.local/opt/neovim), or
              --wipe deletes it along with everything else here. Packages
              are still never touched. Requires --rollback.
  --force     override the work-profile guard on a machine previously
              provisioned with --profile=work
  --dry-run   print what every step would do without doing it: no packages
              installed, no files written/symlinked/removed, no history
              written. Detection (what's already installed, which
              profile/harness branches apply) still runs for real, so the
              preview reflects actual machine state. The one flag allowed
              alongside --rollback, to preview an undo before running it.
  --reseed    force an overwrite of drifted copy-once seeds (VS Code
              settings.json/keybindings.json, Claude Code settings.json,
              opencode.jsonc, Pi settings.json) with the repo's current
              version, instead of only reporting drift. The pre-existing
              file is backed up to
              <name>.bak once, the first time a given file is reseeded;
              later reseeds of the same file reuse that backup rather than
              overwriting it again. Cannot be combined with --rollback.
  --adopt     copy every drifted live copy-once seed back into the repository
              (the reverse of --reseed), scoped to the selected harnesses and
              WSL VS Code files. The repo seed must be tracked and clean;
              adoption creates no backup or history entry and leaves the seed
              dirty by design, so commit it before adopting another edit.
              Missing live files are left missing. Empty, unreadable, dirty,
              untracked, or unparseable opencode.jsonc files are skipped;
              live opencode allowlist bypasses are refused. Cannot be combined
              with --reseed, --rollback, --depart, or --check-links.
  --depart    remove or restore everything a past install run (future
              installs only — this reasons entirely from a baseline
              recorded at install time, not from history.jsonl) owns on
              this machine: files, symlinks, packages, runtimes, and
              services. Must be used alone — the only other flags allowed
              alongside it are --yes and --dry-run. Refuses with no baseline
              recorded (exit 2). Prints a full preflight report and, on a
              real (non-dry-run) run, requires typing the exact token
              DEPART to proceed unless --yes is passed. This is
              local-footprint cleanup, not forensic erasure — see README.md
              for the separate, genuinely destructive WSL unregister/
              recreate path for a guaranteed pristine reset.
  --yes       skip --depart's interactive confirmation prompt. Only valid
              alongside --depart.
  --check-links
              audit the live symlinks against links.toml and exit. Strictly
              read-only: nothing is created, removed, or repointed, so this
              is safe to run at any time. Reports several buckets —
              broken-source (the link is correct but its repo file is
              gone), wrong-target (the link points somewhere other than
              links.toml says), not-a-symlink (a real file sits where a
              link belongs), orphaned (a symlink an earlier run recorded
              in the history that no links.toml entry produces anymore,
              e.g. the entry was deleted or its dest renamed), and
              unmanaged (a file sitting in a directory links.toml declares
              exclusive via a [[managed_dir]] row, that no [[link]] row
              produces) — none of which a plain re-run surfaces, since
              symlink() happily creates a dangling link and never revisits
              a dest that links.toml stopped mentioning. --harness and
              --profile scope which entries are considered; with no
              --harness, every harness's entries are checked, which cannot
              produce false positives because every bucket requires the
              destination to already exist on disk. Links pointing at the
              same file in a different checkout of this repo — the normal
              state of affairs when auditing from a worktree — are
              collapsed into a single informational note instead of one
              finding each, and do not affect the exit code. --report-
              uninstalled additionally reports never-installed: an
              applicable row whose repo source exists but whose
              destination was never linked here at all, as opposed to one
              that was linked once and later removed, which stays silent
              either way; off by default, since a machine that simply
              hasn't run install.sh yet for some entries is not a defect.
              No other flag may be combined with --check-links.
              Exits 0 when nothing is wrong, 1 when any bucket is
              non-empty, 2 if links.toml itself cannot be read.

Examples:
  ./install.sh --harness=claude
  ./install.sh --profile=work --harness=copilot
  ./install.sh --harness=claude,opencode
  ./install.sh --harness=claude,agy
  ./install.sh --dry-run --harness=claude
  ./install.sh --dry-run --rollback
  ./install.sh --rollback --wipe        # full rollback to a blank slate
  ./install.sh --check-links            # read-only symlink audit
  ./install.sh --check-links --report-uninstalled  # also flag never-installed links

Exits 0 if every step ran, 1 if any step was skipped (see summary)."""


# ── skip-and-report plumbing ──────────────────────────────────────────────────


# Serializes whole-line console output: two threads printing half-lines at
# once would garble the terminal. list.append-style mutations stay lock-free
# (GIL-atomic); this only guards print statements.
_io_lock = threading.Lock()


@dataclass
class Reporter:
    """Collects every step that didn't run, for the end-of-run summary.

    Install steps and rollback steps each get their own reporter so their
    tallies (and exit codes) stay separate, mirroring the shell version's
    two arrays.
    """

    skipped: list[str] = field(default_factory=list)

    def skip(self, step: str, reason: str) -> None:
        """Record and print a skipped install step.

        Args:
            step: What was being attempted, e.g. ``"apt package: eza"``.
            reason: Why it didn't happen, in user-facing prose.
        """
        self.note(f"{step} — {reason}")

    def note(self, message: str) -> None:
        """Record and print an already-formatted skip message."""
        self.skipped.append(message)
        with _io_lock:
            print(PALETTE.warn(f"  !! SKIPPED: {message}"))

    def __len__(self) -> int:
        return len(self.skipped)


# ── run history (drives --rollback) ───────────────────────────────────────────


@dataclass
class Manifest:
    """Append-only JSON Lines history of every file mutation, across all runs.

    One JSON object per line, never truncated at the start of a run — that
    is what makes ``--rollback`` a full-history undo rather than a
    last-run-only one. A completed (non-dry-run) rollback deletes the file,
    which is correct: at that point every recorded mutation really has been
    undone, so the next run starts from a genuinely fresh history instead of
    one padded with already-reversed entries.

    Entry kinds:
        ``run``               ``timestamp``, ``profile``
        ``symlink-created``   ``dest``, ``src`` (the link's recorded target)
        ``file-copied``       ``dest``
        ``file-backed-up``    ``dest``, ``backup``
        ``package-installed`` ``name`` (historical ledgers only — no longer
                              written, but ``--rollback`` still reports them)
    """

    path: Path
    dry_run: bool = False
    _append_lock: threading.Lock = field(
        default_factory=threading.Lock, repr=False, compare=False
    )

    def init_run(self, profile: str, quiet: bool = False) -> None:
        """Open a new run in the history (or preview doing so)."""
        if self.dry_run:
            cli_common.qprint(
                PALETTE.dim(f"  [dry-run] would record a new run in {self.path}"),
                quiet=quiet,
            )
            return
        stamp = datetime.now().astimezone().isoformat(timespec="seconds")
        self._append({"kind": "run", "timestamp": stamp, "profile": profile})

    def record_symlink(self, dest: Path, src: Path) -> None:
        """Record a symlink this run created, along with what it points at."""
        self._record({"kind": "symlink-created", "dest": str(dest), "src": str(src)})

    def record_copy(self, dest: Path) -> None:
        """Record a file this run created by copying or downloading."""
        self._record({"kind": "file-copied", "dest": str(dest)})

    def record_backup(self, dest: Path, backup: Path) -> None:
        """Record a pre-existing file this run moved aside."""
        self._record(
            {"kind": "file-backed-up", "dest": str(dest), "backup": str(backup)}
        )

    def entries(self) -> list[dict[str, object]]:
        """Read every recorded entry, oldest first.

        Unparseable lines are dropped rather than raising: a truncated last
        line (power loss mid-append) must not make the whole history
        unrollbackable. A missing file (no run has ever recorded anything
        yet) returns an empty list rather than raising ``FileNotFoundError``
        — callers on the normal (non-rollback) install path, like
        ``--reseed``'s ``has_backup`` lookup, hit this on a fresh machine
        and have no external existence guard of their own.
        """
        if not self.path.is_file():
            return []
        entries: list[dict[str, object]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                entries.append(parsed)
        return entries

    def remove_symlink_entries(self, dests: set[Path]) -> None:
        """Drop every recorded ``symlink-created`` entry for the given destinations.

        Used by the automatic orphan-cleanup pass: those symlinks are gone
        from disk, so leaving their entries would make a later
        ``--rollback`` try (harmlessly, but confusingly) to remove
        something that no longer exists. A whole-file rewrite rather than
        an append, since this drops entries instead of adding one — same
        temp-file + ``os.replace`` convention as :func:`_adopt_file`.
        """
        if self.dry_run or not self.path.is_file():
            return
        dest_strs = {str(d) for d in dests}
        kept = [
            entry
            for entry in self.entries()
            if not (
                entry.get("kind") == "symlink-created"
                and entry.get("dest") in dest_strs
            )
        ]
        fd, temp_name = tempfile.mkstemp(prefix=".history.jsonl-", dir=self.path.parent)
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                for entry in kept:
                    handle.write(json.dumps(entry) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, self.path)
        except OSError:
            temp_path.unlink(missing_ok=True)
            raise

    def has_backup(self, dest: Path) -> bool:
        """Return whether a ``file-backed-up`` entry exists for ``dest``.

        The source of truth for "has this dest's true original already been
        preserved," not merely whether a ``<name>.bak`` file happens to
        exist on disk — see ``seed_file``'s reseed branch.
        """
        return any(
            entry.get("kind") == "file-backed-up" and entry.get("dest") == str(dest)
            for entry in self.entries()
        )

    def _record(self, entry: dict[str, object]) -> None:
        if self.dry_run:
            return
        self._append(entry)

    def _append(self, entry: dict[str, object]) -> None:
        """Durably append one JSON object as a line.

        Appends (rather than the temp-file + ``os.replace`` dance the repo's
        other Python scripts use for whole-file writes) because a single
        ``write`` of one short line under ``O_APPEND`` is already atomic
        with respect to other appenders; flush + fsync is what makes it
        survive a crash. The containing directory is fsynced the first time
        the file is created so the new directory entry is durable too.
        The lock is defensive: each call opens its own handle, so concurrent
        appends can't interleave each other's buffers anyway, but threads are
        a supported caller pattern and the cost is negligible.
        """
        with self._append_lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            is_new = not self.path.exists()
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            if is_new:
                _fsync_dir(self.path.parent)


def _fsync_dir(directory: Path) -> None:
    """fsync a directory so a newly created entry within it is durable."""
    fd = os.open(str(directory), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# ── options and run context ───────────────────────────────────────────────────


@dataclass(frozen=True)
class Options:
    """Validated command-line options for one invocation."""

    harnesses: tuple[str, ...] = ()
    profile: str = "personal"
    rollback: bool = False
    force: bool = False
    dry_run: bool = False
    wipe: bool = False
    reseed: bool = False
    adopt: bool = False
    depart: bool = False
    yes: bool = False
    check_links: bool = False
    report_uninstalled: bool = False
    quiet: bool = False
    verbose: bool = False
    force_harness: bool = False


@dataclass
class Context:
    """Everything a step needs: paths, options, history, and the skip tally."""

    repo_root: Path
    home: Path
    opts: Options
    manifest: Manifest
    reporter: Reporter
    system: str
    is_wsl: bool
    neovim_fallback_failure: str | None = None
    # Set by capture_departure_baseline (Linux, non-dry-run only); package/
    # npm-harness installers record transactions onto it as they run, and
    # run_install saves it back to baseline.json once, after every step.
    departure_baseline: depart.Baseline | None = None

    @property
    def state_dir(self) -> Path:
        """Where the history log and profile marker live.

        Derived from the manifest's own path rather than re-hardcoding the
        directory name a second time -- two independent literals here and
        in ``build_context`` previously had to be kept in sync by hand,
        exactly the kind of duplication that let this directory's name
        drift out of agreement with itself. Invariant this relies on:
        every ``Manifest`` this codebase constructs lives directly inside
        the state directory (``state_dir / "history.jsonl"``, never
        nested deeper or pointed elsewhere) -- confirmed true of every
        real call site as of this writing; a future caller that breaks
        this invariant would silently corrupt ``state_dir`` too.
        """
        return self.manifest.path.parent

    @property
    def profile_marker(self) -> Path:
        """Marker file recording that this machine was provisioned as work."""
        return self.state_dir / "profile"

    @property
    def is_mac(self) -> bool:
        return self.system == "Darwin"

    @property
    def is_linux(self) -> bool:
        return self.system == "Linux"

    def has_harness(self, name: str) -> bool:
        """Return whether ``name`` was named in ``--harness`` this run."""
        return name in self.opts.harnesses

    def display(self, path: Path) -> str:
        """Render ``path`` with the home directory shortened back to ``~``."""
        return link_inspect.format_path(path, self.home)


def build_context(opts: Options, repo_root: Path | None = None) -> Context:
    """Assemble a :class:`Context` for a real run on this machine."""
    root = repo_root or Path(__file__).resolve().parent
    home = Path.home()
    system = platform.system()
    # The manifest path lives in link_inspect.manifest_path (the auditor
    # resolves the same constant) -- the "agent-toolkit" directory name is
    # load-bearing: both repos' install.py write a symlink-creation manifest
    # under a shared state directory, and orphan-cleanup
    # (install_symlinks' _cleanup_orphaned_links, run on every plain
    # install) deletes any manifest-recorded symlink this repo's own
    # links.toml doesn't produce. A shared state directory means each
    # repo's install treats the OTHER repo's still-wanted symlinks as its
    # own stale orphans and deletes them -- reproduced directly: a fresh
    # machine with the origin repo installed, then agent-toolkit installed
    # on top, loses the origin repo's personal-only scripts (dev_status_sync.py,
    # watchcommit_activity.py, herdr_delegate.py,
    # opencode_skills_sync_activity.py) with no warning under --quiet.
    # Giving agent-toolkit its own state directory makes this permanently
    # impossible, not just during a one-time cutover.
    return Context(
        repo_root=root,
        home=home,
        opts=opts,
        manifest=Manifest(manifest_path(home), dry_run=opts.dry_run),
        reporter=Reporter(),
        system=system,
        is_wsl=detect_wsl(system),
    )


# ── argument parsing ──────────────────────────────────────────────────────────


class _Parser(argparse.ArgumentParser):
    """ArgumentParser whose own errors exit 2 with the usage text, like the
    hand-rolled shell parser this replaces."""

    def error(self, message: str) -> NoReturn:
        """Route argparse's own parse failures through the shared exit path."""
        _fail(message, show_usage=True)


def _fail(message: str, *, show_usage: bool = False) -> NoReturn:
    """Print an argument error and exit 2, matching the shell version."""
    print(PALETTE.error(message), file=sys.stderr)
    if show_usage:
        print(USAGE, file=sys.stderr)
    raise SystemExit(2)


HARNESS_BINARIES: dict[str, str] = {
    "claude": "claude",
    "copilot": "copilot",
    "opencode": "opencode",
    "agy": "agy",
    "pi": "pi",
    "codex": "codex",
}

HARNESS_INSTALL_HINTS: dict[str, str] = {
    "claude": "npm install -g @anthropic-ai/claude-code",
    "copilot": "npm install -g @github/copilot",
    "opencode": "curl -fsSL https://opencode.ai/install | bash",
    "agy": "internal workstation installation",
    "pi": "npm install -g @mariozechner/pi-cli",
    "codex": "npm install -g @openai/codex",
}


def check_harness_binaries(ctx: Context) -> list[str]:
    """Return error strings for requested harnesses whose binaries are not on PATH."""
    if ctx.opts.force_harness:
        return []
    errors: list[str] = []
    for harness in ctx.opts.harnesses:
        bin_name = HARNESS_BINARIES.get(harness)
        if bin_name and not have(bin_name):
            hint = HARNESS_INSTALL_HINTS.get(harness, "")
            hint_str = f" (install with: {hint})" if hint else ""
            errors.append(f"'{bin_name}' CLI binary is not installed on PATH{hint_str}")
    return errors


def parse_args(argv: Sequence[str]) -> Options:
    """Parse and validate the command line.

    argparse handles the raw flag shapes; every semantic rule below is
    checked explicitly, in the same order and with the same messages the
    shell version used, because those messages are the documented contract
    (``test/scenarios.sh`` greps for them).

    Args:
        argv: Arguments without the program name.

    Returns:
        The validated options.

    Raises:
        SystemExit: 0 for ``--help``, 2 for any argument error.
    """
    parser = _Parser(add_help=False, allow_abbrev=False)
    cli_common.add_verbosity_args(parser)
    parser.add_argument("--profile", default=DEFAULT_PROFILE)
    # append, not store: --harness=claude --harness=copilot must accumulate
    # both, not silently drop the first on the second flag.
    parser.add_argument("--harness", action="append", default=[])
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--wipe", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true")
    parser.add_argument("--reseed", action="store_true")
    parser.add_argument("--adopt", action="store_true")
    parser.add_argument("--force-harness", dest="force_harness", action="store_true")
    parser.add_argument("--depart", action="store_true")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--check-links", dest="check_links", action="store_true")
    parser.add_argument(
        "--report-uninstalled", dest="report_uninstalled", action="store_true"
    )
    parser.add_argument("-h", "--help", dest="help", action="store_true")

    args, extras = parser.parse_known_args(list(argv))

    if args.help:
        print(USAGE)
        raise SystemExit(0)

    # Unknown flags are reported before anything else, so an invocation with
    # both a typo'd flag and a bad value names the typo first — the same
    # order the shell version's parse loop produced.
    if extras:
        _fail(f"unknown argument: {extras[0]}", show_usage=True)

    if args.profile not in VALID_PROFILES:
        _fail(f"invalid --profile: {args.profile} (must be personal or work)")

    harness_set = bool(args.harness)
    harnesses: list[str] = []
    for flag_value in args.harness:
        harnesses.extend(flag_value.split(","))

    # An empty --harness= (stray trailing comma, or a copy-paste mistake)
    # gets its own message, checked ahead of the unknown-harness loop below —
    # otherwise it would be reported as an "unknown harness" with a blank name.
    for harness in harnesses:
        if not harness:
            _fail("--harness has an empty value — check for a stray comma")

    for harness in harnesses:
        if harness not in VALID_HARNESSES:
            _fail(
                f"unknown harness: {harness} "
                "(must be claude, copilot, opencode, agy, pi, and/or codex)"
            )

    # opencode never belongs on a work machine, full stop — not "tightened
    # settings," excluded entirely, the same way watchcommit is.
    if args.profile == "work" and "opencode" in harnesses:
        _fail("--harness=opencode is not allowed with --profile=work")

    if args.adopt and args.reseed:
        _fail("--adopt and --reseed cannot be used together")

    # --depart is a standalone, undo-everything action, checked first (ahead
    # of --rollback's own alone-check below) so `--rollback --depart` names
    # the --depart conflict, not the rollback one. Written out literally
    # rather than copied from --rollback's check — --wipe and --no-nvim-pin
    # are deliberately included here for reasons specific to --depart that
    # don't apply to --rollback.
    if args.depart and (
        args.rollback
        or args.wipe
        or harness_set
        or args.profile != "personal"
        or args.force
        or args.reseed
        or args.adopt
        or args.check_links
    ):
        _fail("--depart must be used alone, with no other flags")

    # --check-links is a read-only audit, so unlike --depart/--rollback it
    # tolerates the two flags that scope *which* links.toml entries it
    # considers (--harness, --profile). Everything else either mutates the
    # machine or previews a mutation, and would be silently ignored here.
    # Checked ahead of --rollback's own alone-check so
    # `--rollback --check-links` names the audit flag, matching how --depart
    # takes precedence above.
    if args.check_links and (
        args.rollback
        or args.wipe
        or args.force
        or args.reseed
        or args.adopt
        or args.dry_run
    ):
        _fail("--check-links must be used alone, apart from --harness and --profile")

    if args.yes and not args.depart:
        _fail("--yes can only be used with --depart")

    # --rollback is an undo-only action. Rejecting --profile/--force
    # alongside it (not just --harness) keeps them from being silently
    # ignored, which would mislead someone into thinking they rolled back
    # "as work" or similar.
    if args.rollback and (
        harness_set
        or args.profile != "personal"
        or args.force
        or args.reseed
        or args.adopt
    ):
        _fail("--rollback must be used alone, with no other flags")

    if args.wipe and not args.rollback:
        _fail("--wipe can only be used with --rollback")

    if args.report_uninstalled and not args.check_links:
        _fail("--report-uninstalled can only be used with --check-links")

    if (
        not args.rollback
        and not args.depart
        and not args.check_links
        and not harness_set
    ):
        _fail(
            "no --harness specified — pass at least one of: "
            "claude, copilot, opencode, agy, pi, codex",
            show_usage=True,
        )

    # Deduplicate while preserving first-seen order: --harness=claude,claude
    # is a typo, not a request to wire claude twice.
    seen: list[str] = []
    for harness in harnesses:
        if harness not in seen:
            seen.append(harness)

    return Options(
        harnesses=tuple(seen),
        profile=args.profile,
        rollback=args.rollback,
        force=args.force,
        dry_run=args.dry_run,
        wipe=args.wipe,
        reseed=args.reseed,
        adopt=args.adopt,
        depart=args.depart,
        yes=args.yes,
        check_links=args.check_links,
        report_uninstalled=args.report_uninstalled,
        quiet=args.quiet,
        verbose=args.verbose,
        force_harness=args.force_harness,
    )


# ── subprocess plumbing ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class CommandResult:
    """Outcome of one external command: whether it succeeded, and its stdout."""

    ok: bool
    stdout: str = ""


def run_command(
    cmd: Sequence[str] | str, *, shell: bool = False, capture: bool = False
) -> CommandResult:
    """Run an external command, returning success rather than raising.

    Every package manager, installer, and service call in this file goes
    through this one wrapper — both so a missing binary degrades to a skip
    instead of a traceback, and so tests can stub the whole external world
    with a single monkeypatch.

    Args:
        cmd: Argument list, or a shell command string when ``shell`` is set.
        shell: Run through ``/bin/sh`` (needed for the ``curl … | sh``
            installers upstream projects publish).
        capture: Capture stdout instead of letting it stream to the terminal,
            and discard stderr. Every capture=True call site here is a
            detection probe (``brew shellenv``, ``systemctl --user
            show-environment``, ``nvim --version``), where a diagnostic on
            stderr is noise the caller already turns into a clean skip
            message — the same reason the shell version redirected these
            with ``&>/dev/null``.

    Returns:
        A :class:`CommandResult`; ``ok`` is False for a non-zero exit, a
        missing executable, or a permission error.
    """
    try:
        proc = subprocess.run(
            cmd,
            shell=shell,
            check=False,
            text=True,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.DEVNULL if capture else None,
        )
    except OSError:
        return CommandResult(False)
    return CommandResult(proc.returncode == 0, proc.stdout or "")


def have(executable: str) -> bool:
    """Return whether ``executable`` is on PATH."""
    return shutil.which(executable) is not None


# Dry-run preview printing lives in cli_common (shared with settings_seed).
_preview = cli_common.preview


def _header(message: str, *, quiet: bool = False) -> None:
    """Print a section header line."""
    with _io_lock:
        cli_common.qprint(PALETTE.header(message), quiet=quiet)


# ── package inventory probes (departure support) ─────────────────────────────


def _capture_package_snapshot(manager: str) -> dict[str, str] | None:
    """Probe the live package/tool inventory for one manager.

    Returns None on a failed/unavailable probe — callers must skip
    recording that transaction entirely rather than record a misleading
    empty snapshot, since nothing downstream can yet distinguish "empty"
    from "probe failed" for transaction data (that distinction matters for
    departure-time removal decisions, which are not implemented yet — see
    execute_departure's docstring).
    """
    probes: dict[str, tuple[list[str], Callable[[str], dict[str, str]]]] = {
        "apt": (depart.dpkg_query_command(), depart.parse_dpkg_query),
        "dnf": (depart.rpm_qa_command(), depart.parse_rpm_qa),
        "npm": (depart.npm_ls_global_command(), depart.parse_npm_ls_global),
        "uv-tool": (depart.uv_tool_list_command(), depart.parse_uv_tool_list),
    }
    command, parse = probes[manager]
    result = run_command(command, capture=True)
    return parse(result.stdout) if result.ok else None


# ── symlink engine ────────────────────────────────────────────────────────────


# load_links, load_managed_dirs, and the pure link_applies/iter_concrete_links/
# gather_links machinery live in agent-scripts/link_inspect.py now (the
# extracted links.toml-audit module); they are re-exported from this module's
# import block above. Only the Context-coupled wrappers stay here.


def link_applies(spec: LinkSpec, ctx: Context) -> bool:
    """Return whether ``spec`` should be linked for this run's machine/options."""
    return link_inspect.link_applies(
        spec,
        harnesses=ctx.opts.harnesses,
        is_mac=ctx.is_mac,
        is_linux=ctx.is_linux,
        is_wsl=ctx.is_wsl,
        profile=ctx.opts.profile,
    )


def iter_concrete_links(
    spec: LinkSpec, ctx: Context
) -> Iterator[tuple[Path, Path, str]]:
    """Expand one ``links.toml`` row into concrete ``(src, dest, relative_src)`` triples."""
    return link_inspect.iter_concrete_links(
        spec, repo_root=ctx.repo_root, home=ctx.home
    )


def gather_links(
    ctx: Context, specs: Sequence[LinkSpec]
) -> list[tuple[Path, Path, str, bool]]:
    """Expand every ``links.toml`` row into concrete triples, once per run.

    Computed for every spec regardless of whether it applies to this run's
    machine/harness selection — the fourth element flags that separately.
    Creation and collision-detection only look at the applicable triples;
    orphan-detection's "still expected" set spans every triple regardless,
    so a harness/platform-gated row is never misreported as removed. This
    single pass feeds all three, rather than each recomputing its own
    expansion independently.
    """
    return link_inspect.gather_links(
        specs,
        repo_root=ctx.repo_root,
        home=ctx.home,
        harnesses=ctx.opts.harnesses,
        is_mac=ctx.is_mac,
        is_linux=ctx.is_linux,
        is_wsl=ctx.is_wsl,
        profile=ctx.opts.profile,
    )


def _find_link_collision(
    links: Sequence[tuple[Path, Path, str, bool]],
) -> tuple[Path, str, str] | None:
    """Return ``(dest, first_src, second_src)`` for the first destination two
    distinct applicable sources claim, or None.

    Only applicable (this-run) triples are considered — a row merely gated
    off on this machine has nothing written this run, so it cannot collide
    with anything.
    """
    claimed: dict[Path, str] = {}
    for _src, dest, rel, applicable in links:
        if not applicable:
            continue
        seen = claimed.get(dest)
        if seen is not None and seen != rel:
            return dest, seen, rel
        claimed.setdefault(dest, rel)
    return None


def symlink(ctx: Context, src: Path, dest: Path) -> bool:
    """Link ``dest`` → ``src``, backing up whatever non-symlink is in the way.

    An already-correct symlink is a no-op and is deliberately *not*
    re-recorded in the history: recording it again on every re-run would
    make a later rollback try to remove a link an earlier run created and
    already accounted for.

    Args:
        ctx: The run context.
        src: Absolute path inside the repo.
        dest: Absolute destination path.

    Returns:
        True if the link is in place (or would be, in dry-run), False if the
        step was skipped.
    """
    if ctx.opts.dry_run:
        if dest.is_symlink():
            current = os.readlink(dest)
            if current == str(src):
                _preview(
                    f"{dest} already correctly linked → {src}, no-op",
                    quiet=ctx.opts.quiet,
                )
            else:
                _preview(
                    f"would relink {dest} → {src} (currently → {current})",
                    quiet=ctx.opts.quiet,
                )
        elif dest.exists():
            _preview(
                f"would back up {dest} → {dest}.bak, then link {dest} → {src}",
                quiet=ctx.opts.quiet,
            )
        else:
            _preview(f"would link {dest} → {src}", quiet=ctx.opts.quiet)
        return True

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        ctx.reporter.skip(f"symlink {dest}", "could not create parent directory")
        return False

    was_link = dest.is_symlink()
    if dest.exists() and not was_link:
        backup = dest.with_name(dest.name + ".bak")
        try:
            if dest.is_dir():
                shutil.move(str(dest), str(backup))
            else:
                # Copy, never move: dest stays continuously present while
                # the backup is made, so a concurrent reader (e.g. a hook
                # runner resolving a script under this path) never sees
                # the file vanish — the following replace below is the
                # only mutation dest ever sees.
                shutil.copy2(dest, backup)
        except (OSError, shutil.Error):
            ctx.reporter.skip(f"symlink {dest}", "could not back up existing file")
            return False
        ctx.manifest.record_backup(dest, backup)
        print(f"  Backing up {dest} → {backup}")

    try:
        # Atomic placement: build the link under a dot-prefixed,
        # entropy-suffixed name in the same directory, then rename it over
        # the dest. rename(2) never follows symlinks in either argument, so
        # this replaces an existing link in one step — including a symlink
        # to a directory, which is replaced rather than descended into —
        # and never leaves dest missing between operations. The old
        # unlink-then-symlink_to dance had exactly the window this closes:
        # during a repoint of ~28 live ~/.claude/scripts/* links, every
        # active harness session on the machine could observe (and fail
        # hard on) a missing guard_rails.py for the duration.
        # The old comment's `ln -sf`-into-a-directory concern doesn't
        # apply: that was an artifact of unlink-first ordering, not of
        # rename — and fresh creations need no special case either, since
        # rename onto a nonexistent dest is a plain create.
        temp = dest.parent / f".{dest.name}.tmp-{os.getpid()}-{uuid4().hex}"
        try:
            os.symlink(str(src), temp)
            os.replace(temp, dest)
        finally:
            # After a successful replace the temp path no longer exists
            # (it *is* dest now); missing_ok covers that and the
            # failure paths alike.
            temp.unlink(missing_ok=True)
    except OSError:
        ctx.reporter.skip(f"symlink {dest}", "ln failed")
        return False

    if not was_link:
        ctx.manifest.record_symlink(dest, src)
    cli_common.qprint(PALETTE.ok(f"  linked {dest}"), quiet=ctx.opts.quiet)
    return True


def _vscode_wsl_user_dir() -> Path | None:
    """Locate the Windows-side VS Code user directory from WSL.

    Under WSL, VS Code is normally driven from the Windows GUI via the
    Remote-WSL extension, so the real user settings.json lives in the
    Windows user profile, not the WSL filesystem. The profile directory is
    derived from the Windows-side ``code`` shim's own path (inherited onto
    PATH via WSL interop) rather than hardcoding a username.

    Returns:
        The ``.../AppData/Roaming/Code/User`` directory, or None if no
        Windows-side ``code`` CLI is on PATH.
    """
    # Fast path: check PATH directories containing '/mnt/' and 'Code' first
    # to avoid slow full-PATH traversal across dozens of Windows dirs under WSL.
    path_env = os.environ.get("PATH", "")
    code_bin: str | None = None
    for d in path_env.split(":"):
        if "/mnt/" in d and ("VS Code" in d or "Code" in d):
            cand = Path(d) / "code"
            if cand.is_file():
                code_bin = str(cand)
                break
    if not code_bin:
        code_bin = shutil.which("code")
    if not code_bin:
        return None
    parts = Path(code_bin).parts
    if "AppData" not in parts or not code_bin.startswith("/mnt/"):
        return None
    win_user_dir = Path(*parts[: parts.index("AppData")])
    if "Users" not in parts:
        return None
    return win_user_dir / "AppData" / "Roaming" / "Code" / "User"


# The links.toml src whose 5 destinations (~/.claude/CLAUDE.md and its
# codex/copilot/gemini/pi equivalents) dotfiles recomposes with a personal
# overlay on any machine that has both repos checked out. Single source of
# truth lives in link_inspect (do_check_links' audit needs the same fact to
# stay in sync with this write-side guard — see link_inspect.py's docstring
# on the constant for the 2026-09-09 false-positive this prevents).
_PERSONAL_OVERLAY_SRC_REL = link_inspect.PERSONAL_OVERLAY_SRC_REL


def _personal_overlay_unwrapped(ctx: Context, rel: str) -> bool:
    """True if installing ``rel`` here would clobber dotfiles' composed file.

    Guards exactly the ``CORE_INSTRUCTIONS.md`` destinations against a
    direct, unwrapped agent-toolkit install silently overwriting dotfiles'
    composed ``global-instructions.md`` (``CORE_INSTRUCTIONS.md`` +
    ``personal-overlay.md``) on a machine that has both repos checked out.
    ``dotfiles/scripts/install-with-agent-toolkit.sh`` sets
    ``AGENT_TOOLKIT_INSTALL_WRAPPER=1`` before invoking this installer, then
    re-runs dotfiles' own installer right after to reassert the composed
    file — see that script's own comment for the incident this fixes
    (2026-09-07: a swarm worker's direct, unwrapped `install.py` run dropped
    the user's personal-policy content from every harness on this machine).

    Every other shared destination is unaffected by this check — agent-
    toolkit keeps claiming those directly and unconditionally.

    Checks ``ctx.home`` rather than ``Path.home()`` — Context always carries
    its home explicitly so tests never touch the real machine; calling
    ``Path.home()`` here would silently break that for every test exercising
    this destination.
    """
    if rel != _PERSONAL_OVERLAY_SRC_REL:
        return False
    if os.environ.get("AGENT_TOOLKIT_INSTALL_WRAPPER") == "1":
        return False
    return (ctx.home / "dotfiles").is_dir()


def install_symlinks(
    ctx: Context, links: Sequence[tuple[Path, Path, str, bool]]
) -> None:
    """Link every applicable expanded ``links.toml`` entry.

    ``links`` is a pre-gathered expansion (see :func:`gather_links`) rather
    than the raw specs, so a ``dir=true`` row's files are already individual
    triples by the time this runs. The WSL VS Code case isn't handled here
    even though it's a symlink candidate everywhere else: see
    ``seed_vscode_settings``.
    """
    _header("==> Symlinking repo files...", quiet=ctx.opts.quiet)

    for src, dest, rel, applicable in links:
        if not applicable:
            continue
        if _personal_overlay_unwrapped(ctx, rel):
            ctx.reporter.skip(
                f"symlink {dest}",
                "dotfiles is present and composes this file with a personal "
                "overlay -- run dotfiles/scripts/install-with-agent-toolkit.sh "
                "instead of install.py directly, or set "
                "AGENT_TOOLKIT_INSTALL_WRAPPER=1 to override",
            )
            continue
        symlink(ctx, src, dest)


# ── copy-once seeds and drift detection ───────────────────────────────────────


def _replace_stale_vscode_symlink(ctx: Context, dest: Path) -> None:
    """Remove a stale WSL-only VS Code symlink so ``seed_file`` copies for real.

    A symlink WSL creates on a DrvFs path uses a private WSL-only reparse
    tag that native Windows processes — including VS Code itself — can't
    resolve. ``dest.is_file()`` still reports True for the dead link (it
    resolves fine from WSL), so without this, ``seed_file``'s "already
    seeded" check would treat a machine stuck in the broken symlink state
    as done and never migrate it to a real copy.
    """
    if not dest.is_symlink():
        return
    if ctx.opts.dry_run:
        _preview(
            f"would remove stale WSL symlink at {ctx.display(dest)} and copy instead",
            quiet=ctx.opts.quiet,
        )
        return
    dest.unlink()


def seed_vscode_settings(ctx: Context) -> list[tuple[str, tuple[str, str]]]:
    """Seed the Windows-side VS Code settings.json and keybindings.json under WSL.

    These can't be symlinked (see ``install_symlinks``'s docstring): a
    WSL-side symlink onto a DrvFs path is unreadable by native Windows
    processes, so they're copy-once seeds like Claude Code's settings.json
    and opencode's opencode.jsonc, just forced by an OS limitation instead
    of a live-rewrite one.

    Returns:
        ``[(display path, (seed filename, drift description)), ...]``, one
        entry per file, or ``[]`` when not applicable (not WSL, or WSL
        without a Windows-side ``code`` CLI on PATH).
    """
    if not ctx.is_wsl:
        return []
    user_dir = _vscode_wsl_user_dir()
    if user_dir is None:
        ctx.reporter.skip(
            "VS Code settings",
            "WSL detected but no Windows-side 'code' CLI found on PATH",
        )
        return []
    results: list[tuple[str, tuple[str, str]]] = []
    for name in ("settings.json", "keybindings.json"):
        seed = ctx.repo_root / "vscode" / name
        dest = user_dir / name
        if not ctx.opts.adopt:
            _replace_stale_vscode_symlink(ctx, dest)
        drift = seed_file(
            ctx,
            seed,
            dest,
            skip_label=f"{name} seed",
            drift=describe_vscode_drift,
            adopt_drift=_describe_vscode_text,
            run_command=run_command,
        )
        results.append((ctx.display(dest), (name, drift)))
    return results


def seed_claude_settings(ctx: Context) -> tuple[str, str]:
    """Seed ~/.claude/settings.json, if Claude Code was selected.

    Returns:
        ``(seed filename, drift description)``; both empty when the harness
        wasn't selected.
    """
    if not ctx.has_harness("claude"):
        return "", ""
    name = "settings.work.json" if ctx.opts.profile == "work" else "settings.json"
    seed = ctx.repo_root / "claude" / name
    dest = ctx.home / ".claude" / "settings.json"
    return name, seed_file(
        ctx,
        seed,
        dest,
        skip_label="settings.json seed",
        drift=describe_settings_drift,
        adopt_drift=_describe_settings_text,
        run_command=run_command,
    )


def seed_pi_settings(ctx: Context) -> tuple[str, str]:
    """Seed ~/.pi/agent/settings.json, if Pi was selected.

    Structurally like Claude Code's settings.json (just a ``skills`` array,
    no bash permission allowlist to watch for a live bypass on — that lives
    in ``permission-gate.ts``, a plain symlink, not this seeding subsystem),
    so this mirrors ``seed_claude_settings``'s simpler copy-once-and-report-
    drift shape rather than ``seed_opencode_config``'s allowlist-bypass
    detection. See ``pi/CLAUDE_CODE_PARITY.md`` §3 and §7.

    Returns:
        ``(seed filename, drift description)``; both empty when the harness
        wasn't selected.
    """
    if not ctx.has_harness("pi"):
        return "", ""
    name = "settings.json"
    seed = ctx.repo_root / "pi" / name
    dest = ctx.home / ".pi" / "agent" / "settings.json"
    return name, seed_file(
        ctx,
        seed,
        dest,
        skip_label="pi settings.json seed",
        drift=describe_settings_drift,
        adopt_drift=_describe_settings_text,
        run_command=run_command,
    )


def seed_opencode_config(ctx: Context) -> tuple[str, str]:
    """Seed ~/.config/opencode/opencode.jsonc, if opencode was selected.

    opencode is never installed on a work machine at all — parse_args
    rejects --profile=work combined with --harness=opencode outright — so
    there is only one variant of this seed file.

    Returns:
        ``(seed filename, drift description)``; both empty when the harness
        wasn't selected.
    """
    if not ctx.has_harness("opencode"):
        return "", ""
    name = "opencode.jsonc"
    seed = ctx.repo_root / "opencode" / name
    dest = ctx.home / ".config" / "opencode" / "opencode.jsonc"
    return name, seed_file(
        ctx,
        seed,
        dest,
        skip_label="opencode.jsonc seed",
        drift=describe_opencode_drift,
        adopt_drift=lambda seed_text, live_text: _describe_opencode_text(
            seed_text, live_text, adopt=True
        ),
        adopt_blocker=_opencode_adopt_blocker,
        run_command=run_command,
    )


def sync_codex_skills(ctx: Context) -> list[str]:
    """Copy each ``codex/skills/<name>/SKILL.md`` into ``~/.codex/skills/<name>/``.

    Every other harness's skills are plain ``[[link]]`` symlink rows in
    links.toml. Codex's can't be: its skill scanner does not follow
    symlinks for USER-scope discovery -- confirmed live (2026-09-08,
    atk-codex-skill-copy-fix) by swapping one skill's symlink for a real
    file copy at the identical path. The copy appeared in ``codex exec``'s
    skill list immediately; the symlink was invisible to both
    ``$skill-name`` and implicit matching, even after a full session
    restart.

    This mirrors the ``seed_*`` functions' shape above but not their
    copy-once-and-report-drift semantics: nothing here is meant for a user
    to hand-customize (same policy as every symlinked skill on every other
    harness), so unlike settings.json this unconditionally overwrites a
    stale copy back into sync on every run, rather than merely reporting
    the drift. A destination that is still the old (pre-fix) symlink is
    replaced the same way -- ``os.replace`` never follows a symlink target,
    so this always ends with a real file, never a symlink left in place.

    Known gap: unlike the ``[[link]]``-driven symlinks, a skill retired
    from ``codex/skills/`` later leaves its old ``~/.codex/skills/<name>/``
    copy behind with no automatic orphan-cleanup (that mechanism only
    tracks manifest-recorded symlinks) -- remove a retired skill's
    directory by hand if this list ever shrinks.

    Returns:
        Names of skills this run actually wrote (created or updated);
        empty when Codex wasn't selected or every copy was already current.
    """
    if not ctx.has_harness("codex"):
        return []
    src_root = ctx.repo_root / "codex" / "skills"
    if not src_root.is_dir():
        return []
    updated: list[str] = []
    for skill_dir in sorted(p for p in src_root.iterdir() if p.is_dir()):
        src = skill_dir / "SKILL.md"
        if not src.is_file():
            continue
        name = skill_dir.name
        dest = ctx.home / ".codex" / "skills" / name / "SKILL.md"
        src_bytes = src.read_bytes()
        if dest.is_file() and not dest.is_symlink() and dest.read_bytes() == src_bytes:
            continue
        if ctx.opts.dry_run:
            _preview(
                f"would sync {ctx.display(dest)} ← {ctx.display(src)}",
                quiet=ctx.opts.quiet,
            )
            updated.append(name)
            continue
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            ctx.reporter.skip(
                f"codex skill {name}", "could not create parent directory"
            )
            continue
        temp = dest.parent / f".{dest.name}.tmp-{os.getpid()}-{uuid4().hex}"
        try:
            temp.write_bytes(src_bytes)
            os.replace(temp, dest)
        except OSError:
            ctx.reporter.skip(f"codex skill {name}", "copy failed")
            continue
        finally:
            temp.unlink(missing_ok=True)
        ctx.manifest.record_copy(dest)
        cli_common.qprint(
            PALETTE.ok(f"  synced {ctx.display(dest)}"), quiet=ctx.opts.quiet
        )
        updated.append(name)
    return updated


# ── services ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ManagedService:
    """One systemd --user service this installer enables/disables/tracks.

    ``name`` feeds ``depart.service_key("systemd", name)`` for baseline and
    ledger lookups; ``unit`` is the literal systemctl unit name used in
    every systemctl-facing command. These are deliberately two different
    strings, not one — see the depart preflight/execute call sites below.
    """

    name: str
    unit: str


MANAGED_SERVICES: list[ManagedService] = []


def _probe_systemctl_word(word: str, cmd: list[str]) -> bool | None:
    """Run a ``systemctl --user is-<x>``-style probe by matching its stdout word.

    Exit code alone can't distinguish "answered no" from "couldn't run" —
    ``systemctl --user is-enabled`` exits non-zero for a genuinely disabled
    unit too. True/False for a real answer; None (unavailable/unanswerable)
    only when there's no recognizable word at all.
    """
    if not have("systemctl"):
        return None
    text = run_command(cmd, capture=True).stdout.strip()
    if text == word:
        return True
    if text:
        return False
    return None


def _probe_linger(user: str) -> bool | None:
    if not have("loginctl") or not user:
        return None
    text = run_command(
        ["loginctl", "show-user", user, "--property=Linger"], capture=True
    ).stdout.strip()
    if text == "Linger=yes":
        return True
    if text == "Linger=no":
        return False
    return None


def _capture_live_service(ctx: Context, service: ManagedService) -> dict[str, object]:
    """Fresh is-enabled/is-active/linger probe, for capture or classification."""
    enabled = _probe_systemctl_word(
        "enabled", ["systemctl", "--user", "is-enabled", service.unit]
    )
    active = _probe_systemctl_word(
        "active", ["systemctl", "--user", "is-active", service.unit]
    )
    linger = _probe_linger(_current_user())
    return depart.build_service_record(enabled=enabled, active=active, linger=linger)


def capture_service_baseline(ctx: Context) -> None:
    """Capture every managed service's service/linger state, immediately
    before :func:`enable_managed_services` runs — capturing any later would
    record the post-install enabled state as baseline and departure would
    never disable anything.
    """
    if (
        not ctx.is_linux
        or ctx.opts.dry_run
        or ctx.opts.profile == "work"
        or ctx.departure_baseline is None
    ):
        return
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    layer = {
        depart.service_key("systemd", service.name): _capture_live_service(ctx, service)
        for service in MANAGED_SERVICES
    }
    ctx.departure_baseline.add_layer(stamp, layer)


def _enable_service(ctx: Context, service: ManagedService) -> None:
    """Enable and start one managed systemd --user unit (Linux, non-work)."""
    if (
        not have("systemctl")
        or not run_command(["systemctl", "--user", "show-environment"], capture=True).ok
    ):
        ctx.reporter.skip(
            f"{service.name} service",
            "systemd --user unavailable (enable systemd in /etc/wsl.conf?)",
        )
        return
    if ctx.opts.dry_run:
        _preview(
            f"would enable+start {service.name} systemd user service, "
            f"enable-linger for {_current_user()}",
            quiet=ctx.opts.quiet,
        )
        return
    _header(
        f"==> Enabling {service.name} systemd user service...", quiet=ctx.opts.quiet
    )
    run_command(["systemctl", "--user", "daemon-reload"])
    if run_command(["systemctl", "--user", "enable", "--now", service.unit]).ok:
        # Without lingering, the service dies when the last WSL/SSH session
        # closes — enable-linger keeps the user manager (and this unit) up.
        if not run_command(
            ["loginctl", "enable-linger", _current_user()], capture=True
        ).ok:
            cli_common.qprint(
                "  note: loginctl enable-linger failed — "
                "service won't survive full logout",
                quiet=ctx.opts.quiet,
            )
    else:
        ctx.reporter.skip(
            f"{service.name} service", "systemctl --user enable --now failed"
        )


def enable_managed_services(ctx: Context) -> None:
    """Enable and start every managed systemd --user unit (Linux, non-work)."""
    if not ctx.is_linux or ctx.opts.profile == "work":
        return
    for service in MANAGED_SERVICES:
        _enable_service(ctx, service)


GLOBAL_GIT_HOOKS_PATH_KEY = "core.hooksPath"


def _global_git_hooks_path() -> str | None:
    """Read the current global ``core.hooksPath``, or None if unset."""
    result = run_command(
        ["git", "config", "--global", "--get", GLOBAL_GIT_HOOKS_PATH_KEY], capture=True
    )
    value = result.stdout.strip() if result.ok else ""
    return value or None


def _managed_git_hooks_path(ctx: Context) -> str:
    """The value :func:`install_global_git_hooks_path` sets/expects."""
    return str(ctx.repo_root / "githooks-global")


def capture_git_hooks_path_baseline(ctx: Context) -> None:
    """Capture the pre-existing global ``core.hooksPath``, immediately
    before :func:`install_global_git_hooks_path` runs -- capturing any later
    would record the origin repo's own already-set value as if it were the
    original, which would make departure "restore" that repo's own path
    instead of the true pre-install value. ``Baseline.add_layer``'s own is-unrecorded rule
    already makes this capture-once by construction: a second install run
    finds the key already recorded in layer 1 and skips it, the same way
    :func:`capture_service_baseline` relies on for services -- a scalar
    config value needs no extra guard beyond that.
    """
    if ctx.opts.dry_run or ctx.opts.profile == "work" or ctx.departure_baseline is None:
        return
    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    layer = {
        depart.gitconfig_key(GLOBAL_GIT_HOOKS_PATH_KEY): depart.build_gitconfig_record(
            _global_git_hooks_path()
        )
    }
    ctx.departure_baseline.add_layer(stamp, layer)


def install_global_git_hooks_path(ctx: Context) -> None:
    """Point global ``core.hooksPath`` at ``githooks-global/``, so every repo
    without its own local override picks up the no-commit-on-main hook.

    Skipped entirely on a work-profile machine (spec constraint: this must
    not apply to every repo on a work machine). This repo's own
    local ``githooks/pre-commit`` hook is unaffected either way -- git's
    config precedence lets a repo-local ``core.hooksPath`` override the
    global value, so the local hook (which gets the same branch check added
    directly) keeps running regardless of whether this global install ran.
    """
    if ctx.opts.profile == "work":
        return
    target = _managed_git_hooks_path(ctx)
    if ctx.opts.dry_run:
        _preview(
            f"would set global git core.hooksPath to {target}", quiet=ctx.opts.quiet
        )
        return
    if _global_git_hooks_path() == target:
        return  # already set -- idempotent
    _header("==> Setting global git core.hooksPath...", quiet=ctx.opts.quiet)
    if not run_command(
        ["git", "config", "--global", GLOBAL_GIT_HOOKS_PATH_KEY, target]
    ).ok:
        ctx.reporter.skip("global git core.hooksPath", "git config --global failed")


def _current_user() -> str:
    """Return the invoking user's name, for loginctl."""
    return os.environ.get("USER") or os.environ.get("LOGNAME") or ""


# ── departure execution (extracted to depart_exec.py) ────────────────────

# The --depart execution machinery — baseline capture, preflight
# reporting, the confirmation contract, the per-category phases, the
# ledger retry loop, and state finalization — lives in depart_exec.py
# now. install.py keeps thin binding wrappers (exact pre-extraction
# signatures) so its CLI, tests, and external callers are unchanged.
# Dependencies are resolved at call time so test monkeypatching of
# install.run_command / have / _vscode_wsl_user_dir and later mutation
# of MANAGED_SERVICES keep working.


def _departure_deps() -> depart_exec.Deps:
    """Bundle install-provided dependencies for depart_exec, resolved live."""
    return depart_exec.Deps(
        run_command=run_command,
        have=have,
        link_applies=link_applies,
        expand_dest=expand_dest,
        capture_live_service=_capture_live_service,
        capture_package_snapshot=_capture_package_snapshot,
        current_user=_current_user,
        global_git_hooks_path=_global_git_hooks_path,
        header=_header,
        managed_git_hooks_path=_managed_git_hooks_path,
        vscode_wsl_user_dir=_vscode_wsl_user_dir,
        palette=PALETTE,
        managed_services=MANAGED_SERVICES,
        GLOBAL_GIT_HOOKS_PATH_KEY=GLOBAL_GIT_HOOKS_PATH_KEY,
    )


# Re-export so existing install._VSCODE_GUARD_UNRESOLVED_PREFIX references
# (test contract on the ledger outcome prefix) keep resolving.
_VSCODE_GUARD_UNRESOLVED_PREFIX = depart_exec._VSCODE_GUARD_UNRESOLVED_PREFIX


def capture_departure_baseline(ctx: Context, specs: Sequence[LinkSpec]) -> None:
    """Thin shim — implementation in depart_exec."""
    depart_exec.capture_departure_baseline(_departure_deps(), ctx, specs)


def build_preflight_report(ctx: Context) -> dict[str, depart.Classification] | None:
    """Thin shim — implementation in depart_exec."""
    return depart_exec.build_preflight_report(_departure_deps(), ctx)


def build_package_preflight(ctx: Context) -> list[depart.PackageClassification] | None:
    """Thin shim — implementation in depart_exec."""
    return depart_exec.build_package_preflight(_departure_deps(), ctx)


def execute_service_phase(
    ctx: Context, baseline: depart.Baseline, ledger: depart.DepartureLedger
) -> None:
    """Thin shim — implementation in depart_exec."""
    depart_exec.execute_service_phase(_departure_deps(), ctx, baseline, ledger)


def execute_gitconfig_phase(
    ctx: Context, baseline: depart.Baseline, ledger: depart.DepartureLedger
) -> None:
    """Thin shim — implementation in depart_exec."""
    depart_exec.execute_gitconfig_phase(_departure_deps(), ctx, baseline, ledger)


def execute_file_symlink_phase(
    ctx: Context,
    baseline: depart.Baseline,
    report: dict[str, depart.Classification],
    ledger: depart.DepartureLedger,
) -> None:
    """Thin shim — implementation in depart_exec."""
    depart_exec.execute_file_symlink_phase(
        _departure_deps(), ctx, baseline, report, ledger
    )


def execute_directory_phase(
    ctx: Context,
    baseline: depart.Baseline,
    report: dict[str, depart.Classification],
    ledger: depart.DepartureLedger,
) -> None:
    """Thin shim — implementation in depart_exec."""
    depart_exec.execute_directory_phase(
        _departure_deps(), ctx, baseline, report, ledger
    )


def execute_runtime_phase(
    ctx: Context,
    report: dict[str, depart.Classification],
    ledger: depart.DepartureLedger,
) -> None:
    """Thin shim — implementation in depart_exec."""
    depart_exec.execute_runtime_phase(_departure_deps(), ctx, report, ledger)


def live_package_snapshots(
    baseline: depart.Baseline,
) -> dict[str, dict[str, str] | None]:
    """Thin shim — implementation in depart_exec."""
    return depart_exec.live_package_snapshots(_departure_deps(), baseline)


def execute_package_phase(
    ctx: Context, baseline: depart.Baseline, ledger: depart.DepartureLedger
) -> bool:
    """Thin shim — implementation in depart_exec."""
    return depart_exec.execute_package_phase(_departure_deps(), ctx, baseline, ledger)


def execute_departure(
    ctx: Context, baseline: depart.Baseline, report: dict[str, depart.Classification]
) -> depart.DepartureLedger:
    """Thin shim — implementation in depart_exec."""
    return depart_exec.execute_departure(_departure_deps(), ctx, baseline, report)


def do_depart(ctx: Context) -> int:
    """Thin shim — implementation in depart_exec."""
    return depart_exec.do_depart(_departure_deps(), ctx)


def _departure_state_paths(state_dir: Path) -> list[Path]:
    """Thin shim — implementation in depart_exec."""
    return depart_exec._departure_state_paths(_departure_deps(), state_dir)


def _delete_departure_state(ctx: Context) -> None:
    """Thin shim — implementation in depart_exec."""
    depart_exec._delete_departure_state(_departure_deps(), ctx)


def _execute_remove_file(path: Path) -> str:
    """Thin shim — implementation in depart_exec."""
    return depart_exec._execute_remove_file(_departure_deps(), path)


def _vscode_guard_preflight_annotations(
    ctx: Context, report: dict[str, depart.Classification]
) -> dict[str, str]:
    """Thin shim — implementation in depart_exec."""
    return depart_exec._vscode_guard_preflight_annotations(
        _departure_deps(), ctx, report
    )


# ── profile marker ────────────────────────────────────────────────────────────


def write_profile_marker(ctx: Context) -> None:
    """Mark this machine as work-provisioned, so later plain runs are guarded.

    Recorded as a copied file so a rollback removes it, resetting the guard
    along with everything else that run put in place.
    """
    if ctx.opts.profile != "work" or ctx.profile_marker.is_file():
        return
    if ctx.opts.dry_run:
        _preview(
            f"would write profile marker: {ctx.profile_marker}", quiet=ctx.opts.quiet
        )
        return
    ctx.profile_marker.parent.mkdir(parents=True, exist_ok=True)
    ctx.profile_marker.write_text("work\n")
    ctx.manifest.record_copy(ctx.profile_marker)


def work_guard_blocks(ctx: Context) -> bool:
    """Return whether a plain personal run must be refused on this machine."""
    if ctx.opts.profile != "personal" or ctx.opts.force:
        return False
    try:
        return ctx.profile_marker.read_text().strip() == "work"
    except OSError:
        return False


# ── rollback ──────────────────────────────────────────────────────────────────


def do_rollback(ctx: Context) -> int:
    """Reverse every file mutation recorded across every past run.

    Walks the history newest-to-oldest so a path mutated by several runs
    ends up back at its oldest recorded state (the original pre-install
    file, not an intermediate one). Nothing here aborts: anything that
    doesn't match what was recorded is reported and the walk continues.

    Under ``--wipe``, backups are deleted instead of restored, and untracked
    state the installer creates but never records in the manifest (Neovim's
    XDG state dirs, every Linux systemd unit in ``MANAGED_SERVICES``) is
    swept too — even when no manifest exists at all, e.g. a second
    ``--wipe`` run after the first already consumed it.

    Returns:
        Exit status — 1 if any step was skipped, else 0.
    """
    skips = Reporter()
    swept = False

    if ctx.opts.wipe:
        swept = _wipe_managed_services(ctx, skips) or swept
        swept = _wipe_neovim_dirs(ctx, skips) or swept

    manifest = ctx.manifest
    if not manifest.path.is_file():
        if swept:
            cli_common.qprint(
                PALETTE.header(
                    "Wipe swept untracked state — no recorded history to reverse."
                ),
                quiet=ctx.opts.quiet,
            )
            return _report_skips_and_exit(skips)
        print(
            PALETTE.error(f"no manifest at {manifest.path} — nothing to roll back"),
            file=sys.stderr,
        )
        return 1

    entries = manifest.entries()
    run_count = sum(1 for entry in entries if entry.get("kind") == "run")
    verb = "Would roll back" if ctx.opts.dry_run else "Rolling back"
    header_msg = f"==> {verb} {run_count} recorded run(s) from {manifest.path}"
    if ctx.opts.wipe:
        header_msg += (
            " — wipe mode: original configs discarded, not restored; "
            "untracked Neovim/managed-service state swept"
        )
    _header(header_msg, quiet=ctx.opts.quiet)

    # Which backup paths this pass has already restored (or, under --wipe,
    # deleted). An older duplicate file-backed-up entry for the same path
    # (recorded across an earlier backup/rollback/reinstall cycle) is then
    # recognized as already handled rather than misreported as a missing
    # backup.
    restored: set[Path] = set()
    # Which dest paths a file-backed-up entry has already restored (non-wipe
    # path only), processed newest-to-oldest in this same pass. Lets a plain
    # file-copied entry for the same dest — e.g. the original bootstrap copy
    # that predates any --reseed of it — recognize its target was already
    # correctly restored by a later (already-processed) entry, instead of
    # unconditionally deleting it a second time.
    restored_dests: set[Path] = set()

    for entry in reversed(entries):
        match entry.get("kind"):
            case "symlink-created":
                _rollback_symlink(ctx, entry, skips)
            case "file-copied":
                _rollback_copy(ctx, entry, restored_dests)
            case "file-backed-up":
                _rollback_backup(ctx, entry, skips, restored, restored_dests)
            case "package-installed":
                cli_common.qprint(
                    f"  package left installed (profile-independent): "
                    f"{entry.get('name', '')}",
                    quiet=ctx.opts.quiet,
                )
            case "run":
                cli_common.qprint(
                    f"  (run was: {entry.get('timestamp', '')}, "
                    f"profile: {entry.get('profile', '')})",
                    quiet=ctx.opts.quiet,
                )

    if ctx.opts.dry_run:
        if ctx.opts.wipe:
            excluded = {manifest.path, *_departure_state_paths(ctx.state_dir)}
            remaining = (
                [p for p in ctx.state_dir.iterdir() if p not in excluded]
                if ctx.state_dir.is_dir()
                else []
            )
            if not remaining:
                _preview(
                    f"would remove empty state directory {ctx.state_dir}",
                    quiet=ctx.opts.quiet,
                )
        print(
            "Dry run complete — nothing was changed. "
            "Re-run without --dry-run to roll back for real."
        )
    else:
        manifest.path.unlink(missing_ok=True)
        _delete_departure_state(ctx)
        if (
            ctx.opts.wipe
            and ctx.state_dir.is_dir()
            and not any(ctx.state_dir.iterdir())
        ):
            ctx.state_dir.rmdir()
        msg = (
            "Rollback complete — configuration wiped to a blank slate."
            if ctx.opts.wipe
            else "Rollback complete. Re-run ./install.sh with the intended profile."
        )
        print(PALETTE.header(msg))

    return _report_skips_and_exit(skips)


def _report_skips_and_exit(skips: Reporter) -> int:
    """Print the rollback skip tally, if any, and return the matching exit code."""
    if skips.skipped:
        print(
            PALETTE.warn(
                f"⚠ {len(skips.skipped)} rollback step(s) did not apply cleanly "
                "(see SKIPPED lines above)"
            )
        )
        return 1
    return 0


def _wipe_service(ctx: Context, skips: Reporter, service: ManagedService) -> bool:
    """Disable+stop one managed Linux systemd --user unit, under --wipe.

    Gated on live filesystem state, not manifest entries — simpler than
    scanning history, and it's what makes this work even when no manifest
    exists at all.

    Returns:
        Whether the unit symlink existed at the start — that's what
        "swept something" means here, true regardless of whether the probe
        or the disable call then succeed.
    """
    if not (ctx.opts.wipe and ctx.is_linux):
        return False
    unit_path = ctx.home / ".config" / "systemd" / "user" / service.unit
    if not unit_path.is_symlink():
        return False

    if (
        not have("systemctl")
        or not run_command(["systemctl", "--user", "show-environment"], capture=True).ok
    ):
        skips.note(
            f"{unit_path} exists but systemd --user is unavailable — "
            f"could not disable the {service.name} service"
        )
        return True

    if ctx.opts.dry_run:
        _preview(
            f"would disable+stop the {service.name} systemd user service (wipe)",
            quiet=ctx.opts.quiet,
        )
        return True

    if run_command(["systemctl", "--user", "disable", "--now", service.unit]).ok:
        cli_common.qprint(
            f"  disabled+stopped {service.name} systemd user service",
            quiet=ctx.opts.quiet,
        )
    else:
        skips.note(f"could not disable+stop the {service.name} systemd user service")
    return True


def _wipe_managed_services(ctx: Context, skips: Reporter) -> bool:
    """Disable+stop every managed Linux systemd --user unit, under --wipe."""
    swept = False
    for service in MANAGED_SERVICES:
        swept = _wipe_service(ctx, skips, service) or swept
    return swept


def _wipe_neovim_dirs(ctx: Context, skips: Reporter) -> bool:
    """Sweep Neovim's untracked XDG state directories, under --wipe.

    WARNING: these are Neovim's own data/state/cache dirs (where lazy.nvim
    installs plugins, shada, swap files live) — NOT the same thing as
    Neovim's *vendor* runtime tree (share/nvim/runtime: vim.uri, syntax.vim,
    spellfiles). A self-contained Neovim install must never place that tree
    inside ~/.local/share/nvim, since this function `shutil.rmtree`s that
    whole directory. This confusion caused a real incident: a Neovim binary
    installed with its runtime nested here got wiped, leaving a binary that
    couldn't resolve `require('vim.uri')`. A self-contained install goes in
    ~/.local/opt/neovim instead — outside this sweep's reach.

    Returns:
        Whether any of the three currently exist — "swept something" is
        based on pre-sweep state, not on whether removal (or its preview)
        then succeeds.
    """
    if not ctx.opts.wipe:
        return False
    dirs = (
        ctx.home / ".local" / "share" / "nvim",
        ctx.home / ".local" / "state" / "nvim",
        ctx.home / ".cache" / "nvim",
    )
    found = False
    for path in dirs:
        if not path.exists():
            continue
        found = True
        if ctx.opts.dry_run:
            _preview(f"would remove {path} (wipe)", quiet=ctx.opts.quiet)
            continue
        try:
            shutil.rmtree(path)
        except OSError as exc:
            skips.note(f"could not remove {path}: {exc}")
            continue
        cli_common.qprint(f"  removed {path}", quiet=ctx.opts.quiet)
    return found


def _rollback_symlink(ctx: Context, entry: dict[str, object], skips: Reporter) -> None:
    """Undo one ``symlink-created`` entry, unless something else claimed the path."""
    dest = Path(str(entry.get("dest", "")))
    recorded_src = str(entry.get("src", ""))
    if not dest.is_symlink():
        return
    current = os.readlink(dest)
    if recorded_src and current != recorded_src:
        skips.note(
            f"symlink {dest} now points to {current}, not {recorded_src} — "
            "something else has claimed this path, leaving it alone"
        )
        return
    if ctx.opts.dry_run:
        _preview(f"would remove symlink {dest}", quiet=ctx.opts.quiet)
        return
    try:
        dest.unlink()
    except OSError as exc:
        skips.note(f"could not remove symlink {dest}: {exc}")
        return
    cli_common.qprint(f"  removed symlink {dest}", quiet=ctx.opts.quiet)


def _rollback_copy(
    ctx: Context, entry: dict[str, object], restored_dests: set[Path]
) -> None:
    """Undo one ``file-copied`` entry.

    Skipped when ``dest`` is in ``restored_dests``: a newer (already
    processed, since this walk runs newest-to-oldest) ``file-backed-up``
    entry for the same path already correctly restored it this pass, so
    unlinking here would delete that restored original rather than an
    installer-managed copy — see ``do_rollback``'s comment on
    ``restored_dests``.
    """
    dest = Path(str(entry.get("dest", "")))
    if dest in restored_dests:
        cli_common.qprint(
            f"  {dest} left in place (already restored by a later entry)",
            quiet=ctx.opts.quiet,
        )
        return
    if not dest.is_file():
        return
    if ctx.opts.dry_run:
        _preview(f"would remove {dest}", quiet=ctx.opts.quiet)
        return
    dest.unlink()
    cli_common.qprint(f"  removed {dest}", quiet=ctx.opts.quiet)


def _rollback_backup(
    ctx: Context,
    entry: dict[str, object],
    skips: Reporter,
    restored: set[Path],
    restored_dests: set[Path],
) -> None:
    """Restore one ``file-backed-up`` entry from its ``.bak`` path.

    Under --wipe, the backup is deleted instead of restored — the original
    pre-install file is discarded, not brought back.
    """
    dest = Path(str(entry.get("dest", "")))
    backup = Path(str(entry.get("backup", "")))
    if backup.exists():
        if ctx.opts.dry_run:
            if ctx.opts.wipe:
                _preview(
                    f"would delete backup {backup} (wipe — {dest} will not be restored)",
                    quiet=ctx.opts.quiet,
                )
            else:
                _preview(f"would restore {dest} from {backup}", quiet=ctx.opts.quiet)
            return
        if ctx.opts.wipe:
            try:
                backup.unlink()
            except OSError as exc:
                skips.note(f"could not delete backup {backup}: {exc}")
                return
            cli_common.qprint(
                f"  deleted backup {backup} — original {dest} not restored (wipe)",
                quiet=ctx.opts.quiet,
            )
            restored.add(backup)
            return
        try:
            shutil.move(str(backup), str(dest))
        except (OSError, shutil.Error) as exc:
            skips.note(f"could not restore {dest} from {backup}: {exc}")
            return
        cli_common.qprint(f"  restored {dest} from {backup}", quiet=ctx.opts.quiet)
        restored.add(backup)
        restored_dests.add(dest)
        return
    if backup not in restored:
        wording = (
            "already wiped, or removed outside install.sh"
            if ctx.opts.wipe
            else "already restored, or removed outside install.sh"
        )
        skips.note(f"backup {backup} for {dest} not found — {wording}")


# ── summary ───────────────────────────────────────────────────────────────────


def print_summary(
    ctx: Context,
    settings: tuple[str, str],
    opencode: tuple[str, str],
    vscode: Sequence[tuple[str, tuple[str, str]]] = (),
    pi_settings: tuple[str, str] = ("", ""),
) -> None:
    """Print the loud end-of-run summary: skips, drift, and next steps."""
    dry = ctx.opts.dry_run
    print()
    label = "Dry run summary" if dry else "Install summary"
    print(PALETTE.header(f"════════ {label} — profile: {ctx.opts.profile} ════════"))

    if ctx.reporter.skipped:
        print(PALETTE.warn(f"⚠ {len(ctx.reporter.skipped)} step(s) DID NOT run:"))
        for item in ctx.reporter.skipped:
            print(PALETTE.error(f"  ✗ {item}"))
    elif dry:
        print(PALETTE.ok("✓ all steps previewed cleanly (no real detection failures)"))
    else:
        print(PALETTE.ok("✓ all steps completed"))

    for path, (seed_name, drift) in (
        ("~/.claude/settings.json", settings),
        ("~/.config/opencode/opencode.jsonc", opencode),
        ("~/.pi/agent/settings.json", pi_settings),
        *vscode,
    ):
        if drift:
            print(PALETTE.warn(f"⚠ {path} drifted from {seed_name}: {drift}"))
            if ctx.opts.adopt:
                print(
                    "  (adoption was not completed — resolve the reported block, "
                    "then rerun --adopt)"
                )
            else:
                print(
                    "  (copy-once by design — re-run with --reseed to overwrite, "
                    "or port changes manually)"
                )

    if dry:
        print("  dry run — nothing was changed; re-run without --dry-run to apply")
    else:
        print(
            f"  rollback available: ./install.sh --rollback "
            f"(history: {ctx.manifest.path})"
        )
        if ctx.opts.adopt:
            print(
                "  adopted repo seeds are unstaged changes — commit them before rerunning"
            )

    if dry:
        return

    print()
    print("Manual steps:")
    if ctx.is_mac:
        print("  - Log out and back in for Caps Lock → Escape to take effect")
        print("  - Open Karabiner-Elements → grant Input Monitoring + Accessibility")
        print("  - Open Rectangle → grant Accessibility permission")
    # No claude-login line here: watchcommit — the consumer that line was
    # written for — is origin-repo-only (empty MANAGED_SERVICES above, and
    # this repo carries no watchcommit loader at all), so agent-toolkit's
    # install has nothing that auto-consumes claude credentials. Any other
    # repo whose install needs a login step prints its own manual step.
    if ctx.opts.profile == "work":
        print("  - ~/.secrets is sourced if present — for work-issued tokens only;")
        print("    do NOT put a personal ANTHROPIC_API_KEY on this machine")
    if ctx.is_linux:
        print("  - Restart your shell to pick up the new config")


# ── links.toml audit ──────────────────────────────────────────────────────────

# The inspection/classification logic itself — bucket constants, path
# classifiers, orphan detection, drift finding — lives in
# agent-scripts/link_inspect.py now; this module re-exports every moved name
# (see the import block at the top) and keeps only the Context-coupled
# adapters, the repair/deletion execution (_cleanup_orphaned_links), and the
# do_check_links entrypoint.


def _check_applicable_links(
    ctx: Context,
    links: Sequence[tuple[Path, Path, str, bool]],
    *,
    report_uninstalled: bool = False,
) -> tuple[dict[str, list[str]], dict[Path, int]]:
    """Adapter for link_inspect.check_applicable_links; see it for detail."""
    return link_inspect.check_applicable_links(
        links,
        repo_root=ctx.repo_root,
        format_path=ctx.display,
        manifest_entries=ctx.manifest.entries(),
        report_uninstalled=report_uninstalled,
        home=ctx.home,
    )


def _find_orphaned_links(
    ctx: Context, links: Sequence[tuple[Path, Path, str, bool]]
) -> list[Path]:
    """Adapter for link_inspect.find_orphaned_links; see it for detail."""
    return link_inspect.find_orphaned_links(
        links, manifest_entries=ctx.manifest.entries()
    )


def _check_orphaned_links(
    ctx: Context,
    links: Sequence[tuple[Path, Path, str, bool]],
    findings: dict[str, list[str]],
) -> None:
    """Add manifest-recorded symlinks that links.toml no longer produces."""
    link_inspect.check_orphaned_links(
        links,
        findings,
        format_path=ctx.display,
        manifest_entries=ctx.manifest.entries(),
    )


def _live_backup_paths(ctx: Context) -> set[Path]:
    """Adapter for link_inspect.live_backup_paths; see it for detail."""
    return link_inspect.live_backup_paths(ctx.manifest.entries())


def _dir_applies(
    dir_spec: ManagedDirSpec, specs: Sequence[LinkSpec], ctx: Context
) -> bool:
    """Return whether a declared directory is in scope for this run.

    A declared directory inherits its harness, platform, WSL, and profile
    scoping from the ``[[link]]`` rows whose destinations fall inside it,
    rather than carrying gating fields of its own. Reusing
    :func:`link_applies` picks up all four for free. A directory no row targets
    is audited unconditionally, since there is no evidence to gate on.
    """
    directory = expand_dest(dir_spec.dest, ctx.home)
    related = [
        spec
        for spec in specs
        if expand_dest(spec.dest, ctx.home).is_relative_to(directory)
    ]
    if not related:
        return True
    return any(link_applies(spec, ctx) for spec in related)


def _check_unmanaged_files(
    ctx: Context,
    specs: Sequence[LinkSpec],
    links: Sequence[tuple[Path, Path, str, bool]],
    managed_dirs: Sequence[ManagedDirSpec],
    findings: dict[str, list[str]],
) -> int:
    """Adapter for link_inspect.check_unmanaged_files; see it for detail."""
    return link_inspect.check_unmanaged_files(
        managed_dirs,
        links,
        home=ctx.home,
        format_path=ctx.display,
        dir_applies=lambda dir_spec: _dir_applies(dir_spec, specs, ctx),
        findings=findings,
        manifest_entries=ctx.manifest.entries(),
    )


def _cleanup_orphaned_links(
    ctx: Context, links: Sequence[tuple[Path, Path, str, bool]]
) -> None:
    """Remove every orphaned symlink this run finds, plus its manifest entry.

    Runs on every plain install, not just ``--check-links``. Scoped
    strictly to the ORPHANED bucket — a broken-source, wrong-target, or
    not-a-symlink destination indicates something actually inconsistent,
    not "nothing wants this anymore," and stays human-reviewed via
    ``--check-links``. Removing an orphaned symlink only deletes the
    pointer, never real content, so this is safe to do without asking.
    """
    orphans = _find_orphaned_links(ctx, links)
    if not orphans:
        return

    if ctx.opts.dry_run:
        for dest in orphans:
            _preview(
                f"would remove orphaned symlink: {ctx.display(dest)}",
                quiet=ctx.opts.quiet,
            )
        return

    removed: list[Path] = []
    for dest in orphans:
        try:
            dest.unlink()
        except OSError:
            continue
        removed.append(dest)
        cli_common.qprint(
            PALETTE.ok(f"  removed orphaned symlink: {ctx.display(dest)}"),
            quiet=ctx.opts.quiet,
        )
        with contextlib.suppress(OSError):
            dest.parent.rmdir()

    if removed:
        try:
            ctx.manifest.remove_symlink_entries(set(removed))
        except OSError:
            ctx.reporter.skip(
                "orphan cleanup manifest update",
                "could not rewrite the history file — the symlinks were "
                "still removed, but a future --rollback may reference them",
            )


def do_check_links(ctx: Context) -> int:
    """Audit the live symlinks against ``links.toml`` and report, changing nothing.

    Fills the gap between the two existing consistency checks: ``--rollback``
    only inspects what the history recorded and only asks whether the target
    string still matches, while ``--depart`` compares against an install-time
    baseline. Neither notices that a link's repo-side source was deleted or
    renamed, and a plain re-run does not either — ``symlink`` never checks
    ``src.exists()`` before creating the link, and stops visiting a
    destination the moment its links.toml entry goes away.

    Links pointing at the same file in a *different* checkout of this repo
    are reported as an informational note rather than as findings, and do
    not affect the exit code: this repo mandates worktree-first
    development, so auditing from a worktree while the machine is wired to
    the main checkout is routine, and burying the real findings under one
    line per entry would make the tool useless exactly when it is most
    likely to be reached for.

    ``unmanaged`` additionally audits every directory ``links.toml`` declares
    exclusive through a ``[[managed_dir]]`` row, reporting any file there that
    no link produces. Those rows carry no gating fields of their own: each
    directory inherits its scope from the ``[[link]]`` rows inside it, so
    widening ``--harness`` only ever brings more directories into scope and
    never reclassifies a file within one. The older justification for that not
    producing false positives — that every bucket requires a destination
    already on disk — stopped being true when this bucket arrived, since it
    reads directory contents instead.

    Returns:
        Exit status — 0 if every bucket is empty, 1 if anything was found.
    """
    # With no --harness, audit every harness's entries. Widening cannot
    # invent findings: each bucket below requires a destination that already
    # exists on disk, which an unprovisioned harness's entry never has.
    if not ctx.opts.harnesses:
        ctx = replace(ctx, opts=replace(ctx.opts, harnesses=VALID_HARNESSES))

    specs = load_links(ctx.repo_root / "links.toml")
    managed_dirs = load_managed_dirs(ctx.repo_root / "links.toml")
    # The audit computation itself lives in link_inspect.audit_links now,
    # shared with the drift hook; only the printing below stays here.
    findings, foreign, dirs_audited = link_inspect.audit_links(
        repo_root=ctx.repo_root,
        home=ctx.home,
        harnesses=ctx.opts.harnesses,
        is_mac=ctx.is_mac,
        is_linux=ctx.is_linux,
        is_wsl=ctx.is_wsl,
        profile=ctx.opts.profile,
        manifest_file=ctx.manifest.path,
        format_path=ctx.display,
        report_uninstalled=ctx.opts.report_uninstalled,
        specs=specs,
        managed_dirs=managed_dirs,
    )

    _header("==> links.toml audit (read-only)", quiet=ctx.opts.quiet)
    for root, count in sorted(foreign.items()):
        print(
            PALETTE.dim(
                f"  note: {count} link(s) point into {root} rather than this "
                f"checkout ({ctx.repo_root}) — you are running from a worktree, "
                "so those entries were not audited. Re-run --check-links from "
                "that checkout to include them."
            )
        )

    total = sum(len(lines) for lines in findings.values())
    if not total:
        audited = len(specs) - sum(foreign.values())
        print(
            PALETTE.ok(
                f"  {audited} of {len(specs)} entries checked — every applicable "
                "link is present, correct, and backed by a file that exists."
            )
        )
        if dirs_audited:
            noun = "directory" if dirs_audited == 1 else "directories"
            print(
                PALETTE.ok(
                    f"  {dirs_audited} declared exclusive {noun} checked — "
                    "nothing foreign in any of them."
                )
            )
        return 0

    for bucket in CHECK_BUCKETS:
        lines = findings[bucket]
        if not lines:
            continue
        print(PALETTE.header(f"  {bucket} ({len(lines)}):"))
        for line in sorted(lines):
            print(PALETTE.warn(f"    {line}"))

    print(PALETTE.warn(f"⚠ {total} link problem(s) found — nothing was changed."))
    return 1


# ── entry point ───────────────────────────────────────────────────────────────


def run_install(ctx: Context, specs: Sequence[LinkSpec]) -> int:
    """Run every install step in order and return the process exit status.

    Refuses to start — before anything is mutated — if two distinct
    sources would claim the same destination (design point 3 of the
    Fidelity local-skill-fork plan): a structural inconsistency in
    links.toml itself, not a step that failed at runtime, so this is the
    one place install.py's "nothing aborts the run" rule doesn't apply —
    it never entered the run to begin with, same as a malformed
    links.toml already refuses in ``main`` before reaching here.
    """
    links = gather_links(ctx, specs)
    collision = _find_link_collision(links)
    if collision is not None:
        dest, first, second = collision
        print(
            PALETTE.error(
                f"{ctx.display(dest)} would be linked by both {first!r} and "
                f"{second!r} — rename one of them and re-run"
            ),
            file=sys.stderr,
        )
        return 2

    capture_departure_baseline(ctx, specs)
    ctx.manifest.init_run(ctx.opts.profile, quiet=ctx.opts.quiet)
    if ctx.opts.dry_run:
        _header(
            f"==> DRY RUN — no changes will be made. Profile: {ctx.opts.profile}",
            quiet=ctx.opts.quiet,
        )
    else:
        _header(
            f"==> Installing with profile: {ctx.opts.profile}", quiet=ctx.opts.quiet
        )

    install_symlinks(ctx, links)
    _cleanup_orphaned_links(ctx, links)
    sync_codex_skills(ctx)
    opencode_drift = seed_opencode_config(ctx)
    settings_drift = seed_claude_settings(ctx)
    pi_settings_drift = seed_pi_settings(ctx)

    capture_service_baseline(ctx)
    enable_managed_services(ctx)
    capture_git_hooks_path_baseline(ctx)
    install_global_git_hooks_path(ctx)

    write_profile_marker(ctx)

    if ctx.departure_baseline is not None:
        depart.save_baseline(ctx.state_dir, ctx.departure_baseline)

    print_summary(ctx, settings_drift, opencode_drift, (), pi_settings_drift)
    return 1 if ctx.reporter.skipped else 0


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, then roll back, depart, audit links, or install.

    Returns:
        Process exit status: 0 clean, 1 something was skipped (or, under
        ``--check-links``, something was found), 2 refused (bad arguments,
        an unreadable links.toml, or the work-profile guard).
    """
    # Canonical Palette lives in cli_common (shared with settings_seed);
    # mutate its state rather than rebinding the name so every importer
    # observes the same color state.
    cli_common.PALETTE.enabled = color_enabled(sys.stdout)

    opts = parse_args(sys.argv[1:] if argv is None else argv)
    ctx = build_context(opts)

    if opts.rollback:
        return do_rollback(ctx)

    if opts.depart:
        return do_depart(ctx)

    if opts.check_links:
        try:
            return do_check_links(ctx)
        except (ValueError, TypeError) as exc:
            print(
                PALETTE.error(f"could not read the symlink table: {exc}"),
                file=sys.stderr,
            )
            return 2

    if work_guard_blocks(ctx):
        print(
            PALETTE.error(
                f"This machine is provisioned as WORK (marker: {ctx.profile_marker})."
            ),
            file=sys.stderr,
        )
        print(
            PALETTE.error(
                "Pass --profile=work, or --force to provision as personal anyway."
            ),
            file=sys.stderr,
        )
        return 2

    harness_errors = check_harness_binaries(ctx)
    if harness_errors:
        for err in harness_errors:
            print(PALETTE.error(f"✗ {err}"), file=sys.stderr)
        print(
            PALETTE.error(
                "\nNothing was configured. Re-run after installing, or pass --force-harness to configure anyway."
            ),
            file=sys.stderr,
        )
        return 2

    try:
        specs = load_links(ctx.repo_root / "links.toml")
    except (ValueError, TypeError) as exc:
        print(
            PALETTE.error(f"could not read the symlink table: {exc}"), file=sys.stderr
        )
        return 2

    return run_install(ctx, specs)


if __name__ == "__main__":
    raise SystemExit(main())
