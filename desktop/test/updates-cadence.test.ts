// When an update is looked for, and when a release that no trusted key signed is said: the checks' cadence and their
// retries, and the day of failed checks the line waits before it says so. Every OS's app has them alike, so these run
// on a Mac too (vitest.config.ts, MACOS), against the same base as Linux's: its app names Linux's release as its own
// (updates-base.ts), and the rest of what a Mac's app reads is its own.

import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import { describe, expect, it, vi } from "vitest";

import { CHECK_MS, keepChecked, nextCheckIn, NOT_SIGNED, updateLine } from "../src/shell/updates.js";
import { next, servedBase } from "./updates-base.js";

const base = servedBase();
const NO_RELEASE = "is not a release of Surogate Desktop for this computer";

describe("a release that no key this computer trusts has signed", () => {
  const LINE = { text: `Surogate's newest release${NOT_SIGNED}`, button: null };
  // A clock of the test's own, and where the failures that stand are kept.
  const HOUR = 60 * 60 * 1000;
  const standing = () => join(base.dir, "unsigned.json");
  const kept = () => (existsSync(standing()) ? JSON.parse(readFileSync(standing(), "utf8")) as unknown : null);
  const UNSIGNED = "is not signed by Surogate's release key";

  it("says nothing of one at its first failed check, nor for a day: it keeps its version, logs it, and checks on; the line shows once eight checks in a row have failed so and the first of them is a day old", async () => {
    base.publish("1.2.4", {}, next.privateKey);
    let now = Date.UTC(2026, 0, 1);
    const states: string[] = [];
    const found = base.updates({ standing: standing(), now: () => now }, () => states.push(found.state.state));
    // Seven failed checks, the last of them two days after the first: a count short.
    for (let failed = 1; failed <= 7; failed += 1) {
      await expect(found.check(), String(failed)).rejects.toThrow(`${base.url}/desktop/latest.json ${UNSIGNED}`);
      expect([found.state, updateLine(found.state), kept()], String(failed)).toEqual([{ state: "none" }, null, { since: Date.UTC(2026, 0, 1), count: failed }]);
      now += 8 * HOUR;
    }
    expect(now - Date.UTC(2026, 0, 1)).toBeGreaterThan(48 * HOUR);
    // The eighth: both hold, and the line is there.
    await expect(found.check()).rejects.toThrow(UNSIGNED);
    expect([found.state, updateLine(found.state), states]).toEqual([{ state: "unsigned" }, LINE, ["unsigned"]]);
    // Nothing of it was downloaded, and nothing is installed from it.
    expect(base.heard.map(({ url }) => url)).toEqual(Array.from({ length: 8 }, () => ["/desktop/latest.json", base.signatureAt("1.2.4")]).flat());
    expect(existsSync(base.cache())).toBe(false);
    await found.install();
    expect(found.state).toEqual({ state: "unsigned" });
    // The line goes once the base's newest is one its keys take, and what stood is forgotten.
    base.publish("1.2.4");
    await found.check();
    expect(found.state).toMatchObject({ state: "available", version: "1.2.4" });
    expect(kept()).toBeNull();
  });

  it("counts a span as well as checks: eight failed checks within the hour show nothing, and the next one a day after the first does", async () => {
    base.publish("1.2.4", {}, next.privateKey);
    let now = Date.UTC(2026, 0, 1);
    const found = base.updates({ standing: standing(), now: () => now });
    for (let failed = 1; failed <= 12; failed += 1) {
      await expect(found.check()).rejects.toThrow(UNSIGNED);
      now += 5 * 60 * 1000;
    }
    expect([found.state, kept()]).toEqual([{ state: "none" }, { since: Date.UTC(2026, 0, 1), count: 12 }]);
    // A millisecond short of the day, and then the day.
    now = Date.UTC(2026, 0, 2) - 1;
    await expect(found.check()).rejects.toThrow(UNSIGNED);
    expect(found.state).toEqual({ state: "none" });
    now = Date.UTC(2026, 0, 2);
    await expect(found.check()).rejects.toThrow(UNSIGNED);
    expect(found.state).toEqual({ state: "unsigned" });
  });

  it("keeps what has stood across a restart, and forgets it at any answer a trusted key signed, a newer release or not", async () => {
    base.publish("1.2.4", {}, next.privateKey);
    let now = Date.UTC(2026, 0, 1);
    const app = () => base.updates({ standing: standing(), now: () => now });
    for (let failed = 1; failed <= 7; failed += 1) await expect(app().check()).rejects.toThrow(UNSIGNED);
    expect(kept()).toEqual({ since: Date.UTC(2026, 0, 1), count: 7 });
    // An app started two days later shows the line at its first check: the failure has stood.
    now += 48 * HOUR;
    const later = app();
    await expect(later.check()).rejects.toThrow(UNSIGNED);
    expect(later.state).toEqual({ state: "unsigned" });
    // A signed answer that is no newer release clears it, and the count begins again at the next.
    base.publish("1.2.3");
    await later.check();
    expect([later.state, kept()]).toEqual([{ state: "none" }, null]);
    base.publish("1.2.4", {}, next.privateKey);
    await expect(later.check()).rejects.toThrow(UNSIGNED);
    expect([later.state, kept()]).toEqual([{ state: "none" }, { since: now, count: 1 }]);
    // So does one that a trusted key signed and that is no release for this computer.
    base.publish("1.2.4", { arch: "arm64" });
    await expect(later.check()).rejects.toThrow(NO_RELEASE);
    expect(kept()).toBeNull();
    // A base that does not answer neither counts nor clears.
    base.publish("1.2.4", {}, next.privateKey);
    await expect(later.check()).rejects.toThrow(UNSIGNED);
    base.served.delete("/desktop/latest.json");
    await expect(later.check()).rejects.toThrow("answered 404");
    expect(kept()).toEqual({ since: now, count: 1 });
  });

  it("begins again where what was kept cannot be what stood: a first failure in the future, as after the clock was set back, or a file that is no such record", async () => {
    base.publish("1.2.4", {}, next.privateKey);
    const now = Date.UTC(2026, 0, 3);
    const found = base.updates({ standing: standing(), now: () => now });
    for (const written of [JSON.stringify({ since: now + 1, count: 500 }), "{", JSON.stringify({ since: "2026", count: 9 }), JSON.stringify({ since: now - 48 * HOUR, count: 7.5 }), JSON.stringify([now - 48 * HOUR, 9]), JSON.stringify({ since: now - 48 * HOUR, count: 0 }), JSON.stringify({ since: now - 48 * HOUR, count: -9 })]) {
      writeFileSync(standing(), written);
      await expect(found.check(), written.slice(0, 40)).rejects.toThrow(UNSIGNED);
      expect([found.state, kept()], written.slice(0, 40)).toEqual([{ state: "none" }, { since: now, count: 1 }]);
    }
    // And one that is such a record is taken as it is.
    writeFileSync(standing(), JSON.stringify({ since: now - 24 * HOUR, count: 7 }));
    await expect(found.check()).rejects.toThrow(UNSIGNED);
    expect(found.state).toEqual({ state: "unsigned" });
  });

  it("takes the line down at a newer release that a trusted key signed, though its download then fails: what stood was its signature", async () => {
    base.publish("1.2.4", {}, next.privateKey);
    const now = Date.UTC(2026, 0, 3);
    writeFileSync(standing(), JSON.stringify({ since: now - 24 * HOUR, count: 7 }));
    const found = base.updates({ standing: standing(), now: () => now });
    await expect(found.check()).rejects.toThrow(UNSIGNED);
    expect(found.state).toEqual({ state: "unsigned" });
    base.publish("1.2.4");
    base.served.delete(base.tarballAt("1.2.4"));
    await expect(found.check()).rejects.toThrow("answered 404 for Surogate 1.2.4");
    expect([found.state, updateLine(found.state), kept()]).toEqual([{ state: "none" }, null, null]);
  });

  it("leaves the line of an update that is here: one downloaded and offered stays offered", async () => {
    base.publish("1.2.4");
    const found = base.updates();
    await found.check();
    base.publish("1.2.5", {}, next.privateKey);
    await expect(found.check()).rejects.toThrow("is not signed by Surogate's release key");
    expect(found.state).toMatchObject({ state: "available", version: "1.2.4" });
  });

  it("shows the helper's own refusal of such a release whole, as its own line for one is: the way on is the sentence's end", async () => {
    base.publish("1.2.4");
    const refused = `the release's manifest${NOT_SIGNED}`;
    const found = base.updates({ apply: () => Promise.resolve({ code: 1, said: `Surogate Desktop: ${refused}\n` }) });
    await found.check();
    await found.install();
    expect([...refused].length).toBeGreaterThan(240);
    expect(updateLine(found.state)).toEqual({ text: `Surogate could not install its update: ${refused}`, button: "Try again" });
    // No other long line of a helper's is: one that only ends as the sentence does not begin is cut.
    const other = base.updates({ apply: () => Promise.resolve({ code: 1, said: `Surogate Desktop: ${"a".repeat(300)}${NOT_SIGNED}\n` }) });
    await other.check();
    await other.install();
    expect((other.state as { why: string }).why).toHaveLength(240);
  });
});

