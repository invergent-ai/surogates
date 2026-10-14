// A raw disk made a fixed VHD (src/vm/vhd.ts): the footer hcsshim's makeFixedVHDFooter writes, field by field, each
// against the values hcsshim's code gives it (ext4/tar2ext4/vhdfooter.go at bf079cd, its struct as encoding/binary
// writes it, big-endian and packed), but its id, the disk's hash, and its time, zero; given once, at the disk's end,
// with the disk's bytes before it unchanged; and the script the workflows give the agent disk its footer with.

import { spawnSync } from "node:child_process";
import { createHash, randomBytes } from "node:crypto";
import { mkdtempSync, readFileSync, rmSync, statSync, truncateSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { addFooter, endsInFooter, FOOTER_SIZE, vhdFooter } from "../src/vm/vhd.js";
import { built } from "./built.js";

const sha256 = (data: Buffer) => createHash("sha256").update(data).digest("hex");
// The sha256 of nothing: its first 16 bytes are the id.
const EMPTY = sha256(Buffer.alloc(0));
const MIB = 1024 * 1024;
const SCRIPT = fileURLToPath(new URL("../scripts/vhd-footer.mjs", import.meta.url));

let dir: string;
beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "vhd-"));
});
afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

describe("the footer", () => {
  it("is 512 bytes, each field where hcsshim's struct has it, with the values makeFixedVHDFooter gives it", () => {
    const footer = vhdFooter(16 * MIB, EMPTY);
    expect(FOOTER_SIZE).toBe(512);
    expect(footer.length).toBe(512);
    // Cookie [8]byte, "conectix".
    expect(footer.subarray(0, 8).toString("latin1")).toBe("conectix");
    // Features, featureMask: the reserved bit, which is always set.
    expect(footer.readUInt32BE(8)).toBe(0x2);
    // FileFormatVersion, fileFormatVersionMagic.
    expect(footer.readUInt32BE(12)).toBe(0x0001_0000);
    // DataOffset, fixedDataOffset: a fixed disk has no header past its footer.
    expect(footer.readBigInt64BE(16)).toBe(-1n);
    expect(footer.subarray(16, 24).toString("hex")).toBe("ff".repeat(8));
    // TimeStamp: zero, as hcsshim leaves it, so the file is the same whenever it is made.
    expect(footer.readUInt32BE(24)).toBe(0);
    // CreatorApplication [4]byte: none.
    expect(footer.subarray(28, 32).toString("hex")).toBe("00000000");
    // CreatorVersion, creatorVersionMagic.
    expect(footer.readUInt32BE(32)).toBe(0x000a_0000);
    // CreatorHostOS [4]byte: none.
    expect(footer.subarray(36, 40).toString("hex")).toBe("00000000");
    // OriginalSize and CurrentSize: the disk's bytes before the footer.
    expect(footer.readBigInt64BE(40)).toBe(BigInt(16 * MIB));
    expect(footer.readBigInt64BE(48)).toBe(BigInt(16 * MIB));
    // DiskGeometry: none, as hcsshim leaves it.
    expect(footer.readUInt32BE(56)).toBe(0);
    // DiskType, diskTypeFixed.
    expect(footer.readUInt32BE(60)).toBe(2);
    // UniqueID [16]uint8: the first 16 bytes of the disk's sha256, where hcsshim's are random.
    expect(footer.subarray(68, 84).toString("hex")).toBe(EMPTY.slice(0, 32));
    // SavedState, then Reserved [427]uint8: zero.
    expect(footer.subarray(84).equals(Buffer.alloc(428))).toBe(true);
  });

  it("is checksummed as calculateCheckSum does: the ones' complement of the sum of its bytes, its checksum's zero", () => {
    const footer = vhdFooter(16 * MIB, EMPTY);
    // Worked out by hand from vhdfooter.go: "conectix" 861, the features 2, the version 1, the data offset 8 × 255,
    // the creator's version 10, each size's one byte of 1, the disk type 2; then the id's bytes.
    const id = [...Buffer.from(EMPTY.slice(0, 32), "hex")].reduce((sum, byte) => sum + byte, 0);
    expect(861 + 2 + 1 + 8 * 255 + 10 + 1 + 1 + 2 + id).toBe(5369);
    expect(footer.readUInt32BE(64)).toBe(0xffff_eb06);
    expect(footer.readUInt32BE(64)).toBe(~5369 >>> 0);
    const zeroed = Buffer.from(footer);
    zeroed.writeUInt32BE(0, 64);
    expect(~[...zeroed].reduce((sum, byte) => sum + byte, 0) >>> 0).toBe(footer.readUInt32BE(64));
  });

  it("is, byte for byte, what hcsshim's struct is with these sizes and this id, past 4 GiB as below it", () => {
    // encoding/binary's big-endian writing of vhdFooter, worked out apart from this code; past its first 96 bytes, zeros.
    const golden = {
      [16 * MIB]: "636f6e65637469780000000200010000ffffffffffffffff0000000000000000000a000000000000000000000100000000000000010000000000000000000002ffffeb06e3b0c44298fc1c149afbf4c8996fb924000000000000000000000000",
      [5 * 1024 * MIB]: "636f6e65637469780000000200010000ffffffffffffffff0000000000000000000a000000000000000000014000000000000001400000000000000000000002ffffea86e3b0c44298fc1c149afbf4c8996fb924000000000000000000000000",
    };
    for (const [size, hex] of Object.entries(golden)) {
      const footer = vhdFooter(Number(size), EMPTY);
      expect(footer.subarray(0, 96).toString("hex"), size).toBe(hex);
      expect(footer.subarray(96).equals(Buffer.alloc(416)), size).toBe(true);
    }
  });

  it("is the same for the same disk on every computer, and another for another disk", () => {
    const other = sha256(Buffer.from("another disk"));
    expect(vhdFooter(16 * MIB, EMPTY).equals(vhdFooter(16 * MIB, EMPTY))).toBe(true);
    expect(vhdFooter(16 * MIB, other).subarray(68, 84).toString("hex")).toBe(other.slice(0, 32));
    expect(vhdFooter(16 * MIB, other).equals(vhdFooter(16 * MIB, EMPTY))).toBe(false);
  });

  it("is made only for a disk of whole 512-byte sectors, and only from a sha256", () => {
    for (const size of [0, -512, 512.5, 1000, 16 * MIB + 1, Number.MAX_SAFE_INTEGER + 1]) {
      expect(() => vhdFooter(size, EMPTY), String(size)).toThrow(`a disk of ${size} bytes is not a whole number of 512-byte sectors`);
    }
    for (const hash of ["", EMPTY.slice(0, 63), `${EMPTY.slice(0, 63)}g`, EMPTY.toUpperCase()]) {
      expect(() => vhdFooter(512, hash), hash).toThrow(`${hash} is not a sha256`);
    }
    expect(vhdFooter(512, EMPTY).readBigInt64BE(48)).toBe(512n);
  });
});

