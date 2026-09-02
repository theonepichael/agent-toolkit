#!/usr/bin/env python3
"""gen_interfaces.py — regenerate INTERFACES.md mechanically from the sources.

INTERFACES.md used to be hand-written, and drifted: its "other shared
scripts" section was guesswork (dev_status.py was described as "likely
accepts CLI flags" when it is ~3.3k lines with ~20 subcommands). This
script replaces the guessing with extraction. Every per-file fact in the
generated document comes from the source tree: module docstrings, argparse
parser/subparser trees, public function and class signatures, ``os.environ``
lookups, ``sys.exit`` codes, module-level ``Path`` constants, the symlink
table in links.toml, and the YAML frontmatter of each harness's command and
skill documents.

Extraction is static only — the module is parsed with :mod:`ast`, never
imported, and no documented script is executed (not even ``--help``). These
scripts manage live state under ``~/.claude`` and ``~/.local/state``;
importing or running them to document them would be a side effect of
building a doc.

The one subprocess this module does run is ``git ls-files``, to restrict the
asset table to tracked files — read-only, and not one of the scripts being
documented. See :func:`tracked_files` for why that filter is load-bearing.

Usage:
    python3 claude/scripts/gen_interfaces.py                  rewrite INTERFACES.md
    python3 claude/scripts/gen_interfaces.py --check           exit 1/3 if it is stale
    python3 claude/scripts/gen_interfaces.py --stdout          print, write nothing
    python3 claude/scripts/gen_interfaces.py --update-fingerprints
                                                                accept current behavior
                                                                as the new baseline

Section 5 of the generated document cross-references each backing script's
real CLI contract against every skill/command doc's shown example of running
it (a doc→script invocation check: does the doc's copy-pasteable command
still use subcommands/flags the script actually has). ``--check`` treats a
mismatch there as a distinct failure from a stale file — see Exit codes.

``--check`` also compares each in-scope script's *behavior* — subcommand and
argument help text (including ``choices=``/``default=``/``required``), and
leaf-subcommand handler docstrings — against a recorded fingerprint in
``contract_fingerprints.json``. This catches a flag or subcommand that kept
its name while its real behavior changed underneath it, which the invocation
check above cannot see. A mismatch prints a diff and the docs to re-read;
accept the new behavior with ``--update-fingerprints`` only after reading
that diff, never blind. See :func:`check_contract_fingerprints`.

Flags: --check, --stdout, --update-fingerprints, --repo-root <path>,
--output <path>, --quiet/-q, --verbose/-v.
Env vars: none.
Files read: <repo>/links.toml, <repo>/contract_fingerprints.json, and every
file under claude/, copilot/, opencode/, agy/.
Files written: <repo>/INTERFACES.md (skipped by --check and --stdout);
<repo>/claude/scripts/contract_fingerprints.json (only with
--update-fingerprints).
Exit codes: 0 success; 1 --check found the file itself stale (fix: rerun
without --check); 2 bad usage or unreadable input; 3 --check found a doc's
shown command example drifted from its script's real CLI contract, or a
script's recorded contract fingerprint no longer matches its live behavior
(fix: edit the named doc(s), or run --update-fingerprints after reading the
diff — never regenerate INTERFACES.md to fix this).

Requires Python 3.12+.
"""

import argparse
import ast
import difflib
import json
import re
import shlex
import subprocess
import sys
import tomllib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import cli_common

HARNESS_DIRS = ("claude", "copilot", "opencode", "agy", "pi")
SCRIPTS_DIR = "claude/scripts"
ROOT_ENTRYPOINTS = ("install.py", "depart.py")
OUTPUT_NAME = "INTERFACES.md"

PREAMBLE = """\
Scope: `claude/`, `copilot/`, `opencode/`, `agy/`, `pi/`, the shared scripts under
`claude/scripts/` that `links.toml` installs into `~/.claude/scripts/`, and the
repo-root installer entrypoints those harnesses are provisioned by.

**This file is generated. Do not edit it by hand — your edits will be
overwritten.** Regenerate it after changing any harness script:

```bash
python3 claude/scripts/gen_interfaces.py           # rewrite this file
python3 claude/scripts/gen_interfaces.py --check   # exit 1 if it is stale
```

Everything below is extracted statically, with `ast`, `tomllib`, and a small
frontmatter reader. No harness module is imported and no script is run — these
scripts mutate live state under `~/.claude`, so documenting them must not
execute them. The cost of that choice is that anything invisible to static
analysis is reported as unknown rather than guessed at: `choices=` computed at
runtime, hand-rolled `sys.argv` dispatch, and behaviour that only exists in
prose. Where this file says a detail is not statically visible, read the
source.

"Public" below means exactly one thing: defined at module level without a
leading underscore. That is a naming convention, not a curated API — a helper
that simply was never underscored will appear here.

House style for these interfaces is in `STYLE.md`.\
"""

# ── extracted models ─────────────────────────────────────────────────────────


@dataclass
class CliArgument:
    """One ``add_argument`` call, reduced to what a reader needs."""

    usage: str
    label: str
    help_text: str | None = None
    notes: list[str] = field(default_factory=list)


@dataclass
class Subcommand:
    """One ``add_parser`` call and the arguments attached to it."""

    path: tuple[str, ...]
    help_text: str | None = None
    arguments: list[CliArgument] = field(default_factory=list)

    @property
    def name(self) -> str:
        """Return the space-joined subcommand path (e.g. ``pending add``)."""
        return " ".join(self.path)


@dataclass
class CliSpec:
    """The argparse surface of a module."""

    description: str | None = None
    prog: str | None = None
    options: list[CliArgument] = field(default_factory=list)
    subcommands: list[Subcommand] = field(default_factory=list)


@dataclass
class ApiSymbol:
    """A public module-level function or class."""

    signature: str
    summary: str | None = None


@dataclass
class LinkTarget:
    """One ``links.toml`` destination for a repo file, plus the gates on it."""

    dest: str
    gates: list[str] = field(default_factory=list)


LinkTable = dict[str, list[LinkTarget]]


@dataclass
class ModuleInterface:
    """Everything statically known about one Python module."""

    relpath: str
    purpose: str
    shebang: str | None = None
    executable: bool = False
    links: list[LinkTarget] = field(default_factory=list)
    cli: CliSpec | None = None
    argv_dispatch: bool = False
    env_vars: list[str] = field(default_factory=list)
    path_constants: list[str] = field(default_factory=list)
    exit_codes: list[int] = field(default_factory=list)
    internal_imports: list[str] = field(default_factory=list)
    exceptions: list[ApiSymbol] = field(default_factory=list)
    classes: list[ApiSymbol] = field(default_factory=list)
    functions: list[ApiSymbol] = field(default_factory=list)
    handlers: list[str] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)
    leaf_handler_docstrings: dict[tuple[str, ...], str] = field(default_factory=dict)
    unmatched_leaf_subcommands: list[tuple[str, ...]] = field(default_factory=list)


@dataclass
class HelperSpec:
    """A module function that adds arguments to a parser passed as its first arg."""

    params: list[str]
    defaults: dict[str, str]
    calls: list[ast.Call]


# ── small ast helpers ────────────────────────────────────────────────────────


def first_paragraph(text: str | None) -> str:
    """Collapse the first blank-line-delimited paragraph of ``text`` to one line."""
    if not text:
        return ""
    paragraph = text.strip().split("\n\n", 1)[0]
    return " ".join(paragraph.split())


def first_sentence(text: str | None) -> str | None:
    """Return the first sentence of ``text``, or ``None`` when it is empty."""
    collapsed = first_paragraph(text)
    if not collapsed:
        return None
    match = re.search(r"^(.+?[.!?])(?:\s|$)", collapsed)
    return match.group(1) if match else collapsed


