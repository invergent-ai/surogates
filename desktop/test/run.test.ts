import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { OUTPUT_CAP_CHARS, pyJsonLength } from "../src/files/answers.js";
import type { HostStart } from "../src/hosts/messages.js";
import { Harness, PACKAGE } from "./host-harness.js";

type Answer = { ok?: { output: string; returncode: number; timed_out: boolean }; error?: { type: string; message: string } };

let base: string;
let folder: string;
let home: string;
let start: HostStart;
let harnesses: Harness[];
let next = 0;
const id = () => `run-${next++}`;

async function host(overrides: Partial<HostStart> = {}, cwd?: string, env?: NodeJS.ProcessEnv): Promise<Harness> {
  const harness = new Harness(cwd, env);
  harnesses.push(harness);
  harness.send({ ...start, ...overrides });
  await harness.until((messages) => messages.find((message) => message.type === "ready"));
  return harness;
}

const run = (harness: Harness, command: string, workdir: string | null = null, timeout = 10) =>
  harness.op(id(), "run", { command, workdir, timeout }) as Promise<Answer>;

// How many processes match: the sandbox's own included, since its pid namespace is a child of ours.
const running = (pattern: string) => Number(spawnSync("pgrep", ["-fc", pattern], { encoding: "utf8" }).stdout.trim() || 0);

