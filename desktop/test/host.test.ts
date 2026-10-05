import { execFileSync } from "node:child_process";
import { randomBytes } from "node:crypto";
import {
  chmodSync, existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, realpathSync, renameSync, rmSync, statSync,
  symlinkSync, writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { BOOT_ID } from "../src/binding/folder.js";
import { MAX_WRITE_BYTES } from "../src/files/answers.js";
import { findOnPath } from "../src/files/operations.js";
import { FOLDER_UNAVAILABLE, type HostStart } from "../src/hosts/messages.js";
import { bound, Harness, PACKAGE } from "./host-harness.js";

let base: string;
let folder: string;
let start: HostStart;
let harnesses: Harness[];

// Bound to its folder as that folder is now, unless the test says otherwise.
function host(overrides: Partial<HostStart> = {}, cwd?: string): Harness {
  const harness = new Harness(cwd);
  harnesses.push(harness);
  harness.send({ ...start, expect: bound(overrides.folder ?? start.folder), ...overrides });
  return harness;
}

const ready = (harness: Harness) => harness.until((messages) => messages.find((message) => message.type === "ready"));
// Why a host would not start: its failure message, or what it said instead.
async function refusal(harness: Harness): Promise<string> {
  const said = await harness.until((messages) =>
    messages.find((message) => message.type === "ready" || message.type === "failed"));
  return said.type === "failed" ? said.message : `it answered ${said.type}`;
}
const REFUSED = /home folder or the app's own data/;

beforeEach(() => {
  base = realpathSync(mkdtempSync(join(tmpdir(), "host-")));
  folder = join(base, "folder");
  mkdirSync(folder);
  writeFileSync(join(folder, "a.txt"), "alpha\n");
  start = {
    type: "start",
    folder,
    expect: bound(folder),
    domains: [],
    tmp: join(base, "data", "tmp", "root"),
    dataDir: join(base, "data"),
    env: { HOME: process.env.HOME ?? "/home/tester", LANG: "C.UTF-8", PATH: "/usr/bin:/bin" },
    appDirs: [dirname(process.execPath), PACKAGE],
  };
  harnesses = [];
});

afterEach(async () => {
  for (const harness of harnesses) await harness.stop();
  rmSync(base, { recursive: true, force: true });
});

describe("a tool host", { timeout: 30_000 }, () => {
  it("answers operations from inside the folder's sandbox, and stops cleanly", async () => {
    const harness = host();
    await ready(harness);
    expect(await harness.op("1", "resolve", { path: "a.txt" })).toEqual({ ok: `${folder}/a.txt` });
    expect(await harness.op("2", "read", { key: `${folder}/a.txt`, max_bytes: null })).toEqual({
      ok: Buffer.from("alpha\n").toString("base64"),
    });
    harness.send({ type: "stop" });
    expect(await harness.exited).toBe(0);
  });

  it("writes 50 MiB, the most a write takes, through its file helper", async () => {
    const harness = host();
    await ready(harness);
    const data = randomBytes(MAX_WRITE_BYTES);
    expect(await harness.op("1", "write", { key: `${folder}/most.bin`, data: data.toString("base64") })).toEqual({
      ok: null,
    });
    expect(readFileSync(join(folder, "most.bin")).equals(data)).toBe(true);
  });

  it("cannot see what the sandbox hides", async () => {
    const hidden = join(base, "hidden-bin");
    mkdirSync(hidden);
    writeFileSync(join(hidden, "only-outside"), "#!/bin/sh\n", { mode: 0o755 });
    mkdirSync(join(folder, "bin"));
    writeFileSync(join(folder, "bin", "only-inside"), "#!/bin/sh\n", { mode: 0o755 });
    const PATH = `${hidden}:${join(folder, "bin")}:/usr/bin:/bin`;
    expect(findOnPath("only-outside", PATH, folder)).toBe(join(hidden, "only-outside"));
    const harness = host({ env: { ...start.env, PATH } });
    await ready(harness);
    expect(await harness.op("1", "which", { name: "only-outside" })).toEqual({ ok: false });
    // Found only through the app's PATH, so the helper has it.
    expect(await harness.op("2", "which", { name: "only-inside" })).toEqual({ ok: true });
    expect(await harness.op("3", "which", { name: "sh" })).toEqual({ ok: true });
  });

  it("leaves the user's folder as it was", async () => {
    const before = readdirSync(folder).sort();
    // Started in the folder: without its own chdir, srt would mount its
    // placeholders here.
    const harness = host({}, folder);
    await ready(harness);
    await harness.op("1", "stat", { key: `${folder}/a.txt` });
    expect(readdirSync(folder).sort()).toEqual(before);
  });

  it("answers folder_unavailable once the folder is moved", async () => {
    const harness = host();
    await ready(harness);
    renameSync(folder, `${folder}-moved`);
    expect(await harness.op("1", "stat", { key: `${folder}/a.txt` })).toEqual(FOLDER_UNAVAILABLE);
  });

  it("fails to start without its sandbox, and says why", async () => {
    const harness = host({ bwrapPath: "/nonexistent/bwrap" });
    const failed = await harness.until((messages) => messages.find((message) => message.type === "failed"));
    expect(failed.type === "failed" && failed.message).toMatch(/bwrap/);
    expect(failed).not.toHaveProperty("folder");
    expect(await harness.exited).toBe(1);
  });

  it.each([
    ["a regular file", (b: string) => join(b, "folder", "a.txt")],
    ["a path that is not there", (b: string) => join(b, "gone")],
    ["a path under a regular file", (b: string) => join(b, "folder", "a.txt", "sub")],
  ])("answers a folder that is %s as unavailable, and goes", async (_name, chosen) => {
    const harness = host({ folder: chosen(base) });
    const failed = await harness.until((messages) => messages.find((message) => message.type === "failed"));
    expect(failed).toMatchObject({ type: "failed", folder: true });
    expect(await harness.exited).toBe(1);
  });

  it("answers a folder replaced since its chat was bound as unavailable, and goes", async () => {
    const expected = bound(folder);
    renameSync(folder, `${folder}-old`);
    mkdirSync(folder);
    const harness = host({ expect: expected });
    const failed = await harness.until((messages) => messages.find((message) => message.type === "failed"));
    expect(failed).toMatchObject({ type: "failed", folder: true, message: expect.stringMatching(/replaced/) });
    expect(await harness.exited).toBe(1);
    expect(existsSync(join(base, "data", "folders"))).toBe(false);
  });

  it("starts in the folder its chat was bound to", async () => {
    const harness = host({ expect: bound(folder) });
    await ready(harness);
    expect(await harness.op("1", "resolve", { path: "a.txt" })).toEqual({ ok: `${folder}/a.txt` });
  });

  // An unreadable boot id compares as this boot.
  it.each([BOOT_ID, ""])("refuses a folder on another device in the boot it was bound in (%j)", async (boot) => {
    const { dev, ino } = statSync(folder);
    expect(await refusal(host({ expect: { dev: dev + 1, ino, boot } }))).toMatch(/replaced/);
  });

  // st_dev belongs to a mount, which a reboot can number anew.
  it("starts in its folder after a reboot that changed the folder's device number", async () => {
    const { dev, ino } = statSync(folder);
    await ready(host({ expect: { dev: dev + 1, ino, boot: "another-boot" } }));
  });

  it("refuses a folder with another inode after a reboot", async () => {
    const { dev, ino } = statSync(folder);
    expect(await refusal(host({ expect: { dev, ino: ino + 1, boot: "another-boot" } }))).toMatch(/replaced/);
  });

  it("refuses a folder that holds the app's own data or files, or the whole system", async () => {
    for (const refused of [base, "/", PACKAGE]) {
      const harness = host({ folder: refused });
      const failed = await harness.until((messages) => messages.find((message) => message.type === "failed"));
      expect(failed.type === "failed" && failed.message).toMatch(/home folder or the app's own data/);
    }
  });

  it("refuses the home folder and any folder that holds or sits in a credential folder", async () => {
    const home = join(base, "home");
    mkdirSync(join(home, ".config", "gh"), { recursive: true });
    for (const refused of [home, join(home, ".config"), join(home, ".config", "gh")]) {
      const harness = host({ folder: refused, env: { ...start.env, HOME: home } });
      const failed = await harness.until((messages) => messages.find((message) => message.type === "failed"));
      expect(failed.type === "failed" && failed.message).toMatch(/home folder or the app's own data/);
    }
  });

  it("refuses a folder inside an app folder that is spelled with a trailing slash", async () => {
    expect(PACKAGE.endsWith("/")).toBe(true);
    expect(await refusal(host({ folder: join(PACKAGE, "dist") }))).toMatch(REFUSED);
  });

  it("refuses the sibling that a credential folder is a link to", async () => {
    const home = join(base, "home");
    mkdirSync(join(home, "dotfiles"), { recursive: true });
    symlinkSync(join(home, "dotfiles"), join(home, ".ssh"));
    expect(await refusal(host({ folder: join(home, "dotfiles"), env: { ...start.env, HOME: home } }))).toMatch(REFUSED);
  });

  it("refuses the app's data folder when it is reached through a link", async () => {
    mkdirSync(join(base, "real-data", "sub"), { recursive: true });
    symlinkSync(join(base, "real-data"), join(base, "data-link"));
    expect(await refusal(host({ folder: join(base, "real-data", "sub"), dataDir: join(base, "data-link") }))).toMatch(REFUSED);
  });

  it("refuses the home folder when HOME is a link to it", async () => {
    mkdirSync(join(base, "home-real"));
    symlinkSync(join(base, "home-real"), join(base, "home-link"));
    const env = { ...start.env, HOME: join(base, "home-link") };
    expect(await refusal(host({ folder: join(base, "home-real"), env }))).toMatch(REFUSED);
  });

  // A guard that is not there yet is still where its links lead: a later `gh auth login` would write there.
  it.each(["dotfiles/config", "dotfiles"])(
    "refuses %s, which a credential folder that does not exist yet is a link into",
    async (chosen) => {
      const home = join(base, "home");
      mkdirSync(join(home, "dotfiles", "config"), { recursive: true });
      symlinkSync(join(home, "dotfiles", "config"), join(home, ".config"));
      expect(await refusal(host({ folder: join(home, chosen), env: { ...start.env, HOME: home } }))).toMatch(REFUSED);
    },
  );

  it("refuses the folder that holds a data folder, when the data folder is not there yet and its parent is a link", async () => {
    mkdirSync(join(base, "realB"));
    symlinkSync(join(base, "realB"), join(base, "linkB"));
    expect(await refusal(host({ folder: join(base, "realB"), dataDir: join(base, "linkB", "appdata") }))).toMatch(REFUSED);
  });

  it("refuses a folder that holds the place a credential folder is linked to, when that place cannot be read", async () => {
    const home = join(base, "home");
    const locked = join(base, "x", "locked");
    mkdirSync(join(locked, "ssh"), { recursive: true });
    mkdirSync(home);
    symlinkSync(join(locked, "ssh"), join(home, ".ssh"));
    chmodSync(locked, 0o000);
    try {
      expect(await refusal(host({ folder: join(base, "x"), env: { ...start.env, HOME: home } }))).toMatch(REFUSED);
    } finally {
      chmodSync(locked, 0o700);
    }
  });

  const globbed: [string, (base: string, start: HostStart) => Partial<HostStart>][] = [
    ["a folder", (b) => ({ folder: join(b, "x[ab]") })],
    ["a temp folder", (b) => ({ tmp: join(b, "data", "t?mp", "root") })],
    ["an app folder", (b, s) => ({ appDirs: [...s.appDirs, join(b, "a*")] })],
  ];
  it.each(globbed)("refuses %s whose path srt would read as a glob", async (_name, overrides) => {
    mkdirSync(join(base, "x[ab]"));
    expect(await refusal(host(overrides(base, start)))).toMatch(
      /cannot sandbox a folder whose path holds \*, \?, \[ or \]/,
    );
  });

  it.each(["/proc", "/sys", "/dev", "/dev/shm", "/run"])("refuses %s, a system folder", async (system) => {
    expect(await refusal(host({ folder: system }))).toMatch(/system folders/);
  });

  it("wants an absolute temp folder", async () => {
    expect(await refusal(host({ tmp: "relative/tmp" }, base))).toMatch(/absolute/);
  });

  it("ignores a second start once its folder is set", async () => {
    const harness = host();
    await ready(harness);
    harness.send({ ...start, folder: "/" });
    expect(await harness.op("1", "resolve", { path: "a.txt" })).toEqual({ ok: `${folder}/a.txt` });
    expect(harness.messages.some((message) => message.type === "failed")).toBe(false);
  });

  it("goes quietly when its helper is gone before an operation reaches it", async () => {
    const harness = host();
    await ready(harness);
    const pid = harness.child.pid ?? 0;
    // Stopped, so that the operation is in its queue when it learns the helper died.
    process.kill(pid, "SIGSTOP");
    execFileSync("pkill", ["-KILL", "-P", String(pid)]);
    await new Promise((resolve) => setTimeout(resolve, 300));
    harness.send({ type: "op", id: "1", kind: "stat", args: { key: `${folder}/a.txt` } });
    process.kill(pid, "SIGCONT");
    expect(await harness.exited).toBe(1);
    expect(harness.stderr).toBe("");
  });

  it("goes when its helper dies, so the app answers what ran as interrupted", async () => {
    const harness = host();
    await ready(harness);
    execFileSync("pkill", ["-KILL", "-P", String(harness.child.pid)]);
    expect(await harness.exited).toBe(1);
  });
});
