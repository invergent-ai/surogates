// The desktop's own prompts, through the real app, each in a window of the app's own: the
// folder sheet, and the approval prompts of a chat that asks every time. Nothing here runs
// in the VM: a denied command never reaches it, and the file kinds run in the root's file host.

import { existsSync, linkSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";

import type { ElectronApplication, Page } from "playwright-core";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { connect, FakeAgent, signedInAndAdded, webClient } from "./fake-agent.js";
import { dataHome, key, launch, MAIN, press, prompt, promptsShown, quit, shellPage, stubNative } from "./launch.js";

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

// The left edge of each of the characters at *at* in *selector*'s text: drawn as written, they rise.
async function lefts(page: Page, selector: string, at: (text: string) => number[]): Promise<number[]> {
  return page.evaluate(([chosen, indices]) => {
    const text = document.querySelector(chosen)!.firstChild as Text;
    return indices.map((index) => {
      const range = document.createRange();
      range.setStart(text, index);
      range.setEnd(text, index + 1);
      return range.getBoundingClientRect().left;
    });
  }, [selector, at((await text(page, selector))!)] as const);
}

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
    // What allows is held back at first, and the keyboard starts on the mode chosen.
    expect(await sheet.getAttribute('[data-id="accept"]', "aria-disabled")).toBe("true");
    expect(await sheet.evaluate(() => (document.activeElement as HTMLInputElement).value)).toBe("free");
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
        await sheet.focus('[data-id="accept"]');
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

  it("takes nothing from a sentence typed as it opens", async () => {
    const client = await signedIn();
    const prepared = prepare(client);
    let settled = false;
    void prepared.then(() => {
      settled = true;
    });
    const sheet = await prompt(app!);
    // The user typing in the composer as the page opens the sheet: its spaces come long after the input protection.
    await sheet.keyboard.type("please fix the failing test", { delay: 90 });
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect([await promptsShown(app!), settled]).toEqual([1, false]);
    await key(sheet, "Escape");
    expect(await prepared).toBeNull();
  });

  it("shows a path with right-to-left names in the order it is written", async () => {
    const hebrew = join(folder, "א", "ב.txt");
    mkdirSync(hebrew, { recursive: true });
    const client = await signedIn(hebrew);
    const prepared = prepare(client);
    const sheet = await prompt(app!);
    // Left edges of the alef, the slash after it, and the bet: as written, left to right.
    const edges = await lefts(sheet, ".code", (shown) => [shown.indexOf("א"), shown.lastIndexOf("/"), shown.indexOf("ב")]);
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
    const edges = await lefts(sheet, "#prompt-title", (shown) => [shown.indexOf("א"), shown.indexOf("."), shown.indexOf("ב")]);
    expect(edges[0]).toBeLessThan(edges[1]!);
    expect(edges[1]).toBeLessThan(edges[2]!);
    await key(sheet, "Escape");
    expect(await prepared).toBeNull();
  });

  it("names a right-to-left path in the order it is written when it says why a folder cannot be used", async () => {
    // Inside the app's own state root: no chat may work there.
    const hebrew = join(home, "surogate", "א", "ב");
    const client = await signedIn(hebrew);
    mkdirSync(hebrew, { recursive: true });
    const prepared = prepare(client);
    const refused = await prompt(app!);
    expect(await text(refused, "#prompt-lead")).toContain(`the folder ${hebrew} holds`);
    // Left edges of the alef, the slash after it, and the bet: as written, left to right.
    const edges = await lefts(refused, "#prompt-lead", (shown) => [shown.indexOf("א"), shown.lastIndexOf("/"), shown.indexOf("ב")]);
    expect(edges[0]).toBeLessThan(edges[1]!);
    expect(edges[1]).toBeLessThan(edges[2]!);
    await key(refused, "Escape");
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

describe("a prompt's text", () => {
  it("shows a command of 200 000 special characters whole, its end reachable, and its buttons answer", async () => {
    await signedIn();
    const command = `${"\u200B".repeat(200_000)}echo END`;
    const shell = dirname(MAIN);
    // A prompt window of the app's own, as an approval opens one, over the app's window.
    const answered = app!.evaluate(({ BrowserWindow }, [module, page, preload, value]) => {
      // The app's own module, as its main process loaded it: an evaluated function has no import().
      const load = process.getBuiltinModule("node:module").createRequire(module);
      const { openPrompt } = load(module) as typeof import("../../src/shell/prompt-window.js");
      return openPrompt({
        parent: BrowserWindow.getAllWindows()[0]!,
        page,
        preload,
        content: {
          title: "Run a command?",
          lead: "A command of special characters.",
          details: [{ label: "Command", value, code: true, keep: "\n\t" }],
          notes: [],
          choice: null,
          buttons: [{ id: "deny", label: "Deny", allows: false }, { id: "allow", label: "Allow once", allows: true }],
          focus: "deny",
          cancel: "deny",
          enter: null,
          height: 400,
        },
        queue: { waiting: () => 0, onChange: () => () => {} },
        unseen: () => {},
      }, new AbortController().signal);
    }, [join(shell, "prompt-window.js"), join(shell, "pages", "prompt.html"), join(shell, "pages-preload.cjs"), command] as const);
    const asked = await prompt(app!);
    expect(await text(asked, ".code")).toBe(`${"U+200B".repeat(200_000)}echo END`);
    // Scrolled to its end, the last line is in view.
    const seen = await asked.evaluate(() => {
      const body = document.querySelector(".prompt-body")!;
      body.scrollTop = body.scrollHeight;
      const end = document.querySelector(".code")!.getBoundingClientRect().bottom;
      return body.scrollHeight > body.clientHeight && end <= body.getBoundingClientRect().bottom + 1;
    });
    expect(seen).toBe(true);
    await press(asked, "allow");
    expect(await answered).toEqual({ button: "allow", choice: null });
  });
});

const CHAT = "7d2e0f8a-2b3c-4d5e-9f60-718293a4b5c6";
const OTHER = "8e3f1a9b-3c4d-4e6f-a071-8293a4b5c6d7";

let next = 0;
// *chat*'s operation as the server sends it: its id.
function send(kind: string, args: Record<string, unknown>, chat = CHAT): string {
  const id = `op-${(next += 1)}`;
  agent.link.send({
    type: "op", id, session_id: chat, calling_session_id: chat, invocation_id: kind === "bind" ? "bind" : "call",
    ordinal: kind === "bind" ? 0 : 1, kind, args, digest: `d-${id}`,
  });
  return id;
}

const results = (id: string) => agent.link.received.filter((frame) => frame.type === "op_result" && frame.id === id);

async function outcome(id: string): Promise<unknown> {
  await agent.link.until(() => results(id).length === 1, 20_000);
  agent.link.send({ type: "op_ack", id });
  return results(id)[0]?.outcome;
}

// *picked* bound to *chat* as the user accepts it in the sheet, in *mode*.
async function bound(client: Page, picked: string, chat = CHAT, mode = "ask"): Promise<void> {
  await app!.evaluate((_electron, chosen) => Object.assign(globalThis, { folder: chosen }), picked);
  const prepared = prepare(client);
  const sheet = await prompt(app!);
  await sheet.check(`input[value="${mode}"]`);
  await press(sheet, "accept");
  const ready = (await prepared)!;
  expect(await outcome(send("bind", { folder: ready.folder, nonce: ready.nonce }, chat))).toEqual({ ok: null });
}

const write = (name: string, text: string, chat = CHAT, into = folder) =>
  send("write", { key: join(into, name), data: Buffer.from(text).toString("base64") }, chat);

describe("an approval prompt", () => {
  it("asks before a change, showing the new content, focused on Deny, and makes it once allowed", async () => {
    await bound(await signedIn(), folder);
    const id = write("notes.txt", "hello\n");
    const asked = await prompt(app!);
    expect(await text(asked, "#prompt-title")).toBe("Write notes.txt?");
    expect(await asked.$$eval(".code", (blocks) => blocks.map((block) => block.textContent))).toEqual(["notes.txt", "hello\n"]);
    expect(await asked.evaluate(() => (document.activeElement as HTMLElement).dataset.id)).toBe("deny");
    expect(await asked.getAttribute('[data-id="allow"]', "aria-disabled")).toBe("true");
    await press(asked, "allow");
    expect(await outcome(id)).toEqual({ ok: null });
    expect(readFileSync(join(folder, "notes.txt"), "utf8")).toBe("hello\n");
  });

  it("denies a command on Escape, as the terminal reads a blocked one, and shows the characters it hides", async () => {
    await bound(await signedIn(), folder);
    const id = send("run", { command: "echo safe \u202Etxt.exe", workdir: null, timeout: 30 });
    const asked = await prompt(app!);
    expect(await text(asked, ".code .special")).toBe("U+202E");
    expect(await text(asked, ".code")).toBe("echo safe U+202Etxt.exe");
    await key(asked, "Escape");
    expect(await outcome(id)).toEqual({ error: { type: "sandbox", message: "The user denied this command on this computer" } });
  });

  it("shows a long command whole, its end reachable", async () => {
    await bound(await signedIn(), folder);
    // A heredoc of a file with Windows line ends: each \r is marked, 140 000 runs in all.
    const command = `cat > report.md <<'EOF'\n${Array.from({ length: 70_000 }, (_, line) => `line ${line}\r`).join("\n")}\nEOF\necho END`;
    const id = send("run", { command, workdir: null, timeout: 30 });
    const asked = await prompt(app!);
    expect(await text(asked, ".code")).toBe(command.replaceAll("\r", "U+000D"));
    // Scrolled to its end, the last line is in view.
    const seen = await asked.evaluate(() => {
      const body = document.querySelector(".prompt-body")!;
      body.scrollTop = body.scrollHeight;
      const end = document.querySelector(".code")!.getBoundingClientRect().bottom;
      return body.scrollHeight > body.clientHeight && end <= body.getBoundingClientRect().bottom + 1;
    });
    expect(seen).toBe(true);
    await press(asked, "deny");
    expect(await outcome(id)).toMatchObject({ error: { type: "sandbox" } });
  });

  it("lets the chat work freely once its user stops asking", async () => {
    await bound(await signedIn(), folder);
    const first = write("a.txt", "a");
    await press(await prompt(app!), "stop_asking");
    expect(await outcome(first)).toEqual({ ok: null });
    expect(await outcome(write("b.txt", "b"))).toEqual({ ok: null });
    expect(await promptsShown(app!)).toBe(0);
    expect(existsSync(join(folder, "b.txt"))).toBe(true);
  });

  it("shows one prompt at a time, saying more wait", async () => {
    const second = realpathSync(mkdtempSync("/tmp/sf-"));
    try {
      const client = await signedIn();
      await bound(client, folder);
      await bound(client, second, OTHER);
      const [one, two] = [write("a.txt", "a"), write("b.txt", "b", OTHER, second)];
      const first = await prompt(app!);
      await expect.poll(() => text(first, "#prompt-waiting")).toBe("More prompts wait after this one.");
      expect(await promptsShown(app!)).toBe(1);
      await press(first, "deny");
      expect(await outcome(one)).toMatchObject({ error: { type: "os", code: "EACCES" } });
      await expect.poll(async () => text(await prompt(app!), "#prompt-waiting")).toBe("");
      await press(await prompt(app!), "allow");
      expect(await outcome(two)).toEqual({ ok: null });
    } finally {
      rmSync(second, { recursive: true, force: true });
    }
  });

  it("closes once the server cancels its operation, and answers nothing", async () => {
    await bound(await signedIn(), folder);
    const id = write("a.txt", "a");
    await prompt(app!);
    agent.link.send({ type: "cancel", id });
    await expect.poll(() => promptsShown(app!)).toBe(0);
    await new Promise((resolve) => setTimeout(resolve, 300));
    expect(results(id)).toEqual([]);
    expect(existsSync(join(folder, "a.txt"))).toBe(false);
  });
});

describe("a chat's mode, from the page", () => {
  it("works freely only once its user confirms it in the desktop's own window", async () => {
    const client = await signedIn();
    await bound(client, folder);
    const free = (chat: string) => client.evaluate((id) => window.surogateDesktop!.requestFreeMode(id), chat);
    const kept = free(CHAT);
    const asked = await prompt(app!);
    expect(await text(asked, "#prompt-title")).toBe(`Let 127.0.0.1:${new URL(origin).port} work freely in ${folder.split("/").at(-1)}?`);
    expect(await asked.evaluate(() => (document.activeElement as HTMLElement).dataset.id)).toBe("keep");
    await press(asked, "keep");
    expect(await kept).toBe(false);
    // Kept asking: this page is refused without a prompt, until it loads again.
    await expect(free(CHAT)).rejects.toThrow("The user chose to keep this chat asking");
    expect(await promptsShown(app!)).toBe(0);
    await client.reload();
    await client.waitForFunction(() => window.surogateDesktop !== undefined);
    const freed = free(CHAT);
    await press(await prompt(app!), "free");
    expect(await freed).toBe(true);
    // Now nothing is asked.
    expect(await outcome(write("a.txt", "a"))).toEqual({ ok: null });
    expect(await promptsShown(app!)).toBe(0);
  });

  it("asks one folder question and one Work-freely question at a time for a page", async () => {
    const client = await signedIn();
    await bound(client, folder);
    const first = prepare(client);
    const sheet = await prompt(app!);
    await expect(prepare(client)).rejects.toThrow("Surogate is already asking");
    const free = client.evaluate((id) => window.surogateDesktop!.requestFreeMode(id), CHAT);
    await expect(client.evaluate((id) => window.surogateDesktop!.requestFreeMode(id), CHAT)).rejects.toThrow("Surogate is already asking");
    await key(sheet, "Escape");
    expect(await first).toBeNull();
    await press(await prompt(app!), "keep");
    expect(await free).toBe(false);
    expect(await promptsShown(app!)).toBe(0);
  });

  it("can be made to ask every time by the page, and never to work freely", async () => {
    const client = await signedIn();
    await bound(client, folder, CHAT, "free");
    await expect(client.evaluate((id) => window.surogateDesktop!.setMode(id, "free" as "ask"), CHAT))
      .rejects.toThrow("Only the desktop can let a chat work freely");
    await client.evaluate((id) => window.surogateDesktop!.setMode(id, "ask"), CHAT);
    const id = write("a.txt", "a");
    await press(await prompt(app!), "deny");
    expect(await outcome(id)).toMatchObject({ error: { type: "os", code: "EACCES" } });
  });

  it("drops a confirmed folder whose chat was never created", async () => {
    const client = await signedIn();
    const prepared = prepare(client);
    await press(await prompt(app!), "accept");
    const ready = (await prepared)!;
    await client.evaluate((token) => window.surogateDesktop!.cancelPrepared(token), ready.token);
    expect(await outcome(send("bind", { folder: ready.folder, nonce: ready.nonce }))).toEqual({
      error: { type: "binding", message: "This folder was not confirmed on this computer" },
    });
  });
});
