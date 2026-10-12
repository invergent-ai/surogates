// Spike Q7's race (M8), on every OS the file host is built for: a program of this computer's, outside every sandbox,
// flips a folder in the chat's folder between a folder and a link to one beside it, while the file tools work through
// it, in the file host the app starts (on its own node, bin/node, the helper in srt: bubblewrap on Linux, Seatbelt on
// macOS), and in a bare control, the helper's own checks alone, outside srt, which the race beats.

import { spawn } from "node:child_process";
import { mkdirSync, mkdtempSync, readdirSync, readFileSync, realpathSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { BOOT_ID } from "../src/binding/folder.js";
import { perform } from "../src/files/operations.js";
import { APP_DIRS, forkHost, type HostProcess, ToolHosts } from "../src/hosts/tool-hosts.js";
import type { Operation, Outcome } from "../src/link/protocol.js";

const ROOT_A = "11111111-1111-4111-8111-111111111111";

let base: string;
let folders: Record<string, string>;
let identities: Map<string, { dev: number; ino: number; boot: string }>;
let spawned: HostProcess[];
let exits: number;
let hosts: ToolHosts | null;

const op = (kind: string, args: Record<string, unknown>, sessionId = ROOT_A): Operation => ({
  id: `${kind}-${Math.random()}`, sessionId, callingSessionId: sessionId, invocationId: "call", ordinal: 1, kind, args, digest: "d",
});
const signal = () => new AbortController().signal;

async function until(check: () => boolean, timeoutMs = 10_000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (!check()) {
    if (Date.now() > deadline) throw new Error("timed out");
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
}

function toolHosts(): ToolHosts {
  hosts = new ToolHosts({
    bindingOf: (root) => {
      const folder = folders[root];
      const made = folder === undefined ? undefined : identities.get(folder);
      return folder && made ? { folder, ...made } : undefined;
    },
    dataDir: join(base, "data"),
    cacheDir: join(base, "cache", "surogate"),
    env: { HOME: process.env.HOME ?? "/home/tester", LANG: "C.UTF-8" },
    appDirs: APP_DIRS,
    spawnHost: () => {
      const host = forkHost();
      host.onExit(() => {
        exits += 1;
      });
      spawned.push(host);
      return host;
    },
  });
  return hosts;
}

beforeEach(() => {
  base = realpathSync(mkdtempSync(join(tmpdir(), "file-tools-race-")));
  folders = { [ROOT_A]: join(base, "a") };
  identities = new Map();
  for (const folder of Object.values(folders)) {
    mkdirSync(folder);
    const { dev, ino } = statSync(folder);
    identities.set(folder, { dev, ino, boot: BOOT_ID });
  }
  spawned = [];
  exits = 0;
  hosts = null;
});

afterEach(async () => {
  await hosts?.stop();
  await until(() => exits >= spawned.length, 3_000).catch(() => {});
  for (const host of spawned) host.kill();
  rmSync(base, { recursive: true, force: true });
});

describe("the file tools, against a process of this computer's racing them", { timeout: 120_000 }, () => {
  type Answer = (kind: string, args: Record<string, unknown>) => Promise<Outcome>;

  // 1 250 rounds of a write, a read, a listing and a delete through the flipped folder, and a search
  // of it every tenth: what of the folder beside it each reached (its file gone counts), how many
  // writes were answered, what was written beside it, and what its file holds after.
  async function race(answer: Answer, folder: string, outside: string) {
    rmSync(outside, { recursive: true, force: true });
    mkdirSync(outside);
    writeFileSync(join(outside, "only-outside"), "OUTSIDE\n");
    // It ends with this process: a run killed partway leaves no attacker spinning.
    const flipper = spawn("sh", ["-c", `cd '${folder}' && while kill -0 ${process.pid} 2>/dev/null; do rm -rf sub; mkdir sub; echo inside > sub/inside; rm -rf sub; ln -s '${outside}' sub; done`], {
      stdio: "ignore", detached: true,
    });
    let reached = 0;
    let wrote = 0;
    try {
      for (let i = 0; i < 1_250; i += 1) {
        if ("ok" in (await answer("write", { key: join(folder, "sub", `x-${i}`), data: Buffer.from("m8\n").toString("base64") }))) wrote += 1;
        const read = await answer("read", { key: join(folder, "sub", "only-outside"), max_bytes: null });
        if ("ok" in read && typeof read.ok === "string" && Buffer.from(read.ok, "base64").toString() === "OUTSIDE\n") reached += 1;
        const listed = await answer("list_dir", { key: join(folder, "sub") });
        if ("ok" in listed && Array.isArray(listed.ok) && listed.ok.includes("only-outside")) reached += 1;
        await answer("delete", { key: join(folder, "sub", "only-outside") });
        if (i % 10 === 0) {
          const found = await answer("ripgrep", { key: join(folder, "sub"), mode: "count", pattern: "OUTSIDE", glob: null, context: 0 });
          if ("ok" in found && typeof found.ok === "string" && found.ok.includes("only-outside")) reached += 1;
        }
      }
    } finally {
      process.kill(-flipper.pid!, "SIGKILL");
    }
    const left = readdirSync(outside).sort();
    const kept = left.includes("only-outside") ? readFileSync(join(outside, "only-outside"), "utf8") : null;
    return { reached: reached + (kept === null ? 1 : 0), wrote, wroteOutside: left.filter((name) => name !== "only-outside").length, kept };
  }

  it("reach nothing beside the folder through its file host on the app's own node, where the same operations outside any sandbox do", async () => {
    const folder = folders[ROOT_A]!;
    const outside = join(base, "outside");
    // The probe can race: the helper's own checks alone, outside srt, are beaten on Linux. On macOS the helper's opens
    // refuse a link anywhere on their way (O_NOFOLLOW_ANY), so only its listings and deletions, which follow one, can be
    // beaten, and this flipper rarely beats them: there the control's count is written out, not held to.
    const bare = await race((kind, args) => perform(kind, args, { folder, home: base, env: { PATH: "/usr/bin:/bin" } }, signal()), folder, outside);
    if (process.platform === "linux") expect(bare.reached + bare.wroteOutside).toBeGreaterThan(0);
    const executor = toolHosts();
    const sandboxed = await race((kind, args) => executor.run(op(kind, args), signal()), folder, outside);
    console.log(`M8: ${JSON.stringify({ bare, sandboxed })}`);
    // The file host worked in the folder: its writes landed while sub was a folder.
    expect(sandboxed).toEqual({ reached: 0, wrote: expect.any(Number), wroteOutside: 0, kept: "OUTSIDE\n" });
    expect(sandboxed.wrote).toBeGreaterThan(0);
    // The attacker met the file host: some of its writes found sub a link, or gone.
    expect(sandboxed.wrote).toBeLessThan(1_250);
  });
});

