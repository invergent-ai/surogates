// The browser host (spec, Sections 1 and 5): one Chromium-based browser installed on this
// computer, launched for an agent identity with a profile of its own, over a pipe, with its
// own sandbox on and every request through the pinning proxy. Each calling session gets a
// tab, and the popups that tab opens are its too. The host runs in a process of its own
// (main.ts); the browser dies with it, as its pipe closes.

import { randomBytes } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { readdir, readFile, stat } from "node:fs/promises";
import { dirname, join } from "node:path";

import { type BrowserContext, chromium, type Dialog, type Download, type Page } from "playwright-core";

import { MAX_WRITE_BYTES } from "../files/answers.js";
import type { Outcome } from "../link/protocol.js";
import { destination, reach } from "../vm/egress.js";
import { CANCELLED, NEW_TAB, PAUSED } from "./client.js";
import { quoted, type StagedDownload } from "./downloads.js";
import { letGo, OPERATIONS } from "./operations.js";
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
// How long a try of a picked browser waits for it to close.
const TRY_CLOSE_MS = 5_000;
// How long a navigation, a script, a read, a click, a key or a shot may hold its page, as the cloud
// bounds each operation: a page whose own code holds its main thread after its load holds a
// navigation too, and a shot's labels are drawn by code in the page. Above the shot's own 30 s.
const BOUND_MS = 60_000;
const BOUNDED: ReadonlySet<string> = new Set([
  "browser.navigate", "browser.evaluate", "browser.observe", "browser.mouse", "browser.keyboard", "browser.screenshot",
]);
const LATE = Symbol("late");
const HELD = Symbol("held");
// How long a launch's proof that the proxy carries the browser's requests may take.
const CHECK_MS = 15_000;
// How long a new tab may take; and how long a browser that refused one may take to quit, as it does after its last tab.
const TAB_MS = 10_000;
const QUIT_MS = 2_000;
// How long bringing a page to the front may take.
const SHOW_MS = 2_000;
// How long a page whose question was left to its user may take to show that it has been answered.
const ASKING_MS = 500;
// How long a closing browser's processes may take to exit (Edge's take about 5 s on xvfb), below the client's STOP_MS.
const RELEASE_MS = 6_000;
export const PROXY_BYPASSED =
  "The agent's browser would not go through Surogate's proxy: its proxy settings are managed elsewhere on this computer, for example by a policy. So it is not used.";

// What a page did that its agent could not see happen, at most this many to an answer.
const MAX_NOTICES = 20;
export const FILE_ASKED =
  "The page asked for a file to upload. The agent's browser on this computer has no files to give it, so nothing was chosen.";
export const tooLarge = (name: string, bytes: number): string =>
  `The page downloaded ${quoted(name)} (${bytes} bytes), too large to save in the chat's folder at once (at most ${MAX_WRITE_BYTES} bytes), so it was not saved.`;
export const notFinished = (name: string, why: string): string => `The page's download of ${quoted(name)} did not finish (${why}), so it was not saved.`;
const unmeasured = (name: string): string => `The page downloaded ${quoted(name)}, but its size could not be measured, so it was not saved.`;
export const interrupted = (name: string): string =>
  `The page's download of ${quoted(name)} was interrupted when the user took over the agent's browser on this computer, so it was not saved.`;

const failed = (message: string): Outcome => ({ error: { type: "browser", message } });
const DELETED = failed("The chat was deleted, and its tabs closed with it");
const ANOTHER_CHATS = failed("This session's tab in the agent's browser on this computer is another chat's");
export const ASKING = failed(
  "The page asked its user a question while they held the browser, and it is still open. It is theirs to answer, in the agent's browser on this computer: nothing is done in this page until they have.",
);

// A browser's error, its first line: Playwright's call log follows it.
export const said = (error: unknown): string => (error instanceof Error ? error.message : String(error)).split("\n")[0] ?? "";

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

// What *work* settles with, or *answer* at once when *stop* aborts first: whatever *work* settles with
// after that is dropped. Leaves no listener on *stop*, which lasts until the browser is next taken over.
function until<T, S>(work: Promise<T>, stop: AbortSignal, answer: S): Promise<T | S> {
  if (stop.aborted) {
    work.catch(() => {});
    return Promise.resolve(answer);
  }
  return new Promise((resolve, reject) => {
    const stopped = () => resolve(answer);
    stop.addEventListener("abort", stopped, { once: true });
    work.then(resolve, reject).finally(() => stop.removeEventListener("abort", stopped));
  });
}

