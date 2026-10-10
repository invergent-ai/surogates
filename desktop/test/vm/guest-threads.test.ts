// Project threads on folders of this computer, through the app's executor, under QEMU and KVM: the operations
// as the device link delivers them; each thread's file tools in a host of the app's own on its copy, its helper
// in its sandbox; its commands in the guest, which shares the copy at the folder's path; and each copy made by
// the guest's git, in the folder's place in the app's data. Each folder is compared, as it lies, around every
// operation. Behind SUROGATE_VM_TESTS=1: the image built by images/guest/build.sh, and npm run build and npm run
// agent-disk first.

import {
  chmodSync, existsSync, lstatSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, realpathSync, renameSync, rmSync, statSync, writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { BOOT_ID } from "../../src/binding/folder.js";
import { keyOf } from "../../src/history/place.js";
import type { FromHost, HostStart, ToHost } from "../../src/hosts/messages.js";
import { forkHost, type HostProcess, ToolHosts } from "../../src/hosts/tool-hosts.js";
import { OperationJournal } from "../../src/journal/journal.js";
import type { Operation, Outcome } from "../../src/link/protocol.js";
import { VmClient } from "../../src/vm/client.js";
import { VmExecutor } from "../../src/vm/executor.js";
import type { HistoryRequest } from "../../src/vm/history.js";
import type { VmOperation } from "../../src/vm/manager.js";
import { asItLies } from "../as-it-lies.js";
import { agentDisk, background, IMAGE, KVM, needsKvm, signal, until, USER } from "./guest-support.js";

beforeAll(needsKvm);

const ONE = "0b6c1d3e-6f0a-4c1e-9a52-6a1d2c3b4e5f";
const TWO = "7d8e9f00-1a2b-4c3d-8e4f-5a6b7c8d9e0f";
const THREE = "3c4d5e6f-7a8b-4c9d-8e0f-1a2b3c4d5e6f";
// Enough files that a copy's making runs for some seconds: long enough to be cut part of the way.
const FILES = 5_000;
const data64 = (text: string) => Buffer.from(text).toString("base64");

// A folder as it lies (as-it-lies.ts), and each name of it that came, went, or is not in every way as it was.
type Lies = Record<string, unknown>;
const differing = (before: Lies, after: Lies): string[] =>
  [...new Set([...Object.keys(before), ...Object.keys(after)])].filter((name) => JSON.stringify(before[name]) !== JSON.stringify(after[name])).sort();
const up = (path: string) => (path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : ".");
// The folders a file's coming or going shows in: the one it is in, and each above it down to one that was there.
function above(path: string, before: Lies): string[] {
  const folders: string[] = [];
  for (let at = up(path); ; at = up(at)) {
    folders.push(at);
    if (before[at] !== undefined || at === ".") return folders;
  }
}
interface Change {
  path: string;
  before: string | null;
  after: string | null;
}

describe.skipIf(process.env.SUROGATE_VM_TESTS !== "1")("threads on a folder, through the app's executor, each in its copy", { timeout: 300_000 }, () => {
  let dir: string;
  let data: string;
  let folder: string;
  let large: string;
  let run: string;
  let vm: VmClient;
  let journal: OperationJournal;
  let executor: VmExecutor;
  let count = 0;
  // Each folder as it lay before the threads: compared after every operation of theirs.
  const lay = new Map<string, Record<string, unknown>>();
  // What each file host was started on, what the guest was asked to share, and what the folders' histories were asked.
  const starts: HostStart[] = [];
  const shared: VmOperation[] = [];
  const asked: HistoryRequest[] = [];

  // The app's executor on the one guest, as the app makes it at each start.
  const again = async () => {
    await executor?.stop();
    executor = new VmExecutor({
      bindingOf: (root) => journal.bindings.get(root), dataDir: data, cacheDir: join(dir, "cache", "surogate"),
      env: { HOME: USER.home, LANG: "C.UTF-8", PATH: "/usr/bin:/bin" }, idleMs: 60_000, user: "u1",
      spawnHost: () => {
        const host = forkHost();
        return {
          ...host,
          send: (message) => {
            if (message.type === "start") starts.push(message);
            host.send(message);
          },
        };
      },
      vm: {
        perform: (operation, stop) => {
          shared.push(operation);
          return vm.perform(operation, stop);
        },
        teardown: (root) => vm.teardown(root),
        onProcesses: (listener) => vm.onProcesses(listener),
        onAsk: (listener) => vm.onAsk(listener),
        history: (request, stop) => {
          asked.push(request);
          return vm.history(request, stop);
        },
        unplace: (place) => vm.unplace(place),
      },
    });
  };
  const bind = (root: string, at: string) => {
    const { dev, ino } = statSync(at);
    journal.bindings.add({ root, nonce: `nonce-${root}`, folder: at, dev, ino, boot: BOOT_ID, mode: "free", boundAt: Date.now(), history: root });
  };
  // One operation of *root*'s, and every folder as it lay before.
  const operate = async (root: string, kind: string, args: Record<string, unknown>): Promise<Outcome> => {
    count += 1;
    const operation: Operation = { id: `${kind}-${count}`, sessionId: root, callingSessionId: root, invocationId: "17", ordinal: count, kind, args, digest: "d" };
    const outcome = await executor.run(operation, signal());
    for (const [at, was] of lay) expect(asItLies(at), `${at} after ${kind} of ${root}`).toEqual(was);
    return outcome;
  };
  const did = async (root: string, kind: string, args: Record<string, unknown>) => {
    const outcome = await operate(root, kind, args);
    expect(outcome, `${kind} ${JSON.stringify(args).slice(0, 200)}`).toHaveProperty("ok");
    return (outcome as { ok: unknown }).ok;
  };
  const key = (name: string, at = folder) => join(at, name);
  const copyOf = (root: string, at = folder) => join(data, "history", keyOf(at), "threads", root);
  const command = (root: string, line: string) => did(root, "run", { command: line, workdir: null, timeout: 30 });
  const write = (root: string, name: string, text: string) => operate(root, "write", { key: key(name), data: data64(text) });
  const read = (root: string, name: string, at = folder) => operate(root, "read", { key: key(name, at), max_bytes: null });
  const files = (at: string) => (readdirSync(at, { recursive: true }) as string[]).filter((name) => lstatSync(join(at, name)).isFile()).sort();

  beforeAll(async () => {
    dir = realpathSync(mkdtempSync(join(tmpdir(), "vm-threads-")));
    data = join(dir, "data");
    folder = join(dir, "Documents");
    mkdirSync(folder);
    writeFileSync(key("Report.md"), "# Report\n\nTotals: 40\n");
    writeFileSync(key("Budget.csv"), "item,cost\nrent,40\n");
    large = join(dir, "Archive");
    mkdirSync(join(large, "data"), { recursive: true });
    for (let n = 0; n < FILES; n += 1) writeFileSync(join(large, "data", `${n}.txt`), `file ${n}\n`);
    for (const at of [folder, large]) lay.set(at, asItLies(at));
    run = mkdtempSync(join(process.env.XDG_RUNTIME_DIR ?? "/tmp", "sg-vm-"));
    // The manager in a process of its own, as the app runs it: a place and its history go through its channel.
    vm = new VmClient({
      vm: {
        kernel: join(IMAGE, "vmlinuz"), rootfs: join(IMAGE, "rootfs.img"), agentDisk: agentDisk(dir), sessions: join(dir, "sessions.img"),
        run, console: join(dir, "console.log"), user: USER, kvm: KVM,
      },
    });
    journal = new OperationJournal(join(dir, "journal.sqlite"));
    bind(ONE, folder);
    bind(TWO, folder);
    await again();
  });

  afterAll(async () => {
    await executor?.stop();
    await vm?.stop();
    journal?.close();
    if (run) rmSync(run, { recursive: true, force: true });
    if (dir) rmSync(dir, { recursive: true, force: true });
  });

  it("work at once on one folder, each in its copy at the folder's path, through their file tools and their commands, and the folder is as it was", async () => {
    // Their first operations come together: each has its copy made, and a host started on it.
    expect(await Promise.all([did(ONE, "resolve", { path: "Report.md" }), did(TWO, "resolve", { path: "Report.md" })])).toEqual([key("Report.md"), key("Report.md")]);
    // The first thread: a file tool's write, and a command that reads it at once, where the folder is.
    expect(await write(ONE, "Report.md", "# Report\n\nTotals: 40\n\nTidied by A.\n")).toEqual({ ok: null });
    expect(await command(ONE, "pwd; cat Report.md; echo 'notes of A' > A.md")).toMatchObject({ output: `${folder}\n# Report\n\nTotals: 40\n\nTidied by A.\n`, returncode: 0 });
    // The second: its command's files are its file tools' to read at once, a background process's too.
    expect(await write(TWO, "Report.md", "# Report\n\nTotals: 42\n")).toEqual({ ok: null });
    expect(await command(TWO, "pwd; printf 'item,cost\\nrent,42\\n' > Budget.csv; echo 'notes of B' > B.md")).toMatchObject({ output: `${folder}\n` });
    expect(await read(TWO, "B.md")).toEqual({ ok: data64("notes of B\n") });
    const { session_id } = (await did(TWO, "start", background("sleep 1; echo later > Later.md"))) as { session_id: string };
    for (let polls = 0; polls < 100; polls += 1) {
      if (((await did(TWO, "poll", { session_id })) as { status: string }).status === "exited") break;
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    expect(await read(TWO, "Later.md")).toEqual({ ok: data64("later\n") });
    // Each sees its own copy alone, by the folder's names.
    expect(await read(ONE, "B.md")).toEqual({ error: { type: "os", code: "ENOENT", message: `No such file or directory: '${key("B.md")}'` } });
    const found = (await did(ONE, "ripgrep", { key: folder, mode: "files", pattern: "*.md", glob: null, context: 0 })) as string;
    expect(found.split("\n").filter(Boolean).sort()).toEqual([key("A.md"), key("Report.md")]);
    expect(await command(TWO, "ls")).toMatchObject({ output: "B.md\nBudget.csv\nLater.md\nReport.md\n" });
    // What each did is in its copy, of this user's; nothing of git is in the folder or a copy.
    expect(readFileSync(join(copyOf(ONE), "Report.md"), "utf8")).toBe("# Report\n\nTotals: 40\n\nTidied by A.\n");
    expect(readFileSync(join(copyOf(TWO), "Report.md"), "utf8")).toBe("# Report\n\nTotals: 42\n");
    expect(lstatSync(join(copyOf(ONE), "A.md")).uid).toBe(USER.uid);
    expect([folder, copyOf(ONE), copyOf(TWO)].filter((at) => existsSync(join(at, ".git")))).toEqual([]);
    // Every host is on a copy, by the folder's path, and everything the guest shared for them is a copy at that path.
    expect(starts.map((start) => [start.folder, start.at]).sort()).toEqual([[copyOf(ONE), folder], [copyOf(TWO), folder]]);
    expect(new Set(shared.map((operation) => `${operation.root} ${operation.folder.path} ${operation.at}`))).toEqual(
      new Set([`${ONE} ${copyOf(ONE)} ${folder}`, `${TWO} ${copyOf(TWO)} ${folder}`]),
    );
    // Each copy was opened once, where the guest made it, and not moved.
    expect(asked.map((request) => [request.thread, request.action, request.args]).sort()).toEqual([[ONE, "open", { moves: false }], [TWO, "open", { moves: false }]]);
  });

  it("make a copy whose first making the app's quit cut, before the thread's first step works in it", async () => {
    bind(THREE, large);
    const copy = copyOf(THREE, large);
    const asking = asked.length;
    // The thread's first step, on a folder whose copy takes some seconds to make; the app quits as it is made.
    const first = operate(THREE, "read", { key: key("data/0.txt", large), max_bytes: null });
    await until(() => existsSync(`${copy}.making`), 60_000);
    await executor.stop();
    expect(await first).toMatchObject({ error: { type: "unavailable" } });
    // What the cut left is no whole copy: its making was never marked ended.
    expect(existsSync(`${copy}.making`)).toBe(true);
    // The app starts again: the thread's next step has the guest make the copy again, and works in the copy it made.
    await again();
    expect(await read(THREE, "data/0.txt", large)).toEqual({ ok: data64("file 0\n") });
    expect(asked.slice(asking).map((request) => [request.thread, request.action, request.args])).toEqual([
      [THREE, "open", { moves: false }], [THREE, "open", { moves: false }],
    ]);
    expect(existsSync(`${copy}.making`)).toBe(false);
    expect(files(copy)).toEqual(files(large));
    expect(await command(THREE, "ls data | wc -l; pwd")).toMatchObject({ output: `${FILES}\n${large}\n` });
    expect(starts.at(-1)).toMatchObject({ folder: copy, at: large });
  });

  it("make again a copy left half made, before the thread's next step works in it", async () => {
    const copy = copyOf(THREE, large);
    await executor.stop();
    // As a making of the copy again from its branch, cut part of the way, leaves it: marked, and half its files written.
    writeFileSync(`${copy}.making`, "");
    for (let n = FILES / 2; n < FILES; n += 1) rmSync(join(copy, "data", `${n}.txt`));
    const made = statSync(copy).ino;
    const asking = asked.length;
    // The app starts again, and knows no copy as whole: the guest is asked to open it before anything works in it.
    await again();
    expect(await read(THREE, `data/${FILES - 1}.txt`, large)).toEqual({ ok: data64(`file ${FILES - 1}\n`) });
    expect(asked.slice(asking).map((request) => [request.thread, request.action, request.args])).toEqual([[THREE, "open", { moves: false }]]);
    expect(statSync(copy).ino).not.toBe(made);
    expect(existsSync(`${copy}.making`)).toBe(false);
    expect(files(copy)).toEqual(files(large));
    expect(await command(THREE, "ls data | wc -l")).toMatchObject({ output: `${FILES}\n` });
  });
});

// Two threads' turns on one folder, their steps and their landings as the server's worker asks them over the device link:
// each under the invocation of its own the journal takes it under, through the app's executor, its hosts and its guest.
// The folder is compared by every name, mode, time and byte around each step, with what the step may have changed.
describe.skipIf(process.env.SUROGATE_VM_TESTS !== "1")("a thread's checkpoints and its landings, through the app's executor", { timeout: 300_000 }, () => {
  const YOU = { name: "u1", email: "user:u1@surogate" };
  const TITLES: Record<string, string> = { [ONE]: "Tidy the report", [TWO]: "Check the totals" };
  const NOTHING_CUT = { restored: [], beside: [], lost: [], unread: [] };
  let dir: string;
  let data: string;
  let folder: string;
  let run: string;
  let vm: VmClient;
  let journal: OperationJournal;
  let executor: VmExecutor;
  let count = 0;
  const starts: HostStart[] = [];
  // How many landings' hosts were started, and how many of them have gone.
  const landers = { started: 0, gone: 0 };

  // One operation of *root*'s, as the device link delivers it under *invocation*; and around it, the folder: what is
  // not as it was is what *may* names, by the folder as it was.
  const operate = async (root: string, kind: string, args: Record<string, unknown>, invocation = "17", may: (before: Lies) => string[] = () => []) => {
    count += 1;
    const operation: Operation = { id: `${kind}-${count}`, sessionId: root, callingSessionId: root, invocationId: invocation, ordinal: count, kind, args, digest: "d" };
    const before = asItLies(folder);
    const outcome = await executor.run(operation, signal());
    const changed = "ok" in outcome ? may(before).sort() : [];
    expect(differing(before, asItLies(folder)), `what ${kind} ${String(args.action ?? "")} of ${root} changed in the folder`).toEqual(changed);
    return outcome;
  };
  const did = async (root: string, kind: string, args: Record<string, unknown>, invocation?: string, may?: (before: Lies) => string[]) => {
    const outcome = await operate(root, kind, args, invocation, may);
    expect(outcome, `${kind} ${JSON.stringify(args).slice(0, 200)}`).toHaveProperty("ok");
    return (outcome as { ok: Record<string, unknown> }).ok;
  };
  const key = (name: string) => join(folder, name);
  const real = (name: string) => readFileSync(key(name), "utf8");
  const copyOf = (root: string) => join(data, "history", keyOf(folder), "threads", root);
  const kept = (...inside: string[]) => join(data, "landings", keyOf(folder), ...inside);
  const command = (root: string, line: string) => did(root, "run", { command: line, workdir: null, timeout: 30 });
  const write = (root: string, name: string, text: string, more: Record<string, unknown> = {}) => did(root, "write", { key: key(name), data: data64(text), ...more });

  /**
   * One landing of *root*'s turn *turn*, as the worker's saga asks it of this computer: the hold, the files the turn
   * changed, the look at them in the folder, your edits picked up, the turn committed, each file applied at the revision
   * the look saw, the landing recorded, and what it kept forgotten. An apply that is refused has each one before it put
   * back, the newest first, and the landing forgotten without a record. A change with no version on either side is no
   * apply's. The forgetting names every apply that was answered, by its step. *between* runs once the files to apply are
   * fixed. Around each step, the folder changed where the step may change it, and nowhere else.
   */
  const landing = async (root: string, turn: string, between: () => void = () => {}) => {
    const invocation = `land:${turn}`;
    const saga = `saga-${root.slice(0, 8)}-${turn}`;
    const trailers = [["Surogate-Saga", saga]];
    const author = { name: TITLES[root] ?? root, email: `thread:${root}@surogate` };
    const history = (action: string, args: Record<string, unknown> = {}) => did(root, "history", { action, ...args }, invocation);
    const land = (action: string, args: Record<string, unknown>, may?: (before: Lies) => string[]) => operate(root, "land", { action, ...args }, invocation, may);
    // A file's coming, going or replacing shows in it and in the folders above it, down to one that was there.
    const where = (path: string) => (before: Lies) => [...above(path, before), path];
    const recovered = (await land("recover", {}) as { ok: unknown }).ok;
    const { paths } = await history("changed") as { paths: string[] };
    const seen = Object.fromEntries(((await land("revisions", { paths }) as { ok: { revisions: Array<[string, string]> } }).ok.revisions));
    const picked = await history("pickup", { author: YOU, trailers });
    const committed = await history("commit", { author, trailers, pickup: picked.commit });
    const changes = committed.changes as Change[];
    between();
    const applied: Array<Change & { step: number }> = [];
    let failed: { type: string; message: string } | null = null;
    for (const [step, change] of changes.entries()) {
      if (change.before === null && change.after === null) continue;
      const outcome = await land("apply", { saga, step, ...change, expected: seen[change.path] }, where(change.path));
      if ("error" in outcome) {
        failed = outcome.error;
        break;
      }
      applied.push({ step, ...change });
    }
    const putBack: unknown[] = [];
    for (const { step, path } of failed ? [...applied].reverse() : []) {
      putBack.push((await land("unapply", { saga, step, path }, where(path)) as { ok: unknown }).ok);
    }
    const recorded = failed || committed.commit === null ? null : await history("record", {
      turn: committed.commit, applied: changes, author, trailers, main: picked.main, pickup: picked.commit,
    });
    expect(await land("forget", { saga, applied })).toEqual({ ok: {} });
    expect(existsSync(kept(saga))).toBe(false);
    // Forgotten, the landing is over: its host lets the folder go at once, not once idle.
    await until(() => landers.gone === landers.started, 15_000);
    return { state: failed ? "compensated" as const : "completed" as const, failed, recovered, picked, committed, changes, putBack, recorded };
  };

  beforeAll(async () => {
    dir = realpathSync(mkdtempSync(join(tmpdir(), "vm-threads-land-")));
    data = join(dir, "data");
    folder = join(dir, "Documents");
    mkdirSync(join(folder, "notes"), { recursive: true });
    writeFileSync(key("Report.md"), "# Report\n\nTotals: 40\n");
    writeFileSync(key("Budget.csv"), "item,cost\nrent,40\n");
    writeFileSync(key("notes/keep.txt"), "kept as it is\n");
    // A mode of its user's own: a landing that replaces the file leaves it the mode it had.
    chmodSync(key("Budget.csv"), 0o600);
    run = mkdtempSync(join(process.env.XDG_RUNTIME_DIR ?? "/tmp", "sg-vm-"));
    vm = new VmClient({
      vm: {
        kernel: join(IMAGE, "vmlinuz"), rootfs: join(IMAGE, "rootfs.img"), agentDisk: agentDisk(dir), sessions: join(dir, "sessions.img"),
        run, console: join(dir, "console.log"), user: USER, kvm: KVM,
      },
    });
    journal = new OperationJournal(join(dir, "journal.sqlite"));
    for (const root of [ONE, TWO]) {
      const { dev, ino } = statSync(folder);
      journal.bindings.add({ root, nonce: `nonce-${root}`, folder, dev, ino, boot: BOOT_ID, mode: "free", boundAt: Date.now(), history: root });
    }
    executor = new VmExecutor({
      bindingOf: (root) => journal.bindings.get(root), dataDir: data, cacheDir: join(dir, "cache", "surogate"),
      env: { HOME: USER.home, LANG: "C.UTF-8", PATH: "/usr/bin:/bin" }, idleMs: 60_000, user: "u1",
      spawnHost: () => {
        const host = forkHost();
        let lands = false;
        host.onExit(() => {
          if (lands) landers.gone += 1;
        });
        return {
          ...host,
          send: (message) => {
            if (message.type === "start") {
              starts.push(message);
              lands = message.landing !== undefined;
              if (lands) landers.started += 1;
            }
            host.send(message);
          },
        };
      },
      vm: {
        perform: (operation, stop) => vm.perform(operation, stop), teardown: (root) => vm.teardown(root), onProcesses: (listener) => vm.onProcesses(listener),
        onAsk: (listener) => vm.onAsk(listener), history: (request, stop) => vm.history(request, stop), unplace: (place) => vm.unplace(place),
      },
    });
  });

  afterAll(async () => {
    await executor?.stop();
    await vm?.stop();
    journal?.close();
    if (run) rmSync(run, { recursive: true, force: true });
    if (dir) rmSync(dir, { recursive: true, force: true });
  });

  it("take back a stopped step in the thread's copy alone, to the snapshot taken before it", async () => {
    expect(await Promise.all([did(ONE, "history", { action: "open" }, "open:t1"), did(TWO, "history", { action: "open" }, "open:t1")])).toEqual([
      { copy: "made" }, { copy: "made" },
    ]);
    // The first thread's step, its snapshot taken before it.
    expect((await did(ONE, "checkpoint", { action: "take", reason: "before write_file" }, "checkpoint:t1:1:call-1")).hash).toMatch(/^[0-9a-f]{40}$/);
    expect(await write(ONE, "Report.md", "# Report\n\nTotals: 40\n\nTidied by A.\n")).toBeNull();
    await command(ONE, "echo 'notes of A' > A.md");
    // A step stopped part-way: what it made and what it changed are taken back, by its snapshot, in the copy.
    const { hash } = await did(ONE, "checkpoint", { action: "take", reason: "before terminal" }, "checkpoint:t1:2:call-2");
    await command(ONE, "mkdir junk && echo x > junk/x; echo half > scratch.txt; sed -i 's/Tidied/Ruined/' Report.md");
    expect(await did(ONE, "checkpoint", { action: "restore", hash }, `checkpoint:t1:restore:${String(hash)}`)).toEqual({});
    expect(await command(ONE, "ls; cat Report.md")).toMatchObject({ output: "A.md\nBudget.csv\nReport.md\nnotes\n# Report\n\nTotals: 40\n\nTidied by A.\n" });
    // A snapshot from before is no turn of the other thread's: its copy is its own.
    await write(TWO, "Report.md", "# Report\n\nTotals: 42\n");
    await command(TWO, "printf 'item,cost\\nrent,42\\n' > Budget.csv; echo 'notes of B' > B.md");
    expect(readdirSync(folder).sort()).toEqual(["Budget.csv", "Report.md", "notes"]);
  });

  it("land one thread's turn in the folder, and leave the other's copy as it was until its next turn", async () => {
    const first = await landing(ONE, "t1");
    expect([first.state, first.recovered, first.changes.map((change) => change.path)]).toEqual(["completed", NOTHING_CUT, ["A.md", "Report.md"]]);
    expect(first.recorded).toMatchObject({ commit: expect.stringMatching(/^[0-9a-f]{40}$/) });
    expect([real("Report.md"), real("A.md")]).toEqual(["# Report\n\nTotals: 40\n\nTidied by A.\n", "notes of A\n"]);
    expect(await command(TWO, "cat Report.md")).toMatchObject({ output: "# Report\n\nTotals: 42\n" });
    // The landing's host was on the folder itself, given the thread's copy to read.
    expect(starts.filter((start) => start.landing).map((start) => [start.folder, start.landing?.copy])).toEqual([[folder, copyOf(ONE)]]);
  });

  it("never overwrite a file you save while a landing runs: the landing is put back whole, and it names the file the other thread changed", async () => {
    const cut = await landing(TWO, "t1", () => writeFileSync(key("Budget.csv"), "item,cost\nrent,40\npower,5\n"));
    expect(cut).toMatchObject({
      state: "compensated", failed: { type: "conflict", message: "Budget.csv changed in the folder while this landing ran, so it was not replaced" },
    });
    expect([cut.changes.map((change) => change.path), cut.putBack]).toEqual([["B.md", "Budget.csv"], [{ path: "B.md", put_back: true }]]);
    expect(cut.committed.overlapped).toEqual([
      expect.objectContaining({ path: "Report.md", reason: "changed", by: { kind: "thread", id: ONE, title: "Tidy the report" } }),
    ]);
    expect([real("Budget.csv"), statSync(key("Budget.csv")).mode & 0o777]).toEqual(["item,cost\nrent,40\npower,5\n", 0o600]);
    expect(readdirSync(folder).sort()).toEqual(["A.md", "Budget.csv", "Report.md", "notes"]);
  });

  it("land again as a new landing, which names your save and the other thread's change, and leaves both files as they are", async () => {
    const again = await landing(TWO, "t1-again");
    expect([again.state, again.changes.map((change) => change.path)]).toEqual(["completed", ["B.md"]]);
    expect(again.picked.picked_up).toEqual([expect.objectContaining({ path: "Budget.csv" })]);
    expect(again.committed.overlapped).toEqual([
      expect.objectContaining({ path: "Budget.csv", reason: "changed", by: { kind: "you" } }),
      expect.objectContaining({ path: "Report.md", reason: "changed", by: { kind: "thread", id: ONE, title: "Tidy the report" } }),
    ]);
    expect([real("B.md"), real("Budget.csv"), real("Report.md")]).toEqual(["notes of B\n", "item,cost\nrent,40\npower,5\n", "# Report\n\nTotals: 40\n\nTidied by A.\n"]);
  });

  it("redo the second thread's change on the newer files at its next turn, and both threads' changes reach the folder", async () => {
    expect(await did(TWO, "history", { action: "open" }, "open:t2")).toEqual({ copy: "moved" });
    expect(await command(TWO, "cat Report.md Budget.csv")).toMatchObject({ output: "# Report\n\nTotals: 40\n\nTidied by A.\nitem,cost\nrent,40\npower,5\n" });
    // By its file tools, on the version it now reads; and by a command.
    const { revision } = await did(TWO, "stat", { key: key("Report.md") });
    expect(await write(TWO, "Report.md", "# Report\n\nTotals: 47\n\nTidied by A.\n", { expected_revision: revision })).toBeNull();
    await command(TWO, "sed -i 's/rent,40/rent,42/' Budget.csv");
    const redone = await landing(TWO, "t2");
    expect([redone.state, redone.committed.overlapped, redone.changes.map((change) => change.path)]).toEqual(["completed", [], ["Budget.csv", "Report.md"]]);
    expect([real("Report.md"), real("Budget.csv"), statSync(key("Budget.csv")).mode & 0o777]).toEqual([
      "# Report\n\nTotals: 47\n\nTidied by A.\n", "item,cost\nrent,42\npower,5\n", 0o600,
    ]);
    // And the first thread's next turn starts from all of it.
    expect(await did(ONE, "history", { action: "open" }, "open:t2")).toEqual({ copy: "moved" });
    expect(await did(ONE, "read", { key: key("Report.md"), max_bytes: null })).toEqual(data64("# Report\n\nTotals: 47\n\nTidied by A.\n"));
    expect(readdirSync(folder).sort()).toEqual(["A.md", "B.md", "Budget.csv", "Report.md", "notes"]);
    expect(existsSync(kept()) ? readdirSync(kept()) : []).toEqual([]);
  });
});

// A thread's copy made again while one of its commands is under way, as the copies say when they vouch for it no more:
// the tool hosts as the app's executor drives them, the copies a stand-in that gives the copy at its path as it is.
describe.skipIf(process.env.SUROGATE_VM_TESTS !== "1")("a thread's commands, its copy made again under them", { timeout: 300_000 }, () => {
  const FOUR = "5f6a7b8c-9d0e-4f1a-8b2c-3d4e5f6a7b8c";
  const FIVE = "6a7b8c9d-0e1f-4a2b-8c3d-4e5f6a7b8c9d";
  let dir: string;
  let data: string;
  let folder: string;
  let run: string;
  let vm: VmClient;
  let count = 0;
  const made: ToolHosts[] = [];
  const copyOf = (root: string) => join(data, "history", keyOf(folder), "threads", root);
  const asideOf = (root: string) => `${copyOf(root)}.aside`;
  const names = (at: string) => readdirSync(at).sort();
  // The copy made again, as the guest's next open does once the copies vouch for it no more: the one before set aside, and
  // another at its path.
  const again = (root: string) => {
    renameSync(copyOf(root), asideOf(root));
    mkdirSync(copyOf(root));
    writeFileSync(join(copyOf(root), "marker"), "copy made again\n");
  };
  const hostsOf = (spawnHost?: () => HostProcess) => {
    const { dev, ino } = statSync(folder);
    const place = { key: keyOf(folder), history: join(data, "history", keyOf(folder)), real: { path: folder, dev, ino, boot: BOOT_ID } };
    const hosts = new ToolHosts({
      bindingOf: (root) => ({ folder, dev, ino, boot: BOOT_ID, history: root }),
      copies: {
        open: async (root) => {
          const at = statSync(copyOf(root));
          return { copy: { place, folder: { path: copyOf(root), dev: at.dev, ino: at.ino, boot: BOOT_ID }, at: folder }, handle: Object.freeze({ root }) };
        },
        close: () => {},
        ask: async () => ({ ok: { landing: null } }),
      },
      dataDir: data, cacheDir: join(dir, "cache", "surogate"), env: { HOME: USER.home, LANG: "C.UTF-8", PATH: "/usr/bin:/bin" }, idleMs: 60_000,
      release: (root) => vm.teardown(root),
      ...(spawnHost ? { spawnHost } : {}),
    });
    made.push(hosts);
    return hosts;
  };
  // A command of *root*'s as the executor runs one: under its host's hook guard, in the guest, which shares what the host holds.
  const command = (hosts: ToolHosts, root: string, line: string, sent: () => void = () => {}) => {
    count += 1;
    const operation: Operation = {
      id: `run-${count}`, sessionId: root, callingSessionId: root, invocationId: "17", ordinal: count, kind: "run", args: { command: line, workdir: null, timeout: 30 }, digest: "d",
    };
    return hosts.guarded(operation, signal(), "around", ({ folder: shared, at }, aborted, ended) => {
      sent();
      return vm.perform({ id: operation.id, root, folder: shared, ...(at === undefined ? {} : { at }), kind: "run", args: operation.args, ended }, aborted);
    });
  };

  beforeAll(() => {
    dir = realpathSync(mkdtempSync(join(tmpdir(), "vm-threads-again-")));
    data = join(dir, "data");
    folder = join(dir, "Documents");
    mkdirSync(folder);
    for (const root of [FOUR, FIVE]) {
      mkdirSync(copyOf(root), { recursive: true });
      writeFileSync(join(copyOf(root), "marker"), "first copy\n");
    }
    run = mkdtempSync(join(process.env.XDG_RUNTIME_DIR ?? "/tmp", "sg-vm-"));
    vm = new VmClient({
      vm: {
        kernel: join(IMAGE, "vmlinuz"), rootfs: join(IMAGE, "rootfs.img"), agentDisk: agentDisk(dir), sessions: join(dir, "sessions.img"),
        run, console: join(dir, "console.log"), user: USER, kvm: KVM,
      },
    });
  });

  afterAll(async () => {
    for (const hosts of made) await hosts.stop();
    await vm?.stop();
    if (run) rmSync(run, { recursive: true, force: true });
    if (dir) rmSync(dir, { recursive: true, force: true });
  });

  it("run the next host's commands in the copy made again, though one sent as the guest booted was shared the copy before after its letting go", async () => {
    // The app's own hosts, and the thread's first command booting the guest.
    const hosts = hostsOf();
    let sent = false;
    const first = command(hosts, FOUR, "cat marker; echo first > first.txt", () => void (sent = true));
    await until(() => sent, 60_000);
    // Its host is told to go as the guest boots: the guest's letting go of the root finds nothing shared yet.
    await new Promise((resolve) => setTimeout(resolve, 200));
    hosts.replaced(FOUR);
    // The command runs once the guest is up, in the copy it named, shared after that letting go; its host has gone by then.
    expect(await first).toMatchObject({ error: { type: "interrupted" } });
    again(FOUR);
    expect(await command(hosts, FOUR, "cat marker; echo next > next.txt")).toMatchObject({ ok: { output: "copy made again\n" } });
    expect([names(copyOf(FOUR)), names(asideOf(FOUR))]).toEqual([["marker", "next.txt"], ["first.txt", "marker"]]);
  });

  it("run no command of a host told to go once its letting go began, though its hook guard answers it as it stops", async () => {
    // A host that holds the hook guard's question of the second command until its stop, then answers it, as host.ts does.
    let hold = false;
    const held: string[] = [];
    const host = (): HostProcess => {
      const heard: Array<(message: FromHost) => void> = [];
      const exits: Array<() => void> = [];
      const say = (message: FromHost) => heard.forEach((listener) => listener(message));
      return {
        send: (message: ToHost) => queueMicrotask(() => {
          if (message.type === "start") say({ type: "ready", processes: [] });
          else if (message.type === "refusal") {
            if (hold) held.push(message.id);
            else say({ type: "result", id: message.id, outcome: { ok: null } });
          } else if (message.type === "after") say({ type: "result", id: message.id, outcome: message.outcome });
          else if (message.type === "stop") {
            for (const id of held.splice(0)) say({ type: "result", id, outcome: { ok: null } });
            exits.splice(0).forEach((listener) => listener());
          }
        }),
        onMessage: (listener) => void heard.push(listener),
        onExit: (listener) => void exits.push(listener),
        kill: () => exits.splice(0).forEach((listener) => listener()),
      };
    };
    const hosts = hostsOf(host);
    expect(await command(hosts, FIVE, "cat marker")).toMatchObject({ ok: { output: "first copy\n" } });
    hold = true;
    const late = command(hosts, FIVE, "cat marker; touch late.txt");
    await until(() => held.length === 1);
    hold = false;
    hosts.replaced(FIVE);
    expect(await late).toEqual({
      error: {
        type: "unavailable",
        message: `This computer could not open the folder's sandbox: the copy of ${folder} this thread works in was made again while this was asked, so it was not done. Ask again`,
      },
    });
    again(FIVE);
    expect(await command(hosts, FIVE, "cat marker; ls")).toMatchObject({ ok: { output: "copy made again\nmarker\n" } });
    expect([names(copyOf(FIVE)), names(asideOf(FIVE))]).toEqual([["marker"], ["marker"]]);
  });
});
