// The VM layer's Windows backend (spec, Section 11): the guest on Windows's Host Compute Service, through two
// programs. surogate-service.exe (native/windows/service), LocalSystem, makes every HCS call; surogate-vm.exe
// (native/windows/vm), as the user, asks it for the user's VM, binds the guest's three ports on Hyper-V sockets and
// serves each as a named pipe only this user may open. win32.ts drives surogate-vm.exe over the VM helper's line
// protocol (vm-helper.ts), the same protocol macOS's surogate-vm speaks, and reaches the guest's ports through those
// pipes with reach(), as the Linux backend reaches QEMU's sockets and macOS's its surogate-vm's.
//
// Each folder is a Plan 9 share of HCS's own server (guest/protocol.ts, the p9 kind): its uid is applied at the
// guest's mount, in the share's aname, so each root's files are its own, as on Linux. Whose rights the server serves
// a folder with is an open question answered only on a PC: until a standard account has passed that check, no folder is shared, and
// a 9P server of the app's own is built instead. surogate-vm.exe stops the VM when its stdin ends; the service ends it
// besides when surogate-vm.exe, the VM manager or the service itself goes.

import { createHash, randomBytes } from "node:crypto";
import type { Socket } from "node:net";
import { fileURLToPath } from "node:url";

import { execFile } from "node:child_process";
import { mkdirSync } from "node:fs";

import { spawnClean } from "../clean-child.js";
import type { HostUser, Share } from "../guest/protocol.js";
import { MAX_SHARES, PLAN9_PORT } from "../guest/protocol.js";
import { basename, dirname, join } from "../host-path.js";
import type { Addresses } from "./backends.js";
import type { BootVm, VmBackend } from "./manager.js";
import { guestCpus, kernelLine } from "./qemu.js";
import { ended, reach } from "./sockets.js";
import { Helper, late, type Said, type Start } from "./vm-helper.js";

// How long surogate-vm.exe has after its kill line to end the VM and exit, before it is killed.
const KILL_MS = 1_000;

/**
 * The service's words for a boot of a data folder whose last VM it has not ended yet (native/windows/service, core.rs):
 * a VM it still makes or ends in a call of HCS's that the boot did not wait for, as a boot that timed out leaves, or one
 * stopped just before. The service ends that VM, and lets the data folder go, once HCS's call returns.
 */
export const ENDING = "a VM runs for this data folder already";
// How long a boot so refused waits before it is asked again.
const AGAIN_MS = 250;

// surogate-vm.exe is given the environment the system needs and nothing of the user's, as QEMU is on Linux.
const ENV = { SystemRoot: process.env.SystemRoot ?? "C:\\Windows" };

// The same from src/vm and from dist/vm: desktop/, or a packaged app's resources\app.
const PACKAGE = fileURLToPath(new URL("../..", import.meta.url));
/** surogate-vm.exe: beside the app's binaries in a packaged app, the Cargo build's in development. */
export const VM_PROGRAM = join(PACKAGE, "native", "windows", "target", "release", "surogate-vm.exe");

// The guest's runtime folder's own 8 hex, by which this boot's pipes are named apart (places-win32, decision 20).
const mark = (run: string): string => {
  const name = basename(run);
  return name.startsWith("vm-") ? name.slice(3) : createHash("sha256").update(run).digest("hex").slice(0, 8);
};

/**
 * Where the VM manager reaches the guest's three ports, and where the browser's door is, for the runtime folder *run*:
 * named pipes only this user may open, which reach() and Node's net take by name. The backend's tests drive it against
 * the fake over Unix sockets on Linux by giving win32Boot their own address maker (test/vm/win32).
 *
 * The door's name here is a fixed one, for tests and the measurements; the app's door name is a fresh random one made
 * at each start (freshDoorWin32), so no other local user can work it out and listen on it first.
 */
export function addressesWin32(run: string): Addresses {
  const pipe = (port: string) => `\\\\.\\pipe\\surogate-${mark(run)}-${port}`;
  return { control: pipe("control"), net: pipe("net"), inbound: pipe("inbound"), door: pipe("door") };
}

/**
 * A fresh door pipe name, 128 random bits, for one start of the app. Pipe names are global and the data folder's
 * are derivable, so the door -- which the browser's proxy hands its key to -- must be a name no other local user can
 * guess and so cannot create or listen on first. Made just before the door's listen and told to the browser as the
 * door address is now. Nothing but randomness is in the name.
 */
export function freshDoorWin32(): string {
  return `\\\\.\\pipe\\surogate-door-${randomBytes(16).toString("hex")}`;
}

/** The VM's runtime folder for *dataDir*: %LOCALAPPDATA%\Surogate\run\vm-<8 hex>, this user's own, one per data folder. */
export function runtimeFolderWin32(env: NodeJS.ProcessEnv, dataDir: string): string {
  const local = env.LOCALAPPDATA ?? join(env.USERPROFILE ?? "C:\\Users\\Default", "AppData", "Local");
  return join(local, "Surogate", "run", `vm-${createHash("sha256").update(dataDir).digest("hex").slice(0, 8)}`);
}

