// The VM layer's Linux backend (spec, Section 11): QEMU with KVM, the control and net
// ports virtio-serial ports on Unix sockets of QEMU's, and each root's folder shared by
// a virtiofsd of its own, hot-added through QMP. QEMU and virtiofsd run through
// setpriv --pdeathsig, so they die with the manager however it dies, and each
// leaves a pidfile in the runtime folder, so a later manager can end one that did not.

import { type ChildProcess, spawn } from "node:child_process";
import {
  accessSync, closeSync, constants, existsSync, ftruncateSync, mkdirSync, openSync, readdirSync, readFileSync, readlinkSync, rmSync, writeFileSync,
} from "node:fs";
import { connect, type Socket } from "node:net";
import { userInfo } from "node:os";
import { basename, dirname, join } from "node:path";

import type { Share } from "../guest/protocol.js";
import type { BootVm, Emulated, VmBackend, VmOptions } from "./manager.js";
import { Qmp, qemuArgs, ROOT_PORTS, VIRTIOFSD, virtiofsdArgs } from "./qemu.js";

const SESSIONS_BYTES = 32 * 1024 ** 3;
const RETRY_MS = 5;

// Settles with "late" at *deadline* (performance.now()), its timer not holding the process.
const late = (deadline: number) => new Promise<"late">((resolve) => {
  setTimeout(() => resolve("late"), Math.max(0, deadline - performance.now())).unref();
});

/**
 * What a manager that died left running, where its pdeathsig did not reach. Each
 * pidfile this backend writes in *run* names a process, killed only when it is that
 * QEMU or that virtiofsd: its program, and an argument that names its own file in
 * *run*, never a part of its command line. A pid used again since is left alone.
 * The folder is then emptied for the next guest's sockets.
 */
export function sweep(run: string): void {
  let names: string[] = [];
  try {
    names = readdirSync(run);
  } catch {
    // No folder yet.
  }
  for (const name of names) {
    const share = /^vfs-(\d+)\.pid$/.exec(name);
    // virtiofsd's own vfs-<n>.sock.pid is not one of these.
    if (name !== "qemu.pid" && !share) continue;
    try {
      const pid = Number(readFileSync(join(run, name), "utf8").trim());
      if (!Number.isInteger(pid) || pid <= 1) continue;
      const program = readlinkSync(`/proc/${pid}/exe`);
      const argv = readFileSync(`/proc/${pid}/cmdline`, "utf8").split("\0");
      const ours = share
        ? program === VIRTIOFSD && argv.includes(`--socket-path=${join(run, `vfs-${share[1]}.sock`)}`)
        : basename(program) === "qemu-system-x86_64" && argv.includes(join(run, "qemu.pid"));
      if (ours) process.kill(pid, "SIGKILL");
    } catch {
      // Gone, or not ours to read.
    }
  }
  rmSync(run, { recursive: true, force: true });
  mkdirSync(run, { recursive: true, mode: 0o700 });
}

// What QEMU and virtiofsd are given of the app's environment: its PATH, where setpriv finds QEMU and
// virtiofsd finds newuidmap and newgidmap, and nothing else: the user's shell may export what either
// acts on, OPENSSL_CONF and the loader's variables among them.
const toolEnv = () => ({ PATH: process.env.PATH ?? "" });

// A child that dies with this process, and the end of what it said on stderr.
function launch(argv: string[]): { child: ChildProcess; said: () => string } {
  const child = spawn("/usr/bin/setpriv", ["--pdeathsig", "KILL", "--", ...argv], { stdio: ["ignore", "ignore", "pipe"], env: toolEnv() });
  let said = "";
  child.stderr?.on("data", (chunk: Buffer) => {
    said = (said + chunk.toString()).slice(-4000);
  });
  child.on("error", () => {});
  return { child, said: () => said.trim() };
}

// Whether *name* is a program on the PATH, where virtiofsd looks for newuidmap and newgidmap.
function onPath(name: string): boolean {
  return (process.env.PATH ?? "").split(":").some((folder) => {
    try {
      accessSync(join(folder || ".", name), constants.X_OK);
      return true;
    } catch {
      return false;
    }
  });
}

const ended = (child: ChildProcess) => child.exitCode !== null || child.signalCode !== null;

const exited = (child: ChildProcess) => new Promise<void>((resolve) => {
  if (ended(child)) resolve();
  else child.once("exit", () => resolve());
  child.once("error", () => resolve());
});

// A socket QEMU makes as it starts, tried until it takes the connection; null once *qemu* has exited, or at *deadline*.
async function reach(path: string, qemu: ChildProcess, deadline: number): Promise<Socket | null> {
  while (!ended(qemu) && performance.now() <= deadline) {
    const socket = await new Promise<Socket | null>((resolve) => {
      const tried = connect(path);
      tried.once("connect", () => resolve(tried));
      tried.once("error", () => setTimeout(() => resolve(null), RETRY_MS));
    });
    if (socket) return socket;
  }
  return null;
}

