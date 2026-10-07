import type { MenuItemConstructorOptions } from "electron";
import { describe, expect, it } from "vitest";

import { appMenu, type MenuActions } from "../src/shell/menus.js";

// Every action, recording what it was asked.
function recording(): { asked: unknown[][]; actions: MenuActions } {
  const asked: unknown[][] = [];
  const record = (name: string) => (...args: unknown[]) => void asked.push([name, ...args]);
  return {
    asked,
    actions: {
      newChat: record("newChat"), settings: record("settings"), quit: record("quit"), reload: record("reload"),
      zoom: record("zoom"), devTools: record("devTools"), documentation: record("documentation"),
    },
  };
}

const items = (menu: MenuItemConstructorOptions[]) =>
  menu.flatMap((top) => (top.submenu as MenuItemConstructorOptions[]).map((item) => ({ top: top.label, ...item })));

describe("the app's menu", () => {
  it("is File, Edit, View, Developer and Help, each item with its key", () => {
    const menu = appMenu(recording().actions, true);
    expect(menu.map((top) => top.label)).toEqual(["File", "Edit", "View", "Developer", "Help"]);
    expect(items(menu).filter((item) => item.type !== "separator").map((item) => [item.top, item.label ?? item.role, item.accelerator ?? null]))
      .toEqual([
        ["File", "New chat", "CmdOrCtrl+N"],
        ["File", "Settings…", "CmdOrCtrl+Shift+,"],
        ["File", "Close window", null],
        ["File", "Quit Surogate", "CmdOrCtrl+Q"],
        ["Edit", "undo", null],
        ["Edit", "redo", null],
        ["Edit", "cut", null],
        ["Edit", "copy", null],
        ["Edit", "paste", null],
        ["Edit", "selectAll", null],
        ["View", "Reload", "CmdOrCtrl+R"],
        ["View", "Actual size", "CmdOrCtrl+0"],
        ["View", "Zoom in", "CmdOrCtrl+="],
        ["View", "Zoom in", "CmdOrCtrl+Plus"],
        ["View", "Zoom out", "CmdOrCtrl+-"],
        ["Developer", "Developer tools for the agent's page", null],
        ["Developer", "Developer tools for Surogate's window", null],
        ["Help", "Documentation", null],
      ]);
  });

  it("asks each item's action, and reloads or zooms only through the app", () => {
    const { asked, actions } = recording();
    const menu = appMenu(actions, true);
    for (const item of items(menu)) (item.click as (() => void) | undefined)?.();
    expect(asked).toEqual([
      ["newChat"], ["settings"], ["quit"], ["reload"], ["zoom", 0], ["zoom", 1], ["zoom", 1], ["zoom", -1],
      ["devTools", "agent"], ["devTools", "window"], ["documentation"],
    ]);
    // Electron's own reload, zoom and developer tools act on whatever page has the keyboard, the window's own included.
    expect(items(menu).map((item) => item.role).filter(Boolean)).toEqual(["close", "undo", "redo", "cut", "copy", "paste", "selectAll"]);
  });

  it("leaves Developer out of a packaged app, which has no developer mode yet", () => {
    expect(appMenu(recording().actions, false).map((top) => top.label)).toEqual(["File", "Edit", "View", "Help"]);
  });
});
