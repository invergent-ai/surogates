// Launching the built shell under Playwright, with its state in a folder of the test's own.

import { spawn } from "node:child_process";
import { once } from "node:events";
import { mkdirSync, mkdtempSync, rmSync } from "node:fs";
import { createRequire } from "node:module";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { FuseState, FuseV1Options, getCurrentFuseWire } from "@electron/fuses";
import { _electron, type ElectronApplication, type Page } from "playwright-core";
import { afterAll, expect } from "vitest";

// The electron package's main is the path of its binary.
export const ELECTRON = createRequire(import.meta.url)("electron") as string;
export const MAIN = join(import.meta.dirname, "..", "..", "dist", "shell", "main.js");

// Short, under /tmp: a tool host's socket path under the state root must stay within 107 bytes.
export const dataHome = (): string => mkdtempSync("/tmp/sd-");

// Each data home's runtime folder, made once: short, under /tmp, as QEMU's control socket under it must
// stay within 108 bytes. They go with the test file, once its apps have quit.
const runtimes = new Map<string, string>();
afterAll(() => {
  for (const runtime of runtimes.values()) rmSync(runtime, { recursive: true, force: true });
  runtimes.clear();
});

// What a test app takes of the caller's environment, and nothing else: no agent, keyring, session or
// desktop of the user's reaches it. In a Wayland session Electron would draw on the user's desktop:
// WAYLAND_DISPLAY is left out with the rest, so as an X11 session it draws on xvfb's display.
const TAKEN = ["PATH", "DISPLAY", "XAUTHORITY", "SUROGATE_VM_IMAGE"];

/**
 * The environment of every app a test launches: a session of its own under *home*, the test's
 * data home. It has no session bus, so it reaches and starts no keyring daemon, portal, gvfsd or
 * at-spi; its own home, runtime, config, cache, state and temp folders; and X11, on xvfb's display.
 * It throws on any other display, before anything is made or started.
 */
export function shellEnv(home: string): Record<string, string> {
  // xvfb-run -a sets both: its display, never the desktop's :0, and an authority in a folder of its own.
  const { DISPLAY: display, XAUTHORITY: authority } = process.env;
  if (!display || display === ":0" || !/^:\d+$/.test(display) || !authority?.startsWith(join(tmpdir(), "xvfb-run."))) {
    throw new Error(`A test app runs only on an Xvfb's display, under xvfb-run -a: DISPLAY is ${display ?? "unset"}, XAUTHORITY ${authority ?? "unset"}`);
  }
  const env: Record<string, string> = { LANG: process.env.LANG || "C.UTF-8" };
  for (const name of TAKEN) {
    const value = process.env[name];
    if (value) env[name] = value;
  }
  // The temp folder goes with the data home: what the app's browser and Playwright leave there, a killed one's too.
  const [own, config, cache, state, temp] = ["h", "c", "k", "s", "t"].map((name) => join(home, name));
  for (const folder of [own, config, cache, state, temp]) mkdirSync(folder!, { recursive: true, mode: 0o700 });
  // 0700, as mkdtemp makes it.
  const runtime = runtimes.get(home) ?? mkdtempSync("/tmp/rt-");
  runtimes.set(home, runtime);
  return {
    ...env,
    HOME: own!, XDG_RUNTIME_DIR: runtime, XDG_CONFIG_HOME: config!, XDG_CACHE_HOME: cache!, XDG_STATE_HOME: state!, XDG_DATA_HOME: home, TMPDIR: temp!,
    DBUS_SESSION_BUS_ADDRESS: "disabled:", NO_AT_BRIDGE: "1", XDG_SESSION_TYPE: "x11", GDK_BACKEND: "x11",
  };
}

/**
 * A second launch with *args*, as the system's link handler starts one: in the test's session,
 * with the basic store, it hands the running app its arguments and exits. Its exit code. One that
 * has not exited within 10 s found no app to hand them to, and became the app: it is killed, and
 * the test is told.
 */
export async function secondLaunch(home: string, ...args: string[]): Promise<number | null> {
  await appsElectron();
  const second = spawn(ELECTRON, [MAIN, "--password-store=basic", ...args], { env: shellEnv(home), stdio: "ignore", detached: true });
  let late: NodeJS.Timeout | undefined;
  const code = await Promise.race([
    once(second, "exit").then(([exited]) => exited as number | null),
    new Promise<"running">((resolve) => {
      late = setTimeout(resolve, 10_000, "running");
    }),
  ]);
  clearTimeout(late);
  if (code === "running") process.kill(-second.pid!, "SIGKILL");
  await gone(second.pid);
  if (code === "running") throw new Error("A second launch found no app to hand its arguments to, and was killed after 10 s");
  return code;
}

