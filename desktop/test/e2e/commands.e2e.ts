// An agent's commands in a folder of this computer, through the real app: the
// shell's device, its binder and the VmExecutor, the VM manager in its utility
// process, and the guest. Behind SUROGATE_VM_TESTS=1: KVM, QEMU, virtiofsd, the
// image images/guest/build.sh makes (or SUROGATE_VM_IMAGE's folder), and
// npm run agent-disk.

import { spawnSync } from "node:child_process";
import {
  chmodSync, existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, readlinkSync, realpathSync, rmSync, statSync, truncateSync, writeFileSync,
} from "node:fs";
import { open } from "node:fs/promises";
import { createServer } from "node:http";
import type { AddressInfo } from "node:net";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { FuseState, FuseV1Options, getCurrentFuseWire } from "@electron/fuses";
import type { ElectronApplication, Page } from "playwright-core";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { NODE } from "../../src/hosts/tool-hosts.js";
import { REPO_IMAGE, vmOptions } from "../../src/vm/client.js";
import { readManifest } from "../../src/vm/image.js";
import { EMULATED_NOTICE } from "../../src/vm/manager.js";
import { connect, FakeAgent, signedInAndAdded, webClient } from "./fake-agent.js";
import { dataHome, ELECTRON, launch, press, prompt, promptsShown, quit, shellPage, stubNative } from "./launch.js";

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
  // An emulated guest's first command comes about 20 s after its launch here.
  await expect.poll(() => {
    result = agent.link.received.find((frame) => frame.type === "op_result" && frame.id === id);
    return result !== undefined;
  }, { timeout: 120_000 }).toBe(true);
  agent.link.send({ type: "op_ack", id });
  return result?.outcome;
}

// The app launched and signed in, and *folder* bound to the chat as the user picked it.
async function bound(folder: string): Promise<void> {
  await bind(await launched(), folder);
}

