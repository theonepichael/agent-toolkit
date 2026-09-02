---
name: analyze-sessions
description: Analyze coding-agent sessions across pi, Claude Code, opencode, Copilot CLI, and agy: calculate token/USD cost rollups, list user prompts, or search message transcripts. Use when the user asks about session costs, token usage, previous prompts, or wants to search past coding session transcripts across harnesses.
---

Analyze coding-agent sessions across harnesses using `analyze_sessions.py`.

Run:

```bash
python3 ~/.claude/scripts/analyze_sessions.py <subcommand> [flags]
```

### Subcommands

- **Cost and token analysis**:
  ```bash
  python3 ~/.claude/scripts/analyze_sessions.py cost [--by total|day|project|harness|model|session] [--since <date>] [--until <date>] [--cwd <path>] [--model <name>] [--session <id>] [--harness all|pi|claude|opencode|copilot|agy] [--no-subagents] [--json]
  ```
  Calculates token breakdowns and USD costs across harnesses.

- **List prompts**:
  ```bash
  python3 ~/.claude/scripts/analyze_sessions.py prompts [--format markdown|jsonl] [--since <date>] [--until <date>] [--cwd <path>] [--grep <pattern>] [--limit N] [--harness all|pi|claude|opencode|copilot|agy] [--include-subagents] [--json]
  ```
  Lists user prompts across sessions.

- **Search message transcripts**:
  ```bash
  python3 ~/.claude/scripts/analyze_sessions.py search "<query>" [--regex] [--context N] [--since <date>] [--until <date>] [--cwd <path>] [--harness all|pi|claude|opencode|copilot|agy] [--include-subagents] [--json]
  ```
  Searches user and assistant messages across session transcripts.
