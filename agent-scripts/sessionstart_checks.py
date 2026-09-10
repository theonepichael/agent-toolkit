#!/usr/bin/env python3
"""sessionstart_checks.py — run the SessionStart context checks concurrently.

The SessionStart hook list in settings.json used to run its ~10 context
gathering checks serially (~0.5s wall per session start). Every entry is
independent of the others, so this wrapper fans them out across threads and
prints each result in the original list order — same output, one max()
instead of a sum.

The command list below is the single source of truth (settings.json calls
only this wrapper); entries that carry their own ``timeout``/``|| echo``
semantics keep them verbatim. A hang-guard caps entries that lack one, so a
hung check delays the session by its cap instead of forever — strictly
better than the serial status quo, where a hang blocked everything.

Deliberately NOT here: the herdr-agent-state hook. It runs on every
SessionStart event (matcher '*', including resume/clear), not just startup,
so it keeps its own settings.json entry rather than joining this
startup-only fan-out.

Failures never abort the chain: each entry runs in its own subprocess and
thread; a crash prints whatever it produced and the remaining results still
come out.
"""

import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

# (shell command, hang-guard timeout in seconds). Verbatim from the
# SessionStart list this wrapper replaced — keep in lockstep with it. The
# two entries with an inner `timeout` need a guard strictly larger than
# their inner value so the inner timeout (and its `|| echo` fallback, where
# present) fires first.
CHECKS: list[tuple[str, int]] = [
    (
        (
            "python3 ~/.claude/scripts/dev_status.py render 2>&1"
            " || echo '[dev_status] render failed — run /dashboard to debug'"
        ),
        15,
    ),
    ("python3 ~/.claude/scripts/grill.py pending-plan --consume", 15),
    ("python3 ~/.claude/scripts/bundle_drift_check.py 2>/dev/null", 15),
    ("python3 ~/.claude/scripts/settings_seed_drift_check.py", 15),
    (
        (
            "timeout 5s python3 ~/.claude/scripts/harness_discovery_check.py"
            " check --hook"
            " || echo '[harness-discovery] checker failed — run it manually'"
        ),
        15,
    ),
    ("timeout 20s python3 ~/.claude/scripts/link_drift_check.py check 2>/dev/null", 30),
    (
        (
            "if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then"
            " echo; echo 'recent commits:'; git log -n 5 --oneline; fi"
        ),
        15,
    ),
    ("python3 ~/.claude/scripts/watchcommit_activity.py 2>/dev/null", 15),
    ("python3 ~/.claude/scripts/opencode_skills_sync_activity.py 2>/dev/null", 15),
]


def _run(command: str, guard: int) -> str:
    """Run one check via bash, return its full output (stdout then stderr —
    the same channels the serial hooks passed through). A hang-guard expiry
    kills the process and returns whatever it printed so far plus a note;
    an unspawnable command reports the reason. Never raises."""
    try:
        result = subprocess.run(
            ["bash", "-c", command],
            capture_output=True,
            text=True,
            timeout=guard,
        )
    except subprocess.TimeoutExpired as exc:
        partial = exc.stdout or ""
        if isinstance(partial, bytes):
            partial = partial.decode(errors="replace")
        return f"{partial}[sessionstart] exceeded {guard}s guard: {command}\n"
    except OSError as exc:
        return f"[sessionstart] failed to spawn: {exc}\n"
    out = result.stdout or ""
    if result.stderr:
        out += result.stderr
    return out


def run_checks(checks: list[tuple[str, int]] | None = None) -> str:
    """Run all checks concurrently, returning their outputs concatenated in
    the original list order — not completion order — so the session-start
    context stays stable and reviewable run over run."""
    items = checks if checks is not None else CHECKS
    if not items:
        return ""
    with ThreadPoolExecutor(max_workers=len(items)) as pool:
        futures = [pool.submit(_run, cmd, guard) for cmd, guard in items]
        parts: list[str] = []
        for future in futures:
            try:
                parts.append(future.result())
            except Exception as exc:  # noqa: BLE001 — a check bug must never
                # abort the session-start chain; report and keep going.
                parts.append(f"[sessionstart] check failed: {exc}\n")
        return "".join(parts)


def main() -> None:
    sys.stdout.write(run_checks())


if __name__ == "__main__":
    main()
