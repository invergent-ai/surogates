// The guest image's build (images/sandbox/Dockerfile and images/guest/): its kernel pin, its key's
// inputs, the modules it keeps, and the release's check of its kernel.

import { execFile, spawnSync } from "node:child_process";
import { createHash, randomBytes } from "node:crypto";
import { appendFileSync, copyFileSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
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
  // Each once, though two stages copy it.
  expect(hashed.sort()).toEqual([...new Set([...copied, ...bound, "images/guest/build.sh"])].sort());

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

// *test* on a copy of what inputs.sh hashes, in a repository of its own.
function mirrored(test: (mirror: string) => void): void {
  const mirror = mkdtempSync(join(tmpdir(), "inputs-"));
  try {
    for (const file of ["images/guest/inputs.sh", "images/sandbox/Dockerfile", ...inputs(REPO, "--files").split("\n")]) {
      mkdirSync(dirname(join(mirror, file)), { recursive: true });
      copyFileSync(join(REPO, file), join(mirror, file));
    }
    test(mirror);
  } finally {
    rmSync(mirror, { recursive: true, force: true });
  }
}

it("keys each arch's image apart, and neither by the other's kernel stage", () => {
  const amd64 = inputs(REPO, "amd64");
  const arm64 = inputs(REPO, "arm64");
  expect(amd64).toMatch(/^[0-9a-f]{64}$/);
  expect(arm64).toMatch(/^[0-9a-f]{64}$/);
  expect(arm64).not.toBe(amd64);
  // amd64's unless it is told another.
  expect(inputs(REPO)).toBe(amd64);
  mirrored((mirror) => {
    const dockerfile = join(mirror, "images/sandbox/Dockerfile");
    // A line of one arch's kernel stage changes that arch's key alone.
    for (const [arch, own, other] of [["arm64", arm64, amd64], ["amd64", amd64, arm64]] as const) {
      const hash = new RegExp(`^ADD --checksum=sha256:([0-9a-f]{64}) \\\\\\n\\s+\\S+_${arch}\\.deb `, "m").exec(DOCKERFILE)?.[1] ?? "";
      expect(hash).toMatch(/^[0-9a-f]{64}$/);
      writeFileSync(dockerfile, DOCKERFILE.replace(hash, "0".repeat(64)));
      expect(inputs(mirror, arch), arch).not.toBe(own);
      expect(inputs(mirror, arch === "arm64" ? "amd64" : "arm64"), arch).toBe(other);
    }
    // One that both arches build changes both.
    writeFileSync(dockerfile, DOCKERFILE.replace("ARG KERNEL=", "ARG KERNEL=x"));
    expect(inputs(mirror, "amd64")).not.toBe(amd64);
    expect(inputs(mirror, "arm64")).not.toBe(arm64);
  });
  expect(spawnSync(join(REPO, "images/guest/inputs.sh"), ["riscv64"], { encoding: "utf8" })).toMatchObject({ status: 2, stdout: "" });
});

describe("the image's build", () => {
  let dir: string;
  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), "guest-build-"));
    // docker, as build.sh calls it: what it was asked written down, and the guest-disk target's two files left in its output.
    mkdirSync(join(dir, "bin"));
    writeFileSync(join(dir, "bin", "docker"), [
      "#!/bin/sh",
      `printf '%s\\n' "$@" > '${join(dir, "docker-argv")}'`,
      'for arg; do case "$arg" in type=local,dest=*) out="${arg#type=local,dest=}" ;; esac; done',
      'mkdir -p "$out" && printf initrd | zstd -q -c > "$out/initrd.zst" && printf disk | zstd -q -c > "$out/rootfs.img.zst" && printf kernel | zstd -q -c > "$out/vmlinuz.zst"',
    ].join("\n"), { mode: 0o755 });
  });
  afterEach(() => {
    rmSync(dir, { recursive: true, force: true });
  });
  const build = (...args: string[]) =>
    spawnSync(join(REPO, "images/guest/build.sh"), [...args], { encoding: "utf8", env: { ...process.env, PATH: `${join(dir, "bin")}:${process.env.PATH ?? ""}` } });
  const sha256 = (text: string) => createHash("sha256").update(text).digest("hex");

  it("builds the guest's disk for the arch it is told, amd64's unless it says arm64, and names its arch and key in its manifest", () => {
    for (const [args, arch] of [[[], "amd64"], [["--arch", "arm64"], "arm64"], [["--packed", "--arch", "amd64"], "amd64"]] as const) {
      const out = join(dir, `out-${args.join("")}`);
      expect(build(...args, out).status).toBe(0);
      const argv = readFileSync(join(dir, "docker-argv"), "utf8").trim().split("\n");
      expect(argv.slice(0, 5)).toEqual(["build", "--platform", `linux/${arch}`, "--target", "guest-disk"]);
      const download = (name: string) => readFileSync(join(out, `${name}.zst`));
      const file = (name: string, unpacked: string) => ({
        name, size: unpacked.length, sha256: sha256(unpacked), download: `${name}.zst`,
        downloadSize: download(name).length, downloadSha256: createHash("sha256").update(download(name)).digest("hex"),
      });
      expect(JSON.parse(readFileSync(join(out, "manifest.json"), "utf8"))).toEqual({
        key: inputs(REPO, arch), arch, files: [file("initrd", "initrd"), file("rootfs.img", "disk"), file("vmlinuz", "kernel")],
      });
      // Unpacked beside the downloads for the VM tests, unless packed.
      expect(readdirSync(out).sort()).toEqual((args as readonly string[]).includes("--packed")
        ? ["initrd.zst", "manifest.json", "rootfs.img.zst", "vmlinuz.zst"]
        : ["initrd", "initrd.zst", "manifest.json", "rootfs.img", "rootfs.img.zst", "vmlinuz", "vmlinuz.zst"]);
    }
  });

  it("builds no image for an arch it has no guest for", () => {
    expect(build("--arch", "riscv64", join(dir, "out"))).toMatchObject({ status: 2 });
    expect(readdirSync(dir)).toEqual(["bin"]);
  });
});

