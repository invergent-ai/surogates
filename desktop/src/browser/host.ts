// The browser host (spec, Sections 1 and 5): one Chromium-based browser installed on this
// computer, launched for an agent identity with a profile of its own, over a pipe, with its
// own sandbox on and every request through the pinning proxy. Each calling session gets a
// tab, and the popups that tab opens are its too. The host runs in a process of its own
// (main.ts); the browser dies with it, as its pipe closes.

import { randomBytes } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { mkdtemp, readdir, readFile, rm, stat } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";

import { type BrowserContext, chromium, type Dialog, type Download, type FileChooser, type Page, type Request } from "playwright-core";

import { MAX_WRITE_BYTES } from "../files/answers.js";
import type { Outcome } from "../link/protocol.js";
import { destination, reach } from "../vm/egress.js";
import { CANCELLED, NEW_TAB, PAUSED } from "./client.js";
import { interrupted, LEFT_TO_USER, quoted, type StagedDownload, tooLarge } from "./downloads.js";
import { letGo, OPERATIONS, stoppedIn } from "./operations.js";
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
// How long after a take-over the agent's pages still open no file chooser of the browser's own: what the
// agent was doing in a page then (a click on its way, a button this host lets go) has reached it by then.
export const OWN_CHOOSER_MS = 1_000;
// How long a page may take to say where a file input of its is, for an upload's prompt.
const LOOK_MS = 2_000;
// How long a closing browser's processes may take to exit (Edge's take about 5 s on xvfb), below the client's STOP_MS.
const RELEASE_MS = 6_000;
export const PROXY_BYPASSED =
  "The agent's browser would not go through Surogate's proxy: its proxy settings are managed elsewhere on this computer, for example by a policy. So it is not used.";

// How long after the browser was handed back a download with no request known is still taken for its
// user's: the browser says nothing of the request of a link with `download`, of a blob or of a data
// address until the file comes, so one that comes then may have been asked for while they held it. A
// site that takes longer than this to answer such a request is taken for the agent's.
export const AFTER_HAND_BACK_MS = 60_000;
// How many navigations not yet ended are kept, the oldest going first.
const REQUESTS = 256;
// How long after the browser gave a navigation up as a page a download may still be its answer: the
// browser gives up a navigation that becomes a download a moment (5 to 15 ms) before it announces the
// download, and nothing else makes a failed navigation a download's beginning. One that failed longer
// ago is of no download: a refused site, a page closed, a download of that address asked for anew.
export const AFTER_FAILURE_MS = 1_000;
// The page a request is of; none for a new window's first, which is said before the window is a page.
function pageOf(request: Request): Page | null {
  try {
    return request.frame().page();
  } catch {
    return null;
  }
}
// An address without its fragment, which a request's does not carry.
const bare = (url: string): string => url.split("#")[0] ?? url;

// When a navigation's request began: the chat the browser was held from then, or null with nobody holding
// it; what a take-over since would have stopped it by; and when the browser gave it up as a page.
interface Begun {
  by: string | null;
  stop: AbortSignal;
  failed?: number;
}

// What a page did that its agent could not see happen, at most this many to an answer.
const MAX_NOTICES = 20;
export const FILE_ASKED =
  "The page asked for a file to upload. Nothing was chosen: browser_upload_file gives it files of the chat's folder.";
export const NOT_ASKED = "The page has not asked for a file: click its upload button or its file input first";
// An upload its user was asked about, whose input is not where its prompt said by the time its files come.
export const NOT_AS_ASKED =
  "The page is not as it was when the user was asked about this upload, so it was given nothing: click its upload button or its file input again, then upload again";
