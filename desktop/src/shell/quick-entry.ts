// Quick entry, as Claude Desktop's (index.chunk-C3wD9rT7.js, renderer/quick_window/): a frameless,
// transparent window of 606 by 470 over every other (alwaysOnTop "pop-up-menu"), in the middle of the
// screen the pointer is on, whose box takes a message for a new chat. Enter sends it; Escape, a
// click beside the box, or another window taking the focus hides the window, its text kept until the
// new chat's page says it went. Made at its first opening, then hidden, never closed until the app
// quits. Its page is one of the app's files.

import { BrowserWindow, screen } from "electron";

import { lockPage } from "./main-window.js";
import { ownPage } from "./window-policy.js";

export const WIDTH = 606;
export const HEIGHT = 470;
// Its shortcut, Claude Desktop's own on Linux.
export const QUICK_ENTRY_KEYS = "Ctrl+Alt+Space";
// The longest message it takes, in UTF-16 units, as JavaScript counts them: a paste of a whole book is said, not sent.
export const LONGEST = 100_000;

/**
 * A Wayland session, as Claude Desktop tells one: by its session type, or else by a Wayland display.
 * Electron's globalShortcut grabs keys from the X server, and no app's grab is global there.
 */
export const waylandSession = (env: NodeJS.ProcessEnv): boolean =>
  env.XDG_SESSION_TYPE ? env.XDG_SESSION_TYPE === "wayland" : Boolean(env.WAYLAND_DISPLAY);

export interface QuickEntryOptions {
  page: string; // quick.html
  preload: string;
  // The text the user sent, never blank: why it was not sent, or null once the new chat's page sent
  // it. It hides the window (hide()) as it hands the text over: the page may answer much later.
  send(text: string): Promise<string | null>;
}

export class QuickEntry {
  private window: BrowserWindow | null = null;

  constructor(private readonly options: QuickEntryOptions) {}

  /** Hidden, it shows, with the keyboard; shown, it hides: as its shortcut acts. */
  toggle(): void {
    if (this.window?.isVisible()) {
      this.window.hide();
    } else if (this.window) {
      this.reveal(this.window);
    } else {
      const made = this.make();
      made.once("ready-to-show", () => this.reveal(made));
    }
  }

  hide(): void {
    this.window?.hide();
  }

  private reveal(window: BrowserWindow): void {
    if (window.isDestroyed()) return;
    const { workArea } = screen.getDisplayNearestPoint(screen.getCursorScreenPoint());
    window.setBounds({
      x: workArea.x + Math.round((workArea.width - WIDTH) / 2),
      y: workArea.y + Math.round((workArea.height - HEIGHT) / 2),
      width: WIDTH,
      height: HEIGHT,
    });
    window.show();
    window.focus();
  }

  private make(): BrowserWindow {
    const { page, preload } = this.options;
    const window = new BrowserWindow({
      width: WIDTH,
      height: HEIGHT,
      show: false,
      frame: false,
      transparent: true,
      backgroundColor: "#00000000",
      hasShadow: false,
      resizable: false,
      minimizable: false,
      maximizable: false,
      fullscreenable: false,
      skipTaskbar: true,
      title: "Quick entry",
      webPreferences: { preload, sandbox: true, contextIsolation: true, nodeIntegration: false },
    });
    window.setAlwaysOnTop(true, "pop-up-menu");
    // Linux gives every window the app's menu: its keys would act on the agent's page behind this one.
    window.removeMenu();
    // Another window taking the focus hides it, as Claude's does.
    window.on("blur", () => window.hide());
    window.once("closed", () => {
      if (this.window === window) this.window = null;
    });
    this.window = window;
    const contents = window.webContents;
    lockPage(contents);
    const handle = (channel: string, handler: (...args: unknown[]) => unknown) => {
      contents.ipc.handle(channel, (event, ...args: unknown[]) => {
        if (!ownPage(event.senderFrame, page)) throw new Error("Not quick entry's own page");
        return handler(...args);
      });
    };
    handle("quick:send", (text) => {
      if (typeof text !== "string") throw new Error("Not a message");
      if (text.length > LONGEST) return `Surogate sends a message of up to ${LONGEST.toLocaleString("en")} characters.`;
      // Nothing but spaces sends nothing, as Enter in an empty box.
      if (text.trim() !== "") return this.options.send(text);
      window.hide();
      return null;
    });
    handle("quick:dismiss", () => window.hide());
    window.loadFile(page).catch((error: unknown) => {
      // Closed while its page still loads, as by a quit: nothing to say.
      if (window.isDestroyed()) return;
      console.error(error);
      window.destroy();
    });
    return window;
  }
}
