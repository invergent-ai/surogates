// The desktop shell's main process (spec, Sections 1 and 7): one window, as Claude
// Desktop has, signed in to one agent, and this computer's device link to that agent,
// which stays up with the window closed. It never runs what a tool call asks for.
// Readiness is awaited with then(), never a top-level await: an ES module main that
// awaits app.whenReady() deadlocks.

import { existsSync, mkdirSync, readFileSync, realpathSync, rmSync } from "node:fs";
import { homedir, hostname, userInfo } from "node:os";
import { join } from "node:path";

import {
  app, BrowserWindow, dialog, globalShortcut, type IpcMainEvent, Menu, nativeTheme, net, Notification, powerMonitor, safeStorage, session, shell, Tray, utilityProcess,
  type WebContents, webContents,
} from "electron";

import type { DesktopAccount } from "../../../web/src/lib/desktop-bridge-contract.js";
import type { LibraryEntry, Project, ProjectSummary, Routine, ThreadRow, Tier } from "../../../web/src/lib/projects-contract.js";
import { BROWSER_HOST, BrowserClient, type FromBrowser, type ToBrowser } from "../browser/client.js";
import { type BrowserChoice, BrowserSetting, browserVersion, choiceRows, chosenBrowser, findBrowsers, KNOWN, profileOf, profilesOf, unsupportedAt } from "../browser/choose.js";
import { Browsing } from "../browser/executor.js";
import type { ApprovalPrompts } from "../binding/approvals.js";
import type { FolderPrompts } from "../binding/binder.js";
import { revokeDevice, verifyDevice } from "../device.js";
import { fileToolsMissing, pathOutside, toolsMissing } from "../hosts/policy.js";
import { FolderLooks, LOOK_MS } from "./folders-held.js";
import { OperationJournal } from "../journal/journal.js";
import type { LinkStatus } from "../link/client.js";
import { type FromManager, MANAGER, type ManagerProcess, REPO_IMAGE, type ToManager, VmClient, vmEnv, vmOptions } from "../vm/client.js";
import { type Delivery, ImageDelivery, installBase, readManifest } from "../vm/image.js";
import { missingTools } from "../vm/linux.js";
import type { Boot } from "../vm/manager.js";
import { openAbout } from "./about.js";
import { BURST, Burst, followChat, followInbox, type InboxItem, titleOf } from "./agent-events.js";
import { type Agent, AgentStore, connectAgent, describeAgent, type Get, linksFor, linkUrl, partitionFor, readAgent } from "./agents.js";
import { autostartFile, HIDDEN, LAUNCHER, loginRefusal, setStartAtLogin, startsAtLogin } from "./autostart.js";
import { AppearanceStore, Theme } from "./appearance.js";
import { bridgeHandlers } from "./bridge.js";
import { reauthorize, rebind, register } from "./computer.js";
import { type Credential, CredentialStore, type LiveCredential } from "./credentials.js";
import { linkIn, type OpenLink } from "./deep-link.js";
import { type DeviceStack, startDevice, stopDevice } from "./device-stack.js";
import { type FolderRow, listFolders, LiveProcesses, stopOperation } from "./folders.js";
import { letWindowClose, MainWindow } from "./main-window.js";
import { appMenu, trayIcon, trayMenu } from "./menus.js";
import { Notifications } from "./notifications.js";
import { type Fetch, OAuthError, revokeTokens, signInWithBrowser, type Tokens } from "./oauth.js";
import { PreferencesStore } from "./preferences.js";
import { ANSWER_TIMEOUT_MS, PageProjects, TimedOut } from "./projects.js";
import { type BrowserPrompts, desktopPrompts } from "./prompts.js";
import { QUICK_ENTRY_KEYS, QuickEntry, waylandSession } from "./quick-entry.js";
import { type SandboxAction, sandboxLine } from "./sandbox.js";
import { accountOf, DesktopSession, SessionStore, type SignedIn, type SignedInAccount } from "./session.js";
import { appTools, BWRAP } from "./tools.js";
import { asShown } from "./text.js";
import { helperRun, installedUpdates, keepChecked, ROOT_RECORD, updateLine, Updates, type UpdatesOptions } from "./updates.js";
import { ownPage, sameOrigin, webClientPath } from "./window-policy.js";
import { type Bounds, WindowStates } from "./window-state.js";

// Started from VS Code's terminal, or Claude Code's, the app has ELECTRON_RUN_AS_NODE set. Its Electron,
// RunAsNode fuse off, ignores it but keeps it, and what the app starts would take it: the VM manager's
// QEMU and virtiofsd, the system browser, every spawn. It goes before anything is started.
delete process.env.ELECTRON_RUN_AS_NODE;
// No fuse covers Chromium's remote debugging, and on the app's fuses --remote-debugging-port still opens
// CDP: any process of the user's could drive the app's pages, its bridge among them. The installed app
// refuses both switches; this package's own Electron keeps them, for the tests.
if (app.isPackaged && ["remote-debugging-port", "remote-debugging-pipe"].some((name) => app.commandLine.hasSwitch(name))) {
  console.error("Surogate does not start with remote debugging (--remote-debugging-port or --remote-debugging-pipe).");
  process.exit(1);
}

const PAGES = join(import.meta.dirname, "pages");
const PAGES_PRELOAD = join(import.meta.dirname, "pages-preload.cjs");
const BRIDGE_PRELOAD = join(import.meta.dirname, "preload.cjs");
const PANE_PRELOAD = join(import.meta.dirname, "pane-preload.cjs");
const ASSETS = join(import.meta.dirname, "..", "..", "assets");
// The app's version, as its package names it.
const VERSION = (JSON.parse(readFileSync(join(import.meta.dirname, "..", "..", "package.json"), "utf8")) as { version: string }).version;

// Everything the app keeps lives under one root, Electron's own data too: the folder
// guards refuse it as a chat's folder, so no agent reaches a device token through one.
const dataHome = process.env.XDG_DATA_HOME?.startsWith("/") ? process.env.XDG_DATA_HOME : join(app.getPath("home"), ".local", "share");
const root = join(dataHome, "surogate");
// Before ready: the OS keyring names its item after the app.
app.setName("Surogate");
app.setPath("userData", join(root, "electron"));

const states = new WindowStates(join(root, "window-state.json"));
const appearance = new AppearanceStore(join(root, "settings.json"));
const preferences = new PreferencesStore(join(root, "preferences.json"));
// Start at login's entry, in the user's XDG config folder; and what it starts: the installed app's
// launcher, or a development build's Electron on this main.
// A relative XDG_CONFIG_HOME is ignored, as the XDG Base Directory specification says: Electron's appData takes it as it is.
const configHome = process.env.XDG_CONFIG_HOME?.startsWith("/") ? process.env.XDG_CONFIG_HOME : join(app.getPath("home"), ".config");
const autostart = autostartFile(configHome);
const loginCommand = (): string[] => (app.isPackaged ? [LAUNCHER] : [process.execPath, import.meta.filename]);
const agents = new AgentStore(join(root, "agent.json"));
// Settings → Browser: the browser the agent drives on this computer, for every agent.
const browserSetting = new BrowserSetting(join(root, "browser.json"));
let main: MainWindow | null = null;
let theme: Theme;
// After ready: safeStorage answers only then.
let credentials: CredentialStore;
// The desktop's own prompts, over the window, once it is made.
let prompts: FolderPrompts & ApprovalPrompts & BrowserPrompts;
let sessionStore: SessionStore;
// Who is signed in to the app, with the agent: what adds this computer, and what the window's web client takes its session from.
let signedIn: DesktopSession | null = null;
// A sign-in under way in the system browser: starting another cancels it. Aborted with LOG_OUT by a
// log out, which alone ends what it made; with QUIT by a quit, which keeps it; or by a sign-in started again.
let signingIn: AbortController | null = null;
const LOG_OUT = "log out";
const QUIT = "quit";
// Settled once the latest sign-in has stopped: what a quit waits for, so that a token the agent issued it is kept.
let signedInOrStopped: Promise<void> = Promise.resolve();
// Settled once every sign-in and Restore started so far has stopped: what a log out waits for.
let underWay: Promise<unknown> = Promise.resolve();
let signInFailure: string | null = null;
// The agent would add this computer only on a more recent sign-in: the user signs in again.
let signInAgain = false;
// The window's web client loads again with a new sign-in's session: the sign-in shows until it has.
let reloading = false;
// The app's calls to the agent, through Chromium's network as the window's are.
const apiFetch: Fetch = (url, init) => net.fetch(url, init);
/**
 * GET *url* through Chromium's network, telling *hop* each redirect before it is followed:
 * net.fetch names neither the hops nor where they ended.
 */
const getFollowing: Get = (url, hop, signal) => new Promise((resolve, reject) => {
  const request = net.request({ url, redirect: "manual" });
  const fail = (error: unknown): void => {
    request.abort();
    reject(error);
  };
  signal.addEventListener("abort", () => fail(signal.reason), { once: true });
  request.on("redirect", (_status, _method, to) => {
    try {
      hop(to);
      request.followRedirect();
    } catch (error) {
      fail(error);
    }
  });
  request.on("response", (response) => {
    const chunks: Buffer[] = [];
    response.on("data", (chunk) => chunks.push(chunk));
    response.on("end", () => resolve(new Response(chunks.length > 0 ? Buffer.concat(chunks) : null, { status: response.statusCode })));
    response.on("error", reject);
  });
  request.on("error", reject);
  request.end();
});
// Devices logged out while the agent could not hear it, by device: each revoked on a link of its own once it can.
const revocations = new Map<string, { done: Promise<void>; stop(): Promise<void> }>();
// A log out under way, settled once it is done: a quit waits for it.
let signingOut: Promise<void> | null = null;
// A device the agent revoked, being cleaned up: a restore waits for it.
let retiring: Promise<void> = Promise.resolve();
let restoring = false;
// The Restore under way: a log out cancels it.
let restoringNow: AbortController | null = null;
let restoreFailure: string | null = null;
// A kept computer whose token a later sign-in rotates: the agent closes its old link as revoked, which retires nothing.
let rotating: Credential | null = null;

// A credential whose token the agent still takes: one a device can start on.
const live = (credential: Credential | null): credential is LiveCredential => credential?.token != null;
// What a page asking of a computer the agent revoked is told.
const REVOKED = "This computer's access to the agent was revoked: restore it from Surogate's window";
// What the file hosts are told of this computer's user: the home, which no chat's folder may be, and the language.
const env = { HOME: homedir(), LANG: process.env.LANG || "C.UTF-8" };
// This computer's credential for the agent, as stored: what the page is told, and what keeps a second registration out.
let kept: Credential | null = null;
// Its device, from the moment it starts.
let device: { credential: Credential; status: LinkStatus; stack: DeviceStack | null; started: Promise<DeviceStack> } | null = null;
let registering = false;
let connecting = false;
// What the web client tells once its user signed in (undefined until it has said), and the projects it serves.
let account: DesktopAccount | null | undefined;

// Whose the page is: the account it said is signed in, null once it said nobody is, and until it has
// said, whoever is signed in to the app, whose sign-in gave it its session.
function pageOwner(): DesktopAccount | null {
  return account === undefined ? signedIn?.account ?? null : account;
}

// Whether the page is *who*'s. One that said nobody is signed in is no account's.
function pageIs(who: { orgId: string; userId: string }): boolean {
  const owner = pageOwner();
  return owner?.orgId === who.orgId && owner.userId === who.userId;
}
const projects = new PageProjects((message) => main?.webContents()?.send("desktop:projects", message));
let served = false;
// Settled once the page serves its projects: a project chosen while it loads waits for this.
let serving = Promise.withResolvers<void>();
let listed: ProjectSummary[] = [];
// What the centre shows: a page of the web client, the Projects page, or a project's conversation,
// or one of its threads, with the project as the way back (Section 12, View thread).
type Opened = { id: string; name: string; masterSessionId: string };
type View =
  | { kind: "web" }
  | { kind: "projects" }
  | ({ kind: "project"; thread: { id: string; title: string } | null } & Opened);
let view: View = { kind: "web" };
// What the centre showed before the Projects page: Back and Forward leave the page for it.
let beforeProjects: View = { kind: "web" };
// The projects the page named, by their master session: the web client arriving at one shows that project.
const masters = new Map<string, Opened>();
// Each choice of what the centre shows takes a number: an answer that comes after a later choice applies nothing.
let choice = 0;
// Why what the user last asked for did not happen: a project that did not open, a thread not resolved.
let failure: string | null = null;
// A thread's row, open or resolve, that left the pane between its drawing and the click.
const NO_SUCH_THREAD = "No such thread in the open project";
// The open project, for the Overview pane, and what stops following it.
let overview: { project: Project; threads: ThreadRow[]; library: LibraryEntry[]; routines: Routine[] } | null = null;
let unfollow = (): void => {};
// The project dialog's view, while it is open over the window.
let projectDialog: WebContents | null = null;
// The open project's thread read in the Overview pane, beside its conversation.
let reading: { id: string; title: string } | null = null;

const report = (error: unknown): void => {
  console.error(error);
};

// A write of the app's own state that fails is said, and what follows goes on: a log out still ends what it can.
const trying = (write: () => void): void => {
  try {
    write();
  } catch (error) {
    report(error);
  }
};

// What asks Playwright to print what it says to the browser, in the environment of whoever starts the app. The
// browser host says its proxy's sign-in there, so it is started without them: the sign-in is in no log.
const PRINTS = /^(DEBUG|DEBUGP|DEBUG_.*|PWDEBUG.*)$/;
const unprinted = (env: NodeJS.ProcessEnv): NodeJS.ProcessEnv => Object.fromEntries(Object.entries(env).filter(([name]) => !PRINTS.test(name)));

// A process of the app's own in an Electron utility process: the VM manager (spec, Section 11)
// and each device's browser host (Section 1). A hang or a crash there leaves the windows and the
// device link alone. What is sent before it has spawned waits. *temp*: its temp folder, made now; the app's by default.
function utility<To, From>(script: string, serviceName: string, temp?: string): {
  send(message: To): void;
  onMessage(listener: (message: From) => void): void;
  onExit(listener: () => void): void;
  kill(): void;
} {
  if (temp) mkdirSync(temp, { recursive: true, mode: 0o700 });
  const env = temp ? { env: { ...unprinted(process.env), TMPDIR: temp } } : {};
  const child = utilityProcess.fork(script, [], { serviceName, stdio: "inherit", ...env });
  const waiting: To[] = [];
  let spawned = false;
  let exited = false;
  child.once("spawn", () => {
    spawned = true;
    for (const message of waiting.splice(0)) child.postMessage(message);
  });
  child.once("exit", () => {
    exited = true;
  });
  return {
    send: (message) => {
      if (exited) return;
      if (spawned) child.postMessage(message);
      else waiting.push(message);
    },
    onMessage: (listener) => void child.on("message", (message) => listener(message as From)),
    onExit: (listener) => {
      if (exited) listener();
      else child.once("exit", () => listener());
    },
    kill: () => void child.kill(),
  };
}

