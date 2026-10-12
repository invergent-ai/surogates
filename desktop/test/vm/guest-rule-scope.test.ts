// The protected-names rule (vm/rule.bpf.c) as the guest kernel runs it, for what it judges besides
// Linux's sessions: the names a volume that compares names without case takes as protected ones,
// folded (rule-match.h, sg_fold), which the drift test checks against protect.ts on the host; and
// its scope's one load-time uid, which a macOS VM names on the kernel's command line for its
// sessions, all of the share's owner (vm/init, guest/root.ts). Under QEMU and KVM, behind
// SUROGATE_VM_TESTS=1; SUROGATE_VM_IMAGE names another image folder.

import { chmodSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { ControlLink } from "../../src/vm/control.js";
import { bootLinux } from "../../src/vm/linux.js";
import { Guest, type VmOptions } from "../../src/vm/manager.js";
import { agentDiskWith, altered, ARCH, folderOf, IMAGE, KVM, needsKvm, ROOT, signal, until, USER } from "./guest-support.js";

beforeAll(needsKvm);

const SHARE = "/run/surogate/shares/r1";
// As init reads the kernel's command line for the scope, with surogate.uid= named too: Linux's QEMU names none.
const scoped = (uid: string) => ['scope_uid "$(cat /proc/cmdline)"', `scope_uid "$(cat /proc/cmdline) surogate.uid=${uid}"`] as [string, string];
// The rule's scope alone, the agent left Linux's: its roots keep their own uids from 10000 up, which Linux's shares map.
const RULE_ALONE: [string, string] = ['echo "$scope" > /run/surogate/scope', "echo > /run/surogate/scope"];
// As the guest's root, once a root's share is mounted: a write to a protected file and to another, by the scope's uid,
// by a uid besides it, and by root, through a mount of the share's that each can reach (the agent's is root's alone);
// then a change to the frozen scope. On the console, which init's own lines go to.
const PROBE = `(
  until mountpoint -q ${SHARE}; do sleep 0.1; done
  mkdir /run/probe && mount --bind ${SHARE} /run/probe
  for uid in 1234 1235 0; do
    for file in .git/hooks/pre-commit notes.txt; do
      if setpriv --reuid="$uid" --regid="$uid" --clear-groups sh -c "echo x >> /run/probe/$file" 2>/dev/null; then said=written; else said=refused; fi
      echo "scope: $uid $file $said"
    done
  done
  if /usr/sbin/bpftool map update pinned /sys/fs/bpf/surogate-maps/scope key 0 0 0 0 value 0 0 0 0 2>/dev/null; then echo "scope: changed"; else echo "scope: frozen"; fi
  echo "scope: done"
) &
`;

describe.skipIf(process.env.SUROGATE_VM_TESTS !== "1")("the protected-names rule in the guest kernel", { timeout: 120_000 }, () => {
  let dir: string;
  let folder: string;
  let options: VmOptions;

  beforeAll(() => {
    dir = realpathSync(mkdtempSync(join(tmpdir(), "vm-scope-")));
    folder = join(dir, "folder");
    mkdirSync(join(folder, ".git", "hooks"), { recursive: true });
    for (const file of [join(folder, ".git", "hooks", "pre-commit"), join(folder, "notes.txt")]) {
      writeFileSync(file, "");
      chmodSync(file, 0o666);
    }
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

  it("refuses a session the names a volume that compares names without case takes as protected ones, and their neighbours not", async () => {
    const guest = await Guest.boot(bootLinux, { ...options, agentDisk: agentDiskWith(dir, altered()) });
    try {
      expect(await guest.ready(ROOT, folderOf(folder))).toBeNull();
      const names = [".git/hookſ", ".git/hooKs", ".vſcode", ".gıt", ".claude", ".claude/commandſ", "sub", "sub/.Gİt", "hookſ", "ſrc", ".git/refs-ſ"];
      const command = [
        ...names.map((name) => `mkdir '${name}' 2>/dev/null && echo 'made ${name}' || echo 'refused ${name}'`),
        `touch '.git/conﬁg' 2>/dev/null && echo 'made .git/conﬁg' || echo 'refused .git/conﬁg'`,
      ].join("\n");
      const ran = await guest.op(ROOT, "run", { command, workdir: null, timeout: 30 }, signal());
      expect(ran).toMatchObject({ ok: { returncode: 0, timed_out: false } });
      expect((ran as { ok: { output: string } }).ok.output.trim().split("\n")).toEqual([
        "refused .git/hookſ", "refused .git/hooKs", "refused .vſcode", "refused .gıt", "made .claude", "refused .claude/commandſ", "made sub", "refused sub/.Gİt",
        "made hookſ", "made ſrc", "made .git/refs-ſ", "refused .git/conﬁg",
      ]);
    } finally {
      await guest.stop();
    }
  });

  it("judges the one uid the kernel's command line names as surogate.uid= besides the sessions' own, no other, and keeps it frozen", async () => {
    rmSync(options.console, { force: true });
    const init = altered(scoped("1234"), RULE_ALONE, [/^exec /m, `${PROBE}exec `]);
    const guest = await Guest.boot(bootLinux, { ...options, agentDisk: agentDiskWith(dir, init) });
    try {
      expect(await guest.ready(ROOT, folderOf(folder))).toBeNull();
      await until(() => readFileSync(options.console, "utf8").includes("scope: done"), 30_000);
      const said = readFileSync(options.console, "utf8").split("\n").filter((line) => line.startsWith("scope: ")).map((line) => line.slice(7).trim());
      expect(said).toEqual([
        "1234 .git/hooks/pre-commit refused", "1234 notes.txt written", "1235 .git/hooks/pre-commit written", "1235 notes.txt written",
        "0 .git/hooks/pre-commit written", "0 notes.txt written", "frozen", "done",
      ]);
      expect([readFileSync(join(folder, ".git", "hooks", "pre-commit"), "utf8"), readFileSync(join(folder, "notes.txt"), "utf8")]).toEqual(["x\nx\n", "x\nx\nx\n"]);
    } finally {
      await guest.stop();
    }
  });

  it("fails the boot, before its hello, on a scope no session may run as: root's", async () => {
    rmSync(options.console, { force: true });
    const booted = { ...options, agentDisk: agentDiskWith(dir, altered(scoped("0"))) };
    const deadline = performance.now() + 60_000;
    const vm = await bootLinux(booted, undefined, deadline);
    const outcome = await ControlLink.open(vm.control, booted.user, deadline, vm.exited).then(() => "it said hello", (error: Error) => error.message);
    await vm.kill();
    expect(outcome).toContain("The VM exited");
    expect(readFileSync(options.console, "utf8")).toContain("surogate: surogate.uid=0 is not a uid a session may run as");
  });
});
