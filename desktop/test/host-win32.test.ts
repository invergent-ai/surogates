// A file host on Windows, through the app's own path: forkHost starts it through surogate-helper.exe's spawn
// (clean-child.ts), on the app's node, and the host starts its helper with no sandbox, its walk the containment
// (hosts/host.ts). Every folder it is given is named by its path in the guest. A chat's host answers its operations
// and refuses a git hook a command left outright; a landing's host lands a thread's work from its copy and puts it back;
// a recovery's host puts back what a landing cut short. On Windows alone, with the app built (npm run build:win32).

import { createHash } from "node:crypto";
import { closeSync, existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { BOOT_ID } from "../src/binding/folder.js";
import { identityFrom } from "../src/files/fd-paths-win32.js";
import { ntat } from "../src/files/ntat.js";
import { keyOf } from "../src/history/place.js";
import { HOOKS_REFUSED_NOTICE } from "../src/hosts/hooks.js";
import type { FromHost, HostStart } from "../src/hosts/messages.js";
import { forkHost, type HostProcess } from "../src/hosts/tool-hosts.js";
import { guestPathOf } from "../src/session-path.js";

const PACKAGE = fileURLToPath(new URL("..", import.meta.url));
const SAGA = "0f6d1c5e-7a3b-4c2d-9e1f-0a1b2c3d4e5f";
const THREAD = "5b0f2c1e-3d4a-4b5c-8d6e-7f8091a2b3c4";
const blob = (text: string) => createHash("sha1").update(`blob ${Buffer.byteLength(text)}\0`).update(text).digest("hex");

// The identity of the folder *path*, as the walk reads it.
function walkedIdentity(path: string): { dev: number; ino: number } {
  const fd = ntat().openRoot(`\\\\?\\${path}`);
  try {
    return identityFrom(ntat().query(fd));
  } finally {
    closeSync(fd);
  }
}

// A host the app's way, on the app's own node, beside which its rg.exe is, its messages kept.
class Host {
  readonly said: FromHost[] = [];
  gone = false;
  readonly process: HostProcess;

  constructor(start: HostStart) {
    this.process = forkHost();
    this.process.onMessage((message) => this.said.push(message));
    this.process.onExit(() => (this.gone = true));
    this.process.send(start);
  }

  async until<T>(find: (said: FromHost[]) => T | undefined, ms = 30_000): Promise<T> {
    for (const deadline = Date.now() + ms; ;) {
      const found = find(this.said);
      if (found !== undefined) return found;
      if (Date.now() > deadline || this.gone) throw new Error(`no answer; the host said ${JSON.stringify(this.said)}`);
      await new Promise((resolve) => setTimeout(resolve, 20));
    }
  }

  ready(): Promise<FromHost> {
    return this.until((said) => said.find((message) => message.type === "ready" || message.type === "failed")).then((message) => {
      if (message.type === "failed") throw new Error(`the host did not start: ${message.message}`);
      return message;
    });
  }

  result(id: string): Promise<unknown> {
    return this.until((said) => {
      const found = said.find((message) => message.type === "result" && message.id === id);
      return found?.type === "result" ? found.outcome : undefined;
    });
  }

  op(id: string, kind: string, args: Record<string, unknown>): Promise<unknown> {
    this.process.send({ type: "op", id, kind, args });
    return this.result(id);
  }

  async stop(): Promise<void> {
    if (!this.gone) this.process.send({ type: "stop" });
    for (const deadline = Date.now() + 10_000; !this.gone && Date.now() < deadline;) await new Promise((resolve) => setTimeout(resolve, 50));
    if (!this.gone) this.process.kill();
  }
}

describe.runIf(process.platform === "win32")("a file host on Windows", { timeout: 60_000 }, () => {
  let base: string;
  let folder: string;
  let data: string;
  let guest: string;
  let start: HostStart;
  const hosts: Host[] = [];
  const host = (more: Partial<HostStart> = {}) => {
    const made = new Host({ ...start, ...more });
    hosts.push(made);
    return made;
  };

  beforeEach(() => {
    base = realpathSync.native(mkdtempSync(join(tmpdir(), "host-")));
    folder = join(base, "folder");
    data = join(base, "data");
    for (const dir of [folder, data, join(base, "home")]) mkdirSync(dir);
    writeFileSync(join(folder, "a.txt"), "alpha\n");
    guest = guestPathOf(folder);
    start = {
      type: "start",
      folder: guest,
      expect: { ...walkedIdentity(folder), boot: BOOT_ID },
      tmp: join(data, "tmp", "root"),
      dataDir: data,
      cacheDir: join(base, "cache"),
      env: { HOME: join(base, "home"), LANG: "C.UTF-8", PATH: process.env.PATH ?? "" },
      appDirs: [dirname(process.execPath), PACKAGE],
    };
  });

  afterEach(async () => {
    for (const each of hosts.splice(0)) await each.stop();
    rmSync(base, { recursive: true, force: true, maxRetries: 10 });
  });

  it("answers a chat's operations by the folder's path in the guest, through its helper, and stops cleanly", async () => {
    const chat = host();
    await chat.ready();
    expect(await chat.op("1", "read", { key: `${guest}/a.txt`, max_bytes: null })).toEqual({ ok: Buffer.from("alpha\n").toString("base64") });
    expect(await chat.op("2", "write", { key: `${guest}/notes/b.txt`, data: Buffer.from("beta\n").toString("base64") })).toEqual({ ok: null });
    expect(readFileSync(join(folder, "notes", "b.txt"), "utf8")).toBe("beta\n");
    // Its record, kept by the folder's identity, says it runs.
    expect(readdirSync(join(data, "folders"))).toHaveLength(1);
    await chat.stop();
    expect(chat.gone).toBe(true);
  });

  it("refuses a git hook a command left in the folder outright: moved off its name, beside it, and told in the command's output", async () => {
    mkdirSync(join(folder, ".git", "hooks"), { recursive: true });
    writeFileSync(join(folder, ".git", "hooks", "pre-push"), "#!/bin/sh\necho the user's own\n");
    const chat = host();
    await chat.ready();
    chat.process.send({ type: "refusal", id: "r1", run: true });
    expect(await chat.result("r1")).toEqual({ ok: null });
    // What a command in the guest would have left: a hook git for Windows runs, whatever its mode.
    writeFileSync(join(folder, ".git", "hooks", "pre-commit"), "#!/bin/sh\necho planted\n");
    chat.process.send({ type: "after", id: "a1", outcome: { ok: { output: "done", returncode: 0, timed_out: false } } });
    expect(await chat.result("a1")).toEqual({ ok: { output: `done\n${HOOKS_REFUSED_NOTICE}.git/hooks/pre-commit`, returncode: 0, timed_out: false } });
    expect(readdirSync(join(folder, ".git", "hooks")).sort()).toEqual(["pre-commit (refused by Surogate)", "pre-push"]);
    expect(readFileSync(join(folder, ".git", "hooks", "pre-commit (refused by Surogate)"), "utf8")).toBe("#!/bin/sh\necho planted\n");
  });

  it("lands a thread's work from its copy into the folder, and puts it back, through a landing's host", async () => {
    const key = keyOf(guest);
    const copy = join(data, "history", key, "threads", THREAD);
    mkdirSync(copy, { recursive: true });
    writeFileSync(join(copy, "a.txt"), "alpha, by the thread\n");
    const kept = join(data, "landings", key);
    const lander = host({ tmp: join(data, "tmp", "root.land"), landing: { copy: guestPathOf(copy), kept: guestPathOf(kept) } });
    await lander.ready();
    const looked = (await lander.op("1", "land", { action: "revisions", paths: ["a.txt"] })) as { ok: { revisions: Array<[string, string]> } };
    const [[, expected]] = looked.ok.revisions as [[string, string]];
    const apply = { action: "apply", saga: SAGA, step: 1, path: "a.txt", before: blob("alpha\n"), after: blob("alpha, by the thread\n"), expected };
    expect(await lander.op("2", "land", apply)).toMatchObject({ ok: { path: "a.txt" } });
    expect(readFileSync(join(folder, "a.txt"), "utf8")).toBe("alpha, by the thread\n");
    // What it replaced, kept in the app's data, outside the folder.
    expect(existsSync(join(kept, SAGA, "1"))).toBe(true);
    expect(await lander.op("3", "land", { action: "unapply", saga: SAGA, step: 1, path: "a.txt" })).toHaveProperty("ok");
    expect(readFileSync(join(folder, "a.txt"), "utf8")).toBe("alpha\n");
  });

  it("starts a recovery's host on the folder with what its landings keep, which puts back what was cut short", async () => {
    const kept = join(data, "landings", keyOf(guest));
    mkdirSync(kept, { recursive: true });
    const recovery = host({ tmp: join(data, "tmp", "root.recover"), recovery: { kept: guestPathOf(kept) } });
    await recovery.ready();
    expect(await recovery.op("1", "land", { action: "recover" })).toEqual({ ok: { restored: [], beside: [], lost: [], unread: [] } });
    // A recovery's helper does nothing else.
    expect(await recovery.op("2", "read", { key: `${guest}/a.txt`, max_bytes: null })).toHaveProperty("error");
  });
});
