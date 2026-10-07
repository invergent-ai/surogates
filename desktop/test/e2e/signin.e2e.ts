import { existsSync, readFileSync, rmSync } from "node:fs";
import { hostname } from "node:os";
import { join } from "node:path";

import type { ElectronApplication, Page } from "playwright-core";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { ACCOUNT, connect, FakeAgent, opened, ROTATED, signIn, signedInAndAdded, webClient as clientOn } from "./fake-agent.js";
import { dataHome, launch, quit, shellPage, stubNative } from "./launch.js";

let home: string;
let agent: FakeAgent;
let origin: string;
let host: string;
let app: ElectronApplication | undefined;

beforeEach(async () => {
  home = dataHome();
  agent = new FakeAgent();
  origin = await agent.start();
  host = origin.replace("http://", "");
});

afterEach(async () => {
  await quit(app);
  app = undefined;
  await agent.stop();
  await agent.link.stop();
  rmSync(home, { recursive: true, force: true });
});

// The app connected to the fake agent, before anyone signs in: its window's page.
async function connected(): Promise<{ shell: ElectronApplication; page: Page }> {
  const shell = await launch(home);
  app = shell;
  await stubNative(shell);
  const page = await shellPage(shell);
  await connect(page, origin);
  await expect.poll(() => page.isVisible("#sign-in")).toBe(true);
  return { shell, page };
}

const webClient = (shell: ElectronApplication) => clientOn(shell, origin);
const webShown = (shell: ElectronApplication) => shell.evaluate(({ BrowserWindow }) =>
  (BrowserWindow.getAllWindows()[0]!.contentView.children[0] as Electron.WebContentsView).getVisible());
const state = (path: string) => join(home, "surogate", path);

describe("the first run", () => {
  it("asks for the agent's address, and says why one is refused without asking anything", async () => {
    app = await launch(home);
    await stubNative(app);
    const page = await shellPage(app);
    await expect.poll(() => page.isVisible("#first-run")).toBe(true);
    expect(await page.isVisible("#sidebar")).toBe(false);
    await connect(page, "http://agent.example.com");
    await expect.poll(() => page.textContent("#error"))
      .toBe("Use an https:// address: http:// is only for this computer's own servers");
    expect(await app.evaluate(() => (globalThis as unknown as { asked: unknown[] }).asked)).toEqual([]);
  });

  it("connects to the agent once the user confirms it, asks them to sign in, and keeps the agent", async () => {
    const { shell, page } = await connected();
    const asked = await shell.evaluate(() => (globalThis as unknown as { asked: Array<{ message: string }> }).asked);
    expect(asked.map((options) => options.message)).toEqual([`Connect to ${host}?`]);
    expect(await page.textContent("#sign-in-title")).toBe(`Sign in to ${host}`);
    expect(await page.isVisible("#sidebar")).toBe(false);
    // The web client is there, in its own partition, and hidden until someone signs in.
    await webClient(shell);
    const view = await shell.evaluate(({ BrowserWindow }) =>
      (BrowserWindow.getAllWindows()[0]!.contentView.children[0] as Electron.WebContentsView).webContents.session.getStoragePath());
    expect(view).toMatch(new RegExp(`^${join(home, "surogate", "electron", "Partitions")}/agent-[0-9a-f]{32}$`));
    expect(await webShown(shell)).toBe(false);
    await quit(shell);

    app = await launch(home);
    const again = await shellPage(app);
    await expect.poll(() => again.isVisible("#sign-in")).toBe(true);
    expect(await again.isVisible("#first-run")).toBe(false);
    await webClient(app);
    expect(await webShown(app)).toBe(false);
  });

  it("connects once when its address is sent twice", async () => {
    app = await launch(home);
    await stubNative(app);
    const page = await shellPage(app);
    await page.fill("#address", origin);
    await page.evaluate(() => {
      const form = document.getElementById("connect") as HTMLFormElement;
      form.requestSubmit();
      form.requestSubmit();
    });
    await webClient(app);
    await new Promise((resolve) => setTimeout(resolve, 500));
    const asked = await app.evaluate(() => (globalThis as unknown as { asked: Array<{ message: string }> }).asked);
    expect(asked.map((options) => options.message)).toEqual([`Connect to ${host}?`]);
    expect(await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0]!.contentView.children.length)).toBe(1);
  });
});

