// The demo's thread runs its commands in the VM, as the app does: behind
// SUROGATE_VM_TESTS=1, with KVM, the image and npm run agent-disk.

import { mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import type { ElectronApplication, Page } from "playwright-core";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { OperationJournal } from "../../src/journal/journal.js";
import { APP_CLOSED } from "../../src/operations/runner.js";
import { connect, FakeAgent, signedInAndAdded, webClient } from "./fake-agent.js";
import { dataHome, launch, press, prompt, quit, shellPage, stubNative, trayLabels, watchTray } from "./launch.js";

const THREAD = "6c1e9f7d-1a2b-4c3d-8e4f-5a6b7c8d9e0f";

let home: string;
let folder: string;
let agent: FakeAgent;
let origin: string;
let app: ElectronApplication | undefined;

beforeEach(async () => {
  home = dataHome();
  // Outside the app's state root, which no chat's folder may be.
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

let counted = 0;
const asked = (shell: ElectronApplication) =>
  shell.evaluate(() => (globalThis as unknown as { asked: Array<{ message: string; detail: string }> }).asked);
const answer = (shell: ElectronApplication, button: number) => shell.evaluate((_electron, chosen) => {
  Object.assign(globalThis, { answer: chosen });
}, button);
const quitApp = (shell: ElectronApplication) => void shell.evaluate(({ app: electron }) => electron.quit()).catch(() => {});

const results = (id: string) => agent.link.received.filter((frame) => frame.type === "op_result" && frame.id === id);
const op = (id: string, kind: string, args: Record<string, unknown>, bind = false) => ({
  type: "op", id, session_id: THREAD, calling_session_id: THREAD, invocation_id: bind ? "bind" : "1:c",
  ordinal: bind ? 0 : 1, kind, args, digest: `digest-${id}`,
});

// How many of a test's own sleeps run in the thread's root, in the guest, as a command
// of the thread's there counts them: each test sleeps for a length of its own.
async function sleeping(seconds: number): Promise<number> {
  const id = `pgrep-${(counted += 1)}`;
  agent.link.send(op(id, "run", { command: `pgrep -fc '^sleep ${seconds}$' || true`, workdir: null, timeout: 30 }));
  await agent.link.until(() => results(id).length === 1, 30_000);
  agent.link.send({ type: "op_ack", id });
  return Number((results(id)[0]?.outcome as { ok?: { output?: string } }).ok?.output?.trim() || 0);
}

// The app registered with the fake agent, and a thread bound to *folder*, as the web client binds it.
async function bound(): Promise<{ shell: ElectronApplication; page: Page; client: Page }> {
  const shell = await launch(home);
  app = shell;
  await stubNative(shell);
  await shell.evaluate((_electron, picked) => Object.assign(globalThis, { folder: picked }), folder);
  const page = await shellPage(shell);
  await connect(page, origin);
  await signedInAndAdded(shell, page, agent);
  const client = await webClient(shell, origin);
  // The user accepts the folder in the desktop's own sheet, in its first mode, Work freely.
  const preparing = client.evaluate(() => window.surogateDesktop!.prepareFolder("pick"));
  await press(await prompt(shell), "accept");
  const prepared = await preparing;
  expect(prepared).toMatchObject({ folder, mode: "free" });
  // The server records the chat with the folder and nonce, and sends its bind operation.
  agent.link.send(op("bind-1", "bind", { folder: prepared!.folder, nonce: prepared!.nonce }, true));
  const binding = client.evaluate((token) => window.surogateDesktop!.bindSession("6c1e9f7d-1a2b-4c3d-8e4f-5a6b7c8d9e0f", token), prepared!.token);
  await agent.link.until(() => results("bind-1").length === 1, 10_000);
  expect(results("bind-1")[0]?.outcome).toEqual({ ok: null });
  agent.link.send({ type: "op_ack", id: "bind-1" });
  await binding;
  return { shell, page, client };
}

describe.skipIf(process.env.SUROGATE_VM_TESTS !== "1")("the demo", () => {
  it("runs a local thread's command in the folder its user confirmed", async () => {
    await bound();
    const asked = await app!.evaluate(() => (globalThis as unknown as { asked: Array<{ message: string }> }).asked);
    expect(asked.map((options) => options.message)).toEqual([`Connect to ${origin.replace("http://", "")}?`]);
    agent.link.send(op("run-1", "run", { command: "echo hi > made.txt", workdir: null, timeout: 30 }));
    await agent.link.until(() => results("run-1").length === 1, 30_000);
    expect(results("run-1")[0]?.outcome).toMatchObject({ ok: { returncode: 0 } });
    expect(readFileSync(join(folder, "made.txt"), "utf8")).toBe("hi\n");
  });

  it("asks before quitting while a thread works on this computer, and records the work cut off by the app", async () => {
    const { shell, page } = await bound();
    agent.link.send(op("run-2", "run", { command: "sleep 600", workdir: null, timeout: 900 }));
    await expect.poll(() => sleeping(600), { timeout: 30_000 }).toBe(1);
    // Cancel: the app stays, the link stays up, and the command goes on.
    await answer(shell, 2);
    quitApp(shell);
    await expect.poll(async () => (await asked(shell)).at(-1)).toMatchObject({
      message: "Surogate is still working", detail: "1 thread is working on this computer. Quitting now will interrupt that work.",
    });
    await new Promise((resolve) => setTimeout(resolve, 1_000));
    expect(await sleeping(600)).toBe(1);
    expect(await page.getAttribute("#device", "title")).toBe("Connected as Laptop");
    expect(results("run-2")).toEqual([]);
    // Quit anyway: the link closes, the command is recorded cut off by the app, and the app exits.
    await answer(shell, 0);
    const closed = shell.waitForEvent("close");
    quitApp(shell);
    await closed;
    app = undefined;
    const journal = new OperationJournal(join(home, "surogate", "devices", "d", "journal.sqlite"));
    try {
      expect(journal.unsent().find((result) => result.id === "run-2")?.outcome).toEqual(APP_CLOSED);
    } finally {
      journal.close();
    }
  });
});

describe.skipIf(process.env.SUROGATE_VM_TESTS !== "1")("quitting", () => {
  it("says, while it waits, that it quits once the thread finishes, in the window and the tray, and goes from the window first when told to quit now", async () => {
    const { shell, page } = await bound();
    await watchTray(shell);
    // The quit's line is a live region from the start, empty, so that a screen reader hears it when it speaks.
    expect(await page.evaluate(() => {
      const line = document.getElementById("quitting-text")!;
      return [line.getAttribute("role"), line.textContent, line.closest("[hidden]") === null];
    })).toEqual(["status", "", true]);
    expect(await page.isVisible("#quit-now")).toBe(false);
    agent.link.send(op("run-5", "run", { command: "sleep 603", workdir: null, timeout: 900 }));
    await expect.poll(() => sleeping(603), { timeout: 30_000 }).toBe(1);
    await answer(shell, 1);
    quitApp(shell);
    await expect.poll(() => page.textContent("#quitting-text")).toBe("Quitting once 1 thread working on this computer finishes.");
    expect(await page.isVisible("#quit-now")).toBe(true);
    await expect.poll(() => trayLabels(shell)).toEqual(["Show Surogate", "Connected as Laptop", "", "Settings…", "Quit now"]);
    // The window goes the moment the user says quit now, before the device has stopped.
    const hidden = new Promise<void>((resolve) => shell.on("console", (message) => {
      if (message.text() === "window hidden") resolve();
    }));
    await shell.evaluate(({ BrowserWindow }) => {
      BrowserWindow.getAllWindows().find((window) => window.webContents.getURL().endsWith("/shell.html"))!.once("hide", () => console.log("window hidden"));
    });
    const closed = shell.waitForEvent("close");
    await page.click("#quit-now");
    await hidden;
    // Once it goes, a second launch brings no window back while the device stops.
    expect(await shell.evaluate(({ app: electron, BrowserWindow }) => {
      electron.emit("second-instance", {}, [], "");
      return BrowserWindow.getAllWindows().some((window) => window.isVisible());
    }).catch(() => false)).toBe(false);
    await closed;
    app = undefined;
    const journal = new OperationJournal(join(home, "surogate", "devices", "d", "journal.sqlite"));
    try {
      expect(journal.unsent().find((result) => result.id === "run-5")?.outcome).toEqual(APP_CLOSED);
    } finally {
      journal.close();
    }
  });

  it("asks, with Keep running off, before closing the window quits while a thread works, and keeps the window on Cancel", async () => {
    // Off since an earlier run.
    mkdirSync(join(home, "surogate"), { recursive: true });
    writeFileSync(join(home, "surogate", "preferences.json"), JSON.stringify({ keepRunning: false }));
    const { shell, page } = await bound();
    agent.link.send(op("run-6", "run", { command: "sleep 604", workdir: null, timeout: 900 }));
    await expect.poll(() => sleeping(604), { timeout: 30_000 }).toBe(1);
    await answer(shell, 2);
    const shown = () => shell.evaluate(({ BrowserWindow }) =>
      BrowserWindow.getAllWindows().find((window) => window.webContents.getURL().endsWith("/shell.html"))!.isVisible());
    await shell.evaluate(({ BrowserWindow }) =>
      BrowserWindow.getAllWindows().find((window) => window.webContents.getURL().endsWith("/shell.html"))!.close());
    await expect.poll(async () => (await asked(shell)).at(-1)?.message).toBe("Surogate is still working");
    // Cancel: the window stays, and so do the link and the command.
    await new Promise((resolve) => setTimeout(resolve, 500));
    expect(await shown()).toBe(true);
    expect(await page.getAttribute("#device", "title")).toBe("Connected as Laptop");
    expect(await sleeping(604)).toBe(1);
    // Quit anyway, so the test's own quit goes through the app's.
    await answer(shell, 0);
  });

  it("waits for the threads when told to, and quits once they finish", async () => {
    const { shell } = await bound();
    agent.link.send(op("run-3", "run", { command: "sleep 601", workdir: null, timeout: 900 }));
    await expect.poll(() => sleeping(601), { timeout: 30_000 }).toBe(1);
    await answer(shell, 1);
    const closed = shell.waitForEvent("close");
    quitApp(shell);
    await expect.poll(async () => (await asked(shell)).at(-1)?.message).toBe("Surogate is still working");
    // The server cancels the command: nothing works on this computer any more, and the app quits.
    agent.link.send({ type: "cancel", id: "run-3" });
    await closed;
    app = undefined;
  });

  it("asks once while its question is up, and offers to quit now when asked again while it waits", async () => {
    const { shell } = await bound();
    agent.link.send(op("run-4", "run", { command: "sleep 602", workdir: null, timeout: 900 }));
    await expect.poll(() => sleeping(602), { timeout: 30_000 }).toBe(1);
    // Two quits while the question is up: one question. It is answered "Wait for them".
    await shell.evaluate(() => Object.assign(globalThis, { hold: true, answer: 1 }));
    quitApp(shell);
    quitApp(shell);
    await expect.poll(async () => (await asked(shell)).length).toBe(2);
    await new Promise((resolve) => setTimeout(resolve, 500));
    expect((await asked(shell)).map((options) => options.message).slice(1)).toEqual(["Surogate is still working"]);
    await shell.evaluate(() => {
      Object.assign(globalThis, { hold: false });
      (globalThis as unknown as { release(): void }).release();
    });
    // Asked again while it waits: quit now, or keep waiting; "Keep waiting" keeps the app and the command.
    quitApp(shell);
    await expect.poll(async () => (await asked(shell)).at(-1)).toMatchObject({
      message: "Quit now?", detail: "Surogate is waiting for 1 thread working on this computer. Quitting now will interrupt that work.",
    });
    await new Promise((resolve) => setTimeout(resolve, 500));
    expect(await sleeping(602)).toBe(1);
    // "Quit now": the app quits, and records the command cut off by the app.
    await answer(shell, 0);
    const closed = shell.waitForEvent("close");
    quitApp(shell);
    await closed;
    app = undefined;
    const journal = new OperationJournal(join(home, "surogate", "devices", "d", "journal.sqlite"));
    try {
      expect(journal.unsent().find((result) => result.id === "run-4")?.outcome).toEqual(APP_CLOSED);
    } finally {
      journal.close();
    }
  });
});
