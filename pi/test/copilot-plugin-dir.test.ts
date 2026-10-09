// copilotPluginDir(): where Copilot swarm workers load the swarm plugin from
// when the caller passes no pluginDir. The checkout may sit flat
// (~/Workspace/agent-toolkit) or nested (~/Workspace/agent-toolkit/agent-toolkit,
// with worktrees beside it), so a fixed home-relative guess is wrong on one of
// the two layouts; the module locates its own checkout instead.

import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { afterEach, beforeEach, describe, expect, test } from "./helpers/tap";

import { copilotPluginDir, SWARM_PLUGIN_NAME } from "../extensions/swarm-lib/swarm-tool-context.js";

const ROOT = join(import.meta.dirname, "../..");
const PLUGIN = join(ROOT, "copilot", "extensions", "swarm");

let tmp: string;

beforeEach(() => {
  tmp = mkdtempSync(join(tmpdir(), "plugin-dir-"));
});

afterEach(() => {
  rmSync(tmp, { recursive: true, force: true });
});

function makeCheckout(dir: string, withPlugin = true): string {
  mkdirSync(join(dir, "agent-scripts"), { recursive: true });
  writeFileSync(join(dir, "install.py"), "");
  writeFileSync(join(dir, "links.toml"), "");
  if (withPlugin) makePlugin(join(dir, "copilot", "extensions", "swarm"));
  return dir;
}

function makePlugin(dir: string, name: string = SWARM_PLUGIN_NAME): string {
  mkdirSync(join(dir, "lib"), { recursive: true });
  mkdirSync(join(dir, "extensions", "swarm"), { recursive: true });
  writeFileSync(join(dir, "plugin.json"), JSON.stringify({ name }));
  return dir;
}

/** A module URL for a file at `rel` under `base`; the file itself is created. */
function moduleAt(base: string, rel: string): string {
  const file = join(base, rel);
  mkdirSync(join(file, ".."), { recursive: true });
  writeFileSync(file, "");
  return pathToFileURL(file).href;
}

const SOURCE = join("pi", "extensions", "swarm-lib", "swarm-tool-context.ts");
const LIB_BUNDLE = join("copilot", "extensions", "swarm", "lib", "swarm-tool-logic.js");
const ENTRY_BUNDLE = join("copilot", "extensions", "swarm", "extensions", "swarm", "extension.mjs");

describe("copilotPluginDir: self-location", () => {
  for (const [label, rel] of [
    ["pi source", SOURCE],
    ["lib/ bundle", LIB_BUNDLE],
    ["extensions/swarm/ bundle", ENTRY_BUNDLE],
  ] as const) {
    test(`finds its own checkout's plugin from the ${label}`, () => {
      const checkout = makeCheckout(join(tmp, "Workspace", "agent-toolkit", "agent-toolkit"));
      const got = copilotPluginDir({
        env: {},
        home: join(tmp, "home"),
        moduleUrl: moduleAt(checkout, rel),
      });
      expect(got).toBe(join(checkout, "copilot", "extensions", "swarm"));
    });
  }

  test("a worktree finds its own plugin, not the main checkout's", () => {
    const container = join(tmp, "Workspace", "agent-toolkit");
    makeCheckout(join(container, "agent-toolkit"));
    const worktree = makeCheckout(join(container, "agent-toolkit-some-branch"));
    const got = copilotPluginDir({
      env: { AGENT_TOOLKIT_PATH: join(container, "agent-toolkit") },
      home: tmp,
      moduleUrl: moduleAt(worktree, SOURCE),
    });
    expect(got).toBe(join(worktree, "copilot", "extensions", "swarm"));
  });

  test("a plugin copied outside any checkout finds itself", () => {
    const copied = makePlugin(join(tmp, "plugins", "swarm"));
    for (const rel of [
      join("lib", "swarm-tool-logic.js"),
      join("extensions", "swarm", "extension.mjs"),
    ]) {
      const got = copilotPluginDir({
        env: {},
        home: join(tmp, "home"),
        moduleUrl: moduleAt(copied, rel),
      });
      expect(got).toBe(copied);
    }
  });

  test("never walks past its own checkout to a parent checkout's plugin", () => {
    const outer = makeCheckout(join(tmp, "outer"));
    const inner = makeCheckout(join(outer, "nested-checkout"), false);
    expect(() =>
      copilotPluginDir({ env: {}, home: join(tmp, "home"), moduleUrl: moduleAt(inner, SOURCE) }),
    ).toThrow(/COPILOT_SWARM_PLUGIN_DIR/);
  });

  test("a plugin.json with a different name is not the swarm plugin", () => {
    const other = makePlugin(join(tmp, "other-plugin"), "something-else");
    expect(() =>
      copilotPluginDir({
        env: {},
        home: join(tmp, "home"),
        moduleUrl: moduleAt(other, join("lib", "x.js")),
      }),
    ).toThrow(/COPILOT_SWARM_PLUGIN_DIR/);
  });
});