def attr_path(node: ast.expr) -> str:
    """Render a dotted attribute/name expression, or ``""`` for anything else."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = attr_path(node.value)
        return f"{base}.{node.attr}" if base else ""
    return ""


def literal_str(node: ast.expr | None, bindings: dict[str, str]) -> str | None:
    """Resolve ``node`` to a string, substituting ``bindings`` for bare names.

    Returns ``None`` for anything not statically resolvable (a call, a
    comprehension, a name with no binding), which callers treat as "not
    visible to static analysis" rather than guessing.
    """
    if node is None:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return bindings.get(node.id)
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            elif isinstance(value, ast.FormattedValue):
                resolved = literal_str(value.value, bindings)
                if resolved is None:
                    return None
                parts.append(resolved)
            else:
                return None
        return "".join(parts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = literal_str(node.left, bindings)
        right = literal_str(node.right, bindings)
        return None if left is None or right is None else left + right
    return None


def literal_scalar(node: ast.expr | None) -> str | None:
    """Render a simple constant (str/int/float/bool/None) for display."""
    if not isinstance(node, ast.Constant):
        return None
    if isinstance(node.value, str):
        return node.value
    if node.value is None or isinstance(node.value, bool | int | float):
        return repr(node.value)
    return None


def literal_sequence(node: ast.expr | None) -> list[str] | None:
    """Render a literal list/tuple of constants, or ``None`` if it is dynamic."""
    if not isinstance(node, ast.List | ast.Tuple):
        return None
    items: list[str] = []
    for element in node.elts:
        rendered = literal_scalar(element)
        if rendered is None:
            return None
        items.append(rendered)
    return items


def keyword_map(call: ast.Call) -> dict[str, ast.expr]:
    """Return the call's keyword arguments as a name -> node mapping."""
    return {kw.arg: kw.value for kw in call.keywords if kw.arg is not None}


def linear_statements(body: Sequence[ast.stmt]) -> Iterator[ast.stmt]:
    """Yield statements in source order, descending into blocks but not defs.

    Nested ``def``/``class`` bodies are skipped deliberately: a helper nested
    in ``main`` often names its parameter ``p``, which would otherwise collide
    with the ``p`` the caller is currently using for a subparser.
    """
    for statement in body:
        yield statement
        if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        for field_name in ("body", "orelse", "finalbody"):
            nested = getattr(statement, field_name, None)
            if isinstance(nested, list):
                yield from linear_statements(nested)
        for handler in getattr(statement, "handlers", []):
            yield from linear_statements(handler.body)


# ── signatures ───────────────────────────────────────────────────────────────


def render_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Render ``def`` as a one-line signature with annotations intact."""
    args = node.args
    parts: list[str] = []
    positional = list(args.posonlyargs) + list(args.args)
    defaults: list[ast.expr | None] = [None] * (
        len(positional) - len(args.defaults)
    ) + list(args.defaults)
    for index, arg in enumerate(positional):
        parts.append(render_arg(arg, defaults[index]))
        if args.posonlyargs and index == len(args.posonlyargs) - 1:
            parts.append("/")
    if args.vararg is not None:
        parts.append("*" + render_arg(args.vararg, None))
    elif args.kwonlyargs:
        parts.append("*")
    for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True):
        parts.append(render_arg(arg, default))
    if args.kwarg is not None:
        parts.append("**" + render_arg(args.kwarg, None))
    returns = f" -> {ast.unparse(node.returns)}" if node.returns else ""
    prefix = "async def " if isinstance(node, ast.AsyncFunctionDef) else ""
    return f"{prefix}{node.name}({', '.join(parts)}){returns}"


def render_arg(arg: ast.arg, default: ast.expr | None) -> str:
    """Render a single parameter with its annotation and default."""
    rendered = arg.arg
    if arg.annotation is not None:
        rendered += f": {ast.unparse(arg.annotation)}"
    if default is not None:
        separator = " = " if arg.annotation is not None else "="
        rendered += f"{separator}{ast.unparse(default)}"
    return rendered


def render_class(node: ast.ClassDef) -> str:
    """Render a class header, bases included."""
    bases = [ast.unparse(base) for base in node.bases]
    bases += [f"{kw.arg}={ast.unparse(kw.value)}" for kw in node.keywords if kw.arg]
    return f"class {node.name}({', '.join(bases)})" if bases else f"class {node.name}"


def is_exception(node: ast.ClassDef) -> bool:
    """Report whether a class derives from something named like an exception."""
    return any(
        attr_path(base).split(".")[-1].endswith(("Error", "Exception"))
        for base in node.bases
    )


# ── argparse extraction ──────────────────────────────────────────────────────


def parser_subclasses(tree: ast.Module) -> set[str]:
    """Return module-level class names that subclass ``ArgumentParser``.

    ``install.py`` builds its CLI from a ``_Parser(argparse.ArgumentParser)``
    subclass; without this the whole CLI would silently read as absent.
    """
    return {
        node.name
        for node in tree.body
        if isinstance(node, ast.ClassDef)
        and any(
            attr_path(base).split(".")[-1] == "ArgumentParser" for base in node.bases
        )
    }


def is_parser_constructor(node: ast.expr, subclasses: set[str]) -> bool:
    """Report whether ``node`` constructs an :class:`argparse.ArgumentParser`."""
    if not isinstance(node, ast.Call):
        return False
    name = attr_path(node.func).split(".")[-1]
    return name == "ArgumentParser" or name in subclasses


def collect_parser_helpers(tree: ast.Module) -> dict[str, HelperSpec]:
    """Find functions whose first parameter is a parser they add arguments to.

    ``dev_status.py``'s ``_add_id_arg``/``_add_if_rev_arg`` and ``grill.py``'s
    nested ``add_session_flag`` add arguments on behalf of their caller. Without
    resolving them, roughly half of those two CLIs' arguments would be missing.
    """
    helpers: dict[str, HelperSpec] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        positional = list(node.args.posonlyargs) + list(node.args.args)
        if not positional:
            continue
        target = positional[0].arg
        calls = [
            child
            for child in ast.walk(node)
            if isinstance(child, ast.Call)
            and attr_path(child.func) == f"{target}.add_argument"
        ]
        if not calls:
            continue
        defaults: dict[str, str] = {}
        offset = len(positional) - len(node.args.defaults)
        for index, default in enumerate(node.args.defaults):
            resolved = literal_str(default, {})
            if resolved is not None:
                defaults[positional[offset + index].arg] = resolved
        helpers[node.name] = HelperSpec(
            params=[arg.arg for arg in positional],
            defaults=defaults,
            calls=calls,
        )
    return helpers


def helper_bindings(spec: HelperSpec, call: ast.Call) -> dict[str, str]:
    """Bind a helper's parameters to the literal values at one call site."""
    bindings = dict(spec.defaults)
    for index, arg in enumerate(call.args[1:], start=1):
        if index < len(spec.params):
            resolved = literal_str(arg, {})
            if resolved is not None:
                bindings[spec.params[index]] = resolved
    for keyword in call.keywords:
        if keyword.arg is None:
            continue
        resolved = literal_str(keyword.value, {})
        if resolved is not None:
            bindings[keyword.arg] = resolved
    return bindings


VALUELESS_ACTIONS = frozenset(
    {"store_true", "store_false", "store_const", "count", "help", "version"}
)


def build_argument(call: ast.Call, bindings: dict[str, str]) -> CliArgument | None:
    """Turn one ``add_argument`` call into a displayable argument record."""
    names = [
        resolved
        for resolved in (literal_str(arg, bindings) for arg in call.args)
        if resolved is not None
    ]
    if not names:
        return None
    kwargs = keyword_map(call)
    metavar = literal_str(kwargs.get("metavar"), bindings)
    choices = literal_sequence(kwargs.get("choices"))
    action = literal_str(kwargs.get("action"), bindings)
    nargs = literal_scalar(kwargs.get("nargs"))
    required = isinstance(kwargs.get("required"), ast.Constant) and bool(
        kwargs["required"].value  # type: ignore[union-attr]
    )
    notes: list[str] = []
    if choices is not None:
        notes.append("choices: " + ", ".join(choices))
    elif "choices" in kwargs:
        notes.append("choices computed at runtime")
    default = literal_scalar(kwargs.get("default"))
    if default is not None and default != "None":
        notes.append(f"default: {default}")

    if names[0].startswith("-"):
        flag = max((name for name in names if name.startswith("--")), default=names[0])
        if action in VALUELESS_ACTIONS:
            value = ""
        elif metavar:
            value = f" {metavar}"
        elif choices:
            value = " {" + ",".join(choices) + "}"
        else:
            value = f" <{flag.lstrip('-').replace('-', '_').upper()}>"
        usage = f"{flag}{value}" if required else f"[{flag}{value}]"
        label = "/".join(names)
        if required:
            notes.append("required")
    else:
        base = metavar or f"<{names[0]}>"
        usage = f"[{base}]" if nargs == "'?'" or nargs == "?" else base
        label = names[0]
        if nargs is not None:
            notes.append(f"nargs: {nargs}")
    return CliArgument(
        usage=usage,
        label=label,
        help_text=literal_str(kwargs.get("help"), bindings),
        notes=notes,
    )


