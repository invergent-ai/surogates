import { existsSync, mkdtempSync, readFileSync, realpathSync, rmSync } from "node:fs";
import { join } from "node:path";

import type { ElectronApplication, Page } from "playwright-core";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { connect, FakeAgent, opened, ROTATED, signedInAndAdded, signIn, TOKEN, webClient } from "./fake-agent.js";
import { dataHome, launch, quit, shellPage, stubNative } from "./launch.js";

const THREAD = "6c1e9f7d-1a2b-4c3d-8e4f-5a6b7c8d9e0f";

let home: string;
let folder: string;
let agent: FakeAgent;
let origin: string;
let app: ElectronApplication | undefined;

beforeEach(async () => {
  home = dataHome();
  // Outside the app's state root, which no chat's folder may be.
  folder = realpathSync(mkdtempSync("/tmp/sf-"));
  agent = new FakeAgent();
  origin = await agent.start();
});

afterEach(async () => {
  await quit(app);
  app = undefined;
  await agent.stop();
  await agent.link.stop();
  rmSync(home, { recursive: true, force: true });
  rmSync(folder, { recursive: true, force: true });
});

const results = (id: string) => agent.link.received.filter((frame) => frame.type === "op_result" && frame.id === id);
const credentials = () => JSON.parse(readFileSync(join(home, "surogate", "credentials.json"), "utf8")) as Array<Record<string, unknown>>;
const asked = (shell: ElectronApplication) =>
  shell.evaluate(() => (globalThis as unknown as { asked: Array<{ message: string; detail: string }> }).asked);

// Signed in, this computer added, and a thread bound to *folder*, as the web client binds it.
async function bound(): Promise<{ shell: ElectronApplication; page: Page; client: Page }> {
  const shell = await launch(home);
  app = shell;
  await stubNative(shell);
  await shell.evaluate((_electron, picked) => Object.assign(globalThis, { folder: picked }), folder);
  const page = await shellPage(shell);
  await connect(page, origin);
  await signedInAndAdded(shell, page, agent);
  const client = await webClient(shell, origin);
  const prepared = await client.evaluate(() => window.surogateDesktop!.prepareFolder("pick"));
  agent.link.send({
    type: "op", id: "bind-1", session_id: THREAD, calling_session_id: THREAD, invocation_id: "bind", ordinal: 0,
    kind: "bind", args: { folder: prepared!.folder, nonce: prepared!.nonce }, digest: "digest-bind-1",
  });
  const binding = client.evaluate((token) => window.surogateDesktop!.bindSession("6c1e9f7d-1a2b-4c3d-8e4f-5a6b7c8d9e0f", token), prepared!.token);
  await agent.link.until(() => results("bind-1").length === 1, 10_000);
  agent.link.send({ type: "op_ack", id: "bind-1" });
  await binding;
  return { shell, page, client };
}

describe("a computer the agent ends", () => {
  it.each([[4403, "revoked"], [4401, "no longer knows its token"]])(
    "on close %i (%s) forgets its token and what it held, keeps its folders, and offers to restore it",
    async (code) => {
      const { page, client } = await bound();
      agent.link.close(code);
      await expect.poll(() => page.textContent("#device-action-text")).toBe("Local access revoked.");
      expect(await page.textContent("#device-action-button")).toBe("Restore…");
      expect(credentials()).toEqual([expect.not.objectContaining({ plain: expect.anything() })]);
      expect(credentials()[0]).toMatchObject({ deviceId: "d" });
      expect(await client.evaluate(() => window.surogateDesktop!.getDevice())).toEqual({ device: { deviceId: "d", name: "Laptop" }, localFolders: false });
      await expect(client.evaluate(() => window.surogateDesktop!.prepareFolder("pick"))).rejects.toThrow("was revoked");
    },
  );
});

