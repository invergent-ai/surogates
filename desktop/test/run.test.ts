import { execFileSync, spawnSync } from "node:child_process";
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, realpathSync, rmSync, statSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { OUTPUT_CAP_CHARS, pyJsonLength } from "../src/files/answers.js";
import { HOOKS_NOTICE } from "../src/hosts/hooks.js";
import type { HostStart } from "../src/hosts/messages.js";
import { bound, Harness, PACKAGE } from "./host-harness.js";

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

// The folder srt keeps its sockets in for this test's folder.
const srtTmp = () => {
  const { dev, ino } = statSync(folder);
  return join(start.dataDir, "srt", `${dev}-${ino}`);
};
const sockets = () => readdirSync(srtTmp()).filter((name) => name.endsWith(".sock"));
// The processes whose command line names srt's folder: its socat bridges.
const bridges = () => spawnSync("pgrep", ["-f", `${srtTmp()}/`], { encoding: "utf8" }).stdout.split("\n").filter(Boolean).map(Number);
const alive = (pid: number) => {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
};

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
    expect: bound(folder),
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

  it("keeps a nested repo's config protected deeper than srt's default three levels", async () => {
    mkdirSync(join(folder, "a", "b", "c", "d", ".git"), { recursive: true });
    writeFileSync(join(folder, "a", "b", "c", "d", ".git", "config"), "[core]\n");
    const harness = await host();
    expect((await run(harness, "echo y >> a/b/c/d/.git/config")).ok?.output).toMatch(/Read-only file system/);
    expect(readFileSync(join(folder, "a", "b", "c", "d", ".git", "config"), "utf8")).toBe("[core]\n");
  });

  it("makes the hooks a command adds non-executable, and says so", async () => {
    const harness = await host();
    const answer = await run(
      harness,
      "git -c init.defaultBranch=main init -q && printf '#!/bin/sh\\n' > .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit && echo made",
    );
    // git may warn first that it cannot read the hidden home folder's config.
    const notice = HOOKS_NOTICE.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    expect(answer.ok?.output).toMatch(new RegExp(`made\\n\\n${notice}\\.git/hooks/pre-commit$`));
    expect(statSync(join(folder, ".git", "hooks", "pre-commit")).mode & 0o111).toBe(0);
    // git's samples never run, and stay as git made them.
    expect(statSync(join(folder, ".git", "hooks", "pre-commit.sample")).mode & 0o111).not.toBe(0);
  });

  it("makes the target of a hook linked into the session's temp folder non-executable", async () => {
    const harness = await host();
    const answer = await run(
      harness,
      "printf '#!/bin/sh\\n' > \"$TMPDIR/h\" && chmod +x \"$TMPDIR/h\" && mkdir -p .git/hooks && ln -s \"$TMPDIR/h\" .git/hooks/pre-commit",
    );
    expect(answer.ok?.output).toBe(`${HOOKS_NOTICE}.git/hooks/pre-commit`);
    expect((await run(harness, "test -x \"$TMPDIR/h\" || echo not")).ok?.output).toBe("not\n");
  });

  it("leaves the user's own hooks as they are", async () => {
    mkdirSync(join(folder, ".git", "hooks"), { recursive: true });
    writeFileSync(join(folder, ".git", "hooks", "pre-commit"), "#!/bin/sh\n", { mode: 0o755 });
    const harness = await host();
    expect((await run(harness, "true")).ok?.output).toBe("");
    expect(statSync(join(folder, ".git", "hooks", "pre-commit")).mode & 0o111).not.toBe(0);
  });

  it.skipIf(process.getuid?.() === 0)("refuses commands while a folder in it cannot be read", async () => {
    mkdirSync(join(folder, "locked"));
    chmodSync(join(folder, "locked"), 0);
    try {
      const harness = await host();
      expect(await run(harness, "echo hi")).toEqual({ error: { type: "sandbox", message: expect.stringContaining("locked") } });
    } finally {
      chmodSync(join(folder, "locked"), 0o755);
    }
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

  it("removes what srt left when the host before it was killed, and nothing of the user's", async () => {
    // The user's own: empty, and empty and read-only like srt's, and a folder with something in it.
    writeFileSync(join(folder, ".profile"), "");
    writeFileSync(join(folder, ".zshrc"), "", { mode: 0o444 });
    mkdirSync(join(folder, ".vscode"));
    writeFileSync(join(folder, ".vscode", "settings.json"), "{}");
    mkdirSync(join(folder, ".git"));
    const before = readdirSync(folder).sort();
    const first = await host();
    first.send({ type: "op", id: id(), kind: "run", args: { command: "sleep 621", workdir: null, timeout: 900 } });
    await until(() => existsSync(join(folder, ".bashrc")));
    first.killGroup();
    await first.exited;
    expect(readdirSync(folder).sort()).not.toEqual(before);
    await host();
    // What this recovery leaves. srt 0.0.77 itself removes an empty, write-protected,
    // single-link file under its protected names once any command ends, so the user's
    // empty 0444 .zshrc goes after the next command, whatever the recovery does.
    expect(readdirSync(folder).sort()).toEqual(before);
    expect(readdirSync(join(folder, ".git"))).toEqual([]);
  });

  it("makes the hooks a killed host's command left non-executable before the next host runs a command, and keeps the user's own", async () => {
    mkdirSync(join(folder, ".git", "hooks"), { recursive: true });
    writeFileSync(join(folder, ".git", "hooks", "pre-push"), "#!/bin/sh\n", { mode: 0o755 });
    const first = await host();
    first.send({
      type: "op", id: id(), kind: "run", args: {
        command: "git -c init.defaultBranch=main init -q sub && printf '#!/bin/sh\\n' > sub/.git/hooks/pre-commit && chmod +x sub/.git/hooks/pre-commit && sleep 623",
        workdir: null, timeout: 900,
      },
    });
    await until(() => running("^sleep 623$") === 1);
    first.killGroup();
    await first.exited;
    const second = await host();
    // Seen from inside the first command: it was made non-executable before that command ran.
    expect((await run(second, "test -x sub/.git/hooks/pre-commit || echo not")).ok?.output).toBe("not\n");
    expect(statSync(join(folder, ".git", "hooks", "pre-push")).mode & 0o111).not.toBe(0);
  });

  it.skipIf(process.getuid?.() === 0)("keeps the record of a host that stopped without seeing the whole folder", async () => {
    const first = await host();
    try {
      expect((await run(
        first,
        "git -c init.defaultBranch=main init -q sub && printf '#!/bin/sh\\n' > sub/.git/hooks/pre-commit && chmod +x sub/.git/hooks/pre-commit && chmod 0 sub",
      )).ok?.returncode).toBe(0);
      await first.stop();
      // Stopped, not killed by the harness: the record stays because of the look.
      expect(await first.exited).toBe(0);
    } finally {
      // The user does what the refusal asked, and a new host starts. Also lets afterEach remove the folder.
      chmodSync(join(folder, "sub"), 0o755);
    }
    const second = await host();
    expect((await run(second, "test -x sub/.git/hooks/pre-commit || echo not")).ok?.output).toBe("not\n");
  });

  it("does not touch the folder after a host that stopped cleanly", async () => {
    const first = await host();
    await first.stop();
    expect(await first.exited).toBe(0);
    // Made after, by the user: empty and read-only, like srt's, but no host was killed.
    writeFileSync(join(folder, ".bash_profile"), "", { mode: 0o444 });
    await host();
    expect(existsSync(join(folder, ".bash_profile"))).toBe(true);
  });

  it("answers a second host for the same folder, however it is spelled, that another chat has it", async () => {
    await host();
    symlinkSync(folder, join(base, "link"));
    const second = new Harness();
    harnesses.push(second);
    second.send({ ...start, folder: join(base, "link"), tmp: join(base, "data", "tmp", "other") });
    const said = await second.until(
      (messages) => messages.find((message) => message.type === "failed" || message.type === "ready"), 20_000,
    );
    expect(said).toEqual({ type: "failed", message: expect.stringMatching(/another chat on this computer is working in this folder/) });
    expect(await second.exited).toBe(1);
  }, 30_000);

  it("keeps srt's sockets in the app's data, and a new host ends the bridges a killed one left", async () => {
    const first = await host();
    const before = sockets();
    expect(before.length).toBeGreaterThan(0);
    const left = bridges();
    expect(left.length).toBeGreaterThan(0);
    // Killed alone, as when the app dies with it: its sandbox goes with it
    // (die-with-parent), its socat bridges, outside the sandbox, go on.
    process.kill(first.child.pid ?? 0, "SIGKILL");
    await first.exited;
    expect(left.some(alive)).toBe(true);
    await host();
    await until(() => left.filter(alive).length === 0);
    expect(sockets().filter((name) => before.includes(name))).toEqual([]);
  });

  it("leaves no socket behind when its helper dies", async () => {
    const harness = await host();
    expect(sockets().length).toBeGreaterThan(0);
    execFileSync("pkill", ["-KILL", "-P", String(harness.child.pid)]);
    expect(await harness.exited).toBe(1);
    expect(sockets()).toEqual([]);
  });

  it("leaves a command its helper's death stopped unanswered, for the app to answer as interrupted", async () => {
    const harness = await host();
    const opId = id();
    harness.send({ type: "op", id: opId, kind: "run", args: { command: "sleep 627", workdir: null, timeout: 60 } });
    await until(() => running("^sleep 627$") === 1);
    // The helper alone: the command, another of the host's children, runs on until the host stops it.
    execFileSync("pkill", ["-KILL", "-P", String(harness.child.pid), "-f", "files/helper.js"]);
    expect(await harness.exited).toBe(1);
    // Every message the host sent has been read once its channel is closed.
    await until(() => !harness.child.connected);
    expect(harness.messages.filter((message) => message.type === "result" && message.id === opId)).toEqual([]);
    await until(() => running("^sleep 627$") === 0);
  });

  it("stops cleanly when the app's channel closes during a command", async () => {
    const harness = await host();
    harness.send({ type: "op", id: id(), kind: "run", args: { command: "sleep 629", workdir: null, timeout: 60 } });
    await until(() => running("^sleep 629$") === 1);
    harness.child.disconnect();
    const code = await harness.exited;
    const { dev, ino } = statSync(folder);
    const record = JSON.parse(readFileSync(join(start.dataDir, "folders", `${dev}-${ino}.json`), "utf8")) as { state: string };
    // Its final look and srt's reset ran: the way out every stop takes.
    expect({ code, sockets: sockets(), state: record.state }).toEqual({ code: 0, sockets: [], state: "stopped" });
    await until(() => running("^sleep 629$") === 0);
  });

  it("says so when the app's data folder's path is too long for srt's sockets", async () => {
    const harness = new Harness();
    harnesses.push(harness);
    harness.send({ ...start, dataDir: join(base, "d".repeat(90)), tmp: join(base, "d".repeat(90), "tmp", "root") });
    const said = await harness.until((messages) => messages.find((message) => message.type === "failed" || message.type === "ready"));
    expect(said).toEqual({ type: "failed", message: expect.stringContaining("too long for the sandbox's sockets") });
    expect(await harness.exited).toBe(1);
  });
});

