// For the tests alone, never in a packaged app: a stand-in for Hyper-V's devices, under QEMU and KVM on Linux,
// that runs the guest's Windows path as a Windows host's VM gives it: its kernel's line names SCSI's disks, vsock's
// ports and Windows's names (vm/qemu.ts, kernelLine); its disks are SCSI's LUNs 0 to 2 (virtio-scsi), its ports
// vsock streams to the host (vhost-vsock), its console the UART, and its folders Plan 9 shares on vsock's port 564;
// and it boots with the image's initrd. Hyper-V's own drivers, which only Windows runs, are QEMU's in their place:
// the initrd's list of modules loads QEMU's vsock device's instead of Hyper-V's sockets', storvsc's disks are
// virtio-scsi's, built in, and the balloon is virtio's. The host's ends: socat from each vsock port to a Unix
// socket the VM manager reaches as QEMU's, and a Plan 9 server of the test's (p9-server.mjs).
//
// One difference in the guest besides: Linux keeps the vsock ports below 1024 for a process that may bind
// privileged ports, which this one may not, so the Plan 9 server's port is STANDIN_PLAN9_PORT in place of 564.
// The agent disk the guest boots is the one it is given, with that one number changed in the agent's protocol.
//
// SUROGATE_VM_TESTS=1 SUROGATE_VM_STANDIN=hyperv runs the VM suite on it (setup.ts), one VM at a time, since
// a host's vsock ports are the whole host's: vitest run --no-file-parallelism test/vm. It needs the image's
// guest-standin target beside the image (standin.sh), socat, and /dev/vhost-vsock opened to this user as
// /dev/kvm is.

import { type ChildProcess, spawn, spawnSync } from "node:child_process";
import { randomInt } from "node:crypto";
import {
  accessSync, closeSync, constants, copyFileSync, existsSync, ftruncateSync, mkdirSync, mkdtempSync, openSync, readFileSync, rmSync, writeFileSync,
} from "node:fs";
import { Duplex, PassThrough } from "node:stream";
import { dirname, join } from "node:path";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";

import { MAX_SHARES, PLAN9_PORT, type Share, VSOCK_PORTS } from "../../../src/guest/protocol.js";
import type { BootVm, Emulated, VmBackend, VmOptions } from "../../../src/vm/manager.js";
import { QEMU_ARCH } from "../../../src/vm/qemu-arch.js";
import { guestCpus, kernelLine } from "../../../src/vm/qemu.js";
import { ended, reach } from "../../../src/vm/sockets.js";

const SERVER = fileURLToPath(new URL("./p9-server.mjs", import.meta.url));
// QEMU's vsock device's modules, in the initrd's list in Hyper-V's sockets' place: the image's guest-standin target.
export const STANDIN_MODULES = ["vsock", "vmw_vsock_virtio_transport_common", "vmw_vsock_virtio_transport"];
const SESSIONS_BYTES = 32 * 1024 ** 3;
// The Plan 9 server's port on the stand-in's host, one an unprivileged process binds.
export const STANDIN_PLAN9_PORT = 5564;

/** *agentDisk*, copied into *run*, its agent's Plan 9 port STANDIN_PLAN9_PORT in place of PLAN9_PORT; where the copy is. */
export function standinAgentDisk(agentDisk: string, run: string): string {
  const copy = join(run, "agent.img");
  copyFileSync(agentDisk, copy);
  const protocol = join(run, "protocol.js");
  const debugfs = (write: boolean, command: string) => {
    const done = spawnSync("debugfs", [...(write ? ["-w"] : []), copy, "-R", command], { encoding: "utf8" });
    if (done.status !== 0) throw new Error(`debugfs failed: ${done.stderr}`);
  };
  debugfs(false, `dump /guest/protocol.js ${protocol}`);
  const text = readFileSync(protocol, "utf8");
  const port = `export const PLAN9_PORT = ${PLAN9_PORT};`;
  if (text.split(port).length !== 2) throw new Error(`the agent disk's guest/protocol.js has no ${port}`);
  writeFileSync(protocol, text.replace(port, `export const PLAN9_PORT = ${STANDIN_PLAN9_PORT};`));
  debugfs(true, "rm /guest/protocol.js");
  debugfs(true, `write ${protocol} /guest/protocol.js`);
  return copy;
}

// The guest's kernel line on Hyper-V.
export const HYPERV_LINE = kernelLine({ disks: "scsi", transport: "vsock", names: "windows" });

/**
 * QEMU's command line for the stand-in: a guest of CID *cid*, its *initrd*, its console the UART into *console*; on
 * QEMU's TCG where *emulated*, as Linux's backend runs one, and the agent told so on the kernel's line.
 */
