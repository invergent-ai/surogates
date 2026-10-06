// The guest under QEMU and KVM, booted by the VM manager: the image built by
// images/guest/build.sh, the agent disk built from this package (npm run build
// first). Behind SUROGATE_VM_TESTS=1; SUROGATE_VM_IMAGE names another image folder.

import { spawnSync } from "node:child_process";
import { closeSync, existsSync, mkdirSync, mkdtempSync, openSync, readFileSync, readSync, realpathSync, rmSync, statSync, watch, writeFileSync } from "node:fs";
import { tmpdir, userInfo } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { BOOT_ID } from "../../src/binding/folder.js";
import { CANCELLED, SANDBOX_STOPPED } from "../../src/guest/command.js";
import type { HostUser, Share } from "../../src/guest/protocol.js";
import { FOLDER_UNAVAILABLE } from "../../src/hosts/messages.js";
import type { Operation } from "../../src/link/protocol.js";
import { vmOptions } from "../../src/vm/client.js";
import { VmExecutor } from "../../src/vm/executor.js";
import { bootLinux } from "../../src/vm/linux.js";
import { type Folder, Guest, VmManager, type VmOptions } from "../../src/vm/manager.js";

const IMAGE = process.env.SUROGATE_VM_IMAGE ?? fileURLToPath(new URL("../../../images/guest/out", import.meta.url));
const AGENT_DISK = fileURLToPath(new URL("../../vm/agent-disk.sh", import.meta.url));
const ROOT = "0b6c1d3e-6f0a-4c1e-9a52-6a1d2c3b4e5f";
const OTHER = "7d8e9f00-1a2b-4c3d-8e4f-5a6b7c8d9e0f";
const FIRST_UID = 10_000;
const R1: Share = { kind: "virtiofs", tag: "r1" };
// Sockets of families whose modules a stock kernel loads on demand: AppleTalk,
// X.25, CAN, RxRPC, Phonet, AF_ALG and vsock, then SCTP.
const SOCKETS = [
  "import socket",
  "for family in (5, 9, 29, 33, 35, 38, 40):",
  "    for kind in (1, 2, 5):",
  "        try: socket.socket(family, kind).close()",
  "        except OSError: pass",
  "try: socket.socket(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_SCTP).close()",
  "except OSError: pass",
].join("\n");
// Empty files in /var/tmp and in /dev/shm until the next is refused: each holds
// guest memory that a tmpfs's size= does not count.
const FILES = [
  "import errno, os",
  'for folder in ("/var/tmp/many", "/dev/shm/many"):',
  "    os.mkdir(folder)",
  "    try:",
  '        for n in range(10 ** 6): open(folder + "/" + str(n), "x").close()',
  '    except OSError as error: print(errno.errorcode[error.errno], end=" ")',
  "print()",
].join("\n");
// s_feature_incompat, in the superblock at 1024.
const INCOMPAT = 1024 + 0x60;

const signal = () => new AbortController().signal;
const folderOf = (path: string): Folder => {
  const { dev, ino } = statSync(path);
  return { path, dev, ino };
};

// The agent disk from the built agent, into *dir*.
function agentDisk(dir: string): string {
  const image = join(dir, "agent.img");
  const made = spawnSync(AGENT_DISK, [image], { encoding: "utf8" });
  if (made.status !== 0) throw new Error(`agent-disk.sh failed: ${made.error?.message ?? made.stderr}`);
  return image;
}

const host = userInfo();
const USER: HostUser = { uid: host.uid, gid: host.gid, name: host.username, home: host.homedir };

