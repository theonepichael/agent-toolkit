---
description: "Gather assigned work, chat signal, calendar events, pending replies, git commits, and backlog activity into a daily standup draft, saved to a dated file. Use when the user says 'standup', 'prep for standup', or wants their daily status pulled together."
---

Read-only against every external system. Never comments on a ticket, posts a
message, or marks anything read — the only write is the standup file itself.
No autonomous scanning: this only runs when invoked.

## 1. Fetch

Call the `standup` tool with action `fetch` — never `standup.py` via bash.

Its optional `date` field (`YYYY-MM-DD`) overrides the reference date
(defaults to today) — use it after a gap longer than one working day
(holiday, PTO) where the default last-working-day boundary would land on the
wrong day.

Returns one JSON object: `date`, `since` (the computed time boundary every
windowed source used), `git_commits`, `backlog_in_progress`,
`backlog_recent_done`, `backlog_in_review`, `assigned_items`, `messages`,
`chat_thread_updates`, `email_correspondence`, `email_thread_updates`, `calendar_events`
(yesterday's and today's, non-recurring only), `pending_items_open`,
`previous_standup` (most recent saved standup file before `date`, or
`null`), `skipped`. `chat_thread_updates`/`email_thread_updates` are
targeted fetches against threads already in `pending_items_open` — don't
rely on `messages`/`email_correspondence` alone to catch a reply to a
tracked item.

`pending_items_open` is a read-only view of `dev_status.py`'s canonical
pending-items store — `standup.py` never mutates it; all pending-item writes
below go through `dev_status.py`.

`skipped` lists every source that couldn't run (adapter not yet configured,
no git repos set up, `work_backlog_prefixes` missing, etc.) with a reason.
Tell the user what was skipped and why — don't silently produce a partial
standup as if it were complete. See `~/dotfiles/claude/scripts/standup_adapters.py`
for wiring in a real adapter once a source is consistently skipped because
the platform is now known.

## 2. Reconcile pending items

Status moves one step at a time: `waiting_for_reply` → `reply_received` →
`resolved`. A reply landing doesn't mean the thing is closed out — it means
it needs a look. Don't jump straight to `resolved` on a hunch.

For each entry in `pending_items_open`, check `chat_thread_updates` /
`email_thread_updates` (and `messages`/`email_correspondence` for anything
those targeted fetches missed) for a reply. Propose the transition to the
user in chat first — nothing gets written until they confirm, since "was
this actually answered" is a judgment call, not a pattern match — then call
the `dev_status` tool with `action: "pending_update", slug: "<id>", patch:
{"status": "reply_received"}`.

Only move an item to `resolved` when the user confirms it's actually done,
and record what happened: `action: "pending_update", slug: "<id>", patch:
{"status": "resolved", "outcome": "what actually happened"}`.

For anything in the fetched data that looks like a new item worth tracking
across days (an email/chat message still awaiting a reply, an access
request not yet approved) but isn't already in `pending_items_open`,
propose adding it: `action: "pending_add", patch: {"id", "description",
"kind", "source_ref": {...}, "context", "next_steps": [...]}`.

`<id>` must be the pending item's real slug — `standup.py`'s `fetch` output
already gives you it directly. The `dev_status` tool refuses a numeric
`slug` on `pending_update` outright (call it with `action: "show", slug:
"<N>"` first if you only have a cross-section number, e.g. from the
dashboard).

If the `dev_status` tool is genuinely unavailable, fall back to bash —
`python3 ~/.claude/scripts/dev_status.py pending update <id> '{...}'` /
`pending add '{...}'`, same JSON shapes as above.

`kind` is one of `email`, `chat`, `approval`. `source_ref` is a structured
object appropriate to the kind (e.g. `{"to", "subject", "sent_date"}` for
email) — not a free-text string.

## 3. Draft

Write the standup update from the reconciled data. Ground "what I did
yesterday" in `previous_standup`'s content when present — it's what was
actually committed to, not a re-derivation from raw signal — supplemented
by `git_commits`/`backlog_recent_done`. Cover what's in progress
(`backlog_in_progress`, `assigned_items`), what's awaiting review
(`backlog_in_review`), anything noteworthy on the
calendar (`calendar_events` — yesterday's and today's, already filtered to
non-recurring), and anything still blocked (`pending_items_open`,
post-reconciliation). Skip a section entirely if its source had nothing or
was skipped — don't pad the draft to look complete.

## 4. Save and show

Write the draft to `~/.claude/data/standup/YYYY-MM-DD.md` (today's date).
Show the user the draft and the `skipped` list from step 1.
