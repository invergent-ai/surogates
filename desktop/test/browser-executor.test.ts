import { describe, expect, it } from "vitest";

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
    },
    bindingOf: (root) => (bound && root === ROOT ? {} : undefined),
    launch: () => launch,
  });
  return { browsing, ran, browsed, stopped, forgotten };
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