describe("signing in", () => {
  it("happens in the system browser, then shows the web client and adds this computer, and lasts across a launch", async () => {
    const { shell, page } = await connected();
    expect(await signIn(shell, page, agent)).toContain("Signed in. You can close this tab and return to Surogate.");
    const [authorize] = await opened(shell);
    expect(authorize).toMatch(new RegExp(`^${origin}/oauth/authorize\\?`));
    expect(new URL(authorize!).searchParams.get("computer")).toBe(hostname());
    await expect.poll(() => page.isVisible("#sign-in")).toBe(false);
    await expect.poll(() => page.getAttribute("#device", "title")).toBe("Connected as Laptop");
    expect(agent.registered).toEqual([{ name: hostname() }]);
    await expect.poll(() => webShown(shell)).toBe(true);
    // The app's own sign-in, before the web client has said who it is.
    expect(await page.textContent("#user-name")).toBe(ACCOUNT.name);
    const hole = await page.evaluate(() => document.querySelector("#hole")!.getBoundingClientRect().toJSON() as DOMRect);
    const bounds = await shell.evaluate(({ BrowserWindow }) =>
      (BrowserWindow.getAllWindows()[0]!.contentView.children[0] as Electron.WebContentsView).getBounds());
    expect(bounds).toEqual({ x: Math.round(hole.x), y: Math.round(hole.y), width: Math.round(hole.width), height: Math.round(hole.height) });
    // The basic store keeps the tokens as they are, and the shell says so.
    expect(await page.textContent("#notice")).toBe("Credentials on this computer are not encrypted: Linux has no secret store here");
    expect(JSON.parse(readFileSync(state("session.json"), "utf8"))).toMatchObject({ account: ACCOUNT, plain: "rt-1" });
    expect(readFileSync(state("credentials.json"), "utf8")).toContain('"deviceId": "d"');
    await quit(shell);

    const links = agent.link.connections;
    app = await launch(home);
    const again = await shellPage(app);
    await expect.poll(() => again.getAttribute("#device", "title")).toBe("Connected as Laptop");
    expect(await again.isVisible("#sign-in")).toBe(false);
    expect(agent.link.connections).toBe(links + 1);
    expect(agent.registered).toHaveLength(1);
  });

  it("gives the web client a session of its own, from a one-time code", async () => {
    const { shell, page } = await connected();
    await signIn(shell, page, agent);
    const client = await webClient(shell);
    expect(await client.evaluate(() => window.surogateDesktop!.webSignIn())).toEqual({ code: "web-code" });
  });

  it("says a sign-in declined in the browser was declined, and Continue starts again", async () => {
    const { shell, page } = await connected();
    await page.click("#sign-in-button");
    await expect.poll(async () => (await opened(shell)).length).toBe(1);
    expect(await agent.deny((await opened(shell))[0]!)).toContain("Surogate is not signed in");
    await expect.poll(() => page.textContent("#sign-in-error")).toBe("Sign-in was declined in the browser");
    await signIn(shell, page, agent);
    await expect.poll(() => page.isVisible("#sign-in")).toBe(false);
  });

  it("asks for a newer sign-in when the agent will not add this computer on this one", async () => {
    agent.recent = false;
    const { shell, page } = await connected();
    await signIn(shell, page, agent);
    await expect.poll(() => page.textContent("#device-action-text")).toBe(`Sign in again to let ${host} work on folders of this computer.`);
    agent.recent = true;
    const before = (await opened(shell)).length;
    await page.click("#device-action-button");
    await expect.poll(async () => (await opened(shell)).length).toBe(before + 1);
    let release = () => {};
    agent.pagesHeld = new Promise((resolve) => {
      release = resolve;
    });
    await agent.approve((await opened(shell))[before]!);
    // The window, cleared, loads the web client again with the new session: the sign-in shows meanwhile.
    await expect.poll(() => page.isVisible("#sign-in")).toBe(true);
    expect(await webShown(shell)).toBe(false);
    release();
    await expect.poll(() => page.isVisible("#sign-in")).toBe(false);
    await expect.poll(() => page.getAttribute("#device", "title")).toBe("Connected as Laptop");
    expect(await page.isVisible("#device-action")).toBe(false);
  });

  // Signed in and added, then that sign-in ended (as one does after 30 days) while the computer
  // stays, on a token of its own: the app, launched again, with nobody signed in.
  async function signInEnded(): Promise<{ shell: ElectronApplication; page: Page }> {
    const first = await connected();
    await signedInAndAdded(first.shell, first.page, agent);
    await quit(first.shell);
    rmSync(state("session.json"));
    app = await launch(home);
    await stubNative(app);
    const page = await shellPage(app);
    await expect.poll(() => page.isVisible("#sign-in")).toBe(true);
    return { shell: app, page };
  }

  it("binds a later sign-in to the computer it keeps, which runs on the new token the agent issues for it", async () => {
    const { shell, page } = await signInEnded();
    await signIn(shell, page, agent);
    // Revoking the computer at the agent now ends this sign-in too.
    await expect.poll(() => agent.reauthorized).toEqual(["Bearer at-2"]);
    await expect.poll(() => page.getAttribute("#device", "title")).toBe("Connected as Laptop");
    expect(readFileSync(state("credentials.json"), "utf8")).toContain(ROTATED);
    expect(agent.registered).toHaveLength(1);
  });

  it("stays signed in once the agent bound the sign-in, though the computer's link cannot connect: the computer shows offline until it can", async () => {
    const { shell, page } = await signInEnded();
    await expect.poll(() => page.getAttribute("#device", "title")).toBe("Connected as Laptop");
    agent.link.refusing = true;
    await signIn(shell, page, agent);
    await expect.poll(() => agent.reauthorized).toEqual(["Bearer at-2"]);
    await expect.poll(() => page.getAttribute("#device", "title"), { timeout: 5_000 }).toBe("Offline: reconnecting");
    expect(await page.isVisible("#sign-in")).toBe(false);
    expect(existsSync(state("session.json"))).toBe(true);
    // The new token is its only copy: kept, whatever the link does.
    expect(readFileSync(state("credentials.json"), "utf8")).toContain(ROTATED);
    agent.link.refusing = false;
    await expect.poll(() => page.getAttribute("#device", "title"), { timeout: 15_000 }).toBe("Connected as Laptop");
  });

  it("signs out rather than keep a sign-in it cannot bind to the computer", async () => {
    const { shell, page } = await signInEnded();
    agent.reauthorizeStatus = 500;
    await page.click("#sign-in-button");
    await expect.poll(async () => (await opened(shell)).length).toBe(1);
    await agent.approve((await opened(shell))[0]!);
    await expect.poll(() => page.textContent("#sign-in-error"))
      .toBe("Surogate could not tie this sign-in to this computer, so it signed out: The agent did not reauthorize this computer (HTTP 500)");
    expect(existsSync(state("session.json"))).toBe(false);
    expect(agent.oauth.at(-1)).toMatchObject({ token: "rt-2" });
    expect(await webShown(shell)).toBe(false);
  });

  it("ends at the agent a sign-in that fails once its tokens are in hand: none is left valid with no copy here", async () => {
    agent.meStatus = 500;
    const { shell, page } = await connected();
    await page.click("#sign-in-button");
    await expect.poll(async () => (await opened(shell)).length).toBe(1);
    await agent.approve((await opened(shell))[0]!);
    await expect.poll(() => page.textContent("#sign-in-error")).toBe("The agent did not say who signed in (HTTP 500)");
    await expect.poll(() => agent.oauth.filter((form) => "token" in form)).toEqual([expect.objectContaining({ token: "rt-1" })]);
    expect(existsSync(state("session.json"))).toBe(false);
  });

  it("removes the device the agent made when its token connects as another agent, and keeps nothing", async () => {
    agent.config = { ...agent.config, agent_id: "another" };
    const { shell, page } = await connected();
    await signIn(shell, page, agent);
    await expect.poll(() => agent.deleted).toEqual(["d"]);
    expect(await page.getAttribute("#device", "title")).toBe("Sign in to this agent to let it work on folders of this computer");
    expect(existsSync(state("credentials.json"))).toBe(false);
  });
});

