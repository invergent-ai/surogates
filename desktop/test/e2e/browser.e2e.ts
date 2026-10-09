// The agent's browser through the real app: the shell's device, its binder and approvals, the
// desktop's own prompt, the browser host in its utility process, and the user's own browser.
// Behind SUROGATE_BROWSER_TESTS=1, apart from the user's session as the browser unit tests are:
//   npm run build && sh test/isolated.sh npx vitest run -c vitest.e2e.config.ts test/e2e/browser.e2e.ts
// with SUROGATE_TEST_BROWSER naming another browser to drive than Settings would choose.
// Skipped where no supported browser is installed. Settings → Browser launches none, and runs anywhere.
// The last of the tests through the app saves a download and reads an upload through the chat's file
// host, so it needs the folder's sandbox to start; and it clicks in the browser as its user does, by
// X events on that run's own display (x-user.py: python3 and libXtst), in the window xwininfo finds.

import { execFileSync } from "node:child_process";
import { mkdirSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { createServer, type Server } from "node:http";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import type { ElectronApplication, Page } from "playwright-core";
import { afterEach, beforeAll, beforeEach, describe, expect, it } from "vitest";

import { chosenBrowser, findBrowsers } from "../../src/browser/choose.js";
import { FILE_ASKED, NOT_ASKED } from "../../src/browser/host.js";
import { connect, FakeAgent, signedInAndAdded, webClient } from "./fake-agent.js";
import { isolated } from "../isolated.js";
import { handBack as handBackPrompt } from "../../src/shell/prompt-content.js";
import { WIDTH } from "../../src/shell/prompt-window.js";
import { dataHome, heldBack, key, launch, MAIN, press, prompt, promptsShown, quit, shellEnv, shellPage, stubNative } from "./launch.js";

const CHAT = "4e5f6a7b-8c9d-4e0f-a1b2-c3d4e5f6a7b8";
// Another chat of the agent's, where a test binds one.
const OTHER = "6f7a8b9c-0d1e-4f2a-b3c4-d5e6f7a8b9c0";
const THIRD = "7a8b9c0d-1e2f-4a3b-84c5-d6e7f8a9b0c1";
// The browser Settings would choose, or the one SUROGATE_TEST_BROWSER names, chosen in Settings before each launch.
const NAMED = process.env.SUROGATE_TEST_BROWSER;
const BROWSER = NAMED ? findBrowsers().find((browser) => browser.executable === NAMED) ?? null : chosenBrowser({ choice: "auto" }, findBrowsers());
const run = BROWSER !== null && process.env.SUROGATE_BROWSER_TESTS === "1";

let home: string;
let origin: string;
let agent: FakeAgent;
let app: ElectronApplication | undefined;
let canary: Server;
let canaryPort: number;
let hits: string[];
let next = 0;

beforeEach(async () => {
  home = dataHome();
  agent = new FakeAgent();
  hits = [];
  // This computer's own service, which the agent's browser must never reach.
  canary = createServer((req, res) => {
    hits.push(req.url ?? "");
    res.end("canary");
  });
  await new Promise<void>((done) => canary.listen(0, "127.0.0.1", () => done()));
  canaryPort = (canary.address() as { port: number }).port;
});

afterEach(async () => {
  await quit(app);
  app = undefined;
  await agent.stop();
  await agent.link.stop();
  await new Promise<void>((done) => canary.close(() => done()));
  rmSync(home, { recursive: true, force: true });
});

// A chat's operation, as the server sends it, and the app's result for it.
async function operation(kind: string, args: Record<string, unknown>, invocation = `call-${next + 1}`, ordinal = 1, chat = CHAT): Promise<any> {
  const id = `op-${(next += 1)}`;
  agent.link.send({
    type: "op", id, session_id: chat, calling_session_id: chat, invocation_id: invocation, ordinal, kind, args, digest: `d-${id}`,
  });
  let result: Record<string, unknown> | undefined;
  await expect.poll(() => {
    result = agent.link.received.find((frame) => frame.type === "op_result" && frame.id === id);
    return result !== undefined;
  }, { timeout: 60_000 }).toBe(true);
  agent.link.send({ type: "op_ack", id });
  return result?.outcome;
}

// The app launched and signed in, and *folder* bound to the chat in the desktop's own sheet. Each of
// *requires*, a script of the test's, runs in the app before its own code.
async function bound(folder: string, requires: string[] = []): Promise<Page> {
  origin = await agent.start();
  // The app's own environment is the browser's: apart from the user's session, or no launch.
  isolated(shellEnv(home));
  if (NAMED && BROWSER) {
    mkdirSync(join(home, "surogate"), { recursive: true });
    writeFileSync(join(home, "surogate", "browser.json"), JSON.stringify({ choice: BROWSER.id }));
  }
  app = await launch(home, {}, [], requires);
  await stubNative(app);
  const page = await shellPage(app);
  await connect(page, origin);
  await signedInAndAdded(app, page, agent);
  const client = await webClient(app, origin);
  await app.evaluate((_electron, picked) => Object.assign(globalThis, { folder: picked }), folder);
  const preparing = client.evaluate(() => window.surogateDesktop!.prepareFolder("pick")) as Promise<{ folder: string; nonce: string }>;
  await press(await prompt(app), "accept");
  const prepared = await preparing;
  expect(await operation("bind", { folder, nonce: prepared.nonce }, "bind", 0)).toEqual({ ok: null });
  return page;
}

// Another chat of the agent's bound on this computer too, to *folder*, in the desktop's own sheet.
async function alsoBound(client: Page, folder: string, chat = OTHER): Promise<void> {
  mkdirSync(folder);
  await app!.evaluate((_electron, picked) => Object.assign(globalThis, { folder: picked }), folder);
  const preparing = client.evaluate(() => window.surogateDesktop!.prepareFolder("pick")) as Promise<{ folder: string; nonce: string }>;
  await press(await prompt(app!), "accept");
  expect(await operation("bind", { folder, nonce: (await preparing).nonce }, "bind", 0, chat)).toEqual({ ok: null });
}

// A browser operation while its user holds the agent's browser.
const PAUSED = { error: { type: "paused_by_user", message: "The user took over the agent's browser on this computer" } };
// A hand back the page's own code asks for, with no click of its user's.
const HAND_BACK_AT_A_CLICK = "Surogate hands the agent's browser back only when its user asks, with a click";

// One of the bridge's calls about a chat's browser, begun as the page's own button begins it, at its
// user's click, each from a button of its own: what it answered, or why it was refused, once the page has heard.
let asks = 0;
async function clicked(client: Page, call: "show" | "takeOver" | "handBack" | "openSettings", chat = CHAT): Promise<() => Promise<unknown>> {
  const button = `ask-${(asks += 1)}`;
  await client.evaluate(([name, id, of]) => {
    const made = Object.assign(document.createElement("button"), { id: of, textContent: name });
    made.onclick = () => {
      const desktop = window.surogateDesktop!;
      // A call the desktop has not is answered as one it refused.
      Promise.resolve().then((): Promise<unknown> => (name === "openSettings" ? desktop.openSettings!("browser") : desktop.browser![name](id))).then(
        (answer) => (made.dataset.answer = JSON.stringify(answer ?? null)),
        (error: Error) => (made.dataset.answer = JSON.stringify(error.message)),
      );
    };
    document.body.append(made);
  }, [call, chat, button] as const);
  await client.click(`#${button}`);
  return async () => {
    await client.waitForFunction((of) => document.getElementById(of)?.dataset.answer !== undefined, button, { timeout: 15_000 });
    const answer = JSON.parse((await client.getAttribute(`#${button}`, "data-answer"))!) as unknown;
    await client.evaluate((of) => document.getElementById(of)?.remove(), button);
    return answer;
  };
}
const atClick = async (client: Page, call: Parameters<typeof clicked>[1], chat = CHAT) => (await clicked(client, call, chat))();

// The desktop's prompt, once it is up: one that names a chat comes once its title was read, or was not in time.
async function prompted(): Promise<Page> {
  await expect.poll(() => promptsShown(app!), { timeout: 10_000 }).toBe(1);
  return prompt(app!);
}

// What a prompt names, each in its field: the label, and the value as it is drawn.
const fields = (asked: Page) => asked.$$eval("#prompt-details .detail", (blocks) =>
  blocks.map((block) => [block.querySelector(".label")!.textContent, block.querySelector(".value")!.textContent]));

// A hand back asked at its user's click, and the desktop's confirmation answered with *button* once its
// input protection lets it: what the page was told.
async function handBackWith(client: Page, button: "hand_back" | "keep", chat = CHAT): Promise<unknown> {
  const answer = await clicked(client, "handBack", chat);
  await press(await prompted(), button);
  return answer();
}

// The app's own window: whether it is on the screen, and hidden as one closed to the tray is.
const windowShown = () => app!.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().find((window) => window.webContents.getURL().endsWith("/shell.html"))!.isVisible());
const hideWindow = () => app!.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().find((window) => window.webContents.getURL().endsWith("/shell.html"))!.hide());

// Whether Settings, or the project's dialog, is open over the window.
const over = (file: string) => app!.windows().some((window) => window.url().includes(file));

