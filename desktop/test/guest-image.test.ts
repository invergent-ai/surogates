// The guest image's build (images/sandbox/Dockerfile and images/guest/): its kernel pin, its key's
// inputs, the modules it keeps, and the release's check of its kernel.

import { execFile, spawnSync } from "node:child_process";
import { randomBytes } from "node:crypto";
import { appendFileSync, copyFileSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

const DOCKERFILE = readFileSync(new URL("../../images/sandbox/Dockerfile", import.meta.url), "utf8");
// Each stage's text, by its name.
const stagesOf = (dockerfile: string) =>
  new Map([...dockerfile.matchAll(/^FROM \S+ AS (\S+)\n([\s\S]*?)(?=^FROM |(?![\s\S]))/gm)].map(([, name, body]) => [name ?? "", body ?? ""]));
const STAGES = stagesOf(DOCKERFILE);
// The architectures the guest is built for, as Docker and Debian name them: each has a kernel stage of its own.
const ARCHES = ["amd64", "arm64"] as const;
const LAUNCHPAD = "https://launchpad.net/ubuntu/+archive/primary/+files/";
// An ADD the build checks by its hash: its URL, on the line after the checksum.
const PINNED = /^ADD --checksum=sha256:[0-9a-f]{64} \\\n\s+(\S+) \S+$/gm;

// What is wrong with *dockerfile*'s kernel pin, each in words: every arch's kernel stage fetches the four packages of
// KERNEL from Launchpad, each by its hash, all of one version, the same for every arch, and fetches nothing else.
function pinProblems(dockerfile: string): string[] {
  const kernel = /^ARG KERNEL=(\S+)$/m.exec(dockerfile)?.[1] ?? "";
  const abi = kernel.replace(/-generic$/, "");
  // The hardware-enablement series the tools package is named for: 7.0 for 7.0.0-38.
  const series = kernel.split(".").slice(0, 2).join(".");
  const wanted = [`linux-hwe-${series}-tools-${abi}`, `linux-image-${kernel}`, `linux-modules-${kernel}`, `linux-tools-${kernel}`];
  const stages = stagesOf(dockerfile);
  const problems: string[] = [];
  const versions = new Set<string>();
  for (const arch of ARCHES) {
    const body = stages.get(`kernel-${arch}`) ?? "";
    const pinned = [...body.matchAll(PINNED)].map(([, url]) => url ?? "");
    for (const url of [...body.matchAll(/https?:\/\/\S+/g)].map(([url]) => url)) {
      if (!pinned.includes(url)) problems.push(`kernel-${arch} fetches ${url} without its hash`);
    }
    const debs = pinned.map((url) => /^(\S+)_([^_]+)_([^_]+)\.deb$/.exec(url.startsWith(LAUNCHPAD) ? url.slice(LAUNCHPAD.length) : ""));
    if (debs.some((deb) => deb === null)) problems.push(`kernel-${arch} fetches a file that is not a .deb of Launchpad's`);
    const names = debs.map((deb) => deb?.[1] ?? "").sort();
    if (names.join() !== wanted.join()) problems.push(`kernel-${arch} fetches ${names.join(", ")}, not ${wanted.join(", ")}`);
    for (const deb of debs) {
      if (deb && deb[3] !== arch) problems.push(`kernel-${arch} fetches ${deb[1]} for ${deb[3]}`);
      if (deb) versions.add(deb[2] ?? "");
    }
  }
  if (versions.size > 1) problems.push(`the kernel's packages are of ${versions.size} versions: ${[...versions].sort().join(", ")}`);
  // apt would take whatever version it finds now: no stage installs a kernel package from it.
  for (const [name, body] of stages) if (/\bapt(-get)? install[^&]*\blinux-/.test(body)) problems.push(`${name} installs a kernel package from apt`);
  // Nor is a package fetched any other way: each one is an ADD of a kernel stage the build checks by its hash.
  const unpinned = ARCHES.reduce((text, arch) => text.replace(stages.get(`kernel-${arch}`) ?? "\0", ""), dockerfile).replace(PINNED, "");
  for (const [url] of unpinned.matchAll(/https?:\/\/\S+/g)) if (/launchpad\.net|\.deb\b/.test(url)) problems.push(`the Dockerfile fetches ${url} outside a kernel stage`);
  return problems;
}

it("fetches every package of the kernel pin from Launchpad by its hash, for each arch, the kernel's tools too, and none from apt", () => {
  expect(pinProblems(DOCKERFILE)).toEqual([]);
  // Each arch's own stage, which the other arch's build never reads.
  expect([...STAGES.keys()].filter((name) => name.startsWith("kernel-")).sort()).toEqual(ARCHES.map((arch) => `kernel-${arch}`));
  expect(/^FROM kernel-\$\{TARGETARCH\} AS kernel$/m.test(DOCKERFILE)).toBe(true);
});

it("refuses an arm64 kernel package without its hash, or of another version than the pin's, or another kernel's", () => {
  const image = /^ADD --checksum=sha256:[0-9a-f]{64} \\\n(\s+\S+\/linux-image-[^_]+_[^_]+_arm64\.deb \S+)$/m.exec(DOCKERFILE);
  expect(image).not.toBeNull();
  const url = /\S+\.deb/.exec(image?.[1] ?? "")?.[0] ?? "";
  // Not one of the stage's pinned packages, so the pin lacks it too.
  expect(pinProblems(DOCKERFILE.replace(image?.[0] ?? "", `ADD \\\n${image?.[1] ?? ""}`))).toContain(`kernel-arm64 fetches ${url} without its hash`);
  const [, version] = /_([^_]+)_arm64\.deb$/.exec(url) ?? [];
  const older = DOCKERFILE.replace(url, url.replace(`_${version}_`, "_7.0.0-38.38~24.04.3_"));
  expect(pinProblems(older)).toEqual([`the kernel's packages are of 2 versions: 7.0.0-38.38~24.04.3, ${version}`]);
  // Another kernel's package, for this arch: the pin's version, the name of another ABI.
  const other = DOCKERFILE.replace(url, url.replace("/linux-image-7.0.0-38-generic_", "/linux-image-7.0.0-37-generic_"));
  expect(pinProblems(other)).toEqual([expect.stringMatching(/^kernel-arm64 fetches linux-hwe-7\.0-tools-7\.0\.0-38, linux-image-7\.0\.0-37-generic, /)]);
  // An amd64 package in arm64's stage.
  expect(pinProblems(DOCKERFILE.replace(url, url.replace(/_arm64\.deb$/, "_amd64.deb")))).toEqual(["kernel-arm64 fetches linux-image-7.0.0-38-generic for amd64"]);
});

const REPO = fileURLToPath(new URL("../..", import.meta.url));
const inputs = (repo = REPO, ...args: string[]) => {
  const ran = spawnSync(join(repo, "images/guest/inputs.sh"), args, { encoding: "utf8" });
  if (ran.status !== 0) throw new Error(`inputs.sh failed: ${ran.error?.message ?? ran.stderr}`);
  return ran.stdout.trim();
};

it("keys the image by the guest's stages and each file they read from the repository, and not by the cloud sandbox's own stage", () => {
  // Every repository file a stage before the sandbox's reads: what its COPY and ADD lines name that
  // is not a stage's, an image's or a URL, and what a RUN binds from the build's context.
  const guestStages = DOCKERFILE.slice(0, DOCKERFILE.indexOf("\nFROM tools AS sandbox\n")).replace(/\\\n\s*/g, "");
  const copied = [...guestStages.matchAll(/^(?:COPY|ADD) (?![^\n]*--from=)(?!<<)(?:--\S+ )*(.+) \S+$/gm)]
    .flatMap(([, sources]) => (sources ?? "").split(" ")).filter((source) => !/^https?:\/\//.test(source));
  const bound = [...guestStages.matchAll(/--mount=(?![^ ]*from=)[^ ]*source=([^, ]+)/g)].map(([, source]) => source ?? "");
  const hashed = inputs(REPO, "--files").split("\n");
  expect(hashed.sort()).toEqual([...copied, ...bound, "images/guest/build.sh"].sort());

  // A copy of what it hashes: a change to any of it is a new key, one past the guest's stages is not.
  const mirror = mkdtempSync(join(tmpdir(), "inputs-"));
  try {
    for (const file of ["images/guest/inputs.sh", "images/sandbox/Dockerfile", ...hashed]) {
      mkdirSync(dirname(join(mirror, file)), { recursive: true });
      copyFileSync(join(REPO, file), join(mirror, file));
    }
    const key = inputs(mirror);
    expect(key).toMatch(/^[0-9a-f]{64}$/);
    expect(key).toBe(inputs(REPO));
    appendFileSync(join(mirror, "images/sandbox/Dockerfile"), "RUN echo the cloud sandbox only\n");
    expect(inputs(mirror)).toBe(key);
    for (const file of ["desktop/vm/rule-match.h", "images/guest/build.sh"]) {
      appendFileSync(join(mirror, file), "\n");
      expect(inputs(mirror)).not.toBe(key);
    }
    writeFileSync(join(mirror, "images/sandbox/Dockerfile"), DOCKERFILE.replace("ARG KERNEL=", "ARG KERNEL=x"));
    expect(inputs(mirror)).not.toBe(inputs(REPO));
  } finally {
    rmSync(mirror, { recursive: true, force: true });
  }
});

it("keeps of the kernel's modules only those the guest's init loads", () => {
  const kept = [...(STAGES.get("kernel") ?? "").matchAll(/! -name (\S+)\.ko\.zst/g)].map(([, name]) => name);
  const init = readFileSync(new URL("../vm/init", import.meta.url), "utf8");
  const loaded = [...init.matchAll(/^modprobe (\S+)$/gm)].map(([, name]) => name);
  expect(kept.sort()).toEqual(loaded.sort());
  expect(kept.length).toBeGreaterThan(0);
});

describe("the check of the kernel's configuration", () => {
  // What the guest boots on without an initrd, built in; on arm64 the virt machine's PCIe host too.
  const BUILT_IN = ["VIRTIO_BLK", "EXT4_FS", "VIRTIO_CONSOLE", "VIRTIO_PCI", "BPF_LSM", "DEBUG_INFO_BTF", "FUSE_FS"];
  // What vm/init loads, a module or built in.
  const LOADED = ["VIRTIO_FS", "OVERLAY_FS"];
  let dir: string;
  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), "kernel-config-"));
  });
  afterEach(() => {
    rmSync(dir, { recursive: true, force: true });
  });
  // A config-* file as the modules package carries it, with *options* as each is set there.
  const config = (options: Record<string, string>) => {
    const file = join(dir, "config-7.0.0-38-generic");
    writeFileSync(file, ["#", "# Automatically generated file; DO NOT EDIT.", "#", ...Object.entries(options).map(([name, value]) =>
      value === "unset" ? `# CONFIG_${name} is not set` : `CONFIG_${name}=${value}`), "CONFIG_LSM=\"landlock,lockdown,yama,integrity,apparmor\"", ""].join("\n"));
    return file;
  };
  const whole = (arch: string): Record<string, string> => ({
    ...Object.fromEntries(BUILT_IN.map((name) => [name, "y"])), ...Object.fromEntries(LOADED.map((name) => [name, "m"])),
    ...(arch === "arm64" ? { PCI_HOST_GENERIC: "y" } : {}),
  });
  const checked = (...args: string[]) => spawnSync(join(REPO, "images/guest/kernel-config.sh"), args, { encoding: "utf8" });

  it("passes a kernel that builds in what the guest boots on, with what vm/init loads a module or built in", () => {
    for (const arch of ARCHES) {
      expect(checked(arch, config(whole(arch)))).toMatchObject({ status: 0, stderr: "" });
      expect(checked(arch, config({ ...whole(arch), VIRTIO_FS: "y", OVERLAY_FS: "y" })).status).toBe(0);
    }
    // amd64's q35 has a PCIe host of its own: the generic one is arm64's alone.
    expect(checked("amd64", config({ ...whole("amd64"), PCI_HOST_GENERIC: "unset" })).status).toBe(0);
  });

  it("fails on a kernel that lacks any of them, or has one that must be built in as a module, and names each", () => {
    const file = config({ ...whole("arm64"), VIRTIO_BLK: "m", EXT4_FS: "unset", PCI_HOST_GENERIC: "unset", VIRTIO_FS: "unset" });
    const { DEBUG_INFO_BTF: _btf, ...withoutBtf } = whole("arm64");
    expect(checked("arm64", file)).toMatchObject({
      status: 1,
      stderr: `kernel-config.sh: ${file} does not build in CONFIG_VIRTIO_BLK CONFIG_EXT4_FS CONFIG_PCI_HOST_GENERIC, which the guest boots on without an initrd\n`
        + `kernel-config.sh: ${file} has no CONFIG_VIRTIO_FS, which vm/init loads\n`,
    });
    expect(checked("arm64", config(withoutBtf))).toMatchObject({ status: 1, stderr: expect.stringContaining("does not build in CONFIG_DEBUG_INFO_BTF,") });
    // A line that names the option within another's is not it.
    expect(checked("amd64", config({ ...whole("amd64"), BPF_LSM: "unset", BPF_LSM_EXTRA: "y" })).status).toBe(1);
  });

  it("refuses an arch it has no list for, and a config it cannot read", () => {
    expect(checked("riscv64", config(whole("arm64")))).toMatchObject({ status: 2 });
    expect(checked("arm64", join(dir, "missing"))).toMatchObject({ status: 1, stderr: expect.stringContaining("cannot read") });
  });
});

