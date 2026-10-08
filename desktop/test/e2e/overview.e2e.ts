import { readFileSync, rmSync } from "node:fs";
import { join } from "node:path";

import type { ElectronApplication, Page } from "playwright-core";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { FIXTURE_IDS, type ProjectFixtures, projectFixtures } from "../../../web/src/lib/projects.js";
import { ACCOUNT, connect, FakeAgent, signIn, webClient } from "./fake-agent.js";
import { dataHome, launch, quit, shellPage, stubNative } from "./launch.js";

const { report: REPORT, budget: BUDGET, question: QUESTION, approval: APPROVAL, failed: FAILED, idle: IDLE, resolved: RESOLVED } = FIXTURE_IDS;

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

// The app signed in to the fake agent as Flavius, with *project* open.
async function opened(project: string = REPORT): Promise<{ shell: ElectronApplication; page: Page; client: Page }> {
  const shell = await launch(home);
  app = shell;
  await stubNative(shell);
  const page = await shellPage(shell);
  await connect(page, origin);
  await signIn(shell, page, agent);
  const client = await webClient(shell, origin);
  await client.evaluate((account) => window.surogateDesktop!.setAccount(account), ACCOUNT);
  await page.click(`#projects [data-project="${project}"] .project`);
  await page.waitForSelector(".section .thread, #routine-list li, #files li", { state: "attached" });
  return { shell, page, client };
}

const texts = (page: Page, selector: string) => page.$$eval(selector, (found) => found.map((element) => element.textContent));
const views = (shell: ElectronApplication) => shell.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0]!.contentView.children.length);
const rows = (page: Page) => page.$$eval(".section .thread", (found) => found.length);
const pause = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));
// The pane's transcript, the window's view at /transcript/: its address and its bounds, or null while it shows none.
const pane = (shell: ElectronApplication) => shell.evaluate(({ BrowserWindow }) => {
  const found = (BrowserWindow.getAllWindows()[0]!.contentView.children as Electron.WebContentsView[])
    .find((view) => new URL(view.webContents.getURL() || "about:blank").pathname.startsWith("/transcript/"));
  if (!found) return null;
  const { x, y, width, height } = found.getBounds();
  return { url: found.webContents.getURL(), bounds: [x, y, width, height] };
});
const transcript = (thread: string, settings = "textSize=medium&transcriptWidth=medium&motion=system") =>
  `${origin}/transcript/${thread}?${settings}`;
// The hole the pane's transcript fills, as the window's page lays it out.
const hole = (page: Page) => page.$eval("#pane-hole", (found) => {
  const { x, y, width, height } = found.getBoundingClientRect();
  return [Math.round(x), Math.round(y), Math.round(width), Math.round(height)];
});
// *thread* in the centre, as its row's transcript in the pane opens it there.
async function viewThread(page: Page, thread: string): Promise<void> {
  await page.click(`[data-thread="${thread}"]`);
  await page.click("#reading-open");
}

// The fake page's source, as the page keeps it (fake-agent.ts).
interface Served {
  data: ProjectFixtures;
  lists: number;
  reads: Array<string | null>;
  refusal: string | null;
  unreachable: boolean;
  changed(id: string, threadId: string | null): void;
}