const utilityManager = (): ManagerProcess => utility<ToManager, FromManager>(MANAGER, "Surogate VM");
// A browser host keeps its own temp files, the browser's and Playwright's, under *profiles*: they go
// with the profiles, and a host that is killed leaves none in the system's temp folder. What it stages
// of a download is there too, and is read from nowhere else.
const browserTemp = (profiles: string): string => join(profiles, "tmp");
const utilityBrowser = (profiles: string) => () => utility<ToBrowser, FromBrowser>(BROWSER_HOST, "Surogate browser", browserTemp(profiles));

// What the VM needs of this computer (spec, Section 11, Requirements): what it lacks, looked for
// once the app is ready (null until then); its image's download, in a packaged app, or why there
// is none to make; its last boot.
let lacking: string[] | null = null;
let lackingFound: Promise<string[]> = Promise.resolve([]);
// What the VM itself lacks, as last looked for: a change of the folders looks for the file helper's tools alone.
let vmLacking: Promise<string[]> = Promise.resolve([]);
const VM_RESOURCES = app.isPackaged ? join(process.resourcesPath, "vm") : null;
// The environment the VM is made from: a packaged app's has no image or KVM device of a test's.
const VM_ENV = vmEnv(process.env, app.isPackaged);
let delivery: ImageDelivery | null = null;
// Aborted at the quit: a download or an unpack under way stops with the app, never writing on after
// it. The image's and an update's.
const stopDelivery = new AbortController();
// Where the install script recorded the base the app was installed from: root's, in an installed
// app. A development build reads only SUROGATE_INSTALL_JSON's, a test's, so it never downloads
// from an installed app's base.
const INSTALL_RECORD = app.isPackaged ? ROOT_RECORD : process.env.SUROGATE_INSTALL_JSON;
// What the app downloads from that base: no cookie of its own session goes with it, and none of it
// through the HTTP cache: a second copy of what is downloaded, and cached ranges in a resume.
const fromBase = (url: string, init: { headers: Record<string, string>; signal?: AbortSignal }) =>
  net.fetch(url, { ...init, credentials: "omit", cache: "no-store" });
let undeliverable: string | null = null;
let boot: Boot | null = null;
const deliveryState = (): Delivery | null => delivery?.state ?? (undeliverable === null ? null : { state: "failed", why: undeliverable });
const vmUser = () => {
  const { uid, gid, username } = userInfo();
  return { uid, gid, name: username, home: env.HOME };
};

// The guest's image: a packaged app downloads it from where it was installed from (the install
// script's record), and a development build boots the repository's, unless SUROGATE_INSTALL_JSON
// names an install record of a test's. SUROGATE_VM_IMAGE names an image to boot as it is, in a
// development build only.
function imageDelivery(): ImageDelivery | null {
  if (VM_ENV.SUROGATE_VM_IMAGE || !INSTALL_RECORD) return null;
  return new ImageDelivery({
    manifest: readManifest(join(VM_RESOURCES ?? REPO_IMAGE, "manifest.json")),
    // An installed app's record is root's alone to write, as the install script leaves it.
    base: () => installBase(INSTALL_RECORD, app.isPackaged),
    images: join(root, "vm", "images"),
    fetch: fromBase,
    signal: stopDelivery.signal,
  }, changed);
}

// Updates (spec, Section 9, "In-app updates"): an installed app checks the base it was installed
// from at its start and every 6 hours, trusting the release keys that the helper pkexec runs
// lists, and downloads into the user's cache. A development build updates only when a test names
// an install record and a helper of its own, SUROGATE_UPDATE_HELPER.
// A relative XDG_CACHE_HOME is ignored, as the XDG Base Directory specification says.
const cacheHome = process.env.XDG_CACHE_HOME?.startsWith("/") ? process.env.XDG_CACHE_HOME : join(app.getPath("home"), ".cache");
let updates: Updates | null = null;

function startUpdates(): void {
  const cache = join(cacheHome, "surogate", "updates");
  const helper = process.env.SUROGATE_UPDATE_HELPER;
  let options: UpdatesOptions;
  if (app.isPackaged) {
    options = installedUpdates(VERSION, cache, fromBase, stopDelivery.signal);
  } else if (INSTALL_RECORD && helper) {
    // A development build runs its test's helper itself: no pkexec, and no helper of an installed app's.
    options = {
      version: VERSION, record: INSTALL_RECORD, rootOwned: false, helper, installed: null, cache, fetch: fromBase, signal: stopDelivery.signal,
      apply: helperRun([helper]),
    };
  } else {
    return;
  }
  // Whichever build: all that a helper said of an install that did not end well goes to the log.
  // What it keeps of a newest release that no trusted key signed is under the app's own root,
  // which no chat's folder reaches.
  updates = new Updates({ ...options, standing: join(root, "update-unsigned.json"), log: report }, changed);
  // Checked at the start and every six hours; sooner after a check that failed; and when the
  // computer wakes. A quit in the middle of one is no failure to say.
  powerMonitor.on("resume", keepChecked(updates, stopDelivery.signal, report));
}

// The update line's button: the update downloaded installed by the root helper, then the app restarted
// into it; or, installed already, the restart alone. Only while the line shows a button.
async function updateAction(): Promise<void> {
  if (!updates || !updateLine(updates.state)?.button) return;
  // Where the downloaded files were no longer the app's own, the release is looked for again: one
  // that cannot be found is said in the log, as a check's is.
  await updates.install().catch(report);
  if (updates.state.state === "installed") restart();
}

// The image's delivery started, once made: its manifest unreadable is a delivery that failed, so a
// packaged app never boots the repository's image in its place. *check*: as Retry asks.
function startDelivery(check = false): void {
  try {
    delivery ??= imageDelivery();
    undeliverable = null;
    delivery?.start(check);
  } catch (error) {
    undeliverable = error instanceof Error ? error.message : String(error);
  }
}

// What the sandbox lacks of this computer, looked for: at the app's start, and at the line's Check
// again. The file helper's tools first, then the VM's; zstd counts only while the image's delivery
// has something left to unpack.
function lookForTools(): void {
  const unpacking = delivery !== null && delivery.state.state !== "ready";
  // The folders are taken once, for the VM's tools and for the file helper's: the two looks then
  // hold the same folders, and the line and a file tool name the same tools.
  const held = boundFolders();
  vmLacking = held.then((folders) => missingTools({}, unpacking, folders));
  lookForFileTools(true, held);
}

// Each folder this computer's chats are bound to, as its file host holds it: by what it is now. One
// that is not there or cannot be read has no file host, and none starts once the journal is closed.
// Never looked at on this thread: a folder on a mount that has stopped answering would stop the app
// with it. One that does not answer in time is not held (FolderLooks).
const folderLooks = new FolderLooks(undefined, undefined, (folder) => report(new Error(`${folder}, a folder a chat is bound to, did not answer in ${LOOK_MS / 1000} s: its tools are not looked for, and its chat's commands will fail until it answers`)));
function boundFolders(): Promise<Array<{ dev: number; ino: number }>> {
  let folders: string[] = [];
  try {
    folders = openStack()?.bindings.folders() ?? [];
  } catch {
    // The device is stopping.
  }
  return folderLooks.held(folders);
}

// The file helper's tools, looked for again beside what the VM was last found to lack: with each
// look, once the device's folders are known, and at each change of them. They are looked for where
// the folders' file hosts look, by the hosts' own rule (pathOutside): on the app's PATH without its
// relative entries, and without any entry in a folder a chat is bound to, where a command may have
// written a program. So the line and a file tool's answer name the same tools. *said*: tell the
// pages even when nothing changed, as Check again asks.
let toolsOwedSaid = false;
function lookForFileTools(said = false, folders = boundFolders()): void {
  toolsOwedSaid ||= said;
  const look: Promise<string[]> = Promise.all([vmLacking, folders]).then(([vm, held]) => {
    const found = [...fileToolsMissing(BWRAP, pathOutside(process.env.PATH, held)), ...vm];
    // A look that a later one has overtaken says nothing: the later one's folders are the newer,
    // and it tells the pages what this one owed them.
    if (lackingFound !== look) return lackingFound;
    const same = lacking !== null && lacking.join("\n") === found.join("\n");
    lacking = found;
    if (toolsOwedSaid || !same) changed();
    toolsOwedSaid = false;
    return found;
  });
  lackingFound = look;
}

// Resolves once the VM can boot: it lacks nothing of this computer, and its image is here.
async function vmReady(signal: AbortSignal): Promise<void> {
  const found = await lackingFound;
  if (found.length > 0) throw new Error(`cannot start: ${toolsMissing(found)}`);
  if (undeliverable !== null) throw new Error(`could not be downloaded: ${undeliverable}`);
  try {
    await delivery?.wait(signal);
  } catch (error) {
    if (signal.aborted) throw error;
    throw new Error(`could not be downloaded: ${error instanceof Error ? error.message : String(error)}`);
  }
}

// The app's one VM, shared by every device, for this computer's user, and the background processes
// alive in it, which Settings shows with Stop as they change.
let vm: VmClient | null = null;
const alive = new LiveProcesses();
const vmFor = (): VmClient => {
  if (vm) return vm;
  vm = new VmClient({
    vm: vmOptions(root, vmUser(), VM_ENV, { image: delivery?.folder, agentDisk: VM_RESOURCES ? join(VM_RESOURCES, "agent.img") : undefined }),
    ready: vmReady,
    spawn: utilityManager,
  });
  // The status line follows each boot, and the first on this version's image lets the older ones go.
  vm.onBoot((told) => {
    boot = told;
    if ("emulated" in told) delivery?.prune();
    changed();
  });
  vm.onProcesses((processRoot, change) => {
    alive.heard(processRoot, change);
    main?.settingsContents()?.send("settings:changed");
  });
  return vm;
};

// The status line's buttons, each only while the line shows it: Show log of a boot that did not
// start; Check again for the tools; Retry the image's download, or, after a boot of the delivered
// image did not start, its check by its hashes, and the next boot's line is the next boot's.
function sandboxAction(action: unknown): void {
  if (!sandboxLine(lacking, deliveryState(), boot).actions.includes(action as SandboxAction)) return;
  if (action === "log") return void shell.openPath(vmOptions(root, vmUser(), VM_ENV).console);
  if (action === "check") return lookForTools();
  // The manager forgets that boot too, so the line never says Ready while it still refuses.
  boot = null;
  vm?.retry();
  startDelivery(true);
  changed();
}

function bounds(value: unknown): Bounds {
  const { x, y, width, height } = (value ?? {}) as Partial<Bounds>;
  const sides = [x, y, width, height];
  if (!sides.every((side) => typeof side === "number" && Number.isFinite(side) && side >= 0)) throw new Error("Not a place");
  return { x: Math.round(x!), y: Math.round(y!), width: Math.round(width!), height: Math.round(height!) };
}

// What the window's page and an open Settings show changed: each reads its state again.
function changed(): void {
  followAgent();
  updateTray();
  // The web client shows only while someone is signed in to the app, and has its session.
  main?.gate(signedIn === null || reloading);
  main?.window.webContents.send("shell:changed");
  main?.settingsContents()?.send("settings:changed");
}

const appearanceNow = () => ({ ...appearance.get(), theme: theme.dark ? ("dark" as const) : ("light" as const) });

// The web client hears every change of how the app looks: the theme in effect, and the transcript's settings.
// A thread read in the pane is read again in the transcript's settings, which its address carries.
function tellAppearance(): void {
  main?.webContents()?.send("desktop:appearance", appearanceNow());
  if (reading) main?.read(transcriptPath(reading.id));
}

// A thread's transcript, as the pane reads it: the web client's transcript page, in the transcript's settings.
function transcriptPath(threadId: string): string {
  const { textSize, transcriptWidth, motion } = appearance.get();
  return `/transcript/${threadId}?${new URLSearchParams({ textSize, transcriptWidth, motion })}`;
}

/**
 * Read *threadId*, a thread of the open project, in the Overview pane, beside the project's
 * conversation (Section 12); null closes it. One that has left the pane since it was drawn is said
 * so, and so is one whose id is no chat's.
 */
function read(threadId: string | null): void {
  const thread = view.kind === "project" && overview?.project.id === view.id
    ? overview.threads.find((found) => found.id === threadId) : undefined;
  if (threadId !== null && (!thread || !webClientPath(`/chat/${thread.id}`))) {
    failure = NO_SUCH_THREAD;
    return changed();
  }
  reading = thread ? { id: thread.id, title: thread.title } : null;
  // Read, it draws away the refusal of an earlier one: no choice of what the centre shows, so a project opening goes on.
  if (thread) failure = null;
  main?.read(reading ? transcriptPath(reading.id) : null);
  changed();
}

const links = () => linksFor(agents.get());

function openLink(which: unknown): void {
  const known = links();
  const url = typeof which === "string" && Object.hasOwn(known, which) ? known[which] : undefined;
  if (!url) throw new Error("No such link");
  void shell.openExternal(url);
}

let refreshing = false;
// What the next refresh reads: null for everything, or the threads whose rows changed.
let wanted = new Set<string | null>();
// The last pass failed, and lost what it was asked: the next one reads everything.
let stale = false;

