// The file helper in srt's Seatbelt on macOS, real: what a program in its sandbox reaches of the computer (srt-probe.mjs),
// with the policy the host gives it, and the host itself, on the app's node (bin/node), serving the file kinds in a
// folder. Which of the system's folders the helper's node and rg need is measured as well, and written out. On a Mac
// alone: the policy's darwin side is policy-darwin.test.ts's, on any OS.

import { spawnSync } from "node:child_process";
import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, statSync, writeFileSync } from "node:fs";
import { createServer, type Server } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { SandboxManager, type SandboxRuntimeConfig } from "@anthropic-ai/sandbox-runtime";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { FSAT } from "../src/files/fsat.js";
import type { HostStart } from "../src/hosts/messages.js";
import { quote, sandboxPolicy } from "../src/hosts/policy.js";
import { srtOs } from "../src/hosts/srt-os.js";
import { bound, Harness, PACKAGE } from "./host-harness.js";

const PROBE = fileURLToPath(new URL("./srt-probe.mjs", import.meta.url));
const NODE = join(PACKAGE, "bin", "node");
const RG = join(PACKAGE, "bin", "rg");

describe.runIf(process.platform === "darwin")("the file helper in srt's Seatbelt", { timeout: 120_000 }, () => {
  let base: string;
  let at: Record<string, string | number>;
  let policy: SandboxRuntimeConfig;
  let unix: Server;
  let tcp: Server;
  const own = { tmpdir: process.env.TMPDIR, cwd: process.cwd() };

  beforeAll(async () => {
    base = realpathSync(mkdtempSync(join(tmpdir(), "srt-darwin-")));
    const [folder, other, home, tmp, srt] = ["folder", "other", "home", "work", "srt"].map((name) => join(base, name));
    for (const dir of [folder, other, join(home!, ".ssh"), tmp, srt]) mkdirSync(dir!, { recursive: true });
    writeFileSync(join(folder!, "inside.txt"), "inside\n");
    writeFileSync(join(other!, "secret.txt"), "secret\n");
    writeFileSync(join(home!, ".ssh", "id_ed25519"), "a key\n", { mode: 0o600 });
    // Native code a command could have put in the folder.
    copyFileSync(FSAT, join(folder!, "copy.node"));
    unix = createServer((socket) => socket.end("reached\n"));
    await new Promise<void>((resolve) => unix.listen(join(base, "outside.sock"), resolve));
    tcp = createServer((socket) => socket.end("reached\n"));
    await new Promise<void>((resolve) => tcp.listen(0, "127.0.0.1", resolve));
    at = { folder: folder!, other: other!, home: home!, tmp: tmp!, sock: join(base, "outside.sock"), port: (tcp.address() as { port: number }).port, fsat: FSAT, rg: RG };
    policy = sandboxPolicy({ folder: folder!, tmp: tmp!, appDirs: [dirname(process.execPath), PACKAGE] });
    // As the host points it: srt's own files in a folder of their own.
    process.env.TMPDIR = srt;
    await SandboxManager.initialize(policy, () => Promise.resolve(false));
    process.chdir(tmp!);
  });

  afterAll(async () => {
    await SandboxManager.reset().catch(() => {});
    process.chdir(own.cwd);
    if (own.tmpdir === undefined) delete process.env.TMPDIR;
    else process.env.TMPDIR = own.tmpdir;
    unix?.close();
    tcp?.close();
    rmSync(base, { recursive: true, force: true });
  });

  // The probe in the sandbox of *config*, as the host wraps the helper: what it said, or how it failed.
  async function probe(config: SandboxRuntimeConfig = policy, line = srtOs.line): Promise<{ said: Record<string, string> | null; status: number | null; stderr: string }> {
    SandboxManager.updateConfig(config);
    const { argv } = await SandboxManager.wrapWithSandboxArgv(`${quote(process.execPath)} ${quote(PROBE)} ${quote(JSON.stringify(at))}`);
    const [file, flag, wrapped] = argv;
    const ran = spawnSync(file!, ["--norc", "--noprofile", flag!, line(wrapped!)], {
      cwd: String(at.folder), env: { HOME: String(at.home), PATH: "/usr/bin:/bin", LANG: "C.UTF-8" }, encoding: "utf8", timeout: 60_000,
    });
    const last = ran.stdout.trim().split("\n").at(-1) ?? "";
    let said: Record<string, string> | null = null;
    try {
      said = JSON.parse(last) as Record<string, string>;
    } catch {
      // It said nothing a probe says.
    }
    return { said, status: ran.status, stderr: ran.stderr };
  }

  it("has srt's sandbox-exec and nothing else to run it on, and no warning", () => {
    expect(SandboxManager.checkDependencies()).toEqual({ errors: [], warnings: [] });
  });

  it("reads and writes the folder, writes its working folder, loads fsat and runs rg, and reaches nothing else of the computer", async () => {
    const { said, status, stderr } = await probe();
    console.log(`Seatbelt: ${JSON.stringify(said)}`);
    expect(status, stderr).toBe(0);
    // What a copy of native code in the folder does is measured alone, below.
    const reached = Object.entries(said ?? {}).filter(([name, what]) => what === "ok" && name !== "load a copy of fsat in the folder").map(([name]) => name).sort();
    expect(reached).toEqual([
      "list the folder", "load fsat", "read the folder", "run rg", "write the folder", "write the working folder",
    ]);
    expect(Object.keys(said ?? {})).toHaveLength(20);
    expect(readFileSync(join(String(at.home), ".ssh", "id_ed25519"), "utf8")).toBe("a key\n");
    expect(statSync(join(String(at.folder), "made.txt")).isFile()).toBe(true);
  });

  it("measures whether srt's own profile maps fsat as code, without the rule the host adds", async () => {
    const { said } = await probe(policy, (line) => line);
    console.log(`Seatbelt without fsat's rule: load fsat ${said?.["load fsat"]}; a copy in the folder ${said?.["load a copy of fsat in the folder"]}`);
    expect(said?.["read the folder"]).toBe("ok");
  });

  it("measures which of the system's folders the helper's node, fsat and rg need", async () => {
    const needed: string[] = [];
    for (const dir of srtOs.system) {
      const without = { ...policy, filesystem: { ...policy.filesystem, allowRead: (policy.filesystem.allowRead ?? []).filter((path) => path !== dir) } };
      const { said, status } = await probe(without);
      const works = status === 0 && said?.["read the folder"] === "ok" && said["load fsat"] === "ok" && said["run rg"] === "ok";
      if (!works) needed.push(dir);
    }
    console.log(`Seatbelt needs: ${JSON.stringify(needed)} of ${JSON.stringify(srtOs.system)}`);
    expect((await probe()).status).toBe(0);
  });
});

