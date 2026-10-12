// What of the app's menus is Linux's own: the first, File, which holds the app's own items, Settings
// and Quit, beside the chat's and the window's; and the tray's icon.

import type { MenuItemConstructorOptions } from "electron";

import type { MenuActions } from "./menus.js";

/** The menus before Edit: File. */
export function firstMenus(act: MenuActions): MenuItemConstructorOptions[] {
  return [
    {
      label: "File",
      submenu: [
        { id: "new-chat", label: "New chat", accelerator: "CmdOrCtrl+N", click: () => act.newChat() },
        { id: "settings", label: "Settings…", accelerator: "CmdOrCtrl+Shift+,", click: () => act.settings() },
        { type: "separator" },
        { label: "Close window", role: "close" },
        { id: "quit", label: "Quit Surogate", accelerator: "CmdOrCtrl+Q", click: () => act.quit() },
      ],
    },
  ];
}

/** The tray's icon, by Claude Desktop's rule: the light one on a dark panel, as GNOME's always is, the dark one on a light panel. */
export function trayIcon(dark: boolean, desktop: string | undefined): "tray-dark.png" | "tray-light.png" {
  const gnome = (desktop ?? "").split(":").some((name) => name.toLowerCase() === "gnome");
  return dark || gnome ? "tray-dark.png" : "tray-light.png";
}
