// The agent's browser's one way out (spec, Section 5): an HTTP proxy on this computer's
// loopback, which the browser is launched with for every request, loopback's too
// (--proxy-bypass-list=<-loopback>). It looks each host up once, lets through only what
// leads past this computer and its private networks, and connects to the addresses it
// judged, so a name cannot lead elsewhere between the two. Nothing is asked here, and the
// browser has one private destination: a port of a chat's own servers that the chat's user
// allowed (ports.ts), by one of the loopback's three names. A connection to one never goes
// to this computer's loopback: it knocks at the VM manager's door (vm/inbound.ts), which
// carries it into that chat's sandbox.
//
// Its port is one any program on this computer can find and connect to. A public site is
// carried for whoever asks, since that program reaches it by itself. Everything else is its
// own browser's alone, by a sign-in made for each launch of that browser (signIn): a chat's
// port first of all. The profile is every chat's, so the proxy cannot tell whose tab asks;
// what it can read, on a request its own browser signed, is where that browser says the
// request comes from, and it carries only what comes from a page of a chat's own server, its
// user or its agent (ownRequest). A tunnel says nothing of that at its CONNECT, so its first
// bytes are read: only a WebSocket such a page opens is carried (ownSocket).
//
// A port not allowed is refused, and nobody is asked: a tab that goes there is shown a short
// page of the proxy's own, which says what the port is (notOpenPage).

import { createHash, randomBytes, timingSafeEqual } from "node:crypto";
import { createServer, type IncomingHttpHeaders, type IncomingMessage, request as httpRequest, type Server, type ServerResponse } from "node:http";
import { BlockList, isIPv6, type Socket } from "node:net";
import type { Duplex } from "node:stream";

import { tunnel as knock } from "../guest/listeners.js";
import { type Dial, destination, dialFirst, reach, type ReachOptions } from "../vm/egress.js";
import { chatPort, chatPortOf, SANDBOX_PORTS } from "./ports.js";

export interface BrowserProxyOptions extends ReachOptions {
  connect?: Dial; // the connection to a judged address; net.connect by default
  handshakeMs?: number; // how long a tunnel to a chat's port has to send its WebSocket's handshake
}

// How long a tunnel to a chat's port has to say what it is, and the most a WebSocket's handshake holds.
const HANDSHAKE_MS = 5_000;
const MAX_HANDSHAKE = 16 * 1024;

// The ports of chats' own servers a browser may open, and how its connections to them are carried:
// the VM manager's door, and the key its device knocks with there.
interface Forwards {
  ports: ReadonlySet<number>;
  door: string;
  key: string;
}

// Why a connection to a chat's port is not carried: the port is not allowed here, or the door does not open
// it for this device; the sandbox holds every connection it takes from the browser, the device's (the door's
// "busy") or the chat's (the guest's EMFILE); or nothing took it, and there may be no door.
export type NotCarried = "refused" | "busy" | "unreachable";
// What the browser is answered for each.
const NOT_CARRIED: Record<NotCarried, number> = { refused: 403, busy: 503, unreachable: 502 };
const FULL = new Set(["busy", "EMFILE"]);

/**
 * Whether a plain request to a chat's port comes from where the port was allowed for: a page of a
 * chat's own server, on a port *allowed* now, or its user's or its agent's own navigation. Read from
 * what the browser itself says of each request, which no page's code can set: Sec-Fetch-Site, then
 * Origin, else Referer. Believed only of a request that carries the launch's sign-in: any other
 * program can write these. A page of another site gets one thing through, a link followed or a
 * redirect, a GET that opens a tab there, as any site can send a tab anywhere; never a fetch, a
 * frame, a form, a beacon, nor a page loaded ahead of any visit (Sec-Purpose). A request that says
 * nothing is no browser's, and is refused.
 */
export function ownRequest(method: string, headers: IncomingHttpHeaders, allowed: ReadonlySet<number>): boolean {
  const site = headers["sec-fetch-site"];
  if (site === "same-origin" || site === "same-site" || site === "none") return true;
  if (site !== "cross-site") return false;
  const visit = method === "GET" && headers["sec-fetch-mode"] === "navigate" && headers["sec-fetch-dest"] === "document";
  if (visit && headers["sec-purpose"] === undefined) return true;
  // The loopback's other name for a chat's page, which the browser calls another site.
  const from = headers.origin ?? headers.referer;
  const page = typeof from === "string" ? chatPortOf(from) : null;
  return page !== null && allowed.has(page);
}

