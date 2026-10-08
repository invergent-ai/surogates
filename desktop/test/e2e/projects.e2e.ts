import { rmSync } from "node:fs";

import type { ElectronApplication, Page } from "playwright-core";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { FIXTURE_IDS, type Project, type ProjectFixtures, projectFixtures } from "../../../web/src/lib/projects.js";
import { connect, FakeAgent, signIn, webClient } from "./fake-agent.js";
import { dataHome, launch, quit, shellPage, stubNative } from "./launch.js";

const { report: REPORT, budget: BUDGET } = FIXTURE_IDS;
const HIRING = "9f0a1b2c-3d4e-4f50-8a61-7b8c9d0e1f20";
// Each project's conversation: a master session of its own, never the project's id.
const MASTERS: Record<string, string> = {
  [REPORT]: "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d",
  [BUDGET]: "b2c3d4e5-f6a7-4b8c-9d0e-1f2a3b4c5d6e",
  [HIRING]: "c3d4e5f6-a7b8-4c9d-8e0f-2a3b4c5d6e7f",
};

// The fixtures' two projects and a third, listed neither last active first nor newest first, so
// the shell orders them itself. Last active: Report (17 minutes), Hiring (9 hours), Budget (2 days).
// Newest: Hiring (a day old), Report (3 days), Budget (10 days).
function fixtures(): ProjectFixtures {
  const data = projectFixtures();
  const [report, budget] = data.projects as [Project, Project];
  const hours = (count: number) => new Date(Date.now() - count * 3_600_000).toISOString();
  const hiring: Project = { ...budget, id: HIRING, name: "Hiring plan", createdAt: hours(24), updatedAt: hours(9) };
  data.projects = [budget, report, hiring].map((project) => ({ ...project, masterSessionId: MASTERS[project.id]! }));
  return data;
}

let home: string;
let agent: FakeAgent;
let origin: string;
let app: ElectronApplication | undefined;

