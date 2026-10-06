// A minimal launch of the guest for the VM tests: QEMU with Section 11's command
// line, less what later plans add (the net port, QMP, hot-add root ports), one
// virtiofsd sharing one folder at boot as r1, and a client for the control port.
// The app's VM manager replaces it. Both QEMU and virtiofsd die with the test
// process (setpriv --pdeathsig).

import { type ChildProcess, spawn, spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import { connect, type Socket } from "node:net";
import { userInfo } from "node:os";
import { join } from "node:path";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";

import type { FromAgent, HostUser, ToAgent } from "../../src/guest/protocol.js";

export const IMAGE = process.env.SUROGATE_VM_IMAGE ?? fileURLToPath(new URL("../../../images/guest/out", import.meta.url));
const AGENT_DISK = fileURLToPath(new URL("../../vm/agent-disk.sh", import.meta.url));
const VIRTIOFSD = "/usr/libexec/virtiofsd";
const HELLO_MS = 15_000;

// The agent disk from the built agent (npm run build), into *dir*.
export function agentDisk(dir: string): string {
  const image = join(dir, "agent.img");
  const made = spawnSync(AGENT_DISK, [image], { encoding: "utf8" });
  if (made.status !== 0) throw new Error(`agent-disk.sh failed: ${made.error?.message ?? made.stderr}`);
  return image;
}

// A sparse sessions disk, which the guest formats at its first boot.
export function sessionsDisk(dir: string): string {
  const image = join(dir, "sessions.img");
  spawnSync("truncate", ["-s", "32G", image]);
  return image;
}

export interface Guest {
  // When QEMU was launched (performance.now()), and how long after it the agent said hello.
  launched: number;
  helloMs: number;
  // The host user the guest was told about.
  user: HostUser;
  // Sends *message* with the next id, and resolves the agent's answer to it.
  request(message: { type: string; [field: string]: unknown }): Promise<FromAgent>;
  console(): string;
  stop(): Promise<void>;
}

export interface BootOptions {
  agent: string; // agent.img
  sessions: string; // sessions.img
  folder: string; // shared at boot as r1
  uid: number; // the guest uid the host user maps to in r1
  name?: string; // the host user's name the guest is told, if not this user's
}

const exited = (child: ChildProcess) => new Promise<void>((resolve) => {
  if (child.exitCode !== null || child.signalCode !== null) resolve();
  else child.once("exit", () => resolve());
});

export async function boot({ agent, sessions, folder, uid, name }: BootOptions): Promise<Guest> {
  // Under $XDG_RUNTIME_DIR: a vhost-user socket's path must fit in 108 bytes.
  const run = mkdtempSync(join(process.env.XDG_RUNTIME_DIR ?? "/tmp", "sg-vm-"));
  const host = userInfo();
  const user: HostUser = { uid: host.uid, gid: host.gid, name: name ?? host.username, home: host.homedir };
  const begun = performance.now();
  const children: ChildProcess[] = [];
  const dying = (argv: string[], log: string) => {
    const child = spawn("setpriv", ["--pdeathsig", "KILL", "--", ...argv], { stdio: ["ignore", "ignore", "pipe"] });
    let said = "";
    child.stderr?.on("data", (chunk: Buffer) => {
      said = (said + chunk.toString()).slice(-4000);
    });
    child.once("exit", (code, signal) => {
      if (code) console.error(`${log} exited ${code ?? signal}: ${said}`);
    });
    children.push(child);
    return child;
  };
  const share = join(run, "vfs-1.sock");
  dying([
    VIRTIOFSD, `--shared-dir=${folder}`, `--socket-path=${share}`, "--sandbox=namespace", "--cache=auto",
    `--uid-map=:${uid}:${host.uid}:1:`, `--gid-map=:${uid}:${host.gid}:1:`,
  ], "virtiofsd");
  while (!existsSync(share)) await new Promise((resolve) => setTimeout(resolve, 5));
  const control = join(run, "control.sock");
  const qemu = dying([
    "qemu-system-x86_64", "-nodefaults", "-no-user-config", "-display", "none", "-no-reboot",
    "-machine", "q35,accel=kvm,memory-backend=mem", "-cpu", "host", "-smp", "4", "-m", "2G",
    "-object", "memory-backend-memfd,id=mem,size=2G,share=on",
    "-kernel", join(IMAGE, "vmlinuz"),
    "-append", "root=/dev/vda rootfstype=ext4 ro init=/usr/sbin/surogate-init console=hvc0 panic=-1 quiet",
    "-drive", `if=none,id=root,file=${join(IMAGE, "rootfs.img")},format=raw,readonly=on`, "-device", "virtio-blk-pci,drive=root",
    "-drive", `if=none,id=agent,file=${agent},format=raw,readonly=on`, "-device", "virtio-blk-pci,drive=agent",
    "-drive", `if=none,id=sessions,file=${sessions},format=raw,discard=unmap`, "-device", "virtio-blk-pci,drive=sessions",
    "-device", "virtio-serial-pci",
    "-chardev", `socket,id=control,path=${control},server=on,wait=off`,
    "-device", "virtserialport,chardev=control,name=ai.surogate.control",
    "-chardev", `file,id=console,path=${join(run, "console.log")}`, "-device", "virtconsole,chardev=console",
    "-chardev", `socket,id=vfs1,path=${share}`, "-device", "vhost-user-fs-pci,chardev=vfs1,tag=r1",
    "-device", "virtio-rng-pci", "-nic", "none",
    "-sandbox", "on,obsolete=deny,elevateprivileges=deny,spawn=deny,resourcecontrol=deny",
  ], "qemu");
  const stop = async () => {
    for (const child of children) child.kill("SIGKILL");
    await Promise.all(children.map(exited));
    rmSync(run, { recursive: true, force: true });
  };
  const consoleLog = () => {
    try {
      return readFileSync(join(run, "console.log"), "utf8");
    } catch {
      return "";
    }
  };
  let socket: Socket;
  try {
    socket = await new Promise<Socket>((resolve, reject) => {
      const deadline = begun + HELLO_MS;
      const attempt = () => {
        if (qemu.exitCode !== null || qemu.signalCode !== null) return reject(new Error("QEMU exited"));
        const client = connect(control);
        client.once("connect", () => resolve(client));
        client.once("error", () => (performance.now() > deadline ? reject(new Error("no control socket")) : setTimeout(attempt, 5)));
      };
      attempt();
    });
  } catch (error) {
    const said = consoleLog();
    await stop();
    throw new Error(`${(error as Error).message}\n${said}`);
  }
  const waiting = new Map<number, (message: FromAgent) => void>();
  let hello: (message: FromAgent) => void = () => {};
  const greeted = new Promise<FromAgent>((resolve) => {
    hello = resolve;
  });
  createInterface({ input: socket }).on("line", (line) => {
    const message = JSON.parse(line) as FromAgent;
    if (message.type === "hello") hello(message);
    else waiting.get(message.id)?.(message);
  });
  const timeout = new Promise<never>((_resolve, reject) => {
    setTimeout(() => reject(new Error("no hello")), Math.max(0, begun + HELLO_MS - performance.now())).unref();
  });
  try {
    await Promise.race([greeted, timeout, exited(qemu).then(() => Promise.reject(new Error("QEMU exited")))]);
  } catch (error) {
    socket.destroy();
    await new Promise((resolve) => setTimeout(resolve, 200));
    const said = consoleLog();
    await stop();
    throw new Error(`${(error as Error).message}\n${said}`);
  }
  const helloMs = performance.now() - begun;
  const send = (message: ToAgent) => socket.write(`${JSON.stringify(message)}\n`);
  send({ type: "done", id: 0, user });
  let next = 1;
  return {
    launched: begun,
    helloMs,
    user,
    request: (message) => new Promise((resolve) => {
      const id = next++;
      waiting.set(id, (answer) => {
        waiting.delete(id);
        resolve(answer);
      });
      send({ ...message, id } as ToAgent);
    }),
    console: consoleLog,
    stop: async () => {
      socket.destroy();
      await stop();
    },
  };
}
