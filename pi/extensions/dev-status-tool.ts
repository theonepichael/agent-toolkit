import { homedir } from "node:os";
import { join } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { StringEnum } from "@earendil-works/pi-ai";
import { Type } from "typebox";

// Wraps claude/scripts/dev_status.py -- see
// ~/.claude/data/grill/pi-tool-dev-status-spec.md for the full design and
// ~/.claude/data/grill/pi-tool-dev-status-spec-critique-notes.md for why
// the numeric-identity handling below looks the way it does (two earlier
// designs both silently risked mutating the wrong item; this one refuses
// instead).

const DEV_STATUS_PATH = join(homedir(), ".claude", "scripts", "dev_status.py");

const ACTIONS = [
  "render",
  "list",
  "ready",
  "show",
  "add",
  "update",
  "start",
  "done",
  "review",
  "approve",
  "reject",
  "gate_set",
  "gate_pass",
  "run",
  "runs",
  "backfill_gate",
  "rename",
  "remove",
  "block",
  "unblock",
  "prune",
  "recap",
  "pending_add",
  "pending_update",
  "pending_list",
  "out_of_scope_add",
  "out_of_scope_link",
  "out_of_scope_unlink",
  "out_of_scope_remove",
  "out_of_scope_list",
  "out_of_scope_show",
] as const;

export type Action = (typeof ACTIONS)[number];

export type Field =
  | "slug"
  | "secondarySlug"
  | "patch"
  | "feedback"
  | "status"
  | "raw"
  | "prefix"
  | "apply"
  | "force"
  | "allowMain"
  | "claimedBy"
  | "refresh"
  | "backend"
  | "reasonFile"
  | "command"
  | "timeout";

interface ActionFields {
  readonly allowed: readonly Field[];
  readonly required: readonly Field[];
}

// Exhaustive, matching the spec's Per-action field requirements table.
const ACTION_FIELDS: Record<Action, ActionFields> = {
  render: { allowed: [], required: [] },
  list: { allowed: ["status", "raw"], required: [] },
  ready: { allowed: ["prefix"], required: [] },
  show: { allowed: ["slug"], required: ["slug"] },
  add: { allowed: ["patch"], required: ["patch"] },
  update: { allowed: ["slug", "patch"], required: ["slug", "patch"] },
  start: {
    allowed: ["slug", "force", "allowMain", "claimedBy"],
    required: ["slug"],
  },
  done: { allowed: ["slug"], required: ["slug"] },
  review: { allowed: ["slug"], required: ["slug"] },
  approve: { allowed: ["slug"], required: ["slug"] },
  reject: { allowed: ["slug", "feedback"], required: ["slug", "feedback"] },
  gate_set: { allowed: ["slug", "patch"], required: ["slug", "patch"] },
  gate_pass: { allowed: ["slug", "patch"], required: ["slug"] },
  run: { allowed: ["slug", "command", "timeout"], required: ["slug", "command"] },
  runs: { allowed: ["slug"], required: ["slug"] },
  backfill_gate: { allowed: ["apply"], required: [] },
  rename: { allowed: ["slug", "secondarySlug"], required: ["slug", "secondarySlug"] },
  remove: { allowed: ["slug"], required: ["slug"] },
  block: { allowed: ["slug", "secondarySlug"], required: ["slug", "secondarySlug"] },
  unblock: { allowed: ["slug", "secondarySlug"], required: ["slug", "secondarySlug"] },
  prune: { allowed: ["force"], required: ["force"] },
  recap: { allowed: ["refresh", "backend"], required: [] },
  pending_add: { allowed: ["patch"], required: ["patch"] },
  pending_update: { allowed: ["slug", "patch"], required: ["slug", "patch"] },
  pending_list: { allowed: [], required: [] },
  out_of_scope_add: {
    allowed: ["slug", "reasonFile", "secondarySlug"],
    required: ["slug", "reasonFile"],
  },
  out_of_scope_link: {
    allowed: ["slug", "secondarySlug"],
    required: ["slug", "secondarySlug"],
  },
  out_of_scope_unlink: {
    allowed: ["slug", "secondarySlug"],
    required: ["slug", "secondarySlug"],
  },
  out_of_scope_remove: { allowed: ["slug"], required: ["slug"] },
  out_of_scope_list: { allowed: [], required: [] },
  out_of_scope_show: { allowed: ["slug"], required: ["slug"] },
};

// Actions that mutate an existing item by identity -- numeric slugs are
// rejected here (see the spec's Context: a numeric position resolved at
// call time can't be told apart from one that's drifted to a different
// item since the model decided to act). Read-only actions and the two
// "create" actions (add/pending_add, which never take slug at all) are
// deliberately absent.
const MUTATING_ACTIONS: ReadonlySet<Action> = new Set([
  "update",
  "start",
  "done",
  "review",
  "approve",
  "reject",
  "gate_set",
  "gate_pass",
  "run",
  "rename",
  "remove",
  "block",
  "unblock",
  "pending_update",
  "out_of_scope_link",
  "out_of_scope_unlink",
  "out_of_scope_remove",
]);

