# Progressive Disclosure Evaluation Rubric for Agent Guidance

This document defines what "agent-ready" repository documentation looks like,
establishing evaluation criteria for progressive disclosure, context budgeting,
directory hazard boundaries, signposting, and cross-harness symlink pairing.

## 1. Architectural Tiers

Agent-facing guidance is structured in three progressive layers to minimize
unnecessary context consumption while ensuring critical boundaries and hazards
are loaded when an agent works in a specific subtree:

```
┌───────────────────────────────────────────────────────────┐
│ Tier 1: Root Navigation Index (AGENTS.md / CLAUDE.md)     │
│ - Global repository invariants, standards, and workflow   │
│ - Directory signposts indexing subtree instructions       │
│ - Line budget: <= 150 content lines                       │
└─────────────────────────────┬─────────────────────────────┘
                              │
                              ▼
┌───────────────────────────────────────────────────────────┐
│ Tier 2: Directory Instructions (<dir>/AGENTS.md + link)   │
│ - Subtree scope, responsibilities, and architecture role  │
│ - Directory hazards, non-obvious traps, gotchas           │
│ - Subtree-local conventions, commands, and verification   │
└─────────────────────────────┬─────────────────────────────┘
                              │
                              ▼
┌───────────────────────────────────────────────────────────┐
│ Tier 3: Task Skills & Prompts (skills/, commands/, etc.)  │
│ - Workflow procedures loaded on demand per slash command  │
└───────────────────────────────────────────────────────────┘
```

### Tier 1: Root Instructions (`AGENTS.md` / `CLAUDE.md`)
The repository root contains `AGENTS.md` and a paired `CLAUDE.md` symlink.
Its role is an **entrypoint index**, not an exhaustive reference manual:
- Global house style and repo-wide standards (e.g. testing tiers, git policies).
- Harness maintenance tiers and cross-harness instruction file conventions.
- **Directory Signposts**: A dedicated directory index section naming every
  code-holding subtree that carries its own local instructions, summarizing its
  boundary and pointing agents directly to its local `AGENTS.md`.
- **Context Budget**: Root instructions must not exceed **150 content lines**
  (lines excluding blank lines and `<!-- ... -->` comment blocks). Subtree
  details, architecture deep-dives, and tool-specific lists belong in Tier 2
  directory docs or `docs/architecture/`.

### Tier 2: Directory Instructions (`<dir>/AGENTS.md`)
Any directory with substantive code or operational complexity (typically >= 5
tracked files with `.py`, `.ts`, or `.sh` code) warrants its own local
`AGENTS.md` file. It focuses strictly on local context:
- **Responsibilities & Boundary**: What this directory owns and where its
  responsibility ends.
- **Hazards & Signposts**: Non-obvious traps, behavioral gotchas, brittle
  coupling, order-of-operation constraints, and prerequisite reads.
- **Local Conventions**: Subtree-specific test runners, typing/linting rules,
  and formatting practices.

### Tier 3: Task-Specific Instructions
Task-oriented workflows (e.g. `/backlog-item`, `/refresh-guidance`, `/standup`)
live in task skill definitions (e.g. `claude/commands/`, `opencode/skills/`,
`pi/prompts/`) and are invoked only when needed, keeping them out of the agent's
permanent prompt budget.

---

## 2. Directory Hazard Signposting Standards

A signpost is a concise warning and pointer, not a code recap.

### What Good Looks Like
- **Identifies the boundary**: Explains the component's role and interfaces.
- **Names the hazard**: Highlights non-obvious failure modes that an agent
  cannot deduce from superficial inspection (e.g. "every test runs under a
  sandboxed HOME", "untracked node_modules requires bootstrap script").
- **Points to the source of truth**: Mentions the file or test suite that
  enforces the invariant (e.g. "Read `test/AGENTS.md` before writing a test").

### Anti-Patterns to Avoid
- **Recapping implementation code**: Do not list functions, classes, or
  constants that an agent can read directly with grep or view tools.
- **Duplicating root instructions**: Do not re-state git branching or global
  commit rules in directory docs.
- **Unsignposted subtrees**: Having a child `AGENTS.md` that is never cited or
  linked from the root `AGENTS.md`, leaving agents unaware of its existence
  unless their working directory happens to be inside it.

---

## 3. Cross-Harness Symlink Pairing Standard

Different AI coding harnesses look for distinct instruction filenames:
- Claude Code reads `CLAUDE.md` and ignores `AGENTS.md`.
- OpenCode reads `AGENTS.md` and ignores `CLAUDE.md`.
- Pi prefers `AGENTS.md`, falling back to `CLAUDE.md`.
- Copilot reads either.

To guarantee identical guidance across all harnesses without file drift:
1. `AGENTS.md` is the canonical source file in every instruction directory.
2. `CLAUDE.md` is a **relative symlink** pointing directly to `AGENTS.md`
   (`os.symlink("AGENTS.md", "CLAUDE.md")`, git mode `120000`).
3. Never commit a regular file as `CLAUDE.md` in any instruction directory.
4. The pairing requirement applies to the root directory as well as all
   subdirectories containing `AGENTS.md`.

---

## 4. Standard Directory Template

When creating a new directory `AGENTS.md` (via `refresh_guidance.py scaffold <dir>`
or manually), adhere to this standard layout:

```markdown
# AGENTS.md — {dir_name}

<!-- Paired with CLAUDE.md symlink in the same directory -->

## Responsibilities & Boundary

<!-- What this directory owns, key boundaries, and architectural roles -->

## Hazards & Signposts

<!-- Directory-specific hazards, non-obvious traps, gotchas, and prerequisite reads -->

## Local Conventions

<!-- Local conventions, commands, and verification rules for this subtree -->
```

---

## 5. Evaluation & Audit Rules

`refresh_guidance.py check` mechanically audits documentation against this
rubric:

1. **Root File**: `AGENTS.md` must exist at the repository root. Missing root
   instructions is flagged as a `signpost` finding.
2. **Symlink Pairing**: Every discovered `AGENTS.md` must have a sibling
   `CLAUDE.md` that is a symlink pointing directly to `"AGENTS.md"`. Missing,
   regular, or mis-targeted files are flagged as `symlink` findings.
3. **Root Signposting**: Every non-root `AGENTS.md` must be cited in root
   `AGENTS.md` by its directory name (e.g. `` `{dir}/` ``) or markdown link.
   Un-signposted directories are flagged as `signpost` findings.
4. **Context Budget**: Root `AGENTS.md` content lines (excluding blank lines and
   `<!-- ... -->` comments) must not exceed 150 lines. Over-budget files are
   flagged as `budget` findings.