// A page of the chat's to bring to the front, stood in for where no browser runs: a script the app runs
// first. With it the app's tools say a chat's page was shown, and keep each chat they were asked to show,
// as globalThis.shown, once globalThis.paged says the stand-in is in place.
function paged(): string {
  const script = join(home, "paged.cjs");
  writeFileSync(script, [
    'const { pathToFileURL } = require("node:url");',
    "const shown = [];",
    "Object.assign(globalThis, { shown, paged: false });",
    `void import(pathToFileURL(${JSON.stringify(join(dirname(MAIN), "..", "browser", "executor.js"))}).href).then(({ Browsing }) => {`,
    "  Browsing.prototype.show = function (root) { shown.push(root); return Promise.resolve(true); };",
    "  Object.assign(globalThis, { paged: true });",
    "});",
  ].join("\n"));
  return script;
}

// The user's own hand on this run's Xvfb, as X events (x-user.py): a window given the keyboard, and a click
// on the screen. What they do in the agent's browser while they hold it reaches a page no other way.
const X_USER = fileURLToPath(new URL("../x-user.py", import.meta.url));
const asUser = (...args: string[]) => void execFileSync("python3", [X_USER, ...args]);
// The window of the agent's browser on that display, by the title of the tab in front, which its name begins with.
const browserWindow = (title: string): string | undefined => execFileSync("xwininfo", ["-root", "-tree"], { encoding: "utf8" }).split("\n")
  .flatMap((line) => (line.includes(`"${title} - `) ? /^\s+(0x[0-9a-f]+) /.exec(line)?.[1] ?? [] : []))[0];

// The browser's processes with a profile under the app's state.
const profiles = () => join(home, "surogate", "browser-profiles");
const browsers = () => readdirSync("/proc").filter((pid) => /^\d+$/.test(pid)).filter((pid) => {
  try {
    return readFileSync(`/proc/${pid}/cmdline`, "utf8").includes(profiles());
  } catch {
    return false;
  }
});

describe.skipIf(!run)("the agent's browser through the app", () => {
  beforeAll(() => isolated());

  it("asks the chat's first use in the desktop's window, drives the user's own browser in a profile of its own, and never reaches this computer's services", async () => {
    const folder = join(home, "project");
    mkdirSync(folder);
    await bound(folder);
    const navigating = operation("browser.navigate", { url: `http://127.0.0.1:${canaryPort}/`, wait_until: "load" });
    const asked = await prompt(app!);
    expect(await asked.textContent("#prompt-title")).toMatch(/^Let .+ use a browser on this computer\?$/);
    expect(await asked.getAttribute("#prompt-buttons", "data-held")).toBe("true");
    await press(asked, "allow_session");
    // This computer's own address: the proxy refuses it, and says so, though the browser is up with the chat's tab.
    expect(await navigating).toEqual({
      error: { type: "browser", message: `The agent's browser does not reach this computer's own services (127.0.0.1:${canaryPort})` },
    });
    expect(hits).toEqual([]);
    expect(readdirSync(profiles())).toHaveLength(1);
    expect(browsers().length).toBeGreaterThan(0);
    // The browser chosen: Settings' own, or the one the run names.
    expect(browsers().some((pid) => readFileSync(`/proc/${pid}/cmdline`, "utf8").startsWith(BROWSER!.executable))).toBe(true);
    // What the browser and Playwright keep while it runs goes under the identity's profiles, not the app's temp folder.
    const [identity] = readdirSync(profiles());
    expect(readdirSync(join(profiles(), identity!, "tmp")).some((name) => name.startsWith("playwright-artifacts-"))).toBe(true);
    expect(readdirSync(shellEnv(home).TMPDIR!).filter((name) => /playwright|chromium|chrome|edge/i.test(name))).toEqual([]);
    // Allowed for the chat: nothing asks again. A read and a screenshot come back whole.
    expect((await operation("browser.observe", { script: "snapshot@1", params: { selector: null } })).ok.frames).toEqual(expect.any(Array));
    const shot = await operation("browser.screenshot", { clip: null, labels: [] });
    expect(shot).toMatchObject({ ok: expect.any(String) });
    expect(Buffer.from(shot.ok as string, "base64").subarray(1, 4).toString("latin1")).toBe("PNG");
    expect(await promptsShown(app!)).toBe(0);
    // An app that is killed takes the browser with it.
    app!.process().kill("SIGKILL");
    app = undefined;
    await expect.poll(() => browsers().length, { timeout: 15_000 }).toBe(0);
  });

  it("answers the chat's browser operations as denied once its user denies the first use, and asks again at the next", async () => {
    const folder = join(home, "project");
    mkdirSync(folder);
    await bound(folder);
    const navigating = operation("browser.navigate", { url: "https://example.com/", wait_until: "load" });
    await press(await prompt(app!), "deny");
    expect(await navigating).toEqual({
      error: { type: "denied", message: "The user did not let the agent use the browser on this computer in this chat" },
    });
    expect(browsers()).toEqual([]);
    const again = operation("browser.observe", { script: "snapshot@1", params: { selector: null } });
    await press(await prompt(app!), "deny");
    expect((await again).error.type).toBe("denied");
  });

  it("forgets the browser's sign-ins at a log out its user asked to, and closes the browser first", async () => {
    const folder = join(home, "project");
    mkdirSync(folder);
    const page = await bound(folder);
    const navigating = operation("browser.navigate", { url: `http://127.0.0.1:${canaryPort}/`, wait_until: "load" });
    await press(await prompt(app!), "allow_session");
    await navigating;
    expect(readdirSync(profiles())).toHaveLength(1);
    await app!.evaluate(() => Object.assign(globalThis, { checked: true }));
    await page.click("#user");
    await page.click('[data-action="logout"]');
    // The identity's profiles go; another identity's would stay beside them.
    await expect.poll(() => readdirSync(profiles()), { timeout: 15_000 }).toEqual([]);
    expect(browsers()).toEqual([]);
    // And nothing of the browser writes it again after: the profile is forgotten whole.
    await new Promise((done) => setTimeout(done, 2_000));
    expect(readdirSync(profiles())).toEqual([]);
  });

  it("saves a page's download in the chat's folder and tells the agent, gives the page's file input a file of the folder, and is taken over and handed back: a download its user makes meanwhile is asked as theirs, and the agent told nothing of it", async () => {
    const folder = join(home, "project");
    mkdirSync(folder);
    const notes = join(folder, "notes.txt");
    writeFileSync(notes, "the chat's notes");
    await bound(folder);
    const client = await webClient(app!, origin);
    const binding = () => client.evaluate((chat) => window.surogateDesktop!.getBinding!(chat), CHAT);
    const saved = (name: string) => {
      try {
        return readFileSync(join(folder, "Downloads", name), "utf8");
      } catch {
        return null;
      }
    };
    const buttons = (asked: Page) => asked.$$eval("#prompt-buttons button", (drawn) => drawn.map((button) => [button.dataset.id, button.textContent]));
    // What the agent hears of what its page did, with its answers that say so: *first*, or its next ones, each a
    // move of the mouse, until one says something, for 15 s at most. An answer that refuses the move fails here
    // at once, shown as it is: not as a poll that ran out on reading what it has not.
    const hears = async (first: string[] = []): Promise<string[]> => {
      const said = [...first];
      for (const until = Date.now() + 15_000; said.length === 0 && Date.now() < until;) {
        const answer = await operation("browser.mouse", { action: "move", x: 1, y: 1 });
        expect(answer).toMatchObject({ ok: { notices: expect.any(Array) } });
        said.push(...answer.ok.notices);
        if (said.length === 0) await new Promise((done) => setTimeout(done, 100));
      }
      return said;
    };
    // The chat works freely, as the folder's sheet offers first: once its user has let the agent use the browser
    // here, nothing the agent does in it is asked about, a page's download and an upload among it.
    expect(await binding()).toEqual({ folder, mode: "free", takenOver: false });
    // The page needs no site: a script draws it in the chat's new tab, once the chat may use the browser. It has
    // a file input, and a link to a file the page made, large enough for a person's click.
    const drawn = operation("browser.evaluate", {
      code: `document.title = "Statements";
document.body.innerHTML = '<input id="file" type="file" style="position:fixed;left:10px;top:10px;width:200px;height:40px">'
  + '<a id="theirs" download="statement.txt" style="position:fixed;left:10px;top:80px;width:400px;height:240px;background:silver">Statement</a>';
document.getElementById("theirs").href = URL.createObjectURL(new Blob(["statement"]));
return 1;`,
    });
    await press(await prompt(app!), "allow_session");
    expect(await drawn).toEqual({ ok: { value: 1 } });

    // A download the page starts while the agent drives, before anyone has held the browser: the agent's. The
    // browser says no request of a file the page made, so this one is told from a user's own only by nobody
    // having held the browser, now or in the last minute: it comes before the take-over. It is saved under
    // Downloads in the chat's folder, unasked in a chat that works freely, and the agent hears where with its
    // next answer that says what the page did, once the save has answered.
    await operation("browser.evaluate", {
      code: `const link = document.createElement("a"); link.href = URL.createObjectURL(new Blob(["report"])); link.download = "report.txt";
document.body.append(link); link.click(); return 1;`,
    });
    expect(await hears()).toEqual(['The page downloaded "report.txt". It is saved in the chat\'s folder as Downloads/report.txt.']);
    expect(saved("report.txt")).toBe("report");
    expect(await promptsShown(app!)).toBe(0);

    // A file of the folder, given to the input the page asked for a file for: by its name, its type and what it holds.
    // Given only because the chat works freely. This page has no site: an upload its user is asked about is
    // refused for a frame that runs as none, before anyone is asked. Should uploads come to be asked about in a
    // chat that works freely too, these lines need a page of a site.
    const click = () => operation("browser.mouse", { action: "click", x: 50, y: 30, button: "left", clicks: 1 });
    expect(await hears((await click()).ok.notices)).toEqual([FILE_ASKED]);
    expect(await operation("browser.set_input_files", { paths: [notes] })).toEqual({ ok: { files: 1, notices: [] } });
    const given = `const [file] = document.getElementById("file").files; return file ? [file.name, file.type, await file.text()] : null;`;
    expect((await operation("browser.evaluate", { code: given })).ok.value).toEqual(["notes.txt", "text/plain", "the chat's notes"]);
    expect(await promptsShown(app!)).toBe(0);
    // The page asks again, as for one more file.
    expect(await hears((await click()).ok.notices)).toEqual([FILE_ASKED]);
    // Where the page's link is on the screen now, for its user's own click.
    const [x, y] = (await operation("browser.evaluate", {
      code: `const box = document.getElementById("theirs").getBoundingClientRect();
return [
  window.screenX + Math.round((window.outerWidth - window.innerWidth) / 2 + box.x + box.width / 2),
  window.screenY + (window.outerHeight - window.innerHeight) + Math.round(box.y + box.height / 2),
];`,
    })).ok.value as [number, number];

    // Taken over from the agent's page, at its user's click: the chat's page comes to the front, and the agent's
    // browser waits, in whatever it is asked.
    expect(await atClick(client, "takeOver")).toBeNull();
    expect(await binding()).toMatchObject({ takenOver: true });
    expect(await operation("browser.evaluate", { code: "return 2;" })).toEqual(PAUSED);
    expect(await operation("browser.set_input_files", { paths: [notes] })).toEqual(PAUSED);
    expect(await atClick(client, "show")).toBeNull();

    // Its user clicks the page's link themselves, in the browser they hold. The download is theirs: asked in a
    // chat that works freely too, in words that say when it came, with no "stop asking".
    // The click lands on the link only where the browser's window is above the app's at that place. On the
    // display these tests run on, which has no window manager, the order of this test gives that: nothing has
    // shown the app's window, or a prompt of its, since the browser's came to the front. Either would take the click.
    await expect.poll(() => browserWindow("Statements"), { timeout: 10_000 }).toBeDefined();
    asUser("focus", browserWindow("Statements")!);
    asUser("click", String(x), String(y));
    // A prompt is waited for until its buttons are drawn. The rest of what it draws is looked at until it is
    // there too, not once.
    const save = await prompted();
    await expect.poll(() => save.textContent("#prompt-title")).toBe("Save statement.txt?");
    await expect.poll(() => save.textContent("#prompt-lead")).toMatch(
      /^This file was downloaded while you had control of (.+)'s browser, or just after you handed it back, 9 bytes\. Save it in project\? \1 can read what is saved there\.$/,
    );
    await expect.poll(() => fields(save)).toEqual([["File", "Downloads/statement.txt"], ["New content, 9 bytes", "statement"]]);
    await expect.poll(() => buttons(save)).toEqual([["deny", "Deny"], ["allow", "Save"]]);
    await press(save, "allow");
    await expect.poll(() => saved("statement.txt"), { timeout: 15_000 }).toBe("statement");

    // Handed back only at its user's click, and then at the desktop's own confirmation, Keep control where the
    // keyboard starts and Hand back taken only once its input protection has passed: the page's own code hands
    // nothing back.
    await expect(client.evaluate((chat) => window.surogateDesktop!.browser!.handBack(chat), CHAT)).rejects.toThrow(HAND_BACK_AT_A_CLICK);
    expect(await promptsShown(app!)).toBe(0);
    const answer = await clicked(client, "handBack");
    const confirmation = await prompted();
    await expect.poll(() => confirmation.textContent("#prompt-title")).toMatch(/^Hand the browser back to .+\?$/);
    await expect.poll(() => buttons(confirmation)).toEqual([["keep", "Keep control"], ["hand_back", "Hand back"]]);
    await expect.poll(() => confirmation.evaluate(() => (document.activeElement as HTMLElement).dataset.id)).toBe("keep");
    await press(confirmation, "hand_back");
    expect(await answer()).toBe("confirmed");
    expect(await binding()).toMatchObject({ takenOver: false });

    // The agent drives again. It hears nothing of its user's download; and the input that asked before they took
    // the browser over is not the agent's to fill now: the page is given nothing until it asks again.
    expect(await operation("browser.evaluate", { code: "return 2;" })).toEqual({ ok: { value: 2 } });
    expect((await operation("browser.mouse", { action: "move", x: 1, y: 1 })).ok.notices).toEqual([]);
    expect(await operation("browser.set_input_files", { paths: [notes] })).toEqual({ error: { type: "browser", message: NOT_ASKED } });
    expect((await operation("browser.evaluate", { code: given })).ok.value).toEqual(["notes.txt", "text/plain", "the chat's notes"]);
    expect(readdirSync(join(folder, "Downloads")).sort()).toEqual(["report.txt", "statement.txt"]);
    expect(await promptsShown(app!)).toBe(0);
  }, 120_000);
});

