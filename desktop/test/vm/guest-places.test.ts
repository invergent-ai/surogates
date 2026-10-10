// A folder's place in the guest, under QEMU and KVM: its history and the folder itself for the
// agent's own git, and a thread's copy as its root's share, at the folder's path. The image built
// by images/guest/build.sh, the agent disk from this package (npm run build first). Behind
// SUROGATE_VM_TESTS=1.

import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, realpathSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { readonlyFlag } from "../../src/vm/linux.js";
import { type Place, VmManager, type VmOptions } from "../../src/vm/manager.js";
import { VIRTIOFSD } from "../../src/vm/qemu.js";
import { agentDisk, agentDiskWith, altered, folderOf, IMAGE, KVM, needsKvm, OTHER, ROOT, signal, until, USER } from "./guest-support.js";

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
      run: mkdtempSync(join(process.env.XDG_RUNTIME_DIR ?? "/tmp", "sg-vm-")), console: join(dir, "console.log"), user: USER, kvm: KVM,
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
    // Both for the guest's root. Where this computer's virtiofsd can refuse writes itself, the folder's does.
    const refuses = readonlyFlag(spawnSync(VIRTIOFSD, ["--help"], { encoding: "utf8" }).stdout);
    for (const args of Object.values(started)) expect(args).toContain(`--uid-map=:0:${USER.uid}:1:`);
    expect(started[join(dir, "Documents")]!.includes("--readonly")).toBe(refuses);
    expect(started[join(dir, "store")]).not.toContain("--readonly");
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
      user: USER, kvm: KVM,
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
    // Where this computer's virtiofsd refuses writes itself, a remount of the folder changes nothing.
    const refuses = readonlyFlag(spawnSync(VIRTIOFSD, ["--help"], { encoding: "utf8" }).stdout);
    expect(readFileSync(found, "utf8").split("\n")).toEqual([
      `${AT}/history virtiofs rw,nosuid,nodev,noexec,relatime`,
      `${AT}/real virtiofs ro,nosuid,nodev,noexec,relatime`,
      "700",
      "the real report",
      "make: refused",
      "replace: refused",
      "remove: refused",
      `make again: ${refuses ? "refused" : "done"}`,
      "",
    ]);
    expect(statSync(found).uid).toBe(USER.uid);
    expect(readdirSync(join(dir, "Documents"))).toEqual(refuses ? ["Report.docx"] : ["Report.docx", "made-again"]);
    expect(readFileSync(join(dir, "Documents", "Report.docx"), "utf8")).toBe("the real report\n");
  });
});