beforeEach(async () => {
  home = dataHome();
  agent = new FakeAgent();
  agent.projects = fixtures();
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
  await signIn(shell, page, agent);
  const client = await webClient(shell, origin);
  await page.waitForSelector("#projects .project");
  return { shell, page, client };
}

// *project*'s conversation, open in the centre.
async function opened(page: Page, client: Page, project: string): Promise<void> {
  await page.click(`#projects [data-project="${project}"] .project`);
  await expect.poll(() => client.url()).toBe(`${origin}/chat/${MASTERS[project]}`);
}

const row = (project: string) => `#projects [data-project="${project}"] .project`;
const texts = (page: Page, selector: string) => page.$$eval(selector, (found) => found.map((element) => element.textContent));
const webShown = (shell: ElectronApplication) => shell.evaluate(({ BrowserWindow }) =>
  (BrowserWindow.getAllWindows()[0]!.contentView.children[0] as Electron.WebContentsView).getVisible());
// Two clicks in one frame: the second comes before the page has answered the first.
const clickBoth = (page: Page, first: string, second: string) => page.evaluate(([one, two]) => {
  document.querySelector<HTMLElement>(one!)!.click();
  document.querySelector<HTMLElement>(two!)!.click();
}, [first, second]);
const pause = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

describe("the sidebar's projects", () => {
  it("are the agent's, last active first, marked when a thread waits on the user, and searched by name", async () => {
    const { page } = await signedIn();
    expect(await texts(page, "#projects .project .name")).toEqual(["Quarterly report", "Hiring plan", "Budget"]);
    expect(await page.isVisible(`#projects [data-project="${REPORT}"] .waiting`)).toBe(true);
    expect(await page.getAttribute(row(REPORT), "aria-label")).toBe("Quarterly report, waiting on you");
    expect(await page.isVisible(`#projects [data-project="${BUDGET}"] .waiting`)).toBe(false);
    expect(await page.getAttribute(row(BUDGET), "aria-label")).toBe(null);
    expect(await page.isVisible("#projects-head")).toBe(true);
    await page.fill("#search", "budg");
    expect(await page.isVisible(`#projects [data-project="${REPORT}"]`)).toBe(false);
    expect(await page.isVisible(`#projects [data-project="${BUDGET}"]`)).toBe(true);
    // No row left: no heading over nothing.
    await page.fill("#search", "nothing like it");
    expect(await page.isVisible("#projects-head")).toBe(false);
  });

  it("open a project's conversation, its master session, in the centre under the project's header", async () => {
    const { shell, page, client } = await signedIn();
    await opened(page, client, REPORT);
    expect(await page.textContent("#title")).toBe("Quarterly report");
    expect(await webShown(shell)).toBe(true);
    expect(await page.getAttribute(row(REPORT), "aria-current")).toBe("page");
    // A thread waits on the user: the Overview button says so.
    expect(await page.isVisible("#overview-dot")).toBe(true);
    await opened(page, client, BUDGET);
    await expect.poll(() => page.textContent("#title")).toBe("Budget");
    expect(await page.isVisible("#overview-dot")).toBe(false);
    expect(await page.getAttribute(row(REPORT), "aria-current")).toBe(null);
    await page.click("#new");
    await expect.poll(() => client.url()).toBe(`${origin}/chat`);
  });

  it("follow the web client: Back from a second project is the first again, and a page of its own is no project", async () => {
    const { page, client } = await signedIn();
    await opened(page, client, REPORT);
    await opened(page, client, BUDGET);
    await page.click("#back");
    await expect.poll(() => client.url()).toBe(`${origin}/chat/${MASTERS[REPORT]}`);
    await expect.poll(() => page.textContent("#title")).toBe("Quarterly report");
    expect(await page.getAttribute(row(REPORT), "aria-current")).toBe("page");
    await page.click("#forward");
    await expect.poll(() => page.textContent("#title")).toBe("Budget");
    await client.evaluate(() => history.pushState(null, "", "/inbox"));
    await expect.poll(() => page.textContent("#title")).toBe(new URL(origin).host);
    expect(await page.$$eval('#projects [aria-current="page"]', (found) => found.length)).toBe(0);
  });

  it("keep the header in step with a project that is renamed", async () => {
    const { page, client } = await signedIn();
    await opened(page, client, REPORT);
    // The page tells the change once it serves again after the load; it is told until the header follows.
    await expect.poll(async () => {
      await client.evaluate((id) => {
        const fake = (window as unknown as { fakeProjects?: { data: ProjectFixtures; changed(id: string, threadId: null): void } }).fakeProjects;
        const found = fake?.data.projects.find((project) => project.id === id);
        if (!found) return;
        found.name = "Q3 report";
        fake!.changed(id, null);
      }, REPORT).catch(() => {});
      return page.textContent("#title");
    }).toBe("Q3 report");
  });

  it("are gone, with the open project, once the user signs out", async () => {
    const { page, client } = await signedIn();
    await opened(page, client, REPORT);
    await client.waitForLoadState();
    await client.evaluate(() => window.surogateDesktop!.registerProjects(null));
    await expect.poll(() => page.textContent("#title")).toBe(new URL(origin).host);
    expect(await page.$$eval("#projects .project", (found) => found.length)).toBe(0);
    expect(await page.isVisible("#projects-head")).toBe(false);
  });

  it("open the project clicked while the web client reloads", async () => {
    agent.registerAfterMs = 1_500;
    const { page, client } = await signedIn();
    await opened(page, client, REPORT);
    // Report's page is loading, and serves no projects yet.
    await page.click(row(BUDGET));
    await expect.poll(() => client.url(), { timeout: 8_000 }).toBe(`${origin}/chat/${MASTERS[BUDGET]}`);
    await expect.poll(() => page.textContent("#title")).toBe("Budget");
  });

  it("apply only the latest choice of what the centre shows", async () => {
    const { page, client } = await signedIn();
    await clickBoth(page, row(REPORT), row(BUDGET));
    await expect.poll(() => client.url()).toBe(`${origin}/chat/${MASTERS[BUDGET]}`);
    await expect.poll(() => page.textContent("#title")).toBe("Budget");
    await client.waitForLoadState();
    await pause(500);
    await clickBoth(page, row(REPORT), "#open-projects");
    // Report's answer comes after the Projects page was chosen: it opens nothing.
    await pause(1_000);
    expect(await page.isVisible("#projects-page")).toBe(true);
    expect(client.url()).toBe(`${origin}/chat/${MASTERS[BUDGET]}`);
  });

  it("keep serving when the web client is sent to an address outside the agent's, which opens in the browser", async () => {
    const { shell, page, client } = await signedIn();
    await opened(page, client, REPORT);
    // Report's page serves its projects again: its threads are in the pane.
    await page.waitForSelector(".section .thread");
    // As the web client's upgrade sends the user to the payment page.
    await client.evaluate(() => {
      location.href = "https://checkout.example.com/pay";
    });
    // After the sign-in's own address, which the system browser opened first.
    await expect.poll(() => shell.evaluate(() => (globalThis as unknown as { opened: string[] }).opened.slice(1)))
      .toEqual(["https://checkout.example.com/pay"]);
    await page.click(row(BUDGET));
    await expect.poll(() => client.url()).toBe(`${origin}/chat/${MASTERS[BUDGET]}`);
    expect(await page.isVisible("#failure")).toBe(false);
  });

  it("draw away a failure that Back or Forward cleared", async () => {
    agent.projects!.projects.find((project) => project.id === BUDGET)!.masterSessionId = "not-a-chat";
    const { page, client } = await signedIn();
    await opened(page, client, REPORT);
    await page.click(row(BUDGET));
    await expect.poll(() => page.isVisible("#failure")).toBe(true);
    // Nothing to go forward to: only the choice moves.
    await page.click("#forward");
    await expect.poll(() => page.isVisible("#failure")).toBe(false);
  });

  it("say why a project did not open", async () => {
    agent.projects!.projects.find((project) => project.id === BUDGET)!.masterSessionId = "not-a-chat";
    const { page, client } = await signedIn();
    await page.click(row(BUDGET));
    await expect.poll(() => page.textContent("#failure")).toBe("This project's conversation is not a chat");
    expect(await page.isVisible("#failure")).toBe(true);
    await opened(page, client, REPORT);
    await expect.poll(() => page.isVisible("#failure")).toBe(false);
  });
});

describe("the sidebar's pages", () => {
  it("open the web client's list of chats, a page of its own, and the user menu's Devices its Settings there", async () => {
    const { page, client } = await signedIn();
    await opened(page, client, REPORT);
    await page.click('[data-path="/chats"]');
    await expect.poll(() => client.url()).toBe(`${origin}/chats`);
    await expect.poll(() => page.textContent("#title")).toBe(new URL(origin).host);
    await page.click("#user");
    await page.click('#user-menu [data-action="devices"]');
    await expect.poll(() => client.url()).toBe(`${origin}/settings?tab=devices`);
    expect(await page.isVisible("#user-menu")).toBe(false);
  });

  it.each([["with", true], ["without", false]])("open the web client's own Settings from the user menu, for an agent %s local folders, and Devices only with them", async (_, localFolders) => {
    agent.config = { ...agent.config, desktop_sessions: localFolders };
    const { page, client } = await signedIn();
    await page.click("#user");
    expect(await page.isVisible('#user-menu [data-action="devices"]')).toBe(localFolders);
    await page.click('#user-menu [data-action="account"]');
    await expect.poll(() => client.url()).toBe(`${origin}/settings`);
    expect(await page.isVisible("#user-menu")).toBe(false);
  });
});

describe("the Projects page", () => {
  it("shows the projects as cards, sorted and searched, in place of the web client and the Overview", async () => {
    const { shell, page } = await signedIn();
    await page.click("#open-projects");
    await expect.poll(() => page.isVisible("#projects-page")).toBe(true);
    expect(await webShown(shell)).toBe(false);
    expect(await page.getAttribute("#open-projects", "aria-current")).toBe("page");
    // The page fills the window right of the sidebar: no project header over it, no Overview beside it.
    expect(await page.isVisible("#title")).toBe(false);
    expect(await page.isVisible("#overview")).toBe(false);
    expect(await page.isVisible("#panel")).toBe(false);
    expect(await page.isVisible("#centre .head")).toBe(true);
    const [right, width] = await page.evaluate(() => [
      Math.round(document.querySelector("#projects-page")!.getBoundingClientRect().right), window.innerWidth,
    ]);
    expect(right).toBe(width);
    expect(await texts(page, "#cards .card .name")).toEqual(["Quarterly report", "Hiring plan", "Budget"]);
    expect(await texts(page, "#cards .card .age")).toEqual(["17 minutes ago", "9 hours ago", "2 days ago"]);
    await page.selectOption("#sort", "name");
    expect(await texts(page, "#cards .card .name")).toEqual(["Budget", "Hiring plan", "Quarterly report"]);
    await page.selectOption("#sort", "created");
    expect(await texts(page, "#cards .card .name")).toEqual(["Hiring plan", "Quarterly report", "Budget"]);
    await page.fill("#project-search", "nothing like it");
    expect(await texts(page, "#cards .card .name")).toEqual([]);
    expect(await page.isVisible("#no-match")).toBe(true);
    expect(await page.isVisible("#no-projects")).toBe(false);
    await page.fill("#project-search", "quart");
    expect(await texts(page, "#cards .card .name")).toEqual(["Quarterly report"]);
    expect(await page.isVisible("#no-match")).toBe(false);
    await page.click("#cards .card");
    await expect.poll(() => webShown(shell)).toBe(true);
    expect(await page.isVisible("#projects-page")).toBe(false);
    await expect.poll(() => page.textContent("#title")).toBe("Quarterly report");
    expect(await page.isVisible("#title")).toBe(true);
    expect(await page.isVisible("#panel")).toBe(true);
    expect(await page.getAttribute("#open-projects", "aria-current")).toBe(null);
  });

  it("gives each project a mark of its own, the same in the sidebar, on its card and over its conversation", async () => {
    const { page, client } = await signedIn();
    // A mark: its hue and its icon's drawing.
    const mark = (selector: string) => page.$eval(selector, (found) => `${(found as HTMLElement).dataset.hue} ${found.querySelector("svg")!.innerHTML}`);
    const sidebar = await Promise.all([REPORT, BUDGET, HIRING].map((id) => mark(`#projects [data-project="${id}"] .pmark`)));
    expect(new Set(sidebar).size).toBeGreaterThan(1);
    // The dot that says a thread waits sits on the mark, whole: what is drawn past the mark's edge is the dot's too.
    await expect.poll(() => page.$eval(`#projects [data-project="${REPORT}"] .pmark .waiting`, (dot) => {
      const { right, top, height } = dot.getBoundingClientRect();
      return document.elementFromPoint(right - 1.5, top + height / 2) === dot;
    }), { timeout: 5_000 }).toBe(true);
    await page.click("#open-projects");
    await expect.poll(() => Promise.all([REPORT, BUDGET, HIRING].map((id) => mark(`#cards [data-project="${id}"] .pmark`))), { timeout: 5_000 })
      .toEqual(sidebar);
    await page.click(`#cards [data-project="${BUDGET}"]`);
    await expect.poll(() => client.url()).toBe(`${origin}/chat/${MASTERS[BUDGET]}`);
    await expect.poll(() => mark("#project-icon .pmark"), { timeout: 5_000 }).toBe(sidebar[1]);
  });

  it("ages its cards as time goes on, with nothing drawn again", async () => {
    const { page } = await signedIn();
    // The window's page starts again on the test's clock.
    await page.clock.install();
    await page.reload();
    await page.waitForSelector("#projects .project");
    await page.click("#open-projects");
    await expect.poll(() => texts(page, "#cards .card .age"), { timeout: 5_000 }).toEqual(["17 minutes ago", "9 hours ago", "2 days ago"]);
    await page.clock.fastForward("01:00:00");
    await expect.poll(() => texts(page, "#cards .card .age"), { timeout: 5_000 }).toEqual(["1 hour ago", "10 hours ago", "2 days ago"]);
  });

  it("draws as the references do: three columns of cards at most, the first project's ring in sight, and nothing through Couldn't connect", async () => {
    const { shell, page, client } = await signedIn();
    await shell.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0]!.setBounds({ x: 0, y: 0, width: 1600, height: 1000 }));
    await page.click("#open-projects");
    await expect.poll(() => page.$eval("#cards", (found) => getComputedStyle(found).gridTemplateColumns.split(" ").length), { timeout: 5_000 }).toBe(3);
    // The ring the first project's row draws with the keyboard on it, inside the list that scrolls.
    await page.focus(row(REPORT));
    const [ring, list] = await page.evaluate((chosen) => {
      const button = document.querySelector(chosen)!;
      const width = Number.parseFloat(getComputedStyle(button).outlineWidth) + Number.parseFloat(getComputedStyle(button).outlineOffset);
      return [button.getBoundingClientRect().top - width, document.querySelector("#projects")!.getBoundingClientRect().top];
    }, row(REPORT));
    expect(ring).toBeGreaterThanOrEqual(list);
    agent.pagesRedirect = "https://sso.example.com/login";
    await client.reload().catch(() => {});
    await expect.poll(() => page.isVisible("#unreachable"), { timeout: 5_000 }).toBe(true);
    expect(await page.$eval("#unreachable", (found) => getComputedStyle(found).backgroundColor))
      .toBe(await page.$eval("#centre", (found) => getComputedStyle(found).backgroundColor));
  });

  it("is left by Back for the thread that was open, with its project as the way back", async () => {
    const { page, client } = await signedIn();
    await opened(page, client, REPORT);
    const thread = FIXTURE_IDS.question;
    // Its row reads it in the pane; the pane's Open shows it in the centre.
    await page.click(`[data-thread="${thread}"]`);
    await page.click("#reading-open");
    await expect.poll(() => client.url()).toBe(`${origin}/chat/${thread}`);
    await page.click("#open-projects");
    await expect.poll(() => page.isVisible("#projects-page")).toBe(true);
    await page.click("#back");
    await expect.poll(() => page.isVisible("#projects-page")).toBe(false);
    expect(client.url()).toBe(`${origin}/chat/${thread}`);
    await expect.poll(() => page.textContent("#title")).toBe("Check the revenue figures");
    expect(await page.textContent("#to-project")).toBe("Quarterly report");
  });

  it("is left by Back, for the web client where it was", async () => {
    const { shell, page, client } = await signedIn();
    await opened(page, client, REPORT);
    await page.click("#open-projects");
    await expect.poll(() => page.isVisible("#projects-page")).toBe(true);
    await page.click("#back");
    await expect.poll(() => page.isVisible("#projects-page")).toBe(false);
    expect(await webShown(shell)).toBe(true);
    expect(client.url()).toBe(`${origin}/chat/${MASTERS[REPORT]}`);
    await expect.poll(() => page.textContent("#title")).toBe("Quarterly report");
  });
});