async function until(check: () => boolean, timeoutMs = 10_000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (!check()) {
    if (Date.now() > deadline) throw new Error("timed out");
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
}

beforeEach(() => {
  base = realpathSync(mkdtempSync(join(tmpdir(), "run-")));
  folder = join(base, "folder");
  home = join(base, "home");
  mkdirSync(join(folder, "sub"), { recursive: true });
  mkdirSync(home);
  writeFileSync(join(folder, "a.txt"), "alpha\n");
  writeFileSync(join(home, "secret.txt"), "secret\n");
  start = {
    type: "start",
    folder,
    tmp: join(base, "data", "tmp", "root"),
    dataDir: join(base, "data"),
    env: { HOME: home, LANG: "C.UTF-8", PATH: "/usr/bin:/bin" },
    appDirs: [dirname(process.execPath), PACKAGE],
  };
  harnesses = [];
});

afterEach(async () => {
  for (const harness of harnesses) await harness.stop();
  rmSync(base, { recursive: true, force: true });
});

describe("run", { timeout: 30_000 }, () => {
  it("answers a command's output and exit code as the cloud does", async () => {
    const harness = await host();
    expect(await run(harness, "echo out; echo err >&2; exit 3")).toEqual({ ok: { output: "out\n\nerr\n", returncode: 3, timed_out: false } });
    expect(await run(harness, "printf 'a\\000b'")).toEqual({ ok: { output: "a\u0000b", returncode: 0, timed_out: false } });
    expect(await run(harness, "printf '\\377A'")).toEqual({ ok: { output: "�A", returncode: 0, timed_out: false } });
    expect(await run(harness, "a\0b")).toEqual({ ok: { output: "embedded null byte", returncode: -1, timed_out: false } });
  });

  it("keeps the head and the tail of a long output", async () => {
    const harness = await host();
    const answer = await run(harness, "printf START; head -c 600000 /dev/zero | tr '\\000' x; printf END", null, 30);
    const output = answer.ok?.output ?? "";
    expect(output.startsWith("START") && output.endsWith("END")).toBe(true);
    expect(output).toContain("chars omitted by the computer");
    expect(pyJsonLength(output)).toBeLessThan(OUTPUT_CAP_CHARS + 200);
  });

  it("stops a command at its timeout, and everything it started", async () => {
    const harness = await host();
    expect(await run(harness, "sleep 611 & setsid sleep 612 & (sleep 613 &); sleep 614", null, 1)).toEqual({
      ok: { output: "Command timed out after 1 seconds", returncode: 124, timed_out: true },
    });
    await until(() => running("^sleep 61[1-4]$") === 0);
  });

  it("stops a command the session cancels", async () => {
    const harness = await host();
    const opId = id();
    harness.send({ type: "op", id: opId, kind: "run", args: { command: "sleep 615", workdir: null, timeout: 60 } });
    await until(() => running("^sleep 615$") === 1);
    harness.send({ type: "cancel", id: opId });
    await until(() => running("^sleep 615$") === 0);
  });

  it("answers a command killed by a signal as a shell does", async () => {
    const harness = await host();
    expect((await run(harness, "kill -9 $$")).ok?.returncode).toBe(137);
  });

  it("runs in the folder, or in the workdir asked for", async () => {
    const harness = await host();
    expect((await run(harness, "pwd")).ok?.output).toBe(`${folder}\n`);
    expect((await run(harness, "pwd", "sub")).ok?.output).toBe(`${folder}/sub\n`);
    expect((await run(harness, "pwd", "~")).ok?.output).toBe(`${folder}\n`);
    expect((await run(harness, "pwd", "$HOME")).ok?.output).toBe(`${folder}\n`);
  });

  it("refuses a workdir outside the folder, and answers one it cannot enter as the cloud does", async () => {
    const harness = await host();
    expect(await run(harness, "pwd", "/etc")).toEqual({
      error: {
        type: "sandbox",
        message: `Blocked: Path traversal blocked: '/etc' resolves to '/etc' which is outside the workspace '${folder}'. All commands must run within the workspace directory.`,
      },
    });
    expect(await run(harness, "pwd", "nope")).toEqual({
      ok: { output: `[Errno 2] No such file or directory: '${folder}/nope'`, returncode: -1, timed_out: false },
    });
    expect(await run(harness, "pwd", "a.txt")).toEqual({
      ok: { output: `[Errno 20] Not a directory: '${folder}/a.txt'`, returncode: -1, timed_out: false },
    });
    expect(await run(harness, "pwd", "a\0b")).toEqual({ error: { type: "value", message: "embedded null byte" } });
  });

  it("writes the folder and nowhere else", async () => {
    mkdirSync(join(home, ".nvm"));
    const harness = await host();
    expect((await run(harness, "echo hi > made.txt && cat made.txt")).ok?.output).toBe("hi\n");
    expect(readFileSync(join(folder, "made.txt"), "utf8")).toBe("hi\n");
    // The sandbox's /tmp is its own: a write there lands nowhere.
    expect((await run(harness, `echo x > '${base}/outside.txt'`)).ok?.returncode).toBe(0);
    expect(existsSync(join(base, "outside.txt"))).toBe(false);
    // A folder it may read is read-only.
    expect((await run(harness, 'echo x > "$HOME/.nvm/x"')).ok?.output).toMatch(/Read-only file system/);
    expect(existsSync(join(home, ".nvm", "x"))).toBe(false);
  });

  it("hides the home folder and srt's shared temp folder", async () => {
    const harness = await host();
    expect((await run(harness, 'cat "$HOME/secret.txt"')).ok?.output).toMatch(/No such file or directory/);
    // test/srt-tmp.ts makes /tmp/claude for the run when it is absent.
    const marker = join("/tmp/claude", `run-test-${process.pid}`);
    writeFileSync(marker, "x");
    try {
      expect((await run(harness, "ls -A /tmp/claude 2>/dev/null | wc -l")).ok?.output.trim()).toBe("0");
    } finally {
      rmSync(marker, { force: true });
    }
  });

  it("keeps /tmp/claude hidden when a name in the folder holds srt's anchor", async () => {
    const steered = join(folder, "q --dev /dev --unshare-pid ");
    mkdirSync(join(steered, "deeper"), { recursive: true });
    writeFileSync(join(steered, ".bashrc"), "");
    writeFileSync(join(steered, "deeper", ".bashrc"), "");
    const harness = await host();
    const marker = join("/tmp/claude", `run-test-steered-${process.pid}`);
    writeFileSync(marker, "x");
    try {
      expect(await run(harness, "ls -A /tmp/claude 2>/dev/null | wc -l")).toEqual({ ok: { output: "0\n", returncode: 0, timed_out: false } });
    } finally {
      rmSync(marker, { force: true });
    }
  });

  it("runs with the app's environment only, and the session's caches", async () => {
    const harness = await host({ env: { ...start.env, SECRET: "s", BASH_ENV: "/nonexistent", LD_PRELOAD: "/nonexistent.so" } });
    const lines = ((await run(harness, "env")).ok?.output ?? "").split("\n");
    expect(lines.filter((line) => /^(SECRET|BASH_ENV|LD_PRELOAD)=/.test(line))).toEqual([]);
    expect(lines).toContain(`HOME=${home}`);
    expect(lines).toContain(`XDG_CACHE_HOME=${start.tmp}/cache`);
  });

  it("refuses a network host that is not a package host", async () => {
    const harness = await host();
    const answer = await run(harness, "curl -sS -o /dev/null https://example.com 2>&1; echo \"exit $?\"", null, 20);
    expect(answer.ok?.output).toMatch(/403/);
  });

  it("leaves the folder as it was after commands, one after another and at once", async () => {
    mkdirSync(join(folder, ".git"));
    const before = readdirSync(folder).sort();
    const gitBefore = readdirSync(join(folder, ".git")).sort();
    const harness = await host({}, folder);
    for (let i = 0; i < 3; i += 1) expect((await run(harness, "true")).ok?.returncode).toBe(0);
    for (let round = 0; round < 5; round += 1) {
      const together = await Promise.all([run(harness, "sleep 0.3"), run(harness, "true"), run(harness, "sleep 0.1"), run(harness, "true")]);
      expect(together.map((answer) => (answer.ok?.returncode === 0 ? 0 : JSON.stringify(answer)))).toEqual([0, 0, 0, 0]);
    }
    expect((await run(harness, "sleep 5", null, 1)).ok?.timed_out).toBe(true);
    const cancelled = id();
    harness.send({ type: "op", id: cancelled, kind: "run", args: { command: "sleep 617", workdir: null, timeout: 60 } });
    await until(() => running("^sleep 617$") === 1);
    harness.send({ type: "cancel", id: cancelled });
    await harness.until((messages) => messages.find((message) => message.type === "result" && message.id === cancelled));
    expect(readdirSync(folder).sort()).toEqual(before);
    expect(readdirSync(join(folder, ".git")).sort()).toEqual(gitBefore);
  });

  it("protects the folder's code-running names inside a command", async () => {
    mkdirSync(join(folder, ".git"));
    writeFileSync(join(folder, ".git", "config"), "[core]\n");
    const harness = await host();
    const opId = id();
    harness.send({ type: "op", id: opId, kind: "run", args: { command: "sleep 2", workdir: null, timeout: 10 } });
    // While it runs, srt's placeholders cover the names that are missing: the
    // command was wrapped from the folder.
    await until(() => existsSync(join(folder, ".bashrc")));
    await harness.until((messages) => messages.find((message) => message.type === "result" && message.id === opId));
    // A name that is missing is covered by srt with /dev/null, on a mount that allows no
    // devices: the kernel refuses the write as a permission, not as a read-only file system.
    expect((await run(harness, "echo x > .bashrc")).ok?.output).toMatch(/Read-only file system|Permission denied/);
    expect((await run(harness, "echo y >> .git/config")).ok?.output).toMatch(/Read-only file system/);
    expect(readFileSync(join(folder, ".git", "config"), "utf8")).toBe("[core]\n");
    expect(existsSync(join(folder, ".bashrc"))).toBe(false);
  });

  it("keeps a nested repo's config protected after a command writes an ignore file", async () => {
    mkdirSync(join(folder, "sub", ".git"), { recursive: true });
    writeFileSync(join(folder, "sub", ".git", "config"), "[core]\n");
    const harness = await host();
    expect((await run(harness, "printf 'sub\\n' > .ignore")).ok?.returncode).toBe(0);
    expect((await run(harness, "echo y >> sub/.git/config")).ok?.output).toMatch(/Read-only file system/);
    expect(readFileSync(join(folder, "sub", ".git", "config"), "utf8")).toBe("[core]\n");
  });

  it("never runs a program from the folder outside the sandbox", async () => {
    const proof = join(base, "ran-outside");
    // Anything in the folder may be the agent's. srt looks up which and rg through the
    // host's own PATH, from the folder: neither a folder entry nor a relative one may count.
    mkdirSync(join(folder, "bin"));
    for (const name of ["which", "rg"]) {
      writeFileSync(join(folder, "bin", name), `#!/bin/sh\ntouch '${proof}'\nexec /usr/bin/${name} "$@"\n`, { mode: 0o755 });
    }
    const harness = await host({}, folder, { ...process.env, PATH: `${folder}/bin:bin:${process.env.PATH ?? ""}` });
    expect((await run(harness, "true")).ok?.returncode).toBe(0);
    expect(existsSync(proof)).toBe(false);
  });

  it("never reads the user's shell startup files outside the sandbox", async () => {
    // srt's outer bash runs out here. With a socket for stdin, as the helper's is, bash
    // takes itself for a remote shell and reads ~/.bashrc; its output would also spoil
    // the helper's handshake.
    const marker = join(base, "bashrc-ran");
    writeFileSync(join(home, ".bashrc"), `echo Welcome to my shell\ntouch '${marker}'\n`);
    const harness = await host();
    expect((await run(harness, "true")).ok?.returncode).toBe(0);
    expect(existsSync(marker)).toBe(false);
  });

  it("stops its commands when it stops", async () => {
    const harness = await host();
    harness.send({ type: "op", id: id(), kind: "run", args: { command: "sleep 616", workdir: null, timeout: 60 } });
    await until(() => running("^sleep 616$") === 1);
    await harness.stop();
    expect(await harness.exited).toBe(0);
    await until(() => running("^sleep 616$") === 0);
  });

  it("answers a command too long for srt with its message", async () => {
    const harness = await host();
    const answer = await run(harness, `true ${"x".repeat(200_000)}`);
    expect(answer.ok?.returncode).toBe(-1);
    expect(answer.ok?.output).not.toBe("");
  });
});
