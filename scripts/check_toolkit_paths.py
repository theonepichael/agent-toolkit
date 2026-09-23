#!/usr/bin/env python3
"""Repository checks for the toolkit-home migration: path ownership and writer inventory.

ownership   Classify every ``.claude`` path reference in the tracked files as
            toolkit (moves in the cutover), harness (the Claude harness's own
            installation, never moves), or foreign (origin-repo scripts
            symlinked into ~/.claude/scripts, never moved or flagged). A
            reference no rule covers fails. References are found three ways:
            plain text, Python ``Path`` constructions (AST), and TypeScript /
            JavaScript ``join(...)`` calls.
inventory   Check that every entry links.toml installs has a declared kind with
            a checked obligation: Python modules declare ``TOOLKIT_DATA`` (and
            optionally ``TOOLKIT_DATA_VIA``); TypeScript / JavaScript files need a
            row in ``NON_PYTHON``; everything else is an asset covered by the
            ownership check. A new entry with no kind fails.
generators  Check the generator inventory (``GENERATORS``): every file a
            generator emits is claimed by exactly one row, every declared
            output is tracked, and no generated output still names a
            toolkit-owned path under ``.claude``.

The ``TOOLKIT_DATA`` markers are declarations and these rules are a tripwire,
not proof of lock coverage: the execution tests in
test/test_migration_lock_adoption.py are the proof, and every ``writer`` must
be named there.

Usage
  check_toolkit_paths.py ownership [--report]
  check_toolkit_paths.py inventory [--report]
  check_toolkit_paths.py generators [--report]

Exit codes
  0 everything classified and every obligation holds; 1 problems (listed on
  stdout, one per line).
"""

from __future__ import annotations

import argparse
import ast
import importlib
import re
import subprocess
import sys
import tomllib
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

REPO = Path(__file__).resolve().parent.parent

FOREIGN_SCRIPTS = frozenset(
    {
        "dev_status_sync.py",
        "watchcommit_activity.py",
        "opencode_skills_sync_activity.py",
        "gen_core_instructions.py",
    }
)
TOOLKIT_DATA_ENTRIES = frozenset(
    {
        "backlog",
        "backlog-out-of-scope",
        "grill",
        "to-tickets",
        "standup",
        "guard_rails_audit.jsonl",
        "backend_calls.jsonl",
        "toolkit_state.json",
        "toolkit_sync.json",
    }
)
TOOLKIT_TOP = frozenset({"scripts", "hooks", "icons"})
HARNESS_TOP = frozenset(
    {
        "commands",
        "skills",
        "agents",
        "output-styles",
        "projects",
        "daemon",
        "plugins",
        "CLAUDE.md",
        "settings.json",
        "settings.local.json",
        "keybindings.json",
        "statsig",
        "todos",
        "shell-snapshots",
        "ide",
    }
)
CLASSES = ("toolkit", "harness", "foreign")
SKIP_FILES = frozenset({"uv.lock", "pi/package-lock.json", "INTERFACES.md"})
TEST_TREES = ("test/", "pi/test/", "agent-scripts/test_")
MARKER_RE = re.compile(r"path-ownership:\s*(toolkit|harness|foreign)\b")

