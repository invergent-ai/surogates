import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";

import type { ElectronApplication, Page } from "playwright-core";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { type BrowserIdentity, profilesOf } from "../../src/browser/choose.js";
import { connect, FakeAgent, opened, signIn, signedInAndAdded, webClient } from "./fake-agent.js";
import { dataHome, launch, quit, shellPage, stubNative } from "./launch.js";

let home: string;
let agent: FakeAgent;
let origin: string;
let host: string;
let app: ElectronApplication | undefined;

beforeEach(async () => {
  home = dataHome();
  agent = new FakeAgent();
  origin = await agent.start();
  host = origin.replace("http://", "");
});

afterEach(async () => {
  await quit(app);
  app = undefined;
  await agent.stop();
  await agent.link.stop();
  rmSync(home, { recursive: true, force: true });
});

const state = (path: string) => join(home, "surogate", path);
const credentials = () => (existsSync(state("credentials.json")) ? JSON.parse(readFileSync(state("credentials.json"), "utf8")) as Array<Record<string, unknown>> : []);
const asked = (shell: ElectronApplication) =>
  shell.evaluate(() => (globalThis as unknown as { asked: Array<{ message: string; detail: string }> }).asked.map((options) => options.message));
const revokes = () => agent.link.received.filter((frame) => frame.type === "revoke").length;
// The confirmation the user was asked whose message is *message*, as the app passed it.
const confirmation = async (shell: ElectronApplication, message: string) =>
  (await shell.evaluate(() => (globalThis as unknown as { asked: Array<Record<string, unknown>> }).asked)).find((options) => options.message === message);
const FORGET = /^Also forget the sites .+'s browser on this computer is signed in to$/;
// The computer's identity's browser profiles, as the agent's browser would have left them here.
function browserProfiles(): string {
  const profiles = profilesOf(join(home, "surogate"), credentials()[0] as unknown as BrowserIdentity);
  mkdirSync(join(profiles, "chrome", "Default"), { recursive: true });
  writeFileSync(join(profiles, "chrome", "Default", "Cookies"), "a site's sign-in");
  return profiles;
}

// Launched with its state under home, signed in as ACCOUNT, and this computer added; the web client with something in its storage.
async function signedIn(): Promise<{ shell: ElectronApplication; page: Page; client: Page }> {
  const shell = await launch(home);
  app = shell;
  await stubNative(shell);
  const page = await shellPage(shell);
  if (await page.isVisible("#first-run")) await connect(page, origin);
  await signedInAndAdded(shell, page, agent);
  const client = await webClient(shell, origin);
  await client.evaluate(() => localStorage.setItem("surogates_auth_token", "the page's own session"));
  return { shell, page, client };
}

async function logOut(page: Page): Promise<void> {
  await page.click("#user");
  await page.click('[data-action="logout"]');
}

