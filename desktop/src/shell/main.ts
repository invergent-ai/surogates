// The desktop shell's main process (spec, Sections 1 and 7): one window, as Claude
// Desktop has, signed in to one agent, and this computer's device link to that agent,
// which stays up with the window closed. It never runs what a tool call asks for.
// Readiness is awaited with then(), never a top-level await: an ES module main that
// awaits app.whenReady() deadlocks.

import { rmSync } from "node:fs";
import { hostname, userInfo } from "node:os";
import { join } from "node:path";

import {
  app, dialog, type IpcMainEvent, Menu, nativeTheme, net, Notification, safeStorage, session, shell, utilityProcess,
  type WebContents, webContents,
} from "electron";

import type { DesktopAccount } from "../../../web/src/lib/desktop-bridge-contract.js";
import type { LibraryEntry, Project, ProjectSummary, Routine, ThreadRow, Tier } from "../../../web/src/lib/projects-contract.js";
import type { ApprovalPrompts } from "../binding/approvals.js";
import type { FolderPrompts } from "../binding/binder.js";
import { revokeDevice, verifyDevice } from "../device.js";
import { appEnvironment } from "../hosts/environment.js";
import { OperationJournal } from "../journal/journal.js";
import type { LinkStatus } from "../link/client.js";
import { type FromManager, MANAGER, type ManagerProcess, type ToManager, VmClient, vmOptions } from "../vm/client.js";
import { VmExecutor } from "../vm/executor.js";
import { type Agent, AgentStore, connectAgent, describeAgent, type Get, linksFor, linkUrl, partitionFor, readAgent } from "./agents.js";
import { AppearanceStore, Theme } from "./appearance.js";
import { bridgeHandlers } from "./bridge.js";
import { reauthorize, rebind, register } from "./computer.js";
import { type Credential, CredentialStore, type LiveCredential } from "./credentials.js";
import { type DeviceStack, startDevice, stopDevice } from "./device-stack.js";
import { letWindowClose, MainWindow, onSettingsKey } from "./main-window.js";
import { type Fetch, OAuthError, revokeTokens, signInWithBrowser, type Tokens } from "./oauth.js";
import { ANSWER_TIMEOUT_MS, PageProjects } from "./projects.js";
import { desktopPrompts } from "./prompts.js";
import { accountOf, DesktopSession, SessionStore, type SignedIn } from "./session.js";
import { ownPage, sameOrigin, webClientPath } from "./window-policy.js";
import { type Bounds, WindowStates } from "./window-state.js";

const PAGES = join(import.meta.dirname, "pages");
const PAGES_PRELOAD = join(import.meta.dirname, "pages-preload.cjs");
const BRIDGE_PRELOAD = join(import.meta.dirname, "preload.cjs");

// Everything the app keeps lives under one root, Electron's own data too: the folder
// guards refuse it as a chat's folder, so no agent reaches a device token through one.
const dataHome = process.env.XDG_DATA_HOME?.startsWith("/") ? process.env.XDG_DATA_HOME : join(app.getPath("home"), ".local", "share");
const root = join(dataHome, "surogate");
// Before ready: the OS keyring names its item after the app.
app.setName("Surogate");
app.setPath("userData", join(root, "electron"));

const states = new WindowStates(join(root, "window-state.json"));
const appearance = new AppearanceStore(join(root, "settings.json"));
const agents = new AgentStore(join(root, "agent.json"));
let main: MainWindow | null = null;
let theme: Theme;
// After ready: safeStorage answers only then.
let credentials: CredentialStore;
// The desktop's own prompts, over the window, once it is made.
let prompts: FolderPrompts & ApprovalPrompts;
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
// The commands' environment, read from the login shell once.
let environment: Promise<Record<string, string>>;
// This computer's credential for the agent, as stored: what the page is told, and what keeps a second registration out.
let kept: Credential | null = null;
// Its device, from the moment it starts, before the commands' environment is read.
let device: { credential: Credential; status: LinkStatus; stack: DeviceStack | null; started: Promise<DeviceStack> } | null = null;
let registering = false;
let connecting = false;
// Who waits for the threads working on this computer to finish: a quit that the user told to wait.
const idle = new Set<() => void>();
// What the web client tells once its user signed in (undefined until it has said), and the projects it serves.
let account: DesktopAccount | null | undefined;
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

