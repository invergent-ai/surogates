// An agent's commands in a folder of this computer, through the real app: the
// shell's device, its binder and the VmExecutor, the VM manager in its utility
// process, and the guest. Behind SUROGATE_VM_TESTS=1: KVM, QEMU, virtiofsd, the
// image images/guest/build.sh makes (or SUROGATE_VM_IMAGE's folder), and
// npm run agent-disk.

import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import type { ElectronApplication, Page } from "playwright-core";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { vmOptions } from "../../src/vm/client.js";
import { connect, FakeAgent, signedInAndAdded, webClient } from "./fake-agent.js";
import { dataHome, launch, press, prompt, promptsShown, quit, shellPage, stubNative } from "./launch.js";

const IMAGE = process.env.SUROGATE_VM_IMAGE ?? fileURLToPath(new URL("../../../images/guest/out", import.meta.url));
const CHAT = "4e5f6a7b-8c9d-4e0f-a1b2-c3d4e5f6a7b8";
const OTHER = "5f6a7b8c-9d0e-4f1a-b2c3-d4e5f6a7b8c9";

let home: string;
let runtime: string;
let agent: FakeAgent;
let app: ElectronApplication | undefined;
let next = 0;

beforeEach(() => {
  home = dataHome();
  // The VM's sockets: short, as a vhost-user socket's path must be.
  runtime = mkdtempSync("/tmp/sr-");
  agent = new FakeAgent();
});

afterEach(async () => {
  await quit(app);
  app = undefined;
  await agent.stop();
  await agent.link.stop();
  rmSync(home, { recursive: true, force: true });
  rmSync(runtime, { recursive: true, force: true });
});

// *chat*'s operation, as the server sends it, and the app's result for it.
async function operation(kind: string, args: Record<string, unknown>, invocation = "call", ordinal = 1, chat = CHAT): Promise<unknown> {
  const id = `op-${(next += 1)}`;
  agent.link.send({
    type: "op", id, session_id: chat, calling_session_id: chat, invocation_id: invocation, ordinal, kind, args, digest: `d-${id}`,
  });
  let result: Record<string, unknown> | undefined;
  await expect.poll(() => {
    result = agent.link.received.find((frame) => frame.type === "op_result" && frame.id === id);
    return result !== undefined;
  }, { timeout: 30_000 }).toBe(true);
  agent.link.send({ type: "op_ack", id });
  return result?.outcome;
}

// The app launched and signed in, and *folder* bound to the chat as the user picked it.
async function bound(folder: string): Promise<void> {
  await bind(await launched(), folder);
}

// The app launched and signed in: the agent's web client in it.
async function launched(): Promise<Page> {
  const origin = await agent.start();
  app = await launch(home, { XDG_RUNTIME_DIR: runtime, SUROGATE_VM_IMAGE: IMAGE });
  await stubNative(app);
  const page = await shellPage(app);
  await connect(page, origin);
  await signedInAndAdded(app, page, agent);
  const client = await webClient(app, origin);
  return client;
}

// *folder* bound to *chat* as the user picked it: in the system's dialog, then Use this folder in the sheet.
async function bind(client: Page, folder: string, chat = CHAT): Promise<void> {
  await app?.evaluate((_electron, picked) => Object.assign(globalThis, { folder: picked }), folder);
  const preparing = client.evaluate(() => window.surogateDesktop!.prepareFolder("pick")) as Promise<{ folder: string; nonce: string }>;
  await press(await prompt(app!), "accept");
  const prepared = await preparing;
  expect(prepared.folder).toBe(folder);
  expect(await operation("bind", { folder, nonce: prepared.nonce }, "bind", 0, chat)).toEqual({ ok: null });
}

// Settings, open over the window on Folders and permissions, once it shows a chat.
async function foldersSettings(): Promise<Page> {
  await (await shellPage(app!)).click("#open-settings");
  let found: Page | undefined;
  await expect.poll(() => {
    found = app!.windows().find((page) => page.url().endsWith("/settings.html"));
    return found !== undefined;
  }).toBe(true);
  await found!.waitForSelector(".settings-nav .item");
  await found!.click('[data-section="folders"]');
  await found!.waitForSelector("#folders .row");
  return found!;
}

// A curl to *url* in the guest: its response's status, then its proxy's answer to CONNECT (000 for none).
const status = (url: string, flags = "") => `curl -sS --max-time 20 ${flags} -o /dev/null -w '%{http_code} %{http_connect}\\n' ${url} 2>/dev/null`;