// The app launched and signed in, its Settings open at Browser.
async function browserSettings(): Promise<Page> {
  const origin = await agent.start();
  app = await launch(home);
  await stubNative(app);
  const page = await shellPage(app);
  await connect(page, origin);
  await signedInAndAdded(app, page, agent);
  await page.click("#user");
  await page.click('[data-action="settings"]');
  let settings: Page | undefined;
  await expect.poll(() => {
    settings = app!.windows().find((window) => window.url().endsWith("/settings.html"));
    return settings !== undefined;
  }).toBe(true);
  await settings!.waitForSelector('.settings-nav [data-section="browser"]');
  await settings!.click('.settings-nav [data-section="browser"]');
  return settings!;
}

const kept = () => {
  try {
    return readFileSync(join(home, "surogate", "browser.json"), "utf8");
  } catch {
    return "";
  }
};

describe.skipIf(!run)("Custom… in Settings → Browser", () => {
  beforeAll(() => isolated());

  it("keeps a program the user picked only once it launched as a browser, and says why one that did not, or cannot be read, is not kept", async () => {
    isolated(shellEnv(home));
    const settings = await browserSettings();
    // Nothing kept without a pick: Custom… alone names no program.
    await settings.evaluate(() => (window as unknown as { surogateSettings: { set(key: string, value: string): Promise<void> } }).surogateSettings.set("browser", "custom"));
    expect(kept()).not.toContain("custom");
    const pick = async (path: string) => {
      await app!.evaluate((_electron, picked) => Object.assign(globalThis, { folder: picked }), path);
      await settings.selectOption("#browser", "pick");
    };
    // A program that does not launch as a browser, within its launch's bound.
    await pick("/bin/true");
    await expect.poll(() => settings.textContent("#browser-note"), { timeout: 40_000 }).toMatch(/^\/(usr\/)?bin\/true did not start as a browser Surogate can drive: /);
    expect(kept()).not.toContain("custom");
    // A path that leads nowhere.
    await pick(join(home, "gone"));
    await expect.poll(() => settings.textContent("#browser-note")).toBe(`Surogate cannot use ${join(home, "gone")}: it cannot be read here.`);
    expect(kept()).not.toContain("custom");
    // The user's own browser, launched once to see that it runs: kept, for the next launch.
    await pick(BROWSER!.executable);
    await expect.poll(kept, { timeout: 40_000 }).toContain('"choice": "custom"');
    expect(JSON.parse(kept()).executable).toBe(BROWSER!.executable);
    expect(await settings.textContent("#browser-note")).not.toMatch(/cannot use|did not start/);
  }, 120_000);
});

