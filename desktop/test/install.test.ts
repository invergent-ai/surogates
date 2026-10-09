// The install script (release/install.sh) in Ubuntu containers, never on this computer: its
// --apply, which each version ships as bin/surogate-apply-update and runs as root; the install from
// a server, as a user with sudo; --uninstall; and the company's CA, --ca-cert. Releases are small
// stand-ins in the tarball's layout, signed by a key of the test's own. Behind
// SUROGATE_INSTALL_TESTS=1: it needs Docker, the ubuntu:24.04 and ubuntu:26.04 images, and the
// Ubuntu archive for apt. The script's own list of release keys is read without either.

import { type ChildProcess, execFile, spawn, spawnSync } from "node:child_process";
import { createHash, createPublicKey, generateKeyPairSync, type KeyObject, randomBytes, sign } from "node:crypto";
import { chmodSync, copyFileSync, existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, renameSync, rmSync, statSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from "vitest";

import { companyCertificates } from "../src/shell/company-ca.js";
import { AS_ROOT } from "../src/shell/updates.js";

import { certificate, rewritten } from "./certificates.js";
import { HELPER_MODES } from "./helper-modes.js";

const SCRIPT = fileURLToPath(new URL("../release/install.sh", import.meta.url));
const PUBLISH = fileURLToPath(new URL("../release/publish.sh", import.meta.url));
const RELEASES = ["24.04", "26.04"] as const;
const ENABLED = process.env.SUROGATE_INSTALL_TESTS === "1";
// The longest one docker call may take: an install with apt's downloads takes under a minute.
const CALL_MS = 300_000;

// A static server of the folder it is given, on a port of its own, which it prints; over TLS when
// a certificate and its key follow the folder. A proxy's request, which names the whole URL, is
// served by its path alike. While the folder holds a file named unsized, it does not say how much
// it sends, as a server need not.
const SERVE = `
const { existsSync, readFileSync } = require("node:fs");
const { join } = require("node:path");
const [folder, cert, key] = process.argv.slice(1);
const answer = (request, response) => {
  try {
    const body = readFileSync(join(folder, decodeURIComponent(new URL(request.url, "http://x").pathname)));
    if (existsSync(join(folder, "unsized"))) response.write(body);
    else response.setHeader("Content-Length", body.length).write(body);
    response.end();
  } catch {
    response.writeHead(404).end();
  }
};
(cert ? require("node:https").createServer({ cert: readFileSync(cert), key: readFileSync(key) }, answer) : require("node:http").createServer(answer))
  .listen(0, "127.0.0.1", function () { console.log(this.address().port); });
`;

// The helper stopped (SIGKILL) before each command its own shell runs, in turn, each time from the
// install kept in /opt/pristine. A line for each stop: what current names and whether that folder
// is whole, then how the same apply, run again to its end, exits and what it leaves. Counted in
// the helper's own shell alone, as SIGNALS counts: what a $( ) runs counts on from where it
// began, and would stop the helper at a number before its own shell came to that number, so
// that the commands just after a long $( ) were never stopped at. Its renames are among them.
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
  STOP="$stop" bash -T -c 'n=0; trap "(( BASHPID == \$\$ )) && (( ++n == STOP )) && kill -KILL \$\$" DEBUG; . /opt/surogate-test/install.sh "$@"' stopped --apply "$@" >/dev/null 2>&1
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

// The helper stopped (SIGKILL) before each rename it makes, in turn, each time from the install
// kept in /opt/pristine. A line for each stop: what current names, the release the helper's mark
// names and whose the helper pkexec runs is, the release's before or the update's; then how the
// same apply, run again to its end, exits and what it leaves of the three.
const RENAMES = String.raw`
seen() {
  local helper=neither
  ! cmp -s /opt/surogate/bin/surogate-apply-update /opt/pristine/bin/surogate-apply-update || helper="the release's before"
  ! cmp -s /opt/surogate/bin/surogate-apply-update /opt/surogate/versions/1.1.0/bin/surogate-apply-update || helper="the update's"
  echo "current $(basename "$(readlink /opt/surogate/current)"), mark $(jq -r .version /opt/surogate/bin/release.json), helper $helper"
}
for stop in $(seq 100); do
  find /opt/surogate -mindepth 1 -delete
  cp -a /opt/pristine/. /opt/surogate/
  STOP="$stop" bash -T -c 'n=0; trap "[[ \$BASH_COMMAND == mv\ -T\ * ]] && (( ++n == STOP )) && kill -KILL \$\$" DEBUG; . /opt/surogate-test/install.sh "$@"' stopped --apply "$@" >/dev/null 2>&1
  [ "$?" -eq 137 ] || { echo "end"; exit 0; }
  stopped="$(seen)"
  /opt/surogate-test/install.sh --apply "$@" >/dev/null 2>&1
  echo "$stopped; again $?: $(seen)"
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

// The system's id, kept as /opt/hold/id, which says in /tmp/asked that it was asked, and answers
// after a moment: long enough for a signal to reach the script while it waits for the answer.
const SLOW_ID = String.raw`#!/bin/sh
: >>/tmp/asked
/opt/hold/sleep 0.3
exec /opt/hold/id "$@"
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
// What stands installed, all that a refusal leaves as it was: what current names, each version
// and its mark, and the helper pkexec runs and its mark, with whatever else is beside them: each
// by its place on the disk, its kind and mode, its owner, its size, its time and its bytes. The
// tree's own folders are not among it: an apply makes them before it refuses anything.
const STANDING = String.raw`
readlink /opt/surogate/current 2>/dev/null
ls /opt/surogate/versions 2>/dev/null
for mark in /opt/surogate/versions/*/release.json; do [ ! -e "$mark" ] || echo "$mark $(sha256sum <"$mark")"; done
for file in /opt/surogate/bin/*; do
  [ -e "$file" ] || [ -L "$file" ] || continue
  stat -c '%n %i %f %u %g %s %y' -- "$file"
  if [ -L "$file" ]; then readlink "$file"; elif [ -f "$file" ]; then sha256sum <"$file"; fi
done
`;
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
// What an install or a rollback adds, on a computer that has a helper, where no key of the helper's signed the base's release.
// What the script says of a release that no key this computer trusts has signed, behind the
// release's name: plainly, as of a rollback; with the one way on that this computer's own keys
// check, as of an install or an apply; and of one that a key signed which a later release retired.
const UNSIGNED = " is not signed by a release key this computer trusts: nothing was installed, and Surogate Desktop stays at its version";
const MISSED = `${UNSIGNED}. If Surogate's release key has changed since this computer's last update, run Surogate Desktop's install script with --version of the release that brought the new key`;
const RETIRED = " is signed by a release key that Surogate has retired, which this computer no longer trusts: nothing was installed, and Surogate Desktop stays at its version";
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
      url: `releases/${version}/surogate-desktop-${version}-linux-x64.tar.gz`, sha256: sha256(readFileSync(tarball)), size: statSync(tarball).size, stateSchema: 1,
      ...fields,
    })}\n`);
    writeFileSync(join(it.dir, "manifest.json"), manifest);
    writeFileSync(join(it.dir, "manifest.json.sig"), sign(null, manifest, key));
    return manifest;
  };
  const current = () => root("readlink /opt/surogate/current").stdout.trim();
  const versions = () => root("ls /opt/surogate/versions").stdout.trim().split("\n").filter(Boolean);
  const standing = () => root(STANDING).stdout;
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

  return { it, docker, root, as, releaseOf, manifestOf, current, versions, standing, swapped, elsewhere };
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

describe("the install script's packages", () => {
  it("names each program that the app starts by a path of the system's: pkexec, for an update, and perl, with which it starts a child that has none of its descriptors", () => {
    const asked = /apt-get install [^\n]*/.exec(readFileSync(SCRIPT, "utf8"))?.[0].split(" ") ?? [];
    expect(asked).toEqual(expect.arrayContaining(["pkexec", "perl-base"]));
  });

  it("names certutil's package in a line of its own, which only a computer that keeps a company's CA runs", () => {
    const lines = readFileSync(SCRIPT, "utf8").split("\n").filter((line) => line.includes("apt-get install"));
    expect(lines.filter((line) => line.includes("libnss3-tools"))).toEqual(['    apt-get install -y -qq -o DPkg::Lock::Timeout=300 libnss3-tools || fail "could not install libnss3-tools, which holds certutil"']);
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

describe("the install script's refusals that only a removal mends", () => {
  it("are each said in the README as the script says them, and the README says none that the script does not", () => {
    const script = readFileSync(SCRIPT, "utf8");
    // The two files that these refusals name, as the script's own settings place them.
    const placed = spawnSync("bash", ["-c", `. <(sed '$d' "$1") && settings && echo "$HELPER" && echo "$HELPER_MARK"`, "_", SCRIPT], { encoding: "utf8" }).stdout.trim().split("\n");
    expect(placed).toEqual(["/opt/surogate/bin/surogate-apply-update", "/opt/surogate/bin/release.json"]);
    const [helper = "", mark = ""] = placed;
    // Each sentence of the script's that ends in the removal, with those two files' names in it.
    const said = [...new Set([...script.matchAll(/"([^"\n]*: remove Surogate Desktop with --uninstall, and install it again)"/g)]
      .map((match) => (match[1] ?? "").replaceAll("$HELPER_MARK", mark).replaceAll("$HELPER", helper)))].sort();
    expect(said.length).toBe(4);
    for (const sentence of said) expect(sentence).not.toContain("$");
    // The README's section quotes each on a line of its own, and no other line of that kind.
    const readme = readFileSync(fileURLToPath(new URL("../README.md", import.meta.url)), "utf8");
    const section = readme.slice(readme.indexOf("\n## When Surogate Desktop says to remove it and install it again\n"));
    expect(section.startsWith("\n## ")).toBe(true);
    const quoted = [...new Set(section.split("\n").filter((line) => line.startsWith("    /opt/") && line.includes("--uninstall")).map((line) => line.trim()))].sort();
    expect(quoted).toEqual(said);
    // And it says what to do, in the install script's own two commands.
    expect(section).toContain("    curl -fsSL https://surogate.ai/desktop/install.sh | bash -s -- --uninstall\n    curl -fsSL https://surogate.ai/desktop/install.sh | bash\n");
  });
});

describe("the install script's reader of one JSON object", () => {
  it("reads by no bound but a number of bytes in the ten digits, from 1 to a megabyte: its bound goes into the shell's own arithmetic, where any other word is a command", () => {
    const dir = mkdtempSync(join(tmpdir(), "install-test-"));
    try {
      const object = join(dir, "object.json");
      writeFileSync(object, '{"a":1}\n');
      // A locale of this computer's in which bash takes other characters than the ten for digits, where it has one.
      const locales = spawnSync("locale", ["-a"], { encoding: "utf8" }).stdout.trim().split("\n");
      const wide = locales.find((locale) => spawnSync("bash", ["-c", '[[ "$1" =~ ^[0-9]$ ]]', "_", "٣"], { env: { ...process.env, LC_ALL: locale } }).status === 0) ?? "C";
      // The reader, from the script's functions without its last line, with *bound* as its third argument where one is given.
      const read = (...bound: string[]) => spawnSync("bash", ["-c", `cd "$3" && . <(sed '$d' "$1") && settings && one_object "$2" one "\${@:4}"`, "_", SCRIPT, object, dir, ...bound], {
        encoding: "utf8", env: { ...process.env, LC_ALL: wide },
      });
      // With none, a manifest's 4096 bytes; and each bound from the object's own 8 bytes to a megabyte.
      for (const bound of [[], ["8"], ["4096"], ["1048576"]]) expect(read(...bound), bound.join()).toMatchObject({ status: 0, stdout: '{"a":1}\n', stderr: "" });
      // A bound below the object's bytes is a bound, and the object is longer.
      for (const bound of ["1", "7"]) expect(read(bound), bound).toMatchObject({ stdout: "", stderr: "" });
      expect(read("7").status).not.toBe(0);
      // What is no such number is refused before it is reckoned with: in arithmetic a name is a
      // variable's, and what stands in its brackets is run.
      const ran = join(dir, "ran");
      for (const bound of ["0", "08", "+8", "-8", " 8", "8 ", "8.0", "1e3", "0x10", "4096+1", "1048577", "9999999", "99999999999999999999", "٨", "4٠٩٦",
        "most", "x[$(touch ran)]", "a[`touch ran`]", "$(touch ran)", "8;touch ran"]) {
        expect(read(bound), bound).toMatchObject({ status: 1, stdout: "", stderr: "" });
        expect(existsSync(ran), bound).toBe(false);
      }
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});

for (const release of RELEASES) describe.skipIf(!ENABLED)(`the install script's --apply, on Ubuntu ${release}`, { timeout: 120_000 }, () => {
  const { it: box, docker, root, as, releaseOf, manifestOf, current, versions, standing, swapped, elsewhere } = lab(release, APPLY_LAB, OWN_DISK);
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
    // Its last line is where no stop was made any more. How far the stops reach is not read from
    // that number: each test that uses them asks for the states it must have seen.
    expect(lines.pop()).toMatch(/^end \d+$/);
    expect(lines.length).toBeGreaterThan(0);
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
      // A part with a zero before it is no version: dpkg reads 1.0.00 as 1.0.0, a second spelling of one release.
      { version: "1.0.00", url: "releases/1.0.00/surogate-desktop-1.0.00-linux-x64.tar.gz" }, { version: "01.0.0", url: "releases/01.0.0/surogate-desktop-01.0.0-linux-x64.tar.gz" },
      // Its tarball's size is a whole number of bytes, above 0 and below 10^15, or it is no release: undefined leaves the field out.
      { size: undefined }, { size: null }, { size: 0 }, { size: -1 }, { size: 1.5 }, { size: "4096" }, { size: [4096] }, { size: 1e15 }, { size: 1e300 },
      // Its state schema is a whole number from 1 and below 10^15, or it is no release.
      { stateSchema: undefined }, { stateSchema: null }, { stateSchema: 0 }, { stateSchema: -1 }, { stateSchema: 1.5 }, { stateSchema: "1" }, { stateSchema: [1] }, { stateSchema: 1e15 }]) {
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
    // Digits are a number, and never a user's name, where a user is so named, as a company's
    // directory may name one: the reader is the user of that number, who is refused the files of
    // the user of that name, as their own login is.
    expect(root(`echo "${user}:x:1600:1600::/nonexistent:/bin/sh" >>/etc/passwd && mkdir -m 700 /srv/numbered && cp ${files()} /srv/numbered/ && chown -R 1600 /srv/numbered`).status).toBe(0);
    const numbered = root(`PKEXEC_UID=${user} /opt/surogate-test/install.sh --apply /srv/numbered/manifest.json /srv/numbered/manifest.json.sig /srv/numbered/release.tar.gz; said=$?; sed -i '$d' /etc/passwd; exit $said`);
    expect(numbered).toMatchObject({ status: 1, stdout: "", stderr: "Surogate Desktop: /srv/numbered/manifest.json cannot be read by tester: name it by its whole path, in a folder of that user's own\n" });
    expect(root("test -e /opt/surogate/current").status).toBe(1);
    // So it is with a release kept in root's home, as a root shell that sudo started would name one;
    // with a name that is no whole path, which under pkexec is looked for in root's home; and with
    // a file that is there for root and missing for the user.
    expect(root(`rm -rf /root/kept && mkdir -m 700 /root/kept && cp ${files()} /root/kept/`).status).toBe(0);
    expect(root(`SUDO_UID=${user} /opt/surogate-test/install.sh --apply /root/kept/manifest.json /root/kept/manifest.json.sig /root/kept/release.tar.gz`))
      .toMatchObject({ status: 1, stdout: "", stderr: `Surogate Desktop: /root/kept/manifest.json ${theirs}` });
    expect(root(`cd /root/kept && PKEXEC_UID=${user} /opt/surogate-test/install.sh --apply manifest.json manifest.json.sig release.tar.gz`))
      .toMatchObject({ status: 1, stdout: "", stderr: `Surogate Desktop: manifest.json ${theirs}` });
    expect(root(`PKEXEC_UID=${user} /opt/surogate-test/install.sh --apply /home/tester/manifest.json /root/kept/manifest.json.sig /home/tester/release.tar.gz`))
      .toMatchObject({ status: 1, stdout: "", stderr: `Surogate Desktop: /root/kept/manifest.json.sig ${theirs}` });
    // Nor does who reads have a group that the system's own list does not give that user: a
    // folder for a group the user is not in, root's shadow file, and a file of root's alone.
    expect(root(`rm -rf /srv/closed && (getent group closed >/dev/null || groupadd closed) && mkdir -m 770 /srv/closed && cp ${files()} /srv/closed/ && chgrp -R closed /srv/closed && chmod 640 /srv/closed/*`
      + " && ! id -Gn tester | grep -qw closed").status).toBe(0);
    expect(root(`PKEXEC_UID=${user} /opt/surogate-test/install.sh --apply /srv/closed/manifest.json /srv/closed/manifest.json.sig /srv/closed/release.tar.gz`))
      .toMatchObject({ status: 1, stdout: "", stderr: `Surogate Desktop: /srv/closed/manifest.json ${theirs}` });
    expect(root(`PKEXEC_UID=${user} /opt/surogate-test/install.sh --apply /etc/shadow /home/tester/manifest.json.sig /home/tester/release.tar.gz`))
      .toMatchObject({ status: 1, stdout: "", stderr: `Surogate Desktop: /etc/shadow ${theirs}` });
    // A file of root's that root's own group reads: who reads has the user's group, and none of root's.
    expect(root(`rm -rf /srv/roots && mkdir -m 755 /srv/roots && cp ${files()} /srv/roots/ && chown -R root:root /srv/roots && chmod 640 /srv/roots/* && id -G`).stdout).toBe("0\n");
    expect(root(`PKEXEC_UID=${user} /opt/surogate-test/install.sh --apply /srv/roots/manifest.json /srv/roots/manifest.json.sig /srv/roots/release.tar.gz`))
      .toMatchObject({ status: 1, stdout: "", stderr: `Surogate Desktop: /srv/roots/manifest.json ${theirs}` });
    expect(root(`chmod 600 /srv/roots/* && PKEXEC_UID=${user} /opt/surogate-test/install.sh --apply /srv/roots/manifest.json /srv/roots/manifest.json.sig /srv/roots/release.tar.gz`))
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
    // most: as that user alone, in their own group and the others the system's list gives them.
    const user = Number(root("id -u tester").stdout);
    const traced = root(`PKEXEC_UID=${user} strace -f -qq -v -s 300 -o /tmp/trace -e trace=execve /opt/surogate-test/install.sh --apply ${files()} >/dev/null && grep -F 'execve("/usr/bin/timeout"' /tmp/trace`);
    expect(traced.status, traced.stderr).toBe(0);
    const asked = traced.stdout.trim().split("\n").map((line) => [...(/\[(.*)\], \[/.exec(line)?.[1] ?? "").matchAll(/"((?:[^"\\]|\\.)*)"/g)].map((match) => match[1]).join(" "));
    // The reader's own line of perl, as the script's settings have it, handed the user's number, their group's and their groups'.
    const line = /^ {2}AS_READER='([^'\n]+)'$/m.exec(readFileSync(SCRIPT, "utf8"))?.[1];
    expect(line).toMatch(/^use POSIX \(\); /);
    const as = `/usr/bin/perl -e ${line} ${user} ${user} ${root("id -G tester").stdout.trim().replaceAll(" ", ",")}`;
    const read = "iflag=nofollow,nonblock bs=64K status=none";
    expect(asked).toEqual([
      // Root's own three questions to the system's list of users and groups, each under the same bound.
      `timeout --foreground -s KILL 5 getent passwd ${user}`,
      "timeout --foreground -s KILL 5 id -u -- tester",
      "timeout --foreground -s KILL 5 id -G -- tester",
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

  it("reads as the asking user's own login would, with the groups the system's own list gives that user and no other: an update is applied from a cache home that the user reaches through one of them", () => {
    const tarball = releaseOf("1.0.0");
    manifestOf("1.0.0", tarball);
    stage(tarball);
    const user = "$(id -u tester)";
    const noUser = { status: 1, stdout: "", stderr: "Surogate Desktop: PKEXEC_UID names no user of this computer\n" };
    // A department's folder, which its group alone may enter, and the user's home in it: their
    // cache home is their own, and they reach it as a member of that group and in no other way.
    const updates = "/data/shared/tester/.cache/surogate/updates";
    expect(root("(getent group shared >/dev/null || groupadd shared) && gpasswd -a tester shared >/dev/null && rm -rf /data && mkdir -p /data/shared && chown root:shared /data/shared && chmod 770 /data/shared"
      + ` && mkdir -p ${updates} && cp ${files()} ${updates}/ && chown -R tester: /data/shared/tester && chmod -R go= /data/shared/tester`).status).toBe(0);
    const shared = `${updates}/manifest.json ${updates}/manifest.json.sig ${updates}/release.tar.gz`;
    // The user reads the file themselves, as their login does; and one who is in no such group does not.
    expect(root(`(id other >/dev/null 2>&1 || useradd -m other) && runuser -u tester -- cat ${updates}/manifest.json >/dev/null && ! runuser -u other -- cat ${updates}/manifest.json 2>/dev/null`).status).toBe(0);
    for (const asked of [`PKEXEC_UID=${user}`, `SUDO_UID=${user}`]) {
      expect(root("find /opt/surogate -mindepth 1 -delete").status, asked).toBe(0);
      expect(root(`${asked} /opt/surogate-test/install.sh --apply ${shared}`), asked).toMatchObject({ status: 0, stdout: "Surogate Desktop: 1.0.0 is installed\n", stderr: "" });
    }
    expect(root("find /opt/surogate -mindepth 1 -delete").status).toBe(0);
    // A user of the computer who is not in the group is refused the same files, in the same words as ever.
    expect(root(`PKEXEC_UID=$(id -u other) /opt/surogate-test/install.sh --apply ${shared}`))
      .toMatchObject({ status: 1, stdout: "", stderr: `Surogate Desktop: ${updates}/manifest.json cannot be read by other: name it by its whole path, in a folder of that user's own\n` });
    // Who reads, by the script's own functions without its last line: the user, their own group, and that group; none of root's.
    const reader = (asked: string) => root(`${asked} bash -c '. <(sed "\\$d" /opt/surogate-test/install.sh) && settings && asker && as_reader 5 id -G'`);
    expect(reader(`PKEXEC_UID=${user}`).stdout).toBe(root("id -G tester").stdout);
    expect(root("id -G tester").stdout.trim().split(" ")).toEqual([root("id -u tester").stdout.trim(), root("getent group shared | cut -d: -f3").stdout.trim()]);
    // Started by a root that has groups of its own, as sudo's root has: none of them is the reader's.
    expect(root(`(getent group closed >/dev/null || groupadd closed) && PKEXEC_UID=${user} setpriv --groups 0,$(getent group closed | cut -d: -f3),$(getent group shadow | cut -d: -f3) `
      + `bash -c '. <(sed "\\$d" /opt/surogate-test/install.sh) && settings && asker && as_reader 5 id -G'`).stdout).toBe(root("id -G tester").stdout);
    // The groups are handed on as numbers and set as numbers. A group may be named in digits, as a
    // directory may hold one, and that name be another group's number: the reader is in the group
    // of that number, which the user is in, and not in the group of that name, which they are not.
    expect(root("groupadd -g 4242 wing && gpasswd -a tester wing >/dev/null && echo '4242:x:5555:' >>/etc/group && getent group 4242 5555 | cut -d: -f1,3 | tr '\n' ' '").stdout).toBe("wing:4242 4242:5555 ");
    try {
      expect(root("id -G tester").stdout.trim().split(" ")).toContain("4242");
      expect(reader(`PKEXEC_UID=${user}`).stdout).toBe(root("id -G tester").stdout);
      // What the group of that name alone may read is not the reader's to read, as it is not the user's own login's.
      expect(root("printf closed >/srv/five && chown root:5555 /srv/five && chmod 640 /srv/five && ! runuser -u tester -- cat /srv/five 2>/dev/null").status).toBe(0);
      expect(root(`PKEXEC_UID=${user} bash -c '. <(sed "\\$d" /opt/surogate-test/install.sh) && settings && asker && as_reader 5 cat /srv/five'`)).toMatchObject({ status: 1, stdout: "" });
      // And what the group of that number alone may read is.
      expect(root(`chgrp wing /srv/five && PKEXEC_UID=${user} bash -c '. <(sed "\\$d" /opt/surogate-test/install.sh) && settings && asker && as_reader 5 cat /srv/five'`)).toMatchObject({ status: 0, stdout: "closed" });
      // The reader is that user in all three of a process's numbers and all three of its groups': nothing of root's is kept to go back to.
      expect(root(`PKEXEC_UID=${user} bash -c '. <(sed "\\$d" /opt/surogate-test/install.sh) && settings && asker && as_reader 5 grep -E "^[UG]id:" /proc/self/status' | tr -s '\t ' ' '`).stdout)
        .toBe(`Uid: ${Array(4).fill(root("id -u tester").stdout.trim()).join(" ")}\nGid: ${Array(4).fill(root("id -g tester").stdout.trim()).join(" ")}\n`);
    } finally {
      root("sed -i '/^4242:x:5555:$/d' /etc/group; gpasswd -d tester wing >/dev/null; groupdel wing; rm -f /srv/five");
    }
    // The groups the reader has are the ones root was told, handed on, and never ones looked up
    // again as the reader is made: here an id that tells root of the user's own group alone.
    // The reader is then in that group alone, by the system's own id.
    const fewer = '#!/bin/sh\n[ "$1" != -G ] || exec /opt/hold/id -g -- "$3"\nexec /opt/hold/id "$@"\n';
    expect(swapped("id", fewer, [`PKEXEC_UID=${user} bash -c '. <(sed "\\$d" /opt/surogate-test/install.sh) && settings && asker && as_reader 5 /opt/hold/id -G'`]).stdout).toBe(root("id -g tester").stdout);
    expect(root("id -G tester").stdout).not.toBe(root("id -g tester").stdout);
    // Two names of one number, as a directory may have: the list gives the first name for the
    // number, and the reader is in that name's groups, whichever of the two asked. What the second
    // name's own group alone may read is not read; to the kernel the two are one user.
    expect(root(`echo "twin:x:${user}:${user}::/nonexistent:/bin/sh" >>/etc/passwd && groupadd twins && gpasswd -a twin twins >/dev/null && printf closed >/srv/twins && chgrp twins /srv/twins && chmod 640 /srv/twins`).status).toBe(0);
    try {
      expect(root("getent passwd $(id -u tester) | cut -d: -f1; id -Gn twin | tr ' ' '\n' | grep -c twins").stdout).toBe("tester\n1\n");
      expect(reader(`PKEXEC_UID=${user}`).stdout).toBe(root("id -G tester").stdout);
      expect(root(`PKEXEC_UID=${user} bash -c '. <(sed "\\$d" /opt/surogate-test/install.sh) && settings && asker && as_reader 5 cat /srv/twins'`)).toMatchObject({ status: 1, stdout: "" });
    } finally {
      root("sed -i '/^twin:x:/d' /etc/passwd; groupdel twins; rm -f /srv/twins");
    }
    // Where the reader's groups cannot be asked at all, here with an id that answers nothing of
    // groups, none is taken on trust: the reader is refused.
    const silent = '#!/bin/sh\n[ "$1" != -G ] || exit 1\nexec /opt/hold/id "$@"\n';
    expect(swapped("id", silent, [`PKEXEC_UID=${user} /opt/surogate-test/install.sh --apply ${shared}; echo "ended $?"`]))
      .toMatchObject({ stdout: "ended 1\n", stderr: "Surogate Desktop: PKEXEC_UID names no user of this computer\n" });
    expect(root("test ! -e /opt/surogate/current").status).toBe(0);
    // A number with no user of the computer, and one with a user under another's name, as a
    // directory gone wrong may have: that one's list would give the second user the first's groups.
    // Each is refused in the script's own words, and reads nothing.
    expect(root(`PKEXEC_UID=4242 /opt/surogate-test/install.sh --apply ${shared}`)).toMatchObject(noUser);
    expect(root(`echo "tester:x:1700:1700::/nonexistent:/bin/sh" >>/etc/passwd && chmod -R g+rX /data/shared/tester && chgrp -R shared /data/shared/tester`).status).toBe(0);
    try {
      // Read by setpriv alone, the second user does read the first's group's file.
      expect(root(`setpriv --reuid 1700 --regid 1700 --init-groups cat ${updates}/manifest.json >/dev/null`).status).toBe(0);
      expect(root(`PKEXEC_UID=1700 /opt/surogate-test/install.sh --apply ${shared}`)).toMatchObject(noUser);
      expect(reader("PKEXEC_UID=1700")).toMatchObject({ ...noUser, stdout: "" });
    } finally {
      root("sed -i '$d' /etc/passwd");
    }
    // Nor is the second user taken where its own group is the first's, or one of the first's
    // others: the name's number is the first's, whatever its groups.
    for (const group of ["$(id -g tester)", "$(getent group shared | cut -d: -f3)"]) {
      expect(root(`echo "tester:x:1700:${group}::/nonexistent:/bin/sh" >>/etc/passwd`).status, group).toBe(0);
      try {
        expect(root(`PKEXEC_UID=1700 /opt/surogate-test/install.sh --apply ${shared}`), group).toMatchObject(noUser);
        expect(reader("PKEXEC_UID=1700"), group).toMatchObject({ ...noUser, stdout: "" });
      } finally {
        root("sed -i '$d' /etc/passwd");
      }
    }
    // A list that is slow to say a user's groups is said to be that, within its own bound, and
    // is no user that does not exist; and it takes nothing from the time a reader has to read.
    const slow = (asked: string) => `#!/bin/sh\n[ "$1" != ${asked} ] || exec /opt/hold/sleep 8\nexec /opt/hold/id "$@"\n`;
    expect(root("mkdir -p /opt/hold && cp -L /usr/bin/sleep /opt/hold/sleep").status).toBe(0);
    const number = root("id -u tester").stdout.trim();
    for (const asked of ["-G", "-u"]) {
      const began = Date.now();
      expect(swapped("id", slow(asked), [`PKEXEC_UID=${number} /opt/surogate-test/install.sh --apply ${shared}; echo "ended $?"`]), asked)
        .toMatchObject({ stdout: "ended 1\n", stderr: "Surogate Desktop: this computer's list of users and groups did not answer within 5 seconds: try again\n" });
      expect(Date.now() - began, asked).toBeLessThan(7_500);
    }
    expect(root("test ! -e /opt/surogate/current").status).toBe(0);
    // A user and a group whose names have a space in them are a user and a group: read as that
    // user, in that group, and in no other.
    expect(root(`echo "o dd:x:1701:1701::/nonexistent:/bin/sh" >>/etc/passwd && echo "sha red:x:1801:o dd" >>/etc/group && rm -rf /srv/odd && mkdir -m 750 /srv/odd && cp ${files()} /srv/odd/ && chgrp -R 1801 /srv/odd && chmod 640 /srv/odd/*`).status).toBe(0);
    try {
      expect(reader("PKEXEC_UID=1701").stdout).toBe("1701 1801\n");
      expect(root("PKEXEC_UID=1701 /opt/surogate-test/install.sh --apply /srv/odd/manifest.json /srv/odd/manifest.json.sig /srv/odd/release.tar.gz"))
        .toMatchObject({ status: 0, stdout: "Surogate Desktop: 1.0.0 is installed\n", stderr: "" });
      expect(root(`PKEXEC_UID=1701 /opt/surogate-test/install.sh --apply ${shared}`))
        .toMatchObject({ status: 1, stdout: "", stderr: `Surogate Desktop: ${updates}/manifest.json cannot be read by o\\ dd: name it by its whole path, in a folder of that user's own\n` });
    } finally {
      root("sed -i '$d' /etc/passwd /etc/group");
    }
    // The removal's own parts that run as the user have that user's groups too, as runuser gives them.
    expect(root("runuser -u tester -- id -G").stdout).toBe(root("id -G tester").stdout);
  });

  it("clears what a killed apply left in staging before it measures the room", () => {
    const tarball = releaseOf("1.0.0");
    manifestOf("1.0.0", tarball);
    // As an apply killed while it copied leaves it, here with all the room the disk had.
    expect(root("mkdir -p /opt/surogate/staging/apply.killed && head -c 1G /dev/zero >/opt/surogate/staging/apply.killed/release.tar.gz; df --output=avail -k /opt/surogate | tail -n 1").stdout.trim()).toBe("0");
    expect(apply(tarball)).toMatchObject({ status: 0, stdout: "Surogate Desktop: 1.0.0 is installed\n" });
    expect(root("ls -A /opt/surogate/staging").stdout).toBe("");
  });

  it("says that a release is installed, and ends 0, only once nothing it started is still running as root: the app starts again at that", () => {
    const tarball = releaseOf("1.0.0");
    manifestOf("1.0.0", tarball);
    stage(tarball);
    expect(root("find /opt/surogate -mindepth 1 -delete 2>/dev/null; rm -f /tmp/left /tmp/ended").status).toBe(0);
    // A tool of the system's that leaves a program of its own behind, with all the helper had open:
    // here sync, which then goes on for three seconds, as a package's hook or a tool's own helper may.
    const leaving = (seconds: number) => `#!/bin/sh
/opt/hold/sync "$@" || exit
[ -e /tmp/left ] || { : >/tmp/left; (/usr/bin/sleep ${seconds}; : >/tmp/ended) 2>/dev/null & }
exit 0
`;
    const waited = swapped("sync", leaving(3), [`/opt/surogate-test/install.sh --apply ${files()}; echo "ended $? $(test -e /tmp/ended && echo after || echo before)"`]);
    expect(waited).toMatchObject({ stdout: "Surogate Desktop: 1.0.0 is installed\nended 0 after\n", stderr: "" });
    // One that does not end is waited for a bounded while, and the helper then does not end 0: the
    // release is in place, and whoever started the helper is told what still runs.
    expect(root("rm -f /tmp/left /tmp/ended").status).toBe(0);
    const bound = Number(root(`bash -c '. <(sed "\\$d" /opt/surogate-test/install.sh) && settings && echo "$LEFT_WAIT"'`).stdout);
    expect(bound).toBe(30);
    const began = Date.now();
    const left = swapped("sync", leaving(300), [`/opt/surogate-test/install.sh --apply ${files()}; echo "ended $?"; pkill -x sleep; true`]);
    expect(left).toMatchObject({ stdout: "ended 1\n", stderr: "Surogate Desktop: 1.0.0 is in place, and something that its update started as root is still running after 30 seconds: start Surogate again once that has ended\n" });
    expect(Date.now() - began).toBeGreaterThan(29_000);
    expect(current()).toBe("/opt/surogate/versions/1.0.0");
    // And the next apply is not held by what an earlier one left, once that has ended.
    expect(root(`/opt/surogate-test/install.sh --apply ${files()}`)).toMatchObject({ status: 0, stdout: "Surogate Desktop: 1.0.0 is installed\n", stderr: "" });
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

  it("applies a release signed by either key of a rotation's list, the script's own at a first install and the computer's helper's after it, and refuses one signed by neither", () => {
    // A rotating release's script, and its helper: the old key and the new.
    const rotating = withKeys(readFileSync(SCRIPT, "utf8"), [PUBLIC, pem(next.publicKey)]);
    writeFileSync(join(box.dir, "rotating.sh"), rotating, { mode: 0o755 });
    expect(docker(["cp", join(box.dir, "rotating.sh"), `${box.container}:/opt/surogate-test/rotating.sh`]).status).toBe(0);
    const both = (top: string) => writeFileSync(join(top, "bin", "surogate-apply-update"), rotating, { mode: 0o755 });
    // The first by the script's own list, on a computer with no helper yet. The second by the list of
    // the helper the first left, though the script that applies it lists the old key alone.
    for (const [version, key, script] of [["1.0.0", keys, "rotating.sh"], ["1.1.0", next, "install.sh"]] as const) {
      const tarball = releaseOf(version, both);
      manifestOf(version, tarball, {}, key.privateKey);
      expect(apply(tarball, script), version).toMatchObject({ status: 0, stdout: `Surogate Desktop: ${version} is installed\n` });
    }
    const tarball = releaseOf("1.2.0");
    manifestOf("1.2.0", tarball, {}, other.privateKey);
    expect(apply(tarball, "rotating.sh")).toMatchObject({ status: 1, stderr: `Surogate Desktop: the release's manifest${MISSED}\n` });
    expect(current()).toBe("/opt/surogate/versions/1.1.0");
  });

  it("takes the release keys of no helper that is not as an apply leaves it, and puts none in the place of one whose mark is gone", () => {
    const tarball = releaseOf("1.0.0");
    manifestOf("1.0.0", tarball);
    expect(apply(tarball).status).toBe(0);
    const helper = "/opt/surogate/bin/surogate-apply-update";
    const again = "remove Surogate Desktop with --uninstall, and install it again\n";
    const damaged: Array<[string, string]> = [
      // One that others may write, one that is a link, and one that is another user's: none is root's own word.
      [`chmod 775 ${helper}`, `Surogate Desktop: ${helper} is not as Surogate Desktop's install leaves it: ${again}`],
      [`mv ${helper} /root/helper && ln -s /root/helper ${helper}`, `Surogate Desktop: ${helper} is not as Surogate Desktop's install leaves it: ${again}`],
      [`chown tester ${helper}`, `Surogate Desktop: ${helper} is not as Surogate Desktop's install leaves it: ${again}`],
      // One that no one may run, and a folder in its place: pkexec could run neither.
      [`chmod 644 ${helper}`, `Surogate Desktop: ${helper} is not as Surogate Desktop's install leaves it: ${again}`],
      [`rm ${helper} && mkdir ${helper}`, `Surogate Desktop: ${helper} is not as Surogate Desktop's install leaves it: ${again}`],
      // One that lists no key: this script's own list does not stand in for it.
      [`sed -i '/BEGIN PUBLIC KEY/,/END PUBLIC KEY/d' ${helper}`, `Surogate Desktop: ${helper} lists no release key: ${again}`],
      // One whose mark is gone, or names no release: which release it is of is not known, so nothing replaces it.
      ["rm /opt/surogate/bin/release.json", `Surogate Desktop: /opt/surogate/bin/release.json does not say which release ${helper} is of: ${again}`],
      ["echo '{}' >/opt/surogate/bin/release.json", `Surogate Desktop: /opt/surogate/bin/release.json does not say which release ${helper} is of: ${again}`],
      // A link to nothing in the helper's place is no computer without a helper, where this script's own list would count.
      [`rm ${helper} && ln -s /nowhere ${helper}`, `Surogate Desktop: ${helper} is not as Surogate Desktop's install leaves it: ${again}`],
      // Nor is a version that has lost its helper, or the helper's folder with it, at its first
      // install: an apply puts the helper in before it switches to a version, so only root can
      // have left one so, and the list of whichever script asks next is trusted for nothing.
      [`rm ${helper}`, `Surogate Desktop: ${helper} is not as Surogate Desktop's install leaves it: ${again}`],
      ["rm -rf /opt/surogate/bin", `Surogate Desktop: ${helper} is not as Surogate Desktop's install leaves it: ${again}`],
      // A mark that is another user's, or that others may write, is not root's own word for which release.
      ["chown tester /opt/surogate/bin/release.json", `Surogate Desktop: /opt/surogate/bin/release.json does not say which release ${helper} is of: ${again}`],
      ["chmod 664 /opt/surogate/bin/release.json", `Surogate Desktop: /opt/surogate/bin/release.json does not say which release ${helper} is of: ${again}`],
    ];
    // The installed release again, and an update: each is refused, and leaves the installed
    // version, the helper, its mark and the keys it lists the bytes they were.
    const update = releaseOf("1.1.0");
    manifestOf("1.1.0", update);
    expect(root("mkdir /home/tester/update").status).toBe(0);
    for (const [from, to] of [[join(box.dir, "manifest.json"), "manifest.json"], [join(box.dir, "manifest.json.sig"), "manifest.json.sig"], [update, "release.tar.gz"]] as const) {
      expect(docker(["cp", from, `${box.container}:/home/tester/update/${to}`]).status).toBe(0);
    }
    const updated = "/home/tester/update/manifest.json /home/tester/update/manifest.json.sig /home/tester/update/release.tar.gz";
    for (const [damage, said] of damaged) {
      expect(root(`rm -rf /opt/kept && cp -a /opt/surogate/bin /opt/kept && ${damage}`).status, damage).toBe(0);
      const before = standing();
      expect(root(`/opt/surogate-test/install.sh --apply ${files()}`), damage).toMatchObject({ status: 1, stdout: "", stderr: said });
      expect(root(`/opt/surogate-test/install.sh --apply ${updated}`), `an update: ${damage}`).toMatchObject({ status: 1, stdout: "", stderr: said });
      expect(standing(), damage).toBe(before);
      expect(root("ls -A /opt/surogate/staging").stdout, damage).toBe("");
      expect(root("rm -rf /opt/surogate/bin && cp -a /opt/kept /opt/surogate/bin").status, damage).toBe(0);
    }
    // At any mode that leaves it root's alone to write, and a program, the helper is root's own
    // word, as it is to the app, which offers an update by the same rule: an administrator may
    // have closed it to others. The same release again is applied, and the helper is as an apply
    // leaves one.
    // Every mode of the list that the app's own rule is asked with (helper-modes.ts), with each
    // set-id and sticky bit, in one run of the container: who may write the helper and whether
    // it is a program, and no more.
    const asked = HELPER_MODES.map(([mode]) => mode.toString(8));
    const answers = root(`for mode in ${asked.join(" ")}; do chmod 755 ${helper} && chmod "$mode" ${helper} || exit 9; [ "$(stat -c %a ${helper})" = "$mode" ] || exit 8; /opt/surogate-test/install.sh --apply ${files()} >/tmp/said 2>&1; echo "$mode $? $(cat /tmp/said) $(stat -c '%a %U' ${helper})"; done`);
    expect(answers.status, answers.stderr).toBe(0);
    const NOT_LEFT = `Surogate Desktop: ${helper} is not as Surogate Desktop's install leaves it: remove Surogate Desktop with --uninstall, and install it again`;
    expect(answers.stdout.trimEnd().split("\n")).toEqual(HELPER_MODES.map(([mode, answer]) =>
      answer === "taken" ? `${mode.toString(8)} 0 Surogate Desktop: 1.0.0 is installed 755 root` : `${mode.toString(8)} 1 ${NOT_LEFT} ${mode.toString(8)} root`));
    expect(root(`chmod 755 ${helper}`).status).toBe(0);
    // Nor is a link to nothing no helper where the release it is of is asked by itself, from the
    // script's functions without its last line: it is a helper, and here one with no mark.
    expect(root(`rm ${helper} /opt/surogate/bin/release.json && ln -s /nowhere ${helper} && bash -c '. <(sed "\\$d" /opt/surogate-test/install.sh) && settings && helper_release'`))
      .toMatchObject({ status: 1, stdout: "" });
    expect(root("rm -rf /opt/surogate/bin && cp -a /opt/kept /opt/surogate/bin").status).toBe(0);
    expect(root(`/opt/surogate-test/install.sh --apply ${files()}`)).toMatchObject({ status: 0, stdout: "Surogate Desktop: 1.0.0 is installed\n" });
  });

  it("takes a manifest, the helper's mark and the installed version's only as one JSON object on one line, of 4096 bytes at most: of two documents, each would name a version", () => {
    const tarball = releaseOf("1.0.0");
    const object = manifestOf("1.0.0", tarball).toString().trimEnd();
    expect(apply(tarball).status).toBe(0);
    // The object with more in it, to *size* bytes in all with its newline.
    const padded = (size: number) => `${object.slice(0, -1)},"more":"${"x".repeat(size - object.length - 11)}"}\n`;
    expect(Buffer.byteLength(padded(4096))).toBe(4096);
    const notOne: Array<[string, string]> = [
      ["two documents on one line", `${object}${object}\n`],
      ["two documents, a line each", `${object}\n${object}\n`],
      ["an array", `[${object}]\n`],
      ["a number", "1\n"],
      ["an object over two lines", `${object.replace(",", ",\n")}\n`],
      ["an object without its newline", object],
      ["an empty file", ""],
      ["a byte more than the bound", padded(4097)],
    ];
    const helper = "/opt/surogate/bin/surogate-apply-update";
    const mark = "/opt/surogate/bin/release.json";
    // A manifest the release key signed, handed to the helper with the release's tarball.
    const handed = (manifest: string) => {
      writeFileSync(join(box.dir, "manifest.json"), manifest);
      writeFileSync(join(box.dir, "manifest.json.sig"), sign(null, Buffer.from(manifest), keys.privateKey));
      return apply(tarball);
    };
    // Each is refused as no release. But for two: the longest, as it is copied, in the words of a
    // file that is no manifest's size; and the empty one, which no key's signature is taken for.
    const said = (manifest: string) => {
      if (Buffer.byteLength(manifest) > 4096) return "/home/tester/manifest.json is not a downloaded release's file";
      return manifest === "" ? `the release's manifest${MISSED}` : "the release's manifest is not a release of Surogate Desktop for this computer";
    };
    for (const [what, manifest] of notOne) {
      const before = standing();
      expect(handed(manifest), what).toMatchObject({ status: 1, stdout: "", stderr: `Surogate Desktop: ${said(manifest)}\n` });
      expect(standing(), what).toBe(before);
    }
    // One of the bound's own size is a release, and then the helper's mark and the version's.
    expect(handed(padded(4096))).toMatchObject({ status: 0, stdout: "Surogate Desktop: 1.0.0 is installed\n", stderr: "" });
    expect(root(`cmp /home/tester/manifest.json ${mark} && cmp /home/tester/manifest.json /opt/surogate/current/release.json && stat -c %s ${mark}`).stdout).toBe("4096\n");

    // The helper's mark, as only root can leave it: root's own, and no release's manifest. The
    // release is handed again as it was signed; nothing is compared with what such a mark names.
    const marked = (path: string, written: string) => {
      writeFileSync(join(box.dir, "mark"), written);
      expect(docker(["cp", join(box.dir, "mark"), `${box.container}:/root/mark`]).status).toBe(0);
      expect(root(`install -m 0644 /root/mark ${path}`).status).toBe(0);
    };
    for (const [what, written] of [...notOne, ["an object that names no x.y.z", '{"version":"1.1"}\n'], ["a version with a zero before a part", '{"version":"1.0.00"}\n']] as const) {
      marked(mark, written);
      const before = standing();
      expect(root(`/opt/surogate-test/install.sh --apply ${files()}`), what).toMatchObject({
        status: 1, stdout: "", stderr: `Surogate Desktop: ${mark} does not say which release ${helper} is of: remove Surogate Desktop with --uninstall, and install it again\n`,
      });
      expect(standing(), what).toBe(before);
    }
    // One of the bound's own size that names the release is its mark.
    marked(mark, padded(4096));
    expect(root(`/opt/surogate-test/install.sh --apply ${files()}`)).toMatchObject({ status: 0, stdout: "Surogate Desktop: 1.0.0 is installed\n", stderr: "" });

    // The installed version's own mark, where a rollback asks what the installed version keeps:
    // that question by itself, from the script's functions without its last line.
    const keeps = () => root(`bash -c '. <(sed "\\$d" /opt/surogate-test/install.sh) && settings && reads_state /home/tester/manifest.json 1.0.0'`);
    expect(keeps()).toMatchObject({ status: 0, stdout: "", stderr: "" });
    for (const [what, written] of notOne) {
      marked("/opt/surogate/versions/1.0.0/release.json", written);
      expect(keeps(), what).toMatchObject({ status: 1, stdout: "", stderr: "Surogate Desktop: the installed 1.0.0 names no state schema: run Surogate Desktop's install script again\n" });
    }
  });

  it("takes a state schema to the last below 10^15, and none that reads as below it only as it is written: a number is what it rounds to, and a signing writes it so", () => {
    const tarball = releaseOf("1.0.0");
    const object = manifestOf("1.0.0", tarball).toString().trimEnd();
    expect(object.endsWith('"stateSchema":1}')).toBe(true);
    // A manifest with the number as it is written, signed: this test's own JSON would round it first.
    const withSchema = (schema: string) => {
      const manifest = `${object.slice(0, -2)}${schema}}\n`;
      writeFileSync(join(box.dir, "manifest.json"), manifest);
      writeFileSync(join(box.dir, "manifest.json.sig"), sign(null, Buffer.from(manifest), keys.privateKey));
      return apply(tarball);
    };
    // 999999999999999.99 is 10^15: compared as written, it passed for less.
    for (const schema of ["999999999999999.99", "1000000000000000", "1e15", "0.5", "1.5", "0"]) {
      expect(withSchema(schema), schema).toMatchObject({ status: 1, stdout: "", stderr: "Surogate Desktop: the release's manifest is not a release of Surogate Desktop for this computer\n" });
    }
    expect(root("test -e /opt/surogate/current").status).toBe(1);
    for (const schema of ["999999999999999", "1.0"]) {
      expect(withSchema(schema), schema).toMatchObject({ status: 0, stdout: "Surogate Desktop: 1.0.0 is installed\n" });
    }
  });

  it("takes a manifest as JSON is written, and no more of what jq reads besides: the app reads one with another reader, and takes what the helper takes and nothing else", () => {
    const tarball = releaseOf("1.0.0");
    const object = manifestOf("1.0.0", tarball).toString().trimEnd();
    const size = String(statSync(tarball).size);
    expect(object.endsWith(`"size":${size},"stateSchema":1}`)).toBe(true);
    // A manifest as it is written here, signed: this test's own JSON would write none of these.
    const handed = (manifest: string | Buffer) => {
      writeFileSync(join(box.dir, "manifest.json"), manifest);
      writeFileSync(join(box.dir, "manifest.json.sig"), sign(null, Buffer.from(manifest), keys.privateKey));
      return apply(tarball);
    };
    // The release's manifest with one more field, *text* as it is where its value goes; and with
    // its size as *written*.
    const more = (...text: Array<string | Buffer>) => Buffer.concat([`${object.slice(0, -1)},"more":`, ...text, "}\n"].map((part) => Buffer.from(part)));
    const sized = (written: string) => `${object.replace(`"size":${size},`, `"size":${written},`)}\n`;
    const refused: Array<[string, string | Buffer]> = [
      ["a byte order mark before it", `\uFEFF${object}\n`],
      ["a byte that is no UTF-8", more('"', Buffer.from([0xff]), '"')],
      ["a replacement character", more('"\uFFFD"')],
      ...["+1", "01", "1.", ".5", "nan", "infinity"].map((number): [string, Buffer] => [`a number written ${number}`, more(number)]),
      ["a number in 18 digits", more("123456789012345678")],
      ["a number 65 fields and places down", more("[".repeat(64), "1", "]".repeat(64))],
      // A field named twice, whatever its two values: jq keeps the last, and cannot read a first that is deeper than it reads at all.
      ["a field named twice", more('1,"more":2')],
      ["a field named twice, the first 300 down", more("[".repeat(299), "1", "]".repeat(299), ',"more":1')],
      ["its version named twice", `${object.replace('{"version"', '{"version":"9.9.9","version"')}\n`],
      ["its size with a zero before it", sized(`0${size}`)],
      ["its size with a plus before it", sized(`+${size}`)],
      ["its size in 18 digits", sized(`${size}.${"0".repeat(18 - size.length)}`)],
      // As they are written, the one is above nothing and the other below 10^15.
      ["a size that rounds to nothing", sized("1e-400")],
      ["a size that rounds to 10^15", sized("999999999999999.99")],
    ];
    for (const [what, manifest] of refused) {
      expect(handed(manifest), what).toMatchObject({ status: 1, stdout: "", stderr: "Surogate Desktop: the release's manifest is not a release of Surogate Desktop for this computer\n" });
    }
    expect(root("test -e /opt/surogate/current").status).toBe(1);
    const taken: Array<[string, string | Buffer]> = [
      ["a replacement character's escape", more('"\\ufffd"')],
      ["a number in 17 digits", more("12345678901234567")],
      ["a number 64 fields and places down", more("[".repeat(63), "1", "]".repeat(63))],
      ["its size in 17 digits", sized(`${size}.${"0".repeat(17 - size.length)}`)],
    ];
    for (const [what, manifest] of taken) {
      expect(handed(manifest), what).toMatchObject({ status: 0, stdout: "Surogate Desktop: 1.0.0 is installed\n" });
    }
  });

  it("refuses what is no file where the helper's mark goes, on a computer with no helper and on one that has one, before it switches to any version: a first install is never left with a version and no helper", () => {
    const tarball = releaseOf("1.0.0");
    manifestOf("1.0.0", tarball);
    stage(tarball);
    const helper = "/opt/surogate/bin/surogate-apply-update";
    const mark = "/opt/surogate/bin/release.json";
    const again = "remove Surogate Desktop with --uninstall, and install it again\n";
    // A rename replaces a file, and no folder; a link or a pipe there is no mark an apply left.
    const notFiles: Array<[string, string]> = [
      ["a folder", `mkdir ${mark}`],
      ["a link to a file of root's own", `echo kept >/root/elsewhere && ln -s /root/elsewhere ${mark}`],
      ["a link to nothing", `ln -s /nowhere ${mark}`],
      ["a pipe", `mkfifo ${mark}`],
    ];
    // Bounded: none of them is opened, and a pipe that was would be waited on for good.
    const applied = () => root(`timeout 30 /opt/surogate-test/install.sh --apply ${files()}`);
    // With no helper, as at a first install: nothing is installed, and what stands there is left.
    for (const [what, made] of notFiles) {
      expect(root(`find /opt/surogate -mindepth 1 -delete; mkdir /opt/surogate/bin && ${made}`).status, what).toBe(0);
      const before = standing();
      expect(applied(), what).toMatchObject({ status: 1, stdout: "", stderr: `Surogate Desktop: ${mark} is not as Surogate Desktop's install leaves it: ${again}` });
      expect(standing(), what).toBe(before);
      expect(root(`test ! -e /opt/surogate/current && test ! -e ${helper} && find /opt/surogate/versions /opt/surogate/staging -mindepth 1`), what).toMatchObject({ status: 0, stdout: "" });
    }
    // Nor is a link to nothing where the helper itself goes a computer at its first install,
    // which would take this script's own list of keys: it is no helper an apply left.
    expect(root(`find /opt/surogate -mindepth 1 -delete; mkdir /opt/surogate/bin && ln -s /nowhere ${helper}`).status).toBe(0);
    const linked = standing();
    expect(applied()).toMatchObject({ status: 1, stdout: "", stderr: `Surogate Desktop: ${helper} is not as Surogate Desktop's install leaves it: ${again}` });
    expect(standing()).toBe(linked);
    expect(root("test ! -e /opt/surogate/current && find /opt/surogate/versions /opt/surogate/staging -mindepth 1")).toMatchObject({ status: 0, stdout: "" });
    // With a helper: its mark does not say which release it is of, and neither is replaced.
    for (const [what, made] of notFiles) {
      expect(root("find /opt/surogate -mindepth 1 -delete").status, what).toBe(0);
      expect(applied(), what).toMatchObject({ status: 0, stdout: "Surogate Desktop: 1.0.0 is installed\n" });
      expect(root(`rm ${mark} && ${made}`).status, what).toBe(0);
      const before = standing();
      expect(applied(), what).toMatchObject({ status: 1, stdout: "", stderr: `Surogate Desktop: ${mark} does not say which release ${helper} is of: ${again}` });
      expect(standing(), what).toBe(before);
      expect(root("ls -A /opt/surogate/staging").stdout, what).toBe("");
    }
    // Nothing was written through a link.
    expect(root("cat /root/elsewhere").stdout).toBe("kept\n");
    // A file there with no helper that is not root's own, as anyone left one before the folder
    // was root's: replaced, and never read.
    expect(root(`find /opt/surogate -mindepth 1 -delete; mkdir /opt/surogate/bin && echo '{"version":"9.9.9"}' >${mark} && chown tester ${mark}`).status).toBe(0);
    expect(applied()).toMatchObject({ status: 0, stdout: "Surogate Desktop: 1.0.0 is installed\n" });
    expect(root(`cmp /home/tester/manifest.json ${mark} && stat -c '%a %U' ${mark}`).stdout).toBe("644 root\n");
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

  it("puts the helper pkexec runs back as its release has it, when that release is applied again: a repair mends a helper that only root could have changed", () => {
    const tarball = releaseOf("1.0.0");
    manifestOf("1.0.0", tarball);
    expect(apply(tarball).status).toBe(0);
    const helper = "/opt/surogate/bin/surogate-apply-update";
    // A line more in it: still a file of root's own at its mode, and its list of keys as it was.
    expect(root(`echo '# changed' >>${helper} && ! cmp -s ${helper} /opt/surogate/versions/1.0.0/bin/surogate-apply-update`).status).toBe(0);
    expect(root(`/opt/surogate-test/install.sh --apply ${files()}`)).toMatchObject({ status: 0, stdout: "Surogate Desktop: 1.0.0 is installed\n" });
    expect(root(`cmp ${helper} /opt/surogate/versions/1.0.0/bin/surogate-apply-update && cmp /opt/surogate/bin/release.json /home/tester/manifest.json`).status).toBe(0);
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

  it("puts the helper's mark in before the helper, and both before it switches to the version, wherever an update stops between its renames: the helper is never of a newer release than its mark names, and no version is installed before its helper", () => {
    const first = releaseOf("1.0.0");
    manifestOf("1.0.0", first);
    expect(apply(first).status).toBe(0);
    // The update's own helper lists a second key: it is not the release's before, byte for byte.
    const second = releaseOf("1.1.0", (top) => writeFileSync(join(top, "bin", "surogate-apply-update"), withKeys(readFileSync(SCRIPT, "utf8"), [PUBLIC, pem(next.publicKey)]), { mode: 0o755 }));
    manifestOf("1.1.0", second);
    stage(second);
    writeFileSync(join(box.dir, "renames.sh"), RENAMES);
    expect(docker(["cp", join(box.dir, "renames.sh"), `${box.container}:/opt/surogate-test/renames.sh`]).status).toBe(0);
    expect(root("rm -rf /opt/pristine && cp -a /opt/surogate /opt/pristine").status).toBe(0);
    const lines = root(`bash /opt/surogate-test/renames.sh ${files()}`).stdout.trim().split("\n");
    expect(lines.pop()).toBe("end");
    // Which release the helper may be replaced by is its mark's word: no older one than it names.
    // So the mark is never behind the helper. With the helper first, a stop between the two would
    // leave the update's helper under a mark that still names the release before, and a release
    // between the two could then put an older helper, and its older list of keys, in its place.
    // As it is, a stop between the two leaves the helper the computer had under a mark no older
    // release passes, and the next apply, whatever it is of, puts the mark's own helper in first.
    // And current is switched last: a version that is installed has its helper, so that one with
    // none is no first install stopped half way, and the update is not installed until its keys are.
    const again = "again 0: current 1.1.0, mark 1.1.0, helper the update's";
    expect([...new Set(lines)]).toEqual([
      `current 1.0.0, mark 1.0.0, helper the release's before; ${again}`,
      `current 1.0.0, mark 1.1.0, helper the release's before; ${again}`,
      `current 1.0.0, mark 1.1.0, helper the update's; ${again}`,
    ]);
  });

  // The script, killed where it would make one of its last three renames, the mark's, the helper's
  // or current's: the name of that copy of it in the container.
  const stoppedBefore = (rename: "mark" | "helper" | "current") => {
    const killed = {
      mark: String.raw`s|^    mv -T "\$work/helper.json" "\$HELPER_MARK"$|    kill -KILL $$|`,
      helper: String.raw`s|^    mv -T "\$work/helper" "\$HELPER"$|    kill -KILL $$|`,
      current: String.raw`s|^  mv -T "\$work/current" "\$ROOT/current"$|  kill -KILL $$|`,
    }[rename];
    expect(root(`sed '${killed}' /opt/surogate-test/install.sh >/opt/surogate-test/stopped-${rename}.sh && chmod 755 /opt/surogate-test/stopped-${rename}.sh `
      + `&& ! cmp -s /opt/surogate-test/install.sh /opt/surogate-test/stopped-${rename}.sh`).status, rename).toBe(0);
    return `stopped-${rename}.sh`;
  };
  // A change of a release's tree: its own helper lists *trusted*, at *mode*.
  const listing = (trusted: string[], mode = 0o755) => (top: string) => {
    writeFileSync(join(top, "bin", "surogate-apply-update"), withKeys(readFileSync(SCRIPT, "utf8"), trusted));
    chmodSync(join(top, "bin", "surogate-apply-update"), mode);
  };

  it("finishes a helper's pair that an apply left half done before it asks the helper's keys about anything, at an update and at a first install: a release that dropped a key is not left trusting it", () => {
    const helper = "/opt/surogate/bin/surogate-apply-update";
    const mark = "/opt/surogate/bin/release.json";
    // A rotation's first release: the old key signs it, and its helper lists the old key and the new.
    const first = releaseOf("1.0.0", listing([PUBLIC, pem(next.publicKey)]));
    manifestOf("1.0.0", first);
    // A first install stopped at each of the three: the same apply, run again, finishes it, pair and all.
    for (const at of ["mark", "helper", "current"] as const) {
      expect(root("find /opt/surogate -mindepth 1 -delete").status, at).toBe(0);
      expect(apply(first, stoppedBefore(at)).status, at).toBe(137);
      expect(root("test ! -e /opt/surogate/current").status, at).toBe(0);
      expect(root(`/opt/surogate-test/install.sh --apply ${files()}`), at).toMatchObject({ status: 0, stdout: "Surogate Desktop: 1.0.0 is installed\n", stderr: "" });
      expect(root(`cmp ${helper} /opt/surogate/current/bin/surogate-apply-update && cmp ${mark} /opt/surogate/current/release.json`).status, at).toBe(0);
    }
    // Stopped before the helper's rename, a first install has its mark and no helper: the next
    // apply, of whatever, puts that release's own helper in first. The keys asked are then its
    // helper's, and not the list of the script that asks, which here lists the old key alone: what
    // a third key signs is refused, and what the new key signs is taken.
    expect(root("find /opt/surogate -mindepth 1 -delete").status).toBe(0);
    expect(apply(first, stoppedBefore("helper")).status).toBe(137);
    expect(root(`test ! -e ${helper} && test ! -L ${helper} && test ! -e /opt/surogate/current && cmp ${mark} /opt/surogate/versions/1.0.0/release.json`).status).toBe(0);
    const patch = releaseOf("1.0.1", listing([PUBLIC, pem(next.publicKey)]));
    manifestOf("1.0.1", patch, {}, other.privateKey);
    expect(apply(patch)).toMatchObject({ status: 1, stdout: "", stderr: `Surogate Desktop: the release's manifest${MISSED}\n` });
    expect(root(`cmp ${helper} /opt/surogate/versions/1.0.0/bin/surogate-apply-update && stat -c '%a %U' ${helper} && test ! -e /opt/surogate/current && ls /opt/surogate/versions`).stdout).toBe("755 root\n1.0.0\n");
    manifestOf("1.0.1", patch, {}, next.privateKey);
    expect(apply(patch)).toMatchObject({ status: 0, stdout: "Surogate Desktop: 1.0.1 is installed\n" });
    // What stands installed but for the helper pkexec runs, and that helper's place on the disk.
    const helperLine = (snapshot: string) => snapshot.split("\n").findIndex((line) => line.startsWith(`${helper} `));
    const butHelper = (snapshot: string) => snapshot.split("\n").filter((_, at) => at !== helperLine(snapshot) && at !== helperLine(snapshot) + 1).join("\n");
    const inode = (snapshot: string) => snapshot.split("\n")[helperLine(snapshot)]?.split(" ")[1];
    // The release that drops the old key: the new key signs it, and its helper lists the new key
    // alone. Its update is stopped between the mark's rename and the helper's. Its own helper as
    // package.sh packs one, and as one that root alone may run, which an apply takes too.
    for (const mode of [0o755, 0o700]) {
      expect(root("find /opt/surogate -mindepth 1 -delete").status).toBe(0);
      manifestOf("1.0.0", first);
      expect(apply(first).status).toBe(0);
      const second = releaseOf("1.1.0", listing([pem(next.publicKey)], mode));
      manifestOf("1.1.0", second, {}, next.privateKey);
      expect(apply(second, stoppedBefore("helper")).status).toBe(137);
      expect(root(`cmp ${mark} /home/tester/manifest.json && cmp ${helper} /opt/surogate/versions/1.0.0/bin/surogate-apply-update && stat -c %a /opt/surogate/versions/1.1.0/bin/surogate-apply-update && readlink /opt/surogate/current`).stdout)
        .toBe(`${mode.toString(8)}\n/opt/surogate/versions/1.0.0\n`);
      // What the old key signs then, newer than both, is no release to this computer: the pair is
      // finished first, and the keys asked are the ones of the release the mark names.
      const before = standing();
      const third = releaseOf("1.2.0", listing([PUBLIC]));
      manifestOf("1.2.0", third);
      // Told as what it is: the version that is still here lists the key that signed it.
      expect(apply(third)).toMatchObject({ status: 1, stdout: "", stderr: `Surogate Desktop: the release's manifest${RETIRED}\n` });
      // This is the one refusal that does not leave all as it was: the helper is the release's
      // that its mark names, and nothing else is changed.
      const after = standing();
      expect(butHelper(after)).toBe(butHelper(before));
      expect(root(`cmp ${helper} /opt/surogate/versions/1.1.0/bin/surogate-apply-update && stat -c '%a %U' ${helper} && readlink /opt/surogate/current && ls -A /opt/surogate/staging`).stdout)
        .toBe("755 root\n/opt/surogate/versions/1.0.0\n");
      // Put there by a rename, and never written into: pkexec may be running the one that was there.
      expect(inode(after)).not.toBe(inode(before));
      // The update itself, applied again, is installed.
      manifestOf("1.1.0", second, {}, next.privateKey);
      expect(apply(second)).toMatchObject({ status: 0, stdout: "Surogate Desktop: 1.1.0 is installed\n" });
      expect(current()).toBe("/opt/surogate/versions/1.1.0");
    }
    // The helper has its name by one rename, of a copy in the apply's own folder that is on the
    // disk first, as the apply's own renames are: it is not taken away and made again, which would
    // leave pkexec none between the two.
    expect(root("find /opt/surogate -mindepth 1 -delete").status).toBe(0);
    manifestOf("1.0.0", first);
    expect(apply(first).status).toBe(0);
    const second = releaseOf("1.1.0", listing([pem(next.publicKey)]));
    manifestOf("1.1.0", second, {}, next.privateKey);
    expect(apply(second, stoppedBefore("helper")).status).toBe(137);
    const at = traced();
    const renamed = at(/^\d+ +rename\w*\(.*"\/opt\/surogate\/staging\/apply\.\w+\/paired", .*"\/opt\/surogate\/bin\/surogate-apply-update".*\) += 0$/);
    expect(renamed).toBeGreaterThan(-1);
    const synced = at(/^\d+ +syncfs\(\d+\) += 0$/);
    expect(synced).toBeGreaterThan(-1);
    expect(synced).toBeLessThan(renamed);
    expect(current()).toBe("/opt/surogate/versions/1.1.0");
  });

  it("refuses a helper's pair that it finds half done and cannot finish, and one whose mark is behind its helper: none is passed over, and none is finished toward an older release", () => {
    const helper = "/opt/surogate/bin/surogate-apply-update";
    const mark = "/opt/surogate/bin/release.json";
    const refusal = { status: 1, stdout: "", stderr: `Surogate Desktop: ${helper} is not as Surogate Desktop's install leaves it: remove Surogate Desktop with --uninstall, and install it again\n` };
    const applying = () => root(`/opt/surogate-test/install.sh --apply ${files()}`);
    // The finishing by itself, from the script's functions without its last line.
    const finishing = () => root(`rm -rf /root/paired && mkdir /root/paired && bash -c '. <(sed "\\$d" /opt/surogate-test/install.sh) && settings && paired /root/paired'`);
    const first = releaseOf("1.0.0", listing([PUBLIC, pem(next.publicKey)]));
    manifestOf("1.0.0", first);
    expect(apply(first).status).toBe(0);
    // Half done: the update that drops the old key, stopped between its mark's rename and its helper's. Kept, to start each case from.
    const second = releaseOf("1.1.0", listing([pem(next.publicKey)]));
    manifestOf("1.1.0", second, {}, next.privateKey);
    expect(apply(second, stoppedBefore("helper")).status).toBe(137);
    expect(root("rm -rf /opt/half && cp -a /opt/surogate /opt/half").status).toBe(0);
    const halfDone = (then = ":") => expect(root(`find /opt/surogate -mindepth 1 -delete; cp -a /opt/half/. /opt/surogate/ && ${then}`).status, then).toBe(0);
    // What only root can have made of the release that the mark names: the helper is not finished
    // from it, and the apply does not go on with the keys of the release before. Whatever is
    // applied: here the update itself again, which those keys would take.
    const own = "/opt/surogate/versions/1.1.0";
    for (const [what, made] of [
      ["its own helper another user's", `chown tester ${own}/bin/surogate-apply-update`],
      ["its own helper for others to write", `chmod 775 ${own}/bin/surogate-apply-update`],
      ["its own helper with a set-id bit, which no apply leaves in a version", `chmod 4755 ${own}/bin/surogate-apply-update`],
      ["its own helper no program", `chmod 644 ${own}/bin/surogate-apply-update`],
      ["its own mark another user's", `chown tester ${own}/release.json`],
      ["its folder without its program", `rm ${own}/surogate`],
      // Nor a folder that cannot give its helper at all: with none to compare, nothing shows the
      // helper there to be this release's, and the keys of the release before are not asked.
      ["its folder without a helper of its own", `rm ${own}/bin/surogate-apply-update`],
      ["a link where its own helper is", `ln -sf /usr/bin/true ${own}/bin/surogate-apply-update`],
    ] as const) {
      halfDone(made);
      const before = standing();
      expect(applying(), what).toMatchObject(refusal);
      expect(standing(), what).toBe(before);
      expect(root("ls -A /opt/surogate/staging").stdout, what).toBe("");
    }
    // A mark behind its helper, as only root's own hand leaves one: the helper is not finished to
    // the older release that such a mark names. On a computer that runs the newer one:
    halfDone();
    expect(applying()).toMatchObject({ status: 0, stdout: "Surogate Desktop: 1.1.0 is installed\n" });
    expect(root(`install -m 0644 /opt/surogate/versions/1.0.0/release.json ${mark} && ! cmp -s ${helper} /opt/surogate/versions/1.0.0/bin/surogate-apply-update`).status).toBe(0);
    let before = standing();
    expect(applying()).toMatchObject(refusal);
    expect(standing()).toBe(before);
    // And on one that was rolled back to the older, whose helper is still the newer version's own.
    expect(root("ln -sfn /opt/surogate/versions/1.0.0 /opt/surogate/current").status).toBe(0);
    before = standing();
    expect(applying()).toMatchObject(refusal);
    expect(standing()).toBe(before);
    expect(root(`cmp ${helper} ${own}/bin/surogate-apply-update`).status).toBe(0);
    // And where the helper is the own one of no version that is here: the installed version is
    // newer than the mark names, and that alone says the mark is behind.
    expect(root(`ln -sfn /opt/surogate/versions/1.1.0 /opt/surogate/current && install -m 0755 /opt/surogate-test/install.sh ${helper} `
      + `&& ! cmp -s ${helper} ${own}/bin/surogate-apply-update && ! cmp -s ${helper} /opt/surogate/versions/1.0.0/bin/surogate-apply-update`).status).toBe(0);
    before = standing();
    expect(applying()).toMatchObject(refusal);
    expect(standing()).toBe(before);

    // Nothing is half done where the folder of the version that the mark names is another build's
    // than the mark's: the installed version built again, stopped before its mark's rename. That
    // is passed over, and the same apply, run again, ends it.
    expect(root("find /opt/surogate -mindepth 1 -delete").status).toBe(0);
    manifestOf("1.0.0", first);
    expect(apply(first).status).toBe(0);
    const again = releaseOf("1.0.0", listing([PUBLIC]));
    manifestOf("1.0.0", again);
    expect(apply(again, stoppedBefore("mark")).status).toBe(137);
    expect(root(`! cmp -s ${mark} /opt/surogate/versions/1.0.0/release.json && ! cmp -s ${helper} /opt/surogate/versions/1.0.0/bin/surogate-apply-update`).status).toBe(0);
    before = standing();
    expect(finishing()).toMatchObject({ status: 0, stdout: "", stderr: "" });
    expect(standing()).toBe(before);
    expect(applying()).toMatchObject({ status: 0, stdout: "Surogate Desktop: 1.0.0 is installed\n" });
    expect(root(`cmp ${helper} /opt/surogate/versions/1.0.0/bin/surogate-apply-update && cmp ${mark} /opt/surogate/versions/1.0.0/release.json`).status).toBe(0);
    // Stopped after its mark's rename, the mark is that build's, of the version that runs: no
    // older release's, and its helper is put in. A third build, as a tarball has its version's name.
    const third = releaseOf("1.0.0", listing([PUBLIC, pem(next.publicKey)]));
    manifestOf("1.0.0", third);
    expect(apply(third, stoppedBefore("helper")).status).toBe(137);
    expect(root(`cmp ${mark} /opt/surogate/versions/1.0.0/release.json && ! cmp -s ${helper} /opt/surogate/versions/1.0.0/bin/surogate-apply-update && readlink /opt/surogate/current`).stdout).toBe("/opt/surogate/versions/1.0.0\n");
    expect(finishing()).toMatchObject({ status: 0, stdout: "", stderr: "" });
    expect(root(`cmp ${helper} /opt/surogate/versions/1.0.0/bin/surogate-apply-update`).status).toBe(0);
  });

  it("ends at the step that failed where a folder stands in current's place, with the update's helper and mark in already: a state that names no removal, and that only one mends", () => {
    const first = releaseOf("1.0.0");
    manifestOf("1.0.0", first);
    expect(apply(first).status).toBe(0);
    expect(root("rm /opt/surogate/current && mkdir /opt/surogate/current").status).toBe(0);
    const second = releaseOf("1.1.0");
    manifestOf("1.1.0", second);
    const ended = apply(second);
    expect(ended).toMatchObject({ status: 1, stdout: "" });
    expect(ended.stderr).toMatch(/^(mv: [^\n]*\n)?Surogate Desktop: stopped, as this step failed: mv -T [^\n]*\n$/);
    expect(root("cmp /opt/surogate/bin/release.json /opt/surogate/versions/1.1.0/release.json && cmp /opt/surogate/bin/surogate-apply-update /opt/surogate/versions/1.1.0/bin/surogate-apply-update && test -d /opt/surogate/current && ls -A /opt/surogate/staging").stdout).toBe("");
    // The same again says the same; a removal, and the release applied anew, mend it.
    expect(apply(second).stderr).toMatch(/Surogate Desktop: stopped, as this step failed: mv -T [^\n]*\n$/);
    expect(root("/opt/surogate-test/install.sh --uninstall").status).toBe(0);
    expect(apply(second)).toMatchObject({ status: 0, stdout: "Surogate Desktop: 1.1.0 is installed\n" });
    expect(current()).toBe("/opt/surogate/versions/1.1.0");
  });

  it("unpacks the installed version again when its folder has lost a program, and takes an older version out of versions by one rename: but for the helper of the release that the helper's mark names, whose loss leaves a pair that cannot be shown whole", () => {
    const helper = "/opt/surogate/bin/surogate-apply-update";
    const mark = "/opt/surogate/bin/release.json";
    // The two ways a folder is without a program of its own: it is gone, or a link is there, here to one that runs.
    const losses = (program: string) => [`rm ${program}`, `ln -sf /usr/bin/true ${program}`];
    const tarballs: Record<string, string> = {};
    for (const version of ["1.0.0", "1.1.0"]) {
      const tarball = releaseOf(version);
      tarballs[version] = tarball;
      manifestOf(version, tarball);
      expect(apply(tarball).status).toBe(0);
      // Its mark is there, and the app is not: the folder is not whole, and is unpacked again.
      for (const lost of losses(`/opt/surogate/versions/${version}/surogate`)) {
        expect(root(lost).status, lost).toBe(0);
        expect(apply(tarball), lost).toMatchObject({ status: 0, stdout: `Surogate Desktop: ${version} is installed\n`, stderr: "" });
        expect(root(`test -x /opt/surogate/versions/${version}/surogate && test ! -L /opt/surogate/versions/${version}/surogate`).status, lost).toBe(0);
      }
      // Its own helper is not, and it is the release that the helper's mark names: with no helper
      // of the folder's own, nothing shows that the one pkexec runs is this release's and not the
      // one before's, left there by an update that stopped. That pair cannot be finished, and no
      // apply goes on with keys this release may have dropped.
      const own = `/opt/surogate/versions/${version}/bin/surogate-apply-update`;
      for (const lost of losses(own)) {
        expect(root(`cmp ${mark} /opt/surogate/versions/${version}/release.json && ${lost}`).status, lost).toBe(0);
        const before = standing();
        expect(apply(tarball), lost).toMatchObject({
          status: 1, stdout: "", stderr: `Surogate Desktop: ${helper} is not as Surogate Desktop's install leaves it: remove Surogate Desktop with --uninstall, and install it again\n`,
        });
        expect(standing(), lost).toBe(before);
        expect(root("ls -A /opt/surogate/staging").stdout, lost).toBe("");
        // Put back by hand, for what follows.
        expect(root(`rm -f ${own} && install -m 0755 ${helper} ${own}`).status, lost).toBe(0);
      }
    }
    // Beside it, where no pair is half done: the same loss in the folder of a version that the
    // mark does not name is unpacked again, as before. As a rollback leaves a computer, put so by
    // hand: the helper and its mark are the newer release's, whose folder is whole, and the
    // version that runs is the one before.
    expect(root(`ln -sfn /opt/surogate/versions/1.0.0 /opt/surogate/current && cmp ${mark} /opt/surogate/versions/1.1.0/release.json && cmp ${helper} /opt/surogate/versions/1.1.0/bin/surogate-apply-update`).status).toBe(0);
    manifestOf("1.0.0", tarballs["1.0.0"]!);
    for (const lost of losses("/opt/surogate/versions/1.0.0/bin/surogate-apply-update")) {
      expect(root(lost).status, lost).toBe(0);
      expect(apply(tarballs["1.0.0"]!), lost).toMatchObject({ status: 0, stdout: "Surogate Desktop: 1.0.0 is installed\n", stderr: "" });
      expect(root("test -x /opt/surogate/versions/1.0.0/bin/surogate-apply-update && test ! -L /opt/surogate/versions/1.0.0/bin/surogate-apply-update "
        + `&& cmp ${mark} /opt/surogate/versions/1.1.0/release.json && cmp ${helper} /opt/surogate/versions/1.1.0/bin/surogate-apply-update && readlink /opt/surogate/current`).stdout, lost).toBe("/opt/surogate/versions/1.0.0\n");
    }
    expect(root("ln -sfn /opt/surogate/versions/1.1.0 /opt/surogate/current").status).toBe(0);
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
        .toMatchObject({ status: 2, stdout: "", stderr: "usage: publish.sh describe|sign|send <x.y.z> <out>\n" });
    }
  });

  it("takes for a version to roll back to only the ten digits, in a locale that has more: as the user who asks, before sudo, and as root", () => {
    // Nor a part with a zero before it, in any locale: dpkg reads 1.2.03 as 1.2.3.
    for (const version of ["1.2.\u0663", "1.2.\u00b3", "\uff11.\uff12.\uff10", "1.2.03", "01.2.3", "1.02.3"]) for (const user of ["tester", "root"]) {
      expect(docker(["exec", "-u", user, "-e", "LC_ALL=en_US.UTF-8", box.container, "/opt/surogate-test/install.sh", "--version", version]), `${version} as ${user}`)
        .toMatchObject({ status: 1, stdout: "", stderr: "Surogate Desktop: usage: install.sh --version <x.y.z>\n" });
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
    expect(twice).toMatchObject({ stdout: "1 0\n", stderr: `Surogate Desktop: the release's manifest${MISSED}\n` });
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

  it("ends as a signal ends it when one comes while it asks who reads its files, as pkexec starts it at every update, and not with an error of bash's own", () => {
    const uid = root("id -u tester").stdout.trim();
    // The helper for a user, stopped once, by a signal to itself alone, while the first of its two
    // questions of who reads is still unanswered. Ubuntu 24.04's bash runs the signal's handler
    // while it reads the second of two substitutions of one command, and then cannot read the
    // handler's own text: each of the script's commands holds one substitution at most.
    const stopped = swapped("id", SLOW_ID, [
      "cp -L /usr/bin/sleep /opt/hold/sleep",
      "for run in 1 2 3 4 5; do",
      "  rm -f /tmp/asked",
      `  PKEXEC_UID=${uid} /opt/surogate-test/install.sh --apply ${files()} >/tmp/said 2>&1 & helper=$!`,
      "  for try in $(seq 200); do [ -e /tmp/asked ] && break; /opt/hold/sleep 0.01; done",
      "  [ -e /tmp/asked ] || echo 'never asked'",
      "  kill -TERM \"$helper\"; wait \"$helper\"",
      "  echo \"$? $(cat /tmp/said)\"",
      "done",
    ]);
    expect(stopped.stdout).toBe("143 \n".repeat(5));
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

// A desktop's baseline: sudo for its administrator, curl, AppArmor's parser, polkit, the system
// bus and the kvm group; and another user of the computer. The container cannot load a profile
// into the kernel, so its apparmor_parser parses one as the release's own parser reads it, and
// stops there. With them, Ubuntu's German language pack, in which the system's tools say what
// they say in German to whoever's desktop is; and strace, for the test that reads what the
// script starts.
const INSTALL_LAB = [
  "RUN apt-get update && apt-get install -y --no-install-recommends sudo curl ca-certificates apparmor polkitd pkexec dbus",
  "RUN groupadd --system kvm && useradd -m -s /bin/bash -G sudo tester && useradd -m -s /bin/bash other && echo 'tester ALL=(ALL) NOPASSWD:ALL' >/etc/sudoers.d/tester",
  // In the system's own place, the system's parser kept beside it: the install runs the tools of the system's four folders, and no other.
  `RUN mv /usr/sbin/apparmor_parser /usr/sbin/apparmor_parser.own && echo '#!/bin/sh' >/usr/sbin/apparmor_parser && echo 'said=$(/usr/sbin/apparmor_parser.own --skip-kernel-load "$@" 2>&1) || { echo "$said" >&2; exit 1; }' >>/usr/sbin/apparmor_parser && chmod 755 /usr/sbin/apparmor_parser`,
  "RUN apt-get update && apt-get install -y --no-install-recommends language-pack-de strace",
];

for (const release of RELEASES) describe.skipIf(!ENABLED)(`the install script, on Ubuntu ${release}`, { timeout: 600_000 }, () => {
  const { it: box, docker, root, as, releaseOf, manifestOf, current, versions, standing, swapped } = lab(release, INSTALL_LAB, ["--network", "host"]);
  let server: ChildProcess;
  let base: string;
  // What the base serves: desktop/install.sh, latest.json, and each release with its own manifest
  // and signature. latest.json has no signature of its own: its release's is the one asked for.
  const www = () => join(box.dir, "www");
  // Release *version* on the base, signed by *key*, with *fields* in its manifest's place; its own
  // helper lists *trusted*, where a rotation's release lists other keys than the test's one.
  const publish = (version: string, key?: KeyObject, fields: Record<string, unknown> = {}, trusted?: string[]) => {
    const tarball = releaseOf(version, (top) => {
      if (trusted) writeFileSync(join(top, "bin", "surogate-apply-update"), withKeys(readFileSync(SCRIPT, "utf8"), trusted), { mode: 0o755 });
    });
    const manifest = manifestOf(version, tarball, fields, key);
    const folder = join(www(), "desktop", "releases", version);
    mkdirSync(folder, { recursive: true });
    copyFileSync(tarball, join(folder, `surogate-desktop-${version}-linux-x64.tar.gz`));
    // Each release's own manifest, kept beside its tarball, as the release job sends it.
    copyFileSync(join(box.dir, "manifest.json"), join(folder, "manifest.json"));
    copyFileSync(join(box.dir, "manifest.json.sig"), join(folder, "manifest.json.sig"));
    writeFileSync(join(www(), "desktop", "latest.json"), manifest);
  };
  const install = (env = "") => as("tester", `curl -fsSL ${base}/desktop/install.sh | ${env} bash -s -- --base ${base}`);
  const uninstall = (env = "") => as("tester", `curl -fsSL ${base}/desktop/install.sh | ${env} bash -s -- --uninstall`);
  const rollBack = (version: string) => as("tester", `curl -fsSL ${base}/desktop/install.sh | bash -s -- --version ${version}`);
  // The base's newest release named again: *version*'s own manifest as latest.json.
  const latest = (version: string) => {
    copyFileSync(join(www(), "desktop", "releases", version, "manifest.json"), join(www(), "desktop", "latest.json"));
  };
  // The computer as a test that stands alone begins: nothing of a test before it, then each of
  // *releases* published in turn, with *fields* in its manifest's place, and installed.
  const starting = (...releases: Array<[version: string, fields?: Record<string, unknown>]>) => {
    const removed = uninstall();
    expect(removed.status, removed.stderr).toBe(0);
    for (const [version, fields] of releases) {
      publish(version, undefined, fields);
      const installed = install();
      expect(installed.status, `${version}: ${installed.stderr}`).toBe(0);
    }
    expect(current()).toBe(`/opt/surogate/versions/${releases.at(-1)?.[0]}`);
  };
  // The files of the base's release *version* handed to the helper pkexec runs, as the app downloads them.
  const handed = (version: string) => root(`cd /home/tester && curl -fsSLO ${base}/desktop/releases/${version}/manifest.json -O ${base}/desktop/releases/${version}/manifest.json.sig `
    + `-o release.tar.gz ${base}/desktop/releases/${version}/surogate-desktop-${version}-linux-x64.tar.gz && /opt/surogate/bin/surogate-apply-update --apply manifest.json manifest.json.sig release.tar.gz`);

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
    // A base that is no URL is said of --base; what no option takes, of the install's options.
    for (const [args, usage] of [["--base", "--base <http or https URL>"], ["--base ftp://elsewhere.example", "--base <http or https URL>"],
      [`--base ${base} again`, "[--base <url>] [--ca-cert <file>]"]] as const) {
      expect(as("tester", `curl -fsSL ${base}/desktop/install.sh | bash -s -- ${args}`), args)
        .toMatchObject({ status: 1, stdout: "", stderr: `Surogate Desktop: usage: install.sh ${usage}\n` });
    }
    // Nor a base with a user or a password in it, said as what it is, before sudo is asked: the
    // base is written where every user reads it, and the app takes no such base.
    for (const named of ["http://user:secret@127.0.0.1:9", "https://user@surogate.example/", "http://:secret@surogate.example", `http://user:secret@${base.slice("http://".length)}`]) {
      for (const started of [as("tester", `curl -fsSL ${base}/desktop/install.sh | bash -s -- --base ${named}`), root(`/opt/surogate-test/install.sh --base ${named}`)]) {
        expect(started, named).toMatchObject({
          status: 1, stdout: "", stderr: "Surogate Desktop: a base with a user or a password in it is not taken: it would be written where every user of this computer reads it. Name the server alone\n",
        });
      }
    }
    // The record's own writing refuses one too, whoever calls it: asked by itself, from the
    // script's functions without its last line, it writes no record, and no file beside one.
    expect(root(`bash -c '. <(sed "\\$d" /opt/surogate-test/install.sh) && settings && record http://user:secret@surogate.example'; echo "ended $?"; ls -A /etc/surogate 2>/dev/null`)).toMatchObject({
      stdout: "ended 1\n", stderr: "Surogate Desktop: a base with a user or a password in it is not taken: it would be written where every user of this computer reads it. Name the server alone\n",
    });
    // A base names its server right behind its two slashes. With a third slash there, the script
    // would find no server and so no user, and curl, which takes the third for a slip, would send
    // the login: it is no URL here, as it is none to the app. Nor is one with no server at all.
    // The refusal says nothing of what was typed, and comes before sudo is asked, whose log keeps
    // its command line.
    for (const named of ["http:///user:secret@127.0.0.1:9", `http:///user:secret@${base.slice("http://".length)}`, "https:////user:secret@surogate.example", "http://", "https://?user:secret@surogate.example", "http://#user:secret@surogate.example"]) {
      for (const started of [as("tester", `curl -fsSL ${base}/desktop/install.sh | bash -s -- --base '${named}'`), root(`/opt/surogate-test/install.sh --base '${named}'`)]) {
        expect(started, named).toMatchObject({ status: 1, stdout: "", stderr: "Surogate Desktop: usage: install.sh --base <http or https URL>\n" });
      }
    }
    // An at sign after the server's name is no user's: such a base is read on, to this computer's own refusal or its install.
    expect(root("test ! -e /opt/surogate && test ! -e /etc/surogate").status).toBe(0);
    expect(root("cp /etc/os-release /root/os-release").status).toBe(0);
    const others = [
      'ID=ubuntu\nVERSION_ID="25.10"\nVERSION="25.10 (Questing Quokka)"',
      'ID=ubuntu\nVERSION_ID="22.04"\nVERSION="22.04.5 LTS (Jammy Jellyfish)"',
      'ID=debian\nVERSION_ID="12"\nVERSION="12 (bookworm)"',
    ];
    // The sentence as it is: not as a line of the script's own, which would say the app's name twice.
    const unsupported = "Surogate Desktop supports Ubuntu 24.04 LTS or a later LTS release (x64)\n";
    for (const osRelease of others) {
      expect(root(`printf '%s\\n' '${osRelease}' >/etc/os-release`).status).toBe(0);
      expect(install()).toMatchObject({ status: 1, stdout: "", stderr: unsupported });
    }
    // A computer with no such file at all, as one that is no Linux: the same sentence, and nothing of bash's own.
    expect(root("mv /etc/os-release /etc/os-release.away").status).toBe(0);
    const without = install();
    expect(root("mv /etc/os-release.away /etc/os-release").status).toBe(0);
    expect(without).toMatchObject({ status: 1, stdout: "", stderr: unsupported });
    // What is a base is read byte for byte, the same in every locale of its caller's: white space
    // is ASCII's six characters and no other, and bytes that are no letters are bytes. On this
    // computer, which it does not support, a base it takes gets as far as that refusal.
    for (const locale of ["C", "C.UTF-8", "de_DE.UTF-8"]) {
      const based = (url: string) => as("tester", `curl -fsSL ${base}/desktop/install.sh | LC_ALL=${locale} bash -s -- --base ${url}`);
      for (const url of ["$'http://b\\303\\274cher.example'", "$'http://surogate.example/\\343\\200\\200'", "$'http://surogate.example/\\377\\376'", "'http://surogate.example/a@b'", "'http://surogate.example?to=a@b'", "'http://surogate.example#a@b'"]) {
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
    expect(root("dpkg-query -W -f='${Status}\\n' bubblewrap socat ripgrep virtiofsd uidmap zstd pkexec perl-base openssl jq qemu-system-x86 | sort -u").stdout).toBe("install ok installed\n");
    expect(root("dpkg-query -W -f='${Status}\\n' qemu-system-gui 2>/dev/null").stdout).not.toBe("install ok installed\n");
    // certutil is for a company's CA alone: an install that keeps none brings none.
    expect(root("test ! -e /usr/bin/certutil").status).toBe(0);
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
    expect(install()).toMatchObject({ status: 1, stderr: `Surogate Desktop: ${base}/desktop/latest.json${MISSED}\n` });
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
    starting(["1.1.0"]);
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
    // As the app runs it, started from a terminal in a session with no polkit agent, for a user no
    // rule lets update: pkexec asks nothing on the terminal, and ends at once with 127, which the
    // app says as an administrator being needed. With an agent of its own it would wait there for
    // a password: the terminal is closed on it after 20 s, as nothing less ends 26.04's.
    const asked = as("other", `timeout -s KILL 20 script -qec "${AS_ROOT.join(" ")} --apply ${updates}/manifest.json ${updates}/manifest.json.sig ${updates}/release.tar.gz; echo ended \\$?" /dev/null`);
    expect(asked.stdout).toContain("ended 127");
    expect(asked.stdout).not.toContain("AUTHENTICATING");
    // The words the app reads that 127 by: pkexec's own, in no locale, for a command it could not
    // run as another user. Its 127 with any other words is a failure, which the app says as it is.
    expect(asked.stdout).toContain("Error executing command as another user: No authentication agent found.");
    // Such a failure, for the user the rule lets update: the helper is not there for pkexec to run.
    expect(root("mv /opt/surogate/bin/surogate-apply-update /opt/surogate/bin/aside").status).toBe(0);
    try {
      const gone = as("tester", `${AS_ROOT.join(" ")} --apply ${updates}/manifest.json ${updates}/manifest.json.sig ${updates}/release.tar.gz; exit $?`);
      expect(gone).toMatchObject({ status: 127, stdout: "" });
      expect(gone.stderr.trim()).toBe("Error accessing /opt/surogate/bin/surogate-apply-update: No such file or directory");
    } finally {
      expect(root("mv /opt/surogate/bin/aside /opt/surogate/bin/surogate-apply-update").status).toBe(0);
    }
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
    expect(root("/opt/surogate-test/install.sh --remove")).toMatchObject({ status: 1, stderr: "Surogate Desktop: usage: install.sh [--base <url>] [--ca-cert <file>] [--version <x.y.z>] [--uninstall]\n" });
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

    // A folder the session names by no whole path is no folder of XDG's: the entry goes from where
    // XDG's default puts it, and nothing under that name where the script was started.
    expect(install().status).toBe(0);
    expect(as("tester", "mkdir -p .config/autostart cfg-relative/autostart && touch .config/autostart/surogate.desktop cfg-relative/autostart/surogate.desktop").status).toBe(0);
    expect(uninstall("XDG_CONFIG_HOME=cfg-relative")).toMatchObject({ status: 0, stderr: "" });
    expect(root("test ! -e /home/tester/.config/autostart/surogate.desktop && test -e /home/tester/cfg-relative/autostart/surogate.desktop && test ! -e /opt/surogate").status).toBe(0);
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
    // One of *letters* is started in no language too, and reads letters as UTF-8: C.UTF-8 has no words of its own.
    const started = (part: string, tools: string[], letters: string[] = []) => {
      const traced = root(`${GERMAN} strace --seccomp-bpf -f -qq -v -s 256 -o /tmp/trace -e trace=execve ${part} >/dev/null 2>&1; echo "$?"; grep -E '^[0-9]+ +execve\\("[^"]*/(${tools.join("|")})", ' /tmp/trace`);
      const [status, ...calls] = traced.stdout.trim().split("\n");
      expect(status, part).toBe("0");
      const named = calls.map((call) => [/^\d+ +execve\("[^"]*\/([^"/]+)", /.exec(call)?.[1], ...[...call.matchAll(/"((?:LANG|LANGUAGE|LC_\w+)=[^"]*)"/g)].map((match) => match[1]).sort()].join(" "));
      expect([...new Set(named)].sort(), part).toEqual(tools.map((tool) => `${tool} LANG=C LC_ALL=${letters.includes(tool) ? "C.UTF-8" : "C"}`).sort());
    };
    publish("2.0.0");
    started(`/opt/surogate-test/install.sh --base ${base}`, ["apt-get", "curl", "jq"], ["curl"]);
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

  it("downloads no more of a release than its signed manifest names, and no more of a manifest or a signature than one holds, whatever its server sends", () => {
    publish("2.3.0");
    const desktop = join(www(), "desktop");
    const tarball = join(desktop, "releases", "2.3.0", "surogate-desktop-2.3.0-linux-x64.tar.gz");
    const kept = Object.fromEntries(["latest.json", "releases/2.3.0/manifest.json.sig"].map((name) => [name, readFileSync(join(desktop, name))]));
    const release = readFileSync(tarball);
    // What the install says last: curl's own words for a download past its bound, then the script's line.
    const stopped = (line: string) => {
      const installed = install();
      expect(installed.status, line).toBe(1);
      expect(installed.stderr.trimEnd().split("\n").slice(-2), line).toEqual([expect.stringMatching(/^curl: \(63\) .*[Mm]aximum (allowed )?file size/), `Surogate Desktop: ${line}`]);
      // Its download's folder goes, and nothing is installed.
      expect(root("find /tmp -mindepth 1 -maxdepth 1 -name 'tmp.*'; test ! -e /opt/surogate/current").stdout, line).toBe("");
    };
    // A server that does not say how much it sends: the download stops at its bound, into root's /tmp, which is memory on Ubuntu 26.04.
    writeFileSync(join(www(), "unsized"), "");
    try {
      // A manifest is a line, of 4096 bytes at most.
      writeFileSync(join(desktop, "latest.json"), Buffer.concat([kept["latest.json"]!, Buffer.alloc(4097 - kept["latest.json"]!.length, " ")]));
      stopped(`could not download ${base}/desktop/latest.json`);
      writeFileSync(join(desktop, "latest.json"), kept["latest.json"]!);
      // Its signature is Ed25519's 64 bytes.
      writeFileSync(join(desktop, "releases/2.3.0/manifest.json.sig"), Buffer.concat([kept["releases/2.3.0/manifest.json.sig"]!, Buffer.alloc(2)]));
      stopped(`could not download ${base}/desktop/releases/2.3.0/manifest.json.sig`);
      writeFileSync(join(desktop, "releases/2.3.0/manifest.json.sig"), kept["releases/2.3.0/manifest.json.sig"]!);
      // The signature asked for is the release's own, which is never sent a second time: the
      // version is the manifest's, read before any key is asked, and only a version names a place.
      // One that is none is no release, and nothing more is asked of the base.
      for (const named of ["2.3.0/../../../unsized#", "", "2.3", "02.3.0", " 2.3.0", 230]) {
        writeFileSync(join(desktop, "latest.json"), `${JSON.stringify({ ...JSON.parse(kept["latest.json"]!.toString()), version: named })}\n`);
        expect(install(), String(named)).toMatchObject({ status: 1, stderr: `Surogate Desktop: ${base}/desktop/latest.json is not a release of Surogate Desktop for this computer\n` });
      }
      writeFileSync(join(desktop, "latest.json"), kept["latest.json"]!);
      // And there is no other place for it: a signature beside latest.json is not looked for.
      writeFileSync(join(desktop, "latest.json.sig"), kept["releases/2.3.0/manifest.json.sig"]!);
      rmSync(join(desktop, "releases/2.3.0/manifest.json.sig"));
      expect(install()).toMatchObject({ status: 1 });
      expect(install().stderr.trimEnd().split("\n").at(-1)).toBe(`Surogate Desktop: could not download ${base}/desktop/releases/2.3.0/manifest.json.sig`);
      rmSync(join(desktop, "latest.json.sig"));
      writeFileSync(join(desktop, "releases/2.3.0/manifest.json.sig"), kept["releases/2.3.0/manifest.json.sig"]!);
      // The tarball is the size its signed manifest names, and here a megabyte more.
      writeFileSync(tarball, Buffer.concat([release, Buffer.alloc(1024 * 1024)]));
      stopped(`could not download Surogate Desktop 2.3.0 from ${base}`);
    } finally {
      rmSync(join(www(), "unsized"));
    }
    // And from a server that says how much it would send: nothing of it is asked for.
    stopped(`could not download Surogate Desktop 2.3.0 from ${base}`);
    // The release as it was signed is installed.
    writeFileSync(tarball, release);
    const installed = install();
    expect(installed.status, installed.stderr).toBe(0);
    expect(current()).toBe("/opt/surogate/versions/2.3.0");
    expect(uninstall().status).toBe(0);
  });

  it("looks up a server whose name has a letter outside ASCII, as the user's own download of the script did: root's part reads its base's letters as UTF-8", () => {
    // A name no resolver has: the download gets as far as looking it up, and curl says so. In a
    // locale with no letters but ASCII's, curl refuses the name before it looks it up (its
    // "URL using bad/illegal format"), and no server so named can be installed from.
    for (const locale of ["C.UTF-8", "de_DE.UTF-8"]) {
      const installed = as("tester", `curl -fsSL ${base}/desktop/install.sh | LC_ALL=${locale} bash -s -- --base http://b\u00fccher.invalid`);
      expect(installed.status, locale).toBe(1);
      expect(installed.stderr.trimEnd().split("\n").slice(-2), locale).toEqual([
        expect.stringMatching(/^curl: \(6\) Could not resolve host: /),
        "Surogate Desktop: could not download http://b\u00fccher.invalid/desktop/latest.json",
      ]);
    }
    expect(root("test ! -e /opt/surogate && test ! -e /etc/surogate").status).toBe(0);
  });

  it("rolls back, at an administrator's word, to a release that reads what the installed one keeps, and to none that cannot", () => {
    publish("1.4.0");
    // Nothing to roll back on a computer it is not installed on: said once, by the half that runs as root.
    expect(rollBack("1.4.0")).toMatchObject({ status: 1, stderr: "Surogate Desktop: Surogate Desktop is not installed: run its install script first\n" });
    expect(root("test ! -e /opt/surogate && test ! -e /etc/surogate").status).toBe(0);
    // Nor does it follow an /opt/surogate that is a link, as a user or as root's part by itself: refused as the install refuses it.
    expect(root("ln -sfn /nowhere /opt/surogate").status).toBe(0);
    try {
      expect(rollBack("1.4.0")).toMatchObject({ status: 1, stdout: "", stderr: linked() });
      expect(root("/opt/surogate-test/install.sh --version 1.4.0")).toMatchObject({ status: 1, stdout: "", stderr: linked() });
      expect(root("test -L /opt/surogate && test ! -e /nowhere && test ! -e /etc/surogate").status).toBe(0);
    } finally {
      root("rm /opt/surogate");
    }
    expect(install().status).toBe(0);
    publish("1.5.0");
    expect(install().status).toBe(0);
    const back = rollBack("1.4.0");
    expect(back.status, back.stderr).toBe(0);
    expect(back.stdout).toContain("Surogate Desktop: rolling back needs administrator rights: sudo asks for your password once\n");
    expect(back.stdout).toContain("Surogate Desktop: 1.4.0 is installed\n");
    // Here whole, as the version before an update is: only its manifest and signature are asked of the base.
    expect(back.stdout).not.toContain("downloading");
    expect(current()).toBe("/opt/surogate/versions/1.4.0");
    // Kept: the version rolled back to, and the one it replaced.
    expect(versions()).toEqual(["1.4.0", "1.5.0"]);
    // The helper pkexec runs is still the newer release's, as its mark says.
    expect(root("cmp /opt/surogate/bin/release.json /opt/surogate/versions/1.5.0/release.json").status).toBe(0);
    // A release that changes what the app keeps in each user's home: no earlier one reads it.
    publish("1.6.0", undefined, { stateSchema: 2 });
    expect(install().status).toBe(0);
    // From here on every rollback is refused: each leaves the installed version, the helper, its
    // mark and the keys it lists the bytes they are now.
    const installed = standing();
    const unchanged = (after: string) => expect(standing(), after).toBe(installed);
    expect(rollBack("1.5.0")).toMatchObject({
      status: 1, stderr: "Surogate Desktop: 1.5.0 cannot read what the installed 1.6.0 keeps for its users: its state schema is 1, and 1.6.0's 2\n",
    });
    expect(current()).toBe("/opt/surogate/versions/1.6.0");
    unchanged("a release whose state schema is below the installed one's");
    // The apply compares the two itself, with the lock held, whatever was compared before it: an
    // apply of an older release by itself, from the script's functions without its last line.
    expect(root(`cd /home/tester && curl -fsSO ${base}/desktop/releases/1.4.0/manifest.json -O ${base}/desktop/releases/1.4.0/manifest.json.sig `
      + `&& bash -c '. <(sed "\\$d" /opt/surogate-test/install.sh) && settings && trap cleanup EXIT && apply manifest.json manifest.json.sig "" older'`))
      .toMatchObject({ status: 1, stdout: "", stderr: "Surogate Desktop: 1.4.0 cannot read what the installed 1.6.0 keeps for its users: its state schema is 1, and 1.6.0's 2\n" });
    unchanged("the same, compared by the apply itself");
    // curl's own words first, then the script's.
    const missing = rollBack("1.9.9");
    expect(missing.status).toBe(1);
    expect(missing.stderr.endsWith(`Surogate Desktop: could not download ${base}/desktop/releases/1.9.9/manifest.json\n`)).toBe(true);
    unchanged("a release its base does not have");
    // A base whose name has a letter outside ASCII is looked up, as the install looks one up: root's
    // part reads its letters as UTF-8, where in its own locale curl refuses the name before it asks.
    expect(root(`cp /etc/surogate/install.json /root/install.json && jq -c '.base = "http://b\u00fccher.invalid"' /root/install.json >/etc/surogate/install.json`).status).toBe(0);
    const named = rollBack("1.4.0");
    expect(root("cp /root/install.json /etc/surogate/install.json").status).toBe(0);
    expect(named.stderr.trimEnd().split("\n").slice(-2)).toEqual([
      expect.stringMatching(/^curl: \(6\) Could not resolve host: /),
      "Surogate Desktop: could not download http://b\u00fccher.invalid/desktop/releases/1.4.0/manifest.json",
    ]);
    unchanged("a base that no resolver has");
    // Another release's signed manifest, served under this one's name, is not this release.
    const releases = join(www(), "desktop", "releases");
    mkdirSync(join(releases, "1.5.5"));
    for (const end of ["", ".sig"]) copyFileSync(join(releases, "1.6.0", `manifest.json${end}`), join(releases, "1.5.5", `manifest.json${end}`));
    expect(rollBack("1.5.5")).toMatchObject({
      status: 1, stderr: `Surogate Desktop: ${base}/desktop/releases/1.5.5/manifest.json is not release 1.5.5 of Surogate Desktop for this computer\n`,
    });
    unchanged("another release's manifest under the version's name");
    // Nor is one that no release key signed.
    publish("1.6.1", other.privateKey, { stateSchema: 2 });
    expect(rollBack("1.6.1")).toMatchObject({ status: 1, stderr: `Surogate Desktop: ${base}/desktop/releases/1.6.1/manifest.json${UNSIGNED}\n` });
    unchanged("a release no release key signed");
    // No more of a tarball is downloaded than its manifest names: curl's own words, then the script's.
    publish("1.6.2", undefined, { stateSchema: 2 });
    const tarball = join(releases, "1.6.2", "surogate-desktop-1.6.2-linux-x64.tar.gz");
    const signedBytes = readFileSync(tarball);
    writeFileSync(tarball, Buffer.concat([signedBytes, randomBytes(4096)]));
    const longer = rollBack("1.6.2");
    expect(longer.status).toBe(1);
    expect(longer.stderr.endsWith(`Surogate Desktop: could not download Surogate Desktop 1.6.2 from ${base}\n`)).toBe(true);
    unchanged("a tarball longer than its manifest names");
    // One of that size that is not its manifest's is refused by the apply, which leaves nothing of it in staging.
    writeFileSync(tarball, randomBytes(signedBytes.length));
    const swapped = rollBack("1.6.2");
    expect(swapped.status).toBe(1);
    expect(swapped.stderr.endsWith("Surogate Desktop: the downloaded release is not the one its manifest names\n")).toBe(true);
    expect(current()).toBe("/opt/surogate/versions/1.6.0");
    unchanged("a tarball that is not its manifest's");
    latest("1.6.0");
    for (const args of ["--version", "--version 1.4", "--version 1.4.0 again", "--version v1.4.0"]) {
      expect(as("tester", `curl -fsSL ${base}/desktop/install.sh | bash -s -- ${args}`), args)
        .toMatchObject({ status: 1, stdout: "", stderr: "Surogate Desktop: usage: install.sh --version <x.y.z>\n" });
      unchanged(args);
    }
    // On a computer the script does not support, it is refused as the install is, before sudo is asked.
    expect(root(`cp /etc/os-release /root/os-release && printf '%s\\n' 'ID=debian' 'VERSION_ID="12"' 'VERSION="12 (bookworm)"' >/etc/os-release`).status).toBe(0);
    expect(rollBack("1.4.0")).toMatchObject({ status: 1, stdout: "", stderr: "Surogate Desktop supports Ubuntu 24.04 LTS or a later LTS release (x64)\n" });
    expect(root("cp /root/os-release /etc/os-release").status).toBe(0);
    unchanged("a computer the script does not support");
    // Its download's folder goes, and what its apply copied, whether it was applied or refused.
    expect(root("find /tmp -mindepth 1 -maxdepth 1 -name 'tmp.*'; ls -A /opt/surogate/staging").stdout).toBe("");
    // The update the app downloads still never goes back: only an administrator's --version does.
    expect(handed("1.5.0")).toMatchObject({ status: 1, stderr: "Surogate Desktop: 1.5.0 is older than the installed 1.6.0\n" });
    unchanged("an older release handed to the helper");
  });

  it("rolls back to no release whose manifest or signature its base does not serve, or serves longer than one is, and from no installed version whose mark names no state schema: each leaves all as it was", () => {
    const releases = join(www(), "desktop", "releases");
    expect(current()).toBe("/opt/surogate/versions/1.6.0");
    const installed = standing();
    // What a rollback to *version* says last, of *file*: curl's own words, then the script's.
    const stopped = (version: string, file: string, curl: RegExp) => {
      const back = rollBack(version);
      expect(back.status, file).toBe(1);
      expect(back.stderr.trimEnd().split("\n").slice(-2), file).toEqual([expect.stringMatching(curl), `Surogate Desktop: could not download ${base}/desktop/releases/${version}/${file}`]);
      expect(standing(), file).toBe(installed);
    };
    const tooLong = /^curl: \(63\) .*[Mm]aximum (allowed )?file size/;
    // A release whose manifest is there, and no signature of it.
    mkdirSync(join(releases, "1.6.3"));
    copyFileSync(join(releases, "1.6.0", "manifest.json"), join(releases, "1.6.3", "manifest.json"));
    stopped("1.6.3", "manifest.json.sig", /^curl: \(22\) .* 404/);
    // A signature of two bytes more than Ed25519's 64: no more of it is asked for than 65.
    writeFileSync(join(releases, "1.6.3", "manifest.json.sig"), Buffer.alloc(66));
    stopped("1.6.3", "manifest.json.sig", tooLong);
    // A manifest of a byte more than a line of 4096.
    writeFileSync(join(releases, "1.6.3", "manifest.json"), Buffer.alloc(4097, " "));
    stopped("1.6.3", "manifest.json", tooLong);

    // The installed version's own mark without its state schema, as only root can leave it: what
    // it keeps for its users is not known, so no release is taken for one that reads it.
    expect(root("jq -c 'del(.stateSchema)' /opt/surogate/versions/1.6.0/release.json >/root/release.json && cp /root/release.json /opt/surogate/versions/1.6.0/release.json").status).toBe(0);
    const unmarked = standing();
    expect(unmarked).not.toBe(installed);
    for (const version of ["1.6.0", "1.4.0"]) {
      expect(rollBack(version), version).toMatchObject({ status: 1, stderr: "Surogate Desktop: the installed 1.6.0 names no state schema: run Surogate Desktop's install script again\n" });
      expect(standing(), version).toBe(unmarked);
    }
    expect(root("find /tmp -mindepth 1 -maxdepth 1 -name 'tmp.*'; ls -A /opt/surogate/staging").stdout).toBe("");
    // The install script, run again as that says, takes the release from its base again, mark and all.
    const mended = install();
    expect(mended.status, mended.stderr).toBe(0);
    expect(root(`curl -fsS ${base}/desktop/releases/1.6.0/manifest.json | cmp - /opt/surogate/current/release.json`).status).toBe(0);
    expect(versions()).toEqual(["1.4.0", "1.6.0"]);
  });

  it("rolls back from no base that its record does not name as an install wrote it, and to no release while the installed version's mark is not root's own: curl is handed no word of a record's but an http or https URL, and nothing is asked of any base", () => {
    starting(["1.4.0"], ["1.6.0", { stateSchema: 2 }]);
    expect(current()).toBe("/opt/surogate/versions/1.6.0");
    const record = "/etc/surogate/install.json";
    const mark = "/opt/surogate/versions/1.6.0/release.json";
    // curl, kept as /opt/hold/curl, which first writes down what it was asked for.
    const asking = '#!/bin/sh\necho "$*" >>/tmp/curled\nexec /opt/hold/curl "$@"\n';
    // A rollback by root's own part, after *first* and before *last*: how it ended, how many times
    // it ran curl, and what it said. Bounded: a record that is a pipe would be waited on for good.
    const rolled = (version: string, first = ":", last = ":") => swapped("curl", asking, [
      first, ": >/tmp/curled",
      `timeout 60 /opt/surogate-test/install.sh --version ${version} >/tmp/said 2>&1; echo "$? $(wc -l </tmp/curled) $(cat /tmp/said)"`,
      last,
    ]).stdout;
    // As an install wrote the record, the base is asked: three times for a version whose folder
    // has lost its program, for its manifest, its signature and its tarball. And each time for no
    // longer than a bound: a base that takes the connection and never answers, or stops in the
    // middle of an answer, would otherwise hold the rollback for good. The install's own three are
    // asked the same way.
    expect(root(`stat -c '%f %u' ${record} ${mark} && cp -p ${record} /root/record && cp -p ${mark} /root/mark`).stdout).toBe("81a4 0\n81a4 0\n");
    const bounded = (files: string[]) => {
      const requests = root("cat /tmp/curled").stdout.trim().split("\n");
      expect(requests.map((request) => request.replace(/^.* [^ ]*\/desktop\//, ""))).toEqual(files);
      for (const request of requests) expect(request).toContain("--connect-timeout 30 --speed-limit 1024 --speed-time 60");
    };
    const tarball = "releases/1.6.0/surogate-desktop-1.6.0-linux-x64.tar.gz";
    const asked = rolled("1.6.0", "rm /opt/surogate/versions/1.6.0/surogate");
    expect(asked.startsWith("0 3 ") && asked.endsWith("Surogate Desktop: 1.6.0 is installed\n"), asked).toBe(true);
    bounded(["releases/1.6.0/manifest.json", "releases/1.6.0/manifest.json.sig", tarball]);
    expect(swapped("curl", asking, ["rm /opt/surogate/versions/1.6.0/surogate", ": >/tmp/curled", `/opt/surogate-test/install.sh --base ${base} >/dev/null 2>&1; echo "$?"`]).stdout).toBe("0\n");
    bounded(["latest.json", "releases/1.6.0/manifest.json.sig", tarball]);
    const installed = standing();
    const restored = `rm -rf ${record} ${mark}; cp -p /root/record ${record}; cp -p /root/mark ${mark}`;
    const refused = (what: string, made: string, said: string, version = "1.6.0") => {
      expect(rolled(version, made, restored), what).toBe(`1 0 Surogate Desktop: ${said}\n`);
      expect(standing(), what).toBe(installed);
      expect(root("find /tmp -mindepth 1 -maxdepth 1 -name 'tmp.*'; ls -A /opt/surogate/staging").stdout, what).toBe("");
    };
    const noServer = `${record} names no server to roll back from: run Surogate Desktop's install script again`;
    const notRoots = `${record} is not as Surogate Desktop's install leaves it: run its install script again`;
    const noSchema = "the installed 1.6.0 names no state schema: run Surogate Desktop's install script again";
    // A base that begins with a dash is options to curl: here a file of a user's own for it to
    // read its options from, which have it write a file of that user's bytes as root.
    const options = `url = "${base}/desktop/releases/1.6.0/manifest.json"\\nnext\\nurl = "file:///home/tester/payload"\\noutput = "/etc/written-by-root"\\n`;
    expect(root(`mkdir -p /home/tester/x/desktop/releases/1.6.0 && printf '${options}' >/home/tester/x/desktop/releases/1.6.0/manifest.json && echo theirs >/home/tester/payload && chown -R tester: /home/tester/x /home/tester/payload`).status).toBe(0);
    refused("a base that is options to curl", `echo '{"base":"-K/home/tester/x","channel":"stable"}' >${record}`, noServer);
    expect(root("test ! -e /etc/written-by-root").status).toBe(0);
    // Nor any other word that is no http or https URL as --base takes one, and no record that is more than one JSON document of a manifest's size.
    const padded = `printf '{"base":"${base}","more":"%s"}\\n' "$(head -c 4096 /dev/zero | tr '\\0' x)" >${record}`;
    for (const [what, made] of [
      ["a base with a new line in it", `printf '{"base":"${base}\\\\n/elsewhere"}\\n' >${record}`],
      ["a base that is no URL", `echo '{"base":"${base.replace("http://", "ftp://")}"}' >${record}`],
      ["a base that is a number", `echo '{"base":7}' >${record}`],
      ["a base that is a list", `echo '{"base":["${base}"]}' >${record}`],
      ["no base", `echo '{"channel":"stable"}' >${record}`],
      ["two documents", `printf '{"base":"${base}"}\\n{"base":"${base}"}\\n' >${record}`],
      ["no JSON", `echo '${base}' >${record}`],
      ["an empty record", `: >${record}`],
      ["a record of more than 4096 bytes", padded],
    ] as const) refused(what, made, noServer);
    // Nor from one with a slash more before its server, which curl would read a login from.
    refused("a base with a third slash before a login", `echo '{"base":"${base.replace("http://", "http:///user:secret@")}","channel":"stable"}' >${record}`,
      `${record} names no server to roll back from: run Surogate Desktop's install script again`);
    // Nor from a base with a user or a password in it, which no install writes: said as what it is.
    refused("a base with a password in it", `echo '{"base":"${base.replace("http://", "http://user:secret@")}","channel":"stable"}' >${record}`,
      `${record} names a base with a user or a password in it: run Surogate Desktop's install script again, with a base that names its server alone`);
    // Nor a record that is not root's own file, or that anyone else may write, whatever it names: here the base itself.
    for (const [what, made] of [
      ["a link to a user's file", `cp ${record} /home/tester/record.json && chown tester /home/tester/record.json && rm ${record} && ln -s /home/tester/record.json ${record}`],
      ["another user's", `chown tester ${record}`],
      ["one that all may write", `chmod 666 ${record}`],
      ["one that its group may write", `chmod 664 ${record}`],
      // Nor one with a set-id or a sticky bit, which no install gives it.
      ["one with the set-user-id bit", `chmod 4644 ${record}`],
      ["one with the set-group-id bit", `chmod 2644 ${record}`],
      ["one with the sticky bit", `chmod 1644 ${record}`],
      ["a pipe", `rm ${record} && mkfifo ${record}`],
      ["a folder", `rm ${record} && mkdir ${record}`],
    ] as const) refused(what, made, notRoots);
    refused("no record", `rm ${record}`, "Surogate Desktop is not installed: run its install script first");
    // Nor is a computer with a record and no version one that has an installed version's state to
    // compare: it is told the same, and not that a version with no name names no schema.
    expect(rolled("1.6.0", "mv -T /opt/surogate/current /root/current", "mv -T /root/current /opt/surogate/current")).toBe("1 0 Surogate Desktop: Surogate Desktop is not installed: run its install script first\n");
    expect(standing()).toBe(installed);
    // The installed version's mark says what it keeps for its users, and so which releases read
    // it: one that is not root's own file says nothing. Here each would have a release of an
    // earlier state schema taken.
    for (const [what, made] of [
      ["a link to a user's file", `echo '{"stateSchema":1}' >/home/tester/mark.json && chown tester /home/tester/mark.json && rm ${mark} && ln -s /home/tester/mark.json ${mark}`],
      ["another user's", `echo '{"stateSchema":1}' >${mark} && chown tester ${mark}`],
      ["one that others may write", `echo '{"stateSchema":1}' >${mark} && chmod 664 ${mark}`],
    ] as const) refused(`the installed mark: ${what}`, made, noSchema, "1.4.0");
    expect(root(`cmp /root/record ${record} && cmp /root/mark ${mark} && stat -c '%f %u' ${record} ${mark}`).stdout).toBe("81a4 0\n81a4 0\n");
    // The record is root's own word at any mode that lets root alone write it, and not only at the
    // one an install gives it: the base is asked, here for a version that is here whole.
    for (const mode of ["444", "600", "640", "400", "755"]) {
      const taken = rolled("1.6.0", `chmod ${mode} ${record}`, restored);
      expect(taken.startsWith("0 2 ") && taken.endsWith("Surogate Desktop: 1.6.0 is installed\n"), `${mode}: ${taken}`).toBe(true);
    }
    // A mark of the release's own bytes that is not root's own file at the mode an apply gives it
    // says nothing either, and what its refusal names mends it: to the install script, run again,
    // a folder with such a mark is not whole, and its release is unpacked again.
    for (const [what, made] of [
      ["closed to others", `chmod 600 ${mark}`],
      ["one that its group may write", `chmod 664 ${mark}`],
      ["a link to its own bytes", `cp ${mark} /root/same.json && rm ${mark} && ln -s /root/same.json ${mark}`],
      ["another user's", `chown tester ${mark}`],
    ] as const) {
      expect(rolled("1.6.0", made), what).toBe(`1 0 Surogate Desktop: ${noSchema}\n`);
      const again = install();
      expect(again.status, `${what}: ${again.stderr}`).toBe(0);
      expect(root(`stat -c '%f %u' ${mark} && cmp ${mark} /root/mark`).stdout, what).toBe("81a4 0\n");
      const taken = rolled("1.6.0");
      expect(taken.startsWith("0 2 ") && taken.endsWith("Surogate Desktop: 1.6.0 is installed\n"), `${what}: ${taken}`).toBe(true);
    }
  });

  it("rolls back with the system's own tools and into a folder of root's own, whatever PATH and TMPDIR root's own shell has, and through the user's proxy from a base whose name has a letter outside ASCII, for each of its three downloads", () => {
    expect(current()).toBe("/opt/surogate/versions/1.6.0");
    const installed = standing();
    // First on root's PATH, a folder with a tool under every name of the system's four folders:
    // each writes down that it ran, and then runs the system's own by its whole path. Its openssl
    // calls every signature good. None of them is run, by the script started by its name or read
    // by bash: not by a rollback to a release that no release key signed, and not by an install
    // of one.
    const standIns = [
      "mkdir -p /tmp/caller",
      'for tool in /usr/sbin/* /usr/bin/* /sbin/* /bin/*; do',
      '  [ -f "$tool" ] && [ -x "$tool" ] || continue',
      '  [ -e "/tmp/caller/$(basename "$tool")" ] || printf \'#!/bin/sh\\necho %s >>/tmp/caller/ran\\nexec %s "$@"\\n\' "$(basename "$tool")" "$tool" >"/tmp/caller/$(basename "$tool")"',
      "done",
      "printf '#!/bin/sh\\necho openssl >>/tmp/caller/ran\\nexit 0\\n' >/tmp/caller/openssl",
      "chmod 755 /tmp/caller/*",
      "ls /tmp/caller | wc -l",
    ].join("\n");
    expect(Number(root(standIns).stdout)).toBeGreaterThan(300);
    const unsigned = (started: string, said: string) => {
      const refused = root(`PATH=/tmp/caller:$PATH ${started}`);
      expect(refused.status, started).toBe(1);
      expect(refused.stderr.endsWith(`Surogate Desktop: ${said}${started.includes("--version") ? UNSIGNED : MISSED}\n`), `${started}: ${refused.stderr}`).toBe(true);
      expect(root("cat /tmp/caller/ran 2>/dev/null").stdout, started).toBe("");
      expect(standing(), started).toBe(installed);
    };
    unsigned("/opt/surogate-test/install.sh --version 1.6.1", `${base}/desktop/releases/1.6.1/manifest.json`);
    unsigned("/bin/bash -s -- --version 1.6.1 </opt/surogate-test/install.sh", `${base}/desktop/releases/1.6.1/manifest.json`);
    // The base's newest release is that one: the install's own root part asks the system's openssl too.
    latest("1.6.1");
    unsigned(`/opt/surogate-test/install.sh --base ${base}`, `${base}/desktop/latest.json`);
    unsigned(`/bin/bash -s -- --base ${base} </opt/surogate-test/install.sh`, `${base}/desktop/latest.json`);
    latest("1.6.0");
    // Nor is what it downloads put where root's own TMPDIR says, in a folder that is another's:
    // whoever owns that one could put a folder of their own in the download's name. Each folder a
    // rollback makes, and each the install makes, as mktemp was asked for it: one in /tmp, which
    // is root's and where no one renames what is another's, and one in root's own staging.
    const making = '#!/bin/sh\nmade="$(/opt/hold/mktemp "$@")" || exit\necho "$made" >>/tmp/made\necho "$made"\n';
    expect(root("mkdir -p /home/tester/tmp && chown tester: /home/tester/tmp").status).toBe(0);
    for (const started of ["--version 1.6.0", `--base ${base}`]) {
      const made = swapped("mktemp", making, [": >/tmp/made", `TMPDIR=/home/tester/tmp /opt/surogate-test/install.sh ${started} >/dev/null 2>&1; echo "$?"; cat /tmp/made`]).stdout.trim().split("\n");
      expect(made, started).toEqual(["0", expect.stringMatching(/^\/tmp\/tmp\.\w{10}$/), expect.stringMatching(/^\/opt\/surogate\/staging\/apply\.\w{6}$/)]);
    }
    expect(root("ls -A /home/tester/tmp").stdout).toBe("");
    // A release that is not here, from a base that no resolver has: all three of its files are
    // asked of the proxy the user's shell names, which sudo's own environment does not have, and
    // the base's letters are read as UTF-8 for each, or curl refuses the name before it asks.
    publish("1.5.9", undefined, { stateSchema: 2 });
    expect(root(`cp /etc/surogate/install.json /root/install.json && jq -c '.base = "http://b\u00fccher.invalid"' /root/install.json >/etc/surogate/install.json`).status).toBe(0);
    const proxied = as("tester", `curl -fsSL ${base}/desktop/install.sh | http_proxy=${base} bash -s -- --version 1.5.9`);
    expect(root("cp /root/install.json /etc/surogate/install.json").status).toBe(0);
    expect(proxied.status, proxied.stderr).toBe(0);
    expect(proxied.stdout).toContain("Surogate Desktop: downloading Surogate Desktop 1.5.9\n");
    expect(proxied.stdout).toContain("Surogate Desktop: 1.5.9 is installed\n");
    expect(root(`curl -fsS ${base}/desktop/releases/1.5.9/manifest.json | cmp - /opt/surogate/current/release.json`).status).toBe(0);
    expect(versions()).toEqual(["1.5.9", "1.6.0"]);
    // And to the version it replaced, which is here whole and later than the installed one: only the state schema and the keys refuse a version.
    const forward = rollBack("1.6.0");
    expect(forward.status, forward.stderr).toBe(0);
    expect(forward.stdout).not.toContain("downloading");
    expect(current()).toBe("/opt/surogate/versions/1.6.0");
    expect(versions()).toEqual(["1.5.9", "1.6.0"]);
  });

  it("rolls back to a version whose folder a later update removed, taking the release from its base again", () => {
    publish("1.7.0", undefined, { stateSchema: 2 });
    expect(install().status).toBe(0);
    expect(versions()).toEqual(["1.6.0", "1.7.0"]);
    // The update after it keeps 1.7.0 as the version before, and 1.6.0's folder goes.
    publish("1.8.0", undefined, { stateSchema: 2 });
    expect(install().status).toBe(0);
    expect(versions()).toEqual(["1.7.0", "1.8.0"]);
    const back = rollBack("1.6.0");
    expect(back.status, back.stderr).toBe(0);
    expect(back.stdout).toContain("Surogate Desktop: downloading Surogate Desktop 1.6.0\n");
    expect(back.stdout).toContain("Surogate Desktop: 1.6.0 is installed\n");
    expect(current()).toBe("/opt/surogate/versions/1.6.0");
    expect(versions()).toEqual(["1.6.0", "1.8.0"]);
    // Whole again: its mark, its program and its bwrap copy. The helper pkexec runs is still 1.8.0's.
    expect(root(`curl -fsS ${base}/desktop/releases/1.6.0/manifest.json | cmp - /opt/surogate/current/release.json && test -x /opt/surogate/current/surogate `
      + "&& cmp /usr/bin/bwrap /opt/surogate/current/bin/bwrap && cmp /opt/surogate/bin/release.json /opt/surogate/versions/1.8.0/release.json").status).toBe(0);
  });

  it("trusts the release keys of the newest release it has installed, whichever script asks and whatever is installed now: a key that release dropped signs nothing it takes again", () => {
    const [both, fresh] = [[PUBLIC, pem(next.publicKey)], [pem(next.publicKey)]];
    const schema = { stateSchema: 2 };
    const helper = () => root("sha256sum </opt/surogate/bin/surogate-apply-update; cat /opt/surogate/bin/release.json").stdout;
    // A rotation, release by release. The old key signs a release whose helper lists the old and the new.
    publish("3.0.0", keys.privateKey, schema, both);
    expect(install().status).toBe(0);
    // The new key signs the next. The base's install script lists the old key alone: the computer's
    // helper lists the new one too, and its list is the one that counts.
    publish("3.1.0", next.privateKey, schema, both);
    expect(install().status).toBe(0);
    // The release after it drops the old key.
    publish("3.2.0", next.privateKey, schema, fresh);
    expect(install().status).toBe(0);
    expect(current()).toBe("/opt/surogate/versions/3.2.0");
    const newest = helper();
    expect(root("cmp /opt/surogate/bin/surogate-apply-update /opt/surogate/versions/3.2.0/bin/surogate-apply-update && cmp /opt/surogate/bin/release.json /opt/surogate/versions/3.2.0/release.json").status).toBe(0);

    // What the old key signs from now on is no release to this computer: handed to the helper as the
    // app's update is, taken from the base by an install script that lists that key, as an old one
    // does, or asked for by its version.
    publish("3.9.0", keys.privateKey, schema, [PUBLIC]);
    // Each is told what is so for it, and not what mends a computer that missed a change of key:
    // this one has the new key, and the versions that are still here list the one that signed.
    const retired = (when: string) => {
      latest("3.9.0");
      expect(handed("3.9.0"), when).toMatchObject({ status: 1, stderr: `Surogate Desktop: the release's manifest${RETIRED}\n` });
      expect(install(), when).toMatchObject({ status: 1, stderr: `Surogate Desktop: ${base}/desktop/latest.json${RETIRED}\n` });
      for (const version of ["3.9.0", "3.0.0"]) {
        expect(rollBack(version), `${when}: ${version}`)
          .toMatchObject({ status: 1, stderr: `Surogate Desktop: ${base}/desktop/releases/${version}/manifest.json${RETIRED}\n` });
      }
      expect(helper(), when).toBe(newest);
    };
    retired("on the release that dropped it");

    // A rollback to the release before, whose own helper still lists the old key: the helper pkexec
    // runs, and its list, stay the newest release's.
    const back = rollBack("3.1.0");
    expect(back.status, back.stderr).toBe(0);
    expect(current()).toBe("/opt/surogate/versions/3.1.0");
    // How many keys each lists: the version's own helper two, and the one pkexec runs the new key alone.
    expect(root(`grep -c "^ *'-----BEGIN PUBLIC KEY-----$" /opt/surogate/current/bin/surogate-apply-update /opt/surogate/bin/surogate-apply-update`).stdout)
      .toBe("/opt/surogate/current/bin/surogate-apply-update:2\n/opt/surogate/bin/surogate-apply-update:1\n");
    retired("after a rollback");

    // A repair of the version rolled back to, by the helper and by the install script.
    expect(handed("3.1.0")).toMatchObject({ status: 0, stdout: "Surogate Desktop: 3.1.0 is installed\n" });
    latest("3.1.0");
    const repaired = install();
    expect(repaired.status, repaired.stderr).toBe(0);
    expect(repaired.stdout).toContain("Surogate Desktop: 3.1.0 is installed\n");
    expect(helper()).toBe(newest);

    // An update from there to a release that is still below the newest, and lists both keys: its
    // update removes the newest release's folder, and the helper stays that release's.
    publish("3.1.5", next.privateKey, schema, both);
    expect(install().status).toBe(0);
    expect(versions()).toEqual(["3.1.0", "3.1.5"]);
    retired("after a rollback and an update below the newest");

    // A release newer than the newest brings its own helper, and its mark.
    publish("3.3.0", next.privateKey, schema, fresh);
    expect(install().status).toBe(0);
    expect(root("cmp /opt/surogate/bin/surogate-apply-update /opt/surogate/versions/3.3.0/bin/surogate-apply-update && cmp /opt/surogate/bin/release.json /opt/surogate/versions/3.3.0/release.json").status).toBe(0);
  });

  it("runs none of its caller's tools as root where sudo keeps its caller's PATH: the shell that reads its root part is named by its whole path, and each root part's tools are the system's own", () => {
    expect(current()).toBe("/opt/surogate/versions/3.3.0");
    // A folder with a tool under every name of the system's four folders, first on the user's
    // PATH: each writes down that it ran where it runs as root, and then runs the system's own by
    // its whole path. As the user, whose tools they are, each only runs the system's.
    const standIns = [
      "mkdir -p /tmp/sudoer",
      'for tool in /usr/sbin/* /usr/bin/* /sbin/* /bin/*; do',
      '  [ -f "$tool" ] && [ -x "$tool" ] || continue',
      '  [ -e "/tmp/sudoer/$(basename "$tool")" ] || printf \'#!/bin/sh\\n[ "$(/usr/bin/id -u)" != 0 ] || echo %s >>/tmp/sudoer/ran\\nexec %s "$@"\\n\' "$(basename "$tool")" "$tool" >"/tmp/sudoer/$(basename "$tool")"',
      "done",
      "chmod 755 /tmp/sudoer/*",
      "ls /tmp/sudoer | wc -l",
    ].join("\n");
    expect(Number(root(standIns).stdout)).toBeGreaterThan(300);
    // A sudo with no secure_path: what it runs has its caller's PATH, and it looks there for what it is told to run.
    expect(root("echo 'Defaults !secure_path' >/etc/sudoers.d/callers-path && chmod 440 /etc/sudoers.d/callers-path").status).toBe(0);
    try {
      expect(as("tester", "PATH=/tmp/sudoer:$PATH /usr/bin/sudo /usr/bin/printenv PATH").stdout).toMatch(/^\/tmp\/sudoer:/);
      const asked = (args: string) => as("tester", `PATH=/tmp/sudoer:$PATH; /usr/bin/curl -fsSL ${base}/desktop/install.sh | /bin/bash -s -- ${args}`);
      // The install, a rollback and the removal: each has its root part read by a shell, through sudo.
      for (const [args, said] of [[`--base ${base}`, "3.3.0 is installed"], ["--version 3.3.0", "3.3.0 is installed"], ["--uninstall", "removed from this computer"]] as const) {
        const ran = asked(args);
        expect(ran.status, `${args}: ${ran.stderr}`).toBe(0);
        expect(ran.stdout, args).toContain(`Surogate Desktop: ${said}\n`);
        expect(root("cat /tmp/sudoer/ran 2>/dev/null").stdout, args).toBe("");
      }
    } finally {
      root("rm -f /etc/sudoers.d/callers-path");
    }
    expect(root("test ! -e /opt/surogate && test ! -e /etc/surogate").status).toBe(0);
    // Installed again, at a first install: by the list of the base's own script.
    publish("3.4.0", undefined, { stateSchema: 2 });
    expect(install().status).toBe(0);
    expect(current()).toBe("/opt/surogate/versions/3.4.0");
  });

  it("says what mends it where the base's release is signed by a key that an update stopped before its end brings: to run --version of that update first", () => {
    starting(["3.4.0", { stateSchema: 2 }]);
    expect(current()).toBe("/opt/surogate/versions/3.4.0");
    const helper = "/opt/surogate/bin/surogate-apply-update";
    const schema = { stateSchema: 2 };
    const added = [PUBLIC, pem(next.publicKey)];
    // An update that adds a key: the key the computer trusts signs it, and its helper lists that
    // key and the new one. It is stopped between its mark's rename and its helper's.
    publish("3.5.0", keys.privateKey, schema, added);
    const killed = String.raw`s|^    mv -T "\$work/helper" "\$HELPER"$|    kill -KILL $$|`;
    expect(root(`sed '${killed}' /opt/surogate-test/install.sh >/opt/surogate-test/stopped.sh && chmod 755 /opt/surogate-test/stopped.sh && ! cmp -s /opt/surogate-test/install.sh /opt/surogate-test/stopped.sh`).status).toBe(0);
    expect(root(`cd /home/tester && curl -fsSLO ${base}/desktop/releases/3.5.0/manifest.json -O ${base}/desktop/releases/3.5.0/manifest.json.sig `
      + `-o release.tar.gz ${base}/desktop/releases/3.5.0/surogate-desktop-3.5.0-linux-x64.tar.gz && /opt/surogate-test/stopped.sh --apply manifest.json manifest.json.sig release.tar.gz`).status).toBe(137);
    expect(root(`cmp /opt/surogate/bin/release.json /opt/surogate/versions/3.5.0/release.json && ! cmp -s ${helper} /opt/surogate/versions/3.5.0/bin/surogate-apply-update && readlink /opt/surogate/current`).stdout)
      .toBe("/opt/surogate/versions/3.4.0\n");
    // The base's newest then, which the added key alone signs. Before any lock, the keys asked are
    // still the helper's that is there, and the release is none to them: what is said names the
    // update to end first, to an install and to a rollback, and each leaves all as it was.
    publish("3.6.0", next.privateKey, schema, added);
    const stopped = standing();
    const first = (file: string) => `Surogate Desktop: ${base}/desktop/${file} is signed by a release key that the update to 3.5.0 brings, and that update was stopped before its end: `
      + "run Surogate Desktop's install script with --version 3.5.0 first\n";
    const plain = (file: string) => `Surogate Desktop: ${base}/desktop/${file}${MISSED}\n`;
    // A rollback is told no way on: which release an administrator asked for is theirs to know.
    const back = (file: string) => `Surogate Desktop: ${base}/desktop/${file}${UNSIGNED}\n`;
    const refused = (ran: { status: number | null; stderr: string }, said: string) => {
      expect(ran.status, ran.stderr).toBe(1);
      expect(ran.stderr.endsWith(said), ran.stderr).toBe(true);
      expect(standing()).toBe(stopped);
    };
    refused(install(), first("latest.json"));
    refused(rollBack("3.6.0"), first("releases/3.6.0/manifest.json"));
    // One that no key of either list signed is not signed, and no update's end would make it so.
    publish("3.7.0", other.privateKey, schema, added);
    refused(install(), plain("latest.json"));
    refused(rollBack("3.7.0"), back("releases/3.7.0/manifest.json"));
    latest("3.6.0");
    // Nor is the stopped update itself asked for first, where the base now serves it under the
    // added key's signature: that would send a person round in a circle.
    const signature = join(www(), "desktop", "releases", "3.5.0", "manifest.json.sig");
    const signedByOld = readFileSync(signature);
    writeFileSync(signature, sign(null, readFileSync(join(www(), "desktop", "releases", "3.5.0", "manifest.json")), next.privateKey));
    refused(rollBack("3.5.0"), back("releases/3.5.0/manifest.json"));
    writeFileSync(signature, signedByOld);
    // As it says: the update is ended by its --version, and the base's newest is then installed.
    const ended = rollBack("3.5.0");
    expect(ended.status, ended.stderr).toBe(0);
    expect(ended.stdout).toContain("Surogate Desktop: 3.5.0 is installed\n");
    expect(root(`cmp ${helper} /opt/surogate/versions/3.5.0/bin/surogate-apply-update && readlink /opt/surogate/current`).stdout).toBe("/opt/surogate/versions/3.5.0\n");
    const newest = install();
    expect(newest.status, newest.stderr).toBe(0);
    expect(current()).toBe("/opt/surogate/versions/3.6.0");
  });
  it("tells a computer that missed the release which brought a new key the one way on that its own keys check, that release by --version, and never to remove Surogate Desktop", () => {
    // From nothing, whatever the tests before it left.
    expect(uninstall().status).toBe(0);
    const schema = { stateSchema: 2 };
    const [old, both, fresh] = [[PUBLIC], [PUBLIC, pem(other.publicKey)], [pem(other.publicKey)]];
    publish("5.0.0", keys.privateKey, schema, old);
    expect(install().status).toBe(0);
    expect(current()).toBe("/opt/surogate/versions/5.0.0");
    // A rotation, which this computer sleeps through: the release that lists the new key beside
    // the old, and then the first that the new key alone signs. The newest script lists the new.
    publish("5.1.0", keys.privateKey, schema, both);
    publish("5.2.0", other.privateKey, schema, fresh);
    const script = readFileSync(join(www(), "desktop", "install.sh"), "utf8");
    writeFileSync(join(www(), "desktop", "install.sh"), withKeys(script, fresh));
    try {
      const before = standing();
      expect(install()).toMatchObject({ status: 1, stderr: `Surogate Desktop: ${base}/desktop/latest.json${MISSED}\n` });
      // A rollback to it is told plainly that it is not signed, and no more.
      expect(rollBack("5.2.0")).toMatchObject({ status: 1, stderr: `Surogate Desktop: ${base}/desktop/releases/5.2.0/manifest.json${UNSIGNED}\n` });
      // No word of any of them names a removal: that would throw this computer's keys away, and
      // what its keys did not sign may be a forgery as well as a release after a change of key.
      for (const words of [MISSED, UNSIGNED, RETIRED]) expect(words).not.toMatch(/uninstall|remove|install it again/);
      expect(standing()).toBe(before);
      // The way on: the release that brought the key, which the computer's own key signed.
      const brought = rollBack("5.1.0");
      expect(brought.status, brought.stderr).toBe(0);
      expect(install().status).toBe(0);
      expect(current()).toBe("/opt/surogate/versions/5.2.0");
      // A computer with nothing installed is told nothing of a key that changed: no key of the
      // script's own signed what the old key signs.
      expect(uninstall().status).toBe(0);
      latest("5.1.0");
      expect(install()).toMatchObject({ status: 1, stderr: `Surogate Desktop: ${base}/desktop/latest.json is not signed by Surogate's release key\n` });
      latest("5.2.0");
      expect(install().status).toBe(0);
      expect(current()).toBe("/opt/surogate/versions/5.2.0");
    } finally {
      writeFileSync(join(www(), "desktop", "install.sh"), script);
    }
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

for (const release of RELEASES) describe.skipIf(!ENABLED)(`the install script's company CA, on Ubuntu ${release}`, { timeout: 600_000 }, () => {
  // The install's own computer, behind a company's network that signs every site with its CA: the
  // base is served over TLS with a certificate that CA signed, which this computer's roots do not trust.
  const { it: box, docker, root, as, releaseOf, manifestOf, current, versions } = lab(release, INSTALL_LAB, ["--network", "host"]);
  let server: ChildProcess;
  let base: string;
  // The same base as a server of the public web serves it: its certificate signed by an authority
  // that is none of the company's.
  let publicServer: ChildProcess;
  let publicBase: string;
  let certs: string;
  const www = () => join(box.dir, "www");
  // Release *version* on the base, as the release job sends one, signed by *key*: its tarball, its
  // own manifest and signature beside it, and latest.json naming it.
  const publish = (version: string, key?: KeyObject) => {
    const tarball = releaseOf(version);
    const manifest = manifestOf(version, tarball, {}, key);
    const folder = join(www(), "desktop", "releases", version);
    mkdirSync(folder, { recursive: true });
    copyFileSync(tarball, join(folder, `surogate-desktop-${version}-linux-x64.tar.gz`));
    copyFileSync(join(box.dir, "manifest.json"), join(folder, "manifest.json"));
    copyFileSync(join(box.dir, "manifest.json.sig"), join(folder, "manifest.json.sig"));
    writeFileSync(join(www(), "desktop", "latest.json"), manifest);
  };
  // The user fetches the script through the company's network, as their own curl trusts it.
  const install = (args = "") => as("tester", `curl --cacert company.pem -fsSL ${base}/desktop/install.sh | bash -s -- --base ${base} ${args}`);
  const kept = () => root("cat /etc/surogate/ca.pem").stdout;
  // What a refused or failed run leaves in root's temp folder: nothing.
  const leftovers = () => root("find /tmp -mindepth 1 -maxdepth 1 -name 'tmp.*'").stdout;
  // The script's functions by themselves, as root's half has them when sudo ran it for tester: the
  // script without its last line, then *lines*, with no install around them.
  const alone = (lines: string) => root(`SUDO_UID=$(id -u tester) LC_ALL=C PATH=/usr/sbin:/usr/bin:/sbin:/bin bash -c '. <(sed "\\$d" /opt/surogate-test/install.sh); set -Eeuo pipefail; umask 022; settings; trap cleanup EXIT; ${lines}'`);
  // How many certificates the app's own reader takes from *text*, as the kept file would hold it.
  const appTakes = (text: string) => {
    writeFileSync(join(certs, "as-kept.pem"), text);
    return companyCertificates(join(certs, "as-kept.pem")).length;
  };
  // A certificate authority's certificate of the test's own, <name>.pem, with *extensions* beside
  // its basicConstraints.
  const authority = (name: string, ...extensions: string[]) => {
    const made = spawnSync("openssl", ["req", "-x509", "-subj", `/CN=${name}`, "-addext", "basicConstraints=critical,CA:TRUE", ...extensions.flatMap((extension) => ["-addext", extension]),
      "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:P-256", "-nodes", "-keyout", join(certs, `${name}.key`), "-out", join(certs, `${name}.pem`), "-days", "2"], { encoding: "utf8" });
    expect(made.status, made.stderr).toBe(0);
    return join(certs, `${name}.pem`);
  };

  beforeAll(async () => {
    certs = mkdtempSync(join(box.dir, "certs-"));
    const company = certificate(certs, "company");
    const another = certificate(certs, "another");
    // A certificate authority's file that is no CA of this network's.
    certificate(certs, "it");
    const site = certificate(certs, "site", "company");
    // What the administrator hands the script: the company's CA with its key beside it, as a CA's own
    // file holds it; both of the company's CAs in one file; a site's certificate, alone and before a
    // CA's file with its key; and no certificate.
    writeFileSync(join(certs, "company-with-key.pem"), readFileSync(company, "utf8") + readFileSync(join(certs, "company.key"), "utf8"));
    writeFileSync(join(certs, "both.pem"), readFileSync(another, "utf8") + readFileSync(company, "utf8"));
    writeFileSync(join(certs, "key-and-site.pem"), readFileSync(site, "utf8") + readFileSync(join(certs, "company-with-key.pem"), "utf8"));
    writeFileSync(join(certs, "notes.txt"), "the company's CA is on the intranet\n");
    // What the app takes for no certificate authority's, though it says it is one: a certificate
    // whose key may sign no certificate. And one that says nothing of its key, which the app takes.
    authority("signless", "keyUsage=critical,digitalSignature");
    const bare = authority("bare");
    writeFileSync(join(certs, "bare-and-company.pem"), readFileSync(bare, "utf8") + readFileSync(company, "utf8"));
    // Under a megabyte as it is handed over, and over one as openssl writes it again: a certificate
    // with a long comment in it, its base64 in one line, as many times as a megabyte holds.
    const wordy = readFileSync(authority("wordy", `nsComment=${"x".repeat(60_000)}`), "utf8");
    const unwrapped = `-----BEGIN CERTIFICATE-----\n${wordy.split("\n").filter((line) => line !== "" && !line.startsWith("-----")).join("")}\n-----END CERTIFICATE-----\n`;
    const times = Math.floor(1_048_576 / unwrapped.length);
    expect(times * wordy.length).toBeGreaterThan(1_048_576);
    writeFileSync(join(certs, "wide.pem"), unwrapped.repeat(times));
    // The company's CA in a file of a megabyte to the byte, and in one of a byte more.
    for (const [file, bytes] of [["megabyte.pem", 1_048_576], ["megabyte-and-one.pem", 1_048_577]] as const) {
      writeFileSync(join(certs, file), `${readFileSync(company, "utf8")}${"#".repeat(bytes - statSync(company).size - 1)}\n`);
    }
    // More certificates than a process may have files open, behind one that is no CA's.
    writeFileSync(join(certs, "thousand.pem"), readFileSync(site, "utf8") + readFileSync(company, "utf8").repeat(1100));
    for (const file of ["company.pem", "company-with-key.pem", "another.pem", "both.pem", "site.pem", "key-and-site.pem", "notes.txt", "it.pem", "signless.pem", "bare-and-company.pem", "wide.pem", "megabyte.pem", "megabyte-and-one.pem", "thousand.pem"]) {
      expect(docker(["cp", join(certs, file), `${box.container}:/home/tester/${file}`]).status).toBe(0);
    }
    expect(root("chown -R tester /home/tester").status).toBe(0);
    mkdirSync(join(www(), "desktop"), { recursive: true });
    copyFileSync(join(box.dir, "install.sh"), join(www(), "desktop", "install.sh"));
    publish("1.0.0");
    server = spawn(process.execPath, ["-e", SERVE, www(), site, join(certs, "site.key")], { stdio: ["ignore", "pipe", "inherit"] });
    const port = await new Promise<string>((resolve) => server.stdout!.once("data", (chunk: Buffer) => resolve(chunk.toString().trim())));
    base = `https://127.0.0.1:${port}`;
    const elsewhere = certificate(certs, "elsewhere", "it");
    publicServer = spawn(process.execPath, ["-e", SERVE, www(), elsewhere, join(certs, "elsewhere.key")], { stdio: ["ignore", "pipe", "inherit"] });
    publicBase = `https://127.0.0.1:${await new Promise<string>((resolve) => publicServer.stdout!.once("data", (chunk: Buffer) => resolve(chunk.toString().trim())))}`;
  }, 900_000);

  afterAll(() => {
    server.kill();
    publicServer.kill();
  });

  it("installs no release that no trusted key signed, whatever certificate authority it is given, and keeps no CA that came with one", () => {
    // The network's own CA, and a release at its base that another's key signed.
    publish("1.0.0", other.privateKey);
    const unsigned = install("--ca-cert company.pem");
    // Its downloads passed with the CA, and the last word is the signature's. apt's own lines stand before it at a first install.
    expect(unsigned.status).toBe(1);
    expect(unsigned.stderr).not.toContain("curl: (60)");
    expect(unsigned.stderr.endsWith(`Surogate Desktop: ${base}/desktop/latest.json is not signed by Surogate's release key\n`), unsigned.stderr).toBe(true);
    expect(root("test ! -e /etc/surogate/ca.pem && test ! -e /opt/surogate/current").status).toBe(0);
    expect(leftovers()).toBe("");
    publish("1.0.0");
  });

  it("installs the company's CA from --ca-cert for every user, and downloads the release through the network it signs", () => {
    // Without it, the script's own downloads do not trust the company's network.
    const untrusted = install();
    expect(untrusted.status).toBe(1);
    expect(untrusted.stderr).toContain("curl: (60) SSL certificate");
    expect(untrusted.stderr).toContain(`Surogate Desktop: could not download ${base}/desktop/latest.json\n`);
    expect(root("test ! -e /etc/surogate/ca.pem").status).toBe(0);
    const installed = install("--ca-cert company-with-key.pem");
    expect(installed.status, installed.stderr).toBe(0);
    expect(installed.stdout).toContain("Surogate Desktop: every user's Surogate, and their Chrome, Edge and Brave, trust the company's certificate authority in /etc/surogate/ca.pem\n");
    expect(installed.stdout).toContain("Surogate Desktop: 1.0.0 is installed\n");
    expect(current()).toBe("/opt/surogate/versions/1.0.0");
    // The certificate alone, as openssl writes it: never the key the file held beside it.
    expect(kept()).toBe(rewritten(join(certs, "company.pem")));
    expect(root("stat -c '%a %U' /etc/surogate/ca.pem").stdout).toBe("644 root\n");
    // certutil, which the app adds the CA to each user's NSS database with.
    expect(root("dpkg-query -W -f='${Status}\\n' libnss3-tools && test -x /usr/bin/certutil").stdout).toBe("install ok installed\n");
    expect(leftovers()).toBe("");
  });

  it("keeps the company's CA when run again without --ca-cert, takes the next one in its place, and never one its downloads fail with", () => {
    // certutil gone since, as with a package that another's removal took along: a run that keeps a CA brings it back.
    expect(root("apt-get remove -y -qq libnss3-tools >/dev/null 2>&1 && test ! -e /usr/bin/certutil").status).toBe(0);
    const again = install();
    expect(again.status, again.stderr).toBe(0);
    expect(kept()).toBe(rewritten(join(certs, "company.pem")));
    expect(root("test -x /usr/bin/certutil").status).toBe(0);
    // A certificate authority's file, but not this network's: the downloads fail with it, and the CA that works stays.
    const wrong = install("--ca-cert it.pem");
    expect(wrong.status).toBe(1);
    expect(wrong.stderr).toContain("curl: (60) SSL certificate");
    expect(wrong.stdout).not.toContain("trust the company's certificate authority");
    expect(kept()).toBe(rewritten(join(certs, "company.pem")));
    expect(leftovers()).toBe("");
    // The kept CA is root's own word, as the install record is: one that another may write is handed to no download.
    expect(root("chmod 666 /etc/surogate/ca.pem").status).toBe(0);
    expect(install()).toMatchObject({ status: 1, stderr: "Surogate Desktop: /etc/surogate/ca.pem is not as Surogate Desktop's install leaves it: run its install script again with --ca-cert\n" });
    // Given again, it is kept as the install leaves one.
    const both = install("--ca-cert both.pem");
    expect(both.status, both.stderr).toBe(0);
    expect(kept()).toBe(rewritten(join(certs, "another.pem")) + rewritten(join(certs, "company.pem")));
    expect(root("stat -c '%a %U' /etc/surogate/ca.pem").stdout).toBe("644 root\n");
  });

  it("refuses a file that holds no certificate authority's certificate, reads it as the user who asked and no further than a megabyte, and keeps the CA it had", () => {
    const refused = (file: string, said: string) => expect(install(`--ca-cert ${file}`), file).toMatchObject({ status: 1, stderr: `Surogate Desktop: ${said}\n` });
    // Before it asks for sudo.
    expect(install("--ca-cert missing.pem")).toMatchObject({ status: 1, stdout: "", stderr: "Surogate Desktop: missing.pem: no such file\n" });
    expect(install("--ca-cert")).toMatchObject({ status: 1, stdout: "", stderr: "Surogate Desktop: usage: install.sh [--base <url>] [--ca-cert <file>]\n" });
    refused("site.pem", "/home/tester/site.pem holds a certificate that is not a certificate authority's");
    refused("notes.txt", "/home/tester/notes.txt holds no PEM certificate");
    // A site's certificate before a CA's file with its key: nothing of it stays in root's temp folder, the key least of all.
    refused("key-and-site.pem", "/home/tester/key-and-site.pem holds a certificate that is not a certificate authority's");
    expect(leftovers()).toBe("");
    // Read as the user who asked: a file only root reads is refused, though root's half could read it.
    expect(root("mkdir -p /srv/secret && cp /home/tester/it.pem /srv/secret/ca.pem && chmod 600 /srv/secret/ca.pem").status).toBe(0);
    refused("/srv/secret/ca.pem", "/srv/secret/ca.pem is no file that tester can read");
    // A pipe in the file's place is refused at once, and never waited on; a file larger than a megabyte is not copied.
    expect(as("tester", "mkfifo fifo.pem && truncate -s 512M big.pem").status).toBe(0);
    refused("fifo.pem", "/home/tester/fifo.pem is no file that tester can read");
    refused("big.pem", "/home/tester/big.pem holds more than a megabyte, and a file of certificate authorities holds far less");
    expect(leftovers()).toBe("");
    expect(kept()).toBe(rewritten(join(certs, "another.pem")) + rewritten(join(certs, "company.pem")));
  });

  it("takes no newer release that no trusted key signed, with the CA it keeps or with one it is given, and keeps the CA it had", () => {
    publish("1.0.1", other.privateKey);
    for (const args of ["", "--ca-cert company.pem"]) {
      expect(install(args), args).toMatchObject({ status: 1, stderr: `Surogate Desktop: ${base}/desktop/latest.json${MISSED}\n` });
    }
    expect(current()).toBe("/opt/surogate/versions/1.0.0");
    expect(kept()).toBe(rewritten(join(certs, "another.pem")) + rewritten(join(certs, "company.pem")));
    expect(leftovers()).toBe("");
    copyFileSync(join(www(), "desktop", "releases", "1.0.0", "manifest.json"), join(www(), "desktop", "latest.json"));
  });

  it("takes --ca-cert before --base as after it, and keeps every refusal of a base with a CA beside it", () => {
    const script = `curl --cacert company.pem -fsSL ${base}/desktop/install.sh | bash -s --`;
    // Each before sudo is asked, and as root's half would refuse it too.
    for (const [args, said] of [
      ["--ca-cert company.pem --base http://user:secret@127.0.0.1:9", "a base with a user or a password in it is not taken: it would be written where every user of this computer reads it. Name the server alone"],
      ["--base https://user@surogate.example/ --ca-cert company.pem", "a base with a user or a password in it is not taken: it would be written where every user of this computer reads it. Name the server alone"],
      ["--ca-cert company.pem --base ftp://elsewhere.example", "usage: install.sh --base <http or https URL>"],
      ["--ca-cert company.pem --base http:///user:secret@127.0.0.1:9", "usage: install.sh --base <http or https URL>"],
      ["--ca-cert company.pem --base", "usage: install.sh --base <http or https URL>"],
      [`--base ${base} --ca-cert`, "usage: install.sh [--base <url>] [--ca-cert <file>]"],
      ["--ca-cert ''", "usage: install.sh [--base <url>] [--ca-cert <file>]"],
      ["--ca-cert company.pem --uninstall", "usage: install.sh [--base <url>] [--ca-cert <file>]"],
    ] as const) {
      for (const started of [as("tester", `${script} ${args}`), root(`cd /home/tester && /opt/surogate-test/install.sh ${args}`)]) {
        expect(started, args).toMatchObject({ status: 1, stdout: "", stderr: `Surogate Desktop: ${said}\n` });
      }
    }
    const reversed = as("tester", `${script} --ca-cert both.pem --base ${base}`);
    expect(reversed.status, reversed.stderr).toBe(0);
    expect(reversed.stdout).toContain("Surogate Desktop: every user's Surogate, and their Chrome, Edge and Brave, trust the company's certificate authority in /etc/surogate/ca.pem\n");
    expect(JSON.parse(root("cat /etc/surogate/install.json").stdout)).toEqual({ base, channel: "stable" });
    expect(kept()).toBe(rewritten(join(certs, "another.pem")) + rewritten(join(certs, "company.pem")));
  });

  it("keeps no file that the app would then refuse: a certificate whose key may sign none, more than a megabyte once it is written again, and what it keeps the app takes", () => {
    const refused = (file: string, said: string) => expect(alone(`company_ca /home/tester/${file}`), file).toMatchObject({ status: 1, stdout: "", stderr: `Surogate Desktop: ${said}\n` });
    // The app's own reader refuses each of the two as it would be kept.
    expect(() => appTakes(rewritten(join(certs, "signless.pem")))).toThrow("holds a certificate that is not a certificate authority's");
    refused("signless.pem", "/home/tester/signless.pem holds a certificate that is not a certificate authority's");
    expect(() => appTakes(rewritten(join(certs, "wordy.pem")).repeat(13))).toThrow("holds more than a megabyte");
    refused("wide.pem", "/home/tester/wide.pem holds more than a megabyte of certificates, and a file of certificate authorities holds far less");
    // A megabyte whole is read, and not a byte more, as the app reads its own.
    expect(appTakes(readFileSync(join(certs, "megabyte.pem"), "utf8"))).toBe(1);
    expect(() => appTakes(readFileSync(join(certs, "megabyte-and-one.pem"), "utf8"))).toThrow("holds more than a megabyte");
    expect(alone('company_ca /home/tester/megabyte.pem; cat "$GIVEN_CA"')).toMatchObject({ status: 0, stderr: "", stdout: rewritten(join(certs, "company.pem")) });
    refused("megabyte-and-one.pem", "/home/tester/megabyte-and-one.pem holds more than a megabyte, and a file of certificate authorities holds far less");
    // Each certificate is looked at, however many the file holds: the first here is a site's.
    refused("thousand.pem", "/home/tester/thousand.pem holds a certificate that is not a certificate authority's");
    // What it takes, the app takes: a CA that says nothing of its key, and every file kept so far.
    const taken = alone('company_ca /home/tester/bare-and-company.pem; cat "$GIVEN_CA"');
    expect(taken).toMatchObject({ status: 0, stderr: "", stdout: rewritten(join(certs, "bare.pem")) + rewritten(join(certs, "company.pem")) });
    expect(appTakes(taken.stdout)).toBe(2);
    expect(appTakes(kept())).toBe(2);
    expect(leftovers()).toBe("");
  });

  it("puts a CA given again in the place of whatever stands there that the app refuses: a link, a folder, a pipe, a file of another's, and one that the app's users cannot read", () => {
    const refusal = "Surogate Desktop: /etc/surogate/ca.pem is not as Surogate Desktop's install leaves it: run its install script again with --ca-cert\n";
    expect(root("cp /home/tester/it.pem /srv/elsewhere.pem").status).toBe(0);
    for (const [broken, handed] of [
      ["ln -s /srv/elsewhere.pem /etc/surogate/ca.pem", false],
      // A link that leads nowhere, and one to a folder, into which a rename by the link's name would put the file.
      ["ln -s /srv/nowhere /etc/surogate/ca.pem", false],
      ["mkdir -p /srv/folder && ln -s /srv/folder /etc/surogate/ca.pem", false],
      ["mkdir -p /etc/surogate/ca.pem/inside", false],
      ["mkfifo /etc/surogate/ca.pem", false],
      ["cp /home/tester/it.pem /etc/surogate/ca.pem && chown tester /etc/surogate/ca.pem", false],
      // Root's own, so root's downloads read it, and no word of it is a certificate; in a folder closed to the app's users.
      ["echo text >/etc/surogate/ca.pem && chmod 600 /etc/surogate/ca.pem && chmod 700 /etc/surogate", true],
    ] as const) {
      expect(root(`rm -rf /etc/surogate/ca.pem && ${broken}`).status, broken).toBe(0);
      // Without --ca-cert, what is not root's own word is handed to no download, and none is asked of the base.
      if (!handed) expect(alone(`fetch -fsS -o /dev/null ${base}/desktop/latest.json`), broken).toMatchObject({ status: 1, stdout: "", stderr: refusal });
      expect(alone("GIVEN_CA=/home/tester/both.pem; keep_company_ca"), broken).toMatchObject({ status: 0, stderr: "" });
      expect(root("stat -c '%F %a %U' /etc/surogate/ca.pem /etc/surogate").stdout, broken).toBe("regular file 644 root\ndirectory 755 root\n");
      expect(kept(), broken).toBe(readFileSync(join(certs, "both.pem"), "utf8"));
      expect(appTakes(kept()), broken).toBe(2);
    }
    // What the link named is as it was: the CA took the link's place, and was not written through it.
    expect(root("cmp /srv/elsewhere.pem /home/tester/it.pem && test ! -e /srv/nowhere && test -z \"$(ls -A /srv/folder)\"").status).toBe(0);
    expect(root("ls -A /etc/surogate").stdout).toBe("ca.pem\ninstall.json\n");
    const again = install();
    expect(again.status, again.stderr).toBe(0);
  });

  it("trusts the system's own roots as before, beside the company's CA and never in their place: a server that one of them signed is reached with the CA kept, and with one given", () => {
    const reached = (given = "") => alone(`${given}fetch -fsS -o /dev/null ${publicBase}/desktop/latest.json`);
    // Neither the kept CA nor a given one signed it, and the system does not trust who did.
    for (const given of ["", "GIVEN_CA=/home/tester/company.pem; "]) expect(reached(given).stderr, given).toContain("curl: (60) SSL certificate");
    expect(root("cp /home/tester/it.pem /usr/local/share/ca-certificates/it.crt && update-ca-certificates").status).toBe(0);
    for (const given of ["", "GIVEN_CA=/home/tester/company.pem; "]) expect(reached(given), given).toMatchObject({ status: 0, stderr: "" });
    expect(root("rm /usr/local/share/ca-certificates/it.crt && update-ca-certificates --fresh").status).toBe(0);
    expect(reached().stderr).toContain("curl: (60) SSL certificate");
  });

  it("asks a release's file of an http or https address alone, whatever address it is handed", () => {
    for (const address of ["file:///etc/hostname", "ftp://127.0.0.1:9/latest.json"]) {
      expect(alone(`fetch -fsS -o /tmp/fetched ${address}`), address).toMatchObject({ status: 1, stdout: "", stderr: expect.stringContaining("curl: (1) ") });
    }
    expect(root("test -e /etc/hostname && test ! -e /tmp/fetched").status).toBe(0);
  });

  it("rolls back through the network the company's CA signs, as it installs through it, a release it must download again among them", () => {
    // Two newer releases at the base, each installed: the first's folder goes with the second update.
    for (const version of ["1.1.0", "1.2.0"]) {
      publish(version);
      expect(install().status, version).toBe(0);
    }
    expect(versions()).toEqual(["1.1.0", "1.2.0"]);
    const rollBack = (version: string) => as("tester", `curl --cacert company.pem -fsSL ${base}/desktop/install.sh | bash -s -- --version ${version}`);
    const back = rollBack("1.0.0");
    expect(back.status, back.stderr).toBe(0);
    // Its manifest, its signature and its tarball, each through the CA.
    expect(back.stdout).toContain("Surogate Desktop: downloading Surogate Desktop 1.0.0\n");
    expect(back.stdout).toContain("Surogate Desktop: 1.0.0 is installed\n");
    expect(current()).toBe("/opt/surogate/versions/1.0.0");
    expect(leftovers()).toBe("");
    // The CA lets a server be reached, and signs no release: one that no trusted key signed is rolled back to through no CA.
    publish("1.1.5", other.privateKey);
    expect(rollBack("1.1.5")).toMatchObject({ status: 1, stderr: `Surogate Desktop: ${base}/desktop/releases/1.1.5/manifest.json${UNSIGNED}\n` });
    // And a kept CA that is not root's own word is handed to no download of a rollback's either.
    expect(root("chmod 666 /etc/surogate/ca.pem").status).toBe(0);
    expect(rollBack("1.1.0")).toMatchObject({ status: 1, stderr: "Surogate Desktop: /etc/surogate/ca.pem is not as Surogate Desktop's install leaves it: run its install script again with --ca-cert\n" });
    expect(current()).toBe("/opt/surogate/versions/1.0.0");
    expect(leftovers()).toBe("");
  });
});
