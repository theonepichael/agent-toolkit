# GitHub Copilot CLI — pre-approves the shared backlog/pending/standup tooling.
# Copilot's --allow-tool wildcard matching only works on single-word command
# stems (git, gh) per `copilot help permissions` — a per-script pattern like
# shell(python3 ~/.claude/scripts/dev_status.py:*) never matches, so this
# pre-approves python3 broadly rather than just our scripts. Tighten this if
# Copilot ships richer prefix matching (their docs say it's coming).
alias copilot-work='copilot \
  --allow-tool "shell(python3:*)" \
  --allow-tool "shell(git status)" --allow-tool "shell(git log:*)" \
  --allow-tool "shell(git diff:*)"'
