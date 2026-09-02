import { describe, expect, test } from "bun:test";
import {
  assertFields,
  buildArgv,
  extractFinalText,
  type DelegateParams,
} from "../extensions/delegate-tool";

describe("assertFields", () => {
  test("prompt is required and must not be blank", () => {
    expect(() => assertFields({ harness: "opencode" } as DelegateParams)).toThrow(
      /requires: prompt/,
    );
    expect(() => assertFields({ harness: "opencode", prompt: "   " })).toThrow(
      /prompt must not be empty/,
    );
  });

  test("provider is pi-only", () => {
    // Only pi separates provider from model; opencode takes provider/model as
    // one string and agy takes a bare model id.
    expect(() =>
      assertFields({ harness: "opencode", prompt: "go", provider: "opencode-go" }),
    ).toThrow(/provider is only valid for harness "pi"/);
    expect(() =>
      assertFields({ harness: "pi", prompt: "go", provider: "opencode-go", model: "glm-5.2" }),
    ).not.toThrow();
  });

  test("opencode models must carry their provider prefix", () => {
    // `opencode run -m` wants provider/model; a bare id silently resolves to
    // the wrong thing rather than erroring.
    expect(() => assertFields({ harness: "opencode", prompt: "go", model: "glm-5.2" })).toThrow(
      /must be provider\/model/,
    );
    expect(() =>
      assertFields({ harness: "opencode", prompt: "go", model: "opencode-go/glm-5.2" }),
    ).not.toThrow();
  });

  test("timeoutSeconds must be a positive integer", () => {
    for (const bad of [0, -5, 2.5]) {
      expect(() => assertFields({ harness: "agy", prompt: "go", timeoutSeconds: bad })).toThrow(
        /timeoutSeconds must be a positive integer/,
      );
    }
    expect(() => assertFields({ harness: "agy", prompt: "go", timeoutSeconds: 600 })).not.toThrow();
  });
});

describe("buildArgv", () => {
  test("opencode: json format, model, and prompt after --", () => {
    expect(
      buildArgv({ harness: "opencode", prompt: "Implement it", model: "opencode-go/glm-5.2" }),
    ).toEqual([
      "opencode",
      ["run", "--format", "json", "-m", "opencode-go/glm-5.2", "--", "Implement it"],
    ]);
  });

  test("opencode: -p is --password, so the prompt never goes near it", () => {
    const [, argv] = buildArgv({ harness: "opencode", prompt: "Implement it" });
    expect(argv).not.toContain("-p");
    // The prompt is a positional after --, not a flag value.
    expect(argv[argv.length - 1]).toBe("Implement it");
    expect(argv[argv.length - 2]).toBe("--");
  });

  test("opencode: autoApprove adds --auto", () => {
    const [, off] = buildArgv({ harness: "opencode", prompt: "x" });
    expect(off).not.toContain("--auto");
    const [, on] = buildArgv({ harness: "opencode", prompt: "x", autoApprove: true });
    expect(on).toContain("--auto");
  });

  test("agy: prompt is attached to -p, never a separate argument", () => {
    // `agy -p --output-format json "x"` makes -p swallow the next flag as its
    // prompt and ignore the real one. Attaching it is the only safe form.
    const [cmd, argv] = buildArgv({ harness: "agy", prompt: "Implement it", model: "gemini-3" });
    expect(cmd).toBe("agy");
    expect(argv).toContain("-p=Implement it");
    expect(argv).not.toContain("-p");
    // the -p= form must come last, after every other flag
    expect(argv[argv.length - 1]).toBe("-p=Implement it");
    expect(argv).toEqual(["--output-format", "json", "--model", "gemini-3", "-p=Implement it"]);
  });

  test("agy: autoApprove adds --dangerously-skip-permissions", () => {
    const [, on] = buildArgv({ harness: "agy", prompt: "x", autoApprove: true });
    expect(on).toContain("--dangerously-skip-permissions");
  });

  test("pi: json mode, no session, provider and model split", () => {
    expect(
      buildArgv({ harness: "pi", prompt: "Implement it", provider: "opencode-go", model: "glm" }),
    ).toEqual([
      "pi",
      [
        "-p",
        "--no-session",
        "--mode",
        "json",
        "--provider",
        "opencode-go",
        "--model",
        "glm",
        "Implement it",
      ],
    ]);
  });

  test("pi: has no permission system, so autoApprove adds no flag", () => {
    const [, off] = buildArgv({ harness: "pi", prompt: "x" });
    const [, on] = buildArgv({ harness: "pi", prompt: "x", autoApprove: true });
    expect(on).toEqual(off);
  });

  test("a prompt with quotes and apostrophes stays one argv element", () => {
    const nasty = `Implement it's "spec" -- TDD, don't commit`;
    for (const harness of ["opencode", "pi"] as const) {
      const [, argv] = buildArgv({ harness, prompt: nasty });
      expect(argv).toContain(nasty);
    }
    const [, agyArgv] = buildArgv({ harness: "agy", prompt: nasty });
    expect(agyArgv).toContain(`-p=${nasty}`);
  });
});

describe("extractFinalText", () => {
  test("opencode: joins text parts from the NDJSON event stream", () => {
    const raw = [
      '{"type":"step_start","part":{}}',
      '{"type":"text","part":{"type":"text","text":"Hello"}}',
      '{"type":"text","part":{"type":"text","text":" world"}}',
      '{"type":"step_finish"}',
    ].join("\n");
    expect(extractFinalText("opencode", raw)).toBe("Hello world");
  });

  test("opencode: a non-JSON log line is skipped, not fatal", () => {
    const raw = ["some stray log line", '{"type":"text","part":{"text":"OK"}}'].join("\n");
    expect(extractFinalText("opencode", raw)).toBe("OK");
  });

  test("agy: reads .response from the single result object", () => {
    const raw = JSON.stringify({ conversation_id: "x", status: "SUCCESS", response: "OK\n" });
    expect(extractFinalText("agy", raw)).toBe("OK");
  });

  test("pi: takes text parts of the last assistant message, dropping thinking", () => {
    const raw = [
      '{"type":"message_end","message":{"role":"user","content":[{"type":"text","text":"ask"}]}}',
      '{"type":"message_end","message":{"role":"assistant","content":[' +
        '{"type":"thinking","thinking":"hmm"},{"type":"text","text":"OK"}]}}',
    ].join("\n");
    expect(extractFinalText("pi", raw)).toBe("OK");
  });

  test("unparseable output falls back to a tail rather than throwing", () => {
    // A crashed child still has to report something useful.
    const raw = Array.from({ length: 100 }, (_, i) => `line ${i}`).join("\n");
    const out = extractFinalText("opencode", raw);
    expect(out).toContain("line 99");
    expect(out.split("\n").length).toBeLessThanOrEqual(40);
  });

  test("empty output yields an explicit marker, never an empty string", () => {
    expect(extractFinalText("opencode", "")).toMatch(/no output/i);
  });
});
