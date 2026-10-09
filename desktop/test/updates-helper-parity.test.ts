// The app takes for a release only what its root helper takes: a manifest that the helper's own
// reader refuses is never offered as an update, whose install the helper would then refuse. Each
// form of a manifest is read by both: by the helper's release_of, from release/install.sh as it is,
// run with bash and jq as the helper runs it; and by the app's signedRelease, signed by a listed key.

import { spawnSync } from "node:child_process";
import { generateKeyPairSync, sign } from "node:crypto";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { newer, signedRelease } from "../src/shell/updates.js";

const SCRIPT = fileURLToPath(new URL("../release/install.sh", import.meta.url));
const keys = generateKeyPairSync("ed25519");
let dir: string;

beforeAll(() => {
  dir = mkdtempSync(join(tmpdir(), "parity-"));
});

afterAll(() => {
  rmSync(dir, { recursive: true, force: true });
});

const SHA = "a".repeat(64);
// A manifest's fields, in the release job's order, with *fields* in place of its own.
const fields = (changed: Record<string, unknown> = {}, version = "1.2.4") => ({
  version, channel: "stable", platform: "linux", arch: "x64", url: `releases/${version}/surogate-desktop-${version}-linux-x64.tar.gz`, sha256: SHA, size: 171199516, stateSchema: 1, ...changed,
});
// As publish.sh sign writes one: one line, with its newline.
const line = (changed: Record<string, unknown> = {}, version?: string) => `${JSON.stringify(fields(changed, version))}\n`;
// The same line with *written* where a field's value is, as no JSON.stringify writes a number.
const written = (field: "size" | "stateSchema", number: string) => line({ [field]: "@" }).replace('"@"', number);

// What the helper's own reader says of *manifest*: what it takes it for, or null where it refuses it.
function helperTakes(manifest: Buffer): string | null {
  const file = join(dir, "manifest.json");
  writeFileSync(file, manifest);
  const read = spawnSync("bash", ["-c", `. <(sed '$d' "$1") && settings && release_of "$2"`, "_", SCRIPT, file], { encoding: "utf8" });
  return read.status === 0 ? read.stdout.trim() : null;
}

// What the app says of it, signed by a key it lists.
function appTakes(manifest: Buffer): string | null {
  try {
    const release = signedRelease("URL", manifest, sign(null, manifest, keys.privateKey), [keys.publicKey], "stable");
    return `${release.version} ${release.sha256} ${release.size}`;
  } catch {
    return null;
  }
}

