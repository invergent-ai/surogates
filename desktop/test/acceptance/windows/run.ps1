# What a script can check of Surogate Desktop's acceptance on a Windows PC (README.md beside it), as the account the
# acceptance is run in. Each check says ok or FAIL; the script ends 1 once all have run if any failed, and writes what it
# found to acceptance-<phase>-<computer>-<user>.txt in the current folder, to send back.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File run.ps1 -Phase before|installed|removed
#
#   before     the PC, before anything is installed: Windows's version, the processor, virtualization, the volumes
#   installed  after the MSI and its restart: the app, its signatures, its service, its folders and their owners
#   removed    after Remove Surogate from This Computer: nothing of the app is left, and the folders it worked on are
param([Parameter(Mandatory)][ValidateSet('before', 'installed', 'removed')][string] $Phase)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Installed = Join-Path $env:ProgramFiles 'Surogate'
$Data = Join-Path $env:ProgramData 'Surogate'
$Mine = Join-Path $env:LOCALAPPDATA 'Surogate'
$Out = Join-Path (Get-Location) "acceptance-$Phase-$env:COMPUTERNAME-$env:USERNAME.txt"
$said = [Collections.Generic.List[string]]::new()
$failed = [Collections.Generic.List[string]]::new()

function Say([string] $Line) { $said.Add($Line); $Line }
function Check([string] $What, [bool] $Holds, [string] $Found = '') {
  if ($Holds) { Say "ok    $What" } else { $failed.Add($What); Say "FAIL  $What$(if ($Found) { ": $Found" })" }
}

# Who, but SYSTEM, the Administrators and Windows's own servicing, may change *path* by its access rules.
function Get-Writers([string] $Path) {
  $changes = [Security.AccessControl.FileSystemRights]'Write, Delete, DeleteSubdirectoriesAndFiles, ChangePermissions, TakeOwnership'
  @((Get-Acl $Path).Access | Where-Object {
    $_.AccessControlType -eq 'Allow' -and ($_.FileSystemRights -band $changes) -and -not ($_.PropagationFlags -band [Security.AccessControl.PropagationFlags]::InheritOnly) -and
    $_.IdentityReference.Value -notin 'NT AUTHORITY\SYSTEM', 'BUILTIN\Administrators', 'NT SERVICE\TrustedInstaller', 'OWNER RIGHTS'
  } | ForEach-Object { "$($_.IdentityReference) $($_.FileSystemRights)" })
}

