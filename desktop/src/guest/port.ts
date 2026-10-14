// A port in the guest, one of the agent's links to the host (spec, Section 11, Transport): the control
// port, the net port and the inbound port, on the transport the kernel's command line names (vm/qemu.ts,
// kernelLine). On virtio-serial each is a character device, read and written through the file system, and
// found by the name QEMU gave it, as there is no udev in the guest. On vsock (Hyper-V's sockets), Node
// having no vsock, each is a stream surogate-vsock connected to the host and relays to a Unix socket of
// its own, root's alone, which the agent opens by its path (vm/init): so no descriptor of a port's is ever
// one that what the agent runs inherits.

import { open, readdir, readFile } from "node:fs/promises";
import { connect } from "node:net";
import { Duplex, Readable, Writable } from "node:stream";

import type { Transport } from "./protocol.js";

const PORTS = "/run/surogate/ports";
const VIRTIO_PORTS = "/sys/class/virtio-ports";
// While the host is not connected the port reads as ended: it is read again after this.
const RETRY_MS = 20;
// How long the relay's socket for a port has to be there: it is made as the agent starts.
const RELAY_MS = 10_000;

/** The transport the kernel's command line, *cmdline*, names in surogate.transport=. Throws on none, two, or another word. */
export function transportOf(cmdline: string): Transport {
  const named = cmdline.split(/\s+/).filter((word) => word.startsWith("surogate.transport=")).map((word) => word.slice("surogate.transport=".length));
  if (named.length !== 1) throw new Error(`the kernel's command line names surogate.transport= ${named.length === 0 ? "nowhere" : "twice"}`);
  const [transport] = named;
  if (transport !== "virtio-serial" && transport !== "vsock") throw new Error(`surogate.transport=${transport?.slice(0, 32)} is not a transport this guest knows`);
  return transport;
}

/**
 * The port named *name* on *transport*, as a byte stream: what the host writes, and writes in the order made.
 * A virtio-serial port waits while the host is not connected, and never ends. A vsock port ends when the host
 * has gone. *folder*: where vsock's relayed sockets are, and *ports*, virtio-serial's ports in sysfs.
 */
export async function openPortOn(transport: Transport, name: string, folder = PORTS, ports = VIRTIO_PORTS): Promise<Duplex> {
  return transport === "vsock" ? relayedPort(`${folder}/${name}`) : openPort(await findPort(name, ports));
}

// A console port has no name.
export async function findPort(name: string, ports = VIRTIO_PORTS): Promise<string> {
  for (const port of await readdir(ports)) {
    const named = await readFile(`${ports}/${port}/name`, "utf8").catch(() => "");
    if (named.trim() === name) return `/dev/${port}`;
  }
  throw new Error(`no virtio port named ${name}`);
}

// The relay's socket at *path*, connected once it is there, within RELAY_MS.
export async function relayedPort(path: string, ms = RELAY_MS): Promise<Duplex> {
  for (const deadline = performance.now() + ms; ;) {
    const socket = await new Promise<Duplex | null>((resolve) => {
      const tried = connect(path);
      tried.once("connect", () => resolve(tried));
      tried.once("error", () => resolve(null));
    });
    if (socket) return socket;
    if (performance.now() > deadline) throw new Error(`no port at ${path}: the host's vsock port was not relayed`);
    await new Promise((resolve) => setTimeout(resolve, RETRY_MS));
  }
}

/**
 * The virtio-serial port at *path* as a byte stream: what the host writes, and writes in the order
 * made, each waiting while the host is not connected. The control port's lines and the net port's
 * HTTP/2 session each run on one.
 */
export async function openPort(path: string): Promise<Duplex> {
  const handle = await open(path, "r+");
  async function* chunks() {
    const buffer = Buffer.alloc(64 * 1024);
    for (;;) {
      const { bytesRead } = await handle.read(buffer, 0, buffer.length, null);
      if (bytesRead === 0) await new Promise((resolve) => setTimeout(resolve, RETRY_MS));
      else yield Buffer.from(buffer.subarray(0, bytesRead));
    }
  }
  // The port takes at most 32 KiB a write.
  const writeAll = async (data: Buffer) => {
    for (let at = 0; at < data.length;) at += (await handle.write(data, at, data.length - at, null)).bytesWritten;
  };
  return Duplex.from({
    readable: Readable.from(chunks(), { objectMode: false }),
    writable: new Writable({ write: (chunk: Buffer, _encoding, done) => void writeAll(chunk).then(() => done(), done) }),
  });
}
