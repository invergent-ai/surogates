import { existsSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { hostname } from "node:os";
import { join } from "node:path";

import type { ElectronApplication, Page } from "playwright-core";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { ACCOUNT, connect, FakeAgent, register, webClient as clientOn } from "./fake-agent.js";
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

// The app connected to the fake agent: its window's page, and the agent's web client.
async function connected(): Promise<{ shell: ElectronApplication; page: Page; client: Page }> {
  const shell = await launch(home);
  app = shell;
  await stubNative(shell);
  const page = await shellPage(shell);
  await connect(page, origin);
  return { shell, page, client: await webClient(shell) };
}

const webClient = (shell: ElectronApplication) => clientOn(shell, origin);

const opened = (shell: ElectronApplication) => shell.evaluate(() => (globalThis as unknown as { opened: string[] }).opened);

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

  it("connects to the agent once the user confirms it, shows its web client, and keeps it", async () => {
    const { shell, page, client } = await connected();
    const asked = await shell.evaluate(() => (globalThis as unknown as { asked: Array<{ message: string }> }).asked);
    expect(asked.map((options) => options.message)).toEqual([`Connect to ${host}?`]);
    expect(await client.title()).toBe("Fake agent");
    expect(await page.isVisible("#first-run")).toBe(false);
    const hole = await page.evaluate(() => document.querySelector("#hole")!.getBoundingClientRect().toJSON() as DOMRect);
    const view = await shell.evaluate(({ BrowserWindow }) => {
      const child = BrowserWindow.getAllWindows()[0]!.contentView.children[0] as Electron.WebContentsView;
      return { bounds: child.getBounds(), storage: child.webContents.session.getStoragePath() };
    });
    expect(view.bounds).toEqual({
      x: Math.round(hole.x), y: Math.round(hole.y), width: Math.round(hole.width), height: Math.round(hole.height),
    });
    expect(view.storage).toMatch(new RegExp(`^${join(home, "surogate", "electron", "Partitions")}/agent-[0-9a-f]{32}$`));
    await quit(shell);

    app = await launch(home);
    const again = await shellPage(app);
    await webClient(app);
    expect(await again.isVisible("#first-run")).toBe(false);
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

describe("the agent's web client", () => {
  it("stays on the agent's origin, and sends other addresses to the system browser", async () => {
    const { shell, client } = await connected();
    await client.evaluate(() => {
      location.href = "https://example.com/elsewhere";
    });
    await expect.poll(() => opened(shell)).toEqual(["https://example.com/elsewhere"]);
    await client.evaluate(() => window.open("https://example.com/popup", "_blank"));
    await expect.poll(() => opened(shell)).toEqual(["https://example.com/elsewhere", "https://example.com/popup"]);
    expect(new URL(client.url()).origin).toBe(origin);
  });

  it("opens the Composio sign-in as a popup with no bridge", async () => {
    const { shell, client } = await connected();
    const opened = shell.waitForEvent("window");
    await client.evaluate(() => void window.open("https://connect.composio.dev/link/abc", "composio-oauth", "popup=yes"));
    await opened;
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
    const { shell, page } = await connected();
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

  it("registers this computer with the token the agent issued, and keeps it for the next launch", async () => {
    const { shell, page, client } = await connected();
    const { before, device } = await register(client);
    expect(before).toEqual({ device: null, computerName: hostname(), localFolders: true });
    expect(device).toEqual({ deviceId: "d", name: "Laptop" });
    await expect.poll(() => page.getAttribute("#device", "title")).toBe("Connected as Laptop");
    expect((await client.evaluate(() => window.surogateDesktop!.getDevice())).device).toEqual({ deviceId: "d", name: "Laptop" });
    // The basic store keeps tokens as they are, and the shell says so.
    expect(await page.textContent("#notice")).toBe("Credentials on this computer are not encrypted: Linux has no secret store here");
    expect(readFileSync(join(home, "surogate", "credentials.json"), "utf8")).toContain('"deviceId": "d"');
    expect(existsSync(join(home, "surogate", "devices", "d", "journal.sqlite"))).toBe(true);
    await quit(shell);

    const links = agent.link.connections;
    app = await launch(home);
    const again = await shellPage(app);
    await expect.poll(() => again.getAttribute("#device", "title")).toBe("Connected as Laptop");
    expect(agent.link.connections).toBe(links + 1);
  });

  it("says so when the agent revokes this computer's access while the app runs", async () => {
    const { page, client } = await connected();
    await register(client);
    await expect.poll(() => page.getAttribute("#device", "title")).toBe("Connected as Laptop");
    agent.link.close(4403);
    await expect.poll(() => page.getAttribute("#device", "title")).toBe("Local access revoked");
    expect(await page.getAttribute("#device", "class")).toContain("ended");
  });

  it("tells a relaunch that it is registered while its device still starts, so no second device is registered", async () => {
    const first = await connected();
    await register(first.client);
    await expect.poll(() => first.page.getAttribute("#device", "title")).toBe("Connected as Laptop");
    await quit(first.shell);
    // A login shell that takes 3 s: the device starts only once it has answered.
    const slow = join(home, "slow-shell");
    writeFileSync(slow, "#!/bin/sh\nsleep 3\nexec /bin/bash \"$@\"\n", { mode: 0o755 });
    app = await launch(home, { SHELL: slow });
    await stubNative(app);
    const { before } = await register(await webClient(app));
    expect(before.device).toEqual({ deviceId: "d", name: "Laptop" });
    expect(agent.registered).toHaveLength(1);
    const page = await shellPage(app);
    await expect.poll(() => page.getAttribute("#device", "title"), { timeout: 15_000 }).toBe("Connected as Laptop");
  });

  it("refuses a token for another user, or before anyone is signed in, and keeps nothing", async () => {
    const { page, client } = await connected();
    await expect(register(client, { ...ACCOUNT, userId: "someone-else" })).rejects.toThrow("This token is for another user");
    await expect(client.evaluate(async (token) => {
      await window.surogateDesktop!.setAccount(null);
      return window.surogateDesktop!.registerDevice(token);
    }, `surg_dev_${"t".repeat(44)}`)).rejects.toThrow("Sign in to the agent before registering this computer");
    expect(existsSync(join(home, "surogate", "credentials.json"))).toBe(false);
    expect(await page.getAttribute("#device", "title")).toBe("Sign in to this agent to let it work on folders of this computer");
  });

  it("prepares a folder as often as asked, and leaves no listener behind", async () => {
    const { shell, page, client } = await connected();
    await register(client);
    await expect.poll(() => page.getAttribute("#device", "title")).toBe("Connected as Laptop");
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
    const { page, client } = await connected();
    await register(client);
    await expect.poll(() => page.getAttribute("#device", "title")).toBe("Connected as Laptop");
    await client.evaluate((other) => window.surogateDesktop!.setAccount(other), { ...ACCOUNT, userId: "b", email: "b@example.com" });
    expect(await client.evaluate(() => window.surogateDesktop!.getDevice())).toMatchObject({ device: null, localFolders: false });
    await expect(client.evaluate(() => window.surogateDesktop!.prepareFolder("pick"))).rejects.toThrow("another account");
    await expect(client.evaluate(() => window.surogateDesktop!.bindSession("0b6f3c1e-8a2d-4c5e-9f10-1a2b3c4d5e6f", "a".repeat(43))))
      .rejects.toThrow("another account");
    // Its own account again: the computer is theirs.
    await client.evaluate((account) => window.surogateDesktop!.setAccount(account), ACCOUNT);
    expect(await client.evaluate(() => window.surogateDesktop!.getDevice())).toMatchObject({
      device: { deviceId: "d", name: "Laptop" }, localFolders: true,
    });
  });

  it("refuses a token for another agent, and keeps nothing", async () => {
    agent.config = { ...agent.config, agent_id: "another" };
    const { page, client } = await connected();
    await expect(register(client)).rejects.toThrow("This token is for another agent");
    expect(await page.getAttribute("#device", "title")).toBe("Sign in to this agent to let it work on folders of this computer");
    expect(existsSync(join(home, "surogate", "credentials.json"))).toBe(false);
  });
});
