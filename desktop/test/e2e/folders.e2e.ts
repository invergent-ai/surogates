// Settings → Folders and permissions, through the real app: each folder this computer's chats
// work on, its chats, and what each chat's user allowed it. The chats are bound over the fake
// agent's link, as the agent binds them once its user has accepted the folder sheet.

import { mkdtempSync, realpathSync, rmSync } from "node:fs";

import type { ElectronApplication, Page } from "playwright-core";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { ACCOUNT, connect, FakeAgent, signedInAndAdded, webClient } from "./fake-agent.js";
import { dataHome, launch, press, prompt, quit, shellPage, stubNative } from "./launch.js";

const CHAT = "7d2e0f8a-2b3c-4d5e-9f60-718293a4b5c6";
const OTHER = "8e3f1a9b-3c4d-4e6f-a071-8293a4b5c6d7";
const THIRD = "9f4a2b0c-4d5e-4f70-b182-93a4b5c6d7e8";

let home: string;
let folders: string[];
let agent: FakeAgent;
let origin: string;
let app: ElectronApplication | undefined;

beforeEach(async () => {
  home = dataHome();
  folders = [realpathSync(mkdtempSync("/tmp/sf-")), realpathSync(mkdtempSync("/tmp/sf-"))];
  agent = new FakeAgent();
  origin = await agent.start();
});

afterEach(async () => {
  await quit(app);
  app = undefined;
  await agent.stop();
  await agent.link.stop();
  rmSync(home, { recursive: true, force: true });
  for (const folder of folders) rmSync(folder, { recursive: true, force: true });
});

async function signedIn(): Promise<{ shell: ElectronApplication; page: Page; client: Page }> {
  const shell = await launch(home);
  app = shell;
  await stubNative(shell);
  const page = await shellPage(shell);
  await connect(page, origin);
  await signedInAndAdded(shell, page, agent);
  return { shell, page, client: await webClient(shell, origin) };
}

let next = 0;
// *chat*'s operation as the agent sends it: its id.
function send(kind: string, args: Record<string, unknown>, chat: string): string {
  const id = `op-${(next += 1)}`;
  agent.link.send({
    type: "op", id, session_id: chat, calling_session_id: chat, invocation_id: kind === "bind" ? "bind" : "call",
    ordinal: kind === "bind" ? 0 : 1, kind, args, digest: `d-${id}`,
  });
  return id;
}

async function outcome(id: string): Promise<unknown> {
  const results = () => agent.link.received.filter((frame) => frame.type === "op_result" && frame.id === id);
  await agent.link.until(() => results().length === 1, 20_000);
  agent.link.send({ type: "op_ack", id });
  return results()[0]?.outcome;
}

// *chat* bound to *folder* in *mode*, as its user accepts it in the folder sheet.
async function bound(client: Page, folder: string, chat: string, mode: "free" | "ask"): Promise<void> {
  await app!.evaluate((_electron, chosen) => Object.assign(globalThis, { folder: chosen }), folder);
  const prepared = client.evaluate(() => window.surogateDesktop!.prepareFolder("pick"));
  const sheet = await prompt(app!);
  await sheet.check(`input[value="${mode}"]`);
  await press(sheet, "accept");
  const ready = (await prepared)!;
  expect(await outcome(send("bind", { folder: ready.folder, nonce: ready.nonce }, chat))).toEqual({ ok: null });
}

// Settings, open on Folders and permissions.
async function foldersSettings(shell: ElectronApplication, page: Page): Promise<Page> {
  await page.click("#open-settings");
  let found: Page | undefined;
  await expect.poll(() => {
    found = shell.windows().find((each) => each.url().endsWith("/settings.html"));
    return found !== undefined;
  }).toBe(true);
  await found!.waitForSelector(".settings-nav .item");
  await found!.click('[data-section="folders"]');
  return found!;
}

const texts = (page: Page, selector: string) =>
  page.$$eval(selector, (found) => found.filter((element) => (element as HTMLElement).offsetParent !== null)
    .map((element) => element.textContent?.trim()));

