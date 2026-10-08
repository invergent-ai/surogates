// The desktop's release (release/publish.sh): its manifest signed, then sent to a local S3
// (SeaweedFS in Docker) in R2's place. Behind SUROGATE_S3_TESTS=1: it needs Docker and the
// chrislusf/seaweedfs image.

import { spawnSync } from "node:child_process";
import { createHash, generateKeyPairSync, type KeyObject, randomBytes, verify } from "node:crypto";
import { copyFileSync, existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, statSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from "vitest";

import { KNOWN } from "../src/browser/choose.js";

const RELEASE = fileURLToPath(new URL("../release", import.meta.url));
const CREDENTIALS = { AWS_ACCESS_KEY_ID: "release", AWS_SECRET_ACCESS_KEY: "release-secret" };
const NAME = `sg-desktop-publish-${process.pid}`;
const sha256 = (data: Buffer) => createHash("sha256").update(data).digest("hex");
const keys = generateKeyPairSync("ed25519");
const pem = (key: KeyObject) => key.export({ type: "spki", format: "pem" }).toString().trim();
const secret = (key: KeyObject) => key.export({ type: "pkcs8", format: "pem" }).toString();
const PRIVATE = secret(keys.privateKey);
const PUBLIC = pem(keys.publicKey);
// *program* as publish.sh calls it, in *dir*/bin, each argument it is given written down first, in
// *dir*/<program>-argv; then *program* itself, or the lines *instead* gives for its path.
const recording = (dir: string, program: string, instead = (real: string) => [`exec '${real}' "$@"`]) => {
  const real = spawnSync("sh", ["-c", `command -v ${program}`], { encoding: "utf8" }).stdout.trim();
  mkdirSync(join(dir, "bin"), { recursive: true });
  writeFileSync(join(dir, "bin", program), ["#!/bin/sh", `printf '%s\\n' "$@" >> '${join(dir, `${program}-argv`)}'`, ...instead(real), ""].join("\n"), { mode: 0o755 });
};
// curl with one fault of a bucket's, for the object FAULT names as "<fault> <object>": "refused",
// its PUT answered 503; "unread", any other request for it answered 503; "changed", the copy read
// back from it a byte longer than what it was sent; "stalled" and "silent", its PUT and any other
// request for it stopped by curl itself as one that stalls is, with curl's status for it (28).
const faulty = (curl: string) => [
  "upload=; into=; before=",
  "for arg; do",
  '  [ "$before" = -T ] && upload=1',
  '  [ "$before" = -o ] && into="$arg"',
  '  before="$arg"',
  "done",
  // The request's address is its last argument.
  'case "$arg" in */desktop/"${FAULT#* }") fault="${FAULT%% *}" ;; *) fault= ;; esac',
  'if [ "$fault" = refused ] && [ -n "$upload" ]; then printf 503; exit 0; fi',
  'if [ "$fault" = unread ] && [ -z "$upload" ]; then printf 503; exit 0; fi',
  'if [ "$fault" = stalled ] && [ -n "$upload" ]; then exit 28; fi',
  'if [ "$fault" = silent ] && [ -z "$upload" ]; then exit 28; fi',
  `'${curl}' "$@" || exit`,
  'if [ "$fault" = changed ] && [ -z "$upload" ] && [ "$into" != /dev/null ]; then printf x >> "$into"; fi',
  "exit 0",
];
// install.sh with *trusted* in the release keys' place.
const trusting = (trusted = [PUBLIC]) => readFileSync(join(RELEASE, "install.sh"), "utf8")
  .replace(/RELEASE_KEYS=\(\n[^)]*\)/, `RELEASE_KEYS=(\n${trusted.map((key) => `    '${key}'`).join("\n")}\n  )`);
