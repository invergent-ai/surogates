// The agent's ports on the transport the kernel's line names (guest/port.ts): on vsock, the sockets
// surogate-vsock relays each port to, which the agent opens by their paths.

import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { createServer, type Server, type Socket } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { openPortOn, relayedPort, transportOf } from "../src/guest/port.js";
import { INBOUND_PORT, VSOCK_PORTS } from "../src/guest/protocol.js";

let dir: string;
beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "guest-port-"));
});
afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

describe("the transport the kernel's line names", () => {
  it("is the one word of surogate.transport=: virtio-serial's or vsock's", () => {
    expect(transportOf("root=/dev/vda console=hvc0 surogate.disks=virtio surogate.transport=virtio-serial\n")).toBe("virtio-serial");
    expect(transportOf("console=ttyS0 surogate.disks=scsi surogate.transport=vsock")).toBe("vsock");
  });

  it("refuses a line that names none, two, or a word it does not know", () => {
    expect(() => transportOf("console=hvc0")).toThrow("the kernel's command line names surogate.transport= nowhere");
    expect(() => transportOf("surogate.transport=vsock surogate.transport=vsock")).toThrow("the kernel's command line names surogate.transport= twice");
    expect(() => transportOf("surogate.transport=hvsock")).toThrow("surogate.transport=hvsock is not a transport this guest knows");
    expect(() => transportOf("xsurogate.transport=vsock")).toThrow("nowhere");
  });
});

describe("a port on vsock", () => {
  let server: Server | undefined;
  afterEach(async () => {
    if (server) await new Promise((resolve) => server!.close(resolve));
    server = undefined;
  });
  // The relay's socket for *name* in *dir*, as surogate-vsock makes it: one connection, given to *took*.
  const relaying = (name: string, took: (socket: Socket) => void) => new Promise<void>((resolve) => {
    server = createServer(took).listen(join(dir, name), resolve);
  });

  it("is the relay's socket of its name, a byte stream both ways that ends when the host has gone", async () => {
    let host: Socket | undefined;
    await relaying("ai.surogate.control", (socket) => {
      host = socket;
      socket.on("data", (chunk) => socket.write(`heard ${chunk.toString()}`));
    });
    const port = await openPortOn("vsock", "ai.surogate.control", dir);
    const heard = new Promise<string>((done) => port.once("data", (chunk: Buffer) => done(chunk.toString())));
    port.write("hello\n");
    expect(await heard).toBe("heard hello\n");
    const ended = new Promise<string>((done) => port.once("end", () => done("ended")));
    port.resume();
    host!.end();
    expect(await ended).toBe("ended");
  });

  it("waits for the relay's socket while the relay starts, and says so when it never comes", async () => {
    const opened = relayedPort(join(dir, "ai.surogate.net"), 5_000);
    await new Promise((resolve) => setTimeout(resolve, 100));
    await relaying("ai.surogate.net", (socket) => socket.end("there\n"));
    const port = await opened;
    expect(await new Promise((done) => port.once("data", (chunk: Buffer) => done(chunk.toString())))).toBe("there\n");
    await expect(relayedPort(join(dir, "missing"), 200)).rejects.toThrow(`no port at ${join(dir, "missing")}: the host's vsock port was not relayed`);
  });

  it("is each port's own vsock port of the host's, which vm/init has relayed to the agent's socket of its name", () => {
    expect(VSOCK_PORTS).toEqual({ "ai.surogate.control": 5000, "ai.surogate.net": 5001, [INBOUND_PORT]: 5002 });
    const init = readFileSync(new URL("../vm/init", import.meta.url), "utf8");
    const relayed = Object.entries(VSOCK_PORTS).map(([name, port]) => `2:${port}=/run/surogate/ports/${name}`).join(" ");
    expect(init).toContain(`if [ "$transport" = vsock ]; then\n  mkdir -m 0700 /run/surogate/ports\n  /usr/sbin/surogate-vsock ${relayed} &\nfi\n`);
    // Then the agent, as on virtio: its one way in.
    expect(init.trim().split("\n").at(-1)).toBe("exec /usr/bin/tini -- /usr/bin/node /run/surogate/agent/guest/agent.js");
  });
});
