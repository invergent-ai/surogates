// The VM layer's Linux backend (spec, Section 11): QEMU with KVM, or emulated where this
// computer gives it no KVM it can use; the control, net and inbound ports virtio-serial ports on
// Unix sockets of QEMU's, and each root's folder shared by a virtiofsd of its own, hot-added
// through QMP. QEMU and virtiofsd run through setpriv --pdeathsig, so they die with the
// manager however it dies, and each leaves a pidfile in the runtime folder, so a later
// manager can end one that did not.

import { type ChildProcess, execFile } from "node:child_process";
import {
  accessSync, closeSync, constants, existsSync, ftruncateSync, mkdirSync, openSync, readdirSync, readFileSync, readlinkSync, rmSync, writeFileSync,
} from "node:fs";
import { connect, type Socket } from "node:net";
import { userInfo } from "node:os";
import { basename, dirname, join } from "node:path";

import { cleanly, spawnClean } from "../clean-child.js";
import { findOnPath } from "../files/operations.js";
import type { Share } from "../guest/protocol.js";
import { pathOutside } from "../hosts/policy.js";
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

// Where the VM's tools are looked for: where the file helper's are, by the one rule (pathOutside).
// The app's PATH without its relative entries, and without any entry in one of *held*, the folders
// a chat is bound to, where a command may have written a program. The app's check holds them all.
// This process, which starts the VM, knows none of them: it drops the relative entries.
const toolPath = (held: Held = []) => pathOutside(process.env.PATH, held);
type Held = Array<{ dev: number; ino: number }>;
// What QEMU and virtiofsd are given of the app's environment: that PATH, where virtiofsd finds
// newuidmap and newgidmap, and nothing else: the user's shell may export what either acts on,
// OPENSSL_CONF and the loader's variables among them.
const toolEnv = (held: Held = []) => ({ PATH: toolPath(held) });
// QEMU by its whole path, as that PATH finds it, or null: it is run by that path and no other.
const qemuOn = (path: string) => findOnPath("qemu-system-x86_64", path, "/");

// A child that dies with this process, and the end of what it said on stderr.
function launch(argv: string[]): { child: ChildProcess; said: () => string } {
  const child = spawnClean("/usr/bin/setpriv", ["--pdeathsig", "KILL", "--", ...argv], { stdio: ["ignore", "ignore", "pipe"], env: toolEnv() });
  let said = "";
  child.stderr?.on("data", (chunk: Buffer) => {
    said = (said + chunk.toString()).slice(-4000);
  });
  child.on("error", () => {});
  return { child, said: () => said.trim() };
}

// Whether *name* is a program on *path*, where virtiofsd looks for newuidmap and newgidmap.
const onPath = (name: string, path = toolPath()) => findOnPath(name, path, "/") !== null;

// The major and minor version *program* says it is, as `--version` prints it, or null: one that has
// not answered in 5 s is killed, as a SIGTERM may be ignored.
const versionOf = (program: string | null, args: string[], held: Held = []) => new Promise<[number, number] | null>((resolve) => {
  if (program === null) return resolve(null);
  execFile(...cleanly(program, args), { timeout: 5_000, killSignal: "SIGKILL", env: toolEnv(held) }, (error, stdout) => {
    const found = error ? null : /(\d+)\.(\d+)/.exec(stdout);
    resolve(found ? [Number(found[1]), Number(found[2])] : null);
  });
});
const atLeast = (version: [number, number] | null, [major, minor]: [number, number]) =>
  version !== null && (version[0] > major || (version[0] === major && version[1] >= minor));

/**
 * What this computer lacks of what the VM runs on (spec, Section 11, Requirements), each
 * named as the install script installs it: QEMU 8.2 or later; Ubuntu's virtiofsd 1.10 or
 * later; newuidmap and newgidmap, which virtiofsd runs for its id maps; and, while the image
 * has something left to unpack (*unpacking*), zstd, which unpacks its download. Checked at
 * the app's start, and at the status line's Check again. QEMU and the two id-map tools are looked
 * for by the PATH rule of the file helper's tools, with *held* the folders chats are bound to.
 */
export async function missingTools(paths: { virtiofsd?: string; zstd?: string } = {}, unpacking = true, held: Held = []): Promise<string[]> {
  const path = toolPath(held);
  const [qemu, virtiofsd] = await Promise.all([
    versionOf(qemuOn(path), ["--version"], held),
    versionOf(paths.virtiofsd ?? VIRTIOFSD, ["--version"], held),
  ]);
  let zstd = true;
  try {
    accessSync(paths.zstd ?? "/usr/bin/zstd", constants.X_OK);
  } catch {
    zstd = false;
  }
  return [
    ...(atLeast(qemu, [8, 2]) ? [] : ["QEMU 8.2 or later"]),
    ...(atLeast(virtiofsd, [1, 10]) ? [] : ["virtiofsd 1.10 or later"]),
    ...(onPath("newuidmap", path) && onPath("newgidmap", path) ? [] : ["newuidmap and newgidmap"]),
    ...(zstd || !unpacking ? [] : ["zstd"]),
  ];
}

