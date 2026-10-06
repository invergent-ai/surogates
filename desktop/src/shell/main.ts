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
import type { ProjectSummary } from "../../../web/src/lib/projects-contract.js";
import { verifyDevice } from "../device.js";
import { appEnvironment } from "../hosts/environment.js";
import { ToolHosts } from "../hosts/tool-hosts.js";
import type { LinkStatus } from "../link/client.js";
import { type Agent, AgentStore, connectAgent, describeAgent, linkUrl } from "./agents.js";
import { AppearanceStore, Theme } from "./appearance.js";
import { bridgeHandlers } from "./bridge.js";
import { type Credential, CredentialStore } from "./credentials.js";
import { type DeviceStack, startDevice } from "./device-stack.js";
import { letWindowClose, MainWindow } from "./main-window.js";
import { PageProjects } from "./projects.js";
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
// What the web client tells once its user signed in, and the projects it serves.
let account: DesktopAccount | null = null;
const projects = new PageProjects((message) => main?.webContents()?.send("desktop:projects", message));
let served = false;
let listed: ProjectSummary[] = [];

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

function changed(): void {
  main?.window.webContents.send("shell:changed");
}

const appearanceNow = () => ({ ...appearance.get(), theme: theme.dark ? ("dark" as const) : ("light" as const) });

// The projects the page serves, listed again: when it registers its source, and when the window comes to the front.
async function refreshProjects(): Promise<void> {
  if (!served) return;
  try {
    listed = await projects.list();
  } catch (error) {
    report(error);
  }
  changed();
}

// The page withdrew its projects (signed out), or went: nothing is asked of it until it registers again.
function withdrawProjects(signedOut: boolean): void {
  served = false;
  projects.withdrawn();
  if (signedOut) listed = [];
  changed();
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

// A folder is prepared for the window that asked: its prompts go once that window goes or navigates away.
async function preparing<T>(window: string, prepare: (signal: AbortSignal) => Promise<T>): Promise<T> {
  const contents = webContents.fromId(Number(window));
  const controller = new AbortController();
  const gone = () => controller.abort();
  const navigated = (details: { isMainFrame: boolean; isSameDocument: boolean }) => {
    if (details.isMainFrame && !details.isSameDocument) controller.abort();
  };
  if (!contents) controller.abort();
  contents?.once("destroyed", gone);
  contents?.on("did-start-navigation", navigated);
  try {
    return await prepare(controller.signal);
  } finally {
    contents?.off("destroyed", gone);
    contents?.off("did-start-navigation", navigated);
  }
}

// The bridge, on a view of the agent's web client: its calls answer for this agent only.
function bridge(contents: WebContents, agent: Agent): void {
  const registered = (): DeviceStack => {
    if (!device?.stack) throw new Error("This computer is not registered with the agent");
    return device.stack;
  };
  const handlers = bridgeHandlers(agent.origin, {
    getDevice: () => ({
      device: kept && { deviceId: kept.deviceId, name: kept.name },
      computerName: hostname(),
      localFolders: agent.desktopSessions && agent.multiSession,
    }),
    registerDevice: (token) => registerDevice(agent, token),
    prepareFolder: (choice, window) => preparing(window, (signal) => registered().binder.prepareFolder(choice, window, signal)),
    bindSession: async (sessionId, token, window) => {
      await registered().binder.bindSession(sessionId, token, window);
    },
    getAppearance: appearanceNow,
    setAccount: (reported) => {
      account = reported;
      changed();
    },
    registerProjects: (registered) => {
      if (!registered) return withdrawProjects(true);
      served = true;
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
  // A page that loads again starts with no source, until it registers one.
  contents.on("did-start-navigation", (details) => {
    if (details.isMainFrame && !details.isSameDocument && served) withdrawProjects(false);
  });
}

function open(window: MainWindow, agent: Agent): void {
  bridge(window.attach(agent, BRIDGE_PRELOAD), agent);
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
    projects: listed,
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
    : [{ label: "Quit Surogate", accelerator: "Ctrl+Q", click: () => app.quit() }];
  Menu.buildFromTemplate(template).popup({ window: shown.window });
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
    window.go(path);
  });
  handle("shell:back", () => window.back());
  handle("shell:forward", () => window.forward());
  handle("shell:reload", () => window.reload());
  handle("shell:place", (hole) => window.place(bounds(hole)));
  handle("shell:menu", popup);
}

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => main?.show());
  // The device link stays up with the window closed (spec, Section 7).
  app.on("window-all-closed", () => {});
  app.on("before-quit", letWindowClose);
  void app.whenReady().then(() => {
    credentials = new CredentialStore(join(root, "credentials.json"), safeStorage, report);
    environment = appEnvironment();
    // Before the window: its first frame is in the chosen theme.
    theme = new Theme(nativeTheme, appearance, (dark) => {
      main?.paint(dark);
      main?.webContents()?.send("desktop:appearance", appearanceNow());
    });
    const page = join(PAGES, "shell.html");
    main = new MainWindow({ states, page, preload: PAGES_PRELOAD, dark: theme.dark, onChange: changed });
    wire(main, page);
    main.window.on("focus", () => void refreshProjects());
    const agent = agents.get();
    if (!agent) return;
    open(main, agent);
    kept = credentials.list().find((stored) => stored.origin === agent.origin && stored.agentId === agent.agentId) ?? null;
    if (kept) void startStack(agent, kept).catch(report);
  });
}