/** The guest kernel's command line on Windows: Hyper-V's SCSI disks and vsock ports, and the names its rule refuses. */
export const commandLine = (): string => kernelLine({ disks: "scsi", transport: "vsock", names: "windows" });

/**
 * A boot on Windows, as *start* starts surogate-vm.exe: spawnClean in the app. surogate-vm.exe makes the three pipes
 * and says "listening"; the manager connects to them; it sends boot, and surogate-vm.exe asks the service for the VM,
 * binds the ports and answers "started". One the service refuses while it still ends the data folder's last VM is
 * asked again, by a new surogate-vm.exe, until the boot's deadline, and then says so.
 */
export function win32Boot(start: Start, addresses: (run: string) => Addresses = addressesWin32): BootVm {
  const once = bootOnce(start, addresses);
  return async (options, signal, deadline) => {
    for (;;) {
      try {
        return await once(options, signal, deadline);
      } catch (error) {
        const why = (error as Error).message;
        if (!why.includes(ENDING) || signal?.aborted) throw error;
        if (performance.now() + AGAIN_MS >= deadline) throw new Error(`the data folder's last VM had not ended by the boot's deadline: ${why}`);
        await late(performance.now() + AGAIN_MS);
      }
    }
  };
}

// One surogate-vm.exe's boot.
function bootOnce(start: Start, addresses: (run: string) => Addresses): BootVm {
  return async (options, signal, deadline) => {
    if (signal?.aborted) throw new Error("The boot was stopped");
    // The folders surogate-vm.exe makes the sessions disk in and opens the console log in, as Linux's and macOS's
    // backends make them: it makes neither folder itself.
    mkdirSync(dirname(options.sessions), { recursive: true });
    mkdirSync(dirname(options.console), { recursive: true });
    const program = options.program ?? VM_PROGRAM;
    const { control, net, inbound } = addresses(options.run);
    const data = options.data ?? dirname(dirname(options.sessions));
    const helper = new Helper(start, program, ["run", options.run, "--data", data, "--control", control, "--net", net, "--inbound", inbound], ENV);
    const halt = () => helper.child.kill("SIGKILL");
    signal?.addEventListener("abort", halt, { once: true });
    const connected: Socket[] = [];
    try {
      const listening = await helper.first((said) => said.type === "listening" || said.type === "failed", deadline);
      if (listening === "late") throw new Error("surogate-vm did not open its pipes");
      if (listening === "exited" || listening.type !== "listening") throw new Error(helper.words() || "surogate-vm exited");
      for (const path of [control, net, inbound]) {
        const socket = await reach(path, helper.child, deadline);
        if (!socket) throw new Error(ended(helper.child) ? "surogate-vm exited" : "surogate-vm did not take a connection to its pipes");
        connected.push(socket);
      }
      helper.send({
        type: "boot", kernel: options.kernel, initrd: join(dirname(options.kernel), "initrd"), rootfs: options.rootfs, agentDisk: options.agentDisk,
        sessions: options.sessions, console: options.console, cpus: options.cpus ?? guestCpus(), commandLine: commandLine(),
      });
      const started = await helper.first((said) => said.type === "started" || said.type === "failed", deadline);
      if (started === "late") throw new Error("the VM did not start in time");
      if (started === "exited" || started.type !== "started") throw new Error(helper.words() || "surogate-vm exited");
      const [controlSocket, netSocket, inboundSocket] = connected as [Socket, Socket, Socket];
      return new Win32Vm(helper, controlSocket, netSocket, inboundSocket);
    } catch (error) {
      for (const socket of connected) socket.destroy();
      helper.child.kill("SIGKILL");
      await helper.gone;
      const said = helper.words();
      throw new Error([(error as Error).message, said === (error as Error).message ? "" : said].filter(Boolean).join(": "));
    } finally {
      signal?.removeEventListener("abort", halt);
    }
  };
}

/** The boot of the app on Windows. */
export const bootWin32: BootVm = win32Boot(spawnClean);

/** What this computer has of what its VM runs on, as surogate-vm check reports it (native/windows/vm, check.rs). */
export interface Lacks {
  virtualMachinePlatform: boolean;
  service: "present" | "missing";
  hostCompute: "ok" | "old" | "absent";
}

/** surogate-vm check's answer, or the tools missing where it does not run. */
export function checkWin32(program = VM_PROGRAM, ms = 5_000): Promise<Lacks> {
  return new Promise((resolve) => {
    execFile(program, ["check"], { timeout: ms, killSignal: "SIGKILL", env: ENV }, (error, stdout) => {
      try {
        const answer = JSON.parse(stdout) as Partial<Lacks>;
        if (typeof answer.virtualMachinePlatform === "boolean" && (answer.service === "present" || answer.service === "missing")) {
          resolve({ virtualMachinePlatform: answer.virtualMachinePlatform, service: answer.service, hostCompute: answer.hostCompute ?? "absent" });
          return;
        }
      } catch {
        // Not its answer.
      }
      resolve({ virtualMachinePlatform: false, service: "missing", hostCompute: "absent" });
      void error;
    });
  });
}

