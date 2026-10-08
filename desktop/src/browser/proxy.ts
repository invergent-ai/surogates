// The agent's browser's one way out (spec, Section 5): an HTTP proxy on this computer's
// loopback, which the browser is launched with for every request, loopback's too
// (--proxy-bypass-list=<-loopback>). It looks each host up once, lets through only what
// leads past this computer and its private networks, and connects to the addresses it
// judged, so a name cannot lead elsewhere between the two. Nothing is asked: the first
// release gives the agent's browser no private network at all.

import { createServer, type IncomingMessage, request as httpRequest, type Server, type ServerResponse } from "node:http";
import { BlockList, isIPv6, type Socket } from "node:net";
import type { Duplex } from "node:stream";

import { type Dial, destination, dialFirst, reach, type ReachOptions } from "../vm/egress.js";

export interface BrowserProxyOptions extends ReachOptions {
  connect?: Dial; // the connection to a judged address; net.connect by default
}

// The names the proxy answers itself, <token>.proxy-check.invalid: what a launch asks for, to
// prove its browser's requests come here. No resolver answers them (RFC 6761), so a browser
// that goes around the proxy reaches nothing with one.
export const CHECK_DOMAIN = ".proxy-check.invalid";
const checkOf = (host: string): string | null => (host.endsWith(CHECK_DOMAIN) ? host.slice(0, -CHECK_DOMAIN.length) : null);

const subnets = (ranges: Array<[string, number]>) => {
  const list = new BlockList();
  for (const [net, prefix] of ranges) list.addSubnet(net, prefix, "ipv6");
  return list;
};
// NAT64's well-known prefix (RFC 6052): its translator dials the IPv4 address in the last 32 bits.
const NAT64 = subnets([["64:ff9b::", 96]]);
// IPv6 that carries an IPv4 address no site's own does: local-use NAT64 (RFC 8215), which maps
// into a site's own IPv4 networks, 6to4 and Teredo. IPv4-mapped and -compatible are reach's.
const CARRIERS = subnets([["64:ff9b:1::", 48], ["2002::", 16], ["2001::", 32]]);

// The IPv4 address in an IPv6 address's last 32 bits. URL spells it in hex, "::" for its zeros.
function lastIPv4(address: string): string {
  const spelled = new URL(`http://[${address}]/`).hostname.slice(1, -1);
  const tail = spelled.includes("::") ? spelled.slice(spelled.indexOf("::") + 2) : spelled;
  const [hi = 0, lo = 0] = [0, 0, ...tail.split(":").filter(Boolean).map((word) => Number.parseInt(word, 16))].slice(-2);
  return `${hi >> 8}.${hi & 255}.${lo >> 8}.${lo & 255}`;
}

/**
 * The addresses a request to *host*:*port* may be dialed at: those of one lookup, when every
 * one leads past this computer and its private networks, an IPv4 address NAT64 carries too.
 * Null refuses it: no destination, this computer's own, a private network, an address that
 * carries IPv4 otherwise, or a name not looked up in time.
 */
export async function admitted(host: string, port: number, options: ReachOptions = {}): Promise<string[] | null> {
  const found = destination(host, port);
  if (!found) return null;
  try {
    const where = await reach(found.host, options);
    if (where?.reach !== "public") return null;
    for (const address of where.addresses.filter((address) => isIPv6(address))) {
      if (CARRIERS.check(address, "ipv6")) return null;
      if (NAT64.check(address, "ipv6") && (await reach(lastIPv4(address), options))?.reach !== "public") return null;
    }
    return where.addresses;
  } catch {
    // A lookup or an interface read that threw, or an answer that does not parse (a zone id).
    return null;
  }
}

// CONNECT's target, host:port, an IPv6 address in brackets; null for anything else.
function target(named: string | undefined): { host: string; port: number } | null {
  const at = named?.lastIndexOf(":") ?? -1;
  if (named === undefined || at < 1 || !/^\d{1,5}$/.test(named.slice(at + 1))) return null;
  return { host: named.slice(0, at), port: Number(named.slice(at + 1)) };
}

// Each hop's own headers, which a proxy does not pass on (RFC 9110, 7.6.1), and a proxy's
// sign-in either way: a site's 407 must not open the browser's proxy sign-in dialog.
const HOP = new Set([
  "connection", "keep-alive", "proxy-connection", "proxy-authenticate", "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade",
]);
const passed = (headers: IncomingMessage["headers"]) => Object.fromEntries(Object.entries(headers).filter(([name]) => !HOP.has(name)));

export class BrowserProxy {
  private readonly server: Server;
  // Every connection it carries, so a close ends them all.
  private readonly carried = new Set<Duplex>();
  // The checks a launch waits on, by token, and whether each has come through. Only these are
  // kept: a page can ask for any number of others, and none of them can push a launch's out.
  private readonly checks = new Map<string, boolean>();

