// What the guest-kernel rule on protected names (vm/rule.bpf.c) depends on and does not make
// itself, as a session's command finds it in a guest under QEMU and KVM. The rule judges what a
// session's user does to the share's filesystem, at the hooks it attached: it holds only while a
// session has no other user to act as, no other way to a file, and no way into the kernel the rule
// runs in. Each of those is set apart from the rule, in vm/init, vm/enter-root, guest/root.ts and
// the image's build, where a change shows in none of the rule's own tests. Behind
// SUROGATE_VM_TESTS=1; SUROGATE_VM_IMAGE names another image folder.

import { mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { bootLinux } from "../../src/vm/linux.js";
import { Guest, type VmOptions } from "../../src/vm/manager.js";
import { agentDiskWith, altered, folderOf, IMAGE, KVM, needsKvm, ROOT, signal, USER } from "./guest-support.js";

// The rule's own numbers, from its source: the first user it judges, and the one filesystem it judges on.
const RULE = readFileSync(new URL("../../vm/rule.bpf.c", import.meta.url), "utf8");
const defined = (name: string) => Number(new RegExp(`^#define ${name} (0x[0-9a-fA-F]+|\\d+)`, "m").exec(RULE)?.[1]);
const FIRST_UID = defined("FIRST_UID");
const SHARE_MAGIC = defined("FUSE_SUPER_MAGIC");

// The one guarantee no session can read: the kernel shows net.core.bpf_jit_harden to root, in the
// first network namespace, and a session is neither. The guest's init says it on the console, as
// its last line before it starts the agent, and is vm/init in all else.
const SAYS_JIT = 'echo "guarantees: net.core.bpf_jit_harden is $(cat /proc/sys/net/core/bpf_jit_harden)"';

// One command of a session's: each guarantee on lines of its own, named by what they begin with.
// The image is searched whole but for the folders the session's user cannot read, so a search
// that could not run says so in what it found.
const READS = [
  'for name in kernel/io_uring_disabled user/max_user_namespaces kernel/unprivileged_bpf_disabled kernel/modules_disabled; do echo "sysctl: $name $(cat /proc/sys/$name 2>&1)"; done',
  `echo "unshare: $(unshare -U true 2>&1 | tr '\\n' ' ')"`,
  "grep -E '^(Uid|Cap(Inh|Prm|Eff|Bnd|Amb)|NoNewPrivs):' /proc/self/status | tr -s '\\t' ' ' | sed 's/^/status: /'",
  "sed 's/^/mount: /' /proc/self/mountinfo",
  `echo "set-id: $(find / -xdev -type d ! -readable -prune -o -type f -perm /6000 -print 2>&1 | head -5 | tr '\\n' ' ')"`,
  `echo "capabilities: $(find / -xdev -type d ! -readable -prune -o -type f -exec getcap {} + 2>&1 | head -5 | tr '\\n' ' ')"`,
  'echo "magic: $(stat -f -c %t . 2>&1)"',
].join("\n");

// A mount point of /proc/self/mountinfo, which writes a space as \040, and the like.
const unescaped = (path: string) => path.replace(/\\([0-7]{3})/g, (_, code: string) => String.fromCharCode(Number.parseInt(code, 8)));

beforeAll(needsKvm);

describe.skipIf(process.env.SUROGATE_VM_TESTS !== "1")("what the protected-names rule depends on", { timeout: 60_000 }, () => {
  let dir: string;
  let folder: string;
  let options: VmOptions;
  let guest: Guest;

  beforeAll(async () => {
    dir = realpathSync(mkdtempSync(join(tmpdir(), "vm-rule-")));
    // A folder name as people write them, with a space.
    folder = join(dir, "my folder");
    mkdirSync(folder);
    options = {
      kernel: join(IMAGE, "vmlinuz"), rootfs: join(IMAGE, "rootfs.img"), agentDisk: agentDiskWith(dir, altered([/^exec /m, `${SAYS_JIT}\nexec `])),
      sessions: join(dir, "sessions.img"),
      // Under $XDG_RUNTIME_DIR: a vhost-user socket's path must fit in 108 bytes.
      run: mkdtempSync(join(process.env.XDG_RUNTIME_DIR ?? "/tmp", "sg-vm-")), console: join(dir, "console.log"), user: USER, kvm: KVM,
    };
    guest = await Guest.boot(bootLinux, options);
    expect(await guest.ready(ROOT, folderOf(folder))).toBeNull();
  }, 60_000);

  afterAll(async () => {
    await guest?.stop();
    if (options) rmSync(options.run, { recursive: true, force: true });
    if (dir) rmSync(dir, { recursive: true, force: true });
  });

  it("gives a session no way round the protected-names rule: no io_uring, user namespace, BPF or kernel module, no user but its own from the rule's first up, no capability and no set-id program, in a folder on the share the rule guards", async () => {
    expect([FIRST_UID, SHARE_MAGIC].every(Number.isInteger), "vm/rule.bpf.c defines FIRST_UID and FUSE_SUPER_MAGIC, which say whose writes the rule judges, and where").toBe(true);
    const ran = await guest.op(ROOT, "run", { command: READS, workdir: null, timeout: 30 }, signal());
    expect(ran, "a session's command reads what the protected-names rule depends on, and it did not run").toMatchObject({ ok: { returncode: 0, timed_out: false } });
    const lines = (ran as { ok: { output: string } }).ok.output.split("\n");
    // What the command said on its lines that begin with *name*.
    const said = (name: string) => lines.filter((line) => line.startsWith(`${name}:`)).map((line) => line.slice(name.length + 1).trim());
    // A line's first word, and the rest of it.
    const named = (line: string, end: string): [string, string] => [line.slice(0, line.indexOf(end)), line.slice(line.indexOf(end) + 1).trim()];
    const sysctl = new Map(said("sysctl").map((line) => named(line, " ")));
    const status = new Map(said("status").map((line) => named(line, ":")));
    const mounts = said("mount").map((line) => {
      const [seen = "", filesystem = ""] = line.split(" - ");
      const fields = seen.split(" ");
      return { at: unescaped(fields[4] ?? ""), options: (fields[5] ?? "").split(","), type: filesystem.split(" ")[0] ?? "" };
    });
    const jit = /guarantees: net\.core\.bpf_jit_harden is (\S*)/.exec(readFileSync(options.console, "utf8"))?.[1];

    // Each guarantee that does not hold, with what the guest has instead and what the rule needs it for.
    const broken: string[] = [];
    const holds = (guarantee: string, kept: boolean, found: string | undefined, why: string) => {
      if (!kept) broken.push(`${guarantee}. Found: ${found || "nothing"}. The protected-names rule depends on this guarantee: ${why}.`);
    };

    // A second way to a file, and three into the kernel the rule runs in (vm/init).
    for (const [name, value, why] of [
      ["kernel/io_uring_disabled", "2", "io_uring is another way to open, write and rename a file, one the rule's hooks are not tested on"],
      ["user/max_user_namespaces", "0", "in a user namespace of its own a session holds capabilities, and the rule counts on a session with none"],
      ["kernel/unprivileged_bpf_disabled", "1", "the rule is a BPF program, and a session is to have no bpf(2), which loads and unloads one"],
      ["kernel/modules_disabled", "1", "a module is code in the kernel the rule runs in, and a session's socket of a rare family would load one"],
    ] as const) {
      holds(`A session reads ${name.replace("/", ".")} as ${value}`, sysctl.get(name) === value, sysctl.get(name), why);
    }
    const [unshare] = said("unshare");
    holds(
      "unshare -U fails for a session, for want of room for a user namespace", /No space left on device/.test(unshare ?? ""), unshare === "" ? "a user namespace made" : unshare,
      "in a user namespace of its own a session holds capabilities, and the rule counts on a session with none",
    );
    holds(
      "The guest's root reads net.core.bpf_jit_harden as 2", jit === "2", jit,
      "the kernel compiles a session's socket and seccomp filters as it does the rule, and the hardening keeps them from helping an attack on the kernel the rule runs in",
    );

    // The user the rule judges, and no way to another (vm/enter-root, guest/root.ts, the image's build).
    const uids = (status.get("Uid") ?? "").split(" ").filter(Boolean).map(Number);
    holds(
      `A session's real, effective, saved and filesystem uids are ${FIRST_UID} or above`, uids.length === 4 && uids.every((uid) => uid >= FIRST_UID), status.get("Uid"),
      `the rule judges only the users from ${FIRST_UID} up, and a write by any below goes unjudged`,
    );
    const sets = ["CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb"].map((set) => [set, status.get(set)] as const);
    holds(
      "A session has no capability, in any of its five sets", sets.every(([, held]) => /^0+$/.test(held ?? "")), sets.map(([set, held]) => `${set} ${held ?? "missing"}`).join(", "),
      "with one a session could take a user the rule does not judge, or unload the rule",
    );
    holds(
      "A session has no_new_privs set", status.get("NoNewPrivs") === "1", status.get("NoNewPrivs"),
      "without it a set-id program, or a file's capabilities, would give a session another user or a capability",
    );
    // enter-root mounts a session's terminals without nosuid: devpts holds no file a session could run.
    const suid = mounts.filter((mount) => mount.type !== "devpts" && !mount.options.includes("nosuid")).map((mount) => mount.at);
    holds(
      "Every mount a session sees is nosuid, but the one of its terminals, which holds no file", mounts.length > 0 && suid.length === 0,
      mounts.length === 0 ? "no mount read" : `no nosuid on ${suid.join(", ")}`,
      "on a mount without it a set-id program, the image's or one in the user's folder, would give a session another user",
    );
    const [setId] = said("set-id");
    holds(
      "The image holds no set-id program", setId === "", setId ?? "no answer",
      "one would give a session another user, were a mount or no_new_privs ever to let it",
    );
    const [capabilities] = said("capabilities");
    holds(
      "The image holds no file with capabilities", capabilities === "", capabilities ?? "no answer",
      "one would give a session a capability, were a mount or no_new_privs ever to let it",
    );

    // Where the rule judges: the folder, on the share (guest/root.ts, vm/enter-root).
    const share = mounts.findLast((mount) => mount.at === folder);
    const [magic] = said("magic");
    holds(
      `A session's folder is a virtiofs mount, of the filesystem the rule judges on (0x${SHARE_MAGIC.toString(16)})`,
      share?.type === "virtiofs" && Number.parseInt(magic ?? "", 16) === SHARE_MAGIC, `a mount of ${share?.type ?? "none"} there, and the filesystem 0x${magic}`,
      "the rule judges writes to that filesystem alone, and a folder on any other is unguarded",
    );

    // Each on a line of its own in the failure.
    expect(broken.join("\n"), "the guest no longer keeps what the protected-names rule depends on").toBe("");
  });
});
