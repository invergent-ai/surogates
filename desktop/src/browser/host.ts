// The browser host (spec, Sections 1 and 5): one Chromium-based browser installed on this
// computer, launched for an agent identity with a profile of its own, over a pipe, with its
// own sandbox on and every request through the pinning proxy. Each calling session gets a
// tab, and the popups that tab opens are its too. The host runs in a process of its own
// (main.ts); the browser dies with it, as its pipe closes.

import { randomBytes } from "node:crypto";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { mkdtemp, readdir, readFile, rm, stat } from "node:fs/promises";
import { tmpdir } from "node:os";
import { basename, dirname, join } from "node:path";

import { type BrowserContext, type CDPSession, chromium, type Dialog, type Download, type FileChooser, type Frame, type Page, type Request } from "playwright-core";

import { MAX_WRITE_BYTES } from "../files/answers.js";
import type { Outcome } from "../link/protocol.js";
import { destination, reach } from "../vm/egress.js";
import { CANCELLED, NEW_TAB, PAUSED } from "./client.js";
import { interrupted, LEFT_TO_USER, quoted, type StagedDownload, tooLarge, tooMuch } from "./downloads.js";
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
// How long a page of the agent's must have been quiet, once its user holds the browser, before a file
// input in it opens the browser's own chooser. It is how long a page keeps the leave to ask for a file by
// itself that a click gives it (five seconds in Chromium), and three things give a page that leave when
// they reach it, which a busy page lets happen late: what the agent does in it; what this host does in it
// for an upload, the look at where its file input is and each step that gives the files (doing counts
// both, until they have reached the page); and Playwright's own reading of an input the page asked with,
// which is how each ask is heard (asks). So the five seconds are counted from the take-over, from when
// the last of the first two reached the page, and from each ask heard, whichever is last; and when they
// have passed the page is let be only once it has answered that nothing of the third is still on its way
// (quiet). What this host reads of a page by itself gives it no leave (read). So a page let be has none
// left that the agent, or this host, gave it.
export const OWN_CHOOSER_MS = 5_000;
// How long a page may take to say where a file input of its is, for an upload's prompt: less than the
// prompt waits for this host's answer (the approvals' ADDRESS_MS), which then gives the upload up. Its
// session's line is held no longer for it.
export const LOOK_MS = 800;
// How long an upload's question of this host waits its turn in its session's line before it is answered
// that an earlier operation of the session's still runs (EARLIER_RUNNING): with the look at the page after
// it, less than the prompt waits. The upload's prompt names the site that gets its files, so it cannot be
// made without this answer, and the chat's other prompts wait behind it: it is not kept waiting for as long
// as that operation takes, and is told why it has no site to name.
export const TURN_MS = 100;
// How long a page may take to answer once the browser is handed back (settle), before the agent acts in it
// all the same and what it asks for is kept again: a frame of it that is stuck would else keep the agent
// out of the page for good.
export const SETTLE_MS = 10_000;
// How many commands Playwright sends a page, one after the other and each only when the one before has
// answered, between the browser saying that a file input asked (Page.fileChooserOpened) and Playwright
// saying so (its filechooser event); and the Playwright they were counted on. Counted by reading
// playwright-core's lib/coreBundle.js. CRPage._onFileChooserOpened finds the input in the frame's own
// world of Playwright's (_adoptBackendNodeId sends DOM.resolveNode: one). Page._onFileChooserOpened then
// reads it (handle.evaluate, by ExecutionContext._evaluateWithArguments), which first makes Playwright's
// helper in that world where it has made none yet (_utilityScript sends Runtime.evaluate: two; made once
// in each world of each document, so only a frame Playwright has evaluated nothing in takes this step),
// and then reads the input (evaluateWithArguments sends Runtime.callFunctionOn with userGesture: three).
// It reads it twice, each as a gesture that gives the page leave, on two chains: whether the input takes
// several files, after which Playwright says the page asked; and the handle's own description, after
// which it says nothing. Both wait for the helper, so three commands one after the other at most, and the
// second reading can reach a busy page after the first has been heard of (quiet counts from the page's
// answer for that). And by measuring: for a frame that takes all three, with two
// answers the browser's own chooser opened in 7 runs of 9, with three in none of 4. A test fails where
// the Playwright installed is another than this: the steps are to be counted again then, the two headed
// tests of a page and of a frame "that asked for a file on the agent's click and was busy from then on"
// run, and both written here.
export const PLAYWRIGHT_MEASURED = "1.63.0";
export const PLAYWRIGHT_READ_STEPS = 3;
// How many times over a page is asked to answer before what it had asked for counts as heard (heardOut):
// once for each of those steps, and once more, to spare.
export const READS = PLAYWRIGHT_READ_STEPS + 1;
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
// How many of the browser's words that a download begins are kept while no download is matched to them.
const UNMATCHED = 64;
/**
 * How long the browser's word that a download begins is kept for the download it is of. Playwright announces a
 * download from that same word of the browser's, so the two come together: one said this long before is of a
 * download nobody announced here, and would name a later one of its address and name by a file that is not its own.
 */
export const SAID_MS = 10_000;
// How many answers this host remembers the notices of, for one whose cancel crossed it on the way (unanswered).
const CARRIED = 32;
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
// How long a download of the agent's may go with no byte of its own coming, before it is dropped as one that
// did not finish: a site can begin a download and never end it, and it would stay on its way, its part
// kept, for as long as the browser runs. One whose site is silent that long part-way is dropped the same,
// though it would have gone on. Its user's own are never dropped so.
export const STALLED_MS = 60_000;
// The most the agent's own downloads may have staged together: those on their way, each by its own bytes, and
// those that ended and wait to be saved. Past it, every download of the agent's still on its way is stopped
// and dropped, and one that ends is not handed on; its agent is told of each that it was too large to save
// (tooMuch). A file is saved only up to what a write may carry, and is measured for that only once it has
// ended: one that never ends and keeps coming would fill the disk. Eight files as large as one that can be
// saved: a browser fetches six at once from one site, and what waits to be saved is each a write's most or
// less. Its user's own downloads never count toward it, and are never stopped by it.
export const STAGED_MOST_BYTES = 8 * MAX_WRITE_BYTES;
// How often what is staged is looked at for both, at most: what a download that keeps coming adds between
// two looks is what the folder can hold past its most.
const STAGED_LOOK_MS = 1_000;
const stalledFor = (ms: number): string => `no more of it came for ${ms / 1_000} s`;
const unfinished = (failure: string | null): string => (failure === "canceled" ? failure : "the browser stopped it");
const unmeasured = (name: string): string => `The page downloaded ${quoted(name)}, but its size could not be measured, so it was not saved.`;

