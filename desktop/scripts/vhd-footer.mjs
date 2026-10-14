// The agent disk as Windows opens it, a fixed VHD: the raw disk that vm/agent-disk.sh makes, given its VHD footer in
// place by the app's own code (src/vm/vhd.ts), unless it ends in it already. The Windows workflows run it on the
// agent disk they are handed, after npm run build.
//
//   node scripts/vhd-footer.mjs <disk>
import { addFooter } from "../dist/vm/vhd.js";

const [disk, ...rest] = process.argv.slice(2);
if (!disk || rest.length > 0) {
  process.stderr.write("usage: node scripts/vhd-footer.mjs <disk>\n");
  process.exit(2);
}
try {
  await addFooter(disk);
} catch (error) {
  process.stderr.write(`vhd-footer.mjs: ${disk}: ${error instanceof Error ? error.message : String(error)}\n`);
  process.exit(1);
}
