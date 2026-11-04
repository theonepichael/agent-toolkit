# Sourced (never executed directly) by githooks-global/pre-commit. Refuses a
# commit whose staged diff adds a line containing a backlog-store slug, by
# shelling out to the same dev_status.py slug-check gate the land skill runs
# at its commit gate. Fails closed on any nonzero exit: exit 1 (un-overridden
# findings) and exit 2 (store/git/parse error -- including a missing store)
# both refuse the commit; only exit 0 (clean) lets it pass. There is no
# interactive override here (the land gate's --allow flow stays there); the
# hook-side escape is git's own `git commit --no-verify`.

refuse_slug_leak() {
    # This lib lives at <repo>/githooks-global/lib/slug-check.sh, so the
    # toolkit repo root -- and its agent-scripts/dev_status.py -- is two
    # levels up. Resolve from BASH_SOURCE (this lib's own path), not $0 (the
    # sourcing hook), so it works however the lib is reached.
    local lib_dir toolkit_root dev_status
    lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    toolkit_root="$(cd "$lib_dir/../.." && pwd)"
    dev_status="$toolkit_root/agent-scripts/dev_status.py"

    local commit_repo
    commit_repo="$(git rev-parse --show-toplevel 2>/dev/null)"
    if [ -z "$commit_repo" ]; then
        echo "pre-commit: slug-check: cannot resolve the committing repo's toplevel" >&2
        return 1
    fi

    # Capture status from a plain assignment, never `local output="$(...)"`:
    # `local` returns its own success and would mask the command's failure.
    local output status
    output="$(python3 "$dev_status" slug-check --repo "$commit_repo" 2>&1)"
    status=$?
    if [ "$status" -eq 0 ]; then
        return 0
    fi
    echo "$output" >&2
    echo "pre-commit: slug-check refused this commit (see findings above)." >&2
    echo "pre-commit: bypass only via git commit --no-verify." >&2
    return "$status"
}
