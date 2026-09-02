#!/usr/bin/env python3
"""install.py — dotfiles + AI-harness provisioner for macOS and Linux/WSL.

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
  (``~/.local/state/dotfiles/history.jsonl``) that never gets truncated, so
  ``--rollback`` reverses *every* run ever recorded, not just the most
  recent one. Packages are reported but never uninstalled.

The dotfile symlink table itself lives in ``links.toml`` next to this file,
not in code — see that file's header for the per-entry schema.

Flags
  --quiet, -q    suppress non-essential output
  --verbose, -v  emit extra diagnostic messages to stderr

Requires Python 3.12+.
"""

import argparse
import contextlib
import fnmatch
import json
import os
import platform
import plistlib
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import NoReturn

sys.path.insert(0, str(Path(__file__).resolve().parent / "claude" / "scripts"))

import cli_common  # noqa: E402 — sibling dir inserted above

import depart

VALID_HARNESSES = ("claude", "copilot", "opencode", "agy", "pi")
VALID_PROFILES = ("personal", "work")

# Pinned rather than "latest" so every machine ends up with byte-identical
# fonts; bump manually to upgrade. A version-marker file next to the fonts
# keeps re-runs from re-downloading and re-extracting 90+ files every time.
NERD_FONT_VERSION = "3.4.0"
NERD_FONT_URL = (
    "https://github.com/ryanoasis/nerd-fonts/releases/download/"
    f"v{NERD_FONT_VERSION}/JetBrainsMono.zip"
)

# Pinned for the same reason as NERD_FONT_VERSION above, and matched to the
# release already verified working (hash-identical) on the machine this
# fallback path was first built for. Bump manually to upgrade. Only used on
# Linux — apt/dnf's neovim is frequently years behind upstream, with no
# in-repo mechanism to track a moving "latest".
NEOVIM_FALLBACK_VERSION = "0.12.4"
NEOVIM_FALLBACK_ASSETS = {
    "x86_64": "nvim-linux-x86_64.tar.gz",
    "aarch64": "nvim-linux-arm64.tar.gz",
}

BREW_FORMULAE = (
    "python@3.13",
    "uv",
    "ruff",
    "tmux",
    "zoxide",
    "eza",
    "bat",
    "ripgrep",
    "lsd",
    "ncdu",
    "tldr",
    "oh-my-posh",
    "neovim",
    "fd",
)
BREW_CASKS = (
    "karabiner-elements",
    "rectangle",
    "ghostty",
    "visual-studio-code",
    "alt-tab",
    "font-jetbrains-mono-nerd-font",
)
LINUX_PACKAGES = (
    "tmux",
    "zoxide",
    "eza",
    "bat",
    "lsd",
    "ncdu",
    "tldr",
    "ripgrep",
    "unzip",
    "lsof",
    "xclip",
    "fontconfig",
    "neovim",
    "fd-find",
)

# Caps Lock → Escape, in the numeric form macOS stores keyboard modifier
# remaps in (HID usage page << 32 | usage).
CAPS_LOCK_TO_ESCAPE = [
    {
        "HIDKeyboardModifierMappingSrc": 30064771129,  # Caps Lock
        "HIDKeyboardModifierMappingDst": 30064771113,  # Escape
    }
]

USAGE = """\
usage: ./install.sh --harness=<claude,copilot,opencode,agy,pi>[,...] [--profile=personal|work] [--rollback] [--wipe] [--force] [--dry-run] [--no-nvim-pin] [--reseed | --adopt] [--quiet | --verbose]
       ./install.sh --depart [--yes] [--dry-run] [--quiet | --verbose]
       ./install.sh --check-links [--harness=...] [--profile=personal|work] [--quiet | --verbose]

  --quiet, -q   suppress non-essential output
  --verbose, -v emit extra diagnostic messages to stderr
  --harness   required for an install run; not needed by the undo/audit
              actions (--rollback, --depart, --check-links), though
              --check-links accepts it to scope which entries apply.
              Comma-separated, at least one of:
              claude, copilot, opencode, agy, pi. No default — every run must
              state its intent explicitly. Purely additive: omitting a harness
              you previously selected does NOT uninstall or clean it up,
              it just skips re-provisioning it this run. Removal is a
              --rollback concern (reverses every run recorded in the
              history file, not just the most recent one) or manual cleanup.
  --profile   personal (default) or work. Controls machine-level concerns:
              excludes every personal-only managed service (watchcommit,
              opencode-skills-sync), excludes personal API-key setup, seeds
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
              pre-dotfiles files from their .bak backups, deletes the
              backups outright, so nothing dotfiles-related is left behind.
              Also sweeps untracked state the installer creates but never
              records in its history — Neovim's XDG state dirs
              (~/.local/share/nvim, ~/.local/state/nvim, ~/.cache/nvim) and,
              on Linux, every managed systemd --user service (disabled
              and stopped). These are NOT where a Neovim binary itself
              belongs — a self-contained Neovim install (its share/nvim/
              runtime tree) must live outside ~/.local/share/nvim (e.g.
              ~/.local/opt/neovim, what _install_neovim_fallback uses), or
              --wipe deletes it along with everything else here. Packages
              are still never touched. Excludes the
              macOS watchcommit launchd agent, Rectangle preferences, and
              the Caps Lock→Escape remap — no clean filesystem-delete
              equivalent for those. Requires --rollback.
  --force     override the work-profile guard on a machine previously
              provisioned with --profile=work
  --dry-run   print what every step would do without doing it: no packages
              installed, no files written/symlinked/removed, no history
              written. Detection (what's already installed, which
              profile/harness branches apply) still runs for real, so the
              preview reflects actual machine state. The one flag allowed
              alongside --rollback, to preview an undo before running it.
  --no-nvim-pin  Linux only. By default, _install_neovim_fallback always
              ends up with ~/.local/bin/nvim pinned to
              NEOVIM_FALLBACK_VERSION, even when the distro's own neovim
              package already clears the 0.11 floor — reproducible across
              machines regardless of what apt/dnf happens to ship. This
              flag restores the old rescue-only behavior: the fallback is
              only installed when the neovim on PATH is missing, too old,
              or has a broken runtime; a distro package that's merely
              "good enough" is left alone rather than overridden.
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


# ── terminal colors ───────────────────────────────────────────────────────────


class Palette:
    """ANSI colorizer that no-ops when color isn't appropriate.

    Raw escape codes rather than a third-party library: this script runs on
    a machine that by definition hasn't been provisioned yet, so it can only
    depend on the standard library.
    """

    RESET = "\x1b[0m"

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def _wrap(self, text: str, code: str) -> str:
        return f"{code}{text}{self.RESET}" if self.enabled else text

    def header(self, text: str) -> str:
        """Section header (``==>`` lines) and the summary banner."""
        return self._wrap(text, "\x1b[1;36m")

    def ok(self, text: str) -> str:
        """A mutation that succeeded."""
        return self._wrap(text, "\x1b[32m")

    def warn(self, text: str) -> str:
        """A skipped step or a drift report — not fatal, but read it."""
        return self._wrap(text, "\x1b[33m")

    def error(self, text: str) -> str:
        """A hard error (argument errors, blocked run)."""
        return self._wrap(text, "\x1b[31m")

    def dim(self, text: str) -> str:
        """Dry-run previews and other informational asides."""
        return self._wrap(text, "\x1b[2m")


def color_enabled(stream: object) -> bool:
    """Return whether ANSI codes should be emitted to ``stream``.

    Honors the ``NO_COLOR`` convention (any non-empty value disables color)
    and ``TERM=dumb``, and otherwise only colorizes an interactive terminal
    so piped/redirected output stays clean for grep.
    """
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    isatty = getattr(stream, "isatty", None)
    return bool(isatty and isatty())


# Rebound in main() once the output stream is known. Default-off so any
# import-time or test-time use is plain text.
PALETTE = Palette(False)


# ── skip-and-report plumbing ──────────────────────────────────────────────────


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
        ``package-installed`` ``name``
    """

    path: Path
    dry_run: bool = False

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

    def record_package(self, name: str) -> None:
        """Record an installed package (reported, never uninstalled, on undo)."""
        self._record({"kind": "package-installed", "name": name})

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
        """
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
    no_nvim_pin: bool = False
    reseed: bool = False
    adopt: bool = False
    depart: bool = False
    yes: bool = False
    check_links: bool = False
    report_uninstalled: bool = False
    quiet: bool = False
    verbose: bool = False


@dataclass
class Context:
    """Everything a step needs: paths, options, history, and the skip tally."""

    dotfiles: Path
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
        """Where the history log and profile marker live."""
        return self.home / ".local" / "state" / "dotfiles"

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
        try:
            return f"~/{path.relative_to(self.home)}"
        except ValueError:
            return str(path)


def detect_wsl(system: str) -> bool:
    """Return whether this is a WSL kernel (as opposed to native Linux)."""
    if system != "Linux":
        return False
    if os.environ.get("WSL_DISTRO_NAME"):
        return True
    try:
        return "microsoft" in Path("/proc/version").read_text().lower()
    except OSError:
        return False


def build_context(opts: Options, dotfiles: Path | None = None) -> Context:
    """Assemble a :class:`Context` for a real run on this machine."""
    root = dotfiles or Path(__file__).resolve().parent
    home = Path.home()
    system = platform.system()
    state_dir = home / ".local" / "state" / "dotfiles"
    return Context(
        dotfiles=root,
        home=home,
        opts=opts,
        manifest=Manifest(state_dir / "history.jsonl", dry_run=opts.dry_run),
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
    parser.add_argument("--profile", default="personal")
    # append, not store: --harness=claude --harness=copilot must accumulate
    # both, not silently drop the first on the second flag.
    parser.add_argument("--harness", action="append", default=[])
    parser.add_argument("--rollback", action="store_true")
    parser.add_argument("--wipe", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", dest="dry_run", action="store_true")
    parser.add_argument("--no-nvim-pin", dest="no_nvim_pin", action="store_true")
    parser.add_argument("--reseed", action="store_true")
    parser.add_argument("--adopt", action="store_true")
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
                "(must be claude, copilot, opencode, agy, and/or pi)"
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
        or args.no_nvim_pin
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
        or args.no_nvim_pin
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
            "claude, copilot, opencode, agy, pi",
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
        no_nvim_pin=args.no_nvim_pin,
        reseed=args.reseed,
        adopt=args.adopt,
        depart=args.depart,
        yes=args.yes,
        check_links=args.check_links,
        report_uninstalled=args.report_uninstalled,
        quiet=args.quiet,
        verbose=args.verbose,
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


def _preview(message: str, *, quiet: bool = False) -> None:
    """Print a dry-run preview line."""
    cli_common.qprint(PALETTE.dim(f"  [dry-run] {message}"), quiet=quiet)


def _header(message: str, *, quiet: bool = False) -> None:
    """Print a section header line."""
    cli_common.qprint(PALETTE.header(message), quiet=quiet)


# ── packages: macOS ───────────────────────────────────────────────────────────


def _apply_brew_shellenv(brew: Path) -> None:
    """Put Homebrew on PATH for the rest of this process.

    The shell version could ``eval "$(brew shellenv)"``; a Python process
    can't source shell output, so the ``export KEY="value"`` lines are
    parsed back into ``os.environ`` instead.
    """
    result = run_command([str(brew), "shellenv"], capture=True)
    if not result.ok:
        return
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line.startswith("export "):
            continue
        assignment = line[len("export ") :].rstrip(";")
        key, _, value = assignment.partition("=")
        if key:
            os.environ[key] = value.strip().strip('"')


def install_mac_packages(ctx: Context) -> None:
    """Bootstrap Homebrew if needed, then install the formulae and casks."""
    if not have("brew"):
        if ctx.opts.dry_run:
            # Homebrew itself is a real mutation, so it's previewed rather
            # than installed — which means the "brew unavailable" branch
            # below fires in dry-run on a brew-less machine just like it
            # would for real. A preview can only see one install-time
            # dependency deep.
            _preview(
                "would install Homebrew "
                "(curl raw.githubusercontent.com/Homebrew/install | bash)",
                quiet=ctx.opts.quiet,
            )
        else:
            _header("==> Installing Homebrew...", quiet=ctx.opts.quiet)
            installed = run_command(
                '/bin/bash -c "$(curl -fsSL '
                'https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"',
                shell=True,
            )
            if not installed.ok:
                ctx.reporter.skip("Homebrew", "installer failed (network blocked?)")

    for candidate in (Path("/opt/homebrew/bin/brew"), Path("/usr/local/bin/brew")):
        if os.access(candidate, os.X_OK):
            _apply_brew_shellenv(candidate)
            break

    if not have("brew"):
        ctx.reporter.skip("brew formulae + casks", "brew unavailable")
        return

    if ctx.opts.dry_run:
        _preview(
            f"would install formulae: {' '.join(BREW_FORMULAE)}", quiet=ctx.opts.quiet
        )
        _preview(f"would install casks: {' '.join(BREW_CASKS)}", quiet=ctx.opts.quiet)
        return

    _header("==> Installing formulae...", quiet=ctx.opts.quiet)
    if run_command(["brew", "install", *BREW_FORMULAE]).ok:
        ctx.manifest.record_package("brew formulae")
    else:
        ctx.reporter.skip("brew formulae", "brew install failed")

    _header("==> Installing casks...", quiet=ctx.opts.quiet)
    if run_command(["brew", "install", "--cask", *BREW_CASKS]).ok:
        ctx.manifest.record_package("brew casks")
    else:
        ctx.reporter.skip("brew casks", "brew install --cask failed")


# ── packages: Linux ───────────────────────────────────────────────────────────


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


def _record_package_transaction(
    ctx: Context,
    manager: str,
    requested: list[str],
    before: dict[str, str] | None,
    after: dict[str, str] | None,
    epoch: dict[str, str] | None,
) -> None:
    """Record one package transaction onto ctx.departure_baseline, if tracking."""
    if ctx.departure_baseline is None or before is None or after is None:
        return
    depart.record_transaction(
        ctx.departure_baseline,
        manager=manager,
        requested=requested,
        before=before,
        after=after,
        captured_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        epoch=epoch,
    )


def _install_linux_packages_one_by_one(
    ctx: Context, manager: str, epoch: dict[str, str] | None
) -> None:
    """Install each package with its own package-manager invocation.

    Deliberately not one batched install command: ``apt-get install`` fails
    atomically on the first unresolvable name, which would block every
    package after it — e.g. eza/lsd don't exist before Ubuntu 24.04, which
    used to silently take tmux/bat/ncdu/tldr/ripgrep/unzip down with them on
    22.04 machines. Same reasoning applies to dnf.
    """
    base = (
        ["sudo", "dnf", "install", "-y"]
        if manager == "dnf"
        else ["sudo", "apt-get", "install", "-y"]
    )
    _header(f"==> Installing packages ({manager})...", quiet=ctx.opts.quiet)
    for pkg in LINUX_PACKAGES:
        if ctx.opts.dry_run:
            _preview(f"would run: {' '.join(base)} {pkg}", quiet=ctx.opts.quiet)
            continue
        before = (
            _capture_package_snapshot(manager)
            if ctx.departure_baseline is not None
            else None
        )
        outcome = run_command([*base, pkg])
        after = (
            _capture_package_snapshot(manager)
            if ctx.departure_baseline is not None
            else None
        )
        _record_package_transaction(ctx, manager, [pkg], before, after, epoch)
        if outcome.ok:
            ctx.manifest.record_package(pkg)
        else:
            ctx.reporter.skip(
                f"{manager} package: {pkg}",
                "not available in this release's repos, or install failed",
            )


def _shim(ctx: Context, shim_name: str, real_name: str) -> None:
    """Point ``~/.local/bin/<shim_name>`` at a differently-named binary.

    Debian/Ubuntu rename two of these packages' binaries to avoid conflicts
    (bat → batcat, fd → fdfind); Fedora installs them under their real
    names, so this is a no-op there.
    """
    if not have(real_name) or have(shim_name):
        return
    target = ctx.home / ".local" / "bin" / shim_name
    if ctx.opts.dry_run:
        _preview(
            f"would shim {shim_name} → {real_name} ({ctx.display(target)})",
            quiet=ctx.opts.quiet,
        )
        return
    real_path = Path(shutil.which(real_name) or real_name)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() or target.exists():
        target.unlink()
    target.symlink_to(real_path)
    ctx.manifest.record_symlink(target, real_path)
    cli_common.qprint(
        PALETTE.ok(f"  shimmed {shim_name} → {real_name}"), quiet=ctx.opts.quiet
    )


def _install_uv(ctx: Context) -> None:
    """Install uv (not packaged in apt/dnf) via the official installer."""
    if have("uv"):
        return
    if ctx.opts.dry_run:
        _preview(
            "would install uv (curl astral.sh/uv/install.sh | sh)", quiet=ctx.opts.quiet
        )
        return
    _header("==> Installing uv...", quiet=ctx.opts.quiet)
    if run_command("curl -LsSf https://astral.sh/uv/install.sh | sh", shell=True).ok:
        ctx.manifest.record_package("uv")
        # The installer writes ~/.local/bin/env for shells to source; this
        # process can't source it, so put the same directory on PATH
        # directly, otherwise the `uv tool install ruff` step below can't
        # see the uv that was just installed.
        _prepend_path(ctx.home / ".local" / "bin")
    else:
        ctx.reporter.skip("uv", "installer failed (network blocked?)")


def _prepend_path(directory: Path) -> None:
    """Put ``directory`` at the front of this process's PATH."""
    os.environ["PATH"] = f"{directory}{os.pathsep}{os.environ.get('PATH', '')}"