const TAKEN = `1.2.4 ${SHA} 171199516`;
// Each form, and whether the helper takes it and whether the app does. Null for the helper: what jq
// makes of it is jq's own, and may change with jq; only the rule is asked of it, that the app
// takes it no further than the helper does.
const FORMS: Array<[name: string, manifest: string | Buffer, helper: boolean | null, app: boolean]> = [
  ["as the release job writes it", line(), true, true],
  ["with spaces around it", ` ${line().trimEnd()} \n`, true, true],
  ["with a carriage return before its newline", `${line().trimEnd()}\r\n`, true, true],
  ["of 4096 bytes", `${line().trimEnd()}${" ".repeat(4095 - line().trimEnd().length)}\n`, true, true],
  ["of 4097 bytes", `${line().trimEnd()}${" ".repeat(4096 - line().trimEnd().length)}\n`, false, false],
  ["without its newline", line().trimEnd(), false, false],
  ["over two lines", line().replace(",", ",\n"), false, false],
  ["as jq writes an object, a field a line", `${JSON.stringify(fields(), null, 2)}\n`, false, false],
  ["with an empty line after it", `${line()}\n`, false, false],
  ["with an empty line before it", `\n${line()}`, false, false],
  ["with a space after its newline", `${line()} `, false, false],
  ["twice on one line", `${line().trimEnd()}${line()}`, false, false],
  ["with a second document after it", `${line().trimEnd()} 1\n`, false, false],
  ["as a list of one", `[${line().trimEnd()}]\n`, false, false],
  ["null", "null\n", false, false],
  ["a number", "7\n", false, false],
  ["a word", '"1.2.4"\n', false, false],
  ["no JSON", "<html>\n", false, false],
  ["an empty object", "{}\n", false, false],
  ["nothing", "", false, false],
  ["a newline alone", "\n", false, false],
  ["with a byte order mark before it", `\uFEFF${line()}`, null, false],
  // A NUL where the release's own fields refuse nothing: only the reading of the NUL itself does.
  ["with a NUL in a field of its own", line({ note: "@" }).replace("@", "a\0b"), false, false],
  ["with a NUL after it", `${line().trimEnd()}\0\n`, false, false],
  ["with a NUL and then a second release", `${line({}, "9.9.9").trimEnd()}\0${line()}`, false, false],
  ["with a byte that is no UTF-8 in a field of its own", Buffer.concat([Buffer.from(line().trimEnd().slice(0, -1)), Buffer.from(',"note":"'), Buffer.from([0xff]), Buffer.from('"}\n')]), null, false],
  // A version: x.y.z in the ten digits, no part with a zero before it.
  ["of version 0.0.1", line({}, "0.0.1"), true, true],
  ["of version 10.20.30", line({}, "10.20.30"), true, true],
  ["with a zero before a version's last part", line({}, "1.2.04"), false, false],
  ["with a zero before a version's first part", line({}, "01.2.4"), false, false],
  ["with a zero before a version's middle part", line({}, "1.02.4"), false, false],
  ["of version 00.0.1", line({}, "00.0.1"), false, false],
  ["of a version of two parts", line({}, "1.2"), false, false],
  ["of a version with a suffix", line({}, "1.2.4-beta"), false, false],
  ["of a version in other digits", line({}, "1.2.\u0664"), false, false],
  ["of a version that climbs", line({}, "../../etc"), false, false],
  ["of a version that is a number", line({ version: 124 }), false, false],
  ["of a version written with an escape", line().replace('"version":"1.2.4"', '"version":"1.2.\\u0034"'), true, true],
  ["with its version named twice, the release's last", line().replace('{"version"', '{"version":"9.9.9","version"'), true, true],
  // Its other fields.
  ["of another channel", line({ channel: "beta" }), false, false],
  ["of another platform", line({ platform: "darwin" }), false, false],
  ["of another architecture", line({ arch: "arm64" }), false, false],
  ["with a url elsewhere", line({ url: "https://elsewhere.example/surogate.tar.gz" }), false, false],
  ["with another version's url", line({ url: "releases/1.2.5/surogate-desktop-1.2.5-linux-x64.tar.gz" }), false, false],
  ["with a hash in capitals", line({ sha256: "A".repeat(64) }), false, false],
  ["with a hash of 63 letters", line({ sha256: "a".repeat(63) }), false, false],
  // Its size and its state schema: whole numbers, the size above nothing, the schema from 1, each below 10^15.
  ["with a size written 1e3", written("size", "1e3").replace(String(171199516), "1000"), true, true],
  ["with a size written 171199516.0", written("size", "171199516.0"), true, true],
  ["with a size of nothing", line({ size: 0 }), false, false],
  ["with a size below nothing", line({ size: -1 }), false, false],
  ["with a size of one and a half", line({ size: 1.5 }), false, false],
  ["with a size in a word", line({ size: "171199516" }), false, false],
  ["with a size of 10^15", written("size", "1000000000000000"), false, false],
  ["with a size one below 10^15", written("size", "999999999999999"), true, true],
  // The helper compares a size as it is written: just below 10^15, it rounds to 10^15, which the app refuses.
  ["with a size written just below 10^15", written("size", "999999999999999.99"), null, false],
  ["with a size past every number", written("size", "1e400"), false, false],
  ["with a state schema written 1.0", written("stateSchema", "1.0"), true, true],
  ["with a state schema that rounds to 1", written("stateSchema", "0.9999999999999999999"), true, true],
  ["with a state schema of nothing", line({ stateSchema: 0 }), false, false],
  ["with a state schema of one and a half", line({ stateSchema: 1.5 }), false, false],
  ["with a state schema in a word", line({ stateSchema: "1" }), false, false],
  ["with a state schema of 10^15", written("stateSchema", "1000000000000000"), false, false],
  ["with a state schema written just below 10^15", written("stateSchema", "999999999999999.99"), false, false],
  ["without a state schema", line({ stateSchema: undefined }), false, false],
];

describe("a release's manifest, read by the app and by its root helper", () => {
  it("is taken by the app only where the helper takes it, and for the same release", () => {
    const read = FORMS.map(([name, manifest]) => {
      const bytes = Buffer.from(manifest);
      return { name, helper: helperTakes(bytes), app: appTakes(bytes) };
    });
    // The rule itself, whatever each takes: nothing the helper refuses is offered.
    expect(read.filter(({ helper, app }) => app !== null && helper === null).map(({ name }) => name)).toEqual([]);
    // And where both take one, they take it for the same version, hash and size.
    expect(read.filter(({ helper, app }) => app !== null && app !== helper).map(({ name, helper, app }) => ({ name, helper, app }))).toEqual([]);
  });

  it("is taken or refused by each as this list says: the helper as it is on this computer's jq, and the app", () => {
    expect(helperTakes(Buffer.from(line()))).toBe(TAKEN);
    expect(FORMS.map(([name, manifest, helper]) => [name, helper === null ? null : helperTakes(Buffer.from(manifest)) !== null, appTakes(Buffer.from(manifest)) !== null]))
      .toEqual(FORMS.map(([name, , helper, app]) => [name, helper, app]));
  });

  it("counts no version with a zero before a part as newer, or as one a newer is newer than", () => {
    expect([newer("1.2.04", "1.2.3"), newer("1.2.4", "1.2.03"), newer("01.2.4", "1.2.3"), newer("1.2.4", "1.2.3"), newer("0.0.1", "0.0.0")]).toEqual([false, false, false, true, true]);
  });
});
