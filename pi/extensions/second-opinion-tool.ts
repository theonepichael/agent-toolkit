import { homedir } from "node:os";
import { join } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { StringEnum } from "@earendil-works/pi-ai";
import { Type } from "typebox";
import { getEffectiveCwd } from "./cwd";

// Wraps agent-scripts/second_opinion.py, following the pattern set by
// dev-status-tool.ts (see ~/.agent-toolkit/data/grill/pi-tool-dev-status-spec.md).
//
// The script is single-round by design: one call, one critique. The
// multi-round loop, plan revision, and convergence judgment stay in the
// prompt template -- this tool deliberately does not model them.

const SECOND_OPINION_PATH = join(homedir(), ".agent-toolkit", "scripts", "second_opinion.py");

const ACTIONS = ["detect", "review", "probe", "bind-notes", "check-notes"] as const;

export type Action = (typeof ACTIONS)[number];

export type Field =
  | "planFile"
  | "backend"
  | "focusFile"
  | "modelIndex"
  | "dir"
  | "textOnly"
  | "model"
  | "timeoutSeconds"
  | "runId";

interface ActionFields {
  readonly allowed: readonly Field[];
  readonly required: readonly Field[];
}

const ACTION_FIELDS: Record<Action, ActionFields> = {
  detect: { allowed: [], required: [] },
  review: {
    allowed: [
      "planFile",
      "backend",
      "focusFile",
      "modelIndex",
      "dir",
      "textOnly",
      "model",
      "timeoutSeconds",
      "runId",
    ],
    required: ["planFile"],
  },
  // Mirrors `second_opinion.py probe [--backend NAME[,NAME...]]`; `model` and
  // `timeoutSeconds` reach it through the same env vars review uses.
  probe: { allowed: ["backend", "model", "timeoutSeconds"], required: [] },
  "bind-notes": { allowed: ["planFile"], required: ["planFile"] },
  "check-notes": { allowed: ["planFile"], required: ["planFile"] },
};

export interface SecondOpinionParams {
  action: Action;
  planFile?: string;
  backend?: string;
  focusFile?: string;
  modelIndex?: number;
  dir?: string;
  textOnly?: boolean;
  model?: string;
  timeoutSeconds?: number;
  runId?: string;
}

const KNOWN_BACKENDS = ["codex", "agy", "opencode", "pi", "copilot"] as const;

function splitBackends(backend: string | undefined): string[] {
  if (backend === undefined) return [];
  return backend
    .split(",")
    .map((b) => b.trim())
    .filter((b) => b.length > 0);
}

function unknownBackends(names: string[]): string[] {
  const known = new Set<string>(KNOWN_BACKENDS);
  return names.filter((b) => !known.has(b));
}

export function assertFields(action: Action, params: SecondOpinionParams): void {
  const { allowed, required } = ACTION_FIELDS[action];
  const allowedSet = new Set<Field>(allowed);

  const supplied = (Object.keys(params) as (keyof SecondOpinionParams)[]).filter(
    (key) => key !== "action" && params[key] !== undefined,
  ) as Field[];

  const missing = required.filter((field) => params[field] === undefined);
  if (missing.length > 0) {
    throw new Error(`action "${action}" requires: ${missing.join(", ")}`);
  }

  const extra = supplied.filter((field) => !allowedSet.has(field));
  if (extra.length > 0) {
    throw new Error(`action "${action}" does not accept: ${extra.join(", ")}`);
  }

  if (params.planFile !== undefined && params.planFile.trim() === "") {
    throw new Error("planFile must not be empty");
  }

  if (
    params.modelIndex !== undefined &&
    (!Number.isInteger(params.modelIndex) || params.modelIndex < 0)
  ) {
    throw new Error(`modelIndex must be a non-negative integer, got ${params.modelIndex}`);
  }

  // Validate `backend` (if given) names only known backends. Arbitrary names
  // would otherwise become arbitrary SECOND_OPINION_<NAME>_* env vars the
  // script silently ignores.
  if (params.backend !== undefined) {
    const names = splitBackends(params.backend);
    // The script's own list parser rejects a value with no names in it
    // ("," or whitespace); refuse it here rather than passing it through.
    if (names.length === 0) {
      throw new Error("backend must name at least one backend");
    }
    const unknown = unknownBackends(names);
    if (unknown.length > 0) {
      throw new Error(
        `backend must name known backends (${KNOWN_BACKENDS.join(", ")}) — unknown: ${unknown.join(", ")}`,
      );
    }
  }

  // The script's single-model override is per-backend (SECOND_OPINION_<BACKEND>_MODEL);
  // there is no global model env var. So `model` requires exactly one known
  // backend — no backend, an unknown one, or a comma list would silently do
  // nothing or produce a malformed variable (e.g. SECOND_OPINION_CODEX,AGY_MODEL).
  if (params.model !== undefined) {
    const names = splitBackends(params.backend);
    if (names.length !== 1 || unknownBackends(names).length > 0) {
      throw new Error(
        `model requires a single backend — pass exactly one of: ${KNOWN_BACKENDS.join(", ")}`,
      );
    }
    if (params.model.trim() === "") {
      throw new Error("model must not be empty");
    }
    // An explicit --model-index selects the model pool over the single-model
    // override, silently replacing the pinned model. The two are incompatible.
    if (params.modelIndex !== undefined) {
      throw new Error(
        "model and modelIndex are mutually exclusive — an explicit index selects the model pool over the pinned model",
      );
    }
  }
  // A run id scopes the per-round cap to one iterative critique session;
  // the script keys its counter by runId (or, when omitted, by the resolved
  // plan path), so a loop on the same plan is refused past the cap.
  if (params.runId !== undefined && params.runId.trim() === "") {
    throw new Error("runId must not be empty");
  }
}

