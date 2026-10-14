# Builds the Windows app's MSI (windows/msi/Surogate.wxs) from its layout (scripts/package-win.sh) and the MSI's own
# custom action (native/windows/msi, surogate_msi.dll), once the release has signed every program of both, with the
# WiX Toolset v7 as a .NET tool and its Util and Firewall extensions, each by its version and the sha256 of its package
# as nuget.org serves it, fetched for this build alone into a folder of its own. It runs WiX and nothing else: no
# secret is needed, and the job that runs it has none. The MSI it writes is unsigned: the release signs it.
#
#   powershell -File desktop\scripts\msi-win.ps1 <x.y.z> <layout> <custom action folder> <out.msi>
#
# The version is the release's, which an MSI holds as major.minor.build: its first two parts at most 255, its third at
# most 65535 (Microsoft Learn, "ProductVersion property"). A pin moves in one change: the version and each sha256.
param(
  [Parameter(Mandatory)][string] $Version,
  [Parameter(Mandatory)][string] $Layout,
  [Parameter(Mandatory)][string] $CustomAction,
  [Parameter(Mandatory)][string] $Out
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Wix = '7.0.0'
$WixSha256 = '7f992e57c356dcbda2ea961bf3b348e1bd7d31d96795be4ac391e04cf140536d'
$Extensions = [ordered]@{
  'WixToolset.Util.wixext' = '6431ef896ddaff08d21221e67de98a3597fa94fb0557372cb96feb4edae0c23e'
  'WixToolset.Firewall.wixext' = 'c4b5a22fb53a29c0a78cb8724fd280bf0beddf2266add2f3515b8fdbd2271563'
}

if ($Version -notmatch '^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$' -or [int64]$Matches[1] -gt 255 -or [int64]$Matches[2] -gt 255 -or [int64]$Matches[3] -gt 65535) {
  throw "msi-win.ps1: $Version is not a version an MSI holds: x.y.z, x and y at most 255, z at most 65535"
}
foreach ($file in (Join-Path $Layout 'Surogate.exe'), (Join-Path $CustomAction 'surogate_msi.dll')) {
  if (-not (Test-Path -PathType Leaf $file)) { throw "msi-win.ps1: $file is missing" }
}
$Layout = (Resolve-Path $Layout).Path
$CustomAction = (Resolve-Path $CustomAction).Path
$Out = [IO.Path]::GetFullPath($Out)

function Sha256([string] $Path) { (Get-FileHash -Algorithm SHA256 $Path).Hash.ToLowerInvariant() }

$work = Join-Path ([IO.Path]::GetTempPath()) "surogate-wix-$([Guid]::NewGuid().ToString('N'))"
New-Item -ItemType Directory -Path $work | Out-Null
try {
  # The tool, from nuget.org, by its version; then the package dotnet kept of it, by its sha256.
  $tools = Join-Path $work 'tools'
  & dotnet tool install wix --version $Wix --tool-path $tools | Out-Host
  if ($LASTEXITCODE -ne 0) { throw "msi-win.ps1: dotnet could not install WiX $Wix" }
  $package = Join-Path $tools ".store\wix\$Wix\wix\$Wix\wix.$Wix.nupkg"
  if ((Sha256 $package) -ne $WixSha256) { throw "msi-win.ps1: WiX $Wix's package is not the pinned one" }

  # Each extension's package, by its sha256, and its one assembly, which the build is given by its path.
  Add-Type -AssemblyName System.IO.Compression.FileSystem
  $given = foreach ($extension in $Extensions.Keys) {
    $id = $extension.ToLowerInvariant()
    $nupkg = Join-Path $work "$id.$Wix.nupkg"
    Invoke-WebRequest -UseBasicParsing -Uri "https://api.nuget.org/v3-flatcontainer/$id/$Wix/$id.$Wix.nupkg" -OutFile $nupkg
    if ((Sha256 $nupkg) -ne $Extensions[$extension]) { throw "msi-win.ps1: $extension $Wix's package is not the pinned one" }
    $unpacked = Join-Path $work $id
    [IO.Compression.ZipFile]::ExtractToDirectory($nupkg, $unpacked)
    '-ext'
    Join-Path $unpacked "wixext7\$extension.dll"
  }

  # From the source's own folder, where its own files are, by the names it gives them.
  New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Out) | Out-Null
  Push-Location (Join-Path $PSScriptRoot '..\windows\msi')
  try {
    & (Join-Path $tools 'wix.exe') build -acceptEula wix7 -arch x64 @given -d "Version=$Version" -bindpath "Layout=$Layout" -bindpath "Msi=$CustomAction" `
      -pdbtype none -o $Out Surogate.wxs | Out-Host
    if ($LASTEXITCODE -ne 0) { throw "msi-win.ps1: WiX exited $LASTEXITCODE" }
  } finally {
    Pop-Location
  }
} finally {
  Remove-Item -Recurse -Force $work -ErrorAction SilentlyContinue
}
$Out
