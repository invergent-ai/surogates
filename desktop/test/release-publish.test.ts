// The desktop's release (release/publish.sh): its manifest written, then signed, then sent to a local S3
// (SeaweedFS in Docker) in R2's place. Behind SUROGATE_S3_TESTS=1: it needs Docker and the
// chrislusf/seaweedfs image.

import { spawn, spawnSync } from "node:child_process";
import { createHash, generateKeyPairSync, type KeyObject, randomBytes, sign, verify } from "node:crypto";
import { copyFileSync, existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, renameSync, rmSync, statSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from "vitest";

import { KNOWN } from "../src/browser/choose.js";

import { KEY_LISTS } from "./key-list-forms.js";
import { FORMS, line, SHA } from "./manifest-forms.js";

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
  'if [ "$fault" = forbidden ] && [ -z "$upload" ]; then printf 403; exit 0; fi',
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
  // The state schema that the first step read, as its job says it to the signing's: its manifest's, and 1 where it wrote none.
  const schemaRead = () => (existsSync(join(out, "manifest.json")) ? String((JSON.parse(readFileSync(join(out, "manifest.json"), "utf8")) as { stateSchema?: number }).stateSchema ?? 1) : "1");
  const signs = (env: Record<string, string> = {}) => publish("sign", "1.2.3", { DESKTOP_RELEASE_KEY: PRIVATE, ...built(), DESKTOP_STATE_SCHEMA: schemaRead(), ...env });
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
      // A package is read by the rule a manifest is read by: JSON as it is written, where jq reads more.
      ["a package with a byte order mark before it", (top) => writeFileSync(app(top), '\uFEFF{"version":"1.2.3","stateSchema":1}\n')],
      ["a package with a number of its own in 18 digits", (top) => writeFileSync(app(top), '{"version":"1.2.3","stateSchema":1,"more":123456789012345678}\n')],
      ["a package with a number of its own written 01", (top) => writeFileSync(app(top), '{"version":"1.2.3","stateSchema":1,"more":01}\n')],
      ["a package with its state schema named twice", (top) => writeFileSync(app(top), '{"version":"1.2.3","stateSchema":1,"stateSchema":2}\n')],
      ["a package of more than a megabyte", (top) => writeFileSync(app(top), `{"version":"1.2.3","stateSchema":1,"more":"${"x".repeat(1024 * 1024)}"}\n`)],
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

  it("writes no manifest of a tarball whose app is another version than the release: the app says its own version from its package, and would be offered its own release as an update", () => {
    const notIts = { status: 1, stdout: "", stderr: "publish.sh: the tarball's app is not version 1.2.3, by its resources/app/package.json\n" };
    for (const app of [{ version: "9.9.9", stateSchema: 1 }, { version: "1.2.30", stateSchema: 1 }, { version: 123, stateSchema: 1 }, { stateSchema: 1 }]) {
      packed(dir, out, "1.2.3", withApp(app));
      expect(describes(), JSON.stringify(app)).toMatchObject(notIts);
      expect(readdirSync(out), JSON.stringify(app)).toEqual(["surogate-desktop-1.2.3-linux-x64.tar.gz"]);
      expect(readdirSync(tmp), JSON.stringify(app)).toEqual([]);
    }
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
    // A package as npm writes one, a field a line and longer than any manifest, is the app's own.
    packed(dir, out, "1.2.3", withApp({ version: "1.2.3", stateSchema: 2 }));
    expect(describes().status).toBe(0);
    again();
    packed(dir, out, "1.2.3", (top) => writeFileSync(join(top, "resources", "app", "package.json"), `${JSON.stringify({ version: "1.2.3", description: "x".repeat(8192), stateSchema: 7 }, null, 2)}\n`));
    expect(describes().status).toBe(0);
    expect((JSON.parse(readFileSync(join(out, "manifest.json"), "utf8")) as { stateSchema: number }).stateSchema).toBe(7);
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
    // Every file openssl is given is a pipe, but the line it signs and the signature it writes, each in the signing's own folder.
    expect(argv.split("\n").filter((arg) => arg.startsWith("/") && !/^\/dev\/fd\/\d+$/.test(arg)))
      .toEqual(["", ".sig"].map((end) => expect.stringMatching(new RegExp(`^${tmp}/release-signing-\\w{10}/manifest\\.json${end.replace(".", "\\.")}$`))));
    expect(readdirSync(tmp)).toEqual([]);
    // Beside the tarball, the manifest and its signature, and nothing else.
    expect(readdirSync(out).sort()).toEqual(["manifest.json", "manifest.json.sig", "surogate-desktop-1.2.3-linux-x64.tar.gz"]);
    expect(spawnSync("grep", ["-rlF", body, dir], { encoding: "utf8" }).stdout).toBe("");
  });

  it("reads the build's tarball only where no release key is: with one in its environment, set or empty, it starts nothing and writes nothing", () => {
    // Each program the step starts, as publish.sh calls it, written down.
    const started = () => readdirSync(dir).filter((name) => name.endsWith("-argv")).sort();
    for (const program of ["dirname", "sha256sum", "cut", "tail", "mktemp", "tar", "realpath", "cmp", "sed", "grep", "head", "jq", "chmod", "rm", "stat"]) recording(dir, program);
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
    expect(started()).toEqual(["chmod-argv", "cmp-argv", "cut-argv", "dirname-argv", "grep-argv", "head-argv", "jq-argv", "mktemp-argv", "realpath-argv", "rm-argv", "sed-argv", "sha256sum-argv", "stat-argv", "tail-argv", "tar-argv"]);
    expect(readFileSync(join(dir, "tar-argv"), "utf8")).toContain(`${tarball()}\n`);
  });

  it("signs with no tarball there: it opens none, and starts no program that is handed one", () => {
    expect(describes().status).toBe(0);
    // What a signing needs of the tarball is in the build's two words, and of the app in the first step's one: the tarball itself is away, and so is the first step's manifest.
    const said = { ...built(), DESKTOP_STATE_SCHEMA: schemaRead() };
    rmSync(join(out, "manifest.json"));
    renameSync(tarball(), join(dir, "elsewhere"));
    // Each program the step starts, as publish.sh calls it, written down: bash among them, which
    // the script itself is run by. Each first counts the variables of its environment that hold
    // the key's own line, under whatever name.
    const body = PRIVATE.split("\n")[1] ?? "";
    const counted = (real: string) => [`/usr/bin/env | /usr/bin/grep -cF '${body}' >> '${join(dir, "keyed")}'`, `exec '${real}' "$@"`];
    for (const program of ["bash", "dirname", "tail", "sed", "head", "jq", "cmp", "openssl", "tar", "sha256sum", "stat", "realpath", "mktemp", "cut", "rm", "chmod", "mv", "grep"]) recording(dir, program, counted);
    expect(publish("sign", "1.2.3", { DESKTOP_RELEASE_KEY: PRIVATE, ...said })).toMatchObject({ status: 0, stdout: `signed ${out}/manifest.json\n`, stderr: "" });
    expect(verify(null, readFileSync(join(out, "manifest.json")), keys.publicKey, readFileSync(join(out, "manifest.json.sig")))).toBe(true);
    // No tar, and nothing that would unpack, hash or measure one.
    const started = readdirSync(dir).filter((name) => name.endsWith("-argv")).sort();
    expect(started).toEqual(["bash-argv", "dirname-argv", "grep-argv", "head-argv", "jq-argv", "mktemp-argv", "mv-argv", "openssl-argv", "rm-argv", "sed-argv", "tail-argv"]);
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

  it("signs a manifest of four words, which it writes itself: the tag's version, the build's two for its tarball, and the state schema that the first step read; and no file that another wrote", () => {
    expect(describes().status).toBe(0);
    const described = readFileSync(join(out, "manifest.json"));
    const said = { DESKTOP_RELEASE_KEY: PRIVATE, ...built(), DESKTOP_STATE_SCHEMA: "1" };
    const signedAs = () => {
      const manifest = readFileSync(join(out, "manifest.json"));
      expect(verify(null, manifest, keys.publicKey, readFileSync(join(out, "manifest.json.sig")))).toBe(true);
      return manifest.toString();
    };
    // Whatever stands where the manifest goes is not read, and is replaced by the signing's own
    // line, which is the first step's: another manifest that an install would take, what is none,
    // nothing, and a link to a file elsewhere, which is left as it was.
    const fields = JSON.parse(described.toString()) as Record<string, unknown>;
    for (const [what, there] of [
      ["the first step's own", described.toString()],
      ["another release's", `${JSON.stringify({ ...fields, version: "9.9.9", url: "releases/9.9.9/surogate-desktop-9.9.9-linux-x64.tar.gz" })}\n`],
      ["another tarball's", `${JSON.stringify({ ...fields, sha256: "0".repeat(64), size: 1 })}\n`],
      ["another state schema's", `${JSON.stringify({ ...fields, stateSchema: 7 })}\n`],
      ["no manifest", "<html>\n"],
      ["none", null],
    ] as const) {
      again();
      if (there !== null) writeFileSync(join(out, "manifest.json"), there);
      expect(publish("sign", "1.2.3", said), what).toMatchObject({ status: 0, stdout: `signed ${out}/manifest.json\n`, stderr: "" });
      expect(signedAs(), what).toBe(described.toString());
    }
    again();
    writeFileSync(join(dir, "elsewhere.json"), "theirs\n");
    symlinkSync(join(dir, "elsewhere.json"), join(out, "manifest.json"));
    writeFileSync(join(dir, "elsewhere.sig"), "theirs\n");
    symlinkSync(join(dir, "elsewhere.sig"), join(out, "manifest.json.sig"));
    expect(publish("sign", "1.2.3", said).status).toBe(0);
    expect(signedAs()).toBe(described.toString());
    expect([readFileSync(join(dir, "elsewhere.json"), "utf8"), readFileSync(join(dir, "elsewhere.sig"), "utf8")]).toEqual(["theirs\n", "theirs\n"]);
    // The state schema is the one word of the four that the build does not say: signed as it is given.
    again();
    expect(publish("sign", "1.2.3", { ...said, DESKTOP_STATE_SCHEMA: "7" }).status).toBe(0);
    expect(signedAs()).toBe(`${JSON.stringify({ ...fields, stateSchema: 7 })}\n`);
    // Each word in its own form, or nothing is signed and nothing written.
    again();
    for (const [word, values, saidOf] of [
      ["DESKTOP_TARBALL_SIZE", ["0", "12a", "1e3", " 12", "-12", "1000000000000000"], "DESKTOP_TARBALL_SIZE is not a size in bytes, as the build's job gives its tarball's"],
      ["DESKTOP_STATE_SCHEMA", ["0", "01", "+1", "1.0", "1e3", " 1", "-1", "1000000000000000", '1,"more":true'], "DESKTOP_STATE_SCHEMA is not a state schema, a whole number from 1 and below 10^15, as describe reads one"],
      ["DESKTOP_TARBALL_SHA256", ["0", "A".repeat(64), `${"a".repeat(64)}\n`], "DESKTOP_TARBALL_SHA256 is not a sha256, as the build's job gives its tarball's"],
    ] as const) {
      for (const value of values) expect(publish("sign", "1.2.3", { ...said, [word]: value }), `${word}=${value}`).toMatchObject({ status: 1, stdout: "", stderr: `publish.sh: ${saidOf}\n` });
      expect(publish("sign", "1.2.3", { ...said, [word]: "" }), word).toMatchObject({ status: 1, stdout: "", stderr: expect.stringMatching(new RegExp(`${word}: parameter null or not set\\n$`)) });
    }
    expect(readdirSync(out)).toEqual(["surogate-desktop-1.2.3-linux-x64.tar.gz"]);
    expect(readdirSync(tmp)).toEqual([]);
  });

  it("writes, signs and takes a release's manifest by one rule, on every form that its readers are asked about: the first step writes none that an apply refuses, the second signs none, and what the second refuses for its form is what an apply's own reader refuses", { timeout: 180_000 }, async () => {
    const install = join(dir, "release", "install.sh");
    const signature = join(out, "manifest.json.sig");
    // What an apply's own reader says of a manifest: the release it takes it for, as
    // "<version> <sha256> <size>", or null where it refuses it.
    const applied = (manifest: Buffer) => {
      writeFileSync(join(dir, "form.json"), manifest);
      const read = spawnSync("bash", ["-c", `. <(sed '$d' "$1") && settings && release_of "$2"`, "_", install, join(dir, "form.json")], { encoding: "utf8" });
      return read.status === 0 ? read.stdout.trim() : null;
    };
    // What the signing step makes of *manifest*, a form that an apply takes for release *named*:
    // it reads no manifest, and writes its own of the four words that this one says, the version,
    // the hash, the size and the state schema as it rounds. "signed", where this form is that
    // line byte for byte, and "another's" where it is not: no other spelling of a release is ever signed.
    const signed = (manifest: Buffer, named: string) => {
      const [version = "", sha = "", size = ""] = named.split(" ");
      rmSync(signature, { force: true });
      rmSync(join(out, "manifest.json"), { force: true });
      const schema = String(Math.floor((JSON.parse(manifest.toString()) as { stateSchema: number }).stateSchema));
      const step = publish("sign", version, { DESKTOP_RELEASE_KEY: PRIVATE, DESKTOP_TARBALL_SHA256: sha, DESKTOP_TARBALL_SIZE: size, DESKTOP_STATE_SCHEMA: schema });
      if (step.status !== 0) return step.stderr;
      const line = readFileSync(join(out, "manifest.json"));
      if (!verify(null, line, keys.publicKey, readFileSync(signature))) return "a signature of something else";
      if (applied(line) !== named) return "a line that an apply does not take for that release";
      return line.equals(manifest) ? "signed" : "another's";
    };
    // What the first step writes of release *named*, whose app keeps state of schema *schema*. Its
    // tarball is a small one of that version. No tarball has a hash that one chooses: the hash and
    // the size are said by stand-ins for the two tools that measure it, and are the build's words.
    const packedAt = new Map<string, number>();
    const written = (named: string, schema: number) => {
      const [version = "", sha = "", size = ""] = named.split(" ");
      if (packedAt.get(version) !== schema) packed(dir, out, version, withApp({ version, stateSchema: schema }));
      packedAt.set(version, schema);
      recording(dir, "sha256sum", () => [`echo '${sha}  -'`]);
      recording(dir, "stat", () => [`echo '${size}'`]);
      rmSync(join(out, "manifest.json"), { force: true });
      const step = publish("describe", version, { DESKTOP_TARBALL_SHA256: sha });
      for (const tool of ["sha256sum", "stat"]) rmSync(join(dir, "bin", tool));
      return step.status === 0 ? readFileSync(join(out, "manifest.json")) : null;
    };
    // A turn of the event loop between forms: each is several programs run, and vitest's worker
    // answers its runner on that loop within a minute.
    const said: Array<Record<string, unknown>> = [];
    for (const [name, form] of FORMS) {
      await new Promise((resolve) => setImmediate(resolve));
      const manifest = Buffer.from(form);
      const taken = applied(manifest);
      // Refused by an apply: no signing has it to read, and none writes it.
      if (taken === null) {
        said.push({ name, apply: "refuses" });
        continue;
      }
      // Taken by an apply, for a release: what the first step writes of that release is taken for
      // the same one, and signed; and the form itself is signed where it is that writing, byte
      // for byte, and nowhere else.
      const schema = Math.floor((JSON.parse(manifest.toString()) as { stateSchema: number }).stateSchema);
      const own = written(taken, schema);
      const sign = signed(manifest, taken);
      // The line the signing wrote of the release that this form says: the two steps write one line.
      const line = readFileSync(join(out, "manifest.json"));
      said.push({
        name, apply: "takes", sign,
        writes: own === null ? "nothing" : own.equals(manifest) ? "this" : "another",
        itsWriting: own === null ? null : { apply: applied(own) === taken ? "takes" : "refuses", sign: own.equals(line) ? sign === "signed" || sign === "another's" ? "signed" : sign : signed(own, taken) },
        one: own !== null && own.equals(line),
      });
    }
    expect(said).toEqual(FORMS.map(([name, , helper], at) => (helper
      ? { name, apply: "takes", sign: said[at]?.writes === "this" ? "signed" : "another's", writes: said[at]?.writes === "this" ? "this" : "another", itsWriting: { apply: "takes", sign: "signed" }, one: true }
      : { name, apply: "refuses" })));
    // The forms that are the first step's own writing, and so are signed: the release job's line, of each version and size it may have.
    expect(said.filter(({ sign }) => sign === "signed").map(({ name }) => name))
      .toEqual(["as the release job writes it", "of version 0.0.1", "of version 10.20.30", "with a size one below 10^15"]);

    // What the first step is given, it takes as an apply takes a manifest that says the same: a
    // version, which is its argument; and the state schema, as the app's own package writes it.
    // (A tarball's size is what the tarball is, and is given by nothing.) Asked of each form that
    // is the release job's line but for its version, or but for its state schema as it is written.
    const job = line();
    const upTo = job.slice(0, job.lastIndexOf('"stateSchema":'));
    const given = FORMS.flatMap(([name, form, helper]): Array<{ name: string; takes: boolean; version: string; schema: string | null }> => {
      if (typeof form !== "string") return [];
      const version = /^\{"version":"([^"\\]*)",/.exec(form)?.[1];
      if (version !== undefined && version !== "1.2.4" && form === line({}, version)) return [{ name, takes: helper, version, schema: "1" }];
      if (form === `${upTo.replace(/,$/, "")}}\n`) return [{ name, takes: helper, version: "1.2.4", schema: null }];
      const schema = form.startsWith(`${upTo}"stateSchema":`) && form.endsWith("}\n") ? form.slice(`${upTo}"stateSchema":`.length, -2) : null;
      return schema !== null && /^[^{}[\],:]+$/.test(schema) ? [{ name, takes: helper, version: "1.2.4", schema }] : [];
    });
    expect(given.filter(({ version }) => version !== "1.2.4").length).toBeGreaterThan(8);
    expect(given.filter(({ version }) => version === "1.2.4").length).toBeGreaterThan(10);
    const wrote: Array<{ name: string; writes: boolean }> = [];
    for (const { name, version, schema } of given) {
      await new Promise((resolve) => setImmediate(resolve));
      rmSync(join(out, "manifest.json"), { force: true });
      // A tarball of that version, where a file can have the version in its name, whose app's
      // package writes the schema so.
      const named = !version.includes("/");
      if (named) packed(dir, out, version, (top) => writeFileSync(join(top, "resources", "app", "package.json"), `{"version":"${version}"${schema === null ? "" : `,"stateSchema":${schema}`}}\n`));
      const step = publish("describe", version, { DESKTOP_TARBALL_SHA256: named ? sha256(readFileSync(join(out, `${NAME_OF(version)}.tar.gz`))) : SHA });
      wrote.push({ name, writes: step.status === 0 && existsSync(join(out, "manifest.json")) });
    }
    expect(wrote).toEqual(given.map(({ name, takes }) => ({ name, writes: takes })));
  });

  it("gives the release key to no program that a signing starts but openssl, and to openssl by a pipe that the script names: each program writes down its environment, its arguments and its input as it got them", () => {
    expect(describes().status).toBe(0);
    // The key's own line of its PEM: the rest is every such key's.
    const body = PRIVATE.split("\n")[1] ?? "";
    expect(body).toMatch(/^[A-Za-z0-9+/]{64}$/);
    // Every program the signing finds by its name is a stand-in, in a folder that is all of its
    // PATH: a name with no stand-in is not found, and ends the signing. Each writes down, under
    // its own process number, its environment and its arguments as it got them, what each pipe
    // it is handed by name holds, and what comes on its input; and then runs the program itself,
    // by its whole path, with a copy of each pipe.
    const only = join(dir, "only");
    const seen = join(dir, "seen");
    mkdirSync(only);
    mkdirSync(seen);
    const programs = ["bash", "dirname", "grep", "head", "jq", "mktemp", "mv", "openssl", "rm", "sed", "tail"];
    for (const program of programs) {
      const real = spawnSync("sh", ["-c", `command -v ${program}`], { encoding: "utf8" }).stdout.trim();
      expect(real, program).toMatch(/^\/.+/);
      writeFileSync(join(only, program), [
        "#!/bin/sh",
        `seen='${seen}'/$$-${program}`,
        '/usr/bin/env >"$seen.env"',
        // Each descriptor it has open, by its number, its own script's among them: listed by the shell itself, which opens nothing for it but the list.
        'for open in /proc/$$/fd/*; do echo "${open##*/}"; done >"$seen.open"',
        ': >"$seen.argv"',
        "count=$#; at=0",
        "for arg do",
        "  at=$((at + 1))",
        '  /usr/bin/printf \'%s\\n\' "$arg" >>"$seen.argv"',
        '  case "$arg" in /dev/fd/*)',
        '    if [ -p "$arg" ]; then /usr/bin/printf \'pipe\\n\' >"$seen.$at.kind"; else /usr/bin/printf \'file\\n\' >"$seen.$at.kind"; fi',
        '    /usr/bin/cat "$arg" >"$seen.$at.handed"; arg="$seen.$at.handed" ;;',
        // And what each file that it is handed by name holds, as it is then.
        '  *) if [ -f "$arg" ]; then /usr/bin/cat "$arg" >"$seen.$at.named"; fi ;;',
        "  esac",
        '  set -- "$@" "$arg"',
        "done",
        'shift "$count"',
        `/usr/bin/tee "$seen.input" | '${real}' "$@"`,
        "",
      ].join("\n"), { mode: 0o755 });
    }
    const { PATH: _path, ...env } = steps({ DESKTOP_RELEASE_KEY: PRIVATE, ...built(), DESKTOP_STATE_SCHEMA: schemaRead() });
    const signing = spawnSync(join(dir, "release", "publish.sh"), ["sign", "1.2.3", out], { encoding: "utf8", env: { ...env, PATH: only } });
    expect(signing).toMatchObject({ status: 0, stdout: `signed ${out}/manifest.json\n`, stderr: "" });
    expect(verify(null, readFileSync(join(out, "manifest.json")), keys.publicKey, readFileSync(join(out, "manifest.json.sig")))).toBe(true);
    // What each wrote down.
    const files = readdirSync(seen);
    const started = files.filter((file) => file.endsWith(".argv")).map((file) => file.slice(0, -".argv".length)).map((run) => ({
      program: run.replace(/^\d+-/, ""),
      argv: readFileSync(join(seen, `${run}.argv`), "utf8").split("\n").slice(0, -1),
      env: readFileSync(join(seen, `${run}.env`), "utf8"),
      input: readFileSync(join(seen, `${run}.input`), "utf8"),
      named: files.filter((file) => file.startsWith(`${run}.`) && file.endsWith(".named")).map((file) => readFileSync(join(seen, file), "utf8")),
      open: readFileSync(join(seen, `${run}.open`), "utf8").split("\n").slice(0, -1).map(Number),
      handed: files.filter((file) => file.startsWith(`${run}.`) && file.endsWith(".handed")).map((file) => {
        const at = Number(file.slice(run.length + 1, -".handed".length));
        return { at, kind: readFileSync(join(seen, `${run}.${at}.kind`), "utf8").trim(), holds: readFileSync(join(seen, file), "utf8") };
      }),
    }));
    expect([...new Set(started.map(({ program }) => program))].sort()).toEqual(programs);
    expect(started.length).toBeGreaterThan(15);
    // The key is in the environment of the script's own start alone, where the job puts it, under
    // its one name: of no program the script starts, whatever a variable there is called.
    const keyed = started.filter(({ env: its }) => its.includes(body));
    expect(keyed.map(({ program, argv }) => [program, argv])).toEqual([["bash", [join(dir, "release", "publish.sh"), "sign", "1.2.3", out]]]);
    expect(keyed[0]?.env.split(body).length).toBe(2);
    expect(keyed[0]?.env).toContain(`\nDESKTOP_RELEASE_KEY=-----BEGIN PRIVATE KEY-----\n${body}\n`);
    // It is in no program's arguments, which every process of the runner's could read, and comes on none's input.
    expect(started.filter(({ argv }) => argv.join("\n").includes(body)).map(({ program, argv }) => [program, argv])).toEqual([]);
    expect(started.filter(({ input }) => input.includes(body)).map(({ program, argv }) => [program, argv])).toEqual([]);
    // Nor is it in any file that a program is handed by name.
    expect(started.filter(({ named }) => named.some((held) => held.includes(body))).map(({ program, argv }) => [program, argv])).toEqual([]);
    // It is handed to openssl alone, twice, each time in a pipe that the script names: to say the
    // key's public half, and to sign.
    const given = started.flatMap(({ program, argv, handed }) => handed.filter(({ holds }) => holds.includes(body)).map(({ at, kind }) => [program, argv.slice(0, 2).join(" "), argv[at - 2], kind]));
    expect(given.sort()).toEqual([["openssl", "pkey -pubout", "-in", "pipe"], ["openssl", "pkeyutl -sign", "-inkey", "pipe"]]);
    // No program has a descriptor open that the script was not itself started with, but each pipe
    // that its arguments name: one that the script opened on the key before it started them
    // would be every program's to read.
    const startedWith = keyed[0]?.open ?? [];
    expect(startedWith).toEqual(expect.arrayContaining([0, 1, 2]));
    expect(started.flatMap(({ program, argv, open }) => open.filter((number) => !startedWith.includes(number) && !argv.includes(`/dev/fd/${number}`))
      .map((number) => [program, argv.slice(0, 2).join(" "), number]))).toEqual([]);
    // And no program is started but by its name, which is how each of them is a stand-in here:
    // the script names none by a path, and never says where names are looked for.
    const lines = readFileSync(join(RELEASE, "publish.sh"), "utf8").split("\n").slice(1).filter((line) => !line.trim().startsWith("#"));
    expect(lines.filter((line) => /(^|[\s;|&(`"'=])\/(usr|bin|sbin|opt|snap|nix|home|root|var|tmp)\//.test(line))).toEqual([]);
    expect(lines.filter((line) => /\bPATH\b/.test(line))).toEqual([]);
    // Nor by bash's own way round a name: command -p looks in the system's folders whatever PATH
    // says, and exec, enable and hash can each put another program under a name.
    expect(lines.filter((line) => /(^|[\s;|&(`])(command|exec|enable|hash|builtin|alias)\s/.test(line))).toEqual([]);
  });

  it("signs the line it wrote, in a folder of its own: what another puts under the manifest's name while it signs is not what is signed, and is not left there", () => {
    expect(describes().status).toBe(0);
    const described = readFileSync(join(out, "manifest.json"));
    const other = Buffer.from(described.toString().replace('"stateSchema":1}', '"stateSchema":7}'));
    writeFileSync(join(dir, "swapped.json"), other);
    // Put there by a stand-in for openssl, as it is started to sign: after every check the signing makes.
    recording(dir, "openssl", (real) => [`case " $* " in *" -sign "*) /usr/bin/cp '${join(dir, "swapped.json")}' '${join(out, "manifest.json")}' ;; esac`, `exec '${real}' "$@"`]);
    expect(signs()).toMatchObject({ status: 0, stdout: `signed ${out}/manifest.json\n`, stderr: "" });
    const signature = readFileSync(join(out, "manifest.json.sig"));
    expect(readFileSync(join(out, "manifest.json")).equals(described)).toBe(true);
    expect(verify(null, described, keys.publicKey, signature)).toBe(true);
    expect(verify(null, other, keys.publicKey, signature)).toBe(false);
    expect(readdirSync(tmp)).toEqual([]);
    recording(dir, "openssl");
  });

  it("sends no tarball but the one its signed manifest names, by its hash: the job that sends has a download of its own of the build's tarball, and nothing else in that job reads one", () => {
    expect(release().status).toBe(0);
    const bucket = { S3_ENDPOINT: "http://127.0.0.1:9", S3_BUCKET: "releases", ...CREDENTIALS };
    // No request is made of any bucket: curl, were it started, would write its arguments down.
    recording(dir, "curl", () => ["exit 7"]);
    const notIts = { status: 1, stdout: "", stderr: `publish.sh: ${tarball()} is not the tarball that ${out}/manifest.json names, by its sha256: nothing is sent\n` };
    // Another tarball under the release's name, as another job of the run may put one under the artifact's.
    const signed = readFileSync(join(out, "manifest.json"));
    packed(dir, out, "1.2.3", withApp({ version: "1.2.3", stateSchema: 9 }));
    expect((JSON.parse(signed.toString()) as { sha256: string }).sha256).not.toBe(sha256(readFileSync(tarball())));
    expect(publish("send", "1.2.3", bucket)).toMatchObject(notIts);
    // Nor with no tarball.
    rmSync(tarball());
    expect(publish("send", "1.2.3", bucket)).toMatchObject(notIts);
    // Nor a manifest and a signature that are no pair by the install script's keys, whatever the
    // tarball: another release's signature beside the manifest, a signature by a key the script
    // does not list, another version's manifest with its own signature, no manifest, and neither.
    const notAPair = { status: 1, stdout: "", stderr: `publish.sh: ${out}/manifest.json and ${out}/manifest.json.sig are not the manifest of 1.2.3 and its signature by a key that install.sh lists: nothing is sent\n` };
    const pair = [signed, readFileSync(join(out, "manifest.json.sig"))] as const;
    const others = generateKeyPairSync("ed25519");
    const elsewhere = Buffer.from(signed.toString().replaceAll("1.2.3", "1.2.4"));
    for (const [what, manifest, signature] of [
      ["another manifest's signature", signed, sign(null, elsewhere, keys.privateKey)],
      ["a signature by another key", signed, sign(null, signed, others.privateKey)],
      ["another version's pair", elsewhere, sign(null, elsewhere, keys.privateKey)],
      ["what is no manifest", Buffer.from("{}\n"), sign(null, Buffer.from("{}\n"), keys.privateKey)],
      ["no signature", signed, null],
      ["no manifest", null, pair[1]],
    ] as const) {
      for (const [file, bytes] of [["manifest.json", manifest], ["manifest.json.sig", signature]] as const) {
        rmSync(join(out, file), { force: true });
        if (bytes !== null) writeFileSync(join(out, file), bytes);
      }
      expect(publish("send", "1.2.3", bucket), what).toMatchObject(notAPair);
    }
    writeFileSync(join(out, "manifest.json"), pair[0]);
    writeFileSync(join(out, "manifest.json.sig"), pair[1]);
    expect(existsSync(join(dir, "curl-argv"))).toBe(false);
    expect(readdirSync(out).sort()).toEqual(["manifest.json", "manifest.json.sig"]);
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
    // Nor does the first step read the app's package with it, whose refusal would be taken for a
    // package that names no schema: said before anything is unpacked. Its tarball's helper is
    // that script, as the step asks.
    packed(dir, out, "1.2.3");
    recording(dir, "tar");
    expect(describes()).toMatchObject({
      status: 1, stdout: "", stderr: 'publish.sh: install.sh does not end with the line that runs it (main "$@"): no package is read with it\n',
    });
    expect(existsSync(join(dir, "ran"))).toBe(false);
    expect(existsSync(join(dir, "tar-argv"))).toBe(false);
    expect(readdirSync(out)).toEqual(["surogate-desktop-1.2.3-linux-x64.tar.gz"]);
    expect(readdirSync(tmp)).toEqual([]);
  });

  it("signs no line that the install script beside it does not take for the release: one longer than an install reads, or one of another channel than that script's", () => {
    // The script's own checks of its four words let both by: each is a version, a hash, a size and
    // a schema. What refuses them is the install script's reader, asked of the line before it is
    // signed. A signed manifest that no install takes is a release that is never sent again.
    const words = { DESKTOP_RELEASE_KEY: PRIVATE, DESKTOP_TARBALL_SHA256: "a".repeat(64), DESKTOP_TARBALL_SIZE: "1", DESKTOP_STATE_SCHEMA: "1" };
    const before = readdirSync(out);
    // A version of some thousand digits, which the line has three times, makes a line over the
    // 4096 bytes that an install reads of one.
    const long = `1.2.${"7".repeat(1400)}`;
    expect(publish("sign", long, words)).toMatchObject({ status: 1, stdout: "", stderr: `publish.sh: the manifest of ${long} is none that install.sh takes for it: nothing is signed\n` });
    expect(readdirSync(out)).toEqual(before);
    // The longest that an install does read is signed.
    const fits = `1.2.${"7".repeat(1200)}`;
    expect(publish("sign", fits, words)).toMatchObject({ status: 0, stdout: `signed ${out}/manifest.json\n` });
    expect(statSync(join(out, "manifest.json")).size).toBeGreaterThan(3800);
    rmSync(join(out, "manifest.json"));
    rmSync(join(out, "manifest.json.sig"));
    // An install script of another channel takes no release of this one's.
    const script = join(dir, "release", "install.sh");
    const channel = readFileSync(script, "utf8").replace(/^  CHANNEL=stable$/m, "  CHANNEL=beta");
    expect(channel).toContain("\n  CHANNEL=beta\n");
    writeFileSync(script, channel, { mode: 0o755 });
    expect(publish("sign", "1.2.3", words)).toMatchObject({ status: 1, stdout: "", stderr: "publish.sh: the manifest of 1.2.3 is none that install.sh takes for it: nothing is signed\n" });
    expect(readdirSync(out)).toEqual(before);
    expect(readdirSync(tmp)).toEqual([]);
  });

  it("stops with its usage at a version that is no x.y.z or a verb it does not have, and says so where the tarball is not there to describe", () => {
    // A part with a zero before it is no version either: dpkg reads 1.2.03 as 1.2.3, a second spelling of one release.
    for (const [verb, version] of [["sign", "1.2"], ["sign", "1.2.3-rc1"], ["sign", "v1.2.3"], ["sign", "1.2.3/../1.2.3"], ["sign", "1.2.03"], ["describe", "01.2.3"], ["send", "1.02.3"], ["publish", "1.2.3"]] as const) {
      expect(publish(verb, version, verb === "describe" ? built() : { DESKTOP_RELEASE_KEY: PRIVATE, ...built() }), `${verb} ${version}`)
        .toMatchObject({ status: 2, stdout: "", stderr: "usage: publish.sh describe|sign|send <x.y.z> <out>\n" });
    }
    expect(publish("describe", "1.2.4", built())).toMatchObject({
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
    expect(spawnSync(join(dir, "release", "publish.sh"), ["sign", version, out], { env: { ...own, DESKTOP_RELEASE_KEY: PRIVATE, ...built, DESKTOP_STATE_SCHEMA: "1" } }).status).toBe(0);
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
    // latest.json has no signature of its own: its readers ask for its release's, which is sent once.
    expect(object("latest.json.sig")).toBeNull();
    expect(object("releases/1.0.0/manifest.json")?.equals(manifest)).toBe(true);
    expect(object("releases/1.0.0/manifest.json.sig")?.equals(signature)).toBe(true);
    expect(object("releases/1.0.0/surogate-desktop-1.0.0-linux-x64.tar.gz")?.equals(readFileSync(join(out, "surogate-desktop-1.0.0-linux-x64.tar.gz")))).toBe(true);
    expect(object("install.sh")?.equals(readFileSync(join(dir, "release", "install.sh")))).toBe(true);
    // What changes from release to release, never from a cache.
    expect(["install.sh", "latest.json"].map(caching)).toEqual(["no-cache", "no-cache"]);
    // And what is a release's own, for good, is a cache's to keep.
    expect(["releases/1.0.0/manifest.json", "releases/1.0.0/manifest.json.sig"].map(caching)).toEqual([null, null]);
    expect(send("1.0.0", out)).toMatchObject({ status: 1, stdout: "", stderr: "publish.sh: desktop/releases/1.0.0 is published already, and is not sent again\n" });
    // Nor is another build under its version: its manifest is not the one that is there.
    expect(send("1.0.0", released("1.0.0"))).toMatchObject({ status: 1, stdout: "", stderr: "publish.sh: desktop/releases/1.0.0 is published already, with another manifest, and is not sent again\n" });
    expect(object("releases/1.0.0/surogate-desktop-1.0.0-linux-x64.tar.gz")?.equals(readFileSync(join(out, "surogate-desktop-1.0.0-linux-x64.tar.gz")))).toBe(true);
    expect(object("latest.json")?.equals(manifest)).toBe(true);
  });

  it("sends nothing beside an install script whose list of release keys bash reads otherwise than an install does: the script is sent too, and is the next install's", () => {
    const out = released("1.0.0");
    // The list in its form, which signed this release's manifest, and a key more that bash alone
    // sets: the pair is one the script's own reader takes, and the script is not one to send.
    const spelled = KEY_LISTS.find(([name]) => name === "with a key added to it from the middle of another line");
    expect(spelled).toBeDefined();
    const script = join(dir, "release", "install.sh");
    writeFileSync(script, spelled![1](trusting()), { mode: 0o755 });
    try {
      expect(send("1.0.0", out)).toMatchObject({
        status: 1, stdout: "",
        stderr: "publish.sh: install.sh's list of release keys is not in the one form that an install reads (see the list in install.sh): a computer that installed this release would take no later one\n",
      });
      for (const path of ["latest.json", "latest.json.sig", "install.sh", "releases/1.0.0/manifest.json", "releases/1.0.0/manifest.json.sig", "releases/1.0.0/surogate-desktop-1.0.0-linux-x64.tar.gz"]) expect(object(path), path).toBeNull();
    } finally {
      writeFileSync(script, trusting(), { mode: 0o755 });
    }
    expect(send("1.0.0", out)).toMatchObject({ status: 0, stdout: "published desktop/releases/1.0.0 as desktop/latest.json\n" });
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

  it("sends nothing that a reader reads first before what it then asks for: cut short at each object in turn, a send leaves no moment when latest.json names a release whose signature or tarball is not there, and the same send run again ends it", () => {
    const before = released("1.0.0");
    expect(send("1.0.0", before).status).toBe(0);
    const script = readFileSync(join(dir, "release", "install.sh"), "utf8");
    try {
      // The next release, with another install script, as a rotation's last has.
      writeFileSync(join(dir, "release", "install.sh"), script.replace("\nsettings() {\n", "\n# The next release's.\nsettings() {\n"));
      const out = released("1.1.0");
      const [older, newer] = [before, out].map((folder) => readFileSync(join(folder, "manifest.json")));
      const scripts = [Buffer.from(script), readFileSync(join(dir, "release", "install.sh"))] as const;
      const own = ["surogate-desktop-1.1.0-linux-x64.tar.gz", "manifest.json.sig", "manifest.json"].map((name) => `releases/1.1.0/${name}`);
      // Each object of a send, in the order it is sent.
      const order = [...own, "latest.json", "install.sh"];
      const seen = order.map((cut) => {
        expect(send("1.1.0", out, { FAULT: `refused ${cut}` }), cut).toMatchObject({ status: 1, stdout: "", stderr: `publish.sh: sending desktop/${cut} got 503\n` });
        const latest = object("latest.json");
        return {
          cut, latest: latest?.equals(newer!) ? "this release's" : latest?.equals(older!) ? "the one before's" : "neither",
          script: object("install.sh")?.equals(scripts[1]) ? "this release's" : object("install.sh")?.equals(scripts[0]) ? "the one before's" : "neither",
          // What a reader of latest.json then asks for, as it finds them: the signature and the
          // tarball of the release it names, and whether that signature is of that manifest.
          paired: (() => {
            const version = (JSON.parse(latest!.toString()) as { version: string }).version;
            const signature = object(`releases/${version}/manifest.json.sig`);
            return signature !== null && verify(null, latest!, keys.publicKey, signature) && object(`releases/${version}/surogate-desktop-${version}-linux-x64.tar.gz`) !== null;
          })(),
          // No signature beside latest.json at any step.
          beside: object("latest.json.sig"),
        };
      });
      const [was, is] = ["the one before's", "this release's"];
      expect(seen).toEqual([
        { cut: order[0], latest: was, script: was, paired: true, beside: null },
        { cut: order[1], latest: was, script: was, paired: true, beside: null },
        { cut: order[2], latest: was, script: was, paired: true, beside: null },
        { cut: order[3], latest: was, script: was, paired: true, beside: null },
        // The release is the newest, and the script that checks it for a first install is still the one before's, which lists its key.
        { cut: order[4], latest: is, script: was, paired: true, beside: null },
      ]);
      // The same send, run again, ends it: the script alone was owed, and nothing of the release is sent a second time.
      rmSync(join(dir, "curl-argv"), { force: true });
      expect(send("1.1.0", out)).toMatchObject({ status: 0, stdout: "published desktop/releases/1.1.0 as desktop/latest.json\n" });
      const sent = readFileSync(join(dir, "curl-argv"), "utf8").split(/^-q\n/m).filter((args) => args.includes("\n-T\n")).map((args) => args.trimEnd().split("\n").at(-1)?.replace(/^.*\/desktop\//, ""));
      expect(sent).toEqual(["install.sh"]);
      expect(object("install.sh")?.equals(scripts[1])).toBe(true);
      expect(send("1.1.0", out)).toMatchObject({ status: 1, stdout: "", stderr: "publish.sh: desktop/releases/1.1.0 is published already, and is not sent again\n" });
    } finally {
      writeFileSync(join(dir, "release", "install.sh"), script);
    }
  });

  it("ends a send that stopped once its release was whole and before latest.json named it: the release's own three are not sent again", () => {
    const out = released("1.0.0");
    expect(send("1.0.0", out, { FAULT: "refused latest.json" })).toMatchObject({ status: 1, stdout: "", stderr: "publish.sh: sending desktop/latest.json got 503\n" });
    const manifest = readFileSync(join(out, "manifest.json"));
    expect(object("releases/1.0.0/manifest.json")?.equals(manifest)).toBe(true);
    expect([object("latest.json"), object("install.sh")]).toEqual([null, null]);
    rmSync(join(dir, "curl-argv"), { force: true });
    expect(send("1.0.0", out)).toMatchObject({ status: 0, stdout: "published desktop/releases/1.0.0 as desktop/latest.json\n" });
    const sent = readFileSync(join(dir, "curl-argv"), "utf8").split(/^-q\n/m).filter((args) => args.includes("\n-T\n")).map((args) => args.trimEnd().split("\n").at(-1)?.replace(/^.*\/desktop\//, ""));
    expect(sent).toEqual(["latest.json", "install.sh"]);
    expect(object("latest.json")?.equals(manifest)).toBe(true);
    // Nothing of a send stays beside what was sent.
    expect(readdirSync(out).sort()).toEqual(["manifest.json", "manifest.json.sig", "surogate-desktop-1.0.0-linux-x64.tar.gz"]);
  });

  it("sends a release's own three again where a send stopped before its manifest, the mark that they are there", () => {
    const out = released("1.0.0");
    expect(send("1.0.0", out, { FAULT: "refused releases/1.0.0/manifest.json.sig" }).status).toBe(1);
    expect(object("releases/1.0.0/manifest.json")).toBeNull();
    expect(send("1.0.0", out)).toMatchObject({ status: 0, stdout: "published desktop/releases/1.0.0 as desktop/latest.json\n" });
    expect(object("releases/1.0.0/manifest.json")?.equals(readFileSync(join(out, "manifest.json")))).toBe(true);
  });

  it("takes a bucket's 403 for an object that is not there as its 404: R2 answers so to a token that may not list", () => {
    const out = released("1.0.0");
    // Both looks of a first send: for the release's own manifest, and for latest.json.
    expect(send("1.0.0", out, { FAULT: "forbidden releases/1.0.0/manifest.json" })).toMatchObject({ status: 1, stdout: "", stderr: "publish.sh: the bucket's desktop/releases/1.0.0/manifest.json is not what was sent\n" });
    expect(object("releases/1.0.0/surogate-desktop-1.0.0-linux-x64.tar.gz")).not.toBeNull();
    const next = released("1.1.0");
    expect(send("1.1.0", next, { FAULT: "forbidden latest.json" })).toMatchObject({ status: 1, stdout: "", stderr: "publish.sh: the bucket's desktop/latest.json is not what was sent\n" });
    expect(object("releases/1.1.0/manifest.json")?.equals(readFileSync(join(next, "manifest.json")))).toBe(true);
    // And any other answer is not "not there".
    expect(send("1.2.0", released("1.2.0"), { FAULT: "unread releases/1.2.0/manifest.json" })).toMatchObject({ status: 1, stdout: "", stderr: "publish.sh: looking for desktop/releases/1.2.0/manifest.json got 503\n" });
    expect(object("releases/1.2.0/surogate-desktop-1.2.0-linux-x64.tar.gz")).toBeNull();
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

  it("bounds every request to the bucket, and tries again the sending of latest.json, which is small and is what names the release", () => {
    rmSync(join(dir, "curl-argv"), { force: true });
    expect(send("1.0.0", released("1.0.0")).status).toBe(0);
    // Each request as curl was given it: its arguments from one -q to the next, its address the last.
    const requests = readFileSync(join(dir, "curl-argv"), "utf8").split(/^-q\n/m).filter(Boolean).map((args) => args.trimEnd().split("\n"));
    expect(requests.length).toBeGreaterThan(10);
    // One that cannot connect, or that stalls for a minute, stops there, and not at the job's time limit.
    for (const args of requests) expect(args.join(" "), args.at(-1)).toContain("--connect-timeout 30 --speed-limit 1024 --speed-time 60");
    // Tried again: latest.json, sent and read back.
    const again = requests.filter((args) => args.includes("--retry"));
    expect(again.map((args) => args.at(-1)?.replace(/^.*\/desktop\//, ""))).toEqual(["latest.json", "latest.json"]);
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

  it("stops at a bucket that refuses it, at the first object it would send: a 403 for a look is also what a bucket says of an object that is not there, and nothing is sent on the strength of it", () => {
    const refused = send("1.0.0", released("1.0.0"), { AWS_SECRET_ACCESS_KEY: "not-the-secret" });
    expect(refused).toMatchObject({ status: 1, stdout: "", stderr: "publish.sh: sending desktop/releases/1.0.0/surogate-desktop-1.0.0-linux-x64.tar.gz got 403\n" });
    for (const path of ["latest.json", "install.sh", "releases/1.0.0/manifest.json", "releases/1.0.0/manifest.json.sig", "releases/1.0.0/surogate-desktop-1.0.0-linux-x64.tar.gz"]) expect(object(path), path).toBeNull();
  });
});