/**
 * What this computer lacks of what its VM runs on, in Section 11's words, from *probe* (surogate-vm check). The
 * sandbox needs no tool on the PATH on Windows: the image is unpacked with Node's own zstd, and the file host is the
 * file tools' own. Whether this computer's processor has virtualization at all is HCS's own answer at the
 * first boot, HCS_E_HYPERV_NOT_INSTALLED, which the boot surfaces, not a requirement read up front.
 */
export async function requirementsWin32(probe: () => Promise<Lacks> = () => checkWin32()): Promise<string[]> {
  const lacks = await probe();
  if (lacks.service === "missing") {
    return ["Surogate's sandbox tools are missing. Install Surogate again"];
  }
  if (!lacks.virtualMachinePlatform) {
    return ["Windows's Virtual Machine Platform is off. An administrator turns it on from Surogate's Settings, and Windows restarts."];
  }
  if (lacks.hostCompute === "old") {
    return ["This Windows is too old to run Surogate's sandbox. Update to a serviced Windows 11."];
  }
  return [];
}

// One surogate-vm.exe, its VM, and the folders shared into it.
class Win32Vm implements VmBackend {
  readonly exited: Promise<string>;
  readonly emulated = null;
  // HCS's Plan 9 server is given each share's folder by its path (a Plan9Share, native/windows/hcs), and Microsoft's
  // documentation does not say when the server opens it: as it is shared, at the guest's attach, or at each.
  readonly opensByPath = true;
  // Each share in the guest, by its tag; each tag, r<n>, is never given again in this VM.
  private readonly shares = new Map<string, { held: "chat" | "copy" | "place"; removal: Promise<void> | null }>();
  private made = 0;
  private asked = 0;
  private killed: Promise<void> | null = null;

  constructor(
    private readonly helper: Helper,
    readonly control: Socket,
    readonly net: Socket,
    readonly inbound: Socket,
  ) {
    this.exited = helper.gone.then(() => helper.words());
  }

  // surogate-vm.exe's answer to a line of *id*, by *deadline*; one not given in time ends the VM, as one that cannot share any more.
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
   * *folder* shared into the running guest, by *deadline*, as a Plan 9 share of HCS's server. The share's name, r<n>,
   * is the server's AccessName; the uid is applied at the guest's mount, in its aname, so the root's files show as
   * *uid*, each root's its own; 0 is the guest's root's, for a folder's place. *readonly*: the place's real folder,
   * which the server keeps read-only.
   */
  async share(folder: string, uid: number, deadline: number, { readonly = false, copy = false } = {}): Promise<Share> {
    if (this.killed) throw new Error("the VM has gone");
    if (this.shares.size >= MAX_SHARES) throw new Error(this.full());
    const name = `r${(this.made += 1)}`;
    const said = await this.answer({ type: "share", id: (this.asked += 1), name, path: folder, readOnly: readonly }, deadline);
    if (said.type !== "shared") throw new Error(said.type === "refused" ? `the folder could not be shared: ${said.why}` : "surogate-vm answered no share");
    const share: Share = { kind: "p9", tag: said.tag, port: PLAN9_PORT, aname: `uid=${uid};gid=${uid}` };
    this.shares.set(share.tag, { held: uid === 0 ? "place" : copy ? "copy" : "chat", removal: null });
    return share;
  }

  /**
   * *share* out of the running guest, by *deadline*: surogate-vm.exe asks the service to remove the Plan 9 share. One
   * it does not let go of in time, or refuses, ends the VM: the share would hold its place, half-removed.
   */
  unshare(share: Share, deadline: number): Promise<void> {
    const shared = this.shares.get(share.tag);
    if (!shared) return Promise.resolve();
    shared.removal ??= this.remove(share, deadline);
    return shared.removal;
  }

  private async remove(share: Share, deadline: number): Promise<void> {
    const said = await this.answer({ type: "unshare", id: (this.asked += 1), name: share.tag }, deadline);
    if (said.type !== "unshared") {
      await this.kill();
      throw new Error(said.type === "refused" ? `the folder could not be let go: ${said.why}` : "surogate-vm let no folder go");
    }
    this.shares.delete(share.tag);
  }

  // Why no other folder is shared: what holds each place, as on Linux and macOS.
  private full(): string {
    const count = (what: string) => [...this.shares.values()].filter(({ held }) => held === what).length;
    const [chats, copies, places] = [count("chat"), count("copy"), count("place")];
    const held = [
      ...(chats > 0 ? [`the folders of ${chats} ${chats === 1 ? "chat" : "chats"}`] : []),
      ...(copies > 0 ? [copies === 1 ? "the copy of 1 thread" : `the copies of ${copies} threads`] : []),
      ...(places > 0 ? [`${places} for the histories of project folders, which take two each`] : []),
    ];
    const last = held.pop();
    return `it holds as many folders as it has room for, ${MAX_SHARES}: ${held.length > 0 ? `${held.join(", ")}, and ${last}` : last}`;
  }

  // A power cut: surogate-vm.exe asks the service to stop the VM at once, and is killed KILL_MS later if it has not exited.
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
