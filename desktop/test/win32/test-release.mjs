// A release for the test of an update on Windows (test/msi.test.ps1), staged as the app stages one in its cache: in
// <folder>, *msi* as release.msi, its manifest of *version* and *channel* as the release job writes one, and the
// manifest's signature by a key made here; and that key in <keys>, in the list form an install's release-keys has
// (scripts/release-keys.mjs). With --other, the manifest is signed by a second key, which the list does not hold.
//
//   node test/win32/test-release.mjs <folder> <msi> <version> <channel> <keys> [--other]
import { createHash, generateKeyPairSync, sign } from "node:crypto";
import { copyFileSync, mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const [folder, msi, version, channel, keys, ...rest] = process.argv.slice(2);
if (!folder || !msi || !version || !channel || !keys || rest.some((arg) => arg !== "--other")) {
  process.stderr.write("usage: node test/win32/test-release.mjs <folder> <msi> <version> <channel> <keys> [--other]\n");
  process.exit(2);
}
mkdirSync(folder, { recursive: true });
const payload = readFileSync(msi);
copyFileSync(msi, join(folder, "release.msi"));
const manifest = Buffer.from(`${JSON.stringify({
  version, channel, platform: "win32", arch: "x64", url: `releases/${version}/win32-x64/surogate-desktop-${version}-win32-x64.msi`,
  sha256: createHash("sha256").update(payload).digest("hex"), size: payload.length, stateSchema: 1,
})}\n`);
writeFileSync(join(folder, "manifest.json"), manifest);
const listed = generateKeyPairSync("ed25519");
const signer = rest.includes("--other") ? generateKeyPairSync("ed25519") : listed;
writeFileSync(join(folder, "manifest.json.sig"), sign(null, manifest, signer.privateKey));
const pem = listed.publicKey.export({ type: "spki", format: "pem" }).toString().trim();
writeFileSync(keys, `RELEASE_KEYS=(\n  '${pem}'\n)\n`);
