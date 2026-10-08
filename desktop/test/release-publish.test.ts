// The desktop's release (release/publish.sh): its manifest signed, then sent to a local S3
// (SeaweedFS in Docker) in R2's place. Behind SUROGATE_S3_TESTS=1: it needs Docker and the
// chrislusf/seaweedfs image.

import { spawnSync } from "node:child_process";
import { createHash, generateKeyPairSync, type KeyObject, randomBytes, verify } from "node:crypto";
import { copyFileSync, mkdirSync, mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
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
// install.sh with *trusted* in the release keys' place.
const trusting = (trusted = [PUBLIC]) => readFileSync(join(RELEASE, "install.sh"), "utf8")
  .replace(/RELEASE_KEYS=\(\n[^)]*\)/, `RELEASE_KEYS=(\n${trusted.map((key) => `    '${key}'`).join("\n")}\n  )`);

describe("the desktop's release manifest", () => {
  let dir: string;
  let out: string;
  // publish.sh and an install.sh that trusts the test's key, beside each other as in the repository.
  const publish = (verb: string, version: string, env: Record<string, string> = {}) => spawnSync(join(dir, "release", "publish.sh"), [verb, version, out], {
    encoding: "utf8", env: { ...process.env, ...env },
  });

  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), "release-sign-"));
    mkdirSync(join(dir, "release"));
    copyFileSync(join(RELEASE, "publish.sh"), join(dir, "release", "publish.sh"));
    writeFileSync(join(dir, "release", "install.sh"), trusting());
    spawnSync("chmod", ["755", join(dir, "release", "publish.sh")]);
    out = join(dir, "out");
    mkdirSync(out);
    writeFileSync(join(out, "surogate-desktop-1.2.3-linux-x64.tar.gz"), randomBytes(4096));
  });

  it("signs the exact bytes of a manifest that names the tarball by its hash and its size", () => {
    expect(publish("sign", "1.2.3", { DESKTOP_RELEASE_KEY: PRIVATE })).toMatchObject({ status: 0, stdout: `signed ${out}/manifest.json\n` });
    const manifest = readFileSync(join(out, "manifest.json"));
    // The shape install.sh's --apply checks, field for field, on one line.
    expect(manifest.toString()).toBe(`${JSON.stringify({
      version: "1.2.3", channel: "stable", platform: "linux", arch: "x64", url: "releases/1.2.3/surogate-desktop-1.2.3-linux-x64.tar.gz",
      sha256: sha256(readFileSync(join(out, "surogate-desktop-1.2.3-linux-x64.tar.gz"))),
      size: statSync(join(out, "surogate-desktop-1.2.3-linux-x64.tar.gz")).size,
    })}\n`);
    expect(verify(null, manifest, keys.publicKey, readFileSync(join(out, "manifest.json.sig")))).toBe(true);
  });

  it("signs a manifest that the install script's own checks take: its signature, and each of its fields", () => {
    expect(publish("sign", "1.2.3", { DESKTOP_RELEASE_KEY: PRIVATE }).status).toBe(0);
    // What every install and every installed helper checks a release by, from the script's
    // functions without its last line, which runs it: the two cannot drift apart.
    const checked = spawnSync("bash", ["-c", `. <(sed '$d' "$1") && settings && signed "$2" "$2.sig" && release_of "$2"`, "_", join(dir, "release", "install.sh"), join(out, "manifest.json")], {
      encoding: "utf8",
    });
    expect(checked).toMatchObject({ status: 0, stdout: `1.2.3 ${sha256(readFileSync(join(out, "surogate-desktop-1.2.3-linux-x64.tar.gz")))} 4096\n`, stderr: "" });
  });

  it("signs with either key a rotating install.sh lists, and refuses a key whose public half it does not list", () => {
    const next = generateKeyPairSync("ed25519");
    writeFileSync(join(dir, "release", "install.sh"), trusting([PUBLIC, pem(next.publicKey)]));
    for (const { privateKey, publicKey } of [keys, next]) {
      expect(publish("sign", "1.2.3", { DESKTOP_RELEASE_KEY: secret(privateKey) }).status).toBe(0);
      expect(verify(null, readFileSync(join(out, "manifest.json")), publicKey, readFileSync(join(out, "manifest.json.sig")))).toBe(true);
    }
    expect(publish("sign", "1.2.3", { DESKTOP_RELEASE_KEY: secret(generateKeyPairSync("ed25519").privateKey) })).toMatchObject({
      status: 1, stderr: "publish.sh: DESKTOP_RELEASE_KEY is not a key whose public half install.sh trusts\n",
    });
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
    writeFileSync(join(out, `surogate-desktop-${version}-linux-x64.tar.gz`), randomBytes(1024 * 1024));
    expect(spawnSync(join(dir, "release", "publish.sh"), ["sign", version, out], { env: { ...process.env, DESKTOP_RELEASE_KEY: PRIVATE } }).status).toBe(0);
    return out;
  };
  const send = (version: string, out: string, env: Record<string, string> = {}) => spawnSync(join(dir, "release", "publish.sh"), ["send", version, out], {
    encoding: "utf8", env: { ...process.env, S3_ENDPOINT: endpoint, S3_BUCKET: bucket, ...CREDENTIALS, ...env },
  });

  beforeAll(() => {
    dir = mkdtempSync(join(tmpdir(), "release-publish-"));
    mkdirSync(join(dir, "docker"));
    mkdirSync(join(dir, "release"));
    copyFileSync(join(RELEASE, "publish.sh"), join(dir, "release", "publish.sh"));
    spawnSync("chmod", ["755", join(dir, "release", "publish.sh")]);
    writeFileSync(join(dir, "release", "install.sh"), trusting());
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

  it("stops at a bucket that refuses it, rather than taking the release for unpublished", () => {
    const refused = send("1.0.0", released("1.0.0"), { AWS_SECRET_ACCESS_KEY: "not-the-secret" });
    expect(refused.status).toBe(1);
    expect(refused.stderr).toBe("publish.sh: looking for desktop/releases/1.0.0/manifest.json got 403\n");
  });
});
