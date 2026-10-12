// The browsers the app knows on Linux, where each one's package puts it, and what it refuses: the
// list and the rules the app has always had, now Linux's own beside each other OS's.

import { expect, it } from "vitest";

import { LINUX } from "../src/browser/known-linux.js";
import { importedOn } from "./platforms.js";

it("lists each browser where its package puts it, its own program first, in the order Settings lists them, wherever the home is", () => {
  const listed = [
    { id: "chrome", name: "Google Chrome", paths: ["/opt/google/chrome/chrome", "/usr/bin/google-chrome-stable", "/usr/bin/google-chrome"], verified: true },
    { id: "edge", name: "Microsoft Edge", paths: ["/opt/microsoft/msedge/msedge", "/usr/bin/microsoft-edge-stable", "/usr/bin/microsoft-edge"], verified: true },
    { id: "brave", name: "Brave", paths: ["/opt/brave.com/brave/brave", "/usr/bin/brave-browser"], verified: false },
    { id: "vivaldi", name: "Vivaldi", paths: ["/opt/vivaldi/vivaldi", "/usr/bin/vivaldi-stable", "/usr/bin/vivaldi"], verified: false },
    { id: "chromium", name: "Chromium", paths: ["/snap/bin/chromium", "/usr/lib/chromium/chromium", "/usr/bin/chromium", "/usr/bin/chromium-browser"], verified: false },
  ];
  expect(LINUX.candidates("/home/ana")).toEqual(listed);
  expect(LINUX.candidates("/root")).toEqual(listed);
});

it("runs what the user picked in Custom… as it is, resolved, and refuses Snap and Flatpak builds and a script that runs a Snap", () => {
  const disk = {
    realpath: (path: string) => {
      if (path === "/home/ana/chrome") return "/opt/google/chrome/chrome";
      throw new Error("ENOENT");
    },
    executable: () => true,
    head: (path: string) => (path === "/usr/bin/chromium-browser" ? "#!/bin/sh\nexec /snap/bin/chromium \"$@\"\n" : "\x7fELF"),
  };
  expect(LINUX.programOf("/home/ana/chrome", disk)).toBe("/opt/google/chrome/chrome");
  expect(LINUX.programOf("/home/ana/gone", disk)).toBeNull();
  expect([
    LINUX.refused("/opt/google/chrome/chrome", disk), LINUX.refused("/usr/bin/snap", disk), LINUX.refused("/snap/chromium/current/usr/lib/chromium-browser/chrome", disk),
    LINUX.refused("/var/lib/flatpak/exports/bin/com.vivaldi.Vivaldi", disk), LINUX.refused("/usr/bin/chromium-browser", disk),
  ]).toEqual([null, "the Snap build is not supported", "the Snap build is not supported", "a Flatpak build is not supported", "the Snap build is not supported"]);
});

it("knows none on a system the app drives no browser on yet, and refuses each one picked there in the words every such part says", async () => {
  const { KNOWN_BROWSERS } = await importedOn("win32", () => import("../src/browser/known.js"));
  const disk = { realpath: (path: string) => path, executable: () => true, head: () => "" };
  expect(KNOWN_BROWSERS.candidates("C:\\Users\\ana")).toEqual([]);
  expect(KNOWN_BROWSERS.refused("C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe", disk)).toBe("driving a browser is not built for win32 yet");
});
