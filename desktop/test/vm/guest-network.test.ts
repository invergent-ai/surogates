// The guest under QEMU and KVM, booted by the VM manager: the image built by
// images/guest/build.sh, the agent disk built from this package (npm run build
// first). Behind SUROGATE_VM_TESTS=1; SUROGATE_VM_IMAGE names another image folder.

import { spawnSync } from "node:child_process";
import { mkdirSync, mkdtempSync, readdirSync, readFileSync, realpathSync, rmSync, statSync } from "node:fs";
import { networkInterfaces, tmpdir } from "node:os";
import { join } from "node:path";

import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";

import { type ApprovalAnswer, type ApprovalPrompts, type ApprovalRequest, Approvals } from "../../src/binding/approvals.js";
import { BOOT_ID } from "../../src/binding/folder.js";
import { OperationJournal } from "../../src/journal/journal.js";
import type { Operation, Outcome } from "../../src/link/protocol.js";
import { VmClient } from "../../src/vm/client.js";
import { VmExecutor } from "../../src/vm/executor.js";
import { agentDisk, IMAGE, KVM, needsKvm, signal, USER } from "./guest-support.js";

beforeAll(needsKvm);

describe.skipIf(process.env.SUROGATE_VM_TESTS !== "1")("the network, through the VmExecutor and the guest", { timeout: 60_000 }, () => {
  const CHAT = "6a7b8c9d-0e1f-4a2b-8c3d-4e5f6a7b8c9d";
  let dir: string;
  let run: string;
  let folder: string;
  let vm: VmClient;
  let executor: VmExecutor;
  let journal: OperationJournal;
  // Every prompt the chat's user was shown, and what they answer each network one, after a moment.
  let prompts: ApprovalRequest[];
  let answer: (request: Extract<ApprovalRequest, { kind: "network" }>) => ApprovalAnswer;
  // Whether the chat's user leaves each network prompt open until it is dismissed.
  let held: boolean;
  const user: ApprovalPrompts = {
    approve: async (request, dismissed) => {
      prompts.push(request);
      if (held && request.kind === "network") return new Promise((resolve) => dismissed.addEventListener("abort", () => resolve("deny"), { once: true }));
      await new Promise((resolve) => setTimeout(resolve, 300));
      return request.kind === "network" ? answer(request) : "allow";
    },
    confirmFreeMode: async () => false,
  };
  const operation = (kind: string, args: Record<string, unknown>): Operation => ({
    id: `${kind}-${Math.random()}`, sessionId: CHAT, callingSessionId: CHAT, invocationId: "call", ordinal: 1, kind, args, digest: "d",
  });
  const command = (line: string, timeout = 60) => executor.run(operation("run", { command: line, workdir: null, timeout }), signal());
  const networkPrompts = () => prompts.filter((prompt) => prompt.kind === "network").map(({ host, port, privateNetwork }) => ({ host, port, privateNetwork }));
  // The HTTP status a command's curl gets, 000 for none.
  const status = (url: string, flags = "") => `curl -sS --max-time 20 ${flags} -o /dev/null -w '%{http_code}\\n' ${url} 2>/dev/null`;

  beforeAll(() => {
    dir = realpathSync(mkdtempSync(join(tmpdir(), "vm-guest-network-")));
    folder = join(dir, "folder");
    mkdirSync(folder);
    run = mkdtempSync(join(process.env.XDG_RUNTIME_DIR ?? "/tmp", "sg-vm-"));
    vm = new VmClient({
      vm: {
        kernel: join(IMAGE, "vmlinuz"), rootfs: join(IMAGE, "rootfs.img"), agentDisk: agentDisk(dir), sessions: join(dir, "sessions.img"),
        run, console: join(dir, "console.log"), user: USER, kvm: KVM,
      },
    });
    // The chat as the app binds one, working freely: only the network asks.
    journal = new OperationJournal(join(dir, "journal.sqlite"));
    const { dev, ino } = statSync(folder);
    journal.bindings.add({ root: CHAT, nonce: "nonce", folder, dev, ino, boot: BOOT_ID, mode: "free", boundAt: 1 });
    const approvals = new Approvals({ bindings: journal.bindings, prompts: user, agent: "the VM tests" });
    executor = new VmExecutor({
      bindingOf: (root) => journal.bindings.get(root), dataDir: join(dir, "data"), env: { HOME: USER.home, LANG: "C.UTF-8", PATH: "/usr/bin:/bin" },
      network: { askNetwork: (root, asked, cancel) => approvals.askNetwork(root, asked, cancel) }, vm,
    });
  });

  beforeEach(() => {
    prompts = [];
    answer = () => "allow";
    held = false;
  });

  afterAll(async () => {
    await executor?.stop();
    await vm?.stop();
    journal?.close();
    rmSync(run, { recursive: true, force: true });
    rmSync(dir, { recursive: true, force: true });
  });

  it("installs a package from PyPI with no prompt", async () => {
    // The image's pip is uv's, into the root's own PYTHONUSERBASE.
    expect(await command("pip install --no-cache-dir --no-deps --reinstall --quiet cowsay==6.1 && python3 -c 'import cowsay; print(cowsay.__file__)'", 120)).toEqual({
      ok: { output: `${USER.home}/.local/lib/python3.12/site-packages/cowsay/__init__.py\n`, returncode: 0, timed_out: false },
    });
    expect(prompts).toEqual([]);
  });

  // The rule leaves what package managers unpack alone: iconv-lite's tarball holds an .idea folder.
  it("installs an npm package that ships an editor's folder into the shared folder, every file of it", async () => {
    try {
      const npm = async (spec: string) => {
        const installed = await command(`npm install --no-audit --no-fund --no-update-notifier --cache ~/npm-cache ${spec} 2>&1; echo rc=$?`, 180);
        const { output } = (installed as { ok: { output: string } }).ok;
        expect(output).toMatch(/(added|changed) \d+ packages?[^]*\nrc=0\n$/);
        expect(output).not.toMatch(/TAR_ENTRY_ERROR|EPERM|not permitted/);
      };
      await npm("iconv-lite@0.6.3");
      expect(readdirSync(join(folder, "node_modules", "iconv-lite", ".idea"))).toContain("codeStyles");
      // Another version: npm renames the installed one aside within node_modules first, then unpacks this one.
      await npm("iconv-lite@0.6.2");
      expect(JSON.parse(readFileSync(join(folder, "node_modules", "iconv-lite", "package.json"), "utf8")).version).toBe("0.6.2");
      // One with a program: npm links it in node_modules/.bin, a link inside the dependency folder, and commands still run.
      await npm("semver@7.6.3");
      expect(await command("readlink node_modules/.bin/semver")).toMatchObject({ ok: { output: "../semver/bin/semver.js\n" } });
      expect(prompts).toEqual([]);
    } finally {
      for (const name of ["node_modules", "package.json", "package-lock.json"]) rmSync(join(folder, name), { recursive: true, force: true });
    }
  });

  it("asks once for a site's connections in flight, lets them through once allowed, and every port of a host allowed for the session", async () => {
    expect(await command(`${status("https://example.com/")} & ${status("https://example.com/")} & wait`)).toEqual({
      ok: { output: "200\n200\n", returncode: 0, timed_out: false },
    });
    expect(networkPrompts()).toEqual([{ host: "example.com", port: 443, privateNetwork: false }]);
    // Allowed once: the next connection asks again, and this answer lasts.
    answer = () => "allow_session";
    expect(await command(status("https://example.com/"))).toMatchObject({ ok: { output: "200\n" } });
    expect(await command(`${status("https://example.com/")}; ${status("http://example.com/")}`)).toMatchObject({ ok: { output: "200\n200\n" } });
    expect(networkPrompts()).toHaveLength(2);
    expect(journal.bindings.domains(CHAT)).toEqual(["example.com"]);
  });

  it("refuses this computer's own services without asking, however a command names them, and says so", async () => {
    const lan = Object.values(networkInterfaces()).flat().find((entry) => entry && !entry.internal && entry.family === "IPv4")?.address;
    const targets = ["http://127.0.0.1:9/", "http://localhost:9/", "http://[::1]:9/", ...(lan ? [`http://${lan}:9/`] : [])];
    const outcome = await command(targets.map((url) => status(url, "--noproxy ''")).join("; "));
    const named = ["127.0.0.1:9", "localhost:9", "[::1]:9", ...(lan ? [`${lan}:9`] : [])].join(", ");
    expect(outcome).toEqual({
      ok: { output: `${"403\n".repeat(targets.length)}\nThis computer does not let a chat reach its own network services (${named})`, returncode: 0, timed_out: false },
    });
    expect(prompts).toEqual([]);
  });

  it("asks about a private network saying so, and tells the agent what its user denied", async () => {
    answer = () => "deny";
    expect(await command(status("http://10.255.255.1:9/"))).toEqual({
      ok: { output: "403\n\nThis computer did not allow network access to 10.255.255.1:9.", returncode: 0, timed_out: false },
    });
    expect(networkPrompts()).toEqual([{ host: "10.255.255.1", port: 9, privateNetwork: true }]);
  });

  it("times a command out at its own timeout while a connection of it waits for its user, and says it still waits", async () => {
    held = true;
    expect(await command("curl -sS -o /dev/null http://192.0.2.1:9/ 2>/dev/null; echo done", 3)).toEqual({
      ok: { output: "Command timed out after 3 seconds\nStill waiting for this computer's user to allow network access to 192.0.2.1:9.", returncode: 124, timed_out: true },
    });
    expect(networkPrompts()).toEqual([{ host: "192.0.2.1", port: 9, privateNetwork: false }]);
    // Once nothing of the chat runs its prompt is dismissed, a denial the next command is told of.
    expect(await command("echo next")).toEqual({
      ok: { output: "next\n\nThis computer did not allow network access to 192.0.2.1:9.", returncode: 0, timed_out: false },
    });
  });

  it("shows a command no network device but its own loopback, and no name server", async () => {
    expect(await command("ip -o link | cut -d' ' -f2; getent hosts example.com || echo no lookup")).toEqual({
      ok: { output: "lo:\nno lookup\n", returncode: 0, timed_out: false },
    });
  });

  it("carries a large download, a package set and an npm install through the host proxy, timed against this computer's own", { timeout: 1_800_000 }, async () => {
    const WHEEL = "https://files.pythonhosted.org/packages/8b/5c/36c114d120bfe10f9323ed35061bc5878cc74f3f594003854b0ea298942f/torch-2.5.1-cp312-cp312-manylinux1_x86_64.whl";
    const SET = "numpy pandas scipy pyarrow scikit-learn matplotlib pillow lxml cryptography grpcio";
    // uv's, as the image's pip is: into a folder of its own, past what the image has.
    const PIP = `--no-cache --no-deps --reinstall --quiet --python-version 3.12 ${SET}`;
    const NPM = "--no-audit --no-fund --no-package-lock --no-update-notifier --silent webpack@5 eslint@9 typescript@5";
    const scratch = mkdtempSync(join(tmpdir(), "vm-guest-measured-"));
    const uv = spawnSync("bash", ["-c", "command -v uv"], { encoding: "utf8" }).stdout.trim();
    const seconds = (begun: number) => (performance.now() - begun) / 1000;
    const span = (times: number[]) => `${Math.min(...times).toFixed(1)}–${Math.max(...times).toFixed(1)} s`;
    // *line* through the guest and *here* on this computer, each from a cold cache, as the guest's
    // is: in turn and twice, so neither always goes first. The guest's last outcome, and each one's range.
    const twice = async (line: string, expected: object, here: ((turn: number) => string) | null) => {
      const inGuest: number[] = [];
      const onHost: number[] = [];
      const native = (turn: number) => {
        if (!here) return;
        const begun = performance.now();
        spawnSync("bash", ["-c", here(turn)], { encoding: "utf8", timeout: 600_000 });
        onHost.push(seconds(begun));
      };
      let outcome: Outcome = { ok: null };
      for (const turn of [0, 1]) {
        if (turn === 1) native(turn);
        const begun = performance.now();
        outcome = await command(line, 600);
        inGuest.push(seconds(begun));
        expect(outcome).toMatchObject(expected);
        if (turn === 0) native(turn);
      }
      return { outcome, guest: span(inGuest), here: onHost.length > 0 ? span(onHost) : "no uv" };
    };
    try {
      const wheel = await twice(`curl -sS -o /dev/null -w '%{size_download}' ${WHEEL}`, { ok: { output: "906389343", returncode: 0 } }, () => `curl -sS -o /dev/null ${WHEEL}`);
      const set = await twice(
        `uv pip install --python /opt/venv/bin/python --target ~/measured-pip ${PIP} && ls ~/measured-pip | grep -c dist-info; rm -rf ~/measured-pip`,
        { ok: { output: "10\n", returncode: 0 } },
        uv ? (turn) => `${uv} pip install --python-platform x86_64-manylinux_2_28 --target ${scratch}/pip-${turn} ${PIP}` : null,
      );
      const npm = await twice(
        `npm install --cache ~/measured-npm-cache --prefix ~/measured-npm ${NPM} && ls ~/measured-npm/node_modules | wc -l; rm -rf ~/measured-npm ~/measured-npm-cache`,
        { ok: { returncode: 0 } },
        (turn) => `npm install --cache ${scratch}/npm-cache-${turn} --prefix ${scratch}/npm-${turn} ${NPM}`,
      );
      expect(prompts).toEqual([]);
      console.log([
        `M7, twice each in turn: a 906 MB wheel in ${wheel.guest} through the guest, ${wheel.here} on this computer`,
        `ten packages by uv in ${set.guest}, ${set.here} on this computer`,
        `npm install of ${String((npm.outcome as { ok: { output: string } }).ok.output).trim()} top-level packages in ${npm.guest}, ${npm.here} on this computer`,
      ].join("; "));
    } finally {
      rmSync(scratch, { recursive: true, force: true });
    }
  });
});
