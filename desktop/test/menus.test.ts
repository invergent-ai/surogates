import type { MenuItemConstructorOptions } from "electron";
import { describe, expect, it } from "vitest";

import { appMenu, type MenuActions, trayIcon, trayMenu } from "../src/shell/menus.js";

// Every action, recording what it was asked.
function recording(): { asked: unknown[][]; actions: MenuActions } {
  const asked: unknown[][] = [];
  const record = (name: string) => (...args: unknown[]) => void asked.push([name, ...args]);
  return {
    asked,
    actions: {
      newChat: record("newChat"), settings: record("settings"), quit: record("quit"), reload: record("reload"),
      zoom: record("zoom"), devTools: record("devTools"), documentation: record("documentation"), about: record("about"),
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
        ["Help", "About Surogate", null],
      ]);
  });

  it("asks each item's action, and reloads or zooms only through the app", () => {
    const { asked, actions } = recording();
    const menu = appMenu(actions, true);
    for (const item of items(menu)) (item.click as (() => void) | undefined)?.();
    expect(asked).toEqual([
      ["newChat"], ["settings"], ["quit"], ["reload"], ["zoom", 0], ["zoom", 1], ["zoom", 1], ["zoom", -1],
      ["devTools", "agent"], ["devTools", "window"], ["documentation"], ["about"],
    ]);
    // Electron's own reload, zoom and developer tools act on whatever page has the keyboard, the window's own included.
    expect(items(menu).map((item) => item.role).filter(Boolean)).toEqual(["close", "undo", "redo", "cut", "copy", "paste", "selectAll"]);
  });

  it("leaves Developer out unless developer mode is on", () => {
    expect(appMenu(recording().actions, false).map((top) => top.label)).toEqual(["File", "Edit", "View", "Help"]);
  });
});

describe("the tray", () => {
  it("shows the window, opens quick entry, says how this computer is connected, opens Settings and quits", () => {
    const asked: string[] = [];
    const actions = {
      show: () => asked.push("show"), quickEntry: () => asked.push("quickEntry"), settings: () => asked.push("settings"),
      quit: () => asked.push("quit"), quitNow: () => asked.push("quitNow"),
    };
    const menu = trayMenu({ device: "Connected as Laptop", quitting: null, shortcut: null }, actions);
    expect(menu.map((item) => [item.label ?? item.type, item.enabled ?? true])).toEqual([
      ["Show Surogate", true], ["Quick entry", true], ["Connected as Laptop", false], ["separator", true], ["Settings…", true], ["Quit Surogate", true],
    ]);
    for (const item of menu) (item.click as (() => void) | undefined)?.();
    expect(asked).toEqual(["show", "quickEntry", "settings", "quit"]);
    expect(trayMenu({ device: null, quitting: null, shortcut: null }, actions).map((item) => item.label ?? item.type))
      .toEqual(["Show Surogate", "Quick entry", "separator", "Settings…", "Quit Surogate"]);
    // Quick entry says the keys that open it while the app holds them, and leaves them to the app's own hold.
    expect(menu[1]?.accelerator).toBeUndefined();
    const keyed = trayMenu({ device: null, quitting: null, shortcut: "Ctrl+Alt+Space" }, actions)[1];
    expect([keyed?.label, keyed?.accelerator, keyed?.registerAccelerator]).toEqual(["Quick entry", "Ctrl+Alt+Space", false]);
    // While a quit waits for the threads working on this computer: quit now, with no question.
    const waiting = trayMenu({ device: "Connected as Laptop", quitting: 2, shortcut: null }, actions);
    expect(waiting.at(-1)?.label).toBe("Quit now");
    (waiting.at(-1)?.click as () => void)();
    expect(asked.at(-1)).toBe("quitNow");
  });

  it("takes the light icon on a dark panel, as GNOME's always is, and the dark one on a light panel", () => {
    expect([
      trayIcon(false, "XFCE"), trayIcon(true, "XFCE"), trayIcon(false, "ubuntu:GNOME"), trayIcon(false, "GNOME-Flashback:GNOME"), trayIcon(false, undefined),
    ]).toEqual(["tray-light.png", "tray-dark.png", "tray-dark.png", "tray-dark.png", "tray-light.png"]);
  });
});
