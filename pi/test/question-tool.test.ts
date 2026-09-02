import { describe, expect, test } from "bun:test";
import { visibleWidth } from "@earendil-works/pi-tui";
import {
  assertQuestions,
  formatAnswers,
  formatCancellation,
  MAX_HEADER_LENGTH,
  MAX_OPTIONS,
  MAX_QUESTIONS,
  MIN_OPTIONS,
  OTHER_LABEL,
  type QuestionSpec,
} from "../extensions/question-tool";
import registerQuestionTool from "../extensions/question-tool";

function options(n: number): { label: string; description: string }[] {
  return Array.from({ length: n }, (_, i) => ({
    label: `Option ${i + 1}`,
    description: `What option ${i + 1} does`,
  }));
}

function question(overrides: Partial<QuestionSpec> = {}): QuestionSpec {
  return {
    question: "Merge to main, push, and clean up the worktree?",
    header: "Merge",
    options: options(2),
    ...overrides,
  };
}

describe("assertQuestions", () => {
  test("a well-formed single question passes", () => {
    expect(() => assertQuestions({ questions: [question()] })).not.toThrow();
  });

  test("questions must be a non-empty array", () => {
    expect(() => assertQuestions({ questions: [] })).toThrow(/questions must be a non-empty array/);
    // The model can also hand back a non-array; the schema should catch it,
    // but the helper is the thing under test and must not crash on it.
    expect(() => assertQuestions({ questions: undefined as unknown as QuestionSpec[] })).toThrow(
      /questions must be a non-empty array/,
    );
  });

  test("at most MAX_QUESTIONS questions", () => {
    const tooMany = Array.from({ length: MAX_QUESTIONS + 1 }, (_, i) =>
      question({ header: `H${i}` }),
    );
    expect(() => assertQuestions({ questions: tooMany })).toThrow(
      new RegExp(`at most ${MAX_QUESTIONS}`),
    );
    const atLimit = Array.from({ length: MAX_QUESTIONS }, (_, i) => question({ header: `H${i}` }));
    expect(() => assertQuestions({ questions: atLimit })).not.toThrow();
  });

  test("option count must be between MIN_OPTIONS and MAX_OPTIONS", () => {
    // A single option is not a choice, and zero is nothing to pick from --
    // both mean the model should have just stated its plan instead of asking.
    for (const n of [0, 1, MAX_OPTIONS + 1]) {
      expect(() => assertQuestions({ questions: [question({ options: options(n) })] })).toThrow(
        new RegExp(`options must have between ${MIN_OPTIONS} and ${MAX_OPTIONS}`),
      );
    }
    for (const n of [2, 3, 4]) {
      expect(() =>
        assertQuestions({ questions: [question({ options: options(n) })] }),
      ).not.toThrow();
    }
  });

  test("the question text must not be empty", () => {
    expect(() => assertQuestions({ questions: [question({ question: "   " })] })).toThrow(
      /questions\[0\]: question must not be empty/,
    );
  });

  test("the header must not be empty and must stay short", () => {
    expect(() => assertQuestions({ questions: [question({ header: "" })] })).toThrow(
      /questions\[0\]: header must not be empty/,
    );
    const long = "x".repeat(MAX_HEADER_LENGTH + 1);
    expect(() => assertQuestions({ questions: [question({ header: long })] })).toThrow(
      new RegExp(`header must be at most ${MAX_HEADER_LENGTH} characters`),
    );
  });

  test("every option needs a label and a description", () => {
    const missingLabel = [
      { label: "  ", description: "d" },
      { label: "b", description: "d" },
    ];
    expect(() => assertQuestions({ questions: [question({ options: missingLabel })] })).toThrow(
      /questions\[0\]: options\[0\]\.label must not be empty/,
    );

    const missingDescription = [
      { label: "a", description: "d" },
      { label: "b", description: "" },
    ];
    expect(() =>
      assertQuestions({ questions: [question({ options: missingDescription })] }),
    ).toThrow(/questions\[0\]: options\[1\]\.description must not be empty/);
  });

  test("duplicate option labels are refused", () => {
    // The answer comes back by label, so two identical labels make "user
    // selected X" unresolvable.
    const dupes = [
      { label: "Yes", description: "do it" },
      { label: "Yes", description: "also do it" },
    ];
    expect(() => assertQuestions({ questions: [question({ options: dupes })] })).toThrow(
      /duplicate option labels/,
    );
  });

  test("the free-text option's label is reserved", () => {
    // It is appended to every list, so an option claiming it would render
    // twice and come back ambiguous.
    const collides = [
      { label: OTHER_LABEL, description: "collides with the built-in escape hatch" },
      { label: "Yes", description: "do it" },
    ];
    expect(() => assertQuestions({ questions: [question({ options: collides })] })).toThrow(
      /reserved for the free-text option/,
    );
  });

  test("the recommended option must be listed first", () => {
    // House rule: lead with the recommendation. A "(Recommended)" marker
    // sitting on option 3 is the exact failure this catches.
    const misplaced = [
      { label: "Rebase", description: "replay commits" },
      { label: "Merge (Recommended)", description: "keep history" },
    ];
    expect(() => assertQuestions({ questions: [question({ options: misplaced })] })).toThrow(
      /\(Recommended\) option must be listed first/,
    );

    const correct = [
      { label: "Merge (Recommended)", description: "keep history" },
      { label: "Rebase", description: "replay commits" },
    ];
    expect(() => assertQuestions({ questions: [question({ options: correct })] })).not.toThrow();
  });

  test("only one option may be marked recommended", () => {
    const two = [
      { label: "Merge (Recommended)", description: "keep history" },
      { label: "Rebase (Recommended)", description: "replay commits" },
    ];
    expect(() => assertQuestions({ questions: [question({ options: two })] })).toThrow(
      /only one option may be marked \(Recommended\)/,
    );
  });

  test("a question with no recommendation is still allowed", () => {
    expect(() => assertQuestions({ questions: [question()] })).not.toThrow();
  });

  test("the failing question is named by index", () => {
    const questions = [question(), question({ header: "", options: options(2) })];
    expect(() => assertQuestions({ questions })).toThrow(/questions\[1\]: header/);
  });

  test("headers must be distinct so answers can be told apart", () => {
    const questions = [question({ header: "Merge" }), question({ header: "Merge" })];
    expect(() => assertQuestions({ questions })).toThrow(/duplicate headers/);
  });
});