# ── TypeScript / JavaScript files installed by links.toml ────────────────────
# status: "no-toolkit-data" (never touches toolkit data paths) or "unlocked"
# (touches them without the migration lock; the reason says why). This table
# is the checkout-local record the migration plan's risk register refers to.
NON_PYTHON: dict[str, tuple[str, str]] = {
    "agy/hooks/agy-elapsed.js": (
        "no-toolkit-data",
        "state file lives in the OS temp directory",
    ),
    "opencode/plugin/guard-rails.ts": (
        "no-toolkit-data",
        "calls the locked Python CLI; writes no toolkit data itself",
    ),
    "opencode/plugin/notify.ts": (
        "no-toolkit-data",
        "calls the locked Python CLI; writes no toolkit data itself",
    ),
    "opencode/plugin/ruff-format-on-edit.ts": (
        "no-toolkit-data",
        "touches no toolkit data paths",
    ),
    "pi/extensions/compaction-backlog-sync.ts": (
        "no-toolkit-data",
        "calls the locked Python CLI; writes no toolkit data itself",
    ),
    "pi/extensions/custom-footer.ts": (
        "no-toolkit-data",
        "calls the locked Python CLI; writes no toolkit data itself",
    ),
    "pi/extensions/cwd.ts": (
        "no-toolkit-data",
        "touches no toolkit data paths",
    ),
    "pi/extensions/delegate-tool.ts": (
        "no-toolkit-data",
        "writes only a log in a fresh OS temp directory",
    ),
    "pi/extensions/dev-status-tool.ts": (
        "no-toolkit-data",
        "calls the locked Python CLI; writes no toolkit data itself",
    ),
    "pi/extensions/exit-alias.ts": (
        "no-toolkit-data",
        "touches no toolkit data paths",
    ),
    "pi/extensions/fatal-error-exit.ts": (
        "unlocked",
        "writes swarm run-state files without the migration lock; outside every release-0 data domain; release 1 must classify them (risk register)",
    ),
    "pi/extensions/grill-tool.ts": (
        "no-toolkit-data",
        "calls the locked Python CLI; writes no toolkit data itself",
    ),
    "pi/extensions/guard-rails.ts": (
        "no-toolkit-data",
        "calls the locked Python CLI; writes no toolkit data itself",
    ),
    "pi/extensions/herdr-blocked-bridge.ts": (
        "no-toolkit-data",
        "touches no toolkit data paths",
    ),
    "pi/extensions/model-picker.ts": (
        "no-toolkit-data",
        "writes only the harness's own pi settings",
    ),
    "pi/extensions/notify.ts": (
        "no-toolkit-data",
        "calls the locked Python CLI; writes no toolkit data itself",
    ),
    "pi/extensions/pending-plan-surface.ts": (
        "no-toolkit-data",
        "calls the locked Python CLI; writes no toolkit data itself",
    ),
    "pi/extensions/permission-gate.ts": (
        "no-toolkit-data",
        "touches no toolkit data paths",
    ),
    "pi/extensions/philosophy-header.ts": (
        "no-toolkit-data",
        "touches no toolkit data paths",
    ),
    "pi/extensions/question-tool.ts": (
        "no-toolkit-data",
        "touches no toolkit data paths",
    ),
    "pi/extensions/ruff-format-on-edit.ts": (
        "no-toolkit-data",
        "touches no toolkit data paths",
    ),
    "pi/extensions/second-opinion-tool.ts": (
        "no-toolkit-data",
        "calls the locked Python CLI; writes no toolkit data itself",
    ),
    "pi/extensions/standup-tool.ts": (
        "no-toolkit-data",
        "calls the locked Python CLI; writes no toolkit data itself",
    ),
    "pi/extensions/swarm-amend-ack.ts": (
        "unlocked",
        "writes swarm run-state files without the migration lock; outside every release-0 data domain; release 1 must classify them (risk register)",
    ),
    "pi/extensions/swarm-lib/swarm-herdr.ts": (
        "no-toolkit-data",
        "calls the locked Python CLI; writes no toolkit data itself",
    ),
    "pi/extensions/swarm-lib/swarm-picker-copilot.ts": (
        "no-toolkit-data",
        "touches no toolkit data paths",
    ),
    "pi/extensions/swarm-lib/swarm-picker.ts": (
        "no-toolkit-data",
        "touches no toolkit data paths",
    ),
    "pi/extensions/swarm-lib/swarm-scheduling.ts": (
        "no-toolkit-data",
        "calls the locked Python CLI; writes no toolkit data itself",
    ),
    "pi/extensions/swarm-lib/swarm-tool-context.ts": (
        "unlocked",
        "writes swarm run-state files without the migration lock; outside every release-0 data domain; release 1 must classify them (risk register)",
    ),
    "pi/extensions/swarm-tool.ts": (
        "no-toolkit-data",
        "touches no toolkit data paths",
    ),
    "pi/extensions/to-tickets-tool.ts": (
        "no-toolkit-data",
        "calls the locked Python CLI; writes no toolkit data itself",
    ),
    "pi/extensions/trust-session.ts": (
        "no-toolkit-data",
        "touches no toolkit data paths",
    ),
    "pi/extensions/vitals-promotion-tool.ts": (
        "no-toolkit-data",
        "calls the locked Python CLI; writes no toolkit data itself",
    ),
}

