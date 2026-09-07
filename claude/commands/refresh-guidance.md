---
name: refresh-guidance
description: "Audit this repo's hand-authored, agent-facing docs (AGENTS.md, README.md, STYLE.md, CHANGELOG.md, etc.) for mechanically-broken citations — dead file paths, dead command/flag references — and surface which `##` sections haven't had a human-confirmed review in a while. Use when the user says 'refresh guidance', 'audit the docs', 'check the docs for staleness', 'run refresh-guidance', or asks which doc sections need review."
allowed-tools: [Read, "Bash(python3 ~/.claude/scripts/refresh_guidance.py:*)", "Bash(git rev-parse:*)"]
---

Manual, periodic audit — never run this unprompted or on a schedule, and
never as a side effect of an unrelated task.

1. Determine which repo to audit. If the user names one, use it. Otherwise
   resolve the current repo root with `git rev-parse --show-toplevel` and
   map it to a doc-set: `agent-toolkit` if the root is named `agent-toolkit`
   (or has an `agent-scripts/` directory), `dotfiles` if it's the dotfiles
   checkout (or has `dotfiles/claude/scripts/gen_interfaces.py`). Neither match? Tell
   the user this tool only has config for `agent-toolkit`/`dotfiles` and
   stop — don't guess a doc-set.

2. Run:
   ```
   python3 ~/.claude/scripts/refresh_guidance.py check --repo-root <repo-root> --doc-set <agent-toolkit|dotfiles>
   ```

3. Show the full findings list verbatim — doc:line, the cited text, and why
   it's broken. Every finding is a mechanical fact (a path or flag that does
   not exist), not a judgment call, so don't editorialize, filter, or
   summarize them away.

4. Summarize the staleness table oldest-first per doc rather than dumping
   all of it. Call out a section with no git-history fallback distinctly
   from an old-but-reviewed one — no fallback means this is its first-ever
   run, not that it's unusually stale.

5. Show the "Undocumented directories" list too — the inverse signal: a
   code-holding directory with no `AGENTS.md` at all. This is a suggestion,
   not a finding — never create the file yourself unless the user asks.

6. For each finding, let the user decide what to do — fix it now, offer a
   backlog item (per the backlog proactive-capture protocol), or ignore it.
   Never edit a doc to fix a finding without being asked.

7. Never run `mark-reviewed` on your own initiative. Only run it when the
   user explicitly confirms they just read a specific section and it's
   still accurate:
   ```
   python3 ~/.claude/scripts/refresh_guidance.py mark-reviewed "<doc>" "<heading>" --repo-root <repo-root> --doc-set <name>
   ```
   A clean `check` finding nothing broken is not the same as a human
   confirming the prose is current — don't conflate the two, and don't
   mark a section reviewed just because its findings are clean.
