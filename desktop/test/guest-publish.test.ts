// The release's publish of the guest image (images/guest/publish.sh), against a local
// S3 (SeaweedFS in Docker) in R2's place, and a stand-in gh that lists the repository's
// releases as GitHub's API does. Behind SUROGATE_S3_TESTS=1: it needs Docker, the
// chrislusf/seaweedfs image, zstd and jq.

import { execFile, spawnSync } from "node:child_process";
import { createHash, randomBytes } from "node:crypto";
import { chmodSync, mkdirSync, mkdtempSync, readFileSync, renameSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";

const REPO = fileURLToPath(new URL("../..", import.meta.url));
const PUBLISH = join(REPO, "images/guest/publish.sh");
const CREDENTIALS = { AWS_ACCESS_KEY_ID: "release", AWS_SECRET_ACCESS_KEY: "release-secret" };
const NAME = `sg-publish-${process.pid}`;
const REPOSITORY = "invergent-ai/surogates";
const sha256 = (data: Buffer) => createHash("sha256").update(data).digest("hex");

describe.skipIf(process.env.SUROGATE_S3_TESTS !== "1")("the guest image's publish", { timeout: 60_000 }, () => {
  let dir: string;
  let endpoint: string;
  let bucket: string;
  let out: string;
  const key = spawnSync(join(REPO, "images/guest/inputs.sh"), { encoding: "utf8" }).stdout.trim();
  // No credentials of this user's reach the container's start.
  const docker = (...args: string[]) => spawnSync("docker", args, { encoding: "utf8", env: { ...process.env, DOCKER_CONFIG: join(dir, "docker") } });
  const publishEnv = (env: Record<string, string>) => ({
    ...process.env, PATH: `${join(dir, "bin")}:${process.env.PATH ?? ""}`, RELEASES: join(dir, "releases.json"), GITHUB_REPOSITORY: REPOSITORY, GH_TOKEN: "unused",
    S3_ENDPOINT: endpoint, S3_BUCKET: bucket, ...CREDENTIALS, ...env,
  });
  const publish = (verb: string, env: Record<string, string> = {}) => spawnSync(PUBLISH, [verb, out], { encoding: "utf8", env: publishEnv(env) });
  // The same, while this process goes on: what it waits for can change meanwhile.
  const publishing = (verb: string, env: Record<string, string> = {}) => new Promise<{ status: number | null; stdout: string; stderr: string }>((resolve) => {
    execFile(PUBLISH, [verb, out], { env: publishEnv(env) }, (error, stdout, stderr) => {
      resolve({ status: error ? (typeof error.code === "number" ? error.code : null) : 0, stdout, stderr });
    });
  });
  // A request to the bucket, signed as the job signs it.
  const signed = (...args: string[]) => spawnSync("curl", [
    "-q", "-sS", "-f", "--aws-sigv4", "aws:amz:auto:s3", "--user", `${CREDENTIALS.AWS_ACCESS_KEY_ID}:${CREDENTIALS.AWS_SECRET_ACCESS_KEY}`, ...args,
  ], { maxBuffer: 64 * 1024 * 1024 });
  // An object of the bucket, as a signed GET reads it, and one put there in its place.
  const object = (path: string) => signed(`${endpoint}/${bucket}/${path}`).stdout;
  const replace = (path: string, bytes: Buffer) => {
    const file = join(dir, "replacement");
    writeFileSync(file, bytes);
    expect(signed("-o", "/dev/null", "-T", file, "-H", `x-amz-content-sha256: ${sha256(bytes)}`, `${endpoint}/${bucket}/${path}`).status).toBe(0);
  };
  // The repository's releases, as GitHub's API lists them: one, which carries *manifest* as
  // desktop-vm-<key>.json when given, as the release that published the key attaches it.
  const released = (manifest?: Buffer) => {
    const assets = [{ name: "surogates-1.0.0.tar.gz", url: "file:///nowhere" }];
    if (manifest) {
      writeFileSync(join(dir, "released.json"), manifest);
      assets.push({ name: `desktop-vm-${key}.json`, url: `file://${join(dir, "released.json")}` });
    }
    // Whole at each read, as the API's list is.
    writeFileSync(join(dir, "releases.json.new"), JSON.stringify([{ tag_name: "v1.0.0", assets }]));
    renameSync(join(dir, "releases.json.new"), join(dir, "releases.json"));
  };

  beforeAll(async () => {
    dir = mkdtempSync(join(tmpdir(), "guest-publish-"));
    mkdirSync(join(dir, "docker"));
    // gh, as publish.sh calls it: the releases' list through its --jq filter, and an asset's bytes.
    mkdirSync(join(dir, "bin"));
    writeFileSync(join(dir, "bin", "gh"), [
      "#!/bin/sh",
      `if [ "$1 $2 $3 $4" = "api --paginate repos/${REPOSITORY}/releases --jq" ]; then exec jq -r "$5" "$RELEASES"; fi`,
      'if [ "$1 $2 $3" = "api -H Accept: application/octet-stream" ]; then exec cat "${4#file://}"; fi',
      'echo "gh: not as publish.sh calls it: $*" >&2; exit 1',
    ].join("\n"));
    chmodSync(join(dir, "bin", "gh"), 0o755);
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
    out = mkdtempSync(join(dir, "out-"));
    bucket = `releases-${(buckets += 1)}`;
    released();
    for (const end = Date.now() + 30_000; ; await new Promise((resolve) => setTimeout(resolve, 500))) {
      const made = spawnSync("curl", ["-q", "-sS", "-o", "/dev/null", "-w", "%{http_code}", "-X", "PUT", "--aws-sigv4", "aws:amz:auto:s3",
        "--user", `${CREDENTIALS.AWS_ACCESS_KEY_ID}:${CREDENTIALS.AWS_SECRET_ACCESS_KEY}`, `${endpoint}/${bucket}`], { encoding: "utf8" });
      if (made.stdout === "200") break;
      if (Date.now() > end) throw new Error(`SeaweedFS's S3 did not start: ${made.stdout} ${made.stderr}`);
    }
  });

  // What build.sh leaves in *out*: the two downloads, zstd's, and the manifest that names *key*.
  const built = (named = key) => {
    const files = [["rootfs.img", 3 * 1024 * 1024], ["vmlinuz", 64 * 1024]] as const;
    const entries = files.map(([name, bytes]) => {
      const data = randomBytes(bytes);
      writeFileSync(join(out, name), data);
      expect(spawnSync("zstd", ["-q", "-f", "--rm", join(out, name), "-o", join(out, `${name}.zst`)]).status).toBe(0);
      const download = readFileSync(join(out, `${name}.zst`));
      return { name, size: data.length, sha256: sha256(data), download: `${name}.zst`, downloadSize: download.length, downloadSha256: sha256(download) };
    });
    writeFileSync(join(out, "manifest.json"), `${JSON.stringify({ key: named, files: entries })}\n`);
  };
  const sentFiles = () => Object.fromEntries(["rootfs.img.zst", "vmlinuz.zst", "manifest.json"].map((file) => [file, readFileSync(join(out, file))]));

  it("sends a new image's files and then its manifest, checks what the bucket then holds, and from then on takes the manifest from the release that published it", () => {
    expect(publish("fetch")).toMatchObject({ status: 0, stdout: "missing\n" });
    built("0".repeat(64));
    expect(publish("send")).toMatchObject({ status: 1, stderr: `publish.sh: ${out}/manifest.json is not the build of desktop/vm/${key}\n` });
    built();
    const sent = sentFiles();
    expect(publish("send")).toMatchObject({ status: 0, stdout: `published desktop/vm/${key}\n` });
    for (const [file, bytes] of Object.entries(sent)) expect(object(`desktop/vm/${key}/${file}`).equals(bytes)).toBe(true);

    // The next release with the same key: the manifest its first release attached, for its tarball,
    // the bucket checked against it, and no second send.
    released(sent["manifest.json"]);
    out = mkdtempSync(join(dir, "out-"));
    expect(publish("fetch")).toMatchObject({ status: 0, stdout: "published\n" });
    expect(readFileSync(join(out, "manifest.json")).equals(sent["manifest.json"]!)).toBe(true);
    built();
    expect(publish("send")).toMatchObject({ status: 1, stderr: `publish.sh: desktop/vm/${key} is published already, and is not sent again\n` });
    expect(object(`desktop/vm/${key}/rootfs.img.zst`).equals(sent["rootfs.img.zst"]!)).toBe(true);
  });

  it("takes nothing of the bucket's on trust: a key there that no release carries, and a file or a manifest there that is not the release's, fail the job", () => {
    built();
    const sent = sentFiles();
    expect(publish("send").status).toBe(0);
    out = mkdtempSync(join(dir, "out-"));
    // No release carries it while the job waits: the job fails, and says how to recover without deleting the key.
    expect(publish("fetch", { PUBLISH_POLLS: "2", PUBLISH_POLL_S: "0" })).toMatchObject({
      status: 1, stdout: "",
      stderr: `publish.sh: desktop/vm/${key} is in the bucket, but no release of ours carries its manifest (desktop-vm-${key}.json): `
        + `re-run this job once the release run that sent it has attached it, or attach that run's desktop-vm-manifest artifact to its release as desktop-vm-${key}.json\n`,
    });
    released(sent["manifest.json"]);
    // The bucket's image swapped for another, compressed as the build compresses: its hashes are not the release's.
    const other = spawnSync("zstd", ["-q", "-c"], { input: randomBytes(3 * 1024 * 1024), maxBuffer: 8 * 1024 * 1024 }).stdout;
    replace(`desktop/vm/${key}/rootfs.img.zst`, other);
    expect(publish("fetch")).toMatchObject({ status: 1, stdout: "", stderr: expect.stringMatching(`publish.sh: the bucket's desktop/vm/${key}/rootfs.img.zst is not the release's\n$`) });
    replace(`desktop/vm/${key}/rootfs.img.zst`, sent["rootfs.img.zst"]!);
    expect(publish("fetch")).toMatchObject({ status: 0, stdout: "published\n" });
    // And its manifest swapped for one that names the other image.
    const manifest = JSON.parse(sent["manifest.json"]!.toString()) as { files: Array<{ name: string; downloadSha256: string }> };
    manifest.files[0]!.downloadSha256 = sha256(other);
    replace(`desktop/vm/${key}/manifest.json`, Buffer.from(`${JSON.stringify(manifest)}\n`));
    expect(publish("fetch")).toMatchObject({ status: 1, stdout: "", stderr: `publish.sh: the bucket's desktop/vm/${key}/manifest.json is not the release's\n` });
  });

  it("waits for the release run that sent a key to attach its manifest, as a second release pushed meanwhile does, and takes it once it is there", async () => {
    built();
    const sent = sentFiles();
    expect(publish("send").status).toBe(0);
    out = mkdtempSync(join(dir, "out-"));
    // The run that sent it attaches its manifest a moment later, once its release exists.
    const waiting = publishing("fetch", { PUBLISH_POLLS: "30", PUBLISH_POLL_S: "0.2" });
    setTimeout(() => released(sent["manifest.json"]), 1_000);
    expect(await waiting).toMatchObject({ status: 0, stdout: "published\n" });
    expect(readFileSync(join(out, "manifest.json")).equals(sent["manifest.json"]!)).toBe(true);
  });

  it("stops at a bucket that refuses it, rather than taking the image for missing", () => {
    const refused = publish("fetch", { AWS_SECRET_ACCESS_KEY: "not-the-secret" });
    expect(refused.status).toBe(1);
    expect(refused.stdout).toBe("");
    expect(refused.stderr).toMatch(new RegExp(`^publish.sh: looking for desktop/vm/${key}/manifest.json got 403\\n`));
  });
});
