import { spawn } from "node:child_process";
import { once } from "node:events";
import { mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

import type { ElectronApplication } from "playwright-core";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { dataHome, ELECTRON, launch, MAIN, quit, shellEnv } from "./launch.js";

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

const visible = (shell: ElectronApplication) =>
  shell.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().map((window) => window.isVisible()));

describe("the shell", () => {
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
    app = await launch(home);
    const page = await app.firstWindow();
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
    const second = spawn(ELECTRON, [MAIN], { env: shellEnv(home), stdio: "ignore" });
    const [code] = (await once(second, "exit")) as [number | null];
    expect(code).toBe(0);
    await expect.poll(() => visible(app!)).toEqual([true]);
  });

  it("quits on Ctrl+Q", async () => {
    app = await launch(home);
    await (await app.firstWindow()).waitForLoadState();
    const closed = app.waitForEvent("close");
    // As the keyboard sends it: Playwright's own key presses never reach before-input-event.
    void app.evaluate(({ BrowserWindow }) => {
      BrowserWindow.getAllWindows()[0]!.webContents.sendInputEvent({ type: "keyDown", keyCode: "Q", modifiers: ["control"] });
    }).catch(() => {});
    await closed;
    app = undefined;
  });
});