describe("the arm64 kernel as the VM boots it", () => {
  let dir: string;
  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), "kernel-image-"));
  });
  afterEach(() => {
    rmSync(dir, { recursive: true, force: true });
  });
  // An uncompressed arm64 Image: its header (Documentation/arch/arm64/booting.rst), "ARM\x64" at 56, an EFI
  // stub's "MZ" at 0, then the kernel. *magic* in place of the real one.
  const anImage = (magic = "ARM\x64") => {
    const header = Buffer.alloc(64);
    header.write("MZ", 0, "latin1");
    header.write(magic, 56, "latin1");
    return Buffer.concat([header, randomBytes(200_000), Buffer.alloc(100_000)]);
  };
  // An EFI zboot file (drivers/firmware/efi/libstub/zboot-header.S): "MZ", "zimg" at 4, the payload's offset and
  // size at 8 and 12, its compression's name at 24; the payload, as Linux's with_size methods leave it, the
  // uncompressed size after it.
  const zboot = (image: Buffer, type: string, compressed: Buffer) => {
    const header = Buffer.alloc(4096);
    header.write("MZ", 0, "latin1");
    header.write("zimg", 4, "latin1");
    header.writeUInt32LE(header.length, 8);
    header.writeUInt32LE(compressed.length, 12);
    header.write(type, 24, "latin1");
    const size = Buffer.alloc(4);
    size.writeUInt32LE(image.length);
    return Buffer.concat([header, compressed, size, Buffer.alloc(512)]);
  };
  const compressed = (program: string, data: Buffer) => spawnSync(program, ["-c"], { input: data, maxBuffer: 64 * 1024 * 1024 }).stdout;
  const made = (vmlinuz: Buffer) => {
    writeFileSync(join(dir, "vmlinuz"), vmlinuz);
    const ran = spawnSync(join(REPO, "images/guest/kernel-image.sh"), [join(dir, "vmlinuz"), join(dir, "Image")], { encoding: "utf8" });
    return { ...ran, files: readdirSync(dir).sort() };
  };

  it("unpacks an EFI zboot file's payload, zstd or gzip, a gzip of the Image, and takes an Image as it is", () => {
    const image = anImage();
    for (const vmlinuz of [zboot(image, "zstd", compressed("zstd", image)), zboot(image, "gzip", compressed("gzip", image)), compressed("gzip", image), image]) {
      expect(made(vmlinuz)).toMatchObject({ status: 0, stderr: "", files: ["Image", "vmlinuz"] });
      expect(readFileSync(join(dir, "Image")).equals(image)).toBe(true);
      rmSync(join(dir, "Image"));
    }
  });

  it("refuses what is not an arm64 Image once unpacked, and a zboot file of a compression it does not unpack, and leaves nothing", () => {
    const x86 = anImage("HdrS");
    for (const vmlinuz of [x86, compressed("gzip", x86), zboot(x86, "zstd", compressed("zstd", x86))]) {
      expect(made(vmlinuz)).toMatchObject({ status: 1, stderr: `kernel-image.sh: ${join(dir, "vmlinuz")} is not an arm64 kernel: its Image has no ARM\\x64 magic\n`, files: ["vmlinuz"] });
    }
    const image = anImage();
    expect(made(zboot(image, "lz4", compressed("zstd", image)))).toMatchObject({
      status: 1, stderr: `kernel-image.sh: ${join(dir, "vmlinuz")} is an EFI zboot file compressed with lz4, which this does not unpack\n`, files: ["vmlinuz"],
    });
    expect(made(Buffer.from("not a kernel at all"))).toMatchObject({ status: 1, files: ["vmlinuz"] });
  });
});

