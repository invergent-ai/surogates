import { spawn } from "node:child_process";
import { chmodSync, mkdirSync, mkdtempSync, realpathSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { SessionRunner } from "../src/guest/runner-process.js";

const RUNNER = fileURLToPath(new URL("../dist/guest/runner.js", import.meta.url));

let base: string;
let runners: SessionRunner[];

// The root runner as the agent starts it, without its namespaces: the protocol is the same.
async function runner(PATH = "/usr/bin:/bin"): Promise<SessionRunner> {
  const child = spawn(process.execPath, [RUNNER], { cwd: base, env: { PATH, HOME: base }, stdio: ["pipe", "pipe", "pipe"] });
  const started = new SessionRunner(child);
  runners.push(started);
  await started.ready;
  return started;
}

beforeEach(() => {
  base = realpathSync(mkdtempSync(join(tmpdir(), "guest-runner-")));
  mkdirSync(join(base, "sub"));
  runners = [];
});

afterEach(async () => {
  for (const started of runners) await started.stop();
  rmSync(base, { recursive: true, force: true });
});

describe("the root runner's answers about its own view", { timeout: 20_000 }, () => {
  it("places a command's workdir as run does: an alias is the folder, the rest inside it", async () => {
    const started = await runner();
    expect(await started.ask({ type: "place", id: "p1", folder: base, home: base, workdir: null }))
      .toEqual({ type: "placed", id: "p1", cwd: base, unenterable: null });
    // An alias is the folder, whatever the home.
    expect(await started.ask({ type: "place", id: "p2", folder: base, home: join(base, "sub"), workdir: "~" }))
      .toEqual({ type: "placed", id: "p2", cwd: base, unenterable: null });
    symlinkSync("/etc", join(base, "out"));
    expect(await started.ask({ type: "place", id: "p3", folder: base, home: base, workdir: "out" })).toMatchObject({
      type: "refused", id: "p3", refusal: { type: "sandbox", message: expect.stringMatching(/^Blocked: .*All commands must run within the workspace directory\.$/) },
    });
    writeFileSync(join(base, "file"), "");
    expect(await started.ask({ type: "place", id: "p4", folder: base, home: base, workdir: "file" }))
      .toEqual({ type: "placed", id: "p4", cwd: join(base, "file"), unenterable: "ENOTDIR" });
  });

  it("answers which from the commands' PATH, as shutil.which does", async () => {
    const bin = join(base, "bin");
    mkdirSync(bin);
    writeFileSync(join(bin, "tool"), "#!/bin/sh\n");
    chmodSync(join(bin, "tool"), 0o755);
    writeFileSync(join(bin, "plain"), "");
    const started = await runner(`${bin}:/usr/bin:/bin`);
    const found = (name: string, id: string) => started.ask({ type: "which", id, name, cwd: base });
    expect(await found("tool", "w1")).toEqual({ type: "found", id: "w1", found: true });
    expect(await found("sh", "w2")).toEqual({ type: "found", id: "w2", found: true });
    expect(await found("plain", "w3")).toEqual({ type: "found", id: "w3", found: false });
    expect(await found("no-such-tool", "w4")).toEqual({ type: "found", id: "w4", found: false });
    expect(await found("bin/tool", "w5")).toEqual({ type: "found", id: "w5", found: true });
  });

  it.skipIf(process.getuid?.() === 0)("says a folder the root's user cannot enter cannot be entered", async () => {
    const started = await runner();
    mkdirSync(join(base, "locked"));
    chmodSync(join(base, "locked"), 0o000);
    expect(await started.ask({ type: "place", id: "p5", folder: base, home: base, workdir: "locked" }))
      .toEqual({ type: "placed", id: "p5", cwd: join(base, "locked"), unenterable: "EACCES" });
    chmodSync(join(base, "locked"), 0o755);
  });

  it("answers a which it cannot take, and keeps answering", async () => {
    const started = await runner();
    expect(await started.ask({ type: "which", id: "w7", name: 7 as unknown as string, cwd: base }))
      .toMatchObject({ type: "refused", id: "w7", refusal: { type: "other" } });
    expect(await started.ask({ type: "which", id: "w8", name: "sh", cwd: base })).toEqual({ type: "found", id: "w8", found: true });
  });

  it("refuses a question whose id is already waiting, and answers null once the runner has gone, or goes first", async () => {
    // Ready, then gone at the first question it reads.
    const child = spawn(process.execPath, ["-e", `process.stdout.write('{"ready":true}\\n'); process.stdin.once("data", () => process.exit(0));`], {
      stdio: ["pipe", "pipe", "pipe"],
    });
    const leaving = new SessionRunner(child);
    runners.push(leaving);
    await leaving.ready;
    const asked = leaving.ask({ type: "which", id: "w9", name: "sh", cwd: base });
    expect(await leaving.ask({ type: "which", id: "w9", name: "sh", cwd: base })).toMatchObject({ type: "refused", id: "w9" });
    expect(await asked).toBeNull();
    const started = await runner();
    await started.stop();
    expect(await started.ask({ type: "which", id: "w6", name: "sh", cwd: base })).toBeNull();
  });
});
