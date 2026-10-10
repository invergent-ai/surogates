// A folder's history as the agent disk carries it, run in the guest by the guest's own python and git:
// the tree at /run/surogate/agent/history, one request a run, as the guest's root outside every
// root's namespaces, which is where the agent will run it. Master's agent has no way in to it yet,
// so the guest's init starts the scenario (guest-history-tree.py) beside the agent, on the guest's
// own disk, and it says each check on the console. Behind SUROGATE_VM_TESTS=1.

import { mkdtempSync, readFileSync, realpathSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { bootLinux } from "../../src/vm/linux.js";
import { Guest, type VmOptions } from "../../src/vm/manager.js";
import { agentDiskWith, altered, IMAGE, KVM, needsKvm, until, USER } from "./guest-support.js";

beforeAll(needsKvm);

const SCENARIO = readFileSync(fileURLToPath(new URL("guest-history-tree.py", import.meta.url)));
// Written out and started by the guest's init, each line it says marked; the boot goes on beside it.
const RUNS = [
  `echo ${SCENARIO.toString("base64")} | base64 -d > /run/history-tree.py`,
  "(/usr/local/bin/python3 -I -u /run/history-tree.py 2>&1 | sed -u 's/^/history-tree: /') &",
].join("\n");

describe.skipIf(process.env.SUROGATE_VM_TESTS !== "1")("a folder's history in the guest", { timeout: 120_000 }, () => {
  let dir: string;
  let options: VmOptions;
  let guest: Guest;

  beforeAll(async () => {
    dir = realpathSync(mkdtempSync(join(tmpdir(), "vm-history-tree-")));
    options = {
      kernel: join(IMAGE, "vmlinuz"), rootfs: join(IMAGE, "rootfs.img"), agentDisk: agentDiskWith(dir, altered([/^exec /m, `${RUNS}\nexec `])),
      sessions: join(dir, "sessions.img"),
      // Under $XDG_RUNTIME_DIR: a vhost-user socket's path must fit in 108 bytes.
      run: mkdtempSync(join(process.env.XDG_RUNTIME_DIR ?? "/tmp", "sg-vm-")), console: join(dir, "console.log"), user: USER, kvm: KVM,
    };
    guest = await Guest.boot(bootLinux, options);
  }, 120_000);

  afterAll(async () => {
    await guest?.stop();
    if (options) rmSync(options.run, { recursive: true, force: true });
    if (dir) rmSync(dir, { recursive: true, force: true });
  });

  it("lands two threads' turns from the tree on the agent disk, and finishes a record cut after its push and a copy's move cut before its base", async () => {
    const lines = () => readFileSync(options.console, "utf8").split("\n").flatMap((line) => /history-tree: (.*?)\r?$/.exec(line)?.[1] ?? []);
    await until(() => lines().some((line) => line === "passed" || line === "failed"), 100_000);
    const said = lines();
    console.log(`the guest's: ${said[0]}`);
    expect(said.slice(1)).toEqual([
      "each thread's copy is made",
      "the first thread's turn landed, and what it kept may be forgotten only then",
      "a record cut after its push is finished at the thread's next open, and its next landing takes nothing of the other's",
      "what a copy held beyond its turn is set aside, named at every open, and the copy is not put back to it",
      "a clean copy's move to main cut before its base moved is finished by the next act",
      "passed",
    ]);
    expect(said[0]).toMatch(/^python 3\.\d+\.\d+, git version \d+\.\d+/);
  });
});
