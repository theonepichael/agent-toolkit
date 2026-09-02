/**
 * Pending Plan Surface
 *
 * Makes grill-me's clear-and-go resume first-class in pi: when a session
 * starts and a grilled plan is flagged as pending-execution (set via
 * `grill.py mark-pending-execution`), the user gets a banner and the model
 * gets a one-time context hint — so saying "resume the plan" is enough.
 *
 * The extension NEVER passes --consume: the one-shot handout stays reserved
 * for the explicit resume, which the agent performs through the grill tool's
 * `pending_plan` action with `consume: true`. Each `session_start` re-probes
 * the store, so a mid-session consume makes later sessions go quiet.
 */

import { homedir } from "node:os";
import { join } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const GRILL_PATH = join(homedir(), ".claude", "scripts", "grill.py");

const PENDING_MARK = "Grill plan ready to execute: ";
const PLAN_LINE = "   Plan: ";
const BACKLOG_MARK = "Resume via: /backlog-item ";

const MAX_SLUG_LENGTH = 64;

export interface PendingPlan {
  sessionSlug: string;
  planPath?: string;
  backlogSlug?: string;
}

/** Truncate a string to `max` chars, deterministic, single line. */
export function truncate(value: string, max: number = MAX_SLUG_LENGTH): string {
  const clean = value.replace(/\s+/g, " ").trim();
  if (clean.length <= max) {
    return clean;
  }
  return `${clean.slice(0, max - 1)}…`;
}

/**
 * Classify `grill.py pending-plan` stdout (no --consume): returns the parsed
 * pending plan, or null when nothing is pending or the output is
 * unrecognized (version drift must degrade to "nothing pending", never crash).
 */
export function parsePendingOutput(stdout: string): PendingPlan | null {
  const markerLine = stdout.split("\n").find((line) => line.includes(PENDING_MARK));
  if (!markerLine) {
    return null;
  }
  const markerIndex = markerLine.indexOf(PENDING_MARK);
  const sessionSlug = markerLine.slice(markerIndex + PENDING_MARK.length).trim();
  if (!sessionSlug) {
    return null;
  }

  let planPath: string | undefined;
  let backlogSlug: string | undefined;
  for (const line of stdout.split("\n")) {
    if (line.startsWith(PLAN_LINE)) {
      const value = line.slice(PLAN_LINE.length).trim();
      if (value) {
        planPath = value;
      }
    }
    const backlogIndex = line.indexOf(BACKLOG_MARK);
    if (backlogIndex >= 0) {
      const value = line.slice(backlogIndex + BACKLOG_MARK.length).trim();
      if (value) {
        backlogSlug = value.split(/\s+/)[0];
      }
    }
  }

  return { sessionSlug, planPath, backlogSlug };
}

/** User-facing banner text (TUI notification). */
export function bannerText(plan: PendingPlan): string {
  return `Pending grilled plan: ${truncate(plan.sessionSlug)} — say "resume the plan" to pick it up`;
}

/** Model-facing hint injected on the session's first prompt. */
export function resumeHint(plan: PendingPlan): string {
  const parts = [
    `A pending grilled plan was detected at session start: ${truncate(plan.sessionSlug)}.`,
  ];
  if (plan.planPath) {
    parts.push(`Plan file: ${plan.planPath}.`);
  }
  if (plan.backlogSlug) {
    parts.push(
      `It is linked to backlog item "${plan.backlogSlug}" — resume through that item's own flow (backlog-item skill) rather than implementing the plan file directly.`,
    );
  }
  parts.push(
    "If the user asks to resume / pick up / execute this plan, run the grill tool's `pending_plan` action with `consume: true` to receive the one-shot handout, then act on the printed instructions.",
  );
  parts.push(
    "Consume it only when the user asks to resume this plan — for any other request, ignore this notice and do not consume the pending plan.",
  );
  return parts.join(" ");
}

/**
 * One probe per session start; injection armed until the first prompt.
 * Re-probing on every `session_start` (new/resume/fork) reflects a
 * mid-session consume: the next session sees an empty store and stays quiet.
 */
export default function (pi: ExtensionAPI) {
  let pending: PendingPlan | null = null;
  let injected = false;

  pi.on("session_start", async (event, ctx) => {
    pending = null;
    injected = false;

    try {
      const result = await pi.exec("python3", [GRILL_PATH, "pending-plan"]);
      if (result.code === 0) {
        pending = parsePendingOutput(result.stdout);
      }
    } catch {
      return; // Broken probe must never break session startup.
    }

    if (pending && ctx.mode === "tui") {
      ctx.ui.notify(bannerText(pending), "info");
    }
  });

  pi.on("before_agent_start", async () => {
    if (!pending || injected) {
      return undefined;
    }
    injected = true;
    return {
      message: {
        customType: "pending-plan-surface",
        content: resumeHint(pending),
        display: true,
      },
    };
  });
}
