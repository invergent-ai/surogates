import { once } from "node:events";
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { hostname } from "node:os";
import { dirname, join, relative } from "node:path";
import { pathToFileURL } from "node:url";

import type { ElectronApplication, Page } from "playwright-core";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { ACCOUNT, connect, FakeAgent, signedInAndAdded, webClient } from "./fake-agent.js";
import { dataHome, ELECTRON, launch, MAIN, quit, secondLaunch, shellPage, stubNative } from "./launch.js";

let home: string;
let agent: FakeAgent;
let origin: string;
let app: ElectronApplication | undefined;

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

// The app signed in to the fake agent as Flavius, this computer registered.
async function signedIn(): Promise<{ shell: ElectronApplication; page: Page; client: Page }> {
  const shell = await launch(home);
  app = shell;
  await stubNative(shell);
  const page = await shellPage(shell);
  await connect(page, origin);
  await signedInAndAdded(shell, page, agent);
  const client = await webClient(shell, origin);
  // The web client says who is signed in on it, as it does once its session is in.
  await client.evaluate((account) => window.surogateDesktop!.setAccount(account), ACCOUNT);
  return { shell, page, client };
}

// The Settings dialog's page, once it is open over the window.
async function settingsPage(shell: ElectronApplication): Promise<Page> {
  let found: Page | undefined;
  await expect.poll(() => {
    found = shell.windows().find((page) => page.url().endsWith("/settings.html"));
    return found !== undefined;
  }).toBe(true);
  await found!.waitForLoadState();
  await found!.waitForSelector(".settings-nav .item");
  return found!;
}

const settingsOpen = (shell: ElectronApplication) => shell.evaluate(({ BrowserWindow }) =>
  BrowserWindow.getAllWindows()[0]!.contentView.children
    .some((view) => (view as Electron.WebContentsView).webContents.getURL().endsWith("/settings.html")));
// The contents that have the keyboard, by address; and focus moved to the contents at *address*.
const focused = (shell: ElectronApplication) => shell.evaluate(({ webContents }) => webContents.getFocusedWebContents()?.getURL() ?? null);
const focus = (shell: ElectronApplication, address: string) => shell.evaluate(({ webContents }, start) => {
  webContents.getAllWebContents().find((contents) => contents.getURL().startsWith(start))!.focus();
}, address);
const texts = (page: Page, selector: string) =>
  page.$$eval(selector, (found) => found.filter((element) => (element as HTMLElement).offsetParent !== null)
    .map((element) => element.textContent?.trim()));

