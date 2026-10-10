import { existsSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, renameSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createInterface } from "node:readline";
import { duplexPair } from "node:stream";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { CANCELLED, SANDBOX_STOPPED } from "../src/guest/command.js";
import { Control, type ControlPlaces } from "../src/guest/control.js";
import { type Asked, askHistory, Places } from "../src/guest/places.js";
import type { FromAgent, Share } from "../src/guest/protocol.js";
import type { Outcome } from "../src/link/protocol.js";
import { checked, forgettable, type HistoryRequest, named } from "../src/vm/history.js";
import { type BootVm, type Place, VmManager, type VmOptions } from "../src/vm/manager.js";

const KEY = "0123456789abcdef";
const OTHER_KEY = "fedcba9876543210";
const THREAD = "0b6c1d3e-6f0a-4c1e-9a52-6a1d2c3b4e5f";
const R1: Share = { kind: "virtiofs", tag: "r1" };
const R2: Share = { kind: "virtiofs", tag: "r2" };
const ID = "a".repeat(40);
const BLOB = "b".repeat(40);
const signal = () => new AbortController().signal;
const NO_ANSWER = { error: { type: "history", code: "no_answer", message: "This folder's history did not answer" } };
const NOT_HERE = { error: { type: "unavailable", message: "This folder's history is not in the sandbox" } };
const alive = (pid: number) => {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
};
async function until(check: () => boolean, ms = 10_000): Promise<void> {
  for (const end = Date.now() + ms; !check(); await new Promise((resolve) => setTimeout(resolve, 10))) {
    if (Date.now() > end) throw new Error("timed out");
  }
}

let dir: string;

beforeEach(() => {
  dir = realpathSync(mkdtempSync(join(tmpdir(), "history-")));
  for (const name of ["store", "Documents"]) mkdirSync(join(dir, name));
});

afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

describe("the agent, asked for a folder's history", () => {
  // Places whose history is *ask*: what it was given, and what it says.
  const places = (ask: (request: Asked, signal: AbortSignal) => Promise<string>, historyMs?: number) => new Places({
    folder: join(dir, "places"), mount: async () => {}, unmount: async () => {}, ask, historyMs,
  });
  const request = { thread: THREAD, user: "u1", action: "snapshot", args: { reason: "before a step" } };

  it("runs one request on the place as the agent mounts it, and answers what the history said", async () => {
    const asked: Asked[] = [];
    const mounted = places(async (given) => {
      asked.push(given);
      return JSON.stringify({ hash: ID });
    });
    await mounted.mount(KEY, R1, R2);
    expect(await mounted.history(KEY, request, signal())).toEqual({ ok: { hash: ID } });
    expect(asked).toEqual([{
      store: join(dir, "places", KEY, "history"), folder: join(dir, "places", KEY, "real"),
      thread: THREAD, user: "u1", action: "snapshot", args: { reason: "before a step" },
    }]);
  });

  it("answers a place that is not mounted, a history that refused, and one that said nothing it can read", async () => {
    const said: Record<string, string> = {
      open: JSON.stringify({ error: { code: "no_whole_copy", message: "x".repeat(5_000), planted: true } }),
      fetch: "Traceback",
      pickup: "[1]",
      // A refusal is a code and its words: words alone, or a code that is no text, is none, and never an answer.
      restore: JSON.stringify({ error: "git failed" }),
      keep: JSON.stringify({ error: { message: "words, and no code" } }),
      record: JSON.stringify({ error: { code: 7, message: "m" } }),
      changed: JSON.stringify({ paths: [], error: null }),
      forget: "",
    };
    const mounted = places(async ({ action }) => said[action] ?? "{}");
    expect(await mounted.history(KEY, request, signal())).toEqual(NOT_HERE);
    await mounted.mount(KEY, R1, R2);
    // Another place's key is no way to this one's history, nor is a path that leads to it.
    for (const key of [OTHER_KEY, `${OTHER_KEY}/../${KEY}`, `../places/${KEY}`, `${KEY}/`, ""]) expect(await mounted.history(key, request, signal())).toEqual(NOT_HERE);
    const refused = await mounted.history(KEY, { ...request, action: "open" }, signal());
    // The history's code for why not, and its own words, as text: no more of them than a message holds.
    expect(refused).toEqual({ error: { type: "history", code: "no_whole_copy", message: "x".repeat(2_000) } });
    for (const action of ["fetch", "pickup", "restore", "keep", "record", "changed", "forget"]) {
      expect(await mounted.history(KEY, { ...request, action }, signal())).toEqual(NO_ANSWER);
    }
  });

  it("runs one request at a time on a place, and two places' at once", async () => {
    const order: string[] = [];
    const release: Array<() => void> = [];
    const mounted = places(({ store, args }) => new Promise<string>((resolve) => {
      order.push(`start ${store.includes(KEY) ? "a" : "b"}${String(args.n)}`);
      release.push(() => {
        order.push(`end ${store.includes(KEY) ? "a" : "b"}${String(args.n)}`);
        resolve("{}");
      });
    }));
    await mounted.mount(KEY, R1, R2);
    await mounted.mount(OTHER_KEY, { kind: "virtiofs", tag: "r3" }, { kind: "virtiofs", tag: "r4" });
    const all = Promise.all([
      mounted.history(KEY, { ...request, args: { n: 1 } }, signal()), mounted.history(KEY, { ...request, args: { n: 2 } }, signal()),
      mounted.history(OTHER_KEY, { ...request, args: { n: 1 } }, signal()),
    ]);
    await new Promise((resolve) => setTimeout(resolve, 20));
    // The second on the same place waits; the other place's runs beside the first.
    expect(order).toEqual(["start a1", "start b1"]);
    release[0]!();
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(order).toEqual(["start a1", "start b1", "end a1", "start a2"]);
    release[1]!();
    release[2]!();
    expect(await all).toEqual([{ ok: {} }, { ok: {} }, { ok: {} }]);
  });

  // A history that ends only when it is told to, 30 ms after: what it was asked, and when each of its runs began and ended.
  const slow = (order: string[]) => ({ args }: Asked, stop: AbortSignal) => new Promise<string>((resolve, reject) => {
    order.push(`start ${String(args.n)}`);
    if (args.n !== 1) return resolve("{}");
    stop.addEventListener("abort", () => setTimeout(() => {
      order.push("ended 1");
      reject(new Error("killed"));
    }, 30), { once: true });
  });

  it("answers a cancel at once, and ends the request it stopped before the place's next one starts", async () => {
    const order: string[] = [];
    const mounted = places(slow(order));
    await mounted.mount(KEY, R1, R2);
    const cancel = new AbortController();
    const first = mounted.history(KEY, { ...request, args: { n: 1 } }, cancel.signal);
    const second = mounted.history(KEY, { ...request, args: { n: 2 } }, signal());
    await new Promise((resolve) => setTimeout(resolve, 10));
    cancel.abort();
    expect(await first).toEqual(CANCELLED);
    expect(order).toEqual(["start 1"]);
    expect(await second).toEqual({ ok: {} });
    expect(order).toEqual(["start 1", "ended 1", "start 2"]);
  });

  it("starts nothing for a request cancelled while it waits its turn, or before it is asked", async () => {
    const order: string[] = [];
    const mounted = places(slow(order));
    await mounted.mount(KEY, R1, R2);
    const holding = new AbortController();
    const waiting = new AbortController();
    const first = mounted.history(KEY, { ...request, args: { n: 1 } }, holding.signal);
    const second = mounted.history(KEY, { ...request, args: { n: 2 } }, waiting.signal);
    waiting.abort();
    expect(await second).toEqual(CANCELLED);
    expect(await mounted.history(KEY, { ...request, args: { n: 3 } }, waiting.signal)).toEqual(CANCELLED);
    holding.abort();
    expect(await first).toEqual(CANCELLED);
    expect(await mounted.history(KEY, { ...request, args: { n: 4 } }, signal())).toEqual({ ok: {} });
    expect(order).toEqual(["start 1", "ended 1", "start 4"]);
  });

  it("ends a request at its bound, which is its own from when it starts, and answers that the history did not", async () => {
    const order: string[] = [];
    const mounted = places(slow(order), 80);
    await mounted.mount(KEY, R1, R2);
    const began = performance.now();
    const first = mounted.history(KEY, { ...request, args: { n: 1 } }, signal());
    const second = mounted.history(KEY, { ...request, args: { n: 2 } }, signal());
    expect(await first).toEqual(NO_ANSWER);
    expect(performance.now() - began).toBeGreaterThanOrEqual(75);
    // The one that waited behind it has a bound of its own, and starts once the first has ended.
    expect(await second).toEqual({ ok: {} });
    expect(order).toEqual(["start 1", "ended 1", "start 2"]);
  });

  it("ends a place's requests before it lets the place's mounts go, and runs none of them on the mounts that come after", async () => {
    const order: string[] = [];
    const mounted = new Places({
      folder: join(dir, "places"), ask: slow(order),
      mount: async (args) => void order.push(`mount ${args.at(-2)}`),
      unmount: async (args) => void order.push(`umount ${args.at(-1)!.split("/").at(-1)}`),
    });
    await mounted.mount(KEY, R1, R2);
    const first = mounted.history(KEY, { ...request, args: { n: 1 } }, signal());
    const second = mounted.history(KEY, { ...request, args: { n: 2 } }, signal());
    const gone = mounted.unmount(KEY);
    const again = mounted.mount(KEY, { kind: "virtiofs", tag: "r3" }, { kind: "virtiofs", tag: "r4" });
    // The one that ran did not answer; the one that waited never ran, on these mounts or the next.
    expect(await first).toEqual(NO_ANSWER);
    expect(await second).toEqual(NOT_HERE);
    await gone;
    await again;
    expect(order).toEqual(["mount r1", "mount r2", "start 1", "ended 1", "umount real", "umount history", "mount r3", "mount r4"]);
    expect(await mounted.history(KEY, { ...request, args: { n: 3 } }, signal())).toEqual({ ok: {} });
  });

  it("ends every request at the guest's stop, within its bound, and starts none after", async () => {
    const order: string[] = [];
    const mounted = places(slow(order));
    await mounted.mount(KEY, R1, R2);
    await mounted.mount(OTHER_KEY, { kind: "virtiofs", tag: "r3" }, { kind: "virtiofs", tag: "r4" });
    const all = [
      mounted.history(KEY, { ...request, args: { n: 1 } }, signal()), mounted.history(KEY, { ...request, args: { n: 2 } }, signal()),
      mounted.history(OTHER_KEY, { ...request, args: { n: 1 } }, signal()),
    ];
    await until(() => order.length === 2);
    await mounted.stop();
    expect(order).toEqual(["start 1", "start 1", "ended 1", "ended 1"]);
    // Each as the host answers a guest that went, whichever of the two answers reaches whoever asked.
    expect(await Promise.all(all)).toEqual([SANDBOX_STOPPED, SANDBOX_STOPPED, SANDBOX_STOPPED]);
    expect(await mounted.history(KEY, { ...request, args: { n: 3 } }, signal())).toEqual(SANDBOX_STOPPED);
    expect(order).toHaveLength(4);
    // A request that cannot end keeps the stop no longer than its bound.
    const stuck = places(() => new Promise<string>(() => {}));
    await stuck.mount(KEY, R1, R2);
    void stuck.history(KEY, request, signal());
    const began = performance.now();
    await stuck.stop(50);
    expect(performance.now() - began).toBeLessThan(2_000);
  });
});

