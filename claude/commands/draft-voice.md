---
name: draft-voice
description: "Apply the user's own voice and formatting rules when drafting an outgoing informal peer message on their behalf — a Teams reply, a Slack-style ping, a PR comment to a teammate. Use when asked to 'draft a reply', 'write a Teams message', 'draft a Slack message to my teammate', 'write a PR comment', 'respond to this PR comment', 'draft a message to my coworker', or similar. Do not use for formal drafts (email to a director, a written PR description, a public README) — ask before applying these rules there."
---
Informal peer chat only. Anything more formal — email to a director, a
written PR description, a public README — ask before applying this.

1. Apply to the draft only, not your own conversational replies.

2. Keep it short: 1-3 sentences unless the topic is genuinely technical.
   Don't summarize the whole thread back to the recipient.

3. If the user's told you why they did something (an issue author pointed
   them at a branch, a prior decision, etc.), cite it in half a sentence —
   e.g. "alex flagged this in #45, so..." Don't over-justify a solo call.

4. Mechanics:
   - No dashes at all: no em-dashes, en-dashes, or ASCII hyphens as
     sentence-connectors. Break into two sentences or use a comma instead.
   - Lowercased. Sentence-initial lowercase is the default. Proper nouns
     stay capitalized where dropping the cap would read wrong (real names,
     product names).
   - Drop softeners, filler, and throat-clearing openers: no "I was just
     thinking maybe we could...", no "actually/essentially/basically", no
     "Yeah, so...". "fyi", "lmk", "ah ok", "sounds good" are fine.
   - Plain verbs, no cute phrasing. If a metaphor would need explaining, or
     it reads like something you generated rather than a phrase the user
     would naturally use, cut it.
   - Structure only when it earns its keep. Bullets are fine for 2+
     genuinely parallel items. Don't bullet a single point, don't use
     headers in a chat message.

   Example: "hey alex, oncall paged me so i merged the hotfix straight
   through. happy to walk you through it now if you want to check it."

5. This skill drafts, it doesn't send — hand the draft back for the user to
   review and send themselves.
