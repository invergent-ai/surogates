// The app on a Windows PC, through the real app, as a development build runs there: its data in the user's local
// application data, its AppUserModelID before its first window, surogate:// links taken for its user, Linux's menus,
// Windows's own buttons over the window's corner, an .ico in the notification area, the window brought back by the
// notification area's icon and by a second launch, Ctrl+Alt+Space for quick entry, the login item in the Run key,
// sign-in tokens sealed, the PC's own name, a toast's click, Settings' close button, a launch that asks it to quit,
// and test/win32/dev-app.ps1's removal. With no VM: none boots on the Windows runner. On Windows alone.

import { execFileSync, spawnSync } from "node:child_process";
import { existsSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { hostname } from "node:os";
import { dirname, join } from "node:path";

import type { ElectronApplication, Page } from "playwright-core";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { connect, FakeAgent, signedInAndAdded, webClient } from "./fake-agent.js";
import {
  clickNotice, dataHome, dataRoot, ELECTRON, icons, launch, MAIN, notices, quit, secondLaunch, shellEnv, shellPage, stubNative, stubNotifications, watchTray,
} from "./launch.js";

const CHAT = "7d2e0f8a-2b3c-4d5e-9f60-718293a4b5c6";
const DEVELOPMENT_ID = "ai.invergent.surogate.development";
const link = (url: string) => `surogate://open?url=${encodeURIComponent(url)}`;
const SYSTEM32 = join(process.env.SystemRoot ?? "C:\\Windows", "System32");
const REG = join(SYSTEM32, "reg.exe");
const POWERSHELL = join(SYSTEM32, "WindowsPowerShell", "v1.0", "powershell.exe");
const RUN = "HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run";
const LINKS = "HKCU\\Software\\Classes\\surogate\\shell\\open\\command";

let home: string;
let agent: FakeAgent;
let origin: string;
let app: ElectronApplication | undefined;

// A registry value of the user's, as reg.exe prints it, or null where there is none.
function registry(key: string, name: string | null): string | null {
  const done = spawnSync(REG, ["query", key, ...(name === null ? ["/ve"] : ["/v", name])], { encoding: "utf8" });
  if (done.status !== 0) return null;
  const line = done.stdout.split(/\r?\n/).find((found) => /\sREG_(?:EXPAND_)?SZ\s/.test(found));
  return line ? line.replace(/^.*?\sREG_(?:EXPAND_)?SZ\s+/, "") : null;
}

const powershell = (script: string, env: NodeJS.ProcessEnv = process.env) =>
  spawnSync(POWERSHELL, ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script], { encoding: "utf8", env, timeout: 120_000 });

const mainWindow = (shell: ElectronApplication) => shell.evaluate(({ BrowserWindow }) => {
  const found = BrowserWindow.getAllWindows().find((window) => window.webContents.getURL().endsWith("/shell.html"));
  return found ? { visible: found.isVisible(), minimized: found.isMinimized(), loading: found.webContents.isLoading() } : null;
});
const closeWindow = (shell: ElectronApplication) => shell.evaluate(({ BrowserWindow }) =>
  BrowserWindow.getAllWindows().find((window) => window.webContents.getURL().endsWith("/shell.html"))!.close());

async function settingsPage(shell: ElectronApplication, page: Page): Promise<Page> {
  await page.click("#open-settings");
  let found: Page | undefined;
  await expect.poll(() => {
    found = shell.windows().find((window) => window.url().endsWith("/settings.html"));
    return found !== undefined;
  }).toBe(true);
  await found!.waitForSelector(".settings-nav .item");
  return found!;
}
const pressed = (settings: Page, setting: string) =>
  settings.$eval(`[data-setting="${setting}"]`, (control) => control.querySelector<HTMLElement>('[aria-pressed="true"]')?.dataset.value ?? null);

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