def _install_nerd_font(ctx: Context) -> None:
    """Install the pinned JetBrainsMono Nerd Font, unless already at version.

    The patched (icon-glyph) variant isn't in apt/dnf at all, so the release
    zip is pulled directly.
    """
    font_dir = ctx.home / ".local" / "share" / "fonts" / "JetBrainsMonoNerdFont"
    marker = font_dir / ".nerd-fonts-version"
    try:
        current = marker.read_text().strip()
    except OSError:
        current = ""
    if current == NERD_FONT_VERSION:
        return

    if ctx.opts.dry_run:
        _preview(
            f"would install JetBrainsMono Nerd Font v{NERD_FONT_VERSION} to {font_dir}",
            quiet=ctx.opts.quiet,
        )
        return

    _header(
        f"==> Installing JetBrainsMono Nerd Font v{NERD_FONT_VERSION}...",
        quiet=ctx.opts.quiet,
    )
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        archive = tmp_dir / "JetBrainsMono.zip"
        downloaded = run_command(["curl", "-fLo", str(archive), NERD_FONT_URL]).ok
        extracted = False
        if downloaded:
            font_dir.mkdir(parents=True, exist_ok=True)
            extracted = run_command(
                ["unzip", "-oq", str(archive), "-d", str(font_dir)]
            ).ok
        if downloaded and extracted:
            marker.write_text(f"{NERD_FONT_VERSION}\n")
            ctx.manifest.record_package(f"JetBrainsMono Nerd Font v{NERD_FONT_VERSION}")
            run_command(["fc-cache", "-f", str(font_dir)], capture=True)
            # Snapshotted after fc-cache deliberately: anything it leaves
            # inside the font directory is still installer-produced, so it
            # belongs in the manifest. This is departure's only evidence of
            # what the installer put here, and the last chance to take it
            # before the user can add fonts of their own.
            if ctx.departure_baseline is not None:
                depart.record_installed_tree(ctx.departure_baseline, font_dir)
            cli_common.qprint(
                PALETTE.ok(f"  installed to {font_dir}"), quiet=ctx.opts.quiet
            )
        else:
            ctx.reporter.skip(
                "JetBrainsMono Nerd Font",
                "download/extract failed (network blocked, or unzip missing?)",
            )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _fallback_already_pinned(ctx: Context) -> bool:
    """Whether ~/.local/bin/nvim already resolves to our own pinned install.

    Checked by identity (the shim must actually be our symlink into
    ~/.local/opt/neovim), not just by version number — a same-numbered
    binary from some other source shouldn't count, since only our symlink
    is guaranteed to keep pointing at NEOVIM_FALLBACK_VERSION as that
    constant changes. Compares the full ``NVIM vX.Y.Z`` line, not just
    (major, minor) like :func:`parse_neovim_version` — a patch-only pin
    bump must still be detected as "not yet pinned".
    """
    prefix = ctx.home / ".local" / "opt" / "neovim"
    shim = ctx.home / ".local" / "bin" / "nvim"
    nvim_bin = prefix / "bin" / "nvim"
    if not shim.is_symlink():
        return False
    try:
        if shim.resolve() != nvim_bin.resolve():
            return False
    except OSError:
        return False
    result = run_command([str(shim), "--version"], capture=True)
    if not result.ok:
        return False
    first_line = result.stdout.splitlines()[0] if result.stdout.strip() else ""
    return first_line == f"NVIM v{NEOVIM_FALLBACK_VERSION}"


def _install_neovim_fallback(ctx: Context) -> None:
    """Fetch a modern, pinned Neovim onto Linux.

    apt/dnf's neovim (LINUX_PACKAGES) is frequently below the 0.11 floor
    this repo's vendored config needs, and has no upstream mechanism to fix
    that short of a PPA. By default this always ends up with
    ~/.local/bin/nvim pinned to NEOVIM_FALLBACK_VERSION, even when the
    distro package already clears the floor — reproducible across machines
    regardless of what apt/dnf happens to ship. ``--no-nvim-pin`` restores
    the old rescue-only behavior: only install when the Neovim currently on
    PATH is missing, too old, or has a broken runtime (this repo's own
    incident — see _wipe_neovim_dirs). Either way, installs land in
    ~/.local/opt/neovim (never ~/.local/share/nvim — see the warning there)
    with ~/.local/bin/nvim symlinked at it.
    """
    if _fallback_already_pinned(ctx):
        return

    version, runtime_ok = _neovim_status()
    already_good = (
        version is not None and (version[0], version[1]) >= (0, 11) and runtime_ok
    )
    if ctx.opts.no_nvim_pin and already_good:
        return

    machine = platform.machine()
    asset = NEOVIM_FALLBACK_ASSETS.get(machine)
    if asset is None:
        ctx.neovim_fallback_failure = f"unsupported architecture {machine!r}"
        ctx.reporter.skip(
            "Neovim fallback install",
            f"unsupported architecture {machine!r} — apt's Neovim "
            "(if usable) is what you get",
        )
        return

    prefix = ctx.home / ".local" / "opt" / "neovim"
    url = (
        "https://github.com/neovim/neovim/releases/download/"
        f"v{NEOVIM_FALLBACK_VERSION}/{asset}"
    )

    if ctx.opts.dry_run:
        _preview(
            f"would install Neovim v{NEOVIM_FALLBACK_VERSION} to {ctx.display(prefix)}",
            quiet=ctx.opts.quiet,
        )
        return

    _header(
        f"==> Installing Neovim v{NEOVIM_FALLBACK_VERSION} (apt's Neovim is too old or broken)...",
        quiet=ctx.opts.quiet,
    )
    tmp_dir = Path(tempfile.mkdtemp())
    try:
        archive = tmp_dir / asset
        downloaded = run_command(["curl", "-fLo", str(archive), url]).ok
        extracted = (
            downloaded
            and run_command(["tar", "xzf", str(archive), "-C", str(tmp_dir)]).ok
        )
        extracted_dir = tmp_dir / asset.removesuffix(".tar.gz")
        if extracted and not extracted_dir.is_dir():
            # Upstream's tarball top-level directory name is assumed to
            # match the asset name minus its extension; fall back to
            # "whatever single directory the archive actually produced" so
            # a naming-convention change degrades to a skip instead of a
            # wrong-but-silent path.
            candidates = [p for p in tmp_dir.iterdir() if p.is_dir()]
            extracted_dir = candidates[0] if len(candidates) == 1 else None
        if extracted and extracted_dir and extracted_dir.is_dir():
            if prefix.exists() or prefix.is_symlink():
                # Only consult the baseline when there's actually something
                # at prefix that removal could affect -- a clean install
                # (nothing here yet) must never be gated on
                # departure_baseline, since there's nothing to protect
                # against either way.
                if ctx.departure_baseline is None:
                    # is_linux and not dry_run always hold whenever prefix
                    # already exists on a real run (_install_neovim_fallback
                    # only runs under install_linux_packages, and returns
                    # before this point under --dry-run; capture_departure_baseline
                    # is a no-op under the identical condition) -- this is a
                    # defensive fallback for that invariant breaking in a
                    # future refactor, not an expected path today. Fails
                    # *safe* (skip) rather than reproducing the unguarded
                    # clobber this check exists to remove.
                    ctx.neovim_fallback_failure = (
                        "no departure baseline available to verify this "
                        "install is safe to replace"
                    )
                    ctx.reporter.skip(
                        "Neovim fallback install",
                        f"{ctx.neovim_fallback_failure} — remove "
                        f"{ctx.display(prefix)} by hand, then re-run to reinstall",
                    )
                    return

                verdict = depart.installed_tree_verdict(ctx.departure_baseline, prefix)
                if verdict == depart.TREE_MODIFIED:
                    ctx.neovim_fallback_failure = (
                        "existing install may contain changes you made after installing"
                    )
                    ctx.reporter.skip(
                        "Neovim fallback install",
                        f"{ctx.neovim_fallback_failure} (unproven safe to "
                        f"remove) — remove {ctx.display(prefix)} by hand, "
                        "then re-run to reinstall",
                    )
                    return

                # TREE_UNCHANGED or TREE_UNRECORDED (self-heal) -- proceed.
                if prefix.is_symlink() or prefix.is_file():
                    prefix.unlink()
                elif prefix.is_dir():
                    shutil.rmtree(prefix)
            prefix.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(extracted_dir), str(prefix))
            ctx.manifest.record_package(
                f"Neovim v{NEOVIM_FALLBACK_VERSION} (fallback tarball)"
            )

            nvim_bin = prefix / "bin" / "nvim"
            shim = ctx.home / ".local" / "bin" / "nvim"
            shim.parent.mkdir(parents=True, exist_ok=True)
            if shim.is_symlink() or shim.exists():
                shim.unlink()
            shim.symlink_to(nvim_bin)
            ctx.manifest.record_symlink(shim, nvim_bin)
            # See the Nerd Font call site: departure will only remove this
            # tree wholesale if it still matches this snapshot exactly.
            if ctx.departure_baseline is not None:
                depart.record_installed_tree(ctx.departure_baseline, prefix)
            cli_common.qprint(
                PALETTE.ok(
                    f"  installed to {ctx.display(prefix)}, linked from {ctx.display(shim)}"
                ),
                quiet=ctx.opts.quiet,
            )
        else:
            ctx.neovim_fallback_failure = (
                "download/extract failed, or archive layout unexpected"
            )
            ctx.reporter.skip(
                "Neovim fallback install",
                "download/extract failed, or archive layout unexpected "
                "(network blocked, tar missing, or upstream changed the tarball layout?)",
            )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _install_ruff_uv_tool(ctx: Context) -> None:
    """Install ruff via ``uv tool install``, recording a transaction if tracking."""
    if not have("uv"):
        ctx.reporter.skip("ruff", "uv unavailable")
        return
    if ctx.opts.dry_run:
        _preview("would run: uv tool install ruff", quiet=ctx.opts.quiet)
        return
    before = (
        _capture_package_snapshot("uv-tool")
        if ctx.departure_baseline is not None
        else None
    )
    outcome = run_command(["uv", "tool", "install", "ruff"])
    after = (
        _capture_package_snapshot("uv-tool")
        if ctx.departure_baseline is not None
        else None
    )
    _record_package_transaction(ctx, "uv-tool", ["ruff"], before, after, epoch=None)
    if outcome.ok:
        ctx.manifest.record_package("ruff")
    else:
        ctx.reporter.skip("ruff", "uv tool install failed")


def install_linux_packages(ctx: Context) -> None:
    """Install everything the Linux/WSL branch owns: distro packages and extras."""
    manager = "dnf" if have("dnf") else "apt"
    # Captured once, immediately before any package-manager mutation this
    # run makes — the comparand for each manager's *first* transaction in
    # the interference-detection scheme, not re-probed per package.
    epoch = (
        _capture_package_snapshot(manager)
        if ctx.departure_baseline is not None and not ctx.opts.dry_run
        else None
    )

    if manager == "dnf":
        if ctx.opts.dry_run:
            _preview("would run: sudo dnf makecache", quiet=ctx.opts.quiet)
        else:
            _header("==> Refreshing dnf package metadata...", quiet=ctx.opts.quiet)
            if not run_command(["sudo", "dnf", "makecache"]).ok:
                ctx.reporter.skip(
                    "dnf makecache", "dnf makecache failed (offline or blocked?)"
                )
        _install_linux_packages_one_by_one(ctx, "dnf", epoch)
    else:
        if ctx.opts.dry_run:
            _preview("would run: sudo apt-get update", quiet=ctx.opts.quiet)
        else:
            _header("==> Updating apt package lists...", quiet=ctx.opts.quiet)
            if not run_command(["sudo", "apt-get", "update"]).ok:
                ctx.reporter.skip(
                    "apt update", "apt-get update failed (offline or blocked?)"
                )
        _install_linux_packages_one_by_one(ctx, "apt", epoch)

    _install_neovim_fallback(ctx)

    _shim(ctx, "bat", "batcat")
    _shim(ctx, "fd", "fdfind")
    _install_uv(ctx)
    _install_ruff_uv_tool(ctx)

    if not have("oh-my-posh"):
        if ctx.opts.dry_run:
            _preview(
                "would install oh-my-posh "
                "(curl ohmyposh.dev/install.sh | bash -s -- -d ~/.local/bin)",
                quiet=ctx.opts.quiet,
            )
        else:
            _header("==> Installing oh-my-posh...", quiet=ctx.opts.quiet)
            bin_dir = ctx.home / ".local" / "bin"
            if run_command(
                f"curl -s https://ohmyposh.dev/install.sh | bash -s -- -d {bin_dir}",
                shell=True,
            ).ok:
                ctx.manifest.record_package("oh-my-posh")
            else:
                ctx.reporter.skip(
                    "oh-my-posh",
                    "installer failed (network blocked, or unzip missing?)",
                )

    _install_nerd_font(ctx)


# ── packages: Node-based harnesses ────────────────────────────────────────────


def _activate_nvm_node(ctx: Context) -> None:
    """Put the newest nvm-installed node on PATH for the rest of this process.

    ``nvm`` is a shell function, so unlike the shell version this process
    can't ``source nvm.sh`` and have ``npm`` appear on PATH — the installed
    node's bin directory is added explicitly instead.
    """
    versions = ctx.home / ".nvm" / "versions" / "node"
    if not versions.is_dir():
        return
    candidates = sorted(p for p in versions.iterdir() if (p / "bin").is_dir())
    if candidates:
        _prepend_path(candidates[-1] / "bin")


def install_node(ctx: Context) -> None:
    """Install NVM and a Node LTS — only for the harnesses that need npm.

    opencode and agy both manage their own runtime externally (agy is a
    standalone Go binary, not an npm package), so nothing here runs when
    they're the only selection.
    """
    if not (ctx.has_harness("claude") or ctx.has_harness("copilot")):
        return

    if not (ctx.home / ".nvm").is_dir():
        if ctx.opts.dry_run:
            _preview(
                "would install NVM (curl nvm-sh/nvm install.sh | bash)",
                quiet=ctx.opts.quiet,
            )
        else:
            _header("==> Installing NVM...", quiet=ctx.opts.quiet)
            if not run_command(
                "curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/HEAD/"
                "install.sh | bash",
                shell=True,
            ).ok:
                ctx.reporter.skip("NVM", "installer failed (network blocked?)")

    if have("npm"):
        return

    _activate_nvm_node(ctx)
    if have("npm"):
        return

    nvm_sh = ctx.home / ".nvm" / "nvm.sh"
    if not nvm_sh.is_file():
        return
    if ctx.opts.dry_run:
        _preview("would run: nvm install --lts", quiet=ctx.opts.quiet)
        return
    if run_command(f'. "{nvm_sh}" && nvm install --lts', shell=True).ok:
        _activate_nvm_node(ctx)
    else:
        ctx.reporter.skip("node", "nvm install --lts failed")


