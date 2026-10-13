// The protected-names rule (vm/rule.bpf.c) as the guest kernel runs it, for its second load-time value: the set
// of names it refuses besides the protected ones, which the kernel's line names (surogate.names=, vm/init).
// Windows's names those Explorer acts on (files/explorer-names.ts); posix's, Linux's and macOS's, none more.
// Under QEMU and KVM, behind SUROGATE_VM_TESTS=1, with init's read of the line given the set to test.

import { chmodSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { ControlLink } from "../../src/vm/control.js";
import { bootLinux } from "../../src/vm/linux.js";
import { Guest, type VmOptions } from "../../src/vm/manager.js";
import { agentDiskWith, altered, ARCH, folderOf, IMAGE, KVM, needsKvm, ROOT, signal, USER } from "./guest-support.js";

beforeAll(needsKvm);

// init's read of the set, given *line* in place of the kernel's whole command line.
const named = (line: string): [string, string] => ['name_set "$(cat /proc/cmdline)"', `name_set "${line}"`];
// Each name, made, renamed to, and written to where it is already, by a session in its folder; and two names beside them.
const NAMES = ["desktop.ini", "autorun.inf", "a.lnk", "b.url", "c.scf", "d.library-ms", "e.searchconnector-ms", "Desktop.INI", "f.LNK", "x.ſcf"];
const PROBE = [
  ...NAMES.flatMap((name, n) => [
    `touch '${name}' 2>/dev/null && echo 'made ${name}' || echo 'refused ${name}'`,
    `echo x > 'w${n}' && mv 'w${n}' '${name}' 2>/dev/null && echo 'renamed ${name}' || echo 'kept ${name}'`,
    `{ echo x >> 'old/${name}'; } 2>/dev/null && echo 'wrote ${name}' || echo 'left ${name}'`,
  ]),
  "touch notes.txt 2>/dev/null && echo 'made notes.txt' || echo 'refused notes.txt'",
  "touch desktop.ini.txt 2>/dev/null && echo 'made desktop.ini.txt' || echo 'refused desktop.ini.txt'",
].join("\n");

describe.skipIf(process.env.SUROGATE_VM_TESTS !== "1")("the rule's names by the set the kernel's line names", { timeout: 120_000 }, () => {
  let dir: string;
  let options: VmOptions;

  beforeAll(() => {
    dir = realpathSync(mkdtempSync(join(tmpdir(), "vm-names-")));
    options = {
      kernel: join(IMAGE, "vmlinuz"), rootfs: join(IMAGE, "rootfs.img"), arch: ARCH, agentDisk: "",
      sessions: join(dir, "sessions.img"),
      // Under $XDG_RUNTIME_DIR: a vhost-user socket's path must fit in 108 bytes.
      run: mkdtempSync(join(process.env.XDG_RUNTIME_DIR ?? "/tmp", "sg-vm-")), console: join(dir, "console.log"), user: USER, kvm: KVM,
    };
  });

  afterAll(() => {
    if (options) rmSync(options.run, { recursive: true, force: true });
    if (dir) rmSync(dir, { recursive: true, force: true });
  });

  // The probe's lines, as a session ran them in a folder of its own, on a guest whose init reads the set from *line*.
  const said = async (line: string, folder: string) => {
    mkdirSync(join(folder, "old"), { recursive: true, mode: 0o777 });
    chmodSync(join(folder, "old"), 0o777);
    for (const name of NAMES) {
      writeFileSync(join(folder, "old", name), "");
      chmodSync(join(folder, "old", name), 0o666);
    }
    const guest = await Guest.boot(bootLinux, { ...options, agentDisk: agentDiskWith(dir, altered(named(line))) });
    try {
      expect(await guest.ready(ROOT, folderOf(folder))).toBeNull();
      const ran = await guest.op(ROOT, "run", { command: PROBE, workdir: null, timeout: 60 }, signal());
      expect(ran).toMatchObject({ ok: { returncode: 0, timed_out: false } });
      return (ran as { ok: { output: string } }).ok.output.trim().split("\n");
    } finally {
      await guest.stop();
    }
  };

  it("refuses a session's make, write and rename of each name Explorer acts on where the line names windows's set, and its neighbours not", async () => {
    expect(await said("surogate.names=windows", join(dir, "windows"))).toEqual([
      ...NAMES.flatMap((name) => [`refused ${name}`, `kept ${name}`, `left ${name}`]), "made notes.txt", "made desktop.ini.txt",
    ]);
  });

  it("refuses none of them where the line names posix's, as Linux's and macOS's do", async () => {
    expect(await said("surogate.names=posix", join(dir, "posix"))).toEqual([
      ...NAMES.flatMap((name) => [`made ${name}`, `renamed ${name}`, `wrote ${name}`]), "made notes.txt", "made desktop.ini.txt",
    ]);
  });

  it("fails the boot, before its hello, on a line that names neither set, or both", async () => {
    for (const [line, why] of [
      ["surogate.disks=virtio", "the kernel's command line names no surogate.names="],
      ["surogate.names=posix surogate.names=windows", "the kernel's command line names surogate.names= twice"],
    ] as const) {
      rmSync(options.console, { force: true });
      const booted = { ...options, agentDisk: agentDiskWith(dir, altered(named(line))) };
      const deadline = performance.now() + 60_000;
      const vm = await bootLinux(booted, undefined, deadline);
      const outcome = await ControlLink.open(vm.control, booted.user, deadline, vm.exited).then(() => "it said hello", (error: Error) => error.message);
      await vm.kill();
      expect(outcome).toContain("The VM exited");
      expect(readFileSync(options.console, "utf8")).toContain(`surogate: ${why}`);
    }
  });
});
