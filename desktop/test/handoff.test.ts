// The update line's button, the removal from the menu under the user's name, and a start, where the OS's helper goes
// on once the app has quit (src/shell/handoff.ts): on macOS and on Windows the quit's own question comes before the
// helper is started, and a cancel starts nothing; on Linux the button does as it always has. Each OS's row of the
// install's (installer.ts) as that OS's app imports it, with a removal, an update and a quit of the test's own.

import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { type QuitQuestion, quitSettled, removeAction, startDuringApply, updateAction } from "../src/shell/handoff.js";
import type { Removal } from "../src/shell/installer.js";
import type { Places } from "../src/shell/places.js";
import type { Applied, UpdateState } from "../src/shell/updates.js";
import { importedOn } from "./platforms.js";

const rowOf = (platform: NodeJS.Platform) => importedOn(platform, async () => (await import("../src/shell/installer.js")).INSTALLER);

// What the app did, in order.
let did: string[];

// The quit's question, with *working* threads, answered *answer*; the threads end when the test says.
function question(working: number, answer: "quit" | "wait" | "cancel" = "quit"): QuitQuestion & { end(): void } {
  let count = working;
  let done = () => {};
  return {
    working: () => count,
    ask: (asked) => (did.push(`ask ${asked}`), Promise.resolve(answer)),
    threadsDone: () => (did.push("wait"), new Promise<void>((resolve) => {
      done = resolve;
    })),
    end: () => {
      count = 0;
      did.push("threads done");
      done();
    },
  };
}

// An update downloaded, whose install ends in *after*.
function update(after: UpdateState) {
  const files = { manifest: "m", signature: "s", tarball: "t" };
  const updates = {
    state: { state: "available", version: "1.2.4", files } as UpdateState,
    install: () => {
      did.push("install");
      updates.state = after;
      return Promise.resolve();
    },
  };
  return updates;
}

const app = (asked: QuitQuestion) => ({
  question: asked, quit: () => did.push("quit"), restart: () => did.push("restart"), open: (url: string) => did.push(`open ${url}`), report: (error: unknown) => did.push(`report ${String(error)}`),
});

const OS_HANDING_OFF = ["darwin", "win32"] as const;

describe("the update line's button", () => {
  for (const os of OS_HANDING_OFF) {
    it(`on ${os}, asks the quit's question before the helper starts where threads work, and then quits asking nothing more`, async () => {
      const installer = await rowOf(os);
      expect(installer.afterQuit).toBe(true);
      did = [];
      await updateAction(update({ state: "restarting", version: "1.2.4" }), installer, app(question(2)));
      expect(did).toEqual(["ask 2", "install", "quit"]);
      // With none working, nothing is asked.
      did = [];
      await updateAction(update({ state: "restarting", version: "1.2.4" }), installer, app(question(0)));
      expect(did).toEqual(["install", "quit"]);
    });

    it(`on ${os}, starts no helper and does not quit at a cancel, and the line stays as it was`, async () => {
      const installer = await rowOf(os);
      did = [];
      const updates = update({ state: "restarting", version: "1.2.4" });
      const before = updates.state;
      await updateAction(updates, installer, app(question(1, "cancel")));
      expect(did).toEqual(["ask 1"]);
      expect(updates.state).toBe(before);
    });

    it(`on ${os}, waits for the threads first where the answer is to wait, and starts the helper once they are done`, async () => {
      const installer = await rowOf(os);
      did = [];
      const asked = question(3, "wait");
      const clicked = updateAction(update({ state: "restarting", version: "1.2.4" }), installer, app(asked));
      await expect.poll(() => did).toEqual(["ask 3", "wait"]);
      asked.end();
      await clicked;
      expect(did).toEqual(["ask 3", "wait", "threads done", "install", "quit"]);
    });

    it(`on ${os}, quits for nothing where the helper did not go on`, async () => {
      const installer = await rowOf(os);
      did = [];
      await updateAction(update({ state: "failed", version: "1.2.4", files: { manifest: "m", signature: "s", tarball: "t" }, why: "no" }), installer, app(question(1)));
      expect(did).toEqual(["ask 1", "install"]);
    });
  }

  it("on Linux, installs at once while the app runs, and its restart asks as any quit does", async () => {
    const installer = await rowOf("linux");
    expect(installer.afterQuit).toBe(false);
    did = [];
    await updateAction(update({ state: "installed", version: "1.2.4" }), installer, app(question(2)));
    expect(did).toEqual(["install", "restart"]);
  });

  it("opens the disk image where an administrator installs the update, and installs nothing", async () => {
    did = [];
    const updates = { state: { state: "administrator", version: "1.2.4", download: "https://surogate.ai/desktop/darwin-arm64/Surogate.dmg" } as UpdateState, install: () => Promise.reject(new Error("no")) };
    await updateAction(updates, await rowOf("darwin"), app(question(2)));
    expect(did).toEqual(["open https://surogate.ai/desktop/darwin-arm64/Surogate.dmg"]);
    // A line with no button does nothing.
    did = [];
    await updateAction({ state: { state: "installing", version: "1.2.4" }, install: () => Promise.reject(new Error("no")) }, await rowOf("darwin"), app(question(2)));
    expect(did).toEqual([]);
  });
});