describe("Settings → Folders and permissions", () => {
  it("lists each folder this computer's chats work on, with each chat by its title, as text, and its mode", async () => {
    const { shell, page, client } = await signedIn();
    agent.titles.set(CHAT, "Quarterly report");
    // A title the agent wrote, with a right-to-left override in it: shown as text.
    agent.titles.set(OTHER, "Invoices‮gpj.exe");
    const settings = await foldersSettings(shell, page);
    expect(await texts(settings, "#folders-none")).toEqual(["No chat works on a folder of this computer."]);
    // Escape closes the page before Playwright sends the key up.
    await settings.keyboard.press("Escape").catch(() => {});
    await expect.poll(() => settings.isClosed()).toBe(true);
    await bound(client, folders[0]!, CHAT, "ask");
    await bound(client, folders[0]!, OTHER, "free");
    // A chat the agent gives no title.
    await bound(client, folders[1]!, THIRD, "free");
    const open = await foldersSettings(shell, page);
    await expect.poll(() => texts(open, ".folder-path")).toEqual(folders);
    expect(await texts(open, "#folders .row .label > span:first-child")).toEqual(["Quarterly report", "InvoicesU+202Egpj.exe", "A chat"]);
    expect(await texts(open, "#folders .row .label > .desc")).toEqual(["Asks every time", "Works freely", "Works freely"]);
    expect(await open.isVisible("#folders-none")).toBe(false);
    // A chat is found by its title.
    await open.fill("#settings-search", "quarterly");
    expect(await texts(open, ".settings-nav .item")).toEqual(["Folders and permissions"]);
  });

  it("reads the title of a chat the agent names not yet once each time Settings opens, however often it is drawn", async () => {
    const { shell, page, client } = await signedIn();
    await bound(client, folders[0]!, THIRD, "free");
    const settings = await foldersSettings(shell, page);
    await expect.poll(() => texts(settings, "#folders .row .label > span:first-child")).toEqual(["A chat"]);
    expect(agent.asked.title).toBe(1);
    // Drawn again, as each change of the app's state draws it.
    for (const _ of [1, 2, 3]) {
      await shell.evaluate(({ webContents }) => {
        webContents.getAllWebContents().find((contents) => contents.getURL().endsWith("/settings.html"))!.send("settings:changed");
      });
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
    expect(agent.asked.title).toBe(1);
    // Opened again, it asks once more.
    agent.titles.set(THIRD, "Receipts");
    await settings.keyboard.press("Escape").catch(() => {});
    await expect.poll(() => settings.isClosed()).toBe(true);
    const again = await foldersSettings(shell, page);
    await expect.poll(() => texts(again, "#folders .row .label > span:first-child")).toEqual(["Receipts"]);
    expect(agent.asked.title).toBe(2);
  });

  it("draws a chat again in an open Settings once its mode changes", async () => {
    const { shell, page, client } = await signedIn();
    await bound(client, folders[0]!, CHAT, "free");
    const settings = await foldersSettings(shell, page);
    await expect.poll(() => texts(settings, "#folders .row .label > .desc")).toEqual(["Works freely"]);
    // Switched from the chat's own bar, while Settings is open.
    await client.evaluate((id) => window.surogateDesktop!.setMode(id, "ask"), CHAT);
    await expect.poll(() => texts(settings, "#folders .row .label > .desc")).toEqual(["Asks every time"]);
  });

  it("says this computer's access was revoked once the agent revokes it, and refuses a Take back or a Stop", async () => {
    const { shell, page, client } = await signedIn();
    await bound(client, folders[0]!, CHAT, "free");
    agent.link.close(4403);
    await expect.poll(() => page.textContent("#device-action-text")).toBe("Local access revoked.");
    const settings = await foldersSettings(shell, page);
    await settings.evaluate(() => {
      const rejections: string[] = [];
      Object.assign(window, { rejections });
      window.addEventListener("unhandledrejection", (event) => rejections.push(String(event.reason)));
    });
    // Drawn again, as each change of the app's state draws it.
    await shell.evaluate(({ webContents }) => {
      webContents.getAllWebContents().find((contents) => contents.getURL().endsWith("/settings.html"))!.send("settings:changed");
    });
    await expect.poll(() => settings.textContent("#folders-failed")).toBe("This computer's access to the agent was revoked: restore it from Surogate's window");
    expect(await settings.isVisible("#folders-none")).toBe(false);
    // Each refused as anything Settings does not show is.
    const call = (name: "takeBack" | "stop", what: string) => settings.evaluate(([method, root, other]) =>
      (window as unknown as { surogateSettings: Record<string, (root: string, other: string) => Promise<void>> }).surogateSettings[method]!(root, other)
        .then(() => "done", (error: Error) => error.message), [name, CHAT, what] as const);
    expect(await call("takeBack", "example.com")).toBe("Error invoking remote method 'settings:take-back': Error: This chat cannot reach that host");
    expect(await call("stop", "proc_000000000000")).toBe("Error invoking remote method 'settings:stop': Error: This chat runs no such process");
    expect(await settings.evaluate(() => (window as unknown as { rejections: string[] }).rejections)).toEqual([]);
  });

  it("reads each chat's title afresh once its user has logged out, as for another account", async () => {
    const { shell, page, client } = await signedIn();
    agent.titles.set(CHAT, "Quarterly report");
    await bound(client, folders[0]!, CHAT, "free");
    const settings = await foldersSettings(shell, page);
    await expect.poll(() => texts(settings, "#folders .row .label > span:first-child")).toEqual(["Quarterly report"]);
    await settings.keyboard.press("Escape").catch(() => {});
    await expect.poll(() => settings.isClosed()).toBe(true);
    // Logged out, then back in: this computer is added again, and the chat bound on it again.
    await page.click("#user");
    await page.click('[data-action="logout"]');
    await expect.poll(() => page.isVisible("#sign-in")).toBe(true);
    await signedInAndAdded(shell, page, agent);
    // The web client says who is signed in on it, as it does once its session is in.
    await client.evaluate((account) => window.surogateDesktop!.setAccount(account), ACCOUNT);
    agent.titles.set(CHAT, "Receipts");
    await bound(client, folders[0]!, CHAT, "free");
    const again = await foldersSettings(shell, page);
    await expect.poll(() => texts(again, "#folders .row .label > span:first-child")).toEqual(["Receipts"]);
  });

  it("keeps the keyboard on its Take back or Stop as the list is drawn again, and gives it to the next once its line goes", async () => {
    const { shell, page } = await signedIn();
    const settings = await foldersSettings(shell, page);
    // The list as the main process would answer it, the test's own: a chat with two hosts and a process.
    await shell.evaluate(({ webContents }, folder) => {
      const contents = webContents.getAllWebContents().find((found) => found.getURL().endsWith("/settings.html"))!;
      const chat = { root: "r-1", title: "Quarterly report", mode: "free", hosts: ["example.com", "example.org"], processes: [{ id: "p-1", command: "npm run serve" }] };
      for (const channel of ["settings:folders", "settings:take-back", "settings:stop"]) contents.ipc.removeHandler(channel);
      contents.ipc.handle("settings:folders", () => [{ folder, chats: [chat] }]);
      contents.ipc.handle("settings:take-back", (_event, _root, host) => {
        chat.hosts = chat.hosts.filter((found) => found !== host);
      });
      contents.ipc.handle("settings:stop", (_event, _root, id) => {
        chat.processes = chat.processes.filter((found) => found.id !== id);
      });
      contents.send("settings:changed");
    }, folders[0]!);
    const focused = () => settings.evaluate(() => document.activeElement?.getAttribute("aria-label") ?? document.activeElement?.tagName ?? null);
    await settings.focus('[aria-label="Take back example.org"]');
    // Drawn again, as each change of the app's state draws it: the button is a new one, with the keyboard.
    await settings.evaluate(() => Object.assign(document.activeElement!, { drawnBefore: true }));
    await shell.evaluate(({ webContents }) => {
      webContents.getAllWebContents().find((found) => found.getURL().endsWith("/settings.html"))!.send("settings:changed");
    });
    await expect.poll(() => settings.evaluate(() => !("drawnBefore" in document.activeElement!))).toBe(true);
    expect(await focused()).toBe("Take back example.org");
    // Taken back, with Enter held: the line under it takes its place, and the key's repeats stop nothing.
    await settings.keyboard.down("Enter");
    await expect.poll(focused).toBe("Stop npm run serve");
    await settings.keyboard.down("Enter");
    await settings.keyboard.down("Enter");
    await settings.keyboard.up("Enter");
    await new Promise((resolve) => setTimeout(resolve, 500));
    expect(await focused()).toBe("Stop npm run serve");
    // The last line's place is the one before it.
    await settings.keyboard.press("Enter");
    await expect.poll(focused).toBe("Take back example.com");
    // Nothing left to take back or stop: the section's heading has the keyboard.
    await settings.keyboard.press("Enter");
    await expect.poll(focused).toBe("H2");
  });
});