export function standinArgs(
  options: Pick<VmOptions, "kernel" | "rootfs" | "agentDisk" | "sessions">, initrd: string, run: string, console: string, cid: number, cpus = guestCpus(), emulated = false,
): string[] {
  const disk = (id: string, file: string, lun: number, rest: string) => [
    "-drive", `if=none,id=${id},file=${file.replaceAll(",", ",,")},format=raw,${rest}`, "-device", `scsi-hd,drive=${id},bus=scsi.0,channel=0,scsi-id=0,lun=${lun}`,
  ];
  const { machine, cpu } = QEMU_ARCH.x86_64;
  return [
    "-nodefaults", "-no-user-config", "-display", "none", "-no-reboot",
    ...machine(!emulated), ...cpu(!emulated), "-smp", String(cpus), "-m", "2G",
    "-object", "memory-backend-memfd,id=mem,size=2G,share=on",
    "-kernel", options.kernel, "-initrd", initrd, "-append", `${HYPERV_LINE}${emulated ? " surogate.emulated=1" : ""}`,
    "-device", "virtio-scsi-pci,id=scsi",
    ...disk("root", options.rootfs, 0, "readonly=on"), ...disk("agent", options.agentDisk, 1, "readonly=on"), ...disk("sessions", options.sessions, 2, "discard=unmap"),
    "-device", `vhost-vsock-pci,guest-cid=${cid}`,
    "-chardev", `file,id=console,path=${console.replaceAll(",", ",,")}`, "-serial", "chardev:console",
    "-device", "virtio-rng-pci", "-nic", "none",
    "-device", "virtio-balloon-pci,free-page-reporting=on",
    "-pidfile", join(run, "qemu.pid"),
    "-sandbox", "on,obsolete=deny,elevateprivileges=deny,spawn=deny,resourcecontrol=deny",
  ];
}

/**
 * The stand-in's initrd, in *run*: the image's, beside *kernel*, followed by a second archive the kernel unpacks over
 * it, whose /modules lists QEMU's vsock device's modules, from the image's guest-standin target in the standin folder of *image*.
 */
export function standinInitrd(image: string, run: string): string {
  const modules = join(image, "standin");
  const missing = STANDIN_MODULES.filter((name) => !existsSync(join(modules, `${name}.ko.zst`)));
  if (missing.length > 0) throw new Error(`the stand-in for Hyper-V's devices needs the image's guest-standin target in ${modules}: run test/vm/hyperv/standin.sh ${image}`);
  const stage = mkdtempSync(join(run, "initrd-"));
  try {
    mkdirSync(join(stage, "lib", "modules"), { recursive: true });
    for (const name of STANDIN_MODULES) {
      const unpacked = spawnSync("zstd", ["-q", "-dc", join(modules, `${name}.ko.zst`)], { maxBuffer: 64 * 1024 * 1024 });
      if (unpacked.status !== 0) throw new Error(`zstd could not unpack ${name}.ko.zst`);
      writeFileSync(join(stage, "lib", "modules", `${name}.ko`), unpacked.stdout);
    }
    writeFileSync(join(stage, "modules"), `${STANDIN_MODULES.join("\n")}\n`);
    const listed = ["lib", "lib/modules", ...STANDIN_MODULES.map((name) => `lib/modules/${name}.ko`), "modules"].join("\n");
    const archive = spawnSync("cpio", ["-o", "-H", "newc", "-R", "0:0", "--quiet"], { cwd: stage, input: `${listed}\n`, maxBuffer: 64 * 1024 * 1024 });
    if (archive.status !== 0) throw new Error(`cpio could not make the stand-in's initrd: ${archive.stderr}`);
    const initrd = join(run, "initrd");
    const first = spawnSync("cat", [join(image, "initrd")], { maxBuffer: 64 * 1024 * 1024 });
    if (first.status !== 0) throw new Error(`the image has no initrd at ${join(image, "initrd")}`);
    writeFileSync(initrd, Buffer.concat([first.stdout, archive.stdout]));
    return initrd;
  } finally {
    rmSync(stage, { recursive: true, force: true });
  }
}

// A child that dies with this process, and the end of what it said on stderr.
function launch(argv: string[]): { child: ChildProcess; said: () => string } {
  const child = spawn("/usr/bin/setpriv", ["--pdeathsig", "KILL", "--", ...argv], { stdio: ["pipe", "pipe", "pipe"] });
  let said = "";
  child.stderr?.on("data", (chunk: Buffer) => {
    said = (said + chunk.toString()).slice(-8000);
  });
  child.on("error", () => {});
  return { child, said: () => said.trim() };
}

