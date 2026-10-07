// The system's notifications, through the real app: what the app tells while its window is away,
// and what a click opens. Each is recorded in the main process instead of shown.

import { mkdtempSync, realpathSync, rmSync } from "node:fs";

import type { ElectronApplication, Page } from "playwright-core";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { FIXTURE_IDS, projectFixtures } from "../../../web/src/lib/projects.js";
import { ACCOUNT, connect, FakeAgent, quitHeld, signedInAndAdded, webClient } from "./fake-agent.js";
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

// The held quit released, and the app gone.
async function quitted(shell: ElectronApplication, release: () => void): Promise<void> {
  const closed = shell.waitForEvent("close");
  release();
  await closed;
  app = undefined;
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
    await focus(app!);
    await moveTo(client, `/chat/${CHAT}`);
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

  it("tell nothing once a quit has hidden the window, while the app still stops", async () => {
    const client = await signedIn();
    const page = await shellPage(app!);
    await moveTo(client, `/chat/${CHAT}`);
    await hide(app!);
    await expect.poll(() => agent.inboxStreams.size).toBe(1);
    await expect.poll(() => agent.chatStreams.get(CHAT)?.size).toBe(1);
    const release = await quitHeld(app!, page, agent);
    // The inbox and the chat are followed no more: what comes now raises nothing.
    await expect.poll(() => agent.chatStreams.get(CHAT)?.size).toBe(0);
    agent.tell({ kind: "input_required", title: "Which report should I start from?", session_id: OTHER });
    agent.turnEnds(CHAT);
    await new Promise((resolve) => setTimeout(resolve, 500));
    expect(await notices(app!)).toEqual([]);
    await quitted(app!, release);
  });

  it("tell nothing of an item read in the moment the quit goes on", async () => {
    await signedIn();
    const page = await shellPage(app!);
    await hide(app!);
    await expect.poll(() => agent.inboxStreams.size).toBe(1);
    // The item is told on the stream, and its read is under way as the quit goes on.
    const read = agent.hold("item");
    agent.tell({ kind: "input_required", title: "Which report should I start from?", session_id: OTHER });
    await expect.poll(() => agent.asked.item).toBe(1);
    const release = await quitHeld(app!, page, agent);
    read();
    await new Promise((resolve) => setTimeout(resolve, 500));
    expect(await notices(app!)).toEqual([]);
    await quitted(app!, release);
  });

  it("tell nothing of a turn that ends in the moment the quit goes on", async () => {
    const client = await signedIn();
    const page = await shellPage(app!);
    await focus(app!);
    await moveTo(client, `/chat/${CHAT}`);
    await hide(app!);
    await expect.poll(() => agent.chatStreams.get(CHAT)?.size).toBe(1);
    // The turn ends, and the chat's title is being read as the quit goes on.
    const read = agent.hold("title");
    agent.turnEnds(CHAT);
    await expect.poll(() => agent.asked.title).toBe(1);
    const release = await quitHeld(app!, page, agent);
    read();
    await new Promise((resolve) => setTimeout(resolve, 500));
    expect(await notices(app!)).toEqual([]);
    await quitted(app!, release);
  });

  it("tell nothing of a prompt asked once the quit has gone on", async () => {
    const client = await signedIn();
    const page = await shellPage(app!);
    await hide(app!);
    const release = await quitHeld(app!, page, agent);
    // The web client still runs while the app stops, and asks for a folder over the hidden window.
    void client.evaluate(() => window.surogateDesktop!.prepareFolder("pick")).catch(() => {});
    await new Promise((resolve) => setTimeout(resolve, 500));
    expect(await notices(app!)).toEqual([]);
    await quitted(app!, release);
  });

  it("open nothing on a click once the quit has gone on, though they were told before", async () => {
    const client = await signedIn();
    const page = await shellPage(app!);
    await hide(app!);
    await expect.poll(() => agent.inboxStreams.size).toBe(1);
    agent.tell({ kind: "input_required", title: "Which report should I start from?", session_id: CHAT });
    await expect.poll(async () => (await notices(app!)).length).toBe(1);
    const before = client.url();
    const release = await quitHeld(app!, page, agent);
    await clickNotice(app!, 0);
    await new Promise((resolve) => setTimeout(resolve, 500));
    expect([await shown(app!), client.url()]).toEqual([false, before]);
    await quitted(app!, release);
  });

  it("tell a turn's end in the chat followed once, though the inbox has it too, and the chat's other items as the inbox has them", async () => {
    const client = await signedIn();
    agent.titles.set(CHAT, "Quarterly report");
    await focus(app!);
    await moveTo(client, `/chat/${CHAT}`);
    await hide(app!);
    await expect.poll(() => agent.chatStreams.get(CHAT)?.size).toBe(1);
    await expect.poll(() => agent.inboxStreams.size).toBe(1);
    // No page streams the chat, so the agent puts the turn's end in the inbox too.
    agent.tell({ kind: "task_complete", title: "Quarterly report", session_id: CHAT });
    agent.turnEnds(CHAT);
    await expect.poll(async () => (await notices(app!)).length).toBe(1);
    await new Promise((resolve) => setTimeout(resolve, 500));
    expect(await notices(app!)).toEqual([{ title: "Quarterly report", body: "Finished." }]);
    agent.tell({ kind: "progress_checkin", title: "Progress: 3 iterations, 2 min elapsed", session_id: CHAT });
    await expect.poll(async () => (await notices(app!)).at(-1)).toEqual({ title: "Progress: 3 iterations, 2 min elapsed", body: "Checked in." });
  });

  it("follow the chat the window shows no more once the user logs out", async () => {
    const client = await signedIn();
    await focus(app!);
    await moveTo(client, `/chat/${CHAT}`);
    await hide(app!);
    await expect.poll(() => agent.chatStreams.get(CHAT)?.size).toBe(1);
    // The web client's own Log out, while the window is away.
    void client.evaluate(() => window.surogateDesktop!.signOut()).catch(() => {});
    await expect.poll(() => agent.chatStreams.get(CHAT)?.size).toBe(0);
    const asked = agent.chatsAsked.length;
    await new Promise((resolve) => setTimeout(resolve, 500));
    expect(agent.chatsAsked).toHaveLength(asked);
  });
});
