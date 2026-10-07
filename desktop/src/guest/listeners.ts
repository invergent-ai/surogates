// A root's proxies for its commands (spec, Section 11, Network), which its runner
// listens on in the root's own network namespace before any command runs: an HTTP
// proxy on 127.0.0.1:3128, for CONNECT and absolute-form requests, and SOCKS5 CONNECT
// on 127.0.0.1:1080. Each connection is parsed here, in the guest, as the root's own
// user, then goes to the agent through the root's socket (network.ts): its destination
// line, the agent's answer, then the connection's bytes.

import { createServer as createHttpServer, request, STATUS_CODES } from "node:http";
import { connect, createServer, type Server, type Socket } from "node:net";

export const HTTP_PORT = 3128;
export const SOCKS_PORT = 1080;
// What a command's proxy variables name (root.ts, rootEnvironment).
export const PROXY_URL = `http://127.0.0.1:${HTTP_PORT}`;
// The agent's answer is one short line.
const MAX_ANSWER = 512;
// How long a SOCKS client has, in all, to say where it goes.
const HANDSHAKE_MS = 10_000;
// What a client may send before its tunnel answers, kept for the tunnel: a TLS hello, and more.
const MAX_EARLY = 64 * 1024;

// A connection to a destination through the root's socket, or the status and reason that refused it.
export type Tunnel = { socket: Socket } | { status: number; reason: string };

/**
 * A connection to *destination* (host:port) through the root's socket at *path*, which goes
 * once *signal* aborts, answered or not: its client has gone. Never rejects.
 */
export function tunnel(path: string, destination: string, signal?: AbortSignal): Promise<Tunnel> {
  return new Promise((resolve) => {
    const socket = connect({ path, allowHalfOpen: true });
    let said = "";
    let settled = false;
    const settle = (value: Tunnel) => {
      if (settled) return;
      settled = true;
      socket.off("data", read);
      for (const event of ["error", "end", "close"]) socket.off(event, lost);
      // From here on, whoever carries it hears its end.
      socket.on("error", () => {});
      resolve(value);
    };
    // No answer: the agent's socket is gone, or it dropped the line.
    const lost = () => {
      socket.destroy();
      settle({ status: 502, reason: "sandbox" });
    };
    const read = (chunk: Buffer) => {
      said += chunk.toString("latin1");
      const end = said.indexOf("\n");
      if (end < 0) return void (said.length > MAX_ANSWER && lost());
      const [status = "", reason = ""] = said.slice(0, end).split(" ");
      if (status !== "200") {
        socket.destroy();
        return settle({ status: Number(status) || 502, reason });
      }
      socket.pause();
      // The destination's first bytes, which came with the answer.
      if (end + 1 < said.length) socket.unshift(Buffer.from(said.slice(end + 1), "latin1"));
      settle({ socket });
    };
    socket.on("data", read);
    for (const event of ["error", "end", "close"]) socket.on(event, lost);
    signal?.addEventListener("abort", lost, { once: true });
    socket.write(`${destination}\n`);
  });
}

// *a* and *b* carry each other's bytes until both have ended; either one failing ends both.
function join(a: Socket, b: Socket): void {
  a.on("error", () => b.destroy());
  b.on("error", () => a.destroy());
  a.pipe(b);
  b.pipe(a);
  a.resume();
  b.resume();
}

// Until its tunnel answers, *client* is read, so that it is seen to leave: a CONNECT or SOCKS
// client waits for its answer to send, so its end before then is one that gave up, and its
// tunnel goes. What it sends meanwhile, after *early*, is kept for the tunnel.
function waiting(client: Socket, early: Buffer): { signal: AbortSignal; answered(): Buffer } {
  const gone = new AbortController();
  const read = (chunk: Buffer) => {
    early = Buffer.concat([early, chunk]);
    if (early.length > MAX_EARLY) client.destroy();
  };
  const leave = () => gone.abort();
  client.on("data", read);
  client.once("end", leave);
  client.once("close", leave);
  return {
    signal: gone.signal,
    answered: () => {
      client.off("data", read);
      client.off("end", leave);
      client.off("close", leave);
      client.pause();
      return early;
    },
  };
}

// The client's request's headers, as it gave them, but those meant for the proxy.
function forwarded(raw: readonly string[]): string[] {
  const kept: string[] = [];
  for (let at = 0; at + 1 < raw.length; at += 2) {
    const name = raw[at] ?? "";
    if (!/^proxy-/i.test(name)) kept.push(name, raw[at + 1] ?? "");
  }
  return kept;
}

