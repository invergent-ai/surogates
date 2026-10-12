// What of the app's menus is macOS's own, as a Mac app's menu bar is laid out (Apple, Human Interface
// Guidelines, "The menu bar"): the app's menu first, named after the app, with About, Settings…, Services,
// Hide and Quit; File, with the chat's and the window's own; Window and Help last. And the menu bar's
// icon, a template image that macOS draws in the menu bar's own colours.

import type { MenuItemConstructorOptions } from "electron";

import type { MenuActions } from "./menus.js";

/** The menus before Edit: the app's menu, then File. */
export function firstMenus(act: MenuActions): MenuItemConstructorOptions[] {
  return [
    {
      // Electron's appMenu role: the menu macOS names after the app (Apple, NSApplication, mainMenu).
      role: "appMenu",
      submenu: [
        { id: "about", label: "About Surogate", click: () => act.about() },
        { type: "separator" },
        { id: "settings", label: "Settings…", accelerator: "Cmd+,", click: () => act.settings() },
        { type: "separator" },
        { role: "services" },
        { type: "separator" },
        { role: "hide" },
        { role: "hideOthers" },
        { role: "unhide" },
        { type: "separator" },
        { id: "quit", label: "Quit Surogate", accelerator: "Cmd+Q", click: () => act.quit() },
      ],
    },
    {
      label: "File",
      submenu: [
        { id: "new-chat", label: "New chat", accelerator: "Cmd+N", click: () => act.newChat() },
        { type: "separator" },
        { label: "Close window", role: "close" },
      ],
    },
  ];
}

/** The menus after View and Developer: Window, as macOS fills it, and Help, which macOS gives its search. */
export function lastMenus(act: MenuActions): MenuItemConstructorOptions[] {
  return [
    // Electron's windowMenu role: Minimize, Zoom and Bring All to Front (Apple, NSApplication, windowsMenu).
    { role: "windowMenu" },
    // Electron's help role: the menu macOS adds its search field to (Apple, NSApplication, helpMenu).
    { label: "Help", role: "help", submenu: [{ id: "documentation", label: "Documentation", click: () => act.documentation() }] },
  ];
}

/**
 * The menu bar's icon: one template image whatever the theme, which macOS tints for a light or a dark
 * menu bar (Apple, NSImage, isTemplate). Electron takes an image whose file name ends in "Template" as
 * one, its @2x beside it for a Retina display (Electron, nativeImage, "Template Image").
 */
export function trayIcon(_dark: boolean, _desktop: string | undefined): "trayTemplate.png" {
  return "trayTemplate.png";
}