export const NO_SITE = "The file input that asked is in a frame that runs as no site, so it is given no files";
export const ONE_FILE = "The page's file input takes one file at a time";
export const A_FOLDER = "The page asked for a folder, which the agent's browser on this computer does not give";
export const BUSY = "The page was too busy to take the files, so it was given none of them";
// How long the one step that puts an upload's files into its input may take to reach the page, and how
// many times it is sent to a page too busy for that.
const GIVE_MS = 250;
const GIVE_TRIES = 10;
// Told of an upload's own end where its answer could not say it: it was answered paused.
export const GIVEN_AS_TAKEN = "The files of an upload were given to the page just as the user took over the agent's browser on this computer.";
export const notFinished = (name: string, why: string): string => `The page's download of ${quoted(name)} did not finish (${why}), so it was not saved.`;
// Why one did not finish: the browser's own word for one that was cancelled, or whose connection broke; of
// any other, this host's words. What an error says itself is not the agent's to read.
const unfinished = (failure: string | null): string => (failure === "canceled" ? failure : "the browser stopped it");
const unmeasured = (name: string): string => `The page downloaded ${quoted(name)}, but its size could not be measured, so it was not saved.`;

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

// What only looks at a page, as the approvals take it (binding/approvals.ts): it asks no one, so it can run while
// an upload's prompt is open, and the input that prompt named stays held.
const looks = (kind: string, args: Record<string, unknown>): boolean =>
  kind === "browser.observe" || kind === "browser.screenshot" || (kind === "browser.mouse" && (args.action === "move" || args.action === "wheel"));

// A file for a page: its name, its type, and what it holds, in base64.
interface UploadFile {
  name: string;
  mimeType: string;
  buffer: string;
}

// The most files one upload gives a page, as the main side reads them (executor.ts); and the longest a
// file's name or its type may be, in bytes: what a file system takes for a name.
const MAX_UPLOAD_FILES = 10;
const MAX_NAME_BYTES = 255;
// Base64 as the page decodes it: its alphabet, padded to a whole number of fours. Scanned once, end to end.
const BASE64 = /^[A-Za-z0-9+/]*={0,2}$/;

/**
 * An upload's files as the main side sent them: one to ten, each a plain name, a type and its data in
 * base64, of at most what a write may carry in all. Null for anything else: the page is then given none
 * of them. Only those three of each go on to the page.
 */
export function filesOf(value: unknown): UploadFile[] | null {
  if (!Array.isArray(value) || value.length === 0 || value.length > MAX_UPLOAD_FILES) return null;
  let bytes = 0;
  const files = value.map((file: unknown) => {
    if (!isRecord(file)) return null;
    const { name, mimeType, buffer } = file;
    const plain = typeof name === "string" && name !== "" && name !== "." && name !== ".." && !/[/\\\0]/.test(name)
      && Buffer.byteLength(name) <= MAX_NAME_BYTES;
    if (!plain || typeof mimeType !== "string" || Buffer.byteLength(mimeType) > MAX_NAME_BYTES) return null;
    if (typeof buffer !== "string" || buffer.length % 4 !== 0 || !BASE64.test(buffer)) return null;
    bytes += Buffer.byteLength(buffer, "base64");
    return { name, mimeType, buffer };
  });
  return bytes <= MAX_WRITE_BYTES && files.every((file) => file !== null) ? files : null;
}

// Where a file input is: whether it is still a file input of its frame's own document, at what address
// that frame is, and as what site it runs. Said in the page, in the isolated world the input's handle
// lives in (Playwright's own, one for each frame), which no script of the page's reaches: so it is the
// browser's word, not the page's.
interface Place {
  here: boolean;
  href: string;
  origin: string;
}
const place = (input: Node): Place => ({
  here: input instanceof HTMLInputElement && input.type === "file" && input.isConnected && input.ownerDocument === document,
  href: location.href,
  origin: self.origin,
});
// The address that names the site a file given there goes to: its frame's own, or, for a frame with none
// (one its page spells out or writes, a blob), the site it runs as. Null for one that runs as no site, as
// a data address does: there is nothing to ask its user about.
const SITE = /^https?:/;
const siteOf = ({ href, origin }: Place): string | null => (SITE.test(href) ? href : SITE.test(origin) ? origin : null);

