// The acceptance VMs (spec, Sections 9 and 10): Ubuntu 24.04 and 26.04 cloud images booted under
// QEMU, with and without nested virtualization, the restriction of unprivileged user namespaces
// on as Ubuntu ships it. The release this package builds is installed from a server on this
// computer with the install script, as a person installs it; the app starts with its sandbox, a
// file tool runs through its chain on the version's bwrap copy, an update is whole on the disk
// when the power is cut as it ends, and --uninstall removes it. Then an update inside the app: a
// version with a bound folder takes the next release through polkit and starts again on it, a
// user who is no administrator keeps it, and --version rolls it back.
//
// Behind SUROGATE_ACCEPTANCE_TESTS=1, with SUROGATE_ACCEPTANCE_IMAGES naming a folder that holds
// noble.img and resolute.img (cloud-images.ubuntu.com's <release>-server-cloudimg-amd64.img).
// It needs /dev/kvm, QEMU, qemu-img, ssh and ssh-keygen, npm run build first, the Ubuntu archive
// for the VMs' apt, and about 40 minutes. The VMs and everything they wrote are removed after.

import { type ChildProcess, type ChildProcessWithoutNullStreams, spawn } from "node:child_process";
import { generateKeyPairSync } from "node:crypto";
import { chmodSync, copyFileSync, mkdirSync, mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
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
// A release's tarball $1, tarred again as $2, the release of version $4, with the file $5 in its
// app as that app's main, in a folder of its own under $3.
const WITH_MAIN = String.raw`
set -euo pipefail
tree="$(mktemp -d -p "$3")"
tar -xpzf "$1" -C "$tree"
[ -e "$tree/surogate-desktop-$4-linux-x64" ] || mv "$tree"/* "$tree/surogate-desktop-$4-linux-x64"
app="$tree/surogate-desktop-$4-linux-x64/resources/app"
install -m 0644 "$5" "$app/"
jq --arg version "$4" --arg main "$(basename "$5")" '.version = $version | .main = $main' "$app/package.json" >"$app/package.json.new"
mv "$app/package.json.new" "$app/package.json"
tar --sort=name --owner=0 --group=0 --numeric-owner -C "$tree" -czf "$2" "surogate-desktop-$4-linux-x64"
rm -rf "$tree"
`;
// What pkexec is handed by the app, and what the helper it then runs is handed by pkexec, as root
// reads both of a computer while an update installs: of each, once it is seen, its arguments, the
// environment it was started with, its open descriptors and whose it is, into a file of its name
// in the folder $1. The helper is the same process as its pkexec, once pkexec has run it. It
// looks for a minute and a half.
const HANDED = String.raw`
mkdir -p "$1"
for look in $(seq 4500); do
  for pid in $(pgrep -x pkexec) $(pgrep -f '[s]urogate-apply-update --apply'); do
    case "$(readlink "/proc/$pid/exe")" in /usr/bin/pkexec) what=pkexec ;; /usr/bin/bash) what=helper ;; *) continue ;; esac
    [ ! -e "$1/$what" ] || continue
    { tr '\0' '\n' <"/proc/$pid/cmdline" && echo == && tr '\0' '\n' <"/proc/$pid/environ" && echo == && ls -l "/proc/$pid/fd" | sed -n 's/.* \([0-9]*\) -> /\1 /p' \
      && echo == && grep '^Uid:' "/proc/$pid/status"; } >"$1/$what.new" 2>/dev/null && mv "$1/$what.new" "$1/$what"
  done
  if [ -e "$1/pkexec" ] && [ -e "$1/helper" ]; then break; fi
  sleep 0.02
done
`;
// The helper pkexec runs and the install record: each root's own file, which the app reads.
const HELPER = "/opt/surogate/bin/surogate-apply-update";
const RECORD = "/etc/surogate/install.json";
// The test's polkit rule for the update's action, which stands in for a person at a prompt. An
// administrator approves, after two seconds, as a person takes longer over a password: for as
// long, pkexec waits with what the app handed it. And a user whom the computer's policy refuses.
const RULE = 'polkit.addRule(function (action, subject) { if (action.id != "ai.invergent.surogate.update") return polkit.Result.NOT_HANDLED; '
  + 'if (subject.user == "plain") return polkit.Result.NO; if (subject.user == "tester") { polkit.spawn(["/usr/bin/sleep", "2"]); return polkit.Result.YES; } });';
// What a run of the update's probe wrote (test/acceptance/update-probe.mjs).
interface Probed {
  version: string;
  binding: unknown;
  found?: string;
  update?: { state: string; version?: string; why?: string };
  line?: { text: string; button: string | null } | null;
  logged?: string[];
  error?: string;
}

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

  // The release in *out* signed as the release job signs one, in its two steps. The first writes
  // the manifest of the tarball, by the hash the build's job says of it, where no release key is:
  // it refuses to run with one in its environment. The second signs that manifest with the key, by
  // the build's own words for the tarball's hash and its size, and opens no tarball.
  const signed = async (version: string, out: string) => {
    const tarball = join(out, tarballOf(version));
    const hashed = await run("sha256sum", [tarball]);
    expect(hashed.status, hashed.stderr).toBe(0);
    const { DESKTOP_RELEASE_KEY: _held, ...own } = process.env;
    const built = { DESKTOP_TARBALL_SHA256: hashed.stdout.slice(0, 64) };
    const described = await run(join(dir, "release", "publish.sh"), ["describe", version, out], { env: { ...own, ...built } });
    expect(described.status, described.stderr).toBe(0);
    const signing = await run(join(dir, "release", "publish.sh"), ["sign", version, out], {
      env: { ...own, ...built, DESKTOP_TARBALL_SIZE: String(statSync(tarball).size), DESKTOP_RELEASE_KEY: keys.privateKey.export({ type: "pkcs8", format: "pem" }).toString() },
    });
    expect(signing.status, signing.stderr).toBe(0);
  };

  beforeAll(async () => {
    dir = mkdtempSync(join(tmpdir(), "acceptance-"));
    const www = join(dir, "www", "desktop");
    // The install script that trusts the test's own key, and publish.sh beside it, as the two are
    // in the repository: the release job's own signing, of what its own packaging made. Each
    // release's root helper is that install script, as a release's is its tag's: the signing
    // refuses any other, and the helper an install puts in place then trusts the test's key too.
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
      const packed = await run(join(DESKTOP, "scripts", "package.sh"), [version, join(dir, "vm-manifest.json"), out, join(dir, "release", "install.sh")]);
      expect(packed.status, packed.stderr).toBe(0);
      await signed(version, out);
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
      // Another user of the computer, who is no administrator.
      "  - name: plain",
      "    shell: /bin/bash",
      "",
    ].join("\n"));
  }, 600_000);

  afterAll(() => {
    for (const qemu of running) qemu.kill("SIGKILL");
    server?.kill();
    rmSync(dir, { recursive: true, force: true });
  });

  // A clean VM of *image*, booted from a disk of its own, with or without nested virtualization, its
  // DNS a public resolver's and a desktop's libraries installed: how a command runs in it as its
  // tester, its login and its environment for one the test starts itself, QEMU's control of it, its
  // console's last words, and what stops it and removes its disk.
  const booted = async (image: string, nested: boolean) => {
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
    const stop = () => {
      qemu.kill("SIGKILL");
      running.delete(qemu);
      rmSync(vm, { recursive: true, force: true });
    };
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
      return { login, env, ssh, serial, tell, stop };
    } catch (error) {
      stop();
      throw error;
    }
  };

  for (const [release, image] of [["24.04", "noble.img"], ["26.04", "resolute.img"]] as const) {
    for (const nested of [true, false]) {
      it(`installs on a clean Ubuntu ${release} ${nested ? "with" : "without"} hardware virtualization, starts sandboxed, runs a file tool on its bwrap copy, has an update whole after a power cut as it ends, and uninstalls`, async () => {
        const { login, env, ssh, serial, tell, stop } = await booted(image, nested);
        try {
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
          stop();
        }
      });
    }
  }

  // The first release as *version*, with update-probe.mjs as its app's main, on a base of the
  // probe's own, <base>/probe/desktop/, signed as the release job signs one, by the hash and the
  // size of the tarball as it was tarred again. Its root helper is the first release's, which
  // package.sh packed: the test's install script, as the signing asks of it. The newest of them is
  // that base's latest.json.
  const probeBase = () => join(dir, "www", "probe", "desktop");
  const probeRelease = async (version: string) => {
    const out = join(probeBase(), "releases", version);
    mkdirSync(out, { recursive: true });
    const made = await run("bash", ["-c", WITH_MAIN, "_", join(dir, "www", "desktop", "releases", VERSION, tarballOf(VERSION)), join(out, tarballOf(version)), dir, version,
      join(DESKTOP, "test", "acceptance", "update-probe.mjs")]);
    expect(made.status, made.stderr).toBe(0);
    await signed(version, out);
    for (const end of ["", ".sig"]) copyFileSync(join(out, `manifest.json${end}`), join(probeBase(), `latest.json${end}`));
  };

  for (const [release, image] of [["24.04", "noble.img"], ["26.04", "resolute.img"]] as const) {
    it(`on Ubuntu ${release}, updates a version with a bound folder through polkit and starts again on it, keeps it for a user who is no administrator, and rolls back with --version`, async () => {
      rmSync(join(dir, "www", "probe"), { recursive: true, force: true });
      await probeRelease(VERSION);
      copyFileSync(join(dir, "release", "install.sh"), join(probeBase(), "install.sh"));
      const { ssh, stop } = await booted(image, true);
      try {
        const base = `http://10.0.2.2:${port}/probe`;
        const installed = await ssh(`curl -fsSL ${base}/desktop/install.sh | bash -s -- --base ${base}`);
        expect(installed.status, installed.stderr).toBe(0);
        // An X server for every run, which outlives each, as a desktop's does.
        expect((await ssh("(Xvfb :9 -ac -nolisten tcp >/dev/null 2>&1 &) && sleep 2")).status).toBe(0);
        // What *user*'s run of *version* wrote, once it has: a restart's comes after the run that asked for it.
        const written = async (user: string, version: string) => JSON.parse((await ssh(
          `for i in $(seq 60); do sudo test -s /home/${user}/probe-${version}.json && break; sleep 1; done; sudo cat /home/${user}/probe-${version}.json`,
        )).stdout || "null") as Probed | null;
        // The installed app as *user* starts it from the launcher, with *args*, in no session with a
        // polkit agent, as over ssh: what its version's probe wrote.
        const started = async (user: string, args: string, version: string) => {
          await ssh(`sudo -u ${user} rm -f /home/${user}/probe-${version}.json; sudo -u ${user} env -i HOME=/home/${user} PATH=/usr/bin:/bin DISPLAY=:9 DBUS_SESSION_BUS_ADDRESS=disabled: `
            + `/usr/local/bin/surogate --password-store=basic ${args} >/dev/null 2>&1`);
          return written(user, version);
        };
        const current = async () => (await ssh("readlink /opt/surogate/current")).stdout.trim();
        // The release the helper pkexec runs is of, as its mark names it; and the installed one, as its own does.
        const helperOf = async () => (await ssh("jq -r .version /opt/surogate/bin/release.json")).stdout.trim();
        const installedOf = async () => (await ssh("jq -r .version /opt/surogate/current/release.json")).stdout.trim();
        // The app's line, as its sidebar shows each state.
        const AVAILABLE = { text: `Update available: Surogate ${NEXT}`, button: "Restart to update" };
        const NEEDED = { text: "An administrator needs to install this update.", button: "Try again" };
        const BROKEN = { text: "Surogate cannot update itself. Run the install script again.", button: null };
        // What the app logs of a pkexec that could not run the helper as root: its exit, and its words.
        const AS_ANOTHER = `Surogate ${NEXT} was not installed (exit 127): Error executing command as another user:`;

        const bound = await started("tester", "--bind", VERSION);
        expect(bound).toMatchObject({ version: VERSION, binding: { root: "11111111-1111-4111-8111-111111111111", folder: "/home/tester/bound", mode: "ask" } });
        await probeRelease(NEXT);

        // A user who is no administrator: polkit asks for an administrator, and in a session with
        // no agent no one can be asked. pkexec ends 127 in its own words for that, the app says who
        // is needed, and the version stays.
        const refused = await started("plain", "--update", VERSION);
        expect(refused, JSON.stringify(refused)).toMatchObject({
          version: VERSION, found: "available", update: { state: "refused", version: NEXT }, line: NEEDED, logged: [`${AS_ANOTHER} No authentication agent found.`],
        });
        expect(await current()).toBe(`/opt/surogate/versions/${VERSION}`);
        // An administrator is asked the same way, and so, where nothing can ask, is answered the same.
        const unasked = await started("tester", "--update", VERSION);
        expect(unasked, JSON.stringify(unasked)).toMatchObject({ found: "available", update: { state: "refused", version: NEXT }, line: NEEDED, logged: [`${AS_ANOTHER} No authentication agent found.`] });
        // pkexec is the system's own, which the install brought: where the app runs it by its whole
        // path, set-id and root's, and on the PATH the install script pins for itself.
        expect((await ssh("stat -c '%a %U %n' /usr/bin/pkexec && PATH=/usr/sbin:/usr/bin:/sbin:/bin command -v pkexec")).stdout).toBe("4755 root /usr/bin/pkexec\n/usr/bin/pkexec\n");

        // The app takes the helper's release keys, the record's base and the installed version's
        // mark only from files that are root's own, which here they truly are. Each as the install
        // left it, and then in each form that root alone can give it: the app's check, as tester,
        // of a helper or a record that is and that is not root's alone to write.
        expect((await ssh(`stat -c '%a %U:%G' ${HELPER} ${RECORD} /opt/surogate/current/release.json /opt/surogate/bin/release.json`)).stdout).toBe("755 root:root\n644 root:root\n644 root:root\n644 root:root\n");
        const kept = (path: string) => [`mv ${path} ${path}.kept && ln -s ${path}.kept ${path}`, `rm ${path} && mv ${path}.kept ${path}`] as const;
        const forms: Array<[what: string, made: string, mended: string, found: "available" | "broken", why?: string]> = [
          ["as the install left them", "true", "true", "available"],
          ["the helper read-only, and a program", `chmod 0555 ${HELPER}`, `chmod 0755 ${HELPER}`, "available"],
          ["the helper, which its group may write", `chmod 0775 ${HELPER}`, `chmod 0755 ${HELPER}`, "broken", `${HELPER} is not the install script's: only root may write it`],
          ["the helper, the user's own", `chown tester ${HELPER}`, `chown root ${HELPER}`, "broken", `${HELPER} is not the install script's: only root may write it`],
          ["the helper, no program", `chmod 0644 ${HELPER}`, `chmod 0755 ${HELPER}`, "broken", `${HELPER} is not the install script's: it cannot be run`],
          ["a link where the helper is", ...kept(HELPER), "broken", `${HELPER} is not the install script's: it is a link`],
          ["no helper", `mv ${HELPER} ${HELPER}.kept`, `mv ${HELPER}.kept ${HELPER}`, "broken", `ENOENT: no such file or directory, lstat '${HELPER}'`],
          ["the record read-only", `chmod 0444 ${RECORD}`, `chmod 0644 ${RECORD}`, "available"],
          ["the record, which its group may write", `chmod 0664 ${RECORD}`, `chmod 0644 ${RECORD}`, "broken", `${RECORD} is not the install script's: only root may write it`],
          ["the record, the user's own", `chown tester ${RECORD}`, `chown root ${RECORD}`, "broken", `${RECORD} is not the install script's: only root may write it`],
          ["a link where the record is", ...kept(RECORD), "broken", `${RECORD} is not the install script's: it is a link`],
        ];
        for (const [what, made, mended, found, why] of forms) {
          expect((await ssh(`sudo sh -c '${made}'`)).status, what).toBe(0);
          const checked = await started("tester", "--check", VERSION);
          expect((await ssh(`sudo sh -c '${mended}' && stat -c '%a %U' ${HELPER} ${RECORD}`)).stdout, what).toBe("755 root\n644 root\n");
          // A helper or a record that the app cannot take has a line of its own, which says what to
          // do. Why is in the app's log.
          expect(checked, `${what}: ${JSON.stringify(checked)}`).toMatchObject({
            version: VERSION, found, line: { available: AVAILABLE, broken: BROKEN }[found], logged: why === undefined ? [] : [`Error: ${why}`],
          });
        }

        expect((await ssh(`echo '${RULE}' | sudo tee /etc/polkit-1/rules.d/10-acceptance.rules >/dev/null && sleep 2`)).status).toBe(0);
        // A user the computer's policy refuses outright: pkexec's other words for a 127, and the same line.
        const forbidden = await started("plain", "--update", VERSION);
        expect(forbidden, JSON.stringify(forbidden)).toMatchObject({
          found: "available", update: { state: "refused", version: NEXT }, line: NEEDED, logged: [`${AS_ANOTHER} Not authorized\n\nThis incident has been reported.`],
        });

        // An update the administrator approves and the helper refuses, for a reason that is the
        // helper's alone to see. First: a byte of the downloaded tarball changed after the app's own
        // look at it. The helper reads its own copy against the signed manifest, says so, and the
        // app's line says it after the helper; nothing is installed, and nothing is left in staging.
        const changed = await started("tester", "--update --changed", VERSION);
        expect(changed, JSON.stringify(changed)).toMatchObject({
          found: "available", update: { state: "failed", version: NEXT, why: "the downloaded release is not the one its manifest names" },
          line: { text: "Surogate could not install its update: the downloaded release is not the one its manifest names", button: "Try again" },
          logged: [`Surogate ${NEXT} was not installed (exit 1): Surogate Desktop: the downloaded release is not the one its manifest names`],
        });
        expect((await ssh("readlink /opt/surogate/current; sudo ls -A /opt/surogate/staging")).stdout).toBe(`/opt/surogate/versions/${VERSION}\n`);
        // The administrator's update, with the helper at a mode that an install does not leave it
        // at, and that is root's alone to write and a program still: the app takes it by that rule,
        // and so does the helper. With root looking on at what is handed to whom.
        expect((await ssh(`sudo chmod 0555 ${HELPER}`)).status).toBe(0);
        const watching = ssh(`sudo rm -rf /root/handed && sudo bash -s /root/handed <<'HANDED'${HANDED}HANDED`);
        const updated = await started("tester", "--update", VERSION);
        expect(updated, JSON.stringify(updated)).toMatchObject({
          version: VERSION, found: "available", update: { state: "installed", version: NEXT }, line: { text: `Surogate ${NEXT} is installed.`, button: "Restart" }, logged: [],
        });
        // Started again from the launcher, on the update, with the binding its user made.
        expect(await written("tester", NEXT)).toEqual({ version: NEXT, binding: bound!.binding });
        expect(await current()).toBe(`/opt/surogate/versions/${NEXT}`);
        expect((await ssh("ls /opt/surogate/versions")).stdout).toBe(`${VERSION}\n${NEXT}\n`);
        expect([await helperOf(), await installedOf()]).toEqual([NEXT, NEXT]);
        // The update's own check: nothing newer, and no line.
        expect(await started("tester", "--check", NEXT)).toEqual({ version: NEXT, binding: bound!.binding, found: "none", line: null, logged: [] });

        // What was handed to whom. Each is read as its arguments, the environment it began with, its
        // open descriptors by number, and whose it is.
        expect((await watching).status).toBe(0);
        const handed = async (what: string) => {
          const [argv, environment, descriptors, whose] = (await ssh(`sudo cat /root/handed/${what}`)).stdout.trim().split("\n==\n").map((part) => part.split("\n"));
          return { argv, environment, whose, descriptors: Object.fromEntries(descriptors!.map((line) => [line.slice(0, line.indexOf(" ")), line.slice(line.indexOf(" ") + 1)])) };
        };
        const uid = (await ssh("id -u")).stdout.trim();
        const files = ["manifest.json", "manifest.json.sig", "release.tar.gz"].map((name) => `/home/tester/.cache/surogate/updates/${NEXT}/${name}`);
        // pkexec, by the app: its whole path and the helper's, no agent of its own, the three files by
        // their whole paths, and of the app's environment a PATH alone. It runs as root for the user.
        // Its output is nowhere, and what it says goes to the app.
        const byApp = await handed("pkexec");
        expect(byApp).toMatchObject({
          argv: ["/usr/bin/pkexec", "--disable-internal-agent", HELPER, "--apply", ...files], environment: ["PATH=/usr/bin:/bin"], whose: [`Uid:\t${uid}\t0\t0\t0`],
          descriptors: { 0: "/dev/null", 1: "/dev/null", 2: expect.stringMatching(/^socket:/) },
        });
        // The helper, by pkexec: bash as the script's first line names it, the same arguments, and an
        // environment that is pkexec's own making. Root's name and home, the PATH and the locale of
        // the computer's own settings (24.04), and the user's number, which 26.04's pkexec also gives
        // under sudo's two names. Nothing of the app's or of the user's session is in it.
        const byPkexec = await handed("helper");
        expect(byPkexec).toMatchObject({ argv: ["/bin/bash", "-p", HELPER, "--apply", ...files], whose: ["Uid:\t0\t0\t0\t0"] });
        expect(byPkexec.environment!.map((entry) => entry.split("=")[0]).sort())
          .toEqual(release === "24.04" ? ["HOME", "LANG", "LOGNAME", "PATH", "PKEXEC_UID", "USER"] : ["HOME", "LOGNAME", "PATH", "PKEXEC_UID", "SUDO_GID", "SUDO_UID", "USER"]);
        expect(byPkexec.environment).toEqual(expect.arrayContaining(["HOME=/root", "LOGNAME=root", "USER=root", `PKEXEC_UID=${uid}`]));
        // The helper has pkexec's three descriptors and no other of its: what else it has open is
        // bash's own, the script it reads and what the script opened.
        const { 0: input, 1: output, 2: errors, 255: script, ...opened } = byPkexec.descriptors;
        expect([input, output, errors, script]).toEqual(["/dev/null", "/dev/null", byApp.descriptors["2"], HELPER]);
        // And pkexec itself, which is root's process for as long as its prompt is open, held none of
        // what the app's main process has open: no file at all, and beside its three no more than
        // the few it opens for itself, its connection to the system's bus among them.
        const { 0: _input, 1: _output, 2: _errors, ...held } = byApp.descriptors;
        expect(Object.values(held).filter((file) => file.startsWith("/"))).toEqual([]);
        expect(Object.keys(held).length).toBeLessThan(12);
        expect(Object.keys(opened).every((fd) => Number(fd) > 2)).toBe(true);

        // The administrator rolls back: the version before, with the binding still there, and the
        // helper pkexec runs still the update's.
        const back = await ssh(`curl -fsSL ${base}/desktop/install.sh | bash -s -- --version ${VERSION}`);
        expect(back.status, back.stderr).toBe(0);
        expect(back.stdout).not.toContain("downloading");
        expect(await current()).toBe(`/opt/surogate/versions/${VERSION}`);
        expect([await helperOf(), await installedOf()]).toEqual([NEXT, VERSION]);
        // The version that runs again finds the update again, and offers it.
        expect(await started("tester", "--check", VERSION)).toEqual({ version: VERSION, binding: bound!.binding, found: "available", line: AVAILABLE, logged: [] });
        // And takes it again, through the update's own helper, which finds its version here whole.
        expect((await ssh(`rm -f /home/tester/probe-${NEXT}.json`)).status).toBe(0);
        const again = await started("tester", "--update", VERSION);
        expect(again, JSON.stringify(again)).toMatchObject({ found: "available", update: { state: "installed", version: NEXT }, logged: [] });
        expect(await written("tester", NEXT)).toEqual({ version: NEXT, binding: bound!.binding });
        expect([await current(), await helperOf(), await installedOf()]).toEqual([`/opt/surogate/versions/${NEXT}`, NEXT, NEXT]);
      } finally {
        stop();
      }
    });
  }
});
