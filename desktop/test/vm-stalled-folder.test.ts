// A chat's folder on a FUSE mount whose daemon is stopped. Its own file, run alone after
// the rest (vitest.config.ts): a mount that comes or goes while bwrap binds / fails that
// sandbox's start, and srt's file hosts bind / whole.

import { spawn, spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, rmSync, statSync, writeFileSync } from "node:fs";
import { stat } from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { createInterface } from "node:readline";
import { duplexPair } from "node:stream";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { Binder, type FolderSheet } from "../src/binding/binder.js";
import { BOOT_ID } from "../src/binding/folder.js";
import { Control, type ControlRoots } from "../src/guest/control.js";
import { HookGuard } from "../src/hosts/hooks.js";
import { fileToolsMissing, pathOutside } from "../src/hosts/policy.js";
import { OperationJournal } from "../src/journal/journal.js";
import { FolderLooks } from "../src/shell/folders-held.js";
import { missingTools } from "../src/vm/linux.js";
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
  const control = new Control((message) => void guest.write(`${JSON.stringify(message)}\n`), roots, { setClock: async () => {}, woke: () => {}, heard: () => {}, powerOff: kill, trust: async () => {} }, { mount: async () => {}, unmount: async () => {}, history: async () => ({ ok: {} }), stop: async () => {} });
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

// *act*'s answer, and the longest this thread went without running its timers while it ran.
async function stalling<T>(act: () => Promise<T>): Promise<{ answer: T; longest: number }> {
  let last = performance.now();
  let longest = 0;
  const tick = setInterval(() => {
    longest = Math.max(longest, performance.now() - last);
    last = performance.now();
  }, 20);
  try {
    const answer = await act();
    return { answer, longest: Math.max(longest, performance.now() - last) };
  } finally {
    clearInterval(tick);
  }
}

