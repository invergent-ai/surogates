import { once } from "node:events";
import { readFileSync, rmSync, writeFileSync } from "node:fs";
import { hostname } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

import type { ElectronApplication, Page } from "playwright-core";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { ACCOUNT, connect, FakeAgent, signedInAndAdded, webClient } from "./fake-agent.js";
import { dataHome, launch, quit, secondLaunch, shellPage, stubNative } from "./launch.js";

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
    // An agent off surogate.ai has no console the app knows: no usage or billing.
    expect(await texts(page, "#user-menu .menu-item")).toEqual(["SettingsCtrl+Shift+,", "Language", "Get help", "Log out", "Remove this agent…"]);
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
    // Off surogate.ai there is no console; and a name that is no link, a prototype's included, opens nothing.
    for (const which of ["usage", "billing", "constructor", "__proto__"]) {
      await expect(page.evaluate((name) => (window as unknown as { surogateShell: { link(which: string): Promise<void> } })
        .surogateShell.link(name), which)).rejects.toThrow("No such link");
    }
    expect(await shell.evaluate(() => (globalThis as unknown as { opened: unknown[] }).opened.length)).toBe(2);
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
      expect(await settings.getAttribute(`[data-setting="theme"] [data-value="${theme}"]`, "aria-pressed")).toBe("true");
    }
    const overlays = await shell.evaluate(() => (globalThis as unknown as { overlays: Array<{ color: string }> }).overlays);
    expect(overlays.slice(-2).map((overlay) => overlay.color)).toEqual(["#1a1a19", "#f5f4ed"]);
    expect(await client.evaluate(() => (window as unknown as { themes: string[] }).themes.slice(-2))).toEqual(["dark", "light"]);
    await settings.click('[data-setting="textSize"] [data-value="large"]');
    const saved = () => JSON.parse(readFileSync(join(home, "surogate", "settings.json"), "utf8")) as Record<string, string>;
    await expect.poll(() => saved().textSize).toBe("large");
    expect(saved().theme).toBe("light");
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
    // Off surogate.ai Usage stays gone, the search cleared too.
    expect(await texts(settings, ".settings-nav .item")).toEqual([
      "General", "Account", "This computer", "Folders and permissionsLater", "SkillsLater", "ConnectorsLater",
    ]);
    expect(await texts(settings, ".settings-nav h3")).toEqual(["Settings", "Customize"]);
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
    expect(await settings.isDisabled('[data-section="folders"]')).toBe(true);
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