describe("a log out right after the agent ended this computer", () => {
  it("forgets it and its folders, with nothing left to revoke", async () => {
    const { page } = await bound();
    agent.link.close(4403);
    await expect.poll(() => page.textContent("#device-action-button")).toBe("Restore…");
    await page.click("#user");
    await page.click('[data-action="logout"]');
    await expect.poll(() => page.isVisible("#sign-in")).toBe(true);
    await expect.poll(() => credentials()).toEqual([]);
    await expect.poll(() => existsSync(join(home, "surogate", "devices", "d"))).toBe(false);
    expect(agent.link.received.filter((frame) => frame.type === "revoke")).toEqual([]);
  });
});

describe("restoring a revoked computer", () => {
  it("asks the same user to confirm its folders, then the agent rotates the token on the same device", async () => {
    const { shell, page, client } = await bound();
    agent.link.close(4403);
    await expect.poll(() => page.textContent("#device-action-button")).toBe("Restore…");
    await page.click("#device-action-button");
    await expect.poll(() => page.getAttribute("#device", "title")).toBe("Connected as Laptop");
    const restore = (await asked(shell)).find((options) => options.message === "Restore local access on Laptop?");
    expect(restore?.detail).toContain(`• ${folder}`);
    // Restored with the app's sign-in, which the agent binds to the computer.
    expect(agent.reauthorized).toEqual(["Bearer at-1"]);
    expect(credentials()).toEqual([expect.objectContaining({ deviceId: "d", plain: `surg_dev_${"r".repeat(44)}` })]);
    expect(await client.evaluate(() => window.surogateDesktop!.getDevice())).toMatchObject({ localFolders: true });
    expect(await page.isVisible("#device-action")).toBe(false);
  });

  it("changes nothing when the user cancels", async () => {
    const { shell, page } = await bound();
    agent.link.close(4403);
    await expect.poll(() => page.textContent("#device-action-button")).toBe("Restore…");
    await shell.evaluate(() => Object.assign(globalThis, { answer: 1 }));
    await page.click("#device-action-button");
    await expect.poll(async () => (await asked(shell)).some((options) => options.message === "Restore local access on Laptop?")).toBe(true);
    expect(agent.reauthorized).toEqual([]);
    expect(agent.link.token).toBe(TOKEN);
  });

  it("asks for a sign-in in the browser first when the last one is not recent", async () => {
    agent.signedInAgoS = 3600;
    const { shell, page } = await bound();
    agent.link.close(4403);
    await expect.poll(() => page.textContent("#device-action-button")).toBe("Restore…");
    await page.click("#device-action-button");
    await expect.poll(async () => (await opened(shell)).length).toBe(2);
    agent.signedInAgoS = 0;
    await agent.approve((await opened(shell))[1]!);
    await expect.poll(() => page.getAttribute("#device", "title")).toBe("Connected as Laptop");
    // Restored with the new sign-in, once: the revoked computer was not bound to it before the user confirmed.
    expect(agent.reauthorized).toEqual(["Bearer at-2"]);
    // The sign-in it replaced is ended at the agent, not left valid with no copy here.
    await expect.poll(() => agent.oauth.some((form) => form.token === "rt-1")).toBe(true);
  });

  it("keeps the restored token though the link cannot connect yet: the computer shows offline until it can", async () => {
    const { page, client } = await bound();
    agent.link.close(4403);
    await expect.poll(() => page.textContent("#device-action-button")).toBe("Restore…");
    agent.link.refusing = true;
    await page.click("#device-action-button");
    await expect.poll(() => page.getAttribute("#device", "title"), { timeout: 5_000 }).toBe("Offline: reconnecting");
    expect(agent.reauthorized).toEqual(["Bearer at-1"]);
    expect(credentials()).toEqual([expect.objectContaining({ deviceId: "d", plain: ROTATED })]);
    expect(await page.isVisible("#device-action")).toBe(false);
    expect(await client.evaluate(() => window.surogateDesktop!.getDevice())).toMatchObject({ localFolders: true });
    agent.link.refusing = false;
    await expect.poll(() => page.getAttribute("#device", "title"), { timeout: 15_000 }).toBe("Connected as Laptop");
  });

  it("is ended by a log out while the agent reauthorizes it: the token it was issued revokes it, and no device runs", async () => {
    const { shell, page } = await bound();
    agent.link.close(4403);
    await expect.poll(() => page.textContent("#device-action-button")).toBe("Restore…");
    const release = agent.hold("reauthorize");
    await page.click("#device-action-button");
    await expect.poll(() => agent.asked.reauthorize).toBe(1);
    await page.click("#user");
    await page.click('[data-action="logout"]');
    await expect.poll(async () => (await asked(shell)).some((options) => options.message.startsWith("Log out of"))).toBe(true);
    // The log out waits for the Restore under way, which it cancelled.
    await new Promise((resolve) => setTimeout(resolve, 300));
    expect(credentials()).toEqual([expect.objectContaining({ deviceId: "d" })]);
    release();
    await expect.poll(() => agent.link.received.filter((frame) => frame.type === "revoke").length).toBe(1);
    await expect.poll(() => credentials()).toEqual([]);
    await expect.poll(() => page.isVisible("#sign-in")).toBe(true);
    expect(await page.getAttribute("#device", "title")).toBe("Sign in to this agent to let it work on folders of this computer");
    // Nor at the next launch.
    await quit(shell);
    app = await launch(home);
    const again = await shellPage(app);
    await expect.poll(() => again.getAttribute("#device", "title")).toBe("Sign in to this agent to let it work on folders of this computer");
  });

  it("adds this computer afresh when the agent has no such device any more", async () => {
    const { page } = await bound();
    agent.link.close(4403);
    await expect.poll(() => page.textContent("#device-action-button")).toBe("Restore…");
    agent.gone = true;
    await page.click("#device-action-button");
    await expect.poll(() => agent.registered.length).toBe(2);
    await expect.poll(() => page.getAttribute("#device", "title")).toBe("Connected as Laptop");
    expect(credentials()).toEqual([expect.objectContaining({ deviceId: "d", plain: TOKEN })]);
  });
});

