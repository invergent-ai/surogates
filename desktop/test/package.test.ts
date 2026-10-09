// The release's tarball (scripts/package.sh), built from this package's build, and its Electron
// started as the installed app is: renamed, with the app's fuses; and the agent's disk in it
// (vm/agent-disk.sh), made as a release's runner makes it. Behind SUROGATE_PACKAGE_TESTS=1: it needs
// npm run build first, the npm cache npm ci left, xvfb-run, about 1 GB of /tmp, and for the disk
// Docker, the ubuntu:24.04 image and the Ubuntu archive for apt.

import { execFile, spawn, spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

import { FuseState, FuseV1Options, getCurrentFuseWire } from "@electron/fuses";
import { afterAll, afterEach, beforeAll, describe, expect, it } from "vitest";

const DESKTOP = fileURLToPath(new URL("..", import.meta.url));
const VERSION = "1.2.3";
const NAME = `surogate-desktop-${VERSION}-linux-x64`;
// Every file and folder of the agent's disk $1, one a line, as debugfs lists it: its path, its
// mode, its owner and group by number, and a file's size.
const LISTED = String.raw`
listed() {
  debugfs -R "ls -p $2" "$1" 2>/dev/null | while IFS=/ read -r _ inode mode uid gid name size _; do
    case "$name" in "" | . | ..) continue ;; esac
    case "$mode" in
      04*) echo "$2$name/ $mode $uid $gid"; listed "$1" "$2$name/" ;;
      *) echo "$2$name $mode $uid $gid $size" ;;
    esac
  done
}
`;
// What *disk* holds, so listed, in the order of its paths' bytes. debugfs is root's tool, and may
// be on no user's PATH.
const holds = (disk: string) => spawnSync("bash", ["-c", `${LISTED}\nlisted "$1" / | LC_ALL=C sort`, "_", disk], {
  encoding: "utf8", env: { ...process.env, PATH: `${process.env.PATH ?? ""}:/usr/sbin:/sbin` },
}).stdout.trim().split("\n");

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
  // Off the test's event loop: a packaging takes half a minute, and vitest's worker answers its
  // runner on that loop within a minute, or fails the run with every test passed.
  const pack = async (out: string, ...more: string[]) => {
    const dist = join(DESKTOP, "dist");
    const mode = statSync(dist).mode & 0o7777;
    chmodSync(dist, mode | 0o2000);
    try {
      return await new Promise<{ status: number | string | null; stdout: string; stderr: string }>((resolve) => {
        execFile(join(DESKTOP, "scripts", "package.sh"), [VERSION, "vm-manifest.json", out, ...more], {
          cwd: dir, encoding: "utf8", env: { ...process.env, SOURCE_DATE_EPOCH: "1790000000", TMPDIR: join(dir, "tmp") }, maxBuffer: 16 * 1024 * 1024,
        }, (error, stdout, stderr) => resolve({ status: error ? error.code ?? null : 0, stdout, stderr }));
      });
    } finally {
      chmodSync(dist, mode);
    }
  };

  beforeAll(async () => {
    dir = mkdtempSync(join(tmpdir(), "package-test-"));
    writeFileSync(join(dir, "vm-manifest.json"), vmManifest);
    mkdirSync(join(dir, "tmp"));
    chmodSync(join(dir, "tmp"), 0o2775);
    const packed = await pack("out");
    expect(packed.status, packed.stderr).toBe(0);
    expect(packed.stdout.trim().split("\n").at(-1)).toBe(join(dir, "out", `${NAME}.tar.gz`));
    mkdirSync(join(dir, "x"));
    expect(spawnSync("tar", ["-xzf", join(dir, "out", `${NAME}.tar.gz`), "-C", join(dir, "x")]).status).toBe(0);
    top = join(dir, "x", NAME);
  }, 180_000);

  // A turn of the event loop between tests, for the calls that do hold it.
  afterEach(() => new Promise((resolve) => setTimeout(resolve, 0)));

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
    // Its folders and files are root's, with the one mode of their kind, whatever the build's folders hand on.
    const disk = holds(join(top, "resources", "vm", "agent.img"));
    expect(disk.length).toBeGreaterThan(20);
    expect(disk.filter((line) => !/^(\/lost\+found\/ 040700|\S+\/ 040755) 0 0$|^\S*[^/ ] 100(644|755) 0 0 \d+$/.test(line))).toEqual([]);
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

  it("packs the install script it is told as the root helper, a program whatever its file's mode: a test's release trusts a key of the test's own", async () => {
    // The repository's script with a line of the test's, kept as a file that is no program.
    const script = `${readFileSync(join(DESKTOP, "release", "install.sh"), "utf8")}# a test's\n`;
    writeFileSync(join(dir, "install.sh"), script, { mode: 0o644 });
    const packed = await pack("told", "install.sh");
    expect(packed.status, packed.stderr).toBe(0);
    const tarball = join(dir, "told", `${NAME}.tar.gz`);
    expect(spawnSync("tar", ["-xzOf", tarball, `${NAME}/bin/surogate-apply-update`], { encoding: "utf8", maxBuffer: 16 * 1024 * 1024 }).stdout).toBe(script);
    expect(spawnSync("tar", ["-tvzf", tarball, `${NAME}/bin/surogate-apply-update`], { encoding: "utf8" }).stdout).toMatch(/^-rwxr-xr-x 0\/0 /);
    // What is no file, or anything after it, is its usage.
    for (const more of [["nowhere.sh"], ["install.sh", "more"]]) {
      expect(await pack("told", ...more), more.join(" ")).toMatchObject({ status: 2, stdout: "", stderr: "usage: scripts/package.sh <x.y.z> <vm manifest.json> <out> [<install script>]\n" });
    }
  });

  it("packs no version but x.y.z in the ten digits with no zero before a part, as the install script and the signing take one: refused before anything is packed, whatever its caller's locale takes for a digit", async () => {
    // Off the test's event loop too: where one is taken, a packaging follows.
    const asked = (version: string, locale: string) => new Promise<{ status: number | string | null; stdout: string; stderr: string }>((resolve) => {
      execFile(join(DESKTOP, "scripts", "package.sh"), [version, "vm-manifest.json", "refused"], {
        cwd: dir, encoding: "utf8", env: { ...process.env, SOURCE_DATE_EPOCH: "1790000000", TMPDIR: join(dir, "tmp"), LC_ALL: locale }, maxBuffer: 16 * 1024 * 1024,
      }, (error, stdout, stderr) => resolve({ status: error ? error.code ?? null : 0, stdout, stderr }));
    });
    const usage = { status: 2, stdout: "", stderr: "usage: scripts/package.sh <x.y.z> <vm manifest.json> <out> [<install script>]\n" };
    // dpkg reads 1.2.03 as 1.2.3: a version has one spelling, and a tag of another would build
    // for the job's whole length and be refused at its signing.
    for (const version of ["1.2.03", "01.2.3", "1.02.3", "00.0.0", "1.2", "1.2.3.4", "v1.2.3", "1.2.3-rc.1", "1.2.3\n"]) {
      expect(await asked(version, "C"), JSON.stringify(version)).toMatchObject(usage);
    }
    // A locale of this computer's in which bash takes other characters than the ten for digits:
    // most do. Where a computer has none, there is nothing to show.
    const locales = spawnSync("locale", ["-a"], { encoding: "utf8" }).stdout.trim().split("\n");
    const wide = locales.find((locale) => spawnSync("bash", ["-c", '[[ "$1" =~ ^[0-9]$ ]]', "_", "\u0663"], { env: { ...process.env, LC_ALL: locale } }).status === 0);
    if (wide) {
      for (const version of ["1.2.\u0663", "\uff11.2.3", "1.\u00b2.3"]) expect(await asked(version, wide), `${version} in ${wide}`).toMatchObject(usage);
    }
    expect(existsSync(join(dir, "refused"))).toBe(false);
  });

  it("is the same bytes when the same build is packed again: a release sent again puts no other tarball under a name already served", async () => {
    const again = await pack("again");
    expect(again.status, again.stderr).toBe(0);
    const hash = (out: string) => createHash("sha256").update(readFileSync(join(dir, out, `${NAME}.tar.gz`))).digest("hex");
    expect(hash("again")).toBe(hash("out"));
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

describe.skipIf(process.env.SUROGATE_PACKAGE_TESTS !== "1")("the agent's disk", { timeout: 120_000 }, () => {
  // A release's runner: Ubuntu 24.04, whose mke2fs is e2fsprogs 1.47.0, with the one package the
  // job installs for the disk, and a user who is not root. A container is given no right to make
  // a user namespace, as a stock Ubuntu gives none to a program without a profile of its own.
  const RUNNER = ["RUN apt-get update && apt-get install -y --no-install-recommends fakeroot && rm -rf /var/lib/apt/lists/*"];
  const image = `surogate-agent-disk-test:24.04-${createHash("sha256").update(RUNNER.join("\n")).digest("hex").slice(0, 12)}`;
  let dir: string;
  // No credentials of this user's reach the image's pull or the container.
  const docker = (...args: string[]) => spawnSync("docker", args, { encoding: "utf8", env: { ...process.env, DOCKER_CONFIG: join(dir, "docker") }, timeout: 100_000 });
  // *script* in a container of *from*, as a user who is not root, with this package's folder read-only at /desktop.
  const runner = (from: string, script: string) => docker("run", "--rm", "--user", "1000:1000", "-v", `${DESKTOP}:/desktop:ro`, from, "bash", "-c", script);
  // What the disk holds, from this package's build: the agent and the modules it imports, the
  // guest's init and what enters a session's root, each a file all may read, root's.
  const expected = () => {
    const files = (folder: string, base = folder): string[] => readdirSync(folder, { withFileTypes: true }).flatMap((entry) => entry.isDirectory()
      ? [`/${relative(base, join(folder, entry.name))}/ 040755 0 0`, ...files(join(folder, entry.name), base)]
      : entry.name.endsWith(".map") ? [] : [`/${relative(base, join(folder, entry.name))} 100644 0 0 ${statSync(join(folder, entry.name)).size}`]);
    const dist = join(DESKTOP, "dist");
    return [
      "/lost+found/ 040700 0 0",
      "/guest/ 040755 0 0", ...files(join(dist, "guest")).map((line) => `/guest${line}`),
      "/files/ 040755 0 0", ...files(join(dist, "files")).map((line) => `/files${line}`),
      "/link/ 040755 0 0", `/link/protocol.js 100644 0 0 ${statSync(join(dist, "link", "protocol.js")).size}`,
      `/init 100755 0 0 ${statSync(join(DESKTOP, "vm", "init")).size}`, `/enter-root 100755 0 0 ${statSync(join(DESKTOP, "vm", "enter-root")).size}`,
      "/package.json 100644 0 0 18",
    ].sort();
  };

  beforeAll(async () => {
    dir = mkdtempSync(join(tmpdir(), "agent-disk-test-"));
    mkdirSync(join(dir, "docker"));
    mkdirSync(join(dir, "image"));
    writeFileSync(join(dir, "image", "Dockerfile"), ["FROM ubuntu:24.04", ...RUNNER].join("\n"));
    // Off the event loop: a first build takes a minute, and vitest's RPC answers must still get in.
    await promisify(execFile)("docker", ["build", "-q", "-t", image, join(dir, "image")], { env: { ...process.env, DOCKER_CONFIG: join(dir, "docker") } });
  }, 600_000);

  afterEach(() => new Promise((resolve) => setTimeout(resolve, 0)));

  afterAll(() => {
    rmSync(dir, { recursive: true, force: true });
  });

  it("is made where no user namespace can be, by a user who is not root, with every file of it root's", () => {
    const made = runner(image, `${LISTED}
      echo "user $(id -u), mke2fs $(mke2fs -V 2>&1 | head -n 1 | cut -d' ' -f2)"
      echo "a user namespace: $(unshare -r true 2>&1)"
      /desktop/vm/agent-disk.sh /tmp/agent.img >/dev/null || exit
      listed /tmp/agent.img / | LC_ALL=C sort
      e2fsck -fn /tmp/agent.img >/dev/null 2>&1; echo "checked $?"`);
    expect(made.status, made.stderr).toBe(0);
    const [who, namespace, ...lines] = made.stdout.trim().split("\n");
    expect(who).toBe("user 1000, mke2fs 1.47.0");
    // Refused there, as on a stock Ubuntu 24.04: what the disk was made in before.
    expect(namespace).toBe("a user namespace: unshare: unshare failed: Operation not permitted");
    expect(lines.pop()).toBe("checked 0");
    expect(lines).toEqual(expected());
    // And the same disk here, with this computer's own tools.
    const here = join(dir, "agent.img");
    expect(spawnSync(join(DESKTOP, "vm", "agent-disk.sh"), [here], { encoding: "utf8" })).toMatchObject({ status: 0, stderr: "" });
    expect(holds(here)).toEqual(expected());
  });

  it("is the same bytes from the same build, whenever it is made, at one SOURCE_DATE_EPOCH, which is every time in it", () => {
    // Twice, seconds apart, each from a copy of the build made then: on the runner's Ubuntu, and here.
    const twice = (disk: string) => `for made in 1 2; do SOURCE_DATE_EPOCH=1790000000 ${disk} /tmp/agent-$made.img >/dev/null || exit; sleep 1.5; done
      sha256sum </tmp/agent-1.img; sha256sum </tmp/agent-2.img
      debugfs -R "stat /guest/agent.js" /tmp/agent-2.img 2>/dev/null | grep -E "^ *(c|a|m|cr)time:" | cut -d- -f1 | tr -s " \n" " "; echo
      dumpe2fs -h /tmp/agent-2.img 2>/dev/null | grep -E "^Filesystem (UUID|created):" | tr -s " \n" " "`;
    const there = runner(image, twice("/desktop/vm/agent-disk.sh"));
    expect(there.status, there.stderr).toBe(0);
    const [first, second, times, superblock] = there.stdout.trim().split("\n").map((line) => line.trim());
    expect(second).toBe(first);
    // 0x6ab13b80 is 1790000000: when the file was made, changed and read, and when the disk was.
    expect(times).toBe("ctime: 0x6ab13b80:00000000 atime: 0x6ab13b80:00000000 mtime: 0x6ab13b80:00000000 crtime: 0x6ab13b80:00000000");
    expect(superblock).toMatch(/^Filesystem UUID: [0-9a-f-]{36} Filesystem created: Mon Sep 21 14:13:20 2026$/);
    const here = [1, 2].map((made) => {
      const disk = join(dir, `agent-${made}.img`);
      expect(spawnSync(join(DESKTOP, "vm", "agent-disk.sh"), [disk], { encoding: "utf8", env: { ...process.env, SOURCE_DATE_EPOCH: "1790000000" } })).toMatchObject({ status: 0, stderr: "" });
      spawnSync("sleep", ["1.5"]);
      return createHash("sha256").update(readFileSync(disk)).digest("hex");
    });
    expect(here[1]).toBe(here[0]);
    // With no SOURCE_DATE_EPOCH, its times are the moment it is made.
    const now = join(dir, "agent-now.img");
    const { SOURCE_DATE_EPOCH: _, ...env } = process.env;
    expect(spawnSync(join(DESKTOP, "vm", "agent-disk.sh"), [now], { encoding: "utf8", env }).status).toBe(0);
    expect(createHash("sha256").update(readFileSync(now)).digest("hex")).not.toBe(here[0]);
  });

  it("is not made without fakeroot, and nor is a tarball: each script says so before it does anything", () => {
    // Ubuntu's own image, which has mke2fs and no fakeroot, and nothing else a packaging needs.
    const without = runner("ubuntu:24.04", [
      "/desktop/vm/agent-disk.sh /tmp/agent.img; echo \"agent-disk.sh $?\"",
      "/desktop/scripts/package.sh 1.2.3 /desktop/package.json /tmp/out; echo \"package.sh $?\"",
      "ls -A /tmp",
    ].join("\n"));
    expect(without.stdout).toBe("agent-disk.sh 1\npackage.sh 1\n");
    expect(without.stderr).toBe([
      "agent-disk.sh: fakeroot is missing, which makes the disk's files root's with no root and no user namespace: install it (apt install fakeroot)",
      "package.sh: fakeroot is missing, which the agent's disk is made with (vm/agent-disk.sh): install it (apt install fakeroot)",
      "",
    ].join("\n"));
  });
});
