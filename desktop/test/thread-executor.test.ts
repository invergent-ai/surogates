// The app's executor for a project thread's root (spec, Section 13, "The user's computer"): its file kinds in a
// host on its copy, which the executor's copies have the guest make first, and its commands in the guest, which
// shares the copy at the folder's path. The guest and the file hosts are stand-ins here: what they do with what
// they are given is theirs to test.

import { mkdirSync, mkdtempSync, realpathSync, renameSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { BOOT_ID } from "../src/binding/folder.js";
import { downloadSaver } from "../src/browser/downloads.js";
import { Browsing } from "../src/browser/executor.js";
import { kinds } from "../src/files/operations.js";
import type { Making } from "../src/history/copies.js";
import { keyOf } from "../src/history/place.js";
import { FOLDER_UNAVAILABLE, type FromHost, type HostStart, type ToHost } from "../src/hosts/messages.js";
import type { BoundFolder, HostProcess } from "../src/hosts/tool-hosts.js";
import type { Binding } from "../src/journal/bindings.js";
import type { Operation, Outcome } from "../src/link/protocol.js";
import { PROCESS_KINDS, VmExecutor } from "../src/vm/executor.js";
import type { HistoryRequest } from "../src/vm/history.js";
import type { Place, VmOperation } from "../src/vm/manager.js";

const THREAD = "0b6c1d3e-6f0a-4c1e-9a52-6a1d2c3b4e5f";
const OTHER = "3c4d5e6f-7a8b-4c9d-8e0f-1a2b3c4d5e6f";
const CHAT = "7d8e9f00-1a2b-4c3d-8e4f-5a6b7c8d9e0f";
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

// A file host that says it is ready, answers each operation as a file helper on an empty folder would, and lets
// a command run.
function host(): HostProcess {
  const heard: Array<(message: FromHost) => void> = [];
  const exits: Array<() => void> = [];
  let held = "";
  const answers: Record<string, (args: Record<string, unknown>) => unknown> = {
    resolve: (args) => args.path, stat: () => null, write: () => null, read: () => DATA,
  };
  return {
    send: (message: ToHost) => queueMicrotask(() => {
      const say = (outcome: Outcome) => "id" in message && heard.forEach((listener) => listener({ type: "result", id: message.id, outcome }));
      if (message.type === "start") {
        starts.push(message);
        held = message.folder;
        heard.forEach((listener) => listener({ type: "ready", processes: [] }));
      } else if (message.type === "op") {
        ops.push({ held, kind: message.kind, args: message.args });
        say({ ok: (answers[message.kind] ?? (() => ({ did: message.kind })))(message.args) });
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
  bindings = new Map();
  bind(THREAD, true);
  bind(CHAT, false);
  executor = new VmExecutor({
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
      // The guest's history: it makes a copy's folder at its open, as git does.
      history: async (request): Promise<Outcome> => {
        asked.push(request);
        if (request.action === "open") mkdirSync(join(request.place.history, "threads", request.thread), { recursive: true });
        return { ok: request.action === "open" ? { copy: "made" } : {} };
      },
      unplace: async (place) => {
        unplaced.push(place);
        return true;
      },
    },
  });
});

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
    // Every kind an operation can have but a browser's: the file helper's, a thread's own, one that is none, and the commands'.
    const all = [...kinds(), "history", "checkpoint", "no-such-kind", ...PROCESS_KINDS];
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
