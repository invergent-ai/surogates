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
    expect(STUB).toContain('agent=$(disk "$(cat /proc/cmdline)" 1 /sys) || stop "$agent, so the guest stops"');
    expect(vm("init")).toContain('sessions=$(disk "$(cat /proc/cmdline)" 2 /sys) || stop "$sessions, so the guest stops"');
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

describe("the words of the kernel's line the guest's init reads", () => {
  // The value init's named() reads of surogate.<name>= on *cmdline*, run alone.
  const namedOf = (cmdline: string, name: string) => {
    const ran = spawnSync("/bin/sh", ["-euc", `${fnOf(vm("init"), "named")}\nnamed "$1" "$2"`, "sh", cmdline, name], { encoding: "utf8" });
    return ran.status === 0 ? { value: ran.stdout.trim() } : { refused: ran.stdout.trim() };
  };

  it("reads the one word of a name, and refuses none, or two", () => {
    expect(namedOf("console=hvc0 surogate.transport=vsock panic=-1", "transport")).toEqual({ value: "vsock" });
    expect(namedOf("console=hvc0", "transport")).toEqual({ refused: "the kernel's command line names no surogate.transport=" });
    expect(namedOf("surogate.transport=vsock surogate.transport=vsock", "transport")).toEqual({ refused: "the kernel's command line names surogate.transport= twice" });
    expect(namedOf("xsurogate.transport=vsock * /*", "transport")).toEqual({ refused: "the kernel's command line names no surogate.transport=" });
  });

  it("stops the boot on a transport it does not know, and loads the folders' filesystem the transport's shares need", () => {
    expect(vm("init")).toContain('transport=$(named "$(cat /proc/cmdline)" transport) || stop "$transport, so the guest stops"');
    expect(vm("init")).toMatch(/case "\$transport" in\n {2}virtio-serial \| vsock\) ;;\n {2}\*\) stop "surogate\.transport=\$transport is not a transport this guest knows, so the guest stops" ;;\nesac/);
    expect(vm("init")).toContain('if [ "$transport" = vsock ]; then\n  modprobe 9p\n  modprobe 9pnet_fd\nelse\n  modprobe virtiofs\nfi\nmodprobe overlay\n');
  });
});

describe("the rule's names, by the set the kernel's line names", () => {
  // The value init's name_set() prints for *cmdline*, with named() beside it, run alone.
  const setOf = (cmdline: string) => {
    const ran = spawnSync("/bin/sh", ["-euc", `${fnOf(vm("init"), "named")}${fnOf(vm("init"), "name_set")}\nname_set "$1"`, "sh", cmdline], { encoding: "utf8" });
    return ran.status === 0 ? { set: ran.stdout.trim() } : { refused: ran.stdout.trim() };
  };

  it("takes posix's, which adds none, and windows's, the names Explorer acts on, as the map takes each", () => {
    expect(setOf("root=/dev/vda console=hvc0 surogate.names=posix")).toEqual({ set: "0" });
    expect(setOf("console=ttyS0 surogate.names=windows")).toEqual({ set: "1" });
  });

  it("refuses a line that names neither, or both, or another, which fails the rule's load and the boot", () => {
    expect(setOf("root=/dev/vda console=hvc0")).toEqual({ refused: "the kernel's command line names no surogate.names=" });
    expect(setOf("surogate.names=posix surogate.names=windows")).toEqual({ refused: "the kernel's command line names surogate.names= twice" });
    expect(setOf("surogate.names=macos")).toEqual({ refused: "surogate.names=macos is not a set of names this guest knows" });
    expect(vm("init")).toContain('elif ! names=$(name_set "$(cat /proc/cmdline)"); then\n    echo "failed: $names"');
    // Set and frozen before the rule is said to be attached, as the scope is.
    expect(vm("init")).toContain('/usr/sbin/bpftool map update pinned /sys/fs/bpf/surogate-maps/names key 0 0 0 0 value "$names" 0 0 0 \\\n'
      + '      && /usr/sbin/bpftool map freeze pinned /sys/fs/bpf/surogate-maps/names; } >&2; then\n    echo "failed: could not set the protected-names rule\'s names"');
    expect(vm("rule.bpf.c")).toMatch(/^#define NAMES_POSIX 0\n#define NAMES_WINDOWS 1\n/m);
  });
});

describe("the words that stop the guest's boot", () => {
  // What *text*'s stop() says, run alone where no /dev/kmsg takes it from this user: its words on stderr, and its exit.
  const stopped = (text: string) => spawnSync("/bin/sh", ["-uc", `${fnOf(text, "stop")}\nstop "the disk did not come, so the guest stops"`], { encoding: "utf8" });

  it("goes through the kernel's log, which a UART's console prints before the panic, and else on stderr, then ends the boot", () => {
    for (const text of [vm("init"), STUB]) {
      expect(fnOf(text, "stop")).toContain('{ echo "<2>surogate: $*" > /dev/kmsg; } 2> /dev/null || echo "surogate: $*" >&2');
      if (spawnSync("/bin/sh", ["-c", "true > /dev/kmsg"]).status === 0) continue;
      expect(stopped(text)).toMatchObject({ status: 1, stdout: "", stderr: "surogate: the disk did not come, so the guest stops\n" });
    }
    // Every word of a stop is a stop's: no other way out of either init says why on the console itself.
    for (const text of [vm("init"), STUB]) expect(text).not.toMatch(/echo "surogate: [^"]*" >&2; exit 1/);
    expect(vm("init")).toContain('stop "e2fsck could not check the sessions disk (exit code $checked), so the guest stops; the app keeps the disk aside and makes a new one"');
    const initrd = readFileSync(new URL("../../images/guest/initrd/init", import.meta.url), "utf8");
    expect(fnOf(initrd, "fail")).toContain('{ echo "<2>surogate: $*, so the guest stops" > /dev/kmsg; } 2> /dev/null || echo "surogate: $*, so the guest stops"');
  });
});