describe("the Overview pane", () => {
  it("greets the user, and groups the project's threads by what they need", async () => {
    const { page } = await opened();
    expect(await page.textContent("#greeting")).toBe("Welcome back, Flavius.");
    expect(await page.textContent("#greeting-line")).toBe("3 threads are waiting on you.");
    expect(await page.textContent("#thread-count")).toBe("3");
    expect(await texts(page, ".section summary")).toEqual(["Waiting on you 3", "Working 2", "Idle 1", "Resolved 1"]);
    expect(await page.$eval('[data-group="resolved"]', (section) => (section as HTMLDetailsElement).open)).toBe(false);
    expect(await texts(page, '[data-group="waiting"] .thread .title'))
      .toEqual(["Check the revenue figures", "Send the draft to finance", "Convert the old reports"]);
    expect(await texts(page, '[data-group="waiting"] .thread .status')).toEqual([
      "Question · Which quarter's exchange rate should I use?",
      "Approval · Send an email to finance@example.com?",
      "Failed · The PDF could not be opened: it is encrypted",
    ]);
    const first = `[data-thread="${QUESTION}"]`;
    expect(await texts(page, `${first} .chip`)).toEqual(["revenue.xlsx"]);
    expect(await page.textContent(`${first} .progress`)).toBe("2/5");
    expect(await page.textContent(`${first} .age`)).toBe("17m");
    // A thread on this computer carries its laptop; a thread with many files shows two and a count.
    expect(await page.getAttribute(`[data-thread="${FIXTURE_IDS.computer}"] .place`, "title")).toBe("On thinkpad, which is offline");
    expect(await texts(page, `[data-thread="${FIXTURE_IDS.idle}"] .chip`)).toEqual(["north.csv", "south.csv", "+1"]);
  });

  it("shows the project's library, and its routines only when it has some", async () => {
    const { page } = await opened();
    expect(await page.isVisible('[data-tab="routines"]')).toBe(false);
    await page.click('[data-tab="library"]');
    expect(await page.isVisible("#library")).toBe(true);
    expect(await page.isVisible("#threads")).toBe(false);
    expect(await texts(page, "#files .path")).toEqual(["threads/revenue/revenue.xlsx", "threads/summary/summary.docx", "brief.docx"]);
    expect(await texts(page, "#files .from")).toEqual([
      "From Check the revenue figures · 10 KB", "From Draft the summary · 51 KB", "Added by you · 18 KB",
    ]);
    await page.click(`#projects [data-project="${BUDGET}"] .project`);
    await expect.poll(() => page.isVisible('[data-tab="routines"]')).toBe(true);
    await page.click('[data-tab="routines"]');
    expect(await texts(page, "#routine-list .path")).toEqual(["Monthly spend check", "Weekly cash report"]);
    expect(await texts(page, "#routine-list .from")).toEqual(["On the 1st of every month at 09:00", "Every Monday at 08:00"]);
  });

  it("marks a file a thread made on the user's computer with its laptop, apart from the cloud's file of that name", async () => {
    const minutesAgo = (minutes: number) => new Date(Date.now() - minutes * 60_000).toISOString();
    agent.projects!.library[REPORT] = [
      { path: "Budget.xlsx", origin: "added", threadId: null, size: 14, updatedAt: minutesAgo(5), place: { kind: "cloud" } },
      {
        path: "Budget.xlsx", origin: "produced", threadId: FIXTURE_IDS.computer, size: null, updatedAt: minutesAgo(1),
        place: { kind: "device", deviceId: "d", deviceName: "thinkpad", online: true },
      },
    ];
    const { page } = await opened();
    await page.click('[data-tab="library"]');
    expect(await page.$$eval("#files li", (files) => files.map((file) => [
      file.querySelector(".from")!.textContent, file.querySelector(".place")?.getAttribute("title") ?? null,
    ]))).toEqual([["From Tidy the shared folder", "On thinkpad, which is online"], ["Added by you · 1 KB", null]]);
  });

  it("opens a thread in the centre from the pane's Open, with its project as the way back, and draws no second web client", async () => {
    const { shell, page, client } = await opened();
    await viewThread(page, QUESTION);
    await expect.poll(() => client.url()).toBe(`${origin}/chat/${QUESTION}`);
    expect(await page.textContent("#title")).toBe("Check the revenue figures");
    expect(await page.textContent("#to-project")).toBe("Quarterly report");
    await expect.poll(() => views(shell)).toBe(1);
    expect(await page.isVisible("#threads")).toBe(true);
    await page.click("#to-project");
    await expect.poll(() => client.url()).toBe(`${origin}/chat/${REPORT}`);
    expect(await page.isVisible("#to-project")).toBe(false);
    expect(await page.textContent("#title")).toBe("Quarterly report");
  });

  it("goes back to the project's conversation when the open thread drops out of the project", async () => {
    const { page, client } = await opened();
    await viewThread(page, QUESTION);
    await expect.poll(() => client.url()).toBe(`${origin}/chat/${QUESTION}`);
    const drop = (data: ProjectFixtures) => {
      data.threads[REPORT] = data.threads[REPORT]!.filter((thread) => thread.id !== QUESTION);
    };
    drop(agent.projects!);
    // The page that serves the projects drops it too, and tells; it may still be loading, so it is told until the centre moves.
    await expect.poll(async () => {
      await client.evaluate(([project, thread]) => {
        const fake = (window as unknown as { fakeProjects?: { data: ProjectFixtures; changed(id: string, threadId: string): void } }).fakeProjects;
        if (!fake) return;
        fake.data.threads[project!] = fake.data.threads[project!]!.filter((found) => found.id !== thread);
        fake.changed(project!, thread!);
      }, [REPORT, QUESTION]).catch(() => {});
      return client.url();
    }).toBe(`${origin}/chat/${REPORT}`);
    expect(await page.isVisible("#to-project")).toBe(false);
    await expect.poll(() => page.textContent('[data-group="waiting"] .count')).toBe("2");
  });

  it("reads only the row a change names, and the project's counts with it", async () => {
    const { page, client } = await opened();
    await client.evaluate(([project, thread]) => {
      const fake = (window as unknown as { fakeProjects: Served }).fakeProjects;
      fake.reads.length = 0;
      fake.lists = 0;
      fake.data.threads[project!]!.find((found) => found.id === thread)!.statusLine = "Merged the regions";
      fake.changed(project!, thread!);
    }, [REPORT, IDLE]);
    await expect.poll(() => page.textContent(`[data-thread="${IDLE}"] .status`)).toBe("Idle · Merged the regions");
    const fake = await client.evaluate(() => {
      const { reads, lists } = (window as unknown as { fakeProjects: Served }).fakeProjects;
      return { reads, lists };
    });
    expect(fake).toEqual({ reads: [IDLE], lists: 1 });
  });

  it("reads the Library again when a thread's change brings it a file, and not when it brings none", async () => {
    const { page, client } = await opened();
    await page.click('[data-tab="library"]');
    await client.evaluate(([project, thread]) => {
      const fake = (window as unknown as { fakeProjects: Served }).fakeProjects;
      const row = fake.data.threads[project!]!.find((found) => found.id === thread)!;
      row.files = [...row.files, { kind: "file", label: "west.csv", ref: "threads/sales/west.csv", threadId: thread! }];
      fake.data.library[project!] = [...fake.data.library[project!]!, {
        path: "threads/sales/west.csv", origin: "produced", threadId: thread!, size: 1024, updatedAt: new Date().toISOString(), place: { kind: "cloud" },
      }];
      fake.changed(project!, thread!);
    }, [REPORT, IDLE]);
    await expect.poll(() => texts(page, "#files .path")).toContain("threads/sales/west.csv");
    // A change that brings no file leaves the Library as it was read.
    await client.evaluate(([project, thread]) => {
      const fake = (window as unknown as { fakeProjects: Served }).fakeProjects;
      fake.data.library[project!] = [];
      fake.data.threads[project!]!.find((found) => found.id === thread)!.statusLine = "Merged the regions";
      fake.changed(project!, thread!);
    }, [REPORT, IDLE]);
    await expect.poll(() => page.textContent(`[data-thread="${IDLE}"] .status`)).toBe("Idle · Merged the regions");
    expect(await texts(page, "#files .path")).toContain("threads/sales/west.csv");
  });

  it("shows a thread it has not listed yet as one of the project's, as a card's View thread opens it", async () => {
    const { page, client } = await opened();
    const started = "9a8b7c6d-5e4f-4a3b-9c2d-1e0f9a8b7c6d";
    await client.evaluate(([project, id]) => {
      const threads = (window as unknown as { fakeProjects: Served }).fakeProjects.data.threads[project!]!;
      threads.unshift({ ...threads[0]!, id: id!, title: "Summarise B", group: "working", reason: null, statusLine: null });
      history.pushState(null, "", `/chat/${id}`);
    }, [REPORT, started]);
    await expect.poll(() => page.textContent("#title")).toBe("Summarise B");
    expect(await page.textContent("#to-project")).toBe("Quarterly report");
    expect(await page.isVisible(`[data-thread="${started}"]`)).toBe(true);
  });

  it("resolves a thread from its row, and reopens it", async () => {
    const { page } = await opened();
    const act = `[data-act="${IDLE}"]`;
    await page.hover(`[data-thread="${IDLE}"]`);
    expect(await page.getAttribute(act, "aria-label")).toBe("Resolve Collect the sales data");
    await page.click(act);
    await expect.poll(() => texts(page, ".section summary")).toEqual(["Waiting on you 3", "Working 2", "Idle 0", "Resolved 2"]);
    await page.click('[data-group="resolved"] summary');
    await page.hover(`[data-thread="${IDLE}"]`);
    expect(await page.textContent(act)).toBe("Reopen");
    await page.click(act);
    await expect.poll(() => texts(page, ".section summary")).toEqual(["Waiting on you 3", "Working 2", "Idle 1", "Resolved 1"]);
  });

  it("keeps the keyboard on what had it when the window's page draws again: a project in the sidebar, a thread's row", async () => {
    const { shell, page } = await opened();
    const redraw = () => shell.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0]!.webContents.send("shell:changed"));
    for (const selector of [`#projects [data-project="${BUDGET}"] .project`, `[data-thread="${QUESTION}"]`]) {
      await page.focus(selector);
      const before = await page.evaluate(() => (document.activeElement as HTMLElement).outerHTML);
      await redraw();
      // Drawn anew: the element is another, and the keyboard is on it.
      await expect.poll(() => page.evaluate((chosen) => document.activeElement === document.querySelector(chosen), selector)).toBe(true);
      expect(await page.evaluate(() => (document.activeElement as HTMLElement).outerHTML)).toBe(before);
    }
  });

  it("gives the keyboard, after a Resolve or Reopen moves its row, to the row that took its place, or to where the row went", async () => {
    const { page } = await opened();
    const focused = () => page.evaluate(() => (document.activeElement as HTMLElement | null)?.dataset.focus ?? null);
    // The second of three waiting threads, resolved from the keyboard: the third takes its place.
    await page.focus(`[data-act="${APPROVAL}"]`);
    await page.keyboard.press("Enter");
    await expect.poll(() => texts(page, ".section summary")).toEqual(["Waiting on you 2", "Working 2", "Idle 1", "Resolved 2"]);
    await expect.poll(focused).toBe(`act:${FAILED}`);
    // The only idle thread: its group goes, and the folded group it went to has the keyboard.
    await page.focus(`[data-act="${IDLE}"]`);
    await page.keyboard.press("Enter");
    await expect.poll(() => texts(page, ".section summary")).toEqual(["Waiting on you 2", "Working 2", "Idle 0", "Resolved 3"]);
    await expect.poll(focused).toBe("summary:resolved");
    // Reopened from the Resolved group: the next resolved row takes its place.
    await page.keyboard.press("Enter");
    await page.focus(`[data-act="${IDLE}"]`);
    await page.keyboard.press("Enter");
    await expect.poll(() => texts(page, ".section summary")).toEqual(["Waiting on you 2", "Working 2", "Idle 1", "Resolved 2"]);
    await expect.poll(focused).toBe(`act:${RESOLVED}`);
  });

  it("shows a row's Resolve or Reopen in its age's place, over nothing else of the row", async () => {
    // Long chips, which fill their line to the row's end.
    const idle = agent.projects!.threads[REPORT]!.find((thread) => thread.id === IDLE)!;
    idle.files = idle.files.map((file) => ({ ...file, label: `${"regional_sales_".repeat(4)}${file.label}` }));
    const { page } = await opened();
    await page.click('[data-group="resolved"] summary');
    const covered: string[] = [];
    for (const id of await page.$$eval(".section .thread", (found) => found.map((row) => (row as HTMLElement).dataset.thread!))) {
      await page.hover(`[data-thread="${id}"]`);
      covered.push(...await page.evaluate((thread) => {
        const act = document.querySelector(`[data-act="${thread}"]`)!.getBoundingClientRect();
        const hit = (box: DOMRect) => box.left < act.right && act.left < box.right && box.top < act.bottom && act.top < box.bottom;
        return [...document.querySelectorAll(`[data-thread="${thread}"] :is(.title, .status, .chip, .progress, .age)`)]
          .filter((part) => getComputedStyle(part).visibility !== "hidden" && hit(part.getBoundingClientRect()))
          .map((part) => `${part.className} of ${thread}`);
      }, id));
    }
    expect(covered).toEqual([]);
  });

  it("says why a thread was not resolved", async () => {
    const { page, client } = await opened();
    await client.evaluate(() => {
      (window as unknown as { fakeProjects: Served }).fakeProjects.refusal = "No such thread.";
    });
    await page.hover(`[data-thread="${IDLE}"]`);
    await page.click(`[data-act="${IDLE}"]`);
    await expect.poll(() => page.textContent("#failure")).toBe("No such thread.");
    expect(await page.isVisible(`[data-group="idle"] [data-thread="${IDLE}"]`)).toBe(true);
  });

  it("still opens the project clicked while the page reloads when a row is resolved meanwhile", async () => {
    agent.registerAfterMs = 1_500;
    const { page, client } = await opened();
    // The page loads again and serves after a second and a half: Budget's open waits for it.
    await client.reload();
    await page.click(`#projects [data-project="${BUDGET}"] .project`);
    await page.hover(`[data-thread="${IDLE}"]`);
    await page.click(`[data-act="${IDLE}"]`);
    await expect.poll(() => client.url(), { timeout: 8_000 }).toBe(`${origin}/chat/${BUDGET}`);
    await expect.poll(() => page.textContent("#title")).toBe("Budget");
  });

  it("folds away with the Overview button, and the close button, and comes back", async () => {
    const { page } = await opened();
    await page.click("#overview");
    expect(await page.isVisible("#panel")).toBe(false);
    expect(await page.getAttribute("#overview", "aria-pressed")).toBe("false");
    await page.click("#overview");
    expect(await page.isVisible("#panel")).toBe(true);
    // Closed from the keyboard, the pane gives it to the button that opens it again.
    await page.focus("#close-panel");
    await page.keyboard.press("Enter");
    expect(await page.isVisible("#panel")).toBe(false);
    expect(await page.getAttribute("#overview", "aria-pressed")).toBe("false");
    expect(await page.evaluate(() => document.activeElement?.id)).toBe("overview");
  });
});

