// A folder's place in the guest, under QEMU and KVM: its history and the folder itself for the
// agent's own git, and a thread's copy as its root's share, at the folder's path. The image built
// by images/guest/build.sh, the agent disk from this package (npm run build first). Behind
// SUROGATE_VM_TESTS=1.

import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, realpathSync, rmSync, statSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { findOnPath } from "../../src/files/operations.js";
import { readonlyFlag } from "../../src/vm/linux.js";
import { type Place, VmManager, type VmOptions } from "../../src/vm/manager.js";
import { VIRTIOFSD } from "../../src/vm/qemu.js";
import { agentDisk, agentDiskWith, altered, DAEMON, folderOf, IMAGE, KVM, needsKvm, OTHER, ROOT, signal, until, USER } from "./guest-support.js";

beforeAll(needsKvm);

const KEY = "0123456789abcdef";
const AT = `/run/surogate/places/${KEY}`;
// The guest's own root, beside the agent and in its mount namespace, doing in a place what the agent's
// git will: once both mounts are there it says how each is mounted, writes the history, and tries the
// folder, a remount aside. What it found is a file in the history, which this computer reads.
const AGENT = "exec /usr/bin/tini -- /usr/bin/node /run/surogate/agent/guest/agent.js";
const AS_ROOT = `(
  until mountpoint -q ${AT}/real; do sleep 0.1; done
  tried() { "$@" 2>/dev/null && echo done || echo refused; }
  {
    findmnt -rn -o TARGET,FSTYPE,OPTIONS ${AT}/history
    findmnt -rn -o TARGET,FSTYPE,OPTIONS ${AT}/real
    stat -c %a /run/surogate/places
    cat ${AT}/real/Report.docx
    echo "make: $(tried cp /dev/null ${AT}/real/made)"
    echo "replace: $(tried cp /dev/null ${AT}/real/Report.docx)"
    echo "remove: $(tried rm ${AT}/real/Report.docx)"
    mount -o remount,rw ${AT}/real
    echo "make again: $(tried cp /dev/null ${AT}/real/made-again)"
  } > ${AT}/history/found.partial 2>&1
  mv ${AT}/history/found.partial ${AT}/history/found
) &
`;

