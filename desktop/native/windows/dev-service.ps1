# Installs a development build of the Surogate VM service (surogate-service.exe) by hand, elevated, for the VM test
# day on a PC (desktop/test/win32/WINDOWS-TEST-DAY.md). It is the MSI's ServiceInstall in miniature: the service as
# LocalSystem, demand start, with a security descriptor that lets interactive users start it, so a measurement run as
# an ordinary user can reach it. It also grants the VM group read on the development agent disk, which the service
# does not grant (the installed MSI does this for the app's own agent disk).
#
# Run elevated, from desktop/native/windows:
#   powershell -ExecutionPolicy Bypass -File dev-service.ps1            # build and install
#   powershell -ExecutionPolicy Bypass -File dev-service.ps1 -Remove    # stop and remove
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
if (-not $AgentDisk) { $AgentDisk = Join-Path $here "..\..\dist\agent.vhd" }

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Run this in an elevated PowerShell: it installs a Windows service."
}

function Remove-Service {
    $existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
    if ($existing) {
        if ($existing.Status -ne "Stopped") { Stop-Service -Name $ServiceName -Force -ErrorAction SilentlyContinue }
        sc.exe delete $ServiceName | Out-Null
        Write-Host "Removed the $ServiceName service."
    } else {
        Write-Host "The $ServiceName service was not installed."
    }
}

if ($Remove) {
    Remove-Service
    return
}

# Build the service and surogate-vm, pinned, release.
$workspace = $here
Write-Host "Building the Rust programs (cargo build --locked --release)..."
cargo build --locked --release --manifest-path (Join-Path $workspace "Cargo.toml") -p service -p vm
$exe = Join-Path $workspace "target\release\surogate-service.exe"
if (-not (Test-Path $exe)) { throw "surogate-service.exe was not built at $exe" }

Remove-Service

# Demand start as LocalSystem. binPath points at the built exe where it is.
Write-Host "Installing the $ServiceName service as LocalSystem, demand start..."
sc.exe create $ServiceName binPath= "`"$exe`"" start= demand obj= LocalSystem DisplayName= "Surogate VM (development)" | Out-Null
sc.exe failure $ServiceName reset= 86400 actions= restart/5000 | Out-Null

# Let interactive users start and stop it, as the MSI's descriptor does, so measurements run as an ordinary user can
# reach it: SY and BA full, IU start/stop/read.
$sddl = "D:(A;;CCLCSWRPWPDTLOCRRC;;;SY)(A;;CCLCSWRPWPDTLOCRRC;;;BA)(A;;CCLCSWLORC;;;IU)(A;;RPWP;;;IU)S:(AU;FA;CCDCLCSWRPWPDTLOCRSDRCWDWO;;;WD)"
sc.exe sdset $ServiceName $sddl | Out-Null

# The VM group (S-1-5-83-0, "NT VIRTUAL MACHINE\Virtual Machines") reads the development agent disk, since the
# service grants the user's own files but not this one (the installed MSI grants the app's agent disk at install).
if (Test-Path $AgentDisk) {
    icacls $AgentDisk /grant "*S-1-5-83-0:(R)" | Out-Null
    Write-Host "Granted the VM group read on $AgentDisk."
} else {
    Write-Host "No agent disk at $AgentDisk yet; grant the VM group read on it before the first boot."
}

Write-Host "Installed. It starts on demand when surogate-vm.exe asks for a VM, and stops itself when idle."
