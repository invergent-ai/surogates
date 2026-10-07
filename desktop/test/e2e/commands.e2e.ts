// An agent's commands in a folder of this computer, through the real app: the
// shell's device, its binder and the VmExecutor, the VM manager in its utility
// process, and the guest. Behind SUROGATE_VM_TESTS=1: KVM, QEMU, virtiofsd, the
// image images/guest/build.sh makes (or SUROGATE_VM_IMAGE's folder), and
// npm run agent-disk.

import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import type { ElectronApplication, Page } from "playwright-core";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { vmOptions } from "../../src/vm/client.js";
import { connect, FakeAgent, signedInAndAdded, webClient } from "./fake-agent.js";
import { dataHome, launch, quit, shellPage, stubNative } from "./launch.js";

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
  await app?.evaluate((_electron, picked) => Object.assign(globalThis, { folder: picked, answer: 0 }), folder);
  const prepared = await client.evaluate(() => window.surogateDesktop!.prepareFolder("pick")) as { folder: string; nonce: string };
  expect(prepared.folder).toBe(folder);
  expect(await operation("bind", { folder, nonce: prepared.nonce }, "bind", 0, chat)).toEqual({ ok: null });
}

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
    // The folder's git config is read-only in the guest: git there runs no code the agent wrote.
    const config = readFileSync(join(first, ".git", "config"), "utf8");
    expect(await run("(echo '[core]\n\tfsmonitor = ./evil' >> .git/config) 2>&1 | sed 's/.*: //'")).toEqual(ran("Read-only file system\n"));
    expect(readFileSync(join(first, ".git", "config"), "utf8")).toBe(config);
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