// The projects the page serves, asked again: everything when it registers its source, when the
// open project changes, when the window comes to the front and when the project's stream says
// so; only a thread's row when the stream names the thread. One refresh runs at a time, and asks
// once more for whatever changed meanwhile, so an older answer never lands last. The open
// project, once the page lists it no more, is left for a new chat.
async function refreshProjects(threadId: string | null = null): Promise<void> {
  if (!served) return;
  wanted.add(threadId);
  if (refreshing) return;
  refreshing = true;
  try {
    while (wanted.size > 0 && served) {
      const asked = wanted;
      wanted = new Set();
      if (stale) asked.add(null);
      try {
        // The list is one count per project: a thread's change moves the project's counts too.
        listed = await projects.list();
        const open = view.kind === "project" ? view : null;
        if (open && !listed.some((project) => project.id === open.id)) {
          // Archived elsewhere: its conversation or thread goes from the centre with it, as the dialog's archive takes it.
          overview = null;
          show({ kind: "web" });
          main?.go("/chat");
        } else if (asked.has(null) || overview?.project.id !== open?.id) {
          await refreshOverview();
        } else {
          for (const id of asked) await refreshThread(id!);
        }
        stale = false;
      } catch (error) {
        stale = true;
        report(error);
      }
      changed();
    }
  } finally {
    refreshing = false;
  }
}

// The open project's threads, library and routines.
async function refreshOverview(): Promise<void> {
  const open = view.kind === "project" ? view : null;
  if (!open) return;
  const [project, threads, library, routines] = await Promise.all([
    projects.get(open.id), projects.threads(open.id), projects.library(open.id), projects.routines(open.id),
  ]);
  remember(project);
  if (view !== open) return;
  overview = { project, threads, library, routines };
  leaveIfGone(open);
}

// One thread's row of the open project, read alone: it keeps its place, a new one comes first,
// and one the project no longer has goes. A row new or gone, or whose files changed, changes the
// Library too, which is read again with it.
async function refreshThread(threadId: string): Promise<void> {
  const open = view.kind === "project" ? view : null;
  if (!open) return;
  const row = (await projects.threads(open.id, threadId)).find((found) => found.id === threadId);
  if (view !== open || overview?.project.id !== open.id) return;
  const before = overview.threads.find((found) => found.id === threadId);
  if (!row || !before || JSON.stringify(row.files) !== JSON.stringify(before.files)) {
    const library = await projects.library(open.id);
    if (view !== open || overview?.project.id !== open.id) return;
    overview = { ...overview, library };
  }
  overview = { ...overview, threads: merged(overview.threads, threadId, row) };
  leaveIfGone(open);
}

function merged(threads: ThreadRow[], threadId: string, row: ThreadRow | undefined): ThreadRow[] {
  if (!row) return threads.filter((found) => found.id !== threadId);
  return threads.some((found) => found.id === threadId) ? threads.map((found) => (found.id === threadId ? row : found)) : [row, ...threads];
}

// A thread open in the centre that has left the project takes the centre back to the project's conversation.
function leaveIfGone(open: View & { kind: "project" }): void {
  const path = overview ? `/chat/${overview.project.masterSessionId}` : "";
  if (open.thread && overview && !overview.threads.some((thread) => thread.id === open.thread?.id) && webClientPath(path)) {
    view = { ...open, thread: null };
    main?.go(path);
  }
}

// Follow the open project's changes, as Section 12's stream tells them, once more after the page registers again.
function follow(): void {
  unfollow();
  unfollow = view.kind === "project" ? projects.subscribe(view.id, (threadId) => void refreshProjects(threadId)) : () => {};
}

// The page withdrew its projects (signed out), or went: nothing is asked of it until it registers
// again. Signed out, the open project goes too.
function withdrawProjects(signedOut: boolean): void {
  if (served) serving = Promise.withResolvers();
  served = false;
  projects.withdrawn();
  if (signedOut) forgetAccount();
  changed();
}

// The account's own: its projects, the masters its page named, the open project and its pane, and
// why what it last asked for did not happen.
function forgetAccount(): void {
  listed = [];
  failure = null;
  // The project dialog shows one of its projects: it goes too, and nothing it sends reaches the next account's page.
  if (projectDialog && main?.settingsContents() === projectDialog) main.closeSettings();
  beforeProjects = { kind: "web" };
  masters.clear();
  overview = null;
  if (view.kind === "project") show({ kind: "web" });
}

/** Run *replaced* whenever *contents* replaces its page: a load that commits, or one that fails and draws its error page. A load the shell cancels replaces nothing. */
function onReplaced(contents: WebContents, replaced: () => void): () => void {
  const failed = (_event: unknown, code: number, _description: string, _url: string, isMainFrame: boolean) => {
    // -3 is a load another replaced, or one that was cancelled.
    if (isMainFrame && code !== -3) replaced();
  };
  contents.on("did-navigate", replaced);
  contents.on("did-fail-load", failed);
  return () => {
    contents.off("did-navigate", replaced);
    contents.off("did-fail-load", failed);
  };
}

function remember(project: Project): void {
  masters.set(project.masterSessionId, { id: project.id, name: project.name, masterSessionId: project.masterSessionId });
}

function show(next: View): void {
  // The pane's transcript is the open project's: anything else the centre shows closes it, its own thread included.
  if (reading && !(next.kind === "project" && view.kind === "project" && next.id === view.id && next.thread?.id !== reading.id)) {
    reading = null;
    main?.read(null);
  }
  view = next;
  follow();
  changed();
}

// A new choice of what the centre shows: what was chosen before, and its failure, are done with.
function choose(): number {
  failure = null;
  return ++choice;
}

/** Ask the page's projects once it serves them; a call cut off by the page going away is asked once more. */
async function askServed<T>(ask: () => Promise<T>): Promise<T> {
  for (let attempt = 1; ; attempt++) {
    if (!served) {
      const { promise, resolve, reject } = Promise.withResolvers<void>();
      const timer = setTimeout(() => reject(new Error("The agent's page did not serve its projects in time")), ANSWER_TIMEOUT_MS);
      void serving.promise.then(resolve);
      await promise.finally(() => clearTimeout(timer));
    }
    try {
      return await ask();
    } catch (error) {
      if (served || attempt === 2) throw error;
    }
  }
}

/**
 * The web client moved, by Back, Forward, a link or a chat of its own: the view follows it. A project
 * stays open while the client shows its master session or one of its threads; a master session the
 * page named opens its project; anything else is a page of the web client's own.
 */
function navigated(url: string): void {
  if (view.kind === "projects") return;
  const chat = /^\/chat\/([^/]+)$/.exec(new URL(url).pathname)?.[1];
  if (view.kind === "project") {
    if (chat === view.masterSessionId) {
      if (view.thread) show({ ...view, thread: null });
      return;
    }
    if (view.thread && chat === view.thread.id) return;
    const thread = overview?.project.id === view.id ? overview.threads.find((found) => found.id === chat) : undefined;
    if (thread) return show({ ...view, thread: { id: thread.id, title: thread.title } });
    // A thread the pane has not listed yet, as one just started from its card: its row decides. A page
    // that serves nothing yet, as after a full load, cannot answer soon: the chat is a page of its own.
    if (chat !== undefined && !masters.has(chat)) return served ? void openedThread(view, chat) : show({ kind: "web" });
  }
  const known = chat === undefined ? undefined : masters.get(chat);
  if (!known && view.kind === "web") return;
  show(known ? { kind: "project", ...known, thread: null } : { kind: "web" });
  void refreshProjects();
}

// The web client went to *chat* while *open* was shown: a thread of the project, read alone, is
// shown with the project as the way back; anything else is a page of the web client's own.
async function openedThread(open: View & { kind: "project" }, chat: string): Promise<void> {
  let row: ThreadRow | undefined;
  try {
    row = (await projects.threads(open.id, chat)).find((found) => found.id === chat);
  } catch (error) {
    report(error);
  }
  if (view !== open) return;
  if (!row) return show({ kind: "web" });
  if (overview?.project.id === open.id) overview = { ...overview, threads: merged(overview.threads, chat, row) };
  show({ ...open, thread: { id: row.id, title: row.title } });
}

/**
 * Start the device for *credential*. It counts as this computer's device at once, so a page
 * asking meanwhile does not register a second device. A start that fails leaves no device.
 */
function startStack(agent: Agent, credential: LiveCredential): Promise<DeviceStack> {
  const started = Promise.resolve().then(() => startDevice({
    journalPath: join(root, "devices", credential.deviceId, "journal.sqlite"),
    url: linkUrl(agent.origin),
    token: credential.token,
    agent: agent.name,
    identity: { deviceId: credential.deviceId, orgId: credential.orgId, agentId: credential.agentId, userId: credential.userId },
    // The tool layer under the binder: the file kinds in the root's file host, the process kinds in the
    // VM, and the browser's kinds in this identity's browser host, with the browser Settings chose.
    tools: (bindings, network, changed) => new Browsing({
      tools: appTools({ bindingOf: (bound) => bindings.get(bound), network, dataDir: root, cacheDir: join(cacheHome, "surogate"), env, vm: vmFor(), changed }),
      browser: new BrowserClient(utilityBrowser(profilesOf(root, credential))),
      staging: browserTemp(profilesOf(root, credential)),
      bindingOf: (bound) => bindings.get(bound),
      launch: () => {
        const browser = chosenBrowser(browserSetting.get(), findBrowsers());
        return browser && { executable: browser.executable, profile: profileOf(root, credential, browser) };
      },
      // A chat's own servers its user let the browser open: the journal's ports, carried into the chat's sandbox
      // by the VM, which also says whether one listens on a port.
      ports: () => bindings.forwards(),
      vm: vmFor(),
    }),
    prompts,
    approvalPrompts: prompts,
    // The page hears which of this account's chats changed on this computer, while it is this account's page.
    onBindingChanged: (root) => {
      // Settings → Folders and permissions draws the chat's folder, mode and hosts again.
      main?.settingsContents()?.send("settings:changed");
      if (pageIs(credential)) main?.webContents()?.send("desktop:binding-changed", root);
      // A folder bound or forgotten changes where the file hosts look for their tools.
      lookForFileTools();
    },
    onStatus: (status) => {
      if (device?.credential === credential) device.status = status;
      // The agent ended this token while it is this computer's: cleaned up here, its folders kept for a restore.
      if ((status === "revoked" || status === "unauthenticated") && kept === credential && rotating !== credential) void retire(credential);
      changed();
    },
    onWorking: (count) => {
      // A quit told to wait for the threads goes on once none works, and says meanwhile how many are left.
      if (count === 0) waiting?.();
      else if (waiting) changed();
    },
    onError: report,
  }));
  const starting = { credential, status: "connecting" as LinkStatus, stack: null as DeviceStack | null, started };
  device = starting;
  changed();
  return started.then((stack) => {
    starting.stack = stack;
    // This device's folders are known now: the ones its chats were bound to before it started.
    lookForFileTools();
    return stack;
  }, (error: unknown) => {
    if (device === starting) device = null;
    changed();
    throw error;
  });
}

// The window's web client forgets whoever was signed in there: its storage, cookies and caches.
async function clearWindow(agent: Agent): Promise<void> {
  const partition = session.fromPartition(partitionFor(agent.origin, agent.agentId));
  await partition.clearStorageData();
  await partition.clearCache();
}

// The app's sign-in, from now on. *first* holds the tokens of a sign-in that just happened.
function startSession(remembered: SignedIn, first?: Tokens): void {
  signedIn = new DesktopSession(remembered, {
    store: sessionStore,
    fetch: apiFetch,
    // The agent ended the sign-in: the window asks for a new one, and says why. The device stays, on a token of its own.
    onEnded: (why) => {
      signedIn = null;
      signInFailure = why;
      changed();
    },
  }, first);
}

/** Add this computer to the agent for the signed-in user, when the agent has local folders and none is kept. */
async function registerComputer(agent: Agent): Promise<void> {
  const session = signedIn;
  if (!session || kept || registering || !agent.desktopSessions || !agent.multiSession) return;
  registering = true;
  signInAgain = false;
  try {
    const added = await register({
      agent, session, computer: hostname(),
      verify: (token) => verifyDevice(linkUrl(agent.origin), token),
      start: (credential) => startStack(agent, credential),
      save: (credential) => credentials.save(credential),
    });
    if (added === "sign-in-again") signInAgain = true;
    // Logged out while it was being added: the device it made goes too.
    else if (signedIn !== session) await endDevice(added);
    else kept = added;
  } catch (error) {
    // Nothing was kept: no device runs for it.
    device = null;
    report(error);
  } finally {
    registering = false;
    changed();
  }
}

/**
 * Keep the token the agent just issued this computer, its only copy, and run the device on it. The
 * sign-in is bound by then, so nothing here ends it: a link that cannot connect shows offline and
 * retries as any link does, and a device that cannot start is reported, as at launch.
 */
async function keepAndStart(agent: Agent, renewed: LiveCredential): Promise<void> {
  kept = renewed;
  try {
    credentials.save(renewed);
  } catch (error) {
    // Still kept in memory: the device runs on it until the app quits.
    report(error);
  }
  // The device on the old token goes first: the new one keeps the same journal.
  await stopDevice(device?.started, null).catch(report);
  device = null;
  await startStack(agent, renewed).catch(report);
}

/**
 * Bind the sign-in that just happened to the computer kept for its account, before anything uses it:
 * revoking the computer then ends it too, and the window's session made from it. The device starts
 * again on the new token the agent issues for it. A sign-in the agent did not bind is ended rather
 * than run unbound; once it is bound, it stays, whatever the device does. Once *signal* aborts, nothing
 * is reauthorized. A token the agent issued all the same revokes the computer when a log out aborted
 * it; after a quit, it is kept, the only one the agent takes, and the device starts on it at the next launch.
 */
async function bindToComputer(agent: Agent, signal: AbortSignal): Promise<void> {
  const session = signedIn;
  const credential = kept;
  // A revoked computer stays unbound until the user restores it, which binds the sign-in it uses.
  if (!session || !live(credential) || credential.orgId !== session.account.orgId || credential.userId !== session.account.userId) return;
  rotating = credential;
  try {
    let renewed: LiveCredential | null;
    try {
      renewed = await rebind({ session, credential }, signal);
    } catch (error) {
      if (signedIn === session) signedIn = null;
      await session.end().catch(report);
      throw new Error(`Surogate could not tie this sign-in to this computer, so it signed out: ${error instanceof Error ? error.message : String(error)}`);
    }
    if (!renewed) {
      // The agent lists the computer revoked, or has it no more: it is cleaned up here, as its link would be told.
      if (!signal.aborted) void retire(credential);
      return;
    }
    if (signal.reason === LOG_OUT) {
      // Logged out meanwhile: the computer goes with the log out, revoked with the token just issued, its only one.
      await stopDevice(device?.started, null).catch(report);
      await endDevice(renewed);
    } else if (signal.reason === QUIT) {
      kept = renewed;
      trying(() => credentials.save(renewed));
    } else {
      await keepAndStart(agent, renewed);
    }
  } finally {
    rotating = null;
  }
}

