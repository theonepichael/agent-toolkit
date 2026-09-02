import { homedir } from "node:os";
import { join } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const NOTIFY_SCRIPT = join(homedir(), ".claude", "scripts", "notify.py");

export function buildNotifyArgs(options: {
  title?: string;
  message?: string;
  harness?: string;
  type?: "completed" | "waiting_for_input" | "error";
}): string[] {
  const args = ["python3", NOTIFY_SCRIPT];
  if (options.title) {
    args.push("--title", options.title);
  }
  if (options.message) {
    args.push("--message", options.message);
  }
  if (options.harness) {
    args.push("--harness", options.harness);
  }
  if (options.type) {
    args.push("--type", options.type);
  }
  return args;
}

export default function notifyExtension(pi: ExtensionAPI): void {
  pi.on("agent_settled", async (_event, ctx) => {
    const args = buildNotifyArgs({
      harness: "Pi",
      title: "Pi Agent",
      message: "Agent run completed",
      type: "completed",
    });
    try {
      await pi.exec(args[0]!, args.slice(1), { cwd: ctx.cwd });
    } catch {
      // Best-effort notification, never block the session
    }
  });

  pi.on("ui_prompt_start", async (event, ctx) => {
    const promptTitle = (event as { title?: string }).title;
    const msg = promptTitle ? `Waiting for input: ${promptTitle}` : "Waiting for input";
    const args = buildNotifyArgs({
      harness: "Pi",
      title: "Pi Agent",
      message: msg,
      type: "waiting_for_input",
    });
    try {
      await pi.exec(args[0]!, args.slice(1), { cwd: ctx.cwd });
    } catch {
      // Best-effort notification
    }
  });
}