@dataclass
class _ParserRef:
    """A local variable currently bound to a parser (root or subcommand)."""

    subcommand: Subcommand | None


@dataclass
class _SubparsersRef:
    """A local variable currently bound to an ``add_subparsers`` container."""

    parent: Subcommand | None


def extract_cli(tree: ast.Module, module_doc: str) -> CliSpec | None:
    """Extract the argparse surface, or ``None`` when the module has no CLI."""
    subclasses = parser_subclasses(tree)
    builder = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and any(
                is_parser_constructor(child, subclasses) for child in ast.walk(node)
            )
        ),
        None,
    )
    if builder is None:
        return None

    helpers = collect_parser_helpers(tree)
    doc_bindings = {"__doc__": module_doc}
    spec = CliSpec()
    symbols: dict[str, _ParserRef | _SubparsersRef] = {}

    def attach(ref: _ParserRef, argument: CliArgument | None) -> None:
        if argument is None:
            return
        if ref.subcommand is None:
            spec.options.append(argument)
        else:
            ref.subcommand.arguments.append(argument)

    def register(container: _SubparsersRef, call: ast.Call) -> Subcommand | None:
        name = literal_str(call.args[0], {}) if call.args else None
        if name is None:
            return None
        parent_path = container.parent.path if container.parent else ()
        subcommand = Subcommand(
            path=(*parent_path, name),
            help_text=literal_str(keyword_map(call).get("help"), doc_bindings),
        )
        spec.subcommands.append(subcommand)
        return subcommand

    def handle_call(call: ast.Call) -> _ParserRef | _SubparsersRef | None:
        """Interpret one call; return a new symbol value when it produces one."""
        if is_parser_constructor(call, subclasses):
            kwargs = keyword_map(call)
            if spec.description is None:
                spec.description = literal_str(kwargs.get("description"), doc_bindings)
                spec.prog = literal_str(kwargs.get("prog"), doc_bindings)
            return _ParserRef(None)
        dotted = attr_path(call.func)
        owner, _, method = dotted.rpartition(".")
        target = symbols.get(owner)
        if method == "add_subparsers" and isinstance(target, _ParserRef):
            return _SubparsersRef(target.subcommand)
        if method == "add_parser" and isinstance(target, _SubparsersRef):
            subcommand = register(target, call)
            return _ParserRef(subcommand) if subcommand else None
        if method == "add_argument" and isinstance(target, _ParserRef):
            attach(target, build_argument(call, {}))
            return None
        if isinstance(call.func, ast.Name) and call.func.id in helpers and call.args:
            first = call.args[0]
            ref = symbols.get(first.id) if isinstance(first, ast.Name) else None
            if isinstance(ref, _ParserRef):
                helper = helpers[call.func.id]
                bindings = helper_bindings(helper, call)
                for inner in helper.calls:
                    attach(ref, build_argument(inner, bindings))
            return None
        if dotted.endswith("add_verbosity_args") and call.args:
            first = call.args[0]
            ref = symbols.get(first.id) if isinstance(first, ast.Name) else None
            if isinstance(ref, _ParserRef):
                attach(
                    ref,
                    CliArgument(usage="[--quiet]", label="--quiet/-q", help_text=None),
                )
                attach(
                    ref,
                    CliArgument(
                        usage="[--verbose]", label="--verbose/-v", help_text=None
                    ),
                )
        return None

    for statement in linear_statements(builder.body):
        if isinstance(statement, ast.Assign) and isinstance(statement.value, ast.Call):
            produced = handle_call(statement.value)
            for target in statement.targets:
                if isinstance(target, ast.Name):
                    if produced is None:
                        symbols.pop(target.id, None)
                    else:
                        symbols[target.id] = produced
        elif isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call):
            handle_call(statement.value)

    return spec


# ── leaf handler matching (contract-fingerprint doc-drift check) ───────────────


def leaf_subcommand_paths(subcommands: list[Subcommand]) -> set[tuple[str, ...]]:
    """Return the paths with no other subcommand nested under them.

    A subcommand whose path is a strict prefix of another (``("pending",)``
    under ``("pending", "add")``) is a pure routing node with no handler of
    its own by construction — it must never be treated as a match failure
    by :func:`match_leaf_handlers`.
    """
    all_paths = {sc.path for sc in subcommands}
    return {
        path
        for path in all_paths
        if not any(
            len(other) > len(path) and other[: len(path)] == path for other in all_paths
        )
    }


def cmd_function_name(path: tuple[str, ...]) -> str:
    """Render the expected handler function name for a subcommand path.

    Hyphens in any path segment are normalized to underscores before
    joining — e.g. ``("out-of-scope", "add")`` -> ``cmd_out_of_scope_add``,
    matching this repo's actual naming convention even where a subcommand
    group is dispatched through an explicit dict rather than this lookup.
    """
    return "cmd_" + "_".join(segment.replace("-", "_") for segment in path)


def match_leaf_handlers(
    tree: ast.Module, cli: CliSpec
) -> tuple[dict[tuple[str, ...], str], list[tuple[str, ...]]]:
    """Match every leaf subcommand to its ``cmd_<path>`` handler's docstring.

    Returns ``(docstrings, unmatched)``: a leaf's docstring is ``""`` when
    the handler exists but is undocumented (legitimate), and its path is
    reported in ``unmatched`` only when no handler function of that name
    exists at all (an extraction gap that must not be silent). Group nodes
    (see :func:`leaf_subcommand_paths`) never appear in either return value.
    """
    functions = {
        node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)
    }
    docstrings: dict[tuple[str, ...], str] = {}
    unmatched: list[tuple[str, ...]] = []
    for path in sorted(leaf_subcommand_paths(cli.subcommands)):
        node = functions.get(cmd_function_name(path))
        if node is None:
            unmatched.append(path)
            continue
        docstrings[path] = ast.get_docstring(node) or ""
    return docstrings, unmatched


def fingerprint_lines(
    cli: CliSpec,
    leaf_docstrings: dict[tuple[str, ...], str],
    module_purpose: str,
    exit_codes: list[int],
) -> list[str]:
    """Render a script's behavior-carrying CLI surface as sorted, diffable lines.

    One line per subcommand's ``help=`` text (leaf and group alike — a
    group's own help text is still meaningful), one line per argument
    (flag or positional, via :func:`render_notes` so ``choices=``/
    ``default=``/``required`` are covered, not just prose ``help=``), one
    line per matched leaf handler's docstring, and one line for the sorted
    exit codes. A flat CLI (no subcommands at all) fingerprints
    ``module_purpose`` in place of handler docstrings. Every field is
    canonicalized with :func:`inline`, so pure re-wrapping never changes
    the result; sorting the final list makes source order irrelevant too.
    """
    lines: list[str] = []
    for subcommand in cli.subcommands:
        lines.append(
            f"subcommand {subcommand.name}: {inline(subcommand.help_text or '')}"
        )
        for argument in subcommand.arguments:
            lines.append(
                f"arg {subcommand.name} {argument.label}: "
                f"{inline(render_notes(argument))}"
            )
    for argument in cli.options:
        lines.append(
            f"arg (top-level) {argument.label}: {inline(render_notes(argument))}"
        )

    if cli.subcommands:
        for path, docstring in leaf_docstrings.items():
            lines.append(f"handler {' '.join(path)}: {inline(docstring)}")
    else:
        lines.append(f"module docstring: {inline(module_purpose)}")

    lines.append("exit codes: " + ", ".join(str(code) for code in sorted(exit_codes)))
    return sorted(lines)


# ── module-wide extraction ───────────────────────────────────────────────────

ENV_ACCESSORS = frozenset({"os.environ.get", "os.getenv", "environ.get", "getenv"})


def _module_functions(tree: ast.Module) -> dict[str, ast.FunctionDef]:
    """Map top-level function name -> its ``FunctionDef`` node.

    Nested/class-scoped functions are excluded — this repo's env-var
    forwarding pattern (a small module-level helper that resolves one env
    var) only occurs at module scope, so a bare-name call-site match is
    enough and no further scope resolution is needed.
    """
    return {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}


