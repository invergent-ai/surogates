// A virtio-serial port in the guest, the agent's link to the host (spec, Section
// 11, Transport). Node has no vsock; the port is a character device, read and
// written through the file system. There is no udev in the guest, so the port is
// found by the name QEMU gave it.

import { open, readdir, readFile } from "node:fs/promises";
import { Readable } from "node:stream";

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

export interface Port {
  input: Readable;
  // In the order called; a write waits while the host is not connected.
  write(text: string): Promise<void>;
}

export async function openPort(path: string): Promise<Port> {
  const handle = await open(path, "r+");
  async function* chunks() {
    const buffer = Buffer.alloc(64 * 1024);
    for (;;) {
      const { bytesRead } = await handle.read(buffer, 0, buffer.length, null);
      if (bytesRead === 0) await new Promise((resolve) => setTimeout(resolve, RETRY_MS));
      else yield Buffer.from(buffer.subarray(0, bytesRead));
    }
  }
  let queue = Promise.resolve();
  // The port takes at most 32 KiB a write.
  const writeAll = async (data: Buffer) => {
    for (let at = 0; at < data.length;) at += (await handle.write(data, at, data.length - at, null)).bytesWritten;
  };
  return {
    input: Readable.from(chunks()),
    write: (text) => (queue = queue.then(() => writeAll(Buffer.from(text)))),
  };
}
