import { existsSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { CANCELLED } from "../src/guest/command.js";
import { Control, type ControlPlaces } from "../src/guest/control.js";
import { type Asked, askHistory, Places } from "../src/guest/places.js";
import type { FromAgent, Share } from "../src/guest/protocol.js";
import type { Outcome } from "../src/link/protocol.js";
import { checked, type HistoryRequest, named } from "../src/vm/history.js";

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
    // Another place's key is no way to this one's history.
    expect(await mounted.history(OTHER_KEY, request, signal())).toEqual(NOT_HERE);
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
    expect(await Promise.all(all)).toEqual([NO_ANSWER, NOT_HERE, NO_ANSWER]);
    expect(await mounted.history(KEY, { ...request, args: { n: 3 } }, signal())).toEqual(NO_ANSWER);
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
    expect(await first).toEqual(NO_ANSWER);
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
      { action: undefined }, { action: 7 }, { args: null }, { args: [] }, { args: "{}" }, { args: undefined },
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
    // What only this computer says, a cancel or a sandbox that stopped, or a type nobody has, is none of the guest's to say.
    for (const type of ["cancelled", "interrupted", "ok", "History", "", "x".repeat(100_000)]) {
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
});