export function buildArgv(action: Action, params: SecondOpinionParams): string[] {
  switch (action) {
    case "detect":
      return ["detect"];
    case "probe":
      return ["probe", ...(params.backend ? ["--backend", params.backend] : [])];
    case "bind-notes":
    case "check-notes":
      return [action, params.planFile!];
    case "review":
      return [
        "review",
        params.planFile!,
        ...(params.backend ? ["--backend", params.backend] : []),
        ...(params.dir ? ["--dir", params.dir] : []),
        ...(params.textOnly ? ["--text-only"] : []),
        ...(params.focusFile ? ["--focus-file", params.focusFile] : []),
        ...(params.runId ? ["--run-id", params.runId] : []),
        // Compared against undefined, not truthiness: index 0 is round 1 of
        // the rotation, and dropping it would silently fall back to the
        // single-model override instead of the pool.
        ...(params.modelIndex !== undefined ? ["--model-index", String(params.modelIndex)] : []),
      ];
  }
}

const MIN_TIMEOUT_SECONDS = 1;
const MAX_TIMEOUT_SECONDS = 600; // the script's hard ceiling on every timeout

function clampTimeout(seconds: number): number {
  return Math.min(MAX_TIMEOUT_SECONDS, Math.max(MIN_TIMEOUT_SECONDS, Math.floor(seconds)));
}

/**
 * Build the `KEY=VALUE` env-prefix entries for `model` and `timeoutSeconds`.
 * Empty array means no env to set, and `execute` then invokes `python3`
 * directly (unchanged behavior).
 *
 * `model` maps to `SECOND_OPINION_<BACKEND>_MODEL` (the script's per-backend
 * override); `timeoutSeconds` maps to `SECOND_OPINION_<BACKEND>_TIMEOUT_SECONDS`
 * when a backend is named, else the global `SECOND_OPINION_TIMEOUT_SECONDS`.
 */
export function buildEnvPrefix(params: SecondOpinionParams): string[] {
  const env: string[] = [];
  // Validated by assertFields: every name here is a known backend, no empties.
  const names = splitBackends(params.backend);
  if (params.model !== undefined && names.length === 1) {
    env.push(`SECOND_OPINION_${names[0].toUpperCase()}_MODEL=${params.model}`);
  }
  if (params.timeoutSeconds !== undefined) {
    const clamped = clampTimeout(params.timeoutSeconds);
    if (names.length > 0) {
      // A comma list is tried in order, so each listed backend gets its own
      // per-backend timeout override.
      for (const name of names) {
        env.push(`SECOND_OPINION_${name.toUpperCase()}_TIMEOUT_SECONDS=${clamped}`);
      }
    } else {
      env.push(`SECOND_OPINION_TIMEOUT_SECONDS=${clamped}`);
    }
  }
  return env;
}

