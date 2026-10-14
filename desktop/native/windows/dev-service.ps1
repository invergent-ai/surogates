# Installs a development build of the Surogate VM service (surogate-service.exe) by hand, elevated, for the VM test
# day on a PC (desktop/test/win32/WINDOWS-TEST-DAY.md). It is the MSI's ServiceInstall in miniature: the service as
# LocalSystem, demand start, with a security descriptor that lets interactive users start it, so a measurement run as
# an ordinary user can reach it. It is laid out as the installed app is, in a folder of Program Files that only
# administrators write: the service at service\surogate-service.exe, and the agent disk at resources\vm\agent.vhd,
# the one file of that folder the service takes. Its ACL is the folder's own, which lets every user, and so each VM,
# read it: nothing is granted on it, to a VM or to the group of all VMs.
#
# Run elevated, from desktop/native/windows:
#   powershell -ExecutionPolicy Bypass -File dev-service.ps1            # build and install
#   powershell -ExecutionPolicy Bypass -File dev-service.ps1 -Remove    # stop and remove
# -AgentDisk names the agent disk to install, a fixed VHD (node scripts/vhd-footer.mjs gives one its footer); by default
# the package's own, dist\agent.img.
#
# This is a development aid, never shipped: the release installs the signed service through the MSI.

[CmdletBinding()]
param(
    [switch]$Remove,
    [string]$AgentDisk = ""
)

$ErrorActionPreference = "Stop"
$ServiceName = "SurogateVM"
# The script's own folder, however it was invoked ($PSScriptRoot is not yet set while a param default is bound).
$here = Split-Path -Parent $PSCommandPath
if (-not $AgentDisk) { $AgentDisk = Join-Path $here "..\..\dist\agent.img" }
# The development install, beside where the MSI puts the app (%ProgramFiles%\Surogate), never in it.
$install = Join-Path $env:ProgramFiles "Surogate Dev"
$service = Join-Path $install "service\surogate-service.exe"
$installedDisk = Join-Path $install "resources\vm\agent.vhd"

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this in an elevated PowerShell: it installs a Windows service."
}

# sc.exe, run with *arguments*: what it says, or, where it fails, a stop in its own words and its exit code
# ($ErrorActionPreference stops at a cmdlet's error alone, never at a program's exit code).
function Invoke-Sc([string]$What, [string[]]$Arguments) {
    $said = & sc.exe @Arguments | Out-String
    if ($LASTEXITCODE -ne 0) { throw "$What failed: sc.exe $($Arguments -join ' ') exited $LASTEXITCODE`n$said" }
    $said
}

function Uninstall-DevService {
    $existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($existing) {
        if ($existing.Status -ne "Stopped") { Stop-Service -Name $ServiceName -Force }
        Invoke-Sc "Removing the $ServiceName service" @("delete", $ServiceName) | Out-Null
        # Deleted once its last handle is closed: wait for it, so a service made again after this is not refused.
        $by = (Get-Date).AddSeconds(30)
        while (Get-Service -Name $ServiceName -ErrorAction SilentlyContinue) {
            if ((Get-Date) -gt $by) { throw "The $ServiceName service was marked for removal, and is still there after 30 s." }
            Start-Sleep -Milliseconds 200
        }
        Write-Host "Removed the $ServiceName service."
    } else {
        Write-Host "The $ServiceName service was not installed."
    }
    if (Test-Path $install) {
        Remove-Item -Recurse -Force $install
        Write-Host "Removed $install."
    }
}

if ($Remove) {
    Uninstall-DevService
    return
}

# The agent disk as Hyper-V opens a disk, a fixed VHD: its last 512 bytes are a footer that begins "conectix".
if (-not (Test-Path -PathType Leaf $AgentDisk)) { throw "No agent disk at ${AgentDisk}: name one with -AgentDisk." }
$disk = [IO.File]::OpenRead((Resolve-Path $AgentDisk).Path)
try {
    $cookie = New-Object byte[] 8
    $disk.Seek(-512, [IO.SeekOrigin]::End) | Out-Null
    $read = $disk.Read($cookie, 0, 8)
} finally {
    $disk.Dispose()
}
if ($read -ne 8 -or [Text.Encoding]::ASCII.GetString($cookie) -ne "conectix") {
    throw "$AgentDisk is not a fixed VHD: give it its footer first, from desktop: node scripts/vhd-footer.mjs $AgentDisk"
}

# Build the service and surogate-vm, pinned, release, from the workspace, whose rust-toolchain.toml picks the toolchain.
Write-Host "Building the Rust programs (cargo build --locked --release)..."
Push-Location $here
try {
    & cargo build --locked --release -p service -p vm
    if ($LASTEXITCODE -ne 0) { throw "cargo build failed (exit $LASTEXITCODE)." }
} finally {
    Pop-Location
}
$built = Join-Path $here "target\release\surogate-service.exe"
if (-not (Test-Path $built)) { throw "surogate-service.exe was not built at $built" }

Uninstall-DevService

# Laid out as the installed app is, in Program Files, where only administrators write: the service, and the agent disk.
New-Item -ItemType Directory -Force (Split-Path -Parent $service), (Split-Path -Parent $installedDisk) | Out-Null
Copy-Item -Force $built $service
Copy-Item -Force $AgentDisk $installedDisk
Write-Host "Laid out $service and $installedDisk."

# Demand start as LocalSystem (New-Service's account without a credential), from the development install. Its path is
# quoted, as it has spaces in it: New-Service takes it as it is given, where sc.exe's command line would lose the quotes.
Write-Host "Installing the $ServiceName service as LocalSystem, demand start..."
New-Service -Name $ServiceName -BinaryPathName "`"$service`"" -StartupType Manual -DisplayName "Surogate VM (development)" | Out-Null
$made = Get-CimInstance Win32_Service -Filter "Name = '$ServiceName'"
if ($made.PathName -ne "`"$service`"" -or $made.StartName -ne "LocalSystem") { throw "The $ServiceName service was made as $($made.PathName), run as $($made.StartName)." }
Invoke-Sc "Setting the $ServiceName service's restart on failure" @("failure", $ServiceName, "reset=", "86400", "actions=", "restart/5000") | Out-Null

# Let interactive users start and stop it, as the MSI's descriptor does, so measurements run as an ordinary user can
# reach it: SY and BA full, IU start/stop/read.
$sddl = "D:(A;;CCLCSWRPWPDTLOCRRC;;;SY)(A;;CCLCSWRPWPDTLOCRRC;;;BA)(A;;CCLCSWLORC;;;IU)(A;;RPWP;;;IU)S:(AU;FA;CCDCLCSWRPWPDTLOCRSDRCWDWO;;;WD)"
Invoke-Sc "Setting the $ServiceName service's security descriptor" @("sdset", $ServiceName, $sddl) | Out-Null

Write-Host "Installed. It starts on demand when surogate-vm.exe asks for a VM, and stops itself when idle."
