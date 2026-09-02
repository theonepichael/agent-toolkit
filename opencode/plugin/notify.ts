import { execFileSync } from "node:child_process"
import { homedir } from "node:os"
import { join } from "node:path"
import type { Plugin } from "@opencode-ai/plugin"

const NOTIFY_SCRIPT = join(homedir(), ".claude", "scripts", "notify.py")

export const NotifyPlugin: Plugin = async () => {
  return {
    event: async ({ event }) => {
      if (event.type === "session.idle") {
        try {
          execFileSync(
            "python3",
            [
              NOTIFY_SCRIPT,
              "--harness",
              "OpenCode",
              "--title",
              "OpenCode",
              "--message",
              "Task completed",
              "--type",
              "completed",
            ],
            {
              timeout: 5000,
              stdio: "ignore",
            }
          )
        } catch {
          // Best-effort -- never block the session on notification failure.
        }
      }
    },
  }
}

export default NotifyPlugin