/**
 * Wait for the process group *pid* leads to end: an app a test started, with Electron's own children,
 * which can write to its data home for a moment after it has exited. What is left of it after 10 s is
 * killed, so no test leaves a process running, or a data home made again once it was removed.
 */
export async function gone(pid: number | undefined): Promise<void> {
  if (pid === undefined) return;
  const killAt = Date.now() + 10_000;
  for (;;) {
    try {
      process.kill(-pid, Date.now() < killAt ? 0 : "SIGKILL");
    } catch {
      // ESRCH: nothing of it is left.
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
}

/** Close the app; one that does not close within 10 s is killed, so no failed test leaves it running. */
export async function quit(shell: ElectronApplication | undefined): Promise<void> {
  if (!shell) return;
  const child = shell.process();
  await Promise.race([shell.close().catch(() => {}), new Promise((resolve) => setTimeout(resolve, 10_000))]);
  if (child.exitCode === null && child.signalCode === null) child.kill("SIGKILL");
  // Playwright starts it as the leader of a process group of its own.
  await gone(child.pid);
}

// This package's Electron is the app's, RunAsNode off, or no test of it means anything. Read once a run,
// and awaited before every start of it: launch(), secondLaunch() and a test's own spawn.
let fused: Promise<void> | undefined;
export const appsElectron = (): Promise<void> => (fused ??= getCurrentFuseWire(ELECTRON).then((wire) => {
  if (wire[FuseV1Options.RunAsNode] !== FuseState.DISABLE) throw new Error(`${ELECTRON} still runs as Node: run npm run electron:install`);
}));

/**
 * Launch the shell with its state under *home*; *env* adds to its environment, and *args* to its
 * arguments, as the system adds a link it opens. Each of *requires*, a script of the test's, runs
 * in the main process before the app's own code, after Playwright's.
 */
export async function launch(home: string, env: Record<string, string> = {}, args: string[] = [], requires: string[] = []): Promise<ElectronApplication> {
  await appsElectron();
  return _electron.launch({
    executablePath: ELECTRON,
    // The basic store: no test leaves an item in the user's keyring.
    args: [...requires.flatMap((script) => ["-r", script]), MAIN, "--password-store=basic", ...args],
    env: { ...shellEnv(home), ...env },
    // Playwright adds --no-sandbox unless told: the app never runs without its sandbox.
    chromiumSandbox: true,
    // Playwright emulates a light system unless told: the pages follow the app's own theme.
    colorScheme: null,
  });
}

// The native confirmations answer globalThis.answer, their first button unless a test sets
// another, with their checkbox ticked when globalThis.checked is, and with globalThis.hold set,
// only once globalThis.release() is called; the folder dialog picks globalThis.folder, or is
// cancelled; nothing leaves for the system browser.
// What each was asked is kept in the main process, as globalThis.asked and .opened.
export async function stubNative(shell: ElectronApplication): Promise<void> {
  await shell.evaluate(({ dialog, shell: electronShell }) => {
    const asked: unknown[] = [];
    const opened: string[] = [];
    Object.assign(globalThis, { asked, opened, answer: 0 });
    const chosen = globalThis as unknown as { answer: number; checked?: boolean; folder?: string; hold?: boolean; release?: () => void };
    dialog.showMessageBox = ((...args: unknown[]) => {
      asked.push(args.at(-1));
      const answered = () => ({ response: chosen.answer, checkboxChecked: chosen.checked === true });
      if (!chosen.hold) return Promise.resolve(answered());
      return new Promise((resolve) => {
        chosen.release = () => resolve(answered());
      });
    }) as typeof dialog.showMessageBox;
    dialog.showOpenDialog = (() => Promise.resolve({ canceled: !chosen.folder, filePaths: chosen.folder ? [chosen.folder] : [] })) as
      unknown as typeof dialog.showOpenDialog;
    electronShell.openExternal = (url: string) => {
      opened.push(url);
      return Promise.resolve();
    };
  });
}

// The window's own page. Playwright's first window may be the agent's web client instead,
// once there is an agent: its view is made with the window.
export async function shellPage(shell: ElectronApplication): Promise<Page> {
  let found: Page | undefined;
  await expect.poll(() => {
    found = shell.windows().find((page) => page.url().endsWith("/shell.html"));
    return found !== undefined;
  }).toBe(true);
  return found!;
}

// The desktop's prompt open now, once drawn: the folder sheet, an approval, a confirmation.
export async function prompt(shell: ElectronApplication): Promise<Page> {
  let found: Page | undefined;
  await expect.poll(() => {
    found = shell.windows().find((page) => page.url().endsWith("/prompt.html"));
    return found !== undefined;
  }).toBe(true);
  await found!.waitForSelector("#prompt-buttons button");
  return found!;
}

// The prompt answered: its window has gone, so the next prompt() finds the next one.
async function closed(page: Page): Promise<void> {
  if (!page.isClosed()) await page.waitForEvent("close", { timeout: 5_000 });
}

/** Whether *page*'s prompt holds its buttons back: within its input protection, of its showing or of the last key or press. */
export const heldBack = (page: Page): Promise<boolean> =>
  page.$eval("#prompt-buttons", (row) => row.children.length === 0 || (row as HTMLElement).dataset.held !== "false");

/** Press *button* on *page*'s prompt, once its input protection lets it, and wait for the prompt to close. */
export async function press(page: Page, button: string): Promise<void> {
  const selector = `#prompt-buttons button[data-id="${button}"]`;
  await expect.poll(() => heldBack(page)).toBe(false);
  // The window can close before the click is acknowledged: that it closed is what tells it answered.
  await page.click(selector, { noWaitAfter: true }).catch(() => {});
  await closed(page);
}

// How many of the app's prompts are up on the screen.
export const promptsShown = (shell: ElectronApplication) => shell.evaluate(({ BrowserWindow }) =>
  BrowserWindow.getAllWindows().filter((window) => window.isVisible() && window.webContents.getURL().endsWith("/prompt.html")).length);

/**
 * Press *key* on *page*'s prompt, once its input protection lets it, which answers it: its window closes
 * before the key comes up, and can close before the key's press is acknowledged. That it closed is what
 * tells it answered.
 */
export async function key(page: Page, name: string): Promise<void> {
  await expect.poll(() => heldBack(page)).toBe(false);
  await page.keyboard.down(name).catch(() => {});
  await page.keyboard.up(name).catch(() => {});
  await closed(page);
}

// The system's notifications, as the app raises them: kept in the main process as globalThis.notices,
// never shown. A test clicks one by emitting its click, as the notification service would.
export async function stubNotifications(shell: ElectronApplication): Promise<void> {
  await shell.evaluate(({ Notification }) => {
    const notices: Electron.Notification[] = [];
    Object.assign(globalThis, { notices });
    Notification.prototype.show = function show(this: Electron.Notification) {
      notices.push(this);
    };
  });
}

// What each notification raised so far says.
export const notices = (shell: ElectronApplication) => shell.evaluate(() =>
  (globalThis as unknown as { notices: Electron.Notification[] }).notices.map(({ title, body }) => ({ title, body })));

// The user clicks the *index*th notification raised.
export const clickNotice = (shell: ElectronApplication, index: number) => shell.evaluate((_electron, at) => {
  (globalThis as unknown as { notices: Electron.Notification[] }).notices[at]!.emit("click");
}, index);

// What the app sets on its tray from now on: each icon, by file name, and the latest menu. Electron lists no
// trays, so a test reads what the app sets on its own.
export async function watchTray(shell: ElectronApplication): Promise<void> {
  await shell.evaluate(({ Tray }) => {
    const icons: string[] = [];
    Object.assign(globalThis, { icons, trayMenu: null });
    const setImage = Tray.prototype.setImage;
    Tray.prototype.setImage = function (this: Electron.Tray, image: Electron.NativeImage | string) {
      icons.push(String(image).split("/").at(-1)!);
      setImage.call(this, image);
    };
    const setContextMenu = Tray.prototype.setContextMenu;
    Tray.prototype.setContextMenu = function (this: Electron.Tray, menu: Electron.Menu | null) {
      Object.assign(globalThis, { trayMenu: menu });
      setContextMenu.call(this, menu);
    };
  });
}

export const icons = (shell: ElectronApplication) => shell.evaluate(() => (globalThis as unknown as { icons: string[] }).icons);
export const trayLabels = (shell: ElectronApplication) => shell.evaluate(() =>
  (globalThis as unknown as { trayMenu: Electron.Menu | null }).trayMenu?.items.map((item) => item.label) ?? null);
export const pickInTray = (shell: ElectronApplication, label: string) => shell.evaluate((_electron, chosen) => {
  (globalThis as unknown as { trayMenu: Electron.Menu }).trayMenu.items.find((item) => item.label === chosen)!.click();
}, label);
