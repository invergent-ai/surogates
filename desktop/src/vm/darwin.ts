// The VM layer's macOS backend (spec, Section 11): the guest on Apple's Virtualization framework,
// through surogate-vm (native/macos), the one program of the app's with the framework's
// entitlement. It speaks surogate-vm's line protocol on its stdin and stdout, and connects to the
// guest's control, net and inbound ports as Unix sockets surogate-vm makes in the runtime folder,
// as the Linux backend connects to QEMU's. Each folder is shared by Apple's own virtio-fs server,
// which maps no owner and shows each file as the asking uid's: every root runs on its own uid, as
// on Linux, where its folder shows it that uid (guest/root.ts, checkOwner). surogate-vm stops the VM when its
// stdin ends, so the VM ends with the manager however it ends; it writes its pid in the runtime
// folder, and the next boot's sweep ends one that outlived its manager.

import { type ChildProcess, execFile, type SpawnOptions } from "node:child_process";
import { createHash } from "node:crypto";
import { closeSync, existsSync, ftruncateSync, mkdirSync, openSync, realpathSync } from "node:fs";
import type { Socket } from "node:net";
import { tmpdir } from "node:os";
import { basename, dirname, join } from "node:path";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";

import { cleanly, spawnClean } from "../clean-child.js";
import type { Share } from "../guest/protocol.js";
import { MAX_SHARES } from "../guest/protocol.js";
import type { BootVm, VmBackend, VmOptions } from "./manager.js";
import { guestCpus, KERNEL_COMMAND_LINE } from "./qemu.js";
import { ended, reach } from "./sockets.js";

const SESSIONS_BYTES = 32 * 1024 ** 3;
const MEMORY_MIB = 2048;
// How the folders are laid out in the guest (native/macos, Shares.swift): one device that holds every
// folder, or eight of a folder each, as a Mac measures which the framework takes while the guest runs
// (test/vm/darwin/measure/shares.ts).
export type Layout = "multiple" | "single";
export const LAYOUT: Layout = "multiple";
// How long surogate-vm has after its kill line to end the VM and exit, before it is killed.
const KILL_MS = 1_000;
// The longest Unix socket path Darwin takes, its sockaddr_un's sun_path without the NUL.
export const SOCKET_MAX = 103;
const SOCKETS = ["control.sock", "net.sock", "inbound.sock"];

// The same from src/vm and from dist/vm: desktop/, or a packaged app's Contents/Resources/app.
const PACKAGE = fileURLToPath(new URL("../..", import.meta.url));
const PACKAGED = basename(dirname(PACKAGE)) === "Resources" && basename(dirname(dirname(PACKAGE))) === "Contents";
/** The app's own files, whose agent disk surogate-vm boots: Surogate.app in a packaged app, desktop/ in development. */
export const APP_FILES = PACKAGED ? dirname(dirname(dirname(PACKAGE))) : PACKAGE;
/** surogate-vm: beside the app's main executable in a packaged app, native/build.sh's signed copy in development. */
export const VM_PROGRAM = PACKAGED ? join(APP_FILES, "Contents", "MacOS", "surogate-vm") : join(PACKAGE, "native", "out", "surogate-vm");

/** How surogate-vm is started: through spawnClean, so it holds nothing of this process's but its stdio. */
export type Start = (program: string, args: string[], options: SpawnOptions & { env: NodeJS.ProcessEnv }) => ChildProcess;

// surogate-vm is given the PATH alone, as QEMU is on Linux: it reads none of the user's environment.
const ENV = { PATH: "/usr/bin:/bin" };

/**
 * The VM's runtime folder for *dataDir*: under the user's own temp folder, 0700 (*temp*: $TMPDIR, a folder of
 * /var/folders, as os.tmpdir() gives it), one for each data folder, so two apps never share one and a boot's
 * sweep reaches no other app's guest. By the temp folder's real path: /var is a link to /private/var, and
 * surogate-vm takes no path with a link on its way. Its sockets' paths must fit in Darwin's 104 bytes (fitting).
 */
export function runtimeFolder(dataDir: string, temp = tmpdir()): string {
  let real = temp;
  try {
    real = realpathSync(temp);
  } catch {
    // Not there yet: as it is named.
  }
  return join(real, "surogate", `vm-${createHash("sha256").update(dataDir).digest("hex").slice(0, 8)}`);
}

