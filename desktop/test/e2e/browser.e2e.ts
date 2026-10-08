// The agent's browser through the real app: the shell's device, its binder and approvals, the
// desktop's own prompt, the browser host in its utility process, and the user's own browser.
// Behind SUROGATE_BROWSER_TESTS=1, apart from the user's session as the browser unit tests are:
//   npm run build && sh test/isolated.sh npx vitest run -c vitest.e2e.config.ts test/e2e/browser.e2e.ts
// with SUROGATE_TEST_BROWSER naming another browser to drive than Settings would choose.
// Skipped where no supported browser is installed. Settings → Browser launches none, and runs anywhere.

import { mkdirSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { createServer, type Server } from "node:http";
import { dirname, join } from "node:path";

import type { ElectronApplication, Page } from "playwright-core";
import { afterEach, beforeAll, beforeEach, describe, expect, it } from "vitest";

import { chosenBrowser, findBrowsers } from "../../src/browser/choose.js";
import { connect, FakeAgent, signedInAndAdded, webClient } from "./fake-agent.js";
import { isolated } from "../isolated.js";
import { dataHome, launch, MAIN, press, prompt, promptsShown, quit, shellEnv, shellPage, stubNative } from "./launch.js";

const CHAT = "4e5f6a7b-8c9d-4e0f-a1b2-c3d4e5f6a7b8";
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

// The chat's operation, as the server sends it, and the app's result for it.
async function operation(kind: string, args: Record<string, unknown>, invocation = `call-${next + 1}`, ordinal = 1): Promise<any> {
  const id = `op-${(next += 1)}`;
  agent.link.send({
    type: "op", id, session_id: CHAT, calling_session_id: CHAT, invocation_id: invocation, ordinal, kind, args, digest: `d-${id}`,
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

// A browser operation of a chat its user took the browser over.
const PAUSED = { error: { type: "paused_by_user", message: "The user took over the agent's browser on this computer" } };

// One of the bridge's calls about a chat's browser, made as the page's own button makes it, at its
// user's click: what it answered, or why it was refused.
async function atClick(client: Page, call: "show" | "takeOver" | "handBack" | "openSettings", chat = CHAT): Promise<unknown> {
  await client.evaluate(([name, id]) => {
    document.getElementById("ask")?.remove();
    const button = Object.assign(document.createElement("button"), { id: "ask", textContent: name });
    button.onclick = () => {
      const desktop = window.surogateDesktop!;
      // A call the desktop has not is answered as one it refused.
      Promise.resolve().then((): Promise<unknown> => (name === "openSettings" ? desktop.openSettings!("browser") : desktop.browser![name](id))).then(
        (answer) => (button.dataset.answer = JSON.stringify(answer ?? null)),
        (error: Error) => (button.dataset.answer = JSON.stringify(error.message)),
      );
    };
    document.body.append(button);
  }, [call, chat] as const);
  await client.click("#ask");
  await client.waitForFunction(() => document.getElementById("ask")?.dataset.answer !== undefined, undefined, { timeout: 15_000 });
  return JSON.parse((await client.getAttribute("#ask", "data-answer"))!);
}

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
    expect(await asked.getAttribute('[data-id="allow_session"]', "aria-disabled")).toBe("true");
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

  it("answers the chat's browser operations paused while its user holds the browser, tells the page, and hands it back only at the desktop's own confirmation", async () => {
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
    const boxes = () => app!.evaluate(() => (globalThis as unknown as { asked: Array<{ message: string; buttons: string[] }> }).asked);
    expect(await binding()).toMatchObject({ takenOver: false });
    await takeOver();
    expect(await binding()).toMatchObject({ folder, takenOver: true });
    await expect.poll(heard, { timeout: 10_000 }).toEqual([CHAT]);
    // Paused: answered at once, the user asked nothing, and no browser started for it.
    expect(await operation("browser.navigate", { url: "https://example.com/", wait_until: "load" })).toEqual(PAUSED);
    expect(await operation("browser.close", {})).toEqual(PAUSED);
    expect(await promptsShown(app!)).toBe(0);
    expect(browsers()).toEqual([]);
    // Kept at the desktop's own confirmation: still the user's.
    await app!.evaluate(() => Object.assign(globalThis, { answer: 1 }));
    const before = (await boxes()).length;
    expect(await handBack()).toBe(false);
    expect(await binding()).toMatchObject({ takenOver: true });
    // Keep control is its default and its cancel: Enter or Escape keeps the browser the user's.
    expect((await boxes()).at(-1)).toMatchObject({
      message: expect.stringMatching(/^Hand the browser back to .+\?$/), buttons: ["Hand back", "Keep control"], defaultId: 1, cancelId: 1,
    });
    // Kept: the page's own code asks no more, however often, and no box opens for it.
    for (let n = 0; n < 3; n += 1) await expect(handBack()).rejects.toThrow("The user chose to keep the browser");
    expect(await boxes()).toHaveLength(before + 1);
    // Nor after a take-over the page's own code makes again: the chat was its user's already, so it is no new one.
    await takeOver();
    await expect(handBack()).rejects.toThrow("The user chose to keep the browser");
    expect(await boxes()).toHaveLength(before + 1);
    // Its user's own click still asks, and can still keep it.
    expect(await atClick(client, "handBack")).toBe(false);
    expect(await boxes()).toHaveLength(before + 2);
    await app!.evaluate(() => Object.assign(globalThis, { answer: 0 }));
    expect(await atClick(client, "handBack")).toBe(true);
    expect(await binding()).toMatchObject({ takenOver: false });
    await expect.poll(heard, { timeout: 10_000 }).toEqual([CHAT, CHAT, CHAT]);
    // Handed back, the chat's browser asks its first use, as before.
    const navigating = operation("browser.navigate", { url: "https://example.com/", wait_until: "load" });
    await press(await prompt(app!), "deny");
    expect((await navigating).error.type).toBe("denied");
    // Taken over anew, the page may ask once more: what its user chose at the last one is not held against it.
    await takeOver();
    expect(await handBack()).toBe(true);
    expect(await boxes()).toHaveLength(before + 4);
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
    // A take-over by the page's own code pauses the chat, which only makes it safer.
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
