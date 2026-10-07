// The system's notifications, through the real app: what the app tells while its window is away,
// and what a click opens. Each is recorded in the main process instead of shown.

import { mkdtempSync, realpathSync, rmSync } from "node:fs";

import type { ElectronApplication, Page } from "playwright-core";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { connect, FakeAgent, signedInAndAdded, webClient } from "./fake-agent.js";
import { clickNotice, dataHome, launch, notices, press, prompt, promptsShown, quit, shellPage, stubNative, stubNotifications } from "./launch.js";

let home: string;
let folder: string;
let agent: FakeAgent;
let origin: string;
let app: ElectronApplication | undefined;

beforeEach(async () => {
  home = dataHome();
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

// The app signed in and registered, its notifications recorded: the agent's web client.
async function signedIn(): Promise<Page> {
  app = await launch(home);
  await stubNative(app);
  await stubNotifications(app);
  await app.evaluate((_electron, chosen) => Object.assign(globalThis, { folder: chosen }), folder);
  const page = await shellPage(app);
  await connect(page, origin);
  await signedInAndAdded(app, page, agent);
  return webClient(app, origin);
}

const hide = (shell: ElectronApplication) => shell.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0]!.hide());
const shown = (shell: ElectronApplication) => shell.evaluate(({ BrowserWindow }) =>
  BrowserWindow.getAllWindows().find((window) => window.webContents.getURL().endsWith("/shell.html"))!.isVisible());

describe("the app's notifications", () => {
  it("tell of a prompt waiting over the hidden window, and a click shows it", async () => {
    const client = await signedIn();
    await hide(app!);
    const prepared = client.evaluate(() => window.surogateDesktop!.prepareFolder("pick"));
    await expect.poll(() => notices(app!)).toEqual([{ title: "Surogate is asking you something", body: "Open Surogate to answer." }]);
    expect(await promptsShown(app!)).toBe(0);
    await clickNotice(app!, 0);
    await expect.poll(() => shown(app!)).toBe(true);
    await press(await prompt(app!), "accept");
    expect(await prepared).toMatchObject({ folder });
  });
});
