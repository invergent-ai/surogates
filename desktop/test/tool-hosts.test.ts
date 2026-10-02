import { spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, realpathSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import type { Operation } from "../src/link/protocol.js";
import { FOLDER_UNAVAILABLE } from "../src/hosts/messages.js";
import {
  CANCELLED, forkHost, HOST_STOPPED, type HostProcess, NOT_BOUND, ToolHosts, type ToolHostsOptions,
} from "../src/hosts/tool-hosts.js";

const ROOT_A = "11111111-1111-4111-8111-111111111111";
const ROOT_B = "22222222-2222-4222-8222-222222222222";
const SLEEP = "37.25";

// How many of the fake rg's sleeps are running, the sandbox's included.
const sleeping = () => Number(spawnSync("pgrep", ["-fc", `^sleep ${SLEEP}$`], { encoding: "utf8" }).stdout.trim() || 0);

async function until(check: () => boolean, timeoutMs = 10_000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (!check()) {
    if (Date.now() > deadline) throw new Error("timed out");
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
}

let base: string;
let folders: Record<string, string>;
let spawned: HostProcess[];
let exits: number;
let hosts: ToolHosts | null;

const op = (kind: string, args: Record<string, unknown>, sessionId = ROOT_A): Operation => ({
  id: `${kind}-${Math.random()}`, sessionId, callingSessionId: sessionId, invocationId: "call", ordinal: 1, kind, args, digest: "d",
});

function toolHosts(overrides: Partial<ToolHostsOptions> = {}): ToolHosts {
  hosts = new ToolHosts({
    bindingOf: (root) => {
      const folder = folders[root];
      return folder ? { folder } : undefined;
    },
    dataDir: join(base, "data"),
    env: { HOME: process.env.HOME ?? "/home/tester", LANG: "C.UTF-8", PATH: `${folders[ROOT_A]}/bin:/usr/bin:/bin` },
    spawnHost: () => {
      const host = forkHost();
      host.onExit(() => {
        exits += 1;
      });
      spawned.push(host);
      return host;
    },
    ...overrides,
  });
  return hosts;
}

const signal = () => new AbortController().signal;

beforeEach(() => {
  base = realpathSync(mkdtempSync(join(tmpdir(), "tool-hosts-")));
  folders = { [ROOT_A]: join(base, "a"), [ROOT_B]: join(base, "b") };
  for (const folder of Object.values(folders)) mkdirSync(folder);
  // An rg that never answers, for operations that are still running; its sleep
  // has a length only it uses, so the test can see it run and stop.
  mkdirSync(join(folders[ROOT_A] ?? "", "bin"));
  writeFileSync(join(folders[ROOT_A] ?? "", "bin", "rg"), `#!/bin/sh\nexec sleep ${SLEEP}\n`, { mode: 0o755 });
  spawned = [];
  exits = 0;
  hosts = null;
});

afterEach(async () => {
  await hosts?.stop();
  for (const host of spawned) host.kill();
  rmSync(base, { recursive: true, force: true });
});

const slowSearch = () => op("ripgrep", { key: folders[ROOT_A], mode: "files", pattern: "*", glob: null, context: 0 });

describe("ToolHosts", { timeout: 30_000 }, () => {
  it("runs each root's operations in its own folder", async () => {
    const executor = toolHosts();
    expect(await executor.run(op("resolve", { path: "" }, ROOT_A), signal())).toEqual({ ok: folders[ROOT_A] });
    expect(await executor.run(op("resolve", { path: "" }, ROOT_B), signal())).toEqual({ ok: folders[ROOT_B] });
    expect(await executor.run(op("resolve", { path: "x" }, ROOT_A), signal())).toEqual({ ok: `${folders[ROOT_A]}/x` });
    expect(spawned).toHaveLength(2);
  });

  it("answers a root it has no folder for, and a bind it cannot confirm yet", async () => {
    const executor = toolHosts();
    expect(await executor.run(op("stat", { key: "/x" }, "33333333-3333-4333-8333-333333333333"), signal())).toEqual(FOLDER_UNAVAILABLE);
    expect(await executor.run(op("stat", { key: "/x" }, "../../etc"), signal())).toEqual(FOLDER_UNAVAILABLE);
    expect(await executor.run(op("bind", { folder: "/x", nonce: "n" }), signal())).toEqual(NOT_BOUND);
    expect(spawned).toHaveLength(0);
  });

  it("answers every operation when the sandbox cannot start, and tries again for the next", async () => {
    const executor = toolHosts({ bwrapPath: "/nonexistent/bwrap" });
    const first = await executor.run(op("stat", { key: "/x" }), signal());
    expect(first).toMatchObject({ error: { type: "unavailable" } });
    expect((first as { error: { message: string } }).error.message).toMatch(/bwrap/);
    expect(await executor.run(op("stat", { key: "/x" }), signal())).toMatchObject({ error: { type: "unavailable" } });
    expect(spawned).toHaveLength(2);
  });

  it("answers what was running as interrupted when its host dies, and starts a new host", async () => {
    const executor = toolHosts();
    expect(await executor.run(op("resolve", { path: "" }), signal())).toEqual({ ok: folders[ROOT_A] });
    const running = executor.run(slowSearch(), signal());
    await until(() => sleeping() === 1);
    spawned[0]?.kill();
    expect(await running).toEqual(HOST_STOPPED);
    await until(() => sleeping() === 0);
    expect(await executor.run(op("resolve", { path: "" }), signal())).toEqual({ ok: folders[ROOT_A] });
    expect(spawned).toHaveLength(2);
  });

  it("answers a cancelled operation at once and stops its work, and its host goes on", async () => {
    const executor = toolHosts();
    const controller = new AbortController();
    const running = executor.run(slowSearch(), controller.signal);
    await until(() => sleeping() === 1);
    controller.abort();
    expect(await running).toEqual(CANCELLED);
    await until(() => sleeping() === 0);
    expect(await executor.run(op("resolve", { path: "" }), signal())).toEqual({ ok: folders[ROOT_A] });
    expect(spawned).toHaveLength(1);
  });

  it("answers folder_unavailable for a folder that has gone before its host starts", async () => {
    const executor = toolHosts();
    renameSync(folders[ROOT_B] ?? "", `${folders[ROOT_B]}-moved`);
    expect(await executor.run(op("resolve", { path: "" }, ROOT_B), signal())).toEqual(FOLDER_UNAVAILABLE);
    expect(spawned).toHaveLength(0);
  });

  it("starts no host once stopped", async () => {
    const executor = toolHosts();
    await executor.stop();
    expect(await executor.run(op("resolve", { path: "" }), signal())).toMatchObject({ error: { type: "unavailable" } });
    expect(spawned).toHaveLength(0);
  });

  it("stops every host", async () => {
    const executor = toolHosts();
    await executor.run(op("resolve", { path: "" }, ROOT_A), signal());
    await executor.run(op("resolve", { path: "" }, ROOT_B), signal());
    await executor.stop();
    expect(exits).toBe(2);
  });
});