def _env_forwarding_params(tree: ast.Module) -> dict[str, set[str]]:
    """Find, per module-level function, which parameters eventually reach
    an env-var accessor — directly, or via a call to another such function
    in this module (any number of hops).

    E.g. ``_resolve_pooled_model`` doesn't call ``os.environ.get`` itself;
    it forwards to ``_env_stripped``/``_parse_pool``, which do. A single
    fixed-point pass over module-level functions catches chains of any
    depth: each pass can only add new markings (monotonic), so it always
    terminates.
    """
    functions = _module_functions(tree)
    param_names = {
        name: [a.arg for a in fn.args.args] for name, fn in functions.items()
    }
    marked: dict[str, set[str]] = {name: set() for name in functions}

    changed = True
    while changed:
        changed = False
        for name, fn in functions.items():
            own_params = set(param_names[name])
            for node in ast.walk(fn):
                if node is fn:
                    continue
                if isinstance(node, ast.Call):
                    path = attr_path(node.func)
                    if path in ENV_ACCESSORS:
                        if node.args and isinstance(node.args[0], ast.Name):
                            arg_name = node.args[0].id
                            if arg_name in own_params and arg_name not in marked[name]:
                                marked[name].add(arg_name)
                                changed = True
                        continue
                    callee_marked = marked.get(path)
                    if not callee_marked:
                        continue
                    callee_params = param_names[path]
                    for i, arg in enumerate(node.args):
                        if (
                            i >= len(callee_params)
                            or callee_params[i] not in callee_marked
                        ):
                            continue
                        if (
                            isinstance(arg, ast.Name)
                            and arg.id in own_params
                            and arg.id not in marked[name]
                        ):
                            marked[name].add(arg.id)
                            changed = True
                    for kw in node.keywords:
                        if kw.arg is None or kw.arg not in callee_marked:
                            continue
                        if (
                            isinstance(kw.value, ast.Name)
                            and kw.value.id in own_params
                            and kw.value.id not in marked[name]
                        ):
                            marked[name].add(kw.value.id)
                            changed = True
                elif isinstance(node, ast.Subscript) and attr_path(node.value) in (
                    "os.environ",
                    "environ",
                ):
                    if (
                        isinstance(node.slice, ast.Name)
                        and node.slice.id in own_params
                        and node.slice.id not in marked[name]
                    ):
                        marked[name].add(node.slice.id)
                        changed = True
    return {name: params for name, params in marked.items() if params}


def extract_env_vars(tree: ast.Module) -> list[str]:
    """Collect the environment variable names the module reads.

    Includes direct ``os.environ.get``/``os.getenv``/subscript access, plus
    env var names passed as literal string arguments to a local helper
    function whose parameter is itself (transitively) passed to one of
    those accessors — see :func:`_env_forwarding_params`.
    """
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and attr_path(node.func) in ENV_ACCESSORS:
            name = literal_str(node.args[0], {}) if node.args else None
            if name:
                found.add(name)
        elif isinstance(node, ast.Subscript) and attr_path(node.value) in (
            "os.environ",
            "environ",
        ):
            name = literal_str(node.slice, {})
            if name:
                found.add(name)

    forwarding = _env_forwarding_params(tree)
    if forwarding:
        function_params = {
            name: [a.arg for a in fn.args.args]
            for name, fn in _module_functions(tree).items()
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            marked_params = forwarding.get(attr_path(node.func))
            if not marked_params:
                continue
            callee_params = function_params[attr_path(node.func)]
            for i, arg in enumerate(node.args):
                if i >= len(callee_params) or callee_params[i] not in marked_params:
                    continue
                name = literal_str(arg, {})
                if name:
                    found.add(name)
            for kw in node.keywords:
                if kw.arg not in marked_params:
                    continue
                name = literal_str(kw.value, {})
                if name:
                    found.add(name)

    return sorted(found)


def extract_exit_codes(tree: ast.Module) -> list[int]:
    """Collect the literal integer codes passed to ``sys.exit``/``SystemExit``.

    ``install.py`` raises ``SystemExit(2)`` rather than calling ``sys.exit``,
    so both spellings count.
    """
    codes: set[int] = set()
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and attr_path(node.func) in ("sys.exit", "SystemExit")
        ):
            continue
        if not node.args:
            codes.add(0)
        elif isinstance(node.args[0], ast.Constant) and isinstance(
            node.args[0].value, int
        ):
            codes.add(node.args[0].value)
    return sorted(codes)


def extract_path_constants(tree: ast.Module) -> list[str]:
    """Collect module-level uppercase constants that name a filesystem location.

    A constant qualifies if its value mentions ``Path`` or is derived from a
    constant that already qualified, so chains like ``DATA_DIR = Path.home() /
    ...`` followed by ``ITEMS_FILE = DATA_DIR / "items.json"`` are both caught.
    """
    constants: list[str] = []
    known: set[str] = set()
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            targets = statement.targets
            value = statement.value
        elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
            targets = [statement.target]
            value = statement.value
        else:
            continue
        if len(targets) != 1 or not isinstance(targets[0], ast.Name):
            continue
        name = targets[0].id
        if not name.isupper():
            continue
        rendered = ast.unparse(value)
        referenced = {
            child.id for child in ast.walk(value) if isinstance(child, ast.Name)
        }
        if "Path" in rendered or referenced & known:
            known.add(name)
            constants.append(f"{name} = {rendered}")
    return constants


def extract_internal_imports(tree: ast.Module, siblings: set[str]) -> list[str]:
    """Collect imports that resolve to sibling modules in the same directory."""
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names if alias.name in siblings)
        elif isinstance(node, ast.ImportFrom) and node.module in siblings:
            found.add(node.module)
    return sorted(found)


def extract_api(
    tree: ast.Module, cli: CliSpec | None
) -> tuple[list[ApiSymbol], list[ApiSymbol], list[ApiSymbol], list[str]]:
    """Split module-level public definitions into exceptions/classes/functions.

    Subcommand handlers (``cmd_*``) are pulled out separately: their interface
    is the CLI table, and repeating twenty of them as functions would bury the
    module's actual API.
    """
    exceptions: list[ApiSymbol] = []
    classes: list[ApiSymbol] = []
    functions: list[ApiSymbol] = []
    handlers: list[str] = []
    for statement in tree.body:
        if isinstance(statement, ast.ClassDef):
            if statement.name.startswith("_"):
                continue
            symbol = ApiSymbol(
                render_class(statement), first_sentence(ast.get_docstring(statement))
            )
            (exceptions if is_exception(statement) else classes).append(symbol)
        elif isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef):
            if statement.name.startswith("_") or statement.name == "main":
                continue
            if cli is not None and statement.name.startswith("cmd_"):
                handlers.append(statement.name)
                continue
            functions.append(
                ApiSymbol(
                    render_signature(statement),
                    first_sentence(ast.get_docstring(statement)),
                )
            )
    return exceptions, classes, functions, handlers


def uses_raw_argv(tree: ast.Module) -> bool:
    """Report whether the module reads ``sys.argv`` directly."""
    return any(
        isinstance(node, ast.Attribute) and attr_path(node) == "sys.argv"
        for node in ast.walk(tree)
    )


# ── repository-level inputs ──────────────────────────────────────────────────


def link_gates(entry: dict[str, object]) -> list[str]:
    """Render one links.toml entry's harness/platform/profile conditions."""
    gates: list[str] = []
    for key in ("harness", "platform"):
        value = entry.get(key)
        if isinstance(value, str):
            gates.append(value)
    wsl = entry.get("wsl")
    if isinstance(wsl, str):
        gates.append(f"wsl {wsl}")
    excluded = entry.get("profile_exclude")
    if isinstance(excluded, list):
        gates.extend(
            f"not on {profile}" for profile in excluded if isinstance(profile, str)
        )
    return gates


def load_link_table(repo_root: Path) -> LinkTable:
    """Map each repo-relative source in links.toml to its symlink destinations.

    One source can be linked more than once — ``claude/global-instructions.md``
    lands in four harness homes, and the Copilot hooks repeat the same
    destination once per platform — so destinations are collected per source
    and merged by path, accumulating each entry's gates.
    """
    links_file = repo_root / "links.toml"
    if not links_file.is_file():
        return {}
    data = tomllib.loads(links_file.read_text(encoding="utf-8"))
    table: LinkTable = {}
    for entry in data.get("link", []):
        src = entry.get("src")
        dest = entry.get("dest")
        if not (isinstance(src, str) and isinstance(dest, str)):
            continue
        targets = table.setdefault(src, [])
        existing = next((t for t in targets if t.dest == dest), None)
        if existing is None:
            targets.append(LinkTarget(dest, link_gates(entry)))
            continue
        existing.gates.extend(
            gate for gate in link_gates(entry) if gate not in existing.gates
        )
    return table


