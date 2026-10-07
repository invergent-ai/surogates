import { createServer as createHttp, request, type Server as HttpServer } from "node:http";
import { connect as connectTcp, createServer, type Server, type Socket } from "node:net";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { BrowserProxy } from "../src/browser/proxy.js";

// What a name leads to, and how often it was looked up: rebinding.example leads elsewhere, then here.
let lookups: Record<string, number>;
const names: Record<string, string[][]> = {
  "example.com": [["93.184.215.14"]],
  "rebinding.example": [["93.184.215.14"], ["127.0.0.1"]],
  "lan.example": [["192.168.1.5"]],
  "half-lan.example": [["93.184.215.14", "10.0.0.7"]],
  "refusing.example": [["192.0.2.1"]],
  // IPv6 answers that carry an IPv4 address: through the NAT64 prefix (a site's, and a LAN's), its
  // local-use prefix, 6to4, Teredo, IPv4-mapped and IPv4-compatible.
  "nat64-site.example": [["64:ff9b::5db8:d70e"]],
  "nat64-lan.example": [["64:ff9b::c0a8:101"]],
  "nat64-local-use.example": [["64:ff9b:1::5db8:d70e"]],
  "six-to-four.example": [["2002:5db8:d70e::1"]],
  "teredo.example": [["2001:0:4136:e378:8000:63bf:3f57:fefe"]],
  "mapped-lan.example": [["::ffff:192.168.1.1"]],
  "compatible-lan.example": [["::c0a8:101"]],
  // An answer with a zone id, which no URL spells.
  "nat64-scoped.example": [["64:ff9b::5db8:d70e%eth0"]],
  // A site that takes a connection, and then ignores the browser's close.
  "holding.example": [["192.0.2.9"]],
  // A site whose name takes SLOW_MS to look up.
  "slow.example": [["93.184.215.14"]],
};
const SLOW_MS = 200;
const sleep = (ms: number) => new Promise((done) => setTimeout(done, ms));
const resolve = async (name: string) => {
  const seen = (lookups[name] = (lookups[name] ?? 0) + 1);
  if (name === "slow.example") await sleep(SLOW_MS);
  const answers = names[name];
  if (!answers) return Promise.reject(new Error("ENOTFOUND"));
  return Promise.resolve(answers[Math.min(seen, answers.length) - 1] ?? []);
};

let echo: Server;
let web: HttpServer;
let hold: Server;
let ports: { echo: number; web: number; hold: number; closed: number };
// The proxy's connections to the sites that are open now, and the sites' ends of every connection.
let open: Set<Socket>;
let accepted: Set<Socket>;
let proxy: BrowserProxy;
let port: number;
// Each address:port the proxy dialed.
let dialed: string[];
// Each request the site was sent, as it saw it.
let seen: Array<{ method: string; url: string; host: string; headers: string[] }>;

beforeEach(async () => {
  lookups = {};
  dialed = [];
  seen = [];
  open = new Set();
  accepted = new Set();
  echo = createServer((socket) => socket.on("data", (chunk) => socket.write(`echo ${chunk.toString()}`)));
  web = createHttp((req, res) => {
    seen.push({ method: req.method ?? "", url: req.url ?? "", host: req.headers.host ?? "", headers: Object.keys(req.headers) });
    // An answer that streams until the browser goes, and one that never comes.
    if (req.url === "/stream") {
      const streaming = setInterval(() => res.write("x".repeat(1024)), 5);
      return void res.once("close", () => clearInterval(streaming)).writeHead(200);
    }
    if (req.url === "/never") return;
    res.writeHead(201, { "content-type": "text/plain" }).end("hello from the site");
  });
  // Reads all it is sent, and never closes, the browser's end or not.
  hold = createServer({ allowHalfOpen: true }, (socket) => socket.resume());
  const closed = createServer();
  for (const server of [echo, web, hold]) {
    server.on("connection", (socket: Socket) => void accepted.add(socket.once("close", () => accepted.delete(socket))));
  }
  await Promise.all([echo, web, hold, closed].map((server) => new Promise<void>((done) => server.listen(0, "127.0.0.1", () => done()))));
  const portOf = (server: Server | HttpServer) => (server.address() as { port: number }).port;
  ports = { echo: portOf(echo), web: portOf(web), hold: portOf(hold), closed: portOf(closed) };
  await new Promise<void>((done) => closed.close(() => done()));
  proxy = new BrowserProxy({
    resolve,
    local: () => ["127.0.0.1", "::1", "198.51.100.5", "2001:db8:1::5"],
    // This computer's networks: a public IPv4 range, as a campus's is, and a home LAN's global IPv6 prefix.
    subnets: () => ["198.51.100.5/24", "2001:db8:1::5/64"],
    // Every judged address reaches the servers here, by the port asked for; 192.0.2.1 refuses,
    // and 192.0.2.9 holds.
    connect: (address, to) => {
      dialed.push(`${address}:${to}`);
      const at = address === "192.0.2.1" ? ports.closed : address === "192.0.2.9" ? ports.hold : to === 443 ? ports.echo : ports.web;
      const socket = connectTcp({ host: "127.0.0.1", port: at });
      open.add(socket.once("close", () => open.delete(socket)));
      return socket;
    },
  });
  port = await proxy.listen();
});