// In the history's place: a program that reads its request as the history does, starts a writer of its own in the
// place, as a git it runs, and goes on as its request's *then* says. The writer writes for as long as it lives, and
// neither it nor a history that waits outlives the test's folder.
const STAND_IN = `
import { spawn } from "node:child_process";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
let said = "";
for await (const piece of process.stdin) said += piece;
const asked = JSON.parse(said);
const { store, args } = asked;
const wait = () => setInterval(() => existsSync(store) || process.exit(0), 200);
const living = (pid) => {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
};
if (args.then === "look") {
  // Whether the writer of the request before this one still lives, as this one starts.
  process.stdout.write(JSON.stringify({ alive: living(Number(readFileSync(store + "/writer.pid", "utf8"))) }));
} else if (args.then === "say") {
  process.stdout.write(JSON.stringify({ asked, argv: readFileSync("/proc/self/cmdline", "utf8").split("\0").slice(2, -1), env: process.env, cwd: process.cwd() }));
} else if (args.then === "flood") {
  process.stdout.write("x".repeat(7 * 1024 ** 2), wait);
} else {
  const writer = spawn("/bin/sh", ["-c", "while [ -d " + store + " ]; do echo beat >> " + store + "/beat; sleep 0.02; done"], { stdio: "ignore" });
  writeFileSync(store + "/writer.pid", String(writer.pid));
  writer.unref();
  if (args.then === "answer") process.stdout.write("{}", () => process.exit(0));
  else if (args.then === "fail") process.exit(1);
  else wait();
}
`;

