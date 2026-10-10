// A chat's folder on a FUSE mount whose daemon is stopped. Its own file, run alone after
// the rest (vitest.config.ts): a mount that comes or goes while bwrap binds / fails that
// sandbox's start, and srt's file hosts bind / whole.

import { spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, rmSync, statSync } from "node:fs";
import { stat } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createInterface } from "node:readline";
import { duplexPair } from "node:stream";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { Control, type ControlRoots } from "../src/guest/control.js";
import { type BootVm, type Folder, type Place, VmManager, type VmOptions } from "../src/vm/manager.js";

let dir: string;

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "vm-stalled-"));
});

afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

const options = (): VmOptions => ({
  kernel: "/i/vmlinuz", rootfs: "/i/rootfs.img", agentDisk: "/a/agent.img", sessions: join(dir, "data", "sessions.img"),
  run: join(dir, "run"), console: join(dir, "logs", "console.log"), user: { uid: 1000, gid: 1000, name: "ana", home: "/home/ana" },
});

// A VM whose control port reaches the guest's own Control on *roots*, with no QEMU; its agent powers it off at a shutdown.
// *shared*: each folder it was asked to share.
const fakeVm = (roots: ControlRoots, shared: string[] = []): BootVm => async () => {
  const [host, guest] = duplexPair();
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
  const control = new Control((message) => void guest.write(`${JSON.stringify(message)}\n`), roots, { setClock: async () => {}, woke: () => {}, heard: () => {}, powerOff: kill }, { mount: async () => {}, unmount: async () => {}, history: async () => ({ ok: {} }) });
  createInterface({ input: guest }).on("line", (line) => control.receive(line));
  control.hello();
  const share = async (folder: string) => {
    shared.push(folder);
    return { kind: "virtiofs" as const, tag: "r1" };
  };
  return { control: host, net, inbound, exited, emulated: null, share, unshare: async () => {}, kill };
};

// *answer*, or "no answer" once *ms* pass.
const within = <T>(answer: Promise<T>, ms: number) =>
  Promise.race([answer, new Promise<"no answer">((resolve) => setTimeout(() => resolve("no answer"), ms))]);

// A folder on a FUSE mount whose daemon is stopped, in a folder of its own: every look into it
// waits, as on a dead network mount. Bound first, as a chat's folder is; let go by *release*.
// With *stopped* false it answers until *stall* is called.
function stalledFolder(stopped = true): { folder: Folder; stall: () => void; release: () => void } {
  const fuse = mkdtempSync(join(dir, "fuse-"));
  for (const name of ["lower/folder", "upper", "work", "mnt"]) mkdirSync(join(fuse, name), { recursive: true });
  const mnt = join(fuse, "mnt");
  const mounted = spawnSync("fuse-overlayfs", ["-o", `lowerdir=${fuse}/lower,upperdir=${fuse}/upper,workdir=${fuse}/work,timeout=0`, mnt]);
  if (mounted.status !== 0) throw new Error(`fuse-overlayfs: ${mounted.stderr}`);
  // Lazily: a look still on its way out of the mount would keep a plain unmount from taking it.
  const unmount = () => spawnSync("fusermount3", ["-u", "-z", mnt]);
  try {
    const path = join(mnt, "folder");
    const { dev, ino } = statSync(path);
    const pid = Number(spawnSync("pgrep", ["-f", `^fuse-overlayfs .* ${mnt}$`], { encoding: "utf8" }).stdout.trim());
    const stall = () => process.kill(pid, "SIGSTOP");
    if (stopped) stall();
    return {
      folder: { path, dev, ino },
      stall,
      release: () => {
        try {
          process.kill(pid, "SIGCONT");
        } finally {
          unmount();
        }
      },
    };
  } catch (error) {
    unmount();
    throw error;
  }
}

describe("a chat's folder on a mount that does not answer", () => {
  it("is answered as unavailable within the share's bound, and its root torn down without waiting on it", async () => {
    const roots: ControlRoots = { uid: () => 10_000, setup: async () => {}, teardown: async () => {}, perform: async () => ({ ok: true }) };
    const manager = new VmManager({ ...options(), shareMs: 300 }, fakeVm(roots));
    const { folder, release } = stalledFolder();
    try {
      const begun = performance.now();
      const answer = manager.perform({ id: "1", root: "root-1", folder, kind: "which", args: {} }, new AbortController().signal);
      expect(await within(answer, 3_000)).toEqual({
        error: { type: "unavailable", message: "This computer's sandbox could not add this chat's folder: it did not answer within 0.3 s" },
      });
      expect(await within(manager.teardown("root-1"), 1_000)).toBeUndefined();
      expect(performance.now() - begun).toBeLessThan(2_000);
      await manager.stop();
    } finally {
      release();
    }
  });
});