// The VM manager in an Electron utility process (spec, Section 11): a hang or a crash
// there leaves the windows and the device link alone. What is sent before it has spawned waits.
function utilityManager(): ManagerProcess {
  const child = utilityProcess.fork(MANAGER, [], { serviceName: "Surogate VM", stdio: "inherit" });
  const waiting: ToManager[] = [];
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
    onMessage: (listener) => void child.on("message", (message) => listener(message as FromManager)),
    onExit: (listener) => {
      if (exited) listener();
      else child.once("exit", () => listener());
    },
    kill: () => void child.kill(),
  };
}

// The app's one VM, shared by every device, for the user the commands' environment names.
let vm: VmClient | null = null;
const vmFor = (env: Record<string, string>): VmClient => {
  const { uid, gid, username, homedir } = userInfo();
  vm ??= new VmClient({ vm: vmOptions(root, { uid, gid, name: username, home: env.HOME ?? homedir }), spawn: utilityManager });
  return vm;
};

function bounds(value: unknown): Bounds {
  const { x, y, width, height } = (value ?? {}) as Partial<Bounds>;
  const sides = [x, y, width, height];
  if (!sides.every((side) => typeof side === "number" && Number.isFinite(side) && side >= 0)) throw new Error("Not a place");
  return { x: Math.round(x!), y: Math.round(y!), width: Math.round(width!), height: Math.round(height!) };
}

// What the window's page and an open Settings show changed: each reads its state again.
function changed(): void {
  // The web client shows only while someone is signed in to the app, and has its session.
  main?.gate(signedIn === null || reloading);
  main?.window.webContents.send("shell:changed");
  main?.settingsContents()?.send("settings:changed");
}

const appearanceNow = () => ({ ...appearance.get(), theme: theme.dark ? ("dark" as const) : ("light" as const) });

// The web client hears every change of how the app looks: the theme in effect, and the transcript's settings.
function tellAppearance(): void {
  main?.webContents()?.send("desktop:appearance", appearanceNow());
}

const links = () => linksFor(agents.get()?.origin ?? null);

function openLink(which: unknown): void {
  const known = links();
  const url = typeof which === "string" && Object.hasOwn(known, which) ? known[which] : undefined;
  if (!url) throw new Error("No such link");
  void shell.openExternal(url);
}

let refreshing = false;
// What the next refresh reads: null for everything, or the threads whose rows changed.
let wanted = new Set<string | null>();

