// Quick entry on Windows: Ctrl+Alt+Space, from anywhere, which Windows gives the app as a hot key of its own; Settings
// says so where another app holds it, and that the notification area's icon opens quick entry then. A hot key's press
// lets the app bring its window forward with the keyboard.

import type { App, BrowserWindow } from "electron";
import { expect, it } from "vitest";

import { present, quickEntryKeys, unheld } from "../src/shell/quick-entry-win32.js";
import { importedOn } from "./platforms.js";

it("takes Ctrl+Alt+Space, whatever the session's environment says", () => {
  expect([quickEntryKeys({}), quickEntryKeys({ XDG_SESSION_TYPE: "wayland", WAYLAND_DISPLAY: "wayland-0" })]).toEqual(["Ctrl+Alt+Space", "Ctrl+Alt+Space"]);
});

it("says in Settings that another app holds the keys, and where quick entry opens instead", async () => {
  expect(unheld("Ctrl+Alt+Space")).toBe("Another app holds Ctrl+Alt+Space, so it does not open quick entry. Open quick entry from Surogate's icon in the notification area.");
  const windows = await importedOn("win32", () => import("../src/shell/quick-entry-keys.js"));
  expect(windows.unheldWords("Ctrl+Alt+Space")).toBe(unheld("Ctrl+Alt+Space"));
  expect(windows.quickEntryKeys({})).toBe("Ctrl+Alt+Space");
});

it("shows its window and gives it the keyboard", () => {
  const done: string[] = [];
  const app = { focus: () => done.push("app") } as unknown as App;
  const window = { show: () => done.push("show"), focus: () => done.push("focus") } as unknown as BrowserWindow;
  present(app, window);
  expect(done).toEqual(["show", "focus"]);
});