def render_link_targets(targets: list[LinkTarget]) -> str:
    """Render a source's install destinations as one comma-joined phrase."""
    if not targets:
        return "not symlinked by `links.toml`"
    return ", ".join(
        f"`{target.dest}` ({', '.join(target.gates) or 'all harnesses'})"
        for target in targets
    )


def read_frontmatter(path: Path) -> dict[str, str]:
    """Read a markdown file's leading ``---`` frontmatter as flat key/value pairs.

    Deliberately minimal: these files only use scalar keys, so this avoids a
    YAML dependency (harness scripts are stdlib-only).
    """
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return {}
    _, _, rest = text.partition("---\n")
    block, separator, _ = rest.partition("\n---")
    if not separator:
        return {}
    fields: dict[str, str] = {}
    for line in block.splitlines():
        if line.startswith((" ", "\t")) or ":" not in line:
            continue
        key, _, value = line.partition(":")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        fields[key.strip()] = value
    return fields


def find_tests(module: Path, repo_root: Path) -> list[str]:
    """Find test modules covering ``module``, by filename and by import.

    Searches the module's own directory and the repo's ``test/`` directory —
    ``install.py``'s tests live in the latter, everything under
    ``claude/scripts/`` is colocated.
    """
    stem = module.stem
    found: set[str] = set()
    pattern = re.compile(rf"^\s*(?:import|from)\s+{re.escape(stem)}\b", re.MULTILINE)
    for directory in (module.parent, repo_root / "test"):
        if not directory.is_dir():
            continue
        direct = directory / f"test_{stem}.py"
        if direct.is_file():
            found.add(direct.relative_to(repo_root).as_posix())
        for candidate in directory.glob("test_*.py"):
            if pattern.search(candidate.read_text(encoding="utf-8")):
                found.add(candidate.relative_to(repo_root).as_posix())
    return sorted(found)


def analyze_module(
    path: Path,
    repo_root: Path,
    siblings: set[str],
    links: LinkTable,
) -> ModuleInterface:
    """Parse one script and collect every statically visible interface fact."""
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    module_doc = ast.get_docstring(tree) or ""
    relpath = path.relative_to(repo_root).as_posix()
    cli = extract_cli(tree, first_paragraph(module_doc))
    exceptions, classes, functions, handlers = extract_api(tree, cli)
    first_line = source.split("\n", 1)[0]
    leaf_docstrings, unmatched_leaves = (
        match_leaf_handlers(tree, cli) if cli is not None else ({}, [])
    )
    return ModuleInterface(
        relpath=relpath,
        purpose=first_paragraph(module_doc) or "(no module docstring)",
        shebang=first_line if first_line.startswith("#!") else None,
        executable=path.stat().st_mode & 0o111 != 0,
        links=links.get(relpath, []),
        cli=cli,
        argv_dispatch=cli is None and uses_raw_argv(tree),
        env_vars=extract_env_vars(tree),
        path_constants=extract_path_constants(tree),
        exit_codes=extract_exit_codes(tree),
        internal_imports=extract_internal_imports(tree, siblings - {path.stem}),
        exceptions=exceptions,
        classes=classes,
        functions=functions,
        handlers=handlers,
        tests=find_tests(path, repo_root),
        leaf_handler_docstrings=leaf_docstrings,
        unmatched_leaf_subcommands=unmatched_leaves,
    )


# ── rendering ────────────────────────────────────────────────────────────────


def inline(text: str) -> str:
    """Flatten text to a single line safe to drop into a markdown bullet."""
    return " ".join(text.split())


def render_cli(module: ModuleInterface, lines: list[str]) -> None:
    """Append the CLI section for one module."""
    if module.cli is None:
        if module.argv_dispatch:
            lines.append(
                "- CLI: hand-rolled `sys.argv` dispatch — not statically "
                "enumerable; see the module docstring above."
            )
        else:
            lines.append("- CLI: none (library module).")
        return
    cli = module.cli
    description = (
        inline(cli.description) if cli.description else "no `description=` set"
    )
    lines.append(f"- CLI (`argparse`): {description}")
    for option in cli.options:
        lines.append(f"  - `{option.label}` {render_notes(option)}".rstrip())
    if not cli.subcommands:
        return
    lines.append("- Subcommands:")
    for subcommand in cli.subcommands:
        usage = " ".join([subcommand.name, *(a.usage for a in subcommand.arguments)])
        summary = f" — {inline(subcommand.help_text)}" if subcommand.help_text else ""
        lines.append(f"  - `{usage}`{summary}")
        for argument in subcommand.arguments:
            detail = render_notes(argument)
            if detail:
                lines.append(f"    - `{argument.label}` {detail}")


def render_notes(argument: CliArgument) -> str:
    """Render an argument's help text and parenthesised extras, if any."""
    parts: list[str] = []
    if argument.help_text:
        parts.append(f"— {inline(argument.help_text)}")
    if argument.notes:
        parts.append(f"({'; '.join(argument.notes)})")
    return " ".join(parts)


def render_module(module: ModuleInterface) -> list[str]:
    """Render one module's full section."""
    lines = [f"### `{module.relpath}`", "", module.purpose, ""]
    lines.append(f"- Installed at: {render_link_targets(module.links)}")
    lines.append(
        f"- Entrypoint: {'executable' if module.executable else 'not executable'}"
        + (f", `{module.shebang}`" if module.shebang else ", no shebang")
    )
    render_cli(module, lines)
    if module.env_vars:
        lines.append(
            "- Environment: " + ", ".join(f"`{name}`" for name in module.env_vars)
        )
    if module.path_constants:
        lines.append("- Filesystem constants:")
        lines.extend(f"  - `{constant}`" for constant in module.path_constants)
    if module.exit_codes:
        lines.append(
            "- Explicit exit codes: "
            + ", ".join(f"`{code}`" for code in module.exit_codes)
        )
    if module.internal_imports:
        lines.append(
            "- Depends on: "
            + ", ".join(f"`{name}.py`" for name in module.internal_imports)
        )
    for heading, symbols in (
        ("Exceptions", module.exceptions),
        ("Public classes", module.classes),
        ("Public functions", module.functions),
    ):
        if not symbols:
            continue
        lines.append(f"- {heading}:")
        for symbol in symbols:
            summary = f" — {inline(symbol.summary)}" if symbol.summary else ""
            lines.append(f"  - `{symbol.signature}`{summary}")
    if module.handlers:
        lines.append(
            "- Subcommand handlers: "
            + ", ".join(f"`{name}`" for name in module.handlers)
        )
    lines.append(
        "- Tested by: " + (", ".join(f"`{name}`" for name in module.tests) or "nothing")
    )
    lines.append("")
    return lines


def render_command_matrix(repo_root: Path, links: LinkTable) -> list[str]:
    """Render the per-harness skill/command parity matrix from frontmatter."""
    locations = {
        "claude": ["claude/commands/{name}.md"],
        "copilot": ["copilot/skills/{name}/SKILL.md"],
        "opencode": ["opencode/command/{name}.md", "opencode/skills/{name}/SKILL.md"],
        "agy": ["agy/skills/{name}/SKILL.md"],
        "pi": ["pi/skills/{name}/SKILL.md", "pi/prompts/{name}.md"],
    }
    names = sorted(
        path.stem for path in (repo_root / "claude" / "commands").glob("*.md")
    )
    lines = [
        "Each harness gets a port of the same skill surface. Presence below is",
        "the file existing in the repo; the description is the canonical",
        "`claude/commands/` frontmatter.",
        "",
        "| Skill | " + " | ".join(HARNESS_DIRS) + " |",
        "| --- | " + " | ".join("---" for _ in HARNESS_DIRS) + " |",
    ]
    for name in names:
        cells: list[str] = []
        for harness in HARNESS_DIRS:
            present = any(
                (repo_root / template.format(name=name)).is_file()
                for template in locations[harness]
            )
            cells.append("yes" if present else "—")
        lines.append(f"| `/{name}` | " + " | ".join(cells) + " |")
    lines.append("")
    for name in names:
        source = repo_root / "claude" / "commands" / f"{name}.md"
        fields = read_frontmatter(source)
        description = fields.get("description", "(no frontmatter description)")
        relpath = source.relative_to(repo_root).as_posix()
        lines.append(f"- **`/{name}`** — {inline(description)}")
        lines.append(f"  - Source: `{relpath}`")
        lines.append(f"  - Installed at: {render_link_targets(links.get(relpath, []))}")
    lines.append("")
    return lines