// The app launched and signed in, with *env* in its environment: the agent's web client in it.
async function launched(env: Record<string, string> = {}): Promise<Page> {
  const origin = await agent.start();
  app = await launch(home, { XDG_RUNTIME_DIR: runtime, SUROGATE_VM_IMAGE: IMAGE, ...env });
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

  it("runs its file host and file helper on the app's own node, never Electron as Node, and a command in the VM reads what they wrote", async () => {
    // This package's Electron is the app's: RunAsNode is off, as npm run electron:install sets it.
    expect((await getCurrentFuseWire(ELECTRON))[FuseV1Options.RunAsNode]).toBe(FuseState.DISABLE);
    const folder = join(home, "node");
    mkdirSync(folder);
    // Started as VS Code's terminals start it, ELECTRON_RUN_AS_NODE exported: it is the app all the same.
    await bind(await launched({ ELECTRON_RUN_AS_NODE: "1" }), folder);
    const written = Buffer.from("written on the app's node\n").toString("base64");
    expect(await operation("write", { key: join(folder, "notes.txt"), data: written })).toEqual({ ok: null });
    expect(await operation("run", { command: "cat notes.txt", workdir: null, timeout: 30 })).toEqual({
      ok: { output: "written on the app's node\n", returncode: 0, timed_out: false },
    });
    // Every process of the app's, by the program it runs: the file host and its helper in srt run
    // the app's node, and Electron runs only as itself, its own children each with a --type.
    // Each thread's children: Chromium starts its utility processes from a thread of its own.
    const main = app!.process().pid!;
    const tree = (pid: number): number[] => [pid, ...readdirSync(`/proc/${pid}/task`).flatMap((tid) =>
      readFileSync(`/proc/${pid}/task/${tid}/children`, "utf8").trim().split(" ").filter(Boolean).map(Number)).flatMap(tree)];
    const processes = tree(main).flatMap((pid) => {
      try {
        // Chromium writes its processes' command lines over as one string: read as words.
        return [{
          pid, exe: readlinkSync(`/proc/${pid}/exe`), args: readFileSync(`/proc/${pid}/cmdline`, "utf8").split(/[\0 ]/),
          environ: readFileSync(`/proc/${pid}/environ`, "utf8").split("\0"),
        }];
      } catch {
        return []; // gone meanwhile
      }
    });
    const onNode = processes.filter(({ exe }) => exe === realpathSync(NODE)).map(({ args }) => args.find((arg) => arg.endsWith(".js"))?.replace(/^.*\/dist\//, ""));
    expect(onNode.sort()).toEqual(["files/helper.js", "hosts/host.js"]);
    // Neither opens an inspector on SIGUSR1: the app's node has no fuse to refuse it.
    expect(processes.filter(({ exe }) => exe === realpathSync(NODE)).map(({ args }) => args.includes("--disable-sigusr1"))).toEqual([true, true]);
    // Nothing the app starts takes ELECTRON_RUN_AS_NODE from it: its main drops it before it starts any.
    // The main's own /proc environ is the one it was started with, which keeps it.
    const inheriting = processes.filter(({ pid, environ }) => pid !== main && environ.some((entry) => entry.startsWith("ELECTRON_RUN_AS_NODE=")));
    expect(inheriting.map(({ exe, args }) => `${exe.split("/").at(-1)} ${args.find((arg) => arg.startsWith("--type=")) ?? ""}`.trim())).toEqual([]);
    const plainElectron = processes.filter(({ exe, args }) => exe === realpathSync(ELECTRON) && !args.some((arg) => arg.startsWith("--type=")));
    expect(plainElectron.map(({ pid }) => pid)).toEqual([main]);
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
    // Open while the chat is allowed the site: it shows at once.
    const settings = await foldersSettings();
    const reached = run(status("https://example.com/"));
    await expect.poll(() => promptsShown(app!), { timeout: 30_000 }).toBe(1);
    await press(await prompt(app!), "allow_session");
    expect(await reached).toMatchObject({ ok: { returncode: 0 } });
    await expect.poll(() => settings.textContent("#folders .row .line")).toBe("Reaches example.com, on every portTake back");
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

  it("refuses a Take back for any host Settings does not show as the chat's own, and says so in the page: one never allowed, one of a chat not bound here, and one taken back", async () => {
    const folder = join(home, "site");
    mkdirSync(folder);
    await bound(folder);
    const reached = operation("run", { command: status("https://example.com/"), workdir: null, timeout: 60 });
    await expect.poll(() => promptsShown(app!), { timeout: 30_000 }).toBe(1);
    await press(await prompt(app!), "allow_session");
    expect(await reached).toMatchObject({ ok: { returncode: 0 } });
    const settings = await foldersSettings();
    // Settings' own call, as a page talked into it would make it: what it was told.
    const takeBack = (root: unknown, host: unknown) => settings.evaluate(([at, from]) =>
      (window as unknown as { surogateSettings: { takeBack(root: unknown, host: unknown): Promise<void> } }).surogateSettings.takeBack(at, from)
        .then(() => "taken back", (error: Error) => error.message), [root, host]);
    const refused = "Error invoking remote method 'settings:take-back': Error: This chat cannot reach that host";
    expect(await takeBack(CHAT, "example.org")).toBe(refused);
    expect(await takeBack(OTHER, "example.com")).toBe(refused);
    expect(await takeBack(42, "example.com")).toBe(refused);
    expect(await takeBack(CHAT, ["example.com"])).toBe(refused);
    expect(await takeBack(CHAT, "example.com")).toBe("taken back");
    expect(await takeBack(CHAT, "example.com")).toBe(refused);
    // Its button, drawn before the host was taken back: the page catches the refusal, says so, and draws the list again.
    await settings.evaluate(() => {
      const rejections: string[] = [];
      Object.assign(window, { rejections });
      window.addEventListener("unhandledrejection", (event) => rejections.push(String(event.reason)));
    });
    await settings.click("#folders .row .line button");
    await expect.poll(() => settings.textContent("#folders-failed")).toBe("Surogate did not take back example.com: This chat cannot reach that host.");
    expect(await settings.isVisible("#folders-failed")).toBe(true);
    await expect.poll(() => settings.$$("#folders .row .line").then((lines) => lines.length)).toBe(0);
    expect(await settings.evaluate(() => (window as unknown as { rejections: string[] }).rejections)).toEqual([]);
  });

  it("stops a chat's background process from Settings, as the agent's own kill would, and no longer shows it", async () => {
    const folder = join(home, "watch");
    mkdirSync(folder);
    await bound(folder);
    // A command the agent wrote with a right-to-left override in it: shown as text.
    const started = await operation("start", {
      command: "sleep 600 #‮txt", workdir: null, task_id: null, pty: false, notify_on_complete: false, watcher_interval: null,
    }) as { ok: { session_id: string } };
    const settings = await foldersSettings();
    await expect.poll(() => settings.textContent("#folders .row .line")).toBe("Runs sleep 600 #U+202EtxtStop");
    // Named as it is shown.
    expect(await settings.getAttribute("#folders .row .line button", "aria-label")).toBe("Stop sleep 600 #U+202Etxt");
    await settings.click("#folders .row .line button");
    await expect.poll(() => settings.$$("#folders .row .line").then((lines) => lines.length)).toBe(0);
    // The agent finds it ended at its next look, as after its own kill.
    expect(await operation("poll", { session_id: started.ok.session_id })).toMatchObject({ ok: { status: "exited" } });
  });

  it("refuses a Stop for any process Settings does not show as the chat's own: another chat's, one it never ran, and one of a chat no longer bound here", async () => {
    const [first, second] = [join(home, "first"), join(home, "second")];
    for (const folder of [first, second]) mkdirSync(folder);
    const client = await launched();
    await bind(client, first);
    await bind(client, second, OTHER);
    const start = async (chat: string) => (await operation("start", {
      command: "sleep 600", workdir: null, task_id: null, pty: false, notify_on_complete: false, watcher_interval: null,
    }, "call", 1, chat) as { ok: { session_id: string } }).ok.session_id;
    const [mine, theirs] = [await start(CHAT), await start(OTHER)];
    const settings = await foldersSettings();
    await expect.poll(() => settings.$$("#folders .row .line").then((lines) => lines.length)).toBe(2);
    // Settings' own call, as a page talked into it would make it: what it was told.
    const stop = (root: unknown, id: unknown) => settings.evaluate(([at, process]) =>
      (window as unknown as { surogateSettings: { stop(root: unknown, id: unknown): Promise<void> } }).surogateSettings.stop(at, process)
        .then(() => "stopped", (error: Error) => error.message), [root, id]);
    const refused = "Error invoking remote method 'settings:stop': Error: This chat runs no such process";
    expect(await stop(CHAT, theirs)).toBe(refused);
    expect(await stop(CHAT, "proc_000000000000")).toBe(refused);
    expect(await stop(42, mine)).toBe(refused);
    expect(await stop(CHAT, [mine])).toBe(refused);
    for (const [chat, id] of [[CHAT, mine], [OTHER, theirs]] as const) {
      expect(await operation("poll", { session_id: id }, "call", 1, chat)).toMatchObject({ ok: { status: "running" } });
    }
    // A chat deleted meanwhile: its binding goes, while its process is still alive in the VM.
    expect(await operation("retire", {}, "retire", 0, OTHER)).toEqual({ ok: null });
    expect(await stop(OTHER, theirs)).toBe(refused);
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

// The install's base, serving the image this build's manifest names as R2 serves a release's,
// <base>/desktop/vm/<key>/<file>, with a Range; and the install record that names it. While
// *held*, rootfs.img.zst stops after its first 64 MiB; while *missing*, every file is a 404.
// *heard*: each request for a file, its Range and the cookie it carried.
async function installedFrom(state: { held: boolean; missing: boolean }) {
  const { key } = readManifest(join(REPO_IMAGE, "manifest.json"));
  const heard: Array<{ name: string; range: string | undefined; cookie: string | undefined }> = [];
  let release = () => {};
  const released = new Promise<void>((resolve) => {
    release = resolve;
  });
  const server = createServer((request, response) => void (async () => {
    const name = request.url?.startsWith(`/desktop/vm/${key}/`) ? request.url.slice(`/desktop/vm/${key}/`.length) : "";
    heard.push({ name, range: request.headers.range, cookie: request.headers.cookie });
    if (state.missing || !["rootfs.img.zst", "vmlinuz.zst"].includes(name)) return void response.writeHead(404).end();
    const file = await open(join(REPO_IMAGE, name));
    const { size } = await file.stat();
    const from = Number(/^bytes=(\d+)-$/.exec(request.headers.range ?? "")?.[1] ?? 0);
    response.writeHead(from > 0 ? 206 : 200, { "content-length": size - from, ...(from > 0 ? { "content-range": `bytes ${from}-${size - 1}/${size}` } : {}) });
    const chunk = Buffer.alloc(1024 * 1024);
    for (let at = from; at < size && !response.destroyed;) {
      if (state.held && name === "rootfs.img.zst" && at >= 64 * 1024 * 1024) await released;
      const { bytesRead } = await file.read(chunk, 0, chunk.length, at);
      if (!response.write(chunk.subarray(0, bytesRead))) await new Promise((resolve) => response.once("drain", resolve));
      at += bytesRead;
    }
    response.end();
    await file.close();
  })());
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  const record = join(home, "install.json");
  writeFileSync(record, JSON.stringify({ base }));
  servers.push(server);
  return { key, base, host: new URL(base).host, record, release, heard };
}
const servers: Array<ReturnType<typeof createServer>> = [];
afterEach(async () => {
  for (const server of servers.splice(0)) {
    server.closeAllConnections();
    await new Promise((resolve) => server.close(resolve));
  }
});

// The Settings dialog's page, once it is open over the window, on This computer.
async function thisComputer(shell: ElectronApplication): Promise<Page> {
  let found: Page | undefined;
  await expect.poll(() => {
    found = shell.windows().find((page) => page.url().endsWith("/settings.html"));
    return found !== undefined;
  }).toBe(true);
  await found!.waitForSelector('.settings-nav [data-section="computer"]');
  await found!.click('.settings-nav [data-section="computer"]');
  return found!;
}

const NO_KVM = "This computer has no hardware virtualization, so Surogate runs the agent's commands emulated. They work, but several times slower. "
  + "Turning on virtualization (VT-x or AMD-V) in the computer's firmware settings makes them fast";

describe.skipIf(process.env.SUROGATE_VM_TESTS !== "1")("the sandbox's delivery and status, through the app", () => {
  it("downloads its image at the first launch from where Surogate was installed from, says how far it has come, and runs the agent's command on it", { timeout: 180_000 }, async () => {
    const served = await installedFrom({ held: true, missing: false });
    // An older version's image, which goes once this one has booted.
    const images = join(home, "surogate", "vm", "images");
    mkdirSync(join(images, "0".repeat(64)), { recursive: true });
    const folder = join(home, "delivered");
    mkdirSync(folder);
    const client = await launched({ SUROGATE_VM_IMAGE: "", SUROGATE_INSTALL_JSON: served.record });
    const page = await shellPage(app!);
    await expect.poll(() => page.textContent("#sandbox-text"), { timeout: 30_000 }).toMatch(/^Downloading the sandbox for the agent's commands: (\d|1\d)%$/);
    // A screen reader hears the state, never each percent: the percent is outside the live region.
    expect([await page.textContent("#sandbox-said"), await page.getAttribute("#sandbox-said", "role"), await page.getAttribute("#sandbox-text", "role")])
      .toEqual(["Downloading the sandbox for the agent's commands", "status", null]);
    await bind(client, folder);
    // The command waits for the image; the rest of it comes a moment later.
    const ran = operation("run", { command: "echo delivered", workdir: null, timeout: 30 });
    setTimeout(served.release, 1_000);
    expect(await ran).toEqual({ ok: { output: "delivered\n", returncode: 0, timed_out: false } });
    expect(readdirSync(images)).toEqual([served.key]);
    await expect.poll(() => page.isHidden("#sandbox")).toBe(true);
    await page.click("#open-settings");
    expect(await (await thisComputer(app!)).textContent("#sandbox")).toBe("Ready");
  });

  it("says it could not download its image, with Retry, answers the agent why, and downloads it at Retry", { timeout: 180_000 }, async () => {
    const state = { held: false, missing: true };
    const served = await installedFrom(state);
    const folder = join(home, "retried");
    mkdirSync(folder);
    const client = await launched({ SUROGATE_VM_IMAGE: "", SUROGATE_INSTALL_JSON: served.record });
    const page = await shellPage(app!);
    await expect.poll(() => page.textContent("#sandbox-text")).toBe(`Surogate could not download its sandbox: ${served.host} answered 404 for rootfs.img.zst`);
    expect(await page.textContent("#sandbox-retry")).toBe("Retry");
    expect(await page.isHidden("#sandbox-log")).toBe(true);
    await bind(client, folder);
    expect(await operation("run", { command: "echo retried", workdir: null, timeout: 30 })).toEqual({
      error: { type: "unavailable", message: `This computer's sandbox could not be downloaded: ${served.host} answered 404 for rootfs.img.zst` },
    });
    state.missing = false;
    await page.click("#sandbox-retry");
    await expect.poll(() => page.isHidden("#sandbox"), { timeout: 60_000 }).toBe(true);
    expect(await operation("run", { command: "echo retried", workdir: null, timeout: 30 })).toEqual({ ok: { output: "retried\n", returncode: 0, timed_out: false } });
  });

  it("stops a download that nothing comes for in 30 s, with Retry, answers the agent why, and resumes it at Retry with no cookie of the app's", { timeout: 240_000 }, async () => {
    const served = await installedFrom({ held: true, missing: false });
    const folder = join(home, "stalled");
    mkdirSync(folder);
    const client = await launched({ SUROGATE_VM_IMAGE: "", SUROGATE_INSTALL_JSON: served.record });
    // A cookie for the install's base in the app's own session, as a page it showed could set.
    await app!.evaluate(({ session }, url) => session.defaultSession.cookies.set({ url, name: "who", value: "the-user" }), served.base);
    const page = await shellPage(app!);
    await bind(client, folder);
    const stopped = "the download of rootfs.img.zst stopped: nothing came for 30 s";
    expect(await operation("run", { command: "echo resumed", workdir: null, timeout: 30 })).toEqual({
      error: { type: "unavailable", message: `This computer's sandbox could not be downloaded: ${stopped}` },
    });
    expect(await page.textContent("#sandbox-text")).toBe(`Surogate could not download its sandbox: ${stopped}`);
    served.release();
    await page.click("#sandbox-retry");
    await expect.poll(() => page.isHidden("#sandbox"), { timeout: 60_000 }).toBe(true);
    // The rest of what had come, asked for with a Range.
    expect(served.heard.filter(({ name }) => name === "rootfs.img.zst").map(({ range }) => range)).toEqual([undefined, expect.stringMatching(/^bytes=[1-9]\d*-$/)]);
    expect(served.heard.filter(({ cookie }) => cookie !== undefined)).toEqual([]);
    expect(await operation("run", { command: "echo resumed", workdir: null, timeout: 30 })).toEqual({ ok: { output: "resumed\n", returncode: 0, timed_out: false } });
  });

  it("stops its image's unpack with the app's quit, leaving no zstd behind", { timeout: 180_000 }, async () => {
    const served = await installedFrom({ held: false, missing: false });
    await launched({ SUROGATE_VM_IMAGE: "", SUROGATE_INSTALL_JSON: served.record });
    const unpacking = () => Number(spawnSync("pgrep", ["-f", `^/usr/bin/zstd .*${join(home, "surogate", "vm", "images", `${served.key}.partial`)}/`], {
      encoding: "utf8",
    }).stdout.trim().split("\n")[0] || 0);
    await expect.poll(unpacking, { timeout: 60_000, interval: 50 }).toBeGreaterThan(0);
    const zstd = unpacking();
    const closed = app!.waitForEvent("close");
    await app!.evaluate(({ app: electron }) => electron.quit());
    // It goes with the quit, not seconds later once it has written the image out.
    await expect.poll(() => existsSync(`/proc/${zstd}`), { timeout: 1_000, interval: 20 }).toBe(false);
    await closed;
    app = undefined;
  });

  it("says its sandbox did not start on the image it delivered, with Show log and Retry, and at Retry checks the image and downloads it again", { timeout: 240_000 }, async () => {
    const served = await installedFrom({ held: false, missing: false });
    // An image's folder whole by its sizes and its last step, and nothing but zeros: no kernel to boot.
    const image = join(home, "surogate", "vm", "images", served.key);
    mkdirSync(image, { recursive: true });
    for (const file of readManifest(join(REPO_IMAGE, "manifest.json")).files) {
      writeFileSync(join(image, file.name), "");
      truncateSync(join(image, file.name), file.size);
    }
    writeFileSync(join(image, "complete"), `${served.key}\n`);
    const folder = join(home, "damaged");
    mkdirSync(folder);
    const client = await launched({ SUROGATE_VM_IMAGE: "", SUROGATE_INSTALL_JSON: served.record });
    const page = await shellPage(app!);
    await bind(client, folder);
    const outcome = await operation("run", { command: "true", workdir: null, timeout: 30 }) as { error: { message: string } };
    expect(outcome.error.message).toMatch(/^This computer's sandbox did not start: /);
    expect(served.heard).toEqual([]);
    await expect.poll(() => page.textContent("#sandbox-text")).toBe(outcome.error.message);
    expect([await page.textContent("#sandbox-log"), await page.textContent("#sandbox-retry")]).toEqual(["Show log", "Retry"]);
    await page.click("#sandbox-retry");
    await expect.poll(() => page.isHidden("#sandbox"), { timeout: 90_000 }).toBe(true);
    expect(served.heard.map(({ name }) => name).sort()).toEqual(["rootfs.img.zst", "vmlinuz.zst"]);
    expect(await operation("run", { command: "echo checked", workdir: null, timeout: 30 })).toEqual({ ok: { output: "checked\n", returncode: 0, timed_out: false } });
  });

  it("says why the agent's commands run emulated for as long as they do, in the sidebar and in Settings, and tells the agent once", { timeout: 240_000 }, async () => {
    const folder = join(home, "emulated");
    mkdirSync(folder);
    const client = await launched({ SUROGATE_VM_KVM: join(home, "no-kvm") });
    await bind(client, folder);
    const ran = (output: string) => ({ ok: { output, returncode: 0, timed_out: false } });
    expect(await operation("run", { command: "echo slow", workdir: null, timeout: 60 })).toEqual(ran(`slow\n\n${EMULATED_NOTICE}`));
    expect(await operation("run", { command: "echo slow", workdir: null, timeout: 60 })).toEqual(ran("slow\n"));
    const page = await shellPage(app!);
    expect(await page.textContent("#sandbox-text")).toBe(NO_KVM);
    expect([await page.isHidden("#sandbox-log"), await page.isHidden("#sandbox-retry")]).toEqual([true, true]);
    await page.click("#open-settings");
    expect(await (await thisComputer(app!)).textContent("#sandbox")).toBe(NO_KVM);
  });

  it("says its sandbox did not start, with QEMU's words, and Show log opens the guest's console", async () => {
    const folder = join(home, "unstarted");
    mkdirSync(folder);
    const empty = mkdtempSync(join(home, "no-image-"));
    const client = await launched({ SUROGATE_VM_IMAGE: empty });
    await app!.evaluate(({ shell }) => {
      const opened: string[] = [];
      Object.assign(globalThis, { openedPaths: opened });
      shell.openPath = (path: string) => {
        opened.push(path);
        return Promise.resolve("");
      };
    });
    await bind(client, folder);
    const outcome = await operation("run", { command: "true", workdir: null, timeout: 30 }) as { error: { message: string } };
    // QEMU's own words: it opens its sockets, then exits on the disk it cannot open.
    expect(outcome.error.message).toMatch(/^This computer's sandbox did not start: QEMU.*: Could not open '[^']*\/rootfs\.img': No such file or directory$/);
    const page = await shellPage(app!);
    await expect.poll(() => page.textContent("#sandbox-text")).toBe(outcome.error.message);
    // An image of the build's own, not the app's to check: no Retry.
    expect([await page.textContent("#sandbox-log"), await page.isHidden("#sandbox-retry")]).toEqual(["Show log", true]);
    await page.click("#sandbox-log");
    await expect.poll(() => app!.evaluate(() => (globalThis as unknown as { openedPaths: string[] }).openedPaths)).toEqual([
      join(home, "surogate", "logs", "vm-console.log"),
    ]);
  });
});

describe("the sandbox's tools, through the app", () => {
  it("says Surogate's sandbox tools are missing while QEMU is older than it needs, answers the agent so, and looks again at Check again", async () => {
    const bin = mkdtempSync(join(home, "bin-"));
    writeFileSync(join(bin, "qemu-system-x86_64"), "#!/bin/sh\necho 'QEMU emulator version 7.2.0 (Debian 1:7.2+dfsg-7)'\n");
    chmodSync(join(bin, "qemu-system-x86_64"), 0o755);
    const folder = join(home, "tools");
    mkdirSync(folder);
    const client = await launched({ PATH: `${bin}:${process.env.PATH ?? ""}` });
    const page = await shellPage(app!);
    const missing = "Surogate's sandbox tools are missing. Run the install script again. It lacks QEMU 8.2 or later";
    await expect.poll(() => page.textContent("#sandbox-text")).toBe(missing);
    await bind(client, folder);
    expect(await operation("run", { command: "true", workdir: null, timeout: 30 })).toEqual({
      error: { type: "unavailable", message: `This computer's sandbox cannot start: ${missing}` },
    });
    // The install script has put this computer's own QEMU in its place: looked for again, with no restart.
    rmSync(join(bin, "qemu-system-x86_64"));
    expect(await page.textContent("#sandbox-check")).toBe("Check again");
    await page.click("#sandbox-check");
    await expect.poll(() => page.isHidden("#sandbox")).toBe(true);
  });
});
