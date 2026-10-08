// The app's one window, shaped as Claude Desktop shapes its main window (index.js):
// frameless, with the system's own minimise, maximise and close drawn over its top
// right corner in the theme's colours (titleBarStyle "hidden" with titleBarOverlay);
// placed as the user left it; hidden when the user closes it, unless Keep running is off. Its own
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

const openOutside = (url: string): void => {
  if (external(url)) void shell.openExternal(url);
};

// The web client stays on the agent's origin, and gets no other powers. The one popup it may
// open, the Composio sign-in, gets no bridge, and opens nothing further. A redirect of the page
// elsewhere, as a single sign-on gateway sends one, is refused and told *onRefused*.
function confine(contents: WebContents, origin: string, onRefused: (url: string) => void): void {
  contents.on("will-navigate", (event) => {
    if (sameOrigin(origin, event.url)) {
      // The agent's sign-in pages open only in the system browser: the window never shows a password form.
      // The web client routes paths whatever their case, so the refusal does too.
      const path = new URL(event.url).pathname.toLowerCase();
      if (path === "/oauth" || path.startsWith("/oauth/")) event.preventDefault();
      return;
    }
    event.preventDefault();
    openOutside(event.url);
  });
  contents.on("will-redirect", (event) => {
    if (!event.isMainFrame || sameOrigin(origin, event.url)) return;
    event.preventDefault();
    onRefused(event.url);
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
    // Linux gives every window the app's menu: its keys would act on the agent's page behind this one.
    popup.removeMenu();
    popup.webContents.setWindowOpenHandler(({ url }) => {
      openOutside(url);
      return { action: "deny" };
    });
    popup.webContents.on("will-attach-webview", (event) => event.preventDefault());
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
  quitsOnClose(): boolean; // asked as the window closes: true quits the app, through its quit's questions, where it would hide
  hidden: boolean; // not shown once its page has loaded
}

export class MainWindow {
  readonly window: BrowserWindow;
  private web: WebView | null = null;
  private webShown = true; // false while the centre shows a page of the shell's own, the Projects page
  private gated = false; // nobody is signed in to the app: the web client stays hidden under the sign-in
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
    // A window started hidden and never shown has no place of its own to keep: the one it was left in stays.
    let shown = !options.hidden;
    this.window.once("show", () => {
      shown = true;
    });
    if (maximized) {
      // Maximising shows a window: one started hidden is maximised once it is first shown.
      if (options.hidden) this.window.once("show", () => this.window.maximize());
      else this.window.maximize();
    }
    lockPage(this.window.webContents);
    if (!options.hidden) this.window.webContents.once("did-finish-load", () => this.show());
    this.window.on("close", (event) => {
      // A place that cannot be kept, as on a full disk, is said, and the window still hides, or quits.
      try {
        if (shown) options.states.save("main", this.window.getNormalBounds(), this.window.isMaximized());
      } catch (error) {
        console.error(error);
      }
      if (closing) return;
      event.preventDefault();
      if (options.quitsOnClose()) app.quit();
      else this.window.hide();
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
    // Settings, or the project dialog, dims the controls with the window under it.
    const { background, overlay } = chrome(dark, this.settingsView !== null);
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
    this.showWeb(this.webShown);
    const contents = view.webContents;
    confine(contents, agent.origin, (url) => {
      web.unreachable = `${agent.name} sent Surogate to ${new URL(url).host}, which it does not open in its window`;
      this.showWeb(this.webShown);
      this.options.onChange();
    });
    contents.on("did-start-loading", () => {
      web.failing = false;
    });
    contents.on("did-fail-load", (_event, code, description, _url, isMainFrame) => {
      // -3 is a load that another load replaced.
      if (!isMainFrame || code === -3) return;
      web.failing = true;
      web.unreachable = `Check your network connection (${description || "the agent did not answer"})`;
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

  /** Take the web client out of the window, as removing its agent does: the next agent attaches its own. */
  detach(): void {
    const web = this.web;
    if (!web) return;
    this.web = null;
    clearTimeout(web.retry);
    this.window.contentView.removeChildView(web.view);
    web.view.webContents.close();
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
    wire(view.webContents);
    this.settingsView = view;
    this.paint(this.dark);
    // Closed while its page still loads, the load ends with it, and nothing is left to say.
    void view.webContents.loadFile(page).then(() => {
      if (this.settingsView === view) view.webContents.focus();
    }, (error: unknown) => {
      if (this.settingsView === view) console.error(error);
    });
  }

  /** Close Settings, and give the keyboard back to what had it, or else to the window's page. */
  closeSettings(): void {
    const view = this.settingsView;
    if (!view) return;
    this.settingsView = null;
    this.paint(this.dark);
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
    this.web?.view.setVisible(shown && !this.gated && this.web.unreachable === null);
  }

  /** Hide the web client while nobody is signed in to the app; show it again once someone is. */
  gate(closed: boolean): void {
    if (this.gated === closed) return;
    this.gated = closed;
    this.showWeb(this.webShown);
  }

  // The centre's hole, as the page measures it.
  place(hole: Bounds): void {
    this.hole = hole;
    this.web?.view.setBounds(hole);
  }

  /** Load *path* of the web client: true once it has loaded, false once it failed, or another load or the shell's refusal ended it. */
  go(path: string): Promise<boolean> {
    return this.web ? this.load(this.web, path) : Promise.resolve(false);
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

  /** The agent's page a step larger or smaller, or at its own size (0); never the window's own page, which the web client is placed by. */
  zoom(step: -1 | 0 | 1): void {
    const contents = this.web?.view.webContents;
    if (!contents) return;
    contents.setZoomLevel(step === 0 ? 0 : Math.min(3, Math.max(-3, contents.getZoomLevel() + step)));
  }

  private load(web: WebView, path: string): Promise<boolean> {
    clearTimeout(web.retry);
    return web.view.webContents.loadURL(`${web.agent.origin}${path}`).then(() => true, () => false);
  }

  // A failed load is tried again, with the link's backoff, once the agent answers its /auth/config.
  private tryAgain(web: WebView): void {
    clearTimeout(web.retry);
    web.retry = setTimeout(() => {
      void net.fetch(`${web.agent.origin}/api/v1/auth/config`).then((response) => response.ok, () => false).then((up) => {
        // Taken out of the window meanwhile, as removing its agent does: that agent is asked nothing more.
        if (this.web !== web) return;
        if (up) web.view.webContents.reload();
        else this.tryAgain(web);
      });
    }, reconnectDelayMs(web.attempt++));
  }
}
