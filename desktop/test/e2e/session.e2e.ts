// The tests' own session, as the launcher gives it to every app a test starts: the gate. No test
// app reaches the desktop's session, its bus, its keyring, its display or its folders, and a
// launch anywhere but on an Xvfb's display is refused before Electron starts.

import { readdirSync, rmSync, statSync } from "node:fs";

import type { ElectronApplication } from "playwright-core";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { dataHome, launch, quit, secondLaunch, shellEnv } from "./launch.js";

let home: string;
let app: ElectronApplication | undefined;

beforeEach(() => {
  home = dataHome();
});

afterEach(async () => {
  await quit(app);
  app = undefined;
  rmSync(home, { recursive: true, force: true });
});

// All a test app's environment holds: the image a VM test names, when the caller names one.
const LISTED = [
  "DBUS_SESSION_BUS_ADDRESS", "DISPLAY", "GDK_BACKEND", "HOME", "LANG", "NO_AT_BRIDGE", "PATH", "XAUTHORITY",
  "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR", "XDG_SESSION_TYPE", "XDG_STATE_HOME",
  ...(process.env.SUROGATE_VM_IMAGE ? ["SUROGATE_VM_IMAGE"] : []),
].sort();

// The test's own environment with *changed*, for as long as *run* takes.
async function within(changed: Record<string, string | undefined>, run: () => Promise<void>): Promise<void> {
  const before = Object.fromEntries(Object.keys(changed).map((name) => [name, process.env[name]]));
  const set = (values: Record<string, string | undefined>) => {
    for (const [name, value] of Object.entries(values)) {
      if (value === undefined) delete process.env[name];
      else process.env[name] = value;
    }
  };
  set(changed);
  try {
    await run();
  } finally {
    set(before);
  }
}

describe("the tests' own session", () => {
  it("is all a test app's environment holds: no bus, its own folders, X11 on xvfb, and the basic store, a second launch's too", async () => {
    const own = (environment: Record<string, string | undefined>) => {
      expect(environment).toMatchObject({ DBUS_SESSION_BUS_ADDRESS: "disabled:", NO_AT_BRIDGE: "1", XDG_SESSION_TYPE: "x11", GDK_BACKEND: "x11", XDG_DATA_HOME: home });
      for (const name of ["HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"]) {
        expect(environment[name]?.startsWith(`${home}/`), name).toBe(true);
      }
      // Short, under /tmp, and the user's alone: QEMU's control socket under it stays within 108 bytes.
      expect(environment.XDG_RUNTIME_DIR).toMatch(/^\/tmp\/rt-[^/]{6}$/);
      expect(statSync(environment.XDG_RUNTIME_DIR!).mode & 0o777).toBe(0o700);
      // xvfb's display and its authority, never the desktop's.
      expect(environment.DISPLAY).toMatch(/^:\d+$/);
      expect(environment.DISPLAY).not.toBe(":0");
      expect(environment.XAUTHORITY).toMatch(/\/xvfb-run\.[^/]+\/Xauthority$/);
    };
    const environment = shellEnv(home);
    expect(Object.keys(environment).sort()).toEqual(LISTED);
    own(environment);
    app = await launch(home);
    await app.firstWindow();
    const launched = await app.evaluate(() => ({ ...process.env }));
    own(launched);
    expect(launched).toMatchObject(environment);
    // Beside it, only what Chromium sets as it starts.
    expect(Object.keys(launched).filter((name) => !LISTED.includes(name)).sort()).toEqual(["CHROME_DESKTOP", "FC_FONTATIONS"]);
    expect(await app.evaluate(() => process.argv)).toContain("--password-store=basic");
    const handed = app.evaluate(({ app: electron }) => new Promise<string[]>((resolve) => {
      electron.once("second-instance", (_event, argv) => resolve(argv));
    }));
    expect(await secondLaunch(home)).toBe(0);
    expect(await handed).toContain("--password-store=basic");
  });

  it.each([
    ["on the desktop's display", { DISPLAY: ":0" }],
    ["with no display", { DISPLAY: undefined }],
    ["under the desktop's own authority", { XAUTHORITY: "/run/user/1000/gdm/Xauthority" }],
    ["with no authority", { XAUTHORITY: undefined }],
  ])("refuses a launch %s, before Electron starts", async (_why, changed) => {
    await within(changed, async () => {
      expect(() => shellEnv(home)).toThrow("A test app runs only on an Xvfb's display, under xvfb-run -a");
      await expect(launch(home)).rejects.toThrow("A test app runs only on an Xvfb's display");
      await expect(secondLaunch(home)).rejects.toThrow("A test app runs only on an Xvfb's display");
    });
    // Nothing started: no app made its state, and the launcher made none of its folders.
    expect(readdirSync(home)).toEqual([]);
  });
});
