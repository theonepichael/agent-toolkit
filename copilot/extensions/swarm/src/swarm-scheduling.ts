// Pure scheduling decisions over a swarm run's persisted state shape:
// worker naming, READY-item selection, concurrency/pane accounting and
// relay-stall detection. Vendored and kind-parameterized for Copilot CLI swarm.
const OPEN_PANE_SOFT_CAP_MULTIPLIER = 2;

export const TERMINAL_AGENT_STATUSES = ["idle", "done"] as const;

export function isTerminalAgentStatus(status?: string): boolean {
  return status !== undefined && (TERMINAL_AGENT_STATUSES as readonly string[]).includes(status);
}

export const RECONCILE_MIN_AGE_MS = 60_000;

export function staleWorkerRecords(
  state: SwarmState,
  live: readonly { id: string; status?: string }[],
  now: number,
): WorkerRecord[] {
  const statusById = new Map(live.map((e) => [e.id, e.status]));
  return state.workers.filter((w) => {
    const status = statusById.get(w.agent);
    if (status === undefined) return true; // absent: gone outright
    if (!isTerminalAgentStatus(status)) return false;
    const began = w.workingSinceMs ?? w.awaitingRelaySinceMs;
    if (began === undefined) return false; // no age evidence: fail open
    return now - began >= RECONCILE_MIN_AGE_MS;
  });
}

export type WorkerLifecycle = "active" | "awaiting_relay";

export interface WorkerRecord {
  agent: string; // synthetic id, e.g. "w1" -- never the raw slug (herdr names cap at 32 chars)
  slug: string;
  paneId: string;
  tabId?: string;
  paths?: string[];
  workingSinceMs?: number;
  accumulatedWorkingMs?: number;
  cwd?: string;
  checkIns?: number;
  model?: string;
  awaitingRelaySinceMs?: number;
  lastResolveFailure?: { answer: string; reason: string; at: number };
  amendments?: Amendment[];
  lifecycle: WorkerLifecycle;
  copilotSessionId?: string;
  recoveryAttempts?: number;
}

export interface SwarmState {
  runId: string;
  concurrency: number;
  nextCounter: number;
  workers: WorkerRecord[];
  attempted?: string[];
  prefix?: string;
}

const PROJECT_PREFIXES = ["iron-lb-", "meta-", "work-", "atk-"];

export function nextAgentId(runId: string, counter: number, slug?: string): string {
  const cleanSlug = slug ? slug.replace(/[^a-zA-Z0-9_-]/g, "") : "";
  const matched = PROJECT_PREFIXES.filter((prefix) => cleanSlug.startsWith(prefix)).sort(
    (a, b) => b.length - a.length,
  )[0];
  const stripped = matched ? cleanSlug.slice(matched.length) : cleanSlug;
  if (!stripped) return `${runId}-w${counter}`;
  const base = `${runId}-w${counter}-${stripped}`;
  if (base.length <= 32) return base;
  const remaining = 32 - `${runId}-w${counter}-`.length;
  if (remaining < 1) return base.slice(0, 32);
  return `${runId}-w${counter}-${stripped.slice(-remaining)}`;
}

export function stalledRelayWorkers(
  workers: WorkerRecord[],
  now: number,
  stallMs: number,
): WorkerRecord[] {
  return workers.filter(
    (w) =>
      w.lifecycle === "awaiting_relay" &&
      w.awaitingRelaySinceMs !== undefined &&
      now - w.awaitingRelaySinceMs >= stallMs,
  );
}

export interface ReadyItem {
  id: string;
  worker_safe?: unknown;
  related_files?: { path?: unknown }[];
}

export function parseReadyItems(stdout: string): ReadyItem[] {
  try {
    const parsed: unknown = JSON.parse(stdout);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((i): i is ReadyItem => typeof (i as ReadyItem)?.id === "string");
  } catch {
    return [];
  }
}

export interface ShownItem {
  status?: unknown;
}

