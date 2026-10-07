import { execFile } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { createServer as createHttpServer, type IncomingHttpHeaders, type Server as HttpServer } from "node:http";
import { connect, createServer, type Server, type Socket } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { listen } from "../src/guest/listeners.js";

let dir: string;
let agent: Server;
let web: HttpServer;
let webPort: number;
let echo: Server;
let echoPort: number;
let proxies: Server[];
// Each destination line the root's socket was given, and each request the web server took.
let lines: string[];
// Each destination line whose socket the runner let go before the agent answered it.
let left: string[];
let requests: Array<{ method: string | undefined; url: string | undefined; headers: IncomingHttpHeaders; body: string }>;

const portOf = (server: Server) => (server.address() as { port: number }).port;

beforeEach(async () => {
  dir = mkdtempSync(join(tmpdir(), "guest-listeners-"));
  lines = [];
  left = [];
  requests = [];
  // Says how many bytes came once its client has finished sending: a half-closed connection still hears the reply.
  echo = createServer({ allowHalfOpen: true }, (socket) => {
    let bytes = 0;
    socket.on("data", (chunk: Buffer) => {
      bytes += chunk.length;
    });
    socket.on("end", () => socket.end(`got ${bytes}\n`));
  });
  await new Promise<void>((done) => echo.listen(0, "127.0.0.1", done));
  echoPort = portOf(echo);
  web = createHttpServer((req, res) => {
    let body = "";
    req.on("data", (chunk: Buffer) => {
      body += chunk.toString();
    });
    req.on("end", () => {
      requests.push({ method: req.method, url: req.url, headers: req.headers, body });
      res.end(`served ${req.url}\n`);
    });
  });
  await new Promise<void>((done) => web.listen(0, "127.0.0.1", done));
  webPort = (web.address() as { port: number }).port;
  // The agent's end of the root's socket: a destination it refuses, one it cannot reach, one it
  // drops without a word, one it takes 400 ms to decide, one it never decides, the echo server
  // here, and every other carried to the web server here.
  agent = createServer({ allowHalfOpen: true }, (socket) => {
    let said = "";
    const read = (chunk: Buffer) => {
      said += chunk.toString("latin1");
      const end = said.indexOf("\n");
      if (end < 0) return;
      socket.off("data", read);
      const line = said.slice(0, end);
      lines.push(line);
      if (line.startsWith("refused.example:")) return void socket.end("403 denied\n");
      if (line.startsWith("gone.example:")) return void socket.end("502 ECONNREFUSED\n");
      if (line.startsWith("drop.example:")) return void socket.destroy();
      if (line.startsWith("wait.example:")) return void socket.once("end", () => left.push(line));
      const carry = () => {
        const upstream = connect({ host: "127.0.0.1", port: line.startsWith("echo.example:") ? echoPort : webPort, allowHalfOpen: true });
        upstream.on("connect", () => {
          socket.write("200\n");
          if (end + 1 < said.length) upstream.write(Buffer.from(said.slice(end + 1), "latin1"));
          socket.pipe(upstream);
          upstream.pipe(socket);
        });
      };
      if (line.startsWith("slow.example:")) setTimeout(carry, 400);
      else carry();
    };
    socket.on("data", read);
  });
  await new Promise<void>((done) => agent.listen(join(dir, "net.sock"), done));
  proxies = await listen(join(dir, "net.sock"), { http: 0, socks: 0 });
});

afterEach(async () => {
  for (const server of [...proxies, agent, web, echo]) server.close();
  rmSync(dir, { recursive: true, force: true });
});

// curl as a command runs it, beside the proxies in this process, which a synchronous spawn would stall.
const curl = (...args: string[]) => new Promise<{ stdout: string }>((done) => {
  execFile("curl", ["-sS", "--max-time", "10", ...args], (_error, stdout) => done({ stdout }));
});
const http = () => `http://127.0.0.1:${portOf(proxies[0] as Server)}`;

