// The scripts that fetch the app's own node and rg for the computer the app is built on (scripts/node.sh, scripts/rg.sh),
// each run in a package of its own with every program it starts but the shell's own a fake that writes down how it was
// started: uname answers as the computer the test names, and nothing is downloaded. So each computer's commands are
// read one by one, Linux's as they were before Windows had any, and a computer neither script builds for is refused
// before anything is fetched.

import { spawnSync } from "node:child_process";
import { chmodSync, copyFileSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

const PACKAGE = fileURLToPath(new URL("..", import.meta.url));
const FAKED = ["curl", "sha256sum", "tar", "strip", "unzip", "mv"];
const LINUX = ["Linux", "x86_64"] as const;
const WINDOWS = ["MINGW64_NT-10.0-26100", "x86_64"] as const;
let dir: string;

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "fetched-pins-"));
});

afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

/**
 * scripts/*script* run in a package of the test's own on a computer that uname calls *system* and *machine*, with
 * *failing* the fake that exits 1: its status, what it said, and each faked command it started, one a line, its
 * standard input after a "<" where it read one, and its temporary folder written $tmp. *args* are the script's own.
 */
function run(script: string, [system, machine]: readonly [string, string], failing?: string, args: string[] = []): { status: number | null; stderr: string; ran: string[] } {
  const at = mkdtempSync(join(dir, "run-"));
  const fakes = join(at, "fakes");
  const log = join(at, "ran");
  mkdirSync(join(at, "package", "scripts"), { recursive: true });
  mkdirSync(fakes);
  copyFileSync(join(PACKAGE, "scripts", script), join(at, "package", "scripts", script));
  writeFileSync(join(fakes, "uname"), `#!/bin/sh\ncase "$1" in -s) echo '${system}' ;; -m) echo '${machine}' ;; esac\n`);
  for (const name of FAKED) {
    const said = name === "sha256sum" ? `read -r input; echo "${name} $* < $input"` : `echo "${name} $*"`;
    writeFileSync(join(fakes, name), `#!/bin/bash\n{ ${said}; } >>'${log}'\nexit ${name === failing ? 1 : 0}\n`);
  }
  for (const name of ["uname", ...FAKED]) chmodSync(join(fakes, name), 0o755);
  const temp = join(at, "temp");
  mkdirSync(temp);
  const done = spawnSync("bash", [join(at, "package", "scripts", script), ...args], {
    encoding: "utf8", env: { PATH: `${fakes}:/usr/bin:/bin`, HOME: at, TMPDIR: temp },
  });
  const ran = existsSync(log) ? readFileSync(log, "utf8").trim().split("\n").map((line) => line.replace(new RegExp(`${temp}/tmp\\.[A-Za-z0-9]+`, "g"), "$tmp")) : [];
  return { status: done.status, stderr: done.stderr, ran };
}

