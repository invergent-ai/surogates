import { afterEach, describe, expect, it } from "vitest";

import { CLOSE_TIMEOUT_MS, DeviceLink, type LinkHandlers, type LinkStatus } from "../src/link/client.js";
import type { Operation } from "../src/link/protocol.js";
import { FakeLinkServer } from "./fake-server.js";

const servers: FakeLinkServer[] = [];
const links: DeviceLink[] = [];

afterEach(async () => {
  for (const link of links.splice(0)) await link.stop();
  for (const server of servers.splice(0)) await server.stop();
});

interface Seen {
  statuses: LinkStatus[];
  operations: Operation[];
  cancels: string[];
  acks: string[];
  errors: unknown[];
}

async function connected(
  options: ConstructorParameters<typeof FakeLinkServer>[0] = {},
  linkOptions: {
    token?: string;
    open?: string[];
    welcomeTimeoutMs?: number;
    delay?: (attempt: number) => number;
    handlers?: Partial<LinkHandlers>;
  } = {},
): Promise<{ server: FakeLinkServer; link: DeviceLink; seen: Seen }> {
  const server = new FakeLinkServer(options);
  servers.push(server);
  const url = await server.start();
  const seen: Seen = { statuses: [], operations: [], cancels: [], acks: [], errors: [] };
  const link = new DeviceLink({
    url,
    token: linkOptions.token ?? "surg_dev_test",
    openIds: () => linkOptions.open ?? [],
    welcomeTimeoutMs: linkOptions.welcomeTimeoutMs,
    delay: linkOptions.delay ?? (() => 20),
    handlers: {
      onOperation: (operation) => seen.operations.push(operation),
      onCancel: (id) => seen.cancels.push(id),
      onAck: (id) => seen.acks.push(id),
      onStatus: (status) => seen.statuses.push(status),
      onError: (error) => seen.errors.push(error),
      ...linkOptions.handlers,
    },
  });
  links.push(link);
  link.start();
  return { server, link, seen };
}

describe("connecting", () => {
  it("says hello with the protocol, what it holds unfinished, and the device token", async () => {
    const { server, link } = await connected({}, { open: ["op-1"] });
    await server.until(() => link.status === "connected");
    expect(server.hellos).toEqual([{ type: "hello", protocols: [1], open: ["op-1"] }]);
  });

  it("sends nothing before the welcome", async () => {
    const { server, link } = await connected({ welcome: false });
    await server.until(() => server.hellos.length === 1);
    expect(link.send({ type: "ping" })).toBe(false);
  });

  it("reconnects when no welcome arrives in time", async () => {
    const { server } = await connected({ welcome: false }, { welcomeTimeoutMs: 100 });
    await server.until(() => server.connections >= 2);
  });
});

describe("staying connected", () => {
  // The shortest heartbeat a welcome may ask for is 1 s, so these run in real seconds.
  const pings = (server: FakeLinkServer) => server.received.filter((f) => f.type === "ping").length;

  it("pings every heartbeat", async () => {
    const { server } = await connected({ heartbeatS: 1 });
    await server.until(() => pings(server) >= 3, 5_000);
  });

  it("stays connected while the server answers, past the welcome deadline", async () => {
    const { server } = await connected({ heartbeatS: 1 }, { welcomeTimeoutMs: 100 });
    await server.until(() => pings(server) >= 3, 5_000);
    expect(server.connections).toBe(1);
  });

  it("drops a server that falls silent two heartbeats after its first unanswered ping", async () => {
    const began = Date.now();
    const { server } = await connected({ heartbeatS: 1, pong: false });
    await server.until(() => server.connections >= 2, 5_000);
    // The first ping goes out at 1 s, so the drop is at 3 s: after the second
    // ping (2 s), and not a tick later (4 s).
    expect(pings(server)).toBe(2);
    expect(Date.now() - began).toBeLessThan(3_500);
    // 1006: the app cut the link itself, it was not a protocol close.
    expect(server.closes[0]).toBe(1006);
  });

  it("counts any frame as the server answering, not only a pong", async () => {
    const { server } = await connected({ heartbeatS: 1, pong: false });
    for (const ping of [1, 2, 3]) {
      await server.until(() => pings(server) >= ping, 5_000);
      server.send({ type: "op_ack", id: "op-0" });
    }
    expect(server.connections).toBe(1);
  });

  it("reconnects after an ordinary close", async () => {
    const { server, link } = await connected();
    await server.until(() => link.status === "connected");
    server.close(1011);
    await server.until(() => server.connections === 2 && link.status === "connected");
  });

  it("closes a malformed frame with 4400, and reconnects", async () => {
    const { server, link } = await connected();
    await server.until(() => link.status === "connected");
    server.send("{not json");
    await server.until(() => server.connections === 2);
    expect(server.closes[0]).toBe(4400);
  });
});

describe("stopping and starting", () => {
  it("starts again after a stop that came while it waited to reconnect", async () => {
    // A wait too long to end by itself: the stop always lands inside it.
    const { server, link } = await connected({}, { delay: () => 60_000 });
    await server.until(() => link.status === "connected");
    server.close(1011);
    await server.until(() => link.status === "offline");
    await link.stop();
    link.start();
    await server.until(() => server.connections === 2 && link.status === "connected");
  });

  it("does not wait on a server that never answers the close", async () => {
    const { server, link } = await connected();
    await server.until(() => link.status === "connected");
    server.stall();
    const began = Date.now();
    await link.stop();
    expect(Date.now() - began).toBeLessThan(CLOSE_TIMEOUT_MS + 1_000);
    expect(link.status).toBe("stopped");
  });
});