describe("a disk given its footer", () => {
  const disk = () => join(dir, "agent.img");
  // A disk's bytes: data, then zeros, then data, in whole sectors.
  const raw = () => Buffer.concat([randomBytes(4096), Buffer.alloc(MIB), randomBytes(8192)]);

  it("ends in the footer of its bytes, which are as they were", async () => {
    const bytes = raw();
    writeFileSync(disk(), bytes);
    expect(await endsInFooter(disk())).toBe(false);
    await addFooter(disk());
    const now = readFileSync(disk());
    expect(now.length).toBe(bytes.length + 512);
    expect(now.subarray(0, bytes.length).equals(bytes)).toBe(true);
    expect(now.subarray(bytes.length).equals(vhdFooter(bytes.length, sha256(bytes)))).toBe(true);
    expect(await endsInFooter(disk())).toBe(true);
  });

  it("takes its bytes' hash on the word of who knows it, as the image's manifest says it", async () => {
    const bytes = raw();
    writeFileSync(disk(), bytes);
    // A hash the bytes do not have is the one the footer is made from: the caller's word is taken.
    const told = sha256(Buffer.from("told"));
    await addFooter(disk(), told);
    expect(readFileSync(disk()).subarray(bytes.length).equals(vhdFooter(bytes.length, told))).toBe(true);
    expect(await endsInFooter(disk(), told)).toBe(true);
    expect(await endsInFooter(disk())).toBe(false);
    expect(await endsInFooter(disk(), sha256(bytes))).toBe(false);
  });

  it("is never given a second, whether its hash is known or worked out", async () => {
    const bytes = raw();
    writeFileSync(disk(), bytes);
    await addFooter(disk());
    const once = readFileSync(disk());
    await addFooter(disk());
    await addFooter(disk(), sha256(bytes));
    expect(readFileSync(disk()).equals(once)).toBe(true);
  });

  it("does not end in one where its last sector is another disk's footer, another size's, or a broken one", async () => {
    const bytes = raw();
    for (const end of [
      vhdFooter(bytes.length, sha256(Buffer.from("another disk"))),
      vhdFooter(bytes.length - 512, sha256(bytes)),
      (() => {
        const broken = vhdFooter(bytes.length, sha256(bytes));
        broken[100] = 1;
        return broken;
      })(),
    ]) {
      writeFileSync(disk(), Buffer.concat([bytes, end]));
      expect(await endsInFooter(disk())).toBe(false);
    }
  });

  it("is not taken for one by a file too short to hold one, or that is not whole sectors before its last", async () => {
    for (const size of [0, 100, 512, 512 + 100]) {
      writeFileSync(disk(), randomBytes(size));
      expect(await endsInFooter(disk()), String(size)).toBe(false);
    }
  });

  it("is refused one where it is not whole sectors, and left as it was", async () => {
    writeFileSync(disk(), randomBytes(1000));
    await expect(addFooter(disk())).rejects.toThrow("a disk of 1000 bytes is not a whole number of 512-byte sectors");
    expect(statSync(disk()).size).toBe(1000);
  });

  it("stays sparse where its zeros are a hole", async () => {
    writeFileSync(disk(), randomBytes(4096));
    truncateSync(disk(), 64 * MIB);
    await addFooter(disk());
    expect(statSync(disk()).size).toBe(64 * MIB + 512);
    expect(statSync(disk()).blocks * 512).toBeLessThan(MIB);
  });
});

