// Stub picker for Copilot CLI swarm: Copilot workers do not render pi's question-tool
// interactive terminal picker, so any block defaults to needs_human.
//
// The shared context receives this adapter from Copilot's entry point. It is
// intentionally a needs_human-only implementation because Copilot workers do
// not render Pi's interactive picker.
import type { WorkerRecord } from "./swarm-scheduling";
import type { PickerAdapter } from "./swarm-picker";

export type BlockClass = "answerable" | "needs_human";

export interface RenderedOption {
  index: number;
  label: string;
}

export interface ParsedPicker {
  selectedIndex: number | null;
  options: RenderedOption[];
}

export function classifyBlock(_rawPrompt: string | undefined): BlockClass {
  return "needs_human";
}

export function parsePicker(_content: string): ParsedPicker {
  return { selectedIndex: null, options: [] };
}

export function pickerLabels(_rawPrompt: string | undefined): string[] {
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

export const copilotPickerAdapter: PickerAdapter = {
  classifyBlock,
  parsePicker,
  pickerLabels,
};