const NAME_OF = (version: string) => `surogate-desktop-${version}-linux-x64`;
// A release's tarball in *out*, small, in the layout package.sh gives one: a program in Electron's
// place, and as its root helper the install script beside publish.sh in *dir*/release, as
// package.sh packs the repository's. *change* edits its tree before it is tarred; *then* adds to
// the archive after it, as tar's own -r does, before it is compressed.
const packed = (dir: string, out: string, version: string, change: (top: string) => void = () => {}, then: (archive: string, name: string) => void = () => {}) => {
  const tree = mkdtempSync(join(dir, "tree-"));
  const top = join(tree, NAME_OF(version));
  mkdirSync(join(top, "bin"), { recursive: true });
  writeFileSync(join(top, "surogate"), "#!/bin/sh\n", { mode: 0o755 });
  copyFileSync(join(dir, "release", "install.sh"), join(top, "bin", "surogate-apply-update"));
  spawnSync("chmod", ["755", join(top, "bin", "surogate-apply-update")]);
  change(top);
  const archive = join(tree, "release.tar");
  expect(spawnSync("tar", ["--owner=0", "--group=0", "-C", tree, "-cf", archive, NAME_OF(version)]).status).toBe(0);
  then(archive, NAME_OF(version));
  const tarball = join(out, `${NAME_OF(version)}.tar.gz`);
  writeFileSync(tarball, spawnSync("gzip", ["-c", archive], { maxBuffer: 64 * 1024 * 1024 }).stdout);
  rmSync(tree, { recursive: true, force: true });
  return tarball;
};

