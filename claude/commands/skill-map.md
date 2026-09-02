---
name: skill-map
description: Shows how the dotfiles skills connect and flags any skill mentioned by another that no longer exists. Use when the user says "skill map", "show the skill map", "which skill for X", or asks how the skills chain together.
---
1. Run `python3 ~/.claude/scripts/gen_interfaces.py --check`. If it fails, stop and tell the user to regenerate `INTERFACES.md` first — an out-of-date file makes the map below untrustworthy.
2. Read `~/dotfiles/INTERFACES.md`, section "## 6. Skill cross-reference graph".
3. Display that section's table verbatim, or lightly reformatted for readability. Do not add a relationship you infer yourself — the table is a ground-truth cross-reference, extracted by scanning each skill's own file for the other skills' names. It cannot drift silently: step 1's check fails the moment the table would change.
4. If the user asked "which skill for X" rather than for the whole map, match X against the skill descriptions in section 2 of the same file first, then use the table from step 2 only to explain how that skill connects to others.
