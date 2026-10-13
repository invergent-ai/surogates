// The app as Windows knows it: the AppUserModelID its windows, its taskbar button, its toasts and its login item go
// by, the installed app's and a development build's apart; surogate:// links taken per user, in the user's own
// classes, by the command that starts the app; and a launch that asks the running app to quit.

import { describe, expect, it } from "vitest";

import { APP_ACTIVATOR, APP_ID, DEVELOPMENT_ACTIVATOR, DEVELOPMENT_ID } from "../src/shell/app-identity-win32.js";
import { importedOn } from "./platforms.js";

const windows = () => importedOn("win32", () => import("../src/shell/app-identity.js"));

// Electron's app as far as the identity uses it, and what was asked of it, in order.
function electronApp(registered: { path: string; args: string[] } | null = null) {
  const asked: unknown[][] = [];
  return {
    asked,
    setAppUserModelId: (id: string) => void asked.push(["id", id]),
    setToastActivatorCLSID: (id: string) => void asked.push(["activator", id]),
    isDefaultProtocolClient: (protocol: string, path?: string, args?: string[]) =>
      protocol === "surogate" && registered !== null && registered.path === path && JSON.stringify(registered.args) === JSON.stringify(args),
    setAsDefaultProtocolClient: (protocol: string, path?: string, args?: string[]) => {
      asked.push(["links", protocol, path, args]);
      registered = { path: path!, args: args! };
      return true;
    },
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

  it("takes surogate:// links per user by the command that starts it, its switches ended before the link, and asks nothing where it has them", async () => {
    const { takeLinks } = await windows();
    const app = electronApp();
    takeLinks(app, ["C:\\Program Files\\Surogate\\Surogate.exe"]);
    expect(app.asked).toEqual([["links", "surogate", "C:\\Program Files\\Surogate\\Surogate.exe", ["--"]]]);
    takeLinks(app, ["C:\\Program Files\\Surogate\\Surogate.exe"]);
    expect(app.asked).toHaveLength(1);
    // A development build's Electron, on its main, quoted as the Run key's command is.
    const development = electronApp();
    takeLinks(development, ["C:\\a\\electron.exe", "C:\\my work\\dist\\shell\\main.js"]);
    expect(development.asked).toEqual([["links", "surogate", "C:\\a\\electron.exe", ['"C:\\my work\\dist\\shell\\main.js"', "--"]]]);
  });

  it("says where Windows did not take the links", async () => {
    const { takeLinks } = await windows();
    const app = { ...electronApp(), setAsDefaultProtocolClient: () => false };
    expect(() => takeLinks(app, ["C:\\Program Files\\Surogate\\Surogate.exe"])).toThrow("Windows did not give Surogate its surogate:// links");
  });

  it("is asked to quit by a launch whose arguments hold --quit", async () => {
    const { quitAsked } = await windows();
    expect(quitAsked(["C:\\Program Files\\Surogate\\Surogate.exe", "--quit"])).toBe(true);
    expect(quitAsked(["C:\\Program Files\\Surogate\\Surogate.exe"])).toBe(false);
    expect(quitAsked(["C:\\Program Files\\Surogate\\Surogate.exe", "--", "surogate://open?url=--quit"])).toBe(false);
  });
});

describe("the app as Linux and macOS know it", () => {
  it("is named, given its links and asked to quit by nothing of the app's own: an install's files and the bundle do it", async () => {
    for (const os of ["linux", "darwin"] as const) {
      const { nameTheApp, takeLinks, quitAsked } = await importedOn(os, () => import("../src/shell/app-identity.js"));
      const app = electronApp();
      nameTheApp(app, true);
      takeLinks(app, ["/opt/surogate/current/surogate"]);
      expect(app.asked, os).toEqual([]);
      expect(quitAsked(["/opt/surogate/current/surogate", "--quit"]), os).toBe(false);
    }
  });
});