// HTTP: CONNECT for a tunnel, an absolute-form request for plain HTTP, one tunnel a request.
function httpProxy(path: string): Server {
  // A request has no bound, an upload or a long poll; a client that stalls in its headers is
  // dropped after a minute, node:http's default when a request has one.
  const server = createHttpServer({ requestTimeout: 0, headersTimeout: 60_000 });
  // A client that half-closes once its request is sent (an HTTP/1.0 upload, `nc -N`) still
  // hears the answer: node:http's own switch, which its types do not name.
  Object.assign(server, { httpAllowHalfOpen: true });
  server.on("connect", (req, client: Socket, head: Buffer) => {
    client.on("error", () => {});
    const wait = waiting(client, head);
    void tunnel(path, req.url ?? "", wait.signal).then((opened) => {
      if ("status" in opened) {
        return void client.end(`HTTP/1.1 ${opened.status} ${STATUS_CODES[opened.status] ?? ""}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n`);
      }
      const early = wait.answered();
      client.write("HTTP/1.1 200 Connection Established\r\n\r\n");
      if (early.length > 0) opened.socket.write(early);
      join(client, opened.socket);
    });
  });
  server.on("request", (req, res) => {
    let url: URL | null = null;
    try {
      url = new URL(req.url ?? "");
    } catch {
      // An origin-form request: this is a proxy, not a server.
    }
    if (url?.protocol !== "http:") return void res.writeHead(400, { "content-type": "text/plain" }).end("This is the sandbox's proxy\n");
    const target = url;
    // Its connection closed, before its answer or once it is given: its tunnel goes. So does an
    // HTTP/1.1 client that half-closes before its answer, as curl and Python give up; only an
    // HTTP/1.0 upload half-closes and waits for it.
    const gone = new AbortController();
    res.once("close", () => gone.abort());
    if (req.httpVersion !== "1.0") {
      const fin = () => void (res.headersSent || gone.abort());
      req.socket.once("end", fin);
      res.once("close", () => req.socket.off("end", fin));
    }
    void tunnel(path, `${target.hostname}:${target.port || 80}`, gone.signal).then((opened) => {
      if ("status" in opened) return void res.writeHead(opened.status, { "content-type": "text/plain" }).end(`${opened.reason}\n`);
      const upstream = request({
        // Paused since its answer: the request's own reader takes it from here.
        createConnection: () => opened.socket.resume(),
        method: req.method, path: `${target.pathname}${target.search}`, headers: forwarded(req.rawHeaders),
      }, (answer) => {
        res.writeHead(answer.statusCode ?? 502, answer.rawHeaders);
        answer.pipe(res);
      });
      upstream.on("error", () => res.destroy());
      req.pipe(upstream);
    });
  });
  return server;
}

// *count* bytes from *socket*, or null once it ends first.
function take(socket: Socket, count: number): Promise<Buffer | null> {
  return new Promise((resolve) => {
    const done = (value: Buffer | null) => {
      socket.off("readable", attempt);
      socket.off("end", ended);
      socket.off("close", ended);
      resolve(value);
    };
    const attempt = () => {
      const chunk = socket.read(count) as Buffer | null;
      if (chunk !== null) done(chunk);
    };
    const ended = () => done(null);
    socket.on("readable", attempt);
    socket.once("end", ended);
    socket.once("close", ended);
    attempt();
  });
}

// SOCKS5 (RFC 1928), CONNECT only and no authentication; a name goes to the host as it is.
async function socks(client: Socket, path: string, handshakeMs: number): Promise<void> {
  client.on("error", () => {});
  // From its connection until it has said where it goes, in all: the host's decision, its user's prompt included, is not counted.
  const handshake = setTimeout(() => client.destroy(), handshakeMs);
  client.once("close", () => clearTimeout(handshake));
  const reply = (code: number) => Buffer.from([5, code, 0, 1, 0, 0, 0, 0, 0, 0]);
  const greeting = await take(client, 2);
  if (!greeting || greeting[0] !== 5 || !greeting[1]) return void client.destroy();
  const methods = await take(client, greeting[1]);
  if (!methods?.includes(0)) return void client.end(Buffer.from([5, 0xff]));
  client.write(Buffer.from([5, 0]));
  const asked = await take(client, 4);
  if (!asked || asked[0] !== 5) return void client.destroy();
  let host: string | null = null;
  if (asked[3] === 1) host = (await take(client, 4))?.join(".") ?? null;
  else if (asked[3] === 3) {
    const length = (await take(client, 1))?.[0];
    host = length ? ((await take(client, length))?.toString("latin1") ?? null) : null;
  } else if (asked[3] === 4) {
    const address = await take(client, 16);
    host = address ? `[${Array.from({ length: 8 }, (_, n) => address.readUInt16BE(n * 2).toString(16)).join(":")}]` : null;
  } else return void client.end(reply(8));
  const port = await take(client, 2);
  if (host === null || !port) return void client.destroy();
  if (asked[1] !== 1) return void client.end(reply(7));
  clearTimeout(handshake);
  const wait = waiting(client, Buffer.alloc(0));
  const opened = await tunnel(path, `${host}:${port.readUInt16BE(0)}`, wait.signal);
  // Refused by the rules (2), refused by the destination (5), or not reached (4).
  if ("status" in opened) return void client.end(reply(opened.status === 403 ? 2 : opened.reason === "ECONNREFUSED" ? 5 : 4));
  const early = wait.answered();
  client.write(reply(0));
  if (early.length > 0) opened.socket.write(early);
  join(client, opened.socket);
}

/**
 * The root's two proxies, each listening once this resolves, their connections going
 * through the root's socket at *path*. Rejects when either cannot listen.
 */
export async function listen(path: string, ports = { http: HTTP_PORT, socks: SOCKS_PORT }, handshakeMs = HANDSHAKE_MS): Promise<Server[]> {
  const servers = [httpProxy(path), createServer({ allowHalfOpen: true }, (client) => void socks(client, path, handshakeMs))];
  await Promise.all(servers.map((server, at) => new Promise<void>((resolve, reject) => {
    server.once("error", reject);
    server.listen(at === 0 ? ports.http : ports.socks, "127.0.0.1", resolve);
  })));
  return servers;
}
