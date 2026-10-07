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
import { type BootVm, type Folder, VmManager, type VmOptions } from "../src/vm/manager.js";

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
const fakeVm = (roots: ControlRoots): BootVm => async () => {
  const [host, guest] = duplexPair();
  const [net] = duplexPair();
  let gone = (_said: string) => {};
  const exited = new Promise<string>((resolve) => {
    gone = resolve;
  });
  const kill = async () => {
    host.destroy();
    net.destroy();
    gone("");
  };
  const control = new Control((message) => void guest.write(`${JSON.stringify(message)}\n`), roots, { setClock: async () => {}, woke: () => {}, heard: () => {}, powerOff: kill });
  createInterface({ input: guest }).on("line", (line) => control.receive(line));
  control.hello();
  return { control: host, net, exited, share: async () => ({ kind: "virtiofs", tag: "r1" }), unshare: async () => {}, kill };
};

// *answer*, or "no answer" once *ms* pass.
const within = <T>(answer: Promise<T>, ms: number) =>
  Promise.race([answer, new Promise<"no answer">((resolve) => setTimeout(() => resolve("no answer"), ms))]);

// A folder on a FUSE mount whose daemon is stopped, in a folder of its own: every look into it
// waits, as on a dead network mount. Bound first, as a chat's folder is; let go by *release*.
function stalledFolder(): { folder: Folder; release: () => void } {
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
    process.kill(pid, "SIGSTOP");
    return {
      folder: { path, dev, ino },
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