KINDS = ("writer", "reader", "none", "harness-owned", "infrastructure")
INFRASTRUCTURE = frozenset(
    {"agent_toolkit_paths", "migration_lock", "cli_common", "fault_checkpoint"}
)
ADOPTION_TESTS = REPO / "test" / "test_migration_lock_adoption.py"

# ── reference extraction ─────────────────────────────────────────────────────

_TEXT_RE = re.compile(
    r"(?:^|(?<=[\s/~\"'(=`$\[,]))\.claude((?:/[A-Za-z0-9_.-]+)*)(/(?:\*|<))?"
)
_SEGMENT_STOP = re.compile(r"[<*{]")


def _segments(tail: str) -> tuple[str, ...]:
    parts = [p for p in tail.split("/") if p]
    out: list[str] = []
    for p in parts:
        if _SEGMENT_STOP.search(p):
            break
        out.append(p.rstrip(".,;:)'\"`"))
    return tuple(p for p in out if p)


def text_references(text: str) -> list[tuple[int, tuple[str, ...]]]:
    refs: list[tuple[int, tuple[str, ...]]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for m in _TEXT_RE.finditer(line):
            refs.append((lineno, _segments(m.group(1))))
    return refs


def _flatten_div(node: ast.AST, env: dict[str, list[str | None]]) -> list[str | None]:
    """Flatten ``a / b / c`` into string parts; None marks an opaque operand."""
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _flatten_div(node.left, env) + _flatten_div(node.right, env)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.Name) and node.id in env:
        return list(env[node.id])
    return [None]


def _claude_segments(parts: Sequence[str | None]) -> tuple[str, ...] | None:
    """Segments after the first ``.claude`` in ``parts``, or None if absent."""
    flat: list[str | None] = []
    for p in parts:
        if p is None:
            flat.append(None)
        else:
            flat.extend(x for x in p.replace("~/", "/").split("/") if x)
    if ".claude" not in flat:
        return None
    after: list[str] = []
    for p in flat[flat.index(".claude") + 1 :]:
        if p is None or _SEGMENT_STOP.search(p):
            break
        after.append(p)
    return tuple(after)


def python_references(tree: ast.AST) -> list[tuple[int, tuple[str, ...]]]:
    """``.claude`` paths built in code (not plain strings): ``/`` chains and calls."""
    env: dict[str, list[str | None]] = {}
    refs: list[tuple[int, tuple[str, ...]]] = []
    seen: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and isinstance(node.value, ast.BinOp):
                parts = _flatten_div(node.value, env)
                if _claude_segments(parts) is not None:
                    env[target.id] = parts
    for node in ast.walk(tree):
        parts: list[str | None] | None = None
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            parts = _flatten_div(node, env)
        elif isinstance(node, ast.Call):
            func = node.func
            name = (
                func.attr
                if isinstance(func, ast.Attribute)
                else getattr(func, "id", "")
            )
            if name in ("Path", "join", "expanduser", "PurePath"):
                parts = [
                    a.value
                    if isinstance(a, ast.Constant) and isinstance(a.value, str)
                    else None
                    for a in node.args
                ]
        if parts is None:
            continue
        segs = _claude_segments(parts)
        line = getattr(node, "lineno", 0)
        key = hash((line, segs))
        if segs is not None and key not in seen:
            seen.add(key)
            refs.append((line, segs))
    return refs


# esbuild renames colliding imports with a numeric suffix (join2, resolve3).
_JS_JOIN_RE = re.compile(r"\b(?:join|resolve)\d*\(([^()]*(?:\([^()]*\)[^()]*)*)\)")
_JS_STR_RE = re.compile(r"""^\s*["'`]([^"'`]*)["'`]\s*$""")


