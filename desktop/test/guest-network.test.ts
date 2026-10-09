import { spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, rmSync, statSync } from "node:fs";
import { connect as connectTcp, connect, createServer, type Server, type Socket } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { duplexPair } from "node:stream";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { MAX_TUNNELS, Network } from "../src/guest/network.js";
import { openPort } from "../src/guest/port.js";
import { MAX_INBOUND } from "../src/guest/protocol.js";
import type { NetworkAsk } from "../src/hosts/messages.js";
import { NetProxy } from "../src/vm/proxy.js";

const ROOT = "0b6c1d3e-6f0a-4c1e-9a52-6a1d2c3b4e5f";
const OTHER = "7d8e9f00-1a2b-4c3d-8e4f-5a6b7c8d9e0f";
const uid = process.getuid?.() ?? 0;

let dir: string;
let echo: Server;
let echoPort: number;
let proxy: NetProxy;
let network: Network;
let asked: Array<[string, NetworkAsk]>;
// What the host's user answers each ask.
let answer: () => Promise<boolean>;

beforeEach(async () => {
  dir = mkdtempSync(join(tmpdir(), "guest-network-"));
  asked = [];
  answer = () => Promise.resolve(true);
  // Says how many bytes came once its client has finished sending.
  echo = createServer({ allowHalfOpen: true }, (socket) => {
    let bytes = 0;
    socket.on("data", (chunk: Buffer) => {
      bytes += chunk.length;
    });
    socket.on("end", () => socket.end(`got ${bytes}\n`));
  });
  await new Promise<void>((done) => echo.listen(0, "127.0.0.1", done));
  echoPort = (echo.address() as { port: number }).port;
  // The host's end of the net port, its every destination elsewhere reaching the echo server here.
  const [host, guest] = duplexPair();
  proxy = new NetProxy(host, {
    egress: { ask: (root, request) => (asked.push([root, request]), answer()) },
    resolve: async (name) => (name === "echo.example" ? ["192.0.2.10"] : []),
    local: () => [],
    subnets: () => [],
    connect: () => connectTcp({ host: "127.0.0.1", port: echoPort, allowHalfOpen: true }),
  });
  // A destination line has 200 ms here.
  network = new Network(guest, join(dir, "net"), 200);
});

afterEach(async () => {
  proxy.close();
  await new Promise<void>((done) => echo.close(() => done()));
  rmSync(dir, { recursive: true, force: true });
});

// A connection through *root*'s socket to *destination*, as the root's runner opens one: the
// agent's status line, and what the destination answers once *data* is sent and the side ended.
function through(path: string, destination: string, data = "hello"): Promise<{ status: string; reply: string }> {
  return new Promise((done, failed) => {
    const socket: Socket = connect({ path, allowHalfOpen: true });
    let said = "";
    socket.on("error", failed);
    socket.on("data", (chunk: Buffer) => {
      said += chunk.toString();
      if (said.startsWith("200\n") && said.length === 4) socket.end(data);
    });
    socket.on("end", () => {
      const at = said.indexOf("\n");
      done({ status: said.slice(0, at), reply: said.slice(at + 1) });
    });
    socket.write(`${destination}\n`);
  });
}

