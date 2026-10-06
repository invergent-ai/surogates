import { rmSync } from "node:fs";

import type { ElectronApplication, Page } from "playwright-core";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { FIXTURE_IDS, projectFixtures } from "../../../web/src/lib/projects.js";
import { connect, FakeAgent, webClient } from "./fake-agent.js";
import { dataHome, launch, quit, shellPage, stubNative } from "./launch.js";

const { report: REPORT, budget: BUDGET } = FIXTURE_IDS;

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
  rmSync(home, { recursive: true, force: true });
});

// The app connected to the fake agent, whose page serves the fixtures' projects.
async function signedIn(): Promise<{ shell: ElectronApplication; page: Page; client: Page }> {
  const shell = await launch(home);
  app = shell;
  await stubNative(shell);
  const page = await shellPage(shell);
  await connect(page, origin);
  const client = await webClient(shell, origin);
  await page.waitForSelector("#projects .project");
  return { shell, page, client };
}

const texts = (page: Page, selector: string) => page.$$eval(selector, (found) => found.map((element) => element.textContent));
const webShown = (shell: ElectronApplication) => shell.evaluate(({ BrowserWindow }) =>
  (BrowserWindow.getAllWindows()[0]!.contentView.children[0] as Electron.WebContentsView).getVisible());

describe("the sidebar's projects", () => {
  it("are the agent's, last active first, marked when a thread waits on the user, and searched by name", async () => {
    const { page } = await signedIn();
    expect(await texts(page, "#projects .project .name")).toEqual(["Quarterly report", "Budget"]);
    expect(await page.isVisible(`#projects [data-project="${REPORT}"] .waiting`)).toBe(true);
    expect(await page.isVisible(`#projects [data-project="${BUDGET}"] .waiting`)).toBe(false);
    await page.fill("#search", "budg");
    expect(await page.isVisible(`#projects [data-project="${REPORT}"]`)).toBe(false);
    expect(await page.isVisible(`#projects [data-project="${BUDGET}"]`)).toBe(true);
  });

  it("open a project's conversation, its master session, in the centre under the project's header", async () => {
    const { shell, page, client } = await signedIn();
    await page.click(`#projects [data-project="${REPORT}"] .project`);
    await expect.poll(() => client.url()).toBe(`${origin}/chat/${REPORT}`);
    expect(await page.textContent("#title")).toBe("Quarterly report");
    expect(await webShown(shell)).toBe(true);
    // A thread waits on the user: the Overview button says so.
    expect(await page.isVisible("#overview-dot")).toBe(true);
    await page.click(`#projects [data-project="${BUDGET}"] .project`);
    await expect.poll(() => page.textContent("#title")).toBe("Budget");
    expect(await page.isVisible("#overview-dot")).toBe(false);
    await page.click("#new");
    await expect.poll(() => client.url()).toBe(`${origin}/chat`);
  });
});

describe("the Projects page", () => {
  it("shows the projects as cards, sorted and searched, in place of the web client", async () => {
    const { shell, page } = await signedIn();
    await page.click("#open-projects");
    await expect.poll(() => page.isVisible("#projects-page")).toBe(true);
    expect(await webShown(shell)).toBe(false);
    expect(await page.textContent("#title")).toBe("Projects");
    expect(await texts(page, "#cards .card .name")).toEqual(["Quarterly report", "Budget"]);
    expect(await texts(page, "#cards .card .age")).toEqual(["17 minutes ago", "2 days ago"]);
    await page.selectOption("#sort", "name");
    expect(await texts(page, "#cards .card .name")).toEqual(["Budget", "Quarterly report"]);
    await page.selectOption("#sort", "created");
    expect(await texts(page, "#cards .card .name")).toEqual(["Quarterly report", "Budget"]);
    await page.fill("#project-search", "quart");
    expect(await texts(page, "#cards .card .name")).toEqual(["Quarterly report"]);
    await page.click("#cards .card");
    await expect.poll(() => webShown(shell)).toBe(true);
    expect(await page.isVisible("#projects-page")).toBe(false);
    await expect.poll(() => page.textContent("#title")).toBe("Quarterly report");
  });
});