describe("copilotPluginDir: overrides and fallbacks", () => {
  const nowhere = () => moduleAt(tmp, join("loose", "module.js"));

  test("COPILOT_SWARM_PLUGIN_DIR wins verbatim", () => {
    const got = copilotPluginDir({
      env: { COPILOT_SWARM_PLUGIN_DIR: "/explicit/dir" },
      home: tmp,
      moduleUrl: nowhere(),
    });
    expect(got).toBe("/explicit/dir");
  });

  test("falls back to AGENT_TOOLKIT_PATH", () => {
    const checkout = makeCheckout(join(tmp, "src", "atk"));
    const got = copilotPluginDir({
      env: { AGENT_TOOLKIT_PATH: checkout },
      home: tmp,
      moduleUrl: nowhere(),
    });
    expect(got).toBe(join(checkout, "copilot", "extensions", "swarm"));
  });

  test("a set but invalid AGENT_TOOLKIT_PATH throws instead of falling through", () => {
    makeCheckout(join(tmp, "Workspace", "agent-toolkit", "agent-toolkit"));
    expect(() =>
      copilotPluginDir({
        env: { AGENT_TOOLKIT_PATH: join(tmp, "not-a-checkout") },
        home: tmp,
        moduleUrl: nowhere(),
      }),
    ).toThrow(/AGENT_TOOLKIT_PATH/);
  });

  test("falls back to the nested layout, skipping the container", () => {
    const nested = makeCheckout(join(tmp, "Workspace", "agent-toolkit", "agent-toolkit"));
    const got = copilotPluginDir({ env: {}, home: tmp, moduleUrl: nowhere() });
    expect(got).toBe(join(nested, "copilot", "extensions", "swarm"));
  });

  test("falls back to the flat layout", () => {
    const flat = makeCheckout(join(tmp, "Workspace", "agent-toolkit"));
    const got = copilotPluginDir({ env: {}, home: tmp, moduleUrl: nowhere() });
    expect(got).toBe(join(flat, "copilot", "extensions", "swarm"));
  });

  test("throws, naming both overrides, when nothing is found", () => {
    mkdirSync(join(tmp, "Workspace", "agent-toolkit", "agent-toolkit-wt"), { recursive: true });
    expect(() => copilotPluginDir({ env: {}, home: tmp, moduleUrl: nowhere() })).toThrow(
      /COPILOT_SWARM_PLUGIN_DIR.*pluginDir/s,
    );
  });
});

describe("copilotPluginDir: real module locations", () => {
  // No moduleUrl: these prove import.meta.url is a usable file: URL under the
  // loader each host actually uses.
  test("the pi source, loaded by the test loader, finds this checkout's plugin", () => {
    expect(copilotPluginDir({ env: {}, home: tmp })).toBe(PLUGIN);
  });

  test("the committed lib/ bundle finds this checkout's plugin", async () => {
    const bundle = await import(pathToFileURL(join(ROOT, LIB_BUNDLE)).href);
    expect(bundle.copilotPluginDir({ env: {}, home: tmp })).toBe(PLUGIN);
  });
});