// The app signed in to the fake agent, whose page is up and serves nothing until a test registers it.
async function unserved(): Promise<{ shell: ElectronApplication; page: Page; client: Page }> {
  agent.registerAfterMs = -1;
  const shell = await launch(home);
  app = shell;
  await stubNative(shell);
  const page = await shellPage(shell);
  await connect(page, origin);
  await signIn(shell, page, agent);
  return { shell, page, client: await webClient(shell, origin) };
}

// What the page's preload answers *call*, sent as the main process sends one, or "no answer" after *waitMs*.
const callPage = (shell: ElectronApplication, call: { id: number; method: string; args: unknown[]; deadline: number }, waitMs = 2_000) =>
  shell.evaluate(({ webContents }, [url, sent, wait]) => new Promise((resolve) => {
    const contents = webContents.getAllWebContents().find((found) => found.getURL().startsWith(url))!;
    const timer = setTimeout(() => resolve("no answer"), wait);
    contents.ipc.on("desktop:projects-answer", (_event, id: unknown, outcome: unknown) => {
      if (id !== sent.id) return;
      clearTimeout(timer);
      resolve(outcome);
    });
    contents.send("desktop:projects", { type: "call", ...sent });
  }), [origin, call, waitMs] as const);

describe("the page's projects source", () => {
  it("refuses the calls it holds once the page says it serves none", async () => {
    const { shell, client } = await unserved();
    const held = callPage(shell, { id: 999_990, method: "list", args: [], deadline: Date.now() + 60_000 }, 5_000);
    // The call reaches the page's hold before the page says it serves nothing.
    await pause(300);
    await client.evaluate(() => window.surogateDesktop!.registerProjects(null));
    expect(await held).toEqual({ error: "The agent's page serves no projects" });
  });

  it("refuses at once a call that comes after the page said it serves none, and holds none", async () => {
    const { shell, client } = await unserved();
    await client.evaluate(() => window.surogateDesktop!.registerProjects(null));
    const started = Date.now();
    expect(await callPage(shell, { id: 999_991, method: "list", args: [], deadline: Date.now() + 60_000 }))
      .toEqual({ error: "The agent's page serves no projects" });
    expect(Date.now() - started).toBeLessThan(1_000);
  });

  it("says so when the page answers with something it cannot send, rather than letting the call run out of time", async () => {
    const { shell, client } = await unserved();
    // A source whose project holds a function, which no message can carry.
    await client.evaluate(() => {
      const nothing = async () => [];
      void window.surogateDesktop!.registerProjects({
        list: nothing, get: async () => ({ id: "x", open() {} }), create: nothing, update: nothing, archive: nothing,
        threads: nothing, resolve: nothing, reopen: nothing, library: nothing, routines: nothing, subscribe: () => () => {},
      } as never);
    });
    expect(await callPage(shell, { id: 999_993, method: "get", args: [REPORT], deadline: Date.now() + 60_000 }))
      .toEqual({ error: "The agent's page answered with something it cannot send" });
  });

  it("never runs a held call with under a second left, whose answer would come after the main process gave up", async () => {
    const { shell, client } = await unserved();
    const answered = callPage(shell, { id: 999_992, method: "threads", args: [REPORT, "late"], deadline: Date.now() + 1_000 });
    // Held a fifth of a second: the page serves with four fifths left.
    await pause(200);
    await client.evaluate(() => (window as unknown as { fakeProjects: { register(): void } }).fakeProjects.register());
    expect(await answered).toBe("no answer");
    expect(await client.evaluate(() => (window as unknown as { fakeProjects: { reads: unknown[] } }).fakeProjects.reads)).not.toContain("late");
  });

  it("answers a call that reaches the page before it serves, once it serves, and drops one whose time ran out", async () => {
    // The page serves its projects only once the test says so.
    agent.registerAfterMs = -1;
    const shell = await launch(home);
    app = shell;
    await stubNative(shell);
    const page = await shellPage(shell);
    await connect(page, origin);
    await signIn(shell, page, agent);
    const client = await webClient(shell, origin);
    // The page is up and serves nothing yet, as a page whose load has just committed: two calls reach
    // it, one of them past its time, which the main process has refused already.
    await shell.evaluate(({ webContents }, [url, project]) => {
      const contents = webContents.getAllWebContents().find((found) => found.getURL().startsWith(url!))!;
      Object.assign(globalThis, {
        early: new Promise((resolve) => {
          contents.ipc.on("desktop:projects-answer", (_event, id: unknown, answered: unknown) => {
            if (id === 999_998) Object.assign(globalThis, { late: answered });
            if (id === 999_999) resolve(answered);
          });
        }),
      });
      contents.send("desktop:projects", { type: "call", id: 999_998, method: "threads", args: [project], deadline: 0 });
      contents.send("desktop:projects", { type: "call", id: 999_999, method: "list", args: [], deadline: Date.now() + 60_000 });
    }, [origin, REPORT] as const);
    await client.evaluate(() => (window as unknown as { fakeProjects: { register(): void } }).fakeProjects.register());
    const outcome = await shell.evaluate(() => (globalThis as unknown as { early: Promise<unknown> }).early);
    expect((outcome as { ok?: Array<{ name: string }> }).ok?.map((project) => project.name).sort())
      .toEqual(["Budget", "Hiring plan", "Quarterly report"]);
    // Held with the one answered, the late one would have run before it: it never ran.
    expect(await client.evaluate(() => (window as unknown as { fakeProjects: { reads: unknown[] } }).fakeProjects.reads)).toEqual([]);
    expect(await shell.evaluate(() => (globalThis as unknown as { late?: unknown }).late)).toBeUndefined();
  });

  it("refuses a source whose methods are not its own, as a class instance's are, and keeps the one it serves", async () => {
    const { page, client } = await signedIn();
    const refused = await client.evaluate(async () => {
      // Every method a source has, on its prototype: refused only because the copy keeps no prototype.
      const nothing = () => Promise.resolve([]);
      class Source {
        list() { return nothing(); }
        get() { return nothing(); }
        create() { return nothing(); }
        update() { return nothing(); }
        archive() { return Promise.resolve(); }
        threads() { return nothing(); }
        resolve() { return nothing(); }
        reopen() { return nothing(); }
        library() { return nothing(); }
        routines() { return nothing(); }
        subscribe() { return () => {}; }
      }
      const source = new Source();
      try {
        await window.surogateDesktop!.registerProjects(source as never);
        return "registered";
      } catch (error) {
        return (error as Error).message;
      }
    });
    expect(refused).toContain("A projects source's methods must be its own properties");
    // The source it serves still answers: a project's read opens it, where a source swapped in would refuse it.
    await opened(page, client, REPORT);
    await expect.poll(() => page.textContent("#title")).toBe("Quarterly report");
    expect(await page.isVisible("#failure")).toBe(false);
  });
});