describe("the user menu", () => {
  it("opens upward from the user row with its rows, and closes on Escape or a click elsewhere", async () => {
    const { page } = await signedIn();
    expect(await page.textContent("#user-name")).toBe("Flavius Burca");
    expect(await page.textContent("#avatar")).toBe("FB");
    await page.click("#user");
    expect(await page.isVisible("#user-menu")).toBe(true);
    expect(await page.getAttribute("#user", "aria-expanded")).toBe("true");
    expect(await page.textContent("#user-email")).toBe("flavius@example.com");
    // An agent that names no console: no usage or billing.
    expect(await texts(page, "#user-menu .menu-item")).toEqual(["SettingsCtrl+Shift+,", "Account settings", "Devices", "Language", "Get help", "Log out", "Remove this agent…"]);
    // One divider above Log out: none is doubled where the console's rows are hidden.
    expect(await page.$$eval("#user-menu hr", (found) => found.filter((hr) => (hr as HTMLElement).offsetParent !== null).length)).toBe(1);
    expect(await page.isDisabled('[data-action="logout"]')).toBe(false);
    expect(await page.isDisabled('[data-action="language"]')).toBe(true);
    const menu = await page.$eval("#user-menu", (found) => found.getBoundingClientRect().bottom);
    const row = await page.$eval("#user", (found) => found.getBoundingClientRect().top);
    expect(menu).toBeLessThanOrEqual(row);
    await page.keyboard.press("Escape");
    expect(await page.isVisible("#user-menu")).toBe(false);
    await page.click("#user");
    await page.click("#title");
    expect(await page.isVisible("#user-menu")).toBe(false);
  });

  it("takes the keyboard into its rows, moves along them with the arrows, and gives it back to the user row on Escape", async () => {
    const { page } = await signedIn();
    const focused = () => page.evaluate(() => (document.activeElement as HTMLElement).id || document.activeElement?.textContent?.trim());
    await page.focus("#user");
    await page.keyboard.press("Enter");
    expect(await page.isVisible("#user-menu")).toBe(true);
    expect(await focused()).toBe("SettingsCtrl+Shift+,");
    // Language comes later, and is passed over; Usage is hidden off surogate.ai.
    await page.keyboard.press("ArrowDown");
    expect(await focused()).toBe("Get help");
    await page.keyboard.press("End");
    expect(await focused()).toBe("Remove this agent…");
    await page.keyboard.press("ArrowDown");
    expect(await focused()).toBe("SettingsCtrl+Shift+,");
    await page.keyboard.press("ArrowUp");
    expect(await focused()).toBe("Remove this agent…");
    await page.keyboard.press("Escape");
    expect(await page.isVisible("#user-menu")).toBe(false);
    expect(await focused()).toBe("user");
    // Tab leaves it, closed.
    await page.keyboard.press("Enter");
    await page.keyboard.press("Tab");
    expect(await page.isVisible("#user-menu")).toBe(false);
  });

  it("closes once the conversation takes the focus", async () => {
    const { shell, page } = await signedIn();
    // Playwright keeps a page it drives focused: this one hears focus come and go, as in the app.
    await (await page.context().newCDPSession(page)).send("Emulation.setFocusEmulationEnabled", { enabled: false });
    await focus(shell, page.url());
    await page.click("#user");
    expect(await page.isVisible("#user-menu")).toBe(true);
    await focus(shell, origin);
    await expect.poll(() => page.isVisible("#user-menu")).toBe(false);
  });

  it("opens only the links it knows for the agent", async () => {
    const { shell, page } = await signedIn();
    await page.click("#user");
    await page.click('[data-action="help"]');
    // After the sign-in's own address, which the system browser opened first.
    await expect.poll(() => shell.evaluate(() => (globalThis as unknown as { opened: string[] }).opened.slice(1))).toEqual(["https://docs.surogate.ai/work/"]);
    // The agent names no console; and a name that is no link, a prototype's included, opens nothing.
    for (const which of ["usage", "billing", "constructor", "__proto__"]) {
      await expect(page.evaluate((name) => (window as unknown as { surogateShell: { link(which: string): Promise<void> } })
        .surogateShell.link(name), which)).rejects.toThrow("No such link");
    }
    expect(await shell.evaluate(() => (globalThis as unknown as { opened: unknown[] }).opened.length)).toBe(2);
  });

  it("names the user from the app's own sign-in until the web client reports, with nothing of the organisation", async () => {
    const shell = await launch(home);
    app = shell;
    await stubNative(shell);
    const page = await shellPage(shell);
    await connect(page, origin);
    await signedInAndAdded(shell, page, agent);
    const { orgName: _, ...shown } = ACCOUNT;
    await expect.poll(() => page.evaluate(() => (window as unknown as { surogateShell: { state(): Promise<{ account: unknown }> } })
      .surogateShell.state().then((state) => state.account))).toEqual(shown);
  });

  it("forgets the account once the user signs out in the web client", async () => {
    const { page, client } = await signedIn();
    await client.evaluate(() => window.surogateDesktop!.setAccount(null));
    await expect.poll(() => page.textContent("#user-name")).toBe("Not signed in");
    expect(await page.textContent("#avatar")).toBe("");
  });
});