describe("logging out", () => {
  it("revokes this computer at the agent, forgets its folders and the sign-in, and clears the window", async () => {
    const { shell, page, client } = await signedIn();
    await logOut(page);
    await expect.poll(() => page.isVisible("#sign-in")).toBe(true);
    expect(await asked(shell)).toContain(`Log out of ${host}?`);
    expect(revokes()).toBe(1);
    await expect.poll(() => agent.oauth.some((form) => form.token === "rt-1")).toBe(true);
    expect(credentials()).toEqual([]);
    expect(existsSync(state("session.json"))).toBe(false);
    expect(existsSync(state("devices/d"))).toBe(false);
    await expect.poll(() => client.evaluate(() => localStorage.getItem("surogates_auth_token"))).toBeNull();
    // Signed out, the web client has nothing to give: the window asks for a sign-in.
    expect(await client.evaluate(() => window.surogateDesktop!.webSignIn())).toBeNull();
  });

  it("offers to forget the agent's browser sign-ins only where its browser keeps a profile here, and forgets them when ticked", async () => {
    const { shell, page } = await signedIn();
    await logOut(page);
    await expect.poll(() => page.isVisible("#sign-in")).toBe(true);
    // The agent's browser never ran here: there is nothing to forget, so nothing is offered.
    expect((await confirmation(shell, `Log out of ${host}?`))?.checkboxLabel).toBeUndefined();
    await quit(shell);
    const again = await signedIn();
    const profiles = browserProfiles();
    await again.shell.evaluate(() => Object.assign(globalThis, { checked: true }));
    await logOut(again.page);
    await expect.poll(() => again.page.isVisible("#sign-in")).toBe(true);
    expect(await confirmation(again.shell, `Log out of ${host}?`)).toMatchObject({ checkboxLabel: expect.stringMatching(FORGET), checkboxChecked: false });
    expect(existsSync(profiles)).toBe(false);
  });

  it("keeps the agent's browser sign-ins at a log out its user did not tick", async () => {
    const { shell, page } = await signedIn();
    const profiles = browserProfiles();
    await logOut(page);
    await expect.poll(() => page.isVisible("#sign-in")).toBe(true);
    expect((await confirmation(shell, `Log out of ${host}?`))?.checkboxLabel).toMatch(FORGET);
    expect(readFileSync(join(profiles, "chrome", "Default", "Cookies"), "utf8")).toBe("a site's sign-in");
  });

  it("is what the web client's own Log out asks for", async () => {
    const { shell, page, client } = await signedIn();
    void client.evaluate(() => window.surogateDesktop!.signOut()).catch(() => {});
    await expect.poll(() => page.isVisible("#sign-in")).toBe(true);
    expect(await asked(shell)).toContain(`Log out of ${host}?`);
    expect(revokes()).toBe(1);
  });

  it("changes nothing when the user cancels", async () => {
    const { shell, page } = await signedIn();
    await shell.evaluate(() => Object.assign(globalThis, { answer: 1 }));
    await logOut(page);
    await expect.poll(() => asked(shell)).toContain(`Log out of ${host}?`);
    expect(await page.getAttribute("#device", "title")).toBe("Connected as Laptop");
    expect(revokes()).toBe(0);
    expect(credentials()).toHaveLength(1);
  });

  it("revokes this computer once the agent can hear it, when it could not at the log out", async () => {
    const { page } = await signedIn();
    const port = Number(new URL(origin).port);
    await agent.stop();
    await expect.poll(() => page.getAttribute("#device", "title")).not.toBe("Connected as Laptop");
    await logOut(page);
    await expect.poll(() => page.isVisible("#sign-in")).toBe(true);
    // The sign-in shows as soon as nobody is signed in, before the device has stopped.
    await expect.poll(() => credentials()).toEqual([expect.objectContaining({ deviceId: "d", revoking: true })]);
    await expect.poll(() => existsSync(state("devices/d"))).toBe(false);
    await agent.start(port);
    await expect.poll(() => revokes(), { timeout: 15_000 }).toBe(1);
    await expect.poll(() => credentials()).toEqual([]);
  });
});

// The browser came back from a sign-in, whose code the app has exchanged: what it finishes is held at the agent.
async function approved(shell: ElectronApplication, start: () => Promise<void>): Promise<void> {
  const before = (await opened(shell)).length;
  await start();
  await expect.poll(async () => (await opened(shell)).length).toBe(before + 1);
  void agent.approve((await opened(shell))[before]!).catch(() => {});
}

describe("a log out while a sign-in is finishing", () => {
  it("is not undone by a sign-in asking who signed in: that sign-in ends, and nothing of it is kept", async () => {
    agent.recent = false;
    const shell = await launch(home);
    app = shell;
    await stubNative(shell);
    const page = await shellPage(shell);
    await connect(page, origin);
    await signIn(shell, page, agent);
    await expect.poll(() => page.isVisible("#device-action-button")).toBe(true);
    agent.recent = true;
    const release = agent.hold("me");
    const asking = agent.asked.me;
    await approved(shell, () => page.click("#device-action-button"));
    await expect.poll(() => agent.asked.me).toBe(asking + 1);
    await logOut(page);
    await expect.poll(() => asked(shell)).toContain(`Log out of ${host}?`);
    release();
    await expect.poll(() => agent.oauth.filter((form) => form.token).map((form) => form.token).sort()).toEqual(["rt-1", "rt-2"]);
    await expect.poll(() => page.isVisible("#sign-in")).toBe(true);
    expect(existsSync(state("session.json"))).toBe(false);
    expect(agent.registered).toHaveLength(1);
    expect(credentials()).toEqual([]);
  });

  it("is not undone by a sign-in binding the computer: the token it was issued revokes it, and no device runs", async () => {
    const first = await signedIn();
    await quit(first.shell);
    rmSync(state("session.json"));
    app = await launch(home);
    await stubNative(app);
    const page = await shellPage(app);
    await expect.poll(() => page.getAttribute("#device", "title")).toBe("Connected as Laptop");
    const release = agent.hold("reauthorize");
    await approved(app, () => page.click("#sign-in-button"));
    await expect.poll(() => agent.asked.reauthorize).toBe(1);
    // What the Log out row calls: nothing has redrawn the window since the sign-in started.
    void page.evaluate(() => (globalThis as unknown as { surogateShell: { signOut(): Promise<void> } }).surogateShell.signOut());
    await expect.poll(() => asked(app!)).toContain(`Log out of ${host}?`);
    // No sign-in starts while the log out runs, which waits for the one under way to stop.
    await page.evaluate(() => (globalThis as unknown as { surogateShell: { signIn(): Promise<void> } }).surogateShell.signIn());
    await new Promise((resolve) => setTimeout(resolve, 300));
    expect(await opened(app!)).toHaveLength(1);
    expect(revokes()).toBe(0);
    release();
    await expect.poll(() => revokes()).toBe(1);
    await expect.poll(() => credentials()).toEqual([]);
    await expect.poll(() => agent.oauth.some((form) => form.token === "rt-2")).toBe(true);
    await expect.poll(() => page.isVisible("#sign-in")).toBe(true);
    expect(await page.getAttribute("#device", "title")).toBe("Sign in to this agent to let it work on folders of this computer");
    expect(existsSync(state("session.json"))).toBe(false);
    expect(existsSync(state("devices/d"))).toBe(false);
  });
});