def js_references(text: str) -> list[tuple[int, tuple[str, ...]]]:
    refs: list[tuple[int, tuple[str, ...]]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        for m in _JS_JOIN_RE.finditer(line):
            args = [a for a in m.group(1).split(",")]
            parts: list[str | None] = []
            for a in args:
                s = _JS_STR_RE.match(a)
                parts.append(s.group(1) if s else None)
            segs = _claude_segments(parts)
            if segs is not None:
                refs.append((lineno, segs))
    return refs


def classify(segs: tuple[str, ...]) -> str | None:
    """Ownership class for the segments after ``.claude``; None = unclassified."""
    if not segs:
        return "harness"
    top = segs[0]
    if top == "scripts":
        if len(segs) > 1 and segs[1] in FOREIGN_SCRIPTS:
            return "foreign"
        return "toolkit"
    if top == "data":
        if len(segs) == 1 or segs[1] in TOOLKIT_DATA_ENTRIES:
            return "toolkit"
        return None
    if top in TOOLKIT_TOP:
        return "toolkit"
    if top in HARNESS_TOP:
        return "harness"
    return None


# ── ownership check ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Reference:
    path: str
    line: int
    segments: tuple[str, ...]
    cls: str | None
    marked: bool


def tracked_files(repo: Path, skip: frozenset[str] = SKIP_FILES) -> list[str]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout
    return [
        f for f in out.splitlines() if f and f not in skip and "node_modules/" not in f
    ]


def references_in(rel: str, text: str) -> list[Reference]:
    found: dict[tuple[int, tuple[str, ...]], None] = {}
    for item in text_references(text):
        found.setdefault(item)
    if rel.endswith(".py"):
        try:
            tree = ast.parse(text)
        except SyntaxError:
            tree = None
        if tree is not None:
            for item in python_references(tree):
                found.setdefault(item)
    if rel.endswith((".ts", ".js", ".mjs")):
        for item in js_references(text):
            found.setdefault(item)
    lines = text.splitlines()
    refs: list[Reference] = []
    for line, segs in found:
        source_line = lines[line - 1] if 0 < line <= len(lines) else ""
        m = MARKER_RE.search(source_line)
        cls = m.group(1) if m else classify(segs)
        refs.append(Reference(rel, line, segs, cls, m is not None))
    return sorted(refs, key=lambda r: (r.path, r.line, r.segments))


def ownership(
    repo: Path, files: Iterable[str] | None = None
) -> tuple[list[Reference], list[str]]:
    refs: list[Reference] = []
    problems: list[str] = []
    for rel in files if files is not None else tracked_files(repo):
        path = repo / rel
        try:
            text = path.read_text()
        except (UnicodeDecodeError, IsADirectoryError, FileNotFoundError):
            continue
        if ".claude" not in text:
            continue
        for ref in references_in(rel, text):
            refs.append(ref)
            if ref.cls is None and not rel.startswith(TEST_TREES):
                shown = "/".join((".claude",) + ref.segments)
                problems.append(
                    f"{rel}:{ref.line}: {shown}: unclassified -- add a rule to "
                    "scripts/check_toolkit_paths.py (toolkit, harness, or foreign), "
                    "or mark the line 'path-ownership: <class>'"
                )
    return refs, problems


# ── inventory check ──────────────────────────────────────────────────────────


def _marker(tree: ast.Module, name: str) -> object:
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            try:
                return ast.literal_eval(node.value)
            except ValueError:
                return None
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
            and node.value is not None
        ):
            try:
                return ast.literal_eval(node.value)
            except ValueError:
                return None
    return None


def _code_text(tree: ast.Module) -> str:
    """Source with docstrings removed (references inside prose do not count)."""
    body = tree.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(getattr(body[0], "value", None), ast.Constant)
    ):
        body = body[1:]
    return "\n".join(ast.unparse(n) for n in body)


@dataclass
class PyModule:
    name: str
    kind: object
    via: object
    code: str
    imports: frozenset[str]


