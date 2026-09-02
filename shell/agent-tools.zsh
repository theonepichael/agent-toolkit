# shell/agent-tools.zsh
# Agent harness tools — completions, paths, and harness aliases.
# Sourced by ~/.zshrc or standalone environments using agent-toolkit.

# ============================================================================
# COMPLETIONS
# ============================================================================

if [[ -d "$HOME/.zsh/completions" && ":$fpath:" != *":$HOME/.zsh/completions:"* ]]; then
    fpath=("$HOME/.zsh/completions" $fpath)
fi

# ============================================================================
# HARNESS PATHS
# ============================================================================

for pathdir in "$HOME/.local/bin" "$HOME/.opencode/bin" "$HOME/.bun/bin"; do
    if [[ -d "$pathdir" && ":$PATH:" != *":$pathdir:"* ]]; then
        PATH="$pathdir:$PATH"
    fi
done
export PATH

# ============================================================================
# HARNESS ALIASES
# ============================================================================

# Copilot-specific aliases — only present when the copilot harness was
# selected during install.sh (symlinked to ~/.copilot_aliases); absent
# (and silently skipped) otherwise.
[[ -f "$HOME/.copilot_aliases" ]] && source "$HOME/.copilot_aliases"
