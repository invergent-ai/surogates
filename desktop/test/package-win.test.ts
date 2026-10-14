// The Windows app as scripts/package-win.sh lays it out, as the MSI installs it. On any OS: how Surogate.exe is given
// its icon and version resource (scripts/win-exe.mjs), on a program made in the test with Electron's form of both, and
// what the script refuses before it lays anything out. On the Windows runner, where SUROGATE_WIN_LAYOUT names a layout
// the script made of SUROGATE_WIN_VERSION, with the agent disk and VM manifest SUROGATE_WIN_AGENT_DISK and
// SUROGATE_WIN_VM_MANIFEST name: the layout itself, every file the app's code looks for where it looks, and every
// program in it one the release signs or ships as it is.

import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { closeSync, existsSync, mkdtempSync, openSync, readdirSync, readFileSync, readSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, relative, sep } from "node:path";
import { fileURLToPath } from "node:url";

import { FuseV1Options, getCurrentFuseWire } from "@electron/fuses";
import { Data, NtExecutable, NtExecutableResource, Resource } from "resedit";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { listedKeys } from "../src/shell/updates.js";
import { endsInFooter, FOOTER_SIZE } from "../src/vm/vhd.js";

const DESKTOP = fileURLToPath(new URL("..", import.meta.url));
const ICON = join(DESKTOP, "windows", "surogate.ico");
const LANGUAGE = { lang: 1033, codepage: 1200 };
let dir: string;

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "package-win-"));
});

afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

// A program with Electron's own icon group and version resource, as Electron's Windows build has them.
function electronLike(path: string): void {
  const exe = NtExecutable.createEmpty(false, false);
  const resources = NtExecutableResource.from(exe);
  const icon = Data.IconFile.from(readFileSync(ICON)).icons.slice(0, 2).map((each) => each.data);
  Resource.IconGroupEntry.replaceIconsForResource(resources.entries, 1, 1033, icon);
  const info = Resource.VersionInfo.createEmpty();
  info.lang = 1033;
  info.setFileVersion(44, 5, 0, 0, 1033);
  info.setProductVersion(44, 5, 0, 0, 1033);
  info.setStringValues(LANGUAGE, {
    CompanyName: "GitHub, Inc.", FileDescription: "Electron", FileVersion: "44.5.0", InternalName: "electron.exe",
    LegalCopyright: "Copyright (C) 2015 GitHub, Inc. All rights reserved.", OriginalFilename: "electron.exe", ProductName: "Electron",
    ProductVersion: "44.5.0", SquirrelAwareVersion: "1",
  });
  info.outputToResourceEntries(resources.entries);
  resources.outputResource(exe);
  writeFileSync(path, Buffer.from(exe.generate()));
}

// What a program's resources say of it: its icon group's icons, as many as there are, and its version resource.
function described(path: string) {
  const resources = NtExecutableResource.from(NtExecutable.from(readFileSync(path), { ignoreCert: true }));
  const [info] = Resource.VersionInfo.fromEntries(resources.entries);
  return {
    icons: Resource.IconGroupEntry.fromEntries(resources.entries).map((group) => [group.id, group.lang, group.icons.length]),
    strings: info?.getStringValues(LANGUAGE),
    fixed: info ? [info.fixedInfo.fileVersionMS, info.fixedInfo.fileVersionLS, info.fixedInfo.productVersionMS, info.fixedInfo.productVersionLS] : [],
  };
}

const exeTool = (...args: string[]) => spawnSync(process.execPath, [join(DESKTOP, "scripts", "win-exe.mjs"), ...args], { encoding: "utf8" });

