import { execFileSync, spawn, spawnSync } from "node:child_process";
import { createHash, randomBytes } from "node:crypto";
import {
  chmodSync, existsSync, lstatSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, readlinkSync, realpathSync, renameSync, rmSync, statSync,
  symlinkSync, writeFileSync,
} from "node:fs";
import { connect, createServer } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { BOOT_ID } from "../src/binding/folder.js";
import { MAX_WRITE_BYTES } from "../src/files/answers.js";
import { kinds } from "../src/files/operations.js";
import { FINISHED_TTL_SECONDS } from "../src/guest/processes.js";
import { keyOf } from "../src/history/place.js";
import { LOCK_WAIT_MS, lockFolder, readRecord, writeRecord } from "../src/hosts/folder-record.js";
import { HOOKS_NOTICE } from "../src/hosts/hooks.js";
import { FOLDER_UNAVAILABLE, type HostStart } from "../src/hosts/messages.js";
import { READY_MS } from "../src/hosts/start.js";
import { bound, Harness, PACKAGE } from "./host-harness.js";

let base: string;
let folder: string;
let start: HostStart;
let harnesses: Harness[];

// Bound to its folder as that folder is now, unless the test says otherwise.
// *env* is the host's own environment; omitted, it is this process's. *execPath* runs it.
function host(overrides: Partial<HostStart> = {}, cwd?: string, env?: NodeJS.ProcessEnv, execPath?: string): Harness {
  const harness = new Harness(cwd, env, execPath);
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
// The answer to the request with *id*.
const result = (harness: Harness, id: string) => harness.until((messages) => {
  const found = messages.find((message) => message.type === "result" && message.id === id);
  return found?.type === "result" ? found.outcome : undefined;
});
// The folder srt keeps its sockets in for this test's folder, the processes whose command line
// names it (its socat bridges), and the folder's record.
const srtTmp = () => {
  const { dev, ino } = statSync(folder);
  return join(start.dataDir, "srt", `${dev}-${ino}`);
};
const sockets = () => readdirSync(srtTmp()).filter((name) => name.endsWith(".sock"));
const bridges = () => spawnSync("pgrep", ["-f", `${srtTmp()}/`], { encoding: "utf8" }).stdout.split("\n").filter(Boolean).map(Number);
const alive = (pid: number) => {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
};
const recordOf = () => {
  const { dev, ino } = statSync(folder);
  return join(start.dataDir, "folders", `${dev}-${ino}.json`);
};
async function until(check: () => boolean, timeoutMs = 10_000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (!check()) {
    if (Date.now() > deadline) throw new Error("timed out");
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
}

beforeEach(() => {
  base = realpathSync(mkdtempSync(join(tmpdir(), "host-")));
  folder = join(base, "folder");
  mkdirSync(folder);
  writeFileSync(join(folder, "a.txt"), "alpha\n");
  start = {
    type: "start",
    folder,
    expect: bound(folder),
    tmp: join(base, "data", "tmp", "root"),
    dataDir: join(base, "data"),
    cacheDir: join(base, "cache", "surogate"),
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

  it("makes a file only where nothing is for a write told to create, through its file helper in the folder's sandbox, and writes through no link made at a name", async () => {
    const harness = host();
    await ready(harness);
    const b64 = (text: string) => Buffer.from(text).toString("base64");
    const key = `${folder}/Downloads/report.txt`;
    // The first makes the folder too.
    expect(await harness.op("1", "write", { key, data: b64("first"), create: true })).toEqual({ ok: null });
    expect(await harness.op("2", "write", { key, data: b64("second"), create: true })).toEqual({
      error: { type: "os", code: "EEXIST", message: `File exists: '${key}'` },
    });
    // A link made at a name meanwhile, to a file outside the folder that is not there yet.
    symlinkSync(join(base, "outside.txt"), join(folder, "Downloads", "notes.txt"));
    expect(await harness.op("3", "write", { key: `${folder}/Downloads/notes.txt`, data: b64("third"), create: true })).toMatchObject({ error: { type: "sandbox" } });
    expect([readdirSync(join(folder, "Downloads")).sort(), readFileSync(key, "utf8"), existsSync(join(base, "outside.txt"))]).toEqual([
      ["notes.txt", "report.txt"], "first", false,
    ]);
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
    // In Section 9's words, before srt is asked.
    expect(failed).toMatchObject({ type: "failed", message: "Surogate's sandbox tools are missing. Run the install script again. It lacks bubblewrap" });
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

  it("refuses the app's own cache folder, a folder in it, and a folder that holds it", async () => {
    const cache = join(base, "cache", "surogate");
    mkdirSync(join(cache, "updates"), { recursive: true });
    for (const refused of [cache, join(cache, "updates"), join(base, "cache")]) {
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

  it("runs no command: the kinds that would are its helper's to refuse, as kinds it does not do", async () => {
    const harness = host();
    await ready(harness);
    // In the folder, which the helper's sandbox can write: a command run there would leave it.
    const proof = join(folder, "ran");
    const touch = `touch '${proof}'`;
    for (const [kind, args] of [
      ["run", { command: touch, workdir: null, timeout: 10 }],
      ["start", { command: touch, workdir: null, task_id: "t", pty: false, notify_on_complete: false, watcher_interval: null }],
      ["write_stdin", { session_id: "proc_000000000001", data: `${touch}\n` }],
      ["which", { name: "sh" }],
      ["poll", { session_id: "proc_000000000001" }],
      ["list_processes", { task_id: "t" }],
    ] as const) {
      expect(await harness.op(kind, kind, args)).toEqual({ error: { type: "unsupported", message: `This computer cannot do '${kind}' yet` } });
    }
    expect(existsSync(proof)).toBe(false);
  });

  it("drops a handle older than the cloud keeps one from the folder's record", async () => {
    const old = {
      id: "proc_000000000001", command: "echo old", cwd: folder, task_id: "t", started_at: Date.now() / 1000 - FINISHED_TTL_SECONDS - 60,
      ended: { exit_code: 0, output: "old\n", note: null },
    };
    writeRecord(recordOf(), { state: "stopped", hooks: null, processes: [old] });
    const harness = host();
    expect(await ready(harness)).toEqual({ type: "ready", processes: [] });
    await harness.stop();
    expect(await harness.exited).toBe(0);
    expect(readRecord(recordOf())?.processes).toEqual([]);
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

// A guest run is in flight from its refusal to its after, and the look leaves paused rebases' todos alone meanwhile.
describe("a tool host's runs in the guest", { timeout: 30_000 }, () => {
  const todo = () => join(folder, ".git", "rebase-merge", "git-rebase-todo");
  const PLANTED = "exec touch pwned\n";
  const COMMENTED = "# Surogate removed a step that appeared while the chat's commands could write: exec touch pwned\n";
  const answer = (harness: Harness, id: string) => harness.until((messages) => {
    const result = messages.find((message) => message.type === "result" && message.id === id);
    return result?.type === "result" ? result.outcome : undefined;
  });
  const record = () => {
    const { dev, ino } = statSync(folder);
    return JSON.parse(readFileSync(join(start.dataDir, "folders", `${dev}-${ino}.json`), "utf8")) as { state: string };
  };
  beforeEach(() => {
    mkdirSync(join(folder, ".git", "rebase-merge"), { recursive: true });
    writeFileSync(join(folder, ".git", "HEAD"), "ref: refs/heads/main\n");
  });

  it("records a stop during a guest run as unclean, so the next host takes no exec step the run left as the user's", async () => {
    const first = host();
    await ready(first);
    first.send({ type: "refusal", id: "r1", run: true });
    expect(await answer(first, "r1")).toEqual({ ok: null });
    writeFileSync(todo(), PLANTED);
    first.send({ type: "stop" });
    expect(await first.exited).toBe(0);
    expect(readFileSync(todo(), "utf8")).toBe(PLANTED);
    expect(record().state).toBe("running");
    const second = host();
    await ready(second);
    second.send({ type: "refusal", id: "r2", run: false });
    expect(await answer(second, "r2")).toEqual({ ok: null });
    expect(readFileSync(todo(), "utf8")).toBe(COMMENTED);
  });

  it.each([
    ["a start, which no after ends", (harness: Harness) => harness.send({ type: "refusal", id: "s1", run: false })],
    ["a run cancelled while its refusal was asked", (harness: Harness) => {
      harness.send({ type: "refusal", id: "c1", run: true });
      harness.send({ type: "cancel", id: "c1" });
    }],
  ])("holds no look for %s", async (_name, begin) => {
    const harness = host();
    await ready(harness);
    begin(harness);
    await new Promise((resolve) => setTimeout(resolve, 300));
    writeFileSync(todo(), PLANTED);
    harness.send({ type: "stop" });
    expect(await harness.exited).toBe(0);
    expect(readFileSync(todo(), "utf8")).toBe(COMMENTED);
    expect(record().state).toBe("stopped");
  });
});

describe("a tool host's own sandbox", { timeout: 30_000 }, () => {
  it("never runs a program from the folder outside its sandbox", async () => {
    const proof = join(base, "ran-outside");
    // Anything in the folder may be a command's. srt looks up which and rg through the
    // host's own PATH: neither a folder entry nor a relative one may count.
    mkdirSync(join(folder, "bin"));
    for (const name of ["which", "rg"]) {
      writeFileSync(join(folder, "bin", name), `#!/bin/sh\ntouch '${proof}'\nexec /usr/bin/${name} "$@"\n`, { mode: 0o755 });
    }
    const harness = host({}, folder, { ...process.env, PATH: `${folder}/bin:bin:${process.env.PATH ?? ""}` });
    await ready(harness);
    expect(await harness.op("1", "ripgrep", { key: folder, mode: "files", pattern: "*.txt", glob: null, context: 0 })).toEqual({ ok: `${folder}/a.txt\n` });
    expect(existsSync(proof)).toBe(false);
  });

  it("never runs a program from the folder in its sandbox either: its helper's rg comes from the host's own PATH", async () => {
    const proof = join(folder, "ran-inside");
    mkdirSync(join(folder, "bin"));
    writeFileSync(join(folder, "bin", "rg"), `#!/bin/sh\ntouch '${proof}'\nexec /usr/bin/rg "$@"\n`, { mode: 0o755 });
    // The app's PATH and the host's own both name the folder's first, as a user's can.
    const harness = host({ env: { ...start.env, PATH: `${folder}/bin:/usr/bin:/bin` } }, undefined, { ...process.env, PATH: `${folder}/bin:${process.env.PATH ?? ""}` });
    await ready(harness);
    expect(await harness.op("1", "ripgrep", { key: folder, mode: "files", pattern: "*.txt", glob: null, context: 0 })).toEqual({ ok: `${folder}/a.txt\n` });
    expect(existsSync(proof)).toBe(false);
  });

  // Whether this computer lets a test mount in a user namespace of its own.
  const mounts = spawnSync("unshare", ["-Urm", "true"]).status === 0;

  it.skipIf(!mounts)("never runs a program from the folder by another of its paths: a bind mount of it", async () => {
    const proof = join(base, "ran-aliased");
    mkdirSync(join(folder, "bin"));
    for (const name of ["which", "rg"]) {
      writeFileSync(join(folder, "bin", name), `#!/bin/sh\necho "$0" >> '${proof}'\nexec /usr/bin/${name} "$@"\n`, { mode: 0o755 });
    }
    const alias = join(base, "alias");
    mkdirSync(alias);
    // The host's node, in a mount namespace of its own where alias is the folder mounted again.
    const node = join(base, "aliased-node");
    writeFileSync(node, `#!/bin/sh\nexec unshare -Urm sh -c 'mount --bind "$0" "$1" && shift && exec "$@"' '${folder}' '${alias}' '${process.execPath}' "$@"\n`, { mode: 0o755 });
    const harness = host({}, undefined, { ...process.env, PATH: `${alias}/bin:${process.env.PATH ?? ""}` }, node);
    await ready(harness);
    expect(await harness.op("1", "ripgrep", { key: folder, mode: "files", pattern: "*.txt", glob: null, context: 0 })).toEqual({ ok: `${folder}/a.txt\n` });
    expect(existsSync(proof)).toBe(false);
  });

  it("gives srt and its helper each entry of the host's PATH as where it leads at the start, so no link swapped in later moves it", async () => {
    // An rg in a folder of the app's, out of the bound folder through a .. that passes a folder in it,
    // which says what PATH it was given: the helper's own lookups fold a .. by its spelling, srt's which and the kernel by the folders.
    const tools = join(base, "tools");
    mkdirSync(tools);
    mkdirSync(join(folder, "sub"));
    writeFileSync(join(tools, "rg"), `#!/bin/sh\necho "$PATH" > '${folder}/given-path'\nexec /usr/bin/rg "$@"\n`, { mode: 0o755 });
    const harness = host({ appDirs: [...start.appDirs, tools] }, undefined, { ...process.env, PATH: `${folder}/sub/../../tools:${process.env.PATH ?? ""}` });
    await ready(harness);
    expect(await harness.op("1", "ripgrep", { key: folder, mode: "files", pattern: "*.txt", glob: null, context: 0 })).toEqual({ ok: `${folder}/a.txt\n` });
    expect(readFileSync(join(folder, "given-path"), "utf8").split(":")[0]).toBe(tools);
  });

  it("holds its working folder as it holds the chat's: an entry of its PATH inside it is dropped too, where srt keeps what the sandbox may write", async () => {
    const tools = join(base, "tools");
    mkdirSync(tools);
    writeFileSync(join(tools, "rg"), `#!/bin/sh\necho "$PATH" > '${folder}/given-path'\nexec /usr/bin/rg "$@"\n`, { mode: 0o755 });
    const working = join(start.tmp, "bin");
    mkdirSync(working, { recursive: true });
    const harness = host({ appDirs: [...start.appDirs, tools] }, undefined, { ...process.env, PATH: `${working}:${tools}:${process.env.PATH ?? ""}` });
    await ready(harness);
    expect(await harness.op("1", "ripgrep", { key: folder, mode: "files", pattern: "*.txt", glob: null, context: 0 })).toEqual({ ok: `${folder}/a.txt\n` });
    const given = readFileSync(join(folder, "given-path"), "utf8").trim().split(":");
    expect([given[0], given.includes(working)]).toEqual([tools, false]);
  });

  it("never runs a program from the folder through a relative entry of the host's PATH, wherever the host starts", async () => {
    const proof = join(folder, "ran-relative");
    mkdirSync(join(folder, "bin"));
    writeFileSync(join(folder, "bin", "rg"), `#!/bin/sh\necho "$0" >> '${proof}'\nexec /usr/bin/rg "$@"\n`, { mode: 0o755 });
    // Started outside the folder, so bin is no folder of it here; the helper would resolve it against the folder.
    const harness = host({}, base, { ...process.env, PATH: `bin:${process.env.PATH ?? ""}` });
    await ready(harness);
    expect(await harness.op("1", "ripgrep", { key: folder, mode: "files", pattern: "*.txt", glob: null, context: 0 })).toEqual({ ok: `${folder}/a.txt\n` });
    expect(existsSync(proof)).toBe(false);
  });

  it.each([
    // Spelled from the root, where a look that took the PATH as it is would read it.
    ["a relative entry of its PATH", () => [join(base, "only").slice(1), join(base, "only")]],
    ["an entry of its PATH inside the folder", () => [join(folder, "bin"), join(folder, "bin")]],
  ])("looks for its own tools only where it lets srt look: with its only bubblewrap in %s, it says bubblewrap is missing", async (_name, entry) => {
    // Every program of this computer's but bubblewrap.
    const bin = join(base, "bin");
    mkdirSync(bin);
    for (const name of readdirSync("/usr/bin")) if (name !== "bwrap") symlinkSync(join("/usr/bin", name), join(bin, name));
    const [spelled, dir] = entry() as [string, string];
    mkdirSync(dir);
    writeFileSync(join(dir, "bwrap"), "#!/bin/sh\n", { mode: 0o755 });
    const harness = host({}, base, { ...process.env, PATH: `${spelled}:${bin}` });
    expect(await refusal(harness)).toBe("Surogate's sandbox tools are missing. Run the install script again. It lacks bubblewrap");
  });

  it.each([
    ["under a file (ENOTDIR)", () => "/etc/passwd/bin"],
    ["in a loop of links (ELOOP)", () => {
      symlinkSync("loop", join(base, "loop"));
      return join(base, "loop", "bin");
    }],
    ["under a folder it cannot search (EACCES)", () => {
      mkdirSync(join(base, "shut", "bin"), { recursive: true });
      chmodSync(join(base, "shut"), 0o000);
      return join(base, "shut", "bin");
    }],
  ])("starts with an entry of the host's PATH it cannot judge, one %s, which it drops", async (_name, entry) => {
    const harness = host({}, base, { ...process.env, PATH: `${entry()}:${process.env.PATH ?? ""}` });
    try {
      expect(await refusal(harness)).toBe("it answered ready");
    } finally {
      // So that afterEach can remove it.
      if (existsSync(join(base, "shut"))) chmodSync(join(base, "shut"), 0o755);
    }
  });

  it("never reads the user's shell startup files outside its sandbox", async () => {
    // srt's outer bash runs out here. With a socket for stdin, as the helper's is, bash
    // takes itself for a remote shell and reads ~/.bashrc; its output would also spoil
    // the helper's handshake.
    const home = join(base, "home");
    mkdirSync(home);
    const marker = join(base, "bashrc-ran");
    writeFileSync(join(home, ".bashrc"), `echo Welcome to my shell\ntouch '${marker}'\n`);
    const harness = host({ env: { ...start.env, HOME: home } });
    await ready(harness);
    expect(await harness.op("1", "resolve", { path: "a.txt" })).toEqual({ ok: `${folder}/a.txt` });
    expect(existsSync(marker)).toBe(false);
  });

  it("answers a second host for the same folder, however it is spelled, that another chat has it", async () => {
    await ready(host());
    symlinkSync(folder, join(base, "link"));
    const second = host({ folder: join(base, "link"), tmp: join(base, "data", "tmp", "other") });
    const said = await second.until((messages) => messages.find((message) => message.type === "failed" || message.type === "ready"), 20_000);
    expect(said).toEqual({ type: "failed", message: expect.stringMatching(/another chat on this computer is working in this folder/), busy: true });
    expect(await second.exited).toBe(1);
  });

  it("keeps srt's sockets in the app's data, and a new host ends the bridges a killed one left", async () => {
    const first = host();
    await ready(first);
    const before = sockets();
    expect(before.length).toBeGreaterThan(0);
    const left = bridges();
    expect(left.length).toBeGreaterThan(0);
    // Killed alone, as when the app dies with it: its sandbox goes with it
    // (die-with-parent), its socat bridges, outside the sandbox, go on.
    process.kill(first.child.pid ?? 0, "SIGKILL");
    await first.exited;
    expect(left.some(alive)).toBe(true);
    await ready(host());
    await until(() => left.filter(alive).length === 0);
    expect(sockets().filter((name) => before.includes(name))).toEqual([]);
  });

  it("has srt's proxy refuse every connection from its helper's sandbox, through its sockets and with its credential", async () => {
    const harness = host();
    await ready(harness);
    // A server on this computer that the sandbox would reach, were a connection let through.
    const server = createServer((socket) => socket.end());
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    const { port } = server.address() as { port: number };
    try {
      const http = sockets().find((name) => name.startsWith("claude-http"));
      expect(http).toBeDefined();
      // The sandbox's credential, which srt puts on the wrap's command line in HTTP_PROXY.
      const wraps = spawnSync("pgrep", ["-P", String(harness.child.pid)], { encoding: "utf8" }).stdout.split("\n").filter(Boolean);
      const lines = wraps.map((pid) => {
        try {
          return readFileSync(`/proc/${pid}/cmdline`, "latin1");
        } catch {
          return "";
        }
      }).join(" ");
      const credential = /http:\/\/([^:@\s'"]+:[0-9a-f]{32})@/.exec(lines)?.[1];
      expect(credential).toBeDefined();
      const answer = await new Promise<string>((resolve) => {
        const socket = connect(join(srtTmp(), http ?? ""));
        let got = "";
        const timer = setTimeout(() => {
          socket.destroy();
          resolve(`no answer: ${got}`);
        }, 8_000);
        socket.on("data", (chunk: Buffer) => {
          got += chunk.toString();
          if (!got.includes("\r\n")) return;
          clearTimeout(timer);
          socket.destroy();
          resolve(got.split("\r\n")[0] ?? "");
        });
        socket.on("error", (error) => {
          clearTimeout(timer);
          resolve(`error: ${error.message}`);
        });
        const auth = Buffer.from(decodeURIComponent(credential ?? "")).toString("base64");
        socket.write(`CONNECT localhost:${port} HTTP/1.1\r\nHost: localhost:${port}\r\nProxy-Authorization: Basic ${auth}\r\n\r\n`);
      });
      expect(answer).toMatch(/ 403 /);
    } finally {
      server.close();
    }
  });

  it("leaves no socket behind when its helper dies", async () => {
    const harness = host();
    await ready(harness);
    expect(sockets().length).toBeGreaterThan(0);
    execFileSync("pkill", ["-KILL", "-P", String(harness.child.pid)]);
    expect(await harness.exited).toBe(1);
    expect(sockets()).toEqual([]);
  });

  it("stops cleanly when the app's channel closes", async () => {
    const harness = host();
    await ready(harness);
    harness.child.disconnect();
    const code = await harness.exited;
    // Its final look and srt's reset ran: the way out every stop takes.
    expect({ code, sockets: sockets(), state: readRecord(recordOf())?.state }).toEqual({ code: 0, sockets: [], state: "stopped" });
  });

  it("says so when the app's data folder's path is too long for srt's sockets", async () => {
    const harness = host({ dataDir: join(base, "d".repeat(90)), tmp: join(base, "d".repeat(90), "tmp", "root") });
    expect(await refusal(harness)).toMatch(/too long for the sandbox's sockets/);
    expect(await harness.exited).toBe(1);
  });

  it("leaves the user's folder as it was when it is killed, and when the next host starts", async () => {
    mkdirSync(join(folder, ".git"));
    const before = readdirSync(folder).sort();
    const first = host({}, folder);
    await ready(first);
    first.killGroup();
    await first.exited;
    expect(readdirSync(folder).sort()).toEqual(before);
    await ready(host({}, folder));
    expect([readdirSync(folder).sort(), readdirSync(join(folder, ".git"))]).toEqual([before, []]);
  });
});

describe("a tool host's record of its folder", { timeout: 30_000 }, () => {
  const planted = () => join(folder, "sub", ".git", "hooks", "pre-commit");
  // What a command in the guest leaves through the folder's share: an executable hook.
  const plant = () => {
    mkdirSync(dirname(planted()), { recursive: true });
    writeFileSync(planted(), "#!/bin/sh\n", { mode: 0o755 });
  };
  // The refusal before a run in the guest, as the VmExecutor asks it.
  const before = async (harness: Harness, id: string) => {
    harness.send({ type: "refusal", id, run: true });
    return result(harness, id);
  };

  it("makes the hooks left while a killed host held the folder non-executable before the next host lets a command run, and keeps the user's own", async () => {
    mkdirSync(join(folder, ".git", "hooks"), { recursive: true });
    writeFileSync(join(folder, ".git", "hooks", "pre-push"), "#!/bin/sh\n", { mode: 0o755 });
    const first = host();
    await ready(first);
    expect(await before(first, "first")).toEqual({ ok: null });
    plant();
    first.killGroup();
    await first.exited;
    const second = host();
    await ready(second);
    expect(await before(second, "second")).toEqual({ ok: null });
    expect(statSync(planted()).mode & 0o111).toBe(0);
    expect(statSync(join(folder, ".git", "hooks", "pre-push")).mode & 0o111).not.toBe(0);
  });

  it.skipIf(process.getuid?.() === 0)("keeps the record of a host that stopped without seeing the whole folder", async () => {
    const first = host();
    await ready(first);
    expect(await before(first, "first")).toEqual({ ok: null });
    plant();
    chmodSync(join(folder, "sub"), 0);
    try {
      await first.stop();
      // Stopped, not killed by the harness: the record stays because of the look.
      expect(await first.exited).toBe(0);
    } finally {
      // The user does what the refusal asked, and a new host starts. Also lets afterEach remove the folder.
      chmodSync(join(folder, "sub"), 0o755);
    }
    const second = host();
    await ready(second);
    expect(await before(second, "second")).toEqual({ ok: null });
    expect(statSync(planted()).mode & 0o111).toBe(0);
  });
});

// A folder of the user's with a project's thread on it, as the app lays it out: the folder itself in the user's home,
// and in the app's data the folder's place, with its history, each thread's repository and copy, and what its landings keep.
const KEY = "0123456789abcdef";
const THREAD = "0b6c1d3e-6f0a-4c1e-9a52-6a1d2c3b4e5f";
const OTHER = "7d8e9f00-1a2b-4c3d-8e4f-5a6b7c8d9e0f";
const SAGA = "0f6d1c5e-7a3b-4c2d-9e1f-0a1b2c3d4e5f";
const HELPER = fileURLToPath(new URL("../dist/files/helper.js", import.meta.url));
let home: string;
let reports: string;
let data: string;
let place: string;
let copy: string;
let other: string;
let kept: string;

function lay(): void {
  home = join(base, "home");
  reports = join(home, "Reports");
  data = start.dataDir;
  place = join(data, "history", KEY);
  copy = join(place, "threads", THREAD);
  other = join(place, "threads", OTHER);
  kept = join(data, "landings", KEY);
  for (const [path, text] of [
    [join(home, "secret.txt"), "the user's own, outside the folder\n"],
    [join(reports, "a.txt"), "the user's own\n"],
    [join(reports, "sub", "b.txt"), "the user's own, in a folder\n"],
    [join(copy, "a.txt"), "the thread's\n"],
    [join(copy, "sub", "b.txt"), "the thread's, in a folder\n"],
    [join(other, "a.txt"), "another thread's\n"],
    [join(place, "history.git", "HEAD"), "ref: refs/heads/main\n"],
    [join(place, "clones", THREAD, "HEAD"), "ref: refs/heads/thread\n"],
    [join(data, "devices", "credentials.json"), "the device's own\n"],
  ] as const) {
    mkdirSync(dirname(path), { recursive: true });
    writeFileSync(path, text);
  }
}
// A host on the thread's copy, which stands for the folder; and one on the folder itself, for a landing from that copy.
const onCopy = (more: Partial<HostStart> = {}) =>
  host({ folder: copy, at: reports, env: { ...start.env, HOME: home }, tmp: join(data, "tmp", THREAD), ...more });
const forLanding = (more: Partial<HostStart> = {}) =>
  host({ folder: reports, landing: { copy, kept }, env: { ...start.env, HOME: home }, tmp: join(data, "tmp", `${THREAD}.landing`), ...more });
// What a host keeps for itself in the app's data: srt's sockets, its helper's working folder, its folder's record.
const hostsOwn = () => ["srt", "tmp", "folders"].map((name) => join(data, name));
const b64 = (text: string) => Buffer.from(text).toString("base64");
const failed = (harness: Harness) => harness.until((messages) => messages.find((message) => message.type === "failed" || message.type === "ready"));

// Every name under *root* and what it is: its mode, its links, its size, when it last changed, and its bytes. *but*:
// folders left out with all they hold. Two of these that are equal show nothing there was written, made, moved or removed.
function seen(root: string, but: string[] = []): Record<string, string> {
  const all: Record<string, string> = {};
  const look = (path: string) => {
    if (but.includes(path)) return;
    const found = lstatSync(path, { bigint: true });
    const holds = found.isSymbolicLink() ? `-> ${readlinkSync(path)}` : found.isFile() ? createHash("sha256").update(readFileSync(path)).digest("hex") : "";
    all[path] = `${found.mode.toString(8)} ${found.nlink} ${found.size} ${found.mtimeNs} ${found.ctimeNs} ${holds}`;
    if (found.isDirectory()) for (const name of readdirSync(path)) look(join(path, name));
  };
  look(root);
  return all;
}

// The processes below *pid*, by the kernel's own list of each one's children.
function below(pid: number): number[] {
  let children: number[] = [];
  try {
    children = readdirSync(`/proc/${pid}/task`).flatMap((task) => readFileSync(`/proc/${pid}/task/${task}/children`, "utf8").split(" ").filter(Boolean).map(Number));
  } catch {
    // Gone since it was listed.
  }
  return children.flatMap((child) => [child, ...below(child)]);
}
// The file helper of *harness*'s host, in its sandbox: the node that runs it.
function helperOf(harness: Harness): number {
  const helpers = below(harness.child.pid ?? 0).filter((pid) => {
    try {
      const words = readFileSync(`/proc/${pid}/cmdline`, "utf8").split("\0");
      return words[1] === "--disable-sigusr1" && words[2] === HELPER;
    } catch {
      return false;
    }
  });
  expect(helpers).toHaveLength(1);
  return helpers[0]!;
}

// A folder as a helper is told which one it was when its host checked it: its device and its inode.
const is = (path: string) => {
  const { dev, ino } = statSync(path);
  return `${dev}:${ino}`;
};
// What the file helper of *harness*'s host was started with, as the kernel has it: what it was told of its folder,
// and the folder it works in.
function startedWith(harness: Harness): { told: Record<string, string>; cwd: string } {
  const pid = helperOf(harness);
  const told = readFileSync(`/proc/${pid}/environ`, "utf8").split("\0").filter((entry) => entry.startsWith("SUROGATE_")).map((entry) => {
    const at = entry.indexOf("=");
    return [entry.slice(0, at), entry.slice(at + 1)];
  });
  return { told: Object.fromEntries(told) as Record<string, string>, cwd: readlinkSync(`/proc/${pid}/cwd`) };
}

// Loaded into a host before its own code, to do to it what this computer could: hold back what its file helper says
// (a helper slow to be ready), start that helper with another environment (a host that got it wrong), fail the
// making of srt's folder in words of the system's own, or put something else where a folder of its start's is, just as
// its sandbox is made: a link, by its words, or another folder renamed in (and what was there back once the sandbox
// is up), as whoever can write that folder's parent might between the host's look and the bind.
const UPSET = `data:text/javascript,${encodeURIComponent(`
  import cp from "node:child_process";
  import fs from "node:fs";
  import { syncBuiltinESMExports } from "node:module";
  import { PassThrough } from "node:stream";
  const { HELPER_LATE_MS: late, HELPER_ENV: env, SRT_FAILS: fails, FOLDER_SWAPPED: swapped } = process.env;
  const [spawn, mkdir] = [cp.spawn, fs.mkdirSync];
  cp.spawn = (...args) => {
    const helper = Array.isArray(args[1]) && String(args[1].at(-1)).includes("/files/helper.js");
    if (helper && env) Object.assign(args[2].env, JSON.parse(env));
    const swap = helper && swapped ? JSON.parse(swapped) : null;
    if (swap) {
      fs.renameSync(swap.at, swap.at + ".true");
      if (swap.link === undefined) fs.renameSync(swap.folder, swap.at);
      else fs.symlinkSync(swap.link, swap.at);
    }
    const child = spawn(...args);
    // At the first word of the helper, to its host or of its own end, before the host reads it: the sandbox is made by then.
    if (swap && swap.back) {
      let undone = false;
      const undo = () => {
        if (undone) return;
        undone = true;
        if (swap.link === undefined) fs.renameSync(swap.at, swap.folder);
        else fs.unlinkSync(swap.at);
        fs.renameSync(swap.at + ".true", swap.at);
      };
      child.stdout.once("data", undo);
      child.stderr.once("data", undo);
      child.once("exit", undo);
    }
    if (helper && late) {
      const said = child.stdout;
      child.stdout = new PassThrough();
      setTimeout(() => said.pipe(child.stdout), Number(late));
    }
    return child;
  };
  fs.mkdirSync = (...args) => {
    if (fails && String(args[0]).includes("/srt/")) throw new Error(fails);
    return mkdir(...args);
  };
  syncBuiltinESMExports();
`).replaceAll("'", "%27")}`;
// A host so upset, and no other: its own node is the test's, started through a line of sh that loads the above first
// (so the above is written with no apostrophe left in it, which would end the line's word).
function upset(how: { HELPER_LATE_MS?: string; HELPER_ENV?: string; SRT_FAILS?: string; FOLDER_SWAPPED?: string }): Harness {
  const node = join(base, "upset-node");
  // Written once: a second writing would be of a program another host is just being started from.
  if (!existsSync(node)) writeFileSync(node, `#!/bin/sh\nexec '${process.execPath}' --import '${UPSET}' "$@"\n`, { mode: 0o755 });
  const harness = new Harness(undefined, { ...process.env, ...how }, node);
  harnesses.push(harness);
  return harness;
}

// Run where a file helper runs: what each of *asks* comes to there, its value or the system's refusal.
const PROBE = `
  import fs from "node:fs";
  const ACTS = {
    read: ({ path }) => fs.readFileSync(path, "utf8"),
    list: ({ path }) => fs.readdirSync(path).sort(),
    write: ({ path, data }) => fs.writeFileSync(path, data),
    mkdir: ({ path }) => fs.mkdirSync(path),
    rename: ({ path, to }) => fs.renameSync(path, to),
    remove: ({ path }) => fs.rmSync(path, { recursive: true }),
    chmod: ({ path }) => fs.chmodSync(path, 0o777),
    capabilities: () => /^CapEff:\\s*(\\S+)$/m.exec(fs.readFileSync("/proc/self/status", "utf8"))[1],
  };
  const did = (act) => { try { return { ok: act() ?? null }; } catch (error) { return { code: error.code ?? String(error) }; } };
  console.log(JSON.stringify(JSON.parse(process.argv[1]).map((ask) => did(() => ACTS[ask.act](ask)))));
`;
type Ask = { act: "read" | "list" | "mkdir" | "remove" | "chmod" | "capabilities"; path?: string } | { act: "write"; path: string; data: string } | { act: "rename"; path: string; to: string };
type Answer = { ok: unknown } | { code: string };
// A program of the test's in the sandbox of *harness*'s file helper: in that helper's own namespaces (its mounts, its
// users, its processes), as this user and with no capability, as the helper is. So what it reaches is what the helper can.
function inSandbox(harness: Harness, asks: Ask[]): Answer[] {
  const ran = spawnSync(
    "nsenter", ["-t", String(helperOf(harness)), "-U", "-m", "-p", "--preserve-credentials", process.execPath, "--input-type=module", "-e", PROBE, JSON.stringify(asks)],
    { encoding: "utf8", timeout: 20_000 },
  );
  expect([ran.status, ran.stderr]).toEqual([0, ""]);
  return JSON.parse(ran.stdout) as Answer[];
}
// Whether this computer lets a test into a sandbox of its own: where it does not, what a helper's sandbox reaches is not measured.
const enters = spawnSync("nsenter", ["--version"]).status === 0 && spawnSync("unshare", ["-Urm", "true"]).status === 0;
// Everything a program can try on *file* in *dir*, a name that is there: to read it and list beside it, then to write
// over it, make a file and a folder beside it, change its mode, move it and remove it.
const tries = (dir: string, file: string): Ask[] => [
  { act: "read", path: join(dir, file) },
  { act: "list", path: dir },
  { act: "write", path: join(dir, file), data: "written from a sandbox\n" },
  { act: "write", path: join(dir, "planted.txt"), data: "planted from a sandbox\n" },
  { act: "mkdir", path: join(dir, "planted") },
  { act: "chmod", path: join(dir, file) },
  { act: "rename", path: join(dir, file), to: join(dir, "moved") },
  { act: "remove", path: join(dir, file) },
];
// What each of those comes to on a name the sandbox holds nothing of: it is not there, to read or to write.
const NOT_THERE: Answer[] = Array.from({ length: 8 }, () => ({ code: "ENOENT" }));
// And in a folder on the way to one the sandbox holds: a folder of the sandbox's own, with *way* in it and nothing
// else. A program writes there as it likes, and writes nothing on this computer.
const ownFolder = (way: string): Answer[] => [{ code: "ENOENT" }, { ok: [way] }, { ok: null }, { ok: null }, { ok: null }, { ok: null }, { ok: null }, { code: "ENOENT" }];
// The processes of a host's own group that are left, the host among them.
const groupOf = (harness: Harness) => spawnSync("pgrep", ["-g", String(harness.child.pid)], { encoding: "utf8" }).stdout.split("\n").filter(Boolean);
// Whether a host could take the lock of the folder at *path* now.
async function free(path: string): Promise<boolean> {
  const { dev, ino } = statSync(path);
  try {
    (await lockFolder(dev, ino, 0)).close();
    return true;
  } catch {
    return false;
  }
}

// Loaded into a file helper before its own code: it ends the process as a kill does, before the first link it makes at a path that matches.
const CUT = `data:text/javascript,${encodeURIComponent(`
  import fs from "node:fs";
  import { syncBuiltinESMExports } from "node:module";
  const real = fs.linkSync;
  fs.linkSync = (...args) => {
    if (new RegExp(process.env.LAND_CUT).test(String(args[1]))) {
      process.kill(process.pid, "SIGKILL");
      for (;;);
    }
    return real(...args);
  };
  syncBuiltinESMExports();
`)}`;
// Loaded into a file helper before its own code: before the first link it makes at a.txt, it waits as long as it is
// told, as a put-back copying a large file to a slow disk does.
const SLOW_LINK = `data:text/javascript,${encodeURIComponent(`
  import fs from "node:fs";
  import { syncBuiltinESMExports } from "node:module";
  const real = fs.linkSync;
  let once = false;
  fs.linkSync = (...args) => {
    if (!once && /\\/a\\.txt$/.test(String(args[1]))) {
      once = true;
      Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, Number(process.env.LAND_SLOW_MS));
    }
    return real(...args);
  };
  syncBuiltinESMExports();
`)}`;
// A file's blob id, as git names its bytes.
const blob = (text: string) => createHash("sha1").update(`blob ${Buffer.byteLength(text)}\0`).update(text).digest("hex");

// A landing's helper, outside any sandbox, killed between the two renames of its apply over the folder's a.txt: the
// name is empty, and the user's file lies beside it under a name of the landing's own. Its record is in *keeps*.
async function cut(keeps = kept): Promise<bigint> {
  const was = lstatSync(join(reports, "a.txt"), { bigint: true });
  const child = spawn(process.execPath, ["--import", CUT, HELPER], {
    env: { SUROGATE_FOLDER: reports, HOME: home, PATH: "/usr/bin:/bin", SUROGATE_COPY: copy, SUROGATE_KEPT: keeps, LAND_CUT: "/a\\.txt$" },
    stdio: ["pipe", "pipe", "inherit"],
  });
  child.stdin.on("error", () => {});
  const args = { action: "apply", saga: SAGA, step: 1, path: "a.txt", before: blob("the user's own\n"), after: blob("the thread's\n"), expected: "" };
  const ask = (id: string, more: Record<string, unknown>) => child.stdin.write(`${JSON.stringify({ id, kind: "land", args: more })}\n`);
  createInterface({ input: child.stdout }).on("line", (line) => {
    const said = JSON.parse(line) as { ready?: boolean; id?: string; outcome?: { ok: { revisions: Array<[string, string]> } } };
    if (said.ready) ask("look", { action: "revisions", paths: ["a.txt"] });
    else if (said.id === "look") ask("apply", { ...args, expected: said.outcome!.ok.revisions[0]![1] });
  });
  const bound = setTimeout(() => child.kill("SIGKILL"), 20_000);
  const signal = await new Promise<NodeJS.Signals | null>((resolve) => child.once("exit", (_code, how) => resolve(how)));
  clearTimeout(bound);
  // Beside the empty name: the user's file, moved aside, and the landing's own, not yet at the name.
  const beside = readdirSync(reports).filter((name) => name.startsWith(".surogate-")).map((name) => lstatSync(join(reports, name), { bigint: true }).ino);
  expect([signal, existsSync(join(reports, "a.txt")), beside.length, beside.includes(was.ino)]).toEqual(["SIGKILL", false, 2, true]);
  return was.ino;
}

describe("a tool host on a thread's copy", { timeout: 60_000 }, () => {
  beforeEach(lay);

  it("answers the thread's file tools in its copy, by the folder's path, and leaves the folder itself as it was", async () => {
    const before = seen(home);
    const harness = onCopy();
    await ready(harness);
    // Its helper is told the folder's path beside the copy, always: without it, it would be a chat's helper on the
    // copy, answering by the copy's own path. It works in the copy, and is told of no landing.
    // And which folder the copy was when the host checked it: it works in that one or does not start.
    expect(startedWith(harness)).toEqual({ told: { SUROGATE_FOLDER: copy, SUROGATE_FOLDER_IS: is(copy), SUROGATE_AT: reports }, cwd: copy });
    expect(await harness.op("1", "resolve", { path: "a.txt" })).toEqual({ ok: `${reports}/a.txt` });
    expect(await harness.op("2", "read", { key: `${reports}/a.txt`, max_bytes: null })).toEqual({ ok: b64("the thread's\n") });
    expect(await harness.op("3", "write", { key: `${reports}/made.txt`, data: b64("made by the thread\n") })).toEqual({ ok: null });
    expect(await harness.op("4", "delete", { key: `${reports}/sub/b.txt` })).toEqual({ ok: null });
    expect(await harness.op("5", "ripgrep", { key: reports, mode: "files", pattern: "made*", glob: null, context: 0 })).toEqual({ ok: `${reports}/made.txt\n` });
    // The copy's own path names nothing: a key is one under the folder's path alone.
    expect(await harness.op("6", "read", { key: `${copy}/a.txt`, max_bytes: null })).toEqual({
      error: { type: "sandbox", message: `Not a path in this folder: '${copy}/a.txt'` },
    });
    // Nothing lands from a copy's host: its helper was given no copy to land from.
    expect(await harness.op("7", "land", { action: "recover" })).toEqual({ error: { type: "unsupported", message: "This computer cannot do 'land' yet" } });
    expect([readFileSync(join(copy, "made.txt"), "utf8"), existsSync(join(copy, "sub", "b.txt"))]).toEqual(["made by the thread\n", false]);
    harness.send({ type: "stop" });
    expect(await harness.exited).toBe(0);
    expect(seen(home)).toEqual(before);
  });

  it.skipIf(!enters)("reaches its copy from its helper's sandbox and nothing else of the user's: not the folder it answers by, the folder's history, another thread's copy, what its landings keep, or the rest of the app's data", async () => {
    mkdirSync(kept, { recursive: true });
    writeFileSync(join(kept, "replaced"), "a file a landing replaced\n");
    const harness = onCopy();
    await ready(harness);
    expect(inSandbox(harness, [{ act: "capabilities" }])).toEqual([{ ok: "0000000000000000" }]);
    // Its copy: read, listed and written, on this computer.
    expect(inSandbox(harness, [
      { act: "read", path: join(copy, "a.txt") }, { act: "list", path: copy }, { act: "write", path: join(copy, "probe.txt"), data: "from the sandbox\n" },
    ])).toEqual([{ ok: "the thread's\n" }, { ok: ["a.txt", "sub"] }, { ok: null }]);
    expect(readFileSync(join(copy, "probe.txt"), "utf8")).toBe("from the sandbox\n");
    const before = seen(base, [copy, ...hostsOwn()]);
    for (const [what, dir, file, answers] of [
      ["the folder it answers by", reports, "a.txt", NOT_THERE],
      ["a folder in that folder", join(reports, "sub"), "b.txt", NOT_THERE],
      ["the user's home", home, "secret.txt", NOT_THERE],
      ["the folder's history", join(place, "history.git"), "HEAD", NOT_THERE],
      ["the thread's own repository", join(place, "clones", THREAD), "HEAD", NOT_THERE],
      ["another thread's copy", other, "a.txt", NOT_THERE],
      ["what the folder's landings keep", kept, "replaced", NOT_THERE],
      ["the rest of the app's data", join(data, "devices"), "credentials.json", NOT_THERE],
      ["the folder's place", place, "history.git", ownFolder("threads")],
      ["where the place keeps its copies", join(place, "threads"), OTHER, ownFolder(THREAD)],
    ] as const) {
      expect(inSandbox(harness, tries(dir, file)), what).toEqual(answers);
      // Whatever each write was answered, nothing on this computer was written, made, moved or removed.
      expect(seen(base, [copy, ...hostsOwn()]), what).toEqual(before);
    }
  });

  it.skipIf(!enters)("follows no link a command left in its copy out of it: to the folder it answers by, another thread's copy, the history or the user's home, by a whole path or from where the link lies", async () => {
    mkdirSync(kept, { recursive: true });
    writeFileSync(join(kept, "replaced"), "a file a landing replaced\n");
    // What a thread's command leaves in one line: it knows the folder's path, which is its own working folder.
    const links: Array<[string, string, string, Answer[]]> = [
      ["to-folder", reports, "a.txt", NOT_THERE],
      ["to-folder-from-here", "../../../../../home/Reports", "a.txt", NOT_THERE],
      ["to-home", home, "secret.txt", NOT_THERE],
      ["to-other", other, "a.txt", NOT_THERE],
      ["to-other-from-here", `../${OTHER}`, "a.txt", NOT_THERE],
      ["to-history", join(place, "history.git"), "HEAD", NOT_THERE],
      ["to-repository", join(place, "clones", THREAD), "HEAD", NOT_THERE],
      ["to-kept", kept, "replaced", NOT_THERE],
      ["to-data", join(data, "devices"), "credentials.json", NOT_THERE],
      // The folders above the copy are the sandbox's own: a write led there is answered, and is written nowhere.
      ["to-place", place, "history.git", ownFolder("threads")],
      ["to-copies-from-here", "..", OTHER, ownFolder(THREAD)],
    ];
    for (const [name, to] of links) symlinkSync(to, join(copy, name));
    const harness = onCopy();
    await ready(harness);
    const before = seen(base, [copy, ...hostsOwn()]);
    for (const [name, to, file, answers] of links) {
      // Out here the link leads where it says, to the file itself.
      expect(existsSync(join(copy, name, file)), name).toBe(true);
      // By the kernel, in the sandbox, it leads nowhere.
      expect(inSandbox(harness, tries(join(copy, name), file)), `${name} -> ${to}`).toEqual(answers);
      // And the helper follows it nowhere: a key through it is no path in the folder.
      for (const [id, kind, args] of [
        ["r", "read", { key: `${reports}/${name}/${file}`, max_bytes: null }],
        ["w", "write", { key: `${reports}/${name}/${file}`, data: b64("through a link\n") }],
        ["d", "delete", { key: `${reports}/${name}/${file}` }],
      ] as const) {
        expect(await harness.op(`${id}-${name}`, kind, args), `${kind} ${name}`).toEqual({ error: { type: "sandbox", message: `Not a path in this folder: '${reports}/${name}/${file}'` } });
      }
      expect(seen(base, [copy, ...hostsOwn()]), name).toEqual(before);
    }
  });

  it("says nothing by the copy's own path: what its host words itself, a failed start, a busy copy, the hook guard's refusal and its notice, names the folder", async () => {
    const said: unknown[] = [];
    const note = async (harness: Harness) => {
      said.push(await failed(harness));
      expect(await harness.exited).toBe(1);
    };
    // A start refused, each way a copy's is.
    await note(onCopy({ expect: { dev: 1, ino: 1, boot: BOOT_ID } }));
    await note(onCopy({ at: `${reports}\n` }));
    await note(onCopy({ folder: other.replace(OTHER, "0b6c1d3e-0000-4c1e-9a52-6a1d2c3b4e5f") }));
    await note(onCopy({ tmp: join(place, "tmp") }));
    await note(onCopy({ bwrapPath: "/nonexistent/bwrap" }));
    // What the system says of the copy as a start fails, in its own words, is said by the folder too.
    const system = upset({ SRT_FAILS: `EACCES: permission denied, mkdir '${copy}/.srt'` });
    system.send({ ...start, folder: copy, at: reports, expect: bound(copy), env: { ...start.env, HOME: home }, tmp: join(data, "tmp", "system") });
    await note(system);
    expect(said.at(-1)).toEqual({ type: "failed", message: `EACCES: permission denied, mkdir '${reports}/.srt'` });
    // A helper that would not start says so in one line, which names the folder's path as it was given: its end is the start's failure.
    const wrong = upset({ HELPER_ENV: JSON.stringify({ SUROGATE_AT: "Reports" }) });
    wrong.send({ ...start, folder: copy, at: reports, expect: bound(copy), env: { ...start.env, HOME: home }, tmp: join(data, "tmp", "wrong") });
    await note(wrong);
    expect(said.at(-1)).toEqual({ type: "failed", message: "the file helper exited: the file helper's SUROGATE_AT must be the whole path of the folder its copy is of: 'Reports'\n" });
    await until(() => groupOf(wrong).length === 0, 5_000);
    expect(await free(copy)).toBe(true);
    // A host that works: a second one on its copy, then what its hook guard says of the copy, which holds a folder
    // it cannot look through when the host starts.
    mkdirSync(join(copy, "shut", ".git", "hooks"), { recursive: true });
    chmodSync(join(copy, "shut"), 0o000);
    let harness: Harness;
    try {
      harness = onCopy();
      await ready(harness);
      await note(onCopy({ tmp: join(data, "tmp", "second"), lockWaitMs: 100 }));
      harness.send({ type: "refusal", id: "shut", run: true });
      said.push(await result(harness, "shut"));
      expect(said.at(-1)).toEqual({ error: { type: "sandbox", message: expect.stringMatching(/^Blocked: the computer cannot read shut in this folder/) } });
    } finally {
      chmodSync(join(copy, "shut"), 0o755);
    }
    harness.send({ type: "refusal", id: "run", run: true });
    said.push(await result(harness, "run"));
    writeFileSync(join(copy, "shut", ".git", "hooks", "pre-commit"), "#!/bin/sh\n", { mode: 0o755 });
    harness.send({ type: "after", id: "ran", outcome: { ok: { output: "", returncode: 0, timed_out: false } } });
    said.push(await result(harness, "ran"));
    expect(said.at(-1)).toEqual({ ok: { output: `${HOOKS_NOTICE}shut/.git/hooks/pre-commit`, returncode: 0, timed_out: false } });
    // The copy made again under it: the host works in the folder that was there, and says the chat's has gone.
    renameSync(copy, `${copy}.was`);
    mkdirSync(copy);
    said.push(await harness.op("gone", "write", { key: `${reports}/late.txt`, data: b64("into the copy that was\n") }));
    expect([said.at(-1), readdirSync(copy)]).toEqual([FOLDER_UNAVAILABLE, []]);
    expect(said).toHaveLength(12);
    for (const one of said) {
      const text = JSON.stringify(one);
      for (const own of [copy, `${copy}.was`, join(place, "threads"), place]) expect(text, text).not.toContain(own);
    }
    // Each that names a path names the folder's: the four refusals of the copy, and what the system said of it.
    expect(said.filter((one) => JSON.stringify(one).includes(reports))).toHaveLength(5);
  });

  it("holds the copy as a chat's host holds its folder: the record and the hooks it guards are the copy's, and the folder itself is another's to hold", async () => {
    const hook = (folder: string) => join(folder, "sub", ".git", "hooks", "pre-commit");
    const harness = onCopy();
    await ready(harness);
    const recordOn = (folder: string) => {
      const { dev, ino } = statSync(folder);
      return readRecord(join(data, "folders", `${dev}-${ino}.json`))?.state;
    };
    expect([recordOn(copy), recordOn(reports)]).toEqual(["running", undefined]);
    // A command of the thread's, in the guest, writes the copy: a hook it leaves there is stopped, and one in the folder is the user's.
    harness.send({ type: "refusal", id: "r", run: true });
    expect(await result(harness, "r")).toEqual({ ok: null });
    for (const folder of [copy, reports]) {
      mkdirSync(dirname(hook(folder)), { recursive: true });
      writeFileSync(hook(folder), "#!/bin/sh\n", { mode: 0o755 });
    }
    harness.send({ type: "after", id: "a", outcome: { ok: { output: "done", returncode: 0, timed_out: false } } });
    expect(await result(harness, "a")).toEqual({ ok: { output: `done\n${HOOKS_NOTICE}sub/.git/hooks/pre-commit`, returncode: 0, timed_out: false } });
    expect([statSync(hook(copy)).mode & 0o111, statSync(hook(reports)).mode & 0o111]).toEqual([0, 0o111]);
    // The folder's own lock is not the copy's host's: a chat on the folder itself works beside the thread.
    const chat = host({ folder: reports, env: { ...start.env, HOME: home }, tmp: join(data, "tmp", "chat") });
    await ready(chat);
    expect(await chat.op("1", "read", { key: `${reports}/a.txt`, max_bytes: null })).toEqual({ ok: b64("the user's own\n") });
    expect(await harness.op("2", "read", { key: `${reports}/a.txt`, max_bytes: null })).toEqual({ ok: b64("the thread's\n") });
  });

  it("starts only on the copy the app looked at: not one made again since, one that is gone, a link in its stead, or a copy named for a path it cannot stand for", async () => {
    const before = seen(home);
    const { dev, ino } = statSync(copy);
    const again = onCopy({ expect: { dev, ino: ino + 1, boot: BOOT_ID } });
    expect(await failed(again)).toEqual({ type: "failed", folder: true, message: `the copy of ${reports} this thread works in was made again after the app looked at it` });
    const named = onCopy({ at: join(data, "history") });
    expect(await failed(named)).toEqual({ type: "failed", message: `${join(data, "history")} is no path a thread's copy can stand for` });
    renameSync(copy, `${copy}.was`);
    const gone = onCopy();
    expect(await failed(gone)).toEqual({ type: "failed", folder: true, message: `the copy of ${reports} this thread works in is not there` });
    // A link the guest left where the copy was, to the folder itself: a host there would write the user's own files.
    symlinkSync(reports, copy);
    const linked = onCopy();
    expect(await failed(linked)).toEqual({ type: "failed", message: `the copy of ${reports} this thread works in is not a folder of the app's own` });
    for (const harness of [again, named, gone, linked]) expect(await harness.exited).toBe(1);
    expect(seen(home)).toEqual(before);
    expect(hostsOwn().filter((dir) => existsSync(dir))).toEqual([]);
  });
});

describe("a tool host for a landing", { timeout: 60_000 }, () => {
  beforeEach(lay);
  const land = (harness: Harness, id: string, args: Record<string, unknown>) => harness.op(id, "land", args);

  it("lands the thread's file in the folder from its copy, keeps the file it replaced in the app's data, and puts it back", async () => {
    const harness = forLanding();
    await ready(harness);
    // Its helper works in the folder itself, and is told of no folder it stands for: it would not start beside one.
    // Of each of its three folders, which one it was when the host checked it.
    expect(startedWith(harness)).toEqual({
      told: { SUROGATE_FOLDER: reports, SUROGATE_FOLDER_IS: is(reports), SUROGATE_COPY: copy, SUROGATE_COPY_IS: is(copy), SUROGATE_KEPT: kept, SUROGATE_KEPT_IS: is(kept) },
      cwd: reports,
    });
    const looked = await land(harness, "1", { action: "revisions", paths: ["a.txt", "new/c.txt"] }) as { ok: { revisions: Array<[string, string]> } };
    expect(looked.ok.revisions.map(([path, token]) => [path, token === "absent" ? token : "there"])).toEqual([["a.txt", "there"], ["new/c.txt", "absent"]]);
    const [theirs, mine] = [blob("the thread's\n"), blob("the user's own\n")];
    const apply = { action: "apply", saga: SAGA, step: 1, path: "a.txt", before: mine, after: theirs, expected: looked.ok.revisions[0]![1] };
    expect(await land(harness, "2", apply)).toEqual({ ok: { path: "a.txt", before: mine, after: theirs, made: [] } });
    expect([readFileSync(join(reports, "a.txt"), "utf8"), readFileSync(join(kept, SAGA, "1"), "utf8")]).toEqual(["the thread's\n", "the user's own\n"]);
    // What it keeps is this user's alone to open, in a folder the host made so.
    expect([statSync(kept).mode & 0o7777, statSync(join(data, "landings")).mode & 0o7777, statSync(join(kept, SAGA, "1")).mode & 0o7777]).toEqual([0o700, 0o700, 0o600]);
    expect(await land(harness, "3", { action: "unapply", saga: SAGA, step: 1, path: "a.txt" })).toEqual({ ok: { path: "a.txt", put_back: true } });
    expect([readFileSync(join(reports, "a.txt"), "utf8"), readdirSync(kept)]).toEqual(["the user's own\n", []]);
    harness.send({ type: "stop" });
    expect(await harness.exited).toBe(0);
    // The kept folder is the folder's own from its first landing's start: only a start that failed takes away the one it made.
    expect(readdirSync(join(data, "landings"))).toEqual([KEY]);
  });

  it("never runs a program from the thread's copy, or from what it keeps, outside its sandbox: a command wrote the one, and its helper writes the other", async () => {
    const proof = join(base, "ran-outside");
    // srt looks up which and rg through the host's own PATH, out here: neither the copy nor the kept folder may be on it.
    for (const dir of [join(copy, "bin"), join(kept, "bin")]) {
      mkdirSync(dir, { recursive: true });
      for (const name of ["which", "rg"]) writeFileSync(join(dir, name), `#!/bin/sh\necho "$0" >> '${proof}'\nexec /usr/bin/${name} "$@"\n`, { mode: 0o755 });
    }
    const harness = host(
      { folder: reports, landing: { copy, kept }, env: { ...start.env, HOME: home }, tmp: join(data, "tmp", "landing") }, undefined,
      { ...process.env, PATH: `${copy}/bin:${kept}/bin:${process.env.PATH ?? ""}` },
    );
    await ready(harness);
    expect(await land(harness, "1", { action: "revisions", paths: ["a.txt"] })).toMatchObject({ ok: { revisions: [["a.txt", expect.stringMatching(/^[0-9]+:/)]] } });
    expect(existsSync(proof)).toBe(false);
    // The same line run by a chat's host, for which they are folders like any other, does run them: the test's own control.
    const chat = host({ folder: join(home, "Reports", "sub"), env: { ...start.env, HOME: home }, tmp: join(data, "tmp", "chat") }, undefined, {
      ...process.env, PATH: `${copy}/bin:${process.env.PATH ?? ""}`,
    });
    await failed(chat);
    expect(readFileSync(proof, "utf8")).toContain(`${copy}/bin/which`);
  });

  it("takes the land kind and no other, and guards no command: its folder is the user's own, and no record of it is the landing's to keep", async () => {
    const before = seen(home);
    const harness = forLanding();
    await ready(harness);
    const asked: Array<[string, Record<string, unknown>]> = [
      ...kinds().filter((kind) => kind !== "land").map((kind): [string, Record<string, unknown>] => [kind, { key: `${reports}/a.txt`, path: "a.txt", data: b64("over the user's\n"), max_bytes: null }]),
      ["run", { command: `touch '${reports}/ran'`, workdir: null, timeout: 10 }],
      ["bind", {}],
      ["Land", { action: "recover" }],
    ];
    expect(asked.map(([kind]) => kind)).toEqual(expect.arrayContaining(["write", "delete", "read", "ripgrep", "resolve"]));
    for (const [kind, args] of asked) {
      expect(await harness.op(kind, kind, args), kind).toEqual({ error: { type: "unsupported", message: `This computer cannot do '${kind}' here` } });
    }
    // The hook guard's two questions about a command are answered, never left waiting: no command runs here.
    harness.send({ type: "refusal", id: "r", run: true });
    expect(await result(harness, "r")).toEqual({ error: { type: "unsupported", message: "This computer cannot run a command here" } });
    harness.send({ type: "after", id: "a", outcome: { ok: { output: "done", returncode: 0, timed_out: false } } });
    expect(await result(harness, "a")).toEqual({ error: { type: "unsupported", message: "This computer cannot run a command here" } });
    expect(await land(harness, "l", { action: "recover" })).toEqual({ ok: { restored: [], beside: [], lost: [], unread: [] } });
    harness.send({ type: "stop" });
    expect(await harness.exited).toBe(0);
    expect(seen(home)).toEqual(before);
    expect(existsSync(join(data, "folders"))).toBe(false);
  });

  it.skipIf(!enters)("reads the thread's copy and cannot write it, writes the folder and the kept folder, and reaches no other thread's copy, nor the history, nor the rest of the app's data", async () => {
    const harness = forLanding();
    await ready(harness);
    expect(inSandbox(harness, [{ act: "capabilities" }])).toEqual([{ ok: "0000000000000000" }]);
    // The folder and the kept folder: written, on this computer.
    expect(inSandbox(harness, [
      { act: "read", path: join(reports, "a.txt") }, { act: "write", path: join(reports, "probe.txt"), data: "into the folder\n" },
      { act: "write", path: join(kept, "probe"), data: "into the kept folder\n" },
    ])).toEqual([{ ok: "the user's own\n" }, { ok: null }, { ok: null }]);
    expect([readFileSync(join(reports, "probe.txt"), "utf8"), readFileSync(join(kept, "probe"), "utf8")]).toEqual(["into the folder\n", "into the kept folder\n"]);
    const before = seen(base, [reports, kept, ...hostsOwn()]);
    // The copy: read, and nothing of it written, made, moved or removed.
    const [read, listed, ...writes] = inSandbox(harness, tries(copy, "a.txt"));
    expect([read, listed]).toEqual([{ ok: "the thread's\n" }, { ok: ["a.txt", "sub"] }]);
    expect(writes).toEqual(writes.map(() => ({ code: "EROFS" })));
    expect(seen(base, [reports, kept, ...hostsOwn()])).toEqual(before);
    for (const [what, dir, file, answers] of [
      ["another thread's copy", other, "a.txt", NOT_THERE],
      ["the folder's history", join(place, "history.git"), "HEAD", NOT_THERE],
      ["the thread's own repository", join(place, "clones", THREAD), "HEAD", NOT_THERE],
      ["the rest of the app's data", join(data, "devices"), "credentials.json", NOT_THERE],
      ["the user's home", home, "secret.txt", ownFolder("Reports")],
      ["the folder's place", place, "history.git", ownFolder("threads")],
      ["where the place keeps its copies", join(place, "threads"), OTHER, ownFolder(THREAD)],
      ["where the app keeps every folder's replaced files", join(data, "landings"), "fedcba9876543210", ownFolder(KEY)],
    ] as const) {
      expect(inSandbox(harness, tries(dir, file)), what).toEqual(answers);
      expect(seen(base, [reports, kept, ...hostsOwn()]), what).toEqual(before);
    }
  });

  it("takes the copy and the kept folder only by their real paths, through no link: one at either, or above either, refuses the start in words", async () => {
    const elsewhere = join(base, "elsewhere");
    const refusals: Array<[string, () => Partial<HostStart>, RegExp]> = [
      ["a link at the copy's path, to the folder itself", () => {
        rmSync(copy, { recursive: true });
        symlinkSync(reports, copy);
        return {};
      }, /^this landing's copy is not a thread's copy of the folder: the copy of .* is not a folder of the app's own$/],
      ["a link where the place keeps its copies", () => {
        renameSync(join(place, "threads"), join(elsewhere, "threads"));
        symlinkSync(join(elsewhere, "threads"), join(place, "threads"));
        return {};
      }, /^this landing's copy is not a thread's copy of the folder: the copy of .* is not a folder of the app's own$/],
      ["a link at the folder's place", () => {
        renameSync(place, join(elsewhere, KEY));
        symlinkSync(join(elsewhere, KEY), place);
        return {};
      }, /^this landing's copy is not a thread's copy of the folder: the copy of .* is not a folder of the app's own$/],
      ["a link where the app keeps every place", () => {
        renameSync(join(data, "history"), join(elsewhere, "history"));
        symlinkSync(join(elsewhere, "history"), join(data, "history"));
        return {};
      }, /^this landing's copy is not a thread's copy of the folder: the copy of .* is not a folder of the app's own$/],
      ["a link at the kept folder's path", () => {
        mkdirSync(join(data, "landings"));
        symlinkSync(reports, kept);
        return {};
      }, /^the kept folder of .*'s landings is not a folder of the app's own$/],
      ["a link where the app keeps every folder's replaced files", () => {
        symlinkSync(elsewhere, join(data, "landings"));
        return {};
      }, /^the kept folder of .*'s landings is not a folder of the app's own$/],
      ["the copy named through a link to the app's data", () => {
        symlinkSync(data, join(base, "linked"));
        return { dataDir: join(base, "linked"), landing: { copy: join(base, "linked", "history", KEY, "threads", THREAD), kept } };
      }, /^this landing's copy is not a thread's copy of the folder: /],
      ["the kept folder named through a link to the app's data", () => {
        symlinkSync(data, join(base, "linked"));
        return { dataDir: join(base, "linked"), landing: { copy, kept: join(base, "linked", "landings", KEY) } };
      }, /^this landing's kept folder is not the one the app keeps for /],
      ["another thread's id that is none", () => ({ landing: { copy: join(place, "threads", "not-a-thread"), kept } }), /^this landing's copy is not a thread's copy of the folder: /],
      ["a kept folder of another folder's key", () => ({ landing: { copy, kept: join(data, "landings", "fedcba9876543210") } }), /^this landing's kept folder is not the one the app keeps for /],
    ];
    for (const [what, arrange, why] of refusals) {
      rmSync(base, { recursive: true, force: true });
      mkdirSync(base);
      lay();
      mkdirSync(elsewhere);
      const more = arrange();
      const before = seen(base);
      const harness = forLanding(more);
      const said = await failed(harness);
      expect(said.type === "failed" ? said.message : said.type, what).toMatch(why);
      expect(said, what).not.toHaveProperty("folder");
      expect(await harness.exited, what).toBe(1);
      // Refused before it held anything: nothing was made for it, here or where a link leads.
      expect(seen(base), what).toEqual(before);
    }
  });

  it("is given the copy and the kept folder by the real path of the app's data, where the app reaches its data through a link", async () => {
    symlinkSync(data, join(base, "linked"));
    const harness = forLanding({ dataDir: join(base, "linked"), tmp: join(base, "linked", "tmp", "landing") });
    await ready(harness);
    expect(await land(harness, "1", { action: "revisions", paths: ["a.txt"] })).toMatchObject({ ok: { revisions: [["a.txt", expect.stringMatching(/^[0-9]+:/)]] } });
    expect(statSync(kept).isDirectory()).toBe(true);
  });
});

describe("what a landing cut short left, when the next landing's host starts", { timeout: 90_000 }, () => {
  beforeEach(lay);
  it("is put back by the first step its host is asked, before that step looks at the folder: the user's very file is at its name again", async () => {
    const ino = await cut();
    const harness = forLanding();
    await ready(harness);
    // Ready at once: nothing is put back before a step is asked.
    expect(existsSync(join(reports, "a.txt"))).toBe(false);
    const looked = await harness.op("1", "land", { action: "revisions", paths: ["a.txt"] }) as { ok: { revisions: Array<[string, string]> } };
    const now = lstatSync(join(reports, "a.txt"), { bigint: true });
    expect([readFileSync(join(reports, "a.txt"), "utf8"), now.ino, now.nlink, readdirSync(reports).sort()]).toEqual(["the user's own\n", ino, 1n, ["a.txt", "sub"]]);
    expect(looked.ok.revisions).toEqual([["a.txt", expect.stringMatching(new RegExp(`^[0-9]+:${ino}:`))]]);
    expect(await harness.op("2", "land", { action: "recover" })).toEqual({ ok: { restored: ["a.txt"], beside: [], lost: [], unread: [] } });
  });

  it("is put back however long that takes the folder's disk, in the step, which no bound of a start cuts: a landing's helper not ready in the time a chat's has is not waited for", async () => {
    const ino = await cut();
    // Both timers start as the helper is started, in the host itself: the helper's first word comes after a chat's bound, whatever the load.
    const late = { HELPER_LATE_MS: String(READY_MS + 2_500) };
    mkdirSync(join(home, "Notes"));
    const [chat, landing] = [upset(late), upset(late)];
    chat.send({ ...start, folder: join(home, "Notes"), expect: bound(join(home, "Notes")), env: { ...start.env, HOME: home }, tmp: join(data, "tmp", "chat") });
    landing.send({ ...start, folder: reports, expect: bound(reports), landing: { copy, kept }, env: { ...start.env, HOME: home }, tmp: join(data, "tmp", "landing") });
    const said = (harness: Harness) => harness.until((messages) => messages.find((message) => message.type === "failed" || message.type === "ready"), 40_000);
    for (const harness of [chat, landing]) expect(await said(harness)).toMatchObject({ type: "failed", message: expect.stringMatching(/^the file helper did not start/) });
    expect(existsSync(join(reports, "a.txt"))).toBe(false);
    // A landing's helper whose put-back takes longer than that, held inside it, is answered when it is done.
    const slow = upset({ HELPER_ENV: JSON.stringify({ NODE_OPTIONS: `--import=${SLOW_LINK}`, LAND_SLOW_MS: String(READY_MS + 2_500) }) });
    slow.send({ ...start, folder: reports, expect: bound(reports), landing: { copy, kept }, env: { ...start.env, HOME: home }, tmp: join(data, "tmp", "landing") });
    await ready(slow);
    const began = Date.now();
    slow.send({ type: "op", id: "1", kind: "land", args: { action: "recover" } });
    expect(await slow.until((messages) => {
      const answer = messages.find((message) => message.type === "result" && message.id === "1");
      return answer?.type === "result" ? answer.outcome : undefined;
    }, 40_000)).toEqual({ ok: { restored: ["a.txt"], beside: [], lost: [], unread: [] } });
    expect(Date.now() - began).toBeGreaterThanOrEqual(READY_MS + 2_500);
    expect([readFileSync(join(reports, "a.txt"), "utf8"), lstatSync(join(reports, "a.txt"), { bigint: true }).ino]).toEqual(["the user's own\n", ino]);
  });
});

describe("a tool host that puts back what a landing cut short, on the folder with what its landings keep and no copy", { timeout: 60_000 }, () => {
  beforeEach(lay);
  // What the folder's landings keep, where the app keeps it for that folder.
  const keeps = () => join(data, "landings", keyOf(reports));
  const forRecovery = (more: Partial<HostStart> = {}) =>
    host({ folder: reports, recovery: { kept: keeps() }, env: { ...start.env, HOME: home }, tmp: join(data, "tmp", `${keyOf(reports)}.recover`), ...more });
  const ONLY = { error: { type: "unsupported", message: "This computer only puts back here what a landing cut short in the folder" } };

  it("puts back, when it is asked, what a helper killed between an apply's two renames left in the folder, and does nothing else", async () => {
    const ino = await cut(keeps());
    const harness = forRecovery();
    await ready(harness);
    const elsewhere = seen(base, [reports, keeps(), ...hostsOwn()]);
    // Its helper works in the folder itself, and is told of the kept folder and of no copy: which one each was when checked.
    expect(startedWith(harness)).toEqual({ told: { SUROGATE_FOLDER: reports, SUROGATE_FOLDER_IS: is(reports), SUROGATE_KEPT: keeps(), SUROGATE_KEPT_IS: is(keeps()) }, cwd: reports });
    // Any kind but the land kind its host refuses, and any action of it but the put-back its helper does: before anything is touched.
    for (const kind of [...kinds().filter((one) => one !== "land"), "run", "bind"]) {
      expect(await harness.op(kind, kind, { key: `${reports}/a.txt`, path: "a.txt", data: b64("over the user's\n"), max_bytes: null }), kind).toEqual({
        error: { type: "unsupported", message: `This computer cannot do '${kind}' here` },
      });
    }
    for (const [id, args] of [{ action: "revisions", paths: ["a.txt"] }, { action: "unapply", saga: SAGA, step: 1, path: "a.txt" }, { action: "forget", saga: SAGA }].entries()) {
      expect(await harness.op(`l${id}`, "land", args), JSON.stringify(args)).toEqual(ONLY);
    }
    harness.send({ type: "refusal", id: "r", run: true });
    expect(await result(harness, "r")).toEqual({ error: { type: "unsupported", message: "This computer cannot run a command here" } });
    expect(existsSync(join(reports, "a.txt"))).toBe(false);
    expect(await harness.op("1", "land", { action: "recover" })).toEqual({ ok: { restored: ["a.txt"], beside: [], lost: [], unread: [] } });
    const now = lstatSync(join(reports, "a.txt"), { bigint: true });
    expect([readFileSync(join(reports, "a.txt"), "utf8"), now.ino, now.nlink, readdirSync(reports).sort(), readdirSync(keeps())]).toEqual(["the user's own\n", ino, 1n, ["a.txt", "sub"], []]);
    harness.send({ type: "stop" });
    expect(await harness.exited).toBe(0);
    // Nothing else of the user's, or of the app's, changed; and no record of a folder's is kept for it.
    expect(seen(base, [reports, keeps(), ...hostsOwn()])).toEqual(elsewhere);
    expect(existsSync(join(data, "folders"))).toBe(false);
  });

  it.skipIf(!enters)("reaches from its helper's sandbox the folder and what its landings keep, and nothing else of the app's data: no thread's copy, nor the history", async () => {
    mkdirSync(keeps(), { recursive: true });
    const harness = forRecovery();
    await ready(harness);
    expect(inSandbox(harness, [{ act: "capabilities" }])).toEqual([{ ok: "0000000000000000" }]);
    expect(inSandbox(harness, [
      { act: "write", path: join(reports, "probe.txt"), data: "into the folder\n" }, { act: "write", path: join(keeps(), "probe"), data: "into the kept folder\n" },
    ])).toEqual([{ ok: null }, { ok: null }]);
    const before = seen(base, [reports, keeps(), ...hostsOwn()]);
    for (const [what, dir, file, answers] of [
      ["the thread's copy", copy, "a.txt", NOT_THERE],
      ["another thread's copy", other, "a.txt", NOT_THERE],
      ["the folder's history", join(place, "history.git"), "HEAD", NOT_THERE],
      ["the rest of the app's data", join(data, "devices"), "credentials.json", NOT_THERE],
      ["the user's home", home, "secret.txt", ownFolder("Reports")],
      ["where the app keeps every folder's replaced files", join(data, "landings"), "fedcba9876543210", ownFolder(keyOf(reports))],
    ] as const) {
      expect(inSandbox(harness, tries(dir, file)), what).toEqual(answers);
      expect(seen(base, [reports, keeps(), ...hostsOwn()]), what).toEqual(before);
    }
  });

  it("takes the folder's lock as every host does: a chat that holds the folder makes it wait for as long as it is told, then it says the folder is busy, having put nothing back", async () => {
    await cut(keeps());
    const chat = host({ folder: reports, env: { ...start.env, HOME: home }, tmp: join(data, "tmp", "chat") });
    await ready(chat);
    const began = Date.now();
    const waited = forRecovery({ lockWaitMs: 700 });
    expect(await failed(waited)).toEqual({ type: "failed", message: "another chat on this computer is working in this folder; this one can use it once that one is done", busy: true });
    expect(Date.now() - began).toBeGreaterThanOrEqual(700);
    expect(await waited.exited).toBe(1);
    expect(existsSync(join(reports, "a.txt"))).toBe(false);
    // Once the chat lets the folder go, it starts, and puts the file back.
    await chat.stop();
    const after = forRecovery({ lockWaitMs: 0 });
    await ready(after);
    expect(await after.op("1", "land", { action: "recover" })).toMatchObject({ ok: { restored: ["a.txt"] } });
  });

  it("says the folder is not there, or is not the one a landing was cut short in, and touches nothing of it or of what its landings keep", async () => {
    await cut(keeps());
    const was = bound(reports);
    renameSync(reports, join(home, "Reports moved"));
    const before = [seen(join(home, "Reports moved")), seen(data, hostsOwn())];
    const gone = forRecovery({ expect: was });
    expect(await failed(gone)).toEqual({ type: "failed", folder: true, message: `the folder ${reports} is not there` });
    mkdirSync(reports);
    const replaced = forRecovery({ expect: was });
    expect(await failed(replaced)).toEqual({ type: "failed", folder: true, message: `the folder at ${reports} is not the one a landing was cut short in` });
    for (const harness of [gone, replaced]) expect(await harness.exited).toBe(1);
    expect([seen(join(home, "Reports moved")), seen(data, hostsOwn())]).toEqual(before);
  });

  it("is refused before it holds anything for what is kept of another folder, and leaves nothing", async () => {
    mkdirSync(join(data, "landings", "fedcba9876543210"), { recursive: true });
    const before = seen(base);
    const harness = forRecovery({ recovery: { kept: join(data, "landings", "fedcba9876543210") } });
    expect(await failed(harness)).toEqual({ type: "failed", message: `what this recovery is given is not where the app keeps what ${reports}'s landings replaced` });
    expect(await harness.exited).toBe(1);
    expect(seen(base)).toEqual(before);
  });
});

describe("the folder's lock, between a thread's hosts and a chat's", { timeout: 60_000 }, () => {
  beforeEach(lay);
  const chatOn = (folder: string, name: string) => host({ folder, env: { ...start.env, HOME: home }, tmp: join(data, "tmp", name) });
  const BUSY = "another chat on this computer is working in this folder; this one can use it once that one is done";

  it("is the copy's for a thread's host and the folder's for a landing's, so neither waits for the other, whichever starts first", async () => {
    // The thread works in its copy; its landing starts on the folder while it does, and reads that copy.
    const thread = onCopy();
    await ready(thread);
    const landing = forLanding({ lockWaitMs: 0 });
    await ready(landing);
    expect([await free(copy), await free(reports), await free(other)]).toEqual([false, false, true]);
    // Another thread on the same folder works in its own copy meanwhile.
    const second = onCopy({ folder: other, tmp: join(data, "tmp", OTHER), lockWaitMs: 0 });
    await ready(second);
    expect(await thread.op("1", "write", { key: `${reports}/made.txt`, data: b64("while it lands\n") })).toEqual({ ok: null });
    expect(await landing.op("2", "land", { action: "revisions", paths: ["made.txt"] })).toEqual({ ok: { revisions: [["made.txt", "absent"]] } });
    expect(await second.op("3", "read", { key: `${reports}/a.txt`, max_bytes: null })).toEqual({ ok: b64("another thread's\n") });
    // Each lets go of its own, and of nothing else.
    await landing.stop();
    expect([await free(copy), await free(reports)]).toEqual([false, true]);
    await thread.stop();
    expect(await free(copy)).toBe(true);
    // The other way round: the landing's host holds the folder, and the thread's host starts on the copy.
    const first = forLanding({ lockWaitMs: 0 });
    await ready(first);
    await ready(onCopy({ lockWaitMs: 0 }));
  });

  it("makes a landing wait for a chat that holds the folder, for as long as it is told, and then says the folder is busy: nothing began, and nothing is left", async () => {
    const chat = chatOn(reports, "chat");
    await ready(chat);
    const before = seen(base, hostsOwn());
    let began = Date.now();
    const landing = forLanding({ lockWaitMs: 700 });
    expect(await failed(landing)).toEqual({ type: "failed", message: BUSY, busy: true });
    // As long as it was told, and no longer: not the time a chat's host waits.
    expect(Date.now() - began).toBeGreaterThanOrEqual(700);
    expect(Date.now() - began).toBeLessThan(LOCK_WAIT_MS / 2);
    expect(await landing.exited).toBe(1);
    // Not the kept folder, which it makes only once the folder is its own.
    expect([seen(base, hostsOwn()), existsSync(join(data, "landings"))]).toEqual([before, false]);
    // A landing told to wait longer than a chat's host does has the folder once the chat's lets it go, though that is later.
    began = Date.now();
    const patient = forLanding({ lockWaitMs: LOCK_WAIT_MS + 20_000 });
    setTimeout(() => void chat.stop(), LOCK_WAIT_MS + 1_000);
    expect(await patient.until((messages) => messages.find((message) => message.type === "failed" || message.type === "ready"), 40_000)).toMatchObject({ type: "ready" });
    expect(Date.now() - began).toBeGreaterThan(LOCK_WAIT_MS);
    // And while it lands the folder is its own: a second landing's host waits for it in its turn, as a chat's would.
    expect(await free(reports)).toBe(false);
    expect(await failed(forLanding({ lockWaitMs: 300, tmp: join(data, "tmp", "second") }))).toEqual({ type: "failed", message: BUSY, busy: true });
    expect(await patient.op("1", "land", { action: "recover" })).toEqual({ ok: { restored: [], beside: [], lost: [], unread: [] } });
  });

  it("gives a copy one host at a time, as a folder", async () => {
    await ready(onCopy());
    const second = onCopy({ tmp: join(data, "tmp", "again"), lockWaitMs: 300 });
    expect(await failed(second)).toEqual({ type: "failed", message: BUSY, busy: true });
    expect(await second.exited).toBe(1);
  });
});

describe("a tool host that does not start", { timeout: 60_000 }, () => {
  beforeEach(lay);

  // Gone with all it started, its folder's lock free.
  async function gone(harness: Harness, held: string): Promise<void> {
    expect(await harness.exited).toBe(1);
    await until(() => groupOf(harness).length === 0, 5_000);
    expect(await free(held)).toBe(true);
  }

  const linkedCopy = () => {
    rmSync(copy, { recursive: true });
    symlinkSync(reports, copy);
  };
  it.each<[string, () => void, () => Harness, () => string]>([
    ["on a copy that is a link", linkedCopy, () => onCopy(), () => reports],
    ["on a copy made again since the app looked at it", () => {}, () => onCopy({ expect: { dev: 1, ino: 1, boot: BOOT_ID } }), () => copy],
    ["on a copy named for a path in the app's data", () => {}, () => onCopy({ at: join(data, "devices") }), () => copy],
    ["for a landing from a copy that is a link", linkedCopy, () => forLanding(), () => reports],
    ["for a landing from a copy that is not there", () => {}, () => forLanding({ landing: { copy: join(place, "threads", "0b6c1d3e-0000-4c1e-9a52-6a1d2c3b4e5f"), kept } }), () => reports],
    ["for a landing whose kept folder is another folder's", () => {}, () => forLanding({ landing: { copy, kept: join(data, "landings", "fedcba9876543210") } }), () => reports],
    ["for a landing into a folder replaced since it was bound", () => {}, () => forLanding({ expect: { dev: 1, ino: 1, boot: BOOT_ID } }), () => reports],
    ["as both a copy's host and a landing's", () => {}, () => onCopy({ landing: { copy, kept } }), () => copy],
  ])("leaves nothing behind when it is refused %s: no process, no lock, and nothing made in the app's data or the folder", async (_what, arrange, begin, held) => {
    arrange();
    const before = seen(base);
    const harness = begin();
    expect(await failed(harness)).toMatchObject({ type: "failed" });
    await gone(harness, held());
    expect(seen(base)).toEqual(before);
  });

  it("takes away the kept folder it made for a landing whose sandbox then cannot start, and holds nothing", async () => {
    const before = seen(base);
    const harness = forLanding({ bwrapPath: "/nonexistent/bwrap" });
    expect(await failed(harness)).toEqual({ type: "failed", message: "Surogate's sandbox tools are missing. Run the install script again. It lacks bubblewrap" });
    await gone(harness, reports);
    // What is left is what any host keeps for its folder: srt's folder and its helper's working folder, both empty.
    expect(seen(base, hostsOwn())).toEqual({ ...before, [data]: seen(base, hostsOwn())[data] });
    expect([existsSync(join(data, "landings")), existsSync(join(data, "folders")), readdirSync(join(data, "srt")).flatMap((key) => readdirSync(join(data, "srt", key)))]).toEqual([false, false, []]);
    // A kept folder that was there, with what an earlier landing kept, stays as it was.
    mkdirSync(join(kept, SAGA), { recursive: true });
    writeFileSync(join(kept, SAGA, "1"), "a file a landing replaced\n");
    const held = seen(join(data, "landings"));
    const again = forLanding({ bwrapPath: "/nonexistent/bwrap" });
    expect(await failed(again)).toMatchObject({ type: "failed" });
    await gone(again, reports);
    expect(seen(join(data, "landings"))).toEqual(held);
  });

  it("leaves no process and no lock when its helper cannot be started in the copy, once srt's own are running", async () => {
    const before = seen(home);
    // A copy the host cannot enter: srt is ready for the helper, which is never started.
    chmodSync(copy, 0o000);
    try {
      const shut = onCopy();
      expect(await failed(shut)).toMatchObject({ type: "failed" });
      await gone(shut, copy);
      expect(readdirSync(join(data, "srt")).flatMap((key) => readdirSync(join(data, "srt", key)))).toEqual([]);
    } finally {
      chmodSync(copy, 0o755);
    }
    expect(seen(home)).toEqual(before);
  });
});

// What whoever can write a folder's parent might put at its path between a host's look at the folder and its sandbox's
// bind of it: a link, by its words, or another folder renamed in. *back*: taken away once the sandbox is up, and the
// folder that was there back at its path, where every later look of the host's finds it.
interface Swap {
  at: string;
  link?: string;
  folder?: string;
  back?: boolean;
}

describe("a tool host whose folder is another's by the time its sandbox binds it", { timeout: 120_000 }, () => {
  beforeEach(lay);
  // What lies in *root*, by its names from there: modes, links, sizes, times of change and bytes. Not the folder's own
  // line: a folder moved aside and back for a test is the same folder, with all it holds.
  const within = (root: string) => Object.entries(seen(root)).filter(([path]) => path !== root).map(([path, is]) => [path.slice(root.length), is]);
  // The swap undone by the test, where the host's own was not told to undo it: every folder lies where it lay.
  const unswap = ({ at, link, folder }: Swap) => {
    if (!existsSync(`${at}.true`)) return;
    if (link === undefined) renameSync(at, folder!);
    else rmSync(at);
    renameSync(`${at}.true`, at);
  };
  // No byte of the user's, of another thread's or of the app's own, plain or as a read answers it.
  const BYTES = ["the user's own", "outside the folder", "another thread's", "ref: refs/heads", "the device's own", "elsewhere"];
  const heard = (harness: Harness) => {
    const said = JSON.stringify(harness.messages);
    const read = harness.messages.flatMap((message) => (message.type === "result" && "ok" in message.outcome && typeof message.outcome.ok === "string" ? [Buffer.from(message.outcome.ok, "base64").toString()] : []));
    return [said, ...read].filter((text) => BYTES.some((bytes) => text.includes(bytes)));
  };
  const NOT_CHECKED = "is not the folder its host checked: another was at its path as its sandbox was made";

  // One start with *swap* done to it as its helper is started. *watched*: the folders that must be as they were after
  // it. What the host said; and, where it said it was ready, what it then answered a read and a write of the thread's.
  async function swapped(message: HostStart, swap: Swap, watched: string[], asked: Array<[string, Record<string, unknown>]>): Promise<{ said: unknown; answers: unknown[]; harness: Harness }> {
    const before = watched.map(within);
    const harness = upset({ FOLDER_SWAPPED: JSON.stringify(swap) });
    harness.send(message);
    const said = await failed(harness);
    const answers: unknown[] = [];
    if (said.type === "ready") for (const [kind, args] of asked) answers.push(await harness.op(`${kind}-${answers.length}`, kind, args));
    await harness.stop();
    unswap(swap);
    // Nothing of theirs was written, made, moved or removed, and none of it was answered.
    expect(watched.map(within)).toEqual(before);
    expect(heard(harness)).toEqual([]);
    expect(answers).toEqual([]);
    return { said, answers, harness };
  }

  // Every way the copy's path can come to lead to another folder. Each from where the link lies, so that the sandbox
  // binds what it leads to; one by a whole path, which the sandbox refuses by itself.
  const UP = "../../../../home";
  const atCopy: Array<[string, () => Omit<Swap, "back">]> = [
    ["a link to the folder itself", () => ({ at: copy, link: `${UP}/Reports` })],
    ["a link to the folder itself by its whole path", () => ({ at: copy, link: reports })],
    ["a link to another thread's copy", () => ({ at: copy, link: OTHER })],
    ["a link to the folder's history", () => ({ at: copy, link: "../history.git" })],
    ["a link to the user's home", () => ({ at: copy, link: UP })],
    ["a link to the rest of the app's data", () => ({ at: copy, link: "../../../devices" })],
    ["a chain of links to the folder", () => {
      symlinkSync(`${UP}/Reports`, join(place, "threads", "hop"));
      symlinkSync("hop", join(place, "threads", "hop-again"));
      return { at: copy, link: "hop-again" };
    }],
    ["a link where the place keeps its copies", () => {
      mkdirSync(join(place, "decoys"));
      symlinkSync(`${UP}/Reports`, join(place, "decoys", THREAD));
      return { at: join(place, "threads"), link: "decoys" };
    }],
    ["a link at the folder's place", () => {
      mkdirSync(join(data, "history", "decoy", "threads"), { recursive: true });
      symlinkSync(`${UP}/Reports`, join(data, "history", "decoy", "threads", THREAD));
      return { at: place, link: "decoy" };
    }],
    ["another thread's copy renamed in", () => ({ at: copy, folder: other })],
    ["the folder's history renamed in", () => ({ at: copy, folder: join(place, "history.git") })],
  ];
  it.each(atCopy.flatMap(([what, swap]) => [[what, "left there", swap, false], [what, "taken away once the sandbox is up", swap, true]] as const))(
    "starts no helper in a copy's stead for %s, %s: nothing of the user's is read or written, and the start fails by the folder's name",
    async (_what, _how, arrange, back) => {
      const swap = { ...arrange(), back };
      const { said } = await swapped(
        { ...start, folder: copy, at: reports, expect: bound(copy), env: { ...start.env, HOME: home }, tmp: join(data, "tmp", THREAD) }, swap,
        [home, join(data, "devices"), other, join(place, "history.git"), join(place, "clones"), copy],
        // What a thread asks next: a file it reads, and one it writes.
        [["read", { key: `${reports}/a.txt`, max_bytes: null }], ["read", { key: `${reports}/secret.txt`, max_bytes: null }], ["read", { key: `${reports}/HEAD`, max_bytes: null }],
          ["read", { key: `${reports}/credentials.json`, max_bytes: null }], ["write", { key: `${reports}/PLANTED.txt`, data: b64("planted by the thread\n") }]],
      );
      // The helper found another folder where its own should be, and said so before it looked at anything in it; or
      // the sandbox would not bind a link by a whole path. Either way by the folder's name, never the copy's path. And
      // where the copy's path still leads elsewhere, the copy is gone as an operation would find it: the app opens it again.
      expect(said).toEqual({
        type: "failed",
        message: swap.link === reports
          ? expect.stringMatching(/^the file helper exited: bwrap: Can't bind mount /)
          : `the file helper exited: the copy of ${reports} this thread works in ${NOT_CHECKED}\n`,
        ...(back ? {} : { folder: true }),
      });
      expect(JSON.stringify(said)).not.toContain(join(data, "history"));
    },
  );

  // A landing's three folders: the folder it writes, the copy it reads, and where it keeps what it replaces.
  const atLanding: Array<[string, string, () => Omit<Swap, "back">]> = [
    ["the copy it lands from", "the thread's copy a landing in <folder> lands from", () => ({ at: copy, link: OTHER })],
    ["the copy it lands from, a folder renamed in", "the thread's copy a landing in <folder> lands from", () => ({ at: copy, folder: other })],
    ["the copy it lands from, by a link above it", "the thread's copy a landing in <folder> lands from", () => {
      mkdirSync(join(place, "decoys"));
      symlinkSync(`../threads.true/${OTHER}`, join(place, "decoys", THREAD));
      return { at: join(place, "threads"), link: "decoys" };
    }],
    ["the folder it keeps replaced files in", "the folder a landing in <folder> keeps replaced files in", () => ({ at: kept, link: "decoy" })],
    ["the folder it keeps replaced files in, a folder renamed in", "the folder a landing in <folder> keeps replaced files in", () => ({ at: kept, folder: join(data, "landings", "decoy") })],
    ["the folder itself", "the folder <folder>", () => ({ at: reports, link: "Elsewhere" })],
  ];
  it.each(atLanding.flatMap(([what, named, swap]) => [[what, "left there", named, swap, false], [what, "taken away once the sandbox is up", named, swap, true]] as const))(
    "starts no landing's helper with another folder in the stead of %s, %s: what a cut landing left is not put back by a record of another folder's, and nothing lands",
    async (_what, _how, named, arrange, back) => {
      // Another folder's kept files, with the record of a landing cut in this folder: put back by nobody but this folder's own landing.
      const decoy = join(data, "landings", "decoy");
      await cut(decoy);
      mkdirSync(join(decoy, ".forgotten-0b6c1d3e"));
      writeFileSync(join(decoy, ".forgotten-0b6c1d3e", "1"), "kept for another folder\n");
      mkdirSync(join(home, "Elsewhere"));
      writeFileSync(join(home, "Elsewhere", "a.txt"), "elsewhere\n");
      const swap = { ...arrange(), back };
      const theirs = blob("another thread's\n");
      const { said } = await swapped(
        { ...start, folder: reports, expect: bound(reports), landing: { copy, kept }, env: { ...start.env, HOME: home }, tmp: join(data, "tmp", "landing") }, swap,
        [reports, join(home, "Elsewhere"), decoy, other, copy, join(place, "history.git"), join(data, "devices")],
        [["land", { action: "recover" }], ["land", { action: "apply", saga: SAGA, step: 2, path: "sub/b.txt", before: null, after: theirs, expected: "absent" }]],
      );
      expect(said).toEqual({
        type: "failed", message: `the file helper exited: ${named.replace("<folder>", reports)} ${NOT_CHECKED}\n`,
        // The folder itself is gone from its path, as a chat's would be; a copy or a kept folder put back is no reason to say so.
        ...(swap.at === reports && !back ? { folder: true } : {}),
      });
      // The kept folder it made for itself is taken away with the start, where its path still leads to it; the other folder's is as it was.
      const stays = swap.at === kept && !back;
      expect(readdirSync(join(data, "landings"))).toEqual(stays ? [KEY, "decoy"] : ["decoy"]);
      if (stays) expect(readdirSync(kept)).toEqual([]);
    },
  );

  it.skipIf(!enters)("keeps the folder its sandbox bound for its helper's life: a link, or another folder, put at the copy's path once the sandbox is up changes nothing its helper reaches", async () => {
    const harness = onCopy();
    await ready(harness);
    // Nor can anything in the sandbox move it, mount over it, or take its powers to: it has none.
    expect(inSandbox(harness, [{ act: "capabilities" }, { act: "rename", path: copy, to: `${copy}.moved` }])).toEqual([{ ok: "0000000000000000" }, { code: "EBUSY" }]);
    const before = [home, join(data, "devices"), other, join(place, "history.git")].map(within);
    for (const swap of [{ at: copy, link: `${UP}/Reports` }, { at: copy, link: reports }, { at: copy, folder: other }] as Swap[]) {
      // Out here the copy's path leads elsewhere now: to the folder itself, or to another thread's copy.
      renameSync(swap.at, `${swap.at}.true`);
      if (swap.link === undefined) renameSync(swap.folder!, swap.at);
      else symlinkSync(swap.link, swap.at);
      expect(readFileSync(join(copy, "a.txt"), "utf8")).not.toBe("the thread's\n");
      try {
        // In the sandbox it is the folder that was bound: the thread's own copy, read and written.
        expect(inSandbox(harness, [{ act: "read", path: join(copy, "a.txt") }, { act: "write", path: join(copy, "kept.txt"), data: "into the copy that was bound\n" }])).toEqual([{ ok: "the thread's\n" }, { ok: null }]);
        expect(readFileSync(join(`${copy}.true`, "kept.txt"), "utf8")).toBe("into the copy that was bound\n");
        // And the host, which looks from out here, has its helper asked nothing while the path leads elsewhere.
        expect(await harness.op(`w-${swap.link ?? swap.folder}`, "write", { key: `${reports}/late.txt`, data: b64("late\n") })).toEqual(FOLDER_UNAVAILABLE);
      } finally {
        unswap(swap);
      }
      expect([home, join(data, "devices"), other, join(place, "history.git")].map(within)).toEqual(before);
    }
    // The copy back at its path, the host goes on in it.
    expect(await harness.op("r", "read", { key: `${reports}/kept.txt`, max_bytes: null })).toEqual({ ok: b64("into the copy that was bound\n") });
  });

  it("tells a chat's helper, too, which folder its own was when it was checked", async () => {
    const harness = host({ folder: reports, env: { ...start.env, HOME: home }, tmp: join(data, "tmp", "chat") });
    await ready(harness);
    expect(startedWith(harness)).toEqual({ told: { SUROGATE_FOLDER: reports, SUROGATE_FOLDER_IS: is(reports) }, cwd: reports });
  });

  it.each([["left there", false], ["taken away once the sandbox is up", true]])("starts no chat's helper in another folder put at its folder's path, %s", async (_how, back) => {
    mkdirSync(join(home, "Elsewhere"));
    writeFileSync(join(home, "Elsewhere", "a.txt"), "elsewhere\n");
    const { said } = await swapped(
      { ...start, folder: reports, expect: bound(reports), env: { ...start.env, HOME: home }, tmp: join(data, "tmp", "chat") }, { at: reports, link: "Elsewhere", back },
      [reports, join(home, "Elsewhere")],
      [["read", { key: `${reports}/a.txt`, max_bytes: null }], ["write", { key: `${reports}/PLANTED.txt`, data: b64("planted\n") }]],
    );
    // Where the folder at its path is another still, the chat is answered as when its folder is replaced: folder_unavailable.
    expect(said).toEqual({ type: "failed", message: `the file helper exited: the folder ${reports} ${NOT_CHECKED}\n`, ...(back ? {} : { folder: true }) });
  });
});
