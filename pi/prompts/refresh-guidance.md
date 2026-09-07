---
description: "Audit this repo's hand-authored, agent-facing docs (AGENTS.md, README.md, STYLE.md, CHANGELOG.md, etc.) for mechanically-broken citations — dead file paths, dead command/flag references — and surface which `##` sections haven't had a human-confirmed review in a while. Use when the user says 'refresh guidance', 'audit the docs', 'check the docs for staleness', 'run refresh-guidance', or asks which doc sections need review."
---

Manual, periodic audit — never run this unprompted or on a schedule, and
never as a side effect of an unrelated task.

1. Determine which repo to audit. If the user names one, use it. Otherwise
   resolve the current repo root with `git rev-parse --show-toplevel`.

2. Run, with no `--doc-set` flag:
   ```
   python3 ~/.claude/scripts/refresh_guidance.py check --repo-root <repo-root>
   ```
   The tool auto-discovers `<repo-root>/refresh-guidance.toml` when the
   target repo carries one (its own doc-set config, owned by that repo —
   agent-toolkit hardcodes no other repo's layout). Two outcomes:
   - It runs normally — proceed to step 3.
   - It exits 2 with `no doc-set specified: pass --doc-set agent-toolkit,
     or add <repo-root>/refresh-guidance.toml`. If `<repo-root>` actually
     is the agent-toolkit checkout, retry once with `--doc-set
     agent-toolkit` appended. Otherwise the target repo has no
     `refresh-guidance.toml` yet (as of 2026-09, this is true for
     dotfiles — tracked as a separate, not-yet-landed backlog item to add
     one there) — relay this message to the user as an actionable "this
     repo isn't configured for refresh-guidance yet," not a tool failure,
     and stop.

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
   python3 ~/.claude/scripts/refresh_guidance.py mark-reviewed "<doc>" "<heading>" --repo-root <repo-root> [--doc-set agent-toolkit]
   ```
   Same resolution as step 2: omit `--doc-set` when the repo has its own
   `refresh-guidance.toml`, pass `--doc-set agent-toolkit` only when
   auditing agent-toolkit itself.
   A clean `check` finding nothing broken is not the same as a human
   confirming the prose is current — don't conflate the two, and don't
   mark a section reviewed just because its findings are clean.
