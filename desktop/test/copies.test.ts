import { existsSync, mkdirSync, mkdtempSync, readdirSync, realpathSync, renameSync, rmSync, statSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// A look in the app's data, held where a test says: before it is made (its lstat), or once it has seen what is
// there (its realpath, which comes last).
const looks = vi.hoisted(() => ({ held: null as null | { path: string; seen: boolean; reached(): void; gate: Promise<void> } }));
vi.mock("node:fs/promises", async (original) => {
  const real = await original<typeof import("node:fs/promises")>();
  const held = (path: unknown, seen: boolean) => {
    const hold = looks.held;
    if (hold === null || hold.seen !== seen || String(path) !== hold.path) return null;
    looks.held = null;
    return hold;
  };
  const lstat = async (...args: Parameters<typeof real.lstat>) => {
    const hold = held(args[0], false);
    if (hold) {
      hold.reached();
      await hold.gate;
    }
    return real.lstat(...args);
  };
  const realpath = async (...args: Parameters<typeof real.realpath>) => {
    const found = await real.realpath(...args);
    const hold = held(args[0], true);
    if (hold) {
      hold.reached();
      await hold.gate;
    }
    return found;
  };
  return { ...real, lstat, realpath, default: { ...real, lstat, realpath } };
});

import { BOOT_ID } from "../src/binding/folder.js";
import { CANCELLED, SANDBOX_STOPPED } from "../src/guest/command.js";
import { Copies, type CopiesOptions, historyOff } from "../src/history/copies.js";
import { keyOf } from "../src/history/place.js";
import { FOLDER_UNAVAILABLE } from "../src/hosts/messages.js";
import type { BoundFolder } from "../src/hosts/tool-hosts.js";
import type { Outcome } from "../src/link/protocol.js";
import type { HistoryRequest } from "../src/vm/history.js";
import { LET_GO, type Place } from "../src/vm/manager.js";

const ROOT = "0b6c1d3e-6f0a-4c1e-9a52-6a1d2c3b4e5f";
const OTHER = "7d8e9f00-1a2b-4c3d-8e4f-5a6b7c8d9e0f";
const THIRD = "3c4d5e6f-7a8b-4c9d-8e0f-1a2b3c4d5e6f";
const USER = "5f6a7b8c-9d0e-4f1a-8b2c-3d4e5f6a7b8c";
const HASH: Outcome = { ok: { hash: "a".repeat(40) } };
const STEP = { reason: "before a step" };
const signal = () => new AbortController().signal;
const pause = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

let dir: string;
let dataDir: string;
let folder: string;
let asked: HistoryRequest[];
let unplaced: Place[];
// What the guest was asked, in order: each request by its thread and action, and each place let go by its folder's inode.
let events: string[];
// Settles once the guest has let a place go: at once, unless a test holds it.
let letting: Promise<void>;
// The roots told that the copy their host works in is no longer their copy.
let replaced: string[];
let made: Copies[];

const identity = (path: string) => (({ dev, ino }) => ({ dev, ino }))(statSync(path));
const bound = (root = ROOT, at = folder): BoundFolder => ({ folder: at, ...identity(at), boot: BOOT_ID, history: root });
const place = (at = folder) => join(dataDir, "history", keyOf(at));
const copy = (root = ROOT, at = folder) => join(place(at), "threads", root);
// The history's words for a copy that is not whole (surogates/sandbox/local_history.py), which are a person's.
const WORDS = "refused the request: this thread has no whole copy, and its next open makes one";
// Its refusal, as this computer's check passes it on: known by its code, under words that are not the history's.
const NOT_WHOLE: Outcome = { error: { type: "history", code: "no_whole_copy", message: "said some other way, as its words may come to be" } };
const NOT_OWN: Outcome = {
  error: { type: "unavailable", message: "This computer could not make this thread's copy of its folder: what its sandbox left at its path is not a folder of the app's own" },
};
// Another folder at the folder's path: the one that was there is moved away, as its user would.
const replace = () => {
  renameSync(folder, `${folder}.old`);
  mkdirSync(folder);
  writeFileSync(join(folder, "new.txt"), "the new folder's\n");
};
const setAside = () => readdirSync(join(dataDir, "history")).filter((name) => name.includes(".was-") && !name.endsWith(".json"));
// The next look at *path* waits until it is let go: before it is made, or once it has *seen* what is there.
function hold(path: string, seen: boolean): { reached: Promise<void>; release(): void } {
  let release = () => {};
  let reached = () => {};
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  const reaching = new Promise<void>((resolve) => {
    reached = resolve;
  });
  looks.held = { path, seen, reached, gate };
  return { reached: reaching, release };
}

// A guest whose history makes a copy's folder as the real one does; *answers* says otherwise for a request.
function copies(
  answers: (request: HistoryRequest, signal: AbortSignal) => Outcome | undefined | Promise<Outcome | undefined> = () => undefined,
  options: Partial<CopiesOptions> = {},
): Copies {
  const vm = {
    history: async (request: HistoryRequest, aborted: AbortSignal): Promise<Outcome> => {
      asked.push(request);
      events.push(`${request.thread}:${request.action}`);
      const said = await answers(request, aborted);
      if (said !== undefined) return said;
      const at = join(request.place.history, "threads", request.thread);
      if (request.action === "open") {
        const there = existsSync(at);
        mkdirSync(at, { recursive: true });
        return { ok: { copy: there ? "kept" : "made" } };
      }
      return HASH;
    },
    unplace: async (given: Place) => {
      unplaced.push(given);
      events.push(`unplace:${given.real.ino}`);
      await letting;
      return true;
    },
  };
  const one = new Copies({ dataDir, user: USER, vm, idleMs: 60_000, replaced: (root) => void replaced.push(root), ...options });
  made.push(one);
  return one;
}

beforeEach(() => {
  looks.held = null;
  dir = realpathSync(mkdtempSync(join(tmpdir(), "copies-")));
  dataDir = join(dir, "data");
  folder = join(dir, "Reports");
  mkdirSync(folder);
  asked = [];
  unplaced = [];
  events = [];
  replaced = [];
  letting = Promise.resolve();
  made = [];
});

afterEach(() => {
  for (const one of made) one.stop();
  rmSync(dir, { recursive: true, force: true });
});

describe("a thread's copy of its folder", () => {
  it("is made at its root's first use, in the folder's place in the app's data, and is where its root then works", async () => {
    const opened = await copies().open(ROOT, bound(), signal());
    expect(asked).toEqual([{
      place: { key: keyOf(folder), history: place(), real: { path: folder, ...identity(folder), boot: BOOT_ID } },
      thread: ROOT, user: USER, action: "open", args: { moves: false },
    }]);
    expect(opened).toEqual({
      copy: { place: asked[0]?.place, folder: { path: copy(), ...identity(copy()), boot: BOOT_ID }, at: folder },
    });
  });

  it("asks the guest nothing for a copy it has opened, and opens one once for operations that come together", async () => {
    const one = copies();
    const [first, second] = await Promise.all([one.open(ROOT, bound(), signal()), one.open(ROOT, bound(), signal())]);
    expect(first).toEqual(second);
    expect(await one.ask(ROOT, bound(), "snapshot", STEP, signal())).toEqual(HASH);
    expect(await one.open(ROOT, bound(), signal())).toEqual(first);
    expect(asked.map((request) => request.action)).toEqual(["open", "snapshot"]);
    // Another thread on the folder has a copy of its own, in the same place; another folder has a place of its own.
    const elsewhere = join(dir, "Elsewhere");
    mkdirSync(elsewhere);
    expect(await one.open(OTHER, bound(OTHER), signal())).toMatchObject({ copy: { folder: { path: copy(OTHER) }, at: folder } });
    expect(await one.open(THIRD, bound(THIRD, elsewhere), signal())).toMatchObject({ copy: { folder: { path: copy(THIRD, elsewhere) }, at: elsewhere } });
    expect(asked.map((request) => [request.thread, request.action, request.place.key])).toEqual([
      [ROOT, "open", keyOf(folder)], [ROOT, "snapshot", keyOf(folder)], [OTHER, "open", keyOf(folder)], [THIRD, "open", keyOf(elsewhere)],
    ]);
  });

  it("has the guest open a copy that is there when the app starts: only the guest can tell a whole copy from one cut short", async () => {
    const before = await copies().open(ROOT, bound(), signal());
    // The app, started again: the folder at the copy's path may be half of one.
    const one = copies();
    expect(await one.open(ROOT, bound(), signal())).toEqual(before);
    expect(asked.map((request) => [request.action, request.args])).toEqual([["open", { moves: false }], ["open", { moves: false }]]);
    expect(await one.open(ROOT, bound(), signal())).toEqual(before);
    expect(asked).toHaveLength(2);
  });

  it("is the folder the guest left at its path at its last open, and is opened again once another is there", async () => {
    // A guest that makes a copy again at each open: a repository it found cut short, or redirected.
    let held = 0;
    const one = copies((request) => {
      if (request.action !== "open") return undefined;
      const at = join(request.place.history, "threads", request.thread);
      rmSync(at, { recursive: true, force: true });
      // Made first, so the copy's new folder is another than the one removed.
      mkdirSync(join(dir, `held-${(held += 1)}`));
      mkdirSync(at, { recursive: true });
      return { ok: { copy: "made" } };
    });
    const first = await one.open(ROOT, bound(), signal());
    expect(replaced).toEqual([]);
    // A turn's own open: the copy is the folder there after it, not the one before, and the host on that one is told.
    expect(await one.ask(ROOT, bound(), "open", {}, signal())).toEqual({ ok: { copy: "made" } });
    expect(replaced).toEqual([ROOT]);
    // That host goes; the next one starts on the copy there now, with no request.
    one.close(ROOT);
    const second = await one.open(ROOT, bound(), signal());
    expect(second).toEqual({ copy: { place: asked[0]?.place, folder: { path: copy(), ...identity(copy()), boot: BOOT_ID }, at: folder } });
    expect(second).not.toEqual(first);
    expect(asked).toHaveLength(2);
    // A thread with no host on its copy has none to tell.
    expect(await one.ask(OTHER, bound(OTHER), "open", {}, signal())).toEqual({ ok: { copy: "made" } });
    expect(await one.ask(OTHER, bound(OTHER), "open", {}, signal())).toEqual({ ok: { copy: "made" } });
    expect(replaced).toEqual([ROOT]);
    asked.splice(0);
    // Replaced with no open, as nothing of the app's does: the app knows no such folder, asks, and tells the host on the one before.
    rmSync(copy(), { recursive: true });
    mkdirSync(join(dir, "held-again"));
    mkdirSync(copy());
    expect(await one.open(ROOT, bound(), signal())).toMatchObject({ copy: { folder: identity(copy()) } });
    expect(asked.map((request) => [request.action, request.args])).toEqual([["open", { moves: false }]]);
    expect(replaced).toEqual([ROOT, ROOT]);
  });

  it("tells the host on a copy only of a copy other than the last one a host of its root was given", async () => {
    let again = false;
    let held = 0;
    const one = copies((request) => {
      if (request.action !== "open" || !again) return undefined;
      again = false;
      rmSync(copy(), { recursive: true, force: true });
      mkdirSync(join(dir, `held-${(held += 1)}`));
      mkdirSync(copy());
      return { ok: { copy: "made" } };
    });
    await one.open(ROOT, bound(), signal());
    again = true;
    await one.ask(ROOT, bound(), "open", {}, signal());
    expect(replaced).toEqual([ROOT]);
    // The next host starts on the copy made again before the one before it has gone.
    await one.open(ROOT, bound(), signal());
    one.close(ROOT);
    expect(await one.ask(ROOT, bound(), "open", {}, signal())).toEqual({ ok: { copy: "kept" } });
    expect(replaced).toEqual([ROOT]);
  });

  it.each([
    ["cap", /more files than a project's history on this computer takes.*Choose a folder inside it/],
    ["names", /a file whose name a project's history on this computer cannot record.*Choose a folder inside it/],
  ] as const)("is made nowhere for a folder with no history (%s): its thread works nowhere, is told why, and is asked about again", async (reason, words) => {
    // However few files it holds: the guest's word can only deny a thread its copy.
    const one = copies((request) => (request.action === "open" ? { ok: { history: "off", reason } } : undefined));
    expect(await one.open(ROOT, bound(), signal())).toEqual({ failed: historyOff(reason) });
    const off = historyOff(reason);
    expect("error" in off && off.error.type).toBe("history_off");
    expect("error" in off && off.error.message).toMatch(words);
    // A turn's own open is answered as the history said it; every other operation is told why.
    expect(await one.ask(ROOT, bound(), "open", {}, signal())).toEqual({ ok: { history: "off", reason } });
    expect(await one.ask(ROOT, bound(), "snapshot", STEP, signal())).toEqual(historyOff(reason));
    // Nothing is remembered of it: the folder may change.
    expect(await one.open(ROOT, bound(), signal())).toEqual({ failed: historyOff(reason) });
    expect(asked.map((request) => [request.action, request.args])).toEqual([
      ["open", { moves: false }], ["open", {}], ["open", { moves: false }], ["open", { moves: false }],
    ]);
    expect(existsSync(copy())).toBe(false);
  });

  it.each([
    // A link to the user's folder, as a guest that is not ours could leave it: a host on it would write the folder itself.
    ["a link to the user's folder", (at: string) => symlinkSync(folder, at)],
    ["a link to a folder beside it", (at: string) => {
      mkdirSync(`${at}-real`, { recursive: true });
      symlinkSync(`${at}-real`, at);
    }],
    ["a file", (at: string) => writeFileSync(at, "")],
    ["nothing", () => {}],
    // A folder, with a link on its way: the folder of copies led elsewhere.
    ["a folder reached through a link", (at: string) => {
      const elsewhere = join(dir, "elsewhere");
      mkdirSync(join(elsewhere, ROOT), { recursive: true });
      rmSync(join(at, ".."), { recursive: true, force: true });
      symlinkSync(elsewhere, join(at, ".."));
    }],
  ])("is refused where the guest left %s at the copy's path, and opened again by the next operation", async (_what, leave) => {
    const one = copies((request) => {
      if (request.action !== "open") return undefined;
      const at = join(request.place.history, "threads", request.thread);
      mkdirSync(join(at, ".."), { recursive: true });
      rmSync(at, { recursive: true, force: true });
      leave(at);
      return { ok: { copy: "made" } };
    });
    expect(await one.open(ROOT, bound(), signal())).toEqual({ failed: NOT_OWN });
    // A turn's own open is answered the same: what the guest says it made is taken only as it is found.
    expect(await one.ask(ROOT, bound(), "open", {}, signal())).toEqual(NOT_OWN);
    expect(await one.ask(ROOT, bound(), "snapshot", STEP, signal())).toEqual(NOT_OWN);
    expect(asked.map((request) => request.action)).toEqual(["open", "open", "open"]);
  });

  it("tells the host on a copy when the guest left at its path what is no copy", async () => {
    let leave = false;
    const one = copies((request) => {
      if (request.action !== "open" || !leave) return undefined;
      rmSync(copy(), { recursive: true, force: true });
      symlinkSync(folder, copy());
      return { ok: { copy: "made" } };
    });
    await one.open(ROOT, bound(), signal());
    leave = true;
    expect(await one.ask(ROOT, bound(), "open", {}, signal())).toEqual(NOT_OWN);
    expect(replaced).toEqual([ROOT]);
  });

  it("is refused where the guest left another thread's copy at its path", async () => {
    const one = copies((request) => {
      if (request.action !== "open" || request.thread !== ROOT) return undefined;
      // The other thread's copy, moved to this thread's name.
      renameSync(copy(OTHER), copy(ROOT));
      return { ok: { copy: "made" } };
    });
    expect(await one.open(OTHER, bound(OTHER), signal())).toHaveProperty("copy");
    expect(await one.open(ROOT, bound(), signal())).toEqual({ failed: NOT_OWN });
  });

  it("is in the app's data by its real path, where the data is reached through a link", async () => {
    mkdirSync(dataDir);
    const linked = join(dir, "linked");
    symlinkSync(dataDir, linked);
    dataDir = linked;
    const opened = await copies().open(ROOT, bound(), signal());
    const real = join(dir, "data", "history", keyOf(folder));
    expect(opened).toMatchObject({ copy: { place: { history: real }, folder: { path: join(real, "threads", ROOT) } } });
    expect(asked.map((request) => request.place.history)).toEqual([real]);
  });

  it("answers why not when the guest cannot make it, as the guest said it, and asks again for the next operation", async () => {
    let fail: Outcome | null = { error: { type: "unavailable", message: "This computer's sandbox did not start: no KVM here" } };
    const one = copies((request) => (request.action === "open" && fail ? fail : undefined));
    expect(await one.open(ROOT, bound(), signal())).toEqual({ failed: fail });
    // A refusal of the history's keeps its code, for whoever routes it.
    fail = { error: { type: "history", code: "history_refused", message: "refused the project's history: something in it is neither a file nor a folder" } };
    expect(await one.ask(ROOT, bound(), "snapshot", STEP, signal())).toEqual(fail);
    fail = null;
    expect(await one.open(ROOT, bound(), signal())).toMatchObject({ copy: { folder: { path: copy() } } });
    expect(asked.map((request) => request.action)).toEqual(["open", "open", "open"]);
  });

  it("is made for no thread whose folder is no longer the one at its path, and the guest is asked nothing", async () => {
    const was = bound();
    replace();
    const one = copies();
    expect(await one.open(ROOT, was, signal())).toEqual({ failed: FOLDER_UNAVAILABLE });
    expect(await one.ask(ROOT, was, "open", {}, signal())).toEqual(FOLDER_UNAVAILABLE);
    expect(await one.ask(ROOT, was, "snapshot", STEP, signal())).toEqual(FOLDER_UNAVAILABLE);
    expect(asked).toEqual([]);
  });

  it("is made for no root bound to another thread's copy", async () => {
    const one = copies();
    const refused = {
      error: { type: "unavailable", message: "This computer could not make this thread's copy of its folder: this chat is bound to another thread's copy" },
    };
    expect(await one.open(ROOT, bound(OTHER), signal())).toEqual({ failed: refused });
    expect(await one.ask(ROOT, bound(OTHER), "snapshot", STEP, signal())).toEqual(refused);
    expect(await one.ask(ROOT, { ...bound(), history: undefined }, "open", {}, signal())).toEqual(refused);
    expect(asked).toEqual([]);
  });

  it("is not cut short in its making by a cancel of the operation that asked for it, and a cancelled open holds nothing", async () => {
    let finish: (outcome: Outcome | undefined) => void = () => {};
    let stopped = false;
    const one = copies((request, aborted) => {
      if (request.action !== "open") return undefined;
      aborted.addEventListener("abort", () => {
        stopped = true;
      });
      return new Promise<Outcome | undefined>((resolve) => {
        finish = resolve;
      });
    }, { idleMs: 100 });
    const cancel = new AbortController();
    const cancelled = one.open(ROOT, bound(), cancel.signal);
    await vi.waitFor(() => expect(asked).toHaveLength(1));
    const next = one.open(ROOT, bound(), signal());
    cancel.abort();
    expect(await cancelled).toEqual({ failed: CANCELLED });
    expect(stopped).toBe(false);
    finish(undefined);
    expect(await next).toMatchObject({ copy: { folder: { path: copy() } } });
    expect(asked).toHaveLength(1);
    // Held by the open that was answered alone: once it lets go, the place is let go.
    await pause(200);
    expect(unplaced).toEqual([]);
    one.close(ROOT);
    await vi.waitFor(() => expect(unplaced).toHaveLength(1));
  });
});

describe("a request to the history of a thread's folder", () => {
  it("is passed to the guest for the thread's own copy, which is made first for a request that works in it", async () => {
    const one = copies();
    expect(await one.ask(ROOT, bound(), "snapshot", STEP, signal())).toEqual(HASH);
    expect(asked.map((request) => [request.thread, request.action, request.args])).toEqual([
      [ROOT, "open", { moves: false }], [ROOT, "snapshot", STEP],
    ]);
    // A turn's start is the history's own open: asked as it came, its answer passed on as it came, and a copy
    // that is there is asked nothing before it.
    const named: Outcome = { ok: { copy: "kept", set_asides: ["b".repeat(40)] } };
    const turn = copies((request) => (request.action === "open" && request.args.moves === undefined ? named : undefined));
    expect(await turn.ask(ROOT, bound(), "open", {}, signal())).toEqual(named);
    expect(asked.slice(2).map((request) => [request.action, request.args])).toEqual([["open", {}]]);
  });

  it("is asked again, its copy made again first, where the history refuses it for want of a whole copy, and refused in words after", async () => {
    // As after a copy's making was cut short, or its repository was made again.
    let refusals = 1;
    const one = copies((request) => (request.action === "snapshot" && (refusals -= 1) >= 0 ? NOT_WHOLE : undefined));
    await one.open(ROOT, bound(), signal());
    expect(await one.ask(ROOT, bound(), "snapshot", STEP, signal())).toEqual(HASH);
    // Inside a turn, the open that makes it again does not move it.
    expect(asked.map((request) => [request.action, request.args])).toEqual([
      ["open", { moves: false }], ["snapshot", STEP], ["open", { moves: false }], ["snapshot", STEP],
    ]);
    // Refused again after its open: asked twice, and no more, and answered by its code, in words of what was done.
    refusals = 2;
    expect(await one.ask(ROOT, bound(), "snapshot", STEP, signal())).toEqual({
      error: {
        type: "history", code: "no_whole_copy",
        message: "This computer made this thread's copy of its folder again, and its history still found it not whole, so this was not done: said some other way, as its words may come to be",
      },
    });
    expect(asked.slice(4).map((request) => request.action)).toEqual(["snapshot", "open", "snapshot"]);
    // Nothing works in it meanwhile: the next operation has the guest open it first.
    await one.open(ROOT, bound(), signal());
    expect(asked.at(-1)).toMatchObject({ action: "open", args: { moves: false } });
  });

  it.each([
    // The history's own words for a copy that is not whole, under every code that is not that refusal's.
    { type: "history", code: "failed", message: WORDS }, { type: "history", code: "history_refused", message: WORDS },
    { type: "history", code: "name_not_utf8", message: WORDS }, { type: "history", code: "not_a_request", message: WORDS },
    { type: "history", code: "no_answer", message: WORDS }, { type: "history", code: "not_an_answer", message: WORDS },
    // Under no code at all, and as an error that is no history's.
    { type: "history", message: WORDS }, { type: "unavailable", code: "no_whole_copy", message: WORDS },
  ])("goes by a refusal's code, never by its words: %j is answered as it is, and nothing is asked again", async (error) => {
    const one = copies((request) => (request.action === "snapshot" ? { error } : undefined));
    await one.open(ROOT, bound(), signal());
    expect(await one.ask(ROOT, bound(), "snapshot", STEP, signal())).toEqual({ error });
    expect(asked.map((request) => request.action)).toEqual(["open", "snapshot"]);
  });

  it("answers a cancel at once, and tells the guest, which stops its git", async () => {
    let stopped = false;
    const one = copies((request, aborted) => {
      if (request.action !== "commit") return undefined;
      return new Promise<Outcome>((resolve) => aborted.addEventListener("abort", () => {
        stopped = true;
        resolve(CANCELLED);
      }));
    });
    await one.open(ROOT, bound(), signal());
    const cancel = new AbortController();
    const waiting = one.ask(ROOT, bound(), "commit", {}, cancel.signal);
    await vi.waitFor(() => expect(asked.at(-1)?.action).toBe("commit"));
    cancel.abort();
    expect(await waiting).toEqual(CANCELLED);
    expect(stopped).toBe(true);
  });

  it.each<[string, Outcome]>([
    ["cancelled", CANCELLED],
    ["stopped by the sandbox", SANDBOX_STOPPED],
    ["let go before it was answered", LET_GO],
    ["one the history did not answer", { error: { type: "history", code: "no_answer", message: "The folder's history ended without an answer" } }],
    ["one git failed in", { error: { type: "history", code: "failed", message: "git read-tree failed" } }],
  ])("has the guest open the copy again before anything works in it, after a request %s", async (_what, cut) => {
    const one = copies((request) => (request.action === "record" ? cut : undefined));
    await one.open(ROOT, bound(), signal());
    expect(await one.ask(ROOT, bound(), "record", {}, signal())).toEqual(cut);
    // A request cut in the middle may have left the copy half made other: its next open finishes what was cut.
    expect(await one.open(ROOT, bound(), signal())).toHaveProperty("copy");
    expect(asked.map((request) => [request.action, request.args])).toEqual([["open", { moves: false }], ["record", {}], ["open", { moves: false }]]);
  });
});

describe("a folder's place in the guest", () => {
  it("is let go once no thread's copy on the folder is open and no request runs, by the place it gave, and not before", async () => {
    let finish: () => void = () => {};
    const one = copies((request) => {
      if (request.action !== "commit") return undefined;
      return new Promise<Outcome>((resolve) => {
        finish = () => resolve({ ok: {} });
      });
    }, { idleMs: 40 });
    const first = await one.open(ROOT, bound(), signal());
    // A thread bound in an earlier boot, when the folder's device had another number: the same folder, and its place.
    const earlier = { ...bound(OTHER), dev: identity(folder).dev + 1, boot: "an earlier boot" };
    await one.open(OTHER, earlier, signal());
    one.close(ROOT);
    await pause(120);
    // The other thread's copy is open still.
    expect(unplaced).toEqual([]);
    const committing = one.ask(OTHER, earlier, "commit", {}, signal());
    await vi.waitFor(() => expect(asked.at(-1)?.action).toBe("commit"));
    one.close(OTHER);
    await pause(120);
    // A request runs.
    expect(unplaced).toEqual([]);
    finish();
    await committing;
    await vi.waitFor(() => expect(unplaced).toHaveLength(1));
    expect(unplaced).toEqual(["copy" in first ? first.copy.place : null]);
    // A request that comes later has the place added again by the manager, and it is let go again after.
    await one.ask(OTHER, earlier, "changed", {}, signal());
    await vi.waitFor(() => expect(unplaced).toHaveLength(2));
    expect(unplaced[1]).toEqual(asked.at(-1)?.place);
    expect(unplaced[1]?.real.boot).toBe("an earlier boot");
  });

  it("is asked nothing while the guest is still letting it go: a request that comes meanwhile waits, so the guest never adds what it is taking away", async () => {
    let gone: () => void = () => {};
    letting = new Promise<void>((resolve) => {
      gone = resolve;
    });
    const one = copies(undefined, { idleMs: 20 });
    await one.open(ROOT, bound(), signal());
    one.close(ROOT);
    await vi.waitFor(() => expect(unplaced).toHaveLength(1));
    const before = asked.length;
    const waiting = one.ask(ROOT, bound(), "changed", {}, signal());
    // Long enough for a request that did not wait to have been asked.
    await pause(60);
    expect(asked).toHaveLength(before);
    gone();
    expect(await waiting).toHaveProperty("ok");
    expect(asked.slice(before).map((request) => request.action)).toEqual(["changed"]);
  });

  it.each<[string, Outcome]>([
    ["cancelled", CANCELLED],
    ["stopped by the sandbox, its manager dead", SANDBOX_STOPPED],
    ["let go before it was answered", LET_GO],
    ["answered that the sandbox did not start", { error: { type: "unavailable", message: "This computer's sandbox did not start: its manager exited" } }],
  ])("is held by nothing after a request %s: it is let go once idle, and the next request adds it again", async (_what, cut) => {
    let cutting = true;
    const one = copies((request) => (request.action === "pickup" && cutting ? cut : undefined), { idleMs: 50 });
    expect(await one.ask(ROOT, bound(), "pickup", {}, signal())).toEqual(cut);
    await vi.waitFor(() => expect(unplaced).toHaveLength(1));
    cutting = false;
    expect(await one.ask(ROOT, bound(), "pickup", {}, signal())).toEqual(HASH);
    await vi.waitFor(() => expect(unplaced).toHaveLength(2));
  });

  it("is let go no more once the app stops, and a copy's making under way is told to stop", async () => {
    let stopped = false;
    const one = copies((request, aborted) => {
      if (request.thread !== THIRD) return undefined;
      return new Promise<Outcome>((resolve) => aborted.addEventListener("abort", () => {
        stopped = true;
        resolve(CANCELLED);
      }));
    }, { idleMs: 20 });
    await one.open(ROOT, bound(), signal());
    await one.open(OTHER, bound(OTHER), signal());
    const making = one.open(THIRD, bound(THIRD), signal());
    await vi.waitFor(() => expect(asked.at(-1)?.thread).toBe(THIRD));
    one.close(ROOT);
    one.stop();
    one.close(OTHER);
    expect(await making).toEqual({ failed: CANCELLED });
    expect(stopped).toBe(true);
    await pause(80);
    expect(unplaced).toEqual([]);
  });

  it("is let go only once nothing has held it for the idle time, counted from the last that let go", async () => {
    const one = copies(undefined, { idleMs: 200 });
    // A request ends while the copy is open, and the copy is let go a while after.
    await one.open(ROOT, bound(), signal());
    await one.ask(ROOT, bound(), "snapshot", STEP, signal());
    await pause(140);
    one.close(ROOT);
    await pause(120);
    expect(unplaced).toEqual([]);
    await vi.waitFor(() => expect(unplaced).toHaveLength(1));
    // Let go, then a request soon after.
    await one.open(ROOT, bound(), signal());
    one.close(ROOT);
    await pause(120);
    await one.ask(ROOT, bound(), "snapshot", STEP, signal());
    await pause(120);
    expect(unplaced).toHaveLength(1);
    await vi.waitFor(() => expect(unplaced).toHaveLength(2));
  });

  it("is not let go while held again, though it was idle, once the guest has let it go before", async () => {
    let gone: () => void = () => {};
    letting = new Promise<void>((resolve) => {
      gone = resolve;
    });
    const one = copies(undefined, { idleMs: 20 });
    await one.open(ROOT, bound(), signal());
    one.close(ROOT);
    await vi.waitFor(() => expect(unplaced).toHaveLength(1));
    // While the guest lets it go, a host takes the copy and lets it go again, and then another takes it.
    await one.open(ROOT, bound(), signal());
    one.close(ROOT);
    await pause(60);
    await one.open(ROOT, bound(), signal());
    gone();
    await pause(60);
    expect(unplaced).toHaveLength(1);
    one.close(ROOT);
    await vi.waitFor(() => expect(unplaced).toHaveLength(2));
  });

  it("is held for a copy until every host that took it has let it go", async () => {
    const one = copies(undefined, { idleMs: 20 });
    await one.open(ROOT, bound(), signal());
    await one.open(ROOT, bound(), signal());
    one.close(ROOT);
    await pause(60);
    expect(unplaced).toEqual([]);
    one.close(ROOT);
    await vi.waitFor(() => expect(unplaced).toHaveLength(1));
    // A close more than there were opens lets go of nothing more.
    one.close(ROOT);
    await one.open(ROOT, bound(), signal());
    await pause(60);
    expect(unplaced).toHaveLength(1);
  });

  it("is each folder's own, held and let go apart from another folder's", async () => {
    const elsewhere = join(dir, "Elsewhere");
    mkdirSync(elsewhere);
    const one = copies(undefined, { idleMs: 20 });
    await one.open(ROOT, bound(), signal());
    await one.open(THIRD, bound(THIRD, elsewhere), signal());
    one.close(THIRD);
    await vi.waitFor(() => expect(unplaced.map((given) => given.key)).toEqual([keyOf(elsewhere)]));
    one.close(ROOT);
    await vi.waitFor(() => expect(unplaced.map((given) => given.key)).toEqual([keyOf(elsewhere), keyOf(folder)]));
  });
});

describe("a place another folder at the path takes", () => {
  it("is let go by the place it gave, and waited for, before the new folder's place is asked for: each host on its copies lets go first", async () => {
    const was = bound();
    const one = copies();
    const old = await one.open(ROOT, was, signal());
    if (!("copy" in old)) throw new Error("no copy");
    replace();
    const opening = one.open(OTHER, bound(OTHER), signal());
    await vi.waitFor(() => expect(replaced).toEqual([ROOT]));
    // Nothing is let go, renamed or asked for while the old folder's host holds its copy.
    await pause(40);
    expect([unplaced, setAside(), asked.filter((request) => request.thread === OTHER)]).toEqual([[], [], []]);
    one.close(ROOT);
    const opened = await opening;
    expect(unplaced).toEqual([old.copy.place]);
    expect(events).toEqual([`${ROOT}:open`, `unplace:${was.ino}`, `${OTHER}:open`]);
    expect(setAside()).toHaveLength(1);
    expect(opened).toMatchObject({ copy: { place: { key: keyOf(folder), real: identity(folder) }, folder: { path: copy(OTHER) }, at: folder } });
    expect(asked.at(-1)?.place.real).toMatchObject(identity(folder));
    // The old folder's thread works nowhere now, and the guest is asked nothing for it.
    expect(await one.open(ROOT, was, signal())).toEqual({ failed: FOLDER_UNAVAILABLE });
    expect(await one.ask(ROOT, was, "snapshot", STEP, signal())).toEqual(FOLDER_UNAVAILABLE);
    expect(asked).toHaveLength(2);
  });

  it("waits no longer than its hosts take: one that lets its copy go as it is told is not waited for", async () => {
    const one: Copies = copies(undefined, {
      replaced: (root) => {
        replaced.push(root);
        one.close(root);
      },
    });
    await one.open(ROOT, bound(), signal());
    replace();
    const begun = performance.now();
    expect(await one.open(OTHER, bound(OTHER), signal())).toHaveProperty("copy");
    expect(performance.now() - begun).toBeLessThan(1_000);
    expect(replaced).toEqual([ROOT]);
  });

  it("is the old folder's thread's no more: an operation of its that comes while the place is let go works nowhere, and asks the guest nothing", async () => {
    const was = bound();
    const one = copies();
    await one.open(ROOT, was, signal());
    replace();
    const opening = one.open(OTHER, bound(OTHER), signal());
    await vi.waitFor(() => expect(replaced).toEqual([ROOT]));
    // Found as its folder's place, by its record, before that is set aside: it waits, and is found again after.
    const stale = one.ask(ROOT, was, "snapshot", STEP, signal());
    await pause(20);
    one.close(ROOT);
    expect(await stale).toEqual(FOLDER_UNAVAILABLE);
    expect(await opening).toHaveProperty("copy");
    expect(events).toEqual([`${ROOT}:open`, `unplace:${was.ino}`, `${OTHER}:open`]);
    expect(existsSync(copy(ROOT))).toBe(false);
  });

  it("is the old folder's thread's no more where it was found before it was let go, and looked at after", async () => {
    const was = bound();
    const one = copies();
    await one.open(ROOT, was, signal());
    replace();
    const opening = one.open(OTHER, bound(OTHER), signal());
    await vi.waitFor(() => expect(replaced).toEqual([ROOT]));
    // Found as its folder's place, by its record; its folder in the app's data looked at once it was set aside.
    const look = hold(place(), false);
    const stale = one.ask(ROOT, was, "snapshot", STEP, signal());
    await look.reached;
    one.close(ROOT);
    expect(await opening).toHaveProperty("copy");
    look.release();
    expect(await stale).toEqual(FOLDER_UNAVAILABLE);
    expect(events).toEqual([`${ROOT}:open`, `unplace:${was.ino}`, `${OTHER}:open`]);
  });

  it("gives no host a copy it saw before its place was let go", async () => {
    const was = bound();
    const one = copies();
    await one.open(ROOT, was, signal());
    replace();
    const opening = one.open(OTHER, bound(OTHER), signal());
    await vi.waitFor(() => expect(replaced).toEqual([ROOT]));
    // Another host of the old folder's thread starts: its copy is seen as it was, and the place goes meanwhile.
    const look = hold(copy(ROOT), true);
    const second = one.open(ROOT, was, signal());
    await look.reached;
    one.close(ROOT);
    expect(await opening).toHaveProperty("copy");
    look.release();
    expect(await second).toEqual({ failed: FOLDER_UNAVAILABLE });
  });

  it("tells a host that takes a copy of the place while it is let go, and waits for it too", async () => {
    const was = bound();
    const one = copies();
    await one.open(ROOT, was, signal());
    replace();
    const opening = one.open(OTHER, bound(OTHER), signal());
    await vi.waitFor(() => expect(replaced).toEqual([ROOT]));
    expect(await one.open(ROOT, was, signal())).toHaveProperty("copy");
    await vi.waitFor(() => expect(replaced).toEqual([ROOT, ROOT]));
    one.close(ROOT);
    await pause(40);
    expect(unplaced).toEqual([]);
    one.close(ROOT);
    expect(await opening).toHaveProperty("copy");
    expect(unplaced).toHaveLength(1);
  });

  it("is let go for another only once the guest has let it go at its idle time, and a request waiting for that waits for this too", async () => {
    let gone: () => void = () => {};
    letting = new Promise<void>((resolve) => {
      gone = resolve;
    });
    const was = bound();
    const one = copies(undefined, { idleMs: 20 });
    await one.open(ROOT, was, signal());
    one.close(ROOT);
    await vi.waitFor(() => expect(unplaced).toHaveLength(1));
    // A request of the old folder's thread waits for that letting go, and another folder comes to the path meanwhile.
    const stale = one.ask(ROOT, was, "snapshot", STEP, signal());
    await pause(40);
    replace();
    const opening = one.open(OTHER, bound(OTHER), signal());
    await pause(40);
    expect([setAside(), asked.filter((request) => request.thread === OTHER)]).toEqual([[], []]);
    gone();
    expect(await opening).toHaveProperty("copy");
    expect(setAside()).toHaveLength(1);
    expect(await stale).toEqual(FOLDER_UNAVAILABLE);
    expect(events).toEqual([`${ROOT}:open`, `unplace:${was.ino}`, `${OTHER}:open`]);
  });

  it("is let go by the place the app gave, whichever folder's place the sandbox is asked to let go", async () => {
    const one = copies(undefined, { idleMs: 60_000 });
    const old = await one.open(ROOT, bound(), signal());
    if (!("copy" in old)) throw new Error("no copy");
    one.close(ROOT);
    // A record that cannot be read: what is asked to be let go is named by the folder at the path, the new one.
    writeFileSync(join(dataDir, "history", `${keyOf(folder)}.json`), "{");
    replace();
    expect(await one.open(OTHER, bound(OTHER), signal())).toHaveProperty("copy");
    expect(unplaced).toEqual([old.copy.place]);
  });

  it("sets nothing aside while a host of the old folder does not let its copy go: at the bound the new folder's thread is told why", async () => {
    const one = copies(undefined, { closeMs: 50 });
    await one.open(ROOT, bound(), signal());
    replace();
    const refused = await one.open(OTHER, bound(OTHER), signal());
    expect(refused).toEqual({
      failed: {
        error: {
          type: "unavailable",
          message: "This computer could not make this thread's copy of its folder: a thread still works in the history of the folder that was at this path, and did not let it go within 0.05 s",
        },
      },
    });
    expect([replaced, unplaced, setAside(), asked.filter((request) => request.thread === OTHER)]).toEqual([[ROOT], [], [], []]);
    // The next operation tells it again.
    expect(await one.open(OTHER, bound(OTHER), signal())).toHaveProperty("failed");
    expect(replaced).toEqual([ROOT, ROOT]);
    // Once it has, the next operation sets it aside.
    one.close(ROOT);
    expect(await one.open(OTHER, bound(OTHER), signal())).toHaveProperty("copy");
    expect(setAside()).toHaveLength(1);
  });

  it("is let go, and what the app knew of its copies with it, once the place's folder is not the one it knew", async () => {
    const one = copies();
    const first = await one.open(ROOT, bound(), signal());
    if (!("copy" in first)) throw new Error("no copy");
    // The place's folder replaced under the app, as no act of its own does: its record still names the folder.
    renameSync(place(), `${place()}.moved`);
    mkdirSync(place(), { mode: 0o700 });
    const again = one.open(ROOT, bound(), signal());
    await vi.waitFor(() => expect(replaced).toEqual([ROOT]));
    one.close(ROOT);
    expect(await again).toMatchObject({ copy: { folder: { path: copy() } } });
    // The place the guest held is let go before the guest is asked of the new one, and the copy is opened again.
    expect(unplaced).toEqual([first.copy.place]);
    expect(events).toEqual([`${ROOT}:open`, `unplace:${first.copy.place.real.ino}`, `${ROOT}:open`]);
  });
});

describe("operations at once", () => {
  it("of different threads, and of different folders, wait on nothing of each other's: one thread's wait their turn", async () => {
    let finish: () => void = () => {};
    const one = copies((request) => {
      if (request.thread !== ROOT || request.action !== "commit") return undefined;
      return new Promise<Outcome>((resolve) => {
        finish = () => resolve({ ok: {} });
      });
    });
    await one.open(ROOT, bound(), signal());
    const committing = one.ask(ROOT, bound(), "commit", {}, signal());
    await vi.waitFor(() => expect(asked.at(-1)?.action).toBe("commit"));
    // Another thread of the folder, and a thread of another folder, are each answered meanwhile.
    const elsewhere = join(dir, "Elsewhere");
    mkdirSync(elsewhere);
    expect(await one.ask(OTHER, bound(OTHER), "snapshot", STEP, signal())).toEqual(HASH);
    expect(await one.open(THIRD, bound(THIRD, elsewhere), signal())).toHaveProperty("copy");
    // The thread's own next operation waits for its turn.
    let snapped = false;
    const next = one.ask(ROOT, bound(), "snapshot", STEP, signal()).then((outcome) => {
      snapped = true;
      return outcome;
    });
    await pause(40);
    expect(snapped).toBe(false);
    finish();
    expect(await committing).toEqual({ ok: {} });
    expect(await next).toEqual(HASH);
  });
});