describe.skipIf(process.env.SUROGATE_VM_TESTS !== "1")("a folder's place in the guest", { timeout: 60_000 }, () => {
  let dir: string;
  let options: VmOptions;
  let manager: VmManager;
  let place: Place;
  const copyOf = (thread: string) => join(dir, "store", "threads", thread);
  // A command of *root*'s, whose share is *thread*'s copy, at the folder's path.
  const run = (root: string, thread: string, command: string) => manager.perform({
    id: `run-${Math.random()}`, root, folder: folderOf(copyOf(thread)), at: join(dir, "Documents"), kind: "run",
    args: { command, workdir: null, timeout: 20 },
  }, signal());
  // Each share's virtiofsd, by the folder it serves, as it was started.
  const daemons = () => Object.fromEntries(readdirSync(options.run).filter((name) => /^vfs-\d+\.pid$/.test(name)).map((name) => {
    const args = readFileSync(`/proc/${readFileSync(join(options.run, name), "utf8")}/cmdline`, "utf8").split("\0");
    return [args.find((arg) => arg.startsWith("--shared-dir="))!.slice("--shared-dir=".length), args];
  }));

  beforeAll(async () => {
    dir = realpathSync(mkdtempSync(join(tmpdir(), "vm-places-")));
    mkdirSync(join(dir, "Documents"));
    writeFileSync(join(dir, "Documents", "Report.docx"), "the real report\n");
    for (const thread of ["one", "two"]) {
      mkdirSync(copyOf(thread), { recursive: true });
      writeFileSync(join(copyOf(thread), "Report.docx"), `${thread}'s copy\n`);
    }
    options = {
      kernel: join(IMAGE, "vmlinuz"), rootfs: join(IMAGE, "rootfs.img"), agentDisk: agentDisk(dir), sessions: join(dir, "sessions.img"),
      run: mkdtempSync(join(process.env.XDG_RUNTIME_DIR ?? "/tmp", "sg-vm-")), console: join(dir, "console.log"), user: USER, kvm: KVM, ...DAEMON,
    };
    manager = new VmManager(options);
    place = { key: KEY, history: join(dir, "store"), real: folderOf(join(dir, "Documents")) };
  });

  afterAll(async () => {
    await manager.stop();
    rmSync(options.run, { recursive: true, force: true });
    rmSync(dir, { recursive: true, force: true });
  });

  it("shares the folder's history with the guest's root, and the folder itself read-only", async () => {
    expect(await manager.place(place, signal())).toBeNull();
    const started = daemons();
    expect(Object.keys(started).sort()).toEqual([join(dir, "Documents"), join(dir, "store")]);
    // Both for the guest's root: the history by its daemon's map; the folder from bwrap's namespaces, bound read-only there,
    // in a mount namespace of its own past them, and refused every write by its daemon too where this computer's has the option.
    const folder = join(dir, "Documents");
    const refuses = readonlyFlag(spawnSync(process.env.SUROGATE_VM_VIRTIOFSD ?? VIRTIOFSD, ["--help"], { encoding: "utf8" }).stdout);
    expect(started[join(dir, "store")]).toContain(`--uid-map=:0:${USER.uid}:1:`);
    expect(started[join(dir, "store")]).not.toContain("--readonly");
    expect(started[folder]!.join(" ")).toContain(`--ro-bind ${folder} ${folder} --die-with-parent -- /usr/bin/unshare --mount --propagation private --`);
    expect(started[folder]!.includes("--readonly")).toBe(refuses);
  });

  it("binds a thread's copy at its folder's path, where its commands see no other thread's copy, the history or the real files", async () => {
    const seen = await run(ROOT, "one", "pwd; cat Report.docx; ls /run/surogate; echo mine > made.txt; findmnt -rn -o TARGET | grep -c ^/run/surogate/places");
    expect(seen).toEqual({
      ok: { output: `${join(dir, "Documents")}\none's copy\nagent\nnet.sock\n0\n`, returncode: 1, timed_out: false },
    });
    // What it wrote is in its copy on this computer, as this user's, and nowhere else.
    expect(readFileSync(join(copyOf("one"), "made.txt"), "utf8")).toBe("mine\n");
    expect(statSync(join(copyOf("one"), "made.txt")).uid).toBe(USER.uid);
    expect(readdirSync(join(dir, "Documents"))).toEqual(["Report.docx"]);
    expect(readdirSync(copyOf("two"))).toEqual(["Report.docx"]);
    // A second thread on the folder, at the same path, sees its own copy alone.
    expect(await run(OTHER, "two", "cat Report.docx; ls")).toEqual({
      ok: { output: "two's copy\nReport.docx\n", returncode: 0, timed_out: false },
    });
    // No path of a command's leads to the store: its own copy is all of it that it holds.
    const found = await run(ROOT, "one", `ls ${join(dir, "store")} ${join(dir, "store", "threads", "two")} 2>&1; cat /proc/self/mountinfo | grep -c virtiofs`);
    expect(found).toMatchObject({ ok: { returncode: 0 } });
    expect((found as { ok: { output: string } }).ok.output).toMatch(/No such file or directory[\s\S]*No such file or directory[\s\S]*\n1\n$/);
    expect(readFileSync(join(dir, "Documents", "Report.docx"), "utf8")).toBe("the real report\n");
  });

  it("shows a thread's commands the names of the guest's shares and no more of a place: none can be mounted by them", async () => {
    // The guest's four shares, by their tags, in the order they were made: the place's two, and each thread's copy.
    const tags = await run(ROOT, "one", "cat /sys/fs/virtiofs/*/tag; grep virtiofs /proc/self/mountinfo | cut -d' ' -f5,8-");
    expect(tags).toEqual({ ok: { output: `r1\nr2\nr3\nr4\n${join(dir, "Documents")} virtiofs r3 rw\n`, returncode: 0, timed_out: false } });
    // The history's, the folder's and the other thread's copy: as itself, and as the root of a namespace of its own.
    const mounting = ["r1", "r2", "r4"].flatMap((tag) => [`mount -t virtiofs ${tag} /tmp/m`, `unshare -Urm mount -t virtiofs ${tag} /tmp/m`]);
    const tried = await run(ROOT, "one", `mkdir /tmp/m; ${mounting.map((mount) => `${mount} 2>/dev/null; echo $?`).join("; ")}; ls -A /tmp/m | wc -l`);
    expect(tried).toEqual({ ok: { output: "32\n1\n32\n1\n32\n1\n0\n", returncode: 0, timed_out: false } });
  });

  it("lets the place go, its two shares with it, and takes it again", async () => {
    await manager.unplace(place);
    expect(Object.keys(daemons()).sort()).toEqual([copyOf("one"), copyOf("two")]);
    // Nothing of the folder or of its history went with the place, and nothing was put in the folder.
    expect(readdirSync(join(dir, "Documents"))).toEqual(["Report.docx"]);
    expect(readFileSync(join(dir, "Documents", "Report.docx"), "utf8")).toBe("the real report\n");
    expect(readdirSync(join(dir, "store"))).toEqual(["threads"]);
    expect(readdirSync(copyOf("one")).sort()).toEqual(["Report.docx", "made.txt"]);
    expect(await manager.place(place, signal())).toBeNull();
    expect(Object.keys(daemons()).sort()).toEqual([join(dir, "Documents"), join(dir, "store"), copyOf("one"), copyOf("two")].sort());
    // A root set up before the place was added holds none of its mounts either.
    expect(await run(ROOT, "one", "cat made.txt; ls /run/surogate; grep -c virtiofs /proc/self/mountinfo")).toEqual({
      ok: { output: "mine\nagent\nnet.sock\n1\n", returncode: 0, timed_out: false },
    });
  });

  it("is found again in the next guest, booted on other disks, by what this computer keeps of it alone", async () => {
    await manager.stop();
    // Nothing of the guest's own is kept: its agent disk and its sessions disk are new ones.
    mkdirSync(join(dir, "next"));
    options = {
      ...options, agentDisk: agentDisk(join(dir, "next")), sessions: join(dir, "next", "sessions.img"),
      run: mkdtempSync(join(process.env.XDG_RUNTIME_DIR ?? "/tmp", "sg-vm-")), console: join(dir, "next", "console.log"),
    };
    manager = new VmManager(options);
    expect(await manager.place(place, signal())).toBeNull();
    expect(Object.keys(daemons()).sort()).toEqual([join(dir, "Documents"), join(dir, "store")]);
    // The thread's copy holds what its commands wrote in the guest that went.
    expect(await run(ROOT, "one", "cat made.txt Report.docx; ls")).toEqual({
      ok: { output: "mine\none's copy\nReport.docx\nmade.txt\n", returncode: 0, timed_out: false },
    });
    expect(readdirSync(join(dir, "Documents"))).toEqual(["Report.docx"]);
    expect(readFileSync(join(dir, "Documents", "Report.docx"), "utf8")).toBe("the real report\n");
  });

  it("shares no folder read-only where no bubblewrap is to be found, and lets its history's share go", async () => {
    for (const name of ["Letters", "letters-store"]) mkdirSync(join(dir, name));
    // A PATH with virtiofsd's id-map tools on it and no bwrap; the app's own copy is named to no development build.
    const bin = join(dir, "bin");
    mkdirSync(bin);
    for (const name of ["newuidmap", "newgidmap"]) symlinkSync(findOnPath(name, process.env.PATH, "/")!, join(bin, name));
    const path = process.env.PATH;
    process.env.PATH = bin;
    try {
      expect(await manager.place({ key: "fedcba9876543210", history: join(dir, "letters-store"), real: folderOf(join(dir, "Letters")) }, signal())).toEqual({
        error: { type: "unavailable", message: expect.stringContaining("the folder could not be shared read-only: bubblewrap is not on the PATH") },
      });
    } finally {
      process.env.PATH = path;
    }
    expect(Object.keys(daemons()).filter((folder) => [join(dir, "Letters"), join(dir, "letters-store")].includes(folder))).toEqual([]);
  });

  it("says what holds each of its folders where it takes no more: a place takes two", async () => {
    // The place and one thread's copy are in; five chats more fill it, and a sixth finds no room.
    expect(Object.keys(daemons()).sort()).toEqual([join(dir, "Documents"), join(dir, "store"), copyOf("one")].sort());
    const chats = Array.from({ length: 6 }, (_, n) => {
      const path = join(dir, `chat-${n}`);
      mkdirSync(path);
      return [`chat-${n}`, path] as const;
    });
    const ran = (root: string, path: string) => manager.perform({ id: `run-${root}`, root, folder: folderOf(path), kind: "run", args: { command: "true", workdir: null, timeout: 20 } }, signal());
    for (const [root, path] of chats.slice(0, 5)) expect(await ran(root, path)).toMatchObject({ ok: { returncode: 0 } });
    expect(await ran(...chats[5]!)).toEqual({
      error: {
        type: "unavailable",
        message: "This computer's sandbox could not add this chat's folder: it holds as many folders as it has room for, 8: the folders of 6 chats, and 2 for the histories of project folders, which take two each",
      },
    });
    for (const [root] of chats) await manager.teardown(root);
  });
});

