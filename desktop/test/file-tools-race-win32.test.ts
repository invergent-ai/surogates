// The race on Windows (file-tools-race.test.ts on Linux and macOS): a program of this computer's flips a folder
// in the chat's folder between a folder and a junction to one beside it, while the file tools work through it. On
// Windows there is no sandbox: the file helper the app starts, on its own node, is contained by its walk alone
// (src/files/fd-paths-win32.ts), so the race goes to that helper as the host starts it. A bare control does the same
// with Node's own fs on the folder's paths, which follow a junction, and the race beats it. It needs dist/ (tsc),
// dist/native/ntat.node and the app's rg at bin/rg.exe, and runs on Windows alone, on real NTFS.

import { spawn } from "node:child_process";
import { closeSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, realpathSync, rmSync, unlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import type { Outcome } from "../src/link/protocol.js";
import { built } from "./built.js";

const WINDOWS = process.platform === "win32";
const DESKTOP = fileURLToPath(new URL("..", import.meta.url));
const BIN = join(DESKTOP, "bin");
const ROUNDS = 1_250;

let base: string;
let host: string;
let outside: string;

beforeEach(() => {
  if (!WINDOWS) return;
  base = realpathSync.native(mkdtempSync(join(tmpdir(), "file-tools-race-")));
  host = join(base, "a");
  outside = join(base, "outside");
  mkdirSync(host);
});

afterEach(() => {
  if (WINDOWS) rmSync(base, { recursive: true, force: true });
});

type Answer = (kind: string, args: Record<string, unknown>) => Promise<Outcome>;

// The attacker: flips sub between a folder with a file in it and a junction to the folder beside it, as fast as it
// can, until this process is gone. Each step that fails (the folder held, or in use) is tried again on the next round.
// Its file goes only into a folder it has just made: where sub could not be removed, as while a search holds it, sub may
// still be the junction, and the attacker's own write would go through it.
const FLIPPER = `
const fs = require("node:fs");
const [sub, outside, parent] = process.argv.slice(1);
const step = (call) => { try { call(); } catch {} };
for (;;) {
  try { process.kill(Number(parent), 0); } catch { process.exit(0); }
  step(() => fs.rmSync(sub, { recursive: true, force: true }));
  step(() => { fs.mkdirSync(sub); fs.writeFileSync(sub + "\\\\inside", "inside\\n"); });
  step(() => fs.rmSync(sub, { recursive: true, force: true }));
  step(() => fs.symlinkSync(outside, sub, "junction"));
}`;

// 1 250 rounds of a write, a read, a listing and a delete through the flipped folder, and a search of it every tenth:
// what of the folder beside it each reached (its file gone counts), how many writes were answered, what was written
// beside it, and what its file holds after.
async function race(answer: Answer, folder: string) {
  rmSync(outside, { recursive: true, force: true });
  mkdirSync(outside);
  writeFileSync(join(outside, "only-outside"), "OUTSIDE\n");
  const flipper = spawn(process.execPath, ["-e", FLIPPER, join(host, "sub"), outside, String(process.pid)], { stdio: "ignore" });
  let reached = 0;
  let wrote = 0;
  try {
    for (let i = 0; i < ROUNDS; i += 1) {
      if ("ok" in (await answer("write", { key: `${folder}/sub/x-${i}`, data: Buffer.from("raced\n").toString("base64") }))) wrote += 1;
      const read = await answer("read", { key: `${folder}/sub/only-outside`, max_bytes: null });
      if ("ok" in read && typeof read.ok === "string" && Buffer.from(read.ok, "base64").toString() === "OUTSIDE\n") reached += 1;
      const listed = await answer("list_dir", { key: `${folder}/sub` });
      if ("ok" in listed && Array.isArray(listed.ok) && listed.ok.includes("only-outside")) reached += 1;
      await answer("delete", { key: `${folder}/sub/only-outside` });
      if (i % 10 === 0) {
        const found = await answer("ripgrep", { key: `${folder}/sub`, mode: "count", pattern: "OUTSIDE", glob: null, context: 0 });
        if ("ok" in found && typeof found.ok === "string" && found.ok.includes("only-outside")) reached += 1;
      }
    }
  } finally {
    flipper.kill();
    await new Promise((resolve) => flipper.once("exit", resolve));
  }
  const left = readdirSync(outside).sort();
  const kept = left.includes("only-outside") ? readFileSync(join(outside, "only-outside"), "utf8") : null;
  const made = left.filter((name) => name !== "only-outside");
  console.log(`made beside the folder: ${JSON.stringify(made.slice(0, 20))}`);
  return { reached: reached + (kept === null ? 1 : 0), wrote, wroteOutside: made.length, kept };
}

// The same operations as Node's own fs does them on the folder's paths, as a program that trusts its path would.
const bare: Answer = async (kind, args) => {
  const path = (args.key as string).replace(guest, host).replaceAll("/", "\\");
  try {
    switch (kind) {
      case "write":
        writeFileSync(path, Buffer.from(args.data as string, "base64"));
        return { ok: null };
      case "read":
        return { ok: readFileSync(path).toString("base64") };
      case "list_dir":
        return { ok: readdirSync(path) };
      case "delete":
        unlinkSync(path);
        return { ok: null };
      default: {
        // A search by rg of the path as it is.
        const out = await new Promise<string>((resolve) => {
          const rg = spawn(join(BIN, "rg.exe"), ["--no-ignore", "-c", "-e", args.pattern as string, path], { stdio: ["ignore", "pipe", "ignore"] });
          let said = "";
          rg.stdout.on("data", (chunk: Buffer) => (said += chunk.toString()));
          rg.once("close", () => resolve(said));
        });
        return { ok: out };
      }
    }
  } catch (error) {
    return { error: { type: "os", message: String(error) } };
  }
};
let guest = "";

describe.runIf(WINDOWS)("the file tools on Windows, against a process of this computer's racing them", { timeout: 600_000 }, () => {
  it("reach nothing beside the folder through the helper's walk, where the same operations on Node's own fs do", async () => {
    const { identityFrom, guestPathOf } = await import("../src/files/fd-paths-win32.js");
    const { ntat } = await import("../src/files/ntat.js");
    guest = guestPathOf(host)!;
    const control = await race(bare, guest);
    expect(control.reached + control.wroteOutside).toBeGreaterThan(0);

    // The helper as its host starts it on Windows: the app's node, the helper's own line, no sandbox, and the folder's
    // identity as its host read it.
    const root = ntat().openRoot(`\\\\?\\${host}`);
    const is = identityFrom(ntat().query(root));
    closeSync(root);
    const helper = spawn(process.execPath, ["--disable-sigusr1", built(join(DESKTOP, "dist", "files", "helper.js"))], {
      cwd: base,
      env: { SystemRoot: process.env.SystemRoot ?? "", SUROGATE_FOLDER: guest, HOME: guest, SUROGATE_FOLDER_IS: `${Number(is.dev)}:${Number(is.ino)}`, PATH: BIN },
      stdio: ["pipe", "pipe", "pipe"],
    });
    const waiting = new Map<string, (outcome: Outcome) => void>();
    const lines = createInterface({ input: helper.stdout });
    const ready = new Promise<void>((resolve) => {
      lines.on("line", (line) => {
        if (line === '{"ready":true}') return resolve();
        const { id, outcome } = JSON.parse(line) as { id: string; outcome: Outcome };
        waiting.get(id)?.(outcome);
        waiting.delete(id);
      });
    });
    await ready;
    let next = 0;
    const walked: Answer = (kind, args) => new Promise((resolve) => {
      const id = String((next += 1));
      waiting.set(id, resolve);
      helper.stdin.write(`${JSON.stringify({ id, kind, args })}\n`);
    });
    try {
      const contained = await race(walked, guest);
      console.log(`the race on Windows: ${JSON.stringify({ control, contained })}`);
      expect(contained).toEqual({ reached: 0, wrote: expect.any(Number), wroteOutside: 0, kept: "OUTSIDE\n" });
      // The helper worked in the folder: its writes landed while sub was a folder.
      expect(contained.wrote).toBeGreaterThan(0);
      // The attacker met it: some of its writes found sub a junction, or gone.
      expect(contained.wrote).toBeLessThan(ROUNDS);
    } finally {
      helper.kill();
      await new Promise((resolve) => helper.once("exit", resolve));
    }
  });
});
