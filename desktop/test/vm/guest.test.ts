// The guest under QEMU and KVM, booted by the VM manager: the image built by
// images/guest/build.sh, the agent disk built from this package (npm run build
// first). Behind SUROGATE_VM_TESTS=1; SUROGATE_VM_IMAGE names another image folder.

import { spawnSync } from "node:child_process";
import {
  closeSync, existsSync, lstatSync, mkdirSync, mkdtempSync, openSync, readdirSync, readFileSync, readSync, realpathSync, rmSync, statSync, symlinkSync,
  watch, writeFileSync,
} from "node:fs";
import { networkInterfaces, tmpdir, userInfo } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";

import { type ApprovalAnswer, type ApprovalPrompts, type ApprovalRequest, Approvals } from "../../src/binding/approvals.js";
import { BOOT_ID, GUEST_SYSTEM } from "../../src/binding/folder.js";
import { CANCELLED, SANDBOX_STOPPED } from "../../src/guest/command.js";
import type { ProcessHandle } from "../../src/guest/processes.js";
import type { HostUser, Share } from "../../src/guest/protocol.js";
import { FOLDER_UNAVAILABLE } from "../../src/hosts/messages.js";
import { OperationJournal } from "../../src/journal/journal.js";
import type { Operation, Outcome } from "../../src/link/protocol.js";
import { VmClient, vmOptions } from "../../src/vm/client.js";
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
// Its folder's virtiofsd for the newest share in *run*: the daemon, and the child that serves the share.
const shareDaemons = (run: string) => {
  const daemon = newestDaemon(run);
  return [daemon, ...spawnSync("pgrep", ["-P", String(daemon)], { encoding: "utf8" }).stdout.trim().split("\n").filter(Boolean).map(Number)];
};
// A background process that looks in its folder a second after it starts: once the share has stalled, it waits there.
const STUCK = "env -i /usr/bin/setsid /usr/bin/nohup /bin/sh -c '/usr/bin/sleep 1; /usr/bin/stat ./stuck' < /dev/null > /dev/null 2>&1 & echo started";
// A disk's incompatible features, as its superblock names them.
const incompat = (disk: string) => {
  const fd = openSync(disk, "r");
  try {
    const field = Buffer.alloc(4);
    readSync(fd, field, 0, 4, INCOMPAT);
    return field.readUInt32LE(0);
  } finally {
    closeSync(fd);
  }
};

const signal = () => new AbortController().signal;
const background = (command: string) => ({ command, workdir: null, task_id: "vm", pty: false, notify_on_complete: false, watcher_interval: null });

async function until(check: () => boolean, ms = 10_000): Promise<void> {
  for (const end = Date.now() + ms; !check(); await new Promise((resolve) => setTimeout(resolve, 50))) {
    if (Date.now() > end) throw new Error("timed out");
  }
}
const folderOf = (path: string): Folder => {
  const { dev, ino } = statSync(path);
  return { path, dev, ino };
};
// The pid of the newest share's virtiofsd in the runtime folder *run*: each share has a number of its own.
const newestDaemon = (run: string) => {
  const newest = Math.max(...readdirSync(run).map((name) => Number(/^vfs-(\d+)\.pid$/.exec(name)?.[1] ?? 0)));
  return Number(readFileSync(join(run, `vfs-${newest}.pid`), "utf8"));
};
// How many descriptors the newest share's virtiofsd holds, its child that serves the share included.
const descriptors = (run: string) => {
  const daemon = newestDaemon(run);
  const children = spawnSync("pgrep", ["-P", String(daemon)], { encoding: "utf8" }).stdout.trim().split("\n").filter(Boolean).map(Number);
  return [daemon, ...children].reduce((sum, pid) => sum + readdirSync(`/proc/${pid}/fd`).length, 0);
};
const alive = (pid: number) => {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
};
const median = (values: number[]) => [...values].sort((a, b) => a - b)[Math.floor(values.length / 2)] ?? 0;

// The agent disk from the built agent, into *dir*, by *script*.
function agentDisk(dir: string, script = AGENT_DISK): string {
  const image = join(dir, "agent.img");
  const made = spawnSync(script, [image], { encoding: "utf8" });
  if (made.status !== 0) throw new Error(`agent-disk.sh failed: ${made.error?.message ?? made.stderr}`);
  return image;
}

