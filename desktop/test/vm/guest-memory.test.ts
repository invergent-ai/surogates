// The guest under QEMU and KVM, booted by the VM manager: the image built by
// images/guest/build.sh, the agent disk built from this package (npm run build
// first). Behind SUROGATE_VM_TESTS=1; SUROGATE_VM_IMAGE names another image folder.

import { mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { bootLinux } from "../../src/vm/linux.js";
import { Guest, type VmOptions } from "../../src/vm/manager.js";
import { agentDisk, folderOf, IMAGE, KVM, needsKvm, ROOT, signal, until, USER } from "./guest-support.js";

beforeAll(needsKvm);

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
      run: mkdtempSync(join(process.env.XDG_RUNTIME_DIR ?? "/tmp", "sg-vm-")), console: join(dir, "console.log"), user: USER, kvm: KVM,
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
