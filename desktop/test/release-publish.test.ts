// The desktop's release (release/publish.sh): its manifest written, then signed, then sent to a local S3
// (SeaweedFS in Docker) in R2's place. Behind SUROGATE_S3_TESTS=1: it needs Docker and the
// chrislusf/seaweedfs image.

import { spawn, spawnSync } from "node:child_process";
import { createHash, generateKeyPairSync, type KeyObject, randomBytes, verify } from "node:crypto";
import { copyFileSync, existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, renameSync, rmSync, statSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
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
// place, its app's package.json, which names its state schema, and as its root helper the install
// script beside publish.sh in *dir*/release, as package.sh packs the repository's. *change* edits
// its tree before it is tarred; *then* adds to the archive after it, as tar's own -r does, before
// it is compressed.
const packed = (dir: string, out: string, version: string, change: (top: string) => void = () => {}, then: (archive: string, name: string) => void = () => {}) => {
  const tree = mkdtempSync(join(dir, "tree-"));
  const top = join(tree, NAME_OF(version));
  mkdirSync(join(top, "bin"), { recursive: true });
  mkdirSync(join(top, "resources", "app"), { recursive: true });
  writeFileSync(join(top, "surogate"), "#!/bin/sh\n", { mode: 0o755 });
  writeFileSync(join(top, "resources", "app", "package.json"), JSON.stringify({ version, stateSchema: 1 }));
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
// A change of a release's tree: *app* as its app's package.json.
const withApp = (app: unknown) => (top: string) => writeFileSync(join(top, "resources", "app", "package.json"), JSON.stringify(app));
// Members added to a release's archive after its tree, as tar's own -r adds them, each group with
// its *mode*, whatever its mode here: a name that ends with / is a folder, added alone, and any
// other a file of *contents*. So a folder closed to its owner is the archive's, and never one of
// this computer's.
const added = (dir: string, ...groups: Array<[mode: string, members: string[], contents?: string]>) => (archive: string, name: string) => {
  for (const [mode, members, contents = ""] of groups) {
    const after = mkdtempSync(join(dir, "after-"));
    for (const member of members) {
      mkdirSync(join(after, name, member.endsWith("/") ? member : dirname(member)), { recursive: true });
      if (!member.endsWith("/")) writeFileSync(join(after, name, member), contents);
    }
    expect(spawnSync("tar", ["--owner=0", "--group=0", `--mode=${mode}`, "--no-recursion", "-C", after, "-rf", archive, ...members.map((member) => join(name, member))]).status).toBe(0);
  }
};
// A stand-in's lines (see recording) that say it was called, in *dir*/<program>-held, and then wait
// to be let go, by *dir*/go, for ten seconds at most: a test sends its signal meanwhile.
const held = (dir: string, program: string) => [
  `: > '${join(dir, `${program}-held`)}'`,
  `tries=0; while [ -d '${dir}' ] && [ ! -e '${join(dir, "go")}' ] && [ "$tries" -lt 200 ]; do sleep 0.05; tries=$((tries + 1)); done`,
];

describe("the desktop's release manifest", () => {
  let dir: string;
  let out: string;
  // The temporary folder each run unpacks its tarball in: the test's own. The computer's is every
  // run's on it at that moment, another test's among them.
  let tmp: string;
  const tarball = () => join(out, "surogate-desktop-1.2.3-linux-x64.tar.gz");
  // publish.sh and an install.sh that trusts the test's key, beside each other as in the repository.
  // Its environment is the test's own, without a release key that this computer's might hold.
  const steps = (env: Record<string, string>) => {
    const { DESKTOP_RELEASE_KEY: _held, ...own } = process.env;
    return { ...own, PATH: `${join(dir, "bin")}:${process.env.PATH ?? ""}`, TMPDIR: tmp, ...env };
  };
  const publish = (verb: string, version: string, env: Record<string, string> = {}) => spawnSync(join(dir, "release", "publish.sh"), [verb, version, out], { encoding: "utf8", env: steps(env) });
  // What the build's job says of its tarball: its hash, and its size in bytes.
  const built = () => ({ DESKTOP_TARBALL_SHA256: sha256(readFileSync(tarball())), DESKTOP_TARBALL_SIZE: String(statSync(tarball()).size) });
  // The publish job's first step: the manifest written of the tarball, by the hash the build's job
  // gave for it, with no release key anywhere.
  const describes = (env: Record<string, string> = {}) => publish("describe", "1.2.3", { DESKTOP_TARBALL_SHA256: built().DESKTOP_TARBALL_SHA256, ...env });
  // Its second: that manifest signed, with the release key and the build's two words.
  const signs = (env: Record<string, string> = {}) => publish("sign", "1.2.3", { DESKTOP_RELEASE_KEY: PRIVATE, ...built(), ...env });
  // Both, as the job runs them: the second only once the first has ended 0. *env* is the second's.
  const release = (env: Record<string, string> = {}) => {
    const described = describes();
    return described.status === 0 ? signs(env) : described;
  };
  // The first step, going on while the test sends it a signal: to the script's own shell alone,
  // or, started in a group of its own, to all it runs too, as a terminal's signal goes. *exited*
  // is the script's own end; *ended*, how it ended and what was said, once nothing of it still speaks.
  const describing = (group = false) => {
    const child = spawn(join(dir, "release", "publish.sh"), ["describe", "1.2.3", out], {
      detached: group, stdio: ["ignore", "pipe", "pipe"], env: steps({ DESKTOP_TARBALL_SHA256: built().DESKTOP_TARBALL_SHA256 }),
    });
    const { pid } = child;
    if (pid === undefined) throw new Error("the step did not start");
    const said = { stdout: "", stderr: "" };
    child.stdout.on("data", (data: Buffer) => { said.stdout += data.toString(); });
    child.stderr.on("data", (data: Buffer) => { said.stderr += data.toString(); });
    return {
      signal: (signal: NodeJS.Signals) => { process.kill(group ? -pid : pid, signal); },
      exited: new Promise<void>((resolve) => { child.once("exit", () => resolve()); }),
      ended: new Promise<{ status: number | null; signal: NodeJS.Signals | null; stdout: string; stderr: string }>((resolve) => {
        child.once("close", (status, signal) => resolve({ status, signal, ...said }));
      }),
    };
  };
  // tar as publish.sh calls it, still unpacking when a signal comes: it waits, then writes its last
  // member where it unpacks, if that folder is still there, and says which in tar-found.
  const unpacking = (real: string) => [
    "before=; into=",
    'for arg; do [ "$before" = -C ] && into="$arg"; before="$arg"; done',
    `'${real}' "$@" || exit`,
    ...held(dir, "tar"),
    `if [ -d "$into" ]; then echo last > "$into/last"; echo there; else echo gone; fi > '${join(dir, "tar-found")}'`,
  ];
  // Waits for a stand-in to say it was called (see held).
  const called = async (program: string) => {
    for (const end = Date.now() + 10_000; !existsSync(join(dir, `${program}-held`)); await new Promise((resolve) => setTimeout(resolve, 10))) {
      if (Date.now() > end) throw new Error(`${program} was never called`);
    }
  };
  // Lets the stand-in that waits go on, once a script that the signal ended at once would have
  // ended: one that waits for what it runs, or lets the signal by, is still there.
  const letGo = async (run: ReturnType<typeof describing>) => {
    await Promise.race([run.exited, new Promise((resolve) => setTimeout(resolve, 300))]);
    writeFileSync(join(dir, "go"), "");
  };
  // Before another run of one test: nothing of the one before it, nor of the stand-ins that held it.
  const again = (...programs: string[]) => {
    for (const file of [join(out, "manifest.json"), join(out, "manifest.json.sig"), join(dir, "go"), ...programs.map((program) => join(dir, `${program}-held`))]) rmSync(file, { force: true });
  };

  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), "release-sign-"));
    mkdirSync(join(dir, "release"));
    copyFileSync(join(RELEASE, "publish.sh"), join(dir, "release", "publish.sh"));
    writeFileSync(join(dir, "release", "install.sh"), trusting());
    spawnSync("chmod", ["755", join(dir, "release", "publish.sh")]);
    out = join(dir, "out");
    mkdirSync(out);
    tmp = join(dir, "tmp");
    mkdirSync(tmp);
    packed(dir, out, "1.2.3");
    recording(dir, "openssl");
  });

  it("writes a manifest that names the tarball by its hash and size, and the app's state schema, and then signs its exact bytes", () => {
    packed(dir, out, "1.2.3", withApp({ version: "1.2.3", stateSchema: 3 }));
    expect(describes()).toMatchObject({ status: 0, stdout: `wrote ${out}/manifest.json\n`, stderr: "" });
    const manifest = readFileSync(join(out, "manifest.json"));
    // The shape install.sh's --apply checks, field for field, on one line.
    expect(manifest.toString()).toBe(`${JSON.stringify({
      version: "1.2.3", channel: "stable", platform: "linux", arch: "x64", url: "releases/1.2.3/surogate-desktop-1.2.3-linux-x64.tar.gz",
      sha256: sha256(readFileSync(tarball())), size: statSync(tarball()).size, stateSchema: 3,
    })}\n`);
    // Nothing is signed by the step that reads the tarball; the next signs the manifest as it is.
    expect(readdirSync(out).sort()).toEqual(["manifest.json", "surogate-desktop-1.2.3-linux-x64.tar.gz"]);
    expect(signs()).toMatchObject({ status: 0, stdout: `signed ${out}/manifest.json\n`, stderr: "" });
    expect(readFileSync(join(out, "manifest.json")).equals(manifest)).toBe(true);
    expect(verify(null, manifest, keys.publicKey, readFileSync(join(out, "manifest.json.sig")))).toBe(true);
    // Written again of the same tarball, the manifest has no signature beside it that is another's.
    expect(describes().status).toBe(0);
    expect(readdirSync(out).sort()).toEqual(["manifest.json", "surogate-desktop-1.2.3-linux-x64.tar.gz"]);
  });

  it("writes no manifest of a tarball whose app names no state schema, as a release unpacks it", () => {
    const app = (top: string) => join(top, "resources", "app", "package.json");
    const cases: Array<[string, (top: string) => void]> = [
      ...[{ version: "1.2.3" }, { version: "1.2.3", stateSchema: 0 }, { version: "1.2.3", stateSchema: "1" }, { version: "1.2.3", stateSchema: 1.5 }, { version: "1.2.3", stateSchema: 1e15 }]
        .map((named): [string, (top: string) => void] => [JSON.stringify(named), withApp(named)]),
      ["no package of the app's", (top) => rmSync(app(top))],
      // Two documents, each with a schema: neither is the app's own word for it.
      ["a package of two documents", (top) => writeFileSync(app(top), '{"version":"1.2.3","stateSchema":1}\n{"version":"1.2.3","stateSchema":2}\n')],
      ["a package that is no object", (top) => writeFileSync(app(top), '[{"version":"1.2.3","stateSchema":1}]\n')],
      // A package of this computer's that names one, and a link to it where the app's is: the
      // app that is installed would read whatever its own computer has there.
      ["a link in its place", (top) => {
        writeFileSync(join(dir, "elsewhere.json"), JSON.stringify({ version: "1.2.3", stateSchema: 1 }));
        rmSync(app(top));
        symlinkSync(join(dir, "elsewhere.json"), app(top));
      }],
    ];
    for (const [what, change] of cases) {
      packed(dir, out, "1.2.3", change);
      expect(describes(), what).toMatchObject({ status: 1, stdout: "", stderr: "publish.sh: the tarball's resources/app/package.json names no stateSchema\n" });
      // Nothing is written, and nothing of the tarball's is left unpacked.
      expect(readdirSync(out), what).toEqual(["surogate-desktop-1.2.3-linux-x64.tar.gz"]);
      expect(readdirSync(tmp), what).toEqual([]);
    }
  });

  it("names the state schema of the app as a release unpacks it, where the archive holds another under its package's own name", () => {
    const app = `${NAME_OF("1.2.3")}/resources/app/package.json`;
    // The app's package where it is, at schema 3, and after it in the archive, under another name
    // for the same folder, one at schema 4: unpacked, as an install unpacks it, the second replaces the first.
    packed(dir, out, "1.2.3", withApp({ version: "1.2.3", stateSchema: 3 }), (archive, name) => {
      const after = mkdtempSync(join(dir, "after-"));
      mkdirSync(join(after, name, "resources", "app"), { recursive: true });
      writeFileSync(join(after, name, "resources", "app", "package.json"), JSON.stringify({ version: "1.2.3", stateSchema: 4 }));
      symlinkSync("resources/app", join(after, name, "other"));
      expect(spawnSync("tar", ["--owner=0", "--group=0", "-C", after, "-rf", archive, `${name}/other`, `${name}/other/package.json`]).status).toBe(0);
    });
    expect(describes().status).toBe(0);
    expect((JSON.parse(readFileSync(join(out, "manifest.json"), "utf8")) as { stateSchema: number }).stateSchema).toBe(4);
    // The archive does hold the first under the package's own name.
    expect(spawnSync("tar", ["-xzOf", tarball(), app], { encoding: "utf8" }).stdout).toBe(JSON.stringify({ version: "1.2.3", stateSchema: 3 }));
  });

  it("names the state schema of this package in each release it makes", () => {
    const { stateSchema } = JSON.parse(readFileSync(join(RELEASE, "..", "package.json"), "utf8")) as { stateSchema: unknown };
    expect(Number.isInteger(stateSchema) && (stateSchema as number) >= 1).toBe(true);
  });

  it("writes the state schemas the install script's own check takes, and no other: to the last below 10^15, and none that reads as below it only as it is written", () => {
    // The install script's check of a manifest's fields, from its functions without its last line.
    const taken = () => spawnSync("bash", ["-c", `. <(sed '$d' "$1") && settings && release_of "$2"`, "_", join(dir, "release", "install.sh"), join(out, "manifest.json")], { encoding: "utf8" });
    // The app's package with the number as it is written: this test's own JSON would round it first.
    const written = (schema: string) => (top: string) => writeFileSync(join(top, "resources", "app", "package.json"), `{"version":"1.2.3","stateSchema":${schema}}\n`);
    for (const [schema, signedAs] of [["1", 1], ["1.0", 1], ["999999999999999", 999999999999999]] as const) {
      again();
      packed(dir, out, "1.2.3", written(schema));
      expect(describes().status, schema).toBe(0);
      expect((JSON.parse(readFileSync(join(out, "manifest.json"), "utf8")) as { stateSchema: number }).stateSchema, schema).toBe(signedAs);
      expect(taken(), schema).toMatchObject({ status: 0, stdout: `1.2.3 ${sha256(readFileSync(tarball()))} ${statSync(tarball()).size}\n`, stderr: "" });
    }
    // A number is what it rounds to: 999999999999999.99 is 10^15. Compared as it is written, it
    // would pass for less, and be written as 1000000000000000, which no install takes.
    for (const schema of ["999999999999999.99", "1000000000000000", "1e15", "0", "0.5", "1.5", "-1"]) {
      again();
      packed(dir, out, "1.2.3", written(schema));
      expect(describes(), schema).toMatchObject({ status: 1, stdout: "", stderr: "publish.sh: the tarball's resources/app/package.json names no stateSchema\n" });
      expect(readdirSync(out), schema).toEqual(["surogate-desktop-1.2.3-linux-x64.tar.gz"]);
    }
  });

  it("signs a manifest that the install script's own checks take: its signature, and each of its fields", () => {
    expect(release().status).toBe(0);
    // What every install and every installed helper checks a release by, from the script's
    // functions without its last line, which runs it: the two cannot drift apart.
    // As on a computer with no helper yet, where the script's own list is the one that counts: never
    // the list of a Surogate Desktop that this computer has installed.
    const checked = spawnSync("bash", ["-c", `. <(sed '$d' "$1") && settings && HELPER="$3" && signed "$2" "$2.sig" && release_of "$2"`, "_",
      join(dir, "release", "install.sh"), join(out, "manifest.json"), join(dir, "no-helper")], { encoding: "utf8" });
    expect(checked).toMatchObject({ status: 0, stdout: `1.2.3 ${sha256(readFileSync(tarball()))} ${statSync(tarball()).size}\n`, stderr: "" });
  });

  it("hands the release key to openssl through a pipe alone: never on a command line, where any process of the runner's could read it, and in no file", () => {
    expect(release().status).toBe(0);
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

  it("reads the build's tarball only where no release key is: with one in its environment, set or empty, it starts nothing and writes nothing", () => {
    // Each program the step starts, as publish.sh calls it, written down.
    const started = () => readdirSync(dir).filter((name) => name.endsWith("-argv")).sort();
    for (const program of ["dirname", "sha256sum", "cut", "mktemp", "tar", "realpath", "cmp", "jq", "chmod", "rm", "stat"]) recording(dir, program);
    for (const key of [PRIVATE, ""]) {
      expect(describes({ DESKTOP_RELEASE_KEY: key }), key === "" ? "empty" : "set").toMatchObject({
        status: 1, stdout: "", stderr: "publish.sh: describe reads the build's tarball, and runs only where no release key is: DESKTOP_RELEASE_KEY is in its environment\n",
      });
      expect(started()).toEqual([]);
      expect(readdirSync(out)).toEqual(["surogate-desktop-1.2.3-linux-x64.tar.gz"]);
      expect(readdirSync(tmp)).toEqual([]);
    }
    // With none, it starts each of them, tar on the tarball among them.
    expect(describes().status).toBe(0);
    expect(started()).toEqual(["chmod-argv", "cmp-argv", "cut-argv", "dirname-argv", "jq-argv", "mktemp-argv", "realpath-argv", "rm-argv", "sha256sum-argv", "stat-argv", "tar-argv"]);
    expect(readFileSync(join(dir, "tar-argv"), "utf8")).toContain(`${tarball()}\n`);
  });

  it("signs with no tarball there: it opens none, and starts no program that is handed one", () => {
    expect(describes().status).toBe(0);
    // What a signing needs of the tarball is in the manifest and in the build's two words: the tarball itself is away.
    const said = built();
    renameSync(tarball(), join(dir, "elsewhere"));
    // Each program the step starts, as publish.sh calls it, written down: bash among them, which
    // the script itself is run by. Each first counts the variables of its environment that hold
    // the key's own line, under whatever name.
    const body = PRIVATE.split("\n")[1] ?? "";
    const counted = (real: string) => [`/usr/bin/env | /usr/bin/grep -cF '${body}' >> '${join(dir, "keyed")}'`, `exec '${real}' "$@"`];
    for (const program of ["bash", "dirname", "tail", "sed", "head", "jq", "cmp", "openssl", "tar", "sha256sum", "stat", "realpath", "mktemp", "cut"]) recording(dir, program, counted);
    expect(publish("sign", "1.2.3", { DESKTOP_RELEASE_KEY: PRIVATE, ...said })).toMatchObject({ status: 0, stdout: `signed ${out}/manifest.json\n`, stderr: "" });
    expect(verify(null, readFileSync(join(out, "manifest.json")), keys.publicKey, readFileSync(join(out, "manifest.json.sig")))).toBe(true);
    // No tar, and nothing that would unpack, hash or measure one.
    const started = readdirSync(dir).filter((name) => name.endsWith("-argv")).sort();
    expect(started).toEqual(["bash-argv", "cmp-argv", "dirname-argv", "head-argv", "jq-argv", "openssl-argv", "sed-argv", "tail-argv"]);
    // And none of what it starts is given the tarball's path, where it was or where it is: the manifest's alone.
    const given = started.map((name) => readFileSync(join(dir, name), "utf8")).join("");
    expect(given).toContain(`${join(out, "manifest.json")}\n`);
    expect(given).not.toContain(tarball());
    expect(given).not.toContain(join(dir, "elsewhere"));
    // The key is in the environment of the script's own start alone, as the job gives it: of none
    // of the programs the script starts. openssl has it from a pipe.
    const keyed = readFileSync(join(dir, "keyed"), "utf8").trimEnd().split("\n");
    expect(keyed.length).toBeGreaterThan(8);
    expect(keyed).toEqual(["1", ...keyed.slice(1).map(() => "0")]);
  });

  it("signs no manifest but the one of its version and of the build's tarball, written as the first step writes one: it is a file that another step wrote", () => {
    expect(describes().status).toBe(0);
    const written = readFileSync(join(out, "manifest.json"), "utf8");
    const fields = JSON.parse(written) as Record<string, unknown>;
    const line = (manifest: unknown) => `${JSON.stringify(manifest)}\n`;
    const noManifest = `publish.sh: ${out}/manifest.json is no manifest that install.sh takes: nothing is signed\n`;
    const notIts = `publish.sh: ${out}/manifest.json is not the manifest of 1.2.3 and of the tarball the build made, of sha256 ${built().DESKTOP_TARBALL_SHA256} and ${built().DESKTOP_TARBALL_SIZE} bytes: nothing is signed\n`;
    const others: Array<[string, string, string]> = [
      // What no install takes for a manifest.
      ["two documents", `${written}${written}`, noManifest],
      ["an object over two lines", written.replace(",", ",\n"), noManifest],
      ["an object without its newline", written.trimEnd(), noManifest],
      ["no state schema", line({ ...fields, stateSchema: undefined }), noManifest],
      ["another channel's", line({ ...fields, channel: "beta" }), noManifest],
      ["nothing", "", noManifest],
      // What an install would take, and is not this release's as the first step writes it.
      ["another version's", line({ ...fields, version: "1.2.4", url: "releases/1.2.4/surogate-desktop-1.2.4-linux-x64.tar.gz" }), notIts],
      ["another tarball's, by its hash", line({ ...fields, sha256: "0".repeat(64) }), notIts],
      ["another tarball's, by its size", line({ ...fields, size: (fields.size as number) + 1 }), notIts],
      ["with a field more", line({ ...fields, more: true }), notIts],
      ["its fields in another order", line({ stateSchema: fields.stateSchema, ...fields }), notIts],
      ["its schema written with a point", written.replace('"stateSchema":1}', '"stateSchema":1.0}'), notIts],
    ];
    for (const [what, manifest, said] of others) {
      expect(manifest, what).not.toBe(written);
      writeFileSync(join(out, "manifest.json"), manifest);
      expect(signs(), what).toMatchObject({ status: 1, stdout: "", stderr: said });
      expect(existsSync(join(out, "manifest.json.sig")), what).toBe(false);
    }
    // Nor one that is not there, or a link to one that is elsewhere.
    const notThere = { status: 1, stdout: "", stderr: `publish.sh: ${out}/manifest.json is not there: run publish.sh describe first\n` };
    rmSync(join(out, "manifest.json"));
    expect(signs()).toMatchObject(notThere);
    writeFileSync(join(dir, "elsewhere.json"), written);
    symlinkSync(join(dir, "elsewhere.json"), join(out, "manifest.json"));
    expect(signs()).toMatchObject(notThere);
    rmSync(join(out, "manifest.json"));
    // Nor by a word for the tarball's size that is no size in bytes, as the build's job gives one.
    writeFileSync(join(out, "manifest.json"), written);
    for (const size of ["0", "12a", "1e3", " 12", "-12", "1000000000000000"]) {
      expect(signs({ DESKTOP_TARBALL_SIZE: size }), size).toMatchObject({ status: 1, stdout: "", stderr: "publish.sh: DESKTOP_TARBALL_SIZE is not a size in bytes, as the build's job gives its tarball's\n" });
    }
    expect(signs({ DESKTOP_TARBALL_SIZE: "" })).toMatchObject({ status: 1, stdout: "", stderr: expect.stringMatching(/DESKTOP_TARBALL_SIZE: parameter null or not set\n$/) });
    expect(existsSync(join(out, "manifest.json.sig"))).toBe(false);
    // The one the first step wrote is signed.
    expect(signs()).toMatchObject({ status: 0, stdout: `signed ${out}/manifest.json\n` });
  });

  it("signs with either key a rotating install.sh lists, and refuses a key whose public half it does not list", () => {
    const next = generateKeyPairSync("ed25519");
    writeFileSync(join(dir, "release", "install.sh"), trusting([PUBLIC, pem(next.publicKey)]));
    // The release of that install script: its root helper lists both.
    packed(dir, out, "1.2.3");
    expect(describes().status).toBe(0);
    for (const { privateKey, publicKey } of [keys, next]) {
      expect(signs({ DESKTOP_RELEASE_KEY: secret(privateKey) }).status).toBe(0);
      expect(verify(null, readFileSync(join(out, "manifest.json")), publicKey, readFileSync(join(out, "manifest.json.sig")))).toBe(true);
    }
    expect(signs({ DESKTOP_RELEASE_KEY: secret(generateKeyPairSync("ed25519").privateKey) })).toMatchObject({
      status: 1, stderr: "publish.sh: DESKTOP_RELEASE_KEY is not a key whose public half install.sh trusts\n",
    });
  });

  it("says that an install.sh does not end with the line that runs it, and reads no key from it: read without its last line, it would be run, and its refusal taken for a key that is not trusted", () => {
    // Its last line blank, as an editor leaves one, and in the place of the line that runs it, a
    // mark of the test's own: nothing of the script is run.
    const script = trusting().replace(/\nmain "\$@"\n$/, `\ntouch '${join(dir, "ran")}'\n\n`);
    expect(script.endsWith(`}\n\ntouch '${join(dir, "ran")}'\n\n`)).toBe(true);
    writeFileSync(join(dir, "release", "install.sh"), script);
    expect(signs()).toMatchObject({
      status: 1, stdout: "", stderr: 'publish.sh: install.sh does not end with the line that runs it (main "$@"): its release keys are not read\n',
    });
    expect(existsSync(join(dir, "ran"))).toBe(false);
    expect(readdirSync(out)).toEqual(["surogate-desktop-1.2.3-linux-x64.tar.gz"]);
  });

  it("stops with its usage at a version that is no x.y.z or a verb it does not have, and says so where the tarball is not there to describe, or the manifest to sign", () => {
    // A part with a zero before it is no version either: dpkg reads 1.2.03 as 1.2.3, a second spelling of one release.
    for (const [verb, version] of [["sign", "1.2"], ["sign", "1.2.3-rc1"], ["sign", "v1.2.3"], ["sign", "1.2.3/../1.2.3"], ["sign", "1.2.03"], ["describe", "01.2.3"], ["send", "1.02.3"], ["publish", "1.2.3"]] as const) {
      expect(publish(verb, version, verb === "describe" ? built() : { DESKTOP_RELEASE_KEY: PRIVATE, ...built() }), `${verb} ${version}`)
        .toMatchObject({ status: 2, stdout: "", stderr: "usage: publish.sh describe|sign|send <x.y.z> <out>\n" });
    }
    expect(publish("describe", "1.2.4", built())).toMatchObject({
      status: 1, stdout: "", stderr: `publish.sh: ${out}/surogate-desktop-1.2.4-linux-x64.tar.gz is not there: run scripts/package.sh first\n`,
    });
    expect(signs()).toMatchObject({ status: 1, stdout: "", stderr: `publish.sh: ${out}/manifest.json is not there: run publish.sh describe first\n` });
    expect(readdirSync(out)).toEqual(["surogate-desktop-1.2.3-linux-x64.tar.gz"]);
  });

  it("refuses a tarball whose root helper is not the install script beside it, as a release unpacks it: with it go the release keys every later update is checked against", () => {
    const helper = `${NAME_OF("1.2.3")}/bin/surogate-apply-update`;
    // A helper of the build's own, which lists a key of the build's own: the same script but for its keys.
    const theirs = trusting([pem(generateKeyPairSync("ed25519").publicKey)]);
    const differs = `publish.sh: the tarball's root helper, ${helper}, is not the install.sh beside this script, byte for byte: every later update is checked by the release keys it lists\n`;
    const missing = `publish.sh: the tarball has no root helper of its own at ${helper}\n`;
    const broken = `publish.sh: ${tarball()} could not be unpacked\n`;
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
      // Refused with folders closed to their owner in it: what was unpacked goes all the same.
      ["a helper that lists another key, beside a folder its owner may not write", differs, (top) => writeFileSync(join(top, "bin", "surogate-apply-update"), theirs),
        added(dir, ["0555", ["closed/", "closed/file"]])],
      ["the install script in a folder its owner may not open", missing, () => {}, added(dir, ["0000", ["bin/"]])],
      // The helper's folder closed once tar has left it, and then a second helper for it, which only root's tar could put there.
      ["a second helper for a folder closed to its owner by then", broken, () => {},
        added(dir, ["0555", ["bin/"]], ["0755", ["surogate"], "#!/bin/sh\n"], ["0755", ["bin/surogate-apply-update"], theirs])],
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
      expect(describes(), what).toMatchObject({ status: 1, stdout: "", stderr: said });
      // Nothing is written, and nothing of the tarball's is left unpacked.
      expect(readdirSync(out), what).toEqual(["surogate-desktop-1.2.3-linux-x64.tar.gz"]);
      expect(readdirSync(tmp), what).toEqual([]);
    }
    // The last case's archive does hold the install script under the helper's own name, and unpacks to the other.
    expect(spawnSync("tar", ["-xzOf", tarball(), helper], { encoding: "utf8" }).stdout).toBe(trusting());
    // What is no archive at all.
    writeFileSync(tarball(), randomBytes(4096));
    expect(describes()).toMatchObject({ status: 1, stdout: "", stderr: broken });
    expect(readdirSync(out)).toEqual(["surogate-desktop-1.2.3-linux-x64.tar.gz"]);
    expect(readdirSync(tmp)).toEqual([]);
  });

  it("signs a tarball whatever the modes of its folders, which are the build's, and leaves nothing of it unpacked: the helper that installs it unpacks and removes it as root", () => {
    const cases: Array<[string, ReturnType<typeof added>]> = [
      ["as package.sh packs it", added(dir)],
      ["a folder its owner may not write", added(dir, ["0555", ["closed/", "closed/file"]])],
      ["folders their owner may not open, one in the other", added(dir, ["0000", ["shut/", "shut/inner/", "shut/inner/file"]])],
      ["its top folder and the helper's, closed once both are unpacked", added(dir, ["0555", ["./", "bin/"]])],
    ];
    for (const [what, then] of cases) {
      again();
      packed(dir, out, "1.2.3", undefined, then);
      expect(release(), what).toMatchObject({ status: 0, stdout: `signed ${out}/manifest.json\n`, stderr: "" });
      expect(verify(null, readFileSync(join(out, "manifest.json")), keys.publicKey, readFileSync(join(out, "manifest.json.sig"))), what).toBe(true);
      expect(readdirSync(tmp), what).toEqual([]);
    }
  });

  it("ends as a signal ends it, with no manifest written and nothing left unpacked, once what unpacks the tarball has ended: removed beside a tar that still writes, the folder keeps what tar writes after", { timeout: 60_000 }, async () => {
    recording(dir, "tar", unpacking);
    for (const [signal, status] of [["SIGHUP", 129], ["SIGINT", 130], ["SIGPIPE", 141], ["SIGTERM", 143]] as const) {
      again("tar");
      // To the script's own shell alone: tar goes on.
      const run = describing();
      await called("tar");
      run.signal(signal);
      await letGo(run);
      const ended = await run.ended;
      expect(readFileSync(join(dir, "tar-found"), "utf8"), signal).toBe("there\n");
      expect(ended, signal).toEqual({ status, signal: null, stdout: "", stderr: "" });
      expect(readdirSync(out), signal).toEqual(["surogate-desktop-1.2.3-linux-x64.tar.gz"]);
      expect(readdirSync(tmp), signal).toEqual([]);
    }
    // To all it runs too, tar among them, and with folders in the tarball that their owner may not write or open.
    again("tar");
    packed(dir, out, "1.2.3", undefined, added(dir, ["0555", ["closed/", "closed/file"]], ["0000", ["shut/", "shut/file"]]));
    const run = describing(true);
    await called("tar");
    run.signal("SIGTERM");
    await letGo(run);
    expect(await run.ended).toMatchObject({ status: 143, signal: null, stdout: "" });
    expect(readdirSync(out)).toEqual(["surogate-desktop-1.2.3-linux-x64.tar.gz"]);
    expect(readdirSync(tmp)).toEqual([]);
  });

  it("lets a signal by while its folder is made, and from the removal of what it unpacked to its end: stopped there, it would leave a folder it has not named yet, or what its removal had not reached, or a manifest half written", { timeout: 60_000 }, async () => {
    const stand = {
      // mktemp as publish.sh calls it: the folder is made, and its name not said yet.
      mktemp: (real: string) => [`made="$('${real}' "$@")" || exit`, ...held(dir, "mktemp"), 'printf \'%s\\n\' "$made"'],
      // rm as publish.sh calls it: what was unpacked is about to be removed.
      rm: (real: string) => ['case "$*" in *release-unpacked-*)', ...held(dir, "rm"), ";; esac", `exec '${real}' "$@"`],
    };
    for (const program of ["mktemp", "rm"] as const) {
      // One stand-in at a time: each waits for the same word to go on.
      rmSync(join(dir, "bin", "mktemp"), { force: true });
      recording(dir, program, stand[program]);
      // To the script alone, and to all it runs too, as Ctrl+C pressed again reaches rm.
      for (const [group, signal] of [[false, "SIGTERM"], [true, "SIGINT"]] as const) {
        again(program);
        const run = describing(group);
        await called(program);
        run.signal(signal);
        await letGo(run);
        expect(await run.ended, `${program} ${signal}`).toEqual({ status: 0, signal: null, stdout: `wrote ${out}/manifest.json\n`, stderr: "" });
        // Whole: the next step takes it for the manifest of this version and this tarball, byte for byte.
        expect(signs().status, `${program} ${signal}`).toBe(0);
        expect(verify(null, readFileSync(join(out, "manifest.json")), keys.publicKey, readFileSync(join(out, "manifest.json.sig"))), `${program} ${signal}`).toBe(true);
        expect(readdirSync(tmp), `${program} ${signal}`).toEqual([]);
      }
    }
  });

  it("writes no manifest, and says so, when what it unpacked cannot be removed; ends as a signal ends it even then; and says signed of no signing that does not end 0", { timeout: 60_000 }, async () => {
    // rm as publish.sh calls it, which cannot remove what was unpacked. What it leaves is the stand-in's doing, and is removed here.
    recording(dir, "rm", (real) => ['case "$*" in *release-unpacked-*) echo "rm: cannot remove what was unpacked" >&2; exit 1 ;; esac', `exec '${real}' "$@"`]);
    const left = () => {
      const names = readdirSync(tmp);
      expect(names).toEqual([expect.stringMatching(/^release-unpacked-/)]);
      const unpacked = join(tmp, names[0] ?? "");
      rmSync(unpacked, { recursive: true });
      return unpacked;
    };
    const kept = describes();
    expect(kept).toMatchObject({ status: 1, stdout: "" });
    expect(kept.stderr).toContain(`publish.sh: the unpacked tarball could not be removed from ${left()}: no manifest is written\n`);
    expect(readdirSync(out)).toEqual(["surogate-desktop-1.2.3-linux-x64.tar.gz"]);
    // Stopped by a signal, its status is the signal's, and not that of the removal that failed as it ended.
    recording(dir, "tar", unpacking);
    const run = describing();
    await called("tar");
    run.signal("SIGTERM");
    await letGo(run);
    expect(await run.ended).toEqual({ status: 143, signal: null, stdout: "", stderr: "rm: cannot remove what was unpacked\n" });
    expect(readdirSync(out)).toEqual(["surogate-desktop-1.2.3-linux-x64.tar.gz"]);
    left();
    for (const program of ["rm", "tar"]) rmSync(join(dir, "bin", program));
    // openssl as publish.sh calls it, which fails where it signs the manifest that is written.
    expect(describes().status).toBe(0);
    recording(dir, "openssl", (real) => ['case " $* " in *" -sign "*) exit 1 ;; esac', `exec '${real}' "$@"`]);
    expect(signs()).toMatchObject({ status: 1, stdout: "" });
    expect(existsSync(join(out, "manifest.json.sig"))).toBe(false);
    expect(readdirSync(tmp)).toEqual([]);
  });

  it("holds one substitution to a command for as long as a signal ends it: Ubuntu 24.04's bash runs the signal's handler between the two of one command, and ends with an error of its own", () => {
    const lines = readFileSync(join(RELEASE, "publish.sh"), "utf8").split("\n");
    // describe's commands from where a signal ends it to where it lets every signal by: its removal, and all that follows.
    const from = lines.findIndex((line) => line.includes("trap 'exit 143' TERM"));
    const to = lines.findIndex((line, at) => at > from && /^\s+cleanup \|\| fail /.test(line));
    expect(from).toBeGreaterThan(0);
    expect(to).toBeGreaterThan(from);
    const commands = lines.slice(from + 1, to).filter((line) => !line.trim().startsWith("#"));
    expect(commands.filter((line) => line.includes("$(")).length).toBeGreaterThan(0);
    for (const line of commands) expect((line.match(/[$<>]\(/g) ?? []).length, line).toBeLessThanOrEqual(1);
  });

  it("refuses a tarball that is not the one the build's job made, by the hash that job gave: an artifact is its run's, and any job of the run may put another under its name", () => {
    const first = sha256(readFileSync(tarball()));
    // Another tarball under the build's name, whose helper is the install script too.
    packed(dir, out, "1.2.3", (top) => writeFileSync(join(top, "surogate"), "#!/bin/sh\n# another job's\n"));
    const found = sha256(readFileSync(tarball()));
    expect(found).not.toBe(first);
    expect(describes({ DESKTOP_TARBALL_SHA256: first })).toMatchObject({
      status: 1, stdout: "", stderr: `publish.sh: ${tarball()} is not the tarball the build made: its sha256 is ${found}, and the build's ${first}\n`,
    });
    // What is no sha256 names no tarball: nothing of it is compared, or said back.
    for (const hash of [found.toUpperCase(), found.slice(1), `${found} `, `${found}\n${found}`, "$(touch ran)"]) {
      for (const step of [describes, signs]) expect(step({ DESKTOP_TARBALL_SHA256: hash }), hash).toMatchObject({ status: 1, stdout: "", stderr: "publish.sh: DESKTOP_TARBALL_SHA256 is not a sha256, as the build's job gives its tarball's\n" });
    }
    for (const step of [describes, signs]) expect(step({ DESKTOP_TARBALL_SHA256: "" })).toMatchObject({ status: 1, stdout: "", stderr: expect.stringMatching(/DESKTOP_TARBALL_SHA256: parameter null or not set\n$/) });
    expect(readdirSync(out)).toEqual(["surogate-desktop-1.2.3-linux-x64.tar.gz"]);
    expect(describes({ DESKTOP_TARBALL_SHA256: found }).status).toBe(0);
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
    try {
      // However each signing of the test ended, nothing is left where it unpacked.
      expect(readdirSync(tmp)).toEqual([]);
    } finally {
      // What a signing did leave has the modes its archive gave it.
      spawnSync("chmod", ["-R", "u+rwX", dir]);
      rmSync(dir, { recursive: true, force: true });
    }
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
    // As the publish job's two steps: the manifest written where no key is, then signed.
    const { DESKTOP_RELEASE_KEY: _held, ...own } = process.env;
    const built = { DESKTOP_TARBALL_SHA256: sha256(readFileSync(tarball)), DESKTOP_TARBALL_SIZE: String(statSync(tarball).size) };
    expect(spawnSync(join(dir, "release", "publish.sh"), ["describe", version, out], { env: { ...own, DESKTOP_TARBALL_SHA256: built.DESKTOP_TARBALL_SHA256 } }).status).toBe(0);
    expect(spawnSync(join(dir, "release", "publish.sh"), ["sign", version, out], { env: { ...own, DESKTOP_RELEASE_KEY: PRIVATE, ...built } }).status).toBe(0);
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
