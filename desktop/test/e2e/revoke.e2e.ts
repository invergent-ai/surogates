import { mkdtempSync, readFileSync, realpathSync, rmSync } from "node:fs";
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
});
