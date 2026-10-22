// Surogate.app as scripts/package-mac.sh lays it out, as a person drags it to Applications. On any OS: what the
// script refuses before it lays anything out. On the macOS runner, where
// SUROGATE_MAC_LAYOUT names a bundle the script made of SUROGATE_MAC_VERSION, with the agent disk and VM manifest
// SUROGATE_MAC_AGENT_DISK and SUROGATE_MAC_VM_MANIFEST name: the bundle itself, every file the app's code looks for
// where it looks, every program in it Apple silicon's alone, and each signed ad hoc with its own entitlements.

import { spawnSync } from "node:child_process";
import { closeSync, existsSync, lstatSync, mkdtempSync, openSync, readdirSync, readFileSync, readSync, realpathSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, relative, sep } from "node:path";
import { fileURLToPath } from "node:url";

import { FuseV1Options, getCurrentFuseWire } from "@electron/fuses";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { listedKeys } from "../src/shell/updates.js";

const DESKTOP = fileURLToPath(new URL("..", import.meta.url));
const MACOS = join(DESKTOP, "macos");
const HELPERS = ["", " (GPU)", " (Renderer)", " (Plugin)"];
let dir: string;

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "package-mac-"));
});

afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

describe.skipIf(process.platform === "darwin")("scripts/package-mac.sh, elsewhere than on a Mac", () => {
  const packaged = (...args: string[]) => spawnSync("bash", [join(DESKTOP, "scripts", "package-mac.sh"), ...args], { encoding: "utf8" });

  it("lays nothing out but on a Mac with Apple silicon, whose Electron npm installed, and says so before it fetches anything", () => {
    const [manifest, disk] = [join(dir, "manifest.json"), join(dir, "agent.img")];
    writeFileSync(manifest, JSON.stringify({ arch: "arm64" }));
    writeFileSync(disk, "");
    expect(packaged("1.2.3", manifest, disk, join(dir, "out"))).toMatchObject({ status: 1, stderr: "package-mac.sh lays out the macOS app on a Mac with Apple silicon, whose Electron npm installed\n" });
    expect(existsSync(join(dir, "out"))).toBe(false);
  });

  it("is told a version, a VM manifest, an agent disk and a folder, and nothing more", () => {
    const [manifest, disk] = [join(dir, "manifest.json"), join(dir, "agent.img")];
    writeFileSync(manifest, "{}");
    writeFileSync(disk, "");
    const usage = "usage: scripts/package-mac.sh <x.y.z> <vm manifest.json> <agent disk> <out>\n";
    for (const args of [[], ["1.2.3", manifest, disk], ["1.2", manifest, disk, dir], ["1.2.3", join(dir, "none.json"), disk, dir], ["1.2.3", manifest, join(dir, "none.img"), dir], ["1.2.3", manifest, disk, dir, "more"]]) {
      expect(packaged(...args), args.join(" ")).toMatchObject({ status: 2, stderr: usage });
    }
  });

  it("has sign-mac.sh sign on a Mac alone, and say its usage to a verb, a signer or a Team ID it does not take", () => {
    const signing = (...args: string[]) => spawnSync("bash", [join(DESKTOP, "scripts", "sign-mac.sh"), ...args], { encoding: "utf8" });
    expect(signing("sign", join(dir, "Surogate.app"), "-")).toMatchObject({ status: 1, stderr: "sign-mac.sh: it signs on a Mac, with Apple's own tools\n" });
    for (const args of [[], ["seal", dir], ["sign", dir], ["verify", dir, "someone"], ["verify", dir, "adhoc", "more"], ["verify", dir, "trial"], ["verify", dir, "developer-id", "abcde12345"], ["dmg-check", "x.dmg", dir, "developer-id"], ["notarize"], ["forget"], ["staged", dir]]) {
      expect(signing(...args), args.join(" ")).toMatchObject({ status: 2, stderr: expect.stringMatching(/^usage: sign-mac\.sh /) });
    }
  });
});

// The bundle's own files, or its links, each by its path from the bundle's top, never through a link to a folder, as a
// framework's Versions/Current is.
function entriesOf(top: string, links = false): string[] {
  const found: string[] = [];
  const walk = (folder: string) => {
    for (const entry of readdirSync(folder, { withFileTypes: true })) {
      const path = join(folder, entry.name);
      if (entry.isDirectory()) walk(path);
      else if (links ? entry.isSymbolicLink() : entry.isFile()) found.push(relative(top, path).split(sep).join("/"));
    }
  };
  walk(top);
  return found.sort();
}
const filesOf = (top: string): string[] => entriesOf(top);