// The main cases again, with the root's commands in its session runner: a
// background process has started it, and every later command runs in it.
describe("run, in the session runner", { timeout: 30_000 }, () => {
  async function inRunner(overrides: Partial<HostStart> = {}): Promise<Harness> {
    const harness = await host(overrides);
    const started = await harness.op(id(), "start", {
      command: "sleep 680", workdir: null, task_id: "t", pty: false, notify_on_complete: false, watcher_interval: null,
    });
    expect(started).toMatchObject({ ok: { session_id: expect.any(String) } });
    // Only the runner marks a command: this pass is not the per-call wrap again.
    expect((await run(harness, "env")).ok?.output).toMatch(/^SUROGATE_PROCESS=/m);
    return harness;
  }

  it("answers a command's output and exit code as the cloud does", async () => {
    const harness = await inRunner();
    expect(await run(harness, "echo out; echo err >&2; exit 3")).toEqual({ ok: { output: "out\n\nerr\n", returncode: 3, timed_out: false } });
    expect(await run(harness, "printf 'a\\000b'")).toEqual({ ok: { output: "a\u0000b", returncode: 0, timed_out: false } });
    expect(await run(harness, "printf '\\377A'")).toEqual({ ok: { output: "�A", returncode: 0, timed_out: false } });
    expect(await run(harness, "a\0b")).toEqual({ ok: { output: "embedded null byte", returncode: -1, timed_out: false } });
    expect((await run(harness, "kill -9 $$")).ok?.returncode).toBe(137);
  });

  it("keeps the head and the tail of a long output", async () => {
    const harness = await inRunner();
    const output = (await run(harness, "printf START; head -c 600000 /dev/zero | tr '\\000' x; printf END", null, 30)).ok?.output ?? "";
    expect(output.startsWith("START") && output.endsWith("END")).toBe(true);
    expect(pyJsonLength(output)).toBeLessThan(OUTPUT_CAP_CHARS + 200);
  });

  it("stops a command at its timeout, and everything it started", async () => {
    const harness = await inRunner();
    expect(await run(harness, "sleep 681 & setsid sleep 682 & (sleep 683 &); sleep 684", null, 1)).toEqual({
      ok: { output: "Command timed out after 1 seconds", returncode: 124, timed_out: true },
    });
    await until(() => running("^sleep 68[1-4]$") === 0);
  });

  it("stops a command the session cancels, and everything it started", async () => {
    const harness = await inRunner();
    const opId = id();
    harness.send({ type: "op", id: opId, kind: "run", args: { command: "setsid sleep 685 & sleep 686", workdir: null, timeout: 60 } });
    await until(() => running("^sleep 68[56]$") === 2);
    harness.send({ type: "cancel", id: opId });
    await until(() => running("^sleep 68[56]$") === 0);
  });

  it("ends what a command leaves running when it ends", async () => {
    const harness = await inRunner();
    expect(await run(harness, "sleep 687 & echo started")).toEqual({ ok: { output: "started\n", returncode: 0, timed_out: false } });
    await until(() => running("^sleep 687$") === 0);
  });

  it("runs in the folder or the workdir asked for, and refuses one outside it", async () => {
    const harness = await inRunner();
    expect((await run(harness, "pwd")).ok?.output).toBe(`${folder}\n`);
    expect((await run(harness, "pwd", "sub")).ok?.output).toBe(`${folder}/sub\n`);
    expect((await run(harness, "pwd", "~")).ok?.output).toBe(`${folder}\n`);
    expect(await run(harness, "pwd", "/etc")).toMatchObject({ error: { type: "sandbox" } });
    expect(await run(harness, "pwd", "nope")).toEqual({
      ok: { output: `[Errno 2] No such file or directory: '${folder}/nope'`, returncode: -1, timed_out: false },
    });
  });

  it("writes the folder and nowhere else, with the app's environment and the package hosts only", async () => {
    const harness = await inRunner({ env: { ...start.env, SECRET: "s" } });
    expect((await run(harness, "echo hi > made.txt && cat made.txt")).ok?.output).toBe("hi\n");
    expect((await run(harness, `echo x > '${base}/outside.txt'`)).ok?.returncode).toBe(0);
    expect(existsSync(join(base, "outside.txt"))).toBe(false);
    expect((await run(harness, 'cat "$HOME/secret.txt"')).ok?.output).toMatch(/No such file or directory/);
    const lines = ((await run(harness, "env")).ok?.output ?? "").split("\n");
    expect(lines.filter((line) => /^(SECRET|ELECTRON_RUN_AS_NODE|SUROGATE_FOLDER)=/.test(line))).toEqual([]);
    expect(lines).toContain(`XDG_CACHE_HOME=${start.tmp}/cache`);
    const refused = await run(harness, "curl -sS -o /dev/null https://example.com 2>&1; echo \"exit $?\"", null, 20);
    expect(refused.ok?.output).toMatch(/403/);
  });

  it("protects the folder's code-running names inside a command", async () => {
    mkdirSync(join(folder, ".git"));
    writeFileSync(join(folder, ".git", "config"), "[core]\n");
    const harness = await inRunner();
    expect((await run(harness, "echo x > .bashrc")).ok?.output).toMatch(/Read-only file system|Permission denied/);
    expect((await run(harness, "echo y >> .git/config")).ok?.output).toMatch(/Read-only file system/);
    expect(readFileSync(join(folder, ".git", "config"), "utf8")).toBe("[core]\n");
    // srt's placeholder, there while the runner lives, and still empty.
    expect(readFileSync(join(folder, ".bashrc"), "utf8")).toBe("");
  });

  it("hides the home folder and srt's shared temp folder", async () => {
    const harness = await inRunner();
    expect((await run(harness, 'cat "$HOME/secret.txt"')).ok?.output).toMatch(/No such file or directory/);
    const marker = join("/tmp/claude", `run-test-runner-${process.pid}`);
    writeFileSync(marker, "x");
    try {
      expect((await run(harness, "ls -A /tmp/claude 2>/dev/null | wc -l")).ok?.output.trim()).toBe("0");
    } finally {
      rmSync(marker, { force: true });
    }
  });

  it("answers commands that come at once, all in the one runner", async () => {
    const harness = await inRunner();
    const together = await Promise.all([0, 1, 2, 3].map(() => run(harness, "readlink /proc/self/ns/net")));
    expect(together.map((answer) => answer.ok?.returncode)).toEqual([0, 0, 0, 0]);
    // One network namespace: one runner.
    expect(new Set(together.map((answer) => answer.ok?.output)).size).toBe(1);
  });

  it("makes the hooks a command adds non-executable, and says so", async () => {
    const harness = await inRunner();
    const answer = await run(
      harness,
      "git -c init.defaultBranch=main init -q && printf '#!/bin/sh\\n' > .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit && echo made",
    );
    expect(answer.ok?.output.endsWith(`made\n\n${HOOKS_NOTICE}.git/hooks/pre-commit`)).toBe(true);
    expect(statSync(join(folder, ".git", "hooks", "pre-commit")).mode & 0o111).toBe(0);
  });
});
