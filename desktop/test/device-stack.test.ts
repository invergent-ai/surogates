import { mkdirSync, mkdtempSync, realpathSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { FolderGuards } from "../src/binding/folder.js";
import type { NetworkApprovals } from "../src/hosts/tool-hosts.js";
import type { Bindings } from "../src/journal/bindings.js";
import { OperationJournal } from "../src/journal/journal.js";
import type { LinkStatus } from "../src/link/client.js";
import type { Operation, Outcome } from "../src/link/protocol.js";
import { APP_CLOSED } from "../src/operations/runner.js";
import { type DeviceStackOptions, startDevice, type ToolLayer } from "../src/shell/device-stack.js";
import { FakeLinkServer } from "./fake-server.js";

const ROOT = "66666666-6666-4666-8666-666666666666";
// A sub-agent of the chat: its operations run in the chat's folder, as sessions of their own.
const CHILD = "77777777-7777-4777-8777-777777777777";
const IDENTITY = { deviceId: "d", orgId: "o", agentId: "a", userId: "u" };

// The tool layer under the binder, as far as the stack sees it.
class Tools implements ToolLayer {
  readonly ran: Operation[] = [];
  hold: "no" | "until-aborted" | "forever" = "no";
  bindings: Bindings | null = null;
  network: NetworkApprovals | null = null;

  constructor(private readonly base: string, private readonly order: string[]) {}

  guards(): FolderGuards {
    return { home: join(this.base, "home"), dataDir: join(this.base, "data"), appDirs: [] };
  }

  run(operation: Operation, signal: AbortSignal): Promise<Outcome> {
    this.ran.push(operation);
    if (this.hold === "no") return Promise.resolve({ ok: `ran ${operation.kind}` });
    return new Promise((resolve) => {
      if (this.hold === "until-aborted") {
        signal.addEventListener("abort", () => {
          this.order.push("aborted");
          resolve({ error: { type: "cancelled", message: "stopped" } });
        });
      }
    });
  }

  stop(): Promise<void> {
    this.order.push("tools");
    return Promise.resolve();
  }
}

let base: string;
let folder: string;
let server: FakeLinkServer;
let order: string[];
let tools: Tools;
let statuses: LinkStatus[];
let errors: unknown[];
let stops: Array<() => Promise<void>>;

beforeEach(() => {
  base = realpathSync(mkdtempSync(join(tmpdir(), "stack-")));
  folder = join(base, "home", "work");
  mkdirSync(folder, { recursive: true });
  server = new FakeLinkServer();
  order = [];
  tools = new Tools(base, order);
  statuses = [];
  errors = [];
  stops = [];
});

afterEach(async () => {
  for (const stop of stops) await stop();
  await server.stop();
  rmSync(base, { recursive: true, force: true });
});

async function start(overrides: Partial<DeviceStackOptions> = {}) {
  const url = await server.start();
  const device = startDevice({
    journalPath: join(base, "data", "devices", "d", "journal.sqlite"),
    url,
    token: "surg_dev_test",
    agent: "agent.example.com",
    identity: IDENTITY,
    tools: (bindings, network) => {
      tools.bindings = bindings;
      tools.network = network;
      return tools;
    },
    prompts: { pickFolder: () => Promise.resolve(folder), confirmFolder: (sheet) => Promise.resolve({ mode: sheet.mode }) },
    approvalPrompts: { approve: () => Promise.resolve("deny"), confirmFreeMode: () => Promise.resolve(false) },
    onStatus: (status) => {
      statuses.push(status);
      if (status === "stopped") order.push("link");
    },
    onError: (error) => errors.push(error),
    delay: () => 20,
    ...overrides,
  });
  stops.push(() => device.stop());
  return device;
}

function op(id: string, kind: string, args: Record<string, unknown>, own = false, calling = ROOT): Record<string, unknown> {
  return {
    type: "op", id, session_id: ROOT, calling_session_id: calling, invocation_id: own ? "bind" : "1:c",
    ordinal: own ? 0 : 1, kind, args, digest: `digest-${id}`,
  };
}

const results = (id: string) => server.received.filter((frame) => frame.type === "op_result" && frame.id === id);

describe("one agent's device", () => {
  it("runs a bound chat's operations on its tools, through the binder", async () => {
    const device = await start();
    await server.until(() => statuses.includes("connected"));
    const prepared = await device.binder.prepareFolder("pick", "window-1", new AbortController().signal);
    expect(prepared?.folder).toBe(folder);
    server.send(op("bind-1", "bind", { folder: prepared?.folder, nonce: prepared?.nonce }, true));
    await server.until(() => results("bind-1").length === 1);
    expect(results("bind-1")[0]?.outcome).toEqual({ ok: null });
    expect(tools.bindings?.get(ROOT)?.folder).toBe(folder);
    server.send(op("run-1", "run", { command: "echo hi", workdir: null, timeout: 10 }));
    await server.until(() => results("run-1").length === 1);
    expect(results("run-1")[0]?.outcome).toEqual({ ok: "ran run" });
    expect(tools.ran.map((ran) => ran.id)).toEqual(["run-1"]);
    // The tools' network questions go to the binder's approvals: a chat this computer did not bind is denied.
    expect(await tools.network?.askNetwork("77777777-7777-4777-8777-777777777777",
      { host: "example.com", port: 443, privateNetwork: false }, new AbortController().signal)).toBe("deny");
  });

  it("stops its link when the welcome names another identity, and says why", async () => {
    await start({ identity: { ...IDENTITY, agentId: "another-agent" } });
    await server.until(() => statuses.includes("stopped"));
    expect(errors.map(String)).toEqual([
      "Error: This computer's token now connects to agent a, not another-agent, so its link stopped",
    ]);
  });

  it("quits in order: the link, what runs recorded closed by the app, the tools, the journal", async () => {
    const device = await start();
    await server.until(() => statuses.includes("connected"));
    tools.hold = "until-aborted";
    // A chat bound earlier, as the journal keeps it.
    const prepared = await device.binder.prepareFolder("pick", "window-1", new AbortController().signal);
    server.send(op("bind-1", "bind", { folder: prepared?.folder, nonce: prepared?.nonce }, true));
    await server.until(() => results("bind-1").length === 1);
    server.send(op("run-1", "run", { command: "sleep 9", workdir: null, timeout: 10 }));
    await server.until(() => tools.ran.length === 1);
    await device.stop();
    expect(order).toEqual(["link", "aborted", "tools"]);
    // A second stop, as a second quit would ask, changes nothing.
    await device.stop();
    expect(order).toEqual(["link", "aborted", "tools"]);
    // Closed: the file opens again, and the operation is recorded closed by the app.
    const journal = new OperationJournal(join(base, "data", "devices", "d", "journal.sqlite"));
    try {
      expect(journal.unsent().find((result) => result.id === "run-1")?.outcome).toEqual(APP_CLOSED);
    } finally {
      journal.close();
    }
  });

  it("counts the sessions whose operations its tools run, and says each time the count changes", async () => {
    const counts: number[] = [];
    const device = await start({ onWorking: (count) => counts.push(count) });
    await server.until(() => statuses.includes("connected"));
    tools.hold = "until-aborted";
    const prepared = await device.binder.prepareFolder("pick", "window-1", new AbortController().signal);
    server.send(op("bind-1", "bind", { folder: prepared?.folder, nonce: prepared?.nonce }, true));
    await server.until(() => results("bind-1").length === 1);
    expect(device.working()).toBe(0);
    server.send(op("run-1", "run", { command: "sleep 9", workdir: null, timeout: 10 }));
    server.send(op("run-2", "run", { command: "sleep 9", workdir: null, timeout: 10 }));
    await server.until(() => tools.ran.length === 2);
    expect(device.working()).toBe(1);
    server.send({ type: "cancel", id: "run-1" });
    server.send({ type: "cancel", id: "run-2" });
    await server.until(() => device.working() === 0);
    expect(counts).toEqual([1, 0]);
  });

  it("counts a chat's sub-agent apart from the chat, as a session of its own", async () => {
    const counts: number[] = [];
    const device = await start({ onWorking: (count) => counts.push(count) });
    await server.until(() => statuses.includes("connected"));
    tools.hold = "until-aborted";
    const prepared = await device.binder.prepareFolder("pick", "window-1", new AbortController().signal);
    server.send(op("bind-1", "bind", { folder: prepared?.folder, nonce: prepared?.nonce }, true));
    await server.until(() => results("bind-1").length === 1);
    server.send(op("run-1", "run", { command: "sleep 9", workdir: null, timeout: 10 }));
    server.send(op("run-2", "run", { command: "sleep 9", workdir: null, timeout: 10 }, false, CHILD));
    await server.until(() => tools.ran.length === 2);
    expect(device.working()).toBe(2);
    server.send({ type: "cancel", id: "run-1" });
    server.send({ type: "cancel", id: "run-2" });
    await server.until(() => device.working() === 0);
    expect(counts).toEqual([1, 2, 1, 0]);
  });

  it("clears its deadline once what runs has stopped, so nothing is left waiting on it", async () => {
    const device = await start({ quitTimeoutMs: 54_321 });
    await server.until(() => statuses.includes("connected"));
    const armed = vi.spyOn(globalThis, "setTimeout");
    const cleared = vi.spyOn(globalThis, "clearTimeout");
    try {
      await device.stop();
      const deadlines = armed.mock.calls.flatMap((call, index) => (call[1] === 54_321 ? [armed.mock.results[index]?.value] : []));
      expect(deadlines).toHaveLength(1);
      expect(cleared.mock.calls.map(([timer]) => timer)).toContain(deadlines[0]);
    } finally {
      armed.mockRestore();
      cleared.mockRestore();
    }
  });

  it("waits no longer than its deadline for an operation that ignores the quit", async () => {
    const device = await start({ quitTimeoutMs: 200 });
    await server.until(() => statuses.includes("connected"));
    tools.hold = "forever";
    const prepared = await device.binder.prepareFolder("pick", "window-1", new AbortController().signal);
    server.send(op("bind-1", "bind", { folder: prepared?.folder, nonce: prepared?.nonce }, true));
    await server.until(() => results("bind-1").length === 1);
    server.send(op("run-1", "run", { command: "sleep 9", workdir: null, timeout: 10 }));
    await server.until(() => tools.ran.length === 1);
    const started = performance.now();
    await device.stop();
    expect(performance.now() - started).toBeLessThan(2_000);
    expect(order).toEqual(["link", "tools"]);
    // The journal closed with it still started: the next launch answers it interrupted.
    const journal = new OperationJournal(join(base, "data", "devices", "d", "journal.sqlite"));
    try {
      expect(journal.recovered).toBe(1);
    } finally {
      journal.close();
    }
  });
});