def install_npm_harness(ctx: Context, harness: str, label: str, package: str) -> None:
    """Install one npm-distributed harness CLI, if it was selected."""
    if not ctx.has_harness(harness):
        cli_common.qprint(
            f"  {label}: skipped (not in --harness)", quiet=ctx.opts.quiet
        )
        return
    if not have("npm"):
        ctx.reporter.skip(label, "npm unavailable (NVM install failed or skipped)")
        return
    if ctx.opts.dry_run:
        _preview(f"would run: npm install -g {package}", quiet=ctx.opts.quiet)
        return
    _header(f"==> Installing {label}...", quiet=ctx.opts.quiet)
    before = (
        _capture_package_snapshot("npm") if ctx.departure_baseline is not None else None
    )
    outcome = run_command(["npm", "install", "-g", package])
    after = (
        _capture_package_snapshot("npm") if ctx.departure_baseline is not None else None
    )
    _record_package_transaction(ctx, "npm", [package], before, after, epoch=None)
    if outcome.ok:
        ctx.manifest.record_package(package)
    else:
        ctx.reporter.skip(label, "npm install failed (registry blocked?)")


# ── symlink engine ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class LinkSpec:
    """One row of ``links.toml``: a repo file and where it gets linked."""

    src: str
    dest: str
    harness: str | None = None
    platform: str | None = None
    wsl: str | None = None
    profile_exclude: tuple[str, ...] = ()
    dir: bool = False


_LINK_FIELDS = {"src", "dest", "harness", "platform", "wsl", "profile_exclude", "dir"}


@dataclass(frozen=True)
class ManagedDirSpec:
    """One row of ``links.toml``: a directory dotfiles owns exclusively.

    Exclusivity is opt-in rather than inferred. Inferring it from every link's
    ``dest.parent`` surfaces 230 unmanaged entries to find 2 real ones, 68 of
    them in ``$HOME`` alone, because seven separate rows happen to land there.
    """

    dest: str
    ignore: tuple[str, ...] = ()


_MANAGED_DIR_FIELDS = {"dest", "ignore"}


def load_links(path: Path) -> list[LinkSpec]:
    """Parse ``links.toml`` into an ordered list of link specs.

    Unknown keys and bad values are rejected loudly rather than ignored — a
    typo'd gate (``harnes = "claude"``) would otherwise silently widen a
    link to every run.

    Args:
        path: Path to the TOML table.

    Returns:
        The specs, in file order.

    Raises:
        ValueError: If the file is malformed or an entry is invalid.
    """
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"{path}: {exc}") from exc

    rows = data.get("link", [])
    if not isinstance(rows, list):
        raise TypeError(f"{path}: expected a [[link]] array")

    specs: list[LinkSpec] = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise TypeError(f"{path}: entry {index} is not a table")
        unknown = sorted(set(row) - _LINK_FIELDS)
        if unknown:
            raise ValueError(f"{path}: entry {index} has unknown key(s): {unknown}")
        for required in ("src", "dest"):
            if not isinstance(row.get(required), str) or not row[required]:
                raise ValueError(f"{path}: entry {index} is missing '{required}'")
        harness = row.get("harness")
        if harness is not None and harness not in VALID_HARNESSES:
            raise ValueError(f"{path}: entry {index} has unknown harness {harness!r}")
        os_gate = row.get("platform")
        if os_gate is not None and os_gate not in ("mac", "linux"):
            raise ValueError(f"{path}: entry {index} has unknown platform {os_gate!r}")
        wsl = row.get("wsl")
        if wsl is not None and wsl not in ("only", "exclude"):
            raise ValueError(f"{path}: entry {index} has unknown wsl value {wsl!r}")
        excluded = row.get("profile_exclude", [])
        if not isinstance(excluded, list) or any(
            profile not in VALID_PROFILES for profile in excluded
        ):
            raise ValueError(f"{path}: entry {index} has invalid profile_exclude")
        dir_flag = row.get("dir", False)
        if not isinstance(dir_flag, bool):
            raise TypeError(f"{path}: entry {index} has non-bool 'dir'")
        specs.append(
            LinkSpec(
                src=row["src"],
                dest=row["dest"],
                harness=harness,
                platform=os_gate,
                wsl=wsl,
                profile_exclude=tuple(excluded),
                dir=dir_flag,
            )
        )
    return specs


def load_managed_dirs(path: Path) -> list[ManagedDirSpec]:
    """Parse the ``[[managed_dir]]`` rows declaring directories we own exclusively.

    Mirrors :func:`load_links` in refusing to guess — unknown keys and bad
    values are rejected loudly, because a typo'd row would otherwise silently
    widen or narrow the audit. An absent table is not an error: every
    ``links.toml`` predating this mechanism has none, and the audit then finds
    nothing declared.

    Args:
        path: Path to the TOML table.

    Returns:
        The specs, in file order.

    Raises:
        ValueError: If the file is malformed or an entry is invalid.
    """
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ValueError(f"{path}: {exc}") from exc

    rows = data.get("managed_dir", [])
    if not isinstance(rows, list):
        raise TypeError(f"{path}: expected a [[managed_dir]] array")

    specs: list[ManagedDirSpec] = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise TypeError(f"{path}: managed_dir entry {index} is not a table")
        unknown = sorted(set(row) - _MANAGED_DIR_FIELDS)
        if unknown:
            raise ValueError(
                f"{path}: managed_dir entry {index} has unknown key(s): {unknown}"
            )
        dest = row.get("dest")
        if not isinstance(dest, str) or not dest:
            raise ValueError(f"{path}: managed_dir entry {index} is missing 'dest'")
        raw_ignore = row.get("ignore", [])
        if not isinstance(raw_ignore, list) or any(
            not isinstance(item, str) or not item for item in raw_ignore
        ):
            raise ValueError(f"{path}: managed_dir entry {index} has invalid 'ignore'")
        specs.append(ManagedDirSpec(dest=dest, ignore=tuple(raw_ignore)))
    return specs


def link_applies(spec: LinkSpec, ctx: Context) -> bool:
    """Return whether ``spec`` should be linked for this run's machine/options."""
    if spec.harness is not None and not ctx.has_harness(spec.harness):
        return False
    if spec.platform == "mac" and not ctx.is_mac:
        return False
    if spec.platform == "linux" and not ctx.is_linux:
        return False
    if spec.wsl == "exclude" and ctx.is_wsl:
        return False
    if spec.wsl == "only" and not ctx.is_wsl:
        return False
    return ctx.opts.profile not in spec.profile_exclude


def expand_dest(dest: str, home: Path) -> Path:
    """Expand a ``links.toml`` destination against ``home``.

    Explicit rather than :func:`os.path.expanduser` so the destination
    tracks the context's home directory (which tests point at a temporary
    one) instead of the process environment.
    """
    if dest == "~":
        return home
    if dest.startswith("~/"):
        return home / dest[2:]
    return Path(dest)


_JUNK_SUFFIXES = ("~", ".swp", ".swo", ".tmp")


def iter_concrete_links(
    spec: LinkSpec, ctx: Context
) -> Iterator[tuple[Path, Path, str]]:
    """Expand one ``links.toml`` row into concrete ``(src, dest, relative_src)`` triples.

    A normal row (``dir`` unset) yields exactly one triple, unchanged from
    today. A ``dir=true`` row recursively globs its source directory,
    skipping plain subdirectory entries (walked into, never linked
    themselves) and junk — dotfiles and editor swap/backup files — so
    those never get symlinked in. A missing, empty, or unreadable source
    directory yields nothing, with no error and no special-casing: see the
    plan's cleanup design for why "can't confirm" and "confirmed empty"
    are deliberately not distinguished.
    """
    src_root = ctx.dotfiles / spec.src
    dest_root = expand_dest(spec.dest, ctx.home)
    if not spec.dir:
        yield src_root, dest_root, spec.src
        return
    try:
        candidates = sorted(src_root.rglob("*"))
    except OSError:
        return
    for path in candidates:
        if not (path.is_file() or path.is_symlink()):
            continue
        if path.name.startswith(".") or path.name.endswith(_JUNK_SUFFIXES):
            continue
        relative = path.relative_to(src_root)
        yield path, dest_root / relative, f"{spec.src}/{relative}"


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
    result: list[tuple[Path, Path, str, bool]] = []
    for spec in specs:
        applicable = link_applies(spec, ctx)
        for src, dest, rel in iter_concrete_links(spec, ctx):
            result.append((src, dest, rel, applicable))
    return result


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
            shutil.move(str(dest), str(backup))
        except (OSError, shutil.Error):
            ctx.reporter.skip(f"symlink {dest}", "could not back up existing file")
            return False
        ctx.manifest.record_backup(dest, backup)
        print(f"  Backing up {dest} → {backup}")

    try:
        # Unlink first rather than relying on an atomic replace: `ln -sf`
        # onto an existing symlink-to-a-directory would create the new link
        # *inside* that directory instead of replacing it.
        if dest.is_symlink() or dest.exists():
            dest.unlink()
        dest.symlink_to(src)
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
    _header("==> Symlinking dotfiles...", quiet=ctx.opts.quiet)

    if ctx.opts.profile == "work":
        cli_common.qprint(
            "  watchcommit: excluded (work profile)", quiet=ctx.opts.quiet
        )

    for src, dest, _rel, applicable in links:
        if not applicable:
            continue
        symlink(ctx, src, dest)


# ── copy-once seeds and drift detection ───────────────────────────────────────


def json_key_drift(seed: dict[str, object], live: dict[str, object]) -> list[str]:
    """Return the top-level keys whose values differ between seed and live."""
    return sorted(k for k in set(seed) | set(live) if seed.get(k) != live.get(k))


_BYPASS_BASH_PATTERNS = (
    # Take an arbitrary command as their own argument (awk via
    # ``system()``), so their presence isn't "individually risky command
    # a profile could allow" — it defeats the allowlist entirely.
    "xargs *",
    "awk *",
    "sqlite3 *",  # .shell/.system dot-commands run arbitrary shell
    "nohup *",
    # Broaden an otherwise-narrow, already-approved command into a wider
    # category that can reach arbitrary code.
    "git --no-pager *",  # matches any git subcommand, incl. commit/push
    "uv *",  # broadens past the 4 named uv commands; `uv run` is arbitrary
    "python3 -m *",  # any installed module, incl. ones with side effects
    # Inline arbitrary code evaluation.
    "node -e *",
    "python3 -c *",
    "python3 - *",
    # Network-fetches and runs lifecycle hooks / arbitrary packages.
    "npm install*",
    "npm install",
    "npx *",
    # Delegates to a CLI with its own separate permission model, or the
    # same CLI redirected/auto-approved via specific flags.
    "opencode run*",  # --auto/--dir make this a real bypass
    "copilot *",
)


def opencode_bypass_drift(
    seed: dict[str, object], live: dict[str, object]
) -> list[str]:
    """Return allowlist-bypass bash patterns present live but not in the seed.

    This is a curated, fixed set — not a generalized "any key live has
    that seed doesn't" diff. A generalized version would flag a live-only
    key that's merely narrower than, but already behaviorally covered by,
    an existing seed glob (e.g. a one-off interactively-approved
    ``git log --all`` against seed's ``git log*``) as false-positive
    drift. Every pattern here instead shares one of two properties that
    makes a legitimate interactive approval unlikely to ever collide with
    it: it takes an arbitrary command as its own argument (``xargs``,
    ``awk``, ``sqlite3``'s ``.shell``/``.system``, ``nohup``), or it
    broadens an otherwise-narrow, already-approved command into a wider
    category, evaluates code inline, fetches and runs external code, or
    delegates to a separate CLI/permission model entirely.

    This check is diff-gated (only runs when a caller already detected
    seed≠live) and deliberately doesn't attempt full policy compliance —
    only this bypass-shaped subset. It's also a snapshot of known bypass
    shapes, not a taxonomy: a future bypass-shaped tool not in this tuple
    (e.g. ``perl -e *``) isn't automatically caught here or by the seed's
    own policy-compliance test — a policy review has to catch that, same
    as any other undocumented addition. Full policy compliance for the
    *seed* itself (not just this bypass subset, and unconditional on any
    diff existing) is a separate, CI-only pytest check — see
    ``test/test_install.py``'s ``_APPROVED_BASH_PATTERNS``.
    """
    seed_bash = _bash_permissions(seed)
    live_bash = _bash_permissions(live)
    return [k for k in _BYPASS_BASH_PATTERNS if k in live_bash and k not in seed_bash]


def _bash_permissions(config: dict[str, object]) -> dict[str, object]:
    """Return ``permission.bash`` from an opencode config, or ``{}``."""
    permission = config.get("permission")
    if not isinstance(permission, dict):
        return {}
    bash = permission.get("bash")
    return bash if isinstance(bash, dict) else {}


def _load_json_pair_text(
    seed_text: str, live_text: str
) -> tuple[dict[str, object], dict[str, object]] | None:
    """Load a JSON object pair from already-read text."""
    try:
        seed_data = json.loads(seed_text)
        live_data = json.loads(live_text)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(seed_data, dict) or not isinstance(live_data, dict):
        return None
    return seed_data, live_data


def _describe_settings_text(seed_text: str, live_text: str) -> str:
    """Describe settings drift without rereading either side."""
    if seed_text == live_text:
        return ""
    pair = _load_json_pair_text(seed_text, live_text)
    if pair is None:
        return "content differs from the repo copy (unreadable or invalid JSON)"
    return ", ".join(json_key_drift(*pair))


def _describe_opencode_text(
    seed_text: str, live_text: str, *, adopt: bool = False
) -> str:
    """Describe opencode drift without rereading either side."""
    if seed_text == live_text:
        return ""
    pair = _load_json_pair_text(seed_text, live_text)
    if pair is None:
        return "content differs from the repo copy (unreadable or invalid JSON)"
    bypasses = opencode_bypass_drift(*pair)
    if bypasses:
        action = (
            "resolve manually before adopting"
            if adopt
            else "re-run with --reseed to fix"
        )
        return (
            f"SECURITY: {', '.join(bypasses)} still allowed in your live "
            f"opencode.jsonc (allowlist bypass) — {action}"
        )
    return ", ".join(json_key_drift(*pair))


def _describe_vscode_text(seed_text: str, live_text: str) -> str:
    """Describe VS Code JSON/JSONC drift without rereading either side."""
    if seed_text == live_text:
        return ""
    try:
        seed_data: object = json.loads(seed_text)
    except json.JSONDecodeError:
        seed_data = None
    try:
        live_data: object = json.loads(live_text)
    except json.JSONDecodeError:
        live_data = None
    if isinstance(seed_data, dict) and isinstance(live_data, dict):
        return ", ".join(json_key_drift(seed_data, live_data))
    if isinstance(seed_data, list) and isinstance(live_data, list):
        if len(seed_data) != len(live_data):
            return f"{len(live_data)} bindings live vs {len(seed_data)} in seed"
        return f"binding definitions differ ({len(live_data)} bindings)"
    return "content differs from the repo copy"


def describe_settings_drift(seed: Path, live: Path) -> str:
    """Describe how a live settings.json diverged from its seed.

    Text equality is checked first, before any JSON parsing is attempted —
    see ``describe_vscode_drift``'s docstring for why (this mirrors its
    exact shape). Only once text has already proven to differ does an
    unparseable live file get its own non-empty fallback, so a corrupted
    live settings.json is no longer invisible to drift reporting.
    """
    if not seed.is_file() or not live.is_file():
        return ""
    return _describe_settings_text(
        seed.read_text(encoding="utf-8"), live.read_text(encoding="utf-8")
    )


