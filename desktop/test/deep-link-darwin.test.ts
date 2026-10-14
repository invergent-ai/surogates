// surogate:// links on macOS: none in a launch's arguments; each in the running app's open-url event,
// which Launch Services sends it, opened as a second launch's is on Linux.

import { EventEmitter } from "node:events";

import { describe, expect, it } from "vitest";

import type { LinkEvents, OpenLink } from "../src/shell/deep-link.js";
import { importedOn } from "./platforms.js";

const CHAT = "7d2e0f8a-2b3c-4d5e-9f60-718293a4b5c6";
const link = (url: string) => `surogate://open?url=${encodeURIComponent(url)}`;

// The open-url event as Electron emits it: an event the app may prevent the default of, and the address.
function opening(app: EventEmitter, url: string): { prevented: boolean } {
  const event = { prevented: false, preventDefault: () => void (event.prevented = true) };
  app.emit("open-url", event, url);
  return event;
}

describe("a surogate:// link on macOS", () => {
  it("comes in no launch's arguments", async () => {
    const { linkOfLaunch } = await importedOn("darwin", () => import("../src/shell/deep-link.js"));
    expect(linkOfLaunch(["/Applications/Surogate.app/Contents/MacOS/Surogate", link("https://agent.example.com")])).toBeNull();
  });

  it("comes in the app's open-url event, which the app handles itself, and opens what it names", async () => {
    const { listenForLinks } = await importedOn("darwin", () => import("../src/shell/deep-link.js"));
    const app = new EventEmitter();
    const opened: OpenLink[] = [];
    listenForLinks(app as unknown as LinkEvents, (found) => opened.push(found));
    expect(opening(app, link(`https://agent.example.com/chat/${CHAT}`)).prevented).toBe(true);
    // No link the app opens: handled all the same, and nothing opened.
    expect(opening(app, "surogate://grant?folder=%2FUsers%2Fana").prevented).toBe(true);
    expect(opening(app, "https://agent.example.com/").prevented).toBe(true);
    expect(opened).toEqual([{ origin: "https://agent.example.com", path: `/chat/${CHAT}` }]);
  });

  it("is heard of by no event on Linux, whose links come in a launch's arguments", async () => {
    const { listenForLinks } = await importedOn("linux", () => import("../src/shell/deep-link.js"));
    const app = new EventEmitter();
    listenForLinks(app as unknown as LinkEvents, () => {});
    expect(app.listenerCount("open-url")).toBe(0);
  });
});
