# STYLE.md

House style (short, prescriptive)

Scope & philosophy
- Uniformity is paramount. Keep interfaces small, explicit, and testable.
- No runtime third-party dependencies in harness code. The agent-scripts tools and their tests (test/test_*.py, one per module) must stay runnable with the system Python and the standard library alone.
- Development tooling is a separate concern: test/ and CI use uv with pinned pytest and ruff. Keep those dependencies out of anything that runs at harness runtime.

Python
- Target: Python 3.12+ for all non-trivial scripts.
- Shebangs: use #!/usr/bin/env python3 for Python entrypoints.
- Use the standard library for CLIs: argparse or getopt only (no external CLI libs). Prefer argparse for new CLIs.
- Type hints are required on every function/method signature (all parameters and the return type), using modern 3.12+ syntax: built-in generics (`list[str]`, `dict[str, int]`), not `typing.List`/`typing.Dict`; `X | None`, not `Optional[X]`; `X | Y`, not `Union[X, Y]`. Avoid `Any` unless genuinely unavoidable; prefer `object` or a narrower union. Enforced by ruff's ANN rules (see Formatting & linting).
- Keep modules importable from repository root (tests may insert repo root on sys.path).

Shell
- Bootstraps and tiny wrappers: POSIX sh (#!/usr/bin/env sh).
- User shell config under zsh/ should be explicit zsh only; document shell-specific files.
- Strict shell invocation where appropriate: set -eu; use set -o pipefail in bash scripts that require it.

CLI ergonomics
- Use long-form flags with short aliases where appropriate (e.g., --verbose / -v).
- Prefer subcommands for multi-action tools (argparse subparsers).
- Provide --help and clear exit codes. 0 = success; nonzero for failures.

Config & secrets
- Config: JSON, under the owning tool's data directory — e.g. ~/.agent-toolkit/data/standup/config.json, with backlog state in ~/.agent-toolkit/data/backlog/. Harness settings stay in their tool-owned files (~/.claude/settings.json, ~/.config/opencode/opencode.jsonc).
- Prefer JSON over YAML for new config: the standard library parses JSON, and YAML would pull in a third-party dependency the harness rule above forbids.
- XDG paths where a tool writes transient state: honor $XDG_STATE_HOME, falling back to ~/.local/state.
- Precedence: CLI flags > ENV vars > per-user config > system defaults.
- Secrets: Must never be committed. Use environment variables or system vaults. Add checks in code and tests to avoid accidental logging of secrets.

Logging & output
- Scripts should write normal results to stdout and diagnostics to stderr.
- Provide --quiet / --verbose toggles via `cli_common.add_verbosity_args` (agent-scripts/cli_common.py), which also supplies the `qprint` / `vprint` output helpers; gate non-essential stdout with `qprint(..., quiet=quiet)` and diagnostics with `vprint(..., verbose=verbose)`.
- Keep prompts and secrets out of logs by default.

Tests & CI
- One directory (test/), two intentional styles; keep a new test in whichever style matches the code under test.
- Most of test/ — pytest suites covering the top-level tooling: the installer and departure mode, the lint gates, and the cross-harness invariant guards. Run with `uv run pytest test/`.
- test/test_*.py covering an agent-scripts/ module (test_dev_status.py, test_grill.py, and the rest) — standard library unittest, deliberately dependency-free so those tools stay verifiable without a `uv sync`. Each opens with `sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agent-scripts"))` before importing its sibling module; that line is what makes the file runnable both directly (`python3 test_X.py` from test/) and via `uv run pytest` from the repo root (`testpaths` in pyproject.toml is `["test", "scripts"]`; `scripts/` currently has no `test_*.py` of its own) — moved here 2026-09-18 off of agent-scripts/, where they used to sit colocated with the modules they covered.
- The repo-root conftest.py sandboxes `HOME` and blocks unmarked real subprocess calls and production-path writes, for the whole suite. A test that needs either must opt in with a marker, and the failure when it does not is a RuntimeError that reads like broken code rather than a missing marker — read test/AGENTS.md before writing one. (Marker names live there, not here, so there is only one place to update if they change.)
- Dev tooling is declared in the `dev` dependency group in pyproject.toml (pytest and ruff, both pinned) and managed with uv; uv.lock is committed and CI installs from it.
- Tests should not require network or live LLMs. Mock at the subprocess boundary (`_run_command` / `run_backend_command`) rather than invoking real harness binaries; the sole harness exception is the local `opencode --version` SDK-compatibility tripwire, which starts no model or network work.
- CI (.github/workflows/python-quality.yml) runs on pushes to main and on every pull request: `uv sync --locked --dev`, then `uv run ruff check .`, `uv run ruff format --check .`, then a bare `uv run pytest` — which covers both styles via `testpaths`, rather than naming individual files or running a separate unittest discovery step.
- test/run.sh drives the containerized install.sh scenario suite (test/scenarios.sh) against Ubuntu and Fedora images. It needs Docker/Podman and is run locally, not in CI. Never run scenarios.sh directly on a real machine — it mutates real state.

TypeScript
- Two live TypeScript trees have npm toolchains: `pi/` (extensions plus specs under `pi/test/`) and `opencode/` (plugins plus specs under `opencode/test/`). `agy/hooks/*.js` remains a single-file outlier with no toolchain. The separate `copilot/extensions/swarm/src/` build is a host adapter bundled by `scripts/build-copilot-swarm.sh`; scheduling, recovery, the shared `SwarmToolContext`, and both picker adapters still have one source under `pi/extensions/swarm-lib/`. See the directory agent files for details.
- Toolchains are npm-driven and declared independently in `pi/package.json` and `opencode/package.json`: `node:test` via `tsx`, `tsc --noEmit`, oxlint, and Prettier. Run each from its own project root.
- `test/test_pi_ts_checks.py` and `test/test_opencode_ts_checks.py` drive all four stages from pytest, so both toolchains gate CI. Each **fails** rather than skips when its untracked `node_modules` tree is absent.
- Lint and format are deliberately scoped to each project's source and test directories, keeping directory-level markdown and symlinks out of Prettier's TypeScript path.

Formatting & linting
- Ruff is the enforced formatter and linter, configured under [tool.ruff] and [tool.ruff.lint] in pyproject.toml (`line-length = 88`; E, F, W, UP, SIM, I, PIE, ISC, FURB, TRY, ANN selected). No black, no isort.
- Note that E501 is in the `ignore` list: the formatter wraps to 88 columns, but the linter does not fail a line that exceeds it. DTZ011, DTZ005, PLW1510, BLE001 and TRY003 are ignored too.
- ANN (flake8-annotations) enforces the type-hint requirement above on every function, new or existing.
- Run `uv run ruff format .` and `uv run ruff check --fix .` before committing Python changes. CI fails on either check, and test/test_lint.py fails the suite as well.
- Ruff excludes test files (`**/test_*.py`, `test/`). They are lint-exempt, but should still follow the same conventions.
- Shell files are enforced by `test/lint_shell.sh`: it runs `shellcheck --severity=warning` and `shfmt -i 2 -ci -d` over an explicit in-scope list (`install.sh`, `scripts/bootstrap-worktree.sh`, `scripts/rehearse-toolkit-home-migration.sh`, `test/run.sh`, `test/scenarios.sh`, and `test/lint_shell.sh` itself). The pytest suite runs it via `test/test_shell_lint.py`, which is what CI's bare `uv run pytest` step covers it with; run the script locally before committing shell changes too. The list is not every shell file in the repo — `githooks/pre-commit`, `githooks-global/lib/no-commit-on-main.sh`, and `herdr_remote/deploy/deploy.sh` are shell and are not covered.

Files & docs
- Add a short module docstring to each CLI script listing: flags, env vars, files read/written, and primary exit codes. This discipline is what lets INTERFACES.md be generated from source.
- INTERFACES.md is the interface inventory for the harness scripts, generated from those docstrings and argparse definitions. Fix the source when an interface changes; do not hand-edit the generated inventory. Regenerate with `python3 agent-scripts/gen_interfaces.py`, or check for staleness with `--check` (exit 1 if the committed file is stale, exit 3 if a doc's own shown command example no longer matches the script's real contract — both are what the test suite and `githooks/pre-commit` assert against).
- User-facing commands are documented in README.md, per harness — that is where behavioral differences between the Claude Code, Copilot, opencode, agy and Pi ports belong.
- Directory-level agent instructions live in `<dir>/AGENTS.md` with a `CLAUDE.md` symlink beside it (no single filename reaches all five harnesses). Write one only when the directory has a convention an agent would otherwise get wrong; the repo root's AGENTS.md carries the rationale and the index.