it("bounds every transfer of the release's guest jobs: a stall, and its whole time", () => {
  for (const script of ["images/guest/publish.sh", "images/guest/kernel-current.sh"]) {
    const curls = readFileSync(join(REPO, script), "utf8").split("\n").filter((line) => line.includes("curl -q "));
    expect(curls.length, script).toBeGreaterThan(0);
    for (const curl of curls) for (const bound of ["--connect-timeout", "--speed-limit", "--speed-time", "--max-time"]) expect(curl, script).toContain(bound);
  }
});

describe("the release's check of the guest's kernel", () => {
  let server: Server;
  let archive: string;
  let packages: Buffer;
  // noble-security's Packages index, as the archive serves it: xz'd, a stanza a package.
  const index = (...stanzas: Array<[string, string]>) => {
    const text = stanzas.map(([name, version]) => `Package: ${name}\nArchitecture: amd64\nVersion: ${version}\n`).join("\n");
    return spawnSync("xz", ["-c"], { input: text }).stdout;
  };
  // Not spawnSync: the archive is this process's own server, which must answer meanwhile. *repo*: the
  // repository whose script and Dockerfile it reads.
  const check = (repo = REPO) => new Promise<{ status: number | null; stdout: string; stderr: string }>((resolve) => {
    execFile(join(repo, "images/guest/kernel-current.sh"), { env: { ...process.env, UBUNTU_SECURITY: archive } }, (error, stdout, stderr) => {
      resolve({ status: error ? (typeof error.code === "number" ? error.code : null) : 0, stdout, stderr });
    });
  });
  const pin = /linux-image-[^_]*-generic_([^_]*)_amd64\.deb /.exec(DOCKERFILE)?.[1] ?? "";

  beforeEach(async () => {
    server = createServer((request, response) => {
      if (request.url !== "/ubuntu/dists/noble-security/main/binary-amd64/Packages.xz") return void response.writeHead(404).end();
      response.writeHead(200).end(packages);
    });
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    archive = `http://127.0.0.1:${(server.address() as AddressInfo).port}/ubuntu`;
  });

  afterEach(async () => {
    await new Promise((resolve) => server.close(resolve));
  });

  it("passes while noble-security's hardware-enablement kernel is the pin, or older", async () => {
    expect(pin).toMatch(/^\d+\.\d+\.\d+-\d+\.\d+~24\.04\.\d+$/);
    packages = index(["linux-image-generic", "6.8.0-90.91"], ["linux-image-generic-hwe-24.04", pin]);
    expect(await check()).toMatchObject({ status: 0, stdout: `the guest's kernel ${pin} is noble-security's current one\n` });
    packages = index(["linux-image-generic-hwe-24.04", "7.0.0-28.28~24.04.1"]);
    expect((await check()).status).toBe(0);
  });

  it("fails a release while noble-security carries a newer one, in any of its stanzas, and says which", async () => {
    // Newer than any pin of the 7.0 series, so a pin moved to noble-security's current one stays older.
    const later = "7.0.0-999.999~24.04.1";
    const newer = {
      status: 1,
      stderr: `kernel-current.sh: noble-security carries linux-image-generic-hwe-24.04 ${later}, newer than the guest's pin ${pin}: pin it in images/sandbox/Dockerfile\n`,
    };
    packages = index(["linux-image-generic-hwe-24.04", later]);
    expect(await check()).toMatchObject(newer);
    // An index that lists two versions, the newer first.
    packages = index(["linux-image-generic-hwe-24.04", later], ["linux-image-generic-hwe-24.04", pin]);
    expect(await check()).toMatchObject(newer);
  });

  it("fails on a version dpkg cannot compare, the archive's or the pin's, rather than taking it for not newer", async () => {
    packages = index(["linux-image-generic-hwe-24.04", "v7.0.0-38.38~24.04.4"], ["linux-image-generic-hwe-24.04", pin]);
    expect(await check()).toMatchObject({
      status: 1, stderr: `kernel-current.sh: dpkg cannot compare noble-security's linux-image-generic-hwe-24.04 v7.0.0-38.38~24.04.4 with the guest's pin ${pin}\n`,
    });
    // A Dockerfile whose text names a second kernel .deb: two lines, no one version.
    const mirror = mkdtempSync(join(tmpdir(), "kernel-current-"));
    try {
      mkdirSync(join(mirror, "images/guest"), { recursive: true });
      mkdirSync(join(mirror, "images/sandbox"));
      copyFileSync(join(REPO, "images/guest/kernel-current.sh"), join(mirror, "images/guest/kernel-current.sh"));
      writeFileSync(join(mirror, "images/sandbox/Dockerfile"), `${DOCKERFILE}# https://x/linux-image-7.0.0-99-generic_7.0.0-99.99~24.04.1_amd64.deb old\n`);
      packages = index(["linux-image-generic-hwe-24.04", "7.0.0-38.38~24.04.4"]);
      expect(await check(mirror)).toMatchObject({ status: 1, stderr: "kernel-current.sh: images/sandbox/Dockerfile's kernel pin is not one version dpkg can compare\n" });
    } finally {
      rmSync(mirror, { recursive: true, force: true });
    }
  });

  it("fails when the archive cannot say", async () => {
    packages = index(["linux-image-generic", "6.8.0-90.91"]);
    expect(await check()).toMatchObject({ status: 1, stderr: "kernel-current.sh: noble-security lists no linux-image-generic-hwe-24.04\n" });
    archive = `${archive}/missing`;
    expect((await check()).status).not.toBe(0);
  });
});

