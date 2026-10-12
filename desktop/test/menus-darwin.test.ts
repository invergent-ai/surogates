// The app's menus on macOS: the app's menu first, with About, Settings… on Cmd+, , Hide and Quit; File;
// Edit and View as on Linux, View acting on the agent's page; Developer in developer mode; then Window
// and Help. The menu bar's icon is a template image, for a light menu bar and a dark one alike.

import { existsSync } from "node:fs";
import { join } from "node:path";

import type { MenuItemConstructorOptions } from "electron";
import { describe, expect, it } from "vitest";

import type { MenuActions } from "../src/shell/menus.js";
import { importedOn } from "./platforms.js";

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

const mac = () => importedOn("darwin", () => import("../src/shell/menus.js"));
const items = (menu: MenuItemConstructorOptions[]) =>
  menu.flatMap((top) => ((top.submenu ?? []) as MenuItemConstructorOptions[]).map((item) => ({ top: top.label ?? top.role, ...item })));

describe("the app's menu on macOS", () => {
  it("is the app's menu, File, Edit, View, Developer, Window and Help, each item with its key", async () => {
    const { appMenu } = await mac();
    const menu = appMenu(recording().actions, true);
    expect(menu.map((top) => top.label ?? top.role)).toEqual(["appMenu", "File", "Edit", "View", "Developer", "windowMenu", "Help"]);
    expect(menu.at(-1)?.role).toBe("help");
    expect(items(menu).filter((item) => item.type !== "separator").map((item) => [item.top, item.label ?? item.role, item.accelerator ?? null])).toEqual([
      ["appMenu", "About Surogate", null],
      ["appMenu", "Settings…", "Cmd+,"],
      ["appMenu", "services", null],
      ["appMenu", "hide", null],
      ["appMenu", "hideOthers", null],
      ["appMenu", "unhide", null],
      ["appMenu", "Quit Surogate", "Cmd+Q"],
      ["File", "New chat", "Cmd+N"],
      ["File", "Close window", null],
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

  it("asks each item's action, and leaves to macOS only its own: Services, Hide, Close, Edit's and the Window menu", async () => {
    const { appMenu } = await mac();
    const { asked, actions } = recording();
    const menu = appMenu(actions, true);
    for (const item of items(menu)) (item.click as (() => void) | undefined)?.();
    expect(asked).toEqual([
      ["about"], ["settings"], ["quit"], ["newChat"], ["reload"], ["zoom", 0], ["zoom", 1], ["zoom", 1], ["zoom", -1],
      ["devTools", "agent"], ["devTools", "window"], ["documentation"],
    ]);
    expect([...menu, ...items(menu)].map((item) => item.role).filter(Boolean)).toEqual([
      "appMenu", "windowMenu", "help", "services", "hide", "hideOthers", "unhide", "close", "undo", "redo", "cut", "copy", "paste", "selectAll",
    ]);
  });

  it("has no Developer out of developer mode", async () => {
    const { appMenu } = await mac();
    expect(appMenu(recording().actions, false).map((top) => top.label ?? top.role)).toEqual(["appMenu", "File", "Edit", "View", "windowMenu", "Help"]);
  });
});

it("puts one template image in the menu bar, whatever the theme", async () => {
  const { trayIcon } = await mac();
  expect([trayIcon(true, undefined), trayIcon(false, undefined), trayIcon(false, "GNOME")]).toEqual(["trayTemplate.png", "trayTemplate.png", "trayTemplate.png"]);
  // Its Retina image beside it, as Electron looks for one.
  for (const image of ["trayTemplate.png", "trayTemplate@2x.png"]) expect(existsSync(join(import.meta.dirname, "..", "assets", image)), image).toBe(true);
});
