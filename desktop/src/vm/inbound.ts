// The host's side of the way into a chat's sandbox from outside it (spec, Section 5 and Section 11,
// Network, "Later"). The guest's inbound port carries one HTTP/2 session, the host its client: a
// CONNECT stream for each connection, to a port of the loopback's address, with the root in a header,
// which the agent has that root's runner dial in the root's own namespaces (guest/inbound.ts). The
// guest is not trusted: its answer is a status, a short reason, and then bytes.
//
// The browser's proxy runs in another process, so it reaches this one through a door: a Unix socket
// in the VM's runtime folder, where each connection knocks with its device's key and a port. The door
// opens only for a port the app forwarded for that key, to the root the app named, and only into a
// root set up in the guest.

import { timingSafeEqual } from "node:crypto";
import { chmodSync, mkdirSync, rmSync } from "node:fs";
import { type ClientHttp2Session, type ClientHttp2Stream, connect as connectH2, constants } from "node:http2";
import { createServer, type Server, type Socket } from "node:net";
import { dirname } from "node:path";
import type { Duplex } from "node:stream";

import { SANDBOX_PORTS } from "../guest/listeners.js";
import { MAX_INBOUND } from "../guest/protocol.js";

// The door's name in the VM's runtime folder, this user's own.
export const DOOR = "browser.sock";
// A knock: a device's key, 256 bits in hex, a port as its digits spell it, and " 6" where the browser's
// address named the loopback's IPv6 address, which the root's runner then tries first.
const KNOCK = /^([0-9a-f]{64}) ([1-9]\d{0,4})( 6)?$/;
const MAX_KNOCK = 128;
const KNOCK_MS = 5_000;
// How many connections the door holds at once, every device's and those that have not knocked yet.
export const MAX_OPEN = 512;
// How many of them one device's browser may have carried at once, over all its chats.
export const MAX_PER_KEY = 256;
// How long the agent has to answer a stream: past its own bound on a runner's dial (guest/network.ts).
export const REACH_MS = 10_000;
// Why the guest took no connection, as it may say it: an errno's name, or that the root has no sandbox there.
const REASON = /^([A-Z]{1,16}|sandbox)$/;

/**
 * Lets go of a connection into a root, so that the guest lets go of it too. A stream is reset with a code
 * first: destroyed alone, it is reset with none, and the agent's end, which may still hold bytes of the
 * server's answer it could not write, is then never closed, nor the root's connection behind it (measured).
 */
export function letGo(reached: Duplex): void {
  const stream = reached as Partial<Pick<ClientHttp2Stream, "close" | "closed">>;
  if (typeof stream.close === "function" && stream.closed === false) stream.close(constants.NGHTTP2_CANCEL);
  reached.destroy();
}

/** The ports of chats' own servers each device's browser may open, as the app told them: by the key that device's proxy knocks with. */
export class Forwarded {
  private readonly devices = new Map<string, Map<number, string>>();

  /**
   * What *key*'s browser may open from now on, each port with the root it leads to; none forgets the key.
   * Never a port of the sandbox's own proxies, whoever names it.
   */
  set(key: string, ports: ReadonlyArray<readonly [number, string]>): void {
    const kept = new Map(ports.filter(([port]) => !SANDBOX_PORTS.has(port)).map(([port, root]) => [port, root]));
    if (kept.size === 0) this.devices.delete(key);
    else this.devices.set(key, kept);
  }

  /** The root *port* leads to for the device that knocks with *key*; null for a key nobody has, or a port not forwarded for it. */
  rootOf(key: string, port: number): string | null {
    const given = Buffer.from(key);
    let root: string | null = null;
    // Every key is compared, each in constant time.
    for (const [known, ports] of this.devices) {
      const own = Buffer.from(known);
      if (own.length === given.length && timingSafeEqual(own, given)) root = ports.get(port) ?? null;
    }
    return root;
  }
}

/** The host's end of the guest's inbound port: a stream for each connection into a root. */
export class Carrier {
  private readonly session: ClientHttp2Session;

  constructor(channel: Duplex, private readonly reachMs = REACH_MS) {
    this.session = connectH2("http://guest", { createConnection: () => channel, settings: { enablePush: false } });
    // The guest ends it only by going: a stream opened after answers that the root has no sandbox.
    this.session.on("error", () => {});
  }

  /**
   * A connection to *port* of *root*'s own loopback in the guest, the family *first* names tried
   * before the other, or why there is none: what the agent said, when it is a reason an agent gives,
   * "unreachable" for anything else, and "ETIMEDOUT" when it said nothing in time. Never rejects.
   */
  open(root: string, port: number, first: 4 | 6 = 4): Promise<ClientHttp2Stream | string> {
    return new Promise((resolve) => {
      let stream: ClientHttp2Stream;
      try {
        stream = this.session.request({ ":method": "CONNECT", ":authority": `${first === 6 ? "[::1]" : "127.0.0.1"}:${port}`, "surogate-root": root });
      } catch {
        return resolve("sandbox");
      }
      const timer = setTimeout(() => settle("ETIMEDOUT"), this.reachMs);
      const settle = (answer: ClientHttp2Stream | string) => {
        clearTimeout(timer);
        if (typeof answer === "string") letGo(stream);
        resolve(answer);
      };
      stream.on("error", () => {});
      stream.once("close", () => settle("sandbox"));
      stream.once("response", (headers) => {
        if (headers[":status"] === 200) return settle(stream);
        const reason = String(headers["surogate-reason"] ?? "");
        settle(REASON.test(reason) ? reason : "unreachable");
      });
    });
  }

  close(): void {
    this.session.destroy();
  }
}

