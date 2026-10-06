import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { WindowStates } from "../src/shell/window-state.js";

const SCREEN = [{ x: 0, y: 0, width: 1920, height: 1080 }];
const DEFAULTS = { width: 1200, height: 800 };

let dir: string;

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "window-state-"));
});

afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

const states = () => new WindowStates(join(dir, "window-state.json"));

describe("a window's place", () => {
  it("is its default size, centred, the first time", () => {
    expect(states().restore("agents", DEFAULTS, SCREEN)).toEqual({ width: 1200, height: 800, maximized: false });
  });

  it("is where the user left it, maximized or not, per window", () => {
    states().save("agents", { x: 10, y: 20, width: 700, height: 500 }, false);
    states().save("https://agent.example.com", { x: 100, y: 50, width: 1000, height: 700 }, true);
    expect(states().restore("agents", DEFAULTS, SCREEN)).toEqual({ x: 10, y: 20, width: 700, height: 500, maximized: false });
    expect(states().restore("https://agent.example.com", DEFAULTS, SCREEN))
      .toEqual({ x: 100, y: 50, width: 1000, height: 700, maximized: true });
  });

  it("is its default again when the display it was on has gone", () => {
    states().save("agents", { x: 2000, y: 20, width: 700, height: 500 }, false);
    expect(states().restore("agents", DEFAULTS, SCREEN)).toEqual({ width: 1200, height: 800, maximized: false });
    expect(states().restore("agents", DEFAULTS, [...SCREEN, { x: 1920, y: 0, width: 1920, height: 1080 }]))
      .toEqual({ x: 2000, y: 20, width: 700, height: 500, maximized: false });
  });

  it("is its default when the file is gone", () => {
    const path = join(dir, "window-state.json");
    new WindowStates(path).save("agents", { x: 1, y: 1, width: 700, height: 500 }, false);
    rmSync(path);
    expect(new WindowStates(path).restore("agents", DEFAULTS, SCREEN)).toEqual({ width: 1200, height: 800, maximized: false });
  });

  it("is its default when the file cannot be read, which is said, and the next save writes it whole", () => {
    const path = join(dir, "window-state.json");
    writeFileSync(path, "{ not json");
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    try {
      expect(new WindowStates(path).restore("agents", DEFAULTS, SCREEN)).toEqual({ width: 1200, height: 800, maximized: false });
      expect(String(warn.mock.calls[0]?.[0])).toMatch(/window-state\.json could not be read, so Surogate starts without it/);
      new WindowStates(path).save("agents", { x: 1, y: 1, width: 700, height: 500 }, false);
      expect(new WindowStates(path).restore("agents", DEFAULTS, SCREEN)).toEqual({ x: 1, y: 1, width: 700, height: 500, maximized: false });
    } finally {
      warn.mockRestore();
    }
  });
});