def describe_opencode_drift(seed: Path, live: Path) -> str:
    """Describe how a live opencode.jsonc diverged from its seed.

    A returned allowlist bypass outranks (and replaces) the generic key
    list: it's a security regression, not config drift to skim past. Text
    equality is checked before any JSON parsing, same as
    ``describe_settings_drift`` — this is what keeps a byte-identical
    ``opencode.jsonc`` containing a ``//`` comment from being misreported as
    drifted just because ``json.loads`` can't parse it.
    """
    if not seed.is_file() or not live.is_file():
        return ""
    return _describe_opencode_text(
        seed.read_text(encoding="utf-8"), live.read_text(encoding="utf-8")
    )


def describe_vscode_drift(seed: Path, live: Path) -> str:
    """Describe how a live VS Code settings/keybindings file diverged from its seed.

    Text equality is the definitive drift signal, not JSON equality: VS
    Code's live files are legal JSONC (``//`` comments, trailing commas),
    which ``json.loads`` can't parse, so a JSON-first check would miss real
    drift whenever the live file merely contains a comment. JSON parsing
    below only runs after text drift is already confirmed, purely to
    enrich the message. A missing seed or live file means there's nothing
    to compare — not a difference to report.
    """
    if not seed.is_file() or not live.is_file():
        return ""
    seed_text = seed.read_text(encoding="utf-8")
    live_text = live.read_text(encoding="utf-8")
    if seed_text == live_text:
        return ""

    return _describe_vscode_text(seed_text, live_text)


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
        seed = ctx.dotfiles / "vscode" / name
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
        )
        results.append((ctx.display(dest), (name, drift)))
    return results


def _load_json_pair(
    seed: Path, live: Path
) -> tuple[dict[str, object], dict[str, object]] | None:
    """Load a (seed, live) JSON pair, or None if either can't be read."""
    try:
        seed_text = seed.read_text(encoding="utf-8")
        live_text = live.read_text(encoding="utf-8")
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return _load_json_pair_text(seed_text, live_text)


def seed_file(
    ctx: Context,
    seed: Path,
    dest: Path,
    *,
    skip_label: str,
    drift: Callable[[Path, Path], str],
    adopt_drift: Callable[[str, str], str] | None = None,
    adopt_blocker: Callable[[Context, Path, Path, str, str], str | None] | None = None,
) -> str:
    """Copy ``seed`` to ``dest`` once, or report drift if it's already there.

    These files (Claude Code's settings.json, opencode's opencode.jsonc,
    Pi's settings.json) are copied rather than symlinked because each tool
    rewrites its own copy in place live (permissions approved, settings
    edited, etc.), which would replace a symlink with a plain file and
    silently detach it from the repo. So the repo copy is a *seed*: written
    once, never overwritten, with divergence reported instead.

    Args:
        ctx: The run context.
        seed: Repo-side seed file (already profile-resolved by the caller).
        dest: Live destination path.
        skip_label: Step name used if the copy fails.
        drift: Callback describing divergence when ``dest`` already exists.

    Returns:
        A drift description for the end-of-run summary, or ``""``.
    """
    if ctx.opts.adopt:
        return _adopt_seed(
            ctx,
            seed,
            dest,
            skip_label=skip_label,
            drift=adopt_drift,
            blocker=adopt_blocker,
        )

    if not dest.is_file():
        if ctx.opts.dry_run:
            _preview(
                f"would copy {ctx.display(dest)} (from {seed.name})",
                quiet=ctx.opts.quiet,
            )
            return ""
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(seed, dest)
        except OSError:
            ctx.reporter.skip(skip_label, "copy failed")
            return ""
        ctx.manifest.record_copy(dest)
        cli_common.qprint(
            PALETTE.ok(f"  copied {ctx.display(dest)} (from {seed.name})"),
            quiet=ctx.opts.quiet,
        )
        return ""

    drift_desc = drift(seed, dest)
    if not drift_desc or not ctx.opts.reseed:
        return drift_desc
    return _reseed_file(ctx, seed, dest, skip_label=skip_label, drift_desc=drift_desc)


def _adopt_seed(
    ctx: Context,
    seed: Path,
    dest: Path,
    *,
    skip_label: str,
    drift: Callable[[str, str], str] | None,
    blocker: Callable[[Context, Path, Path, str, str], str | None] | None,
) -> str:
    """Validate one live snapshot, then adopt it into a clean repo seed."""
    if not dest.exists() and not dest.is_symlink():
        return ""
    if seed.is_symlink():
        ctx.reporter.skip(
            skip_label,
            f"repo seed {ctx.display(seed)} is a symlink — resolve manually",
        )
        return "content differs from the repo copy"

    try:
        live_text = dest.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeDecodeError):
        ctx.reporter.skip(
            skip_label,
            f"could not read live file {ctx.display(dest)} — repair it before rerunning",
        )
        return "content differs from the repo copy"
    if not live_text:
        ctx.reporter.skip(
            skip_label,
            f"live file {ctx.display(dest)} is empty — repair it before rerunning",
        )
        return "content differs from the repo copy"

    try:
        seed_text = seed.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        ctx.reporter.skip(
            skip_label,
            f"could not read repo seed {ctx.display(seed)} — repair it before rerunning",
        )
        return "content differs from the repo copy"

    normalized_seed = _normalize_seed_text(seed_text)
    normalized_live = _normalize_seed_text(live_text)
    if normalized_seed == normalized_live:
        return ""

    drift_desc = (
        drift(normalized_seed, normalized_live)
        if drift is not None
        else "content differs from the repo copy"
    )
    reasons: list[str] = []
    if blocker is not None:
        reason = blocker(ctx, seed, dest, normalized_seed, normalized_live)
        if reason:
            reasons.append(reason)
    git_reason = _adopt_git_reason(ctx, seed)
    if git_reason:
        reasons.append(git_reason)
    if reasons:
        ctx.reporter.skip(skip_label, "; ".join(reasons))
        return drift_desc or "content differs from the repo copy"

    if ctx.opts.dry_run:
        _preview(
            f"would adopt {ctx.display(dest)} → {ctx.display(seed)} "
            "(repo seed will become dirty)",
            quiet=ctx.opts.quiet,
        )
        return ""

    if not _adopt_file(ctx, seed, normalized_live, skip_label=skip_label):
        return drift_desc or "content differs from the repo copy"
    cli_common.qprint(
        PALETTE.ok(
            f"  adopted {ctx.display(dest)} → {ctx.display(seed)} "
            "(commit the repo seed before adopting another edit)"
        ),
        quiet=ctx.opts.quiet,
    )
    return ""


def _normalize_seed_text(text: str) -> str:
    """Normalize Windows CRLF text without changing other content."""
    return text.replace("\r\n", "\n")


def _adopt_git_reason(ctx: Context, seed: Path) -> str:
    """Return a refusal reason unless Git proves the seed is tracked and clean."""
    try:
        relative = seed.relative_to(ctx.dotfiles).as_posix()
    except ValueError:
        return "repo seed is outside the Git checkout — repair the path manually"
    prefix = ["git", "-C", str(ctx.dotfiles)]
    tracked = run_command(
        [*prefix, "ls-files", "--error-unmatch", "--", relative], capture=True
    )
    if not tracked.ok or not tracked.stdout.strip():
        return (
            f"repo seed {ctx.display(seed)} is untracked or Git is unavailable "
            "— track it and repair Git access before rerunning"
        )
    status = run_command(
        [*prefix, "status", "--porcelain", "--", relative], capture=True
    )
    if not status.ok:
        return (
            f"Git could not inspect {ctx.display(seed)} — repair Git access "
            "before rerunning"
        )
    if status.stdout:
        return (
            f"repo seed {ctx.display(seed)} is dirty — commit or stash it "
            "before rerunning"
        )
    return ""


def _adopt_file(ctx: Context, seed: Path, live_text: str, *, skip_label: str) -> bool:
    """Atomically write normalized live text while preserving the seed mode."""
    if seed.is_symlink():
        ctx.reporter.skip(
            skip_label,
            f"repo seed {ctx.display(seed)} is a symlink — resolve manually",
        )
        return False
    temp_path: Path | None = None
    try:
        mode = stat.S_IMODE(seed.stat().st_mode)
        fd, temp_name = tempfile.mkstemp(prefix=f".{seed.name}.adopt-", dir=seed.parent)
        temp_path = Path(temp_name)
        os.close(fd)
        temp_path.write_text(
            _normalize_seed_text(live_text), encoding="utf-8", newline=""
        )
        os.chmod(temp_path, mode)
        os.replace(temp_path, seed)
    except OSError:
        ctx.reporter.skip(
            skip_label,
            f"could not atomically write {ctx.display(seed)} — "
            "repair the writable directory or disk before rerunning",
        )
        return False
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass
    return True


def _opencode_adopt_blocker(
    ctx: Context,
    seed: Path,
    dest: Path,
    seed_text: str,
    live_text: str,
) -> str | None:
    """Refuse live opencode parses that fail or introduce an allowlist bypass."""
    try:
        live_data = json.loads(live_text)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return (
            f"live opencode file {ctx.display(dest)} is not a JSON object "
            "(comments, trailing commas, or invalid JSON are unsupported) — "
            "resolve it manually before adopting"
        )
    if not isinstance(live_data, dict):
        return (
            f"live opencode file {ctx.display(dest)} is not a JSON object — "
            "resolve it manually before adopting"
        )

    seed_data: object
    try:
        seed_data = json.loads(seed_text)
    except (json.JSONDecodeError, UnicodeDecodeError):
        seed_data = {}
    if not isinstance(seed_data, dict):
        seed_data = {}

    bypasses = opencode_bypass_drift(seed_data, live_data)
    if not bypasses:
        return None
    return (
        f"SECURITY: {', '.join(bypasses)} present in live {ctx.display(dest)} "
        "(allowlist bypass) — resolve the security change manually before adopting"
    )


def _reseed_file(
    ctx: Context, seed: Path, dest: Path, *, skip_label: str, drift_desc: str
) -> str:
    """Back up and overwrite a drifted copy-once seed with the repo's version.

    Only called once ``seed_file`` has already confirmed real drift and
    ``--reseed`` is set. ``dest``'s true original is preserved exactly
    once, tracked via the manifest (not merely a ``<name>.bak``'s presence
    on disk — see ``Manifest.has_backup``): a foreign, unrecorded ``.bak``
    blocks the reseed entirely rather than risking either file, and a
    recorded backup whose ``.bak`` was since deleted is treated as if no
    backup had ever been taken.
    """
    backup = dest.with_name(dest.name + ".bak")
    has_backup = ctx.manifest.has_backup(dest)
    backup_exists = backup.exists()

    # A .bak this dotfiles tool never recorded — don't touch either file.
    if not has_backup and backup_exists:
        if ctx.opts.dry_run:
            _preview(
                f"would skip reseeding {ctx.display(dest)} — {backup.name} exists "
                "but isn't a recorded backup, resolve manually",
                quiet=ctx.opts.quiet,
            )
            return drift_desc
        ctx.reporter.skip(
            skip_label,
            f"{backup} exists but isn't a recorded backup — resolve manually",
        )
        return drift_desc

    # True original not (or no longer) safely preserved anywhere.
    needs_backup = not has_backup or not backup_exists

    if ctx.opts.dry_run:
        if needs_backup:
            _preview(
                f"would back up {ctx.display(dest)} → {dest.name}.bak, "
                f"then reseed from {seed.name}",
                quiet=ctx.opts.quiet,
            )
        else:
            _preview(
                f"would reseed {ctx.display(dest)} from {seed.name} (already backed up)",
                quiet=ctx.opts.quiet,
            )
        return ""

    if needs_backup:
        try:
            shutil.move(str(dest), str(backup))
        except (OSError, shutil.Error):
            ctx.reporter.skip(skip_label, "reseed backup failed")
            return ""
        ctx.manifest.record_backup(dest, backup)
        cli_common.qprint(f"  Backing up {dest} → {backup}", quiet=ctx.opts.quiet)

    try:
        shutil.copy(seed, dest)
    except OSError:
        ctx.reporter.skip(skip_label, "copy failed")
        if needs_backup:
            try:
                shutil.move(str(backup), str(dest))
            except (OSError, shutil.Error):
                ctx.reporter.skip(
                    skip_label,
                    f"could not restore {dest} from {backup} after failed "
                    f"reseed — {dest.name} is missing; restore manually",
                )
        return ""

    ctx.manifest.record_copy(dest)
    cli_common.qprint(
        PALETTE.ok(f"  reseeded {ctx.display(dest)} (from {seed.name})"),
        quiet=ctx.opts.quiet,
    )
    return ""


