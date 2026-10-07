// The desktop's own prompts, through the real app, each in a window of the app's own: the
// folder sheet, and the approval prompts of a chat that asks every time. Nothing here runs
// in the VM: a denied command never reaches it, and the file kinds run in the root's file host.

import { linkSync, mkdirSync, mkdtempSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import type { ElectronApplication, Page } from "playwright-core";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { connect, FakeAgent, signedInAndAdded, webClient } from "./fake-agent.js";
import { dataHome, key, launch, press, prompt, promptsShown, quit, shellPage, stubNative } from "./launch.js";

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

// The app signed in and registered: the agent's web client, with the folder dialog picking *picked*.
async function signedIn(picked = folder): Promise<Page> {
  app = await launch(home);
  await stubNative(app);
  await app.evaluate((_electron, chosen) => Object.assign(globalThis, { folder: chosen }), picked);
  const page = await shellPage(app);
  await connect(page, origin);
  await signedInAndAdded(app, page, agent);
  return webClient(app, origin);
}

const prepare = (client: Page) => client.evaluate(() => window.surogateDesktop!.prepareFolder("pick"));

const text = (page: Page, selector: string) => page.textContent(selector);

describe("the folder sheet", () => {
  it("binds the folder the user accepts in the desktop's own window, in the mode chosen there", async () => {
    const client = await signedIn();
    const prepared = prepare(client);
    const sheet = await prompt(app!);
    expect(await text(sheet, "#prompt-title")).toBe(`Work in ${folder.split("/").at(-1)}?`);
    expect(await text(sheet, ".code")).toBe(folder);
    // Each mode is named by its label alone, and described by its description.
    expect(await sheet.getByRole("radio", { name: "Work freely", exact: true }).isChecked()).toBe(true);
    expect(await sheet.getAttribute('input[value="free"]', "aria-describedby")).toBe("choice-free");
    // What allows is held back at first, and the keyboard starts on it.
    expect(await sheet.getAttribute('[data-id="accept"]', "aria-disabled")).toBe("true");
    expect(await sheet.evaluate(() => (document.activeElement as HTMLElement).dataset.id)).toBe("accept");
    expect(await sheet.getAttribute(".prompt", "role")).toBe("alertdialog");
    await sheet.check('input[value="ask"]');
    await press(sheet, "accept");
    expect(await prepared).toMatchObject({ folder, mode: "ask" });
    await expect.poll(() => promptsShown(app!)).toBe(0);
  });

  it("takes no Enter before its input protection has passed, nor one held down, and Enter accepts after", async () => {
    const client = await signedIn();
    const prepared = prepare(client);
    const sheet = await prompt(app!);
    expect(await sheet.getAttribute('[data-id="accept"]', "aria-disabled")).toBe("true");
    // Typed as it opened, then held: the key repeats once it may answer.
    await sheet.keyboard.down("Enter");
    await expect.poll(() => sheet.getAttribute('[data-id="accept"]', "aria-disabled")).toBe("false");
    await sheet.keyboard.down("Enter");
    await sheet.keyboard.up("Enter");
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(await promptsShown(app!)).toBe(1);
    await key(sheet, "Enter");
    expect(await prepared).toMatchObject({ folder, mode: "free" });
  });

  it("takes an answer that allows anything only once its input protection has passed, even straight from its page", async () => {
    const client = await signedIn();
    const prepared = prepare(client);
    const sheet = await prompt(app!);
    const answer = (button: string, choice: string | null) => sheet.evaluate(([pressed, chosen]) =>
      (window as unknown as { surogatePrompt: { answer(b: string, c: string | null): Promise<boolean> } }).surogatePrompt.answer(pressed!, chosen ?? null), [button, choice]);
    expect(await sheet.getAttribute('[data-id="accept"]', "aria-disabled")).toBe("true");
    expect(await answer("accept", "free")).toBe(false);
    await expect(answer("accept", "everything")).rejects.toThrow("Not an option of this prompt");
    await expect(answer("grant", "free")).rejects.toThrow("Not a button of this prompt");
    expect(await promptsShown(app!)).toBe(1);
    await expect.poll(() => sheet.getAttribute('[data-id="accept"]', "aria-disabled")).toBe("false");
    // Taken: the window closes as it answers.
    expect(await answer("accept", "ask").catch(() => true)).toBe(true);
    expect(await prepared).toMatchObject({ folder, mode: "ask" });
  });

  it("takes no press begun before its input protection has passed, by key or by mouse, wherever it ends", async () => {
    const client = await signedIn();
    const begun: Array<(sheet: Page) => Promise<() => Promise<void>>> = [
      async (sheet) => {
        await sheet.keyboard.down(" ");
        return () => sheet.keyboard.up(" ");
      },
      async (sheet) => {
        await sheet.hover('[data-id="accept"]');
        await sheet.mouse.down();
        return () => sheet.mouse.up();
      },
    ];
    for (const begin of begun) {
      const prepared = prepare(client);
      const sheet = await prompt(app!);
      expect(await sheet.getAttribute('[data-id="accept"]', "aria-disabled")).toBe("true");
      const release = await begin(sheet);
      await expect.poll(() => sheet.getAttribute('[data-id="accept"]', "aria-disabled")).toBe("false");
      await release();
      await new Promise((resolve) => setTimeout(resolve, 100));
      expect(await promptsShown(app!)).toBe(1);
      await key(sheet, "Escape");
      expect(await prepared).toBeNull();
    }
  });

  it("shows a path with right-to-left names in the order it is written", async () => {
    const hebrew = join(folder, "א", "ב.txt");
    mkdirSync(hebrew, { recursive: true });
    const client = await signedIn(hebrew);
    const prepared = prepare(client);
    const sheet = await prompt(app!);
    // Left edges of the alef, the slash after it, and the bet: as written, left to right.
    const edges = await sheet.evaluate(() => {
      const text = document.querySelector(".code")!.firstChild as Text;
      const left = (at: number) => {
        const range = document.createRange();
        range.setStart(text, at);
        range.setEnd(text, at + 1);
        return range.getBoundingClientRect().left;
      };
      return [left(text.data.indexOf("א")), left(text.data.lastIndexOf("/")), left(text.data.indexOf("ב"))];
    });
    expect(edges[0]).toBeLessThan(edges[1]!);
    expect(edges[1]).toBeLessThan(edges[2]!);
    await key(sheet, "Escape");
    expect(await prepared).toBeNull();
  });

  it("names a right-to-left folder in its title in the order it is written", async () => {
    const hebrew = join(folder, "א.ב.txt");
    mkdirSync(hebrew);
    const client = await signedIn(hebrew);
    const prepared = prepare(client);
    const sheet = await prompt(app!);
    expect(await text(sheet, "#prompt-title")).toBe("Work in א.ב.txt?");
    // Left edges of the alef, the dot after it, and the bet: as written, left to right.
    const edges = await sheet.evaluate(() => {
      const text = document.querySelector("#prompt-title")!.firstChild as Text;
      const left = (at: number) => {
        const range = document.createRange();
        range.setStart(text, at);
        range.setEnd(text, at + 1);
        return range.getBoundingClientRect().left;
      };
      return [left(text.data.indexOf("א")), left(text.data.indexOf(".")), left(text.data.indexOf("ב"))];
    });
    expect(edges[0]).toBeLessThan(edges[1]!);
    expect(edges[1]).toBeLessThan(edges[2]!);
    await key(sheet, "Escape");
    expect(await prepared).toBeNull();
  });

  it("cancels on Escape", async () => {
    const client = await signedIn();
    const prepared = prepare(client);
    const sheet = await prompt(app!);
    await key(sheet, "Escape");
    expect(await prepared).toBeNull();
  });

  it("closes when the page that asked goes away", async () => {
    const client = await signedIn();
    void prepare(client).catch(() => {});
    await prompt(app!);
    await client.evaluate(() => {
      location.href = "/elsewhere";
    });
    await expect.poll(() => app!.evaluate(({ BrowserWindow }) =>
      BrowserWindow.getAllWindows().filter((window) => window.webContents.getURL().endsWith("/prompt.html")).length)).toBe(0);
  });

  it("offers only Change and Cancel for a folder that cannot be used, then shows the folder chosen next", async () => {
    // The app's own state root: no chat may work there.
    const client = await signedIn(join(home, "surogate"));
    const prepared = prepare(client);
    const refused = await prompt(app!);
    expect(await text(refused, "#prompt-title")).toBe("surogate cannot be used");
    expect(await refused.$$eval("#prompt-buttons button", (buttons) => buttons.map((button) => button.dataset.id))).toEqual(["cancel", "change"]);
    await app!.evaluate((_electron, chosen) => Object.assign(globalThis, { folder: chosen }), folder);
    await press(refused, "change");
    await expect.poll(async () => text(await prompt(app!), "#prompt-title")).toBe(`Work in ${folder.split("/").at(-1)}?`);
    await press(await prompt(app!), "accept");
    expect(await prepared).toMatchObject({ folder });
  });

  it("says which files in the folder are also linked from elsewhere", async () => {
    const outside = join(home, "outside.txt");
    writeFileSync(outside, "o");
    linkSync(outside, join(folder, "linked.txt"));
    const client = await signedIn();
    const prepared = prepare(client);
    const sheet = await prompt(app!);
    expect(await text(sheet, ".note")).toBe(
      "1 file in this folder is also linked from elsewhere; commands the agent runs can change those copies too: linked.txt.",
    );
    await press(sheet, "cancel");
    expect(await prepared).toBeNull();
  });

  it("waits, unseen, while the app's window is hidden, and opens once it is shown", async () => {
    const client = await signedIn();
    await app!.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0]!.hide());
    const prepared = prepare(client);
    await expect.poll(() => app!.windows().some((page) => page.url().endsWith("/prompt.html"))).toBe(true);
    await new Promise((resolve) => setTimeout(resolve, 500));
    expect(await promptsShown(app!)).toBe(0);
    await app!.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0]!.show());
    await expect.poll(() => promptsShown(app!)).toBe(1);
    await press(await prompt(app!), "accept");
    expect(await prepared).toMatchObject({ folder });
  });
});