describe("a request to a folder's history, as the agent runs it", () => {
  let mounted: Places;
  let store: string;
  const request = (then: string) => ({ thread: THREAD, user: "u1", action: "open", args: { then } });
  // The writer a request started, once it writes.
  const writer = async () => {
    await until(() => existsSync(join(store, "beat")));
    return Number(readFileSync(join(store, "writer.pid"), "utf8"));
  };
  const real = (historyMs?: number) => new Places({
    folder: join(dir, "places"), mount: async () => {}, unmount: async () => {}, historyMs,
    ask: (asked, stop) => askHistory(asked, stop, [process.execPath, join(dir, "history.mjs")]),
  });

  beforeEach(async () => {
    writeFileSync(join(dir, "history.mjs"), STAND_IN);
    store = join(dir, "places", KEY, "history");
    mounted = real();
    await mounted.mount(KEY, R1, R2);
  });

  afterEach(async () => {
    await mounted.stop();
  });

  it("gives the history its request on its input alone: nothing of it is on a command line or in the environment", async () => {
    const odd = { thread: "$(touch /tmp/x)", user: "-o/../x", action: "--upload-pack=/x", args: { then: "say", more: ["; id", "\n"] } };
    const said = await mounted.history(KEY, odd, signal());
    expect(said).toEqual({ ok: {
      asked: { store, folder: join(dir, "places", KEY, "real"), ...odd },
      argv: [], env: { PATH: "/usr/local/bin:/usr/bin:/bin", HOME: "/nonexistent", LANG: "C.UTF-8" }, cwd: "/",
    } });
  });

  it("ends everything a request started at its bound, a git it left writing too, before the place's next request starts", async () => {
    await mounted.stop();
    mounted = real(400);
    await mounted.mount(KEY, R1, R2);
    const first = mounted.history(KEY, request("wait"), signal());
    const next = mounted.history(KEY, request("look"), signal());
    const pid = await writer();
    expect(alive(pid)).toBe(true);
    expect(await first).toEqual(NO_ANSWER);
    expect(await next).toEqual({ ok: { alive: false } });
    expect(alive(pid)).toBe(false);
  });

  it("ends everything a request started at a cancel, before the place's next request starts", async () => {
    const cancel = new AbortController();
    const first = mounted.history(KEY, request("wait"), cancel.signal);
    const next = mounted.history(KEY, request("look"), signal());
    const pid = await writer();
    cancel.abort();
    expect(await first).toEqual(CANCELLED);
    expect(await next).toEqual({ ok: { alive: false } });
    expect(alive(pid)).toBe(false);
  });

  it("ends everything its requests started at the guest's stop, and when their place is let go", async () => {
    const first = mounted.history(KEY, request("wait"), signal());
    const pid = await writer();
    await mounted.stop();
    expect(alive(pid)).toBe(false);
    expect(await first).toEqual(SANDBOX_STOPPED);
    // And a place's mounts go only once nothing of its requests is left to write through them.
    let living: boolean | undefined;
    mounted = new Places({
      folder: join(dir, "places"), mount: async () => {},
      unmount: async () => {
        living ??= alive(Number(readFileSync(join(store, "writer.pid"), "utf8")));
      },
      ask: (asked, stop) => askHistory(asked, stop, [process.execPath, join(dir, "history.mjs")]),
    });
    rmSync(join(store, "beat"));
    await mounted.mount(KEY, R1, R2);
    const second = mounted.history(KEY, request("wait"), signal());
    const other = await writer();
    expect(other).not.toBe(pid);
    await mounted.unmount(KEY);
    expect(living).toBe(false);
    expect(alive(other)).toBe(false);
    expect(await second).toEqual(NO_ANSWER);
  });

  it("leaves nothing running that a history left behind when it ended, with an answer or without", async () => {
    expect(await mounted.history(KEY, request("answer"), signal())).toEqual({ ok: {} });
    const pid = Number(readFileSync(join(store, "writer.pid"), "utf8"));
    expect(alive(pid)).toBe(false);
    expect(await mounted.history(KEY, request("fail"), signal())).toEqual(NO_ANSWER);
    const other = Number(readFileSync(join(store, "writer.pid"), "utf8"));
    expect(other).not.toBe(pid);
    expect(alive(other)).toBe(false);
  });

  it("answers that the history did not, for an answer past its size and a history that cannot be run", async () => {
    expect(await mounted.history(KEY, request("flood"), signal())).toEqual(NO_ANSWER);
    const none = new Places({
      folder: join(dir, "places"), mount: async () => {}, unmount: async () => {},
      ask: (asked, stop) => askHistory(asked, stop, [join(dir, "no-python"), "-I", join(dir, "history.mjs")]),
    });
    await none.mount(KEY, R1, R2);
    expect(await none.history(KEY, request("say"), signal())).toEqual(NO_ANSWER);
    expect(await none.history(KEY, request("say"), signal())).toEqual(NO_ANSWER);
  });
});

describe("the agent's control port, asked for a history", () => {
  const control = (history?: ControlPlaces["history"]) => {
    const sent: FromAgent[] = [];
    const agent = new Control(
      (message) => sent.push(message),
      { uid: () => 10_000, setup: async () => {}, teardown: async () => {}, perform: async () => ({ ok: true }) },
      undefined,
      history && { mount: async () => {}, unmount: async () => {}, history },
    );
    const tell = async (message: unknown) => {
      agent.receive(JSON.stringify(message));
      await new Promise((resolve) => setTimeout(resolve, 0));
    };
    return { sent, tell };
  };

  it("passes a request on and answers its outcome, and stops one that is cancelled", async () => {
    const asked: unknown[] = [];
    const { sent, tell } = control((key, request, stop) => new Promise<Outcome>((resolve) => {
      asked.push([key, request]);
      if (request.action === "open") stop.addEventListener("abort", () => resolve(CANCELLED), { once: true });
      else resolve({ ok: { hash: ID } });
    }));
    await tell({ type: "history", id: 1, key: KEY, thread: THREAD, user: "u1", action: "snapshot", args: { reason: "r" } });
    await tell({ type: "history", id: 2, key: KEY, thread: THREAD, user: "u1", action: "open", args: {} });
    // An id still running keeps its request, and its cancel.
    await tell({ type: "history", id: 2, key: KEY, thread: THREAD, user: "u1", action: "snapshot", args: {} });
    await tell({ type: "cancel", id: 2 });
    expect(asked).toEqual([
      [KEY, { thread: THREAD, user: "u1", action: "snapshot", args: { reason: "r" } }],
      [KEY, { thread: THREAD, user: "u1", action: "open", args: {} }],
    ]);
    expect(sent).toEqual([
      { type: "result", id: 1, outcome: { ok: { hash: ID } } },
      { type: "result", id: 2, outcome: { error: { type: "other", message: "An operation with this id is already running" } } },
      { type: "result", id: 2, outcome: CANCELLED },
    ]);
  });

  it("refuses a request whose fields are not a history's, and one asked of an agent that keeps no folder's history", async () => {
    const { sent, tell } = control(async () => ({ ok: {} }));
    const whole = { type: "history", key: KEY, thread: THREAD, user: "u1", action: "open", args: {} };
    const changes = [{ thread: 7 }, { args: [] }, { args: null }, { key: null }, { user: undefined }, { action: {} }];
    for (const [id, change] of changes.entries()) await tell({ ...whole, id, ...change });
    expect(sent).toEqual(changes.map((_change, id) => ({
      type: "result", id, outcome: { error: { type: "value", message: "The agent cannot take this history request" } },
    })));
    const none = control();
    await none.tell({ ...whole, id: 1 });
    expect(none.sent).toEqual([{ type: "result", id: 1, outcome: { error: { type: "unavailable", message: "The agent keeps no folder's history" } } }]);
  });
});

const NOT_AN_ANSWER = {
  error: { type: "history", code: "not_an_answer", message: "This computer's sandbox answered what is not a history's answer, so it was not used" },
};

describe("a request to a folder's history", () => {
  const request: HistoryRequest = {
    place: { key: "0123456789abcdef", history: "/data/history/0123456789abcdef", real: { path: "/home/ana/Documents", dev: 1, ino: 2 } },
    thread: "0b6c1d3e-6f0a-4c1e-9a52-6a1d2c3b4e5f", user: "ana@corp.example", action: "open", args: {},
  };

  it("names a thread and a user by their ids, an action and its arguments, or is none", () => {
    expect(named(request)).toBe(true);
    for (const change of [
      { thread: "../x" }, { thread: `${request.thread}\n` }, { thread: request.thread.toUpperCase() }, { thread: `${request.thread}/..` }, { thread: "" },
      { thread: 7 }, { thread: [request.thread] }, { thread: undefined },
      { user: "" }, { user: "u1\n" }, { user: "u 1" }, { user: "u1;id" }, { user: "u/../x" }, { user: "--upload-pack=/x" }, { user: "u".repeat(129) },
      { user: undefined }, { user: null }, { user: 7 }, { user: ["u1"] },
      { action: undefined }, { action: 7 }, { args: null }, { args: [] }, { args: "{}" }, { args: undefined }, { place: undefined }, { place: null },
    ]) {
      expect(named({ ...request, ...change } as unknown as HistoryRequest), JSON.stringify(change)).toBe(false);
    }
  });
});