const exited = (child: ChildProcess) => new Promise<void>((resolve) => {
  if (ended(child)) resolve();
  else child.once("exit", () => resolve());
});

// Settles once *child*'s stderr, or its stdout, says *line*, or rejects once it has exited, or at *deadline*.
function heard(child: ChildProcess, line: RegExp, deadline: number, said: () => string, out = false): Promise<void> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error(`did not start: ${said()}`)), Math.max(0, deadline - performance.now()));
    const look = (chunk: Buffer) => {
      if (line.test(chunk.toString())) {
        clearTimeout(timer);
        resolve();
      }
    };
    (out ? child.stdout : child.stderr)?.on("data", look);
    child.once("exit", () => {
      clearTimeout(timer);
      reject(new Error(`exited: ${said()}`));
    });
  });
}

// The last stand-in's QEMU, and the end of all of it: the next binds the host's vsock ports only once that has come.
let last: { qemu: ChildProcess; gone: Promise<void> } | null = null;

export const bootStandin: BootVm = async (options, signal, deadline) => {
  if (signal?.aborted) throw new Error("The boot was stopped");
  // One stopping is waited for, 10 s at most; one still running is another VM at once.
  if (last && !ended(last.qemu)) {
    await Promise.race([last.gone, new Promise((resolve) => setTimeout(resolve, Math.max(0, Math.min(10_000, deadline - performance.now()))).unref())]);
    if (!ended(last.qemu)) throw new Error("the stand-in for Hyper-V's devices runs one VM at a time: the host's vsock ports are the whole host's");
  }
  await last?.gone;
  try {
    accessSync("/dev/vhost-vsock", constants.R_OK | constants.W_OK);
  } catch {
    throw new Error("the stand-in for Hyper-V's devices needs /dev/vhost-vsock opened to this user");
  }
  // KVM where this user can open it, as Linux's backend takes it, and QEMU's TCG otherwise.
  let emulated: Emulated | null = null;
  try {
    accessSync(options.kvm ?? "/dev/kvm", constants.R_OK | constants.W_OK);
  } catch {
    emulated = "no-kvm";
  }
  rmSync(options.run, { recursive: true, force: true });
  mkdirSync(options.run, { recursive: true, mode: 0o700 });
  if (!existsSync(options.sessions)) {
    mkdirSync(dirname(options.sessions), { recursive: true, mode: 0o700 });
    const fd = openSync(options.sessions, "wx", 0o600);
    ftruncateSync(fd, SESSIONS_BYTES);
    closeSync(fd);
  }
  mkdirSync(dirname(options.console), { recursive: true, mode: 0o700 });
  const initrd = standinInitrd(dirname(options.kernel), options.run);
  const agentDisk = standinAgentDisk(options.agentDisk, options.run);
  const children: ChildProcess[] = [];
  const halt = () => {
    for (const child of children) child.kill("SIGKILL");
  };
  signal?.addEventListener("abort", halt, { once: true });
  try {
    // The host's ends, listening before the guest dials them: each of the agent's ports, a stream to its Unix
    // socket, and the Plan 9 server's port, a stream to the server's socket for each mount.
    // A port another test file's stand-in has not let go of yet is tried again, until the deadline.
    const listening = async (argv: string[]) => {
      for (;;) {
        const { child, said } = launch(argv);
        children.push(child);
        try {
          return await heard(child, /listening on/, deadline, said);
        } catch (error) {
          if (!/Address already in use/.test(said()) || performance.now() > deadline) throw error;
          await new Promise((resolve) => setTimeout(resolve, 100));
        }
      }
    };
    for (const [name, port] of Object.entries(VSOCK_PORTS)) {
      await listening(["socat", "-d", "-d", `VSOCK-LISTEN:${port}`, `UNIX-LISTEN:${join(options.run, `${name}.sock`)},unlink-early,mode=600`]);
    }
    const p9 = join(options.run, "p9.sock");
    const server = launch([process.execPath, SERVER, p9]);
    children.push(server.child);
    await heard(server.child, /"listening":true/, deadline, server.said, true);
    await listening(["socat", "-d", "-d", `VSOCK-LISTEN:${STANDIN_PLAN9_PORT},fork`, `UNIX-CONNECT:${p9}`]);
    const { child: qemu, said } = launch(["/usr/bin/prlimit", "--memlock=0:0", "--", QEMU_ARCH.x86_64.program, ...standinArgs({ ...options, agentDisk }, initrd, options.run, options.console, randomInt(1000, 2 ** 31), options.cpus, emulated !== null)]);
    children.unshift(qemu);
    // The guest dials its ports as it boots: each is there to the manager at once, as QEMU's are, and connects once the guest has.
    const [control, net, inbound] = Object.keys(VSOCK_PORTS).map((name) => dialed(join(options.run, `${name}.sock`), qemu)) as [Duplex, Duplex, Duplex];
    const vm = new StandinVm(qemu, said, children, server.child, control, net, inbound, emulated);
    last = { qemu, gone: vm.exited.then(() => Promise.all(children.map(exited))).then(() => {}) };
    return vm;
  } catch (error) {
    halt();
    const gone = Promise.all(children.map(exited)).then(() => {});
    last = children[0] ? { qemu: children[0], gone } : null;
    await gone;
    throw error;
  } finally {
    signal?.removeEventListener("abort", halt);
  }
};

