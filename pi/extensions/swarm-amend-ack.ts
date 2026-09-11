/**
 * Swarm-worker amend acknowledgement.
 *
 * When a pi swarm worker receives `AMEND_INSTRUCTION` (the content-free
 * re-read prompt), write a sidecar next to `PI_SWARM_CAPTURE_FILE` so the
 * orchestrator can treat receipt as positive turn identity instead of the
 * two-axis timing inference. Attended sessions have no capture env and
 * no-op. The model never sees this file.
 *
 * The input is NOT handled: it must still reach the agent.
 */
import { renameSync, writeFileSync } from "node:fs";
import type { ExtensionAPI, InputEvent } from "@earendil-works/pi-coding-agent";
import { AMEND_INSTRUCTION, amendAckPath, type AmendAckPayload } from "./swarm-lib/swarm-herdr";

export const CAPTURE_ENV = "PI_SWARM_CAPTURE_FILE";

export function captureFileFromEnv(env: NodeJS.Dict<string>): string | undefined {
  const value = env[CAPTURE_ENV];
  return value && value.length > 0 ? value : undefined;
}

export function writeAmendAckFile(captureFile: string, payload: AmendAckPayload): void {
  const path = amendAckPath(captureFile);
  const tmp = `${path}.tmp`;
  writeFileSync(tmp, `${JSON.stringify(payload)}\n`, "utf8");
  renameSync(tmp, path);
}

export function handleAmendInput(opts: {
  text: string;
  streamingBehavior?: InputEvent["streamingBehavior"];
  env?: NodeJS.Dict<string>;
  now?: number;
  write?: (captureFile: string, payload: AmendAckPayload) => void;
}): boolean {
  const capture = captureFileFromEnv(opts.env ?? process.env);
  if (!capture) return false;
  if (opts.text !== AMEND_INSTRUCTION) return false;
  const payload: AmendAckPayload = {
    t: opts.now ?? Date.now(),
    streamingBehavior: opts.streamingBehavior,
  };
  (opts.write ?? writeAmendAckFile)(capture, payload);
  return true;
}

export default function swarmAmendAck(pi: ExtensionAPI): void {
  pi.on("input", async (event: InputEvent) => {
    try {
      handleAmendInput({
        text: event.text,
        streamingBehavior: event.streamingBehavior,
      });
    } catch {
      // Fail open: a write error must not swallow the amendment.
    }
  });
}