// The log out is held in its revoke: the agent stops reading the link, so the revoke waits out its 5 s,
// and refuses a new link, so the revocation cannot be paid meanwhile.
describe("a quit during a log out whose revoke the agent never confirms", () => {
  it.each([
    ["waits for the log out before it quits, and the next launch pays the revocation owed", false],
    ["is killed with the revocation already saved as owed, and the next launch pays it", true],
  ])(
    "%s",
    async (_title, killed) => {
      const first = await signedIn();
      await quit(first.shell);
      const shell = await launch(home);
      app = shell;
      await stubNative(shell);
      const page = await shellPage(shell);
      await expect.poll(() => page.getAttribute("#device", "title")).toBe("Connected as Laptop");
      agent.link.stall();
      agent.link.refusing = true;
      await logOut(page);
      await expect.poll(() => asked(shell)).toContain(`Log out of ${host}?`);
      app = undefined;
      if (killed) {
        const child = shell.process();
        child.kill("SIGKILL");
        if (child.exitCode === null && child.signalCode === null) await new Promise((resolve) => child.once("exit", resolve));
      } else {
        await quit(shell);
        // The quit waited for the log out: its folders went with it.
        expect(existsSync(state("devices/d"))).toBe(false);
      }
      expect(credentials()).toEqual([expect.objectContaining({ deviceId: "d", revoking: true })]);
      agent.link.refusing = false;
      // The stalled link's revoke frame is read once it is dropped.
      agent.link.drop();
      app = await launch(home);
      const again = await shellPage(app);
      await expect.poll(() => again.getAttribute("#device", "title")).toBe("Sign in to this agent to let it work on folders of this computer");
      await expect.poll(() => credentials(), { timeout: 15_000 }).toEqual([]);
      expect(revokes()).toBeGreaterThanOrEqual(1);
      expect(existsSync(state("devices/d"))).toBe(false);
    },
  );
});

describe("removing the agent", () => {
  it("logs out, forgets the agent, and asks for one again", async () => {
    const { shell, page } = await signedIn();
    await page.click("#user");
    await page.click('[data-action="remove"]');
    await expect.poll(() => page.isVisible("#first-run")).toBe(true);
    expect(await asked(shell)).toContain(`Remove ${host} from Surogate?`);
    expect(revokes()).toBe(1);
    expect(existsSync(state("agent.json"))).toBe(false);
    expect(await shell.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0]!.contentView.children.length)).toBe(0);
  });

  it("offers to forget the agent's browser sign-ins, ticked, and forgets them", async () => {
    const { shell, page } = await signedIn();
    const profiles = browserProfiles();
    await shell.evaluate(() => Object.assign(globalThis, { checked: true }));
    await page.click("#user");
    await page.click('[data-action="remove"]');
    await expect.poll(() => page.isVisible("#first-run")).toBe(true);
    expect(await confirmation(shell, `Remove ${host} from Surogate?`)).toMatchObject({ checkboxLabel: expect.stringMatching(FORGET), checkboxChecked: true });
    expect(existsSync(profiles)).toBe(false);
  });

  it("offers to forget nothing on the sign-in screen when this computer keeps no access to the agent", async () => {
    const first = await signedIn();
    await quit(first.shell);
    // Nobody signed in, and no access of this computer's kept: whose browser profiles there were is not known.
    rmSync(state("session.json"));
    rmSync(state("credentials.json"));
    app = await launch(home);
    await stubNative(app);
    const page = await shellPage(app);
    await expect.poll(() => page.isVisible("#sign-in")).toBe(true);
    await page.click("#sign-in-remove");
    await expect.poll(() => page.isVisible("#first-run")).toBe(true);
    expect((await confirmation(app, `Remove ${host} from Surogate?`))?.checkboxLabel).toBeUndefined();
  });

  it("is offered on the sign-in screen too, with nobody signed in", async () => {
    const first = await signedIn();
    await quit(first.shell);
    // The sign-in ended (it expired): the next launch asks for one, while the computer still runs.
    rmSync(state("session.json"));
    app = await launch(home);
    await stubNative(app);
    const page = await shellPage(app);
    await expect.poll(() => page.isVisible("#sign-in")).toBe(true);
    await expect.poll(() => page.getAttribute("#device", "title")).toBe("Connected as Laptop");
    await page.click("#sign-in-remove");
    await expect.poll(() => page.isVisible("#first-run")).toBe(true);
    expect(await asked(app)).toContain(`Remove ${host} from Surogate?`);
    expect(revokes()).toBe(1);
    expect(credentials()).toEqual([]);
    expect(existsSync(state("agent.json"))).toBe(false);
    expect(existsSync(state("devices/d"))).toBe(false);
  });
});

