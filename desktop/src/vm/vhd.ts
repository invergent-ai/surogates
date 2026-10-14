// A raw disk file made a fixed VHD, the form in which Hyper-V opens a disk: the 512-byte footer that Microsoft's
// Virtual Hard Disk Image Format Specification puts at a fixed disk's end, after its bytes, which stay as they were.
// Ported from hcsshim (github.com/microsoft/hcsshim, at bf079cd), which makes its rootfs.vhd so:
// ext4/tar2ext4/vhdfooter.go:9-68, makeFixedVHDFooter and calculateCheckSum, and tar2ext4.go:347-369, ConvertToVhd
// and ConvertFileToVhd. Its fields are hcsshim's, in its struct's order and big-endian, but for its id: hcsshim's is
// random (vhdfooter.go:70-76), and this one is the disk's sha256's first 16 bytes. Its time is zero, as hcsshim's is.
// So a disk is the same file on every computer, and its footer follows from its size and its hash alone.

import { statSync } from "node:fs";
import { open } from "node:fs/promises";

import { hashOf } from "../download.js";

/** A footer's length: the file's last 512 bytes. */
export const FOOTER_SIZE = 512;
const SECTOR = 512;
const SHA256 = /^[0-9a-f]{64}$/;

// vhdfooter.go:10-17.
const COOKIE = "conectix";
const FEATURES = 0x2; // featureMask: the reserved bit, which the specification says is always set
// FileFormatVersion 1.0, its major and minor version each a 16-bit half (hcsshim's 0x00010000).
const FILE_FORMAT_MAJOR = 1;
const FILE_FORMAT_MINOR = 0;
const FIXED_DATA_OFFSET = -1n; // a fixed disk has no dynamic disk header
const CREATOR_VERSION = 0x000a_0000;
const DISK_TYPE_FIXED = 2;

/**
 * The footer of a fixed disk of *size* bytes, whose sha256 is *sha256* in hex: the 512 bytes after them. Its fields at
 * the offsets of hcsshim's vhdFooter struct (vhdfooter.go:19-36); those it never sets, and the time, zero.
 */
export function vhdFooter(size: number, sha256: string): Buffer {
  if (!Number.isSafeInteger(size) || size <= 0 || size % SECTOR !== 0) throw new Error(`a disk of ${size} bytes is not a whole number of 512-byte sectors`);
  if (!SHA256.test(sha256)) throw new Error(`${sha256} is not a sha256`);
  const footer = Buffer.alloc(FOOTER_SIZE);
  footer.write(COOKIE, 0, "latin1"); // Cookie
  footer.writeUInt32BE(FEATURES, 8); // Features
  footer.writeUInt16BE(FILE_FORMAT_MAJOR, 12); // FileFormatVersion, its major half
  footer.writeUInt16BE(FILE_FORMAT_MINOR, 14); // and its minor half
  footer.writeBigInt64BE(FIXED_DATA_OFFSET, 16); // DataOffset
  // TimeStamp (24) and CreatorApplication (28) stay zero.
  footer.writeUInt32BE(CREATOR_VERSION, 32); // CreatorVersion
  // CreatorHostOS (36) stays zero.
  footer.writeBigInt64BE(BigInt(size), 40); // OriginalSize
  footer.writeBigInt64BE(BigInt(size), 48); // CurrentSize
  // DiskGeometry (56) stays zero.
  footer.writeUInt32BE(DISK_TYPE_FIXED, 60); // DiskType
  Buffer.from(sha256, "hex").copy(footer, 68, 0, 16); // UniqueID
  // SavedState (84) and Reserved (85) stay zero.
  // Checksum: the ones' complement of the sum of every byte, its own zero (calculateCheckSum).
  let sum = 0;
  for (const byte of footer) sum += byte;
  footer.writeUInt32BE(~sum >>> 0, 64);
  return footer;
}

/**
 * Whether *path* ends in the footer of its bytes before it, as addFooter gives one. *sha256* is those bytes' hash on
 * the word of who knows it, as the image's manifest says it; without it they are hashed.
 */
export async function endsInFooter(path: string, sha256?: string): Promise<boolean> {
  const size = statSync(path).size - FOOTER_SIZE;
  if (size <= 0 || size % SECTOR !== 0) return false;
  const end = Buffer.alloc(FOOTER_SIZE);
  const handle = await open(path, "r");
  try {
    await handle.read(end, 0, FOOTER_SIZE, size);
  } finally {
    await handle.close();
  }
  return end.equals(vhdFooter(size, sha256 ?? (await hashOf(path, size)).digest("hex")));
}

/**
 * *path*, a raw disk, made a fixed VHD: its footer written after its bytes, unless it ends in it already, so that no
 * disk has two. *sha256* is its bytes' hash on the word of who knows it; without it they are hashed. A hole stays one.
 */
export async function addFooter(path: string, sha256?: string): Promise<void> {
  if (await endsInFooter(path, sha256)) return;
  const size = statSync(path).size;
  const footer = vhdFooter(size, sha256 ?? (await hashOf(path)).digest("hex"));
  const handle = await open(path, "r+");
  try {
    await handle.write(footer, 0, FOOTER_SIZE, size);
  } finally {
    await handle.close();
  }
}