describe("the arm64 kernel's BTF, which the rule is compiled and checked against", () => {
  let dir: string;
  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), "kernel-btf-"));
  });
  afterEach(() => {
    rmSync(dir, { recursive: true, force: true });
  });
  // A raw BTF: its header (magic 0xeB9F, version 1, flags 0, a 24-byte header), its types and strings.
  const btf = () => {
    const [types, strings] = [randomBytes(3000), Buffer.from("\0bpf_lsm_file_open\0")];
    const header = Buffer.alloc(24);
    header.writeUInt16LE(0xeb9f, 0);
    header.writeUInt8(1, 2);
    header.writeUInt32LE(24, 4);
    header.writeUInt32LE(0, 8);
    header.writeUInt32LE(types.length, 12);
    header.writeUInt32LE(types.length, 16);
    header.writeUInt32LE(strings.length, 20);
    return Buffer.concat([header, types, strings]);
  };
  // An uncompressed Image with *blob* at *at*, and the System.map that names where the kernel's text and its .BTF are.
  const kernel = (blob: Buffer, at: number, symbols = ["_text", "__start_BTF", "__stop_BTF"]) => {
    writeFileSync(join(dir, "Image"), Buffer.concat([randomBytes(at), blob, randomBytes(4096)]));
    const base = 0xffff800080000000n;
    const where = { _text: base, __start_BTF: base + BigInt(at), __stop_BTF: base + BigInt(at + blob.length) } as Record<string, bigint>;
    writeFileSync(join(dir, "System.map"), [
      `${base.toString(16)} t _head`, ...symbols.map((name) => `${(where[name] ?? 0n).toString(16)} ${name === "_text" ? "T" : "R"} ${name}`),
      `${(base + 16n).toString(16)} T _stext`, "",
    ].join("\n"));
  };
  const read = () => spawnSync(join(REPO, "images/guest/kernel-btf.sh"), [join(dir, "Image"), join(dir, "System.map"), join(dir, "btf")], { encoding: "utf8" });

  it("reads the .BTF section out of the Image, where System.map puts it from the kernel's text", () => {
    const blob = btf();
    kernel(blob, 70_000);
    expect(read()).toMatchObject({ status: 0, stderr: "" });
    expect(readFileSync(join(dir, "btf")).equals(blob)).toBe(true);
  });

  it("refuses an Image that has no BTF where its map says, and a map that does not say, and leaves nothing", () => {
    const shifted = Buffer.concat([Buffer.alloc(8), btf()]);
    kernel(shifted, 70_000);
    expect(read()).toMatchObject({ status: 1, stderr: expect.stringMatching(/^kernel-btf\.sh: .*Image has no BTF where .*System\.map puts its \.BTF\n$/) });
    expect(readdirSync(dir).sort()).toEqual(["Image", "System.map"]);
    kernel(btf(), 70_000, ["_text", "__start_BTF"]);
    expect(read()).toMatchObject({ status: 1, stderr: expect.stringMatching(/^kernel-btf\.sh: .*System\.map names no __stop_BTF\n$/) });
    expect(readdirSync(dir).sort()).toEqual(["Image", "System.map"]);
  });
});

