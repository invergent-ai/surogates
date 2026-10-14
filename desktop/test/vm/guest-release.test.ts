// Whether the guest can tell that nothing of a share's device is left in it (guest/root.ts, Superblocks), as macOS's
// backend asks before it gives one of Apple's eight devices another folder (src/vm/darwin.ts): here on Linux's own
// virtio-fs shares under QEMU and KVM, the guest kernel's FUSE code being Apple's guest's too. The guest's root, beside
// the agent, holds each of a place's two mounts as a root could once the agent's own have gone, and asks. Behind
// SUROGATE_VM_TESTS=1; SUROGATE_VM_IMAGE names another image folder.

import { mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { bootLinux } from "../../src/vm/linux.js";
import { Guest, type VmOptions } from "../../src/vm/manager.js";
import { agentDiskWith, altered, ARCH, DAEMON, folderOf, IMAGE, KVM, needsKvm, until, USER } from "./guest-support.js";

beforeAll(needsKvm);

const KEY = "0123456789abcdef";
const AT = `/run/surogate/places/${KEY}`;
// As the guest's root, once the place's two mounts are there, with the agent's own Superblocks: each mount's
// superblock remembered, and its backing device there; then each held as a root could hold its share once the agent's own
// mount has gone, the folder's by a file left open, the history's by a namespace of its own with a bind of it and its
// working folder there, as a root's namespace has; then each let go. Each answer a line on the console.
const PROBE = `(
  until mountpoint -q ${AT}/history && mountpoint -q ${AT}/real; do sleep 0.1; done
  /usr/bin/node --input-type=module -e '
    const { spawn } = await import("node:child_process");
    const { closeSync, existsSync, openSync, readFileSync } = await import("node:fs");
    const { BACKING_DEVICES, Superblocks, superblockOf } = await import("/run/surogate/agent/guest/root.js");
    const say = (line) => console.error("release: " + line);
    const run = (program, ...args) => new Promise((resolve) => spawn(program, args, { stdio: "ignore" }).on("exit", resolve));
    const history = "${AT}/history", real = "${AT}/real";
    const mounts = readFileSync("/proc/self/mountinfo", "utf8");
    const tagAt = (at) => mounts.split("\\n").find((line) => line.split(" ")[4] === at).split(" - ")[1].split(" ")[1];
    const [kept, opened] = [tagAt(history), tagAt(real)];
    const made = new Superblocks({ releaseMs: 1000 });
    await made.remember(kept, history);
    await made.remember(opened, real);
    say("while mounted " + (await made.released(opened)));
    const listed = [superblockOf(mounts, history, kept), superblockOf(mounts, real, opened)].map((device) => existsSync(BACKING_DEVICES + "/" + device));
    say("listed " + listed.join(" "));
    const file = openSync(real + "/file.txt", "r");
    await run("/usr/bin/umount", "-l", real);
    const holder = spawn("/usr/bin/unshare", ["--mount", "--propagation", "private", "--", "/bin/sh", "-c",
      "mkdir -p /run/held && mount --bind " + history + " /run/held && cd /run/held && echo held && exec sleep 600"], { stdio: ["ignore", "pipe", "ignore"] });
    await new Promise((resolve) => holder.stdout.once("data", resolve));
    await run("/usr/bin/umount", "-l", history);
    say("a file left open " + (await made.released(opened)));
    say("a namespace with a bind " + (await made.released(kept)));
    closeSync(file);
    holder.kill("SIGKILL");
    await new Promise((resolve) => holder.once("exit", resolve));
    say("the file closed " + (await made.released(opened)));
    say("the namespace gone " + (await made.released(kept)));
    say("done");
  '
) &
`;

describe.skipIf(process.env.SUROGATE_VM_TESTS !== "1")("a share's device released in the guest", { timeout: 120_000 }, () => {
  let dir: string;
  let options: VmOptions;

  beforeAll(() => {
    dir = realpathSync(mkdtempSync(join(tmpdir(), "vm-release-")));
    for (const name of ["store", "Documents"]) mkdirSync(join(dir, name));
    writeFileSync(join(dir, "Documents", "file.txt"), "the folder's\n");
    options = {
      kernel: join(IMAGE, "vmlinuz"), rootfs: join(IMAGE, "rootfs.img"), arch: ARCH, agentDisk: agentDiskWith(dir, altered([/^exec /m, `${PROBE}exec `])),
      sessions: join(dir, "sessions.img"), console: join(dir, "console.log"), user: USER, kvm: KVM, ...DAEMON,
      // Under $XDG_RUNTIME_DIR: a vhost-user socket's path must fit in 108 bytes.
      run: mkdtempSync(join(process.env.XDG_RUNTIME_DIR ?? "/tmp", "sg-vm-")),
    };
  });

  afterAll(() => {
    if (options) rmSync(options.run, { recursive: true, force: true });
    if (dir) rmSync(dir, { recursive: true, force: true });
  });

  it("is not while a mount of it is left, in any namespace, or a file of it is open, and is once the last has gone", async () => {
    const guest = await Guest.boot(bootLinux, options);
    try {
      expect(await guest.place({ key: KEY, history: join(dir, "store"), real: folderOf(join(dir, "Documents")) })).toBeNull();
      await until(() => readFileSync(options.console, "utf8").includes("release: done"), 60_000);
      const said = readFileSync(options.console, "utf8").split("\n").filter((line) => line.startsWith("release: ")).map((line) => line.slice(9).trim());
      expect(said).toEqual([
        "while mounted false", "listed true true", "a file left open false", "a namespace with a bind false", "the file closed true", "the namespace gone true", "done",
      ]);
    } finally {
      await guest.stop();
    }
  });
});
