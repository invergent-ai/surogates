// The app's menu, through the real app: it replaces Electron's own, its keys reach every
// page, and View acts on the agent's page only.

import { readFileSync, rmSync } from "node:fs";
import { join } from "node:path";

import type { ElectronApplication, Page } from "playwright-core";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { connect, FakeAgent, opened, signedInAndAdded, webClient } from "./fake-agent.js";
import { dataHome, launch, prompt, quit, shellPage, stubNative } from "./launch.js";

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

  it("shows About from Help, once, over the window, with the version it copies, and closes on Escape", async () => {
    const { shell } = await signedIn();
    const { version } = JSON.parse(readFileSync(join(import.meta.dirname, "..", "..", "package.json"), "utf8")) as { version: string };
    await pick(shell, "about");
    await pick(shell, "about");
    let about: Page | undefined;
    await expect.poll(() => {
      about = shell.windows().find((page) => page.url().endsWith("/about.html"));
      return about !== undefined;
    }).toBe(true);
    await expect.poll(() => about!.textContent("#version")).toBe(`Version ${version}`);
    // Surogate's mark, drawn as data: its disc and its figure.
    expect(await about!.evaluate(() => {
      const svg = document.querySelector("#about-mark svg");
      return [svg?.getAttribute("viewBox"), svg?.querySelector(":scope > circle")?.getAttribute("fill"), svg?.querySelectorAll("path").length];
    })).toEqual(["0 0 113 113", "#ffaf10", 1]);
    expect(await shell.evaluate(({ BrowserWindow }) => {
      const windows = BrowserWindow.getAllWindows();
      const shown = windows.filter((window) => window.webContents.getURL().endsWith("/about.html"));
      const main = windows.find((window) => window.webContents.getURL().endsWith("/shell.html"));
      return shown.map((window) => [window.getSize(), window.isModal(), window.getParentWindow() === main]);
    })).toEqual([[[320, 400], true, true]]);
    await about!.click("#version");
    await expect.poll(() => about!.textContent("#copied")).toBe("Copied to the clipboard");
    const chromium = await shell.evaluate(() => process.versions.chrome);
    expect(await shell.evaluate(({ clipboard }) => clipboard.readText())).toBe(`Surogate ${version} (Electron 44.5.0, Chromium ${chromium})`);
    await about!.click("#documentation");
    await expect.poll(() => opened(shell)).toContain("https://docs.surogate.ai/work/");
    // Escape closes the window before Playwright sends the key up.
    await about!.keyboard.press("Escape").catch(() => {});
    await expect.poll(() => shell.windows().some((page) => page.url().endsWith("/about.html"))).toBe(false);
  });

  it("leaves the agent's page as it was on Ctrl+R from a prompt or About, whose windows have no menu", async () => {
    const { shell, client } = await signedIn();
    // Each reload of the agent's page, counted where the menu's Reload asks it.
    await shell.evaluate(({ BrowserWindow, webContents }) => {
      const window = BrowserWindow.getAllWindows()[0]!.webContents;
      const view = webContents.getAllWebContents().find((contents) => contents !== window)!;
      const reloads = { count: 0 };
      Object.assign(globalThis, { reloads });
      view.reload = () => void reloads.count++;
    });
    const reloads = () => shell.evaluate(() => (globalThis as unknown as { reloads: { count: number } }).reloads.count);
    // Ctrl+R, then Escape, as the keyboard sends them to the window with the focus once it is *page*'s:
    // the menu's keys reach only that window. Escape closes a prompt or About, after Ctrl+R has been taken.
    const keys = async (page: string, escape: boolean) => {
      await expect.poll(() => shell.evaluate(({ BrowserWindow }) => BrowserWindow.getFocusedWindow()?.webContents.getURL() ?? ""))
        .toMatch(new RegExp(`/${page}$`));
      await shell.evaluate(({ BrowserWindow }, closing) => {
        const contents = BrowserWindow.getFocusedWindow()!.webContents;
        for (const keyCode of closing ? ["R", "Escape"] : ["R"]) {
          const modifiers: Array<"control"> = keyCode === "R" ? ["control"] : [];
          contents.sendInputEvent({ type: "keyDown", keyCode, modifiers });
          contents.sendInputEvent({ type: "keyUp", keyCode, modifiers });
        }
      }, escape).catch(() => {});
    };
    const shown = (page: string) => shell.evaluate(({ BrowserWindow }, name) =>
      BrowserWindow.getAllWindows().some((window) => window.webContents.getURL().endsWith(`/${name}`)), page);
    // From the window, Ctrl+R reloads the agent's page.
    await keys("shell.html", false);
    await expect.poll(reloads).toBe(1);
    // The folder sheet for a new chat, over the window.
    void client.evaluate(() => window.surogateDesktop!.prepareFolder("last")).catch(() => {});
    await prompt(shell);
    await keys("prompt.html", true);
    await expect.poll(() => shown("prompt.html")).toBe(false);
    expect(await reloads()).toBe(1);
    await pick(shell, "about");
    await keys("about.html", true);
    await expect.poll(() => shown("about.html")).toBe(false);
    expect(await reloads()).toBe(1);
  });

  it("copies and pastes on Ctrl+C and Ctrl+V in a prompt and in About, whose windows have no Edit menu", async () => {
    const { shell, client } = await signedIn();
    // A field of the page's, with *text* selected in it, as a user selects it; then Ctrl+C, a clipboard of
    // something else, an empty field and Ctrl+V, as the keyboard sends them to the window with the focus.
    const copyAndPaste = async (page: Page, file: string, text: string) => {
      await page.evaluate((selected) => {
        const field = document.createElement("input");
        field.id = "typed";
        document.body.append(field);
        field.value = selected;
        field.focus();
        field.select();
      }, text);
      const keys = (keyCode: string) => shell.evaluate(({ BrowserWindow }, [name, code]) => {
        const window = BrowserWindow.getAllWindows().find((found) => found.webContents.getURL().endsWith(`/${name}`))!;
        window.focus();
        window.webContents.sendInputEvent({ type: "keyDown", keyCode: code!, modifiers: ["control"] });
        window.webContents.sendInputEvent({ type: "keyUp", keyCode: code!, modifiers: ["control"] });
      }, [file, keyCode] as const);
      await expect.poll(() => shell.evaluate(({ BrowserWindow }) => BrowserWindow.getFocusedWindow()?.webContents.getURL() ?? ""))
        .toMatch(new RegExp(`/${file}$`));
      await keys("C");
      await expect.poll(() => shell.evaluate(({ clipboard }) => clipboard.readText())).toBe(text);
      await shell.evaluate(({ clipboard }) => clipboard.writeText("pasted from the clipboard"));
      await page.evaluate(() => {
        const field = document.querySelector<HTMLInputElement>("#typed")!;
        field.value = "";
        field.focus();
      });
      await keys("V");
      await expect.poll(() => page.inputValue("#typed")).toBe("pasted from the clipboard");
    };
    void client.evaluate(() => window.surogateDesktop!.prepareFolder("last")).catch(() => {});
    await copyAndPaste(await prompt(shell), "prompt.html", "copied from a prompt");
    await pick(shell, "about");
    let about: Page | undefined;
    await expect.poll(() => {
      about = shell.windows().find((page) => page.url().endsWith("/about.html"));
      return about !== undefined;
    }).toBe(true);
    await about!.waitForSelector("#version");
    await copyAndPaste(about!, "about.html", "copied from About");
  });

  it("leaves the agent's page as it was on Ctrl+R from the Composio sign-in, a window the agent's page opens with no menu", async () => {
    const { shell, client } = await signedIn();
    await shell.evaluate(({ BrowserWindow, webContents }) => {
      const window = BrowserWindow.getAllWindows()[0]!.webContents;
      const view = webContents.getAllWebContents().find((contents) => contents !== window)!;
      const reloads = { count: 0 };
      Object.assign(globalThis, { reloads });
      view.reload = () => void reloads.count++;
      // The sign-in's own page stays out of the test: the popup opens on nothing outside.
      view.session.webRequest.onBeforeRequest({ urls: ["https://connect.composio.dev/*"] }, (_details, answer) => answer({ cancel: true }));
    });
    const reloads = () => shell.evaluate(() => (globalThis as unknown as { reloads: { count: number } }).reloads.count);
    const popped = shell.waitForEvent("window");
    await client.evaluate(() => void window.open("https://connect.composio.dev/link/abc", "composio-oauth", "popup=yes"));
    await popped;
    const popup = () => shell.evaluate(({ BrowserWindow }) => {
      const found = BrowserWindow.getAllWindows().find((window) => !window.webContents.getURL().endsWith("/shell.html"))!;
      found.focus();
      return found.isFocused();
    });
    await expect.poll(popup).toBe(true);
    await shell.evaluate(({ BrowserWindow }) => {
      const contents = BrowserWindow.getFocusedWindow()!.webContents;
      contents.sendInputEvent({ type: "keyDown", keyCode: "R", modifiers: ["control"] });
      contents.sendInputEvent({ type: "keyUp", keyCode: "R", modifiers: ["control"] });
    });
    await new Promise((resolve) => setTimeout(resolve, 500));
    expect(await reloads()).toBe(0);
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