describe("scripts/node.sh", () => {
  it("runs on Linux on x64 the commands it ran before it fetched for Windows", () => {
    expect(run("node.sh", LINUX)).toEqual({
      status: 0, stderr: "", ran: [
        "curl -q -fsSL --proto =https -o $tmp/node.tar.gz https://nodejs.org/dist/v22.23.3/node-v22.23.3-linux-x64.tar.gz",
        "sha256sum --check --quiet < 1084aa36196bba4c3a5e69a1ee388a6e4ff729dad09445fbcd434b28fe3c24af  $tmp/node.tar.gz",
        "tar -xzf $tmp/node.tar.gz -C $tmp --strip-components=2 node-v22.23.3-linux-x64/bin/node",
        "strip $tmp/node",
        "mv $tmp/node bin/node",
      ],
    });
  });

  it("fetches for win32-x64, on Windows on x64, the win-x64 node.exe by its pin, and leaves it as nodejs.org signed it", () => {
    expect(run("node.sh", WINDOWS, undefined, ["win32-x64"])).toEqual({
      status: 0, stderr: "", ran: [
        "curl -q -fsSL --proto =https -o $tmp/node.zip https://nodejs.org/dist/v22.23.3/node-v22.23.3-win-x64.zip",
        "sha256sum --check --quiet < 2b0ff57b049cda1bbcea2240eec20467018713c1efe1f7360c2681859b90ed71  $tmp/node.zip",
        "unzip -q $tmp/node.zip node-v22.23.3-win-x64/node.exe -d $tmp",
        "mv $tmp/node-v22.23.3-win-x64/node.exe bin/node.exe",
      ],
    });
    expect(run("node.sh", ["MSYS_NT-10.0-26100", "x86_64"], undefined, ["win32-x64"]).ran[0]).toMatch(/node-v22\.23\.3-win-x64\.zip$/);
  });

  it("puts no node in place whose archive is not the pinned one", () => {
    for (const [computer, args] of [[LINUX, []], [WINDOWS, ["win32-x64"]]] as const) {
      const { status, ran } = run("node.sh", computer, "sha256sum", [...args]);
      expect(status, computer[0]).not.toBe(0);
      expect(ran.map((line) => line.split(" ")[0]), computer[0]).toEqual(["curl", "sha256sum"]);
    }
  });

  it("is the Linux app's build's first step alone on Windows, and refuses it there before it fetches anything", () => {
    for (const computer of [WINDOWS, ["MSYS_NT-10.0-26100", "x86_64"], ["CYGWIN_NT-10.0-26100", "x86_64"]] as const) {
      expect(run("node.sh", computer), computer.join(" ")).toEqual({
        status: 1, stderr: "npm run build is the Linux app's build, with Linux's node: on Windows, build the app with npm run build:win32\n", ran: [],
      });
    }
  });

  it("refuses every other computer, and Windows's node anywhere but on Windows on x64, before it fetches anything", () => {
    const refused = "node.sh fetches the Linux app's node on Linux on x64, and with win32-x64 the Windows app's on Windows on x64\n";
    for (const computer of [["Linux", "aarch64"], ["Darwin", "arm64"], ["Darwin", "x86_64"]] as const) {
      expect(run("node.sh", computer), computer.join(" ")).toEqual({ status: 1, stderr: refused, ran: [] });
    }
    for (const computer of [LINUX, ["MINGW64_NT-10.0-26100", "aarch64"], ["Darwin", "arm64"]] as const) {
      expect(run("node.sh", computer, undefined, ["win32-x64"]), computer.join(" ")).toEqual({ status: 1, stderr: refused, ran: [] });
    }
    expect(run("node.sh", LINUX, undefined, ["linux-x64"])).toEqual({ status: 1, stderr: refused, ran: [] });
  });

  it("is of the version whose headers and import library ntat is built against", () => {
    const version = /^VERSION=v([\d.]+)$/m.exec(readFileSync(join(PACKAGE, "scripts", "node.sh"), "utf8"))?.[1];
    expect(readFileSync(join(PACKAGE, "native", "ntat", "build.ps1"), "utf8")).toMatch(new RegExp(`^\\$Version = '${version?.replaceAll(".", "\\.")}'$`, "m"));
  });
});

describe("scripts/rg.sh", () => {
  it("fetches on Windows on x64 ripgrep's own x86_64-pc-windows-msvc release by its pin", () => {
    expect(run("rg.sh", WINDOWS)).toEqual({
      status: 0, stderr: "", ran: [
        "curl -q -fsSL --proto =https -o $tmp/rg.zip https://github.com/BurntSushi/ripgrep/releases/download/15.2.0/ripgrep-15.2.0-x86_64-pc-windows-msvc.zip",
        "sha256sum --check --status < 71b2fef860abe467217a538ff31de02f5258807c0129f771846f87bd029aafc5  $tmp/rg.zip",
        "unzip -q $tmp/rg.zip ripgrep-15.2.0-x86_64-pc-windows-msvc/rg.exe -d $tmp",
        "mv $tmp/ripgrep-15.2.0-x86_64-pc-windows-msvc/rg.exe bin/rg.exe",
      ],
    });
  });

  it("puts no rg.exe in place whose archive is not the pinned one", () => {
    const { status, stderr, ran } = run("rg.sh", WINDOWS, "sha256sum");
    expect([status, stderr]).toEqual([1, "rg.sh: the archive is not the pinned one\n"]);
    expect(ran.map((line) => line.split(" ")[0])).toEqual(["curl", "sha256sum"]);
  });

  it("refuses every computer it does not build for before it fetches anything, Linux's among them", () => {
    for (const computer of [LINUX, ["Linux", "aarch64"], ["Darwin", "x86_64"], ["MINGW64_NT-10.0-26100", "aarch64"]] as const) {
      expect(run("rg.sh", computer), computer.join(" ")).toEqual({ status: 1, stderr: "rg.sh fetches the app's rg for macOS on Apple silicon and Windows on x64\n", ran: [] });
    }
  });
});
