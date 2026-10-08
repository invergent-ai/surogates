// The guest under QEMU and KVM, booted by the VM manager: the image built by
// images/guest/build.sh, the agent disk built from this package (npm run build
// first). Behind SUROGATE_VM_TESTS=1; SUROGATE_VM_IMAGE names another image folder.
// Without KVM they fail, as Section 11 has a VM job do: a job runs them emulated only by
// naming a device that does not exist in SUROGATE_VM_KVM, as the app takes it.

import { spawnSync } from "node:child_process";
import { existsSync, lstatSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";

import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { GUEST_SYSTEM } from "../../src/binding/folder.js";
import { CANCELLED, SANDBOX_STOPPED } from "../../src/guest/command.js";
import type { ProcessHandle } from "../../src/guest/processes.js";
import { bootLinux } from "../../src/vm/linux.js";
import { Guest, type VmOptions } from "../../src/vm/manager.js";
import {
  agentDisk, agentDiskWith, altered, background, descriptors, FILES, FIRST_UID, folderOf, IMAGE, incompat, INIT, KVM, median, needsKvm, OTHER, R1, ROOT,
  shareDaemons, signal, SOCKETS, STUCK, USER,
} from "./guest-support.js";

beforeAll(needsKvm);

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
      run: mkdtempSync(join(process.env.XDG_RUNTIME_DIR ?? "/tmp", "sg-vm-")), console: join(dir, "logs", "console.log"), user: USER, kvm: KVM,
    };
    const first = await Guest.boot(bootLinux, options);
    // A /dev/kvm that opens but QEMU cannot use boots emulated by itself: the file would then pass slowly, unasked.
    if (KVM === undefined) {
      expect(first.emulated, "QEMU could not use this computer's KVM: the VM tests run emulated only with SUROGATE_VM_KVM naming a device that does not exist").toBeNull();
    }
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
    // A leak grows them by one per process or per setup (200, 30); the kernel frees dying cgroups late, a few at a time.
    expect([processed - before, after - before].map((grown) => grown < 20)).toEqual([true, true]);
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

