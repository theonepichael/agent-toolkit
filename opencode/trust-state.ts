import { mkdirSync, readdirSync, readFileSync, renameSync, rmSync, writeFileSync } from "node:fs"
import { homedir } from "node:os"
import { basename, join } from "node:path"

export const TRUST_STATE_PATH = ".local/state/agent-toolkit/trust-sessions"
const DEFAULT_STATE: TrustState = { trusted: false }

export type TrustState = {
  trusted: boolean
  updatedAt?: number
}

function stateDir(): string {
  return join(homedir(), TRUST_STATE_PATH)
}

function fileForSession(sessionID: string): string {
  return join(stateDir(), `${encodeURIComponent(sessionID)}.json`)
}

export function readTrustState(sessionID: string): TrustState {
  try {
    const value: unknown = JSON.parse(readFileSync(fileForSession(sessionID), "utf8"))
    if (!value || typeof value !== "object") return { ...DEFAULT_STATE }
    const record = value as Record<string, unknown>
    if (typeof record.trusted !== "boolean") return { ...DEFAULT_STATE }
    return {
      trusted: record.trusted,
      ...(typeof record.updatedAt === "number" ? { updatedAt: record.updatedAt } : {}),
    }
  } catch {
    return { ...DEFAULT_STATE }
  }
}

export function writeTrustState(sessionID: string, state: Omit<TrustState, "updatedAt">): void {
  const directory = stateDir()
  mkdirSync(directory, { recursive: true })
  const destination = fileForSession(sessionID)
  const temporary = `${destination}.${process.pid}.tmp`
  const record = { ...state, updatedAt: Date.now() }
  writeFileSync(temporary, `${JSON.stringify(record)}\n`, { encoding: "utf8", mode: 0o600 })
  renameSync(temporary, destination)
}

export function cleanupTrustState(activeSessionIDs: ReadonlySet<string>): void {
  let files: string[]
  try {
    files = readdirSync(stateDir())
  } catch {
    return
  }
  for (const file of files) {
    if (!file.endsWith(".json")) continue
    const sessionID = decodeURIComponent(basename(file, ".json"))
    if (activeSessionIDs.has(sessionID)) continue
    try {
      JSON.parse(readFileSync(join(stateDir(), file), "utf8"))
    } catch {
      rmSync(join(stateDir(), file), { force: true })
    }
  }
}
