import { spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

const vm = (name: string) => readFileSync(new URL(`../vm/${name}`, import.meta.url), "utf8");

// The guest's init stops the boot unless every program of the rule attached, so it must count them all.
it("expects at boot as many attached hooks as the guest rule has lsm programs", () => {
  const programs = vm("rule.bpf.c").match(/^SEC\("lsm\//gm) ?? [];
  expect(Number(/^want=(\d+)/m.exec(vm("init"))?.[1])).toBe(programs.length);
});

// The rule's scope as init reads it from the kernel's command line: its function, run alone.
const scopeOf = (cmdline: string) => {
  const fn = /^scope_uid\(\) \{\n[\s\S]*?\n\}\n/m.exec(vm("init"))?.[0];
  const ran = spawnSync("/bin/sh", ["-euc", `${fn}\nscope_uid "$1"`, "sh", cmdline], { encoding: "utf8" });
  return ran.status === 0 ? { uid: ran.stdout.trim() } : { refused: ran.stdout.trim() };
};

it("judges the one uid the kernel's command line names as surogate.uid=, besides those from 10000 up, and none where it names none", () => {
  const linux = "root=/dev/vda ro console=hvc0 panic=-1 lsm=landlock,lockdown,yama,integrity,apparmor,bpf";
  expect(scopeOf(linux)).toEqual({ uid: "" });
  expect(scopeOf(`${linux} surogate.uid=501`)).toEqual({ uid: "501" });
  expect(scopeOf(`surogate.uid=1 ${linux}`)).toEqual({ uid: "1" });
  expect(scopeOf(`${linux} surogate.uid=4294967294`)).toEqual({ uid: "4294967294" });
  // A word that merely holds it is not it, and a glob is no file.
  expect(scopeOf(`${linux} xsurogate.uid=0 * /*`)).toEqual({ uid: "" });
});

it("refuses a scope no session may run as, and a second one, which fails the boot", () => {
  for (const value of ["0", "00", "0501", "-1", "501x", "", " ", "4294967295", "99999999999", "5e2", "+501"]) {
    expect(scopeOf(`console=hvc0 surogate.uid=${value}`), value).toEqual({ refused: `surogate.uid=${value.trim()} is not a uid a session may run as` });
  }
  expect(scopeOf("surogate.uid=501 surogate.uid=501")).toEqual({ refused: "the kernel's command line names surogate.uid= twice" });
  // The outcome the agent reads: a refused scope is a failed rule, and the scope it judges is what the agent runs sessions as.
  expect(vm("init")).toContain('elif ! scope=$(scope_uid "$(cat /proc/cmdline)"); then\n    echo "failed: $scope"');
  expect(vm("init")).toMatch(/map freeze pinned \/sys\/fs\/bpf\/surogate-maps\/scope && echo "\$scope" > \/run\/surogate\/scope/);
});

// A function of init's, *name*, as the file *text* holds it: from its line to the first line that closes it.
const fnOf = (text: string, name: string) => new RegExp(`^${name}\\(\\) \\{\\n[\\s\\S]*?\\n\\}\\n`, "m").exec(text)?.[0] ?? "";
const STUB = readFileSync(new URL("../../images/guest/surogate-init", import.meta.url), "utf8");

describe("the guest's disks, found by the bus the kernel's line names", () => {
  let sys: string;
  beforeEach(() => {
    sys = mkdtempSync(join(tmpdir(), "vm-init-sys-"));
  });
  afterEach(() => {
    rmSync(sys, { recursive: true, force: true });
  });
  // A SCSI disk at *address* (host:channel:target:lun), the block device *name*, as sysfs shows it.
  const scsi = (address: string, name: string) => mkdirSync(join(sys, "class", "scsi_disk", address, "device", "block", name), { recursive: true });
  // The disk of *role* that *text*'s disk() finds on *cmdline*, run alone.
  const diskOf = (cmdline: string, role: number, text = vm("init")) => {
    const ran = spawnSync("/bin/sh", ["-euc", `${fnOf(text, "disk")}\ndisk "$1" "$2" "$3"`, "sh", cmdline, String(role), sys], { encoding: "utf8" });
    return ran.status === 0 ? { disk: ran.stdout.trim() } : { refused: ran.stdout.trim() };
  };

  it("is one function in the image's stub init and in the agent disk's init", () => {
    expect(fnOf(vm("init"), "disk")).not.toBe("");
    expect(fnOf(STUB, "disk")).toBe(fnOf(vm("init"), "disk"));
    // Each init takes its own disk by it: the stub the agent disk, the agent disk's init the sessions disk.
    expect(STUB).toContain('agent=$(disk "$(cat /proc/cmdline)" 1 /sys) || { echo "surogate: $agent, so the guest stops" >&2; exit 1; }');
    expect(vm("init")).toContain('sessions=$(disk "$(cat /proc/cmdline)" 2 /sys) || { echo "surogate: $sessions, so the guest stops" >&2; exit 1; }');
    expect(`${STUB}${vm("init")}`).not.toMatch(/\/dev\/vd[bc](?! ;;)/);
  });

  it("takes virtio's disks by their names, in the disks' order", () => {
    const line = "root=/dev/vda ro console=hvc0 surogate.disks=virtio surogate.transport=virtio-serial";
    expect([diskOf(line, 1), diskOf(line, 2)]).toEqual([{ disk: "/dev/vdb" }, { disk: "/dev/vdc" }]);
  });

  it("takes SCSI's disks by their LUNs, whatever the kernel named them", () => {
    scsi("0:0:0:0", "sdb");
    scsi("0:0:0:1", "sdc");
    scsi("0:0:0:2", "sda");
    const line = "console=ttyS0 surogate.disks=scsi surogate.transport=vsock";
    expect([diskOf(line, 1), diskOf(line, 2)]).toEqual([{ disk: "/dev/sdc" }, { disk: "/dev/sda" }]);
    expect(diskOf(line, 1, STUB)).toEqual({ disk: "/dev/sdc" });
  });

  it("refuses a LUN that no disk or two disks are at, and one with no block device", () => {
    const line = "surogate.disks=scsi";
    scsi("0:0:0:1", "sdb");
    expect(diskOf(line, 2)).toEqual({ refused: "no SCSI disk is at LUN 2" });
    // Another LUN that ends as this one's does is not it.
    scsi("0:0:0:12", "sdd");
    expect(diskOf(line, 2)).toEqual({ refused: "no SCSI disk is at LUN 2" });
    scsi("1:0:0:1", "sdc");
    expect(diskOf(line, 1)).toEqual({ refused: "two SCSI disks are at LUN 1" });
    mkdirSync(join(sys, "class", "scsi_disk", "0:0:0:2", "device", "block"), { recursive: true });
    expect(diskOf(line, 2)).toEqual({ refused: "the SCSI disk at LUN 2 has no block device" });
  });

  it("refuses a line that names no bus, two, or one it does not know, which fails the boot", () => {
    expect(diskOf("root=/dev/vda ro console=hvc0", 1)).toEqual({ refused: "the kernel's command line names no surogate.disks=" });
    expect(diskOf("surogate.disks=virtio surogate.disks=virtio", 1)).toEqual({ refused: "the kernel's command line names surogate.disks= twice" });
    expect(diskOf("surogate.disks=nvme", 1)).toEqual({ refused: "surogate.disks=nvme is not a bus this guest knows" });
    // A word that merely holds it is not it, and a glob is no file.
    expect(diskOf("xsurogate.disks=virtio * /*", 1)).toEqual({ refused: "the kernel's command line names no surogate.disks=" });
  });
});
