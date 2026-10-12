// The app's menu, as Claude Desktop builds its own (index.chunk-B33tFrRY.js): File, Edit,
// View, Developer and Help, in place of Electron's default. Its accelerators are the app's
// keys, from every page and view: Chromium matches them on the key's place, whatever the
// keyboard's layout. View acts on the agent's page, never on the window's own. And the tray's menu and icon.

import type { MenuItemConstructorOptions } from "electron";

import { notBuilt } from "../platform.js";
import * as linux from "./menus-linux.js";

export interface MenuActions {
  newChat(): void;
  settings(): void;
  quit(): void;
  reload(): void; // the agent's page
  zoom(step: -1 | 0 | 1): void; // the agent's page; 0 is its own size
  devTools(which: "agent" | "window"): void;
  documentation(): void;
  about(): void;
}

// What differs by OS: the menus before Edit, and the tray's icon (menus-linux.ts).
const OS = process.platform === "linux" ? linux : {
  firstMenus: (): MenuItemConstructorOptions[] => notBuilt("Surogate Desktop's menus"),
  trayIcon: (): string => notBuilt("Surogate Desktop's tray icon"),
};

/** The app's menu; *developer* adds Developer, in developer mode. */
export function appMenu(act: MenuActions, developer: boolean): MenuItemConstructorOptions[] {
  const menu: MenuItemConstructorOptions[] = [
    ...OS.firstMenus(act),
    {
      label: "Edit",
      submenu: [
        { role: "undo" }, { role: "redo" }, { type: "separator" },
        { role: "cut" }, { role: "copy" }, { role: "paste" }, { role: "selectAll" },
      ],
    },
    {
      label: "View",
      submenu: [
        { id: "reload", label: "Reload", accelerator: "CmdOrCtrl+R", click: () => act.reload() },
        { type: "separator" },
        { id: "zoom-reset", label: "Actual size", accelerator: "CmdOrCtrl+0", click: () => act.zoom(0) },
        { id: "zoom-in", label: "Zoom in", accelerator: "CmdOrCtrl+=", click: () => act.zoom(1) },
        // Ctrl and + as a US keyboard types it, Ctrl+Shift+=, as Electron's own Zoom In takes it too.
        { label: "Zoom in", accelerator: "CmdOrCtrl+Plus", visible: false, acceleratorWorksWhenHidden: true, click: () => act.zoom(1) },
        { id: "zoom-out", label: "Zoom out", accelerator: "CmdOrCtrl+-", click: () => act.zoom(-1) },
      ],
    },
    {
      label: "Developer",
      submenu: [
        { id: "dev-agent", label: "Developer tools for the agent's page", click: () => act.devTools("agent") },
        { id: "dev-window", label: "Developer tools for Surogate's window", click: () => act.devTools("window") },
      ],
    },
    {
      label: "Help",
      submenu: [
        { id: "documentation", label: "Documentation", click: () => act.documentation() },
        { type: "separator" },
        { id: "about", label: "About Surogate", click: () => act.about() },
      ],
    },
  ];
  return developer ? menu : menu.filter((top) => top.label !== "Developer");
}

export interface TrayState {
  device: string | null; // how this computer is connected to the agent, as the sidebar says it; null with no agent
  quitting: number | null; // while a quit waits for the threads working on this computer: how many
  shortcut: string | null; // the keys that open quick entry, while the app holds them
}

export interface TrayActions {
  show(): void;
  quickEntry(): void;
  settings(): void;
  quit(): void;
  quitNow(): void; // a quit that waits goes now, with no question
}

// The tray's menu, as Claude Desktop's (index.chunk-B33tFrRY.js): the window and quick entry first, then what to know, then Quit.
export function trayMenu(state: TrayState, act: TrayActions): MenuItemConstructorOptions[] {
  return [
    { label: "Show Surogate", click: () => act.show() },
    // The keys are said, not taken: the app holds them for the whole display, not for its menus.
    { label: "Quick entry", ...(state.shortcut === null ? {} : { accelerator: state.shortcut, registerAccelerator: false }), click: () => act.quickEntry() },
    ...(state.device === null ? [] : [{ label: state.device, enabled: false }]),
    { type: "separator" },
    { label: "Settings…", click: () => act.settings() },
    state.quitting === null ? { label: "Quit Surogate", click: () => act.quit() } : { label: "Quit now", click: () => act.quitNow() },
  ];
}

/** The tray's icon, among the app's assets, for a theme that is *dark* or not, on the desktop *desktop* (XDG_CURRENT_DESKTOP). */
export function trayIcon(dark: boolean, desktop: string | undefined): string {
  return OS.trayIcon(dark, desktop);
}