afterEach(async () => {
  await proxy.close();
  for (const socket of accepted) socket.destroy();
  await Promise.all([echo, web, hold].map((server) => new Promise<void>((done) => server.close(() => done()))));
});

// How many of the proxy's connections to the sites are still open once a second has passed for them to close.
async function stillOpen(): Promise<number> {
  for (let waited = 0; open.size > 0 && waited < 1_000; waited += 25) await sleep(25);
  return open.size;
}

// A browser's raw connection to the proxy that has asked to CONNECT *authority*, once the proxy is looking it up.
async function asking(authority: string): Promise<Socket> {
  const browser = connectTcp({ host: "127.0.0.1", port });
  browser.on("error", () => {});
  browser.write(`CONNECT ${authority} HTTP/1.1\r\nHost: ${authority}\r\n\r\n`);
  while (!lookups[authority.slice(0, authority.lastIndexOf(":"))]) await sleep(5);
  return browser;
}

// CONNECT *authority* through the proxy, as the browser asks for https and WebSockets: its status, and the tunnel.
function connect(authority: string): Promise<{ status: number; socket: Socket }> {
  return new Promise((done, fail) => {
    const asked = request({ host: "127.0.0.1", port, method: "CONNECT", path: authority, headers: { host: authority } });
    asked.on("connect", (answer, socket) => done({ status: answer.statusCode ?? 0, socket }));
    asked.on("error", fail);
    asked.end();
  });
}

function said(socket: Socket, line: string): Promise<string> {
  return new Promise((done) => {
    socket.once("data", (chunk: Buffer) => done(chunk.toString()));
    socket.write(line);
  });
}

// A plain http request, as the browser sends one to a proxy: *path* is the request target as written.
function get(path: string, host: string): Promise<{ status: number; body: string }> {
  return new Promise((done, fail) => {
    const asked = request({ host: "127.0.0.1", port, method: "GET", path, headers: { host, "proxy-connection": "keep-alive" } }, (answer) => {
      let body = "";
      answer.on("data", (chunk: Buffer) => {
        body += chunk.toString();
      });
      answer.on("end", () => done({ status: answer.statusCode ?? 0, body }));
    });
    asked.on("error", fail);
    asked.end();
  });
}

