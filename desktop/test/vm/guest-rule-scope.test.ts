// The protected-names rule (vm/rule.bpf.c) as the guest kernel runs it, for whom and what it judges: the
// names a volume that compares names without case takes as protected ones, folded (rule-match.h,
// sg_fold), which the drift test checks against protect.ts on the host; and its scope, the sessions'
// uids from 10000 up and none below, on every OS. With it, the owner a root's folder shows when asked
// as the root's own uid, as a root's setup on Apple's share asks it (guest/root.ts, shownTo and
// checkOwner), here of Linux's shares, whose server maps the host user to each root's uid. Under QEMU
// and KVM, behind SUROGATE_VM_TESTS=1; SUROGATE_VM_IMAGE names another image folder.

import { chmodSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { bootLinux } from "../../src/vm/linux.js";
import { Guest, type VmOptions } from "../../src/vm/manager.js";
import { agentDiskWith, altered, ARCH, folderOf, IMAGE, KVM, needsKvm, OTHER, ROOT, signal, until, USER } from "./guest-support.js";

beforeAll(needsKvm);

const SHARE = "/run/surogate/shares/r1";
const KEY = "0123456789abcdef";
// As the guest's root, once a root's share is mounted: a write to a protected file and to another, by a session's
// uid, by the uid just below the sessions', and by root, through a mount of the share's that each can reach (the
// agent's is root's alone); then whether the rule has a map that could add a uid to its scope. On the console, which
// init's own lines go to.
const PROBE = `(
  until mountpoint -q ${SHARE}; do sleep 0.1; done
  mkdir /run/probe && mount --bind ${SHARE} /run/probe
  for uid in 10000 9999 0; do
    for file in .git/hooks/pre-commit notes.txt; do
      if setpriv --reuid="$uid" --regid="$uid" --clear-groups sh -c "echo x >> /run/probe/$file" 2>/dev/null; then said=written; else said=refused; fi
      echo "scope: $uid $file $said"
    done
  done
  echo "scope: maps named scope $(/usr/sbin/bpftool map show | grep -c ' name scope ' || true)"
  echo "scope: done"
) &
`;
// As the guest's root, once two roots' shares and a place are mounted: the agent's own look at a folder's owner as a
// uid, of the first root's share as its uid and as the second root's, of the second's as its own, and of the place's
// history, which is the guest's root's, as the first root's uid; and the agent's verdict on each.
const ASKS = [[SHARE, 10000], [SHARE, 10001], ["/run/surogate/shares/r2", 10001], [`/run/surogate/places/${KEY}/history`, 10000]];
const OWNERS = `(
  until mountpoint -q ${SHARE} && mountpoint -q /run/surogate/shares/r2 && mountpoint -q /run/surogate/places/${KEY}/history; do sleep 0.1; done
  /usr/bin/node --input-type=module -e '
    const { checkOwner, shownTo } = await import("/run/surogate/agent/guest/root.js");
    for (const [path, uid] of ${JSON.stringify(ASKS)}) {
      const shown = await shownTo(path, uid, 5000);
      let verdict = "taken";
      try { checkOwner(shown ?? -1, uid); } catch (error) { verdict = error.message; }
      console.error(\`owner: \${uid} \${shown} \${verdict}\`);
    }
    console.error("owner: done");
  '
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

  it("judges sessions from uid 10000 up, and no uid below them, the guest's root among them, with no map to add one", async () => {
    rmSync(options.console, { force: true });
    const guest = await Guest.boot(bootLinux, { ...options, agentDisk: agentDiskWith(dir, altered([/^exec /m, `${PROBE}exec `])) });
    try {
      expect(await guest.ready(ROOT, folderOf(folder))).toBeNull();
      await until(() => readFileSync(options.console, "utf8").includes("scope: done"), 30_000);
      const said = readFileSync(options.console, "utf8").split("\n").filter((line) => line.startsWith("scope: ")).map((line) => line.slice(7).trim());
      expect(said).toEqual([
        "10000 .git/hooks/pre-commit refused", "10000 notes.txt written", "9999 .git/hooks/pre-commit written", "9999 notes.txt written",
        "0 .git/hooks/pre-commit written", "0 notes.txt written", "maps named scope 0", "done",
      ]);
      expect([readFileSync(join(folder, ".git", "hooks", "pre-commit"), "utf8"), readFileSync(join(folder, "notes.txt"), "utf8")]).toEqual(["x\nx\n", "x\nx\nx\n"]);
    } finally {
      await guest.stop();
    }
  });

  it("takes a root's folder that shows its own uid when asked as it, and refuses one that shows another user's or root's", async () => {
    rmSync(options.console, { force: true });
    const other = join(dir, "other");
    const history = join(dir, "history");
    const real = join(dir, "real");
    for (const made of [other, history, real]) mkdirSync(made, { recursive: true });
    const guest = await Guest.boot(bootLinux, { ...options, agentDisk: agentDiskWith(dir, altered([/^exec /m, `${OWNERS}exec `])) });
    try {
      expect(await guest.ready(ROOT, folderOf(folder))).toBeNull();
      expect(await guest.ready(OTHER, folderOf(other))).toBeNull();
      expect(await guest.place({ key: KEY, history, real: folderOf(real) })).toBeNull();
      await until(() => readFileSync(options.console, "utf8").includes("owner: done"), 30_000);
      const said = readFileSync(options.console, "utf8").split("\n").filter((line) => line.startsWith("owner: ")).map((line) => line.slice(7).trim());
      expect(said).toEqual([
        "10000 10000 taken",
        "10001 10000 This chat's folder shows as another user's in the sandbox, so no command runs in it",
        "10001 10001 taken",
        "10000 0 This chat's folder shows as root's in the sandbox, so no command runs in it",
        "done",
      ]);
    } finally {
      await guest.stop();
    }
  });
});