describe("a thread read in the Overview pane", () => {
  it("is its transcript, from a page of the web client's own with no bridge, beside the project's conversation", async () => {
    const { shell, page, client } = await opened();
    await page.click(`[data-thread="${QUESTION}"]`);
    await expect.poll(async () => (await pane(shell))?.url).toBe(transcript(QUESTION));
    expect(await page.textContent("#reading-title")).toBe("Check the revenue figures");
    expect(await page.isVisible("#threads")).toBe(false);
    expect(await page.isVisible(".tabs")).toBe(false);
    expect(client.url()).toBe(`${origin}/chat/${REPORT}`);
    expect(await page.textContent("#title")).toBe("Quarterly report");
    await expect.poll(async () => (await pane(shell))?.bounds).toEqual(await hole(page));
    const reader = shell.windows().find((found) => found.url().includes("/transcript/"))!;
    expect(await reader.evaluate(() => "surogateDesktop" in window)).toBe(false);
    // Folded away, the pane's transcript goes with it.
    await page.click("#overview");
    await expect.poll(async () => (await pane(shell))?.bounds[2]).toBe(0);
  });

  it("stays over its hole as the window is resized, which moves the pane at its widest without resizing it", async () => {
    const { shell, page } = await opened();
    await page.click(`[data-thread="${QUESTION}"]`);
    await expect.poll(async () => (await pane(shell))?.url).toBe(transcript(QUESTION));
    for (const width of [2400, 2200]) {
      await shell.evaluate(({ BrowserWindow }, wide) => BrowserWindow.getAllWindows()[0]!.setBounds({ width: wide, height: 900 }), width);
      await expect.poll(() => page.evaluate(() => window.innerWidth)).toBe(width);
      await expect.poll(async () => (await pane(shell))?.bounds).toEqual(await hole(page));
    }
  });

  it("goes with Back, with another project, and with the account", async () => {
    const { shell, page, client } = await opened();
    await page.click(`[data-thread="${QUESTION}"]`);
    await expect.poll(async () => (await pane(shell))?.url).toBe(transcript(QUESTION));
    await page.click("#reading-back");
    await expect.poll(() => pane(shell)).toBeNull();
    expect(await page.isVisible("#threads")).toBe(true);
    await page.click(`[data-thread="${QUESTION}"]`);
    await expect.poll(async () => (await pane(shell))?.url).toBe(transcript(QUESTION));
    await page.click(`#projects [data-project="${BUDGET}"] .project`);
    await expect.poll(() => pane(shell)).toBeNull();
    await page.click(`#projects [data-project="${REPORT}"] .project`);
    await page.click(`[data-thread="${IDLE}"]`);
    await expect.poll(async () => (await pane(shell))?.url).toBe(transcript(IDLE));
    await client.evaluate(() => window.surogateDesktop!.setAccount(null));
    await expect.poll(() => pane(shell)).toBeNull();
  });

  it("goes as the centre shows its thread, from a card's View thread or a notification's click", async () => {
    const { shell, page, client } = await opened();
    await page.click(`[data-thread="${QUESTION}"]`);
    await expect.poll(async () => (await pane(shell))?.url).toBe(transcript(QUESTION));
    // A thread card's View thread: the web client routes to the thread in place.
    await client.evaluate((id) => history.pushState(null, "", `/chat/${id}`), QUESTION);
    await expect.poll(() => page.textContent("#title")).toBe("Check the revenue figures");
    await expect.poll(() => pane(shell)).toBeNull();
    await page.click("#to-project");
    await expect.poll(() => client.url()).toBe(`${origin}/chat/${REPORT}`);
    await page.click(`[data-thread="${QUESTION}"]`);
    await expect.poll(async () => (await pane(shell))?.url).toBe(transcript(QUESTION));
    // A notification's click: the centre loads the thread's page.
    await page.evaluate((path) => (window as unknown as { surogateShell: { go(path: string): Promise<void> } }).surogateShell.go(path),
      `/chat/${QUESTION}`);
    await expect.poll(() => client.url()).toBe(`${origin}/chat/${QUESTION}`);
    await expect.poll(() => pane(shell)).toBeNull();
  });

  it("takes the keyboard to its Back as it opens, and back to the thread's row as it closes", async () => {
    const { shell, page } = await opened();
    await page.focus(`[data-thread="${QUESTION}"]`);
    await page.keyboard.press("Enter");
    await expect.poll(async () => (await pane(shell))?.url).toBe(transcript(QUESTION));
    await expect.poll(() => page.evaluate(() => document.activeElement?.id)).toBe("reading-back");
    await page.keyboard.press("Enter");
    await expect.poll(() => pane(shell)).toBeNull();
    await expect.poll(() => page.evaluate(() => (document.activeElement as HTMLElement | null)?.dataset.thread)).toBe(QUESTION);
  });

  it("takes the keyboard into its transcript after its head, to scroll it there, and out only at its edges", async () => {
    const { shell, page } = await opened();
    await page.focus(`[data-thread="${QUESTION}"]`);
    await page.keyboard.press("Enter");
    await expect.poll(async () => (await pane(shell))?.url).toBe(transcript(QUESTION));
    const reader = shell.windows().find((found) => found.url().includes("/transcript/"))!;
    await reader.waitForLoadState();
    // Two controls of its own, and room to scroll.
    await reader.evaluate(() => {
      document.body.insertAdjacentHTML("afterbegin", '<button id="one">One</button><button id="two">Two</button>');
      document.body.style.height = "5000px";
    });
    // Which has the keyboard, as the app's own process sees it: the window's page, and the pane's transcript.
    const keyboardIn = () => shell.evaluate(({ BrowserWindow }) => {
      const window = BrowserWindow.getAllWindows()[0]!;
      const reading = (window.contentView.children as Electron.WebContentsView[]).find((view) => view.webContents.getURL().includes("/transcript/"));
      return [window.webContents.isFocused(), reading?.webContents.isFocused() ?? false];
    });
    // A key the user presses in the transcript, as the window's input reaches it.
    const pressed = (keyCode: string, modifiers: Array<"shift"> = []) => shell.evaluate(({ BrowserWindow }, [code, held]) => {
      const reading = (BrowserWindow.getAllWindows()[0]!.contentView.children as Electron.WebContentsView[])
        .find((view) => view.webContents.getURL().includes("/transcript/"))!;
      for (const type of ["keyDown", "keyUp"] as const) reading.webContents.sendInputEvent({ type, keyCode: code as string, modifiers: held as Array<"shift"> });
    }, [keyCode, modifiers] as const);
    const focused = () => reader.evaluate(() => document.activeElement?.id ?? "");
    const inShell = () => page.evaluate(() => document.activeElement?.id);
    await expect.poll(inShell).toBe("reading-back");
    await page.keyboard.press("Tab");
    expect(await inShell()).toBe("reading-open");
    await page.keyboard.press("Tab");
    await expect.poll(keyboardIn).toEqual([false, true]);
    await reader.keyboard.press("PageDown");
    await expect.poll(() => reader.evaluate(() => window.scrollY)).toBeGreaterThan(0);
    const paged = await reader.evaluate(() => window.scrollY);
    await reader.keyboard.press("ArrowDown");
    await expect.poll(() => reader.evaluate(() => window.scrollY)).toBeGreaterThan(paged);
    // Shift+Tab from a later control steps back inside the page; from its first, out to the head's Open.
    await reader.evaluate(() => document.getElementById("two")!.focus());
    await pressed("Tab", ["shift"]);
    await expect.poll(focused).toBe("one");
    expect(await keyboardIn()).toEqual([false, true]);
    await pressed("Tab", ["shift"]);
    await expect.poll(keyboardIn).toEqual([true, false]);
    expect(await inShell()).toBe("reading-open");
    await page.keyboard.press("Tab");
    await expect.poll(keyboardIn).toEqual([false, true]);
    // Escape closes the page's dialog, which takes the key as a Radix dialog does, and only that: gone at once here,
    // its taking the key is all that keeps it the page's.
    await reader.evaluate(() => {
      document.body.insertAdjacentHTML("beforeend", '<div role="dialog" id="dialog">A dialog</div>');
      document.addEventListener("keydown", (event) => {
        if (event.key !== "Escape" || !document.getElementById("dialog")) return;
        event.preventDefault();
        document.getElementById("dialog")!.remove();
      }, { capture: true });
    });
    await pressed("Escape");
    await expect.poll(() => reader.evaluate(() => document.getElementById("dialog") === null)).toBe(true);
    expect(await keyboardIn()).toEqual([false, true]);
    // A dialog that keeps the key to itself keeps the keyboard in the page too.
    await reader.evaluate(() => document.body.insertAdjacentHTML("beforeend", '<div role="dialog" id="kept">Kept</div>'));
    await pressed("Escape");
    await pause(300);
    expect(await keyboardIn()).toEqual([false, true]);
    await reader.evaluate(() => document.getElementById("kept")!.remove());
    // With nothing open, Escape gives the keyboard back to Back.
    await pressed("Escape");
    await expect.poll(keyboardIn).toEqual([true, false]);
    expect(await inShell()).toBe("reading-back");
  });

  it("is read again as Settings shapes the transcript", async () => {
    const { shell, page } = await opened();
    await page.click(`[data-thread="${QUESTION}"]`);
    await expect.poll(async () => (await pane(shell))?.url).toBe(transcript(QUESTION));
    await page.click("#open-settings");
    let settings: Page | undefined;
    await expect.poll(() => {
      settings = shell.windows().find((found) => found.url().endsWith("/settings.html"));
      return settings !== undefined;
    }).toBe(true);
    await settings!.waitForSelector('[data-setting="textSize"] [data-value="large"]');
    await settings!.click('[data-setting="textSize"] [data-value="large"]');
    await expect.poll(async () => (await pane(shell))?.url)
      .toBe(transcript(QUESTION, "textSize=large&transcriptWidth=medium&motion=system"));
  });

  it("paints its first frame in a dark desktop's theme, with no bridge, before any script of its own", async () => {
    const { shell, page, client } = await opened();
    await page.click("#open-settings");
    let settings: Page | undefined;
    await expect.poll(() => {
      settings = shell.windows().find((found) => found.url().endsWith("/settings.html"));
      return settings !== undefined;
    }).toBe(true);
    await settings!.waitForSelector('[data-setting="theme"] [data-value="dark"]');
    await settings!.click('[data-setting="theme"] [data-value="dark"]');
    await expect.poll(() => client.evaluate(() => matchMedia("(prefers-color-scheme: dark)").matches)).toBe(true);
    // Escape closes the page before Playwright sends the key up.
    await settings!.keyboard.press("Escape").catch(() => {});
    await expect.poll(() => shell.windows().some((found) => found.url().endsWith("/settings.html"))).toBe(false);
    // From now on the agent serves the web client's own HTML, its bundle and the fonts' sheet not: only its
    // head can theme the pane's first frame, and the body's first script reads the root that frame paints from.
    agent.page = readFileSync(join(import.meta.dirname, "..", "..", "..", "web", "index.html"), "utf8")
      .replace(/<link [^>]*https:[^>]*>/g, "")
      .replace("<body>", "<body><script>window.firstFrame = document.documentElement.className</script>");
    await page.click(`[data-thread="${QUESTION}"]`);
    await expect.poll(async () => (await pane(shell))?.url).toBe(transcript(QUESTION));
    const reader = shell.windows().find((found) => found.url().includes("/transcript/"))!;
    await reader.waitForLoadState();
    expect(await reader.evaluate(() => "surogateDesktop" in window)).toBe(false);
    expect(await reader.evaluate(() => (window as unknown as { firstFrame: string }).firstFrame)).toBe("dark");
  });

  it("stays on its transcript, and sends an address outside the agent's to the browser", async () => {
    const { shell, page } = await opened();
    await page.click(`[data-thread="${QUESTION}"]`);
    await expect.poll(async () => (await pane(shell))?.url).toBe(transcript(QUESTION));
    const reader = shell.windows().find((found) => found.url().includes("/transcript/"))!;
    await reader.waitForLoadState();
    const where = () => reader.evaluate(() => location.href + String((window as unknown as { kept?: boolean }).kept ?? false))
      .catch(() => "");
    // A footnote of its own: the pane scrolls to it, and is not loaded again.
    await reader.evaluate(() => {
      (window as unknown as { kept: boolean }).kept = true;
      location.hash = "note";
    });
    await pause(500);
    expect(await where()).toBe(`${transcript(QUESTION)}#notetrue`);
    // A page of the agent's it would load in its place: the pane stays where it was.
    await reader.evaluate(() => {
      location.href = "/settings";
    }).catch(() => {});
    await pause(500);
    expect(await where()).toBe(`${transcript(QUESTION)}#notetrue`);
    // A page of its own the web client would route to in place: the pane loads its transcript again.
    await reader.evaluate(() => {
      (window as unknown as { kept: boolean }).kept = true;
      history.pushState(null, "", "/chat");
    });
    await expect.poll(where).toBe(`${transcript(QUESTION)}false`);
    await reader.evaluate(() => {
      location.href = "https://example.com/report";
    }).catch(() => {});
    await expect.poll(() => shell.evaluate(() => (globalThis as unknown as { opened: string[] }).opened)).toContain("https://example.com/report");
    expect((await pane(shell))?.url).toBe(transcript(QUESTION));
    // Its router's own rewrite of its address, the query left out: still its transcript, not loaded again.
    await reader.evaluate(() => {
      (window as unknown as { kept: boolean }).kept = true;
      history.replaceState(null, "", location.pathname);
    });
    await pause(500);
    expect(await where()).toBe(`${origin}/transcript/${QUESTION}true`);
  });
});

