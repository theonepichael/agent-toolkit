import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
import {
  Editor,
  type EditorTheme,
  Key,
  matchesKey,
  Text,
  visibleWidth,
  wrapTextWithAnsi,
} from "@earendil-works/pi-tui";
import { Type } from "typebox";

// Registers `question` -- Pi's stand-in for Claude Code's AskUserQuestion,
// which Pi has no built-in equivalent of (CLAUDE_CODE_PARITY.md section 1).
// The TUI half is adapted from Pi's own shipped example at
// ~/.npm-global/lib/node_modules/@earendil-works/pi-coding-agent/examples/extensions/question.ts
// (arrow-key option list + inline free-text editor + Escape-to-cancel);
// the schema, validation, multi-select, and multi-question loop are this
// repo's, shaped to match AskUserQuestion's own parameter shape so the
// prompt files can ask the same way across harnesses.
//
// Validation lives in exported pure helpers rather than inline in execute()
// so it is unit-testable -- the ui.custom path itself is not.

export const MIN_OPTIONS = 2;
export const MAX_OPTIONS = 4;
export const MAX_QUESTIONS = 4;
export const MAX_HEADER_LENGTH = 12;

/** Appended to every question so the user is never boxed into the listed options. */
export const OTHER_LABEL = "Something else (type it)";

const RECOMMENDED_MARKER = "(Recommended)";

export interface QuestionOption {
  label: string;
  description: string;
}

export interface QuestionSpec {
  question: string;
  header: string;
  multiSelect?: boolean;
  options: QuestionOption[];
}

export interface QuestionToolParams {
  questions: QuestionSpec[];
}

export interface Answer {
  header: string;
  question: string;
  /** Chosen option labels, or the single free-text string when wasCustom. */
  selected: string[];
  wasCustom: boolean;
  /**
   * Milliseconds from raising this question's picker to receiving its answer.
   * Optional: absent on answers recorded before this was tracked.
   */
  latencyMs?: number;
}

export interface QuestionDetails {
  cancelled: boolean;
  answers: Answer[];
}

function isBlank(value: unknown): boolean {
  return typeof value !== "string" || value.trim() === "";
}

export function assertQuestions(params: QuestionToolParams): void {
  const questions = params.questions;

  if (!Array.isArray(questions) || questions.length === 0) {
    throw new Error("questions must be a non-empty array");
  }

  if (questions.length > MAX_QUESTIONS) {
    throw new Error(
      `questions accepts at most ${MAX_QUESTIONS} entries, got ${questions.length} -- ` +
        `ask the most decision-blocking ones now and the rest after they are answered`,
    );
  }

  const seenHeaders = new Set<string>();

  questions.forEach((spec, index) => {
    const where = `questions[${index}]`;

    if (isBlank(spec?.question)) {
      throw new Error(`${where}: question must not be empty`);
    }

    if (isBlank(spec?.header)) {
      throw new Error(`${where}: header must not be empty`);
    }

    const header = spec.header.trim();
    if (header.length > MAX_HEADER_LENGTH) {
      throw new Error(
        `${where}: header must be at most ${MAX_HEADER_LENGTH} characters, ` +
          `got ${header.length} ("${header}") -- it is a column label, not a restatement ` +
          `of the question`,
      );
    }

    const headerKey = header.toLowerCase();
    if (seenHeaders.has(headerKey)) {
      throw new Error(
        `${where}: duplicate headers ("${header}") -- each question needs its own ` +
          `header so its answer can be told apart`,
      );
    }
    seenHeaders.add(headerKey);

    const options = spec?.options;
    if (!Array.isArray(options) || options.length < MIN_OPTIONS || options.length > MAX_OPTIONS) {
      const got = Array.isArray(options) ? options.length : 0;
      throw new Error(
        `${where}: options must have between ${MIN_OPTIONS} and ${MAX_OPTIONS} entries, ` +
          `got ${got} -- fewer than ${MIN_OPTIONS} is not a choice, more than ${MAX_OPTIONS} ` +
          `is a menu the user has to study`,
      );
    }

    const seenLabels = new Set<string>();
    let recommendedIndex = -1;

    options.forEach((option, optionIndex) => {
      if (isBlank(option?.label)) {
        throw new Error(`${where}: options[${optionIndex}].label must not be empty`);
      }
      if (isBlank(option?.description)) {
        throw new Error(
          `${where}: options[${optionIndex}].description must not be empty -- say what ` +
            `picking it actually means`,
        );
      }

      const label = option.label.trim();
      const labelKey = label.toLowerCase();
      if (seenLabels.has(labelKey)) {
        throw new Error(
          `${where}: duplicate option labels ("${label}") -- the answer comes back by ` +
            `label, so two identical labels are unresolvable`,
        );
      }
      seenLabels.add(labelKey);

      // The free-text escape hatch is appended to every list under this
      // label, so an option carrying it would render twice and come back
      // ambiguous -- one of the two would be wasCustom and one not.
      if (labelKey === OTHER_LABEL.toLowerCase()) {
        throw new Error(
          `${where}: options[${optionIndex}].label must not be "${OTHER_LABEL}" -- that ` +
            `label is reserved for the free-text option every question already gets`,
        );
      }

      if (label.includes(RECOMMENDED_MARKER)) {
        if (recommendedIndex !== -1) {
          throw new Error(
            `${where}: only one option may be marked ${RECOMMENDED_MARKER}, found ` +
              `options[${recommendedIndex}] and options[${optionIndex}]`,
          );
        }
        recommendedIndex = optionIndex;
      }
    });

    if (recommendedIndex > 0) {
      throw new Error(
        `${where}: the ${RECOMMENDED_MARKER} option must be listed first, found it at ` +
          `options[${recommendedIndex}] -- lead with the recommendation`,
      );
    }
  });
}