describe("the agent's web client", () => {
  // Signed in, with the web client showing, and this computer added.
  async function signedIn(): Promise<{ shell: ElectronApplication; page: Page; client: Page }> {
    const { shell, page } = await connected();
    await signedInAndAdded(shell, page, agent);
    const client = await webClient(shell);
    await client.evaluate((account) => window.surogateDesktop!.setAccount(account), ACCOUNT);
    return { shell, page, client };
  }

  it("stays on the agent's origin, and sends other addresses to the system browser", async () => {
    const { shell, client } = await signedIn();
    const before = (await opened(shell)).length;
    await client.evaluate(() => {
      location.href = "https://example.com/elsewhere";
    });
    await expect.poll(async () => (await opened(shell)).slice(before)).toEqual(["https://example.com/elsewhere"]);
    await client.evaluate(() => window.open("https://example.com/popup", "_blank"));
    await expect.poll(async () => (await opened(shell)).slice(before)).toEqual(["https://example.com/elsewhere", "https://example.com/popup"]);
    expect(new URL(client.url()).origin).toBe(origin);
  });

  it("opens the Composio sign-in as a popup with no bridge", async () => {
    const { shell, client } = await signedIn();
    const popped = shell.waitForEvent("window");
    await client.evaluate(() => void window.open("https://connect.composio.dev/link/abc", "composio-oauth", "popup=yes"));
    await popped;
    const preferences = await shell.evaluate(({ BrowserWindow }) => {
      const popup = BrowserWindow.getAllWindows().find((window) => !window.webContents.getURL().endsWith("/shell.html"))!;
      // Electron's own, though its typings leave it out: the preferences the popup was made with.
      const made = popup.webContents as unknown as { getLastWebPreferences(): Electron.WebPreferences | null };
      const { preload, additionalArguments } = made.getLastWebPreferences() ?? {};
      return { preload: preload ?? null, additionalArguments: additionalArguments ?? [] };
    });
    expect(preferences).toEqual({ preload: null, additionalArguments: [] });
  });

  it("is replaced by what failed when the agent cannot be reached, and comes back on Refresh", async () => {
    const { shell, page } = await signedIn();
    const port = Number(new URL(origin).port);
    await agent.stop();
    await shell.evaluate(({ BrowserWindow }) =>
      (BrowserWindow.getAllWindows()[0]!.contentView.children[0] as Electron.WebContentsView).webContents.reload());
    await expect.poll(() => page.isVisible("#unreachable")).toBe(true);
    expect(await page.textContent("#headline")).toBe(`Couldn't connect to ${host}`);
    await agent.start(port);
    await page.click("#refresh");
    await expect.poll(() => page.isVisible("#unreachable")).toBe(false);
  });

  it.each(["/oauth/authorize", "/OAuth/Authorize"])("keeps the agent's sign-in pages out of the window, at %s: the password form is only ever in the browser", async (path) => {
    const { shell, client } = await signedIn();
    const before = { url: client.url(), opened: (await opened(shell)).length };
    await client.evaluate((to) => {
      location.href = `${to}?client_id=surogate-desktop`;
    }, path);
    await new Promise((resolve) => setTimeout(resolve, 500));
    expect(client.url()).toBe(before.url);
    expect((await opened(shell)).length).toBe(before.opened);
  });

  it("prepares a folder as often as asked, and leaves no listener behind", async () => {
    const { shell, client } = await signedIn();
    const listeners = () => shell.evaluate(({ BrowserWindow }) => {
      const view = (BrowserWindow.getAllWindows()[0]!.contentView.children[0] as Electron.WebContentsView).webContents;
      return ["did-navigate", "did-fail-load", "destroyed"].map((name) => view.listenerCount(name));
    });
    const before = await listeners();
    // The folder dialog is cancelled each time.
    for (let time = 0; time < 12; time += 1) {
      expect(await client.evaluate(() => window.surogateDesktop!.prepareFolder("pick"))).toBeNull();
    }
    expect(await listeners()).toEqual(before);
  });

  it("answers only the account it was registered for", async () => {
    const { client } = await signedIn();
    await client.evaluate((other) => window.surogateDesktop!.setAccount(other), { ...ACCOUNT, userId: "b", email: "b@example.com" });
    expect(await client.evaluate(() => window.surogateDesktop!.getDevice())).toEqual({ device: null, localFolders: false });
    await expect(client.evaluate(() => window.surogateDesktop!.prepareFolder("pick"))).rejects.toThrow("another account");
    await expect(client.evaluate(() => window.surogateDesktop!.bindSession("0b6f3c1e-8a2d-4c5e-9f10-1a2b3c4d5e6f", "a".repeat(43))))
      .rejects.toThrow("another account");
    // Its own account again: the computer is theirs.
    await client.evaluate((account) => window.surogateDesktop!.setAccount(account), ACCOUNT);
    expect(await client.evaluate(() => window.surogateDesktop!.getDevice())).toEqual({
      device: { deviceId: "d", name: "Laptop" }, localFolders: true,
    });
  });
});
