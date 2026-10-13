// Quick entry's shortcut on Linux, from anywhere on the display, where the X server grabs keys
// for an app; none in a Wayland session, whose portal for global shortcuts the app does not
// ask, and where the tray opens quick entry.

import { expect, it } from "vitest";

import { quickEntryKeys } from "../src/shell/quick-entry-linux.js";

it("takes Ctrl+Alt+Space in an X11 session, and no keys in a Wayland session, as its session type or else its display says", () => {
  expect(quickEntryKeys({ XDG_SESSION_TYPE: "x11", DISPLAY: ":0" })).toBe("Ctrl+Alt+Space");
  expect(quickEntryKeys({ DISPLAY: ":0" })).toBe("Ctrl+Alt+Space");
  expect(quickEntryKeys({ XDG_SESSION_TYPE: "wayland" })).toBeNull();
  expect(quickEntryKeys({ WAYLAND_DISPLAY: "wayland-0" })).toBeNull();
  // The session's type, where it says one, over a display a program left behind.
  expect(quickEntryKeys({ XDG_SESSION_TYPE: "x11", WAYLAND_DISPLAY: "wayland-0" })).toBe("Ctrl+Alt+Space");
});

it("says nothing in Settings while another app holds the keys, and shows its window with the keyboard", async () => {
  const { present, unheld } = await import("../src/shell/quick-entry-linux.js");
  expect(unheld("Ctrl+Alt+Space")).toBeNull();
  const done: string[] = [];
  present({ focus: () => done.push("app") } as never, { show: () => done.push("show"), focus: () => done.push("focus") } as never);
  expect(done).toEqual(["show", "focus"]);
});
