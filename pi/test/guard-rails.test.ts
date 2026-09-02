import { describe, expect, test } from "bun:test";
import { getGitCommitTarget, isDangerousRm, isProtectedPath } from "../extensions/guard-rails";

describe("isDangerousRm", () => {
  test("flags rm with both recursive and force (combined short flags)", () => {
    expect(isDangerousRm("rm -rf /")).toBe(true);
    expect(isDangerousRm("rm -fr ./build")).toBe(true);
  });

  test("flags separate short flags", () => {
    expect(isDangerousRm("rm -r -f ./build")).toBe(true);
    expect(isDangerousRm("rm -f -r ./build")).toBe(true);
  });

  test("flags long flags", () => {
    expect(isDangerousRm("rm --recursive --force ./build")).toBe(true);
    expect(isDangerousRm("rm --force --recursive ./build")).toBe(true);
  });

  test("single dangerous dimension is not flagged", () => {
    expect(isDangerousRm("rm -r ./build")).toBe(false);
    expect(isDangerousRm("rm -f file.txt")).toBe(false);
    expect(isDangerousRm("rm file.txt")).toBe(false);
  });

  test("unrelated commands are not flagged", () => {
    expect(isDangerousRm("ls -rf")).toBe(false);
  });

  test("conservative substring match: rm -rf anywhere in the command string flags, even quoted", () => {
    // Known limitation, documented: the matcher has no command-position
    // awareness, so a mention inside a git commit message is flagged too —
    // blocking a safe command (false positive) beats letting a dangerous
    // one through.
    expect(isDangerousRm("git commit -m 'rm -rf cleanup'")).toBe(true);
  });

  test("-- separator ends the flag section, leading or mid-command", () => {
    // Everything after -- is a file name, never a flag. "rm -- -rf" deletes a
    // file literally named "-rf" and is not recursive-force.
    expect(isDangerousRm("rm -- -rf")).toBe(false);
    expect(isDangerousRm("rm file1 -- -rf")).toBe(false);
  });

  test("flags before a -- separator still flag", () => {
    expect(isDangerousRm("rm -rf -- ./build")).toBe(true);
  });
});

describe("isProtectedPath", () => {
  test("env files are protected", () => {
    expect(isProtectedPath(".env")).toBe(true);
    expect(isProtectedPath(".env.local")).toBe(true);
    expect(isProtectedPath("config/.env.production")).toBe(true);
  });

  test(".git and node_modules segments are protected", () => {
    expect(isProtectedPath(".git/config")).toBe(true);
    expect(isProtectedPath("repo/.git/HEAD")).toBe(true);
    expect(isProtectedPath("node_modules/pkg/index.js")).toBe(true);
  });

  test("ordinary paths are not protected", () => {
    expect(isProtectedPath("src/index.ts")).toBe(false);
    expect(isProtectedPath("environment")).toBe(false);
    expect(isProtectedPath("")).toBe(false);
  });

  test("windows-style separators are normalized", () => {
    expect(isProtectedPath("repo\\.git\\config")).toBe(true);
    expect(isProtectedPath("C:\\repo\\.env")).toBe(true);
  });
});

describe("getGitCommitTarget", () => {
  test("plain commit uses the default cwd", () => {
    expect(getGitCommitTarget("git commit -m 'x'", "/repo")).toEqual({
      isCommit: true,
      cwd: "/repo",
    });
  });

  test("absolute -C overrides the default cwd", () => {
    expect(getGitCommitTarget("git -C /other commit -m 'x'", "/repo")).toEqual({
      isCommit: true,
      cwd: "/other",
    });
  });

  test("relative -C resolves against the default cwd", () => {
    expect(getGitCommitTarget("git -C sub commit", "/repo")).toEqual({
      isCommit: true,
      cwd: "/repo/sub",
    });
  });

  test("glued -C<dir> form resolves the same way", () => {
    expect(getGitCommitTarget("git -Csub commit", "/repo")).toEqual({
      isCommit: true,
      cwd: "/repo/sub",
    });
  });

  test("non-commit git commands are not a commit target", () => {
    expect(getGitCommitTarget("git status", "/repo")).toEqual({ isCommit: false, cwd: "/repo" });
    expect(getGitCommitTarget("git -C /other log", "/repo")).toEqual({
      isCommit: false,
      cwd: "/repo",
    });
  });

  test("finds the commit subcommand in a compound command", () => {
    expect(getGitCommitTarget("ls; git commit -m 'x'", "/repo")).toEqual({
      isCommit: true,
      cwd: "/repo",
    });
  });

  test("non-git commands are not a commit target", () => {
    expect(getGitCommitTarget("echo hello", "/repo")).toEqual({ isCommit: false, cwd: "/repo" });
  });

  test("a leading cd sets the commit cwd", () => {
    expect(getGitCommitTarget("cd /worktree && git commit -m 'x'", "/repo")).toEqual({
      isCommit: true,
      cwd: "/worktree",
    });
  });

  test("a relative cd resolves against the default cwd", () => {
    expect(getGitCommitTarget("cd sub && git commit", "/repo")).toEqual({
      isCommit: true,
      cwd: "/repo/sub",
    });
  });

  test("cd works with ; and newline separators too", () => {
    expect(getGitCommitTarget("cd /worktree ; git commit", "/repo")).toEqual({
      isCommit: true,
      cwd: "/worktree",
    });
    expect(getGitCommitTarget("cd /worktree\ngit commit", "/repo")).toEqual({
      isCommit: true,
      cwd: "/worktree",
    });
  });

  test("a quoted cd target is unquoted", () => {
    expect(getGitCommitTarget('cd "/my worktree" && git commit', "/repo")).toEqual({
      isCommit: true,
      cwd: "/my worktree",
    });
  });

  test("an explicit -C beats a preceding cd", () => {
    expect(getGitCommitTarget("cd /worktree && git -C /other commit", "/repo")).toEqual({
      isCommit: true,
      cwd: "/other",
    });
  });

  test("a relative -C resolves against the cd'd directory", () => {
    expect(getGitCommitTarget("cd /worktree && git -C sub commit", "/repo")).toEqual({
      isCommit: true,
      cwd: "/worktree/sub",
    });
  });

  test("chained cds accumulate", () => {
    expect(getGitCommitTarget("cd /worktree && cd sub && git commit", "/repo")).toEqual({
      isCommit: true,
      cwd: "/worktree/sub",
    });
  });

  test("a cd with no resolvable target leaves the cwd alone", () => {
    expect(getGitCommitTarget("cd && git commit", "/repo")).toEqual({
      isCommit: true,
      cwd: "/repo",
    });
    expect(getGitCommitTarget("cd - && git commit", "/repo")).toEqual({
      isCommit: true,
      cwd: "/repo",
    });
  });

  test("a cd after the commit does not affect it", () => {
    expect(getGitCommitTarget("git commit && cd /worktree", "/repo")).toEqual({
      isCommit: true,
      cwd: "/repo",
    });
  });
});
