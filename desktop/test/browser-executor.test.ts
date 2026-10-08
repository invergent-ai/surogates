import { describe, expect, it } from "vitest";

import { PAUSED } from "../src/browser/client.js";
import { Browsing, NO_BROWSER } from "../src/browser/executor.js";
import type { Launch } from "../src/browser/host.js";
import { FOLDER_UNAVAILABLE } from "../src/hosts/messages.js";
import type { Operation, Outcome } from "../src/link/protocol.js";
import type { ToolLayer } from "../src/shell/device-stack.js";

const ROOT = "4e5f6a7b-8c9d-4e0f-a1b2-c3d4e5f6a7b8";
// Another chat of the agent's on this computer: its browser is the same one.
const OTHER = "5f6a7b8c-9d0e-4f1a-b2c3-d4e5f6a7b8c9";
const op = (kind: string, root = ROOT): Operation => ({
  id: `op-${kind}`, sessionId: root, callingSessionId: root, invocationId: "call", ordinal: 1, kind, args: {}, digest: "d",
});
const LAUNCH: Launch = { executable: "/opt/google/chrome/chrome", profile: "/data/browser-profiles/x/chrome" };
const signal = new AbortController().signal;

function rig(launch: Launch | null = LAUNCH, bound = true) {
  const ran: string[] = [];
  const browsed: Array<{ launch: Launch; kind: string }> = [];
  const stopped: string[] = [];
  const forgotten: string[] = [];
  const paused: Array<[string, boolean]> = [];
  const shown: string[] = [];
  // The chats this computer bound, until one is deleted.
  const chats = new Set(bound ? [ROOT, OTHER] : []);
  const tools: ToolLayer = {
    run: (operation) => (ran.push(operation.kind), Promise.resolve({ ok: "tools" })),
    refusal: () => ({ error: { type: "other", message: "from the tools" } }),
    guards: () => ({ home: "/home/u", dataDir: "/data", appDirs: [] }),
    live: () => [],
    stop: () => (stopped.push("tools"), Promise.resolve()),
    end: () => (stopped.push("tools ended"), Promise.resolve()),
    retired: (root) => void forgotten.push(`tools ${root}`),
  };
  const browsing = new Browsing({
    tools,
    browser: {
      perform: (chosen, operation) => (browsed.push({ launch: chosen, kind: operation.kind }), Promise.resolve<Outcome>({ ok: "browser" })),
      forget: (root) => void forgotten.push(`browser ${root}`),
      stop: () => (stopped.push("browser"), Promise.resolve()),
      end: () => (stopped.push("browser ended"), Promise.resolve()),
      address: (session) => Promise.resolve(`https://example.com/${session}`),
      pause: (root, held) => void paused.push([root, held]),
      show: (root) => (shown.push(root), Promise.resolve(true)),
    },
    bindingOf: (root) => (chats.has(root) ? {} : undefined),
    launch: () => launch,
  });
  return { browsing, ran, browsed, stopped, forgotten, paused, shown, chats };
}