// A folder on a FUSE mount whose daemon is stopped, in a folder of its own: every look into it
// waits, as on a dead network mount. Bound first, as a chat's folder is; let go by *release*.
// With *stopped* false it answers until *stall* is called. *wakeIn*: it answers again in *ms*, from
// another process, should this one wait on it.
function stalledFolder(stopped = true): { folder: Folder; stall: () => void; wakeIn: (ms: number) => void; release: () => void } {
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
      wakeIn: (ms) => spawn("/bin/sh", ["-c", `sleep ${ms / 1000}; kill -CONT ${pid}`], { stdio: "ignore", detached: true }).unref(),
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

describe("a new chat's folder on a mount that does not answer", () => {
  let journal: OperationJournal;
  let home: string;
  let sheets: FolderSheet[];
  beforeEach(() => {
    journal = new OperationJournal(join(dir, "journal.db"));
    home = join(dir, "home");
    mkdirSync(home);
    sheets = [];
  });
  afterEach(() => journal.close());
  // The binder, its dialog picking *picked*, and its sheet cancelled once seen; its folders looked at by *looks*.
  const binder = (picked: string | null = null, looks = new FolderLooks(undefined, 300)) => new Binder({
    bindings: journal.bindings,
    prompts: {
      pickFolder: async () => picked,
      confirmFolder: async (sheet) => {
        sheets.push(sheet);
        return null;
      },
    },
    guards: { home, dataDir: join(dir, "data"), cacheDir: join(dir, "cache"), appDirs: [] },
    agent: "Research assistant",
    hosts: { run: async () => ({ ok: null }) },
    approvalPrompts: { approve: async () => "deny", confirmFreeMode: async () => false },
    answers: (path) => looks.answers(path),
  });

  it("as one that answers, while the app's looks wait on two bound folders that do not: taken, and its PATH entry looked on", async () => {
    const [one, two] = [stalledFolder(), stalledFolder()];
    try {
      const looks = new FolderLooks(undefined, 300);
      expect(await looks.held([one.folder.path, two.folder.path])).toEqual([]);
      const healthy = join(dir, "healthy");
      mkdirSync(healthy);
      const { answer, longest } = await stalling(() => binder(healthy, looks).prepareFolder("pick", "window-1", new AbortController().signal));
      expect(answer).toBeNull();
      expect(sheets.map(({ folder: shown, refusal }) => [shown, refusal])).toEqual([[healthy, null]]);
      expect(await looks.path(`/usr/bin:${healthy}:/bin`)).toBe(`/usr/bin:${healthy}:/bin`);
      expect(longest).toBeLessThan(1_000);
    } finally {
      one.release();
      two.release();
    }
  });

  it("as the last chat's: a folder of its own is offered once the look's bound has passed, nothing on this thread waiting on the mount", async () => {
    const { folder, wakeIn, release } = stalledFolder();
    wakeIn(6_000);
    try {
      journal.bindings.add({ root: "root-1", nonce: "n", folder: folder.path, dev: folder.dev, ino: folder.ino, boot: BOOT_ID, mode: "free", boundAt: Date.now() });
      const { answer, longest } = await stalling(() => binder().prepareFolder("last", "window-1", new AbortController().signal));
      expect(answer).toBeNull();
      expect(sheets.map((sheet) => sheet.folder)).toEqual([expect.stringMatching(new RegExp(`^${home}/Surogate/Research-assistant/`))]);
      expect(longest).toBeLessThan(1_000);
    } finally {
      release();
    }
  });

  it("as picked in the dialog: the sheet says it did not answer, once the look's bound has passed, nothing on this thread waiting on the mount", async () => {
    const { folder, wakeIn, release } = stalledFolder();
    wakeIn(6_000);
    try {
      const { answer, longest } = await stalling(() => binder(folder.path).prepareFolder("pick", "window-1", new AbortController().signal));
      expect(answer).toBeNull();
      expect(sheets.map(({ folder: shown, refusal }) => [shown, refusal])).toEqual([[folder.path, `the folder ${folder.path} did not answer`]]);
      expect(longest).toBeLessThan(1_000);
    } finally {
      release();
    }
  });
});

describe("a paused rebase's todo on a mount that stops answering", () => {
  it("is read for the next command with nothing on this thread waiting on the mount, and the command goes on once it answers", async () => {
    const { folder, stall, wakeIn, release } = stalledFolder(false);
    try {
      const todo = join(folder.path, ".git", "rebase-merge", "git-rebase-todo");
      mkdirSync(dirname(todo), { recursive: true });
      writeFileSync(todo, "pick abc one\nexec make test\n");
      const guard = new HookGuard(folder.path);
      // Its looks find the todo while the mount answers; the command's refusal then reads the user's steps in it.
      await guard.watch();
      stall();
      wakeIn(2_000);
      const { answer, longest } = await stalling(() => guard.refusal());
      expect(answer).toBeNull();
      expect(longest).toBeLessThan(1_000);
    } finally {
      release();
    }
  });
});

describe("the app's PATH with an entry on a mount that does not answer", () => {
  it("is looked on for the app's tools without that entry once the look's bound has passed, nothing on this thread waiting on the mount", async () => {
    const { folder, wakeIn, release } = stalledFolder();
    wakeIn(6_000);
    const app = process.env.PATH;
    process.env.PATH = `/usr/bin:${folder.path}:/bin`;
    try {
      const said: string[][] = [];
      const looks = new FolderLooks(undefined, 300, (entry, why) => said.push([entry, why]));
      const { answer, longest } = await stalling(async () => {
        const path = await looks.path(process.env.PATH);
        fileToolsMissing(undefined, pathOutside(path, []));
        await missingTools({}, false, [], path);
        return path;
      });
      expect(answer).toBe("/usr/bin:/bin");
      expect(longest).toBeLessThan(1_000);
      // Said to the log; and its look, killed at its bound, waits on the mount no longer than that.
      expect(said).toEqual([[folder.path, "silent"]]);
      await expect.poll(() => spawnSync("pgrep", ["-f", `^/usr/bin/stat -L --printf= -- ${folder.path}$`]).status, { timeout: 2_000 }).toBe(1);
    } finally {
      process.env.PATH = app;
      release();
    }
  });
});
