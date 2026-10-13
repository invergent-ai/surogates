# What this computer's file system does that the file host's walk on Windows meets (src/files/fd-paths-win32.ts), as
# Markdown for a job's summary or a test day's record: whether the volumes make 8.3 short names, whether a folder can
# tell names apart by case, whether a junction and a symlink can be made without an administrator's rights, and
# whether there is a Dev Drive. Every check records what it found, and none fails the script.
#   powershell -File desktop\test\win32\ntfs-facts.ps1 > ntfs-facts.md
$ErrorActionPreference = 'Continue'
Set-StrictMode -Version Latest

function Said([string] $Title, [scriptblock] $Look) {
  "### $Title"
  '```'
  try { & $Look 2>&1 | Out-String -Width 200 } catch { "failed: $_" }
  '```'
}

$work = Join-Path $env:TEMP "ntfs-facts-$PID"
New-Item -ItemType Directory -Force -Path $work | Out-Null

"## The file system of this computer"
Said 'Windows' { $os = Get-CimInstance Win32_OperatingSystem; "$($os.Caption) $($os.Version) build $($os.BuildNumber)" }
Said 'Volumes' { Get-Volume | Where-Object DriveLetter | Format-Table -AutoSize DriveLetter, FileSystem, FileSystemLabel, DriveType, Size }
Said 'Where the temp folder is' { $env:TEMP; (Get-Item $env:TEMP).FullName }
Said '8.3 short names, the system-wide setting' { fsutil 8dot3name query }
foreach ($volume in (Get-Volume | Where-Object { $_.DriveLetter -and $_.FileSystem -in 'NTFS', 'ReFS' })) {
  $letter = "$($volume.DriveLetter):"
  Said "8.3 short names on $letter" { fsutil 8dot3name query $letter }
}
Said 'The short name a new .git folder is given in the temp folder' {
  $git = Join-Path $work '.git'
  New-Item -ItemType Directory -Force -Path $git | Out-Null
  cmd /c dir /x /a "$work"
}
Said 'A folder that tells names apart by case' {
  $dir = Join-Path $work 'cased'
  New-Item -ItemType Directory -Force -Path $dir | Out-Null
  fsutil file queryCaseSensitiveInfo $dir
  fsutil file setCaseSensitiveInfo $dir enable
  fsutil file queryCaseSensitiveInfo $dir
  Set-Content -Path (Join-Path $dir 'a') -Value 'lower'
  Set-Content -Path (Join-Path $dir 'A') -Value 'upper'
  "names in it: $((Get-ChildItem -Force $dir).Name -join ', ')"
}
Said 'This account, and whether it is elevated' {
  whoami
  $admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
  "elevated administrator: $admin"
}
# A Basic User's token, without the Administrators group (runas /trustlevel:0x20000): what it can make.
Said 'A junction and a symlink made with a Basic User token' {
  $target = Join-Path $work 'target'
  New-Item -ItemType Directory -Force -Path $target | Out-Null
  $out = Join-Path $work 'basic.txt'
  $line = "whoami /groups | findstr /i S-1-5-32-544 & mklink /J `"$work\junction`" `"$target`" & mklink /D `"$work\symlink`" `"$target`""
  Set-Content -Path (Join-Path $work 'basic.cmd') -Value "@($line) > `"$out`" 2>&1"
  runas /trustlevel:0x20000 "cmd /c `"$work\basic.cmd`""
  for ($i = 0; $i -lt 30 -and -not (Test-Path $out); $i++) { Start-Sleep -Milliseconds 500 }
  Start-Sleep -Seconds 2
  if (Test-Path $out) { Get-Content $out } else { 'runas started nothing that wrote its answer' }
  "junction made: $(Test-Path (Join-Path $work 'junction')); symlink made: $(Test-Path (Join-Path $work 'symlink'))"
}
Said 'Developer Mode (symlinks without the privilege)' {
  Get-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\AppModelUnlock' -Name AllowDevelopmentWithoutDevLicense -ErrorAction SilentlyContinue
}
# Whether a folder can be made a junction in place, FSCTL_SET_REPARSE_POINT on the folder itself, and with which access:
# the search's pin holds each folder on its way with an entry in it, on the rule that a folder that holds a name takes
# no reparse point. The answer is Win32's error, 0 for taken.
Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
public static class InPlace {
  [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
  static extern SafeFileHandle CreateFileW(string name, uint access, uint share, IntPtr security, uint disposition, uint flags, IntPtr template);
  [DllImport("kernel32.dll", SetLastError = true)]
  static extern bool DeviceIoControl(SafeFileHandle handle, uint code, byte[] input, int inputSize, IntPtr output, int outputSize, out int returned, IntPtr overlapped);
  // FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, OPEN_EXISTING
  const uint FLAGS = 0x02000000 | 0x00200000;
  public static SafeFileHandle Hold(string path, uint share) { return CreateFileW(path, 0x80, share, IntPtr.Zero, 3, FLAGS, IntPtr.Zero); }
  // A mount point's REPARSE_DATA_BUFFER (MS-FSCC 2.1.2.5) to *target*, set on *dir* opened with *access*.
  public static int Junction(string dir, string target, uint access) {
    byte[] sub = System.Text.Encoding.Unicode.GetBytes("\\??\\" + target), print = System.Text.Encoding.Unicode.GetBytes(target);
    int data = 8 + sub.Length + 2 + print.Length + 2;
    byte[] buffer = new byte[8 + data];
    BitConverter.GetBytes(0xA0000003u).CopyTo(buffer, 0);
    BitConverter.GetBytes((ushort)data).CopyTo(buffer, 4);
    BitConverter.GetBytes((ushort)0).CopyTo(buffer, 8);
    BitConverter.GetBytes((ushort)sub.Length).CopyTo(buffer, 10);
    BitConverter.GetBytes((ushort)(sub.Length + 2)).CopyTo(buffer, 12);
    BitConverter.GetBytes((ushort)print.Length).CopyTo(buffer, 14);
    sub.CopyTo(buffer, 16);
    print.CopyTo(buffer, 16 + sub.Length + 2);
    using (SafeFileHandle handle = CreateFileW(dir, access, 7, IntPtr.Zero, 3, FLAGS, IntPtr.Zero)) {
      if (handle.IsInvalid) return -Marshal.GetLastWin32Error();
      int returned;
      return DeviceIoControl(handle, 0x000900A4, buffer, buffer.Length, IntPtr.Zero, 0, out returned, IntPtr.Zero) ? 0 : Marshal.GetLastWin32Error();
    }
  }
}
'@
Said 'A folder made a junction in place (Win32 error, 0 taken; a negative one is the open''s)' {
  $target = Join-Path $work 'target'
  New-Item -ItemType Directory -Force -Path $target | Out-Null
  function Fresh([string] $name) { $dir = Join-Path $work $name; New-Item -ItemType Directory -Force -Path $dir | Out-Null; $dir }
  "empty, opened for FILE_WRITE_ATTRIBUTES alone: $([InPlace]::Junction((Fresh 'w-attributes'), $target, 0x100))"
  "empty, opened for FILE_WRITE_DATA alone: $([InPlace]::Junction((Fresh 'w-data'), $target, 0x2))"
  $pinned = Fresh 'pinned'
  $pin = [InPlace]::Hold($pinned, 3)
  "empty, held by another handle sharing read and write but not delete: $([InPlace]::Junction($pinned, $target, 0x100))"
  $pin.Dispose()
  $readOnly = Fresh 'read-only-share'
  $pin = [InPlace]::Hold($readOnly, 1)
  "empty, held by another handle sharing read alone, opened for FILE_WRITE_DATA: $([InPlace]::Junction($readOnly, $target, 0x2))"
  "empty, held by another handle sharing read alone, opened for FILE_WRITE_ATTRIBUTES: $([InPlace]::Junction($readOnly, $target, 0x100))"
  $pin.Dispose()
  $full = Fresh 'holds-a-name'
  Set-Content -Path (Join-Path $full 'x') -Value 'x'
  "holding a file: $([InPlace]::Junction($full, $target, 0x100))"
  $sub = Fresh 'holds-a-folder'
  New-Item -ItemType Directory -Force -Path (Join-Path $sub 'inner') | Out-Null
  "holding a folder: $([InPlace]::Junction($sub, $target, 0x100))"
}
Said 'Dev Drive' { fsutil devdrv query }
Said 'Defender' { Get-MpComputerStatus | Format-List AMRunningMode, RealTimeProtectionEnabled, IsTamperProtected }

Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
exit 0
