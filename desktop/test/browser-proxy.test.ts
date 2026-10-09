import { mkdtempSync, rmSync } from "node:fs";
import { Agent, createServer as createHttp, type IncomingHttpHeaders, request, type Server as HttpServer } from "node:http";
import { connect as connectTcp, createServer, type Server, type Socket } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { BrowserProxy, ownRequest, SITE_SIGN_IN, unsigned } from "../src/browser/proxy.js";

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
  // A public name that leads to this computer's loopback, as a rebinding site's does.
  "loop.example": [["127.0.0.1"]],
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
// What the launch's own browser sends with each request, and what another program has: nothing.
let signed: string;
const UNSIGNED = null;
const signIn = ({ username, password }: { username: string; password: string }) => `Basic ${Buffer.from(`${username}:${password}`).toString("base64")}`;
const signing = (as: string | null): Record<string, string> => (as === null ? {} : { "proxy-authorization": as });
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
    // A site that hangs up partway through its answer.
    if (req.url === "/cut") {
      res.writeHead(200, { "content-length": "100000" }).write("x".repeat(1000));
      return void setTimeout(() => res.socket?.destroy(), 50);
    }
    // A site that asks for a proxy's sign-in, as only a proxy may.
    if (req.url === "/sign-in") return void res.writeHead(407, { "proxy-authenticate": 'Basic realm="site"', "x-of-the-site": "chosen" }).end("the site's own words");
    if (req.url === "/sign-in-bare") return void res.writeHead(407).end("the site's own words");
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
  signed = signIn(proxy.signIn());
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
  browser.write(`CONNECT ${authority} HTTP/1.1\r\nHost: ${authority}\r\nProxy-Authorization: ${signed}\r\n\r\n`);
  while (!lookups[authority.slice(0, authority.lastIndexOf(":"))]) await sleep(5);
  return browser;
}

