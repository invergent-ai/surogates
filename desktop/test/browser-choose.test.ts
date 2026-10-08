import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, describe, expect, it } from "vitest";

import {
  BrowserSetting, choiceRows, chosenBrowser, confined, type Disk, findBrowsers, profileOf, profilesOf,
} from "../src/browser/choose.js";

// A computer's programs in a test: each path, what it resolves to, whether that can run, and the scripts' first lines.
function disk(links: Record<string, string>, runnable: string[] = Object.values(links), heads: Record<string, string> = {}): Disk {
  return {
    realpath: (path) => {
      const real = links[path];
      if (real === undefined) throw new Error("ENOENT");
      return real;
    },
    executable: (path) => runnable.includes(path),
    head: (path) => heads[path] ?? "\x7fELF",
  };
}

// Ubuntu's chromium-browser package: a script that runs the Snap.
const UBUNTU_WRAPPER = `#!/bin/sh
if ! [ -x /snap/bin/chromium ]; then
  echo "Command '$0' requires the chromium snap to be installed." >&2
  exit 1
fi
exec /snap/bin/chromium "$@"
`;

describe("choosing the agent's browser", () => {
  it("finds each known browser once, at its own program, in the order Settings lists them", () => {
    const found = findBrowsers(disk({
      "/usr/bin/microsoft-edge": "/opt/microsoft/msedge/microsoft-edge",
      "/opt/microsoft/msedge/msedge": "/opt/microsoft/msedge/msedge",
      "/usr/bin/google-chrome": "/opt/google/chrome/google-chrome",
      "/opt/google/chrome/chrome": "/opt/google/chrome/chrome",
      "/opt/brave.com/brave/brave": "/opt/brave.com/brave/brave",
    }));
    expect(found).toEqual([
      { id: "chrome", name: "Google Chrome", executable: "/opt/google/chrome/chrome", verified: true, unsupported: null },
      { id: "edge", name: "Microsoft Edge", executable: "/opt/microsoft/msedge/msedge", verified: true, unsupported: null },
      { id: "brave", name: "Brave", executable: "/opt/brave.com/brave/brave", verified: false, unsupported: null },
    ]);
  });

  it("lists Ubuntu's Snap Chromium and a Flatpak as found but not supported, and none of them is chosen", () => {
    const found = findBrowsers(disk({ "/snap/bin/chromium": "/usr/bin/snap", "/usr/bin/vivaldi": "/var/lib/flatpak/exports/bin/com.vivaldi.Vivaldi" }));
    expect(found.map(({ id, unsupported }) => [id, unsupported])).toEqual([
      ["vivaldi", "a Flatpak build is not supported"],
      ["chromium", "the Snap build is not supported"],
    ]);
    expect(chosenBrowser({ choice: "auto" }, found)).toBeNull();
    expect(chosenBrowser({ choice: "chromium" }, found)).toBeNull();
    expect(confined("/snap/chromium/current/usr/lib/chromium-browser/chrome")).not.toBeNull();
  });

  it("refuses Ubuntu's chromium-browser, a script that runs the Snap, as the Snap build, beside the Snap itself", () => {
    const ubuntu = { "/usr/bin/chromium-browser": "/usr/bin/chromium-browser", "/snap/bin/chromium": "/usr/bin/snap" };
    const found = findBrowsers(disk(ubuntu, Object.values(ubuntu), { "/usr/bin/chromium-browser": UBUNTU_WRAPPER }));
    expect(found).toEqual([
      { id: "chromium", name: "Chromium", executable: "/usr/bin/snap", verified: false, unsupported: "the Snap build is not supported" },
    ]);
    expect(chosenBrowser({ choice: "auto" }, found)).toBeNull();
    // The wrapper alone, the Snap removed: refused all the same.
    const wrapper = { "/usr/bin/chromium-browser": "/usr/bin/chromium-browser" };
    expect(findBrowsers(disk(wrapper, Object.values(wrapper), { "/usr/bin/chromium-browser": UBUNTU_WRAPPER }))[0]?.unsupported).toBe("the Snap build is not supported");
    // Debian's Chromium, its own program, beside the Snap: the one that can be the agent's.
    const debian = { "/snap/bin/chromium": "/usr/bin/snap", "/usr/lib/chromium/chromium": "/usr/lib/chromium/chromium" };
    expect(findBrowsers(disk(debian))[0]).toMatchObject({ executable: "/usr/lib/chromium/chromium", unsupported: null });
    // Picked as Custom…: refused too.
    expect(chosenBrowser({ choice: "custom", executable: "/usr/bin/chromium-browser", version: "" }, [], disk(wrapper, Object.values(wrapper), { "/usr/bin/chromium-browser": UBUNTU_WRAPPER }))).toBeNull();
  });

  it("takes the first supported browser under Automatic, and only the one chosen otherwise", () => {
    const found = findBrowsers(disk({ "/opt/microsoft/msedge/msedge": "/opt/microsoft/msedge/msedge", "/opt/vivaldi/vivaldi": "/opt/vivaldi/vivaldi" }));
    expect(chosenBrowser({ choice: "auto" }, found)?.id).toBe("edge");
    expect(chosenBrowser({ choice: "vivaldi" }, found)?.id).toBe("vivaldi");
    // Chosen, then uninstalled: no browser, rather than another the user did not pick.
    expect(chosenBrowser({ choice: "chrome" }, found)).toBeNull();
  });

  it("keeps a custom browser while it is still there and not confined", () => {
    const custom = { choice: "custom" as const, executable: "/home/u/bin/chrome-dev", version: "155.0.1" };
    expect(chosenBrowser(custom, [], disk({ "/home/u/bin/chrome-dev": "/home/u/apps/chrome-dev/chrome" }))).toEqual({
      id: "custom", name: "Your browser", executable: "/home/u/apps/chrome-dev/chrome", verified: false, unsupported: null,
    });
    expect(chosenBrowser(custom, [], disk({}))).toBeNull();
    expect(chosenBrowser(custom, [], disk({ "/home/u/bin/chrome-dev": "/snap/bin/chromium" }))).toBeNull();
    expect(chosenBrowser(custom, [], disk({ "/home/u/bin/chrome-dev": "/home/u/chrome" }, []))).toBeNull();
  });

  it("offers Automatic, each browser found with its version, the custom one kept and Custom…", () => {
    const found = findBrowsers(disk({ "/opt/google/chrome/chrome": "/opt/google/chrome/chrome", "/opt/brave.com/brave/brave": "/opt/brave.com/brave/brave", "/snap/bin/chromium": "/usr/bin/snap" }));
    const rows = choiceRows({ choice: "custom", executable: "/home/u/chrome", version: "155.0" }, found, new Map([["/opt/google/chrome/chrome", "154.0.8037.92"]]));
    expect(rows).toEqual([
      { value: "auto", label: "Automatic (Google Chrome 154.0.8037.92)", disabled: false },
      { value: "chrome", label: "Google Chrome 154.0.8037.92", disabled: false },
      { value: "brave", label: "Brave (not verified)", disabled: false },
      { value: "chromium", label: "Chromium: the Snap build is not supported", disabled: true },
      { value: "custom", label: "/home/u/chrome 155.0", disabled: false },
      { value: "pick", label: "Custom…", disabled: false },
    ]);
    expect(choiceRows({ choice: "auto" }, [], new Map())[0]?.label).toBe("Automatic (none found)");
  });
});