describe("Settings", () => {
  it("opens over the window with Ctrl+Shift+, and from the menu, and closes on Escape", async () => {
    const { shell, page } = await signedIn();
    // Asked from the conversation, which has the keyboard.
    await focus(shell, origin);
    await expect.poll(async () => (await focused(shell))?.startsWith(origin)).toBe(true);
    await shell.evaluate(({ BrowserWindow }) => {
      BrowserWindow.getAllWindows()[0]!.webContents.sendInputEvent({ type: "keyDown", keyCode: ",", modifiers: ["control", "shift"] });
    });
    const settings = await settingsPage(shell);
    expect(await texts(settings, ".settings-nav h3")).toEqual(["Settings", "Customize"]);
    // Escape closes the page before Playwright sends the key up.
    await settings.keyboard.press("Escape").catch(() => {});
    await expect.poll(() => settingsOpen(shell)).toBe(false);
    // The keyboard goes back to the conversation.
    await expect.poll(async () => (await focused(shell))?.startsWith(origin)).toBe(true);
    await page.click("#user");
    await page.click('[data-action="settings"]');
    await settingsPage(shell);
    await expect.poll(() => settingsOpen(shell)).toBe(true);
    // Asked again while open: still one dialog.
    await shell.evaluate(({ BrowserWindow }) => {
      BrowserWindow.getAllWindows()[0]!.webContents.sendInputEvent({ type: "keyDown", keyCode: ",", modifiers: ["control", "shift"] });
    });
    await new Promise((resolve) => setTimeout(resolve, 500));
    expect(shell.windows().filter((found) => found.url().endsWith("/settings.html"))).toHaveLength(1);
  });

  it("changes the theme everywhere, and keeps it", async () => {
    const { shell, page, client } = await signedIn();
    await shell.evaluate(({ BrowserWindow }) => {
      const window = BrowserWindow.getAllWindows()[0]!;
      const set = window.setTitleBarOverlay.bind(window);
      const overlays: unknown[] = [];
      Object.assign(globalThis, { overlays });
      window.setTitleBarOverlay = (overlay) => {
        overlays.push(overlay);
        set(overlay);
      };
    });
    await client.evaluate(() => {
      Object.assign(window, { themes: [] as string[] });
      window.surogateDesktop!.onAppearanceChanged((appearance) => (window as unknown as { themes: string[] }).themes.push(appearance.theme));
    });
    await page.click("#open-settings");
    const settings = await settingsPage(shell);
    for (const theme of ["light", "dark", "light"] as const) {
      await settings.click(`[data-setting="theme"] [data-value="${theme}"]`);
      await expect.poll(() => page.evaluate(() => document.documentElement.dataset.theme)).toBe(theme);
      // Each page hears the theme from its own media query, in its own time.
      await expect.poll(() => settings.evaluate(() => document.documentElement.dataset.theme)).toBe(theme);
      // The agent's web client too: it takes the desktop's theme from its media query.
      await expect.poll(() => client.evaluate(() => matchMedia("(prefers-color-scheme: dark)").matches)).toBe(theme === "dark");
      expect(await settings.getAttribute(`[data-setting="theme"] [data-value="${theme}"]`, "aria-pressed")).toBe("true");
    }
    const overlays = await shell.evaluate(() => (globalThis as unknown as { overlays: Array<{ color: string }> }).overlays);
    // Under Settings, dimmed with the window.
    expect(overlays.slice(-2).map((overlay) => overlay.color)).toEqual(["#0c0c0b", "#6e6e6b"]);
    expect(await client.evaluate(() => (window as unknown as { themes: string[] }).themes.slice(-2))).toEqual(["dark", "light"]);
    await settings.click('[data-setting="textSize"] [data-value="large"]');
    const saved = () => JSON.parse(readFileSync(join(home, "surogate", "settings.json"), "utf8")) as Record<string, string>;
    await expect.poll(() => saved().textSize).toBe("large");
    expect(saved().theme).toBe("light");
  });

  it("paints the web client's first frame in a dark desktop's theme, its native controls too, before any script of its own", async () => {
    // The web client's own HTML, its bundle and the fonts' sheet not served: only its head can theme the
    // first frame, and the body's first script reads the root that frame paints from.
    agent.page = readFileSync(join(import.meta.dirname, "..", "..", "..", "web", "index.html"), "utf8")
      .replace(/<link [^>]*https:[^>]*>/g, "")
      .replace("<body>", "<body><script>window.firstFrame = [document.documentElement.className, document.documentElement.style.colorScheme]</script>");
    const { shell, page, client } = await signedIn();
    await page.click("#open-settings");
    const settings = await settingsPage(shell);
    await settings.click('[data-setting="theme"] [data-value="dark"]');
    await expect.poll(() => client.evaluate(() => matchMedia("(prefers-color-scheme: dark)").matches)).toBe(true);
    await client.reload();
    expect(await client.evaluate(() => (window as unknown as { firstFrame: string[] }).firstFrame)).toEqual(["dark", "dark"]);
  });

  it("filters its nav and its rows by what is typed in its search", async () => {
    const { shell, page } = await signedIn();
    await page.click("#open-settings");
    const settings = await settingsPage(shell);
    await settings.fill("#settings-search", "width");
    expect(await texts(settings, ".settings-nav .item")).toEqual(["General"]);
    expect(await texts(settings, ".row:not([hidden]) .label > span:first-child")).toEqual(["Transcript width"]);
    await settings.fill("#settings-search", "added");
    expect(await texts(settings, ".settings-nav .item")).toEqual(["This computer"]);
    expect(await texts(settings, ".row:not([hidden]) .label > span:first-child")).toEqual(["Added"]);
    await settings.fill("#settings-search", "");
    // With no console named, Usage stays gone, the search cleared too.
    expect(await texts(settings, ".settings-nav .item")).toEqual([
      "General", "Account", "This computer", "Browser", "Folders and permissions", "SkillsLater", "ConnectorsLater",
    ]);
    expect(await texts(settings, ".settings-nav h3")).toEqual(["Settings", "Customize"]);
  });

  it("dims the system's controls with the window while it is open, and fills the window as Claude's does", async () => {
    const { shell, page } = await signedIn();
    await shell.evaluate(({ BrowserWindow, nativeTheme }) => {
      const window = BrowserWindow.getAllWindows()[0]!;
      window.setBounds({ x: 0, y: 0, width: 1600, height: 1000 });
      const set = window.setTitleBarOverlay.bind(window);
      const overlays: unknown[] = [];
      Object.assign(globalThis, { overlays });
      window.setTitleBarOverlay = (overlay) => {
        overlays.push(overlay);
        set(overlay);
      };
      nativeTheme.themeSource = "dark";
    });
    const colours = () => shell.evaluate(() => (globalThis as unknown as { overlays: Array<{ color: string }> }).overlays.map((overlay) => overlay.color));
    await expect.poll(colours).toEqual(["#1a1a19"]);
    await page.click("#open-settings");
    const settings = await settingsPage(shell);
    await expect.poll(colours).toEqual(["#1a1a19", "#0c0c0b"]);
    const { width, height } = await settings.$eval(".dialog", (found) => found.getBoundingClientRect().toJSON() as DOMRect);
    const content = await shell.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0]!.getContentBounds());
    expect([width, height]).toEqual([content.width - 96, content.height - 96]);
    await settings.click("#close");
    await expect.poll(colours).toEqual(["#1a1a19", "#0c0c0b", "#1a1a19"]);
  });

  it("names each segmented control as a group, by its row, for a screen reader", async () => {
    const { shell, page } = await signedIn();
    await page.click("#open-settings");
    const settings = await settingsPage(shell);
    const groups = await settings.$$eval(".segmented", (found) => found.map((control) =>
      [control.getAttribute("role"), control.getAttribute("aria-label"), control.closest<HTMLElement>(".row")!.dataset.label]));
    expect(groups.filter(([role, label, row]) => role !== "group" || label !== row)).toEqual([]);
    expect(groups.map(([, label]) => label)).toEqual(expect.arrayContaining(["Theme", "Transcript text size", "Transcript width", "Motion"]));
  });

  it("clears its search on Escape, and closes on the next", async () => {
    const { shell, page } = await signedIn();
    await page.click("#open-settings");
    const settings = await settingsPage(shell);
    const nav = await texts(settings, ".settings-nav .item");
    await settings.fill("#settings-search", "width");
    await settings.press("#settings-search", "Escape");
    expect(await settings.inputValue("#settings-search")).toBe("");
    expect(await texts(settings, ".settings-nav .item")).toEqual(nav);
    expect(await settingsOpen(shell)).toBe(true);
    // Settings can close before the key is acknowledged: that it closed is what tells.
    await settings.press("#settings-search", "Escape").catch(() => {});
    await expect.poll(() => settingsOpen(shell)).toBe(false);
  });

  it("keeps its own page: a dropped file cannot replace it", async () => {
    const { shell, page } = await signedIn();
    await page.click("#open-settings");
    const settings = await settingsPage(shell);
    const dropped = join(home, "dropped.html");
    writeFileSync(dropped, "<!doctype html><title>Dropped</title>");
    await settings.evaluate((url) => {
      location.href = url;
    }, pathToFileURL(dropped).href);
    await new Promise((resolve) => setTimeout(resolve, 500));
    expect(settings.url()).toMatch(/\/settings\.html$/);
    expect(await settings.evaluate(() => document.title)).toBe("Settings");
  });

  it("shows the account, and this computer as the agent registered it", async () => {
    const { shell, page, client } = await signedIn();
    await page.click("#open-settings");
    const settings = await settingsPage(shell);
    await settings.click('[data-section="account"]');
    expect(await settings.textContent("#email")).toBe("flavius@example.com");
    // By its name, not its id.
    expect(await settings.textContent("#organisation")).toBe("Surogate");
    await settings.click('[data-section="computer"]');
    expect(await settings.textContent("#computer")).toBe(hostname());
    expect(await settings.textContent("#connection")).toBe("Connected as Laptop");
    expect(await settings.textContent("#added")).toMatch(/^\w+ \d+, \d{4}$/);
    expect(await settings.textContent("#agents")).toBe(origin.replace("http://", ""));
    // A development build boots the repository's image, and nothing has stopped it yet.
    expect(await settings.textContent("#sandbox")).toBe("Ready");
    expect([await settings.isHidden("#sandbox-log"), await settings.isHidden("#sandbox-retry")]).toEqual([true, true]);
    // What changes while it is open shows at once.
    await client.evaluate(() => window.surogateDesktop!.setAccount(null));
    await settings.click('[data-section="account"]');
    await expect.poll(() => settings.textContent("#email")).toBe("Not signed in");
    // Named by the app's own sign-in, not by what the web client reports.
    expect(await settings.textContent("#organisation")).toBe("Surogate");
  });
});

