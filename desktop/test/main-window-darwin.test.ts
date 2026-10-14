// The main window's controls on macOS: the system's own buttons inset over the frameless window's top
// left corner, centred in the pages' top strip, in macOS's colours whatever the theme; and no window of
// the app's drops a menu, a Mac app's one menu bar being its own.

import type { BrowserWindow } from "electron";
import { expect, it } from "vitest";

import { chrome } from "../src/shell/appearance.js";
import { windowControls } from "../src/shell/main-window-darwin.js";

// A window that fails the test at any call made of it.
const untouched = new Proxy({}, { get: (_target, name) => () => expect.unreachable(`the window was asked ${String(name)}`) }) as unknown as BrowserWindow;

it("insets the system's buttons over the window's top left corner, centred in its 36 px strip", () => {
  expect(windowControls.options(chrome(true).overlay)).toEqual({ titleBarStyle: "hiddenInset", trafficLightPosition: { x: 13, y: 11 } });
});

it("paints nothing at a change of theme, and drops no window's menu", () => {
  windowControls.paint(untouched, chrome(false).overlay);
  windowControls.dropMenu(untouched);
});
