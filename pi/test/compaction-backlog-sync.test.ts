import { describe, expect, test } from "bun:test";
import type { SessionCompactEvent } from "@earendil-works/pi-coding-agent";
import {
  buildCompactionSummaryData,
  buildMergedNextSteps,
  parseBacklogListRaw,
} from "../extensions/compaction-backlog-sync";

function makeEvent(details?: {
  readFiles?: string[];
  modifiedFiles?: string[];
}): SessionCompactEvent {
  return {
    type: "session_compact",
    compactionEntry: {
      type: "compaction",
      id: "e1",
      parentId: null,
      timestamp: "2026-08-31T12:00:00.000Z",
      summary: "did the thing",
      firstKeptEntryId: "e0",
      tokensBefore: 1000,
      details,
    },
    fromExtension: false,
    reason: "threshold",
    willRetry: false,
  };
}

describe("buildCompactionSummaryData", () => {
  test("carries summary, reason, timestamp, and file lists through", () => {
    const data = buildCompactionSummaryData(
      makeEvent({ readFiles: ["a.ts"], modifiedFiles: ["b.ts"] }),
    );
    expect(data).toEqual({
      summary: "did the thing",
      readFiles: ["a.ts"],
      modifiedFiles: ["b.ts"],
      reason: "threshold",
      timestamp: "2026-08-31T12:00:00.000Z",
    });
  });

  test("missing details defaults both file lists to empty arrays", () => {
    const data = buildCompactionSummaryData(makeEvent(undefined));
    expect(data.readFiles).toEqual([]);
    expect(data.modifiedFiles).toEqual([]);
  });
});

describe("parseBacklogListRaw", () => {
  test("skips the leading rev comment and parses tab-separated rows", () => {
    const stdout =
      "# rev=1727\nfoo\tin-progress\tDo the thing\nbar\tin-progress\tDo another thing\n";
    expect(parseBacklogListRaw(stdout)).toEqual([
      { slug: "foo", status: "in-progress", summary: "Do the thing" },
      { slug: "bar", status: "in-progress", summary: "Do another thing" },
    ]);
  });

  test("empty stdout yields no rows", () => {
    expect(parseBacklogListRaw("")).toEqual([]);
  });

  test("blank lines and slug-less rows are skipped", () => {
    const stdout = "# rev=1\n\n\tin-progress\tno slug\nfoo\tin-progress\tok\n";
    expect(parseBacklogListRaw(stdout)).toEqual([
      { slug: "foo", status: "in-progress", summary: "ok" },
    ]);
  });
});

describe("buildMergedNextSteps", () => {
  const data = {
    summary: "did the thing",
    readFiles: [],
    modifiedFiles: [],
    reason: "threshold" as const,
    timestamp: "2026-08-31T12:00:00.000Z",
  };

  test("empty existing next_steps: just the compaction block, no separator", () => {
    expect(buildMergedNextSteps("", data)).toBe(
      "From compaction (threshold, 2026-08-31T12:00:00.000Z):\ndid the thing",
    );
  });

  test("whitespace-only existing next_steps counts as empty", () => {
    expect(buildMergedNextSteps("   \n  ", data)).toBe(
      "From compaction (threshold, 2026-08-31T12:00:00.000Z):\ndid the thing",
    );
  });

  test("non-empty existing next_steps: existing text first, then separator, then compaction block", () => {
    const merged = buildMergedNextSteps("pick up from step 3", data);
    expect(merged).toBe(
      "pick up from step 3\n\n---\nFrom compaction (threshold, 2026-08-31T12:00:00.000Z):\ndid the thing",
    );
  });
});