/**
 * The agent's capabilities, read again: a server that gained local folders since it was added now
 * offers them, and the console it names now is the one the links open. Kept while the agent is the
 * one added; anything else is said and changes nothing.
 */
async function refreshAgent(agent: Agent): Promise<void> {
  try {
    const fresh = await readAgent(agent.origin, getFollowing);
    if (fresh.origin !== agent.origin || fresh.agentId !== agent.agentId) {
      report(new Error(`${agent.origin} now answers as agent ${fresh.agentId} at ${fresh.origin}: Surogate keeps the agent it added`));
      return;
    }
    const { desktopSessions, multiSession, consoleUrl } = fresh;
    if (desktopSessions === agent.desktopSessions && multiSession === agent.multiSession && consoleUrl === agent.consoleUrl) return;
    // Removed while it was asked: it is not kept again.
    const now = agents.get();
    if (now?.origin !== agent.origin || now.agentId !== agent.agentId) return;
    // In place: the bridge and the device hold this agent.
    Object.assign(agent, { desktopSessions, multiSession, consoleUrl });
    agents.set(agent);
    changed();
    void registerComputer(agent);
  } catch (error) {
    report(error);
  }
}

/** Revoke *credential*'s device on a link of its own, with the link's backoff, until the agent hears it; then forget it. */
function revokeLater(credential: Credential): void {
  if (revocations.has(credential.deviceId) || credential.token === null) return;
  const revoking = revokeDevice(linkUrl(credential.origin), credential.token);
  revocations.set(credential.deviceId, revoking);
  void revoking.done.then(() => {
    revocations.delete(credential.deviceId);
    trying(() => credentials.remove(credential.deviceId));
  });
}

/**
 * End this computer's access for *credential*'s user (spec, Section 7): what runs stops at once,
 * the agent revokes the device (later, on a link of its own, when it cannot hear it now), and
 * the folders it was given here go with its journal.
 */
async function endDevice(credential: Credential): Promise<void> {
  const ending = device?.credential === credential ? device : null;
  device = null;
  kept = null;
  // Owed before anything stops: a quit or a crash from here on still revokes it, at the next launch.
  if (credential.token !== null) trying(() => credentials.save({ ...credential, revoking: true }));
  const stack = credential.token === null ? null : await ending?.started.catch(() => null);
  // A token the agent already ended needs no revoking. A stop that fails still ends access here: the
  // revocation is then owed, as when the agent could not hear it.
  if (credential.token === null || (stack && await stack.revoke().catch((error: unknown) => {
    report(error);
    return false;
  }))) {
    trying(() => credentials.remove(credential.deviceId));
  } else {
    // Kept in memory too, so it is revoked during this run even when it could not be saved.
    revokeLater(credential);
  }
  // A revoked device still being cleaned up has its journal open.
  await retiring;
  trying(() => rmSync(join(root, "devices", credential.deviceId), { recursive: true, force: true }));
  // What was read and heard of its chats goes with them: their titles, and the background processes they ran.
  titles.clear();
  reads.clear();
  alive.clear();
}

// The app's native message boxes up now. While one is, a link waits, the first that comes; it opens
// once the last is answered, and after what answered it, a quit's included, has acted on the answer.
let boxes = 0;
let linkWaiting: OpenLink | null = null;

/** A native message box, over *parent* when there is one: the button pressed. */
async function messageBox(options: Electron.MessageBoxOptions, parent: BrowserWindow | undefined = main?.window): Promise<number> {
  return (await messageBoxResult(options, parent)).response;
}

/** A native message box, over *parent* when there is one: the button pressed, and whether its checkbox was ticked. */
async function messageBoxResult(options: Electron.MessageBoxOptions, parent: BrowserWindow | undefined = main?.window): Promise<Electron.MessageBoxReturnValue> {
  boxes += 1;
  try {
    return parent ? await dialog.showMessageBox(parent, options) : await dialog.showMessageBox(options);
  } finally {
    boxes -= 1;
    const link = boxes === 0 ? linkWaiting : null;
    if (link) {
      linkWaiting = null;
      setTimeout(() => void openDeepLink(link).catch(report), 0);
    }
  }
}

async function ask(options: Electron.MessageBoxOptions): Promise<boolean> {
  return (await messageBox(options)) === 0;
}

// "1 thread working on this computer stops." when one works: what a log out cuts off.
function cutOff(): string {
  const working = device?.stack?.working() ?? 0;
  if (working === 0) return "";
  return working === 1 ? " 1 thread working on this computer stops." : ` ${working} threads working on this computer stop.`;
}

/**
 * Log out of the agent, or remove it (spec, Section 7), once the user confirms: this computer's
 * access first, then the app's sign-in, then whatever the window held. Removing also forgets
 * the agent, and the app starts over at its first run.
 */
async function signOut(agent: Agent, removing: boolean): Promise<void> {
  if (signingOut) return;
  const options: Electron.MessageBoxOptions = removing
    ? {
      type: "warning", message: `Remove ${agent.name} from Surogate?`,
      detail: `You are logged out, this computer's access to ${agent.name} ends, and the folders it was given here are forgotten. Surogate then asks for an agent again.${cutOff()}`,
      buttons: ["Remove", "Cancel"], defaultId: 1, cancelId: 1, noLink: true,
    }
    : {
      type: "warning", message: `Log out of ${agent.name}?`,
      detail: `This computer's access to ${agent.name} ends, and the folders it was given here are forgotten.${cutOff()}`,
      buttons: ["Log out", "Cancel"], defaultId: 1, cancelId: 1, noLink: true,
    };
  // Whose browser profiles they are, before the log out forgets the computer's credential: none
  // when this computer keeps no access to the agent, or its browser never ran here.
  const profiles = kept && existsSync(profilesOf(root, kept)) ? profilesOf(root, kept) : null;
  // What the agent's browser here is signed in to is the user's to keep or not (spec, Section 7), with a window or without,
  // asked only where there is something to forget.
  const asked = profiles
    ? { ...options, checkboxLabel: `Also forget the sites ${agent.name}'s browser on this computer is signed in to`, checkboxChecked: removing }
    : options;
  const { response, checkboxChecked: forgetBrowser } = await messageBoxResult(asked);
  const confirmed = response === 0;
  if (!confirmed || signingOut) return;
  const { promise, resolve: done } = Promise.withResolvers<void>();
  signingOut = promise;
  try {
    // A sign-in or a Restore under way is cancelled, and stops first: one that finished afterwards
    // would sign the user back in, or bring the computer back.
    signingIn?.abort(LOG_OUT);
    restoringNow?.abort();
    await underWay;
    const ending = signedIn;
    signedIn = null;
    signInAgain = false;
    sessionStore.clear();
    if (kept) await endDevice(kept);
    // The browser closed with the device: its profiles can go now.
    if (forgetBrowser && profiles) trying(() => rmSync(profiles, { recursive: true, force: true, maxRetries: 3 }));
    // Ended at the agent too, best effort: offline, the refresh token stays valid there until it expires.
    void ending?.end().catch(report);
    account = null;
    forgetAccount();
    // What the agent told the user who logged out opens nothing more.
    notifications?.closeAll();
    await clearWindow(agent);
    if (removing) {
      agents.clear();
      main?.detach();
      withdrawProjects(true);
    } else {
      main?.go("/");
    }
  } finally {
    signingOut = null;
    done();
    changed();
  }
}

/**
 * The agent revoked this computer, or no longer knows its token (spec, Section 8): the token goes,
 * the device stops, and its journal keeps nothing to send or run. The identity and its folders
 * stay, for a restore by the same user.
 */
function retire(credential: Credential): Promise<void> {
  const retired: Credential = { ...credential, token: null };
  kept = retired;
  restoreFailure = null;
  // A save that fails still stops the device: the token is gone at the agent either way.
  trying(() => credentials.save(retired));
  const ending = device?.credential === credential ? device : null;
  retiring = (async () => {
    const stack = await ending?.started.catch(() => null);
    await stack?.retire();
  })().catch(report);
  return retiring;
}

// The folders a device's chats are bound to, from its journal, which nothing else holds open.
function foldersOf(credential: Credential): string[] {
  const journal = new OperationJournal(join(root, "devices", credential.deviceId, "journal.sqlite"));
  try {
    return journal.bindings.folders();
  } finally {
    journal.close();
  }
}

/**
 * Restore the revoked computer's access for the same user, once they confirm its folders natively
 * (spec, Section 8). It needs a recent sign-in, as adding does: the browser asks for one first.
 */
async function restore(agent: Agent): Promise<void> {
  const credential = kept;
  if (restoring || !credential || credential.token !== null || !signedIn || signingOut) return;
  // What a log out cancels, and waits for, as it does a sign-in.
  const cancel = new AbortController();
  restoringNow = cancel;
  const { promise: stopped, resolve: stop } = Promise.withResolvers<void>();
  underWay = Promise.all([underWay, stopped]);
  restoring = true;
  restoreFailure = null;
  changed();
  try {
    await retiring;
    const folders = foldersOf(credential);
    const confirmed = await ask({
      type: "question",
      message: `Restore local access on ${credential.name}?`,
      detail: folders.length > 0
        ? `${agent.name} revoked this computer's access. Restoring it lets the chats on these folders work here again:\n${folders.map((folder) => `• ${folder}`).join("\n")}\nWork that was cancelled stays cancelled.`
        : `${agent.name} revoked this computer's access. Restoring it lets ${agent.name} work on folders of this computer again.`,
      buttons: ["Restore access", "Cancel"], defaultId: 0, cancelId: 1, noLink: true,
    });
    // Signed out while the user was asked: nothing to restore with.
    if (!confirmed || !signedIn || cancel.signal.aborted) return;
    if (!signedIn.recent()) await signIn(agent);
    const session = signedIn;
    // The sign-in did not finish or was cancelled, another account signed in, which ended this device, or a log out began.
    if (!session?.recent() || kept !== credential || cancel.signal.aborted) return;
    const restored = await reauthorize({ session, credential });
    if (cancel.signal.aborted) {
      // Logged out meanwhile: nothing of it is kept, and a token the agent issued all the same revokes the computer.
      if (typeof restored === "object") await endDevice(restored);
    } else if (restored === "gone") {
      // The agent has no such device any more: its folders here go too, and this computer is added afresh.
      kept = null;
      device = null;
      trying(() => credentials.remove(credential.deviceId));
      trying(() => rmSync(join(root, "devices", credential.deviceId), { recursive: true, force: true }));
      await registerComputer(agent);
    } else if (restored === "sign-in-again") {
      restoreFailure = "The agent wants a newer sign-in: sign in again, then restore";
    } else {
      await keepAndStart(agent, restored);
    }
  } catch (error) {
    // The agent did not restore it: nothing was issued, and nothing started.
    restoreFailure = error instanceof Error ? error.message : String(error);
  } finally {
    restoring = false;
    if (restoringNow === cancel) restoringNow = null;
    stop();
    changed();
  }
}

/** Sign in to the agent in the system browser. A sign-in started again cancels the one under way. */
async function signIn(agent: Agent): Promise<void> {
  // No sign-in starts while a log out runs: it would sign the user back in.
  if (signingOut) return;
  signingIn?.abort();
  const attempt = new AbortController();
  signingIn = attempt;
  const { promise: stopped, resolve: stop } = Promise.withResolvers<void>();
  underWay = Promise.all([underWay, stopped]);
  signedInOrStopped = stopped;
  signInFailure = null;
  changed();
  // The refresh token of a sign-in no session keeps yet: one that fails is ended at the agent, as nothing here would.
  let unkept: string | null = null;
  try {
    const tokens = await signInWithBrowser({
      origin: agent.origin, computer: hostname(), fetch: apiFetch, signal: attempt.signal, open: (url) => shell.openExternal(url),
    });
    unkept = tokens.refreshToken;
    let started: DesktopSession | null = null;
    // Cancelled once the browser came back: it goes no further. Tokens never kept are ended at the
    // agent, as nothing here would end them. A sign-in already kept ends only with a log out: a quit
    // leaves it saved, and a sign-in started again replaces it once it finishes.
    const cancelled = (): boolean => {
      if (!attempt.signal.aborted) return false;
      if (started === null) {
        void revokeTokens(agent.origin, tokens.refreshToken, apiFetch).catch(report);
      } else if (attempt.signal.reason === LOG_OUT && signedIn === started) {
        signedIn = null;
        void started.end().catch(report);
      }
      return true;
    };
    const who = await accountOf(agent.origin, tokens.accessToken, apiFetch);
    if (cancelled()) return;
    // This computer works for one account at a time: another's access here ends first, once the user agrees.
    const previous = kept;
    if (previous && (previous.orgId !== who.orgId || previous.userId !== who.userId)) {
      const switching = await ask({
        type: "warning", message: `Sign in as ${who.email}?`,
        detail: `This computer works for another account of ${agent.name}. Signing in as ${who.email} ends that account's access here, and forgets the folders it was given.${cutOff()}`,
        buttons: ["Sign in", "Cancel"], defaultId: 1, cancelId: 1, noLink: true,
      });
      if (cancelled()) return;
      if (!switching) {
        void revokeTokens(agent.origin, tokens.refreshToken, apiFetch).catch(report);
        signInFailure = `Not signed in as ${who.email}: this computer keeps working for the account that added it`;
        return;
      }
      await endDevice(previous);
      if (cancelled()) return;
    }
    // What the agent told another account here opens nothing more: its notices go with its sign-in.
    const before = signedIn?.account ?? previous;
    if (before && (before.orgId !== who.orgId || before.userId !== who.userId)) notifications?.closeAll();
    // The sign-in this one replaces ends at the agent: none is left valid with no copy here.
    void signedIn?.end().catch(report);
    const signedInNow: SignedIn = { origin: agent.origin, agentId: agent.agentId, account: who, authTime: tokens.authTime, refreshToken: tokens.refreshToken };
    sessionStore.save(signedInNow);
    startSession(signedInNow, tokens);
    started = signedIn;
    unkept = null;
    await bindToComputer(agent, attempt.signal);
    if (cancelled()) return;
    // The web client's session comes from this sign-in: whatever the window held before goes, and
    // the sign-in shows until the web client has loaded again.
    reloading = true;
    changed();
    try {
      await clearWindow(agent);
      await main?.go("/");
    } finally {
      reloading = false;
    }
    if (cancelled()) return;
    void registerComputer(agent);
    void refreshAgent(agent);
  } catch (error) {
    if (unkept !== null) void revokeTokens(agent.origin, unkept, apiFetch).catch(report);
    if (!(error instanceof OAuthError && error.code === "cancelled")) {
      signInFailure = error instanceof Error ? error.message : String(error);
    }
  } finally {
    if (signingIn === attempt) signingIn = null;
    stop();
    changed();
  }
}

