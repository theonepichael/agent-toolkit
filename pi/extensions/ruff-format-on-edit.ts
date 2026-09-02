import { existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { spawn } from "node:child_process";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

// Confirmed against docs/extensions.md's Tool Events section (not from
// prose alone): tool_execution_start carries `{ toolCallId, toolName, args
// }` and tool_execution_end carries `{ toolCallId, toolName, result,
// isError }` — the write/edit tools' target path is only available on
// _start (as `args.path`), never on _end, so the path has to be stashed
// keyed by toolCallId and looked up when the matching _end fires. This
// differs from opencode's tool.execute.after, which carries args directly.
export function findPyprojectRoot(startDir: string): string | null {
  let dir = startDir;
  while (true) {
    if (existsSync(join(dir, "pyproject.toml"))) return dir;
    const parent = dirname(dir);
    if (parent === dir) return null;
    dir = parent;
  }
}

function run(cmd: string, args: string[], cwd: string): Promise<void> {
  return new Promise((resolve) => {
    const child = spawn(cmd, args, { cwd, stdio: "ignore" });
    child.on("error", () => resolve());
    child.on("exit", () => resolve());
  });
}

export default function (pi: ExtensionAPI) {
  const pendingPaths = new Map<string, string>();

  pi.on("tool_execution_start", async (event) => {
    if (event.toolName !== "write" && event.toolName !== "edit") return;
    const path = (event.args as { path?: string } | undefined)?.path;
    if (typeof path === "string") {
      pendingPaths.set(event.toolCallId, path);
    }
  });

  pi.on("tool_execution_end", async (event) => {
    const path = pendingPaths.get(event.toolCallId);
    pendingPaths.delete(event.toolCallId);
    if (!path || event.isError) return;
    if (!path.endsWith(".py")) return;

    const root = findPyprojectRoot(dirname(path));
    if (!root) return;

    // Best-effort — never block the agent loop on a lint failure. run()
    // never rejects (errors resolve like a normal exit), matching that.
    await run("uv", ["run", "ruff", "format", path], root);
    await run("uv", ["run", "ruff", "check", "--fix", path], root);
  });
}
