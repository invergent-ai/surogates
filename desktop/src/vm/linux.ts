// The VM layer's Linux backend (spec, Section 11): QEMU with KVM, the control port
// a virtio-serial port on a Unix socket of QEMU's, and each root's folder shared by
// a virtiofsd of its own, hot-added through QMP. QEMU and virtiofsd run through
// setpriv --pdeathsig, so they die with the manager however it dies, and each
// leaves a pidfile in the runtime folder, so a later manager can end one that did not.

import { type ChildProcess, spawn } from "node:child_process";
import {
  accessSync, closeSync, constants, existsSync, ftruncateSync, mkdirSync, openSync, readdirSync, readFileSync, readlinkSync, rmSync, writeFileSync,
} from "node:fs";
import { connect, type Socket } from "node:net";
import { basename, dirname, join } from "node:path";

import type { BootVm, VmBackend, VmOptions } from "./manager.js";
import { Qmp, qemuArgs, ROOT_PORTS, VIRTIOFSD, virtiofsdArgs } from "./qemu.js";

// A share's hot-add, its virtiofsd's start included (Section 11's timeouts).
const SHARE_MS = 15_000;
const SESSIONS_BYTES = 32 * 1024 ** 3;
const RETRY_MS = 5;

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

// A child that dies with this process, and the end of what it said on stderr.
function launch(argv: string[]): { child: ChildProcess; said: () => string } {
  const child = spawn("/usr/bin/setpriv", ["--pdeathsig", "KILL", "--", ...argv], { stdio: ["ignore", "ignore", "pipe"] });
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
 * QEMU on *options*, once its control port and its monitor have taken their
 * connections: its runtime folder swept first, and the sparse sessions disk made
 * at the first boot. Rejects with why not, QEMU's own words included.
 */
export const bootLinux: BootVm = async (options, signal, deadline) => {
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
  const { child: qemu, said } = launch(["qemu-system-x86_64", ...qemuArgs(options, options.run, options.console, options.cpus)]);
  const halt = () => qemu.kill("SIGKILL");
  signal?.addEventListener("abort", halt, { once: true });
  let control: Socket | null = null;
  try {
    control = await reach(join(options.run, "control.sock"), qemu, deadline);
    const monitor = control ? await reach(join(options.run, "qmp.sock"), qemu, deadline) : null;
    if (!control || !monitor) throw new Error(ended(qemu) ? "QEMU exited" : "QEMU did not open its sockets");
    return new LinuxVm(options, qemu, said, control, await Qmp.open(monitor, deadline));
  } catch (error) {
    control?.destroy();
    qemu.kill("SIGKILL");
    await exited(qemu);
    throw new Error([(error as Error).message, said()].filter(Boolean).join(": "));
  } finally {
    signal?.removeEventListener("abort", halt);
  }
};

// One QEMU, and a virtiofsd for each folder shared into it.
class LinuxVm implements VmBackend {
  readonly exited: Promise<string>;
  private readonly daemons: ChildProcess[] = [];
  private port = 0;
  private killed: Promise<void> | null = null;

  constructor(
    private readonly options: VmOptions,
    private readonly qemu: ChildProcess,
    said: () => string,
    readonly control: Socket,
    private readonly qmp: Qmp,
  ) {
    this.exited = exited(qemu).then(said);
  }

  /** *folder* on a root port of its own: its virtiofsd, mapping the host user to *uid*, then QMP's chardev-add and device_add. */
  async share(folder: string, uid: number): Promise<string> {
    if (this.killed) throw new Error("the VM has gone");
    if (this.port >= ROOT_PORTS) throw new Error(`it holds ${ROOT_PORTS} folders already, its most until the app restarts`);
    this.port += 1;
    const n = this.port;
    const socket = join(this.options.run, `vfs-${n}.sock`);
    const { child: daemon, said } = launch([VIRTIOFSD, ...virtiofsdArgs(folder, socket, uid, this.options.user)]);
    this.daemons.push(daemon);
    writeFileSync(join(this.options.run, `vfs-${n}.pid`), String(daemon.pid ?? ""));
    const deadline = performance.now() + SHARE_MS;
    while (!existsSync(socket)) {
      if (ended(daemon) || performance.now() > deadline) {
        daemon.kill("SIGKILL");
        throw new Error(`virtiofsd did not start: ${said()}`);
      }
      await new Promise((resolve) => setTimeout(resolve, RETRY_MS));
    }
    try {
      await this.qmp.execute("chardev-add", {
        id: `vfs${n}`, backend: { type: "socket", data: { addr: { type: "unix", data: { path: socket } }, server: false } },
      }, deadline);
      await this.qmp.execute("device_add", { driver: "vhost-user-fs-pci", id: `fs${n}`, chardev: `vfs${n}`, tag: `r${n}`, bus: `rp${n}` }, deadline);
    } catch (error) {
      // Not added: its daemon serves the folder to nobody.
      daemon.kill("SIGKILL");
      throw error;
    }
    // The folder is the guest's now: a daemon that goes takes the VM with it.
    void exited(daemon).then(() => this.qemu.kill("SIGKILL"));
    return `r${n}`;
  }

  // A power cut: only the sessions disk is written, and it is journaled and checked at the next boot.
  // The sockets and pidfiles stay for the next boot's sweep, or the manager's stop.
  kill(): Promise<void> {
    this.killed ??= (async () => {
      this.qmp.close();
      this.control.destroy();
      for (const child of [this.qemu, ...this.daemons]) child.kill("SIGKILL");
      await Promise.all([exited(this.qemu), ...this.daemons.map(exited)]);
    })();
    return this.killed;
  }
}
