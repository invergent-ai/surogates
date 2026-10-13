// The app's native programs for Windows (native/windows): one Cargo workspace, every crate in it, one toolchain and
// one lock, nothing from git; and the one script that builds every native part of the Windows app.

import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

const DESKTOP = fileURLToPath(new URL("..", import.meta.url));
const WINDOWS = join(DESKTOP, "native", "windows");
const read = (...path: string[]) => readFileSync(join(...path), "utf8");
// Each folder of native/windows with a crate of its own.
const crates = () => readdirSync(WINDOWS, { withFileTypes: true })
  .filter((entry) => entry.isDirectory() && existsSync(join(WINDOWS, entry.name, "Cargo.toml"))).map(({ name }) => name).sort();

describe("the Windows native programs", () => {
  it("are one workspace, with every crate of native/windows a member", () => {
    const members = /^members = \[([^\]]*)\]$/m.exec(read(WINDOWS, "Cargo.toml"))?.[1]?.match(/"[^"]+"/g)?.map((name) => name.slice(1, -1)) ?? [];
    expect(crates()).toContain("helper");
    expect([...members].sort()).toEqual(crates());
  });

  it("are built by one toolchain and one lock, at the workspace's root alone, with nothing from git", () => {
    for (const crate of crates()) {
      for (const own of ["Cargo.lock", "rust-toolchain.toml", "rust-toolchain"]) expect(existsSync(join(WINDOWS, crate, own)), `${crate}/${own}`).toBe(false);
      expect(read(WINDOWS, crate, "Cargo.toml"), crate).not.toMatch(/\bgit\s*=|^\[profile/m);
    }
    expect(read(WINDOWS, "rust-toolchain.toml")).toMatch(/^channel = "1\.99\.0"$/m);
    const lock = read(WINDOWS, "Cargo.lock");
    expect(lock).not.toContain("git+");
    for (const name of ["hcs", "pipe", "service", "vm", "surogate-helper"]) expect(lock, name).toContain(`\nname = "${name}"\n`);
  });

  it("are built, with every other native part of the Windows app, by one script", () => {
    const { scripts } = JSON.parse(read(DESKTOP, "package.json")) as { scripts: Record<string, string> };
    const steps = scripts["build:win32"]!.split(" && ");
    // The app's code first, which ntat is built beside (dist/native); then the app's rg, the node its hosts run on, the
    // one first on the PATH, as a development build's on a Mac (scripts/mac-build.sh); ntat, by Windows PowerShell with
    // its own modules, not those of a PowerShell 7 the build may have been started from (Windows PowerShell reads
    // PSModulePath as it finds it); and last the workspace, from its own folder, whose rust-toolchain.toml picks the
    // toolchain.
    expect(steps.slice(-5)).toEqual([
      "scripts/rg.sh",
      'cp "$(command -v node)" bin/node.exe',
      "env -u PSModulePath powershell -NoProfile -ExecutionPolicy Bypass -File native/ntat/build.ps1",
      "cd native/windows",
      "cargo build --release --locked --workspace",
    ]);
    expect(steps.indexOf("tsc -p tsconfig.build.json")).toBeGreaterThan(-1);
    expect(steps.indexOf("tsc -p tsconfig.build.json")).toBeLessThan(steps.length - 5);
    // No other script builds a part of native/windows.
    for (const [name, script] of Object.entries(scripts)) if (name !== "build:win32") expect(script, name).not.toMatch(/native\/windows|ntat|cargo/);
  });
});