// CONNECT *authority* through the proxy, as the browser asks for https and WebSockets: its status, and the tunnel.
function connect(authority: string, as: string | null = signed): Promise<{ status: number; socket: Socket }> {
  return new Promise((done, fail) => {
    const asked = request({ host: "127.0.0.1", port, method: "CONNECT", path: authority, headers: { host: authority, ...signing(as) } });
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
function get(path: string, host: string, as: string | null = signed): Promise<{ status: number; body: string }> {
  return new Promise((done, fail) => {
    const asked = request({ host: "127.0.0.1", port, method: "GET", path, headers: { host, "proxy-connection": "keep-alive", ...signing(as) } }, (answer) => {
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

// Whatever *written* is, sent to the proxy as it stands by a program of this computer's: all the proxy answers, until it closes or a moment after its answer's head.
function raw(written: string): Promise<string> {
  return new Promise((done) => {
    const program = connectTcp({ host: "127.0.0.1", port });
    let answer = "";
    const end = () => {
      program.destroy();
      done(answer);
    };
    program.on("data", (chunk: Buffer) => {
      answer += chunk.toString();
      // Its head has come: a moment more, for anything it says after.
      if (answer.includes("\r\n\r\n")) setTimeout(end, 30);
    });
    program.on("error", end);
    program.on("close", end);
    program.on("connect", () => program.write(written));
    setTimeout(end, 500);
  });
}
// The proxy's challenge, and nothing else: no body, no word of what it would have carried.
const CHALLENGED = /^HTTP\/1\.1 407 Proxy Authentication Required\r\n(?:proxy-authenticate: Basic realm="Surogate"\r\ncontent-length: 0\r\n(?:Date: [^\r]+\r\n)?(?:Connection: [^\r]+\r\n)?(?:Keep-Alive: [^\r]+\r\n)?|Proxy-Authenticate: Basic realm="Surogate"\r\nContent-Length: 0\r\n)\r\n$/;
// What needs the sign-in today: each is refused to its own browser too, but a launch's check, which is answered.
const OWN = ["127.0.0.1", "localhost", "[::1]", "app.localhost", "0.0.0.0", "192.168.1.1", "lan.example", "rebinding-now.example", "missing.example", "[64:ff9b::7f00:1]", "0123abcd.proxy-check.invalid"];

describe("the browser's proxy", () => {
  it("places a request as one anyone may send only when it leads to a public site: its own names, this computer, a private network and what it cannot place need the sign-in", async () => {
    const options = { resolve, local: () => ["127.0.0.1", "::1"], subnets: () => [] };
    expect(await unsigned({ host: "example.com", port: 80 }, options)).toEqual(["93.184.215.14"]);
    for (const host of ["127.0.0.1", "localhost", "::1", "app.localhost", "0.0.0.0", "192.168.1.1", "lan.example", "half-lan.example", "missing.example", "*.example.com", ""]) {
      expect(await unsigned({ host, port: 3000 }, options), host).toBeNull();
    }
    expect(await unsigned({ host: "example.com", port: 99999 }, options)).toBeNull();
    expect(await unsigned(null, options)).toBeNull();
    // One of its own names leads nowhere: nothing is looked up for it.
    expect(await unsigned({ host: "0123abcd.proxy-check.invalid", port: 80 }, options)).toBeNull();
    expect(lookups["0123abcd.proxy-check.invalid"]).toBeUndefined();
  });

  it("carries a public site for a program that does not sign in, as for its browser, and its browser's sign-in reaches no site", async () => {
    expect(await get("http://example.com/a", "example.com", UNSIGNED)).toEqual({ status: 201, body: "hello from the site" });
    expect(await get("http://example.com/b", "example.com", "Basic bm90Om1pbmU=")).toEqual({ status: 201, body: "hello from the site" });
    const { status, socket } = await connect("example.com:443", UNSIGNED);
    expect(status).toBe(200);
    expect(await said(socket, "hi")).toBe("echo hi");
    socket.destroy();
    expect(dialed).toEqual(["93.184.215.14:80", "93.184.215.14:80", "93.184.215.14:443"]);
    expect(seen.map(({ headers }) => headers.includes("proxy-authorization"))).toEqual([false, false]);
  });

  it("answers its challenge and nothing else to what needs the sign-in and comes without it, whatever its headers say, and dials nothing", async () => {
    names["rebinding-now.example"] = [["127.0.0.1"]];
    proxy.expect("0123abcd");
    for (const host of OWN) {
      const at = `${host}:3000`;
      for (const written of [
        `GET http://${at}/ HTTP/1.1\r\nHost: ${at}\r\n\r\n`,
        // A browser's own headers, written by hand.
        `GET http://${at}/ HTTP/1.1\r\nHost: ${at}\r\nSec-Fetch-Site: same-origin\r\nSec-Fetch-Mode: cors\r\nSec-Fetch-Dest: empty\r\n\r\n`,
        `POST http://${at}/ HTTP/1.1\r\nHost: ${at}\r\nSec-Fetch-Site: none\r\nSec-Fetch-Mode: navigate\r\nSec-Fetch-User: ?1\r\nContent-Length: 0\r\n\r\n`,
        `DELETE http://${at}/ HTTP/1.1\r\nHost: ${at}\r\nSec-Fetch-Site: cross-site\r\nOrigin: http://localhost:3000\r\n\r\n`,
        // A tunnel, and a WebSocket's handshake written into it before any answer.
        `CONNECT ${at} HTTP/1.1\r\nHost: ${at}\r\n\r\n`,
        `CONNECT ${at} HTTP/1.1\r\nHost: ${at}\r\n\r\nGET /ws HTTP/1.1\r\nHost: ${at}\r\nOrigin: http://localhost:3000\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n`,
      ]) {
        expect(await raw(written), written).toMatch(CHALLENGED);
      }
    }
    // And what is no proxy's request at all.
    for (const written of ["GET /index.html HTTP/1.1\r\nHost: localhost:3000\r\n\r\n", "GET https://example.com/ HTTP/1.1\r\nHost: example.com\r\n\r\n", "CONNECT example.com HTTP/1.1\r\n\r\n"]) {
      expect(await raw(written), written).toMatch(CHALLENGED);
    }
    expect(dialed).toEqual([]);
    expect(seen).toEqual([]);
    expect(proxy.checked("0123abcd")).toBe(false);
  });

  it("takes no sign-in but its launch's own: another secret, another name, the launch before's, another scheme, or two of them", async () => {
    const before = signed;
    const { username, password } = proxy.signIn();
    signed = signIn({ username, password });
    const wrong = [
      before,
      signIn({ username, password: `${password.slice(0, -1)}${password.endsWith("A") ? "B" : "A"}` }),
      signIn({ username: "another", password }),
      signIn({ username, password: "" }),
      // Of another length, short and long: told apart by nothing.
      signIn({ username, password: "x" }),
      signIn({ username, password: "x".repeat(4096) }),
      `Bearer ${password}`,
      signed.toLowerCase(),
      ` ${signed}x`,
      "",
    ];
    proxy.expect("0123abcd");
    for (const as of wrong) {
      expect((await get("http://0123abcd.proxy-check.invalid/", "0123abcd.proxy-check.invalid", as)).status, as).toBe(407);
      expect((await connect("localhost:3000", as)).status, as).toBe(407);
    }
    // Two sign-ins, the right one first or last: one request never carries two.
    for (const pair of [[signed, before], [before, signed], [signed, signed]]) {
      const twice = pair.map((as) => `Proxy-Authorization: ${as}\r\n`).join("");
      expect(await raw(`GET http://0123abcd.proxy-check.invalid/ HTTP/1.1\r\nHost: 0123abcd.proxy-check.invalid\r\n${twice}\r\n`)).toMatch(CHALLENGED);
      expect(await raw(`CONNECT localhost:3000 HTTP/1.1\r\nHost: localhost:3000\r\n${twice}\r\n`)).toMatch(CHALLENGED);
    }
    expect(proxy.checked("0123abcd")).toBe(false);
    proxy.expect("0123abcd");
    expect((await get("http://0123abcd.proxy-check.invalid/", "0123abcd.proxy-check.invalid")).status).toBe(204);
    expect(proxy.checked("0123abcd")).toBe(true);
  });

  it("takes no sign-in at all before a launch has made one", async () => {
    const fresh = new BrowserProxy({ resolve });
    const at = await fresh.listen();
    try {
      for (const as of [signed, "", "Basic ", signIn({ username: "surogate", password: "" })]) {
        const answer = await new Promise<number>((done, fail) => {
          const headers = { host: "0123abcd.proxy-check.invalid", "proxy-authorization": as };
          request({ host: "127.0.0.1", port: at, path: "http://0123abcd.proxy-check.invalid/", headers }, (answered) => done(answered.resume().statusCode ?? 0)).on("error", fail).end();
        });
        expect(answer, as).toBe(407);
      }
    } finally {
      await fresh.close();
    }
  });

  it("keeps a launch's secret nowhere: what it holds of it tells nobody the sign-in", () => {
    const { password } = proxy.signIn();
    expect(password).toMatch(/^[A-Za-z0-9_-]{43}$/);
    expect(proxy.signIn().password).not.toBe(password);
    const { password: last } = proxy.signIn();
    // Every field of the proxy's but its server, a buffer in each spelling.
    const kept = Object.entries(proxy).filter(([name]) => name !== "server")
      .map(([, value]) => (Buffer.isBuffer(value) ? ["latin1", "hex", "base64", "base64url"].map((spelling) => value.toString(spelling as BufferEncoding)).join(" ") : JSON.stringify(value) ?? "")).join(" ");
    for (const spelled of [last, Buffer.from(`surogate:${last}`).toString("base64"), Buffer.from(last).toString("hex")]) expect(kept).not.toContain(spelled);
  });

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

  it("sends a site the target's own host and no proxy's sign-in, and gives the browser none of a site's own: 502 in its place, in the proxy's words, and the site let go", async () => {
    for (const path of ["/sign-in", "/sign-in-bare"]) {
      seen = [];
      const answer = await new Promise<{ status: number; headers: IncomingHttpHeaders; body: string }>((done, fail) => {
        const headers = { host: "intranet.corp", "proxy-authorization": signed };
        const asked = request({ host: "127.0.0.1", port, path: `http://example.com:8080${path}`, headers }, (answered) => {
          let body = "";
          answered.on("data", (chunk: Buffer) => (body += chunk.toString()));
          answered.on("end", () => done({ status: answered.statusCode ?? 0, headers: answered.headers, body }));
        });
        asked.on("error", fail);
        asked.end();
      });
      // A browser reads a 407 on a request it signed as its proxy refusing the sign-in, and signs no more.
      expect([answer.status, answer.body], path).toEqual([502, SITE_SIGN_IN]);
      expect(Object.keys(answer.headers).sort().filter((name) => !["connection", "date", "keep-alive"].includes(name)), path).toEqual(["content-length", "content-type"]);
      expect(seen).toEqual([{ method: "GET", url: path, host: "example.com:8080", headers: expect.not.arrayContaining(["proxy-authorization"]) }]);
      expect(await stillOpen()).toBe(0);
    }
  });

  it("answers a launch's check itself, at its own name and over the https upgrade's tunnel, and dials nothing for it", async () => {
    proxy.expect("0123abcd");
    proxy.expect("4567ef00");
    expect(proxy.checked("0123abcd")).toBe(false);
    proxy.expect("0123abcd");
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

  it("ends the browser's plain answer when its site hangs up partway through it", async () => {
    const ended = await new Promise<string>((done) => {
      const asked = request({ host: "127.0.0.1", port, path: "http://example.com/cut", headers: { host: "example.com" } }, (answer) => {
        answer.resume();
        answer.once("end", () => done("ended whole"));
        answer.once("close", () => done(answer.complete ? "ended whole" : "cut short"));
      });
      asked.on("error", () => done("cut short"));
      asked.end();
      setTimeout(() => done("still open"), 2_000);
    });
    expect(ended).toBe("cut short");
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

  it("forgets a check once asked about it, and keeps none a launch does not wait on, however many a page asks for", async () => {
    proxy.expect("0123abcd");
    await get("http://0123abcd.proxy-check.invalid/", "0123abcd.proxy-check.invalid");
    expect(proxy.checked("0123abcd")).toBe(true);
    expect(proxy.checked("0123abcd")).toBe(false);
    for (let at = 0; at < 40; at += 1) await get(`http://page${at}.proxy-check.invalid/`, `page${at}.proxy-check.invalid`);
    expect((proxy as unknown as { checks: Map<string, boolean> }).checks.size).toBe(0);
    expect(proxy.checked("page0")).toBe(false);
  });

  it("keeps a launch's own check however many a page asks for meanwhile", async () => {
    proxy.expect("launch1");
    await get("http://launch1.proxy-check.invalid/", "launch1.proxy-check.invalid");
    for (let at = 0; at < 40; at += 1) await get(`http://page${at}.proxy-check.invalid/`, `page${at}.proxy-check.invalid`);
    expect(proxy.checked("launch1")).toBe(true);
  });

  it("closes at once, with a browser's connection kept alive and a plain request still waiting on its site", async () => {
    const kept = new Agent({ keepAlive: true });
    await new Promise((done) => {
      request({ host: "127.0.0.1", port, path: "http://example.com/", headers: { host: "example.com" }, agent: kept }, (answer) => answer.resume().on("end", done)).end();
    });
    const waiting = request({ host: "127.0.0.1", port, path: "http://example.com/never", headers: { host: "example.com" }, agent: new Agent({ keepAlive: true }) });
    waiting.on("error", () => {});
    waiting.end();
    while (!seen.some(({ url }) => url === "/never")) await sleep(5);
    const started = performance.now();
    await proxy.close();
    expect(performance.now() - started).toBeLessThan(1_000);
    kept.destroy();
  });

  it("answers 400 for what is not a proxy's request", async () => {
    expect((await get("/index.html", "example.com")).status).toBe(400);
    expect((await get("https://example.com/", "example.com")).status).toBe(400);
    expect(dialed).toEqual([]);
  });
});

describe("a chat's own servers, through the browser's proxy", () => {
  const KEY = "ab".repeat(32);
  // The VM manager's door (vm/inbound.ts), standing here: each knock, and behind it ports 3000 and 3001 the web
  // server above, nothing on 3002, and every connection made behind it.
  let folder: string;
  let door: Server;
  let knocks: string[];
  let behind: Set<Socket>;
  // How long the door takes to answer a knock it carries.
  let slow: number;
  const path = () => join(folder, "browser.sock");
  // What the browser itself says of its own navigation, and its user's.
  const own = { "sec-fetch-site": "none", "sec-fetch-mode": "navigate", "sec-fetch-dest": "document" };
  // A plain request as the browser sends one, with the headers it adds itself, signed in as *as*.
  const fetched = (target: string, headers: Record<string, string> = own, method = "GET", as: string | null = signed) => new Promise<{ status: number; body: string }>((done, fail) => {
    const asked = request({ host: "127.0.0.1", port, method, path: target, headers: { host: new URL(target).host, ...headers, ...signing(as) } }, (answer) => {
      let body = "";
      answer.on("data", (chunk: Buffer) => {
        body += chunk.toString();
      });
      answer.on("end", () => done({ status: answer.statusCode ?? 0, body }));
    });
    asked.on("error", fail);
    asked.end();
  });
  // This computer's own service on a port, on both of its loopback's families: what it heard.
  const ownService = async () => {
    const heard = { hits: 0, port: 0, close: async () => {} };
    for (;;) {
      const [six, four] = [0, 1].map(() => createServer((socket) => {
        heard.hits += 1;
        socket.destroy();
      })) as [Server, Server];
      await new Promise<void>((done) => six.listen(0, "::1", done));
      heard.port = (six.address() as { port: number }).port;
      heard.close = async () => void (await Promise.all([six, four].map((server) => new Promise<void>((done) => server.close(() => done())))));
      if (await new Promise<boolean>((done) => four.once("error", () => done(false)).listen(heard.port, "127.0.0.1", () => done(true)))) return heard;
      await new Promise<void>((done) => six.close(() => done()));
    }
  };

  beforeEach(async () => {
    folder = mkdtempSync(join(tmpdir(), "browser-door-"));
    knocks = [];
    behind = new Set();
    slow = 0;
    door = createServer((socket) => {
      socket.on("error", () => {});
      socket.once("data", (chunk: Buffer) => {
        const line = chunk.toString().trimEnd();
        knocks.push(line);
        if (!line.startsWith(`${KEY} `)) return void socket.end("403 refused\n");
        if (!/^[0-9a-f]{64} 300[01]( 6)?$/.test(line)) return void socket.end("502 ECONNREFUSED\n");
        const upstream = connectTcp({ host: "127.0.0.1", port: ports.web });
        behind.add(upstream.once("close", () => behind.delete(upstream)));
        upstream.on("error", () => socket.destroy());
        upstream.once("connect", async () => {
          await sleep(slow);
          socket.write("200\n");
          socket.pipe(upstream);
          upstream.pipe(socket);
        });
        socket.once("close", () => upstream.destroy());
        upstream.once("close", () => socket.destroy());
      });
    });
    await new Promise<void>((done) => door.listen(path(), done));
    proxy.forwards([3000, 3001, 3002], path(), KEY);
  });

  afterEach(async () => {
    for (const socket of behind) socket.destroy();
    await new Promise<void>((done) => door.close(() => done()));
    rmSync(folder, { recursive: true, force: true });
  });

  it("carries a request for an allowed port only when it carries its launch's sign-in: whatever another program writes is challenged, with no knock at the door", async () => {
    const service = await ownService();
    try {
      proxy.forwards([3000, service.port], path(), KEY);
      for (const at of ["localhost:3000", "127.0.0.1:3000", "[::1]:3000", "LOCALHOST:3000", "localhost.:3000", `localhost:${service.port}`, `127.0.0.1:${service.port}`, `[::1]:${service.port}`]) {
        for (const written of [
          `GET http://${at}/ HTTP/1.1\r\nHost: ${at}\r\nSec-Fetch-Site: none\r\nSec-Fetch-Mode: navigate\r\nSec-Fetch-Dest: document\r\n\r\n`,
          // A browser's own headers, written by hand: a page's own request, forged.
          `GET http://${at}/ HTTP/1.1\r\nHost: ${at}\r\nSec-Fetch-Site: same-origin\r\nSec-Fetch-Mode: cors\r\nSec-Fetch-Dest: empty\r\n\r\n`,
          // A WebSocket's handshake, written by hand: as a plain request, and into a tunnel before any answer.
          `GET http://${at}/ws HTTP/1.1\r\nHost: ${at}\r\nOrigin: http://${at}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n`,
          `CONNECT ${at} HTTP/1.1\r\nHost: ${at}\r\n\r\nGET /ws HTTP/1.1\r\nHost: ${at}\r\nOrigin: http://${at}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n`,
          // A sign-in that is not the launch's, and the launch's own said twice.
          `GET http://${at}/ HTTP/1.1\r\nHost: ${at}\r\nSec-Fetch-Site: none\r\nProxy-Authorization: Basic c3Vyb2dhdGU6Z3Vlc3M=\r\n\r\n`,
          `GET http://${at}/ HTTP/1.1\r\nHost: ${at}\r\nSec-Fetch-Site: none\r\nProxy-Authorization: ${signed}\r\nProxy-Authorization: ${signed}\r\n\r\n`,
        ]) {
          expect(await raw(written), written).toMatch(CHALLENGED);
        }
      }
      // Nor is a name of a chat's server looked up for it.
      expect([knocks, seen, dialed, service.hits, lookups]).toEqual([[], [], [], 0, {}]);
      // The same request, from the launch's own browser: carried through the door, and nowhere on this computer.
      expect(await fetched("http://localhost:3000/a?b=c")).toEqual({ status: 201, body: "hello from the site" });
      expect((await fetched(`http://localhost:${service.port}/`)).status).toBe(502);
      expect([knocks, dialed, service.hits]).toEqual([[`${KEY} 3000`, `${KEY} ${service.port}`], [], 0]);
      // The sign-in goes no further than the proxy.
      expect(seen).toEqual([{ method: "GET", url: "/a?b=c", host: "localhost:3000", headers: expect.not.arrayContaining(["proxy-authorization"]) }]);
    } finally {
      await service.close();
    }
  });

  it("carries a request for an allowed port to the manager's door under its device's key, by each name of this computer's loopback, the family its name says first, and dials nothing here", async () => {
    const hosts = ["localhost", "127.0.0.1", "[::1]", "2130706433", "0x7f.1", "0177.0.0.1", "127.1", "LOCALHOST"];
    for (const host of hosts) expect(await fetched(`http://${host}:3000/a?b=c`), host).toEqual({ status: 201, body: "hello from the site" });
    // Only the address that names IPv6's loopback has the door try that family first.
    expect(knocks).toEqual(hosts.map((host) => (host === "[::1]" ? `${KEY} 3000 6` : `${KEY} 3000`)));
    // The chat's server sees the name its page was opened by.
    expect(seen.map(({ host }) => host)).toEqual([
      "localhost:3000", "127.0.0.1:3000", "[::1]:3000", "127.0.0.1:3000", "127.0.0.1:3000", "127.0.0.1:3000", "127.0.0.1:3000", "localhost:3000",
    ]);
    expect(dialed).toEqual([]);
  });

  it("refuses a port not allowed, either of the sandbox's own proxies' ports, every other spelling of this computer on an allowed one, and every tunnel, without a knock or a dial", async () => {
    proxy.forwards([3000, 3128, 1080], path(), KEY);
    for (const target of [
      "http://localhost:3003/", "http://127.0.0.1:80/", "http://[::1]:8080/",
      // The sandbox's own proxies for its commands, though the app named them.
      "http://localhost:3128/", "http://127.0.0.1:1080/",
      // On a port that is allowed: only the loopback's three names are a chat's servers.
      "http://0.0.0.0:3000/", "http://127.0.0.2:3000/", "http://app.localhost:3000/", "http://localhost.localdomain:3000/",
      // A name with a dot after it is another origin to the browser: no fourth name for a chat's server.
      "http://localhost.:3000/", "http://LOCALHOST.:3000/",
      "http://[::ffff:127.0.0.1]:3000/", "http://[::]:3000/", "http://[::ffff:7f00:1]:3000/", "http://[64:ff9b::7f00:1]:3000/",
      // A public name that leads here, as a rebinding site's does, and this computer's own address on its network.
      "http://loop.example:3000/", "http://rebinding.example:3000/", "http://198.51.100.5:3000/", "http://lan.example:3000/",
    ]) {
      expect((await fetched(target)).status, target).toBe(target.includes("rebinding") ? 201 : 403);
    }
    // The rebinding name led elsewhere at its first lookup, and is refused once it leads here.
    expect((await fetched("http://rebinding.example:3000/")).status).toBe(403);
    // A tunnel to a chat's port is carried for nobody, allowed or not: https, and a page's socket.
    for (const authority of ["localhost:3000", "127.0.0.1:3000", "[::1]:3000", "localhost:3003", "localhost.:3000", "0.0.0.0:3000", "127.0.0.2:3000", "app.localhost:3000", "[::ffff:127.0.0.1]:3000", "loop.example:3000"]) {
      expect((await connect(authority)).status, authority).toBe(403);
    }
    expect(knocks).toEqual([]);
    expect(dialed).toEqual(["93.184.215.14:3000"]);
  });

  it("carries only what the browser says comes from a page of a port allowed now, its user or its agent, and a link followed there", async () => {
    const fromSite = { origin: "http://evil.example", referer: "http://evil.example/" };
    const cors = { "sec-fetch-site": "cross-site", "sec-fetch-mode": "cors", "sec-fetch-dest": "empty" };
    const refused: Array<[string, Record<string, string>, string?]> = [
      ["a page of another site fetching it", { ...cors, ...fromSite }],
      ["its image, with no origin said", { "sec-fetch-site": "cross-site", "sec-fetch-mode": "no-cors", "sec-fetch-dest": "image", referer: "http://evil.example/" }],
      ["its script", { "sec-fetch-site": "cross-site", "sec-fetch-mode": "no-cors", "sec-fetch-dest": "script", referer: "http://evil.example/" }],
      ["its frame", { "sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate", "sec-fetch-dest": "iframe", referer: "http://evil.example/" }],
      ["its form posted there", { "sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate", "sec-fetch-dest": "document", ...fromSite }, "POST"],
      ["its beacon", { "sec-fetch-site": "cross-site", "sec-fetch-mode": "no-cors", "sec-fetch-dest": "empty", ...fromSite }, "POST"],
      ["its service worker's script", { "sec-fetch-site": "cross-site", "sec-fetch-mode": "same-origin", "sec-fetch-dest": "serviceworker", referer: "http://evil.example/" }],
      ["a page it has the browser load ahead of any visit", { "sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate", "sec-fetch-dest": "document", "sec-purpose": "prefetch;prerender" }],
      ["a link followed by another method", { "sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate", "sec-fetch-dest": "document", referer: "http://evil.example/" }, "HEAD"],
      ["a page that says no referrer", { "sec-fetch-site": "cross-site", "sec-fetch-mode": "no-cors", "sec-fetch-dest": "image" }],
      ["an opaque origin's", { ...cors, origin: "null" }],
      ["a name that only looks like this computer's", { ...cors, origin: "http://localhost.evil.example:3000" }],
      ["an https page of this computer's name, which no chat's server is", { ...cors, origin: "https://localhost:3000" }],
      ["a page of this computer's name on a port not allowed", { ...cors, origin: "http://localhost:1" }],
      ["one on a port not allowed, by its referrer", { "sec-fetch-site": "cross-site", "sec-fetch-mode": "no-cors", "sec-fetch-dest": "image", referer: "http://127.0.0.1:5173/" }],
      ["a page of another site whose referrer is put forward as a chat's", { ...cors, origin: "http://evil.example", referer: "http://localhost:3000/" }],
      ["a request that says nothing of where it comes from, as no browser's does", {}],
      ["one that says something else", { "sec-fetch-site": "cross-origin" }],
      ["one that says something else, as a link followed", { "sec-fetch-site": "cross-origin", "sec-fetch-mode": "navigate", "sec-fetch-dest": "document" }],
      ["a document asked for by no navigation", { "sec-fetch-site": "cross-site", "sec-fetch-mode": "no-cors", "sec-fetch-dest": "document", referer: "http://evil.example/" }],
    ];
    for (const [what, headers, method] of refused) expect((await fetched("http://localhost:3000/", headers, method)).status, what).toBe(403);
    // A port taken back is no chat's page from then on.
    proxy.forwards([3000], path(), KEY);
    expect((await fetched("http://localhost:3000/", { ...cors, origin: "http://127.0.0.1:3001" })).status).toBe(403);
    proxy.forwards([3000, 3001, 3002], path(), KEY);
    expect(knocks).toEqual([]);
    const carried: Array<[string, Record<string, string>, string?]> = [
      ["the agent's own navigation, or its user's", own],
      ["the page's own request", { "sec-fetch-site": "same-origin", "sec-fetch-mode": "cors", "sec-fetch-dest": "empty" }, "POST"],
      ["a page on another port of the same name", { "sec-fetch-site": "same-site", "sec-fetch-mode": "cors", "sec-fetch-dest": "empty", origin: "http://localhost:3001" }, "POST"],
      ["a chat's page under the loopback's other name", { ...cors, origin: "http://127.0.0.1:3001" }, "POST"],
      ["its image, by its referrer", { "sec-fetch-site": "cross-site", "sec-fetch-mode": "no-cors", "sec-fetch-dest": "image", referer: "http://[::1]:3002/" }],
      ["a link followed from another site", { "sec-fetch-site": "cross-site", "sec-fetch-mode": "navigate", "sec-fetch-dest": "document", referer: "http://evil.example/" }],
    ];
    for (const [what, headers, method] of carried) expect((await fetched("http://localhost:3000/", headers, method)).status, what).toBe(201);
    expect(knocks).toHaveLength(carried.length);
    // The rule alone, as the proxy applies it.
    const allowed = new Set([3000]);
    expect([
      ownRequest("GET", own, allowed), ownRequest("POST", { ...own, "sec-fetch-site": "cross-site" }, allowed), ownRequest("GET", {}, allowed),
      ownRequest("GET", { ...cors, origin: "http://localhost:3000" }, allowed), ownRequest("GET", { ...cors, origin: "http://localhost:3001" }, allowed),
    ]).toEqual([true, false, false, true, false]);
    expect(dialed).toEqual([]);
  });

  it("gives the browser none of a chat's server's own 407: 502 in its place, in the proxy's words, and the server let go", async () => {
    expect(await fetched("http://localhost:3000/sign-in")).toEqual({ status: 502, body: SITE_SIGN_IN });
    await vi.waitFor(() => expect(behind.size).toBe(0));
  });

  it("answers 502 for a port nothing takes a connection on, and a door that is not there; 403 for one the door refuses; and says which when asked", async () => {
    expect([(await fetched("http://localhost:3002/")).status, await proxy.reaches(3002), await proxy.reaches(3000)]).toEqual([502, "unreachable", "open"]);
    // Asked by a knock that says nothing, and is let go.
    await vi.waitFor(() => expect(behind.size).toBe(0));
    expect(seen).toEqual([]);
    // A port not allowed is asked of nobody.
    const before = knocks.length;
    expect([await proxy.reaches(3003), knocks.length]).toEqual(["refused", before]);
    // Another device's key at the door: refused there.
    proxy.forwards([3000], path(), "cd".repeat(32));
    expect([(await fetched("http://localhost:3000/")).status, await proxy.reaches(3000)]).toEqual([403, "refused"]);
    // No guest runs: its door is not there.
    proxy.forwards([3000], join(folder, "gone.sock"), KEY);
    expect([(await fetched("http://localhost:3000/")).status, await proxy.reaches(3000)]).toEqual([502, "unreachable"]);
    // Told nothing yet, as a proxy just launched: nothing is a chat's.
    const fresh = new BrowserProxy({ resolve });
    expect(await fresh.reaches(3000)).toBe("refused");
    await fresh.close();
    expect(dialed).toEqual([]);
  });

  it("ends what it carries to a port taken back, at once, carries nothing more to it, and leaves another port's alone", async () => {
    const streaming = (to: number) => {
      const asked = request({ host: "127.0.0.1", port, path: `http://localhost:${to}/stream`, headers: { host: `localhost:${to}`, ...own, ...signing(signed) } });
      const state = { cut: false };
      asked.on("response", (answer) => answer.on("data", () => {}).on("close", () => (state.cut = true)).on("error", () => (state.cut = true)));
      asked.on("error", () => (state.cut = true));
      asked.end();
      return state;
    };
    const taken = streaming(3000);
    const kept = streaming(3001);
    await vi.waitFor(() => expect(seen.filter(({ url }) => url === "/stream")).toHaveLength(2));
    expect(behind.size).toBe(2);
    // Port 3001 stays; 3000 is taken back.
    proxy.forwards([3001], path(), KEY);
    await vi.waitFor(() => expect(taken.cut).toBe(true));
    await vi.waitFor(() => expect(behind.size).toBe(1));
    expect(kept.cut).toBe(false);
    expect((await fetched("http://localhost:3000/")).status).toBe(403);
    proxy.forwards([], path(), KEY);
    await vi.waitFor(() => expect(kept.cut).toBe(true));
    await vi.waitFor(() => expect(behind.size).toBe(0));
    expect((await fetched("http://localhost:3001/")).status).toBe(403);
  });

  it("carries nothing to a port taken back while the door answered its knock: the connection is let go, and the chat's server asked nothing", async () => {
    slow = 200;
    const asking = fetched("http://localhost:3000/late");
    await vi.waitFor(() => expect(knocks).toEqual([`${KEY} 3000`]));
    proxy.forwards([3001], path(), KEY);
    expect((await asking).status).toBe(403);
    await vi.waitFor(() => expect(behind.size).toBe(0));
    expect(seen).toEqual([]);
  });

  it("leaves nothing open behind the door when the browser leaves an answer partway through, or gives up before any", async () => {
    for (const target of ["/stream", "/never"]) {
      const asked = request({ host: "127.0.0.1", port, path: `http://localhost:3000${target}`, headers: { host: "localhost:3000", ...own, ...signing(signed) } });
      asked.on("error", () => {});
      asked.end();
      await vi.waitFor(() => expect(seen.some(({ url }) => url === target)).toBe(true));
      expect(behind.size).toBe(1);
      asked.destroy();
      await vi.waitFor(() => expect(behind.size).toBe(0));
    }
  });
});
