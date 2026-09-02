import { homedir } from "node:os";
import { join } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { StringEnum } from "@earendil-works/pi-ai";
import { Type } from "typebox";

// Wraps claude/scripts/vitals_promotion.py, following the pattern set by
// dev-status-tool.ts (see ~/.claude/data/grill/pi-tool-dev-status-spec.md).
//
// The script has no subcommands, only flags, so the two things a caller
// actually wants -- run the promotion pass, or read the latest
// needs-review summary -- are modelled as two actions rather than as a
// bare flag bag. That keeps --apply from being offered on the
// summary-only path, where the script would silently ignore it.

const VITALS_PROMOTION_PATH = join(homedir(), ".claude", "scripts", "vitals_promotion.py");

const ACTIONS = ["run", "needs_review_summary"] as const;

export type Action = (typeof ACTIONS)[number];

export type Field = "apply" | "dataDir";

interface ActionFields {
  readonly allowed: readonly Field[];
  readonly required: readonly Field[];
}

const ACTION_FIELDS: Record<Action, ActionFields> = {
  run: { allowed: ["apply", "dataDir"], required: [] },
  needs_review_summary: { allowed: ["dataDir"], required: [] },
};

export interface VitalsPromotionParams {
  action: Action;
  apply?: boolean;
  dataDir?: string;
}

export function assertFields(action: Action, params: VitalsPromotionParams): void {
  const { allowed, required } = ACTION_FIELDS[action];
  const allowedSet = new Set<Field>(allowed);

  const supplied = (Object.keys(params) as (keyof VitalsPromotionParams)[]).filter(
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
}

export function buildArgv(action: Action, params: VitalsPromotionParams): string[] {
  const dataDir = params.dataDir ? ["--data-dir", params.dataDir] : [];
  switch (action) {
    case "run":
      return [...(params.apply ? ["--apply"] : []), ...dataDir];
    case "needs_review_summary":
      return ["--needs-review-summary", ...dataDir];
  }
}

export default function (pi: ExtensionAPI) {
  pi.registerTool({
    name: "vitals_promotion",
    label: "Vitals",
    description:
      "Run the mechanical vitals-promotion pass over grill session data, or read the latest needs-review summary.",
    promptSnippet: "Promote settled grill decisions into the vitals store",
    promptGuidelines: [
      "Never invoke vitals_promotion.py via bash, for any reason, including a dry run or a plain needs-review read -- always use vitals_promotion instead.",
      'vitals_promotion covers everything vitals_promotion.py does: action "run" is the promote/supersede pass (apply: true writes, omitted is a dry run that only prints), and action "needs_review_summary" prints the one-line summary of the latest needs-review file. If you are about to compose a `python3 ~/.claude/scripts/vitals_promotion.py ...` bash command, use vitals_promotion with the matching action instead.',
      "The pass is global, not per-session: it re-classifies every session on disk, so it also catches drift from sessions closed since the last run. Show its printed report to the user rather than summarizing the counts away.",
    ],
    parameters: Type.Object({
      action: StringEnum(ACTIONS),
      apply: Type.Optional(
        Type.Boolean({
          description:
            'run: write the vitals/needs-review files. Omit for a dry run that only prints the report. Not accepted on "needs_review_summary".',
        }),
      ),
      dataDir: Type.Optional(
        Type.String({
          description: "Grill session data directory. Defaults to ~/.claude/data/grill.",
        }),
      ),
    }),
    async execute(_toolCallId, params, signal) {
      const typed = params as VitalsPromotionParams;

      assertFields(typed.action, typed);

      const argv = buildArgv(typed.action, typed);

      const result = await pi.exec("python3", [VITALS_PROMOTION_PATH, ...argv], { signal });

      if (result.code !== 0) {
        throw new Error(
          result.stderr || result.stdout || `vitals_promotion.py exited ${result.code}`,
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
