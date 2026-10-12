// The file host on a Mac's own places, by hand (test/acceptance/macos/file-host.sh runs it): for each folder in
// SUROGATE_FILE_HOST_PLACES, colon-separated (one in ~/Documents, on a removable volume, in iCloud Drive), a folder
// of the test's own is made in it, a host is started on it in srt's Seatbelt, on the app's node, and the file kinds,
// the folder's lock and the race of a process of this computer's are tried there. Skipped unless that is set.

import { spawn } from "node:child_process";
import { mkdirSync, readdirSync, readFileSync, realpathSync, rmSync, statSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";

import { describe, expect, it } from "vitest";

import type { HostStart } from "../src/hosts/messages.js";
import { bound, Harness, PACKAGE } from "./host-harness.js";

const PLACES = (process.env.SUROGATE_FILE_HOST_PLACES ?? "").split(":").filter(Boolean);
const NODE = join(PACKAGE, "bin", "node");
const ROUNDS = 250;

describe.runIf(process.platform === "darwin" && PLACES.length > 0)("the file host on a Mac's own places", { timeout: 300_000 }, () => {
  it.each(PLACES)("serves the file kinds in %s, holds its lock, and reaches nothing beside its folder while a process flips a link in it", async (place) => {
    const base = join(realpathSync(place), `surogate-file-host-probe-${process.pid}`);
    const folder = join(base, "folder");
    const outside = join(base, "outside");
    const data = join(realpathSync(PACKAGE), "dist", "probe-data", String(process.pid));
    mkdirSync(folder, { recursive: true });
    mkdirSync(outside);
    writeFileSync(join(folder, "a.txt"), "alpha\n");
    writeFileSync(join(outside, "only-outside"), "OUTSIDE\n");
    const harness = new Harness(undefined, undefined, NODE);
    const flipper = spawn("sh", ["-c", `cd '${folder}' && while kill -0 ${process.pid} 2>/dev/null; do rm -rf sub; mkdir sub; rm -rf sub; ln -s '${outside}' sub; done`], {
      stdio: "ignore", detached: true,
    });
    try {
      const start: HostStart = {
        type: "start", folder, expect: bound(folder), tmp: join(data, "tmp"), dataDir: data, cacheDir: join(data, "cache"),
        env: { HOME: process.env.HOME ?? "/Users/tester", LANG: "C.UTF-8", PATH: "/usr/bin:/bin" }, appDirs: [dirname(process.execPath), PACKAGE],
      };
      harness.send(start);
      const said = await harness.until((messages) => messages.find((message) => message.type === "ready" || message.type === "failed"), 60_000);
      expect({ said, stderr: harness.stderr }).toMatchObject({ said: { type: "ready" } });
      expect(await harness.op("read", "read", { key: join(folder, "a.txt"), max_bytes: null })).toEqual({ ok: Buffer.from("alpha\n").toString("base64") });
      expect(await harness.op("write", "write", { key: join(folder, "b.txt"), data: Buffer.from("beta\n").toString("base64") })).toEqual({ ok: null });
      expect(await harness.op("search", "ripgrep", { key: folder, mode: "count", pattern: "alpha", glob: null, context: 0 })).toMatchObject({ ok: expect.stringContaining("a.txt") });
      const { dev, ino } = statSync(folder);
      expect(statSync(join(data, "locks", `${process.getuid?.()}-${dev}-${ino}`)).isFile()).toBe(true);
      let reached = 0;
      let wrote = 0;
      for (let i = 0; i < ROUNDS; i += 1) {
        if ("ok" in (await harness.op(`w${i}`, "write", { key: join(folder, "sub", `x-${i}`), data: Buffer.from("m8\n").toString("base64") }) as object)) wrote += 1;
        const read = await harness.op(`r${i}`, "read", { key: join(folder, "sub", "only-outside"), max_bytes: null }) as { ok?: string };
        if (read.ok !== undefined && Buffer.from(read.ok, "base64").toString() === "OUTSIDE\n") reached += 1;
        const listed = await harness.op(`l${i}`, "list_dir", { key: join(folder, "sub") }) as { ok?: string[] };
        if (listed.ok?.includes("only-outside")) reached += 1;
        await harness.op(`d${i}`, "delete", { key: join(folder, "sub", "only-outside") });
      }
      const left = readdirSync(outside).sort();
      console.log(`${place}: ${JSON.stringify({ reached, wrote, left })}`);
      expect({ reached, left, kept: readFileSync(join(outside, "only-outside"), "utf8") }).toEqual({ reached: 0, left: ["only-outside"], kept: "OUTSIDE\n" });
    } finally {
      process.kill(-flipper.pid!, "SIGKILL");
      await harness.stop();
      rmSync(base, { recursive: true, force: true });
      rmSync(data, { recursive: true, force: true });
    }
  });
});
