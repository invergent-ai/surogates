// The install script (release/install.sh) in Ubuntu containers, never on this computer: its
// --apply, which each version ships as bin/surogate-apply-update and runs as root; the install from
// a server, as a user with sudo; and --uninstall. Releases are small stand-ins in the tarball's
// layout, signed by a key of the test's own. Behind SUROGATE_INSTALL_TESTS=1: it needs Docker, the
// ubuntu:24.04 and ubuntu:26.04 images, and the Ubuntu archive for apt. The script's own list of
// release keys is read without either.

import { type ChildProcess, execFile, spawn, spawnSync } from "node:child_process";
import { createHash, createPublicKey, generateKeyPairSync, type KeyObject, randomBytes, sign } from "node:crypto";
import { chmodSync, copyFileSync, existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, renameSync, rmSync, statSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from "vitest";

const SCRIPT = fileURLToPath(new URL("../release/install.sh", import.meta.url));
const PUBLISH = fileURLToPath(new URL("../release/publish.sh", import.meta.url));
const RELEASES = ["24.04", "26.04"] as const;
const ENABLED = process.env.SUROGATE_INSTALL_TESTS === "1";
// The longest one docker call may take: an install with apt's downloads takes under a minute.
const CALL_MS = 300_000;

// A static server of the folder it is given, on a port of its own, which it prints. A proxy's
// request, which names the whole URL, is served by its path alike.
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

// The helper stopped (SIGKILL) before each command it runs, in turn, each time from the install
// kept in /opt/pristine. A line for each stop: what current names and whether that folder is
// whole, then how the same apply, run again to its end, exits and what it leaves.
const STATE = String.raw`
state() {
  local now
  now="$(readlink /opt/surogate/current)"
  if [ ! -e "$now" ]; then echo "$(basename "$now") gone"
  elif [ -f "$now/release.json" ] && [ -x "$now/surogate" ] && [ -x "$now/bin/bwrap" ] && [ -x "$now/bin/surogate-apply-update" ]; then echo "$(basename "$now") whole"
  else echo "$(basename "$now") broken"
  fi
}
`;
const STOPS = String.raw`${STATE}
for stop in $(seq 1000); do
  find /opt/surogate -mindepth 1 -delete
  cp -a /opt/pristine/. /opt/surogate/
  STOP="$stop" bash -T -c 'n=0; trap "(( ++n == STOP )) && kill -KILL \$\$" DEBUG; . /opt/surogate-test/install.sh "$@"' stopped --apply "$@" >/dev/null 2>&1
  [ "$?" -eq 137 ] || { echo "end $stop"; exit 0; }
  stopped="$(state)"
  /opt/surogate-test/install.sh --apply "$@" >/dev/null 2>&1
  echo "$stop: $stopped; again $?: $(state), versions $(ls /opt/surogate/versions | tr '\n' ' '), staging $(ls -A /opt/surogate/staging | wc -l)"
done
`;

// The helper stopped by a signal (SIGTERM) to itself before each command its own shell runs, in
// turn, each time from the install kept in /opt/pristine. A line for each stop: how the helper
// ended, what current names and whether that folder is whole, and how much is left in staging.
const SIGNALS = String.raw`${STATE}
for stop in $(seq 1000); do
  find /opt/surogate -mindepth 1 -delete
  cp -a /opt/pristine/. /opt/surogate/
  rm -f /tmp/sent
  STOP="$stop" bash -T -c 'n=0; trap "(( BASHPID == \$\$ )) && (( ++n == STOP )) && { : >/tmp/sent; kill -TERM \$\$; }" DEBUG; . /opt/surogate-test/install.sh "$@"' stopped --apply "$@" >/dev/null 2>&1
  ended="$?"
  [ -e /tmp/sent ] || { echo "end $stop"; exit 0; }
  echo "$stop: $ended $(state), staging $(ls -A /opt/surogate/staging 2>/dev/null | wc -l)"
done
`;

// The system's rm, kept as /opt/hold/rm, which sends a signal to all of the helper's processes, itself
// among them, as it is asked to clear an apply's folder: the second signal of two.
const SIGNALLING = String.raw`#!/bin/sh
case "$*" in *" /opt/surogate/staging/apply."*) kill -TERM 0 ;; esac
exec /opt/hold/rm "$@"
`;

// The system's rm, kept as /opt/cut/rm, until it is asked to remove /opt/surogate itself: there it
// takes one file of each version and ends the script that runs it, as a kill does, with the rest of
// the tree in place.
const CUT = String.raw`#!/bin/sh
for operand in "$@"; do
  if [ "$operand" = /opt/surogate ]; then
    /opt/cut/rm -f /opt/surogate/versions/*/resources/app/package.json
    kill -KILL "$PPID"
    exit 137
  fi
done
exec /opt/cut/rm "$@"
`;

// The system's own tool, kept as /opt/hold/<its name>, which waits where it is told to: the first
// time *operand* is among its operands, it says so in /tmp/held, and goes on once /tmp/go is
// there, or after 20 s.
const holding = (operand: string) => String.raw`#!/bin/sh
for operand in "$@"; do
  if [ "$operand" = ${operand} ] && [ ! -e /tmp/held ]; then
    touch /tmp/held
    for try in $(seq 400); do [ -e /tmp/go ] && break; sleep 0.05; done
  fi
done
exec "/opt/hold/$(basename "$0")" "$@"
`;

// The system's mktemp, kept as /opt/hold/mktemp. A name it gives out for which nothing is there yet
// is taken at once by another user of the computer, as a folder with a file of their own in it,
// and written down in /tmp/taken: what one who watches for the name can do before the script makes
// its folder there.
const TAKING = String.raw`#!/bin/sh
name="$(/opt/hold/mktemp "$@")" || exit
[ -e "$name" ] || { runuser -u other -- mkdir "$name" 2>/dev/null && runuser -u other -- touch "$name/theirs" && echo "$name" >>/tmp/taken; }
echo "$name"
`;

// The system's mktemp, kept as /opt/hold/mktemp, which sends *signal* to all of the script's
// processes, itself among them, once it has made a folder and before it has said its name, as a
// terminal's signal may come: each folder it made is written down in /tmp/made.
const interrupting = (signal: string) => String.raw`#!/bin/sh
made="$(/opt/hold/mktemp "$@")" || exit
[ ! -d "$made" ] || { echo "$made" >>/tmp/made; kill -${signal} 0; }
echo "$made"
`;

// The lock that one install, update or removal at a time holds, in root's own folder under /run;
// and a shell of the test's own that holds it, on its descriptor 8.
const LOCKS = "/run/surogate-desktop";
const HOLD = `mkdir -p -m 700 ${LOCKS} && exec 8>>${LOCKS}/lock && flock 8`;
// What stands in the folder's place and is not as root makes it, each with how it came there.
const NOT_ROOTS_OWN: Array<[string, string]> = [
  ["another user's folder", `mkdir -m 700 ${LOCKS} && chown tester: ${LOCKS}`],
  ["a folder its group opens", `mkdir -m 750 ${LOCKS}`],
  ["a folder all open", `mkdir -m 755 ${LOCKS}`],
  ["a link to a folder of root's own", `mkdir -m 700 /root/locks && ln -s /root/locks ${LOCKS}`],
  ["a link to another user's folder", `mkdir -m 700 /home/tester/locks && chown tester: /home/tester/locks && ln -s /home/tester/locks ${LOCKS}`],
  ["a file of root's own that no one else opens", `touch ${LOCKS} && chmod 700 ${LOCKS}`],
];
const NOT_ROOTS_OWN_SAID = `Surogate Desktop: ${LOCKS} must be a folder of root's own that no one else opens (mode 700), and no link: remove what is there, and run this again\n`;
// What the script says of an /opt/surogate that is a link, before it follows it; *then* is what it asks for.
const linked = (then = "remove the link, and run this again") =>
  `Surogate Desktop: /opt/surogate is a link, where Surogate Desktop keeps a folder of its own or a disk mounted there: ${then}\n`;
const LINKED_NOTHING_REMOVED = linked("nothing was removed. Remove the link, and run this again: what it names is then yours to remove");
// What --apply needs, on a desktop's baseline: openssl, jq and bubblewrap, which the install
// script installs, and nothing of Surogate's; strace, for the tests that read the helper's system
// calls; and a locale as a desktop's user has one, en_US.UTF-8.
const APPLY_LAB = [
  "RUN apt-get update && apt-get install -y --no-install-recommends openssl jq bubblewrap && rm -rf /var/lib/apt/lists/*",
  "RUN useradd -m tester",
  "RUN apt-get update && apt-get install -y --no-install-recommends strace && rm -rf /var/lib/apt/lists/*",
  "RUN apt-get update && apt-get install -y --no-install-recommends locales && localedef -i en_US -f UTF-8 en_US.UTF-8 && rm -rf /var/lib/apt/lists/*",
];
// /opt/surogate as a small disk of the container's own, in memory: a copy with no bound fills
// that, and never this computer's disk.
const OWN_DISK = ["--tmpfs", "/opt/surogate:exec,mode=755,size=512m"];

const sha256 = (data: Buffer) => createHash("sha256").update(data).digest("hex");

// The program that holds Electron's place in a test's release: this computer's sleep, GNU's. Where
// Ubuntu's own sleep is uutils' (26.04), it is one program of many, which runs under no other name
// and needs a newer libc than 24.04's: GNU's is beside it, as gnusleep.
const SLEEP = existsSync("/usr/bin/gnusleep") ? "/usr/bin/gnusleep" : "/usr/bin/sleep";

// Keys of the test's own, and the script with their public halves in the release keys' place.
const keys = generateKeyPairSync("ed25519");
const next = generateKeyPairSync("ed25519");
const other = generateKeyPairSync("ed25519");
const pem = (key: KeyObject) => key.export({ type: "spki", format: "pem" }).toString().trim();
const PUBLIC = pem(keys.publicKey);
const withKeys = (script: string, trusted = [PUBLIC]) =>
  script.replace(/RELEASE_KEYS=\(\n[^)]*\)/, `RELEASE_KEYS=(\n${trusted.map((key) => `    '${key}'`).join("\n")}\n  )`);

// A container of *release*, built from *setup*'s lines, and what the tests do with it.
function lab(release: string, setup: string[], run: string[] = []) {
  const it = { dir: "", container: "", image: "" };
  // No credentials of this user's reach the image's pull or the container.
  const env = () => ({ ...process.env, DOCKER_CONFIG: join(it.dir, "docker") });
  // No call outlives CALL_MS: it is synchronous, so a command that never ended would hold the worker for good.
  const docker = (args: string[]) => spawnSync("docker", args, { encoding: "utf8", env: env(), maxBuffer: 64 * 1024 * 1024, timeout: CALL_MS });
  const root = (command: string) => docker(["exec", it.container, "bash", "-c", command]);
  const as = (user: string, command: string) => docker(["exec", "-u", user, "-w", `/home/${user}`, "-e", `HOME=/home/${user}`, it.container, "bash", "-c", command]);

  // A release in the tarball's layout: Electron's place held by this computer's sleep, so that a
  // version can be seen running; *change* edits its tree before it is tarred.
  const releaseOf = (version: string, change: (top: string) => void = () => {}) => {
    const name = `surogate-desktop-${version}-linux-x64`;
    const tree = mkdtempSync(join(it.dir, "tree-"));
    const top = join(tree, name);
    mkdirSync(join(top, "bin"), { recursive: true });
    mkdirSync(join(top, "resources", "app"), { recursive: true });
    copyFileSync(SLEEP, join(top, "surogate"));
    writeFileSync(join(top, "bin", "surogate-apply-update"), withKeys(readFileSync(SCRIPT, "utf8")), { mode: 0o755 });
    writeFileSync(join(top, "resources", "app", "package.json"), JSON.stringify({ version }));
    change(top);
    const tarball = join(it.dir, `${name}.tar.gz`);
    expect(spawnSync("tar", ["--owner=0", "--group=0", "-C", tree, "-czf", tarball, ...readdirSync(tree)]).status).toBe(0);
    return tarball;
  };
  // Its manifest, as the release job writes it, signed by *key*, into <dir>/manifest.json and its
  // .sig; *fields* replace its own.
  const manifestOf = (version: string, tarball: string, fields: Record<string, unknown> = {}, key: KeyObject = keys.privateKey) => {
    const manifest = Buffer.from(`${JSON.stringify({
      version, channel: "stable", platform: "linux", arch: "x64",
      url: `releases/${version}/surogate-desktop-${version}-linux-x64.tar.gz`, sha256: sha256(readFileSync(tarball)), size: statSync(tarball).size, ...fields,
    })}\n`);
    writeFileSync(join(it.dir, "manifest.json"), manifest);
    writeFileSync(join(it.dir, "manifest.json.sig"), sign(null, manifest, key));
    return manifest;
  };
  const current = () => root("readlink /opt/surogate/current").stdout.trim();
  const versions = () => root("ls /opt/surogate/versions").stdout.trim().split("\n").filter(Boolean);
  // The system's *tool* with *standIn* in its place, which finds the tool itself as
  // /opt/hold/<tool>, for the one docker call that runs *lines*.
  const swapped = (tool: string, standIn: string, lines: string[]) => {
    writeFileSync(join(it.dir, "stand-in"), standIn, { mode: 0o755 });
    expect(docker(["cp", join(it.dir, "stand-in"), `${it.container}:/opt/surogate-test/stand-in`]).status).toBe(0);
    return root([
      `mkdir -p /opt/hold && cp -L /usr/bin/${tool} /opt/hold/${tool}`,
      `mv /usr/bin/${tool} /usr/bin/${tool}.away && cp /opt/surogate-test/stand-in /usr/bin/${tool}`,
      ...lines,
      `mv -f /usr/bin/${tool}.away /usr/bin/${tool}`,
    ].join("\n"));
  };

  beforeAll(async () => {
    it.dir = mkdtempSync(join(tmpdir(), "install-test-"));
    mkdirSync(join(it.dir, "docker"));
    mkdirSync(join(it.dir, "image"));
    writeFileSync(join(it.dir, "image", "Dockerfile"), [`FROM ubuntu:${release}`, ...setup].join("\n"));
    it.image = `surogate-install-test:${release}-${sha256(Buffer.from(setup.join("\n"))).slice(0, 12)}`;
    // Off the event loop: a first build takes minutes, and vitest's RPC answers must still get in.
    await promisify(execFile)("docker", ["build", "-q", "-t", it.image, join(it.dir, "image")], { env: env() });
    // Root's own ptrace right, which Docker leaves out: root reads every process's program, as on a computer.
    it.container = docker(["run", "-d", "--rm", "--cap-add", "SYS_PTRACE", ...run, it.image, "sleep", "infinity"]).stdout.trim();
    expect(it.container).not.toBe("");
    writeFileSync(join(it.dir, "install.sh"), withKeys(readFileSync(SCRIPT, "utf8")), { mode: 0o755 });
    expect(root("mkdir -p /opt/surogate-test").status).toBe(0);
    expect(docker(["cp", join(it.dir, "install.sh"), `${it.container}:/opt/surogate-test/install.sh`]).status).toBe(0);
  }, 900_000);

  // Each docker call blocks the event loop: a turn of it between tests lets vitest's RPC answers in,
  // which otherwise time out after 60 s and fail the run however its tests went.
  afterEach(() => new Promise((resolve) => setTimeout(resolve, 0)));

  afterAll(() => {
    if (it.container) docker(["rm", "-f", it.container]);
    rmSync(it.dir, { recursive: true, force: true });
  });

  // Another computer of the same image, started with *run*, for what the tests' own cannot be made
  // into: handed the script and *files*, in /srv, used by *use*, then removed.
  const elsewhere = (run: string[], files: string[], use: (root: (command: string) => ReturnType<typeof docker>) => void) => {
    const container = docker(["run", "-d", "--rm", "--cap-add", "SYS_PTRACE", ...run, it.image, "sleep", "infinity"]).stdout.trim();
    expect(container).not.toBe("");
    try {
      for (const file of [join(it.dir, "install.sh"), ...files]) expect(docker(["cp", file, `${container}:/srv/`]).status, file).toBe(0);
      use((command) => docker(["exec", container, "bash", "-c", command]));
    } finally {
      docker(["rm", "-f", container]);
    }
  };

  return { it, docker, root, as, releaseOf, manifestOf, current, versions, swapped, elsewhere };
}

describe("the install script's release keys", () => {
  it("are Ed25519 public keys, each written as OpenSSL writes one: an entry that does not load is skipped without a word, and every release refused", () => {
    const list = /RELEASE_KEYS=\(\n([^)]*)\)/.exec(readFileSync(SCRIPT, "utf8"))?.[1] ?? "";
    const entries = [...list.matchAll(/'([^']*)'/g)].map((match) => match[1] ?? "");
    expect(entries.length).toBeGreaterThan(0);
    // The list holds its entries and nothing between them.
    expect(list.replace(/'[^']*'/g, "").trim()).toBe("");
    for (const entry of entries) {
      const key = createPublicKey(entry);
      expect(key.asymmetricKeyType).toBe("ed25519");
      // No indent, no other line ends, nothing before or after: signed() hands the entry to openssl as it is.
      expect(entry).toBe(pem(key));
    }
  });
});