  constructor(private readonly options: BrowserProxyOptions = {}) {
    this.server = createServer((request, response) => void this.forward(request, response));
    this.server.on("connect", (request: IncomingMessage, client: Duplex, head: Buffer) => void this.tunnel(request, client, head));
    this.server.on("clientError", (_error, socket: Duplex) => socket.destroy());
  }

  /** Listens on 127.0.0.1, on a port the system picks, and gives that port. */
  listen(): Promise<number> {
    return new Promise((resolve, reject) => {
      this.server.once("error", reject);
      this.server.listen(0, "127.0.0.1", () => resolve((this.server.address() as { port: number }).port));
    });
  }

  /** A launch's check, waited on from now on: checked() says whether its request came through. */
  expect(token: string): void {
    this.checks.set(token, false);
  }

  /** Whether a request for *token*'s check name came through this proxy since it was expected; asking forgets it. */
  checked(token: string): boolean {
    const came = this.checks.get(token) === true;
    this.checks.delete(token);
    return came;
  }

  // A check's name, answered here, and recorded when a launch waits on it: nothing is dialed for it.
  private answered(host: string): boolean {
    const token = checkOf(host);
    if (token === null) return false;
    if (this.checks.has(token)) this.checks.set(token, true);
    return true;
  }

  close(): Promise<void> {
    for (const connection of this.carried) connection.destroy();
    const closed = new Promise<void>((resolve) => this.server.close(() => resolve()));
    // Each of the browser's connections ends now, one kept alive too, not at its keep-alive timeout.
    this.server.closeAllConnections();
    return closed;
  }

  private keep(connection: Duplex): void {
    this.carried.add(connection);
    connection.once("close", () => this.carried.delete(connection));
    connection.on("error", () => connection.destroy());
  }

  // https and WebSockets: one tunnel, dialed at what was judged.
  private async tunnel(request: IncomingMessage, client: Duplex, head: Buffer): Promise<void> {
    this.keep(client);
    // Before the lookup, so a browser that resets during it, or a close, has nothing dialed for it.
    const gone = new AbortController();
    client.once("close", () => gone.abort());
    const to = target(request.url);
    // The https upgrade's try at a check comes here; the plain request after it is answered.
    if (to && this.answered(to.host)) return void client.end("HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n");
    const addresses = to && (await admitted(to.host, to.port, this.options));
    if (!to || !addresses) return void client.end("HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n");
    let upstream: Socket;
    try {
      upstream = await dialFirst(addresses, to.port, gone.signal, this.options.connect);
    } catch {
      return void client.end("HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n");
    }
    this.keep(upstream);
    upstream.once("close", () => client.destroy());
    client.once("close", () => upstream.destroy());
    // A browser closes a tunnel whole, never half: its end ends the tunnel, whether the site hears it or not.
    client.once("end", () => upstream.destroy());
    client.write("HTTP/1.1 200 Connection Established\r\n\r\n");
    if (head.length > 0) upstream.write(head);
    upstream.pipe(client);
    client.pipe(upstream);
  }

  // Plain http, as a browser sends it to a proxy: the absolute address, one request a connection.
  private async forward(request: IncomingMessage, response: ServerResponse): Promise<void> {
    let url: URL | null = null;
    try {
      url = new URL(request.url ?? "");
    } catch {
      // Not a proxy's request.
    }
    if (!url || url.protocol !== "http:") return void response.writeHead(400).end();
    if (this.answered(url.hostname)) return void response.writeHead(204).end();
    const port = Number(url.port || 80);
    // Before the lookup, so a request the browser gave up on during it is not sent.
    const gone = new AbortController();
    response.once("close", () => gone.abort());
    const addresses = await admitted(url.hostname, port, this.options);
    if (!addresses) return void response.writeHead(403).end();
    let socket: Socket;
    try {
      socket = await dialFirst(addresses, port, gone.signal, this.options.connect);
    } catch {
      if (!response.headersSent) response.writeHead(502).end();
      return;
    }
    this.keep(socket);
    // The browser gone, partway through the answer or before it: the site's connection goes too.
    response.once("close", () => socket.destroy());
    // ponytail: one connection a request, none kept for the next; a pool if page loads ever show it.
    const upstream = httpRequest(
      {
        createConnection: () => socket, method: request.method, path: `${url.pathname}${url.search}`,
        // The target's own host, as RFC 9112 (3.2.2) has a proxy send it, whatever the client's said.
        headers: { ...passed(request.headers), host: url.host }, setHost: false,
      },
      (answer) => {
        response.writeHead(answer.statusCode ?? 502, passed(answer.headers));
        // A site that hangs up partway through its answer: the browser's is cut short too, not left open.
        answer.once("close", () => {
          if (!answer.complete) response.destroy();
        });
        answer.pipe(response);
      },
    );
    upstream.on("error", () => {
      if (response.headersSent) response.destroy();
      else response.writeHead(502).end();
    });
    request.pipe(upstream);
  }
}