describe("backing off", () => {
  it("does not start the backoff over at a welcome", async () => {
    const attempts: number[] = [];
    const { server, link } = await connected({}, { delay: (attempt) => (attempts.push(attempt), 20) });
    await server.until(() => link.status === "connected");
    server.close(1011);
    await server.until(() => server.connections === 2 && link.status === "connected");
    server.close(1011);
    await server.until(() => server.connections === 3);
    expect(attempts).toEqual([0, 1]);
  });

  it("starts the backoff over when the server answers a ping", async () => {
    const attempts: number[] = [];
    const { server, link, seen } = await connected(
      { heartbeatS: 1 },
      { delay: (attempt) => (attempts.push(attempt), 20) },
    );
    await server.until(() => link.status === "connected");
    server.close(1011);
    await server.until(() => server.connections === 2 && link.status === "connected");
    await server.until(() => server.received.some((frame) => frame.type === "ping"), 5_000);
    // Frames arrive in order: once this cancel is in, the pong before it was read.
    server.send({ type: "cancel", id: "op-0" });
    await server.until(() => seen.cancels.length === 1);
    server.close(1011);
    await server.until(() => server.connections === 3);
    expect(attempts).toEqual([0, 0]);
  });
});

describe("stopping for good", () => {
  it.each([
    [4401, "unauthenticated"],
    [4403, "revoked"],
    [4409, "superseded"],
  ] as const)("after close %i it reports %s and does not reconnect", async (code, status) => {
    const { server, link, seen } = await connected();
    await server.until(() => link.status === "connected");
    server.close(code);
    await server.until(() => link.status === status);
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(server.connections).toBe(1);
    expect(seen.statuses.at(-1)).toBe(status);
  });

  it("keeps the reason it ended when it is stopped afterwards", async () => {
    const { server, link } = await connected();
    await server.until(() => link.status === "connected");
    server.close(4403);
    await server.until(() => link.status === "revoked");
    await link.stop();
    expect(link.status).toBe("revoked");
  });

  it("stops when the server needs a newer app", async () => {
    const { server, link } = await connected({ welcome: false });
    await server.until(() => server.hellos.length === 1);
    server.send({ type: "error", code: "unsupported_protocol", supported: [2] });
    server.close(4400);
    await server.until(() => link.status === "update_required");
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(server.connections).toBe(1);
  });

  it("is refused a wrong token, and stops", async () => {
    const { server, link } = await connected({}, { token: "surg_dev_wrong" });
    await server.until(() => link.status === "unauthenticated");
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(server.connections).toBe(1);
  });
});

describe("what the server sends", () => {
  it("takes the first welcome and ignores a repeat", async () => {
    const { server, link, seen } = await connected();
    await server.until(() => link.status === "connected");
    server.send({
      type: "welcome", protocol: 1, device_id: "d", org_id: "o", agent_id: "a",
      user_id: "u", name: "Laptop", heartbeat_s: 15,
    });
    server.send({ type: "cancel", id: "op-9" });
    await server.until(() => seen.cancels.length === 1);
    expect(seen.statuses.filter((status) => status === "connected")).toHaveLength(1);
  });

  it("passes on operations, acknowledgements and cancels", async () => {
    const { server, link, seen } = await connected();
    await server.until(() => link.status === "connected");
    server.send({
      type: "op", id: "op-1", session_id: "r", calling_session_id: "r", invocation_id: "1:c",
      ordinal: 1, kind: "which", args: { name: "sh" }, digest: "d",
    });
    server.send({ type: "op_ack", id: "op-0" });
    server.send({ type: "cancel", id: "op-2" });
    await server.until(() => seen.operations.length === 1 && seen.acks.length === 1 && seen.cancels.length === 1);
    expect(seen.operations[0]?.id).toBe("op-1");
    expect(seen.acks).toEqual(["op-0"]);
    expect(seen.cancels).toEqual(["op-2"]);
  });
});

describe("a handler that throws", () => {
  const failure = new Error("journal broke");
  const throws = () => {
    throw failure;
  };
  const op = {
    type: "op", id: "op-1", session_id: "r", calling_session_id: "r", invocation_id: "1:c",
    ordinal: 1, kind: "which", args: { name: "sh" }, digest: "d",
  };

  it.each([
    ["onWelcome", { onWelcome: throws }, null],
    ["onOperation", { onOperation: throws }, op],
    ["onCancel", { onCancel: throws }, { type: "cancel", id: "op-2" }],
    ["onAck", { onAck: throws }, { type: "op_ack", id: "op-0" }],
  ] as const)("in %s is reported, and stops the link for good", async (_name, handlers, frame) => {
    const { server, link, seen } = await connected({}, { handlers });
    await server.until(() => server.hellos.length === 1);
    if (frame !== null) {
      await server.until(() => link.status === "connected");
      server.send({ ...frame });
    }
    await server.until(() => link.status === "stopped");
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(seen.errors).toEqual([failure]);
    expect(server.connections).toBe(1);
    expect(link.status).toBe("stopped");
  });
});
