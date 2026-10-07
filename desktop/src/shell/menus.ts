// The app's menu, as Claude Desktop builds its own (index.chunk-B33tFrRY.js): File, Edit,
// View, Developer and Help, in place of Electron's default. Its accelerators are the app's
// keys, from every page and view: Chromium matches them on the key's place, whatever the
// keyboard's layout. View acts on the agent's page, never on the window's own.

import type { MenuItemConstructorOptions } from "electron";

export interface MenuActions {
  newChat(): void;
  settings(): void;
  quit(): void;
  reload(): void; // the agent's page
  zoom(step: -1 | 0 | 1): void; // the agent's page; 0 is its own size
  devTools(which: "agent" | "window"): void;
  documentation(): void;
}

/** The app's menu; *developer* adds Developer, which a packaged app leaves out until it has a developer mode. */
export function appMenu(act: MenuActions, developer: boolean): MenuItemConstructorOptions[] {
  const menu: MenuItemConstructorOptions[] = [
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
      ],
    },
  ];
  return developer ? menu : menu.filter((top) => top.label !== "Developer");
}