describe.skipIf(process.env.SUROGATE_VM_TESTS !== "1")("a folder's place, to the guest's own root", { timeout: 60_000 }, () => {
  let dir: string;
  let options: VmOptions;
  let manager: VmManager;

  beforeAll(() => {
    dir = realpathSync(mkdtempSync(join(tmpdir(), "vm-places-")));
    for (const name of ["Documents", "store"]) mkdirSync(join(dir, name));
    writeFileSync(join(dir, "Documents", "Report.docx"), "the real report\n");
    options = {
      kernel: join(IMAGE, "vmlinuz"), rootfs: join(IMAGE, "rootfs.img"), agentDisk: agentDiskWith(dir, altered([AGENT, `${AS_ROOT}${AGENT}`])),
      sessions: join(dir, "sessions.img"), run: mkdtempSync(join(process.env.XDG_RUNTIME_DIR ?? "/tmp", "sg-vm-")), console: join(dir, "console.log"),
      user: USER, kvm: KVM, ...DAEMON,
    };
    manager = new VmManager(options);
  });

  afterAll(async () => {
    await manager.stop();
    rmSync(options.run, { recursive: true, force: true });
    rmSync(dir, { recursive: true, force: true });
  });

  it("is its history for writing, as this user's files, and the folder for reading alone, with no program to run in either", async () => {
    expect(await manager.place({ key: KEY, history: join(dir, "store"), real: folderOf(join(dir, "Documents")) }, signal())).toBeNull();
    const found = join(dir, "store", "found");
    await until(() => existsSync(found));
    // A remount of the folder changes nothing: it is read-only on this computer too, whatever its virtiofsd.
    expect(readFileSync(found, "utf8").split("\n")).toEqual([
      `${AT}/history virtiofs rw,nosuid,nodev,noexec,relatime`,
      `${AT}/real virtiofs ro,nosuid,nodev,noexec,relatime`,
      "700",
      "the real report",
      "make: refused",
      "replace: refused",
      "remove: refused",
      "make again: refused",
      "",
    ]);
    expect(statSync(found).uid).toBe(USER.uid);
    expect(readdirSync(join(dir, "Documents"))).toEqual(["Report.docx"]);
    expect(readFileSync(join(dir, "Documents", "Report.docx"), "utf8")).toBe("the real report\n");
  });
});

