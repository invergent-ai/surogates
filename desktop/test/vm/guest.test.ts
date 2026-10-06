// The guest under QEMU and KVM: the image built by images/guest/build.sh, the
// agent disk built from this package. Behind SUROGATE_VM_TESTS=1.

import { spawnSync } from "node:child_process";
import { closeSync, mkdirSync, mkdtempSync, openSync, readSync, realpathSync, rmSync, statSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { agentDisk, boot, type Guest, sessionsDisk } from "./guest.js";

const ROOT = "0b6c1d3e-6f0a-4c1e-9a52-6a1d2c3b4e5f";
const OTHER = "7d8e9f00-1a2b-4c3d-8e4f-5a6b7c8d9e0f";
const FIRST_UID = 10_000;
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

describe.skipIf(process.env.SUROGATE_VM_TESTS !== "1")("the guest", { timeout: 60_000 }, () => {
  let dir: string;
  let folder: string;
  let disks: Parameters<typeof boot>[0];
  let guest: Guest;

  const run = async (command: string, root = ROOT) => {
    const answer = await guest.request({ type: "op", root, kind: "run", args: { command, workdir: null, timeout: 30 } });
    if (answer.type !== "result") throw new Error(`run answered ${JSON.stringify(answer)}`);
    return answer.outcome;
  };

  // The first boot formats the sessions disk; the tests run on the second, which checks it, as every later boot does.
  beforeAll(async () => {
    dir = realpathSync(mkdtempSync(join(tmpdir(), "vm-guest-")));
    // A folder name as people write them, with a space and a quote.
    folder = join(dir, "my folder's");
    mkdirSync(folder);
    disks = { agent: agentDisk(dir), sessions: sessionsDisk(dir), folder, uid: FIRST_UID };
    const first = await boot(disks);
    console.log(`M1, first boot, formatting the sessions disk: hello after ${first.helloMs.toFixed(0)} ms`);
    await first.stop();
    guest = await boot(disks);
  }, 60_000);

  afterAll(async () => {
    await guest?.stop();
    rmSync(dir, { recursive: true, force: true });
  });

  it("says hello within 15 s, sets up a root, and runs its command in its folder", async () => {
    expect(guest.helloMs).toBeLessThan(15_000);
    expect(await guest.request({ type: "setup", root: ROOT, folder, tag: "r1" })).toMatchObject({ type: "done" });
    expect(await run("echo hi")).toEqual({ ok: { output: "hi\n", returncode: 0, timed_out: false } });
    console.log(`M1: hello after ${guest.helloMs.toFixed(0)} ms, the first command answered after ${(performance.now() - guest.launched).toFixed(0)} ms`);
    expect(await run("pwd; echo $HOME; id -un")).toEqual({
      ok: { output: `${folder}\n${guest.user.home}\n${guest.user.name}\n`, returncode: 0, timed_out: false },
    });
    expect(await guest.request({ type: "setup", root: ROOT, folder, tag: "r1" })).toMatchObject({
      type: "failed", message: "This chat's sandbox is already set up",
    });
    expect(await run("true", OTHER)).toMatchObject({ error: { type: "unavailable" } });
    for (const [field, value, message] of [
      ["tag", "../r1", "not a share tag: ../r1"],
      ["folder", "relative/path", "not a folder: relative/path"],
      ["folder", "/", "not a folder: /"],
      ["root", "../etc", "not a root session id: ../etc"],
    ] as const) {
      expect(await guest.request({ type: "setup", root: OTHER, folder, tag: "r1", [field]: value })).toEqual(expect.objectContaining({
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
    const which = async (name: string) => {
      const answer = await guest.request({ type: "op", root: ROOT, kind: "which", args: { name } });
      return answer.type === "result" ? answer.outcome : answer;
    };
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
      "grep -E '^(CapEff|NoNewPrivs):' /proc/self/status | tr -s '\\t' ' '",
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
          "lo:", "tini", "CapEff: 0000000000000000", "NoNewPrivs: 1", "agent", "Permission denied", "Read-only file system",
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

  it("keeps two roots apart, and one that fills its memory, with bytes or with files, leaves the other room", async () => {
    expect(await guest.request({ type: "setup", root: OTHER, folder, tag: "r1" })).toMatchObject({ type: "done" });
    const filled = await run([
      "head -c 900M /dev/zero > /var/tmp/fill 2>/dev/null; head -c 900M /dev/zero > /dev/shm/fill 2>/dev/null; du -m /var/tmp/fill /dev/shm/fill | cut -f1",
      `python3 -c '${FILES}'`,
      "df --output=itotal,iavail /var/tmp /dev/shm | tail -n +2 | tr -s ' '",
    ].join("; "));
    expect(await run([
      "python3 -c 'b = bytearray(300 * 1024 * 1024); print(len(b) >> 20)'",
      "id -u",
      "find ~ /var/tmp /dev/shm -mindepth 1 | wc -l",
      "ps -e -o comm= | sort | tr '\\n' ' '; echo",
      "(echo more >> made-in-guest) 2>&1 | sed 's/.*: //'",
      "touch /var/tmp/own /dev/shm/own && echo files of its own",
    ].join("; "), OTHER)).toEqual({
      ok: { output: `300\n${FIRST_UID + 1}\n0\nbash node ps sort tini tr \nPermission denied\nfiles of its own\n`, returncode: 0, timed_out: false },
    });
    expect(filled).toEqual({ ok: { output: "256\n64\nENOSPC ENOSPC \n 32768 0\n 8192 0\n", returncode: 0, timed_out: false } });
    await run("rm -rf /var/tmp/fill /dev/shm/fill /var/tmp/many /dev/shm/many");
  });

  it("repairs a sessions disk the quick check cannot, and keeps the homes on it", async () => {
    expect(await run("echo kept > ~/kept; touch ~/victim; sync")).toEqual({ ok: { output: "", returncode: 0, timed_out: false } });
    await guest.stop();
    // A file in use marked deleted, on a disk marked not clean, after the journal is replayed so it cannot undo that.
    for (const argv of [
      ["e2fsck", "-p", "-E", "journal_only", disks.sessions],
      ["debugfs", "-w", "-R", `set_inode_field /roots/${ROOT}/home/victim dtime 12345`, disks.sessions],
      ["debugfs", "-w", "-R", "ssv state 0", disks.sessions],
    ]) {
      expect(spawnSync(argv[0] as string, argv.slice(1), { stdio: "ignore" }).status).toBe(0);
    }
    guest = await boot(disks);
    expect(await guest.request({ type: "setup", root: ROOT, folder, tag: "r1" })).toMatchObject({ type: "done" });
    expect(await run("cat ~/kept")).toEqual({ ok: { output: "kept\n", returncode: 0, timed_out: false } });
  });

  it("stops the boot, and keeps the homes, on a sessions disk e2fsck cannot check", async () => {
    await guest.stop();
    const incompat = () => {
      const fd = openSync(disks.sessions, "r");
      try {
        const field = Buffer.alloc(4);
        readSync(fd, field, 0, 4, INCOMPAT);
        return field.readUInt32LE(0);
      } finally {
        closeSync(fd);
      }
    };
    expect(spawnSync("e2fsck", ["-p", "-E", "journal_only", disks.sessions], { stdio: "ignore" }).status).toBe(0);
    // A feature this e2fsck does not know, as a newer mke2fs could set: e2fsck exits 8, though the disk was made.
    const known = incompat();
    expect(spawnSync("debugfs", ["-w", "-R", `ssv feature_incompat ${(known | 0x8000_0000) >>> 0}`, disks.sessions], { stdio: "ignore" }).status).toBe(0);
    const failed = await boot(disks).then(async (booted) => {
      await booted.stop();
      return null;
    }, (error: Error) => error);
    expect(failed?.message ?? "booted").toContain("surogate: e2fsck could not check the sessions disk (exit code 8), so the guest stops and leaves it as it is");
    // debugfs opens a disk with a feature it does not know only when forced.
    const restored = spawnSync("debugfs", ["-f", "-"], {
      input: `open -w -f ${disks.sessions}\nssv feature_incompat ${known}\nclose\n`, stdio: ["pipe", "ignore", "ignore"],
    });
    expect([restored.status, incompat()]).toEqual([0, known]);
    guest = await boot(disks);
    expect(await guest.request({ type: "setup", root: ROOT, folder, tag: "r1" })).toMatchObject({ type: "done" });
    expect(await run("cat ~/kept")).toEqual({ ok: { output: "kept\n", returncode: 0, timed_out: false } });
  });

  it("gives the root the host user's name, an image account's or a directory's too, and refuses one a passwd line cannot hold", async () => {
    for (const name of ["daemon", "ana@corp.example"]) {
      await guest.stop();
      guest = await boot({ ...disks, name });
      expect(await guest.request({ type: "setup", root: ROOT, folder, tag: "r1" })).toMatchObject({ type: "done" });
      expect(await run(`id -un; id -u; getent passwd '${name}' | cut -d: -f3,6`)).toEqual({
        ok: { output: `${name}\n${FIRST_UID}\n${FIRST_UID}:${guest.user.home}\n`, returncode: 0, timed_out: false },
      });
    }
    await guest.stop();
    guest = await boot({ ...disks, name: "ana:x" });
    expect(await guest.request({ type: "setup", root: ROOT, folder, tag: "r1" })).toEqual(expect.objectContaining({
      type: "failed", message: "not a user name: ana:x",
    }));
  });
});