/** Why the VM's sockets cannot be made in *run*, its longest path over Darwin's SOCKET_MAX bytes, or null. */
export function fitting(run: string): string | null {
  const longest = join(run, SOCKETS.reduce((a, b) => (b.length > a.length ? b : a)));
  const bytes = Buffer.byteLength(longest);
  return bytes > SOCKET_MAX ? `this Mac's temp folder's path is too long for the sandbox's sockets: ${longest} is ${bytes} bytes, over ${SOCKET_MAX}` : null;
}

// What surogate-vm says, one JSON object a line.
type Said =
  | { type: "listening" | "started" }
  | { type: "failed"; why: string }
  | { type: "shared"; id: number; tag: string; name?: string }
  | { type: "unshared"; id: number }
  | { type: "refused"; id?: number; why: string }
  | { type: "stopped"; reason: string; why?: string };

// Settles with "late" at *deadline* (performance.now()), its timer not holding the process.
const late = (deadline: number) => new Promise<"late">((resolve) => {
  setTimeout(() => resolve("late"), Math.max(0, deadline - performance.now())).unref();
});

const exited = (child: ChildProcess) => new Promise<void>((resolve) => {
  if (ended(child)) resolve();
  else child.once("exit", () => resolve());
  child.once("error", () => resolve());
});

/** surogate-vm, its lines heard, its stderr's end kept. */
class Helper {
  readonly child: ChildProcess;
  readonly gone: Promise<void>;
  private said = "";
  // The last words of its stopped or failed line.
  last = "";
  private readonly listeners = new Set<(said: Said) => void>();

  constructor(start: Start, program: string, args: string[]) {
    this.child = start(program, args, { stdio: ["pipe", "pipe", "pipe"], env: ENV });
    this.child.on("error", () => {});
    this.child.stdin?.on("error", () => {});
    this.child.stderr?.on("data", (chunk: Buffer) => {
      this.said = (this.said + chunk.toString()).slice(-4000);
    });
    if (this.child.stdout) {
      createInterface({ input: this.child.stdout, crlfDelay: Infinity }).on("line", (line) => {
        let said: Said;
        try {
          said = JSON.parse(line) as Said;
        } catch {
          return;
        }
        if (said.type === "failed") this.last = said.why;
        if (said.type === "stopped") this.last = said.why ?? "";
        for (const listener of this.listeners) listener(said);
      });
    }
    this.gone = exited(this.child);
  }

  send(line: object): void {
    if (!ended(this.child)) this.child.stdin?.write(`${JSON.stringify(line)}\n`);
  }