SKILL_DIR_MARKERS = ("/commands/", "/command/", "/skills/")


def is_generated_artifact(relpath: str) -> bool:
    """Report whether a path is build output or a dotfile rather than a source."""
    parts = relpath.split("/")
    return any(part == "__pycache__" or part.startswith(".") for part in parts)


def tracked_files(repo_root: Path) -> set[str] | None:
    """Return every git-tracked path under ``repo_root``, or None if unavailable.

    The asset table below walks the filesystem, which would otherwise pull in
    whatever untracked cruft happens to sit in one machine's working copy —
    an editor backup, a stray ``settings.json.bak.<stamp>`` — and bake it into
    the committed inventory. That makes the document depend on *which checkout*
    generated it: ``--check`` then fails in every other clean checkout and in
    CI, which is exactly what happened once.

    Git's index is the authoritative answer to "is this file part of the repo,"
    so it's what the filter uses. Shelling out to ``git`` does not contradict
    this module's no-subprocess rule: that rule exists so documenting a harness
    script never *executes* it (they mutate live state under ``~/.claude``).
    ``git ls-files`` is read-only and is not a harness script.

    Returns None when git isn't available or this isn't a checkout, so the
    generator still works standalone — it just falls back to the unfiltered
    filesystem walk.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "ls-files", "-z"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return {entry for entry in result.stdout.split("\0") if entry}


def render_assets(
    repo_root: Path, links: LinkTable, tracked: set[str] | None = None
) -> list[str]:
    """Render the non-Python, non-skill harness assets and where they install.

    ``tracked`` restricts the walk to git-tracked paths; None means git was
    unavailable, in which case every file on disk is listed (see
    :func:`tracked_files`).
    """
    lines = [
        "Everything under the harness directories that is neither a shared script",
        "(section 1) nor a skill document (section 2). A source with no",
        "destination is either read by another file in the repo or seeded by",
        "install.py rather than symlinked — `settings.json` and `opencode.jsonc`",
        "are copy-once seeds for exactly that reason.",
        "",
        "| Source | Installed at |",
        "| --- | --- |",
    ]
    for harness in HARNESS_DIRS:
        base = repo_root / harness
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file() or path.suffix == ".py":
                continue
            relpath = path.relative_to(repo_root).as_posix()
            if tracked is not None and relpath not in tracked:
                continue
            if is_generated_artifact(relpath) or any(
                marker in relpath for marker in SKILL_DIR_MARKERS
            ):
                continue
            lines.append(
                f"| `{relpath}` | {render_link_targets(links.get(relpath, []))} |"
            )
    lines.append("")
    return lines


# ── skill/command doc contract drift ────────────────────────────────────────

CONTRACT_FINGERPRINTS_PATH = "claude/scripts/contract_fingerprints.json"
"""Where the recorded per-script behavior fingerprints live.

Covers ``second_opinion.py`` too — the general fingerprint check
(:func:`check_contract_fingerprints`) subsumes what
``gen_second_opinion.py``'s narrower ``GUARD_PHRASES``/``CONTRACT_TOKENS``
used to guard, so there is no longer a separate exclusion for it here.
"""

SHELL_PROMPT_TOKENS = frozenset({"$", "#", "&&", ";", "|", "|&"})
INTERPRETER_PREFIXES = frozenset({"python3", "python", "uv", "run", "bun", "node"})

_CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")
_FENCED_BLOCK_RE = re.compile(r"```[a-zA-Z0-9]*\n(.*?)```", re.DOTALL)
_ENV_ASSIGNMENT_RE = re.compile(r"[A-Z_][A-Z0-9_]*=\S*")
_COMMAND_SHAPED_RE = re.compile(r"[A-Za-z0-9_-]+")


@dataclass
class DocInvocation:
    """One doc's code-span example of running a target script."""

    doc: str
    raw: str
    tokens: list[str]


@dataclass
class DriftProblem:
    """One doc invocation using a subcommand/flag the script doesn't have."""

    script: str
    doc: str
    invocation: str
    detail: str


def discover_doc_paths(repo_root: Path) -> list[Path]:
    """Return every skill/command doc across the four harnesses, sorted."""
    paths: list[Path] = []
    for harness in HARNESS_DIRS:
        base = repo_root / harness
        paths += (base / "commands").glob("*.md")
        paths += (base / "command").glob("*.md")
        paths += (base / "skills").glob("*/SKILL.md")
    return sorted(set(paths))


def code_regions(text: str) -> list[str]:
    """Return every inline code span and fenced code block's inner text."""
    regions = [m.group(1) for m in _CODE_SPAN_RE.finditer(text)]
    regions += [m.group(1) for m in _FENCED_BLOCK_RE.finditer(text)]
    return regions


def tokenize_invocation_line(line: str) -> list[str]:
    """Shell-tokenize one line, after stripping argparse-usage brackets.

    ``[--date YYYY-MM-DD]`` is optionality markup doc authors copy from
    ``--help`` output, never literal shell syntax; left in place it breaks
    flag-token recognition (the leading ``[`` makes ``[--date`` fail the
    "starts with a hyphen" check).
    """
    line = line.replace("[", "").replace("]", "")
    try:
        return shlex.split(line, posix=True)
    except ValueError:
        return line.split()


def invocation_tokens(tokens: list[str], script_basename: str) -> list[str] | None:
    """Return the token stream starting at ``script_basename``, or None.

    Strips known shell-prompt/separator tokens, interpreter/runner prefixes,
    and ``NAME=VALUE`` environment assignments from the front, then requires
    the script's basename as the next token — rejecting lines where the
    script name is not effectively first (``cp dev_status.py backup_dir``)
    while still matching ``$ dev_status.py show 5`` or
    ``python3 ~/.claude/scripts/dev_status.py show 5``.
    """
    for index, token in enumerate(tokens):
        if Path(token).name == script_basename:
            return tokens[index:]
        if token in SHELL_PROMPT_TOKENS or token in INTERPRETER_PREFIXES:
            continue
        if _ENV_ASSIGNMENT_RE.fullmatch(token):
            continue
        return None
    return None


def find_invocations(
    doc_paths: Sequence[Path], repo_root: Path, script_basename: str
) -> list[DocInvocation]:
    """Scan every doc for code-span invocations of ``script_basename``."""
    found: list[DocInvocation] = []
    for doc_path in doc_paths:
        text = doc_path.read_text(encoding="utf-8", errors="replace")
        if script_basename not in text:
            continue
        relpath = doc_path.relative_to(repo_root).as_posix()
        for region in code_regions(text):
            for raw_line in region.splitlines():
                line = raw_line.rstrip("\\").strip()
                if not line or script_basename not in line:
                    continue
                tokens = tokenize_invocation_line(line)
                matched = invocation_tokens(tokens, script_basename)
                if matched is not None:
                    found.append(DocInvocation(doc=relpath, raw=line, tokens=matched))
    return found


def flag_takes_value(argument: CliArgument) -> bool:
    """Report whether ``argument`` consumes a following token as its value.

    Inferred from the already-extracted ``usage`` text rather than
    re-deriving it: a valueless flag renders as just the flag name
    (``[--quiet]``), while a value-taking one always has a trailing
    metavar or choice set separated by a space (``[--if-rev <N>]``,
    ``[--date YYYY-MM-DD]``, ``[--status {open,closed}]``), whether or not
    the metavar itself uses angle brackets.
    """
    return " " in argument.usage.strip("[]")


