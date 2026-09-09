// Stub picker for Copilot CLI swarm: Copilot workers do not render pi's question-tool
// interactive terminal picker, so any block defaults to needs_human.
import type { WorkerRecord } from "./swarm-scheduling";

export type BlockClass = "answerable" | "needs_human";

export interface RenderedOption {
  index: number;
  label: string;
}

export interface ParsedPicker {
  selectedIndex: number | null;
  options: RenderedOption[];
}

export function classifyBlock(rawPrompt: string | undefined): BlockClass {
  return "needs_human";
}

export function parsePicker(content: string): ParsedPicker {
  return { selectedIndex: null, options: [] };
}

export function pickerLabels(rawPrompt: string | undefined): string[] {
  return [];
}

export function noteResolveFailure(
  worker: WorkerRecord,
  answer: string,
  reason: string,
  now: number,
): void {
  worker.lastResolveFailure = { answer, reason, at: now };
}