describe("the install script's waits", () => {
  it("are shorter for all an apply reads with the lock held than for the lock: one who was let apply once keeps no other waiting until it gives up", () => {
    const script = readFileSync(SCRIPT, "utf8");
    const seconds = (name: string) => Number(new RegExp(`^  ${name}=(\\d+)$`, "m").exec(script)?.[1]);
    // With the lock held, the asking user's processes read a manifest and a signature, each small, and a tarball.
    expect(seconds("LOCK_WAIT")).toBeGreaterThan(2 * seconds("SMALL_WAIT") + seconds("READ_WAIT"));
  });
});

for (const release of RELEASES) describe.skipIf(!ENABLED)(`the install script's --apply, on Ubuntu ${release}`, { timeout: 120_000 }, () => {
  const { it: box, docker, root, as, releaseOf, manifestOf, current, versions, swapped, elsewhere } = lab(release, APPLY_LAB, OWN_DISK);
  // The files as the app leaves them for the helper: in the user's cache, copied into the container.
  const files = (manifest = "/home/tester/manifest.json", tarball = "/home/tester/release.tar.gz") => `${manifest} /home/tester/manifest.json.sig ${tarball}`;
  const stage = (tarball: string) => {
    for (const file of ["manifest.json", "manifest.json.sig"]) expect(docker(["cp", join(box.dir, file), `${box.container}:/home/tester/${file}`]).status).toBe(0);
    expect(docker(["cp", tarball, `${box.container}:/home/tester/release.tar.gz`]).status).toBe(0);
  };
  const apply = (tarball: string, script = "install.sh") => {
    stage(tarball);
    return root(`/opt/surogate-test/${script} --apply ${files()}`);
  };

  // The helper stopped where it waits for the update's lock, after every check it makes of a name,
  // while the user runs *swap*; then let go on. *asked* is how pkexec or sudo names the user it
  // runs the helper for.
  const held = (swap: string, manifest?: string, tarball?: string, asked = "") => root([
    HOLD,
    `${asked} timeout 60 /opt/surogate-test/install.sh --apply ${files(manifest, tarball)} 8<&- &`,
    "for try in $(seq 200); do pgrep -x flock >/dev/null && break; sleep 0.05; done",
    "pgrep -x flock >/dev/null || exit 9",
    `runuser -u tester -- bash -c '${swap}'`,
    "flock -u 8",
    "wait $!",
  ].join("\n"));

  // Every stop of the apply of *these*, from the install as it is now: by a kill, or by a signal.
  const stops = (these = files(), script = STOPS) => {
    writeFileSync(join(box.dir, "stops.sh"), script);
    expect(docker(["cp", join(box.dir, "stops.sh"), `${box.container}:/opt/surogate-test/stops.sh`]).status).toBe(0);
    expect(root("rm -rf /opt/pristine && cp -a /opt/surogate /opt/pristine").status).toBe(0);
    const lines = root(`bash /opt/surogate-test/stops.sh ${these}`).stdout.trim().split("\n");
    expect(lines.pop()).toMatch(/^end \d{2,}$/);
    return lines;
  };

  // The apply of the staged files, and the system calls it and its commands made to flush a
  // filesystem and to rename, in order.
  const traced = () => {
    const apply = root(`strace -f -qq -o /tmp/trace -e trace=syncfs,rename,renameat,renameat2 /opt/surogate-test/install.sh --apply ${files()} >/dev/null && cat /tmp/trace`);
    expect(apply.status, apply.stderr).toBe(0);
    const calls = apply.stdout.split("\n");
    return (call: RegExp) => calls.findIndex((line) => call.test(line));
  };

  beforeEach(() => {
    expect(root("find /opt/surogate -mindepth 1 -delete").status).toBe(0);
  });

  it("applies a signed release into its version folder, root's alone, with the system's bwrap, and switches current to it", () => {
    const tarball = releaseOf("1.0.0");
    manifestOf("1.0.0", tarball);
    expect(apply(tarball)).toMatchObject({ status: 0, stdout: "Surogate Desktop: 1.0.0 is installed\n" });
    expect(current()).toBe("/opt/surogate/versions/1.0.0");
    expect(root("cmp /usr/bin/bwrap /opt/surogate/current/bin/bwrap && cmp /home/tester/manifest.json /opt/surogate/current/release.json").status).toBe(0);
    // The helper pkexec runs: the current version's, at a path with no link in it.
    expect(root("test ! -L /opt/surogate/bin && cmp /opt/surogate/current/bin/surogate-apply-update /opt/surogate/bin/surogate-apply-update").status).toBe(0);
    // Nothing in it is anyone's but root's, or writable by anyone else.
    expect(root("find /opt/surogate ! -type l \\( ! -user root -o -perm /022 \\) -print").stdout).toBe("");
    expect(root("stat -c '%a %U' /opt/surogate /opt/surogate/versions /opt/surogate/staging /opt/surogate/bin/surogate-apply-update").stdout).toBe("755 root\n755 root\n700 root\n755 root\n");
  });

  it("refuses a manifest the release key did not sign, one for another computer or channel, and a tarball it does not name", () => {
    const tarball = releaseOf("1.0.0");
    manifestOf("1.0.0", tarball, {}, other.privateKey);
    expect(apply(tarball)).toMatchObject({ status: 1, stderr: "Surogate Desktop: the release's manifest is not signed by Surogate's release key\n" });
    // A field is whole or refused: jq's $ also matches before a last newline.
    const hash = sha256(readFileSync(tarball));
    for (const fields of [{ arch: "arm64" }, { platform: "darwin" }, { channel: "beta" }, { url: "https://elsewhere.example/r.tar.gz" }, { version: "1.0" },
      { version: "1.0.0\n", url: "releases/1.0.0\n/surogate-desktop-1.0.0\n-linux-x64.tar.gz" }, { sha256: `${hash}\n` },
      // Its tarball's size is a whole number of bytes, above 0 and below 10^15, or it is no release: undefined leaves the field out.
      { size: undefined }, { size: null }, { size: 0 }, { size: -1 }, { size: 1.5 }, { size: "4096" }, { size: [4096] }, { size: 1e15 }, { size: 1e300 }]) {
      manifestOf("1.0.0", tarball, fields);
      expect(apply(tarball), JSON.stringify(fields)).toMatchObject({ status: 1, stderr: "Surogate Desktop: the release's manifest is not a release of Surogate Desktop for this computer\n" });
    }
    manifestOf("1.0.0", tarball, { sha256: "0".repeat(64) });
    expect(apply(tarball)).toMatchObject({ status: 1, stderr: "Surogate Desktop: the downloaded release is not the one its manifest names\n" });

    // A file it is handed as a link is never read through it: here a signed manifest that the user may not read.
    manifestOf("1.0.0", tarball);
    stage(tarball);
    expect(root(`cp /home/tester/manifest.json /root/manifest.json && ln -sf /root/manifest.json /home/tester/linked.json && /opt/surogate-test/install.sh --apply ${files("/home/tester/linked.json")}`))
      .toMatchObject({ status: 1, stderr: "Surogate Desktop: /home/tester/linked.json is not a downloaded release's file\n" });
    // Nor one the user swaps once the helper has started: here while it waits for the update's lock,
    // after any check of the name. A link is refused, and a pipe gives an empty copy at once.
    const swapped = (swap: string) => root([
      HOLD,
      "cp /home/tester/manifest.json /home/tester/swapped.json",
      `/opt/surogate-test/install.sh --apply ${files("/home/tester/swapped.json")} 8<&- &`,
      // A helper that ended before it reached the lock would never be seen waiting for it.
      "for try in $(seq 200); do pgrep -x flock >/dev/null && break; sleep 0.05; done",
      "pgrep -x flock >/dev/null || exit 9",
      swap,
      "flock -u 8",
      "wait $!",
    ].join("\n"));
    expect(swapped("ln -sf /root/manifest.json /home/tester/swapped.json"))
      .toMatchObject({ status: 1, stderr: "Surogate Desktop: /home/tester/swapped.json is not a downloaded release's file\n" });
    expect(swapped("rm /home/tester/swapped.json && mkfifo /home/tester/swapped.json"))
      .toMatchObject({ status: 1, stderr: "Surogate Desktop: the release's manifest is not signed by Surogate's release key\n" });
    expect(root("test -e /opt/surogate/current").status).toBe(1);
  });

  it("refuses a signed manifest that is more than one JSON document", () => {
    const tarball = releaseOf("1.0.0");
    const one = manifestOf("1.0.0", tarball);
    for (const after of [one, Buffer.from('{"version":"9.9.9"}\n'), Buffer.from("1\n")]) {
      const manifest = Buffer.concat([one, after]);
      writeFileSync(join(box.dir, "manifest.json"), manifest);
      writeFileSync(join(box.dir, "manifest.json.sig"), sign(null, manifest, keys.privateKey));
      expect(apply(tarball), after.toString()).toMatchObject({ status: 1, stdout: "", stderr: "Surogate Desktop: the release's manifest is not a release of Surogate Desktop for this computer\n" });
    }
    expect(root("test -e /opt/surogate/current").status).toBe(1);
  });

  it("refuses a tarball that is not the size its signed manifest names, longer or shorter, before it reads it for its hash", () => {
    const tarball = releaseOf("1.0.0");
    manifestOf("1.0.0", tarball);
    const size = statSync(tarball).size;
    const refusal = { status: 1, stdout: "", stderr: `Surogate Desktop: the downloaded release is not the ${size} bytes its manifest names\n` };
    for (const change of ["echo >>", "truncate -s +1G", "truncate -s -1", "truncate -s 0"]) {
      stage(tarball);
      expect(root(`${change} /home/tester/release.tar.gz && /opt/surogate-test/install.sh --apply ${files()}`), change).toMatchObject(refusal);
      expect(root("ls -A /opt/surogate/staging").stdout).toBe("");
    }
    // A manifest that names its tarball's hash and another size.
    for (const named of [size + 1, size - 1]) {
      manifestOf("1.0.0", tarball, { size: named });
      expect(apply(tarball), `${named}`).toMatchObject({ status: 1, stdout: "", stderr: `Surogate Desktop: the downloaded release is not the ${named} bytes its manifest names\n` });
    }
    expect(root("test -e /opt/surogate/current").status).toBe(1);
    // The room it needs is the room for the size its manifest names, whatever the file it is handed holds.
    manifestOf("1.0.0", tarball, { size: 999_999_999_999_999 });
    stage(tarball);
    expect(root(`/opt/surogate-test/install.sh --apply ${files()}`))
      .toMatchObject({ status: 1, stdout: "", stderr: expect.stringMatching(/^Surogate Desktop: \/opt\/surogate needs 3814697266 MB free to apply this release, and has \d+ MB\n$/) });
    // A size written another way is the same number.
    const written = Buffer.from(manifestOf("1.0.0", tarball).toString().replace(`"size":${size}`, `"size":${size}.0`));
    writeFileSync(join(box.dir, "manifest.json"), written);
    writeFileSync(join(box.dir, "manifest.json.sig"), sign(null, written, keys.privateKey));
    expect(written.toString()).toContain(`"size":${size}.0`);
    expect(apply(tarball)).toMatchObject({ status: 0, stdout: "Surogate Desktop: 1.0.0 is installed\n" });
  });

  it("takes the update's lock where only root can, so that no other user of the computer stalls it, and gives up on a lock held too long", () => {
    const tarball = releaseOf("1.0.0");
    manifestOf("1.0.0", tarball);
    expect(apply(tarball).status).toBe(0);
    // Any user may open a folder that all may read, and hold a lock on it: /opt/surogate is one.
    expect(docker(["exec", "-d", "-u", "tester", box.container, "bash", "-c", "exec 7</opt/surogate && flock 7 && touch /tmp/held && sleep 300"]).status).toBe(0);
    expect(root("for try in $(seq 100); do [ -e /tmp/held ] && break; sleep 0.05; done; test -e /tmp/held").status).toBe(0);
    const stalled = root(`timeout 20 /opt/surogate-test/install.sh --apply ${files()}`);
    root("pkill -u tester -x sleep; rm -f /tmp/held");
    expect(stalled).toMatchObject({ status: 0, stdout: "Surogate Desktop: 1.0.0 is installed\n" });
    // The lock is in a folder that is root's alone.
    expect(as("tester", `exec 7<${LOCKS}/lock`)).toMatchObject({ status: 1, stderr: expect.stringContaining("Permission denied") });
    // Held by another apply for longer than this one waits, here a second: it says so.
    const impatient = withKeys(readFileSync(SCRIPT, "utf8")).replace("LOCK_WAIT=300", "LOCK_WAIT=1");
    writeFileSync(join(box.dir, "impatient.sh"), impatient, { mode: 0o755 });
    expect(docker(["cp", join(box.dir, "impatient.sh"), `${box.container}:/opt/surogate-test/impatient.sh`]).status).toBe(0);
    expect(root(`${HOLD} && /opt/surogate-test/impatient.sh --apply ${files()} 8<&-`))
      .toMatchObject({ status: 1, stdout: "", stderr: "Surogate Desktop: another install or update of Surogate Desktop is still running: try again once it has finished\n" });
  });

  it("makes the folder of its lock itself, closed to everyone else from its first moment, and refuses what stands in its place and is not root's own", () => {
    const tarball = releaseOf("1.0.0");
    manifestOf("1.0.0", tarball);
    stage(tarball);
    expect(root(`rm -rf ${LOCKS}`).status).toBe(0);
    const made = root(`strace -f -qq -o /tmp/trace -e trace=mkdir,mkdirat /opt/surogate-test/install.sh --apply ${files()} >/dev/null && grep -F '${LOCKS}"' /tmp/trace`);
    expect(made.stdout).toMatch(/^\d+ +mkdir\("\/run\/surogate-desktop", 0700\) += 0\n$/);
    // So is staging, where an apply copies and unpacks: made closed, and not closed once it is made.
    expect(root("grep -E 'staging\", [0-7]+\\) += 0$' /tmp/trace").stdout).toMatch(/^\d+ +mkdir\("(\/opt\/surogate\/)?staging", 0700\) += 0\n$/);
    expect(root(`stat -c '%F %U %a' ${LOCKS} ${LOCKS}/lock`).stdout).toBe("directory root 700\nregular empty file root 644\n");
    expect(as("tester", `ls ${LOCKS}`)).toMatchObject({ status: 2, stderr: expect.stringContaining("Permission denied") });
    for (const [what, how] of NOT_ROOTS_OWN) {
      expect(root(`find /opt/surogate -mindepth 1 -delete; rm -rf ${LOCKS} /root/locks /home/tester/locks; ${how}`).status, what).toBe(0);
      const before = root(`stat -c '%F %U %a' ${LOCKS}`).stdout;
      expect(root(`/opt/surogate-test/install.sh --apply ${files()}`), what).toMatchObject({ status: 1, stdout: "", stderr: NOT_ROOTS_OWN_SAID });
      // Refused as it is, and never taken over; nothing was put in it, behind it or in the tree.
      expect(root(`stat -c '%F %U %a' ${LOCKS}`).stdout, what).toBe(before);
      expect(root(`ls -A /opt/surogate; [ ! -d ${LOCKS} ] || ls -A ${LOCKS}/`).stdout, what).toBe("");
    }
    expect(root(`rm -rf ${LOCKS} /root/locks /home/tester/locks`).status).toBe(0);
    expect(root(`/opt/surogate-test/install.sh --apply ${files()}`)).toMatchObject({ status: 0, stdout: "Surogate Desktop: 1.0.0 is installed\n" });
  });

  it("says what is wrong with /run itself, where the folder of its lock is not there and cannot be made: missing, no folder, read-only, or full", () => {
    const tarball = releaseOf("1.0.0");
    manifestOf("1.0.0", tarball);
    const release = [join(box.dir, "manifest.json"), join(box.dir, "manifest.json.sig"), tarball];
    const files = `/srv/manifest.json /srv/manifest.json.sig /srv/${tarball.split("/").pop()}`;
    // Each on a computer of its own: /run taken away, a file in its place, mounted read-only, and with room for nothing more.
    const states: Array<[string, string[], string]> = [
      ["/run is missing", [], "rm -rf /run"],
      ["/run is no folder", [], "rm -rf /run && touch /run"],
      ["/run is read-only", ["--tmpfs", "/run:ro"], "! touch /run/any 2>/dev/null"],
      ["is /run full?", ["--tmpfs", "/run:nr_inodes=1"], "! touch /run/any 2>/dev/null"],
    ];
    for (const [why, run, how] of states) elsewhere(run, release, (root) => {
      // What an install left there, for a removal to take away.
      expect(root(`${how} && mkdir -p /opt/surogate/versions/1.0.0 && touch /opt/surogate/versions/1.0.0/release.json`).status, why).toBe(0);
      const refusal = { status: 1, stdout: "", stderr: `Surogate Desktop: ${LOCKS}, the folder of its lock, could not be made: ${why}\n` };
      expect(root(`/srv/install.sh --apply ${files}`), why).toMatchObject(refusal);
      expect(root("/srv/install.sh --uninstall"), why).toMatchObject(refusal);
      // Nothing was made in the tree, and nothing of it removed.
      expect(root("find /opt/surogate -mindepth 1 | sort").stdout, why).toBe("/opt/surogate/versions\n/opt/surogate/versions/1.0.0\n/opt/surogate/versions/1.0.0/release.json\n");
    });
  });

  it("makes its folders root's own, whoever's they were, and takes none of them that is a link", () => {
    const tarball = releaseOf("1.0.0");
    manifestOf("1.0.0", tarball);
    // The tree and its folders as another user's, which only root can have left so.
    const folders = "/opt/surogate /opt/surogate/versions /opt/surogate/bin /opt/surogate/staging";
    expect(root(`mkdir -p ${folders} && chown tester: ${folders} && chmod 777 ${folders}`).status).toBe(0);
    expect(apply(tarball)).toMatchObject({ status: 0, stdout: "Surogate Desktop: 1.0.0 is installed\n" });
    expect(root(`stat -c '%a %U:%G' ${folders}`).stdout).toBe("755 root:root\n755 root:root\n755 root:root\n700 root:root\n");
    // One of the three as a link, here to a folder of that user's: root makes no other folder its own, and works in none.
    for (const inner of ["staging", "versions", "bin"]) {
      expect(root(`find /opt/surogate -mindepth 1 -delete; rm -rf /home/tester/theirs; mkdir -m 755 /home/tester/theirs && chown tester: /home/tester/theirs && ln -s /home/tester/theirs /opt/surogate/${inner}`).status).toBe(0);
      expect(root(`/opt/surogate-test/install.sh --apply ${files()}`), inner).toMatchObject({
        status: 1, stdout: "", stderr: `Surogate Desktop: /opt/surogate/${inner} is a link, where Surogate Desktop keeps a folder of its own: remove it, and run this again\n`,
      });
      expect(root("ls -A /home/tester/theirs; stat -c '%a %U' /home/tester/theirs").stdout, inner).toBe("755 tester\n");
    }
  });

  it("applies a release under a folder that hands its group and its set-id bits on to what is made in it", () => {
    const tarball = releaseOf("1.0.0");
    manifestOf("1.0.0", tarball);
    // The tree's own folder so, as it is made under an /opt that is; then each folder in it, staging among them.
    const states = [
      "chgrp tester /opt/surogate && chmod 2775 /opt/surogate",
      "mkdir /opt/surogate/staging /opt/surogate/versions /opt/surogate/bin && chmod 7777 /opt/surogate/staging /opt/surogate/versions /opt/surogate/bin",
    ];
    for (const state of states) {
      expect(root(`find /opt/surogate -mindepth 1 -delete; ${state}`).status, state).toBe(0);
      expect(apply(tarball), state).toMatchObject({ status: 0, stdout: "Surogate Desktop: 1.0.0 is installed\n", stderr: "" });
      expect(root("stat -c '%a %U:%G' /opt/surogate /opt/surogate/versions /opt/surogate/bin /opt/surogate/staging").stdout, state).toBe("755 root:root\n755 root:root\n755 root:root\n700 root:root\n");
      // Nothing in the tree has a bit or a group of that folder's.
      expect(root("find /opt/surogate ! -type l \\( ! -user root -o ! -group root -o -perm /7022 \\) -print").stdout, state).toBe("");
    }
  });

  it("reads each file as the user who asked, so that a folder swapped for a link gets them nothing they could not read themselves", () => {
    const tarball = releaseOf("1.0.0");
    manifestOf("1.0.0", tarball);
    stage(tarball);
    // Where the app downloads an update, a folder of the user's own; and the signed manifest where only root reads it.
    const fresh = "rm -rf /home/tester/updates /home/tester/updates.real /root/updates && mkdir -m 700 /home/tester/updates /root/updates"
      + " && cp /home/tester/manifest.json /home/tester/manifest.json.sig /home/tester/release.tar.gz /home/tester/updates/"
      + " && chmod 600 /home/tester/updates/* && chown -R tester: /home/tester/updates && cp /home/tester/manifest.json /root/updates/";
    const swap = "mv /home/tester/updates /home/tester/updates.real && ln -s /root/updates /home/tester/updates";
    const user = "$(id -u tester)";
    // What that user cannot read is said as not theirs to read, and never as what root would find it to be.
    const theirs = "cannot be read by tester: name it by its whole path, in a folder of that user's own\n";
    for (const asked of [`PKEXEC_UID=${user}`, `SUDO_UID=${user}`, `PKEXEC_UID=0 SUDO_UID=${user}`]) {
      expect(root(fresh).status).toBe(0);
      expect(held(swap, "/home/tester/updates/manifest.json", undefined, asked), asked)
        .toMatchObject({ status: 1, stdout: "", stderr: `Surogate Desktop: /home/tester/updates/manifest.json ${theirs}` });
    }
    expect(root("test -e /opt/surogate/current").status).toBe(1);
    // What names the user is a number, of a user of this computer.
    expect(root(`PKEXEC_UID=tester /opt/surogate-test/install.sh --apply ${files()}`))
      .toMatchObject({ status: 1, stderr: "Surogate Desktop: PKEXEC_UID is not a user's number\n" });
    expect(root(`SUDO_UID=4242 /opt/surogate-test/install.sh --apply ${files()}`))
      .toMatchObject({ status: 1, stderr: "Surogate Desktop: SUDO_UID names no user of this computer\n" });
    // Who reads is the user that number names, and no other. A number past the last one counts
    // from 0 again, where root is, and then the user.
    for (const past of ["4294967296", `$(( 4294967296 + ${user} ))`]) {
      expect(root(`PKEXEC_UID=${past} /opt/surogate-test/install.sh --apply ${files()}`), past)
        .toMatchObject({ status: 1, stdout: "", stderr: "Surogate Desktop: PKEXEC_UID names no user of this computer\n" });
    }
    // And digits are a user's name first, where a user is so named, as a company's directory may name one.
    expect(root(`echo "${user}:x:1600:1600::/nonexistent:/bin/sh" >>/etc/passwd && mkdir -m 700 /srv/numbered && cp ${files()} /srv/numbered/ && chown -R 1600 /srv/numbered`).status).toBe(0);
    const numbered = root(`PKEXEC_UID=${user} /opt/surogate-test/install.sh --apply /srv/numbered/manifest.json /srv/numbered/manifest.json.sig /srv/numbered/release.tar.gz; said=$?; sed -i '$d' /etc/passwd; exit $said`);
    expect(numbered).toMatchObject({ status: 1, stdout: "", stderr: "Surogate Desktop: PKEXEC_UID names no user of this computer\n" });
    expect(root("test -e /opt/surogate/current").status).toBe(1);
    // So it is with a release kept in root's home, as a root shell that sudo started would name one;
    // with a name that is no whole path, which under pkexec is looked for in root's home; with a
    // file that is there for root and missing for the user; and with one the user reads only as a
    // member of another group.
    expect(root(`rm -rf /root/kept /srv/shared && mkdir -m 700 /root/kept && cp ${files()} /root/kept/ && (getent group shared >/dev/null || groupadd shared) && gpasswd -a tester shared >/dev/null`
      + ` && mkdir -m 750 /srv/shared && cp ${files()} /srv/shared/ && chgrp -R shared /srv/shared`).status).toBe(0);
    expect(root(`SUDO_UID=${user} /opt/surogate-test/install.sh --apply /root/kept/manifest.json /root/kept/manifest.json.sig /root/kept/release.tar.gz`))
      .toMatchObject({ status: 1, stdout: "", stderr: `Surogate Desktop: /root/kept/manifest.json ${theirs}` });
    expect(root(`cd /root/kept && PKEXEC_UID=${user} /opt/surogate-test/install.sh --apply manifest.json manifest.json.sig release.tar.gz`))
      .toMatchObject({ status: 1, stdout: "", stderr: `Surogate Desktop: manifest.json ${theirs}` });
    expect(root(`PKEXEC_UID=${user} /opt/surogate-test/install.sh --apply /home/tester/manifest.json /root/kept/manifest.json.sig /home/tester/release.tar.gz`))
      .toMatchObject({ status: 1, stdout: "", stderr: `Surogate Desktop: /root/kept/manifest.json.sig ${theirs}` });
    expect(root(`PKEXEC_UID=${user} /opt/surogate-test/install.sh --apply /srv/shared/manifest.json /srv/shared/manifest.json.sig /srv/shared/release.tar.gz`))
      .toMatchObject({ status: 1, stdout: "", stderr: `Surogate Desktop: /srv/shared/manifest.json ${theirs}` });
    // A file of root's that root's own group reads: who reads has the user's group, and none of root's.
    expect(root(`rm -rf /srv/roots && mkdir -m 755 /srv/roots && cp ${files()} /srv/roots/ && chown -R root:root /srv/roots && chmod 640 /srv/roots/* && id -G`).stdout).toBe("0\n");
    expect(root(`PKEXEC_UID=${user} /opt/surogate-test/install.sh --apply /srv/roots/manifest.json /srv/roots/manifest.json.sig /srv/roots/release.tar.gz`))
      .toMatchObject({ status: 1, stdout: "", stderr: `Surogate Desktop: /srv/roots/manifest.json ${theirs}` });
    // What the user cannot read is refused before anything is made: no folder of the tree's, and none for the lock.
    expect(root(`find /opt/surogate -mindepth 1 -delete; rm -rf ${LOCKS}`).status).toBe(0);
    expect(root(`PKEXEC_UID=${user} /opt/surogate-test/install.sh --apply /root/kept/manifest.json /root/kept/manifest.json.sig /root/kept/release.tar.gz`))
      .toMatchObject({ status: 1, stdout: "", stderr: `Surogate Desktop: /root/kept/manifest.json ${theirs}` });
    expect(root(`ls -A /opt/surogate; test ! -e ${LOCKS}`)).toMatchObject({ status: 0, stdout: "" });
    // And who reads holds none of the helper's open files: not its descriptor 9, which is the
    // lock's, and here its caller's. A file that is there only for a process that has it open is not there.
    expect(root(`/opt/surogate-test/install.sh --apply /proc/self/fdinfo/9 /home/tester/manifest.json.sig /home/tester/release.tar.gz 9</dev/null`))
      .toMatchObject({ status: 1, stdout: "", stderr: "Surogate Desktop: /proc/self/fdinfo/9 is not a downloaded release's file\n" });
    // Root, asked for no one, is told what it was before: the file is its own to read.
    expect(root("/opt/surogate-test/install.sh --apply /root/kept/none.json /root/kept/manifest.json.sig /root/kept/release.tar.gz"))
      .toMatchObject({ status: 1, stdout: "", stderr: "Surogate Desktop: /root/kept/none.json is not a downloaded release's file\n" });
    // The user's own files, in a folder only they open, are read as theirs.
    expect(root(fresh).status).toBe(0);
    const own = "/home/tester/updates/manifest.json /home/tester/updates/manifest.json.sig /home/tester/updates/release.tar.gz";
    expect(root(`PKEXEC_UID=${user} /opt/surogate-test/install.sh --apply ${own}`)).toMatchObject({ status: 0, stdout: "Surogate Desktop: 1.0.0 is installed\n" });
  });

  it("copies no more than a release's own bytes, whatever its files become once it has started, and leaves nothing of an apply it refused", () => {
    const tarball = releaseOf("1.0.0");
    manifestOf("1.0.0", tarball);
    expect(root("mkdir -p /opt/surogate/staging /opt/surogate/versions").status).toBe(0);
    const room = () => Number(root("df --output=avail -k /opt/surogate | tail -n 1").stdout);
    const free = room();
    // Refused, for *file* or with *said*, with nothing left in staging and the disk's room as it was.
    const nothing = (refused: ReturnType<typeof root>, file: string, said = `${file} is not a downloaded release's file`) => {
      expect(refused, file).toMatchObject({ status: 1, stdout: "", stderr: expect.stringMatching(new RegExp(`^Surogate Desktop: ${said}\n$`)) });
      expect(root("ls -A /opt/surogate/staging").stdout).toBe("");
      expect(free - room()).toBeLessThan(64);
    };
    // No more of a tarball is copied than the size its manifest names, and one byte, which shows it is another's.
    const other = `the downloaded release is not the ${statSync(tarball).size} bytes its manifest names`;
    // The tarball swapped for one of 10 GiB while the helper waited: the disk has no room for it.
    stage(tarball);
    nothing(held("rm /home/tester/release.tar.gz && truncate -s 10G /home/tester/release.tar.gz"), "/home/tester/release.tar.gz", other);
    // A file that holds more than its size says, as /proc's do.
    stage(tarball);
    nothing(root(`/opt/surogate-test/install.sh --apply ${files(undefined, "/proc/cpuinfo")}`), "/proc/cpuinfo", other);
    // Its folder swapped for a link to /dev: the name's last part, zero, is no link, and has no end.
    stage(tarball);
    expect(root("rm -rf /home/tester/dl /home/tester/dl.real && mkdir /home/tester/dl && cp /home/tester/release.tar.gz /home/tester/dl/zero && chown -R tester: /home/tester/dl").status).toBe(0);
    nothing(held("mv /home/tester/dl /home/tester/dl.real && ln -s /dev /home/tester/dl", undefined, "/home/tester/dl/zero"), "/home/tester/dl/zero", other);
    // A manifest and a signature have sizes of their own.
    expect(root("truncate -s 150M /home/tester/big.sig && truncate -s 1M /home/tester/big.json").status).toBe(0);
    nothing(root(`/opt/surogate-test/install.sh --apply /home/tester/manifest.json /home/tester/big.sig /home/tester/release.tar.gz`), "/home/tester/big.sig");
    nothing(root(`/opt/surogate-test/install.sh --apply ${files("/home/tester/big.json")}`), "/home/tester/big.json");
    // To the byte: one more than a signature's 64, and than the 4096 of a manifest.
    expect(root("head -c 65 /dev/zero >/home/tester/big.sig && head -c 4097 /dev/zero >/home/tester/big.json").status).toBe(0);
    nothing(root(`/opt/surogate-test/install.sh --apply /home/tester/manifest.json /home/tester/big.sig /home/tester/release.tar.gz`), "/home/tester/big.sig");
    nothing(root(`/opt/surogate-test/install.sh --apply ${files("/home/tester/big.json")}`), "/home/tester/big.json");
    // A file of 2^62 bytes is copied no further than the others, and counted without overflow.
    nothing(root(`truncate -s 4611686018427387904 /dev/shm/huge.tar.gz && /opt/surogate-test/install.sh --apply ${files(undefined, "/dev/shm/huge.tar.gz")}; said=$?; rm -f /dev/shm/huge.tar.gz; exit $said`),
      "/dev/shm/huge.tar.gz", other);
    // The honest release after them is applied.
    expect(apply(tarball)).toMatchObject({ status: 0, stdout: "Surogate Desktop: 1.0.0 is installed\n" });
    expect(root("ls -A /opt/surogate/staging").stdout).toBe("");
  });

  it("refuses a file whose read outlasts its bound, in a line of its own, and bounds each read by its kind: a tarball's, and the small ones", () => {
    const tarball = releaseOf("1.0.0");
    manifestOf("1.0.0", tarball);
    stage(tarball);
    // A helper that waits a moment too short for any read, for its tarball alone, and one that waits so for the others.
    for (const [wait, cut] of [["READ_WAIT=120", "release.tar.gz"], ["SMALL_WAIT=5", "manifest.json"]] as const) {
      const hasty = withKeys(readFileSync(SCRIPT, "utf8")).replace(wait, `${wait.split("=")[0]}=0.0001`);
      expect(hasty).toContain(`${wait.split("=")[0]}=0.0001`);
      writeFileSync(join(box.dir, "hasty.sh"), hasty, { mode: 0o755 });
      expect(docker(["cp", join(box.dir, "hasty.sh"), `${box.container}:/opt/surogate-test/hasty.sh`]).status).toBe(0);
      expect(root(`/opt/surogate-test/hasty.sh --apply ${files()}`), wait)
        .toMatchObject({ status: 1, stdout: "", stderr: `Surogate Desktop: /home/tester/${cut} is not a downloaded release's file\n` });
      expect(root("ls -A /opt/surogate/staging 2>/dev/null").stdout).toBe("");
    }
    // All that root has the asking user's processes do, in order, and for how many seconds each at
    // most: as that user alone, in their own group and no other.
    const user = Number(root("id -u tester").stdout);
    const traced = root(`PKEXEC_UID=${user} strace -f -qq -v -s 300 -o /tmp/trace -e trace=execve /opt/surogate-test/install.sh --apply ${files()} >/dev/null && grep -F 'execve("/usr/bin/timeout"' /tmp/trace`);
    expect(traced.status, traced.stderr).toBe(0);
    const asked = traced.stdout.trim().split("\n").map((line) => [...(/\[(.*)\], \[/.exec(line)?.[1] ?? "").matchAll(/"((?:[^"\\]|\\.)*)"/g)].map((match) => match[1]).join(" "));
    const as = `setpriv --reuid ${user} --regid ${user} --clear-groups`;
    const read = "iflag=nofollow,nonblock bs=64K status=none";
    expect(asked).toEqual([
      `timeout --foreground -s KILL 5 ${as} id -u`,
      `timeout --foreground -s KILL 5 ${as} id -g`,
      `timeout --foreground -s KILL 5 ${as} test -f /home/tester/manifest.json`,
      `timeout --foreground -s KILL 5 ${as} test -f /home/tester/manifest.json.sig`,
      `timeout --foreground -s KILL 5 ${as} test -f /home/tester/release.tar.gz`,
      `timeout --foreground -s KILL 5 ${as} dd if=/home/tester/manifest.json ${read}`,
      `timeout --foreground -s KILL 5 ${as} dd if=/home/tester/manifest.json.sig ${read}`,
      `timeout --foreground -s KILL 120 ${as} dd if=/home/tester/release.tar.gz ${read}`,
    ]);
  });

  it("clears what a killed apply left in staging before it measures the room", () => {
    const tarball = releaseOf("1.0.0");
    manifestOf("1.0.0", tarball);
    // As an apply killed while it copied leaves it, here with all the room the disk had.
    expect(root("mkdir -p /opt/surogate/staging/apply.killed && head -c 1G /dev/zero >/opt/surogate/staging/apply.killed/release.tar.gz; df --output=avail -k /opt/surogate | tail -n 1").stdout.trim()).toBe("0");
    expect(apply(tarball)).toMatchObject({ status: 0, stdout: "Surogate Desktop: 1.0.0 is installed\n" });
    expect(root("ls -A /opt/surogate/staging").stdout).toBe("");
  });

  it("runs the system's own tools, and no script that its caller's environment names", () => {
    const tarball = releaseOf("1.0.0");
    manifestOf("1.0.0", tarball, {}, other.privateKey);
    stage(tarball);
    // First on the caller's PATH, an openssl that calls every signature good; and a script bash reads as it starts.
    expect(root("mkdir -p /tmp/caller && printf '#!/bin/sh\\nexit 0\\n' >/tmp/caller/openssl && chmod 755 /tmp/caller/openssl && echo 'touch /tmp/caller/read' >/tmp/caller/env.sh").status).toBe(0);
    expect(root(`PATH=/tmp/caller:$PATH BASH_ENV=/tmp/caller/env.sh /opt/surogate-test/install.sh --apply ${files()}`))
      .toMatchObject({ status: 1, stdout: "", stderr: "Surogate Desktop: the release's manifest is not signed by Surogate's release key\n" });
    expect(root("test -e /tmp/caller/read").status).toBe(1);
    expect(root("test -e /opt/surogate/current").status).toBe(1);
  });

  it("applies a release signed by either key it lists, as a rotation needs, and refuses one signed by neither", () => {
    // A rotating release's script: the old key and the new.
    writeFileSync(join(box.dir, "rotating.sh"), withKeys(readFileSync(SCRIPT, "utf8"), [PUBLIC, pem(next.publicKey)]), { mode: 0o755 });
    expect(docker(["cp", join(box.dir, "rotating.sh"), `${box.container}:/opt/surogate-test/rotating.sh`]).status).toBe(0);
    for (const [version, key] of [["1.0.0", keys], ["1.1.0", next]] as const) {
      const tarball = releaseOf(version);
      manifestOf(version, tarball, {}, key.privateKey);
      expect(apply(tarball, "rotating.sh")).toMatchObject({ status: 0, stdout: `Surogate Desktop: ${version} is installed\n` });
    }
    const tarball = releaseOf("1.2.0");
    manifestOf("1.2.0", tarball, {}, other.privateKey);
    expect(apply(tarball, "rotating.sh")).toMatchObject({ status: 1, stderr: "Surogate Desktop: the release's manifest is not signed by Surogate's release key\n" });
    expect(current()).toBe("/opt/surogate/versions/1.1.0");
  });

  it("refuses an archive that holds anything outside its folder, a link out of it, a special file or a hard link", () => {
    const refusals: Array<[(top: string) => void, string]> = [
      [(top) => symlinkSync("/etc", join(top, "resources", "etc")), "the release's archive holds a link to a whole path: resources/etc"],
      [(top) => symlinkSync("../../../../../../tmp", join(top, "bin", "up")), "the release's archive links outside itself: bin/up"],
      [(top) => spawnSync("mkfifo", [join(top, "fifo")]), "the release's archive holds a special file, a set-id file or a hard link"],
      [(top) => spawnSync("ln", [join(top, "surogate"), join(top, "bin", "again")]), "the release's archive holds a special file, a set-id file or a hard link"],
      [(top) => writeFileSync(join(top, "..", "beside"), ""), "the release's archive holds more than surogate-desktop-1.0.0-linux-x64/"],
      [(top) => rmSync(join(top, "surogate")), "the release's archive is not Surogate Desktop"],
    ];
    for (const [change, refusal] of refusals) {
      const tarball = releaseOf("1.0.0", change);
      manifestOf("1.0.0", tarball);
      expect(apply(tarball)).toMatchObject({ status: 1, stderr: `Surogate Desktop: ${refusal}\n` });
    }
    // Bytes its manifest names that are no archive.
    const garbage = join(box.dir, "garbage.tar.gz");
    writeFileSync(garbage, randomBytes(4096));
    manifestOf("1.0.0", garbage);
    expect(apply(garbage)).toMatchObject({ status: 1, stderr: "Surogate Desktop: the release's archive could not be unpacked\n" });
    // A link inside it is a link like any other; a release.json of its own is replaced, never written through.
    const tarball = releaseOf("1.0.0", (top) => {
      symlinkSync("../surogate", join(top, "bin", "surogate"));
      symlinkSync("surogate", join(top, "release.json"));
    });
    manifestOf("1.0.0", tarball);
    expect(apply(tarball).status).toBe(0);
    expect(root("test ! -L /opt/surogate/current/release.json && cmp /home/tester/manifest.json /opt/surogate/current/release.json").status).toBe(0);
    expect(root("sha256sum </opt/surogate/current/surogate").stdout).toBe(`${sha256(readFileSync(SLEEP))}  -\n`);
    expect(versions()).toEqual(["1.0.0"]);
    expect(root("ls -A /opt/surogate/staging").stdout).toBe("");
  });

  it("refuses a link that would leave its version once the tree has its place, whatever it names where the tree is unpacked", () => {
    const outside = "the release's archive links outside itself";
    const wholePath = "the release's archive holds a link to a whole path";
    const refusals: Array<[(top: string) => void, string, string?]> = [
      // Out of the tree and back in by the folder's name in the archive, which is not its name in versions.
      [(top) => symlinkSync("../surogate-desktop-1.0.0-linux-x64/surogate", join(top, "back")), "back"],
      [(top) => symlinkSync("../../surogate-desktop-1.0.0-linux-x64/surogate", join(top, "resources", "back")), "resources/back"],
      // The same through a link of the tree's own, where the path as written never leaves the tree.
      [(top) => {
        symlinkSync("..", join(top, "resources", "app", "short"));
        symlinkSync("resources/app/short/../../surogate-desktop-1.0.0-linux-x64/surogate", join(top, "through"));
      }, "through"],
      // And the other way: a path that leaves the tree as it is written, and comes back to it as it
      // resolves, through a link of the tree's own to a folder two below its top.
      [(top) => {
        symlinkSync("resources/app", join(top, "deep"));
        symlinkSync("deep/../../surogate", join(top, "odd"));
      }, "odd"],
      // By the name it will have, and by its whole path.
      [(top) => symlinkSync("../1.0.0/surogate", join(top, "there")), "there"],
      [(top) => symlinkSync("/opt/surogate/versions/1.0.0/surogate", join(top, "whole")), "whole", wholePath],
      [(top) => symlinkSync("/opt/surogate/current/surogate", join(top, "whole")), "whole", wholePath],
    ];
    // The installed version's own files are there to be named.
    const installed = releaseOf("1.0.0");
    manifestOf("1.0.0", installed);
    expect(apply(installed).status).toBe(0);
    for (const [change, link, said = outside] of refusals) {
      const tarball = releaseOf("1.0.0", change);
      manifestOf("1.0.0", tarball);
      expect(apply(tarball), link).toMatchObject({ status: 1, stdout: "", stderr: `Surogate Desktop: ${said}: ${link}\n` });
    }
    // Links that stay in the tree are links like any other: to a folder above them, and to nothing.
    const tarball = releaseOf("1.0.0", (top) => {
      symlinkSync("..", join(top, "resources", "app", "short"));
      symlinkSync("app/short/app/package.json", join(top, "resources", "round"));
      symlinkSync("nothing/there", join(top, "resources", "dangling"));
    });
    manifestOf("1.0.0", tarball);
    expect(apply(tarball)).toMatchObject({ status: 0, stderr: "" });
    expect(root("cat /opt/surogate/current/resources/round").stdout).toBe('{"version":"1.0.0"}');
  });

  it("refuses an archive that lists a set-id file or folder, which tar unpacks without the bit", () => {
    for (const [member, mode] of [["surogate", 0o4755], ["bin/surogate-apply-update", 0o2755], ["resources", 0o2755]] as const) {
      const tarball = releaseOf("1.0.0", (top) => {
        chmodSync(join(top, member), mode);
        expect(statSync(join(top, member)).mode & 0o7777).toBe(mode);
      });
      manifestOf("1.0.0", tarball);
      expect(apply(tarball), member).toMatchObject({ status: 1, stdout: "", stderr: "Surogate Desktop: the release's archive holds a special file, a set-id file or a hard link\n" });
    }
    expect(root("test -e /opt/surogate/current").status).toBe(1);
    expect(root("ls -A /opt/surogate/staging").stdout).toBe("");
  });

  it("takes only a tree whose app and helper are programs, the helper in a bin folder of its own, and gives it its mark, its bwrap and its mode itself", () => {
    const refusals: Array<(top: string) => void> = [
      (top) => { rmSync(join(top, "surogate")); mkdirSync(join(top, "surogate")); },
      (top) => { rmSync(join(top, "bin", "surogate-apply-update")); mkdirSync(join(top, "bin", "surogate-apply-update")); },
      (top) => { renameSync(join(top, "bin"), join(top, "tools")); symlinkSync("tools", join(top, "bin")); },
      // Each a link to a program of the tree's own.
      (top) => { renameSync(join(top, "surogate"), join(top, "resources", "app", "surogate")); symlinkSync("resources/app/surogate", join(top, "surogate")); },
      (top) => { renameSync(join(top, "bin", "surogate-apply-update"), join(top, "resources", "helper")); symlinkSync("../resources/helper", join(top, "bin", "surogate-apply-update")); },
    ];
    for (const [index, change] of refusals.entries()) {
      const tarball = releaseOf("1.0.0", change);
      manifestOf("1.0.0", tarball);
      expect(apply(tarball), `refusal ${index}`).toMatchObject({ status: 1, stdout: "", stderr: "Surogate Desktop: the release's archive is not Surogate Desktop\n" });
    }
    // A release.json and a bin/bwrap of the archive's own, here folders, and a top folder only its owner opens.
    const tarball = releaseOf("1.0.0", (top) => {
      mkdirSync(join(top, "release.json"));
      writeFileSync(join(top, "release.json", "inside"), "");
      mkdirSync(join(top, "bin", "bwrap"));
      chmodSync(top, 0o700);
    });
    manifestOf("1.0.0", tarball);
    expect(apply(tarball)).toMatchObject({ status: 0, stdout: "Surogate Desktop: 1.0.0 is installed\n", stderr: "" });
    expect(root("stat -c %a /opt/surogate/versions/1.0.0").stdout).toBe("755\n");
    expect(root("test -f /opt/surogate/current/release.json && cmp /home/tester/manifest.json /opt/surogate/current/release.json && cmp /usr/bin/bwrap /opt/surogate/current/bin/bwrap").status).toBe(0);
  });

  it("keeps the version before it, and removes older ones once nothing runs from them", () => {
    for (const version of ["1.0.0", "1.1.0"]) {
      const tarball = releaseOf(version);
      manifestOf(version, tarball);
      expect(apply(tarball).status).toBe(0);
    }
    // Another user's app still runs 1.0.0.
    expect(docker(["exec", "-d", "-u", "tester", box.container, "/opt/surogate/versions/1.0.0/surogate", "600"]).status).toBe(0);
    for (const version of ["1.2.0", "1.3.0"]) {
      const tarball = releaseOf(version);
      manifestOf(version, tarball);
      expect(apply(tarball).status).toBe(0);
    }
    expect(versions()).toEqual(["1.0.0", "1.2.0", "1.3.0"]);
    root("pkill -f '^/opt/surogate/versions/1.0.0/surogate'");
    const tarball = releaseOf("1.4.0");
    manifestOf("1.4.0", tarball);
    expect(apply(tarball).status).toBe(0);
    expect(versions()).toEqual(["1.3.0", "1.4.0"]);
  });

  it("refuses a version older than the installed one, and applies the installed one again as a repair", () => {
    const newer = releaseOf("1.1.0");
    manifestOf("1.1.0", newer);
    expect(apply(newer).status).toBe(0);
    const older = releaseOf("1.0.0");
    manifestOf("1.0.0", older);
    expect(apply(older)).toMatchObject({ status: 1, stderr: "Surogate Desktop: 1.0.0 is older than the installed 1.1.0\n" });
    // The repair takes its bwrap again, and leaves the version's files as they are.
    expect(root("echo stale > /opt/surogate/versions/1.1.0/bin/bwrap").status).toBe(0);
    const inode = root("ls -i /opt/surogate/versions/1.1.0/surogate").stdout;
    manifestOf("1.1.0", newer);
    expect(apply(newer).status).toBe(0);
    expect(root("cmp /usr/bin/bwrap /opt/surogate/versions/1.1.0/bin/bwrap").status).toBe(0);
    expect(root("ls -i /opt/surogate/versions/1.1.0/surogate").stdout).toBe(inode);
    expect(current()).toBe("/opt/surogate/versions/1.1.0");
  });

  it("keeps the version before the installed one, when the installed one is applied again", () => {
    const first = releaseOf("1.0.0");
    manifestOf("1.0.0", first);
    expect(apply(first).status).toBe(0);
    const second = releaseOf("1.1.0");
    manifestOf("1.1.0", second);
    expect(apply(second).status).toBe(0);
    expect(apply(second)).toMatchObject({ status: 0, stdout: "Surogate Desktop: 1.1.0 is installed\n" });
    expect(versions()).toEqual(["1.0.0", "1.1.0"]);
  });

  it("leaves current naming a whole version wherever an update stops, and the same apply, run again, keeps the version before it", () => {
    const first = releaseOf("1.0.0");
    manifestOf("1.0.0", first);
    expect(apply(first).status).toBe(0);
    const second = releaseOf("1.1.0");
    manifestOf("1.1.0", second);
    stage(second);
    const seen = new Set<string>();
    for (const stop of stops()) {
      const [, stopped] = /^\d+: (1\.[01]\.0 whole); again 0: 1\.1\.0 whole, versions 1\.0\.0 1\.1\.0 , staging 0$/.exec(stop) ?? [];
      expect(stopped, stop).toBeDefined();
      seen.add(stopped!);
    }
    expect(seen).toEqual(new Set(["1.0.0 whole", "1.1.0 whole"]));
  }, 300_000);

  it("unpacks the installed version again when its folder has lost a program, and takes an older version out of versions by one rename", () => {
    for (const version of ["1.0.0", "1.1.0"]) {
      const tarball = releaseOf(version);
      manifestOf(version, tarball);
      expect(apply(tarball).status).toBe(0);
      // Its mark is there, and a program is not: the folder is not whole.
      for (const program of ["surogate", "bin/surogate-apply-update"]) {
        expect(root(`rm /opt/surogate/versions/${version}/${program}`).status).toBe(0);
        expect(apply(tarball), program).toMatchObject({ status: 0, stdout: `Surogate Desktop: ${version} is installed\n`, stderr: "" });
        expect(root(`test -x /opt/surogate/versions/${version}/${program}`).status).toBe(0);
        // Nor when the program is a link, here to one that runs: it is no file of the folder's own.
        expect(root(`ln -sf /usr/bin/true /opt/surogate/versions/${version}/${program}`).status).toBe(0);
        expect(apply(tarball), program).toMatchObject({ status: 0, stdout: `Surogate Desktop: ${version} is installed\n`, stderr: "" });
        expect(root(`test -x /opt/surogate/versions/${version}/${program} && test ! -L /opt/surogate/versions/${version}/${program}`).status, program).toBe(0);
      }
    }
    // An update removes the version before the last: it leaves versions whole, by a rename into staging.
    const tarball = releaseOf("1.2.0");
    manifestOf("1.2.0", tarball);
    stage(tarball);
    const at = traced();
    expect(at(/^\d+ +rename\w*\(.*"\/opt\/surogate\/versions\/1\.0\.0", .*"\/opt\/surogate\/staging\/apply\.\w+\/removed\.1\.0\.0".*\) += 0$/)).toBeGreaterThan(-1);
    expect(versions()).toEqual(["1.1.0", "1.2.0"]);
    expect(root("ls -A /opt/surogate/staging").stdout).toBe("");
  });

  it("has a version's files on the disk before the version has its name, and before current names it", () => {
    const first = releaseOf("1.0.0");
    manifestOf("1.0.0", first);
    expect(apply(first).status).toBe(0);
    const second = releaseOf("1.1.0");
    manifestOf("1.1.0", second);
    stage(second);
    const flushed = /^\d+ +syncfs\(\d+\) += 0$/;
    const switched = /rename\w*\(.*"\/opt\/surogate\/staging\/apply\.\w+\/current", .*"\/opt\/surogate\/current".*\) += 0$/;
    // An update: its tree is flushed where it was unpacked, in staging.
    const update = traced();
    const named = update(/rename\w*\(.*"\/opt\/surogate\/staging\/apply\.\w+\/tree[^"]*", .*"\/opt\/surogate\/versions\/1\.1\.0".*\) += 0$/);
    expect(update(flushed)).toBeGreaterThan(-1);
    expect(named).toBeGreaterThan(update(flushed));
    expect(update(switched)).toBeGreaterThan(named);
    // A repair: its bwrap is flushed before it has its name in the version's folder.
    const repair = traced();
    const placed = repair(/rename\w*\(.*"\/opt\/surogate\/staging\/apply\.\w+\/bwrap", .*"\/opt\/surogate\/versions\/1\.1\.0\/bin\/bwrap".*\) += 0$/);
    expect(repair(flushed)).toBeGreaterThan(-1);
    expect(placed).toBeGreaterThan(repair(flushed));
    expect(repair(switched)).toBeGreaterThan(placed);
  });

  it("has the installed version whole again by one rename, when a release of it built again replaces its folder", () => {
    for (const version of ["1.0.0", "1.1.0"]) {
      const tarball = releaseOf(version);
      manifestOf(version, tarball);
      expect(apply(tarball).status).toBe(0);
    }
    const rebuilt = releaseOf("1.1.0", (top) => writeFileSync(join(top, "resources", "app", "rebuilt"), ""));
    manifestOf("1.1.0", rebuilt);
    stage(rebuilt);
    const gone: string[] = [];
    for (const stop of stops()) {
      const [, stopped] = /^\d+: 1\.1\.0 (whole|gone); again 0: 1\.1\.0 whole, versions 1\.0\.0 1\.1\.0 , staging 0$/.exec(stop) ?? [];
      expect(stopped, stop).toBeDefined();
      if (stopped === "gone") gone.push(stop);
    }
    // Between the old folder's rename out of its name and the new one's into it, and at no other stop.
    expect(gone).toHaveLength(1);
    expect(root("test -e /opt/surogate/current/resources/app/rebuilt").status).toBe(0);
  }, 300_000);

  it("leaves current naming a whole version, whenever an update is cut short", () => {
    const first = releaseOf("1.0.0");
    manifestOf("1.0.0", first);
    expect(apply(first).status).toBe(0);
    // Large enough that its copy and unpacking take most of a second.
    const second = releaseOf("1.1.0", (top) => writeFileSync(join(top, "resources", "app", "large"), randomBytes(48 * 1024 * 1024)));
    manifestOf("1.1.0", second);
    stage(second);
    // Killed at 20 points along its way, spread over the time it takes here and half as much
    // again; each time current is whole, and the next apply finishes.
    const took = Number(root(`start=$(date +%s%N); /opt/surogate-test/install.sh --apply ${files()} >/dev/null && echo $(( ($(date +%s%N) - start) / 1000000 ))`).stdout);
    expect(took).toBeGreaterThan(0);
    const seen = new Set<string>();
    for (let cut = 1; cut <= 20; cut++) {
      const ms = Math.round((took * 1.5 * cut) / 20);
      root(`rm -rf /opt/surogate/versions/1.1.0 && ln -sfn /opt/surogate/versions/1.0.0 /opt/surogate/current && timeout -s KILL ${ms / 1000} /opt/surogate-test/install.sh --apply ${files()}`);
      const now = current();
      seen.add(now);
      expect(root(`test -f ${now}/release.json && test -x ${now}/surogate && test -x ${now}/bin/bwrap`).status, `${ms} ms: ${now}`).toBe(0);
    }
    expect(seen).toEqual(new Set(["/opt/surogate/versions/1.0.0", "/opt/surogate/versions/1.1.0"]));
    expect(apply(second).status).toBe(0);
    expect(current()).toBe("/opt/surogate/versions/1.1.0");
    expect(root("ls -A /opt/surogate/staging").stdout).toBe("");
  });

  it("says in a line of its own what it did not expect: a step of its own that fails, a disk that is full, and room short of a megabyte", () => {
    const tarball = releaseOf("1.0.0");
    manifestOf("1.0.0", tarball);
    stage(tarball);
    // A step of its own that fails, here because its bin folder's name is a file's: the step's words, then the helper's line.
    expect(root("touch /opt/surogate/bin").status).toBe(0);
    const failed = root(`/opt/surogate-test/install.sh --apply ${files()}`);
    expect(failed).toMatchObject({ status: 1, stdout: "" });
    expect(failed.stderr.trimEnd().split("\n").at(-1)).toBe('Surogate Desktop: stopped, as this step failed: mkdir -p "$ROOT/versions" "$ROOT/bin"');
    expect(root("rm /opt/surogate/bin").status).toBe(0);
    // A disk with no room at all: the copy's own failure is not the file's.
    expect(root(`head -c 1G /dev/zero >/opt/surogate/filler 2>/dev/null; /opt/surogate-test/install.sh --apply ${files()}`))
      .toMatchObject({ status: 1, stdout: "", stderr: "Surogate Desktop: /opt/surogate/staging could not be written: is its disk full?\n" });
    // Room for the manifest and its signature, and not for the release: the megabytes it needs are no 0.
    expect(root(`truncate -s -32K /opt/surogate/filler && /opt/surogate-test/install.sh --apply ${files()}`))
      .toMatchObject({ status: 1, stdout: "", stderr: "Surogate Desktop: /opt/surogate needs 1 MB free to apply this release, and has 0 MB\n" });
    expect(root("rm /opt/surogate/filler && ls -A /opt/surogate/staging").stdout).toBe("");
    // A link that leads round to itself: GNU's realpath lets it be, uutils' refuses it, and the words of neither are said.
    const looped = releaseOf("1.0.0", (top) => {
      symlinkSync("round", join(top, "resources", "loop"));
      symlinkSync("loop", join(top, "resources", "round"));
    });
    manifestOf("1.0.0", looped);
    const loop = apply(looped);
    expect(`${loop.status} ${loop.stderr}`).toMatch(/^(0 |1 Surogate Desktop: the release's archive links outside itself: resources\/(loop|round)\n)$/);
  });

  it("says a file's or a link's name as one word of one line, whatever the name holds", () => {
    const tarball = releaseOf("1.0.0");
    manifestOf("1.0.0", tarball);
    stage(tarball);
    // A name that would read as a line of the helper's own, and colour what follows it.
    const line = "\nSurogate Desktop: 9.9.9 is installed\u001b[31m";
    const quoted = "\\nSurogate Desktop: 9.9.9 is installed\\E[31m";
    for (const [index, name] of ["manifest.json", "manifest.json.sig", "release.tar.gz"].entries()) {
      const names = ["/home/tester/manifest.json", "/home/tester/manifest.json.sig", "/home/tester/release.tar.gz"];
      names[index] = `/home/tester/no ${name}${line}`;
      expect(docker(["exec", box.container, "/opt/surogate-test/install.sh", "--apply", ...names]), name)
        .toMatchObject({ status: 1, stdout: "", stderr: `Surogate Desktop: $'/home/tester/no ${name}${quoted}' is not a downloaded release's file\n` });
    }
    for (const [target, said] of [["/etc", "holds a link to a whole path"], ["../../../etc", "links outside itself"]] as const) {
      const linked = releaseOf("1.0.0", (top) => symlinkSync(target, join(top, "resources", `out${line}`)));
      manifestOf("1.0.0", linked);
      expect(apply(linked), target).toMatchObject({ status: 1, stdout: "", stderr: `Surogate Desktop: the release's archive ${said}: $'resources/out${quoted}'\n` });
    }
  });

  it("reads a name and a user's number in no locale of its caller's, which pkexec and sudo pass on", () => {
    const tarball = releaseOf("1.0.0");
    manifestOf("1.0.0", tarball);
    stage(tarball);
    const inLocale = (locale: string, env: string[], run: string[]) => docker(["exec", "-e", `LC_ALL=${locale}`, ...env.flatMap((pair) => ["-e", pair]), box.container, ...run]);
    // A name's letters outside ASCII are said by their bytes: among them is the mark that turns the line's direction.
    for (const locale of ["C.UTF-8", "en_US.UTF-8"]) {
      expect(inLocale(locale, [], ["/opt/surogate-test/install.sh", "--apply", "/home/tester/n\u00e9\u202e.json", "/home/tester/manifest.json.sig", "/home/tester/release.tar.gz"]), locale)
        .toMatchObject({ status: 1, stdout: "", stderr: "Surogate Desktop: $'/home/tester/n\\303\\251\\342\\200\\256.json' is not a downloaded release's file\n" });
    }
    // Where the caller's locale has more than ten digits, none but the ten is a user's number.
    for (const digits of ["\u00b2", "\u0661\u0660\u0660\u0661", "\uff11\uff10\uff10\uff11", "1\u00b2"]) {
      expect(inLocale("en_US.UTF-8", [`PKEXEC_UID=${digits}`], ["/opt/surogate-test/install.sh", "--apply", ...files().split(" ")]), digits)
        .toMatchObject({ status: 1, stdout: "", stderr: "Surogate Desktop: PKEXEC_UID is not a user's number\n" });
    }
    // Nor would what is no number read as root's, in such a locale: the search for the user by
    // itself, from the script's functions without its last line, which runs it.
    const alone = inLocale("en_US.UTF-8", ["PKEXEC_UID=\u00b2"], ["bash", "-c", `. <(sed '$d' /opt/surogate-test/install.sh) && settings && asker; echo "reads as \${READER[*]}"`]);
    expect(alone).toMatchObject({ status: 1, stdout: "", stderr: "Surogate Desktop: PKEXEC_UID names no user of this computer\n" });
    expect(root("test -e /opt/surogate/current").status).toBe(1);
  });

  // The script that publishes a release, beside the install script. Its other tests are
  // release-publish.test.ts's: this one needs a locale in which more than ten characters are
  // digits, as this computer's en_US.UTF-8 is.
  it("takes none but the ten digits for a version's, in whatever locale a release is published", () => {
    expect(docker(["cp", PUBLISH, `${box.container}:/opt/surogate-test/publish.sh`]).status).toBe(0);
    for (const version of ["1.0.\u00b2", "1.\u0661.0", "\uff11.0.0"]) for (const verb of ["sign", "send"]) {
      expect(docker(["exec", "-e", "LC_ALL=en_US.UTF-8", "-e", "DESKTOP_RELEASE_KEY=none", box.container, "bash", "/opt/surogate-test/publish.sh", verb, version, "/tmp"]), `${verb} ${version}`)
        .toMatchObject({ status: 2, stdout: "", stderr: "usage: publish.sh sign|send <x.y.z> <out>\n" });
    }
  });

  it("leaves nothing in staging when a signal stops it: a terminal closed, Ctrl+C, a kill", () => {
    const first = releaseOf("1.0.0");
    manifestOf("1.0.0", first);
    expect(apply(first).status).toBe(0);
    // Large enough that its copy and unpacking take a second.
    const second = releaseOf("1.1.0", (top) => writeFileSync(join(top, "resources", "app", "large"), randomBytes(48 * 1024 * 1024)));
    manifestOf("1.1.0", second);
    stage(second);
    // Each signal at five points along the time an apply takes here: fixed times would all come
    // after its end on a faster computer, and nothing would be stopped.
    const took = Number(root(`start=$(date +%s%N); /opt/surogate-test/install.sh --apply ${files()} >/dev/null && echo $(( ($(date +%s%N) - start) / 1000000 ))`).stdout);
    expect(took).toBeGreaterThan(0);
    const stops = new Set<string>();
    for (const signal of ["HUP", "INT", "TERM"]) for (const part of [0.15, 0.3, 0.45, 0.6, 0.75]) {
      const ms = Math.max(1, Math.round(took * part));
      root("rm -rf /opt/surogate/versions/1.1.0 && ln -sfn /opt/surogate/versions/1.0.0 /opt/surogate/current");
      // Stopped (124), or done before the signal came (0): staging is empty as the helper ends.
      const stopped = root(`timeout -s ${signal} ${ms / 1000} /opt/surogate-test/install.sh --apply ${files()} >/dev/null 2>&1; echo "$? $(ls -A /opt/surogate/staging | wc -l)"`);
      expect(stopped.stdout.trim(), `${signal} at ${ms} ms`).toMatch(/^(124|0) 0$/);
      if (stopped.stdout.startsWith("124 ")) stops.add(signal);
    }
    // Each of the three did come while an apply ran.
    expect([...stops].sort()).toEqual(["HUP", "INT", "TERM"]);
    expect(apply(second)).toMatchObject({ status: 0, stdout: "Surogate Desktop: 1.1.0 is installed\n" });
  });

  it("leaves nothing in staging and current naming a whole version wherever one signal stops it, and nothing when a second comes as it clears up, as Ctrl+C pressed twice sends", () => {
    for (const version of ["1.0.0", "1.1.0"]) {
      const tarball = releaseOf(version);
      manifestOf(version, tarball);
      expect(apply(tarball).status).toBe(0);
    }
    // The installed version built again: its apply makes a folder of its own, unpacks, and replaces a version's folder.
    const rebuilt = releaseOf("1.1.0", (top) => writeFileSync(join(top, "resources", "app", "rebuilt"), ""));
    manifestOf("1.1.0", rebuilt);
    stage(rebuilt);
    const lines = stops(files(), SIGNALS);
    expect(lines.length).toBeGreaterThan(100);
    for (const line of lines) expect(line).toMatch(/^\d+: (143|0) 1\.1\.0 whole, staging 0$/);
    // Stopped (143) up to the making of its own folder; let finish (0) from there until the folder
    // is listed as its own, where a signal would leave it made and unlisted; stopped again up to
    // its first rename; let finish from there to its last, where a signal would leave current
    // naming no folder; and stopped again after it, until it clears up.
    expect(lines.map((line) => line.split(" ")[1]).join(" ")).toMatch(/^(143 )+(0 )+(143 )+(0 )+(143 )+(0 ?)+$/);
    expect(root("test -e /opt/surogate/current/resources/app/rebuilt").status).toBe(0);
    // An apply it refuses ends with its own folder still to clear: a signal that comes as it starts to, as wherever else.
    manifestOf("1.1.0", rebuilt, {}, other.privateKey);
    stage(rebuilt);
    const refused = stops(files(), SIGNALS);
    expect(refused.length).toBeGreaterThan(50);
    for (const line of refused) expect(line).toMatch(/^\d+: (143|1) 1\.1\.0 whole, staging 0$/);
    // A second signal, to all of the helper's processes, as it removes what it staged.
    writeFileSync(join(box.dir, "signalling"), SIGNALLING, { mode: 0o755 });
    expect(docker(["cp", join(box.dir, "signalling"), `${box.container}:/opt/surogate-test/signalling`]).status).toBe(0);
    const twice = root("mkdir -p /opt/hold && cp -L /usr/bin/rm /opt/hold/rm && mv /usr/bin/rm /usr/bin/rm.away && cp /opt/surogate-test/signalling /usr/bin/rm"
      + `; setsid -w /opt/surogate-test/install.sh --apply ${files()}; said=$?; mv -f /usr/bin/rm.away /usr/bin/rm; echo "$said $(ls -A /opt/surogate/staging | wc -l)"`);
    expect(twice).toMatchObject({ stdout: "1 0\n", stderr: "Surogate Desktop: the release's manifest is not signed by Surogate's release key\n" });
  }, 300_000);

  it("leaves no folder of its own when a signal comes as the folder is made, before the script has its name: the signal is let by, and the apply goes on", () => {
    const tarball = releaseOf("1.0.0");
    manifestOf("1.0.0", tarball);
    stage(tarball);
    for (const signal of ["HUP", "INT", "PIPE", "TERM"]) {
      expect(root("find /opt/surogate -mindepth 1 -delete").status).toBe(0);
      // In a session of its own: the signal goes to all of the helper's processes, as a terminal's does.
      const signalled = swapped("mktemp", interrupting(signal), [
        ": >/tmp/made",
        `setsid -w /opt/surogate-test/install.sh --apply ${files()} >/tmp/said 2>&1; said=$?`,
        'echo "$said: $(wc -l </tmp/made) made, $(for made in $(cat /tmp/made); do [ ! -e "$made" ] || echo "$made"; done | wc -l) left, staging $(ls -A /opt/surogate/staging | wc -l); $(cat /tmp/said)"',
      ]);
      expect(signalled.stdout, signal).toBe("0: 1 made, 0 left, staging 0; Surogate Desktop: 1.0.0 is installed\n");
    }
  });

  it("says what stops it: its arguments, a user who is not root, too little room, and bubblewrap missing", () => {
    const usage = "Surogate Desktop: usage: surogate-apply-update --apply <manifest> <signature> <tarball>\n";
    expect(root("/opt/surogate-test/install.sh --apply /home/tester/manifest.json")).toMatchObject({ status: 1, stderr: usage });
    expect(root("/opt/surogate-test/install.sh --apply /home/tester/manifest.json /home/tester/manifest.json.sig ''")).toMatchObject({ status: 1, stderr: usage });
    expect(docker(["exec", "-u", "tester", box.container, "/opt/surogate-test/install.sh", "--apply", ...files().split(" ")]))
      .toMatchObject({ status: 1, stderr: "Surogate Desktop: applying a release needs administrator rights\n" });
    const tarball = releaseOf("1.0.0");
    manifestOf("1.0.0", tarball);
    stage(tarball);
    // Room for its copy and its tree, four times the size its manifest names: none has that for 10 TiB.
    manifestOf("1.0.0", tarball, { size: 10 * 2 ** 40 });
    stage(tarball);
    expect(root(`/opt/surogate-test/install.sh --apply ${files()}`))
      .toMatchObject({ status: 1, stderr: expect.stringMatching(/^Surogate Desktop: \/opt\/surogate needs 41943040 MB free to apply this release, and has \d+ MB\n$/) });
    manifestOf("1.0.0", tarball);
    stage(tarball);
    const missing = root(`mv /usr/bin/bwrap /usr/bin/bwrap.away; /opt/surogate-test/install.sh --apply ${files()}; said=$?; mv /usr/bin/bwrap.away /usr/bin/bwrap; exit $said`);
    expect(missing).toMatchObject({ status: 1, stderr: "Surogate Desktop: bubblewrap is missing: run Surogate Desktop's install script again\n" });
    expect(root("test -e /opt/surogate/current").status).toBe(1);
    // Run again with it there, the apply finishes.
    expect(apply(tarball)).toMatchObject({ status: 0, stdout: "Surogate Desktop: 1.0.0 is installed\n" });
  });
});

for (const release of RELEASES) describe.skipIf(!ENABLED)(`the install script, on Ubuntu ${release}`, { timeout: 600_000 }, () => {
  // A desktop's baseline: sudo for its administrator, curl, AppArmor's parser, polkit, the system
  // bus and the kvm group; and another user of the computer. The container cannot load a profile
  // into the kernel, so its apparmor_parser parses one as the release's own parser reads it, and
  // stops there. With them, Ubuntu's German language pack, in which the system's tools say what
  // they say in German to whoever's desktop is; and strace, for the test that reads what the
  // script starts.
  const { it: box, docker, root, as, releaseOf, manifestOf, current, versions, swapped } = lab(release, [
    "RUN apt-get update && apt-get install -y --no-install-recommends sudo curl ca-certificates apparmor polkitd pkexec dbus",
    "RUN groupadd --system kvm && useradd -m -s /bin/bash -G sudo tester && useradd -m -s /bin/bash other && echo 'tester ALL=(ALL) NOPASSWD:ALL' >/etc/sudoers.d/tester",
    `RUN echo '#!/bin/sh' >/usr/local/sbin/apparmor_parser && echo 'said=$(/usr/sbin/apparmor_parser --skip-kernel-load "$@" 2>&1) || { echo "$said" >&2; exit 1; }' >>/usr/local/sbin/apparmor_parser && chmod 755 /usr/local/sbin/apparmor_parser`,
    "RUN apt-get update && apt-get install -y --no-install-recommends language-pack-de strace",
  ], ["--network", "host"]);
  let server: ChildProcess;
  let base: string;
  // What the base serves: desktop/install.sh, latest.json and its signature, and each release.
  const www = () => join(box.dir, "www");
  const publish = (version: string, key?: KeyObject) => {
    const tarball = releaseOf(version);
    const manifest = manifestOf(version, tarball, {}, key);
    const folder = join(www(), "desktop", "releases", version);
    mkdirSync(folder, { recursive: true });
    copyFileSync(tarball, join(folder, `surogate-desktop-${version}-linux-x64.tar.gz`));
    writeFileSync(join(www(), "desktop", "latest.json"), manifest);
    copyFileSync(join(box.dir, "manifest.json.sig"), join(www(), "desktop", "latest.json.sig"));
  };
  const install = (env = "") => as("tester", `curl -fsSL ${base}/desktop/install.sh | ${env} bash -s -- --base ${base}`);
  const uninstall = (env = "") => as("tester", `curl -fsSL ${base}/desktop/install.sh | ${env} bash -s -- --uninstall`);

  beforeAll(async () => {
    mkdirSync(join(www(), "desktop"), { recursive: true });
    copyFileSync(join(box.dir, "install.sh"), join(www(), "desktop", "install.sh"));
    // A process of its own: every docker call here blocks this one's event loop.
    server = spawn(process.execPath, ["-e", SERVE, www()], { stdio: ["ignore", "pipe", "inherit"] });
    const port = await new Promise<string>((resolve) => server.stdout!.once("data", (chunk: Buffer) => resolve(chunk.toString().trim())));
    base = `http://127.0.0.1:${port}`;
  });

  afterAll(() => {
    server.kill();
  });

  it("refuses a computer it does not support before it changes anything, or asks for sudo, and a base that is no http or https URL", () => {
    for (const args of ["--base", "--base ftp://elsewhere.example", `--base ${base} again`]) {
      expect(as("tester", `curl -fsSL ${base}/desktop/install.sh | bash -s -- ${args}`), args)
        .toMatchObject({ status: 1, stdout: "", stderr: "Surogate Desktop: usage: install.sh --base <http or https URL>\n" });
    }
    expect(root("cp /etc/os-release /root/os-release").status).toBe(0);
    const others = [
      'ID=ubuntu\nVERSION_ID="25.10"\nVERSION="25.10 (Questing Quokka)"',
      'ID=ubuntu\nVERSION_ID="22.04"\nVERSION="22.04.5 LTS (Jammy Jellyfish)"',
      'ID=debian\nVERSION_ID="12"\nVERSION="12 (bookworm)"',
    ];
    const unsupported = "Surogate Desktop: Surogate Desktop supports Ubuntu 24.04 LTS or a later LTS release (x64)\n";
    for (const osRelease of others) {
      expect(root(`printf '%s\\n' '${osRelease}' >/etc/os-release`).status).toBe(0);
      expect(install()).toMatchObject({ status: 1, stdout: "", stderr: unsupported });
    }
    // What is a base is read byte for byte, the same in every locale of its caller's: white space
    // is ASCII's six characters and no other, and bytes that are no letters are bytes. On this
    // computer, which it does not support, a base it takes gets as far as that refusal.
    for (const locale of ["C", "C.UTF-8", "de_DE.UTF-8"]) {
      const based = (url: string) => as("tester", `curl -fsSL ${base}/desktop/install.sh | LC_ALL=${locale} bash -s -- --base ${url}`);
      for (const url of ["$'http://b\\303\\274cher.example'", "$'http://surogate.example/\\343\\200\\200'", "$'http://surogate.example/\\377\\376'"]) {
        expect(based(url), `${locale} ${url}`).toMatchObject({ status: 1, stdout: "", stderr: unsupported });
      }
      for (const url of ["'http://surogate.example/a b'", "$'http://surogate.example/a\\tb'", "$'http://surogate.example\\n'"]) {
        expect(based(url), `${locale} ${url}`).toMatchObject({ status: 1, stdout: "", stderr: "Surogate Desktop: usage: install.sh --base <http or https URL>\n" });
      }
    }
    expect(root("cp /root/os-release /etc/os-release && test ! -e /opt/surogate && test ! -e /etc/surogate").status).toBe(0);
  });

  it("refuses an /opt/surogate that is a link before it changes anything, or asks for sudo, whatever the link names", () => {
    // A link to another user's folder, and one to nothing.
    for (const target of ["/home/other/elsewhere", "/nowhere"]) {
      expect(root(`rm -rf /home/other/elsewhere && mkdir -m 755 /home/other/elsewhere && chown other: /home/other/elsewhere && ln -sfn ${target} /opt/surogate`).status, target).toBe(0);
      // As a user, and as root's part by itself, as under sudo bash install.sh.
      expect(install(), target).toMatchObject({ status: 1, stdout: "", stderr: linked() });
      expect(root(`/opt/surogate-test/install.sh --base ${base}`), target).toMatchObject({ status: 1, stdout: "", stderr: linked() });
      expect(uninstall(), target).toMatchObject({ status: 1, stdout: "", stderr: LINKED_NOTHING_REMOVED });
      expect(root("/opt/surogate-test/install.sh --uninstall"), target).toMatchObject({ status: 1, stdout: "", stderr: LINKED_NOTHING_REMOVED });
      // Nothing is behind the link, which is whose it was; and the link is there still.
      expect(root("ls -A /home/other/elsewhere; stat -c '%U %a' /home/other/elsewhere; readlink /opt/surogate").stdout, target).toBe(`other 755\n${target}\n`);
    }
    expect(root("rm /opt/surogate && test ! -e /etc/surogate && test ! -e /usr/local/bin/surogate").status).toBe(0);
  });

  it("installs from its base, as a user who has sudo, all that the app needs, for every user of the computer", () => {
    publish("1.0.0");
    // A package source of the computer's own that apt cannot update from: the install goes on.
    expect(root(`echo 'deb ${base}/nowhere noble main' >/etc/apt/sources.list.d/broken.list`).status).toBe(0);
    const installed = install();
    expect(root("rm /etc/apt/sources.list.d/broken.list").status).toBe(0);
    expect(installed.status, installed.stderr).toBe(0);
    expect(installed.stdout).toContain("Surogate Desktop: installing needs administrator rights: sudo asks for your password once\n");
    expect(installed.stdout).toContain("Surogate Desktop: apt-get update failed for a package source of this computer's: installing from what apt knows already\n");
    expect(installed.stdout).toContain("Surogate Desktop: 1.0.0 is installed\n");
    expect(installed.stdout).toContain("Surogate Desktop: no supported browser is installed");
    // A container has no /dev/kvm.
    expect(installed.stdout).toContain("Surogate Desktop: This computer has no hardware virtualization (VT-x or AMD-V)");
    expect(installed.stdout).toContain("Surogate Desktop: tester was added to the kvm group: log out and back in to make the agent's commands fast.\n");
    expect(current()).toBe("/opt/surogate/versions/1.0.0");

    // The packages, QEMU's without its recommends, and its firmware for q35.
    expect(root("dpkg-query -W -f='${Status}\\n' bubblewrap socat ripgrep virtiofsd uidmap zstd openssl jq qemu-system-x86 | sort -u").stdout).toBe("install ok installed\n");
    expect(root("dpkg-query -W -f='${Status}\\n' qemu-system-gui 2>/dev/null").stdout).not.toBe("install ok installed\n");
    expect(root("timeout 3 qemu-system-x86_64 -machine q35,accel=tcg -display none -nodefaults -S").status).toBe(124);
    expect(root("getent group kvm").stdout).toMatch(/\btester\b/);

    // The profile, as both releases' parsers read it.
    expect(root("cat /etc/apparmor.d/surogate-desktop").stdout).toBe([
      "abi <abi/4.0>,", "include <tunables/global>", "",
      "profile surogate-desktop /opt/surogate/versions/*/surogate flags=(unconfined) {", "  userns,", "",
      "  include if exists <local/surogate-desktop>", "}", "",
    ].join("\n"));
    expect(root("/usr/sbin/apparmor_parser --skip-kernel-load -Q /etc/apparmor.d/surogate-desktop").status).toBe(0);

    // The launcher clears ELECTRON_RUN_AS_NODE, and passes its arguments on.
    expect(as("tester", "ELECTRON_RUN_AS_NODE=1 setsid /usr/local/bin/surogate 600 & sleep 1").status).toBe(0);
    const pid = root("pgrep -f '^/opt/surogate/current/surogate 600'").stdout.trim();
    expect(root(`tr '\\0' '\\n' </proc/${pid}/environ | grep -c ELECTRON_RUN_AS_NODE`).stdout).toBe("0\n");
    root(`kill ${pid}`);
    // The desktop entry registers surogate:// for every user.
    expect(root("desktop-file-validate /usr/share/applications/surogate.desktop").stdout).toBe("");
    expect(root("grep -x 'x-scheme-handler/surogate=surogate.desktop;' /usr/share/applications/mimeinfo.cache").status).toBe(0);
    expect(root("grep -x 'Exec=/usr/local/bin/surogate %u' /usr/share/applications/surogate.desktop").status).toBe(0);
    // The install record, root's, which every user reads.
    expect(root("stat -c '%a %U' /etc/surogate/install.json /usr/local/bin/surogate /usr/share/polkit-1/actions/ai.invergent.surogate.update.policy").stdout)
      .toBe("644 root\n755 root\n644 root\n");
    expect(JSON.parse(root("cat /etc/surogate/install.json").stdout)).toEqual({ base, channel: "stable" });
  });

  it("installs again as a repair, through the user's proxy, takes a newer release, and refuses one the release key did not sign", () => {
    // sudo resets the environment: the root half's downloads still go through the proxy the
    // user's shell names, here the base itself, for a server name that does not resolve.
    const proxied = as("tester", `curl -fsSL ${base}/desktop/install.sh | http_proxy=${base} bash -s -- --base http://surogate.invalid`);
    expect(proxied.status, proxied.stderr).toBe(0);
    expect(JSON.parse(root("cat /etc/surogate/install.json").stdout)).toEqual({ base: "http://surogate.invalid", channel: "stable" });
    expect(current()).toBe("/opt/surogate/versions/1.0.0");
    publish("1.1.0");
    expect(install().status).toBe(0);
    expect(versions()).toEqual(["1.0.0", "1.1.0"]);
    publish("1.2.0", other.privateKey);
    expect(install()).toMatchObject({ status: 1, stderr: `Surogate Desktop: ${base}/desktop/latest.json is not signed by Surogate's release key\n` });
    expect(current()).toBe("/opt/surogate/versions/1.1.0");
    // Its download's folder goes, whether it finished or not.
    expect(root("find /tmp -mindepth 1 -maxdepth 1 -name 'tmp.*'").stdout).toBe("");
    publish("1.1.0");
  });

  it("installs the same release again, without downloading it while it is here whole, and keeps the version before it", () => {
    // The server's 1.1.0 is a build of its own since the last install: the installed one is replaced by it.
    const replaced = install();
    expect(replaced.status, replaced.stderr).toBe(0);
    expect(replaced.stdout).toContain("Surogate Desktop: downloading Surogate Desktop 1.1.0\n");
    expect(versions()).toEqual(["1.0.0", "1.1.0"]);
    // Here whole, it is repaired as it is: its bwrap taken again, and nothing downloaded.
    expect(root("echo stale >/opt/surogate/versions/1.1.0/bin/bwrap").status).toBe(0);
    const repaired = install();
    expect(repaired.status, repaired.stderr).toBe(0);
    expect(repaired.stdout).not.toContain("downloading");
    expect(repaired.stdout).toContain("Surogate Desktop: 1.1.0 is installed\n");
    expect(root("cmp /usr/bin/bwrap /opt/surogate/versions/1.1.0/bin/bwrap").status).toBe(0);
    expect(versions()).toEqual(["1.0.0", "1.1.0"]);
    // Without one of its programs, it is downloaded and unpacked again.
    expect(root("rm /opt/surogate/versions/1.1.0/surogate").status).toBe(0);
    const mended = install();
    expect(mended.status, mended.stderr).toBe(0);
    expect(mended.stdout).toContain("Surogate Desktop: downloading Surogate Desktop 1.1.0\n");
    expect(root("test -x /opt/surogate/versions/1.1.0/surogate").status).toBe(0);
    expect(versions()).toEqual(["1.0.0", "1.1.0"]);
  });

  it("lets an administrator approve through polkit the update a user downloaded, and nothing else of the helper", () => {
    // The system bus and polkit, as a desktop runs them, and an administrator who has just approved.
    expect(root("mkdir -p /run/dbus && dbus-daemon --system --fork && (/usr/lib/polkit-1/polkitd --no-debug >/dev/null 2>&1 &) && sleep 2").status).toBe(0);
    expect(root(`echo 'polkit.addRule(function (action, subject) { if (action.id == "ai.invergent.surogate.update" && subject.user == "tester") return polkit.Result.YES; });' >/etc/polkit-1/rules.d/10-test.rules && sleep 2`).status).toBe(0);
    const tarball = releaseOf("1.3.0");
    manifestOf("1.3.0", tarball);
    // Where the app downloads it, as the user.
    const updates = "/home/tester/.cache/surogate/updates";
    expect(root(`mkdir -p ${updates}`).status).toBe(0);
    for (const [from, to] of [[join(box.dir, "manifest.json"), "manifest.json"], [join(box.dir, "manifest.json.sig"), "manifest.json.sig"], [tarball, "release.tar.gz"]] as const) {
      expect(docker(["cp", from, `${box.container}:${updates}/${to}`]).status).toBe(0);
    }
    expect(root("chown -R tester /home/tester/.cache").status).toBe(0);
    // The helper reads as the user pkexec or sudo ran it for, whatever that user's own environment
    // says of who asked: a signed manifest only root reads, behind a link of the user's, is not read.
    expect(root(`mkdir -m 700 /root/updates && cp ${updates}/manifest.json /root/updates/ && ln -s /root/updates /home/tester/linked`).status).toBe(0);
    for (const run of ["pkexec", "sudo"]) {
      const through = as("tester", `PKEXEC_UID=0 SUDO_UID=0 ${run} /opt/surogate/bin/surogate-apply-update --apply /home/tester/linked/manifest.json ${updates}/manifest.json.sig ${updates}/release.tar.gz; exit $?`);
      expect(through, run).toMatchObject({ status: 1, stdout: "", stderr: "Surogate Desktop: /home/tester/linked/manifest.json cannot be read by tester: name it by its whole path, in a folder of that user's own\n" });
    }
    expect(current()).toBe("/opt/surogate/versions/1.1.0");
    // Not exec'd by bash, as the app spawns it: polkit reads its caller's start, and docker exec's has none.
    const applied = as("tester", `pkexec /opt/surogate/bin/surogate-apply-update --apply ${updates}/manifest.json ${updates}/manifest.json.sig ${updates}/release.tar.gz; exit $?`);
    expect(applied, applied.stderr).toMatchObject({ status: 0, stdout: "Surogate Desktop: 1.3.0 is installed\n" });
    expect(current()).toBe("/opt/surogate/versions/1.3.0");
    // Any other use of the helper is not the update's action: polkit asks for an administrator, and nobody is there to answer.
    const other = as("tester", "pkexec /opt/surogate/bin/surogate-apply-update --uninstall; exit $?");
    expect(other).toMatchObject({ status: 127, stderr: expect.stringContaining("Error creating textual authentication agent") });
    expect(root("test -e /opt/surogate/current").status).toBe(0);
    root("rm /etc/polkit-1/rules.d/10-test.rules");
  });

  it("repairs an install newer than its server's latest, and keeps that version", () => {
    // The administrator's update made it 1.3.0, and the server still names 1.1.0, as a mirror that lags may.
    expect(root("rm /usr/local/bin/surogate").status).toBe(0);
    const repaired = install();
    expect(repaired.status, repaired.stderr).toBe(0);
    expect(repaired.stdout).toContain("Surogate Desktop: kept the installed 1.3.0, newer than the server's 1.1.0\n");
    expect(repaired.stdout).not.toContain("downloading");
    expect(current()).toBe("/opt/surogate/versions/1.3.0");
    expect(root("test -x /usr/local/bin/surogate").status).toBe(0);
  });

  it("uninstalls for every user, and asks before deleting the user's own data, as that user, under the folders their session names", () => {
    expect(root("/opt/surogate-test/install.sh --remove")).toMatchObject({ status: 1, stderr: "Surogate Desktop: usage: install.sh [--base <url>] [--uninstall]\n" });
    expect(root("/opt/surogate-test/install.sh --uninstall now")).toMatchObject({ status: 1, stderr: "Surogate Desktop: usage: install.sh --uninstall\n" });
    const folders = "XDG_CONFIG_HOME=/home/tester/cfg XDG_DATA_HOME=/home/tester/dat XDG_CACHE_HOME=/home/tester/cch";
    expect(as("tester", "mkdir -p cfg/autostart dat/surogate/electron cch/surogate/updates Surogate/agent/2026-10-08 && touch cfg/autostart/surogate.desktop Surogate/agent/2026-10-08/report.docx").status).toBe(0);
    expect(as("other", "mkdir -p .config/autostart .local/share/surogate && touch .config/autostart/surogate.desktop").status).toBe(0);
    // An apply holds the update's lock: the uninstall waits for it, rather than remove a tree being made.
    expect(root(`flock ${LOCKS}/lock timeout 3 /opt/surogate-test/install.sh --uninstall`).status).toBe(124);
    expect(root("test -e /opt/surogate/current").status).toBe(0);
    // A running app is never removed from under it.
    expect(as("tester", "setsid /usr/local/bin/surogate 600 & sleep 1").status).toBe(0);
    expect(uninstall()).toMatchObject({ status: 1, stderr: "Surogate Desktop: Surogate is running: quit it first, for every user of this computer\n" });
    root("pkill -f '^/opt/surogate/current/surogate 600'");

    // Without a terminal to answer on, the data stays, and nothing is said about the terminal.
    const kept = uninstall(folders);
    expect(kept).toMatchObject({ status: 0, stderr: "" });
    expect(kept.stdout).toContain("Surogate Desktop: kept tester's app data, in /home/tester/dat/surogate\n");
    for (const gone of ["/opt/surogate", "/etc/surogate", "/usr/local/bin/surogate", "/usr/share/applications/surogate.desktop", "/etc/apparmor.d/surogate-desktop",
      "/usr/share/polkit-1/actions/ai.invergent.surogate.update.policy", "/home/tester/cfg/autostart/surogate.desktop"]) {
      expect(root(`test ! -e ${gone}`).status, gone).toBe(0);
    }
    expect(root("grep -c surogate /usr/share/applications/mimeinfo.cache").stdout).toBe("0\n");
    for (const stays of ["/home/tester/dat/surogate/electron", "/home/tester/cch/surogate/updates", "/home/tester/Surogate/agent/2026-10-08/report.docx",
      "/home/other/.config/autostart/surogate.desktop", "/home/other/.local/share/surogate"]) {
      expect(root(`test -e ${stays}`).status, stays).toBe(0);
    }
    expect(root("dpkg-query -W -f='${Status}\\n' qemu-system-x86").stdout).toBe("install ok installed\n");

    // Installed again, then removed with a terminal that answers yes.
    const answered = () => as("tester", `printf 'y\\n' | script -qec "curl -fsSL ${base}/desktop/install.sh | ${folders} bash -s -- --uninstall" /dev/null`);
    const again = install();
    expect(again.status, again.stderr).toBe(0);
    const deleted = answered();
    expect(deleted.status, deleted.stderr).toBe(0);
    expect(deleted.stdout).toContain("Surogate Desktop: deleted tester's app data");
    expect(root("test ! -e /home/tester/dat/surogate && test ! -e /home/tester/cch/surogate && test -e /home/tester/Surogate/agent/2026-10-08/report.docx && test -e /home/other/.local/share/surogate").status).toBe(0);

    // A yes deletes only what is the user's to change: the deletion runs as the user.
    expect(install().status).toBe(0);
    expect(as("tester", "mkdir -p dat/surogate").status).toBe(0);
    expect(root("mkdir -p /home/tester/cch/surogate && touch /home/tester/cch/surogate/root-only").status).toBe(0);
    const refused = answered();
    expect(refused.status, refused.stdout).toBe(1);
    // The terminal carries both of its streams.
    expect(refused.stdout).toContain("Surogate Desktop: removed from this computer");
    expect(refused.stdout).toContain("Surogate Desktop: could not delete all of tester's app data: what tester may not change stays, in /home/tester/dat/surogate and /home/tester/cch/surogate");
    expect(root("test ! -e /home/tester/dat/surogate && test -e /home/tester/cch/surogate/root-only && test ! -e /opt/surogate").status).toBe(0);
  });

  it("uninstalls under sudo too, finding the user's own XDG_CONFIG_HOME in their login", () => {
    const installed = install();
    expect(installed.status, installed.stderr).toBe(0);
    expect(as("tester", "echo 'export XDG_CONFIG_HOME=/home/tester/login-cfg' >>.profile && mkdir -p login-cfg/autostart .local/share/surogate && touch login-cfg/autostart/surogate.desktop").status).toBe(0);
    expect(as("tester", `curl -fsSL ${base}/desktop/install.sh -o install.sh && sudo bash install.sh --uninstall`).status).toBe(0);
    expect(root("test ! -e /home/tester/login-cfg/autostart/surogate.desktop && test ! -e /opt/surogate && test -e /home/tester/.local/share/surogate").status).toBe(0);
  });

  it("refuses --uninstall with anything after it as a user too, before it asks for sudo", () => {
    expect(install().status).toBe(0);
    for (const args of ["--dry-run", "--help", "/home/tester/cfg /home/tester/dat /home/tester/cch"]) {
      expect(as("tester", `curl -fsSL ${base}/desktop/install.sh | bash -s -- --uninstall ${args}`), args)
        .toMatchObject({ status: 1, stdout: "", stderr: "Surogate Desktop: usage: install.sh --uninstall\n" });
    }
    expect(root("test -x /opt/surogate/current/surogate && test -e /usr/local/bin/surogate").status).toBe(0);
    expect(uninstall().status).toBe(0);
  });

  it("removes again where nothing of it is left, with the system's own tools, and says the user's folders as one word of one line", () => {
    // The root half, as the user's half starts it for tester: handed the user's three folders, or none, as under sudo.
    const none = "/home/tester/none";
    const half = (folders: string[]) => docker(["exec", "-e", "SUDO_USER=tester", box.container, "/opt/surogate-test/install.sh", "--uninstall", ...folders]);
    // First on its caller's PATH, an rm that leaves a mark: the script's own rm, and the one it runs as the user, are the system's.
    expect(root("mkdir -p /tmp/caller && printf '#!/bin/sh\\ntouch /tmp/caller/ran\\n' >/tmp/caller/rm && chmod 755 /tmp/caller/rm").status).toBe(0);
    expect(root(`PATH=/tmp/caller:$PATH SUDO_USER=tester /opt/surogate-test/install.sh --uninstall ${none} ${none} ${none}`))
      .toMatchObject({ status: 0, stdout: "Surogate Desktop: removed from this computer\n", stderr: "" });
    expect(root("test -e /tmp/caller/ran").status).toBe(1);
    // Under sudo the folders are the login's: tester's data is where XDG's default puts it. The lock
    // is root's alone: the user's login is not handed it.
    expect(as("tester", "echo 'ls -l /proc/$$/fd >/home/tester/login-fds' >>.profile").status).toBe(0);
    expect(half([])).toMatchObject({ status: 0, stderr: "", stdout: "Surogate Desktop: removed from this computer\nSurogate Desktop: kept tester's app data, in /home/tester/.local/share/surogate\n" });
    expect(root(`test ! -e /opt/surogate && grep -c /dev/null /home/tester/login-fds && grep -c ${LOCKS} /home/tester/login-fds`).stdout).toMatch(/^[1-9]\d*\n0\n$/);

    // A data folder whose name would read as a line of the script's own, and colour what follows it: kept, then asked about.
    const data = "/home/tester/da ta\nSurogate Desktop: 9.9.9 is installed\u001b[31m";
    const quoted = "$'/home/tester/da ta\\nSurogate Desktop: 9.9.9 is installed\\E[31m/surogate'";
    expect(docker(["exec", "-u", "tester", box.container, "mkdir", "-p", `${data}/surogate`]).status).toBe(0);
    expect(half([none, data, none])).toMatchObject({ status: 0, stderr: "", stdout: `Surogate Desktop: removed from this computer\nSurogate Desktop: kept tester's app data, in ${quoted}\n` });
    const asked = docker(["exec", "-e", "SUDO_USER=tester", "-e", `DATA=${data}`, box.container, "bash", "-c",
      `printf 'y\\n' | script -qec '/opt/surogate-test/install.sh --uninstall ${none} "$DATA" ${none}' /dev/null`]);
    expect(asked.status, asked.stdout).toBe(0);
    expect(asked.stdout).toContain(`Surogate Desktop: also delete tester's sign-in, device token and browser profiles, in ${quoted}? Chat folders stay. [y/N] `);
    expect(asked.stdout).not.toMatch(/^Surogate Desktop: 9\.9\.9 is installed/m);
    expect(asked.stdout).toContain("Surogate Desktop: deleted tester's app data");
    expect(docker(["exec", box.container, "test", "-e", `${data}/surogate`]).status).toBe(1);

    // A Start at login entry in a folder that is not the user's to change stays: rm's own words, then a line of the script's.
    expect(root("mkdir -p /home/tester/rooted/autostart && touch /home/tester/rooted/autostart/surogate.desktop").status).toBe(0);
    const stopped = half(["/home/tester/rooted", none, none]);
    expect(stopped).toMatchObject({ status: 1, stdout: "Surogate Desktop: removed from this computer\n" });
    expect(stopped.stderr.trimEnd().split("\n").at(-1)).toBe('Surogate Desktop: stopped, as this step failed: runuser -u "$user" -- rm -f -- "$config/autostart/surogate.desktop"');
    expect(root("test -e /home/tester/rooted/autostart/surogate.desktop").status).toBe(0);
  });

  it("leaves no version that an install would take as whole, when it is stopped as it removes them", () => {
    const installed = install();
    expect(installed.status, installed.stderr).toBe(0);
    writeFileSync(join(box.dir, "rm"), CUT, { mode: 0o755 });
    expect(docker(["cp", join(box.dir, "rm"), `${box.container}:/opt/surogate-test/rm`]).status).toBe(0);
    // Killed as its rm starts on the tree, a file of the version gone: a kill, Ctrl+C or a power cut part-way.
    const cut = root("mkdir -p /opt/cut && cp -L /usr/bin/rm /opt/cut/rm && mv /usr/bin/rm /usr/bin/rm.away && cp /opt/surogate-test/rm /usr/bin/rm"
      + "; /opt/surogate-test/install.sh --uninstall; said=$?; mv -f /usr/bin/rm.away /usr/bin/rm; exit $said");
    expect(cut.status, cut.stderr).toBe(137);
    expect(root("test -x /opt/surogate/versions/1.1.0/surogate && test ! -e /opt/surogate/versions/1.1.0/resources/app/package.json").status).toBe(0);
    // The install after it unpacks the release again: found whole, the version would have stayed as it was left.
    const again = install();
    expect(again.status, again.stderr).toBe(0);
    expect(again.stdout).toContain("Surogate Desktop: downloading Surogate Desktop 1.1.0\n");
    expect(root("test -e /opt/surogate/current/resources/app/package.json").status).toBe(0);
    expect(uninstall().status).toBe(0);
    expect(root("test ! -e /opt/surogate").status).toBe(0);
  });

  // A release's files in a folder of their own in the container, and their names as --apply takes them.
  const staged = (version: string) => {
    const tarball = releaseOf(version);
    manifestOf(version, tarball);
    const folder = `/home/tester/staged-${version}`;
    expect(root(`mkdir -p ${folder}`).status).toBe(0);
    for (const [from, to] of [[join(box.dir, "manifest.json"), "manifest.json"], [join(box.dir, "manifest.json.sig"), "manifest.json.sig"], [tarball, "release.tar.gz"]] as const) {
      expect(docker(["cp", from, `${box.container}:${folder}/${to}`]).status).toBe(0);
    }
    return `${folder}/manifest.json ${folder}/manifest.json.sig ${folder}/release.tar.gz`;
  };
  // The system's *tool* made to wait at *operand*, for the one docker call that runs *lines*.
  const withHeld = (tool: string, operand: string, lines: string[]) => {
    writeFileSync(join(box.dir, "holding"), holding(operand), { mode: 0o755 });
    expect(docker(["cp", join(box.dir, "holding"), `${box.container}:/opt/surogate-test/holding`]).status).toBe(0);
    return root([
      `mkdir -p /opt/hold && cp -L /usr/bin/${tool} /opt/hold/${tool} && rm -f /tmp/held /tmp/go`,
      `mv /usr/bin/${tool} /usr/bin/${tool}.away && cp /opt/surogate-test/holding /usr/bin/${tool}`,
      ...lines,
      `mv -f /usr/bin/${tool}.away /usr/bin/${tool}`,
      'echo "$said"; cat /tmp/first.out; echo --; cat /tmp/second.out',
    ].join("\n"));
  };
  // Run in the background until /tmp/held is there, then until one more waits for the lock.
  const untilHeld = "for try in $(seq 200); do [ -e /tmp/held ] && break; sleep 0.05; done";
  const untilWaiting = "for try in $(seq 200); do pgrep -x flock >/dev/null && break; sleep 0.05; done; pgrep -x flock >/dev/null; waiting=$?";

  it("makes the tree again when it waited for a removal that took it away, and never finds its own folders gone", () => {
    const files = staged("1.0.0");
    expect(root(`/opt/surogate-test/install.sh --apply ${files}`).status).toBe(0);
    // The removal stopped as it starts on the tree, the lock taken; the apply starts then.
    const both = withHeld("rm", "/opt/surogate", [
      "/opt/surogate-test/install.sh --uninstall >/tmp/first.out 2>&1 & removal=$!",
      untilHeld,
      `/opt/surogate-test/install.sh --apply ${files} >/tmp/second.out 2>&1 & apply=$!`,
      untilWaiting,
      "touch /tmp/go; wait $removal; removed=$?; wait $apply; applied=$?",
      'said="waiting $waiting, removed $removed, applied $applied"',
    ]);
    expect(both.stdout).toBe("waiting 0, removed 0, applied 0\nSurogate Desktop: removed from this computer\n--\nSurogate Desktop: 1.0.0 is installed\n");
    expect(root("test -x /opt/surogate/current/surogate && test -x /opt/surogate/current/bin/bwrap && cmp /opt/surogate/current/bin/surogate-apply-update /opt/surogate/bin/surogate-apply-update").status).toBe(0);
  });

  it("removes nothing while an apply runs: the removal waits for it, and then takes away what it made", () => {
    const files = staged("1.1.0");
    // The apply stopped where it flushes what it staged, before any rename; the removal starts then.
    const both = withHeld("sync", "-f", [
      `/opt/surogate-test/install.sh --apply ${files} >/tmp/first.out 2>&1 & apply=$!`,
      untilHeld,
      "/opt/surogate-test/install.sh --uninstall >/tmp/second.out 2>&1 & removal=$!",
      untilWaiting,
      'staged="$(ls -A /opt/surogate/staging | wc -l)"',
      "touch /tmp/go; wait $apply; applied=$?; wait $removal; removed=$?",
      'said="waiting $waiting, staged $staged, applied $applied, removed $removed"',
    ]);
    expect(both.stdout).toBe("waiting 0, staged 1, applied 0, removed 0\nSurogate Desktop: 1.1.0 is installed\n--\nSurogate Desktop: removed from this computer\n");
    expect(root("test ! -e /opt/surogate && test ! -e /usr/local/bin/surogate").status).toBe(0);
  });

  it("makes the folder of its download itself, in one step with its name: another user who takes each name the moment it is given out gets none, and loses no folder of their own", () => {
    publish("1.6.0");
    const taken = swapped("mktemp", TAKING, [
      "rm -f /tmp/taken",
      `runuser -u tester -- bash -c 'cd && curl -fsSL ${base}/desktop/install.sh | bash -s -- --base ${base}' >/tmp/said 2>&1; said=$?`,
      'echo "$said"; for name in $(cat /tmp/taken 2>/dev/null); do [ -e "$name/theirs" ] && echo "taken, and still theirs: $name" || echo "taken, and removed: $name"; done; tail -n 1 /tmp/said',
    ]);
    // No name was theirs to take, and the install went on to its end.
    expect(taken.stdout).toBe("0\nSurogate Desktop: open Surogate from your applications, or run surogate\n");
    expect(current()).toBe("/opt/surogate/versions/1.6.0");
    expect(root("find /tmp -mindepth 1 -maxdepth 1 -name 'tmp.*'").stdout).toBe("");
  });

  it("leaves no folder of its download when a signal comes as the folder is made, before the script has its name: the signal is let by, and the install goes on", () => {
    publish("1.7.0");
    // Root's part by itself, in a session of its own: the signal goes to all of its processes, as Ctrl+C on a terminal does.
    const signalled = swapped("mktemp", interrupting("INT"), [
      ": >/tmp/made",
      `setsid -w /opt/surogate-test/install.sh --base ${base} >/tmp/said 2>&1; said=$?`,
      'echo "$said: $(grep -c "^/tmp/tmp\\." /tmp/made) made in /tmp, $(for made in $(cat /tmp/made); do [ ! -e "$made" ] || echo "$made"; done | wc -l) left; $(tail -n 1 /tmp/said)"',
    ]);
    expect(signalled.stdout).toBe("0: 1 made in /tmp, 0 left; Surogate Desktop: open Surogate from your applications, or run surogate\n");
    expect(current()).toBe("/opt/surogate/versions/1.7.0");
    expect(uninstall().status).toBe(0);
  });

  // A desktop in German, as Ubuntu's installer sets one up: its locale, and the language of what
  // every tool says, which sudo and pkexec both pass on to what they run as root.
  const GERMAN = "LANG=de_DE.UTF-8 LANGUAGE=de";
  // The system's tools do speak German there, to root too: the head of the one column of df's that the script reads.
  const german = () => expect(as("tester", `${GERMAN} sudo df --output=avail -k /opt | head -n 1`).stdout.trim()).toBe("Verf.");

  it("installs, installs again, applies an update and uninstalls for a user whose desktop is German, as it does in English", () => {
    german();
    publish("2.0.0");
    const installed = install(GERMAN);
    expect(installed.status, installed.stderr).toBe(0);
    expect(installed.stdout).toContain("Surogate Desktop: 2.0.0 is installed\n");
    expect(installed.stdout).toContain("Surogate Desktop: open Surogate from your applications, or run surogate\n");
    expect(current()).toBe("/opt/surogate/versions/2.0.0");
    expect(root("test -x /usr/local/bin/surogate && test -e /etc/surogate/install.json").status).toBe(0);
    // Again, as a repair: here whole, it is not downloaded.
    const repaired = install(GERMAN);
    expect(repaired.status, repaired.stderr).toBe(0);
    expect(repaired.stdout).not.toContain("downloading");
    expect(repaired.stdout).toContain("Surogate Desktop: 2.0.0 is installed\n");
    // An update the user downloaded, through the installed helper.
    expect(as("tester", `${GERMAN} sudo /opt/surogate/bin/surogate-apply-update --apply ${staged("2.1.0")}`)).toMatchObject({ status: 0, stdout: "Surogate Desktop: 2.1.0 is installed\n" });
    expect(versions()).toEqual(["2.0.0", "2.1.0"]);
    // And its removal. The user's data stays without a terminal to ask on: its folder's letters
    // outside ASCII are said by their bytes, as root's part of the script says every name.
    expect(as("tester", "mkdir -p Schl\u00fcssel/surogate").status).toBe(0);
    const removed = uninstall(`${GERMAN} XDG_DATA_HOME=/home/tester/Schl\u00fcssel`);
    expect(removed).toMatchObject({
      status: 0, stderr: "",
      stdout: "Surogate Desktop: removing it needs administrator rights: sudo asks for your password once\nSurogate Desktop: removed from this computer\n"
        + "Surogate Desktop: kept tester's app data, in $'/home/tester/Schl\\303\\274ssel/surogate'\n",
    });
    expect(root("test ! -e /opt/surogate && test ! -e /usr/local/bin/surogate && test ! -e /etc/surogate && test -d /home/tester/Schl\u00fcssel/surogate").status).toBe(0);
  });

  it("starts the system's tools in no locale and no language of its caller's, in each part that runs as root: the install, an apply and the removal", () => {
    german();
    // What each of *tools* was started with, of all that names a locale or a language, as *part* started it for a caller whose desktop is German.
    const started = (part: string, tools: string[]) => {
      const traced = root(`${GERMAN} strace --seccomp-bpf -f -qq -v -s 256 -o /tmp/trace -e trace=execve ${part} >/dev/null 2>&1; echo "$?"; grep -E '^[0-9]+ +execve\\("[^"]*/(${tools.join("|")})", ' /tmp/trace`);
      const [status, ...calls] = traced.stdout.trim().split("\n");
      expect(status, part).toBe("0");
      const named = calls.map((call) => [/^\d+ +execve\("[^"]*\/([^"/]+)", /.exec(call)?.[1], ...[...call.matchAll(/"((?:LANG|LANGUAGE|LC_\w+)=[^"]*)"/g)].map((match) => match[1]).sort()].join(" "));
      expect([...new Set(named)].sort(), part).toEqual(tools.map((tool) => `${tool} LANG=C LC_ALL=C`).sort());
    };
    publish("2.0.0");
    started(`/opt/surogate-test/install.sh --base ${base}`, ["apt-get", "curl", "jq"]);
    started(`/opt/surogate-test/install.sh --apply ${staged("2.2.0")}`, ["flock", "df", "tar"]);
    started("/opt/surogate-test/install.sh --uninstall", ["flock", "mountpoint", "rm"]);
    expect(root("test ! -e /opt/surogate").status).toBe(0);
  });

  it("takes the folder of its lock for root's own by its numbers, whatever a folder is called in its caller's language", () => {
    german();
    // The lock by itself, from the script's functions without its last line, in German: its folder made, then found there.
    const alone = root(`rm -rf ${LOCKS}; for found in no yes; do ${GERMAN} bash -c '. <(sed "\\$d" /opt/surogate-test/install.sh) && settings && lock' || exit; done; stat -c '%f %u' ${LOCKS}`);
    expect(alone).toMatchObject({ status: 0, stdout: "41c0 0\n", stderr: "" });
  });
});

for (const release of RELEASES) describe.skipIf(!ENABLED)(`the install script's --uninstall as root, on Ubuntu ${release}`, { timeout: 120_000 }, () => {
  // The --apply tests' computer, with /opt/surogate a disk of its own (a mount point), as some keep it.
  const { it: box, docker, root, releaseOf, manifestOf, elsewhere } = lab(release, APPLY_LAB, OWN_DISK);
  const files = "/home/tester/manifest.json /home/tester/manifest.json.sig /home/tester/release.tar.gz";
  const installed = (version: string) => {
    const tarball = releaseOf(version);
    manifestOf(version, tarball);
    for (const [from, to] of [[join(box.dir, "manifest.json"), "manifest.json"], [join(box.dir, "manifest.json.sig"), "manifest.json.sig"], [tarball, "release.tar.gz"]] as const) {
      expect(docker(["cp", from, `${box.container}:/home/tester/${to}`]).status).toBe(0);
    }
    expect(root(`/opt/surogate-test/install.sh --apply ${files}`)).toMatchObject({ status: 0, stdout: `Surogate Desktop: ${version} is installed\n` });
  };

  it("refuses what stands in its lock's folder's place and is not root's own, and removes nothing", () => {
    installed("1.0.0");
    for (const [what, how] of NOT_ROOTS_OWN) {
      expect(root(`rm -rf ${LOCKS} /root/locks /home/tester/locks; ${how}`).status, what).toBe(0);
      expect(root("/opt/surogate-test/install.sh --uninstall"), what).toMatchObject({ status: 1, stdout: "", stderr: NOT_ROOTS_OWN_SAID });
      expect(root("test -x /opt/surogate/current/surogate && test -f /opt/surogate/current/release.json").status, what).toBe(0);
    }
    expect(root(`rm -rf ${LOCKS} /root/locks /home/tester/locks`).status).toBe(0);
  });

  it("removes all that is in /opt/surogate where the folder is a disk of its own, and the rest of the install with it, and says that the folder is left", () => {
    installed("1.0.0");
    // What an install puts around the tree, and a file of the disk's own beside the tree's.
    const around = ["/etc/surogate/install.json", "/etc/apparmor.d/surogate-desktop", "/usr/local/bin/surogate", "/usr/share/applications/surogate.desktop",
      "/usr/share/polkit-1/actions/ai.invergent.surogate.update.policy"];
    expect(root(`mkdir -p /etc/surogate /etc/apparmor.d /usr/share/applications /usr/share/polkit-1/actions && touch ${around.join(" ")} /opt/surogate/.hidden`).status).toBe(0);
    expect(root("/opt/surogate-test/install.sh --uninstall")).toMatchObject({
      status: 0, stderr: "",
      stdout: "Surogate Desktop: removed from this computer\nSurogate Desktop: left /opt/surogate itself, now empty: it is a disk of its own (a mount point)\n",
    });
    expect(root("ls -A /opt/surogate && mountpoint -q /opt/surogate")).toMatchObject({ status: 0, stdout: "" });
    for (const gone of [...around, "/etc/surogate"]) expect(root(`test ! -e ${gone}`).status, gone).toBe(0);
    // The install after it finds the disk as a first install does.
    installed("1.0.0");
    expect(root("/opt/surogate-test/install.sh --uninstall").status).toBe(0);
  });

  it("refuses an /opt/surogate that is a link to a disk of its own before it follows it: a removal removes nothing and says why, and an apply makes nothing there its own", () => {
    const tarball = releaseOf("1.0.0");
    manifestOf("1.0.0", tarball);
    const files = `/srv/manifest.json /srv/manifest.json.sig /srv/${tarball.split("/").pop()}`;
    // A computer with a disk of its own beside /opt, in memory.
    elsewhere(["--tmpfs", "/mnt/disk:exec,mode=755,size=256m"], [join(box.dir, "manifest.json"), join(box.dir, "manifest.json.sig"), tarball], (root) => {
      // Installed in a folder, then moved to the disk by hand, with a link left in the folder's place; and what an install puts around the tree.
      const around = ["/etc/surogate/install.json", "/usr/local/bin/surogate", "/usr/share/applications/surogate.desktop", "/usr/share/polkit-1/actions/ai.invergent.surogate.update.policy"];
      expect(root(`/srv/install.sh --apply ${files} && cp -a /opt/surogate/. /mnt/disk/ && rm -rf /opt/surogate && ln -s /mnt/disk /opt/surogate && chown tester: /mnt/disk`
        + ` && mkdir -p /etc/surogate /usr/share/applications /usr/share/polkit-1/actions && touch ${around.join(" ")} && mountpoint -q /opt/surogate/ && test -x /opt/surogate/current/surogate`).status).toBe(0);
      const there = () => root(`find /mnt/disk | sort; stat -c '%U %a' /mnt/disk; ls -d /opt/surogate ${around.join(" ")}`).stdout;
      const before = there();
      expect(root("/srv/install.sh --uninstall")).toMatchObject({ status: 1, stdout: "", stderr: LINKED_NOTHING_REMOVED });
      expect(root(`/srv/install.sh --apply ${files}`)).toMatchObject({ status: 1, stdout: "", stderr: linked() });
      // The disk, its owner, the link and the rest of the install are as they were.
      expect(there()).toBe(before);
      expect(before).toContain("/mnt/disk/versions/1.0.0/release.json\n");
    });
  });

  it("finds the user's data, asks about it and deletes it in a home that root cannot read, as the user each time", () => {
    // Root without its right to pass by a folder's mode, as it is in a home that another computer
    // serves; and tester's folders, which only tester opens.
    const folders = "/home/tester/closed/cfg /home/tester/closed/dat /home/tester/closed/cch";
    const squashed = `setpriv --bounding-set=-dac_override,-dac_read_search env SUDO_USER=tester /opt/surogate-test/install.sh --uninstall ${folders}`;
    const fresh = "rm -rf /home/tester/closed && runuser -u tester -- sh -c 'umask 077 && mkdir -p /home/tester/closed/cfg/autostart /home/tester/closed/dat/surogate /home/tester/closed/cch"
      + " && touch /home/tester/closed/cfg/autostart/surogate.desktop /home/tester/closed/dat/surogate/token'";
    expect(root(`${fresh} && setpriv --bounding-set=-dac_override,-dac_read_search test -e /home/tester/closed/dat/surogate`).status).toBe(1);
    // Without a terminal the data stays, and the script says where.
    expect(root(squashed)).toMatchObject({ status: 0, stderr: "", stdout: expect.stringMatching(/\nSurogate Desktop: kept tester's app data, in \/home\/tester\/closed\/dat\/surogate\n$/) });
    expect(root("test ! -e /home/tester/closed/cfg/autostart/surogate.desktop && test -e /home/tester/closed/dat/surogate/token").status).toBe(0);
    // With one that answers yes, it goes.
    const asked = root(`${fresh} && printf 'y\\n' | script -qec '${squashed}' /dev/null`);
    expect(asked.status, asked.stdout).toBe(0);
    expect(asked.stdout).toContain("Surogate Desktop: also delete tester's sign-in, device token and browser profiles, in /home/tester/closed/dat/surogate? Chat folders stay. [y/N] ");
    expect(asked.stdout).toContain("Surogate Desktop: deleted tester's app data");
    expect(root("test ! -e /home/tester/closed/dat/surogate && test -d /home/tester/closed/dat").status).toBe(0);
  });

  it("finds what the user has in their cache folder alone, as the user, and asks about it and deletes it as it does the rest", () => {
    const folders = "/home/tester/closed/cfg /home/tester/closed/dat /home/tester/closed/cch";
    const squashed = `setpriv --bounding-set=-dac_override,-dac_read_search env SUDO_USER=tester /opt/surogate-test/install.sh --uninstall ${folders}`;
    // An update the app downloaded, and nothing else of the app's: no data folder is there. Tester's folders, which only tester opens.
    const fresh = "rm -rf /home/tester/closed && runuser -u tester -- sh -c 'umask 077 && mkdir -p /home/tester/closed/cfg /home/tester/closed/dat /home/tester/closed/cch/surogate/updates"
      + " && touch /home/tester/closed/cch/surogate/updates/release.tar.gz'";
    expect(root(`${fresh} && setpriv --bounding-set=-dac_override,-dac_read_search test -e /home/tester/closed/cch/surogate`).status).toBe(1);
    // Without a terminal it stays, and the script says that the user's data was kept.
    expect(root(squashed)).toMatchObject({ status: 0, stderr: "", stdout: expect.stringMatching(/\nSurogate Desktop: kept tester's app data, in \/home\/tester\/closed\/dat\/surogate\n$/) });
    expect(root("test -e /home/tester/closed/cch/surogate/updates/release.tar.gz").status).toBe(0);
    // With one that answers yes, it goes.
    const asked = root(`${fresh} && printf 'y\\n' | script -qec '${squashed}' /dev/null`);
    expect(asked.status, asked.stdout).toBe(0);
    expect(asked.stdout).toContain("Surogate Desktop: also delete tester's sign-in, device token and browser profiles, in /home/tester/closed/dat/surogate? Chat folders stay. [y/N] ");
    expect(asked.stdout).toContain("Surogate Desktop: deleted tester's app data");
    expect(root("test ! -e /home/tester/closed/cch/surogate && test -d /home/tester/closed/cch").status).toBe(0);
  });
});
