import path from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { isToolCallEventType } from "@earendil-works/pi-coding-agent";

let enabled = true;

/**
 * True when no human is watching this session -- a swarm worker in its own
 * herdr tab, spawned with `--env PI_AGENT_UNATTENDED=1`.
 *
 * This does NOT disable guard-rails. It changes what the two interactive
 * confirmations do, and nothing else: `rm -rf` and `sudo` are BLOCKED instead
 * of asked about. Every other rule here -- protected-path writes, the
 * git-commit-on-main worktree policy, the shared guard verdicts -- stays
 * armed exactly as in an attended session.
 *
 * Blocking is what an unanswerable question should resolve to. Before this,
 * a worker reaching either dialog waited on a human who had not been told to
 * look, while herdr still reported it as working; the run stopped making
 * progress with no signal (observed live, 2026-09-02). A refusal the agent
 * can read is both safer and more useful than a dialog nobody answers. It is
 * the same conclusion the `!ctx.hasUI` branches below already reach, for the
 * same reason.
 *
 * Read from the environment here rather than imported from permission-gate.ts:
 * pi loads each extension separately, and a value shared across extension
 * files does not reliably reach the loaded instance -- proven live on
 * 2026-09-02, when /trust-session called both gates' exported setters and
 * neither loaded gate observed the change.
 */
export function isUnattended(): boolean {
  return process.env.PI_AGENT_UNATTENDED === "1";
}

/** Read-only view of the gate, so a caller (or a test) can confirm its state. */
export function isGuardRailsEnabled(): boolean {
  return enabled;
}

// No exported setter on purpose. One used to live here ("Exported so
// trust-session.ts can flip this gate") -- but pi evaluates every extension
// in its own jiti registry, so trust-session's imported setter ran against a
// private copy and the loaded gate never saw the flip: /trust-session
// reported success while changing nothing (proven live 2026-09-02).
// Cross-extension toggling now arrives over the shared event bus below.

