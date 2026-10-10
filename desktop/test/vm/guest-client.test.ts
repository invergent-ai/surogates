// A folder's place and its history in the guest, under QEMU and KVM, as the app's main process reaches them: through
// VmClient and the VM manager in its own process. The image built by images/guest/build.sh, the manager and the agent
// disk from this package (npm run build first). Behind SUROGATE_VM_TESTS=1.

import { existsSync, lstatSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { CANCELLED, SANDBOX_STOPPED } from "../../src/guest/command.js";
import { forkManager, type ManagerProcess, VmClient } from "../../src/vm/client.js";
import type { Place } from "../../src/vm/manager.js";
import { agentDisk, alive, folderOf, IMAGE, KVM, needsKvm, signal, until, USER } from "./guest-support.js";

beforeAll(needsKvm);

const KEY = "0123456789abcdef";
const ONE = "0b6c1d3e-6f0a-4c1e-9a52-6a1d2c3b4e5f";
const TWO = "7d8e9f00-1a2b-4c3d-8e4f-5a6b7c8d9e0f";
const THREE = "3c4d5e6f-7a8b-4c9d-8e0f-1a2b3c4d5e6f";
// Enough files that a thread's first open runs for some seconds: long enough to be stopped part of the way.
const FILES = 5_000;

describe.skipIf(process.env.SUROGATE_VM_TESTS !== "1")("a folder's place and its history, through the VM manager's own process", { timeout: 120_000 }, () => {
  let dir: string;
  let folder: string;
  let store: string;
  let run: string;
  let place: Place;
  let vm: VmClient;
  // Each manager the client started, in its own process.
  const managers: ManagerProcess[] = [];
  const ask = (thread: string, action: string, stop = signal()) => vm.history({ place, thread, user: "u1", action, args: {} }, stop);
  const qemu = () => Number(readFileSync(join(run, "qemu.pid"), "utf8"));
  const shares = () => readdirSync(run).filter((name) => /^vfs-\d+\.pid$/.test(name));
  // Every file and folder under *at*, with its size: what was written there.
  const tree = (at: string) => (readdirSync(at, { recursive: true }) as string[]).sort().map((name) => `${name} ${lstatSync(join(at, name)).size}`);
  const pause = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

  beforeAll(() => {
    dir = realpathSync(mkdtempSync(join(tmpdir(), "vm-client-")));
    folder = join(dir, "Documents");
    store = join(dir, "store");
    mkdirSync(join(folder, "data"), { recursive: true });
    mkdirSync(store);
    writeFileSync(join(folder, "Report.docx"), "the report, v1\n");
    for (let n = 0; n < FILES; n += 1) writeFileSync(join(folder, "data", `${n}.txt`), `file ${n}\n`);
    // Under $XDG_RUNTIME_DIR: a vhost-user socket's path must fit in 108 bytes.
    run = mkdtempSync(join(process.env.XDG_RUNTIME_DIR ?? "/tmp", "sg-vm-"));
    vm = new VmClient({
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
    place = { key: KEY, history: store, real: folderOf(folder) };
  });

  afterAll(async () => {
    await vm?.stop();
    for (const manager of managers) manager.kill();
    if (run) rmSync(run, { recursive: true, force: true });
    if (dir) rmSync(dir, { recursive: true, force: true });
  });

  it("adds a folder's place to the guest it boots, and its history makes a thread's copy there, the folder as it was", async () => {
    expect(await vm.place(place, signal())).toBeNull();
    expect(managers).toHaveLength(1);
    // The history and the folder, each shared with the guest.
    expect(shares()).toHaveLength(2);
    expect(await ask(ONE, "open")).toEqual({ ok: { copy: "made" } });
    expect(readFileSync(join(store, "threads", ONE, "Report.docx"), "utf8")).toBe("the report, v1\n");
    expect(readdirSync(join(store, "threads", ONE, "data"))).toHaveLength(FILES);
    // Its next turn: a copy with nothing unlanded moves to the folder as its history has it now.
    expect(await ask(ONE, "open")).toEqual({ ok: { copy: "moved" } });
    expect(readdirSync(folder).sort()).toEqual(["Report.docx", "data"]);
  });

  it("stops a request cancelled in the main process in the guest too: the copy it was making is not made, and nothing of it writes the place after", async () => {
    const cancel = new AbortController();
    const opening = ask(TWO, "open", cancel.signal);
    // It runs in the guest: the thread's repository is begun in the place, and its files are being read.
    await until(() => existsSync(join(store, "clones", TWO)), 60_000);
    const begun = performance.now();
    cancel.abort();
    expect(await opening).toEqual(CANCELLED);
    expect(performance.now() - begun).toBeLessThan(500);
    // What the guest's git had written lands within its end; after that nothing more does.
    await pause(1_000);
    const settled = tree(store);
    await pause(1_000);
    expect(tree(store)).toEqual(settled);
    // Its first open never ended: the next makes the copy again, whole.
    expect(await ask(TWO, "open")).toEqual({ ok: { copy: "made" } });
    expect(readdirSync(join(store, "threads", TWO, "data"))).toHaveLength(FILES);
  });

  it("answers a request whose manager's process died as stopped by the sandbox, takes the place for held no longer, and starts a manager again for the next", async () => {
    const guest = qemu();
    const opening = ask(THREE, "open");
    await until(() => existsSync(join(store, "clones", THREE)), 60_000);
    managers[0]!.kill();
    expect(await opening).toEqual(SANDBOX_STOPPED);
    // Its guest went with it, and the place with the guest: nothing is let go, and no manager is started to ask.
    await until(() => !alive(guest));
    expect(await vm.unplace(place)).toBe(false);
    expect(managers).toHaveLength(1);
    // The next request starts another manager, once its backoff has passed, which boots another guest and adds the place again.
    expect(await ask(THREE, "open")).toEqual({ ok: { copy: "made" } });
    expect(managers).toHaveLength(2);
    expect(qemu()).not.toBe(guest);
    expect(await ask(ONE, "open")).toEqual({ ok: { copy: "moved" } });
  });

  it("lets the place go, its two shares with it, and the guest, which holds nothing else, stops", async () => {
    const guest = qemu();
    expect(shares()).toHaveLength(2);
    expect(await vm.unplace(place)).toBe(true);
    expect(shares()).toHaveLength(0);
    await until(() => !alive(guest), 30_000);
    // Held no longer, it is let go no more; the manager's process runs on.
    expect(await vm.unplace(place)).toBe(false);
    expect(managers).toHaveLength(2);
    expect(readdirSync(folder).sort()).toEqual(["Report.docx", "data"]);
  });
});
