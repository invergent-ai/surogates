// The guest under QEMU and KVM, booted by the VM manager: the image built by
// images/guest/build.sh, the agent disk built from this package (npm run build
// first). Behind SUROGATE_VM_TESTS=1; SUROGATE_VM_IMAGE names another image folder.

import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, statSync, watch, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { CANCELLED, SANDBOX_STOPPED } from "../../src/guest/command.js";
import { FOLDER_UNAVAILABLE } from "../../src/hosts/messages.js";
import { vmOptions } from "../../src/vm/client.js";
import { VmManager, type VmOptions } from "../../src/vm/manager.js";
import {
  agentDisk, alive, background, FIRST_UID, folderOf, IMAGE, incompat, KVM, needsKvm, newestDaemon, OTHER, ROOT, shareDaemons, signal, STUCK, until, USER,
} from "./guest-support.js";

beforeAll(needsKvm);

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
      run: mkdtempSync(join(process.env.XDG_RUNTIME_DIR ?? "/tmp", "sg-vm-")), console: join(dir, "console.log"), user: USER, kvm: KVM,
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

  it("starts QEMU and each folder's virtiofsd with none of the app's environment but its PATH", async () => {
    // What a user's shell may export, and either program acts on, or a node it started would.
    const exported = { OPENSSL_CONF: join(dir, "openssl.cnf"), NODE_OPTIONS: "--title=leaked", SUROGATE_EXPORTED: "1" };
    Object.assign(process.env, exported);
    try {
      managers.push(new VmManager(options));
      expect(await op(ROOT, join(dir, "a"), "run", { command: "true", workdir: null, timeout: 10 })).toMatchObject({ ok: { returncode: 0 } });
    } finally {
      for (const name of Object.keys(exported)) delete process.env[name];
    }
    // Each process's environment as it was started, as /proc keeps it.
    const environ = (pid: number) => readFileSync(`/proc/${pid}/environ`, "utf8").split("\0").filter(Boolean).map((entry) => entry.slice(0, entry.indexOf("=")));
    expect(environ(qemuPid())).toEqual(["PATH"]);
    expect(environ(newestDaemon(options.run))).toEqual(["PATH"]);
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
    const env = { SUROGATE_VM_IMAGE: IMAGE, XDG_RUNTIME_DIR: runtime, ...(KVM === undefined ? {} : { SUROGATE_VM_KVM: KVM }) };
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