describe("what the guest answered, checked on this computer", () => {
  const version = { path: "Report.docx", before: BLOB, after: ID };
  const turn = { commit: ID, base: ID, changes: [], overlapped: [], excluded: [], repositories: [], not_taken: [] };
  const picked = { main: null, commit: ID, picked_up: [], packs: 0 };
  // Each action's answers, as the history gives them (local_history.py, _ACTIONS).
  const answers: Record<string, unknown[]> = {
    open: [{ copy: "made" }, { copy: "moved" }, { copy: "kept" }, { copy: "kept", set_asides: [BLOB, ID] }],
    changed: [{ paths: ["Report.docx", "new folder/a b.md"] }, { paths: [] }],
    snapshot: [{ hash: ID }],
    restore: [{}],
    fetch: [{ main: ID, landing: BLOB, hidden: false, packs: 12, missing: [BLOB] }, { main: null, landing: null, hidden: true, packs: 0, missing: [] }],
    pickup: [{ main: null, commit: ID, picked_up: [version], packs: 0 }, { main: ID, commit: null, picked_up: [], packs: 7 }],
    commit: [
      {
        commit: ID, base: ID, changes: [version, { path: "gone.txt", before: BLOB, after: null }, { path: "gone before.txt", before: null, after: null }],
        overlapped: [
          { path: "a.txt", reason: "changed", before: BLOB, after: ID, by: { kind: "thread", id: "t1", title: "Draft A" } },
          { path: "b.txt", reason: "changed", before: null, after: ID, by: { kind: "you" } },
          { path: "c.txt", reason: "with", before: BLOB, after: null },
          { path: "d.txt", reason: "shape", before: null, after: ID, by: { kind: "routine", name: "Nightly" } },
        ],
        excluded: ["build/", "notes.tmp"], repositories: ["vendor/lib/"], not_taken: ["helpers.md"],
      },
      { ...turn, commit: null },
    ],
    record: [{ commit: ID, set_aside: null }, { commit: ID, set_aside: BLOB }],
    keep: [{ commit: ID, not_taken: [] }, { commit: ID, not_taken: ["helpers.md"] }],
    forget: [{ landing: ID }, { landing: null }],
  };
  const source = (name: string) => readFileSync(new URL(`../../surogates/sandbox/${name}`, import.meta.url), "utf8");

  it("takes each action's answer, and passes on its own fields alone", () => {
    for (const [action, given] of Object.entries(answers)) for (const answer of given) expect(checked(action, { ok: answer })).toEqual({ ok: answer });
    // A folder with no history says why: more files than history tracks, or a name that is not UTF-8.
    expect(checked("open", { ok: { history: "off", reason: "cap", planted: "x" } })).toEqual({ ok: { history: "off", reason: "cap" } });
    expect(checked("open", { ok: { history: "off", reason: "names" } })).toEqual({ ok: { history: "off", reason: "names" } });
    expect(checked("snapshot", { ok: { hash: ID, also: { deep: [1] } } })).toEqual({ ok: { hash: ID } });
    expect(checked("pickup", { ok: { main: null, commit: null, picked_up: [{ ...version, mode: "100755" }], packs: 0 } }))
      .toEqual({ ok: { main: null, commit: null, picked_up: [version], packs: 0 } });
    expect(checked("fetch", { ok: { main: ID, landing: null, hidden: false, packs: 0, missing: [], has_saga: true } }))
      .toEqual({ ok: { main: ID, landing: null, hidden: false, packs: 0, missing: [] } });
    expect(checked("restore", { ok: { commit: ID } })).toEqual({ ok: {} });
  });

  // What of a thread's own the place keeps set aside whole, each by its folder's name there: the order it was set
  // aside in, when, whose, and which of the two.
  const aside = (n: number, kind: string, thread = THREAD) => `${String(n).padStart(8, "0")}-20261010T03${String(n % 60).padStart(2, "0")}00Z-${thread}.${kind}`;

  it("takes from an open the names of what was set aside whole of the request's own thread, kept and gone, on either of its forms", () => {
    const kept = [aside(3, "repository"), aside(3, "copy"), aside(7, "copy")];
    const gone = [aside(1, "copy"), aside(2, "repository")];
    for (const answer of [
      { copy: "made", set_aside_folders: kept }, { copy: "made", set_aside_folders: kept, set_aside_gone: gone },
      { copy: "kept", set_asides: [ID], set_aside_gone: gone }, { history: "off", reason: "cap", set_aside_folders: kept },
      { history: "off", reason: "names", set_aside_folders: kept, set_aside_gone: gone },
      // As many as a thread is told of, with room: it keeps four times, of two names each, and is told the last sixteen times one went.
      { copy: "moved", set_aside_folders: Array.from({ length: 64 }, (_unused, n) => aside(n, "copy")), set_aside_gone: Array.from({ length: 64 }, (_unused, n) => aside(n, "repository")) },
    ]) {
      expect(checked("open", { ok: answer }, THREAD)).toEqual({ ok: answer });
    }
    // No other action's answer names any.
    expect(checked("snapshot", { ok: { hash: ID, set_aside_folders: kept } }, THREAD)).toEqual({ ok: { hash: ID } });
  });

  const OTHER = "7d8e9f00-1a2b-4c3d-8e4f-5a6b7c8d9e0f";
  it.each<[string, unknown]>([
    // Another thread's, or no thread's the check was told of.
    ["another thread's", [aside(1, "copy", OTHER)]],
    ["one that ends in the thread's and starts with another's", [`${aside(1, "copy", OTHER).slice(0, -5)}-${THREAD}.copy`]],
    ["a name that is the thread's alone", [`${THREAD}.copy`]],
    // A name that is not as the history makes one.
    ["a path out of the place", [`../../${aside(1, "copy")}`]],
    ["a path into it", [`${aside(1, "copy")}/Report.docx`]],
    ["a third kind", [aside(1, "gone")]],
    ["one that went, by the name it has on disk", [`${aside(1, "copy")}.gone`]],
    ["a count of fewer digits", [aside(1, "copy").slice(1)]],
    ["a count that is none", [`x${aside(1, "copy").slice(1)}`]],
    ["a time that is none", [aside(1, "copy").replace("T03", "T3x")]],
    ["upper case", [aside(1, "copy").toUpperCase()]],
    ["a line more", [`${aside(1, "copy")}\n`]],
    ["no text", [7]],
    ["none", [null]],
    ["a list in a list", [[aside(1, "copy")]]],
    // What is no list, or more than any thread is told.
    ["text", aside(1, "copy")],
    ["an object", { 0: aside(1, "copy"), length: 1 }],
    ["sixty-five", Array.from({ length: 65 }, (_unused, n) => aside(n, "copy"))],
    ["a million", Array<string>(1_000_000).fill(aside(1, "copy"))],
  ])("refuses an open that names, among what was set aside, %s", (_what, names) => {
    for (const key of ["set_aside_folders", "set_aside_gone"]) {
      expect(checked("open", { ok: { copy: "made", [key]: names } }, THREAD)).toEqual(NOT_AN_ANSWER);
      expect(checked("open", { ok: { history: "off", reason: "cap", [key]: names } }, THREAD)).toEqual(NOT_AN_ANSWER);
    }
  });

  it("refuses every name of what was set aside where it is not told whose the request was", () => {
    for (const thread of [undefined, "", OTHER, THREAD.toUpperCase(), `${THREAD} `, ".*"]) {
      expect(checked("open", { ok: { copy: "made", set_aside_folders: [aside(1, "copy")] } }, thread)).toEqual(NOT_AN_ANSWER);
    }
    // Nor a name made for what it was told, where that is no thread's id.
    for (const thread of ["", "x", ".*", "../..", THREAD.toUpperCase(), `${THREAD} `, `${THREAD}/x`]) {
      expect(checked("open", { ok: { copy: "made", set_aside_folders: [aside(1, "copy", thread)] } }, thread), thread).toEqual(NOT_AN_ANSWER);
    }
    // An open that names none is an open's answer whoever asked.
    expect(checked("open", { ok: { copy: "made" } })).toEqual({ ok: { copy: "made" } });
  });

  it("knows every action a folder's history takes, and no other", () => {
    const actions = /^_ACTIONS[^]*?^\}/m.exec(source("local_history.py"))![0];
    expect([...actions.matchAll(/^ {4}"([a-z_]+)": \(/gm)].map(([, action]) => action).sort()).toEqual(Object.keys(answers).sort());
    for (const action of ["apply", "unapply", "prune", "take_up", "opened", "hand_off", "hand_back", "keep_apart", "drop_hand_off", "constructor", "__proto__", "toString", ""]) {
      expect(checked(action, { ok: {} })).toEqual(NOT_AN_ANSWER);
    }
  });

  // The history's own codes, in history.py and local_history.py: each name a refusal is raised with, by its word.
  const HISTORYS = [
    "failed", "history_refused", "conflict", "no_whole_copy", "name_not_utf8", "not_a_request", "record_unfinished", "move_unfinished",
    "landing_unsettled", "not_on_base",
  ];

  it("knows every code a folder's history refuses with, and the agent's own for a history that wrote no answer", () => {
    const sources = `${source("history.py")}\n${source("local_history.py")}`;
    const words = new Map([...sources.matchAll(/^([A-Z][A-Z0-9_]+) = "([a-z0-9_]+)"$/gm)].map(([, name, word]) => [name, word]));
    const raised = new Set([...sources.matchAll(/\bcode"?(?:: str)?\s*[=:]\s*([A-Z][A-Z0-9_]+)\b/g)].map(([, name]) => words.get(name!)));
    expect([...raised].sort()).toEqual([...HISTORYS].sort());
    for (const code of [...HISTORYS, "no_answer"]) {
      expect(checked("snapshot", { error: { type: "history", code, message: "its words", planted: { deep: [1] } } }))
        .toEqual({ error: { type: "history", code, message: "its words" } });
    }
  });

  it("passes on why a history did not answer as one of its codes with its words, and the agent's own errors with none", () => {
    expect(checked("open", { error: { type: "history", code: "failed", message: "x".repeat(5_000) } }))
      .toEqual({ error: { type: "history", code: "failed", message: "x".repeat(2_000) } });
    // A history's refusal with a code that is none of them, or with none, is no refusal of one: whoever asked goes by the code.
    for (const code of [undefined, null, 7, "", "made_up", "NO_WHOLE_COPY", "not_an_answer", "has_saga", ["failed"], { code: "failed" }]) {
      expect(checked("snapshot", { error: { type: "history", code, message: "refused the request: this thread has no whole copy, and its next open makes one" } }))
        .toEqual(NOT_AN_ANSWER);
    }
    // The agent's own, for a place that is not in the guest and a request it cannot take: each has no code, whatever it was sent with.
    expect(checked("open", { error: { type: "unavailable", code: "no_whole_copy", message: "This folder's history is not in the sandbox" } }))
      .toEqual({ error: { type: "unavailable", message: "This folder's history is not in the sandbox" } });
    expect(checked("open", { error: { type: "value", message: "The agent cannot take this history request", detail: [1] } }))
      .toEqual({ error: { type: "value", message: "The agent cannot take this history request" } });
    expect(checked("open", { error: { type: "other", message: "y".repeat(2_500) } })).toEqual({ error: { type: "other", message: "y".repeat(2_000) } });
    // The guest's own stop, as this computer says a guest went.
    expect(checked("open", { ...SANDBOX_STOPPED, more: 1 })).toEqual(SANDBOX_STOPPED);
    // A cancel is this computer's alone to say, and a type nobody has is nobody's.
    for (const type of ["cancelled", "ok", "History", "", "x".repeat(100_000)]) {
      expect(checked("open", { error: { type, message: "The session stopped this command" } })).toEqual(NOT_AN_ANSWER);
    }
  });

  it("leaves out of a turn's applies every file whose change could run code on this computer, whatever the guest's git said", () => {
    const change = (path: string) => ({ path, before: null, after: ID });
    const answer = {
      ...turn, overlapped: [{ path: "b.txt", reason: "with", before: null, after: ID }],
      changes: ["Report.docx", ".git/hooks/pre-commit", ".vscode/tasks.json", "src/.gitmodules", ".github/workflows/ci.yml", ".claude/commands/go.md"].map(change),
    };
    expect(checked("commit", { ok: answer })).toEqual({ ok: {
      ...answer,
      changes: [change("Report.docx"), change(".github/workflows/ci.yml")],
      overlapped: [
        { ...change(".claude/commands/go.md"), reason: "protected" }, { ...change(".git/hooks/pre-commit"), reason: "protected" },
        { ...change(".vscode/tasks.json"), reason: "protected" }, { path: "b.txt", reason: "with", before: null, after: ID },
        { ...change("src/.gitmodules"), reason: "protected" },
      ],
    } });
  });

  const changing = (path: unknown) => ({ ...turn, changes: [{ path, before: null, after: ID }] });
  it.each<[string, unknown]>([
    // A path that leads out of the folder, or is none.
    ["commit", changing("../outside.txt")],
    ["commit", changing("/etc/passwd")],
    ["commit", changing("a/../../b")],
    ["commit", changing("a//b")],
    ["commit", changing("a/")],
    ["commit", changing("")],
    ["commit", changing(".")],
    ["commit", changing("a\0b")],
    ["commit", changing("x".repeat(4_097))],
    ["commit", changing("half a \ud83d character")],
    ["commit", changing(7)],
    ["commit", changing(["a.txt"])],
    ["commit", { ...turn, excluded: ["a//b/"] }],
    ["commit", { ...turn, excluded: [7] }],
    ["commit", { ...turn, repositories: ["../vendor/"] }],
    ["commit", { ...turn, not_taken: ["/abs"] }],
    ["pickup", { ...picked, picked_up: [{ path: "a/./b", before: null, after: ID }] }],
    ["pickup", { ...picked, picked_up: [{ path: "a\0b", before: null, after: ID }] }],
    ["pickup", { ...picked, picked_up: "all" }],
    ["changed", { paths: ["Report.docx", "../../.ssh/authorized_keys"] }],
    ["changed", { paths: { length: 1, 0: "Report.docx" } }],
    ["keep", { commit: ID, not_taken: ["../outside.txt"] }],
    // An id that is not forty hex digits.
    ["snapshot", { hash: "--upload-pack=/x" }],
    ["snapshot", { hash: ID.toUpperCase() }],
    ["snapshot", { hash: `${ID}\n` }],
    ["snapshot", { hash: ID.slice(1) }],
    ["snapshot", { hash: [ID] }],
    ["fetch", { main: ID, landing: null, hidden: false, packs: 0, missing: ["--upload-pack=/x"] }],
    ["fetch", { main: "main", landing: null, hidden: false, packs: 0, missing: [] }],
    ["fetch", { main: ID, landing: "refs/heads/main", hidden: false, packs: 0, missing: [] }],
    ["commit", { ...turn, base: "main" }],
    ["commit", { ...turn, base: null }],
    ["commit", { ...turn, changes: [{ path: "a.txt", before: "HEAD", after: ID }] }],
    ["record", { commit: null, set_aside: null }],
    ["record", { commit: ID, set_aside: "refs/set-aside/1" }],
    ["open", { copy: "kept", set_asides: [ID, "ID"] }],
    ["forget", { landing: "main" }],
    // A shape that is not the action's: a field of another type, one that is missing, a word nobody says.
    ["open", { copy: "everywhere" }],
    ["open", { history: "off" }],
    ["open", { history: "off", reason: "whenever" }],
    ["open", { history: "on", reason: "cap" }],
    ["open", { copy: "kept", set_asides: ID }],
    ["open", "made"],
    ["open", ["made"]],
    ["open", null],
    ["restore", null],
    ["restore", []],
    ["fetch", { main: ID, has_saga: true, packs: 0, missing: [] }],
    ["fetch", { main: ID, landing: null, hidden: "no", packs: 0, missing: [] }],
    ["fetch", { main: ID, landing: null, hidden: false, packs: -1, missing: [] }],
    ["fetch", { main: ID, landing: null, hidden: false, packs: 1.5, missing: [] }],
    ["fetch", { main: ID, landing: null, hidden: false, packs: 2 ** 60, missing: [] }],
    ["fetch", { main: ID, landing: null, hidden: false, packs: "0", missing: [] }],
    ["commit", { ...turn, overlapped: [{ path: "a", reason: "changed", before: null, after: ID, by: { kind: "root" } }] }],
    ["commit", { ...turn, overlapped: [{ path: "a", reason: "changed", before: null, after: ID, by: { kind: "thread", id: "t1" } }] }],
    ["commit", { ...turn, overlapped: [{ path: "a", reason: "changed", before: null, after: ID, by: { kind: "thread", id: "t1", title: "x".repeat(4_097) } }] }],
    ["commit", { ...turn, overlapped: [{ path: "a", reason: "changed", before: null, after: ID, by: null }] }],
    ["commit", { ...turn, overlapped: [{ path: "a", reason: "because", before: null, after: ID }] }],
    ["commit", { ...turn, overlapped: [{ path: "a", reason: "protected", before: null, after: ID }] }],
    ["commit", { ...turn, not_taken: undefined }],
    ["record", { commit: ID }],
    ["keep", { commit: ID }],
    ["forget", {}],
    ["forget", { landing: undefined }],
    // More than any answer names.
    ["changed", { paths: Array<string>(1_000_000).fill("a.txt") }],
    ["commit", { ...turn, changes: Array<unknown>(50_001).fill({ path: "a.txt", before: null, after: ID }) }],
    ["open", { copy: "kept", set_asides: Array<string>(50_001).fill(ID) }],
    // An action that is none of this computer's history.
    ["prune", { pruned: true }],
    ["apply", { path: "a.txt" }],
  ])("refuses %s's answer %j", (action, answer) => {
    expect(checked(action, { ok: answer })).toEqual(NOT_AN_ANSWER);
  });
});

describe("what a landing kept of a folder's files, forgotten only by the history's word", () => {
  const UNSETTLED = {
    error: { type: "history", code: "landing_unsettled", message: "refused the request: this landing was neither recorded nor put back whole" },
  };
  const NOT_A_FORGETTING = {
    error: { type: "value", message: "This is no answer of a folder's history to forgetting a landing, so what the landing kept was not forgotten" },
  };

  it("may be forgotten once the history holds the landing, or says each file it applied is as it was before", () => {
    expect(forgettable({ ok: { landing: ID } })).toBeNull();
    expect(forgettable({ ok: { landing: null } })).toBeNull();
    // As the check gives a guest's answer on: its own field alone.
    expect(forgettable(checked("forget", { ok: { landing: ID, main: ID, planted: [1] } }))).toBeNull();
  });

  it("is not while the history refuses, or did not answer: the refusal is why", () => {
    for (const refusal of [UNSETTLED, NO_ANSWER, NOT_AN_ANSWER, NOT_HERE, CANCELLED, SANDBOX_STOPPED, { error: { type: "history", code: "failed", message: "git failed" } }]) {
      expect(forgettable(refusal)).toBe(refusal);
    }
    expect(forgettable(checked("forget", { ok: { landing: "main" } }))).toEqual(NOT_AN_ANSWER);
  });

  it("is not by an answer that is no forgetting's: a look at the history names a landing too, and null there is one that did not land", () => {
    const others: unknown[] = [
      { main: ID, landing: null, hidden: false, packs: 0, missing: [] }, { main: null, landing: ID, hidden: false, packs: 0, missing: [] },
      { landing: null, main: ID }, { landing: ID, hidden: false }, { commit: ID, set_aside: null }, { commit: ID, not_taken: [] }, { copy: "made" },
      {}, { landing: "main" }, { landing: undefined }, { landing: ID.toUpperCase() }, { landing: [ID] }, { Landing: ID }, null, undefined, [], [null], [ID],
      "landing", 7, true,
    ];
    for (const ok of others) expect(forgettable({ ok }), JSON.stringify(ok)).toEqual(NOT_A_FORGETTING);
    // And what is no outcome at all, or one that is both.
    for (const outcome of [null, undefined, 7, "ok", [], {}]) expect(forgettable(outcome as unknown as Outcome)).toEqual(NOT_A_FORGETTING);
    expect(forgettable({ ok: { landing: null }, error: null } as unknown as Outcome)).not.toBeNull();
    expect(forgettable({ ok: { landing: ID }, error: UNSETTLED.error } as unknown as Outcome)).not.toBeNull();
  });
});

describe("what the guest sent in an outcome's place, checked on this computer", () => {
  it.each([
    [null], [undefined], [7], ["ok"], [true], [[]], [[{ ok: { copy: "made" } }]], [{}], [{ error: null }], [{ error: "git failed" }], [{ error: 7 }],
    [{ error: [] }], [{ error: { type: 7, message: "m" } }], [{ error: { type: "history" } }], [{ error: { type: "history", code: "failed" } }],
    [{ error: { type: "history", code: "failed", message: 7 } }], [{ error: { type: "history", message: "words alone" } }],
    [{ error: { message: "words alone" } }], [{ ok: null }], [{ ok: null, error: null }], [{ ok: { copy: "made" }, error: null }],
  ])("refuses %j, and never throws", (outcome) => {
    expect(checked("open", outcome)).toEqual(NOT_AN_ANSWER);
  });

  it("refuses what cannot even be read, and never throws", () => {
    const thrower = new Proxy({}, {
      get: () => {
        throw new Error("read");
      },
      has: () => {
        throw new Error("looked for");
      },
      ownKeys: () => {
        throw new Error("listed");
      },
    });
    const trapped = { get copy(): string {
      throw new Error("read");
    } };
    for (const outcome of [thrower, { ok: thrower }, { error: thrower }, { ok: trapped }, { ok: { paths: thrower } }]) {
      expect(checked("open", outcome)).toEqual(NOT_AN_ANSWER);
      expect(checked("changed", outcome)).toEqual(NOT_AN_ANSWER);
    }
    expect(checked(thrower as unknown as string, { ok: {} })).toEqual(NOT_AN_ANSWER);
  });

  it("holds an answer of a million entries, or of one long word, for no longer than it takes to count it", () => {
    const began = performance.now();
    const huge = "x".repeat(8 * 1024 ** 2);
    for (const outcome of [
      { ok: { paths: Array<string>(1_000_000).fill(huge) } }, { ok: { hash: huge } }, { ok: { copy: huge } },
      { error: { type: huge, message: huge } }, { error: { type: "history", code: huge, message: huge } },
    ]) {
      expect(checked("changed", outcome)).toEqual(NOT_AN_ANSWER);
    }
    expect(checked("open", { error: { type: "history", code: "failed", message: huge } })).toEqual({ error: { type: "history", code: "failed", message: "x".repeat(2_000) } });
    expect(performance.now() - began).toBeLessThan(2_000);
  });

  // How long the check of *answer*, a turn's commit, takes, and what it gave.
  const timed = (answer: unknown) => {
    const began = performance.now();
    const taken = checked("commit", { ok: answer });
    return { took: performance.now() - began, taken: taken as { ok: { changes: unknown[]; overlapped: unknown[] } } };
  };
  const commit = { commit: ID, base: ID, overlapped: [], excluded: [], repositories: [], not_taken: [] };
  // The most the check of one line of the control port may take, on a loaded computer: it took about a third of a
  // second for the deepest paths a line can hold where this was written, and more than half a minute before each
  // part of a path was looked at once.
  const LINE_MS = 2_000;

  it("checks a turn's commit in time that grows with its size, whatever its paths are made of: one of as many parts as a path may have, a line full of them", () => {
    // 4,096 units hold 2,048 names of a letter each, and one line of the control port 2,009 changes of such a path.
    const deep = Array<string>(2_048).fill("a").join("/");
    const changes = Array<unknown>(2_009).fill({ path: deep, before: null, after: ID });
    expect(JSON.stringify({ type: "result", id: 1, outcome: { ok: { ...commit, changes } } }).length).toBeGreaterThan(8 * 1024 ** 2 - 8_192);
    const plain = timed({ ...commit, changes });
    expect(plain.taken.ok.changes).toHaveLength(2_009);
    expect(plain.took).toBeLessThan(LINE_MS);
    // With one name that no landing writes, among as many files left out: the list they are sorted into is as long.
    const left = Array<unknown>(1_000).fill({ path: deep, reason: "with", before: null, after: ID });
    const one = timed({ ...commit, overlapped: left, changes: [...changes.slice(0, 1_000), { path: `${deep.slice(0, -18)}/.git/hooks/commit`, before: null, after: ID }] });
    expect(one.taken.ok.changes).toHaveLength(1_000);
    expect(one.taken.ok.overlapped).toHaveLength(1_001);
    expect(one.took).toBeLessThan(LINE_MS);
    // Paths made of git's own folders, each of which is looked into, are no slower.
    for (const part of [".git", ".git/modules/a/b", "node_modules", ".claude", "modules/hooks"]) {
      const names = `${part}/`.repeat(Math.floor(4_090 / (part.length + 1)));
      const such = timed({ ...commit, changes: Array<unknown>(2_009).fill({ path: `${names}x`, before: null, after: ID }) });
      expect(such.took, part).toBeLessThan(LINE_MS);
    }
  });

  it("takes a landing as large as a folder's history can answer: the agent's 6 MiB of changes, or as many as a folder history tracks has files", () => {
    // As many files as a folder with a history may hold, each made by the turn: 50,000 fit an answer where their names are short.
    const every = Array.from({ length: 50_000 }, (_unused, n) => ({ path: `d${n % 100}/f${n}`, before: null, after: ID }));
    expect(JSON.stringify({ ...commit, changes: every }).length).toBeLessThan(6 * 1024 ** 2);
    const whole = timed({ ...commit, changes: every });
    expect(whole.taken.ok.changes).toHaveLength(50_000);
    expect(whole.took).toBeLessThan(LINE_MS);
    // And an answer's full size of files named as a project names them.
    const named = Array.from({ length: 29_000 }, (_unused, n) => ({
      path: `packages/area-${n % 40}/src/components/feature-${n % 700}/a rather long file name, as a document has, ${n}.docx`, before: BLOB, after: ID,
    }));
    const size = JSON.stringify({ ...commit, changes: named }).length;
    expect(size).toBeGreaterThan(5.5 * 1024 ** 2);
    expect(size).toBeLessThan(6 * 1024 ** 2);
    const full = timed({ ...commit, changes: named });
    expect(full.taken.ok.changes).toHaveLength(29_000);
    expect(full.took).toBeLessThan(LINE_MS);
    // One file more than a folder with a history has is no answer.
    expect(checked("commit", { ok: { ...commit, changes: [...every, { path: "one more", before: null, after: ID }] } })).toEqual(NOT_AN_ANSWER);
  });
});

describe("the VM manager, asked for a folder's history", () => {
  const options = (): VmOptions => ({
    kernel: "/i/vmlinuz", rootfs: "/i/rootfs.img", agentDisk: "/a/agent.img", sessions: join(dir, "data", "sessions.img"),
    run: join(dir, "run"), console: join(dir, "logs", "console.log"), user: { uid: 1000, gid: 1000, name: "ana", home: "/home/ana" }, kvm: join(dir, "kvm"),
  });
  const place = (): Place => ({ key: KEY, history: join(dir, "store"), real: { path: join(dir, "Documents"), ...statSync(join(dir, "Documents")) } });
  const ask = (action: string, args: Record<string, unknown> = {}): HistoryRequest => ({ place: place(), thread: THREAD, user: "u1", action, args });
  // A guest whose agent's Control is the real one, its places answering *history*: what the agent was asked, and when the guest went.
  const guest = (asked: unknown[], history: ControlPlaces["history"], places: Partial<ControlPlaces> = {}): BootVm => async () => {
    const [host, agent] = duplexPair();
    const [net] = duplexPair();
    const [inbound] = duplexPair();
    let gone = (_said: string) => {};
    const exited = new Promise<string>((resolve) => {
      gone = resolve;
    });
    const kill = async () => {
      host.destroy();
      net.destroy();
      inbound.destroy();
      gone("");
    };
    void exited.then(() => asked.push(["gone"]));
    const control = new Control(
      (message) => void agent.write(`${JSON.stringify(message)}\n`),
      { uid: () => 10_000, setup: async () => {}, teardown: async () => {}, perform: async () => ({ ok: true }) },
      { setClock: async () => {}, woke: () => {}, heard: () => {}, powerOff: kill },
      {
        mount: async (key, store, real) => void asked.push(["mount", key, store.tag, real.tag]),
        unmount: async (key) => void asked.push(["unmount", key]),
        history, ...places,
      },
    );
    createInterface({ input: agent }).on("line", (line) => control.receive(line));
    control.hello();
    let made = 0;
    return { control: host, net, inbound, exited, emulated: null, kill, share: async () => ({ kind: "virtiofs", tag: `r${(made += 1)}` }), unshare: async () => {} };
  };

  it("places the folder first, then asks the agent, and checks its answer", async () => {
    const asked: unknown[] = [];
    const manager = new VmManager(options(), guest(asked, async (key, request) => {
      asked.push(["history", key, request]);
      return { ok: request.action === "snapshot" ? { hash: ID, planted: true } : { hash: "not an id" } };
    }));
    expect(await manager.history(ask("snapshot", { reason: "r" }), signal())).toEqual({ ok: { hash: ID } });
    expect(asked).toEqual([["mount", KEY, "r1", "r2"], ["history", KEY, { thread: THREAD, user: "u1", action: "snapshot", args: { reason: "r" } }]]);
    // A second thread's request finds the place there.
    expect(await manager.history(ask("snapshot"), signal())).toEqual({ ok: { hash: ID } });
    expect(asked.filter((entry) => (entry as string[])[0] === "mount")).toHaveLength(1);
    // What is not an answer's is refused here, whatever the guest says.
    const lying = new VmManager(options(), guest([], async () => ({ ok: { hash: "not an id" } })));
    expect(await lying.history(ask("snapshot"), signal())).toEqual(NOT_AN_ANSWER);
    await manager.stop();
    await lying.stop();
  });

  it("answers a refusal, never a rejection, when the guest's outcome is none at all, or its history could not even be asked", async () => {
    for (const outcome of [null, 7, "ok", { error: null }, { error: { type: "cancelled", message: "The session stopped this command" } }]) {
      const manager = new VmManager(options(), guest([], async () => outcome as unknown as Outcome));
      expect(await manager.history(ask("open"), signal())).toEqual(NOT_AN_ANSWER);
      await manager.stop();
    }
    const failing = new VmManager(options(), guest([], () => Promise.reject(new Error("spawn EAGAIN"))));
    expect(await failing.history(ask("open"), signal())).toEqual({ error: { type: "other", message: "Error: spawn EAGAIN" } });
    await failing.stop();
  });

  it("asks nothing for a thread or a user that is not one's id, and answers a cancel at once", async () => {
    const asked: unknown[] = [];
    const manager = new VmManager(options(), guest(asked, (_key, _request, stop) => new Promise<Outcome>((resolve) => {
      stop.addEventListener("abort", () => resolve(CANCELLED), { once: true });
    })));
    for (const change of [{ thread: "../x" }, { thread: THREAD.toUpperCase().replace("0B", "0G") }, { user: "" }, { user: "u1\n" }, { action: 7 }, { args: null }, { place: null }]) {
      expect(await manager.history({ ...ask("open"), ...change } as unknown as HistoryRequest, signal())).toEqual({
        error: { type: "value", message: "This request names no thread, user or action of a history's" },
      });
    }
    // No guest was asked, and none was booted to refuse it.
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(asked).toEqual([]);
    const cancel = new AbortController();
    const waiting = manager.history(ask("open"), cancel.signal);
    setTimeout(() => cancel.abort(), 50);
    expect(await waiting).toEqual(CANCELLED);
    await manager.stop();
  });

  // A history whose requests are answered when the test says: what was asked, and each one's answer let go.
  const held = (asked: unknown[]) => {
    const release: Array<() => void> = [];
    const history: ControlPlaces["history"] = (_key, request) => new Promise<Outcome>((resolve) => {
      asked.push(["history", request.action]);
      release.push(() => {
        asked.push(["answered", request.action]);
        resolve({ ok: { paths: [] } });
      });
    });
    return { release, history };
  };

  it("is done in the order it was asked of its place: after a letting go asked before it, and before one asked after it", async () => {
    const asked: unknown[] = [];
    const { release, history } = held(asked);
    const manager = new VmManager(options(), guest(asked, history));
    const first = manager.history(ask("changed"), signal());
    await until(() => release.length === 1);
    // Let go while its request runs: the place stays until the request is answered.
    const gone = manager.unplace(place());
    // Asked for again meanwhile: another place, added once the first has gone, with shares of its own.
    const second = manager.history(ask("open"), signal());
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(asked).toEqual([["mount", KEY, "r1", "r2"], ["history", "changed"]]);
    release[0]!();
    expect(await first).toEqual({ ok: { paths: [] } });
    expect(await gone).toBe(true);
    await until(() => release.length === 2);
    expect(asked).toEqual([
      ["mount", KEY, "r1", "r2"], ["history", "changed"], ["answered", "changed"], ["unmount", KEY], ["mount", KEY, "r3", "r4"], ["history", "open"],
    ]);
    release[1]!();
    // Its answer is not an open's: checked as the answer of the action it was asked with.
    expect(await second).toEqual(NOT_AN_ANSWER);
    await manager.stop();
  });

  it("leaves nothing running when it is cancelled while it waits for its place's letting go: no place, no request, and a guest that stops", async () => {
    const asked: unknown[] = [];
    let unmounted = () => {};
    const manager = new VmManager(options(), guest(asked, async () => {
      asked.push(["history"]);
      return { ok: {} };
    }, {
      unmount: (key) => new Promise<void>((resolve) => {
        asked.push(["unmount", key]);
        unmounted = resolve;
      }),
    }));
    expect(await manager.place(place(), signal())).toBeNull();
    const gone = manager.unplace(place());
    await until(() => asked.length === 2);
    const cancel = new AbortController();
    const waiting = manager.history(ask("open"), cancel.signal);
    await new Promise((resolve) => setTimeout(resolve, 20));
    cancel.abort();
    expect(await waiting).toEqual(CANCELLED);
    unmounted();
    expect(await gone).toBe(true);
    // Nothing was added for it, the agent was asked nothing, and the guest, holding nothing, goes.
    await until(() => asked.some((entry) => (entry as string[])[0] === "gone"));
    expect(asked).toEqual([["mount", KEY, "r1", "r2"], ["unmount", KEY], ["gone"]]);
    await manager.stop();
  });

  it("is never answered from another folder's history: a key the guest holds for one folder takes no request for another", async () => {
    const asked: unknown[] = [];
    const manager = new VmManager(options(), guest(asked, async (key, request) => {
      asked.push(["history", key, request.action]);
      return { ok: { paths: ["Report.docx"] } };
    }));
    expect(await manager.history(ask("changed"), signal())).toEqual({ ok: { paths: ["Report.docx"] } });
    for (const name of ["other store", "Other"]) mkdirSync(join(dir, name));
    const others: Place[] = [
      { ...place(), real: { path: join(dir, "Other"), ...statSync(join(dir, "Other")) } },
      { ...place(), history: join(dir, "other store") },
    ];
    for (const other of others) {
      expect(await manager.history({ ...ask("changed"), place: other }, signal())).toEqual({
        error: { type: "unavailable", message: "This computer's sandbox could not add this folder's history: its key is another folder's place in the sandbox" },
      });
    }
    expect(asked).toEqual([["mount", KEY, "r1", "r2"], ["history", KEY, "changed"]]);
    // Nor from a history set aside under the key: the one at its path now is another, and the place must be let go first.
    renameSync(join(dir, "store"), join(dir, "store.was"));
    mkdirSync(join(dir, "store"));
    expect(await manager.history(ask("changed"), signal())).toEqual({
      error: { type: "unavailable", message: "This computer's sandbox could not add this folder's history: it was moved while the sandbox holds it, and must be let go first" },
    });
    expect(asked).toHaveLength(2);
    expect(await manager.unplace(place())).toBe(true);
    expect(await manager.history(ask("changed"), signal())).toEqual({ ok: { paths: ["Report.docx"] } });
    // Let go, it is added anew from the folder at its path, in whichever guest runs by then.
    expect(asked.slice(2).map((entry) => (entry as string[])[0]).filter((what) => what !== "gone")).toEqual(["unmount", "mount", "history"]);
    await manager.stop();
  });

  it("answers a request the guest stopped under as stopped by the sandbox, and one asked of a manager that is stopping", async () => {
    const asked: unknown[] = [];
    const { release, history } = held(asked);
    const manager = new VmManager(options(), guest(asked, history));
    const running = manager.history(ask("changed"), signal());
    await until(() => release.length === 1);
    const stopped = manager.stop();
    expect(await running).toEqual(SANDBOX_STOPPED);
    await stopped;
    expect(await manager.history(ask("changed"), signal())).toEqual({ error: { type: "unavailable", message: "This computer's sandbox is stopping" } });
  });
});