const places: Places = { root: "/u/data", cache: "/u/cache", autostartFolder: null, launcher: null };

// A removal of the test's own, whose helper answers *applied*.
function removal(applied: Applied): Removal {
  return {
    item: "Remove Surogate from This Mac…", asked: () => ({ message: "Remove?", detail: "d", checkbox: "c" }), forget: (given) => [given.root, given.cache],
    run: (forget) => (did.push(`run ${forget.join(" ")}`.trim()), Promise.resolve(applied)),
  };
}

const removing = (asked: QuitQuestion, answer: { remove: boolean; forget: boolean }) => ({
  question: asked,
  confirm: (words: { message: string }) => (did.push(`confirm ${words.message}`), Promise.resolve(answer)),
  loginOff: () => (did.push("login item off"), Promise.resolve()),
  quit: () => did.push("quit"),
  failed: (applied: Applied) => (did.push(`failed ${applied.code ?? "-"}`), Promise.resolve()),
});

describe("the removal", () => {
  for (const os of OS_HANDING_OFF) {
    it(`on ${os}, confirms first, then asks the quit's question where threads work, and only then turns the login item off and starts the helper`, async () => {
      expect((await rowOf(os)).uninstall).not.toBeNull();
      did = [];
      await removeAction(removal({ code: 0, said: "", restarts: true }), places, removing(question(1), { remove: true, forget: true }));
      expect(did).toEqual(["confirm Remove?", "ask 1", "login item off", "run /u/data /u/cache", "quit"]);
      did = [];
      await removeAction(removal({ code: 0, said: "", restarts: true }), places, removing(question(0), { remove: true, forget: false }));
      expect(did).toEqual(["confirm Remove?", "login item off", "run", "quit"]);
    });

    it(`on ${os}, does nothing more at a no to the removal, or a cancel of the quit`, async () => {
      did = [];
      await removeAction(removal({ code: 0, said: "", restarts: true }), places, removing(question(1), { remove: false, forget: true }));
      expect(did).toEqual(["confirm Remove?"]);
      did = [];
      await removeAction(removal({ code: 0, said: "", restarts: true }), places, removing(question(1, "cancel"), { remove: true, forget: true }));
      expect(did).toEqual(["confirm Remove?", "ask 1"]);
    });

    it(`on ${os}, says what came of a removal that did not go on, and does not quit`, async () => {
      did = [];
      await removeAction(removal({ code: 1223, said: "No administrator approved it" }), places, removing(question(0), { remove: true, forget: false }));
      expect(did).toEqual(["confirm Remove?", "login item off", "run", "failed 1223"]);
    });
  }

  it("on Linux, is none: the install script removes the app", async () => {
    did = [];
    const installer = await rowOf("linux");
    await removeAction(installer.uninstall, places, removing(question(1), { remove: true, forget: true }));
    expect(did).toEqual([]);
  });
});

describe("a start while the helper puts an update in place", () => {
  it("touches nothing, says so, and quits, where its OS's helper is applying one", () => {
    did = [];
    const tell = { tell: (message: string, detail: string) => did.push(`${message}. ${detail}`), quit: () => did.push("quit") };
    expect(startDuringApply({ applying: (cache) => (did.push(`look in ${cache}`), true) }, "/u/cache/updates", tell)).toBe(true);
    expect(did).toEqual(["look in /u/cache/updates", "Surogate is installing its update. It opens again by itself once the update is in place.", "quit"]);
    did = [];
    expect(startDuringApply({ applying: () => false }, "/u/cache/updates", tell)).toBe(false);
    expect(did).toEqual([]);
  });

  it("is never found on Linux or Windows, whose rows look for none, and on macOS by a locked result file", async () => {
    const dir = mkdtempSync(join(tmpdir(), "handoff-"));
    try {
      mkdirSync(join(dir, "1.2.4"));
      writeFileSync(join(dir, "1.2.4", "result.json"), "");
      for (const os of ["linux", "win32"] as const) expect((await rowOf(os)).applying(dir), os).toBe(false);
      const darwin = await importedOn("darwin", async () => ({
        installer: (await import("../src/shell/installer.js")).INSTALLER, applyingUnder: (await import("../src/shell/installer-darwin.js")).applyingUnder,
      }));
      expect(darwin.installer.applying).toBe(darwin.applyingUnder);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});

describe("the quit's question before a handoff", () => {
  it("lets the app quit where no thread works, or the answer is to quit; after the threads where it is to wait; and not at a cancel", async () => {
    did = [];
    expect(await quitSettled(question(0))).toBe(true);
    expect(await quitSettled(question(2, "quit"))).toBe(true);
    expect(await quitSettled(question(2, "cancel"))).toBe(false);
    const asked = question(2, "wait");
    const settled = quitSettled(asked);
    await expect.poll(() => did.at(-1)).toBe("wait");
    asked.end();
    expect(await settled).toBe(true);
    expect(did).toEqual(["ask 2", "ask 2", "ask 2", "wait", "threads done"]);
  });
});
