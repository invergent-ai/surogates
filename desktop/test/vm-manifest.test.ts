// The guest image's manifest as the app reads it, for this computer's arch: the same on every OS the
// app is built for, so the macOS project runs it too (vitest.config.ts).

import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { imageArch, type ImageManifest, readManifest } from "../src/vm/image.js";

// A manifest as images/guest/build.sh writes it.
const manifest: ImageManifest = {
  key: "a".repeat(64),
  arch: "amd64",
  files: ["initrd", "rootfs.img", "vmlinuz"].map((name, n) => ({
    name, size: 3_000_000 + n, sha256: String(n).repeat(64), download: `${name}.zst`, downloadSize: 1_000_000 + n, downloadSha256: String(n + 2).repeat(64),
  })),
};

let dir: string;
beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "vm-manifest-"));
});
afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

describe("the image's manifest", () => {
  it("reads the manifest build.sh writes, and refuses one that is not JSON, whose names are not the image's files, or whose key is not a hash", () => {
    const path = join(dir, "manifest.json");
    writeFileSync(path, JSON.stringify(manifest));
    expect(readManifest(path, "amd64")).toEqual(manifest);
    for (const bad of [
      { ...manifest, key: "../x" },
      { ...manifest, arch: "x86_64" },
      { key: manifest.key, files: manifest.files },
      { ...manifest, files: [manifest.files[0]] },
      // The image as it was before it had Hyper-V's initrd.
      { ...manifest, files: manifest.files.filter((file) => file.name !== "initrd") },
      { ...manifest, files: manifest.files.map((file) => ({ ...file, download: "../../.bashrc" })) },
      { ...manifest, files: manifest.files.map((file) => (file.name === "vmlinuz" ? { ...file, name: "../vmlinuz" } : file)) },
      { ...manifest, files: manifest.files.map((file) => ({ ...file, size: -1 })) },
    ]) {
      writeFileSync(path, JSON.stringify(bad));
      expect(() => readManifest(path, "amd64")).toThrow("is not the guest image's manifest");
    }
    writeFileSync(path, "{");
    expect(() => readManifest(path, "amd64")).toThrow(`${path} is not the guest image's manifest`);
  });

  it("delivers only the image of this computer's arch: a packaged app whose manifest names another's is refused, and boots none", () => {
    const path = join(dir, "manifest.json");
    writeFileSync(path, JSON.stringify({ ...manifest, arch: "arm64" }));
    expect(() => readManifest(path, "amd64")).toThrow(`${path} is the manifest of the guest image for arm64, and this computer runs amd64's`);
    expect(readManifest(path, "arm64")).toEqual({ ...manifest, arch: "arm64" });
    // This computer's own, by Node's names: the tests' is x64's or arm64's.
    expect([imageArch("x64"), imageArch("arm64"), imageArch("ia32")]).toEqual(["amd64", "arm64", null]);
  });
});
