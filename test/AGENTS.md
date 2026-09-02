# test/ — agent notes

Only what is easy to get wrong here; general conventions are in the repo
root's `AGENTS.md` and `STYLE.md`.

## Every test runs in a sandbox, and it fails loudly when you leave it

The repo-root `conftest.py` applies to the whole suite, before any test
runs. It:

- redirects `HOME` to a throwaway directory,
- blocks every real `subprocess.Popen` (so `run`, `call` and
  `check_output` too),
- blocks writes, deletes, renames and `mkdir` under the **real**
  `~/.claude` and `~/.config`.

A test that trips one of these dies with a `RuntimeError`, not a normal
assertion failure, and the message does not look like a missing-marker
problem — it looks like the code under test is broken. It is not. Add the
marker:

```python
@pytest.mark.allow_real_subprocess   # then say what it runs, and why that is safe
@pytest.mark.allow_production_paths  # also restores the real HOME for this test
```

Reach for a marker only when the test genuinely needs the real thing —
`test_pi_ts_checks.py` shelling out to `bun` is the canonical case. Mocking
at the subprocess boundary is still the default; the markers are the
exception, and each one should carry a comment saying why.

Note the asymmetry: `allow_production_paths` also puts the **real** `HOME`
back for that test. It is not just a write permit.

## Two tiers, and they are not interchangeable

- **`test/`** — pytest. Covers the top-level tooling: the installer,
  departure mode, lint gates, and the cross-harness guards.
- **`claude/scripts/test_*.py`** — standard library `unittest`, colocated
  with the scripts they cover and deliberately dependency-free, so those
  tools stay runnable on a machine that has never run `uv sync`. Keep them
  importable and runnable as `python3 test_X.py` from `claude/scripts/`.

Both are collected by `uv run pytest` — `pyproject.toml` sets
`testpaths = ["test", "claude/scripts"]` — so the `conftest.py` guards above
apply to the colocated tests too when they run that way, and not when they
are run directly with `python3`.

## `scenarios.sh` is container-only

`test/run.sh` drives `test/scenarios.sh` inside throwaway Docker containers.
**Never run `scenarios.sh` directly on a real machine.** It mutates real
state, including a git-tracked file, and has already done so once.

## Ruff does not lint this directory

`pyproject.toml` sets `extend-exclude = ["**/test_*.py", "test/"]`. Test
files are lint-exempt but should still follow the same conventions as the
rest of the repo — type hints included.
