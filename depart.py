#!/usr/bin/env python3
"""Pristine-state departure mode: baseline capture and ownership tracking.

Kept separate from ``install.py`` — see that file's module docstring and the
execution plan this implements
(``~/.claude/data/grill/2026-08-13-dotfiles-depart-mode-v2-narrowed-plan.md``)
for the full design rationale. This module owns only the data model and
pure capture/comparison logic (Implementation Sequence step 1); CLI wiring,
package-transaction tracking, and the actual cleanup live alongside it in
``install.py`` and later modules as the feature grows.

Ownership keys are ``(type, path)`` pairs, rendered as ``"type:path"``
strings for storage (``file:/home/user/.zshrc``,
``package:uv-tool/ruff``, ...). Every key's baseline value is one of three
states — ``present``, ``absent``, or ``unknown`` — never conflated with
*unrecorded*, which means the key has no entry in any baseline layer at
all (see :func:`Baseline.is_unrecorded`).

Linux/WSL (apt) and Fedora (dnf) only — this feature does not apply on
macOS, matching Implementation Sequence step 6.
"""

import fcntl
import hashlib
import json
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

STATE_PRESENT = "present"
STATE_ABSENT = "absent"
STATE_UNKNOWN = "unknown"

RC_FILENAMES = (".bashrc", ".zshrc", ".profile")

# Shared Neovim XDG state/cache dirs the installer never owns the *contents*
# of, only whether the directory itself pre-existed — see
# ``_wipe_neovim_dirs`` in install.py for why these three specifically.
SHARED_NEOVIM_DIRS = (
    (".local", "share", "nvim"),
    (".local", "state", "nvim"),
    (".cache", "nvim"),
)


# ── ownership keys ────────────────────────────────────────────────────────


def file_key(path: Path) -> str:
    return f"file:{path}"


def symlink_key(path: Path) -> str:
    return f"symlink:{path}"


def directory_key(path: Path) -> str:
    return f"directory:{path}"


def package_key(manager: str, name: str) -> str:
    return f"package:{manager}/{name}"


def service_key(manager: str, name: str) -> str:
    return f"service:{manager}/{name}"


def runtime_key(root: Path) -> str:
    return f"runtime:{root}"


def gitconfig_key(name: str) -> str:
    return f"gitconfig:{name}"


def key_type(key: str) -> str:
    """Return the ``type`` half of an ownership key string."""
    type_, _, _ = key.partition(":")
    return type_


# ── capturing individual keys ─────────────────────────────────────────────


def capture_file(path: Path, *, blob_dir: Path | None = None) -> dict[str, object]:
    """Capture a ``file:`` record: present (with size+sha256), absent, or unknown.

    A symlink at ``path`` is *not* a regular file for this key's purposes —
    it is captured separately as a ``symlink:`` key at the same path, since
    ``file:<path>`` and ``symlink:<path>`` are deliberately distinct keys
    for the same path (see the module docstring).

    Args:
        path: The file to capture.
        blob_dir: When given and the file is present, also write its
            content to a blob under this directory (normally the state
            directory) and record the digest as ``blob``. Callers only pass
            this for rc files and links.toml/seed destinations — the only
            categories that can ever need content restored, per
            Implementation Sequence step 1's blob-creation rule.
    """
    try:
        if path.is_symlink() or not path.is_file():
            return {"state": STATE_ABSENT}
        content = path.read_bytes()
    except OSError:
        return {"state": STATE_UNKNOWN}
    record: dict[str, object] = {
        "state": STATE_PRESENT,
        "size": len(content),
        "sha256": hashlib.sha256(content).hexdigest(),
    }
    if blob_dir is not None:
        record["blob"] = write_blob(blob_dir, content)
    return record


def capture_symlink(path: Path) -> dict[str, object]:
    """Capture a ``symlink:`` record: present (with target text), absent, unknown."""
    try:
        if not path.is_symlink():
            return {"state": STATE_ABSENT}
        target = os.readlink(path)
    except OSError:
        return {"state": STATE_UNKNOWN}
    return {"state": STATE_PRESENT, "target": target}


def capture_directory(path: Path) -> dict[str, object]:
    """Capture a ``directory:`` record: present, absent, or unknown.

    A symlink at ``path`` is not a directory for this key's purposes, same
    reasoning as :func:`capture_file`.
    """
    try:
        if path.is_symlink():
            return {"state": STATE_ABSENT}
        exists = path.is_dir()
    except OSError:
        return {"state": STATE_UNKNOWN}
    return {"state": STATE_PRESENT if exists else STATE_ABSENT}


def ancestor_directories(path: Path, home: Path) -> list[Path]:
    """Every directory strictly between ``path``'s parent and ``home``.

    Used to enumerate ``mkdir(parents=True)`` candidates for a destination
    the installer might create — ``~/.local/bin``, a symlink destination's
    parent, and so on — per Implementation Sequence step 1's parent-
    directory coverage bullet. ``home`` itself is never included (it always
    pre-exists and is never installer-owned), and a ``path`` outside
    ``home`` entirely yields an empty list rather than walking to the
    filesystem root.
    """
    ancestors: list[Path] = []
    current = path.parent
    while current != home and home in current.parents:
        ancestors.append(current)
        current = current.parent
    return ancestors


