// A disk given its VHD footer (src/vm/vhd.ts), as Windows itself opens one: mounted read-only by Mount-DiskImage,
// which needs no Hyper-V role, as a fixed VHD of the size its footer says, and dismounted; and the same disk raw
// refused. On Windows alone, as an administrator, as the runner is: Windows mounts a VHD for no other.

import { spawnSync } from "node:child_process";
import { randomBytes } from "node:crypto";
import { mkdtempSync, realpathSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { describe, expect, it } from "vitest";

import { addFooter } from "../src/vm/vhd.js";

const POWERSHELL = join(process.env.SystemRoot ?? "C:\\Windows", "System32", "WindowsPowerShell", "v1.0", "powershell.exe");
// Windows PowerShell's own modules, Storage's among them: a PowerShell 7 that started the tests puts its own first.
const ENV = Object.fromEntries(Object.entries(process.env).filter(([name]) => name.toUpperCase() !== "PSMODULEPATH"));
const MIB = 1024 * 1024;

const powershell = (script: string) =>
  spawnSync(POWERSHELL, ["-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", `$ErrorActionPreference = 'Stop'; ${script}`], { encoding: "utf8", env: ENV, timeout: 60_000 });

// *disk* mounted read-only, what Windows says of it as a disk image and as a disk, and dismounted.
const mounted = (disk: string) => powershell(`
  Mount-DiskImage -ImagePath '${disk}' -Access ReadOnly -NoDriveLetter | Out-Null
  try {
    $image = Get-DiskImage -ImagePath '${disk}'
    $disk = $image | Get-Disk
    [pscustomobject]@{ attached = $image.Attached; storageType = $image.StorageType; size = $image.Size; readOnly = $disk.IsReadOnly; diskSize = $disk.Size } | ConvertTo-Json -Compress
  } finally {
    Dismount-DiskImage -ImagePath '${disk}' | Out-Null
  }`);

describe.runIf(process.platform === "win32")("a disk with its VHD footer on Windows", { timeout: 120_000 }, () => {
  it("is mounted read-only as a fixed VHD of its size, which the same disk raw is not, and is dismounted", async () => {
    const dir = realpathSync.native(mkdtempSync(join(tmpdir(), "vhd-")));
    // Named as a VHD, as Windows takes a disk image's kind from its name.
    const disk = join(dir, "fixture.vhd");
    try {
      writeFileSync(disk, Buffer.concat([randomBytes(4096), Buffer.alloc(16 * MIB - 4096)]));
      const raw = mounted(disk);
      expect(raw.status, raw.stdout + raw.stderr).not.toBe(0);
      await addFooter(disk);
      const footed = mounted(disk);
      expect(footed.status, footed.stdout + footed.stderr).toBe(0);
      // StorageType 2: a VHD (MSFT_DiskImage).
      expect(JSON.parse(footed.stdout)).toEqual({ attached: true, storageType: 2, size: 16 * MIB, readOnly: true, diskSize: 16 * MIB });
      const after = powershell(`(Get-DiskImage -ImagePath '${disk}').Attached`);
      expect(after.stdout.trim(), after.stderr).toBe("False");
    } finally {
      powershell(`Dismount-DiskImage -ImagePath '${disk}' -ErrorAction SilentlyContinue | Out-Null`);
      rmSync(dir, { recursive: true, force: true });
    }
  });
});
