import { spawn } from "node:child_process";
import { once } from "node:events";
import { mkdirSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

import type { ElectronApplication } from "playwright-core";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { dataHome, ELECTRON, launch, MAIN, quit, secondLaunch, shellEnv, shellPage } from "./launch.js";

let home: string;
let app: ElectronApplication | undefined;

beforeEach(() => {
  home = dataHome();
});

afterEach(async () => {
  await quit(app);
  app = undefined;
  rmSync(home, { recursive: true, force: true });
});

// The app's keys are its menu's, which reach only the window with the focus, as the keyboard does.
const focused = (shell: ElectronApplication) =>
  expect.poll(() => shell.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0]!.isFocused())).toBe(true);

// An agent kept from an earlier run, at an address nothing answers on, and its user's sign-in, as
// the basic store keeps it: the window shows its three columns.
function seedAgent(): void {
  mkdirSync(join(home, "surogate"), { recursive: true });
  writeFileSync(join(home, "surogate", "agent.json"), JSON.stringify({
    origin: "http://127.0.0.1:9", agentId: "a", name: "127.0.0.1:9", desktopSessions: true, multiSession: true,
  }));
  writeFileSync(join(home, "surogate", "session.json"), JSON.stringify({
    origin: "http://127.0.0.1:9", agentId: "a", authTime: 1_700_000_000, plain: "surg_rt_seeded",
    account: { name: "Flavius Burca", email: "flavius@example.com", userId: "u", orgId: "o" },
  }));
}

const visible = (shell: ElectronApplication) =>
  shell.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().map((window) => window.isVisible()));

