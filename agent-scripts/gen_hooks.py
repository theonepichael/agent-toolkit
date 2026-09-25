#!/usr/bin/env python3
"""gen_hooks.py — compile lifecycle hooks across Claude, Copilot, and agy.

Compiles lifecycle hooks (PreToolUse, PostToolUse, SessionStart, Stop,
Notification) from the central declarative manifest in harness_spec.py into
per-harness configuration files: copilot/hooks/*.json, agy/hooks.json, and
claude/settings*.json.

Usage:
    python3 agent-scripts/gen_hooks.py            rewrite every copy
    python3 agent-scripts/gen_hooks.py --check     exit 1 if any copy is stale
    python3 agent-scripts/gen_hooks.py --stdout    print the rendered copies,
                                                   write nothing

Flags: --check, --stdout, --repo-root <path>, --quiet/-q, --verbose/-v.
Env vars: none.
Files read: agent-scripts/harness_spec.py, claude/settings.json, claude/settings.work.json.
Files written: copilot/hooks/pre-tool-use.json, copilot/hooks/post-tool-use.json,
copilot/hooks/session-start.json, copilot/hooks/agent-stop.json, agy/hooks.json,
claude/settings.json, claude/settings.work.json.
Exit codes: 0 success; 1 --check found stale output; 2 bad usage.

Requires Python 3.12+.
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path
from typing import Any

import cli_common
import harness_spec

# What this module does with toolkit data (checked by scripts/check_toolkit_paths.py).
TOOLKIT_DATA = "none"


def compile_hooks(repo_root: Path) -> dict[Path, str]:
    """Compile declarative hook manifests into target file contents."""
    manifest = harness_spec.LIFECYCLE_HOOKS
    outputs: dict[Path, str] = {}

    # 1. Copilot hooks
    pre = manifest["PreToolUse"]["copilot"]
    outputs[repo_root / "copilot/hooks/pre-tool-use.json"] = (
        json.dumps(
            {
                "version": 1,
                "hooks": {
                    "preToolUse": [
                        {
                            "type": pre["type"],
                            "matcher": pre["matcher"],
                            "bash": pre["bash"],
                            "timeoutSec": pre["timeoutSec"],
                        }
                    ]
                },
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )

    post = manifest["PostToolUse"]["copilot"]
    outputs[repo_root / "copilot/hooks/post-tool-use.json"] = (
        json.dumps(
            {
                "version": 1,
                "hooks": {
                    "postToolUse": [
                        {
                            "type": post["type"],
                            "matcher": post["matcher"],
                            "bash": post["bash"],
                            "timeoutSec": post["timeoutSec"],
                        }
                    ]
                },
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )

    ss = manifest["SessionStart"]["copilot"]
    outputs[repo_root / "copilot/hooks/session-start.json"] = (
        json.dumps(
            {
                "version": 1,
                "hooks": {
                    "sessionStart": [
                        {
                            "type": ss["type"],
                            "bash": ss["bash"],
                            "timeoutSec": ss["timeoutSec"],
                        }
                    ]
                },
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )

    stop = manifest["Stop"]["copilot"]
    outputs[repo_root / "copilot/hooks/agent-stop.json"] = (
        json.dumps(
            {
                "version": 1,
                "hooks": {
                    "agentStop": [
                        {
                            "type": stop["type"],
                            "bash": stop["bash"],
                            "timeoutSec": stop["timeoutSec"],
                        }
                    ]
                },
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )

    # 2. AGY hooks
    pre_inv = manifest["PreInvocation"]["agy"]
    stop_agy = manifest["Stop"]["agy"]
    post_agy = manifest["PostToolUse"]["agy"]
    pre_agy = manifest["PreToolUse"]["agy"]

    outputs[repo_root / "agy/hooks.json"] = (
        json.dumps(
            {
                "herdr": {
                    "PreInvocation": [
                        {
                            "command": pre_inv["command"],
                            "timeout": pre_inv["timeout"],
                            "type": pre_inv["type"],
                        }
                    ]
                },
                "notify-on-stop": {
                    "Stop": [
                        {
                            "command": stop_agy["command"],
                            "timeout": stop_agy["timeout"],
                            "type": stop_agy["type"],
                        }
                    ]
                },
                "ruff-format-on-edit": {
                    "PostToolUse": [
                        {
                            "hooks": [
                                {
                                    "command": post_agy["command"],
                                    "timeout": post_agy["timeout"],
                                    "type": post_agy["type"],
                                }
                            ],
                            "matcher": post_agy["matcher"],
                        }
                    ]
                },
                "worktree-guard": {
                    "PreToolUse": [
                        {
                            "hooks": [
                                {
                                    "command": pre_agy["command"],
                                    "timeout": pre_agy["timeout"],
                                    "type": pre_agy["type"],
                                }
                            ],
                            "matcher": pre_agy["matcher"],
                        }
                    ]
                },
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )

    # 3. Claude settings files
    def compile_claude_hooks(profile: str) -> dict[str, Any]:
        pre_claude = manifest["PreToolUse"]["claude"]
        hooks_dict: dict[str, Any] = {
            "PreToolUse": [
                {
                    "matcher": pre_claude["matcher"],
                    "hooks": [
                        {
                            "type": pre_claude["type"],
                            "command": pre_claude["command"],
                        }
                    ],
                }
            ]
        }

        if profile == "default" and "claude" in manifest.get("PostToolUse", {}):
            post_claude = manifest["PostToolUse"]["claude"]
            hooks_dict["PostToolUse"] = [
                {
                    "matcher": post_claude["matcher"],
                    "hooks": [
                        {
                            "type": post_claude["type"],
                            "command": post_claude["command"],
                        }
                    ],
                }
            ]

        if "claude" in manifest.get("Notification", {}):
            notif_claude = manifest["Notification"]["claude"]
            hooks_dict["Notification"] = [
                {
                    "matcher": notif_claude["matcher"],
                    "hooks": [
                        {
                            "type": notif_claude["type"],
                            "command": notif_claude["command"],
                        }
                    ],
                }
            ]

        if "claude" in manifest.get("Stop", {}):
            stop_claude = manifest["Stop"]["claude"]
            hooks_dict["Stop"] = [
                {
                    "hooks": [
                        {
                            "type": stop_claude["type"],
                            "command": stop_claude["command"],
                        }
                    ]
                }
            ]

        ss_key = "claude-work" if profile == "work" else "claude"
        if ss_key in manifest.get("SessionStart", {}):
            hooks_dict["SessionStart"] = manifest["SessionStart"][ss_key]

        return hooks_dict

    for rel_path, profile in [
        ("claude/settings.json", "default"),
        ("claude/settings.work.json", "work"),
    ]:
        path = repo_root / rel_path
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        data["hooks"] = compile_claude_hooks(profile)
        outputs[path] = json.dumps(data, indent=2, ensure_ascii=False) + "\n"

    return outputs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compile lifecycle hooks across Claude, Copilot, and agy from harness_spec.py."
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if any target file differs from compiled output",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="print rendered output to stdout and write nothing",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        help="repository root directory (default: parent of agent-scripts/)",
    )
    cli_common.add_verbosity_args(parser)

    args = parser.parse_args(argv)
    repo_root = (
        args.repo_root.resolve()
        if args.repo_root
        else Path(__file__).resolve().parent.parent
    )

    outputs = compile_hooks(repo_root)

    if args.stdout:
        for path, content in outputs.items():
            rel = path.relative_to(repo_root)
            print(f"=== {rel} ===")
            print(content)
        return 0

    if args.check:
        stale: list[Path] = []
        for path, compiled in outputs.items():
            if not path.exists():
                stale.append(path)
                cli_common.vprint(f"Missing file: {path}", verbose=args.verbose)
                continue
            current = path.read_text(encoding="utf-8")
            if current != compiled:
                stale.append(path)
                diff = difflib.unified_diff(
                    current.splitlines(keepends=True),
                    compiled.splitlines(keepends=True),
                    fromfile=f"a/{path.relative_to(repo_root)}",
                    tofile=f"b/{path.relative_to(repo_root)}",
                )
                sys.stderr.writelines(diff)

        if stale:
            cli_common.qprint(
                f"gen_hooks.py --check: {len(stale)} file(s) stale.",
                quiet=args.quiet,
            )
            return 1
        return 0

    # Write outputs
    for path, content in outputs.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        cli_common.vprint(f"Wrote {path.relative_to(repo_root)}", verbose=args.verbose)

    return 0


if __name__ == "__main__":
    sys.exit(main())
