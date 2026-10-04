import { spawnSync } from "node:child_process";
import {
  existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, realpathSync, renameSync, rmSync, writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import type { HostStart } from "../src/hosts/messages.js";
import { RESTARTED, restartNotice } from "../src/hosts/processes.js";
import { appeared, GRANT_CHANGED, MAX_EXTRA_DENIES } from "../src/hosts/restarts.js";
import { CANCELLED, SANDBOX_STOPPED } from "../src/hosts/run.js";
import { Harness, PACKAGE } from "./host-harness.js";

type Answer = { ok?: any; error?: { type: string; message: string } };

let base: string;
let folder: string;
let harnesses: Harness[];
let next = 0;
const id = () => `rr-${next++}`;

async function host(): Promise<Harness> {
  const harness = new Harness();
  harnesses.push(harness);
  const start: HostStart = {
    type: "start",
    folder,
    tmp: join(base, "data", "tmp", "root"),
    dataDir: join(base, "data"),
    env: { HOME: join(base, "home"), LANG: "C.UTF-8", PATH: "/usr/bin:/bin" },
    appDirs: [dirname(process.execPath), PACKAGE],
  };
  harness.send(start);
  await harness.until((messages) => messages.find((message) => message.type === "ready"));
  return harness;
}

const ask = (harness: Harness, kind: string, args: Record<string, unknown>) => harness.op(id(), kind, args) as Promise<Answer>;
const begin = (harness: Harness, command: string) => ask(harness, "start", { command, workdir: null, task_id: "t", pty: false });
const run = (harness: Harness, command: string) => ask(harness, "run", { command, workdir: null, timeout: 10 });
const poll = async (harness: Harness, session_id: string) => (await ask(harness, "poll", { session_id })).ok;
const tryWrite = (path: string) => `if { printf x >> ${path}; } 2>/dev/null; then echo written; else echo denied; fi`;
const tryCreate = (path: string) => `if { : > ${path}; } 2>/dev/null; then echo written; else echo denied; fi`;
const GIT_INIT = "git -c init.defaultBranch=main init -q";

async function until(check: () => boolean | Promise<boolean>, timeoutMs = 20_000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (!(await check())) {
    if (Date.now() > deadline) throw new Error("timed out");
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
}

beforeEach(() => {
  base = realpathSync(mkdtempSync(join(tmpdir(), "runner-restarts-")));
  folder = join(base, "folder");
  mkdirSync(folder);
  mkdirSync(join(base, "home"));
  harnesses = [];
});

afterEach(async () => {
  for (const harness of harnesses) await harness.stop();
  rmSync(base, { recursive: true, force: true });
});

describe("restarting a session runner", { timeout: 40_000 }, () => {
  it("restarts when a background process makes a repository: the next command cannot write its config or hooks, and is told once", async () => {
    const harness = await host();
    const first = (await begin(harness, `sleep 0.5; ${GIT_INIT} .; sleep 633`)).ok.session_id as string;
    // The timed look finds the repository.
    await until(async () => (await poll(harness, first)).status === "exited");
    expect(await poll(harness, first)).toMatchObject({ exit_code: null, note: RESTARTED });
    expect((await run(harness, `${tryWrite(".git/config")}; ${tryCreate(".git/hooks/x")}`)).ok?.output)
      .toBe(`denied\ndenied\n\n${restartNotice(appeared(".git/config"))}`);
    expect((await run(harness, "echo again")).ok?.output).toBe("again\n");
  });

  it("restarts without a word when no background process is alive", async () => {
    const harness = await host();
    const session_id = (await begin(harness, "true")).ok.session_id as string;
    await ask(harness, "wait", { session_id, timeout: 10 });
    // The look after this command restarts the runner.
    expect((await run(harness, `${GIT_INIT} . && echo made`)).ok?.output).toBe("made\n");
    expect((await run(harness, tryWrite(".git/config"))).ok?.output).toBe("denied\n");
    expect((await ask(harness, "list_processes", { task_id: "t" })).ok[0]).not.toHaveProperty("note");
  });

  it("lets a run in flight finish when a timed look sees a repository it made, and restarts after it", async () => {
    const harness = await host();
    const session_id = (await begin(harness, "true")).ok.session_id as string;
    await ask(harness, "wait", { session_id, timeout: 10 });
    // A timed look comes while it sleeps, and sees sub/.git.
    expect(await run(harness, `${GIT_INIT} sub && sleep 7 && echo done`)).toMatchObject({ ok: { output: "done\n", returncode: 0 } });
    expect((await run(harness, tryWrite("sub/.git/config"))).ok?.output).toBe("denied\n");
  });

  it("answers a run and a start cancelled while they wait for a restart at once, before the restart ends", async () => {
    // 300 000 files: each walk of the folder takes about a second, and a restart does two.
    mkdirSync(join(folder, "many"));
    spawnSync("bash", ["-c", "seq 1 300000 | xargs touch"], { cwd: join(folder, "many") });
    const harness = await host();
    await begin(harness, "sleep 626");
    // The look after this command starts the restart before its answer is sent.
    expect((await run(harness, "mkdir -p sub/.vscode && touch sub/.vscode/a && echo made")).ok?.output).toBe("made\n");
    const order: string[] = [];
    const at: Record<string, number> = {};
    const answered = (name: string) => (answer: unknown) => {
      order.push(name);
      at[name] = Date.now();
      return answer;
    };
    const [queuedRun, queuedStart] = [id(), id()];
    const cancelled = harness.op(queuedRun, "run", { command: "echo never", workdir: null, timeout: 10 }).then(answered("run"));
    const cancelledStart = harness.op(queuedStart, "start", { command: "true", workdir: null, task_id: "t", pty: false })
      .then(answered("cancelled start"));
    // A start answers once the new runner is up, so once the restart has ended.
    const started = begin(harness, "sleep 606").then(answered("start"));
    // Once they wait for the restart, which takes seconds.
    await new Promise((resolve) => setTimeout(resolve, 300));
    harness.send({ type: "cancel", id: queuedRun });
    harness.send({ type: "cancel", id: queuedStart });
    expect(await cancelled).toEqual(CANCELLED);
    expect(await cancelledStart).toEqual(CANCELLED);
    await started;
    expect(order).toEqual(["run", "cancelled start", "start"]);
    // Not at the restart's end: a walk of the folder and more were still to come.
    expect((at.start ?? 0) - (at["cancelled start"] ?? 0)).toBeGreaterThan(500);
  });

  it("does not restart for a protected name under node_modules", async () => {
    const harness = await host();
    const session_id = (await begin(harness, "sleep 634")).ok.session_id as string;
    expect((await run(harness, "mkdir -p node_modules/pkg/.vscode && touch node_modules/pkg/.vscode/settings.json && echo made")).ok?.output)
      .toBe("made\n");
    expect((await run(harness, "echo next")).ok?.output).toBe("next\n");
    expect((await poll(harness, session_id)).status).toBe("running");
  });

  it("runs commands and starts that come during a restart in the new runner, and tells the command", async () => {
    const harness = await host();
    const old = (await begin(harness, "sleep 635")).ok.session_id as string;
    // The look after this command starts the restart before its answer is sent.
    expect((await run(harness, "mkdir -p sub/.vscode && touch sub/.vscode/a && echo made")).ok?.output).toBe("made\n");
    const [after, started] = await Promise.all([
      run(harness, "readlink /proc/self/ns/net"),
      begin(harness, "readlink /proc/self/ns/net; sleep 636"),
    ]);
    const [namespace, ...rest] = (after.ok?.output as string).split("\n");
    expect(namespace).toMatch(/^net:\[\d+\]$/);
    // The old runner is gone: its process ended with it. A namespace's number is reused once it is freed.
    expect(await poll(harness, old)).toMatchObject({ status: "exited", note: RESTARTED });
    expect(rest.join("\n")).toBe(`\n${restartNotice(appeared("sub/.vscode"))}`);
    const session_id = started.ok.session_id as string;
    await until(async () => (await poll(harness, session_id)).output_preview !== "");
    expect((await poll(harness, session_id)).output_preview).toBe(`${namespace}\n`);
  });

  it("protects a repository a command makes deeper than srt's scan, once the runner has restarted", async () => {
    const harness = await host();
    await begin(harness, "sleep 640");
    const deep = Array.from({ length: 12 }, (_, i) => `n${i + 1}`).join("/");
    expect((await run(harness, `mkdir -p ${deep} && ${GIT_INIT} ${deep} && echo made`)).ok?.output).toBe("made\n");
    expect((await run(harness, tryWrite(`${deep}/.git/config`))).ok?.output)
      .toBe(`denied\n\n${restartNotice(appeared(`${deep}/.git/config`))}`);
  });

  it("restarts an idle runner when a repository is made outside the app, before the next command runs", async () => {
    const harness = await host();
    const session_id = (await begin(harness, "true")).ok.session_id as string;
    await ask(harness, "wait", { session_id, timeout: 10 });
    // Past the one look that follows the last process's end: from here only the runner keeps them coming.
    await new Promise((resolve) => setTimeout(resolve, 6_000));
    // Made outside the app: no command of this chat runs, and no process lives.
    spawnSync("git", ["-c", "init.defaultBranch=main", "init", "-q", folder]);
    // The runner's next timed look sees it.
    await new Promise((resolve) => setTimeout(resolve, 6_500));
    expect((await run(harness, tryWrite(".git/config"))).ok?.output).toBe("denied\n");
  });

  it("restarts when a protected file is replaced from outside, as git writes its config, and the new runner denies it", async () => {
    spawnSync("git", ["-c", "init.defaultBranch=main", "init", "-q", folder]);
    const harness = await host();
    const session_id = (await begin(harness, "sleep 607")).ok.session_id as string;
    expect((await run(harness, tryWrite(".git/config"))).ok?.output).toBe("denied\n");
    // A new file renamed over the old one, as git and editors save: the runner's mount held the old one.
    const config = join(folder, ".git", "config");
    writeFileSync(join(base, "config.new"), readFileSync(config));
    renameSync(join(base, "config.new"), config);
    await until(async () => (await poll(harness, session_id)).status === "exited");
    expect(await poll(harness, session_id)).toMatchObject({ exit_code: null, note: RESTARTED });
    expect((await run(harness, tryWrite(".git/config"))).ok?.output).toBe(`denied\n\n${restartNotice(appeared(".git/config"))}`);
  });

  it("does not restart for a new entry under a folder its wrap denies already", async () => {
    mkdirSync(join(folder, ".idea"));
    const harness = await host();
    const session_id = (await begin(harness, "sleep 608")).ok.session_id as string;
    // An IDE's save, made outside the app: it is read-only inside the runner as it is.
    writeFileSync(join(folder, ".idea", "x"), "");
    // Past two timed looks.
    await new Promise((resolve) => setTimeout(resolve, 11_000));
    expect((await poll(harness, session_id)).status).toBe("running");
    expect((await run(harness, tryWrite(".idea/x"))).ok?.output).toBe("denied\n");
  });

  it("gives commands sandboxes of their own when the new runner is refused, and refuses starts", async () => {
    const harness = await host();
    await begin(harness, "sleep 624");
    const deep = Array.from({ length: 10 }, (_, i) => `d${i + 1}`).join("/");
    const many = `for i in $(seq 0 ${MAX_EXTRA_DENIES}); do mkdir -p ${deep}/p$i/.vscode && : > ${deep}/p$i/.vscode/a; done; echo made`;
    expect((await run(harness, many)).ok?.output).toBe("made\n");
    expect((await run(harness, "true")).ok?.output).toBe(restartNotice(appeared(`${deep}/p0/.vscode`)));
    // Sandboxes of their own: two commands at once are in two network namespaces (a freed one's number is reused).
    const together = await Promise.all([0, 1].map(() => run(harness, "readlink /proc/self/ns/net; sleep 1")));
    expect(new Set(together.map((answer) => answer.ok?.output)).size).toBe(2);
    expect(await begin(harness, "sleep 625")).toEqual({
      error: { type: "sandbox", message: expect.stringContaining(`this folder has ${MAX_EXTRA_DENIES + 1} protected paths`) },
    });
  });
});

describe("a restart the app asks for", { timeout: 40_000 }, () => {
  it("restarts the runner on a grant, and tells the next command", async () => {
    const harness = await host();
    const session_id = (await begin(harness, "sleep 637")).ok.session_id as string;
    harness.send({ type: "restart", reason: "grant" });
    await until(async () => (await poll(harness, session_id)).status === "exited");
    expect(await poll(harness, session_id)).toMatchObject({ exit_code: null, note: RESTARTED });
    expect((await run(harness, "echo hi")).ok?.output).toBe(`hi\n\n${restartNotice(GRANT_CHANGED)}`);
  });

  it("answers a command it interrupts, a wait on a process it ends, and the work after", async () => {
    const harness = await host();
    const session_id = (await begin(harness, "sleep 605")).ok.session_id as string;
    const waiting = ask(harness, "wait", { session_id, timeout: 60 });
    const running = run(harness, ": > begun; sleep 5; echo late");
    // In the old runner before the restart starts.
    await until(() => existsSync(join(folder, "begun")));
    harness.send({ type: "restart", reason: "grant" });
    expect(await running).toEqual(SANDBOX_STOPPED);
    expect((await waiting).ok).toEqual({ status: "exited", exit_code: null, output: "", note: RESTARTED });
    expect((await run(harness, "echo next")).ok?.output).toBe(`next\n\n${restartNotice(GRANT_CHANGED)}`);
  });

  it("stops cleanly while it restarts, and leaves the folder as it was", async () => {
    const harness = await host();
    await begin(harness, "sleep 628");
    harness.send({ type: "restart", reason: "grant" });
    await harness.stop();
    expect(await harness.exited).toBe(0);
    expect(readdirSync(folder)).toEqual([]);
  });

  it("does nothing with no runner", async () => {
    const harness = await host();
    harness.send({ type: "restart", reason: "grant" });
    expect((await run(harness, "echo hi")).ok?.output).toBe("hi\n");
    expect((await begin(harness, "true")).ok?.session_id).toEqual(expect.any(String));
  });
});

describe("restarts that come quickly", { timeout: 60_000 }, () => {
  it("come at most once in 10 seconds: a key that comes and goes sooner restarts when the window ends", async () => {
    const harness = await host();
    const first = (await begin(harness, "sleep 638")).ok.session_id as string;
    harness.send({ type: "restart", reason: "grant" });
    await until(async () => (await poll(harness, first)).status === "exited");
    const restarted = Date.now();
    // A start waits for the new runner, whose baseline is then taken.
    const second = (await begin(harness, "sleep 639")).ok.session_id as string;
    // Made outside the app, as the user's own tools would, and gone before the window ends.
    mkdirSync(join(folder, "sub", ".idea"), { recursive: true });
    writeFileSync(join(folder, "sub", ".idea", "x"), "");
    await new Promise((resolve) => setTimeout(resolve, 7_000));
    rmSync(join(folder, "sub"), { recursive: true });
    expect((await poll(harness, second)).status).toBe("running");
    await until(async () => (await poll(harness, second)).status === "exited");
    expect(Date.now() - restarted).toBeGreaterThanOrEqual(9_000);
    expect(await poll(harness, second)).toMatchObject({ exit_code: null, note: RESTARTED });
    expect((await run(harness, "true")).ok?.output).toBe(restartNotice(appeared("sub/.idea")));
  });

  it("tells the latest reason asked for while a restart waits for the window", async () => {
    const harness = await host();
    const first = (await begin(harness, "sleep 601")).ok.session_id as string;
    harness.send({ type: "restart", reason: "grant" });
    await until(async () => (await poll(harness, first)).status === "exited");
    const second = (await begin(harness, "sleep 602")).ok.session_id as string;
    // A key the next look sees, whose restart waits for the window; then a grant.
    mkdirSync(join(folder, "sub", ".idea"), { recursive: true });
    writeFileSync(join(folder, "sub", ".idea", "x"), "");
    await new Promise((resolve) => setTimeout(resolve, 6_000));
    harness.send({ type: "restart", reason: "grant" });
    await until(async () => (await poll(harness, second)).status === "exited");
    // The polls above took the first restart's notice.
    expect((await run(harness, "true")).ok?.output).toBe(restartNotice(GRANT_CHANGED));
  });

  it("lets a run in flight finish when the window ends on a key's restart, and restarts after it", async () => {
    const harness = await host();
    const first = (await begin(harness, "sleep 603")).ok.session_id as string;
    harness.send({ type: "restart", reason: "grant" });
    await until(async () => (await poll(harness, first)).status === "exited");
    const second = (await begin(harness, "sleep 604")).ok.session_id as string;
    // This poll takes the grant's notice.
    expect((await poll(harness, second)).status).toBe("running");
    // A key the next timed look sees with no run in flight: its restart waits for the window.
    mkdirSync(join(folder, "sub", ".idea"), { recursive: true });
    await new Promise((resolve) => setTimeout(resolve, 6_000));
    // The window ends while this sleeps.
    expect(await run(harness, "sleep 6 && echo done")).toMatchObject({ ok: { output: "done\n", returncode: 0 } });
    // Its own look restarts the runner.
    await until(async () => (await poll(harness, second)).status === "exited");
    expect(await poll(harness, second)).toMatchObject({ exit_code: null, note: RESTARTED });
    expect((await run(harness, "true")).ok?.output).toBe(restartNotice(appeared("sub/.idea")));
  });
});