// The names the kernel stage's modules.sh *mode* is given, in their order.
const modulesGiven = (mode: string) => {
  const run = runsOf("modules").flatMap(chain).find((command) => command.startsWith(`modules.sh ${mode} `)) ?? "";
  return run.split(" ").slice(3).filter((word) => !word.startsWith("$(") && word !== "'s/#.*//'" && !word.endsWith(")"));
};

it("keeps of the kernel's modules only those the guest's init loads, with those they need, and copies out the initrd's", () => {
  const init = readFileSync(new URL("../vm/init", import.meta.url), "utf8");
  const loaded = [...init.matchAll(/^\s*modprobe (\S+)$/gm)].map(([, name]) => name);
  // The two the 9P client and its fd transport need, which the image's build checks against modules.dep (modules.sh).
  expect(modulesGiven("keep")).toEqual(["netfs", "9pnet", "9pnet_fd", "9p", "virtiofs", "overlay"]);
  expect(modulesGiven("keep").filter((name) => !["netfs", "9pnet"].includes(name)).sort()).toEqual([...loaded].sort());
  // Never Hyper-V's time sync, which would set the guest's clock under the agent; the initrd's, from its list.
  expect(loaded).not.toContain("hv_utils");
  expect(runsOf("modules").flatMap(chain)).toContain('modules.sh copy "/image/lib/modules/$KERNEL" /initrd/lib $(sed \'s/#.*//\' /initrd/modules)');
  const listed = readFileSync(join(REPO, "images/guest/initrd/modules"), "utf8").replace(/#.*/g, "").split(/\s+/).filter(Boolean);
  expect(listed).toEqual(["hv_vmbus", "scsi_transport_fc", "hv_storvsc", "vsock", "hv_sock", "hv_balloon"]);
});

// Each RUN of *stage* as its shell reads it: its continuations joined, and each run of spaces one space.
const runsOf = (stage: string) =>
  [...(STAGES.get(stage) ?? "").replace(/\\\n/g, " ").replace(/[ \t]+/g, " ").matchAll(/^RUN (.+)$/gm)].map(([, run]) => run ?? "");
// The commands an && chain runs, in order.
const chain = (run: string) => run.split(" && ").map((command) => command.trim());
// The commands of *arch*'s branch of the RUN in *stage* that picks one by TARGETARCH.
const branchOf = (stage: string, arch: string) => {
  const run = runsOf(stage).find((each) => each.includes('case "$TARGETARCH" in ')) ?? "";
  return chain(new RegExp(`case "\\$TARGETARCH" in .*?\\b${arch}\\) (.*?) ;;`).exec(run)?.[1] ?? "");
};

it("checks the kernel's configuration, makes arm64's Image and BTF, and compiles the rule for the arch it builds, in the stages that need each", () => {
  for (const [stage, scripts] of [["kernel", ["kernel-config.sh"]], ["modules", ["modules.sh"]], ["bpf", ["kernel-image.sh", "kernel-btf.sh"]], ["guest-disk-build", ["kernel-image.sh"]], ["initrd", ["initrd.sh"]]] as const) {
    for (const script of scripts) {
      expect(new RegExp(`^COPY --chmod=0755 (?:\\S+ )*images/guest/${script.replace(".", "\\.")} (?:\\S+ )*/usr/local/bin/`, "m").test(STAGES.get(stage) ?? ""), `${stage} copies ${script}`).toBe(true);
    }
  }
  // The check, of the config the modules package carries, in the chain that unpacks it: a kernel it fails is never built on.
  expect(runsOf("kernel").map(chain)).toEqual([[
    "dpkg-deb -x /tmp/image.deb /kernel", "dpkg-deb -x /tmp/modules.deb /kernel", "dpkg-deb -x /tmp/tools.deb /tools", "dpkg-deb -x /tmp/hwe-tools.deb /tools",
    'kernel-config.sh "$TARGETARCH" "/kernel/boot/config-$KERNEL"',
  ]]);
  // The modules, by the modules.dep depmod makes of the pinned kernel's whole tree, before any is kept or copied out.
  expect(runsOf("modules").at(-1)?.split(" && ").map((command) => command.trim())).toEqual([
    '--mount=type=bind,from=kernel,source=/kernel/lib/modules,target=/all mkdir -p /image/lib/modules', 'cp -a "/all/$KERNEL" /image/lib/modules/',
    'depmod -b /image "$KERNEL"', expect.stringMatching(/^modules\.sh copy .* \/initrd\/lib /),
    'modules.sh copy "/image/lib/modules/$KERNEL" /standin/lib vsock vmw_vsock_virtio_transport_common vmw_vsock_virtio_transport',
    expect.stringMatching(/^modules\.sh keep /),
  ]);
  // The stand-in's modules, for the tests alone, in a target of their own: in neither the image nor its disk.
  expect((STAGES.get("guest-standin") ?? "").split("\n").filter((line) => line && !line.startsWith("#"))).toEqual(["COPY --from=modules /standin/lib/ /"]);
  for (const stage of ["guest", "guest-disk-build", "guest-disk", "initrd"]) expect(STAGES.get(stage), stage).not.toMatch(/standin/);
  expect(STAGES.get("guest")).toContain("COPY --from=modules /image/lib/modules/ /usr/lib/modules/");
  // The initrd, from the kernel stage's copy of its modules, beside the kernel the VM boots.
  expect(runsOf("initrd").at(-1)).toBe("--mount=type=bind,from=modules,source=/initrd,target=/modules mkdir /out && initrd.sh /src/init /bin/busybox /modules/lib /modules/modules /out/initrd");
  expect(runsOf("guest-disk-build").flatMap(chain)).toContain("zstd -q -19 -o /out/initrd.zst /initrd/initrd");
  // The rule's BTF: amd64's the vmlinux its bzImage holds, arm64's read out of the Image. Each compiled for its own arch.
  expect(branchOf("bpf", "amd64").at(-1)).toBe("bpf_arch=x86");
  expect(branchOf("bpf", "amd64").join(" ")).not.toContain("kernel-");
  expect(branchOf("bpf", "arm64")).toEqual(['kernel-image.sh "/boot/vmlinuz-$KERNEL" /Image', 'kernel-btf.sh /Image "/boot/System.map-$KERNEL" /btf', "bpf_arch=arm64"]);
  const compile = runsOf("bpf").flatMap(chain).filter((command) => command.startsWith("clang "));
  expect(compile).toEqual([expect.stringContaining(' "-D__TARGET_ARCH_$bpf_arch" ')]);
  expect(compile.join(" ").match(/__TARGET_ARCH_/g)).toHaveLength(1);
  // The kernel the VM boots: amd64's vmlinuz as it is, arm64's its Image.
  expect(branchOf("guest-disk-build", "amd64")).toEqual(['zstd -q -19 -o /out/vmlinuz.zst "/boot/vmlinuz-$KERNEL"']);
  expect(branchOf("guest-disk-build", "arm64")).toEqual(['kernel-image.sh "/boot/vmlinuz-$KERNEL" /out/vmlinuz', "zstd -q -19 --rm -o /out/vmlinuz.zst /out/vmlinuz"]);
});

describe("the check of the kernel's configuration", () => {
  // What the guest boots on without an initrd, built in; on arm64 the virt machine's PCIe host too.
  const BUILT_IN = ["VIRTIO_BLK", "EXT4_FS", "VIRTIO_CONSOLE", "VIRTIO_PCI", "BPF_LSM", "DEBUG_INFO_BTF", "FUSE_FS"];
  // What it boots on with Hyper-V's initrd, built in: on amd64 Hyper-V's clock and the UART too.
  const HYPERV_BUILT_IN = ["BLK_DEV_INITRD", "HYPERV", "SCSI", "BLK_DEV_SD"];
  // What vm/init loads, and what the initrd does, a module or built in.
  const LOADED = ["VIRTIO_FS", "OVERLAY_FS", "NETFS_SUPPORT", "NET_9P", "NET_9P_FD", "9P_FS"];
  const INITRD = ["HYPERV_VMBUS", "SCSI_FC_ATTRS", "HYPERV_STORAGE", "VSOCKETS", "HYPERV_VSOCKETS", "HYPERV_BALLOON"];
  const hyperv = (arch: string) => [...HYPERV_BUILT_IN, ...(arch === "amd64" ? ["HYPERV_TIMER", "SERIAL_8250_CONSOLE"] : [])];
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
    ...Object.fromEntries([...BUILT_IN, ...hyperv(arch)].map((name) => [name, "y"])), ...Object.fromEntries([...LOADED, ...INITRD].map((name) => [name, "m"])),
    ...(arch === "arm64" ? { PCI_HOST_GENERIC: "y" } : {}),
  });
  const checked = (...args: string[]) => spawnSync(join(REPO, "images/guest/kernel-config.sh"), args, { encoding: "utf8" });

  it("passes a kernel that builds in what the guest boots on, with what vm/init and the initrd load a module or built in", () => {
    for (const arch of ARCHES) {
      expect(checked(arch, config(whole(arch)))).toMatchObject({ status: 0, stderr: "" });
      expect(checked(arch, config({ ...whole(arch), VIRTIO_FS: "y", OVERLAY_FS: "y", HYPERV_VMBUS: "y" })).status).toBe(0);
    }
    // amd64's q35 has a PCIe host of its own: the generic one is arm64's alone. Hyper-V's clock and UART are amd64's.
    expect(checked("amd64", config({ ...whole("amd64"), PCI_HOST_GENERIC: "unset" })).status).toBe(0);
    expect(checked("arm64", config({ ...whole("arm64"), HYPERV_TIMER: "unset", SERIAL_8250_CONSOLE: "unset" })).status).toBe(0);
  });

  it("fails on a kernel that lacks any of them, or has one that must be built in as a module, and names each", () => {
    const file = config({ ...whole("arm64"), VIRTIO_BLK: "m", EXT4_FS: "unset", PCI_HOST_GENERIC: "unset", VIRTIO_FS: "unset", HYPERV: "m", HYPERV_STORAGE: "unset" });
    const { DEBUG_INFO_BTF: _btf, ...withoutBtf } = whole("arm64");
    expect(checked("arm64", file)).toMatchObject({
      status: 1,
      stderr: `kernel-config.sh: ${file} does not build in CONFIG_VIRTIO_BLK CONFIG_EXT4_FS CONFIG_PCI_HOST_GENERIC, which the guest boots on without an initrd\n`
        + `kernel-config.sh: ${file} does not build in CONFIG_HYPERV, which the guest boots on with Hyper-V's initrd\n`
        + `kernel-config.sh: ${file} has no CONFIG_VIRTIO_FS, which vm/init loads\n`
        + `kernel-config.sh: ${file} has no CONFIG_HYPERV_STORAGE, which Hyper-V's initrd loads\n`,
    });
    expect(checked("arm64", config(withoutBtf))).toMatchObject({ status: 1, stderr: expect.stringContaining("does not build in CONFIG_DEBUG_INFO_BTF,") });
    // A line that names the option within another's is not it.
    expect(checked("amd64", config({ ...whole("amd64"), BPF_LSM: "unset", BPF_LSM_EXTRA: "y" })).status).toBe(1);
  });

  it("fails on a kernel without any one of them, or with any one that must be built in as a module, and names that one", () => {
    for (const arch of ARCHES) {
      for (const name of Object.keys(whole(arch))) {
        const loaded = LOADED.includes(name) || INITRD.includes(name);
        const said = (file: string) => LOADED.includes(name)
          ? `kernel-config.sh: ${file} has no CONFIG_${name}, which vm/init loads\n`
          : INITRD.includes(name)
            ? `kernel-config.sh: ${file} has no CONFIG_${name}, which Hyper-V's initrd loads\n`
            : hyperv(arch).includes(name)
              ? `kernel-config.sh: ${file} does not build in CONFIG_${name}, which the guest boots on with Hyper-V's initrd\n`
              : `kernel-config.sh: ${file} does not build in CONFIG_${name}, which the guest boots on without an initrd\n`;
        const without = config(Object.fromEntries(Object.entries(whole(arch)).filter(([option]) => option !== name)));
        expect(checked(arch, without), `${arch} without ${name}`).toMatchObject({ status: 1, stderr: said(without) });
        if (loaded) continue;
        const module = config({ ...whole(arch), [name]: "m" });
        expect(checked(arch, module), `${arch} with ${name} a module`).toMatchObject({ status: 1, stderr: said(module) });
      }
    }
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

describe("the modules each part of the guest loads, out of the kernel's tree", () => {
  let tree: string;
  beforeEach(() => {
    tree = mkdtempSync(join(tmpdir(), "modules-"));
    // A kernel's tree as its modules package lays it out: each module's file, zstd's, and modules.dep naming what each needs.
    const modules: Record<string, string[]> = {
      "kernel/fs/netfs/netfs.ko.zst": [], "kernel/net/9p/9pnet.ko.zst": ["kernel/fs/netfs/netfs.ko.zst"],
      "kernel/fs/9p/9p.ko.zst": ["kernel/net/9p/9pnet.ko.zst", "kernel/fs/netfs/netfs.ko.zst"], "kernel/fs/overlayfs/overlay.ko.zst": [],
      "kernel/drivers/hv/hv_utils.ko.zst": [], "kernel/sound/snd.ko.zst": [],
    };
    for (const file of Object.keys(modules)) {
      mkdirSync(dirname(join(tree, file)), { recursive: true });
      writeFileSync(join(tree, file), spawnSync("zstd", ["-q", "-c"], { input: `module ${file}` }).stdout);
    }
    writeFileSync(join(tree, "modules.dep"), Object.entries(modules).map(([file, needs]) => `${file}:${needs.map((need) => ` ${need}`).join("")}\n`).join(""));
  });
  afterEach(() => {
    rmSync(tree, { recursive: true, force: true });
  });
  const modules = (...args: string[]) => spawnSync(join(REPO, "images/guest/modules.sh"), args, { encoding: "utf8" });
  const left = () => spawnSync("find", [join(tree, "kernel"), "-type", "f", "-printf", "%f\\n"], { encoding: "utf8" }).stdout.trim().split("\n").sort();

  it("keeps the modules it is given and none other, each after those it needs", () => {
    expect(modules("keep", tree, "netfs", "9pnet", "9p", "overlay")).toMatchObject({ status: 0, stderr: "" });
    expect(left()).toEqual(["9p.ko.zst", "9pnet.ko.zst", "netfs.ko.zst", "overlay.ko.zst"]);
    expect(readFileSync(join(tree, "modules.dep"), "utf8")).toContain("kernel/fs/9p/9p.ko.zst:");
  });

  it("copies the modules it is given out, as their files", () => {
    const out = join(tree, "out");
    expect(modules("copy", tree, out, "netfs", "9pnet")).toMatchObject({ status: 0, stderr: "" });
    expect(readdirSync(out).sort()).toEqual(["9pnet.ko.zst", "netfs.ko.zst"]);
    expect(readFileSync(join(out, "9pnet.ko.zst")).equals(readFileSync(join(tree, "kernel/net/9p/9pnet.ko.zst")))).toBe(true);
    expect(left()).toHaveLength(6);
  });

  it("fails on a module the tree lacks, and one given before a module it needs or without it, names each, and changes nothing", () => {
    expect(modules("keep", tree, "9p", "netfs", "virtiofs")).toMatchObject({
      status: 1, stderr: "modules.sh: 9p needs 9pnet, which is not given before it\nmodules.sh: 9p needs netfs, which is not given before it\nmodules.sh: the kernel has no module virtiofs\n",
    });
    expect(left()).toHaveLength(6);
    expect(modules("copy", tree, join(tree, "out"), "9pnet")).toMatchObject({ status: 1, stderr: "modules.sh: 9pnet needs netfs, which is not given before it\n" });
    expect(readdirSync(tree)).not.toContain("out");
    // A name that merely holds another's is not it.
    expect(modules("keep", tree, "net")).toMatchObject({ status: 1, stderr: "modules.sh: the kernel has no module net\n" });
    expect(modules("keep", tree).status).toBe(2);
    expect(modules("keep", join(tree, "missing"), "netfs")).toMatchObject({ status: 1, stderr: expect.stringContaining("cannot read") });
  });
});

describe("Hyper-V's initrd", () => {
  let dir: string;
  beforeEach(() => {
    dir = mkdtempSync(join(tmpdir(), "initrd-"));
    mkdirSync(join(dir, "modules"));
    for (const name of ["hv_vmbus", "hv_storvsc"]) writeFileSync(join(dir, "modules", `${name}.ko.zst`), spawnSync("zstd", ["-q", "-c"], { input: `the module ${name}` }).stdout);
    writeFileSync(join(dir, "busybox"), "#!/bin/sh\n# busybox\n");
    writeFileSync(join(dir, "list"), "# its comment\nhv_vmbus\n\nhv_storvsc  # its own\n");
  });
  afterEach(() => {
    rmSync(dir, { recursive: true, force: true });
  });
  const INIT = join(REPO, "images/guest/initrd/init");
  const made = (out: string, list = join(dir, "list")) =>
    spawnSync(join(REPO, "images/guest/initrd.sh"), [INIT, join(dir, "busybox"), join(dir, "modules"), list, out], { encoding: "utf8" });
  // The archive's entries, with their modes and owners, as cpio lists them; and the archive unpacked into *into*.
  const listed = (archive: string) => spawnSync("cpio", ["-t", "-v", "--quiet", "-F", archive], { encoding: "utf8" }).stdout.trim().split("\n");
  const unpacked = (archive: string, into: string) => {
    mkdirSync(into);
    expect(spawnSync("cpio", ["-i", "-d", "--quiet", "-F", archive], { cwd: into }).status).toBe(0);
    return into;
  };

  it("holds its init, busybox, and the modules of its list unpacked, which /modules names in the list's order, all root's", () => {
    expect(made(join(dir, "initrd"))).toMatchObject({ status: 0, stderr: "" });
    const names = listed(join(dir, "initrd")).map((line) => line.split(/\s+/).at(-1));
    expect(names).toEqual(["bin", "bin/busybox", "dev", "init", "lib", "lib/modules", "lib/modules/hv_storvsc.ko", "lib/modules/hv_vmbus.ko", "modules", "proc", "root", "sys"]);
    for (const line of listed(join(dir, "initrd"))) expect(line).toMatch(/^\S+\s+\d+ root\s+root\s/);
    const root = unpacked(join(dir, "initrd"), join(dir, "root"));
    expect(readFileSync(join(root, "lib/modules/hv_vmbus.ko"), "utf8")).toBe("the module hv_vmbus");
    expect(readFileSync(join(root, "modules"), "utf8")).toBe("hv_vmbus\nhv_storvsc\n");
    expect(readFileSync(join(root, "init")).equals(readFileSync(INIT))).toBe(true);
    expect(statSync(join(root, "init")).mode & 0o777).toBe(0o755);
  });

  it("makes the same bytes of the same inputs, and nothing of a list whose module is missing", () => {
    expect(made(join(dir, "one")).status).toBe(0);
    expect(made(join(dir, "two")).status).toBe(0);
    expect(readFileSync(join(dir, "one")).equals(readFileSync(join(dir, "two")))).toBe(true);
    writeFileSync(join(dir, "missing"), "hv_vmbus\nhv_sock\n");
    expect(made(join(dir, "three"), join(dir, "missing"))).toMatchObject({ status: 1, stderr: `initrd.sh: ${join(dir, "modules")} has no module hv_sock\n` });
    expect(readdirSync(dir)).not.toContain("three");
    expect(readdirSync(dir)).not.toContain("three.partial");
  });

  // A function of the initrd's init, by its name.
  const fnOf = (name: string) => new RegExp(`^${name}\\(\\) \\{\\n[\\s\\S]*?\\n\\}\\n`, "m").exec(readFileSync(INIT, "utf8"))?.[0] ?? "";
  // The image's disk the init's disks() finds, as busybox's shell runs it, with sysfs at *sys* and devtmpfs at *dev*, waiting *tenths*.
  const disks = (sys: string, dev: string, tenths = 10) => {
    const ran = spawnSync("busybox", ["sh", "-euc", `WAIT=${tenths}\n${fnOf("at")}${fnOf("disks")}disks "$1" "$2"`, "sh", sys, dev], { encoding: "utf8" });
    return ran.status === 0 ? { root: ran.stdout.trim() } : { refused: ran.stdout.trim() };
  };
  const scsi = (address: string, name: string) => {
    mkdirSync(join(dir, "sys", "class", "scsi_disk", address, "device", "block", name), { recursive: true });
    mkdirSync(join(dir, "dev"), { recursive: true });
    writeFileSync(join(dir, "dev", name), "");
  };

  it("parses as busybox's shell, loads its list's modules in order, and waits 5 s for the disks", () => {
    expect(spawnSync("busybox", ["sh", "-n", INIT]).status).toBe(0);
    const init = readFileSync(INIT, "utf8");
    expect(init.startsWith("#!/bin/busybox sh\n")).toBe(true);
    expect(init).toMatch(/^WAIT=50$/m);
    expect(init).toContain('while read -r module; do\n  insmod "/lib/modules/$module.ko" || fail "the kernel did not load $module"\ndone < /modules');
    expect(init).toContain('root=$(disks /sys /dev) || fail "$root"');
    expect(init.trim().split("\n").at(-1)).toBe("exec switch_root /root /usr/sbin/surogate-init");
  });

  it("takes the image's disk at LUN 0 once the agent disk and the sessions disk are there too", () => {
    scsi("0:0:0:0", "sdc");
    scsi("0:0:0:1", "sda");
    scsi("0:0:0:2", "sdb");
    expect(disks(join(dir, "sys"), join(dir, "dev"))).toEqual({ root: join(dir, "dev", "sdc") });
  });

  it("fails with words when a disk does not come in time, or two are at one LUN", () => {
    scsi("0:0:0:0", "sda");
    scsi("0:0:0:1", "sdb");
    const started = performance.now();
    expect(disks(join(dir, "sys"), join(dir, "dev"))).toEqual({ refused: "no disk came at LUN 2 within 1 s" });
    expect(performance.now() - started).toBeGreaterThan(900);
    scsi("0:0:0:2", "sdc");
    scsi("1:0:0:1", "sdd");
    expect(disks(join(dir, "sys"), join(dir, "dev"))).toEqual({ refused: "two SCSI disks are at LUN 1" });
  });
});