const NUMERIC_ID = /^\d+$/;

export interface DevStatusParams {
  action: Action;
  slug?: string;
  secondarySlug?: string;
  patch?: Record<string, unknown>;
  feedback?: string;
  status?: string;
  raw?: boolean;
  prefix?: string;
  apply?: boolean;
  force?: boolean;
  allowMain?: boolean;
  claimedBy?: string;
  refresh?: boolean;
  backend?: string;
  reasonFile?: string;
  command?: string[];
  timeout?: number;
}

export function assertNotNumericIdentity(action: Action, params: DevStatusParams): void {
  if (!MUTATING_ACTIONS.has(action)) return;
  for (const field of ["slug", "secondarySlug"] as const) {
    const value = params[field];
    if (typeof value === "string" && NUMERIC_ID.test(value)) {
      throw new Error(
        `${field} must be a slug for action "${action}", not a numeric ` +
          `position -- call action: "show" with this position first and ` +
          `use its id field`,
      );
    }
  }
}

export function assertFields(action: Action, params: DevStatusParams): void {
  const { allowed, required } = ACTION_FIELDS[action];
  const allowedSet = new Set<Field>(allowed);

  const supplied = (Object.keys(params) as (keyof DevStatusParams)[]).filter(
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

  if ((action === "add" || action === "pending_add") && !params.patch?.id) {
    throw new Error(`action "${action}" requires patch.id`);
  }

  if (action === "prune" && params.force !== true) {
    throw new Error('action "prune" requires force: true to run');
  }
}

export function buildArgv(action: Action, params: DevStatusParams): string[] {
  const patchJson = () => JSON.stringify(params.patch);
  switch (action) {
    case "render":
      return ["render"];
    case "ready":
      return ["ready", ...(params.prefix ? ["--prefix", params.prefix] : [])];
    case "list":
      return [
        "list",
        ...(params.status ? ["--status", params.status] : []),
        ...(params.raw ? ["--raw"] : []),
      ];
    case "show":
      return ["show", params.slug!];
    case "add":
      return ["add", patchJson()];
    case "update":
      return ["update", params.slug!, patchJson()];
    case "start":
      return [
        "start",
        params.slug!,
        ...(params.force ? ["--force"] : []),
        ...(params.allowMain ? ["--allow-main"] : []),
        ...(params.claimedBy ? ["--claimed-by", params.claimedBy] : []),
      ];
    case "done":
      return ["done", params.slug!];
    case "review":
      return ["review", params.slug!];
    case "approve":
      return ["approve", params.slug!];
    case "reject":
      return ["reject", params.slug!, params.feedback!];
    case "gate_set":
      return ["gate-set", params.slug!, patchJson()];
    case "gate_pass":
      return ["gate-pass", params.slug!, ...(params.patch ? [patchJson()] : [])];
    case "run":
      return [
        "run",
        params.slug!,
        ...(params.timeout ? ["--timeout", String(params.timeout)] : []),
        "--",
        ...params.command!,
      ];
    case "runs":
      return ["runs", params.slug!];
    case "backfill_gate":
      return ["backfill-gate", ...(params.apply ? ["--apply"] : [])];
    case "rename":
      return ["rename", params.slug!, params.secondarySlug!];
    case "remove":
      return ["remove", params.slug!];
    case "block":
      return ["block", params.slug!, params.secondarySlug!];
    case "unblock":
      return ["unblock", params.slug!, params.secondarySlug!];
    case "prune":
      return ["prune", "--force"];
    case "recap":
      return [
        "recap",
        ...(params.refresh ? ["--refresh"] : []),
        ...(params.backend ? ["--backend", params.backend] : []),
      ];
    case "pending_add":
      return ["pending", "add", patchJson()];
    case "pending_update":
      return ["pending", "update", params.slug!, patchJson()];
    case "pending_list":
      return ["pending", "list"];
    case "out_of_scope_add":
      return [
        "out-of-scope",
        "add",
        params.slug!,
        "--reason-file",
        params.reasonFile!,
        ...(params.secondarySlug ? ["--related-item", params.secondarySlug] : []),
      ];
    case "out_of_scope_link":
      return ["out-of-scope", "link", params.slug!, params.secondarySlug!];
    case "out_of_scope_unlink":
      return ["out-of-scope", "unlink", params.slug!, params.secondarySlug!];
    case "out_of_scope_remove":
      return ["out-of-scope", "remove", params.slug!];
    case "out_of_scope_list":
      return ["out-of-scope", "list"];
    case "out_of_scope_show":
      return ["out-of-scope", "show", params.slug!];
  }
}

export default function (pi: ExtensionAPI) {
  pi.registerTool({
    name: "dev_status",
    label: "Backlog",
    description: "Read and mutate the personal dev_status.py backlog/pending store.",
    promptSnippet: "Read or mutate the personal backlog/pending store",
    promptGuidelines: [
      "Never invoke dev_status.py via bash, for any reason, including a plain read like listing pending items or checking status -- always use dev_status instead. This applies to every action, not just ones a slash command already told you to use dev_status for.",
      "dev_status covers everything dev_status.py's CLI does: render, list, ready, show, add, update, start, done, review, approve, reject, gate_set, gate_pass, run, runs, backfill_gate, rename, remove, block, unblock, prune, recap, pending_add, pending_update, pending_list, and the out_of_scope_* actions. If you're about to compose a `python3 ~/.claude/scripts/dev_status.py ...` bash command for any of these, use dev_status with the matching action instead.",
      "dev_status's patch field is a plain object, not a JSON string -- never hand-encode it.",
      'dev_status refuses a numeric slug on any mutating action -- call action: "show" first to resolve a numeric position to its real slug.',
      "start refuses to run from a main/master checkout (worktree guard) or when the item is actively claimed by another live session (claim collision) -- pass allowMain or force respectively to override, or claimedBy to correct a wrong auto-detected harness name.",
    ],
    parameters: Type.Object({
      action: StringEnum(ACTIONS),
      slug: Type.Optional(
        Type.String({
          description:
            "Primary identifier: backlog item slug or number, or out-of-scope concept slug.",
        }),
      ),
      secondarySlug: Type.Optional(
        Type.String({
          description:
            "Secondary identifier: new slug (rename), blocker slug (block/unblock), backlog slug (out_of_scope_link/unlink), or related item (out_of_scope_add).",
        }),
      ),
      patch: Type.Optional(
        Type.Record(Type.String(), Type.Unknown(), {
          description:
            "JSON patch body for update/gate_set/pending_update, or the new-item body for add/pending_add " +
            "(id goes inside patch, e.g. patch.id -- never also pass slug for those two actions). Common keys: " +
            "summary, context, next_steps, priority, category, status, related_files (array of {path, note}), " +
            "blocked_by (array of slugs); gate_set wants {required: boolean, criteria: string[]}; gate_pass " +
            'wants {coverage: {"<criterion#>": "run:<run_id>" or "manual:<note>"}}; pending_add ' +
            "wants {id, description, kind, source_ref?, context?, next_steps?}.",
        }),
      ),
      feedback: Type.Optional(Type.String({ description: "reject: rejection feedback text." })),
      status: Type.Optional(Type.String({ description: "list: filter by status." })),
      raw: Type.Optional(Type.Boolean({ description: "list: machine-readable TSV output." })),
      prefix: Type.Optional(
        Type.String({
          description: 'ready: only items whose slug starts with this, e.g. "meta-".',
        }),
      ),
      apply: Type.Optional(
        Type.Boolean({ description: "backfill_gate: write changes instead of a dry run." }),
      ),
      force: Type.Optional(
        Type.Boolean({
          description:
            "prune: must be true -- confirms the destructive prune. start: take over " +
            "an item actively claimed by another live session.",
        }),
      ),
      allowMain: Type.Optional(
        Type.Boolean({
          description:
            "start: allow starting from a main/master checkout, bypassing the worktree guard.",
        }),
      ),
      claimedBy: Type.Optional(
        Type.String({
          description: "start: override the auto-detected claiming harness name.",
        }),
      ),
      refresh: Type.Optional(Type.Boolean({ description: "recap: bypass the freshness cache." })),
      backend: Type.Optional(Type.String({ description: "recap: force this backend." })),
      reasonFile: Type.Optional(
        Type.String({ description: "out_of_scope_add: path to a file with the rejection reason." }),
      ),
      command: Type.Optional(
        Type.Array(Type.String(), {
          description:
            "run: argv to execute and record as evidence -- pre-tokenized, executed " +
            'without a shell (e.g. ["pytest", "-q"]).',
        }),
      ),
      timeout: Type.Optional(
        Type.Number({
          description: "run: kill the command after this many seconds (default 1800).",
        }),
      ),
    }),
    async execute(_toolCallId, params, signal) {
      const typed = params as DevStatusParams;

      assertNotNumericIdentity(typed.action, typed);
      assertFields(typed.action, typed);

      const argv = buildArgv(typed.action, typed);

      // pi.exec's ExecOptions has no `env` field (confirmed against the
      // real, installed dist/core/exec.d.ts -- {signal?, timeout?, cwd?}
      // only), so DEVSTATUS_AGENT=1 is set via the `env` coreutil as the
      // actual command, not via a (nonexistent) options.env.
      const result = await pi.exec(
        "env",
        ["DEVSTATUS_AGENT=1", "python3", DEV_STATUS_PATH, ...argv],
        { signal },
      );

      if (result.code !== 0) {
        throw new Error(result.stderr || result.stdout || `dev_status.py exited ${result.code}`);
      }

      const text = result.stderr ? `${result.stdout}\n\n${result.stderr}` : result.stdout;

      return {
        content: [{ type: "text", text }],
        details: { stdout: result.stdout, stderr: result.stderr },
      };
    },
  });
}