// The two steps a file input is given its files in, each in that same world of the page.
// First the files are made there: the input has none of them yet, and the page sees nothing.
const make = (_input: Node, sent: UploadFile[]): DataTransfer => {
  const made = new DataTransfer();
  for (const { name, mimeType, buffer } of sent) {
    const held = atob(buffer);
    const bytes = new Uint8Array(held.length);
    for (let at = 0; at < held.length; at += 1) bytes[at] = held.charCodeAt(at);
    made.items.add(new File([bytes], name, { type: mimeType }));
  }
  return made;
};
// Then they are put into the input, in one step of the page's, heard there as a person's choice of them is
// (input, then change): "given", or why not. "late": the step ran after *by*, on this computer's clock,
// as when the page was busy: it gives nothing then, since its user may hold the browser by now. "gone":
// it is no file input of its frame's own document now. "moved": its frame is not at the address, or does
// not run as the site, that *at* says, which is what its user was asked about. Looked at and given in the
// one step, so that nothing the page does comes between.
const put = (
  made: DataTransfer, { input, by, at }: { input: Node; by: number; at: { href: string; origin: string } | null },
): "given" | "gone" | "moved" | "single" | "folder" | "late" => {
  if (Date.now() > by) return "late";
  if (!(input instanceof HTMLInputElement) || input.type !== "file" || !input.isConnected || input.ownerDocument !== document) return "gone";
  if (at !== null && (location.href !== at.href || self.origin !== at.origin)) return "moved";
  if (input.webkitdirectory) return "folder";
  if (made.files.length > 1 && !input.multiple) return "single";
  input.files = made.files;
  input.dispatchEvent(new Event("input", { bubbles: true, composed: true }));
  input.dispatchEvent(new Event("change", { bubbles: true }));
  return "given";
};

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

// Where a host keeps the downloads it stages: a folder of its own in its temporary folder, named by its
// process. Playwright removes each file when its browser closes; a host that is killed leaves them.
const STAGING = "surogate-downloads-";

// Whether a process of that number runs: one this user may not ask after counts as running.
function runs(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    return (error as NodeJS.ErrnoException).code !== "ESRCH";
  }
}

/**
 * What hosts that are gone left staged in *temp*, removed: each staging folder names its host's process,
 * and one whose process runs is never touched, so a host cannot clear what a running host has staged.
 */
