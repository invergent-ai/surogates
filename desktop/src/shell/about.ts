// About Surogate, as Claude Desktop shows its own (index.chunk-B33tFrRY.js, renderer/about_window/):
// a small modal window over the app's, 320 by 400, one at a time, with the app's name and its
// version, which a click copies with what it runs on, and its documentation. Its page is one
// of the app's files, and draws the window as a whole: no frame, moved by its bare parts.

import { BrowserWindow, clipboard } from "electron";

import { chrome } from "./appearance.js";
import { lockPage } from "./main-window.js";
import { ownPage } from "./window-policy.js";

export interface AboutOptions {
  parent: BrowserWindow; // the app's window
  page: string; // about.html
  preload: string;
  dark: boolean;
  version: string; // the app's own
  documentation(): void;
}

let shown: BrowserWindow | null = null;

/** Open About over *parent*, or bring forward the one already open. */
export function openAbout(options: AboutOptions): void {
  if (shown) return shown.focus();
  const window = new BrowserWindow({
    width: 320,
    height: 400,
    parent: options.parent,
    modal: true,
    show: false,
    frame: false,
    resizable: false,
    minimizable: false,
    maximizable: false,
    fullscreenable: false,
    skipTaskbar: true,
    title: "About Surogate",
    backgroundColor: chrome(options.dark).background,
    webPreferences: { preload: options.preload, sandbox: true, contextIsolation: true, nodeIntegration: false },
  });
  // Linux gives every window the app's menu: its keys would act on the agent's page behind this one.
  window.removeMenu();
  shown = window;
  window.once("closed", () => {
    shown = null;
  });
  const contents = window.webContents;
  lockPage(contents);
  const handle = (channel: string, handler: () => unknown) => {
    contents.ipc.handle(channel, (event) => {
      if (!ownPage(event.senderFrame, options.page)) throw new Error("Not About's own page");
      return handler();
    });
  };
  const versions = `Surogate ${options.version} (Electron ${process.versions.electron}, Chromium ${process.versions.chrome})`;
  handle("about:state", () => ({ version: options.version }));
  handle("about:copy", () => clipboard.writeText(versions));
  handle("about:documentation", () => options.documentation());
  handle("about:close", () => window.close());
  window.once("ready-to-show", () => window.show());
  window.loadFile(options.page).catch((error: unknown) => {
    // Closed while its page still loads: nothing to say.
    if (window.isDestroyed()) return;
    console.error(error);
    window.destroy();
  });
}
