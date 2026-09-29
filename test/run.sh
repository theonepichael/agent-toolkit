#!/usr/bin/env bash
# Builds the install.sh test images (one per distro, to cover both the apt
# and dnf branches) and runs the scenario suite against a throwaway
# container for each. Mounts the repo read-only so nothing here can touch
# the real machine or the checked-out working tree. Needs network access at
# run time: section 15 installs an unrelated distro package to prove
# --depart leaves it alone.
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"

# distro:dockerfile pairs
DISTROS=(
  "ubuntu:$HERE/Dockerfile"
  "fedora:$HERE/Dockerfile.fedora"
)

OVERALL=0
for entry in "${DISTROS[@]}"; do
  distro="${entry%%:*}"
  dockerfile="${entry#*:}"
  IMAGE="agent-toolkit-install-test-$distro"

  echo "==> Building test image ($distro)..."
  if ! docker build -q -t "$IMAGE" -f "$dockerfile" "$HERE" >/dev/null; then
    echo "==> $distro: image build failed"
    OVERALL=1
    continue
  fi

  echo "==> Running scenario suite ($distro)..."
  # :ro,Z relabels the bind mount for SELinux (rootless podman on an
  # enforcing host otherwise rejects the mount with a bare "Permission
  # denied" inside the container, even for root — no AVC denial logged, so
  # this is easy to misdiagnose as a UID/GID mapping issue instead). Safe
  # under plain Docker too — the SELinux label flag is a documented bind-mount
  # option there as well, just a no-op without SELinux enforcing.
  #
  # The copy lands at ~/agent-toolkit, never ~/dotfiles: install.py refuses
  # to link CORE_INSTRUCTIONS.md destinations when a ~/dotfiles directory
  # exists (that repo composes those files itself). The copy's .git is then
  # replaced with a one-commit snapshot repo: a checkout run from a git
  # worktree carries a .git *file* pointing at a host path that does not
  # exist in the container, and every git call from inside the copy —
  # including install.py's `git config --global` — then dies with "not a
  # git repository".
  if ! docker run --rm \
    -v "$REPO:/agent-toolkit:ro,Z" \
    -w /home/tester \
    "$IMAGE" \
    bash -c 'set -e
      cp -r /agent-toolkit /home/tester/agent-toolkit
      cd /home/tester/agent-toolkit
      rm -rf .git
      git -c init.defaultBranch=main init -q
      git add -A
      git -c user.name=scenario -c user.email=scenario@localhost commit -q -m snapshot
      bash /home/tester/agent-toolkit/test/scenarios.sh'; then
    echo "==> $distro: scenario suite failed"
    OVERALL=1
  fi
done

exit $OVERALL