// The browser's own name for *page*: its target's.
async function targetOf(page: Page): Promise<string> {
  const session = await page.context().newCDPSession(page);
  try {
    return (await session.send("Target.getTargetInfo")).targetInfo.targetId;
  } finally {
    await session.detach().catch(() => {});
  }
}

// A new tab of *context*'s, made behind the tab in front: every tab of the agent's is a tab of one
// window, and one that came to the front would take its user's keys from the page they are in.
// Playwright's own newPage makes it in front. The page is found by its target among the pages the
// context reports: a popup, or a tab its user opened, may be reported at the same moment. Once
// *given* aborts it is looked for no more, and its tab is closed where it was made.
async function behind(context: BrowserContext, given: AbortSignal): Promise<Page> {
  const browser = context.browser();
  if (!browser) throw new Error("The computer's browser has no browser session");
  const cdp = await browser.newBrowserCDPSession();
  const reported: Page[] = [];
  let heard = (): void => {};
  const report = (page: Page) => {
    reported.push(page);
    heard();
  };
  context.on("page", report);
  try {
    const { targetId } = await cdp.send("Target.createTarget", { url: NEW_TAB, background: true });
    const dropped = new Promise<void>((resolve) => given.addEventListener("abort", () => resolve(), { once: true }));
    while (!given.aborted) {
      for (const page of reported.splice(0)) {
        if ((await targetOf(page).catch(() => null)) === targetId) return page;
      }
      if (reported.length === 0) {
        await Promise.race([dropped, new Promise<void>((resolve) => {
          heard = resolve;
        })]);
      }
    }
    await cdp.send("Target.closeTarget", { targetId }).catch(() => {});
    throw new Error("The computer's browser did not open a tab");
  } finally {
    context.off("page", report);
    await cdp.detach().catch(() => {});
  }
}

// A new tab of *context*'s, or why not: a browser that is closing may never answer for one.
function opened(context: BrowserContext): Promise<Page> {
  const given = new AbortController();
  let settle = (): void => {};
  const refused = new Promise<never>((_, reject) => {
    const timer = setTimeout(() => reject(new Error(`The computer's browser did not open a tab within ${TAB_MS / 1_000} s`)), TAB_MS);
    const closed = () => reject(new Error("The computer's browser closed"));
    context.once("close", closed);
    settle = () => {
      clearTimeout(timer);
      context.off("close", closed);
    };
  });
  const page = behind(context, given.signal);
  return Promise.race([page, refused]).catch((error: unknown) => {
    // One that comes after all is no session's.
    given.abort();
    void page.then((late) => late.close(), () => {}).catch(() => {});
    throw error;
  }).finally(settle);
}

// How long a look at this computer's processes may take.
const SCAN_MS = 1_000;
// ponytail: the command lines that did not come within SCAN_MS, for the host's life: a process
// blocked in the kernel (as on a dead mount) blocks its readers too, and each such read holds one of
// libuv's four threads until it returns. They are not asked again.
const unread = new Set<string>();

/**
 * The processes on *profile*, by pid: each names it on its command line. None where /proc is not.
 * Read off the event loop, and passed over where one does not come within SCAN_MS.
 */
export async function holding(profile: string, read = (path: string) => readFile(path, "utf8")): Promise<number[]> {
  const named = [`${profile}\0`, `${profile}/`, `${profile} `];
  const pids = (await readdir("/proc").catch(() => [] as string[])).filter((pid) => /^\d+$/.test(pid));
  const found: number[] = [];
  const waiting = new Set<string>();
  const reads = pids.map((pid) => `/proc/${pid}/cmdline`).filter((path) => !unread.has(path)).map((path) => {
    waiting.add(path);
    return read(path).then((line) => {
      if (named.some((name) => line.includes(name))) found.push(Number(path.split("/")[2]));
    }, () => {}).finally(() => waiting.delete(path));
  });
  let timer: NodeJS.Timeout | undefined;
  await Promise.race([Promise.all(reads), new Promise((resolve) => {
    timer = setTimeout(resolve, SCAN_MS);
  })]);
  clearTimeout(timer);
  for (const path of waiting) unread.add(path);
  return found.sort((a, b) => a - b);
}