describe("the script the workflows give a disk its footer with", () => {
  const script = (...args: string[]) => {
    built(fileURLToPath(new URL("../dist/vm/vhd.js", import.meta.url)));
    return spawnSync(process.execPath, [SCRIPT, ...args], { encoding: "utf8" });
  };

  it("gives the disk it names its footer through the app's own code, once however often it is run", () => {
    const disk = join(dir, "agent.img");
    const bytes = Buffer.concat([randomBytes(4096), Buffer.alloc(MIB)]);
    writeFileSync(disk, bytes);
    for (let n = 0; n < 2; n += 1) {
      expect(script(disk)).toMatchObject({ status: 0, stdout: "", stderr: "" });
      expect(readFileSync(disk).equals(Buffer.concat([bytes, vhdFooter(bytes.length, sha256(bytes))]))).toBe(true);
    }
  });

  it("says how it is used, and why a disk cannot be given one", () => {
    for (const args of [[], ["a", "b"]]) expect(script(...args)).toMatchObject({ status: 2, stdout: "", stderr: "usage: node scripts/vhd-footer.mjs <disk>\n" });
    writeFileSync(join(dir, "short.img"), randomBytes(1000));
    expect(script(join(dir, "short.img"))).toMatchObject({
      status: 1, stdout: "", stderr: `vhd-footer.mjs: ${join(dir, "short.img")}: a disk of 1000 bytes is not a whole number of 512-byte sectors\n`,
    });
  });
});
