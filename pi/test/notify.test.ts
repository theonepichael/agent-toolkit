import { describe, expect, test } from "bun:test";
import { buildNotifyArgs } from "../extensions/notify";

describe("buildNotifyArgs", () => {
  test("builds basic args with defaults", () => {
    const args = buildNotifyArgs({});
    expect(args[0]).toBe("python3");
    expect(args[1]).toContain("notify.py");
  });

  test("applies title, message, harness, type", () => {
    const args = buildNotifyArgs({
      title: "Custom Title",
      message: "Custom Message",
      harness: "Pi",
      type: "waiting_for_input",
    });
    expect(args).toContain("--title");
    expect(args).toContain("Custom Title");
    expect(args).toContain("--message");
    expect(args).toContain("Custom Message");
    expect(args).toContain("--harness");
    expect(args).toContain("Pi");
    expect(args).toContain("--type");
    expect(args).toContain("waiting_for_input");
  });
});
