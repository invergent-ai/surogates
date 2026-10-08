// The acceptance VMs (spec, Sections 9 and 10): Ubuntu 24.04 and 26.04 cloud images booted under
// QEMU, with and without nested virtualization, the restriction of unprivileged user namespaces
// on as Ubuntu ships it. The release this package builds is installed from a server on this
// computer with the install script, as a person installs it; the app starts with its sandbox, a
// file tool runs through its chain on the version's bwrap copy, an update is whole on the disk
// when the power is cut as it ends, and --uninstall removes it.
//
// Behind SUROGATE_ACCEPTANCE_TESTS=1, with SUROGATE_ACCEPTANCE_IMAGES naming a folder that holds
// noble.img and resolute.img (cloud-images.ubuntu.com's <release>-server-cloudimg-amd64.img).
// It needs /dev/kvm, QEMU, qemu-img, ssh and ssh-keygen, npm run build first, the Ubuntu archive
// for the VMs' apt, and about 40 minutes. The VMs and everything they wrote are removed after.

import { type ChildProcess, type ChildProcessWithoutNullStreams, spawn } from "node:child_process";
import { generateKeyPairSync } from "node:crypto";
import { chmodSync, copyFileSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { createServer } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";

import { afterAll, beforeAll, describe, expect, it } from "vitest";

const DESKTOP = fileURLToPath(new URL("..", import.meta.url));
const IMAGES = process.env.SUROGATE_ACCEPTANCE_IMAGES ?? "";
const VERSION = "1.2.3";
// The release an update brings, applied just before the power is cut.
const NEXT = "1.2.4";
const tarballOf = (version: string) => `surogate-desktop-${version}-linux-x64.tar.gz`;
const keys = generateKeyPairSync("ed25519");
const PUBLIC = keys.publicKey.export({ type: "spki", format: "pem" }).toString().trim();
// A static server of the folder it is given, on a port of its own, which it prints.
const SERVE = `
const { createServer } = require("node:http");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
createServer((request, response) => {
  try {
    response.end(readFileSync(join(process.argv[1], decodeURIComponent(new URL(request.url, "http://x").pathname))));
  } catch {
    response.writeHead(404).end();
  }
}).listen(0, "127.0.0.1", function () { console.log(this.address().port); });
`;
// What a desktop has that a server image lacks: the libraries Electron draws with, and an X server for the test.
const DESKTOP_LIBRARIES = "xvfb xauth libgtk-3-0t64 libnss3 libgbm1 libasound2t64";
// The app's AppArmor profile, as the kernel names it on a process it has.
const LABEL = "surogate-desktop (unconfined)";

// A program run to its end, off the test's event loop: vitest's worker answers its runner on that
// loop within a minute, and one that waited on a VM for longer would fail the run with every test
// passed. One still running after *timeout* is killed, and settles as one a signal ended: status null.
const run = (program: string, args: string[], { timeout = 600_000, env = process.env }: { timeout?: number; env?: NodeJS.ProcessEnv } = {}) =>
  new Promise<{ status: number | null; stdout: string; stderr: string }>((resolve) => {
    const child = spawn(program, args, { stdio: ["ignore", "pipe", "pipe"], env });
    let stdout = "";
    let stderr = "";
    child.stdout.on("data", (chunk: Buffer) => (stdout += chunk.toString()));
    child.stderr.on("data", (chunk: Buffer) => (stderr += chunk.toString()));
    const timer = setTimeout(() => child.kill("SIGKILL"), timeout);
    child.on("error", (error) => (stderr += error.message));
    child.on("close", (status) => {
      clearTimeout(timer);
      resolve({ status, stdout, stderr });
    });
  });

const freePort = () => new Promise<number>((resolve) => {
  const probe = createServer().listen(0, "127.0.0.1", () => {
    const { port } = probe.address() as { port: number };
    probe.close(() => resolve(port));
  });
});

// QEMU's own control of the VM it runs, on its standard streams (-qmp stdio), once it takes
// commands: what it is told there, as to reset, no guest can refuse or put off. Each command
// settles with QEMU's answer. A QEMU that ends before it takes any says why.
const control = (qemu: ChildProcessWithoutNullStreams) => new Promise<(command: string) => Promise<void>>((resolve, reject) => {
  let answered: (() => void) | undefined;
  let complaint = "";
  const tell = (command: string) => new Promise<void>((done) => {
    answered = done;
    qemu.stdin.write(`${JSON.stringify({ execute: command })}\n`);
  });
  createInterface({ input: qemu.stdout }).on("line", (line) => {
    const said = JSON.parse(line) as { QMP?: unknown; return?: unknown; error?: unknown };
    // Its greeting, answered with the one command it takes first; then each command's own answer.
    // What it says by itself, as that the VM was reset, is no answer.
    if (said.QMP) void tell("qmp_capabilities").then(() => resolve(tell));
    else if (said.error) reject(new Error(`QEMU refused a command: ${line}`));
    else if ("return" in said) answered?.();
  });
  qemu.stderr.on("data", (chunk: Buffer) => (complaint += chunk.toString()));
  qemu.stdin.on("error", reject);
  qemu.on("close", () => reject(new Error(`QEMU ended: ${complaint}`)));
});

describe("the acceptance VMs' probe", () => {
  it("runs the executor the app's main makes its own through, which makes no other", () => {
    // The probe is a second main: what it runs is the app's only as long as the app's main makes
    // its executor where the probe does, and gives no file host a bwrap of its own choosing.
    const makes = (file: string) => readFileSync(join(DESKTOP, file), "utf8").match(/new (?:VmExecutor|ToolHosts)\(|appTools\(|bwrapPath/g);
    expect(makes("src/shell/main.ts")).toEqual(["appTools("]);
    // The probe's other executor is its control's, which is given no bwrap either.
    expect(makes("test/acceptance/probe.mjs")).toEqual(["new VmExecutor(", "appTools("]);
  });
});

describe.skipIf(process.env.SUROGATE_ACCEPTANCE_TESTS !== "1")("the acceptance VMs", { timeout: 1_200_000 }, () => {
  let dir: string;
  let server: ChildProcess;
  let port: string;
  // How many files the update's release holds.
  let files: number;
  // Each VM that runs: one whose test was cut short at its time limit ends with the others.
  const running = new Set<ChildProcess>();

  beforeAll(async () => {
    dir = mkdtempSync(join(tmpdir(), "acceptance-"));
    const www = join(dir, "www", "desktop");
    // The install script that trusts the test's own key, and publish.sh beside it, as the two are
    // in the repository: the release job's own signing, of what its own packaging made.
    const script = readFileSync(join(DESKTOP, "release", "install.sh"), "utf8").replace(/RELEASE_KEYS=\(\n[^)]*\)/, `RELEASE_KEYS=(\n    '${PUBLIC}'\n  )`);
    mkdirSync(join(dir, "release"));
    writeFileSync(join(dir, "release", "install.sh"), script);
    copyFileSync(join(DESKTOP, "release", "publish.sh"), join(dir, "release", "publish.sh"));
    chmodSync(join(dir, "release", "publish.sh"), 0o755);
    writeFileSync(join(dir, "vm-manifest.json"), `${JSON.stringify({ key: "a".repeat(64), files: [] })}\n`);
    // Each release where the bucket has it, <base>/desktop/releases/<version>/: its tarball, and
    // its manifest and signature.
    for (const version of [VERSION, NEXT]) {
      const out = join(www, "releases", version);
      const packed = await run(join(DESKTOP, "scripts", "package.sh"), [version, join(dir, "vm-manifest.json"), out]);
      expect(packed.status, packed.stderr).toBe(0);
      const signed = await run(join(dir, "release", "publish.sh"), ["sign", version, out], {
        env: { ...process.env, DESKTOP_RELEASE_KEY: keys.privateKey.export({ type: "pkcs8", format: "pem" }).toString() },
      });
      expect(signed.status, signed.stderr).toBe(0);
    }
    // Each file of the update by its hash, as sha256sum checks a list: what a whole version of it holds.
    const hashes = await run("tar", ["-xzf", join(www, "releases", NEXT, tarballOf(NEXT)), "--to-command", 'printf "%s  %s\\n" "$(sha256sum | cut -d" " -f1)" "${TAR_FILENAME#*/}"']);
    expect(hashes.status, hashes.stderr).toBe(0);
    files = hashes.stdout.trim().split("\n").length;
    writeFileSync(join(dir, "www", "update.sha256"), hashes.stdout);
    // The newest release is the first: the update is one the app would have found later.
    copyFileSync(join(www, "releases", VERSION, "manifest.json"), join(www, "latest.json"));
    copyFileSync(join(www, "releases", VERSION, "manifest.json.sig"), join(www, "latest.json.sig"));
    writeFileSync(join(www, "install.sh"), script);
    copyFileSync(join(DESKTOP, "test", "acceptance", "probe.mjs"), join(dir, "www", "probe.mjs"));
    // The VMs' own login key: never this user's, and no agent of theirs is offered.
    expect((await run("ssh-keygen", ["-q", "-t", "ed25519", "-N", "", "-f", join(dir, "key")])).status).toBe(0);
    server = spawn(process.execPath, ["-e", SERVE, join(dir, "www")], { stdio: ["ignore", "pipe", "inherit"] });
    port = await new Promise<string>((resolve) => server.stdout!.once("data", (chunk: Buffer) => resolve(chunk.toString().trim())));
    mkdirSync(join(dir, "www", "seed"));
    writeFileSync(join(dir, "www", "seed", "meta-data"), "instance-id: surogate-acceptance\n");
    writeFileSync(join(dir, "www", "seed", "user-data"), [
      "#cloud-config",
      "users:",
      "  - name: tester",
      "    groups: [sudo]",
      "    shell: /bin/bash",
      "    sudo: \"ALL=(ALL) NOPASSWD:ALL\"",
      `    ssh_authorized_keys: ["${readFileSync(join(dir, "key.pub"), "utf8").trim()}"]`,
      "",
    ].join("\n"));
  }, 600_000);

  afterAll(() => {
    for (const qemu of running) qemu.kill("SIGKILL");
    server?.kill();
    rmSync(dir, { recursive: true, force: true });
  });

  for (const [release, image] of [["24.04", "noble.img"], ["26.04", "resolute.img"]] as const) {
    for (const nested of [true, false]) {
      it(`installs on a clean Ubuntu ${release} ${nested ? "with" : "without"} hardware virtualization, starts sandboxed, runs a file tool on its bwrap copy, has an update whole after a power cut as it ends, and uninstalls`, async () => {
        const vm = mkdtempSync(join(dir, "vm-"));
        expect((await run("qemu-img", ["create", "-q", "-f", "qcow2", "-b", join(IMAGES, image), "-F", "qcow2", join(vm, "disk.qcow2"), "20G"])).status).toBe(0);
        const sshPort = await freePort();
        const qemu = spawn("qemu-system-x86_64", [
          "-machine", "q35,accel=kvm", "-cpu", nested ? "host" : "host,-vmx,-svm", "-m", "4096", "-smp", "4", "-display", "none", "-serial", `file:${join(vm, "console.log")}`,
          "-drive", `file=${join(vm, "disk.qcow2")},if=virtio`, "-nic", `user,hostfwd=tcp:127.0.0.1:${sshPort}-:22`,
          "-smbios", `type=1,serial=ds=nocloud;s=http://10.0.2.2:${port}/seed/`,
          "-qmp", "stdio", "-pidfile", join(vm, "qemu.pid"),
        ]);
        running.add(qemu);
        // As the VM's tester, over ssh with the test's key alone: no config or agent of this user's.
        const login = [
          "-F", "/dev/null", "-i", join(dir, "key"), "-o", "IdentitiesOnly=yes", "-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null",
          "-o", "LogLevel=ERROR", "-o", "ConnectTimeout=5", "-p", String(sshPort), "tester@127.0.0.1",
        ];
        const env = { PATH: process.env.PATH ?? "/usr/bin:/bin" };
        const ssh = (command: string, timeout = 600_000) => run("ssh", [...login, command], { timeout, env });
        const serial = () => readFileSync(join(vm, "console.log"), "utf8").slice(-2000);
        try {
          const tell = await control(qemu);
          // Done, or done with a recoverable error of the image's own (exit 2): both have booted.
          for (const end = Date.now() + 300_000; !(await ssh("cloud-init status --wait; true", 300_000)).stdout.includes("status: done");) {
            if (Date.now() > end) throw new Error(`the VM did not come up: ${serial()}`);
            await new Promise((resolve) => setTimeout(resolve, 3_000));
          }
          expect((await ssh("cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns")).stdout).toBe("1\n");
          // QEMU's user network forwards DNS to this computer's /etc/resolv.conf, which a computer on
          // systemd-resolved alone may not have: the VM asks a public resolver. apt waits for the
          // lock the image's own first upgrades may hold.
          const libraries = await ssh("sudo mkdir -p /etc/systemd/resolved.conf.d && printf '[Resolve]\\nDNS=1.1.1.1 9.9.9.9\\n' | sudo tee /etc/systemd/resolved.conf.d/acceptance.conf >/dev/null "
            + `&& sudo systemctl restart systemd-resolved && sudo apt-get update -qq && sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq -o DPkg::Lock::Timeout=300 ${DESKTOP_LIBRARIES}`);
          expect(libraries.status, libraries.stderr).toBe(0);

          const base = `http://10.0.2.2:${port}`;
          const installed = await ssh(`curl -fsSL ${base}/desktop/install.sh | bash -s -- --base ${base}`);
          expect(installed.status, installed.stderr).toBe(0);
          expect(installed.stdout).toContain(`Surogate Desktop: ${VERSION} is installed\n`);
          expect(installed.stdout.includes("This computer has no hardware virtualization")).toBe(!nested);
          expect((await ssh(`sudo grep -c '^${LABEL}$' /sys/kernel/security/apparmor/profiles`)).stdout).toBe("1\n");

          // The app as the launcher starts it, in an X session of its own: Electron's own sandbox
          // takes its user namespaces from the profile, never --no-sandbox.
          // ELECTRON_RUN_AS_NODE as VS Code's terminals export it: the app starts as the app.
          const app = await ssh("(DBUS_SESSION_BUS_ADDRESS=disabled: ELECTRON_RUN_AS_NODE=1 xvfb-run -a /usr/local/bin/surogate --password-store=basic >app.log 2>&1 &) ; "
            // Its renderer in its sandbox is waited for, as long as a slow computer takes to start one.
            // Anchored or bracketed patterns, so they do not find this command's own shell.
            + "for wait in $(seq 90); do main=$(pgrep -o -f '^/opt/surogate/versions/[^ ]*/surogate --password-store=basic$'); renderer=$(pgrep -o -f 'surogate [-]-type=renderer'); "
            + "[ -n \"$main\" ] && [ -n \"$renderer\" ] && grep -q '^Seccomp:[[:space:]]2$' /proc/$renderer/status && break; sleep 1; done; "
            + "echo \"main $(ps -o args= -p $main)\"; echo \"label $(cat /proc/$main/attr/current)\"; "
            + "[ \"$(readlink /proc/$renderer/ns/user)\" != \"$(readlink /proc/$main/ns/user)\" ] && grep -q '^Seccomp:[[:space:]]2$' /proc/$renderer/status && echo 'renderer sandboxed'; "
            + "echo \"unsandboxed $(pgrep -fc '[-]-no-sandbox')\"; cat app.log");
          // The launcher starts current/surogate; Electron names its resolved program, the version's own.
          expect(app.stdout).toContain(`main /opt/surogate/versions/${VERSION}/surogate --password-store=basic\nlabel ${LABEL}\nrenderer sandboxed\nunsandboxed 0\n`);
          expect(app.stdout).not.toContain("No usable sandbox");
          await ssh("pkill -f '^/opt/surogate/versions/'; sleep 2");
          // The app's Electron never runs as Node. Its own program, with no launcher before it to
          // clear the variable, is handed a script as a Node would take one: it starts as the app,
          // and the script never runs. An Electron that still ran as Node would print the line and end.
          const asNode = await ssh("echo 'console.log(\"ran as node\")' >as-node.js; "
            + `(DBUS_SESSION_BUS_ADDRESS=disabled: ELECTRON_RUN_AS_NODE=1 xvfb-run -a /opt/surogate/versions/${VERSION}/surogate "$PWD/as-node.js" --password-store=basic >as-node.log 2>&1 &) ; `
            + "for wait in $(seq 90); do pgrep -f 'surogate [-]-type=renderer' >/dev/null && break; grep -q 'ran as node' as-node.log && break; sleep 1; done; "
            + "echo \"renderers $(pgrep -fc 'surogate [-]-type=renderer')\"; cat as-node.log");
          expect(asNode.stdout).toMatch(/^renderers [1-9]/);
          expect(asNode.stdout).not.toContain("ran as node");
          await ssh("pkill -f '^/opt/surogate/versions/'; sleep 2");
          // The same Electron anywhere else meets the restriction, as an unprofiled program does.
          const elsewhere = await ssh("cp -a \"$(readlink -f /opt/surogate/current)\" elsewhere && (DBUS_SESSION_BUS_ADDRESS=disabled: timeout 20 xvfb-run -a elsewhere/surogate --password-store=basic >elsewhere.log 2>&1; true); cat elsewhere.log");
          expect(elsewhere.stdout).toContain("The SUID sandbox helper binary was found, but is not configured correctly");

          // A file tool's chain, as the app runs it: a probe main in a version of its own beside the
          // installed one, which makes its executor where the app's main makes its own.
          // What it prints, each program that ran by its label; or, of a probe that printed nothing, what it
          // said in place of it: one that cannot start shows Electron's error box, and ends only when it is ended.
          const probe = async (env = "") => {
            const said = await ssh(`DBUS_SESSION_BUS_ADDRESS=disabled: ${env} timeout 120 xvfb-run -a /opt/surogate/versions/0.0.1/surogate --password-store=basic 2>probe.log | grep '^{' || { tail -c 2000 probe.log >&2; false; }`);
            expect(said.status, said.stderr).toBe(0);
            const { programs, ...rest } = JSON.parse(said.stdout) as { main: string; outcome: { ok?: null; error?: { message: string } }; written: string | null; programs: Array<{ cmd: string; label: string }> };
            return { ...rest, programs: Object.fromEntries(programs.map(({ cmd, label }) => [cmd.split(" ")[0]!, label])) };
          };
          expect((await ssh(`sudo cp -a /opt/surogate/versions/${VERSION} /opt/surogate/versions/0.0.1 && curl -fsS ${base}/probe.mjs | sudo tee /opt/surogate/versions/0.0.1/resources/app/probe.mjs >/dev/null `
            + "&& sudo sh -c 'jq \".main = \\\"probe.mjs\\\"\" /opt/surogate/versions/0.0.1/resources/app/package.json >/tmp/p.json && mv /tmp/p.json /opt/surogate/versions/0.0.1/resources/app/package.json'")).status).toBe(0);
          // The bwrap that ran is the version's own copy, which the probe did not name: the app's executor did.
          const copy = await probe();
          expect(copy).toMatchObject({ main: LABEL, outcome: { ok: null }, written: "written in srt\n" });
          expect(copy.programs).toMatchObject({ "/opt/surogate/versions/0.0.1/resources/app/bin/node": LABEL, "/opt/surogate/versions/0.0.1/bin/bwrap": LABEL });
          // The control, the system's own /usr/bin/bwrap, which a file host given none finds on its
          // PATH: 26.04 confines it with its bwrap profile, which refuses srt's helper its nested
          // user namespace (Section 4). That is why the copy.
          const system = await probe("PROBE_ON_PATH=1");
          expect(Object.keys(system.programs)).not.toContain("/opt/surogate/versions/0.0.1/bin/bwrap");
          if (release === "24.04") expect(system).toMatchObject({ outcome: { ok: null }, programs: { "/usr/bin/bwrap": LABEL } });
          else expect(system.outcome.error?.message).toContain("nested userns is capability-restricted");
          await ssh("sudo rm -rf /opt/surogate/versions/0.0.1");

          // The scheme registered for every user, the user in kvm, and the record.
          expect((await ssh("grep -x 'x-scheme-handler/surogate=surogate.desktop;' /usr/share/applications/mimeinfo.cache")).status).toBe(0);
          expect((await ssh("getent group kvm")).stdout).toMatch(/\btester\b/);
          expect((await ssh("test -e /dev/kvm")).status).toBe(nested ? 0 : 1);
          expect(JSON.parse((await ssh("cat /etc/surogate/install.json")).stdout)).toEqual({ base, channel: "stable" });

          // An update, downloaded as the app downloads one, into a folder of the user's own, and
          // applied as root by the install script the test's key signs for, as the installed helper
          // applies one. Once the helper has ended, another program writes a file of its own and waits
          // for it to reach the disk, as some program of a desktop does every few seconds: the disk
          // then has all the helper renamed, the switch among it, and of each file's bytes only
          // what was written out by then. Then the power is cut: QEMU resets the VM, and nothing the
          // guest had not yet given its disk is ever written. Cut without that other program, the
          // disk has none of the update for half a minute, and names the version before.
          const from = `${base}/desktop/releases/${NEXT}`;
          expect((await ssh(`mkdir update && cd update && curl -fsS -O ${from}/manifest.json -O ${from}/manifest.json.sig -O ${from}/${tarballOf(NEXT)} `
            + `&& curl -fsS -o surogate-apply-update ${base}/desktop/install.sh && chmod 0755 surogate-apply-update`)).status).toBe(0);
          const boot = (await ssh("cat /proc/sys/kernel/random/boot_id")).stdout;
          const helper = spawn("ssh", [...login, `sudo "$PWD/update/surogate-apply-update" --apply "$PWD/update/manifest.json" "$PWD/update/manifest.json.sig" "$PWD/update/${tarballOf(NEXT)}"; status=$?; ended=$(date +%s%N); `
            + "python3 -c \"import os; file = os.open('another-program', os.O_WRONLY | os.O_CREAT, 0o600); os.write(file, b'x'); os.fsync(file)\"; "
            + "echo \"helper exited $status, $(( ($(date +%s%N) - ended) / 1000000 )) ms ago\""], { stdio: ["ignore", "pipe", "pipe"], env });
          const cut = await new Promise<{ status: string; said: string; ms: number }>((resolve, reject) => {
            let said = "";
            let at = 0;
            helper.stderr.on("data", (chunk: Buffer) => (said += chunk.toString()));
            helper.stdout.on("data", (chunk: Buffer) => {
              said += chunk.toString();
              const exited = /^helper exited (\d+), (\d+) ms ago$/m.exec(said);
              if (!exited || at) return;
              at = Date.now();
              // How long after the helper's end the VM was reset: what the guest counted, and then this computer.
              void tell("system_reset").then(() => resolve({ status: exited[1]!, said, ms: Number(exited[2]) + Date.now() - at }));
            });
            // Closed, and so read to its end, without its status: the helper's run did not end by itself.
            helper.on("close", () => {
              if (!at) reject(new Error(`the helper's run ended without its status: ${said}`));
            });
          });
          helper.kill();
          expect(cut.status, cut.said).toBe("0");
          expect(cut.said).toContain(`Surogate Desktop: ${NEXT} is installed\n`);
          expect(cut.ms).toBeLessThan(1_000);
          for (const end = Date.now() + 300_000; ; await new Promise((resolve) => setTimeout(resolve, 2_000))) {
            const now = (await ssh("cat /proc/sys/kernel/random/boot_id", 15_000)).stdout;
            if (now && now !== boot) break;
            if (Date.now() > end) throw new Error(`the VM did not come back after the power cut: ${serial()}`);
          }
          // The disk had the switch: current names the update. And the update is whole: each file of
          // its release as the release has it, its mark, its bwrap, and the helper pkexec runs. An
          // apply that switched before its files were on the disk leaves some of them empty here.
          expect((await ssh("readlink /opt/surogate/current")).stdout).toBe(`/opt/surogate/versions/${NEXT}\n`);
          const whole = await ssh(`cd /opt/surogate/versions/${NEXT} && { curl -fsS ${base}/update.sha256 | sha256sum --quiet -c - 2>&1 | head -n 20; [ "\${PIPESTATUS[*]}" = "0 0 0" ]; } `
            + `&& cmp release.json <(curl -fsS ${from}/manifest.json) && cmp bin/bwrap /usr/bin/bwrap `
            + "&& test -s /opt/surogate/bin/surogate-apply-update && cmp bin/surogate-apply-update /opt/surogate/bin/surogate-apply-update && find . -type f | wc -l");
          expect(whole.status, whole.stdout + whole.stderr).toBe(0);
          // Its release's files, its mark and its bwrap, and nothing more.
          expect(Number(whole.stdout)).toBe(files + 2);

          const removed = await ssh(`curl -fsSL ${base}/desktop/install.sh | bash -s -- --uninstall`);
          expect(removed.status, removed.stderr).toBe(0);
          expect((await ssh("test ! -e /opt/surogate && test ! -e /usr/local/bin/surogate && ! sudo grep -q surogate-desktop /sys/kernel/security/apparmor/profiles")).status).toBe(0);
        } finally {
          qemu.kill("SIGKILL");
          running.delete(qemu);
          rmSync(vm, { recursive: true, force: true });
        }
      });
    }
  }
});