def capture_tree_manifest(root: Path) -> dict[str, object]:
    """Full recursive manifest for a small, fully installer-controlled tree.

    Every regular file's size+SHA-256, every symlink's target text, and
    every directory entry (including empty directories) — used only for
    the Neovim fallback prefix and the Nerd Font directory, per
    Implementation Sequence step 1's tree-manifest exception. Anything
    changed anywhere in the tree is meant to make the whole thing
    divergent, so this must not silently skip entries it can't read.
    """
    try:
        if root.is_symlink() or not root.is_dir():
            return {"state": STATE_ABSENT}
    except OSError:
        return {"state": STATE_UNKNOWN}

    entries: dict[str, object] = {}
    try:
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            rel_dir = Path(dirpath).relative_to(root)
            for name in dirnames:
                child = Path(dirpath) / name
                rel = str(rel_dir / name) if str(rel_dir) != "." else name
                if child.is_symlink():
                    entries[f"symlink:{rel}"] = os.readlink(child)
                else:
                    entries[f"dir:{rel}"] = True
            for name in filenames:
                child = Path(dirpath) / name
                rel = str(rel_dir / name) if str(rel_dir) != "." else name
                if child.is_symlink():
                    entries[f"symlink:{rel}"] = os.readlink(child)
                else:
                    content = child.read_bytes()
                    entries[f"file:{rel}"] = {
                        "size": len(content),
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
    except OSError:
        return {"state": STATE_UNKNOWN}
    return {"state": STATE_PRESENT, "entries": entries}


def tree_manifest_matches(root: Path, recorded: dict[str, object]) -> bool:
    """Whether ``root``'s current tree still matches a recorded tree manifest.

    False for anything other than an exact match, including a recorded
    ``unknown``/``absent`` state or a current capture that errors — callers
    treat "doesn't match" as "don't remove wholesale," which is the safe
    default for a tree-manifest artifact.
    """
    if recorded.get("state") != STATE_PRESENT:
        return False
    current = capture_tree_manifest(root)
    return current.get("state") == STATE_PRESENT and current.get(
        "entries"
    ) == recorded.get("entries")


def capture_runtime_nvm(home: Path) -> dict[str, object]:
    """Capture the lightweight ``runtime:<root>`` record for NVM/Node.

    Deliberately *not* a full tree manifest (see the module-level docstring
    on ``runtime:`` divergence in the execution plan) — only the root's
    existence and the installed Node version directory names, since normal
    ``npm install -g`` usage constantly adds files underneath ``~/.nvm``
    and must not count as divergence.
    """
    root = home / ".nvm"
    try:
        if root.is_symlink() or not root.is_dir():
            return {"state": STATE_ABSENT}
        versions_dir = root / "versions" / "node"
        versions = (
            sorted(p.name for p in versions_dir.iterdir() if p.is_dir())
            if versions_dir.is_dir()
            else []
        )
    except OSError:
        return {"state": STATE_UNKNOWN}
    return {"state": STATE_PRESENT, "versions": versions}


def runtime_nvm_diverged(home: Path, recorded: dict[str, object]) -> bool:
    """Whether live NVM state has diverged from its recorded top-level markers.

    Only a change to what the installer itself introduced counts:
    disappearance of the root, or removal of a Node version baseline
    recorded as installed. Additional versions or npm packages added since
    are normal use, not divergence.
    """
    if recorded.get("state") != STATE_PRESENT:
        return False
    current = capture_runtime_nvm(home)
    if current.get("state") != STATE_PRESENT:
        return True
    recorded_versions = set(recorded.get("versions", []))  # type: ignore[arg-type]
    current_versions = set(current.get("versions", []))  # type: ignore[arg-type]
    return not recorded_versions.issubset(current_versions)


# ── content blobs ─────────────────────────────────────────────────────────


def blob_path(state_dir: Path, digest: str) -> Path:
    return state_dir / f"baseline-snapshot-{digest}.blob"


def write_blob(state_dir: Path, content: bytes) -> str:
    """Write a content-addressed blob, returning its SHA-256 hex digest.

    A no-op if a blob with this exact content already exists (the same rc
    file captured across two supplementary layers, for instance) — content
    addressing makes that safe rather than merely tolerable.
    """
    digest = hashlib.sha256(content).hexdigest()
    path = blob_path(state_dir, digest)
    if not path.is_file():
        state_dir.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return digest


def read_blob(state_dir: Path, digest: str) -> bytes | None:
    """Read a blob's content back, or None if it's missing/unreadable.

    A missing referenced blob is a capture-time bug or on-disk corruption,
    never a signal to fabricate content — callers must treat this as
    ``unresolved``, not fall back to any other source.
    """
    try:
        return blob_path(state_dir, digest).read_bytes()
    except OSError:
        return None


# ── baseline layers ───────────────────────────────────────────────────────


@dataclass
class Layer:
    """One capture pass: a timestamp and the ownership-key records from it."""

    captured_at: str
    records: dict[str, dict[str, object]] = field(default_factory=dict)


@dataclass
class Baseline:
    """The full departure baseline: immutable first layer, later supplements.

    A key's authoritative value is whichever layer first recorded it —
    layers are appended, never rewritten (see ``is_unrecorded`` and
    ``add_layer``).
    """

    layers: list[Layer] = field(default_factory=list)
    transactions: list[dict[str, object]] = field(default_factory=list)
    # Post-install tree manifests, keyed by absolute path — see
    # record_installed_tree. Deliberately NOT a layer: layers hold
    # pre-install values under an immutable-first-wins rule, and these
    # trees are already recorded there as `absent`. This is the different,
    # later fact of what the installer itself produced, so it sits beside
    # `transactions` rather than fighting the layering model.
    installed_trees: dict[str, dict[str, object]] = field(default_factory=dict)

    def value_for(self, key: str) -> dict[str, object] | None:
        """The recorded record for ``key``, from the earliest layer that has it."""
        for layer in self.layers:
            if key in layer.records:
                return layer.records[key]
        return None

    def is_unrecorded(self, key: str) -> bool:
        """Whether ``key`` has no entry in *any* layer yet.

        Distinct from a recorded ``absent`` value — see the module
        docstring. A key already captured as ``absent`` in an earlier layer
        is not unrecorded, and must never be re-captured into a later layer
        just because its value happens to be ``absent``.
        """
        return self.value_for(key) is None

    def all_keys(self) -> set[str]:
        keys: set[str] = set()
        for layer in self.layers:
            keys.update(layer.records)
        return keys

    def add_layer(
        self, captured_at: str, candidate_records: dict[str, dict[str, object]]
    ) -> None:
        """Append a new layer containing only genuinely unrecorded keys.

        A no-op layer (nothing in ``candidate_records`` was unrecorded) is
        not appended at all, so ``layers`` never accumulates empty entries.
        The very first call — when ``layers`` is empty — makes every
        candidate key unrecorded by definition, so the whole set becomes
        the immutable first layer.
        """
        to_add = {
            key: record
            for key, record in candidate_records.items()
            if self.is_unrecorded(key)
        }
        if to_add:
            self.layers.append(Layer(captured_at=captured_at, records=to_add))


# Verdicts from :func:`installed_tree_verdict`.
TREE_UNCHANGED = "unchanged"
TREE_MODIFIED = "modified"
TREE_UNRECORDED = "unrecorded"


def record_installed_tree(baseline: Baseline, root: Path) -> None:
    """Snapshot ``root`` as the installer just produced it.

    Called immediately after the installer creates a fully-owned tree (the
    Nerd Font directory, the pinned Neovim prefix). Without this snapshot
    there is nothing to compare live state against at departure: the
    baseline layer only holds the *pre-install* ``absent`` value, which
    proves the installer created the tree but says nothing about whether
    the user has since added or edited anything inside it.

    Re-recording on a later run is correct and intended — the tree the
    installer most recently produced is the one departure should expect to
    find, so this overwrites rather than preserving the first value.
    """
    baseline.installed_trees[str(root)] = capture_tree_manifest(root)


def installed_tree_verdict(baseline: Baseline, root: Path) -> str:
    """Classify a wholly installer-owned tree for safe wholesale removal.

    Returns ``TREE_UNCHANGED`` only when a post-install manifest was
    recorded and the tree on disk still matches it exactly — the sole case
    where deleting the whole tree cannot destroy something the user added.
    ``TREE_MODIFIED`` when a manifest exists but no longer matches.
    ``TREE_UNRECORDED`` when none was ever recorded (a baseline captured
    before snapshotting existed), which callers must treat as "cannot
    prove this is safe," never as permission to remove.
    """
    recorded = baseline.installed_trees.get(str(root))
    if recorded is None:
        return TREE_UNRECORDED
    return TREE_UNCHANGED if tree_manifest_matches(root, recorded) else TREE_MODIFIED


def remove_manifest_tree(baseline: Baseline, path: Path) -> str:
    """Remove ``path`` wholesale, but only if its :func:`installed_tree_verdict`
    is ``TREE_UNCHANGED`` — the departure-time policy: refuse unless
    provably unchanged, since refusing is departure's safe terminal outcome.
    (``install._install_neovim_fallback`` has its own, more permissive
    install-time policy for the same tree-manifest-tracked prefix — it also
    self-heals on ``TREE_UNRECORDED``, which this function deliberately does
    not, so it calls :func:`installed_tree_verdict` directly instead of
    this function. Both are still the only ``shutil.rmtree`` sites for a
    manifest-tracked directory; see ``test_depart_removal_guard.py``'s
    file-wide ban on calling ``shutil.rmtree`` anywhere else.) Returns the
    verdict: ``TREE_UNCHANGED`` means the removal happened;
    ``TREE_MODIFIED``/``TREE_UNRECORDED`` mean nothing was touched and the
    caller must decide how to report that.

    Propagates ``OSError`` from the removal itself (e.g. a permissions
    error, or the directory vanishing between the verdict check and the
    ``rmtree`` call) rather than swallowing it — same contract as
    ``shutil.rmtree``. Callers must catch it, the way
    ``install._remove_tree_manifest_directory`` already does.

    No separate existence/symlink precondition is needed here: a
    ``TREE_UNCHANGED`` verdict already implies ``path`` is a real, existing
    directory, since :func:`installed_tree_verdict` can only return it when
    a fresh :func:`capture_tree_manifest` of ``path`` is exactly
    ``STATE_PRESENT`` and matches a ``STATE_PRESENT`` recorded baseline — an
    absent, symlinked, or non-directory ``path`` always captures as
    ``STATE_ABSENT``, which can never equal a ``STATE_PRESENT`` baseline.
    """
    verdict = installed_tree_verdict(baseline, path)
    if verdict == TREE_UNCHANGED:
        shutil.rmtree(path)
    return verdict


# ── serialization ─────────────────────────────────────────────────────────

BASELINE_VERSION = 1


def baseline_path(state_dir: Path) -> Path:
    return state_dir / "baseline.json"


def baseline_to_dict(baseline: Baseline) -> dict[str, object]:
    return {
        "version": BASELINE_VERSION,
        "layers": [
            {"captured_at": layer.captured_at, "records": layer.records}
            for layer in baseline.layers
        ],
        "transactions": baseline.transactions,
        "installed_trees": baseline.installed_trees,
    }


def baseline_from_dict(data: dict[str, object]) -> Baseline:
    raw_layers = data.get("layers", [])
    layers = [
        Layer(
            captured_at=str(entry.get("captured_at", "")),
            records=dict(entry.get("records", {})),  # type: ignore[arg-type]
        )
        for entry in raw_layers  # type: ignore[union-attr]
    ]
    raw_transactions = data.get("transactions", [])
    raw_trees = data.get("installed_trees", {})
    return Baseline(
        layers=layers,
        transactions=list(raw_transactions),  # type: ignore[arg-type]
        installed_trees=dict(raw_trees),  # type: ignore[arg-type]
    )


def save_baseline(state_dir: Path, baseline: Baseline) -> None:
    """Durably write ``baseline.json`` via a temp-file-then-replace swap."""

    path = baseline_path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(baseline_to_dict(baseline), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


def load_baseline(state_dir: Path) -> Baseline | None:
    """Load ``baseline.json``, or None if it's missing, empty, or unparseable."""

    path = baseline_path(state_dir)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.strip():
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return baseline_from_dict(data)


# ── package managers: probe commands and output parsing ───────────────────
#
# Implementation Sequence step 2. These are pure functions — command argv
# builders and output parsers — so they're testable without a subprocess.
# Wiring them into the live install flow (running the probes through
# install.run_command at the right point in each package-installing
# function, and recording the resulting Transaction) is a separate,
# larger change to install.py's already heavily-tested package-install
# surface and is not done by this module.

PACKAGE_MANAGERS = ("apt", "dnf", "npm", "uv-tool")


def dpkg_query_command() -> list[str]:
    """List every installed apt package and its version, tab-separated."""
    return ["dpkg-query", "-W", "-f=${Package}\t${Version}\n"]


def parse_dpkg_query(output: str) -> dict[str, str]:
    """Parse :func:`dpkg_query_command` output into ``{name: version}``."""
    versions: dict[str, str] = {}
    for line in output.splitlines():
        name, sep, version = line.partition("\t")
        if sep and name:
            versions[name] = version.strip()
    return versions


def rpm_qa_command() -> list[str]:
    """List every installed dnf/rpm package and its version, tab-separated."""
    return ["rpm", "-qa", "--qf", "%{NAME}\t%{VERSION}-%{RELEASE}\n"]


def parse_rpm_qa(output: str) -> dict[str, str]:
    """Parse :func:`rpm_qa_command` output into ``{name: version}``."""
    return parse_dpkg_query(output)  # same tab-separated shape


def npm_ls_global_command() -> list[str]:
    """List globally installed npm packages as JSON."""
    return ["npm", "ls", "-g", "--depth=0", "--json"]


def parse_npm_ls_global(output: str) -> dict[str, str]:
    """Parse :func:`npm_ls_global_command` JSON output into ``{name: version}``.

    Unparseable or unexpectedly-shaped output yields ``{}`` rather than
    raising — a probe failure is signaled by the caller checking the
    command's own exit status, not by this parser guessing.
    """
    try:
        data = json.loads(output)
    except json.JSONDecodeError:
        return {}
    deps = data.get("dependencies") if isinstance(data, dict) else None
    if not isinstance(deps, dict):
        return {}
    versions: dict[str, str] = {}
    for name, info in deps.items():
        if isinstance(info, dict) and isinstance(info.get("version"), str):
            versions[name] = info["version"]
    return versions


def uv_tool_list_command() -> list[str]:
    """List installed ``uv tool`` packages and their versions."""
    return ["uv", "tool", "list"]


def parse_uv_tool_list(output: str) -> dict[str, str]:
    """Parse :func:`uv_tool_list_command` output into ``{name: version}``.

    Each installed tool is a non-indented ``<name> v<version>`` line,
    followed by indented lines naming the executables it provides — those
    are skipped by only matching lines with no leading whitespace.
    """
    versions: dict[str, str] = {}
    for line in output.splitlines():
        if not line or line[0].isspace():
            continue
        name, sep, version = line.strip().partition(" ")
        if sep and version.startswith("v"):
            versions[name] = version[1:]
    return versions


def apt_remove_command(package: str) -> list[str]:
    """Pinned apt removal command — never ``purge`` or ``autoremove``."""
    return ["sudo", "apt-get", "remove", "-y", package]


def dnf_remove_command(package: str) -> list[str]:
    """Pinned dnf removal command — never ``autoremove``."""
    return ["sudo", "dnf", "remove", "-y", package]


def npm_uninstall_command(package: str) -> list[str]:
    return ["npm", "uninstall", "-g", package]


def uv_tool_uninstall_command(name: str) -> list[str]:
    return ["uv", "tool", "uninstall", name]


def apt_downgrade_command(package: str, version: str) -> list[str]:
    return ["sudo", "apt-get", "install", "-y", f"{package}={version}"]


def dnf_downgrade_command(package: str, version: str) -> list[str]:
    return ["sudo", "dnf", "downgrade", "-y", f"{package}-{version}"]


def apt_rdepends_command(package: str) -> list[str]:
    return ["apt-cache", "rdepends", "--installed", package]


def dnf_whatrequires_command(package: str) -> list[str]:
    return ["dnf", "repoquery", "--whatrequires", "--installed", package]


def classify_rdepends_result(ok: bool, stdout: str) -> str:
    """Classify a reverse-dependency probe's outcome for removal eligibility.

    Returns:
        ``"removable"`` only when the probe succeeded and returned an
        explicitly empty result. ``"in-use"`` when the probe succeeded but
        named at least one installed reverse dependency. ``"unknown"`` for
        any failed/unavailable probe — never treated as proof of
        removability, exactly like an ``unknown`` baseline record blocks
        removal.
    """
    if not ok:
        return "unknown"
    return "removable" if not stdout.strip() else "in-use"


# ── package transactions ────────────────────────────────────────────────


@dataclass
class Transaction:
    """One package-manager operation's before/after state and provenance.

    ``requested`` is what this transaction asked for (usually a single
    package name); ``before``/``after`` are fresh ``{name: version}``
    snapshots of the whole manager taken immediately around the operation.
    ``comparand_source`` and ``interference`` record — purely for
    diagnostic purposes, never blocking anything, matching this installer's
    no-abort convention — whether anything unrelated changed between the
    previous same-manager transaction (or, for a manager's first
    transaction in a run, its epoch inventory) and this one's fresh
    ``before`` snapshot.
    """

    manager: str
    requested: list[str]
    before: dict[str, str]
    after: dict[str, str]
    comparand_source: str  # "epoch" | "previous" | "none"
    interference: list[str]
    captured_at: str

    def introduced(self) -> list[str]:
        """Packages that appeared as a side effect of this transaction.

        Excludes the requested package(s) themselves — those are removed
        via their own pinned top-level removal command, not the
        rdepends-gated introduced-dependency path.
        """
        return sorted(set(self.after) - set(self.before) - set(self.requested))


def transaction_to_dict(txn: Transaction) -> dict[str, object]:
    return {
        "manager": txn.manager,
        "requested": txn.requested,
        "before": txn.before,
        "after": txn.after,
        "comparand_source": txn.comparand_source,
        "interference": txn.interference,
        "captured_at": txn.captured_at,
    }


def transaction_from_dict(data: dict[str, object]) -> Transaction:
    return Transaction(
        manager=str(data.get("manager", "")),
        requested=list(data.get("requested", [])),  # type: ignore[arg-type]
        before=dict(data.get("before", {})),  # type: ignore[arg-type]
        after=dict(data.get("after", {})),  # type: ignore[arg-type]
        comparand_source=str(data.get("comparand_source", "none")),
        interference=list(data.get("interference", [])),  # type: ignore[arg-type]
        captured_at=str(data.get("captured_at", "")),
    )


def _manager_transactions(baseline: Baseline, manager: str) -> list[Transaction]:
    return [
        transaction_from_dict(t)
        for t in baseline.transactions
        if t.get("manager") == manager
    ]


def record_transaction(
    baseline: Baseline,
    *,
    manager: str,
    requested: list[str],
    before: dict[str, str],
    after: dict[str, str],
    captured_at: str,
    epoch: dict[str, str] | None = None,
) -> Transaction:
    """Build a :class:`Transaction`, append it to ``baseline``, and return it.

    The interference comparand is the immediately preceding transaction of
    the *same manager* (its ``after`` snapshot) if one exists in this
    baseline yet; otherwise ``epoch`` — that manager's install-epoch
    inventory, captured once before its first transaction ever runs — if
    given; otherwise there is nothing to compare against yet
    (``comparand_source="none"``, no interference computed).
    """
    prior = _manager_transactions(baseline, manager)
    if prior:
        comparand = prior[-1].after
        comparand_source = "previous"
    elif epoch is not None:
        comparand = epoch
        comparand_source = "epoch"
    else:
        comparand = {}
        comparand_source = "none"

    interference = (
        sorted(
            name
            for name in set(comparand) | set(before)
            if comparand.get(name) != before.get(name)
        )
        if comparand_source != "none"
        else []
    )

    txn = Transaction(
        manager=manager,
        requested=list(requested),
        before=before,
        after=after,
        comparand_source=comparand_source,
        interference=interference,
        captured_at=captured_at,
    )
    baseline.transactions.append(transaction_to_dict(txn))
    return txn


# ── downgrade ladder ─────────────────────────────────────────────────────


def earliest_recorded_version(
    baseline: Baseline, manager: str, name: str
) -> str | None:
    """``name``'s true pre-install version for ``manager``, if ever recorded.

    The first ``before`` value across that manager's transactions, in
    baseline order — i.e. the value captured immediately preceding the very
    first transaction that ever touched this package.
    """
    for txn in _manager_transactions(baseline, manager):
        if name in txn.before:
            return txn.before[name]
    return None


def downgrade_candidates(baseline: Baseline, manager: str, name: str) -> list[str]:
    """Versions to try, in order, when downgrading an installer-upgraded package.

    The earliest recorded (true pre-install) version first, then every
    other distinct version this package has been recorded ``after`` a
    transaction at, most-recently-observed first — matching the plan's
    downgrade ladder (direct-to-earliest, then intermediate versions in
    reverse chronological order).
    """
    earliest = earliest_recorded_version(baseline, manager, name)
    afters: list[str] = []
    for txn in _manager_transactions(baseline, manager):
        version = txn.after.get(name)
        if version is not None and version not in afters:
            afters.append(version)

    candidates: list[str] = []
    if earliest is not None:
        candidates.append(earliest)
    for version in reversed(afters):
        if version not in candidates:
            candidates.append(version)
    return candidates


# ── preflight classification ────────────────────────────────────────────
#
# Implementation Sequence step 3. Deliberately conservative: only the two
# structurally unambiguous cases are ever classified `owned` here — a key
# genuinely absent at baseline that now exists (safe to remove), and a key
# whose live value is identical to its recorded baseline value (never
# touched, `preserved`). Every other observed difference — an rc file with
# appended content, a symlink-then-restore destination, a retargeted
# symlink, a still-non-empty directory — is left `unresolved` rather than
# guessed. Those per-category restore rules are step 4's job; a wrong guess
# here would be a destructive-action bug, not a cosmetic one, so this
# module does not attempt them ahead of that step landing.

BUCKET_OWNED = "owned"
BUCKET_PRESERVED = "preserved"
BUCKET_DRIFTED = "drifted"
BUCKET_UNRESOLVED = "unresolved"

ACTION_REMOVE = "remove"
ACTION_RESTORE = "restore"


@dataclass(frozen=True)
class Classification:
    bucket: str
    action: str | None
    reason: str


def _values_equal(a: dict[str, object], b: dict[str, object]) -> bool:
    """Whether two capture records for the same key describe the same content."""
    if a.get("state") != b.get("state"):
        return False
    if a.get("state") != STATE_PRESENT:
        return True
    if "sha256" in a or "sha256" in b:
        return a.get("sha256") == b.get("sha256") and a.get("size") == b.get("size")
    if "target" in a or "target" in b:
        return a.get("target") == b.get("target")
    if "entries" in a or "entries" in b:
        return a.get("entries") == b.get("entries")
    if "versions" in a or "versions" in b:
        return set(a.get("versions", [])) == set(b.get("versions", []))  # type: ignore[arg-type]
    return a == b


def classify_ownership_key(
    key: str, recorded: dict[str, object] | None, live: dict[str, object]
) -> Classification:
    """Classify one ownership key for the departure preflight report."""
    if recorded is None:
        return Classification(
            BUCKET_UNRESOLVED, None, "no baseline record for this key"
        )
    if recorded.get("state") == STATE_UNKNOWN:
        return Classification(
            BUCKET_UNRESOLVED, None, "baseline capture failed for this key (unknown)"
        )
    if live.get("state") == STATE_UNKNOWN:
        return Classification(
            BUCKET_UNRESOLVED, None, "current state could not be read"
        )

    if recorded.get("state") == STATE_ABSENT:
        if live.get("state") == STATE_ABSENT:
            return Classification(BUCKET_PRESERVED, None, "never existed")
        return Classification(
            BUCKET_OWNED, ACTION_REMOVE, "absent at baseline, now present"
        )

    # recorded state is "present" from here on.
    if _values_equal(recorded, live):
        return Classification(BUCKET_PRESERVED, None, "unchanged since baseline")
    if live.get("state") == STATE_ABSENT:
        return Classification(
            BUCKET_DRIFTED,
            None,
            "present at baseline, now missing — removed outside this installer",
        )
    return Classification(
        BUCKET_UNRESOLVED,
        None,
        "changed since baseline — restore rule not yet implemented (step 4)",
    )


def reclassify_rc_file(
    recorded: dict[str, object] | None,
    baseline_content: bytes | None,
    live_content: bytes | None,
) -> Classification | None:
    """Override the generic classification for one rc file, if warranted.

    Handles the one rc-file case Implementation Sequence step 1/3 calls
    out by name: NVM/``_install_uv``/oh-my-posh only ever *append* lines to
    an rc file, so a live file whose content still starts with the exact
    baseline content is safely restorable by discarding everything after
    it — never a guess at which lines to strip. Any other kind of change
    (something inserted or edited within the original content) is left
    for the generic classifier's ``unresolved`` fallback.

    Returns:
        A replacement :class:`Classification`, or None to defer to
        :func:`classify_ownership_key`'s own result for this key.
    """
    if recorded is None or recorded.get("state") != STATE_PRESENT:
        return None
    if baseline_content is None:
        return Classification(
            BUCKET_UNRESOLVED, None, "recorded rc-file blob is missing or unreadable"
        )
    if live_content is None:
        return (
            None  # defer — the generic "present at baseline, now missing" rule applies
        )
    if live_content == baseline_content:
        return None  # unchanged — defer to the generic "preserved" result
    if live_content.startswith(baseline_content):
        return Classification(
            BUCKET_OWNED,
            ACTION_RESTORE,
            "appended to since baseline — restore original content",
        )
    return Classification(
        BUCKET_UNRESOLVED,
        None,
        "changed since baseline in a way that isn't a pure append",
    )


def reclassify_symlink_destination_pair(
    file_recorded: dict[str, object] | None,
    file_live: dict[str, object],
    symlink_recorded: dict[str, object] | None,
    symlink_live: dict[str, object],
) -> tuple[Classification, Classification] | None:
    """Jointly reclassify a destination's ``file:``/``symlink:`` key pair.

    Handles the named edge case from Implementation Sequence step 1:
    ``symlink()`` (``install.py:1301-1324``) takes no ``.bak`` and never
    calls ``record_symlink`` when the destination was already a symlink
    before install — so the only trace of "this was backed up and replaced"
    is the *baseline* recording ``file:<dest>`` present while the installer's
    new symlink now sits at that path. Classifying each key independently
    would otherwise leave both permanently ``unresolved`` (the file key sees
    baseline-present/live-absent → ``drifted``; the symlink key sees
    baseline-absent/live-present → ``owned``/remove) instead of restoring
    the original content.

    Returns:
        ``(file classification, symlink classification)`` when the pattern
        matches, else None to defer to the independent per-key results.
    """
    if not (
        file_recorded is not None
        and file_recorded.get("state") == STATE_PRESENT
        and file_live.get("state") == STATE_ABSENT
        and symlink_recorded is not None
        and symlink_recorded.get("state") == STATE_ABSENT
        and symlink_live.get("state") == STATE_PRESENT
    ):
        return None
    return (
        Classification(
            BUCKET_OWNED,
            ACTION_RESTORE,
            "backed up then symlinked over — restore the original content",
        ),
        Classification(
            BUCKET_OWNED,
            ACTION_REMOVE,
            "installer-created symlink over a backed-up original",
        ),
    )


# ── departure ledger ─────────────────────────────────────────────────────


def departure_ledger_path(state_dir: Path) -> Path:
    return state_dir / "departure.jsonl"


def _fsync_dir(directory: Path) -> None:
    fd = os.open(str(directory), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@dataclass
class DepartureLedger:
    """Crash-safe, fsynced-per-entry record of completed departure actions.

    Mirrors install.py's ``Manifest._append`` durability pattern: one JSON
    object per line, flushed and fsynced immediately after each action
    completes, so a retry can tell exactly which actions already finished
    without redoing — or worse, re-mutating — them.
    """

    path: Path

    def entries(self) -> list[dict[str, object]]:
        if not self.path.is_file():
            return []
        out: list[dict[str, object]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                out.append(parsed)
        return out

    def completed_keys(self) -> set[str]:
        """Every ownership key already recorded complete, regardless of outcome.

        A ledger-complete artifact is never mutated again — later changes
        surface as separate post-departure drift, not a re-attempt.
        """
        return {str(e["key"]) for e in self.entries() if "key" in e}

    def keys_with_outcome_prefix(self, prefix: str) -> set[str]:
        """Every key with at least one recorded outcome starting with ``prefix``.

        For a feature that wants a subset of its ledger-recorded keys to
        stay retryable across depart attempts -- unlike this class's
        default, where every recorded key is done for good regardless of
        outcome -- record a distinguishing, wording-independent outcome
        prefix (not a fragment of the user-facing message, which can be
        reworded independently), then use this at the call site to carve
        those keys back out of ``completed_keys()``'s exclusion.
        """
        return {
            str(e["key"])
            for e in self.entries()
            if "key" in e and str(e.get("outcome", "")).startswith(prefix)
        }

    def record(self, key: str, action: str, outcome: str) -> None:
        entry = {"key": key, "action": action, "outcome": outcome}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        is_new = not self.path.exists()
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if is_new:
            _fsync_dir(self.path.parent)

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


# ── advisory lock ─────────────────────────────────────────────────────────


def departure_lock_path(state_dir: Path) -> Path:
    return state_dir / "departure.lock"


@dataclass(frozen=True)
class LockInfo:
    pid: int
    start_time: int


def parse_lock_text(text: str) -> LockInfo | None:
    try:
        data = json.loads(text)
        return LockInfo(pid=int(data["pid"]), start_time=int(data["start_time"]))
    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
        return None


def read_lock(path: Path) -> LockInfo | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return parse_lock_text(text)


def process_start_time(pid: int) -> int | None:
    """Read a process's start-time field (field 22) from ``/proc/<pid>/stat``.

    Returns None if the process doesn't exist. Raises ``PermissionError``
    if ``/proc/<pid>/stat`` exists but can't be read — callers must not
    treat that as "dead"; see :func:`is_lock_holder_alive`.
    """
    try:
        text = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return None
    # Field 2 (comm) may itself contain spaces/parens; split after the last
    # ')' so the remaining whitespace-separated fields count correctly.
    _, _, rest = text.rpartition(")")
    fields = rest.split()
    try:
        return int(fields[19])  # field 3 is fields[0]; field 22 is fields[19]
    except (IndexError, ValueError):
        return None


def is_lock_holder_alive(
    pid: int,
    recorded_start_time: int,
    *,
    start_time_fn: Callable[[int], int | None] = process_start_time,
) -> bool:
    """Whether the recorded lock holder is still running the same process.

    A ``PermissionError`` while probing ``/proc`` is treated as alive — it
    can't be disproven, so the safe assumption is that it's still there. A
    live PID whose start time no longer matches the recorded one is a
    *different* process that reused the PID after a crash — stale, not
    alive; this is what makes that window detectable at all.
    """
    try:
        current_start = start_time_fn(pid)
    except PermissionError:
        return True
    if current_start is None:
        return False
    return current_start == recorded_start_time


def acquire_departure_lock(
    state_dir: Path,
    *,
    pid: int | None = None,
    start_time_fn: Callable[[int], int | None] = process_start_time,
) -> tuple[bool, LockInfo | None]:
    """Try to acquire the departure advisory lock.

    The lock's own content (recorded PID + start time) is the source of
    truth for staleness — an ``flock`` on the file only serializes the
    read-check-write sequence below between concurrent acquirers, so a
    genuinely live holder (recorded moments earlier by a concurrent
    process) is still seen and refused correctly.

    Returns:
        ``(True, None)`` on success. ``(False, stale_info_or_None)`` on
        refusal — the second element is the stale holder's info if one was
        found and logged, purely for a caller's warning message.
    """
    lock_path = departure_lock_path(state_dir)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            handle.seek(0)
            existing = parse_lock_text(handle.read())
            if existing is not None and is_lock_holder_alive(
                existing.pid, existing.start_time, start_time_fn=start_time_fn
            ):
                return False, None

            our_pid = pid if pid is not None else os.getpid()
            our_start = start_time_fn(our_pid) or 0
            handle.seek(0)
            handle.truncate()
            handle.write(json.dumps({"pid": our_pid, "start_time": our_start}))
            handle.flush()
            os.fsync(handle.fileno())
            return True, existing
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def release_departure_lock(state_dir: Path, pid: int | None = None) -> None:
    """Release and unlink the lock, only if it's still recorded as ours."""
    our_pid = pid if pid is not None else os.getpid()
    lock_path = departure_lock_path(state_dir)
    existing = read_lock(lock_path)
    if existing is not None and existing.pid == our_pid:
        lock_path.unlink(missing_ok=True)


# ── package classification (Implementation Sequence step 4) ─────────────

ACTION_DOWNGRADE = "downgrade"


@dataclass(frozen=True)
class PackageClassification:
    """One requested-or-introduced package's departure classification."""

    key: str
    manager: str
    name: str
    bucket: str
    action: str | None
    reason: str


def _classify_one_package(
    txn: Transaction, name: str, snapshot: dict[str, str] | None, *, requested: bool
) -> PackageClassification:
    key = package_key(txn.manager, name)
    if snapshot is None:
        return PackageClassification(
            key,
            txn.manager,
            name,
            BUCKET_UNRESOLVED,
            None,
            "current package state could not be probed",
        )
    if name not in snapshot:
        return PackageClassification(
            key, txn.manager, name, BUCKET_PRESERVED, None, "already absent"
        )
    if not requested:
        return PackageClassification(
            key,
            txn.manager,
            name,
            BUCKET_OWNED,
            ACTION_REMOVE,
            "introduced as a dependency by this transaction",
        )
    if name in txn.before:
        if txn.before[name] == txn.after.get(name):
            return PackageClassification(
                key,
                txn.manager,
                name,
                BUCKET_PRESERVED,
                None,
                "already installed, unchanged by this transaction",
            )
        return PackageClassification(
            key,
            txn.manager,
            name,
            BUCKET_OWNED,
            ACTION_DOWNGRADE,
            "already installed before this transaction upgraded it",
        )
    return PackageClassification(
        key,
        txn.manager,
        name,
        BUCKET_OWNED,
        ACTION_REMOVE,
        "installed fresh by this transaction",
    )


def classify_package_transactions(
    baseline: Baseline, live_snapshots: dict[str, dict[str, str] | None]
) -> list[PackageClassification]:
    """Classify every requested/introduced package, in departure removal order.

    Walks ``baseline.transactions`` in reverse (the plan's pinned removal
    order — never "reverse ledger/installation order", since the ledger
    starts empty and ``history.jsonl`` is off-limits to ``--depart``) and
    yields one classification per distinct ``(manager, name)`` the first
    time it's encountered walking backwards — a package touched by more
    than one transaction across repeated installs is classified from its
    *most recent* transaction only.

    Args:
        baseline: The departure baseline, with its recorded transactions.
        live_snapshots: Fresh ``{manager: {name: version} | None}`` probe
            results — ``None`` for a manager whose probe failed.
    """
    results: list[PackageClassification] = []
    seen: set[str] = set()
    for txn_dict in reversed(baseline.transactions):
        txn = transaction_from_dict(txn_dict)
        snapshot = live_snapshots.get(txn.manager)
        for name in txn.requested:
            key = package_key(txn.manager, name)
            if key in seen:
                continue
            seen.add(key)
            results.append(_classify_one_package(txn, name, snapshot, requested=True))
        for name in txn.introduced():
            key = package_key(txn.manager, name)
            if key in seen:
                continue
            seen.add(key)
            results.append(_classify_one_package(txn, name, snapshot, requested=False))
    return results


# ── service/linger ──────────────────────────────────────────────────────

ACTION_DISABLE = "disable"


def build_service_record(
    *, enabled: bool | None, active: bool | None, linger: bool | None
) -> dict[str, object]:
    """Build a ``service:`` record from already-probed values.

    Any probe returning None (unavailable/unanswerable) makes the whole
    record ``unknown`` — matching the tri-state's "a capture error always
    records unknown" rule; there's no meaningful partial state here.
    """
    if enabled is None or active is None or linger is None:
        return {"state": STATE_UNKNOWN}
    return {
        "state": STATE_PRESENT,
        "enabled": enabled,
        "active": active,
        "linger": linger,
    }


def classify_service(
    recorded: dict[str, object] | None, live: dict[str, object]
) -> Classification:
    """Classify the watchcommit service/linger key.

    Conservative, same principle as :func:`classify_ownership_key`: the
    only ``owned`` case is baseline recording it disabled and live showing
    it now enabled (this installer's own ``enable_watchcommit_service``
    unconditionally enabling it) — any other kind of change (someone
    toggled it independently, baseline was already enabled and something
    about it still shifted) is left ``drifted`` rather than guessed at.
    """
    if recorded is None or recorded.get("state") == STATE_UNKNOWN:
        return Classification(
            BUCKET_UNRESOLVED, None, "no reliable baseline for this service"
        )
    if live.get("state") == STATE_UNKNOWN:
        return Classification(
            BUCKET_UNRESOLVED, None, "current service state could not be read"
        )
    if recorded.get("enabled") == live.get("enabled") and recorded.get(
        "active"
    ) == live.get("active"):
        return Classification(BUCKET_PRESERVED, None, "unchanged since baseline")
    if recorded.get("enabled") is False and live.get("enabled") is True:
        return Classification(
            BUCKET_OWNED,
            ACTION_DISABLE,
            "enabled by this installer, was disabled at baseline",
        )
    return Classification(
        BUCKET_DRIFTED,
        None,
        "service state changed since baseline in an unexpected way",
    )


# ── global git config (core.hooksPath) ────────────────────────────────────


def build_gitconfig_record(value: str | None) -> dict[str, object]:
    """Build a ``gitconfig:`` record from an already-read global config value."""
    if value is None:
        return {"state": STATE_ABSENT}
    return {"state": STATE_PRESENT, "value": value}


def classify_gitconfig(
    recorded: dict[str, object] | None, live: dict[str, object], managed_value: str
) -> Classification:
    """Classify a single global git config key this installer manages.

    Same conservative principle as :func:`classify_service`: the only
    ``owned`` case is baseline recording something other than
    ``managed_value`` (or absent) and live now showing exactly
    ``managed_value`` -- this installer's own
    ``install_global_git_hooks_path`` having set it. Anything else (someone
    else set the same value independently, or the value drifted to a third
    value outside this installer) is left ``drifted``/``unresolved`` rather
    than guessed at.
    """
    if recorded is None:
        return Classification(
            BUCKET_UNRESOLVED, None, "no baseline record for this key"
        )
    if live.get("state") == STATE_UNKNOWN:
        return Classification(
            BUCKET_UNRESOLVED, None, "current value could not be read"
        )
    if _values_equal(recorded, live):
        return Classification(BUCKET_PRESERVED, None, "unchanged since baseline")
    if live.get("state") == STATE_PRESENT and live.get("value") == managed_value:
        return Classification(
            BUCKET_OWNED,
            ACTION_RESTORE,
            "set by this installer, differed at baseline",
        )
    return Classification(
        BUCKET_DRIFTED,
        None,
        "changed since baseline, not to this installer's managed value",
    )
