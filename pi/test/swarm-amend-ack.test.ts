import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, test } from "./helpers/tap";
import { AMEND_INSTRUCTION } from "../extensions/swarm-lib/swarm-herdr";
import {
  amendAckConfirms,
  amendAckPath,
  parseAmendAck,
  sidecarUpgradesHold,
} from "../extensions/swarm-lib/swarm-herdr";
import { handleAmendInput, writeAmendAckFile } from "../extensions/swarm-amend-ack";

describe("amendAckPath", () => {
  test("derives a per-worker sidecar from the capture file, in the same directory", () => {
    expect(amendAckPath("/state/swarm-r1-capture-my-slug.json")).toBe(
      "/state/swarm-r1-amend-ack-my-slug.json",
    );
  });

  test("two workers in one state dir do not share a sidecar", () => {
    const a = amendAckPath("/state/swarm-r1-capture-one.json");
    const b = amendAckPath("/state/swarm-r1-capture-two.json");
    expect(a).not.toBe(b);
    expect(a).toBe("/state/swarm-r1-amend-ack-one.json");
    expect(b).toBe("/state/swarm-r1-amend-ack-two.json");
  });
});

describe("parseAmendAck / amendAckConfirms / sidecarUpgradesHold", () => {
  test("a payload whose t is at or after requestedAtMs confirms", () => {
    expect(amendAckConfirms({ t: 1000 }, 1000)).toBe(true);
    expect(amendAckConfirms({ t: 1001 }, 1000)).toBe(true);
  });

  test("a payload whose t is before requestedAtMs is a zombie, not a confirm", () => {
    expect(amendAckConfirms({ t: 999 }, 1000)).toBe(false);
  });

  test("garbage JSON is not an ack", () => {
    expect(parseAmendAck("nope")).toBeNull();
    expect(parseAmendAck("{}")).toBeNull();
    expect(parseAmendAck(JSON.stringify({ t: "later" }))).toBeNull();
  });

  test("Copilot never upgrades from a sidecar, even a fresh one", () => {
    expect(sidecarUpgradesHold("copilot", { t: 2000 }, 1000)).toBe(false);
  });

  test("pi upgrades only when the sidecar confirms", () => {
    expect(sidecarUpgradesHold("pi", null, 1000)).toBe(false);
    expect(sidecarUpgradesHold("pi", { t: 999 }, 1000)).toBe(false);
    expect(sidecarUpgradesHold("pi", { t: 1000 }, 1000)).toBe(true);
  });
});

describe("handleAmendInput", () => {
  let dir: string;

  afterEach(() => {
    if (dir) rmSync(dir, { recursive: true, force: true });
  });

  test("attended sessions (no capture env) write nothing", () => {
    const wrote: string[] = [];
    const ok = handleAmendInput({
      text: AMEND_INSTRUCTION,
      env: {},
      write: (path) => wrote.push(path),
    });
    expect(ok).toBe(false);
    expect(wrote).toHaveLength(0);
  });

  test("a non-amend prompt does not write, even in a swarm worker", () => {
    const wrote: string[] = [];
    const ok = handleAmendInput({
      text: "hello",
      env: { PI_SWARM_CAPTURE_FILE: "/state/swarm-r1-capture-x.json" },
      write: (path) => wrote.push(path),
    });
    expect(ok).toBe(false);
    expect(wrote).toHaveLength(0);
  });

  test("an amend in a swarm worker writes the sidecar next to the capture file", () => {
    dir = mkdtempSync(join(tmpdir(), "amend-ack-ext-"));
    const capture = join(dir, "swarm-r1-capture-x.json");
    writeFileSync(capture, "{}");
    const ok = handleAmendInput({
      text: AMEND_INSTRUCTION,
      streamingBehavior: "steer",
      env: { PI_SWARM_CAPTURE_FILE: capture },
      now: 1234,
    });
    expect(ok).toBe(true);
    const ackPath = amendAckPath(capture);
    expect(existsSync(ackPath)).toBe(true);
    const parsed = parseAmendAck(readFileSync(ackPath, "utf8"));
    expect(parsed).toEqual({ t: 1234, streamingBehavior: "steer" });
    expect(existsSync(`${ackPath}.tmp`)).toBe(false);
  });
});

describe("writeAmendAckFile", () => {
  test("tmp+rename leaves no .tmp behind on success", () => {
    const dir = mkdtempSync(join(tmpdir(), "amend-ack-write-"));
    try {
      const capture = join(dir, "swarm-r1-capture-x.json");
      writeAmendAckFile(capture, { t: 1, streamingBehavior: "followUp" });
      const ackPath = amendAckPath(capture);
      expect(existsSync(ackPath)).toBe(true);
      expect(existsSync(`${ackPath}.tmp`)).toBe(false);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});