// The door's answer where it carries nothing: its line, and the connection ended, whether its proxy closes it or not.
const answer = (socket: Socket, line: string): void => void socket.end(`${line}\n`, () => socket.destroy());

export interface DoorBounds {
  perRoot?: number; // a root's connections at once: at it, the one idle longest goes to admit the next
  perKey?: number; // a device's, over all its chats: past it a knock is answered busy
  open?: number; // the door's own, every device's and those that have not knocked yet
  knockMs?: number; // how long a knock has to come whole
}

// A connection the door carries, or asked the guest for: whose it is, where it leads, and when it last carried a byte.
interface Carried {
  socket: Socket;
  reached: Duplex | null;
  key: string;
  root: string;
  at: number;
}

/** The door the browser's proxy knocks at, for one guest: it goes with the guest. */
export class Door {
  private readonly server: Server;
  private readonly open = new Set<Socket>();
  private readonly carried = new Set<Carried>();
  private readonly perRoot: number;
  private readonly perKey: number;
  private readonly knockMs: number;

  // *reach*: a connection to a port of a root's own loopback, the family *first* names tried before the other, or why none (Guest.reach).
  constructor(
    private readonly path: string, private readonly forwarded: Forwarded,
    private readonly reach: (root: string, port: number, first: 4 | 6) => Promise<Duplex | string>,
    bounds: DoorBounds = {},
  ) {
    this.perRoot = bounds.perRoot ?? MAX_INBOUND;
    this.perKey = bounds.perKey ?? MAX_PER_KEY;
    this.knockMs = bounds.knockMs ?? KNOCK_MS;
    this.server = createServer({ allowHalfOpen: true }, (socket) => this.knocked(socket));
    this.server.maxConnections = bounds.open ?? MAX_OPEN;
    this.server.on("error", () => {});
    mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
    rmSync(path, { force: true });
    // Its folder is this user's alone already; so is the door.
    this.server.listen(path, () => chmodSync(path, 0o600));
  }

  close(): void {
    this.server.close();
    for (const socket of this.open) socket.destroy();
    rmSync(this.path, { force: true });
  }

  // A connection of the browser's proxy: its knock, then the door's answer, "200" and the connection's
  // bytes, or a status and why. A knock that does not come whole in time, or with bytes after it, is dropped unheard.
  private knocked(socket: Socket): void {
    this.open.add(socket);
    socket.on("close", () => this.open.delete(socket));
    socket.on("error", () => {});
    const timer = setTimeout(() => socket.destroy(), this.knockMs);
    socket.once("close", () => clearTimeout(timer));
    // Its proxy closes a connection whole, never half: one ended before its knock is gone.
    const gone = () => socket.destroy();
    socket.once("end", gone);
    let head = Buffer.alloc(0);
    const read = (chunk: Buffer) => {
      head = Buffer.concat([head, chunk]);
      const end = head.indexOf(0x0a);
      if (end < 0) {
        if (head.length > MAX_KNOCK) socket.destroy();
        return;
      }
      clearTimeout(timer);
      socket.off("data", read);
      socket.off("end", gone);
      if (end + 1 < head.length) return void socket.destroy();
      const knock = KNOCK.exec(head.subarray(0, end).toString("latin1"));
      const key = knock?.[1] ?? "";
      const port = Number(knock?.[2] ?? 0);
      const root = knock && port <= 65_535 ? this.forwarded.rootOf(key, port) : null;
      if (root === null) return answer(socket, "403 refused");
      void this.carry(socket, key, root, port, knock?.[3] === undefined ? 4 : 6);
    };
    socket.on("data", read);
  }

  private async carry(socket: Socket, key: string, root: string, port: number, first: 4 | 6): Promise<void> {
    const held = [...this.carried];
    // A device at its bound is told so, and nothing of its own is ended for it.
    if (held.filter((other) => other.key === key).length >= this.perKey) return answer(socket, "502 busy");
    // A root at its bound gives up the connection that has carried no byte, either way, for longest: a page's
    // new request is worth more than what has sat idle. Nothing is ended while there is room.
    const into = held.filter((other) => other.root === root);
    if (into.length >= this.perRoot) this.end(into.reduce((idle, other) => (other.at < idle.at ? other : idle)));
    const own: Carried = { socket, reached: null, key, root, at: performance.now() };
    this.carried.add(own);
    socket.once("close", () => this.carried.delete(own));
    // Read meanwhile, so the proxy is seen to leave: its end before the answer is the browser giving up.
    let left = false;
    const leave = () => {
      left = true;
    };
    socket.once("end", leave);
    socket.once("close", leave);
    socket.resume();
    const reached = await this.reach(root, port, first).catch(() => "unreachable");
    if (typeof reached === "string") return answer(socket, `502 ${reached}`);
    if (left || socket.destroyed) {
      socket.destroy();
      return void letGo(reached);
    }
    socket.off("end", leave);
    // And its end after the answer is the browser letting go, whatever the root's server would still say.
    socket.once("end", () => socket.destroy());
    own.reached = reached;
    const carrying = () => {
      own.at = performance.now();
    };
    socket.on("data", carrying);
    reached.on("data", carrying);
    reached.on("error", () => socket.destroy());
    // The guest's side gone: the browser's goes too, once it has every byte of an answer that ended.
    reached.once("close", () => {
      if (reached.readableEnded && !socket.writableFinished) socket.once("finish", () => socket.destroy());
      else socket.destroy();
    });
    socket.once("close", () => letGo(reached));
    socket.write("200\n");
    socket.pipe(reached, { end: false });
    reached.pipe(socket);
  }

  // Ends a connection now, in the guest too; its place is free at once.
  private end(carried: Carried): void {
    this.carried.delete(carried);
    if (carried.reached) letGo(carried.reached);
    carried.socket.destroy();
  }
}
