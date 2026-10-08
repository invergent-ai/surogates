import { mkdtempSync, rmSync } from "node:fs";
import { connect, createServer, type Server, type Socket } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createInterface } from "node:readline";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { guestCpus, Qmp, qemuArgs, virtiofsdArgs } from "../src/vm/qemu.js";

const DISKS = { kernel: "/i/vmlinuz", rootfs: "/i/rootfs.img", agentDisk: "/a/agent.img", sessions: "/d/sessions.img" };

describe("QEMU's command line", () => {
  it("is Section 11's, with eight empty root ports for shares, the net port, no network device, and the guest's free pages reported", () => {
    const ports = Array.from({ length: 8 }, (_, n) => ["-device", `pcie-root-port,id=rp${n + 1},chassis=${n + 1}`]).flat();
    expect(qemuArgs(DISKS, "/run/user/1000/surogate/vm", "/d/logs/vm-console.log", 4)).toEqual([
      "-nodefaults", "-no-user-config", "-display", "none", "-no-reboot",
      "-machine", "q35,accel=kvm,memory-backend=mem", "-cpu", "host", "-smp", "4", "-m", "2G",
      "-object", "memory-backend-memfd,id=mem,size=2G,share=on",
      "-kernel", "/i/vmlinuz",
      "-append", "root=/dev/vda rootfstype=ext4 ro init=/usr/sbin/surogate-init console=hvc0 panic=-1 quiet lsm=landlock,lockdown,yama,integrity,apparmor,bpf",
      "-drive", "if=none,id=root,file=/i/rootfs.img,format=raw,readonly=on", "-device", "virtio-blk-pci,drive=root",
      "-drive", "if=none,id=agent,file=/a/agent.img,format=raw,readonly=on", "-device", "virtio-blk-pci,drive=agent",
      "-drive", "if=none,id=sessions,file=/d/sessions.img,format=raw,discard=unmap", "-device", "virtio-blk-pci,drive=sessions",
      "-device", "virtio-serial-pci",
      "-chardev", "socket,id=control,path=/run/user/1000/surogate/vm/control.sock,server=on,wait=off",
      "-device", "virtserialport,chardev=control,name=ai.surogate.control",
      "-chardev", "socket,id=net,path=/run/user/1000/surogate/vm/net.sock,server=on,wait=off",
      "-device", "virtserialport,chardev=net,name=ai.surogate.net",
      "-chardev", "file,id=console,path=/d/logs/vm-console.log", "-device", "virtconsole,chardev=console",
      ...ports,
      "-device", "virtio-rng-pci", "-nic", "none",
      "-device", "virtio-balloon-pci,free-page-reporting=on",
      "-qmp", "unix:/run/user/1000/surogate/vm/qmp.sock,server=on,wait=off",
      "-pidfile", "/run/user/1000/surogate/vm/qemu.pid",
      "-sandbox", "on,obsolete=deny,elevateprivileges=deny,spawn=deny,resourcecontrol=deny",
    ]);
  });

  it("doubles a comma in a path, which QEMU would read as the next option", () => {
    const args = qemuArgs({ ...DISKS, rootfs: "/home/a,b/rootfs.img", sessions: "/d,x/sessions.img" }, "/run/a,b", "/l,c/console.log", 2);
    expect(args).toContain("if=none,id=root,file=/home/a,,b/rootfs.img,format=raw,readonly=on");
    expect(args).toContain("if=none,id=sessions,file=/d,,x/sessions.img,format=raw,discard=unmap");
    expect(args).toContain("socket,id=control,path=/run/a,,b/control.sock,server=on,wait=off");
    expect(args).toContain("socket,id=net,path=/run/a,,b/net.sock,server=on,wait=off");
    expect(args).toContain("file,id=console,path=/l,,c/console.log");
    expect(args).toContain("unix:/run/a,,b/qmp.sock,server=on,wait=off");
    // Not an option list: taken whole.
    expect(args).toContain("/run/a,b/qemu.pid");
  });

  it("gives the guest half this computer's threads, at most four", () => {
    expect([1, 2, 3, 8, 20].map((threads) => guestCpus(threads))).toEqual([1, 1, 1, 4, 4]);
  });

  it("maps the host user to the root's guest uid in its folder's virtiofsd, which the guest does not cache", () => {
    expect(virtiofsdArgs("/home/ana/My folder's", "/run/vm/vfs-1.sock", 10_001, { uid: 1000, gid: 1001 })).toEqual([
      "--shared-dir=/home/ana/My folder's", "--socket-path=/run/vm/vfs-1.sock", "--sandbox=namespace", "--cache=never",
      "--uid-map=:10001:1000:1:", "--gid-map=:10001:1001:1:",
    ]);
  });
});