export function isDangerousRm(command: string): boolean {
  const rmMatches = command.matchAll(/\brm\s+([^;&|`$()]+)/gi);
  for (const match of rmMatches) {
    const args = match[1] ?? "";
    // Leading space so a `--` in first position splits like any other one:
    // everything after `--` is a file name, so "rm -- -rf" has no flags at all.
    const flagSection = ` ${args}`.split(/\s--\s/)[0] ?? args;
    const hasR =
      /(?:^|\s)-[a-zA-Z]*r/i.test(flagSection) || /(?:^|\s)--recursive\b/i.test(flagSection);
    const hasF = /(?:^|\s)-[a-zA-Z]*f/i.test(flagSection) || /(?:^|\s)--force\b/i.test(flagSection);
    if (hasR && hasF) return true;
  }
  return false;
}

export function isProtectedPath(filePath: string): boolean {
  const normalized = filePath.replace(/\\/g, "/");
  const segments = normalized.split("/").filter(Boolean);
  if (segments.length === 0) return false;
  const basename = segments[segments.length - 1]!;
  if (basename === ".env" || basename.startsWith(".env.")) return true;
  if (segments.includes(".git") || segments.includes("node_modules")) return true;
  return false;
}

export function getGitCommitTarget(
  command: string,
  defaultCwd: string,
): { isCommit: boolean; cwd: string } {
  const subcommands = command.split(/[;&|\n]+/);
  // Tracks `cd` between subcommands: `cd <worktree> && git commit` runs the
  // commit in the worktree, not in the session cwd.
  let shellCwd = defaultCwd;
  for (const sub of subcommands) {
    const trimmed = sub.trim();

    const cd = trimmed.match(/^cd\s+(.+)$/i);
    if (cd) {
      const dir = (cd[1] ?? "").trim().replace(/^["']|["']$/g, "");
      // `cd -` (and a bare `cd`, which never matches here) has no target we
      // can resolve statically — leave the tracked cwd as it is.
      if (dir && dir !== "-") {
        shellCwd = path.isAbsolute(dir) ? dir : path.resolve(shellCwd, dir);
      }
      continue;
    }

    const match = trimmed.match(/\bgit(?:\.exe)?\b\s+(.*)/i);
    if (!match) continue;

    const rest = match[1] ?? "";
    const words = rest.split(/\s+/);
    let targetCwd = shellCwd;
    let isCommit = false;

    for (let i = 0; i < words.length; i++) {
      const w = words[i]!;
      if (w === "-C" && i + 1 < words.length) {
        const dir = words[i + 1]!.replace(/^["']|["']$/g, "");
        targetCwd = path.isAbsolute(dir) ? dir : path.resolve(shellCwd, dir);
        i++;
      } else if (w.startsWith("-C")) {
        const dir = w.slice(2).replace(/^["']|["']$/g, "");
        targetCwd = path.isAbsolute(dir) ? dir : path.resolve(shellCwd, dir);
      } else if (w.startsWith("-")) {
        continue;
      } else {
        if (w.toLowerCase() === "commit") {
          isCommit = true;
        }
        break;
      }
    }

    if (isCommit) {
      return { isCommit: true, cwd: targetCwd };
    }
  }
  return { isCommit: false, cwd: defaultCwd };
}

async function currentGitBranch(pi: ExtensionAPI, cwd: string): Promise<string | null> {
  try {
    const result = await pi.exec("git", ["branch", "--show-current"], { cwd, timeout: 2000 });
    if (result.code === 0) {
      const branch = result.stdout.trim();
      return branch || null;
    }
  } catch {
    // Not a git repo or git not available
  }
  return null;
}

// R2/R3 (main-checkout writes, stale worktree base) and the bash-family
// core.hooksPath-override companion check live in claude/scripts/
// guard_rails.py so all five harnesses share one source of truth. The rm -rf
// / sudo / protected-path / git-commit rules above stay here: they need
// ctx.ui.confirm(), which a subprocess cannot do. The git-commit-on-main
// rule above is the same proven-bypassable shell-string parser this item
// replaced with a real git hook for every other harness -- it stays here,
// unmodified, as a non-authoritative accidental-catch layer (see
// meta-git-commit-main-guard-mechanism's spec: no anchoring choice avoids
// trading a bypass for a false positive, so it isn't worth trying to fix).
// The companion check's own known gaps (git commit -n not checked, no full
// shell-evaluation of .git/config writes) are documented on
// evaluate_bash_override's docstring in guard_rails.py.
type SharedVerdict = { decision: "allow" | "deny" | "warn"; reason?: string };

async function sharedGuard(
  pi: ExtensionAPI,
  tool: string,
  filePath: string,
): Promise<SharedVerdict | null> {
  try {
    const result = await pi.exec(
      "python3",
      [
        `${process.env.HOME}/.claude/scripts/guard_rails.py`,
        "--tool",
        tool,
        "--cwd",
        path.dirname(filePath),
        "--path",
        filePath,
      ],
      { timeout: 5000 },
    );
    if (result.code !== 0) return null;
    return JSON.parse(result.stdout) as SharedVerdict;
  } catch {
    // Fail open: a guard that cannot answer must not block the loop.
    return null;
  }
}

// Bash-family companion check (core.hooksPath override/redirect, --no-verify,
// direct .git/config writes) -- see claude/scripts/guard_rails.py's module
// docstring for the full rule. Only fires on a protected branch; everywhere
// else guard_rails.py itself allows without even needing the git-commit
// blocking logic above, since the two checks are independent.
async function sharedBashGuard(
  pi: ExtensionAPI,
  cwd: string,
  command: string,
): Promise<SharedVerdict | null> {
  try {
    const result = await pi.exec(
      "python3",
      [
        `${process.env.HOME}/.claude/scripts/guard_rails.py`,
        "--tool",
        "bash",
        "--cwd",
        cwd,
        "--command",
        command,
      ],
      { timeout: 5000 },
    );
    if (result.code !== 0) return null;
    return JSON.parse(result.stdout) as SharedVerdict;
  } catch {
    // Fail open: a guard that cannot answer must not block the loop.
    return null;
  }
}

export default function (pi: ExtensionAPI) {
  // /trust-session and /trust-session-off broadcast here (see their comment
  // for why the channel name is a duplicated literal rather than an import).
  // The listener mutates the same module-level `enabled` the tool_call
  // handler reads. Malformed payloads are ignored: a wrong shape must never
  // silently disarm (or spuriously re-arm) the gate.
  pi.events.on("session-trust-changed", (data) => {
    const trusted = (data as { trusted?: unknown } | null | undefined)?.trusted;
    if (typeof trusted === "boolean") enabled = !trusted;
  });

  pi.on("tool_call", async (event, ctx) => {
    if (!enabled) return undefined;

    // Bash guard rails
    if (isToolCallEventType("bash", event)) {
      const command = event.input.command;

      // rm -rf confirmation gate
      if (isDangerousRm(command)) {
        if (!ctx.hasUI || isUnattended()) {
          return {
            block: true,
            reason: isUnattended()
              ? "rm -rf blocked: no human is watching this session to confirm it"
              : "rm -rf blocked (no UI for confirmation in headless mode)",
          };
        }
        const ok = await ctx.ui.confirm("⚠️ rm -rf", `Allow destructive command?\n\n  ${command}`);
        if (!ok) {
          return { block: true, reason: "rm -rf blocked by user" };
        }
      }

      // sudo confirmation gate
      if (/\bsudo\b/i.test(command)) {
        if (!ctx.hasUI || isUnattended()) {
          return {
            block: true,
            reason: isUnattended()
              ? "sudo blocked: no human is watching this session to confirm it"
              : "sudo blocked (no UI for confirmation in headless mode)",
          };
        }
        const ok = await ctx.ui.confirm("⚠️ sudo", `Allow privileged command?\n\n  ${command}`);
        if (!ok) {
          return { block: true, reason: "sudo blocked by user" };
        }
      }

      // git commit on main/master — worktree policy
      const gitCommit = getGitCommitTarget(command, ctx.cwd);
      if (gitCommit.isCommit) {
        const branch = await currentGitBranch(pi, gitCommit.cwd);
        if (branch === "main" || branch === "master") {
          return {
            block: true,
            reason: `git commit blocked on '${branch}' branch. House policy: never commit directly to main. Use a worktree or create a feature branch.`,
          };
        }
      }

      const bashVerdict = await sharedBashGuard(pi, ctx.cwd, command);
      if (bashVerdict?.decision === "deny") {
        return { block: true, reason: bashVerdict.reason ?? "Blocked by guard-rails" };
      }

      return undefined;
    }

    // Write/edit guard rails
    if (isToolCallEventType("write", event) || isToolCallEventType("edit", event)) {
      const filePath = event.input.path as string;
      if (isProtectedPath(filePath)) {
        if (ctx.hasUI) {
          ctx.ui.notify(`Blocked write to protected path: ${filePath}`, "warning");
        }
        return { block: true, reason: `Path "${filePath}" is protected by guard-rails` };
      }

      const verdict = await sharedGuard(pi, "write", filePath);
      if (verdict?.decision === "deny") {
        return { block: true, reason: verdict.reason ?? "Blocked by guard-rails" };
      }
      if (verdict?.decision === "warn" && ctx.hasUI && verdict.reason) {
        ctx.ui.notify(verdict.reason, "warning");
      }
    }

    return undefined;
  });

  pi.registerCommand("guard-rails-disable", {
    description: "Disable guard-rails for this session",
    handler: async (_args, ctx) => {
      enabled = false;
      ctx.ui.notify("Guard rails disabled", "warning");
    },
  });

  pi.registerCommand("guard-rails-enable", {
    description: "Re-enable guard-rails",
    handler: async (_args, ctx) => {
      enabled = true;
      ctx.ui.notify("Guard rails enabled", "info");
    },
  });
}