def validate_invocation(cli: CliSpec, tokens: list[str]) -> list[str]:
    """Walk one invocation's tokens against ``cli``; return problem strings.

    Left to right, not bucketed into "subcommand tokens" and "flag tokens"
    upfront — bucketing loses the adjacency between a flag and its value
    (e.g. the ``5`` in ``start 4 --if-rev 5`` would land in the wrong
    bucket). Consumes a subcommand path to a leaf first, then walks the
    remaining tokens one at a time, consuming a value-taking flag's next
    token as its value rather than validating it in its own right.
    """
    rest = tokens[1:]
    path: tuple[str, ...] = ()
    leaf: Subcommand | None = None
    valid_args: list[CliArgument]

    if cli.subcommands:
        known_paths = {sc.path for sc in cli.subcommands}
        by_path = {sc.path: sc for sc in cli.subcommands}
        consumed = 0
        while consumed < len(rest) and not rest[consumed].startswith("-"):
            candidate = path + (rest[consumed],)
            if any(known[: len(candidate)] == candidate for known in known_paths):
                path = candidate
                leaf = by_path.get(path)
                consumed += 1
                continue
            break
        if path and leaf is None:
            return [f"unknown subcommand path {' '.join(path)!r}"]
        if not path:
            if (
                rest
                and not rest[0].startswith("-")
                and _COMMAND_SHAPED_RE.fullmatch(rest[0])
            ):
                return [f"unknown subcommand {rest[0]!r}"]
            valid_args = cli.options
        else:
            rest = rest[consumed:]
            valid_args = leaf.arguments if leaf else []
    else:
        valid_args = cli.options

    flags: dict[str, CliArgument] = {}
    for argument in valid_args:
        for name in argument.label.split("/"):
            flags[name] = argument

    problems: list[str] = []
    index = 0
    while index < len(rest):
        token = rest[index]
        if token == "--":
            break
        if token.startswith("-"):
            name, _, inline_value = token.partition("=")
            argument = flags.get(name)
            if argument is None:
                where = " ".join(path) or "(top level)"
                problems.append(f"unknown flag {name!r} for {where!r}")
                index += 1
                continue
            index += 2 if (flag_takes_value(argument) and not inline_value) else 1
        else:
            index += 1
    return problems


def check_doc_drift(
    repo_root: Path, modules: Sequence[ModuleInterface]
) -> tuple[list[DriftProblem], dict[str, dict[str, bool]]]:
    """Validate every doc's shown invocations against each module's ``CliSpec``.

    Returns the concrete problems found, plus a per-script per-doc
    OK/missing summary for the rendered coverage section.
    """
    doc_paths = discover_doc_paths(repo_root)
    problems: list[DriftProblem] = []
    coverage: dict[str, dict[str, bool]] = {}
    for module in modules:
        if module.cli is None:
            continue
        script = Path(module.relpath).name
        invocations = find_invocations(doc_paths, repo_root, script)
        if not invocations:
            continue
        per_doc_ok: dict[str, bool] = {}
        for invocation in invocations:
            ok = per_doc_ok.get(invocation.doc, True)
            for issue in validate_invocation(module.cli, invocation.tokens):
                problems.append(
                    DriftProblem(
                        script=script,
                        doc=invocation.doc,
                        invocation=invocation.raw,
                        detail=issue,
                    )
                )
                ok = False
            per_doc_ok[invocation.doc] = ok
        coverage[script] = per_doc_ok
    return problems, coverage


def render_doc_drift_section(coverage: dict[str, dict[str, bool]]) -> list[str]:
    """Render the per-script per-doc coverage summary, sorted for determinism."""
    lines = [
        "For each backing script below, every skill/command doc that shows an",
        "example of running it, and whether that example's subcommand and",
        "flags still match the script's real CLI contract. A doc with no shown",
        "invocation of a given script is not listed. `--check` exits `3` (not",
        "`1`) when this section would change, since the fix is editing the",
        "named doc, not regenerating this file.",
        "",
    ]
    for script in sorted(coverage):
        lines.append(f"### `{script}`")
        lines.append("")
        lines.append("| Doc | Status |")
        lines.append("| --- | --- |")
        for doc in sorted(coverage[script]):
            status = "OK" if coverage[script][doc] else "MISSING"
            lines.append(f"| `{doc}` | {status} |")
        lines.append("")
    return lines


# ── contract fingerprints (semantic doc-drift check) ────────────────────────


@dataclass
class FingerprintProblem:
    """One contract-fingerprint failure for ``--check`` to report."""

    script: str
    kind: str  # "unmatched-handler" | "mismatch"
    message: str


def load_contract_fingerprints(repo_root: Path) -> dict[str, list[str]]:
    """Read ``contract_fingerprints.json``, treating anything unreadable as empty.

    A missing or malformed file is not a crash — it just means every
    in-scope script looks like it has no prior fingerprint recorded, the
    same outcome as a script newly entering scope.
    """
    path = repo_root / CONTRACT_FINGERPRINTS_PATH
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {
        key: value
        for key, value in data.items()
        if isinstance(key, str)
        and isinstance(value, list)
        and all(isinstance(item, str) for item in value)
    }


def load_repo_modules(repo_root: Path, links: LinkTable) -> list[ModuleInterface]:
    """Parse every shared script under ``SCRIPTS_DIR`` into a ``ModuleInterface``."""
    script_dir = repo_root / SCRIPTS_DIR
    modules_paths = sorted(
        path for path in script_dir.glob("*.py") if not path.name.startswith("test_")
    )
    siblings = {path.stem for path in script_dir.glob("*.py")}
    return [analyze_module(path, repo_root, siblings, links) for path in modules_paths]


def check_contract_fingerprints(
    repo_root: Path,
    modules: Sequence[ModuleInterface],
    coverage: dict[str, dict[str, bool]],
) -> list[FingerprintProblem]:
    """Check every in-scope script's live fingerprint against the recorded one.

    Scope is exactly ``coverage``'s keys — the same scripts
    :func:`check_doc_drift` already found at least one doc invocation for,
    never a separately maintained list. A script with an unmatched leaf
    handler aborts before any fingerprint comparison for it, reported
    distinctly from an ordinary mismatch (the fix is the extractor's naming
    assumption, not a doc).
    """
    modules_by_name = {Path(module.relpath).name: module for module in modules}
    recorded = load_contract_fingerprints(repo_root)
    problems: list[FingerprintProblem] = []
    for script in sorted(coverage):
        module = modules_by_name.get(script)
        if module is None or module.cli is None:
            continue
        docs = sorted(coverage[script])
        if module.unmatched_leaf_subcommands:
            names = ", ".join(
                " ".join(path) for path in module.unmatched_leaf_subcommands
            )
            problems.append(
                FingerprintProblem(
                    script=script,
                    kind="unmatched-handler",
                    message=(
                        f"[gen_interfaces] contract-fingerprint extraction failed "
                        f"for `{script}`: no cmd_<path> handler found for "
                        f"subcommand(s) {names} — fix the extractor's naming "
                        "assumption, this is not a doc to re-read"
                    ),
                )
            )
            continue
        live = fingerprint_lines(
            module.cli,
            module.leaf_handler_docstrings,
            module.purpose,
            module.exit_codes,
        )
        stored = recorded.get(script)
        if stored == live:
            continue
        docs_text = ", ".join(f"`{doc}`" for doc in docs) or "(no doc found)"
        if stored is None:
            detail = (
                f"no prior fingerprint recorded for `{script}` — this is expected "
                "the first time a doc references it"
            )
        else:
            detail = "".join(
                difflib.unified_diff(
                    [line + "\n" for line in stored],
                    [line + "\n" for line in live],
                    fromfile=f"{script} (recorded)",
                    tofile=f"{script} (current)",
                )
            )
        problems.append(
            FingerprintProblem(
                script=script,
                kind="mismatch",
                message=(
                    f"[gen_interfaces] contract drift — `{script}`'s CLI behavior "
                    f"changed; re-read {docs_text}, then run `gen_interfaces.py "
                    f"--update-fingerprints`:\n{detail}"
                ),
            )
        )
    return problems