// The stand-in's end of a port of the guest's, at *path*, which socat makes once the guest has dialed it: a stream at
// once, its bytes kept until then, and ended when QEMU exits first.
function dialed(path: string, qemu: ChildProcess): Duplex {
  const toGuest = new PassThrough();
  const fromGuest = new PassThrough();
  const port = Duplex.from({ readable: fromGuest, writable: toGuest });
  void reach(path, qemu, Infinity).then((socket) => {
    if (!socket) return void fromGuest.end();
    socket.on("error", () => {});
    toGuest.pipe(socket);
    socket.pipe(fromGuest);
    port.once("close", () => socket.destroy());
  });
  return port;
}

class StandinVm implements VmBackend {
  readonly exited: Promise<string>;
  private made = 0;
  // Each share in the guest, by its tag, with what it holds, as Linux's backend counts them.
  private readonly held = new Map<string, "chat" | "copy" | "place">();
  private readonly answers = new Map<string, () => void>();
  private killed: Promise<void> | null = null;

  constructor(
    private readonly qemu: ChildProcess,
    said: () => string,
    private readonly children: ChildProcess[],
    private readonly server: ChildProcess,
    readonly control: Duplex,
    readonly net: Duplex,
    readonly inbound: Duplex,
    readonly emulated: Emulated | null,
  ) {
    this.exited = exited(qemu).then(() => {
      for (const child of children) child.kill("SIGKILL");
      return said();
    });
    createInterface({ input: server.stdout! }).on("line", (line) => {
      const answer = JSON.parse(line) as { added?: string; removed?: string };
      const key = answer.added ? `added ${answer.added}` : `removed ${answer.removed}`;
      this.answers.get(key)?.();
      this.answers.delete(key);
    });
  }

  private ask(asked: Record<string, unknown>, key: string, deadline: number): Promise<void> {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("the Plan 9 server did not answer")), Math.max(0, deadline - performance.now()));
      this.answers.set(key, () => {
        clearTimeout(timer);
        resolve();
      });
      this.server.stdin!.write(`${JSON.stringify(asked)}\n`);
    });
  }

  async share(folder: string, uid: number, deadline: number, { readonly = false, copy = false } = {}): Promise<Share> {
    if (this.killed || ended(this.qemu)) throw new Error("the VM has gone");
    if (this.held.size >= MAX_SHARES) throw new Error(this.full());
    const tag = `r${(this.made += 1)}`;
    this.held.set(tag, uid === 0 ? "place" : copy ? "copy" : "chat");
    await this.ask({ add: tag, path: folder, readOnly: readonly }, `added ${tag}`, deadline);
    return { kind: "p9", tag, port: STANDIN_PLAN9_PORT, aname: `uid=${uid};gid=${uid}` };
  }

  async unshare(share: Share, deadline: number): Promise<void> {
    if (!this.held.has(share.tag)) return;
    await this.ask({ remove: share.tag }, `removed ${share.tag}`, deadline);
    this.held.delete(share.tag);
  }

  // Why no other folder is shared, in Linux's backend's words.
  private full(): string {
    const count = (what: string) => [...this.held.values()].filter((held) => held === what).length;
    const [chats, copies, places] = [count("chat"), count("copy"), count("place")];
    const held = [
      ...(chats > 0 ? [`the folders of ${chats} ${chats === 1 ? "chat" : "chats"}`] : []),
      ...(copies > 0 ? [copies === 1 ? "the copy of 1 thread" : `the copies of ${copies} threads`] : []),
      ...(places > 0 ? [`${places} for the histories of project folders, which take two each`] : []),
    ];
    const last = held.pop();
    return `it holds as many folders as it has room for, ${MAX_SHARES}: ${held.length > 0 ? `${held.join(", ")}, and ${last}` : last}`;
  }

  kill(): Promise<void> {
    this.killed ??= (async () => {
      for (const socket of [this.control, this.net, this.inbound]) socket.destroy();
      for (const child of this.children) child.kill("SIGKILL");
      await Promise.all(this.children.map(exited));
    })();
    return this.killed;
  }
}