// What a window asked for, a folder, Work freely or a hand back: its prompts go once that window goes or its page is replaced.
async function preparing<T>(window: string, prepare: (signal: AbortSignal) => Promise<T>): Promise<T> {
  const contents = webContents.fromId(Number(window));
  const controller = new AbortController();
  const gone = () => controller.abort();
  if (!contents) controller.abort();
  contents?.once("destroyed", gone);
  const stop = contents ? onReplaced(contents, gone) : () => {};
  try {
    return await prepare(controller.signal);
  } finally {
    contents?.off("destroyed", gone);
    stop();
  }
}

// The bridge, on a view of the agent's web client: its calls answer for this agent only.
function bridge(contents: WebContents, agent: Agent): void {
  // Each load of the page is a page of its own: what its user refused there holds until it is replaced.
  let load = 0;
  // The device is the account's it was registered for: a page of anyone else's, or of nobody's, sees none.
  const anotherAccount = () => kept !== null && !pageIs(kept);
  // The device, once started: a page asking while it still starts, as at a launch, waits for it.
  const registered = async (): Promise<DeviceStack> => {
    if (anotherAccount()) throw new Error("This computer is registered with the agent for another account");
    if (kept?.token === null) throw new Error(REVOKED);
    if (!device) throw new Error("This computer is not registered with the agent");
    return device.stack ?? device.started;
  };
  // The browser is one for every chat of the agent's here, held from the chat that took it over until that chat hands it back.
  const HELD_FROM_ANOTHER_CHAT = "The agent's browser on this computer is taken over from another chat, and is handed back there";
  // The device, for a call about a chat's browser: refused for a chat with no folder here.
  const browsing = async (sessionId: string): Promise<DeviceStack> => {
    const stack = await registered();
    if (!stack.bindings.get(sessionId)) throw new Error("This chat has no folder on this computer");
    return stack;
  };
  const handlers = bridgeHandlers(agent.origin, {
    getDevice: () => {
      // The capabilities as last read and kept: a sign-in's read reaches the file, not the agent this bridge was opened with.
      const now = agents.get();
      return {
        device: kept && !anotherAccount() ? { deviceId: kept.deviceId, name: kept.name } : null,
        // A revoked computer binds no folder until it is restored.
        localFolders: !anotherAccount() && live(kept) && now?.desktopSessions === true && now.multiSession,
      };
    },
    signOut: () => signOut(agent, false),
    // A one-time code for the web client's own session, from the app's sign-in.
    webSignIn: async () => {
      if (!signedIn) return null;
      const response = await signedIn.api("/api/v1/auth/oauth/web-code", { method: "POST" });
      if (!response.ok) throw new Error(`The agent did not give this window a session (HTTP ${response.status})`);
      const { code } = (await response.json()) as { code?: unknown };
      if (typeof code !== "string") throw new Error("The agent gave this window no session");
      return { code };
    },
    prepareFolder: (choice, window, thread) =>
      preparing(window, async (signal) => (await registered()).binder.prepareFolder(choice, window, signal, thread)),
    bindSession: async (sessionId, token, window) => {
      await (await registered()).binder.bindSession(sessionId, token, window);
    },
    setMode: async (sessionId, mode) => (await registered()).binder.approvals.setMode(sessionId, mode),
    requestFreeMode: (sessionId, window) =>
      preparing(window, async (signal) => (await registered()).binder.approvals.requestFreeMode(sessionId, signal, `${window}:${load}`)),
    cancelPrepared: async (token, window) => (await registered()).binder.cancelPrepared(token, window),
    getBinding: async (sessionId) => {
      const stack = await registered();
      const binding = stack.binder.bindingOf(sessionId);
      return binding && { ...binding, takenOver: stack.tools.takenOver?.(sessionId) ?? false };
    },
    // Shown selected in its parent, never opened: a file put at its path after the look is only selected, never run.
    revealFolder: async (sessionId) => shell.showItemInFolder(await (await registered()).binder.folderToShow(sessionId)),
    showBrowser: async (sessionId) => {
      const stack = await browsing(sessionId);
      if (!(await stack.tools.show?.(sessionId))) throw new Error("The agent's browser has no page open for this chat");
    },
    // The user drives the agent's browser from now on, held from this chat, and the page hears the change.
    // Another chat's take-over stands: this one does not end it.
    takeOver: async (sessionId) => {
      const stack = await browsing(sessionId);
      if (!stack.takeOver(sessionId)) throw new Error(HELD_FROM_ANOTHER_CHAT);
      contents.send("desktop:binding-changed", sessionId);
    },
    // Only at the desktop's own confirmation, in a prompt window of its own: its Hand back takes nothing until
    // the prompts' input protection has passed, so a press its user began for the page answers nothing there.
    // The page's preload asks for it only at its user's click, before they kept the browser and after: a page
    // cannot wear its user down.
    handBack: (sessionId, window) => preparing(window, async (signal) => {
      const stack = await browsing(sessionId);
      const held = stack.tools.takenOver?.(sessionId) ?? false;
      // Nobody holds it: the agent drives it already, and nothing was handed back by anyone.
      if (held === false) return "released";
      // Held from another chat that is here: handed back there, and nothing is asked here.
      if (held === "elsewhere") throw new Error(HELD_FROM_ANOTHER_CHAT);
      // Held from this chat, which the confirmation names by its title; or from one that is gone, which can
      // hand nothing back and is named no more: this chat's to hand back.
      const title = held === true ? await titleSoon(sessionId) : null;
      // The window first, where it was hidden since the click: the confirmation opens over it.
      main?.show();
      const asked = new AbortController();
      handBacks.add(asked);
      try {
        if (!(await prompts.confirmHandBack({ agent: agent.name, gone: held !== true, title }, AbortSignal.any([signal, asked.signal])))) return false;
        // Who holds it now, and not when the confirmation opened: it can have changed while it was up.
        const now = stack.tools.takenOver?.(sessionId) ?? false;
        // Whether anything was handed back: the browser may have been taken over from another chat while the
        // confirmation was up, and is that chat's to hand back then.
        if (!stack.handBack(sessionId)) return false;
        handedBack(asked);
        contents.send("desktop:binding-changed", sessionId);
        // Which hand back it was, for the page to tell the server: only of the browser this chat held is it the
        // one its user confirmed for this chat, which gives its agent a turn. Held from a chat that is gone,
        // their confirmation handed back nobody's hold.
        return now === true ? "confirmed" : "released";
      } finally {
        handBacks.delete(asked);
      }
    }),
    openSettings: async (section) => {
      // A project's dialog is over the window: Settings does not open over it.
      if (projectDialog !== null && main?.settingsContents() === projectDialog) {
        throw new Error("Surogate has a project's dialog open: close it to open Settings");
      }
      main?.show();
      showSettings(section);
    },
    getAppearance: appearanceNow,
    setAccount: (reported) => {
      // Another account, or none, or the first: nothing listed before is theirs. A page that
      // still serves its source answers for whoever is signed in now.
      const another = reported?.userId !== account?.userId;
      account = reported;
      if (another) {
        forgetAccount();
        void refreshProjects();
      }
      changed();
    },
    registerProjects: (registered) => {
      if (!registered) return withdrawProjects(true);
      served = true;
      serving.resolve();
      follow();
      void refreshProjects();
    },
  });
  for (const [name, handler] of Object.entries(handlers)) {
    contents.ipc.handle(`desktop:${name}`, (event, ...args: unknown[]) => handler(event.senderFrame, String(contents.id), ...args));
  }
  const fromView = (event: IpcMainEvent) => event.senderFrame?.parent === null && sameOrigin(agent.origin, event.senderFrame.url);
  contents.ipc.on("desktop:projects-answer", (event, id: unknown, outcome: unknown) => {
    if (fromView(event)) projects.answered(id, outcome);
  });
  contents.ipc.on("desktop:projects-changed", (event, id: unknown, threadId: unknown) => {
    if (fromView(event)) projects.changed(id, threadId);
  });
  // The page's word on a text it was handed, by the id it was handed with: its reason is text, bounded as every text of the page's is.
  contents.ipc.on("desktop:quick-entry-answer", (event, id: unknown, refused: unknown) => {
    if (fromView(event) && typeof id === "string") handed(id, typeof refused === "string" ? refused.slice(0, 1_000) : null);
  });
  // A page that loads again starts with no source, until it registers one. Until its load commits,
  // the page there still serves: a load the shell cancels, as to an address outside the agent's, changes nothing.
  onReplaced(contents, () => {
    load += 1;
    if (served) withdrawProjects(false);
    // A text quick entry handed the page that went is not sent: its box says so.
    if (handing) handed(handing.id, LEFT_CHAT);
  });
  // Nor is one handed a page whose view went with no other page in its place, as removing the agent takes it.
  contents.once("destroyed", () => {
    if (handing) handed(handing.id, LEFT_CHAT);
  });
}

function open(window: MainWindow, agent: Agent): void {
  const contents = window.attach(agent, BRIDGE_PRELOAD);
  bridge(contents, agent);
  contents.on("did-navigate", (_event, url) => navigated(url));
  contents.on("did-navigate-in-page", (_event, url, isMainFrame) => {
    if (isMainFrame) navigated(url);
  });
  // The chat it shows, followed for the end of its turns while the window is away.
  contents.on("did-navigate", (_event, url) => showing(url));
  contents.on("did-navigate-in-page", (_event, url, isMainFrame) => {
    if (isMainFrame) showing(url);
  });
}

// A link being opened: one that comes meanwhile only shows the window.
let linking = false;
// A link handed before the window was made.
let linkEarly: OpenLink | null = null;

/** Connect to the agent at *address*, once the user confirms it natively: why it did not, or null. One connection at a time. */
async function connectTo(address: string): Promise<string | null> {
  if (agents.get()) return "This app is already connected to an agent";
  if (connecting) return "Surogate is already connecting";
  connecting = true;
  try {
    const agent = await connectAgent(address, { get: getFollowing, store: agents, confirm: confirmAgent });
    if (agent && main) open(main, agent);
    changed();
    return null;
  } catch (error) {
    return error instanceof Error ? error.message : String(error);
  } finally {
    connecting = false;
  }
}

/**
 * Open a surogate:// link the system handed the app, in its window: the agent it names, at the page
 * it names. An agent new to the app is connected to once the user confirms it, as the first run's
 * Connect is; another agent than the one added is said, and opened not: the app works for one agent.
 */
async function openDeepLink(link: OpenLink): Promise<void> {
  if (leaving) return;
  // A second launch can hand one while the app still starts: it is opened once the window is there.
  if (!main) {
    linkEarly ??= link;
    return;
  }
  main.show();
  // One link at a time: one that comes while a link, or the first run's Connect, is asked only shows the window.
  if (linking || connecting) return;
  if (boxes > 0) {
    linkWaiting ??= link;
    return;
  }
  linking = true;
  try {
    const agent = agents.get();
    const host = new URL(link.origin).host;
    if (!agent) {
      // A link any web page can make: nothing is asked of its address, or of where that sends Surogate, before the user says so.
      const continued = await ask({
        type: "question", message: `Open a link to connect to ${host}?`,
        detail: `A link asks Surogate to connect to ${host}. Surogate contacts it only if you continue, and asks again before this computer joins it.`,
        buttons: ["Continue", "Cancel"], defaultId: 1, cancelId: 1, noLink: true,
      });
      if (!continued) return;
      const refused = await connectTo(link.origin);
      if (refused) await ask({ type: "warning", message: `Surogate did not connect to ${host}`, detail: refused, buttons: ["OK"], noLink: true });
    } else if (agent.origin !== link.origin) {
      await ask({
        type: "info", message: `Surogate works for ${agent.name}`,
        detail: `This link is for ${host}. Remove ${agent.name} from Surogate to connect to another agent.`, buttons: ["OK"], noLink: true,
      });
    } else if (link.path !== "/") {
      goWeb(link.path);
    }
  } finally {
    linking = false;
  }
}

async function confirmAgent(agent: Agent, typed: string): Promise<boolean> {
  const redirected = typed === agent.origin ? "" : `${typed} sent Surogate to ${agent.origin}. `;
  const options = {
    type: "question" as const,
    message: `Connect to ${agent.name}?`,
    detail: `${redirected}You sign in to ${agent.origin} next, and this computer joins it. Connect only to servers you trust.`,
    buttons: ["Connect", "Cancel"],
    defaultId: 0,
    cancelId: 1,
    noLink: true,
  };
  return (await messageBox(options)) === 0;
}

// What the user can do about this computer, as the sidebar offers it.
function deviceAction(agent: Agent | null): { text: string; button: string; action: "sign-in" | "restore" } | null {
  if (!agent || !signedIn) return null;
  if (kept?.token === null) {
    if (restoring) return { text: "Restoring local access…", button: "", action: "restore" };
    return { text: restoreFailure ? `Local access revoked. ${restoreFailure}.` : "Local access revoked.", button: "Restore…", action: "restore" };
  }
  if (signInAgain) return { text: `Sign in again to let ${agent.name} work on folders of this computer.`, button: "Sign in again", action: "sign-in" };
  return null;
}

// The device's line: its link's status, or revoked for a kept computer the agent ended.
function deviceLine(agent: Agent): string {
  if (device) return describeAgent(agent, { status: device.status, computer: device.credential.name });
  return describeAgent(agent, kept?.token === null ? { status: "revoked", computer: kept.name } : null);
}

// Who the sidebar names: whose the page is (pageOwner), with the app's own sign-in's organisation, which is Settings' alone, left out.
function sidebarAccount(): DesktopAccount | null {
  const owner: SignedInAccount | null = pageOwner();
  if (owner === null) return null;
  const { orgName: _, ...shown } = owner;
  return shown;
}