def write_contract_fingerprints(repo_root: Path) -> dict[str, list[str]]:
    """Recompute and write ``contract_fingerprints.json`` for ``--update-fingerprints``.

    Covers exactly the scripts :func:`check_doc_drift` finds a doc
    invocation for (the same scope `--check` uses) — a script no longer
    referenced by any doc drops out; one referenced for the first time is
    added. A script with an unmatched leaf handler is skipped rather than
    given an untrustworthy entry — `--check` keeps failing it with the
    distinct unmatched-handler message until the extractor's assumption is
    fixed.
    """
    links = load_link_table(repo_root)
    modules = load_repo_modules(repo_root, links)
    _, coverage = check_doc_drift(repo_root, modules)
    modules_by_name = {Path(module.relpath).name: module for module in modules}
    fingerprints: dict[str, list[str]] = {}
    for script in sorted(coverage):
        module = modules_by_name.get(script)
        if module is None or module.cli is None or module.unmatched_leaf_subcommands:
            continue
        fingerprints[script] = fingerprint_lines(
            module.cli,
            module.leaf_handler_docstrings,
            module.purpose,
            module.exit_codes,
        )
    path = repo_root / CONTRACT_FINGERPRINTS_PATH
    path.write_text(
        json.dumps(fingerprints, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return fingerprints


_SKILL_MENTION_RE_CACHE: dict[str, re.Pattern[str]] = {}


def _skill_mention_pattern(name: str) -> re.Pattern[str]:
    """Return (and cache) the whole-word mention pattern for a skill name."""
    if name not in _SKILL_MENTION_RE_CACHE:
        _SKILL_MENTION_RE_CACHE[name] = re.compile(
            rf"(?<![\w-]){re.escape(name)}(?![\w-])"
        )
    return _SKILL_MENTION_RE_CACHE[name]


def extract_skill_mentions(source: Path, names: Sequence[str]) -> list[str]:
    """Return which other skill names this skill's own file text mentions.

    A whole-word match only — this is deliberately conservative (a hyphen or
    letter on either side disqualifies it) so a short name like `spec`
    doesn't false-positive on "specific" or "specification".
    """
    text = source.read_text(encoding="utf-8")
    mentioned = []
    for other in names:
        if other == source.stem:
            continue
        if _skill_mention_pattern(other).search(text):
            mentioned.append(other)
    return mentioned


def render_skill_graph_section(repo_root: Path) -> list[str]:
    """Render which skill mentions which other skills, by name, in its own text."""
    names = sorted(
        path.stem for path in (repo_root / "claude" / "commands").glob("*.md")
    )
    lines = [
        "Built by scanning each `claude/commands/*.md` skill's own text for",
        "whole-word mentions of the other skills' names (frontmatter",
        "description included). This regenerates with the rest of the file,",
        "so it cannot silently drift the way hand-written relationship notes",
        "could — if a skill stops mentioning another, or starts mentioning a",
        "new one, `--check` catches it the same as any other stale content.",
        "",
        "| Skill | Mentions |",
        "| --- | --- |",
    ]
    for name in names:
        source = repo_root / "claude" / "commands" / f"{name}.md"
        mentions = extract_skill_mentions(source, names)
        cell = ", ".join(f"`{m}`" for m in mentions) if mentions else "—"
        lines.append(f"| `/{name}` | {cell} |")
    lines.append("")
    return lines


def build_document(repo_root: Path) -> str:
    """Build the complete INTERFACES.md text for ``repo_root``."""
    return build_document_and_drift(repo_root)[0]


def build_document_and_drift(
    repo_root: Path,
) -> tuple[str, list[DriftProblem], list[FingerprintProblem]]:
    """Build INTERFACES.md text alongside the doc-drift and fingerprint problems.

    A single pass so ``--check`` doesn't re-parse every script twice.
    """
    links = load_link_table(repo_root)
    modules = load_repo_modules(repo_root, links)

    lines = [f"# {OUTPUT_NAME}", "", PREAMBLE, "", "---", ""]
    lines += ["## 1. Shared scripts (`claude/scripts/`)", ""]
    lines += ["| Module | Purpose |", "| --- | --- |"]
    for module in modules:
        name = Path(module.relpath).name
        lines.append(f"| [`{name}`](#{anchor(module.relpath)}) | {module.purpose} |")
    lines.append("")
    for module in modules:
        lines += render_module(module)
    lines += ["---", "", "## 2. Skill and command surface", ""]
    lines += render_command_matrix(repo_root, links)
    lines += ["---", "", "## 3. Other harness assets", ""]
    lines += render_assets(repo_root, links, tracked_files(repo_root))

    entrypoints = [
        analyze_module(repo_root / name, repo_root, set(), links)
        for name in ROOT_ENTRYPOINTS
        if (repo_root / name).is_file()
    ]
    if entrypoints:
        lines += [
            "---",
            "",
            "## 4. Installer entrypoints",
            "",
            "Not harness code, but the plumbing that puts everything above in",
            "place. `install.sh` is a POSIX-sh bootstrap with no interface of its",
            "own: it locates a Python 3.12+ interpreter and execs `install.py`,",
            "forwarding argv unchanged.",
            "",
        ]
        for module in entrypoints:
            lines += render_module(module)

    problems, coverage = check_doc_drift(repo_root, modules)
    fingerprint_problems = check_contract_fingerprints(repo_root, modules, coverage)
    lines += ["---", "", "## 5. Skill/command doc contract coverage", ""]
    lines += render_doc_drift_section(coverage)
    lines += ["---", "", "## 6. Skill cross-reference graph", ""]
    lines += render_skill_graph_section(repo_root)
    return "\n".join(lines).rstrip("\n") + "\n", problems, fingerprint_problems


def anchor(relpath: str) -> str:
    """Return the GitHub heading anchor for a module section."""
    return re.sub(r"[^a-z0-9]+", "", relpath.lower().replace("/", "").replace(".", ""))


# ── cli ──────────────────────────────────────────────────────────────────────


def default_repo_root() -> Path:
    """Return the repo root inferred from this script's real location."""
    return Path(__file__).resolve().parents[2]


def main() -> None:
    """Parse argv, then regenerate, check, or print the interface inventory."""
    parser = argparse.ArgumentParser(
        prog="gen_interfaces",
        description="regenerate INTERFACES.md from the harness sources",
    )
    cli_common.add_verbosity_args(parser)
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "exit 3 (with a report on stderr) if a doc's shown command example "
            "no longer matches its script, else exit 1 if the file on disk is stale"
        ),
    )
    parser.add_argument(
        "--stdout", action="store_true", help="print the document, write nothing"
    )
    parser.add_argument(
        "--update-fingerprints",
        action="store_true",
        help=(
            "recompute and record every in-scope script's contract fingerprint "
            "in contract_fingerprints.json, accepting its current behavior as "
            "the new baseline — run this only after re-reading the --check "
            "diff, never blind"
        ),
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        metavar="<path>",
        help="repository root (default: inferred from this script's path)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        metavar="<path>",
        help=f"output file (default: <repo-root>/{OUTPUT_NAME})",
    )
    args = parser.parse_args()

    repo_root = (args.repo_root or default_repo_root()).resolve()
    if not (repo_root / SCRIPTS_DIR).is_dir():
        print(f"[gen_interfaces] no {SCRIPTS_DIR}/ under {repo_root}", file=sys.stderr)
        sys.exit(2)
    output = args.output or repo_root / OUTPUT_NAME

    if args.update_fingerprints:
        written = write_contract_fingerprints(repo_root)
        cli_common.qprint(
            f"[gen_interfaces] recorded contract fingerprints for {len(written)} "
            f"script(s) in {CONTRACT_FINGERPRINTS_PATH}",
            quiet=args.quiet,
        )
        return

    document, drift_problems, fingerprint_problems = build_document_and_drift(repo_root)

    if args.stdout:
        sys.stdout.write(document)
        return
    if args.check:
        if drift_problems or fingerprint_problems:
            for problem in drift_problems:
                print(
                    f"[gen_interfaces] doc drift — edit `{problem.doc}`: "
                    f"`{problem.invocation}` -> {problem.detail}",
                    file=sys.stderr,
                )
            for fp_problem in fingerprint_problems:
                print(fp_problem.message, file=sys.stderr)
            print(
                "[gen_interfaces] the script's real CLI contract is the source "
                "of truth — update the doc(s) above, do not regenerate "
                f"{output.name} to fix this",
                file=sys.stderr,
            )
            sys.exit(3)
        current = output.read_text(encoding="utf-8") if output.is_file() else ""
        if current == document:
            cli_common.qprint(
                f"[gen_interfaces] {output.name} is up to date",
                quiet=args.quiet,
            )
            return
        diff = difflib.unified_diff(
            current.splitlines(keepends=True),
            document.splitlines(keepends=True),
            fromfile=f"{output.name} (on disk)",
            tofile=f"{output.name} (generated)",
        )
        sys.stderr.writelines(diff)
        print(
            f"[gen_interfaces] {output.name} is stale — run "
            "`python3 claude/scripts/gen_interfaces.py`",
            file=sys.stderr,
        )
        sys.exit(1)

    output.write_text(document, encoding="utf-8")
    cli_common.qprint(f"[gen_interfaces] wrote {output}", quiet=args.quiet)


if __name__ == "__main__":
    main()