describe("a later sign-in, while this computer is revoked", () => {
  it.each([
    ["revoked it, as its list of computers says", 4403, true],
    ["no longer takes its token, though it still lists it", 4401, false],
  ])("leaves it for the user to restore when the agent %s", async (_name, code, listed) => {
    const { shell, page } = await bound();
    if (listed) agent.revokedAt = new Date().toISOString();
    agent.link.close(code);
    await expect.poll(() => page.textContent("#device-action-button")).toBe("Restore…");
    // That sign-in ended, and the user signs in again at the next launch.
    await quit(shell);
    rmSync(join(home, "surogate", "session.json"));
    app = await launch(home);
    await stubNative(app);
    const again = await shellPage(app);
    await expect.poll(() => again.isVisible("#sign-in")).toBe(true);
    await signIn(app, again, agent);
    await expect.poll(() => again.textContent("#device-action-button")).toBe("Restore…");
    // Not bound to the new sign-in: restoring it is the user's to confirm, with its folders.
    expect(agent.reauthorized).toEqual([]);
    expect(credentials()).toEqual([expect.not.objectContaining({ plain: expect.anything() })]);
    expect(agent.registered).toHaveLength(1);
  });

  it("retires it once the agent lists it revoked, though its link here has not been told", async () => {
    const { shell } = await bound();
    await quit(shell);
    rmSync(join(home, "surogate", "session.json"));
    agent.revokedAt = new Date().toISOString();
    app = await launch(home);
    await stubNative(app);
    const again = await shellPage(app);
    await expect.poll(() => again.getAttribute("#device", "title")).toBe("Connected as Laptop");
    await signIn(app, again, agent);
    await expect.poll(() => again.textContent("#device-action-button")).toBe("Restore…");
    expect(agent.reauthorized).toEqual([]);
    expect(credentials()).toEqual([expect.not.objectContaining({ plain: expect.anything() })]);
  });
});