// A SOCKS5 exchange, byte by byte as a client sends it: what the proxy answered each step.
// Once connected, the client sends *then* and half-closes, as `nc -q` does.
function socks5(request: number[], then = "", proxy = proxies[1] as Server): Promise<Buffer[]> {
  return new Promise((done) => {
    const socket: Socket = connect({ host: "127.0.0.1", port: portOf(proxy) });
    const heard: Buffer[] = [];
    let step = 0;
    socket.on("data", (chunk: Buffer) => {
      heard.push(chunk);
      step += 1;
      if (step === 1) socket.write(Buffer.from(request));
      else if (step === 2 && chunk[1] === 0 && then) socket.end(then);
    });
    socket.on("close", () => done(heard));
    socket.on("error", () => {});
    socket.write(Buffer.from([5, 1, 0]));
    setTimeout(() => socket.end(), 1_000);
  });
}
const domain = (name: string, port: number, command = 1) => [5, command, 0, 3, name.length, ...Buffer.from(name), port >> 8, port & 255];

describe("a root's HTTP proxy", () => {
  it("carries a plain HTTP request to its host, as the root asked it, without what was for the proxy", async () => {
    const got = await curl("-x", http(), "-H", "X-Kept: yes", "-H", "Proxy-Authorization: Basic eDp5", "--data", "a=1", "http://web.example/path?q=1");
    expect(got.stdout).toBe("served /path?q=1\n");
    expect(lines).toEqual(["web.example:80"]);
    expect(requests).toMatchObject([{ method: "POST", url: "/path?q=1", headers: { "x-kept": "yes", host: "web.example" }, body: "a=1" }]);
    expect(requests[0]?.headers["proxy-authorization"]).toBeUndefined();
  });

  it("opens a tunnel for CONNECT, to the port asked", async () => {
    const got = await curl("-p", "-x", http(), "http://web.example:8443/tunnelled");
    expect(got.stdout).toBe("served /tunnelled\n");
    expect(lines).toEqual(["web.example:8443"]);
  });

  it("answers a client that half-closed once its request was sent: a tunnel's, and an HTTP/1.0 request's", async () => {
    // What a client hears once it has sent *request* and half-closed, or, for a tunnel, once it has sent *then* on its 200.
    const heard = (request: string, then?: string) => new Promise<string>((done) => {
      const socket = connect({ host: "127.0.0.1", port: portOf(proxies[0] as Server), allowHalfOpen: true });
      let said = "";
      socket.on("data", (chunk: Buffer) => {
        said += chunk.toString();
        if (then !== undefined && said === "HTTP/1.1 200 Connection Established\r\n\r\n") socket.end(then);
      });
      socket.on("end", () => done(said));
      if (then === undefined) socket.end(request);
      else socket.write(request);
    });
    expect(await heard("CONNECT echo.example:7 HTTP/1.1\r\nHost: echo.example:7\r\n\r\n", "hello")).toBe("HTTP/1.1 200 Connection Established\r\n\r\ngot 5\n");
    expect(await heard("GET http://web.example/half HTTP/1.0\r\nHost: web.example\r\n\r\n")).toMatch(/^HTTP\/1\.1 200 OK\r\n.*\r\n\r\nserved \/half\n$/s);
    expect(lines).toEqual(["echo.example:7", "web.example:80"]);
  });

  it("answers what is refused with the proxy's status, and a socket that says nothing as unreachable", async () => {
    expect((await curl("-x", http(), "-o", "/dev/null", "-w", "%{http_code}", "http://refused.example/")).stdout).toBe("403");
    expect((await curl("-p", "-x", http(), "-o", "/dev/null", "-w", "%{http_connect}", "http://refused.example:443/")).stdout).toBe("403");
    expect((await curl("-x", http(), "-o", "/dev/null", "-w", "%{http_code}", "http://gone.example/")).stdout).toBe("502");
    expect((await curl("-x", http(), "-o", "/dev/null", "-w", "%{http_code}", "http://drop.example/")).stdout).toBe("502");
    // Asked as a server, it is none.
    expect((await curl("-o", "/dev/null", "-w", "%{http_code}", `${http()}/`)).stdout).toBe("400");
  });
});