function state() {
  const agent = agents.get();
  return {
    first: agent === null,
    agent: agent && { name: agent.name, desktopSessions: agent.desktopSessions },
    device: agent && {
      text: deviceLine(agent),
      status: device?.status ?? (kept?.token === null ? "revoked" : null),
    },
    account: sidebarAccount(),
    view,
    overview: view.kind === "project" && overview?.project.id === view.id ? overview : null,
    reading,
    projects: listed,
    failure,
    links: Object.keys(links()),
    unreachable: main?.unreachable ?? null,
    notice: credentials.unencrypted() || sessionStore.unencrypted()
      ? "Credentials on this computer are not encrypted: Linux has no secret store here" : null,
    signIn: { needed: agent !== null && (signedIn === null || reloading), pending: signingIn !== null, failure: signInFailure },
    deviceAction: deviceAction(agent),
    // While a quit waits for the threads working on this computer: how many it waits for.
    quitting: waiting ? (device?.stack?.working() ?? 0) : null,
    sandbox: sandboxLine(lacking, deliveryState(), boot),
    update: updateLine(updates?.state ?? null),
  };
}

// The tray, once the app is ready; and its menu as last set, set again only when it changes.
let tray: Tray | null = null;
let trayDrawn = "";

const trayImage = (): string => join(ASSETS, trayIcon(theme.dark, process.env.XDG_CURRENT_DESKTOP));

function updateTray(): void {
  if (!tray) return;
  const agent = agents.get();
  const template = trayMenu({ device: agent ? deviceLine(agent) : null, quitting: waiting ? (device?.stack?.working() ?? 0) : null, shortcut }, {
    show: () => main?.show(),
    quickEntry: toggleQuickEntry,
    settings: menuActions.settings,
    quit: () => app.quit(),
    quitNow: () => waiting?.(),
  });
  const drawn = JSON.stringify(template.map((item) => [item.label, item.enabled, item.accelerator]));
  if (drawn === trayDrawn) return;
  trayDrawn = drawn;
  tray.setContextMenu(Menu.buildFromTemplate(template));
}

// The system's notifications, once the app is ready.
let notifications: Notifications | null = null;

// What an inbox item's notification says under its title, by its kind: the app's own words.
const TOLD: Record<string, string> = {
  input_required: "Asks you a question.",
  action_required: "Needs you to do something.",
  governance_gate: "Waits for your approval.",
  task_complete: "Finished.",
  progress_checkin: "Checked in.",
};

// Only the app's own words for a kind it knows: the agent's kind is no key of an object's prototype.
const bodyOf = (kind: string): string => Object.hasOwn(TOLD, kind) ? TOLD[kind]! : "Has something for you.";

// The window is away: hidden, minimised, or behind another app's.
const away = (): boolean => BrowserWindow.getFocusedWindow() === null;

// A page of the web client, the window shown: what a notification's click opens. Once the quit goes
// on there is no notice left to click: the quit closes them all as it hides the window.
function openPage(path: string): void {
  if (!main || !webClientPath(path)) return;
  main.show();
  goWeb(path);
}

const burst = new Burst();

// Once the quit goes on, nothing more is told: the user is done with the app. The end of a turn in
// the chat followed is told by its follow, though the inbox has it too while no page streams the chat;
// before the follow has started, it may never hear of that turn, and the inbox tells it.
function tellItem(item: InboxItem): void {
  if (leaving || !away()) return;
  if (item.kind === "task_complete" && chat?.id === item.sessionId && chat.started()) return;
  const told = burst.add(item);
  if (told.length <= BURST) {
    notifications?.show({
      tag: `chat:${item.sessionId}`, title: item.title, body: bodyOf(item.kind), open: () => openPage(`/chat/${item.sessionId}`),
    });
    return;
  }
  // A flood, as after a night asleep, is one notice of how many came, which opens the inbox; the burst's own go.
  for (const each of told) notifications?.close(`chat:${each.sessionId}`);
  notifications?.show({ tag: "inbox", title: `${told.length} new items in your inbox`, body: "Open your inbox to see them.", open: () => openPage("/inbox") });
}

function tellTurnEnd(sessionId: string, title: string): void {
  if (leaving || !away()) return;
  notifications?.show({ tag: `chat:${sessionId}`, title, body: "Finished.", open: () => openPage(`/chat/${sessionId}`) });
}

// The chat the web client shows: while the window is away, it is followed to the end of each turn,
// which the agent leaves out of the inbox while the window's own page streams the chat.
let chatShown: string | null = null;

function showing(url: string): void {
  const path = new URL(url).pathname;
  chatShown = path.startsWith("/chat/") && webClientPath(path) ? path.slice("/chat/".length) : null;
  followAgent();
}

// What the agent tells, followed on the app's own sign-in, for whoever is signed in now: their
// inbox, and the chat the web client shows while the window is away. What comes for a sign-in that
// has ended meanwhile is told no more, and nothing is followed once the quit goes on.
let inbox: { session: DesktopSession; stop(): void } | null = null;
let chat: { session: DesktopSession; id: string; stop(): void; started(): boolean } | null = null;

function followAgent(): void {
  const session = leaving ? null : signedIn;
  const agentId = agents.get()?.agentId ?? "";
  const api = (path: string, init?: RequestInit) => session!.api(path, init);
  if (inbox?.session !== session) {
    inbox?.stop();
    inbox = session && {
      session,
      stop: followInbox({
        api, agentId, onError: report, onItem: (item) => {
          if (signedIn === session) tellItem(item);
        },
      }),
    };
  }
  const watched = session && away() ? chatShown : null;
  if (chat?.session !== session || chat?.id !== watched) {
    chat?.stop();
    chat = session && watched !== null
      ? {
        session, id: watched,
        ...followChat({
          api, agentId, onError: report, sessionId: watched, onTurnEnd: (title) => {
            if (signedIn === session) tellTurnEnd(watched, title);
          },
        }),
      }
      : null;
  }
}

// A prompt waits while the window is hidden: the system's notification says so, and opens the window.
function notifyAsking(): void {
  if (leaving) return;
  notifications?.show({ tag: "asking", title: "Surogate is asking you something", body: "Open Surogate to answer.", open: () => main?.show() });
}

// A page of the web client in the centre: what the sidebar's links, New chat, quick entry and a
// notification open. True once it has loaded; false once it failed, or another load took its place.
function goWeb(path: string): Promise<boolean> {
  if (!main) return Promise.resolve(false);
  choose();
  // The open project's conversation, or a thread its pane lists, keeps the project open, with its
  // crumb and Overview, as View thread does; any other page leaves it.
  const open = view.kind === "project" && overview?.project.id === view.id ? view : null;
  const chatId = path.startsWith("/chat/") ? path.slice("/chat/".length) : null;
  const thread = open ? overview?.threads.find((found) => found.id === chatId) : undefined;
  if (open && (thread || chatId === open.masterSessionId)) {
    view = { ...open, thread: thread ? { id: thread.id, title: thread.title } : null };
    // Shown in the centre, its transcript in the pane has nothing more to show.
    if (thread && reading?.id === thread.id) read(null);
    changed();
  } else {
    show({ kind: "web" });
  }
  main.showWeb(true);
  return main.go(path);
}

// Quick entry, once the app is ready: what the user types there starts the chat New starts.
let quickEntry: QuickEntry | null = null;
// The keys that open it from anywhere, once the app holds them: never in a Wayland session, nor while another app holds them.
let shortcut: string | null = null;

/** Quick entry shown, or hidden when it shows. With nobody signed in, the window shows instead: it asks them to sign in. */
function toggleQuickEntry(): void {
  if (leaving || !main) return;
  if (!signedIn || !main.webContents()) return main.show();
  quickEntry?.toggle();
}

// The text quick entry handed the agent's page, until the page says what became of it.
let handing: { id: string; settle(refused: string | null): void } | null = null;
const LEFT_CHAT = "Surogate's window left the new chat before it was made, so nothing was sent.";

// The page's word on the text it was handed: null once it sent it, or why it did not.
function handed(id: string, refused: string | null): void {
  if (!handing || handing.id !== id) return;
  const { settle } = handing;
  handing = null;
  settle(refused);
}

// Quick entry's text starts a new chat, as New does: Settings closes, the window shows, and its web
// client loads /chat. Only once that load is the page on screen is the text handed to it, by an id
// of its own: the page sends it as the new chat's first message once it may, and says so. Its
// preload holds it until the page listens. Why it was not sent, or null once the page sent it.
async function sendQuickEntry(text: string): Promise<string | null> {
  const agent = agents.get();
  const shown = main;
  const view = shown?.webContents();
  const session = signedIn;
  // A page that said nobody, or another account, is not who the text is from: before the load, nor
  // after it, as when the user logs out while it loads.
  const notTheirs = () => leaving || session === null || signedIn !== session || !pageIs(session.account);
  const SIGN_IN = "Sign in to your agent in Surogate's window first.";
  if (!shown || !view || !agent || notTheirs()) return SIGN_IN;
  const unreachable = `Surogate cannot reach ${agent.name} right now, so nothing was sent.`;
  if (shown.unreachable !== null) return unreachable;
  // Quick entry starts new chats, and an agent of one conversation has none to start: said before
  // Settings closes or the window loads anything. Its page says the same, where it knows better than the app's last read.
  if (agent.multiSession === false) return "This agent keeps one conversation, and quick entry starts new chats, so nothing was sent. Write to it in Surogate's window.";
  // A newer message takes the place of one still on its way.
  if (handing) handed(handing.id, "A newer message took its place.");
  quickEntry?.hide();
  shown.closeSettings();
  shown.show();
  const loaded = await goWeb("/chat");
  if (shown.unreachable !== null) return unreachable;
  if (notTheirs()) return SIGN_IN;
  // A load another replaced, or one that ended on another page, is not the new chat.
  if (!loaded || shown.webContents() !== view || new URL(view.getURL()).pathname !== "/chat") return LEFT_CHAT;
  const id = crypto.randomUUID();
  return new Promise((settle) => {
    handing = { id, settle };
    view.send("desktop:quick-entry", { id, text });
  });
}

// The window's menu button opens the app's own menu, as Claude Desktop's does; the project's opens its own.
function popup(which: unknown): void {
  const shown = main;
  const agent = agents.get();
  if (!shown) return;
  const menu = which === "project"
    ? Menu.buildFromTemplate([
      { label: "Reload", click: () => shown.reload() },
      { label: "Open in browser", enabled: agent !== null, click: () => agent && void shell.openExternal(agent.origin) },
    ])
    : Menu.getApplicationMenu();
  menu?.popup({ window: shown.window });
}

// What the app's menu does: on the agent's page, or the window, which it shows first.
const menuActions = {
  newChat: () => {
    main?.show();
    goWeb("/chat");
  },
  settings: () => {
    main?.show();
    showSettings();
  },
  quit: () => app.quit(),
  reload: () => main?.reload(),
  zoom: (step: -1 | 0 | 1) => main?.zoom(step),
  devTools: (which: "agent" | "window") => (which === "agent" ? main?.webContents() : main?.window.webContents)?.openDevTools({ mode: "detach" }),
  documentation: () => openLink("help"),
  about: () => {
    if (!main) return;
    main.show();
    openAbout({
      parent: main.window, page: join(PAGES, "about.html"), preload: PAGES_PRELOAD, dark: theme.dark, version: VERSION,
      documentation: () => openLink("help"),
    });
  },
};

// Why the last browser picked with Custom… was not kept, until the next choice.
let browserFailure: string | null = null;
// Whether Settings is asking to hand the agent's browser back: one confirmation at a time.
let handingBack = false;
// The hand back confirmations asked and not answered yet, from a chat's page or from Settings, and what closes
// each: once the browser is handed back by one, the others ask about a hold that is gone.
const handBacks = new Set<AbortController>();
// The browser was handed back through *by*: every other confirmation is closed, and nothing is asked after it.
const handedBack = (by: AbortController): void => {
  for (const other of handBacks) if (other !== by) other.abort();
};

// Settings → Browser's rows: what is found here, each named with the version it says.
async function browserState() {
  const found = findBrowsers();
  const choice = browserSetting.get();
  const versions = new Map<string, string>();
  await Promise.all(found.filter((browser) => browser.unsupported === null).map(async (browser) => {
    const version = await browserVersion(browser.executable);
    if (version) versions.set(browser.executable, version);
  }));
  return {
    choice: choice.choice, rows: choiceRows(choice, found, versions), none: chosenBrowser(choice, found) === null, failure: browserFailure,
    // Held from a chat that is gone: handed back here, where no chat's own page may be left to do it in.
    held: openStack()?.heldFromGone() ?? false,
  };
}

/**
 * A choice in Settings → Browser: Automatic or a browser found here, kept as it is; or Custom…, a
 * program the user picks in the system's dialog, kept only once it has launched as the agent's
 * browser does. The next launch of the agent's browser uses it.
 */
async function chooseBrowser(value: unknown): Promise<void> {
  browserFailure = null;
  try {
    if (value === "custom") return;
    if (value !== "pick") {
      if (value !== "auto" && !KNOWN.some(({ id }) => id === value)) throw new Error(`No browser ${String(value)}`);
      browserSetting.set({ choice: value } as BrowserChoice);
      return;
    }
    const options = { title: "Choose the agent's browser", buttonLabel: "Choose", properties: ["openFile" as const] };
    const picked = main ? await dialog.showOpenDialog(main.window, options) : await dialog.showOpenDialog(options);
    const path = picked.canceled ? undefined : picked.filePaths[0];
    if (!path) return;
    let real: string;
    try {
      real = realpathSync(path);
    } catch {
      // Gone since it was picked, or a link that leads nowhere.
      browserFailure = `Surogate cannot use ${path}: it cannot be read here.`;
      return;
    }
    const why = unsupportedAt(real);
    if (why) {
      browserFailure = `Surogate cannot use ${real}: ${why}.`;
      return;
    }
    // A try's own profile is a passing one, in the folder of every identity's profiles.
    const once = new BrowserClient(utilityBrowser(join(root, "browser-profiles")));
    const tried = await once.tryBrowser(real).finally(() => once.stop());
    if ("error" in tried) browserFailure = `${real} did not start as a browser Surogate can drive: ${tried.error.message}`;
    else browserSetting.set({ choice: "custom", executable: real, version: String((tried.ok as { version?: unknown } | null)?.version ?? "") });
  } finally {
    changed();
  }
}

// The app's menu, with Developer in developer mode only: its tools read and change everything on the agent's page.
function setMenu(): void {
  Menu.setApplicationMenu(Menu.buildFromTemplate(appMenu(menuActions, preferences.get().developer)));
}