describe("Settings → General", () => {
  // The value a setting's control shows chosen, or null.
  const pressed = (settings: Page, setting: string) =>
    settings.$eval(`[data-setting="${setting}"]`, (control) => control.querySelector<HTMLElement>('[aria-pressed="true"]')?.dataset.value ?? null)
      .catch(() => null);
  const preferences = () => JSON.parse(readFileSync(join(home, "surogate", "preferences.json"), "utf8")) as Record<string, unknown>;
  const menu = (shell: ElectronApplication) => shell.evaluate(({ Menu }) => Menu.getApplicationMenu()?.items.map((item) => item.label));
  // The app's own questions, as the native box was asked them; and the box held up until it is let go.
  const asked = (shell: ElectronApplication) =>
    shell.evaluate(() => (globalThis as unknown as { asked: Array<{ message?: string }> }).asked.map((options) => options.message));
  const hold = (shell: ElectronApplication) => shell.evaluate(() => Object.assign(globalThis, { hold: true }));
  const release = (shell: ElectronApplication) => shell.evaluate(() => (globalThis as unknown as { release(): void }).release());

  it("starts at login from an entry in the user's own autostart folder, once it is on, and hidden", async () => {
    const { shell, page } = await signedIn();
    // The test's own config folder, as its session's XDG_CONFIG_HOME: never the user's.
    const entry = join(home, "c", "autostart", "surogate.desktop");
    await page.click("#open-settings");
    const settings = await settingsPage(shell);
    expect(await pressed(settings, "startAtLogin")).toBe("off");
    await settings.click('[data-setting="startAtLogin"] [data-value="on"]');
    await expect.poll(() => pressed(settings, "startAtLogin")).toBe("on");
    // A development build starts itself: its Electron, on its main.
    expect(readFileSync(entry, "utf8").split("\n").find((line) => line.startsWith("Exec="))).toBe(`Exec=${ELECTRON} ${MAIN} --hidden`);
    await settings.click('[data-setting="startAtLogin"] [data-value="off"]');
    await expect.poll(() => pressed(settings, "startAtLogin")).toBe("off");
    expect(existsSync(entry)).toBe(false);
  });

  it("starts at login from the user's own ~/.config when XDG_CONFIG_HOME is not an absolute path, as the XDG Base Directory specification says", async () => {
    // A relative one, which would land in the test's own folder were it taken.
    app = await launch(home, { XDG_CONFIG_HOME: relative(process.cwd(), join(home, "relative")) });
    await shellPage(app);
    await app.evaluate(({ Menu }) => Menu.getApplicationMenu()!.getMenuItemById("settings")!.click());
    const settings = await settingsPage(app);
    await settings.click('[data-setting="startAtLogin"] [data-value="on"]');
    await expect.poll(() => pressed(settings, "startAtLogin")).toBe("on");
    // The test's own home: never the user's.
    expect(existsSync(join(home, "h", ".config", "autostart", "surogate.desktop"))).toBe(true);
    expect(existsSync(join(home, "relative", "autostart"))).toBe(false);
  });

  // The main window, once there is one: whether its page still loads, and whether it shows.
  const mainWindow = () => app!.evaluate(({ BrowserWindow }) => {
    const [main] = BrowserWindow.getAllWindows();
    return main ? { loading: main.webContents.isLoading(), visible: main.isVisible() } : null;
  });

  it("shows no window when it starts at login, until it is launched again", async () => {
    app = await launch(home, {}, ["--hidden"]);
    await expect.poll(async () => (await mainWindow())?.loading).toBe(false);
    expect((await mainWindow())?.visible).toBe(false);
    expect(await secondLaunch(home)).toBe(0);
    await expect.poll(async () => (await mainWindow())?.visible).toBe(true);
  });

  it("shows no window at a second start at login while it runs hidden", async () => {
    app = await launch(home, {}, ["--hidden"]);
    await expect.poll(async () => (await mainWindow())?.loading).toBe(false);
    // Started at login again, as a second graphical login of the same user starts it.
    expect(await secondLaunch(home, "--hidden")).toBe(0);
    await new Promise((resolve) => setTimeout(resolve, 1_000));
    expect((await mainWindow())?.visible).toBe(false);
  });

  it("shows no window when it starts at login with the window left maximised", async () => {
    // Maximising a window shows it: one started hidden is maximised once it is first shown.
    mkdirSync(join(home, "surogate"), { recursive: true });
    writeFileSync(join(home, "surogate", "window-state.json"), JSON.stringify({ main: { x: 0, y: 0, width: 1000, height: 700, maximized: true } }));
    app = await launch(home, {}, ["--hidden"]);
    await expect.poll(async () => (await mainWindow())?.loading).toBe(false);
    await new Promise((resolve) => setTimeout(resolve, 1_000));
    expect((await mainWindow())?.visible).toBe(false);
    expect(await secondLaunch(home)).toBe(0);
    await expect.poll(async () => (await mainWindow())?.visible).toBe(true);
  });

  it("keeps the window maximised after a start at login that quits before the window is shown", async () => {
    const state = join(home, "surogate", "window-state.json");
    mkdirSync(join(home, "surogate"), { recursive: true });
    writeFileSync(state, JSON.stringify({ main: { x: 0, y: 0, width: 1000, height: 700, maximized: true } }));
    app = await launch(home, {}, ["--hidden"]);
    await expect.poll(async () => (await mainWindow())?.loading).toBe(false);
    // The tray's Quit Surogate, with no thread working: the window was never shown.
    const exited = once(app.process(), "exit");
    void app.evaluate(({ app: electron }) => electron.quit()).catch(() => {});
    await exited;
    expect(JSON.parse(readFileSync(state, "utf8")).main.maximized).toBe(true);
  });

  it("does not start at login from a build whose path GNOME would not start, and says so", async () => {
    // This build's Electron, as a folder whose name holds a % would give it.
    const percent = join(home, "percent.cjs");
    writeFileSync(percent, `process.execPath = ${JSON.stringify("/opt/100%/electron")};`);
    app = await launch(home, {}, [], [percent]);
    await shellPage(app);
    await app.evaluate(({ Menu }) => Menu.getApplicationMenu()!.getMenuItemById("settings")!.click());
    const settings = await settingsPage(app);
    expect(await settings.textContent("#login-refused")).toBe("This build cannot start at login: GNOME does not start a program whose path holds a %.");
    expect(await settings.isDisabled('[data-setting="startAtLogin"] [data-value="on"]')).toBe(true);
    // Asked all the same, as the page could ask: refused, and nothing is written.
    const set = settings.evaluate(() => (globalThis as unknown as { surogateSettings: { set(key: string, value: string): Promise<void> } })
      .surogateSettings.set("startAtLogin", "on"));
    await expect(set).rejects.toThrow("GNOME does not start a program whose path holds a %");
    expect(existsSync(join(home, "c", "autostart"))).toBe(false);
  });

  it("turns off an entry already there from a build that cannot start at login", async () => {
    // Written by an earlier build, or by the user.
    const entry = join(home, "c", "autostart", "surogate.desktop");
    mkdirSync(dirname(entry), { recursive: true });
    writeFileSync(entry, "[Desktop Entry]\nType=Application\nName=Surogate\nExec=/usr/local/bin/surogate --hidden\n");
    const percent = join(home, "percent.cjs");
    writeFileSync(percent, `process.execPath = ${JSON.stringify("/opt/100%/electron")};`);
    app = await launch(home, {}, [], [percent]);
    await shellPage(app);
    await app.evaluate(({ Menu }) => Menu.getApplicationMenu()!.getMenuItemById("settings")!.click());
    const settings = await settingsPage(app);
    await expect.poll(() => pressed(settings, "startAtLogin")).toBe("on");
    expect(await settings.isDisabled('[data-setting="startAtLogin"] [data-value="on"]')).toBe(true);
    expect(await settings.isDisabled('[data-setting="startAtLogin"] [data-value="off"]')).toBe(false);
    await settings.click('[data-setting="startAtLogin"] [data-value="off"]');
    await expect.poll(() => pressed(settings, "startAtLogin")).toBe("off");
    expect(existsSync(entry)).toBe(false);
  });

  it("does not start at login from a build whose path systemd's autostart reader would misread, and says so", async () => {
    // This build's Electron, as a folder whose name holds a $ would give it.
    const dollar = join(home, "dollar.cjs");
    writeFileSync(dollar, `process.execPath = ${JSON.stringify("/opt/a$b/electron")};`);
    app = await launch(home, {}, [], [dollar]);
    await shellPage(app);
    await app.evaluate(({ Menu }) => Menu.getApplicationMenu()!.getMenuItemById("settings")!.click());
    const settings = await settingsPage(app);
    expect(await settings.textContent("#login-refused")).toBe(
      "This build cannot start at login: KDE and other desktops start it through systemd, which misreads a $, a ` or a \\ in its path.",
    );
    expect(await settings.isDisabled('[data-setting="startAtLogin"] [data-value="on"]')).toBe(true);
    const set = settings.evaluate(() => (globalThis as unknown as { surogateSettings: { set(key: string, value: string): Promise<void> } })
      .surogateSettings.set("startAtLogin", "on"));
    await expect(set).rejects.toThrow("which misreads a $, a ` or a \\ in its path");
    expect(existsSync(join(home, "c", "autostart"))).toBe(false);
  });

  it("quits when the window is closed once Keep running is off, and keeps the choice", async () => {
    const { shell, page } = await signedIn();
    await page.click("#open-settings");
    const settings = await settingsPage(shell);
    // On, as the app starts: closing the window hides it, and the device link stays up.
    expect(await pressed(settings, "keepRunning")).toBe("on");
    await settings.click('[data-setting="keepRunning"] [data-value="off"]');
    await expect.poll(() => pressed(settings, "keepRunning")).toBe("off");
    expect(preferences()).toMatchObject({ keepRunning: false });
    const exited = once(shell.process(), "exit");
    await shell.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0]!.close());
    await exited;
  });

  it("puts Developer in the menu only in developer mode, which it asks about before it turns it on", async () => {
    const { shell, page } = await signedIn();
    expect(await menu(shell)).toEqual(["File", "Edit", "View", "Help"]);
    await page.click("#open-settings");
    const settings = await settingsPage(shell);
    expect(await pressed(settings, "developer")).toBe("off");
    // Cancelled: it stays off.
    await shell.evaluate(() => Object.assign(globalThis, { answer: 1 }));
    await settings.click('[data-setting="developer"] [data-value="on"]');
    await expect.poll(async () => (await asked(shell)).at(-1)).toBe("Turn on developer mode?");
    expect(await pressed(settings, "developer")).toBe("off");
    expect(await menu(shell)).toEqual(["File", "Edit", "View", "Help"]);
    await shell.evaluate(() => Object.assign(globalThis, { answer: 0 }));
    await settings.click('[data-setting="developer"] [data-value="on"]');
    await expect.poll(() => menu(shell)).toEqual(["File", "Edit", "View", "Developer", "Help"]);
    expect(preferences()).toMatchObject({ developer: true });
    // Off again, with no question, and the developer tools it opened close with it.
    const questions = (await asked(shell)).length;
    await shell.evaluate(({ Menu }) => Menu.getApplicationMenu()!.getMenuItemById("dev-agent")!.click());
    const tools = () => shell.evaluate(({ webContents }) => webContents.getAllWebContents().filter((contents) => contents.isDevToolsOpened()).length);
    await expect.poll(tools).toBe(1);
    await settings.click('[data-setting="developer"] [data-value="off"]');
    await expect.poll(() => menu(shell)).toEqual(["File", "Edit", "View", "Help"]);
    await expect.poll(tools).toBe(0);
    expect((await asked(shell)).length).toBe(questions);
  });

  it("asks about developer mode once, however often On is pressed while its question is up", async () => {
    const { shell, page } = await signedIn();
    await page.click("#open-settings");
    const settings = await settingsPage(shell);
    await hold(shell);
    const asking = (await asked(shell)).length + 1;
    await settings.click('[data-setting="developer"] [data-value="on"]');
    await expect.poll(async () => (await asked(shell)).length).toBe(asking);
    await settings.click('[data-setting="developer"] [data-value="on"]');
    await new Promise((resolve) => setTimeout(resolve, 500));
    expect((await asked(shell)).length).toBe(asking);
    // Its one answer turns it on.
    await release(shell);
    await expect.poll(() => pressed(settings, "developer")).toBe("on");
  });

  it("keeps a surogate:// link waiting while it asks about developer mode, and opens it once answered", async () => {
    const chat = "7d2e0f8a-2b3c-4d5e-9f60-718293a4b5c6";
    const { shell, page, client } = await signedIn();
    await page.click("#open-settings");
    const settings = await settingsPage(shell);
    await hold(shell);
    await settings.click('[data-setting="developer"] [data-value="on"]');
    await expect.poll(async () => (await asked(shell)).at(-1)).toBe("Turn on developer mode?");
    const before = client.url();
    expect(await secondLaunch(home, `surogate://open?url=${encodeURIComponent(`${origin}/chat/${chat}`)}`)).toBe(0);
    await new Promise((resolve) => setTimeout(resolve, 500));
    expect(client.url()).toBe(before);
    await release(shell);
    await expect.poll(() => new URL(client.url()).pathname).toBe(`/chat/${chat}`);
  });
});