// The projects the page serves, asked again: everything when it registers its source, when the
// open project changes, when the window comes to the front and when the project's stream says
// so; only a thread's row when the stream names the thread. One refresh runs at a time, and asks
// once more for whatever changed meanwhile, so an older answer never lands last. The open
// project, once the page lists it no more, is left.
async function refreshProjects(threadId: string | null = null): Promise<void> {
  if (!served) return;
  wanted.add(threadId);
  if (refreshing) return;
  refreshing = true;
  try {
    while (wanted.size > 0 && served) {
      const asked = wanted;
      wanted = new Set();
      try {
        // The list is one count per project: a thread's change moves the project's counts too.
        listed = await projects.list();
        const open = view.kind === "project" ? view : null;
        if (open && !listed.some((project) => project.id === open.id)) {
          overview = null;
          show({ kind: "web" });
        } else if (asked.has(null) || overview?.project.id !== open?.id) {
          await refreshOverview();
        } else {
          for (const id of asked) await refreshThread(id!);
        }
      } catch (error) {
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
// and one the project no longer has goes.
async function refreshThread(threadId: string): Promise<void> {
  const open = view.kind === "project" ? view : null;
  if (!open) return;
  const row = (await projects.threads(open.id, threadId)).find((found) => found.id === threadId);
  if (view !== open || overview?.project.id !== open.id) return;
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
    // A thread the pane has not listed yet, as one just started from its card: its row decides.
    if (chat !== undefined && !masters.has(chat)) return void openedThread(view, chat);
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
 * Start the device for *credential*. It counts as this computer's device at once: the commands'
 * environment comes from a login shell, which can take seconds, and the shell shows it as
 * connecting meanwhile. A start that fails leaves no device.
 */
function startStack(agent: Agent, credential: LiveCredential): Promise<DeviceStack> {
  const started = environment.then((env) => startDevice({
    journalPath: join(root, "devices", credential.deviceId, "journal.sqlite"),
    url: linkUrl(agent.origin),
    token: credential.token,
    agent: agent.name,
    identity: { deviceId: credential.deviceId, orgId: credential.orgId, agentId: credential.agentId, userId: credential.userId },
    // The tool layer under the binder: the file kinds in the root's file host, the process kinds in the VM.
    tools: (bindings, network) => new VmExecutor({ bindingOf: (bound) => bindings.get(bound), network, dataDir: root, env, vm: vmFor(env) }),
    prompts,
    approvalPrompts: prompts,
    onStatus: (status) => {
      if (device?.credential === credential) device.status = status;
      // The agent ended this token while it is this computer's: cleaned up here, its folders kept for a restore.
      if ((status === "revoked" || status === "unauthenticated") && kept === credential && rotating !== credential) void retire(credential);
      changed();
    },
    onWorking: (count) => {
      if (count > 0) return;
      for (const resume of idle) resume();
      idle.clear();
    },
    onError: report,
  }));
  const starting = { credential, status: "connecting" as LinkStatus, stack: null as DeviceStack | null, started };
  device = starting;
  changed();
  return started.then((stack) => {
    starting.stack = stack;
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
 * offers them. Kept while the agent is the one added; anything else is said and changes nothing.
 */
async function refreshAgent(agent: Agent): Promise<void> {
  try {
    const fresh = await readAgent(agent.origin, getFollowing);
    if (fresh.origin !== agent.origin || fresh.agentId !== agent.agentId) {
      report(new Error(`${agent.origin} now answers as agent ${fresh.agentId} at ${fresh.origin}: Surogate keeps the agent it added`));
      return;
    }
    if (fresh.desktopSessions === agent.desktopSessions && fresh.multiSession === agent.multiSession) return;
    // Removed while it was asked: it is not kept again.
    const now = agents.get();
    if (now?.origin !== agent.origin || now.agentId !== agent.agentId) return;
    // In place: the bridge and the device hold this agent.
    Object.assign(agent, { desktopSessions: fresh.desktopSessions, multiSession: fresh.multiSession });
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
}

async function ask(options: Electron.MessageBoxOptions): Promise<boolean> {
  const { response } = main ? await dialog.showMessageBox(main.window, options) : await dialog.showMessageBox(options);
  return response === 0;
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
  const confirmed = await ask(removing
    ? {
      type: "warning", message: `Remove ${agent.name} from Surogate?`,
      detail: `You are logged out, this computer's access to ${agent.name} ends, and the folders it was given here are forgotten. Surogate then asks for an agent again.${cutOff()}`,
      buttons: ["Remove", "Cancel"], defaultId: 1, cancelId: 1, noLink: true,
    }
    : {
      type: "warning", message: `Log out of ${agent.name}?`,
      detail: `This computer's access to ${agent.name} ends, and the folders it was given here are forgotten.${cutOff()}`,
      buttons: ["Log out", "Cancel"], defaultId: 1, cancelId: 1, noLink: true,
    });
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
    // Ended at the agent too, best effort: offline, the refresh token stays valid there until it expires.
    void ending?.end().catch(report);
    account = null;
    forgetAccount();
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

// What a window asked for, a folder or Work freely: its prompts go once that window goes or its page is replaced.
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
  // The device is the account's it was registered for: a page signed in as anyone else sees none.
  // Until the page says who it is, it is whoever is signed in to the app, whose sign-in gave it its session.
  const anotherAccount = () => {
    const owner = account ?? signedIn?.account ?? null;
    return kept !== null && (owner?.orgId !== kept.orgId || owner.userId !== kept.userId);
  };
  // The device, once started: a page asking while it still starts, as at a launch, waits for it.
  const registered = async (): Promise<DeviceStack> => {
    if (anotherAccount()) throw new Error("This computer is registered with the agent for another account");
    if (kept?.token === null) throw new Error("This computer's access to the agent was revoked: restore it from Surogate's window");
    if (!device) throw new Error("This computer is not registered with the agent");
    return device.stack ?? device.started;
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
    prepareFolder: (choice, window) => preparing(window, async (signal) => (await registered()).binder.prepareFolder(choice, window, signal)),
    bindSession: async (sessionId, token, window) => {
      await (await registered()).binder.bindSession(sessionId, token, window);
    },
    setMode: async (sessionId, mode) => (await registered()).binder.approvals.setMode(sessionId, mode),
    requestFreeMode: (sessionId, window) =>
      preparing(window, async (signal) => (await registered()).binder.approvals.requestFreeMode(sessionId, signal, `${window}:${load}`)),
    cancelPrepared: async (token, window) => (await registered()).binder.cancelPrepared(token, window),
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
  // A page that loads again starts with no source, until it registers one. Until its load commits,
  // the page there still serves: a load the shell cancels, as to an address outside the agent's, changes nothing.
  onReplaced(contents, () => {
    load += 1;
    if (served) withdrawProjects(false);
  });
}

function open(window: MainWindow, agent: Agent): void {
  const contents = window.attach(agent, BRIDGE_PRELOAD);
  bridge(contents, agent);
  contents.on("did-navigate", (_event, url) => navigated(url));
  contents.on("did-navigate-in-page", (_event, url, isMainFrame) => {
    if (isMainFrame) navigated(url);
  });
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
  const { response } = main ? await dialog.showMessageBox(main.window, options) : await dialog.showMessageBox(options);
  return response === 0;
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

function state() {
  const agent = agents.get();
  return {
    first: agent === null,
    agent: agent && { name: agent.name },
    device: agent && {
      text: deviceLine(agent),
      status: device?.status ?? (kept?.token === null ? "revoked" : null),
    },
    // Who the web client says is signed in, or until it has said, the app's own sign-in.
    account: account === undefined ? signedIn?.account ?? null : account,
    view,
    overview: view.kind === "project" && overview?.project.id === view.id ? overview : null,
    projects: listed,
    failure,
    links: Object.keys(links()),
    unreachable: main?.unreachable ?? null,
    notice: credentials.unencrypted() || sessionStore.unencrypted()
      ? "Credentials on this computer are not encrypted: Linux has no secret store here" : null,
    signIn: { needed: agent !== null && (signedIn === null || reloading), pending: signingIn !== null, failure: signInFailure },
    deviceAction: deviceAction(agent),
  };
}

// Held until it is clicked or closed: a notification nothing holds can lose its click.
let notice: Notification | null = null;

// A prompt waits while the window is hidden: the system's notification says so, and opens the window.
// It names nothing the agent sent: some notification services read markup in a body.
function notifyAsking(): void {
  if (!Notification.isSupported()) return;
  const shown = new Notification({ title: "Surogate is asking you something", body: "Open Surogate to answer." });
  notice = shown;
  const done = () => {
    if (notice === shown) notice = null;
  };
  shown.on("click", () => {
    done();
    main?.show();
  });
  shown.on("close", done);
  shown.show();
}

function popup(which: unknown): void {
  const shown = main;
  const agent = agents.get();
  if (!shown) return;
  const template: Electron.MenuItemConstructorOptions[] = which === "project"
    ? [
      { label: "Reload", click: () => shown.reload() },
      { label: "Open in browser", enabled: agent !== null, click: () => agent && void shell.openExternal(agent.origin) },
    ]
    : [
      { label: "Settings…", accelerator: "Ctrl+Shift+,", click: showSettings },
      { type: "separator" },
      { label: "Quit Surogate", accelerator: "Ctrl+Q", click: () => app.quit() },
    ];
  Menu.buildFromTemplate(template).popup({ window: shown.window });
}

function settingsState() {
  const agent = agents.get();
  return {
    appearance: appearance.get(),
    account,
    computer: {
      name: hostname(),
      connection: agent ? deviceLine(agent) : "",
      added: kept?.addedAt ?? null,
      organisation: account?.orgId ?? kept?.orgId ?? null,
      agents: agent ? [agent.name] : [],
    },
    links: { usage: "usage" in links() },
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

async function confirmArchive(name: string): Promise<boolean> {
  if (!main) return false;
  const { response } = await dialog.showMessageBox(main.window, {
    type: "warning",
    message: `Archive ${name}?`,
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
  main?.openSettings(page, PAGES_PRELOAD, (contents) => {
    projectDialog = contents;
    const handle = (channel: string, handler: (...args: unknown[]) => unknown) => {
      contents.ipc.handle(channel, (event, ...args: unknown[]) => {
        if (!ownPage(event.senderFrame, page)) throw new Error("Not the project dialog's own page");
        return handler(...args);
      });
    };
    const refused = (error: unknown) => (error instanceof Error ? error.message : String(error));
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
      try {
        const project = editing
          ? await projects.update(editing.id, fields)
          : await projects.create({ name: fields.name, goal: fields.goal });
        remember(project);
        close();
        // A new project opens on its conversation, as one chosen in the sidebar does.
        if (!editing && webClientPath(`/chat/${project.masterSessionId}`)) {
          choose();
          show({ kind: "project", id: project.id, name: project.name, masterSessionId: project.masterSessionId, thread: null });
          main?.showWeb(true);
          main?.go(`/chat/${project.masterSessionId}`);
        }
        void refreshProjects();
        return null;
      } catch (error) {
        return refused(error);
      }
    });
    handle("project:archive", async () => {
      if (!editing || !(await confirmArchive(shown?.name ?? editing.name))) return null;
      try {
        await projects.archive(editing.id);
      } catch (error) {
        return refused(error);
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

// Settings, over the window: its page's calls are answered on its own view only.
function showSettings(): void {
  const page = join(PAGES, "settings.html");
  main?.openSettings(page, PAGES_PRELOAD, (contents) => {
    const handle = (channel: string, handler: (...args: unknown[]) => unknown) => {
      contents.ipc.handle(channel, (event, ...args: unknown[]) => {
        if (!ownPage(event.senderFrame, page)) throw new Error("Not Settings' own page");
        return handler(...args);
      });
    };
    handle("settings:state", settingsState);
    // A theme in effect that changes reaches the web client through the theme's own paint.
    handle("settings:set", (key, value) => {
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
  handle("shell:connect", async (address) => {
    if (agents.get()) return "This app is already connected to an agent";
    if (connecting) return "Surogate is already connecting";
    if (typeof address !== "string" || address.length > 2048) return "That is not a web address";
    connecting = true;
    try {
      const agent = await connectAgent(address, { get: getFollowing, store: agents, confirm: confirmAgent });
      if (agent) open(window, agent);
      changed();
      return null;
    } catch (error) {
      return error instanceof Error ? error.message : String(error);
    } finally {
      connecting = false;
    }
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
    choose();
    show({ kind: "web" });
    window.showWeb(true);
    window.go(path);
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
    window.go(path);
    changed();
  });
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
  handle("shell:menu", popup);
  handle("shell:settings", showSettings);
  handle("shell:new-project", () => showProject(null));
  handle("shell:project-settings", () => {
    if (view.kind !== "project") throw new Error("No project is open");
    showProject({ id: view.id, name: view.name, masterSessionId: view.masterSessionId });
  });
  handle("shell:link", openLink);
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
  const shown = main?.window.isVisible() ? main.window : undefined;
  const { response } = shown ? await dialog.showMessageBox(shown, options) : await dialog.showMessageBox(options);
  return (["quit", "wait", "cancel"] as const)[response] ?? "cancel";
}

let quitting: Promise<void> | null = null;
// While a quit waits for the threads: what ends the wait at once.
let waiting: (() => void) | null = null;
let askingAgain = false;
let stopped = false;

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
  const shown = main?.window.isVisible() ? main.window : undefined;
  const { response } = shown ? await dialog.showMessageBox(shown, options) : await dialog.showMessageBox(options);
  if (response === 0) waiting?.();
}

async function quit(): Promise<void> {
  const working = device?.stack?.working() ?? 0;
  if (working > 0) {
    const answer = await confirmQuit(working);
    if (answer === "cancel") return;
    if (answer === "wait" && (device?.stack?.working() ?? 0) > 0) {
      await new Promise<void>((resume) => {
        waiting = resume;
        idle.add(resume);
      });
      waiting = null;
    }
  }
  // A sign-in under way closes its port in the browser's face, and keeps what the agent already
  // issued it; revocations still owed are tried at the next launch.
  signingIn?.abort(QUIT);
  await signedInOrStopped;
  // A log out under way finishes first: it keeps the revocation owed, and forgets the folders.
  await signingOut;
  await Promise.all([...revocations.values()].map((revoking) => revoking.stop()));
  // The device, then the VM, which stops even when the device's stop fails. A stop that
  // fails still quits: the next launch answers what it cut off.
  try {
    await stopDevice(device?.started, vm);
  } finally {
    stopped = true;
    app.quit();
  }
}

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => main?.show());
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
    sessionStore = new SessionStore(join(root, "session.json"), safeStorage, report);
    environment = appEnvironment();
    // Before the window: its first frame is in the chosen theme.
    theme = new Theme(nativeTheme, appearance, (dark) => {
      main?.paint(dark);
      tellAppearance();
    });
    onSettingsKey(showSettings);
    prompts = desktopPrompts({ parent: () => main?.window, page: join(PAGES, "prompt.html"), preload: PAGES_PRELOAD, unseen: notifyAsking });
    const page = join(PAGES, "shell.html");
    main = new MainWindow({ states, page, preload: PAGES_PRELOAD, dark: theme.dark, onChange: changed });
    wire(main, page);
    main.window.on("focus", () => void refreshProjects());
    // Revocations owed from an earlier run, whatever agent they were for: each keeps its agent's address, device and token.
    const stored = credentials.list();
    for (const owed of stored.filter((credential) => credential.revoking)) {
      // Its folders went with the log out, or go now, after a crash that cut it short.
      rmSync(join(root, "devices", owed.deviceId), { recursive: true, force: true });
      revokeLater(owed);
    }
    const agent = agents.get();
    if (!agent) return;
    const remembered = sessionStore.get();
    if (remembered?.origin === agent.origin && remembered.agentId === agent.agentId) startSession(remembered);
    // Gated before the web client is attached: with nobody signed in, it never shows.
    changed();
    // A window no sign-in of the app's owns has nobody signed in: whatever an older one left there goes.
    const shown = main;
    void (signedIn ? Promise.resolve() : clearWindow(agent)).catch(report).then(() => open(shown, agent));
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
