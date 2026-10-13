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
Said 'Dev Drive' { fsutil devdrv query }
Said 'Defender' { Get-MpComputerStatus | Format-List AMRunningMode, RealTimeProtectionEnabled, IsTamperProtected }

Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
exit 0