/**
 * Settles once no process holds *profile*. A browser can answer its close before its last
 * process has exited, as Edge does, and those still running write the profile again: a removal
 * would leave it half there, and a launch would meet them on it. What is left at RELEASE_MS is killed.
 */
async function released(profile: string): Promise<void> {
  const deadline = Date.now() + RELEASE_MS;
  while ((await holding(profile)).length > 0 && Date.now() < deadline) await new Promise((resolve) => setTimeout(resolve, 50));
  for (const pid of await holding(profile)) {
    try {
      process.kill(pid, "SIGKILL");
    } catch {
      // Gone already.
    }
  }
}

// The browser's own temp folder holds its singleton's socket, whose path a Unix socket keeps
// under 108 bytes: a profile's folder is too deep for it, so it goes in the user's runtime
// folder (this host's own temp files, Playwright's, go under the profiles: main.ts). A try's too.
const browserEnv = () => ({ ...process.env, TMPDIR: process.env.XDG_RUNTIME_DIR || "/tmp" });

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
    // A download is kept in Playwright's own temporary folder, this host's, until it is saved under the chat's folder.
    acceptDownloads: true,
    env: browserEnv(),
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
  downloaded?: (download: StagedDownload) => void; // where each download a page finished goes, to be saved
  downloadBytes?: number; // the most a download may be; a write's most unless told
}

export class BrowserHost {
  private proxy: { server: BrowserProxy; port: number } | null = null;
  private running: Promise<BrowserContext> | null = null;
  // The browser in service: the one running's, once launched, until it closes or is being closed.
  private live: BrowserContext | null = null;
  // A browser being closed whole: the next launch waits for it, as the profile is still its.
  private ending: Promise<void> | null = null;
  // The profile of the browser launched last: a close waits until nothing holds it.
  private profile: string | null = null;
  // Each calling session's pages: its tab first, then the popups it opened, in order.
  private readonly tabs = new Map<string, Page[]>();
  // The chat each calling session is of: its root's. Kept while the browser closes and opens
  // again, so a tab a session opens in the next one is its chat's too.
  private readonly roots = new Map<string, string>();
  // ponytail: how many times each chat was forgotten, for the host's life, one entry per chat
  // deleted while it runs: an operation that waited in its session's line across one opens no tab.
  private readonly forgets = new Map<string, number>();
  // The page a new browser opens with, until a session takes it.
  private spare: Page | null = null;
  // One operation of a session at a time, in the order they came.
  private readonly lines = new Map<string, Promise<unknown>>();
  // What each session's pages did that its agent could not see happen, until its next answer.
  private readonly unseen = new Map<string, string[]>();
  // The sessions whose tab was made and whose agent has not been told: the first navigation to answer
  // in it says it opened, whether or not that navigation made it. One that failed, or a script that came
  // first, has told nobody.
  private readonly untold = new Set<string>();
  // The sessions' pages whose own question (an alert, a confirm, a prompt, a leave-this-page) opened while
  // their user held the browser, and was left for them to answer there: until the page is seen to answer again.
  private readonly asking = new Set<Page>();
  // The chat whose user holds the browser, until that chat hands it back: every chat's operation here is
  // answered paused meanwhile. The browser is the agent's one browser on this computer, every tab a tab of
  // one window, on one profile.
  private held: string | null = null;
  // Aborted when the browser is taken over, for the operations acting then, each of which keeps the signal
  // it began under: answered paused at once, it types, drags and loads no further, and is never taken up
  // again, at a hand back either. Those that begin after a hand back get the next.
  private interrupt = new AbortController();
  private closing = false;

  constructor(private readonly options: BrowserHostOptions = {}) {}