describe("Surogate.exe's icon and version resource", () => {
  it("are Surogate's, of the release's version, in place of Electron's, its copyright and Squirrel's marker gone", () => {
    const program = join(dir, "Surogate.exe");
    electronLike(program);
    expect(exeTool(program, "2.13.4", ICON)).toMatchObject({ status: 0, stderr: "" });
    expect(described(program)).toEqual({
      icons: [[1, 1033, Data.IconFile.from(readFileSync(ICON)).icons.length]],
      strings: {
        CompanyName: "Surogate", FileDescription: "Surogate", FileVersion: "2.13.4", InternalName: "Surogate", OriginalFilename: "Surogate.exe",
        ProductName: "Surogate", ProductVersion: "2.13.4",
      },
      fixed: [(2 << 16) | 13, 4 << 16, (2 << 16) | 13, 4 << 16],
    });
  });

  it("are given only to a program with Electron's one icon group and one version resource, and only for a version of three parts", () => {
    const program = join(dir, "Surogate.exe");
    writeFileSync(program, Buffer.from(NtExecutable.createEmpty(false, false).generate()));
    expect(exeTool(program, "2.13.4", ICON)).toMatchObject({ status: 1, stderr: `win-exe.mjs: ${program} has not the one icon group and one version resource of Electron's program\n` });
    electronLike(program);
    for (const version of ["2.13", "2.13.04", "2.13.65536", "v2.13.4"]) {
      expect(exeTool(program, version, ICON), version).toMatchObject({ status: 2, stderr: "usage: node scripts/win-exe.mjs <program> <x.y.z> <icon.ico>\n" });
    }
  });
});

describe.skipIf(process.platform === "win32")("scripts/package-win.sh, elsewhere than on Windows", () => {
  const packaged = (...args: string[]) => spawnSync("bash", [join(DESKTOP, "scripts", "package-win.sh"), ...args], { encoding: "utf8" });

  it("lays nothing out but on Windows on x64, whose Electron npm installed, and says so before it fetches anything", () => {
    const [manifest, disk] = [join(dir, "manifest.json"), join(dir, "agent.img")];
    writeFileSync(manifest, JSON.stringify({ arch: "amd64" }));
    writeFileSync(disk, "");
    expect(packaged("1.2.3", manifest, disk, join(dir, "out"))).toMatchObject({ status: 1, stderr: "package-win.sh lays out the Windows app on Windows on x64, whose Electron npm installed\n" });
    expect(existsSync(join(dir, "out"))).toBe(false);
  });

  it("is told a version, a VM manifest, an agent disk and a folder, and nothing more", () => {
    const [manifest, disk] = [join(dir, "manifest.json"), join(dir, "agent.img")];
    writeFileSync(manifest, "{}");
    writeFileSync(disk, "");
    const usage = "usage: scripts/package-win.sh <x.y.z> <vm manifest.json> <agent disk> <out>\n";
    for (const args of [[], ["1.2.3", manifest, disk], ["1.2", manifest, disk, dir], ["1.2.3", join(dir, "none.json"), disk, dir], ["1.2.3", manifest, join(dir, "none.img"), dir], ["1.2.3", manifest, disk, dir, "more"]]) {
      expect(packaged(...args), args.join(" ")).toMatchObject({ status: 2, stderr: usage });
    }
  });
});

// The layout's own folders and files, each by its path from the layout's top, with Windows's separator.
function filesOf(top: string): string[] {
  return (readdirSync(top, { recursive: true, withFileTypes: true }) as Array<{ isFile(): boolean; parentPath: string; name: string }>)
    .filter((entry) => entry.isFile()).map((entry) => relative(top, join(entry.parentPath, entry.name)).split(sep).join("\\")).sort();
}

// Whether *path* is a Windows program or library, by its header: MZ, and PE\0\0 where the header says.
function portable(path: string): boolean {
  const head = Buffer.alloc(4096);
  const fd = openSync(path, "r");
  try {
    const read = readSync(fd, head, 0, head.length, 0);
    if (read < 64 || head.toString("latin1", 0, 2) !== "MZ") return false;
    const at = head.readUInt32LE(0x3c);
    return at + 4 <= read && head.toString("latin1", at, at + 4) === "PE\0\0";
  } finally {
    closeSync(fd);
  }
}

