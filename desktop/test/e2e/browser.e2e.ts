// The agent's browser through the real app: the shell's device, its binder and approvals, the
// desktop's own prompt, the browser host in its utility process, and the user's own browser.
// Behind SUROGATE_BROWSER_TESTS=1, apart from the user's session as the browser unit tests are:
//   npm run build && sh test/isolated.sh npx vitest run -c vitest.e2e.config.ts test/e2e/browser.e2e.ts
// Skipped where no supported browser is installed. Settings → Browser launches none, and runs anywhere.

import { mkdirSync, readdirSync, readFileSync, rmSync } from "node:fs";
import { createServer, type Server } from "node:http";
import { join } from "node:path";

import type { ElectronApplication, Page } from "playwright-core";
import { afterEach, beforeAll, beforeEach, describe, expect, it } from "vitest";

import { chosenBrowser, findBrowsers } from "../../src/browser/choose.js";
import { connect, FakeAgent, signedInAndAdded, webClient } from "./fake-agent.js";
import { isolated } from "../isolated.js";
import { dataHome, launch, press, prompt, promptsShown, quit, shellEnv, shellPage, stubNative } from "./launch.js";

const CHAT = "4e5f6a7b-8c9d-4e0f-a1b2-c3d4e5f6a7b8";
const BROWSER = chosenBrowser({ choice: "auto" }, findBrowsers());
const run = BROWSER !== null && process.env.SUROGATE_BROWSER_TESTS === "1";

let home: string;
let agent: FakeAgent;
let app: ElectronApplication | undefined;
let canary: Server;
let canaryPort: number;
let hits: string[];
let next = 0;

beforeEach(async () => {
  home = dataHome();
  agent = new FakeAgent();
  hits = [];
  // This computer's own service, which the agent's browser must never reach.
  canary = createServer((req, res) => {
    hits.push(req.url ?? "");
    res.end("canary");
  });
  await new Promise<void>((done) => canary.listen(0, "127.0.0.1", () => done()));
  canaryPort = (canary.address() as { port: number }).port;
});

afterEach(async () => {
  await quit(app);
  app = undefined;
  await agent.stop();
  await agent.link.stop();
  await new Promise<void>((done) => canary.close(() => done()));
  rmSync(home, { recursive: true, force: true });
});

// The chat's operation, as the server sends it, and the app's result for it.
async function operation(kind: string, args: Record<string, unknown>, invocation = `call-${next + 1}`, ordinal = 1): Promise<any> {
  const id = `op-${(next += 1)}`;
  agent.link.send({
    type: "op", id, session_id: CHAT, calling_session_id: CHAT, invocation_id: invocation, ordinal, kind, args, digest: `d-${id}`,
  });
  let result: Record<string, unknown> | undefined;
  await expect.poll(() => {
    result = agent.link.received.find((frame) => frame.type === "op_result" && frame.id === id);
    return result !== undefined;
  }, { timeout: 60_000 }).toBe(true);
  agent.link.send({ type: "op_ack", id });
  return result?.outcome;
}

// The app launched and signed in, and *folder* bound to the chat in the desktop's own sheet.
async function bound(folder: string): Promise<Page> {
  const origin = await agent.start();
  // The app's own environment is the browser's: apart from the user's session, or no launch.
  isolated(shellEnv(home));
  app = await launch(home);
  await stubNative(app);
  const page = await shellPage(app);
  await connect(page, origin);
  await signedInAndAdded(app, page, agent);
  const client = await webClient(app, origin);
  await app.evaluate((_electron, picked) => Object.assign(globalThis, { folder: picked }), folder);
  const preparing = client.evaluate(() => window.surogateDesktop!.prepareFolder("pick")) as Promise<{ folder: string; nonce: string }>;
  await press(await prompt(app), "accept");
  const prepared = await preparing;
  expect(await operation("bind", { folder, nonce: prepared.nonce }, "bind", 0)).toEqual({ ok: null });
  return page;
}

// The browser's processes with a profile under the app's state.
const profiles = () => join(home, "surogate", "browser-profiles");
const browsers = () => readdirSync("/proc").filter((pid) => /^\d+$/.test(pid)).filter((pid) => {
  try {
    return readFileSync(`/proc/${pid}/cmdline`, "utf8").includes(profiles());
  } catch {
    return false;
  }
});