describe("a root's SOCKS5 proxy", () => {
  it("connects a name through the root's socket, as curl asks it", async () => {
    const got = await curl("--socks5-hostname", `127.0.0.1:${portOf(proxies[1] as Server)}`, "http://web.example:81/socks");
    expect(got.stdout).toBe("served /socks\n");
    expect(lines).toEqual(["web.example:81"]);
  });

  it("names an address as the host proxy reads one", async () => {
    await socks5([5, 1, 0, 1, 192, 0, 2, 1, 0, 80]);
    await socks5([5, 1, 0, 4, 0x20, 0x01, 0x0d, 0xb8, ...Array(11).fill(0), 1, 1, 187]);
    expect(lines).toEqual(["192.0.2.1:80", "[2001:db8:0:0:0:0:0:1]:443"]);
  });

  it("answers each refusal with its own code, and a command it does not take, with none of them dialed", async () => {
    const replies = async (request: number[]) => (await socks5(request))[1]?.[1];
    expect(await replies(domain("refused.example", 443))).toBe(2);
    expect(await replies(domain("gone.example", 443))).toBe(5);
    expect(await replies(domain("drop.example", 443))).toBe(4);
    // BIND, and an address type it does not know.
    expect(await replies(domain("web.example", 443, 2))).toBe(7);
    expect(await replies([5, 1, 0, 9, 0, 0])).toBe(8);
    expect(lines).toEqual(["refused.example:443", "gone.example:443", "drop.example:443"]);
  });

  it("carries the reply to a client that half-closed once connected", async () => {
    const heard = await socks5(domain("echo.example", 7), "hello");
    expect(heard[1]?.[1]).toBe(0);
    expect(Buffer.concat(heard.slice(2)).toString()).toBe("got 5\n");
  });

  it("gives a client its bound in all to say where it goes, and none of the time its host takes to decide", async () => {
    const quick = await listen(join(dir, "net.sock"), { http: 0, socks: 0 }, 200);
    proxies.push(...quick);
    // Decided in 400 ms, past the bound: connected all the same.
    expect((await socks5(domain("slow.example", 80), "", quick[1]))[1]?.[1]).toBe(0);
    // A byte every 50 ms keeps it busy, not within the bound: dropped before it has said where it goes.
    const heard = await new Promise<Buffer[]>((done) => {
      const socket = connect({ host: "127.0.0.1", port: portOf(quick[1] as Server) });
      const said: Buffer[] = [];
      const request = domain("web.example", 80);
      const trickle = setInterval(() => void socket.write(Buffer.from(request.splice(0, 1))), 50);
      socket.on("error", () => {});
      socket.on("data", (chunk: Buffer) => void said.push(chunk));
      socket.on("close", () => {
        clearInterval(trickle);
        done(said);
      });
      socket.write(Buffer.from([5, 1, 0]));
    });
    expect(heard.map((chunk) => [...chunk])).toEqual([[5, 0]]);
    expect(lines).toEqual(["slow.example:80"]);
  });

  it("refuses a client that offers no method it takes", async () => {
    const heard = await new Promise<Buffer>((done) => {
      const socket = connect({ host: "127.0.0.1", port: portOf(proxies[1] as Server) });
      socket.on("data", done);
      socket.write(Buffer.from([5, 1, 2]));
    });
    expect([...heard]).toEqual([5, 0xff]);
  });
});

describe("a root's proxies", () => {
  it("let a client's tunnel go when the client leaves before its answer: CONNECT's, SOCKS5's, and a request's", async () => {
    // CONNECT's client gives up at its own timeout.
    await curl("--max-time", "1", "-p", "-x", http(), "http://wait.example:443/");
    await vi.waitFor(() => expect(left).toEqual(["wait.example:443"]), { timeout: 1_000 });
    // SOCKS5's ends once its request is sent.
    await socks5(domain("wait.example", 444));
    await vi.waitFor(() => expect(left).toEqual(["wait.example:443", "wait.example:444"]), { timeout: 1_000 });
    // A request's resets its connection.
    const client = connect({ host: "127.0.0.1", port: portOf(proxies[0] as Server) });
    client.on("error", () => {});
    client.write("GET http://wait.example:445/ HTTP/1.1\r\nHost: wait.example:445\r\n\r\n");
    await vi.waitFor(() => expect(lines).toContain("wait.example:445"), { timeout: 1_000 });
    client.resetAndDestroy();
    await vi.waitFor(() => expect(left).toEqual(["wait.example:443", "wait.example:444", "wait.example:445"]), { timeout: 1_000 });
  });

  it("do not start where their ports are taken", async () => {
    const taken = portOf(proxies[0] as Server);
    await expect(listen(join(dir, "net.sock"), { http: taken, socks: 0 })).rejects.toThrow(/EADDRINUSE/);
  });
});
