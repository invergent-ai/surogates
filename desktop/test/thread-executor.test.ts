// The app's executor for a project thread's root (spec, Section 13, "The user's computer"): its file kinds in a
// host on its copy, which the executor's copies have the guest make first, and its commands in the guest, which
// shares the copy at the folder's path. The guest and the file hosts are stand-ins here: what they do with what
// they are given is theirs to test.

import { existsSync, mkdirSync, mkdtempSync, readdirSync, realpathSync, renameSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { Binder } from "../src/binding/binder.js";
import { BOOT_ID } from "../src/binding/folder.js";
import { downloadSaver } from "../src/browser/downloads.js";
import { Browsing } from "../src/browser/executor.js";
import { kinds } from "../src/files/operations.js";
import type { Making } from "../src/history/copies.js";
import { NOT_A_CHECKPOINT, NOT_A_FORGETTING_ASKED, NOT_A_THREAD, NOT_ITS_TURN, THREAD_KINDS } from "../src/history/kinds.js";
import { keyOf } from "../src/history/place.js";
import { FOLDER_UNAVAILABLE, type FromHost, type HostStart, type ToHost } from "../src/hosts/messages.js";
import { type BoundFolder, CANCELLED, type HostProcess, type Recovery } from "../src/hosts/tool-hosts.js";
import type { Binding } from "../src/journal/bindings.js";
import { OperationJournal } from "../src/journal/journal.js";
import type { Operation, Outcome } from "../src/link/protocol.js";
import { PROCESS_KINDS, VmExecutor, type VmExecutorOptions } from "../src/vm/executor.js";
import type { HistoryRequest } from "../src/vm/history.js";
import type { Place, VmOperation } from "../src/vm/manager.js";

const THREAD = "0b6c1d3e-6f0a-4c1e-9a52-6a1d2c3b4e5f";
const OTHER = "3c4d5e6f-7a8b-4c9d-8e0f-1a2b3c4d5e6f";
const CHAT = "7d8e9f00-1a2b-4c3d-8e4f-5a6b7c8d9e0f";
const HELPER = "5f6a7b8c-9d0e-4f1a-8b2c-3d4e5f6a7b8c";
const USER = "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d";
const DATA = Buffer.from("the copy's\n").toString("base64");
const signal = () => new AbortController().signal;
async function until(check: () => boolean, ms = 5_000): Promise<void> {
  for (const end = Date.now() + ms; !check(); await new Promise((resolve) => setTimeout(resolve, 10))) {
    if (Date.now() > end) throw new Error("timed out");
  }
}

let dir: string;
let dataDir: string;
let folder: string;
let executor: VmExecutor;
let performed: VmOperation[];
let asked: HistoryRequest[];
let starts: HostStart[];
// Each host's operations, by the folder its start named.
let ops: Array<{ held: string; kind: string; args: Record<string, unknown> }>;
let stops: string[];
let made: Making[];
let unplaced: Place[];
// The roots bound here: each a thread on its folder, or a chat.
let bindings: Map<string, BoundFolder>;
// What the guest's history answers a request but a copy's open, where a test says; else as a whole history would.
let refuses: ((request: HistoryRequest) => Outcome | undefined | Promise<Outcome | undefined>) | undefined;
// Whether the guest's history makes a copy again at each open, as for a repository it found cut short.
let remakes: boolean;
// The landing's steps a landing's helper holds its answer to, by their action, until the test lets them go.
let holding: Set<string>;
let unanswered: Array<() => void>;
// What a host started to put back what a landing cut short says at its start in place of ready, where a test says.
let recoveryStarts: Extract<FromHost, { type: "failed" }> | null;

// A step's record, as a landing's helper writes it before the step changes anything (files/land.ts).
const record = (path: string) => JSON.stringify({ path, was: null, wrote: null, mode: null, made: [], above: [], temp: null, aside: null, moved: false, out: null, back: null, copied: 0 });

// A file host that says it is ready, answers each operation as a file helper on an empty folder would, and lets
// a command run. A landing's answers its steps as the land kind does, keeping a record of each apply until it is put
// back or its landing forgotten.
function host(): HostProcess {
  const heard: Array<(message: FromHost) => void> = [];
  const exits: Array<() => void> = [];
  let held = "";
  let kept = "";
  const answers: Record<string, (args: Record<string, unknown>) => unknown> = {
    resolve: (args) => args.path, stat: () => null, write: () => null, read: () => DATA,
  };
  const land = (args: Record<string, unknown>): unknown => {
    const { action, saga, step, path, before, after } = args;
    const steps = join(kept, String(saga));
    if (action === "recover") return { restored: [], beside: [], lost: [], unread: [] };
    if (action === "revisions") return { revisions: (args.paths as string[]).map((one) => [one, "absent"]) };
    if (action === "apply") {
      mkdirSync(steps, { recursive: true });
      writeFileSync(join(steps, `${String(step)}.json`), record(String(path)));
      return { path, before, after, made: [] };
    }
    if (action === "unapply") {
      rmSync(join(steps, `${String(step)}.json`), { force: true });
      return { path, put_back: true };
    }
    rmSync(steps, { recursive: true, force: true });
    return {};
  };
  return {
    send: (message: ToHost) => queueMicrotask(() => {
      const say = (outcome: Outcome) => "id" in message && heard.forEach((listener) => listener({ type: "result", id: message.id, outcome }));
      if (message.type === "start") {
        starts.push(message);
        held = message.folder;
        kept = message.landing?.kept ?? message.recovery?.kept ?? "";
        const failing = message.recovery ? recoveryStarts : null;
        heard.forEach((listener) => listener(failing ?? { type: "ready", processes: [] }));
        if (failing) exits.splice(0).forEach((listener) => listener());
      } else if (message.type === "op") {
        ops.push({ held, kind: message.kind, args: message.args });
        const answer = () => say({ ok: message.kind === "land" && kept ? land(message.args) : (answers[message.kind] ?? (() => ({ did: message.kind })))(message.args) });
        if (holding.has(String(message.args.action))) unanswered.push(answer);
        else answer();
      } else if (message.type === "refusal") say({ ok: null });
      else if (message.type === "after") say(message.outcome);
      else if (message.type === "stop") {
        stops.push(held);
        exits.splice(0).forEach((listener) => listener());
      }
    }),
    onMessage: (listener) => void heard.push(listener),
    onExit: (listener) => void exits.push(listener),
    kill: () => exits.splice(0).forEach((listener) => listener()),
  };
}

const bind = (root: string, thread: boolean, at = folder) => {
  const { dev, ino } = statSync(at);
  bindings.set(root, { folder: at, dev, ino, boot: BOOT_ID, ...(thread ? { history: root } : {}) });
};
const op = (kind: string, args: Record<string, unknown>, root = THREAD): Operation => ({
  id: `${kind}-${Math.random()}`, sessionId: root, callingSessionId: root, invocationId: "17", ordinal: 1, kind, args, digest: "d",
});
// Where the executor's copies keep a thread's copy of the folder: the app's data by its real path.
const copyOf = (root: string, at = folder) => join(realpathSync(dataDir), "history", keyOf(at), "threads", root);
const identity = (path: string) => (({ dev, ino }) => ({ dev, ino, boot: BOOT_ID }))(statSync(path));

beforeEach(() => {
  dir = realpathSync(mkdtempSync(join(tmpdir(), "thread-executor-")));
  dataDir = join(dir, "data");
  folder = join(dir, "Reports");
  mkdirSync(folder);
  performed = [];
  asked = [];
  starts = [];
  ops = [];
  stops = [];
  made = [];
  unplaced = [];
  refuses = undefined;
  remakes = false;
  holding = new Set();
  unanswered = [];
  bindings = new Map();
  recoveryStarts = null;
  bind(THREAD, true);
  bind(CHAT, false);
  executor = executorOf();
});

// The app's executor, on the stand-ins above, with *more* of its options.
function executorOf(more: Partial<VmExecutorOptions> = {}): VmExecutor {
  return new VmExecutor({
    bindingOf: (root) => bindings.get(root), dataDir, cacheDir: join(dir, "cache", "surogate"), env: { HOME: dir, LANG: "C.UTF-8" },
    user: USER, spawnHost: host, making: (event) => void made.push(event),
    vm: {
      perform: async (operation) => {
        performed.push(operation);
        return { ok: true };
      },
      teardown: async () => {},
      onProcesses: () => () => {},
      onAsk: () => () => {},
      // The guest's history: it makes a copy's folder at its open, as git does, and answers each step as a whole one would.
      history: async (request): Promise<Outcome> => {
        asked.push(request);
        if (request.action === "open") {
          const copy = join(request.place.history, "threads", request.thread);
          if (remakes) {
            rmSync(copy, { recursive: true, force: true });
            // Made first, so the copy's new folder is another than the one removed.
            mkdirSync(join(dir, `held-${asked.length}`));
          }
          mkdirSync(copy, { recursive: true });
          return { ok: { copy: "made" } };
        }
        const refused = await refuses?.(request);
        if (refused) return refused;
        return { ok: request.action === "snapshot" ? { hash: "a".repeat(40) } : request.action === "forget" ? { landing: null } : {} };
      },
      unplace: async (place) => {
        unplaced.push(place);
        return true;
      },
    },
    ...more,
  });
}

afterEach(async () => {
  await executor.stop();
  rmSync(dir, { recursive: true, force: true });
});

describe("a thread's operations, through the app's executor", { timeout: 10_000 }, () => {
  it("share the thread's copy at the folder's path in the guest, where a chat's share its folder", async () => {
    expect(await executor.run(op("which", { name: "git" }), signal())).toEqual({ ok: true });
    expect(await executor.run(op("which", { name: "git" }, CHAT), signal())).toEqual({ ok: true });
    // The copy the guest made, in the folder's place in the app's data, named by the thread.
    const copy = copyOf(THREAD);
    expect(asked.map((request) => [request.place.key, request.place.history, request.thread, request.user, request.action, request.args])).toEqual([
      [keyOf(folder), join(realpathSync(dataDir), "history", keyOf(folder)), THREAD, USER, "open", { moves: false }],
    ]);
    expect(performed.map(({ root, folder: shared, at }) => ({ root, shared, at }))).toEqual([
      { root: THREAD, shared: { path: copy, ...identity(copy) }, at: folder },
      { root: CHAT, shared: { path: folder, ...identity(folder) }, at: undefined },
    ]);
    // Its file host is on the copy, asked by the folder's path, byte for byte; the chat's on the folder.
    expect(starts.map(({ folder: held, expect: was, at }) => ({ held, was, at }))).toEqual([
      { held: copy, was: identity(copy), at: folder },
      { held: folder, was: identity(folder), at: undefined },
    ]);
  });

  it("go each to the thread's copy, every kind of it, and none to the folder itself or to a share of it", async () => {
    // Every kind an operation can have but a browser's and a thread's own: the file helper's, one that is none, and the commands'.
    const all = [...kinds().filter((kind) => !THREAD_KINDS.has(kind)), "no-such-kind", ...PROCESS_KINDS];
    for (const kind of all) expect(await executor.run(op(kind, { key: join(folder, "a.txt"), name: "git", command: "true" }), signal()), kind).toHaveProperty("ok");
    const copy = copyOf(THREAD);
    expect(starts.map((start) => start.folder)).toEqual([copy]);
    expect(ops.map((done) => [done.held, done.kind])).toEqual(all.filter((kind) => !PROCESS_KINDS.has(kind)).map((kind) => [copy, kind]));
    expect(performed.map((done) => [done.folder.path, done.at, done.kind])).toEqual([...PROCESS_KINDS].map((kind) => [copy, folder, kind]));
  });

  it("give a page a file of the thread's copy, and save what a page downloads in it, by the folder's names", async () => {
    const browsed: Operation[] = [];
    const browsing = new Browsing({
      tools: executor,
      browser: {
        perform: async (_launch, operation) => {
          browsed.push(operation);
          return { ok: null };
        },
        forget: () => {}, stop: async () => {}, end: async () => {}, address: async () => "https://example.com/", notComing: () => {},
        pause: () => {}, show: async () => true, onDownload: () => {}, forwards: () => {},
      },
      bindingOf: (root) => bindings.get(root), launch: () => ({ executable: "/usr/bin/true", profile: join(dir, "profile") }),
      staging: join(dir, "staging"), ports: () => [], vm: { forwards: () => {}, listening: async () => false, door: join(dir, "door") },
    });
    expect(await browsing.run(op("browser.navigate", { url: "https://example.com/" }), signal())).toEqual({ ok: null });
    expect(await browsing.run(op("browser.set_input_files", { paths: [join(folder, "a.txt")] }), signal())).toEqual({ ok: null });
    expect(browsed.map((operation) => [operation.kind, operation.args])).toEqual([
      ["browser.navigate", { url: "https://example.com/" }],
      ["browser.set_input_files", { files: [{ name: "a.txt", mimeType: "text/plain", buffer: DATA }] }],
    ]);
    mkdirSync(join(dir, "staging"));
    const staged = join(dir, "staging", "page.txt");
    writeFileSync(staged, "from a page\n");
    const save = downloadSaver({ get: (root) => bindings.get(root) as Binding | undefined }, { admit: async () => null, run: (operation, stop) => browsing.run(operation, stop) });
    expect(await save({ root: THREAD, session: THREAD, name: "page.txt", path: staged, user: false })).toMatch(/It is saved in the chat's folder as Downloads\/page\.txt\.$/);
    const copy = copyOf(THREAD);
    expect(ops.map((done) => [done.held, done.kind, done.args.key ?? done.args.path])).toEqual([
      [copy, "read", join(folder, "a.txt")],
      [copy, "resolve", join(folder, "Downloads")], [copy, "stat", join(folder, "Downloads")],
      [copy, "resolve", join(folder, "Downloads", "page.txt")], [copy, "stat", join(folder, "Downloads", "page.txt")], [copy, "write", join(folder, "Downloads", "page.txt")],
    ]);
    expect([starts.map((start) => start.folder), performed]).toEqual([[copy], []]);
  });

  it("tell the binder that a thread works in a copy here, through the browser's layer above them too", () => {
    const browsing = new Browsing({
      tools: executor,
      browser: {
        perform: async () => ({ ok: null }), forget: () => {}, stop: async () => {}, end: async () => {}, address: async () => "https://example.com/",
        notComing: () => {}, pause: () => {}, show: async () => true, onDownload: () => {}, forwards: () => {},
      },
      bindingOf: (root) => bindings.get(root), launch: () => null, staging: join(dir, "staging"), ports: () => [],
      vm: { forwards: () => {}, listening: async () => false, door: join(dir, "door") },
    });
    expect([executor.keepsCopies(), browsing.keepsCopies()]).toEqual([true, true]);
  });

  it("say when the guest is asked to make a thread's copy, and when that ends, and nothing for a copy known whole", async () => {
    await executor.run(op("resolve", { path: "" }), signal());
    await executor.run(op("which", { name: "git" }), signal());
    expect(made).toEqual([{ root: THREAD, folder, state: "begun" }, { root: THREAD, folder, state: "ended" }]);
  });

  it("stop the host on a thread's copy once another folder at the folder's path takes its place, and the thread works nowhere after", async () => {
    expect(await executor.run(op("read", { key: join(folder, "a.txt"), max_bytes: null }), signal())).toEqual({ ok: DATA });
    const old = copyOf(THREAD);
    const before = asked[0]?.place;
    // Another folder at the path, and a thread bound to it.
    renameSync(folder, `${folder}.old`);
    mkdirSync(folder);
    bind(OTHER, true);
    expect(await executor.run(op("read", { key: join(folder, "a.txt"), max_bytes: null }, OTHER), signal())).toEqual({ ok: DATA });
    // The host on the old folder's copy stopped, the guest let go of the old place, and the new thread's host is on a copy of its own.
    expect(stops).toEqual([old]);
    expect(unplaced).toEqual([before]);
    expect(starts.map((start) => start.folder)).toEqual([old, copyOf(OTHER)]);
    expect(await executor.run(op("read", { key: join(folder, "a.txt"), max_bytes: null }), signal())).toEqual(FOLDER_UNAVAILABLE);
    expect(starts).toHaveLength(2);
  });
});

// The kinds only a project's thread has (history/kinds.ts): each the worker's own, under an invocation of its own,
// never a tool call's. A snapshot is any session's of the thread, as each takes one before its step; a step of its
// history or of its landing is the thread's alone.
describe("a thread's own kinds, through the app's executor", { timeout: 10_000 }, () => {
  const HASH = "b".repeat(40);
  const [A, B] = ["c".repeat(40), "d".repeat(40)];
  const SNAPSHOT = { ok: { hash: "a".repeat(40) } };
  const own = (kind: string, invocationId: string) => (args: Record<string, unknown>, more: Partial<Operation> = {}): Operation => ({
    ...op(kind, args), invocationId, ...more,
  });
  const checkpoint = own("checkpoint", "checkpoint:turn-1:3:call-7");
  const history = own("history", "land:turn-1");
  const land = own("land", "land:turn-1");
  const said = () => asked.map((request) => [request.thread, request.action, request.args]);
  // Where the folder's landings keep what they replace, in the app's data.
  const kept = (...inside: string[]) => join(realpathSync(dataDir), "landings", keyOf(folder), ...inside);
  const apply = (step: number, path: string, before: string | null, after: string | null) => land({ action: "apply", saga: "saga-1", step, path, before, after, expected: "absent" });
  const forgets = () => ops.filter((done) => done.kind === "land" && done.args.action === "forget");

  it("take a snapshot of the thread's copy before a step, from any session of the thread's, and put the copy back to one, by the history's own names", async () => {
    expect(await executor.run(checkpoint({ action: "take", reason: "before write_file" }), signal())).toEqual(SNAPSHOT);
    // A session under the thread works in the thread's copy: its step's snapshot is the thread's copy's.
    expect(await executor.run(checkpoint({ action: "take", reason: "before terminal" }, { callingSessionId: HELPER }), signal())).toEqual(SNAPSHOT);
    expect(await executor.run(checkpoint({ action: "restore", hash: HASH }, { invocationId: `checkpoint:turn-1:restore:${HASH}` }), signal())).toEqual({ ok: {} });
    expect(said()).toEqual([
      [THREAD, "open", { moves: false }], [THREAD, "snapshot", { reason: "before write_file" }],
      [THREAD, "snapshot", { reason: "before terminal" }], [THREAD, "restore", { commit: HASH }],
    ]);
    // No file host is needed for either.
    expect(starts).toEqual([]);
  });

  it("pass each step of the thread's history to git in the guest for its own copy, with its arguments as they came", async () => {
    const author = { name: "Draft A", email: `thread:${THREAD}@surogate` };
    const trailers = [["Surogate-Saga", "saga-1"]];
    expect(await executor.run(history({ action: "open" }, { invocationId: "open:turn-1" }), signal())).toEqual({ ok: { copy: "made" } });
    const steps: Array<[string, Record<string, unknown>]> = [
      ["changed", {}], ["fetch", { commits: [HASH], saga: "saga-1", since: null }], ["pickup", { author, trailers }],
      ["commit", { author, trailers, pickup: null }], ["record", { turn: HASH, applied: [], author, trailers, main: HASH, pickup: null, left: [] }],
      ["keep", { author, trailers, base: true }], ["forget", { saga: "saga-1", applied: [] }],
    ];
    for (const [action, args] of steps) {
      expect(await executor.run(history({ action, ...args }), signal()), action).toEqual({ ok: action === "forget" ? { landing: null } : {} });
    }
    // A turn's open under its landing's invocation too.
    expect(await executor.run(history({ action: "open" }), signal())).toEqual({ ok: { copy: "made" } });
    expect(said()).toEqual([[THREAD, "open", {}], ...steps.map(([action, args]) => [THREAD, action, args]), [THREAD, "open", {}]]);
    expect(asked.every((request) => request.user === USER && request.place.real.path === folder)).toBe(true);
    expect(starts).toEqual([]);
  });

  it("answer what the history refused as it came, with the code the worker goes by; only a step refused for want of a whole copy is asked again, once", async () => {
    const unnamed = { error: { type: "history", code: "name_not_utf8", message: "refused the request: a file's name is not UTF-8, which history cannot record" } };
    refuses = () => unnamed;
    expect(await executor.run(history({ action: "changed" }), signal())).toEqual(unnamed);
    expect(await executor.run(checkpoint({ action: "take", reason: "before write_file" }), signal())).toEqual(unnamed);
    // A snapshot taken on another base than the copy's is not one it is put back to.
    const elsewhere = { error: { type: "history", code: "not_on_base", message: "refused the request: the snapshot is not on this copy's base" } };
    refuses = () => elsewhere;
    expect(await executor.run(checkpoint({ action: "restore", hash: HASH }), signal())).toEqual(elsewhere);
    refuses = () => ({ error: { type: "history", code: "no_whole_copy", message: "refused the request: this thread has no whole copy" } });
    expect(await executor.run(history({ action: "changed" }), signal())).toMatchObject({ error: { type: "history", code: "no_whole_copy" } });
    expect(said().map(([, action]) => action)).toEqual(["open", "changed", "snapshot", "restore", "changed", "open", "changed"]);
  });

  it("stop the thread's hosts on its copy where a turn's open made the copy again, and its next operation works in the copy that is there", async () => {
    await executor.run(op("resolve", { path: "" }), signal());
    const first = statSync(copyOf(THREAD)).ino;
    remakes = true;
    expect(await executor.run(history({ action: "open" }, { invocationId: "open:turn-2" }), signal())).toEqual({ ok: { copy: "made" } });
    expect(statSync(copyOf(THREAD)).ino).not.toBe(first);
    await until(() => stops.length === 1);
    expect(await executor.run(op("resolve", { path: "" }), signal())).toEqual({ ok: "" });
    expect(starts.map((start) => [start.folder, start.expect.ino])).toEqual([[copyOf(THREAD), first], [copyOf(THREAD), statSync(copyOf(THREAD)).ino]]);
    // The copy the guest left is taken as it is: nothing more is asked of it.
    expect(said()).toEqual([[THREAD, "open", { moves: false }], [THREAD, "open", {}]]);
  });

  it("land through a host of the landing's own on the folder itself, given the thread's copy to read, and never through the host on the copy", async () => {
    await executor.run(op("resolve", { path: "" }), signal());
    expect(await executor.run(land({ action: "revisions", paths: ["a.txt"] }), signal())).toEqual({ ok: { revisions: [["a.txt", "absent"]] } });
    const copy = copyOf(THREAD);
    expect(starts.map((start) => [start.folder, start.at, start.landing])).toEqual([[copy, folder, undefined], [folder, undefined, { copy, kept: kept() }]]);
    expect(ops.map((done) => [done.held, done.kind])).toEqual([[copy, "resolve"], [folder, "land"]]);
  });

  it("forget what a landing kept only once the folder's history says it may go: asked first, for every apply that was sent, then the helper", async () => {
    const applies = [{ step: 0, path: "a.txt", before: A, after: B }, { step: 2, path: "b/c.txt", before: null, after: B }];
    for (const { step, path, before, after } of applies) expect(await executor.run(apply(step, path, before, after), signal())).toMatchObject({ ok: { path } });
    expect(readdirSync(kept("saga-1")).sort()).toEqual(["0.json", "2.json"]);
    // When the history is asked, the helper has forgotten nothing yet.
    const keptWhenAsked: boolean[] = [];
    refuses = (request) => void (request.action === "forget" && keptWhenAsked.push(existsSync(kept("saga-1"))));
    // A change with no version on either side was no apply's, and may be named all the same.
    const applied = [...applies, { step: 1, path: "gone.md", before: null, after: null }];
    expect(await executor.run(land({ action: "forget", saga: "saga-1", applied }), signal())).toEqual({ ok: {} });
    expect([said().at(-1), keptWhenAsked]).toEqual([[THREAD, "forget", { saga: "saga-1", applied }], [true]]);
    expect([forgets().length, existsSync(kept("saga-1"))]).toEqual([1, false]);
    // The landing over, its host lets the folder go.
    await until(() => stops.includes(folder));
  });

  it.each([
    ["refuses it, the landing neither recorded nor put back whole", { error: { type: "history", code: "landing_unsettled", message: "refused the request: this landing was neither recorded nor put back whole" } }, null],
    ["did not answer", { error: { type: "history", code: "no_answer", message: "the history ended without an answer" } }, null],
    ["answers what is no forgetting's", { ok: { landing: null, main: null } }, {
      error: { type: "value", message: "This is no answer of a folder's history to forgetting a landing, so what the landing kept was not forgotten" },
    }],
  ])("forget nothing where the folder's history %s, and leave the landing's host to its idle time", async (_, answer: Outcome, said: Outcome | null) => {
    await executor.run(apply(0, "a.txt", A, B), signal());
    refuses = (request) => (request.action === "forget" ? answer : undefined);
    expect(await executor.run(land({ action: "forget", saga: "saga-1", applied: [{ step: 0, path: "a.txt", before: A, after: B }] }), signal())).toEqual(said ?? answer);
    expect([readdirSync(kept("saga-1")), forgets()]).toEqual([["0.json"], []]);
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(stops).toEqual([]);
  });

  it.each([
    ["leaves out a step its helper holds a record of", [{ step: 0, path: "a.txt", before: A, after: B }], 2, "b.txt"],
    ["names a step for another file than the one its record names", [{ step: 0, path: "a.txt", before: A, after: B }, { step: 2, path: "c.txt", before: null, after: B }], 2, "b.txt"],
    ["leaves out a step whose record cannot be read", [{ step: 0, path: "a.txt", before: A, after: B }, { step: 2, path: "b.txt", before: null, after: B }], 3, null],
  ])("forget nothing where the forgetting %s, and say which step", async (_, applied, step: number, path: string | null) => {
    await executor.run(apply(0, "a.txt", A, B), signal());
    await executor.run(apply(2, "b.txt", null, B), signal());
    writeFileSync(kept("saga-1", "3.json"), "not a record");
    expect(await executor.run(land({ action: "forget", saga: "saga-1", applied }), signal())).toEqual({
      error: {
        type: "conflict",
        message: `Step ${step} of this landing${path === null ? "" : `, of ${path},`} was applied on this computer, and the steps named to forget the landing leave it out, so nothing the landing kept was forgotten`,
      },
    });
    expect([readdirSync(kept("saga-1")).sort(), forgets()]).toEqual([["0.json", "2.json", "3.json"], []]);
    // Named, the record that cannot be read is the helper's to refuse.
    const all = [...applied.filter((one) => one.step !== 2), { step: 2, path: "b.txt", before: null, after: B }, { step: 3, path: "d.txt", before: null, after: B }];
    expect(await executor.run(land({ action: "forget", saga: "saga-1", applied: all }), signal())).toEqual({ ok: {} });
    expect(forgets()).toHaveLength(1);
  });

  it.each([
    ["no saga", { applied: [] }],
    ["a saga no landing is named", { saga: "../saga-1", applied: [] }],
    ["no applies", { saga: "saga-1" }],
    ["an apply with no step", { saga: "saga-1", applied: [{ path: "a.txt", before: A, after: B }] }],
    ["a step twice", { saga: "saga-1", applied: [{ step: 0, path: "a.txt", before: A, after: B }, { step: 0, path: "b.txt", before: A, after: B }] }],
    ["a step that is no number of one", { saga: "saga-1", applied: [{ step: -1, path: "a.txt", before: A, after: B }] }],
    ["a file's version as git names none", { saga: "saga-1", applied: [{ step: 0, path: "a.txt", before: "HEAD", after: B }] }],
    ["an apply of no file", { saga: "saga-1", applied: [{ step: 0, path: "", before: A, after: B }] }],
  ])("refuse in words a landing's forgetting that names %s, and ask nothing of anyone", async (_, args: Record<string, unknown>) => {
    expect(await executor.run(land({ action: "forget", ...args }), signal())).toEqual(NOT_A_FORGETTING_ASKED);
    expect([asked, starts]).toEqual([[], []]);
  });

  it("give a hold of the folder back through the history's forgetting where the thread holds it, and answer one it does not hold at once, asking nothing", async () => {
    const release = land({ action: "forget", saga: "hold:41", applied: [] }, { invocationId: "land:41:release:9" });
    expect(await executor.run(release, signal())).toEqual({ ok: {} });
    expect([asked, starts]).toEqual([[], []]);
    expect(await executor.run(land({ action: "recover" }, { invocationId: "land:41:hold" }), signal())).toEqual({ ok: { restored: [], beside: [], lost: [], unread: [] } });
    expect(await executor.run({ ...release, id: "release-again" }, signal())).toEqual({ ok: {} });
    expect([said().at(-1), forgets().length]).toEqual([[THREAD, "forget", { saga: "hold:41", applied: [] }], 1]);
    await until(() => stops.includes(folder));
  });

  it("ask the history to forget a landing only once every step sent before has been answered, and send none between its answer and the helper's forgetting", async () => {
    holding.add("apply");
    const cancel = new AbortController();
    const applying = executor.run(apply(0, "a.txt", A, B), cancel.signal);
    await until(() => unanswered.length === 1);
    // Its caller is answered at once; the helper runs the step all the same.
    cancel.abort();
    expect(await applying).toEqual(CANCELLED);
    let answer: (outcome: Outcome | undefined) => void = () => {};
    refuses = (request) => (request.action === "forget" ? new Promise((resolve) => void (answer = resolve)) : undefined);
    const forgetting = executor.run(land({ action: "forget", saga: "saga-1", applied: [{ step: 0, path: "a.txt", before: A, after: B }] }), signal());
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(said().filter(([, action]) => action === "forget")).toEqual([]);
    // The step answered, its record kept: the history is asked now, and the landing's next step waits for the forgetting.
    unanswered.shift()?.();
    await until(() => said().some(([, action]) => action === "forget"));
    const looking = executor.run(land({ action: "revisions", paths: ["a.txt"] }), signal());
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(ops.filter((done) => done.kind === "land").map((done) => done.args.action)).toEqual(["apply"]);
    answer(undefined);
    expect(await forgetting).toEqual({ ok: {} });
    expect(ops.filter((done) => done.kind === "land").map((done) => done.args.action)).toEqual(["apply", "forget"]);
    // Forgotten, the landing is over: its host begins no step after, and the next is a new landing's.
    expect(await looking).toMatchObject({ error: { type: "unavailable" } });
    expect(await executor.run(land({ action: "revisions", paths: ["a.txt"] }), signal())).toEqual({ ok: { revisions: [["a.txt", "absent"]] } });
    expect(starts.filter((start) => start.landing)).toHaveLength(2);
  });

  it("are refused, before anything is asked, for a chat that works in its folder itself, and for a root bound nowhere here", async () => {
    const chat = { sessionId: CHAT, callingSessionId: CHAT };
    for (const operation of [
      checkpoint({ action: "take", reason: "x" }, chat), checkpoint({ action: "restore", hash: HASH }, chat), history({ action: "pickup", author: {}, trailers: [] }, chat),
      history({ action: "forget", saga: "s", applied: [] }, chat), land({ action: "apply", saga: "s", step: 0, path: "a.txt" }, chat), land({ action: "recover" }, chat),
    ]) {
      expect(executor.refusal(operation), operation.kind).toEqual(NOT_A_THREAD);
      expect(await executor.run(operation, signal()), operation.kind).toEqual(NOT_A_THREAD);
    }
    const nowhere = land({ action: "recover" }, { sessionId: OTHER, callingSessionId: OTHER });
    expect([executor.refusal(nowhere), await executor.run(nowhere, signal())]).toEqual([FOLDER_UNAVAILABLE, FOLDER_UNAVAILABLE]);
    expect([asked, starts]).toEqual([[], []]);
    // What every chat has is asked about as before.
    expect(executor.refusal(op("write", { key: join(folder, "a.txt"), data: "" }, CHAT))).toBeNull();
    expect(executor.refusal(op("write", { key: join(folder, "a.txt"), data: "" }))).toBeNull();
  });

  it.each([
    ["a step of the history under a tool call's invocation", () => history({ action: "record" }, { invocationId: "17" })],
    ["a step of the history under the user's own request", () => history({ action: "record" }, { invocationId: "request:5c" })],
    ["a step of the history asked by a session under the thread", () => history({ action: "commit" }, { callingSessionId: HELPER })],
    ["the history's forgetting asked by a session under the thread", () => history({ action: "forget", saga: "s", applied: [] }, { callingSessionId: HELPER })],
    ["a landing's write asked by a session under the thread", () => land({ action: "apply", saga: "s", step: 0, path: "a.txt" }, { callingSessionId: HELPER })],
    ["a landing's put-back of what was cut, asked by a session under the thread", () => land({ action: "recover" }, { callingSessionId: HELPER })],
    ["a landing's write under a tool call's invocation", () => land({ action: "apply", saga: "s", step: 0, path: "a.txt" }, { invocationId: "17" })],
    ["a landing's forgetting under a checkpoint's invocation", () => land({ action: "forget", saga: "s", applied: [] }, { invocationId: "checkpoint:turn-1:3:call-7" })],
    ["the history's forgetting under a checkpoint's invocation", () => history({ action: "forget", saga: "s", applied: [] }, { invocationId: "checkpoint:turn-1:3:call-7" })],
    ["a landing's step under a name that only holds a landing's", () => land({ action: "recover" }, { invocationId: "x:land:turn-1" })],
    ["a snapshot under a tool call's invocation", () => checkpoint({ action: "take", reason: "x" }, { invocationId: "17" })],
    ["a put-back under a landing's invocation", () => checkpoint({ action: "restore", hash: HASH }, { invocationId: "land:turn-1" })],
    ["a turn's open under a checkpoint's invocation", () => history({ action: "open" }, { invocationId: "checkpoint:turn-1:3:call-7" })],
    ["a step under a turn's open", () => history({ action: "record" }, { invocationId: "open:turn-1" })],
    ["a landing's step under a turn's open", () => land({ action: "recover" }, { invocationId: "open:turn-1" })],
    ["a copy's removal, which is the app's own to ask", () => history({ action: "close" })],
    ["a thread's dropping, which is the app's own to ask", () => history({ action: "drop" })],
    ["a snapshot as a step of the history", () => history({ action: "snapshot", reason: "x" })],
    ["a pruning, which this computer's history does not take", () => history({ action: "prune" })],
    ["a step with no action", () => history({})],
    ["a step whose action is no word", () => land({ action: ["recover"] })],
    ["a file kind as a landing's", () => land({ action: "write", key: "a.txt" })],
    ["a checkpoint's listing", () => checkpoint({ action: "list" })],
    ["a step of another thread's copy", () => land({ action: "recover" }, { sessionId: OTHER, callingSessionId: OTHER })],
  ])("refuse %s, before anything is asked", async (_what, make) => {
    // A root bound to the thread's copy of another root's: none is, and none may ask for that copy's.
    bindings.set(OTHER, { ...bindings.get(THREAD)!, history: THREAD });
    const operation = make();
    expect(executor.refusal(operation)).toEqual(NOT_ITS_TURN);
    expect(await executor.run(operation, signal())).toEqual(NOT_ITS_TURN);
    expect([asked, starts]).toEqual([[], []]);
  });

  it("take a snapshot's words and a put-back's commit only as what they are, and refuse anything else in words", async () => {
    for (const args of [
      { action: "take" }, { action: "take", reason: 7 }, { action: "take", reason: "" }, { action: "take", reason: "x".repeat(201) },
      { action: "take", reason: "é".repeat(201) }, { action: "take", reason: "😀".repeat(201) }, { action: "take", reason: "a\0b" }, { action: "take", reason: "\ud800" },
      { action: "restore" }, { action: "restore", hash: "--upload-pack=/x" }, { action: "restore", hash: "b".repeat(39) }, { action: "restore", hash: "B".repeat(40) },
    ]) {
      expect(await executor.run(checkpoint(args), signal()), JSON.stringify(args)).toEqual(NOT_A_CHECKPOINT);
    }
    // Two hundred characters, as the server counts them, however many of JavaScript's units they take.
    expect(await executor.run(checkpoint({ action: "take", reason: "😀".repeat(200) }), signal())).toEqual(SNAPSHOT);
    expect(said()).toEqual([[THREAD, "open", { moves: false }], [THREAD, "snapshot", { reason: "😀".repeat(200) }]]);
  });

  it("reach the executor through the binder with no prompt in a thread that asks every time, and are refused there before any", async () => {
    const journal = new OperationJournal(join(dir, "journal.sqlite"));
    try {
      const { dev, ino } = statSync(folder);
      journal.bindings.add({ root: THREAD, nonce: "nonce", folder, dev, ino, boot: BOOT_ID, mode: "ask", boundAt: 1, history: THREAD });
      const prompts: unknown[] = [];
      const binder = new Binder({
        bindings: journal.bindings, guards: executor.guards(), agent: "the tests", hosts: executor,
        prompts: { confirmFolder: async () => null, pickFolder: async () => null },
        refusal: (operation) => executor.refusal(operation),
        approvalPrompts: {
          approve: async (request) => {
            prompts.push(request);
            return "deny";
          },
          confirmFreeMode: async () => false,
        },
      });
      for (const operation of [checkpoint({ action: "take", reason: "before write_file" }), history({ action: "commit" }), land({ action: "apply", saga: "s", step: 0, path: "a.txt" })]) {
        expect(await binder.admit(operation, signal()), operation.kind).toBeNull();
      }
      expect(await binder.admit(history({ action: "record" }, { invocationId: "17" }), signal())).toEqual(NOT_ITS_TURN);
      expect(await binder.admit(land({ action: "apply", saga: "s", step: 0, path: "a.txt" }, { callingSessionId: HELPER }), signal())).toEqual(NOT_ITS_TURN);
      expect(prompts).toEqual([]);
      // The thread's own write into its copy is asked about, as any chat's is.
      expect(await binder.admit(op("write", { key: join(folder, "a.txt"), data: "" }), signal())).toMatchObject({ error: { code: "EACCES" } });
      expect(prompts).toHaveLength(1);
    } finally {
      journal.close();
    }
  });

  it("are refused, and a deleted thread's hosts stopped, through the browser's layer above the executor too, as the app stacks them", async () => {
    const browsing = new Browsing({
      tools: executor,
      browser: {
        perform: async () => ({ ok: null }), forget: () => {}, stop: async () => {}, end: async () => {}, address: async () => "https://example.com/",
        notComing: () => {}, pause: () => {}, show: async () => true, onDownload: () => {}, forwards: () => {},
      },
      bindingOf: (root) => bindings.get(root), launch: () => null, staging: join(dir, "staging"), ports: () => [],
      vm: { forwards: () => {}, listening: async () => false, door: join(dir, "door") },
    });
    expect(browsing.refusal(land({ action: "recover" }, { sessionId: CHAT, callingSessionId: CHAT }))).toEqual(NOT_A_THREAD);
    expect(browsing.refusal(history({ action: "record" }, { invocationId: "17" }))).toEqual(NOT_ITS_TURN);
    expect(browsing.refusal(land({ action: "recover" }))).toBeNull();
    await executor.run(op("resolve", { path: "" }), signal());
    browsing.retired(THREAD);
    await until(() => stops.length === 1);
    expect(stops).toEqual([copyOf(THREAD)]);
  });

  it("stop a deleted thread's hosts on its copy and leave the copy where it is; a chat and a root bound nowhere here are left alone", async () => {
    await executor.run(op("resolve", { path: "" }), signal());
    await executor.run(op("resolve", { path: "" }, CHAT), signal());
    // As the binder retires a root: the tools are told while its binding is still there, then it is forgotten.
    executor.retired(THREAD);
    bindings.delete(THREAD);
    await until(() => stops.length === 1);
    expect([stops, existsSync(copyOf(THREAD))]).toEqual([[copyOf(THREAD)], true]);
    // Nothing is asked of the guest for its copy or its repository: they stay as the thread left them.
    expect(said()).toEqual([[THREAD, "open", { moves: false }]]);
    executor.retired(CHAT);
    executor.retired(OTHER);
    executor.retired("not a session");
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(stops).toEqual([copyOf(THREAD)]);
    expect(await executor.run(op("resolve", { path: "" }), signal())).toEqual(FOLDER_UNAVAILABLE);
  });

  it("keep a deleted thread's landing that wrote in its folder in its host, so the steps that put it back and forget it still reach it, until it is forgotten", async () => {
    await executor.run(op("resolve", { path: "" }), signal());
    const applied = { step: 0, path: "a.txt", before: A, after: B };
    await executor.run(apply(0, "a.txt", A, B), signal());
    executor.retired(THREAD);
    bindings.delete(THREAD);
    await until(() => stops.length === 1);
    expect(stops).toEqual([copyOf(THREAD)]);
    // Nothing else of the thread's is taken now: no file tool, command or snapshot.
    for (const operation of [op("resolve", { path: "" }), op("which", { name: "git" }), checkpoint({ action: "take", reason: "x" })]) {
      expect(await executor.run(operation, signal()), operation.kind).toEqual(FOLDER_UNAVAILABLE);
    }
    // The landing put back as the worker puts a deleted thread's back: the history looked at, the apply put back, the landing forgotten.
    for (const operation of [
      history({ action: "fetch", commits: [], saga: "saga-1", since: null }), land({ action: "unapply", saga: "saga-1", step: 0, path: "a.txt" }),
      land({ action: "forget", saga: "saga-1", applied: [applied] }),
    ]) {
      expect(executor.refusal(operation), String(operation.args.action)).toBeNull();
      expect(await executor.run(operation, signal()), String(operation.args.action)).toHaveProperty("ok");
    }
    await until(() => stops.includes(folder));
    // Once its landing's host has gone, nothing of the deleted thread's is taken, and no host is started for it.
    for (const operation of [land({ action: "recover" }), history({ action: "changed" })]) expect(await executor.run(operation, signal())).toEqual(FOLDER_UNAVAILABLE);
    expect([starts.length, said().map(([, action]) => action)]).toEqual([2, ["open", "fetch", "forget"]]);
  });

  it("stop a deleted thread's landing's host that wrote nothing in its folder once the step its helper runs has ended, and answer the deletion at once", async () => {
    holding.add("revisions");
    const looking = executor.run(land({ action: "revisions", paths: ["a.txt"] }), signal());
    await until(() => unanswered.length === 1);
    executor.retired(THREAD);
    bindings.delete(THREAD);
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(stops).toEqual([]);
    unanswered.shift()?.();
    expect(await looking).toEqual({ ok: { revisions: [["a.txt", "absent"]] } });
    await until(() => stops.includes(folder));
    expect(await executor.run(land({ action: "recover" }), signal())).toEqual(FOLDER_UNAVAILABLE);
  });
});

describe("what a landing cut short in a thread's folder, put back through the app's executor", { timeout: 10_000 }, () => {
  const history = (action: string, invocationId = "land:turn-1"): Operation => ({ ...op("history", { action }), invocationId });
  // What the folder's landings keep, holding a step's record as a landing's helper cut short leaves it.
  const left = () => {
    const kept = join(realpathSync(dataDir), "landings", keyOf(folder));
    mkdirSync(join(kept, "saga-1"), { recursive: true });
    writeFileSync(join(kept, "saga-1", "0.json"), record("a.txt"));
    return kept;
  };
  const recoveries = () => starts.filter((start) => start.recovery !== undefined);
  const actions = () => asked.map((request) => request.action);
  const nothing = { restored: [], beside: [], lost: [], unread: [] };

  it("put it back before a pickup or a turn's open is asked of the guest, where the folder's landings keep anything and no landing's host is on it, and once", async () => {
    // The thread's first operation makes the folder's place in the app's data, and its record.
    await executor.run(op("resolve", { path: "" }), signal());
    const kept = left();
    holding.add("recover");
    const pickup = executor.run(history("pickup"), signal());
    await until(() => ops.some((done) => done.kind === "land" && done.args.action === "recover"));
    // On the folder its place's record names, with what its landings keep; the guest is asked nothing meanwhile.
    expect(recoveries()).toEqual([expect.objectContaining({ folder, expect: identity(folder), recovery: { kept } })]);
    expect(actions()).toEqual(["open"]);
    holding.delete("recover");
    for (const answer of unanswered.splice(0)) answer();
    expect(await pickup).toEqual({ ok: {} });
    expect(actions()).toEqual(["open", "pickup"]);
    // Put back, and no landing's host on the folder since: a turn's open and the next pickup start none.
    expect(await executor.run(history("open", "open:turn-2"), signal())).toEqual({ ok: { copy: "made" } });
    expect(await executor.run(history("pickup"), signal())).toEqual({ ok: {} });
    expect(recoveries()).toHaveLength(1);
    // A landing's host there since may have been cut: the next one does.
    expect(await executor.run({ ...op("land", { action: "revisions", paths: [] }), invocationId: "land:turn-2" }, signal())).toMatchObject({ ok: {} });
    expect(await executor.run({ ...op("land", { action: "forget", saga: "saga-2", applied: [] }), invocationId: "land:turn-2" }, signal())).toMatchObject({ ok: {} });
    await until(() => stops.includes(folder));
    expect(await executor.run(history("open", "open:turn-3"), signal())).toEqual({ ok: { copy: "made" } });
    expect(recoveries()).toHaveLength(2);
  });

  it("put nothing back for them where a landing's host is on the folder: its helper's first step put back what was cut short", async () => {
    await executor.run(op("resolve", { path: "" }), signal());
    left();
    expect(await executor.run({ ...op("land", { action: "recover" }), invocationId: "land:turn-1:hold" }, signal())).toEqual({ ok: nothing });
    expect(await executor.run(history("pickup"), signal())).toEqual({ ok: {} });
    expect(recoveries()).toEqual([]);
    // Nor where the folder's landings keep nothing.
    rmSync(join(realpathSync(dataDir), "landings"), { recursive: true });
    expect(await executor.run(history("open", "open:turn-2"), signal())).toEqual({ ok: { copy: "made" } });
    expect(recoveries()).toEqual([]);
  });

  it("answer a pickup and a turn's open busy, and ask the guest nothing, where another holds the folder for as long as the recovery waits for it", async () => {
    await executor.run(op("resolve", { path: "" }), signal());
    left();
    recoveryStarts = { type: "failed", message: "another chat on this computer is working in this folder; this one can use it once that one is done", busy: true };
    const busy = {
      error: { type: "busy", message: `Another chat on this computer is working in ${folder}, and what a landing cut short there is not put back yet, so this was not done. Ask again once that one is done` },
    };
    expect(await executor.run(history("pickup"), signal())).toEqual(busy);
    expect(await executor.run(history("open", "open:turn-2"), signal())).toEqual(busy);
    expect([actions(), recoveries().length]).toEqual([["open"], 2]);
  });

  it("read the folder all the same where the folder its place's record names is not there or is another: what its landings keep is left, and said", async () => {
    await executor.run(op("resolve", { path: "" }), signal());
    left();
    recoveryStarts = { type: "failed", message: `the folder ${folder} is not there`, folder: true };
    expect(await executor.run(history("pickup"), signal())).toEqual({ ok: {} });
    expect(actions()).toEqual(["open", "pickup"]);
    expect(executor.recoveries()).toEqual([{ folder, state: "left", why: `the folder ${folder} is not there` }]);
  });

  it("put it back as this computer's tools start, in each folder whose landings keep anything, and say what it found", async () => {
    await executor.run(op("resolve", { path: "" }), signal());
    const kept = left();
    await executor.stop();
    starts = [];
    const told: Recovery[] = [];
    executor = executorOf({ recovered: (recovery) => void told.push(recovery) });
    await until(() => told.length === 2);
    expect(recoveries().map((start) => [start.folder, start.recovery])).toEqual([[folder, { kept }]]);
    expect(told).toEqual([{ folder, state: "begun" }, { folder, state: "found", found: nothing }]);
    expect(executor.recoveries()).toEqual([{ folder, state: "found", found: nothing }]);
  });
});