describe("a thread read in the Overview pane, as the agent answers", () => {
  it("is loaded again once the agent answers after its load failed, and after its page crashed", async () => {
    const { shell, page } = await opened();
    // *code* run in the pane's page, while one is there to answer: null while none is, as when it crashes.
    const inPane = (code: string) => shell.evaluate(({ BrowserWindow }, script) => {
      const found = (BrowserWindow.getAllWindows()[0]!.contentView.children as Electron.WebContentsView[])
        .find((view) => view.webContents.getURL().includes("/transcript/"));
      if (!found || found.webContents.isCrashed()) return null;
      return Promise.race([found.webContents.executeJavaScript(script), new Promise((resolve) => setTimeout(() => resolve(null), 500))]);
    }, code);
    await agent.stop();
    await page.click(`[data-thread="${QUESTION}"]`);
    await pause(1_000);
    expect(await inPane("document.title")).not.toBe("Fake agent");
    await agent.start(Number(new URL(origin).port));
    await expect.poll(() => inPane("document.title"), { timeout: 10_000 }).toBe("Fake agent");
    await inPane("window.kept = true");
    // Its page's process ends, as a crash ends it.
    await shell.evaluate(({ BrowserWindow }) => process.kill((BrowserWindow.getAllWindows()[0]!.contentView.children as Electron.WebContentsView[])
      .find((view) => view.webContents.getURL().includes("/transcript/"))!.webContents.getOSProcessId(), "SIGKILL"));
    await expect.poll(() => inPane("[document.title, window.kept === true]"), { timeout: 10_000 }).toEqual(["Fake agent", false]);
  });

  it("follows a redirect of its load only to its own transcript", async () => {
    const { shell, page } = await opened();
    const shown = () => shell.evaluate(({ BrowserWindow }) =>
      (BrowserWindow.getAllWindows()[0]!.contentView.children as Electron.WebContentsView[]).map((view) => view.webContents.getURL()));
    // To another page of the agent's: refused, and that page is drawn nowhere.
    agent.pagesRedirect = `${origin}/settings`;
    await page.click(`[data-thread="${QUESTION}"]`);
    await pause(1_000);
    expect(await shown()).not.toContain(`${origin}/settings`);
    await page.click("#reading-back");
    await expect.poll(() => page.isVisible("#threads")).toBe(true);
    // To its own transcript, in other settings: followed.
    agent.pagesRedirect = transcript(QUESTION, "textSize=large&transcriptWidth=medium&motion=system");
    await page.click(`[data-thread="${QUESTION}"]`);
    await expect.poll(async () => (await pane(shell))?.url).toBe(agent.pagesRedirect);
  });
});

