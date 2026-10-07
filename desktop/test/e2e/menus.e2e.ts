// The app's menu, through the real app: it replaces Electron's own, its keys reach every
// page, and View acts on the agent's page only.

import { rmSync } from "node:fs";

import type { ElectronApplication, Page } from "playwright-core";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { connect, FakeAgent, signedInAndAdded, webClient } from "./fake-agent.js";
import { dataHome, launch, quit, shellPage, stubNative } from "./launch.js";

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

async function signedIn(): Promise<{ shell: ElectronApplication; page: Page; client: Page }> {
  const shell = await launch(home);
  app = shell;
  await stubNative(shell);
  const page = await shellPage(shell);
  await connect(page, origin);
  await signedInAndAdded(shell, page, agent);
  return { shell, page, client: await webClient(shell, origin) };
}

// Click the app menu's item *id*, as the user picks it.
const pick = (shell: ElectronApplication, id: string) => shell.evaluate(({ Menu }, chosen) => {
  const item = Menu.getApplicationMenu()?.getMenuItemById(chosen);
  if (!item) throw new Error(`No menu item ${chosen}`);
  item.click();
}, id);

describe("the app's menu", () => {
  it("replaces Electron's own, and no page has a key handler of its own", async () => {
    const { shell } = await signedIn();
    expect(await shell.evaluate(({ Menu }) => Menu.getApplicationMenu()?.items.map((item) => item.label)))
      .toEqual(["File", "Edit", "View", "Developer", "Help"]);
    // Electron's reload, zoom and developer tools act on whatever has the keyboard: none is left.
    expect(await shell.evaluate(({ Menu }) => Menu.getApplicationMenu()!.items.flatMap((item) => item.submenu?.items ?? [])
      .map((item) => item.role).filter(Boolean))).toEqual(["close", "undo", "redo", "cut", "copy", "paste", "selectall"]);
    // Ctrl+Q and Ctrl+Shift+, are the menu's, matched on the key's place: no page matches a key's name.
    expect(await shell.evaluate(({ webContents }) => webContents.getAllWebContents()
      .map((contents) => contents.listenerCount("before-input-event")))).toEqual([0, 0]);
  });

  it("opens from the window's menu button", async () => {
    const { shell, page } = await signedIn();
    await shell.evaluate(({ Menu }) => {
      const popped: boolean[] = [];
      Object.assign(globalThis, { popped });
      Menu.prototype.popup = function (this: Electron.Menu) {
        popped.push(this === Menu.getApplicationMenu());
      };
    });
    await page.click("#menu");
    await expect.poll(() => shell.evaluate(() => (globalThis as unknown as { popped: boolean[] }).popped)).toEqual([true]);
  });

  it("zooms the agent's page, never the window's own, and opens a new chat", async () => {
    const { shell, client } = await signedIn();
    const levels = () => shell.evaluate(({ BrowserWindow, webContents }) => {
      const window = BrowserWindow.getAllWindows()[0]!.webContents;
      const view = webContents.getAllWebContents().find((contents) => contents !== window)!;
      return [window.getZoomLevel(), view.getZoomLevel()];
    });
    await pick(shell, "zoom-in");
    await pick(shell, "zoom-in");
    expect(await levels()).toEqual([0, 2]);
    await pick(shell, "zoom-out");
    expect(await levels()).toEqual([0, 1]);
    await pick(shell, "zoom-reset");
    expect(await levels()).toEqual([0, 0]);
    await pick(shell, "new-chat");
    await expect.poll(() => new URL(client.url()).pathname).toBe("/chat");
  });

  it("zooms in on Ctrl++ as on Ctrl+=, from the agent's page", async () => {
    const { shell } = await signedIn();
    const press = (keyCode: string, modifiers: Array<"control" | "shift">) => shell.evaluate(({ BrowserWindow, webContents }, [code, held]) => {
      const window = BrowserWindow.getAllWindows()[0]!.webContents;
      const view = webContents.getAllWebContents().find((contents) => contents !== window)!;
      view.focus();
      view.sendInputEvent({ type: "keyDown", keyCode: code as string, modifiers: held as Array<"control" | "shift"> });
      view.sendInputEvent({ type: "keyUp", keyCode: code as string, modifiers: held as Array<"control" | "shift"> });
    }, [keyCode, modifiers] as const);
    const level = () => shell.evaluate(({ BrowserWindow, webContents }) => {
      const window = BrowserWindow.getAllWindows()[0]!.webContents;
      return webContents.getAllWebContents().find((contents) => contents !== window)!.getZoomLevel();
    });
    await press("=", ["control"]);
    await expect.poll(level).toBe(1);
    await press("Plus", ["control", "shift"]);
    await expect.poll(level).toBe(2);
  });

  it("quits on Ctrl+Q from the agent's page", async () => {
    const { shell } = await signedIn();
    const closed = shell.waitForEvent("close");
    void shell.evaluate(({ BrowserWindow, webContents }) => {
      const window = BrowserWindow.getAllWindows()[0]!.webContents;
      const view = webContents.getAllWebContents().find((contents) => contents !== window)!;
      view.focus();
      view.sendInputEvent({ type: "keyDown", keyCode: "Q", modifiers: ["control"] });
    }).catch(() => {});
    await closed;
    app = undefined;
  });
});