// Developer mode is asked about before it is turned on, in a box of the app's own, so a link waits
// while it is up. One question at a time: On pressed again while it is up asks nothing more.
let askingDeveloper: Promise<boolean> | null = null;

function confirmDeveloper(): Promise<boolean> {
  askingDeveloper ??= ask({
    type: "warning",
    message: "Turn on developer mode?",
    detail: "Its developer tools can read and change everything on the agent's page, your sign-in to it included. "
      + "Turn it on only if you know why you need it, never because a page or a message asks you to.",
    buttons: ["Turn on", "Cancel"],
    defaultId: 1,
    cancelId: 1,
    noLink: true,
  }).finally(() => {
    askingDeveloper = null;
  });
  return askingDeveloper;
}

// Keep running and developer mode, as Settings sends them: "on" or "off".
async function setPreference(key: "keepRunning" | "developer", value: unknown): Promise<void> {
  if (value !== "on" && value !== "off") throw new Error(`No setting ${key} = ${String(value)}`);
  const on = value === "on";
  if (key === "developer" && on && !preferences.get().developer && !(await confirmDeveloper())) return;
  preferences.set(key, on);
  if (key !== "developer") return;
  setMenu();
  // Off, the tools it opened close with it.
  if (!on) for (const contents of [main?.webContents(), main?.window.webContents]) contents?.closeDevTools();
}

const onOff = (on: boolean) => (on ? "on" : "off");

// Past the guest's own bound on a kill: 2 s for the process to end on SIGTERM, 2 s more on SIGKILL.
const STOP_PROCESS_MS = 15_000;

// Each bound chat's title, read on the app's own sign-in, and kept once the agent named it. One it
// names not yet, or whose read failed, is read once each time Settings opens, not at each redraw.
// ponytail: kept until the app quits or this computer's access ends for its user, one per chat bound here: a chat renamed meanwhile keeps its old title until then.
const titles = new Map<string, string>();
const reads = new Map<string, Promise<string>>(); // this opening of Settings' own

function chatTitle(root: string): Promise<string> {
  const known = titles.get(root);
  if (known !== undefined) return Promise.resolve(known);
  const session = signedIn;
  if (!session) return Promise.resolve("A chat");
  let read = reads.get(root);
  if (!read) {
    read = titleOf((path, init) => session.api(path, init), agents.get()?.agentId ?? "", root).then((title) => {
      if (title !== "A chat") titles.set(root, title);
      return title;
    }, (error: unknown) => {
      report(error);
      return "A chat";
    });
    reads.set(root, read);
  }
  return read;
}

// How long a prompt waits to name a chat: its user asked for it with a click, and an agent slow to answer holds it no longer.
const TITLE_MS = 2_000;

/** Chat *root*'s title for a prompt that names it: null for one the agent names not, or does not name within TITLE_MS. */
async function titleSoon(root: string): Promise<string | null> {
  let late: NodeJS.Timeout | undefined;
  const title = await Promise.race([
    chatTitle(root),
    new Promise<string>((resolve) => {
      late = setTimeout(resolve, TITLE_MS, "A chat");
    }),
  ]);
  clearTimeout(late);
  return title === "A chat" ? null : title;
}

// The device's stack while its journal is open: a computer the agent revoked keeps its stack, closed, until it is restored.
const openStack = (): DeviceStack | null => (kept?.token === null ? null : device?.stack ?? null);

// Settings → Folders and permissions: the folders this computer works on for its device's account.
async function folderRows(): Promise<FolderRow[]> {
  if (kept?.token === null) throw new Error(REVOKED);
  const stack = device?.stack;
  return stack ? listFolders(stack.bindings, chatTitle, alive) : [];
}

// What Settings shows, read once the browsers have said their versions: a state asked for
// before a change, and answered after it, still shows the change.
async function settingsState() {
  const browser = await browserState();
  const agent = agents.get();
  const { keepRunning, developer } = preferences.get();
  return {
    browser,
    appearance: appearance.get(),
    preferences: { startAtLogin: onOff(startsAtLogin(autostart)), keepRunning: onOff(keepRunning), developer: onOff(developer) },
    // Why this build cannot start at login, or null.
    startAtLoginRefused: loginRefusal(loginCommand()),
    account,
    computer: {
      name: hostname(),
      connection: agent ? deviceLine(agent) : "",
      added: kept?.addedAt ?? null,
      // The app's own sign-in names it, before the web client reports and whatever it reports.
      organisation: signedIn?.account.orgName ?? null,
      agents: agent ? [agent.name] : [],
    },
    links: { usage: "usage" in links() },
    sandbox: sandboxLine(lacking, deliveryState(), boot),
  };
}

// What the project dialog sends for a project: its name and goal, and, for one that exists, its
// instructions and tiers. Lengths are the routes' own, in UTF-16 units as JavaScript counts them.
interface ProjectFields {
  name: string;
  goal: string;
  instructions?: string;
  coordinatorTier?: Tier;
  threadTier?: Tier;
}

const tierOf = (value: unknown): value is Tier => value === null || value === "basic" || value === "pro";

function projectFields(value: unknown, editing: boolean): ProjectFields {
  const { name, goal, instructions, coordinatorTier, threadTier } = (value ?? {}) as Record<string, unknown>;
  const fits = (field: unknown, max: number) => typeof field === "string" && field.length <= max;
  if (!fits(name, 256) || !fits(goal, 2_000)) throw new Error("Not a project's fields");
  if (editing && !(fits(instructions, 16_000) && tierOf(coordinatorTier) && tierOf(threadTier))) throw new Error("Not a project's fields");
  const named = { name: (name as string).trim(), goal: goal as string };
  return editing
    ? { ...named, instructions: instructions as string, coordinatorTier: coordinatorTier as Tier, threadTier: threadTier as Tier }
    : named;
}

// What the dialog changed of *shown*, the project as it showed it: a change made elsewhere meanwhile
// to a field the user left alone is kept. The goal it showed empty is a null one.
function changedFrom(shown: Project, fields: ProjectFields): Partial<ProjectFields> {
  return Object.fromEntries(Object.entries(fields).filter(([key, value]) =>
    value !== (key === "goal" ? shown.goal ?? "" : shown[key as keyof ProjectFields])));
}

// The archive asks under the project's name as the app's prompts show text: a control, bidi or invisible
// character as its code point (U+202E), so none reorders or hides the question around it.
async function confirmArchive(name: string): Promise<boolean> {
  if (!main) return false;
  const response = await messageBox({
    type: "warning",
    message: `Archive ${asShown(name)}?`,
    detail: "It leaves your projects, with its conversation and its threads. Its files and its memory are kept.",
    buttons: ["Archive", "Cancel"],
    defaultId: 1,
    cancelId: 1,
    noLink: true,
  });
  return response === 0;
}

// The project dialog, over the window: a new project, or the open project's settings. Its page's
// calls are answered on its own view only; each answers why it was refused, or null once done.
function showProject(editing: Opened | null): void {
  const page = join(PAGES, "project.html");
  main?.openSettings(page, PAGES_PRELOAD, undefined, (contents) => {
    projectDialog = contents;
    const handle = (channel: string, handler: (...args: unknown[]) => unknown) => {
      contents.ipc.handle(channel, (event, ...args: unknown[]) => {
        if (!ownPage(event.senderFrame, page)) throw new Error("Not the project dialog's own page");
        return handler(...args);
      });
    };
    const refused = (error: unknown) => (error instanceof Error ? error.message : String(error));
    // A change the page did not answer in time may have been made all the same: it is said so, and the
    // projects are read again, so one that was made shows.
    const unanswered = (error: unknown, what: string) => {
      if (!(error instanceof TimedOut)) return refused(error);
      void refreshProjects();
      return `${error.message}: ${what}`;
    };
    // The project as the dialog showed it: its archive is asked under the name the user sees.
    let shown: Project | null = null;
    // A project that cannot be read, gone or out of reach, is said so, with nothing to save.
    handle("project:state", async () => {
      if (!editing) return { editing: false, project: null, refused: null };
      try {
        shown = await askServed(() => projects.get(editing.id));
        return { editing: true, project: shown, refused: null };
      } catch (error) {
        return { editing: true, project: null, refused: refused(error) };
      }
    });
    // Only this dialog: one the user closed meanwhile may have given its place to Settings, or another.
    const close = () => {
      if (main?.settingsContents() === contents) main.closeSettings();
    };
    handle("project:save", async (value) => {
      const fields = projectFields(value, editing !== null);
      if (fields.name === "") return "Name the project.";
      const change = editing && shown ? changedFrom(shown, fields) : fields;
      if (editing && Object.keys(change).length === 0) {
        close();
        return null;
      }
      try {
        const project = editing
          ? await projects.update(editing.id, change)
          : await projects.create({ name: fields.name, goal: fields.goal });
        remember(project);
        // A new project opens on its conversation, as one chosen in the sidebar does, unless its dialog
        // was closed meanwhile: the user has gone on to something else, which a late answer leaves alone.
        const opening = !editing && main?.settingsContents() === contents;
        close();
        if (opening && webClientPath(`/chat/${project.masterSessionId}`)) {
          choose();
          show({ kind: "project", id: project.id, name: project.name, masterSessionId: project.masterSessionId, thread: null });
          main?.showWeb(true);
          main?.go(`/chat/${project.masterSessionId}`);
        }
        void refreshProjects();
        return null;
      } catch (error) {
        return unanswered(error, editing ? "the change may have been made" : "the project may have been made");
      }
    });
    handle("project:archive", async () => {
      if (!editing || !(await confirmArchive(shown?.name ?? editing.name))) return null;
      try {
        await projects.archive(editing.id);
      } catch (error) {
        return unanswered(error, "the project may have been archived");
      }
      close();
      if (view.kind === "project" && view.id === editing.id) {
        show({ kind: "web" });
        main?.go("/chat");
      }
      void refreshProjects();
      return null;
    });
    handle("project:close", () => main?.closeSettings());
  });
}

// Settings, over the window, on *section* when one is named: its page's calls are answered on its own view only.
function showSettings(section?: "browser"): void {
  const page = join(PAGES, "settings.html");
  // Open already: its page is shown the section. ponytail: one that still loads does not hear it, and opens on its own.
  const open = main?.settingsContents();
  if (section && open && open !== projectDialog) open.send("settings:show", section);
  main?.openSettings(page, PAGES_PRELOAD, section, (contents) => {
    // A chat named not yet is asked about again, once.
    reads.clear();
    const handle = (channel: string, handler: (...args: unknown[]) => unknown) => {
      contents.ipc.handle(channel, (event, ...args: unknown[]) => {
        if (!ownPage(event.senderFrame, page)) throw new Error("Not Settings' own page");
        return handler(...args);
      });
    };
    handle("settings:state", settingsState);
    handle("settings:folders", folderRows);
    // A host a chat's user let it reach, taken back: the chat's next connection there asks again.
    handle("settings:take-back", (root, host) => {
      const bindings = openStack()?.bindings;
      if (!bindings || typeof root !== "string" || typeof host !== "string" || !bindings.domains(root).includes(host)) {
        throw new Error("This chat cannot reach that host");
      }
      bindings.disallowDomain(root, host);
    });
    // A chat's browser taken back by its user: its agent's next browser call asks its first use again. Its tabs stay.
    // The agent's browser held from that chat stays held: taking this back hands nothing back.
    handle("settings:take-back-browser", (root) => {
      const bindings = openStack()?.bindings;
      if (!bindings || typeof root !== "string" || !bindings.browsing(root)) throw new Error("This chat does not use the browser on this computer");
      bindings.disallowBrowser(root);
    });
    // The agent's browser handed back from here, where it is held from a chat that is gone: such a chat has no
    // page to hand it back in, and another chat has a pane for it only where its own browser is open. Through the
    // same confirmation as from a chat's page, at its user's click in the desktop's own page; one at a time, and
    // closed with Settings. Whether it was handed back.
    handle("settings:hand-back-browser", async () => {
      const stack = openStack();
      const agent = agents.get();
      if (!stack?.heldFromGone() || !agent || handingBack) return false;
      handingBack = true;
      const closed = new AbortController();
      const gone = () => closed.abort();
      contents.once("destroyed", gone);
      handBacks.add(closed);
      try {
        if (!(await prompts.confirmHandBack({ agent: agent.name, gone: true, title: null }, closed.signal))) return false;
        // Whether anything was handed back: a chat may have taken the browser over while the confirmation was up.
        if (!stack.handBackGone()) return false;
        handedBack(closed);
        // Every chat's page is told: none of them holds it, and each may use it again.
        for (const { root } of stack.bindings.all()) main?.webContents()?.send("desktop:binding-changed", root);
        return true;
      } finally {
        handingBack = false;
        handBacks.delete(closed);
        contents.off("destroyed", gone);
        if (!contents.isDestroyed()) contents.send("settings:changed");
      }
    });
    // A chat's background process, stopped by its user, as the agent's own kill stops one. Only one
    // Settings shows: the VM runs other devices' chats too, and a chat deleted here keeps its processes there.
    handle("settings:stop", async (processRoot, id) => {
      const stack = openStack();
      if (
        !stack || typeof processRoot !== "string" || typeof id !== "string" || !stack.bindings.get(processRoot)
        || !alive.of(processRoot).some((found) => found.id === id)
      ) {
        throw new Error("This chat runs no such process");
      }
      const outcome = await stack.binder.run(stopOperation(processRoot, id), AbortSignal.timeout(STOP_PROCESS_MS));
      if ("error" in outcome) throw new Error(outcome.error.message);
    });
    // A theme in effect that changes reaches the web client through the theme's own paint.
    handle("settings:set", async (key, value) => {
      if (key === "browser") return chooseBrowser(value);
      if (key === "keepRunning" || key === "developer") return setPreference(key, value);
      if (key === "startAtLogin") {
        if (value !== "on" && value !== "off") throw new Error(`No setting startAtLogin = ${String(value)}`);
        return setStartAtLogin(autostart, value === "on", loginCommand());
      }
      if (key !== "theme") {
        appearance.set(String(key), value);
        tellAppearance();
      } else if (value === "system" || value === "light" || value === "dark") {
        theme.choose(value);
      } else {
        throw new Error(`No appearance setting theme = ${String(value)}`);
      }
    });
    handle("settings:link", openLink);
    handle("settings:sandbox", sandboxAction);
    handle("settings:close", () => main?.closeSettings());
  });
}

