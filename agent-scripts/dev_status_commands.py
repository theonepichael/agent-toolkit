#!/usr/bin/env python3
"""Canonical dev_status subcommand names, shared by CLI dispatch and slug validation.

Single source of truth for the ``dev_status.py`` subcommand surface. Both
``dev_status_impl`` (argparse dispatch, ``choices``/metavar, and the
``dispatch``/``SUBCOMMANDS`` drift check) and ``dev_status_mutation``
(reserved-slug validation in ``validate_slug``) import these rather than
redefining them. Collapsed 2026-11-06 from two hand-maintained copies that
had drifted: the mutation-side list was missing ``sendback``, ``slug-check``,
``validate``, ``worktree``, and ``integration-merge``, leaving the two
hyphenated names (``slug-check``, ``integration-merge``) legal as item ids.
"""

# What this module does with toolkit data (checked by scripts/check_toolkit_paths.py).
TOOLKIT_DATA = "none"

# Subcommand names blocked from use as item slugs. Match is exact: only a
# slug equal to one of these bare verbs is refused — `remove-probe`,
# `add-feature`, etc. are accepted. Argparse never confuses a hyphenated
# slug with a subcommand (the subcommand is parsed from argv positionally),
# and the bare-verb reservation exists purely for dashboard clarity (no
# item literally named `remove`). Prefix-match refusal was considered and
# rejected: it would forbid natural slugs like `update-deps` for no real
# dispatch-safety gain (2026-07-25 decision).
SUBCOMMANDS: tuple[str, ...] = (
    "render",
    "list",
    "ready",
    "show",
    "add",
    "update",
    "start",
    "done",
    "reopen",
    "review",
    "approve",
    "reject",
    "sendback",
    "gate-set",
    "gate-pass",
    "acceptance-set",
    "verifier-check",
    "contract-touch",
    "slug-check",
    "cited-by",
    "run",
    "runs",
    "backfill-gate",
    "rename",
    "remove",
    "block",
    "unblock",
    "validate",
    "prune",
    "recap",
    "worktree",
    "machine-id",
    "integration-merge",
)

# The reserved-word set: every subcommand name plus a handful of dashboard /
# future-use names. The single frozenset the slug validator consults; nothing
# else keeps a second copy.
RESERVED_SLUGS: frozenset[str] = frozenset(
    set(SUBCOMMANDS) | {"pending", "out-of-scope", "all", "help", "new"}
)