function Get-Product { @(Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*' -ErrorAction SilentlyContinue | Where-Object { $_.PSObject.Properties['DisplayName'] -and $_.DisplayName -eq 'Surogate' }) }

$os = Get-CimInstance Win32_OperatingSystem
$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
$admin = [Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
Say "Windows $($os.Version) build $($os.BuildNumber), $($os.Caption); $($cpu.Name); account $env:USERNAME, $(if ($admin) { 'an administrator, elevated' } else { 'not elevated' })"

switch ($Phase) {
  'before' {
    $hypervisor = (Get-CimInstance Win32_ComputerSystem).HypervisorPresent
    Say "virtualization in the firmware: $($cpu.VirtualizationFirmwareEnabled); a hypervisor running: $hypervisor"
    $vmp = try { (Get-WindowsOptionalFeature -Online -FeatureName VirtualMachinePlatform).State.ToString() } catch { "not readable as this account: $_" }
    Say "Virtual Machine Platform: $vmp"
    Say "short names on the system volume: $((fsutil 8dot3name query $env:SystemDrive | Out-String).Trim())"
    $documents = [Environment]::GetFolderPath('MyDocuments')
    Say "Documents: $documents$(if ($documents -match 'OneDrive') { ' (in OneDrive)' })"
    Check 'Windows 11, on x64' ([int]$os.BuildNumber -ge 22000 -and $env:PROCESSOR_ARCHITECTURE -eq 'AMD64') "$($os.BuildNumber) $env:PROCESSOR_ARCHITECTURE"
    Check 'Surogate is not installed yet' (@(Get-Product).Count -eq 0 -and -not (Test-Path $Installed))
  }
  'installed' {
    $product = @(Get-Product)
    Check 'Installed apps lists Surogate once, by Surogate' ($product.Count -eq 1 -and $product[0].Publisher -eq 'Surogate') "$(@($product | ForEach-Object { "$($_.DisplayVersion) by $($_.Publisher)" }) -join '; ')"
    if ($product.Count -eq 1) { Say "version: $($product[0].DisplayVersion)" }
    $vmp = try { (Get-WindowsOptionalFeature -Online -FeatureName VirtualMachinePlatform).State.ToString() } catch { '' }
    if ($vmp) { Check 'Virtual Machine Platform is on' ($vmp -eq 'Enabled') $vmp }
    $service = Get-Service -Name SurogateVM -ErrorAction SilentlyContinue
    Check 'the Surogate VM service is installed, started on demand' ($service -and $service.StartType -eq 'Manual') "$(if ($service) { "$($service.Status) $($service.StartType)" } else { 'missing' })"
    # Every program of the install signed by one signer, whom Windows trusts; node.exe by its own maker.
    foreach ($program in @(Get-ChildItem -Recurse -File -Include *.exe, *.dll, *.node $Installed -ErrorAction SilentlyContinue)) {
      $signature = Get-AuthenticodeSignature $program.FullName
      $name = $program.FullName.Substring($Installed.Length + 1)
      if ($signature.Status -ne 'Valid') { Check "$name is signed, and Windows trusts its signature" $false "$($signature.Status)" }
    }
    $signers = @(Get-ChildItem -Recurse -File -Include *.exe $Installed -ErrorAction SilentlyContinue | Where-Object { $_.Name -ne 'node.exe' } | ForEach-Object { (Get-AuthenticodeSignature $_.FullName).SignerCertificate.Subject } | Sort-Object -Unique)
    Check "every program of the app is signed by one signer: $($signers -join '; ')" ($signers.Count -eq 1)
    foreach ($folder in $Installed, (Join-Path $Installed 'bin'), (Join-Path $Installed 'service'), $Data, (Join-Path $Data 'apply')) {
      if (Test-Path $folder) {
        $writers = @(Get-Writers $folder)
        Check "only administrators may change $folder" ($writers.Count -eq 0) ($writers -join '; ')
      } else {
        Check "$folder is there" $false
      }
    }
    $record = Join-Path $Data 'install.json'
    $text = if (Test-Path $record) { Get-Content -Raw $record } else { '' }
    Check 'the install record names where updates come from, their channel and the version installed' ($text -match '"base":"https://' -and $text -match '"channel":"' -and $text -match '"version":"') $text
    $writersOfRecord = @(if (Test-Path $record) { Get-Writers $record })
    Check 'only administrators may change the install record' ((Test-Path $record) -and $writersOfRecord.Count -eq 0) ($writersOfRecord -join '; ')
    $scheme = Get-ItemProperty 'HKLM:\Software\Classes\surogate\shell\open\command' -ErrorAction SilentlyContinue
    Check 'surogate:// links open the installed app' ($scheme -and $scheme.'(default)' -eq "`"$Installed\Surogate.exe`" -- `"%1`"") "$(if ($scheme) { $scheme.'(default)' })"
    $shortcut = Join-Path ([Environment]::GetFolderPath('CommonPrograms')) 'Surogate.lnk'
    Check 'the Start menu has Surogate' (Test-Path $shortcut)
  }
  'removed' {
    Check 'Installed apps no longer lists Surogate' (@(Get-Product).Count -eq 0)
    foreach ($place in $Installed, $Data, 'HKLM:\Software\Surogate', 'HKLM:\Software\Classes\surogate') { Check "$place is gone" (-not (Test-Path $place)) }
    Check 'the Surogate VM service is gone' (-not (Get-Service -Name SurogateVM -ErrorAction SilentlyContinue))
    Check 'the Start menu no longer has Surogate' (-not (Test-Path (Join-Path ([Environment]::GetFolderPath('CommonPrograms')) 'Surogate.lnk')))
    $rules = @(Get-NetFirewallRule -ErrorAction SilentlyContinue | Where-Object { $_.DisplayName -like "Surogate's file tools*" })
    Check "the app's firewall rules are gone" ($rules.Count -eq 0) "$(@($rules | ForEach-Object DisplayName) -join '; ')"
    $run = Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -ErrorAction SilentlyContinue
    $starts = @(if ($run) { $run.PSObject.Properties | Where-Object { "$($_.Value)" -match 'Surogate\.exe' } | ForEach-Object Name })
    Check "nothing starts Surogate when $env:USERNAME signs in" ($starts.Count -eq 0) ($starts -join '; ')
    Say "this account's data, $Mine: $(if (Test-Path $Mine) { 'kept, as asked' } else { 'deleted, as asked' })"
  }
}

$said | Set-Content -Encoding utf8 $Out
"what was found is in $Out"
if ($failed.Count) {
  "run.ps1: $($failed.Count) checks failed: $($failed -join '; ')"
  exit 1
}
