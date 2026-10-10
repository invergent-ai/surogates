// The guest under QEMU and KVM, booted by the VM manager: the image built by
// images/guest/build.sh, the agent disk built from this package (npm run build
// first). Behind SUROGATE_VM_TESTS=1; SUROGATE_VM_IMAGE names another image folder.

import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, realpathSync, rmSync, statSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { BOOT_ID } from "../../src/binding/folder.js";
import type { Operation, Outcome } from "../../src/link/protocol.js";
import { VmClient } from "../../src/vm/client.js";
import { VmExecutor } from "../../src/vm/executor.js";
import { agentDisk, background, IMAGE, KVM, needsKvm, signal, USER } from "./guest-support.js";

beforeAll(needsKvm);

describe.skipIf(process.env.SUROGATE_VM_TESTS !== "1")("the VmExecutor, with the guest", { timeout: 60_000 }, () => {
  const CHAT = "5f6a7b8c-9d0e-4f1a-8b2c-3d4e5f6a7b8c";
  let dir: string;
  let run: string;
  let vm: VmClient;
  let executor: VmExecutor;
  const operation = (kind: string, args: Record<string, unknown>): Operation => ({
    id: `${kind}-${Math.random()}`, sessionId: CHAT, callingSessionId: CHAT, invocationId: "call", ordinal: 1, kind, args, digest: "d",
  });
  const command = (line: string) => executor.run(operation("run", { command: line, workdir: null, timeout: 10 }), signal());

  beforeAll(() => {
    dir = realpathSync(mkdtempSync(join(tmpdir(), "vm-executor-")));
    const folder = join(dir, "folder");
    mkdirSync(folder);
    run = mkdtempSync(join(process.env.XDG_RUNTIME_DIR ?? "/tmp", "sg-vm-"));
    // The manager in a process of its own, as the app runs it: what it tells of a root's processes comes through its channel.
    vm = new VmClient({
      vm: {
        kernel: join(IMAGE, "vmlinuz"), rootfs: join(IMAGE, "rootfs.img"), agentDisk: agentDisk(dir), sessions: join(dir, "sessions.img"),
        run, console: join(dir, "console.log"), user: USER, kvm: KVM,
      },
    });
    const { dev, ino } = statSync(folder);
    executor = new VmExecutor({
      bindingOf: (root) => (root === CHAT ? { folder, dev, ino, boot: BOOT_ID } : undefined),
      dataDir: join(dir, "data"), cacheDir: join(dir, "cache", "surogate"), env: { HOME: USER.home, LANG: "C.UTF-8", PATH: "/usr/bin:/bin" }, idleMs: 500, vm, user: "u1",
    });
  });

  afterAll(async () => {
    await executor?.stop();
    await vm?.stop();
    rmSync(run, { recursive: true, force: true });
    rmSync(dir, { recursive: true, force: true });
  });

  it("answers a chat's first which, its file host and its guest started for it", async () => {
    const begun = performance.now();
    expect(await executor.run(operation("which", { name: "pandoc" }), signal())).toEqual({ ok: true });
    const cold = performance.now() - begun;
    const again = performance.now();
    expect(await executor.run(operation("which", { name: "pandoc" }), signal())).toEqual({ ok: true });
    console.log(`which: ${cold.toFixed(0)} ms with its file host and the guest cold, ${(performance.now() - again).toFixed(0)} ms warm`);
  });

  it("keeps the chat's file host while its background process lives, and answers for it once the guest that ran it goes", async () => {
    const started = await executor.run(operation("start", background("sleep 30")), signal()) as { ok: { session_id: string } };
    const { session_id } = started.ok;
    // Past the file host's 500 ms idle time: had it let the folder go, the root's teardown would have ended the process.
    await new Promise((resolve) => setTimeout(resolve, 1_500));
    expect(await executor.run(operation("poll", { session_id }), signal())).toMatchObject({ ok: { status: "running" } });
    process.kill(Number(readFileSync(join(run, "qemu.pid"), "utf8")), "SIGKILL");
    await new Promise((resolve) => setTimeout(resolve, 500));
    // A new guest, which the file host gives what it kept.
    expect(await executor.run(operation("poll", { session_id }), signal())).toMatchObject({
      ok: { status: "exited", exit_code: null, note: "The process ended because the computer's sandbox stopped" },
    });
  });

  it("lets a slow rebase, a cherry-pick sequence and a merge complete under the rule, and one stopped for a conflict go on or be aborted in the next command", { timeout: 120_000 }, async () => {
    const folder = join(dir, "folder");
    const long = (line: string) => executor.run(operation("run", { command: line, workdir: null, timeout: 60 }), signal());
    // A repository as the user has it: a topic of six commits on main, and a branch that changes main's file another way.
    expect(spawnSync("bash", ["-c", [
      "git init -q -b main . && git config user.email a@b && git config user.name a",
      "echo base > base.txt && git add -A && git commit -qm base",
      "git checkout -qb topic && for i in 1 2 3 4 5 6; do echo $i > t$i.txt && git add -A && git commit -qm t$i; done",
      "git checkout -qb other main && echo other > base.txt && git commit -qam other",
      "git checkout -q main && echo main > base.txt && git commit -qam main",
    ].join(" && ")], { cwd: folder }).status).toBe(0);
    // Each stops for base.txt's conflict.
    const stopped = (start: string, state: string) => `${start} >/dev/null 2>&1; test -e .git/${state} && echo stopped`;
    const resolved = "echo both > base.txt && git add base.txt && GIT_EDITOR=true";
    try {
      // The root's first command: from its answer on, the file host looks every 5 s.
      expect(await command("true")).toMatchObject({ ok: { returncode: 0 } });
      // Six picks a second apart, so a look comes while it runs, as the review's probe broke at pick 10 of 12.
      expect(await long("git checkout -q topic && out=$(git rebase -q -x 'sleep 1' main 2>&1) || echo \"$out\"; git log --format=%s main..topic | tr '\\n' ' '")).toMatchObject({
        ok: { output: "t6 t5 t4 t3 t2 t1 " },
      });
      expect(await command(stopped("git checkout -qb c1 other && git rebase main", "rebase-merge"))).toMatchObject({ ok: { output: "stopped\n" } });
      expect(await command(`${resolved} git rebase --continue >/dev/null 2>&1; git log --format=%s -2 | tr '\\n' ' '`)).toMatchObject({ ok: { output: "other main " } });
      expect(await command(stopped("git checkout -qb c2 other && git rebase main", "rebase-merge"))).toMatchObject({ ok: { output: "stopped\n" } });
      expect(await command("git rebase --abort 2>&1; git rev-parse --abbrev-ref HEAD; git status --porcelain")).toMatchObject({ ok: { output: "c2\n" } });
      expect(await command(stopped("git checkout -qb picks main && git cherry-pick topic~1 other topic", "sequencer"))).toMatchObject({ ok: { output: "stopped\n" } });
      expect(await command(`${resolved} git cherry-pick --continue >/dev/null 2>&1; git log --format=%s -4 | tr '\\n' ' '`)).toMatchObject({ ok: { output: "t6 other t5 main " } });
      expect(await command(stopped("git checkout -qb merged main && git merge other", "MERGE_HEAD"))).toMatchObject({ ok: { output: "stopped\n" } });
      expect(await command(`${resolved} git commit -q --no-edit 2>&1; git log -1 --format=%p | wc -w`)).toMatchObject({ ok: { output: "2\n" } });
      // Git's own state is gone from the host's repository, and the rule still refuses a write to its config.
      expect(["rebase-merge", "sequencer", "MERGE_HEAD"].filter((name) => existsSync(join(folder, ".git", name)))).toEqual([]);
      expect(await command("(echo '[alias] x = !evil' >> .git/config) 2>&1 | sed 's/.*: //'")).toMatchObject({ ok: { output: "Operation not permitted\n" } });
      expect(readFileSync(join(folder, ".git", "config"), "utf8")).not.toContain("evil");
    } finally {
      for (const name of [".git", "base.txt", "t1.txt", "t2.txt", "t3.txt", "t4.txt", "t5.txt", "t6.txt"]) rmSync(join(folder, name), { recursive: true, force: true });
    }
  });

  // The host's git rebase --continue runs a paused rebase's exec steps outside the sandbox, and the rule lets
  // commands write git's transient state: the look after a command comments out each step it added.
  it("strips a command-planted rebase exec line on the host, but not git's own", async () => {
    const folder = join(dir, "folder");
    const todo = join(folder, ".git", "rebase-merge", "git-rebase-todo");
    const steps = join(dir, "steps.log");
    expect(spawnSync("bash", ["-c", [
      "git init -q -b main . && git config user.email a@b && git config user.name a",
      "for i in 1 2 3; do echo $i > f$i.txt && git add -A && git commit -qm c$i; done",
    ].join(" && ")], { cwd: folder }).status).toBe(0);
    // A command of the chat's first: from its answer on, the file host looks every 5 s.
    expect(await command("git log --oneline | wc -l")).toMatchObject({ ok: { output: "3\n" } });
    // Then the user's own rebase -x, on the host, stopped before its first pick: its exec steps are the
    // user's git's, and a look while nothing of the chat's runs takes them as the user's.
    expect(spawnSync("bash", ["-c", `GIT_SEQUENCE_EDITOR='sed -i 1ibreak' git rebase -q -i -x 'echo user-step >> ${steps}' HEAD~2`], { cwd: folder }).status).toBe(0);
    const own = readFileSync(todo, "utf8");
    expect(own.match(/^exec /gm)).toHaveLength(2);
    await new Promise((resolve) => setTimeout(resolve, 6_000));
    expect(readFileSync(todo, "utf8")).toBe(own);
    try {
      // The rule lets the write through; the look after the command comments it out, and says so.
      expect(await command(`echo 'exec touch ${folder}/pwned' >> .git/rebase-merge/git-rebase-todo && echo planted`)).toMatchObject({
        ok: { output: expect.stringMatching(/^planted\n\nThe computer removed a step .*: \.git\/rebase-merge\/git-rebase-todo$/) },
      });
      expect(readFileSync(todo, "utf8")).toBe(`${own}# Surogate removed a step that appeared while the chat's commands could write: exec touch ${folder}/pwned\n`);
      // The user's git goes on, with its own steps and none of the command's.
      expect(spawnSync("git", ["rebase", "--continue"], { cwd: folder }).status).toBe(0);
      expect(existsSync(join(folder, "pwned"))).toBe(false);
      expect(readFileSync(steps, "utf8")).toBe("user-step\nuser-step\n");
    } finally {
      for (const name of [".git", "f1.txt", "f2.txt", "f3.txt", "pwned"]) rmSync(join(folder, name), { recursive: true, force: true });
      rmSync(steps, { force: true });
    }
  });

  it("runs every exec step of a guest rebase -x that runs as one long command, with no notice, and strips those of one paused at a command's end", async () => {
    const folder = join(dir, "folder");
    const long = (line: string) => executor.run(operation("run", { command: line, workdir: null, timeout: 60 }), signal());
    expect(spawnSync("bash", ["-c", [
      "git init -q -b main . && git config user.email a@b && git config user.name a",
      "for i in 1 2 3 4 5 6 7; do echo $i > f$i.txt && git add -A && git commit -qm c$i; done",
    ].join(" && ")], { cwd: folder }).status).toBe(0);
    try {
      // From its answer on, the file host looks every 5 s: at least one look comes while the rebase's eight seconds run.
      expect(await command("true")).toMatchObject({ ok: { returncode: 0 } });
      expect(await long("git rebase -q -x 'sleep 1.3; echo step >> steps.log' HEAD~6 2>&1; wc -l < steps.log")).toMatchObject({ ok: { output: "6\n" } });
      expect(existsSync(join(folder, ".git", "rebase-merge"))).toBe(false);
      // One the command leaves paused: the look after it comments out the steps it wrote.
      expect(await command("GIT_SEQUENCE_EDITOR='sed -i 1ibreak' git rebase -q -i -x 'touch guest-step' HEAD~2 >/dev/null 2>&1; echo paused")).toMatchObject({
        ok: { output: expect.stringMatching(/^paused\n\nThe computer removed a step .*: \.git\/rebase-merge\/git-rebase-todo$/) },
      });
      const todo = readFileSync(join(folder, ".git", "rebase-merge", "git-rebase-todo"), "utf8");
      expect(todo.match(/^# Surogate removed a step that appeared while the chat's commands could write: exec touch guest-step$/gm)).toHaveLength(2);
      expect(todo).not.toMatch(/^exec /m);
    } finally {
      for (const name of [".git", "steps.log", "f1.txt", "f2.txt", "f3.txt", "f4.txt", "f5.txt", "f6.txt", "f7.txt"]) rmSync(join(folder, name), { recursive: true, force: true });
    }
  });

  // A ceiling: a new linked worktree needs its .git file and its commondir, which the rule refuses, so git worktree
  // add is the host's to run. One the user made on the host stays the user's: what would send it to a config is refused.
  it("refuses git worktree add in the guest, in git's own words, and every write that would send the host's linked worktree to a config", { timeout: 60_000 }, async () => {
    const folder = join(dir, "folder");
    const admin = join(folder, ".git", "worktrees", "wt");
    const said = (line: string) => `(${line}) 2>&1 | sed 's/.*: //'`;
    expect(spawnSync("bash", ["-c", [
      "git init -q -b main . && git config user.email a@b && git config user.name a && git config extensions.worktreeConfig true",
      "echo base > base.txt && git add -A && git commit -qm base",
      "git worktree add -q wt -b wtb && git -C wt config --worktree core.editor true",
    ].join(" && ")], { cwd: folder }).status).toBe(0);
    try {
      expect(await command("git worktree add -q wt2 -b wtb2 2>&1; echo rc=$?")).toMatchObject({
        ok: { output: "fatal: could not open 'wt2/.git' for writing: Operation not permitted\nrc=128\n" },
      });
      // git takes back what it had made of it.
      expect([existsSync(join(folder, "wt2")), existsSync(join(folder, ".git", "worktrees", "wt2"))]).toEqual([false, false]);
      const commondir = readFileSync(join(admin, "commondir"), "utf8");
      // A git folder of the command's own, whose config would run a program at the next status on the host.
      expect(await command([
        "git init -q --bare evil.git && git -C evil.git config core.fsmonitor 'touch pwned'",
        said("echo \"$PWD/evil.git\" > .git/worktrees/wt/commondir"),
        said("echo '[core] fsmonitor = touch pwned' >> .git/worktrees/wt/config.worktree"),
        said("mv .git/worktrees/wt .git/worktrees/wt-old"),
        said("mv .git/worktrees .git/worktrees-old"),
        "git -C wt status --porcelain",
      ].join("; "))).toMatchObject({ ok: { output: "Operation not permitted\n".repeat(4) } });
      expect(readFileSync(join(admin, "commondir"), "utf8")).toBe(commondir);
      expect(readFileSync(join(admin, "config.worktree"), "utf8")).not.toContain("fsmonitor");
      expect(spawnSync("git", ["-C", join(folder, "wt"), "status", "--porcelain"]).status).toBe(0);
      expect(existsSync(join(folder, "wt", "pwned"))).toBe(false);
    } finally {
      for (const name of [".git", "wt", "evil.git", "base.txt"]) rmSync(join(folder, name), { recursive: true, force: true });
    }
  });

  it("runs commands beside a protected name linked out of the folder, or to a protected name in it, whose file the rule keeps", async () => {
    const folder = join(dir, "folder");
    // An editor's settings shared with a sibling worktree, which the guest does not have.
    const shared = join(dir, "shared-vscode");
    mkdirSync(shared);
    writeFileSync(join(shared, "settings.json"), "{}\n");
    symlinkSync(shared, join(folder, ".vscode"));
    mkdirSync(join(folder, ".idea"));
    writeFileSync(join(folder, ".idea", "mcp.json"), "{}\n");
    symlinkSync(".idea/mcp.json", join(folder, ".mcp.json"));
    try {
      expect(await command("echo ran; echo '{\"x\": 1}' 2>/dev/null > .mcp.json || echo refused")).toMatchObject({ ok: { output: "ran\nrefused\n" } });
      expect(await command("echo ran")).toMatchObject({ ok: { output: "ran\n" } });
      expect([readFileSync(join(folder, ".idea", "mcp.json"), "utf8"), readFileSync(join(shared, "settings.json"), "utf8")]).toEqual(["{}\n", "{}\n"]);
    } finally {
      for (const name of [".vscode", ".mcp.json", ".idea"]) rmSync(join(folder, name), { recursive: true, force: true });
      rmSync(shared, { recursive: true, force: true });
    }
  });

  // The rule judges a write by the path it reaches: through this link, mcp.json, which it does not keep.
  it("refuses a command, before it runs, while a protected name links to a file in the folder the rule does not keep, and runs it once the name is a file", async () => {
    const folder = join(dir, "folder");
    writeFileSync(join(folder, "mcp.json"), "{}\n");
    symlinkSync("mcp.json", join(folder, ".mcp.json"));
    try {
      // A look sees the link the host made, at the host's start or after this command: commands are refused from the next one.
      await command("true");
      expect(await command("touch ran")).toEqual({
        error: { type: "sandbox", message: "Blocked: .mcp.json is a link to mcp.json in this folder. Make it a file or folder of its own, or point it outside the folder or at a protected name, to run commands here." },
      });
      expect(existsSync(join(folder, "ran"))).toBe(false);
      rmSync(join(folder, ".mcp.json"));
      writeFileSync(join(folder, ".mcp.json"), "{}\n");
      expect(await command("touch ran && echo ran")).toMatchObject({ ok: { output: "ran\n" } });
    } finally {
      for (const name of [".mcp.json", "mcp.json", "ran"]) rmSync(join(folder, name), { force: true });
    }
  });

  // A write through .git or .claude reaches the config or the commands under where it leads; so does one
  // through a chain that leaves the folder and comes back. Each layout returns what it made in the folder.
  const layouts: Array<[string, (folder: string) => string[], string]> = [
    ["a .git", (folder) => {
      mkdirSync(join(folder, "realgit"));
      writeFileSync(join(folder, "realgit", "config"), "[core]\n");
      symlinkSync("realgit", join(folder, ".git"));
      return [".git", "realgit"];
    }, ".git is a link to realgit"],
    [".claude", (folder) => {
      mkdirSync(join(folder, "dotclaude", "commands"), { recursive: true });
      symlinkSync("dotclaude", join(folder, ".claude"));
      return [".claude", "dotclaude"];
    }, ".claude is a link to dotclaude"],
    ["a chain that leaves the folder and comes back", (folder) => {
      mkdirSync(join(folder, "config"));
      symlinkSync(join(folder, "config", "mcp.json"), join(dir, "hop"));
      symlinkSync(join(dir, "hop"), join(folder, ".mcp.json"));
      return [".mcp.json", "config", "../hop"];
    }, ".mcp.json is a link to config/mcp.json"],
  ];
  for (const [title, layout, linked] of layouts) {
    it(`refuses a command, before it runs, while ${title} links into the folder, and runs it once the link is gone`, async () => {
      const folder = join(dir, "folder");
      const made = layout(folder);
      const clear = () => {
        for (const name of [...made, "ran"]) rmSync(join(folder, name), { recursive: true, force: true });
      };
      try {
        await command("true");
        expect(await command("touch ran")).toEqual({
          error: { type: "sandbox", message: `Blocked: ${linked} in this folder. Make it a file or folder of its own, or point it outside the folder or at a protected name, to run commands here.` },
        });
        expect(existsSync(join(folder, "ran"))).toBe(false);
        clear();
        expect(await command("echo ran")).toMatchObject({ ok: { output: "ran\n" } });
      } finally {
        clear();
      }
    });
  }

  // A command can unpack an editor's folder below node_modules (the rule leaves dependency folders alone),
  // and a link outside it would show that folder where editors read it.
  it("refuses a command, before it runs, while a link a command made leads into a dependency folder, and runs it once the link is gone", async () => {
    const folder = join(dir, "folder");
    const clear = () => {
      for (const name of ["node_modules", "sub", "ran"]) rmSync(join(folder, name), { recursive: true, force: true });
    };
    try {
      expect(await command("mkdir -p node_modules/p/.vscode && echo '{}' > node_modules/p/.vscode/tasks.json && ln -s node_modules/p sub && echo made")).toMatchObject({
        ok: { output: "made\n" },
      });
      expect(await command("touch ran")).toEqual({
        error: { type: "sandbox", message: "Blocked: sub leads into node_modules. Remove the link to run commands here." },
      });
      expect(existsSync(join(folder, "ran"))).toBe(false);
      rmSync(join(folder, "sub"));
      expect(await command("echo ran")).toMatchObject({ ok: { output: "ran\n" } });
    } finally {
      clear();
    }
  });

  it("shows a command what the file tools wrote just before it, each time, with nothing to wait for", async () => {
    const folder = join(dir, "folder");
    const key = join(folder, "lint.py");
    const write = (text: string) => executor.run(operation("write", { key, data: Buffer.from(text).toString("base64") }), signal());
    let stale = 0;
    let begun = performance.now();
    for (let i = 0; i < 1_000; i += 1) {
      expect(await write(`x = ${i}\n`)).toEqual({ ok: null });
      if ((await command("cat lint.py") as { ok: { output: string } }).ok.output !== `x = ${i}\n`) stale += 1;
    }
    const seen = (performance.now() - begun) / 1_000;
    // A patch, then its lint, fifty times: the lint sees each patch.
    begun = performance.now();
    for (let i = 0; i < 50; i += 1) {
      expect(await write(i % 2 ? `x = ${i}\n` : "x = (\n")).toEqual({ ok: null });
      expect(await command("python3 -m py_compile lint.py 2>/dev/null && echo clean || echo broken")).toMatchObject({ ok: { output: i % 2 ? "clean\n" : "broken\n" } });
    }
    console.log(`M4: ${stale} of 1000 commands right after a write saw the old file; a write and the command after it ${seen.toFixed(1)} ms, a patch and its lint ${((performance.now() - begun) / 50).toFixed(0)} ms`);
    expect(stale).toBe(0);
    rmSync(key, { force: true });
  });

  it("keeps the file tools in the folder while a command in the guest flips a folder of it into a link out of it, through 5 000 operations", { timeout: 300_000 }, async () => {
    const folder = join(dir, "folder");
    const outside = join(dir, "outside");
    mkdirSync(outside);
    writeFileSync(join(outside, "only-outside"), "OUTSIDE\n");
    // In the guest the link's target names nothing; on the host it leads out of the folder.
    const flipper = `while :; do rm -rf sub; mkdir sub; echo inside > sub/inside; rm -rf sub; ln -s '${outside}' sub; done`;
    const started = await executor.run(operation("start", background(flipper)), signal()) as { ok: { session_id: string } };
    const tally: Record<string, number> = {};
    const count = (kind: string, outcome: Outcome) => {
      const error = "error" in outcome ? outcome.error as { type: string; code?: string } : null;
      const key = `${kind} ${error ? error.code ?? error.type : "ok"}`;
      tally[key] = (tally[key] ?? 0) + 1;
    };
    try {
      for (let i = 0; i < 1_250; i += 1) {
        count("write", await executor.run(operation("write", { key: join(folder, "sub", `x-${i}`), data: Buffer.from("m8\n").toString("base64") }), signal()));
        const read = await executor.run(operation("read", { key: join(folder, "sub", "only-outside"), max_bytes: null }), signal());
        expect("ok" in read && Buffer.from(String(read.ok), "base64").toString()).not.toBe("OUTSIDE\n");
        count("read", read);
        const listed = await executor.run(operation("list_dir", { key: join(folder, "sub") }), signal());
        expect("ok" in listed && (listed.ok as string[]).includes("only-outside")).toBe(false);
        count("list_dir", listed);
        count("delete", await executor.run(operation("delete", { key: join(folder, "sub", "only-outside") }), signal()));
      }
    } finally {
      await executor.run(operation("kill", { session_id: started.ok.session_id }), signal());
      rmSync(join(folder, "sub"), { recursive: true, force: true });
    }
    console.log(`M8: 5 000 file operations against a guest command's flips: ${JSON.stringify(tally)}`);
    expect(readdirSync(outside)).toEqual(["only-outside"]);
    expect(readFileSync(join(outside, "only-outside"), "utf8")).toBe("OUTSIDE\n");
  });

  it("ends what a command left running once the chat's file host lets its folder go", async () => {
    // What a background process left, in a session of its own and with its environment cleared, once that process has ended.
    const leaver = "env -i /usr/bin/setsid /usr/bin/nohup /usr/bin/sleep 300 < /dev/null > /dev/null 2>&1 & echo started";
    expect(await executor.run(operation("start", background(leaver)), signal())).toMatchObject({ ok: { session_id: expect.any(String) } });
    expect(await command("sleep 0.5; pgrep -c -x sleep")).toMatchObject({ ok: { output: "1\n" } });
    // Idle for 500 ms, the file host tears the guest's root down, then lets the folder go.
    await new Promise((resolve) => setTimeout(resolve, 2_000));
    const begun = performance.now();
    expect(await executor.run(operation("which", { name: "pandoc" }), signal())).toEqual({ ok: true });
    console.log(`which: ${(performance.now() - begun).toFixed(0)} ms with its file host cold and the guest warm`);
    expect(await command("pgrep -c -x sleep || true")).toMatchObject({ ok: { output: "0\n" } });
  });
});