  on(listener: (said: Said) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  /** The first line *wanted* takes, by *deadline*, or what ended the wait: the helper's exit, or the deadline. */
  async first(wanted: (said: Said) => boolean, deadline: number): Promise<Said | "exited" | "late"> {
    let stop = () => {};
    const heard = new Promise<Said>((resolve) => {
      stop = this.on((said) => {
        if (wanted(said)) resolve(said);
      });
    });
    try {
      return await Promise.race([heard, this.gone.then(() => "exited" as const), late(deadline)]);
    } finally {
      stop();
    }
  }

  /** Its last words: its stopped or failed line's, else the end of what it said on stderr. */
  words(): string {
    return this.last || this.said.trim();
  }
}

/**
 * A boot on macOS, as *start* starts surogate-vm: spawnClean in the app; the measurements on a Mac
 * give Node's own until the app's clean spawn on macOS is built.
 */
export function darwinBoot(start: Start): BootVm {
  return async (options, signal, deadline) => {
    if (signal?.aborted) throw new Error("The boot was stopped");
    const tooLong = fitting(options.run);
    if (tooLong) throw new Error(tooLong);
    const program = options.program ?? VM_PROGRAM;
    mkdirSync(dirname(options.run), { recursive: true, mode: 0o700 });
    mkdirSync(options.run, { recursive: true, mode: 0o700 });
    await sweep(start, program, options.run, deadline);
    if (!existsSync(options.sessions)) {
      mkdirSync(dirname(options.sessions), { recursive: true, mode: 0o700 });
      // Sparse: the guest formats it at its first boot, and it grows as it is used.
      const fd = openSync(options.sessions, "wx", 0o600);
      ftruncateSync(fd, SESSIONS_BYTES);
      closeSync(fd);
    }
    mkdirSync(dirname(options.console), { recursive: true, mode: 0o700 });
    const data = options.data ?? dirname(dirname(options.sessions));
    const helper = new Helper(start, program, ["run", options.run, "--data", data, "--image", dirname(options.kernel), "--bundle", options.bundle ?? dirname(options.agentDisk)]);
    const halt = () => helper.child.kill("SIGKILL");
    signal?.addEventListener("abort", halt, { once: true });
    const sockets: Socket[] = [];
    try {
      const listening = await helper.first((said) => said.type === "listening" || said.type === "failed", deadline);
      if (listening === "late") throw new Error("surogate-vm did not open its sockets");
      if (listening === "exited" || listening.type !== "listening") throw new Error(helper.words() || "surogate-vm exited");
      for (const name of SOCKETS) {
        const socket = await reach(join(options.run, name), helper.child, deadline);
        if (!socket) throw new Error(ended(helper.child) ? "surogate-vm exited" : "surogate-vm did not take a connection to its sockets");
        sockets.push(socket);
      }
      helper.send({
        type: "boot", kernel: options.kernel, rootfs: options.rootfs, agentDisk: options.agentDisk, sessions: options.sessions, console: options.console,
        cpus: options.cpus ?? guestCpus(), memoryMiB: MEMORY_MIB, commandLine: KERNEL_COMMAND_LINE, layout: options.layout ?? LAYOUT,
      });
      const started = await helper.first((said) => said.type === "started" || said.type === "failed", deadline);
      if (started === "late") throw new Error("the VM did not start in time");
      if (started === "exited" || started.type !== "started") throw new Error(helper.words() || "surogate-vm exited");
      const [control, net, inbound] = sockets as [Socket, Socket, Socket];
      return new DarwinVm(options, helper, control, net, inbound);
    } catch (error) {
      for (const socket of sockets) socket.destroy();
      helper.child.kill("SIGKILL");
      await helper.gone;
      const said = helper.words();
      throw new Error([(error as Error).message, said === (error as Error).message ? "" : said].filter(Boolean).join(": "));
    } finally {
      signal?.removeEventListener("abort", halt);
    }
  };
}

/** The boot of the app on macOS. */
export const bootDarwin: BootVm = darwinBoot(spawnClean);

// What a manager that died left: surogate-vm's sweep, which ends the surogate-vm its pidfile names
// while it is still one run for this folder, and empties the folder.
async function sweep(start: Start, program: string, run: string, deadline: number): Promise<void> {
  const swept = new Helper(start, program, ["sweep", run]);
  if ((await Promise.race([swept.gone, late(deadline)])) === "late") {
    swept.child.kill("SIGKILL");
    throw new Error("surogate-vm did not sweep its folder in time");
  }
  if (swept.child.exitCode !== 0) throw new Error(`surogate-vm could not sweep its folder: ${swept.words() || `it ended ${swept.child.exitCode ?? swept.child.signalCode}`}`);
}

/**
 * What this Mac lacks of what its VM runs on: virtualization the app can use, as `surogate-vm check` says
 * (VZVirtualMachine.isSupported, and the framework's entitlement), in Section 11's words for a Mac that
 * cannot. *program*: surogate-vm; *ms*: how long its answer is waited for.
 */
export function requirementsDarwin(program = VM_PROGRAM, ms = 5_000): Promise<string[]> {
  const cannot = (why: string) => [`This Mac cannot run Surogate's sandbox: ${why}`];
  let argv: [string, string[]];
  try {
    argv = cleanly(program, ["check"]);
  } catch (error) {
    return Promise.resolve(cannot((error as Error).message));
  }
  return new Promise((resolve) => {
    execFile(...argv, { timeout: ms, killSignal: "SIGKILL", env: ENV }, (error, stdout) => {
      let answer: { supported?: unknown; reason?: unknown } = {};
      try {
        answer = JSON.parse(stdout) as typeof answer;
      } catch {
        // Not its answer.
      }
      if (answer.supported === true) return resolve([]);
      resolve(cannot(typeof answer.reason === "string" ? answer.reason : error ? `surogate-vm did not answer: ${error.message}` : "surogate-vm did not answer"));
    });
  });
}

// A folder shared into the guest: its name in surogate-vm, and what holds it.
interface Shared {
  name: string;
  held: "chat" | "copy" | "place";
  removal: Promise<void> | null;
}

// One surogate-vm, its VM, and the folders shared into it.
class DarwinVm implements VmBackend {
  readonly exited: Promise<string>;
  readonly emulated = null;
  // VZSharedDirectory's init(url:readOnly:) takes a folder by its URL, and Apple's documentation does not say when the
  // framework's server opens it: as it is shared, at the guest's first look, or at each.
  readonly opensByPath = true;
  // Each share in the guest, by its tag and name; each name, r<n>, is never given again in this VM.
  private readonly shares = new Map<string, Shared>();
  private made = 0;
  private asked = 0;
  private killed: Promise<void> | null = null;

