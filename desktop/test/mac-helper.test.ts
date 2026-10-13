// surogate-helper, the app's own program on macOS: where the app finds it, how it is run, and, on a Mac,
// what each of its commands answers.

import { execFileSync, spawn, spawnSync } from "node:child_process";
import { X509Certificate } from "node:crypto";
import { chmodSync, existsSync, mkdtempSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { certificatesIn } from "../src/shell/company-ca.js";
import { HELPER, helper, helperAt, helperSync } from "../src/mac-helper.js";

describe("where the app finds surogate-helper", () => {
  it("is beside the app's own executable in its bundle, and its build's own in a development build", () => {
    expect(helperAt("/Applications/Surogate.app/Contents/Resources/app/dist")).toBe("/Applications/Surogate.app/Contents/MacOS/surogate-helper");
    expect(helperAt("/Users/ana/Applications/Surogate.app/Contents/Resources/app/dist")).toBe("/Users/ana/Applications/Surogate.app/Contents/MacOS/surogate-helper");
    expect(helperAt("/Users/ana/surogates/desktop/dist")).toBe("/Users/ana/surogates/desktop/native/helper/.build/surogate-helper");
    // Only the app's own code in a bundle's Resources: no other folder of one.
    expect(helperAt("/Applications/Surogate.app/Contents/Resources/other/dist")).toBe("/Applications/Surogate.app/Contents/Resources/other/native/helper/.build/surogate-helper");
    expect(HELPER).toBe(join(import.meta.dirname, "..", "native", "helper", ".build", "surogate-helper"));
  });
});

// A program in its place, started as the app starts the helper (clean-child.ts).
describe("a run of the helper", () => {
  let dir: string;
  let program: string;

  beforeAll(() => {
    dir = mkdtempSync(join(tmpdir(), "helper-"));
    program = join(dir, "surogate-helper");
    // Says its arguments and what it was given of the environment; fails as asked.
    writeFileSync(program, '#!/bin/sh\nif [ "$1" = fail ]; then echo "first" >&2; echo "it would not" >&2; exit 3; fi\necho "$@"\nenv | sort\n');
    chmodSync(program, 0o755);
  });

  afterAll(() => {
    rmSync(dir, { recursive: true, force: true });
  });

  it("answers what it printed, with no environment of the app's but a PATH of the system's", async () => {
    for (const said of [helperSync(["name"], program), await helper(["procs", "--x"], program)]) {
      expect(said).toContain("PATH=/usr/bin:/bin\n");
      expect(said).not.toContain("HOME=");
    }
    expect(helperSync(["name"], program).split("\n")[0]).toBe("name");
    expect((await helper(["procs", "--x"], program)).split("\n")[0]).toBe("procs --x");
  });

  it("fails in its last words", async () => {
    expect(() => helperSync(["fail"], program)).toThrow(`${program} fail failed: it would not`);
    await expect(helper(["fail"], program)).rejects.toThrow(`${program} fail failed: it would not`);
  });
});

// The helper built for this Mac (native/helper/build.sh), run as it is: where the run has built it, as the
// macOS workflow's shell job does.
describe.runIf(process.platform === "darwin" && existsSync(HELPER))("surogate-helper on a Mac", () => {
  const run = (...args: string[]) => spawnSync(HELPER, args, { encoding: "utf8" });

  it("names this Mac as System Settings does", () => {
    expect(run("name")).toMatchObject({ status: 0, stdout: `${execFileSync("/usr/sbin/scutil", ["--get", "ComputerName"], { encoding: "utf8" }).trim()}\n` });
  });

  it("lists this user's processes, each with its program and arguments", async () => {
    const marker = join(tmpdir(), "a profile's folder");
    // A program that takes any argument and waits, as BSD's sleep takes none but its time.
    const waiting = spawn(process.execPath, ["-e", "setInterval(() => {}, 1000)", marker], { stdio: "ignore" });
    try {
      await new Promise((resolve) => waiting.once("spawn", resolve));
      const listed = run("procs").stdout.trim().split("\n").map((line) => JSON.parse(line) as { pid: number; path: string; args: string[] });
      expect(listed.find(({ pid }) => pid === waiting.pid)).toEqual({ pid: waiting.pid, path: realpathSync(process.execPath), args: [process.execPath, "-e", "setInterval(() => {}, 1000)", marker] });
      expect(listed.some(({ pid }) => pid === process.pid)).toBe(true);
    } finally {
      waiting.kill();
    }
  });

  it("prints the admin domain's roots for SSL as PEM certificates, each one a certificate authority's or a root's", () => {
    const done = run("trust");
    expect(done.status).toBe(0);
    for (const pem of certificatesIn(done.stdout)) expect(() => new X509Certificate(pem)).not.toThrow();
    expect(done.stdout.replace(/-----BEGIN CERTIFICATE-----\n[A-Za-z0-9+/=\n]+-----END CERTIFICATE-----\n/g, "")).toBe("");
  });

  it("answers no other command", () => {
    expect(run("apply")).toMatchObject({ status: 2, stdout: "", stderr: "usage: surogate-helper name | trust | procs\n" });
    expect(run("name", "again").status).toBe(2);
  });
});
