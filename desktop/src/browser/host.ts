// The browser host (spec, Sections 1 and 5): one Chromium-based browser installed on this
// computer, launched for an agent identity with a profile of its own, over a pipe, with its
// own sandbox on and every request through the pinning proxy. Each calling session gets a
// tab, and the popups that tab opens are its too. The host runs in a process of its own
// (main.ts); the browser dies with it, as its pipe closes.

import { randomBytes } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";

import { type BrowserContext, chromium, type Page } from "playwright-core";

import type { Outcome } from "../link/protocol.js";
import { destination, reach } from "../vm/egress.js";
import { CANCELLED } from "./client.js";
import { OPERATIONS } from "./operations.js";
import { BrowserProxy, type BrowserProxyOptions, CHECK_DOMAIN } from "./proxy.js";

// What to launch: the browser the user chose, and the identity's profile for it.
export interface Launch {
  executable: string;
  profile: string;
}

// Playwright's own --disable-features (playwright-core 1.63.0), dropped whole: it holds HttpsUpgrades.
const PLAYWRIGHT_FEATURES =
  "--disable-features=AvoidUnnecessaryBeforeUnloadCheckSync,DestroyProfileOnBrowserClose,DialMediaRouteProvider,GlobalMediaControls,HttpsUpgrades,LensOverlay,MediaRouter,PaintHolding,ThirdPartyStoragePartitioning,BlockOriginHeaderModificationOnRedirect,Translate,AutoDeElevate,OptimizationHints,msForceBrowserSignIn,msEdgeUpdateLaunchServicesPreferredVersion";
// Playwright's defaults that weaken a browser a person uses: popups, Safe Browsing,
// component and CRL updates, saved logins kept without the keyring, and the https upgrade.
export const WEAKENING = [
  "--disable-popup-blocking",
  "--disable-client-side-phishing-detection",
  "--disable-component-update",
  "--disable-background-networking",
  "--password-store=basic",
  "--use-mock-keychain",
  PLAYWRIGHT_FEATURES,
];
// Playwright's list without what guards a browser a person signs in to: the https upgrade, and
// third-party storage kept apart by the site it is embedded in. The rest is the browser's own UI.
const FEATURES = PLAYWRIGHT_FEATURES.replace(",HttpsUpgrades", "").replace(",ThirdPartyStoragePartitioning", "");

const LAUNCH_MS = 30_000;
// How long a script, a read, a click or a key may hold its page, as the cloud bounds each operation.
const BOUND_MS = 60_000;
const BOUNDED: ReadonlySet<string> = new Set(["browser.evaluate", "browser.observe", "browser.mouse", "browser.keyboard"]);
const LATE = Symbol("late");
// How long a launch's proof that the proxy carries the browser's requests may take.
const CHECK_MS = 15_000;
export const PROXY_BYPASSED =
  "The agent's browser would not go through Surogate's proxy: its proxy settings are managed elsewhere on this computer, for example by a policy. So it is not used.";

// What a page did that its agent could not see happen, at most this many to an answer.
const MAX_NOTICES = 20;
export const FILE_ASKED =
  "The page asked for a file to upload. The agent's browser on this computer has no files to give it, so nothing was chosen.";
export const downloaded = (name: string): string =>
  `The page started a download (${JSON.stringify(name.slice(0, 200))}). This computer does not keep the agent's downloads, so it was not saved.`;

const failed = (message: string): Outcome => ({ error: { type: "browser", message } });

// A browser's error, its first line: Playwright's call log follows it.
export const said = (error: unknown): string => (error instanceof Error ? error.message : String(error)).split("\n")[0] ?? "";

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

/** How the browser is launched: headed, its sandbox on, over the pipe, and every request through the proxy on *port*. */
export function launchOptions(executable: string, port: number, extra: readonly string[] = []) {
  return {
    executablePath: executable,
    headless: false,
    chromiumSandbox: true,
    // The page takes the window's size, as a person's does.
    viewport: null,
    ignoreDefaultArgs: WEAKENING,
    args: [FEATURES, `--proxy-server=http://127.0.0.1:${port}`, "--proxy-bypass-list=<-loopback>", ...extra],
    serviceWorkers: "block" as const,
    // Nothing the agent's pages download is kept: no file reaches this computer that way.
    acceptDownloads: false,
    // The host decides when the browser ends: with its own end.
    handleSIGINT: false,
    handleSIGTERM: false,
    handleSIGHUP: false,
    timeout: LAUNCH_MS,
  };
}

/**
 * The profile's WebRTC preference, written before each launch, whatever the browser wrote at
 * its last exit: WebRTC sends no UDP around the proxy (spec, Section 5: the command-line switch has no effect).
 */
export function keepWebRtcProxied(profile: string): void {
  const path = join(profile, "Default", "Preferences");
  mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
  let prefs: Record<string, unknown> = {};
  try {
    const read: unknown = JSON.parse(readFileSync(path, "utf8"));
    if (isRecord(read)) prefs = read;
  } catch {
    // None yet, or one the browser would reset anyway.
  }
  const webrtc = isRecord(prefs.webrtc) ? prefs.webrtc : {};
  writeFileSync(path, JSON.stringify({ ...prefs, webrtc: { ...webrtc, ip_handling_policy: "disable_non_proxied_udp" } }));
}