/**
 * Below this, an answer did not come from a person.
 *
 * A human keypress and a `herdr agent send-keys` keypress are the same
 * terminal input -- pi exposes nothing that tells them apart, so the answer
 * text alone can never say who chose. Timing can. Measured 2026-09-02
 * against a real pi agent in a herdr pane: send-keys answered a picker
 * 182ms after the tool raised it, while the two answers a human gave that
 * evening took 17s and 36s. Two orders of magnitude apart, so 1s sits in
 * empty space: no one reads a commit diff and decides inside a second.
 *
 * This is evidence, not proof of authorship -- a fast answer is reported,
 * never silently discarded.
 */
export const IMPLAUSIBLY_FAST_MS = 1000;

function formatLatency(ms: number): string {
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`;
}

function describeAnswer(answer: Answer): string {
  const choices = answer.selected.map((s) => `"${s}"`).join(", ");
  const how = answer.wasCustom ? "typed" : "selected";
  let line = `[${answer.header}] ${answer.question}\n  User ${how}: ${choices}`;

  if (answer.latencyMs !== undefined) {
    line += ` (after ${formatLatency(answer.latencyMs)}`;
    if (answer.latencyMs < IMPLAUSIBLY_FAST_MS) {
      // The reader here is usually a model about to act on what it thinks is
      // an approval, so say what to do, not just that the timing is odd.
      line +=
        " -- IMPLAUSIBLY FAST for a human answer; treat this as an unattended " +
        "or self-answered picker, not as approval, and confirm with the user " +
        "before acting on it";
    }
    line += ")";
  }
  return line;
}

export function formatAnswers(answers: Answer[]): string {
  return answers.map(describeAnswer).join("\n");
}

export function formatCancellation(answered: Answer[]): string {
  const lead =
    "User cancelled the question without answering it. This is a dismissal, not a " +
    "choice: do not infer an answer, do not fall back to the first or recommended " +
    "option, and do not proceed as if anything was picked. Ask in plain text or stop " +
    "and wait for the user.";

  if (answered.length === 0) {
    return lead;
  }

  return (
    `${lead}\n\nPartial -- answered before cancelling (the remaining questions were ` +
    `never shown, so this run is incomplete):\n${formatAnswers(answered)}`
  );
}

const OptionSchema = Type.Object({
  label: Type.String({
    description:
      "Short display label for the option. The recommended option goes first and its " +
      "label ends with (Recommended).",
  }),
  description: Type.String({
    description: "One line on what picking this option actually means.",
  }),
});

const QuestionSchema = Type.Object({
  question: Type.String({ description: "The question to put to the user." }),
  header: Type.String({
    description: `Short label for this question (at most ${MAX_HEADER_LENGTH} characters), e.g. "Merge" or "Scope".`,
  }),
  multiSelect: Type.Optional(
    Type.Boolean({
      description: "Allow more than one option to be chosen. Defaults to false.",
    }),
  ),
  options: Type.Array(OptionSchema, {
    minItems: MIN_OPTIONS,
    maxItems: MAX_OPTIONS,
    description: `Between ${MIN_OPTIONS} and ${MAX_OPTIONS} options, recommendation first.`,
  }),
});

const QuestionToolSchema = Type.Object({
  questions: Type.Array(QuestionSchema, {
    minItems: 1,
    maxItems: MAX_QUESTIONS,
    description: `Between 1 and ${MAX_QUESTIONS} questions, asked one after another.`,
  }),
});

type DisplayOption = QuestionOption & { isOther?: boolean };

interface UIResult {
  selected: string[];
  wasCustom: boolean;
}

/**
 * RPC mode has a UI (`ctx.hasUI` is true) but no TUI, so `ctx.ui.custom()`
 * is unavailable there -- `ctx.ui.select()` works in both. Multi-select
 * degrades to one choice here; that is the trade for working at all.
 */
async function askViaSelect(ctx: ExtensionContext, spec: QuestionSpec): Promise<UIResult | null> {
  const labels = spec.options.map((o) => o.label);
  const choice = await ctx.ui.select(spec.question, labels);
  if (choice === undefined) return null;
  return { selected: [choice], wasCustom: false };
}

async function askViaCustomUI(
  ctx: ExtensionContext,
  spec: QuestionSpec,
  position: string,
): Promise<UIResult | null> {
  const multiSelect = spec.multiSelect === true;
  const allOptions: DisplayOption[] = [
    ...spec.options,
    { label: OTHER_LABEL, description: "Answer in your own words instead.", isOther: true },
  ];

  return await ctx.ui.custom<UIResult | null>((tui, theme, _keybindings, done) => {
    let optionIndex = 0;
    let editMode = false;
    const toggled = new Set<number>();
    // Keyed by the width it was built at. pi-tui calls requestRender() on a
    // terminal resize but never invalidate() (TUI.invalidate() is reached only
    // from consumeCellSizeResponse), so a width change arrives as a plain
    // re-render and the component has to spot it here. Lines built for a wider
    // pane would otherwise be replayed into a narrower one.
    let cached: { width: number; lines: string[] } | undefined;

    const editorTheme: EditorTheme = {
      borderColor: (s) => theme.fg("accent", s),
      selectList: {
        selectedPrefix: (t) => theme.fg("accent", t),
        selectedText: (t) => theme.fg("accent", t),
        description: (t) => theme.fg("muted", t),
        scrollInfo: (t) => theme.fg("dim", t),
        noMatch: (t) => theme.fg("warning", t),
      },
    };
    const editor = new Editor(tui, editorTheme);

    function refresh() {
      cached = undefined;
      tui.requestRender();
    }

    editor.onSubmit = (value) => {
      const trimmed = value.trim();
      if (trimmed) {
        done({ selected: [trimmed], wasCustom: true });
      } else {
        editMode = false;
        editor.setText("");
        refresh();
      }
    };

    function confirmSelection() {
      const selected = allOptions[optionIndex];
      if (selected.isOther) {
        editMode = true;
        refresh();
        return;
      }
      if (multiSelect) {
        // Enter with nothing toggled takes the highlighted option, so the
        // single-answer case still works without learning the space key.
        const chosen = toggled.size > 0 ? [...toggled].sort((a, b) => a - b) : [optionIndex];
        done({
          selected: chosen.map((i) => allOptions[i].label),
          wasCustom: false,
        });
        return;
      }
      done({ selected: [selected.label], wasCustom: false });
    }

    function handleInput(data: string) {
      if (editMode) {
        if (matchesKey(data, Key.escape)) {
          editMode = false;
          editor.setText("");
          refresh();
          return;
        }
        editor.handleInput(data);
        refresh();
        return;
      }

      if (matchesKey(data, Key.up)) {
        optionIndex = Math.max(0, optionIndex - 1);
        refresh();
        return;
      }
      if (matchesKey(data, Key.down)) {
        optionIndex = Math.min(allOptions.length - 1, optionIndex + 1);
        refresh();
        return;
      }
      if (multiSelect && data === " ") {
        const option = allOptions[optionIndex];
        if (!option.isOther) {
          if (toggled.has(optionIndex)) {
            toggled.delete(optionIndex);
          } else {
            toggled.add(optionIndex);
          }
          refresh();
        }
        return;
      }
      if (matchesKey(data, Key.enter)) {
        confirmSelection();
        return;
      }
      if (matchesKey(data, Key.escape)) {
        done(null);
      }
    }

    function render(width: number): string[] {
      const renderWidth = Math.max(1, width);
      if (cached && cached.width === renderWidth) return cached.lines;

      const lines: string[] = [];

      function addWrapped(text: string) {
        lines.push(...wrapTextWithAnsi(text, renderWidth));
      }

      function addWrappedWithPrefix(prefix: string, text: string) {
        const prefixWidth = visibleWidth(prefix);
        if (prefixWidth >= renderWidth) {
          addWrapped(prefix + text);
          return;
        }
        const wrapped = wrapTextWithAnsi(text, renderWidth - prefixWidth);
        const continuationPrefix = " ".repeat(prefixWidth);
        for (let i = 0; i < wrapped.length; i++) {
          lines.push(`${i === 0 ? prefix : continuationPrefix}${wrapped[i]}`);
        }
      }

      lines.push(theme.fg("accent", "─".repeat(renderWidth)));
      addWrappedWithPrefix(" ", theme.fg("dim", `${spec.header}${position}`));
      addWrappedWithPrefix(" ", theme.fg("text", spec.question));
      lines.push("");

      for (let i = 0; i < allOptions.length; i++) {
        const option = allOptions[i];
        const highlighted = i === optionIndex;
        const isOther = option.isOther === true;
        const marker = multiSelect && !isOther ? (toggled.has(i) ? "[x] " : "[ ] ") : "";
        const prefix = highlighted ? theme.fg("accent", "> ") : "  ";
        const label = `${marker}${i + 1}. ${option.label}${isOther && editMode ? " ✎" : ""}`;
        const color = highlighted || (isOther && editMode) ? "accent" : "text";

        addWrappedWithPrefix(prefix, theme.fg(color, label));
        if (option.description) {
          addWrappedWithPrefix("     ", theme.fg("muted", option.description));
        }
      }

      if (editMode) {
        lines.push("");
        addWrappedWithPrefix(" ", theme.fg("muted", "Your answer:"));
        for (const line of editor.render(Math.max(1, renderWidth - 2))) {
          lines.push(` ${line}`);
        }
      }

      lines.push("");
      if (editMode) {
        addWrappedWithPrefix(" ", theme.fg("dim", "Enter to submit • Esc to go back"));
      } else if (multiSelect) {
        addWrappedWithPrefix(
          " ",
          theme.fg("dim", "↑↓ navigate • Space to toggle • Enter to confirm • Esc to cancel"),
        );
      } else {
        addWrappedWithPrefix(" ", theme.fg("dim", "↑↓ navigate • Enter to select • Esc to cancel"));
      }
      lines.push(theme.fg("accent", "─".repeat(renderWidth)));

      cached = { width: renderWidth, lines };
      return lines;
    }

    return {
      render,
      invalidate: () => {
        cached = undefined;
      },
      handleInput,
    };
  });
}

export default function (pi: ExtensionAPI) {
  pi.registerTool({
    name: "question",
    label: "Question",
    description:
      "Ask the user one to four multiple-choice questions in an interactive picker and " +
      "get back their choices. Use at any judgment call where the user has to decide " +
      "between concrete options.",
    promptSnippet: "Ask the user a multiple-choice question and wait for their answer",
    promptGuidelines: [
      "Use question whenever a step needs the user to pick among 2-4 concrete, enumerable options -- a merge/push/cleanup gate, an approach choice, a scope call. Do not write the options out in plain text and wait for a typed reply; that costs a round trip and has to be parsed back. question is this session's structured multi-choice prompt.",
      "Always lead with a recommendation: the option you recommend is options[0] and its label ends with (Recommended). question refuses a call that puts the recommended option anywhere else, or marks more than one.",
      "Every option needs a description saying what picking it actually means -- not a restatement of the label.",
      "Keep header short (a couple of words); it labels the question, it does not repeat it.",
      "Use question only for genuinely enumerable choices. A genuinely open-ended question -- one whose useful answers are not a short list -- still gets asked in plain text.",
      "Set multiSelect only when more than one option can hold at once; leave it off for an either/or.",
      "A cancelled question is not an answer. If question reports a cancellation, never infer a choice or fall back to the recommended option -- stop and wait for the user.",
      "question needs an interactive session; it errors out in -p and JSON modes. In those modes, state the options and the recommendation in plain text instead.",
    ],
    parameters: QuestionToolSchema,
    executionMode: "sequential",

    async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
      const typed = params as QuestionToolParams;

      assertQuestions(typed);

      // hasUI is true in TUI and RPC modes, false in print (-p) and JSON
      // modes (docs/extensions.md, ExtensionContext). There is nothing to
      // prompt through in those, and silently answering for the user is the
      // one outcome worse than failing, so this errors out instead --
      // matching how guard-rails.ts handles the same !ctx.hasUI case.
      if (!ctx.hasUI) {
        throw new Error(
          "question needs an interactive session: this one is headless " +
            `(mode "${ctx.mode}"), so there is no UI to prompt through. Do not pick an ` +
            "option on the user's behalf. State the options and your recommendation in " +
            "plain text and let the user reply, or re-run interactively.",
        );
      }

      const answers: Answer[] = [];

      // No `herdr:blocked` emit here any more. This tool used to raise that
      // event by hand so herdr would report the picker as blocked, which
      // worked but covered only this one call site -- permission-gate's
      // confirm and every other ctx.ui.* prompt stayed invisible, and that
      // is what stranded swarm workers on 2026-09-02. herdr-blocked-bridge.ts
      // now drives it from pi's own `ui_prompt_start`/`ui_prompt_end` events,
      // which fire for this picker too, so emitting here as well would just
      // double-count. Nothing is lost when that bridge is absent: it goes
      // missing only under `-ne`, which also drops herdr-agent-state.ts, the
      // sole listener -- so the emit had nowhere to land in that case either.
      for (let i = 0; i < typed.questions.length; i++) {
        const spec = typed.questions[i];
        const position = typed.questions.length > 1 ? ` (${i + 1}/${typed.questions.length})` : "";

        // Measured across the ask itself, so it covers exactly the window a
        // human spends reading and deciding -- see IMPLAUSIBLY_FAST_MS.
        const askedAt = Date.now();
        const result =
          ctx.mode === "tui"
            ? await askViaCustomUI(ctx, spec, position)
            : await askViaSelect(ctx, spec);
        const latencyMs = Date.now() - askedAt;

        if (!result) {
          return {
            content: [{ type: "text", text: formatCancellation(answers) }],
            details: { cancelled: true, answers } as QuestionDetails,
          };
        }

        answers.push({
          header: spec.header.trim(),
          question: spec.question,
          selected: result.selected,
          wasCustom: result.wasCustom,
          latencyMs,
        });
      }

      return {
        content: [{ type: "text", text: formatAnswers(answers) }],
        details: { cancelled: false, answers } as QuestionDetails,
      };
    },

    renderCall(args, theme) {
      const typed = args as QuestionToolParams;
      const questions = Array.isArray(typed.questions) ? typed.questions : [];
      const first = questions[0];
      let text = theme.fg("toolTitle", theme.bold("question "));
      text += theme.fg("muted", first ? first.question : "");
      if (questions.length > 1) {
        text += theme.fg("dim", ` (+${questions.length - 1} more)`);
      }
      return new Text(text, 0, 0);
    },

    renderResult(result, _options, theme) {
      const details = result.details as QuestionDetails | undefined;
      if (!details) {
        const first = result.content[0];
        return new Text(first?.type === "text" ? first.text : "", 0, 0);
      }
      if (details.cancelled) {
        return new Text(theme.fg("warning", "Cancelled"), 0, 0);
      }
      const summary = details.answers
        .map((a) => `${a.header}: ${a.selected.join(", ")}${a.wasCustom ? " (typed)" : ""}`)
        .join(" • ");
      return new Text(theme.fg("success", "✓ ") + theme.fg("accent", summary), 0, 0);
    },
  });
}
