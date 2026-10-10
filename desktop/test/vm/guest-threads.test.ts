// Project threads on folders of this computer, through the app's executor, under QEMU and KVM: the operations
// as the device link delivers them; each thread's file tools in a host of the app's own on its copy, its helper
// in its sandbox; its commands in the guest, which shares the copy at the folder's path; and each copy made by
// the guest's git, in the folder's place in the app's data. Each folder is compared, as it lies, around every
// operation. Behind SUROGATE_VM_TESTS=1: the image built by images/guest/build.sh, and npm run build and npm run
// agent-disk first.

import { existsSync, lstatSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, realpathSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { BOOT_ID } from "../../src/binding/folder.js";
import { keyOf } from "../../src/history/place.js";
import type { HostStart } from "../../src/hosts/messages.js";
import { forkHost } from "../../src/hosts/tool-hosts.js";
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
