// The sidebar's Recents: the agent's chats in no project, as its page serves them (the projects source's chats), newest
// first, under Projects, each opened in the centre by a click; more as the sidebar is scrolled to its end; none from a
// page of an agent older than the app, which serves no chats. The sidebar has no Chats item of its own.

import type { ElectronApplication, Page } from "playwright-core";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { FIXTURE_IDS, type ProjectFixtures, projectFixtures, type RecentChat } from "../../../web/src/lib/projects.js";
import { connect, FakeAgent, signedInAndAdded, webClient } from "./fake-agent.js";
import { dataHome, launch, quit, shellPage, stubNative } from "./launch.js";

const { tripNotes: TRIP, budgetQuestions: BUDGET_CHAT, untitled: UNTITLED, report: REPORT } = FIXTURE_IDS;

let home: string;
let agent: FakeAgent;
let origin: string;
let app: ElectronApplication | undefined;

beforeEach(async () => {
  home = dataHome();
  agent = new FakeAgent();
  agent.projects = projectFixtures();
  origin = await agent.start();
});

afterEach(async () => {
  await quit(app);
  app = undefined;
  await agent.stop();
  await agent.link.stop();
});

async function signedIn(): Promise<{ page: Page; client: Page }> {
  const shell = await launch(home);
  app = shell;
  await stubNative(shell);
  const page = await shellPage(shell);
  await connect(page, origin);
  await signedInAndAdded(shell, page, agent);
  const client = await webClient(shell, origin);
  return { page, client };
}

const titles = (page: Page) => page.$$eval("#recents .recent:not([hidden]) .name", (found) => found.map((element) => element.textContent));
const served = (client: Page) => client.evaluate(() => (window as unknown as { fakeProjects: { data: ProjectFixtures; pages: Array<[number, number]> } }).fakeProjects);

// *count* chats in no project, a minute apart, the newest first.
function many(count: number): RecentChat[] {
  return Array.from({ length: count }, (_, at) => ({
    id: `0000${String(at).padStart(4, "0")}-0000-4000-8000-000000000000`, title: `Chat ${at + 1}`,
    updatedAt: new Date(Date.now() - at * 60_000).toISOString(), computer: null,
  }));
}