/**
 * Null when this user can open *kvm* read and write, as QEMU does for KVM; otherwise why
 * the guest runs emulated. One that cannot open it but is in its group (in *groups*,
 * /etc/group), as the install script's user is until they log in again, is told to.
 */
export function emulation(kvm = "/dev/kvm", groups = "/etc/group"): Emulated | null {
  try {
    closeSync(openSync(kvm, constants.O_RDWR));
    return null;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code !== "EACCES" && (error as NodeJS.ErrnoException).code !== "EPERM") return "no-kvm";
  }
  let line: string | undefined;
  try {
    line = readFileSync(groups, "utf8").split("\n").find((entry) => entry.startsWith(`${basename(kvm)}:`));
  } catch {
    // No group file to read: as one that does not name this user.
  }
  const [, , gid, members = ""] = line?.split(":") ?? [];
  const named = members.split(",").includes(userInfo().username);
  return named && !process.getgroups?.().includes(Number(gid)) ? "relogin" : "no-access";
}

// QEMU's words when it cannot use KVM it could open: another hypervisor holds it, or the host's VM lacks it.
const KVM_FAILED = /failed to initialize kvm|Could not access KVM kernel module/;

/**
 * QEMU on *options*, once its control and net ports and its monitor have taken their
 * connections: with KVM when this user can open it, and emulated otherwise, or when
 * QEMU could not use it. Its runtime folder is swept first, and the sparse sessions
 * disk made at the first boot. Rejects with why not, QEMU's own words included.
 */
export const bootLinux: BootVm = async (options, signal, deadline) => {
  const emulated = emulation(options.kvm);
  try {
    return await launchVm(options, signal, deadline, emulated);
  } catch (error) {
    if (emulated !== null || signal?.aborted || !KVM_FAILED.test((error as Error).message)) throw error;
    return launchVm(options, signal, deadline, "kvm-failed");
  }
};

async function launchVm(options: VmOptions, signal: AbortSignal | undefined, deadline: number, emulated: Emulated | null): Promise<VmBackend> {
  if (signal?.aborted) throw new Error("The boot was stopped");
  // Its uid and gid maps: without them no folder could be shared, and virtiofsd's own words would not reach the user.
  if (!onPath("newuidmap") || !onPath("newgidmap")) throw new Error("virtiofsd needs newuidmap and newgidmap (the uidmap package)");
  sweep(options.run);
  if (!existsSync(options.sessions)) {
    mkdirSync(dirname(options.sessions), { recursive: true, mode: 0o700 });
    // Sparse: the guest formats it at its first boot, and it grows as it is used.
    const fd = openSync(options.sessions, "wx", 0o600);
    ftruncateSync(fd, SESSIONS_BYTES);
    closeSync(fd);
  }
  mkdirSync(dirname(options.console), { recursive: true, mode: 0o700 });
  const { child: qemu, said } = launch(["qemu-system-x86_64", ...qemuArgs(options, options.run, options.console, options.cpus, emulated !== null)]);
  const halt = () => qemu.kill("SIGKILL");
  signal?.addEventListener("abort", halt, { once: true });
  let control: Socket | null = null;
  let net: Socket | null = null;
  try {
    control = await reach(join(options.run, "control.sock"), qemu, deadline);
    net = control ? await reach(join(options.run, "net.sock"), qemu, deadline) : null;
    const monitor = net ? await reach(join(options.run, "qmp.sock"), qemu, deadline) : null;
    if (!control || !net || !monitor) throw new Error(ended(qemu) ? "QEMU exited" : "QEMU did not open its sockets");
    return new LinuxVm(options, qemu, said, control, net, await Qmp.open(monitor, deadline), emulated);
  } catch (error) {
    control?.destroy();
    net?.destroy();
    qemu.kill("SIGKILL");
    await exited(qemu);
    throw new Error([(error as Error).message, said()].filter(Boolean).join(": "));
  } finally {
    signal?.removeEventListener("abort", halt);
  }
}

// A folder shared into the guest: its number, the root port it is on, and its virtiofsd.
interface Shared {
  n: number;
  port: number;
  daemon: ChildProcess;
  // Its removal, once asked: its daemon's end is the removal's, not a loss.
  removal: Promise<void> | null;
}

// One QEMU, and a virtiofsd for each folder shared into it.
class LinuxVm implements VmBackend {
  readonly exited: Promise<string>;
  private readonly daemons: ChildProcess[] = [];
  // Each share in the guest, by its tag.
  private readonly shares = new Map<string, Shared>();
  // The root ports no share is on. A share's number, in its tag, ids, socket and pidfile,
  // is never used again in this VM, so nothing of a share removed is taken for the next's.
  private readonly free = new Set(Array.from({ length: ROOT_PORTS }, (_, n) => n + 1));
  private made = 0;
  private killed: Promise<void> | null = null;

  constructor(
    private readonly options: VmOptions,
    private readonly qemu: ChildProcess,
    said: () => string,
    readonly control: Socket,
    readonly net: Socket,
    private readonly qmp: Qmp,
    readonly emulated: Emulated | null,
  ) {
    this.exited = exited(qemu).then(said);
  }