// The DLLs a program imports, by its import table and its delay-load table (Microsoft, "PE Format"), each in lower case.
function importsOf(path: string): string[] {
  const pe = readFileSync(path);
  const at = pe.readUInt32LE(0x3c);
  const optional = at + 24;
  const directories = optional + (pe.readUInt16LE(optional) === 0x20b ? 112 : 96);
  const sections = optional + pe.readUInt16LE(at + 20);
  const offsetOf = (rva: number): number => {
    for (let index = 0; index < pe.readUInt16LE(at + 6); index += 1) {
      const header = sections + index * 40;
      const [size, address, raw] = [Math.max(pe.readUInt32LE(header + 8), pe.readUInt32LE(header + 16)), pe.readUInt32LE(header + 12), pe.readUInt32LE(header + 20)];
      if (rva >= address && rva < address + size) return rva - address + raw;
    }
    throw new Error(`${path}: no section holds ${rva}`);
  };
  const names: string[] = [];
  // The import descriptors, 20 bytes each with the DLL's name at 12, and the delay-load ones, 32 with it at 4.
  for (const [entry, size, field] of [[1, 20, 12], [13, 32, 4]] as const) {
    const rva = pe.readUInt32LE(directories + entry * 8);
    if (rva === 0) continue;
    for (let descriptor = offsetOf(rva); pe.readUInt32LE(descriptor + field) !== 0; descriptor += size) {
      const name = offsetOf(pe.readUInt32LE(descriptor + field));
      names.push(pe.toString("latin1", name, pe.indexOf(0, name)).toLowerCase());
    }
  }
  return names;
}

const LAYOUT = process.env.SUROGATE_WIN_LAYOUT;
const VERSION = process.env.SUROGATE_WIN_VERSION ?? "";

