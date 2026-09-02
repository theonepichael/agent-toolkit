# claude/scripts/ — agent notes

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
python3 claude/scripts/gen_interfaces.py           # rewrite
python3 claude/scripts/gen_interfaces.py --check   # 1 = stale, 3 = doc drift
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