  /**
   * *folder* on a free root port, by *deadline*: its virtiofsd, which maps the host user
   * to *uid* (so the guest mounts it as it is), then QMP's chardev-add and device_add.
   * A share not added gives its port back.
   */
  async share(folder: string, uid: number, deadline: number): Promise<Share> {
    if (this.killed) throw new Error("the VM has gone");
    const port = Math.min(...this.free);
    if (port === Infinity) throw new Error(`it holds ${ROOT_PORTS} folders already, each of a chat at work`);
    this.free.delete(port);
    const n = (this.made += 1);
    const socket = join(this.options.run, `vfs-${n}.sock`);
    const pidfile = join(this.options.run, `vfs-${n}.pid`);
    const { child: daemon, said } = launch([VIRTIOFSD, ...virtiofsdArgs(folder, socket, uid, this.options.user)]);
    this.daemons.push(daemon);
    writeFileSync(pidfile, String(daemon.pid ?? ""));
    try {
      while (!existsSync(socket)) {
        if (ended(daemon) || performance.now() > deadline) throw new Error(`virtiofsd did not start: ${said()}`);
        await new Promise((resolve) => setTimeout(resolve, RETRY_MS));
      }
      await this.qmp.execute("chardev-add", {
        id: `vfs${n}`, backend: { type: "socket", data: { addr: { type: "unix", data: { path: socket } }, server: false } },
      }, deadline);
      await this.qmp.execute("device_add", { driver: "vhost-user-fs-pci", id: `fs${n}`, chardev: `vfs${n}`, tag: `r${n}`, bus: `rp${port}` }, deadline)
        .catch(async (error: unknown) => {
          if (!this.qmp.gone) await this.qmp.execute("chardev-remove", { id: `vfs${n}` }, deadline).catch(() => {});
          throw error;
        });
    } catch (error) {
      // Not added: its daemon serves the folder to nobody.
      daemon.kill("SIGKILL");
      rmSync(pidfile, { force: true });
      // A monitor that has gone, as at a command QEMU did not answer in time, adds
      // no folder again: the VM goes, and the share fails once it has, its guest lost.
      if (this.qmp.gone) {
        this.qemu.kill("SIGKILL");
        await this.exited;
      } else {
        this.free.add(port);
      }
      throw error;
    }
    const shared: Shared = { n, port, daemon, removal: null };
    this.shares.set(`r${n}`, shared);
    // The folder is the guest's now: a daemon that goes, but by its removal, takes the VM with it.
    void exited(daemon).then(() => {
      if (!shared.removal) this.qemu.kill("SIGKILL");
    });
    return { kind: "virtiofs", tag: `r${n}` };
  }

  /**
   * *share* out of the running guest once the guest has let it go: QMP's device_del and
   * its DEVICE_DELETED, then chardev-remove, after which its virtiofsd ends by itself.
   * Its root port takes the next share. A guest that does not let it go by *deadline*,
   * such as one whose request to a stalled share is still waiting, ends the VM: the
   * share would hold its port and its folder's daemon, half-removed.
   */
  unshare(share: Share, deadline: number): Promise<void> {
    const shared = this.shares.get(share.tag);
    if (!shared) return Promise.resolve();
    // Asked twice, as by two teardowns of one root at once: the one removal answers both.
    shared.removal ??= this.remove(share, shared, deadline);
    return shared.removal;
  }

  private async remove(share: Share, shared: Shared, deadline: number): Promise<void> {
    const { n, daemon } = shared;
    let stop = () => {};
    const deleted = new Promise<"deleted">((resolve) => {
      stop = this.qmp.on("DEVICE_DELETED", (data) => {
        if (data.device === `fs${n}`) resolve("deleted");
      });
    });
    try {
      await this.qmp.execute("device_del", { id: `fs${n}` }, deadline);
      const letGo = await Promise.race([deleted, this.exited.then(() => "exited" as const), late(deadline)]);
      if (letGo !== "deleted") throw new Error(letGo === "exited" ? "the VM has gone" : `the guest did not let ${share.tag} go in time`);
      await this.qmp.execute("chardev-remove", { id: `vfs${n}` }, deadline);
    } catch (error) {
      this.qemu.kill("SIGKILL");
      await this.exited;
      throw error;
    } finally {
      stop();
    }
    // Its connection has gone: one that has not ended by the deadline is ended.
    if ((await Promise.race([exited(daemon), late(deadline)])) === "late") daemon.kill("SIGKILL");
    this.shares.delete(share.tag);
    rmSync(join(this.options.run, `vfs-${n}.pid`), { force: true });
    this.free.add(shared.port);
  }

  // A power cut: only the sessions disk is written, and it is journaled and checked at the next boot.
  // The sockets and pidfiles stay for the next boot's sweep, or the manager's stop.
  kill(): Promise<void> {
    this.killed ??= (async () => {
      this.qmp.close();
      this.control.destroy();
      this.net.destroy();
      for (const child of [this.qemu, ...this.daemons]) child.kill("SIGKILL");
      await Promise.all([exited(this.qemu), ...this.daemons.map(exited)]);
    })();
    return this.killed;
  }
}