describe("the desktop's release manifest", () => {
  let dir: string;
  let out: string;
  const tarball = () => join(out, "surogate-desktop-1.2.3-linux-x64.tar.gz");
  // publish.sh and an install.sh that trusts the test's key, beside each other as in the repository.
  const publish = (verb: string, version: string, env: Record<string, string> = {}) => spawnSync(join(dir, "release", "publish.sh"), [verb, version, out], {
    encoding: "utf8", env: { ...process.env, PATH: `${join(dir, "bin")}:${process.env.PATH ?? ""}`, ...env },
  });
  // Signed as the publish job signs: with the release key, and the hash the build's job gave for its tarball.
  const sign = (env: Record<string, string> = {}) => publish("sign", "1.2.3", { DESKTOP_RELEASE_KEY: PRIVATE, DESKTOP_TARBALL_SHA256: sha256(readFileSync(tarball())), ...env });

  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), "release-sign-"));
    mkdirSync(join(dir, "release"));
    copyFileSync(join(RELEASE, "publish.sh"), join(dir, "release", "publish.sh"));
    writeFileSync(join(dir, "release", "install.sh"), trusting());
    spawnSync("chmod", ["755", join(dir, "release", "publish.sh")]);
    out = join(dir, "out");
    mkdirSync(out);
    packed(dir, out, "1.2.3");
    recording(dir, "openssl");
  });

  it("signs the exact bytes of a manifest that names the tarball by its hash and its size", () => {
    expect(sign()).toMatchObject({ status: 0, stdout: `signed ${out}/manifest.json\n` });
    const manifest = readFileSync(join(out, "manifest.json"));
    // The shape install.sh's --apply checks, field for field, on one line.
    expect(manifest.toString()).toBe(`${JSON.stringify({
      version: "1.2.3", channel: "stable", platform: "linux", arch: "x64", url: "releases/1.2.3/surogate-desktop-1.2.3-linux-x64.tar.gz",
      sha256: sha256(readFileSync(tarball())), size: statSync(tarball()).size,
    })}\n`);
    expect(verify(null, manifest, keys.publicKey, readFileSync(join(out, "manifest.json.sig")))).toBe(true);
  });

  it("signs a manifest that the install script's own checks take: its signature, and each of its fields", () => {
    expect(sign().status).toBe(0);
    // What every install and every installed helper checks a release by, from the script's
    // functions without its last line, which runs it: the two cannot drift apart.
    const checked = spawnSync("bash", ["-c", `. <(sed '$d' "$1") && settings && signed "$2" "$2.sig" && release_of "$2"`, "_", join(dir, "release", "install.sh"), join(out, "manifest.json")], {
      encoding: "utf8",
    });
    expect(checked).toMatchObject({ status: 0, stdout: `1.2.3 ${sha256(readFileSync(tarball()))} ${statSync(tarball()).size}\n`, stderr: "" });
  });

  it("hands the release key to openssl through a pipe alone: never on a command line, where any process of the runner's could read it, and in no file", () => {
    expect(sign().status).toBe(0);
    // The key's own line of its PEM: the rest is every such key's.
    const body = PRIVATE.split("\n")[1] ?? "";
    expect(body).toMatch(/^[A-Za-z0-9+/]{64}$/);
    const argv = readFileSync(join(dir, "openssl-argv"), "utf8");
    expect(argv).toContain("-sign");
    expect(argv).not.toContain(body);
    // Every file openssl is given is a pipe, but the manifest it signs and the signature it writes.
    expect(argv.split("\n").filter((arg) => arg.startsWith("/") && !/^\/dev\/fd\/\d+$/.test(arg))).toEqual([join(out, "manifest.json"), join(out, "manifest.json.sig")]);
    // Beside the tarball, the manifest and its signature, and nothing else.
    expect(readdirSync(out).sort()).toEqual(["manifest.json", "manifest.json.sig", "surogate-desktop-1.2.3-linux-x64.tar.gz"]);
    expect(spawnSync("grep", ["-rlF", body, dir], { encoding: "utf8" }).stdout).toBe("");
  });

  it("signs with either key a rotating install.sh lists, and refuses a key whose public half it does not list", () => {
    const next = generateKeyPairSync("ed25519");
    writeFileSync(join(dir, "release", "install.sh"), trusting([PUBLIC, pem(next.publicKey)]));
    // The release of that install script: its root helper lists both.
    packed(dir, out, "1.2.3");
    for (const { privateKey, publicKey } of [keys, next]) {
      expect(sign({ DESKTOP_RELEASE_KEY: secret(privateKey) }).status).toBe(0);
      expect(verify(null, readFileSync(join(out, "manifest.json")), publicKey, readFileSync(join(out, "manifest.json.sig")))).toBe(true);
    }
    expect(sign({ DESKTOP_RELEASE_KEY: secret(generateKeyPairSync("ed25519").privateKey) })).toMatchObject({
      status: 1, stderr: "publish.sh: DESKTOP_RELEASE_KEY is not a key whose public half install.sh trusts\n",
    });
  });

  it("says that an install.sh does not end with the line that runs it, and reads no key from it: read without its last line, it would be run, and its refusal taken for a key that is not trusted", () => {
    // Its last line blank, as an editor leaves one, and in the place of the line that runs it, a
    // mark of the test's own: nothing of the script is run.
    const script = trusting().replace(/\nmain "\$@"\n$/, `\ntouch '${join(dir, "ran")}'\n\n`);
    expect(script.endsWith(`}\n\ntouch '${join(dir, "ran")}'\n\n`)).toBe(true);
    writeFileSync(join(dir, "release", "install.sh"), script);
    expect(sign()).toMatchObject({
      status: 1, stdout: "", stderr: 'publish.sh: install.sh does not end with the line that runs it (main "$@"): its release keys are not read\n',
    });
    expect(existsSync(join(dir, "ran"))).toBe(false);
    expect(readdirSync(out)).toEqual(["surogate-desktop-1.2.3-linux-x64.tar.gz"]);
  });

  it("stops with its usage at a version that is no x.y.z or a verb it does not have, and says so where the tarball is not there", () => {
    for (const [verb, version] of [["sign", "1.2"], ["sign", "1.2.3-rc1"], ["sign", "v1.2.3"], ["sign", "1.2.3/../1.2.3"], ["publish", "1.2.3"]] as const) {
      expect(publish(verb, version, { DESKTOP_RELEASE_KEY: PRIVATE, DESKTOP_TARBALL_SHA256: sha256(readFileSync(tarball())) }), `${verb} ${version}`)
        .toMatchObject({ status: 2, stdout: "", stderr: "usage: publish.sh sign|send <x.y.z> <out>\n" });
    }
    expect(publish("sign", "1.2.4", { DESKTOP_RELEASE_KEY: PRIVATE, DESKTOP_TARBALL_SHA256: sha256(readFileSync(tarball())) })).toMatchObject({
      status: 1, stdout: "", stderr: `publish.sh: ${out}/surogate-desktop-1.2.4-linux-x64.tar.gz is not there: run scripts/package.sh first\n`,
    });
    expect(readdirSync(out)).toEqual(["surogate-desktop-1.2.3-linux-x64.tar.gz"]);
  });

  it("refuses a tarball whose root helper is not the install script beside it, as a release unpacks it: with it go the release keys every later update is checked against", () => {
    const helper = `${NAME_OF("1.2.3")}/bin/surogate-apply-update`;
    // A helper of the build's own, which lists a key of the build's own: the same script but for its keys.
    const theirs = trusting([pem(generateKeyPairSync("ed25519").publicKey)]);
    const differs = `publish.sh: the tarball's root helper, ${helper}, is not the install.sh beside this script, byte for byte: every later update is checked by the release keys it lists\n`;
    const missing = `publish.sh: the tarball has no root helper of its own at ${helper}\n`;
    const cases: Array<[string, string, Parameters<typeof packed>[3], Parameters<typeof packed>[4]?]> = [
      ["a helper that lists another key", differs, (top) => writeFileSync(join(top, "bin", "surogate-apply-update"), theirs)],
      ["a helper a line longer", differs, (top) => writeFileSync(join(top, "bin", "surogate-apply-update"), `${trusting()}\n`)],
      ["no helper", missing, (top) => rmSync(join(top, "bin", "surogate-apply-update"))],
      ["a folder in the helper's place", missing, (top) => {
        rmSync(join(top, "bin", "surogate-apply-update"));
        mkdirSync(join(top, "bin", "surogate-apply-update"));
      }],
      // The install script itself, elsewhere in the tree, and a link to it where the helper is.
      ["a link in the helper's place", missing, (top) => {
        copyFileSync(join(top, "bin", "surogate-apply-update"), join(top, "install.sh"));
        rmSync(join(top, "bin", "surogate-apply-update"));
        symlinkSync("../install.sh", join(top, "bin", "surogate-apply-update"));
      }],
      // The install script where the helper is, and after it in the archive, under another name for
      // the same folder, a helper of the build's own: unpacked, the second replaces the first.
      ["a helper replaced as the archive is unpacked, through a link to its folder", differs, () => {}, (archive, name) => {
        const after = mkdtempSync(join(dir, "after-"));
        mkdirSync(join(after, name, "bin"), { recursive: true });
        writeFileSync(join(after, name, "bin", "surogate-apply-update"), theirs, { mode: 0o755 });
        symlinkSync("bin", join(after, name, "other"));
        expect(spawnSync("tar", ["--owner=0", "--group=0", "-C", after, "-rf", archive, `${name}/other`, `${name}/other/surogate-apply-update`]).status).toBe(0);
      }],
    ];
    for (const [what, said, change, then] of cases) {
      packed(dir, out, "1.2.3", change, then);
      expect(sign(), what).toMatchObject({ status: 1, stdout: "", stderr: said });
      // Nothing is signed, and nothing of the tarball's is left unpacked.
      expect(readdirSync(out), what).toEqual(["surogate-desktop-1.2.3-linux-x64.tar.gz"]);
    }
    // The last case's archive does hold the install script under the helper's own name, and unpacks to the other.
    expect(spawnSync("tar", ["-xzOf", tarball(), helper], { encoding: "utf8" }).stdout).toBe(trusting());
    // What is no archive at all.
    writeFileSync(tarball(), randomBytes(4096));
    expect(sign()).toMatchObject({ status: 1, stdout: "", stderr: `publish.sh: ${tarball()} could not be unpacked\n` });
    expect(readdirSync(out)).toEqual(["surogate-desktop-1.2.3-linux-x64.tar.gz"]);
    expect(readdirSync(tmpdir()).filter((name) => name.startsWith("release-unpacked-"))).toEqual([]);
  });

  it("refuses a tarball that is not the one the build's job made, by the hash that job gave: an artifact is its run's, and any job of the run may put another under its name", () => {
    const built = sha256(readFileSync(tarball()));
    // Another tarball under the build's name, whose helper is the install script too.
    packed(dir, out, "1.2.3", (top) => writeFileSync(join(top, "surogate"), "#!/bin/sh\n# another job's\n"));
    const found = sha256(readFileSync(tarball()));
    expect(found).not.toBe(built);
    expect(sign({ DESKTOP_TARBALL_SHA256: built })).toMatchObject({
      status: 1, stdout: "", stderr: `publish.sh: ${tarball()} is not the tarball the build made: its sha256 is ${found}, and the build's ${built}\n`,
    });
    // What is no sha256 names no tarball: nothing of it is compared, or said back.
    for (const hash of [found.toUpperCase(), found.slice(1), `${found} `, `${found}\n${found}`, "$(touch ran)"]) {
      expect(sign({ DESKTOP_TARBALL_SHA256: hash }), hash).toMatchObject({ status: 1, stdout: "", stderr: "publish.sh: DESKTOP_TARBALL_SHA256 is not a sha256, as the build's job gives its tarball's\n" });
    }
    expect(sign({ DESKTOP_TARBALL_SHA256: "" })).toMatchObject({ status: 1, stdout: "", stderr: expect.stringMatching(/DESKTOP_TARBALL_SHA256: parameter null or not set\n$/) });
    expect(readdirSync(out)).toEqual(["surogate-desktop-1.2.3-linux-x64.tar.gz"]);
    expect(sign({ DESKTOP_TARBALL_SHA256: found }).status).toBe(0);
  });

  it("lists, in the repository, one release key: the public half in install.sh", () => {
    const script = readFileSync(join(RELEASE, "install.sh"), "utf8");
    expect(script.match(/-----BEGIN PUBLIC KEY-----/g)).toHaveLength(1);
    expect(/RELEASE_KEYS=\(\n    '-----BEGIN PUBLIC KEY-----\nMCowBQYDK2VwAyEA[A-Za-z0-9+/]{43}=\n-----END PUBLIC KEY-----'\n  \)/.test(script)).toBe(true);
  });

  it("notes a missing browser where the app looks for one: each known browser's own program", () => {
    const listed = /for browser in ([^;]+); do/.exec(readFileSync(join(RELEASE, "install.sh"), "utf8"))?.[1]?.split(" ");
    expect(listed).toEqual(KNOWN.map(({ paths }) => paths.find((path) => !path.startsWith("/snap/") && !path.startsWith("/usr/bin/"))));
  });

  afterEach(() => {
    rmSync(dir, { recursive: true, force: true });
  });
});

