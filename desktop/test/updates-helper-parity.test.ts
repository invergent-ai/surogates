// The app takes for a release what its root helper takes, and nothing else: a manifest that the
// helper's own reader refuses is never offered as an update, whose install the helper would then
// refuse; and one the helper would install is never kept from its user. Each form of a manifest is
// read by both: by the helper's release_of, from release/install.sh as it is, run with bash and jq
// as the helper runs it; and by the app's signedRelease, signed by a listed key.

import { spawnSync } from "node:child_process";
import { generateKeyPairSync, sign } from "node:crypto";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { afterAll, beforeAll, describe, expect, it } from "vitest";

import { newer, signedRelease } from "../src/shell/updates.js";

import { FORMS, line, TAKEN } from "./manifest-forms.js";

const SCRIPT = fileURLToPath(new URL("../release/install.sh", import.meta.url));
const keys = generateKeyPairSync("ed25519");
let dir: string;

beforeAll(() => {
  dir = mkdtempSync(join(tmpdir(), "parity-"));
});

afterAll(() => {
  rmSync(dir, { recursive: true, force: true });
});

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

describe("a release's manifest, read by the app and by its root helper", () => {
  it("is taken by the app where the helper takes it and nowhere else, and for the same release", () => {
    const read = FORMS.map(([name, manifest]) => {
      const bytes = Buffer.from(manifest);
      return { name, helper: helperTakes(bytes), app: appTakes(bytes) };
    });
    // The rule itself, whatever each takes: nothing the helper refuses is offered, and nothing it
    // would install is kept from the user.
    const byOneAlone = (takes: "app" | "helper", refuses: "app" | "helper") => read.filter((form) => form[takes] !== null && form[refuses] === null).map(({ name }) => name);
    expect({ offeredAndThenRefused: byOneAlone("app", "helper"), neverOffered: byOneAlone("helper", "app") }).toEqual({ offeredAndThenRefused: [], neverOffered: [] });
    // And where both take one, they take it for the same version, hash and size.
    expect(read.filter(({ helper, app }) => app !== null && app !== helper).map(({ name, helper, app }) => ({ name, helper, app }))).toEqual([]);
  });

  it("is taken or refused by each as this list says: the helper as it is on this computer's jq, and the app", () => {
    expect(helperTakes(Buffer.from(line()))).toBe(TAKEN);
    expect(FORMS.map(([name, manifest]) => [name, helperTakes(Buffer.from(manifest)) !== null, appTakes(Buffer.from(manifest)) !== null]))
      .toEqual(FORMS.map(([name, , helper, app]) => [name, helper, app]));
  });

  it("counts no version with a zero before a part as newer, or as one a newer is newer than", () => {
    expect([newer("1.2.04", "1.2.3"), newer("1.2.4", "1.2.03"), newer("01.2.4", "1.2.3"), newer("1.2.4", "1.2.3"), newer("0.0.1", "0.0.0")]).toEqual([false, false, false, true, true]);
  });
});
