import { describe, expect, it } from "vitest";

import { PAUSED } from "../src/browser/client.js";
import { Browsing, NO_BROWSER } from "../src/browser/executor.js";
import type { Launch } from "../src/browser/host.js";
import { FOLDER_UNAVAILABLE } from "../src/hosts/messages.js";
import type { Operation, Outcome } from "../src/link/protocol.js";
import type { ToolLayer } from "../src/shell/device-stack.js";

const ROOT = "4e5f6a7b-8c9d-4e0f-a1b2-c3d4e5f6a7b8";
const op = (kind: string): Operation => ({
  id: `op-${kind}`, sessionId: ROOT, callingSessionId: ROOT, invocationId: "call", ordinal: 1, kind, args: {}, digest: "d",
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
    bindingOf: (root) => (bound && root === ROOT ? {} : undefined),
    launch: () => launch,
  });
  return { browsing, ran, browsed, stopped, forgotten, paused, shown };
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

  it("answers a chat its user took the browser over paused_by_user, before anyone is asked, until it is handed back", async () => {
    const { browsing, browsed, paused, ran } = rig();
    browsing.takeOver(ROOT);
    expect(browsing.takenOver(ROOT)).toBe(true);
    for (const kind of ["browser.navigate", "browser.observe", "browser.close"]) {
      expect(browsing.refusal(op(kind))).toEqual(PAUSED);
      // One the binder let through before the take-over is answered so too, and never reaches the browser.
      expect(await browsing.run(op(kind), signal)).toEqual(PAUSED);
    }
    expect(browsed).toEqual([]);
    // The chat's other tools are not the browser's: they run as before.
    expect(browsing.refusal(op("read"))).toEqual({ error: { type: "other", message: "from the tools" } });
    expect(await browsing.run(op("read"), signal)).toEqual({ ok: "tools" });
    expect(ran).toEqual(["read"]);
    browsing.handBack(ROOT);
    expect(browsing.takenOver(ROOT)).toBe(false);
    expect(browsing.refusal(op("browser.navigate"))).toBeNull();
    expect(await browsing.run(op("browser.navigate"), signal)).toEqual({ ok: "browser" });
    // The browser host is told each, for an operation already waiting there.
    expect(paused).toEqual([[ROOT, true], [ROOT, false]]);
  });

  it("forgets a deleted chat's take-over with its tabs", () => {
    const { browsing } = rig();
    browsing.takeOver(ROOT);
    browsing.retired(ROOT);
    expect(browsing.takenOver(ROOT)).toBe(false);
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