  /** One operation of *session*'s, of the chat *root*, in its tab, launching the browser first if none runs. Never rejects. */
  perform(launch: Launch, root: string, session: string, kind: string, args: Record<string, unknown>, signal: AbortSignal): Promise<Outcome> {
    // A session's tab is its chat's: an operation that names the session under another chat acts in no page
    // of that chat's, and closes none. Only the server could send one.
    const of = this.roots.get(session);
    if (of !== undefined && of !== root && (this.tabs.get(session) ?? []).some((page) => !page.isClosed())) return Promise.resolve(ANOTHER_CHATS);
    this.roots.set(session, root);
    const forgets = this.forgets.get(root);
    // A close does not wait in the session's line: a page stuck in a script closes with the rest.
    const work = kind === "browser.close"
      ? (this.held !== null ? Promise.resolve(PAUSED) : this.closeTab(session).then((closed): Outcome => ({ ok: { closed } })))
      : this.inLine(session, () => {
        if (this.forgets.get(root) !== forgets) return Promise.resolve(DELETED);
        // Its user took the browser over while it waited: it does nothing there.
        return this.held !== null ? Promise.resolve(PAUSED) : this.run(launch, session, kind, args, signal, this.interrupt.signal);
      });
    return Promise.race([work, new Promise<Outcome>((resolve) => {
      if (signal.aborted) resolve(CANCELLED);
      signal.addEventListener("abort", () => resolve(CANCELLED), { once: true });
    })]);
  }

  /**
   * The address of the page *session*'s next operation acts in, once those before it in its line
   * have run: its newest open page's, a popup's or its tab's, or a new tab's. Never rejects.
   */
  address(session: string): Promise<string> {
    return this.inLine(session, async () => (this.tabs.get(session) ?? []).filter((page) => !page.isClosed()).at(-1)?.url() ?? NEW_TAB)
      .catch(() => NEW_TAB);
  }

  /**
   * The browser taken over by the user of the chat *root* (*paused*), for every chat, or handed back by
   * the chat that holds it. Which chat may take it is its client's to say: the last one told holds it.
   */
  pause(root: string, paused: boolean): void {
    if (!paused) {
      if (this.held === root) this.held = null;
      return;
    }
    this.held = root;
    this.interrupt.abort();
    this.interrupt = new AbortController();
    // A button the agent pressed and holds, in any session's page, comes up: not left down under its user's hand.
    for (const pages of this.tabs.values()) for (const page of pages) void letGo(page);
  }

  /**
   * The newest open page of the chat's own tab, else of its first sub-agent with one, brought to the
   * front of its window. Whether there was one. Never rejects.
   */
  async show(root: string): Promise<boolean> {
    const sessions = [...this.roots].filter(([, of]) => of === root).map(([session]) => session)
      .sort((a, b) => Number(b === root) - Number(a === root));
    const page = sessions.map((session) => (this.tabs.get(session) ?? []).filter((open) => !open.isClosed()).at(-1)).find((open) => open);
    if (!page) return false;
    let timer: NodeJS.Timeout | undefined;
    await Promise.race([page.bringToFront().catch(() => {}), new Promise((resolve) => {
      timer = setTimeout(resolve, SHOW_MS);
    })]);
    clearTimeout(timer);
    return true;
  }

  /** Whether *executable* launches headless with its sandbox on within LAUNCH_MS, and its version. Never rejects. */
  async tryBrowser(executable: string): Promise<Outcome> {
    try {
      const browser = await chromium.launch({ executablePath: executable, headless: true, chromiumSandbox: true, env: browserEnv(), timeout: LAUNCH_MS });
      const version = browser.version();
      // A program that launched as a browser but does not close is answered all the same: the host's
      // stop, after the try, ends what is left of it.
      await Promise.race([browser.close(), new Promise((resolve) => setTimeout(resolve, TRY_CLOSE_MS))]);
      return { ok: { version } };
    } catch (error) {
      return failed(said(error));
    }
  }

  /**
   * A deleted chat: every tab of its sessions closes, with the popups they opened. A browser it held
   * stays held: deleting a chat hands nothing back.
   */
  async forget(root: string): Promise<void> {
    this.forgets.set(root, (this.forgets.get(root) ?? 0) + 1);
    const sessions = [...this.roots].filter(([, of]) => of === root).map(([session]) => session);
    // Together, so that closing the browser's last tabs closes it whole.
    await this.closePages(sessions.flatMap((session) => this.untab(session)));
  }

  /** The browser closes, and with it every session's tab. */
  async close(): Promise<void> {
    this.closing = true;
    const running = await this.running?.catch(() => null);
    this.running = null;
    this.live = null;
    if (running) await this.shut(running);
    await this.ending;
    await this.proxy?.server.close();
    this.proxy = null;
  }

