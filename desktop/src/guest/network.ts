// The agent's side of the guest's network port (spec, Section 11, Network): one HTTP/2
// session to the host proxy on ai.surogate.net, and a Unix socket for each root, which
// its runner connects to for each connection a command makes. The first line of each is
// its destination, host:port; the agent opens a CONNECT stream for it, naming the root
// whose socket it came on, never anything the connection says, and answers the line
// with the proxy's status: "200", then the connection's bytes, or "<status> <reason>".

import { chmodSync, chownSync, mkdirSync, rmSync } from "node:fs";
import { type ClientHttp2Session, connect as connectH2 } from "node:http2";
import { createServer, type Socket } from "node:net";
import { join } from "node:path";
import type { Duplex } from "node:stream";

// The roots' sockets, the agent's own: each root's namespace has its own bound at /run/surogate/net.sock.
const TUNNELS = "/run/surogate/net";
// A destination line: a host, as a command spelled it and the host proxy judges it, and a port.
const DESTINATION = /^[^\s/]{1,255}:\d{1,5}$/;
// A destination line's bound: its bytes, and the time it has to end.
const MAX_LINE = 512;
const LINE_MS = 5_000;
// How many connections one root may have open at once: past it, a connection is refused at once.
export const MAX_TUNNELS = 256;

/** *root*'s socket, in the agent's folder of them unless *folder* is given. */
export function socketOf(root: string, folder = TUNNELS): string {
  return join(folder, `${root}.sock`);
}

export class Network {
  private readonly session: ClientHttp2Session;

  constructor(port: Duplex, private readonly folder = TUNNELS, private readonly lineMs = LINE_MS) {
    this.session = connectH2("http://guest", { createConnection: () => port });
    // The host ends it only with the guest: a tunnel opened after answers that the sandbox has no network.
    this.session.on("error", () => {});
  }

  /** *root*'s socket, only its guest user's to connect to; resolves with what closes it and removes it. */
  async listen(root: string, uid: number): Promise<() => void> {
    mkdirSync(this.folder, { recursive: true, mode: 0o700 });
    const path = this.path(root);
    rmSync(path, { force: true });
    const server = createServer({ allowHalfOpen: true }, (socket) => this.tunnel(root, socket));
    server.maxConnections = MAX_TUNNELS;
    await new Promise<void>((resolve, reject) => {
      server.once("error", reject);
      server.listen(path, resolve);
    });
    // Until these, only the agent could connect to it.
    chownSync(path, uid, uid);
    chmodSync(path, 0o600);
    return () => {
      server.close();
      rmSync(path, { force: true });
    };
  }

  path(root: string): string {
    return socketOf(root, this.folder);
  }

  // A connection of *root*'s: its destination line, then a stream for it.
  private tunnel(root: string, socket: Socket): void {
    socket.on("error", () => {});
    const timer = setTimeout(() => socket.destroy(), this.lineMs);
    let head = Buffer.alloc(0);
    const read = (chunk: Buffer) => {
      head = Buffer.concat([head, chunk]);
      const end = head.indexOf(0x0a);
      if (end < 0) {
        if (head.length > MAX_LINE) socket.destroy();
        return;
      }
      clearTimeout(timer);
      socket.off("data", read);
      // The runner sends nothing past its line before the answer.
      if (end + 1 < head.length) return void socket.destroy();
      this.open(root, head.subarray(0, end).toString("latin1"), socket);
    };
    socket.on("data", read);
  }

  private open(root: string, destination: string, socket: Socket): void {
    if (!DESTINATION.test(destination)) return void socket.end("403 invalid\n");
    let stream;
    try {
      stream = this.session.request({ ":method": "CONNECT", ":authority": destination, "surogate-root": root });
    } catch {
      return void socket.end("502 sandbox\n");
    }
    const opened = stream;
    let answered = false;
    // Until the answer the socket is read, so a runner that leaves is seen: its end, before
    // its 200, is its client giving up, and the stream goes, with the host's ask or dial.
    // A byte from it is no runner's, which sends nothing before then.
    const early = () => socket.destroy();
    const left = () => opened.destroy();
    socket.on("data", early);
    socket.once("end", left);
    // Gone before the host answered, as with the guest's session: the sandbox has no network.
    opened.on("close", () => {
      if (!answered) socket.end("502 sandbox\n");
    });
    opened.on("error", () => {
      if (answered) socket.destroy();
    });
    socket.on("close", () => opened.destroy());
    opened.on("response", (headers) => {
      answered = true;
      socket.off("data", early);
      socket.off("end", left);
      const status = Number(headers[":status"]);
      if (status !== 200) return void socket.end(`${status} ${String(headers["surogate-reason"] ?? "")}\n`);
      socket.write("200\n");
      socket.pipe(opened);
      opened.pipe(socket);
    });
  }
}
