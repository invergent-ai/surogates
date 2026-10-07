// A virtio-serial port in the guest, one of the agent's two links to the host (spec,
// Section 11, Transport): the control port and the net port. Node has no vsock; the
// port is a character device, read and written through the file system. There is no
// udev in the guest, so the port is found by the name QEMU gave it.

import { open, readdir, readFile } from "node:fs/promises";
import { Duplex, Readable, Writable } from "node:stream";

const PORTS = "/sys/class/virtio-ports";
// While the host is not connected the port reads as ended: it is read again after this.
const RETRY_MS = 20;

// A console port has no name.
export async function findPort(name: string, ports = PORTS): Promise<string> {
  for (const port of await readdir(ports)) {
    const named = await readFile(`${ports}/${port}/name`, "utf8").catch(() => "");
    if (named.trim() === name) return `/dev/${port}`;
  }
  throw new Error(`no virtio port named ${name}`);
}

/**
 * The port at *path* as a byte stream: what the host writes, and writes in the order
 * made, each waiting while the host is not connected. The control port's lines and the
 * net port's HTTP/2 session each run on one.
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
