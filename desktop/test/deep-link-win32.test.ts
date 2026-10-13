// surogate:// links on Windows: as on Linux, in the arguments the app is started with, or in a second launch's, which
// Electron hands the running app, by the command the scheme's registration names (the MSI's, machine-wide, or a
// development build's script's, for its user); the system hands it none as an event.

import { EventEmitter } from "node:events";

import { describe, expect, it } from "vitest";

import type { LinkEvents } from "../src/shell/deep-link.js";
import { importedOn } from "./platforms.js";

const CHAT = "7d2e0f8a-2b3c-4d5e-9f60-718293a4b5c6";
const link = (url: string) => `surogate://open?url=${encodeURIComponent(url)}`;

describe("a surogate:// link on Windows", () => {
  it("comes in a launch's arguments, after the end of its switches, as the scheme's command gives it", async () => {
    const { linkOfLaunch } = await importedOn("win32", () => import("../src/shell/deep-link.js"));
    expect(linkOfLaunch(["C:\\Program Files\\Surogate\\Surogate.exe", "--", link(`https://agent.example.com/chat/${CHAT}`)]))
      .toEqual({ origin: "https://agent.example.com", path: `/chat/${CHAT}` });
    expect(linkOfLaunch(["C:\\Program Files\\Surogate\\Surogate.exe"])).toBeNull();
    expect(linkOfLaunch(["C:\\Program Files\\Surogate\\Surogate.exe", "--", "surogate://grant?folder=C%3A%5CUsers"])).toBeNull();
  });

  it("is heard of by no event", async () => {
    const { listenForLinks } = await importedOn("win32", () => import("../src/shell/deep-link.js"));
    const app = new EventEmitter();
    listenForLinks(app as unknown as LinkEvents, () => {});
    expect(app.listenerCount("open-url")).toBe(0);
  });
});
