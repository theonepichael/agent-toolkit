// pi/extensions/swarm-lib/swarm-scheduling.ts
var OPEN_PANE_SOFT_CAP_MULTIPLIER = 2;
var TERMINAL_AGENT_STATUSES = ["idle", "done"];
function isTerminalAgentStatus(status) {
  return status !== void 0 && TERMINAL_AGENT_STATUSES.includes(status);
}
var RECONCILE_MIN_AGE_MS = 6e4;
function staleWorkerRecords(state, live, now) {
  const statusById = new Map(live.map((e) => [e.id, e.status]));
  return state.workers.filter((w) => {
    if (!statusById.has(w.agent)) return true;
    const status = statusById.get(w.agent);
    if (status === void 0) return false;
    if (!isTerminalAgentStatus(status)) return false;
    const began = w.workingSinceMs ?? w.awaitingRelaySinceMs;
    if (began === void 0) return false;
    return now - began >= RECONCILE_MIN_AGE_MS;
  });
}
var SIDECAR_VERSION = 1;
var FATAL_SIDECAR_RESULT = "fatal_error";
function parseFatalSidecar(raw) {
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null) return null;
  const rec = parsed;
  if (rec.v !== SIDECAR_VERSION) return null;
  if (rec.result !== FATAL_SIDECAR_RESULT) return null;
  if (typeof rec.stopReason !== "string") return null;
  if (typeof rec.model !== "string") return null;
  if (typeof rec.writtenAtMs !== "number" || !Number.isFinite(rec.writtenAtMs)) return null;
  return {
    v: SIDECAR_VERSION,
    result: FATAL_SIDECAR_RESULT,
    stopReason: rec.stopReason,
    model: rec.model,
    writtenAtMs: rec.writtenAtMs
  };
}
function classifyOutcomeDraft(kind, witnesses) {
  const base = {
    sidecarProbed: witnesses.sidecarProbed,
    ...witnesses.herdrStatus !== void 0 ? { herdrStatus: witnesses.herdrStatus } : {}
  };
  if (witnesses.sidecar) {
    return { ...base, processResult: "fatal_error", evidence: "fatal_sidecar" };
  }
  if (witnesses.paneMatch === "fatal_sentinel") {
    return { ...base, processResult: "unknown_crash", evidence: "pane_sentinel" };
  }
  if (witnesses.paneMatch === "provider_wording") {
    return { ...base, processResult: "unknown_crash", evidence: "provider_wording" };
  }
  if (kind === "timed_out") {
    return {
      ...base,
      processResult: "deadline_stopped",
      evidence: witnesses.livenessConfirmed === false ? "no_observation" : "herdr_status"
    };
  }
  if (kind === "error") {
    return { ...base, processResult: "gone", evidence: "herdr_status" };
  }
  return {
    ...base,
    processResult: "settled_alive",
    evidence: witnesses.paneRead ? "pane_clear" : "no_observation"
  };
}
function appendOutcome(state, outcome) {
  const outcomes = state.outcomes ?? [];
  const at = outcomes.findIndex(
    (o) => o.agent === outcome.agent && o.decidedAtMs === outcome.decidedAtMs
  );
  if (at >= 0) outcomes.splice(at, 1, outcome);
  else outcomes.push(outcome);
  state.outcomes = outcomes;
  return outcomes;
}
function priorOutcome(state, agent) {
  return (state.outcomes ?? []).find((o) => o.agent === agent);
}
var PROJECT_PREFIXES = ["iron-lb-", "meta-", "work-", "atk-"];
function nextAgentId(runId, counter, slug) {
  const cleanSlug = slug ? slug.replace(/[^a-zA-Z0-9_-]/g, "") : "";
  const matched = PROJECT_PREFIXES.filter((prefix) => cleanSlug.startsWith(prefix)).sort(
    (a, b) => b.length - a.length
  )[0];
  const stripped = matched ? cleanSlug.slice(matched.length) : cleanSlug;
  if (!stripped) return `${runId}-w${counter}`;
  const base = `${runId}-w${counter}-${stripped}`;
  if (base.length <= 32) return base;
  const remaining = 32 - `${runId}-w${counter}-`.length;
  if (remaining < 1) return base.slice(0, 32);
  return `${runId}-w${counter}-${stripped.slice(-remaining)}`;
}
function stalledRelayWorkers(workers, now, stallMs) {
  return workers.filter(
    (w) => w.lifecycle === "awaiting_relay" && w.awaitingRelaySinceMs !== void 0 && now - w.awaitingRelaySinceMs >= stallMs
  );
}
function parseReadyItems(stdout) {
  try {
    const parsed = JSON.parse(stdout);
    if (!Array.isArray(parsed)) return [];
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
  if (captureCount > 0) return false;
  return shownStatus === "open" || shownStatus === "in-progress";
}
var PROVIDER_CRASH_SCAN_LINES = 10;
var PROVIDER_CRASH_MAX_GAP = 80;
var PROVIDER_CRASH_SIGNATURE_PAIRS = [
  ["usage limit", ["reached", "exceeded"]],
  ["rate limit", ["exceeded", "hit"]]
];
function stripTerminalNoise(text) {
  const csi = /\x1b\[[0-9;?]*[ -/]*[@-~]/g;
  const osc = /\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)/g;
  const c0 = new RegExp(`[\\x00-\\x08\\x0b-\\x1f\\x7f]`, "g");
  return text.replace(csi, "").replace(osc, "").replace(c0, "");
}
function normalizedPaneWindow(content, windowLines) {
  const recent = content.split("\n").filter((line) => line.trim() !== "").slice(-windowLines).join(" ");
  return stripTerminalNoise(recent).replace(/\s+/g, " ");
}
function occurrenceIndices(haystack, needle) {
  const lower = haystack.toLowerCase();
  const target = needle.toLowerCase();
  const indices = [];
  for (let i = lower.indexOf(target); i !== -1; i = lower.indexOf(target, i + 1)) {
    indices.push(i);
  }
  return indices;
}
var EXCERPT_BEFORE = 60;
var EXCERPT_AFTER = 140;
function providerCrashMatch(content) {
  const normalized = normalizedPaneWindow(content, PROVIDER_CRASH_SCAN_LINES);
  if (normalized.trim() === "") return null;
  for (const [keyword, verbs] of PROVIDER_CRASH_SIGNATURE_PAIRS) {
    const keywordIndices = occurrenceIndices(normalized, keyword);
    if (keywordIndices.length === 0) continue;
    for (const verb of verbs) {
      const verbIndices = occurrenceIndices(normalized, verb);
      for (const ki of keywordIndices) {
        for (const vi of verbIndices) {
          if (Math.abs(ki - vi) <= PROVIDER_CRASH_MAX_GAP) {
            const start = Math.max(0, Math.min(ki, vi) - EXCERPT_BEFORE);
            const end = Math.min(normalized.length, Math.max(ki, vi) + EXCERPT_AFTER);
            const excerpt = (start > 0 ? "..." : "") + normalized.slice(start, end).trim() + (end < normalized.length ? "..." : "");
            return { signature: `${keyword} ~ ${verb}`, excerpt };
          }
        }
      }
    }
  }
  return null;
}
var FATAL_ERROR_EXIT_TOKEN = "[fatal-error-exit]";
var FATAL_ERROR_EXIT_SCAN_LINES = 40;
function fatalErrorExitMatch(content) {
  const normalized = normalizedPaneWindow(content, FATAL_ERROR_EXIT_SCAN_LINES);
  const at = normalized.indexOf(FATAL_ERROR_EXIT_TOKEN);
  if (at === -1) return null;
  const start = Math.max(0, at - EXCERPT_BEFORE);
  const end = Math.min(normalized.length, at + FATAL_ERROR_EXIT_TOKEN.length + EXCERPT_AFTER);
  const excerpt = (start > 0 ? "..." : "") + normalized.slice(start, end).trim() + (end < normalized.length ? "..." : "");
  return { signature: FATAL_ERROR_EXIT_TOKEN, excerpt };
}
function itemPaths(item) {
  const paths = (item.related_files ?? []).map((f) => f?.path).filter((p) => typeof p === "string" && p.length > 0);
  return [...new Set(paths)];
}
function pathsCollide(a, b) {
  const x = a.replace(/\/+$/, "");
  const y = b.replace(/\/+$/, "");
  if (x === y) return true;
  return x.startsWith(`${y}/`) || y.startsWith(`${x}/`);
}
function selectSchedulable(candidates, takenPaths, headroom, mode = "concurrent") {
  const slugs = [];
  const deferred = [];
  const skipped = [];
  const refused = [];
  const taken = [...takenPaths];
  const seen = /* @__PURE__ */ new Set();
  for (const candidate of candidates) {
    if (seen.has(candidate.id)) continue;
    seen.add(candidate.id);
    const eligibility = mode === "serial" ? candidate.serial_safe : candidate.worker_safe;
    if (eligibility !== true) {
      refused.push({
        slug: candidate.id,
        reason: mode === "serial" && typeof candidate.serial_safety_reason === "string" ? candidate.serial_safety_reason : eligibility === false ? "the backlog reports this item is not worker-safe -- its prefix names the harness repo, or is unrecognised. A worker would be editing the code it is running. Work it in a normal session." : `dev_status.py ready reported no ${mode === "serial" ? "serial_safe" : "worker_safe"} field for this item, so eligibility is unknown and it is refused rather than assumed safe. Update the installed dev_status.py.`
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
      if (hit !== void 0) {
        clashPath = p;
        clashHolder = hit.holder;
        break;
      }
    }
    if (clashPath !== void 0) {
      deferred.push({
        slug: candidate.id,
        reason: `file overlap with ${clashHolder}: ${clashPath}`
      });
      continue;
    }
    slugs.push(candidate.id);
    taken.push(
      ...paths.map((p) => ({
        path: p,
        holder: `candidate ${candidate.id} (selected earlier this wave)`
      }))
    );
  }
  return { slugs, deferred, skipped, refused };
}
function pendingAmendWorkers(state) {
  return state.workers.filter((w) => w.pendingAmend !== void 0);
}
function pendingAmendCount(state) {
  return pendingAmendWorkers(state).length;
}
function activeWorkerCount(state) {
  return state.workers.filter((w) => w.lifecycle === "active").length;
}
function canSpawnNew(state) {
  if (state.mode === "serial") return state.workers.length === 0;
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
  if (state.mode === "serial") return state.workers.length === 0 && readyCount > 0 ? 1 : 0;
  const byConcurrency = Math.max(0, state.concurrency - activeWorkerCount(state));
  const byPaneCap = Math.max(0, openPaneSoftCap(state.concurrency) - openPaneCount(state));
  return Math.min(byConcurrency, byPaneCap, readyCount);
}
export {
  FATAL_ERROR_EXIT_SCAN_LINES,
  FATAL_ERROR_EXIT_TOKEN,
  FATAL_SIDECAR_RESULT,
  PROJECT_PREFIXES,
  PROVIDER_CRASH_MAX_GAP,
  PROVIDER_CRASH_SCAN_LINES,
  RECONCILE_MIN_AGE_MS,
  SIDECAR_VERSION,
  TERMINAL_AGENT_STATUSES,
  activeWorkerCount,
  appendOutcome,
  canOpenNewPane,
  canSpawnNew,
  classifyOutcomeDraft,
  fatalErrorExitMatch,
  isSuspiciousFinish,
  isTerminalAgentStatus,
  itemPaths,
  nextAgentId,
  openPaneCount,
  openPaneSoftCap,
  parseFatalSidecar,
  parseReadyItems,
  parseShownItem,
  pendingAmendCount,
  pendingAmendWorkers,
  priorOutcome,
  providerCrashMatch,
  selectSchedulable,
  spawnBudget,
  staleWorkerRecords,
  stalledRelayWorkers
};
