// The list of release keys in a script, read by each of its three readers in each spelling
// (key-list-forms.ts): the install script's own reader, which every install and every helper
// reads a helper's list with; the release job, which writes and signs a manifest only for a script
// whose list reads so; and the app. One answer from all three: a release whose helper's list one
// takes and another does not is installed, and then takes no later release.

import { spawnSync } from "node:child_process";
import { createHash, createPublicKey, generateKeyPairSync, type KeyObject } from "node:crypto";
import { chmodSync, copyFileSync, mkdirSync, mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { releaseKeys } from "../src/shell/updates.js";

import { inForm, KEY_LISTS } from "./key-list-forms.js";

const RELEASE = fileURLToPath(new URL("../release", import.meta.url));
const pem = (key: KeyObject) => key.export({ type: "spki", format: "pem" }).toString().trim();
const first = generateKeyPairSync("ed25519");
const second = generateKeyPairSync("ed25519");
const PRIVATE = first.privateKey.export({ type: "pkcs8", format: "pem" }).toString();
// The repository's script with two keys of the test's own as its list, in the list's form.
const SCRIPT = inForm(readFileSync(join(RELEASE, "install.sh"), "utf8"), [pem(first.publicKey), pem(second.publicKey)]);
let dir: string;

beforeAll(() => {
  dir = mkdtempSync(join(tmpdir(), "key-list-"));
});

afterAll(() => {
  rmSync(dir, { recursive: true, force: true });
});

// The entries that the install script's own reader takes of *script*'s list: those of listed, from
// the repository's script's functions without its last line.
function helperLists(script: string): string[] {
  const file = join(dir, "helper.sh");
  writeFileSync(file, script);
  const read = spawnSync("bash", ["-c", `. <(sed '$d' "$1") && settings && listed keys "$2" && for key in "\${keys[@]}"; do printf '%s\\0' "$key"; done`, "_", join(RELEASE, "install.sh"), file], { encoding: "utf8" });
  expect(read.status, read.stderr).toBe(0);
  return read.stdout.split("\0").slice(0, -1);
}

// How many keys that reader reads of it: its entries that are keys as OpenSSL writes one.
function helperReads(script: string): number {
  return helperLists(script).filter((entry) => {
    try {
      const key = createPublicKey(entry);
      return key.asymmetricKeyType === "ed25519" && pem(key) === entry;
    } catch {
      return false;
    }
  }).length;
}

// How many the app reads.
function appReads(script: string): number {
  const file = join(dir, "app.sh");
  writeFileSync(file, script);
  try {
    return releaseKeys(file).length;
  } catch {
    return 0;
  }
}

// Whether the release job writes and signs a manifest of a release whose install script is
// *script*: each of its two steps, with the script beside publish.sh and in the tarball.
function releaseJob(script: string, name: string): { describe: string; sign: string } {
  const root = mkdtempSync(join(dir, "job-"));
  const out = join(root, "out");
  mkdirSync(join(root, "release"));
  mkdirSync(out);
  copyFileSync(join(RELEASE, "publish.sh"), join(root, "release", "publish.sh"));
  chmodSync(join(root, "release", "publish.sh"), 0o755);
  writeFileSync(join(root, "release", "install.sh"), script, { mode: 0o755 });
  const top = join(root, "tree", "surogate-desktop-1.2.3-linux-x64");
  mkdirSync(join(top, "bin"), { recursive: true });
  mkdirSync(join(top, "resources", "app"), { recursive: true });
  writeFileSync(join(top, "surogate"), "#!/bin/sh\n", { mode: 0o755 });
  writeFileSync(join(top, "resources", "app", "package.json"), JSON.stringify({ version: "1.2.3", stateSchema: 1 }));
  writeFileSync(join(top, "bin", "surogate-apply-update"), script, { mode: 0o755 });
  const tarball = join(out, "surogate-desktop-1.2.3-linux-x64.tar.gz");
  expect(spawnSync("tar", ["--owner=0", "--group=0", "-C", join(root, "tree"), "-czf", tarball, "surogate-desktop-1.2.3-linux-x64"]).status, name).toBe(0);
  const { DESKTOP_RELEASE_KEY: _held, ...env } = process.env;
  const built = { DESKTOP_TARBALL_SHA256: createHash("sha256").update(readFileSync(tarball)).digest("hex"), DESKTOP_TARBALL_SIZE: String(statSync(tarball).size) };
  const step = (verb: string, more: Record<string, string>) => {
    const ran = spawnSync(join(root, "release", "publish.sh"), [verb, "1.2.3", out], { encoding: "utf8", env: { ...env, TMPDIR: root, ...more } });
    return ran.status === 0 ? "does" : ran.stderr.trim();
  };
  const described = step("describe", { DESKTOP_TARBALL_SHA256: built.DESKTOP_TARBALL_SHA256 });
  // The signing is asked by itself too, of a manifest that the repository's own script's job wrote.
  if (described !== "does") writeFileSync(join(out, "manifest.json"), `${JSON.stringify({ version: "1.2.3", channel: "stable", platform: "linux", arch: "x64", url: "releases/1.2.3/surogate-desktop-1.2.3-linux-x64.tar.gz", sha256: built.DESKTOP_TARBALL_SHA256, size: Number(built.DESKTOP_TARBALL_SIZE), stateSchema: 1 })}\n`);
  return { describe: described, sign: step("sign", { DESKTOP_RELEASE_KEY: PRIVATE, ...built, DESKTOP_STATE_SCHEMA: "1" }) };
}

describe("a script's list of release keys, read by the install script, by the release job and by the app", () => {
  it("is read alike by the install script's own reader and by the app, in every spelling: the list in its one form, and no key of any other", () => {
    expect(KEY_LISTS.map(([name, written]) => [name, helperReads(written(SCRIPT)), appReads(written(SCRIPT))]))
      .toEqual(KEY_LISTS.map(([name, , read]) => [name, read, read]));
  });

  it("is taken by the install script's reader entry for entry as it is written: no entry of a list out of its form, and none in another spelling than the list's own, whether or not it is a key", () => {
    // What the reader hands on is what a signature is asked of, by OpenSSL, which reads more
    // spellings of a key than the one: so the entries are counted before any is asked.
    expect(KEY_LISTS.map(([name, written]) => [name, helperLists(written(SCRIPT)).length]))
      .toEqual(KEY_LISTS.map(([name, , read, entries = read]) => [name, entries]));
    expect(helperLists(SCRIPT)).toEqual([pem(first.publicKey), pem(second.publicKey)]);
  });

  it("is no list where the script ends in it: a helper cut short behind a key, in one, or behind the line that opens its list hands on no key that it has whole", () => {
    // Not among the spellings: the release job says of such a script that its last line is not
    // the one that runs it, before it asks its list. An install reads the helper that is there.
    const last = "-----END PUBLIC KEY-----'\n";
    const behindFirst = SCRIPT.indexOf(last) + last.length;
    const cuts: Array<[string, string]> = [
      ["behind its last key", SCRIPT.slice(0, SCRIPT.lastIndexOf(last) + last.length)],
      ["behind its first key", SCRIPT.slice(0, behindFirst)],
      ["in its second key", SCRIPT.slice(0, SCRIPT.indexOf("\n", SCRIPT.indexOf("-----BEGIN PUBLIC KEY-----", behindFirst)) + 1)],
      ["behind its last key, with no newline", SCRIPT.slice(0, SCRIPT.lastIndexOf(last) + last.length - 1)],
      ["behind the line that opens its list", SCRIPT.slice(0, SCRIPT.indexOf("RELEASE_KEYS=(\n") + "RELEASE_KEYS=(\n".length)],
    ];
    expect(cuts.map(([name, cut]) => [name, cut.includes("-----BEGIN PUBLIC KEY-----") || name.startsWith("behind the line"), helperLists(cut), appReads(cut)]))
      .toEqual(cuts.map(([name]) => [name, true, [], 0]));
    // And whole to the line that closes its list, it is the list: what follows that line is no part of it.
    const closed = SCRIPT.slice(0, SCRIPT.indexOf("\n  )\n", behindFirst) + "\n  )\n".length);
    expect([helperLists(closed).length, appReads(closed)]).toEqual([2, 2]);
  });

  it("is described and signed by the release job only where every install reads it as bash set it: both keys, each one that loads", { timeout: 120_000 }, () => {
    const notRead = "publish.sh: install.sh's list of release keys is not in the one form that an install reads (see the list in install.sh): a computer that installed this release would take no later one";
    expect(KEY_LISTS.map(([name, written]) => [name, releaseJob(written(SCRIPT), name)]))
      .toEqual(KEY_LISTS.map(([name]) => [name, name === "as it is" ? { describe: "does", sign: "does" } : { describe: notRead, sign: notRead }]));
  });

  it("holds the repository's own list to that form", () => {
    const script = readFileSync(join(RELEASE, "install.sh"), "utf8");
    expect(helperReads(script)).toBeGreaterThan(0);
    expect(appReads(script)).toBe(helperReads(script));
    expect(script.match(/^ *RELEASE_KEYS\+?=/gm)).toHaveLength(1);
  });
});