// The guest's init, as the agent disk carries it.
const INIT = readFileSync(join(dirname(AGENT_DISK), "init"), "utf8");
// INIT with each [from, to] made, each found.
const altered = (...changes: Array<[string | RegExp, string]>) => changes.reduce((init, [from, to]) => {
  const made = init.replace(from, to);
  if (made === init) throw new Error(`vm/init has no ${String(from)}`);
  return made;
}, INIT);
// An agent disk as agentDisk() makes, from a folder of its own in *dir*, with *init* for vm/init.
function agentDiskWith(dir: string, init: string): string {
  const desktop = mkdtempSync(join(dir, "desktop-"));
  mkdirSync(join(desktop, "vm"));
  for (const name of ["agent-disk.sh", "enter-root"]) symlinkSync(join(dirname(AGENT_DISK), name), join(desktop, "vm", name));
  symlinkSync(join(dirname(AGENT_DISK), "..", "dist"), join(desktop, "dist"));
  writeFileSync(join(desktop, "vm", "init"), init, { mode: 0o755 });
  return agentDisk(desktop, join(desktop, "vm", "agent-disk.sh"));
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
  const setUp = async (root: string, path: string) => guest.request({ type: "setup", root, folder: path, share: await guest.share(root, folderOf(path)), ended: [] });

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
    expect(await guest.request({ type: "setup", root: ROOT, folder, share: R1, ended: [] })).toMatchObject({
      type: "failed", message: "This chat's sandbox is already set up",
    });
    expect(await run("true", OTHER)).toMatchObject({ error: { type: "unavailable" } });
    const valid = { type: "setup", root: OTHER, folder, share: R1, ended: [] as ProcessHandle[] } as const;
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

  it("activates the bpf LSM and attaches all eleven rule hooks before sessions run", () => {
    // No command runs as the guest's root: its agent says on the console what the init's load of the rule found, before its hello.
    const said = /surogate: the protected-names rule attached (\d+) of \d+ hooks, under the LSMs (\S+)/.exec(readFileSync(options.console, "utf8"));
    expect(said?.slice(1)).toEqual(["11", expect.stringMatching(/\bbpf\b/)]);
  });

  it("runs a root's background process in its runner, where the next command reaches it, and answers for it", async () => {
    writeFileSync(join(folder, "served.txt"), "served\n");
    const started = await guest.op(ROOT, "start", background("python3 -m http.server 8765 --bind 127.0.0.1"), signal()) as { ok: { session_id: string } };
    const { session_id } = started.ok;
    let fetched = await run("curl -sS http://127.0.0.1:8765/served.txt");
    for (let tries = 0; tries < 50 && (fetched as { ok?: { output: string } }).ok?.output !== "served\n"; tries += 1) {
      await new Promise((resolve) => setTimeout(resolve, 200));
      fetched = await run("curl -sS http://127.0.0.1:8765/served.txt");
    }
    expect(fetched).toEqual({ ok: { output: "served\n", returncode: 0, timed_out: false } });
    expect(await guest.op(ROOT, "poll", { session_id }, signal())).toMatchObject({ ok: { status: "running", command: "python3 -m http.server 8765 --bind 127.0.0.1" } });
    expect(await guest.op(ROOT, "kill", { session_id }, signal())).toEqual({ ok: { status: "killed", session_id } });
    rmSync(join(folder, "served.txt"));
  });

  it("ends what a command left running when it ends, a process that left its session and its environment too", async () => {
    const leaver = "env -i /usr/bin/setsid /usr/bin/nohup /usr/bin/sleep 303 < /dev/null > /dev/null 2>&1 & sleep 0.5; echo started";
    expect(await run(leaver)).toEqual({ ok: { output: "started\n", returncode: 0, timed_out: false } });
    expect(await run("for i in 1 2 3 4 5 6 7 8 9 10; do pgrep -x sleep > /dev/null || break; sleep 0.1; done; pgrep -c -x sleep || true")).toMatchObject({
      ok: { output: "0\n" },
    });
  });

  it("stops a command at its timeout, and at a cancel, with everything it started, a process that left its session and its environment too", async () => {
    const leaving = (seconds: number) =>
      `env -i /usr/bin/setsid /usr/bin/nohup /usr/bin/sleep ${seconds} < /dev/null > /dev/null 2>&1 & (sleep ${seconds + 1} &); exec sleep ${seconds + 2}`;
    expect(await run(leaving(311), ROOT, 1)).toEqual({ ok: { output: "Command timed out after 1 seconds", returncode: 124, timed_out: true } });
    const cancel = new AbortController();
    const cancelled = guest.op(ROOT, "run", { command: leaving(321), workdir: null, timeout: 60 }, cancel.signal);
    await new Promise((resolve) => setTimeout(resolve, 500));
    cancel.abort();
    expect(await cancelled).toEqual(CANCELLED);
    expect(await run("for i in 1 2 3 4 5 6 7 8 9 10; do pgrep -x sleep > /dev/null || break; sleep 0.1; done; pgrep -c -x sleep || true")).toMatchObject({
      ok: { output: "0\n" },
    });
  });

  it("reaches nothing of this computer past its root's folder: what lies beside it, the user's ~/.ssh, or a write beside it", async () => {
    expect(await run([
      `ls -A "${dir}"`,
      "ls ~/.ssh 2>&1 | sed 's/.*: //'",
      `(echo x > "${dir}/outside.txt") 2>&1 | sed 's/.*: //'`,
    ].join("; "))).toEqual({ ok: { output: "my folder's\nNo such file or directory\nPermission denied\n", returncode: 0, timed_out: false } });
    expect(existsSync(join(dir, "outside.txt"))).toBe(false);
  });

  it("kills a background process with everything it started, and notes one the guest ended for memory", async () => {
    const begin = async (command: string) => ((await guest.op(ROOT, "start", background(command), signal())) as { ok: { session_id: string } }).ok.session_id;
    const session_id = await begin("env -i /usr/bin/setsid /usr/bin/nohup /usr/bin/sleep 304 < /dev/null > /dev/null 2>&1 & exec sleep 305");
    expect(await run("sleep 0.5; pgrep -c -x sleep")).toMatchObject({ ok: { output: "2\n" } });
    expect(await guest.op(ROOT, "kill", { session_id }, signal())).toEqual({ ok: { status: "killed", session_id } });
    expect(await run("pgrep -c -x sleep || true")).toMatchObject({ ok: { output: "0\n" } });
    // More than the roots may have together, less than the guest's 2 GiB.
    const hungry = await begin("python3 -c 'b = b\"x\" * (1800 * 2 ** 20)'");
    expect(await guest.op(ROOT, "wait", { session_id: hungry, timeout: 60 }, signal())).toEqual({
      ok: { status: "exited", exit_code: 137, output: "", note: "The computer's sandbox ran out of memory and ended this process, or one it started" },
    });
  });

  it("kills what a background process left that ignores SIGTERM once kill's grace has passed, though the process itself ended at once", async () => {
    const started = await guest.op(ROOT, "start", background("(trap '' TERM; exec setsid sleep 308) </dev/null >/dev/null 2>&1 & exec sleep 309"), signal());
    const { session_id } = (started as { ok: { session_id: string } }).ok;
    expect(await run("sleep 0.5; pgrep -c -x sleep")).toMatchObject({ ok: { output: "2\n" } });
    expect(await guest.op(ROOT, "kill", { session_id }, signal())).toEqual({ ok: { status: "killed", session_id } });
    // KILL_GRACE_MS after the SIGTERM.
    await new Promise((resolve) => setTimeout(resolve, 2_000));
    expect(await run("for i in 1 2 3 4 5 6 7 8 9 10; do pgrep -x sleep > /dev/null || break; sleep 0.1; done; pgrep -c -x sleep || true")).toMatchObject({
      ok: { output: "0\n" },
    });
  });

  it("gives each command a cgroup of its own, gone once it ends, and the root's user none of the root's own limits", async () => {
    expect(await run("true")).toMatchObject({ ok: { returncode: 0 } });
    expect(await run([
      "ls /sys/fs/cgroup/run | grep -c '^op-'",
      "cat /proc/self/cgroup",
      // A run's memory is not counted apart: the root's own cgroup counts it, for all of the root.
      'test -e "/sys/fs/cgroup$(cut -d: -f3 /proc/self/cgroup)/memory.events" || echo a run has no memory cgroup',
      "(echo 1 > /sys/fs/cgroup/pids.max) 2>&1 | sed 's/.*: //'",
      "(echo 1 > /sys/fs/cgroup/init/cgroup.kill) 2>&1 | sed 's/.*: //'",
      "(mkdir /sys/fs/cgroup/mine) 2>&1 | sed 's/.*: //'",
      "(mkdir /sys/fs/cgroup/proc/mine) 2>&1 | sed 's/.*: //'",
    ].join("; "))).toEqual({
      ok: {
        output: expect.stringMatching(/^1\n0::\/run\/op-\d+\na run has no memory cgroup\nPermission denied\nPermission denied\nPermission denied\nPermission denied\n$/),
        returncode: 0,
        timed_out: false,
      },
    });
  });

  it("makes each background process's cgroup itself, counting no memory, which the root's user can enter and end but not make, and removes it once the process ends", async () => {
    const started = await guest.op(ROOT, "start", background("sleep 310"), signal()) as { ok: { session_id: string } };
    const { session_id } = started.ok;
    const cgroup = `/sys/fs/cgroup/proc/${session_id}`;
    expect(await run([
      `stat -c %u ${cgroup} ${cgroup}/cgroup.procs ${cgroup}/cgroup.kill | tr '\\n' ' '; echo`,
      // It counts no memory: the root's own cgroup does, for all of the root.
      `test -e ${cgroup}/memory.max && echo counted || echo uncounted`,
      "(mkdir /sys/fs/cgroup/proc/mine) 2>&1 | sed 's/.*: //'",
      `(mkdir ${cgroup}/below) 2>&1 | sed 's/.*: //'`,
      `(rmdir ${cgroup}) 2>&1 | sed 's/.*: //'`,
      `sh -c 'echo $$ > ${cgroup}/cgroup.procs && cut -d: -f3 /proc/self/cgroup'`,
    ].join("; "))).toEqual({
      ok: { output: `0 ${FIRST_UID} ${FIRST_UID} \nuncounted\nPermission denied\nPermission denied\nPermission denied\n/proc/${session_id}\n`, returncode: 0, timed_out: false },
    });
    expect(await guest.op(ROOT, "kill", { session_id }, signal())).toEqual({ ok: { status: "killed", session_id } });
    expect(await run("find /sys/fs/cgroup/proc -mindepth 1 -type d | wc -l")).toMatchObject({ ok: { output: "0\n" } });
  });

  it("stops a command that makes cgroups at its root's bound, none below a command's, and the guest keeps its memory", async () => {
    const make = [
      "import errno, os",
      "def free():",
      "    return int(next(line for line in open('/proc/meminfo') if line.startswith('MemAvailable')).split()[1])",
      "before = free()",
      "n = 0",
      "try:",
      "    while n < 100000:",
      "        os.mkdir('/sys/fs/cgroup/run/c%d' % n)",
      "        n += 1",
      "except OSError as error:",
      "    print(n, errno.errorcode[error.errno])",
      "try:",
      "    os.mkdir('/sys/fs/cgroup/run/c0/below')",
      "except OSError as error:",
      "    print(errno.errorcode[error.errno])",
      "print('MiB taken', (before - free()) // 1024)",
      "for k in range(n): os.rmdir('/sys/fs/cgroup/run/c%d' % k)",
    ].join("\n");
    const outcome = await run(`python3 -c "${make}"`) as { ok: { output: string } };
    console.log(`cgroups a command made: ${outcome.ok.output.replaceAll("\n", "; ")}`);
    const [, made, taken] = /^(\d+) EAGAIN\nEAGAIN\nMiB taken (-?\d+)\n$/.exec(outcome.ok.output) ?? [];
    // 256 below the root: init, run, proc and the command's own cgroup, then the command's.
    expect(Number(made)).toBe(252);
    expect(Number(taken)).toBeLessThan(32);
  });

  it("leaves no dying cgroup behind runs that each leave a file in /tmp", async () => {
    for (let i = 0; i < 1000; i += 1) await run(`echo ${i} > /tmp/run-${i}`);
    const stat = await run("grep nr_dying_descendants /sys/fs/cgroup/cgroup.stat; rm -f /tmp/run-*") as { ok: { output: string } };
    console.log(`after 1000 runs: ${stat.ok.output.trim()}`);
    expect(Number(/nr_dying_descendants (\d+)/.exec(stat.ok.output)?.[1])).toBeLessThan(20);
  });

  it("gives back the cgroup of each command that cannot start, so a root still runs commands after more of them than its bound", async () => {
    // Past Linux's 128 KiB for one argument: the spawn throws E2BIG at once, once the command's cgroup is made.
    const long = `: ${"x".repeat(140_000)}`;
    for (let i = 0; i < 260; i += 1) expect(await run(long)).toMatchObject({ ok: { output: "spawn E2BIG", returncode: -1 } });
    expect(await run("echo still here")).toEqual({ ok: { output: "still here\n", returncode: 0, timed_out: false } });
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
          "NoNewPrivs: 1", "2", "2", "2", "agent", "net.sock", "Permission denied", "Read-only file system",
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
        output: "ALL_PROXY GIT_CONFIG_COUNT GIT_CONFIG_KEY_0 GIT_CONFIG_VALUE_0 HOME HTTPS_PROXY HTTP_PROXY LANG LOGNAME NO_PROXY NPM_CONFIG_PREFIX PATH PIP_USER PWD " +
          "PYTHONDONTWRITEBYTECODE PYTHONUNBUFFERED PYTHONUSERBASE SHLVL " +
          "USER UV_CACHE_DIR XDG_CACHE_HOME _ all_proxy http_proxy https_proxy no_proxy ",
        returncode: 0,
        timed_out: false,
      },
    });
  });

  // A repository the user made, shared with a root of its own: the rule refuses its commands' writes to
  // what the host's git and editors act on, whatever the route, and leaves git's own work and the rest alone.
  describe("the protected-names rule", () => {
    const RULE = "root-rule";
    // From a file in DEEP/10 up to .vscode is twelve components, the walk's bound; from one in DEEP/10/11, thirteen.
    const DEEP = "deep/.vscode/1/2/3/4/5/6/7/8/9";
    let shared: string;
    let repo: string;
    // What the host has at *path* in the repository: its inode, links, mode and, for a file, its bytes.
    const state = (path: string) => {
      const stats = lstatSync(join(repo, path), { throwIfNoEntry: false });
      return stats && { ino: stats.ino, nlink: stats.nlink, mode: stats.mode, bytes: stats.isFile() ? readFileSync(join(repo, path), "utf8") : null };
    };
    const inRepo = async (command: string, at = "repo") => ((await run(`cd ${at}; ${command}`, RULE)) as { ok: { output: string } }).ok.output;

    beforeAll(async () => {
      shared = join(dir, "rule");
      repo = join(shared, "repo");
      mkdirSync(repo, { recursive: true });
      expect(spawnSync("bash", ["-c", "git init -q -b master && git config user.email a@b && git config user.name a && echo a > a.txt && git add -A && git commit -qm a && git init -q sub"], { cwd: repo }).status).toBe(0);
      // A submodule's git folder, a linked worktree's, the user's own hook, which is not executable, and an MCP config of theirs.
      for (const [path, text] of [
        [".git/modules/foo/config", "[core]\n"], [".git/worktrees/wt/commondir", "../..\n"], [".git/worktrees/wt/HEAD", "ref: refs/heads/wt\n"], [".git/hooks/post-merge", "#!/bin/sh\n"],
        ["tool/.mcp.json", "{}\n"],
      ] as const) {
        mkdirSync(dirname(join(repo, path)), { recursive: true });
        writeFileSync(join(repo, path), text);
      }
      mkdirSync(join(repo, DEEP, "10", "11"), { recursive: true });
      expect(await guest.ready(RULE, folderOf(shared))).toBeNull();
    });

    afterAll(async () => {
      await guest.teardown(RULE);
      rmSync(shared, { recursive: true, force: true });
    });

    it("refuses: a hard link out of .git/config, so a write through the link leaves the host's config as it was", async () => {
      const config = state(".git/config");
      expect(await inRepo("ln .git/config cfg-link 2>&1; echo '[core] hooksPath = ../evil' >> cfg-link")).toContain("Operation not permitted");
      expect(state(".git/config")).toEqual(config);
    });

    // git config on the host renames a new file over the old one: the rule judges the path, whichever file is there.
    it("refuses: a write to a .git/config the host's git config replaced since", async () => {
      expect(spawnSync("git", ["-C", repo, "config", "core.editor", "true"]).status).toBe(0);
      const config = state(".git/config");
      expect(await inRepo("echo '[alias] x = !evil' >> .git/config 2>&1")).toContain("Operation not permitted");
      expect(state(".git/config")).toEqual(config);
    });

    it("refuses: a .git remade under a folder a command moved aside, and the moved repository keeps its config", async () => {
      const config = state("sub/.git/config");
      expect(await inRepo("mv sub sub-old && { mkdir -p sub/.git && echo '[core] fsmonitor = ../evil' > sub/.git/config; } 2>&1")).toContain("Operation not permitted");
      expect([state("sub/.git"), state("sub-old/.git/config")]).toEqual([undefined, config]);
    });

    // Each refused with the rule's EPERM, and the host's file at the case's path as it was, or still absent.
    const refuse = [
      ["hook create", "printf '#!/bin/sh\\n' > .git/hooks/pre-commit", ".git/hooks/pre-commit"],
      ["hook append via fd", "exec 3>>.git/hooks/post-merge", ".git/hooks/post-merge"],
      ["hooks subdir", "mkdir .git/hooks/x", ".git/hooks/x"],
      ["hook symlink", "ln -s /bin/true .git/hooks/post-commit", ".git/hooks/post-commit"],
      ["hook hardlink", "echo x > e; ln e .git/hooks/pre-push", ".git/hooks/pre-push"],
      ["hook chmod +x", "chmod +x .git/hooks/post-merge", ".git/hooks/post-merge"],
      ["config append", "echo '[core]' >> .git/config", ".git/config"],
      ["config truncate", ": > .git/config", ".git/config"],
      // A read-only open that truncates: the host's file is emptied before the size change is judged.
      ["config truncate read-only", "python3 -c \"import os; os.open('.git/config', os.O_RDONLY | os.O_TRUNC)\"", ".git/config"],
      ["hook truncate read-only", "python3 -c \"import os; os.open('.git/hooks/post-merge', os.O_RDONLY | os.O_TRUNC)\"", ".git/hooks/post-merge"],
      ["mcp.json truncate read-only", "python3 -c \"import os; os.open('tool/.mcp.json', os.O_RDONLY | os.O_TRUNC)\"", "tool/.mcp.json"],
      ["core.hooksPath", "printf '[core]\\n  hooksPath = ../evil\\n' >> .git/config", ".git/config"],
      ["gitconfig", "echo x > .gitconfig", ".gitconfig"],
      ["vscode dir", "mkdir .vscode", ".vscode"],
      ["mcp.json", "echo x > .mcp.json", ".mcp.json"],
      ["rename to .gitmodules", "echo x > m; mv m .gitmodules", ".gitmodules"],
      ["claude commands", "mkdir -p .claude; mkdir .claude/commands", ".claude/commands"],
      ["a FIFO at a protected name", "mkfifo .mcp.json", ".mcp.json"],
      ["a socket at a protected name", "python3 -c \"import socket; socket.socket(socket.AF_UNIX).bind('.git/hooks/pre-push')\"", ".git/hooks/pre-push"],
      ["a directory renamed into .claude", "mkdir -p cl/commands && echo x > cl/commands/evil.md && mv -T cl .claude", ".claude/commands"],
      ["a directory renamed into .git/modules/<name>", "mkdir sm && echo '[core] fsmonitor = ../evil' > sm/config && mv sm .git/modules/bar", ".git/modules/bar"],
      ["a directory renamed into .git/worktrees/<name>", "mkdir wtx && echo /evil > wtx/commondir && mv wtx .git/worktrees/wtx", ".git/worktrees/wtx"],
      ["within-bound deep key", "mkdir -p a/b/c && echo x > a/b/c/.mcp.json", "a/b/c/.mcp.json"],
      ["a key twelve components up, the walk's bound", `echo x > ${DEEP}/10/x`, `${DEEP}/10/x`],
      ["C1 gitdir-pointer file", "mkdir d; echo 'gitdir: ./evil' > d/.git", "d/.git"],
      ["C1 mkdir sub/.git", "mkdir s2; mkdir s2/.git", "s2/.git"],
      ["C2 submodule config", "echo x >> .git/modules/foo/config", ".git/modules/foo/config"],
      ["C3 worktree commondir", "echo x >> .git/worktrees/wt/commondir", ".git/worktrees/wt/commondir"],
      // What a dependency folder holds goes unjudged, and would carry its editor's and agent's folders out.
      ["a directory renamed out of node_modules", "mkdir -p node_modules/p/.vscode && echo x > node_modules/p/.vscode/tasks.json && mv node_modules/p planted", "planted"],
      ["node_modules itself renamed", "mkdir -p nm/node_modules/q/.idea && mv nm/node_modules nm/plain", "nm/plain"],
      ["a directory exchanged out of site-packages", "mkdir -p lib/site-packages/r/.vscode outside-r && python3 -c \"import ctypes; libc = ctypes.CDLL(None, use_errno=True); import os; libc.renameat2(-100, b'lib/site-packages/r', -100, b'outside-r', 2) == 0 or exit(os.strerror(ctypes.get_errno()))\"", "outside-r/.vscode"],
      ["C1 mv .git (I3)", "mv .git x", ".git"],
    ] as const;
    for (const [name, command, path] of refuse) {
      it(`refuses: ${name}`, async () => {
        const before = state(path);
        const output = await inRepo(`{ ${command}; } 2>&1; echo rc=$?`);
        expect(output).toContain("Operation not permitted");
        expect(output).toMatch(/rc=[1-9]\d*\n$/);
        expect(state(path)).toEqual(before);
      });
    }

    // Refused by the rule at the move itself, not by a mount held over .git.
    it("C1/I3: mv .git is refused at the mv itself and the host repo is intact", async () => {
      const [git, config] = [state(".git"), state(".git/config")];
      expect(await inRepo("mv .git x 2>&1; echo mv=$?; ls .git/HEAD >/dev/null 2>&1; echo head=$?")).toMatch(/Operation not permitted\nmv=1\nhead=0\n$/);
      expect([state(".git"), state(".git/config"), state("x")]).toEqual([git, config, undefined]);
    });

    const allow = [
      ["ordinary write", "echo x > notes.txt"],
      ["an ordinary file truncated by a read-only open", "echo x > plain-trunc && python3 -c \"import os; os.open('plain-trunc', os.O_RDONLY | os.O_TRUNC)\" && test ! -s plain-trunc"],
      ["I1 branch named hooks", "git branch hooks"],
      ["I1 branch named config", "git branch config"],
      ["a ref file directly", "echo 0000000000000000000000000000000000000000 > .git/refs/heads/zz"],
      // Removed again, as git does once a rebase ends.
      ["git-state rebase todo", "mkdir -p .git/rebase-merge && echo x > .git/rebase-merge/git-rebase-todo && rm -r .git/rebase-merge"],
      ["worktree op file", "echo x > .git/worktrees/wt/HEAD"],
      ["a file renamed in a submodule's git folder, as git writes one", "echo x > .git/modules/foo/HEAD.lock && mv .git/modules/foo/HEAD.lock .git/modules/foo/HEAD"],
      // Removed again: the root's later git would read it.
      ["home gitconfig (own disk)", "echo x > $HOME/.gitconfig && rm $HOME/.gitconfig"],
      ["a FIFO at an ordinary name", "mkfifo plain-fifo"],
      ["a hard link of an ordinary file", "echo x > plain && ln plain plain-link"],
      ["an ordinary directory renamed", "mkdir plain-dir && mv plain-dir plain-dir2"],
      ["an editor's folder below node_modules, as a package ships one", "mkdir -p node_modules/t/.idea && echo x > node_modules/t/.idea/x.xml"],
      ["a directory renamed within node_modules, as npm retires one", "mkdir -p node_modules/s/.vscode && mv node_modules/s node_modules/.s-retired"],
      ["a directory renamed into node_modules", "mkdir plain-in && mv plain-in node_modules/plain-in"],
      ["a file renamed out of node_modules", "echo x > node_modules/f.js && mv node_modules/f.js f.js"],
      // A ceiling the rule names: past its walk, a protected name above is not seen.
      ["a key thirteen components up, past the walk's bound", `echo x > ${DEEP}/10/11/x`],
    ] as const;
    for (const [name, command] of allow) {
      it(`allows: ${name}`, async () => {
        expect(await inRepo(`{ ${command}; } 2>&1; echo rc=$?`)).toBe("rc=0\n");
      });
    }

    // Git's own work in a repository the user has: its transient state, and the folders and files it
    // moves, are none the rule refuses. A new repository, or a linked worktree's admin files, are.
    describe("git's transient state", () => {
      let upstream: string;
      // A superproject the user set up on the host, with its submodule one commit behind what it records,
      // and a linked worktree beside it in the share.
      const WORKTREE = () => join(shared, "work-wt");
      beforeAll(() => {
        upstream = join(dir, "rule-upstream");
        const made = spawnSync("bash", ["-c", [
          "set -e",
          `git init -q -b master "${upstream}" && cd "${upstream}" && git config user.email a@b && git config user.name a`,
          "echo 1 > f && git add f && git commit -qm one && echo 2 > f && git commit -qam two",
          'cd "$0" && git init -q -b master work && cd work && git config user.email a@b && git config user.name a',
          "echo w > w.txt && git add w.txt && git commit -qm w",
          `git -c protocol.file.allow=always submodule add -q "${upstream}" lib && git commit -qm lib`,
          "git -C lib checkout -q HEAD~1",
          `git worktree add -q "${WORKTREE()}" -b wtb`,
        ].join("\n"), shared], { encoding: "utf8" });
        expect(made.status, made.stderr).toBe(0);
      });

      afterAll(() => rmSync(upstream, { recursive: true, force: true }));

      it("lets git rebase, merge-with-conflict and cherry-pick finish in an existing repo", async () => {
        const s = `rm -rf .git/rebase-merge
          git checkout -q -b feat && echo c > c.txt && git add c.txt && git commit -qm feat
          git checkout -q master && echo d > d.txt && git add d.txt && git commit -qm master
          git rebase -q master feat && echo REBASE_OK
          git checkout -q master
          git checkout -q -b f2 && echo e > a.txt && git commit -qam f2
          git checkout -q master && echo f > a.txt && git commit -qam m2
          git merge f2 >/dev/null 2>&1; test -e .git/MERGE_HEAD && echo CONFLICTED
          echo resolved > a.txt && git add a.txt && git commit -qm resolved && echo MERGE_OK
          git checkout -q -b f3 && echo g > g.txt && git add g.txt && git commit -qm g
          git checkout -q master && git cherry-pick f3 && echo CHERRY_OK`;
        const r = await inRepo(s);
        for (const m of ["REBASE_OK", "CONFLICTED", "MERGE_OK", "CHERRY_OK"]) expect(r).toContain(m);
        expect(state(".git/rebase-merge")).toBeUndefined();
      });

      it("refuses git init / clone of a new repo in the share", async () => {
        expect(await inRepo("git init -q new 2>&1; echo rc=$?; git clone -q . cloned 2>&1; echo rc=$?")).toMatch(
          /Operation not permitted[\s\S]*rc=[1-9]\d*\n[\s\S]*Operation not permitted[\s\S]*rc=[1-9]\d*\n$/,
        );
        expect([state("new/.git"), state("cloned/.git")]).toEqual([undefined, undefined]);
        // The agent makes its own repositories in its home, which the rule does not guard.
        expect(await inRepo('git init -q "$HOME/own" && echo made; rm -rf "$HOME/own"')).toBe("made\n");
      });

      it("lets git gc, in a repository and in its submodule's git folder, and git worktree move finish in an existing repo", async () => {
        expect(await inRepo("git -C lib gc -q 2>&1; echo rc=$?; git gc -q 2>&1; echo rc=$?", "work")).toBe("rc=0\nrc=0\n");
        const moved = `${WORKTREE()}-moved`;
        expect(await inRepo(`git worktree move "${WORKTREE()}" "${moved}" 2>&1; git -C "${moved}" rev-parse --abbrev-ref HEAD`, "work")).toBe("wtb\n");
        expect(readFileSync(join(shared, "work", ".git", "worktrees", "work-wt", "gitdir"), "utf8")).toBe(`${moved}/.git\n`);
        expect(spawnSync("git", ["-C", moved, "status", "--porcelain"], { encoding: "utf8" })).toMatchObject({ status: 0, stdout: "" });
      });

      // Ceilings, each at a file that sends git to a config. git submodule update rewrites the submodule's
      // core.worktree, renaming its config.lock over .git/modules/<name>/config; a new linked worktree needs
      // its .git file and its commondir, and a removed one loses them.
      it("refuses git submodule update, worktree add and worktree remove, at the files that send git to a config", async () => {
        const work = join(shared, "work");
        const config = readFileSync(join(work, ".git", "modules", "lib", "config"), "utf8");
        expect(await inRepo([
          "git submodule update --init 2>&1; echo rc=$?; git -C lib log -1 --format=%s",
          // Its lock is git's to make; the rename over the config is what the rule refuses.
          "touch .git/modules/lib/config.lock && echo locked; mv .git/modules/lib/config.lock .git/modules/lib/config 2>&1; rm .git/modules/lib/config.lock",
          `git worktree add -q "${shared}/wt-new" -b wt-new 2>&1; echo rc=$?`,
          'git worktree add -q "$HOME/wt-home" -b wt-home 2>&1; echo rc=$?; rm -rf "$HOME/wt-home"',
          `git worktree remove --force "${WORKTREE()}-moved" 2>&1; echo rc=$?`,
        ].join("; "), "work")).toBe([
          `error: could not write config file ${work}/.git/modules/lib/config: Operation not permitted`,
          "fatal: could not set 'core.worktree' to '../../../lib'", "rc=128", "one",
          "locked", "mv: cannot move '.git/modules/lib/config.lock' to '.git/modules/lib/config': Operation not permitted",
          `fatal: could not open '${shared}/wt-new/.git' for writing: Operation not permitted`, "rc=128",
          "fatal: could not open '.git/worktrees/wt-home/commondir' for writing: Operation not permitted", "rc=128",
          `error: failed to delete '${WORKTREE()}-moved': Operation not permitted`,
          "error: failed to delete '.git/worktrees/work-wt': Operation not permitted", "rc=255", "",
        ].join("\n"));
        expect(readFileSync(join(work, ".git", "modules", "lib", "config"), "utf8")).toBe(config);
        expect([`${WORKTREE()}-moved/.git`, join(work, ".git", "worktrees", "work-wt", "commondir"), `${shared}/wt-new/.git`].map((path) => existsSync(path)))
          .toEqual([true, true, false]);
      });
    });
  });

  it("holds its tools in the folders a chat's folder may not be, hold or lie in, and in no other", async () => {
    // Each folder of the image's, at its top and in its /opt and /var, that holds what the root's user can see.
    const listed = await run('for d in /* /opt/* /var/*; do [ -d "$d" ] && [ ! -L "$d" ] && [ -n "$(ls -A "$d" 2>/dev/null)" ] && echo "$d"; done') as { ok: { output: string } };
    // The root's own: its namespace's mounts, its home, and the folders of the two below.
    const own = new Set(["/dev", "/home", "/opt", "/proc", "/run", "/sys", "/tmp", "/var", "/var/tmp"]);
    expect(listed.ok.output.trim().split("\n").filter((folder) => !own.has(folder)).sort()).toEqual([...GUEST_SYSTEM].sort());
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

  it("lets two roots each run a server on one port, each reached by its own commands only", async () => {
    const ids: string[] = [];
    for (const [root, path] of [[ROOT, folder], [OTHER, other]] as const) {
      writeFileSync(join(path, "who.txt"), `${root}\n`);
      const started = await guest.op(root, "start", background("python3 -m http.server 8766 --bind 127.0.0.1"), signal()) as { ok: { session_id: string } };
      ids.push(started.ok.session_id);
    }
    for (const root of [ROOT, OTHER]) {
      let fetched = await run("curl -sS http://127.0.0.1:8766/who.txt", root);
      for (let tries = 0; tries < 50 && (fetched as { ok?: { output: string } }).ok?.output !== `${root}\n`; tries += 1) {
        await new Promise((resolve) => setTimeout(resolve, 200));
        fetched = await run("curl -sS http://127.0.0.1:8766/who.txt", root);
      }
      expect(fetched).toEqual({ ok: { output: `${root}\n`, returncode: 0, timed_out: false } });
    }
    for (const [index, root] of [ROOT, OTHER].entries()) await guest.op(root, "kill", { session_id: ids[index] }, signal());
    for (const path of [folder, other]) rmSync(join(path, "who.txt"));
  });

  it("gives each root its own socket to the host proxy, which judges a command's connection there for that root", async () => {
    // A connection as the root's runner opens one: its destination line, then the agent's answer.
    const through = (destination: string) =>
      `python3 -c 'import socket; s = socket.socket(socket.AF_UNIX); s.connect("/run/surogate/net.sock"); s.sendall(b"${destination}\\n"); print(s.recv(64).decode(), end="")'`;
    expect(await run([
      "stat -c '%u %a' /run/surogate/net.sock",
      through("127.0.0.1:9"),
      "(rm -f /run/surogate/net.sock) 2>&1 | sed 's/.*: //'",
    ].join("; "))).toEqual({
      ok: {
        output: `${FIRST_UID} 600\n403 own\nPermission denied\n\nThis computer does not let a chat reach its own network services (127.0.0.1:9)`,
        returncode: 0,
        timed_out: false,
      },
    });
    // The other root's connection is its own: its notice comes with its next run, not this root's.
    expect(await run(through("127.0.0.1:7"), OTHER)).toMatchObject({
      ok: { output: "403 own\n\nThis computer does not let a chat reach its own network services (127.0.0.1:7)" },
    });
    expect(await run("true")).toEqual({ ok: { output: "", returncode: 0, timed_out: false } });
  });

  it("has its runner's proxies take a command's connections, past its own loopback, to the host proxy, which refuses this computer's own", async () => {
    // The HTTP status, then CONNECT's, as curl got them: 000 for none.
    const status = (flags: string, url: string) => `curl -sS --max-time 10 ${flags} -o /dev/null -w '%{http_code} %{http_connect}\\n' ${url} 2>/dev/null`;
    expect(await run([
      status("--noproxy ''", "http://127.0.0.1:9/"),
      status("--noproxy '' -p", "http://127.0.0.1:9/"),
      status("--noproxy '' --socks5-hostname 127.0.0.1:1080", "http://localhost:9/"),
      // Its own loopback is direct, so a session's own servers answer as they are: here, none.
      // So are the address a server says it listens on, and the root's own name.
      status("", "http://0.0.0.0:9/"),
      status("", "http://surogate:9/"),
      status("", "http://127.0.0.1:9/"),
    ].join("; "))).toEqual({
      ok: {
        output: "403 000\n000 403\n000 000\n000 000\n000 000\n000 000\n\nThis computer does not let a chat reach its own network services (127.0.0.1:9, localhost:9)",
        // The last curl's: it could not connect.
        returncode: 7,
        timed_out: false,
      },
    });
  });

  it("keeps its guest, and another root's process, through a start whose command is longer than a control line", async () => {
    const other = await guest.op(OTHER, "start", background("sleep 306"), signal()) as { ok: { session_id: string } };
    // 1.5M characters, each six bytes once escaped in JSON: past the host's 8 MiB line, inside the link's 2M-character frame.
    expect(await guest.op(ROOT, "start", background(`: ${"\u0001".repeat(1_500_000)}`), signal())).not.toEqual(SANDBOX_STOPPED);
    await new Promise((resolve) => setTimeout(resolve, 500));
    expect(guest.ended).toBe(false);
    expect(await guest.op(OTHER, "poll", { session_id: other.ok.session_id }, signal())).toMatchObject({ ok: { status: "running" } });
    await guest.op(OTHER, "kill", { session_id: other.ok.session_id }, signal());
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

  it("holds a folder for each root up to its eight root ports, says why it takes no ninth, and takes it once one has left", async () => {
    // Two are in; six more, then one too many.
    const more = Array.from({ length: 7 }, (_, n) => {
      const path = join(dir, `more-${n + 3}`);
      mkdirSync(path);
      return [`root-${n + 3}`, folderOf(path)] as const;
    });
    for (const [root, shared] of more.slice(0, 6)) expect(await guest.ready(root, shared)).toBeNull();
    const [ninth, its] = more[6]!;
    expect(await guest.ready(ninth, its)).toEqual({
      error: { type: "unavailable", message: "This computer's sandbox could not add this chat's folder: it holds 8 folders already, each of a chat at work" },
    });
    await guest.teardown("root-3");
    expect(await guest.ready(ninth, its)).toBeNull();
    expect(await guest.op(ninth, "run", { command: "pwd", workdir: null, timeout: 10 }, signal())).toMatchObject({ ok: { output: `${its.path}\n` } });
    for (const [root] of more.slice(1)) await guest.teardown(root);
  });

  it("adds and removes a root's folder again and again, two roots at once, each time on a root port another has let go", async () => {
    const roots = [["root-a", join(dir, "cycle-a")], ["root-b", join(dir, "cycle-b")]] as const;
    for (const [, path] of roots) mkdirSync(path, { recursive: true });
    const added: number[] = [];
    const removed: number[] = [];
    for (let cycle = 0; cycle < 20; cycle += 1) {
      await Promise.all(roots.map(async ([root, path]) => {
        writeFileSync(join(path, "cycle"), `${cycle}\n`);
        let begun = performance.now();
        expect(await guest.ready(root, folderOf(path))).toBeNull();
        added.push(performance.now() - begun);
        expect(await guest.op(root, "run", { command: "cat cycle", workdir: null, timeout: 10 }, signal())).toMatchObject({ ok: { output: `${cycle}\n` } });
        begun = performance.now();
        await guest.teardown(root);
        removed.push(performance.now() - begun);
      }));
    }
    expect(guest.ended).toBe(false);
    console.log(
      `M3: a folder added and its root set up in ${median(added).toFixed(0)} ms (median, max ${Math.max(...added).toFixed(0)}), ` +
      `torn down and removed in ${median(removed).toFixed(0)} ms (median, max ${Math.max(...removed).toFixed(0)}), 40 cycles, none failed`,
    );
  });

  it("takes a folder of 150 000 files, as a node_modules is, with descriptors to spare", { timeout: 900_000 }, async () => {
    const big = join(dir, "big");
    // 1 500 packages of 100 files each, tracked by git, as the host made them.
    for (let p = 0; p < 1_500; p += 1) {
      mkdirSync(join(big, "node_modules", `p${p}`), { recursive: true });
      writeFileSync(join(big, "node_modules", `p${p}`, "package.json"), "{}\n");
      for (let f = 0; f < 99; f += 1) writeFileSync(join(big, "node_modules", `p${p}`, `f${f}.js`), "");
    }
    expect(spawnSync("bash", ["-c", "git init -q && git add -A && git -c user.email=a@b -c user.name=a commit -qm init"], { cwd: big }).status).toBe(0);
    const timed = async (command: string) => {
      const begun = performance.now();
      const outcome = await guest.op("root-big", "run", { command, workdir: null, timeout: 600 }, signal()) as { ok: { output: string; returncode: number } };
      return { ...outcome.ok, ms: performance.now() - begun };
    };
    try {
      expect(await guest.ready("root-big", folderOf(big))).toBeNull();
      // The first rehashes none, though the host's index holds the host's owner: git in the guest does not compare it.
      const first = await timed("git status --porcelain 2>&1 | wc -l");
      const again = await timed("git status --porcelain 2>&1 | wc -l");
      const walk = await timed("find node_modules -type f 2>&1 | wc -l");
      // What an install writes: a file at a time, 150 000 of them.
      const write = await timed("python3 -c 'import os\nfor p in range(1500):\n    os.makedirs(f\"made/p{p}\")\n    for f in range(100): open(f\"made/p{p}/f{f}.js\", \"w\").close()' 2>&1; find made -type f | wc -l");
      const held = descriptors(options.run);
      console.log(
        `M9: git status ${(first.ms / 1000).toFixed(1)} s first, ${(again.ms / 1000).toFixed(1)} s again; find ${(walk.ms / 1000).toFixed(1)} s; ` +
        `150 000 files written ${(write.ms / 1000).toFixed(1)} s; virtiofsd holds ${held} descriptors`,
      );
      expect([first.output, again.output, walk.output, write.output]).toEqual(["0\n", "0\n", "150000\n", "150000\n"]);
      expect(held).toBeLessThan(1_000);
    } finally {
      await guest.teardown("root-big");
      rmSync(big, { recursive: true, force: true });
    }
  });

  it("lets git in the guest take an index the host's git refreshed last, so its status after one on the host rehashes nothing", { timeout: 300_000 }, async () => {
    const repo = join(dir, "repo");
    // 20 000 files, tracked by git, as the host made them.
    for (let p = 0; p < 200; p += 1) {
      mkdirSync(join(repo, `p${p}`), { recursive: true });
      for (let f = 0; f < 100; f += 1) writeFileSync(join(repo, `p${p}`, `f${f}.js`), `${p}.${f}\n`);
    }
    expect(spawnSync("bash", ["-c", "git init -q && git add -A && git -c user.email=a@b -c user.name=a commit -qm init"], { cwd: repo }).status).toBe(0);
    const timed = async (command: string) => {
      const begun = performance.now();
      const outcome = await guest.op("root-git", "run", { command, workdir: null, timeout: 120 }, signal()) as { ok: { output: string } };
      return { output: outcome.ok.output, ms: performance.now() - begun };
    };
    try {
      expect(await guest.ready("root-git", folderOf(repo))).toBeNull();
      // Git on the host refreshes the index last, with the host's owner in it.
      expect(spawnSync("git", ["-C", repo, "status", "--porcelain"]).status).toBe(0);
      const first = await timed("git status --porcelain 2>&1 | wc -l");
      const again = await timed("git status --porcelain 2>&1 | wc -l");
      console.log(`git status of 20 000 files in the guest after one on the host: ${first.ms.toFixed(0)} ms, then ${again.ms.toFixed(0)} ms`);
      expect([first.output, again.output]).toEqual(["0\n", "0\n"]);
      expect(first.ms).toBeLessThan(again.ms * 2 + 250);
    } finally {
      await guest.teardown("root-git");
      // Git may still be writing into .git (auto-maintenance), so the removal retries.
      rmSync(repo, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
    }
  });

  it("refuses a shared mapping of a folder's file, writable or read-only, so SQLite's WAL fails there plainly and works in the home folder", async () => {
    const mapped = await run([
      "python3 - <<'EOF'",
      "import mmap, os, sqlite3",
      "with open('mapped', 'w+b') as f:",
      "    f.write(b'x' * 4096)",
      "    f.flush()",
      "    for access in (mmap.ACCESS_WRITE, mmap.ACCESS_READ):",
      "        try:",
      "            mmap.mmap(f.fileno(), 4096, access=access)",
      "            print('shared')",
      "        except OSError as error:",
      "            print('shared', error.errno)",
      "    mmap.mmap(f.fileno(), 4096, access=mmap.ACCESS_COPY)",
      "    print('private')",
      "for path in ('wal.db', os.path.expanduser('~/wal.db')):",
      "    try:",
      "        db = sqlite3.connect(path)",
      "        db.execute('pragma journal_mode=wal')",
      "        db.execute('create table t (x)')",
      "        print('wal')",
      "    except sqlite3.Error as error:",
      "        print('wal', error)",
      "EOF",
      "rm -f mapped wal.db wal.db-wal wal.db-shm ~/wal.db ~/wal.db-wal ~/wal.db-shm",
    ].join("\n"));
    // The folder is served uncached without --allow-mmap: a shared mapping fails with ENODEV, as the
    // model's note says, rather than lose writes either side makes while it is mapped. The home is the guest's own disk.
    expect(mapped).toEqual({ ok: { output: "shared 19\nshared 19\nprivate\nwal disk I/O error\nwal\n", returncode: 0, timed_out: false } });
  });

  it("runs the folder's own programs: a checked-in script, a built binary and a node_modules/.bin tool", async () => {
    expect(await run([
      "printf '#!/bin/sh\\necho configured\\n' > configure && chmod +x configure && ./configure",
      "cp /usr/bin/true built && ./built && echo built",
      // As npm installs a package's command: a script in the package, linked from node_modules/.bin.
      "mkdir -p node_modules/tool/bin node_modules/.bin && printf '#!/usr/bin/env node\\nconsole.log(\"tool\")\\n' > node_modules/tool/bin/cli.js",
      "chmod +x node_modules/tool/bin/cli.js && ln -s ../tool/bin/cli.js node_modules/.bin/tool && ./node_modules/.bin/tool",
      "rm -rf configure built node_modules",
    ].join(" && "))).toEqual({ ok: { output: "configured\nbuilt\ntool\n", returncode: 0, timed_out: false } });
  });

  it("sets its clock to the time the host tells it at a wake, which its commands then see", async () => {
    const ahead = Date.now() + 3_600_000;
    expect(await guest.request({ type: "time", now: ahead, slept: 0 })).toMatchObject({ type: "done" });
    const said = await run("date +%s") as { ok: { output: string } };
    expect(Math.abs(Number(said.ok.output) - ahead / 1000)).toBeLessThan(5);
    expect(await guest.request({ type: "time", now: Date.now(), slept: 0 })).toMatchObject({ type: "done" });
  });

  it("is timed out by this computer, which ends the command in the guest", async () => {
    const begun = performance.now();
    expect(await run("sleep 311", ROOT, 1)).toEqual({ ok: { output: "Command timed out after 1 seconds", returncode: 124, timed_out: true } });
    expect(performance.now() - begun).toBeLessThan(3_000);
    expect(await run("sleep 0.5; pgrep -c -f '^sleep 311$' || true")).toMatchObject({ ok: { output: "0\n" } });
  });

  it("notes no shortage of memory for a background process a command ended with SIGKILL", async () => {
    const started = await guest.op(ROOT, "start", background("exec sleep 311"), signal()) as { ok: { session_id: string; pid: number } };
    expect(await run(`sleep 0.5; kill -KILL ${started.ok.pid}`)).toMatchObject({ ok: { returncode: 0 } });
    expect(await guest.op(ROOT, "wait", { session_id: started.ok.session_id, timeout: 10 }, signal())).toEqual({
      ok: { status: "exited", exit_code: 137, output: "" },
    });
  });

  it("leaves no memory cgroup behind background processes that each leave a file in /tmp, nor behind a root set up again and again", async () => {
    const MANY = "3e4f5a6b-7c8d-4e9f-8a0b-1c2d3e4f5a6b";
    const path = join(dir, "many");
    mkdirSync(path);
    // The guest's memory cgroups, the dying among them: /proc/cgroups is not namespaced, as the
    // root's own cgroup.stat is, and a root set up again in a new cgroup would hide those of the old.
    const memcgs = async () => Number((await run("awk '$1 == \"memory\" { print $3 }' /proc/cgroups", MANY) as { ok: { output: string } }).ok.output);
    expect(await guest.ready(MANY, folderOf(path))).toBeNull();
    const before = await memcgs();
    for (let n = 0; n < 200; n += 1) {
      const started = await guest.op(MANY, "start", background(`echo ${n} > /tmp/bg-${n}`), signal()) as { ok: { session_id: string } };
      expect(await guest.op(MANY, "wait", { session_id: started.ok.session_id, timeout: 10 }, signal())).toMatchObject({ ok: { status: "exited" } });
    }
    const processed = await memcgs();
    for (let n = 0; n < 30; n += 1) {
      await guest.teardown(MANY);
      expect(await guest.ready(MANY, folderOf(path))).toBeNull();
      expect(await run(`echo ${n} > /tmp/again-${n}`, MANY)).toMatchObject({ ok: { returncode: 0 } });
    }
    const after = await memcgs();
    console.log(`memory cgroups in the guest: ${before} before, ${processed} after 200 background processes, ${after} after 30 setups more`);
    expect([processed - before, after - before].map((grown) => grown < 5)).toEqual([true, true]);
    await guest.teardown(MANY);
  });

  it("writes a home's file out at its stop, with no sync of its own, and leaves the sessions disk nothing to recover", async () => {
    expect(await run("echo written > ~/written; head -c 50000000 /dev/urandom > ~/big")).toMatchObject({ ok: { returncode: 0 } });
    const begun = performance.now();
    await guest.stop();
    const stopped = performance.now() - begun;
    console.log(`stop: the guest powered off ${stopped.toFixed(0)} ms after its shutdown was asked`);
    // debugfs reads the disk as it lies, replaying no journal: what it shows was written out.
    const debugfs = (request: string) => spawnSync("debugfs", ["-R", request, options.sessions], { encoding: "utf8" }).stdout;
    expect(debugfs(`cat /roots/${ROOT}/home/written`)).toBe("written\n");
    expect(/Size: (\d+)/.exec(debugfs(`stat /roots/${ROOT}/home/big`))?.[1]).toBe("50000000");
    expect(spawnSync("dumpe2fs", ["-h", options.sessions], { encoding: "utf8" }).stdout).not.toMatch(/^Filesystem features:.*needs_recovery/m);
    // Its agent's power-off, well inside the 5 s past which the VM is ended. 7.0 prints no
    // "reboot: Power down" after this line, as 6.8 did.
    expect(stopped).toBeLessThan(2_000);
    expect(readFileSync(options.console, "utf8")).toContain("sysrq: Power Off");
    guest = await Guest.boot(bootLinux, options);
    expect(await setUp(ROOT, folder)).toMatchObject({ type: "done" });
    expect(await run("rm ~/big; cat ~/written")).toEqual({ ok: { output: "written\n", returncode: 0, timed_out: false } });
  });

  it("powers off around a process waiting on a share that stalled, and keeps what its home was written", async () => {
    const STALLED = "4f5a6b7c-8d9e-4f0a-9b1c-2d3e4f5a6b7c";
    const path = join(dir, "stalled");
    mkdirSync(path);
    expect(await guest.ready(STALLED, folderOf(path))).toBeNull();
    const daemons = shareDaemons(options.run);
    expect(await guest.op(STALLED, "start", background(STUCK), signal())).toMatchObject({ ok: { session_id: expect.any(String) } });
    expect(await run("echo kept > ~/kept-stalled", STALLED)).toMatchObject({ ok: { returncode: 0 } });
    await new Promise((resolve) => setTimeout(resolve, 500));
    for (const pid of daemons) process.kill(pid, "SIGSTOP");
    try {
      await new Promise((resolve) => setTimeout(resolve, 1_500));
      const begun = performance.now();
      await guest.stop();
      console.log(`stop around a stalled share: ${(performance.now() - begun).toFixed(0)} ms`);
      // The guest powers off around it; QEMU, whose device stop waits on the stopped daemon, is ended at the bound.
      expect(performance.now() - begun).toBeLessThan(6_000);
      expect(readFileSync(options.console, "utf8")).toContain("sysrq: Power Off");
    } finally {
      for (const pid of daemons) {
        try {
          process.kill(pid, "SIGKILL");
        } catch {
          // Gone with its guest.
        }
      }
    }
    guest = await Guest.boot(bootLinux, options);
    expect(await setUp(ROOT, folder)).toMatchObject({ type: "done" });
    expect(await guest.ready(STALLED, folderOf(path))).toBeNull();
    expect(await run("cat ~/kept-stalled", STALLED)).toEqual({ ok: { output: "kept\n", returncode: 0, timed_out: false } });
    await guest.teardown(STALLED);
  });

  it("fails the boot before its hello when the guest-kernel rule cannot load, and says why", async () => {
    // QEMU, with the bpf LSM left out of the guest kernel's command line: the rule has nothing to attach to.
    const bin = join(dir, "no-bpf");
    mkdirSync(bin);
    writeFileSync(join(bin, "qemu-system-x86_64"), `#!/bin/sh\nfor arg; do shift; set -- "$@" "$(printf '%s' "$arg" | sed 's/,bpf$//')"; done\nexec /usr/bin/qemu-system-x86_64 "$@"\n`, { mode: 0o755 });
    await guest.stop();
    const path = process.env.PATH;
    process.env.PATH = `${bin}:${path}`;
    try {
      const begun = performance.now();
      await expect(Guest.boot(bootLinux, options)).rejects.toThrow("The VM exited");
      expect(performance.now() - begun).toBeLessThan(10_000);
    } finally {
      process.env.PATH = path;
    }
    expect(readFileSync(options.console, "utf8")).toContain("surogate: the bpf LSM is not active; refusing to run commands unprotected");
    guest = await Guest.boot(bootLinux, options);
    expect(await setUp(ROOT, folder)).toMatchObject({ type: "done" });
  });

  it("fails the boot before its hello when the rule's outcome comes after the agent's start: fewer hooks, a failed load, or none", { timeout: 120_000 }, async () => {
    const want = Number(/^want=(\d+)/m.exec(INIT)?.[1]);
    // The outcome *s* seconds late, past where the agent's ports would open: a hello before it would come first.
    const late = (s: number): [string, string] => ["(\n  if ", `(\n  sleep ${s}\n  if `];
    const cases: Array<[string, string]> = [
      [altered(late(3), [/^want=\d+/m, `want=${want + 1}`]), `surogate: the protected-names rule attached ${want} of ${want + 1} hooks`],
      [altered(late(3), ["/usr/lib/surogate/rule.bpf.o", "/usr/lib/surogate/missing.bpf.o"]), "surogate: could not load the guest-kernel protected-names rule"],
      [altered(late(600)), "surogate: the protected-names rule did not finish loading"],
    ];
    await guest.stop();
    for (const [init, said] of cases) {
      const outcome = await Guest.boot(bootLinux, { ...options, agentDisk: agentDiskWith(dir, init) }).then(async (booted) => {
        await booted.stop();
        return "it said hello";
      }, (error: Error) => error.message);
      expect(outcome, said).toContain("The VM exited");
      expect(readFileSync(options.console, "utf8")).toContain(said);
    }
    guest = await Guest.boot(bootLinux, options);
    expect(await setUp(ROOT, folder)).toMatchObject({ type: "done" });
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
    expect(spawnSync("e2fsck", ["-p", "-E", "journal_only", options.sessions], { stdio: "ignore" }).status).toBe(0);
    // A feature this e2fsck does not know, as a newer mke2fs could set: e2fsck exits 8, though the disk was made.
    const known = incompat(options.sessions);
    expect(spawnSync("debugfs", ["-w", "-R", `ssv feature_incompat ${(known | 0x8000_0000) >>> 0}`, options.sessions], { stdio: "ignore" }).status).toBe(0);
    // The guest panics, which ends QEMU.
    await expect(Guest.boot(bootLinux, options)).rejects.toThrow("The VM exited");
    expect(readFileSync(options.console, "utf8")).toContain(
      "surogate: e2fsck could not check the sessions disk (exit code 8), so the guest stops; the app keeps the disk aside and makes a new one",
    );
    // debugfs opens a disk with a feature it does not know only when forced.
    const restored = spawnSync("debugfs", ["-f", "-"], {
      input: `open -w -f ${options.sessions}\nssv feature_incompat ${known}\nclose\n`, stdio: ["pipe", "ignore", "ignore"],
    });
    expect([restored.status, incompat(options.sessions)]).toEqual([0, known]);
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

describe.skipIf(process.env.SUROGATE_VM_TESTS !== "1")("the guest's memory", { timeout: 60_000 }, () => {
  let dir: string;
  let options: VmOptions;
  let guest: Guest;
  const pss = (pid: number) => Number(/^Pss:\s+(\d+)/m.exec(readFileSync(`/proc/${pid}/smaps_rollup`, "utf8"))?.[1] ?? 0) / 1024;

  beforeAll(async () => {
    dir = realpathSync(mkdtempSync(join(tmpdir(), "vm-memory-")));
    mkdirSync(join(dir, "folder"));
    options = {
      kernel: join(IMAGE, "vmlinuz"), rootfs: join(IMAGE, "rootfs.img"), agentDisk: agentDisk(dir), sessions: join(dir, "sessions.img"),
      run: mkdtempSync(join(process.env.XDG_RUNTIME_DIR ?? "/tmp", "sg-vm-")), console: join(dir, "console.log"), user: USER,
    };
    guest = await Guest.boot(bootLinux, options);
    expect(await guest.ready(ROOT, folderOf(join(dir, "folder")))).toBeNull();
  });

  afterAll(async () => {
    await guest?.stop();
    rmSync(options.run, { recursive: true, force: true });
    rmSync(dir, { recursive: true, force: true });
  });

  it("gives this computer back the memory a command freed within 15 s, and logs what the guest costs it (M5)", async () => {
    const qemu = Number(readFileSync(join(options.run, "qemu.pid"), "utf8"));
    const before = pss(qemu);
    // 1.2 GiB touched, then freed as the process exits.
    const touched = "python3 -c 'b = bytearray(1200 * 2 ** 20); b[::4096] = b\"x\" * len(b[::4096])'";
    expect(await guest.op(ROOT, "run", { command: touched, workdir: null, timeout: 60 }, signal())).toMatchObject({ ok: { returncode: 0 } });
    const used = pss(qemu);
    await until(() => pss(qemu) < before + 300, 15_000);
    console.log(`M5: QEMU Pss ${before.toFixed(0)} MiB before, ${used.toFixed(0)} MiB after a 1.2 GiB command, ${pss(qemu).toFixed(0)} MiB once its pages were reported`);
    expect(used).toBeGreaterThan(before + 900);
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

  it("answers a cancel while the guest boots at once, adds no folder for it, and powers off the guest it booted for nothing", async () => {
    managers.push(new VmManager(options));
    const cancel = new AbortController();
    const begun = performance.now();
    const answer = op(ROOT, join(dir, "a"), "run", { command: "true", workdir: null, timeout: 10 }, cancel.signal);
    setTimeout(() => cancel.abort(), 100);
    expect(await answer).toEqual(CANCELLED);
    expect(performance.now() - begun).toBeLessThan(1_000);
    // The boot goes on; the guest it brings holds no root, so it stops, and the cancelled operation's folder was never added.
    await until(() => existsSync(join(options.run, "qemu.pid")));
    const qemu = qemuPid();
    await until(() => !alive(qemu));
    expect(readFileSync(options.console, "utf8")).toContain("sysrq: Power Off");
    expect(existsSync(join(options.run, "vfs-1.pid"))).toBe(false);
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
    process.kill(newestDaemon(options.run), "SIGKILL");
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
    // What a background process left, in a session of its own and with its environment
    // cleared, once that process has ended. It beats in the folder, where the host sees it.
    const leaver = "env -i /usr/bin/setsid /usr/bin/nohup /bin/sh -c 'while :; do /usr/bin/date +%s%N > beat; /usr/bin/sleep 0.1; done'"
      + " < /dev/null > /dev/null 2>&1 & echo started";
    expect(await op(ROOT, a, "start", background(leaver))).toMatchObject({ ok: { session_id: expect.any(String) } });
    await until(() => existsSync(join(a, "beat")));
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

  it("keeps a torn-down root's share while a process of it waits on the share that stalled, and the guest and another root's process go on", async () => {
    // A removal's bound of its own: the share's. The manager before it stops first, so its guest has let the disks go.
    await managers.at(-1)?.stop();
    managers.push(new VmManager({ ...options, shareMs: 3_000 }));
    const [a, b] = [join(dir, "a"), join(dir, "b")];
    const other = await op(OTHER, b, "start", background("sleep 300")) as { ok: { session_id: string } };
    expect(await op(ROOT, a, "run", { command: "true", workdir: null, timeout: 10 })).toMatchObject({ ok: { returncode: 0 } });
    const daemons = shareDaemons(options.run);
    const qemu = qemuPid();
    // A process that looks in the folder once its share has stalled: it cannot end while it waits.
    expect(await op(ROOT, a, "start", background(STUCK))).toMatchObject({ ok: { session_id: expect.any(String) } });
    await new Promise((resolve) => setTimeout(resolve, 500));
    for (const pid of daemons) process.kill(pid, "SIGSTOP");
    try {
      await new Promise((resolve) => setTimeout(resolve, 1_500));
      const begun = performance.now();
      await managers.at(-1)!.teardown(ROOT);
      // The agent's 3 s for the root's processes to end, and no removal to wait on.
      expect(performance.now() - begun).toBeLessThan(6_000);
      expect(alive(qemu)).toBe(true);
      expect(await op(OTHER, b, "poll", { session_id: other.ok.session_id })).toMatchObject({ ok: { status: "running" } });
    } finally {
      for (const pid of daemons) process.kill(pid, "SIGCONT");
    }
    // Its share answers again, so what waited on it ends: the root is set up again, on a share of its own, in the same guest.
    await new Promise((resolve) => setTimeout(resolve, 1_000));
    expect(await op(ROOT, a, "run", { command: "echo back", workdir: null, timeout: 10 })).toMatchObject({ ok: { output: "back\n" } });
    expect(qemuPid()).toBe(qemu);
    // The tests after this one bound a share by the default 15 s.
    await managers.at(-1)!.stop();
    managers.push(new VmManager(options));
    expect(await op(ROOT, a, "run", { command: "true", workdir: null, timeout: 10 })).toMatchObject({ ok: { returncode: 0 } });
  });

  it("tears another chat down while one chat's share stalls: what it wrote written out, its share let go, and kept through a power cut", async () => {
    await managers.at(-1)?.stop();
    managers.push(new VmManager({ ...options, shareMs: 3_000 }));
    const [a, b] = [join(dir, "a"), join(dir, "b")];
    // The chat whose share stalls, with a process that waits on it.
    expect(await op(ROOT, a, "start", background(STUCK))).toMatchObject({ ok: { session_id: expect.any(String) } });
    const stalled = shareDaemons(options.run);
    expect(await op(OTHER, b, "run", { command: "echo durable > ~/durable", workdir: null, timeout: 10 })).toMatchObject({ ok: { returncode: 0 } });
    const its = shareDaemons(options.run);
    await new Promise((resolve) => setTimeout(resolve, 500));
    for (const pid of stalled) process.kill(pid, "SIGSTOP");
    try {
      await new Promise((resolve) => setTimeout(resolve, 1_500));
      const begun = performance.now();
      // Its flush is of its own filesystems: sync(2) would wait for good on the stalled share.
      await managers.at(-1)!.teardown(OTHER);
      expect(performance.now() - begun).toBeLessThan(3_000);
      // Its share removed: its virtiofsd ends.
      await until(() => its.every((pid) => !alive(pid)));
      // A power cut now keeps what the chat wrote before its teardown.
      const qemu = qemuPid();
      process.kill(qemu, "SIGKILL");
      await until(() => !alive(qemu));
    } finally {
      for (const pid of stalled) {
        try {
          process.kill(pid, "SIGKILL");
        } catch {
          // Gone with its guest.
        }
      }
    }
    expect(await op(OTHER, b, "run", { command: "cat ~/durable", workdir: null, timeout: 10 })).toMatchObject({ ok: { output: "durable\n" } });
  });

  it("powers its guest off once its last root has let its folder go, and boots another for the next operation", async () => {
    const a = join(dir, "a");
    await managers.at(-1)?.stop();
    managers.push(new VmManager(options));
    expect(await op(ROOT, a, "run", { command: "true", workdir: null, timeout: 10 })).toMatchObject({ ok: { returncode: 0 } });
    const qemu = qemuPid();
    const begun = performance.now();
    await managers.at(-1)!.teardown(ROOT);
    await until(() => !alive(qemu));
    console.log(`idle stop: the guest powered off ${(performance.now() - begun).toFixed(0)} ms after its last root's teardown began`);
    expect(readFileSync(options.console, "utf8")).toContain("sysrq: Power Off");
    expect(await op(ROOT, a, "run", { command: "echo again", workdir: null, timeout: 10 })).toMatchObject({ ok: { output: "again\n" } });
    expect(qemuPid()).not.toBe(qemu);
  });

  it("keeps a sessions disk its guest could not check aside, and boots on a new one", async () => {
    await managers.at(-1)?.stop();
    managers.push(new VmManager(options));
    const a = join(dir, "a");
    expect(await op(ROOT, a, "run", { command: "echo kept > ~/kept-aside", workdir: null, timeout: 10 })).toMatchObject({ ok: { returncode: 0 } });
    await managers.at(-1)!.stop();
    // A feature this e2fsck does not know, as a newer mke2fs could set: e2fsck exits 8, though the disk was made.
    const known = incompat(options.sessions);
    expect(spawnSync("debugfs", ["-w", "-R", `ssv feature_incompat ${(known | 0x8000_0000) >>> 0}`, options.sessions], { stdio: "ignore" }).status).toBe(0);
    managers.push(new VmManager(options));
    expect(await op(ROOT, a, "run", { command: "cat ~/kept-aside 2>/dev/null || echo new", workdir: null, timeout: 10 })).toMatchObject({ ok: { output: "new\n" } });
    // The old one, beside the new, as it was: its homes, under a feature only a newer e2fsck knows.
    expect([statSync(`${options.sessions}.unchecked`).size, incompat(`${options.sessions}.unchecked`)]).toEqual([32 * 1024 ** 3, (known | 0x8000_0000) >>> 0]);
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

  // M10, by hand: the computer sleeps during a command, put to sleep by `systemctl suspend` and
  // woken by its user after at least three minutes. Behind SUROGATE_SUSPEND_TEST=1 as well: never on a
  // computer others are using, as every process on it sleeps too. It checks the hypothesis that the
  // guest's kvm-clock counts the sleep (its monotonic clock jumps, its wall clock does not lag), and
  // pins what holds either way: a command the computer slept through is not timed out for the sleep.
  it.skipIf(process.env.SUROGATE_SUSPEND_TEST !== "1")("finishes a command the computer slept through once it wakes, untimed-out for the sleep, its guest kept (M10)", { timeout: 900_000 }, async () => {
    // A manager of its own, with the keepalive's own pace.
    await managers.at(-1)?.stop();
    managers.push(new VmManager(options));
    const a = join(dir, "a");
    const clocks = async () => {
      const said = await op(ROOT, a, "run", { command: "cut -d' ' -f1 /proc/uptime; date +%s.%N", workdir: null, timeout: 10 }) as { ok: { output: string } };
      const [uptime = 0, wall = 0] = said.ok.output.trim().split("\n").map(Number);
      return { uptime, wall, hostWall: Date.now() / 1000, hostAwake: performance.now() / 1000 };
    };
    const qemu = (await clocks(), qemuPid());
    const before = await clocks();
    // Its timeout, 120 s, past its own 60 s and short of the sleep: a timeout kept on a clock that counted the sleep would end it.
    const running = op(ROOT, a, "run", { command: "sleep 60; echo slept", workdir: null, timeout: 120 });
    await new Promise((resolve) => setTimeout(resolve, 5_000));
    spawnSync("systemctl", ["suspend"]);
    // Asleep, the wall clock goes on and the monotonic one does not: at the wake they are apart by the sleep.
    const asleep = () => Date.now() / 1000 - before.hostWall - (performance.now() / 1000 - before.hostAwake);
    await until(() => asleep() > 10, 600_000);
    // As the shell does at powerMonitor's resume: the keepalive's tick may have told the guest first.
    managers.at(-1)!.resume();
    const ran = await running as { ok: { output: string; returncode: number; timed_out: boolean } };
    const after = await clocks();
    const awake = after.hostAwake - before.hostAwake;
    console.log(
      `M10: asleep ${asleep().toFixed(0)} s, ${awake.toFixed(0)} s awake; the guest's monotonic clock moved ${(after.uptime - before.uptime).toFixed(0)} s ` +
      `(${(after.uptime - before.uptime - awake).toFixed(0)} s past the time awake: the sleep, if kvm-clock counts it); its wall clock is ` +
      `${(after.wall - after.hostWall).toFixed(2)} s off this computer's; the command answered ${JSON.stringify(ran.ok)}`,
    );
    // Past the command's timeout and the guest's backstop, 130 s, or the run proves nothing of either.
    expect(asleep()).toBeGreaterThan(150);
    expect(ran.ok).toEqual({ output: "slept\n", returncode: 0, timed_out: false });
    expect(qemuPid()).toBe(qemu);
    expect(Math.abs(after.wall - after.hostWall)).toBeLessThan(2);
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
  let vm: VmClient;
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
    // The manager in a process of its own, as the app runs it: what it tells of a root's processes comes through its channel.
    vm = new VmClient({
      vm: {
        kernel: join(IMAGE, "vmlinuz"), rootfs: join(IMAGE, "rootfs.img"), agentDisk: agentDisk(dir), sessions: join(dir, "sessions.img"),
        run, console: join(dir, "console.log"), user: USER,
      },
    });
    const { dev, ino } = statSync(folder);
    executor = new VmExecutor({
      bindingOf: (root) => (root === CHAT ? { folder, dev, ino, boot: BOOT_ID } : undefined),
      dataDir: join(dir, "data"), env: { HOME: USER.home, LANG: "C.UTF-8", PATH: "/usr/bin:/bin" }, idleMs: 500, vm,
    });
  });

  afterAll(async () => {
    await executor?.stop();
    await vm?.stop();
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

  it("keeps the chat's file host while its background process lives, and answers for it once the guest that ran it goes", async () => {
    const started = await executor.run(operation("start", background("sleep 30")), signal()) as { ok: { session_id: string } };
    const { session_id } = started.ok;
    // Past the file host's 500 ms idle time: had it let the folder go, the root's teardown would have ended the process.
    await new Promise((resolve) => setTimeout(resolve, 1_500));
    expect(await executor.run(operation("poll", { session_id }), signal())).toMatchObject({ ok: { status: "running" } });
    process.kill(Number(readFileSync(join(run, "qemu.pid"), "utf8")), "SIGKILL");
    await new Promise((resolve) => setTimeout(resolve, 500));
    // A new guest, which the file host gives what it kept.
    expect(await executor.run(operation("poll", { session_id }), signal())).toMatchObject({
      ok: { status: "exited", exit_code: null, note: "The process ended because the computer's sandbox stopped" },
    });
  });

  it("lets a slow rebase, a cherry-pick sequence and a merge complete under the rule, and one stopped for a conflict go on or be aborted in the next command", { timeout: 120_000 }, async () => {
    const folder = join(dir, "folder");
    const long = (line: string) => executor.run(operation("run", { command: line, workdir: null, timeout: 60 }), signal());
    // A repository as the user has it: a topic of six commits on main, and a branch that changes main's file another way.
    expect(spawnSync("bash", ["-c", [
      "git init -q -b main . && git config user.email a@b && git config user.name a",
      "echo base > base.txt && git add -A && git commit -qm base",
      "git checkout -qb topic && for i in 1 2 3 4 5 6; do echo $i > t$i.txt && git add -A && git commit -qm t$i; done",
      "git checkout -qb other main && echo other > base.txt && git commit -qam other",
      "git checkout -q main && echo main > base.txt && git commit -qam main",
    ].join(" && ")], { cwd: folder }).status).toBe(0);
    // Each stops for base.txt's conflict.
    const stopped = (start: string, state: string) => `${start} >/dev/null 2>&1; test -e .git/${state} && echo stopped`;
    const resolved = "echo both > base.txt && git add base.txt && GIT_EDITOR=true";
    try {
      // The root's first command: from its answer on, the file host looks every 5 s.
      expect(await command("true")).toMatchObject({ ok: { returncode: 0 } });
      // Six picks a second apart, so a look comes while it runs, as the review's probe broke at pick 10 of 12.
      expect(await long("git checkout -q topic && out=$(git rebase -q -x 'sleep 1' main 2>&1) || echo \"$out\"; git log --format=%s main..topic | tr '\\n' ' '")).toMatchObject({
        ok: { output: "t6 t5 t4 t3 t2 t1 " },
      });
      expect(await command(stopped("git checkout -qb c1 other && git rebase main", "rebase-merge"))).toMatchObject({ ok: { output: "stopped\n" } });
      expect(await command(`${resolved} git rebase --continue >/dev/null 2>&1; git log --format=%s -2 | tr '\\n' ' '`)).toMatchObject({ ok: { output: "other main " } });
      expect(await command(stopped("git checkout -qb c2 other && git rebase main", "rebase-merge"))).toMatchObject({ ok: { output: "stopped\n" } });
      expect(await command("git rebase --abort 2>&1; git rev-parse --abbrev-ref HEAD; git status --porcelain")).toMatchObject({ ok: { output: "c2\n" } });
      expect(await command(stopped("git checkout -qb picks main && git cherry-pick topic~1 other topic", "sequencer"))).toMatchObject({ ok: { output: "stopped\n" } });
      expect(await command(`${resolved} git cherry-pick --continue >/dev/null 2>&1; git log --format=%s -4 | tr '\\n' ' '`)).toMatchObject({ ok: { output: "t6 other t5 main " } });
      expect(await command(stopped("git checkout -qb merged main && git merge other", "MERGE_HEAD"))).toMatchObject({ ok: { output: "stopped\n" } });
      expect(await command(`${resolved} git commit -q --no-edit 2>&1; git log -1 --format=%p | wc -w`)).toMatchObject({ ok: { output: "2\n" } });
      // Git's own state is gone from the host's repository, and the rule still refuses a write to its config.
      expect(["rebase-merge", "sequencer", "MERGE_HEAD"].filter((name) => existsSync(join(folder, ".git", name)))).toEqual([]);
      expect(await command("(echo '[alias] x = !evil' >> .git/config) 2>&1 | sed 's/.*: //'")).toMatchObject({ ok: { output: "Operation not permitted\n" } });
      expect(readFileSync(join(folder, ".git", "config"), "utf8")).not.toContain("evil");
    } finally {
      for (const name of [".git", "base.txt", "t1.txt", "t2.txt", "t3.txt", "t4.txt", "t5.txt", "t6.txt"]) rmSync(join(folder, name), { recursive: true, force: true });
    }
  });

  // The host's git rebase --continue runs a paused rebase's exec steps outside the sandbox, and the rule lets
  // commands write git's transient state: the look after a command comments out each step it added.
  it("strips a command-planted rebase exec line on the host, but not git's own", async () => {
    const folder = join(dir, "folder");
    const todo = join(folder, ".git", "rebase-merge", "git-rebase-todo");
    const steps = join(dir, "steps.log");
    expect(spawnSync("bash", ["-c", [
      "git init -q -b main . && git config user.email a@b && git config user.name a",
      "for i in 1 2 3; do echo $i > f$i.txt && git add -A && git commit -qm c$i; done",
    ].join(" && ")], { cwd: folder }).status).toBe(0);
    // A command of the chat's first: from its answer on, the file host looks every 5 s.
    expect(await command("git log --oneline | wc -l")).toMatchObject({ ok: { output: "3\n" } });
    // Then the user's own rebase -x, on the host, stopped before its first pick: its exec steps are the
    // user's git's, and a look while nothing of the chat's runs takes them as the user's.
    expect(spawnSync("bash", ["-c", `GIT_SEQUENCE_EDITOR='sed -i 1ibreak' git rebase -q -i -x 'echo user-step >> ${steps}' HEAD~2`], { cwd: folder }).status).toBe(0);
    const own = readFileSync(todo, "utf8");
    expect(own.match(/^exec /gm)).toHaveLength(2);
    await new Promise((resolve) => setTimeout(resolve, 6_000));
    expect(readFileSync(todo, "utf8")).toBe(own);
    try {
      // The rule lets the write through; the look after the command comments it out, and says so.
      expect(await command(`echo 'exec touch ${folder}/pwned' >> .git/rebase-merge/git-rebase-todo && echo planted`)).toMatchObject({
        ok: { output: expect.stringMatching(/^planted\n\nThe computer removed a step .*: \.git\/rebase-merge\/git-rebase-todo$/) },
      });
      expect(readFileSync(todo, "utf8")).toBe(`${own}# Surogate removed a step that appeared while the chat's commands could write: exec touch ${folder}/pwned\n`);
      // The user's git goes on, with its own steps and none of the command's.
      expect(spawnSync("git", ["rebase", "--continue"], { cwd: folder }).status).toBe(0);
      expect(existsSync(join(folder, "pwned"))).toBe(false);
      expect(readFileSync(steps, "utf8")).toBe("user-step\nuser-step\n");
    } finally {
      for (const name of [".git", "f1.txt", "f2.txt", "f3.txt", "pwned"]) rmSync(join(folder, name), { recursive: true, force: true });
      rmSync(steps, { force: true });
    }
  });

  it("runs every exec step of a guest rebase -x that runs as one long command, with no notice, and strips those of one paused at a command's end", async () => {
    const folder = join(dir, "folder");
    const long = (line: string) => executor.run(operation("run", { command: line, workdir: null, timeout: 60 }), signal());
    expect(spawnSync("bash", ["-c", [
      "git init -q -b main . && git config user.email a@b && git config user.name a",
      "for i in 1 2 3 4 5 6 7; do echo $i > f$i.txt && git add -A && git commit -qm c$i; done",
    ].join(" && ")], { cwd: folder }).status).toBe(0);
    try {
      // From its answer on, the file host looks every 5 s: at least one look comes while the rebase's eight seconds run.
      expect(await command("true")).toMatchObject({ ok: { returncode: 0 } });
      expect(await long("git rebase -q -x 'sleep 1.3; echo step >> steps.log' HEAD~6 2>&1; wc -l < steps.log")).toMatchObject({ ok: { output: "6\n" } });
      expect(existsSync(join(folder, ".git", "rebase-merge"))).toBe(false);
      // One the command leaves paused: the look after it comments out the steps it wrote.
      expect(await command("GIT_SEQUENCE_EDITOR='sed -i 1ibreak' git rebase -q -i -x 'touch guest-step' HEAD~2 >/dev/null 2>&1; echo paused")).toMatchObject({
        ok: { output: expect.stringMatching(/^paused\n\nThe computer removed a step .*: \.git\/rebase-merge\/git-rebase-todo$/) },
      });
      const todo = readFileSync(join(folder, ".git", "rebase-merge", "git-rebase-todo"), "utf8");
      expect(todo.match(/^# Surogate removed a step that appeared while the chat's commands could write: exec touch guest-step$/gm)).toHaveLength(2);
      expect(todo).not.toMatch(/^exec /m);
    } finally {
      for (const name of [".git", "steps.log", "f1.txt", "f2.txt", "f3.txt", "f4.txt", "f5.txt", "f6.txt", "f7.txt"]) rmSync(join(folder, name), { recursive: true, force: true });
    }
  });

  // A ceiling: a new linked worktree needs its .git file and its commondir, which the rule refuses, so git worktree
  // add is the host's to run. One the user made on the host stays the user's: what would send it to a config is refused.
  it("refuses git worktree add in the guest, in git's own words, and every write that would send the host's linked worktree to a config", { timeout: 60_000 }, async () => {
    const folder = join(dir, "folder");
    const admin = join(folder, ".git", "worktrees", "wt");
    const said = (line: string) => `(${line}) 2>&1 | sed 's/.*: //'`;
    expect(spawnSync("bash", ["-c", [
      "git init -q -b main . && git config user.email a@b && git config user.name a && git config extensions.worktreeConfig true",
      "echo base > base.txt && git add -A && git commit -qm base",
      "git worktree add -q wt -b wtb && git -C wt config --worktree core.editor true",
    ].join(" && ")], { cwd: folder }).status).toBe(0);
    try {
      expect(await command("git worktree add -q wt2 -b wtb2 2>&1; echo rc=$?")).toMatchObject({
        ok: { output: "fatal: could not open 'wt2/.git' for writing: Operation not permitted\nrc=128\n" },
      });
      // git takes back what it had made of it.
      expect([existsSync(join(folder, "wt2")), existsSync(join(folder, ".git", "worktrees", "wt2"))]).toEqual([false, false]);
      const commondir = readFileSync(join(admin, "commondir"), "utf8");
      // A git folder of the command's own, whose config would run a program at the next status on the host.
      expect(await command([
        "git init -q --bare evil.git && git -C evil.git config core.fsmonitor 'touch pwned'",
        said("echo \"$PWD/evil.git\" > .git/worktrees/wt/commondir"),
        said("echo '[core] fsmonitor = touch pwned' >> .git/worktrees/wt/config.worktree"),
        said("mv .git/worktrees/wt .git/worktrees/wt-old"),
        said("mv .git/worktrees .git/worktrees-old"),
        "git -C wt status --porcelain",
      ].join("; "))).toMatchObject({ ok: { output: "Operation not permitted\n".repeat(4) } });
      expect(readFileSync(join(admin, "commondir"), "utf8")).toBe(commondir);
      expect(readFileSync(join(admin, "config.worktree"), "utf8")).not.toContain("fsmonitor");
      expect(spawnSync("git", ["-C", join(folder, "wt"), "status", "--porcelain"]).status).toBe(0);
      expect(existsSync(join(folder, "wt", "pwned"))).toBe(false);
    } finally {
      for (const name of [".git", "wt", "evil.git", "base.txt"]) rmSync(join(folder, name), { recursive: true, force: true });
    }
  });

  it("runs commands beside a protected name linked out of the folder, or to a protected name in it, whose file the rule keeps", async () => {
    const folder = join(dir, "folder");
    // An editor's settings shared with a sibling worktree, which the guest does not have.
    const shared = join(dir, "shared-vscode");
    mkdirSync(shared);
    writeFileSync(join(shared, "settings.json"), "{}\n");
    symlinkSync(shared, join(folder, ".vscode"));
    mkdirSync(join(folder, ".idea"));
    writeFileSync(join(folder, ".idea", "mcp.json"), "{}\n");
    symlinkSync(".idea/mcp.json", join(folder, ".mcp.json"));
    try {
      expect(await command("echo ran; echo '{\"x\": 1}' 2>/dev/null > .mcp.json || echo refused")).toMatchObject({ ok: { output: "ran\nrefused\n" } });
      expect(await command("echo ran")).toMatchObject({ ok: { output: "ran\n" } });
      expect([readFileSync(join(folder, ".idea", "mcp.json"), "utf8"), readFileSync(join(shared, "settings.json"), "utf8")]).toEqual(["{}\n", "{}\n"]);
    } finally {
      for (const name of [".vscode", ".mcp.json", ".idea"]) rmSync(join(folder, name), { recursive: true, force: true });
      rmSync(shared, { recursive: true, force: true });
    }
  });

  // The rule judges a write by the path it reaches: through this link, mcp.json, which it does not keep.
  it("refuses a command, before it runs, while a protected name links to a file in the folder the rule does not keep, and runs it once the name is a file", async () => {
    const folder = join(dir, "folder");
    writeFileSync(join(folder, "mcp.json"), "{}\n");
    symlinkSync("mcp.json", join(folder, ".mcp.json"));
    try {
      // A look sees the link the host made, at the host's start or after this command: commands are refused from the next one.
      await command("true");
      expect(await command("touch ran")).toEqual({
        error: { type: "sandbox", message: "Blocked: .mcp.json is a link to mcp.json in this folder. Make it a file or folder of its own, or point it outside the folder or at a protected name, to run commands here." },
      });
      expect(existsSync(join(folder, "ran"))).toBe(false);
      rmSync(join(folder, ".mcp.json"));
      writeFileSync(join(folder, ".mcp.json"), "{}\n");
      expect(await command("touch ran && echo ran")).toMatchObject({ ok: { output: "ran\n" } });
    } finally {
      for (const name of [".mcp.json", "mcp.json", "ran"]) rmSync(join(folder, name), { force: true });
    }
  });

  // A write through .git or .claude reaches the config or the commands under where it leads; so does one
  // through a chain that leaves the folder and comes back. Each layout returns what it made in the folder.
  const layouts: Array<[string, (folder: string) => string[], string]> = [
    ["a .git", (folder) => {
      mkdirSync(join(folder, "realgit"));
      writeFileSync(join(folder, "realgit", "config"), "[core]\n");
      symlinkSync("realgit", join(folder, ".git"));
      return [".git", "realgit"];
    }, ".git is a link to realgit"],
    [".claude", (folder) => {
      mkdirSync(join(folder, "dotclaude", "commands"), { recursive: true });
      symlinkSync("dotclaude", join(folder, ".claude"));
      return [".claude", "dotclaude"];
    }, ".claude is a link to dotclaude"],
    ["a chain that leaves the folder and comes back", (folder) => {
      mkdirSync(join(folder, "config"));
      symlinkSync(join(folder, "config", "mcp.json"), join(dir, "hop"));
      symlinkSync(join(dir, "hop"), join(folder, ".mcp.json"));
      return [".mcp.json", "config", "../hop"];
    }, ".mcp.json is a link to config/mcp.json"],
  ];
  for (const [title, layout, linked] of layouts) {
    it(`refuses a command, before it runs, while ${title} links into the folder, and runs it once the link is gone`, async () => {
      const folder = join(dir, "folder");
      const made = layout(folder);
      const clear = () => {
        for (const name of [...made, "ran"]) rmSync(join(folder, name), { recursive: true, force: true });
      };
      try {
        await command("true");
        expect(await command("touch ran")).toEqual({
          error: { type: "sandbox", message: `Blocked: ${linked} in this folder. Make it a file or folder of its own, or point it outside the folder or at a protected name, to run commands here.` },
        });
        expect(existsSync(join(folder, "ran"))).toBe(false);
        clear();
        expect(await command("echo ran")).toMatchObject({ ok: { output: "ran\n" } });
      } finally {
        clear();
      }
    });
  }

  // A command can unpack an editor's folder below node_modules (the rule leaves dependency folders alone),
  // and a link outside it would show that folder where editors read it.
  it("refuses a command, before it runs, while a link a command made leads into a dependency folder, and runs it once the link is gone", async () => {
    const folder = join(dir, "folder");
    const clear = () => {
      for (const name of ["node_modules", "sub", "ran"]) rmSync(join(folder, name), { recursive: true, force: true });
    };
    try {
      expect(await command("mkdir -p node_modules/p/.vscode && echo '{}' > node_modules/p/.vscode/tasks.json && ln -s node_modules/p sub && echo made")).toMatchObject({
        ok: { output: "made\n" },
      });
      expect(await command("touch ran")).toEqual({
        error: { type: "sandbox", message: "Blocked: sub leads into node_modules. Remove the link to run commands here." },
      });
      expect(existsSync(join(folder, "ran"))).toBe(false);
      rmSync(join(folder, "sub"));
      expect(await command("echo ran")).toMatchObject({ ok: { output: "ran\n" } });
    } finally {
      clear();
    }
  });

  it("shows a command what the file tools wrote just before it, each time, with nothing to wait for", async () => {
    const folder = join(dir, "folder");
    const key = join(folder, "lint.py");
    const write = (text: string) => executor.run(operation("write", { key, data: Buffer.from(text).toString("base64") }), signal());
    let stale = 0;
    let begun = performance.now();
    for (let i = 0; i < 1_000; i += 1) {
      expect(await write(`x = ${i}\n`)).toEqual({ ok: null });
      if ((await command("cat lint.py") as { ok: { output: string } }).ok.output !== `x = ${i}\n`) stale += 1;
    }
    const seen = (performance.now() - begun) / 1_000;
    // A patch, then its lint, fifty times: the lint sees each patch.
    begun = performance.now();
    for (let i = 0; i < 50; i += 1) {
      expect(await write(i % 2 ? `x = ${i}\n` : "x = (\n")).toEqual({ ok: null });
      expect(await command("python3 -m py_compile lint.py 2>/dev/null && echo clean || echo broken")).toMatchObject({ ok: { output: i % 2 ? "clean\n" : "broken\n" } });
    }
    console.log(`M4: ${stale} of 1000 commands right after a write saw the old file; a write and the command after it ${seen.toFixed(1)} ms, a patch and its lint ${((performance.now() - begun) / 50).toFixed(0)} ms`);
    expect(stale).toBe(0);
    rmSync(key, { force: true });
  });

  it("keeps the file tools in the folder while a command in the guest flips a folder of it into a link out of it, through 5 000 operations", { timeout: 300_000 }, async () => {
    const folder = join(dir, "folder");
    const outside = join(dir, "outside");
    mkdirSync(outside);
    writeFileSync(join(outside, "only-outside"), "OUTSIDE\n");
    // In the guest the link's target names nothing; on the host it leads out of the folder.
    const flipper = `while :; do rm -rf sub; mkdir sub; echo inside > sub/inside; rm -rf sub; ln -s '${outside}' sub; done`;
    const started = await executor.run(operation("start", background(flipper)), signal()) as { ok: { session_id: string } };
    const tally: Record<string, number> = {};
    const count = (kind: string, outcome: Outcome) => {
      const error = "error" in outcome ? outcome.error as { type: string; code?: string } : null;
      const key = `${kind} ${error ? error.code ?? error.type : "ok"}`;
      tally[key] = (tally[key] ?? 0) + 1;
    };
    try {
      for (let i = 0; i < 1_250; i += 1) {
        count("write", await executor.run(operation("write", { key: join(folder, "sub", `x-${i}`), data: Buffer.from("m8\n").toString("base64") }), signal()));
        const read = await executor.run(operation("read", { key: join(folder, "sub", "only-outside"), max_bytes: null }), signal());
        expect("ok" in read && Buffer.from(String(read.ok), "base64").toString()).not.toBe("OUTSIDE\n");
        count("read", read);
        const listed = await executor.run(operation("list_dir", { key: join(folder, "sub") }), signal());
        expect("ok" in listed && (listed.ok as string[]).includes("only-outside")).toBe(false);
        count("list_dir", listed);
        count("delete", await executor.run(operation("delete", { key: join(folder, "sub", "only-outside") }), signal()));
      }
    } finally {
      await executor.run(operation("kill", { session_id: started.ok.session_id }), signal());
      rmSync(join(folder, "sub"), { recursive: true, force: true });
    }
    console.log(`M8: 5 000 file operations against a guest command's flips: ${JSON.stringify(tally)}`);
    expect(readdirSync(outside)).toEqual(["only-outside"]);
    expect(readFileSync(join(outside, "only-outside"), "utf8")).toBe("OUTSIDE\n");
  });

  it("ends what a command left running once the chat's file host lets its folder go", async () => {
    // What a background process left, in a session of its own and with its environment cleared, once that process has ended.
    const leaver = "env -i /usr/bin/setsid /usr/bin/nohup /usr/bin/sleep 300 < /dev/null > /dev/null 2>&1 & echo started";
    expect(await executor.run(operation("start", background(leaver)), signal())).toMatchObject({ ok: { session_id: expect.any(String) } });
    expect(await command("sleep 0.5; pgrep -c -x sleep")).toMatchObject({ ok: { output: "1\n" } });
    // Idle for 500 ms, the file host tears the guest's root down, then lets the folder go.
    await new Promise((resolve) => setTimeout(resolve, 2_000));
    const begun = performance.now();
    expect(await executor.run(operation("which", { name: "pandoc" }), signal())).toEqual({ ok: true });
    console.log(`which: ${(performance.now() - begun).toFixed(0)} ms with its file host cold and the guest warm`);
    expect(await command("pgrep -c -x sleep || true")).toMatchObject({ ok: { output: "0\n" } });
  });
});

describe.skipIf(process.env.SUROGATE_VM_TESTS !== "1")("the network, through the VmExecutor and the guest", { timeout: 60_000 }, () => {
  const CHAT = "6a7b8c9d-0e1f-4a2b-8c3d-4e5f6a7b8c9d";
  let dir: string;
  let run: string;
  let folder: string;
  let vm: VmClient;
  let executor: VmExecutor;
  let journal: OperationJournal;
  // Every prompt the chat's user was shown, and what they answer each network one, after a moment.
  let prompts: ApprovalRequest[];
  let answer: (request: Extract<ApprovalRequest, { kind: "network" }>) => ApprovalAnswer;
  // Whether the chat's user leaves each network prompt open until it is dismissed.
  let held: boolean;
  const user: ApprovalPrompts = {
    approve: async (request, dismissed) => {
      prompts.push(request);
      if (held && request.kind === "network") return new Promise((resolve) => dismissed.addEventListener("abort", () => resolve("deny"), { once: true }));
      await new Promise((resolve) => setTimeout(resolve, 300));
      return request.kind === "network" ? answer(request) : "allow";
    },
    confirmFreeMode: async () => false,
  };
  const operation = (kind: string, args: Record<string, unknown>): Operation => ({
    id: `${kind}-${Math.random()}`, sessionId: CHAT, callingSessionId: CHAT, invocationId: "call", ordinal: 1, kind, args, digest: "d",
  });
  const command = (line: string, timeout = 60) => executor.run(operation("run", { command: line, workdir: null, timeout }), signal());
  const networkPrompts = () => prompts.filter((prompt) => prompt.kind === "network").map(({ host, port, privateNetwork }) => ({ host, port, privateNetwork }));
  // The HTTP status a command's curl gets, 000 for none.
  const status = (url: string, flags = "") => `curl -sS --max-time 20 ${flags} -o /dev/null -w '%{http_code}\\n' ${url} 2>/dev/null`;

  beforeAll(() => {
    dir = realpathSync(mkdtempSync(join(tmpdir(), "vm-guest-network-")));
    folder = join(dir, "folder");
    mkdirSync(folder);
    run = mkdtempSync(join(process.env.XDG_RUNTIME_DIR ?? "/tmp", "sg-vm-"));
    vm = new VmClient({
      vm: {
        kernel: join(IMAGE, "vmlinuz"), rootfs: join(IMAGE, "rootfs.img"), agentDisk: agentDisk(dir), sessions: join(dir, "sessions.img"),
        run, console: join(dir, "console.log"), user: USER,
      },
    });
    // The chat as the app binds one, working freely: only the network asks.
    journal = new OperationJournal(join(dir, "journal.sqlite"));
    const { dev, ino } = statSync(folder);
    journal.bindings.add({ root: CHAT, nonce: "nonce", folder, dev, ino, boot: BOOT_ID, mode: "free", boundAt: 1 });
    const approvals = new Approvals({ bindings: journal.bindings, prompts: user, agent: "the VM tests" });
    executor = new VmExecutor({
      bindingOf: (root) => journal.bindings.get(root), dataDir: join(dir, "data"), env: { HOME: USER.home, LANG: "C.UTF-8", PATH: "/usr/bin:/bin" },
      network: { askNetwork: (root, asked, cancel) => approvals.askNetwork(root, asked, cancel) }, vm,
    });
  });

  beforeEach(() => {
    prompts = [];
    answer = () => "allow";
    held = false;
  });

  afterAll(async () => {
    await executor?.stop();
    await vm?.stop();
    journal?.close();
    rmSync(run, { recursive: true, force: true });
    rmSync(dir, { recursive: true, force: true });
  });

  it("installs a package from PyPI with no prompt", async () => {
    // The image's pip is uv's, into the root's own PYTHONUSERBASE.
    expect(await command("pip install --no-cache-dir --no-deps --reinstall --quiet cowsay==6.1 && python3 -c 'import cowsay; print(cowsay.__file__)'", 120)).toEqual({
      ok: { output: `${USER.home}/.local/lib/python3.12/site-packages/cowsay/__init__.py\n`, returncode: 0, timed_out: false },
    });
    expect(prompts).toEqual([]);
  });

  // The rule leaves what package managers unpack alone: iconv-lite's tarball holds an .idea folder.
  it("installs an npm package that ships an editor's folder into the shared folder, every file of it", async () => {
    try {
      const npm = async (spec: string) => {
        const installed = await command(`npm install --no-audit --no-fund --no-update-notifier --cache ~/npm-cache ${spec} 2>&1; echo rc=$?`, 180);
        const { output } = (installed as { ok: { output: string } }).ok;
        expect(output).toMatch(/(added|changed) \d+ packages?[^]*\nrc=0\n$/);
        expect(output).not.toMatch(/TAR_ENTRY_ERROR|EPERM|not permitted/);
      };
      await npm("iconv-lite@0.6.3");
      expect(readdirSync(join(folder, "node_modules", "iconv-lite", ".idea"))).toContain("codeStyles");
      // Another version: npm renames the installed one aside within node_modules first, then unpacks this one.
      await npm("iconv-lite@0.6.2");
      expect(JSON.parse(readFileSync(join(folder, "node_modules", "iconv-lite", "package.json"), "utf8")).version).toBe("0.6.2");
      // One with a program: npm links it in node_modules/.bin, a link inside the dependency folder, and commands still run.
      await npm("semver@7.6.3");
      expect(await command("readlink node_modules/.bin/semver")).toMatchObject({ ok: { output: "../semver/bin/semver.js\n" } });
      expect(prompts).toEqual([]);
    } finally {
      for (const name of ["node_modules", "package.json", "package-lock.json"]) rmSync(join(folder, name), { recursive: true, force: true });
    }
  });

  it("asks once for a site's connections in flight, lets them through once allowed, and every port of a host allowed for the session", async () => {
    expect(await command(`${status("https://example.com/")} & ${status("https://example.com/")} & wait`)).toEqual({
      ok: { output: "200\n200\n", returncode: 0, timed_out: false },
    });
    expect(networkPrompts()).toEqual([{ host: "example.com", port: 443, privateNetwork: false }]);
    // Allowed once: the next connection asks again, and this answer lasts.
    answer = () => "allow_session";
    expect(await command(status("https://example.com/"))).toMatchObject({ ok: { output: "200\n" } });
    expect(await command(`${status("https://example.com/")}; ${status("http://example.com/")}`)).toMatchObject({ ok: { output: "200\n200\n" } });
    expect(networkPrompts()).toHaveLength(2);
    expect(journal.bindings.domains(CHAT)).toEqual(["example.com"]);
  });

  it("refuses this computer's own services without asking, however a command names them, and says so", async () => {
    const lan = Object.values(networkInterfaces()).flat().find((entry) => entry && !entry.internal && entry.family === "IPv4")?.address;
    const targets = ["http://127.0.0.1:9/", "http://localhost:9/", "http://[::1]:9/", ...(lan ? [`http://${lan}:9/`] : [])];
    const outcome = await command(targets.map((url) => status(url, "--noproxy ''")).join("; "));
    const named = ["127.0.0.1:9", "localhost:9", "[::1]:9", ...(lan ? [`${lan}:9`] : [])].join(", ");
    expect(outcome).toEqual({
      ok: { output: `${"403\n".repeat(targets.length)}\nThis computer does not let a chat reach its own network services (${named})`, returncode: 0, timed_out: false },
    });
    expect(prompts).toEqual([]);
  });

  it("asks about a private network saying so, and tells the agent what its user denied", async () => {
    answer = () => "deny";
    expect(await command(status("http://10.255.255.1:9/"))).toEqual({
      ok: { output: "403\n\nThis computer did not allow network access to 10.255.255.1:9.", returncode: 0, timed_out: false },
    });
    expect(networkPrompts()).toEqual([{ host: "10.255.255.1", port: 9, privateNetwork: true }]);
  });

  it("times a command out at its own timeout while a connection of it waits for its user, and says it still waits", async () => {
    held = true;
    expect(await command("curl -sS -o /dev/null http://192.0.2.1:9/ 2>/dev/null; echo done", 3)).toEqual({
      ok: { output: "Command timed out after 3 seconds\nStill waiting for this computer's user to allow network access to 192.0.2.1:9.", returncode: 124, timed_out: true },
    });
    expect(networkPrompts()).toEqual([{ host: "192.0.2.1", port: 9, privateNetwork: false }]);
    // Once nothing of the chat runs its prompt is dismissed, a denial the next command is told of.
    expect(await command("echo next")).toEqual({
      ok: { output: "next\n\nThis computer did not allow network access to 192.0.2.1:9.", returncode: 0, timed_out: false },
    });
  });

  it("shows a command no network device but its own loopback, and no name server", async () => {
    expect(await command("ip -o link | cut -d' ' -f2; getent hosts example.com || echo no lookup")).toEqual({
      ok: { output: "lo:\nno lookup\n", returncode: 0, timed_out: false },
    });
  });

  it("carries a large download, a package set and an npm install through the host proxy, timed against this computer's own", { timeout: 1_800_000 }, async () => {
    const WHEEL = "https://files.pythonhosted.org/packages/8b/5c/36c114d120bfe10f9323ed35061bc5878cc74f3f594003854b0ea298942f/torch-2.5.1-cp312-cp312-manylinux1_x86_64.whl";
    const SET = "numpy pandas scipy pyarrow scikit-learn matplotlib pillow lxml cryptography grpcio";
    // uv's, as the image's pip is: into a folder of its own, past what the image has.
    const PIP = `--no-cache --no-deps --reinstall --quiet --python-version 3.12 ${SET}`;
    const NPM = "--no-audit --no-fund --no-package-lock --no-update-notifier --silent webpack@5 eslint@9 typescript@5";
    const scratch = mkdtempSync(join(tmpdir(), "vm-guest-measured-"));
    const uv = spawnSync("bash", ["-c", "command -v uv"], { encoding: "utf8" }).stdout.trim();
    const seconds = (begun: number) => (performance.now() - begun) / 1000;
    const span = (times: number[]) => `${Math.min(...times).toFixed(1)}–${Math.max(...times).toFixed(1)} s`;
    // *line* through the guest and *here* on this computer, each from a cold cache, as the guest's
    // is: in turn and twice, so neither always goes first. The guest's last outcome, and each one's range.
    const twice = async (line: string, expected: object, here: ((turn: number) => string) | null) => {
      const inGuest: number[] = [];
      const onHost: number[] = [];
      const native = (turn: number) => {
        if (!here) return;
        const begun = performance.now();
        spawnSync("bash", ["-c", here(turn)], { encoding: "utf8", timeout: 600_000 });
        onHost.push(seconds(begun));
      };
      let outcome: Outcome = { ok: null };
      for (const turn of [0, 1]) {
        if (turn === 1) native(turn);
        const begun = performance.now();
        outcome = await command(line, 600);
        inGuest.push(seconds(begun));
        expect(outcome).toMatchObject(expected);
        if (turn === 0) native(turn);
      }
      return { outcome, guest: span(inGuest), here: onHost.length > 0 ? span(onHost) : "no uv" };
    };
    try {
      const wheel = await twice(`curl -sS -o /dev/null -w '%{size_download}' ${WHEEL}`, { ok: { output: "906389343", returncode: 0 } }, () => `curl -sS -o /dev/null ${WHEEL}`);
      const set = await twice(
        `uv pip install --python /opt/venv/bin/python --target ~/measured-pip ${PIP} && ls ~/measured-pip | grep -c dist-info; rm -rf ~/measured-pip`,
        { ok: { output: "10\n", returncode: 0 } },
        uv ? (turn) => `${uv} pip install --python-platform x86_64-manylinux_2_28 --target ${scratch}/pip-${turn} ${PIP}` : null,
      );
      const npm = await twice(
        `npm install --cache ~/measured-npm-cache --prefix ~/measured-npm ${NPM} && ls ~/measured-npm/node_modules | wc -l; rm -rf ~/measured-npm ~/measured-npm-cache`,
        { ok: { returncode: 0 } },
        (turn) => `npm install --cache ${scratch}/npm-cache-${turn} --prefix ${scratch}/npm-${turn} ${NPM}`,
      );
      expect(prompts).toEqual([]);
      console.log([
        `M7, twice each in turn: a 906 MB wheel in ${wheel.guest} through the guest, ${wheel.here} on this computer`,
        `ten packages by uv in ${set.guest}, ${set.here} on this computer`,
        `npm install of ${String((npm.outcome as { ok: { output: string } }).ok.output).trim()} top-level packages in ${npm.guest}, ${npm.here} on this computer`,
      ].join("; "));
    } finally {
      rmSync(scratch, { recursive: true, force: true });
    }
  });
});
