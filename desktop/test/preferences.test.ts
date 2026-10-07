import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { PreferencesStore } from "../src/shell/preferences.js";

let dir: string;
let path: string;

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "preferences-"));
  path = join(dir, "preferences.json");
});

afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

describe("the app's preferences", () => {
  it("keep running, with no developer mode, until the user chooses otherwise, and keep what they choose", () => {
    const store = new PreferencesStore(path);
    expect(store.get()).toEqual({ keepRunning: true, developer: false });
    store.set("keepRunning", false);
    store.set("developer", true);
    expect(new PreferencesStore(path).get()).toEqual({ keepRunning: false, developer: true });
  });

  it("refuses what is no preference, and reads a value of another kind as its default", () => {
    const store = new PreferencesStore(path);
    expect(() => store.set("theme", true)).toThrow("No preference theme = true");
    expect(() => store.set("developer", "on")).toThrow("No preference developer = on");
    expect(() => store.set("__proto__", true)).toThrow("No preference __proto__ = true");
    writeFileSync(path, JSON.stringify({ keepRunning: "no", developer: 1, other: true }));
    expect(store.get()).toEqual({ keepRunning: true, developer: false });
  });
});