describe("the browser setting and the profiles", () => {
  let folder = "";
  afterEach(() => rmSync(folder, { recursive: true, force: true }));

  it("is Automatic until a choice is kept, and reads anything else it finds as Automatic", () => {
    folder = mkdtempSync(join(tmpdir(), "sd-browser-"));
    const setting = new BrowserSetting(join(folder, "browser.json"));
    expect(setting.get()).toEqual({ choice: "auto" });
    setting.set({ choice: "edge" });
    expect(setting.get()).toEqual({ choice: "edge" });
    setting.set({ choice: "custom", executable: "/home/u/chrome", version: "155" });
    expect(setting.get()).toEqual({ choice: "custom", executable: "/home/u/chrome", version: "155" });
    for (const saved of ['{"choice":"firefox"}', '{"choice":"custom","executable":"relative/chrome"}', "[]"]) {
      writeFileSync(join(folder, "browser.json"), saved);
      expect(setting.get()).toEqual({ choice: "auto" });
    }
  });

  it("keeps one profile per identity and per browser, under the app's own state", () => {
    const who = { origin: "https://acme.surogate.ai", orgId: "o", agentId: "a", userId: "u" };
    const chrome = { id: "chrome" as const, name: "Google Chrome", executable: "/opt/google/chrome/chrome", verified: true, unsupported: null };
    const edge = { ...chrome, id: "edge" as const, executable: "/opt/microsoft/msedge/msedge" };
    expect(profileOf("/data", who, chrome)).toBe(join(profilesOf("/data", who), "chrome"));
    expect(profilesOf("/data", who)).toMatch(/^\/data\/browser-profiles\/[0-9a-f]{32}$/);
    expect(profileOf("/data", who, edge)).not.toBe(profileOf("/data", who, chrome));
    // Another user of the same agent never shares one.
    expect(profilesOf("/data", { ...who, userId: "v" })).not.toBe(profilesOf("/data", who));
    expect(profileOf("/data", who, { ...chrome, id: "custom" })).toMatch(/\/custom-[0-9a-f]{12}$/);
  });
});
