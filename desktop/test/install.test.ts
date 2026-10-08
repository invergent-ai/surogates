// The install script (release/install.sh) in Ubuntu containers, never on this computer: its
// --apply, which each version ships as bin/surogate-apply-update and runs as root. Releases are
// small stand-ins in the tarball's layout, signed by a key of the test's own. Behind
// SUROGATE_INSTALL_TESTS=1: it needs Docker, the ubuntu:24.04 and ubuntu:26.04 images, and the
// Ubuntu archive for apt.

import { execFile, spawnSync } from "node:child_process";
import { createHash, generateKeyPairSync, type KeyObject, randomBytes, sign } from "node:crypto";
import { copyFileSync, existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from "vitest";

const SCRIPT = fileURLToPath(new URL("../release/install.sh", import.meta.url));
const RELEASES = ["24.04", "26.04"] as const;
const ENABLED = process.env.SUROGATE_INSTALL_TESTS === "1";
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
  const it = { dir: "", container: "" };
  // No credentials of this user's reach the image's pull or the container.
  const env = () => ({ ...process.env, DOCKER_CONFIG: join(it.dir, "docker") });
  const docker = (args: string[]) => spawnSync("docker", args, { encoding: "utf8", env: env(), maxBuffer: 64 * 1024 * 1024 });
  const root = (command: string) => docker(["exec", it.container, "bash", "-c", command]);

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
      url: `releases/${version}/surogate-desktop-${version}-linux-x64.tar.gz`, sha256: sha256(readFileSync(tarball)), ...fields,
    })}\n`);
    writeFileSync(join(it.dir, "manifest.json"), manifest);
    writeFileSync(join(it.dir, "manifest.json.sig"), sign(null, manifest, key));
    return manifest;
  };
  const current = () => root("readlink /opt/surogate/current").stdout.trim();
  const versions = () => root("ls /opt/surogate/versions").stdout.trim().split("\n").filter(Boolean);

  beforeAll(async () => {
    it.dir = mkdtempSync(join(tmpdir(), "install-test-"));
    mkdirSync(join(it.dir, "docker"));
    mkdirSync(join(it.dir, "image"));
    writeFileSync(join(it.dir, "image", "Dockerfile"), [`FROM ubuntu:${release}`, ...setup].join("\n"));
    const image = `surogate-install-test:${release}-${sha256(Buffer.from(setup.join("\n"))).slice(0, 12)}`;
    // Off the event loop: a first build takes minutes, and vitest's RPC answers must still get in.
    await promisify(execFile)("docker", ["build", "-q", "-t", image, join(it.dir, "image")], { env: env() });
    // Root's own ptrace right, which Docker leaves out: root reads every process's program, as on a computer.
    it.container = docker(["run", "-d", "--rm", "--cap-add", "SYS_PTRACE", ...run, image, "sleep", "infinity"]).stdout.trim();
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

  return { it, docker, root, releaseOf, manifestOf, current, versions };
}

for (const release of RELEASES) describe.skipIf(!ENABLED)(`the install script's --apply, on Ubuntu ${release}`, { timeout: 120_000 }, () => {
  // What --apply needs, on a desktop's baseline: openssl, jq and bubblewrap, which the install
  // script installs, and nothing of Surogate's.
  const { it: box, docker, root, releaseOf, manifestOf, current, versions } = lab(release, [
    "RUN apt-get update && apt-get install -y --no-install-recommends openssl jq bubblewrap && rm -rf /var/lib/apt/lists/*",
    "RUN useradd -m tester",
  ]);
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

  beforeEach(() => {
    expect(root("rm -rf /opt/surogate").status).toBe(0);
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
      { version: "1.0.0\n", url: "releases/1.0.0\n/surogate-desktop-1.0.0\n-linux-x64.tar.gz" }, { sha256: `${hash}\n` }]) {
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
      "mkdir -p /opt/surogate && exec 8</opt/surogate && flock 8",
      "cp /home/tester/manifest.json /home/tester/swapped.json",
      `/opt/surogate-test/install.sh --apply ${files("/home/tester/swapped.json")} 8<&- &`,
      "until pgrep -x flock >/dev/null; do sleep 0.05; done",
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
      [(top) => symlinkSync("/etc", join(top, "resources", "etc")), "the release's archive links outside itself: resources/etc"],
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

  it("leaves current naming a whole version, whenever an update is cut short", () => {
    const first = releaseOf("1.0.0");
    manifestOf("1.0.0", first);
    expect(apply(first).status).toBe(0);
    // Large enough that its copy and unpacking take most of a second.
    const second = releaseOf("1.1.0", (top) => writeFileSync(join(top, "resources", "app", "large"), randomBytes(48 * 1024 * 1024)));
    manifestOf("1.1.0", second);
    stage(second);
    // Killed at 20 points along its way; each time current is whole, and the next apply finishes.
    const seen = new Set<string>();
    for (let ms = 50; ms <= 2000; ms += 100) {
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

  it("says what stops it: its arguments, a user who is not root, too little room, and bubblewrap missing", () => {
    const usage = "Surogate Desktop: usage: surogate-apply-update --apply <manifest> <signature> <tarball>\n";
    expect(root("/opt/surogate-test/install.sh --apply /home/tester/manifest.json")).toMatchObject({ status: 1, stderr: usage });
    expect(docker(["exec", "-u", "tester", box.container, "/opt/surogate-test/install.sh", "--apply", ...files().split(" ")]))
      .toMatchObject({ status: 1, stderr: "Surogate Desktop: applying a release needs administrator rights\n" });
    const tarball = releaseOf("1.0.0");
    manifestOf("1.0.0", tarball);
    stage(tarball);
    // Room for its copy and its tree, four times its size: none has that for a sparse 10 TiB.
    expect(root(`truncate -s 10T /home/tester/huge.tar.gz && /opt/surogate-test/install.sh --apply ${files(undefined, "/home/tester/huge.tar.gz")}`))
      .toMatchObject({ status: 1, stderr: expect.stringMatching(/^Surogate Desktop: \/opt\/surogate needs 41943040 MB free to apply this release, and has \d+ MB\n$/) });
    const missing = root(`mv /usr/bin/bwrap /usr/bin/bwrap.away; /opt/surogate-test/install.sh --apply ${files()}; said=$?; mv /usr/bin/bwrap.away /usr/bin/bwrap; exit $said`);
    expect(missing).toMatchObject({ status: 1, stderr: "Surogate Desktop: bubblewrap is missing: run Surogate Desktop's install script again\n" });
    expect(root("test -e /opt/surogate/current").status).toBe(1);
    // Run again with it there, the apply finishes.
    expect(apply(tarball)).toMatchObject({ status: 0, stdout: "Surogate Desktop: 1.0.0 is installed\n" });
  });
});
