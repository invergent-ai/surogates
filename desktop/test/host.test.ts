import { execFileSync, spawnSync } from "node:child_process";
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
import { FINISHED_TTL_SECONDS } from "../src/guest/processes.js";
import { readRecord, writeRecord } from "../src/hosts/folder-record.js";
import { FOLDER_UNAVAILABLE, type HostStart } from "../src/hosts/messages.js";
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

  it("runs no command: the kinds that would are its helper's to refuse, as kinds it does not do", async () => {
    const harness = host();
    await ready(harness);
    const proof = join(base, "ran");
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
    expect(said).toEqual({ type: "failed", message: expect.stringMatching(/another chat on this computer is working in this folder/) });
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
