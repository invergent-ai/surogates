import { type ClientHttp2Session, type ClientHttp2Stream, connect as connectH2, constants } from "node:http2";
import { connect, createServer, type Server, type Socket } from "node:net";
import { duplexPair } from "node:stream";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { Inbound } from "../src/guest/inbound.js";

const ROOT = "0b6c1d3e-6f0a-4c1e-9a52-6a1d2c3b4e5f";

let server: Server;
let port: number;
let host: ClientHttp2Session;
// Each root and port the agent was asked to reach, and what each reach answers: a socket to the server here unless told.
let reached: Array<[string, number]>;
let reach: (root: string, port: number) => Promise<Socket | string>;
// The server's end of each connection it took.
let taken: Socket[];

beforeEach(async () => {
  reached = [];
  taken = [];
  // Says what it was sent back, in capitals, as a chat's server would answer a request.
  server = createServer((socket) => {
    taken.push(socket);
    socket.on("error", () => {});
    socket.on("data", (chunk: Buffer) => socket.write(chunk.toString().toUpperCase()));
  });
  await new Promise<void>((done) => server.listen(0, "127.0.0.1", done));
  port = (server.address() as { port: number }).port;
  reach = () => new Promise((done) => {
    const socket = connect({ host: "127.0.0.1", port, allowHalfOpen: true });
    socket.once("connect", () => done(socket));
  });
  const [ours, guests] = duplexPair();
  new Inbound(guests, (root, to) => (reached.push([root, to]), reach(root, to)));
  host = connectH2("http://guest", { createConnection: () => ours });
  host.on("error", () => {});
});

afterEach(async () => {
  host.destroy();
  for (const socket of taken) socket.destroy();
  await new Promise<void>((done) => server.close(() => done()));
});

// A stream the host opens, and the status and reason the agent answers it with: status 0 for one HTTP/2 itself reset.
function open(headers: Record<string, string>): Promise<{ status: number; reason: string; stream: ClientHttp2Stream }> {
  return new Promise((done) => {
    const stream = host.request(headers);
    stream.on("error", () => {});
    stream.on("response", (answer) => done({ status: Number(answer[":status"]), reason: String(answer["surogate-reason"] ?? ""), stream }));
    stream.on("close", () => done({ status: 0, reason: "reset", stream }));
  });
}
const into = (authority: string, root = ROOT, method = "CONNECT") => open({ ":method": method, ":authority": authority, "surogate-root": root });

describe("the agent's door for connections into a root", () => {
  it("carries a stream the host opens to a port of that root's own loopback, both ways", async () => {
    const { status, stream } = await into("127.0.0.1:3000");
    expect(status).toBe(200);
    stream.write("get /");
    expect(await new Promise<string>((done) => stream.once("data", (chunk: Buffer) => done(chunk.toString())))).toBe("GET /");
    expect(reached).toEqual([[ROOT, 3000]]);
    // The host's end ends the server's connection.
    stream.close();
    await new Promise<void>((done) => taken[0]?.once("close", () => done()));
  });

  it("reaches nothing but 127.0.0.1 and a port, for a root named as one, whatever the host asks", async () => {
    for (const authority of [
      "localhost:3000", "127.0.0.2:3000", "[::1]:3000", "10.0.0.5:80", "example.com:443", "127.0.0.1", "127.0.0.1:0", "127.0.0.1:65536",
      "127.0.0.1:03000", "0x7f.1:3000", "2130706433:3000",
    ]) {
      expect(await into(authority), authority).toMatchObject({ status: 400, reason: "invalid" });
    }
    // What is no authority at all never becomes a stream: HTTP/2 resets it, or the agent refuses it.
    for (const authority of ["127.0.0.1:3000/", "127.0.0.1:3000 ", "127.0.0.1:3000\t"]) expect([0, 400], authority).toContain((await into(authority)).status);
    for (const root of ["", "../other", "a b", "x".repeat(65)]) expect(await into("127.0.0.1:3000", root), root).toMatchObject({ status: 400 });
    expect(await open({ ":method": "GET", ":path": "/", ":scheme": "http", ":authority": "127.0.0.1:3000", "surogate-root": ROOT })).toMatchObject({ status: 400 });
    expect(await open({ ":method": "CONNECT", ":authority": "127.0.0.1:3000" })).toMatchObject({ status: 400 });
    expect(reached).toEqual([]);
  });

  it("answers why nothing took the connection, and that the root has no sandbox", async () => {
    for (const reason of ["ECONNREFUSED", "sandbox", "ETIMEDOUT"]) {
      reach = () => Promise.resolve(reason);
      expect(await into("127.0.0.1:3000")).toMatchObject({ status: 502, reason });
    }
    // A reason that is none of the agent's is not passed on as it is.
    reach = () => Promise.resolve("a\r\nb");
    expect(await into("127.0.0.1:3000")).toMatchObject({ status: 502, reason: "unreachable" });
    reach = () => Promise.reject(new Error("broken"));
    expect(await into("127.0.0.1:3000")).toMatchObject({ status: 502, reason: "unreachable" });
  });

  it("lets go of a connection that arrives once the host has given the stream up", async () => {
    const arriving = Promise.withResolvers<Socket | string>();
    reach = () => arriving.promise;
    const stream = host.request({ ":method": "CONNECT", ":authority": "127.0.0.1:3000", "surogate-root": ROOT });
    stream.on("error", () => {});
    // Given up once the agent has asked its root's runner, and before the runner brings anything.
    await vi.waitFor(() => expect(reached).toEqual([[ROOT, 3000]]));
    const gone = new Promise<void>((done) => stream.once("close", () => done()));
    stream.close();
    await gone;
    const socket = connect({ host: "127.0.0.1", port });
    await new Promise<void>((done) => socket.once("connect", () => done()));
    const closed = new Promise<void>((done) => socket.once("close", () => done()));
    arriving.resolve(socket);
    await closed;
    expect(socket.destroyed).toBe(true);
  });

  it("ends the root's connection when the host resets a stream whose answer is still on its way, with a code or with none", async () => {
    // A server that answers without end, as a download does: the stream holds bytes the host has not read.
    const endless = createServer((socket) => {
      taken.push(socket);
      socket.on("error", () => {});
      const part = Buffer.alloc(64 * 1024, "d");
      const more = () => {
        while (!socket.destroyed && socket.write(part));
      };
      socket.on("drain", more);
      more();
    });
    await new Promise<void>((done) => endless.listen(0, "127.0.0.1", done));
    const to = (endless.address() as { port: number }).port;
    reach = () => new Promise((done) => {
      const socket = connect({ host: "127.0.0.1", port: to });
      socket.once("connect", () => done(socket));
    });
    try {
      for (const reset of [(stream: ClientHttp2Stream) => stream.close(constants.NGHTTP2_CANCEL), (stream: ClientHttp2Stream) => stream.destroy()]) {
        const { status, stream } = await into("127.0.0.1:3000");
        expect(status).toBe(200);
        // Never read: the agent's end fills, and the server's write waits.
        await vi.waitFor(() => expect(taken.at(-1)?.writableNeedDrain).toBe(true), { timeout: 5_000 });
        const before = taken.length;
        reset(stream);
        await vi.waitFor(() => expect(taken.slice(before - 1).every((socket) => socket.destroyed)).toBe(true), { timeout: 5_000 });
      }
      expect(await new Promise<number>((done) => endless.getConnections((_error, count) => done(count)))).toBe(0);
    } finally {
      await new Promise<void>((done) => endless.close(() => done()));
    }
  });
});
