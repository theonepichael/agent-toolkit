# INTERFACES.md

Scope: `claude/`, `copilot/`, `opencode/`, `agy/`, `pi/`, the shared scripts under
`agent-scripts/` that `links.toml` installs into `~/.claude/scripts/`, and the
repo-root installer entrypoints those harnesses are provisioned by.

**This file is generated. Do not edit it by hand — your edits will be
overwritten.** Regenerate it after changing any harness script:

```bash
python3 agent-scripts/gen_interfaces.py           # rewrite this file
python3 agent-scripts/gen_interfaces.py --check   # exit 1 if it is stale
```

Everything below is extracted statically, with `ast`, `tomllib`, and a small
frontmatter reader. No harness module is imported and no script is run — these
scripts mutate live state under `~/.claude`, so documenting them must not
execute them. The cost of that choice is that anything invisible to static
analysis is reported as unknown rather than guessed at: `choices=` computed at
runtime, hand-rolled `sys.argv` dispatch, and behaviour that only exists in
prose. Where this file says a detail is not statically visible, read the
source.

"Public" below means exactly one thing: defined at module level without a
leading underscore. That is a naming convention, not a curated API — a helper
that simply was never underscored will appear here.

House style for these interfaces is in `STYLE.md`.

---

## 1. Shared scripts (`agent-scripts/`)

