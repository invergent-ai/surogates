// The agent's browser through the real app. Settings → Browser here; the browser itself
// comes with the demo's tests.

import { readFileSync, rmSync } from "node:fs";
import { join } from "node:path";

import type { ElectronApplication, Page } from "playwright-core";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { findBrowsers } from "../../src/browser/choose.js";
import { connect, FakeAgent, signedInAndAdded } from "./fake-agent.js";
import { dataHome, launch, quit, shellPage, stubNative } from "./launch.js";

let home: string;
let agent: FakeAgent;
let app: ElectronApplication | undefined;

beforeEach(() => {
  home = dataHome();
  agent = new FakeAgent();
});

afterEach(async () => {
  await quit(app);
  app = undefined;
  await agent.stop();
  await agent.link.stop();
  rmSync(home, { recursive: true, force: true });
});

describe("Settings → Browser", () => {
  it("lists the browsers found here, with Automatic and Custom…, and keeps the one chosen for the next launch", async () => {
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