/** Whether a virtiofsd whose --help says *help* refuses a guest's writes itself (--readonly: 1.11 and later). */
export const readonlyFlag = (help: string) => /^\s*--readonly(\s|$)/m.test(help);

// What each virtiofsd's --help said of --readonly, once it was read.
const helps = new Map<string, Promise<boolean>>();

/**
 * Whether *program*, a virtiofsd, refuses a guest's writes itself, by its --help, read within *ms*.
 * Only a help that was read is an answer, and is kept: one that could not be read rejects, so the
 * folder is not shared without the option on a computer whose virtiofsd has it, and the next share asks again.
 */
export function refusesWrites(program = VIRTIOFSD, ms = 5_000): Promise<boolean> {
  const known = helps.get(program);
  if (known) return known;
  const asked = new Promise<boolean>((resolve, reject) => {
    execFile(...cleanly(program, ["--help"]), { timeout: ms, killSignal: "SIGKILL", env: toolEnv() }, (error, stdout) => {
      if (error) reject(new Error("the folder could not be shared read-only: virtiofsd did not say whether it refuses a guest's writes"));
      else resolve(readonlyFlag(stdout));
    });
  });
  helps.set(program, asked);
  asked.catch(() => {
    if (helps.get(program) === asked) helps.delete(program);
  });
  return asked;
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
 * QEMU on *options*, once its control, net and inbound ports and its monitor have taken their
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
  const program = qemuOn(toolPath());
  if (program === null) throw new Error("QEMU is not on the PATH (the qemu-system-x86 package)");
  const { child: qemu, said } = launch([program, ...qemuArgs(options, options.run, options.console, options.cpus, emulated !== null)]);
  const halt = () => qemu.kill("SIGKILL");
  signal?.addEventListener("abort", halt, { once: true });
  let control: Socket | null = null;
  let net: Socket | null = null;
  let inbound: Socket | null = null;
  try {
    control = await reach(join(options.run, "control.sock"), qemu, deadline);
    net = control ? await reach(join(options.run, "net.sock"), qemu, deadline) : null;
    inbound = net ? await reach(join(options.run, "inbound.sock"), qemu, deadline) : null;
    const monitor = inbound ? await reach(join(options.run, "qmp.sock"), qemu, deadline) : null;
    if (!control || !net || !inbound || !monitor) throw new Error(ended(qemu) ? "QEMU exited" : "QEMU did not open its sockets");
    return new LinuxVm(options, qemu, said, control, net, inbound, await Qmp.open(monitor, deadline), emulated);
  } catch (error) {
    control?.destroy();
    net?.destroy();
    inbound?.destroy();
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
    readonly inbound: Socket,
    private readonly qmp: Qmp,
    readonly emulated: Emulated | null,
  ) {
    this.exited = exited(qemu).then(said);
  }

  /**
   * *folder* on a free root port, by *deadline*: its virtiofsd, which maps the host user
   * to *uid* (so the guest mounts it as it is), then QMP's chardev-add and device_add.
   * A share not added gives its port back. *readonly*: its virtiofsd refuses every write, where it
   * has the option; the guest mounts it read-only either way (guest/places.ts). Where it cannot be
   * told whether it has, the folder is not shared.
   */
  async share(folder: string, uid: number, deadline: number, readonly = false): Promise<Share> {
    const refusing = readonly && (await refusesWrites());
    if (this.killed) throw new Error("the VM has gone");
    const port = Math.min(...this.free);
    if (port === Infinity) throw new Error(`it holds ${ROOT_PORTS} folders already, each of a chat at work`);
    this.free.delete(port);
    const n = (this.made += 1);
    const socket = join(this.options.run, `vfs-${n}.sock`);
    const pidfile = join(this.options.run, `vfs-${n}.pid`);
    const { child: daemon, said } = launch([VIRTIOFSD, ...virtiofsdArgs(folder, socket, uid, this.options.user, refusing)]);
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
      this.inbound.destroy();
      for (const child of [this.qemu, ...this.daemons]) child.kill("SIGKILL");
      await Promise.all([exited(this.qemu), ...this.daemons.map(exited)]);
    })();
    return this.killed;
  }
}