describe.runIf(process.platform === "win32")("the app on a Windows PC", () => {
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
    rmSync(home, { recursive: true, force: true, maxRetries: 10 });
  });

  it("keeps its data and Electron's own in %LOCALAPPDATA%\\Surogate", async () => {
    app = await launch(home);
    await shellPage(app);
    expect(await app.evaluate(({ app: running }) => running.getPath("userData"))).toBe(join(dataRoot(home), "electron"));
    await expect.poll(() => existsSync(join(dataRoot(home), "electron"))).toBe(true);
  });

  it("names itself to Windows by its own AppUserModelID before its first window, and takes surogate:// links for its user", async () => {
    const naming = join(home, "naming.cjs");
    writeFileSync(naming, [
      'const { app, BrowserWindow } = require("electron");',
      "const set = app.setAppUserModelId.bind(app);",
      "app.setAppUserModelId = (id) => { Object.assign(globalThis, { named: { id, windows: BrowserWindow.getAllWindows().length } }); set(id); };",
    ].join("\n"));
    app = await launch(home, {}, [], [naming]);
    await shellPage(app);
    expect(await app.evaluate(() => (globalThis as unknown as { named: unknown }).named)).toEqual({ id: DEVELOPMENT_ID, windows: 0 });
    // Its own command, the link after the end of its switches.
    const command = registry(LINKS, null);
    expect(command?.startsWith(`"${ELECTRON}" `), command ?? "none").toBe(true);
    expect(command).toContain(MAIN);
    expect(command?.endsWith(' -- "%1"'), command ?? "none").toBe(true);
    // Each argument of a scheme's command, as Electron writes it: a path with a space quoted, as Windows reads it back.
    const probe = "HKCU\\Software\\Classes\\surogate-quoting-test";
    try {
      expect(await app.evaluate(({ app: running }) => running.setAsDefaultProtocolClient("surogate-quoting-test", "C:\\a b\\x.exe", ["C:\\a b\\main.js", "--"]))).toBe(true);
      expect(registry(`${probe}\\shell\\open\\command`, null)).toBe('"C:\\a b\\x.exe" "C:\\a b\\main.js" -- "%1"');
    } finally {
      spawnSync(REG, ["delete", probe, "/f"], { stdio: "ignore" });
    }
  });

  it("puts File first and Help last, as on Linux, with no app menu", async () => {
    app = await launch(home);
    await shellPage(app);
    expect(await app.evaluate(({ Menu }) => Menu.getApplicationMenu()!.items.map((item) => item.label))).toEqual(["File", "Edit", "View", "Help"]);
  });

  it("lays Windows's own buttons over the window's top right corner, and its page leaves them their room", async () => {
    const { page } = await signedIn();
    // The window's title bar area, as Chromium gives it to a window whose buttons are laid over it.
    const room = () => page.evaluate(() => {
      const top = document.querySelector<HTMLElement>(".panel .top");
      return top && top.offsetParent !== null ? Number.parseFloat(getComputedStyle(top).paddingRight) : null;
    });
    await expect.poll(room).toBeGreaterThan(100);
  });

  it("draws an .ico in the notification area by the taskbar's own mode, and its click shows the window closed to it", async () => {
    const watching = join(home, "watch-tray.cjs");
    writeFileSync(watching, [
      'const { Tray } = require("electron");',
      "const setToolTip = Tray.prototype.setToolTip;",
      "Tray.prototype.setToolTip = function (tip) { Object.assign(globalThis, { tray: this }); return setToolTip.call(this, tip); };",
    ].join("\n"));
    app = await launch(home, {}, [], [watching]);
    await shellPage(app);
    await watchTray(app);
    // A change of theme draws the icon again: Windows's taskbar's own mode picks it, whatever the app's is.
    await app.evaluate(({ nativeTheme }) => {
      nativeTheme.themeSource = "dark";
    });
    const taskbarDark = await app.evaluate(({ nativeTheme }) => nativeTheme.shouldUseDarkColorsForSystemIntegratedUI);
    await expect.poll(async () => (await icons(app!)).length).toBeGreaterThan(0);
    for (const icon of await icons(app)) expect(icon.endsWith(taskbarDark ? "tray-dark.ico" : "tray-light.ico"), icon).toBe(true);
    expect(await app.evaluate(({ nativeImage }, at) => nativeImage.createFromPath(at).isEmpty(), (await icons(app))[0]!)).toBe(false);
    // Closed by its own close button, with Keep running on: it goes to the notification area, and comes back at a click there.
    await closeWindow(app);
    await expect.poll(async () => (await mainWindow(app!))?.visible).toBe(false);
    await app.evaluate(() => (globalThis as unknown as { tray: Electron.Tray }).tray.emit("click"));
    await expect.poll(async () => (await mainWindow(app!))?.visible).toBe(true);
  });

  it("brings its window back at a second launch, as its pinned taskbar button or its Start menu shortcut starts one: closed, or minimised", async () => {
    app = await launch(home);
    await shellPage(app);
    await closeWindow(app);
    await expect.poll(async () => (await mainWindow(app!))?.visible).toBe(false);
    expect(await secondLaunch(home)).toBe(0);
    await expect.poll(async () => (await mainWindow(app!))?.visible).toBe(true);
    await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().find((window) => window.webContents.getURL().endsWith("/shell.html"))!.minimize());
    await expect.poll(async () => (await mainWindow(app!))?.minimized).toBe(true);
    expect(await secondLaunch(home)).toBe(0);
    await expect.poll(async () => (await mainWindow(app!))?.minimized).toBe(false);
  });

  it("opens the chat a link names, handed by a second launch while it runs, and shows its window", async () => {
    const { shell, client } = await signedIn();
    await closeWindow(shell);
    expect(await secondLaunch(home, "--", link(`${origin}/chat/${CHAT}`))).toBe(0);
    await expect.poll(async () => (await mainWindow(shell))?.visible).toBe(true);
    await expect.poll(() => new URL(client.url()).pathname).toBe(`/chat/${CHAT}`);
  });

  it("holds Ctrl+Alt+Space for quick entry", async () => {
    app = await launch(home);
    await shellPage(app);
    await expect.poll(() => app!.evaluate(({ globalShortcut }) => globalShortcut.isRegistered("Ctrl+Alt+Space"))).toBe(true);
  });

  it("says in Settings that another app holds Ctrl+Alt+Space, where one does", async () => {
    const holding = join(home, "hold-keys.cjs");
    writeFileSync(holding, 'const { app, globalShortcut } = require("electron");\napp.whenReady().then(() => globalShortcut.register("Ctrl+Alt+Space", () => {}));\n');
    app = await launch(home, {}, [], [holding]);
    await shellPage(app);
    await app.evaluate(({ Menu }) => Menu.getApplicationMenu()!.getMenuItemById("settings")!.click());
    let settings: Page | undefined;
    await expect.poll(() => {
      settings = app!.windows().find((found) => found.url().endsWith("/settings.html"));
      return settings !== undefined;
    }).toBe(true);
    await expect.poll(() => settings!.textContent("#quick-entry-unheld"))
      .toBe("Another app holds Ctrl+Alt+Space, so it does not open quick entry. Open quick entry from Surogate's icon in the notification area.");
  });

  it("starts at login from its own command in the user's Run key, once it is on, with its window waiting", async () => {
    const { shell, page } = await signedIn();
    const settings = await settingsPage(shell, page);
    await expect.poll(() => pressed(settings, "startAtLogin")).toBe("off");
    await settings.click('[data-setting="startAtLogin"] [data-value="on"]');
    await expect.poll(() => pressed(settings, "startAtLogin")).toBe("on");
    try {
      // A development build starts itself: its Electron, on its main, named by its own AppUserModelID; Electron
      // quotes a path, as CommandLineToArgvW reads it back.
      expect(registry(RUN, DEVELOPMENT_ID)).toBe(`"${ELECTRON}" "${MAIN}" --login`);
    } finally {
      await settings.click('[data-setting="startAtLogin"] [data-value="off"]');
    }
    await expect.poll(() => pressed(settings, "startAtLogin")).toBe("off");
    expect(registry(RUN, DEVELOPMENT_ID)).toBeNull();
  });

  it("shows no window when it starts at login, until it is launched again", async () => {
    app = await launch(home, {}, ["--login"]);
    await expect.poll(async () => (await mainWindow(app!))?.loading).toBe(false);
    expect((await mainWindow(app))?.visible).toBe(false);
    expect(await secondLaunch(home, "--login")).toBe(0);
    await new Promise((resolve) => setTimeout(resolve, 1_000));
    expect((await mainWindow(app))?.visible).toBe(false);
    expect(await secondLaunch(home)).toBe(0);
    await expect.poll(async () => (await mainWindow(app!))?.visible).toBe(true);
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

  it("names this PC in Settings by its device name", async () => {
    const { shell, page } = await signedIn();
    const settings = await settingsPage(shell, page);
    await expect.poll(() => settings.textContent("#computer")).toBe(hostname());
  });

  it("opens the chat a toast is for when it is clicked", async () => {
    const { shell, client } = await signedIn();
    await closeWindow(shell);
    await expect.poll(() => agent.inboxStreams.size).toBe(1);
    agent.tell({ kind: "input_required", title: "Which report should I start from?", session_id: CHAT });
    await expect.poll(() => notices(shell)).toEqual([{ title: "Which report should I start from?", body: "Asks you a question." }]);
    await clickNotice(shell, 0);
    await expect.poll(async () => (await mainWindow(shell))?.visible).toBe(true);
    await expect.poll(() => new URL(client.url()).pathname).toBe(`/chat/${CHAT}`);
  });

  it("closes Settings from its close button: while Settings is open, nothing of the window beneath it drags the window", async () => {
    const { shell, page } = await signedIn();
    const dragging = () => page.evaluate(() => [...document.querySelectorAll("*")]
      .filter((element) => getComputedStyle(element).getPropertyValue("-webkit-app-region") === "drag").length);
    expect(await dragging()).toBeGreaterThan(0);
    const settings = await settingsPage(shell, page);
    await expect.poll(dragging).toBe(0);
    await settings.click("#close");
    await expect.poll(() => shell.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().some((window) => window.contentView.children
      .some((view) => (view as Electron.WebContentsView).webContents.getURL().endsWith("/settings.html"))))).toBe(false);
    await expect.poll(dragging).toBeGreaterThan(0);
  });

  it("quits at a launch that asks it to, as its own Quit does", async () => {
    app = await launch(home);
    await shellPage(app);
    const closed = app.waitForEvent("close");
    expect(await secondLaunch(home, "--quit")).toBe(0);
    await closed;
    app = undefined;
  });
});

