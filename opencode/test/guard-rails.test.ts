import assert from "node:assert/strict"
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs"
import { tmpdir } from "node:os"
import { join } from "node:path"
import { test } from "node:test"

import { GuardRails } from "../plugin/guard-rails"

// opencode's plugin captures promisify(execFile) at module load, so there is
// no injectable exec seam the way pi's extension has ExtensionAPI.exec. The
// test instead points HOME at a throwaway dir holding a stub guard_rails.py:
// the plugin's real execFile call launches the stub, which records its argv
// to a file named by GUARD_STUB_ARGV and answers an allow verdict. This
// exercises the real call path and both call sites without launching the real
// guard or touching the developer's HOME.
const STUB = `import json, os, sys
with open(os.environ["GUARD_STUB_ARGV"], "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
print(json.dumps({"decision": "allow"}))
`

type ExecuteBefore = (
  input: { tool?: string; sessionID?: string },
  output: { args?: Record<string, unknown> },
) => Promise<void>

// The plugin's factory ignores its PluginInput argument, so narrow it to the
// zero-arg shape its implementation actually has (matching the handler types
// declared in plugin/guard-rails.ts, which are looser than the SDK's Hooks).
const GuardRailsPlugin = GuardRails as unknown as () => Promise<{
  "tool.execute.before": ExecuteBefore
}>

function recordArgv(home: string, argvLog: string): void {
  const scriptDir = join(home, ".agent-toolkit", "scripts")
  mkdirSync(scriptDir, { recursive: true })
  writeFileSync(join(scriptDir, "guard_rails.py"), STUB)
  process.env.GUARD_STUB_ARGV = argvLog
}

function readArgvLines(argvLog: string): string[][] {
  return readFileSync(argvLog, "utf8")
    .trim()
    .split("\n")
    .filter((line) => line.length > 0)
    .map((line) => JSON.parse(line) as string[])
}

test("opencode guard-rails passes --harness opencode at both call sites", async () => {
  const originalHome = process.env.HOME
  const originalArgvLog = process.env.GUARD_STUB_ARGV
  const home = mkdtempSync(join(tmpdir(), "opencode-guard-rails-"))
  const argvLog = join(home, "argv.jsonl")
  try {
    recordArgv(home, argvLog)
    process.env.HOME = home

    const plugin = await GuardRailsPlugin()
    const handler = plugin["tool.execute.before"]

    // Bash hook: --tool bash --cwd <cwd> --command <command>
    await handler({ tool: "bash" }, { args: { command: "ls" } })
    // Write-family hook: --tool write --cwd <dirname> --path <filePath>
    await handler({ tool: "write" }, { args: { filePath: "/tmp/opencode/reports/x.py" } })

    const calls = readArgvLines(argvLog)
    assert.equal(calls.length, 2)
    for (const argv of calls) {
      const harnessIdx = argv.indexOf("--harness")
      assert.ok(harnessIdx >= 0, `expected --harness in argv: ${JSON.stringify(argv)}`)
      assert.equal(argv[harnessIdx + 1], "opencode")
    }
    const [bashCall, writeCall] = calls
    assert.equal(bashCall![bashCall!.indexOf("--tool") + 1], "bash")
    assert.equal(bashCall![bashCall!.indexOf("--command") + 1], "ls")
    assert.equal(writeCall![writeCall!.indexOf("--tool") + 1], "write")
    assert.equal(writeCall![writeCall!.indexOf("--path") + 1], "/tmp/opencode/reports/x.py")
  } finally {
    if (originalHome === undefined) delete process.env.HOME
    else process.env.HOME = originalHome
    if (originalArgvLog === undefined) delete process.env.GUARD_STUB_ARGV
    else process.env.GUARD_STUB_ARGV = originalArgvLog
    rmSync(home, { recursive: true, force: true })
  }
})
