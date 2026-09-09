// pi/extensions/swarm-lib/swarm-picker-copilot.ts
function classifyBlock(_rawPrompt) {
  return "needs_human";
}
function parsePicker(_content) {
  return { selectedIndex: null, options: [] };
}
function pickerLabels(_rawPrompt) {
  return [];
}
function noteResolveFailure(worker, answer, reason, now) {
  worker.lastResolveFailure = { answer, reason, at: now };
}
var copilotPickerAdapter = {
  classifyBlock,
  parsePicker,
  pickerLabels
};
export {
  classifyBlock,
  copilotPickerAdapter,
  noteResolveFailure,
  parsePicker,
  pickerLabels
};
