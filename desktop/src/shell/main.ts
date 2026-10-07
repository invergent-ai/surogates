// The desktop shell's main process (spec, Sections 1 and 7): one window, as Claude
// Desktop has, signed in to one agent, and this computer's device link to that agent,
// which stays up with the window closed. It never runs what a tool call asks for.
// Readiness is awaited with then(), never a top-level await: an ES module main that
// awaits app.whenReady() deadlocks.

import { rmSync } from "node:fs";
import { hostname, userInfo } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import {
  app, dialog, type IpcMainEvent, type IpcMainInvokeEvent, Menu, nativeTheme, net, safeStorage, session, shell, utilityProcess,
  type WebContents, webContents,
} from "electron";

import type { DesktopAccount } from "../../../web/src/lib/desktop-bridge-contract.js";
import type { LibraryEntry, Project, ProjectSummary, Routine, ThreadRow } from "../../../web/src/lib/projects-contract.js";
import { revokeDevice, verifyDevice } from "../device.js";
import { appEnvironment } from "../hosts/environment.js";
import type { LinkStatus } from "../link/client.js";
import { type FromManager, MANAGER, type ManagerProcess, type ToManager, VmClient, vmOptions } from "../vm/client.js";
import { VmExecutor } from "../vm/executor.js";
import { type Agent, AgentStore, connectAgent, describeAgent, linksFor, linkUrl, partitionFor } from "./agents.js";
import { AppearanceStore, Theme } from "./appearance.js";
import { bridgeHandlers } from "./bridge.js";
import { rebind, register } from "./computer.js";
import { type Credential, CredentialStore } from "./credentials.js";
import { type DeviceStack, startDevice, stopDevice } from "./device-stack.js";
import { letWindowClose, MainWindow, onSettingsKey } from "./main-window.js";
import { type Fetch, OAuthError, revokeTokens, signInWithBrowser, type Tokens } from "./oauth.js";
import { ANSWER_TIMEOUT_MS, PageProjects } from "./projects.js";
import { folderPrompts, refusingApprovals } from "./prompts.js";
import { accountOf, DesktopSession, SessionStore, type SignedIn } from "./session.js";
import { sameOrigin, webClientPath } from "./window-policy.js";
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
let sessionStore: SessionStore;
// Who is signed in to the app, with the agent: what adds this computer, and what the window's web client takes its session from.
let signedIn: DesktopSession | null = null;
// A sign-in under way in the system browser: starting another cancels it.
let signingIn: AbortController | null = null;
let signInFailure: string | null = null;
// The agent would add this computer only on a more recent sign-in: the user signs in again.
let signInAgain = false;
// The window's web client loads again with a new sign-in's session: the sign-in shows until it has.
let reloading = false;
// The app's calls to the agent, through Chromium's network as the window's are.
const apiFetch: Fetch = (url, init) => net.fetch(url, init);
// Devices logged out while the agent could not hear it, by device: each revoked on a link of its own once it can.
const revocations = new Map<string, { done: Promise<void>; stop(): Promise<void> }>();
let signingOut = false;
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
// The projects the page named, by their master session: the web client arriving at one shows that project.
const masters = new Map<string, Opened>();
// Each choice of what the centre shows takes a number: an answer that comes after a later choice applies nothing.
let choice = 0;
// Why the project last chosen did not open.
let failure: string | null = null;
// The open project, for the Overview pane, and what stops following it.
let overview: { project: Project; threads: ThreadRow[]; library: LibraryEntry[]; routines: Routine[] } | null = null;
let unfollow = (): void => {};

