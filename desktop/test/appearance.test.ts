import { EventEmitter } from "node:events";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AppearanceStore, chrome, DEFAULTS, Theme, type ThemeSource } from "../src/shell/appearance.js";

// Electron's nativeTheme, as far as the shell uses it: the system's own theme is *system*.
class NativeTheme extends EventEmitter implements ThemeSource {
  themeSource: "system" | "light" | "dark" = "system";
  system: "light" | "dark" = "light";

  get shouldUseDarkColors(): boolean {
    return this.themeSource === "system" ? this.system === "dark" : this.themeSource === "dark";
  }

  flip(to: "light" | "dark"): void {
    this.system = to;
    this.emit("updated");
  }
}

let dir: string;
let path: string;

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "appearance-"));
  path = join(dir, "settings.json");
});

afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

describe("the appearance settings", () => {
  it("are Match system, medium text, medium width and the system's motion until the user changes them", () => {
    expect(new AppearanceStore(path).get()).toEqual(DEFAULTS);
    expect(DEFAULTS).toEqual({ theme: "system", textSize: "medium", transcriptWidth: "medium", motion: "system" });
  });

  it("keep what the user chose", () => {
    new AppearanceStore(path).set("theme", "dark");
    new AppearanceStore(path).set("textSize", "large");
    expect(new AppearanceStore(path).get()).toEqual({ ...DEFAULTS, theme: "dark", textSize: "large" });
  });

  it.each([["theme", "blue"], ["textSize", 3], ["colour", "dark"]])("refuse %s = %s", (key, value) => {
    expect(() => new AppearanceStore(path).set(key, value)).toThrow(`No appearance setting ${key} = ${String(value)}`);
  });

  it("are the defaults when the file cannot be read, which is said", () => {
    writeFileSync(path, "theme: dark");
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    try {
      expect(new AppearanceStore(path).get()).toEqual(DEFAULTS);
      expect(warn).toHaveBeenCalledTimes(1);
    } finally {
      warn.mockRestore();
    }
  });

  it("are the defaults when the file holds JSON that is no object, which is said", () => {
    writeFileSync(path, "null");
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    try {
      expect(new AppearanceStore(path).get()).toEqual(DEFAULTS);
      expect(String(warn.mock.calls[0]?.[0])).toMatch(/settings\.json could not be read, so Surogate starts without it: it holds null/);
    } finally {
      warn.mockRestore();
    }
  });

  it("read a value the file should not hold as its default", () => {
    writeFileSync(path, JSON.stringify({ theme: "blue", motion: "reduced" }));
    expect(new AppearanceStore(path).get()).toEqual({ ...DEFAULTS, motion: "reduced" });
  });
});

describe("the theme", () => {
  it("paints the window and the controls over the title bar in the theme's colours, 36 px high as Claude Desktop's", () => {
    expect(chrome(true)).toEqual({ background: "#151515", overlay: { color: "#1a1a19", symbolColor: "#c2c0b6", height: 36 } });
    expect(chrome(false)).toEqual({ background: "#faf9f5", overlay: { color: "#f5f4ed", symbolColor: "#3d3d3a", height: 36 } });
  });

  it("dims the controls' strip as Settings' backdrop dims the window, its symbols left to read", () => {
    // Black over the strip's colour at the backdrop's 0.55; the light theme's symbols turn light on it.
    expect(chrome(true, true).overlay).toEqual({ color: "#0c0c0b", symbolColor: "#c2c0b6", height: 36 });
    expect(chrome(false, true).overlay).toEqual({ color: "#6e6e6b", symbolColor: "#faf9f5", height: 36 });
    expect(chrome(true, true).background).toBe("#151515");
  });

  it("is the saved choice from the start, and follows the system's under Match system", () => {
    const native = new NativeTheme();
    const painted: boolean[] = [];
    new AppearanceStore(path).set("theme", "system");
    const theme = new Theme(native, new AppearanceStore(path), (dark) => painted.push(dark));
    expect(native.themeSource).toBe("system");
    expect(theme.dark).toBe(false);
    native.flip("dark");
    expect(painted).toEqual([true]);
    expect(theme.dark).toBe(true);
  });

  it("changes, and is kept, when the user chooses another", () => {
    const native = new NativeTheme();
    const painted: boolean[] = [];
    const theme = new Theme(native, new AppearanceStore(path), (dark) => painted.push(dark));
    theme.choose("dark");
    native.emit("updated");
    expect(native.themeSource).toBe("dark");
    expect(painted).toEqual([true]);
    expect(new AppearanceStore(path).get().theme).toBe("dark");
    // A light system no longer matters.
    native.flip("light");
    expect(painted).toEqual([true, true]);
  });
});