describe.skipIf(process.env.SUROGATE_VM_TESTS !== "1")("the guest", { timeout: 60_000 }, () => {
  let dir: string;
  let folder: string;
  let other: string;
  let options: VmOptions;
  let guest: Guest;

  const run = (command: string, root = ROOT, timeout = 30) => guest.op(root, "run", { command, workdir: null, timeout }, signal());
  // *path* added to the guest for *root*, then *root* set up on it.
  const setUp = async (root: string, path: string) => guest.request({ type: "setup", root, folder: path, share: await guest.share(root, folderOf(path)) });

  // The first boot formats the sessions disk; the tests run on the second, which checks it, as every later boot does.
  beforeAll(async () => {
    dir = realpathSync(mkdtempSync(join(tmpdir(), "vm-guest-")));
    // A folder name as people write them, with a space and a quote.
    folder = join(dir, "my folder's");
    other = join(dir, "other");
    mkdirSync(folder);
    mkdirSync(other);
    options = {
      kernel: join(IMAGE, "vmlinuz"), rootfs: join(IMAGE, "rootfs.img"), agentDisk: agentDisk(dir), sessions: join(dir, "sessions.img"),
      // Under $XDG_RUNTIME_DIR: a vhost-user socket's path must fit in 108 bytes.
      run: mkdtempSync(join(process.env.XDG_RUNTIME_DIR ?? "/tmp", "sg-vm-")), console: join(dir, "logs", "console.log"), user: USER,
    };
    const first = await Guest.boot(bootLinux, options);
    console.log(`M1, first boot, formatting the sessions disk: hello after ${first.helloMs.toFixed(0)} ms`);
    expect(statSync(options.sessions).size).toBe(32 * 1024 ** 3);
    await first.stop();
    guest = await Guest.boot(bootLinux, options);
  }, 60_000);

  afterAll(async () => {
    await guest?.stop();
    rmSync(options.run, { recursive: true, force: true });
    rmSync(dir, { recursive: true, force: true });
  });

  it("says hello within 15 s, adds a root's folder, sets it up, and runs its command there", async () => {
    expect(guest.helloMs).toBeLessThan(15_000);
    expect(await setUp(ROOT, folder)).toMatchObject({ type: "done" });
    expect(await run("echo hi")).toEqual({ ok: { output: "hi\n", returncode: 0, timed_out: false } });
    console.log(`M1: hello after ${guest.helloMs.toFixed(0)} ms, the first command answered after ${(performance.now() - guest.launched).toFixed(0)} ms`);
    expect(await run("pwd; echo $HOME; id -un")).toEqual({
      ok: { output: `${folder}\n${USER.home}\n${USER.name}\n`, returncode: 0, timed_out: false },
    });
    expect(await guest.request({ type: "setup", root: ROOT, folder, share: R1 })).toMatchObject({
      type: "failed", message: "This chat's sandbox is already set up",
    });
    expect(await run("true", OTHER)).toMatchObject({ error: { type: "unavailable" } });
    const valid = { type: "setup", root: OTHER, folder, share: R1 } as const;
    for (const [fields, message] of [
      [{ share: { kind: "virtiofs", tag: "../r1" } }, "not a share tag: ../r1"],
      [{ folder: "relative/path" }, "not a folder: relative/path"],
      [{ folder: "/" }, "not a folder: /"],
      [{ root: "../etc" }, "not a root session id: ../etc"],
    ] as const) {
      expect(await guest.request({ ...valid, ...fields })).toEqual(expect.objectContaining({
        type: "failed", message,
      }));
    }
  });

  it("answers ping, and carries large results", async () => {
    const times: number[] = [];
    for (let i = 0; i < 1000; i += 1) {
      const begun = performance.now();
      expect(await guest.request({ type: "ping" })).toMatchObject({ type: "pong" });
      times.push(performance.now() - begun);
    }
    times.sort((a, b) => a - b);
    // 196 000 bytes, 261 336 characters of base64: under the output cap, so each result carries all of it.
    const begun = performance.now();
    let chars = 0;
    for (let i = 0; i < 20; i += 1) {
      const outcome = await run("head -c 196000 /dev/zero | base64 -w0");
      // Whole, though the port takes at most 32 KiB a write.
      expect(outcome).toEqual({ ok: { output: Buffer.alloc(196_000).toString("base64"), returncode: 0, timed_out: false } });
      chars += JSON.stringify(outcome).length;
    }
    const seconds = (performance.now() - begun) / 1000;
    console.log(
      `M2: ping median ${times[500]?.toFixed(3)} ms, p95 ${times[950]?.toFixed(3)} ms; ` +
      `run results ${(chars / seconds / 1e6).toFixed(1)} MB/s (${(chars / 1e6).toFixed(1)} MB in ${seconds.toFixed(2)} s)`,
    );
  });

  it("gives each root a guest uid of its own, from 10000 up, and keeps it", async () => {
    expect(await guest.request({ type: "uid", root: ROOT })).toMatchObject({ type: "done", uid: FIRST_UID });
    expect(await guest.request({ type: "uid", root: OTHER })).toMatchObject({ type: "done", uid: FIRST_UID + 1 });
    expect(await guest.request({ type: "uid", root: OTHER })).toMatchObject({ type: "done", uid: FIRST_UID + 1 });
    expect(await guest.request({ type: "uid", root: "../etc" })).toMatchObject({ type: "failed" });
  });

  it("answers which as the cloud does for the tools both images share, and false for what the guest leaves out", async () => {
    const which = (name: string) => guest.op(ROOT, "which", { name }, signal());
    for (const name of ["python", "node", "pandoc", "rg", "git", "soffice", "tesseract", "ffmpeg", "pip", "uv", "gh", "npm"]) {
      expect([name, await which(name)]).toEqual([name, { ok: true }]);
    }
    for (const name of ["claude", "codex", "geesefs", "tool-executor", "sudo", "no-such-tool"]) {
      expect([name, await which(name)]).toEqual([name, { ok: false }]);
    }
  });

  it("makes a root's files its own in the guest and the host user's on the host", async () => {
    expect(await run("id -u; stat -c %u .; touch made-in-guest; stat -c %u made-in-guest")).toEqual({
      ok: { output: `${FIRST_UID}\n${FIRST_UID}\n${FIRST_UID}\n`, returncode: 0, timed_out: false },
    });
    expect(statSync(join(folder, "made-in-guest")).uid).toBe(process.getuid?.());
  });

  it("keeps a root to its own namespaces, without capabilities or user namespaces, on read-only disks", async () => {
    const outcome = await run([
      "ip -o link | cut -d' ' -f2",
      "cat /proc/1/comm",
      "grep -E '^(Cap(Prm|Eff|Bnd|Amb)|NoNewPrivs):' /proc/self/status | tr -s '\\t' ' '",
      // The image's and the folder's mounts: a setuid program or a device node on either counts for nothing.
      "for at in / .; do findmnt -no OPTIONS -T $at | tr , '\\n' | grep -cE '^no(suid|dev)$'; done",
      "cat /proc/sys/kernel/io_uring_disabled",
      "ls /run/surogate",
      "touch /usr/bin/x 2>&1 | sed 's/.*: //'",
      "touch /run/surogate/agent/x 2>&1 | sed 's/.*: //'",
      "unshare -U true 2>&1 | sed 's/.*: //'",
      "test -e /run/surogate/sessions || echo no sessions disk",
      "touch ~/in-home /tmp/in-tmp && echo home and tmp writable",
    ].join("; "));
    expect(outcome).toEqual({
      ok: {
        output: [
          "lo:", "tini", "CapPrm: 0000000000000000", "CapEff: 0000000000000000", "CapBnd: 0000000000000000", "CapAmb: 0000000000000000",
          "NoNewPrivs: 1", "2", "2", "2", "agent", "Permission denied", "Read-only file system",
          "No space left on device", "no sessions disk", "home and tmp writable", "",
        ].join("\n"),
        returncode: 0,
        timed_out: false,
      },
    });
  });

  it("gives a command the cloud's environment and the user's names, and nothing of how its root was made", async () => {
    expect(await run("env | cut -d= -f1 | sort | tr '\\n' ' '")).toEqual({
      ok: {
        output: "HOME LANG LOGNAME NPM_CONFIG_PREFIX PATH PIP_USER PWD PYTHONDONTWRITEBYTECODE PYTHONUNBUFFERED PYTHONUSERBASE SHLVL " +
          "SUROGATE_PROCESS USER UV_CACHE_DIR XDG_CACHE_HOME _ ",
        returncode: 0,
        timed_out: false,
      },
    });
  });

  it("loads no kernel module for a command, and keeps the hardening a host's sysctl files would set", async () => {
    const outcome = await run([
      "before=$(wc -l < /proc/modules)",
      `python3 -c '${SOCKETS}'`,
      'echo "modules: $before $(wc -l < /proc/modules)"',
      "cd /proc/sys && cat kernel/modules_disabled kernel/kptr_restrict kernel/dmesg_restrict fs/protected_symlinks fs/protected_regular | tr '\\n' ' '",
    ].join("; "));
    expect(outcome).toMatchObject({ ok: { output: expect.stringMatching(/^modules: (\d+) \1\n1 2 1 1 2 $/), returncode: 0 } });
  });

  it("resolves localhost, and its own hostname, without DNS", async () => {
    expect(await run([
      `node -e 'const s = require("net").createServer().listen(0, "localhost", () => { console.log("node listens"); s.close(); })'`,
      `python3 -c 'import socket; s = socket.socket(); s.bind(("localhost", 0)); print("python binds", s.getsockname()[0])'`,
      "cat /proc/sys/kernel/hostname",
      "getent hosts surogate",
    ].join("; "))).toEqual({
      ok: { output: "node listens\npython binds 127.0.0.1\nsurogate\n127.0.1.1       surogate\n", returncode: 0, timed_out: false },
    });
  });

  it("keeps two roots on two folders apart, and one that fills its memory, with bytes or with files, leaves the other room", async () => {
    expect(await setUp(OTHER, other)).toMatchObject({ type: "done" });
    const filled = await run([
      "head -c 900M /dev/zero > /var/tmp/fill 2>/dev/null; head -c 900M /dev/zero > /dev/shm/fill 2>/dev/null; du -m /var/tmp/fill /dev/shm/fill | cut -f1",
      `python3 -c '${FILES}'`,
      "df --output=itotal,iavail /var/tmp /dev/shm | tail -n +2 | tr -s ' '",
    ].join("; "));
    expect(await run([
      "python3 -c 'b = bytearray(300 * 1024 * 1024); print(len(b) >> 20)'",
      "id -u",
      "pwd",
      "find ~ /var/tmp /dev/shm -mindepth 1 | wc -l",
      "ps -e -o comm= | sort | tr '\\n' ' '; echo",
      `ls "${folder}" 2>&1 | sed 's/.*: //'`,
      "touch /var/tmp/own /dev/shm/own && echo files of its own",
    ].join("; "), OTHER)).toEqual({
      ok: {
        output: `300\n${FIRST_UID + 1}\n${other}\n0\nbash node ps sort tini tr \nNo such file or directory\nfiles of its own\n`,
        returncode: 0,
        timed_out: false,
      },
    });
    expect(filled).toEqual({ ok: { output: "256\n64\nENOSPC ENOSPC \n 32768 0\n 8192 0\n", returncode: 0, timed_out: false } });
    await run("rm -rf /var/tmp/fill /dev/shm/fill /var/tmp/many /dev/shm/many");
  });

  it("bounds a root's memory and processes in its cgroup, and the agent and the other root go on", async () => {
    // More than the roots may have together, less than the guest's 2 GiB.
    expect(await run("python3 -c 'b = b\"x\" * (1800 * 2 ** 20); print(\"kept\")'", OTHER)).toMatchObject({ ok: { output: "", returncode: 137 } });
    expect(await guest.request({ type: "ping" })).toMatchObject({ type: "pong" });
    // Children that end by themselves, until the root's cgroup refuses one more.
    const bomb = [
      "import os, time", "n = 0", "try:", "    while True:", "        if os.fork() == 0:", "            time.sleep(2)", "            os._exit(0)",
      "        n += 1", "except OSError as error:", "    print(n < 4096, error.errno)",
    ].join("\n");
    expect(await run(`python3 -c '${bomb}'`, OTHER)).toEqual({ ok: { output: "True 11\n", returncode: 0, timed_out: false } });
    expect(await run("echo still here")).toEqual({ ok: { output: "still here\n", returncode: 0, timed_out: false } });
    await new Promise((resolve) => setTimeout(resolve, 2_500));
  });

  it("holds a folder for each root up to its eight root ports, and says why it takes no ninth", async () => {
    // Two are in; six more, then one too many.
    for (let n = 3; n <= 9; n += 1) {
      const path = join(dir, `more-${n}`);
      mkdirSync(path);
      const added = guest.share(`root-${n}`, folderOf(path));
      if (n <= 8) expect(await added).toEqual({ kind: "virtiofs", tag: `r${n}` });
      else await expect(added).rejects.toThrow("it holds 8 folders already, its most until the app restarts");
    }
  });

  it("repairs a sessions disk the quick check cannot, and keeps the homes on it", async () => {
    expect(await run("echo kept > ~/kept; touch ~/victim; sync")).toEqual({ ok: { output: "", returncode: 0, timed_out: false } });
    await guest.stop();
    // A file in use marked deleted, on a disk marked not clean, after the journal is replayed so it cannot undo that.
    for (const argv of [
      ["e2fsck", "-p", "-E", "journal_only", options.sessions],
      ["debugfs", "-w", "-R", `set_inode_field /roots/${ROOT}/home/victim dtime 12345`, options.sessions],
      ["debugfs", "-w", "-R", "ssv state 0", options.sessions],
    ]) {
      expect(spawnSync(argv[0] as string, argv.slice(1), { stdio: "ignore" }).status).toBe(0);
    }
    guest = await Guest.boot(bootLinux, options);
    expect(await setUp(ROOT, folder)).toMatchObject({ type: "done" });
    expect(await run("cat ~/kept")).toEqual({ ok: { output: "kept\n", returncode: 0, timed_out: false } });
  });

  it("stops the boot, and keeps the homes, on a sessions disk e2fsck cannot check", async () => {
    await guest.stop();
    const incompat = () => {
      const fd = openSync(options.sessions, "r");
      try {
        const field = Buffer.alloc(4);
        readSync(fd, field, 0, 4, INCOMPAT);
        return field.readUInt32LE(0);
      } finally {
        closeSync(fd);
      }
    };
    expect(spawnSync("e2fsck", ["-p", "-E", "journal_only", options.sessions], { stdio: "ignore" }).status).toBe(0);
    // A feature this e2fsck does not know, as a newer mke2fs could set: e2fsck exits 8, though the disk was made.
    const known = incompat();
    expect(spawnSync("debugfs", ["-w", "-R", `ssv feature_incompat ${(known | 0x8000_0000) >>> 0}`, options.sessions], { stdio: "ignore" }).status).toBe(0);
    // The guest panics, which ends QEMU.
    await expect(Guest.boot(bootLinux, options)).rejects.toThrow("The VM exited");
    expect(readFileSync(options.console, "utf8")).toContain(
      "surogate: e2fsck could not check the sessions disk (exit code 8), so the guest stops and leaves it as it is",
    );
    // debugfs opens a disk with a feature it does not know only when forced.
    const restored = spawnSync("debugfs", ["-f", "-"], {
      input: `open -w -f ${options.sessions}\nssv feature_incompat ${known}\nclose\n`, stdio: ["pipe", "ignore", "ignore"],
    });
    expect([restored.status, incompat()]).toEqual([0, known]);
    guest = await Guest.boot(bootLinux, options);
    expect(await setUp(ROOT, folder)).toMatchObject({ type: "done" });
    expect(await run("cat ~/kept")).toEqual({ ok: { output: "kept\n", returncode: 0, timed_out: false } });
  });

  it("works in a folder under the home, and follows no link a command left on the way to it when the root is set up again", async () => {
    const home = join(dir, "home", "ana");
    const thesis = join(home, "My Projects", "Ana's thesis");
    const ANA = "1c2d3e4f-5a6b-4c7d-8e9f-0a1b2c3d4e5f";
    mkdirSync(thesis, { recursive: true });
    await guest.stop();
    guest = await Guest.boot(bootLinux, { ...options, user: { ...USER, home } });
    expect(await setUp(ANA, thesis)).toMatchObject({ type: "done" });
    expect(await run("pwd; echo $HOME", ANA)).toEqual({ ok: { output: `${thesis}\n${home}\n`, returncode: 0, timed_out: false } });
    // The way to the folder, in the root's own home, swapped for a link into the sessions disk; kept through the stop.
    expect(await run('mv ~/"My Projects" ~/moved && ln -s /run/surogate/sessions/roots ~/"My Projects" && sync && echo planted', ANA))
      .toMatchObject({ ok: { output: "planted\n" } });
    await guest.stop();
    guest = await Guest.boot(bootLinux, { ...options, user: { ...USER, home } });
    expect(await setUp(ANA, thesis)).toEqual(expect.objectContaining({
      type: "failed", message: expect.stringContaining("leads elsewhere, so this chat's sandbox is not set up"),
    }));
  });

  it("gives no uid for a root whose folder on the sessions disk this guest did not make", async () => {
    const MADE = "2d3e4f5a-6b7c-4d8e-9f0a-1b2c3d4e5f6a";
    await guest.stop();
    for (const argv of [["e2fsck", "-p", "-E", "journal_only", options.sessions], ["debugfs", "-w", "-R", `mkdir /roots/${MADE}`, options.sessions]]) {
      expect(spawnSync(argv[0] as string, argv.slice(1), { stdio: "ignore" }).status).toBe(0);
    }
    guest = await Guest.boot(bootLinux, options);
    expect(await guest.request({ type: "uid", root: MADE })).toEqual(expect.objectContaining({
      type: "failed", message: `the folder of ${MADE} on the sessions disk is not one this guest made`,
    }));
  });

  it("gives the root the host user's name, an image account's or a directory's too, and refuses one a passwd line cannot hold", async () => {
    for (const name of ["daemon", "ana@corp.example"]) {
      await guest.stop();
      guest = await Guest.boot(bootLinux, { ...options, user: { ...USER, name } });
      expect(await setUp(ROOT, folder)).toMatchObject({ type: "done" });
      expect(await run(`id -un; id -u; getent passwd '${name}' | cut -d: -f3,6`)).toEqual({
        ok: { output: `${name}\n${FIRST_UID}\n${FIRST_UID}:${USER.home}\n`, returncode: 0, timed_out: false },
      });
    }
    await guest.stop();
    guest = await Guest.boot(bootLinux, { ...options, user: { ...USER, name: "ana:x" } });
    expect(await setUp(ROOT, folder)).toEqual(expect.objectContaining({ type: "failed", message: "not a user name: ana:x" }));
  });
});

