import { spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, realpathSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, expect, it } from "vitest";

import { connectDevice } from "../src/device.js";
import { ToolHosts } from "../src/hosts/tool-hosts.js";
import { OperationJournal } from "../src/journal/journal.js";
import type { DeviceLink } from "../src/link/client.js";
import { ACCESS_ENDED } from "../src/operations/runner.js";
import { FakeLinkServer } from "./fake-server.js";

const ROOT = "33333333-3333-4333-8333-333333333333";
const sleeping = () => Number(spawnSync("pgrep", ["-fc", "^sleep 619$"], { encoding: "utf8" }).stdout.trim() || 0);

async function until(check: () => boolean, timeoutMs = 10_000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (!check()) {
    if (Date.now() > deadline) throw new Error("timed out");
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
}

let base: string;
let server: FakeLinkServer;
let journal: OperationJournal;
let hosts: ToolHosts;
let link: DeviceLink | null;

beforeEach(() => {
  base = realpathSync(mkdtempSync(join(tmpdir(), "revocation-")));
  mkdirSync(join(base, "folder"));
  server = new FakeLinkServer();
  link = null;
  journal = new OperationJournal(join(base, "journal.sqlite"));
  hosts = new ToolHosts({
    bindingOf: () => ({ folder: join(base, "folder") }),
    dataDir: join(base, "data"),
    env: { HOME: process.env.HOME ?? "/home/tester", LANG: "C.UTF-8", PATH: "/usr/bin:/bin" },
  });
});

afterEach(async () => {
  await link?.stop();
  await hosts.stop();
  await server.stop();
  journal.close();
  rmSync(base, { recursive: true, force: true });
});

it("ends a running command when the computer's access is revoked, and records it interrupted", async () => {
  const url = await server.start();
  const device = connectDevice({ url, token: "surg_dev_test", journal, executor: hosts, onError: () => {}, delay: () => 20 });
  link = device.link;
  device.link.start();
  await server.until(() => device.link.status === "connected");
  server.send({
    type: "op", id: "op-1", session_id: ROOT, calling_session_id: ROOT, invocation_id: "1:c", ordinal: 1,
    kind: "run", args: { command: "sleep 619", workdir: null, timeout: 900 }, digest: "d1",
  });
  await until(() => sleeping() >= 1, 20_000);
  server.close(4403);
  await until(() => sleeping() === 0);
  await until(() => journal.unsent().length === 1);
  expect(journal.unsent()[0]?.outcome).toEqual(ACCESS_ENDED);
}, 40_000);
