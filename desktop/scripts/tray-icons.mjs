// The tray's two icons, from Surogate's mark (web/public/favicon.svg): its disc in one colour
// with the figure cut out, light for a dark panel and dark for a light one. Run once with
// Inkscape on the PATH, from desktop/: node scripts/tray-icons.mjs; the PNGs are committed.

import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const desktop = join(import.meta.dirname, "..");
const mark = readFileSync(join(desktop, "..", "web", "public", "favicon.svg"), "utf8");
// The mark's figure is its longest path; the other is the registered sign, left out at this size.
const figure = [...mark.matchAll(/<path class="cls-1" d="([^"]+)"/g)].map((found) => found[1]).sort((a, b) => b.length - a.length)[0];
if (!figure) throw new Error("No figure in favicon.svg");
const scratch = mkdtempSync(join(tmpdir(), "tray-icons-"));
try {
  for (const [name, colour] of [["tray-dark", "#f5f4ed"], ["tray-light", "#3d3d3a"]]) {
    const svg = join(scratch, `${name}.svg`);
    writeFileSync(svg, `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 113 113"><mask id="m"><rect width="113" height="113" fill="#fff"/><path d="${figure}" fill="#000"/></mask><circle cx="56.5" cy="56.5" r="56.5" fill="${colour}" mask="url(#m)"/></svg>`);
    // With no session bus: Inkscape reaches nothing of the desktop's session, and starts nothing on it.
    execFileSync("inkscape", [svg, "--export-type=png", "--export-width=32", "--export-height=32", `--export-filename=${join(desktop, "assets", `${name}.png`)}`], {
      stdio: "ignore", env: { ...process.env, DBUS_SESSION_BUS_ADDRESS: "disabled:" },
    });
  }
} finally {
  rmSync(scratch, { recursive: true, force: true });
}