describe("removing the agent while it cannot be reached", () => {
  it("revokes this computer at the next launch, with no agent kept, and then forgets it", async () => {
    const { shell, page } = await signedIn();
    const port = Number(new URL(origin).port);
    await agent.stop();
    await expect.poll(() => page.getAttribute("#device", "title")).not.toBe("Connected as Laptop");
    await page.click("#user");
    await page.click('[data-action="remove"]');
    await expect.poll(() => page.isVisible("#first-run")).toBe(true);
    expect(credentials()).toEqual([expect.objectContaining({ deviceId: "d", revoking: true })]);
    await quit(shell);
    await agent.start(port);
    app = await launch(home);
    await expect.poll(() => revokes(), { timeout: 15_000 }).toBe(1);
    await expect.poll(() => credentials()).toEqual([]);
    expect(existsSync(state("agent.json"))).toBe(false);
  });
});

describe("a second user of the agent on this computer", () => {
  const OTHER = { name: "Bea Other", email: "bea@example.com", userId: "b", orgId: "o" };

  // The first user's sign-in ended (it expired): the next launch asks for one, while their device still runs.
  async function firstOneExpired(): Promise<{ shell: ElectronApplication; page: Page }> {
    const first = await signedIn();
    await quit(first.shell);
    rmSync(state("session.json"));
    const shell = await launch(home);
    app = shell;
    await stubNative(shell);
    const page = await shellPage(shell);
    await expect.poll(() => page.isVisible("#sign-in")).toBe(true);
    await expect.poll(() => page.getAttribute("#device", "title")).toBe("Connected as Laptop");
    return { shell, page };
  }

  it("ends the first one's access here once they agree, and this computer is added for them", async () => {
    const { shell, page } = await firstOneExpired();
    // Another user signs in at the agent's page, and the agent adds their own device.
    agent.account = OTHER;
    agent.link.identity = { ...agent.link.identity, device_id: "d2", user_id: "b" };
    await signIn(shell, page, agent);
    await expect.poll(() => credentials().map((credential) => [credential.deviceId, credential.userId])).toEqual([["d2", "b"]]);
    await expect.poll(() => page.getAttribute("#device", "title")).toBe("Connected as Laptop");
    expect(await asked(shell)).toContain("Sign in as bea@example.com?");
    expect(revokes()).toBe(1);
    expect(existsSync(state("devices/d"))).toBe(false);
  });

  it("leaves the first one's computer as it is when they cancel, and keeps nothing of their sign-in", async () => {
    const { shell, page } = await firstOneExpired();
    agent.account = OTHER;
    await shell.evaluate(() => Object.assign(globalThis, { answer: 1 }));
    await approved(shell, () => page.click("#sign-in-button"));
    await expect.poll(() => page.textContent("#sign-in-error")).toBe(
      "Not signed in as bea@example.com: this computer keeps working for the account that added it",
    );
    await expect.poll(() => agent.oauth.some((form) => form.token === "rt-2")).toBe(true);
    expect(credentials().map((credential) => [credential.deviceId, credential.userId])).toEqual([["d", "u"]]);
    expect(await page.getAttribute("#device", "title")).toBe("Connected as Laptop");
    expect(existsSync(state("session.json"))).toBe(false);
    expect(revokes()).toBe(0);
  });
});
