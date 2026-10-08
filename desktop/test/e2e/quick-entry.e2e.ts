// Quick entry, through the real app: Claude Desktop's frameless box over every other window,
// opened from the tray, whose text starts the chat New starts. The agent's page hears the text
// through the bridge, as the web client's chat page does, sends it as that chat's first message, and
// says so: until then the box keeps the text.

import { rmSync } from "node:fs";

import type { ElectronApplication, Page } from "playwright-core";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { connect, FakeAgent, signedInAndAdded, webClient } from "./fake-agent.js";
import { dataHome, launch, pickInTray, quit, shellPage, stubNative, trayLabels, watchTray } from "./launch.js";

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

// The app signed in and registered, its tray watched: the window's page and the agent's web client.
async function signedIn(): Promise<{ page: Page; client: Page }> {
  app = await launch(home);
  await stubNative(app);
  await watchTray(app);
  const page = await shellPage(app);
  await connect(page, origin);
  await signedInAndAdded(app, page, agent);
  return { page, client: await webClient(app, origin) };
}

// Quick entry's window, by its page, and what it is like on the screen.
const QUICK = "/quick.html";
const quickWindow = (shell: ElectronApplication) => shell.evaluate(({ BrowserWindow }, page) => {
  const found = BrowserWindow.getAllWindows().find((window) => window.webContents.getURL().endsWith(page));
  return found ? { bounds: found.getBounds(), onTop: found.isAlwaysOnTop(), shown: found.isVisible(), resizable: found.isResizable() } : null;
}, QUICK);

async function quickPage(shell: ElectronApplication): Promise<Page> {
  let found: Page | undefined;
  await expect.poll(() => {
    found = shell.windows().find((page) => page.url().endsWith(QUICK));
    return found !== undefined;
  }).toBe(true);
  await found!.waitForSelector("#text");
  await expect.poll(async () => (await quickWindow(shell))?.shown).toBe(true);
  return found!;
}

const mainShown = (shell: ElectronApplication) => shell.evaluate(({ BrowserWindow }) =>
  BrowserWindow.getAllWindows().find((window) => window.webContents.getURL().endsWith("/shell.html"))!.isVisible());

// What the agent's page hears from quick entry, from now on: it listens once it has loaded, as the web client's chat page does.
type Heard = { id: string; text: string };
const listen = (client: Page) => client.evaluate(() => {
  const heard: Heard[] = [];
  Object.assign(window, { heard });
  window.surogateDesktop!.onQuickEntry!((message) => heard.push(message));
});
const heard = (client: Page) => client.evaluate(() => (window as unknown as { heard?: Heard[] }).heard ?? null);
// The page's word on the last it heard: null once it sent it, or why it did not.
const answer = (client: Page, refused: string | null) => client.evaluate((said) => {
  const { heard } = window as unknown as { heard: Heard[] };
  window.surogateDesktop!.answerQuickEntry!(heard.at(-1)!.id, said);
}, refused);

// Whether Settings is open over the window: quick entry's is a window too.
const settingsOpen = (shell: ElectronApplication) => shell.evaluate(({ BrowserWindow }) =>
  BrowserWindow.getAllWindows().find((window) => window.webContents.getURL().endsWith("/shell.html"))!.contentView.children
    .some((view) => (view as Electron.WebContentsView).webContents.getURL().endsWith("/settings.html")));
// The agent's view, and a load of *path* there, as a link of the window's would start.
const loadsNow = (shell: ElectronApplication) => shell.evaluate(({ webContents }, at) =>
  webContents.getAllWebContents().find((contents) => contents.getURL().startsWith(at))!.isLoading(), origin);
const loadInView = (shell: ElectronApplication, path: string) => shell.evaluate(({ webContents }, [at, to]) => {
  void webContents.getAllWebContents().find((contents) => contents.getURL().startsWith(at))!.loadURL(`${at}${to}`).catch(() => {});
}, [origin, path] as const);

// What the main process hands the agent's page from quick entry, from now on.
const watchHanded = (shell: ElectronApplication) => shell.evaluate(({ webContents }, at) => {
  const view = webContents.getAllWebContents().find((contents) => contents.getURL().startsWith(at))!;
  const send = view.send.bind(view);
  const handed: unknown[] = [];
  Object.assign(globalThis, { handed });
  view.send = (channel: string, ...args: unknown[]) => {
    if (channel === "desktop:quick-entry") handed.push(args[0]);
    send(channel, ...args);
  };
}, origin);
const handed = (shell: ElectronApplication) => shell.evaluate(() => (globalThis as unknown as { handed: unknown[] }).handed);

