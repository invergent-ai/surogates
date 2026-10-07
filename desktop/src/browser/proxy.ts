// The agent's browser's one way out (spec, Section 5): an HTTP proxy on this computer's
// loopback, which the browser is launched with for every request, loopback's too
// (--proxy-bypass-list=<-loopback>). It looks each host up once, lets through only what
// leads past this computer and its private networks, and connects to the addresses it
// judged, so a name cannot lead elsewhere between the two. Nothing is asked: the first
// release gives the agent's browser no private network at all.

import { createServer, type IncomingMessage, request as httpRequest, type Server, type ServerResponse } from "node:http";
import type { Socket } from "node:net";
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

/**
 * The addresses a request to *host*:*port* may be dialed at: those of one lookup, when every
 * one leads past this computer and its private networks. Null refuses it: no destination, this
 * computer's own, a private network, or a name not looked up in time.
 */
export async function admitted(host: string, port: number, options: ReachOptions = {}): Promise<string[] | null> {
  const found = destination(host, port);
  if (!found) return null;
  const where = await reach(found.host, options).catch(() => null);
  return where?.reach === "public" ? where.addresses : null;
}

// CONNECT's target, host:port, an IPv6 address in brackets; null for anything else.
function target(named: string | undefined): { host: string; port: number } | null {
  const at = named?.lastIndexOf(":") ?? -1;
  if (named === undefined || at < 1 || !/^\d{1,5}$/.test(named.slice(at + 1))) return null;
  return { host: named.slice(0, at), port: Number(named.slice(at + 1)) };
}

// Each hop's own headers, which a proxy does not pass on (RFC 9110, 7.6.1).
const HOP = new Set(["connection", "keep-alive", "proxy-connection", "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade"]);
const passed = (headers: IncomingMessage["headers"]) => Object.fromEntries(Object.entries(headers).filter(([name]) => !HOP.has(name)));

export class BrowserProxy {
  private readonly server: Server;
  // Every connection it carries, so a close ends them all.
  private readonly carried = new Set<Duplex>();
  // The checks asked for through it, by token.
  private readonly checks = new Set<string>();

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

  /** Whether a request for *token*'s check name came through this proxy. */
  checked(token: string): boolean {
    return this.checks.has(token);
  }

  // A check's name, recorded and answered here: nothing is dialed for it.
  private answered(host: string): boolean {
    const token = checkOf(host);
    if (token !== null) this.checks.add(token);
    return token !== null;
  }

  close(): Promise<void> {
    for (const connection of this.carried) connection.destroy();
    return new Promise((resolve) => this.server.close(() => resolve()));
  }

  private keep(connection: Duplex): void {
    this.carried.add(connection);
    connection.once("close", () => this.carried.delete(connection));
    connection.on("error", () => connection.destroy());
  }

  // https and WebSockets: one tunnel, dialed at what was judged.
  private async tunnel(request: IncomingMessage, client: Duplex, head: Buffer): Promise<void> {
    this.keep(client);
    const to = target(request.url);
    // The https upgrade's try at a check comes here; the plain request after it is answered.
    if (to && this.answered(to.host)) return void client.end("HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n");
    const addresses = to && (await admitted(to.host, to.port, this.options));
    if (!to || !addresses) return void client.end("HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n");
    const gone = new AbortController();
    client.once("close", () => gone.abort());
    let upstream: Socket;
    try {
      upstream = await dialFirst(addresses, to.port, gone.signal, this.options.connect);
    } catch {
      return void client.end("HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n\r\n");
    }
    this.keep(upstream);
    upstream.once("close", () => client.destroy());
    client.once("close", () => upstream.destroy());
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
    // ponytail: one connection a request, none kept for the next; a pool if page loads ever show it.
    const upstream = httpRequest(
      { createConnection: () => socket, method: request.method, path: `${url.pathname}${url.search}`, headers: passed(request.headers), setHost: false },
      (answer) => {
        response.writeHead(answer.statusCode ?? 502, passed(answer.headers));
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
