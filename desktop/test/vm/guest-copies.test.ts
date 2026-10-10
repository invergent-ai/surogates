// A thread's copy of its folder as the app asks for it, under QEMU and KVM: Copies over VmClient and the VM
// manager in its own process, the guest's git making each copy in the folder's place in the app's data. The
// image built by images/guest/build.sh, the manager and the agent disk from this package (npm run build
// first). Behind SUROGATE_VM_TESTS=1.

import { existsSync, lstatSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, realpathSync, renameSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { BOOT_ID } from "../../src/binding/folder.js";
import { CANCELLED } from "../../src/guest/command.js";
import { Copies } from "../../src/history/copies.js";
import { keyOf } from "../../src/history/place.js";
import { FOLDER_UNAVAILABLE } from "../../src/hosts/messages.js";
import type { BoundFolder } from "../../src/hosts/tool-hosts.js";
import { forkManager, type ManagerProcess, VmClient } from "../../src/vm/client.js";
import type { HistoryRequest } from "../../src/vm/history.js";
import type { Place } from "../../src/vm/manager.js";
import { agentDisk, alive, IMAGE, KVM, needsKvm, signal, until, USER } from "./guest-support.js";

beforeAll(needsKvm);

const ONE = "0b6c1d3e-6f0a-4c1e-9a52-6a1d2c3b4e5f";
const TWO = "7d8e9f00-1a2b-4c3d-8e4f-5a6b7c8d9e0f";
const THREE = "3c4d5e6f-7a8b-4c9d-8e0f-1a2b3c4d5e6f";
// Enough files that a copy's making runs for some seconds: long enough to be cut part of the way.
const FILES = 5_000;

describe.skipIf(process.env.SUROGATE_VM_TESTS !== "1")("a thread's copy of its folder, in the guest", { timeout: 180_000 }, () => {
  let dir: string;
  let dataDir: string;
  let folder: string;
  let run: string;
  let client: VmClient;
  let copies: Copies;
  const managers: ManagerProcess[] = [];
  // What the guest was asked, and each place it was asked to let go with what it answered.
  const asked: HistoryRequest[] = [];
  const unplaced: Array<{ place: Place; gone: boolean }> = [];
  // The roots told that the copy their host works in is theirs no more; each such host lets it go.
  const replaced: string[] = [];
  const bound = (thread: string, at = folder): BoundFolder => {
    const { dev, ino } = statSync(at);
    return { folder: at, dev, ino, boot: BOOT_ID, history: thread };
  };
  const copyOf = (thread: string) => join(dataDir, "history", keyOf(folder), "threads", thread);
  const qemu = () => Number(readFileSync(join(run, "qemu.pid"), "utf8"));
  const files = (at: string) => (readdirSync(at, { recursive: true }) as string[]).filter((name) => lstatSync(join(at, name)).isFile()).sort();
  const opened = async (thread: string, at = folder) => {
    const answer = await copies.open(thread, bound(thread, at), signal());
    if (!("copy" in answer)) throw new Error(`no copy: ${JSON.stringify(answer)}`);
    return answer.copy;
  };

  beforeAll(() => {
    dir = realpathSync(mkdtempSync(join(tmpdir(), "vm-copies-")));
    dataDir = join(dir, "data");
    folder = join(dir, "Documents");
    mkdirSync(join(folder, "data"), { recursive: true });
    writeFileSync(join(folder, "Report.docx"), "the report, v1\n");
    for (let n = 0; n < FILES; n += 1) writeFileSync(join(folder, "data", `${n}.txt`), `file ${n}\n`);
    // Under $XDG_RUNTIME_DIR: a vhost-user socket's path must fit in 108 bytes.
    run = mkdtempSync(join(process.env.XDG_RUNTIME_DIR ?? "/tmp", "sg-vm-"));
    client = new VmClient({
      vm: {
        kernel: join(IMAGE, "vmlinuz"), rootfs: join(IMAGE, "rootfs.img"), agentDisk: agentDisk(dir), sessions: join(dir, "sessions.img"),
        run, console: join(dir, "console.log"), user: USER, kvm: KVM,
      },
      spawn: () => {
        const manager = forkManager();
        managers.push(manager);
        return manager;
      },
    });
    const vm = {
      history: (request: HistoryRequest, stop: AbortSignal) => {
        asked.push(request);
        return client.history(request, stop);
      },
      unplace: async (place: Place) => {
        const gone = await client.unplace(place);
        unplaced.push({ place, gone });
        return gone;
      },
    };
    copies = new Copies({
      dataDir, user: "u1", vm, idleMs: 1_500,
      replaced: (root) => {
        replaced.push(root);
        copies.close(root);
      },
    });
  });

  afterAll(async () => {
    copies?.stop();
    await client?.stop();
    for (const manager of managers) manager.kill();
    if (run) rmSync(run, { recursive: true, force: true });
    if (dir) rmSync(dir, { recursive: true, force: true });
  });

  it("makes a thread's copy in the folder's place, a folder of the app's own, and works in it with no request more", async () => {
    const copy = await opened(ONE);
    expect(copy).toEqual({
      place: { key: keyOf(folder), history: join(dataDir, "history", keyOf(folder)), real: { path: folder, ...identity(folder), boot: BOOT_ID } },
      folder: { path: copyOf(ONE), ...identity(copyOf(ONE)), boot: BOOT_ID }, at: folder,
    });
    expect(lstatSync(copyOf(ONE)).isDirectory() && lstatSync(copyOf(ONE)).uid).toBe(USER.uid);
    expect(files(copyOf(ONE))).toEqual(files(folder));
    expect(readFileSync(join(copyOf(ONE), "Report.docx"), "utf8")).toBe("the report, v1\n");
    // The app's own open does not move the copy, and a copy it has opened costs no request.
    expect(asked.map((request) => [request.thread, request.action, request.args])).toEqual([[ONE, "open", { moves: false }]]);
    expect(await opened(ONE)).toEqual(copy);
    expect(await copies.ask(ONE, bound(ONE), "snapshot", { reason: "before a step" }, signal())).toEqual({ ok: { hash: expect.stringMatching(/^[0-9a-f]{40}$/) } });
    expect(asked.map((request) => request.action)).toEqual(["open", "snapshot"]);
    copies.close(ONE);
    copies.close(ONE);
    expect(readdirSync(folder).sort()).toEqual(["Report.docx", "data"]);
  });

  it("makes again a copy whose making was cut, before anything works in it", async () => {
    const cancel = new AbortController();
    // A turn's own open, stopped as the guest makes the copy.
    const opening = copies.ask(TWO, bound(TWO), "open", {}, cancel.signal);
    await until(() => existsSync(join(dataDir, "history", keyOf(folder), "clones", TWO)), 60_000);
    cancel.abort();
    expect(await opening).toEqual(CANCELLED);
    // The app's next open has the guest make it whole.
    const copy = await opened(TWO);
    expect(files(copy.folder.path)).toEqual(files(folder));
    expect(asked.slice(-2).map((request) => [request.thread, request.action, request.args])).toEqual([[TWO, "open", {}], [TWO, "open", { moves: false }]]);
    copies.close(TWO);
  });

  it("asks a step again once its copy is made again where the history finds it not whole, and tells the host on the one before", async () => {
    const copy = await opened(ONE);
    // The thread's own work, and then its copy's index lost, as a removal cut short leaves it.
    writeFileSync(join(copy.folder.path, "Draft.md"), "the thread's own\n");
    rmSync(join(dataDir, "history", keyOf(folder), "clones", ONE, "worktrees", ONE, "index"));
    const before = asked.length;
    expect(await copies.ask(ONE, bound(ONE), "snapshot", { reason: "before a step" }, signal())).toEqual({ ok: { hash: expect.stringMatching(/^[0-9a-f]{40}$/) } });
    expect(asked.slice(before).map((request) => [request.action, request.args])).toEqual([
      ["snapshot", { reason: "before a step" }], ["open", { moves: false }], ["snapshot", { reason: "before a step" }],
    ]);
    // Another folder at the copy's path now: the host on the one before was told, and let it go.
    expect(replaced).toEqual([ONE]);
    expect(identity(copyOf(ONE))).not.toEqual({ dev: copy.folder.dev, ino: copy.folder.ino });
    // What the copy held of its own was set aside whole, by the app's own open: the turn's next open names it, as every open does.
    expect(existsSync(join(copyOf(ONE), "Draft.md"))).toBe(false);
    const turn = await copies.ask(ONE, bound(ONE), "open", {}, signal());
    expect(turn).toMatchObject({ ok: { copy: expect.any(String), set_aside_folders: [expect.stringMatching(new RegExp(`-${ONE}\\.copy$`))] } });
    const [aside] = (turn as { ok: { set_aside_folders: string[] } }).ok.set_aside_folders;
    expect(readFileSync(join(dataDir, "history", keyOf(folder), "set-aside", aside!, "Draft.md"), "utf8")).toBe("the thread's own\n");
    // The copy it works in now is the one the guest made again.
    expect((await opened(ONE)).folder).toMatchObject(identity(copyOf(ONE)));
    copies.close(ONE);
  });

  it("lets the folder's place go, and waits for it, before another folder at the path takes its key", async () => {
    const was = bound(ONE);
    const old = await opened(ONE);
    // A request of the thread's has the guest hold the place again, as a turn's steps do.
    expect(await copies.ask(ONE, was, "changed", {}, signal())).toEqual({ ok: { paths: [] } });
    renameSync(folder, `${folder}.old`);
    mkdirSync(folder);
    writeFileSync(join(folder, "Other.md"), "the new folder's\n");
    const told = replaced.length;
    const copy = await opened(THREE);
    // The host on the old folder's copy was told and let go, and the guest let go of the place it had been given.
    expect(replaced.slice(told)).toEqual([ONE]);
    expect(unplaced.at(-1)).toEqual({ place: old.place, gone: true });
    // The new folder's copy is of its own files, in a place of its own under the same key.
    expect(copy.place).toMatchObject({ key: old.place.key, real: identity(folder) });
    expect(files(copy.folder.path)).toEqual(["Other.md"]);
    expect(readdirSync(join(dataDir, "history")).filter((name) => name.includes(".was-") && !name.endsWith(".json"))).toHaveLength(1);
    // The old folder's thread works nowhere now.
    expect(await copies.open(ONE, was, signal())).toEqual({ failed: FOLDER_UNAVAILABLE });
  });

  it("lets the place go once nothing holds it, and the guest, holding nothing else, stops", async () => {
    const guest = qemu();
    copies.close(THREE);
    await until(() => unplaced.length > 0 && unplaced.at(-1)!.place.real.ino === identity(folder).ino, 30_000);
    expect(unplaced.at(-1)?.gone).toBe(true);
    await until(() => !alive(guest), 60_000);
    // A later operation adds the place again, in a guest booted for it.
    expect(await copies.ask(THREE, bound(THREE), "changed", {}, signal())).toEqual({ ok: { paths: [] } });
    expect(qemu()).not.toBe(guest);
  });
});

function identity(path: string): { dev: number; ino: number } {
  const { dev, ino } = statSync(path);
  return { dev, ino };
}
