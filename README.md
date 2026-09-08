# Agent Toolkit

A modular, multi-harness platform for AI agent workflows and paired development across:
- **Claude Code** (Anthropic)
- **GitHub Copilot CLI** (GitHub)
- **OpenCode** (Local / Multi-provider TUI)
- **Google Antigravity (AGY)** (Google DeepMind)
- **Pi** (Lightweight extensible terminal assistant)
- **Codex CLI** (OpenAI) — best-effort tier

> **Status: not yet independent, but no longer manual.** This repository
> began as a snapshot of a personal dotfiles repo, which is still where
> harness work lands first — reconciliation is now
> `scripts/sync_from_dotfiles.py`, a repeatable command, not a hand-derived
> diff. A private GitHub remote exists; coworker access isn't open yet. See
> [MIGRATION.md](MIGRATION.md) for what remains and the order it must be done
> in.

---

## Features & Capabilities

- **Unified Core Workflows**: Standardized slash commands and skills across harnesses (`/grill-me` for design alignment, `/second-opinion` for architectural critique, `/spec` for structured specifications, `/backlog-item` for gated TDD lifecycle, `/standup` for daily progress synthesis, `/dashboard` for work tracking).
- **Automated Guard Rails**: Pre-tool hooks and git pre-commit hooks that prevent committing directly to `main` and protect active task checkouts (see [Git hooks](#git-hooks)).
- **Seed Rewrite Guard**: A pre-commit check refuses a commit whose staged seed would lose SessionStart hook groups relative to HEAD — the lossy-rewrite race that once committed a seed without its matcher-`*` herdr group. Intentional drops use `SEED_HOOK_ALLOW_DROP=1`, never `--no-verify`.
- **Auto-Formatting on Tool Use**: Automatic Ruff formatting/lint fixing on Python edits.
- **Shell Completions & Environment Helpers**: Portable `shell/agent-tools.zsh` providing completions, PATH setup, and harness aliases.
- **Cross-Harness Code Generation**: Generators keep documentation (`INTERFACES.md`) and prompt templates (`templates/*.tmpl`) in sync across all 6 harnesses.

---

## Prerequisites: Install your chosen harness(es)

Install whichever CLI harness(es) you plan to use:

| Harness | Recommended Installation |
| :--- | :--- |
| **Claude Code** | `npm install -g @anthropic-ai/claude-code` |
| **GitHub Copilot CLI** | `npm install -g @github/copilot` |
| **OpenCode** | `curl -fsSL https://opencode.ai/install \| bash` |
| **Google Antigravity (AGY)** | Internal workstation installation |
| **Pi** | `npm install -g @mariozechner/pi-cli` |
| **Codex CLI** | `npm install -g @openai/codex` |

System tools:
- **Python 3.12+**
- **Git**
- Optional: `uv` (recommended for test execution), `bun` / `node` (for Pi TypeScript extensions)

### Git hooks

The repo ships `githooks/pre-commit` (no-commit-on-main, a seed SessionStart-subset guard, and the generated-doc checks), but git reads the hook path from config, which cannot be a committed file — wire it once per clone (worktrees of the clone inherit it):

```bash
git config core.hooksPath githooks
```

To commit an intentional SessionStart hook-group drop without disabling the rest of the hook:

```bash
SEED_HOOK_ALLOW_DROP=1 git commit ...
```

---

## Quick Start & Installation

Clone the repository and run the provisioner:

```bash
git clone git@github.com:theonepichael/agent-toolkit.git ~/Workspace/agent-toolkit
cd ~/Workspace/agent-toolkit
./install.sh
```

### Fresh worktrees: bootstrap dependencies first

`node_modules` is untracked, so a fresh `git worktree add` of this repo has
no `pi/node_modules` (and no root venv) — the pi TypeScript checks skip
silently until it exists. Run the per-directory installs in one
step after creating a worktree:

```bash
scripts/bootstrap-worktree.sh   # uv sync at the root + bun install in pi/
```

It warns (and skips that step) if `uv` or `bun` is missing, and is safe to
rerun.

### Options & Flags

```bash
# Provision specific harnesses only:
./install.sh --harness=claude,pi

# Work profile (skips personal sync endpoints):
./install.sh --profile=work

# Dry run (preview symlinks and config without mutating filesystem):
./install.sh --dry-run

# Audit symlinks against manifest:
./install.sh --check-links

# Roll back all symlinks and mutations:
./install.sh --rollback
```

### Shell Integration

Add this line to your `~/.zshrc` (or `~/.bashrc`):

```zsh
[[ -f "$HOME/.agent-tools.zsh" ]] && source "$HOME/.agent-tools.zsh"
```

---

## What you supply vs. what the toolkit creates

The toolkit never asks you to configure anything before it runs — but a few
scripts read state from outside this repo, and knowing which of those paths
the toolkit **creates**, which it **expects**, and which are **optional** is
the difference between a working install and silent fallbacks.

### Paths

| Path | Status | What it holds |
| :--- | :--- | :--- |
| `~/.claude/data/` | **Created on first use** by `dev_status.py` | The backlog/pending store (`backlog.json`, journal, out-of-scope concepts). Per-user by construction — it lives in your home, not in the repo. Hardcoded location: `Path.home() / ".claude" / "data"`; there is no `XDG_DATA_HOME` support. |
| `~/.claude/data/grill/` | **Created on first use** by `grill.py` and `second_opinion.py` | Spec, plan, and critique artifacts written by the `/spec`, `/grill-me`, and `/second-opinion` skills. Same hardcoded base path as above. |
| `~/.claude/data/to-tickets/` | **Created on first use** by `to_tickets_runner.py` | Batch files drafted by the `/to-tickets` skill. |
| `~/.secrets` (or wherever you keep shell env) | **Expected, user-supplied — never created by the installer** | This machine's `SECOND_OPINION_*` model pools live here. The toolkit itself never opens this file — it reads environment variables, however you set them. |

### Environment variables (all optional)

`second_opinion.py`'s model pools are configured entirely through the
environment. Every variable below is optional — unset means the backend
picks its own default model — but if you want pool rotation or pinned
models, these are the contract:

| Variable | Purpose | Unset behavior |
| :--- | :--- | :--- |
| `SECOND_OPINION_AGY_MODEL` | Force the agy backend's model | Built-in default (`Gemini 3.7 Flash (High)`) |
| `SECOND_OPINION_AGY_MODEL_POOL` | Comma-separated agy pool for `--model-index` rotation | agy falls back to the single override or its built-in default |
| `SECOND_OPINION_PI_MODEL` / `_PI_MODEL_POOL` | Same contract for the pi backend | pi's own default for the `opencode-go` provider |
| `SECOND_OPINION_OPENCODE_MODEL` / `_OPENCODE_MODEL_POOL` | Same contract for the opencode backend | opencode's live config |
| `SECOND_OPINION_COPILOT_MODEL` / `_COPILOT_MODEL_POOL` | Same contract for the copilot backend | copilot's implicit default routing |
| `SECOND_OPINION_TIMEOUT_SECONDS` (+ per-backend `_<BACKEND>_TIMEOUT_SECONDS`) | Per-call timeout budget in seconds | 120 (hard ceiling 300) |

Two things worth knowing before you copy someone else's pool values:

- **Copilot's `--model` flag requires a GitHub Copilot Pro or Enterprise
  plan.** On a free tier, every pooled model id is rejected with `Model X
  from --model flag is not available` — that wording describes an
  entitlement failure, not a wrong id. The script re-raises it as an
  entitlement error telling you to unset the copilot pool; on a free-tier
  account, leave `SECOND_OPINION_COPILOT_MODEL_POOL` unset.
- **A missing pool is announced, not silent.** A review whose dispatched
  backend has no pool and no single-model override prints a one-line stderr
  notice naming the variable, where to set it, and a realistic example
  (suppressed by `--quiet`). The run still proceeds with the backend's
  default model — the notice is there so an unconfigured machine says so
  instead of quietly degrading.

Example pool configuration (in `~/.zshrc`, or a sourced secrets file such
as `~/.secrets`):

```zsh
export SECOND_OPINION_AGY_MODEL_POOL="Gemini 3.7 Flash (High),Gemini 3.7 Pro (High)"
export SECOND_OPINION_PI_MODEL_POOL="opencode-go/glm-5.2,opencode-go/glm-5.3-flash"
```

---

## Harness Setup Notes

| Harness | Primary Config | Skills / Prompts Path | Extensions / Hooks |
| :--- | :--- | :--- | :--- |
| **Claude Code** | `~/.claude/CLAUDE.md` | `~/.claude/commands/` | `~/.claude/scripts/guard_rails.py` |
| **GitHub Copilot** | `~/.copilot/copilot-instructions.md` | `~/.copilot/skills/` | `~/.copilot/hooks/` |
| **OpenCode** | `~/.config/opencode/opencode.jsonc` | `~/.config/opencode/commands/` | `~/.config/opencode/plugin/` |
| **Antigravity (AGY)** | `~/.gemini/GEMINI.md` | `~/.gemini/antigravity-cli/skills/` | `~/.gemini/config/hooks.json` |
| **Pi** | `~/.pi/agent/AGENTS.md` | `~/.pi/agent/prompts/` | `~/.pi/agent/extensions/` |
| **Codex CLI** | `~/.codex/AGENTS.md` | `~/.codex/skills/` | none provisioned |

**Codex notes.** Codex CLI (best-effort tier, like Copilot/AGY — see AGENTS.md's
"Harness maintenance tiers") reads global instructions from `~/.codex/AGENTS.md`
and USER-scope skills from `~/.codex/skills/` (sibling to the bundled
`~/.codex/skills/.system/` skills, not the `~/.agents/skills/` shared
namespace other harnesses use — confirmed via `codex debug prompt-input`'s
skill-roots table on a live 0.153.4 install; no exclusivity audit, per-file
drift checks only). One caveat: Codex caps the combined instruction chain at
`project_doc_max_bytes` (32 KiB default), and the shared instructions file is
~27 KB of that budget, leaving ~5 KB headroom for a repo's own AGENTS.md
chain — raise `project_doc_max_bytes` in `~/.codex/config.toml` if a
heavy-AGENTS.md repo truncates. `~/.codex/config.toml` itself (auth, model,
trust) is never provisioned or managed.

**Structured choices in Pi.** Pi has no *built-in* multi-choice prompt, but the bundled `question-tool.ts` extension supplies a `question` tool for interactive sessions. It is a hard error in headless `-p`/JSON modes and absent when Pi is launched with `--no-extensions`, so the ported skills route enumerable judgment calls to the `question` tool and fall back to plain conversational text only where it is genuinely unavailable.

---

## Development & Verification

Run the test suite:

```bash
uv run pytest
```

Check interface and skill documentation consistency:

```bash
python3 agent-scripts/gen_interfaces.py --check
python3 agent-scripts/gen_skills.py
python3 agent-scripts/gen_second_opinion.py
```


## Measuring skill latency

Set `AGENT_TOOLKIT_TIMING=1` in the environment of the harness running your
skills. Instrumented `dev_status.py` and `second_opinion.py` calls append
JSONL to `$XDG_STATE_HOME/agent-toolkit/timing.jsonl`, defaulting to
`~/.local/state/agent-toolkit/timing.jsonl`. Unset the variable to stop recording.
For example, to measure a dashboard call:

```sh
AGENT_TOOLKIT_TIMING=1 python3 ~/.claude/scripts/dev_status.py render
```

Each record has `name`, `started_at`, `duration_seconds`, `outcome`, `pid`,
`trace_id`, `span_id`, and `parent_id`. Command records name the script and
subcommand; second-opinion backend records identify the backend and candidate
order; subprocess attempt records include attempt number and exit code when
available. Timeout attempts remain visible even when a retry succeeds. Records
exclude arguments, prompts, outputs, item contents, and exception messages.
Timing is opt-in, best-effort, and does not change normal stdout or exit codes.

The script span measures `main()`, including argument parsing, but excludes
Python startup and imports. Command, preparation, backend, and attempt spans
are nested: **do not add their durations together**. Each backend candidate
contains its attempts; increasing candidate numbers expose fallback and
increasing attempt numbers expose retries. The shared subprocess instrumentation
also covers second-opinion's custom opencode path. Detached recap children have
separate traces; their time is not dashboard latency. Other scripts using the
shared backend runner can emit standalone attempt spans.

Compare a script span with its enclosing session tool-call interval to locate
waits outside the script. Approval time, shell startup, output transport and
agent scheduling are not measured by these records. The log does not rotate;
remove or archive it when finished. New log files are created with mode 0600.
