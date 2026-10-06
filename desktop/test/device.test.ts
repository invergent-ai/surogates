import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { connectDevice, verifyDevice } from "../src/device.js";
import { OperationJournal } from "../src/journal/journal.js";
import type { DeviceLink } from "../src/link/client.js";
import type { Welcome } from "../src/link/protocol.js";
import type { Executor } from "../src/operations/runner.js";
import { FakeLinkServer } from "./fake-server.js";

const WELCOME: Welcome = { deviceId: "d", orgId: "o", agentId: "a", userId: "u", name: "Laptop", heartbeatS: 15 };
const nothing: Executor = { run: () => Promise.resolve({ ok: null }) };

let dir: string;
let server: FakeLinkServer;
let journal: OperationJournal;
let link: DeviceLink | null;

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "device-"));
  server = new FakeLinkServer();
  journal = new OperationJournal(join(dir, "journal.sqlite"));
  link = null;
});

afterEach(async () => {
  await link?.stop();
  await server.stop();
  journal.close();
  rmSync(dir, { recursive: true, force: true });
});

describe("the welcome", () => {
  it("is handed to onWelcome with the device's identity", async () => {
    const url = await server.start();
    const welcomes: Welcome[] = [];
    const device = connectDevice({
      url, token: "surg_dev_test", journal, executor: nothing, onError: () => {}, onWelcome: (w) => welcomes.push(w),
    });
    link = device.link;
    device.link.start();
    await server.until(() => welcomes.length === 1);
    expect(welcomes).toEqual([WELCOME]);
    expect(journal.claim("d")).toBe(true);
  });

  it("that onWelcome refuses stops the link, says why, and leaves the journal unclaimed", async () => {
    const url = await server.start();
    server.behindWelcome = [{
      type: "op", id: "op-1", session_id: "r", calling_session_id: "r", invocation_id: "1:c", ordinal: 1,
      kind: "which", args: { name: "sh" }, digest: "d1",
    }];
    const errors: unknown[] = [];
    const ran: string[] = [];
    const device = connectDevice({
      url, token: "surg_dev_test", journal, delay: () => 20, onError: (error) => errors.push(error),
      executor: { run: (op) => { ran.push(op.id); return Promise.resolve({ ok: null }); } },
      onWelcome: () => {
        throw new Error("this token is another agent's");
      },
    });
    link = device.link;
    device.link.start();
    await server.until(() => device.link.status === "stopped");
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(errors.map(String)).toEqual(["Error: this token is another agent's"]);
    expect(ran).toEqual([]);
    expect(server.connections).toBe(1);
    expect(journal.claim("another-device")).toBe(true);
  });
});

describe("verifying a token", () => {
  it("connects, reads the welcome, and closes without reporting anything held", async () => {
    const url = await server.start();
    expect(await verifyDevice(url, "surg_dev_test")).toEqual(WELCOME);
    await server.until(() => server.closes.length === 1);
    expect(server.closes).toEqual([1000]);
    expect(server.hellos).toEqual([{ type: "hello", protocols: [1], open: [] }]);
  });

  it("rejects a token the server does not know", async () => {
    const url = await server.start();
    await expect(verifyDevice(url, "surg_dev_other")).rejects.toThrow(
      "The agent did not accept this computer's token (unauthenticated)",
    );
    expect(server.connections).toBe(1);
  });

  it("rejects when no welcome comes in time", async () => {
    server = new FakeLinkServer({ welcome: false });
    const url = await server.start();
    await expect(verifyDevice(url, "surg_dev_test", 300)).rejects.toThrow("The agent did not answer in time");
  });
});
