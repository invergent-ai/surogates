// The release's tarball (scripts/package.sh), built from this package's build, and its Electron
// started as the installed app is: renamed, with the app's fuses. Behind SUROGATE_PACKAGE_TESTS=1:
// it needs npm run build first, the npm cache npm ci left, xvfb-run, and about 1 GB of /tmp.

import { spawn, spawnSync } from "node:child_process";
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { FuseState, FuseV1Options, getCurrentFuseWire } from "@electron/fuses";
import { afterAll, beforeAll, describe, expect, it } from "vitest";

const DESKTOP = fileURLToPath(new URL("..", import.meta.url));
const VERSION = "1.2.3";
const NAME = `surogate-desktop-${VERSION}-linux-x64`;

describe.skipIf(process.env.SUROGATE_PACKAGE_TESTS !== "1")("the release's tarball", { timeout: 180_000 }, () => {
  let dir: string;
  let top: string;
  const vmManifest = `${JSON.stringify({ key: "a".repeat(64), files: [] })}\n`;

  // The packaged app, started in a session of the test's own: a scratch home and XDG folders, no
  // session bus, X11 on an Xvfb of its own. Settles with its exit code and what it wrote, or with
  // null once it has run *aliveMs* without exiting; either way only once all it started is gone.
  const started = (args: string[], aliveMs = 8_000) => new Promise<{ code: number | null; output: string }>((resolve) => {
    const home = mkdtempSync(join(dir, "home-"));
    const run = mkdtempSync(join(tmpdir(), "rt-"));
    const env: Record<string, string> = {
      PATH: "/usr/bin:/bin", HOME: home, XDG_CONFIG_HOME: join(home, "c"), XDG_DATA_HOME: join(home, "d"), XDG_CACHE_HOME: join(home, "k"),
      XDG_STATE_HOME: join(home, "s"), XDG_RUNTIME_DIR: run, DBUS_SESSION_BUS_ADDRESS: "disabled:", XDG_SESSION_TYPE: "x11", GDK_BACKEND: "x11",
      // As VS Code's terminals export it: the app still starts as the app.
      ELECTRON_RUN_AS_NODE: "1",
    };
    // fds 3 and 4 open, as a pipe's debugger would hand them; a process group of its own, to end whole.
    const app = spawn("xvfb-run", ["-a", join(top, "surogate"), "--password-store=basic", ...args], { env, detached: true, stdio: ["ignore", "pipe", "pipe", "pipe", "pipe"] });
    let output = "";
    let timedOut = false;
    app.stdout!.on("data", (chunk) => (output += chunk));
    app.stderr!.on("data", (chunk) => (output += chunk));
    const timer = setTimeout(() => {
      timedOut = true;
      process.kill(-app.pid!, "SIGTERM");
    }, aliveMs);
    app.on("exit", async (code) => {
      clearTimeout(timer);
      // Its Xvfb and Electron's helpers can outlive xvfb-run by a moment, writing into the home.
      for (const end = Date.now() + 10_000; Date.now() < end; await new Promise((wait) => setTimeout(wait, 100))) {
        try {
          process.kill(-app.pid!, 0);
        } catch {
          break;
        }
      }
      rmSync(run, { recursive: true, force: true });
      resolve({ code: timedOut ? null : code, output });
    });
  });

  // The tarball packed into *out*, as the release's job calls package.sh: from a folder that is not
  // this package's, its paths as that folder names them; *more* is what a test adds to the job's
  // three arguments. Built as in a checkout under a folder that hands its group on: every folder
  // made there has the set-gid bit, here the build's own dist, and so has the folder for temporary files.
  const pack = (out: string, ...more: string[]) => {
    const dist = join(DESKTOP, "dist");
    const mode = statSync(dist).mode & 0o7777;
    chmodSync(dist, mode | 0o2000);
    try {
      return spawnSync(join(DESKTOP, "scripts", "package.sh"), [VERSION, "vm-manifest.json", out, ...more], {
        cwd: dir, encoding: "utf8", env: { ...process.env, SOURCE_DATE_EPOCH: "1790000000", TMPDIR: join(dir, "tmp") }, maxBuffer: 16 * 1024 * 1024,
      });
    } finally {
      chmodSync(dist, mode);
    }
  };

  beforeAll(() => {
    dir = mkdtempSync(join(tmpdir(), "package-test-"));
    writeFileSync(join(dir, "vm-manifest.json"), vmManifest);
    mkdirSync(join(dir, "tmp"));
    chmodSync(join(dir, "tmp"), 0o2775);
    const packed = pack("out");
    expect(packed.status, packed.stderr).toBe(0);
    expect(packed.stdout.trim().split("\n").at(-1)).toBe(join(dir, "out", `${NAME}.tar.gz`));
    mkdirSync(join(dir, "x"));
    expect(spawnSync("tar", ["-xzf", join(dir, "out", `${NAME}.tar.gz`), "-C", join(dir, "x")]).status).toBe(0);
    top = join(dir, "x", NAME);
  }, 180_000);

  afterAll(() => {
    rmSync(dir, { recursive: true, force: true });
  });

  it("lays the app out as one folder beside Electron, renamed, with the release's version and nothing of the tests'", () => {
    expect(readdirSync(join(dir, "x"))).toEqual([NAME]);
    expect(statSync(join(top, "surogate")).mode & 0o777).toBe(0o755);
    expect(existsSync(join(top, "electron")) || existsSync(join(top, "resources", "default_app.asar"))).toBe(false);
    expect(readdirSync(join(top, "resources")).sort()).toEqual(["app", "surogate.svg", "vm"]);
    const app = join(top, "resources", "app");
    expect(readdirSync(app).sort()).toEqual(["assets", "bin", "dist", "node_modules", "package.json"]);
    expect((JSON.parse(readFileSync(join(app, "package.json"), "utf8")) as { version: string }).version).toBe(VERSION);
    expect(spawnSync(join(app, "bin", "node"), ["--version"], { encoding: "utf8" }).stdout).toBe("v22.23.3\n");
    // The production dependencies alone, and no test of the app's, its echo client among them.
    expect(readdirSync(join(app, "node_modules"))).not.toContain("electron");
    expect(readdirSync(join(app, "node_modules"))).not.toContain("vitest");
    expect(existsSync(join(app, "node_modules", "@anthropic-ai", "sandbox-runtime"))).toBe(true);
    expect(existsSync(join(app, "dist", "testing")) || existsSync(join(app, "dist", "agent.img"))).toBe(false);
    expect(spawnSync("find", [join(app, "dist"), "-name", "*.map"], { encoding: "utf8" }).stdout).toBe("");
    // The VM's files, and the install script as the version's root helper.
    expect(readFileSync(join(top, "resources", "vm", "manifest.json"), "utf8")).toBe(vmManifest);
    expect(spawnSync("file", ["-b", join(top, "resources", "vm", "agent.img")], { encoding: "utf8" }).stdout).toContain('volume name "surogate-agent"');
    expect(readFileSync(join(top, "bin", "surogate-apply-update"), "utf8")).toBe(readFileSync(join(DESKTOP, "release", "install.sh"), "utf8"));
    expect(statSync(join(top, "bin", "surogate-apply-update")).mode & 0o777).toBe(0o755);
    // Root installs it: nothing in it is writable by anyone but its owner.
    expect(spawnSync("find", [top, "!", "-type", "l", "-perm", "/022"], { encoding: "utf8" }).stdout).toBe("");
    const listing = spawnSync("tar", ["-tvzf", join(dir, "out", `${NAME}.tar.gz`)], { encoding: "utf8", maxBuffer: 64 * 1024 * 1024 }).stdout;
    expect(listing.split("\n").filter((line) => line && !line.includes(" 0/0 "))).toEqual([]);
    // What the root helper refuses is not in it, whatever the build's folders hand on: no set-id
    // bit, and no member that is not a file, a folder or a link, each with the one mode of its kind.
    expect(listing.split("\n").filter((line) => /^(.{3}|.{6}|.{9})[sStT]/.test(line))).toEqual([]);
    expect(listing.split("\n").filter((line) => line && !/^(-rw-r--r--|-rwxr-xr-x|drwxr-xr-x|lrwxrwxrwx) /.test(line))).toEqual([]);
  });

  it("packs the install script it is told as the root helper, a program whatever its file's mode: a test's release trusts a key of the test's own", () => {
    // The repository's script with a line of the test's, kept as a file that is no program.
    const script = `${readFileSync(join(DESKTOP, "release", "install.sh"), "utf8")}# a test's\n`;
    writeFileSync(join(dir, "install.sh"), script, { mode: 0o644 });
    const packed = pack("told", "install.sh");
    expect(packed.status, packed.stderr).toBe(0);
    const tarball = join(dir, "told", `${NAME}.tar.gz`);
    expect(spawnSync("tar", ["-xzOf", tarball, `${NAME}/bin/surogate-apply-update`], { encoding: "utf8", maxBuffer: 16 * 1024 * 1024 }).stdout).toBe(script);
    expect(spawnSync("tar", ["-tvzf", tarball, `${NAME}/bin/surogate-apply-update`], { encoding: "utf8" }).stdout).toMatch(/^-rwxr-xr-x 0\/0 /);
    // What is no file, or anything after it, is its usage.
    for (const more of [["nowhere.sh"], ["install.sh", "more"]]) {
      expect(pack("told", ...more), more.join(" ")).toMatchObject({ status: 2, stdout: "", stderr: "usage: scripts/package.sh <x.y.z> <vm manifest.json> <out> [<install script>]\n" });
    }
  });

  it("gives Electron the app's fuses: never Node, no NODE_OPTIONS, no inspector, and its cookies encrypted", async () => {
    const wire = await getCurrentFuseWire(join(top, "surogate"));
    expect([
      wire[FuseV1Options.RunAsNode], wire[FuseV1Options.EnableNodeOptionsEnvironmentVariable],
      wire[FuseV1Options.EnableNodeCliInspectArguments], wire[FuseV1Options.EnableCookieEncryption],
    ]).toEqual([FuseState.DISABLE, FuseState.DISABLE, FuseState.DISABLE, FuseState.ENABLE]);
  });

  it("refuses to start with remote debugging, which no fuse covers, and starts as the app without it", async () => {
    const refusal = "Surogate does not start with remote debugging (--remote-debugging-port or --remote-debugging-pipe).";
    for (const args of [["--remote-debugging-port=0"], ["--remote-debugging-port", "9229"], ["--remote-debugging-pipe"]]) {
      const { code, output } = await started(args);
      expect(code, output).toBe(1);
      expect(output).toContain(refusal);
      expect(output).not.toContain("DevTools listening");
    }
    const { code, output } = await started([]);
    expect(code, output).toBeNull();
    expect(output).not.toContain(refusal);
  });
});
