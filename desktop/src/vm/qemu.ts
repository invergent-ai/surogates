// QEMU's and virtiofsd's command lines for the guest (spec, Section 11,
// Lifecycle and Folders), and a client for QEMU's machine protocol (QMP), through
// which the Linux backend (linux.ts) adds a root's folder to the running guest.

import type { Socket } from "node:net";
import { availableParallelism } from "node:os";
import { join } from "node:path";
import { createInterface } from "node:readline";

// Ubuntu's, whose AppArmor profile lets its namespace sandbox work under 24.04's restriction.
export const VIRTIOFSD = "/usr/libexec/virtiofsd";
// The guest's empty PCIe slots: one for each folder added while it runs.
export const ROOT_PORTS = 8;
const MEMORY = "2G";

export interface Disks {
  kernel: string; // vmlinuz
  rootfs: string; // the image, read-only
  agentDisk: string; // agent.img, read-only
  sessions: string; // sessions.img: each root's home and temp folder
}

// QEMU reads a comma as the start of the next option: a path's own is doubled.
const option = (path: string) => path.replaceAll(",", ",,");

// Half this computer's threads, at most 4.
export function guestCpus(threads = availableParallelism()): number {
  return Math.min(4, Math.max(1, Math.floor(threads / 2)));
}

// *run* holds the control and QMP sockets and QEMU's pidfile; *console* is the guest's console log.
export function qemuArgs(disks: Disks, run: string, console: string, cpus = guestCpus()): string[] {
  const ports: string[] = [];
  for (let n = 1; n <= ROOT_PORTS; n += 1) ports.push("-device", `pcie-root-port,id=rp${n},chassis=${n}`);
  return [
    "-nodefaults", "-no-user-config", "-display", "none", "-no-reboot",
    "-machine", "q35,accel=kvm,memory-backend=mem", "-cpu", "host", "-smp", String(cpus), "-m", MEMORY,
    // vhost-user-fs needs the guest's memory shared with virtiofsd.
    "-object", `memory-backend-memfd,id=mem,size=${MEMORY},share=on`,
    "-kernel", disks.kernel,
    "-append", "root=/dev/vda rootfstype=ext4 ro init=/usr/sbin/surogate-init console=hvc0 panic=-1 quiet",
    "-drive", `if=none,id=root,file=${option(disks.rootfs)},format=raw,readonly=on`, "-device", "virtio-blk-pci,drive=root",
    "-drive", `if=none,id=agent,file=${option(disks.agentDisk)},format=raw,readonly=on`, "-device", "virtio-blk-pci,drive=agent",
    "-drive", `if=none,id=sessions,file=${option(disks.sessions)},format=raw,discard=unmap`, "-device", "virtio-blk-pci,drive=sessions",
    "-device", "virtio-serial-pci",
    "-chardev", `socket,id=control,path=${option(join(run, "control.sock"))},server=on,wait=off`,
    "-device", "virtserialport,chardev=control,name=ai.surogate.control",
    "-chardev", `file,id=console,path=${option(console)}`, "-device", "virtconsole,chardev=console",
    ...ports,
    "-device", "virtio-rng-pci", "-nic", "none",
    "-qmp", `unix:${option(join(run, "qmp.sock"))},server=on,wait=off`,
    "-pidfile", join(run, "qemu.pid"),
    "-sandbox", "on,obsolete=deny,elevateprivileges=deny,spawn=deny,resourcecontrol=deny",
  ];
}

// One folder's daemon, in its namespace sandbox, which can reach nothing outside the
// folder: it maps the host user to the root's guest uid, so the root's files are its
// own in the guest and the host user's on the host.
export function virtiofsdArgs(folder: string, socket: string, guestUid: number, host: { uid: number; gid: number }): string[] {
  return [
    `--shared-dir=${folder}`, `--socket-path=${socket}`, "--sandbox=namespace", "--cache=auto",
    `--uid-map=:${guestUid}:${host.uid}:1:`, `--gid-map=:${guestUid}:${host.gid}:1:`,
  ];
}

