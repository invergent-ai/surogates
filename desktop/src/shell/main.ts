// The desktop shell's main process (spec, Sections 1 and 7): one window, as Claude
// Desktop has, signed in to one agent, and this computer's device link to that agent,
// which stays up with the window closed. It never runs what a tool call asks for.
// Readiness is awaited with then(), never a top-level await: an ES module main that
// awaits app.whenReady() deadlocks.

import { hostname } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import {
  app, dialog, type IpcMainEvent, type IpcMainInvokeEvent, Menu, nativeTheme, net, safeStorage, shell, type WebContents,
  webContents,
} from "electron";

import type { DesktopAccount, DesktopDevice } from "../../../web/src/lib/desktop-bridge-contract.js";
import type { LibraryEntry, Project, ProjectSummary, Routine, ThreadRow } from "../../../web/src/lib/projects-contract.js";
import { verifyDevice } from "../device.js";
import { appEnvironment } from "../hosts/environment.js";
import { ToolHosts } from "../hosts/tool-hosts.js";
import type { LinkStatus } from "../link/client.js";
import { type Agent, AgentStore, connectAgent, describeAgent, linksFor, linkUrl } from "./agents.js";
import { AppearanceStore, Theme } from "./appearance.js";
import { bridgeHandlers } from "./bridge.js";
import { type Credential, CredentialStore } from "./credentials.js";
import { type DeviceStack, startDevice } from "./device-stack.js";
import { letWindowClose, MainWindow, onSettingsKey } from "./main-window.js";
import { ANSWER_TIMEOUT_MS, PageProjects } from "./projects.js";
import { folderPrompts, refusingApprovals } from "./prompts.js";
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
// What the web client tells once its user signed in, and the projects it serves.
let account: DesktopAccount | null = null;
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
 * environment comes from a login shell, which can take seconds, and a page asking meanwhile must
 * not register a second device. A start that fails leaves no device.
 */
function startStack(agent: Agent, credential: Credential): Promise<DeviceStack> {
  const started = environment.then((env) => startDevice({
    journalPath: join(root, "devices", credential.deviceId, "journal.sqlite"),
    url: linkUrl(agent.origin),
    token: credential.token,
    agent: agent.name,
    identity: { deviceId: credential.deviceId, orgId: credential.orgId, agentId: credential.agentId, userId: credential.userId },
    // The tool layer under the binder: srt's tool hosts until the VM's executor replaces them.
    tools: (bindings, network) => new ToolHosts({ bindingOf: (bound) => bindings.get(bound), network, dataDir: root, env }),
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

async function registerDevice(agent: Agent, token: string): Promise<DesktopDevice> {
  // ponytail: one device per agent on this computer; signing out and restoring access come with sign-in in the system browser.
  if (kept || registering) throw new Error("This computer is already registered with this agent");
  const signedIn = account;
  if (!signedIn) throw new Error("Sign in to the agent before registering this computer");
  registering = true;
  try {
    const welcome = await verifyDevice(linkUrl(agent.origin), token);
    if (welcome.agentId !== agent.agentId) throw new Error("This token is for another agent");
    if (welcome.orgId !== signedIn.orgId || welcome.userId !== signedIn.userId) throw new Error("This token is for another user");
    const credential: Credential = {
      origin: agent.origin, orgId: welcome.orgId, agentId: welcome.agentId, userId: welcome.userId,
      deviceId: welcome.deviceId, name: welcome.name, addedAt: new Date().toISOString(), token,
    };
    // Kept only once its device runs: a start that fails leaves nothing behind.
    const stack = await startStack(agent, credential);
    try {
      credentials.save(credential);
    } catch (error) {
      device = null;
      await stack.stop();
      throw error;
    }
    kept = credential;
    changed();
    return { deviceId: credential.deviceId, name: credential.name };
  } finally {
    registering = false;
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
  const anotherAccount = () => kept !== null && (account?.orgId !== kept.orgId || account.userId !== kept.userId);
  const registered = (): DeviceStack => {
    if (anotherAccount()) throw new Error("This computer is registered with the agent for another account");
    if (!device?.stack) throw new Error("This computer is not registered with the agent");
    return device.stack;
  };
  const handlers = bridgeHandlers(agent.origin, {
    getDevice: () => ({
      device: kept && !anotherAccount() ? { deviceId: kept.deviceId, name: kept.name } : null,
      computerName: hostname(),
      localFolders: !anotherAccount() && agent.desktopSessions && agent.multiSession,
    }),
    registerDevice: (token) => registerDevice(agent, token),
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

function state() {
  const agent = agents.get();
  return {
    first: agent === null,
    agent: agent && { name: agent.name },
    device: agent && {
      text: describeAgent(agent, device && { status: device.status, computer: device.credential.name }),
      status: device?.status ?? null,
    },
    account,
    view,
    overview: view.kind === "project" && overview?.project.id === view.id ? overview : null,
    projects: listed,
    failure,
    links: Object.keys(links()),
    unreachable: main?.unreachable ?? null,
    notice: credentials.unencrypted() ? "Credentials on this computer are not encrypted: Linux has no secret store here" : null,
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
  // A device still starting is stopped once it has started. A stop that fails still quits:
  // the next launch answers what it cut off.
  try {
    await device?.started.then((stack) => stack.stop(), () => {});
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
    const agent = agents.get();
    if (!agent) return;
    open(main, agent);
    kept = credentials.list().find((stored) => stored.origin === agent.origin && stored.agentId === agent.agentId) ?? null;
    if (kept) void startStack(agent, kept).catch(report);
  }).catch((error: unknown) => {
    // A start that fails is said, and ends the app: one left with no window would keep the
    // single-instance lock, and every later launch would hand it nothing.
    report(error);
    app.exit(1);
  });
}