/**
 * Whether *head*, a tunnel's first bytes up to their empty line, is a WebSocket's handshake (RFC 6455)
 * from a page of a chat's own server, on a port *allowed* now: a GET that upgrades, with one Origin,
 * which the browser writes and no page's code can set. The browser sends no fetch metadata with it.
 * Every line is a header's own: one folded into the line before, or that names nothing, refuses it.
 */
export function ownSocket(head: string, allowed: ReadonlySet<number>): boolean {
  const [first = "", ...lines] = head.split("\r\n");
  if (!/^GET \S+ HTTP\/1\.1$/.test(first) || lines.some((line) => !/^[!#-'*+\-.0-9A-Z^-z|~]+:/.test(line))) return false;
  const named = (name: string) => lines.filter((line) => line.toLowerCase().startsWith(`${name}:`)).map((line) => line.slice(name.length + 1).trim());
  const [origin, ...others] = named("origin");
  if (origin === undefined || others.length > 0 || !named("upgrade").some((value) => value.toLowerCase() === "websocket")) return false;
  const page = chatPortOf(origin);
  return page !== null && allowed.has(page);
}

/** The page a tab is shown at *port* of a chat's servers when it is not allowed: made of the port's number alone, with nothing of the request. */
export function notOpenPage(port: number): string {
  return `<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="color-scheme" content="light dark"><title>Port ${port} is not open</title></head>`
    + `<body><h1>Port ${port} of a chat's servers is not open in this browser</h1><p>It opens when that chat's agent navigates to it and the chat's user allows it.</p>`
    + "<p>The ports allowed now are listed in Surogate's Settings, under Folders and permissions.</p></body></html>";
}
// What it is sent with: nothing in it runs or loads, and no copy of it is kept past a port's allowing.
const NOT_OPEN_HEADERS = {
  "content-type": "text/html; charset=utf-8", "content-security-policy": "default-src 'none'", "x-content-type-options": "nosniff", "cache-control": "no-store",
};

// The names the proxy answers itself, <token>.proxy-check.invalid: what a launch asks for, to
// prove its browser's requests come here. No resolver answers them (RFC 6761), so a browser
// that goes around the proxy reaches nothing with one.
export const CHECK_DOMAIN = ".proxy-check.invalid";
const checkOf = (host: string): string | null => (host.endsWith(CHECK_DOMAIN) ? host.slice(0, -CHECK_DOMAIN.length) : null);

// The name the proxy's own browser signs in under; the secret is each launch's own.
const SIGN_IN_AS = "surogate";
// What is answered to whatever needs the sign-in and comes without it, and nothing else.
const CHALLENGE = 'Basic realm="Surogate"';
/** What the browser is told in place of a site's own 407: the proxy's words, with nothing the site chose. */
export const SITE_SIGN_IN = "This site answered as a proxy that wants a sign-in (407), which a site may not. Surogate's proxy did not pass its answer on.";
const digest = (text: string): Buffer => createHash("sha256").update(text).digest();

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

/**
 * The addresses a request for *to* is carried to whoever sends it: a public site's, which any
 * program on this computer reaches by itself. Null where it needs the browser's sign-in: one of
 * the proxy's own names, this computer, a private network, and whatever cannot be placed.
 */
export async function unsigned(to: { host: string; port: number } | null, options: ReachOptions = {}): Promise<string[] | null> {
  // A chat's own server is no public site's, whatever a lookup would say of its name.
  if (!to || chatPort(to.host, to.port) !== null) return null;
  // Its own names lead nowhere (RFC 6761), so they are no public site's.
  return admitted(to.host, to.port, options);
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
  // What the browser of the last launch signs in with, as a digest: the secret itself is given
  // to that launch and kept nowhere here. Null until a launch: nobody is signed in.
  private signed: Buffer | null = null;
  // What this browser may open of its chats' own servers; and what it carries to each port now, which ends when the port is taken back.
  private chats: Forwards | null = null;
  private readonly toChats = new Map<number, Set<Duplex>>();

  constructor(private readonly options: BrowserProxyOptions = {}) {
    this.server = createServer((request, response) => void this.forward(request, response));
    this.server.on("connect", (request: IncomingMessage, client: Duplex, head: Buffer) => void this.tunnel(request, client, head));
    this.server.on("clientError", (_error, socket: Duplex) => socket.destroy());
  }

  /**
   * A sign-in for the browser about to be launched, its secret 256 random bits made now. What
   * the launch before signed in with is taken no more.
   */
  signIn(): { username: string; password: string } {
    const password = randomBytes(32).toString("base64url");
    this.signed = digest(`Basic ${Buffer.from(`${SIGN_IN_AS}:${password}`).toString("base64")}`);
    return { username: SIGN_IN_AS, password };
  }

  // Whether a request or a tunnel carries the launch's sign-in, once: digests compared, so in constant time.
  private own(request: IncomingMessage): boolean {
    const said = request.headersDistinct["proxy-authorization"];
    return this.signed !== null && said?.length === 1 && timingSafeEqual(digest(said[0] ?? ""), this.signed);
  }

  /**
   * The ports of chats' own servers this browser may open from now on, the VM manager's *door*, and the
   * *key* its device knocks with there. What it carries to a port no longer among them ends now. Never a
   * port of the sandbox's own proxies, whoever names it.
   */
  forwards(ports: readonly number[], door: string, key: string): void {
    const allowed = new Set(ports.filter((port) => !SANDBOX_PORTS.has(port)));
    this.chats = { ports: allowed, door, key };
    for (const [port, carried] of this.toChats) {
      if (!allowed.has(port)) for (const connection of carried) connection.destroy();
    }
  }

  // A connection to *port* of the chat's servers it is forwarded to, through the manager's door, the
  // loopback's family *first* tried first there; or why there is none, in the door's own words for a
  // sandbox that is full. Nothing here is dialed.
  private async toChat(port: number, first: 4 | 6, gone: AbortSignal): Promise<Socket | NotCarried> {
    const chats = this.chats;
    if (!chats?.ports.has(port)) return "refused";
    const opened = await knock(chats.door, `${chats.key} ${port}${first === 6 ? " 6" : ""}`, gone);
    if ("status" in opened) return opened.status === 403 ? "refused" : opened.status === 502 && FULL.has(opened.reason) ? "busy" : "unreachable";
    // Taken back, or its browser gone, while the door answered.
    if (!this.chats?.ports.has(port) || gone.aborted) {
      opened.socket.destroy();
      return "refused";
    }
    const carried = this.toChats.get(port) ?? new Set<Duplex>();
    this.toChats.set(port, carried.add(opened.socket));
    opened.socket.once("close", () => {
      carried.delete(opened.socket);
      if (carried.size === 0 && this.toChats.get(port) === carried) this.toChats.delete(port);
    });
    return opened.socket;
  }

  /**
   * Whether a connection to *port* of a chat's servers is carried now, asked by one made through the door
   * and let go: "open", or why not (NotCarried). Never rejects.
   */
  async reaches(port: number): Promise<"open" | NotCarried> {
    const carried = await this.toChat(port, 4, new AbortController().signal).catch(() => "unreachable" as const);
    if (typeof carried === "string") return carried;
    carried.destroy();
    return "open";
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
    const addresses = await unsigned(to, this.options);
    // Not a public site's, and not its own browser's: the challenge, with nothing dialed and no word of why.
    if (!addresses && !this.own(request)) {
      return void client.end(`HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: ${CHALLENGE}\r\nContent-Length: 0\r\n\r\n`);
    }
    // The https upgrade's try at a check comes here; the plain request after it is answered.
    if (to && this.answered(to.host)) return void client.end("HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n");
    // Signed in, or it was challenged above: a chat's own server is carried into its sandbox, and never dialed here.
    const chat = to && !addresses ? chatPort(to.host, to.port) : null;
    if (to && chat !== null) return void this.socketToChat(chat, to.host === "[::1]" ? 6 : 4, client, head, gone.signal);
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

  // A tunnel to a chat's port: only a WebSocket that a page of a chat's own server opens is carried. The browser
  // says nothing of who asks with the CONNECT itself, so the tunnel is taken and its first bytes read, a moment
  // and so many of them: another site's socket, https, whose inside cannot be read, and anything else end it,
  // with no knock at the door.
  private async socketToChat(port: number, first: 4 | 6, client: Duplex, early: Buffer, gone: AbortSignal): Promise<void> {
    if (!this.chats?.ports.has(port)) return void client.end("HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n");
    client.write("HTTP/1.1 200 Connection Established\r\n\r\n");
    const head = await new Promise<Buffer | null>((resolve) => {
      let read = early;
      const timer = setTimeout(() => settle(null), this.options.handshakeMs ?? HANDSHAKE_MS);
      const settle = (whole: Buffer | null) => {
        clearTimeout(timer);
        client.off("data", more);
        client.off("close", left);
        client.pause();
        resolve(whole);
      };
      const left = () => settle(null);
      const more = (chunk: Buffer) => {
        read = Buffer.concat([read, chunk]);
        // A handshake begins with its GET, as a TLS hello does not.
        if (read.length > MAX_HANDSHAKE || !"GET ".startsWith(read.subarray(0, 4).toString("latin1"))) return settle(null);
        if (read.includes("\r\n\r\n")) settle(read);
      };
      client.on("data", more);
      client.once("close", left);
      more(Buffer.alloc(0));
    });
    const allowed = this.chats?.ports;
    if (!head || !allowed || !ownSocket(head.subarray(0, head.indexOf("\r\n\r\n")).toString("latin1"), allowed)) return void client.destroy();
    const upstream = await this.toChat(port, first, gone);
    if (typeof upstream === "string") return void client.destroy();
    this.keep(upstream);
    upstream.once("close", () => client.destroy());
    client.once("close", () => upstream.destroy());
    // A browser closes a tunnel whole, never half: its end ends the tunnel. A chat's server that ends its
    // half has said all it will: the tunnel goes once the browser has every byte of it.
    client.once("end", () => upstream.destroy());
    upstream.once("end", () => {
      if (client.writableFinished) client.destroy();
      else client.once("finish", () => client.destroy());
    });
    upstream.write(head);
    upstream.pipe(client);
    client.pipe(upstream);
    upstream.resume();
    client.resume();
  }

  // Plain http, as a browser sends it to a proxy: the absolute address, one request a connection.
  private async forward(request: IncomingMessage, response: ServerResponse): Promise<void> {
    let url: URL | null = null;
    try {
      url = new URL(request.url ?? "");
    } catch {
      // Not a proxy's request.
    }
    if (url?.protocol !== "http:") url = null;
    const port = Number(url?.port || 80);
    // Before the lookup, so a request the browser gave up on during it is not sent.
    const gone = new AbortController();
    response.once("close", () => gone.abort());
    const addresses = await unsigned(url && { host: url.hostname, port }, this.options);
    // Not a public site's, and not its own browser's: the challenge, with nothing dialed and no word of why.
    if (!addresses && !this.own(request)) return void response.writeHead(407, { "proxy-authenticate": CHALLENGE, "content-length": 0 }).end();
    if (!url) return void response.writeHead(400).end();
    if (this.answered(url.hostname)) return void response.writeHead(204).end();
    let socket: Socket;
    if (addresses) {
      try {
        socket = await dialFirst(addresses, port, gone.signal, this.options.connect);
      } catch {
        if (!response.headersSent) response.writeHead(502).end();
        return;
      }
    } else {
      // Signed in, or it was challenged above. A chat's own server is carried into its sandbox, for a request
      // of a chat's own page, and never dialed here; whatever else needs the sign-in is carried for nobody.
      const chat = chatPort(url.hostname, port);
      const allowed = this.chats?.ports;
      // A tab sent to a port not allowed, by its user or by a page: nobody is asked, and the tab is told what the port is.
      if (chat !== null && !allowed?.has(chat) && !SANDBOX_PORTS.has(chat) && request.headers["sec-fetch-mode"] === "navigate" && request.headers["sec-fetch-dest"] === "document") {
        const page = notOpenPage(chat);
        return void response.writeHead(403, { ...NOT_OPEN_HEADERS, "content-length": Buffer.byteLength(page) }).end(page);
      }
      if (chat === null || !allowed?.has(chat) || !ownRequest(request.method ?? "", request.headers, allowed)) return void response.writeHead(403).end();
      const carried = await this.toChat(chat, url.hostname === "[::1]" ? 6 : 4, gone.signal);
      if (typeof carried === "string") return void (response.headersSent || response.writeHead(NOT_CARRIED[carried]).end());
      socket = carried;
    }
    this.keep(socket);
    // The browser gone, partway through the answer or before it: the site's connection goes too.
    response.once("close", () => socket.destroy());
    // ponytail: one connection a request, none kept for the next; a pool if page loads ever show it.
    const upstream = httpRequest(
      {
        // One from the door comes paused, its first bytes kept.
        createConnection: () => socket.resume(), method: request.method, path: `${url.pathname}${url.search}`,
        // The target's own host, as RFC 9112 (3.2.2) has a proxy send it, whatever the client's said.
        headers: { ...passed(request.headers), host: url.host }, setHost: false,
      },
      (answer) => {
        // A 407 is the proxy's alone to answer: a browser that reads one on a request it signed takes its
        // sign-in for refused, and signs no more. A site's own is answered in the proxy's words, and the site let go.
        if (answer.statusCode === 407) {
          response.writeHead(502, { "content-type": "text/plain; charset=utf-8", "content-length": Buffer.byteLength(SITE_SIGN_IN) }).end(SITE_SIGN_IN);
          return void socket.destroy();
        }
        // A chat's server's answer is never kept: a port's next chat would be shown the former's pages and scripts
        // from the browser's cache, never asking its own server. A public site's keeps its own word.
        response.writeHead(answer.statusCode ?? 502, addresses ? passed(answer.headers) : { ...passed(answer.headers), "cache-control": "no-store" });
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