describe("QEMU's machine protocol", () => {
  let dir: string;
  let server: Server;
  let heard: string[];
  // How the fake QEMU answers each command it is sent, after its greeting.
  let answer: (command: Record<string, unknown>, socket: Socket) => void;
  // What it does when the monitor connects: greet, close at once, or say nothing.
  let greeting: "greet" | "close" | "silent";

  beforeEach(async () => {
    dir = mkdtempSync(join(tmpdir(), "qmp-"));
    heard = [];
    greeting = "greet";
    answer = (_command, socket) => socket.write('{"return": {}}\n');
    server = createServer((socket) => {
      if (greeting === "close") return void socket.end();
      if (greeting === "silent") return;
      socket.write('{"QMP": {"version": {"qemu": {"major": 8, "minor": 2, "micro": 2}}, "capabilities": ["oob"]}}\n');
      createInterface({ input: socket }).on("line", (line) => {
        heard.push(line);
        answer(JSON.parse(line) as Record<string, unknown>, socket);
      });
    });
    await new Promise<void>((resolve) => server.listen(join(dir, "qmp.sock"), resolve));
  });

  afterEach(async () => {
    await new Promise((resolve) => server.close(resolve));
    rmSync(dir, { recursive: true, force: true });
  });

  it("negotiates capabilities, then answers each command in turn, past the events in between", async () => {
    const qmp = await Qmp.open(connect(join(dir, "qmp.sock")));
    answer = (command, socket) => {
      socket.write('{"timestamp": {"seconds": 1, "microseconds": 2}, "event": "RTC_CHANGE", "data": {}}\n');
      socket.write(command.execute === "device_add"
        ? '{"error": {"class": "GenericError", "desc": "Bus \'rp9\' not found"}}\n'
        : '{"return": {"id": "vfs1"}}\n');
    };
    expect(await qmp.execute("chardev-add", { id: "vfs1" })).toEqual({ id: "vfs1" });
    await expect(qmp.execute("device_add", { driver: "vhost-user-fs-pci", bus: "rp9" })).rejects.toThrow("Bus 'rp9' not found");
    expect(heard.map((line) => JSON.parse(line) as unknown)).toEqual([
      { execute: "qmp_capabilities" },
      { execute: "chardev-add", arguments: { id: "vfs1" } },
      { execute: "device_add", arguments: { driver: "vhost-user-fs-pci", bus: "rp9" } },
    ]);
    qmp.close();
  });

  it("tells an event's listeners its data between the answers, until they stop listening", async () => {
    const qmp = await Qmp.open(connect(join(dir, "qmp.sock")));
    answer = (command, socket) => {
      const id = (command.arguments as { id: string }).id;
      socket.write('{"return": {}}\n');
      socket.write(`${JSON.stringify({ event: "DEVICE_DELETED", data: { device: id, path: `/machine/peripheral/${id}` }, timestamp: { seconds: 1, microseconds: 2 } })}\n`);
    };
    const heard: unknown[] = [];
    try {
      const stop = qmp.on("DEVICE_DELETED", (data) => heard.push(data));
      await qmp.execute("device_del", { id: "fs1" });
      await qmp.execute("query-status", { id: "x" });
      stop();
      await qmp.execute("device_del", { id: "fs2" });
      expect(heard).toEqual([{ device: "fs1", path: "/machine/peripheral/fs1" }, { device: "x", path: "/machine/peripheral/x" }]);
    } finally {
      qmp.close();
    }
  });

  it("fails to open a monitor QEMU closes before its greeting, or that never greets by the deadline", async () => {
    greeting = "close";
    await expect(Qmp.open(connect(join(dir, "qmp.sock")))).rejects.toThrow("QEMU's monitor closed");
    greeting = "silent";
    await expect(Qmp.open(connect(join(dir, "qmp.sock")), performance.now() + 200)).rejects.toThrow("QEMU's monitor did not answer");
  });

  it("fails to open a monitor whose connection QEMU resets, as when it exits with what it was sent unread, or that never connects", async () => {
    const resetting = createServer({ pauseOnConnect: true }, (socket) => {
      socket.write('{"QMP": {"version": {}, "capabilities": ["oob"]}}\n');
      // qmp_capabilities is still unread: the kernel resets the connection.
      setTimeout(() => socket.destroy(), 50);
    });
    const path = join(dir, "reset.sock");
    await new Promise<void>((resolve) => resetting.listen(path, resolve));
    await expect(Qmp.open(connect(path))).rejects.toThrow("QEMU's monitor closed");
    await expect(Qmp.open(connect(join(dir, "missing.sock")))).rejects.toThrow("QEMU's monitor closed");
    await new Promise((resolve) => resetting.close(resolve));
  });

  it("gives up on a command at its deadline, and closes the monitor, whose next answer would be taken for the next command's", async () => {
    const qmp = await Qmp.open(connect(join(dir, "qmp.sock")));
    answer = () => {};
    await expect(qmp.execute("device_add", {}, performance.now() + 200)).rejects.toThrow("QEMU's monitor did not answer");
    await expect(qmp.execute("query-status")).rejects.toThrow("QEMU's monitor closed");
  });

  it("fails what waits, and what comes after, once QEMU has gone", async () => {
    const qmp = await Qmp.open(connect(join(dir, "qmp.sock")));
    answer = (_command, socket) => socket.destroy();
    await expect(qmp.execute("device_add", {})).rejects.toThrow("QEMU's monitor closed");
    await expect(qmp.execute("device_add", {})).rejects.toThrow("QEMU's monitor closed");
  });
});