describe("the release's check of the arm64 guest's kernel", () => {
  let server: Server;
  let base: string;
  // Each archive's noble-security index, by its path: the ports archive carries arm64's, the security archive amd64's.
  const served = new Map<string, Buffer>();
  const index = (arch: string, version: string) =>
    spawnSync("xz", ["-c"], { input: `Package: linux-image-generic-hwe-24.04\nArchitecture: ${arch}\nVersion: ${version}\n` }).stdout;
  const check = (...args: string[]) => new Promise<{ status: number | null; stdout: string; stderr: string }>((resolve) => {
    const env = { ...process.env, UBUNTU_SECURITY: `${base}/ubuntu`, UBUNTU_PORTS: `${base}/ubuntu-ports` };
    execFile(join(REPO, "images/guest/kernel-current.sh"), args, { env }, (error, stdout, stderr) => {
      resolve({ status: error ? (typeof error.code === "number" ? error.code : null) : 0, stdout, stderr });
    });
  });
  const pin = /linux-image-[^_]*-generic_([^_]*)_arm64\.deb /.exec(DOCKERFILE)?.[1] ?? "";
  const later = "7.0.0-999.999~24.04.1";

  beforeEach(async () => {
    served.clear();
    server = createServer((request, response) => {
      const body = served.get(request.url ?? "");
      if (!body) return void response.writeHead(404).end();
      response.writeHead(200).end(body);
    });
    await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
    base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
  });

  afterEach(async () => {
    await new Promise((resolve) => server.close(resolve));
  });

  it("reads the ports archive's arm64 index against the arm64 pin, and amd64's own archive for amd64", async () => {
    expect(pin).toMatch(/^\d+\.\d+\.\d+-\d+\.\d+~24\.04\.\d+$/);
    served.set("/ubuntu-ports/dists/noble-security/main/binary-arm64/Packages.xz", index("arm64", pin));
    served.set("/ubuntu/dists/noble-security/main/binary-amd64/Packages.xz", index("amd64", later));
    expect(await check("--arch", "arm64")).toMatchObject({ status: 0, stdout: `the guest's arm64 kernel ${pin} is noble-security's current one\n` });
    expect(await check("--arch", "amd64")).toMatchObject({ status: 1, stderr: expect.stringContaining(`carries linux-image-generic-hwe-24.04 ${later}`) });
    // amd64's unless it is told another.
    expect((await check()).status).toBe(1);
  });

  it("fails a release while the ports archive carries a newer arm64 kernel, and says which", async () => {
    served.set("/ubuntu-ports/dists/noble-security/main/binary-arm64/Packages.xz", index("arm64", later));
    served.set("/ubuntu/dists/noble-security/main/binary-amd64/Packages.xz", index("amd64", pin));
    expect(await check("--arch", "arm64")).toMatchObject({
      status: 1,
      stderr: `kernel-current.sh: noble-security carries linux-image-generic-hwe-24.04 ${later} for arm64, newer than the guest's pin ${pin}: pin it in images/sandbox/Dockerfile\n`,
    });
  });

  it("refuses an arch it builds no guest for", async () => {
    expect(await check("--arch", "riscv64")).toMatchObject({ status: 2, stderr: "usage: images/guest/kernel-current.sh [--arch amd64|arm64]\n" });
  });
});
