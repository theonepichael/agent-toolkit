#!/usr/bin/env python3
"""Reversible rewrite of stored toolkit-data paths, with dependent hashes.

Part of the toolkit-home migration; installer-only, never installed. The
stage step runs it on staged copies of the stores while it holds the
migration lock exclusively.

What it rewrites
----------------
Absolute (or ``~/``) references to a moved toolkit data root, in these named
fields only: ``related_files[].path`` on work and pending items,
``source_ref.*`` on pending items, ``plan_path`` on decision sessions,
``[].related_files[].path`` in ticket batches, and ``git_repos[]`` in the
standup config. Everything else that mentions an old root — prose fields,
markdown, instruction files — is reported by :func:`stale_report`, never
rewritten. Checks run on the raw string: a value with ``.``, ``..`` or empty
segments is *ambiguous* and never rewritten; a file domain matches only
itself.

Dependent hashes
----------------
Two stored hashes cover rewritten fields: an item's ``review_content_hash``
(``dev_status_mutation._content_hash``) and a ticket batch's ``batch_hash``
(sha256 of the batch bytes, in ``<batch>.state.json``). Each is recomputed
only when it matched the content before the rewrite. A hash that was already
stale is left alone and reported; one that was stale before but would match
the rewritten content is a :class:`TransformError`, because the rewrite would
silently turn it valid.

Crash safety
------------
:func:`save_plan` stores the exact before and after bytes of every changed
file plus a manifest (the commit point). :func:`apply_saved` and
:func:`rollback_saved` work from that saved plan alone, verify it against the
manifest digest the journal recorded, refuse if any target is neither at its
before nor its after digest, and replace whole files atomically one by one.
Both are idempotent, so after a crash at any boundary either one brings every
file to one side. This is recoverable, not atomic across files.

Requires Python 3.12+. Standard library only.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import sys
from collections.abc import Callable, Iterator, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

sys.path.insert(0, str(Path(__file__).resolve().parent / "agent-scripts"))

import agent_toolkit_paths  # noqa: E402 — sibling dir inserted above
import dev_status_mutation  # noqa: E402 — sibling dir inserted above
import fault_checkpoint  # noqa: E402 — sibling dir inserted above

import migrate_toolkit_home  # noqa: E402 — sibling module next to this file

FILE_DOMAINS = frozenset({"guard-rail-log", "backend-log"})
MANIFEST_VERSION = 1
MANIFEST = "manifest.json"
TMP_SUFFIX = ".transform-tmp"

Kind = Literal["rewrite", "untouched", "ambiguous", "unmatched-structured"]


class TransformError(RuntimeError):
    """The transform cannot proceed safely; nothing was written."""


# ── roots and classification ─────────────────────────────────────────────────


@dataclass(frozen=True)
class Classification:
    kind: Kind
    new: str | None = None


@dataclass(frozen=True)
class RootMap:
    home: Path
    dirs: tuple[tuple[Path, Path], ...]
    files: tuple[tuple[Path, Path], ...]

    @classmethod
    def for_home(
        cls,
        home: Path,
        *,
        carried_dirs: Sequence[tuple[Path, Path]] = (),
        carried_files: Sequence[tuple[Path, Path]] = (),
    ) -> RootMap:
        dirs: list[tuple[Path, Path]] = []
        files: list[tuple[Path, Path]] = []
        for domain in agent_toolkit_paths.DOMAINS:
            pair = (
                agent_toolkit_paths.layout_path(home, domain, "legacy"),
                agent_toolkit_paths.layout_path(home, domain, "toolkit-home"),
            )
            (files if domain in FILE_DOMAINS else dirs).append(pair)
        dirs.extend(carried_dirs)
        files.extend(carried_files)
        return cls(home=home, dirs=tuple(dirs), files=tuple(files))

    def _old_roots(self) -> list[str]:
        return [str(old) for old, _ in (*self.dirs, *self.files)]

    def classify(self, value: object) -> Classification:
        """Decide what to do with one stored string, from its raw text."""
        if not isinstance(value, str):
            return Classification("untouched")
        home = str(self.home)
        if value.startswith("~/"):
            tilde, expanded = True, home + value[1:]
        elif value.startswith("/"):
            tilde, expanded = False, value
        else:
            return Classification("untouched")
        trailing = len(expanded) > 1 and expanded.endswith("/")
        core = expanded[:-1] if trailing else expanded

        def names(root: str) -> bool:
            return core == root or core.startswith(root + "/")

        if not any(names(root) for root in self._old_roots()):
            return Classification("untouched")
        if any(seg in ("", ".", "..") for seg in core.split("/")[1:]):
            return Classification("ambiguous")
        for old, new in self.files:
            if core == str(old) and not trailing:
                return Classification("rewrite", self._form(str(new), tilde))
            if names(str(old)):
                return Classification("unmatched-structured")
        best: tuple[str, str] | None = None
        for old, new in self.dirs:
            if names(str(old)) and (best is None or len(str(old)) > len(best[0])):
                best = (str(old), str(new))
        if best is None:
            return Classification("untouched")
        rewritten = best[1] + core[len(best[0]) :] + ("/" if trailing else "")
        return Classification("rewrite", self._form(rewritten, tilde))

    def _form(self, path: str, tilde: bool) -> str:
        home = str(self.home)
        if tilde and (path == home or path.startswith(home + "/")):
            return "~" + path[len(home) :]
        return path

    def mentions(self, text: str) -> list[str]:
        """Old-root references inside free text (absolute or ``~/`` form)."""
        found: list[str] = []
        home = str(self.home)
        for root in self._old_roots():
            forms = [root]
            if root.startswith(home + "/"):
                forms.append("~" + root[len(home) :])
            for form in forms:
                start = 0
                while (at := text.find(form, start)) != -1:
                    end = at + len(form)
                    if end == len(text) or not (
                        text[end].isalnum() or text[end] in "-_."
                    ):
                        stop = end
                        while (
                            stop < len(text)
                            and not text[stop].isspace()
                            and text[stop] not in "\"'`()<>,;"
                        ):
                            stop += 1
                        found.append(text[at:stop])
                    start = end
        return found


# ── records and plan ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class PathChange:
    file: str
    pointer: str
    old: str
    new: str


@dataclass(frozen=True)
class HashChange:
    file: str
    pointer: str
    old: str
    new: str
    kind: str


@dataclass(frozen=True)
class StaleRef:
    file: str
    pointer: str
    value: str
    reason: str


@dataclass(frozen=True)
class Skipped:
    file: str
    pointer: str
    kind: str
    reason: str


@dataclass(frozen=True)
class FileChange:
    path: Path
    before: bytes
    after: bytes


@dataclass
class StoreFiles:
    root: Path
    items: Path | None = None
    pending: Path | None = None
    grill_dir: Path | None = None
    batches_dir: Path | None = None
    standup_config: Path | None = None


@dataclass
class TransformPlan:
    root: Path
    changes: list[FileChange] = field(default_factory=list)
    paths: list[PathChange] = field(default_factory=list)
    hashes: list[HashChange] = field(default_factory=list)
    stale: list[StaleRef] = field(default_factory=list)
    skipped: list[Skipped] = field(default_factory=list)


# ── filesystem safety ────────────────────────────────────────────────────────


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _check_under_root(root: Path, path: Path) -> None:
    """``path`` sits lexically under ``root`` and no component below root is a link."""
    try:
        rel = Path(os.path.abspath(path)).relative_to(os.path.abspath(root))
    except ValueError:
        raise TransformError(f"{path} is outside the staging root {root}") from None
    if ".." in rel.parts:
        raise TransformError(f"{path} escapes the staging root {root}")
    probe = Path(os.path.abspath(root))
    for part in rel.parts:
        probe = probe / part
        try:
            if probe.is_symlink():
                raise TransformError(f"{probe} is a symlink inside the staging root")
        except OSError as exc:
            raise TransformError(f"cannot inspect {probe}: {exc}") from exc


def _read_json(path: Path) -> tuple[bytes, object]:
    raw = path.read_bytes()
    try:
        return raw, json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TransformError(f"invalid JSON in {path}: {exc}") from exc


def _walk_files(directory: Path, skipped: list[Skipped]) -> Iterator[Path]:
    """Regular files under ``directory``, never following links (links are reported)."""
    for current, dirnames, filenames in os.walk(directory, followlinks=False):
        base = Path(current)
        for name in sorted(dirnames):
            if (base / name).is_symlink():
                skipped.append(Skipped(str(base / name), "", "discovery", "symlink"))
        for name in sorted(filenames):
            path = base / name
            if path.is_symlink():
                skipped.append(Skipped(str(path), "", "discovery", "symlink"))
                continue
            yield path


# ── planning ─────────────────────────────────────────────────────────────────


class _Planner:
    def __init__(self, stores: StoreFiles, roots: RootMap) -> None:
        self.stores = stores
        self.roots = roots
        self.plan = TransformPlan(root=stores.root)

    # A rewrite of one structured string. Returns the new value, or None.
    def _field(self, file: Path, pointer: str, value: object) -> str | None:
        got = self.roots.classify(value)
        if got.kind == "rewrite" and got.new is not None and got.new != value:
            self.plan.paths.append(PathChange(str(file), pointer, str(value), got.new))
            return got.new
        if got.kind in ("ambiguous", "unmatched-structured"):
            self.plan.stale.append(StaleRef(str(file), pointer, str(value), got.kind))
        return None

    def _prose(self, file: Path, pointer: str, value: object) -> None:
        if isinstance(value, str):
            for match in self.roots.mentions(value):
                self.plan.stale.append(StaleRef(str(file), pointer, match, "prose"))
        elif isinstance(value, dict):
            for key, child in value.items():
                self._prose(file, f"{pointer}/{key}", child)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                self._prose(file, f"{pointer}/{index}", child)

    def _related_files(self, file: Path, pointer: str, entries: object) -> bool:
        changed = False
        if not isinstance(entries, list):
            return False
        for index, entry in enumerate(entries):
            where = f"{pointer}/{index}"
            if not isinstance(entry, dict):
                self._prose(file, where, entry)
                continue
            for key, value in entry.items():
                if key == "path":
                    new = self._field(file, f"{where}/path", value)
                    if new is not None:
                        entry["path"] = new
                        changed = True
                else:
                    self._prose(file, f"{where}/{key}", value)
        return changed

    def _envelope(
        self, path: Path
    ) -> tuple[bytes, dict[str, object], list[dict[str, object]]]:
        _check_under_root(self.stores.root, path)
        raw, data = _read_json(path)
        if not isinstance(data, dict) or list(data) != ["schema_version", "items"]:
            raise TransformError(
                f"{path}: unsupported envelope (expected exactly schema_version, items)"
            )
        items = data["items"]
        if not isinstance(items, list) or not all(isinstance(i, dict) for i in items):
            raise TransformError(f"{path}: items must be a list of objects")
        return raw, data, items  # type: ignore[return-value]

    def _store(self, path: Path, *, pending: bool) -> FileChange | None:
        raw, data, items = self._envelope(path)
        any_change = False
        for index, item in enumerate(items):
            before_item = copy.deepcopy(item)
            where = f"/items/{index}"
            changed = self._related_files(
                path, f"{where}/related_files", item.get("related_files")
            )
            for key, value in item.items():
                if key == "related_files":
                    continue
                if pending and key == "source_ref" and isinstance(value, dict):
                    for ref_key, ref in value.items():
                        new = self._field(path, f"{where}/source_ref/{ref_key}", ref)
                        if new is not None:
                            value[ref_key] = new
                            changed = True
                        elif not isinstance(ref, str):
                            self._prose(path, f"{where}/source_ref/{ref_key}", ref)
                    continue
                self._prose(path, f"{where}/{key}", value)
            if changed and not pending and "review_content_hash" in item:
                self._review_hash(path, where, before_item, item)
            any_change = any_change or changed
        if not any_change:
            return None
        after = json.dumps(
            {"schema_version": data["schema_version"], "items": items}, indent=2
        ).encode()
        return FileChange(path, raw, after)

    def _review_hash(
        self,
        path: Path,
        where: str,
        before: dict[str, object],
        after: dict[str, object],
    ) -> None:
        stored = after.get("review_content_hash")
        old = dev_status_mutation._content_hash(before)  # type: ignore[arg-type]
        new = dev_status_mutation._content_hash(after)  # type: ignore[arg-type]
        pointer = f"{where}/review_content_hash"
        if stored == old:
            after["review_content_hash"] = new
            self.plan.hashes.append(
                HashChange(str(path), pointer, old, new, "review_content_hash")
            )
        elif stored == new:
            raise TransformError(
                f"{path}{pointer}: a stale review hash would become valid after the "
                "rewrite; resolve the review before migrating"
            )
        else:
            self.plan.skipped.append(
                Skipped(
                    str(path), pointer, "review_content_hash", "stale before transform"
                )
            )

    def _sessions(self) -> list[FileChange]:
        directory = self.stores.grill_dir
        if directory is None or not directory.is_dir():
            return []
        _check_under_root(self.stores.root, directory)
        changes: list[FileChange] = []
        for path in sorted(directory.iterdir()):
            if path.suffix != ".json":
                continue
            if path.is_symlink():
                self.plan.skipped.append(Skipped(str(path), "", "discovery", "symlink"))
                continue
            if not path.is_file():
                continue
            raw, data = _read_json(path)
            if not isinstance(data, dict):
                continue
            changed = False
            for key, value in data.items():
                if key == "plan_path":
                    new = self._field(path, "/plan_path", value)
                    if new is not None:
                        data["plan_path"] = new
                        changed = True
                else:
                    self._prose(path, f"/{key}", value)
            if changed:
                changes.append(
                    FileChange(path, raw, json.dumps(data, indent=2).encode())
                )
        return changes

    def _batches(self) -> tuple[list[FileChange], list[FileChange]]:
        directory = self.stores.batches_dir
        if directory is None or not directory.is_dir():
            return [], []
        _check_under_root(self.stores.root, directory)
        batches: list[FileChange] = []
        states: list[FileChange] = []
        for path in _walk_files(directory, self.plan.skipped):
            if path.suffix != ".json" or path.name.endswith(".state.json"):
                continue
            raw, data = _read_json(path)
            if not isinstance(data, list) or not data:
                continue
            changed = False
            for index, ticket in enumerate(data):
                if not isinstance(ticket, dict):
                    continue
                for key, value in ticket.items():
                    if key == "related_files":
                        changed |= self._related_files(
                            path, f"/{index}/related_files", value
                        )
                    else:
                        self._prose(path, f"/{index}/{key}", value)
            if not changed:
                continue
            after = json.dumps(data, indent=2).encode()
            batches.append(FileChange(path, raw, after))
            state = self._batch_state(path, raw, after)
            if state is not None:
                states.append(state)
        return batches, states

    def _batch_state(
        self, batch: Path, before: bytes, after: bytes
    ) -> FileChange | None:
        state_path = batch.with_suffix(".state.json")
        if not state_path.exists():
            return None
        if state_path.is_symlink():
            raise TransformError(f"{state_path} is a symlink inside the staging root")
        raw, state = _read_json(state_path)
        if not isinstance(state, dict) or not isinstance(state.get("batch_hash"), str):
            raise TransformError(
                f"{state_path}: expected an object with a string batch_hash"
            )
        stored, old, new = state["batch_hash"], _sha(before), _sha(after)
        if stored == old:
            state["batch_hash"] = new
            self.plan.hashes.append(
                HashChange(str(state_path), "/batch_hash", old, new, "batch_hash")
            )
            return FileChange(state_path, raw, json.dumps(state, indent=2).encode())
        if stored == new:
            raise TransformError(
                f"{state_path}: a stale batch hash would become valid after the rewrite"
            )
        self.plan.skipped.append(
            Skipped(
                str(state_path), "/batch_hash", "batch_hash", "stale before transform"
            )
        )
        return None

    def _standup(self) -> list[FileChange]:
        path = self.stores.standup_config
        if path is None or not path.exists():
            return []
        _check_under_root(self.stores.root, path)
        raw, data = _read_json(path)
        if not isinstance(data, dict):
            return []
        changed = False
        for key, value in data.items():
            if key == "git_repos" and isinstance(value, list):
                for index, repo in enumerate(value):
                    new = self._field(path, f"/git_repos/{index}", repo)
                    if new is not None:
                        value[index] = new
                        changed = True
            else:
                self._prose(path, f"/{key}", value)
        if not changed:
            return []
        return [FileChange(path, raw, (json.dumps(data, indent=2) + "\n").encode())]

    def _text_files(self, extra_files: Sequence[Path]) -> None:
        sources: list[tuple[Path, str]] = []
        for directory in (
            self.stores.grill_dir,
            self.stores.standup_config and self.stores.standup_config.parent,
        ):
            if directory is not None and Path(directory).is_dir():
                sources += [
                    (p, "markdown")
                    for p in _walk_files(Path(directory), self.plan.skipped)
                    if p.suffix == ".md"
                ]
        sources += [(Path(p), "extra-file") for p in extra_files]
        for path, reason in sources:
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for number, line in enumerate(lines, start=1):
                for match in self.roots.mentions(line):
                    self.plan.stale.append(
                        StaleRef(str(path), f"line:{number}", match, reason)
                    )

    def run(self, extra_files: Sequence[Path]) -> TransformPlan:
        batches, states = self._batches()
        sessions = self._sessions()
        standup = self._standup()
        pending = (
            self._store(self.stores.pending, pending=True)
            if self.stores.pending
            else None
        )
        items = (
            self._store(self.stores.items, pending=False) if self.stores.items else None
        )
        self.plan.changes = [
            *batches,
            *states,
            *sessions,
            *standup,
            *([pending] if pending else []),
            *([items] if items else []),
        ]
        self._text_files(extra_files)
        return self.plan


def plan_transform(
    stores: StoreFiles, roots: RootMap, extra_files: Sequence[Path] = ()
) -> TransformPlan:
    """Compute every rewrite and dependent hash change. Writes nothing."""
    for path in (stores.items, stores.pending):
        if path is not None and path.exists():
            _check_under_root(stores.root, path)
    return _Planner(stores, roots).run(extra_files)


def stale_report(
    stores: StoreFiles, roots: RootMap, extra_files: Sequence[Path] = ()
) -> list[StaleRef]:
    """Every old-root reference the transform leaves in place."""
    return plan_transform(stores, roots, extra_files).stale


TEXT_SUFFIXES = frozenset({".md", ".json", ".jsonl", ".txt"})


def mentions_in_tree(
    root: Path, roots: RootMap, *, legacy: Path | None = None
) -> list[dict[str, str]]:
    """Old-root mentions in every TEXT file under ``root`` (md/json/jsonl/txt).

    The carry step's prose scan: carried trees are reported, never rewritten,
    and the report is text-only by design (binary files are skipped). An
    UNREADABLE text file is reported as its own record, never silently
    dropped. Each record names both the file's path under the scanned copy
    and — when ``legacy`` is given — the legacy source path the file returns
    to on undo, so the reference stays findable after an abort or recovery.
    """
    records: list[dict[str, str]] = []
    stack = [root]
    while stack:
        current = stack.pop()
        if not current.is_dir() or current.is_symlink():
            continue
        for child in sorted(current.iterdir()):
            if child.is_dir() and not child.is_symlink():
                stack.append(child)
                continue
            if child.suffix not in TEXT_SUFFIXES or child.is_symlink():
                continue
            try:
                lines = child.read_text(encoding="utf-8", errors="strict").splitlines()
            except (OSError, UnicodeDecodeError) as exc:
                records.append(
                    {
                        "file": str(child),
                        "line": "0",
                        "value": f"unreadable ({type(exc).__name__}: {exc})",
                        "kind": "unreadable",
                    }
                )
                continue
            for number, line in enumerate(lines, start=1):
                for match in roots.mentions(line):
                    record = {
                        "file": str(child),
                        "line": str(number),
                        "value": match,
                    }
                    if legacy is not None:
                        record["source"] = str(legacy / child.relative_to(root))
                    records.append(record)
    return records


# ── saved plan ───────────────────────────────────────────────────────────────


def _write_new(path: Path, data: bytes, mode: int = 0o444) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        migrate_toolkit_home._write_all(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def save_plan(plan: TransformPlan, work_dir: Path) -> tuple[Path, str]:
    """Persist a plan's exact bytes and manifest. Returns (plan dir, manifest sha256)."""
    plan_dir = work_dir / "transform"
    if os.path.lexists(plan_dir):
        raise TransformError(f"{plan_dir} already exists; a saved plan is never reused")
    entries: list[dict[str, object]] = []
    seen: set[str] = set()
    for index, change in enumerate(plan.changes):
        _check_under_root(plan.root, change.path)
        rel = str(
            Path(os.path.abspath(change.path)).relative_to(os.path.abspath(plan.root))
        )
        if rel in seen:
            raise TransformError(f"{rel} is listed twice")
        seen.add(rel)
        entries.append(
            {
                "index": index,
                "path": rel,
                "before_sha256": _sha(change.before),
                "after_sha256": _sha(change.after),
            }
        )
    migrate_toolkit_home._mkdir_durable(plan_dir)
    for sub in ("before", "after"):
        migrate_toolkit_home._mkdir_durable(plan_dir / sub)
    for index, change in enumerate(plan.changes):
        _write_new(plan_dir / "before" / str(index), change.before)
        _write_new(plan_dir / "after" / str(index), change.after)
    migrate_toolkit_home._fsync_dir(plan_dir / "before")
    migrate_toolkit_home._fsync_dir(plan_dir / "after")
    fault_checkpoint.checkpoint("transform.save.snapshots-written")
    manifest = {
        "version": MANIFEST_VERSION,
        "staging_root": os.path.abspath(plan.root),
        "files": entries,
        "paths": [asdict(p) for p in plan.paths],
        "hashes": [asdict(h) for h in plan.hashes],
        "skipped": [asdict(s) for s in plan.skipped],
    }
    payload = (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()
    tmp = plan_dir / f"{MANIFEST}{TMP_SUFFIX}"
    _write_new(tmp, payload)
    os.replace(tmp, plan_dir / MANIFEST)
    migrate_toolkit_home._fsync_dir(plan_dir)
    return plan_dir, _sha(payload)


@dataclass(frozen=True)
class _Entry:
    index: int
    target: Path
    before: bytes
    after: bytes
    before_sha: str
    after_sha: str


def _parse_manifest(raw: bytes) -> tuple[Path, list[dict[str, object]]]:
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise TransformError(f"unreadable transform manifest: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("version") != MANIFEST_VERSION:
        raise TransformError("unsupported transform manifest version")
    root = Path(str(manifest.get("staging_root", "")))
    if not root.is_absolute():
        raise TransformError("transform manifest has no absolute staging root")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise TransformError("transform manifest has no file list")
    seen: set[str] = set()
    for entry in files:
        rel = str(entry.get("path", "")) if isinstance(entry, dict) else ""
        parts = Path(rel).parts
        if not rel or Path(rel).is_absolute() or ".." in parts or rel in seen:
            raise TransformError(f"bad transform target {rel!r}")
        seen.add(rel)
    return root, files


def _load(plan_dir: Path, manifest_sha256: str) -> list[_Entry]:
    """Verify the manifest against the journalled digest and every snapshot against it."""
    try:
        raw = (plan_dir / MANIFEST).read_bytes()
    except OSError as exc:
        raise TransformError(f"cannot read {plan_dir / MANIFEST}: {exc}") from exc
    if _sha(raw) != manifest_sha256:
        raise TransformError(
            f"{plan_dir / MANIFEST} does not match the journalled digest"
        )
    root, files = _parse_manifest(raw)
    entries: list[_Entry] = []
    for entry in files:
        index = int(entry["index"])
        before = (plan_dir / "before" / str(index)).read_bytes()
        after = (plan_dir / "after" / str(index)).read_bytes()
        if (
            _sha(before) != entry["before_sha256"]
            or _sha(after) != entry["after_sha256"]
        ):
            raise TransformError(f"saved snapshot {index} does not match the manifest")
        target = root / str(entry["path"])
        _check_under_root(root, target)
        entries.append(
            _Entry(
                index,
                target,
                before,
                after,
                str(entry["before_sha256"]),
                str(entry["after_sha256"]),
            )
        )
    return entries


def _current(path: Path) -> str | None:
    try:
        return _sha(path.read_bytes())
    except FileNotFoundError:
        return None


def _replace(target: Path, data: bytes) -> None:
    tmp = target.with_name(f".{target.name}{TMP_SUFFIX}")
    tmp.unlink(missing_ok=True)
    mode = target.stat().st_mode & 0o777 if target.exists() else 0o644
    _write_new(tmp, data, mode)
    os.replace(tmp, target)
    migrate_toolkit_home._fsync_dir(target.parent)


def _move(
    plan_dir: Path,
    manifest_sha256: str,
    *,
    source: Callable[[_Entry], str],
    target_bytes: Callable[[_Entry], bytes],
    target_sha: Callable[[_Entry], str],
    label: str,
) -> int:
    entries = _load(plan_dir, manifest_sha256)
    for entry in entries:
        state = _current(entry.target)
        if state not in (entry.before_sha, entry.after_sha):
            raise TransformError(
                f"{entry.target} changed outside the transform; refusing to {label}"
            )
    for entry in entries:
        entry.target.with_name(f".{entry.target.name}{TMP_SUFFIX}").unlink(
            missing_ok=True
        )
    replaced = 0
    for entry in entries:
        if _current(entry.target) == target_sha(entry):
            continue
        if _current(entry.target) != source(entry):
            raise TransformError(f"{entry.target} changed during the {label}")
        _replace(entry.target, target_bytes(entry))
        replaced += 1
        fault_checkpoint.checkpoint(f"transform.{label}.{entry.index}")
    return replaced


def apply_saved(plan_dir: Path, manifest_sha256: str) -> int:
    """Bring every target to its after bytes. Idempotent. Returns files replaced."""
    return _move(
        plan_dir,
        manifest_sha256,
        source=lambda e: e.before_sha,
        target_bytes=lambda e: e.after,
        target_sha=lambda e: e.after_sha,
        label="apply",
    )


def rollback_saved(plan_dir: Path, manifest_sha256: str) -> int:
    """Bring every target back to its before bytes. Idempotent. Returns files replaced."""
    return _move(
        plan_dir,
        manifest_sha256,
        source=lambda e: e.after_sha,
        target_bytes=lambda e: e.before,
        target_sha=lambda e: e.before_sha,
        label="rollback",
    )


def discard_plan(plan_dir: Path) -> None:
    """Remove a saved plan that was never applied."""
    manifest = plan_dir / MANIFEST
    if manifest.exists():
        root, files = _parse_manifest(manifest.read_bytes())
        for entry in files:
            target = root / str(entry["path"])
            if _current(target) != entry["before_sha256"]:
                raise TransformError(
                    f"{target} is not at its pre-transform content; roll back instead"
                )
    shutil.rmtree(plan_dir)