describe("the browser's kinds beside the tools", () => {
  it("sends the browser's kinds to the browser with the launch chosen now, and every other kind to the tools", async () => {
    const { browsing, ran, browsed } = rig();
    expect(await browsing.run(op("browser.navigate"), signal)).toEqual({ ok: "browser" });
    expect(await browsing.run(op("read"), signal)).toEqual({ ok: "tools" });
    expect(browsed).toEqual([{ launch: LAUNCH, kind: "browser.navigate" }]);
    expect(ran).toEqual(["read"]);
  });

  it("refuses the browser's kinds before anyone is asked when no browser is here, and leaves the tools' own refusals to them", async () => {
    expect(rig(null).browsing.refusal(op("browser.observe"))).toEqual(NO_BROWSER);
    expect(rig().browsing.refusal(op("browser.observe"))).toBeNull();
    expect(rig().browsing.refusal(op("write"))).toEqual({ error: { type: "other", message: "from the tools" } });
    expect(await rig(null).browsing.run(op("browser.navigate"), signal)).toEqual(NO_BROWSER);
  });

  it("runs no browser operation for a chat this computer did not bind", async () => {
    const { browsing, browsed } = rig(LAUNCH, false);
    expect(await browsing.run(op("browser.navigate"), signal)).toEqual(FOLDER_UNAVAILABLE);
    expect(browsed).toEqual([]);
  });

  it("answers every chat's browser operations paused_by_user while a chat's user holds the browser, before anyone is asked, until that chat hands it back", async () => {
    const { browsing, browsed, paused, ran } = rig();
    expect(browsing.takeOver(ROOT)).toBe(true);
    expect(browsing.takenOver(ROOT)).toBe(true);
    // The browser is the agent's one browser here: a chat that never asked for the take-over is answered so too.
    for (const root of [ROOT, OTHER]) {
      for (const kind of ["browser.navigate", "browser.observe", "browser.close"]) {
        expect(browsing.refusal(op(kind, root))).toEqual(PAUSED);
        // One the binder let through before the take-over is answered so too, and never reaches the browser.
        expect(await browsing.run(op(kind, root), signal)).toEqual(PAUSED);
      }
    }
    expect(browsed).toEqual([]);
    // A chat's other tools are not the browser's: they run as before.
    expect(browsing.refusal(op("read"))).toEqual({ error: { type: "other", message: "from the tools" } });
    expect(await browsing.run(op("read"), signal)).toEqual({ ok: "tools" });
    expect(ran).toEqual(["read"]);
    browsing.handBack(ROOT);
    expect(browsing.takenOver(ROOT)).toBe(false);
    expect(browsing.takenOver(OTHER)).toBe(false);
    for (const root of [ROOT, OTHER]) {
      expect(browsing.refusal(op("browser.navigate", root))).toBeNull();
      expect(await browsing.run(op("browser.navigate", root), signal)).toEqual({ ok: "browser" });
    }
    // The browser host is told each, for an operation already waiting or acting there.
    expect(paused).toEqual([[ROOT, true], [ROOT, false]]);
  });

  it("lets no other chat take the browser from the chat that holds it, nor hand it back", async () => {
    const { browsing, paused } = rig();
    expect(browsing.takeOver(ROOT)).toBe(true);
    // Another chat's take-over does not steal it: that chat is told it is held elsewhere, and the first still holds it.
    expect(browsing.takeOver(OTHER)).toBe(false);
    expect(browsing.takenOver(OTHER)).toBe("elsewhere");
    expect(browsing.takenOver(ROOT)).toBe(true);
    // Nor does its hand back end it.
    browsing.handBack(OTHER);
    expect(browsing.takenOver(ROOT)).toBe(true);
    expect(browsing.refusal(op("browser.navigate", OTHER))).toEqual(PAUSED);
    // Taken over again by the chat that holds it: held as before, and the host is told nothing more.
    expect(browsing.takeOver(ROOT)).toBe(true);
    expect(paused).toEqual([[ROOT, true]]);
  });

  it("keeps the browser held when the chat that took it over is deleted, and lets any chat hand it back then", async () => {
    const { browsing, browsed, chats, paused } = rig();
    browsing.takeOver(ROOT);
    // Deleted, as a page can have a chat deleted: nothing is handed back by that.
    browsing.retired(ROOT);
    chats.delete(ROOT);
    expect(browsing.refusal(op("browser.navigate", OTHER))).toEqual(PAUSED);
    expect(await browsing.run(op("browser.navigate", OTHER), signal)).toEqual(PAUSED);
    expect(browsed).toEqual([]);
    // No chat holds it now, and every chat is told so: neither "held from this chat" nor "not held".
    expect(browsing.takenOver(OTHER)).toBe("orphaned");
    // The chat that held it can hand nothing back, so any chat's hand back ends it: the desktop confirms that one as any.
    browsing.handBack(OTHER);
    expect(browsing.takenOver(OTHER)).toBe(false);
    expect(browsing.refusal(op("browser.navigate", OTHER))).toBeNull();
    expect(paused).toEqual([[ROOT, true], [ROOT, false]]);
  });

  it("takes a deleted chat for gone though its folder could not be forgotten here, and lets the next chat take the browser over as well", () => {
    const { browsing, paused } = rig();
    browsing.takeOver(ROOT);
    // Deleted, its binding left behind.
    browsing.retired(ROOT);
    expect(browsing.takenOver(OTHER)).toBe("orphaned");
    // Taken over from another chat: that one holds it now, as any holder.
    expect(browsing.takeOver(OTHER)).toBe(true);
    expect(browsing.takenOver(OTHER)).toBe(true);
    expect(browsing.takenOver(ROOT)).toBe("elsewhere");
    browsing.handBack(ROOT);
    expect(browsing.takenOver(OTHER)).toBe(true);
    browsing.handBack(OTHER);
    expect(browsing.takenOver(OTHER)).toBe(false);
    expect(paused).toEqual([[ROOT, true], [OTHER, true], [OTHER, false]]);
    // Held anew from a chat that is here, it is that chat's alone again.
    browsing.takeOver(OTHER);
    expect(browsing.takenOver(ROOT)).toBe("elsewhere");
  });

  it("tells each chat where the agent's browser is held: from it, by nobody, from another chat that is here, or from one that is gone", () => {
    const { browsing, chats } = rig();
    const told = () => [browsing.takenOver(ROOT), browsing.takenOver(OTHER)];
    expect(told()).toEqual([false, false]);
    browsing.takeOver(ROOT);
    // The other chat can neither take it nor hand it back, and is told so: not "nobody holds it".
    expect(told()).toEqual([true, "elsewhere"]);
    browsing.handBack(ROOT);
    expect(told()).toEqual([false, false]);
    browsing.takeOver(OTHER);
    expect(told()).toEqual(["elsewhere", true]);
    // The chat it is held from is gone: any chat may hand it back, and each that is left is told that.
    chats.delete(OTHER);
    expect(browsing.takenOver(ROOT)).toBe("orphaned");
    browsing.handBack(ROOT);
    expect(browsing.takenOver(ROOT)).toBe(false);
  });

  it("takes a chat whose folder was forgotten on this computer for gone too", () => {
    const { browsing, chats } = rig();
    browsing.takeOver(ROOT);
    chats.delete(ROOT);
    expect(browsing.takenOver(OTHER)).toBe("orphaned");
    browsing.handBack(OTHER);
    expect(browsing.takenOver(OTHER)).toBe(false);
  });

  it("asks the browser to show a chat's page", async () => {
    const { browsing, shown } = rig();
    expect(await browsing.show(ROOT)).toBe(true);
    expect(shown).toEqual([ROOT]);
  });

  it("asks the browser for the address of the page a session acts in", async () => {
    expect(await rig().browsing.address("child")).toBe("https://example.com/child");
  });

  it("closes a deleted chat's tabs, and tells the tools beneath", () => {
    const { browsing, forgotten } = rig();
    browsing.retired(ROOT);
    expect(forgotten).toEqual([`browser ${ROOT}`, `tools ${ROOT}`]);
  });

  it("closes the browser with the tools at a stop, and when the computer's access ends", async () => {
    const { browsing, stopped } = rig();
    await browsing.end();
    await browsing.stop();
    expect(stopped).toEqual(["browser ended", "tools ended", "browser", "tools"]);
  });
});
