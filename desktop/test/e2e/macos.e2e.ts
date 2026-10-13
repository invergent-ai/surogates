// The app on a Mac, through the real app, as a development build runs there: its data in the user's
// Library, the app's menu in the menu bar, the window's own buttons inset over its corner, a template
// image in the menu bar, Option+Space for quick entry, links from Launch Services' open-url, the Dock's
// click, sign-in tokens sealed, the file helper's tools on the PATH an app started from the Dock has, the
// Mac's own name, and a notification's click. With no VM: none boots on the macOS runner. On a Mac alone.

import { execFileSync } from "node:child_process";
import { existsSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import type { ElectronApplication, Page } from "playwright-core";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { connect, FakeAgent, signedInAndAdded, webClient } from "./fake-agent.js";
import { clickNotice, dataHome, dataRoot, launch, notices, quit, secondLaunch, shellPage, stubNative, stubNotifications } from "./launch.js";

const CHAT = "7d2e0f8a-2b3c-4d5e-9f60-718293a4b5c6";
const ASSETS = join(import.meta.dirname, "..", "..", "assets");
const link = (url: string) => `surogate://open?url=${encodeURIComponent(url)}`;

let home: string;
let agent: FakeAgent;
let origin: string;
let app: ElectronApplication | undefined;

const asked = (shell: ElectronApplication) => shell.evaluate(() => (globalThis as unknown as { asked: Array<{ message: string }> }).asked.map(({ message }) => message));
const visible = (shell: ElectronApplication) => shell.evaluate(({ BrowserWindow }) =>
  BrowserWindow.getAllWindows().find((window) => window.webContents.getURL().endsWith("/shell.html"))!.isVisible());
const hide = (shell: ElectronApplication) => shell.evaluate(({ BrowserWindow }) =>
  BrowserWindow.getAllWindows().find((window) => window.webContents.getURL().endsWith("/shell.html"))!.hide());
// Launch Services hands the running app a link, as Electron emits it.
const openUrl = (shell: ElectronApplication, url: string) => shell.evaluate(({ app: running }, address) => {
  running.emit("open-url", { preventDefault: () => {} }, address);
}, url);

async function signedIn(): Promise<{ shell: ElectronApplication; page: Page; client: Page }> {
  const shell = await launch(home);
  app = shell;
  await stubNative(shell);
  await stubNotifications(shell);
  const page = await shellPage(shell);
  await connect(page, origin);
  await signedInAndAdded(shell, page, agent);
  return { shell, page, client: await webClient(shell, origin) };
}

describe.runIf(process.platform === "darwin")("the app on a Mac", () => {
  beforeEach(async () => {
    home = dataHome();
    agent = new FakeAgent();
    origin = await agent.start();
  });

  afterEach(async () => {
    await quit(app);
    app = undefined;
    await agent.stop();
    await agent.link.stop();
    rmSync(home, { recursive: true, force: true });
  });

  it("keeps its data and Electron's own in ~/Library/Application Support/Surogate", async () => {
    app = await launch(home);
    await shellPage(app);
    expect(await app.evaluate(({ app: running }) => running.getPath("userData"))).toBe(join(dataRoot(home), "electron"));
    await expect.poll(() => existsSync(join(dataRoot(home), "electron"))).toBe(true);
  });

  it("puts the app's menu first in the menu bar, Settings on Cmd+, , and Window and Help last", async () => {
    app = await launch(home);
    await shellPage(app);
    const menu = await app.evaluate(({ Menu }) => Menu.getApplicationMenu()!.items.map((item) => ({
      role: item.role ?? null, label: item.label, items: item.submenu?.items.filter((sub) => sub.type !== "separator").map((sub) => [sub.label, sub.accelerator ?? null]) ?? [],
    })));
    expect(menu[0]?.role?.toLowerCase()).toBe("appmenu");
    expect(menu[0]?.items).toEqual(expect.arrayContaining([["About Surogate", null], ["Settings…", "Cmd+,"], ["Quit Surogate", "Cmd+Q"]]));
    expect(menu.slice(1).map(({ label }) => label)).toEqual(["File", "Edit", "View", "Window", "Help"]);
    expect(menu.find(({ label }) => label === "Window")?.role?.toLowerCase()).toBe("windowmenu");
  });

  it("insets the window's own buttons over its top left corner, and its page leaves them room and names the Mac's keys", async () => {
    app = await launch(home);
    const page = await shellPage(app);
    expect(await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0]!.getWindowButtonPosition())).toEqual({ x: 13, y: 11 });
    await expect.poll(() => page.evaluate(() => getComputedStyle(document.querySelector(".sidebar .top")!).paddingLeft)).toBe("80px");
    expect(await page.textContent('[data-action="settings"] .shortcut')).toBe("⌘,");
  });

  it("draws a template image in the menu bar, which macOS tints for a light menu bar and a dark one", async () => {
    app = await launch(home);
    await shellPage(app);
    expect(await app.evaluate(({ nativeImage }, at) => nativeImage.createFromPath(at).isTemplateImage(), join(ASSETS, "trayTemplate.png"))).toBe(true);
  });

  it("holds Option+Space for quick entry", async () => {
    app = await launch(home);
    await shellPage(app);
    await expect.poll(() => app!.evaluate(({ globalShortcut }) => globalShortcut.isRegistered("Alt+Space"))).toBe(true);
  });

  it("says in Settings that another app holds Option+Space, where one does", async () => {
    // Taken before the app's own ready handler asks for it, as another app holding it would.
    const holding = join(home, "hold-keys.cjs");
    writeFileSync(holding, 'const { app, globalShortcut } = require("electron");\napp.whenReady().then(() => globalShortcut.register("Alt+Space", () => {}));\n');
    app = await launch(home, {}, [], [holding]);
    await shellPage(app);
    // From the app's menu: the first run shows no sidebar to open it from.
    await app.evaluate(({ Menu }) => Menu.getApplicationMenu()!.getMenuItemById("settings")!.click());
    let settings: Page | undefined;
    await expect.poll(() => {
      settings = app!.windows().find((found) => found.url().endsWith("/settings.html"));
      return settings !== undefined;
    }).toBe(true);
    await expect.poll(() => settings!.textContent("#quick-entry-unheld"))
      .toBe("Another app holds Option+Space, so it does not open quick entry. Open quick entry from Surogate's icon in the menu bar.");
    expect(await settings!.evaluate(() => document.getElementById("quick-entry")!.classList.contains("absent"))).toBe(false);
  });

  it("opens a link Launch Services hands it before it is ready, once its window is there", async () => {
    // Before ready, as Launch Services sends one with the launch it starts: its confirmation recorded, and cancelled.
    const early = join(home, "early-link.cjs");
    writeFileSync(early, [
      'const { app, dialog } = require("electron");',
      "Object.assign(globalThis, { asked: [] });",
      "dialog.showMessageBox = (...args) => { globalThis.asked.push(args.at(-1)); return Promise.resolve({ response: 1, checkboxChecked: false }); };",
      `app.once("will-finish-launching", () => app.emit("open-url", { preventDefault() {} }, ${JSON.stringify(link(origin))}));`,
    ].join("\n"));
    app = await launch(home, {}, [], [early]);
    await shellPage(app);
    await expect.poll(() => asked(app!)).toEqual([`Open a link to connect to ${new URL(origin).host}?`]);
    expect(agent.asked.config).toBe(0);
  });

  it("opens the chat a link names, handed while it runs, and shows its window", async () => {
    const { shell, client } = await signedIn();
    await hide(shell);
    await openUrl(shell, link(`${origin}/chat/${CHAT}`));
    await expect.poll(() => visible(shell)).toBe(true);
    await expect.poll(() => new URL(client.url()).pathname).toBe(`/chat/${CHAT}`);
  });

  it("shows its window at a second launch and at a click on its Dock icon", async () => {
    app = await launch(home);
    await shellPage(app);
    await hide(app);
    expect(await secondLaunch(home)).toBe(0);
    await expect.poll(() => visible(app!)).toBe(true);
    await hide(app);
    await app.evaluate(({ app: running }) => running.emit("activate"));
    await expect.poll(() => visible(app!)).toBe(true);
  });

  it("keeps its sign-in and its device token sealed, and says nothing of encryption", async () => {
    const { page } = await signedIn();
    for (const file of ["session.json", "credentials.json"]) {
      const kept = readFileSync(join(dataRoot(home), file), "utf8");
      expect(kept, file).toContain('"sealed"');
      expect(kept, file).not.toContain('"plain"');
    }
    expect(await page.isHidden("#notice")).toBe(true);
  });

  it("finds the file helper's tools, its own rg beside its node, on the bare PATH Finder and the Dock give an app", async () => {
    // As scripts/rg.sh fetches it, or this test means nothing.
    expect(existsSync(join(import.meta.dirname, "..", "..", "bin", "rg")), "the app's rg, by scripts/rg.sh").toBe(true);
    app = await launch(home, { PATH: "/usr/bin:/bin:/usr/sbin:/sbin" });
    const page = await shellPage(app);
    // The line says what the sandbox lacks once its looks are done: this runner's VM, which none boots on, and no tool.
    await expect.poll(() => page.textContent("#sandbox-text"), { timeout: 15_000 }).toMatch(/This Mac cannot run Surogate's sandbox|tools are missing/);
    expect(await page.textContent("#sandbox-text")).not.toContain("tools are missing");
  });

  it("closes Settings from its close button: while Settings is open, nothing of the window beneath it drags the window", async () => {
    const { shell, page } = await signedIn();
    const dragging = () => page.evaluate(() => [...document.querySelectorAll("*")]
      .filter((element) => getComputedStyle(element).getPropertyValue("-webkit-app-region") === "drag").length);
    expect(await dragging()).toBeGreaterThan(0);
    await page.click("#open-settings");
    let settings: Page | undefined;
    await expect.poll(() => {
      settings = shell.windows().find((found) => found.url().endsWith("/settings.html"));
      return settings !== undefined;
    }).toBe(true);
    await expect.poll(dragging).toBe(0);
    await settings!.click("#close");
    await expect.poll(() => shell.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().some((window) => window.contentView.children
      .some((view) => (view as Electron.WebContentsView).webContents.getURL().endsWith("/settings.html"))))).toBe(false);
    await expect.poll(dragging).toBeGreaterThan(0);
  });

  it("names this Mac in Settings by the name System Settings gives it", async () => {
    const { shell, page } = await signedIn();
    await page.click("#open-settings");
    let settings: Page | undefined;
    await expect.poll(() => {
      settings = shell.windows().find((found) => found.url().endsWith("/settings.html"));
      return settings !== undefined;
    }).toBe(true);
    // Read by the helper as the app starts it.
    const named = execFileSync("/usr/sbin/scutil", ["--get", "ComputerName"], { encoding: "utf8" }).trim();
    await expect.poll(() => settings!.textContent("#computer")).toBe(named);
  });

  it("opens the chat a notification is for when it is clicked", async () => {
    const { shell, client } = await signedIn();
    await hide(shell);
    await expect.poll(() => agent.inboxStreams.size).toBe(1);
    agent.tell({ kind: "input_required", title: "Which report should I start from?", session_id: CHAT });
    await expect.poll(() => notices(shell)).toEqual([{ title: "Which report should I start from?", body: "Asks you a question." }]);
    await clickNotice(shell, 0);
    await expect.poll(() => visible(shell)).toBe(true);
    await expect.poll(() => new URL(client.url()).pathname).toBe(`/chat/${CHAT}`);
  });
});