  // An operation at its turn. Taken over while it acts, it is answered paused at once, whatever it has
  // done: nothing it reads from the page after that is the agent's, and its session's line goes on.
  private run(launch: Launch, session: string, kind: string, args: Record<string, unknown>, signal: AbortSignal, stop: AbortSignal): Promise<Outcome> {
    return until(this.act(launch, session, kind, args, signal, stop), stop, PAUSED);
  }

  private async act(launch: Launch, session: string, kind: string, args: Record<string, unknown>, signal: AbortSignal, stop: AbortSignal): Promise<Outcome> {
    if (signal.aborted) return CANCELLED;
    const operation = OPERATIONS[kind];
    if (!operation) return { error: { type: "unsupported", message: `This computer's browser does not handle ${kind}` } };
    let page: Page | undefined;
    try {
      const found = await this.pageFor(launch, session, stop);
      // Its user took the browser over while it launched, or while its tab opened: it has no page, and does
      // nothing in one. The last look before it acts, and the only one where its session has its tab already.
      if (found === null || stop.aborted) return PAUSED;
      page = found;
      // A question its page asked its user is theirs still, handed back or not: nothing acts in the page,
      // and nothing waits on it, until they have answered it.
      if (this.asking.has(page)) {
        if (!(await this.answers(page))) return ASKING;
        if (stop.aborted) return PAUSED;
      }
      const value = BOUNDED.has(kind) ? await this.bounded(page, operation(page, args, stop), stop) : await operation(page, args, stop);
      // Taken over while it acted: what its pages did meanwhile stays for its session's next answer.
      if (stop.aborted) return PAUSED;
      if (!isRecord(value) || kind === "browser.observe" || kind === "browser.evaluate") return { ok: value ?? null };
      const notices = this.unseen.get(session) ?? [];
      this.unseen.delete(session);
      return { ok: { ...value, ...(kind === "browser.navigate" ? { opened: this.untold.delete(session) } : {}), notices } };
    } catch (error) {
      // Taken over: a navigation stopped for it is no failure to wait an error page for.
      if (stop.aborted) return PAUSED;
      if (kind !== "browser.navigate") return failed(said(error));
      // The error page a refused navigation shows can commit after goto has given up, and paint
      // later still: the next operation would meet it arriving, a script or a shot (Edge). So it
      // is waited for, a moment, until it has drawn a frame, before the answer.
      if (page && said(error).includes("net::ERR_")) {
        const drawn = page.waitForURL((url) => url.protocol === "chrome-error:", { waitUntil: "commit", timeout: 2_000 })
          .then(() => page?.evaluate("new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)))"));
        await Promise.race([drawn, new Promise((done) => setTimeout(done, 2_000))]).catch(() => {});
      }
      // The browser says only net::ERR_* of what its proxy refused: a navigation's answer says why.
      return failed((await this.refused(args.url)) ?? said(error));
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

  // The newest open page of the session's: a popup it opened, or its tab, made if it has none. Null where
  // none is made for it: its user took the browser over meanwhile.
  private async pageFor(launch: Launch, session: string, stop: AbortSignal): Promise<Page | null> {
    const open = (this.tabs.get(session) ?? []).filter((page) => !page.isClosed());
    const newest = open.at(-1);
    if (newest) return newest;
    const page = await this.tab(launch, stop);
    if (page === null) return null;
    this.tabs.set(session, []);
    this.adopt(session, page);
    this.untold.add(session);
    return page;
  }

  // A new tab: the new browser's first page, or one opened now. A browser that was closing
  // refuses one or never answers: then the tab is opened in the next browser, once. Null once its user
  // took the browser over (*stop*): none is taken or opened for an operation that waited for the launch,
  // and one that opened meanwhile is closed again, no session's.
  private async tab(launch: Launch, stop: AbortSignal, again = true): Promise<Page | null> {
    const context = await this.browser(launch);
    if (stop.aborted) return null;
    const spare = this.spare !== null && !this.spare.isClosed() ? this.spare : null;
    this.spare = null;
    if (spare) return spare;
    let page: Page;
    try {
      page = await opened(context);
    } catch (error) {
      if (again && (await this.gone(context))) return this.tab(launch, stop, false);
      throw error;
    }
    if (!stop.aborted) return page;
    await page.close().catch(() => {});
    return null;
  }

  // Whether *context* is out of service, or goes within QUIT_MS.
  private async gone(context: BrowserContext): Promise<boolean> {
    if (this.live === context) {
      await new Promise<void>((resolve) => {
        const timer = setTimeout(resolve, QUIT_MS);
        context.once("close", () => {
          clearTimeout(timer);
          resolve();
        });
      });
    }
    return this.live !== context;
  }

  // *page* is *session*'s, and so is every popup it opens. A file it asks for opens no dialog, and
  // its agent is told with its next answer; a download it finishes is staged, to be saved under the
  // chat's folder. No service worker answers it (bypassWorkers).
  private adopt(session: string, page: Page): void {
    this.tabs.get(session)?.push(page);
    // Gone from the session's pages once it closes: a long session opens many popups.
    page.once("close", () => {
      const pages = this.tabs.get(session);
      if (pages?.includes(page)) pages.splice(pages.indexOf(page), 1);
      this.asking.delete(page);
    });
    page.on("popup", (popup) => this.adopt(session, popup));
    // With a listener, the browser opens no file dialog of its own: no path the agent did not get reaches a page.
    page.on("filechooser", () => this.note(session, FILE_ASKED));
    page.on("download", (download) => void this.stage(session, download));
  }

  // A download once it has finished, in Playwright's temporary folder: handed on for the chat's folder,
  // or, when it did not finish, cannot be measured or is too large to save, gone, and its agent told why.
  // Whose it is goes by when it started, which is when the browser says so. Started while its user holds
  // the browser, from whichever chat, it is theirs: nothing of it is told to the agent, and no later
  // take-over stops it. Started while the agent drives, it is the agent's, and is interrupted as the
  // operation that started it is, by the signal it began under: taken over before it is handed on, it is
  // stopped where it is and dropped, never taken up again at a hand back, and its agent is told so with its
  // session's next answer. One handed on before the take-over is the chat's to save, as a write of the
  // chat's that waits or asks goes on.
  private async stage(session: string, download: Download): Promise<void> {
    const name = download.suggestedFilename();
    const root = this.roots.get(session);
    const user = this.held !== null;
    const stop = user ? null : this.interrupt.signal;
    const halt = () => void download.cancel().catch(() => {});
    stop?.addEventListener("abort", halt, { once: true });
    // The agent's own is told whatever came of it, held meanwhile or not: it began before any take-over.
    const tell = (notice: string) => {
      if (!user) this.keep(session, notice);
    };
    try {
      let path: string;
      try {
        path = await download.path();
      } catch (error) {
        tell(stop?.aborted ? interrupted(name) : notFinished(name, (await download.failure().catch(() => null)) ?? said(error)));
        return;
      }
      const size = await stat(path).then((found) => found.size, () => null);
      if (stop?.aborted || root === undefined || !this.options.downloaded || size === null || size > (this.options.downloadBytes ?? MAX_WRITE_BYTES)) {
        if (root !== undefined && this.options.downloaded) tell(stop?.aborted ? interrupted(name) : size === null ? unmeasured(name) : tooLarge(name, size));
        await download.delete().catch(() => {});
        return;
      }
      this.options.downloaded({ root, session, name, path, user });
    } finally {
      stop?.removeEventListener("abort", halt);
    }
  }

  // A page's own question (an alert, a confirm, a prompt, a leave-this-page), in any tab of the browser's:
  // a session's, or one its user opened themselves. While the agent drives, it is answered at once and
  // unseen, as Playwright answers one nobody listens for: left open it would hold every operation in its
  // page. While its user holds the browser it is theirs to answer, in the browser: nobody answers it for
  // them, and their agent is told nothing of it. A session's page is kept, as one its agent is refused
  // until they have answered.
  private asked(dialog: Dialog): void {
    if (this.held === null) return void (dialog.type() === "beforeunload" ? dialog.accept() : dialog.dismiss()).catch(() => {});
    const page = dialog.page();
    if (page && [...this.tabs.values()].some((pages) => pages.includes(page))) this.asking.add(page);
  }

  // Whether *page* answers now. One with a question open answers nothing until it is answered: so a page
  // that answers has had its question answered since, and is its agent's again.
  private async answers(page: Page): Promise<boolean> {
    let timer: NodeJS.Timeout | undefined;
    const answered = await Promise.race([page.evaluate("1").then(() => true, () => true), new Promise<false>((resolve) => {
      timer = setTimeout(() => resolve(false), ASKING_MS);
    })]);
    clearTimeout(timer);
    if (answered) this.asking.delete(page);
    return answered;
  }

  // What a session's page did that its agent could not see happen. While its user holds the browser, what
  // a page asks or starts is their own doing, or the page's under their hand: its agent is told nothing of it.
  private note(session: string, notice: string): void {
    if (this.held === null) this.keep(session, notice);
  }

  // Kept for *session*'s next answer that says what its pages did.
  private keep(session: string, notice: string): void {
    const notices = this.unseen.get(session) ?? [];
    if (notices.length < MAX_NOTICES) notices.push(notice);
    this.unseen.set(session, notices);
  }

  // A page that does not answer within the bound is closed: the call stuck on it ends, and its
  // session's line is free again. Another page of the session's is not touched. Once its user took the
  // browser over (*stop*) the wait ends there, and the page stays: no bound closes a page they hold, nor
  // the browser with its last one.
  private async bounded(page: Page, work: Promise<unknown>, stop: AbortSignal): Promise<unknown> {
    const limit = this.options.boundMs ?? BOUND_MS;
    let timer: NodeJS.Timeout | undefined;
    const late = new Promise<typeof LATE>((resolve) => {
      timer = setTimeout(() => resolve(LATE), limit);
    });
    const first = await until(Promise.race([work, late]), stop, HELD).finally(() => clearTimeout(timer));
    if (first === HELD) {
      work.catch(() => {});
      return undefined;
    }
    if (first !== LATE) return first;
    work.catch(() => {});
    await this.closePages([page]);
    throw new Error(`The page did not answer within ${limit / 1_000} s, so it was closed`);
  }

  private async closeTab(session: string): Promise<boolean> {
    const open = this.untab(session);
    await this.closePages(open);
    return open.length > 0;
  }

  // *session* has no tab now: its open pages, to close.
  private untab(session: string): Page[] {
    const pages = this.tabs.get(session) ?? [];
    this.tabs.delete(session);
    this.unseen.delete(session);
    this.untold.delete(session);
    this.roots.delete(session);
    return pages.filter((page) => !page.isClosed());
  }

  // Closed at once, a page stuck in a script too (about 500 ms). The browser quits with its last
  // tab, and would refuse a new one meanwhile: then it is closed whole, taken out of service
  // first, and the next operation launches another once it has gone.
  private async closePages(pages: Page[]): Promise<void> {
    const open = pages.filter((page) => !page.isClosed());
    const context = open[0]?.context();
    if (!context) return;
    if (!context.pages().every((page) => open.includes(page))) {
      await Promise.all(open.map((page) => page.close().catch(() => {})));
      return;
    }
    if (this.live === context) {
      this.live = null;
      this.running = null;
      this.spare = null;
    }
    const ending = this.shut(context);
    this.ending = ending;
    await ending;
    if (this.ending === ending) this.ending = null;
  }

  // *context*'s browser closes, every process of it: one still running at RELEASE_MS is killed.
  private async shut(context: BrowserContext): Promise<void> {
    const closing = context.close().catch(() => {});
    if (this.profile !== null) await released(this.profile);
    await closing;
  }

  // The browser in service, or one launched now: a browser the user closed is launched again.
  private browser(launch: Launch): Promise<BrowserContext> {
    if (this.closing) return Promise.reject(new Error("The computer's browser is closing"));
    if (this.running === null) {
      const mine: Promise<BrowserContext> = this.launch(launch).catch((error: unknown) => {
        if (this.running === mine) this.running = null;
        throw error;
      });
      this.running = mine;
    }
    return this.running;
  }

  // A browser closed by its user, or gone: the next operation launches another, in new tabs. A
  // browser already out of service changes nothing, whatever runs after it.
  private retire(context: BrowserContext): void {
    if (this.live !== context) return;
    this.live = null;
    this.running = null;
    this.spare = null;
    this.tabs.clear();
    this.untold.clear();
  }

  private async launch(launch: Launch): Promise<BrowserContext> {
    // The browser before it has gone, every process of it: a new one on its profile would hand itself to it.
    await this.ending;
    await released(launch.profile);
    this.profile = launch.profile;
    this.proxy ??= await (async () => {
      const server = new BrowserProxy(this.options.proxy);
      return { server, port: await server.listen() };
    })();
    keepWebRtcProxied(launch.profile);
    const context = await chromium.launchPersistentContext(launch.profile, launchOptions(launch.executable, this.proxy.port, this.options.args));
    context.on("close", () => this.retire(context));
    context.on("dialog", (dialog) => this.asked(dialog));
    try {
      await this.bypassWorkers(context);
      this.spare = await this.proxied(context, this.proxy.server);
    } catch (error) {
      await context.close().catch(() => {});
      throw error;
    }
    this.live = context;
    return context;
  }

  // No service worker answers any page of the browser's, or any frame of one, from its first
  // request, so one a page installs never answers another chat's tab (Playwright's own block
  // replaces only navigator.serviceWorker.register). Playwright reports a popup only once its first navigation
  // has been answered, too late for a bypass set then: so each new page is held at its start until
  // its bypass is on. A page whose bypass fails is closed.
  private async bypassWorkers(context: BrowserContext): Promise<void> {
    const browser = context.browser();
    if (!browser) throw new Error("The computer's browser has no browser session");
    // Each page's bypass is set over a non-flat session of root's, the kind Playwright's CDPSession
    // can speak through, which stays attached, and the bypass with it.
    const root = await browser.newBrowserCDPSession();
    const bypassing = new Map<string, Promise<void>>();
    const keepers = new Map<string, string>();
    root.on("Target.detachedFromTarget", ({ sessionId }) => {
      const page = keepers.get(sessionId);
      keepers.delete(sessionId);
      if (page !== undefined) bypassing.delete(page);
    });
    const bypass = async (targetId: string): Promise<void> => {
      try {
        const { sessionId } = await root.send("Target.attachToTarget", { targetId, flatten: false });
        keepers.set(sessionId, targetId);
        // Each is in force once the browser has taken it; the page itself answers only once it runs.
        const send = (id: number, method: string, params = {}) =>
          root.send("Target.sendMessageToTarget", { sessionId, message: JSON.stringify({ id, method, params }) });
        await send(1, "Network.enable");
        await send(2, "Network.setBypassServiceWorker", { bypass: true });
      } catch {
        bypassing.delete(targetId);
        await root.send("Target.closeTarget", { targetId }).catch(() => {});
      }
    };
    const bypassOnce = (targetId: string): Promise<void> => {
      const known = bypassing.get(targetId);
      if (known) return known;
      const done = bypass(targetId);
      bypassing.set(targetId, done);
      return done;
    };
    // A cross-site frame is a target of its own, and its own later navigations are its: each is
    // bypassed as it appears. Its first load is its page's, under the page's bypass already.
    root.on("Target.targetCreated", ({ targetInfo: { targetId, type } }) => {
      if (type === "iframe") void bypassOnce(targetId);
    });
    await root.send("Target.setDiscoverTargets", { discover: true });
    // A holder holds each page that opens after it. Its sessions are flat, which Playwright's
    // CDPSession cannot speak to, so it lets its pages go only by detaching whole: it spends itself
    // on the first, the next is armed, then this one lets its pages go, each once it is bypassed.
    const hold = async (): Promise<void> => {
      const holder = await browser.newBrowserCDPSession();
      const holding: Array<Promise<void>> = [];
      let spent = false;
      holder.on("Target.attachedToTarget", ({ targetInfo: { targetId }, waitingForDebugger }) => {
        const done = bypassOnce(targetId);
        if (!waitingForDebugger) return;
        holding.push(done);
        if (spent) return;
        spent = true;
        // With no next holder, the next page would go unheld: the browser closes instead.
        hold().then(async () => {
          await Promise.all(holding);
          await holder.detach();
        }, () => context.close()).catch(() => {});
      });
      await holder.send("Target.setAutoAttach", { autoAttach: true, waitForDebuggerOnStart: true, flatten: true, filter: [{ type: "page" }] });
    };
    await hold();
    // The pages open before the first holder: the first tab's.
    await Promise.all(bypassing.values());
  }

  // The proxy is proven to carry the browser's requests before any page is the agent's: a policy,
  // or anything else managing the browser's proxy settings, outranks --proxy-server. The page
  // asks for a name only the proxy answers; it is the first tab's page after.
  private async proxied(context: BrowserContext, proxy: BrowserProxy): Promise<Page> {
    const page = context.pages()[0] ?? (await context.newPage());
    const token = randomBytes(8).toString("hex");
    proxy.expect(token);
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
