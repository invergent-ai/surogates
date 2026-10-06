// An agent's commands in a folder of this computer, through the real app: the
// shell's device, its binder and the VmExecutor, the VM manager in its utility
// process, and the guest. Behind SUROGATE_VM_TESTS=1: KVM, QEMU, virtiofsd, the
// image images/guest/build.sh makes (or SUROGATE_VM_IMAGE's folder), and
// npm run agent-disk.

import { existsSync, mkdirSync, mkdtempSync, rmSync, statSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import type { ElectronApplication } from "playwright-core";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { vmOptions } from "../../src/vm/client.js";
import { connect, FakeAgent, register, webClient } from "./fake-agent.js";
import { dataHome, launch, quit, shellPage, stubNative } from "./launch.js";

const IMAGE = process.env.SUROGATE_VM_IMAGE ?? fileURLToPath(new URL("../../../images/guest/out", import.meta.url));
const CHAT = "4e5f6a7b-8c9d-4e0f-a1b2-c3d4e5f6a7b8";

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

// The chat's operation, as the server sends it, and the app's result for it.
async function operation(kind: string, args: Record<string, unknown>, invocation = "call", ordinal = 1): Promise<unknown> {
  const id = `op-${(next += 1)}`;
  agent.link.send({
    type: "op", id, session_id: CHAT, calling_session_id: CHAT, invocation_id: invocation, ordinal, kind, args, digest: `d-${id}`,
  });
  let result: Record<string, unknown> | undefined;
  await expect.poll(() => {
    result = agent.link.received.find((frame) => frame.type === "op_result" && frame.id === id);
    return result !== undefined;
  }, { timeout: 30_000 }).toBe(true);
  agent.link.send({ type: "op_ack", id });
  return result?.outcome;
}

describe.skipIf(process.env.SUROGATE_VM_TESTS !== "1")("commands through the app", () => {
  it("binds a folder the user picked, and runs the agent's commands in it, in the VM", async () => {
    const folder = join(home, "project");
    mkdirSync(folder);
    writeFileSync(join(folder, "notes.txt"), "alpha\n");
    const origin = await agent.start();
    app = await launch(home, { XDG_RUNTIME_DIR: runtime, SUROGATE_VM_IMAGE: IMAGE });
    await stubNative(app);
    await connect(await shellPage(app), origin);
    const client = await webClient(app, origin);
    await register(client);
    // The user picks the folder in the system's dialog, then Use this folder in the sheet.
    await app.evaluate((_electron, picked) => Object.assign(globalThis, { folder: picked, answer: 0 }), folder);
    const prepared = await client.evaluate(() => window.surogateDesktop!.prepareFolder("pick")) as { folder: string; nonce: string };
    expect(prepared.folder).toBe(folder);
    expect(await operation("bind", { folder, nonce: prepared.nonce }, "bind", 0)).toEqual({ ok: null });
    expect(await operation("run", { command: "cat notes.txt; pwd; id -u", workdir: null, timeout: 30 })).toEqual({
      ok: { output: `alpha\n${folder}\n10000\n`, returncode: 0, timed_out: false },
    });
    expect(await operation("which", { name: "pandoc" })).toEqual({ ok: true });
    // The file kinds stay on the host, in the helper's sandbox, and see what the command wrote.
    expect(await operation("run", { command: "echo made > made.txt", workdir: null, timeout: 30 })).toMatchObject({ ok: { returncode: 0 } });
    expect(await operation("read", { key: join(folder, "made.txt"), max_bytes: null })).toEqual({ ok: Buffer.from("made\n").toString("base64") });
    expect(statSync(join(folder, "made.txt")).uid).toBe(process.getuid?.());
    // The app's quit stops the VM itself, which takes its sockets with it; a VM that only died with the app would leave them.
    const { run } = vmOptions(join(home, "surogate"), { uid: 0, gid: 0, name: "", home: "" }, { XDG_RUNTIME_DIR: runtime });
    expect(existsSync(join(run, "control.sock"))).toBe(true);
    await quit(app);
    app = undefined;
    expect(existsSync(run)).toBe(false);
  });
});