// The VM's runtime folder for the app's state root, which its stop removes.
const vmRun = () => vmOptions(join(home, "surogate"), { uid: 0, gid: 0, name: "", home: "" }, { XDG_RUNTIME_DIR: runtime }).run;

describe.skipIf(process.env.SUROGATE_VM_TESTS !== "1")("commands through the app", () => {
  it("binds a folder the user picked, and runs the agent's commands in it, in the VM", async () => {
    const folder = join(home, "project");
    mkdirSync(folder);
    writeFileSync(join(folder, "notes.txt"), "alpha\n");
    await bound(folder);
    expect(await operation("run", { command: "cat notes.txt; pwd; id -u", workdir: null, timeout: 30 })).toEqual({
      ok: { output: `alpha\n${folder}\n10000\n`, returncode: 0, timed_out: false },
    });
    expect(await operation("which", { name: "pandoc" })).toEqual({ ok: true });
    // The file kinds stay on the host, in the helper's sandbox, and see what the command wrote.
    expect(await operation("run", { command: "echo made > made.txt", workdir: null, timeout: 30 })).toMatchObject({ ok: { returncode: 0 } });
    expect(await operation("read", { key: join(folder, "made.txt"), max_bytes: null })).toEqual({ ok: Buffer.from("made\n").toString("base64") });
    expect(statSync(join(folder, "made.txt")).uid).toBe(process.getuid?.());
    // The app's quit stops the VM itself, which takes its sockets with it; a VM that only died with the app would leave them.
    expect(existsSync(join(vmRun(), "control.sock"))).toBe(true);
    await quit(app);
    app = undefined;
    expect(existsSync(vmRun())).toBe(false);
  });

  it("runs the folder's own configure script, refuses SQLite's WAL there plainly, and keeps a database with a rollback journal that this computer reads", async () => {
    const folder = join(home, "build");
    mkdirSync(folder);
    // A configure script as a project checks one in: made on this computer, executable, looking for its tools.
    writeFileSync(join(folder, "configure"), [
      "#!/bin/sh",
      'for tool in python3 node; do command -v "$tool" >/dev/null || { echo "configure: $tool not found" >&2; exit 1; }; done',
      "echo PREFIX=/usr/local > config.status",
      "echo 'configure: wrote config.status'",
    ].join("\n") + "\n", { mode: 0o755 });
    await bound(folder);
    const ran = (output: string) => ({ ok: { output, returncode: 0, timed_out: false } });
    expect(await operation("run", { command: "./configure && cat config.status", workdir: null, timeout: 30 })).toEqual(
      ran("configure: wrote config.status\nPREFIX=/usr/local\n"),
    );
    const databases = [
      "python3 - <<'EOF'",
      "import sqlite3",
      "try:",
      "    wal = sqlite3.connect('wal.db')",
      "    wal.execute('pragma journal_mode=wal')",
      "    wal.execute('create table t (x)')",
      "    print('wal')",
      "except sqlite3.Error as error:",
      "    print('wal:', error)",
      "db = sqlite3.connect('app.db')",
      "db.execute('create table t (x)')",
      "db.execute('insert into t values (7)')",
      "db.commit()",
      "print(db.execute('pragma journal_mode').fetchone()[0])",
      "EOF",
    ].join("\n");
    // A shared mapping of the folder's file fails, as the model's note says: WAL fails plainly, and a rollback journal works
    // while nothing on this computer has the database open (it is read here only after the command has ended).
    expect(await operation("run", { command: databases, workdir: null, timeout: 30 })).toEqual(ran("wal: disk I/O error\ndelete\n"));
    // The file tools, on this computer, read what the script wrote; the user's own sqlite reads the database.
    expect(await operation("read", { key: join(folder, "config.status"), max_bytes: null })).toEqual({ ok: Buffer.from("PREFIX=/usr/local\n").toString("base64") });
    const read = spawnSync("python3", ["-c", "import sqlite3, sys; print(sqlite3.connect(sys.argv[1]).execute('select x from t').fetchone()[0])", join(folder, "app.db")], { encoding: "utf8" });
    expect(read.stdout).toBe("7\n");
  });

  it("runs two chats' commands in two folders at once, lints a patch it has just written, and refuses a command's write to the folder's git config", async () => {
    const [first, second] = [join(home, "first"), join(home, "second")];
    // Each a repository already, as most chats' folders are.
    for (const folder of [first, second]) expect(spawnSync("git", ["init", "-q", folder]).status).toBe(0);
    const client = await launched();
    await bind(client, first);
    await bind(client, second, OTHER);
    const run = (command: string, chat = CHAT) => operation("run", { command, workdir: null, timeout: 30 }, "call", 1, chat);
    const ran = (output: string) => ({ ok: { output, returncode: 0, timed_out: false } });
    // At once: each in its own folder, as a guest user of its own.
    const both = await Promise.all([run("sleep 1; pwd; id -u"), run("sleep 1; pwd; id -u", OTHER)]) as Array<{ ok: { output: string } }>;
    const seen = both.map(({ ok }) => ok.output.trim().split("\n"));
    expect(seen.map(([path]) => path)).toEqual([first, second]);
    expect(new Set([...seen.map(([, uid]) => uid), "10000", "10001"]).size).toBe(2);
    // A patch, and its lint at once after it: the lint sees each version.
    const patch = (text: string) => operation("write", { key: join(first, "app.py"), data: Buffer.from(text).toString("base64") });
    const lint = () => run("python3 -m py_compile app.py 2>/dev/null && echo clean || echo broken");
    expect(await patch("def f(:\n")).toEqual({ ok: null });
    expect(await lint()).toEqual(ran("broken\n"));
    expect(await patch("def f():\n    return 1\n")).toEqual({ ok: null });
    expect(await lint()).toEqual(ran("clean\n"));
    // The guest refuses a command's write to the folder's git config: git there runs no code the agent wrote.
    const config = readFileSync(join(first, ".git", "config"), "utf8");
    expect(await run("(echo '[core]\n\tfsmonitor = ./evil' >> .git/config) 2>&1 | sed 's/.*: //'")).toEqual(ran("Operation not permitted\n"));
    expect(readFileSync(join(first, ".git", "config"), "utf8")).toBe(config);
  });

  it("refuses every way a command could plant a git hook or send the folder's git elsewhere, and lets git rebase, merge and cherry-pick finish", { timeout: 120_000 }, async () => {
    const folder = join(home, "repo");
    const upstream = join(home, "upstream");
    // The user's repository, with a submodule, a linked worktree and a repository of its own in it, as git leaves them.
    const made = spawnSync("bash", ["-c", [
      "set -e",
      `git init -q -b master "${upstream}" && git -C "${upstream}" -c user.email=a@b -c user.name=a commit -q --allow-empty -m up`,
      `git init -q -b master "${folder}" && cd "${folder}" && git config user.email a@b && git config user.name a`,
      "echo a > a.txt && git add a.txt && git commit -qm a",
      `git -c protocol.file.allow=always submodule add -q "${upstream}" lib && git commit -qm lib`,
      "git worktree add -q wt -b wt && git init -q inner",
    ].join("\n")], { encoding: "utf8" });
    expect(made.status, made.stderr).toBe(0);
    const read = (path: string) => readFileSync(join(folder, path), "utf8");
    const KEPT = [".git/config", ".git/modules/lib/config", ".git/worktrees/wt/commondir", "wt/.git"];
    const hooks = () => [".git/hooks", ".git/modules/lib/hooks"].map((dir) => readdirSync(join(folder, dir)).sort());
    const [kept, hooked, inner] = [KEPT.map(read), hooks(), read("inner/.git/config")];
    await bound(folder);
    const run = (command: string) => operation("run", { command, workdir: null, timeout: 60 });
    const ran = (output: string) => ({ ok: { output, returncode: 0, timed_out: false } });

    // Each refused by the guest's kernel, in its own words; an ordinary file and folder of the command's are its own.
    const plants = [
      ["a hook where there was none", "printf '#!/bin/sh\\n' > .git/hooks/pre-commit"],
      ["a .git where there was none", "mkdir -p plain/.git/hooks"],
      ["the .git renamed and remade", "mv .git aside && mkdir -p .git/hooks"],
      ["a repository moved aside and its .git remade", "mv inner inner-old && mkdir -p inner/.git/hooks"],
      ["a hard link", "ln evil .git/hooks/post-commit"],
      ["a symlink", "ln -s ../../evil .git/hooks/pre-push"],
      ["a gitdir-pointer file", "mkdir -p ptr && echo \"gitdir: $PWD/aside\" > ptr/.git"],
      ["core.hooksPath through .git/config", "git config core.hooksPath evil-hooks"],
      ["a submodule's config", "git -C lib config core.hooksPath ../evil-hooks"],
      ["a submodule's hook", "printf '#!/bin/sh\\n' > .git/modules/lib/hooks/pre-commit"],
      ["a linked worktree's commondir", "echo \"$PWD/aside\" > .git/worktrees/wt/commondir"],
      ["a linked worktree's .git", "echo \"gitdir: $PWD/aside\" > wt/.git"],
    ] as const;
    expect(await run([
      "printf '#!/bin/sh\\necho planted\\n' > evil && chmod +x evil && mkdir evil-hooks && cp evil evil-hooks/pre-commit",
      ...plants.map(([name, command]) =>
        `(${command}) >/tmp/out 2>&1 && echo "${name}: ran" || { grep -q 'Operation not permitted' /tmp/out && echo "${name}: refused" || { echo "${name}: failed"; cat /tmp/out; }; }`),
    ].join("\n"))).toEqual(ran(plants.map(([name]) => `${name}: refused\n`).join("")));
    expect([KEPT.map(read), hooks(), read("inner-old/.git/config")]).toEqual([kept, hooked, inner]);
    for (const path of ["aside", "plain/.git", "inner/.git", "ptr/.git"]) expect(existsSync(join(folder, path)), path).toBe(false);
    for (const at of [folder, join(folder, "lib")]) expect(spawnSync("git", ["-C", at, "config", "--get", "core.hooksPath"]).status).toBe(1);

    // Git's own work in the repository, its transient state and all, is none of that.
    expect(await run([
      "git checkout -q -b feat && echo c > c.txt && git add c.txt && git commit -qm feat",
      "git checkout -q master && echo d > d.txt && git add d.txt && git commit -qm master",
      "git rebase -q master feat && echo rebased",
      "git checkout -q master && git checkout -q -b f2 && echo e > a.txt && git commit -qam f2",
      "git checkout -q master && echo f > a.txt && git commit -qam m2",
      "git merge f2 >/dev/null 2>&1; test -e .git/MERGE_HEAD && echo conflicted",
      "echo resolved > a.txt && git add a.txt && git commit -qm resolved && echo merged",
      "git checkout -q -b f3 && echo g > g.txt && git add g.txt && git commit -qm g",
      "git checkout -q master && git cherry-pick f3 >/dev/null && echo picked",
    ].join("\n"))).toEqual(ran("rebased\nconflicted\nmerged\npicked\n"));
    // And the host's git reads what it did, in the repository, its worktree and its submodule.
    const git = (at: string, ...args: string[]) => spawnSync("git", ["-C", join(folder, at), ...args], { encoding: "utf8" }).stdout;
    expect([git(".", "log", "-3", "--first-parent", "--format=%s"), git("wt", "branch", "--show-current"), git("lib", "log", "-1", "--format=%s")])
      .toEqual(["g\nresolved\nm2\n", "wt\n", "up\n"]);
    expect([KEPT.map(read), hooks()]).toEqual([kept, hooked]);
  });

  it("installs a package from PyPI in the VM with no prompt, and tells the agent what the app refused: its own services, and a site its prompts deny", async () => {
    const folder = join(home, "net");
    mkdirSync(folder);
    await bound(folder);
    const run = (command: string) => operation("run", { command, workdir: null, timeout: 120 });
    expect(await run("pip install --no-cache-dir --no-deps --reinstall --quiet cowsay==6.1 && python3 -c 'import cowsay; print(\"installed\")'")).toEqual({
      ok: { output: "installed\n", returncode: 0, timed_out: false },
    });
    // Its own services are refused unasked; a site off the package hosts asks, in the desktop's own window.
    const refused = run(`${status("https://example.com/")}; ${status("http://127.0.0.1:9/", "--noproxy ''")}`);
    // Asked once the host has looked the name up.
    await expect.poll(() => promptsShown(app!), { timeout: 30_000 }).toBe(1);
    const asked = await prompt(app!);
    expect(await asked.textContent("#prompt-title")).toBe("Connect to example.com:443?");
    expect(await asked.textContent(".code")).toBe("example.com:443");
    expect(await asked.evaluate(() => (document.activeElement as HTMLElement).dataset.id)).toBe("deny");
    await press(asked, "deny");
    expect(await refused).toEqual({
      ok: {
        output: "000 403\n403 000\n\nThis computer does not let a chat reach its own network services (127.0.0.1:9)\nThis computer did not allow network access to example.com:443.",
        returncode: 0,
        timed_out: false,
      },
    });
  });

  it("lets a command reach a site off the package hosts once its user allows it for the chat in the desktop's own window, on every port", async () => {
    const folder = join(home, "site");
    mkdirSync(folder);
    await bound(folder);
    const run = (command: string) => operation("run", { command, workdir: null, timeout: 60 });
    const reached = run(status("https://example.com/"));
    // Asked once the VM has started for the chat's first command.
    await expect.poll(() => promptsShown(app!), { timeout: 30_000 }).toBe(1);
    const asked = await prompt(app!);
    expect(await asked.textContent("#prompt-title")).toBe("Connect to example.com:443?");
    expect(await asked.$$eval("#prompt-buttons button", (buttons) => buttons.map((button) => button.dataset.id))).toEqual(["deny", "allow_session", "allow"]);
    await press(asked, "allow_session");
    // The app's part only, whatever the site answers: its proxy opened the tunnel, and told the agent of no refusal.
    expect(await reached).toEqual({ ok: { output: expect.stringMatching(/^\d{3} 200\n$/), returncode: 0, timed_out: false } });
    // Its other ports too, for the rest of the chat, unasked, whatever the site answers: a refusal would come with a notice.
    expect(await run(status("http://example.com/"))).toEqual({ ok: { output: expect.stringMatching(/^\d{3} 000\n$/), returncode: 0, timed_out: false } });
    expect(await promptsShown(app!)).toBe(0);
  });

  it("takes a site allowed for the chat back in Settings, after which the chat's next connection to it asks again", async () => {
    const folder = join(home, "site");
    mkdirSync(folder);
    await bound(folder);
    const run = (command: string) => operation("run", { command, workdir: null, timeout: 60 });
    const reached = run(status("https://example.com/"));
    await expect.poll(() => promptsShown(app!), { timeout: 30_000 }).toBe(1);
    await press(await prompt(app!), "allow_session");
    expect(await reached).toMatchObject({ ok: { returncode: 0 } });
    const settings = await foldersSettings();
    expect(await settings.textContent("#folders .row .line")).toBe("Reaches example.com, on every portTake back");
    expect(await settings.getAttribute("#folders .row .line button", "aria-label")).toBe("Take back example.com");
    await settings.click("#folders .row .line button");
    await expect.poll(() => settings.$$("#folders .row .line").then((lines) => lines.length)).toBe(0);
    const again = run(status("https://example.com/"));
    await expect.poll(() => promptsShown(app!), { timeout: 30_000 }).toBe(1);
    await press(await prompt(app!), "deny");
    expect(await again).toEqual({
      // curl's own exit for a tunnel its proxy refused.
      ok: { output: "000 403\n\nThis computer did not allow network access to example.com:443.", returncode: 56, timed_out: false },
    });
  });

  it("runs a background server in the folder, which the agent's next command reaches, and stops it with the app", async () => {
    const folder = join(home, "site");
    mkdirSync(folder);
    writeFileSync(join(folder, "index.html"), "<p>hello</p>\n");
    await bound(folder);
    const started = await operation("start", {
      command: "python3 -m http.server 8765 --bind 127.0.0.1", workdir: null, task_id: "demo", pty: false, notify_on_complete: false, watcher_interval: null,
    }) as { ok: { session_id: string } };
    const { session_id } = started.ok;
    expect(session_id).toMatch(/^proc_[0-9a-f]{12}$/);
    // The next command shares the server's network, as it would in the cloud.
    await expect.poll(async () => {
      const fetched = await operation("run", { command: "curl -sS http://127.0.0.1:8765/index.html", workdir: null, timeout: 10 }) as { ok?: { output: string } };
      return fetched.ok?.output;
    }, { timeout: 30_000, interval: 200 }).toBe("<p>hello</p>\n");
    expect(await operation("poll", { session_id })).toMatchObject({ ok: { status: "running", command: "python3 -m http.server 8765 --bind 127.0.0.1" } });
    // Background processes stop with the app: the VM they ran in stops with it.
    expect(existsSync(join(vmRun(), "control.sock"))).toBe(true);
    await quit(app);
    app = undefined;
    expect(existsSync(vmRun())).toBe(false);
  });
});