describe.runIf(process.platform === "darwin")("a tool host on macOS", { timeout: 60_000 }, () => {
  let base: string;
  let folder: string;
  let harness: Harness | null = null;

  beforeAll(() => {
    base = realpathSync(mkdtempSync(join(tmpdir(), "host-darwin-")));
    folder = join(base, "folder");
    mkdirSync(folder);
    writeFileSync(join(folder, "a.txt"), "alpha\n");
    mkdirSync(join(base, "outside"));
    writeFileSync(join(base, "outside", "secret.txt"), "secret\n");
  });

  afterAll(async () => {
    await harness?.stop();
    rmSync(base, { recursive: true, force: true });
  });

  it("serves the file kinds in its folder from inside srt's Seatbelt, on the app's node, with the app's rg", async () => {
    const start: HostStart = {
      type: "start", folder, expect: bound(folder), tmp: join(base, "data", "tmp", "root"), dataDir: join(base, "data"),
      cacheDir: join(base, "cache", "surogate"), env: { HOME: process.env.HOME ?? "/Users/tester", LANG: "C.UTF-8", PATH: "/usr/bin:/bin" },
      appDirs: [dirname(process.execPath), PACKAGE],
    };
    harness = new Harness(undefined, undefined, NODE);
    harness.send(start);
    const said = await harness.until((messages) => messages.find((message) => message.type === "ready" || message.type === "failed"));
    expect(said, harness.stderr).toMatchObject({ type: "ready" });
    expect(await harness.op("1", "read", { key: `${folder}/a.txt`, max_bytes: null })).toEqual({ ok: Buffer.from("alpha\n").toString("base64") });
    expect(await harness.op("2", "write", { key: `${folder}/b.txt`, data: Buffer.from("beta\n").toString("base64") })).toEqual({ ok: null });
    expect(readFileSync(join(folder, "b.txt"), "utf8")).toBe("beta\n");
    expect(await harness.op("3", "list_dir", { key: folder })).toMatchObject({ ok: expect.arrayContaining(["a.txt", "b.txt"]) });
    expect(await harness.op("4", "ripgrep", { key: folder, mode: "count", pattern: "alpha", glob: null, context: 0 })).toMatchObject({ ok: expect.stringContaining("a.txt") });
    expect(await harness.op("5", "walk", { key: folder, skip: [], skip_top: [], skip_hidden: false, since: null })).toMatchObject({ ok: { truncated: false } });
    // Its lock is a file of the app's data.
    const { dev, ino } = statSync(folder);
    expect(statSync(join(base, "data", "locks", `${process.getuid?.()}-${dev}-${ino}`)).isFile()).toBe(true);
  });
});