describe("a chat's browser taken over, and handed back", () => {
  const SHOW_AT_A_CLICK = "Surogate shows the agent's browser only when its user asks, with a click";
  const SETTINGS_AT_A_CLICK = "Surogate opens its Settings only when its user asks, with a click";
  const LEAD = "It will act in its browser on this computer again, in every chat.";
  const HAND_BACK = '[data-id="hand_back"]';
  // The native boxes the desktop has opened: the hand back's confirmation is none of them.
  const boxes = () => app!.evaluate(() => (globalThis as unknown as { asked: unknown[] }).asked);

  it("answers the chat's browser operations paused while its user holds the browser, tells the page, and hands it back only at its user's click and the desktop's own confirmation", async () => {
    const folder = join(home, "project");
    mkdirSync(folder);
    await bound(folder);
    const client = await webClient(app!, origin);
    await client.evaluate(() => {
      const heard: string[] = [];
      Object.assign(window, { heard });
      window.surogateDesktop!.onBindingChanged!((chat) => heard.push(chat));
    });
    const binding = () => client.evaluate((chat) => window.surogateDesktop!.getBinding!(chat), CHAT);
    const heard = () => client.evaluate(() => (window as unknown as { heard: string[] }).heard);
    const takeOver = () => client.evaluate((chat) => window.surogateDesktop!.browser!.takeOver(chat), CHAT);
    // A hand back the page's own code asks for, with no click of its user's.
    const handBack = () => client.evaluate((chat) => window.surogateDesktop!.browser!.handBack(chat), CHAT);
    expect(await binding()).toMatchObject({ takenOver: false });
    await takeOver();
    expect(await binding()).toMatchObject({ folder, takenOver: true });
    await expect.poll(heard, { timeout: 10_000 }).toEqual([CHAT]);
    // Paused: answered at once, the user asked nothing, and no browser started for it.
    expect(await operation("browser.navigate", { url: "https://example.com/", wait_until: "load" })).toEqual(PAUSED);
    expect(await operation("browser.close", {})).toEqual(PAUSED);
    expect(await promptsShown(app!)).toBe(0);
    expect(browsers()).toEqual([]);
    // The page's own code asks to hand it back, with no click of its user's: refused, however often, and
    // nothing is asked for it.
    const before = (await boxes()).length;
    for (let n = 0; n < 3; n += 1) await expect(handBack()).rejects.toThrow(HAND_BACK_AT_A_CLICK);
    expect(await promptsShown(app!)).toBe(0);
    expect(await binding()).toMatchObject({ takenOver: true });
    // At its user's click the desktop asks, in a prompt of its own: Keep control first, where the keyboard
    // starts, and Hand back held back as it opens. A chat the agent names not has no field for its name.
    const answer = await clicked(client, "handBack");
    const asked = await prompted();
    expect(await asked.textContent("#prompt-title")).toMatch(/^Hand the browser back to .+\?$/);
    expect(await asked.textContent("#prompt-lead")).toBe(`${LEAD} It was taken over from this chat.`);
    expect(await asked.$$eval("#prompt-buttons button", (buttons) => buttons.map((button) => [button.dataset.id, button.textContent]))).toEqual([
      ["keep", "Keep control"], ["hand_back", "Hand back"],
    ]);
    expect(await asked.evaluate(() => (document.activeElement as HTMLElement).dataset.id)).toBe("keep");
    expect(await fields(asked)).toEqual([]);
    // Kept: still the user's.
    await press(asked, "keep");
    expect(await answer()).toBe(false);
    expect(await binding()).toMatchObject({ takenOver: true });
    // Kept: the page's own code is asked nothing still, nor after a take-over it makes again.
    await expect(handBack()).rejects.toThrow(HAND_BACK_AT_A_CLICK);
    await takeOver();
    await expect(handBack()).rejects.toThrow(HAND_BACK_AT_A_CLICK);
    expect(await promptsShown(app!)).toBe(0);
    expect(await binding()).toMatchObject({ takenOver: true });
    // A new click of its user's asks again, and they can still keep it; at the next they hand it back.
    expect(await handBackWith(client, "keep")).toBe(false);
    expect(await binding()).toMatchObject({ takenOver: true });
    expect(await handBackWith(client, "hand_back")).toBe("confirmed");
    expect(await binding()).toMatchObject({ takenOver: false });
    await expect.poll(heard, { timeout: 10_000 }).toEqual([CHAT, CHAT, CHAT]);
    // Handed back, the chat's browser asks its first use, as before.
    const navigating = operation("browser.navigate", { url: "https://example.com/", wait_until: "load" });
    await press(await prompt(app!), "deny");
    expect((await navigating).error.type).toBe("denied");
    // Taken over anew, the page's own code is refused as before: only its user's click asks.
    await takeOver();
    await expect(handBack()).rejects.toThrow(HAND_BACK_AT_A_CLICK);
    expect(await promptsShown(app!)).toBe(0);
    expect(await handBackWith(client, "hand_back")).toBe("confirmed");
    // Not one native box in all of it: the confirmation is the desktop's own window.
    expect(await boxes()).toHaveLength(before);
  });

  it("takes nothing that would hand the browser back until the confirmation's input protection has passed: not keys, not a click, not its page's own word", async () => {
    const folder = join(home, "project");
    mkdirSync(folder);
    await bound(folder);
    const client = await webClient(app!, origin);
    await client.evaluate((chat) => window.surogateDesktop!.browser!.takeOver(chat), CHAT);
    const answer = await clicked(client, "handBack");
    const asked = await prompted();
    expect(await asked.getAttribute("#prompt-buttons", "data-held")).toBe("true");
    // As it opens. Tab then Space, as a form invites: Tab moves the keyboard, as in any window, and nothing is pressed.
    await asked.keyboard.press("Tab");
    await asked.keyboard.press(" ");
    // A click where Hand back is.
    await asked.click(HAND_BACK, { force: true, noWaitAfter: true });
    // And the prompt's own page saying it was pressed, right after those: the main process goes by its own count.
    expect(await asked.evaluate(() =>
      (window as unknown as { surogatePrompt: { answer(button: string, choice: string | null): Promise<boolean> } }).surogatePrompt.answer("hand_back", null))).toBe(false);
    // Nothing came of any: the confirmation is up still, the browser its user's, and the page not answered.
    await expect.poll(() => asked.getAttribute("#prompt-buttons", "data-held"), { timeout: 10_000 }).toBe("false");
    expect(await promptsShown(app!)).toBe(1);
    expect(await client.evaluate((chat) => window.surogateDesktop!.getBinding!(chat), CHAT)).toMatchObject({ takenOver: true });
    expect(await client.evaluate(() => [...document.querySelectorAll<HTMLElement>("button[id^=ask-]")].map((button) => button.dataset.answer ?? null))).toEqual([null]);
    // Once it has passed, Hand back hands it back.
    await press(asked, "hand_back");
    expect(await answer()).toBe("confirmed");
    expect(await client.evaluate((chat) => window.surogateDesktop!.getBinding!(chat), CHAT)).toMatchObject({ takenOver: false });
  });

  it("answers the hand back's confirmation at a button pressed with no key and no press, as assistive technology presses one, and by the keyboard alone: Keep control keeps the browser, Hand back hands it back", async () => {
    const folder = join(home, "project");
    mkdirSync(folder);
    await bound(folder);
    const client = await webClient(app!, origin);
    const activate = (asked: Page, button: string, simulated: boolean) => asked.evaluate(([id, mouse]) => {
      const pressed = document.querySelector<HTMLButtonElement>(`#prompt-buttons button[data-id="${id}"]`)!;
      if (!mouse) return pressed.click();
      for (const type of ["mousedown", "mouseup", "click"]) pressed.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, view: window }));
    }, [button, simulated] as const).catch(() => {});
    for (const simulated of [false, true]) {
      await client.evaluate((chat) => window.surogateDesktop!.browser!.takeOver(chat), CHAT);
      let answer = await clicked(client, "handBack");
      let asked = await prompted();
      // No button is said to be unavailable, held back or not.
      expect(await asked.$$eval("#prompt-buttons button", (buttons) => buttons.filter((button) => button.hasAttribute("aria-disabled")).length)).toBe(0);
      await expect.poll(() => heldBack(asked)).toBe(false);
      await activate(asked, "keep", simulated);
      expect(await answer()).toBe(false);
      answer = await clicked(client, "handBack");
      asked = await prompted();
      await expect.poll(() => heldBack(asked)).toBe(false);
      await activate(asked, "hand_back", simulated);
      expect(await answer()).toBe("confirmed");
    }
    // By the keyboard alone: Tab to Hand back, and Enter once the protection has passed since Tab.
    await client.evaluate((chat) => window.surogateDesktop!.browser!.takeOver(chat), CHAT);
    const answer = await clicked(client, "handBack");
    const asked = await prompted();
    await expect.poll(() => heldBack(asked)).toBe(false);
    await asked.keyboard.press("Tab");
    expect(await asked.evaluate(() => (document.activeElement as HTMLElement).dataset.id)).toBe("hand_back");
    await new Promise((resolve) => setTimeout(resolve, 650));
    await asked.keyboard.press("Enter").catch(() => {});
    expect(await answer()).toBe("confirmed");
  });

  it("answers the hand back's confirmation with no key and no press of a person who is typing: keys one after the other, a key held down, a click, and a press begun before it showed take nothing, each holding it back anew; and a choice made after a quiet input protection answers", async () => {
    const folder = join(home, "project");
    mkdirSync(folder);
    await bound(folder);
    const client = await webClient(app!, origin);
    await client.evaluate((chat) => window.surogateDesktop!.browser!.takeOver(chat), CHAT);
    const answer = await clicked(client, "handBack");
    const asked = await prompted();
    const sleep = (ms: number) => new Promise((done) => setTimeout(done, ms));
    // Whether the confirmation is up still, the browser its user's, and whether it is held back.
    const state = async () => [
      await promptsShown(app!), (await client.evaluate((chat) => window.surogateDesktop!.getBinding!(chat), CHAT))?.takenOver, await heldBack(asked),
    ];
    // Typed for the agent's browser, a key every 150 ms for three seconds: six times the input protection, which
    // each key begins anew. Tab then Return, as a form takes them; Space; Escape; letters. Tab moves the keyboard,
    // as in any window, onto Hand back and off it again; and none answers, wherever the keyboard is.
    for (let n = 0; n < 4; n += 1) {
      for (const name of ["a", "Tab", "Enter", " ", "Enter", "Escape"]) {
        await asked.keyboard.press(name);
        await sleep(150);
      }
    }
    expect(await state()).toEqual([1, true, true]);
    // The main process counts those keys by itself: its page's word that Hand back was pressed, right after one of
    // them, is not taken, though the confirmation has been up six times its protection.
    const word = () => asked.evaluate(() =>
      (window as unknown as { surogatePrompt: { answer(button: string, choice: string | null): Promise<boolean> } }).surogatePrompt.answer("hand_back", null));
    expect(await word()).toBe(false);
    // A key held down: it repeats for two seconds, well past the protection, and answers nothing.
    await asked.keyboard.down("Enter");
    for (let n = 0; n < 40; n += 1) {
      await sleep(50);
      await asked.keyboard.down("Enter");
    }
    // Nor does a repeat that comes a quiet protection time after the one before, as a key's first repeat can.
    await sleep(700);
    await asked.keyboard.down("Enter");
    expect(await word()).toBe(false);
    await asked.keyboard.up("Enter");
    expect(await state()).toEqual([1, true, true]);
    // A click on Hand back while still typing: nothing.
    await asked.keyboard.press("Tab");
    await asked.click(HAND_BACK, { force: true, noWaitAfter: true });
    expect(await state()).toEqual([1, true, true]);
    // A press begun before the confirmation was there, in the page under it, and let go over Hand back: no press of the confirmation's.
    await sleep(700);
    expect(await heldBack(asked)).toBe(false);
    const box = (await asked.locator(HAND_BACK).boundingBox())!;
    await asked.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await asked.mouse.up();
    await sleep(200);
    expect(await state()).toEqual([1, true, false]);
    // Its user has stopped, and chooses: Tab moves the keyboard to Hand back, which holds the confirmation back
    // anew, so that a Return right after it takes nothing; a Return after a quiet protection time hands the browser back.
    const on = () => asked.evaluate(() => (document.activeElement as HTMLElement).dataset.id);
    for (let n = 0; n < 4 && (n === 0 || (await on()) !== "hand_back"); n += 1) await asked.keyboard.press("Tab");
    expect([await on(), await state()]).toEqual(["hand_back", [1, true, true]]);
    await asked.keyboard.press("Enter");
    await sleep(200);
    expect([await on(), await state()]).toEqual(["hand_back", [1, true, true]]);
    await key(asked, "Enter");
    expect(await answer()).toBe("confirmed");
    expect(await client.evaluate((chat) => window.surogateDesktop!.getBinding!(chat), CHAT)).toMatchObject({ takenOver: false });
  });

  it("keeps the browser its user's at Escape, at Enter as the confirmation opens, and when it is closed some other way", async () => {
    const folder = join(home, "project");
    mkdirSync(folder);
    await bound(folder);
    const client = await webClient(app!, origin);
    await client.evaluate((chat) => window.surogateDesktop!.browser!.takeOver(chat), CHAT);
    const kept: Array<[string, (asked: Page) => Promise<unknown>]> = [
      ["Escape", (asked) => key(asked, "Escape")],
      // Enter, once the confirmation may be answered, lands on Keep control, where the keyboard starts.
      ["Enter", (asked) => key(asked, "Enter")],
      ["closed", () => app!.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows().find((window) => window.webContents.getURL().endsWith("/prompt.html"))!.close())],
    ];
    for (const [how, keep] of kept) {
      const answer = await clicked(client, "handBack");
      await keep(await prompted());
      expect(await answer(), how).toBe(false);
      expect(await client.evaluate((chat) => window.surogateDesktop!.getBinding!(chat), CHAT), how).toMatchObject({ takenOver: true });
      await expect.poll(() => promptsShown(app!), { timeout: 10_000 }).toBe(0);
    }
  });

  it("closes the confirmation when the page that asked loads again, and keeps the browser its user's", async () => {
    const folder = join(home, "project");
    mkdirSync(folder);
    await bound(folder);
    const client = await webClient(app!, origin);
    await client.evaluate((chat) => window.surogateDesktop!.browser!.takeOver(chat), CHAT);
    void (await clicked(client, "handBack"))().catch(() => {});
    const asked = await prompted();
    await expect.poll(() => asked.getAttribute("#prompt-buttons", "data-held"), { timeout: 10_000 }).toBe("false");
    // The page under it loads again, by its own code or its user's reload: nobody is left to answer for.
    await Promise.all([client.waitForEvent("load", { timeout: 15_000 }), client.evaluate(() => location.reload()).catch(() => {})]);
    await expect.poll(() => app!.evaluate(({ BrowserWindow }) =>
      BrowserWindow.getAllWindows().filter((window) => window.webContents.getURL().endsWith("/prompt.html")).length), { timeout: 10_000 }).toBe(0);
    await client.waitForFunction(() => window.surogateDesktop !== undefined, undefined, { timeout: 15_000 });
    expect(await client.evaluate((chat) => window.surogateDesktop!.getBinding!(chat), CHAT)).toMatchObject({ takenOver: true });
    // The new page asks afresh, at its user's click.
    expect(await handBackWith(client, "hand_back")).toBe("confirmed");
  });

  it("asks one hand back at a time: a second while the confirmation is up is refused, and changes nothing of it", async () => {
    const folder = join(home, "project");
    mkdirSync(folder);
    await bound(folder);
    const client = await webClient(app!, origin);
    await client.evaluate((chat) => window.surogateDesktop!.browser!.takeOver(chat), CHAT);
    const first = await clicked(client, "handBack");
    const asked = await prompted();
    await expect.poll(() => asked.getAttribute("#prompt-buttons", "data-held"), { timeout: 10_000 }).toBe("false");
    await asked.evaluate(() => Object.assign(window, { drawn: document.querySelector("#prompt-buttons button") }));
    // Its user's next click in the page asks again: refused, and no second prompt waits behind the first.
    expect(await atClick(client, "handBack")).toContain("Surogate is already asking");
    expect(await promptsShown(app!)).toBe(1);
    expect(await asked.textContent("#prompt-waiting")).toBe("");
    // The one up is as it was: not drawn anew, and not held back anew.
    expect(await asked.evaluate(() => (window as unknown as { drawn: Element }).drawn === document.querySelector("#prompt-buttons button"))).toBe(true);
    expect(await asked.getAttribute("#prompt-buttons", "data-held")).toBe("false");
    await press(asked, "hand_back");
    expect(await first()).toBe("confirmed");
  });

  it("holds the agent's browser for every chat from the chat that took it over: another chat's browser calls wait, and it can neither take the browser nor hand it back", async () => {
    const HELD_FROM_ANOTHER_CHAT = "The agent's browser on this computer is taken over from another chat, and is handed back there";
    const folder = join(home, "project");
    mkdirSync(folder);
    await bound(folder);
    const client = await webClient(app!, origin);
    const second = join(home, "second");
    await alsoBound(client, second);
    const binding = (chat: string) => client.evaluate((id) => window.surogateDesktop!.getBinding!(id), chat);
    await client.evaluate((chat) => window.surogateDesktop!.browser!.takeOver(chat), CHAT);
    expect(await binding(CHAT)).toMatchObject({ takenOver: true });
    // The other chat does not hold the browser, and its page is told where it is held: neither "from this chat"
    // nor "by nobody". Its agent's browser calls wait all the same: answered at once, its user asked nothing.
    expect(await binding(OTHER)).toMatchObject({ folder: second, takenOver: "elsewhere" });
    expect(await operation("browser.navigate", { url: "https://example.com/", wait_until: "load" }, undefined, 1, OTHER)).toEqual(PAUSED);
    expect(await promptsShown(app!)).toBe(0);
    // Its take-over does not steal the browser, and it hands nothing back: each says where it is held, at its
    // user's click too, and nothing is asked.
    expect(await atClick(client, "takeOver", OTHER)).toContain(HELD_FROM_ANOTHER_CHAT);
    expect(await atClick(client, "handBack", OTHER)).toContain(HELD_FROM_ANOTHER_CHAT);
    expect(await promptsShown(app!)).toBe(0);
    expect(await binding(OTHER)).toMatchObject({ takenOver: "elsewhere" });
    expect(await binding(CHAT)).toMatchObject({ takenOver: true });
    expect(await operation("browser.close", {}, undefined, 1, OTHER)).toEqual(PAUSED);
    // Handed back from the chat that holds it: nobody holds it, as the other chat's page is told, and its
    // browser asks its first use, as any chat's.
    expect(await handBackWith(client, "hand_back")).toBe("confirmed");
    expect(await binding(OTHER)).toMatchObject({ takenOver: false });
    const navigating = operation("browser.navigate", { url: "https://example.com/", wait_until: "load" }, undefined, 1, OTHER);
    await press(await prompt(app!), "deny");
    expect((await navigating).error.type).toBe("denied");
    // Nothing is held now: a hand back has nothing to ask.
    expect(await atClick(client, "handBack", OTHER)).toBe("released");
    expect(await promptsShown(app!)).toBe(0);
  });

  it("keeps the agent's browser held when the chat it is held from is deleted, and hands it back from any chat then, only at its user's click and the desktop's own confirmation", async () => {
    const folder = join(home, "project");
    mkdirSync(folder);
    // The chat that will ask has a name: the confirmation shows none of it, for the browser was not taken over from it.
    agent.titles.set(OTHER, "Invoices");
    await bound(folder);
    const client = await webClient(app!, origin);
    await alsoBound(client, join(home, "second"));
    const binding = (chat: string) => client.evaluate((id) => window.surogateDesktop!.getBinding!(id), chat);
    const navigated = () => operation("browser.navigate", { url: "https://example.com/", wait_until: "load" }, undefined, 1, OTHER);
    await client.evaluate((chat) => window.surogateDesktop!.browser!.takeOver(chat), CHAT);
    // The chat it is held from is deleted, as a page can have one deleted: that hands nothing back.
    expect(await operation("retire", {}, "retire", 0, CHAT)).toEqual({ ok: null });
    expect(await binding(CHAT)).toBeNull();
    expect(await navigated()).toEqual(PAUSED);
    // No chat holds it now, and a chat's page can tell: neither held from it, nor free.
    expect(await binding(OTHER)).toMatchObject({ takenOver: "orphaned" });
    // The page's own code hands nothing back for the other chat either, and nothing is asked for it.
    await expect(client.evaluate((chat) => window.surogateDesktop!.browser!.handBack(chat), OTHER)).rejects.toThrow(HAND_BACK_AT_A_CLICK);
    expect(await promptsShown(app!)).toBe(0);
    expect(await navigated()).toEqual(PAUSED);
    // At its user's click the desktop asks, saying that the chat it was taken over from is gone: they keep it, held still.
    const answer = await clicked(client, "handBack", OTHER);
    const asked = await prompted();
    expect(await asked.textContent("#prompt-lead")).toBe(`${LEAD} The chat it was taken over from is gone.`);
    expect(await fields(asked)).toEqual([]);
    await press(asked, "keep");
    expect(await answer()).toBe(false);
    expect(await binding(OTHER)).toMatchObject({ takenOver: "orphaned" });
    expect(await navigated()).toEqual(PAUSED);
    // At the next they hand it back: the agent's browser is every chat's again, asking its first use as any.
    // Not the hand back of a chat that held it: the page is told so, and gives no agent a turn for it.
    expect(await handBackWith(client, "hand_back", OTHER)).toBe("released");
    expect(await binding(OTHER)).toMatchObject({ takenOver: false });
    const navigating = navigated();
    await press(await prompt(app!), "deny");
    expect((await navigating).error.type).toBe("denied");
  });

  it("offers a Hand back in Settings → Browser for a browser held from a chat that is gone, where no chat is left with a page to hand it back in: through the desktop's own confirmation, at its user's click there; and offers none while the chat it is held from is here", async () => {
    const folder = join(home, "project");
    mkdirSync(folder);
    await bound(folder);
    const client = await webClient(app!, origin);
    const binding = (chat: string) => client.evaluate((id) => window.surogateDesktop!.getBinding!(id), chat);
    const navigated = () => operation("browser.navigate", { url: "https://example.com/", wait_until: "load" }, undefined, 1, OTHER);
    // Settings, opened at Browser from the agent's page at its user's click, as the page's own link opens it.
    const settingsAtBrowser = async (): Promise<Page> => {
      expect(await atClick(client, "openSettings")).toBeNull();
      let settings: Page | undefined;
      await expect.poll(() => {
        settings = app!.windows().find((window) => window.url().includes("/settings.html"));
        return settings !== undefined;
      }, { timeout: 10_000 }).toBe(true);
      await settings!.waitForSelector("#browser option", { state: "attached" });
      return settings!;
    };
    await client.evaluate((chat) => window.surogateDesktop!.browser!.takeOver(chat), CHAT);
    // Held from a chat that is here: that chat's own page hands it back, and Settings offers nothing.
    let settings = await settingsAtBrowser();
    expect(await settings.isHidden("#browser-held")).toBe(true);
    await settings.keyboard.press("Escape").catch(() => {});
    await expect.poll(() => over("/settings.html"), { timeout: 10_000 }).toBe(false);
    // That chat is deleted, as a page can have one deleted. Another chat is here, which never opened the browser:
    // its page has no browser pane, so nothing in it hands the browser back, and its browser calls wait.
    expect(await operation("retire", {}, "retire", 0, CHAT)).toEqual({ ok: null });
    expect(await binding(CHAT)).toBeNull();
    await alsoBound(client, join(home, "second"));
    expect(await navigated()).toEqual(PAUSED);
    // Settings says so, and offers the hand back itself.
    settings = await settingsAtBrowser();
    await expect.poll(() => settings.isVisible("#browser-held")).toBe(true);
    expect(await settings.textContent("#browser-held .desc")).toBe(
      "You took the agent's browser over from a chat that is gone. Until you hand it back, the agent's browser waits in every chat.",
    );
    // At its user's click there the desktop asks, as it does from a chat's page: Keep control keeps it.
    await settings.click("#browser-hand-back");
    let asked = await prompted();
    expect(await asked.textContent("#prompt-title")).toMatch(/^Hand the browser back to .+\?$/);
    expect(await asked.textContent("#prompt-lead")).toBe(`${LEAD} The chat it was taken over from is gone.`);
    expect(await asked.evaluate(() => (document.activeElement as HTMLElement).dataset.id)).toBe("keep");
    await press(asked, "keep");
    await expect.poll(() => promptsShown(app!)).toBe(0);
    expect([await settings.isVisible("#browser-held"), await navigated()]).toEqual([true, PAUSED]);
    expect(await settings.isHidden("#browser-handed-back")).toBe(true);
    // Hand back hands it back: Settings offers it no more, the chat's page is told, and the agent's browser is
    // every chat's again, asking its first use as any.
    await settings.click("#browser-hand-back");
    asked = await prompted();
    await press(asked, "hand_back");
    await expect.poll(() => settings.isHidden("#browser-held")).toBe(true);
    expect(await binding(OTHER)).toMatchObject({ takenOver: false });
    // No agent is given a turn from here, and Settings says so.
    await expect.poll(() => settings.isVisible("#browser-handed-back")).toBe(true);
    expect(await settings.textContent("#browser-handed-back")).toBe(
      "The browser is the agent's again. The agent does not go on by itself: write to it in a chat to go on.",
    );
    await settings.keyboard.press("Escape").catch(() => {});
    const navigating = navigated();
    await press(await prompt(app!), "deny");
    expect((await navigating).error.type).toBe("denied");
  });

  it("tells the page nothing was handed back when the browser was taken over from another chat while the confirmation was up", async () => {
    const folder = join(home, "project");
    mkdirSync(folder);
    await bound(folder);
    const client = await webClient(app!, origin);
    await alsoBound(client, join(home, "second"));
    await alsoBound(client, join(home, "third"), THIRD);
    const binding = (chat: string) => client.evaluate((id) => window.surogateDesktop!.getBinding!(id), chat);
    await client.evaluate((chat) => window.surogateDesktop!.browser!.takeOver(chat), CHAT);
    // The chat it is held from is deleted: any chat may ask to hand it back, and one does, at its user's click.
    expect(await operation("retire", {}, "retire", 0, CHAT)).toEqual({ ok: null });
    const answer = await clicked(client, "handBack", OTHER);
    const asked = await prompted();
    // While the confirmation is up, the page's own code takes the browser over from a third chat, which needs no click.
    await client.evaluate((chat) => window.surogateDesktop!.browser!.takeOver(chat), THIRD);
    expect(await binding(THIRD)).toMatchObject({ takenOver: true });
    // Hand back, for the chat that asked: the browser is the third chat's to hand back now, so nothing is, and the page is told so.
    await press(asked, "hand_back");
    expect(await answer()).toBe(false);
    expect(await binding(THIRD)).toMatchObject({ takenOver: true });
    expect(await binding(OTHER)).toMatchObject({ takenOver: "elsewhere" });
    expect(await operation("browser.navigate", { url: "https://example.com/", wait_until: "load" }, undefined, 1, OTHER)).toEqual(PAUSED);
  });

  it("asks nothing for a page that loads itself again and asks to hand the browser back, before its user kept it or after", async () => {
    const folder = join(home, "project");
    mkdirSync(folder);
    await bound(folder);
    const client = await webClient(app!, origin);
    await client.evaluate((chat) => window.surogateDesktop!.browser!.takeOver(chat), CHAT);
    // The page's own code loads the page again, then asks: what it was answered.
    const reloadedAndAsked = async () => {
      await Promise.all([client.waitForEvent("load", { timeout: 15_000 }), client.evaluate(() => location.reload()).catch(() => {})]);
      await client.waitForFunction(() => window.surogateDesktop !== undefined, undefined, { timeout: 15_000 });
      return client.evaluate((chat) => window.surogateDesktop!.browser!.handBack(chat).then(String, (error: Error) => error.message), CHAT);
    };
    for (let n = 0; n < 3; n += 1) expect(await reloadedAndAsked()).toBe(HAND_BACK_AT_A_CLICK);
    expect(await promptsShown(app!)).toBe(0);
    // Kept at its user's click, and the same after it.
    expect(await handBackWith(client, "keep")).toBe(false);
    for (let n = 0; n < 3; n += 1) expect(await reloadedAndAsked()).toBe(HAND_BACK_AT_A_CLICK);
    expect(await promptsShown(app!)).toBe(0);
    expect(await client.evaluate((chat) => window.surogateDesktop!.getBinding!(chat), CHAT)).toMatchObject({ takenOver: true });
  });

  it("names the chat in the hand back's confirmation in a field of its own, as text, and shows a hidden window before it asks", async () => {
    const folder = join(home, "project");
    mkdirSync(folder);
    // A title the agent wrote, with a right-to-left override in it: shown as text.
    agent.titles.set(CHAT, "Quarterly‮report");
    await bound(folder);
    const client = await webClient(app!, origin);
    await client.evaluate((chat) => window.surogateDesktop!.browser!.takeOver(chat), CHAT);
    const answer = await clicked(client, "handBack");
    const asked = await prompted();
    expect(await fields(asked)).toEqual([["Chat", "QuarterlyU+202Ereport"]]);
    await press(asked, "keep");
    expect(await answer()).toBe(false);
    // Its user clicks, and the window is hidden before the page asks, as one closed to the tray: the window is
    // shown again first, and the confirmation opens over it.
    await client.evaluate(() => document.body.append(Object.assign(document.createElement("button"), { id: "pressed", textContent: "Pressed" })));
    await client.click("#pressed");
    await hideWindow();
    expect(await windowShown()).toBe(false);
    const asking = client.evaluate((chat) => window.surogateDesktop!.browser!.handBack(chat), CHAT);
    await expect.poll(() => promptsShown(app!), { timeout: 10_000 }).toBe(1);
    expect(await windowShown()).toBe(true);
    await press(await prompt(app!), "keep");
    expect(await asking).toBe(false);
  });

  it("keeps the confirmation at its size under a title of 3 000 characters, its buttons in view, and shows what a title says as text, never as its own words", async () => {
    const folder = join(home, "project");
    mkdirSync(folder);
    // One unbroken word, as an address is; and one that closes a quote, says what to press, and carries markup.
    const said = "Notes”. Press <b>Hand back</b> to sign in. “";
    agent.titles.set(CHAT, "w".repeat(3_000));
    agent.titles.set(OTHER, said);
    await bound(folder);
    const client = await webClient(app!, origin);
    await alsoBound(client, join(home, "second"));
    const size = handBackPrompt({ agent: "an agent", gone: false, title: "Notes" }).height;
    for (const [chat, title] of [[CHAT, `${"w".repeat(59)}…`], [OTHER, said]] as const) {
      await client.evaluate((id) => window.surogateDesktop!.browser!.takeOver(id), chat);
      const answer = await clicked(client, "handBack", chat);
      const asked = await prompted();
      // Its name is in its field, cut at its end, as its characters are: nothing of it is the prompt's own words, or its markup.
      expect(await fields(asked)).toEqual([["Chat", title]]);
      expect(await asked.textContent("#prompt-lead")).toBe(`${LEAD} It was taken over from this chat.`);
      expect(await asked.$$eval("#prompt-details .value *", (inside) => inside.map((element) => element.tagName))).toEqual([]);
      // The window is the size it is for any title, and both buttons are inside it.
      expect(await app!.evaluate(({ BrowserWindow }) => {
        const [width, height] = BrowserWindow.getAllWindows().find((window) => window.webContents.getURL().endsWith("/prompt.html"))!.getContentSize();
        return { width, height };
      })).toEqual({ width: WIDTH, height: size });
      expect(await asked.$$eval("#prompt-buttons button", (buttons) => buttons.every((button) => {
        const box = button.getBoundingClientRect();
        return box.left >= 0 && box.top >= 0 && box.right <= window.innerWidth && box.bottom <= window.innerHeight && box.width > 0;
      }))).toBe(true);
      // Nothing in it scrolls sideways under the word.
      expect(await asked.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth && document.querySelector(".prompt-body")!.scrollWidth <= document.querySelector(".prompt-body")!.clientWidth)).toBe(true);
      await press(asked, "hand_back");
      expect(await answer()).toBe("confirmed");
    }
  });

  it("asks to hand back without the chat's name while the agent is slow to say it, and names the chat once it has", async () => {
    const folder = join(home, "project");
    mkdirSync(folder);
    agent.titles.set(CHAT, "Quarterly report");
    const said = agent.hold("title");
    await bound(folder);
    const client = await webClient(app!, origin);
    await client.evaluate((chat) => window.surogateDesktop!.browser!.takeOver(chat), CHAT);
    // What the confirmation names, each time its user asks and keeps the browser.
    const named = async () => {
      const answer = await clicked(client, "handBack");
      const asked = await prompted();
      const names = await fields(asked);
      await press(asked, "keep");
      expect(await answer()).toBe(false);
      return names;
    };
    // Its user's click is not left waiting for the agent: the confirmation opens within the time a title is given.
    const began = Date.now();
    expect(await named()).toEqual([]);
    expect(Date.now() - began).toBeLessThan(10_000);
    said();
    await expect.poll(named, { timeout: 15_000, interval: 500 }).toEqual([["Chat", "Quarterly report"]]);
  });

  it("refuses the browser's calls for a chat this computer did not bind", async () => {
    const folder = join(home, "project");
    mkdirSync(folder);
    await bound(folder);
    const client = await webClient(app!, origin);
    const other = "5e6f7a8b-9c0d-4e1f-a2b3-c4d5e6f7a8b9";
    for (const call of ["show", "takeOver", "handBack"] as const) {
      expect(await atClick(client, call, other), call).toContain("This chat has no folder on this computer");
    }
  });

  it("shows the agent's browser and opens Settings only at its user's click: a page that keeps asking by itself brings nothing to the front", async () => {
    const folder = join(home, "project");
    mkdirSync(folder);
    await bound(folder);
    const client = await webClient(app!, origin);
    // The page's own code, asking again and again. Playwright's evaluate gives the page an activation, but no click of its user's.
    const said = await client.evaluate(async (chat) => {
      const desktop = window.surogateDesktop!;
      const answers: string[] = [];
      const answered = (asked: Promise<unknown>) => asked.then(() => "done", (error: Error) => error.message).then((answer) => answers.push(answer));
      for (let n = 0; n < 3; n += 1) {
        await answered(desktop.browser!.show(chat));
        await answered(desktop.openSettings!("browser"));
      }
      // A click the page's own code made is no user's.
      const button = document.createElement("button");
      button.onclick = () => void answered(desktop.openSettings!("browser"));
      document.body.append(button);
      button.click();
      await new Promise((done) => setTimeout(done, 100));
      return answers;
    }, CHAT);
    expect(said).toEqual([SHOW_AT_A_CLICK, SETTINGS_AT_A_CLICK, SHOW_AT_A_CLICK, SETTINGS_AT_A_CLICK, SHOW_AT_A_CLICK, SETTINGS_AT_A_CLICK, SETTINGS_AT_A_CLICK]);
    // A take-over by the page's own code holds the browser, which stops the agent and lets it do nothing more.
    await client.evaluate((chat) => window.surogateDesktop!.browser!.takeOver(chat), CHAT);
    expect(await client.evaluate((chat) => window.surogateDesktop!.getBinding!(chat), CHAT)).toMatchObject({ takenOver: true });
    // None of it reached the desktop: Settings did not open.
    expect(over("/settings.html")).toBe(false);
    // At its user's click the desktop is asked: this chat has no page open to show.
    expect(await atClick(client, "show")).toContain("The agent's browser has no page open for this chat");
    // One click, one call: a second made with it is refused.
    await client.evaluate((chat) => {
      const desktop = window.surogateDesktop!;
      const both = Object.assign(document.createElement("button"), { id: "both", textContent: "Both" });
      both.onclick = () => {
        const answers = [desktop.browser!.show(chat), desktop.openSettings!("browser")].map((asked) => asked.then(() => "done", (error: Error) => error.message));
        void Promise.all(answers).then((all) => (both.dataset.answers = JSON.stringify(all)));
      };
      document.body.append(both);
    }, CHAT);
    await client.click("#both");
    await client.waitForFunction(() => document.getElementById("both")?.dataset.answers !== undefined, undefined, { timeout: 15_000 });
    expect(JSON.parse((await client.getAttribute("#both", "data-answers"))!)).toEqual([
      expect.stringContaining("The agent's browser has no page open for this chat"), SETTINGS_AT_A_CLICK,
    ]);
    expect(over("/settings.html")).toBe(false);
  });

  it("lets one press of its user's through one call, though a label passes its click on to the control it wraps", async () => {
    const folder = join(home, "project");
    mkdirSync(folder);
    await bound(folder);
    const client = await webClient(app!, origin);
    // A label that wraps a checkbox, with words to press on. A press on the words is a click at the label, which
    // the browser passes on to the checkbox as a second: the page asks to show the browser at the first, and
    // for Settings at the second.
    await client.evaluate((chat) => {
      const desktop = window.surogateDesktop!;
      const label = Object.assign(document.createElement("label"), { id: "wrapped" });
      label.style.cssText = "display: block; width: 300px; padding: 20px";
      label.append(Object.assign(document.createElement("input"), { type: "checkbox" }), " Words to press on");
      const heard: string[] = [];
      const answers: Record<string, string> = {};
      Object.assign(window, { pressed: { heard, answers } });
      label.addEventListener("click", (event) => {
        if (!event.isTrusted) return;
        const first = event.target === label;
        heard.push(first ? "label" : "checkbox");
        const [name, asked] = first ? (["show", desktop.browser!.show(chat)] as const) : (["openSettings", desktop.openSettings!("browser")] as const);
        void asked.then(() => "done", (error: Error) => error.message).then((answer) => (answers[name] = answer));
      });
      document.body.append(label);
    }, CHAT);
    await client.click("#wrapped", { position: { x: 200, y: 30 } });
    const pressed = () => client.evaluate(() => (window as unknown as { pressed: { heard: string[]; answers: Record<string, string> } }).pressed);
    await expect.poll(async () => Object.keys((await pressed()).answers).length, { timeout: 10_000 }).toBe(2);
    // Two clicks of the one press, both the browser's own, and one call let through.
    expect(await pressed()).toEqual({
      heard: ["label", "checkbox"],
      answers: { show: expect.stringContaining("The agent's browser has no page open for this chat"), openSettings: SETTINGS_AT_A_CLICK },
    });
    expect(over("/settings.html")).toBe(false);
  });

  it("lets one press of a key through one call, however long the key is held: its repeats are no presses", async () => {
    const folder = join(home, "project");
    mkdirSync(folder);
    await bound(folder);
    const client = await webClient(app!, origin);
    // A button of the page's own, with the keyboard on it: at each click it hears, the page asks to show the browser.
    await client.evaluate((chat) => {
      const desktop = window.surogateDesktop!;
      const button = Object.assign(document.createElement("button"), { id: "held", textContent: "Held" });
      const heard: boolean[] = [];
      const answers: string[] = [];
      Object.assign(window, { held: { heard, answers } });
      button.addEventListener("click", (event) => {
        heard.push(event.isTrusted);
        void desktop.browser!.show(chat).then(() => "done", (error: Error) => error.message).then((answer) => answers.push(answer));
      });
      document.body.append(button);
      button.focus();
    }, CHAT);
    const held = () => client.evaluate(() => (window as unknown as { held: { heard: boolean[]; answers: string[] } }).held);
    // The calls that reached the desktop, which has no page to show for the chat, and those refused in the page.
    const reached = async (clicks: number) => {
      await expect.poll(async () => (await held()).answers.length, { timeout: 10_000 }).toBe(clicks);
      const { heard, answers } = await held();
      expect(heard).toEqual(Array.from({ length: clicks }, () => true));
      return [
        answers.filter((answer) => answer.includes("The agent's browser has no page open for this chat")).length,
        answers.filter((answer) => answer === SHOW_AT_A_CLICK).length,
      ];
    };
    // Enter held down: the keyboard repeats it, and the browser makes a click of each repeat, its own every one.
    for (let n = 0; n < 6; n += 1) await client.keyboard.down("Enter");
    await client.keyboard.up("Enter");
    expect(await reached(6)).toEqual([1, 5]);
    // The key came up: the next press of it is a press, and so is Space held and let go, whose one click comes as it rises.
    await client.keyboard.press("Enter");
    expect(await reached(7)).toEqual([2, 5]);
    for (let n = 0; n < 4; n += 1) await client.keyboard.down(" ");
    await client.keyboard.up(" ");
    expect(await reached(8)).toEqual([3, 5]);
    // A click of the mouse is a press whatever the keyboard does: here with a key still down, repeating.
    for (let n = 0; n < 3; n += 1) await client.keyboard.down("a");
    await client.click("#held");
    await client.keyboard.up("a");
    expect(await reached(9)).toEqual([4, 5]);
  });

  it("brings the chat's page to the front at a take-over only at its user's click: one the page's own code makes raises nothing", async () => {
    const folder = join(home, "project");
    mkdirSync(folder);
    await bound(folder, [paged()]);
    const client = await webClient(app!, origin);
    await expect.poll(() => app!.evaluate(() => (globalThis as unknown as { paged: boolean }).paged), { timeout: 10_000 }).toBe(true);
    const shown = () => app!.evaluate(() => (globalThis as unknown as { shown: string[] }).shown);
    // The page's own code, again and again: the chat is taken over, and its page stays where it is.
    for (let n = 0; n < 3; n += 1) await client.evaluate((chat) => window.surogateDesktop!.browser!.takeOver(chat), CHAT);
    expect(await client.evaluate((chat) => window.surogateDesktop!.getBinding!(chat), CHAT)).toMatchObject({ takenOver: true });
    expect(await shown()).toEqual([]);
    // At its user's click its page comes to the front too.
    expect(await atClick(client, "takeOver")).toBeNull();
    expect(await shown()).toEqual([CHAT]);
  });

  it("opens Settings on Browser at a click in the agent's page, shows Browser in a Settings open already, and opens none over a project's dialog", async () => {
    const folder = join(home, "project");
    mkdirSync(folder);
    const page = await bound(folder);
    const client = await webClient(app!, origin);
    expect(await atClick(client, "openSettings")).toBeNull();
    let settings: Page | undefined;
    await expect.poll(() => {
      settings = app!.windows().find((window) => window.url().includes("/settings.html"));
      return settings !== undefined;
    }, { timeout: 10_000 }).toBe(true);
    const selected = () => settings!.getAttribute('.settings-nav [data-section="browser"]', "class");
    await expect.poll(() => settings!.isVisible('section[data-section="browser"]'), { timeout: 10_000 }).toBe(true);
    await expect.poll(selected, { timeout: 10_000 }).toContain("selected");
    // Open already, on another section: it is shown Browser.
    await settings!.click('.settings-nav [data-section="general"]');
    await expect.poll(selected, { timeout: 10_000 }).not.toContain("selected");
    expect(await atClick(client, "openSettings")).toBeNull();
    await expect.poll(selected, { timeout: 10_000 }).toContain("selected");
    await expect.poll(() => settings!.isVisible('section[data-section="browser"]'), { timeout: 10_000 }).toBe(true);
    // A project's dialog over the window: Settings does not open over it, and the page is told why.
    await settings!.click("#close", { noWaitAfter: true }).catch(() => {});
    await expect.poll(() => over("/settings.html"), { timeout: 10_000 }).toBe(false);
    await page.evaluate(() => (window as unknown as { surogateShell: { newProject(): Promise<void> } }).surogateShell.newProject());
    await expect.poll(() => over("/project.html"), { timeout: 10_000 }).toBe(true);
    expect(await atClick(client, "openSettings")).toContain("Surogate has a project's dialog open: close it to open Settings");
    expect(over("/settings.html")).toBe(false);
  });
});

