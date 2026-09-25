import assert from "node:assert/strict"
import { test } from "node:test"
import guardRails from "../plugin/guard-rails"
import notify from "../plugin/notify"
import ruffFormatOnEdit from "../plugin/ruff-format-on-edit"
import trustSession from "../plugins/trust-session"

test("opencode plugins export callable factories", () => {
  for (const plugin of [guardRails, notify, ruffFormatOnEdit, trustSession]) {
    assert.equal(typeof plugin, "function")
  }
})
