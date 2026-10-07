import { type ClientHttp2Session, type ClientHttp2Stream, connect as connectH2 } from "node:http2";
import { connect as connectTcp, createServer, type Server } from "node:net";
import { duplexPair } from "node:stream";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { MAX_TUNNELS } from "../src/guest/network.js";
import { MAX_SHARES } from "../src/guest/protocol.js";
import type { NetworkAsk } from "../src/hosts/messages.js";
import { NetProxy, networkNotice, withNotice } from "../src/vm/proxy.js";

const ROOT = "0b6c1d3e-6f0a-4c1e-9a52-6a1d2c3b4e5f";
const OTHER = "7d8e9f00-1a2b-4c3d-8e4f-5a6b7c8d9e0f";

// What a name leads to, and how often it was looked up: rebinding.example leads elsewhere, then here.
let lookups: Record<string, number>;
const names: Record<string, string[][]> = {
  "pypi.org": [["151.101.0.223"]],
  "example.com": [["93.184.215.14"]],
  "rebinding.example": [["93.184.215.14"], ["127.0.0.1"]],
  "two.example": [["192.0.2.1", "192.0.2.2"]],
  "nowhere.example": [["192.0.2.1"]],
};
const resolve = (name: string) => {
  const seen = (lookups[name] = (lookups[name] ?? 0) + 1);
  // A name whose lookup takes 200 ms.
  if (name === "slow.example") return new Promise<string[]>((done) => setTimeout(() => done(["93.184.215.14"]), 200));
  const answers = names[name];
  if (!answers) return Promise.reject(new Error("ENOTFOUND"));
  return Promise.resolve(answers[Math.min(seen, answers.length) - 1] ?? []);
};

let echo: Server;
let echoPort: number;
let closedPort: number;
let client: ClientHttp2Session;
let proxy: NetProxy;
// Each address the proxy dialed, and each ask it made.
let dialed: string[];
let asked: Array<{ root: string; asked: NetworkAsk }>;
let answer: (root: string, asked: NetworkAsk) => Promise<boolean>;

beforeEach(async () => {
  lookups = {};
  dialed = [];
  asked = [];
  answer = () => Promise.resolve(true);
  // Says how many bytes came once its client has finished sending: a half-closed connection still hears the reply.
  echo = createServer({ allowHalfOpen: true }, (socket) => {
    let bytes = 0;
    socket.on("data", (chunk: Buffer) => {
      bytes += chunk.length;
    });
    socket.on("end", () => socket.end(`got ${bytes}\n`));
  });
  await new Promise<void>((done) => echo.listen(0, "127.0.0.1", done));
  echoPort = (echo.address() as { port: number }).port;
  const closed = createServer();
  await new Promise<void>((done) => closed.listen(0, "127.0.0.1", done));
  closedPort = (closed.address() as { port: number }).port;
  await new Promise<void>((done) => closed.close(() => done()));
  const [host, guest] = duplexPair();
  proxy = new NetProxy(host, {
    egress: { ask: (root, request) => (asked.push({ root, asked: request }), answer(root, request)) },
    resolve,
    local: () => ["127.0.0.1", "::1"],
    // Every address judged reaches the echo server here, but 192.0.2.1, which refuses.
    connect: (address, port) => {
      dialed.push(`${address}:${port}`);
      return connectTcp({ host: "127.0.0.1", port: address === "192.0.2.1" ? closedPort : echoPort, allowHalfOpen: true });
    },
  });
  client = connectH2("http://guest", { createConnection: () => guest });
});

afterEach(async () => {
  client.destroy();
  proxy.close();
  await new Promise<void>((done) => echo.close(() => done()));
});

// A CONNECT stream for *root* to *authority*, as the agent opens one: its status, the reason a refusal gives, and the stream.
function tunnel(authority: string, root = ROOT, method = "CONNECT"): Promise<{ status: number; reason: string | undefined; stream: ClientHttp2Stream }> {
  const stream = client.request({ ":method": method, ":authority": authority, ...(method === "CONNECT" ? {} : { ":path": "/" }), "surogate-root": root });
  return new Promise((done) => {
    stream.on("response", (headers) => done({ status: Number(headers[":status"]), reason: headers["surogate-reason"] as string | undefined, stream }));
  });
}

