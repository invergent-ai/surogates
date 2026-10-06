// The app's one window, shaped as Claude Desktop shapes its main window (index.js):
// frameless, with the system's own minimise, maximise and close drawn over its top
// right corner in the theme's colours (titleBarStyle "hidden" with titleBarOverlay);
// placed as the user left it; hidden, not closed, when the user closes it. Its own
// page draws the sidebar, the centre's header and the Overview pane. The agent's web
// client fills the centre's hole, in a WebContentsView of the agent's own partition
// that stays on the agent's origin, as claude.ai fills Claude Desktop's window.

import { app, BrowserWindow, net, screen, shell, type WebContents, WebContentsView, webContents } from "electron";

import { reconnectDelayMs } from "../link/backoff.js";
import { type Agent, partitionFor } from "./agents.js";
import { chrome } from "./appearance.js";
import { external, permitted, sameOrigin, windowOpen } from "./window-policy.js";
import type { Bounds, WindowStates } from "./window-state.js";

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

let openSettings = (): void => {};

/** What Ctrl+Shift+, does, as in Claude Desktop. */
export function onSettingsKey(open: () => void): void {
  openSettings = open;
}

// Ctrl+Q quits, and Ctrl+Shift+, opens Settings, from the window's page and from every view in it.
export function keys(contents: WebContents): void {
  contents.on("before-input-event", (event, input) => {
    if (input.type !== "keyDown" || !input.control || input.alt) return;
    if (!input.shift && input.key.toLowerCase() === "q") {
      event.preventDefault();
      app.quit();
    } else if (input.shift && input.code === "Comma") {
      event.preventDefault();
      openSettings();
    }
  });
}

const openOutside = (url: string): void => {
  if (external(url)) void shell.openExternal(url);
};

// The web client stays on the agent's origin, and gets no other powers. The one popup it may
// open, the Composio sign-in, gets no bridge, and opens nothing further.
function confine(contents: WebContents, origin: string): void {
  contents.on("will-navigate", (event) => {
    if (sameOrigin(origin, event.url)) return;
    event.preventDefault();
    openOutside(event.url);
  });
  contents.on("will-redirect", (event) => {
    if (event.isMainFrame && !sameOrigin(origin, event.url)) event.preventDefault();
  });
  contents.on("will-attach-webview", (event) => event.preventDefault());
  contents.setWindowOpenHandler(({ url, frameName }) => {
    const action = windowOpen(url, frameName);
    if (action === "external") openOutside(url);
    if (action !== "popup") return { action: "deny" };
    const webPreferences = { preload: undefined, additionalArguments: [], sandbox: true, contextIsolation: true, nodeIntegration: false };
    return { action: "allow", overrideBrowserWindowOptions: { webPreferences } };
  });
  contents.on("did-create-window", (popup) => {
    popup.webContents.setWindowOpenHandler(({ url }) => {
      openOutside(url);
      return { action: "deny" };
    });
    popup.webContents.on("will-attach-webview", (event) => event.preventDefault());
    keys(popup.webContents);
  });
  contents.session.setPermissionRequestHandler((_contents, permission, grant, details) =>
    grant(permitted(origin, permission, details.requestingUrl)));
  contents.session.setPermissionCheckHandler((_contents, permission, requestingOrigin) =>
    permitted(origin, permission, requestingOrigin));
}

interface WebView {
  agent: Agent;
  view: WebContentsView;
  unreachable: string | null; // why its last load failed
  failing: boolean; // the load under way failed: the error page that follows is no answer
  attempt: number;
  retry?: NodeJS.Timeout;
}

export interface MainWindowOptions {
  states: WindowStates;
  page: string;
  preload: string; // the page's
  dark: boolean;
  onChange(): void; // what the centre shows changed
}

export class MainWindow {
  readonly window: BrowserWindow;
  private web: WebView | null = null;
  private webShown = true; // false while the centre shows a page of the shell's own, the Projects page
  private hole: Bounds = { x: 0, y: 0, width: 0, height: 0 };
  // Settings, over everything: a transparent view whose page dims the window beneath it.
  private settingsView: WebContentsView | null = null;
  private opener: WebContents | null = null; // what had the keyboard when Settings opened
  private dark: boolean;

  constructor(private readonly options: MainWindowOptions) {
    this.dark = options.dark;
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
      webPreferences: { preload: options.preload, sandbox: true, contextIsolation: true, nodeIntegration: false },
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
    this.dark = dark;
    const { background, overlay } = chrome(dark);
    this.window.setBackgroundColor(background);
    this.window.setTitleBarOverlay(overlay);
    this.web?.view.setBackgroundColor(background);
  }

  get unreachable(): string | null {
    return this.web?.unreachable ?? null;
  }