describe("a later sign-in, which rotates this computer's token", () => {
  const OTHER = "7d2f0a8e-2b3c-4d4e-9f5a-6b7c8d9e0f1a";

  it("is no revocation: what the computer had not sent yet goes out on the new token", async () => {
    const { shell, client } = await bound();
    // A second chat bound to the folder, whose result the agent has not acknowledged.
    const prepared = await client.evaluate(() => window.surogateDesktop!.prepareFolder("pick"));
    agent.link.send({
      type: "op", id: "bind-2", session_id: OTHER, calling_session_id: OTHER, invocation_id: "bind", ordinal: 0,
      kind: "bind", args: { folder: prepared!.folder, nonce: prepared!.nonce }, digest: "digest-bind-2",
    });
    // The page's confirmation settles only once the agent acknowledges the bind, which it never does here.
    void client.evaluate((token) => window.surogateDesktop!.bindSession("7d2f0a8e-2b3c-4d4e-9f5a-6b7c8d9e0f1a", token), prepared!.token).catch(() => {});
    await agent.link.until(() => results("bind-2").length === 1, 10_000);
    // That sign-in ended, and the app is launched again with nobody signed in: its device sends the result again.
    await quit(shell);
    rmSync(join(home, "surogate", "session.json"));
    app = await launch(home);
    await stubNative(app);
    const page = await shellPage(app);
    await expect.poll(() => page.isVisible("#sign-in")).toBe(true);
    await agent.link.until(() => results("bind-2").length === 2, 10_000);
    // The agent closes the old token's link as revoked once it issued the new one.
    await signIn(app, page, agent);
    await expect.poll(() => agent.reauthorized).toEqual(["Bearer at-2"]);
    await agent.link.until(() => results("bind-2").length === 3, 10_000);
    await expect.poll(() => page.getAttribute("#device", "title")).toBe("Connected as Laptop");
    expect(credentials()).toEqual([expect.objectContaining({ deviceId: "d", plain: ROTATED })]);
    expect(await page.isVisible("#device-action")).toBe(false);
  });

  it("is no log out when the app quits while the agent rotates it: the computer, its token, its folders and the sign-in stay", async () => {
    const { shell } = await bound();
    await quit(shell);
    rmSync(join(home, "surogate", "session.json"));
    const quitting = await launch(home);
    await stubNative(quitting);
    const page = await shellPage(quitting);
    await expect.poll(() => page.getAttribute("#device", "title")).toBe("Connected as Laptop");
    const release = agent.hold("reauthorize");
    await page.click("#sign-in-button");
    await expect.poll(async () => (await opened(quitting)).length).toBe(1);
    await agent.approve((await opened(quitting))[0]!);
    await expect.poll(() => agent.asked.reauthorize).toBe(1);
    const links = agent.link.connections;
    // The user quits while the agent is still rotating the token, and it answers during the quit.
    const closing = quit(quitting);
    await new Promise((resolve) => setTimeout(resolve, 500));
    release();
    await closing;
    // Nothing started on the new token, and nothing was revoked.
    expect(agent.link.connections).toBe(links);
    expect(agent.link.received.filter((frame) => frame.type === "revoke")).toEqual([]);
    expect(agent.oauth.filter((form) => "token" in form)).toEqual([]);
    expect(credentials()).toEqual([expect.objectContaining({ deviceId: "d", plain: ROTATED })]);
    expect(credentials()[0]).not.toHaveProperty("revoking");
    expect(existsSync(join(home, "surogate", "devices", "d", "journal.sqlite"))).toBe(true);
    expect(existsSync(join(home, "surogate", "session.json"))).toBe(true);
    // The next launch is signed in, and the computer runs on the new token.
    app = await launch(home);
    const again = await shellPage(app);
    await expect.poll(() => again.getAttribute("#device", "title")).toBe("Connected as Laptop");
    expect(await again.isVisible("#sign-in")).toBe(false);
  });
});
