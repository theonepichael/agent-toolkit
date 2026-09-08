// Pure parsing and navigation for question-tool.ts's rendered option
// picker -- what `swarm_resolve_blocked` reads off a blocked worker's pane
// and the arrow-key path it drives to answer it. Extracted verbatim from
// swarm-tool.ts; no module state, no I/O.
import type { WorkerRecord } from "./swarm-scheduling";
/**
 * Whether a blocked worker's prompt is one the swarm can answer.
 *
 * Detection of blocked-ness is already class-wide -- herdr-blocked-bridge.ts
 * listens to pi's ui_prompt_start, which fires for every blocking `ctx.ui.*`
 * prompt -- but ANSWERING is not. `swarm_resolve_blocked` drives
 * question-tool.ts's numbered picker by arrow key, and nothing else. A
 * guard-rails `rm -rf` confirmation, a `/compact` select, or any pi built-in
 * prompt parks the worker just the same, and the orchestrator had no way to
 * tell the two apart: both simply appeared as `blocked`.
 *
 * So an unattended batch that hit a non-picker prompt stopped making progress
 * with no signal naming the pane or the reason (observed live 2026-09-02,
 * cleared by a human send-keys).
 */
export type BlockClass = "answerable" | "needs_human";

/**
 * Classify a blocked worker's captured pane as answerable or not.
 *
 * The test is exactly what `swarm_resolve_blocked` can actually drive: a
 * question-tool picker that `parsePicker` recognises. That is deliberate --
 * the classifier must never claim answerable for something the relay would
 * then fail on, so it reuses the same parser rather than a looser heuristic
 * of its own.
 *
 * An absent or empty capture is `needs_human`, not `answerable`. The read
 * failed or the pane was empty, and guessing answerable would send arrow
 * keys at a prompt nobody has read.
 *
 * This does NOT teach the relay to answer confirm dialogs. Approving
 * arbitrary bash on a human's behalf is what the gate exists to prevent;
 * `parsePicker` refusing them is correct and stays. The point here is only
 * to say WHICH kind of block this is, so the orchestrator can relay one and
 * escalate the other instead of treating every block alike.
 */
export function classifyBlock(rawPrompt: string | undefined): BlockClass {
  if (!rawPrompt) return "needs_human";
  return parsePicker(rawPrompt).options.length > 0 ? "answerable" : "needs_human";
}

/**
 * Workers parked awaiting a relay for longer than `stallMs`.
 *
 * A SECOND clock, deliberately separate from the working-time budget. That
 * budget measures time a worker spends WORKING and is folded shut the moment
 * it parks, precisely so hours spent waiting on a human are never charged
 * against it -- charging them would stop a worker at the instant its relay
 * was finally answered. The consequence is that a parked worker is otherwise
 * completely unbounded, which is the gap this closes: the budget bounds a
 * worker that is working, this bounds one waiting on a human who may never
 * come.
 *
 * An `active` worker is never stalled here however long it has run -- that is
 * the budget's business, and double-reporting it would make a busy worker
 * look stuck.
 *
 * A parked record with no stamp is not reported. Such a record was written
 * before this existed, and inventing a stall for it would fire a spurious
 * escalation on the first poll after an upgrade. The caller stamps it
 * instead, so it is bounded from that moment on.
 */
/**
 * The option labels a blocked worker is really rendering.
 *
 * `swarm_resolve_blocked` matches an answer against these exact strings, read
 * fresh off the pane -- so an orchestrator that composes its OWN labels for
 * the human ("Merge, push, clean up" for a worker showing "Leave it
 * unlanded") produces an answer that matches nothing. The resolve returns
 * needs_manual, the worker is never moved off awaiting_relay, and the run
 * strands with no message explaining why.
 *
 * Putting them on the event is what lets the orchestrator mirror them
 * structurally instead of reading them out of prose and retyping them. Three
 * of four workers in one run stranded this way on 2026-09-03; the one that
 * did not was the one where the orchestrator had just been corrected by hand,
 * and the correction did not survive to the next worker.
 */
export function pickerLabels(rawPrompt: string | undefined): string[] {
  if (!rawPrompt) return [];
  return parsePicker(rawPrompt).options.map((o) => o.label);
}

/** Record a relay answer that matched no listed option, so a later poll can say so. */
export function noteResolveFailure(
  worker: WorkerRecord,
  answer: string,
  reason: string,
  now: number,
): void {
  worker.lastResolveFailure = { answer, reason, at: now };
}

// ---------------------------------------------------------------------------
// Answering a blocked worker's picker -- `herdr agent prompt` refuses a
// blocked agent outright (agent_blocked error, confirmed live) and there is
// no `agent send-text` for literal input. The only real path is driving
// question-tool.ts's rendered picker via `agent send-keys` (arrow-key
// navigation, confirmed live: "down" moves the `>` marker, "enter" submits).
// Free-text answers matching no listed option aren't auto-answerable this
// way -- see swarm_resolve_blocked's needsManual outcome below.
// ---------------------------------------------------------------------------