function wire(window: MainWindow, page: string): void {
  const handle = (channel: string, handler: (...args: unknown[]) => unknown) => {
    window.window.webContents.ipc.handle(channel, (event, ...args: unknown[]) => {
      if (!ownPage(event.senderFrame, page)) throw new Error("Not the window's own page");
      return handler(...args);
    });
  };
  handle("shell:state", state);
  // One connection at a time: a second Enter while the first is asked waits for none.
  handle("shell:connect", (address) => {
    if (typeof address !== "string" || address.length > 2048) return "That is not a web address";
    return connectTo(address);
  });
  handle("shell:sign-in", () => {
    const agent = agents.get();
    if (agent) void signIn(agent);
  });
  handle("shell:sign-out", () => {
    const agent = agents.get();
    if (agent) void signOut(agent, false);
  });
  handle("shell:restore", () => {
    const agent = agents.get();
    if (agent) void restore(agent).catch(report);
  });
  handle("shell:remove", () => {
    const agent = agents.get();
    if (agent) void signOut(agent, true);
  });
  handle("shell:go", (path) => {
    if (typeof path !== "string" || !webClientPath(path)) throw new Error("Not a page of the web client");
    goWeb(path);
  });
  handle("shell:projects", () => {
    choose();
    if (view.kind !== "projects") beforeProjects = view;
    show({ kind: "projects" });
    window.showWeb(false);
  });
  // A project opens on its conversation: the page's answer names it, and is checked as a chat's path.
  // A project chosen while the page loads waits for it to serve; a failure is said in the sidebar.
  handle("shell:project", async (id) => {
    const mine = choose();
    changed();
    try {
      if (typeof id !== "string") throw new Error("No such project");
      const project = await askServed(() => projects.get(id));
      const path = `/chat/${project.masterSessionId}`;
      if (!webClientPath(path)) throw new Error("This project's conversation is not a chat");
      remember(project);
      if (mine !== choice) return;
      show({ kind: "project", id: project.id, name: project.name, masterSessionId: project.masterSessionId, thread: null });
      window.showWeb(true);
      window.go(path);
    } catch (error) {
      if (mine !== choice) return;
      failure = error instanceof Error ? error.message : String(error);
      changed();
    }
  });
  // A thread of the open project, in the centre. One that has left the pane since it was drawn is said so.
  handle("shell:thread", (id) => {
    const thread = view.kind === "project" && overview?.project.id === view.id
      ? overview.threads.find((found) => found.id === id) : undefined;
    const path = `/chat/${String(id)}`;
    choose();
    if (view.kind !== "project" || !thread || !webClientPath(path)) {
      failure = NO_SUCH_THREAD;
      return changed();
    }
    view = { ...view, thread: { id: thread.id, title: thread.title } };
    // Shown in the centre, its transcript in the pane has nothing more to show.
    if (reading?.id === thread.id) read(null);
    window.go(path);
    changed();
  });
  handle("shell:read", (id) => {
    if (id !== null && typeof id !== "string") throw new Error("Not a thread");
    read(id);
  });
  handle("shell:focus-pane", () => window.focusPane());
  // A thread of the open project resolved, or reopened, from its row: the page's answer is its row.
  // A row's action is no choice of what the centre shows, so a project opening meanwhile still opens:
  // it takes a number of its own, and only the latest action's refusal is said.
  let settling = 0;
  const settle = (how: "resolve" | "reopen") => async (id: unknown) => {
    const open = view.kind === "project" && overview?.project.id === view.id ? view : null;
    if (!open || typeof id !== "string" || !overview?.threads.some((found) => found.id === id)) {
      failure = NO_SUCH_THREAD;
      return changed();
    }
    const mine = ++settling;
    failure = null;
    changed();
    try {
      const row = await projects[how](open.id, id);
      if (view === open && overview?.project.id === open.id) overview = { ...overview, threads: merged(overview.threads, id, row) };
    } catch (error) {
      if (mine === settling) failure = error instanceof Error ? error.message : String(error);
    }
    changed();
  };
  handle("shell:resolve", settle("resolve"));
  handle("shell:reopen", settle("reopen"));
  // Back and Forward move the web client; on the Projects page they leave it, for what the centre
  // showed before it, as the client still is there. A failure the choice cleared is drawn away.
  const move = (step: () => void) => {
    choose();
    changed();
    if (view.kind !== "projects") return step();
    show(beforeProjects);
    window.showWeb(true);
    const url = window.webContents()?.getURL();
    if (url) navigated(url);
    void refreshProjects();
  };
  handle("shell:back", () => move(() => window.back()));
  handle("shell:forward", () => move(() => window.forward()));
  handle("shell:reload", () => window.reload());
  handle("shell:place", (hole) => window.place(bounds(hole)));
  handle("shell:place-pane", (hole) => window.placePane(bounds(hole)));
  handle("shell:menu", popup);
  handle("shell:settings", () => showSettings());
  handle("shell:new-project", () => showProject(null));
  handle("shell:project-settings", () => {
    if (view.kind !== "project") throw new Error("No project is open");
    showProject({ id: view.id, name: view.name, masterSessionId: view.masterSessionId });
  });
  // A quit waiting for the threads goes now: the user said so, in the window.
  handle("shell:quit-now", () => waiting?.());
  handle("shell:link", openLink);
  handle("shell:sandbox", sandboxAction);
  handle("shell:update", updateAction);
}

// Quitting, as Claude Desktop quits (its updater's session guard): with threads working on this
// computer, the user is asked first, and may wait for them; then the device stops in its order.
async function confirmQuit(working: number): Promise<"quit" | "wait" | "cancel"> {
  const options = {
    type: "warning" as const,
    message: "Surogate is still working",
    detail: `${working === 1 ? "1 thread is" : `${working} threads are`} working on this computer. Quitting now will interrupt that work.`,
    buttons: ["Quit anyway", "Wait for them", "Cancel"],
    defaultId: 1,
    cancelId: 2,
    noLink: true,
  };
  const response = await messageBox(options, main?.window.isVisible() ? main.window : undefined);
  return (["quit", "wait", "cancel"] as const)[response] ?? "cancel";
}

let quitting: Promise<void> | null = null;
// While a quit waits for the threads: what ends the wait at once.
let waiting: (() => void) | null = null;
let askingAgain = false;
let stopped = false;
// Once the quit goes on: nothing shows the window again while the device stops.
let leaving = false;
// A quit that restarts the app into its update once it is done: the installed app started again from
// its launcher, never process.execPath, which is the old version's own folder.
let restarting = false;

// A quit the user asked for stays a quit: once one is under way, an update that ends installed does
// not turn it into a restart, nor ask "Quit now?" of a quit that waits. The update stays installed,
// and the next start is the user's own.
function restart(): void {
  if (quitting) return;
  restarting = true;
  app.quit();
}

// A quit asked again while the first waits for the threads: quit now, or keep waiting.
async function quitNow(): Promise<void> {
  const working = device?.stack?.working() ?? 0;
  const options = {
    type: "warning" as const,
    message: "Quit now?",
    detail: `Surogate is waiting for ${working === 1 ? "1 thread" : `${working} threads`} working on this computer. Quitting now will interrupt that work.`,
    buttons: ["Quit now", "Keep waiting"],
    defaultId: 1,
    cancelId: 1,
    noLink: true,
  };
  if ((await messageBox(options, main?.window.isVisible() ? main.window : undefined)) === 0) waiting?.();
}

async function quit(): Promise<void> {
  const working = device?.stack?.working() ?? 0;
  if (working > 0) {
    const answer = await confirmQuit(working);
    if (answer === "cancel") {
      // The update stays installed: the line's Restart asks again.
      restarting = false;
      return;
    }
    if (answer === "wait" && (device?.stack?.working() ?? 0) > 0) {
      await new Promise<void>((resume) => {
        waiting = resume;
        changed();
      });
      waiting = null;
    }
  }
  // The user is done with the app: its window, its tray and what it told go now, while the device
  // stops in its order, and nothing brings the window back. Before ready there is nothing to close.
  leaving = true;
  main?.window.hide();
  // Every other window of the app's goes with it: About, a prompt, the Composio sign-in.
  for (const window of BrowserWindow.getAllWindows()) if (window !== main?.window) window.close();
  tray?.destroy();
  tray = null;
  notifications?.closeAll();
  // What the agent tells is followed no more, though a window hidden already emits no hide to stop it.
  followAgent();
  // A sign-in under way closes its port in the browser's face, and keeps what the agent already
  // issued it; revocations still owed are tried at the next launch.
  signingIn?.abort(QUIT);
  await signedInOrStopped;
  // A log out under way finishes first: it keeps the revocation owed, and forgets the folders.
  await signingOut;
  await Promise.all([...revocations.values()].map((revoking) => revoking.stop()));
  stopDelivery.abort(new Error("Surogate quit"));
  // The device, then the VM, which stops even when the device's stop fails. A stop that
  // fails still quits: the next launch answers what it cut off.
  try {
    await stopDevice(device?.started, vm);
  } finally {
    stopped = true;
    if (restarting) {
      const [execPath, ...args] = loginCommand();
      app.relaunch({ execPath: execPath!, args });
    }
    app.quit();
  }
}

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  // A second launch shows the window, unless it is a start at login, and hands it the link it was started
  // with, if any; once the quit goes on, it does neither.
  app.on("second-instance", (_event, argv) => {
    if (leaving) return;
    if (!argv.includes(HIDDEN)) main?.show();
    const link = linkIn(argv);
    if (link) void openDeepLink(link).catch(report);
  });
  // The device link stays up with the window closed (spec, Section 7).
  app.on("window-all-closed", () => {});
  app.on("before-quit", (event) => {
    if (stopped) {
      letWindowClose();
      return;
    }
    event.preventDefault();
    if (waiting && !askingAgain) {
      askingAgain = true;
      void quitNow().catch(report).finally(() => {
        askingAgain = false;
      });
      return;
    }
    quitting ??= quit().catch(report).finally(() => {
      quitting = null;
    });
  });
  void app.whenReady().then(() => {
    credentials = new CredentialStore(join(root, "credentials.json"), safeStorage, report);
    notifications = new Notifications(Notification.isSupported() ? (content) => new Notification(content) : null, report);
    sessionStore = new SessionStore(join(root, "session.json"), safeStorage, report);
    // Before the window: its first frame is in the chosen theme.
    theme = new Theme(nativeTheme, appearance, (dark) => {
      main?.paint(dark);
      tray?.setImage(trayImage());
      tellAppearance();
    });
    // Electron's own menu goes: its reload, zoom and developer tools would act on the window's own pages.
    setMenu();
    prompts = desktopPrompts({ parent: () => main?.window, page: join(PAGES, "prompt.html"), preload: PAGES_PRELOAD, unseen: notifyAsking });
    // The VM slept with the computer: at its wake its clock is set, and its keepalive starts afresh.
    powerMonitor.on("resume", () => vm?.resume());
    // Its image downloaded in the background, and what the VM needs of this computer looked for.
    startDelivery();
    lookForTools();
    startUpdates();
    const page = join(PAGES, "shell.html");
    main = new MainWindow({
      states, page, preload: PAGES_PRELOAD, panePreload: PANE_PRELOAD, dark: theme.dark, onChange: changed,
      // A quit already waiting for the threads asks nothing more: the window hides meanwhile, as with Keep running on.
      quitsOnClose: () => !preferences.get().keepRunning && !waiting,
      // Started at login: the window waits for the user, in the tray or at the next launch.
      hidden: process.argv.includes(HIDDEN),
    });
    wire(main, page);
    // In the tray where the desktop has one; GNOME without one shows the window at the next launch.
    tray = new Tray(trayImage());
    tray.setToolTip("Surogate");
    tray.on("click", () => main?.show());
    updateTray();
    quickEntry = new QuickEntry({ page: join(PAGES, "quick.html"), preload: PAGES_PRELOAD, send: sendQuickEntry });
    // From anywhere on the display, where the X server grabs keys for an app: a Wayland session has
    // its GlobalShortcuts portal instead, which Surogate does not ask. There the tray opens quick entry.
    if (!waylandSession(process.env)) {
      if (globalShortcut.register(QUICK_ENTRY_KEYS, toggleQuickEntry)) shortcut = QUICK_ENTRY_KEYS;
      else report(new Error(`Another app holds ${QUICK_ENTRY_KEYS}: quick entry opens from the tray only`));
      updateTray();
    }
    main.window.on("focus", () => void refreshProjects());
    // The window going away, or coming back, starts or ends the follow of the chat it shows.
    app.on("browser-window-focus", () => followAgent());
    app.on("browser-window-blur", () => followAgent());
    main.window.on("show", () => followAgent());
    main.window.on("hide", () => followAgent());
    // Revocations owed from an earlier run, whatever agent they were for: each keeps its agent's address, device and token.
    const stored = credentials.list();
    for (const owed of stored.filter((credential) => credential.revoking)) {
      // Its folders went with the log out, or go now, after a crash that cut it short.
      rmSync(join(root, "devices", owed.deviceId), { recursive: true, force: true });
      revokeLater(owed);
    }
    // The link the app was started with, or one a second launch handed it meanwhile, once its window can show it.
    const launched = linkIn(process.argv) ?? linkEarly;
    const agent = agents.get();
    if (!agent) {
      if (launched) void openDeepLink(launched).catch(report);
      return;
    }
    const remembered = sessionStore.get();
    if (remembered?.origin === agent.origin && remembered.agentId === agent.agentId) startSession(remembered);
    // Gated before the web client is attached: with nobody signed in, it never shows.
    changed();
    // A window no sign-in of the app's owns has nobody signed in: whatever an older one left there goes.
    const shown = main;
    void (signedIn ? Promise.resolve() : clearWindow(agent)).catch(report).then(() => {
      open(shown, agent);
      if (launched) void openDeepLink(launched).catch(report);
    });
    kept = stored.find((credential) => !credential.revoking && credential.origin === agent.origin && credential.agentId === agent.agentId) ?? null;
    if (live(kept)) void startStack(agent, kept).catch(report);
    // An earlier try to add this computer did not end in a device: try again, once.
    void registerComputer(agent);
    void refreshAgent(agent);
  }).catch((error: unknown) => {
    // A start that fails is said, and ends the app: one left with no window would keep the
    // single-instance lock, and every later launch would hand it nothing.
    report(error);
    app.exit(1);
  });
}