describe("the Overview pane, at its edges", () => {
  it("forgets a project's threads when another account signs in, or the user signs out", async () => {
    const { page, client } = await opened();
    await client.evaluate((account) => window.surogateDesktop!.setAccount(account), { ...ACCOUNT, userId: "someone-else" });
    await expect.poll(() => rows(page)).toBe(0);
    await client.evaluate(() => window.surogateDesktop!.registerProjects(null));
    await expect.poll(() => page.textContent("#greeting-line")).toBe("Open a project to see its threads.");
    expect(await rows(page)).toBe(0);
    // The next account's page serves projects of its own.
    agent.projects!.projects = agent.projects!.projects.filter((project) => project.id !== REPORT);
    await client.reload();
    await page.waitForSelector(`#projects [data-project="${BUDGET}"]`);
    expect(await page.$$eval("#projects .project", (found) => found.length)).toBe(1);
    expect(await rows(page)).toBe(0);
  });

  it("leaves the open project once the page lists it no more", async () => {
    const { page, client } = await opened();
    agent.projects!.projects = agent.projects!.projects.filter((project) => project.id !== REPORT);
    await client.reload();
    await expect.poll(() => page.textContent("#title")).toBe(new URL(origin).host);
    expect(await rows(page)).toBe(0);
    expect(await page.textContent("#greeting-line")).toBe("Open a project to see its threads.");
  });

  it("leaves the project at once for a plain chat loaded in full, while its page serves nothing yet", async () => {
    const { page, client } = await opened();
    // The chat's page serves nothing: the shell cannot ask it whether the chat is one of the project's threads.
    agent.registerAfterMs = -1;
    const plain = "7e6d5c4b-3a29-4180-9f7e-6d5c4b3a2918";
    await client.evaluate((path) => {
      location.href = path;
    }, `/chat/${plain}`).catch(() => {});
    await expect.poll(() => client.url()).toBe(`${origin}/chat/${plain}`);
    await expect.poll(() => page.textContent("#title"), { timeout: 2_000 }).toBe(new URL(origin).host);
  });

  it("takes the centre to a new chat when the open project is archived elsewhere", async () => {
    const { page, client } = await opened();
    await viewThread(page, QUESTION);
    await expect.poll(() => client.url()).toBe(`${origin}/chat/${QUESTION}`);
    // Archived on another device: the page lists it no more, and says the project changed.
    await expect.poll(async () => {
      await client.evaluate((project) => {
        const fake = (window as unknown as { fakeProjects?: Served }).fakeProjects;
        if (!fake) return;
        fake.data.projects = fake.data.projects.filter((found) => found.id !== project);
        fake.changed(project, null);
      }, REPORT).catch(() => {});
      return page.textContent("#title");
    }).toBe(new URL(origin).host);
    await expect.poll(() => client.url()).toBe(`${origin}/chat`);
  });

  it("opens only a thread of the project that is open, and says why it opened none", async () => {
    const { page, client } = await opened();
    const outcome = await page.evaluate(async ([budget, question]) => {
      const shell = (window as unknown as { surogateShell: { project(id: string): Promise<void>; thread(id: string): Promise<void> } }).surogateShell;
      await shell.project(budget!);
      return shell.thread(question!).then(() => "answered", () => "rejected");
    }, [BUDGET, QUESTION]);
    expect(outcome).toBe("answered");
    await expect.poll(() => page.textContent("#failure")).toBe("No such thread in the open project");
    expect(client.url()).not.toBe(`${origin}/chat/${QUESTION}`);
  });

  it("says why a thread that has just left the pane was not resolved, reopened or opened", async () => {
    const { page } = await opened();
    const shell = (how: "resolve" | "reopen" | "thread") => page.evaluate((call) => {
      const calls = (window as unknown as { surogateShell: Record<string, (id: string) => Promise<void>> }).surogateShell;
      return calls[call]!("9a8b7c6d-5e4f-4a3b-9c2d-1e0f9a8b7c6d").then(() => "answered", () => "rejected");
    }, how);
    for (const how of ["resolve", "reopen", "thread"] as const) {
      // Forward, with nothing to go forward to, draws the last failure away.
      await page.click("#forward");
      await expect.poll(() => page.isVisible("#failure")).toBe(false);
      expect(await shell(how), how).toBe("answered");
      await expect.poll(() => page.textContent("#failure")).toBe("No such thread in the open project");
    }
  });

  it("reads everything again after a read that failed, so the change it lost is not lost for good", async () => {
    const { page, client } = await opened();
    const lists = () => client.evaluate(() => (window as unknown as { fakeProjects: Served }).fakeProjects.lists);
    const before = await lists();
    // The agent is out of reach while the first change is told: its read fails.
    await client.evaluate(([project, thread]) => {
      const fake = (window as unknown as { fakeProjects: Served }).fakeProjects;
      fake.unreachable = true;
      fake.data.threads[project!]!.find((found) => found.id === thread)!.statusLine = "Merged the regions";
      fake.changed(project!, thread!);
    }, [REPORT, IDLE]);
    await expect.poll(lists).toBeGreaterThan(before);
    // Back in reach, a change of another thread is told.
    await client.evaluate(([project, thread]) => {
      const fake = (window as unknown as { fakeProjects: Served }).fakeProjects;
      fake.unreachable = false;
      fake.data.threads[project!]!.find((found) => found.id === thread)!.statusLine = "Asked which rate to use";
      fake.changed(project!, thread!);
    }, [REPORT, QUESTION]);
    await expect.poll(() => page.textContent(`[data-thread="${QUESTION}"] .status`)).toBe("Question · Asked which rate to use");
    await expect.poll(() => page.textContent(`[data-thread="${IDLE}"] .status`)).toBe("Idle · Merged the regions");
  });

  it("asks the page again once, not once a change, when changes come together", async () => {
    const { client } = await opened();
    await client.evaluate((project) => {
      const fake = (window as unknown as { fakeProjects: Served }).fakeProjects;
      fake.lists = 0;
      for (let count = 0; count < 5; count++) fake.changed(project, null);
    }, REPORT);
    await pause(1_000);
    const lists = await client.evaluate(() => (window as unknown as { fakeProjects: Served }).fakeProjects.lists);
    expect(lists).toBeGreaterThanOrEqual(1);
    expect(lists).toBeLessThanOrEqual(2);
  });

  it("keeps a long file name in the pane, and draws only the groups that have threads", async () => {
    agent.projects!.threads[REPORT]![0]!.files[0]!.label = `${"quarterly_revenue_".repeat(6)}.xlsx`;
    const { page } = await opened();
    const [chip, pane] = await page.evaluate((question) => [
      document.querySelector(`[data-thread="${question}"] .chip`)!.getBoundingClientRect().right,
      document.querySelector("#panel")!.getBoundingClientRect().right,
    ], QUESTION);
    expect(chip).toBeLessThanOrEqual(pane!);
    // Waiting on you says what it holds only while it holds nothing.
    expect(await page.isVisible('[data-group="waiting"] > .desc')).toBe(false);
    await page.click(`#projects [data-project="${BUDGET}"] .project`);
    await expect.poll(() => page.textContent("#greeting-line")).toBe("Nothing is waiting on you.");
    expect(await page.isVisible('[data-group="waiting"]')).toBe(true);
    expect(await page.isVisible('[data-group="waiting"] > .desc')).toBe(true);
    for (const group of ["working", "idle", "resolved"]) expect(await page.isVisible(`[data-group="${group}"]`)).toBe(false);
    expect(await page.isVisible("#thread-count")).toBe(false);
  });

  it("keeps the header in step with a thread that is renamed", async () => {
    const { page, client } = await opened();
    await viewThread(page, QUESTION);
    await expect.poll(() => client.url()).toBe(`${origin}/chat/${QUESTION}`);
    await expect.poll(async () => {
      await client.evaluate(([project, thread]) => {
        const fake = (window as unknown as { fakeProjects?: Served }).fakeProjects;
        const found = fake?.data.threads[project!]?.find((each) => each.id === thread);
        if (!found) return;
        found.title = "Check the third quarter's revenue";
        fake!.changed(project!, thread!);
      }, [REPORT, QUESTION]).catch(() => {});
      return page.textContent("#title");
    }).toBe("Check the third quarter's revenue");
  });

  it("keeps a transcript's title in the pane in step with a thread that is renamed", async () => {
    const { page, client } = await opened();
    await page.click(`[data-thread="${QUESTION}"]`);
    await expect.poll(() => page.textContent("#reading-title")).toBe("Check the revenue figures");
    await client.evaluate(([project, thread]) => {
      const fake = (window as unknown as { fakeProjects: Served }).fakeProjects;
      fake.data.threads[project!]!.find((each) => each.id === thread)!.title = "Check the third quarter's revenue";
      fake.changed(project!, thread!);
    }, [REPORT, QUESTION]);
    await expect.poll(() => page.textContent("#reading-title")).toBe("Check the third quarter's revenue");
  });

  it("draws away a read's refusal once a thread is read", async () => {
    const { shell, page } = await opened();
    await page.evaluate((id) => (window as unknown as { surogateShell: { read(id: string): Promise<void> } }).surogateShell.read(id),
      "9a8b7c6d-5e4f-4a3b-9c2d-1e0f9a8b7c6d");
    await expect.poll(() => page.textContent("#failure")).toBe("No such thread in the open project");
    await page.click(`[data-thread="${QUESTION}"]`);
    await expect.poll(async () => (await pane(shell))?.url).toBe(transcript(QUESTION));
    await expect.poll(() => page.isVisible("#failure")).toBe(false);
  });

  it("never loads a master session that is not a chat when a thread leaves", async () => {
    const { page, client } = await opened();
    await viewThread(page, QUESTION);
    await expect.poll(() => client.url()).toBe(`${origin}/chat/${QUESTION}`);
    // The thread's page serves the projects and the shell has read them: the change below is read
    // after that read, never across it.
    await expect.poll(() => client.evaluate(() => (window as unknown as { fakeProjects?: Served }).fakeProjects?.reads.length ?? 0)
      .catch(() => 0)).toBeGreaterThan(0);
    await client.evaluate(([project, thread]) => {
      const fake = (window as unknown as { fakeProjects: Served }).fakeProjects;
      fake.data.projects.find((found) => found.id === project)!.masterSessionId = "not-a-chat";
      fake.data.threads[project!] = fake.data.threads[project!]!.filter((found) => found.id !== thread);
      // Project-wide: the project itself is read again, with its new master.
      fake.changed(project!, null);
    }, [REPORT, QUESTION]);
    await expect.poll(() => page.textContent('[data-group="waiting"] .count')).toBe("2");
    await pause(500);
    expect(client.url()).toBe(`${origin}/chat/${QUESTION}`);
  });

  it("says a project is loading until its threads come, and greets a user with no name plainly", async () => {
    agent.registerAfterMs = 1_500;
    const { page, client } = await opened();
    await page.click(`#projects [data-project="${BUDGET}"] .project`);
    await expect.poll(() => page.textContent("#greeting-line")).toBe("Loading the project's threads…");
    await expect.poll(() => page.textContent("#greeting-line"), { timeout: 8_000 }).toBe("Nothing is waiting on you.");
    await client.evaluate((account) => window.surogateDesktop!.setAccount(account), { ...ACCOUNT, name: "" });
    await expect.poll(() => page.textContent("#greeting")).toBe("Welcome back.");
  });
});

