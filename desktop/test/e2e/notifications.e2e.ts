// The system's notifications, through the real app: what the app tells while its window is away,
// and what a click opens. Each is recorded in the main process instead of shown.

import { mkdtempSync, realpathSync, rmSync } from "node:fs";

import type { ElectronApplication, Page } from "playwright-core";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { FIXTURE_IDS, projectFixtures } from "../../../web/src/lib/projects.js";
import { ACCOUNT, connect, FakeAgent, signedInAndAdded, webClient } from "./fake-agent.js";
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

const CHAT = "7d2e0f8a-2b3c-4d5e-9f60-718293a4b5c6";
const OTHER = "8e3f1a9b-3c4d-4e6f-a071-8293a4b5c6d7";

// The web client moves to *path*, as its own links move it.
const moveTo = (client: Page, path: string) => client.evaluate((to) => history.pushState(null, "", to), path);

// The window in front, with the keyboard, as the user brings it there.
async function focus(shell: ElectronApplication): Promise<void> {
  await shell.evaluate(({ BrowserWindow }) => {
    const window = BrowserWindow.getAllWindows().find((found) => found.webContents.getURL().endsWith("/shell.html"))!;
    window.show();
    window.focus();
  });
  await expect.poll(() => shell.evaluate(({ BrowserWindow }) => BrowserWindow.getFocusedWindow() !== null)).toBe(true);
}

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

  it("tell of a question in the inbox while the window is hidden, and a click opens its chat", async () => {
    const client = await signedIn();
    await hide(app!);
    await expect.poll(() => agent.inboxStreams.size).toBe(1);
    agent.tell({ kind: "input_required", title: "Which report should I start from?", session_id: CHAT });
    await expect.poll(() => notices(app!)).toEqual([{ title: "Which report should I start from?", body: "Asks you a question." }]);
    await clickNotice(app!, 0);
    await expect.poll(() => shown(app!)).toBe(true);
    await expect.poll(() => new URL(client.url()).pathname).toBe(`/chat/${CHAT}`);
  });

  it("tell nothing the inbox held, nor anything while the window has the focus, and tell what came while the agent was away", async () => {
    agent.tell({ kind: "task_complete", title: "An older chat", session_id: CHAT });
    await signedIn();
    await focus(app!);
    await expect.poll(() => agent.inboxStreams.size).toBe(1);
    agent.tell({ kind: "task_complete", title: "Seen in the window", session_id: CHAT });
    await new Promise((resolve) => setTimeout(resolve, 500));
    expect(await notices(app!)).toEqual([]);
    await hide(app!);
    // Gone while the agent restarts: what came meanwhile is in the inbox it opens again.
    agent.dropInbox();
    agent.tell({ kind: "governance_gate", title: "Delete the old drafts?", session_id: CHAT });
    await expect.poll(() => notices(app!), { timeout: 10_000 }).toEqual([{ title: "Delete the old drafts?", body: "Waits for your approval." }]);
  });

  it("tell a flood of items as one notice, which opens the inbox", async () => {
    const client = await signedIn();
    await hide(app!);
    await expect.poll(() => agent.inboxStreams.size).toBe(1);
    for (let n = 1; n <= 5; n++) agent.tell({ kind: "input_required", title: `Question ${n}`, session_id: `0000000${n}-2b3c-4d5e-9f60-718293a4b5c6` });
    await expect.poll(async () => (await notices(app!)).at(-1)).toEqual({ title: "5 new items in your inbox", body: "Open your inbox to see them." });
    await clickNotice(app!, (await notices(app!)).length - 1);
    await expect.poll(() => shown(app!)).toBe(true);
    await expect.poll(() => new URL(client.url()).pathname).toBe("/inbox");
  });

  it("open a thread of the open project in the project, with the project as the way back and its Overview", async () => {
    agent.projects = projectFixtures();
    const client = await signedIn();
    const page = await shellPage(app!);
    await client.evaluate((account) => window.surogateDesktop!.setAccount(account), ACCOUNT);
    await page.click(`#projects [data-project="${FIXTURE_IDS.report}"] .project`);
    await page.waitForSelector(".section .thread", { state: "attached" });
    await hide(app!);
    await expect.poll(() => agent.inboxStreams.size).toBe(1);
    agent.tell({ kind: "input_required", title: "Which quarter's exchange rate should I use?", session_id: FIXTURE_IDS.question });
    await expect.poll(async () => (await notices(app!)).length).toBe(1);
    await clickNotice(app!, 0);
    await expect.poll(() => shown(app!)).toBe(true);
    await expect.poll(() => new URL(client.url()).pathname).toBe(`/chat/${FIXTURE_IDS.question}`);
    expect(await page.textContent("#title")).toBe("Check the revenue figures");
    expect(await page.textContent("#to-project")).toBe("Quarterly report");
    expect(await page.$$eval(".section .thread", (found) => found.length)).toBeGreaterThan(0);
  });

  it("tell of a turn that ends in the chat the window shows, followed across its turns only while the window is away", async () => {
    const client = await signedIn();
    agent.titles.set(CHAT, "Quarterly report");
    await moveTo(client, `/chat/${CHAT}`);
    await focus(app!);
    // In front, the window's own page tells the user: the app follows nothing.
    await new Promise((resolve) => setTimeout(resolve, 500));
    expect(agent.chatsAsked).toEqual([]);
    await hide(app!);
    // From the chat's newest event on, through the turns to come: none of its earlier turns is told.
    await expect.poll(() => agent.chatsAsked).toEqual([`${CHAT}?after=-1&watch=1`]);
    agent.turnEnds(CHAT);
    await expect.poll(() => notices(app!)).toEqual([{ title: "Quarterly report", body: "Finished." }]);
    // The agent restarts: the follow takes up after the last event it heard.
    agent.dropChats();
    await expect.poll(() => agent.chatsAsked.at(-1), { timeout: 10_000 }).toBe(`${CHAT}?after=101&watch=1`);
    agent.turnEnds(CHAT);
    await expect.poll(async () => (await notices(app!)).length).toBe(2);
    // Another chat in the centre: the first is followed no more.
    await moveTo(client, `/chat/${OTHER}`);
    await expect.poll(() => agent.chatStreams.get(CHAT)?.size).toBe(0);
    await expect.poll(() => agent.chatsAsked.at(-1)).toBe(`${OTHER}?after=-1&watch=1`);
    // The window in front again: nothing is followed.
    await focus(app!);
    await expect.poll(() => agent.chatStreams.get(OTHER)?.size).toBe(0);
    await clickNotice(app!, 1);
    await expect.poll(() => new URL(client.url()).pathname).toBe(`/chat/${CHAT}`);
  });
});