describe.skipIf(process.env.SUROGATE_VM_TESTS !== "1")("the VM manager", { timeout: 60_000 }, () => {
  let dir: string;
  let options: VmOptions;
  let managers: VmManager[];
  const op = (root: string, path: string, kind: string, args: Record<string, unknown>, cancel = signal()) =>
    managers.at(-1)!.perform({ id: `${kind}-${Math.random()}`, root, folder: folderOf(path), kind, args }, cancel);
  const qemuPid = () => Number(readFileSync(join(options.run, "qemu.pid"), "utf8"));

  beforeAll(() => {
    dir = realpathSync(mkdtempSync(join(tmpdir(), "vm-manager-")));
    for (const name of ["a", "b"]) mkdirSync(join(dir, name));
    options = {
      kernel: join(IMAGE, "vmlinuz"), rootfs: join(IMAGE, "rootfs.img"), agentDisk: agentDisk(dir), sessions: join(dir, "sessions.img"),
      run: mkdtempSync(join(process.env.XDG_RUNTIME_DIR ?? "/tmp", "sg-vm-")), console: join(dir, "console.log"), user: USER,
    };
    managers = [];
  });

  afterAll(async () => {
    for (const manager of managers) await manager.stop();
    rmSync(options.run, { recursive: true, force: true });
    rmSync(dir, { recursive: true, force: true });
  });

  it("answers a cancel while the guest boots at once, and adds no folder for it", async () => {
    managers.push(new VmManager(options));
    const cancel = new AbortController();
    const begun = performance.now();
    const answer = op(ROOT, join(dir, "a"), "run", { command: "true", workdir: null, timeout: 10 }, cancel.signal);
    setTimeout(() => cancel.abort(), 100);
    expect(await answer).toEqual(CANCELLED);
    expect(performance.now() - begun).toBeLessThan(1_000);
    // The boot goes on, for the next operation; the cancelled one's folder was never added.
    await new Promise((resolve) => setTimeout(resolve, 3_000));
    expect(existsSync(join(options.run, "control.sock"))).toBe(true);
    expect(existsSync(join(options.run, "vfs-1.sock"))).toBe(false);
  });

  it("boots at a root's first operation, adds each root's folder, and answers there", async () => {
    managers.push(new VmManager(options));
    expect(await op(ROOT, join(dir, "a"), "run", { command: "pwd", workdir: null, timeout: 10 })).toEqual({
      ok: { output: `${join(dir, "a")}\n`, returncode: 0, timed_out: false },
    });
    expect(await op(OTHER, join(dir, "b"), "which", { name: "pandoc" })).toEqual({ ok: true });
    expect(await op(OTHER, join(dir, "b"), "run", { command: "pwd; id -u", workdir: null, timeout: 10 })).toEqual({
      ok: { output: `${join(dir, "b")}\n${FIRST_UID + 1}\n`, returncode: 0, timed_out: false },
    });
  });

  it("refuses a folder replaced since its chat was bound", async () => {
    const bound = folderOf(join(dir, "b"));
    const path = join(dir, "replaced");
    mkdirSync(path);
    const stale = { ...folderOf(path), ino: bound.ino };
    expect(await managers.at(-1)!.perform({ id: "x", root: "root-replaced", folder: stale, kind: "run", args: {} }, signal()))
      .toEqual(FOLDER_UNAVAILABLE);
  });

  it("answers a run whose guest goes as stopped by the sandbox, and boots a new guest for the next", async () => {
    const running = op(ROOT, join(dir, "a"), "run", { command: "sleep 30", workdir: null, timeout: 60 });
    await new Promise((resolve) => setTimeout(resolve, 500));
    process.kill(qemuPid(), "SIGKILL");
    expect(await running).toEqual(SANDBOX_STOPPED);
    expect(await op(ROOT, join(dir, "a"), "run", { command: "echo again", workdir: null, timeout: 10 })).toEqual({
      ok: { output: "again\n", returncode: 0, timed_out: false },
    });
  });

  it("stops a guest whose folder's virtiofsd goes, and boots a new one for the next operation", async () => {
    const running = op(ROOT, join(dir, "a"), "run", { command: "sleep 30", workdir: null, timeout: 60 });
    await new Promise((resolve) => setTimeout(resolve, 500));
    process.kill(Number(readFileSync(join(options.run, "vfs-1.pid"), "utf8")), "SIGKILL");
    expect(await running).toEqual(SANDBOX_STOPPED);
    expect(await op(ROOT, join(dir, "a"), "run", { command: "echo again", workdir: null, timeout: 10 })).toMatchObject({ ok: { output: "again\n" } });
  });

  it("sets a root up again once its own command stops its runner, and answers what waited on it as stopped by the sandbox", async () => {
    const a = join(dir, "a");
    // The runner is the command's parent.
    expect(await op(ROOT, a, "run", { command: "kill -STOP $PPID", workdir: null, timeout: 1 })).toEqual({
      ok: { output: "Command timed out after 1 seconds", returncode: 124, timed_out: true },
    });
    const begun = performance.now();
    expect(await op(ROOT, a, "run", { command: "echo next", workdir: null, timeout: 30 })).toEqual(SANDBOX_STOPPED);
    // Its question unanswered for 10 s, not the run's 30.
    expect(performance.now() - begun).toBeLessThan(15_000);
    expect(await op(ROOT, a, "run", { command: "echo back", workdir: null, timeout: 10 })).toMatchObject({ ok: { output: "back\n" } });
    // Killed by its own command, it is set up again too.
    expect(await op(ROOT, a, "run", { command: "kill -KILL $PPID; sleep 5", workdir: null, timeout: 10 })).toEqual(SANDBOX_STOPPED);
    expect(await op(ROOT, a, "run", { command: "echo again", workdir: null, timeout: 10 })).toMatchObject({ ok: { output: "again\n" } });
  });

  it("ends everything of a root it tears down, a process that left its session and its environment too, and sets it up again", async () => {
    const a = join(dir, "a");
    // No marker of its command's, cleared before the command ends, and a session of its
    // own: only the root's cgroup still holds it. It beats in the folder, where the host sees it.
    const leaver = "env -i /usr/bin/setsid /usr/bin/nohup /bin/sh -c 'while :; do /usr/bin/date +%s%N > beat; /usr/bin/sleep 0.1; done'"
      + " < /dev/null > /dev/null 2>&1 & sleep 0.5; echo started";
    expect(await op(ROOT, a, "run", { command: leaver, workdir: null, timeout: 10 })).toMatchObject({ ok: { output: "started\n" } });
    const beats = async () => {
      const before = readFileSync(join(a, "beat"), "utf8");
      await new Promise((resolve) => setTimeout(resolve, 1_000));
      return readFileSync(join(a, "beat"), "utf8") !== before;
    };
    expect(await beats()).toBe(true);
    const begun = performance.now();
    await managers.at(-1)!.teardown(ROOT);
    // Well before the host would stop a guest whose agent does not answer, which would end it too.
    expect(performance.now() - begun).toBeLessThan(5_000);
    expect(await beats()).toBe(false);
    rmSync(join(a, "beat"));
    expect(await op(ROOT, a, "run", { command: "echo again", workdir: null, timeout: 10 })).toMatchObject({ ok: { output: "again\n" } });
  });

  it("leaves the agent room to start processes, and to set up another root, while four roots hold all theirs", async () => {
    // Threads until the root's cgroup refuses one more, held until the host says.
    const hold = [
      "import os, threading, time", "threading.stack_size(262144)", "n = 0", "try:", "    while True:",
      "        threading.Thread(target=time.sleep, args=(120,), daemon=True).start()", "        n += 1",
      "except RuntimeError:", "    pass", 'open("count", "w").write(str(n))',
      'while not os.path.exists("release"):', "    time.sleep(0.1)", "print(n)",
    ].join("\n");
    const busy = ["busy-1", "busy-2", "busy-3", "busy-4"];
    for (const root of [...busy, "fifth"]) mkdirSync(join(dir, root));
    const holding = busy.map((root) => op(root, join(dir, root), "run", { command: `python3 -c '${hold}'`, workdir: null, timeout: 60 }));
    for (const end = Date.now() + 30_000; !busy.every((root) => existsSync(join(dir, root, "count")));) {
      if (Date.now() > end) throw new Error(`not all held: ${JSON.stringify(await Promise.race([Promise.all(holding), Promise.resolve("holding")]))}`);
      await new Promise((resolve) => setTimeout(resolve, 100));
    }
    try {
      expect(await op("fifth", join(dir, "fifth"), "run", { command: "echo fifth", workdir: null, timeout: 10 })).toMatchObject({ ok: { output: "fifth\n" } });
    } finally {
      for (const root of busy) writeFileSync(join(dir, root, "release"), "");
    }
    // Each was refused by its own bound, a little under 4096 with its runner's own.
    for (const held of await Promise.all(holding)) expect(Number((held as { ok: { output: string } }).ok.output)).toBeGreaterThan(4_000);
    for (const root of [...busy, "fifth"]) await managers.at(-1)!.teardown(root);
  });

  it("sets a root up again only once everything of it has ended, and says why not until then", async () => {
    const a = join(dir, "a");
    // Its folder's virtiofsd: the daemon, and the child that serves the share.
    const daemon = Number(readFileSync(join(options.run, "vfs-1.pid"), "utf8"));
    const share = [daemon, ...spawnSync("pgrep", ["-P", String(daemon)], { encoding: "utf8" }).stdout.trim().split("\n").map(Number)];
    // A process that looks in the folder once its share has stalled: until the share answers, it cannot end.
    const stuck = "env -i /usr/bin/setsid /usr/bin/nohup /bin/sh -c '/usr/bin/sleep 1; /usr/bin/stat ./stuck' < /dev/null > /dev/null 2>&1 & sleep 0.5; echo started";
    expect(await op(ROOT, a, "run", { command: stuck, workdir: null, timeout: 10 })).toMatchObject({ ok: { output: "started\n" } });
    for (const pid of share) process.kill(pid, "SIGSTOP");
    try {
      await new Promise((resolve) => setTimeout(resolve, 1_500));
      await managers.at(-1)!.teardown(ROOT);
      expect(await op(ROOT, a, "run", { command: "echo back", workdir: null, timeout: 10 })).toEqual({
        error: { type: "unavailable", message: "This computer's sandbox could not set up this chat: what this chat ran before has not ended yet" },
      });
    } finally {
      for (const pid of share) process.kill(pid, "SIGCONT");
    }
    expect(await op(ROOT, a, "run", { command: "echo back", workdir: null, timeout: 10 })).toMatchObject({ ok: { output: "back\n" } });
  });

  it("stops a guest that has not added a root's folder in 15 s, and answers as stopped by the sandbox", async () => {
    const slow = join(dir, "slow");
    mkdirSync(slow);
    const pid = qemuPid();
    process.kill(pid, "SIGSTOP");
    const begun = performance.now();
    try {
      expect(await op("slow", slow, "run", { command: "true", workdir: null, timeout: 10 })).toEqual(SANDBOX_STOPPED);
    } finally {
      try {
        process.kill(pid, "SIGCONT");
      } catch {
        // Gone with its guest.
      }
    }
    // The share's 15 s, before three keepalives at 10 s each would be missed.
    expect(performance.now() - begun).toBeLessThan(20_000);
    expect(await op("slow", slow, "run", { command: "echo back", workdir: null, timeout: 10 })).toMatchObject({ ok: { output: "back\n" } });
  });

  it("stops a guest whose monitor does not add a root's folder in 15 s, and boots a new one for the next operation", async () => {
    const stalled = join(dir, "stalled");
    mkdirSync(stalled);
    expect(await op(ROOT, join(dir, "a"), "run", { command: "true", workdir: null, timeout: 10 })).toMatchObject({ ok: { returncode: 0 } });
    const pid = qemuPid();
    // Once the agent has given the root's uid and its virtiofsd is launched, QEMU answers its monitor no more.
    const watcher = watch(options.run, (_event, name) => {
      if (/^vfs-\d+\.pid$/.test(String(name))) process.kill(pid, "SIGSTOP");
    });
    const begun = performance.now();
    try {
      expect(await op("stalled", stalled, "run", { command: "true", workdir: null, timeout: 10 })).toEqual(SANDBOX_STOPPED);
    } finally {
      watcher.close();
      try {
        process.kill(pid, "SIGCONT");
      } catch {
        // Gone with its guest.
      }
    }
    expect(performance.now() - begun).toBeLessThan(20_000);
    expect(() => process.kill(pid, 0)).toThrow();
    expect(await op("stalled", stalled, "run", { command: "echo back", workdir: null, timeout: 10 })).toMatchObject({ ok: { output: "back\n" } });
    expect(qemuPid()).not.toBe(pid);
  });

  it("stops a guest that misses three keepalives, and boots a new one for the next operation", async () => {
    managers.push(new VmManager({ ...options, pingMs: 200 }));
    expect(await op(ROOT, join(dir, "a"), "run", { command: "true", workdir: null, timeout: 10 })).toMatchObject({ ok: { returncode: 0 } });
    const running = op(ROOT, join(dir, "a"), "run", { command: "sleep 20", workdir: null, timeout: 60 });
    await new Promise((resolve) => setTimeout(resolve, 300));
    const begun = performance.now();
    process.kill(qemuPid(), "SIGSTOP");
    expect(await running).toEqual(SANDBOX_STOPPED);
    // Three pings unanswered, at 200 ms each, well before the command's own end.
    expect(performance.now() - begun).toBeLessThan(5_000);
    expect(await op(ROOT, join(dir, "a"), "run", { command: "echo back", workdir: null, timeout: 10 })).toMatchObject({ ok: { output: "back\n" } });
  });

  it("gives two apps' data folders two guests, and one's boot leaves the other's running", async () => {
    // A runtime folder of the test's own, as each app has its own data.
    const runtime = mkdtempSync(join(process.env.XDG_RUNTIME_DIR ?? "/tmp", "sg-rt-"));
    const env = { SUROGATE_VM_IMAGE: IMAGE, XDG_RUNTIME_DIR: runtime };
    const [one, two] = ["one", "two"].map((app) => new VmManager({ ...vmOptions(join(dir, app), USER, env), agentDisk: options.agentDisk }));
    try {
      const echo = (manager: VmManager | undefined, root: string, path: string, line: string) =>
        manager!.perform({ id: `run-${Math.random()}`, root, folder: folderOf(path), kind: "run", args: { command: line, workdir: null, timeout: 10 } }, signal());
      expect(await echo(one, ROOT, join(dir, "a"), "echo one")).toMatchObject({ ok: { output: "one\n" } });
      // The first app's command goes on through the second's boot, and its sweep.
      const going = echo(one, ROOT, join(dir, "a"), "sleep 4; echo slept");
      expect(await echo(two, OTHER, join(dir, "b"), "echo two")).toMatchObject({ ok: { output: "two\n" } });
      expect(await going).toMatchObject({ ok: { output: "slept\n" } });
    } finally {
      await one?.stop();
      await two?.stop();
      rmSync(runtime, { recursive: true, force: true });
    }
  });

  it("takes its guest's sockets and pidfiles with it when it stops", async () => {
    await managers.at(-1)!.stop();
    expect(existsSync(options.run)).toBe(false);
  });
});

