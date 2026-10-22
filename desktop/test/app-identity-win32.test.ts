// The app as Windows knows it: the AppUserModelID its windows, its taskbar button, its toasts and its login item go
// by, the installed app's and a development build's apart; and a launch that asks the running app to quit. The app
// registers no surogate:// scheme itself, on any OS: an install does (the MSI, machine-wide), or a development
// build's script (test/win32/dev-app.ps1, for its user).

import { describe, expect, it } from "vitest";

import { APP_ACTIVATOR, APP_ID, DEVELOPMENT_ACTIVATOR, DEVELOPMENT_ID } from "../src/shell/app-identity-win32.js";
import { importedOn } from "./platforms.js";

const windows = () => importedOn("win32", () => import("../src/shell/app-identity.js"));

// Electron's app as far as the identity uses it, and what was asked of it, in order.
function electronApp() {
  const asked: unknown[][] = [];
  return {
    asked,
    setAppUserModelId: (id: string) => void asked.push(["id", id]),
    setToastActivatorCLSID: (id: string) => void asked.push(["activator", id]),
  };
}

describe("the app as Windows knows it", () => {
  it("goes by the installed app's AppUserModelID and toast activator, and a development build by its own", async () => {
    const { nameTheApp } = await windows();
    expect(APP_ID).toBe("ai.invergent.surogate");
    expect(DEVELOPMENT_ID).toBe("ai.invergent.surogate.development");
    for (const clsid of [APP_ACTIVATOR, DEVELOPMENT_ACTIVATOR]) expect(clsid).toMatch(/^\{[0-9A-F]{8}-[0-9A-F]{4}-4[0-9A-F]{3}-[89AB][0-9A-F]{3}-[0-9A-F]{12}\}$/);
    expect(APP_ACTIVATOR).not.toBe(DEVELOPMENT_ACTIVATOR);
    const installed = electronApp();
    nameTheApp(installed, true);
    const development = electronApp();
    nameTheApp(development, false);
    expect([installed.asked, development.asked]).toEqual([
      [["id", APP_ID], ["activator", APP_ACTIVATOR]],
      [["id", DEVELOPMENT_ID], ["activator", DEVELOPMENT_ACTIVATOR]],
    ]);
  });

  it("is asked to quit by a launch whose arguments hold --quit", async () => {
    const { quitAsked } = await windows();
    expect(quitAsked(["C:\\Program Files\\Surogate\\Surogate.exe", "--quit"])).toBe(true);
    expect(quitAsked(["C:\\Program Files\\Surogate\\Surogate.exe"])).toBe(false);
    expect(quitAsked(["C:\\Program Files\\Surogate\\Surogate.exe", "--", "surogate://open?url=--quit"])).toBe(false);
  });
});

describe("the app as Linux and macOS know it", () => {
  it("is named and asked to quit by nothing of the app's own: an install's files and the bundle do it", async () => {
    for (const os of ["linux", "darwin"] as const) {
      const { nameTheApp, quitAsked } = await importedOn(os, () => import("../src/shell/app-identity.js"));
      const app = electronApp();
      nameTheApp(app, true);
      expect(app.asked, os).toEqual([]);
      expect(quitAsked(["/opt/surogate/current/surogate", "--quit"]), os).toBe(false);
    }
  });
});
