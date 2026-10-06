// The app's one window, shaped as Claude Desktop shapes its main window (index.js):
// frameless, with the system's own minimise, maximise and close drawn over its top
// right corner in the theme's colours (titleBarStyle "hidden" with titleBarOverlay);
// placed as the user left it; hidden, not closed, when the user closes it. Its own
// page draws the sidebar, the centre's header and the Overview pane.

import { app, BrowserWindow, screen, type WebContents } from "electron";

import { chrome } from "./appearance.js";
import type { WindowStates } from "./window-state.js";

let closing = false;

/** The app is quitting for good: from now on a closed window closes. */
export function letWindowClose(): void {
  closing = true;
}

// The app's own pages stay what they are: a dropped file, a link or a script cannot take one
// elsewhere, or open a window from it, and no permission is granted to the app's own session.
export function lockPage(contents: WebContents): void {
  contents.on("will-navigate", (event) => event.preventDefault());
  contents.on("will-redirect", (event) => event.preventDefault());
  contents.on("will-attach-webview", (event) => event.preventDefault());
  contents.setWindowOpenHandler(() => ({ action: "deny" }));
  contents.session.setPermissionRequestHandler((_contents, _permission, grant) => grant(false));
  contents.session.setPermissionCheckHandler(() => false);
}

// Ctrl+Q quits from the window's page and from every view in it.
export function keys(contents: WebContents): void {
  contents.on("before-input-event", (event, input) => {
    if (input.type !== "keyDown" || !input.control || input.alt) return;
    if (!input.shift && input.key.toLowerCase() === "q") {
      event.preventDefault();
      app.quit();
    }
  });
}

export interface MainWindowOptions {
  states: WindowStates;
  page: string;
  dark: boolean;
}

export class MainWindow {
  readonly window: BrowserWindow;

  constructor(options: MainWindowOptions) {
    const { workArea } = screen.getPrimaryDisplay();
    const first = { width: Math.min(1440, workArea.width), height: Math.min(900, workArea.height) };
    const { width, height, maximized, ...at } = options.states.restore(
      "main", first, screen.getAllDisplays().map((display) => display.bounds),
    );
    const { background, overlay } = chrome(options.dark);
    this.window = new BrowserWindow({
      ...at,
      width,
      height,
      minWidth: 960,
      minHeight: 600,
      title: "Surogate",
      show: false,
      titleBarStyle: "hidden",
      titleBarOverlay: overlay,
      backgroundColor: background,
      webPreferences: { sandbox: true, contextIsolation: true, nodeIntegration: false },
    });
    if (maximized) this.window.maximize();
    lockPage(this.window.webContents);
    keys(this.window.webContents);
    this.window.webContents.once("did-finish-load", () => this.show());
    this.window.on("close", (event) => {
      options.states.save("main", this.window.getNormalBounds(), this.window.isMaximized());
      if (closing) return;
      event.preventDefault();
      this.window.hide();
    });
    void this.window.loadFile(options.page);
  }

  show(): void {
    if (!this.window.isVisible()) this.window.show();
    if (this.window.isMinimized()) this.window.restore();
    this.window.focus();
  }

  paint(dark: boolean): void {
    const { background, overlay } = chrome(dark);
    this.window.setBackgroundColor(background);
    this.window.setTitleBarOverlay(overlay);
  }
}
