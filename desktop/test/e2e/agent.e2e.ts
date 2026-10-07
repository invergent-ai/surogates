import { existsSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import type { ElectronApplication, Page } from "playwright-core";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { connect, FakeAgent, signIn, signedInAndAdded, webClient } from "./fake-agent.js";
import { dataHome, launch, quit, shellPage, stubNative } from "./launch.js";

let home: string;
let agent: FakeAgent;
let origin: string;
let host: string;
let app: ElectronApplication | undefined;
const others: FakeAgent[] = [];

beforeEach(async () => {
  home = dataHome();
  agent = new FakeAgent();
  origin = await agent.start();
  host = origin.replace("http://", "");
});

afterEach(async () => {
  await quit(app);
  app = undefined;
  for (const server of [agent, ...others.splice(0)]) {
    await server.stop();
    await server.link.stop();
  }
  rmSync(home, { recursive: true, force: true });
});

async function launched(env: Record<string, string> = {}): Promise<{ shell: ElectronApplication; page: Page }> {
  const shell = await launch(home, env);
  app = shell;
  await stubNative(shell);
  return { shell, page: await shellPage(shell) };
}

const asked = (shell: ElectronApplication) =>
  shell.evaluate(() => (globalThis as unknown as { asked: Array<{ message: string; detail: string }> }).asked);

describe("the agent's address", () => {
  it("is followed through its redirects, and the confirmation names where it ended", async () => {
    const moved = new FakeAgent();
    others.push(moved);
    const there = await moved.start();
    agent.configRedirect = `${there}/api/v1/auth/config`;
    const { shell, page } = await launched();
    await connect(page, origin);
    await expect.poll(() => page.isVisible("#sign-in")).toBe(true);
    const [question] = await asked(shell);
    expect(question?.message).toBe(`Connect to ${there.replace("http://", "")}?`);
    expect(question?.detail).toMatch(new RegExp(`^${origin} sent Surogate to ${there}\\.`));
    expect(JSON.parse(readFileSync(join(home, "surogate", "agent.json"), "utf8")).origin).toBe(there);
  });

  it("is refused when a redirect on the way is plain http to another computer", async () => {
    agent.configRedirect = "http://agents.example.com/api/v1/auth/config";
    const { shell, page } = await launched();
    await connect(page, origin);
    await expect.poll(() => page.textContent("#error")).toBe("Use an https:// address: http:// is only for this computer's own servers");
    expect(await asked(shell)).toEqual([]);
  });
});

describe("the agent's web client", () => {
  it("says why it is not shown when the agent sends it elsewhere, as a sign-on gateway does", async () => {
    const { shell, page } = await launched();
    await connect(page, origin);
    await signedInAndAdded(shell, page, agent);
    const client = await webClient(shell, origin);
    agent.pagesRedirect = "https://sso.example.com/login";
    await client.reload().catch(() => {});
    await expect.poll(() => page.isVisible("#unreachable")).toBe(true);
    expect(await page.textContent("#why")).toBe(`${host} sent Surogate to sso.example.com, which it does not open in its window`);
  });
});

describe("the agent's capabilities", () => {
  it("are read again at launch: a server that gained local folders gets this computer added", async () => {
    agent.config = { ...agent.config, desktop_sessions: false };
    const first = await launched();
    await connect(first.page, origin);
    await signIn(first.shell, first.page, agent);
    await expect.poll(() => first.page.getAttribute("#device", "title")).toBe("This server doesn't support local folders yet");
    expect(agent.registered).toEqual([]);
    await quit(first.shell);
    agent.config = { ...agent.config, desktop_sessions: true };
    const again = await launched();
    await expect.poll(() => again.page.getAttribute("#device", "title")).toBe("Connected as Laptop");
    expect(JSON.parse(readFileSync(join(home, "surogate", "agent.json"), "utf8")).desktopSessions).toBe(true);
  });

  it("are read again at a sign-in, and the web client is told it has local folders", async () => {
    agent.config = { ...agent.config, desktop_sessions: false };
    const { shell, page } = await launched();
    await connect(page, origin);
    await expect.poll(() => page.isVisible("#sign-in")).toBe(true);
    agent.config = { ...agent.config, desktop_sessions: true };
    await signedInAndAdded(shell, page, agent);
    const client = await webClient(shell, origin);
    expect(await client.evaluate(() => window.surogateDesktop!.getDevice())).toEqual({ device: { deviceId: "d", name: "Laptop" }, localFolders: true });
  });

  it("read while the agent is removed keep nothing of it", async () => {
    agent.config = { ...agent.config, desktop_sessions: false };
    const first = await launched();
    await connect(first.page, origin);
    await signIn(first.shell, first.page, agent);
    await quit(first.shell);
    agent.config = { ...agent.config, desktop_sessions: true };
    const release = agent.hold("config");
    const asking = agent.asked.config;
    const again = await launched();
    await expect.poll(() => agent.asked.config).toBe(asking + 1);
    await again.page.evaluate(() => (window as unknown as { surogateShell: { remove(): Promise<void> } }).surogateShell.remove());
    await expect.poll(() => again.page.isVisible("#first-run")).toBe(true);
    release();
    // The capabilities' answer lands, and changes nothing.
    await new Promise((resolve) => setTimeout(resolve, 500));
    expect(existsSync(join(home, "surogate", "agent.json"))).toBe(false);
    expect(await again.page.isVisible("#first-run")).toBe(true);
  });
});

describe("a folder asked for while the device starts", () => {
  it("waits for the device, rather than saying this computer is not registered", async () => {
    const first = await launched();
    await connect(first.page, origin);
    await signedInAndAdded(first.shell, first.page, agent);
    await quit(first.shell);
    // A login shell that takes 3 s: the device starts only once it has answered.
    const slow = join(home, "slow-shell");
    writeFileSync(slow, "#!/bin/sh\nsleep 3\nexec /bin/bash \"$@\"\n", { mode: 0o755 });
    const { shell } = await launched({ SHELL: slow });
    const client = await webClient(shell, origin);
    // The folder dialog is cancelled: the answer is no folder, not a refusal.
    expect(await client.evaluate(() => window.surogateDesktop!.prepareFolder("pick"))).toBeNull();
  });
});