describe("a chat's folder on a mount that does not answer, asked again and again", () => {
  it("holds none of the threads this computer's other lookups need", async () => {
    const roots: ControlRoots = { uid: () => 10_000, setup: async () => {}, teardown: async () => {}, perform: async () => ({ ok: true }) };
    const manager = new VmManager({ ...options(), shareMs: 200 }, fakeVm(roots));
    const { folder, release } = stalledFolder();
    try {
      // libuv's pool has four threads: one look each would hold them all.
      for (let n = 0; n < 4; n += 1) {
        expect(await within(manager.perform({ id: `${n}`, root: `root-${n}`, folder, kind: "which", args: {} }, new AbortController().signal), 3_000))
          .toMatchObject({ error: { type: "unavailable" } });
      }
      expect(await within(stat(dir).then(() => "answered"), 1_000)).toBe("answered");
      await manager.stop();
    } finally {
      release();
    }
  });
});

describe("a folder's place on a mount that does not answer", () => {
  const roots: ControlRoots = { uid: () => 10_000, setup: async () => {}, teardown: async () => {}, perform: async () => ({ ok: true }) };
  const KEY = "0123456789abcdef";
  // A folder that answers, in the tests' own.
  const answering = (name: string): Folder => {
    const path = join(dir, name);
    mkdirSync(path);
    const { dev, ino } = statSync(path);
    return { path, dev, ino };
  };
  const refused = (which: string, s: string) => ({
    error: { type: "unavailable", message: `This computer's sandbox could not add this folder's history: ${which} did not answer within ${s} s` },
  });

  it("is answered as unavailable within the share's bound, by which of the two did not answer, and nothing of it is shared", async () => {
    const shared: string[] = [];
    const manager = new VmManager({ ...options(), shareMs: 300 }, fakeVm(roots, shared));
    const { folder, release } = stalledFolder();
    try {
      const begun = performance.now();
      const place: Place = { key: KEY, history: answering("store").path, real: folder };
      expect(await within(manager.place(place, new AbortController().signal), 3_000)).toEqual(refused("the folder", "0.3"));
      // The app's data on such a mount: the folder answers, and its history does not.
      const other: Place = { key: KEY, history: folder.path, real: answering("Documents") };
      expect(await within(manager.place(other, new AbortController().signal), 3_000)).toEqual(refused("its place in the app's data", "0.3"));
      expect(performance.now() - begun).toBeLessThan(2_000);
      expect(shared).toEqual([]);
      await manager.stop();
    } finally {
      release();
    }
  });

  it("is not answered as held once its history's mount stops answering, within the share's bound", async () => {
    const manager = new VmManager({ ...options(), shareMs: 300 }, fakeVm(roots));
    const { folder, stall, release } = stalledFolder(false);
    try {
      const place: Place = { key: KEY, history: folder.path, real: answering("Documents") };
      expect(await within(manager.place(place, new AbortController().signal), 3_000)).toBeNull();
      stall();
      const begun = performance.now();
      expect(await within(manager.place(place, new AbortController().signal), 3_000)).toEqual(refused("its place in the app's data", "0.3"));
      expect(performance.now() - begun).toBeLessThan(2_000);
      await manager.stop();
    } finally {
      release();
    }
  });

  it("holds none of the threads this computer's other lookups need, asked again and again", async () => {
    const manager = new VmManager({ ...options(), shareMs: 200 }, fakeVm(roots));
    const { folder, release } = stalledFolder();
    try {
      const store = answering("store").path;
      for (let n = 0; n < 4; n += 1) {
        expect(await within(manager.place({ key: KEY, history: store, real: folder }, new AbortController().signal), 3_000)).toEqual(refused("the folder", "0.2"));
      }
      expect(await within(stat(dir).then(() => "answered"), 1_000)).toBe("answered");
      await manager.stop();
    } finally {
      release();
    }
  });
});