export function parseShownItem(stdout: string): ShownItem | null {
  try {
    const parsed: unknown = JSON.parse(stdout);
    return parsed && typeof parsed === "object" ? (parsed as ShownItem) : null;
  } catch {
    return null;
  }
}

export function isSuspiciousFinish(shownStatus: unknown, captureCount: number): boolean {
  if (captureCount > 0) return false;
  return shownStatus === "open" || shownStatus === "in-progress";
}

export function itemPaths(item: ReadyItem): string[] {
  const paths = (item.related_files ?? [])
    .map((f) => f?.path)
    .filter((p): p is string => typeof p === "string" && p.length > 0);
  return [...new Set(paths)];
}

function pathsCollide(a: string, b: string): boolean {
  const x = a.replace(/\/+$/, "");
  const y = b.replace(/\/+$/, "");
  if (x === y) return true;
  return x.startsWith(`${y}/`) || y.startsWith(`${x}/`);
}

export interface SelectionResult {
  slugs: string[];
  deferred: { slug: string; reason: string }[];
  skipped: string[];
  refused: { slug: string; reason: string }[];
}

export function selectSchedulable(
  candidates: readonly ReadyItem[],
  takenPaths: readonly { path: string; holder: string }[],
  headroom: number,
): SelectionResult {
  const slugs: string[] = [];
  const deferred: { slug: string; reason: string }[] = [];
  const skipped: string[] = [];
  const refused: { slug: string; reason: string }[] = [];
  const taken = [...takenPaths];
  const seen = new Set<string>();

  for (const candidate of candidates) {
    if (seen.has(candidate.id)) continue;
    seen.add(candidate.id);
    if (candidate.worker_safe !== true) {
      refused.push({
        slug: candidate.id,
        reason:
          candidate.worker_safe === false
            ? "the backlog reports this item is not worker-safe -- its prefix " +
              "names the harness repo, or is unrecognised. A worker would be " +
              "editing the code it is running. Work it in a normal session."
            : "dev_status.py ready reported no worker_safe field for this " +
              "item, so eligibility is unknown and it is refused rather than " +
              "assumed safe. Update the installed dev_status.py.",
      });
      continue;
    }
    if (slugs.length >= headroom) {
      skipped.push(candidate.id);
      continue;
    }
    const paths = itemPaths(candidate);
    let clashPath: string | undefined;
    let clashHolder: string | undefined;
    for (const p of paths) {
      const hit = taken.find((t) => pathsCollide(p, t.path));
      if (hit !== undefined) {
        clashPath = p;
        clashHolder = hit.holder;
        break;
      }
    }
    if (clashPath !== undefined) {
      deferred.push({
        slug: candidate.id,
        reason: `file overlap with ${clashHolder}: ${clashPath}`,
      });
      continue;
    }
    slugs.push(candidate.id);
    taken.push(
      ...paths.map((p) => ({
        path: p,
        holder: `candidate ${candidate.id} (selected earlier this wave)`,
      })),
    );
  }

  return { slugs, deferred, skipped, refused };
}

export interface Amendment {
  at: number;
  by: string;
}

export function activeWorkerCount(state: SwarmState): number {
  return state.workers.filter((w) => w.lifecycle === "active").length;
}

export function canSpawnNew(state: SwarmState): boolean {
  return activeWorkerCount(state) < state.concurrency;
}

export function openPaneCount(state: SwarmState): number {
  return state.workers.length;
}

export function openPaneSoftCap(concurrency: number): number {
  return concurrency * OPEN_PANE_SOFT_CAP_MULTIPLIER;
}

export function canOpenNewPane(state: SwarmState): boolean {
  return openPaneCount(state) < openPaneSoftCap(state.concurrency);
}

export function spawnBudget(state: SwarmState, readyCount: number): number {
  const byConcurrency = Math.max(0, state.concurrency - activeWorkerCount(state));
  const byPaneCap = Math.max(0, openPaneSoftCap(state.concurrency) - openPaneCount(state));
  return Math.min(byConcurrency, byPaneCap, readyCount);
}
