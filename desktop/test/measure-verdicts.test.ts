// How the macOS VM's measurements judge a root's commands (test/vm/darwin/measure/verdicts.ts), which run on a Mac
// alone: here, on any OS.

import { describe, expect, it } from "vitest";

import { failures, judged, ranInRoot, UNTESTED } from "./vm/darwin/measure/verdicts.js";

describe("a measurement's checks of what a root's commands said", () => {
  it("takes a command as run in its root only where it ran there and ended: not where the runner answered for it", () => {
    expect(ranInRoot({ ok: { output: "10000\n", returncode: 0, timed_out: false } })).toBe(true);
    expect(ranInRoot({ ok: { output: "", returncode: 1, timed_out: false } })).toBe(true);
    // The runner's answer for a root whose folder it cannot enter.
    expect(ranInRoot({ ok: { output: "[Errno 2] No such file or directory: '/Users/ana/escape-mine'", returncode: -1, timed_out: false } })).toBe(false);
    expect(ranInRoot({ ok: { output: "Command timed out after 60 seconds", returncode: 124, timed_out: true } })).toBe(false);
    expect(ranInRoot({ error: { type: "unavailable", message: "This computer's sandbox has not set up this chat" } })).toBe(false);
  });

  it("is UNTESTED, never PASS, unless every command it judges ran in its root, and fails the measurement with that word", () => {
    const [ran, answered] = [{ ran: true }, { ran: false }];
    expect([judged([ran], true), judged([ran], false), judged([ran, answered], true), judged([answered], false)]).toEqual([true, false, UNTESTED, UNTESTED]);
    expect(failures({ "a write to .git/hooks refused": UNTESTED, "no network interface but lo": false, "no host ~/.ssh": true })).toEqual([
      "UNTESTED a write to .git/hooks refused", "FAIL no network interface but lo",
    ]);
  });
});