// What a file is, by its first eight bytes: a Mach-O file of one arch and its CPU type, a universal one, or neither.
function machO(path: string): "arm64" | "other" | "universal" | null {
  const head = Buffer.alloc(8);
  const fd = openSync(path, "r");
  try {
    if (readSync(fd, head, 0, 8, 0) < 8) return null;
  } finally {
    closeSync(fd);
  }
  if (head.readUInt32BE(0) === 0xcafebabe) return "universal";
  if (head.readUInt32LE(0) !== 0xfeedfacf && head.readUInt32LE(0) !== 0xfeedface) return null;
  return head.readUInt32LE(4) === 0x0100000c ? "arm64" : "other";
}

// A plist as JSON, by macOS's own plutil.
function plist(path: string): Record<string, unknown> {
  const read = spawnSync("plutil", ["-convert", "json", "-o", "-", path], { encoding: "utf8" });
  expect(read.status, read.stderr).toBe(0);
  return JSON.parse(read.stdout) as Record<string, unknown>;
}

const LAYOUT = process.env.SUROGATE_MAC_LAYOUT;
const VERSION = process.env.SUROGATE_MAC_VERSION ?? "";

describe.runIf(process.platform === "darwin" && LAYOUT)("Surogate.app's layout", { timeout: 120_000 }, () => {
  const top = LAYOUT!;
  const at = (...path: string[]) => join(top, ...path);
  const electron = join(DESKTOP, "node_modules", "electron", "dist", "Electron.app");

  it("has Surogate, Electron's program renamed, beside surogate-vm and surogate-helper, with the app's fuses, and no default app", async () => {
    expect(readdirSync(at("Contents", "MacOS")).sort()).toEqual(["Surogate", "surogate-helper", "surogate-vm"]);
    expect(existsSync(at("Contents", "Resources", "default_app.asar"))).toBe(false);
    const wire = await getCurrentFuseWire(top);
    const on = (fuse: FuseV1Options) => String.fromCharCode(wire[fuse] as number);
    expect([FuseV1Options.RunAsNode, FuseV1Options.EnableNodeOptionsEnvironmentVariable, FuseV1Options.EnableNodeCliInspectArguments, FuseV1Options.EnableCookieEncryption].map(on))
      .toEqual(["0", "0", "0", "1"]);
  });

  it("has Info.plist as Electron's, with Surogate's keys in place of its own and no asar's hash", () => {
    const keys = JSON.parse(readFileSync(join(MACOS, "info-plist.json"), "utf8").replaceAll("@VERSION@", VERSION)) as Record<string, unknown>;
    const theirs = plist(join(electron, "Contents", "Info.plist"));
    const ours = plist(at("Contents", "Info.plist"));
    const { ElectronAsarIntegrity: _asar, ...kept } = theirs;
    expect(ours).toEqual({ ...kept, ...keys });
    expect(ours.CFBundleShortVersionString).toBe(VERSION);
  });

  it("has Electron's four helper apps, each renamed Surogate's with its program, and its own identifier", () => {
    const frameworks = readdirSync(at("Contents", "Frameworks")).sort();
    expect(frameworks).toEqual([
      "Electron Framework.framework", "Mantle.framework", "ReactiveObjC.framework", "Squirrel.framework",
      "Surogate Helper (GPU).app", "Surogate Helper (Plugin).app", "Surogate Helper (Renderer).app", "Surogate Helper.app",
    ]);
    for (const kind of HELPERS) {
      const helper = at("Contents", "Frameworks", `Surogate Helper${kind}.app`, "Contents");
      expect(readdirSync(join(helper, "MacOS")), kind).toEqual([`Surogate Helper${kind}`]);
      const suffix = kind.replace(/[ ()]/g, "").toLowerCase();
      expect(plist(join(helper, "Info.plist")), kind).toMatchObject({
        CFBundleName: `Surogate Helper${kind}`, CFBundleExecutable: `Surogate Helper${kind}`, CFBundleIdentifier: `ai.invergent.surogate.helper${suffix ? `.${suffix}` : ""}`,
      });
    }
  });

  it("has every file of Electron's build under its new name, each but its programs, plists and seals as Electron made it", () => {
    const renamed = (file: string) => file.replace(/^Contents\/MacOS\/Electron$/, "Contents/MacOS/Surogate").replaceAll("Electron Helper", "Surogate Helper");
    const theirs = filesOf(electron).filter((file) => !["Contents/Resources/default_app.asar", "Contents/Resources/electron.icns"].includes(file));
    const ours = new Set(filesOf(top));
    expect(theirs.map(renamed).filter((file) => !ours.has(file))).toEqual([]);
    for (const file of theirs) {
      if (machO(join(electron, file)) !== null || file.endsWith("Info.plist") || file.includes("_CodeSignature/")) continue;
      expect(readFileSync(at(renamed(file))).equals(readFileSync(join(electron, file))), file).toBe(true);
    }
    expect(readFileSync(at("Contents", "Resources", "surogate.icns")).equals(readFileSync(join(MACOS, "surogate.icns")))).toBe(true);
  });

  it("has the app as a folder, of the release's version, with fsat in dist/native, and its own node, rg and surogate-spawn in bin", () => {
    const app = (...path: string[]) => at("Contents", "Resources", "app", ...path);
    expect(readdirSync(app()).sort()).toEqual(["assets", "bin", "dictionaries", "dist", "node_modules", "package.json"]);
    const own = JSON.parse(readFileSync(join(DESKTOP, "package.json"), "utf8")) as Record<string, unknown>;
    expect(JSON.parse(readFileSync(app("package.json"), "utf8"))).toEqual({ ...own, version: VERSION });
    expect(statSync(app("dist", "native", "fsat.node")).isFile()).toBe(true);
    expect(existsSync(app("dist", "testing"))).toBe(false);
    expect(filesOf(app("dist")).filter((file) => file.endsWith(".map"))).toEqual([]);
    expect(readdirSync(app("bin")).sort()).toEqual(["node", "rg", "surogate-spawn"]);
    const version = /^VERSION=(v[\d.]+)$/m.exec(readFileSync(join(DESKTOP, "scripts", "node.sh"), "utf8"))?.[1];
    expect(spawnSync(app("bin", "node"), ["-p", "`${process.version} ${process.platform}-${process.arch}`"], { encoding: "utf8" }).stdout).toBe(`${version} darwin-arm64\n`);
    expect(spawnSync(app("bin", "rg"), ["--version"], { encoding: "utf8" }).stdout).toMatch(/^ripgrep 15\.2\.0\b/);
    // Production's packages alone, and none of srt's Linux or Windows programs, which the macOS app never runs.
    expect(existsSync(app("node_modules", "vitest"))).toBe(false);
    expect(existsSync(app("node_modules", "@anthropic-ai", "sandbox-runtime", "package.json"))).toBe(true);
    expect(readdirSync(app("node_modules", "@anthropic-ai", "sandbox-runtime", "vendor"))).not.toEqual(expect.arrayContaining(["seccomp"]));
    expect(readdirSync(app("node_modules", "@anthropic-ai", "sandbox-runtime", "vendor"))).not.toEqual(expect.arrayContaining(["srt-win"]));
  });

  it("has the agent disk and the guest image's manifest it was given, where the app reads them", () => {
    expect(readdirSync(at("Contents", "Resources", "vm")).sort()).toEqual(["agent.img", "manifest.json"]);
    expect(readFileSync(at("Contents", "Resources", "vm", "agent.img")).equals(readFileSync(process.env.SUROGATE_MAC_AGENT_DISK!))).toBe(true);
    expect(readFileSync(at("Contents", "Resources", "vm", "manifest.json")).equals(readFileSync(process.env.SUROGATE_MAC_VM_MANIFEST!))).toBe(true);
  });

  it("has install.sh's release keys, key for key, as the app reads them, in one file that only its owner writes", () => {
    const pem = (keys: ReturnType<typeof listedKeys>) => keys.map((key) => key.export({ type: "spki", format: "pem" }).toString());
    const theirs = pem(listedKeys(readFileSync(join(DESKTOP, "release", "install.sh"), "utf8")));
    expect(theirs.length).toBeGreaterThan(0);
    const keys = at("Contents", "Resources", "release-keys");
    expect(pem(listedKeys(readFileSync(keys, "utf8")))).toEqual(theirs);
    // The app takes its keys only from a regular file of one name, with no group or other write.
    const held = lstatSync(keys);
    expect([held.isFile(), held.isSymbolicLink(), held.nlink, held.mode & 0o777]).toEqual([true, false, 1, 0o644]);
  });

  it("has these programs and libraries and no other, each Apple silicon's alone, and no link that leads out of it", () => {
    const programs = filesOf(top).filter((file) => machO(at(file)) !== null);
    expect(programs).toEqual([
      "Contents/Frameworks/Electron Framework.framework/Versions/A/Electron Framework",
      "Contents/Frameworks/Electron Framework.framework/Versions/A/Helpers/chrome_crashpad_handler",
      "Contents/Frameworks/Electron Framework.framework/Versions/A/Libraries/libffmpeg.dylib",
      "Contents/Frameworks/Electron Framework.framework/Versions/A/Libraries/libvk_swiftshader.dylib",
      "Contents/Frameworks/Mantle.framework/Versions/A/Mantle",
      "Contents/Frameworks/ReactiveObjC.framework/Versions/A/ReactiveObjC",
      "Contents/Frameworks/Squirrel.framework/Versions/A/Resources/ShipIt",
      "Contents/Frameworks/Squirrel.framework/Versions/A/Squirrel",
      ...HELPERS.map((kind) => `Contents/Frameworks/Surogate Helper${kind}.app/Contents/MacOS/Surogate Helper${kind}`).sort(),
      "Contents/MacOS/Surogate", "Contents/MacOS/surogate-helper", "Contents/MacOS/surogate-vm",
      "Contents/Resources/app/bin/node", "Contents/Resources/app/bin/rg", "Contents/Resources/app/bin/surogate-spawn",
      "Contents/Resources/app/dist/native/fsat.node",
    ].sort());
    for (const file of programs) expect(machO(at(file)), file).toBe("arm64");
    const links = entriesOf(top, true);
    expect(links).toEqual(expect.arrayContaining(["Contents/Frameworks/Electron Framework.framework/Versions/Current", "Contents/Frameworks/Electron Framework.framework/Electron Framework"]));
    for (const link of links) expect(relative(realpathSync(top), realpathSync(at(link))).startsWith(".."), link).toBe(false);
  });

  it("is signed ad hoc, inside out: each program and library by itself with exactly its entitlements, and the whole by codesign's strict check", () => {
    const verified = spawnSync("bash", [join(DESKTOP, "scripts", "sign-mac.sh"), "verify", top, "adhoc"], { encoding: "utf8" });
    expect(verified.status, verified.stderr).toBe(0);
    // The four whose entitlements are their own, as codesign reads each, apart from the script's own table.
    const held = (file: string) => spawnSync("codesign", ["-d", "--entitlements", "-", "--xml", at(file)], { encoding: "utf8" }).stdout;
    for (const file of ["Contents/MacOS/Surogate", "Contents/Resources/app/bin/node", ...HELPERS.map((kind) => `Contents/Frameworks/Surogate Helper${kind}.app/Contents/MacOS/Surogate Helper${kind}`)]) {
      expect(held(file), file).toContain("<key>com.apple.security.cs.allow-jit</key>");
      expect(held(file).match(/<key>/g), file).toHaveLength(1);
    }
    expect(held("Contents/MacOS/surogate-vm")).toContain("<key>com.apple.security.virtualization</key>");
    expect(held("Contents/MacOS/surogate-vm").match(/<key>/g)).toHaveLength(1);
    for (const file of ["Contents/MacOS/surogate-helper", "Contents/Resources/app/bin/rg", "Contents/Resources/app/bin/surogate-spawn", "Contents/Resources/app/dist/native/fsat.node"]) {
      expect(held(file), file).not.toContain("<key>");
    }
    // surogate-helper checks an update's bundle by its own signer's Team ID: it is signed as the app is.
    const signer = (file: string) => spawnSync("codesign", ["-dvvv", at(file)], { encoding: "utf8" }).stderr.split("\n").filter((line) => /^(Authority|TeamIdentifier|Signature)=/.test(line)).sort();
    expect(signer("Contents/MacOS/Surogate")).toEqual(["Signature=adhoc", "TeamIdentifier=not set"]);
    expect(signer("Contents/MacOS/surogate-helper")).toEqual(signer("Contents/MacOS/Surogate"));
  });
});
