// What a folder's landings keep is the app's, by this computer's own path: the app reads it, a forgetting's records
// among it, and finds a landing's host by it before the folder is read, while that host's helper is handed it by its
// path in the guest, as it is every folder (session-path.ts). On Linux and macOS the two are one path; here they are
// made two, as they are on Windows, so that the one used for the other shows.

import { mkdirSync, mkdtempSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { BOOT_ID } from "../src/binding/folder.js";
import { RECORDS_UNREAD } from "../src/history/kinds.js";
import { keyOf } from "../src/history/place.js";
import type { FromHost, HostStart, ToHost } from "../src/hosts/messages.js";
import { type HostProcess, type Recovery, ToolHosts } from "../src/hosts/tool-hosts.js";
import type { Operation } from "../src/link/protocol.js";

// A path in the guest that is no path of this computer's, and back.
vi.mock("../src/session-path.js", async (original) => ({
  ...(await original<typeof import("../src/session-path.js")>()),
  guestPathOf: (path: string) => `/guest${path}`,
  hostPathOf: (path: string) => path.replace(/^\/guest/, ""),
}));

const ROOT = "11111111-1111-4111-8111-111111111111";
const SAGA = "s1";

// A host whose helper answers each step ok, and goes when it is told to stop.
class AnsweringHost implements HostProcess {
  readonly sent: ToHost[] = [];
  private readonly heard: Array<(message: FromHost) => void> = [];
  private readonly ends: Array<() => void> = [];
  private gone = false;

  send(message: ToHost): void {
    this.sent.push(message);
    queueMicrotask(() => {
      if (message.type === "start") this.say({ type: "ready", processes: [] });
      if (message.type === "op") this.say({ type: "result", id: message.id, outcome: { ok: {} } });
      if (message.type === "stop") this.kill();
    });
  }

  onMessage(listener: (message: FromHost) => void): void {
    this.heard.push(listener);
  }

  onExit(listener: () => void): void {
    this.ends.push(listener);
  }

  kill(): void {
    if (this.gone) return;
    this.gone = true;
    for (const listener of this.ends) listener();
  }

  private say(message: FromHost): void {
    for (const listener of this.heard) listener(message);
  }
}

describe("what a folder's landings keep, where its path in the guest is another than this computer's", () => {
  let base: string;
  let tools: ToolHosts;
  let spawned: AnsweringHost[];
  let told: Recovery[];
  let folder: string;
  let kept: string;
  const step = (action: string, more: Record<string, unknown> = {}): Operation => ({
    id: `${action}-${Math.random()}`, sessionId: ROOT, callingSessionId: ROOT, invocationId: "land:7", ordinal: 1, kind: "land", args: { action, ...more }, digest: "d",
  });
  const signal = () => new AbortController().signal;

  beforeEach(() => {
    base = realpathSync(mkdtempSync(join(tmpdir(), "landing-kept-")));
    folder = `/guest${join(base, "folder")}`;
    const key = keyOf(folder);
    const history = join(base, "data", "history", key);
    kept = join(base, "data", "landings", key);
    mkdirSync(history, { recursive: true });
    spawned = [];
    told = [];
    const identity = { dev: 1, ino: 2, boot: BOOT_ID };
    tools = new ToolHosts({
      bindingOf: (root) => (root === ROOT ? { folder, ...identity, history: ROOT } : undefined),
      copies: {
        open: async (root) => ({
          copy: { place: { key, history, real: { path: folder, ...identity } }, folder: { path: `/guest${join(history, "threads", root)}`, dev: 1, ino: 3, boot: BOOT_ID }, at: folder },
          handle: Object.freeze({ root }),
        }),
        close: () => {},
        // The folder's history: the landing may go, recorded.
        ask: async () => ({ ok: { landing: "e".repeat(40) } }),
      },
      dataDir: join(base, "data"),
      cacheDir: join(base, "cache"),
      env: { HOME: base, LANG: "C.UTF-8" },
      spawnHost: () => {
        const host = new AnsweringHost();
        spawned.push(host);
        return host;
      },
      recovered: (recovery) => void told.push(recovery),
    });
  });

  afterEach(async () => {
    await tools.stop();
    rmSync(base, { recursive: true, force: true });
  });

  it("is handed to a landing's helper by its path in the guest, and read by the app by its own: a forgetting it cannot check forgets nothing", async () => {
    expect(await tools.land(step("revisions", { paths: ["a.txt"] }), signal())).toEqual({ ok: {} });
    expect((spawned[0]?.sent[0] as HostStart).landing).toEqual({ copy: `/guest${join(base, "data", "history", keyOf(folder), "threads", ROOT)}`, kept: `/guest${kept}` });
    // What its helper kept of a step, a record the app cannot read: no forgetting can name every step it holds.
    mkdirSync(join(kept, SAGA), { recursive: true });
    writeFileSync(join(kept, SAGA, "0.json"), "a step's record");
    expect(await tools.land(step("forget", { saga: SAGA, applied: [] }), signal())).toEqual(RECORDS_UNREAD);
    expect(spawned[0]?.sent.filter((message) => message.type === "op")).toHaveLength(1);
  });

  it("finds the landing's host on the folder before the folder is read: its helper has put back what was cut short, and no recovery's host is started", async () => {
    expect(await tools.land(step("revisions", { paths: ["a.txt"] }), signal())).toEqual({ ok: {} });
    mkdirSync(join(kept, SAGA), { recursive: true });
    writeFileSync(join(kept, SAGA, "0.json"), "a step's record");
    expect(await tools.recoverBefore(folder, signal())).toBeNull();
    expect([spawned.length, told]).toEqual([1, []]);
  });
});