export interface BrowserHostOptions {
  proxy?: BrowserProxyOptions; // what the proxy judges and dials with; the system's by default
  args?: readonly string[]; // switches added to every launch: the tests' own
  boundMs?: number; // BOUND_MS unless told
}

export class BrowserHost {
  private proxy: { server: BrowserProxy; port: number } | null = null;
  private running: Promise<BrowserContext> | null = null;
  // Each calling session's pages: its tab first, then the popups it opened, in order.
  private readonly tabs = new Map<string, Page[]>();
  // The chat each calling session is of: its root's.
  private readonly roots = new Map<string, string>();
  // The page a new browser opens with, until a session takes it.
  private spare: Page | null = null;
  // One operation of a session at a time, in the order they came.
  private readonly lines = new Map<string, Promise<unknown>>();
  // What each session's pages did that its agent could not see happen, until its next answer.
  private readonly unseen = new Map<string, string[]>();
  private closing = false;

  constructor(private readonly options: BrowserHostOptions = {}) {}

  /** One operation of *session*'s, of the chat *root*, in its tab, launching the browser first if none runs. Never rejects. */
  perform(launch: Launch, root: string, session: string, kind: string, args: Record<string, unknown>, signal: AbortSignal): Promise<Outcome> {
    this.roots.set(session, root);
    // A close does not wait in the session's line: a page stuck in a script closes with the rest.
    const work = kind === "browser.close"
      ? this.closeTab(session).then((closed): Outcome => ({ ok: { closed } }))
      : this.inLine(session, () => this.run(launch, session, kind, args, signal));
    return Promise.race([work, new Promise<Outcome>((resolve) => {
      if (signal.aborted) resolve(CANCELLED);
      signal.addEventListener("abort", () => resolve(CANCELLED), { once: true });
    })]);
  }

  /** Whether *executable* launches headless with its sandbox on within LAUNCH_MS, and its version. Never rejects. */
  async tryBrowser(executable: string): Promise<Outcome> {
    try {
      const browser = await chromium.launch({ executablePath: executable, headless: true, chromiumSandbox: true, timeout: LAUNCH_MS });
      const version = browser.version();
      await browser.close();
      return { ok: { version } };
    } catch (error) {
      return failed(said(error));
    }
  }

  /** A deleted chat: every tab of its sessions closes, with the popups they opened. */
  async forget(root: string): Promise<void> {
    const sessions = [...this.roots].filter(([, of]) => of === root).map(([session]) => session);
    await Promise.all(sessions.map((session) => this.closeTab(session)));
  }

  /** The browser closes, and with it every session's tab. */
  async close(): Promise<void> {
    this.closing = true;
    const running = await this.running?.catch(() => null);
    this.running = null;
    await running?.close().catch(() => {});
    await this.proxy?.server.close();
    this.proxy = null;
  }

  private async run(launch: Launch, session: string, kind: string, args: Record<string, unknown>, signal: AbortSignal): Promise<Outcome> {
    if (signal.aborted) return CANCELLED;
    const operation = OPERATIONS[kind];
    if (!operation) return { error: { type: "unsupported", message: `This computer's browser does not handle ${kind}` } };
    try {
      const { page, opened } = await this.pageFor(launch, session);
      const value = BOUNDED.has(kind) ? await this.bounded(page, operation(page, args)) : await operation(page, args);
      if (!isRecord(value) || kind === "browser.observe" || kind === "browser.evaluate") return { ok: value ?? null };
      const notices = this.unseen.get(session) ?? [];
      this.unseen.delete(session);
      return { ok: { ...value, ...(kind === "browser.navigate" ? { opened } : {}), notices } };
    } catch (error) {
      // The browser says only net::ERR_* of what its proxy refused: a navigation's answer says why.
      return failed((kind === "browser.navigate" ? await this.refused(args.url) : null) ?? said(error));
    }
  }

  // Why the proxy refuses the address a navigation opened, as the host proxy tells a command; null when it would not.
  private async refused(url: unknown): Promise<string | null> {
    let address: URL;
    try {
      address = new URL(String(url));
    } catch {
      return null;
    }
    const found = destination(address.hostname, Number(address.port || (address.protocol === "https:" ? 443 : 80)));
    if (!found) return null;
    const key = `${found.host}:${found.port}`;
    const where = await reach(found.host, this.options.proxy).catch(() => null);
    if (where === null) return `The agent's browser could not look up ${found.host}`;
    if (where.reach === "own") return `The agent's browser does not reach this computer's own services (${key})`;
    return where.reach === "private" ? `The agent's browser does not reach private networks (${key})` : null;
  }

  // The newest open page of the session's: a popup it opened, or its tab, made if it has none.
  private async pageFor(launch: Launch, session: string): Promise<{ page: Page; opened: boolean }> {
    const open = (this.tabs.get(session) ?? []).filter((page) => !page.isClosed());
    const newest = open.at(-1);
    if (newest) return { page: newest, opened: false };
    const context = await this.browser(launch);
    const spare = this.spare !== null && !this.spare.isClosed() ? this.spare : null;
    this.spare = null;
    const page = spare ?? (await context.newPage());
    this.tabs.set(session, []);
    await this.adopt(session, page);
    return { page, opened: true };
  }