describe("the browser's proxy", () => {
  it("tunnels to a site past this computer, at the address it judged", async () => {
    const { status, socket } = await connect("example.com:443");
    expect(status).toBe(200);
    expect(await said(socket, "hi")).toBe("echo hi");
    socket.destroy();
    expect(dialed).toEqual(["93.184.215.14:443"]);
  });

  it("refuses this computer's own and its private networks, however they are spelled, and dials nothing", async () => {
    for (const authority of [
      "127.0.0.1:9", "127.1:9", "[::1]:3000", "localhost:8080", "app.localhost:80", "0.0.0.0:80", "169.254.169.254:80",
      "192.168.1.1:80", "10.0.0.1:443", "[fe80::1]:80", "lan.example:443", "half-lan.example:443",
      "198.51.100.7:80", "[2001:db8:1::1234]:443",
    ]) {
      expect((await connect(authority)).status, authority).toBe(403);
    }
    expect(dialed).toEqual([]);
  });

  it("refuses an IPv4 address an IPv6 one carries as it refuses the IPv4 one, and 6to4, Teredo and local-use NAT64 whatever they carry", async () => {
    for (const authority of [
      // NAT64 to a LAN, this computer, the metadata address, and this computer's own public range.
      "[64:ff9b::c0a8:101]:80", "[64:ff9b::7f00:1]:80", "[64:ff9b::a9fe:a9fe]:80", "[64:ff9b::c633:6407]:80", "nat64-lan.example:80",
      "[64:ff9b:1::c0a8:101]:80", "[64:ff9b:1::5db8:d70e]:80", "nat64-local-use.example:80",
      "[2002:c0a8:101::1]:443", "[2002:5db8:d70e::1]:443", "six-to-four.example:443",
      "[2001:0:4136:e378:8000:63bf:3f57:fefe]:443", "teredo.example:443",
      "[::ffff:192.168.1.1]:80", "mapped-lan.example:80", "[::192.168.1.1]:80", "compatible-lan.example:80",
      "nat64-scoped.example:443",
    ]) {
      expect((await connect(authority)).status, authority).toBe(403);
    }
    expect(dialed).toEqual([]);
  });

  it("tunnels to a site past this computer through the NAT64 prefix, at the address it judged", async () => {
    for (const authority of ["[64:ff9b::5db8:d70e]:443", "nat64-site.example:443"]) {
      const { status, socket } = await connect(authority);
      expect(status, authority).toBe(200);
      socket.destroy();
    }
    expect(dialed).toEqual(["64:ff9b::5db8:d70e:443", "64:ff9b::5db8:d70e:443"]);
  });

  it("refuses what is no destination, and a name it cannot look up", async () => {
    for (const authority of ["*.example.com:443", "example.com", "missing.example:443", "example.com:99999"]) {
      expect((await connect(authority)).status, authority).toBe(403);
    }
    expect(dialed).toEqual([]);
  });

  it("judges every connection when it is made, and dials what it judged, never a later lookup", async () => {
    const first = await connect("rebinding.example:443");
    expect(first.status).toBe(200);
    first.socket.destroy();
    // The name now leads to this computer: the next connection is refused.
    expect((await connect("rebinding.example:443")).status).toBe(403);
    expect(dialed).toEqual(["93.184.215.14:443"]);
    expect(lookups["rebinding.example"]).toBe(2);
  });

  it("answers 502 for a site it judged that does not take the connection", async () => {
    expect((await connect("refusing.example:443")).status).toBe(502);
  });

  it("forwards a plain http request to the address it judged, with the site's own host and none of the proxy's headers", async () => {
    expect(await get("http://example.com/a/b?c=d", "example.com")).toEqual({ status: 201, body: "hello from the site" });
    expect(seen).toEqual([{ method: "GET", url: "/a/b?c=d", host: "example.com", headers: expect.not.arrayContaining(["proxy-connection"]) }]);
    expect(dialed).toEqual(["93.184.215.14:80"]);
    expect((await get("http://127.0.0.1:8080/", "127.0.0.1:8080")).status).toBe(403);
    expect((await get("http://lan.example/", "lan.example")).status).toBe(403);
  });

  it("answers a launch's check itself, at its own name and over the https upgrade's tunnel, and dials nothing for it", async () => {
    expect(proxy.checked("0123abcd")).toBe(false);
    expect(await get("http://0123abcd.proxy-check.invalid/", "0123abcd.proxy-check.invalid")).toEqual({ status: 204, body: "" });
    expect(proxy.checked("0123abcd")).toBe(true);
    expect((await connect("4567ef00.proxy-check.invalid:443")).status).toBe(403);
    expect(proxy.checked("4567ef00")).toBe(true);
    expect(dialed).toEqual([]);
  });

  it("leaves nothing open when the browser leaves a plain answer partway through", async () => {
    await new Promise<void>((done) => {
      const asked = request({ host: "127.0.0.1", port, path: "http://example.com/stream", headers: { host: "example.com" } }, (answer) => {
        answer.once("data", () => done(asked.destroy() && undefined));
      });
      asked.on("error", () => {});
      asked.end();
    });
    expect(await stillOpen()).toBe(0);
  });

  it("leaves nothing open when the browser gives up on a plain request before any answer", async () => {
    const asked = request({ host: "127.0.0.1", port, path: "http://example.com/never", headers: { host: "example.com" } });
    asked.on("error", () => {});
    asked.end();
    while (!seen.some(({ url }) => url === "/never")) await sleep(5);
    asked.destroy();
    expect(await stillOpen()).toBe(0);
  });

  it("leaves nothing open when the browser closes a tunnel whose site ignores the close", async () => {
    const { status, socket } = await connect("holding.example:443");
    socket.on("error", () => {});
    expect(status).toBe(200);
    const closed = new Promise((done) => socket.once("close", done));
    socket.resume().end();
    expect(await stillOpen()).toBe(0);
    await closed;
  });

  it("dials nothing for a tunnel the browser resets during its lookup", async () => {
    (await asking("slow.example:443")).resetAndDestroy();
    await sleep(2 * SLOW_MS);
    expect(dialed).toEqual([]);
    expect(await stillOpen()).toBe(0);
  });

  it("dials nothing for a tunnel still being looked up when it closes", async () => {
    await asking("slow.example:443");
    await proxy.close();
    await sleep(2 * SLOW_MS);
    expect(dialed).toEqual([]);
    expect(await stillOpen()).toBe(0);
  });

  it("answers 400 for what is not a proxy's request", async () => {
    expect((await get("/index.html", "example.com")).status).toBe(400);
    expect((await get("https://example.com/", "example.com")).status).toBe(400);
    expect(dialed).toEqual([]);
  });
});
