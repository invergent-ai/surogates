// surogate:// links, through the real app: one it starts with, and one a second launch hands it,
// as the system's link handler starts the app. A link opens the agent it names, at a page of
// its web client; one for an agent new to the app asks to connect first.

import { rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import type { ElectronApplication, Page } from "playwright-core";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { connect, FakeAgent, signedInAndAdded, webClient } from "./fake-agent.js";
import { dataHome, launch, quit, secondLaunch, shellPage, stubNative } from "./launch.js";

const CHAT = "7d2e0f8a-2b3c-4d5e-9f60-718293a4b5c6";

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

const link = (url: string) => `surogate://open?url=${encodeURIComponent(url)}`;

const asked = (shell: ElectronApplication) =>
  shell.evaluate(() => (globalThis as unknown as { asked: Array<{ message: string; detail: string }> }).asked);
const visible = (shell: ElectronApplication) => shell.evaluate(({ BrowserWindow }) =>
  BrowserWindow.getAllWindows().find((window) => window.webContents.getURL().endsWith("/shell.html"))!.isVisible());

async function signedIn(): Promise<{ shell: ElectronApplication; page: Page; client: Page }> {
  const shell = await launch(home);
  app = shell;
  await stubNative(shell);
  const page = await shellPage(shell);
  await connect(page, origin);
  await signedInAndAdded(shell, page, agent);
  return { shell, page, client: await webClient(shell, origin) };
}

describe("a surogate:// link", () => {
  it("handed by a second launch, shows the window at the chat it names", async () => {
    const { shell, client } = await signedIn();
    await shell.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0]!.hide());
    expect(await secondLaunch(home, link(`${origin}/chat/${CHAT}`))).toBe(0);
    await expect.poll(() => visible(shell)).toBe(true);
    await expect.poll(() => new URL(client.url()).pathname).toBe(`/chat/${CHAT}`);
  });

  it("that the app starts with, opens the chat it names", async () => {
    const first = await signedIn();
    await first.shell.close();
    app = await launch(home, {}, [link(`${origin}/chat/${CHAT}`)]);
    const client = await webClient(app, origin);
    await expect.poll(() => new URL(client.url()).pathname).toBe(`/chat/${CHAT}`);
  });

  it("asks to connect to the agent it names at the first run, and connects once confirmed", async () => {
    app = await launch(home);
    await stubNative(app);
    const page = await shellPage(app);
    expect(await secondLaunch(home, link(origin))).toBe(0);
    await expect.poll(async () => (await asked(app!)).map((options) => options.message)).toEqual([`Connect to ${new URL(origin).host}?`]);
    await expect.poll(() => page.isVisible("#sign-in")).toBe(true);
  });

  it("for another agent says so and opens nothing, and one that is no link opens nothing either", async () => {
    const { shell, client } = await signedIn();
    const before = client.url();
    expect(await secondLaunch(home, link("https://elsewhere.example.com/inbox"))).toBe(0);
    await expect.poll(async () => (await asked(shell)).at(-1)).toMatchObject({
      message: `Surogate works for ${new URL(origin).host}`,
      detail: `This link is for elsewhere.example.com. Remove ${new URL(origin).host} from Surogate to connect to another agent.`,
    });
    const count = (await asked(shell)).length;
    expect(await secondLaunch(home, "surogate://grant?folder=%2Fhome")).toBe(0);
    await new Promise((resolve) => setTimeout(resolve, 500));
    expect([(await asked(shell)).length, client.url()]).toEqual([count, before]);
  });

  it("waits while one of the app's own questions is up, and opens once it is answered", async () => {
    const { shell, client } = await signedIn();
    await shell.evaluate(() => Object.assign(globalThis, { hold: true }));
    // The web client's Log out asks first.
    const asking = (await asked(shell)).length + 1;
    void client.evaluate(() => window.surogateDesktop!.signOut()).catch(() => {});
    await expect.poll(async () => (await asked(shell)).length).toBe(asking);
    const before = client.url();
    expect(await secondLaunch(home, link(`${origin}/chat/${CHAT}`))).toBe(0);
    await new Promise((resolve) => setTimeout(resolve, 500));
    expect([(await asked(shell)).length, client.url()]).toEqual([asking, before]);
    // Cancelled: the link opens its chat, with no question of its own.
    await shell.evaluate(() => {
      const chosen = globalThis as unknown as { answer: number; release(): void };
      chosen.answer = 1;
      chosen.release();
    });
    await expect.poll(() => new URL(client.url()).pathname).toBe(`/chat/${CHAT}`);
    expect((await asked(shell)).length).toBe(asking);
  });

  it("that a second launch hands the app before its window is there, is kept until it is", async () => {
    // The app's start held, as a busy computer holds it, until the test lets it go on.
    const holding = join(home, "hold-ready.cjs");
    writeFileSync(holding, [
      'const { app } = require("electron");',
      "const ready = app.whenReady.bind(app);",
      "const held = Promise.withResolvers();",
      "Object.assign(globalThis, { releaseReady: held.resolve });",
      "app.whenReady = () => held.promise.then(ready);",
    ].join("\n"));
    app = await launch(home, {}, [], [holding]);
    await stubNative(app);
    expect(await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().length)).toBe(0);
    const handed = app.evaluate(({ app: electron }) => new Promise<void>((resolve) => electron.once("second-instance", () => resolve())));
    expect(await secondLaunch(home, link(origin))).toBe(0);
    await handed;
    await app.evaluate(() => (globalThis as unknown as { releaseReady(): void }).releaseReady());
    await expect.poll(async () => (await asked(app!)).map((options) => options.message)).toEqual([`Connect to ${new URL(origin).host}?`]);
  });

  it("asks about one link at a time: one that comes while a link, or the first run's Connect, is asked only shows the window", async () => {
    app = await launch(home);
    await stubNative(app);
    const page = await shellPage(app);
    await app.evaluate(() => Object.assign(globalThis, { hold: true }));
    // The user's own Connect is asked: a link for the same agent asks nothing more.
    await connect(page, origin);
    await expect.poll(async () => (await asked(app!)).length).toBe(1);
    expect(await secondLaunch(home, link(origin))).toBe(0);
    await new Promise((resolve) => setTimeout(resolve, 500));
    expect((await asked(app!)).length).toBe(1);
    await app.evaluate(() => (globalThis as unknown as { release(): void }).release());
    await expect.poll(() => page.isVisible("#sign-in")).toBe(true);
    // Three links for other agents at once: the first is asked about, and the others only show the window.
    for (const n of [1, 2, 3]) expect(await secondLaunch(home, link(`https://elsewhere-${n}.example.com`))).toBe(0);
    await expect.poll(async () => (await asked(app!)).length).toBe(2);
    await new Promise((resolve) => setTimeout(resolve, 500));
    expect((await asked(app!)).map((options) => options.detail).at(-1))
      .toBe(`This link is for elsewhere-1.example.com. Remove ${new URL(origin).host} from Surogate to connect to another agent.`);
    expect((await asked(app!)).length).toBe(2);
  });
});
