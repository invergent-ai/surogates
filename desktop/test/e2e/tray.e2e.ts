// The tray, through the real app: its icon follows the theme, and its menu shows the window,
// says how this computer is connected, opens Settings and quits. Electron lists no trays, so
// the test reads what the app sets on its tray.

import { rmSync, writeFileSync } from "node:fs";
import { join } from "node:path";

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

  it("says the app's name when pointed at, and shows the window on a click", async () => {
    // The tray, kept as the app makes it, with what it says when pointed at.
    const watching = join(home, "watch-tray.cjs");
    writeFileSync(watching, [
      'const { Tray } = require("electron");',
      "const setToolTip = Tray.prototype.setToolTip;",
      "Tray.prototype.setToolTip = function (tip) { Object.assign(globalThis, { tray: this, tooltip: tip }); return setToolTip.call(this, tip); };",
    ].join("\n"));
    app = await launch(home, {}, [], [watching]);
    await (await app.firstWindow()).waitForLoadState();
    expect(await app.evaluate(() => (globalThis as unknown as { tooltip: string }).tooltip)).toBe("Surogate");
    const shown = () => app!.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0]!.isVisible());
    await app.evaluate(({ BrowserWindow }) => BrowserWindow.getAllWindows()[0]!.hide());
    expect(await shown()).toBe(false);
    // A left click, as the desktop sends one where it sends one.
    await app.evaluate(() => (globalThis as unknown as { tray: Electron.Tray }).tray.emit("click"));
    await expect.poll(shown).toBe(true);
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
