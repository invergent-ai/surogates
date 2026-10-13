# Builds ntat, the file host's calls relative to a folder it holds open on Windows (src/files/ntat.ts), for x64, at
# dist\native\ntat.node beside the app's code. On Windows, with Visual Studio's C++ build tools:
#   powershell -File desktop\native\ntat\build.ps1
# It links against node.exe's own import library and includes Node's N-API headers, each the app's node's version,
# fetched from nodejs.org and checked against the sha256 pinned here, which nodejs.org's SHASUMS256.txt for that
# version gives. The pin moves with the app's node.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Version = '22.23.3'
$HeadersSha256 = 'eff1a7e67736bdf25db421ddaa613ed605f751a7b9fe07fb473ea880d1348987'
$LibSha256 = 'c243185f2347870cadb637c909bf123fc703dc65dbfa769b17ec45a4b661d782'

$Desktop = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Build = Join-Path $PSScriptRoot 'build'
New-Item -ItemType Directory -Force -Path $Build, (Join-Path $Desktop 'dist\native') | Out-Null

# A file from nodejs.org's folder of this version, kept in build\ once it is the pinned one.
function Fetch([string] $Path, [string] $Sha256, [string] $To) {
  if ((Test-Path $To) -and (Get-FileHash -Algorithm SHA256 $To).Hash -eq $Sha256) { return }
  Invoke-WebRequest -UseBasicParsing -Uri "https://nodejs.org/dist/v$Version/$Path" -OutFile $To
  $got = (Get-FileHash -Algorithm SHA256 $To).Hash
  if ($got -ne $Sha256) { Remove-Item $To; throw "ntat: $Path is not the pinned one ($got)" }
}

$Headers = Join-Path $Build "node-v$Version-headers.tar.gz"
$Lib = Join-Path $Build 'node.lib'
Fetch "node-v$Version-headers.tar.gz" $HeadersSha256 $Headers
Fetch 'win-x64/node.lib' $LibSha256 $Lib
$Include = Join-Path $Build "node-v$Version\include\node"
if (-not (Test-Path (Join-Path $Include 'node_api.h'))) {
  # Windows's own tar, by its path: a shell's that comes first on the PATH, as Git for Windows' GNU tar, reads C: in a
  # path as a host to reach.
  & (Join-Path $env:SystemRoot 'System32\tar.exe') -xzf $Headers -C $Build "node-v$Version/include/node"
  if ($LASTEXITCODE -ne 0) { throw 'ntat: the headers did not unpack' }
}

# Visual Studio's developer shell for x64, from the newest install that has the C++ tools.
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
$vs = & $vswhere -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $vs) { throw 'ntat: no Visual Studio with the C++ build tools' }
& (Join-Path $vs 'Common7\Tools\Launch-VsDevShell.ps1') -Arch amd64 -HostArch amd64 -SkipAutomaticLocation | Out-Null

# N-API's symbols, and libuv's, are node.exe's: delay-loaded, so that the hook in ntat.c finds them in whichever
# program loaded the addon.
$Out = Join-Path $Desktop 'dist\native\ntat.node'
cl /nologo /std:c17 /O2 /W4 /utf-8 /LD "/I$Include" "/Fo$Build\" (Join-Path $PSScriptRoot 'ntat.c') "/Fe$Out" `
  /link $Lib delayimp.lib advapi32.lib /DELAYLOAD:node.exe "/IMPLIB:$Build\ntat.lib"
if ($LASTEXITCODE -ne 0) { throw "ntat: cl exited $LASTEXITCODE" }