// The project dialog's page, once it is open over the window and has drawn what it shows, which
// can take *timeout* ms when it waits for the page to serve.
async function projectDialog(shell: ElectronApplication, timeout = 1_000): Promise<Page> {
  let found: Page | undefined;
  await expect.poll(() => {
    found = shell.windows().find((page) => page.url().endsWith("/project.html"));
    return found !== undefined;
  }).toBe(true);
  await found!.waitForLoadState();
  await expect.poll(() => found!.textContent("#heading"), { timeout }).not.toBe("");
  return found!;
}

const overlayOpen = (shell: ElectronApplication, page: string) => shell.evaluate(({ BrowserWindow }, file) =>
  BrowserWindow.getAllWindows()[0]!.contentView.children
    .some((view) => (view as Electron.WebContentsView).webContents.getURL().endsWith(file)), page);
const dialogOpen = (shell: ElectronApplication) => overlayOpen(shell, "/project.html");
// Click *selector* on the dialog, which closes it: the dialog can go before the click is acknowledged,
// and that it went is what tells the click landed.
const clickClosing = (dialog: Page, selector: string) => dialog.click(selector, { noWaitAfter: true }).catch(() => {});

describe("the project dialog", () => {
  it("makes a new project from its name and goal, lists it at once, and opens its conversation", async () => {
    const { shell, page, client } = await signedIn();
    await page.click("#open-projects");
    await page.click("#new-project");
    const dialog = await projectDialog(shell);
    expect(await dialog.textContent("#heading")).toBe("New project");
    expect(await dialog.isVisible("#instructions")).toBe(false);
    expect(await dialog.isVisible("#archive")).toBe(false);
    await dialog.fill("#name", "  Hiring brief ");
    await dialog.fill("#goal", "Hire two analysts by December.");
    await clickClosing(dialog, "#save");
    await expect.poll(() => dialogOpen(shell)).toBe(false);
    const made = agent.projects!.projects.find((project) => project.name === "Hiring brief")!;
    expect(made.goal).toBe("Hire two analysts by December.");
    await expect.poll(() => client.url()).toBe(`${origin}/chat/${made.masterSessionId}`);
    await expect.poll(() => page.textContent("#title")).toBe("Hiring brief");
    expect(await page.getAttribute(row(made.id), "aria-current")).toBe("page");
  });

  it("changes the open project's name, instructions and tiers, then archives it", async () => {
    const { shell, page, client } = await signedIn();
    await opened(page, client, REPORT);
    await page.click("#project-settings");
    let dialog = await projectDialog(shell);
    expect(await dialog.textContent("#heading")).toBe("Project settings");
    expect(await dialog.inputValue("#name")).toBe("Quarterly report");
    await dialog.fill("#name", "Q3 report");
    await dialog.fill("#instructions", "Write in French.");
    await dialog.selectOption("#thread-tier", "pro");
    await clickClosing(dialog, "#save");
    await expect.poll(() => dialogOpen(shell)).toBe(false);
    await expect.poll(() => page.textContent("#title")).toBe("Q3 report");
    expect(agent.projects!.projects.find((project) => project.id === REPORT))
      .toMatchObject({ name: "Q3 report", instructions: "Write in French.", coordinatorTier: null, threadTier: "pro" });
    await page.click("#project-settings");
    dialog = await projectDialog(shell);
    await clickClosing(dialog, "#archive");
    await expect.poll(() => dialogOpen(shell)).toBe(false);
    const asked = await shell.evaluate(() => (globalThis as unknown as { asked: Array<{ message: string }> }).asked);
    expect(asked.at(-1)!.message).toBe("Archive Q3 report?");
    await expect.poll(() => page.isVisible(`#projects [data-project="${REPORT}"]`)).toBe(false);
    await expect.poll(() => client.url()).toBe(`${origin}/chat`);
    expect(await page.textContent("#title")).toBe(new URL(origin).host);
  });

  for (const [why, cut, said] of [
    ["archived on another device", "archived", "No such project"],
    ["with the agent out of reach", "unreachable", "API server is not reachable."],
    ["while the page has served nothing for ten seconds", "loading", "The agent's page did not serve its projects in time"],
  ] as const) {
    it(`says why a project's settings cannot open: ${why}`, async () => {
      const { shell, page, client } = await signedIn();
      await opened(page, client, REPORT);
      if (cut === "loading") {
        // The page loads again, and serves nothing this time.
        agent.registerAfterMs = -1;
        await client.reload();
      } else {
        await client.evaluate(([how, project]) => {
          const fake = (window as unknown as { fakeProjects: { data: ProjectFixtures; unreachable: boolean } }).fakeProjects;
          if (how === "unreachable") fake.unreachable = true;
          else fake.data.projects = fake.data.projects.filter((found) => found.id !== project);
        }, [cut, REPORT] as const);
      }
      await page.click("#project-settings");
      const dialog = await projectDialog(shell, 15_000);
      expect(await dialog.textContent("#heading")).toBe("Project settings");
      expect(await dialog.textContent("#error")).toBe(said);
      expect(await dialog.isVisible("#save")).toBe(false);
      expect(await dialog.isVisible("#archive")).toBe(false);
      // Nothing to change: only Cancel is left, with the focus.
      for (const field of ["#name", "#goal", "#instructions", "#coordinator-tier", "#thread-tier"]) {
        expect(await dialog.isEnabled(field), field).toBe(false);
      }
      expect(await dialog.evaluate(() => document.activeElement?.id)).toBe("cancel");
      await clickClosing(dialog, "#cancel");
      await expect.poll(() => dialogOpen(shell)).toBe(false);
    });
  }

  it("says a create that did not answer in time may have made the project, and lists it once made", async () => {
    const { shell, page, client } = await signedIn();
    await client.evaluate(() => {
      (window as unknown as { fakeProjects: { lag: number } }).fakeProjects.lag = 11_000;
    });
    await page.click("#open-projects");
    await page.click("#new-project");
    const dialog = await projectDialog(shell);
    await dialog.fill("#name", "Late");
    await dialog.click("#save");
    await expect.poll(() => dialog.textContent("#error"), { timeout: 15_000 })
      .toBe("The agent's page did not answer create in time: the project may have been made");
    expect(await dialog.inputValue("#name")).toBe("Late");
    await expect.poll(() => texts(page, "#projects .project .name")).toContain("Late");
  });

  it("names each field by its label alone, describes it by its help, and gives the name the focus back after a refusal", async () => {
    const { shell, page, client } = await signedIn();
    await opened(page, client, REPORT);
    await page.click("#project-settings");
    const dialog = await projectDialog(shell);
    const named = (role: "textbox" | "combobox", name: string) => dialog.getByRole(role, { name, exact: true }).getAttribute("id");
    expect(await named("textbox", "Goal")).toBe("goal");
    expect(await named("textbox", "Instructions")).toBe("instructions");
    expect(await named("combobox", "The conversation's model")).toBe("coordinator-tier");
    expect(await named("combobox", "The threads' model")).toBe("thread-tier");
    const described = (id: string) => dialog.$eval(`#${id}`, (field) =>
      (field.getAttribute("aria-describedby") ?? "").split(" ").map((by) => document.getElementById(by)?.textContent).join(" "));
    expect(await described("goal")).toBe("What the project is for. Its conversation and its threads see it.");
    expect(await described("thread-tier")).toBe("A model above your plan's runs as your plan's.");
    expect(await described("coordinator-tier")).toBe("A model above your plan's runs as your plan's.");
    await dialog.fill("#name", " ");
    await dialog.click("#save");
    await expect.poll(() => dialog.textContent("#error")).toBe("Name the project.");
    expect(await dialog.evaluate(() => document.activeElement?.id)).toBe("name");
  });

  it("says a create or a save is under way while the agent answers it", async () => {
    const { shell, page, client } = await signedIn();
    await client.evaluate(() => {
      (window as unknown as { fakeProjects: { lag: number } }).fakeProjects.lag = 1_500;
    });
    await page.click("#open-projects");
    await page.click("#new-project");
    let dialog = await projectDialog(shell);
    await dialog.fill("#name", "  ");
    await dialog.click("#save");
    await expect.poll(() => dialog.textContent("#error")).toBe("Name the project.");
    expect([await dialog.textContent("#save"), await dialog.getAttribute("#form", "aria-busy")]).toEqual(["Create project", null]);
    await dialog.fill("#name", "Busy");
    await dialog.click("#save");
    expect([await dialog.textContent("#save"), await dialog.getAttribute("#form", "aria-busy")]).toEqual(["Creating…", "true"]);
    await expect.poll(() => dialogOpen(shell), { timeout: 5_000 }).toBe(false);
    await expect.poll(() => page.textContent("#title")).toBe("Busy");
    // The new project's conversation is a load of its own, which can start after the title shows: its
    // page is as slow again, once it has loaded.
    const busy = agent.projects!.projects.find((project) => project.name === "Busy")!;
    await client.waitForURL(`${origin}/chat/${busy.masterSessionId}`);
    await client.evaluate(() => {
      (window as unknown as { fakeProjects: { lag: number } }).fakeProjects.lag = 1_500;
    });
    await page.click("#project-settings");
    dialog = await projectDialog(shell);
    await dialog.fill("#name", "Busier");
    await dialog.click("#save");
    expect([await dialog.textContent("#save"), await dialog.getAttribute("#form", "aria-busy")]).toEqual(["Saving…", "true"]);
    await expect.poll(() => dialogOpen(shell), { timeout: 5_000 }).toBe(false);
  });

  it("makes one project for a Create clicked twice before the agent answers", async () => {
    const { shell, page, client } = await signedIn();
    await client.evaluate(() => {
      (window as unknown as { fakeProjects: { lag: number } }).fakeProjects.lag = 1_000;
    });
    await page.click("#open-projects");
    await page.click("#new-project");
    const dialog = await projectDialog(shell);
    await dialog.fill("#name", "Twice");
    await dialog.evaluate(() => {
      document.querySelector<HTMLButtonElement>("#save")!.click();
      document.querySelector<HTMLButtonElement>("#save")!.click();
    });
    await expect.poll(() => dialogOpen(shell), { timeout: 5_000 }).toBe(false);
    expect(agent.projects!.projects.filter((project) => project.name === "Twice")).toHaveLength(1);
  });

  it("opens nothing for a create that lands after its dialog closed, and leaves Settings opened since", async () => {
    const { shell, page, client } = await signedIn();
    const before = client.url();
    await client.evaluate(() => {
      (window as unknown as { fakeProjects: { lag: number } }).fakeProjects.lag = 1_500;
    });
    await page.click("#open-projects");
    await page.click("#new-project");
    const dialog = await projectDialog(shell);
    await dialog.fill("#name", "Escaped");
    await dialog.click("#save");
    // Closed while the create is under way, and Settings opened in its place.
    await dialog.keyboard.press("Escape").catch(() => {});
    await expect.poll(() => dialogOpen(shell)).toBe(false);
    await page.evaluate(() => (window as unknown as { surogateShell: { settings(): Promise<void> } }).surogateShell.settings());
    await expect.poll(() => overlayOpen(shell, "/settings.html")).toBe(true);
    await expect.poll(() => texts(page, "#projects .project .name"), { timeout: 5_000 }).toContain("Escaped");
    expect(await overlayOpen(shell, "/settings.html")).toBe(true);
    expect(await page.isVisible("#projects-page")).toBe(true);
    expect(client.url()).toBe(before);
  });

  it("saves only what the user changed, and keeps what was changed elsewhere meanwhile", async () => {
    const { shell, page, client } = await signedIn();
    await opened(page, client, REPORT);
    await page.click("#project-settings");
    const dialog = await projectDialog(shell);
    // Another device changes the goal and the instructions while the dialog shows the old ones.
    await client.evaluate((project) => {
      const fake = (window as unknown as { fakeProjects: { data: ProjectFixtures } }).fakeProjects;
      Object.assign(fake.data.projects.find((found) => found.id === project)!, { goal: "Changed elsewhere", instructions: "Also elsewhere" });
    }, REPORT);
    await dialog.selectOption("#thread-tier", "pro");
    await clickClosing(dialog, "#save");
    await expect.poll(() => dialogOpen(shell)).toBe(false);
    expect(agent.projects!.projects.find((project) => project.id === REPORT)).toMatchObject({
      name: "Quarterly report", goal: "Changed elsewhere", instructions: "Also elsewhere", threadTier: "pro",
    });
  });

  it("names the project in the archive box with its control and invisible characters as their code points", async () => {
    agent.projects!.projects.find((project) => project.id === REPORT)!.name = "Q3‮ report​";
    const { shell, page, client } = await signedIn();
    await opened(page, client, REPORT);
    await page.click("#project-settings");
    const dialog = await projectDialog(shell);
    await shell.evaluate(() => Object.assign(globalThis, { answer: 1 }));
    await dialog.click("#archive");
    await expect.poll(() => shell.evaluate(() => (globalThis as unknown as { asked: Array<{ message: string }> }).asked.at(-1)?.message))
      .toBe("Archive Q3U+202E reportU+200B?");
  });

  it("goes with the account whose project it shows", async () => {
    const { shell, page, client } = await signedIn();
    await opened(page, client, REPORT);
    await page.click("#project-settings");
    await projectDialog(shell);
    // The page says nobody is signed in, as after its session expired.
    await client.evaluate(() => window.surogateDesktop!.registerProjects(null));
    await expect.poll(() => dialogOpen(shell)).toBe(false);
  });

  it("stays open, with what was typed, on an Escape that cancels an input method's composition", async () => {
    const { shell, page } = await signedIn();
    await page.click("#open-projects");
    await page.click("#new-project");
    const dialog = await projectDialog(shell);
    await dialog.fill("#name", "Hiring brief");
    await dialog.evaluate(() => {
      document.getElementById("name")!.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", isComposing: true, bubbles: true }));
    });
    await pause(500);
    expect(await dialogOpen(shell)).toBe(true);
    expect(await dialog.inputValue("#name")).toBe("Hiring brief");
  });

  it("asks for a name, says what the agent refused, and keeps a project when the archive is cancelled", async () => {
    const { shell, page, client } = await signedIn();
    await page.click("#open-projects");
    await page.click("#new-project");
    let dialog = await projectDialog(shell);
    await dialog.fill("#name", "   ");
    await dialog.fill("#goal", "Hire two analysts by December.");
    await dialog.click("#save");
    await expect.poll(() => dialog.textContent("#error")).toBe("Name the project.");
    expect([await dialog.inputValue("#name"), await dialog.inputValue("#goal")]).toEqual(["   ", "Hire two analysts by December."]);
    await client.evaluate(() => {
      (window as unknown as { fakeProjects: { refusal: string | null } }).fakeProjects.refusal = "This agent keeps a single conversation, so it has no projects.";
    });
    await dialog.fill("#name", "Hiring brief");
    await dialog.click("#save");
    await expect.poll(() => dialog.textContent("#error")).toBe("This agent keeps a single conversation, so it has no projects.");
    expect([await dialog.inputValue("#name"), await dialog.inputValue("#goal")]).toEqual(["Hiring brief", "Hire two analysts by December."]);
    // The dialog goes on the key's way down, before its way up.
    await dialog.press("#name", "Escape").catch(() => {});
    await expect.poll(() => dialogOpen(shell)).toBe(false);
    await client.evaluate(() => {
      (window as unknown as { fakeProjects: { refusal: string | null } }).fakeProjects.refusal = null;
    });
    await opened(page, client, REPORT);
    await page.click("#project-settings");
    dialog = await projectDialog(shell);
    await shell.evaluate(() => Object.assign(globalThis, { answer: 1 }));
    await dialog.click("#archive");
    await expect.poll(() => shell.evaluate(() => (globalThis as unknown as { asked: Array<{ message: string }> }).asked.at(-1)!.message))
      .toBe("Archive Quarterly report?");
    expect(await dialogOpen(shell)).toBe(true);
    expect(agent.projects!.projects.some((project) => project.id === REPORT)).toBe(true);
  });
});
