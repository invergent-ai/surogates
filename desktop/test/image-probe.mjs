// The guest image's delivery (dist/vm/image.js) where the app unpacks its image with Node's own zstd, on a Node whose
// zlib has zstd, which this package's tests' own Node 22.14 has not: run by vm-image-node.test.ts in this package's
// Electron, as its main process, on Windows and on a Mac; on Linux in the app's own node, bin/node, with the module
// imported as *platform*'s app imports it. Each answer is one line: PROBE, then its JSON.
//   <runtime> image-probe.mjs <platform> pack <folder> <level> <name>...  each <folder>/<name> made <name>.zst at that level
//   <runtime> image-probe.mjs <platform> deliver <options>                deliver(), with the options' JSON: the folder, or why not
//   <runtime> image-probe.mjs <platform> delivery <options> [check]       an ImageDelivery started, its files checked first, and waited for
//   <runtime> image-probe.mjs <platform> quit <options>                   deliver(), its signal aborted as its first unpack is under way
import { createReadStream, createWriteStream, readdirSync } from "node:fs";
import { join } from "node:path";
import { pipeline } from "node:stream/promises";
import * as zlib from "node:zlib";

const [platform, action, ...rest] = process.argv.slice(2);
const answer = (value) => process.stdout.write(`PROBE ${JSON.stringify(value)}\n`, () => process.exit(0));

// Everything within the one try, so that whatever goes wrong is answered: in Electron's main process an error left to
// itself is shown in a dialog, which no one closes.
try {
  if (platform !== process.platform) Object.defineProperty(process, "platform", { ...Object.getOwnPropertyDescriptor(process, "platform"), value: platform });
  const { deliver, ImageDelivery } = await import(new URL("../dist/vm/image.js", import.meta.url).href);
  if (action === "pack") {
    const [folder, level, ...names] = rest;
    for (const name of names) {
      const params = { [zlib.constants.ZSTD_c_compressionLevel]: Number(level) };
      await pipeline(createReadStream(join(folder, name)), zlib.createZstdCompress({ params }), createWriteStream(join(folder, `${name}.zst`)));
    }
    answer({ packed: names });
  } else if (action === "deliver") {
    answer({ folder: await deliver(JSON.parse(rest[0])) });
  } else if (action === "delivery") {
    const options = JSON.parse(rest[0]);
    const delivery = new ImageDelivery({ ...options, base: () => options.base });
    delivery.start(rest[1] === "check");
    await delivery.wait(new AbortController().signal);
    answer({ state: delivery.state });
  } else if (action === "quit") {
    const options = JSON.parse(rest[0]);
    const stop = new AbortController();
    const work = join(options.images, `${options.manifest.key}.partial`);
    try {
      await deliver({ ...options, signal: stop.signal, unpacking: () => setTimeout(() => stop.abort(new Error("the app quit")), 100) });
      answer({ folder: "delivered whole before its quit" });
    } catch (error) {
      answer({ error: error.message, left: readdirSync(work).sort() });
    }
  } else {
    answer({ error: `no action ${action}` });
  }
} catch (error) {
  answer({ error: error instanceof Error ? error.message : String(error) });
}