describe.skipIf(process.env.SUROGATE_S3_TESTS !== "1")("the desktop's release on the bucket", { timeout: 60_000 }, () => {
  let dir: string;
  let endpoint: string;
  let bucket: string;
  // No credentials of this user's reach the container's start.
  const docker = (...args: string[]) => spawnSync("docker", args, { encoding: "utf8", env: { ...process.env, DOCKER_CONFIG: join(dir, "docker") } });
  const signed = (...args: string[]) => spawnSync("curl", ["-q", "-sS", "--aws-sigv4", "aws:amz:auto:s3", "--user", `${CREDENTIALS.AWS_ACCESS_KEY_ID}:${CREDENTIALS.AWS_SECRET_ACCESS_KEY}`, ...args], {
    maxBuffer: 64 * 1024 * 1024,
  });
  const object = (path: string) => {
    const got = signed("-f", `${endpoint}/${bucket}/desktop/${path}`);
    return got.status === 0 ? got.stdout : null;
  };
  // An object's Cache-Control, as the bucket serves it.
  const caching = (path: string) => /^cache-control: (.*)\r$/im.exec(signed("-f", "-I", `${endpoint}/${bucket}/desktop/${path}`).stdout.toString())?.[1] ?? null;
  // A release of *version*, packaged and signed, in an out folder of its own.
  const released = (version: string) => {
    const out = mkdtempSync(join(dir, "out-"));
    // A megabyte that does not compress: a send takes as long as one of a megabyte.
    const tarball = packed(dir, out, version, (top) => writeFileSync(join(top, "large"), randomBytes(1024 * 1024)));
    expect(spawnSync(join(dir, "release", "publish.sh"), ["sign", version, out], {
      env: { ...process.env, DESKTOP_RELEASE_KEY: PRIVATE, DESKTOP_TARBALL_SHA256: sha256(readFileSync(tarball)) },
    }).status).toBe(0);
    return out;
  };
  const send = (version: string, out: string, env: Record<string, string> = {}) => spawnSync(join(dir, "release", "publish.sh"), ["send", version, out], {
    encoding: "utf8", env: { ...process.env, PATH: `${join(dir, "bin")}:${process.env.PATH ?? ""}`, S3_ENDPOINT: endpoint, S3_BUCKET: bucket, ...CREDENTIALS, ...env },
  });

  beforeAll(() => {
    dir = mkdtempSync(join(tmpdir(), "release-publish-"));
    mkdirSync(join(dir, "docker"));
    mkdirSync(join(dir, "release"));
    copyFileSync(join(RELEASE, "publish.sh"), join(dir, "release", "publish.sh"));
    spawnSync("chmod", ["755", join(dir, "release", "publish.sh")]);
    writeFileSync(join(dir, "release", "install.sh"), trusting());
    recording(dir, "curl", faulty);
    writeFileSync(join(dir, "s3.json"), JSON.stringify({
      identities: [{ name: "release", credentials: [{ accessKey: CREDENTIALS.AWS_ACCESS_KEY_ID, secretKey: CREDENTIALS.AWS_SECRET_ACCESS_KEY }], actions: ["Admin", "Read", "Write", "List"] }],
    }));
    const started = docker("run", "-d", "--rm", "--name", NAME, "-p", "127.0.0.1::8333", "-v", `${join(dir, "s3.json")}:/etc/s3.json:ro`,
      "chrislusf/seaweedfs", "server", "-s3", "-s3.config=/etc/s3.json", "-dir=/data");
    expect(started.status, started.stderr).toBe(0);
    endpoint = `http://${docker("port", NAME, "8333").stdout.trim().split("\n")[0]}`;
  });

  afterAll(() => {
    docker("rm", "-f", NAME);
    rmSync(dir, { recursive: true, force: true });
  });

  // Each test's own bucket, made once SeaweedFS's S3 answers.
  let buckets = 0;
  beforeEach(async () => {
    bucket = `desktop-${(buckets += 1)}`;
    for (const end = Date.now() + 30_000; ; await new Promise((resolve) => setTimeout(resolve, 500))) {
      const made = spawnSync("curl", ["-q", "-sS", "-o", "/dev/null", "-w", "%{http_code}", "-X", "PUT", "--aws-sigv4", "aws:amz:auto:s3",
        "--user", `${CREDENTIALS.AWS_ACCESS_KEY_ID}:${CREDENTIALS.AWS_SECRET_ACCESS_KEY}`, `${endpoint}/${bucket}`], { encoding: "utf8" });
      if (made.stdout === "200") break;
      if (Date.now() > end) throw new Error(`SeaweedFS's S3 did not start: ${made.stdout} ${made.stderr}`);
    }
  });

  it("sends a release, the install script and latest.json, each as signed and read back, and never a release again", () => {
    const out = released("1.0.0");
    expect(send("1.0.0", out)).toMatchObject({ status: 0, stdout: "published desktop/releases/1.0.0 as desktop/latest.json\n" });
    const manifest = readFileSync(join(out, "manifest.json"));
    const signature = readFileSync(join(out, "manifest.json.sig"));
    expect(object("latest.json")?.equals(manifest)).toBe(true);
    expect(object("latest.json.sig")?.equals(signature)).toBe(true);
    expect(object("releases/1.0.0/manifest.json")?.equals(manifest)).toBe(true);
    expect(object("releases/1.0.0/manifest.json.sig")?.equals(signature)).toBe(true);
    expect(object("releases/1.0.0/surogate-desktop-1.0.0-linux-x64.tar.gz")?.equals(readFileSync(join(out, "surogate-desktop-1.0.0-linux-x64.tar.gz")))).toBe(true);
    expect(object("install.sh")?.equals(readFileSync(join(dir, "release", "install.sh")))).toBe(true);
    // What changes from release to release, never from a cache.
    expect(["install.sh", "latest.json", "latest.json.sig"].map(caching)).toEqual(["no-cache", "no-cache", "no-cache"]);
    expect(send("1.0.0", released("1.0.0"))).toMatchObject({ status: 1, stderr: "publish.sh: desktop/releases/1.0.0 is published already, and is not sent again\n" });
    expect(object("latest.json")?.equals(manifest)).toBe(true);
  });

  it("keeps latest.json at the newest version when an older line's release comes after it", () => {
    const newest = released("2.1.0");
    expect(send("2.1.0", newest).status).toBe(0);
    expect(send("2.0.5", released("2.0.5"))).toMatchObject({ status: 0, stdout: "published desktop/releases/2.0.5; desktop/latest.json stays 2.1.0\n" });
    expect(object("latest.json")?.equals(readFileSync(join(newest, "manifest.json")))).toBe(true);
    expect(object("releases/2.0.5/manifest.json")).not.toBeNull();
  });

  it("moves latest.json and the install script to a newer release, its numbers compared as numbers, and leaves both for an older line's", () => {
    const script = readFileSync(join(dir, "release", "install.sh"));
    expect(send("2.9.0", released("2.9.0")).stdout).toBe("published desktop/releases/2.9.0 as desktop/latest.json\n");
    // Letter by letter, 2.10.0 would come before 2.9.0, and 2.9.5 after 2.10.0.
    const newest = released("2.10.0");
    expect(send("2.10.0", newest).stdout).toBe("published desktop/releases/2.10.0 as desktop/latest.json\n");
    expect(object("latest.json")?.equals(readFileSync(join(newest, "manifest.json")))).toBe(true);
    // An older line's tag holds an install script of its own, signed for before it is changed here.
    const older = released("2.9.5");
    writeFileSync(join(dir, "release", "install.sh"), Buffer.concat([Buffer.from("# an older line's\n"), script]));
    try {
      expect(send("2.9.5", older).stdout).toBe("published desktop/releases/2.9.5; desktop/latest.json stays 2.10.0\n");
    } finally {
      writeFileSync(join(dir, "release", "install.sh"), script);
    }
    expect(object("install.sh")?.equals(script)).toBe(true);
    expect(object("latest.json")?.equals(readFileSync(join(newest, "manifest.json")))).toBe(true);
  });

  it("leaves a release unpublished when its send stops part-way, so that the next send sends it whole", () => {
    const out = released("1.0.0");
    expect(send("1.0.0", out, { FAULT: "refused latest.json" })).toMatchObject({ status: 1, stdout: "", stderr: "publish.sh: sending desktop/latest.json got 503\n" });
    // Its own manifest, the mark that it is published, is not there: with it there, the next send would be refused.
    expect(object("releases/1.0.0/manifest.json")).toBeNull();
    expect(send("1.0.0", out)).toMatchObject({ status: 0, stdout: "published desktop/releases/1.0.0 as desktop/latest.json\n" });
    const manifest = readFileSync(join(out, "manifest.json"));
    expect(object("latest.json")?.equals(manifest)).toBe(true);
    expect(object("releases/1.0.0/manifest.json")?.equals(manifest)).toBe(true);
    // Nothing of a send stays beside what was sent.
    expect(readdirSync(out).sort()).toEqual(["manifest.json", "manifest.json.sig", "surogate-desktop-1.0.0-linux-x64.tar.gz"]);
  });

  it("takes nothing for sent that the bucket does not give back as it was sent", () => {
    const tarball = "releases/1.0.0/surogate-desktop-1.0.0-linux-x64.tar.gz";
    expect(send("1.0.0", released("1.0.0"), { FAULT: `changed ${tarball}` })).toMatchObject({
      status: 1, stdout: "", stderr: `publish.sh: the bucket's desktop/${tarball} is not what was sent\n`,
    });
    expect(object("releases/1.0.0/manifest.json")).toBeNull();
    expect(object("latest.json")).toBeNull();
  });

  it("stops when the bucket does not say which release is its newest, rather than taking it to have none", () => {
    const newest = released("2.1.0");
    expect(send("2.1.0", newest).status).toBe(0);
    expect(send("2.0.5", released("2.0.5"), { FAULT: "unread latest.json" })).toMatchObject({ status: 1, stdout: "", stderr: "publish.sh: looking for desktop/latest.json got 503\n" });
    expect(object("latest.json")?.equals(readFileSync(join(newest, "manifest.json")))).toBe(true);
    expect(object("releases/2.0.5/surogate-desktop-2.0.5-linux-x64.tar.gz")).toBeNull();
  });

  it("keeps the bucket's secret off curl's command line, where any process of the runner's could read it", () => {
    rmSync(join(dir, "curl-argv"), { force: true });
    expect(send("1.0.0", released("1.0.0")).status).toBe(0);
    const argv = readFileSync(join(dir, "curl-argv"), "utf8");
    expect(argv).toContain("aws:amz:auto:s3");
    expect(argv).not.toContain(CREDENTIALS.AWS_SECRET_ACCESS_KEY);
  });

  it("bounds every request to the bucket, and tries again the sending of latest.json and of its signature: stopped between the two, a send would leave a manifest whose signature does not verify", () => {
    rmSync(join(dir, "curl-argv"), { force: true });
    expect(send("1.0.0", released("1.0.0")).status).toBe(0);
    // Each request as curl was given it: its arguments from one -q to the next, its address the last.
    const requests = readFileSync(join(dir, "curl-argv"), "utf8").split(/^-q\n/m).filter(Boolean).map((args) => args.trimEnd().split("\n"));
    expect(requests.length).toBeGreaterThan(10);
    // One that cannot connect, or that stalls for a minute, stops there, and not at the job's time limit.
    for (const args of requests) expect(args.join(" "), args.at(-1)).toContain("--connect-timeout 30 --speed-limit 1024 --speed-time 60");
    // Tried again: latest.json and its signature, each sent and each read back.
    const again = requests.filter((args) => args.includes("--retry"));
    expect(again.map((args) => args.at(-1)?.replace(/^.*\/desktop\//, ""))).toEqual(["latest.json", "latest.json", "latest.json.sig", "latest.json.sig"]);
    // Ubuntu 24.04's curl (8.5) ends 23, and does not try again, where the answer it would write
    // again goes to no file: each of these writes its answer to one.
    for (const args of again) expect(args[args.indexOf("-o") + 1], args.at(-1)).toMatch(/\/out-[^/]+\/sent$/);
  });

  it("says which request to the bucket curl stopped, and with what status of curl's, and leaves nothing of the send beside the release", () => {
    const tarball = "releases/1.0.0/surogate-desktop-1.0.0-linux-x64.tar.gz";
    for (const [fault, said] of [
      ["silent releases/1.0.0/manifest.json", "looking for desktop/releases/1.0.0/manifest.json stopped: curl exit 28"],
      ["silent latest.json", "looking for desktop/latest.json stopped: curl exit 28"],
      [`stalled ${tarball}`, `sending desktop/${tarball} stopped: curl exit 28`],
      [`silent ${tarball}`, `reading desktop/${tarball} back stopped: curl exit 28`],
    ] as const) {
      const out = released("1.0.0");
      expect(send("1.0.0", out, { FAULT: fault }), fault).toMatchObject({ status: 1, stdout: "", stderr: `publish.sh: ${said}\n` });
      expect(readdirSync(out).sort(), fault).toEqual(["manifest.json", "manifest.json.sig", "surogate-desktop-1.0.0-linux-x64.tar.gz"]);
    }
    expect(object("releases/1.0.0/manifest.json")).toBeNull();
  });

  it("stops at a bucket that refuses it, rather than taking the release for unpublished", () => {
    const refused = send("1.0.0", released("1.0.0"), { AWS_SECRET_ACCESS_KEY: "not-the-secret" });
    expect(refused.status).toBe(1);
    expect(refused.stderr).toBe("publish.sh: looking for desktop/releases/1.0.0/manifest.json got 403\n");
  });
});