// The development build as a Windows app of its own (test/win32/dev-app.ps1), on a CI runner alone: it makes a
// shortcut in the account's own Start menu, and its links and login item are the account's.
describe.runIf(process.platform === "win32" && process.env.CI === "true")("a development build made a Windows app of its own", () => {
  const SCRIPT = join(import.meta.dirname, "..", "win32", "dev-app.ps1");
  const shortcutPath = () => join(execFileSync(POWERSHELL, ["-NoProfile", "-Command", "[Environment]::GetFolderPath('Programs')"], { encoding: "utf8" }).trim(), "Surogate Dev.lnk");
  // Its app's processes: this package's Electron on its main.
  const running = () => powershell(`@(Get-CimInstance Win32_Process -Filter "Name = 'electron.exe'" | Where-Object { $_.CommandLine -like '*${MAIN}*' }).Count`).stdout.trim();

  beforeEach(() => {
    home = dataHome();
  });

  afterEach(() => {
    rmSync(home, { recursive: true, force: true, maxRetries: 10 });
  });

  it("has a Start menu shortcut with its own AppUserModelID; its removal quits it, and takes out its login item, its links and the shortcut", async () => {
    // Its data in the test's own folder, as the app's data home is a test's.
    const env = { ...process.env, ...shellEnv(home) };
    const made = powershell(`& '${SCRIPT}'`, env);
    expect(made.status, made.stderr).toBe(0);
    const shortcut = shortcutPath();
    expect(existsSync(shortcut)).toBe(true);
    const named = powershell(`(New-Object -ComObject Shell.Application).NameSpace('${dirname(shortcut)}').ParseName('Surogate Dev.lnk').ExtendedProperty('System.AppUserModel.ID')`);
    expect(named.stdout.trim()).toBe(DEVELOPMENT_ID);
    // Started, it takes its links; and its login item is on, as Settings sets it.
    await expect.poll(() => registry(LINKS, null)?.startsWith(`"${ELECTRON}" `) ?? false, { timeout: 30_000 }).toBe(true);
    expect(running()).not.toBe("0");
    spawnSync(REG, ["add", RUN, "/v", DEVELOPMENT_ID, "/t", "REG_SZ", "/d", `"${ELECTRON}" ${MAIN} --login`, "/f"]);
    const removed = powershell(`& '${SCRIPT}' -Remove`, env);
    expect(removed.status, removed.stderr).toBe(0);
    expect(running()).toBe("0");
    expect(registry(RUN, DEVELOPMENT_ID)).toBeNull();
    expect(registry(LINKS, null)).toBeNull();
    expect(existsSync(shortcut)).toBe(false);
  }, 180_000);
});