describe("quick entry", () => {
  it("opens from the tray as a frameless 606 by 470 window over every other, which Escape hides, its text kept", async () => {
    await signedIn();
    await expect.poll(() => trayLabels(app!)).toContain("Quick entry");
    await pickInTray(app!, "Quick entry");
    const quick = await quickPage(app!);
    expect(await quickWindow(app!)).toMatchObject({ bounds: { width: 606, height: 470 }, onTop: true, resizable: false });
    await quick.fill("#text", "Draft the March invoices");
    await quick.press("#text", "Escape");
    await expect.poll(async () => (await quickWindow(app!))?.shown).toBe(false);
    await pickInTray(app!, "Quick entry");
    await expect.poll(async () => (await quickWindow(app!))?.shown).toBe(true);
    expect(await quick.inputValue("#text")).toBe("Draft the March invoices");
  });

  it("starts the chat New starts with its text, which the chat's page hears once it has loaded and listens, and keeps it until the page sent it", async () => {
    const { page, client } = await signedIn();
    await watchHanded(app!);
    await client.evaluate(() => history.pushState(null, "", "/inbox"));
    // Settings open over the window: the new chat opens on the window itself.
    await page.click("#open-settings");
    await expect.poll(() => settingsOpen(app!)).toBe(true);
    await app!.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0]!.hide());
    await pickInTray(app!, "Quick entry");
    const quick = await quickPage(app!);
    // An Enter in a box of spaces sends nothing; an Enter or an Escape that ends or cancels a composition is the composition's.
    await quick.fill("#text", "   ");
    await quick.press("#text", "Enter");
    await expect.poll(async () => (await quickWindow(app!))?.shown).toBe(false);
    expect(new URL(client.url()).pathname).toBe("/inbox");
    await pickInTray(app!, "Quick entry");
    await expect.poll(async () => (await quickWindow(app!))?.shown).toBe(true);
    await quick.fill("#text", "Draft the March invoices");
    for (const key of ["Enter", "Escape"]) {
      await quick.evaluate((pressed) => document.getElementById("text")!.dispatchEvent(new KeyboardEvent("keydown", { key: pressed, isComposing: true, bubbles: true })), key);
    }
    // Shift+Enter and Alt+Enter are new lines.
    await quick.press("#text", "End");
    await quick.press("#text", "Shift+Enter");
    await quick.type("#text", "from the ledger");
    await quick.press("#text", "Alt+Enter");
    await quick.type("#text", "and the bank");
    expect((await quickWindow(app!))?.shown).toBe(true);
    expect(await settingsOpen(app!)).toBe(true);
    await quick.press("#text", "Enter");
    const text = "Draft the March invoices\nfrom the ledger\nand the bank";
    await expect.poll(async () => (await quickWindow(app!))?.shown).toBe(false);
    // Settings closes, the window shows, and its web client loads /chat, as New opens it.
    await expect.poll(() => mainShown(app!)).toBe(true);
    expect(await settingsOpen(app!)).toBe(false);
    await expect.poll(() => new URL(client.url()).pathname).toBe("/chat");
    await client.waitForLoadState();
    // Handed before the page listens, by an id of its own: its preload holds it for the first listener.
    await expect.poll(() => handed(app!)).toEqual([{ id: expect.any(String), text }]);
    await listen(client);
    await expect.poll(() => heard(client)).toEqual(await handed(app!));
    // The box keeps the text until the page says it sent it: then it is empty for the next one.
    expect(await quick.inputValue("#text")).toBe(text);
    await answer(client, null);
    await expect.poll(() => quick.inputValue("#text")).toBe("");
    expect(await quick.textContent("#refused")).toBe("");
  });

  it("keeps the text, and says why, when the chat it was for never had it: a load in its place, the page gone from it, the page's refusal", async () => {
    const { client } = await signedIn();
    await watchHanded(app!);
    await pickInTray(app!, "Quick entry");
    const quick = await quickPage(app!);
    const send = async (text: string) => {
      if (!(await quickWindow(app!))?.shown) await pickInTray(app!, "Quick entry");
      await expect.poll(async () => (await quickWindow(app!))?.shown).toBe(true);
      await quick.fill("#text", text);
      await quick.press("#text", "Enter");
    };
    const said = async (refused: string, text: string) => {
      await expect.poll(() => quick.textContent("#refused")).toBe(refused);
      expect(await quick.inputValue("#text")).toBe(text);
    };
    // Another load takes the new chat's place while it still loads: nothing is handed.
    const paused = Promise.withResolvers<void>();
    agent.pagesHeld = paused.promise;
    await send("Draft the March invoices");
    await expect.poll(() => loadsNow(app!)).toBe(true);
    await loadInView(app!, "/inbox");
    await said("Surogate's window left the new chat before it was made, so nothing was sent.", "Draft the March invoices");
    agent.pagesHeld = null;
    paused.resolve();
    await expect.poll(() => new URL(client.url()).pathname).toBe("/inbox");
    expect(await handed(app!)).toEqual([]);
    // Handed, but the page leaves the chat before it listens: it never hears it.
    await send("Draft the April invoices");
    await expect.poll(() => handed(app!)).toHaveLength(1);
    await client.waitForLoadState();
    await client.evaluate(() => history.pushState(null, "", "/inbox"));
    await said("The agent's page left the new chat before it heard the message, so nothing was sent.", "Draft the April invoices");
    await listen(client);
    expect(await heard(client)).toEqual([]);
    // Heard, and refused by the page, in its own words.
    await send("Draft the May invoices");
    await expect.poll(() => handed(app!)).toHaveLength(2);
    await client.waitForLoadState();
    await listen(client);
    await expect.poll(() => heard(client)).toHaveLength(1);
    await answer(client, "You declined the agent's AI disclosure, so nothing was sent.");
    await said("You declined the agent's AI disclosure, so nothing was sent.", "Draft the May invoices");
    // A page that says nobody is signed in to it is handed nothing.
    await client.evaluate(() => window.surogateDesktop!.setAccount(null));
    await send("Draft the June invoices");
    await said("Sign in to your agent in Surogate's window first.", "Draft the June invoices");
    expect(await handed(app!)).toHaveLength(2);
  });

  it("hands nothing to a page that says nobody is signed in to it while the new chat loads", async () => {
    const { client } = await signedIn();
    await watchHanded(app!);
    // The page says so as the load of the new chat begins, which the agent holds meanwhile.
    const nobody = new Promise<void>((resolve) => client.on("console", (message) => {
      if (message.text() === "nobody") resolve();
    }));
    await client.evaluate(() => addEventListener("beforeunload", () => {
      void window.surogateDesktop!.setAccount(null).then(() => console.log("nobody"));
    }));
    const paused = Promise.withResolvers<void>();
    agent.pagesHeld = paused.promise;
    await pickInTray(app!, "Quick entry");
    const quick = await quickPage(app!);
    await quick.fill("#text", "Draft the March invoices");
    await quick.press("#text", "Enter");
    await nobody;
    agent.pagesHeld = null;
    paused.resolve();
    await expect.poll(() => quick.textContent("#refused")).toBe("Sign in to your agent in Surogate's window first.");
    expect(await quick.inputValue("#text")).toBe("Draft the March invoices");
    expect(new URL(client.url()).pathname).toBe("/chat");
    expect(await handed(app!)).toEqual([]);
  });

  it("says why it sent nothing while the agent cannot be reached, and keeps the text", async () => {
    const { page, client } = await signedIn();
    agent.pagesRedirect = "https://sso.example.com/login";
    await client.reload().catch(() => {});
    await expect.poll(() => page.isVisible("#unreachable")).toBe(true);
    await pickInTray(app!, "Quick entry");
    const quick = await quickPage(app!);
    await quick.fill("#text", "Draft the March invoices");
    await quick.press("#text", "Enter");
    await expect.poll(() => quick.textContent("#refused")).toBe(`Surogate cannot reach ${new URL(origin).host} right now, so nothing was sent.`);
    expect((await quickWindow(app!))?.shown).toBe(true);
    expect(await quick.inputValue("#text")).toBe("Draft the March invoices");
  });

  it("shows the window instead while nobody is signed in, which asks them to", async () => {
    app = await launch(home);
    await stubNative(app);
    await watchTray(app);
    const page = await shellPage(app);
    await connect(page, origin);
    await expect.poll(() => page.isVisible("#sign-in")).toBe(true);
    await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0]!.hide());
    await expect.poll(() => trayLabels(app!)).toContain("Quick entry");
    await pickInTray(app, "Quick entry");
    await expect.poll(() => mainShown(app!)).toBe(true);
    expect(await quickWindow(app)).toBeNull();
  });
});