describe("the sidebar's Recents", () => {
  it("lists the agent's chats in no project under Projects, newest first, and has no Chats item of its own", async () => {
    const { page } = await signedIn();
    await expect.poll(() => titles(page)).toEqual(["Notes on the trip", "Budget questions", "New chat"]);
    expect(await page.isVisible("#recents-head")).toBe(true);
    expect(await page.textContent("#recents-head")).toBe("Recents");
    // Under Projects, in the one list that scrolls.
    expect(await page.evaluate(() => {
      const [projects, recents] = ["#projects-head", "#recents-head"].map((selector) => document.querySelector(selector)!);
      return projects!.compareDocumentPosition(recents!) & Node.DOCUMENT_POSITION_FOLLOWING;
    })).toBeTruthy();
    // A chat on a folder of a computer has its mark, and says which computer.
    expect(await page.isVisible(`#recents [data-chat="${TRIP}"] .on-computer`)).toBe(true);
    expect(await page.getAttribute(`#recents [data-chat="${TRIP}"]`, "title")).toBe("Notes on the trip, on Laptop");
    expect(await page.isVisible(`#recents [data-chat="${BUDGET_CHAT}"] .on-computer`)).toBe(false);
    expect(await page.$$('[data-path="/chats"]')).toHaveLength(0);
  });

  it("opens a chat in the centre at a click, and marks the one the centre shows", async () => {
    const { page, client } = await signedIn();
    await page.waitForSelector(`#recents [data-chat="${BUDGET_CHAT}"]`);
    await page.click(`#recents [data-chat="${BUDGET_CHAT}"]`);
    await expect.poll(() => client.url()).toBe(`${origin}/chat/${BUDGET_CHAT}`);
    await expect.poll(() => page.getAttribute(`#recents [data-chat="${BUDGET_CHAT}"]`, "aria-current")).toBe("page");
    expect(await page.getAttribute(`#recents [data-chat="${TRIP}"]`, "aria-current")).toBeNull();
    // From a project too: the chat leaves the project for a page of its own.
    await page.click(`#projects [data-project="${REPORT}"] .project`);
    await expect.poll(() => page.textContent("#title")).toBe("Quarterly report");
    await page.click(`#recents [data-chat="${UNTITLED}"]`);
    await expect.poll(() => client.url()).toBe(`${origin}/chat/${UNTITLED}`);
    await expect.poll(() => page.textContent("#title")).toBe(new URL(origin).host);
  });

  it("lists a chat begun meanwhile once the centre shows it", async () => {
    const { page, client } = await signedIn();
    await expect.poll(() => titles(page)).toHaveLength(3);
    const begun = "cd4e5f60-7182-4d93-8ea4-f5a6b7c8d9ea";
    await client.evaluate((chat) => {
      (window as unknown as { fakeProjects: { data: ProjectFixtures } }).fakeProjects.data.chats.push(chat);
      history.pushState(null, "", `/chat/${chat.id}`);
    }, { id: begun, title: "A new question", updatedAt: new Date().toISOString(), computer: null });
    await expect.poll(() => titles(page)).toEqual(["A new question", "Notes on the trip", "Budget questions", "New chat"]);
    await expect.poll(() => page.getAttribute(`#recents [data-chat="${begun}"]`, "aria-current")).toBe("page");
  });

  it("asks for the next page of chats as the sidebar is scrolled to its end, each chat once", async () => {
    agent.projects!.chats = many(70);
    const { page, client } = await signedIn();
    await expect.poll(() => titles(page)).toHaveLength(30);
    const scrolled = () => page.evaluate(() => {
      const lists = document.querySelector("#lists")!;
      lists.scrollTop = lists.scrollHeight;
    });
    await scrolled();
    await expect.poll(() => titles(page)).toHaveLength(60);
    await scrolled();
    await expect.poll(() => titles(page)).toHaveLength(70);
    const listed = await titles(page);
    expect(new Set(listed).size).toBe(70);
    expect(listed[0]).toBe("Chat 1");
    expect(listed.at(-1)).toBe("Chat 70");
    // The last page said no more come: the end of the list asks for none.
    const asked = (await served(client)).pages.length;
    await scrolled();
    await new Promise((resolve) => setTimeout(resolve, 500));
    expect((await served(client)).pages.length).toBe(asked);
    expect((await served(client)).pages.slice(0, 3)).toEqual([[0, 30], [30, 30], [60, 30]]);
  });

  it("is filtered by the sidebar's search with the projects", async () => {
    const { page } = await signedIn();
    await expect.poll(() => titles(page)).toHaveLength(3);
    await page.fill("#search", "budget");
    await expect.poll(() => titles(page)).toEqual(["Budget questions"]);
    expect(await page.isVisible("#recents-head")).toBe(true);
    await page.fill("#search", "trip");
    await expect.poll(() => titles(page)).toEqual(["Notes on the trip"]);
    expect(await page.isVisible("#projects-head")).toBe(false);
    await page.fill("#search", "nothing like it");
    await expect.poll(() => page.isVisible("#recents-head")).toBe(false);
    await page.fill("#search", "");
    await expect.poll(() => titles(page)).toHaveLength(3);
  });

  it("lists none from a page of an agent older than the app, which serves no chats, nor once the page serves nothing", async () => {
    agent.registerAfterMs = -1;
    const { page, client } = await signedIn();
    await client.evaluate(() => (window as unknown as { fakeProjects: { register(lacks: string[]): void } }).fakeProjects.register(["chats"]));
    await page.waitForSelector("#projects .project");
    await new Promise((resolve) => setTimeout(resolve, 500));
    expect(await page.$$("#recents .recent")).toHaveLength(0);
    expect(await page.isVisible("#recents-head")).toBe(false);
    // Served again in full, then withdrawn.
    await client.evaluate(() => (window as unknown as { fakeProjects: { register(lacks: string[]): void } }).fakeProjects.register([]));
    await expect.poll(() => titles(page)).toHaveLength(3);
    await client.evaluate(() => window.surogateDesktop!.registerProjects(null));
    await expect.poll(() => page.$$eval("#recents .recent", (found) => found.length)).toBe(0);
    expect(await page.isVisible("#recents-head")).toBe(false);
  });
});