const PROBE_STATUSES = new Set(["ok", "unavailable", "not_installed"]);

export interface ProbeCounts {
  ok: number;
  unavailable: number;
  notInstalled: number;
}

/**
 * Parse `second_opinion.py probe` stdout into per-status counts, or `null`
 * when it is not a well-formed report: a JSON object whose `probes` array
 * holds only objects with a string `backend` and a known `status`.
 */
export function parseProbeReport(stdout: string): ProbeCounts | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(stdout);
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null) return null;
  const probes = (parsed as { probes?: unknown }).probes;
  if (!Array.isArray(probes)) return null;
  const counts: ProbeCounts = { ok: 0, unavailable: 0, notInstalled: 0 };
  for (const entry of probes) {
    if (typeof entry !== "object" || entry === null) return null;
    const { backend, status } = entry as { backend?: unknown; status?: unknown };
    if (typeof backend !== "string" || typeof status !== "string") return null;
    if (!PROBE_STATUSES.has(status)) return null;
    if (status === "ok") counts.ok++;
    else if (status === "unavailable") counts.unavailable++;
    else counts.notInstalled++;
  }
  return counts;
}

export default function (pi: ExtensionAPI) {
  pi.registerTool({
    name: "second_opinion",
    label: "Critique",
    description:
      "Get one adversarial critique of a plan from a non-Claude backend, list which backends are available, probe each backend's model pool for per-model availability, or bind/check a plan's critique-notes companion against its content hash.",
    promptSnippet: "Get an outside adversarial critique of a plan file",
    promptGuidelines: [
      "Never invoke second_opinion.py via bash -- always use second_opinion instead.",
      'second_opinion covers everything second_opinion.py does: action "detect" lists available backends as JSON, action "review" returns one critique of the plan at planFile, action "probe" sends one trivial request to each model in the selected backend\'s pool (or its single override / default model) and returns a per-model availability report as JSON, action "bind-notes" stamps planFile\'s -critique-notes.md companion with planFile\'s content hash, and action "check-notes" prints that binding\'s status (current, stale, unbound, missing, or no-artifact). If you are about to compose a `python3 ~/.agent-toolkit/scripts/second_opinion.py ...` bash command, use second_opinion instead.',
      "Never shell out to codex, agy, pi, opencode, or copilot directly for a critique -- all backend I/O goes through this tool.",
      'Action "probe" calls every probed model once for real, so pass `backend` deliberately rather than probing every installed backend. An unavailable model is not a tool error: the JSON report comes back with a second line counting ok, unavailable, and not-installed entries. A report with 0 ok tested nothing. `model` (with one `backend`) pins the model probed only when that backend has no pool configured; a configured pool is probed instead.',
      "It is single-round: one call, one critique. The multi-round loop, the plan revision between rounds, and the convergence judgment are yours, not the tool's.",
      "The script enforces a per-run cap (3 rounds by default): pass a stable `runId` for the whole loop (or rely on the plan-file path) and the 4th `review` call for that run is refused with a finalize-and-stop message.",
      "Always pass planFile as a path. Never inline plan text -- write the plan to a file first.",
      "modelIndex is 0-based: round 1 is 0, round 2 is 1. If a call fails with a pool configuration error naming --model-index, retry that same round once with modelIndex omitted. That is the valid fallback, not a skipped round.",
      "To pin a model for the critique, pass `model` together with `backend` (Pi sets SECOND_OPINION_<BACKEND>_MODEL for you) — never set that env var directly. To raise the timeout on a slow critique, pass `timeoutSeconds` (clamped to 600); do not hand-set SECOND_OPINION_TIMEOUT_SECONDS.",
    ],
    parameters: Type.Object({
      action: StringEnum(ACTIONS),
      planFile: Type.Optional(
        Type.String({
          description:
            "review: path to the plan file to critique, conventionally ~/.agent-toolkit/data/grill/<topic-slug>-plan.md. A path, never inline plan text. bind-notes/check-notes: the plan or spec whose -critique-notes.md companion to bind or check.",
        }),
      ),
      backend: Type.Optional(
        Type.String({
          description:
            "review: force this backend instead of priority-order fallback. Use action detect to see what is available. " +
            "probe: probe only these backend(s) (comma-separated) instead of every installed backend.",
        }),
      ),
      dir: Type.Optional(
        Type.String({
          description:
            "review: root directory of the codebase to inspect in grounded review (defaults to current working directory).",
        }),
      ),
      textOnly: Type.Optional(
        Type.Boolean({
          description:
            "review: disable codebase exploration and run ungrounded text-only critique (default: false).",
        }),
      ),
      focusFile: Type.Optional(
        Type.String({
          description:
            "review: path to a file of plan-specific risk hints, appended to the critique prompt as areas to scrutinize. Supplements the generic adversarial mandate, never replaces it. Omit it rather than writing generic filler.",
        }),
      ),
      modelIndex: Type.Optional(
        Type.Integer({
          minimum: 0,
          description:
            "review: 0-based index into the backend's model pool for this call. Round 1 is 0, round 2 is 1. A hard error if the pool is unset/empty or the index is out of range.",
        }),
      ),
      model: Type.Optional(
        Type.String({
          description:
            "review/probe: force the model used for the critique (probe: the model probed when " +
            "the backend has no pool configured). Requires `backend` — it sets " +
            "SECOND_OPINION_<BACKEND>_MODEL for the named backend (the script has no global " +
            "model override).",
        }),
      ),
      timeoutSeconds: Type.Optional(
        Type.Integer({
          minimum: 1,
          description:
            "review/probe: per-call timeout in seconds, clamped to the [1, 600] range and written to " +
            "SECOND_OPINION_<BACKEND>_TIMEOUT_SECONDS (or the global SECOND_OPINION_TIMEOUT_SECONDS " +
            "when no backend is set).",
        }),
      ),
      runId: Type.Optional(
        Type.String({
          description:
            "review: stable id for one iterative critique session. Scopes the " +
            "script's per-run round cap to this loop (the second-opinion skill " +
            "passes one for the whole loop). When omitted, the script keys the " +
            "cap by the resolved plan-file path instead.",
        }),
      ),
    }),
    async execute(_toolCallId, params, signal, _onUpdate, ctx) {
      const typed = params as SecondOpinionParams;

      assertFields(typed.action, typed);

      const argv = buildArgv(typed.action, typed);
      const envPrefix = buildEnvPrefix(typed);
      const cwd = ctx ? getEffectiveCwd(ctx) : undefined;

      // pi.exec has no `env` option, so env is delivered via the `env`
      // coreutil as the command (same pattern as dev-status-tool's
      // DEVSTATUS_AGENT=1). No env to set -> keep invoking python3 directly.
      const command = envPrefix.length > 0 ? "env" : "python3";
      const baseArgs =
        envPrefix.length > 0
          ? [...envPrefix, "python3", SECOND_OPINION_PATH, ...argv]
          : [SECOND_OPINION_PATH, ...argv];

      const result = await pi.exec(command, baseArgs, {
        signal,
        ...(cwd ? { cwd } : {}),
      });

      // `probe` exits 1 when any probed model is unavailable, right after
      // printing its full report. That report is the answer the model asked
      // for, so only that exact shape (a valid report with at least one
      // unavailable entry) is returned; every other nonzero exit still throws.
      const probeCounts = typed.action === "probe" ? parseProbeReport(result.stdout) : null;
      const probeReportExit =
        result.code === 1 && probeCounts !== null && probeCounts.unavailable > 0;

      if (result.code !== 0 && !probeReportExit) {
        throw new Error(
          result.stderr || result.stdout || `second_opinion.py exited ${result.code}`,
        );
      }

      const text = result.stderr ? `${result.stdout}\n\n${result.stderr}` : result.stdout;

      if (probeCounts !== null) {
        // The summary is a separate block so the first one stays the
        // script's JSON verbatim. Counts, not a healthy flag: a report with
        // zero ok entries exits 0 yet tested nothing.
        const summary = `probe: ${probeCounts.ok} ok, ${probeCounts.unavailable} unavailable, ${probeCounts.notInstalled} not installed`;
        return {
          content: [
            { type: "text", text },
            { type: "text", text: summary },
          ],
          details: {
            stdout: result.stdout,
            stderr: result.stderr,
            exitCode: result.code,
            ...probeCounts,
          },
        };
      }

      return {
        content: [{ type: "text", text }],
        details: { stdout: result.stdout, stderr: result.stderr },
      };
    },
  });
}
