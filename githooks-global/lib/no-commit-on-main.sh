# Sourced (never executed directly) by githooks/pre-commit and
# githooks-global/pre-commit. Refuses a commit that would move `main` or
# `master`, structurally: it reads git's own resolved branch state after the
# shell has already run, so it cannot be fooled by how the commit command
# was obfuscated (bash -c, a heredoc fed to sh, a mid-word backslash) --
# unlike a PreToolUse hook that has to parse the command string before the
# shell ever sees it. See meta-git-commit-main-guard-mechanism's spec for
# the bypasses this replaces.

refuse_if_protected_branch() {
    local branch

    # symbolic-ref, not rev-parse --abbrev-ref: on an unborn branch (a
    # brand-new repo before its first commit) rev-parse --abbrev-ref HEAD
    # prints the literal string "HEAD" and exits 128 with "fatal: ambiguous
    # argument 'HEAD'" -- indistinguishable from genuine detached HEAD.
    # symbolic-ref prints the target branch name and exits 0 on an unborn
    # branch, and fails only when HEAD really is detached.
    if ! branch="$(git symbolic-ref --short -q HEAD)"; then
        # Detached HEAD: this commit cannot move main/master's ref at all,
        # so there is no policy violation to block. Fail open.
        return 0
    fi

    case "$branch" in
        main|master) ;;
        *) return 0 ;;
    esac

    # The very first commit in a repo lands on main/master by construction
    # -- there is no other branch to make it on yet. Only refuse once the
    # branch already has history: a repo with no commits yet has no HEAD
    # commit to verify.
    if ! git rev-parse --verify -q HEAD >/dev/null 2>&1; then
        return 0
    fi

    echo "pre-commit: direct commits to '$branch' are blocked. Create a branch or worktree instead:" >&2
    echo "  git worktree add ../<repo>-<slug> -b <slug>" >&2
    return 1
}