const report = (error: unknown): void => {
  console.error(error);
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

// A call from *page*, the window's own, in its top frame: no other page, a file dropped there included.
const fromPage = (event: IpcMainInvokeEvent, page: string): boolean => {
  const frame = event.senderFrame;
  if (frame?.parent !== null || !frame.url.startsWith("file:")) return false;
  return fileURLToPath(frame.url) === page;
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
let again = false;

// The projects the page serves, asked again: when it registers its source, when the open
// project changes, and when the window comes to the front. One refresh runs at a time, and
// asks once more for whatever changed meanwhile, so an older answer never lands last. The
// open project, once the page lists it no more, is left.
async function refreshProjects(): Promise<void> {
  if (!served) return;
  if (refreshing) {
    again = true;
    return;
  }
  refreshing = true;
  try {
    do {
      again = false;
      try {
        listed = await projects.list();
        const open = view.kind === "project" ? view : null;
        if (open && !listed.some((project) => project.id === open.id)) {
          overview = null;
          show({ kind: "web" });
        }
        await refreshOverview();
      } catch (error) {
        report(error);
      }
      changed();
    } while (again && served);
  } finally {
    refreshing = false;
  }
}

// The open project's threads, library and routines. A thread open in the centre that has left the
// project takes the centre back to the project's conversation.
async function refreshOverview(): Promise<void> {
  const open = view.kind === "project" ? view : null;
  if (!open) return;
  const [project, threads, library, routines] = await Promise.all([
    projects.get(open.id), projects.threads(open.id), projects.library(open.id), projects.routines(open.id),
  ]);
  remember(project);
  if (view !== open) return;
  overview = { project, threads, library, routines };
  const path = `/chat/${project.masterSessionId}`;
  if (open.thread && !threads.some((thread) => thread.id === open.thread?.id) && webClientPath(path)) {
    view = { ...open, thread: null };
    main?.go(path);
  }
}

// Follow the open project's changes, as Section 12's stream tells them, once more after the page registers again.
function follow(): void {
  unfollow();
  unfollow = view.kind === "project" ? projects.subscribe(view.id, () => void refreshProjects()) : () => {};
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

// The account's own: its projects, the masters its page named, the open project and its pane.
function forgetAccount(): void {
  listed = [];
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
  }
  const known = chat === undefined ? undefined : masters.get(chat);
  if (!known && view.kind === "web") return;
  show(known ? { kind: "project", ...known, thread: null } : { kind: "web" });
  void refreshProjects();
}

/**
 * Start the device for *credential*. It counts as this computer's device at once: the commands'
 * environment comes from a login shell, which can take seconds, and the shell shows it as
 * connecting meanwhile. A start that fails leaves no device.
 */
function startStack(agent: Agent, credential: Credential): Promise<DeviceStack> {
  const started = environment.then((env) => startDevice({
    journalPath: join(root, "devices", credential.deviceId, "journal.sqlite"),
    url: linkUrl(agent.origin),
    token: credential.token,
    agent: agent.name,
    identity: { deviceId: credential.deviceId, orgId: credential.orgId, agentId: credential.agentId, userId: credential.userId },
    // The tool layer under the binder: the file kinds in the root's file host, the process kinds in the VM.
    tools: (bindings, network) => new VmExecutor({ bindingOf: (bound) => bindings.get(bound), network, dataDir: root, env, vm: vmFor(env) }),
    prompts: folderPrompts(() => main?.window),
    approvalPrompts: refusingApprovals,
    onStatus: (status) => {
      if (device?.credential === credential) device.status = status;
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
const clearWindow = (agent: Agent): Promise<void> => session.fromPartition(partitionFor(agent.origin, agent.agentId)).clearStorageData();

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
 * Bind the sign-in that just happened to the computer kept for its account, before anything uses it:
 * revoking the computer then ends it too, and the window's session made from it. The device starts
 * again on the new token the agent issues for it. A sign-in that cannot be bound is ended rather than
 * run unbound.
 */
async function bindToComputer(agent: Agent): Promise<void> {
  const session = signedIn;
  const credential = kept;
  if (!session || !credential || credential.orgId !== session.account.orgId || credential.userId !== session.account.userId) return;
  try {
    const restored = await rebind({
      agent, session, credential,
      verify: (token) => verifyDevice(linkUrl(agent.origin), token),
      // The device on the old token goes first: the new one keeps the same journal.
      start: async (renewed) => {
        await stopDevice(device?.started, null);
        device = null;
        return startStack(agent, renewed);
      },
      save: (renewed) => credentials.save(renewed),
    });
    if (restored) kept = restored;
  } catch (error) {
    if (signedIn === session) signedIn = null;
    await session.end().catch(report);
    throw new Error(`Surogate could not tie this sign-in to this computer, so it signed out: ${error instanceof Error ? error.message : String(error)}`);
  }
}

/** Revoke *credential*'s device on a link of its own, with the link's backoff, until the agent hears it; then forget it. */
function revokeLater(credential: Credential): void {
  if (revocations.has(credential.deviceId)) return;
  const revoking = revokeDevice(linkUrl(credential.origin), credential.token);
  revocations.set(credential.deviceId, revoking);
  void revoking.done.then(() => {
    credentials.remove(credential.deviceId);
    revocations.delete(credential.deviceId);
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
  const stack = await ending?.started.catch(() => null);
  // A stop that fails still ends access here: the revocation is then owed, as when the agent could not hear it.
  if (stack && await stack.revoke().catch((error: unknown) => {
    report(error);
    return false;
  })) {
    credentials.remove(credential.deviceId);
  } else {
    credentials.save({ ...credential, revoking: true });
    revokeLater(credential);
  }
  rmSync(join(root, "devices", credential.deviceId), { recursive: true, force: true });
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
      buttons: ["Remove", "Cancel"], defaultId: 0, cancelId: 1, noLink: true,
    }
    : {
      type: "warning", message: `Log out of ${agent.name}?`,
      detail: `This computer's access to ${agent.name} ends, and the folders it was given here are forgotten.${cutOff()}`,
      buttons: ["Log out", "Cancel"], defaultId: 0, cancelId: 1, noLink: true,
    });
  if (!confirmed) return;
  signingOut = true;
  try {
    signingIn?.abort();
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
    signingOut = false;
    changed();
  }
}

/** Sign in to the agent in the system browser. A sign-in started again cancels the one under way. */
async function signIn(agent: Agent): Promise<void> {
  signingIn?.abort();
  const attempt = new AbortController();
  signingIn = attempt;
  signInFailure = null;
  changed();
  try {
    const tokens = await signInWithBrowser({
      origin: agent.origin, computer: hostname(), fetch: apiFetch, signal: attempt.signal, open: (url) => shell.openExternal(url),
    });
    const who = await accountOf(agent.origin, tokens.accessToken, apiFetch);
    // This computer works for one account at a time: another's access here ends first, once the user agrees.
    const previous = kept;
    if (previous && (previous.orgId !== who.orgId || previous.userId !== who.userId)) {
      const switching = await ask({
        type: "warning", message: `Sign in as ${who.email}?`,
        detail: `This computer works for another account of ${agent.name}. Signing in as ${who.email} ends that account's access here, and forgets the folders it was given.`,
        buttons: ["Sign in", "Cancel"], defaultId: 1, cancelId: 1, noLink: true,
      });
      if (!switching) {
        void revokeTokens(agent.origin, tokens.refreshToken, apiFetch).catch(report);
        signInFailure = `Not signed in as ${who.email}: this computer keeps working for the account that added it`;
        return;
      }
      await endDevice(previous);
    }
    // The sign-in this one replaces ends at the agent: none is left valid with no copy here.
    void signedIn?.end().catch(report);
    const signedInNow: SignedIn = { origin: agent.origin, agentId: agent.agentId, account: who, authTime: tokens.authTime, refreshToken: tokens.refreshToken };
    sessionStore.save(signedInNow);
    startSession(signedInNow, tokens);
    await bindToComputer(agent);
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
    void registerComputer(agent);
  } catch (error) {
    if (!(error instanceof OAuthError && error.code === "cancelled")) {
      signInFailure = error instanceof Error ? error.message : String(error);
    }
  } finally {
    if (signingIn === attempt) signingIn = null;
    changed();
  }
}

// A folder is prepared for the window that asked: its prompts go once that window goes or its page is replaced.
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
  // The device is the account's it was registered for: a page signed in as anyone else sees none.
  // Until the page says who it is, it is whoever is signed in to the app, whose sign-in gave it its session.
  const anotherAccount = () => {
    const owner = account ?? signedIn?.account ?? null;
    return kept !== null && (owner?.orgId !== kept.orgId || owner.userId !== kept.userId);
  };
  const registered = (): DeviceStack => {
    if (anotherAccount()) throw new Error("This computer is registered with the agent for another account");
    if (!device?.stack) throw new Error("This computer is not registered with the agent");
    return device.stack;
  };
  const handlers = bridgeHandlers(agent.origin, {
    getDevice: () => ({
      device: kept && !anotherAccount() ? { deviceId: kept.deviceId, name: kept.name } : null,
      localFolders: !anotherAccount() && agent.desktopSessions && agent.multiSession,
    }),
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
    prepareFolder: (choice, window) => preparing(window, (signal) => registered().binder.prepareFolder(choice, window, signal)),
    bindSession: async (sessionId, token, window) => {
      await registered().binder.bindSession(sessionId, token, window);
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
  // A page that loads again starts with no source, until it registers one. Until its load commits,
  // the page there still serves: a load the shell cancels, as to an address outside the agent's, changes nothing.
  onReplaced(contents, () => {
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
function deviceAction(agent: Agent | null): { text: string; button: string; action: "sign-in" } | null {
  if (!agent || !signedIn || !signInAgain) return null;
  return { text: `Sign in again to let ${agent.name} work on folders of this computer.`, button: "Sign in again", action: "sign-in" };
}

function state() {
  const agent = agents.get();
  return {
    first: agent === null,
    agent: agent && { name: agent.name },
    device: agent && {
      text: describeAgent(agent, device && { status: device.status, computer: device.credential.name }),
      status: device?.status ?? null,
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
      connection: agent ? describeAgent(agent, device && { status: device.status, computer: device.credential.name }) : "",
      added: kept?.addedAt ?? null,
      organisation: account?.orgId ?? kept?.orgId ?? null,
      agents: agent ? [agent.name] : [],
    },
    links: { usage: "usage" in links() },
  };
}

// Settings, over the window: its page's calls are answered on its own view only.
function showSettings(): void {
  const page = join(PAGES, "settings.html");
  main?.openSettings(page, PAGES_PRELOAD, (contents) => {
    const handle = (channel: string, handler: (...args: unknown[]) => unknown) => {
      contents.ipc.handle(channel, (event, ...args: unknown[]) => {
        if (!fromPage(event, page)) throw new Error("Not Settings' own page");
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
      if (!fromPage(event, page)) throw new Error("Not the window's own page");
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
      const agent = await connectAgent(address, { fetch: (url) => net.fetch(url), store: agents, confirm: confirmAgent });
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
  // A thread of the open project, in the centre.
  handle("shell:thread", (id) => {
    const thread = view.kind === "project" && overview?.project.id === view.id
      ? overview.threads.find((found) => found.id === id) : undefined;
    const path = `/chat/${String(id)}`;
    if (view.kind !== "project" || !thread || !webClientPath(path)) throw new Error("No such thread in the open project");
    choose();
    view = { ...view, thread: { id: thread.id, title: thread.title } };
    window.go(path);
    changed();
  });
  // Back and Forward move the web client; on the Projects page they leave it, for the client where it is.
  const move = (step: () => void) => {
    choose();
    if (view.kind !== "projects") return step();
    show({ kind: "web" });
    window.showWeb(true);
    const url = window.webContents()?.getURL();
    if (url) navigated(url);
  };
  handle("shell:back", () => move(() => window.back()));
  handle("shell:forward", () => move(() => window.forward()));
  handle("shell:reload", () => window.reload());
  handle("shell:place", (hole) => window.place(bounds(hole)));
  handle("shell:menu", popup);
  handle("shell:settings", showSettings);
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
  // A sign-in under way closes its port in the browser's face; revocations still owed are tried at the next launch.
  signingIn?.abort();
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
    const page = join(PAGES, "shell.html");
    main = new MainWindow({ states, page, preload: PAGES_PRELOAD, dark: theme.dark, onChange: changed });
    wire(main, page);
    main.window.on("focus", () => void refreshProjects());
    // Revocations owed from an earlier run, whatever agent they were for: each keeps its agent's address, device and token.
    const stored = credentials.list();
    for (const owed of stored.filter((credential) => credential.revoking)) revokeLater(owed);
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
    if (kept) void startStack(agent, kept).catch(report);
    // An earlier try to add this computer did not end in a device: try again, once.
    void registerComputer(agent);
  }).catch((error: unknown) => {
    // A start that fails is said, and ends the app: one left with no window would keep the
    // single-instance lock, and every later launch would hand it nothing.
    report(error);
    app.exit(1);
  });
}