describe("formatAnswers", () => {
  test("a single choice reads as a selection, not a guess", () => {
    const text = formatAnswers([
      {
        header: "Merge",
        question: "Merge to main?",
        selected: ["Yes (Recommended)"],
        wasCustom: false,
      },
    ]);
    expect(text).toContain("Merge");
    expect(text).toContain("Yes (Recommended)");
    expect(text).not.toMatch(/cancel/i);
  });

  test("multi-select answers list every choice", () => {
    const text = formatAnswers([
      {
        header: "Cleanup",
        question: "What should happen next?",
        selected: ["Merge", "Push"],
        wasCustom: false,
      },
    ]);
    expect(text).toContain("Merge");
    expect(text).toContain("Push");
  });

  // Answer latency is the only signal available that separates a human
  // selection from a programmatically-driven one: a keypress from a hand and
  // a keypress from `herdr agent send-keys` arrive as identical terminal
  // input, and pi exposes nothing to tell them apart. Timing does separate
  // them, decisively. Measured 2026-09-02 against a real pi agent in a herdr
  // pane: send-keys answered a picker 182ms after the tool raised it (109ms
  // blocked->answered at the herdr layer, the send-keys call itself 2ms),
  // versus 17s and 36s for the two answers a human made the same evening.
  // Roughly two orders of magnitude apart, so a 1s threshold sits in empty
  // space -- nobody reads a commit diff and decides inside a second.
  test("an implausibly fast answer is called out as not human", () => {
    const text = formatAnswers([
      {
        header: "Commit",
        question: "Commit these changes?",
        selected: ["Yes, commit (Recommended)"],
        wasCustom: false,
        latencyMs: 182,
      },
    ]);
    expect(text).toContain("182ms");
    expect(text).toMatch(/implausibly fast/i);
    // The warning has to say what to DO, since the reader is usually a model
    // deciding whether it just received an approval.
    expect(text).toMatch(/not.*approval|do not treat/i);
  });

  test("a human-paced answer records its latency without crying wolf", () => {
    const text = formatAnswers([
      {
        header: "Commit",
        question: "Commit these changes?",
        selected: ["Yes, commit (Recommended)"],
        wasCustom: false,
        latencyMs: 17_000,
      },
    ]);
    expect(text).toContain("17.0s");
    expect(text).not.toMatch(/implausibly fast/i);
  });

  test("an answer with no recorded latency renders exactly as before", () => {
    const text = formatAnswers([
      {
        header: "Merge",
        question: "Merge to main?",
        selected: ["Yes"],
        wasCustom: false,
      },
    ]);
    expect(text).toBe('[Merge] Merge to main?\n  User selected: "Yes"');
  });

  test("free-text answers are marked as typed, not picked", () => {
    const text = formatAnswers([
      {
        header: "Merge",
        question: "Merge to main?",
        selected: ["hold off until Monday"],
        wasCustom: true,
      },
    ]);
    expect(text).toContain("hold off until Monday");
    expect(text).toMatch(/typed/i);
  });

  test("each question's answer appears on its own", () => {
    const text = formatAnswers([
      { header: "Merge", question: "Merge?", selected: ["Yes"], wasCustom: false },
      { header: "Push", question: "Push?", selected: ["No"], wasCustom: false },
    ]);
    expect(text).toContain("Merge");
    expect(text).toContain("Push");
    expect(text.split("\n").length).toBeGreaterThan(1);
  });
});

