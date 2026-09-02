#!/usr/bin/env python3
"""Generate a zsh `#compdef` completion file for a harness CLI.

Supports all five harness CLIs in this repo — `claude`, `copilot`, `agy`,
`opencode`, `pi` — via a small per-harness adapter registry rather than a
single auto-detecting parser: each harness's `--help` output has a
genuinely different shape (Commander.js for claude/copilot, Go's `flag`
package for agy, yargs for opencode, which ships its own native completion
generator; Pi has no native completion command, but its `--help` shape is
close enough to Commander's to reuse that adapter with one accommodation —
see the `pi` entry in `HARNESSES` and `parse_commands`'s `strip_cli`).

Usage:
    python3 ~/.claude/scripts/gen_shell_completion.py --harness agy
    python3 ~/.claude/scripts/gen_shell_completion.py --harness all
    python3 ~/.claude/scripts/gen_shell_completion.py --harness claude --stdout

Writes to ~/.zsh/completions/_<cli> by default.

Flags
  --quiet, -q    suppress non-essential output
  --verbose, -v  emit extra diagnostic messages to stderr
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import cli_common

DEFAULT_OUT_DIR = Path.home() / ".zsh/completions"

SECTION_RE = re.compile(r"^(Arguments|Options|Commands):\s*$")
OPT_START_RE = re.compile(r"^ {2}(-\S)")  # option line begins with "  -"
CMD_START_RE = re.compile(r"^ {2}(\S)")  # command line begins with "  <nonspace>"
DEF_DESC_SPLIT = re.compile(r"^(.*?)(\s{2,})(.*)$")
CHOICES_RE = re.compile(r"\(choices:\s*([^)]+)\)")
BRACKET_ATTACHED_RE = re.compile(r"^(-[\w-]+)(\[.*\])$")
# Commander command/alias tokens are lowercase words, optionally with dashes/digits
# and pipe-separated aliases. "Examples:" and other spurious tokens must be rejected.
CMD_NAME_RE = re.compile(r"^[a-z][a-z0-9-]*(\|[a-z][a-z0-9-]*)*$")
USAGE_RE = re.compile(r"^Usage:\s+(.+?)(?:\s+\[options\]|\s+\[command\]|\s*$)")

GOFLAG_SECTION_RE = re.compile(r"^(Flags|Commands|Available subcommands):\s*$")

MAX_DEPTH = 6


@dataclass
class HarnessSpec:
    cli: str  # binary name, e.g. "agy"
    format: str  # "commander" | "go-flag" | "native-passthrough"
    native_command: list[str] | None = None  # e.g. ["completion"] for opencode


HARNESSES: dict[str, HarnessSpec] = {
    "claude": HarnessSpec(cli="claude", format="commander"),
    "copilot": HarnessSpec(cli="copilot", format="commander"),
    "agy": HarnessSpec(cli="agy", format="go-flag"),
    "opencode": HarnessSpec(
        cli="opencode", format="native-passthrough", native_command=["completion"]
    ),
    # Pi has no native completion subcommand (`pi --help` / `pi config --help`
    # checked, neither lists one). Its --help output is otherwise close
    # enough to Commander's shape (Usage:/Commands:/Options: sections, same
    # 2-space option/command indent) to reuse the "commander" adapter,
    # except every Commands-section line repeats "pi" as its own first
    # token ("  pi install <source> ...") where claude/copilot start
    # straight with the subcommand name — parse_commands's strip_cli param
    # exists specifically to strip that repeated token for this harness.
    "pi": HarnessSpec(cli="pi", format="commander"),
}


@dataclass
class Option:
    names: list[str]
    takes_arg: bool
    arg_placeholder: str | None
    choices: list[str] | None
    desc: str


@dataclass
class Node:
    path: tuple[str, ...]
    desc: str = ""
    options: list[Option] = field(default_factory=list)
    subcommands: dict[str, Node] = field(default_factory=dict)
    aliases: list[str] = field(default_factory=list)


def _run(argv: list[str], *, verbose: bool = False) -> str:
    try:
        r = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except Exception as e:
        cli_common.vprint(
            f"warn: command failed: {' '.join(argv)}: {e}", verbose=verbose
        )
        return ""
    else:
        # Go's `flag` package (agy) writes --help/usage text to stderr, not
        # stdout; other harnesses use stdout. Fall back to stderr only when
        # stdout is empty so a genuine stdout-emitting harness is unaffected.
        return r.stdout or r.stderr or ""


def run_help(cli: str, path: list[str], *, verbose: bool = False) -> str:
    return _run([cli, *path, "--help"], verbose=verbose)


def run_goflag_subcommand_help(cli: str, name: str, *, verbose: bool = False) -> str:
    return _run([cli, "help", name], verbose=verbose)


# -- Shared helpers (commander + go-flag both use these) -------------------


def collect_sections(text: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        m = SECTION_RE.match(line)
        if m:
            current = m.group(1)
            sections.setdefault(current, [])
            continue
        if current is None:
            continue
        if line.strip() == "":
            # Blank lines don't end a section; skip.
            continue
        if line.startswith("  "):
            sections[current].append(line)
        else:
            current = None
    return sections


def _group_indented(
    lines: list[str], is_new_entry: Callable[[str], bool]
) -> list[list[str]]:
    """Group wrapped continuation lines under their entry's first line.

    Kept as separate raw lines (not flattened into one string) because some
    commander entries put the whole description on continuation lines with
    no inline gap on the first line at all — a long flag name plus
    placeholder overflows the column, so commander drops straight to the
    next line instead of padding a gap. A caller that needs a single string
    must decide for itself where the defn/desc boundary falls; joining here
    would erase that distinction and make a 2+-space-gap split impossible to
    find.
    """
    entries: list[list[str]] = []
    cur: list[str] | None = None
    for line in lines:
        if is_new_entry(line):
            if cur is not None:
                entries.append(cur)
            cur = [line]
        else:
            if cur is not None:
                cur.append(line)
    if cur is not None:
        entries.append(cur)
    return entries


def _split_entry(entry_lines: list[str]) -> tuple[str, str]:
    """Split a grouped entry into (defn, desc).

    The common case is a 2+-space gap between defn and desc on the first
    line, with any continuation lines extending desc. When the first line
    has no such gap (name+placeholder alone filled the line), the whole
    first line is the defn and desc comes entirely from continuation lines.
    """
    first = entry_lines[0].strip()
    rest = " ".join(line.strip() for line in entry_lines[1:])
    m = DEF_DESC_SPLIT.match(first)
    if m:
        defn, _, desc_head = m.groups()
        desc = f"{desc_head.strip()} {rest}".strip() if rest else desc_head.strip()
        return defn.strip(), desc
    return first, rest


def _split_name_and_placeholder(token: str) -> tuple[str, str | None]:
    """Split a single option token into (flag name, placeholder).

    Commander separates name and placeholder with whitespace (`--flag
    <value>`); copilot also attaches a placeholder directly to the flag with
    no whitespace (`--flag[=value]`). Try the bracket-attached form first
    since it has no internal whitespace to split on — a plain `.split(None,
    1)` would otherwise capture the whole `--flag[=value...]` string as the
    flag name.
    """
    m = BRACKET_ATTACHED_RE.match(token)
    if m:
        return m.group(1), m.group(2)
    toks = token.split(None, 1)
    if not toks:
        return "", None
    return toks[0], toks[1].strip() if len(toks) > 1 else None


def parse_options(lines: list[str]) -> list[Option]:
    entries = _group_indented(lines, lambda ln: bool(OPT_START_RE.match(ln)))
    out: list[Option] = []
    for entry in entries:
        defn, desc = _split_entry(entry)
        if not defn:
            continue
        parts = [p.strip() for p in defn.split(",")]
        names: list[str] = []
        placeholder: str | None = None
        for p in parts:
            name, ph = _split_name_and_placeholder(p)
            if not name.startswith("-"):
                continue
            names.append(name)
            if placeholder is None and ph is not None:
                placeholder = ph
        if not names:
            continue
        takes_arg = placeholder is not None and placeholder.startswith(("<", "["))
        choices: list[str] | None = None
        cm = CHOICES_RE.search(desc)
        if cm:
            choices = [c.strip().strip('"').strip("'") for c in cm.group(1).split(",")]
            choices = [c for c in choices if c]
        out.append(
            Option(
                names=names,
                takes_arg=takes_arg,
                arg_placeholder=placeholder,
                choices=choices,
                desc=desc.strip(),
            )
        )
    return out


def parse_commands(
    lines: list[str], *, strip_cli: str | None = None
) -> list[tuple[str, list[str], str]]:
    """Return list of (primary_name, aliases, description).

    ``strip_cli``: Pi's Commands section repeats its own binary name as the
    first token of every line ("  pi install <source> ...  Install..."),
    unlike claude/copilot/agy, whose command lines start directly with the
    subcommand name. When set, a leading token equal to ``strip_cli`` is
    dropped before tokenizing, so the real subcommand name is parsed
    instead of the repeated binary name. ``None`` (the default) leaves
    existing callers' behavior unchanged.
    """
    entries = _group_indented(lines, lambda ln: bool(CMD_START_RE.match(ln)))
    out: list[tuple[str, list[str], str]] = []
    for entry in entries:
        defn, desc = _split_entry(entry)
        if not defn:
            continue
        tokens = defn.strip().split()
        if not tokens:
            continue
        if strip_cli is not None and len(tokens) > 1 and tokens[0] == strip_cli:
            tokens = tokens[1:]
        head = tokens[0]
        # Reject anything that isn't a valid command token. This filters out
        # "Examples:" and similar spurious tokens that arise when `--help`
        # intersperses example blocks at column 2.
        if not CMD_NAME_RE.match(head):
            continue
        parts = head.split("|")
        name = parts[0]
        aliases = parts[1:]
        if name == "help":
            continue
        out.append((name, aliases, desc.strip()))
    return out


def is_dir_option(opt: Option) -> bool:
    text = (opt.arg_placeholder or "") + " " + " ".join(opt.names)
    text = text.lower()
    return any(
        kw in text for kw in ("<dir", "<directory", "<directories", "--add-dir", "-dir")
    )


def is_file_option(opt: Option) -> bool:
    if is_dir_option(opt):
        return False
    text = (opt.arg_placeholder or "") + " " + " ".join(opt.names)
    text = text.lower()
    return any(
        kw in text
        for kw in (
            "<file",
            "<path",
            "<configs",
            "<config",
            "--settings",
            "--mcp-config",
            "--debug-file",
            "--file",
            "--json-schema",
        )
    )


# -- Commander adapter (claude, copilot) -----------------------------------


def help_matches_path(cli: str, text: str, path: tuple[str, ...]) -> bool:
    """Check that the help output's `Usage:` line reflects the path we asked for.

    Commander re-prints the parent's help when given an unknown subcommand, so
    a mismatch here means the path is bogus and we must not recurse further.
    """
    for line in text.splitlines():
        m = USAGE_RE.match(line)
        if not m:
            continue
        usage_cmd = m.group(1).strip()
        tokens = usage_cmd.split()
        # tokens[0] is the cli name; remainder should be the path (commander
        # may drop trailing positionals like "[command]" which USAGE_RE
        # already trims, but placeholders like "<name>" may still appear).
        if not tokens or tokens[0] != cli:
            return True  # Unrecognized format — don't block.
        usage_tokens = [t for t in tokens[1:] if not t.startswith(("<", "["))]
        if len(usage_tokens) != len(path):
            return False
        # Each token may carry commander aliases ("plugin|plugins"); any match counts.
        return all(want in got.split("|") for want, got in zip(path, usage_tokens))
    return True


def build_tree(
    cli: str,
    path: tuple[str, ...],
    seen: set[tuple[str, ...]],
    *,
    verbose: bool = False,
) -> Node:
    if path in seen:
        return Node(path=path)
    seen.add(path)
    if len(path) > MAX_DEPTH:
        cli_common.vprint(
            f"warn: max depth exceeded at {' '.join(path)}", verbose=verbose
        )
        return Node(path=path)
    text = run_help(cli, list(path), verbose=verbose)
    if path and not help_matches_path(cli, text, path):
        # Commander fell back to a parent's help — this path isn't real.
        cli_common.vprint(
            f"warn: {' '.join(path)} is not a real subcommand; skipping",
            verbose=verbose,
        )
        return Node(path=path)
    sections = collect_sections(text)
    node = Node(path=path)
    node.options = parse_options(sections.get("Options", []))
    for name, aliases, desc in parse_commands(
        sections.get("Commands", []), strip_cli=cli
    ):
        child = build_tree(cli, path + (name,), seen, verbose=verbose)
        child.aliases = aliases
        child.desc = desc
        node.subcommands[name] = child
    return node


# -- go-flag adapter (agy) --------------------------------------------------


def collect_goflag_sections(text: str, *, is_root: bool) -> dict[str, list[str]]:
    """Split go-flag `--help`/`help <name>` output into Flags/Commands blocks.

    Unlike commander, go-flag sections are terminated by a blank line (not
    just by the next non-indented line), and the root's flag block has no
    header at all — it follows "Usage of <cli>:" directly.
    """
    lines = text.splitlines()
    sections: dict[str, list[str]] = {}
    current: str | None = None
    if is_root and lines and lines[0].startswith("Usage of "):
        current = "Flags"
        sections[current] = []
        lines = lines[1:]
    for line in lines:
        m = GOFLAG_SECTION_RE.match(line)
        if m:
            current = (
                "Commands" if m.group(1) == "Available subcommands" else m.group(1)
            )
            sections.setdefault(current, [])
            continue
        if line.strip() == "":
            current = None
            continue
        if current is not None and line.startswith("  "):
            sections[current].append(line)
    return sections


def build_tree_goflag(cli: str, *, verbose: bool = False) -> Node:
    """Build a 2-level-deep tree: root flags/subcommands, one level of

    subcommand flags/children. agy has no deeper nesting worth completing —
    leaf actions like `mcp add`/`plugin install` are completed by name only.
    """
    root = Node(path=())
    root_text = run_help(cli, [], verbose=verbose)
    root_sections = collect_goflag_sections(root_text, is_root=True)
    root.options = parse_options(root_sections.get("Flags", []))
    for name, _aliases, desc in parse_commands(root_sections.get("Commands", [])):
        sub_text = run_goflag_subcommand_help(cli, name, verbose=verbose)
        sub_sections = collect_goflag_sections(sub_text, is_root=False)
        sub = Node(path=(name,), desc=desc)
        sub.options = parse_options(sub_sections.get("Flags", []))
        for leaf_name, leaf_aliases, leaf_desc in parse_commands(
            sub_sections.get("Commands", [])
        ):
            sub.subcommands[leaf_name] = Node(
                path=(name, leaf_name), desc=leaf_desc, aliases=leaf_aliases
            )
        root.subcommands[name] = sub
    return root


# -- native-passthrough adapter (opencode) ----------------------------------


def run_native_passthrough(spec: HarnessSpec, *, verbose: bool = False) -> str | None:
    assert spec.native_command is not None
    argv = [spec.cli, *spec.native_command]
    out = _run(argv, verbose=verbose)
    if not out.startswith(f"#compdef {spec.cli}"):
        print(
            f"error: `{' '.join(argv)}` did not emit a `#compdef {spec.cli}` "
            "header; refusing to write",
            file=sys.stderr,
        )
        return None
    return out


# -- zsh emission (commander + go-flag both feed this) ----------------------


def option_label(opt: Option) -> str:
    ph = opt.arg_placeholder or ""
    m = re.search(r"[<\[]=?([a-zA-Z][a-zA-Z0-9_-]*)", ph)
    return m.group(1) if m else "value"


def esc_desc(s: str) -> str:
    return s.replace("\\", "\\\\").replace("]", "\\]").replace("'", "'\\''")


def format_option(opt: Option) -> str:
    desc = esc_desc(opt.desc)
    arg_part = ""
    if opt.takes_arg:
        label = option_label(opt)
        if opt.choices:
            arg_part = f":{label}:({' '.join(opt.choices)})"
        elif is_dir_option(opt):
            arg_part = f":{label}:_files -/"
        elif is_file_option(opt):
            arg_part = f":{label}:_files"
        else:
            arg_part = f":{label}:"
    eq = "=" if opt.takes_arg else ""
    if len(opt.names) == 1:
        return f"'{opt.names[0]}{eq}[{desc}]{arg_part}'"
    excl = " ".join(opt.names)
    names_csv = ",".join(opt.names)
    return f"'({excl})'{{{names_csv}}}'{eq}[{desc}]{arg_part}'"


def sanitize(path: tuple[str, ...]) -> str:
    return "_".join(p.replace("-", "_") for p in path)


def needs_function(node: Node) -> bool:
    return bool(node.subcommands) or bool(node.options)


def emit_zsh(root: Node, cli: str) -> str:
    lines: list[str] = []
    lines.append(f"#compdef {cli}")
    lines.append("")
    lines.append(f"# zsh completion for the `{cli}` CLI — AUTO-GENERATED.")
    lines.append(
        f"# Regenerate with: python3 ~/.claude/scripts/gen_shell_completion.py --harness {cli}"
    )
    lines.append("# Do not edit by hand.")
    lines.append("")

    def emit_function(node: Node) -> None:
        fname = f"_{cli}" if not node.path else f"_{cli}_{sanitize(node.path)}"
        arg_specs = [format_option(o) for o in node.options]

        lines.append(f"{fname}() {{")
        if node.subcommands:
            lines.append('  local curcontext="$curcontext" state line')
            lines.append("  typeset -A opt_args")
            lines.append("")
            lines.append("  _arguments -C \\")
            for spec in arg_specs:
                lines.append(f"    {spec} \\")
            lines.append("    '1: :->cmds' \\")
            lines.append("    '*:: :->args' && return 0")
            lines.append("")
            lines.append("  case $state in")
            lines.append("    cmds)")
            lines.append(f"      {fname}_commands")
            lines.append("      ;;")
            dispatch = [
                (n, c) for n, c in node.subcommands.items() if needs_function(c)
            ]
            if dispatch:
                lines.append("    args)")
                lines.append("      case $line[1] in")
                for name, child in dispatch:
                    label = "|".join([name, *child.aliases])
                    child_fname = f"_{cli}_{sanitize(child.path)}"
                    lines.append(f"        {label})")
                    lines.append(f"          {child_fname}")
                    lines.append("          ;;")
                lines.append("      esac")
                lines.append("      ;;")
            lines.append("  esac")
        elif arg_specs:
            lines.append("  _arguments \\")
            for i, spec in enumerate(arg_specs):
                sep = " \\" if i < len(arg_specs) - 1 else ""
                lines.append(f"    {spec}{sep}")
        else:
            lines.append("  return 0")
        lines.append("}")
        lines.append("")

        if node.subcommands:
            lines.append(f"{fname}_commands() {{")
            lines.append("  local -a commands")
            lines.append("  commands=(")
            for name, child in node.subcommands.items():
                desc = esc_desc(child.desc)
                lines.append(f"    '{name}:{desc}'")
                for alias in child.aliases:
                    lines.append(f"    '{alias}:{desc}'")
            lines.append("  )")
            lines.append(f"  _describe -t commands '{cli} command' commands")
            lines.append("}")
            lines.append("")

        for child in node.subcommands.values():
            if needs_function(child):
                emit_function(child)

    emit_function(root)
    lines.append(f'_{cli} "$@"')
    return "\n".join(lines) + "\n"


# -- CLI ---------------------------------------------------------------


def generate(spec: HarnessSpec, *, verbose: bool = False) -> str | None:
    if spec.format == "native-passthrough":
        return run_native_passthrough(spec, verbose=verbose)

    probe = run_help(spec.cli, [], verbose=verbose)
    if not probe:
        print(
            f"error: `{spec.cli} --help` produced no output; is the CLI installed?",
            file=sys.stderr,
        )
        return None

    if spec.format == "commander":
        root = build_tree(spec.cli, (), set(), verbose=verbose)
    elif spec.format == "go-flag":
        root = build_tree_goflag(spec.cli, verbose=verbose)
    else:
        raise ValueError(f"unknown harness format: {spec.format!r}")
    return emit_zsh(root, spec.cli)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    cli_common.add_verbosity_args(ap)
    ap.add_argument(
        "--harness",
        required=True,
        choices=[*HARNESSES, "all"],
        help="harness to generate a completion for, or 'all'",
    )
    ap.add_argument(
        "--out",
        type=Path,
        default=None,
        help="output path (only valid for a single harness)",
    )
    ap.add_argument(
        "--stdout", action="store_true", help="print to stdout instead of writing"
    )
    args = ap.parse_args()

    if args.out is not None and args.harness == "all":
        print("error: --out requires a single --harness, not 'all'", file=sys.stderr)
        return 1

    names = list(HARNESSES) if args.harness == "all" else [args.harness]
    exit_code = 0
    for name in names:
        spec = HARNESSES[name]
        script = generate(spec, verbose=args.verbose)
        if script is None:
            exit_code = 1
            continue
        if args.stdout:
            sys.stdout.write(script)
            continue
        out_path = (
            args.out if args.out is not None else DEFAULT_OUT_DIR / f"_{spec.cli}"
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(script)
        cli_common.qprint(f"wrote {out_path}", quiet=args.quiet)
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
