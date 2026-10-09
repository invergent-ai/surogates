// The agent's side of the guest's network port (spec, Section 11, Network): one HTTP/2
// session to the host proxy on ai.surogate.net, and a Unix socket for each root, which
// its runner connects to for each connection a command makes. The first line of each is
// its destination, host:port; the agent opens a CONNECT stream for it, naming the root
// whose socket it came on, never anything the connection says, and answers the line
// with the proxy's status: "200", then the connection's bytes, or "<status> <reason>".
// The other way (spec, Section 5): a connection the agent asked a root's runner for, into the
// root, comes on that root's socket too, under the id it was asked (protocol.ts, INBOUND_LINE).

import { chmodSync, chownSync, mkdirSync, rmSync } from "node:fs";
import { type ClientHttp2Session, connect as connectH2 } from "node:http2";
import { createServer, type Socket } from "node:net";
import { join } from "node:path";
import type { Duplex } from "node:stream";

import { INBOUND_LINE, MAX_INBOUND } from "./protocol.js";

// The roots' sockets, the agent's own: each root's namespace has its own bound at /run/surogate/net.sock.
const TUNNELS = "/run/surogate/net";
// A destination line: a host, as a command spelled it and the host proxy judges it, in visible
// ASCII but '/', and a port.
const DESTINATION = /^[\x21-\x2e\x30-\x7e]{1,255}:\d{1,5}$/;
// A destination line's bound: its bytes, and the time it has to end.
const MAX_LINE = 512;
const LINE_MS = 5_000;
// How many connections one root's commands may have open at once: past it, a connection is closed unheard.
// Those its runner brings for the browser are counted apart (protocol.ts, MAX_INBOUND).
export const MAX_TUNNELS = 256;
// How long a root's runner has to bring a connection the agent asked it for: its dial is to its own loopback.
const ARRIVAL_MS = 5_000;

/** *root*'s socket, in the agent's folder of them unless *folder* is given. */
export function socketOf(root: string, folder = TUNNELS): string {
  return join(folder, `${root}.sock`);
}

export class Network {
  private readonly session: ClientHttp2Session;
  // The connections asked of each root's runner and not brought yet, by root and id.
  private readonly awaited = new Map<string, (brought: Socket | string) => void>();
  // The connections into each root: how many are asked of its runner and not answered, and those handed over and still open.
  private readonly inward = new Map<string, { asked: number; handed: Set<Socket> }>();

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
    // Its connections, which end with it.
    const open = new Set<Socket>();
    // Those of them that are its commands': each one, until its line says its runner brings it for the browser.
    const commands = new Set<Socket>();
    const server = createServer({ allowHalfOpen: true }, (socket) => {
      open.add(socket);
      commands.add(socket);
      socket.on("close", () => {
        open.delete(socket);
        commands.delete(socket);
      });
      this.tunnel(root, socket, commands);
    });
    // Its commands' and the browser's, each with a bound of its own, so that neither takes the other's place.
    server.maxConnections = MAX_TUNNELS + MAX_INBOUND;
    await new Promise<void>((resolve, reject) => {
      server.once("error", reject);
      server.listen(path, resolve);
    });
    // Until these, only the agent could connect to it.
    chownSync(path, uid, uid);
    chmodSync(path, 0o600);
    return () => {
      server.close();
      for (const socket of open) socket.destroy();
      rmSync(path, { force: true });
    };
  }

  path(root: string): string {
    return socketOf(root, this.folder);
  }

  /**
   * The connection into *root* its runner was asked for under *id*, once the runner brings it on
   * that root's own socket; or why there is none: what the runner said of its dial, or "ETIMEDOUT"
   * when it brought nothing in *ms*. Never rejects. For a root that has MAX_INBOUND of them already,
   * awaited or open, the answer is "EMFILE", said at once and not as a promise: nothing is awaited,
   * so its runner is to be asked for no dial. One let go counts no more from then, before its close.
   */
  arrival(root: string, id: string, ms = ARRIVAL_MS): Promise<Socket | string> | "EMFILE" {
    const into = this.inward.get(root) ?? { asked: 0, handed: new Set<Socket>() };
    for (const socket of into.handed) if (socket.destroyed) into.handed.delete(socket);
    if (into.asked + into.handed.size >= MAX_INBOUND) return "EMFILE";
    this.inward.set(root, into);
    const key = `${root} ${id}`;
    const left = () => {
      if (into.asked === 0 && into.handed.size === 0 && this.inward.get(root) === into) this.inward.delete(root);
    };
    into.asked += 1;
    return new Promise((resolve) => {
      const timer = setTimeout(() => settle("ETIMEDOUT"), ms);
      const settle = (brought: Socket | string) => {
        clearTimeout(timer);
        this.awaited.delete(key);
        into.asked -= 1;
        // One handed over keeps its place until it is let go.
        if (typeof brought !== "string") {
          into.handed.add(brought);
          brought.once("close", () => {
            into.handed.delete(brought);
            left();
          });
        }
        left();
        resolve(brought);
      };
      this.awaited.set(key, settle);
    });
  }

  // A connection of *root*'s: its destination line, then a stream for it. *commands*: the root's open
  // connections that are its commands', this one among them until its line says otherwise.
  private tunnel(root: string, socket: Socket, commands: Set<Socket>): void {
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
      const line = head.subarray(0, end).toString("latin1");
      const inbound = INBOUND_LINE.exec(line);
      if (inbound) {
        commands.delete(socket);
        // What the root's server said first comes with the line, and is kept for whoever asked.
        socket.pause();
        if (end + 1 < head.length) socket.unshift(head.subarray(end + 1));
        // Only the root whose socket it came on was asked: an id another root's command names brings nothing.
        const asked = this.awaited.get(`${root} ${inbound[1]}`);
        if (!asked || inbound[2] !== undefined) socket.destroy();
        return void asked?.(inbound[2] ?? socket);
      }
      // The runner sends nothing past its line before the answer; and one past its commands' bound is closed unheard.
      if (end + 1 < head.length || commands.size > MAX_TUNNELS) return void socket.destroy();
      this.open(root, line, socket);
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
