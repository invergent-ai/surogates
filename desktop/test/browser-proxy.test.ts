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
};
const resolve = (name: string) => {
  const seen = (lookups[name] = (lookups[name] ?? 0) + 1);
  const answers = names[name];
  if (!answers) return Promise.reject(new Error("ENOTFOUND"));
  return Promise.resolve(answers[Math.min(seen, answers.length) - 1] ?? []);
};

let echo: Server;
let web: HttpServer;
let ports: { echo: number; web: number; closed: number };
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
  echo = createServer((socket) => socket.on("data", (chunk) => socket.write(`echo ${chunk.toString()}`)));
  web = createHttp((req, res) => {
    seen.push({ method: req.method ?? "", url: req.url ?? "", host: req.headers.host ?? "", headers: Object.keys(req.headers) });
    res.writeHead(201, { "content-type": "text/plain" }).end("hello from the site");
  });
  const closed = createServer();
  await Promise.all([echo, web, closed].map((server) => new Promise<void>((done) => server.listen(0, "127.0.0.1", () => done()))));
  const portOf = (server: Server | HttpServer) => (server.address() as { port: number }).port;
  ports = { echo: portOf(echo), web: portOf(web), closed: portOf(closed) };
  await new Promise<void>((done) => closed.close(() => done()));
  proxy = new BrowserProxy({
    resolve,
    local: () => ["127.0.0.1", "::1", "198.51.100.5", "2001:db8:1::5"],
    // This computer's networks: a public IPv4 range, as a campus's is, and a home LAN's global IPv6 prefix.
    subnets: () => ["198.51.100.5/24", "2001:db8:1::5/64"],
    // Every judged address reaches the servers here, by the port asked for; 192.0.2.1 refuses.
    connect: (address, to) => {
      dialed.push(`${address}:${to}`);
      const at = address === "192.0.2.1" ? ports.closed : to === 443 ? ports.echo : ports.web;
      return connectTcp({ host: "127.0.0.1", port: at });
    },
  });
  port = await proxy.listen();
});

afterEach(async () => {
  await proxy.close();
  await Promise.all([echo, web].map((server) => new Promise<void>((done) => server.close(() => done()))));
});

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

  it("answers 400 for what is not a proxy's request", async () => {
    expect((await get("/index.html", "example.com")).status).toBe(400);
    expect((await get("https://example.com/", "example.com")).status).toBe(400);
    expect(dialed).toEqual([]);
  });
});