| Module | Purpose |
| --- | --- |
| [`analyze_sessions.py`](#agentscriptsanalyzesessionspy) | analyze_sessions.py — multi-harness session analysis tool. |
| [`cli_common.py`](#agentscriptsclicommonpy) | Shared CLI helpers used across dotfiles scripts. |
| [`dev_status.py`](#agentscriptsdevstatuspy) | dev_status.py v2 — slug IDs, structured dependency graph, pure render. |
| [`dotfiles_sync_check.py`](#agentscriptsdotfilessynccheckpy) | SessionStart hook: flag when the dotfiles repo has drifted from the last commit bundled over to a GitHub-blocked work machine. |
| [`gen_interfaces.py`](#agentscriptsgeninterfacespy) | gen_interfaces.py — regenerate INTERFACES.md mechanically from the sources. |
| [`gen_second_opinion.py`](#agentscriptsgensecondopinionpy) | gen_second_opinion.py — regenerate the second-opinion skill copies (one per harness, named in HARNESS_TABLE) from one canonical template. |
| [`gen_shell_completion.py`](#agentscriptsgenshellcompletionpy) | Generate a zsh `#compdef` completion file for a harness CLI. |
| [`gen_skills.py`](#agentscriptsgenskillspy) | gen_skills.py — regenerate the dashboard/recap/grill-me/backlog-item/ make-skill/spec/standup/to-tickets/swarm skill copies from one template per skill, plus a shared per-harness capability table. dashboard/recap/ grill-me/backlog-item/make-skill cover all 5 harnesses (claude, copilot, opencode, agy, pi); spec/standup/to-tickets cover only claude/opencode/pi; swarm covers only claude/copilot (user-directed; pi already owns the orchestration surface) — see `SKILL_HARNESSES` below and AGENTS.md's "Harness maintenance tiers" section for why copilot/agy stop getting new generated skills. |
| [`gen_skills_params.py`](#agentscriptsgenskillsparamspy) | gen_skills_params.py — per-(skill, harness) content tables for gen_skills.py. |
| [`grill.py`](#agentscriptsgrillpy) | grill.py — grill-me session state CLI. All session mutations go through here. |
| [`guard_rails.py`](#agentscriptsguardrailspy) | Pre-tool guard shared by every harness: refuse a write into a repository's main checkout while a backlog item for that repository is in progress, warn when the current worktree's base has fallen behind ``origin/main``, (Bash, Claude Code only) deny the git-native ways to defeat the no-commit-on-main git hook (``githooks/pre-commit`` / ``githooks-global/pre-commit``), and require an active backlog-item claim before a write that points at an in-progress item. |
| [`harness_discovery_check.py`](#agentscriptsharnessdiscoverycheckpy) | SessionStart hook + CLI: detect when a harness's instruction-file discovery behavior may have drifted from the version-pinned facts in README.md. |
| [`herdr_delegate.py`](#agentscriptsherdrdelegatepy) | Launch pi agents in herdr tabs to work backlog items. |
| [`link_drift_check.py`](#agentscriptslinkdriftcheckpy) | SessionStart hook + CLI: flag when a managed symlink on this machine no longer points where links.toml says it should. |
| [`llm_backends.py`](#agentscriptsllmbackendspy) | llm_backends.py — shared subprocess plumbing for CLI-agent backends (agy, opencode, pi, copilot). Extracted from second_opinion.py so dev_status.py's recap generation can reuse the same process-lifecycle handling (timeouts, process-group kills, opencode JSON-event parsing) with its own timeout and model choices, without duplicating it. |
| [`notify.py`](#agentscriptsnotifypy) | Cross-platform agent notification dispatcher. |
| [`outlook_calendar.py`](#agentscriptsoutlookcalendarpy) | outlook_calendar.py — CLI tool and agent interface for Windows Outlook Calendar via PowerShell COM. |
| [`outlook_email.py`](#agentscriptsoutlookemailpy) | outlook_email.py — CLI tool and agent interface for Windows Outlook via PowerShell COM. |
| [`refresh_guidance.py`](#agentscriptsrefreshguidancepy) | refresh_guidance.py — audit-by-inspection for hand-authored, agent-facing docs. |
| [`repo_identity.py`](#agentscriptsrepoidentitypy) | repo_identity.py — which repo this checkout is. |
| [`second_opinion.py`](#agentscriptssecondopinionpy) | second_opinion.py — one-shot adversarial critique of a plan from a non-Claude backend. Single-round by design: the multi-round loop, plan revision, and convergence judgment all require LLM reasoning and live in prose instructions, not here. |
| [`seed_hook_subset_guard.py`](#agentscriptsseedhooksubsetguardpy) | seed_hook_subset_guard.py — refuse a commit that drops a seed's SessionStart hook groups. |
| [`sessionstart_checks.py`](#agentscriptssessionstartcheckspy) | sessionstart_checks.py — run the SessionStart context checks concurrently. |
| [`settings_seed_drift_check.py`](#agentscriptssettingsseeddriftcheckpy) | SessionStart hook + CLI: detect (and optionally fix) drift between the live ``~/.claude/settings.json`` / ``~/.config/opencode/opencode.jsonc`` / (under WSL) the Windows-side VS Code ``settings.json`` and ``keybindings.json`` and their seeds in the dotfiles repo. |
| [`standup.py`](#agentscriptsstanduppy) | standup.py — /standup skill CLI: local data gathering. |
| [`standup_adapters.py`](#agentscriptsstandupadapterspy) | standup_adapters.py — provider-agnostic adapter interfaces for /standup. |
| [`statusline.py`](#agentscriptsstatuslinepy) | Claude Code status line: render the model name and a color-coded context window usage bar with the used percentage, from the JSON session payload Claude Code pipes to this script on stdin. |
| [`to_tickets_runner.py`](#agentscriptstoticketsrunnerpy) | to_tickets_runner.py — create a linked batch of dev_status.py backlog items from a confirmed vertical-slice/tracer-bullet ticket breakdown. |
| [`vitals_promotion.py`](#agentscriptsvitalspromotionpy) | vitals-promotion.py — mechanical vitals-promotion pass over grill session data. |

### `agent-scripts/analyze_sessions.py`

analyze_sessions.py — multi-harness session analysis tool.

- Installed at: `~/.claude/scripts/analyze_sessions.py` (all harnesses)
- Entrypoint: executable, `#!/usr/bin/env python3`
- CLI (`argparse`): Multi-harness session analysis tool across pi, Claude Code, opencode, Copilot CLI, and agy.
  - `--quiet/-q`
  - `--verbose/-v`
- Subcommands:
  - `cost [--harness {all,pi,claude,opencode,copilot,agy}] [--since <SINCE>] [--until <UNTIL>] [--cwd <CWD>] [--model <MODEL>] [--session <SESSION>] [--limit <LIMIT>] [--grep <GREP>] [--json] [--quiet] [--verbose] [--by {total,day,project,harness,model,session}] [--no-subagents]` — token and USD cost analysis and rollups
    - `--harness` — harness to analyze: all, pi, claude, opencode, copilot, agy (default: all; note: agy is opt-in and excluded from 'all') (choices: all, pi, claude, opencode, copilot, agy; default: all)
    - `--since` — filter sessions on/after date/time (ISO 8601 YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS, or relative e.g. 7d, today)
    - `--until` — filter sessions on/before date/time (ISO 8601 YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)
    - `--cwd` — filter by working directory substring
    - `--model` — filter by model name substring
    - `--session` — filter by session ID substring
    - `--limit` — limit number of results
    - `--grep/-g` — filter message text by substring or regex
    - `--json` — emit output as JSON
    - `--by` — rollup grouping: total, day, project, harness, model, session (default: total) (choices: total, day, project, harness, model, session; default: total)
    - `--no-subagents` — exclude subagent / child sessions (included by default)
  - `prompts [--harness {all,pi,claude,opencode,copilot,agy}] [--since <SINCE>] [--until <UNTIL>] [--cwd <CWD>] [--model <MODEL>] [--session <SESSION>] [--limit <LIMIT>] [--grep <GREP>] [--json] [--quiet] [--verbose] [--format {markdown,jsonl}] [--include-subagents]` — list user prompts
    - `--harness` — harness to analyze: all, pi, claude, opencode, copilot, agy (default: all; note: agy is opt-in and excluded from 'all') (choices: all, pi, claude, opencode, copilot, agy; default: all)
    - `--since` — filter sessions on/after date/time (ISO 8601 YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS, or relative e.g. 7d, today)
    - `--until` — filter sessions on/before date/time (ISO 8601 YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)
    - `--cwd` — filter by working directory substring
    - `--model` — filter by model name substring
    - `--session` — filter by session ID substring
    - `--limit` — limit number of results
    - `--grep/-g` — filter message text by substring or regex
    - `--json` — emit output as JSON
    - `--format` — output format (default: markdown) (choices: markdown, jsonl; default: markdown)
    - `--include-subagents` — include subagent / child sessions (excluded by default)
  - `search [<query>] [--harness {all,pi,claude,opencode,copilot,agy}] [--since <SINCE>] [--until <UNTIL>] [--cwd <CWD>] [--model <MODEL>] [--session <SESSION>] [--limit <LIMIT>] [--grep <GREP>] [--json] [--quiet] [--verbose] [--regex] [--context <CONTEXT>] [--include-subagents]` — search message transcripts
    - `query` — search query text (nargs: ?)
    - `--harness` — harness to analyze: all, pi, claude, opencode, copilot, agy (default: all; note: agy is opt-in and excluded from 'all') (choices: all, pi, claude, opencode, copilot, agy; default: all)
    - `--since` — filter sessions on/after date/time (ISO 8601 YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS, or relative e.g. 7d, today)
    - `--until` — filter sessions on/before date/time (ISO 8601 YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)
    - `--cwd` — filter by working directory substring
    - `--model` — filter by model name substring
    - `--session` — filter by session ID substring
    - `--limit` — limit number of results
    - `--grep/-g` — filter message text by substring or regex
    - `--json` — emit output as JSON
    - `--regex/-r` — treat query as regular expression
    - `--context/-C` — context lines around match (default: 0) (default: 0)
    - `--include-subagents` — include subagent / child sessions (excluded by default)
- Depends on: `cli_common.py`
- Public classes:
  - `class SessionRecord`
- Public functions:
  - `normalize_timestamp(ts: object) -> tuple[str, datetime | None]` — Convert string ISO timestamp or int/float epoch ms/s into ISO 8601 string and datetime.
  - `parse_date_boundary(val: str | None, *, is_until: bool = False) -> datetime | None`
  - `calculate_claude_cost(model: str | None, in_tok: int, out_tok: int, cr_tok: int, cw_tok: int) -> tuple[float | None, str]`
  - `load_pi_records(base_dir: Path | None = None, since_dt: datetime | None = None, until_dt: datetime | None = None) -> list[SessionRecord]`
  - `load_claude_records(base_dir: Path | None = None, since_dt: datetime | None = None, until_dt: datetime | None = None) -> list[SessionRecord]`
  - `load_opencode_records(db_path: Path | None = None, since_dt: datetime | None = None, until_dt: datetime | None = None, cwd_filter: str | None = None, session_filter: str | None = None) -> list[SessionRecord]`
  - `load_copilot_records(db_path: Path | None = None, since_dt: datetime | None = None, until_dt: datetime | None = None, cwd_filter: str | None = None, session_filter: str | None = None) -> list[SessionRecord]`
  - `load_agy_records(base_dir: Path | None = None, since_dt: datetime | None = None, until_dt: datetime | None = None, session_filter: str | None = None) -> list[SessionRecord]`
  - `load_all_records(harness: str = 'all', since_dt: datetime | None = None, until_dt: datetime | None = None, cwd_filter: str | None = None, model_filter: str | None = None, session_filter: str | None = None, *, include_subagents: bool = True, pi_dir: Path | None = None, claude_dir: Path | None = None, opencode_db: Path | None = None, copilot_db: Path | None = None, agy_dir: Path | None = None) -> list[SessionRecord]`
  - `build_parser() -> argparse.ArgumentParser`
- Subcommand handlers: `cmd_cost`, `cmd_prompts`, `cmd_search`
- Tested by: `agent-scripts/test_analyze_sessions.py`

### `agent-scripts/cli_common.py`

Shared CLI helpers used across dotfiles scripts.

- Installed at: `~/.claude/scripts/cli_common.py` (all harnesses)
- Entrypoint: not executable, no shebang
- CLI: none (library module).
- Environment: `AGENT_TOOLKIT_TIMING`, `XDG_STATE_HOME`
- Public functions:
  - `add_verbosity_args(parser: argparse.ArgumentParser) -> None` — Add mutually-exclusive --quiet/-q and --verbose/-v flags to a parser.
  - `vprint(msg: str, *, verbose: bool, file: TextIO | None = None) -> None` — Print a diagnostic message when verbose mode is enabled.
  - `qprint(msg: str, *, quiet: bool, file: TextIO | None = None) -> None` — Print a message unless quiet mode is enabled.
  - `get_logger(name: str, *, verbose: bool = False, quiet: bool = False) -> logging.Logger` — Return a stderr-only diagnostic logger, a structured complement to vprint.
  - `append_jsonl(path: Path, record: dict[str, object]) -> None` — Best-effort: append one JSON record to `path` as a single JSONL line.
  - `redact_secrets(text: str, *, max_length: int = 200) -> str` — Mask secret-shaped substrings, then truncate to max_length.
  - `timing_span(name: str, **fields: str | int) -> Iterator[dict[str, object]]` — Opt-in nested timings; callers must supply only fixed operational labels.
- Tested by: `agent-scripts/test_cli_common.py`, `agent-scripts/test_timing.py`

### `agent-scripts/dev_status.py`

dev_status.py v2 — slug IDs, structured dependency graph, pure render.

- Installed at: `~/.claude/scripts/dev_status.py` (all harnesses)
- Entrypoint: not executable, `#!/usr/bin/env python3`
- CLI (`argparse`): deterministic backlog dashboard v2
  - `--quiet/-q`
  - `--verbose/-v`
- Subcommands:
  - `render` — render dashboard (pure — no side effects)
  - `list [--status <STATUS>] [--raw]` — grouped backlog table (--raw for tab-separated output)
    - `--status` — only show items with this status (choices computed at runtime)
    - `--raw` — machine-readable TSV (id\tstatus\tsummary) instead of the table
  - `ready [--prefix <PREFIX>]` — print the READY bucket as JSON (open, unblocked, full records)
    - `--prefix` — only items whose slug starts with this (e.g. meta-)
  - `show <slug|N>` — print full JSON for an item
  - `add '{"id": "my-slug", "summary": "...", "priority": "high"}'` — append a new item (id required in JSON)
  - `update <slug|N> '{"field": "value", "priority": "high"}' [--if-rev <N>]` — merge JSON patch into an item
    - `--if-rev` — required when <id> is numeric; get the current value from render/list/show immediately before this call
  - `start <slug|N> [--if-rev <N>] [--force] [--no-worktree-check] [--claimed-by HARNESS]` — mark item in-progress
    - `--if-rev` — required when <id> is numeric; get the current value from render/list/show immediately before this call
    - `--force/-f` — force start even if item is actively claimed by another session
    - `--allow-main/--no-worktree-check` — allow starting item from the main/master repository checkout
    - `--claimed-by` — override claimed harness/session identifier
  - `done <slug|N> [--if-rev <N>]` — mark item done
    - `--if-rev` — required when <id> is numeric; get the current value from render/list/show immediately before this call
  - `review <slug|N> [--if-rev <N>]` — submit (or re-submit) an item for review
    - `--if-rev` — required when <id> is numeric; get the current value from render/list/show immediately before this call
  - `approve <slug|N> [--if-rev <N>]` — approve an in-review item, marking it done
    - `--if-rev` — required when <id> is numeric; get the current value from render/list/show immediately before this call
  - `reject <slug|N> <feedback> [--if-rev <N>]` — reject an in-review item, sending it back to in-progress
    - `--if-rev` — required when <id> is numeric; get the current value from render/list/show immediately before this call
  - `gate-set <slug|N> '{"required": true, "criteria": ["..."]}' [--if-rev <N>]` — classify an item's judgment-verification gate
    - `--if-rev` — required when <id> is numeric; get the current value from render/list/show immediately before this call
  - `gate-pass <slug|N> ['{"coverage": {"1": "run:<run_id>" | "manual:<note>"}}'] [--if-rev <N>]` — record that an item's gate criteria are satisfied
    - `json` — per-criterion coverage: run evidence or a manual note per criterion (nargs: ?)
    - `--if-rev` — required when <id> is numeric; get the current value from render/list/show immediately before this call
  - `run <slug|N> [--if-rev <N>] [--timeout SECONDS] [--cwd PATH] -- <command...>` — execute a command and record it as run evidence for an item
    - `--if-rev` — required when <id> is numeric; get the current value from render/list/show immediately before this call
    - `--timeout` — kill the command after this many seconds (default: 1800) (default: 1800.0)
    - `--cwd` — working directory for command execution (defaults to repo root of item's related_files, or session cwd)
    - `command` — command to execute and record (everything after --; no shell) (nargs: *)
  - `runs <slug|N>` — list recorded run evidence for an item
  - `backfill-gate [--apply]` — stamp an explicit inert gate on legacy items
    - `--apply` — write changes (default: dry run)
  - `rename <slug|N> <new_slug> [--if-rev <N>]` — rename slug (rewrites all references)
    - `--if-rev` — required when <old_slug> is numeric; get the current value from render/list/show immediately before this call
  - `remove <slug|N> [--if-rev <N>]` — permanently remove one item by slug or number
    - `--if-rev` — required when <id> is numeric; get the current value from render/list/show immediately before this call
  - `block <slug|N> <blocker-slug> [--if-rev <N>]` — add a blocker to an item
    - `--if-rev` — required when <id> is numeric; get the current value from render/list/show immediately before this call
  - `unblock <slug|N> <blocker-slug> [--if-rev <N>]` — remove a blocker from an item
    - `--if-rev` — required when <id> is numeric; get the current value from render/list/show immediately before this call
  - `prune --force` — permanently remove done/resolved items older than 14 days
    - `--force` — required to prevent accidental prune (required)
  - `recap [--refresh] [--backend <BACKEND>]` — print a friendly prose recap of recent activity
    - `--refresh` — bypass the freshness cache and regenerate the recap now
    - `--backend` — force this backend instead of priority-order fallback (choices computed at runtime)
  - `pending` — manage pending (waiting-on-reply) items
  - `pending add '{"id", "description", "kind", ["source_ref"], ["context"], ["next_steps"], ["blocking"]}'` — track a new pending item
  - `pending update <slug|N> '{"status": "reply_received", ...}' [--if-rev <N>]` — merge a JSON patch into an existing pending item
    - `--if-rev` — required when <id> is numeric; get the current value from render/list/show immediately before this call
  - `pending list` — list pending items as JSON lines
  - `out-of-scope` — record/browse rejected feature concepts (distinct from 'reject', which sends an in-review item back for rework)
  - `out-of-scope add <concept-slug> --reason-file <path> [--related-item <backlog-slug>]` — record a rejected concept
    - `--reason-file` (required)
  - `out-of-scope link <concept-slug> <backlog-slug>` — reference a backlog item from a rejected concept
  - `out-of-scope unlink <concept-slug> <backlog-slug>` — remove a backlog-item reference from a rejected concept
  - `out-of-scope remove <concept-slug>` — delete a rejected concept's record
  - `out-of-scope list` — list rejected concepts, newest-first
  - `out-of-scope show <concept-slug>` — print a rejected concept's full record
- Environment: `AGY_SESSION`, `ANTHROPIC_CLI`, `ANTIGRAVITY`, `CLAUDE_CODE`, `COPILOT`, `DEVSTATUS_AGENT`, `DEVSTATUS_CLAIM_TTL_SECONDS`, `DEVSTATUS_HARNESS`, `DEVSTATUS_RECAP_AGY_MODEL`, `DEVSTATUS_RECAP_DISABLE`, `DEVSTATUS_RECAP_TIMEOUT_SECONDS`, `GITHUB_COPILOT`, `OPENCODE`, `OPENCODE_GATEWAY`, `PI_CODING_AGENT`, `PI_SESSION`
- Filesystem constants:
  - `DATA_DIR = Path.home() / '.claude' / 'data' / 'backlog'`
  - `ITEMS_FILE = DATA_DIR / 'items.json'`
  - `PENDING_FILE = DATA_DIR / 'pending_items.json'`
  - `META_FILE = DATA_DIR / '_meta.json'`
  - `LOCK_FILE = DATA_DIR / '.backlog.lock'`
  - `JOURNAL_FILE = DATA_DIR / 'journal.jsonl'`
  - `RUNS_FILE = DATA_DIR / 'runs.jsonl'`
  - `MACHINE_ID_FILE = DATA_DIR / '_machine_id'`
  - `RECAP_CACHE_FILE = DATA_DIR / 'recap-cache.json'`
  - `RECAP_REGEN_LOCK_FILE = DATA_DIR / 'recap-regen.lock'`
  - `OUT_OF_SCOPE_DIR = Path.home() / '.claude' / 'data' / 'backlog-out-of-scope'`
  - `OUT_OF_SCOPE_INDEX_FILE = OUT_OF_SCOPE_DIR / 'index.json'`
  - `OUT_OF_SCOPE_LOCK_FILE = OUT_OF_SCOPE_DIR / '.out-of-scope.lock'`
- Explicit exit codes: `1`
- Depends on: `cli_common.py`, `llm_backends.py`
- Public classes:
  - `class Gate(TypedDict)` — A judgment-step verification checkpoint on a backlog item.
  - `class RunRecord(TypedDict)` — One recorded command execution — a row of the ``runs.jsonl`` sidecar.
  - `class BacklogItem(TypedDict)` — A single backlog item as stored in ``items.json`` (schema v2).
  - `class PendingItem(TypedDict)` — A single waiting-on-someone-else item as stored in ``pending_items.json``.
- Public functions:
  - `today() -> str` — Return today's date as an ISO-8601 string (``YYYY-MM-DD``).
  - `machine_id() -> str` — Return this machine's stable short id, creating it on first use.
  - `validate_slug(slug: str, context: str = '') -> str | None` — Validate a candidate item slug.
  - `load_items() -> list[BacklogItem]` — Load all backlog items from :data:`ITEMS_FILE`.
  - `save_items(items: list[BacklogItem]) -> None` — Atomically persist ``items`` to :data:`ITEMS_FILE`.
  - `load_pending() -> list[PendingItem]` — Load all pending items from :data:`PENDING_FILE`.
  - `save_pending(pending_items: list[PendingItem]) -> None` — Atomically persist ``pending_items`` to :data:`PENDING_FILE`.
  - `backlog_lock() -> Iterator[None]` — Hold an exclusive lock over a mutating command's full read-modify-write cycle.
  - `out_of_scope_lock() -> Iterator[None]` — Hold an exclusive lock over an out-of-scope command's read-modify-write cycle.
  - `load_rev() -> int` — Read the current revision counter.
  - `bump_rev() -> int` — Increment and persist the revision counter.
  - `build_index(items: list[BacklogItem]) -> BacklogIndex` — Build a slug → item lookup for ``items``.
  - `effective_blockers(item: BacklogItem, index: BacklogIndex) -> list[str]` — Return ``item``'s ``blocked_by`` slugs whose referent isn't done.
  - `detect_cycle(start: str, new_dep: str, index: BacklogIndex) -> bool` — Check whether adding ``new_dep`` as a blocker of ``start`` would cycle.
  - `prefix_of(slug: str) -> str` — The slug's prefix, preferring the longest known one.
  - `is_worker_safe(prefix: str) -> bool` — Whether a swarm worker may be handed items under this prefix.
  - `resolve_id(arg: str, items: list[BacklogItem], pending_items: list[PendingItem]) -> tuple[str, str]` — Resolve a display number or slug to a ``(kind, slug)`` pair.
  - `require_kind(cmd: str, arg: str, kind: str, expected: str) -> None` — Exit with a helpful message if ``kind`` doesn't match ``expected``.
  - `enforce_rev_guard(cmd: str, id_arg: str, if_rev_arg: int | None, current_rev: int, items: list[BacklogItem], pending_items: list[PendingItem]) -> None` — Refuse a numeric-id mutation that lacks a fresh ``--if-rev``.
  - `render(items: list[BacklogItem] | None = None, pending_items: list[PendingItem] | None = None, *, out: TextIO | None = None, err: TextIO | None = None, rev: int | None = None, dispatch: bool = False) -> None` — Render the full dashboard: pending items, then the five backlog sections.
  - `append_journal_event(entry: dict[str, object], *, verbose: bool = False) -> None` — Append one event to the journal, best-effort.
  - `load_runs(item: str | None = None) -> list[RunRecord]` — Load run-evidence rows from :data:`RUNS_FILE`, optionally for one item.
  - `write_runs_file(runs: Sequence[RunRecord]) -> None` — Atomically rewrite :data:`RUNS_FILE` with ``runs`` (one JSON line each).
  - `append_run_record(record: RunRecord) -> bool` — Append one run-evidence row to :data:`RUNS_FILE` (best-effort).
  - `read_journal_entries(within_hours: float | None = None, *, verbose: bool = False) -> list[dict[str, object]]` — Read journal entries, optionally filtered to the last ``within_hours``.
  - `confirm_resolution(cmd: str, arg: str | int, item: BacklogItem | PendingItem, summary_key: str = 'summary', *, quiet: bool = False) -> None` — Echo what a mutating command resolved to, so misresolution is visible.
  - `build_parser() -> argparse.ArgumentParser` — Build the full argument parser for every subcommand.
- Subcommand handlers: `cmd_internal_regen`, `cmd_recap`, `cmd_render`, `cmd_ready`, `cmd_list`, `cmd_show`, `cmd_add`, `cmd_update`, `cmd_start`, `cmd_done`, `cmd_review`, `cmd_approve`, `cmd_reject`, `cmd_gate_set`, `cmd_gate_pass`, `cmd_run`, `cmd_runs`, `cmd_backfill_gate`, `cmd_rename`, `cmd_block`, `cmd_unblock`, `cmd_out_of_scope_add`, `cmd_out_of_scope_link`, `cmd_out_of_scope_unlink`, `cmd_out_of_scope_remove`, `cmd_out_of_scope_list`, `cmd_out_of_scope_show`, `cmd_pending_add`, `cmd_pending_update`, `cmd_pending_list`, `cmd_remove`, `cmd_prune`
- Tested by: `agent-scripts/test_dev_status.py`, `agent-scripts/test_sweep_dead_claims.py`, `agent-scripts/test_to_tickets_runner.py`

### `agent-scripts/dotfiles_sync_check.py`

SessionStart hook: flag when the dotfiles repo has drifted from the last commit bundled over to a GitHub-blocked work machine.

- Installed at: `~/.claude/scripts/dotfiles_sync_check.py` (all harnesses)
- Entrypoint: not executable, `#!/usr/bin/env python3`
- CLI (`argparse`): Flag when the dotfiles repo has drifted from the last commit bundled over to a GitHub-blocked work machine.
  - `--quiet/-q`
  - `--verbose/-v`
- Subcommands:
  - `check` — print a drift note if HEAD is ahead of the marker (default)
  - `mark [<sha>]` — record the given (or current HEAD) commit as last-bundled
    - `sha` — commit to record (defaults to HEAD) (nargs: ?)
- Filesystem constants:
  - `REPO = Path(__file__).resolve().parents[1]`
  - `STATE_DIR = Path.home() / '.local' / 'state' / 'agent-toolkit'`
  - `MARKER = STATE_DIR / 'last-bundled-commit'`
- Explicit exit codes: `1`
- Depends on: `cli_common.py`
- Public functions:
  - `git(*args: str) -> str | None`
  - `build_parser() -> argparse.ArgumentParser`
- Subcommand handlers: `cmd_check`, `cmd_mark`
- Tested by: `agent-scripts/test_dotfiles_sync_check.py`

### `agent-scripts/gen_interfaces.py`

gen_interfaces.py — regenerate INTERFACES.md mechanically from the sources.

- Installed at: `~/.claude/scripts/gen_interfaces.py` (all harnesses)
- Entrypoint: not executable, `#!/usr/bin/env python3`
- CLI (`argparse`): regenerate INTERFACES.md from the harness sources
  - `--quiet/-q`
  - `--verbose/-v`
  - `--check` — exit 3 (with a report on stderr) if a doc's shown command example no longer matches its script, else exit 1 if the file on disk is stale
  - `--stdout` — print the document, write nothing
  - `--update-fingerprints` — recompute and record every in-scope script's contract fingerprint in contract_fingerprints.json, accepting its current behavior as the new baseline — run this only after re-reading the --check diff, never blind
  - `--repo-root` — repository root (default: inferred from this script's path)
  - `--output`
- Explicit exit codes: `1`, `2`, `3`
- Depends on: `cli_common.py`
- Public classes:
  - `class CliArgument` — One ``add_argument`` call, reduced to what a reader needs.
  - `class Subcommand` — One ``add_parser`` call and the arguments attached to it.
  - `class CliSpec` — The argparse surface of a module.
  - `class ApiSymbol` — A public module-level function or class.
  - `class LinkTarget` — One ``links.toml`` destination for a repo file, plus the gates on it.
  - `class ModuleInterface` — Everything statically known about one Python module.
  - `class HelperSpec` — A module function that adds arguments to a parser passed as its first arg.
  - `class DocInvocation` — One doc's code-span example of running a target script.
  - `class DriftProblem` — One doc invocation using a subcommand/flag the script doesn't have.
  - `class FingerprintProblem` — One contract-fingerprint failure for ``--check`` to report.
- Public functions:
  - `first_paragraph(text: str | None) -> str` — Collapse the first blank-line-delimited paragraph of ``text`` to one line.
  - `first_sentence(text: str | None) -> str | None` — Return the first sentence of ``text``, or ``None`` when it is empty.
  - `attr_path(node: ast.expr) -> str` — Render a dotted attribute/name expression, or ``""`` for anything else.
  - `literal_str(node: ast.expr | None, bindings: dict[str, str]) -> str | None` — Resolve ``node`` to a string, substituting ``bindings`` for bare names.
  - `literal_scalar(node: ast.expr | None) -> str | None` — Render a simple constant (str/int/float/bool/None) for display.
  - `literal_sequence(node: ast.expr | None) -> list[str] | None` — Render a literal list/tuple of constants, or ``None`` if it is dynamic.
  - `keyword_map(call: ast.Call) -> dict[str, ast.expr]` — Return the call's keyword arguments as a name -> node mapping.
  - `linear_statements(body: Sequence[ast.stmt]) -> Iterator[ast.stmt]` — Yield statements in source order, descending into blocks but not defs.
  - `render_signature(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str` — Render ``def`` as a one-line signature with annotations intact.
  - `render_arg(arg: ast.arg, default: ast.expr | None) -> str` — Render a single parameter with its annotation and default.
  - `render_class(node: ast.ClassDef) -> str` — Render a class header, bases included.
  - `is_exception(node: ast.ClassDef) -> bool` — Report whether a class derives from something named like an exception.
  - `parser_subclasses(tree: ast.Module) -> set[str]` — Return module-level class names that subclass ``ArgumentParser``.
  - `is_parser_constructor(node: ast.expr, subclasses: set[str]) -> bool` — Report whether ``node`` constructs an :class:`argparse.ArgumentParser`.
  - `collect_parser_helpers(tree: ast.Module) -> dict[str, HelperSpec]` — Find functions whose first parameter is a parser they add arguments to.
  - `helper_bindings(spec: HelperSpec, call: ast.Call) -> dict[str, str]` — Bind a helper's parameters to the literal values at one call site.
  - `build_argument(call: ast.Call, bindings: dict[str, str]) -> CliArgument | None` — Turn one ``add_argument`` call into a displayable argument record.
  - `extract_cli(tree: ast.Module, module_doc: str) -> CliSpec | None` — Extract the argparse surface, or ``None`` when the module has no CLI.
  - `leaf_subcommand_paths(subcommands: list[Subcommand]) -> set[tuple[str, ...]]` — Return the paths with no other subcommand nested under them.
  - `match_leaf_handlers(tree: ast.Module, cli: CliSpec) -> tuple[dict[tuple[str, ...], str], list[tuple[str, ...]]]` — Match every leaf subcommand to its ``cmd_<path>`` handler's docstring.
  - `fingerprint_lines(cli: CliSpec, leaf_docstrings: dict[tuple[str, ...], str], module_purpose: str, exit_codes: list[int]) -> list[str]` — Render a script's behavior-carrying CLI surface as sorted, diffable lines.
  - `extract_env_vars(tree: ast.Module) -> list[str]` — Collect the environment variable names the module reads.
  - `extract_exit_codes(tree: ast.Module) -> list[int]` — Collect the literal integer codes passed to ``sys.exit``/``SystemExit``.
  - `extract_path_constants(tree: ast.Module) -> list[str]` — Collect module-level uppercase constants that name a filesystem location.
  - `extract_internal_imports(tree: ast.Module, siblings: set[str]) -> list[str]` — Collect imports that resolve to sibling modules in the same directory.
  - `extract_api(tree: ast.Module, cli: CliSpec | None) -> tuple[list[ApiSymbol], list[ApiSymbol], list[ApiSymbol], list[str]]` — Split module-level public definitions into exceptions/classes/functions.
  - `uses_raw_argv(tree: ast.Module) -> bool` — Report whether the module reads ``sys.argv`` directly.
  - `link_gates(entry: dict[str, object]) -> list[str]` — Render one links.toml entry's harness/platform/profile conditions.
  - `load_link_table(repo_root: Path) -> LinkTable` — Map each repo-relative source in links.toml to its symlink destinations.
  - `render_link_targets(targets: list[LinkTarget]) -> str` — Render a source's install destinations as one comma-joined phrase.
  - `read_frontmatter(path: Path) -> dict[str, str]` — Read a markdown file's leading ``---`` frontmatter as flat key/value pairs.
  - `find_tests(module: Path, repo_root: Path) -> list[str]` — Find test modules covering ``module``, by filename and by import.
  - `resolve_script_source(path: Path) -> tuple[Path, str]` — Resolve the source that documents ``path``, following a thin launcher.
  - `analyze_module(path: Path, repo_root: Path, siblings: set[str], links: LinkTable) -> ModuleInterface` — Parse one script and collect every statically visible interface fact.
  - `inline(text: str) -> str` — Flatten text to a single line safe to drop into a markdown bullet.
  - `render_cli(module: ModuleInterface, lines: list[str]) -> None` — Append the CLI section for one module.
  - `render_notes(argument: CliArgument) -> str` — Render an argument's help text and parenthesised extras, if any.
  - `render_module(module: ModuleInterface) -> list[str]` — Render one module's full section.
  - `render_command_matrix(repo_root: Path, links: LinkTable) -> list[str]` — Render the per-harness skill/command parity matrix from frontmatter.
  - `is_generated_artifact(relpath: str) -> bool` — Report whether a path is build output or a dotfile rather than a source.
  - `tracked_files(repo_root: Path) -> set[str] | None` — Return every git-tracked path under ``repo_root``, or None if unavailable.
  - `render_assets(repo_root: Path, links: LinkTable, tracked: set[str] | None = None) -> list[str]` — Render the non-Python, non-skill harness assets and where they install.
  - `discover_doc_paths(repo_root: Path) -> list[Path]` — Return every skill/command doc across the four harnesses, sorted.
  - `code_regions(text: str) -> list[str]` — Return every inline code span and fenced code block's inner text.
  - `tokenize_invocation_line(line: str) -> list[str]` — Shell-tokenize one line, after stripping argparse-usage brackets.
  - `invocation_tokens(tokens: list[str], script_basename: str) -> list[str] | None` — Return the token stream starting at ``script_basename``, or None.
  - `find_invocations(doc_paths: Sequence[Path], repo_root: Path, script_basename: str) -> list[DocInvocation]` — Scan every doc for code-span invocations of ``script_basename``.
  - `flag_takes_value(argument: CliArgument) -> bool` — Report whether ``argument`` consumes a following token as its value.
  - `validate_invocation(cli: CliSpec, tokens: list[str]) -> list[str]` — Walk one invocation's tokens against ``cli``; return problem strings.
  - `check_doc_drift(repo_root: Path, modules: Sequence[ModuleInterface]) -> tuple[list[DriftProblem], dict[str, dict[str, bool]]]` — Validate every doc's shown invocations against each module's ``CliSpec``.
  - `render_doc_drift_section(coverage: dict[str, dict[str, bool]]) -> list[str]` — Render the per-script per-doc coverage summary, sorted for determinism.
  - `load_contract_fingerprints(repo_root: Path) -> dict[str, list[str]]` — Read ``contract_fingerprints.json``, treating anything unreadable as empty.
  - `load_repo_modules(repo_root: Path, links: LinkTable) -> list[ModuleInterface]` — Parse every shared script under ``SCRIPTS_DIR`` into a ``ModuleInterface``.
  - `check_contract_fingerprints(repo_root: Path, modules: Sequence[ModuleInterface], coverage: dict[str, dict[str, bool]]) -> list[FingerprintProblem]` — Check every in-scope script's live fingerprint against the recorded one.
  - `write_contract_fingerprints(repo_root: Path) -> dict[str, list[str]]` — Recompute and write ``contract_fingerprints.json`` for ``--update-fingerprints``.
  - `extract_skill_mentions(source: Path, names: Sequence[str]) -> list[str]` — Return which other skill names this skill's own file text mentions.
  - `render_skill_graph_section(repo_root: Path) -> list[str]` — Render which skill mentions which other skills, by name, in its own text.
  - `build_document(repo_root: Path) -> str` — Build the complete INTERFACES.md text for ``repo_root``.
  - `build_document_and_drift(repo_root: Path) -> tuple[str, list[DriftProblem], list[FingerprintProblem]]` — Build INTERFACES.md text alongside the doc-drift and fingerprint problems.
  - `anchor(relpath: str) -> str` — Return the GitHub heading anchor for a module section.
  - `default_repo_root() -> Path` — Return the repo root inferred from this script's real location.
- Subcommand handlers: `cmd_function_name`
- Tested by: `agent-scripts/test_gen_interfaces.py`, `test/test_dev_status_tool_action_coverage.py`

### `agent-scripts/gen_second_opinion.py`

gen_second_opinion.py — regenerate the second-opinion skill copies (one per harness, named in HARNESS_TABLE) from one canonical template.

- Installed at: `~/.claude/scripts/gen_second_opinion.py` (all harnesses)
- Entrypoint: not executable, `#!/usr/bin/env python3`
- CLI (`argparse`): regenerate the second-opinion skill copies from one template
  - `--quiet/-q`
  - `--verbose/-v`
  - `--check` — exit 1 (with diffs on stderr) if any copy, the contract shape, or a guard phrase is stale
  - `--stdout` — print the rendered copies, write nothing
  - `--repo-root` — repository root (default: inferred from this script's path)
- Explicit exit codes: `1`, `2`
- Depends on: `cli_common.py`
- Public classes:
  - `class HarnessParams` — One harness's frontmatter block plus its body placeholder values.
- Public functions:
  - `substitutions(params: HarnessParams) -> dict[str, str]` — Map each `{{TOKEN}}` in the template to this harness's value.
  - `apply_placeholders(text: str, values: dict[str, str]) -> str` — Replace every `{{TOKEN}}` in ``text`` with its harness-specific value.
  - `render_body(template_text: str, params: HarnessParams) -> str` — Render one harness's body: substitute placeholders, then reflow prose.
  - `render_file(template_text: str, relpath: str, params: HarnessParams) -> str` — Render one harness's complete file: frontmatter, marker, body.
  - `render_all(repo_root: Path) -> dict[str, str]` — Render every harness's file, keyed by its repo-relative output path.
  - `check_contract_shape(repo_root: Path, template_text: str) -> list[str]` — Return problems where a CONTRACT_TOKENS entry is missing from the template.
  - `check_row_comments(repo_root: Path) -> list[str]` — Return problems where a HARNESS_TABLE keyword argument has no comment on the line immediately above it.
  - `default_repo_root() -> Path` — Return the repo root inferred from this script's real location.
- Tested by: `agent-scripts/test_gen_second_opinion.py`

### `agent-scripts/gen_shell_completion.py`

Generate a zsh `#compdef` completion file for a harness CLI.

- Installed at: `~/.claude/scripts/gen_shell_completion.py` (all harnesses)
- Entrypoint: not executable, `#!/usr/bin/env python3`
- CLI (`argparse`): Generate a zsh `#compdef` completion file for a harness CLI.
  - `--quiet/-q`
  - `--verbose/-v`
  - `--harness` — harness to generate a completion for, or 'all' (choices computed at runtime; required)
  - `--out` — output path (only valid for a single harness)
  - `--stdout` — print to stdout instead of writing
- Filesystem constants:
  - `DEFAULT_OUT_DIR = Path.home() / '.zsh/completions'`
- Depends on: `cli_common.py`
- Public classes:
  - `class HarnessSpec`
  - `class Option`
  - `class Node`
- Public functions:
  - `run_help(cli: str, path: list[str], *, verbose: bool = False) -> str`
  - `run_goflag_subcommand_help(cli: str, name: str, *, verbose: bool = False) -> str`
  - `collect_sections(text: str) -> dict[str, list[str]]`
  - `parse_options(lines: list[str]) -> list[Option]`
  - `parse_commands(lines: list[str], *, strip_cli: str | None = None) -> list[tuple[str, list[str], str]]` — Return list of (primary_name, aliases, description).
  - `is_dir_option(opt: Option) -> bool`
  - `is_file_option(opt: Option) -> bool`
  - `help_matches_path(cli: str, text: str, path: tuple[str, ...]) -> bool` — Check that the help output's `Usage:` line reflects the path we asked for.
  - `build_tree(cli: str, path: tuple[str, ...], seen: set[tuple[str, ...]], *, verbose: bool = False) -> Node`
  - `collect_goflag_sections(text: str, *, is_root: bool) -> dict[str, list[str]]` — Split go-flag `--help`/`help <name>` output into Flags/Commands blocks.
  - `build_tree_goflag(cli: str, *, verbose: bool = False) -> Node` — Build a 2-level-deep tree: root flags/subcommands, one level of
  - `run_native_passthrough(spec: HarnessSpec, *, verbose: bool = False) -> str | None`
  - `option_label(opt: Option) -> str`
  - `esc_desc(s: str) -> str`
  - `format_option(opt: Option) -> str`
  - `sanitize(path: tuple[str, ...]) -> str`
  - `needs_function(node: Node) -> bool`
  - `emit_zsh(root: Node, cli: str) -> str`
  - `generate(spec: HarnessSpec, *, verbose: bool = False) -> str | None`
- Tested by: `agent-scripts/test_gen_shell_completion.py`

### `agent-scripts/gen_skills.py`

gen_skills.py — regenerate the dashboard/recap/grill-me/backlog-item/ make-skill/spec/standup/to-tickets/swarm skill copies from one template per skill, plus a shared per-harness capability table. dashboard/recap/ grill-me/backlog-item/make-skill cover all 5 harnesses (claude, copilot, opencode, agy, pi); spec/standup/to-tickets cover only claude/opencode/pi; swarm covers only claude/copilot (user-directed; pi already owns the orchestration surface) — see `SKILL_HARNESSES` below and AGENTS.md's "Harness maintenance tiers" section for why copilot/agy stop getting new generated skills.

- Installed at: `~/.claude/scripts/gen_skills.py` (all harnesses)
- Entrypoint: not executable, `#!/usr/bin/env python3`
- CLI (`argparse`): regenerate the dashboard/grill-me/backlog-item/make-skill copies from one template per skill
  - `--quiet/-q`
  - `--verbose/-v`
  - `--check` — exit 1 (with diffs on stderr) if any copy is stale
  - `--stdout` — print the rendered copies, write nothing
  - `--repo-root` — repository root (default: inferred from this script's path)
- Explicit exit codes: `1`, `2`
- Depends on: `cli_common.py`, `gen_skills_params.py`
- Public functions:
  - `do_not_edit_marker(skill: str) -> str` — Return this skill's marker, naming its own template file by name.
  - `capability_tokens(harness: str) -> dict[str, str]` — Map the shared capability facts to the `{{TOKEN}}` names templates use.
  - `apply_placeholders(text: str, values: dict[str, str]) -> str` — Replace every `{{TOKEN}}` in ``text`` with its harness-specific value.
  - `render_body(template_text: str, values: dict[str, str]) -> str` — Render one harness's body: substitute placeholders, no reflow.
  - `render_one(skill: str, harness: str, template_text: str, params: dict) -> str` — Render one (skill, harness) pair's complete file.
  - `render_all(repo_root: Path, skill_params: dict[str, dict[str, dict]]) -> dict[str, str]` — Render every (skill, harness) pair, keyed by its repo-relative output path.
  - `default_repo_root() -> Path` — Return the repo root inferred from this script's real location.
- Tested by: `agent-scripts/test_gen_skills.py`

### `agent-scripts/gen_skills_params.py`

gen_skills_params.py — per-(skill, harness) content tables for gen_skills.py.

- Installed at: `~/.claude/scripts/gen_skills_params.py` (all harnesses)
- Entrypoint: not executable, no shebang
- CLI: none (library module).
- Depends on: `repo_identity.py`
- Public functions:
  - `edit_root(relpath: str, dotfiles_relpath: str | None = None) -> str` — Return self-contained "where to edit this file" markdown.
  - `symlink_cmd(relpath: str, dest: str) -> str` — Return a self-contained, copy-pasteable `ln -s` command.
  - `probe_add_dir() -> str` — Return the bare --add-dir flag argument for claude's headless probe.
- Tested by: `agent-scripts/test_gen_skills.py`

### `agent-scripts/grill.py`

grill.py — grill-me session state CLI. All session mutations go through here.

- Installed at: `~/.claude/scripts/grill.py` (all harnesses)
- Entrypoint: not executable, `#!/usr/bin/env python3`
- CLI (`argparse`): grill-me session state CLI (all mutations go through here)
  - `--quiet/-q`
  - `--verbose/-v`
- Subcommands:
  - `new '{"topic": "..."}'` — create a session
  - `ask '{"id", "question", ["reasoning"], ["depends_on"]}' [--session <SESSION>]` — register an open decision point
    - `--session/-s` — session slug or unique substring (default: most recent)
  - `decide '{"id", "decision", ["question"], ["reasoning"], ["source"], ["depends_on"]}' [--session <SESSION>]` — resolve an open decision point (or add+decide in one shot)
    - `--session/-s` — session slug or unique substring (default: most recent)
  - `revise <decision_id> '{"decision": "...", ["depends_on"]}' [--session <SESSION>]` — amend a decision (resets its verdict)
    - `--session/-s` — session slug or unique substring (default: most recent)
  - `rm <decision_id> [--force] [--session <SESSION>]` — remove a decision point from a session
    - `--force` — bypass the referential-integrity check (dangling depends_on allowed)
    - `--session/-s` — session slug or unique substring (default: most recent)
  - `verdict <decision_id> '{"result": "VERIFIED|DISPUTED|UNVERIFIABLE", "evidence": "..."}' [--session <SESSION>]` — record a verification verdict
    - `--session/-s` — session slug or unique substring (default: most recent)
  - `plan <path> [--session <SESSION>]` — record the path of the model-authored plan artifact
    - `--session/-s` — session slug or unique substring (default: most recent)
  - `mark-pending-execution [--backlog-slug <BACKLOG_SLUG>] [--session <SESSION>]` — flag a session's plan as ready for clear-and-go resume
    - `--backlog-slug` — dev_status.py item this plan belongs to, if any
    - `--session/-s` — session slug or unique substring (default: most recent)
  - `pending-plan [--consume]` — print (and optionally consume) the most recent pending-execution plan
    - `--consume` — clear pending_execution on the printed session (one-shot)
  - `next [--session <SESSION>]` — print the first frontier-ready open decision point
    - `--session/-s` — session slug or unique substring (default: most recent)
  - `frontier [--session <SESSION>]` — print the batch of currently-askable (dependency-resolved) open decisions
    - `--session/-s` — session slug or unique substring (default: most recent)
  - `render [--session <SESSION>]` — print session status as markdown
    - `--session/-s` — session slug or unique substring (default: most recent)
  - `list` — list sessions
  - `show [<decision_id>] [--session <SESSION>]` — print session (or one decision) as JSON
    - `decision_id` (nargs: ?)
    - `--session/-s` — session slug or unique substring (default: most recent)
- Filesystem constants:
  - `DATA_DIR = Path.home() / '.claude' / 'data' / 'grill'`
- Explicit exit codes: `1`
- Depends on: `cli_common.py`
- Public classes:
  - `class Verdict(TypedDict)` — A recorded verification result for one decision.
  - `class Decision(TypedDict)` — One decision point within a grill session.
  - `class Session(TypedDict)` — A grill session as stored at ``DATA_DIR/<slug>.json``.
- Public functions:
  - `today() -> str` — Return today's date as an ISO-8601 string (``YYYY-MM-DD``).
  - `now() -> str` — Return the current local time as a full ISO-8601 timestamp.
  - `die(context: str, msg: str) -> NoReturn` — Print an error to stderr and exit the process with status 1.
  - `slugify(text: str) -> str` — Lowercase ``text`` and collapse runs of non-alphanumerics to single hyphens.
  - `validate_decision_id(decision_id: str, context: str) -> None` — Validate a decision id's format and length.
  - `parse_json_arg(raw: str, context: str) -> dict[str, object]` — Parse a CLI argument as a JSON object.
  - `session_path(slug: str) -> Path` — Return the on-disk path for the session identified by ``slug``.
  - `load_session(slug: str) -> Session` — Load one session by slug.
  - `ensure_data_dir() -> None` — Create ``DATA_DIR`` if it is missing.
  - `save_session(session: Session) -> None` — Atomically persist ``session`` to its slug-derived path.
  - `all_session_slugs() -> list[str]` — Return every session slug on disk, sorted, or ``[]`` if none exist.
  - `session_lock(slug: str) -> Iterator[None]` — Hold an exclusive lock over one session's read-modify-write cycle.
  - `resolve_session(arg: str | None, context: str) -> Session` — Resolve ``--session`` (see :func:`_resolve_slug`) and load it.
  - `find_decision(session: Session, decision_id: str, context: str) -> Decision` — Find a decision by id within a session.
  - `is_open(decision: Decision) -> bool` — Return whether ``decision`` has not yet been decided.
  - `confirm(context: str, session: Session, detail: str, verbose: bool = False) -> None` — Echo a mutating command's outcome to stderr.
  - `touch(session: Session) -> None` — Stamp ``session['updated']`` with the current timestamp, in place.
  - `frontier(session: Session) -> DecisionList` — Return every open decision whose dependencies are all resolved.
  - `render_markdown(session: Session) -> str` — Render a session's status as a Markdown document.
- Subcommand handlers: `cmd_new`, `cmd_ask`, `cmd_decide`, `cmd_revise`, `cmd_rm`, `cmd_verdict`, `cmd_plan`, `cmd_mark_pending_execution`, `cmd_pending_plan`, `cmd_next`, `cmd_frontier`, `cmd_render`, `cmd_list`, `cmd_show`
- Tested by: `agent-scripts/test_grill.py`, `agent-scripts/test_second_opinion.py`, `agent-scripts/test_to_tickets_runner.py`

### `agent-scripts/guard_rails.py`

Pre-tool guard shared by every harness: refuse a write into a repository's main checkout while a backlog item for that repository is in progress, warn when the current worktree's base has fallen behind ``origin/main``, (Bash, Claude Code only) deny the git-native ways to defeat the no-commit-on-main git hook (``githooks/pre-commit`` / ``githooks-global/pre-commit``), and require an active backlog-item claim before a write that points at an in-progress item.

- Installed at: `~/.claude/scripts/guard_rails.py` (all harnesses)
- Entrypoint: not executable, `#!/usr/bin/env python3`
- CLI (`argparse`): pre-tool guard shared by every harness
  - `--harness` — read this harness's payload on stdin and answer in its shape (choices: claude, agy, copilot)
  - `--tool` — tool name, for the neutral form
  - `--cwd` — session working directory, for the neutral form
  - `--path` — target file path, for the neutral write-family form
  - `--command` — shell command, for the neutral bash-family form
  - `--quiet/-q`
  - `--verbose/-v`
- Environment: `GUARD_RAILS_OFF`, `GUARD_RAILS_STORE`
- Filesystem constants:
  - `DEFAULT_BACKLOG_ITEMS = Path.home() / '.claude' / 'data' / 'backlog' / 'items.json'`
- Depends on: `cli_common.py`, `dev_status_impl.py`
- Public classes:
  - `class Request` — A normalized tool call: what family, from where, against which path (write-family) or command (bash-family).
  - `class Verdict`
  - `class RepoInfo`
- Public functions:
  - `tool_family(name: object) -> str` — Collapse a harness's tool name to a family.
  - `git(*args: str, cwd: str | None = None) -> str | None` — Run git, returning stripped stdout, or None on any failure.
  - `common_dir_of(directory: str) -> str | None` — Canonical git common directory for a path, or None if it is not in a repo.
  - `repo_info(directory: str) -> RepoInfo | None` — Classify a directory: which repo, worktree or main checkout, bare or not, and on which branch.
  - `backlog_items_path() -> Path` — Where the backlog store lives.
  - `load_in_progress() -> list[dict] | None` — In-progress backlog items, or None when the store cannot be read.
  - `evaluate_bash_override(command: str, cwd: str) -> Verdict` — Deny the git-native ways to defeat the no-commit-on-main git hook, on a protected branch only -- see the module docstring.
  - `evaluate(req: Request) -> Verdict` — Apply R2 then R3 to write-family calls, and R4's claim check to any checkout they land in, plus the bash-family override check to Bash calls.
  - `parse_payload(harness: str, payload: object) -> Request | None` — Normalize a harness's native hook payload.
  - `render(harness: str | None, verdict: Verdict) -> tuple[str, int]` — Shape a verdict into the harness's own reply.
  - `build_parser() -> argparse.ArgumentParser`
- Tested by: `agent-scripts/test_guard_rails.py`, `test/test_guard_rails_claim.py`, `test/test_guard_rails_topology.py`

### `agent-scripts/harness_discovery_check.py`

SessionStart hook + CLI: detect when a harness's instruction-file discovery behavior may have drifted from the version-pinned facts in README.md.

- Installed at: `~/.claude/scripts/harness_discovery_check.py` (all harnesses)
- Entrypoint: not executable, `#!/usr/bin/env python3`
- CLI (`argparse`): Detect harness instruction-file discovery drift against README.md's version-pinned facts.
  - `--quiet/-q`
  - `--verbose/-v`
- Subcommands:
  - `check [--hook] [--strict]` — stateless version-pin comparison for load-bearing harnesses (default)
    - `--hook` — format output for SessionStart hook consumption
    - `--strict` — exit 2 when a version mismatch is noted (default: exit 0)
  - `probe [--harness {claude,opencode,pi,copilot,agy}]` — on-demand live semantic verification (~10-15 API calls)
    - `--harness` — probe a single harness instead of all five (choices: claude, opencode, pi, copilot, agy)
- Environment: `OPENCODE_PROBE_MODEL`, `XDG_CACHE_HOME`
- Depends on: `cli_common.py`
- Exceptions:
  - `class HarnessCheckError(Exception)` — Raised when a harness check can't proceed (subprocess failure, not a missing binary).
- Public functions:
  - `resolve_binary(name: str) -> Path | None` — Resolve a harness binary via ``shutil.which`` then ``Path.resolve()``.
  - `run_version(name: str, binary: Path, run_command: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> str` — Run ``<binary> --version`` and return the extracted version string.
  - `build_parser() -> argparse.ArgumentParser`
- Subcommand handlers: `cmd_check`, `cmd_probe`
- Tested by: `agent-scripts/test_harness_discovery_check.py`

### `agent-scripts/herdr_delegate.py`

Launch pi agents in herdr tabs to work backlog items.

- Installed at: `~/.claude/scripts/herdr_delegate.py` (all harnesses)
- Entrypoint: not executable, `#!/usr/bin/env python3`
- CLI (`argparse`): Launch pi agents in herdr tabs to work backlog items.
- Subcommands:
  - `plan` — READY queue grouped by prefix, as JSON
  - `launch [--slug <SLUG>] [--swarm <SWARM>] [--prefix <PREFIX>] [--model <MODEL>] [--cwd <CWD>]` — start a pi worker or orchestrator
    - `--slug` — single item for one unattended worker
    - `--swarm` — fan out across N workers
    - `--prefix` — queue scope, required with --swarm
    - `--model` — model passed through to pi after a bare --
    - `--cwd` — working directory
  - `restart [--swarm <SWARM>] [--prefix <PREFIX>] [--run-id <RUN_ID>] [--model <MODEL>] [--cwd <CWD>]` — close a live swarm orchestrator's tab, relaunch it, resume the same run
    - `--swarm` — fan out across N workers
    - `--prefix` — queue scope, required with --swarm
    - `--run-id` — runId to resume; discovered from persisted state when omitted
    - `--model` — model passed through to pi after a bare --
    - `--cwd` — working directory
- Environment: `PI_SWARM_STATE_DIR`
- Filesystem constants:
  - `DEV_STATUS = Path(__file__).parent / 'dev_status.py'`
- Explicit exit codes: `1`
- Depends on: `dev_status.py`
- Exceptions:
  - `class RefusedError(RuntimeError)` — A launch that must not proceed, with a reason fit to show the user.
- Public functions:
  - `require_herdr_env(env: dict[str, str] | os._Environ[str]) -> None` — Refuse unless this process is inside a herdr-managed pane.
  - `check_launchable(*, slug: str | None = None, prefix: str | None = None) -> None` — Refuse a launch that targets the harness's own repo.
  - `group_by_prefix(slugs: list[str]) -> list[dict[str, object]]` — Group slugs by prefix, worker-safe prefixes first, then largest first.
  - `build_tab_argv(*, cwd: str, label: str) -> list[str]` — `herdr tab create` argv.
  - `agent_name_for(label: str) -> str` — The herdr agent name derived from a tab label.
  - `build_tab_list_argv() -> list[str]` — `herdr tab list` argv.
  - `build_agent_list_argv() -> list[str]` — `herdr agent list` argv.
  - `build_agent_start_argv(*, name: str, pane: str, model: str | None) -> list[str]` — `herdr agent start` argv, with any model passed through after a bare ``--``.
  - `worker_prompt(slug: str) -> str` — One worker, one item, unattended.
  - `orchestrator_prompt(concurrency: int) -> str` — One orchestrator; `swarm_spawn` owns the fan-out from here.
  - `orchestrator_resume_prompt(concurrency: int, run_id: str, prefix: str) -> str` — One orchestrator, resuming an interrupted run.
  - `validate_run_id(run_id: str) -> str` — Refuse a runId the delegate cannot safely pass through.
  - `swarm_state_dir() -> Path` — Where swarm-tool.ts persists per-runId state (same override, same default).
  - `state_matches_prefix(state: object, prefix: str) -> bool` — Whether one parsed state file belongs to a run scoped to ``prefix``.
  - `discover_run_id(prefix: str) -> str` — The runId of the newest state file belonging to ``prefix``.
  - `resolve_resume_run_id(prefix: str, run_id: str | None) -> str` — The runId a restart will resume.
  - `parse_tab_list(listing: dict[str, object]) -> list[dict[str, object]]` — Tabs out of a `herdr tab list` envelope; [] on anything unexpected.
  - `live_tab_ids_with_label(label: str) -> list[str]` — Ids of every live tab carrying exactly ``label``.
  - `parse_agent_names(listing: dict[str, object]) -> list[str]` — Agent names out of a `herdr agent list` envelope; [] on anything unexpected.
  - `wait_agent_deregistered(name: str) -> None` — Poll until no live agent carries ``name``, bounded; refuse if it persists.
  - `spawn_in_new_tab(*, cwd: str, label: str, prompt: str, model: str | None) -> dict[str, object]` — Create a tab, start pi in it, and hand it its prompt.
  - `ready_slugs() -> list[str]` — Slugs currently in READY, straight from ``dev_status.py ready``.
  - `herdr(argv: list[str]) -> dict[str, object]` — Run a herdr command and return its parsed JSON result.
- Subcommand handlers: `cmd_plan`, `cmd_launch`, `cmd_restart`
- Tested by: `agent-scripts/test_herdr_delegate.py`

### `agent-scripts/link_drift_check.py`

SessionStart hook + CLI: flag when a managed symlink on this machine no longer points where links.toml says it should.

- Installed at: `~/.claude/scripts/link_drift_check.py` (all harnesses)
- Entrypoint: not executable, `#!/usr/bin/env python3`
- CLI (`argparse`): Flag managed symlinks that no longer point where links.toml says they should.
  - `--quiet/-q`
  - `--verbose/-v`
- Subcommands:
  - `check` — print a line per drifted bucket (default)
- Environment: `XDG_CACHE_HOME`
- Filesystem constants:
  - `REPO = Path(__file__).resolve().parents[1]`
- Depends on: `cli_common.py`
- Public functions:
  - `build_parser() -> argparse.ArgumentParser`
- Subcommand handlers: `cmd_check`
- Tested by: `agent-scripts/test_link_drift_check.py`

### `agent-scripts/llm_backends.py`

llm_backends.py — shared subprocess plumbing for CLI-agent backends (agy, opencode, pi, copilot). Extracted from second_opinion.py so dev_status.py's recap generation can reuse the same process-lifecycle handling (timeouts, process-group kills, opencode JSON-event parsing) with its own timeout and model choices, without duplicating it.

- Installed at: `~/.claude/scripts/llm_backends.py` (all harnesses)
- Entrypoint: not executable, `#!/usr/bin/env python3`
- CLI: none (library module).
- Depends on: `cli_common.py`
- Exceptions:
  - `class IsolationError(RuntimeError)` — A backend cannot be invoked because it does not meet the contract.
  - `class BackendError(Exception)` — A backend was invoked but failed (timeout or nonzero exit).
  - `class BackendTimeoutError(BackendError)` — A backend call failed because every attempt (initial + retries) timed out -- the specific silent-stall failure mode instrumentation exists to measure, distinct from a normal nonzero-exit or empty-output failure.
  - `class BackendPayloadSizeError(BackendError)` — A backend call was rejected before invocation because the payload exceeds the maximum size known to work reliably for that backend.
  - `class BackendModelPolicyError(BackendError)` — A backend rejected a model because the ACCOUNT cannot select models through the model flag -- an entitlement failure, not a bad model id.
- Public functions:
  - `containment_available() -> bool` — Whether OS containment can actually be established on this host.
  - `daemon_listening(backend: str) -> bool` — Whether a daemon belonging to ``backend`` currently holds a listening socket.
  - `build_isolated_command(backend: str, prompt: str, *, model: str | None) -> list[str]` — Build the only command any caller may run for ``backend``.
  - `eligibility_report() -> dict[str, dict[str, object]]` — Per-backend presence and contract eligibility, with a reason when not.
  - `check_prompt_size(prompt: str, max_bytes: int = GLOBAL_MAX_PROMPT_BYTES) -> None` — Raise :class:`BackendPayloadSizeError` if ``prompt`` exceeds ``max_bytes``.
  - `available_backends() -> list[str]` — Return the backends in :data:`BACKEND_PRIORITY` that are on ``PATH``.
  - `eligible_backends() -> list[str]` — Backends that are installed AND meet the isolation contract, in priority order.
  - `resolve_backend() -> str | None` — Return the highest-priority eligible backend, or ``None`` if none is.
  - `run_with_fallback(runner: 'Callable[[str], str]', *, backends: list[str] | None = None) -> tuple[str, str]` — Try each eligible backend in turn; return ``(backend, output)``.
  - `run_backend_command(cmd: list[str], timeout: float) -> str` — Run a backend CLI command and return its critique/prose text.
  - `run_agy(prompt: str, *, model: str, timeout: float) -> str` — Run the ``agy`` backend with the given model and return its text output.
  - `run_copilot(prompt: str, *, model: str | None, timeout: float) -> str` — Run the ``copilot`` backend and return its text output.
  - `run_pi(prompt: str, *, model: str | None, timeout: float) -> str` — Run Pi's headless mode and return its text output.
  - `run_opencode(prompt: str, *, model: str | None, timeout: float) -> str` — Run opencode's default agent (no ``--agent`` override) and return its text output.
- Tested by: `agent-scripts/test_dev_status.py`, `agent-scripts/test_gen_interfaces.py`, `agent-scripts/test_llm_backends.py`, `agent-scripts/test_second_opinion.py`, `agent-scripts/test_timing.py`, `test/test_backend_isolation.py`, `test/test_backend_isolation_live.py`

### `agent-scripts/notify.py`

Cross-platform agent notification dispatcher.

- Installed at: `~/.claude/scripts/notify.py` (all harnesses)
- Entrypoint: executable, `#!/usr/bin/env python3`
- CLI (`argparse`): Cross-platform agent notification dispatcher for WSL, macOS, and Linux.
  - `--quiet/-q`
  - `--verbose/-v`
  - `positional_message` — notification body text (nargs: ?)
  - `--title/-t` — notification title (default: 'Agent Notification') (default: Agent Notification)
  - `--message/-m` — notification body text (overrides positional message)
  - `--harness/-H` — originating harness name (e.g. Claude, Pi, AGY, OpenCode, Copilot)
  - `--icon/-i` — custom icon path or name
  - `--urgency/-u` — urgency level (default: normal) (choices: low, normal, critical; default: normal)
  - `--type` — notification event type (default: completed) (choices: completed, waiting_for_input, error; default: completed)
- Environment: `TMUX`, `WSL_DISTRO_NAME`, `WSL_INTEROP`
- Filesystem constants:
  - `ICONS_DIR = Path(__file__).resolve().parent.parent / 'claude' / 'icons'`
- Depends on: `cli_common.py`
- Public functions:
  - `is_wsl() -> bool` — Detect whether running inside Windows Subsystem for Linux.
  - `get_harness_icon(harness: str | None, custom_icon: str | None = None) -> Path | None` — Resolve the icon file path for a given harness.
  - `sync_icons_to_windows() -> tuple[Path, str] | None` — Ensure all harness icons are synced to Windows AppData for native WinRT access.
  - `send_wsl_toast(title: str, message: str, harness: str | None = None, icon_path: Path | None = None, verbose: bool = False) -> bool` — Send a native Windows Toast notification from WSL via powershell.exe with branding.
  - `send_macos_notification(title: str, message: str, verbose: bool = False) -> bool` — Send a macOS desktop notification via osascript.
  - `send_linux_notification(title: str, message: str, icon_path: Path | None = None, urgency: str = 'normal', verbose: bool = False) -> bool` — Send a desktop notification on native Linux via notify-send.
  - `send_terminal_osc(title: str, message: str, verbose: bool = False) -> bool` — Emit OSC 777 and OSC 9 escape sequences to the controlling TTY.
  - `dispatch_notification(title: str, message: str, harness: str | None = None, icon: str | None = None, urgency: str = 'normal', event_type: str = 'completed', verbose: bool = False) -> None` — Route notification to terminal OSC and appropriate OS bridge with icon.
  - `build_parser() -> argparse.ArgumentParser`
- Tested by: `agent-scripts/test_notify.py`

### `agent-scripts/outlook_calendar.py`

outlook_calendar.py — CLI tool and agent interface for Windows Outlook Calendar via PowerShell COM.

- Installed at: `~/.claude/scripts/outlook_calendar.py` (all harnesses)
- Entrypoint: executable, `#!/usr/bin/env python3`
- CLI (`argparse`): Outlook Calendar tool via PowerShell COM automation.
- Subcommands:
  - `list [--since <SINCE>] [--until <UNTIL>] [--days <DAYS>] [--limit <LIMIT>] [--json]` — List calendar events
    - `--since` — Start date (YYYY-MM-DD)
    - `--until` — End date (YYYY-MM-DD)
    - `--days` — Number of days from since/today
    - `--limit` — Max results (default: 50)
    - `--json` — Output JSON
  - `get --id <ID> [--json]` — Get appointment details by EntryID
    - `--id` — Outlook EntryID (required)
    - `--json` — Output JSON
- Exceptions:
  - `class OutlookCalendarError(Exception)` — Raised when Outlook Calendar COM automation or PowerShell execution fails.
- Public functions:
  - `find_powershell() -> str` — Locate powershell.exe or pwsh.exe on the system.
  - `run_powershell_script(script: str, timeout: float = 20.0, runner: Callable[[str], str] | None = None) -> str` — Execute a PowerShell script block and return its raw stdout string.
  - `run_powershell_json(script: str, timeout: float = 20.0, runner: Callable[[str], str] | None = None) -> dict[str, object]` — Execute a PowerShell script and parse the returned JSON payload.
  - `get_calendar_events_range(start_date: date | None = None, end_date: date | None = None, limit: int = 50, runner: Callable[[str], str] | None = None) -> list[dict[str, object]]` — Query Outlook calendar appointments within a bounded date range.
  - `get_appointment(entry_id: str, runner: Callable[[str], str] | None = None) -> dict[str, object]` — Retrieve appointment details by EntryID.
- Tested by: `agent-scripts/test_outlook_calendar.py`

### `agent-scripts/outlook_email.py`

outlook_email.py — CLI tool and agent interface for Windows Outlook via PowerShell COM.

- Installed at: `~/.claude/scripts/outlook_email.py` (all harnesses)
- Entrypoint: executable, `#!/usr/bin/env python3`
- CLI (`argparse`): Outlook email tool via PowerShell COM automation.
- Subcommands:
  - `search [--query <QUERY>] [--sender <SENDER>] [--since <SINCE>] [--limit <LIMIT>] [--json]` — Search emails
    - `--query/-q` — Search text in subject or body
    - `--sender/-s` — Sender name or email
    - `--since` — Filter since date (YYYY-MM-DD)
    - `--limit` — Max results (default: 20)
    - `--json` — Output JSON
  - `draft --to <TO> [--cc <CC>] --subject <SUBJECT> --body <BODY> [--no-display] [--json]` — Create an email draft
    - `--to` — Recipient email (required)
    - `--cc` — CC recipient email
    - `--subject` — Email subject (required)
    - `--body` — Email body text (required)
    - `--no-display` — Do not pop modal window
    - `--json` — Output JSON
  - `get --id <ID> [--json]` — Get email details by EntryID
    - `--id` — Outlook EntryID (required)
    - `--json` — Output JSON
  - `recent [--since <SINCE>] [--limit <LIMIT>] [--json]` — Fetch recent correspondence
    - `--since` — Filter since date (YYYY-MM-DD)
    - `--limit` — Max results (default: 30)
    - `--json` — Output JSON
- Exceptions:
  - `class OutlookError(Exception)` — Raised when Outlook COM automation or PowerShell execution fails.
- Public functions:
  - `find_powershell() -> str` — Locate powershell.exe or pwsh.exe on the system.
  - `run_powershell_script(script: str, timeout: float = 20.0, runner: Callable[[str], str] | None = None) -> str` — Execute a PowerShell script block and return its raw stdout string.
  - `run_powershell_json(script: str, timeout: float = 20.0, runner: Callable[[str], str] | None = None) -> dict[str, object]` — Execute a PowerShell script and parse the returned JSON payload.
  - `search_emails(query: str | None = None, sender: str | None = None, since: date | None = None, folder: int = 6, limit: int = 20, runner: Callable[[str], str] | None = None) -> list[dict[str, object]]` — Search Outlook emails in the specified folder matching criteria.
  - `create_draft(to: str, subject: str, body: str, cc: str | None = None, display: bool = True, runner: Callable[[str], str] | None = None) -> dict[str, object]` — Create a draft email in Outlook and optionally display inspector modal.
  - `get_email(entry_id: str, runner: Callable[[str], str] | None = None) -> dict[str, object]` — Retrieve detailed email content by EntryID.
  - `get_recent_correspondence(since: date | None = None, limit: int = 50, runner: Callable[[str], str] | None = None) -> list[dict[str, object]]` — Retrieve recent emails received in Inbox.
- Tested by: `agent-scripts/test_outlook_email.py`

### `agent-scripts/refresh_guidance.py`

refresh_guidance.py — audit-by-inspection for hand-authored, agent-facing docs.

- Installed at: `~/.claude/scripts/refresh_guidance.py` (all harnesses)
- Entrypoint: not executable, `#!/usr/bin/env python3`
- CLI (`argparse`): Audit hand-authored, agent-facing docs for mechanically-checkable stale references and per-section human-review staleness.
  - `--quiet/-q`
  - `--verbose/-v`
- Subcommands:
  - `check [--repo-root <REPO_ROOT>] [--doc-set <DOC_SET>] [--agent-toolkit-root <AGENT_TOOLKIT_ROOT>]` — scan the configured doc-set and print a findings + staleness report (default)
    - `--repo-root` — repo root to scan (default: this checkout)
    - `--doc-set` — which built-in doc-set config to use. No default -- either pass this, or let <repo-root>/refresh-guidance.toml be auto-discovered (never both). (choices computed at runtime)
    - `--agent-toolkit-root` — agent-toolkit checkout used to resolve cross-repo script citations for doc-sets with cross_repo_scripts set (default: $AGENT_TOOLKIT_PATH or ~/Workspace/agent-toolkit)
  - `mark-reviewed [--repo-root <REPO_ROOT>] [--doc-set <DOC_SET>] [--agent-toolkit-root <AGENT_TOOLKIT_ROOT>] <doc> <heading> [--commit <COMMIT>] [--date <DATE>]` — record human sign-off that one doc's `## <heading>` section is current
    - `--repo-root` — repo root to scan (default: this checkout)
    - `--doc-set` — which built-in doc-set config to use. No default -- either pass this, or let <repo-root>/refresh-guidance.toml be auto-discovered (never both). (choices computed at runtime)
    - `--agent-toolkit-root` — agent-toolkit checkout used to resolve cross-repo script citations for doc-sets with cross_repo_scripts set (default: $AGENT_TOOLKIT_PATH or ~/Workspace/agent-toolkit)
    - `doc` — repo-relative doc path, e.g. AGENTS.md
    - `heading` — exact `## <heading>` text
    - `--commit` — commit sha to record (default: current HEAD)
    - `--date` — YYYY-MM-DD to record (default: today)
- Environment: `AGENT_TOOLKIT_PATH`
- Filesystem constants:
  - `DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[1]`
  - `DEFAULT_AGENT_TOOLKIT_ROOT = Path(os.environ.get('AGENT_TOOLKIT_PATH', str(Path.home() / 'Workspace' / 'agent-toolkit')))`
- Explicit exit codes: `2`
- Depends on: `cli_common.py`, `gen_interfaces.py`
- Exceptions:
  - `class ConfigError(Exception)` — A refresh-guidance.toml or --doc-set resolution problem.
- Public classes:
  - `class DocSetConfig` — What to scan for one repo: fixed docs, plus where its scripts live.
  - `class UndocumentedDir`
  - `class Section` — One `##` heading's line range.
  - `class Claim`
  - `class Finding`
  - `class SectionStatus`
  - `class CheckResult`
- Public functions:
  - `load_external_doc_set(repo_root: Path) -> DocSetConfig | None` — Load `<repo_root>/refresh-guidance.toml` into a `DocSetConfig`, or `None` if the file doesn't exist.
  - `resolve_doc_set(repo_root: Path, doc_set_name: str | None) -> tuple[DocSetConfig, str]` — Resolve the doc-set to use for `repo_root`, given the (possibly absent) `--doc-set` value.
  - `discover_agents_md(repo_root: Path) -> list[str]` — Return every tracked `AGENTS.md`'s repo-relative path, sorted.
  - `discover_undocumented_dirs(repo_root: Path, threshold: int = UNDOCUMENTED_DIR_THRESHOLD) -> list[UndocumentedDir]` — Flag a top-level directory that looks complex enough to warrant its own `AGENTS.md` but doesn't have one -- the inverse signal from the rest of this module: not "this existing doc is stale" but "no one has written a doc for this yet." Purely a suggestion for a human to weigh; never creates anything.
  - `discover_basename_index(repo_root: Path) -> dict[str, list[str]]` — Map every tracked file's basename to its repo-relative path(s).
  - `discovered_docs(repo_root: Path, doc_set: DocSetConfig) -> list[str]` — Union of the doc-set's fixed docs (only those present) and every auto-discovered `AGENTS.md` -- a fixed doc absent from this repo (e.g.
  - `discover_scripts(repo_root: Path, doc_set: DocSetConfig, agent_toolkit_root: Path | None = None) -> dict[str, Path]` — Map every script basename under the doc-set's script directories (plus its root entrypoints) to its path -- the universe of scripts a command citation can legitimately name.
  - `classify_span(text: str, known_scripts: set[str]) -> tuple[str, str, list[str], str | None, str | None] | None` — Classify one code-span's text as a path or command claim, or None if it isn't claim-shaped at all (a bare flag, an env var name, a glob pattern, a generic shell command with no known script in it, ...).
  - `parse_document(doc_relpath: str, text: str, known_scripts: set[str]) -> tuple[list[Section], list[Claim]]` — One pass over a doc's lines: track `##` headings and fenced-code state together, so a `## `-looking line inside a fenced example is never mistaken for a real section boundary.
  - `check_path_claim(claim: Claim, repo_root: Path, basename_index: dict[str, list[str]]) -> str | None` — Verify a path claim's file/dir exists, and -- when it carries a `#fragment` -- that a matching `##` heading exists in the target doc.
  - `check_command_claim(claim: Claim, scripts: dict[str, Path], cli_cache: dict[Path, gen_interfaces.CliSpec | None], repo_root: Path, basename_index: dict[str, list[str]]) -> str | None` — Verify a command claim's script exists and its cited flags/subcommands are real, by reusing `gen_interfaces`'s own argparse extraction and invocation validator -- the same machinery it uses to keep INTERFACES.md honest, rather than a second implementation of argparse introspection.
  - `git_blame_range(repo_root: Path, doc: str, start_line: int, end_line: int) -> tuple[str | None, str | None]` — Last commit (short sha, date) that touched a section's line range, via `git log -L` -- the secondary staleness signal for a section with no review-state entry yet.
  - `load_state(repo_root: Path, doc_set: DocSetConfig) -> dict[str, dict[str, str]]`
  - `save_state(repo_root: Path, doc_set: DocSetConfig, state: dict[str, dict[str, str]]) -> None`
  - `run_check(repo_root: Path, doc_set_name: str | None, agent_toolkit_root: Path = DEFAULT_AGENT_TOOLKIT_ROOT) -> CheckResult`
  - `render_report(result: CheckResult, doc_set_label: str) -> str`
  - `build_parser() -> argparse.ArgumentParser`
- Subcommand handlers: `cmd_check`, `cmd_mark_reviewed`
- Tested by: `agent-scripts/test_refresh_guidance.py`

### `agent-scripts/repo_identity.py`

repo_identity.py — which repo this checkout is.

- Installed at: `~/.claude/scripts/repo_identity.py` (all harnesses)
- Entrypoint: not executable, `#!/usr/bin/env python3`
- CLI: none (library module).
- Tested by: `agent-scripts/test_gen_skills.py`

### `agent-scripts/second_opinion.py`

second_opinion.py — one-shot adversarial critique of a plan from a non-Claude backend. Single-round by design: the multi-round loop, plan revision, and convergence judgment all require LLM reasoning and live in prose instructions, not here.

- Installed at: `~/.claude/scripts/second_opinion.py` (all harnesses)
- Entrypoint: executable, `#!/usr/bin/env python3`
- CLI (`argparse`): one-shot adversarial critique of a plan from a non-Claude backend
  - `--quiet/-q`
  - `--verbose/-v`
- Subcommands:
  - `detect` — list available backends as JSON
  - `review <plan-file-or-text> [--backend <BACKEND>] [--focus-file <FOCUS_FILE>] [--model-index N]` — get one critique from the priority-selected backend
    - `--backend` — force this backend instead of priority-order fallback (choices computed at runtime)
    - `--focus-file` — path to a file of plan-specific risk hints, appended to the critique prompt as areas to scrutinize (supplements, not replaces, the generic adversarial mandate)
    - `--model-index` — 0-based index into the backend model pool (SECOND_OPINION_{AGY,PI,OPENCODE,COPILOT}_MODEL_POOL) for this call -- round 1 of a rotation is index 0, round 2 is index 1, etc. Supported for agy/pi/opencode/copilot; an explicit index selects the pool even when a single-model override is set, and is a hard error if the pool is unset/empty or the index is out of range (was previously a silent no-op/fallback).
- Environment: `SECOND_OPINION_AGY_MODEL`, `SECOND_OPINION_AGY_MODEL_POOL`, `SECOND_OPINION_AGY_TIMEOUT_SECONDS`, `SECOND_OPINION_COPILOT_MODEL`, `SECOND_OPINION_COPILOT_MODEL_POOL`, `SECOND_OPINION_COPILOT_TIMEOUT_SECONDS`, `SECOND_OPINION_OPENCODE_MODEL`, `SECOND_OPINION_OPENCODE_MODEL_POOL`, `SECOND_OPINION_OPENCODE_TIMEOUT_SECONDS`, `SECOND_OPINION_PI_MODEL`, `SECOND_OPINION_PI_MODEL_POOL`, `SECOND_OPINION_PI_TIMEOUT_SECONDS`, `SECOND_OPINION_TIMEOUT_SECONDS`
- Filesystem constants:
  - `DATA_DIR = Path.home() / '.claude' / 'data' / 'grill'`
- Explicit exit codes: `1`
- Depends on: `cli_common.py`, `llm_backends.py`
- Public functions:
  - `build_prompt(plan_text: str, focus_hints: str | None) -> str` — Build the critique prompt, optionally inserting plan-specific focus hints.
  - `die(msg: str) -> NoReturn` — Print an error to stderr, prefixed for this script, and exit with status 1.
  - `sanitize_plan_text(plan_text: str) -> tuple[str, int]` — Strip ephemeral review debris headers/sections from a plan.
  - `resolve_plan_text(arg: str) -> str` — Resolve a CLI argument to plan text: a file's contents, or the arg itself.
  - `run_agy(prompt: str, *, model_index: int | None = None) -> str` — Run the ``agy`` backend and return its critique text.
  - `run_opencode(prompt: str, *, model_index: int | None = None) -> str` — Run the ``opencode`` backend's adversary agent and return its critique text.
  - `run_copilot(prompt: str, *, model_index: int | None = None) -> str` — Run the ``copilot`` backend and return its critique text.
  - `run_pi(prompt: str, *, model_index: int | None = None) -> str` — Run the ``pi`` backend and return its critique text.
  - `backend_label(backend: str, *, model_index: int | None = None) -> str` — Return ``backend``'s display label, appending the resolved model if any.
  - `build_parser() -> argparse.ArgumentParser` — Build the full argument parser for every subcommand.
  - `ensure_data_dir() -> None` — Create ``DATA_DIR`` if it is missing.
- Subcommand handlers: `cmd_detect`, `cmd_review`
- Tested by: `agent-scripts/test_second_opinion.py`, `agent-scripts/test_timing.py`

### `agent-scripts/seed_hook_subset_guard.py`

seed_hook_subset_guard.py — refuse a commit that drops a seed's SessionStart hook groups.

- Installed at: `~/.claude/scripts/seed_hook_subset_guard.py` (all harnesses)
- Entrypoint: not executable, `#!/usr/bin/env python3`
- CLI (`argparse`): refuse a commit that drops a seed's SessionStart hook groups
  - `--repo-root` — repository root (default: git's toplevel of the cwd)
- Public functions:
  - `check_path(repo_root: Path, path: str) -> tuple[str | None, str | None]` — Return ``(failure_message, skip_note)`` for one seed path — exactly one is non-None.
- Tested by: `test/test_seed_hook_subset_guard.py`

### `agent-scripts/sessionstart_checks.py`

sessionstart_checks.py — run the SessionStart context checks concurrently.

- Installed at: `~/.claude/scripts/sessionstart_checks.py` (all harnesses)
- Entrypoint: not executable, `#!/usr/bin/env python3`
- CLI: none (library module).
- Public functions:
  - `run_checks(checks: list[tuple[str, int]] | None = None) -> str` — Run all checks concurrently, returning their outputs concatenated in the original list order — not completion order — so the session-start context stays stable and reviewable run over run.
- Tested by: `agent-scripts/test_sessionstart_checks.py`

### `agent-scripts/settings_seed_drift_check.py`

SessionStart hook + CLI: detect (and optionally fix) drift between the live ``~/.claude/settings.json`` / ``~/.config/opencode/opencode.jsonc`` / (under WSL) the Windows-side VS Code ``settings.json`` and ``keybindings.json`` and their seeds in the dotfiles repo.

- Installed at: `~/.claude/scripts/settings_seed_drift_check.py` (all harnesses)
- Entrypoint: executable, `#!/usr/bin/env python3`
- CLI (`argparse`): no `description=` set
  - `--quiet/-q`
  - `--verbose/-v`
- Subcommands:
  - `check`
  - `fix`
  - `sync-to-seed [--dotfiles-root <DOTFILES_ROOT>]`
  - `push-vscode [--dotfiles-root <DOTFILES_ROOT>] [--yes]`
- Environment: `PATH`
- Filesystem constants:
  - `HOME = Path.home()`
  - `DOTFILES = Path(__file__).resolve().parents[1]`
  - `PROFILE_MARKER = HOME / '.local' / 'state' / 'agent-toolkit' / 'profile'`
- Depends on: `cli_common.py`
- Exceptions:
  - `class DriftCheckError(Exception)` — Raised when drift checking can't proceed (parse failure, not a missing file).
- Public functions:
  - `json_key_drift(seed: dict[str, object], live: dict[str, object]) -> list[str]` — Return the top-level keys whose values differ between seed and live.
  - `opencode_bypass_drift(seed: dict[str, object], live: dict[str, object]) -> list[str]` — Return allowlist-bypass bash patterns present live but not in the seed.
  - `resolve_profile() -> str` — Return "work" if this machine is work-provisioned, else "personal".
  - `settings_seed_path(root: Path | None = None) -> Path` — Return the seed settings.json path for this machine's profile, under ``root`` (default the ``DOTFILES`` module constant — resolved at call time, not bound at import, so callers that don't pass ``root`` still pick up a patched/overridden ``DOTFILES``).
  - `opencode_seed_path(root: Path | None = None) -> Path | None` — Return the opencode.jsonc seed path under ``root``, or None on a work machine.
  - `vscode_seed_path(name: str, root: Path | None = None) -> Path` — Return the seed path for a VS Code file (``settings.json`` or ``keybindings.json``) under ``root``.
  - `settings_drift(seed: Path, live: Path) -> list[str]` — Return the non-cosmetic settings.json keys that diverged, or [] if either file is missing.
  - `opencode_drift(seed: Path, live: Path) -> str` — Return a drift description for opencode.jsonc non-cosmetic keys, or "".
  - `vscode_drift(seed: Path, live: Path) -> str` — Describe how a live VS Code settings.json/keybindings.json diverged from its seed, or "" if there's nothing to compare or nothing drifted.
- Subcommand handlers: `cmd_check`, `cmd_fix`, `cmd_sync_to_seed`, `cmd_push_vscode`
- Tested by: `agent-scripts/test_settings_seed_drift_check.py`, `test/test_install.py`

### `agent-scripts/standup.py`

standup.py — /standup skill CLI: local data gathering.

- Installed at: `~/.claude/scripts/standup.py` (all harnesses)
- Entrypoint: not executable, `#!/usr/bin/env python3`
- CLI (`argparse`): /standup skill CLI
  - `--quiet/-q`
  - `--verbose/-v`
- Subcommands:
  - `fetch [--date <DATE>]` — gather all sources as JSON
    - `--date` — override reference date (YYYY-MM-DD) — for re-running after a gap (holiday, PTO) where the default last-working-day boundary would miss it
- Filesystem constants:
  - `DATA_DIR = Path.home() / '.claude' / 'data' / 'standup'`
  - `CONFIG_FILE = DATA_DIR / 'config.json'`
  - `BACKLOG_FILE = Path.home() / '.claude' / 'data' / 'backlog' / 'items.json'`
  - `CANONICAL_PENDING_FILE = Path.home() / '.claude' / 'data' / 'backlog' / 'pending_items.json'`
- Explicit exit codes: `1`
- Depends on: `cli_common.py`, `standup_adapters.py`
- Public functions:
  - `today() -> str`
  - `last_working_day(ref: date) -> date`
  - `find_previous_standup(before: date) -> dict[str, str] | None`
  - `load_config() -> dict[str, object]`
  - `load_canonical_pending() -> list[dict[str, object]]` — Read-only view of dev_status.py's pending-items store.
  - `git_commits(repos: list[str], since_days: int) -> tuple[list[dict[str, str]], list[dict[str, str]]]`
  - `backlog_items(prefixes: list[str], recent_done_days: int) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]], list[dict[str, str]]]`
- Subcommand handlers: `cmd_fetch`
- Tested by: `agent-scripts/test_standup.py`

### `agent-scripts/standup_adapters.py`

standup_adapters.py — provider-agnostic adapter interfaces for /standup.

- Installed at: `~/.claude/scripts/standup_adapters.py` (all harnesses)
- Entrypoint: not executable, `#!/usr/bin/env python3`
- CLI: none (library module).
- Depends on: `outlook_calendar.py`, `outlook_email.py`
- Exceptions:
  - `class NotConfiguredError(Exception)` — Raised by a stub adapter — no concrete implementation exists yet.
- Public classes:
  - `class Item`
  - `class Message`
  - `class CalEvent`
  - `class IssueTrackerAdapter(Protocol)`
  - `class ChatAdapter(Protocol)`
  - `class EmailAdapter(Protocol)`
  - `class CalendarAdapter(Protocol)`
  - `class StubIssueTrackerAdapter`
  - `class StubChatAdapter`
  - `class OutlookEmailAdapter` — Email adapter communicating with Outlook on Windows host via PowerShell COM.
  - `class StubEmailAdapter`
  - `class OutlookCalendarAdapter` — Calendar adapter communicating with Outlook on Windows host via PowerShell COM.
  - `class StubCalendarAdapter`
- Tested by: `agent-scripts/test_gen_interfaces.py`, `agent-scripts/test_outlook_calendar.py`, `agent-scripts/test_outlook_email.py`

### `agent-scripts/statusline.py`

Claude Code status line: render the model name and a color-coded context window usage bar with the used percentage, from the JSON session payload Claude Code pipes to this script on stdin.

- Installed at: `~/.claude/scripts/statusline.py` (all harnesses)
- Entrypoint: executable, `#!/usr/bin/env python3`
- CLI: none (library module).
- Explicit exit codes: `0`
- Tested by: `agent-scripts/test_statusline.py`

### `agent-scripts/to_tickets_runner.py`

to_tickets_runner.py — create a linked batch of dev_status.py backlog items from a confirmed vertical-slice/tracer-bullet ticket breakdown.

- Installed at: `~/.claude/scripts/to_tickets_runner.py` (all harnesses)
- Entrypoint: executable, `#!/usr/bin/env python3`
- CLI (`argparse`): Create a linked batch of dev_status.py backlog items from a confirmed ticket breakdown.
- Subcommands:
  - `run <batch_file>` — create every ticket in a batch file
    - `batch_file` — path to the batch JSON file
- Filesystem constants:
  - `DATA_DIR = Path.home() / '.claude' / 'data' / 'to-tickets'`
- Explicit exit codes: `1`
- Depends on: `dev_status.py`
- Exceptions:
  - `class BatchError(Exception)` — A problem with the batch itself: bad schema, a cycle, an unknown slug.
  - `class SlugCollisionError(Exception)` — A drafted slug collides with an unrelated, pre-existing item.
- Public classes:
  - `class Ticket(TypedDict)`
- Public functions:
  - `ensure_data_dir() -> None` — Create ``DATA_DIR`` if it is missing.
  - `load_batch(path: Path) -> list[Ticket]` — Load and validate the batch file at ``path``.
  - `compute_order(tickets: list[Ticket], index: dev_status.BacklogIndex) -> list[str]` — Compute a safe creation order for ``tickets`` from their ``blocked_by`` edges.
  - `load_state(batch_path: Path) -> dict[str, object] | None` — Load the state file for ``batch_path``, or ``None`` if absent/unreadable.
  - `write_state(batch_path: Path, state: dict[str, object]) -> None` — Atomically write ``state`` to ``batch_path``'s state file.
  - `delete_state(batch_path: Path) -> None` — Remove ``batch_path``'s state file, if any.
  - `run(batch_path: Path) -> list[str]` — Create every ticket in ``batch_path``'s batch, resuming if interrupted before.
  - `build_parser() -> argparse.ArgumentParser`
- Subcommand handlers: `cmd_run`
- Tested by: `agent-scripts/test_to_tickets_runner.py`

### `agent-scripts/vitals_promotion.py`

vitals-promotion.py — mechanical vitals-promotion pass over grill session data.

- Installed at: `~/.claude/scripts/vitals_promotion.py` (all harnesses)
- Entrypoint: not executable, `#!/usr/bin/env python3`
- CLI (`argparse`): vitals-promotion.py — mechanical vitals-promotion pass over grill session data.
  - `--quiet/-q`
  - `--verbose/-v`
  - `--data-dir` — grill session data directory (default: ~/.claude/data/grill)
  - `--apply` — write vitals/needs-review files (default: dry-run, prints only)
  - `--needs-review-summary` — print a one-line summary of the latest needs-review file and exit
- Filesystem constants:
  - `DATA_DIR = Path.home() / '.claude' / 'data' / 'grill'`
  - `VITALS_DIR = DATA_DIR / 'vitals'`
  - `NEEDS_REVIEW_DIR = DATA_DIR / 'needs-review'`
- Depends on: `cli_common.py`
- Public classes:
  - `class Verdict(TypedDict)`
  - `class Decision(TypedDict)`
  - `class Session(TypedDict)`
  - `class VitalsRecord(TypedDict, total=False)`
  - `class NeedsReviewEntry(TypedDict)`
  - `class Report(TypedDict)`
- Public functions:
  - `now_iso() -> str`
  - `is_open(decision: Decision) -> bool`
  - `load_all_sessions(data_dir: Path) -> list[Session]`
  - `build_decision_lookup(sessions: list[Session]) -> dict[DecisionKey, Decision]`
  - `atomic_write_json(path: Path, payload: object) -> None`
  - `load_vitals_file(path: Path) -> list[VitalsRecord]`
  - `vitals_path(vitals_dir: Path, backlog_slug: str | None) -> Path`
  - `latest_needs_review_file(needs_review_dir: Path) -> Path | None` — Return the most recently dated needs-review file, or None if none exist.
  - `summarize_needs_review(entries: list[NeedsReviewEntry]) -> str` — One-line summary: count plus the earliest-dated entry, by source_slug prefix.
  - `anomaly_reason(decision: Decision) -> str`
  - `classify_decision(decision: Decision) -> str` — Classify one closed decision.
  - `needs_review_reason(decision: Decision) -> str` — Which NEEDS_REVIEW sub-condition fired, in priority order (for the breakdown).
  - `supersede_reason(record: VitalsRecord, lookup: dict[DecisionKey, Decision]) -> str | None` — Return why ``record`` should be superseded, or None if it's still valid.
  - `run(data_dir: Path, apply: bool) -> Report`
  - `print_report(report: Report, apply: bool, quiet: bool = False) -> None`
- Tested by: `agent-scripts/test_vitals_promotion.py`

---

## 2. Skill and command surface

Each harness gets a port of the same skill surface. Presence below is
the file existing in the repo; the description is the canonical
`claude/commands/` frontmatter.

| Skill | claude | copilot | opencode | agy | pi |
| --- | --- | --- | --- | --- | --- |
| `/analyze-sessions` | yes | yes | yes | yes | yes |
| `/backlog-item` | yes | yes | yes | yes | yes |
| `/dashboard` | yes | yes | yes | yes | yes |
| `/draft-voice` | yes | — | — | — | — |
| `/grill-me` | yes | yes | yes | yes | yes |
| `/make-skill` | yes | yes | yes | yes | yes |
| `/recap` | yes | yes | yes | yes | yes |
| `/refresh-guidance` | yes | — | yes | — | yes |
| `/second-opinion` | yes | yes | yes | yes | yes |
| `/skill-map` | yes | — | — | — | — |
| `/spec` | yes | yes | yes | yes | yes |
| `/standup` | yes | yes | yes | yes | yes |
| `/swarm` | yes | yes | — | — | — |
| `/to-tickets` | yes | yes | yes | yes | yes |

- **`/analyze-sessions`** — Analyze coding-agent sessions across pi, Claude Code, opencode, Copilot CLI, and agy: calculate token/USD cost rollups, list user prompts, or search message transcripts. Use when the user asks about session costs, token usage, previous prompts, or wants to search past coding session transcripts across harnesses.
  - Source: `claude/commands/analyze-sessions.md`
  - Installed at: `~/.claude/commands/analyze-sessions.md` (claude)
- **`/backlog-item`** — Runs a dev_status.py backlog item end-to-end: resolve, worktree, spec (escalating to grill-me only for a genuinely open design branch), second-opinion critique, execution handoff, TDD implement, verify, commit/merge/push gates, review+approve. Use when the user says 'work on backlog item 4', 'pick up <slug>', 'let's do the next backlog item', or otherwise names a specific item to work end-to-end. Add --auto (optionally with a slug) for an unattended single-item or full-READY-batch run — commit and merge/push gates still stop live, per item.
  - Source: `claude/commands/backlog-item.md`
  - Installed at: `~/.claude/commands/backlog-item.md` (claude)
- **`/dashboard`** — surfaces backlog and pending items as a dashboard. use when the user says 'dashboard', 'what's pending', 'show backlog', 'where we at', 'what am i working on', 'open items', or any variant of checking current work status. Renamed from /status to avoid colliding with Claude Code's built-in /status (plan usage/rate-limit view) — a naming collision with a built-in command can silently break custom command loading. (session start is covered by a SessionStart hook — do not run this again unprompted.)
  - Source: `claude/commands/dashboard.md`
  - Installed at: `~/.claude/commands/dashboard.md` (claude)
- **`/draft-voice`** — Apply the user's own voice and formatting rules when drafting an outgoing informal peer message on their behalf — a Teams reply, a Slack-style ping, a PR comment to a teammate. Use when asked to 'draft a reply', 'write a Teams message', 'draft a Slack message to my teammate', 'write a PR comment', 'respond to this PR comment', 'draft a message to my coworker', or similar. Do not use for formal drafts (email to a director, a written PR description, a public README) — ask before applying these rules there.
  - Source: `claude/commands/draft-voice.md`
  - Installed at: `~/.claude/commands/draft-voice.md` (claude)
- **`/grill-me`** — Interview the user relentlessly about a plan or design until reaching shared understanding, resolving each branch of the decision tree. Use when user wants to stress-test a plan, get grilled on their design, or mentions "grill me".
  - Source: `claude/commands/grill-me.md`
  - Installed at: `~/.claude/commands/grill-me.md` (claude)
- **`/make-skill`** — Author or revise a Claude Code skill (slash command) using a trigger/structure/steering/pruning rubric. Use when the user wants to create a new skill, improve or simplify an existing one, or complains a skill isn't triggering or isn't being followed.
  - Source: `claude/commands/make-skill.md`
  - Installed at: `~/.claude/commands/make-skill.md` (claude)
- **`/recap`** — prints a friendly prose recap of recent activity. use when the user says 'recap', 'what did we do', 'catch me up', 'summary of recent work', or any variant of requesting a recap.
  - Source: `claude/commands/recap.md`
  - Installed at: `~/.claude/commands/recap.md` (claude)
- **`/refresh-guidance`** — Audit this repo's hand-authored, agent-facing docs (AGENTS.md, README.md, STYLE.md, CHANGELOG.md, etc.) for mechanically-broken citations — dead file paths, dead command/flag references — and surface which `##` sections haven't had a human-confirmed review in a while. Use when the user says 'refresh guidance', 'audit the docs', 'check the docs for staleness', 'run refresh-guidance', or asks which doc sections need review.
  - Source: `claude/commands/refresh-guidance.md`
  - Installed at: `~/.claude/commands/refresh-guidance.md` (claude)
- **`/second-opinion`** — Send a plan to a non-Claude model for adversarial critique, then iterate — revise, re-send, repeat — until the critique stops surfacing anything new or a round cap is hit. Use when the user wants a second opinion, an outside critique, or to stress-test a plan against a different model.
  - Source: `claude/commands/second-opinion.md`
  - Installed at: `~/.claude/commands/second-opinion.md` (claude)
- **`/skill-map`** — Shows how the dotfiles skills connect and flags any skill mentioned by another that no longer exists. Use when the user says "skill map", "show the skill map", "which skill for X", or asks how the skills chain together.
  - Source: `claude/commands/skill-map.md`
  - Installed at: `~/.claude/commands/skill-map.md` (claude)
- **`/spec`** — Turn a vague coding task into a structured specification (objective, context, inputs, output format, constraints, evaluation criteria, edge cases, verification steps) before generation begins. Use when the user wants to formalize a task, write a spec, or invokes /spec.
  - Source: `claude/commands/spec.md`
  - Installed at: `~/.claude/commands/spec.md` (claude)
- **`/standup`** — Gather assigned work, chat signal, calendar events, pending replies, git commits, and backlog activity into a daily standup draft, saved to a dated file. Use when the user says 'standup', 'prep for standup', or wants their daily status pulled together.
  - Source: `claude/commands/standup.md`
  - Installed at: `~/.claude/commands/standup.md` (claude)
- **`/swarm`** — Hand READY backlog items to pi agents running in herdr tabs — a real fan-out across the queue by default, or a single item when one is named. Use when the user says 'swarm', 'swarm the backlog', 'hand this to pi', 'give <item> to a pi agent', or 'delegate to a pi worker'. Requires HERDR_ENV=1; says so and stops otherwise.
  - Source: `claude/commands/swarm.md`
  - Installed at: `~/.claude/commands/swarm.md` (claude)
- **`/to-tickets`** — Decompose a plan or spec into multiple linked dev_status.py backlog items — vertical-slice/tracer-bullet tickets joined by blocked_by edges — after confirming the breakdown with the user. Use when the user wants a plan broken into tickets, wants a spec turned into backlog items, or invokes /to-tickets.
  - Source: `claude/commands/to-tickets.md`
  - Installed at: `~/.claude/commands/to-tickets.md` (claude)

---

## 3. Other harness assets

Everything under the harness directories that is neither a shared script
(section 1) nor a skill document (section 2). A source with no
destination is either read by another file in the repo or seeded by
install.py rather than symlinked — `settings.json` and `opencode.jsonc`
are copy-once seeds for exactly that reason.

| Source | Installed at |
| --- | --- |
| `claude/CORE_INSTRUCTIONS.md` | `~/.claude/CLAUDE.md` (claude), `~/.copilot/copilot-instructions.md` (copilot), `~/.gemini/GEMINI.md` (agy), `~/.pi/agent/AGENTS.md` (pi) |
| `claude/icons/agy.png` | not symlinked by `links.toml` |
| `claude/icons/claude.png` | not symlinked by `links.toml` |
| `claude/icons/copilot.png` | not symlinked by `links.toml` |
| `claude/icons/opencode.png` | not symlinked by `links.toml` |
| `claude/icons/pi.png` | not symlinked by `links.toml` |
| `claude/output-styles/ConciseSTE.md` | `~/.claude/output-styles/ConciseSTE.md` (claude) |
| `claude/settings.json` | not symlinked by `links.toml` |
| `claude/settings.work.json` | not symlinked by `links.toml` |
| `copilot/CLAUDE_CODE_PARITY.md` | not symlinked by `links.toml` |
| `copilot/aliases.zsh` | `~/.copilot_aliases` (copilot) |
| `copilot/hooks/agent-stop.json` | `~/.copilot/hooks/agent-stop.json` (copilot, mac, linux) |
| `copilot/hooks/post-tool-use.json` | `~/.copilot/hooks/post-tool-use.json` (copilot, mac, linux) |
| `copilot/hooks/pre-tool-use.json` | `~/.copilot/hooks/pre-tool-use.json` (copilot, mac, linux) |
| `copilot/hooks/session-start.json` | `~/.copilot/hooks/session-start.json` (copilot, mac, linux) |
| `opencode/CLAUDE_CODE_PARITY.md` | not symlinked by `links.toml` |
| `opencode/opencode.jsonc` | not symlinked by `links.toml` |
| `opencode/plugin/guard-rails.ts` | `~/.config/opencode/plugin/guard-rails.ts` (opencode) |
| `opencode/plugin/notify.ts` | `~/.config/opencode/plugin/notify.ts` (opencode) |
| `opencode/plugin/ruff-format-on-edit.ts` | `~/.config/opencode/plugin/ruff-format-on-edit.ts` (opencode) |
| `opencode/tui.json` | `~/.config/opencode/tui.json` (opencode) |
| `agy/CLAUDE_CODE_PARITY.md` | not symlinked by `links.toml` |
| `agy/hooks/agy-elapsed.js` | `~/.claude/hooks/agy-elapsed.js` (agy) |
| `agy/hooks/agy-elapsed.test.js` | not symlinked by `links.toml` |
| `agy/hooks.json` | `~/.gemini/config/hooks.json` (agy) |
| `pi/AGENTS.md` | not symlinked by `links.toml` |
| `pi/CLAUDE.md` | not symlinked by `links.toml` |
| `pi/CLAUDE_CODE_PARITY.md` | not symlinked by `links.toml` |
| `pi/bun.lock` | not symlinked by `links.toml` |
| `pi/bunfig.toml` | not symlinked by `links.toml` |
| `pi/extensions/compaction-backlog-sync.ts` | `~/.pi/agent/extensions/compaction-backlog-sync.ts` (pi) |
| `pi/extensions/custom-footer.ts` | `~/.pi/agent/extensions/custom-footer.ts` (pi) |
| `pi/extensions/cwd.ts` | `~/.pi/agent/extensions/cwd.ts` (pi) |
| `pi/extensions/delegate-tool.ts` | `~/.pi/agent/extensions/delegate-tool.ts` (pi) |
| `pi/extensions/dev-status-tool.ts` | `~/.pi/agent/extensions/dev-status-tool.ts` (pi) |
| `pi/extensions/exit-alias.ts` | `~/.pi/agent/extensions/exit-alias.ts` (pi) |
| `pi/extensions/grill-tool.ts` | `~/.pi/agent/extensions/grill-tool.ts` (pi) |
| `pi/extensions/guard-rails.ts` | `~/.pi/agent/extensions/guard-rails.ts` (pi) |
| `pi/extensions/herdr-blocked-bridge.ts` | `~/.pi/agent/extensions/herdr-blocked-bridge.ts` (pi) |
| `pi/extensions/model-picker.ts` | `~/.pi/agent/extensions/model-picker.ts` (pi) |
| `pi/extensions/notify.ts` | `~/.pi/agent/extensions/notify.ts` (pi) |
| `pi/extensions/pending-plan-surface.ts` | `~/.pi/agent/extensions/pending-plan-surface.ts` (pi) |
| `pi/extensions/permission-gate.ts` | `~/.pi/agent/extensions/permission-gate.ts` (pi) |
| `pi/extensions/philosophy-header.ts` | `~/.pi/agent/extensions/philosophy-header.ts` (pi) |
| `pi/extensions/question-tool.ts` | `~/.pi/agent/extensions/question-tool.ts` (pi) |
| `pi/extensions/ruff-format-on-edit.ts` | `~/.pi/agent/extensions/ruff-format-on-edit.ts` (pi) |
| `pi/extensions/second-opinion-tool.ts` | `~/.pi/agent/extensions/second-opinion-tool.ts` (pi) |
| `pi/extensions/standup-tool.ts` | `~/.pi/agent/extensions/standup-tool.ts` (pi) |
| `pi/extensions/swarm-tool.ts` | `~/.pi/agent/extensions/swarm-tool.ts` (pi) |
| `pi/extensions/to-tickets-tool.ts` | `~/.pi/agent/extensions/to-tickets-tool.ts` (pi) |
| `pi/extensions/trust-session.ts` | `~/.pi/agent/extensions/trust-session.ts` (pi) |
| `pi/extensions/vitals-promotion-tool.ts` | `~/.pi/agent/extensions/vitals-promotion-tool.ts` (pi) |
| `pi/package.json` | not symlinked by `links.toml` |
| `pi/prompts/analyze-sessions.md` | `~/.pi/agent/prompts/analyze-sessions.md` (pi) |
| `pi/prompts/backlog-item.md` | `~/.pi/agent/prompts/backlog-item.md` (pi) |
| `pi/prompts/dashboard.md` | `~/.pi/agent/prompts/dashboard.md` (pi) |
| `pi/prompts/grill-me.md` | `~/.pi/agent/prompts/grill-me.md` (pi) |
| `pi/prompts/make-skill.md` | `~/.pi/agent/prompts/make-skill.md` (pi) |
| `pi/prompts/recap.md` | `~/.pi/agent/prompts/recap.md` (pi) |
| `pi/prompts/refresh-guidance.md` | `~/.pi/agent/prompts/refresh-guidance.md` (pi) |
| `pi/prompts/second-opinion.md` | `~/.pi/agent/prompts/second-opinion.md` (pi) |
| `pi/prompts/spec.md` | `~/.pi/agent/prompts/spec.md` (pi) |
| `pi/prompts/standup.md` | `~/.pi/agent/prompts/standup.md` (pi) |
| `pi/prompts/to-tickets.md` | `~/.pi/agent/prompts/to-tickets.md` (pi) |
| `pi/settings.json` | not symlinked by `links.toml` |
| `pi/test/compaction-backlog-sync.test.ts` | not symlinked by `links.toml` |
| `pi/test/cwd.test.ts` | not symlinked by `links.toml` |
| `pi/test/delegate-tool.test.ts` | not symlinked by `links.toml` |
| `pi/test/dev-status-tool.test.ts` | not symlinked by `links.toml` |
| `pi/test/grill-tool.test.ts` | not symlinked by `links.toml` |
| `pi/test/guard-rails.test.ts` | not symlinked by `links.toml` |
| `pi/test/helpers/sanitize-test-env.ts` | not symlinked by `links.toml` |
| `pi/test/herdr-blocked-bridge.test.ts` | not symlinked by `links.toml` |
| `pi/test/model-picker.test.ts` | not symlinked by `links.toml` |
| `pi/test/notify.test.ts` | not symlinked by `links.toml` |
| `pi/test/pending-plan-surface.test.ts` | not symlinked by `links.toml` |
| `pi/test/permission-gate.test.ts` | not symlinked by `links.toml` |
| `pi/test/philosophy-header.test.ts` | not symlinked by `links.toml` |
| `pi/test/question-tool.test.ts` | not symlinked by `links.toml` |
| `pi/test/ruff-format-on-edit.test.ts` | not symlinked by `links.toml` |
| `pi/test/second-opinion-tool.test.ts` | not symlinked by `links.toml` |
| `pi/test/standup-tool.test.ts` | not symlinked by `links.toml` |
| `pi/test/swarm-tool.test.ts` | not symlinked by `links.toml` |
| `pi/test/test-env.test.ts` | not symlinked by `links.toml` |
| `pi/test/to-tickets-tool.test.ts` | not symlinked by `links.toml` |
| `pi/test/toggle-check.test.ts` | not symlinked by `links.toml` |
| `pi/test/vitals-promotion-tool.test.ts` | not symlinked by `links.toml` |
| `pi/tsconfig.json` | not symlinked by `links.toml` |

---

## 4. Installer entrypoints

Not harness code, but the plumbing that puts everything above in
place. `install.sh` is a POSIX-sh bootstrap with no interface of its
own: it locates a Python 3.12+ interpreter and execs `install.py`,
forwarding argv unchanged.

### `install.py`

install.py — dotfiles + AI-harness provisioner for macOS and Linux/WSL.

- Installed at: not symlinked by `links.toml`
- Entrypoint: executable, `#!/usr/bin/env python3`
- CLI (`argparse`): no `description=` set
  - `--quiet/-q`
  - `--verbose/-v`
  - `--profile` (default: personal)
  - `--harness`
  - `--rollback`
  - `--wipe`
  - `--force`
  - `--dry-run`
  - `--no-nvim-pin`
  - `--reseed`
  - `--adopt`
  - `--force-harness`
  - `--depart`
  - `--yes`
  - `--check-links`
  - `--report-uninstalled`
  - `-h/--help`
- Environment: `AGENT_TOOLKIT_INSTALL_WRAPPER`, `LOGNAME`, `NO_COLOR`, `PATH`, `TERM`, `USER`, `WSL_DISTRO_NAME`
- Filesystem constants:
  - `GLOBAL_GIT_HOOKS_PATH_KEY = 'core.hooksPath'`
- Explicit exit codes: `0`, `2`
- Public classes:
  - `class Palette` — ANSI colorizer that no-ops when color isn't appropriate.
  - `class Reporter` — Collects every step that didn't run, for the end-of-run summary.
  - `class Manifest` — Append-only JSON Lines history of every file mutation, across all runs.
  - `class Options` — Validated command-line options for one invocation.
  - `class Context` — Everything a step needs: paths, options, history, and the skip tally.
  - `class CommandResult` — Outcome of one external command: whether it succeeded, and its stdout.
  - `class LinkSpec` — One row of ``links.toml``: a repo file and where it gets linked.
  - `class ManagedDirSpec` — One row of ``links.toml``: a directory dotfiles owns exclusively.
  - `class ManagedService` — One systemd --user service this installer enables/disables/tracks.
- Public functions:
  - `color_enabled(stream: object) -> bool` — Return whether ANSI codes should be emitted to ``stream``.
  - `detect_wsl(system: str) -> bool` — Return whether this is a WSL kernel (as opposed to native Linux).
  - `build_context(opts: Options, dotfiles: Path | None = None) -> Context` — Assemble a :class:`Context` for a real run on this machine.
  - `check_harness_binaries(ctx: Context) -> list[str]` — Return error strings for requested harnesses whose binaries are not on PATH.
  - `parse_args(argv: Sequence[str]) -> Options` — Parse and validate the command line.
  - `run_command(cmd: Sequence[str] | str, *, shell: bool = False, capture: bool = False) -> CommandResult` — Run an external command, returning success rather than raising.
  - `have(executable: str) -> bool` — Return whether ``executable`` is on PATH.
  - `install_mac_packages(ctx: Context) -> None` — Bootstrap Homebrew if needed, then install the formulae and casks.
  - `install_linux_packages(ctx: Context) -> None` — Install everything the Linux/WSL branch owns: distro packages and extras.
  - `load_links(path: Path) -> list[LinkSpec]` — Parse ``links.toml`` into an ordered list of link specs.
  - `load_managed_dirs(path: Path) -> list[ManagedDirSpec]` — Parse the ``[[managed_dir]]`` rows declaring directories we own exclusively.
  - `link_applies(spec: LinkSpec, ctx: Context) -> bool` — Return whether ``spec`` should be linked for this run's machine/options.
  - `expand_dest(dest: str, home: Path) -> Path` — Expand a ``links.toml`` destination against ``home``.
  - `iter_concrete_links(spec: LinkSpec, ctx: Context) -> Iterator[tuple[Path, Path, str]]` — Expand one ``links.toml`` row into concrete ``(src, dest, relative_src)`` triples.
  - `gather_links(ctx: Context, specs: Sequence[LinkSpec]) -> list[tuple[Path, Path, str, bool]]` — Expand every ``links.toml`` row into concrete triples, once per run.
  - `symlink(ctx: Context, src: Path, dest: Path) -> bool` — Link ``dest`` → ``src``, backing up whatever non-symlink is in the way.
  - `install_symlinks(ctx: Context, links: Sequence[tuple[Path, Path, str, bool]]) -> None` — Link every applicable expanded ``links.toml`` entry.
  - `json_key_drift(seed: dict[str, object], live: dict[str, object]) -> list[str]` — Return the top-level keys whose values differ between seed and live.
  - `opencode_bypass_drift(seed: dict[str, object], live: dict[str, object]) -> list[str]` — Return allowlist-bypass bash patterns present live but not in the seed.
  - `describe_settings_drift(seed: Path, live: Path) -> str` — Describe how a live settings.json diverged from its seed.
  - `describe_opencode_drift(seed: Path, live: Path) -> str` — Describe how a live opencode.jsonc diverged from its seed.
  - `describe_vscode_drift(seed: Path, live: Path) -> str` — Describe how a live VS Code settings/keybindings file diverged from its seed.
  - `seed_vscode_settings(ctx: Context) -> list[tuple[str, tuple[str, str]]]` — Seed the Windows-side VS Code settings.json and keybindings.json under WSL.
  - `seed_file(ctx: Context, seed: Path, dest: Path, *, skip_label: str, drift: Callable[[Path, Path], str], adopt_drift: Callable[[str, str], str] | None = None, adopt_blocker: Callable[[Context, Path, Path, str, str], str | None] | None = None) -> str` — Copy ``seed`` to ``dest`` once, or report drift if it's already there.
  - `seed_claude_settings(ctx: Context) -> tuple[str, str]` — Seed ~/.claude/settings.json, if Claude Code was selected.
  - `seed_pi_settings(ctx: Context) -> tuple[str, str]` — Seed ~/.pi/agent/settings.json, if Pi was selected.
  - `seed_opencode_config(ctx: Context) -> tuple[str, str]` — Seed ~/.config/opencode/opencode.jsonc, if opencode was selected.
  - `capture_service_baseline(ctx: Context) -> None` — Capture every managed service's service/linger state, immediately before :func:`enable_managed_services` runs — capturing any later would record the post-install enabled state as baseline and departure would never disable anything.
  - `enable_managed_services(ctx: Context) -> None` — Enable and start every managed systemd --user unit (Linux, non-work).
  - `capture_git_hooks_path_baseline(ctx: Context) -> None` — Capture the pre-existing global ``core.hooksPath``, immediately before :func:`install_global_git_hooks_path` runs -- capturing any later would record dotfiles' own already-set value as if it were the original, which would make departure "restore" dotfiles' own path instead of the true pre-dotfiles value.
  - `install_global_git_hooks_path(ctx: Context) -> None` — Point global ``core.hooksPath`` at ``githooks-global/``, so every repo without its own local override picks up the no-commit-on-main hook.
  - `import_rectangle_prefs(ctx: Context) -> None` — Import the repo's Rectangle window-manager preferences.
  - `set_caps_lock_to_escape(ctx: Context) -> None` — Remap Caps Lock to Escape by rewriting the ByHost GlobalPreferences plist.
  - `install_vim_plug(ctx: Context) -> None` — Download vim-plug into ~/.vim/autoload, if it isn't there already.
  - `parse_neovim_version(output: str) -> tuple[int, int] | None` — Extract ``(major, minor)`` from ``nvim --version`` output.
  - `neovim_runtime_ok() -> bool` — Whether the Neovim binary on PATH can actually resolve its Lua runtime.
  - `bootstrap_neovim(ctx: Context) -> None` — Sync the vendored Neovim config's plugins with lazy.nvim.
  - `capture_departure_baseline(ctx: Context, specs: Sequence[LinkSpec]) -> None` — Capture this run's departure baseline layer before any install step runs.
  - `write_profile_marker(ctx: Context) -> None` — Mark this machine as work-provisioned, so later plain runs are guarded.
  - `work_guard_blocks(ctx: Context) -> bool` — Return whether a plain personal run must be refused on this machine.
  - `do_rollback(ctx: Context) -> int` — Reverse every file mutation recorded across every past run.
  - `print_summary(ctx: Context, settings: tuple[str, str], opencode: tuple[str, str], vscode: Sequence[tuple[str, tuple[str, str]]] = (), pi_settings: tuple[str, str] = ('', '')) -> None` — Print the loud end-of-run summary: skips, drift, and next steps.
  - `build_preflight_report(ctx: Context) -> dict[str, depart.Classification] | None` — Classify every tracked ownership key, or None if there's no baseline.
  - `build_package_preflight(ctx: Context) -> list[depart.PackageClassification] | None` — Classify every requested/introduced package, or None if there's no baseline.
  - `execute_service_phase(ctx: Context, baseline: depart.Baseline, ledger: depart.DepartureLedger) -> None` — Disable+stop every owned managed service, then reconcile linger once.
  - `execute_gitconfig_phase(ctx: Context, baseline: depart.Baseline, ledger: depart.DepartureLedger) -> None` — Restore the pre-dotfiles global core.hooksPath value, if this installer owns the current value.
  - `execute_file_symlink_phase(ctx: Context, baseline: depart.Baseline, report: dict[str, depart.Classification], ledger: depart.DepartureLedger) -> None` — Execute every owned ``file:``/``symlink:`` action, in pinned order.
  - `execute_directory_phase(ctx: Context, baseline: depart.Baseline, report: dict[str, depart.Classification], ledger: depart.DepartureLedger) -> None` — Execute every owned ``directory:`` action, deepest-path-first.
  - `execute_runtime_phase(ctx: Context, report: dict[str, depart.Classification], ledger: depart.DepartureLedger) -> None` — Remove the NVM root wholesale, if owned and not already done.
  - `live_package_snapshots(baseline: depart.Baseline) -> dict[str, dict[str, str] | None]` — Fresh probe results for every manager appearing in recorded transactions.
  - `execute_package_phase(ctx: Context, baseline: depart.Baseline, ledger: depart.DepartureLedger) -> bool` — Remove/downgrade owned packages, reverse transactions order.
  - `execute_departure(ctx: Context, baseline: depart.Baseline, report: dict[str, depart.Classification]) -> depart.DepartureLedger` — Perform every safe ``owned`` action, retry-safe via the departure ledger.
  - `do_depart(ctx: Context) -> int` — Preview and execute a pristine-state departure.
  - `do_check_links(ctx: Context) -> int` — Audit the live symlinks against ``links.toml`` and report, changing nothing.
  - `run_install(ctx: Context, specs: Sequence[LinkSpec]) -> int` — Run every install step in order and return the process exit status.
- Tested by: `test/test_install.py`

### `depart.py`

Pristine-state departure mode: baseline capture and ownership tracking.

- Installed at: not symlinked by `links.toml`
- Entrypoint: not executable, `#!/usr/bin/env python3`
- CLI: none (library module).
- Public classes:
  - `class Layer` — One capture pass: a timestamp and the ownership-key records from it.
  - `class Baseline` — The full departure baseline: immutable first layer, later supplements.
  - `class Transaction` — One package-manager operation's before/after state and provenance.
  - `class Classification`
  - `class DepartureLedger` — Crash-safe, fsynced-per-entry record of completed departure actions.
  - `class LockInfo`
  - `class PackageClassification` — One requested-or-introduced package's departure classification.
- Public functions:
  - `file_key(path: Path) -> str`
  - `symlink_key(path: Path) -> str`
  - `directory_key(path: Path) -> str`
  - `package_key(manager: str, name: str) -> str`
  - `service_key(manager: str, name: str) -> str`
  - `runtime_key(root: Path) -> str`
  - `gitconfig_key(name: str) -> str`
  - `key_type(key: str) -> str` — Return the ``type`` half of an ownership key string.
  - `capture_file(path: Path, *, blob_dir: Path | None = None) -> dict[str, object]` — Capture a ``file:`` record: present (with size+sha256), absent, or unknown.
  - `capture_symlink(path: Path) -> dict[str, object]` — Capture a ``symlink:`` record: present (with target text), absent, unknown.
  - `capture_directory(path: Path) -> dict[str, object]` — Capture a ``directory:`` record: present, absent, or unknown.
  - `ancestor_directories(path: Path, home: Path) -> list[Path]` — Every directory strictly between ``path``'s parent and ``home``.
  - `capture_tree_manifest(root: Path) -> dict[str, object]` — Full recursive manifest for a small, fully installer-controlled tree.
  - `tree_manifest_matches(root: Path, recorded: dict[str, object]) -> bool` — Whether ``root``'s current tree still matches a recorded tree manifest.
  - `capture_runtime_nvm(home: Path) -> dict[str, object]` — Capture the lightweight ``runtime:<root>`` record for NVM/Node.
  - `runtime_nvm_diverged(home: Path, recorded: dict[str, object]) -> bool` — Whether live NVM state has diverged from its recorded top-level markers.
  - `blob_path(state_dir: Path, digest: str) -> Path`
  - `write_blob(state_dir: Path, content: bytes) -> str` — Write a content-addressed blob, returning its SHA-256 hex digest.
  - `read_blob(state_dir: Path, digest: str) -> bytes | None` — Read a blob's content back, or None if it's missing/unreadable.
  - `record_installed_tree(baseline: Baseline, root: Path) -> None` — Snapshot ``root`` as the installer just produced it.
  - `installed_tree_verdict(baseline: Baseline, root: Path) -> str` — Classify a wholly installer-owned tree for safe wholesale removal.
  - `remove_manifest_tree(baseline: Baseline, path: Path) -> str` — Remove ``path`` wholesale, but only if its :func:`installed_tree_verdict` is ``TREE_UNCHANGED`` — the departure-time policy: refuse unless provably unchanged, since refusing is departure's safe terminal outcome.
  - `baseline_path(state_dir: Path) -> Path`
  - `baseline_to_dict(baseline: Baseline) -> dict[str, object]`
  - `baseline_from_dict(data: dict[str, object]) -> Baseline`
  - `save_baseline(state_dir: Path, baseline: Baseline) -> None` — Durably write ``baseline.json`` via a temp-file-then-replace swap.
  - `load_baseline(state_dir: Path) -> Baseline | None` — Load ``baseline.json``, or None if it's missing, empty, or unparseable.
  - `dpkg_query_command() -> list[str]` — List every installed apt package and its version, tab-separated.
  - `parse_dpkg_query(output: str) -> dict[str, str]` — Parse :func:`dpkg_query_command` output into ``{name: version}``.
  - `rpm_qa_command() -> list[str]` — List every installed dnf/rpm package and its version, tab-separated.
  - `parse_rpm_qa(output: str) -> dict[str, str]` — Parse :func:`rpm_qa_command` output into ``{name: version}``.
  - `npm_ls_global_command() -> list[str]` — List globally installed npm packages as JSON.
  - `parse_npm_ls_global(output: str) -> dict[str, str]` — Parse :func:`npm_ls_global_command` JSON output into ``{name: version}``.
  - `uv_tool_list_command() -> list[str]` — List installed ``uv tool`` packages and their versions.
  - `parse_uv_tool_list(output: str) -> dict[str, str]` — Parse :func:`uv_tool_list_command` output into ``{name: version}``.
  - `apt_remove_command(package: str) -> list[str]` — Pinned apt removal command — never ``purge`` or ``autoremove``.
  - `dnf_remove_command(package: str) -> list[str]` — Pinned dnf removal command — never ``autoremove``.
  - `npm_uninstall_command(package: str) -> list[str]`
  - `uv_tool_uninstall_command(name: str) -> list[str]`
  - `apt_downgrade_command(package: str, version: str) -> list[str]`
  - `dnf_downgrade_command(package: str, version: str) -> list[str]`
  - `apt_rdepends_command(package: str) -> list[str]`
  - `dnf_whatrequires_command(package: str) -> list[str]`
  - `classify_rdepends_result(ok: bool, stdout: str) -> str` — Classify a reverse-dependency probe's outcome for removal eligibility.
  - `transaction_to_dict(txn: Transaction) -> dict[str, object]`
  - `transaction_from_dict(data: dict[str, object]) -> Transaction`
  - `record_transaction(baseline: Baseline, *, manager: str, requested: list[str], before: dict[str, str], after: dict[str, str], captured_at: str, epoch: dict[str, str] | None = None) -> Transaction` — Build a :class:`Transaction`, append it to ``baseline``, and return it.
  - `earliest_recorded_version(baseline: Baseline, manager: str, name: str) -> str | None` — ``name``'s true pre-install version for ``manager``, if ever recorded.
  - `downgrade_candidates(baseline: Baseline, manager: str, name: str) -> list[str]` — Versions to try, in order, when downgrading an installer-upgraded package.
  - `classify_ownership_key(key: str, recorded: dict[str, object] | None, live: dict[str, object]) -> Classification` — Classify one ownership key for the departure preflight report.
  - `reclassify_rc_file(recorded: dict[str, object] | None, baseline_content: bytes | None, live_content: bytes | None) -> Classification | None` — Override the generic classification for one rc file, if warranted.
  - `reclassify_symlink_destination_pair(file_recorded: dict[str, object] | None, file_live: dict[str, object], symlink_recorded: dict[str, object] | None, symlink_live: dict[str, object]) -> tuple[Classification, Classification] | None` — Jointly reclassify a destination's ``file:``/``symlink:`` key pair.
  - `departure_ledger_path(state_dir: Path) -> Path`
  - `departure_lock_path(state_dir: Path) -> Path`
  - `parse_lock_text(text: str) -> LockInfo | None`
  - `read_lock(path: Path) -> LockInfo | None`
  - `process_start_time(pid: int) -> int | None` — Read a process's start-time field (field 22) from ``/proc/<pid>/stat``.
  - `is_lock_holder_alive(pid: int, recorded_start_time: int, *, start_time_fn: Callable[[int], int | None] = process_start_time) -> bool` — Whether the recorded lock holder is still running the same process.
  - `acquire_departure_lock(state_dir: Path, *, pid: int | None = None, start_time_fn: Callable[[int], int | None] = process_start_time) -> tuple[bool, LockInfo | None]` — Try to acquire the departure advisory lock.
  - `release_departure_lock(state_dir: Path, pid: int | None = None) -> None` — Release and unlink the lock, only if it's still recorded as ours.
  - `classify_package_transactions(baseline: Baseline, live_snapshots: dict[str, dict[str, str] | None]) -> list[PackageClassification]` — Classify every requested/introduced package, in departure removal order.
  - `build_service_record(*, enabled: bool | None, active: bool | None, linger: bool | None) -> dict[str, object]` — Build a ``service:`` record from already-probed values.
  - `classify_service(recorded: dict[str, object] | None, live: dict[str, object]) -> Classification` — Classify the watchcommit service/linger key.
  - `build_gitconfig_record(value: str | None) -> dict[str, object]` — Build a ``gitconfig:`` record from an already-read global config value.
  - `classify_gitconfig(recorded: dict[str, object] | None, live: dict[str, object], managed_value: str) -> Classification` — Classify a single global git config key this installer manages.
- Tested by: `test/test_depart.py`, `test/test_depart_transactions.py`, `test/test_install.py`

### `scripts/sync_from_dotfiles.py`

sync_from_dotfiles.py — replay dotfiles' harness changes onto this toolkit.

- Installed at: not symlinked by `links.toml`
- Entrypoint: not executable, `#!/usr/bin/env python3`
- CLI (`argparse`): replay dotfiles' harness changes onto this toolkit
  - `--apply` — derive, verify, and apply the sync (default: report/diff only)
  - `--since` — override the stored dotfiles BASE (first run, or recovery)
  - `--dotfiles-path` — path to the dotfiles checkout (default: ~/dotfiles)
  - `--quiet/-q`
  - `--verbose/-v`
- Filesystem constants:
  - `REPO_ROOT = Path(__file__).resolve().parent.parent`
  - `DEFAULT_DOTFILES_PATH = Path.home() / 'dotfiles'`
- Explicit exit codes: `1`, `2`
- Public functions:
  - `state_path(repo_root: Path) -> Path` — Return the path to the committed sync-state marker.
  - `load_state(repo_root: Path) -> dict[str, object] | None` — Load the sync-state marker, or None if this repo has never synced.
  - `write_state(repo_root: Path, *, dotfiles_sha: str, toolkit_commit: str | None) -> None` — Record a successful sync as the new BASE for the next run.
  - `run_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]` — Run a git command in ``repo``, capturing output as text.
  - `changed_paths(repo: Path, base: str, tip: str) -> frozenset[str]` — Return every path git reports as changed between two refs.
  - `path_exists_at(repo: Path, ref: str, path: str) -> bool` — Return whether ``path`` exists in ``repo`` at ``ref``.
  - `read_at(repo: Path, ref: str, path: str) -> bytes` — Return the raw bytes of ``path`` in ``repo`` at ``ref``.
  - `resolve_head(repo: Path) -> str` — Return the current HEAD commit of ``repo``.
  - `root_commit(repo: Path) -> str` — Return the earliest commit reachable from HEAD in ``repo``.
  - `resolve_toolkit_anchor(repo_root: Path, state: dict[str, object] | None) -> str` — Toolkit-side anchor for conflict-set math.
  - `git_add(repo_root: Path, paths: Sequence[str]) -> None` — Stage the given repo-relative paths.
  - `write_copies(repo_root: Path, dotfiles_path: Path, tip: str, paths: Sequence[str]) -> None` — Write each path's byte-identical content from dotfiles@tip into the repo.
  - `run_generator_sweep(repo_root: Path, sweep: Sequence[str], *, quiet: bool, verbose: bool) -> None` — Run every generator in ``sweep`` so copied artifacts describe this repo.
  - `apply_sync(repo_root: Path, dotfiles_path: Path, tip: str, plain_copies: Sequence[str], handled: Sequence[str], base: str, *, generator_sweep: Sequence[str] = GENERATOR_SWEEP, quiet: bool = False, verbose: bool = False) -> None` — Write copies, apply handled conflicts, sweep, then record new state.
  - `match_never_synced(path: str) -> bool` — Whether ``path`` is never synced between the two checkouts.
  - `compute_copy_set(dotfiles_changed: frozenset[str]) -> frozenset[str]` — Paths to replay onto the toolkit: everything dotfiles changed, minus the deliberate-deletion blocklist, the dotfiles-only exclude list, and the never-synced repo-specific paths (same relative path in both checkouts, intentionally different content — copying one over the other would clobber).
  - `compute_conflict_set(dotfiles_changed: frozenset[str], toolkit_changed: frozenset[str]) -> frozenset[str]` — Paths both sides changed since the last sync — never assumed, always derived.
  - `find_unclassified_divergences(common_paths: Iterable[str], diverged_paths: Iterable[str], diverged: Callable[[str], bool]) -> list[str]` — Same-path divergences between the two checkouts with no classification.
  - `classify_conflict(path: str) -> str` — Classify one conflict path: "generated_artifact", "handled", or "unclassified" (category 4 — stop and hand-resolve, the safe default).
  - `verify_invariants(dotfiles_path: Path, base: str, tip: str, copy_set: frozenset[str], plain_copies: Sequence[str], toolkit_root: Path = REPO_ROOT) -> list[str]` — Check the invariants that must hold before any write.
  - `build_parser() -> argparse.ArgumentParser` — Build the argument parser.
- Tested by: `scripts/test_sync_from_dotfiles.py`

---

## 5. Skill/command doc contract coverage

For each backing script below, every skill/command doc that shows an
example of running it, and whether that example's subcommand and
flags still match the script's real CLI contract. A doc with no shown
invocation of a given script is not listed. `--check` exits `3` (not
`1`) when this section would change, since the fix is editing the
named doc, not regenerating this file.

### `analyze_sessions.py`

| Doc | Status |
| --- | --- |
| `agy/skills/analyze-sessions/SKILL.md` | OK |
| `claude/commands/analyze-sessions.md` | OK |
| `copilot/skills/analyze-sessions/SKILL.md` | OK |
| `opencode/skills/analyze-sessions/SKILL.md` | OK |

### `dev_status.py`

| Doc | Status |
| --- | --- |
| `agy/skills/backlog-item/SKILL.md` | OK |
| `agy/skills/dashboard/SKILL.md` | OK |
| `agy/skills/recap/SKILL.md` | OK |
| `agy/skills/second-opinion/SKILL.md` | OK |
| `agy/skills/standup/SKILL.md` | OK |
| `claude/commands/backlog-item.md` | OK |
| `claude/commands/dashboard.md` | OK |
| `claude/commands/recap.md` | OK |
| `claude/commands/second-opinion.md` | OK |
| `claude/commands/standup.md` | OK |
| `copilot/skills/backlog-item/SKILL.md` | OK |
| `copilot/skills/dashboard/SKILL.md` | OK |
| `copilot/skills/recap/SKILL.md` | OK |
| `copilot/skills/second-opinion/SKILL.md` | OK |
| `copilot/skills/standup/SKILL.md` | OK |
| `opencode/command/backlog-item.md` | OK |
| `opencode/command/dashboard.md` | OK |
| `opencode/command/recap.md` | OK |
| `opencode/command/second-opinion.md` | OK |
| `opencode/command/standup.md` | OK |
| `opencode/skills/second-opinion/SKILL.md` | OK |
| `pi/skills/backlog-item/SKILL.md` | OK |
| `pi/skills/dashboard/SKILL.md` | OK |
| `pi/skills/recap/SKILL.md` | OK |
| `pi/skills/second-opinion/SKILL.md` | OK |
| `pi/skills/standup/SKILL.md` | OK |

### `gen_interfaces.py`

| Doc | Status |
| --- | --- |
| `claude/commands/skill-map.md` | OK |

### `grill.py`

| Doc | Status |
| --- | --- |
| `agy/skills/grill-me/SKILL.md` | OK |
| `agy/skills/second-opinion/SKILL.md` | OK |
| `agy/skills/spec/SKILL.md` | OK |
| `agy/skills/to-tickets/SKILL.md` | OK |
| `claude/commands/grill-me.md` | OK |
| `claude/commands/second-opinion.md` | OK |
| `claude/commands/spec.md` | OK |
| `claude/commands/to-tickets.md` | OK |
| `copilot/skills/grill-me/SKILL.md` | OK |
| `copilot/skills/second-opinion/SKILL.md` | OK |
| `copilot/skills/spec/SKILL.md` | OK |
| `copilot/skills/to-tickets/SKILL.md` | OK |
| `opencode/command/grill-me.md` | OK |
| `opencode/command/second-opinion.md` | OK |
| `opencode/command/spec.md` | OK |
| `opencode/command/to-tickets.md` | OK |
| `opencode/skills/grill-me/SKILL.md` | OK |
| `opencode/skills/second-opinion/SKILL.md` | OK |
| `opencode/skills/spec/SKILL.md` | OK |
| `pi/skills/grill-me/SKILL.md` | OK |
| `pi/skills/second-opinion/SKILL.md` | OK |
| `pi/skills/spec/SKILL.md` | OK |
| `pi/skills/to-tickets/SKILL.md` | OK |

### `herdr_delegate.py`

| Doc | Status |
| --- | --- |
| `claude/commands/swarm.md` | OK |
| `copilot/skills/swarm/SKILL.md` | OK |

### `refresh_guidance.py`

| Doc | Status |
| --- | --- |
| `claude/commands/refresh-guidance.md` | OK |
| `opencode/skills/refresh-guidance/SKILL.md` | OK |

### `second_opinion.py`

| Doc | Status |
| --- | --- |
| `agy/skills/grill-me/SKILL.md` | OK |
| `agy/skills/second-opinion/SKILL.md` | OK |
| `agy/skills/spec/SKILL.md` | OK |
| `claude/commands/grill-me.md` | OK |
| `claude/commands/second-opinion.md` | OK |
| `claude/commands/spec.md` | OK |
| `copilot/skills/grill-me/SKILL.md` | OK |
| `copilot/skills/second-opinion/SKILL.md` | OK |
| `copilot/skills/spec/SKILL.md` | OK |
| `opencode/command/grill-me.md` | OK |
| `opencode/command/second-opinion.md` | OK |
| `opencode/command/spec.md` | OK |
| `opencode/skills/grill-me/SKILL.md` | OK |
| `opencode/skills/second-opinion/SKILL.md` | OK |
| `opencode/skills/spec/SKILL.md` | OK |
| `pi/skills/grill-me/SKILL.md` | OK |
| `pi/skills/second-opinion/SKILL.md` | OK |
| `pi/skills/spec/SKILL.md` | OK |

### `standup.py`

| Doc | Status |
| --- | --- |
| `agy/skills/standup/SKILL.md` | OK |
| `claude/commands/standup.md` | OK |
| `copilot/skills/standup/SKILL.md` | OK |
| `opencode/command/standup.md` | OK |
| `pi/skills/standup/SKILL.md` | OK |

### `to_tickets_runner.py`

| Doc | Status |
| --- | --- |
| `agy/skills/to-tickets/SKILL.md` | OK |
| `claude/commands/to-tickets.md` | OK |
| `copilot/skills/to-tickets/SKILL.md` | OK |
| `opencode/command/to-tickets.md` | OK |
| `pi/skills/to-tickets/SKILL.md` | OK |

### `vitals_promotion.py`

| Doc | Status |
| --- | --- |
| `agy/skills/grill-me/SKILL.md` | OK |
| `claude/commands/grill-me.md` | OK |
| `copilot/skills/grill-me/SKILL.md` | OK |
| `opencode/command/grill-me.md` | OK |
| `opencode/skills/grill-me/SKILL.md` | OK |
| `pi/skills/grill-me/SKILL.md` | OK |

---

## 6. Skill cross-reference graph

Built by scanning each `claude/commands/*.md` skill's own text for
whole-word mentions of the other skills' names (frontmatter
description included). This regenerates with the rest of the file,
so it cannot silently drift the way hand-written relationship notes
could — if a skill stops mentioning another, or starts mentioning a
new one, `--check` catches it the same as any other stale content.

| Skill | Mentions |
| --- | --- |
| `/analyze-sessions` | — |
| `/backlog-item` | `dashboard`, `grill-me`, `second-opinion`, `spec` |
| `/dashboard` | `recap` |
| `/draft-voice` | — |
| `/grill-me` | `second-opinion`, `spec` |
| `/make-skill` | `grill-me` |
| `/recap` | — |
| `/refresh-guidance` | — |
| `/second-opinion` | — |
| `/skill-map` | — |
| `/spec` | `backlog-item`, `grill-me`, `second-opinion` |
| `/standup` | `dashboard` |
| `/swarm` | `backlog-item` |
| `/to-tickets` | `grill-me`, `second-opinion`, `spec` |