describe("a check", () => {
  it("is made every 6 hours", () => {
    expect(CHECK_MS).toBe(6 * 60 * 60 * 1000);
    expect([0, 1, 2, 3, 4, 9, 10, 11, 40].map((failed) => nextCheckIn(failed) / 60_000)).toEqual([360, 1, 2, 4, 8, 256, 360, 360, 360]);
  });

  it("is made at the app's start and six hours after each that ends well; one that fails is tried again after a minute, then two, then four, to six hours; and at once when the computer wakes", async () => {
    vi.useFakeTimers();
    try {
      let fails = true;
      let checks = 0;
      const said: unknown[] = [];
      const quit = new AbortController();
      const now = keepChecked({ check: () => (checks += 1, fails ? Promise.reject(new Error("could not reach the base")) : Promise.resolve()) }, quit.signal, (error) => said.push(String(error)));
      const after = async (minutes: number) => (await vi.advanceTimersByTimeAsync(minutes * 60_000), checks);
      expect(await after(0)).toBe(1);
      // Each failure is said, and the wait doubles from a minute.
      expect([await after(0.99), await after(0.01), await after(1.99), await after(0.01), await after(4)]).toEqual([1, 2, 2, 3, 4]);
      expect(said).toEqual(Array.from({ length: 4 }, () => "Error: could not reach the base"));
      // One that ends well is followed six hours later, and a failure after it waits a minute again.
      fails = false;
      expect([await after(8), await after(359), await after(1)]).toEqual([5, 5, 6]);
      fails = true;
      expect([await after(360), await after(1), await after(1.9)]).toEqual([7, 8, 8]);
      // Awake again: checked at once, whatever was left of the wait, and no second check comes of the wait.
      fails = false;
      now();
      expect([await after(0), await after(0.2), await after(359.7), await after(0.1)]).toEqual([9, 9, 9, 10]);
      // The app's quit ends it: a check it cut short is no failure to say, and none follows.
      fails = true;
      await after(360);
      const before = said.length;
      let cut: (error: Error) => void = () => {};
      const stops = keepChecked({ check: () => new Promise<void>((_resolve, reject) => (cut = reject)) }, quit.signal, (error) => said.push(String(error)));
      quit.abort(new Error("Surogate quit"));
      cut(new Error("Surogate quit"));
      stops();
      expect([await after(360), await after(360), said.length]).toEqual([11, 11, before]);
    } finally {
      vi.useRealTimers();
    }
  });

  it("is one at a time where the computer wakes while one runs, with one wait after it; and none begins at a wake after the quit", async () => {
    vi.useFakeTimers();
    try {
      let checks = 0;
      let end = () => {};
      const quit = new AbortController();
      const now = keepChecked({ check: () => (checks += 1, new Promise<void>((resolve) => (end = resolve))) }, quit.signal, () => {});
      const after = async (ms: number) => (await vi.advanceTimersByTimeAsync(ms), checks);
      expect(checks).toBe(1);
      // Awake twice while it runs: the check that runs is the one, and once it ends one check follows six hours on.
      now();
      now();
      expect(await after(0)).toBe(1);
      end();
      expect([await after(CHECK_MS - 1), await after(1), await after(CHECK_MS - 1)]).toEqual([1, 2, 2]);
      // Ended, and the app quit since: a wake checks nothing, nor does any wait.
      end();
      await after(0);
      quit.abort(new Error("Surogate quit"));
      now();
      expect([await after(0), await after(CHECK_MS * 2)]).toEqual([2, 2]);
    } finally {
      vi.useRealTimers();
    }
  });
});