const failed = (message: string): Outcome => ({ error: { type: "browser", message } });
const DELETED = failed("The chat was deleted, and its tabs closed with it");
const ANOTHER_CHATS = failed("This session's tab in the agent's browser on this computer is another chat's");
// True of an operation that still runs, and of one that was answered already and still acts in its page: one
// the server cancelled is answered at once, and goes on in the page until it ends there.
export const EARLIER_RUNNING =
  "The agent's browser on this computer was still busy with what this session did before, which has not ended in its page yet, though it may have been answered or cancelled. Nobody was asked about this upload, and the page was given nothing. Send it again in a moment.";
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
// that frame is, and as what site it runs; and what it takes, several files or a folder. Said in the page,
// in the isolated world the input's handle lives in (Playwright's own, one for each frame), which no
// script of the page's reaches: so it is the browser's word, not the page's.
interface Place {
  here: boolean;
  href: string;
  origin: string;
  multiple: boolean;
  folder: boolean;
}
const place = (input: Node): Place => {
  const file = input instanceof HTMLInputElement && input.type === "file";
  return {
    here: file && input.isConnected && input.ownerDocument === document,
    href: location.href,
    origin: self.origin,
    multiple: file && input.multiple,
    folder: file && input.webkitdirectory,
  };
};
// Why an input that is at *now* takes none of *count* files, where *at* is what the upload's prompt said of
// it (null for an upload nobody was asked about); null where it would take them.
type Unfit = "gone" | "moved" | "single" | "folder";
const unfitFor = (now: Place, count: number, at: { href: string; origin: string } | null): Unfit | null => {
  if (!now.here) return "gone";
  if (at !== null && (now.href !== at.href || now.origin !== at.origin)) return "moved";
  if (now.folder) return "folder";
  return count > 1 && !now.multiple ? "single" : null;
};
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
  stalledMs?: number; // STALLED_MS unless told
  stagedBytes?: number; // STAGED_MOST_BYTES unless told
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
  // opens no file chooser of its own there. Null once its user holds the browser and the page has been quiet
  // for OWN_CHOOSER_MS: a file input is theirs then. *acting*: how many operations of the agent's are still
  // doing something in it. *quiet*: what lets it be, while it is held and heard.
  // What the last answers carried of what their sessions' pages did, by their operations' ids (took).
  private readonly carried = new Map<string, { session: string; notices: string[] }>();
  // The uploads whose prompts have asked this host where their input is and have no answer yet, by their
  // operations' ids: whether each is still coming.
  private readonly sought = new Map<string, { coming: boolean }>();
  // The inputs an upload is giving files to now: held until it has.
  private readonly giving = new Set<FileChooser>();
  // Those whose handle this host has let go.
  private readonly released = new WeakSet<FileChooser>();
  // Each page this host took, and how it is heard when it asks for a file. *heard*: Playwright's listener, while
  // the agent drives. *lines*: lines of this host's own to the page and to each frame a process of its own
  // draws, while its user holds the browser (overhear); *over*: those being made. *turn*: the page's quiet, as
  // it is counted now, and what ends the waits it began (quiet).
  private readonly hearing = new Map<Page, {
    session: string; heard: ((chooser: FileChooser) => void) | null; lines?: CDPSession[];
    over?: { lines: CDPSession[]; more: Array<Promise<unknown>> };
    acting: number; quiet?: NodeJS.Timeout; turn?: { end(): void };
  }>();
  // Of a page's own lines, the one to each of its frames, as it will be once made: none where the frame's page's
  // process draws it (framed).
  private readonly frameLines = new WeakMap<CDPSession[], Map<Frame, Promise<CDPSession | null>>>();
  // The pages whose tab crashed, and has not been loaded again since.
  private readonly crashed = new WeakSet<Page>();
  // The pages that have not answered yet since the browser was last handed back (settle): what one of them
  // asks for is kept for no one, and no operation of the agent's acts in it, until it has.
  private readonly settling = new Map<Page, Promise<void>>();
  // The file input each session's pages asked for a file for last, until it is given one.
  private readonly choosers = new Map<string, FileChooser>();
  // What an upload's prompt named for each session: the input, with the address its frame was at and the
  // site it ran as then; that upload's files go to it, there, and to no input that asks after. Or no
  // input, and *why* that upload is given to none: nothing had asked, or what had runs as no site.
  // *of*: the upload it was named for, by its operation's id, which alone is given it.
  private readonly named = new Map<string, { input: { chooser: FileChooser; href: string; origin: string } | null; why: string; of: string | undefined }>();
  // The uploads whose user was asked about them, by their operations' ids, each until it comes, or until
  // this host is told it is not coming (notComing). One of these whose name is the session's no more is given
  // to nothing, where one nobody was asked about goes to what asked last.
  private readonly prompted = new Set<string>();
  // Where this host stages downloads, once it has launched a browser: its own folder, until it closes.
  private staging: string | null = null;
  // The agent's downloads on their way, until each is handed on or dropped: a take-over stops them all.
  private readonly arriving = new Set<Download>();
  // The look at what is staged, while a download of the agent's is on its way (watch); for each of those, its
  // own bytes at the last look and when they last grew; and the downloads dropped for none having come.
  private watching: NodeJS.Timeout | null = null;
  private readonly grew = new Map<Download, { bytes: number; at: number }>();
  private readonly stalled = new WeakSet<Download>();
  // The id the browser names each download's staged file by, for every download on its way, the agent's and
  // its user's: said by the browser on a line of this host's own as the download begins (begins), and matched
  // to the download Playwright announces by its address and its name, in the order both come. *said*: ids
  // no download is matched to yet; *unnamed*: downloads the browser has said no id of yet.
  private readonly ids = new Map<Download, string>();
  private readonly said: Array<{ id: string; url: string; name: string; at: number }> = [];
  private readonly unnamed: Download[] = [];
  // The agent's files that ended and were handed on to be saved, by where each is staged: counted with the
  // agent's own until it is gone from there.
  private readonly waiting = new Set<string>();
  // The downloads stopped because more was staged than may be.
  private readonly overfull = new WeakSet<Download>();
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
    // What a take-over from here on stops an upload by. It comes with files read before it came, and gives none
    // of them once its user has held the browser since, though it waited its turn through the hand back.
    const sent = this.interrupt.signal;
    // A close does not wait in the session's line: a page stuck in a script closes with the rest.
    const work = kind === "browser.close"
      ? (this.held !== null ? Promise.resolve(PAUSED) : this.closeTab(session).then((closed): Outcome => ({ ok: { closed } })))
      : this.inLine(session, () => {
        if (this.forgets.get(root) !== forgets) return Promise.resolve(DELETED);
        // Its user took the browser over while it waited: it does nothing there.
        if (this.held !== null || (kind === "browser.set_input_files" && sent.aborted)) return Promise.resolve(PAUSED);
        return this.run(launch, session, kind, args, signal, this.interrupt.signal, id);
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
   * kept for it alone. Where the input is in a frame that runs as no site, there is none to ask its user
   * about, and the upload is given to nothing: answered so, in place of an address; and so where an earlier
   * operation of the session's still runs, behind which this would wait longer than its prompt does. *root*: the chat that
   * asks. A session's page is said, and its input named, only for the chat the session is of: any other
   * is told a new tab's, as its operation there would be refused (perform). Never rejects.
   */
  address(session: string, upload = false, of?: string, root?: string): Promise<string | { refused: string }> {
    // This waits its turn in the session's line, and for the page: its prompt, which waits less long, can
    // have given the upload up before it answers (notComing). Nothing is kept then for an upload that is
    // not coming: it is not known as asked about, and no input is named for it.
    const sought = { coming: true };
    if (upload && of !== undefined) this.sought.set(of, sought);
    let turn = false;
    const said = this.inLine(session, async () => {
      turn = true;
      const open = (this.tabs.get(session) ?? []).filter((page) => !page.isClosed());
      if (root !== undefined && open.length > 0 && this.roots.get(session) !== root) return NEW_TAB;
      const tab = open.at(-1)?.url() ?? NEW_TAB;
      if (!upload || !sought.coming) return tab;
      // Asked about from here on, whatever is named for it.
      if (of !== undefined) this.prompted.add(of);
      const chooser = this.choosers.get(session);
      // What a take-over from here on stops: the page can be slow to say where its input is.
      const taken = this.interrupt.signal;
      const at = chooser ? await this.placed(chooser) : null;
      // Its user holds the browser, or took it over since this began, handed back or not: no input is named,
      // or kept, for any upload. The one that had asked did so before they held it.
      if (this.held !== null || taken.aborted) return tab;
      if (!sought.coming) return tab;
      // The input named for the prompt before this one, if any, is named no more.
      const before = this.named.get(session)?.input?.chooser;
      if (!chooser || !at?.here) {
        // Nothing has asked: its user is asked by the tab's page, and the upload is given to nothing, though an input asks after.
        this.named.set(session, { input: null, why: NOT_ASKED, of });
        this.release(before);
        return tab;
      }
      const site = siteOf(at);
      this.named.set(session, site === null ? { input: null, why: NO_SITE, of } : { input: { chooser, href: at.href, origin: at.origin }, why: NOT_AS_ASKED, of });
      this.release(before);
      return site ?? { refused: NO_SITE };
    }).catch(() => NEW_TAB).finally(() => {
      if (of !== undefined && this.sought.get(of) === sought) this.sought.delete(of);
    });
    if (!upload) return said;
    // An upload's question whose turn has not come within TURN_MS is behind an operation of its session's
    // that still runs: answered so, at once, and nothing is kept for the upload when its turn does come.
    let timer: NodeJS.Timeout | undefined;
    const busy = new Promise<{ refused: string }>((resolve) => {
      timer = setTimeout(() => {
        if (turn) return;
        sought.coming = false;
        resolve({ refused: EARLIER_RUNNING });
      }, TURN_MS);
    });
    return Promise.race([said, busy]).finally(() => clearTimeout(timer));
  }

  /**
   * The upload this host was asked about under the operation *of* is not coming: its prompt was denied, ran
   * out or went, or it ended before it came here. It is known no more, and no input is kept for it.
   */
  notComing(of: string): void {
    const sought = this.sought.get(of);
    if (sought) sought.coming = false;
    this.prompted.delete(of);
    for (const [session, kept] of this.named) {
      if (kept.of !== of) continue;
      this.named.delete(session);
      this.release(kept.input?.chooser);
    }
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
      for (const [page, kept] of this.hearing) {
        clearTimeout(kept.quiet);
        delete kept.quiet;
        kept.turn?.end();
        delete kept.turn;
        // Playwright hears the page again, and this host's own lines hear it until the page has answered: by
        // then Playwright's listener is on in the browser, so nothing a page asks for is heard by neither. What
        // both hear meanwhile is taken from Playwright alone (overheard).
        const { lines } = kept;
        delete kept.lines;
        delete kept.over;
        this.hear(page);
        this.settle(page);
        if (lines) void this.settling.get(page)?.then(() => this.drop(lines));
      }
      return;
    }
    this.held = root;
    this.interrupt.abort();
    this.interrupt = new AbortController();
    // What its pages asked for before is not the agent's to answer once its user has held the browser: no
    // input is kept for an upload, named for a prompt or not, and none is taken up again at the hand back.
    const kept = [...this.choosers.values(), ...[...this.named.values()].map((named) => named.input?.chooser)];
    this.choosers.clear();
    this.named.clear();
    for (const chooser of new Set(kept)) this.release(chooser);
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
    // A file input is its user's while they hold the browser, opening the browser's own chooser: but in each
    // page only once nothing the agent did there can open one (quiet). Until then a page's ask is still
    // heard, and kept for no one (asks). A page in which something of the agent's, or of this host's for an
    // upload, is still on its way waits for that to reach it (doing).
    for (const [page, kept] of this.hearing) {
      this.overhear(page);
      if (kept.acting === 0) this.quiet(page);
    }
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
    this.unwatch();
    if (this.staging !== null) await rm(this.staging, { recursive: true, force: true }).catch(() => {});
    this.staging = null;
    for (const kept of this.hearing.values()) {
      clearTimeout(kept.quiet);
      kept.turn?.end();
      if (kept.lines) this.drop(kept.lines);
      delete kept.lines;
      delete kept.over;
    }
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
    if (kind === "browser.set_input_files") return this.upload(session, args, signal, stop, id);
    // Its agent acted since an upload's prompt named an input: that prompt's upload is not coming, or, allowed
    // and still having its files read, comes to nothing (upload).
    if (!looks(kind, args)) this.unname(session);
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
      // Handed back a moment ago, its page may not have answered yet for what it did before: it acts once it
      // has. That wait has its own bound (settle), and the operation's begins after it: a page slow to answer
      // after a hand back is not closed for it, and the operation has all the time it is meant to.
      const settled = this.settling.get(page);
      if (settled) {
        if ((await until(settled, stop, HELD)) === HELD) return PAUSED;
        if (signal.aborted) return CANCELLED;
      }
      const work = this.doing(page, operation(page, args, stop));
      const value = BOUNDED.has(kind) ? await this.bounded(page, work, stop) : await work;
      // Taken over while it acted: what its pages did meanwhile stays for its session's next answer.
      if (stop.aborted) return PAUSED;
      if (!isRecord(value) || kind === "browser.observe" || kind === "browser.evaluate") return { ok: value ?? null };
      // Cancelled while it acted: its answer goes to no one, and takes nothing with it of what its pages did.
      if (signal.aborted) return CANCELLED;
      const notices = this.took(session, id);
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
      const kept = this.hearing.get(page);
      clearTimeout(kept?.quiet);
      kept?.turn?.end();
      if (kept?.lines) this.drop(kept.lines);
      this.hearing.delete(page);
      this.settling.delete(page);
    });
    page.on("popup", (popup) => this.adopt(session, popup));
    // A tab that crashed answers nothing until it is loaded again: it is not waited for meanwhile (read).
    page.on("crash", () => this.crashed.add(page));
    page.on("framenavigated", (frame) => {
      if (frame === page.mainFrame()) this.crashed.delete(page);
      else this.framed(page, frame);
    });
    page.on("framedetached", (frame) => this.unframed(page, frame));
    this.hearing.set(page, { session, heard: null, acting: 0 });
    // One that opens under its user's hand is heard from the hand back.
    if (this.held === null) this.hear(page);
  }

  // A session's *page* is heard when it asks for a file. With a listener, the browser opens no file dialog
  // of its own: no path the agent did not get reaches a page.
  private hear(page: Page): void {
    const kept = this.hearing.get(page);
    if (!kept || kept.heard !== null) return;
    const { session } = kept;
    kept.heard = (chooser: FileChooser) => this.asks(page, session, chooser);
    page.on("filechooser", kept.heard);
  }

  // *page* is heard no more: a file input in it opens the browser's own chooser, as in any browser.
  private unhear(page: Page): void {
    const kept = this.hearing.get(page);
    if (!kept) return;
    if (kept.heard) page.off("filechooser", kept.heard);
    kept.heard = null;
    if (kept.lines) this.drop(kept.lines);
    delete kept.lines;
    delete kept.over;
  }

  // While its user holds the browser, what *page* asks for is heard on lines of this host's own, and not by
  // Playwright. Playwright reads each input that asks, twice, as a gesture: so a page that keeps asking keeps
  // what a click gave it for as long as it asks, and with it can open a window in front of its user, fill
  // their screen or write their clipboard, from any tab the agent ever opened. The browser says an ask to
  // each line that listens for it, and on this host's nobody reads the input: the page is given nothing, and
  // its leave runs out. A line to the page, and one to each frame a process of its own draws: those it has
  // now, and each it makes or sends elsewhere while they hold the browser (framed). Playwright's listener
  // goes only once every one of those lines hears, which a busy page keeps waiting: until then it hears as before.
  private overhear(page: Page): void {
    const kept = this.hearing.get(page);
    if (!kept?.heard || kept.lines || kept.over) return;
    const lines: CDPSession[] = [];
    const mine = { lines, more: [] as Array<Promise<unknown>> };
    kept.over = mine;
    const made = async (): Promise<void> => {
      await Promise.allSettled([
        this.line(page, page, lines),
        ...page.frames().filter((frame) => frame !== page.mainFrame()).map((frame) => this.frameLine(page, frame, lines)),
      ]);
      // And those of the frames that came meanwhile.
      for (let next = 0; next < mine.more.length; next += 1) await mine.more[next];
    };
    void made().then(() => {
      if (this.hearing.get(page) !== kept || kept.over !== mine || this.held === null) return void this.drop(lines);
      delete kept.over;
      kept.lines = lines;
      if (kept.heard) page.off("filechooser", kept.heard);
      kept.heard = null;
    });
  }

  // A line of this host's own to *target*, a page or a frame of it, among its page's *lines*: it hears what
  // the target asks for, and the browser opens no chooser of its own for it. None for a frame its page's own
  // process draws, which that page's line hears, nor for a page that is gone. Never rejects.
  private async line(page: Page, target: Page | Frame, lines: CDPSession[]): Promise<CDPSession | null> {
    const line = await page.context().newCDPSession(target).catch(() => null);
    if (line === null) return null;
    lines.push(line);
    line.on("Page.fileChooserOpened", () => this.overheard(page, lines));
    // The browser stops a page's own chooser for a line only once the line has the page's events.
    await Promise.all([line.send("Page.enable"), line.send("Page.setInterceptFileChooserDialog", { enabled: true })]).catch(() => {});
    return line;
  }

  // The line to *frame* among its page's *lines*, made anew: a frame sent to another site is drawn by another
  // process from then on, which the line it had does not reach. One at a time for a frame, and one line kept
  // for it: a page that sends a frame to and fro makes no more of them than it has frames.
  private frameLine(page: Page, frame: Frame, lines: CDPSession[]): Promise<CDPSession | null> {
    const each = this.frameLines.get(lines) ?? new Map<Frame, Promise<CDPSession | null>>();
    this.frameLines.set(lines, each);
    const made = (each.get(frame) ?? Promise.resolve(null)).then(async (before) => {
      const line = await this.line(page, frame, lines);
      if (line === null) return before;
      if (before !== null) this.unline(before, lines);
      return line;
    });
    each.set(frame, made);
    return made;
  }

  // *line*, one of a page's *lines*, hears no more and is closed.
  private unline(line: CDPSession, lines: CDPSession[]): void {
    if (lines.includes(line)) lines.splice(lines.indexOf(line), 1);
    this.drop([line]);
  }

  // *frame* of *page* has a document now, made or sent elsewhere while its user holds the browser: heard on a
  // line of this host's own as the frames the page had at the take-over are, where a process of its own draws
  // it. Without one, the browser's own chooser would open for it while the page it is in is not let be yet.
  private framed(page: Page, frame: Frame): void {
    const kept = this.hearing.get(page);
    const lines = kept?.lines ?? kept?.over?.lines;
    if (this.held === null || !kept || !lines) return;
    const made = this.frameLine(page, frame, lines);
    kept.over?.more.push(made);
  }

  // *frame* is gone from *page*: so is the line to it.
  private unframed(page: Page, frame: Frame): void {
    const kept = this.hearing.get(page);
    const lines = kept?.lines ?? kept?.over?.lines;
    const each = lines && this.frameLines.get(lines);
    const made = each?.get(frame);
    if (!lines || !each || !made) return;
    each.delete(frame);
    void made.then((line) => {
      if (line !== null) this.unline(line, lines);
    });
  }

  // *page* asked for a file, as one of this host's own *lines* to it heard. While its user holds the browser
  // it is kept for no one, and the page's quiet begins anew, as where Playwright hears it (asks). Handed back,
  // those lines hear a little longer beside Playwright, which is the one listened to then.
  private overheard(page: Page, lines: CDPSession[]): void {
    const kept = this.hearing.get(page);
    if (this.held === null || !kept || (kept.lines !== lines && kept.over === undefined)) return;
    if (kept.acting === 0) this.quiet(page);
  }

  // This host's own *lines* to a page hear it no more, and are closed.
  private drop(lines: CDPSession[]): void {
    for (const line of lines) {
      void line.send("Page.setInterceptFileChooserDialog", { enabled: false }).catch(() => {});
      void line.detach().catch(() => {});
    }
  }

  // *page* is asked to answer, READS times over, now that the browser is handed back (heardOut).
  // Playwright says a page asked for a file only once it has read the input, which a busy page keeps
  // waiting: so what a page asked for while its user held the browser, or before they took it, can be
  // heard of only after the hand back. Until the page has answered, what it asks for is kept for no one
  // (asks) and nothing of the agent's acts in it (act). And for SETTLE_MS at most: a page one of whose
  // frames is stuck is the agent's again then, and what it asked for before, if it says so only after, is kept.
  private settle(page: Page): void {
    let timer: NodeJS.Timeout | undefined;
    const late = new Promise<void>((resolve) => {
      timer = setTimeout(resolve, SETTLE_MS);
    });
    const settled: Promise<void> = Promise.race([this.heardOut(page, () => true, late), late]).then(() => {
      clearTimeout(timer);
      if (this.settling.get(page) === settled) this.settling.delete(page);
    });
    this.settling.set(page, settled);
  }

  // *page* is asked to answer READS times over, one after the other: by the last answer, what it had asked
  // for before the first was sent has been heard of. Playwright reads an input that asked in up to three
  // steps, each sent when the one before has answered: it finds the input, makes its own helper in that
  // frame where it has none yet, and reads the input, which gives the page leave and is when the ask is
  // heard. A busy page keeps the first of them waiting, and the rest follow once it is free. Each answer
  // here comes after one more of those steps was sent, this host's reading being sent later than the step
  // it follows: so three cover the three, and one more is to spare. *still*: asked no more once it is not so.
  private async heardOut(page: Page, still: () => boolean = () => true, over?: Promise<unknown>): Promise<void> {
    for (let n = 0; n < READS && still(); n += 1) await this.read(page, true, over);
  }

  // *page* is asked to answer, wherever its frames run: the page, and each frame of it that a process of
  // its own draws, the rest sharing a thread with one of those. Settled once each has answered, or cannot.
  // Asked over a line of this host's own to the browser, and not through Playwright: Playwright sends
  // every reading of a page as its user's own act, which gives the frame what a click of theirs gives it
  // for five seconds: leave to open a window, fill the screen, write the clipboard, ask for a file, send
  // the tab elsewhere from a frame. This reading is the host's own, of every page there is, at moments the
  // agent did not choose, so it gives a page nothing, and tells it nothing: it runs none of the page's code.
  // *over*: once it settles the answer is waited for no longer. Each line is closed when its answer has come,
  // when that wait is over, and when the page's tab has crashed, which answers nothing more: none is left open.
  private read(page: Page, framed = true, over?: Promise<unknown>): Promise<unknown> {
    if (this.crashed.has(page)) return Promise.resolve();
    const targets = [page, ...(framed ? page.frames().filter((frame) => frame !== page.mainFrame()) : [])];
    let crashes = (): void => {};
    const crashed = new Promise<void>((resolve) => {
      crashes = () => resolve();
      page.once("crash", crashes);
    });
    const ended = over === undefined ? crashed : Promise.race([over, crashed]);
    return Promise.allSettled(targets.map(async (target) => {
      // None for a frame its page's own process draws, nor for a page that is gone.
      const line = await page.context().newCDPSession(target).catch(() => null);
      if (line === null) return;
      try {
        await Promise.race([line.send("Runtime.evaluate", { expression: "1" }), ended]);
      } finally {
        void line.detach().catch(() => {});
      }
    })).finally(() => page.off("crash", crashes));
  }

  // *page*, held and heard, is let be OWN_CHOOSER_MS after it has answered for all that was sent it before
  // now (heardOut), unless it is heard of again before: and then only once it has answered again, with
  // nothing heard of it meanwhile. Counted from its answer, and not from now: Playwright reads an input
  // that asked twice, each as a gesture, and says the page asked after the first. A page busy from then on
  // gets the second late, and with it leave: five seconds from the ask it would be let be with that leave
  // fresh, ask on it, and the browser's own chooser would open. And answered again before it is let be: a
  // page that asked and was busy from then on is heard of only when the first reading reaches it, which
  // begins its quiet anew (asks). With no bound: a page one of whose frames never answers is not let be, and
  // a file input in it opens nothing for its user while they hold the browser.
  private quiet(page: Page): void {
    const kept = this.hearing.get(page);
    if (!kept || (kept.heard === null && !kept.lines)) return;
    clearTimeout(kept.quiet);
    // The turn before this one is over: what it still waits for of the page is waited for no longer.
    kept.turn?.end();
    let end = (): void => {};
    const over = new Promise<void>((resolve) => {
      end = resolve;
    });
    const turn = { end };
    kept.turn = turn;
    const mine = () => this.hearing.get(page) === kept && kept.turn === turn && (kept.heard !== null || kept.lines !== undefined);
    void this.heardOut(page, mine, over).then(() => {
      if (!mine()) return;
      kept.quiet = setTimeout(() => {
        void this.heardOut(page, mine, over).then(() => {
          if (mine() && kept.acting === 0) this.unhear(page);
        });
      }, OWN_CHOOSER_MS);
    });
  }

  // *work* is something done in *page* that gives it leave when it reaches it, until it ends: an operation
  // of the agent's, or what this host does there for an upload (placed, fill). Taken over meanwhile, the
  // operation is answered at once, but what was sent the page can reach it later, as a click does that
  // waits on a busy page: so the page's quiet is counted only from the end of the last such work, whatever
  // bound the one that waited for it had.
  private doing<T>(page: Page, work: Promise<T>): Promise<T> {
    const kept = this.hearing.get(page);
    if (!kept) return work;
    kept.acting += 1;
    void work.then(() => {}, () => {}).then(() => {
      kept.acting -= 1;
      if (this.held !== null && kept.acting === 0) this.quiet(page);
    });
    return work;
  }

  // Where *chooser*'s input is now, as the browser says it (place). Null where its page is closed, went
  // elsewhere since it asked, or does not say within LOOK_MS: the question is on its way to the page all
  // the same, and counted so until it has reached it (doing).
  private async placed(chooser: FileChooser): Promise<Place | null> {
    let timer: NodeJS.Timeout | undefined;
    const late = new Promise<null>((resolve) => {
      timer = setTimeout(() => resolve(null), LOOK_MS);
    });
    try {
      return await Promise.race([this.doing(chooser.page(), chooser.element().evaluate(place).catch(() => null)), late]);
    } finally {
      clearTimeout(timer);
    }
  }

  // *page*, of *session*'s, asked for a file: the input is kept for an upload, and its agent told. Not while
  // its user holds the browser: what a page asks for then is their own doing, or the page's under their
  // hand, and no input of theirs is the agent's to fill, at the hand back either. Heard then, the page has
  // leave to ask again, so its quiet begins anew: or, where something is still on its way to it, when that
  // has reached it (doing), and not five seconds from now with that still to come.
  private asks(page: Page, session: string, chooser: FileChooser): void {
    if (this.held !== null) {
      if ((this.hearing.get(page)?.acting ?? 0) === 0) this.quiet(page);
      return void this.release(chooser);
    }
    // Handed back, and not answered since: it asked for this before, under its user's hand or by what the take-over stopped.
    if (this.settling.has(page)) return void this.release(chooser);
    const before = this.choosers.get(session);
    this.choosers.set(session, chooser);
    this.release(before);
    this.note(session, FILE_ASKED);
  }

  // This host's handle on *chooser*'s input is let go, where nothing keeps the input for an upload any
  // more, or is giving it files: each file a page asks for leaves one here, and a page can ask without end.
  private release(chooser: FileChooser | undefined): void {
    if (!chooser || this.giving.has(chooser) || this.released.has(chooser)) return;
    for (const kept of this.choosers.values()) if (kept === chooser) return;
    for (const kept of this.named.values()) if (kept.input?.chooser === chooser) return;
    this.released.add(chooser);
    void chooser.element().dispose().catch(() => {});
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
    // The id the browser said for it is its own, and is kept for no later download of its address and name.
    this.name(download);
    await download.cancel().catch(() => {});
    await download.delete().catch(() => {});
    this.nameless(download);
  }

  // The files the main side read from the chat's folder, given once to the file input an upload's
  // prompt named, or, unasked, to the one the session's pages asked for last: the page is given names
  // and what they hold, never a path. Taken over while it is on its way, it gives the page nothing (give).
  // *id*: its operation's. One its user was asked about is given only to what its own prompt named: where
  // that is the session's no more, since its agent acted before its files came or another upload was
  // asked about meanwhile, it is given to nothing, and never to whatever asked last.
  private async upload(session: string, args: Record<string, unknown>, signal: AbortSignal, stop: AbortSignal, id: string | undefined): Promise<Outcome> {
    const kept = this.named.get(session);
    const named = kept !== undefined && kept.of === id ? kept : undefined;
    if (named) this.named.delete(session);
    const asked = id !== undefined && this.prompted.delete(id);
    if (asked && !named) return failed(NOT_AS_ASKED);
    if (named && named.input === null) return failed(named.why);
    const chooser = named?.input?.chooser ?? this.choosers.get(session);
    if (!chooser) return failed(NOT_ASKED);
    try {
      return await this.fill(session, chooser, named?.input ?? null, args, signal, stop, id);
    } finally {
      // Named for this upload alone, or given its files: this host's handle on it is let go.
      this.release(chooser);
    }
  }

  // An upload's files given to *chooser*'s input, of *session*'s: *at*, where its prompt named it.
  private async fill(
    session: string, chooser: FileChooser, at: { href: string; origin: string } | null, args: Record<string, unknown>, signal: AbortSignal, stop: AbortSignal,
    id: string | undefined,
  ): Promise<Outcome> {
    // Its page closed, or is this session's no more.
    if (this.sessionOf(chooser.page()) !== session) return failed(NOT_ASKED);
    const files = filesOf(args.files);
    if (!files) return failed("A file for the page is a name, its type and what it holds");
    // A question the input's page asked its user is theirs still, as for anything else the agent would do in
    // the page (act): nothing is given into it, or into a frame of it that the question does not hold.
    if (this.asking.has(chooser.page())) {
      if (!(await this.answers(chooser.page()))) return ASKING;
      if (stop.aborted) return PAUSED;
    }
    let refused: unknown;
    try {
      // Answered at once where its user takes the browser over, its steps may reach the page later: the
      // input is held until the last of them has.
      this.giving.add(chooser);
      const giving = this.give(session, chooser, files, at, stop).finally(() => {
        this.giving.delete(chooser);
        this.release(chooser);
      });
      refused = await this.bounded(chooser.page(), this.doing(chooser.page(), giving), stop);
    } catch (error) {
      if (stop.aborted) return PAUSED;
      return failed(said(error));
    }
    if (stop.aborted || refused === HELD) return PAUSED;
    if (typeof refused === "string") return failed(refused);
    // The input that asked last has its files: its agent is not told again, with this answer, that the page asked
    // for one. Where another input has asked since, that is still to tell.
    const last = this.choosers.get(session) === chooser;
    if (last) {
      this.choosers.delete(session);
      this.unseen.set(session, (this.unseen.get(session) ?? []).filter((notice) => notice !== FILE_ASKED));
    }
    // Cancelled while its files were given: its answer goes to no one, and takes nothing more with it.
    if (signal.aborted) return CANCELLED;
    return { ok: { files: files.length, notices: this.took(session, id) } };
  }

  // What *session*'s pages did since its last answer that said so, for the answer to its operation *id*: kept
  // no more for the next, unless that answer turns out to have reached no one (unanswered).
  private took(session: string, id: string | undefined): string[] {
    const notices = this.unseen.get(session) ?? [];
    this.unseen.delete(session);
    if (id !== undefined && notices.length > 0) {
      this.carried.set(id, { session, notices });
      // The oldest goes: a cancel that crosses an answer comes within a moment of it.
      if (this.carried.size > CARRIED) this.carried.delete(this.carried.keys().next().value as string);
    }
    return notices;
  }

  /**
   * The answer to the operation *id* reached no one: its cancel crossed the answer on the way, and whoever
   * waited for it had been answered cancelled. What the answer carried of what its session's pages did is
   * kept for the session's next answer again, before what they have done since. Nothing for an operation
   * whose answer carried none, or whose session has no tab now.
   */
  unanswered(id: string): void {
    const carried = this.carried.get(id);
    if (!carried) return;
    this.carried.delete(id);
    if (!this.tabs.has(carried.session)) return;
    const since = (this.unseen.get(carried.session) ?? []).filter((notice) => notice !== FILE_ASKED || !carried.notices.includes(FILE_ASKED));
    this.unseen.set(carried.session, [...carried.notices, ...since].slice(0, MAX_NOTICES));
  }

  // No input is named for *session*'s upload any more.
  private unname(session: string): void {
    const named = this.named.get(session);
    this.named.delete(session);
    this.release(named?.input?.chooser);
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
    const said = (why: Unfit): string => (why === "single" ? ONE_FILE : why === "folder" ? A_FOLDER : at ? NOT_AS_ASKED : NOT_ASKED);
    // Looked at first: an input that would take none of them is sent none of them. The step that gives them
    // looks again, as it gives.
    const unfit = unfitFor(await input.evaluate(place), files.length, at);
    if (unfit !== null) return said(unfit);
    if (stop.aborted) return HELD;
    const made = await input.evaluateHandle(make, files);
    try {
      for (let tries = 0; tries < GIVE_TRIES; tries += 1) {
        if (stop.aborted) return HELD;
        const came = await made.evaluate(put, { input, by: Date.now() + GIVE_MS, at: at && { href: at.href, origin: at.origin } });
        // The page was too busy to take them in time: looked at again, and sent again.
        if (came === "late") continue;
        if (came !== "given") return said(came);
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
    this.name(download);
    if (stop) {
      this.arriving.add(download);
      this.grew.set(download, { bytes: -1, at: this.now() });
      this.watch();
    }
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
        this.endedIn(download, basename(path));
      } catch {
        if (stop?.aborted) tell(interrupted(name));
        else if (this.overfull.has(download)) tell(tooMuch(name, this.options.stagedBytes ?? STAGED_MOST_BYTES));
        else if (this.stalled.has(download)) tell(notFinished(name, stalledFor(this.options.stalledMs ?? STALLED_MS)));
        else tell(notFinished(name, unfinished(await download.failure().catch(() => null))));
        return;
      }
      const size = await stat(path).then((found) => found.size, () => null);
      const most = this.options.downloadBytes ?? MAX_WRITE_BYTES;
      if (stop?.aborted || root === undefined || !this.options.downloaded || size === null || size > most) {
        if (root !== undefined && this.options.downloaded) tell(stop?.aborted ? interrupted(name) : size === null ? unmeasured(name) : tooLarge(name, size, most));
        await download.delete().catch(() => {});
        return;
      }
      // One of the agent's that ends is handed on only where the agent's own, with it, are no more than may be staged.
      const bound = this.options.stagedBytes ?? STAGED_MOST_BYTES;
      if (!user && (await this.staged({ download, path })).own + size > bound) {
        tell(tooMuch(name, bound));
        await download.delete().catch(() => {});
        return;
      }
      if (!user) this.waiting.add(path);
      this.options.downloaded({ root, session, name, path, user, ...(user && of.after === true ? { afterHandBack: true as const } : {}) });
    } finally {
      this.arriving.delete(download);
      this.grew.delete(download);
      this.nameless(download);
      if (this.arriving.size === 0) this.unwatch();
    }
  }

  /**
   * The browser says a download begins, on a line of this host's own: *id*, the name of the file it stages it
   * under, for the download Playwright announces with the same address and the same name.
   */
  begins(id: string, url: string, name: string): void {
    const at = this.unnamed.findIndex((download) => download.url() === url && download.suggestedFilename() === name);
    if (at !== -1) return void this.ids.set(this.unnamed.splice(at, 1)[0] as Download, id);
    this.said.push({ id, url, name, at: this.now() });
    // One Playwright never announces here is matched to nothing: the oldest go.
    if (this.said.length > UNMATCHED) this.said.shift();
  }

  // *download* is announced: the id the browser said for it, where it has; else it waits for the browser's word.
  // The oldest said of its address and name, as the browser says them in the order Playwright announces them:
  // two of one address and one name, begun together, are each held under the id of its own file. Not one
  // said SAID_MS before, which is of a download nobody announced.
  private name(download: Download): void {
    const stale = this.now() - SAID_MS;
    for (let nth = this.said.length - 1; nth >= 0; nth -= 1) if ((this.said[nth] as { at: number }).at < stale) this.said.splice(nth, 1);
    const at = this.said.findIndex(({ url, name }) => url === download.url() && name === download.suggestedFilename());
    if (at === -1) return void this.unnamed.push(download);
    this.ids.set(download, (this.said.splice(at, 1)[0] as { id: string }).id);
  }

  // *download* has ended in the file the browser named by *id*, which is its id whatever was held for it till
  // now. Another download held under that id till now has the one this was held under: the two were taken for
  // each other, and from here on each is counted by its own file.
  private endedIn(download: Download, id: string): void {
    const held = this.ids.get(download);
    if (held === id) return;
    const said = this.said.findIndex((one) => one.id === id);
    if (said !== -1) this.said.splice(said, 1);
    for (const [other, its] of this.ids) {
      if (its !== id) continue;
      if (held === undefined) this.ids.delete(other);
      else this.ids.set(other, held);
    }
    this.ids.set(download, id);
  }

  // *download* is over: nothing is held for it.
  private nameless(download: Download): void {
    this.ids.delete(download);
    if (this.unnamed.includes(download)) this.unnamed.splice(this.unnamed.indexOf(download), 1);
  }

  // What the agent's own downloads have staged now: *own*, all of it but that of *ending*, which is measured
  // by itself; *each*, the bytes of each on its way. One on its way is counted by the file the browser named
  // for it. Where the browser said no id for one of the agent's, what is in the folder under no id this host
  // knows is taken for it: the agent's own cannot then be told from its user's, and the bound and the stall
  // are the folder's.
  private async staged(ending?: { download: Download; path: string }): Promise<{ own: number; each: Map<Download, number> }> {
    const files = await this.sizes();
    for (const path of this.waiting) if (!files.has(basename(path))) this.waiting.delete(path);
    const waits = [...this.waiting].map((path) => basename(path));
    // A download's file is named by its id once it has ended, and by its id with an ending of the browser's
    // own (.crdownload) while it is on its way.
    const of = (name: string): string => name.split(".")[0] as string;
    const known = new Set([...this.ids.values(), ...waits, ...(ending ? [basename(ending.path)] : [])]);
    const stray = [...files].reduce((sum, [name, size]) => (known.has(of(name)) ? sum : sum + size), 0);
    const bytes = (id: string): number => [...files].reduce((sum, [name, size]) => (of(name) === id ? sum + size : sum), 0);
    const each = new Map<Download, number>();
    let unsaid = false;
    for (const download of this.arriving) {
      if (download === ending?.download) continue;
      const id = this.ids.get(download);
      unsaid ||= id === undefined;
      each.set(download, id === undefined ? stray : bytes(id));
    }
    const own = waits.reduce((sum, name) => sum + (files.get(name) ?? 0), 0)
      + [...each].reduce((sum, [download, bytes]) => (this.ids.has(download) ? sum + bytes : sum), 0)
      + (unsaid ? stray : 0);
    return { own, each };
  }

  // What is staged is looked at, once a second, while a download of the agent's is on its way: each file of the
  // staging folder, which the browser names by its download's id, and its size. Once the agent's own, on their
  // way and waiting to be saved, are more than may be staged, every one of the agent's still on its way is
  // stopped and dropped; and one of them whose own bytes have not grown for the stated time is stopped and
  // dropped alone (stage tells why). Its user's own are counted nowhere, and are not among those stopped.
  private watch(): void {
    if (this.watching !== null) return;
    const limit = this.options.stalledMs ?? STALLED_MS;
    let looking = false;
    const mine: NodeJS.Timeout = setInterval(() => {
      if (looking) return;
      looking = true;
      void this.staged().then(({ own, each }) => {
        looking = false;
        if (this.watching !== mine) return;
        const overfull = own > (this.options.stagedBytes ?? STAGED_MOST_BYTES);
        for (const download of this.arriving) {
          const bytes = each.get(download) ?? 0;
          const kept = this.grew.get(download);
          if (!kept || bytes > kept.bytes) this.grew.set(download, { bytes, at: this.now() });
          const why = overfull ? this.overfull : kept && bytes <= kept.bytes && this.now() - kept.at >= limit ? this.stalled : null;
          if (why === null) continue;
          why.add(download);
          void download.cancel().catch(() => {});
        }
      });
    }, Math.min(STAGED_LOOK_MS, limit / 4));
    this.watching = mine;
  }

  private unwatch(): void {
    if (this.watching !== null) clearInterval(this.watching);
    this.watching = null;
  }

  // The browser is asked to say each download as it begins, on a line of this host's own that stays for as
  // long as the browser runs: with the id it names the download's staged file by (begins). Asked as Playwright
  // asks it, with the same folder, so that nothing of where downloads go changes. Where the browser will not
  // say, a download of the agent's is counted by what the folder holds under no id (staged).
  private async saysDownloads(context: BrowserContext, staging: string): Promise<void> {
    try {
      const line = await context.browser()?.newBrowserCDPSession();
      if (!line) return;
      line.on("Browser.downloadWillBegin", ({ guid, url, suggestedFilename }) => this.begins(guid, url, suggestedFilename));
      await line.send("Browser.setDownloadBehavior", { behavior: "allowAndName", downloadPath: staging, eventsEnabled: true });
    } catch {
      // Said by nobody: counted by the folder.
    }
  }

  // Each file of the staging folder now, and its size: none where there is no folder, or it cannot be read.
  private async sizes(): Promise<Map<string, number>> {
    const found = new Map<string, number>();
    const folder = this.staging;
    if (folder === null) return found;
    for (const name of await readdir(folder).catch(() => [])) {
      const size = await stat(join(folder, name)).then((file) => file.size, () => null);
      if (size !== null) found.set(name, size);
    }
    return found;
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
  // that answers has had its question answered since, and is its agent's again. Asked as this host asks
  // (read), which gives the page nothing: it is asked before each thing the agent would do there.
  private async answers(page: Page): Promise<boolean> {
    let timer: NodeJS.Timeout | undefined;
    const late = new Promise<false>((resolve) => {
      timer = setTimeout(() => resolve(false), ASKING_MS);
    });
    const answered = await Promise.race([late, this.read(page, false, late).then(() => true)]);
    clearTimeout(timer);
    if (answered) this.asking.delete(page);
    return answered;
  }

  // What a session's page did that its agent could not see happen. While its user holds the browser, what
  // a page asks or starts is their own doing, or the page's under their hand: its agent is told nothing of it.
  // Told once until its session's next answer carries it, however often the page does it meanwhile: a page that
  // keeps asking for a file would else fill the answer, and leave no room for what is kept of the agent's own acts.
  private note(session: string, notice: string): void {
    if (this.held === null && !(this.unseen.get(session) ?? []).includes(notice)) this.keep(session, notice);
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
    const asked = this.choosers.get(session);
    this.choosers.delete(session);
    this.unname(session);
    this.release(asked);
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
    for (const kept of this.hearing.values()) {
      clearTimeout(kept.quiet);
      kept.turn?.end();
      if (kept.lines) this.drop(kept.lines);
      delete kept.lines;
      delete kept.over;
    }
    this.hearing.clear();
    this.settling.clear();
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
      await this.saysDownloads(context, staging);
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
