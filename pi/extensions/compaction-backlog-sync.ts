import { homedir } from "node:os";
import { join } from "node:path";
import type { ExtensionAPI, SessionCompactEvent } from "@earendil-works/pi-coding-agent";

// Wraps claude/scripts/dev_status.py's `update`, following the
// pi.exec/DEVSTATUS_AGENT pattern set by dev-status-tool.ts. See
// ~/.claude/data/grill/pi-compaction-backlog-sync-spec.md for the full
// design and ~/.claude/data/grill/pi-native-context-management-findings-critique-notes.md
// for why this never auto-writes: session_compact fires with no user
// present to resolve a target-item choice, confirm the merged text, or
// retry a failed write, so every dev_status.py mutation here is triggered
// by the user explicitly running /backlog-sync, never by the hook itself.

const DEV_STATUS_PATH = join(homedir(), ".claude", "scripts", "dev_status.py");

export const COMPACTION_SUMMARY_ENTRY_TYPE = "compaction-backlog-summary";

export interface CompactionSummaryData {
  summary: string;
  readFiles: string[];
  modifiedFiles: string[];
  reason: string;
  // SessionEntryBase.timestamp is a string, not a number, per the
  // installed package's dist/core/session-manager.d.ts (extensions.md's
  // inline example is stale on this field).
  timestamp: string;
}

/** Build the appendEntry payload from a session_compact event. */
export function buildCompactionSummaryData(event: SessionCompactEvent): CompactionSummaryData {
  const details = event.compactionEntry.details as
    { readFiles?: string[]; modifiedFiles?: string[] } | undefined;
  return {
    summary: event.compactionEntry.summary,
    readFiles: details?.readFiles ?? [],
    modifiedFiles: details?.modifiedFiles ?? [],
    reason: event.reason,
    timestamp: event.compactionEntry.timestamp,
  };
}

export interface BacklogListRow {
  slug: string;
  status: string;
  summary: string;
}

/**
 * Parse `dev_status.py list --status in-progress --raw` stdout into rows.
 * Skips the leading `# rev=N` comment line.
 */
export function parseBacklogListRaw(stdout: string): BacklogListRow[] {
  return stdout
    .split("\n")
    .filter((line) => line.trim() !== "" && !line.startsWith("#"))
    .map((line) => {
      const [slug, status, summary] = line.split("\t");
      return { slug: slug ?? "", status: status ?? "", summary: summary ?? "" };
    })
    .filter((row) => row.slug !== "");
}

/**
 * Build the merged next_steps text: existing text (if any), a separator,
 * then the compaction summary block. Never drops existing text --
 * dev_status.py update's patch is a shallow replace, so this extension
 * owns not clobbering it.
 */
export function buildMergedNextSteps(
  existingNextSteps: string,
  data: CompactionSummaryData,
): string {
  const isoTimestamp = new Date(data.timestamp).toISOString();
  const compactionBlock = `From compaction (${data.reason}, ${isoTimestamp}):\n${data.summary}`;
  const trimmedExisting = existingNextSteps.trim();
  if (trimmedExisting === "") {
    return compactionBlock;
  }
  return `${trimmedExisting}\n\n---\n${compactionBlock}`;
}

interface DevStatusItem {
  next_steps?: string;
}

export default function (pi: ExtensionAPI) {
  pi.on("session_compact", async (event) => {
    pi.appendEntry(COMPACTION_SUMMARY_ENTRY_TYPE, buildCompactionSummaryData(event));
  });

  pi.registerCommand("backlog-sync", {
    description:
      "Merge the most recent compaction summary into a dev_status.py backlog item's next_steps",
    handler: async (_args, ctx) => {
      const entries = ctx.sessionManager.getEntries();
      let latestData: CompactionSummaryData | undefined;
      for (const entry of entries) {
        if (entry.type === "custom" && entry.customType === COMPACTION_SUMMARY_ENTRY_TYPE) {
          latestData = entry.data as CompactionSummaryData;
        }
      }
      if (!latestData) {
        ctx.ui.notify("No compaction summary in this session yet", "warning");
        return;
      }

      const listResult = await pi.exec(
        "env",
        [
          "DEVSTATUS_AGENT=1",
          "python3",
          DEV_STATUS_PATH,
          "list",
          "--status",
          "in-progress",
          "--raw",
        ],
        { signal: ctx.signal },
      );
      if (listResult.code !== 0) {
        ctx.ui.notify(
          listResult.stderr || listResult.stdout || "dev_status.py list failed",
          "error",
        );
        return;
      }
      const rows = parseBacklogListRaw(listResult.stdout);
      if (rows.length === 0) {
        ctx.ui.notify("No IN PROGRESS backlog items to sync into", "warning");
        return;
      }

      const labels = rows.map((row) => `${row.slug} — ${row.summary}`);
      const choiceLabel = await ctx.ui.select("Sync compaction summary into which item?", labels);
      if (choiceLabel === undefined || choiceLabel === null) {
        return;
      }
      const chosenIndex = labels.indexOf(choiceLabel);
      const chosen = rows[chosenIndex];
      if (!chosen) {
        return;
      }

      const showResult = await pi.exec(
        "env",
        ["DEVSTATUS_AGENT=1", "python3", DEV_STATUS_PATH, "show", chosen.slug],
        { signal: ctx.signal },
      );
      if (showResult.code !== 0) {
        ctx.ui.notify(
          showResult.stderr || showResult.stdout || "dev_status.py show failed",
          "error",
        );
        return;
      }
      const jsonStart = showResult.stdout.indexOf("{");
      const item = JSON.parse(showResult.stdout.slice(jsonStart)) as DevStatusItem;
      const mergedNextSteps = buildMergedNextSteps(item.next_steps ?? "", latestData);

      const confirmed = await ctx.ui.confirm(
        `Update ${chosen.slug}?`,
        `Current next_steps:\n${item.next_steps ?? "(empty)"}\n\nNew next_steps:\n${mergedNextSteps}`,
      );
      if (!confirmed) {
        ctx.ui.notify("Cancelled", "info");
        return;
      }

      const updateResult = await pi.exec(
        "env",
        [
          "DEVSTATUS_AGENT=1",
          "python3",
          DEV_STATUS_PATH,
          "update",
          chosen.slug,
          JSON.stringify({ next_steps: mergedNextSteps }),
        ],
        { signal: ctx.signal },
      );
      if (updateResult.code !== 0) {
        ctx.ui.notify(
          updateResult.stderr || updateResult.stdout || "dev_status.py update failed",
          "error",
        );
        return;
      }

      ctx.ui.notify(`Synced into ${chosen.slug}`, "info");
    },
  });
}