  // The agent's web client's contents, once attached.
  webContents(): WebContents | undefined {
    return this.web?.view.webContents;
  }

  /** The agent's web client in the centre, made once, with *preload* as its bridge: its contents. */
  attach(agent: Agent, preload: string): WebContents {
    const view = new WebContentsView({
      webPreferences: {
        partition: partitionFor(agent.origin, agent.agentId),
        preload,
        // The preload exposes the bridge only on this origin.
        additionalArguments: [`--surogate-origin=${agent.origin}`],
        sandbox: true,
        contextIsolation: true,
        nodeIntegration: false,
        // Still answering the bridge and the device with the window hidden.
        backgroundThrottling: false,
      },
    });
    view.setBackgroundColor(chrome(this.dark).background);
    view.setBounds(this.hole);
    // Under the window's other views: Settings opens over it.
    this.window.contentView.addChildView(view, 0);
    const web: WebView = { agent, view, unreachable: null, failing: false, attempt: 0 };
    this.web = web;
    const contents = view.webContents;
    confine(contents, agent.origin);
    keys(contents);
    contents.on("did-start-loading", () => {
      web.failing = false;
    });
    contents.on("did-fail-load", (_event, code, description, _url, isMainFrame) => {
      // -3 is a load that another load replaced.
      if (!isMainFrame || code === -3) return;
      web.failing = true;
      web.unreachable = description || "The agent did not answer";
      this.showWeb(this.webShown);
      this.options.onChange();
      this.tryAgain(web);
    });
    // Electron finishes the error page of a failed load too, at the address that failed.
    contents.on("did-finish-load", () => {
      if (web.failing) return;
      clearTimeout(web.retry);
      web.attempt = 0;
      if (web.unreachable === null) return;
      web.unreachable = null;
      this.showWeb(this.webShown);
      this.options.onChange();
    });
    this.load(web, "/");
    return contents;
  }

  /** Open Settings over the window, with *preload*; *wire* registers its page's handlers. */
  openSettings(page: string, preload: string, wire: (contents: WebContents) => void): void {
    if (this.settingsView) {
      this.settingsView.webContents.focus();
      return;
    }
    this.opener = webContents.getFocusedWebContents();
    const view = new WebContentsView({ webPreferences: { preload, sandbox: true, contextIsolation: true, nodeIntegration: false } });
    view.setBackgroundColor("#00000000");
    this.window.contentView.addChildView(view);
    const fit = () => {
      const { width, height } = this.window.getContentBounds();
      view.setBounds({ x: 0, y: 0, width, height });
    };
    fit();
    this.window.on("resize", fit);
    view.webContents.once("destroyed", () => this.window.off("resize", fit));
    lockPage(view.webContents);
    keys(view.webContents);
    wire(view.webContents);
    this.settingsView = view;
    void view.webContents.loadFile(page).then(() => view.webContents.focus());
  }

  /** Close Settings, and give the keyboard back to what had it, or else to the window's page. */
  closeSettings(): void {
    const view = this.settingsView;
    if (!view) return;
    this.settingsView = null;
    this.window.contentView.removeChildView(view);
    view.webContents.close();
    const back = this.opener && !this.opener.isDestroyed() ? this.opener : this.window.webContents;
    this.opener = null;
    back.focus();
  }

  // Settings' contents, while it is open.
  settingsContents(): WebContents | undefined {
    return this.settingsView?.webContents;
  }

  /** The web client in the centre, or the page beneath it: it shows only while it has something to show. */
  showWeb(shown: boolean): void {
    this.webShown = shown;
    this.web?.view.setVisible(shown && this.web.unreachable === null);
  }

  // The centre's hole, as the page measures it.
  place(hole: Bounds): void {
    this.hole = hole;
    this.web?.view.setBounds(hole);
  }

  go(path: string): void {
    if (this.web) this.load(this.web, path);
  }

  back(): void {
    const history = this.web?.view.webContents.navigationHistory;
    if (history?.canGoBack()) history.goBack();
  }

  forward(): void {
    const history = this.web?.view.webContents.navigationHistory;
    if (history?.canGoForward()) history.goForward();
  }

  reload(): void {
    this.web?.view.webContents.reload();
  }

  private load(web: WebView, path: string): void {
    clearTimeout(web.retry);
    void web.view.webContents.loadURL(`${web.agent.origin}${path}`).catch(() => {});
  }

  // A failed load is tried again, with the link's backoff, once the agent answers its /auth/config.
  private tryAgain(web: WebView): void {
    clearTimeout(web.retry);
    web.retry = setTimeout(() => {
      void net.fetch(`${web.agent.origin}/api/v1/auth/config`).then((response) => response.ok, () => false)
        .then((up) => (up ? web.view.webContents.reload() : this.tryAgain(web)));
    }, reconnectDelayMs(web.attempt++));
  }
}
