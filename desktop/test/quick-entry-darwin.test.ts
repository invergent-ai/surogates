// Quick entry on macOS: Option+Space in every session; Settings says so where another app holds the keys;
// and its window takes the keyboard from the app that had it.

import type { App, BrowserWindow } from "electron";
import { expect, it } from "vitest";

import { present, quickEntryKeys, unheld } from "../src/shell/quick-entry-darwin.js";
import { importedOn } from "./platforms.js";

it("takes Option+Space, whatever the session's environment says", () => {
  expect([quickEntryKeys({}), quickEntryKeys({ XDG_SESSION_TYPE: "wayland", WAYLAND_DISPLAY: "wayland-0" })]).toEqual(["Alt+Space", "Alt+Space"]);
});

it("says in Settings that another app holds the keys, and where quick entry opens instead", async () => {
  expect(unheld("Alt+Space")).toBe("Another app holds Option+Space, so it does not open quick entry. Open quick entry from Surogate's icon in the menu bar.");
  const mac = await importedOn("darwin", () => import("../src/shell/quick-entry-keys.js"));
  expect(mac.unheldWords("Alt+Space")).toBe(unheld("Alt+Space"));
  expect(mac.quickEntryKeys({})).toBe("Alt+Space");
});

it("makes the app the active one before its window shows and takes the keyboard", () => {
  const done: string[] = [];
  const app = { focus: (options?: { steal: boolean }) => done.push(`app ${JSON.stringify(options)}`) } as unknown as App;
  const window = { show: () => done.push("show"), focus: () => done.push("focus") } as unknown as BrowserWindow;
  present(app, window);
  expect(done).toEqual(['app {"steal":true}', "show", "focus"]);
});
