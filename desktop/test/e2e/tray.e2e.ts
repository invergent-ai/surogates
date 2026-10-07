// The tray, through the real app: its icon follows the theme, and its menu shows the window,
// says how this computer is connected, opens Settings and quits. Electron lists no trays, so
// the test reads what the app sets on its tray.

import { rmSync } from "node:fs";

import type { ElectronApplication } from "playwright-core";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { connect, FakeAgent, signedInAndAdded } from "./fake-agent.js";
import { dataHome, icons, launch, pickInTray, quit, shellPage, stubNative, trayLabels, watchTray } from "./launch.js";

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

const theme = (shell: ElectronApplication, source: "light" | "dark") => shell.evaluate(({ nativeTheme }, chosen) => {
  nativeTheme.themeSource = chosen;
}, source);

describe("the tray", () => {
  it("takes the dark theme's icon and the light one's in turn, outside GNOME", async () => {
    app = await launch(home, { XDG_CURRENT_DESKTOP: "XFCE" });
    await (await app.firstWindow()).waitForLoadState();
    await watchTray(app);
    await theme(app, "dark");
    await expect.poll(() => icons(app!)).toEqual(["tray-dark.png"]);
    await theme(app, "light");
    await expect.poll(() => icons(app!)).toEqual(["tray-dark.png", "tray-light.png"]);
  });

  it("keeps the light icon on GNOME's dark panel, whatever the theme", async () => {
    app = await launch(home, { XDG_CURRENT_DESKTOP: "ubuntu:GNOME" });
    await (await app.firstWindow()).waitForLoadState();
    await watchTray(app);
    await theme(app, "dark");
    await theme(app, "light");
    await expect.poll(() => icons(app!)).toEqual(["tray-dark.png", "tray-dark.png"]);
  });

  it("shows the window, says how this computer is connected, opens Settings, and quits", async () => {
    app = await launch(home);
    await stubNative(app);
    await watchTray(app);
    const page = await shellPage(app);
    await connect(page, origin);
    await signedInAndAdded(app, page, agent);
    await expect.poll(() => trayLabels(app!)).toEqual(["Show Surogate", "Connected as Laptop", "", "Settings…", "Quit Surogate"]);
    const shown = () => app!.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0]!.isVisible());
    await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0]!.hide());
    await pickInTray(app, "Show Surogate");
    await expect.poll(shown).toBe(true);
    await pickInTray(app, "Settings…");
    await expect.poll(() => app!.windows().some((found) => found.url().endsWith("/settings.html"))).toBe(true);
    const closed = app.waitForEvent("close");
    void pickInTray(app, "Quit Surogate").catch(() => {});
    await closed;
    app = undefined;
  });
});
