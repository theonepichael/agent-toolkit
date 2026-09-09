import { homedir } from "node:os";
import { join } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { StringEnum } from "@earendil-works/pi-ai";
import { Type } from "typebox";
import { getEffectiveCwd } from "./cwd";

// Wraps agent-scripts/vitals_promotion.py, following the pattern set by
// dev-status-tool.ts (see ~/.claude/data/grill/pi-tool-dev-status-spec.md).
//
// The script has no subcommands, only flags, so the two things a caller
// actually wants -- run the promotion pass, or search the vitals store for
// already-settled facts on a topic -- are modelled as two actions rather
// than as a bare flag bag. That keeps --apply and the search-only fields
// from being offered on the wrong action, where the script would silently
// ignore them.

const VITALS_PROMOTION_PATH = join(homedir(), ".claude", "scripts", "vitals_promotion.py");

const ACTIONS = ["run", "search"] as const;

export type Action = (typeof ACTIONS)[number];

export type Field = "apply" | "dataDir" | "query" | "includeSuperseded" | "backlogSlug";

interface ActionFields {
  readonly allowed: readonly Field[];
  readonly required: readonly Field[];
}

const ACTION_FIELDS: Record<Action, ActionFields> = {
  run: { allowed: ["apply", "dataDir"], required: [] },
  search: {
    allowed: ["query", "includeSuperseded", "backlogSlug", "dataDir"],
    required: ["query"],
  },
};

export interface VitalsPromotionParams {
  action: Action;
  apply?: boolean;
  query?: string;
  includeSuperseded?: boolean;
  backlogSlug?: string;
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
    case "search":
      return [
        "--search",
        params.query ?? "",
        ...(params.includeSuperseded ? ["--include-superseded"] : []),
        ...(params.backlogSlug ? ["--backlog-slug", params.backlogSlug] : []),
        ...dataDir,
      ];
  }
}

export default function (pi: ExtensionAPI) {
  pi.registerTool({
    name: "vitals_promotion",
    label: "Vitals",
    description:
      "Run the mechanical vitals-promotion pass over grill session data, or search the vitals store for already-settled facts on a topic.",
    promptSnippet: "Promote settled grill decisions into the vitals store, or search it",
    promptGuidelines: [
      "Never invoke vitals_promotion.py via bash, for any reason, including a dry run or a search -- always use vitals_promotion instead.",
      'vitals_promotion covers everything vitals_promotion.py does: action "run" is the promote/supersede pass (apply: true writes, omitted is a dry run that only prints), and action "search" looks up vitals records matching query (space-separated keywords, AND-combined -- every keyword must appear, no length filtering) without loading the whole store. If you are about to compose a `python3 ~/.claude/scripts/vitals_promotion.py ...` bash command, use vitals_promotion with the matching action instead.',
      "The pass is global, not per-session: it re-classifies every session on disk, so it also catches drift from sessions closed since the last run. Show its printed report to the user rather than summarizing the counts away.",
      "search defaults to the global vitals store only; pass backlogSlug to also search that backlog item's own vitals file. Superseded records are excluded unless includeSuperseded is set -- a superseded record is not a settled fact.",
    ],
    parameters: Type.Object({
      action: StringEnum(ACTIONS),
      apply: Type.Optional(
        Type.Boolean({
          description:
            'run: write the vitals files. Omit for a dry run that only prints the report. Not accepted on "search".',
        }),
      ),
      query: Type.Optional(
        Type.String({
          description:
            'search (required): space-separated keywords, AND-combined, matched case-insensitively against each record\'s text and reasoning. Not accepted on "run".',
        }),
      ),
      includeSuperseded: Type.Optional(
        Type.Boolean({
          description:
            'search: also match superseded records (excluded by default). Not accepted on "run".',
        }),
      ),
      backlogSlug: Type.Optional(
        Type.String({
          description:
            'search: also search this backlog item\'s own vitals file, not just the global store. Not accepted on "run".',
        }),
      ),
      dataDir: Type.Optional(
        Type.String({
          description: "Grill session data directory. Defaults to ~/.claude/data/grill.",
        }),
      ),
    }),
    async execute(_toolCallId, params, signal, _onUpdate, ctx) {
      const typed = params as VitalsPromotionParams;

      assertFields(typed.action, typed);

      const argv = buildArgv(typed.action, typed);
      const cwd = ctx ? getEffectiveCwd(ctx) : undefined;

      const result = await pi.exec("python3", [VITALS_PROMOTION_PATH, ...argv], {
        signal,
        ...(cwd ? { cwd } : {}),
      });

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
