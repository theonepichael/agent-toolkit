import { existsSync } from "node:fs"
import { dirname, join } from "node:path"
import type { Plugin } from "@opencode-ai/plugin"

// Confirmed empirically (probing a diagnostic tool.execute.after hook, not
// from docs alone): the "edit" and "write" tools both put the target path
// at input.args.filePath. The doc examples for this hook show no args at
// all on the input type -- the real payload does carry them, same shape
// as tool.execute.before's documented example.
function findPyprojectRoot(startDir: string): string | null {
  let dir = startDir
  while (true) {
    if (existsSync(join(dir, "pyproject.toml"))) return dir
    const parent = dirname(dir)
    if (parent === dir) return null
    dir = parent
  }
}

export const RuffFormatOnEdit: Plugin = async ({ $ }) => {
  return {
    "tool.execute.after": async (input) => {
      if (input.tool !== "edit" && input.tool !== "write") return
      const filePath: string | undefined = input.args?.filePath
      if (!filePath || !filePath.endsWith(".py")) return
      const root = findPyprojectRoot(dirname(filePath))
      if (!root) return
      try {
        await $`cd ${root} && uv run ruff format ${filePath} && uv run ruff check --fix ${filePath}`.quiet()
      } catch {
        // Best-effort -- never block the agent loop on a lint failure.
      }
    },
  }
}

export default RuffFormatOnEdit