describe.runIf(process.platform === "win32" && LAYOUT)("the Windows app's layout", { timeout: 120_000 }, () => {
  const top = LAYOUT!;
  const at = (...path: string[]) => join(top, ...path);

  it("has Surogate.exe, Electron's program renamed, with Surogate's icon and version resource and the app's fuses, and no default app", async () => {
    expect(existsSync(at("electron.exe"))).toBe(false);
    expect(existsSync(at("resources", "default_app.asar"))).toBe(false);
    expect(described(at("Surogate.exe"))).toMatchObject({
      icons: [[1, 1033, Data.IconFile.from(readFileSync(ICON)).icons.length]],
      strings: { CompanyName: "Surogate", FileDescription: "Surogate", ProductName: "Surogate", OriginalFilename: "Surogate.exe", FileVersion: VERSION, ProductVersion: VERSION },
    });
    const wire = await getCurrentFuseWire(at("Surogate.exe"));
    const on = (fuse: FuseV1Options) => String.fromCharCode(wire[fuse] as number);
    expect([FuseV1Options.RunAsNode, FuseV1Options.EnableNodeOptionsEnvironmentVariable, FuseV1Options.EnableNodeCliInspectArguments, FuseV1Options.EnableCookieEncryption].map(on))
      .toEqual(["0", "0", "0", "1"]);
  });

  it("has every file of Electron's build beside it, but its program and its default app", () => {
    const electron = join(DESKTOP, "node_modules", "electron", "dist");
    const theirs = filesOf(electron).filter((file) => !["electron.exe", "resources\\default_app.asar"].includes(file));
    const ours = new Set(filesOf(top));
    expect(theirs.filter((file) => !ours.has(file))).toEqual([]);
    for (const file of theirs.filter((name) => name.endsWith(".dll"))) expect(readFileSync(at(file)).equals(readFileSync(join(electron, file))), file).toBe(true);
  });

  it("has the app as a folder, of the release's version, with ntat in dist\\native and its own node and rg in bin", () => {
    expect(readdirSync(at("resources", "app")).sort()).toEqual(["assets", "bin", "dictionaries", "dist", "node_modules", "package.json"]);
    const own = JSON.parse(readFileSync(join(DESKTOP, "package.json"), "utf8")) as Record<string, unknown>;
    expect(JSON.parse(readFileSync(at("resources", "app", "package.json"), "utf8"))).toEqual({ ...own, version: VERSION });
    expect(statSync(at("resources", "app", "dist", "native", "ntat.node")).isFile()).toBe(true);
    expect(existsSync(at("resources", "app", "dist", "testing"))).toBe(false);
    expect(filesOf(at("resources", "app", "dist")).filter((file) => file.endsWith(".map"))).toEqual([]);
    expect(readdirSync(at("resources", "app", "bin")).sort()).toEqual(["node.exe", "rg.exe"]);
    const version = /^VERSION=(v[\d.]+)$/m.exec(readFileSync(join(DESKTOP, "scripts", "node.sh"), "utf8"))?.[1];
    expect(spawnSync(at("resources", "app", "bin", "node.exe"), ["-p", "`${process.version} ${process.platform}-${process.arch}`"], { encoding: "utf8" }).stdout).toBe(`${version} win32-x64\n`);
    expect(spawnSync(at("resources", "app", "bin", "rg.exe"), ["--version"], { encoding: "utf8" }).stdout).toMatch(/^ripgrep 15\.2\.0\b/);
    // Production's packages alone, and none of srt's own programs, which the Windows app never runs.
    expect(existsSync(at("resources", "app", "node_modules", "vitest"))).toBe(false);
    expect(existsSync(at("resources", "app", "node_modules", "@anthropic-ai", "sandbox-runtime", "package.json"))).toBe(true);
    expect(existsSync(at("resources", "app", "node_modules", "@anthropic-ai", "sandbox-runtime", "vendor", "srt-win"))).toBe(false);
  });

  it("has the VM's programs in bin and the service in service, each as the workspace built it", () => {
    const built = join(DESKTOP, "native", "windows", "target", "release");
    expect(readdirSync(at("bin")).sort()).toEqual(["surogate-helper.exe", "surogate-vm.exe"]);
    expect(readdirSync(at("service"))).toEqual(["surogate-service.exe"]);
    for (const [folder, program] of [["bin", "surogate-helper.exe"], ["bin", "surogate-vm.exe"], ["service", "surogate-service.exe"]] as const) {
      expect(readFileSync(at(folder, program)).equals(readFileSync(join(built, program))), program).toBe(true);
    }
  });

  it("has the agent disk as a fixed VHD, its raw bytes the disk's, and the guest image's manifest", async () => {
    const given = readFileSync(process.env.SUROGATE_WIN_AGENT_DISK!);
    const disk = at("resources", "vm", "agent.img");
    expect(readdirSync(at("resources", "vm")).sort()).toEqual(["agent.img", "manifest.json"]);
    expect(statSync(disk).size).toBe(given.length + FOOTER_SIZE);
    expect(await endsInFooter(disk, createHash("sha256").update(given).digest("hex"))).toBe(true);
    expect(readFileSync(disk).subarray(0, given.length).equals(given)).toBe(true);
    expect(readFileSync(at("resources", "vm", "manifest.json")).equals(readFileSync(process.env.SUROGATE_WIN_VM_MANIFEST!))).toBe(true);
  });

  it("has install.sh's release keys, key for key, as the app reads them", () => {
    const pem = (keys: ReturnType<typeof listedKeys>) => keys.map((key) => key.export({ type: "spki", format: "pem" }).toString());
    const theirs = pem(listedKeys(readFileSync(join(DESKTOP, "release", "install.sh"), "utf8")));
    expect(theirs.length).toBeGreaterThan(0);
    expect(pem(listedKeys(readFileSync(at("resources", "release-keys"), "utf8")))).toEqual(theirs);
  });

  it("has the app's own programs carry the C runtime in them, as a PC without Visual C++'s redistributable runs them", () => {
    const ours = ["bin\\surogate-helper.exe", "bin\\surogate-vm.exe", "service\\surogate-service.exe", "resources\\app\\dist\\native\\ntat.node"].map((file) => at(file));
    for (const program of ours) {
      const imports = importsOf(program);
      expect(imports.length, program).toBeGreaterThan(0);
      expect(imports.filter((name) => /^(vcruntime|msvcp|ucrtbase|api-ms-win-crt-)/.test(name)), program).toEqual([]);
    }
  });

  it("has no program but those the release signs and node.exe, which ships as nodejs.org signed it", () => {
    const programs = filesOf(top).filter((file) => portable(at(file)));
    expect(programs).toEqual([
      "Surogate.exe", "bin\\surogate-helper.exe", "bin\\surogate-vm.exe", "d3dcompiler_47.dll", "dxcompiler.dll", "dxil.dll", "ffmpeg.dll",
      "resources\\app\\bin\\node.exe", "resources\\app\\bin\\rg.exe", "resources\\app\\dist\\native\\ntat.node", "service\\surogate-service.exe",
      "vk_swiftshader.dll", "vulkan-1.dll",
    ]);
  });
});
