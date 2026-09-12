# agent-scripts/ — agent notes

The shared workflow tools. Every harness in this repo — Claude Code,
Copilot, opencode, agy, Pi — calls these same paths through
`~/.claude/scripts/`. Only what is easy to get wrong here; general
conventions are in the repo root's `AGENTS.md` and `STYLE.md`.

## Standard library only

No runtime third-party imports, ever. These tools have to run on a machine
that has never run `uv sync` — that is the whole reason the rule exists.
`argparse` for CLIs, `json` for config, `tomllib` for TOML. `pytest` and
`ruff` are development tooling and stay out of anything that runs at harness
runtime.

## The docstrings are source, not commentary

`INTERFACES.md` is **generated** from each script's module docstring and its
argparse definitions. When an interface changes, fix the docstring and the
argparse definition — never `INTERFACES.md` itself — then regenerate:

```sh
python3 agent-scripts/gen_interfaces.py           # rewrite
python3 agent-scripts/gen_interfaces.py --check   # 1 = stale, 3 = doc drift
```

`githooks/pre-commit` runs `--check` and blocks the commit on either exit
code, so a stale inventory cannot land. Exit 3 is the more interesting one:
it means a skill or command doc still describes a CLI contract the script no
longer has.

That check is a string comparison, not a reading. A flag can keep its name
while its behaviour changes underneath, and every doc naming it still
passes. After changing a script's behaviour, re-read the skill docs that
name it — `claude/commands/`, `opencode/skills/`, `copilot/skills/`,
`agy/skills/`, `pi/prompts/` — and fix the wording before committing.

## Every production script needs a `links.toml` entry — and no test may have one

`test/test_install.py` asserts both directions. A production script with no
entry means `~/.claude/scripts/<name>` silently never exists, and the skills
that call it fail on a machine where it was never hand-linked. This has been
caught live twice.

Test files are the inverse: they are always run in-repo
(`python3 test_X.py`), never through the deployed path, so a `links.toml`
entry for one is dead state and the suite rejects it.

## Colocated tests are `unittest`, not pytest

`test_*.py` here use the standard library `unittest`, so they stay runnable
without a `uv sync`. They are *also* collected by `uv run pytest` from the
repo root — `testpaths` includes this directory — which means the root
`conftest.py` sandbox applies when they run that way and not when they are
run directly. See `test/AGENTS.md` before writing one.

## The `dev_status` stack: module map

The backlog engine is the largest thing in this directory and split across
several files. Before touching it, know which one owns what:

- **`dev_status.py`** — thin CLI entrypoint; imports `dev_status_impl` and
  gets out of the way.
- **`dev_status_impl.py`** — CLI command handlers, `render()`, journal/run
  wiring, recap generation/caching/dispatch. Side-effectful orchestration.
- **`dev_status_mutation.py`** — the state-transition verbs (`add_item`,
  `start_item`, `done_item`, `review_item`, `set_gate`, `pass_gate`, …) plus
  a claim/PID-liveness cluster (`_is_pid_alive`, `_find_owner_pid`,
  `_detect_harness`, `_make_claim`, `_check_claim_collision` and friends).
- **`dev_status_storage.py`** — atomic writes, locking, the rev counter,
  journal/run-record read/write. No argparse, no CLI.
- **`dev_status_formatting.py`** — pure text/templating (section dividers,
  `ellipsize`, recap prompt assembly). No I/O.
- **`dev_status_read.py`** — read-only queries (`get_item`, `ready_items`,
  `in_progress_items`, `claim_info`) for callers that only need to look, not
  mutate.
- **`dev_status_types.py`** — the on-disk schema: `Gate`, `RunRecord`,
  `BacklogItem`, `PendingItem`, `BacklogIndex`. Single source — nothing
  else defines these. (Collapsed 2026-09-11 from two independent copies,
  one in `dev_status_impl.py` and one in `dev_status_storage.py`, that had
  drifted apart in nothing but docstring completeness so far — don't
  reintroduce a second copy.)
- **`backlog_claim_lookup.py`** — a narrow `Protocol` + local implementation
  for claim-ownership checks, for callers that don't want the full
  `dev_status_read` surface.

**The boundary that decides further splits**: pure/testable logic (I/O
primitives, text templating, read-only queries, schema) gets its own
module; CLI orchestration and side-effectful state transitions stay in
`dev_status_impl.py` / `dev_status_mutation.py`. Apply that same test
before splitting anything further — not line count. The mutation verbs
share one rev-guard/lock/journal contract, which is why they stay in one
file despite its size: splitting them apart from each other would scatter
that shared contract across files without reducing any actual coupling.

By that same test, the claim/PID-liveness cluster in
`dev_status_mutation.py` is a plausible next module (it's self-contained
process introspection, not item-state-transition logic) — flagged, not yet
done.
