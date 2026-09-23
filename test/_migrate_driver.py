#!/usr/bin/env python3
"""Child-process driver for the toolkit-home migration crash tests.

``fault_checkpoint`` kills the direct child of the test, so the crash tests
launch this script rather than install.py. It swaps the migration's
fresh-process validation for a stub (the sandbox HOME has no installed
runtime for ``dev_status.py validate`` or ``--check-links`` to pass against),
then either runs install.py's ``main`` or recovery alone.

Usage
  _migrate_driver.py install <install.py args...>
  _migrate_driver.py recover

Environment
  MIGRATE_DRIVER_VALIDATION=pass|fail   what the stubbed validation reports
                                        (default pass)
"""

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "agent-scripts"))

import install  # noqa: E402
import link_inspect  # noqa: E402
import migrate_toolkit_home as mth  # noqa: E402
import migration_lock  # noqa: E402


def _stub_validation(_ctx: object) -> tuple[bool, list[dict[str, object]]]:
    verdict = os.environ.get("MIGRATE_DRIVER_VALIDATION", "pass")
    return verdict == "pass", [{"command": ["stub"], "exit": 0 if verdict == "pass" else 1}]


def main() -> int:
    mth._run_validation = _stub_validation
    mode, args = sys.argv[1], sys.argv[2:]
    if mode == "install":
        return install.main(args)
    if mode == "recover":
        history = link_inspect.manifest_path(Path.home())
        with migration_lock.exclusive(mth.LOCK_SITE, blocking=False):
            actions = mth.recover(history.parent, history, repo_root=REPO)
        print(json.dumps({"recovered": actions}))
        return 0
    raise SystemExit(f"unknown mode {mode!r}")


if __name__ == "__main__":
    raise SystemExit(main())
