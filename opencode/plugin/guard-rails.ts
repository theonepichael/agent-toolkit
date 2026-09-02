import { execFile } from "node:child_process"
import { dirname } from "node:path"
import { promisify } from "node:util"
import type { Plugin } from "@opencode-ai/plugin"

const run = promisify(execFile)

// The verdict logic lives in claude/scripts/guard_rails.py so all five
// harnesses share one source of truth; this plugin only adapts opencode's
// shape to it. Note the asymmetry with the other harnesses: tool.execute.before
// can throw or return, with nothing in between, so opencode has no channel for
// a "warn" verdict. R3 (stale worktree base) is therefore not surfaced here --
// see opencode/CLAUDE_CODE_PARITY.md. A warn degrades to a silent allow rather
// than to a thrown Error.
type Verdict = { decision: "allow" | "deny" | "warn"; reason?: string }

export const GuardRails: Plugin = async () => {
  return {
    "tool.execute.before": async (input: { tool?: string }, output: { args?: Record<string, unknown> }) => {
      const tool = input?.tool

      if (tool === "bash") {
        // No cwd field in this payload (confirmed live 2026-09-01, same
        // probe technique as the write-family payload below) -- opencode's
        // plugin runs in-process, so process.cwd() is the session's cwd.
        const command = output?.args?.command
        if (typeof command !== "string" || !command) return

        let bashVerdict: Verdict
        try {
          const { stdout } = await run(
            "python3",
            [
              `${process.env.HOME}/.claude/scripts/guard_rails.py`,
              "--tool", "bash",
              "--cwd", process.cwd(),
              "--command", command,
            ],
            { timeout: 5000 },
          )
          bashVerdict = JSON.parse(stdout)
        } catch {
          // Fail open: a guard that cannot answer must not block the loop.
          return
        }

        if (bashVerdict.decision === "deny") {
          throw new Error(bashVerdict.reason || "Blocked by guard-rails")
        }
        return
      }

      if (tool !== "edit" && tool !== "write") return
      const filePath = output?.args?.filePath
      if (typeof filePath !== "string" || !filePath) return

      let verdict: Verdict
      try {
        const { stdout } = await run(
          "python3",
          [
            `${process.env.HOME}/.claude/scripts/guard_rails.py`,
            "--tool", tool,
            "--cwd", dirname(filePath),
            "--path", filePath,
          ],
          { timeout: 5000 },
        )
        verdict = JSON.parse(stdout)
      } catch {
        // Fail open: a guard that cannot answer must not block the loop.
        return
      }

      if (verdict.decision === "deny") {
        throw new Error(verdict.reason || "Blocked by guard-rails")
      }
    },
  }
}

export default GuardRails