describe("formatCancellation", () => {
  test("cancellation is unmistakable and forbids inferring a choice", () => {
    const text = formatCancellation([]);
    expect(text).toMatch(/cancel/i);
    expect(text).toMatch(/did not|not an answer|do not/i);
  });

  test("a partial run reports what was answered but still reads as cancelled", () => {
    const text = formatCancellation([
      { header: "Merge", question: "Merge?", selected: ["Yes"], wasCustom: false },
    ]);
    expect(text).toMatch(/cancel/i);
    expect(text).toContain("Merge");
    expect(text).toMatch(/before cancelling|incomplete|partial/i);
  });
});

// ---------------------------------------------------------------------------
// Picker render caching.
//
// pi kills the session with an uncaughtException when a rendered line is wider
// than the terminal, so every component gets its width per render call and has
// to honour it. The picker is the one component in this tree that caches its
// rendered lines, and pi-tui never tells it a resize happened: TUI.start wires
// the terminal's resize handler to requestRender() (dist/tui.js), while
// TUI.invalidate() -- the only thing that reaches a component's invalidate() --
// is called from exactly one place, consumeCellSizeResponse, which fires on a
// cell-pixel-size report and not on a resize. So a cache that is cleared only
// by invalidate() and by local state changes survives a width change, and the
// picker keeps drawing at the width it first saw.
//
// The orchestrator reads this rendering back over `herdr agent read` to answer
// a blocked worker, so stale lines are not only a display fault.
// ---------------------------------------------------------------------------

interface PickerComponent {
  render(width: number): string[];
  invalidate?(): void;
  handleInput?(data: string): void;
}

/** Drives the real `question` tool far enough to get its live picker component back. */
async function capturePicker(spec: QuestionSpec): Promise<PickerComponent> {
  const tools = new Map<string, { execute: (...a: never[]) => Promise<unknown> }>();
  registerQuestionTool({
    registerTool: (def: { name: string }) => tools.set(def.name, def as never),
  } as never);
  const tool = tools.get("question");
  if (!tool) throw new Error("question tool was never registered");

  let captured: PickerComponent | undefined;
  const ctx = {
    hasUI: true,
    mode: "tui",
    ui: {
      custom<T>(
        factory: (
          tui: unknown,
          theme: unknown,
          keybindings: unknown,
          done: (value: T) => void,
        ) => PickerComponent,
      ): Promise<T> {
        return new Promise<T>((resolve) => {
          // Plain passthrough colouring, so visibleWidth measures exactly the
          // characters the assertions care about.
          const theme = { fg: (_role: string, text: string) => text };
          captured = factory({ requestRender() {} }, theme, {}, resolve);
          resolve({ selected: [spec.options[0]!.label], wasCustom: false } as T);
        });
      },
    },
  };

  await tool.execute(
    ...(["call-1", { questions: [spec] }, undefined, undefined, ctx] as unknown as never[]),
  );
  if (!captured) throw new Error("ui.custom was never called");
  return captured;
}

describe("picker render caching", () => {
  test("renders every line within the width it was given", async () => {
    const picker = await capturePicker(question());
    for (const line of picker.render(80)) {
      expect(visibleWidth(line)).toBeLessThanOrEqual(80);
    }
  });

  test("a narrowing width re-renders instead of replaying the wider lines", async () => {
    const picker = await capturePicker(question());
    picker.render(80);
    // No invalidate() in between: pi-tui does not call one on a resize, so the
    // component has to notice the width itself.
    for (const line of picker.render(40)) {
      expect(visibleWidth(line)).toBeLessThanOrEqual(40);
    }
  });

  test("a widening width re-renders too, not just a narrowing one", async () => {
    const picker = await capturePicker(question());
    const narrow = picker.render(40);
    const wide = picker.render(100);
    expect(wide).not.toEqual(narrow);
    expect(visibleWidth(wide[0]!)).toBe(100);
  });

  test("an unchanged width still serves the cache", async () => {
    const picker = await capturePicker(question());
    const first = picker.render(80);
    expect(picker.render(80)).toBe(first);
  });
});
