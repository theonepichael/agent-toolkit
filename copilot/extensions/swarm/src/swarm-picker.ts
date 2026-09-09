// Stub picker for Copilot CLI swarm: Copilot workers do not render pi's question-tool
// interactive terminal picker, so any block defaults to needs_human.
//
// UNLIKE swarm-scheduling.ts and swarm-herdr.ts in this same directory (both
// built straight from pi/extensions/swarm-lib/ instead), this file is a deliberately
// different implementation, not a fork awaiting unification. Do not delete
// it or repoint scripts/build-copilot-swarm.sh at pi's real parser; a
// follow-up item wires pi/extensions/swarm-lib/swarm-picker.ts's
// PickerAdapter interface into a shared class instead, at which point this
// file's implementation becomes that class's Copilot-side StubPickerAdapter.
import type { WorkerRecord } from "../../../../pi/extensions/swarm-lib/swarm-scheduling";

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
