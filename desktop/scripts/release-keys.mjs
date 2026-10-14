// The release keys an app that has no install script of its own checks its updates by, Windows's: install.sh's list,
// as the app reads it (listedKeys, src/shell/updates.ts), written again in the list's one form, which that reader takes
// back key for key. A script whose list the app reads no key of gives no copy: an app that shipped one would take no
// later release. scripts/package-win.sh writes it at resources\release-keys, after npm run build.
//
//   node scripts/release-keys.mjs <install.sh> <out>
import { readFileSync, writeFileSync } from "node:fs";

import { listedKeys } from "../dist/shell/updates.js";

const [script, out, ...rest] = process.argv.slice(2);
if (!script || !out || rest.length > 0) {
  process.stderr.write("usage: node scripts/release-keys.mjs <install.sh> <out>\n");
  process.exit(2);
}
const keys = listedKeys(readFileSync(script, "utf8")).map((key) => key.export({ type: "spki", format: "pem" }).toString().trim());
if (keys.length === 0) {
  process.stderr.write(`release-keys.mjs: ${script} lists no release key in the one form that an install reads\n`);
  process.exit(1);
}
writeFileSync(out, `RELEASE_KEYS=(\n${keys.map((key) => `  '${key}'`).join("\n")}\n)\n`);