def seed_claude_settings(ctx: Context) -> tuple[str, str]:
    """Seed ~/.claude/settings.json, if Claude Code was selected.

    Returns:
        ``(seed filename, drift description)``; both empty when the harness
        wasn't selected.
    """
    if not ctx.has_harness("claude"):
        return "", ""
    name = "settings.work.json" if ctx.opts.profile == "work" else "settings.json"
    seed = ctx.dotfiles / "claude" / name
    dest = ctx.home / ".claude" / "settings.json"
    return name, seed_file(
        ctx,
        seed,
        dest,
        skip_label="settings.json seed",
        drift=describe_settings_drift,
        adopt_drift=_describe_settings_text,
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
    seed = ctx.dotfiles / "pi" / name
    dest = ctx.home / ".pi" / "agent" / "settings.json"
    return name, seed_file(
        ctx,
        seed,
        dest,
        skip_label="pi settings.json seed",
        drift=describe_settings_drift,
        adopt_drift=_describe_settings_text,
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
    seed = ctx.dotfiles / "opencode" / name
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
    )


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


MANAGED_SERVICES = [
    ManagedService(name="watchcommit", unit="watchcommit.service"),
    ManagedService(name="opencode-skills-sync", unit="opencode-skills-sync.service"),
]


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
    return str(ctx.dotfiles / "githooks-global")


def capture_git_hooks_path_baseline(ctx: Context) -> None:
    """Capture the pre-existing global ``core.hooksPath``, immediately
    before :func:`install_global_git_hooks_path` runs -- capturing any later
    would record dotfiles' own already-set value as if it were the original,
    which would make departure "restore" dotfiles' own path instead of the
    true pre-dotfiles value. ``Baseline.add_layer``'s own is-unrecorded rule
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
    not apply to every repo on a work machine). This dotfiles checkout's own
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


def load_watchcommit_agent(ctx: Context) -> None:
    """(Re)load watchcommit's launchd agent (macOS, non-work)."""
    if ctx.opts.profile == "work":
        return
    plist = ctx.home / "Library" / "LaunchAgents" / "com.user.watchcommit.plist"
    if ctx.opts.dry_run:
        _preview("would (re)load watchcommit launchd agent", quiet=ctx.opts.quiet)
        return
    _header("==> Loading watchcommit launchd agent...", quiet=ctx.opts.quiet)
    run_command(["launchctl", "unload", str(plist)], capture=True)
    if not run_command(["launchctl", "load", str(plist)]).ok:
        ctx.reporter.skip("watchcommit agent", "launchctl load failed")


# ── macOS extras ──────────────────────────────────────────────────────────────


def import_rectangle_prefs(ctx: Context) -> None:
    """Import the repo's Rectangle window-manager preferences."""
    plist = ctx.dotfiles / "rectangle" / "com.knollsoft.Rectangle.plist"
    if ctx.opts.dry_run:
        _preview(
            f"would import Rectangle preferences from {plist}", quiet=ctx.opts.quiet
        )
        return
    _header("==> Importing Rectangle preferences...", quiet=ctx.opts.quiet)
    if not run_command(
        ["defaults", "import", "com.knollsoft.Rectangle", str(plist)]
    ).ok:
        ctx.reporter.skip("Rectangle preferences", "defaults import failed")


def set_caps_lock_to_escape(ctx: Context) -> None:
    """Remap Caps Lock to Escape by rewriting the ByHost GlobalPreferences plist.

    macOS stores the keyboard modifier mapping under a per-host preferences
    domain, keyed by a name that varies by OS version — hence the substring
    match on ``modifiermapping`` rather than a fixed key.
    """
    if ctx.opts.dry_run:
        _preview(
            "would set Caps Lock → Escape "
            "(rewrite ~/Library/Preferences/ByHost/.GlobalPreferences.*.plist)",
            quiet=ctx.opts.quiet,
        )
        return

    _header("==> Setting Caps Lock → Escape...", quiet=ctx.opts.quiet)
    byhost = ctx.home / "Library" / "Preferences" / "ByHost"
    plists = sorted(byhost.glob(".GlobalPreferences.*.plist"))
    if not plists:
        cli_common.qprint(
            "  No ByHost GlobalPreferences plist found — skipping", quiet=ctx.opts.quiet
        )
        return

    for path in plists:
        try:
            with open(path, "rb") as handle:
                prefs = plistlib.load(handle)
            for key in list(prefs):
                if "modifiermapping" in key:
                    prefs[key] = CAPS_LOCK_TO_ESCAPE
            with open(path, "wb") as handle:
                plistlib.dump(prefs, handle)
        except (OSError, ValueError, plistlib.InvalidFileException):
            ctx.reporter.skip("Caps Lock → Escape", "plist rewrite failed")
            return
        cli_common.qprint(f"  Updated {path.name}", quiet=ctx.opts.quiet)


# ── editors ───────────────────────────────────────────────────────────────────


def install_vim_plug(ctx: Context) -> None:
    """Download vim-plug into ~/.vim/autoload, if it isn't there already."""
    target = ctx.home / ".vim" / "autoload" / "plug.vim"
    if target.is_file():
        return
    if ctx.opts.dry_run:
        _preview(
            f"would install vim-plug to {ctx.display(target)}", quiet=ctx.opts.quiet
        )
        return
    _header("==> Installing vim-plug...", quiet=ctx.opts.quiet)
    target.parent.mkdir(parents=True, exist_ok=True)
    url = "https://raw.githubusercontent.com/junegunn/vim-plug/master/plug.vim"
    if run_command(["curl", "-fLo", str(target), "--create-dirs", url]).ok:
        ctx.manifest.record_copy(target)
        cli_common.qprint(
            "  Run :PlugInstall inside vim to install plugins", quiet=ctx.opts.quiet
        )
    else:
        ctx.reporter.skip("vim-plug", "download failed (network blocked?)")


def parse_neovim_version(output: str) -> tuple[int, int] | None:
    """Extract ``(major, minor)`` from ``nvim --version`` output.

    Returns:
        The parsed version, or None if the first line isn't recognizable.
    """
    first = output.splitlines()[0] if output.strip() else ""
    if not first.startswith("NVIM v"):
        return None
    parts = first[len("NVIM v") :].split(".")
    try:
        return int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        return None


def neovim_runtime_ok() -> bool:
    """Whether the Neovim binary on PATH can actually resolve its Lua runtime.

    A binary installed without its accompanying share/nvim/runtime tree
    (this repo's own incident: the tree got swept by ``--wipe`` because it
    was nested inside ``~/.local/share/nvim`` — see the warning on
    :func:`_wipe_neovim_dirs`) starts but can't require ``vim.uri``. This is
    the cheapest way to catch that before ``Lazy! sync`` dumps a confusing
    Lua traceback.

    ``--clean`` is load-bearing, not cosmetic: without it this loads the
    vendored config's own init.lua and tries to bootstrap lazy.nvim, which
    can itself error loudly on a broken/old binary (a real smoke-test
    finding — an unclean probe reproduced this repo's original incident's
    own scary traceback instead of a clean pass/fail). ``vim.uri`` is a
    core Lua module bundled in the runtime itself, not a user plugin, so
    ``--clean`` doesn't affect what's actually being tested here.
    """
    return run_command(
        [
            "nvim",
            "--headless",
            "--clean",
            "-c",
            "lua os.exit(pcall(require, 'vim.uri') and 0 or 1)",
        ]
    ).ok


def _neovim_status() -> tuple[tuple[int, int] | None, bool]:
    """The Neovim binary on PATH's parsed ``(major, minor)`` and runtime health.

    ``(None, False)`` when Neovim is missing or its version output doesn't
    parse — there's nothing meaningful to runtime-probe in that case.
    Callers re-run this fresh rather than caching it, since install steps
    in between (a fallback Neovim install) can change what's on PATH.
    """
    if not have("nvim"):
        return None, False
    result = run_command(["nvim", "--version"], capture=True)
    version = parse_neovim_version(result.stdout) if result.ok else None
    if version is None:
        return None, False
    return version, neovim_runtime_ok()


def bootstrap_neovim(ctx: Context) -> None:
    """Sync the vendored Neovim config's plugins with lazy.nvim.

    The vendored config targets Neovim 0.11+; older distro repos still ship
    older builds (same class of version gap as eza/lsd on Ubuntu 22.04), so
    this degrades to a skip rather than the upstream config's own hard
    ``exit 1`` — consistent with this script never aborting a run.
    """
    if not have("nvim"):
        ctx.reporter.skip("Neovim plugin bootstrap", "Neovim not installed")
        return

    version, runtime_ok = _neovim_status()
    if version is None:
        ctx.reporter.skip(
            "Neovim plugin bootstrap", "could not determine Neovim version"
        )
        return

    major, minor = version
    pretty = f"{major}.{minor}"
    if major == 0 and minor < 11:
        reason = f"Neovim {pretty} found, config needs >=0.11"
        if ctx.neovim_fallback_failure:
            reason += f" (fallback install also failed: {ctx.neovim_fallback_failure})"
        ctx.reporter.skip("Neovim plugin bootstrap", reason)
        return
    if not runtime_ok:
        ctx.reporter.skip(
            "Neovim plugin bootstrap",
            f"Neovim {pretty} found but its runtime doesn't resolve "
            "(vim.uri unavailable) — broken or incomplete install",
        )
        return
    if ctx.opts.dry_run:
        _preview(
            f'would run: nvim --headless "+Lazy! sync" +qa (Neovim {pretty})',
            quiet=ctx.opts.quiet,
        )
        return

    _header(
        f"==> Bootstrapping Neovim plugins (lazy.nvim sync, Neovim {pretty})...",
        quiet=ctx.opts.quiet,
    )
    if run_command(["nvim", "--headless", "+Lazy! sync", "+qa"]).ok:
        cli_common.qprint(PALETTE.ok("  plugins synced"), quiet=ctx.opts.quiet)
    else:
        ctx.reporter.skip(
            "Neovim plugin sync",
            "'Lazy! sync' reported errors — run :Lazy sync manually inside nvim",
        )


# ── departure baseline capture ──────────────────────────────────────────────


def _is_state_dir_or_its_ancestor(path: Path, ctx: Context) -> bool:
    """Whether ``path`` is the state directory itself, or one of its own ancestors.

    The state directory holds this feature's own ``baseline.json``/
    ``history.jsonl``/``departure.jsonl``/lock — its removal (and any of
    its ancestors that become empty as a result) is entirely
    ``_finalize_departure_state``'s job, run *after* the generic directory
    phase. Letting the generic phase track and act on these paths would
    make it try to rmdir them while they (or the state directory nested
    inside them) still hold files that haven't been cleared yet — an
    unbreakable "not empty" that would permanently block a clean departure.
    """
    return path == ctx.state_dir or path in ctx.state_dir.parents


def _departure_owned_destinations(
    ctx: Context, specs: Sequence[LinkSpec]
) -> list[Path]:
    """Every links.toml/seed destination this run's options make applicable.

    These are the only categories whose ``file:``/``symlink:`` keys can ever
    need content restored (rc files are handled separately) — see
    :func:`capture_departure_baseline`'s blob-writing rule.
    """
    destinations: list[Path] = []
    for spec in specs:
        if link_applies(spec, ctx):
            destinations.append(expand_dest(spec.dest, ctx.home))
    if ctx.has_harness("claude"):
        destinations.append(ctx.home / ".claude" / "settings.json")
    if ctx.has_harness("opencode"):
        destinations.append(ctx.home / ".config" / "opencode" / "opencode.jsonc")
    if ctx.has_harness("pi"):
        destinations.append(ctx.home / ".pi" / "agent" / "settings.json")
    return destinations


def capture_departure_baseline(ctx: Context, specs: Sequence[LinkSpec]) -> None:
    """Capture this run's departure baseline layer before any install step runs.

    Linux/WSL and Fedora only (Implementation Sequence step 6 — this feature
    does not apply on macOS) and a no-op under ``--dry-run`` (step 1: a
    dry-run install writes no ``baseline.json`` and creates no immutable
    first layer). Must run before ``install_linux_packages`` — ``_install_uv``
    and the oh-my-posh installer both run inside it, earlier than
    ``install_node``/NVM, and can mutate rc files themselves.
    """
    if not ctx.is_linux or ctx.opts.dry_run:
        return

    state_dir = ctx.state_dir
    baseline = depart.load_baseline(state_dir) or depart.Baseline()
    records: dict[str, dict[str, object]] = {}
    seen_dirs: set[Path] = set()

    def _track_ancestors(path: Path) -> None:
        for ancestor in depart.ancestor_directories(path, ctx.home):
            if ancestor in seen_dirs or _is_state_dir_or_its_ancestor(ancestor, ctx):
                continue
            seen_dirs.add(ancestor)
            records[depart.directory_key(ancestor)] = depart.capture_directory(ancestor)

    for rc_name in depart.RC_FILENAMES:
        rc_path = ctx.home / rc_name
        records[depart.file_key(rc_path)] = depart.capture_file(
            rc_path, blob_dir=state_dir
        )

    for dest in _departure_owned_destinations(ctx, specs):
        records[depart.file_key(dest)] = depart.capture_file(dest, blob_dir=state_dir)
        records[depart.symlink_key(dest)] = depart.capture_symlink(dest)
        bak = dest.with_name(dest.name + ".bak")
        records[depart.file_key(bak)] = depart.capture_file(bak)
        _track_ancestors(dest)

    for path in (
        ctx.home / ".local" / "bin" / "uv",
        ctx.home / ".local" / "bin" / "oh-my-posh",
        ctx.home / ".vim" / "autoload" / "plug.vim",
    ):
        records[depart.file_key(path)] = depart.capture_file(path)
        _track_ancestors(path)

    vscode_user_dir = _vscode_wsl_user_dir()
    if vscode_user_dir is not None:
        for name in ("settings.json", "keybindings.json"):
            path = vscode_user_dir / name
            bak = path.with_name(path.name + ".bak")
            for guarded_path in (path, bak):
                record = depart.capture_file(guarded_path)
                record["needs_vscode_guard"] = True
                records[depart.file_key(guarded_path)] = record

    records[depart.file_key(ctx.profile_marker)] = depart.capture_file(
        ctx.profile_marker
    )
    _track_ancestors(ctx.profile_marker)

    for path in (
        ctx.home / ".local" / "bin" / "bat",
        ctx.home / ".local" / "bin" / "fd",
    ):
        records[depart.symlink_key(path)] = depart.capture_symlink(path)
        _track_ancestors(path)

    font_dir = ctx.home / ".local" / "share" / "fonts" / "JetBrainsMonoNerdFont"
    records[depart.directory_key(font_dir)] = depart.capture_tree_manifest(font_dir)
    _track_ancestors(font_dir)

    neovim_prefix = ctx.home / ".local" / "opt" / "neovim"
    records[depart.directory_key(neovim_prefix)] = depart.capture_tree_manifest(
        neovim_prefix
    )
    _track_ancestors(neovim_prefix)

    for parts in depart.SHARED_NEOVIM_DIRS:
        shared_dir = ctx.home.joinpath(*parts)
        records[depart.directory_key(shared_dir)] = depart.capture_directory(shared_dir)

    records[depart.runtime_key(ctx.home / ".nvm")] = depart.capture_runtime_nvm(
        ctx.home
    )

    stamp = datetime.now().astimezone().isoformat(timespec="seconds")
    baseline.add_layer(stamp, records)
    depart.save_baseline(state_dir, baseline)
    ctx.departure_baseline = baseline


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


def _departure_state_paths(state_dir: Path) -> list[Path]:
    """This feature's own state files, if present — never anything else.

    Snapshot naming is pinned (``baseline.json`` plus
    ``baseline-snapshot-<sha256>.blob``, flat in the state directory), so a
    glob is always exactly correct here — regardless of whether
    ``baseline.json`` itself is missing, empty, or unparseable at rollback
    time. Never includes ``history.jsonl``, the profile marker, or anything
    else this feature doesn't own.
    """
    if not state_dir.is_dir():
        return []
    paths = [depart.baseline_path(state_dir)]
    paths.extend(sorted(state_dir.glob("baseline-snapshot-*.blob")))
    paths.append(state_dir / "departure.lock")
    paths.append(state_dir / "departure.jsonl")
    return paths


def _delete_departure_state(ctx: Context) -> None:
    """Delete this feature's own state files during a real (non-dry-run) rollback."""
    for path in _departure_state_paths(ctx.state_dir):
        path.unlink(missing_ok=True)


def do_rollback(ctx: Context) -> int:
    """Reverse every file mutation recorded across every past run.

    Walks the history newest-to-oldest so a path mutated by several runs
    ends up back at its oldest recorded state (the original pre-dotfiles
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
    couldn't resolve `require('vim.uri')`. See _install_neovim_fallback,
    which installs into ~/.local/opt/neovim instead — outside this sweep's
    reach.

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
    unlinking here would delete that restored original rather than a
    dotfiles-managed copy — see ``do_rollback``'s comment on
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
    pre-dotfiles file is discarded, not brought back.
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
    if ctx.opts.profile == "work":
        print("  - ~/.secrets is sourced if present — for work-issued tokens only;")
        print("    do NOT put a personal ANTHROPIC_API_KEY on this machine")
    elif ctx.has_harness("claude"):
        print(
            "  - Run 'claude login' if you haven't, so watchcommit can "
            "generate commit messages"
        )
    if ctx.is_linux:
        print("  - Restart your shell to pick up the new config")


# ── departure preflight and CLI ─────────────────────────────────────────────


def _tree_manifest_directories(ctx: Context) -> set[Path]:
    """Directories tracked as full tree manifests, not plain directory: keys."""
    return {
        ctx.home / ".local" / "share" / "fonts" / "JetBrainsMonoNerdFont",
        ctx.home / ".local" / "opt" / "neovim",
    }


def _recapture_live_value(ctx: Context, key: str) -> dict[str, object]:
    """Fresh, read-only live value for one *already-recorded* ownership key.

    Dispatches purely on the key itself — deliberately never re-derives
    "is this destination applicable" from links.toml + the current
    invocation's ``--harness`` selection. ``--depart`` is standalone
    (``parse_args`` rejects ``--harness`` alongside it), so at departure
    time ``ctx.opts.harnesses`` is always empty; re-deriving applicability
    from it would make every harness-gated links.toml entry (``~/.claude/
    CLAUDE.md``, its commands, the copy-once seed files, ...) silently
    invisible to preflight — the real bug this replaced (caught via a real
    container run, not the fast unit-test suite, since every fast test
    happened to capture and recapture with the same harness selection).
    The baseline itself is the only source of truth for what was ever
    installer-tracked; this function only ever answers "what's live at
    this exact key's path right now."
    """
    type_, path_str = key.split(":", 1)
    path = Path(path_str)
    if type_ == "file":
        return depart.capture_file(path)
    if type_ == "symlink":
        return depart.capture_symlink(path)
    if type_ == "directory":
        if path in _tree_manifest_directories(ctx):
            return depart.capture_tree_manifest(path)
        return depart.capture_directory(path)
    if type_ == "runtime":
        return depart.capture_runtime_nvm(path.parent)
    return {"state": depart.STATE_UNKNOWN}


def _recapture_departure_live_state(
    ctx: Context, baseline: depart.Baseline
) -> dict[str, dict[str, object]]:
    """Re-capture every tracked ownership key's *current* value, read-only.

    Driven entirely by ``baseline.all_keys()`` — see
    :func:`_recapture_live_value`'s docstring for why that's load-bearing,
    not incidental. Never writes a blob or persists anything; this only
    builds the "live" half of a preflight comparison.
    """
    return {
        key: _recapture_live_value(ctx, key)
        for key in baseline.all_keys()
        if depart.key_type(key) not in ("service", "gitconfig")
    }


def _apply_rc_file_reclassification(
    ctx: Context, baseline: depart.Baseline, report: dict[str, depart.Classification]
) -> None:
    """Override the generic result for each rc file with the append-aware rule."""
    for rc_name in depart.RC_FILENAMES:
        rc_path = ctx.home / rc_name
        key = depart.file_key(rc_path)
        recorded = baseline.value_for(key)
        if recorded is None or recorded.get("state") != depart.STATE_PRESENT:
            continue
        blob_digest = recorded.get("blob")
        baseline_content = (
            depart.read_blob(ctx.state_dir, str(blob_digest))
            if isinstance(blob_digest, str)
            else None
        )
        try:
            live_content: bytes | None = rc_path.read_bytes()
        except OSError:
            live_content = None
        override = depart.reclassify_rc_file(recorded, baseline_content, live_content)
        if override is not None:
            report[key] = override


def _apply_symlink_pair_reclassification(
    baseline: depart.Baseline,
    live: dict[str, dict[str, object]],
    report: dict[str, depart.Classification],
) -> None:
    """Override the generic per-key results for each backed-up-then-symlinked pair.

    Candidate paths come from the report's own keys (i.e. the baseline),
    never re-derived from links.toml — same reasoning as
    :func:`_recapture_live_value`.
    """
    file_paths = {
        Path(k.split(":", 1)[1]) for k in report if depart.key_type(k) == "file"
    }
    symlink_paths = {
        Path(k.split(":", 1)[1]) for k in report if depart.key_type(k) == "symlink"
    }
    for path in file_paths & symlink_paths:
        file_key = depart.file_key(path)
        symlink_key = depart.symlink_key(path)
        override = depart.reclassify_symlink_destination_pair(
            baseline.value_for(file_key),
            live.get(file_key, {"state": depart.STATE_UNKNOWN}),
            baseline.value_for(symlink_key),
            live.get(symlink_key, {"state": depart.STATE_UNKNOWN}),
        )
        if override is not None:
            report[file_key], report[symlink_key] = override


def build_preflight_report(ctx: Context) -> dict[str, depart.Classification] | None:
    """Classify every tracked ownership key, or None if there's no baseline."""
    baseline = depart.load_baseline(ctx.state_dir)
    if baseline is None:
        return None
    live = _recapture_departure_live_state(ctx, baseline)
    report: dict[str, depart.Classification] = {}
    for key in sorted(baseline.all_keys()):
        # service:/gitconfig: keys use their own dedicated classifier (their
        # record shapes don't fit the tri-state present/absent model the
        # generic classifier expects — gitconfig's "owned" case in
        # particular needs to compare live against a *managed value*, which
        # classify_ownership_key has no parameter for).
        if depart.key_type(key) in ("service", "gitconfig"):
            continue
        recorded = baseline.value_for(key)
        live_value = live.get(key, {"state": depart.STATE_UNKNOWN})
        report[key] = depart.classify_ownership_key(key, recorded, live_value)

    _apply_rc_file_reclassification(ctx, baseline, report)
    _apply_symlink_pair_reclassification(baseline, live, report)

    for service in MANAGED_SERVICES:
        service_key = depart.service_key("systemd", service.name)
        if service_key in baseline.all_keys():
            report[service_key] = depart.classify_service(
                baseline.value_for(service_key), _capture_live_service(ctx, service)
            )

    hooks_key = depart.gitconfig_key(GLOBAL_GIT_HOOKS_PATH_KEY)
    if hooks_key in baseline.all_keys():
        report[hooks_key] = depart.classify_gitconfig(
            baseline.value_for(hooks_key),
            depart.build_gitconfig_record(_global_git_hooks_path()),
            _managed_git_hooks_path(ctx),
        )
    return report


def build_package_preflight(ctx: Context) -> list[depart.PackageClassification] | None:
    """Classify every requested/introduced package, or None if there's no baseline."""
    baseline = depart.load_baseline(ctx.state_dir)
    if baseline is None:
        return None
    return depart.classify_package_transactions(
        baseline, live_package_snapshots(baseline)
    )


def _vscode_guard_preflight_annotations(
    ctx: Context, report: dict[str, depart.Classification]
) -> dict[str, str]:
    """Display-only: flag guarded VS Code keys headed for removal when
    Windows VS Code is running or its status couldn't be verified.

    Purely advisory — never touches the stored ``Classification`` objects
    in ``report`` and never gates anything. The real gate is
    ``execute_file_symlink_phase``'s own, independent, execution-time check
    (see its call site for the TOCTOU rationale): this probe only keeps
    ``--depart --dry-run`` from printing a removal it already knows won't
    happen, it does not replace that later check.
    """
    baseline = depart.load_baseline(ctx.state_dir)
    if baseline is None:
        return {}
    guarded_removals = [
        key
        for key, c in report.items()
        if c.bucket == depart.BUCKET_OWNED
        and c.action == depart.ACTION_REMOVE
        and (baseline.value_for(key) or {}).get("needs_vscode_guard")
    ]
    if not guarded_removals:
        return {}
    from settings_seed_drift_check import _vscode_process_running

    if _vscode_process_running() is False:
        return {}
    note = (
        "Windows VS Code is running (or could not be verified) — this will "
        "land as unresolved, not removed"
    )
    return dict.fromkeys(guarded_removals, note)


def _print_preflight_report(
    report: dict[str, depart.Classification],
    package_report: Sequence[depart.PackageClassification] = (),
    quiet: bool = False,
    guard_annotations: dict[str, str] | None = None,
) -> None:
    """Print the full departure preflight, grouped by bucket."""
    guard_annotations = guard_annotations or {}
    _header("==> Departure preflight", quiet=quiet)
    for bucket in (
        depart.BUCKET_OWNED,
        depart.BUCKET_DRIFTED,
        depart.BUCKET_UNRESOLVED,
        depart.BUCKET_PRESERVED,
    ):
        keys = sorted(k for k, c in report.items() if c.bucket == bucket)
        package_lines = [c for c in package_report if c.bucket == bucket]
        if not keys and not package_lines:
            continue
        print(PALETTE.header(f"  {bucket} ({len(keys) + len(package_lines)}):"))
        warn = bucket in (depart.BUCKET_UNRESOLVED, depart.BUCKET_DRIFTED)
        for key in keys:
            c = report[key]
            action = f" [{c.action}]" if c.action else ""
            line = f"    {key}{action} — {c.reason}"
            line_warn = warn
            if key in guard_annotations:
                line += f" ({guard_annotations[key]})"
                line_warn = True
            print(PALETTE.warn(line) if line_warn else line)
        for pc in sorted(package_lines, key=lambda c: c.key):
            action = f" [{pc.action}]" if pc.action else ""
            line = f"    {pc.key}{action} — {pc.reason}"
            print(PALETTE.warn(line) if warn else line)


def _read_confirmation_token() -> str:
    """Read one line from stdin, stripping exactly one trailing LF/CRLF.

    Surrounding spaces/tabs are deliberately left in place — the caller
    compares for an exact ``"DEPART"`` match, so ``" DEPART"`` or an EOF
    (empty string) both correctly fail to match.
    """
    line = sys.stdin.readline()
    if line.endswith("\r\n"):
        return line[:-2]
    if line.endswith("\n"):
        return line[:-1]
    return line


def _restore_target_still_occupied(dest: Path) -> bool:
    return dest.exists() or dest.is_symlink()


def _execute_restore(
    ctx: Context, dest: Path, recorded: dict[str, object], *, expect_absent: bool
) -> str:
    """Restore ``dest``'s content from its recorded blob.

    ``expect_absent`` is set only for the backed-up-then-symlinked pair
    case, where the paired symlink was just removed in the prior phase —
    if ``dest`` is unexpectedly occupied afterward, something else has
    claimed the path and the restore aborts rather than overwriting it.
    For a plain in-place restore (an appended-to rc file), ``dest`` is
    expected to already exist and gets overwritten with the recorded
    content directly.
    """
    digest = recorded.get("blob")
    if not isinstance(digest, str):
        return "unresolved: no blob recorded for restore"
    content = depart.read_blob(ctx.state_dir, digest)
    if content is None:
        return "unresolved: recorded blob is missing or unreadable"
    if expect_absent and _restore_target_still_occupied(dest):
        return "unresolved: destination still occupied after symlink removal"
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(content)
    except OSError as exc:
        return f"unresolved: restore failed ({exc})"
    return "ok"


def _execute_remove_symlink(path: Path) -> str:
    if not path.is_symlink():
        return "ok: already absent"
    try:
        path.unlink()
    except OSError as exc:
        return f"unresolved: could not remove symlink ({exc})"
    return "ok"


def _execute_remove_file(path: Path) -> str:
    if not path.is_file() or path.is_symlink():
        return "ok: already absent"
    try:
        path.unlink()
    except OSError as exc:
        return f"unresolved: could not remove file ({exc})"
    return "ok"


def _maybe_consume_bak(dest: Path, baseline: depart.Baseline) -> None:
    """Delete ``dest``'s ``.bak`` once a clean restore succeeds, if it's
    provably departure-owned — never touch a ``.bak`` this feature can't
    prove it created.

    Only ever called after :func:`_execute_restore` has already returned
    ``"ok"`` — its authoritative source is the content blob, so a
    qualifying ``.bak`` is now redundant leftover, not a second restore
    source. See depart.reclassify_symlink_destination_pair's docstring and
    Implementation Sequence step 4's ``.bak`` provenance rule.
    """
    bak = dest.with_name(dest.name + ".bak")
    file_recorded = baseline.value_for(depart.file_key(dest))
    bak_recorded = baseline.value_for(depart.file_key(bak))
    if not (
        file_recorded is not None
        and file_recorded.get("state") == depart.STATE_PRESENT
        and bak_recorded is not None
        and bak_recorded.get("state") == depart.STATE_ABSENT
    ):
        return
    try:
        if bak.is_file() and not bak.is_symlink():
            bak.unlink()
    except OSError:
        pass  # best-effort cleanup — never fails the restore itself


def _other_enabled_user_units(exclude: frozenset[str]) -> list[str] | None:
    """Every enabled systemd ``--user`` unit other than those in ``exclude``.

    None if the listing probe itself failed/is unavailable — callers must
    treat that as "can't prove it's safe," never as an empty list.
    """
    if not have("systemctl"):
        return None
    result = run_command(
        ["systemctl", "--user", "list-unit-files", "--state=enabled", "--no-legend"],
        capture=True,
    )
    if not result.ok:
        return None
    units = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if parts and parts[0] not in exclude:
            units.append(parts[0])
    return units


def _execute_service_disable(service: ManagedService) -> str:
    """Disable+stop one managed service's systemd --user unit.

    Unit-level only — linger is a separate, machine-wide concern handled by
    :func:`_reconcile_linger` outside the departure ledger (see
    install-multi-service-depart-adopt-spec.md's Design decision: bundling
    linger into a single service's outcome breaks once more than one
    service can be departed in the same run).
    """
    if not have("systemctl"):
        return "unresolved: systemd --user unavailable"
    if not run_command(["systemctl", "--user", "disable", "--now", service.unit]).ok:
        return "unresolved: systemctl --user disable --now failed"
    return "ok"


def _reconcile_linger(ctx: Context, baseline: depart.Baseline) -> None:
    """Best-effort, not ledger-tracked: restore linger once nothing managed
    still needs it.

    Runs unconditionally at the end of every :func:`execute_service_phase`
    call. Eligibility is recomputed from live state every call (not from
    "did this run's loop just disable something"), so a failed attempt
    self-heals on a later ``--depart`` invocation with no persisted flag
    needed. Never raises, never returns an outcome, never affects
    ``do_depart``'s exit code — a failure here is advisory only, matching
    the existing install-time ``loginctl enable-linger`` failure precedent
    in :func:`_enable_service`.
    """
    if not ctx.is_linux:
        return
    eligible_off = frozenset(
        service.unit
        for service in MANAGED_SERVICES
        if (recorded := baseline.value_for(depart.service_key("systemd", service.name)))
        is not None
        and recorded.get("linger") is False
        and _capture_live_service(ctx, service).get("enabled") is False
    )
    if not eligible_off:
        return

    others = _other_enabled_user_units(exclude=eligible_off)
    if others is None:
        cli_common.qprint(
            "  note: could not check for other enabled systemd --user units — "
            "linger left as-is",
            quiet=ctx.opts.quiet,
        )
        return
    if others:
        cli_common.qprint(
            "  note: linger left enabled — other systemd --user units depend "
            f"on it ({', '.join(others)})",
            quiet=ctx.opts.quiet,
        )
        return
    if not run_command(
        ["loginctl", "disable-linger", _current_user()], capture=True
    ).ok:
        cli_common.qprint(
            "  note: loginctl disable-linger failed", quiet=ctx.opts.quiet
        )


def execute_service_phase(
    ctx: Context, baseline: depart.Baseline, ledger: depart.DepartureLedger
) -> None:
    """Disable+stop every owned managed service, then reconcile linger once."""
    for service in MANAGED_SERVICES:
        key = depart.service_key("systemd", service.name)
        if key in ledger.completed_keys():
            continue
        recorded = baseline.value_for(key)
        if recorded is None:
            continue  # never captured (e.g. a work-profile install) — nothing to check
        c = depart.classify_service(recorded, _capture_live_service(ctx, service))
        if c.bucket != depart.BUCKET_OWNED:
            continue
        ledger.record(
            key, c.action or depart.ACTION_DISABLE, _execute_service_disable(service)
        )

    _reconcile_linger(ctx, baseline)


def _execute_gitconfig_restore(recorded: dict[str, object]) -> str:
    """Restore the global core.hooksPath value baseline recorded: unset if
    it was absent before dotfiles set it, otherwise set it back."""
    if recorded.get("state") == depart.STATE_ABSENT:
        ok = run_command(
            ["git", "config", "--global", "--unset", GLOBAL_GIT_HOOKS_PATH_KEY]
        ).ok
    else:
        value = recorded.get("value")
        ok = (
            isinstance(value, str)
            and run_command(
                ["git", "config", "--global", GLOBAL_GIT_HOOKS_PATH_KEY, value]
            ).ok
        )
    return "ok" if ok else "unresolved: git config --global restore failed"


def execute_gitconfig_phase(
    ctx: Context, baseline: depart.Baseline, ledger: depart.DepartureLedger
) -> None:
    """Restore the pre-dotfiles global core.hooksPath value, if this
    installer owns the current value."""
    key = depart.gitconfig_key(GLOBAL_GIT_HOOKS_PATH_KEY)
    if key in ledger.completed_keys():
        return
    recorded = baseline.value_for(key)
    if recorded is None:
        return  # never captured (e.g. a work-profile install) — nothing to check
    live = depart.build_gitconfig_record(_global_git_hooks_path())
    c = depart.classify_gitconfig(recorded, live, _managed_git_hooks_path(ctx))
    if c.bucket != depart.BUCKET_OWNED:
        return
    ledger.record(
        key, c.action or depart.ACTION_RESTORE, _execute_gitconfig_restore(recorded)
    )


_VSCODE_GUARD_UNRESOLVED_PREFIX = "unresolved [vscode-guard-blocked]:"


def execute_file_symlink_phase(
    ctx: Context,
    baseline: depart.Baseline,
    report: dict[str, depart.Classification],
    ledger: depart.DepartureLedger,
) -> None:
    """Execute every owned ``file:``/``symlink:`` action, in pinned order.

    Symlink removals run before same-path file restores — the identical
    problem ``do_rollback``'s ``restored_dests`` ordering already solves —
    so a paired restore never finds its own soon-to-be-removed symlink
    still occupying the path.

    A key whose ledger history shows it was blocked by the VS Code guard
    at least once stays retryable across ``done``'s otherwise-permanent
    exclusion — without this, a guard-blocked key would never be retried
    even after VS Code closes, contradicting the guard's own "close it
    first, then re-run --depart" message. Every other outcome (including a
    non-guard failure on the same guarded key) keeps the ledger's normal
    never-re-attempted contract.
    """
    done = ledger.completed_keys()
    guard_retryable = ledger.keys_with_outcome_prefix(_VSCODE_GUARD_UNRESOLVED_PREFIX)
    owned = {
        key: c
        for key, c in report.items()
        if c.bucket == depart.BUCKET_OWNED
        and depart.key_type(key) in ("file", "symlink")
        and (key not in done or key in guard_retryable)
    }

    for key in sorted(owned):
        c = owned[key]
        if depart.key_type(key) != "symlink" or c.action != depart.ACTION_REMOVE:
            continue
        path = Path(key.partition(":")[2])
        ledger.record(key, c.action, _execute_remove_symlink(path))

    vscode_running: bool | None = None
    vscode_checked = False
    for key in sorted(owned):
        c = owned[key]
        if depart.key_type(key) != "file":
            continue
        path = Path(key.partition(":")[2])
        if c.action == depart.ACTION_REMOVE:
            recorded = baseline.value_for(key) or {}
            if recorded.get("needs_vscode_guard"):
                if not vscode_checked:
                    from settings_seed_drift_check import _vscode_process_running

                    vscode_running = _vscode_process_running()
                    vscode_checked = True
                if vscode_running is not False:
                    ledger.record(
                        key,
                        c.action,
                        f"{_VSCODE_GUARD_UNRESOLVED_PREFIX} Windows VS Code "
                        "is running (or could not be verified) — close it "
                        "first, then re-run --depart",
                    )
                    continue
            ledger.record(key, c.action, _execute_remove_file(path))
            continue
        recorded = baseline.value_for(key) or {}
        paired_symlink = report.get(depart.symlink_key(path))
        expect_absent = (
            paired_symlink is not None
            and paired_symlink.bucket == depart.BUCKET_OWNED
            and paired_symlink.action == depart.ACTION_REMOVE
        )
        outcome = _execute_restore(ctx, path, recorded, expect_absent=expect_absent)
        if outcome == "ok":
            _maybe_consume_bak(path, baseline)
        ledger.record(key, c.action or "restore", outcome)


def _wholesale_removal_directories(ctx: Context) -> set[Path]:
    """Directories removed wholesale (bypassing the empty-only rule) when owned.

    The Neovim fallback prefix and Nerd Font directory (tree-manifest
    artifacts) plus the three shared Neovim state/cache dirs — matching
    Implementation Sequence step 4's named exceptions to the generic
    empty-only ``directory:`` removal rule.
    """
    wholesale = {
        ctx.home / ".local" / "share" / "fonts" / "JetBrainsMonoNerdFont",
        ctx.home / ".local" / "opt" / "neovim",
    }
    wholesale.update(ctx.home.joinpath(*parts) for parts in depart.SHARED_NEOVIM_DIRS)
    return wholesale


def _execute_remove_directory(path: Path, *, wholesale: bool) -> str:
    try:
        if path.is_symlink() or not path.is_dir():
            return "ok: already absent"
        if wholesale:
            shutil.rmtree(path)
            return "ok"
        if any(path.iterdir()):
            return "unresolved: directory not empty"
        path.rmdir()
    except OSError as exc:
        return f"unresolved: {exc}"
    return "ok"


def _remove_tree_manifest_directory(baseline: depart.Baseline, path: Path) -> str:
    """Remove a wholly installer-owned tree, but only if it is untouched.

    The Nerd Font directory and the pinned Neovim prefix are the two trees
    departure deletes outright rather than emptying, so they are the two
    where a wholesale ``rmtree`` could destroy something the user added
    after installing. Gated on the post-install manifest: an exact match is
    the only proof that everything inside is the installer's own.
    """
    try:
        if path.is_symlink() or not path.is_dir():
            return "ok: already absent"
    except OSError as exc:
        return f"unresolved: {exc}"

    try:
        verdict = depart.remove_manifest_tree(baseline, path)
    except OSError as exc:
        return f"unresolved: {exc}"
    if verdict == depart.TREE_MODIFIED:
        return (
            "unresolved: tree changed since install — something was added or "
            "edited inside it, so it was left in place rather than removed "
            "wholesale; remove it by hand if you are sure"
        )
    if verdict == depart.TREE_UNRECORDED:
        return (
            "unresolved: no post-install manifest recorded for this tree, so "
            "it cannot be proven unmodified — left in place; remove it by "
            "hand, or re-run install.sh to record one"
        )
    return "ok"


def execute_directory_phase(
    ctx: Context,
    baseline: depart.Baseline,
    report: dict[str, depart.Classification],
    ledger: depart.DepartureLedger,
) -> None:
    """Execute every owned ``directory:`` action, deepest-path-first.

    Deepest-first so a parent directory is only empty-checked after its own
    contents have already been processed this same run.
    """
    done = ledger.completed_keys()
    wholesale_dirs = _wholesale_removal_directories(ctx)
    manifest_dirs = _tree_manifest_directories(ctx)
    owned_dirs = [
        key
        for key, c in report.items()
        if c.bucket == depart.BUCKET_OWNED
        and depart.key_type(key) == "directory"
        and c.action == depart.ACTION_REMOVE
        and key not in done
    ]

    def _depth(key: str) -> int:
        return len(Path(key.partition(":")[2]).parts)

    for key in sorted(owned_dirs, key=_depth, reverse=True):
        path = Path(key.partition(":")[2])
        if path in manifest_dirs:
            outcome = _remove_tree_manifest_directory(baseline, path)
        else:
            outcome = _execute_remove_directory(path, wholesale=path in wholesale_dirs)
        ledger.record(key, depart.ACTION_REMOVE, outcome)


def execute_runtime_phase(
    ctx: Context,
    report: dict[str, depart.Classification],
    ledger: depart.DepartureLedger,
) -> None:
    """Remove the NVM root wholesale, if owned and not already done."""
    key = depart.runtime_key(ctx.home / ".nvm")
    if key in ledger.completed_keys():
        return
    c = report.get(key)
    if c is None or c.bucket != depart.BUCKET_OWNED or c.action != depart.ACTION_REMOVE:
        return
    outcome = _execute_remove_directory(ctx.home / ".nvm", wholesale=True)
    ledger.record(key, depart.ACTION_REMOVE, outcome)


_REMOVAL_COMMANDS: dict[str, Callable[[str], list[str]]] = {
    "apt": depart.apt_remove_command,
    "dnf": depart.dnf_remove_command,
    "npm": depart.npm_uninstall_command,
    "uv-tool": depart.uv_tool_uninstall_command,
}
_RDEPENDS_COMMANDS: dict[str, Callable[[str], list[str]]] = {
    "apt": depart.apt_rdepends_command,
    "dnf": depart.dnf_whatrequires_command,
}
_DOWNGRADE_COMMANDS: dict[str, Callable[[str, str], list[str]]] = {
    "apt": depart.apt_downgrade_command,
    "dnf": depart.dnf_downgrade_command,
}


def live_package_snapshots(
    baseline: depart.Baseline,
) -> dict[str, dict[str, str] | None]:
    """Fresh probe results for every manager appearing in recorded transactions."""
    managers = {t.get("manager") for t in baseline.transactions if t.get("manager")}
    return {str(m): _capture_package_snapshot(str(m)) for m in managers}


def _execute_package_removal(manager: str, name: str) -> str:
    builder = _REMOVAL_COMMANDS.get(manager)
    if builder is None:
        return f"unresolved: no removal command for manager {manager!r}"
    if run_command(builder(name)).ok:
        return "ok"
    return f"unresolved: {manager} removal failed for {name}"


def _execute_dependency_removal(manager: str, name: str) -> str:
    """Remove an introduced dependency, gated on an explicitly-empty rdepends probe.

    Never a broad autoremove — only ever this one named package, and only
    once its own probe proves nothing else installed still depends on it.
    """
    probe_builder = _RDEPENDS_COMMANDS.get(manager)
    if probe_builder is None:
        return f"unresolved: no reverse-dependency probe for manager {manager!r}"
    result = run_command(probe_builder(name), capture=True)
    verdict = depart.classify_rdepends_result(result.ok, result.stdout)
    if verdict != "removable":
        return f"unresolved: reverse-dependency probe {verdict}"
    return _execute_package_removal(manager, name)


def _execute_downgrade(baseline: depart.Baseline, manager: str, name: str) -> str:
    """Try each downgrade candidate in order (earliest first, per the ladder).

    Returns ``"halt: ..."`` only for the one named exception to this
    installer's general no-abort convention: a downgrade command that ran
    and left the package manager's own reported state different from both
    the pre-attempt and target versions — state left genuinely uncertain
    mid-operation, per the plan's "changed-state-then-failed" rule.
    """
    candidates = depart.downgrade_candidates(baseline, manager, name)
    builder = _DOWNGRADE_COMMANDS.get(manager)
    if not candidates or builder is None:
        return "unresolved: no recorded downgrade target for this package"
    pre = _capture_package_snapshot(manager)
    for version in candidates:
        if run_command(builder(name, version)).ok:
            return "ok"
        post = _capture_package_snapshot(manager)
        if (
            pre is not None
            and post is not None
            and post.get(name) != pre.get(name)
            and post.get(name) != version
        ):
            return "halt: changed-state-then-failed downgrade"
        pre = post
    return "unresolved: downgrade ladder exhausted, no safe version installed"


def execute_package_phase(
    ctx: Context, baseline: depart.Baseline, ledger: depart.DepartureLedger
) -> bool:
    """Remove/downgrade owned packages, reverse transactions order.

    Returns False only when a changed-state-then-failed downgrade halted
    the phase — callers must skip the subsequent runtime/shared-state
    phase too when this happens, per the plan's explicit exception to the
    general no-abort/report-skips convention.
    """
    done = ledger.completed_keys()
    pending_managers = {
        depart.transaction_from_dict(t).manager
        for t in baseline.transactions
        if any(
            depart.package_key(t.get("manager", ""), name) not in done
            for name in (
                *depart.transaction_from_dict(t).requested,
                *depart.transaction_from_dict(t).introduced(),
            )
        )
    }
    snapshots = {m: _capture_package_snapshot(m) for m in pending_managers}
    for c in depart.classify_package_transactions(baseline, snapshots):
        if c.key in done or c.bucket != depart.BUCKET_OWNED:
            continue
        if c.action == depart.ACTION_DOWNGRADE:
            outcome = _execute_downgrade(baseline, c.manager, c.name)
            ledger.record(c.key, c.action, outcome)
            if outcome.startswith("halt:"):
                return False
        elif c.reason == "introduced as a dependency by this transaction":
            ledger.record(
                c.key, c.action, _execute_dependency_removal(c.manager, c.name)
            )
        else:
            ledger.record(c.key, c.action, _execute_package_removal(c.manager, c.name))
    return True


def execute_departure(
    ctx: Context,
    baseline: depart.Baseline,
    report: dict[str, depart.Classification],
) -> depart.DepartureLedger:
    """Perform every safe ``owned`` action, retry-safe via the departure ledger.

    Order: services stop/disable first, then the global git hooksPath
    restore, then file/symlink restore-or-remove, then directories
    deepest-first, then packages in reverse transaction order, then the NVM
    runtime last. A changed-state-then-failed downgrade halts the package
    phase and skips the runtime phase too.
    """
    ledger = depart.DepartureLedger(depart.departure_ledger_path(ctx.state_dir))
    execute_service_phase(ctx, baseline, ledger)
    execute_gitconfig_phase(ctx, baseline, ledger)
    execute_file_symlink_phase(ctx, baseline, report, ledger)
    execute_directory_phase(ctx, baseline, report, ledger)
    if execute_package_phase(ctx, baseline, ledger):
        execute_runtime_phase(ctx, report, ledger)
    return ledger


def _finalize_departure_state(ctx: Context) -> None:
    """After a fully successful departure: release the lock and delete state.

    Deletes baseline snapshots, ``baseline.json``, ``history.jsonl``,
    ``departure.jsonl``, the profile marker, and the state directory itself
    if it's now empty. Only called when zero unresolved/drifted items
    remain — a partial departure retains everything for a retry.

    Also makes a best-effort (non-ledger, never-blocking) sweep of the
    state directory's own now-possibly-empty ancestors — ``~/.local/state``
    and ``~/.local`` — since the generic directory phase deliberately never
    touches them (see ``_is_state_dir_or_its_ancestor``) precisely because
    their emptiness could only ever be known *after* this cleanup runs.
    """
    depart.release_departure_lock(ctx.state_dir)
    for path in _departure_state_paths(ctx.state_dir):
        path.unlink(missing_ok=True)
    ctx.manifest.path.unlink(missing_ok=True)
    ctx.profile_marker.unlink(missing_ok=True)
    if ctx.state_dir.is_dir() and not any(ctx.state_dir.iterdir()):
        ctx.state_dir.rmdir()

    ancestor = ctx.state_dir.parent
    while ancestor != ctx.home and ctx.home in ancestor.parents:
        try:
            if not ancestor.is_dir() or any(ancestor.iterdir()):
                break
            ancestor.rmdir()
        except OSError:
            break
        ancestor = ancestor.parent


def do_depart(ctx: Context) -> int:
    """Preview and execute a pristine-state departure.

    Implements the zero-evidence refusal, the four-bucket preflight
    report, the confirmation/exit-code contract, retryable execution via
    the departure ledger, and advisory-lock acquisition/release from
    Implementation Sequence steps 3 and 4. The classifier is deliberately
    conservative (see ``depart.classify_ownership_key`` and its two named
    reclassification overrides) — anything it can't classify with
    confidence lands in ``unresolved`` rather than being guessed at, so
    this only ever mutates what preflight already reported as ``owned``.
    Package removal and service/linger handling are not implemented yet
    (see ``execute_departure``'s docstring).
    """
    baseline_file = depart.baseline_path(ctx.state_dir)
    if not baseline_file.is_file():
        print(
            PALETTE.error(f"no baseline at {baseline_file} — nothing to depart from"),
            file=sys.stderr,
        )
        print(
            PALETTE.error(
                "for a guaranteed pristine reset, see the WSL unregister/recreate "
                "instructions in README.md"
            ),
            file=sys.stderr,
        )
        return 2

    # A real (non-dry-run) run with no --yes and no keyboard to confirm on
    # is a guaranteed refusal no matter what the preflight report says, so
    # check for it before printing that report — otherwise the error that
    # actually matters ends up buried under a 100+ line dump the user can't
    # act on anyway.
    if not ctx.opts.dry_run and not ctx.opts.yes and not sys.stdin.isatty():
        print(
            PALETTE.error("refusing a non-interactive real run without --yes"),
            file=sys.stderr,
        )
        return 2

    report = build_preflight_report(ctx)
    if report is None:
        print(
            PALETTE.error(f"no baseline at {baseline_file} — nothing to depart from"),
            file=sys.stderr,
        )
        return 2
    package_report = build_package_preflight(ctx) or []
    guard_annotations = _vscode_guard_preflight_annotations(ctx, report)

    _print_preflight_report(
        report,
        package_report,
        quiet=ctx.opts.quiet,
        guard_annotations=guard_annotations,
    )

    if ctx.opts.dry_run:
        print(PALETTE.header("Dry run complete — nothing was changed."))
        return 0

    if not ctx.opts.yes:
        print()
        print("Type DEPART to proceed: ", end="", flush=True)
        if _read_confirmation_token() != "DEPART":
            print(
                PALETTE.error("confirmation not received — aborting"), file=sys.stderr
            )
            return 2

    acquired, stale = depart.acquire_departure_lock(ctx.state_dir)
    if not acquired:
        print(
            PALETTE.error("another --depart is already running on this machine"),
            file=sys.stderr,
        )
        return 2
    if stale is not None:
        print(
            PALETTE.warn(
                f"reclaimed a stale departure lock (was held by pid {stale.pid})"
            )
        )

    try:
        baseline = depart.load_baseline(ctx.state_dir)
        if baseline is None:
            print(
                PALETTE.error(
                    f"no baseline at {baseline_file} — nothing to depart from"
                ),
                file=sys.stderr,
            )
            return 2

        ledger = execute_departure(ctx, baseline, report)
        failed = [
            e
            for e in ledger.entries()
            if str(e.get("outcome", "")).startswith(("unresolved", "halt"))
        ]
        unresolved_keys = [
            key
            for key, c in report.items()
            if c.bucket in (depart.BUCKET_UNRESOLVED, depart.BUCKET_DRIFTED)
        ] + [
            c.key
            for c in package_report
            if c.bucket in (depart.BUCKET_UNRESOLVED, depart.BUCKET_DRIFTED)
        ]

        if not failed and not unresolved_keys:
            _finalize_departure_state(ctx)
            print(
                PALETTE.header("Departure complete — no installer footprint remains.")
            )
            return 0

        # Preflight explains why something was never *attempted*; these are
        # the ones that were attempted and did not complete. Their reasons
        # only ever reached departure.jsonl, so a run that deliberately
        # preserved something — a tree the user added to, a package still
        # depended on — looked identical to an unexplained failure.
        if failed:
            print(PALETTE.warn(f"  attempted but not completed ({len(failed)}):"))
            for entry in failed:
                reason = str(entry.get("outcome", "")).partition(": ")[2]
                print(PALETTE.warn(f"    {entry.get('key', '')} — {reason}"))

        print(
            PALETTE.warn(
                f"⚠ departure incomplete — {len(failed) + len(unresolved_keys)} "
                "item(s) remain unresolved (see the preflight report above); "
                "re-run --depart to retry"
            )
        )
        return 1
    finally:
        depart.release_departure_lock(ctx.state_dir)


# ── links.toml audit ──────────────────────────────────────────────────────────

CHECK_BUCKET_BROKEN_SOURCE = "broken-source"
CHECK_BUCKET_WRONG_TARGET = "wrong-target"
CHECK_BUCKET_NOT_A_SYMLINK = "not-a-symlink"
CHECK_BUCKET_ORPHANED = "orphaned"
CHECK_BUCKET_UNMANAGED = "unmanaged"
CHECK_BUCKET_NEVER_INSTALLED = "never-installed"

CHECK_BUCKETS = (
    CHECK_BUCKET_BROKEN_SOURCE,
    CHECK_BUCKET_WRONG_TARGET,
    CHECK_BUCKET_NOT_A_SYMLINK,
    CHECK_BUCKET_ORPHANED,
    CHECK_BUCKET_UNMANAGED,
    CHECK_BUCKET_NEVER_INSTALLED,
)


def _is_symlink(path: Path) -> bool:
    """Return whether ``path`` is a symlink, catching OSError when unreadable."""
    try:
        return path.is_symlink()
    except OSError:
        return False


def _path_exists(path: Path) -> bool:
    """Return whether ``path`` exists, catching OSError when unreadable."""
    try:
        return path.exists()
    except OSError:
        return False


def _link_target(dest: Path) -> Path:
    """Return what ``dest`` points at, as an absolute path.

    ``symlink`` only ever writes absolute targets, but a link placed there
    by hand may be relative — resolve those against the link's own
    directory the way the kernel does, rather than against the cwd.
    """
    target = Path(os.readlink(dest))
    return target if target.is_absolute() else dest.parent / target


def _same_path(left: Path, right: Path) -> bool:
    """Compare two paths that may or may not exist, ignoring symlinked parents.

    A plain string comparison is the common case; the ``resolve`` fallback
    catches an installer run whose repo path reached the link through a
    symlink (a symlinked home, ``/tmp`` → ``/private/tmp`` on macOS), which
    would otherwise read as a wrong target.
    """
    if left == right:
        return True
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return False


def _implied_repo_root(target: Path, relative_src: str) -> Path | None:
    """Return the repo root ``target`` implies, if it ends with ``relative_src``.

    ``/home/u/dotfiles-wt/claude/global-instructions.md`` with a
    ``claude/global-instructions.md`` entry implies ``/home/u/dotfiles-wt``.
    None if the tail doesn't match, which means the link points at
    something unrelated rather than at the same file in a different
    checkout.
    """
    tail = Path(relative_src).parts
    parts = target.parts
    if len(parts) <= len(tail) or parts[-len(tail) :] != tail:
        return None
    return Path(*parts[: -len(tail)])


def _is_dotfiles_checkout(root: Path) -> bool:
    """Return whether ``root`` looks like another checkout of this repo."""
    return (root / "links.toml").is_file() and (root / "install.py").is_file()


def _is_main_checkout(root: Path) -> bool:
    """Return whether ``root`` is the repo's primary checkout, not a worktree.

    `git worktree add` gives a worktree a `.git` *file* holding a `gitdir:`
    pointer, while the primary checkout keeps `.git` as a directory. That
    difference is the whole test, and reading it costs one stat -- no git
    subprocess, which keeps this usable from the audit and from a test suite
    that blocks real subprocess calls.
    """
    try:
        return (root / ".git").is_dir()
    except OSError:
        return False


def _check_applicable_links(
    ctx: Context,
    links: Sequence[tuple[Path, Path, str, bool]],
    *,
    report_uninstalled: bool = False,
) -> tuple[dict[str, list[str]], dict[Path, int]]:
    """Report inconsistencies on destinations in scope for this machine.

    Entries whose destination does not exist at all are silently fine by
    default: that is simply a link this machine has not installed (yet), not
    a defect. That is also what makes the widened-harness default safe — an
    entry for a harness that was never provisioned has no destination to
    report on. Passing ``report_uninstalled`` additionally flags a missing
    destination whose source exists but that the manifest has never once
    recorded creating — genuinely never installed, as distinct from a link
    that was installed and later removed (rollback or manual cleanup), which
    a manifest record still explains and which stays silent either way.

    Returns:
        The findings by bucket, and a count per *other* checkout the live
        links point into. The second value is not a finding: this repo
        mandates worktree-first development, so running the audit from a
        worktree while the machine's links point at the main checkout is
        the normal case, not a defect (see :func:`do_check_links`).
    """
    findings: dict[str, list[str]] = {bucket: [] for bucket in CHECK_BUCKETS}
    foreign: dict[Path, int] = {}
    installed_dests: set[Path] = set()
    if report_uninstalled:
        installed_dests = {
            Path(str(entry["dest"]))
            for entry in ctx.manifest.entries()
            if entry.get("kind") == "symlink-created" and "dest" in entry
        }
    for src, dest, rel, applicable in links:
        if not applicable:
            continue
        if not _is_symlink(dest) and not _path_exists(dest):
            if report_uninstalled and _path_exists(src) and dest not in installed_dests:
                findings[CHECK_BUCKET_NEVER_INSTALLED].append(
                    f"{ctx.display(dest)} — {src} exists in the repo but was "
                    "never linked here; run install.sh to link it"
                )
            continue

        if not _is_symlink(dest):
            # Reached through a symlinked *parent* (a directory-level entry
            # linking the ancestor) the file is still correctly wired, even
            # though this path is not itself a link.
            if not _same_path(dest, src):
                findings[CHECK_BUCKET_NOT_A_SYMLINK].append(
                    f"{ctx.display(dest)} — a real "
                    f"{'directory' if dest.is_dir() else 'file'} sits where a "
                    f"symlink to {src} belongs; the next install run would "
                    "back it up and replace it"
                )
            continue

        target = _link_target(dest)
        if not _same_path(target, src):
            other_root = _implied_repo_root(target, rel)
            # Only a link into the PRIMARY checkout is excusable. The
            # direction matters and used to be ignored: auditing from a
            # worktree while the machine points at main is the normal state
            # under worktree-first development, but a link pointing INTO a
            # worktree is drift -- someone hand-pointed it for a live test and
            # left it there, and it dangles the moment that worktree is
            # removed, silently unloading whatever it provided. Both cases
            # answered "a different checkout?" the same way, so the second was
            # filed as a benign note and dropped from the audited count. It
            # has bitten three times: custom-footer.ts, then permission-gate.ts
            # and swarm-tool.ts on 2026-09-02.
            same_file_other_checkout = (
                other_root is not None
                and not _same_path(other_root, ctx.dotfiles)
                and _is_dotfiles_checkout(other_root)
                and _is_main_checkout(other_root)
            )
            if same_file_other_checkout:
                # A dangling link is a real machine problem regardless of
                # which checkout it points into, so that still gets reported.
                if not _path_exists(target):
                    findings[CHECK_BUCKET_BROKEN_SOURCE].append(
                        f"{ctx.display(dest)} — links to {target}, which no "
                        "longer exists (dangling symlink)"
                    )
                else:
                    assert other_root is not None  # narrowed by the guard above
                    foreign[other_root] = foreign.get(other_root, 0) + 1
                continue
            findings[CHECK_BUCKET_WRONG_TARGET].append(
                f"{ctx.display(dest)} — points at {target}, but links.toml says {src}"
            )
            continue

        if not _path_exists(src):
            findings[CHECK_BUCKET_BROKEN_SOURCE].append(
                f"{ctx.display(dest)} — links to {src}, which no longer "
                "exists in the repo (dangling symlink)"
            )
    return findings, foreign


def _find_orphaned_links(
    ctx: Context, links: Sequence[tuple[Path, Path, str, bool]]
) -> list[Path]:
    """Return manifest-recorded symlink destinations no current entry produces.

    Compared against *every* triple's destination rather than only the
    applicable ones: a triple that is merely gated off on this machine (a
    mac-only link seen from Linux, a harness not selected this run) has not
    been removed from links.toml, so its recorded destination is not an
    orphan — reporting it as one would be a false positive on every
    cross-platform machine, or on a run scoped to a different harness.
    """
    known = {dest for _src, dest, _rel, _applicable in links}
    orphans: list[Path] = []
    seen: set[Path] = set()
    for entry in ctx.manifest.entries():
        if entry.get("kind") != "symlink-created":
            continue
        dest = Path(str(entry.get("dest", "")))
        if dest in known or dest in seen:
            continue
        seen.add(dest)
        # A dest that no longer exists needs no report: a past --rollback,
        # or the user, already cleaned it up.
        if not _is_symlink(dest) and not _path_exists(dest):
            continue
        orphans.append(dest)
    return orphans


def _check_orphaned_links(
    ctx: Context,
    links: Sequence[tuple[Path, Path, str, bool]],
    findings: dict[str, list[str]],
) -> None:
    """Add manifest-recorded symlinks that links.toml no longer produces."""
    for dest in _find_orphaned_links(ctx, links):
        if _is_symlink(dest):
            detail = f"still symlinked → {_link_target(dest)}"
        else:
            detail = "still present as a real file"
        findings[CHECK_BUCKET_ORPHANED].append(
            f"{ctx.display(dest)} — recorded by a past install run, but no "
            f"links.toml entry produces it anymore; {detail}"
        )


def _live_backup_paths(ctx: Context) -> set[Path]:
    """Return manifest-recorded backups that are still live ``--rollback`` payload.

    Liveness means "the destination is still present at all", not "it
    resolves": ``shutil.move(backup, dest)`` replaces a dangling symlink just
    as readily as a healthy one, so a broken link does not make its backup
    disposable. Reporting one would tell the user to delete the only copy of
    their pre-dotfiles original, which is the opposite of what the backup is
    for.
    """
    live: set[Path] = set()
    for entry in ctx.manifest.entries():
        if entry.get("kind") != "file-backed-up":
            continue
        dest = Path(str(entry.get("dest", "")))
        backup = Path(str(entry.get("backup", "")))
        if not dest.parts or not backup.parts:
            continue
        if _path_exists(backup) and (_path_exists(dest) or _is_symlink(dest)):
            live.add(backup)
    return live


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
    """Report foreign entries in directories ``links.toml`` owns exclusively.

    Nothing else catches these. ``--rollback`` only inspects what the history
    recorded, ``--depart`` compares against an install-time baseline, and the
    repo-to-links.toml parity tests check both directions of the mapping yet
    cannot see a file that exists only on the installed side.

    Args:
        specs: Parsed ``[[link]]`` rows, for :func:`_dir_applies`.
        links: Every gathered triple, applicable or not — a gated row's
            destination is still ours, so it must never read as foreign.
        managed_dirs: Parsed ``[[managed_dir]]`` rows.
        findings: Bucket map to append into.

    Returns:
        How many declared directories were actually audited.
    """
    live_backups = _live_backup_paths(ctx)
    audited = 0
    for dir_spec in managed_dirs:
        directory = expand_dest(dir_spec.dest, ctx.home)
        if not directory.is_dir():
            continue
        if not _dir_applies(dir_spec, specs, ctx):
            continue
        audited += 1
        managed = {
            dest
            for _src, dest, _rel, _applicable in links
            if dest.is_relative_to(directory)
        }
        try:
            entries = sorted(os.listdir(directory))
        except OSError as exc:
            findings[CHECK_BUCKET_UNMANAGED].append(
                f"{ctx.display(directory)} — declared exclusive, but unreadable "
                f"({exc.strerror or exc}), so it could not be audited"
            )
            continue
        for name in entries:
            path = directory / name
            if path in managed or path in live_backups:
                continue
            if any(fnmatch.fnmatch(name, pat) for pat in dir_spec.ignore):
                continue
            # Hidden files are skipped: macOS drops .DS_Store into any directory
            # the user merely opens in Finder.
            if name.startswith(".") or name.endswith(_JUNK_SUFFIXES):
                continue
            if path.is_dir() and not _is_symlink(path):
                continue
            findings[CHECK_BUCKET_UNMANAGED].append(
                f"{ctx.display(path)} — {dir_spec.dest} is declared exclusive "
                "to dotfiles, but no links.toml entry produces it"
            )
    return audited


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

    specs = load_links(ctx.dotfiles / "links.toml")
    managed_dirs = load_managed_dirs(ctx.dotfiles / "links.toml")
    links = gather_links(ctx, specs)
    findings, foreign = _check_applicable_links(
        ctx, links, report_uninstalled=ctx.opts.report_uninstalled
    )
    _check_orphaned_links(ctx, links, findings)
    dirs_audited = _check_unmanaged_files(ctx, specs, links, managed_dirs, findings)

    _header("==> links.toml audit (read-only)", quiet=ctx.opts.quiet)
    for root, count in sorted(foreign.items()):
        print(
            PALETTE.dim(
                f"  note: {count} link(s) point into {root} rather than this "
                f"checkout ({ctx.dotfiles}) — you are running from a worktree, "
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

    if ctx.is_mac:
        install_mac_packages(ctx)
    elif ctx.is_linux:
        install_linux_packages(ctx)

    install_node(ctx)
    install_npm_harness(ctx, "claude", "Claude Code", "@anthropic-ai/claude-code")
    install_npm_harness(ctx, "copilot", "Copilot CLI", "@github/copilot")

    install_symlinks(ctx, links)
    _cleanup_orphaned_links(ctx, links)
    opencode_drift = seed_opencode_config(ctx)
    settings_drift = seed_claude_settings(ctx)
    pi_settings_drift = seed_pi_settings(ctx)
    vscode_drift = seed_vscode_settings(ctx)

    if ctx.is_mac:
        import_rectangle_prefs(ctx)
        set_caps_lock_to_escape(ctx)
        load_watchcommit_agent(ctx)
    capture_service_baseline(ctx)
    enable_managed_services(ctx)
    capture_git_hooks_path_baseline(ctx)
    install_global_git_hooks_path(ctx)

    install_vim_plug(ctx)
    bootstrap_neovim(ctx)
    write_profile_marker(ctx)

    if ctx.departure_baseline is not None:
        depart.save_baseline(ctx.state_dir, ctx.departure_baseline)

    print_summary(ctx, settings_drift, opencode_drift, vscode_drift, pi_settings_drift)
    return 1 if ctx.reporter.skipped else 0


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, then roll back, depart, audit links, or install.

    Returns:
        Process exit status: 0 clean, 1 something was skipped (or, under
        ``--check-links``, something was found), 2 refused (bad arguments,
        an unreadable links.toml, or the work-profile guard).
    """
    global PALETTE
    PALETTE = Palette(color_enabled(sys.stdout))

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

    try:
        specs = load_links(ctx.dotfiles / "links.toml")
    except (ValueError, TypeError) as exc:
        print(
            PALETTE.error(f"could not read the symlink table: {exc}"), file=sys.stderr
        )
        return 2

    return run_install(ctx, specs)


if __name__ == "__main__":
    raise SystemExit(main())