  constructor(
    private readonly options: VmOptions,
    private readonly helper: Helper,
    readonly control: Socket,
    readonly net: Socket,
    readonly inbound: Socket,
  ) {
    this.exited = helper.gone.then(() => helper.words());
  }

  // surogate-vm's answer to a line of *id*, by *deadline*; one not given in time ends the VM, as one that cannot share any more.
  private async answer(line: { type: string; id: number } & Record<string, unknown>, deadline: number): Promise<Said> {
    const id = line.id;
    const pending = this.helper.first((said) => "id" in said && said.id === id, deadline);
    this.helper.send(line);
    const said = await pending;
    if (said === "exited") throw new Error("the VM has gone");
    if (said === "late") {
      await this.kill();
      throw new Error("surogate-vm did not answer in time");
    }
    return said;
  }

  /**
   * *folder* shared into the running guest, by *deadline*, as a directory of the one device for every folder
   * (or one of eight, as surogate-vm lays them out). Apple's share maps no owner, so *uid* maps nothing: it
   * shows each file as the asking uid's, the root's own; 0 is the guest's root's, for a folder's place.
   * *readonly*: the place's real folder, which
   * Apple's server keeps read-only (VZSharedDirectory's readOnly).
   */
  async share(folder: string, uid: number, deadline: number, { readonly = false, copy = false } = {}): Promise<Share> {
    if (this.killed) throw new Error("the VM has gone");
    if (this.shares.size >= MAX_SHARES) throw new Error(this.full());
    const name = `r${(this.made += 1)}`;
    const said = await this.answer({ type: "share", id: (this.asked += 1), name, path: folder, readOnly: readonly }, deadline);
    if (said.type !== "shared") throw new Error(said.type === "refused" ? `the folder could not be shared: ${said.why}` : "surogate-vm answered no share");
    const share: Share = said.name === undefined ? { kind: "vz", tag: said.tag } : { kind: "vz", tag: said.tag, name: said.name };
    this.shares.set(key(share), { name, held: uid === 0 ? "place" : copy ? "copy" : "chat", removal: null });
    return share;
  }

  /**
   * *share* out of the running guest, by *deadline*: surogate-vm takes its folder out of the device's share.
   * One it does not let go of in time, or refuses, ends the VM: the share would hold its place, half-removed.
   */
  unshare(share: Share, deadline: number): Promise<void> {
    const shared = this.shares.get(key(share));
    if (!shared) return Promise.resolve();
    shared.removal ??= this.remove(share, shared, deadline);
    return shared.removal;
  }

  private async remove(share: Share, shared: Shared, deadline: number): Promise<void> {
    const said = await this.answer({ type: "unshare", id: (this.asked += 1), name: shared.name }, deadline);
    if (said.type !== "unshared") {
      await this.kill();
      throw new Error(said.type === "refused" ? `the folder could not be let go: ${said.why}` : "surogate-vm let no folder go");
    }
    this.shares.delete(key(share));
  }

  // Why no other folder is shared: what holds each place, as on Linux.
  private full(): string {
    const count = (what: Shared["held"]) => [...this.shares.values()].filter(({ held }) => held === what).length;
    const [chats, copies, places] = [count("chat"), count("copy"), count("place")];
    const held = [
      ...(chats > 0 ? [`the folders of ${chats} ${chats === 1 ? "chat" : "chats"}`] : []),
      ...(copies > 0 ? [copies === 1 ? "the copy of 1 thread" : `the copies of ${copies} threads`] : []),
      ...(places > 0 ? [`${places} for the histories of project folders, which take two each`] : []),
    ];
    const last = held.pop();
    return `it holds as many folders as it has room for, ${MAX_SHARES}: ${held.length > 0 ? `${held.join(", ")}, and ${last}` : last}`;
  }

  // A power cut: surogate-vm stops the VM at once (VZVirtualMachine.stop(completionHandler:)), and is killed
  // KILL_MS later if it has not exited. Its pidfile stays for the next boot's sweep, or the manager's stop.
  kill(): Promise<void> {
    this.killed ??= (async () => {
      this.control.destroy();
      this.net.destroy();
      this.inbound.destroy();
      this.helper.send({ type: "kill" });
      if ((await Promise.race([this.helper.gone, late(performance.now() + KILL_MS)])) === "late") this.helper.child.kill("SIGKILL");
      await this.helper.gone;
    })();
    return this.killed;
  }
}

const key = (share: Share) => `${share.tag}/${share.kind === "vz" ? share.name ?? "" : ""}`;