describe.skipIf(process.env.SUROGATE_VM_TESTS !== "1")("the VM's processes through the app, each killed in turn", () => {
  const pidIn = (file: string) => Number(readFileSync(join(vmRun(), file), "utf8"));
  const newestShare = () => Math.max(...readdirSync(vmRun()).map((name) => Number(/^vfs-(\d+)\.pid$/.exec(name)?.[1] ?? 0)));
  // A process's parent, from /proc: QEMU's is the VM manager, which execs it through setpriv.
  const parentOf = (pid: number) => Number(readFileSync(`/proc/${pid}/stat`, "utf8").split(") ")[1]?.split(" ")[1]);
  const alive = (pid: number) => {
    try {
      process.kill(pid, 0);
      return true;
    } catch {
      return false;
    }
  };
  const run = (command: string) => operation("run", { command, workdir: null, timeout: 60 });

  // Four kills, each with a boot and a backoff of 1–2 s: past vitest's 60 s on a busy machine.
  it("answers the command each one stopped as interrupted, and runs the next: QEMU, the folder's virtiofsd, the VM manager and the file host", { timeout: 180_000 }, async () => {
    const folder = join(home, "kills");
    mkdirSync(folder);
    await bound(folder);
    expect(await run("true")).toMatchObject({ ok: { returncode: 0 } });
    const main = app!.process().pid!;
    const victims: Array<[string, () => number]> = [
      ["QEMU", () => pidIn("qemu.pid")],
      ["the folder's virtiofsd", () => pidIn(`vfs-${newestShare()}.pid`)],
      ["the VM manager", () => parentOf(pidIn("qemu.pid"))],
      ["the file host", () => Number(spawnSync("pgrep", ["-P", String(main), "-f", "dist/hosts/host.js"], { encoding: "utf8" }).stdout.trim().split("\n")[0])],
    ];
    for (const [n, [name, pidOf]] of victims.entries()) {
      const running = run(`touch started-${n}; sleep 30`);
      await expect.poll(() => existsSync(join(folder, `started-${n}`)), { timeout: 30_000 }).toBe(true);
      process.kill(pidOf(), "SIGKILL");
      expect([name, await running]).toEqual([name, { error: expect.objectContaining({ type: "interrupted" }) }]);
      expect([name, await run(`echo back from ${n}`)]).toEqual([name, { ok: { output: `back from ${n}\n`, returncode: 0, timed_out: false } }]);
    }
  });

  it("leaves nothing of its VM running when the app itself is killed, and runs the next command at its next launch", async () => {
    const folder = join(home, "crash");
    mkdirSync(folder);
    await bound(folder);
    expect(await run("true")).toMatchObject({ ok: { returncode: 0 } });
    const qemu = pidIn("qemu.pid");
    const vm = [qemu, parentOf(qemu), pidIn(`vfs-${newestShare()}.pid`)];
    const connected = agent.link.hellos.length;
    app!.process().kill("SIGKILL");
    await expect.poll(() => vm.filter(alive), { timeout: 10_000 }).toEqual([]);
    app = await launch(home, { XDG_RUNTIME_DIR: runtime, SUROGATE_VM_IMAGE: IMAGE });
    await expect.poll(() => agent.link.hellos.length, { timeout: 30_000 }).toBe(connected + 1);
    expect(await run("echo again")).toEqual({ ok: { output: "again\n", returncode: 0, timed_out: false } });
  });

  it("counts a chat whose background process runs in the VM as working at the quit, and the process ends with the app", async () => {
    const folder = join(home, "server");
    mkdirSync(folder);
    await bound(folder);
    expect(await operation("start", {
      command: "sleep 3600", workdir: null, task_id: "demo", pty: false, notify_on_complete: false, watcher_interval: null,
    })).toMatchObject({ ok: { session_id: expect.any(String) } });
    await app!.evaluate(() => Object.assign(globalThis, { hold: true, answer: 0 }));
    void app!.evaluate(({ app: electron }) => electron.quit()).catch(() => {});
    await expect.poll(() => app!.evaluate(() => (globalThis as unknown as { asked: Array<{ detail?: string }> }).asked.at(-1)?.detail)).toBe(
      "1 thread is working on this computer. Quitting now will interrupt that work.",
    );
    const qemu = pidIn("qemu.pid");
    const closed = app!.waitForEvent("close");
    await app!.evaluate(() => (globalThis as unknown as { release(): void }).release());
    await closed;
    app = undefined;
    expect(alive(qemu)).toBe(false);
    expect(existsSync(vmRun())).toBe(false);
  });
});
