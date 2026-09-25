import assert from "node:assert/strict"
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs"
import { homedir, tmpdir } from "node:os"
import { join } from "node:path"
import { test } from "node:test"

import {
  TRUST_STATE_PATH,
  cleanupTrustState,
  readTrustState,
  writeTrustState,
} from "../trust-state"

function withTemporaryHome(run: (home: string) => void): void {
  const originalHome = process.env.HOME
  const home = mkdtempSync(join(tmpdir(), "opencode-trust-state-"))
  process.env.HOME = home
  try {
    run(home)
  } finally {
    if (originalHome === undefined) delete process.env.HOME
    else process.env.HOME = originalHome
    rmSync(home, { recursive: true, force: true })
  }
}

test("state is per-session, atomic, and fail-closed for missing data", () => {
  withTemporaryHome((home) => {
    assert.equal(readTrustState("missing").trusted, false)

    writeTrustState("session-a", { trusted: false })
    writeTrustState("session-b", { trusted: true })
    assert.deepEqual(readTrustState("session-a"), {
      trusted: false,
      updatedAt: readTrustState("session-a").updatedAt,
    })
    assert.equal(readTrustState("session-b").trusted, true)
    assert.notEqual(readTrustState("session-a").updatedAt, undefined)

    const statePath = join(home, TRUST_STATE_PATH, "session-a.json")
    assert.equal(JSON.parse(readFileSync(statePath, "utf8")).trusted, false)
  })
})

test("cleanup removes malformed files but retains active sessions", () => {
  withTemporaryHome((home) => {
    writeTrustState("active", { trusted: false })
    const stateDir = join(home, TRUST_STATE_PATH)
    const malformed = join(stateDir, "malformed.json")
    rmSync(malformed, { force: true })
    writeFileSync(malformed, "not json")
    cleanupTrustState(new Set(["active"]))
    assert.equal(readTrustState("active").trusted, false)
    assert.throws(() => readFileSync(malformed), /ENOENT/)
  })
})

test("state path is under the cutover-immune local state root", () => {
  assert.equal(TRUST_STATE_PATH, ".local/state/agent-toolkit/trust-sessions")
  assert.equal(homedir().length > 0, true)
})
