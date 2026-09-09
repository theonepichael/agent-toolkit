// copilot/extensions/swarm/src/swarm-picker.ts
function classifyBlock(rawPrompt) {
  return "needs_human";
}
function parsePicker(content) {
  return { selectedIndex: null, options: [] };
}
function pickerLabels(rawPrompt) {
  return [];
}
function noteResolveFailure(worker, answer, reason, now) {
  worker.lastResolveFailure = { answer, reason, at: now };
}
export {
  classifyBlock,
  noteResolveFailure,
  parsePicker,
  pickerLabels
};