export interface RenderedOption {
  index: number;
  label: string;
}

export interface ParsedPicker {
  selectedIndex: number | null;
  options: RenderedOption[];
}

const OPTION_LINE = /^\s*(>)?\s*(\d+)\.\s+(.+?)\s*$/;

const OTHER_OPTION_LABEL = "Something else (type it)";

/**
 * question-tool.ts renders each option with a 2-visible-column prefix ("> " highlighted,
 * "  " not) and wraps the label at renderWidth - 2, continuation lines carrying a 2-space
 * prefix; descriptions (and their continuations) carry a 5-space prefix. In a narrow worker
 * pane the LABEL wraps too, and a capture that keeps only the first line collapses two
 * options that share their first word ("Merge + push + cleanup (Recommended)" and
 * "Merge and push only" both became "Merge" on 2026-09-07) -- unresolvable for matchOption.
 * Continuations are therefore reattached, keyed off the option line's own number column
 * (the char offset of its \\d+, with the marker counted as its 2 visible columns -- captures
 * are ANSI-stripped, so chars are columns). Both "  1. X" and "> 1. X" put the number at
 * column 2, and a globally padded capture shifts number and continuations equally, so the
 * baseline is relative, never an absolute indent. Descriptions start exactly 3 columns past
 * the number (5 - 2), so anything at or beyond baseline + 3 is skipped.
 */
const DESCRIPTION_COLUMN_OFFSET = 3;

/**
 * A line that still SMELLS like an option but fails OPTION_LINE -- a multi-select picker's
 * toggled row ("[x] 2. Push only") is the live case -- must never be appended to the
 * previous option's label as if it were a wrapped continuation. Today's parser skipped it
 * and the broken numbering degraded the window to needs_manual; appending would instead
 * corrupt a neighboring label, which is a worse way to fail. Skip it, keep the state, and
 * let contiguity refuse the window as before.
 */
const SMELLS_LIKE_OPTION = /^\d+\.\s/;

/**
 * question-tool.ts's picker closes with one of exactly three hint lines --
 * select, multi-select, and the free-text edit mode -- and wraps the whole
 * block in full-width accent rules. Those are the only marks in the pane that
 * belong to the live picker and nothing else, so they are what the parse
 * anchors on.
 *
 * Matched on the OPENING of the hint line only, never the whole of it. pi
 * wraps the footer to the pane width before it is ever captured, and a worker
 * pane in a real three-way split is 42 columns, where
 * "↑↓ navigate • Enter to select • Esc to cancel" breaks after "Esc to". An
 * anchor spanning the full sentence matched nothing there, so parsePicker
 * returned no options and every relay came back needs_manual with "(none
 * parsed)" -- observed live on 2026-09-02. Both openings are short enough to
 * survive any width the pane guard permits.
 */
const PICKER_FOOTER = /↑↓ navigate|Enter to submit/;

const PICKER_RULE = /^─{3,}\s*$/;

/**
 * Parse question-tool.ts's rendered picker (plain `recent-unwrapped` text, no
 * ANSI) into its option list and current selection.
 *
 * Anchored to the LAST rendered picker rather than scanning the whole
 * capture, because `agent read` hands back 500 lines of scrollback and any
 * numbered line in it used to parse as an option. Observed on 2026-09-02: a
 * plan list sitting above a real picker contributed "1. Merge to main and
 * push" as option 1, and the navigation keys were then computed from that
 * fabricated index -- submitting whatever happened to sit at the resulting
 * offset. Reading the wrong list is worse than reading none, so anything
 * unexpected inside the window yields no options at all and the caller falls
 * back to needs_manual.
 */