describe("the shell", () => {
  it("runs in a session of the test's own: no bus, its own folders, X11 on xvfb, and the basic store, a second launch too", async () => {
    const own = (environment: Record<string, string | undefined>) => {
      expect(environment).toMatchObject({ DBUS_SESSION_BUS_ADDRESS: "disabled:", XDG_SESSION_TYPE: "x11", GDK_BACKEND: "x11", XDG_DATA_HOME: home });
      for (const name of ["HOME", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"]) {
        expect(environment[name]?.startsWith(`${home}/`), name).toBe(true);
      }
      expect(statSync(environment.XDG_RUNTIME_DIR!).mode & 0o777).toBe(0o700);
      expect(environment.WAYLAND_DISPLAY).toBeUndefined();
      // xvfb's display, never the desktop's.
      expect(environment.DISPLAY).toMatch(/^:\d+$/);
      expect(environment.DISPLAY).not.toBe(":0");
    };
    // Checked before anything launches: an app started without it would reach the desktop's session.
    own(shellEnv(home));
    app = await launch(home);
    await app.firstWindow();
    own(await app.evaluate(() => ({ ...process.env })));
    expect(await app.evaluate(() => process.argv)).toContain("--password-store=basic");
    const handed = app.evaluate(({ app: electron }) => new Promise<string[]>((resolve) => {
      electron.once("second-instance", (_event, argv) => resolve(argv));
    }));
    expect(await secondLaunch(home)).toBe(0);
    expect(await handed).toContain("--password-store=basic");
  });

  it("opens its window in its sandbox, with its state under one root", async () => {
    app = await launch(home);
    const page = await app.firstWindow();
    expect(await page.title()).toBe("Surogate");
    expect(await app.evaluate(({ app: shell }) => [
      shell.getName(), shell.getPath("userData"), shell.commandLine.hasSwitch("no-sandbox"),
    ])).toEqual(["Surogate", join(home, "surogate", "electron"), false]);
    const pid = await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0]!.webContents.getOSProcessId());
    expect(readFileSync(`/proc/${pid}/status`, "utf8")).toMatch(/^Seccomp:\s+2$/m);
    // On xvfb's display, never on the desktop the tests run from.
    expect(readFileSync(`/proc/${pid}/cmdline`, "utf8")).not.toContain("--ozone-platform=wayland");
  });

  it("is one window of three columns with no native frame, under the system's own controls", async () => {
    seedAgent();
    app = await launch(home);
    const page = await shellPage(app);
    await page.waitForSelector("#panel #close-panel");
    const layout = await page.evaluate(() => {
      const box = (selector: string) => document.querySelector(selector)!.getBoundingClientRect().toJSON() as DOMRect;
      // The Window Controls Overlay API, which TypeScript's DOM library does not have yet.
      const overlay = (navigator as unknown as { windowControlsOverlay: { visible: boolean; getTitlebarAreaRect(): DOMRect } })
        .windowControlsOverlay;
      return {
        overlay: overlay.visible, titleBar: overlay.getTitlebarAreaRect().toJSON() as DOMRect,
        sidebar: box("#sidebar"), centre: box("#centre"), panel: box("#panel"), close: box("#close-panel"),
      };
    });
    expect(layout.overlay).toBe(true);
    // Side by side, to within the sub-pixel a fractional width leaves.
    expect(layout.sidebar.right).toBeCloseTo(layout.centre.left, 1);
    expect(layout.centre.right).toBeCloseTo(layout.panel.left, 1);
    expect(Math.min(layout.sidebar.width, layout.centre.width, layout.panel.width)).toBeGreaterThan(200);
    // The controls take the top right corner, right of the title bar's area: nothing of the panel's sits under them.
    const controls = { left: layout.titleBar.x + layout.titleBar.width, bottom: layout.titleBar.y + layout.titleBar.height };
    expect(controls.bottom).toBe(40);
    expect(layout.close.right <= controls.left || layout.close.top >= controls.bottom).toBe(true);
  });

  it("opens where it was left, and never smaller than 960 by 600", async () => {
    app = await launch(home);
    await app.firstWindow();
    const shape = () => app!.evaluate(({ BrowserWindow }) => {
      const shown = BrowserWindow.getAllWindows()[0]!;
      return { minimum: shown.getMinimumSize(), bounds: shown.getBounds() };
    });
    expect((await shape()).minimum).toEqual([960, 600]);
    await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0]!.setBounds({ x: 40, y: 30, width: 1100, height: 700 }));
    const left = (await shape()).bounds;
    await app.close();
    app = await launch(home);
    await app.firstWindow();
    expect((await shape()).bounds).toEqual(left);
  });

  it("keeps its own page: a dropped file or a link cannot replace it, open a window or be granted anything", async () => {
    app = await launch(home);
    const page = await app.firstWindow();
    await page.waitForLoadState();
    const dropped = join(home, "dropped.html");
    writeFileSync(dropped, "<!doctype html><title>Dropped</title>");
    // A dropped file navigates the page as a link does: through will-navigate.
    await page.evaluate((url) => {
      location.href = url;
    }, pathToFileURL(dropped).href);
    await new Promise((resolve) => setTimeout(resolve, 500));
    expect(page.url()).toMatch(/\/shell\.html$/);
    // Playwright's title() misreads a page after a cancelled navigation: the document says.
    expect(await page.evaluate(() => [document.title, Boolean(document.getElementById("sidebar"))])).toEqual(["Surogate", true]);
    expect(await page.evaluate(() => window.open("https://example.com/") === null)).toBe(true);
    expect(await page.evaluate(async () => (await navigator.permissions.query({ name: "notifications" })).state)).toBe("denied");
  });

  it("opens with its defaults when its state files hold JSON that is no object", async () => {
    mkdirSync(join(home, "surogate"), { recursive: true });
    for (const name of ["window-state.json", "settings.json", "agent.json", "credentials.json"]) {
      writeFileSync(join(home, "surogate", name), "null");
    }
    app = await launch(home);
    const page = await shellPage(app);
    await expect.poll(() => visible(app!)).toEqual([true]);
    await expect.poll(() => page.isVisible("#first-run")).toBe(true);
  });

  it("exits with an error, rather than live on with no window, when it cannot start", async () => {
    // The system's theme cannot be set: the start fails once the app is ready.
    const failing = join(home, "no-theme.cjs");
    writeFileSync(failing, [
      'const { nativeTheme } = require("electron");',
      'Object.defineProperty(nativeTheme, "themeSource", { get: () => "system", set: () => { throw new Error("No theme here"); } });',
    ].join("\n"));
    // Loaded before the main, as Playwright loads its own: under NODE_OPTIONS it would run before electron exists.
    const started = spawn(ELECTRON, ["-r", failing, MAIN, "--password-store=basic"], { env: shellEnv(home), stdio: "ignore" });
    try {
      const ended = once(started, "exit").then(([code]) => code as number | null);
      expect(await Promise.race([ended, new Promise((resolve) => setTimeout(resolve, 10_000, "still running"))])).toBe(1);
    } finally {
      started.kill("SIGKILL");
    }
  });

  it("opens in the saved theme", async () => {
    mkdirSync(join(home, "surogate"), { recursive: true });
    writeFileSync(join(home, "surogate", "settings.json"), JSON.stringify({ theme: "dark" }));
    app = await launch(home);
    const page = await app.firstWindow();
    await page.waitForLoadState();
    expect(await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0]!.getBackgroundColor())).toBe("#151515");
    expect(await page.evaluate(() => [
      matchMedia("(prefers-color-scheme: dark)").matches, document.documentElement.dataset.theme,
      getComputedStyle(document.querySelector("#sidebar")!).backgroundColor,
    ])).toEqual([true, "dark", "rgb(17, 17, 17)"]);
  });

  it("hides when it is closed, and a second launch shows it again", async () => {
    app = await launch(home);
    await app.firstWindow();
    await expect.poll(() => visible(app!)).toEqual([true]);
    await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0]!.close());
    expect(await visible(app)).toEqual([false]);
    expect(await secondLaunch(home)).toBe(0);
    await expect.poll(() => visible(app!)).toEqual([true]);
  });

  it("hides when it is closed even when its place cannot be kept, and a second launch shows it again", async () => {
    // A folder where the place's file goes: no write of it can land, as on a full disk.
    mkdirSync(join(home, "surogate", "window-state.json", "taken"), { recursive: true });
    app = await launch(home);
    await app.firstWindow();
    await expect.poll(() => visible(app!)).toEqual([true]);
    // Kept here: Electron would otherwise draw an error dialog nobody answers.
    await app.evaluate(() => {
      const uncaught: string[] = [];
      Object.assign(globalThis, { uncaught });
      process.on("uncaughtException", (error) => uncaught.push(String(error)));
    });
    await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0]!.close());
    expect(await visible(app)).toEqual([false]);
    expect(await app.evaluate(() => (globalThis as unknown as { uncaught: string[] }).uncaught)).toEqual([]);
    expect(await secondLaunch(home)).toBe(0);
    await expect.poll(() => visible(app!)).toEqual([true]);
  });

  it("closes Settings while its page still loads, and leaves no failure unhandled", async () => {
    app = await launch(home);
    await (await app.firstWindow()).waitForLoadState();
    await focused(app);
    await app.evaluate(({ BrowserWindow }) => {
      const rejections: string[] = [];
      Object.assign(globalThis, { rejections });
      process.on("unhandledRejection", (reason) => rejections.push(String(reason)));
      BrowserWindow.getAllWindows()[0]!.webContents.sendInputEvent({ type: "keyDown", keyCode: ",", modifiers: ["control", "shift"] });
    });
    // Closed from its own page the moment there is one, before its load has finished.
    const settings = await app.waitForEvent("window");
    await settings.evaluate(() => (window as unknown as { surogateSettings: { close(): Promise<void> } }).surogateSettings.close()).catch(() => {});
    await expect.poll(() => app!.evaluate(({ webContents }) =>
      webContents.getAllWebContents().some((contents) => contents.getURL().endsWith("/settings.html")))).toBe(false);
    // A rejection nobody handles is told within a turn of the main process's loop.
    await app.evaluate(() => new Promise((resolve) => setTimeout(resolve, 100)));
    expect(await app.evaluate(() => (globalThis as unknown as { rejections: string[] }).rejections)).toEqual([]);
  });

  it("quits on Ctrl+Q", async () => {
    app = await launch(home);
    await (await app.firstWindow()).waitForLoadState();
    await focused(app);
    const closed = app.waitForEvent("close");
    // As the keyboard sends it: Playwright's own key presses never reach the menu's accelerators.
    void app.evaluate(({ BrowserWindow }) => {
      BrowserWindow.getAllWindows()[0]!.webContents.sendInputEvent({ type: "keyDown", keyCode: "Q", modifiers: ["control"] });
    }).catch(() => {});
    await closed;
    app = undefined;
  });
});