def _imports(tree: ast.Module) -> frozenset[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return frozenset(names)


def _installed(repo: Path) -> list[str]:
    data = tomllib.loads((repo / "links.toml").read_text())
    out: list[str] = []
    for entry in data.get("link", []):
        src = entry.get("src")
        if not isinstance(src, str):
            continue
        p = repo / src
        if p.is_dir():
            out.extend(
                f for f in tracked_files(repo) if f.startswith(src.rstrip("/") + "/")
            )
        else:
            out.append(src)
    return sorted(set(out))


def inventory(
    repo: Path, entries: Sequence[str] | None = None
) -> tuple[Counter[str], list[str]]:
    counts: Counter[str] = Counter()
    problems: list[str] = []
    modules: dict[str, PyModule] = {}
    installed = list(entries) if entries is not None else _installed(repo)
    for src in installed:
        if src.endswith(".py"):
            tree = ast.parse((repo / src).read_text())
            name = Path(src).stem
            modules[name] = PyModule(
                name,
                _marker(tree, "TOOLKIT_DATA"),
                _marker(tree, "TOOLKIT_DATA_VIA"),
                _code_text(tree),
                _imports(tree),
            )
        elif src.endswith((".ts", ".js", ".mjs")):
            row = NON_PYTHON.get(src)
            if row is None:
                problems.append(
                    f"{src}: no NON_PYTHON row -- add one to scripts/check_toolkit_paths.py "
                    "('no-toolkit-data', or 'unlocked' with a reason)"
                )
                continue
            status, reason = row
            if status not in ("no-toolkit-data", "unlocked") or (
                status == "unlocked" and not reason.strip()
            ):
                problems.append(f"{src}: bad NON_PYTHON row {row!r}")
            counts[f"non-python:{status}"] += 1
        elif (
            src.endswith(
                (".md", ".json", ".toml", ".zsh", ".sh", ".svg", ".png", ".ico")
            )
            or "/icons" in src
        ):
            counts["asset"] += 1
        else:
            problems.append(f"{src}: unknown kind of installed entry")
    adoption_text = ADOPTION_TESTS.read_text() if ADOPTION_TESTS.exists() else ""
    for mod in modules.values():
        problems.extend(_check_module(mod, modules, adoption_text))
        counts[f"python:{mod.kind}"] += 1
    return counts, problems


def _check_module(
    mod: PyModule, modules: dict[str, PyModule], adoption_text: str
) -> list[str]:
    where = f"agent-scripts/{mod.name}.py"
    if mod.kind not in KINDS:
        kinds = ", ".join(KINDS)
        return [f"{where}: TOOLKIT_DATA must be one of {kinds} (found {mod.kind!r})"]
    via = mod.via if mod.via is not None else ()
    if not isinstance(via, tuple) or not all(isinstance(v, str) for v in via):
        return [f"{where}: TOOLKIT_DATA_VIA must be a tuple of module names"]
    problems: list[str] = []
    for target in via:
        if target not in modules:
            problems.append(
                f"{where}: TOOLKIT_DATA_VIA names {target!r}, not an installed module"
            )
    if problems:
        return problems
    if _via_cycle(mod.name, modules):
        return [f"{where}: TOOLKIT_DATA_VIA has a cycle"]
    locks = "migration_lock" in mod.imports
    paths = "agent_toolkit_paths" in mod.imports
    if mod.kind == "infrastructure":
        if mod.name not in INFRASTRUCTURE:
            problems.append(
                f"{where}: 'infrastructure' is only for {sorted(INFRASTRUCTURE)}"
            )
    elif mod.kind == "writer":
        if any(modules[t].kind != "writer" for t in via):
            problems.append(f"{where}: a writer may only delegate to writers")
        if not locks and not (
            via and all("migration_lock" in modules[t].imports for t in via)
        ):
            problems.append(
                f"{where}: writer does not reference migration_lock (itself or via)"
            )
        if not paths and not via:
            problems.append(
                f"{where}: writer does not reference agent_toolkit_paths (itself or via)"
            )
        if not re.search(rf"\b{re.escape(mod.name)}\b", adoption_text):
            problems.append(
                f"{where}: writer has no execution test naming it in "
                "test/test_migration_lock_adoption.py"
            )
    elif mod.kind == "reader":
        if locks:
            problems.append(
                f"{where}: reader references migration_lock (a module that locks is a writer)"
            )
        if not paths and not via:
            problems.append(
                f"{where}: reader does not reference agent_toolkit_paths (itself or via)"
            )
        if any(modules[t].kind not in ("reader", "writer") for t in via):
            problems.append(
                f"{where}: a reader may only delegate to readers or writers"
            )
    elif mod.kind == "none":
        built = any(
            segs[:1] == ("data",) for _l, segs in python_references(ast.parse(mod.code))
        )
        if locks or paths or built:
            problems.append(
                f"{where}: marked 'none' but touches toolkit data paths or the lock"
            )
    return problems


def _via_cycle(start: str, modules: dict[str, PyModule]) -> bool:
    stack = [(start, frozenset({start}))]
    while stack:
        name, seen = stack.pop()
        via = modules[name].via or ()
        for t in via if isinstance(via, tuple) else ():
            if t in seen:
                return True
            if t in modules:
                stack.append((t, seen | {t}))
    return False


# ── generator inventory ──────────────────────────────────────────────────────

GENERATED_HEADER_RE = re.compile(
    r"<!-- generated by agent-scripts/(gen_[a-z_]+\.py) — do not edit"
)
SWARM_OUTPUTS = (
    "copilot/extensions/swarm/extensions/swarm/extension.mjs",
    "copilot/extensions/swarm/lib/swarm-tool-logic.js",
    "copilot/extensions/swarm/lib/swarm-scheduling.js",
    "copilot/extensions/swarm/lib/swarm-herdr.js",
    "copilot/extensions/swarm/lib/swarm-picker.js",
)


def _agent_script(name: str) -> ModuleType:
    """Import a generator module from agent-scripts/ to read its output table."""
    scripts = str(REPO / "agent-scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    return importlib.import_module(name)


@dataclass(frozen=True)
class Generator:
    """One generator: how to run it, how to check it, and what it emits.

    ``sources`` is documentation only; nothing checks it. ``link_lines``
    matches output lines that render a links.toml destination verbatim: those
    move when links.toml does, not when the generator's own text does.
    """

    command: str
    check: str | None
    sources: tuple[str, ...]
    outputs: Callable[[], list[str]]
    link_lines: re.Pattern[str] | None = None


GENERATORS: dict[str, Generator] = {
    "gen_skills.py": Generator(
        "python3 agent-scripts/gen_skills.py",
        "python3 agent-scripts/gen_skills.py --check",
        ("templates/*.md.tmpl", "agent-scripts/gen_skills_params.py"),
        lambda: sorted(_agent_script("gen_skills").OUTPUT_PATHS.values()),
    ),
    "gen_second_opinion.py": Generator(
        "python3 agent-scripts/gen_second_opinion.py",
        "python3 agent-scripts/gen_second_opinion.py --check",
        ("templates/second_opinion.md.tmpl", "agent-scripts/gen_second_opinion.py"),
        lambda: sorted(_agent_script("gen_second_opinion").HARNESS_TABLE),
    ),
    "gen_interfaces.py": Generator(
        "python3 agent-scripts/gen_interfaces.py [--update-fingerprints]",
        "python3 agent-scripts/gen_interfaces.py --check",
        ("agent-scripts/*.py", "links.toml", "the harness command trees"),
        lambda: ["INTERFACES.md", "agent-scripts/contract_fingerprints.json"],
        # the per-module "Installed at" line and the asset table's link column
        re.compile(r"^(?:- Installed at: |\| `[^`]+` \| )`([^`]+)`"),
    ),
    "gen_shell_completion.py": Generator(
        "python3 agent-scripts/gen_shell_completion.py --harness all",
        None,
        ("agent-scripts/gen_shell_completion.py",),
        list,  # writes ~/.zsh/completions, outside the repository
    ),
    "build-copilot-swarm.sh": Generator(
        "scripts/build-copilot-swarm.sh",
        "scripts/build-copilot-swarm.sh --check",
        ("pi/extensions/swarm-lib/*.ts", "copilot/extensions/swarm/src/extension.ts"),
        lambda: list(SWARM_OUTPUTS),
    ),
}


def generators(
    repo: Path,
    table: dict[str, Generator] | None = None,
    files: Sequence[str] | None = None,
) -> list[str]:
    """Problems with the generator inventory: every generated file has one owner."""
    table = GENERATORS if table is None else table
    tracked = set(tracked_files(repo, frozenset()) if files is None else files)
    owners: dict[str, list[str]] = defaultdict(list)
    problems: list[str] = []
    for name, gen in table.items():
        for out in gen.outputs():
            owners[out].append(name)
            if out not in tracked:
                problems.append(f"{name}: declared output {out} is not tracked")
    for out, names in sorted(owners.items()):
        if len(names) > 1:
            problems.append(f"{out}: claimed by {', '.join(sorted(names))}")
    for rel in sorted(tracked):
        if not rel.endswith(".md") or rel in owners:
            continue
        try:
            text = (repo / rel).read_text()
        except (UnicodeDecodeError, IsADirectoryError, FileNotFoundError):
            continue
        m = GENERATED_HEADER_RE.search(text)
        if m:
            problems.append(
                f"{rel}: header names {m.group(1)} but no GENERATORS row "
                "declares it as an output"
            )
    return problems


def _link_destinations(repo: Path) -> frozenset[str]:
    data = tomllib.loads((repo / "links.toml").read_text())
    return frozenset(
        e["dest"] for e in data.get("link", []) if isinstance(e.get("dest"), str)
    )


def generated_legacy_references(
    repo: Path, table: dict[str, Generator] | None = None
) -> list[str]:
    """Toolkit-owned ``.claude`` paths a generator still emits.

    Harness-owned and foreign references pass, as in the ownership check. A
    line the row's ``link_lines`` pattern matches passes when it names an
    actual links.toml destination.
    """
    table = GENERATORS if table is None else table
    dests = _link_destinations(repo)
    problems: list[str] = []
    for name, gen in table.items():
        for rel in gen.outputs():
            try:
                text = (repo / rel).read_text()
            except FileNotFoundError:
                continue
            lines = text.splitlines()
            for ref in references_in(rel, text):
                if ref.cls != "toolkit":
                    continue
                line = lines[ref.line - 1] if 0 < ref.line <= len(lines) else ""
                m = gen.link_lines.match(line) if gen.link_lines else None
                if m and m.group(1) in dests:
                    continue
                shown = "/".join((".claude",) + ref.segments)
                problems.append(
                    f"{rel}:{ref.line}: {shown}: legacy toolkit path emitted by "
                    f"{name} -- route it through harness_spec.TOOLKIT_PATH_TOKENS"
                )
    return problems


# ── CLI ──────────────────────────────────────────────────────────────────────


def cmd_ownership(args: argparse.Namespace) -> int:
    refs, problems = ownership(REPO)
    for p in problems:
        print(p)
    if args.report:
        by_class: dict[str, set[str]] = defaultdict(set)
        counts: Counter[str] = Counter()
        for r in refs:
            key = r.cls or "unclassified"
            counts[key] += 1
            by_class[key].add(r.path)
        print("\n# ownership report")
        for key in (*CLASSES, "unclassified"):
            print(f"{key}: {counts[key]} references in {len(by_class[key])} files")
        marked = [r for r in refs if r.marked]
        print(f"marked lines: {len(marked)}")
        for r in marked:
            print(f"  {r.path}:{r.line} -> {r.cls}")
        print("\n# toolkit-owned files (release-1 conversion work list)")
        for f in sorted(by_class["toolkit"]):
            print(f"  {f}")
    return 1 if problems else 0


def cmd_inventory(args: argparse.Namespace) -> int:
    counts, problems = inventory(REPO)
    for p in problems:
        print(p)
    if args.report:
        print("\n# inventory report")
        for key, n in sorted(counts.items()):
            print(f"{key}: {n}")
    return 1 if problems else 0


def cmd_generators(args: argparse.Namespace) -> int:
    problems = generators(REPO) + generated_legacy_references(REPO)
    for p in problems:
        print(p)
    if args.report:
        print("\n# generator inventory")
        for name, gen in GENERATORS.items():
            check = gen.check or "(no check mode)"
            print(f"{name}: {len(gen.outputs())} outputs; check: {check}")
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Toolkit-home migration repository checks."
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name, help_text in (
        ("ownership", "classify every .claude path reference"),
        ("inventory", "check every links.toml entry's declared kind"),
        ("generators", "check the generator inventory and its emitted paths"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument(
            "--report", action="store_true", help="print counts and work lists"
        )
    args = parser.parse_args(argv)
    return {
        "ownership": cmd_ownership,
        "inventory": cmd_inventory,
        "generators": cmd_generators,
    }[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