// What comes back once *data* is sent and the stream's side is ended.
function exchange(stream: ClientHttp2Stream, data: string): Promise<string> {
  return new Promise((done) => {
    let reply = "";
    stream.on("data", (chunk: Buffer) => {
      reply += chunk.toString();
    });
    stream.on("end", () => done(reply));
    stream.end(data);
  });
}

describe("the host proxy", () => {
  it("dials a package host at the address it judged, without asking, and carries a half-closed connection's reply", async () => {
    const { status, stream } = await tunnel("pypi.org:443");
    expect(status).toBe(200);
    expect(await exchange(stream, "hello")).toBe("got 5\n");
    expect(dialed).toEqual(["151.101.0.223:443"]);
    expect(asked).toEqual([]);
  });

  it("judges every connection when it is made, and dials what it judged, never a later lookup", async () => {
    const first = await tunnel("rebinding.example:443");
    expect(first.status).toBe(200);
    expect(await exchange(first.stream, "x")).toBe("got 1\n");
    // The name now leads to this computer: the next connection is refused, as its own.
    expect(await tunnel("rebinding.example:443")).toMatchObject({ status: 403, reason: "own" });
    expect(dialed).toEqual(["93.184.215.14:443"]);
    expect(lookups["rebinding.example"]).toBe(2);
    expect(proxy.takeNotice(ROOT)).toBe("This computer does not let a chat reach its own network services (rebinding.example:443)");
  });

  it("refuses this computer's own and a name it cannot look up without asking, and says so once in the root's next notice", async () => {
    expect(await tunnel("127.1:9")).toMatchObject({ status: 403, reason: "own" });
    expect(await tunnel("[::1]:3000")).toMatchObject({ status: 403, reason: "own" });
    expect(await tunnel("missing.example:443")).toMatchObject({ status: 403, reason: "unknown" });
    expect(asked).toEqual([]);
    expect(dialed).toEqual([]);
    expect(proxy.takeNotice(OTHER)).toBeNull();
    expect(proxy.takeNotice(ROOT)).toBe([
      "This computer does not let a chat reach its own network services (127.0.0.1:9, [::1]:3000)",
      "This computer could not look up missing.example:443.",
    ].join("\n"));
    expect(proxy.takeNotice(ROOT)).toBeNull();
  });

  it("refuses a stream that is not a CONNECT, names no root, or no port, and tells no one", async () => {
    expect(await tunnel("example.com:443", "../etc")).toMatchObject({ status: 403, reason: "invalid" });
    expect(await tunnel("example.com:443", "")).toMatchObject({ status: 403, reason: "invalid" });
    expect(await tunnel("example.com:443", ROOT, "GET")).toMatchObject({ status: 403, reason: "invalid" });
    expect(await tunnel("example.com")).toMatchObject({ status: 403, reason: "invalid" });
    expect(await tunnel("*.example.com:443")).toMatchObject({ status: 403, reason: "invalid" });
    expect([asked, dialed, proxy.takeNotice(ROOT)]).toEqual([[], [], null]);
  });

  it("asks once for a root's connections to one destination in flight, and dials them all once allowed", async () => {
    // Each ask's answer, in the order asked.
    const answers: Array<(allowed: boolean) => void> = [];
    answer = () => new Promise((done) => answers.push(done));
    const both = [tunnel("example.com:443"), tunnel("Example.COM.:443")];
    await new Promise((done) => setTimeout(done, 100));
    // Another root, and another port, ask for themselves.
    void tunnel("example.com:443", OTHER);
    void tunnel("example.com:8443");
    await new Promise((done) => setTimeout(done, 100));
    expect(asked).toEqual([
      { root: ROOT, asked: { host: "example.com", port: 443, privateNetwork: false } },
      { root: OTHER, asked: { host: "example.com", port: 443, privateNetwork: false } },
      { root: ROOT, asked: { host: "example.com", port: 8443, privateNetwork: false } },
    ]);
    // While it waits, the root's next notice says so, once.
    expect(proxy.takeNotice(ROOT)).toBe("Still waiting for this computer's user to allow network access to example.com:443, example.com:8443.");
    expect(proxy.takeNotice(ROOT)).toBeNull();
    answers[0]?.(true);
    expect((await Promise.all(both)).map(({ status }) => status)).toEqual([200, 200]);
    expect(dialed).toEqual(["93.184.215.14:443", "93.184.215.14:443"]);
    // The next connection after the answer asks again.
    answer = () => Promise.resolve(true);
    expect(await tunnel("example.com:443")).toMatchObject({ status: 200 });
    expect(asked).toHaveLength(4);
  });

  it("refuses what its user denies, and what an ask that fails decides, and says so in the next notice", async () => {
    answer = () => Promise.resolve(false);
    expect(await tunnel("example.com:443")).toMatchObject({ status: 403, reason: "denied" });
    answer = () => Promise.reject(new Error("the prompt failed"));
    expect(await tunnel("example.com:8080")).toMatchObject({ status: 403, reason: "denied" });
    expect(dialed).toEqual([]);
    expect(proxy.takeNotice(ROOT)).toBe("This computer did not allow network access to example.com:443, example.com:8080.");
  });

  it("tries each address it judged until one takes the connection, and answers 502 when none does", async () => {
    const { status } = await tunnel("two.example:443");
    expect(status).toBe(200);
    expect(dialed).toEqual(["192.0.2.1:443", "192.0.2.2:443"]);
    expect(await tunnel("nowhere.example:443")).toMatchObject({ status: 502, reason: "ECONNREFUSED" });
  });

  it("forgets what a torn-down root met", async () => {
    await tunnel("127.0.0.1:9");
    proxy.forget(ROOT);
    expect(proxy.takeNotice(ROOT)).toBeNull();
  });

  it("asks anew for a root set up again, past an ask of its torn-down namespace still open", async () => {
    const answers: Array<(allowed: boolean) => void> = [];
    answer = () => new Promise((done) => answers.push(done));
    const before = tunnel("example.com:443");
    await new Promise((done) => setTimeout(done, 50));
    proxy.forget(ROOT);
    const after = tunnel("example.com:443");
    await new Promise((done) => setTimeout(done, 50));
    expect(asked).toHaveLength(2);
    answers[1]?.(false);
    expect(await after).toMatchObject({ status: 403, reason: "denied" });
    expect(proxy.takeNotice(ROOT)).toBe("This computer did not allow network access to example.com:443.");
    // The torn-down namespace's answer is its own connection's alone.
    answers[0]?.(true);
    expect(await before).toMatchObject({ status: 200 });
    expect(proxy.takeNotice(ROOT)).toBeNull();
  });

  it("asks no one about a connection gone before it was judged", async () => {
    const stream = client.request({ ":method": "CONNECT", ":authority": "slow.example:443", "surogate-root": ROOT });
    stream.on("error", () => {});
    await new Promise((done) => setTimeout(done, 50));
    stream.close();
    await new Promise((done) => setTimeout(done, 300));
    expect(lookups["slow.example"]).toBe(1);
    expect(asked).toEqual([]);
  });

  it("lets the guest open as many streams at once as its roots can have tunnels, and no more", async () => {
    await tunnel("pypi.org:443");
    expect(client.remoteSettings.maxConcurrentStreams).toBe(MAX_SHARES * MAX_TUNNELS);
  });
});

describe("a network notice", () => {
  it("names at most 20 destinations of a kind, then how many more", () => {
    const ports = Array.from({ length: 23 }, (_, n) => `127.0.0.1:${n + 1}`);
    expect(networkNotice({ own: ports, refused: [], waiting: [], unknown: [] })).toBe(
      `This computer does not let a chat reach its own network services (${ports.slice(0, 20).join(", ")} and 3 more)`,
    );
    expect(networkNotice({ own: [], refused: [], waiting: [], unknown: [] })).toBeNull();
  });

  it("goes after a command's output, only when it answered", () => {
    const ran = { ok: { output: "403\n", returncode: 0, timed_out: false } };
    expect(withNotice(ran, "This computer did not allow network access to a:1.")).toEqual({
      ok: { output: "403\n\nThis computer did not allow network access to a:1.", returncode: 0, timed_out: false },
    });
    expect(withNotice({ ok: { output: "", returncode: 0, timed_out: false } }, "n")).toEqual({ ok: { output: "n", returncode: 0, timed_out: false } });
    const refused = { error: { type: "sandbox", message: "Blocked" } };
    expect(withNotice(refused, "n")).toBe(refused);
    expect(withNotice(ran, null)).toBe(ran);
  });
});
