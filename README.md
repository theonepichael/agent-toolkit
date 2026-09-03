# Agent Toolkit

A modular, multi-harness platform for AI agent workflows and paired development across:
- **Claude Code** (Anthropic)
- **GitHub Copilot CLI** (GitHub)
- **OpenCode** (Local / Multi-provider TUI)
- **Google Antigravity (AGY)** (Google DeepMind)
- **Pi** (Lightweight extensible terminal assistant)

---

## Features & Capabilities

- **Unified Core Workflows**: Standardized slash commands and skills across harnesses (`/grill-me` for design alignment, `/second-opinion` for architectural critique, `/spec` for structured specifications, `/backlog-item` for gated TDD lifecycle, `/standup` for daily progress synthesis, `/dashboard` for work tracking).
- **Automated Guard Rails**: Pre-tool hooks and git pre-commit hooks that prevent committing directly to `main` and protect active task checkouts.
- **Auto-Formatting on Tool Use**: Automatic Ruff formatting/lint fixing on Python edits.
- **Shell Completions & Environment Helpers**: Portable `shell/agent-tools.zsh` providing completions, PATH setup, and harness aliases.
- **Cross-Harness Code Generation**: Generators keep documentation (`INTERFACES.md`) and prompt templates (`templates/*.tmpl`) in sync across all 5 harnesses.

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

System tools:
- **Python 3.12+**
- **Git**
- Optional: `uv` (recommended for test execution), `bun` / `node` (for Pi TypeScript extensions)

---

## Quick Start & Installation

Clone the repository and run the provisioner:

```bash
git clone git@github.com:theonepichael/agent-toolkit.git ~/Workspace/agent-toolkit
cd ~/Workspace/agent-toolkit
./install.sh
```

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

## Harness Setup Notes

| Harness | Primary Config | Skills / Prompts Path | Extensions / Hooks |
| :--- | :--- | :--- | :--- |
| **Claude Code** | `~/.claude/CLAUDE.md` | `~/.claude/commands/` | `~/.claude/scripts/guard_rails.py` |
| **GitHub Copilot** | `~/.copilot/copilot-instructions.md` | `~/.copilot/skills/` | `~/.copilot/hooks/` |
| **OpenCode** | `~/.config/opencode/opencode.jsonc` | `~/.config/opencode/commands/` | `~/.config/opencode/plugin/` |
| **Antigravity (AGY)** | `~/.gemini/GEMINI.md` | `~/.gemini/antigravity-cli/skills/` | `~/.gemini/config/hooks.json` |
| **Pi** | `~/.pi/agent/AGENTS.md` | `~/.pi/agent/prompts/` | `~/.pi/agent/extensions/` |

**Structured choices in Pi.** Pi has no *built-in* multi-choice prompt, but the bundled `question-tool.ts` extension supplies a `question` tool for interactive sessions. It is a hard error in headless `-p`/JSON modes and absent when Pi is launched with `--no-extensions`, so the ported skills route enumerable judgment calls to the `question` tool and fall back to plain conversational text only where it is genuinely unavailable.

---

## Development & Verification

Run the test suite:

```bash
uv run pytest
```

Check interface and skill documentation consistency:

```bash
python3 claude/scripts/gen_interfaces.py --check
python3 claude/scripts/gen_skills.py
python3 claude/scripts/gen_second_opinion.py
```