describe.skipIf(!run)("the agent's browser through the app", () => {
  beforeAll(() => isolated());

  it("asks the chat's first use in the desktop's window, drives the user's own browser in a profile of its own, and never reaches this computer's services", async () => {
    const folder = join(home, "project");
    mkdirSync(folder);
    await bound(folder);
    const navigating = operation("browser.navigate", { url: `http://127.0.0.1:${canaryPort}/`, wait_until: "load" });
    const asked = await prompt(app!);
    expect(await asked.textContent("#prompt-title")).toMatch(/^Let .+ use a browser on this computer\?$/);
    expect(await asked.getAttribute('[data-id="allow_session"]', "aria-disabled")).toBe("true");
    await press(asked, "allow_session");
    // This computer's own address: the proxy refuses it, and says so, though the browser is up with the chat's tab.
    expect(await navigating).toEqual({
      error: { type: "browser", message: `The agent's browser does not reach this computer's own services (127.0.0.1:${canaryPort})` },
    });
    expect(hits).toEqual([]);
    expect(readdirSync(profiles())).toHaveLength(1);
    expect(browsers().length).toBeGreaterThan(0);
    // What the browser and Playwright keep while it runs goes under the identity's profiles, not the app's temp folder.
    const [identity] = readdirSync(profiles());
    expect(readdirSync(join(profiles(), identity!, "tmp")).some((name) => name.startsWith("playwright-artifacts-"))).toBe(true);
    expect(readdirSync(shellEnv(home).TMPDIR!).filter((name) => /playwright|chromium|chrome|edge/i.test(name))).toEqual([]);
    // Allowed for the chat: nothing asks again. A read and a screenshot come back whole.
    expect((await operation("browser.observe", { script: "snapshot@1", params: { selector: null } })).ok.frames).toEqual(expect.any(Array));
    const shot = await operation("browser.screenshot", { clip: null, labels: [] });
    expect(Buffer.from(shot.ok as string, "base64").subarray(1, 4).toString("latin1")).toBe("PNG");
    expect(await promptsShown(app!)).toBe(0);
    // An app that is killed takes the browser with it.
    app!.process().kill("SIGKILL");
    app = undefined;
    await expect.poll(() => browsers().length, { timeout: 15_000 }).toBe(0);
  });

  it("answers the chat's browser operations as denied once its user denies the first use, and asks again at the next", async () => {
    const folder = join(home, "project");
    mkdirSync(folder);
    await bound(folder);
    const navigating = operation("browser.navigate", { url: "https://example.com/", wait_until: "load" });
    await press(await prompt(app!), "deny");
    expect(await navigating).toEqual({
      error: { type: "denied", message: "The user did not let the agent use the browser on this computer in this chat" },
    });
    expect(browsers()).toEqual([]);
    const again = operation("browser.observe", { script: "snapshot@1", params: { selector: null } });
    await press(await prompt(app!), "deny");
    expect((await again).error.type).toBe("denied");
  });

  it("forgets the browser's sign-ins at a log out its user asked to, and closes the browser first", async () => {
    const folder = join(home, "project");
    mkdirSync(folder);
    const page = await bound(folder);
    const navigating = operation("browser.navigate", { url: `http://127.0.0.1:${canaryPort}/`, wait_until: "load" });
    await press(await prompt(app!), "allow_session");
    await navigating;
    expect(readdirSync(profiles())).toHaveLength(1);
    await app!.evaluate(() => Object.assign(globalThis, { checked: true }));
    await page.click("#user");
    await page.click('[data-action="logout"]');
    // The identity's profiles go; another identity's would stay beside them.
    await expect.poll(() => readdirSync(profiles()), { timeout: 15_000 }).toEqual([]);
    expect(browsers()).toEqual([]);
  });
});

describe("Settings → Browser", () => {
  it("lists the browsers found here, with Automatic and Custom…, and keeps the one chosen for the next launch", async () => {
    const origin = await agent.start();
    app = await launch(home);
    await stubNative(app);
    const page = await shellPage(app);
    await connect(page, origin);
    await signedInAndAdded(app, page, agent);
    await page.click("#user");
    await page.click('[data-action="settings"]');
    let settings: Page | undefined;
    await expect.poll(() => {
      settings = app!.windows().find((window) => window.url().endsWith("/settings.html"));
      return settings !== undefined;
    }).toBe(true);
    await settings!.waitForSelector('.settings-nav [data-section="browser"]');
    await settings!.click('.settings-nav [data-section="browser"]');
    await expect.poll(() => settings!.$$eval("#browser option", (options) => options.length)).toBeGreaterThan(1);
    const values = await settings!.$$eval("#browser option", (options) => options.map((option) => (option as HTMLOptionElement).value));
    expect(values[0]).toBe("auto");
    expect(values.at(-1)).toBe("pick");
    expect(await settings!.inputValue("#browser")).toBe("auto");
    const found = findBrowsers().filter((browser) => browser.unsupported === null);
    if (found[0]) {
      await settings!.selectOption("#browser", found[0].id);
      await expect.poll(() => readFileSync(join(home, "surogate", "browser.json"), "utf8")).toContain(`"choice": "${found[0].id}"`);
    }
  });
});
