// Usage labels in the web client (web/src/lib/usage-units.ts).
import assert from "node:assert/strict";
import { test } from "node:test";

import { approxTurnsLabel } from "../web/src/lib/usage-units.ts";

test("a 300K-token trial reads as the turns it buys, not messages", () => {
  assert.equal(approxTurnsLabel(300_000, 100_000), "~3 turns");
  assert.equal(approxTurnsLabel(100_000, 100_000), "~1 turn");
  assert.equal(approxTurnsLabel(99_999, 100_000), "under 1 turn");
  assert.equal(approxTurnsLabel(0, 100_000), "0 turns");
});

test("without an estimate amounts stay in plain tokens", () => {
  assert.equal(approxTurnsLabel(300_000, null), "300K tokens");
  assert.equal(approxTurnsLabel(2_500_000, undefined), "2.5M tokens");
  assert.equal(approxTurnsLabel(-1, null), "0 tokens");
});

test("turns are floored", () => {
  assert.equal(approxTurnsLabel(199_999, 100_000), "~1 turn");
});
