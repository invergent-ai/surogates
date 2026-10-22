// The stand-in for Hyper-V's devices (test/vm/hyperv/standin.ts), for the tests alone: QEMU's command line for it,
// the initrd it boots, the agent disk it gives the guest, and that nothing of the app reaches it.

import { execFileSync, spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { PLAN9_PORT } from "../src/guest/protocol.js";
import { HYPERV_LINE, STANDIN_MODULES, STANDIN_PLAN9_PORT, standinAgentDisk, standinArgs, standinInitrd } from "./vm/hyperv/standin.js";

const DESKTOP = fileURLToPath(new URL("..", import.meta.url));
let dir: string;
beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "standin-"));
});
afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

describe("the stand-in for Hyper-V's devices", () => {
  it("boots the guest as Hyper-V would: SCSI's disks at LUNs 0 to 2, vsock, the UART, the initrd, Hyper-V's line, and no virtio port or folder device", () => {
    const args = standinArgs({ kernel: "/i/vmlinuz", rootfs: "/i/rootfs.img", agentDisk: "/a/agent.img", sessions: "/d/sessions.img" }, "/r/initrd", "/r", "/d/console.log", 1234, 2);
    expect(args).toEqual([
      "-nodefaults", "-no-user-config", "-display", "none", "-no-reboot",
      "-machine", "q35,accel=kvm,memory-backend=mem", "-cpu", "host", "-smp", "2", "-m", "2G",
      "-object", "memory-backend-memfd,id=mem,size=2G,share=on",
      "-kernel", "/i/vmlinuz", "-initrd", "/r/initrd", "-append", HYPERV_LINE,
      "-device", "virtio-scsi-pci,id=scsi",
      "-drive", "if=none,id=root,file=/i/rootfs.img,format=raw,readonly=on", "-device", "scsi-hd,drive=root,bus=scsi.0,channel=0,scsi-id=0,lun=0",
      "-drive", "if=none,id=agent,file=/a/agent.img,format=raw,readonly=on", "-device", "scsi-hd,drive=agent,bus=scsi.0,channel=0,scsi-id=0,lun=1",
      "-drive", "if=none,id=sessions,file=/d/sessions.img,format=raw,discard=unmap", "-device", "scsi-hd,drive=sessions,bus=scsi.0,channel=0,scsi-id=0,lun=2",
      "-device", "vhost-vsock-pci,guest-cid=1234",
      "-chardev", "file,id=console,path=/d/console.log", "-serial", "chardev:console",
      "-device", "virtio-rng-pci", "-nic", "none",
      "-device", "virtio-balloon-pci,free-page-reporting=on",
      "-pidfile", "/r/qemu.pid",
      "-sandbox", "on,obsolete=deny,elevateprivileges=deny,spawn=deny,resourcecontrol=deny",
    ]);
    // Emulated, as Linux's backend runs a guest without KVM: QEMU's TCG, and the agent told so.
    const emulated = standinArgs({ kernel: "/i/vmlinuz", rootfs: "/i/rootfs.img", agentDisk: "/a/agent.img", sessions: "/d/sessions.img" }, "/r/initrd", "/r", "/d/console.log", 1234, 2, true);
    expect(emulated.slice(5, 9)).toEqual(["-machine", "q35,memory-backend=mem", "-accel", "tcg,thread=multi,tb-size=256"]);
    expect(emulated[emulated.indexOf("-append") + 1]).toBe(`${HYPERV_LINE} surogate.emulated=1`);
    expect(HYPERV_LINE).toBe("console=ttyS0 panic=-1 quiet surogate.disks=scsi surogate.transport=vsock surogate.names=windows lsm=landlock,lockdown,yama,integrity,apparmor,bpf");
  });

  it("boots the image's initrd with a second archive over it, whose list loads QEMU's vsock device's modules in Hyper-V's sockets' place", () => {
    const image = join(dir, "image");
    mkdirSync(join(image, "standin"), { recursive: true });
    // The image's initrd, as initrd.sh makes it: its init and its list.
    mkdirSync(join(dir, "first"));
    writeFileSync(join(dir, "first", "init"), "#!/bin/busybox sh\n");
    writeFileSync(join(dir, "first", "modules"), "hv_vmbus\nhv_sock\n");
    writeFileSync(join(image, "initrd"), execFileSync("cpio", ["-o", "-H", "newc", "--quiet"], { cwd: join(dir, "first"), input: "init\nmodules\n" }));
    for (const name of STANDIN_MODULES) writeFileSync(join(image, "standin", `${name}.ko.zst`), spawnSync("zstd", ["-q", "-c"], { input: `module ${name}` }).stdout);
    const run = join(dir, "run");
    mkdirSync(run);
    const initrd = standinInitrd(image, run);
    // Unpacked as the kernel unpacks it, each archive in turn, the later's files over the earlier's.
    const root = join(dir, "root");
    mkdirSync(root);
    const archive = readFileSync(initrd);
    const second = archive.indexOf(Buffer.from("070701"), archive.indexOf("TRAILER!!!"));
    for (const part of [archive.subarray(0, second), archive.subarray(second)]) {
      expect(spawnSync("cpio", ["-i", "-d", "-u", "--quiet"], { cwd: root, input: part }).status).toBe(0);
    }
    expect(readFileSync(join(root, "modules"), "utf8")).toBe(`${STANDIN_MODULES.join("\n")}\n`);
    expect(readFileSync(join(root, "init"), "utf8")).toBe("#!/bin/busybox sh\n");
    expect(readdirSync(join(root, "lib", "modules")).sort()).toEqual(STANDIN_MODULES.map((name) => `${name}.ko`).sort());
    expect(readFileSync(join(root, "lib", "modules", "vsock.ko"), "utf8")).toBe("module vsock");
    expect(readdirSync(run)).toEqual(["initrd"]);
    rmSync(join(image, "standin", "vsock.ko.zst"));
    expect(() => standinInitrd(image, run)).toThrow(`run test/vm/hyperv/standin.sh ${image}`);
  });

  it("gives the guest its agent disk with one number changed: the Plan 9 server's port, which an unprivileged host binds", () => {
    expect(STANDIN_PLAN9_PORT).toBeGreaterThan(1023);
    expect(PLAN9_PORT).toBe(564);
    const disk = join(dir, "agent.img");
    const made = spawnSync(join(DESKTOP, "vm", "agent-disk.sh"), [disk], { encoding: "utf8" });
    expect(made.status, made.stderr).toBe(0);
    const run = join(dir, "run");
    mkdirSync(run);
    const copy = standinAgentDisk(disk, run);
    const read = (image: string) => spawnSync("debugfs", [image, "-R", "cat /guest/protocol.js"], { encoding: "utf8" }).stdout;
    expect(read(copy)).toBe(read(disk).replace(`export const PLAN9_PORT = ${PLAN9_PORT};`, `export const PLAN9_PORT = ${STANDIN_PLAN9_PORT};`));
    expect(read(copy)).not.toBe(read(disk));
  });
});
