// macOS's privacy consent (TCC) for folders shared into the guest: ~/Documents, ~/Desktop, ~/Downloads,
// iCloud Drive, and a removable volume when MEASURE_VOLUME names one (/Volumes/<name>). Apple does not say
// which process its framework's accesses are attributed to, surogate-vm, the framework's own, or the app that
// started them (here, the terminal this runs in), nor whether a consent given to that app covers them; and
// an iCloud file macOS has evicted is read through the share to see whether it downloads. Decides the
// binding's words for a folder the guest cannot read.
//   npx vitest run -c test/vm/darwin/measure/vitest.config.ts tcc
//   (MEASURE_VOLUME=/Volumes/USB npx vitest run -c test/vm/darwin/measure/vitest.config.ts tcc)
// Watch for privacy prompts while it runs, write down the app each names, and allow it. Written down, no
// PASS or FAIL: for each folder, whether the guest listed it and read its file, or what it said instead.

import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

import { expect, it } from "vitest";

import { lab, ready, record, ROOTS } from "./harness.js";

const ICLOUD = join(homedir(), "Library", "Mobile Documents", "com~apple~CloudDocs");

it("shares folders behind macOS's privacy consent into the guest, or says what it met", async () => {
  const places = [join(homedir(), "Documents"), join(homedir(), "Desktop"), join(homedir(), "Downloads"), ICLOUD, process.env.MEASURE_VOLUME]
    .filter((place): place is string => !!place && existsSync(place));
  const booted = await lab("tcc");
  const found: Record<string, unknown> = {};
  for (const [n, place] of places.entries()) {
    const folder = join(place, "SurogateMeasure");
    try {
      rmSync(folder, { recursive: true, force: true });
      mkdirSync(folder, { recursive: true });
      writeFileSync(join(folder, "file.txt"), "readable\n");
    } catch (error) {
      found[place] = `the host could not make its folder: ${(error as Error).message}`;
      continue;
    }
    console.log(`MEASURE tcc: sharing ${folder}: watch for a privacy prompt, and note the app it names`);
    try {
      await ready(booted.guest, ROOTS[n % ROOTS.length]!, realpathSync(folder));
      found[place] = (await booted.run(ROOTS[n % ROOTS.length]!, "ls -la; cat file.txt", 60)).output.trim();
      await booted.guest.teardown(ROOTS[n % ROOTS.length]!);
    } catch (error) {
      found[place] = `not shared: ${(error as Error).message}`;
    }
  }
  if (existsSync(ICLOUD)) {
    const evicted = join(ICLOUD, "SurogateMeasure", "file.txt");
    try {
      execFileSync("/usr/bin/brctl", ["evict", evicted], { encoding: "utf8" });
      await new Promise((resolve) => setTimeout(resolve, 5_000));
      await ready(booted.guest, ROOTS[0]!, realpathSync(join(ICLOUD, "SurogateMeasure")));
      found["an evicted iCloud file, read in the guest"] = (await booted.run(ROOTS[0]!, "cat file.txt", 120)).output.trim();
    } catch (error) {
      found["an evicted iCloud file, read in the guest"] = `not read: ${(error as Error).message}`;
    }
  }
  await booted.stop();
  record("tcc", found);
  expect(Object.keys(found).length).toBeGreaterThan(0);
});