// M12: the same workload in a guest booted emulated, under QEMU's TCG, as this computer
// would boot it with no KVM, and in one with KVM. Logged for Progress; the emulated
// guest must answer within Section 11's emulated waits.
// A job that hides KVM boots every VM test's guest emulated already: M12 measures the two side by side.
describe.skipIf(process.env.SUROGATE_VM_TESTS !== "1" || KVM !== undefined)("the emulated guest (M12)", { timeout: 600_000 }, () => {
  let dir: string;
  const runs: string[] = [];
  const pss = (pid: number) => Number(/^Pss:\s+(\d+)/m.exec(readFileSync(`/proc/${pid}/smaps_rollup`, "utf8"))?.[1] ?? 0) / 1024;

  beforeAll(() => {
    dir = realpathSync(mkdtempSync(join(tmpdir(), "vm-emulated-")));
  });

  afterAll(() => {
    for (const run of runs) rmSync(run, { recursive: true, force: true });
    rmSync(dir, { recursive: true, force: true });
  });

  // One guest's workload, booted with *kvm* the device KVM is opened from: its numbers, by name.
  async function measure(name: string, kvm: string): Promise<Record<string, number | string | boolean | null>> {
    const folder = join(dir, `${name}-folder`);
    mkdirSync(folder);
    const run = mkdtempSync(join(process.env.XDG_RUNTIME_DIR ?? "/tmp", "sg-vm-"));
    runs.push(run);
    const options: VmOptions = {
      kernel: join(IMAGE, "vmlinuz"), rootfs: join(IMAGE, "rootfs.img"), agentDisk: agentDisk(dir), sessions: join(dir, `${name}-sessions.img`),
      run, console: join(dir, `${name}-console.log`), user: USER, kvm,
    };
    // The first boot formats the sessions disk; the one measured checks it, as every later boot does.
    await (await Guest.boot(bootLinux, options)).stop();
    const guest = await Guest.boot(bootLinux, options);
    const command = (line: string, timeout = 300) => guest.op(ROOT, "run", { command: line, workdir: null, timeout }, signal());
    const took = async (work: () => Promise<unknown>) => {
      const begun = performance.now();
      const done = await work();
      expect(done).toMatchObject({ ok: { returncode: 0, timed_out: false } });
      return Math.round(performance.now() - begun);
    };
    try {
      const qemu = Number(readFileSync(join(run, "qemu.pid"), "utf8"));
      const booted = pss(qemu);
      expect(await guest.ready(ROOT, folderOf(folder))).toBeNull();
      const firstMs = Math.round(performance.now() - guest.launched);
      expect(await command("echo hi")).toMatchObject({ ok: { output: "hi\n" } });
      // What the agent reads to grow its own bounds.
      const flagged = ((await command("cat /proc/cmdline")) as { ok: { output: string } }).ok.output.includes(" surogate.emulated=1");
      const pings: number[] = [];
      for (let n = 0; n < 20; n += 1) {
        const begun = performance.now();
        expect(await guest.request({ type: "ping" })).toMatchObject({ type: "pong" });
        pings.push(performance.now() - begun);
      }
      // A file this computer wrote, read at once by a command, twenty times; then 64 MiB read through the share.
      const fresh = await took(async () => {
        for (let n = 0; n < 20; n += 1) {
          writeFileSync(join(folder, "fresh.txt"), `${n}\n`);
          expect(await command("cat fresh.txt")).toMatchObject({ ok: { output: `${n}\n` } });
        }
        return { ok: { returncode: 0, timed_out: false } };
      });
      writeFileSync(join(folder, "big.bin"), Buffer.alloc(64 * 1024 * 1024, 7));
      const readMs = await took(() => command("cat big.bin > /dev/null"));
      const pipMs = await took(() => command("pip install --no-cache-dir --quiet requests==2.32.3 && python3 -c 'import requests'"));
      const importMs = await took(() => command("python3 -c 'import numpy, pandas, matplotlib'"));
      const used = pss(qemu);
      // The keepalive's margin: the agent's answer to a ping while eight busy loops hold every vCPU for 20 s.
      const busy = guest.op(ROOT, "run", { command: "for n in 1 2 3 4 5 6 7 8; do timeout 20 sh -c 'while :; do :; done' & done; wait", workdir: null, timeout: 60 }, signal());
      let slowest = 0;
      for (const end = performance.now() + 20_000; performance.now() < end; await new Promise((resolve) => setTimeout(resolve, 500))) {
        const begun = performance.now();
        expect(await guest.request({ type: "ping" }, 90_000)).toMatchObject({ type: "pong" });
        slowest = Math.max(slowest, performance.now() - begun);
      }
      await busy;
      return {
        emulated: guest.emulated, flagged, helloMs: Math.round(guest.helloMs), firstMs, pingMs: Number(median(pings).toFixed(2)),
        freshMs: Math.round(fresh / 20), readMBs: Math.round(64 / (readMs / 1000)), pipMs, importMs,
        busyPingMs: Math.round(slowest), qemuPssMiB: Math.round(booted), qemuPssAfterMiB: Math.round(used),
      };
    } finally {
      await guest.stop();
    }
  }

  it("boots emulated with no KVM, within the emulated hello's 120 s, and runs what a guest with KVM runs", async () => {
    const emulated = await measure("tcg", join(dir, "no-kvm"));
    const kvm = await measure("kvm", "/dev/kvm");
    console.log(`M12 emulated: ${JSON.stringify(emulated)}`);
    console.log(`M12 KVM: ${JSON.stringify(kvm)}`);
    expect(emulated).toMatchObject({ emulated: "no-kvm", flagged: true });
    expect(kvm).toMatchObject({ emulated: null, flagged: false });
    expect(emulated.helloMs).toBeLessThan(120_000);
    expect(readFileSync(join(dir, "tcg-console.log"), "utf8")).toMatch(/surogate: the protected-names rule attached 11 of 11 hooks/);
  });
});
