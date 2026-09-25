import assert from "node:assert/strict"
import { test } from "node:test"
import guardRails from "../plugin/guard-rails"
import notify from "../plugin/notify"
import ruffFormatOnEdit from "../plugin/ruff-format-on-edit"
import trustSession from "../plugins/trust-session"
import permissionGate from "../tui/permission-gate"
import trustSessionTui from "../tui/trust-session"

test("opencode plugins export callable factories", () => {
  for (const plugin of [guardRails, notify, ruffFormatOnEdit, trustSession]) {
    assert.equal(typeof plugin, "function")
  }
  for (const plugin of [permissionGate, trustSessionTui]) {
    assert.equal(typeof plugin, "object")
    assert.equal(typeof plugin.tui, "function")
  }
})
