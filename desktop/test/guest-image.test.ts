// The guest image's build (images/sandbox/Dockerfile and images/guest/): its kernel pin, its key's
// inputs, the modules it keeps, and the release's check of its kernel.

import { execFile, spawnSync } from "node:child_process";
import { appendFileSync, copyFileSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { createServer, type Server } from "node:http";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

const DOCKERFILE = readFileSync(new URL("../../images/sandbox/Dockerfile", import.meta.url), "utf8");
// Each stage's text, by its name.
const STAGES = new Map([...DOCKERFILE.matchAll(/^FROM \S+ AS (\S+)\n([\s\S]*?)(?=^FROM |(?![\s\S]))/gm)].map(([, name, body]) => [name, body ?? ""]));

it("fetches every package of the kernel pin from Launchpad by its hash, the kernel's tools too, and none from apt", () => {
  const kernel = /^ARG KERNEL=(\S+)$/m.exec(DOCKERFILE)?.[1] ?? "";
  const abi = kernel.replace(/-generic$/, "");
  // The hardware-enablement series the tools package is named for: 7.0 for 7.0.0-34.
  const series = kernel.split(".").slice(0, 2).join(".");
  const fetched = [...(STAGES.get("kernel") ?? "").matchAll(/^ADD --checksum=sha256:[0-9a-f]{64} \\\n\s+(\S+) \S+$/gm)].map(([, url]) => url ?? "");
  expect(fetched.map((url) => url.replace(/^https:\/\/launchpad\.net\/ubuntu\/\+archive\/primary\/\+files\//, "").split("_")[0]).sort()).toEqual(
    [`linux-hwe-${series}-tools-${abi}`, `linux-image-${kernel}`, `linux-modules-${kernel}`, `linux-tools-${kernel}`],
  );
  // apt would take whatever version it finds now: no stage installs a kernel package from it.
  const fromApt = [...STAGES].filter(([, body]) => /\bapt(-get)? install[^&]*\blinux-/.test(body)).map(([name]) => name);
  expect(fromApt).toEqual([]);
  // Nor is a package fetched any other way: each one is an ADD the build checks by its hash.
  const unpinned = DOCKERFILE.replace(/^ADD --checksum=sha256:[0-9a-f]{64} \\\n\s+\S+ \S+$/gm, "");
  expect([...unpinned.matchAll(/https?:\/\/\S+/g)].map(([url]) => url).filter((url) => /launchpad\.net|\.deb\b/.test(url))).toEqual([]);
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
  // Not spawnSync: the archive is this process's own server, which must answer meanwhile.
  const check = () => new Promise<{ status: number | null; stdout: string; stderr: string }>((resolve) => {
    execFile(join(REPO, "images/guest/kernel-current.sh"), { env: { ...process.env, UBUNTU_SECURITY: archive } }, (error, stdout, stderr) => {
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
    const newer = {
      status: 1,
      stderr: `kernel-current.sh: noble-security carries linux-image-generic-hwe-24.04 7.0.0-38.38~24.04.4, newer than the guest's pin ${pin}: pin it in images/sandbox/Dockerfile\n`,
    };
    packages = index(["linux-image-generic-hwe-24.04", "7.0.0-38.38~24.04.4"]);
    expect(await check()).toMatchObject(newer);
    // An index that lists two versions, the newer first.
    packages = index(["linux-image-generic-hwe-24.04", "7.0.0-38.38~24.04.4"], ["linux-image-generic-hwe-24.04", pin]);
    expect(await check()).toMatchObject(newer);
  });

  it("fails when the archive cannot say", async () => {
    packages = index(["linux-image-generic", "6.8.0-90.91"]);
    expect(await check()).toMatchObject({ status: 1, stderr: "kernel-current.sh: noble-security lists no linux-image-generic-hwe-24.04\n" });
    archive = `${archive}/missing`;
    expect((await check()).status).not.toBe(0);
  });
});