describe("an account's projects", () => {
  const OTHER = { name: "Bea Other", email: "bea@example.com", userId: "b", orgId: "o" };
  const others = (): ProjectFixtures => {
    const data = projectFixtures();
    data.projects = data.projects.filter((project) => project.id !== REPORT);
    return data;
  };

  it("are gone, with the account, once its session expires, and come back for no one else", async () => {
    const { page, client } = await opened();
    await expect.poll(() => page.textContent("#user-name")).toBe(ACCOUNT.name);
    // The session expires: the web client goes to its sign-in page, which serves nothing and,
    // having no sign-in, tells the desktop that nobody is signed in (leaveDesktop).
    agent.projects = null;
    await client.evaluate(() => {
      location.href = "/login";
    }).catch(() => {});
    await expect.poll(() => client.url()).toBe(`${origin}/login`);
    await client.waitForLoadState();
    await client.evaluate(async () => {
      await window.surogateDesktop!.setAccount(null);
      await window.surogateDesktop!.registerProjects(null);
    });
    await expect.poll(() => page.textContent("#user-name")).toBe("Not signed in");
    expect(await page.textContent("#user-email")).toBe("Signing in…");
    expect(await page.textContent("#title")).toBe(new URL(origin).host);
    expect(await page.$$eval("#projects .project", (found) => found.length)).toBe(0);
    expect(await rows(page)).toBe(0);
    // Another account signs in, and the web client goes back to the chat the first had open.
    agent.projects = others();
    await client.evaluate((path) => {
      location.href = path;
    }, `/chat/${REPORT}`).catch(() => {});
    await expect.poll(() => client.url()).toBe(`${origin}/chat/${REPORT}`);
    await client.waitForLoadState();
    await client.evaluate((account) => window.surogateDesktop!.setAccount(account), OTHER);
    await page.waitForSelector(`#projects [data-project="${BUDGET}"]`);
    expect(await page.textContent("#title")).toBe(new URL(origin).host);
    expect(await page.isVisible(`#projects [data-project="${REPORT}"]`)).toBe(false);
    expect(await rows(page)).toBe(0);
    expect(await page.textContent("#user-name")).toBe(OTHER.name);
  });

  it("leave the next account no failure line of theirs", async () => {
    const { page, client } = await opened();
    await client.evaluate(() => {
      (window as unknown as { fakeProjects: Served }).fakeProjects.refusal = "No such thread.";
    });
    await page.hover(`[data-thread="${IDLE}"]`);
    await page.click(`[data-act="${IDLE}"]`);
    await expect.poll(() => page.textContent("#failure")).toBe("No such thread.");
    await client.evaluate((account) => window.surogateDesktop!.setAccount(account), OTHER);
    await expect.poll(() => page.textContent("#user-name")).toBe(OTHER.name);
    expect(await page.isVisible("#failure")).toBe(false);
  });

  it("are forgotten, the open one with them, when another account signs in on the same page", async () => {
    const { page, client } = await opened();
    await client.evaluate(([project, account]) => {
      const fake = (window as unknown as { fakeProjects: Served }).fakeProjects;
      fake.data.projects = fake.data.projects.filter((found) => found.id !== project);
      return window.surogateDesktop!.setAccount(account);
    }, [REPORT, OTHER] as const);
    await expect.poll(() => page.textContent("#title")).toBe(new URL(origin).host);
    expect(await page.$$eval('[aria-current="page"]', (found) => found.length)).toBe(0);
    expect(await rows(page)).toBe(0);
    await expect.poll(() => page.isVisible(`#projects [data-project="${REPORT}"]`)).toBe(false);
    await page.waitForSelector(`#projects [data-project="${BUDGET}"]`);
  });
});
