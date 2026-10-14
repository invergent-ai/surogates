// The main window's controls on Windows: Linux's, Windows's own minimise, maximise and close over the frameless
// window's top right corner in the shell's colours (titleBarOverlay), painted again at each change of theme; and every
// other window of the app's drops the menu Windows would give it, whose keys would act on the agent's page behind it.

import type { BrowserWindow, TitleBarOverlay } from "electron";
import { expect, it, vi } from "vitest";

import { chrome } from "../src/shell/appearance.js";
import { windowControls } from "../src/shell/main-window-win32.js";

it("lays Windows's own buttons over the window in the shell's colours, each theme's, and paints them again at a change", () => {
  for (const dark of [false, true]) {
    const { overlay } = chrome(dark);
    expect(windowControls.options(overlay)).toEqual({ titleBarStyle: "hidden", titleBarOverlay: overlay });
    const setTitleBarOverlay = vi.fn();
    windowControls.paint({ setTitleBarOverlay } as unknown as BrowserWindow, overlay);
    expect(setTitleBarOverlay).toHaveBeenCalledWith(overlay satisfies TitleBarOverlay);
  }
  const removeMenu = vi.fn();
  windowControls.dropMenu({ removeMenu } as unknown as BrowserWindow);
  expect(removeMenu).toHaveBeenCalledOnce();
});
