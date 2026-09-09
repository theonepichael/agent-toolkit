// copilot/extensions/swarm/src/swarm-scheduling.ts
var OPEN_PANE_SOFT_CAP_MULTIPLIER = 2;
var TERMINAL_AGENT_STATUSES = ["idle", "done"];
function isTerminalAgentStatus(status) {
  return status !== undefined && TERMINAL_AGENT_STATUSES.includes(status);
}
var RECONCILE_MIN_AGE_MS = 60000;
function staleWorkerRecords(state, live, now) {
  const statusById = new Map(live.map((e) => [e.id, e.status]));
  return state.workers.filter((w) => {
    const status = statusById.get(w.agent);
    if (status === undefined)
      return true;
    if (!isTerminalAgentStatus(status))
      return false;
    const began = w.workingSinceMs ?? w.awaitingRelaySinceMs;
    if (began === undefined)
      return false;
    return now - began >= RECONCILE_MIN_AGE_MS;
  });
}
var PROJECT_PREFIXES = ["iron-lb-", "meta-", "work-", "atk-"];
function nextAgentId(runId, counter, slug) {
  const cleanSlug = slug ? slug.replace(/[^a-zA-Z0-9_-]/g, "") : "";
  const matched = PROJECT_PREFIXES.filter((prefix) => cleanSlug.startsWith(prefix)).sort((a, b) => b.length - a.length)[0];
  const stripped = matched ? cleanSlug.slice(matched.length) : cleanSlug;
  if (!stripped)
    return `${runId}-w${counter}`;
  const base = `${runId}-w${counter}-${stripped}`;
  if (base.length <= 32)
    return base;
  const remaining = 32 - `${runId}-w${counter}-`.length;
  if (remaining < 1)
    return base.slice(0, 32);
  return `${runId}-w${counter}-${stripped.slice(-remaining)}`;
}
function stalledRelayWorkers(workers, now, stallMs) {
  return workers.filter((w) => w.lifecycle === "awaiting_relay" && w.awaitingRelaySinceMs !== undefined && now - w.awaitingRelaySinceMs >= stallMs);
}
function parseReadyItems(stdout) {
  try {
    const parsed = JSON.parse(stdout);
    if (!Array.isArray(parsed))
      return [];
    return parsed.filter((i) => typeof i?.id === "string");
  } catch {
    return [];
  }
}
function parseShownItem(stdout) {
  try {
    const parsed = JSON.parse(stdout);
    return parsed && typeof parsed === "object" ? parsed : null;
  } catch {
    return null;
  }
}
function isSuspiciousFinish(shownStatus, captureCount) {
  if (captureCount > 0)
    return false;
  return shownStatus === "open" || shownStatus === "in-progress";
}
function itemPaths(item) {
  const paths = (item.related_files ?? []).map((f) => f?.path).filter((p) => typeof p === "string" && p.length > 0);
  return [...new Set(paths)];
}
function pathsCollide(a, b) {
  const x = a.replace(/\/+$/, "");
  const y = b.replace(/\/+$/, "");
  if (x === y)
    return true;
  return x.startsWith(`${y}/`) || y.startsWith(`${x}/`);
}
function selectSchedulable(candidates, takenPaths, headroom) {
  const slugs = [];
  const deferred = [];
  const skipped = [];
  const refused = [];
  const taken = [...takenPaths];
  const seen = new Set;
  for (const candidate of candidates) {
    if (seen.has(candidate.id))
      continue;
    seen.add(candidate.id);
    if (candidate.worker_safe !== true) {
      refused.push({
        slug: candidate.id,
        reason: candidate.worker_safe === false ? "the backlog reports this item is not worker-safe -- its prefix " + "names the harness repo, or is unrecognised. A worker would be " + "editing the code it is running. Work it in a normal session." : "dev_status.py ready reported no worker_safe field for this " + "item, so eligibility is unknown and it is refused rather than " + "assumed safe. Update the installed dev_status.py."
      });
      continue;
    }
    if (slugs.length >= headroom) {
      skipped.push(candidate.id);
      continue;
    }
    const paths = itemPaths(candidate);
    let clashPath;
    let clashHolder;
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
        reason: `file overlap with ${clashHolder}: ${clashPath}`
      });
      continue;
    }
    slugs.push(candidate.id);
    taken.push(...paths.map((p) => ({
      path: p,
      holder: `candidate ${candidate.id} (selected earlier this wave)`
    })));
  }
  return { slugs, deferred, skipped, refused };
}
function activeWorkerCount(state) {
  return state.workers.filter((w) => w.lifecycle === "active").length;
}
function canSpawnNew(state) {
  return activeWorkerCount(state) < state.concurrency;
}
function openPaneCount(state) {
  return state.workers.length;
}
function openPaneSoftCap(concurrency) {
  return concurrency * OPEN_PANE_SOFT_CAP_MULTIPLIER;
}
function canOpenNewPane(state) {
  return openPaneCount(state) < openPaneSoftCap(state.concurrency);
}
function spawnBudget(state, readyCount) {
  const byConcurrency = Math.max(0, state.concurrency - activeWorkerCount(state));
  const byPaneCap = Math.max(0, openPaneSoftCap(state.concurrency) - openPaneCount(state));
  return Math.min(byConcurrency, byPaneCap, readyCount);
}
export {
  RECONCILE_MIN_AGE_MS,
  TERMINAL_AGENT_STATUSES,
  activeWorkerCount,
  canOpenNewPane,
  canSpawnNew,
  isSuspiciousFinish,
  isTerminalAgentStatus,
  itemPaths,
  nextAgentId,
  openPaneCount,
  openPaneSoftCap,
  parseReadyItems,
  parseShownItem,
  selectSchedulable,
  spawnBudget,
  staleWorkerRecords,
  stalledRelayWorkers
};