describe("the agent's network", () => {
  it("gives each root a socket of its own user's, and carries a connection through it to what the host proxy dials", async () => {
    const unlisten = await network.listen(ROOT, uid);
    const path = network.path(ROOT);
    expect([statSync(path).uid, statSync(path).mode & 0o777]).toEqual([uid, 0o600]);
    expect(await through(path, "echo.example:80")).toEqual({ status: "200", reply: "got 5\n" });
    expect(asked).toEqual([[ROOT, { host: "echo.example", port: 80, privateNetwork: false }]]);
    unlisten();
    expect(existsSync(path)).toBe(false);
  });

  it("ends a root's open connections with its socket", async () => {
    const unlisten = await network.listen(ROOT, uid);
    const closed = await new Promise<string>((done) => {
      const socket = connect({ path: network.path(ROOT), allowHalfOpen: true });
      let said = "";
      socket.on("error", () => {});
      socket.on("data", (chunk: Buffer) => {
        said += chunk.toString();
        if (said === "200\n") unlisten();
      });
      socket.on("end", () => done(said));
      setTimeout(() => done("still open"), 2_000);
      socket.write("echo.example:80\n");
    });
    expect(closed).toBe("200\n");
  });

  it("names the root whose socket a connection came on, whatever the connection says", async () => {
    await network.listen(ROOT, uid);
    await network.listen(OTHER, uid);
    expect(await through(network.path(OTHER), "echo.example:443")).toMatchObject({ status: "200" });
    expect(await through(network.path(ROOT), "echo.example:443")).toMatchObject({ status: "200" });
    expect(asked.map(([root]) => root)).toEqual([OTHER, ROOT]);
  });

  // What a root's runner sends on *path* for a connection the agent asked it for: its line, then *data*.
  // Resolves with what came back once the agent's side closed it, or "kept" when it is still open after 300 ms.
  const brought = (path: string, line: string, data = "") => new Promise<string>((done) => {
    const socket: Socket = connect({ path, allowHalfOpen: true });
    let heard = "";
    socket.on("error", () => {});
    socket.on("data", (chunk: Buffer) => {
      heard += chunk.toString();
    });
    socket.on("end", () => done(`closed ${heard}`));
    setTimeout(() => done(`kept ${heard}`), 300);
    socket.write(`${line}\n${data}`);
  });
  const ID = "0123456789abcdef0123456789abcdef";

  it("hands a connection a root's runner brings to whoever asked that root for it, with what the server said first", async () => {
    await network.listen(ROOT, uid);
    const asked = network.arrival(ROOT, ID);
    const kept = brought(network.path(ROOT), `/in/${ID}`, "220 ready\n");
    const socket = await asked;
    if (typeof socket === "string") throw new Error(socket);
    // Handed over paused, so nothing is lost before whoever asked reads it.
    expect(await new Promise<string>((done) => socket.once("data", (chunk: Buffer) => done(chunk.toString())).resume())).toBe("220 ready\n");
    socket.write("EHLO\n");
    expect(await kept).toBe("kept EHLO\n");
    socket.destroy();
  });

  it("answers why a root's runner reached nothing, and that it brought nothing in time", async () => {
    await network.listen(ROOT, uid);
    const refused = network.arrival(ROOT, ID);
    expect(await brought(network.path(ROOT), `/in/${ID} ECONNREFUSED`)).toBe("closed ");
    expect(await refused).toBe("ECONNREFUSED");
    expect(await network.arrival(ROOT, ID, 50)).toBe("ETIMEDOUT");
  });

  it("drops a connection no one asked that root for: another root's id, one answered already, and a line that is none", async () => {
    await network.listen(ROOT, uid);
    await network.listen(OTHER, uid);
    const asked = network.arrival(ROOT, ID, 400);
    // Another chat's command that learned the id: the agent knows a root by its socket, never by what it says.
    expect(await brought(network.path(OTHER), `/in/${ID}`, "from another chat")).toBe("closed ");
    expect(await brought(network.path(ROOT), "/in/ffffffffffffffffffffffffffffffff")).toBe("closed ");
    expect(await brought(network.path(ROOT), `/in/${ID.toUpperCase()}`)).toBe("closed 403 invalid\n");
    expect(await brought(network.path(ROOT), `/in/${ID} econnrefused`)).toBe("closed 403 invalid\n");
    const first = brought(network.path(ROOT), `/in/${ID}`);
    const socket = await asked;
    expect(typeof socket).toBe("object");
    // Once handed over, the id brings nothing more.
    expect(await brought(network.path(ROOT), `/in/${ID}`)).toBe("closed ");
    expect(await first).toBe("kept ");
    if (typeof socket !== "string") socket.destroy();
  });

  it("leaves a root's commands their connections while the browser's are at their bound, and the other way", { timeout: 20_000 }, async () => {
    await network.listen(ROOT, uid);
    await network.listen(OTHER, uid);
    const id = (n: number) => n.toString(16).padStart(32, "0");
    // A connection the root's runner brings under *n*'s id, as it was asked: the agent's end, and the runner's.
    const bring = async (root: string, n: number) => {
      const asked = network.arrival(root, id(n));
      const runner: Socket = connect({ path: network.path(root), allowHalfOpen: true });
      runner.on("error", () => {});
      runner.write(`/in/${id(n)}\n`);
      return { handed: await asked, runner };
    };
    // One asked for and never brought, or that nothing took, holds no place once it is answered.
    expect(new Set(await Promise.all(Array.from({ length: MAX_INBOUND }, (_, n) => network.arrival(ROOT, id(n), 50))))).toEqual(new Set(["ETIMEDOUT"]));
    const held = [];
    for (let n = 0; n < MAX_INBOUND; n += 1) held.push(await bring(ROOT, n));
    expect(held.every(({ handed }) => typeof handed === "object")).toBe(true);
    // The next is refused, asked of nobody; a command of that root still has its network, and another root its own bound.
    expect(await network.arrival(ROOT, id(MAX_INBOUND))).toBe("EMFILE");
    expect(await through(network.path(ROOT), "echo.example:80")).toEqual({ status: "200", reply: "got 5\n" });
    const others = await bring(OTHER, 0);
    expect(typeof others.handed).toBe("object");
    // One that ends gives its place to the next.
    const [first] = held;
    if (typeof first?.handed !== "object") throw new Error("not handed over");
    first.handed.destroy();
    await vi.waitFor(async () => expect(typeof (await bring(ROOT, MAX_INBOUND)).handed).toBe("object"));
    expect(await network.arrival(ROOT, id(MAX_INBOUND + 1))).toBe("EMFILE");

    // The other way: its commands at their bound, each waiting on its user's answer.
    answer = () => new Promise(() => {});
    asked = [];
    for (let port = 1; port <= MAX_TUNNELS; port += 1) {
      const socket = connect(network.path(OTHER));
      socket.on("error", () => {});
      socket.write(`echo.example:${port}\n`);
    }
    await vi.waitFor(() => expect(asked).toHaveLength(MAX_TUNNELS), { timeout: 5_000 });
    expect(typeof (await bring(OTHER, 1)).handed).toBe("object");
    // One more of its commands' is closed unheard, as before.
    const heard = await new Promise<string | null>((done) => {
      const socket = connect(network.path(OTHER));
      let said = "";
      socket.on("error", () => {});
      socket.on("data", (chunk: Buffer) => {
        said += chunk.toString();
      });
      socket.on("close", () => done(said));
      setTimeout(() => done(null), 2_000);
      socket.write("echo.example:257\n");
    });
    expect([heard, asked.length]).toEqual(["", MAX_TUNNELS]);
  });

  it("answers what the host proxy refuses with its status and reason, and ends the connection", async () => {
    await network.listen(ROOT, uid);
    expect(await through(network.path(ROOT), "127.0.0.1:9")).toEqual({ status: "403 own", reply: "" });
    expect(await through(network.path(ROOT), "nowhere.example:443")).toEqual({ status: "403 unknown", reply: "" });
  });

  it("refuses a line that is no destination without asking the host, and drops one that never ends", async () => {
    await network.listen(ROOT, uid);
    expect(await through(network.path(ROOT), "a b:80")).toEqual({ status: "403 invalid", reply: "" });
    expect(await through(network.path(ROOT), "http://echo.example:80")).toEqual({ status: "403 invalid", reply: "" });
    // A control character, and a byte past ASCII: never handed to the HTTP/2 layer.
    expect(await through(network.path(ROOT), "echo\x01.example:80")).toEqual({ status: "403 invalid", reply: "" });
    expect(await through(network.path(ROOT), "\u00e9cho.example:80")).toEqual({ status: "403 invalid", reply: "" });
    const closed = await new Promise<boolean>((done) => {
      const socket = connect(network.path(ROOT));
      socket.on("error", () => {});
      socket.on("close", () => done(true));
      socket.write("x".repeat(600));
    });
    expect(closed).toBe(true);
    expect(asked).toEqual([]);
  });

  it("drops a line that stalls short of its bound, once its time is up", async () => {
    await network.listen(ROOT, uid);
    const closed = await new Promise<boolean>((done) => {
      const socket = connect(network.path(ROOT));
      socket.on("error", () => {});
      socket.on("close", () => done(true));
      setTimeout(() => done(false), 2_000);
      socket.write("echo.example:80");
    });
    expect(closed).toBe(true);
    expect(asked).toEqual([]);
  });

  it("takes at most 256 connections of a root at once, and closes the next unheard", async () => {
    await network.listen(ROOT, uid);
    // Each waits on its user's answer.
    answer = () => new Promise(() => {});
    for (let port = 1; port <= 256; port += 1) {
      const socket = connect(network.path(ROOT));
      socket.on("error", () => {});
      socket.write(`echo.example:${port}\n`);
    }
    await vi.waitFor(() => expect(asked).toHaveLength(256), { timeout: 5_000 });
    const heard = await new Promise<string | null>((done) => {
      const socket = connect(network.path(ROOT));
      let said = "";
      socket.on("error", () => {});
      socket.on("data", (chunk: Buffer) => {
        said += chunk.toString();
      });
      socket.on("close", () => done(said));
      setTimeout(() => done(null), 2_000);
      socket.write("echo.example:257\n");
    });
    expect(heard).toBe("");
    expect(asked).toHaveLength(256);
  });

  it("frees the place of a connection that leaves while its ask waits, so 256 that gave up do not shut the root out", async () => {
    await network.listen(ROOT, uid);
    answer = () => new Promise(() => {});
    const waiting: Socket[] = [];
    for (let port = 1; port <= 256; port += 1) {
      const socket = connect(network.path(ROOT));
      socket.on("error", () => {});
      socket.write(`echo.example:${port}\n`);
      waiting.push(socket);
    }
    await vi.waitFor(() => expect(asked).toHaveLength(256), { timeout: 5_000 });
    // Each gives up, as a command's client does at its own timeout.
    for (const socket of waiting) socket.destroy();
    answer = () => Promise.resolve(true);
    await vi.waitFor(async () => expect(await through(network.path(ROOT), "echo.example:257")).toEqual({ status: "200", reply: "got 5\n" }), { timeout: 2_000 });
  });

  it("answers that the sandbox has no network once the host's end has gone", async () => {
    await network.listen(ROOT, uid);
    proxy.close();
    await new Promise((done) => setTimeout(done, 50));
    expect(await through(network.path(ROOT), "echo.example:80")).toEqual({ status: "502 sandbox", reply: "" });
  });
});

describe("a guest's port", () => {
  it("is a byte stream both ways: a FIFO opened through it hears back what it writes", async () => {
    const fifo = join(dir, "port");
    expect(spawnSync("mkfifo", [fifo]).status).toBe(0);
    const port = await openPort(fifo);
    const heard = new Promise<string>((done) => port.once("data", (chunk: Buffer) => done(chunk.toString())));
    port.write("hello\n");
    expect(await heard).toBe("hello\n");
    // A port destroyed with its read open says so: the agent never destroys one.
    port.on("error", () => {});
    port.destroy();
  });
});