  // *page* is *session*'s, and so is every popup it opens. A file it asks for opens no dialog,
  // and a download it starts is not kept; its agent is told of each with its next answer.
  // Service workers answer none of its requests, so one a page installs never answers another
  // chat's tab (Playwright's own block replaces only navigator.serviceWorker.register).
  private async adopt(session: string, page: Page): Promise<void> {
    this.tabs.get(session)?.push(page);
    page.on("popup", (popup) => void this.adopt(session, popup).catch(() => {}));
    // With a listener, the browser opens no file dialog of its own: no path the agent did not get reaches a page.
    page.on("filechooser", () => this.note(session, FILE_ASKED));
    page.on("download", (download) => this.note(session, downloaded(download.suggestedFilename())));
    const protocol = await page.context().newCDPSession(page);
    await protocol.send("Network.enable");
    await protocol.send("Network.setBypassServiceWorker", { bypass: true });
  }

  private note(session: string, notice: string): void {
    const notices = this.unseen.get(session) ?? [];
    if (notices.length < MAX_NOTICES) notices.push(notice);
    this.unseen.set(session, notices);
  }

  // A page that does not answer within the bound is closed: the call stuck on it ends, and its
  // session's line is free again. Another page of the session's is not touched.
  private async bounded(page: Page, work: Promise<unknown>): Promise<unknown> {
    const limit = this.options.boundMs ?? BOUND_MS;
    let timer: NodeJS.Timeout | undefined;
    const late = new Promise<typeof LATE>((resolve) => {
      timer = setTimeout(() => resolve(LATE), limit);
    });
    const first = await Promise.race([work, late]).finally(() => clearTimeout(timer));
    if (first !== LATE) return first;
    work.catch(() => {});
    await this.closePages([page]);
    throw new Error(`The page did not answer within ${limit / 1_000} s, so it was closed`);
  }

  private async closeTab(session: string): Promise<boolean> {
    const pages = this.tabs.get(session) ?? [];
    this.tabs.delete(session);
    this.unseen.delete(session);
    this.roots.delete(session);
    const open = pages.filter((page) => !page.isClosed());
    await this.closePages(open);
    return open.length > 0;
  }

  // Closed at once, a page stuck in a script too (about 500 ms). The browser quits with its last
  // tab, and would refuse a new one meanwhile: then it is closed whole, and the next operation launches it.
  private async closePages(pages: Page[]): Promise<void> {
    const open = pages.filter((page) => !page.isClosed());
    const context = open[0]?.context();
    if (!context) return;
    if (context.pages().every((page) => open.includes(page))) await context.close().catch(() => {});
    else await Promise.all(open.map((page) => page.close().catch(() => {})));
  }

  // The browser that runs, or one launched now: a browser the user closed is launched again.
  private browser(launch: Launch): Promise<BrowserContext> {
    if (this.closing) return Promise.reject(new Error("The computer's browser is closing"));
    this.running ??= this.launch(launch).catch((error: unknown) => {
      this.running = null;
      throw error;
    });
    return this.running;
  }

  private async launch(launch: Launch): Promise<BrowserContext> {
    this.proxy ??= await (async () => {
      const server = new BrowserProxy(this.options.proxy);
      return { server, port: await server.listen() };
    })();
    keepWebRtcProxied(launch.profile);
    const context = await chromium.launchPersistentContext(launch.profile, launchOptions(launch.executable, this.proxy.port, this.options.args));
    context.on("close", () => {
      // Closed by its user, or gone: the next operation launches it again, in new tabs.
      this.running = null;
      this.spare = null;
      this.tabs.clear();
      this.roots.clear();
    });
    try {
      this.spare = await this.proxied(context, this.proxy.server);
    } catch (error) {
      await context.close().catch(() => {});
      throw error;
    }
    return context;
  }

  // The proxy is proven to carry the browser's requests before any page is the agent's: a policy,
  // or anything else managing the browser's proxy settings, outranks --proxy-server. The page
  // asks for a name only the proxy answers; it is the first tab's page after.
  private async proxied(context: BrowserContext, proxy: BrowserProxy): Promise<Page> {
    const page = context.pages()[0] ?? (await context.newPage());
    const token = randomBytes(8).toString("hex");
    await page.goto(`http://${token}${CHECK_DOMAIN}/`, { timeout: CHECK_MS }).catch(() => {});
    if (!proxy.checked(token)) throw new Error(PROXY_BYPASSED);
    await page.goto("about:blank").catch(() => {});
    return page;
  }

  private inLine<T>(session: string, work: () => Promise<T>): Promise<T> {
    const before = this.lines.get(session) ?? Promise.resolve();
    const mine = before.then(work, work);
    const settled = mine.then(() => {}, () => {});
    this.lines.set(session, settled);
    void settled.then(() => {
      if (this.lines.get(session) === settled) this.lines.delete(session);
    });
    return mine;
  }
}