// QEMU's monitor: a greeting, the capabilities' negotiation, then one command at a
// time, answered in turn by "return" or "error"; events come in between and are skipped.
export class Qmp {
  private readonly waiting: Array<{ resolve: (value: unknown) => void; reject: (error: Error) => void }> = [];
  private greet: (error?: Error) => void = () => {};
  private readonly greeted = new Promise<void>((resolve, reject) => {
    this.greet = (error) => (error ? reject(error) : resolve());
  });
  private closed = false;

  private constructor(private readonly socket: Socket) {
    // Nobody may be waiting for it when the monitor closes.
    this.greeted.catch(() => {});
    // readline passes the socket's errors on: a reset, or a connect that fails, ends in 'close' below.
    createInterface({ input: socket }).on("line", (line) => this.received(line)).on("error", () => {});
    socket.on("error", () => {});
    socket.on("close", () => {
      this.closed = true;
      this.greet(new Error("QEMU's monitor closed"));
      for (const waiter of this.waiting.splice(0)) waiter.reject(new Error("QEMU's monitor closed"));
    });
  }

  /**
   * The monitor on *socket*, QEMU's QMP socket, once QEMU has greeted it and taken
   * its capabilities; rejects when it closes first, or at *deadline* (performance.now()).
   */
  static async open(socket: Socket, deadline = Infinity): Promise<Qmp> {
    const qmp = new Qmp(socket);
    const ready = (async () => {
      await qmp.greeted;
      await qmp.execute("qmp_capabilities");
    })();
    ready.catch(() => {});
    let timer: NodeJS.Timeout | undefined;
    const late = new Promise<never>((_resolve, reject) => {
      if (deadline !== Infinity) {
        timer = setTimeout(() => reject(new Error("QEMU's monitor did not answer")), Math.max(0, deadline - performance.now()));
      }
    });
    try {
      await Promise.race([ready, late]);
    } catch (error) {
      qmp.close();
      throw error;
    } finally {
      clearTimeout(timer);
    }
    return qmp;
  }

  /**
   * Its "return", or a rejection with QEMU's own description of the error. At
   * *deadline* (performance.now()) it fails, and the monitor closes: QEMU answers
   * in turn, so its late answer would be taken for the next command's.
   */
  execute(command: string, args?: Record<string, unknown>, deadline = Infinity): Promise<unknown> {
    if (this.closed) return Promise.reject(new Error("QEMU's monitor closed"));
    return new Promise((resolve, reject) => {
      const timer = deadline === Infinity ? undefined : setTimeout(() => {
        reject(new Error("QEMU's monitor did not answer"));
        this.close();
      }, Math.max(0, deadline - performance.now()));
      const settled = () => clearTimeout(timer);
      this.waiting.push({
        resolve: (value) => (settled(), resolve(value)),
        reject: (error) => (settled(), reject(error)),
      });
      this.socket.write(`${JSON.stringify({ execute: command, ...(args ? { arguments: args } : {}) })}\n`);
    });
  }

  // Once QEMU closed it, or a command went unanswered: no command reaches QEMU again.
  get gone(): boolean {
    return this.closed;
  }

  close(): void {
    this.closed = true;
    this.socket.destroy();
  }

  private received(line: string): void {
    let message: Record<string, unknown>;
    try {
      message = JSON.parse(line) as Record<string, unknown>;
    } catch {
      return;
    }
    if ("QMP" in message) return this.greet();
    if (!("return" in message) && !("error" in message)) return;
    const waiter = this.waiting.shift();
    if ("return" in message) waiter?.resolve(message.return);
    else waiter?.reject(new Error(String((message.error as { desc?: unknown } | null)?.desc ?? "QEMU refused the command")));
  }
}