export function parsePicker(content: string): ParsedPicker {
  const lines = content.split("\n");

  let footer = -1;
  for (let i = lines.length - 1; i >= 0; i--) {
    if (PICKER_FOOTER.test(lines[i]!)) {
      footer = i;
      break;
    }
  }
  if (footer === -1) return { selectedIndex: null, options: [] };

  let opening = -1;
  for (let i = footer - 1; i >= 0; i--) {
    if (PICKER_RULE.test(lines[i]!)) {
      opening = i;
      break;
    }
  }
  if (opening === -1) return { selectedIndex: null, options: [] };

  const options: RenderedOption[] = [];
  const selected: number[] = [];
  // Visible column of the active option's number -- 2 for both "  1. X" and "> 1. X"; see
  // DESCRIPTION_COLUMN_OFFSET's comment for why it is measured per option line.
  let baseline = 0;
  for (const line of lines.slice(opening + 1, footer)) {
    const m = OPTION_LINE.exec(line);
    if (m) {
      options.push({ index: Number(m[2]), label: m[3]! });
      if (m[1] === ">") selected.push(Number(m[2]));
      baseline = line.indexOf(m[2]!);
      continue;
    }
    if (options.length === 0) continue;
    const trimmed = line.trim();
    if (!trimmed) continue; // a blank line never detaches a continuation from its option
    const indent = line.length - line.trimStart().length;
    if (indent >= baseline + DESCRIPTION_COLUMN_OFFSET || SMELLS_LIKE_OPTION.test(trimmed)) {
      // A description line (or its continuations), or something that smells like a
      // mis-rendered option row: skipped, state kept -- never part of a label.
      continue;
    }
    const last = options[options.length - 1]!;
    last.label = `${last.label} ${trimmed}`;
  }

  // render() numbers options `${i + 1}`, so a real picker's indices are always
  // 1..N with no gaps. Anything else means the window caught something that is
  // not an option list -- a description that happens to start with a number,
  // a redraw seam -- and there is no safe way to navigate a list we misread.
  const contiguous = options.length > 0 && options.every((option, i) => option.index === i + 1);
  if (!contiguous) return { selectedIndex: null, options: [] };

  return { selectedIndex: selected.length === 1 ? selected[0]! : null, options };
}

/** Shortest answer allowed to match as a fragment; below this, only an exact label will do. */
const MIN_PARTIAL_ANSWER = 3;

/** True when `needle` appears in `haystack` bounded by non-alphanumerics on both sides. */
function containsAsWord(haystack: string, needle: string): boolean {
  const isWordChar = (c: string | undefined) => c !== undefined && /[a-z0-9]/.test(c);
  let from = 0;
  for (;;) {
    const at = haystack.indexOf(needle, from);
    if (at === -1) return false;
    if (!isWordChar(haystack[at - 1]) && !isWordChar(haystack[at + needle.length])) {
      return true;
    }
    from = at + 1;
  }
}

/**
 * Match a free-text answer to a listed option -- case-insensitive exact match
 * first, then a whole-word fragment match, both excluding the always-present
 * free-text escape option (never auto-select "Something else" via fuzzy
 * matching). Returns null on no match or an ambiguous (multiple) match -- the
 * caller falls back to reporting needsManual rather than guessing.
 *
 * The fragment match is bounded on word edges because a bare substring test
 * inverted answers on the approval gate this tool exists to relay:
 * matchOption("no", ["Commit now (Recommended)", "Stop here"]) found "no"
 * inside "now", matched exactly one option, and committed for a user who had
 * said no. Reproduced live on 2026-09-02 against a real picker. Answers
 * shorter than MIN_PARTIAL_ANSWER skip the fragment pass entirely, so a
 * two-letter answer can only ever take the exact path -- "no" still answers
 * an option actually labelled "No".
 */
export function matchOption(
  answer: string,
  options: readonly RenderedOption[],
): RenderedOption | null {
  const candidates = options.filter(
    (o) => o.label.toLowerCase() !== OTHER_OPTION_LABEL.toLowerCase(),
  );
  const needle = answer.trim().toLowerCase();
  if (!needle) return null;

  const exact = candidates.filter((o) => o.label.toLowerCase() === needle);
  if (exact.length === 1) return exact[0]!;
  if (needle.length < MIN_PARTIAL_ANSWER) return null;

  const partial = candidates.filter((o) => containsAsWord(o.label.toLowerCase(), needle));
  if (partial.length === 1) return partial[0]!;

  // Whitespace-insensitive last pass, for labels that only the reassembly path can produce:
  // wrapTextWithAnsi hard-breaks a word wider than the pane ("(Recommended)" ->
  // "(Recommended" + ")"), and single-space joining freezes the break in ("Commit
  // (Recommended )"), so neither the exact nor the word-bounded pass can pair the true label
  // text with the reassembled one. Word boundaries do not survive whitespace removal
  // ("stop here" -> "stophere"), so this pass is plain substring containment -- the
  // MIN_PARTIAL_ANSWER floor above gates it exactly like the fragment pass, and it still
  // counts candidates and refuses ambiguity like every other pass.
  const strippedNeedle = needle.replace(/\s+/g, "");
  const stripped = candidates.filter((o) =>
    o.label.toLowerCase().replace(/\s+/g, "").includes(strippedNeedle),
  );
  if (stripped.length === 1) return stripped[0]!;

  return null;
}

/** Arrow-key presses to move from the currently selected option to the target, then submit. */
export function navigationKeys(fromIndex: number, toIndex: number): string[] {
  const steps = toIndex - fromIndex;
  const key = steps > 0 ? "down" : "up";
  return [...Array(Math.abs(steps)).fill(key), "enter"];
}
