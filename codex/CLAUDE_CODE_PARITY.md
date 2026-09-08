# Codex CLI — Claude Code parity notes

Confirmed facts about OpenAI Codex CLI as a harness in this toolkit,
verified against the official docs (developers.openai.com/codex and
learn.chatgpt.com docs pages) and a live 0.153.4 install on 2026-09-08,
the same way copilot/agy/opencode/pi were grounded. Feeds
`gen_skills.py`'s `CAPABILITY_TABLE` and the skill templates' wording.

## 1. Structured choices

No `AskUserQuestion`-style structured multi-choice widget exists in
Codex CLI (absent from `--help`, `codex exec --help`, and the docs
surface). Enumerable judgment calls go through plain conversational text:
state the options, give a recommendation, wait for a plain-text reply.

## 2. Instructions loading

- Global: `~/.codex/AGENTS.md` (or `AGENTS.override.md`, which wins if
  present; first non-empty file at this level).
- Project: walks repo root → cwd reading `AGENTS.md` per directory; extra
  fallback names only via the `project_doc_fallback_filenames` config
  (unset here).
- Cap: combined chain limited by `project_doc_max_bytes` (32 KiB default).
  The shared `claude/CORE_INSTRUCTIONS.md` is ~27 KB of that budget —
  documented in README.md's codex row, user-side mitigation is raising
  `project_doc_max_bytes` in `~/.codex/config.toml`.

## 3. Skills

- USER scope: `~/.codex/skills/<name>/`, **sibling to** the bundled
  `~/.codex/skills/.system/` skills — NOT `~/.agents/skills/`.
  CORRECTED 2026-09-08 (`atk-codex-skill-root-fix`): the original entry
  here claimed `~/.agents/skills/` from reading upstream docs alone,
  which turned out to be wrong (or at least not true for 0.153.4) —
  a live install's skills there were completely invisible to both
  `$skill-name` and implicit matching. Caught by dumping the actual
  model-visible skill index (`codex debug prompt-input`), whose
  `skills_instructions` block lists a "Skill roots" table — on this
  machine it showed exactly one root, `r0 = ~/.codex/skills/.system`,
  and no `~/.agents/skills` root at all. Codex's own bundled
  `skill-installer` system skill independently confirms the real
  convention: "Installs into `$CODEX_HOME/skills/<skill-name>` (defaults
  to `~/.codex/skills`)." Repo scope and ADMIN scope below are still
  doc-sourced only, not live-reverified the same way — treat them with
  the same suspicion until spot-checked:
  - Repo scope: `.agents/skills/` walking cwd up to the repo root.
  - ADMIN: `/etc/codex/skills`.
- `SKILL.md` frontmatter requires `name` + `description` (agentskills.io
  standard); optional `references/`, `scripts/`, `assets/`,
  `agents/openai.yaml`. This repo's generated codex skills are
  self-contained (no ref dirs).
- Codex does **not** follow symlinks for USER-scope skill discovery.
  CORRECTED 2026-09-08 (`atk-codex-skill-copy-fix`): this doc previously
  claimed the opposite ("Codex follows symlinked skill folders/files when
  scanning (docs, build-skills) — symlink deployment works"), sourced from
  Codex's own docs about a different feature (doc/build-skills scanning),
  not live-verified for skill discovery itself — same mistake as the
  USER-scope path above. Disproved directly: swapped one skill's symlink
  for a real file at the identical path — the file appeared in a fresh
  `codex exec` session's skill list immediately, the symlink never did,
  even after a full interactive-session restart. This repo's generated
  codex skills are deployed by `install.py`'s `sync_codex_skills()`
  copying `codex/skills/<name>/SKILL.md` into place, not by a `links.toml`
  `[[link]]` symlink row like every other harness here.
- Invocation: explicit (`$skill-name`, `/skills`) or implicit
  (description match). Initial skill list budget: 2% of the context
  window or 8,000 characters, descriptions shortened first.

## 4. Hooks

Codex has lifecycle hooks (`~/.codex/hooks.json` or inline `[hooks]` in
config.toml; events include `SessionStart`, `PreToolUse`, `Stop`, …),
but every non-managed hook requires per-definition trust review via
`/hooks`, tracked by content hash. This toolkit deliberately provisions
no codex hooks — that trust gate is a poor fit for provisioned files.
Resume semantics in generated skills therefore mirror agy's (manual
`grill.py pending-plan --consume`), not pi/claude's.

## 5. Slash commands / custom prompts

`~/.codex/prompts/*.md` custom prompts are **deprecated** upstream
("Use skills for reusable prompts"). No command surface is provisioned;
skills cover both implicit and explicit triggering.

## 6. Non-interactive invocation

`codex exec [OPTIONS] [PROMPT]` — positional PROMPT, stdin when piped;
`-m/--model`, `-c key=value` TOML overrides, `--profile`,
`--enable/--disable <FEATURE>`, `--dangerously-bypass-hook-trust`.
`codex --version` reports `codex-cli <version>`. This is the
`harness_discovery_check.py` probe path (opt-in `probe --harness codex`
only; `check` stays pin+metadata since codex is not load-bearing), with
`CODEX_PROBE_MODEL` as the model-override env var mirroring opencode's.

## 7. Config

User-level `~/.codex/config.toml` (TOML; auth, model, trust, sandbox) —
never provisioned or managed by this toolkit. Project `.codex/` layers
load only for trusted projects. System layer: `/etc/codex/config.toml`.