// The guest's own root again, once this computer says it has mounted something in the folder after the
// share began (go, in the history): what it tries there, past a remount, a mount made before the share too.
const AS_ROOT_ON_MOUNTS = `(
  until mountpoint -q ${AT}/real && [ -e ${AT}/history/go ]; do sleep 0.1; done
  tried() { "$@" 2>/dev/null && echo done || echo refused; }
  {
    mount -o remount,rw ${AT}/real && echo remounted
    echo "top: $(tried cp /dev/null ${AT}/real/made)"
    echo "before: $(tried cp /dev/null ${AT}/real/before/made)"
    echo "after: $(tried cp /dev/null ${AT}/real/after/made)"
  } > ${AT}/history/found.partial 2>&1
  mv ${AT}/history/found.partial ${AT}/history/found
) &
`;

describe.skipIf(process.env.SUROGATE_VM_TESTS !== "1")("a folder's place, to the guest's own root, with drives mounted in the folder", { timeout: 60_000 }, () => {
  let dir: string;
  let options: VmOptions;
  let manager: VmManager;
  const mounted: string[] = [];
  // A FUSE mount at *at* in the folder, as a drive, a NAS's share or an sshfs folder would be: its own files, writable.
  const mountAt = (at: string) => {
    const fuse = mkdtempSync(join(dir, "fuse-"));
    for (const name of ["lower", "upper", "work"]) mkdirSync(join(fuse, name));
    mkdirSync(at);
    const made = spawnSync("fuse-overlayfs", ["-o", `lowerdir=${fuse}/lower,upperdir=${fuse}/upper,workdir=${fuse}/work`, at], { encoding: "utf8" });
    if (made.status !== 0) throw new Error(`fuse-overlayfs: ${made.stderr}`);
    mounted.push(at);
  };

  beforeAll(() => {
    dir = realpathSync(mkdtempSync(join(tmpdir(), "vm-places-")));
    for (const name of ["Documents", "store"]) mkdirSync(join(dir, name));
    options = {
      kernel: join(IMAGE, "vmlinuz"), rootfs: join(IMAGE, "rootfs.img"), agentDisk: agentDiskWith(dir, altered([AGENT, `${AS_ROOT_ON_MOUNTS}${AGENT}`])),
      sessions: join(dir, "sessions.img"), run: mkdtempSync(join(process.env.XDG_RUNTIME_DIR ?? "/tmp", "sg-vm-")), console: join(dir, "console.log"),
      user: USER, kvm: KVM, ...DAEMON,
    };
    manager = new VmManager(options);
  });

  afterAll(async () => {
    await manager.stop();
    for (const at of mounted) spawnSync("fusermount3", ["-u", "-z", at]);
    rmSync(options.run, { recursive: true, force: true });
    rmSync(dir, { recursive: true, force: true });
  });

  it("refuses the guest's root a write past a remount, on a drive mounted in the folder before its share began or after", async () => {
    const folder = join(dir, "Documents");
    mountAt(join(folder, "before"));
    expect(await manager.place({ key: KEY, history: join(dir, "store"), real: folderOf(folder) }, signal())).toBeNull();
    mountAt(join(folder, "after"));
    writeFileSync(join(dir, "store", "go"), "");
    const found = join(dir, "store", "found");
    await until(() => existsSync(found));
    expect(readFileSync(found, "utf8").split("\n")).toEqual(["remounted", "top: refused", "before: refused", "after: refused", ""]);
    expect([readdirSync(folder).sort(), readdirSync(join(folder, "before")), readdirSync(join(folder, "after"))]).toEqual([["after", "before"], [], []]);
  });
});
