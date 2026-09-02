import { homedir } from "node:os";
import { join } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { StringEnum } from "@earendil-works/pi-ai";
import { Type } from "typebox";

// Wraps claude/scripts/second_opinion.py, following the pattern set by
// dev-status-tool.ts (see ~/.claude/data/grill/pi-tool-dev-status-spec.md).
//
// The script is single-round by design: one call, one critique. The
// multi-round loop, plan revision, and convergence judgment stay in the
// prompt template -- this tool deliberately does not model them.

const SECOND_OPINION_PATH = join(homedir(), ".claude", "scripts", "second_opinion.py");

const ACTIONS = ["detect", "review"] as const;

export type Action = (typeof ACTIONS)[number];

export type Field = "planFile" | "backend" | "focusFile" | "modelIndex";

interface ActionFields {
  readonly allowed: readonly Field[];
  readonly required: readonly Field[];
}

const ACTION_FIELDS: Record<Action, ActionFields> = {
  detect: { allowed: [], required: [] },
  review: {
    allowed: ["planFile", "backend", "focusFile", "modelIndex"],
    required: ["planFile"],
  },
};

export interface SecondOpinionParams {
  action: Action;
  planFile?: string;
  backend?: string;
  focusFile?: string;
  modelIndex?: number;
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
}

export function buildArgv(action: Action, params: SecondOpinionParams): string[] {
  switch (action) {
    case "detect":
      return ["detect"];
    case "review":
      return [
        "review",
        params.planFile!,
        ...(params.backend ? ["--backend", params.backend] : []),
        ...(params.focusFile ? ["--focus-file", params.focusFile] : []),
        // Compared against undefined, not truthiness: index 0 is round 1 of
        // the rotation, and dropping it would silently fall back to the
        // single-model override instead of the pool.
        ...(params.modelIndex !== undefined ? ["--model-index", String(params.modelIndex)] : []),
      ];
  }
}

export default function (pi: ExtensionAPI) {
  pi.registerTool({
    name: "second_opinion",
    label: "Critique",
    description:
      "Get one adversarial critique of a plan from a non-Claude backend, or list which backends are available.",
    promptSnippet: "Get an outside adversarial critique of a plan file",
    promptGuidelines: [
      "Never invoke second_opinion.py via bash -- always use second_opinion instead.",
      'second_opinion covers everything second_opinion.py does: action "detect" lists available backends as JSON, and action "review" returns one critique of the plan at planFile. If you are about to compose a `python3 ~/.claude/scripts/second_opinion.py ...` bash command, use second_opinion instead.',
      "Never shell out to agy, opencode, pi, or copilot directly for a critique -- all backend I/O goes through this tool.",
      "It is single-round: one call, one critique. The multi-round loop, the plan revision between rounds, and the convergence judgment are yours, not the tool's.",
      "Always pass planFile as a path. Never inline plan text -- write the plan to a file first.",
      "modelIndex is 0-based: round 1 is 0, round 2 is 1. If a call fails with a pool configuration error naming --model-index, retry that same round once with modelIndex omitted. That is the valid fallback, not a skipped round.",
    ],
    parameters: Type.Object({
      action: StringEnum(ACTIONS),
      planFile: Type.Optional(
        Type.String({
          description:
            "review: path to the plan file to critique, conventionally ~/.claude/data/grill/<topic-slug>-plan.md. A path, never inline plan text.",
        }),
      ),
      backend: Type.Optional(
        Type.String({
          description:
            "review: force this backend instead of priority-order fallback. Use action detect to see what is available.",
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
    }),
    async execute(_toolCallId, params, signal) {
      const typed = params as SecondOpinionParams;

      assertFields(typed.action, typed);

      const argv = buildArgv(typed.action, typed);

      const result = await pi.exec("python3", [SECOND_OPINION_PATH, ...argv], { signal });

      if (result.code !== 0) {
        throw new Error(
          result.stderr || result.stdout || `second_opinion.py exited ${result.code}`,
        );
      }

      const text = result.stderr ? `${result.stdout}\n\n${result.stderr}` : result.stdout;

      return {
        content: [{ type: "text", text }],
        details: { stdout: result.stdout, stderr: result.stderr },
      };
    },
  });
}