export async function clearStaged(temp: string): Promise<void> {
  for (const name of await readdir(temp).catch(() => [] as string[])) {
    const pid = /^surogate-downloads-(\d+)-/.exec(name)?.[1];
    if (pid !== undefined && !runs(Number(pid))) await rm(join(temp, name), { recursive: true, force: true }).catch(() => {});
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
    // A download is kept in this host's own staging folder, in its temporary folder, until it is saved under the chat's folder.
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
  now?: () => number; // the clock, in milliseconds; the process's own steady one unless told
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
  // Each session's page this host took, and what hears it ask for a file. While a page is heard the browser
  // opens no file chooser of its own there. Null while its user holds the browser: a file input is theirs then.
  private readonly hearing = new Map<Page, { session: string; heard: ((chooser: FileChooser) => void) | null }>();
  // What takes them off, a moment after a take-over.
  private ownChooser: NodeJS.Timeout | undefined;
  // The file input each session's pages asked for a file for last, until it is given one.
  private readonly choosers = new Map<string, FileChooser>();
  // What an upload's prompt named for each session: the input, with the address its frame was at and the
  // site it ran as then; that upload's files go to it, there, and to no input that asks after. Or no
  // input, and *why* that upload is given to none: nothing had asked, or what had runs as no site.
  // *of*: the upload it was named for, by its operation's id, which alone is given it.
  private readonly named = new Map<string, { input: { chooser: FileChooser; href: string; origin: string } | null; why: string; of: string | undefined }>();
  // ponytail: the uploads whose user was asked about them, by their operations' ids, each until it comes:
  // one that was denied never does, and stays for the host's life. One of these whose name is the
  // session's no more is given to nothing, where one nobody was asked about goes to what asked last.
  private readonly prompted = new Set<string>();
  // Where this host stages downloads, once it has launched a browser: its own folder, until it closes.
  private staging: string | null = null;
  // The agent's downloads on their way, until each is handed on or dropped: a take-over stops them all.
  private readonly arriving = new Set<Download>();
  // The chat that last handed the browser back, and when.
  private handed: { by: string; at: number } | null = null;
  // When each navigation's request began, for as long as the browser keeps the request: a redirect's next
  // request takes its beginning from the one it follows.
  private readonly begun = new WeakMap<Request, Begun>();
  // The navigations not known to have ended as a page, oldest first: a download may be the end of one.
  private readonly open = new Map<Request, Begun>();
  private closing = false;

  constructor(private readonly options: BrowserHostOptions = {}) {}

  /**
   * One operation of *session*'s, of the chat *root*, in its tab, launching the browser first if none runs.
   * *id*: the operation's own, by which an upload is known as the one its user was asked about. Never rejects.
   */
  perform(launch: Launch, root: string, session: string, kind: string, args: Record<string, unknown>, signal: AbortSignal, id?: string): Promise<Outcome> {
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
        return this.held !== null ? Promise.resolve(PAUSED) : this.run(launch, session, kind, args, signal, this.interrupt.signal, id);
      });
    return Promise.race([work, new Promise<Outcome>((resolve) => {
      if (signal.aborted) resolve(CANCELLED);
      signal.addEventListener("abort", () => resolve(CANCELLED), { once: true });
    })]);
  }

  /**
   * The address of the page *session*'s next operation acts in, once those before it in its line
   * have run: its newest open page's, a popup's or its tab's, or a new tab's. For an *upload*: of
   * the frame of the file input its pages asked for last, which is the site that gets the files,
   * whatever page frames it, as the browser says it and not the page (place); that input is then held
   * for the upload, so one that asks after cannot take the files in its place, and the upload gives
   * nothing if the input is elsewhere by then. *of*: that upload, by its operation's id; the input is
   * kept for it alone. Never rejects.
   */
  address(session: string, upload = false, of?: string): Promise<string> {
    return this.inLine(session, async () => {
      const tab = (this.tabs.get(session) ?? []).filter((page) => !page.isClosed()).at(-1)?.url() ?? NEW_TAB;
      if (!upload) return tab;
      // Asked about from here on, whatever is named for it.
      if (of !== undefined) this.prompted.add(of);
      const chooser = this.choosers.get(session);
      const at = chooser ? await this.placed(chooser) : null;
      // Its user holds the browser, or took it over meanwhile: no input is named, or kept, for any upload.
      if (this.held !== null) return tab;
      if (!chooser || !at?.here) {
        // Nothing has asked: its user is asked by the tab's page, and the upload is given to nothing, though an input asks after.
        this.named.set(session, { input: null, why: NOT_ASKED, of });
        return tab;
      }
      const site = siteOf(at);
      this.named.set(session, site === null ? { input: null, why: NO_SITE, of } : { input: { chooser, href: at.href, origin: at.origin }, why: NOT_AS_ASKED, of });
      return site ?? at.href;
    }).catch(() => NEW_TAB);
  }

  /**
   * The browser taken over by the user of the chat *root* (*paused*), for every chat, or handed back by
   * the chat that holds it. Which chat may take it is its client's to say: the last one told holds it.
   */
  pause(root: string, paused: boolean): void {
    if (!paused) {
      if (this.held !== root) return;
      this.held = null;
      this.handed = { by: root, at: this.now() };
      // The agent drives again: a file its pages ask for is heard, and opens no chooser of the browser's own,
      // also where it was handed back before they were let be.
      clearTimeout(this.ownChooser);
      for (const page of this.hearing.keys()) this.hear(page);
      return;
    }
    this.held = root;
    this.interrupt.abort();
    this.interrupt = new AbortController();
    // What its pages asked for before is not the agent's to answer once its user has held the browser: no
    // input is kept for an upload, named for a prompt or not, and none is taken up again at the hand back.
    this.choosers.clear();
    this.named.clear();
    // The agent's downloads on their way stop where they are: each is dropped once it has ended.
    for (const download of this.arriving) void download.cancel().catch(() => {});
    // A button the agent pressed and holds, in any session's page, comes up: not left down under its user's hand.
    for (const pages of this.tabs.values()) for (const page of pages) void letGo(page);
    // A navigation of the agent's that this take-over itself stopped answers nothing: its request is forgotten.
    // A download of that address their user then makes, of which the browser says no request, is not its answer.
    for (const page of [...this.tabs.values()].flat().filter((page) => stoppedIn(page))) {
      for (const request of this.open.keys()) {
        if (pageOf(request) === page) this.open.delete(request);
      }
    }
    // A file input is its user's while they hold the browser, opening the browser's own chooser: but only once
    // what the agent was doing at this moment has reached its page, so that no act of the agent's opens one.
    // Until then a page's ask is still heard, and kept for no one (asks).
    clearTimeout(this.ownChooser);
    this.ownChooser = setTimeout(() => {
      for (const page of this.hearing.keys()) this.unhear(page);
    }, OWN_CHOOSER_MS);
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
    // What it staged and nobody saved went with its browser; its folder goes now.
    if (this.staging !== null) await rm(this.staging, { recursive: true, force: true }).catch(() => {});
    this.staging = null;
    clearTimeout(this.ownChooser);
  }

  // An operation at its turn. Taken over while it acts, it is answered paused at once, whatever it has
  // done: nothing it reads from the page after that is the agent's, and its session's line goes on.
  private run(
    launch: Launch, session: string, kind: string, args: Record<string, unknown>, signal: AbortSignal, stop: AbortSignal, id: string | undefined,
  ): Promise<Outcome> {
    return until(this.act(launch, session, kind, args, signal, stop, id), stop, PAUSED);
  }

  private async act(
    launch: Launch, session: string, kind: string, args: Record<string, unknown>, signal: AbortSignal, stop: AbortSignal, id: string | undefined,
  ): Promise<Outcome> {
    if (signal.aborted) return CANCELLED;
    if (kind === "browser.set_input_files") return this.upload(session, args, stop, id);
    // Its agent acted since an upload's prompt named an input: that prompt's upload is not coming, or, allowed
    // and still having its files read, comes to nothing (upload).
    if (!looks(kind, args)) this.named.delete(session);
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
  // its agent is told with its next answer; a download in it is its chat's (arrived). No service
  // worker answers it (bypassWorkers).
  private adopt(session: string, page: Page): void {
    this.tabs.get(session)?.push(page);
    // Gone from the session's pages once it closes: a long session opens many popups.
    page.once("close", () => {
      const pages = this.tabs.get(session);
      if (pages?.includes(page)) pages.splice(pages.indexOf(page), 1);
      this.asking.delete(page);
      this.hearing.delete(page);
    });
    page.on("popup", (popup) => this.adopt(session, popup));
    this.hearing.set(page, { session, heard: null });
    // One that opens under its user's hand is heard from the hand back.
    if (this.held === null) this.hear(page);
  }

  // A session's *page* is heard when it asks for a file. With a listener, the browser opens no file dialog
  // of its own: no path the agent did not get reaches a page.
  private hear(page: Page): void {
    const kept = this.hearing.get(page);
    if (!kept || kept.heard !== null) return;
    const { session } = kept;
    kept.heard = (chooser: FileChooser) => this.asks(session, chooser);
    page.on("filechooser", kept.heard);
  }

  // *page* is heard no more: a file input in it opens the browser's own chooser, as in any browser.
  private unhear(page: Page): void {
    const kept = this.hearing.get(page);
    if (!kept?.heard) return;
    page.off("filechooser", kept.heard);
    kept.heard = null;
  }

  // Where *chooser*'s input is now, as the browser says it (place). Null where its page is closed, went
  // elsewhere since it asked, or does not say within LOOK_MS.
  private async placed(chooser: FileChooser): Promise<Place | null> {
    let timer: NodeJS.Timeout | undefined;
    const late = new Promise<null>((resolve) => {
      timer = setTimeout(() => resolve(null), LOOK_MS);
    });
    try {
      return await Promise.race([chooser.element().evaluate(place).catch(() => null), late]);
    } finally {
      clearTimeout(timer);
    }
  }

  // A page of *session*'s asked for a file: the input is kept for an upload, and its agent told. Not while
  // its user holds the browser: what a page asks for then is their own doing, or the page's under their
  // hand, and no input of theirs is the agent's to fill, at the hand back either.
  private asks(session: string, chooser: FileChooser): void {
    if (this.held !== null) return;
    this.choosers.set(session, chooser);
    this.note(session, FILE_ASKED);
  }

  // The session whose page *page* is now, a popup of its too: none for a tab its user opened themselves,
  // for the page a new browser opens with until a session takes it, and for one its session has closed.
  private sessionOf(page: Page): string | undefined {
    for (const [session, pages] of this.tabs) {
      if (pages.includes(page)) return session;
    }
    return undefined;
  }

  // The time the minute after a hand back and a failed request's second are counted by: a clock that cannot be
  // set, not the computer's, which its user, or its network, puts on and back. It stands still while the
  // computer sleeps (on Linux), so a minute begun before a sleep lasts that much longer: the safe side.
  private now(): number {
    return this.options.now?.() ?? performance.now();
  }

  // A request the browser says has begun, in any page of its. A navigation's is kept with who held the
  // browser at that moment: a download that is a navigation's answer (a link, a form, a new window, a
  // frame) is whose its request was. No request is said of a link with `download`, of a blob or of a data
  // address, and what a page loads beside its own document is no download's beginning.
  private requested(request: Request): void {
    if (!request.isNavigationRequest()) return;
    const from = request.redirectedFrom();
    const begun = (from && this.begun.get(from)) ?? { by: this.held, stop: this.interrupt.signal };
    this.begun.set(request, begun);
    if (from) this.open.delete(from);
    this.open.set(request, begun);
    this.swept();
  }

  // What is kept of requests, looked over at each request the browser says and at each download it announces:
  // one it gave up as a page more than AFTER_FAILURE_MS ago counts for nothing and goes, and of more than
  // REQUESTS the oldest go.
  private swept(): void {
    const now = this.now();
    for (const [request, { failed }] of this.open) {
      if (this.open.size > REQUESTS || (failed !== undefined && now - failed > AFTER_FAILURE_MS)) this.open.delete(request);
    }
  }

  // It loaded as a page: no download.
  private loaded(request: Request): void {
    this.open.delete(request);
  }

  // The browser gave it up as a page, as it does a moment before it announces it as a download: from then
  // it counts for a download for AFTER_FAILURE_MS, and no longer.
  private failed(request: Request): void {
    const begun = this.open.get(request);
    if (begun) this.open.set(request, { ...begun, failed: this.now() });
  }

  // Whose a download is: its user's, with the chat the browser was held from; or the agent's, with what a
  // take-over since its beginning stops it by. It is the agent's only where a request of it is known to
  // have begun while nobody held the browser, or where no request of it is known, nobody holds the browser
  // now, and it was last handed back more than AFTER_HAND_BACK_MS ago. Every other is its user's: one whose
  // request began while they held it, however late it comes; one with no request known that comes while
  // they hold it, or in the time after in which it may have been asked for under their hand; and, in doubt
  // between two requests of one address, either. *after*: theirs for that last reason alone, the time
  // after a hand back. Such a one may be the agent's own, and the agent is told what came of it; of one
  // that is theirs outright, held now or by its request, never, whatever the clock says.
  private whose(download: Download): { by: string; after?: true } | { stop: AbortSignal } {
    this.swept();
    const address = bare(download.url());
    const known = [...this.open].filter(([request]) => bare(request.url()) === address);
    if (known.length > 0) {
      const [request, begun] = known.find(([, { by }]) => by !== null) ?? known[0]!;
      this.open.delete(request);
      return begun.by !== null ? { by: begun.by } : { stop: begun.stop };
    }
    if (this.held !== null) return { by: this.held };
    if (this.handed !== null && this.now() - this.handed.at <= AFTER_HAND_BACK_MS) return { by: this.handed.by, after: true };
    return { stop: this.interrupt.signal };
  }

  // A download the browser announces in *page*, any page of its. Its user's is saved in the chat its tab
  // is of, asked there in either mode; in a tab no chat owns, in the chat the browser was held from when
  // it began, unless that chat is deleted. The agent's is its session's chat's. One that is the agent's
  // in a tab no chat owns is no chat's to ask: it is stopped at once and what it left removed, as before
  // downloads were kept.
  private arrived(page: Page, download: Download): Promise<void> {
    const session = this.sessionOf(page);
    const whose = this.whose(download);
    if (session !== undefined) {
      return "stop" in whose
        ? this.stage(download, { root: this.roots.get(session), session }, whose.stop)
        : this.stage(download, { root: this.roots.get(session), session, after: whose.after === true }, null);
    }
    // In a tab no chat owns no agent acts: one there is its user's outright, in the time after a hand back too.
    if ("stop" in whose || this.forgets.has(whose.by)) return this.discard(download);
    return this.stage(download, { root: whose.by, session: whose.by }, null);
  }

  // Stopped where it is, and what it had written removed: one that had ended already too. Never rejects.
  private async discard(download: Download): Promise<void> {
    await download.cancel().catch(() => {});
    await download.delete().catch(() => {});
  }

  // The files the main side read from the chat's folder, given once to the file input an upload's
  // prompt named, or, unasked, to the one the session's pages asked for last: the page is given names
  // and what they hold, never a path. Taken over while it is on its way, it gives the page nothing (give).
  // *id*: its operation's. One its user was asked about is given only to what its own prompt named: where
  // that is the session's no more, since its agent acted before its files came or another upload was
  // asked about meanwhile, it is given to nothing, and never to whatever asked last.
  private async upload(session: string, args: Record<string, unknown>, stop: AbortSignal, id: string | undefined): Promise<Outcome> {
    const kept = this.named.get(session);
    const named = kept !== undefined && kept.of === id ? kept : undefined;
    if (named) this.named.delete(session);
    const asked = id !== undefined && this.prompted.delete(id);
    if (asked && !named) return failed(NOT_AS_ASKED);
    if (named && named.input === null) return failed(named.why);
    const chooser = named?.input?.chooser ?? this.choosers.get(session);
    // Its page closed, or is this session's no more.
    if (!chooser || this.sessionOf(chooser.page()) !== session) return failed(NOT_ASKED);
    const files = filesOf(args.files);
    if (!files) return failed("A file for the page is a name, its type and what it holds");
    let refused: unknown;
    try {
      refused = await this.bounded(chooser.page(), this.give(session, chooser, files, named?.input ?? null, stop), stop);
    } catch (error) {
      if (stop.aborted) return PAUSED;
      return failed(said(error));
    }
    if (stop.aborted || refused === HELD) return PAUSED;
    if (typeof refused === "string") return failed(refused);
    if (this.choosers.get(session) === chooser) this.choosers.delete(session);
    const notices = this.unseen.get(session) ?? [];
    this.unseen.delete(session);
    return { ok: { files: files.length, notices } };
  }

  // *files* given to *chooser*'s input: null once it has them, why not in words, or HELD. They are made
  // ready in the page where no script of its reaches them, which takes as long as they are large (make),
  // and then put into the input in one short step (put). Between the two is the last look at whether its
  // user has taken the browser over (*stop*): taken over before it, the page is given nothing, what was
  // made ready is dropped, and nothing of it is taken up again. The step sent after that look gives the
  // files only if the page takes it within GIVE_MS: a page too busy for that is given nothing by it, and
  // is looked at again first. So a page gets a file at most GIVE_MS after its user took the browser over,
  // and only from a step sent before they did; its session's next answer then says so, since this
  // operation's own is paused. *at*: where the upload's prompt said the input is; null for an upload
  // nobody was asked about.
  private async give(
    session: string, chooser: FileChooser, files: UploadFile[], at: { href: string; origin: string } | null, stop: AbortSignal,
  ): Promise<string | null | typeof HELD> {
    const input = chooser.element();
    const made = await input.evaluateHandle(make, files);
    try {
      for (let tries = 0; tries < GIVE_TRIES; tries += 1) {
        if (stop.aborted) return HELD;
        const came = await made.evaluate(put, { input, by: Date.now() + GIVE_MS, at: at && { href: at.href, origin: at.origin } });
        // The page was too busy to take them in time: looked at again, and sent again.
        if (came === "late") continue;
        if (came === "single") return ONE_FILE;
        if (came === "folder") return A_FOLDER;
        if (came !== "given") return at ? NOT_AS_ASKED : NOT_ASKED;
        if (stop.aborted) this.keep(session, GIVEN_AS_TAKEN);
        return null;
      }
      return BUSY;
    } finally {
      void made.dispose().catch(() => {});
    }
  }

  // A download once it has finished, in this host's staging folder: handed on for the chat's folder,
  // or, when it did not finish, cannot be measured or is too large to save, gone, and its agent told why.
  // *of*: the chat it is saved in, and the session it is told to. *stop*: null for its user's own, which
  // nothing of the agent's stops. Of one that is theirs outright the agent is told nothing. Of one that
  // is theirs only by the time after a hand back (*of.after*), which may be the agent's own, the agent is
  // told that it was not saved where it is not handed on, with no name and no reason; handed on, what
  // saves it says what came of it. The agent's is interrupted as the operation that started it is, by the
  // signal it began under: taken over before it is handed on, or before it was announced, it is stopped
  // where it is and dropped, never taken up again at a hand back, and its agent is told so with its
  // session's next answer. One handed on before the take-over is the chat's to save, as a write of the
  // chat's that waits or asks goes on.
  private async stage(
    download: Download, of: { root: string | undefined; session: string; after?: boolean }, stop: AbortSignal | null,
  ): Promise<void> {
    const name = download.suggestedFilename();
    const { root, session } = of;
    const user = stop === null;
    if (stop) this.arriving.add(download);
    // Taken over between its request and its announcement: stopped as one on its way is.
    if (stop?.aborted) void download.cancel().catch(() => {});
    // The agent's own is told whatever came of it, held meanwhile or not: it began before any take-over.
    const tell = (notice: string) => {
      if (!user) this.keep(session, notice);
      // Theirs only by the time after a hand back: not why, and not its name.
      else if (of.after === true) this.keep(session, LEFT_TO_USER);
    };
    try {
      let path: string;
      try {
        path = await download.path();
      } catch {
        tell(stop?.aborted ? interrupted(name) : notFinished(name, unfinished(await download.failure().catch(() => null))));
        return;
      }
      const size = await stat(path).then((found) => found.size, () => null);
      const most = this.options.downloadBytes ?? MAX_WRITE_BYTES;
      if (stop?.aborted || root === undefined || !this.options.downloaded || size === null || size > most) {
        if (root !== undefined && this.options.downloaded) tell(stop?.aborted ? interrupted(name) : size === null ? unmeasured(name) : tooLarge(name, size, most));
        await download.delete().catch(() => {});
        return;
      }
      this.options.downloaded({ root, session, name, path, user, ...(user && of.after === true ? { afterHandBack: true as const } : {}) });
    } finally {
      this.arriving.delete(download);
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
    this.choosers.delete(session);
    this.named.delete(session);
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

  // *context*'s browser has closed: none of its requests is answered now, and the next browser starts with none.
  private closed(context: BrowserContext): void {
    this.open.clear();
    this.retire(context);
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
    this.hearing.clear();
    this.choosers.clear();
    this.named.clear();
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
    // Its first launch: what a host that was killed left staged here goes, and this host's own folder is made.
    let staging = this.staging;
    if (staging === null) {
      await clearStaged(tmpdir());
      staging = await mkdtemp(join(tmpdir(), `${STAGING}${process.pid}-`));
      this.staging = staging;
    }
    const context = await chromium.launchPersistentContext(launch.profile, {
      ...launchOptions(launch.executable, this.proxy.port, this.options.args), downloadsPath: staging,
    });
    context.on("close", () => this.closed(context));
    context.on("dialog", (dialog) => this.asked(dialog));
    // A download is heard in every page of the browser's, whoever opened it: the page it opens with, a
    // session's tab, a popup, and a tab its user opens themselves.
    const watch = (page: Page) => void page.on("download", (download) => void this.arrived(page, download));
    context.on("page", watch);
    context.pages().forEach(watch);
    // And every request, on the context: a new window's first is said before the window is a page.
    context.on("request", (request) => this.requested(request));
    context.on("requestfinished", (request) => this.loaded(request));
    context.on("requestfailed", (request) => this.failed(request));
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
