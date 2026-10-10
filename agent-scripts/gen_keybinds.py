#!/usr/bin/env python3
"""Regenerate the opencode leader-chord table in the parity doc from an opencode binary.

The harness binds leader chords (trust-session on ``<leader>d``, permission-gate on
``<leader>p``). A chord opencode already owns does not fail loudly -- the two bindings
simply fight, and whichever loses stops responding. That is not hypothetical:
``<leader>y`` is core's ``messages_copy``, and a harness command shipped on it while
the whole suite stayed green.

So the table of taken chords is generated from a real opencode binary rather than
hand-copied. The committed table tracks the PINNED release -- the version of
``@opencode-ai/plugin`` in ``opencode/package.json``, which the repo already treats
as the CLI pin -- so it is the same table on every machine and in CI.

Where the binary comes from:

- ``--fetch-pinned`` downloads the pinned release's platform package with
  ``npm pack`` into a temporary directory. CI runs ``--check --fetch-pinned``: its
  runners have no opencode, so this is the only way CI can compare anything.
- ``--fetch-latest`` (with ``--check`` only) downloads the newest release instead:
  the ``latest`` npm dist-tag of ``opencode-ai``, the CLI package, packed as that
  exact version of the platform package. A scheduled workflow runs it so drift in
  a release nobody has pinned yet is reported before the pin is bumped. It
  compares without ``--allow-unpinned`` and never writes. It compares whatever
  ``latest`` names, even a release older than the pin: that is what a fresh
  ``npm i -g opencode-ai`` installs.
- ``--binary PATH`` uses that file.
- Otherwise the ``opencode`` on ``PATH``.

``--check`` never reports success without comparing. A binary that is not the pinned
release exits 2 ("cannot check") unless ``--allow-unpinned`` is given, which the local
test suite passes so the build actually installed here -- often a development build
-- is still compared. Writing always requires the pinned release.

This is a *generator*, not a test-time check, and that placement is deliberate. A
parse that silently degraded inside the collision guard would yield an empty
denylist and a guard that passes everything. Here every doubt raises: the keybind
object must be found by its structure (never by a minified name, which changes
per build), every top-level entry in it must parse, identity keys must be present,
and floors must be met.

Exit codes: 0 = compared and up to date (or written); 1 = the committed table is
stale; 2 = could not compare or write with confidence (no binary, unpinned binary,
fetch/version/parse/I-O failure).

Files read: the opencode binary, ``opencode/package.json``, and
``opencode/CLAUDE_CODE_PARITY.md``. Files written: ``opencode/CLAUDE_CODE_PARITY.md``
-- only the text between the ``<!-- leader-chords-core:begin -->`` and
``<!-- leader-chords-core:end -->`` anchors. Everything else in that file is
hand-authored and is left byte-for-byte alone.

Standard library only, like every other script in this directory.
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from collections import defaultdict
from pathlib import Path

import harness_spec

# Reads an opencode binary and rewrites one region of a tracked doc; --fetch-pinned
# downloads into a throwaway temp dir. Touches no toolkit data path -- no
# ~/.agent-toolkit/data, no ~/.local/state/agent-toolkit.
TOOLKIT_DATA = "none"

REPO_ROOT = Path(__file__).resolve().parent.parent
PARITY_DOC = REPO_ROOT / "opencode" / "CLAUDE_CODE_PARITY.md"
PACKAGE_JSON = REPO_ROOT / "opencode" / "package.json"
PIN_PACKAGE = harness_spec.HARNESSES["opencode"].version_pin_package

BEGIN_ANCHOR = "<!-- leader-chords-core:begin -->"
END_ANCHOR = "<!-- leader-chords-core:end -->"
LEADER_PREFIX = "<leader>"

# The keybind table is minified into the binary as
#   H=(_,J)=>({default:_,description:J}),x_={leader:H(M9,"..."),app_exit:H("...","..."),...}
# The helper's NAME differs per build (``g`` in one, ``H`` in another), so find the
# helper by its definition shape and the table by ``{leader:<helper>(``.
_IDENT = rb"[A-Za-z_$][\w$]*"
HELPER_DEF_RE = re.compile(
    rb"(?<![\w$])(" + _IDENT + rb")=\((" + _IDENT + rb"),(" + _IDENT + rb")\)=>"
    rb"\(\{default:\2,description:\3\}\)"
)
_STR = r'"(?:[^"\\]|\\.)*"'

# Floors and identity keys, so a parse that silently degrades raises instead of
# writing a table that looks authoritative and is wrong. A vacuous generator is
# worse than no generator: it would overwrite a good table with an empty one.
MIN_KEYBINDS_PARSED = 50
MIN_LEADER_CHORDS = 10
IDENTITY_KEYS = frozenset({"app_exit", "session_new", "messages_copy"})

# What `opencode --version` may print: the same grammar the SDK pin check in
# test_opencode_ts_checks.py accepts (optional leading v, optional +build).
VERSION_LINE_RE = re.compile(
    r"v?(\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?)"
)
# Strict SemVer 2.0 without build metadata: what npm's ``latest`` tag holds
# (npm strips ``+build`` on publish). Anything else is refused rather than packed.
_SEMVER_NUM = r"(?:0|[1-9]\d*)"
_SEMVER_ID = r"(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
SEMVER_RE = re.compile(
    rf"({_SEMVER_NUM})\.({_SEMVER_NUM})\.({_SEMVER_NUM})"
    rf"(?:-({_SEMVER_ID}(?:\.{_SEMVER_ID})*))?"
)
CLI_PACKAGE = "opencode-ai"
NPM_OS = {"Linux": "linux", "Darwin": "darwin"}
NPM_ARCH = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}
PACKED_BINARY = "package/bin/opencode"


class KeybindExtractionError(RuntimeError):
    """The opencode keybind table could not be read with confidence."""


def pinned_version(package_json: Path) -> str:
    """The pinned opencode release: the ``@opencode-ai/plugin`` devDependency."""
    try:
        return harness_spec.read_manifest_pin(package_json, PIN_PACKAGE)
    except harness_spec.VersionPinError as exc:
        raise KeybindExtractionError(str(exc)) from exc


def opencode_version(binary: Path) -> str:
    """The binary's reported version; raises unless it ran and printed one."""
    try:
        result = subprocess.run(
            [str(binary), "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise KeybindExtractionError(
            f"could not run {binary} --version: {exc}"
        ) from exc
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    match = VERSION_LINE_RE.fullmatch(lines[0]) if lines else None
    if result.returncode != 0 or match is None:
        raise KeybindExtractionError(
            f"{binary} --version exited {result.returncode} with "
            f"{result.stdout.strip()!r}; cannot tell which opencode this is"
        )
    return match.group(1)


def _npm_platform() -> tuple[str, str]:
    """The npm platform/arch names for this machine; raises if unsupported."""
    npm_os = NPM_OS.get(platform.system())
    npm_arch = NPM_ARCH.get(platform.machine().lower())
    if npm_os is None or npm_arch is None:
        raise KeybindExtractionError(
            f"unsupported platform {platform.system()}/{platform.machine()}; "
            f"pass --binary instead"
        )
    return npm_os, npm_arch


def fetch_pinned_binary(version: str, dest: Path) -> Path:
    """Download the pinned release's binary for this platform into ``dest``."""
    npm_os, npm_arch = _npm_platform()
    npm = shutil.which("npm")
    if npm is None:
        raise KeybindExtractionError("--fetch-pinned needs npm on PATH")
    spec = f"opencode-{npm_os}-{npm_arch}@{version}"
    result = subprocess.run(
        [npm, "pack", spec, "--pack-destination", str(dest)],
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    if result.returncode != 0:
        raise KeybindExtractionError(
            f"npm pack {spec} failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    tarballs = sorted(dest.glob("*.tgz"))
    if len(tarballs) != 1:
        raise KeybindExtractionError(
            f"npm pack {spec} left {len(tarballs)} tarballs in {dest}, expected 1"
        )
    with tarfile.open(tarballs[0], "r:gz") as tar:
        try:
            member = tar.getmember(PACKED_BINARY)
        except KeyError as exc:
            raise KeybindExtractionError(
                f"{tarballs[0].name} has no {PACKED_BINARY}"
            ) from exc
        tar.extract(member, path=dest, filter="data")
    return dest / PACKED_BINARY


def latest_version() -> str:
    """The newest opencode release: the ``latest`` npm dist-tag of the CLI package.

    Read from ``opencode-ai`` itself rather than a platform package, whose tag could
    lag or be rolled back independently. Raises unless npm printed exactly one
    strict version (a JSON string, or a one-element list).
    """
    npm = shutil.which("npm")
    if npm is None:
        raise KeybindExtractionError("--fetch-latest needs npm on PATH")
    cmd = [npm, "view", CLI_PACKAGE, "dist-tags.latest", "--json"]
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=120, check=False
    )
    if result.returncode != 0:
        raise KeybindExtractionError(
            f"npm view {CLI_PACKAGE} dist-tags.latest failed "
            f"(exit {result.returncode}): {result.stderr.strip()}"
        )
    try:
        value = json.loads(result.stdout)
    except ValueError:
        value = None
    if isinstance(value, list) and len(value) == 1:
        value = value[0]
    if not isinstance(value, str):
        raise KeybindExtractionError(
            f"npm view {CLI_PACKAGE} dist-tags.latest printed "
            f"{result.stdout.strip()!r}, not one version"
        )
    if SEMVER_RE.fullmatch(value) is None:
        raise KeybindExtractionError(
            f"npm view {CLI_PACKAGE} dist-tags.latest printed {value!r}, not a "
            f"strict MAJOR.MINOR.PATCH[-PRERELEASE] version"
        )
    return value


def _skip_string(data: bytes, index: int) -> int:
    """Index just past the string literal opening at ``index``, or -1 if unterminated.

    Handles ``"``, ``'`` and template literals with backslash escapes. Template
    ``${...}`` interpolation is treated as opaque string content: the keybind table
    has none, and a mis-scan here fails whole-object accounting instead of passing.
    """
    quote = data[index]
    i = index + 1
    end = len(data)
    while i < end:
        char = data[i]
        if char == 0x5C:  # backslash
            i += 2
            continue
        if char == quote:
            return i + 1
        if char == 0x0A and quote != 0x60:  # newline ends a broken "..."/'...'
            return -1
        i += 1
    return -1


def _object_end(data: bytes, start: int) -> int:
    """Index of the ``}`` closing the object that opens at ``start``, or -1."""
    depth = 0
    i = start
    end = len(data)
    while i < end:
        char = data[i]
        if char in b"\"'`":
            i = _skip_string(data, i)
            if i < 0:
                return -1
            continue
        if char in b"{([":
            depth += 1
        elif char in b"})]":
            depth -= 1
            if depth == 0:
                return i if char == 0x7D else -1
            if depth < 0:
                return -1
        i += 1
    return -1


def _top_level_entries(body: bytes) -> list[str]:
    """Split an object body into its top-level ``key:value`` entries."""
    entries: list[str] = []
    depth = 0
    start = 0
    i = 0
    while i < len(body):
        char = body[i]
        if char in b"\"'`":
            i = _skip_string(body, i)
            if i < 0:
                raise KeybindExtractionError("unterminated string in keybind table")
            continue
        if char in b"{([":
            depth += 1
        elif char in b"})]":
            depth -= 1
        elif char == 0x2C and depth == 0:  # comma
            entries.append(body[start:i].decode("utf-8", "replace"))
            start = i + 1
        i += 1
    entries.append(body[start:].decode("utf-8", "replace"))
    return entries


# Bare words an object/array default may contain besides keys: anything else is a
# reference to a value the extractor cannot see.
_LITERAL_WORDS = frozenset({"true", "false", "null", "undefined"})


def _default_keys(name: str, arg: str) -> list[str] | None:
    """Every key spec in a helper call's default argument, or None if unreadable.

    The default is a string literal (``"ctrl+c,<leader>q"``) or an object/array
    literal (``{key:"ctrl+v",preventDefault:!1}``); for those, every string inside
    counts -- over-reporting a chord is safe, missing one is not. A default held in
    a variable could hide a chord, so it is refused, except for ``leader`` itself:
    that entry is the leader key, never a leader chord.
    """
    if re.fullmatch(_STR, arg):
        return [json.loads(arg)]
    if re.fullmatch(r"[A-Za-z_$][\w$]*", arg):
        return [] if name == "leader" else None
    if arg[:1] not in "{[":
        return None
    raw = arg.encode()
    if _object_end(raw, 0) != len(raw) - 1:
        return None
    without_strings = re.sub(_STR, '""', arg)
    for word in re.finditer(
        r"(?<![\w$])[A-Za-z_$][\w$]*(?![\w$])(?!\s*:)", without_strings
    ):
        if word.group(0) not in _LITERAL_WORDS:
            return None
    return [json.loads(literal) for literal in re.findall(_STR, arg)]


def _parse_table(data: bytes, helper: str, start: int) -> list[tuple[str, list[str]]]:
    """Every ``(name, key specs)`` entry of the object opening at ``start``.

    Raises unless EVERY top-level entry is a call to ``helper`` -- that
    whole-object accounting is what catches a truncated or overrun scan that
    happened to clear the floors, and an entry shape the parser does not know.
    """
    end = _object_end(data, start)
    if end < 0:
        raise KeybindExtractionError(
            "unbalanced braces or strings around the keybind table"
        )
    entry_re = re.compile(
        rf"([A-Za-z_$][\w$]*|{_STR}):{re.escape(helper)}\((.+),{_STR}\)", re.DOTALL
    )
    parsed: list[tuple[str, list[str]]] = []
    for raw in _top_level_entries(data[start + 1 : end]):
        entry = raw.strip()
        match = entry_re.fullmatch(entry)
        name = match.group(1) if match else ""
        name = json.loads(name) if name.startswith('"') else name
        keys = _default_keys(name, match.group(2)) if match else None
        if keys is None:
            raise KeybindExtractionError(
                f"keybind table entry {entry[:80]!r} is not a {helper}(...) call "
                "with a readable default"
            )
        parsed.append((name, keys))
    return parsed


def _chords(entries: list[tuple[str, list[str]]]) -> dict[str, list[str]]:
    owners: dict[str, set[str]] = defaultdict(set)
    for name, specs in entries:
        for spec in specs:
            for part in spec.split(","):
                part = part.strip()
                if part.startswith(LEADER_PREFIX):
                    token = part[len(LEADER_PREFIX) :].strip()
                    if token:
                        owners[token].add(name)
    return {token: sorted(names) for token, names in sorted(owners.items())}


def _validated_chords(data: bytes, helper: str, position: int) -> dict[str, list[str]]:
    """The chords of one candidate table, if it passes identity and floor checks."""
    entries = _parse_table(data, helper, position)
    missing = IDENTITY_KEYS - {name for name, _ in entries}
    if missing:
        raise KeybindExtractionError(f"missing identity keys {sorted(missing)}")
    chords = _chords(entries)
    if len(entries) < MIN_KEYBINDS_PARSED or len(chords) < MIN_LEADER_CHORDS:
        raise KeybindExtractionError(
            f"{len(entries)} keybinds / {len(chords)} leader chords is under "
            f"the floor ({MIN_KEYBINDS_PARSED} / {MIN_LEADER_CHORDS})"
        )
    return chords


def leader_chords(binary: Path) -> dict[str, list[str]]:
    """Map each leader-chord token to the keybind names that own it.

    Tokens are bare (``d``, not ``<leader>d``), so the caller can compare against a
    binding's suffix without a format mismatch silently making every comparison
    false. Exactly one object in the binary must pass every structural check.
    """
    data = binary.read_bytes()
    helpers = {m.group(1).decode() for m in HELPER_DEF_RE.finditer(data)}
    accepted: list[dict[str, list[str]]] = []
    rejected: list[str] = []
    for helper in sorted(helpers):
        marker = b"{leader:" + helper.encode() + b"("
        position = data.find(marker)
        while position >= 0:
            try:
                accepted.append(_validated_chords(data, helper, position))
            except KeybindExtractionError as exc:
                rejected.append(f"helper {helper!r} at byte {position}: {exc}")
            position = data.find(marker, position + 1)
    if len(accepted) == 1:
        return accepted[0]
    detail = "; ".join(rejected) or (
        f"no {{leader:<helper>(...}} object found for helpers {sorted(helpers)}"
    )
    raise KeybindExtractionError(
        f"found {len(accepted)} candidate keybind tables in {binary} (need exactly 1); "
        f"the table's shape changed, so the leader chords cannot be read with "
        f"confidence. {detail}"
    )


def render_block(chords: dict[str, list[str]]) -> str:
    """The markdown table body, without the surrounding anchors."""
    lines = ["| Chord | Core keybind |", "|---|---|"]
    for token, names in chords.items():
        joined = ", ".join(f"`{name}`" for name in names)
        lines.append(f"| `{LEADER_PREFIX}{token}` | {joined} |")
    return "\n".join(lines) + "\n"


def current_block(text: str) -> str:
    """The block currently committed between the anchors."""
    if text.count(BEGIN_ANCHOR) != 1 or text.count(END_ANCHOR) != 1:
        raise KeybindExtractionError(
            f"{PARITY_DOC} must contain exactly one {BEGIN_ANCHOR} and one "
            f"{END_ANCHOR}; the generator only edits the region between them"
        )
    begin = text.find(BEGIN_ANCHOR)
    end = text.find(END_ANCHOR)
    if begin > end:
        raise KeybindExtractionError(f"{BEGIN_ANCHOR} appears after {END_ANCHOR}")
    return text[begin + len(BEGIN_ANCHOR) : end].strip("\n")


def splice_block(text: str, body: str) -> str:
    """Return ``text`` with the anchored region replaced by ``body``."""
    current_block(text)  # validates the anchors
    begin = text.find(BEGIN_ANCHOR)
    end = text.find(END_ANCHOR)
    return (
        text[: begin + len(BEGIN_ANCHOR)]
        + "\n\n"
        + body.rstrip("\n")
        + "\n\n"
        + text[end:]
    )


def _tokens(block: str) -> set[str]:
    """Bare tokens named by a committed block, for the staleness message."""
    return {
        match.group(1).strip()
        for match in re.finditer(rf"`{re.escape(LEADER_PREFIX)}([^`]+)`", block)
    }


def _committed_chords(block: str) -> dict[str, list[str]]:
    """The chord->owners map of a committed block, parsed from its markdown rows.

    Strict: every ``<leader>`` row must parse as ``| `<leader>X` | `name`, ... |``,
    a chord may not repeat, and every row must name at least one owner. A malformed
    committed block is exit 2 (cannot compare), not a silently empty chord diff.
    """
    chords: dict[str, list[str]] = {}
    rows = [
        line.strip()
        for line in block.splitlines()
        if line.strip().startswith(f"| `{LEADER_PREFIX}")
    ]
    for line in rows:
        match = re.fullmatch(
            rf"\| `{re.escape(LEADER_PREFIX)}([^`]+)` \| (.+) \|", line
        )
        if match is None:
            raise KeybindExtractionError(f"malformed committed chord row: {line!r}")
        token = match.group(1).strip()
        if token in chords:
            raise KeybindExtractionError(
                f"duplicate committed chord row for {LEADER_PREFIX}{token}"
            )
        names = [
            name.strip().strip("`")
            for name in match.group(2).split(",")
            if name.strip()
        ]
        if not names:
            raise KeybindExtractionError(
                f"committed chord row names no keybind: {line!r}"
            )
        chords[token] = names
    return chords


def _resolve_binary(args: argparse.Namespace, pin: str, tmp: Path) -> tuple[Path, bool]:
    """The binary to read, and whether it was fetched for the pin."""
    if args.fetch_pinned:
        return fetch_pinned_binary(pin, tmp), True
    if args.binary:
        return Path(args.binary), False
    found = shutil.which("opencode")
    if found is None:
        raise KeybindExtractionError(
            "no opencode binary: not on PATH. Pass --fetch-pinned to download the "
            "pinned release, or --binary PATH"
        )
    return Path(found), False


def _run(args: argparse.Namespace, tmp: Path) -> int:
    pin = pinned_version(PACKAGE_JSON)
    expected = pin
    if args.fetch_latest:
        _npm_platform()  # unsupported platform is exit 2 before any npm call
        expected = latest_version()
        binary, fetched = fetch_pinned_binary(expected, tmp), True
    else:
        binary, fetched = _resolve_binary(args, pin, tmp)
    version = opencode_version(binary)
    if fetched and version != expected:
        raise KeybindExtractionError(
            f"fetched opencode reports {version}, expected {expected}"
        )
    if version != pin and not fetched:
        if not args.check:
            raise KeybindExtractionError(
                f"refusing to write from opencode {version} ({binary}): the committed "
                f"table tracks the pinned release {pin}. Use --fetch-pinned"
            )
        if not args.allow_unpinned:
            raise KeybindExtractionError(
                f"opencode {version} ({binary}) is not the pinned release {pin}; cannot "
                f"check. Use --fetch-pinned, or --allow-unpinned to compare anyway"
            )

    chords = leader_chords(binary)
    body = render_block(chords)
    existing = PARITY_DOC.read_text(encoding="utf-8")
    label = f"opencode {version}" + ("" if version == pin else f", pin {pin}")

    if args.check:
        committed = current_block(existing)
        if committed.strip() == body.strip():
            print(
                f"gen_keybinds: leader-chord table is up to date "
                f"({len(chords)} chords, {label})"
            )
            return 0
        if args.fetch_latest:
            committed_chords = _committed_chords(committed)
            added = sorted(set(chords) - set(committed_chords))
            removed = sorted(set(committed_chords) - set(chords))
            changed = sorted(
                token
                for token in set(chords) & set(committed_chords)
                if chords[token] != committed_chords[token]
            )
            lines = [
                (
                    f"gen_keybinds: opencode {version} (npm latest) changes the core "
                    f"leader chords in the committed table (pin {pin})."
                ),
            ]
            if added:
                lines.append(f"  added:   {added}")
            if removed:
                lines.append(f"  removed: {removed}")
            for token in changed:
                lines.append(
                    f"  changed: {LEADER_PREFIX}{token} "
                    f"{committed_chords[token]} -> {chords[token]}"
                )
            if not (added or removed or changed):
                lines.append(
                    "  (the committed and latest tables render differently, but no "
                    "chord was added, removed, or re-owned; the difference is "
                    "formatting, not a chord change)"
                )
            lines.append(
                f"Check now whether a harness chord is among the new entries: if one "
                f"is, users on opencode {version} already hit that collision."
            )
            lines.append(
                f"The committed table tracks the pin, so regenerate it if you move "
                f"the pin to {version}: python3 agent-scripts/gen_keybinds.py "
                f"--fetch-pinned"
            )
            lines.append(
                "Then update CORE_LETTERS in test/test_opencode_trust_wiring.py to "
                "match."
            )
            print("\n".join(lines), file=sys.stderr)
            return 1
        print(
            f"gen_keybinds: leader-chord table is STALE against {label}.\n"
            f"  committed: {sorted(_tokens(committed))}\n"
            f"  binary:    {sorted(chords)}\n"
            f"Regenerate with: python3 agent-scripts/gen_keybinds.py --fetch-pinned\n"
            f"Then update CORE_LETTERS in test/test_opencode_trust_wiring.py to match, "
            f"and check whether a harness chord is among the new entries.",
            file=sys.stderr,
        )
        return 1

    PARITY_DOC.write_text(splice_block(existing, body), encoding="utf-8")
    print(f"gen_keybinds: wrote {len(chords)} leader chords to {PARITY_DOC} ({label})")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Regenerate the opencode leader-chord table in the parity doc."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if the committed table is stale, 2 if it cannot be checked",
    )
    # Plain add_argument, not a mutually exclusive group: gen_interfaces documents
    # only top-level parser arguments, and CI's --fetch-pinned must be documented.
    parser.add_argument("--binary", help="read this opencode binary instead of PATH's")
    parser.add_argument(
        "--fetch-pinned",
        action="store_true",
        help="download the pinned release with npm pack and read that (what CI runs)",
    )
    parser.add_argument(
        "--fetch-latest",
        action="store_true",
        help=(
            "with --check: compare against the newest release (npm latest of "
            "opencode-ai) instead of the pin"
        ),
    )
    parser.add_argument(
        "--allow-unpinned",
        action="store_true",
        help="with --check: compare against a binary that is not the pinned release",
    )
    args = parser.parse_args(argv)
    if args.binary and args.fetch_pinned:
        parser.error("--binary and --fetch-pinned are mutually exclusive")
    if args.fetch_latest and (args.binary or args.fetch_pinned):
        parser.error("--fetch-latest excludes --binary and --fetch-pinned")
    if args.fetch_latest and not args.check:
        parser.error("--fetch-latest only applies to --check")
    if args.allow_unpinned and not args.check:
        parser.error("--allow-unpinned only applies to --check")

    try:
        with tempfile.TemporaryDirectory(prefix="gen_keybinds-") as tmp:
            return _run(args, Path(tmp))
    except (
        KeybindExtractionError,
        OSError,
        ValueError,
        tarfile.TarError,
        subprocess.SubprocessError,
    ) as exc:
        print(f"gen_keybinds: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