describe("Settings → Browser", () => {
  it("shows a hidden window before Settings opens over it at a click in the agent's page", async () => {
    const folder = join(home, "project");
    mkdirSync(folder);
    await bound(folder);
    const client = await webClient(app!, origin);
    // Its user clicks, and the window is hidden before the page asks, as one closed to the tray.
    await client.evaluate(() => document.body.append(Object.assign(document.createElement("button"), { id: "pressed", textContent: "Pressed" })));
    await client.click("#pressed");
    await hideWindow();
    expect(await windowShown()).toBe(false);
    await client.evaluate(() => window.surogateDesktop!.openSettings!("browser"));
    expect(await windowShown()).toBe(true);
    await expect.poll(() => over("/settings.html"), { timeout: 10_000 }).toBe(true);
  });

  it("lists the browsers found here, with Automatic and Custom…, and keeps the one chosen for the next launch", async () => {
    const settings = await browserSettings();
    await expect.poll(() => settings!.$$eval("#browser option", (options) => options.length)).toBeGreaterThan(1);
    const values = await settings!.$$eval("#browser option", (options) => options.map((option) => (option as HTMLOptionElement).value));
    expect(values[0]).toBe("auto");
    expect(values.at(-1)).toBe("pick");
    expect(await settings!.inputValue("#browser")).toBe("auto");
    const found = findBrowsers().filter((browser) => browser.unsupported === null);
    if (found[0]) {
      await settings!.selectOption("#browser", found[0].id);
      await expect.poll(() => readFileSync(join(home, "surogate", "browser.json"), "utf8")).toContain(`"choice": "${found[0].id}"`);
    }
  });
});