describe.skipIf(process.env.SUROGATE_VM_TESTS !== "1")("the VmExecutor, with the guest", { timeout: 60_000 }, () => {
  const CHAT = "5f6a7b8c-9d0e-4f1a-8b2c-3d4e5f6a7b8c";
  let dir: string;
  let run: string;
  let manager: VmManager;
  let executor: VmExecutor;
  const operation = (kind: string, args: Record<string, unknown>): Operation => ({
    id: `${kind}-${Math.random()}`, sessionId: CHAT, callingSessionId: CHAT, invocationId: "call", ordinal: 1, kind, args, digest: "d",
  });
  const command = (line: string) => executor.run(operation("run", { command: line, workdir: null, timeout: 10 }), signal());

  beforeAll(() => {
    dir = realpathSync(mkdtempSync(join(tmpdir(), "vm-executor-")));
    const folder = join(dir, "folder");
    mkdirSync(folder);
    run = mkdtempSync(join(process.env.XDG_RUNTIME_DIR ?? "/tmp", "sg-vm-"));
    manager = new VmManager({
      kernel: join(IMAGE, "vmlinuz"), rootfs: join(IMAGE, "rootfs.img"), agentDisk: agentDisk(dir), sessions: join(dir, "sessions.img"),
      run, console: join(dir, "console.log"), user: USER,
    });
    const { dev, ino } = statSync(folder);
    executor = new VmExecutor({
      bindingOf: (root) => (root === CHAT ? { folder, dev, ino, boot: BOOT_ID } : undefined),
      dataDir: join(dir, "data"), env: { HOME: USER.home, LANG: "C.UTF-8", PATH: "/usr/bin:/bin" }, idleMs: 500, vm: manager,
    });
  });

  afterAll(async () => {
    await executor?.stop();
    await manager?.stop();
    rmSync(run, { recursive: true, force: true });
    rmSync(dir, { recursive: true, force: true });
  });

  it("answers a chat's first which, its file host and its guest started for it", async () => {
    const begun = performance.now();
    expect(await executor.run(operation("which", { name: "pandoc" }), signal())).toEqual({ ok: true });
    const cold = performance.now() - begun;
    const again = performance.now();
    expect(await executor.run(operation("which", { name: "pandoc" }), signal())).toEqual({ ok: true });
    console.log(`which: ${cold.toFixed(0)} ms with its file host and the guest cold, ${(performance.now() - again).toFixed(0)} ms warm`);
  });

  it("ends what a command left running once the chat's file host lets its folder go", async () => {
    // No marker of its command's, cleared before the command ends, and a session of its own: only the root's cgroup still holds it.
    const leaver = "env -i /usr/bin/setsid /usr/bin/nohup /usr/bin/sleep 300 < /dev/null > /dev/null 2>&1 & sleep 0.5; echo started";
    expect(await command(leaver)).toMatchObject({ ok: { output: "started\n" } });
    expect(await command("pgrep -c -x sleep")).toMatchObject({ ok: { output: "1\n" } });
    // Idle for 500 ms, the file host tears the guest's root down, then lets the folder go.
    await new Promise((resolve) => setTimeout(resolve, 2_000));
    const begun = performance.now();
    expect(await executor.run(operation("which", { name: "pandoc" }), signal())).toEqual({ ok: true });
    console.log(`which: ${(performance.now() - begun).toFixed(0)} ms with its file host cold and the guest warm`);
    expect(await command("pgrep -c -x sleep || true")).toMatchObject({ ok: { output: "0\n" } });
  });
});
