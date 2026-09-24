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
  `~/.claude`, `~/.config`, `~/.local/state/agent-toolkit`, and
  `~/.agent-toolkit`.

The path guard resolves every path argument the way the kernel would —
relative and `..`-laden paths against the call's `dir_fd=` anchor (via the
platform fd link) or the process cwd, following symlink aliases — and
denies a resolved landing inside a guarded root. The sandboxed `HOME` is
still what keeps the *sandbox* itself off real state, so do not build test
paths from the real home.

One documented limit remains: on a platform with neither
`/proc/self/fd` nor `/dev/fd`, a `dir_fd=` call's relative path cannot be
resolved and is not checked.

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

## One directory, two styles — and they are not interchangeable

Every test in the repo lives here now (moved 2026-09-18 off of
`agent-scripts/`, where `test_*.py` used to sit colocated with the modules
they covered — two homes for tests was the actual complaint; the fix was
consolidating the location, not the style below).

- **Most of `test/`** — pytest. Covers the top-level tooling: the
  installer, departure mode, lint gates, and the cross-harness guards.
- **The former `agent-scripts/test_*.py` files** (`test_dev_status.py`,
  `test_grill.py`, `test_second_opinion.py`, and the rest covering
  `agent-scripts/` modules) — standard library `unittest`, still
  deliberately dependency-free so those tools stay verifiable on a machine
  that has never run `uv sync`. Each does
  `sys.path.insert(0, str(Path(__file__).resolve().parent.parent /
  "agent-scripts"))` before importing its sibling production module — that
  line is why the file works both as `python3 test_X.py` from `test/` and
  under pytest from the repo root; don't delete it when editing one of
  these, and give a new one the same line rather than relying on being
  colocated.

  A file in this tier that wants pytest markers imports them through the
  stdlib-only shim, `from pytest_shim import pytest` (see
  `test/pytest_shim.py`), never a bare `import pytest` — under pytest the
  shim re-exports the real module; on a direct run it hands back an
  identity stand-in whose `mark.<anything>` accepts both bare and called
  decorator forms. The shim covers markers only: a file needing any other
  pytest API (fixtures, `pytest.raises`, annotations like
  `pytest.MonkeyPatch`) is not dependency-free and must keep a real
  `import pytest` — `test_settings_seed.py` is the current precedent.
  The contract is enforced by `test_contract_files_import_without_pytest`
  in `test/test_direct_unittest_sandbox.py` (its `CONTRACT_TEST_FILES` /
  `KNOWN_EXCEPTIONS` lists, kept honest in both directions).

Both styles are collected by the same `uv run pytest` from the repo root
— `pyproject.toml` sets `testpaths = ["test", "scripts"]` (`scripts/` has
no `test_*.py` of its own; the entry currently collects nothing there) —
so the `conftest.py` guards above apply to the unittest-style files too
when they run that way, and — since the shared bootstrap below — when
they are run directly with `python3` as well.

## Both tiers share a common bootstrap

The safety machinery above lives in `agent-scripts/test_bootstrap.py`, not
in `conftest.py`: the sandboxed `HOME`, the guarded mutation-API patches,
and the activation flags. `conftest.py` only wires pytest to it (bootstrap
at import, then per-test guard activation honoring the markers), and each
unittest-style file calls `test_bootstrap.run_unittest_main()` from its
`__main__` block, so a direct run is sandboxed too:

- `HOME` is redirected to a throwaway sandbox directory before any test
  runs (also before any production module that bakes `Path.home()`-rooted
  constants is imported — that is why the `import test_bootstrap` line
  sits at the top of the file, not in `__main__`).
- Path mutation guards are active for both tiers: writes to `~/.claude`,
  `~/.config`, `~/.local/state/agent-toolkit`, and `~/.agent-toolkit` are
  blocked.

### Direct-run divergences from pytest

1. **Subprocess guard is off for direct runs**: plain unittest applies no
   marker machinery — under `pytest_shim` the marker decorators are
   accepted but inert, so `@pytest.mark.allow_real_subprocess` neither
   fails nor opts in — and the
   unittest-style tests frequently spawn subprocesses (`git`, child
   python interpreters). The sandboxed `HOME` limits blast radius.
2. **Path guard has no direct-run opt-out**: there is no
   `allow_production_paths` equivalent under plain unittest. Any test
   requiring access to real production paths must run under pytest.

## Regression markers

A regression test carries `@pytest.mark.regression(label, red)` where
`label` is a kebab-case name describing the regression itself (e.g.
`sandbox-guard-blocks-real-home-write`) and `red` is the verbatim pre-fix
failure string actually observed during the red run. Keep ticket linkage
in the backlog store, not in git history — the label is deliberately not
a ticket id. Enforced statically across `test/` via
`scripts/check_regressions.py`. Bare `@mark.regression` is disallowed.

## `scenarios.sh` is container-only

`test/run.sh` drives `test/scenarios.sh` inside throwaway Docker containers.
**Never run `scenarios.sh` directly on a real machine.** It mutates real
state, including a git-tracked file, and has already done so once.

## Ruff does not lint this directory

`pyproject.toml` sets `extend-exclude = ["**/test_*.py", "test/"]`. Test
files are lint-exempt but should still follow the same conventions as the
rest of the repo — type hints included.
