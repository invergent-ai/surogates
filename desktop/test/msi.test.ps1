# The Windows app's MSI (windows/msi/Surogate.wxs), installed, checked and removed on this computer, as an
# administrator on a computer where Virtual Machine Platform is on, as the Windows runner is: on such a computer the MSI
# leaves the feature on and asks for no restart. What a computer without the feature does, its restart among it, is
# measured by hand, on a PC without the feature, at the test day. Each check says ok or FAIL; the script fails once all
# have run if any failed. Before the first install, a standard user of this computer (made here, and removed at the
# end) makes Surogate's folders of ProgramData, as any user may, with files of theirs in them; before another, a link to
# a folder of theirs stands in the folder's place. With -Newer, an MSI of a later version made from the same layout: it
# upgrades the first in place, and the first, installed over it, takes its place again, each keeping what the install
# before it was given.
#
#   pwsh -File desktop\test\msi.test.ps1 -Msi <msi> -Version <x.y.z> -Layout <its layout> [-Newer <msi> -NewerVersion <x.y.z>]
param(
  [Parameter(Mandatory)][string] $Msi,
  [Parameter(Mandatory)][string] $Version,
  [Parameter(Mandatory)][string] $Layout,
  [string] $Newer,
  [string] $NewerVersion
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
# By their whole paths: Windows Installer's service opens a package from its own folder, not the caller's.
$Msi = (Resolve-Path $Msi).Path
$Layout = (Resolve-Path $Layout).Path
if ($Newer) { $Newer = (Resolve-Path $Newer).Path }

$Installed = Join-Path $env:ProgramFiles 'Surogate'
$Data = Join-Path $env:ProgramData 'Surogate'
$Scheme = 'HKLM:\Software\Classes\surogate'
$Shortcut = Join-Path ([Environment]::GetFolderPath('CommonPrograms')) 'Surogate.lnk'
$Rules = "Surogate's file tools: node", "Surogate's file tools: rg"
$Remembered = 'HKLM:\Software\Surogate'
$Elsewhere = Join-Path $env:SystemDrive 'SurogateElsewhere'
$Squatter = 'surogate-squatter'
$Victim = Join-Path $env:SystemDrive 'SurogateSquatted'
$Logs = Join-Path ([IO.Path]::GetTempPath()) "surogate-msi-$([Guid]::NewGuid().ToString('N'))"
New-Item -ItemType Directory -Path $Logs | Out-Null
$failed = [Collections.Generic.List[string]]::new()

function Check([string] $What, [bool] $Holds, [string] $Said = '') {
  if ($Holds) { "ok    $What" } else { "FAIL  $What$(if ($Said) { ": $Said" })"; $failed.Add($What) }
}

# msiexec, waited for, with a verbose log of its own: its exit code and the log's text.
function Msiexec([string] $Name, [string] $Arguments) {
  $log = Join-Path $Logs "$Name.log"
  $run = Start-Process -FilePath (Join-Path $env:SystemRoot 'System32\msiexec.exe') -ArgumentList "$Arguments /qn /norestart /l*v `"$log`"" -Wait -PassThru
  # Its log's encoding by its byte order mark, which Windows Installer writes.
  [pscustomobject]@{ Code = $run.ExitCode; Log = $(if (Test-Path $log) { Get-Content -Raw $log } else { '' }) }
}

# The installed app's version, as its package names it; none where it is not there.
function Get-AppVersion {
  $package = Join-Path $Installed 'resources\app\package.json'
  if (Test-Path $package) { (Get-Content -Raw $package | ConvertFrom-Json).version } else { '' }
}

function Get-ServiceState { $service = Get-Service -Name SurogateVM -ErrorAction SilentlyContinue; if ($service) { $service.Status.ToString() } else { 'missing' } }

function Get-Product { @(Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\*' -ErrorAction SilentlyContinue | Where-Object { $_.PSObject.Properties['DisplayName'] -and $_.DisplayName -eq 'Surogate' }) }
function Get-Vmp { (Get-WindowsOptionalFeature -Online -FeatureName VirtualMachinePlatform).State.ToString() }
function Get-Rule([string] $Name) { Get-NetFirewallRule -DisplayName $Name -ErrorAction SilentlyContinue }

# The rights *sddl* gives the trustee *sid*, by its ACEs that allow: each two-letter right of them.
function Get-Granted([string] $Sddl, [string] $Sid) {
  $rights = foreach ($ace in [regex]::Matches($Sddl, '\(A;[^;]*;([^;]*);;;([^)]*)\)')) { if ($ace.Groups[2].Value -eq $Sid) { $ace.Groups[1].Value } }
  (($rights -join '') -split '(..)' | Where-Object { $_ }) | Sort-Object -Unique
}

# Who, but SYSTEM, administrators and the trusted installer, may write or change *path* by its access rules.
function Get-Writers([string] $Path) {
  if (-not (Test-Path $Path)) { return "$Path is missing" }
  $writing = [Security.AccessControl.FileSystemRights]'WriteData, AppendData, WriteExtendedAttributes, WriteAttributes, Delete, DeleteSubdirectoriesAndFiles, ChangePermissions, TakeOwnership'
  $trusted = 'NT AUTHORITY\SYSTEM', 'BUILTIN\Administrators', 'NT SERVICE\TrustedInstaller', 'CREATOR OWNER'
  (Get-Acl $Path).Access | Where-Object {
    $_.AccessControlType -eq 'Allow' -and $trusted -notcontains $_.IdentityReference.Value -and ($_.FileSystemRights -band $writing) -ne 0
  } | ForEach-Object { "$($_.IdentityReference) $($_.FileSystemRights)" }
}

# The names in one list and not the other, a few of them, each with the side it is on.
function Get-Unlike([string[]] $Left, [string[]] $Right) {
  $l = [Collections.Generic.HashSet[string]]::new([string[]]@($Left), [StringComparer]::OrdinalIgnoreCase)
  $r = [Collections.Generic.HashSet[string]]::new([string[]]@($Right), [StringComparer]::OrdinalIgnoreCase)
  @(@($Left | Where-Object { -not $r.Contains($_) } | ForEach-Object { "layout only: $_" }) + @($Right | Where-Object { -not $l.Contains($_) } | ForEach-Object { "installed only: $_" }))
}

# Every file of *top*, by its path from it.
function Get-Files([string] $Top) {
  $at = (Resolve-Path $Top).Path.TrimEnd('\').Length + 1
  Get-ChildItem -Path $Top -Recurse -File | ForEach-Object { $_.FullName.Substring($at) } | Sort-Object
}

# What the shell says of the shortcut: its target, and its AppUserModelID and toast activator, as Windows reads them.
function Get-ShortcutFacts {
  try {
    $link = (New-Object -ComObject WScript.Shell).CreateShortcut($Shortcut)
    $item = (New-Object -ComObject Shell.Application).NameSpace((Split-Path -Parent $Shortcut)).ParseName((Split-Path -Leaf $Shortcut))
    [pscustomobject]@{
      Target = $link.TargetPath
      Id = "$($item.ExtendedProperty('System.AppUserModel.ID'))"
      Activator = "$($item.ExtendedProperty('System.AppUserModel.ToastActivatorCLSID'))"
    }
  } catch {
    [pscustomobject]@{ Target = "unread: $_"; Id = "unread: $_"; Activator = "unread: $_" }
  }
}

# Nothing of the app's left on the computer: its folders, its service, its scheme, its shortcut, its rules, its record.
function Check-Gone([string] $When) {
  Check "$When, no install folder" (-not (Test-Path $Installed))
  Check "$When, no %ProgramData%\Surogate" (-not (Test-Path $Data))
  Check "$When, no Surogate VM service" (-not (Get-Service -Name SurogateVM -ErrorAction SilentlyContinue))
  Check "$When, no surogate:// scheme" (-not (Test-Path $Scheme))
  Check "$When, no Start menu shortcut" (-not (Test-Path $Shortcut))
  foreach ($rule in $Rules) { Check "$When, no firewall rule $rule" (-not (Get-Rule $rule)) }
  Check "$When, no entry in Installed apps" (@(Get-Product).Count -eq 0)
  Check "$When, nothing remembered under HKLM\Software\Surogate" (-not (Test-Path $Remembered))
}

# The access rules *path* has of its own, each "<who> <rights> <inheritance>", in order of their words; whether any is
# inherited is said as "inherited".
function Get-Rules([string] $Path) {
  if (-not (Test-Path $Path)) { return @("$Path is missing") }
  @((Get-Acl $Path).Access | ForEach-Object { if ($_.IsInherited) { 'inherited' } else { "$($_.IdentityReference) $($_.AccessControlType) $($_.FileSystemRights) $($_.InheritanceFlags)" } } | Sort-Object)
}

# A folder made as the squatter makes one in ProgramData: theirs, with every right of theirs, and *files* of theirs in it,
# each read-only and then theirs alone, so that only its owner may touch it.
function Squat([string] $Path, [string[]] $Files) {
  New-Item -ItemType Directory -Force $Path | Out-Null
  icacls $Path /setowner $Squatter | Out-Null
  icacls $Path /grant "${Squatter}:(OI)(CI)F" | Out-Null
  foreach ($file in $Files) {
    $at = Join-Path $Path $file
    Set-Content -Path $at -Value '{"base":"https://squatter.example.test","channel":"squatted"}'
    (Get-Item $at).Attributes = 'ReadOnly'
    icacls $at /setowner $Squatter | Out-Null
    icacls $at /inheritance:r /grant:r "${Squatter}:F" | Out-Null
  }
}

# The folder *path* as the install leaves it: SYSTEM's or administrators', with exactly *rules* of its own and none
# inherited.
function Check-Taken([string] $What, [string] $Path, [string[]] $Rules) {
  $acl = if (Test-Path $Path) { Get-Acl $Path } else { $null }
  $owner = if ($acl) { $acl.Owner } else { 'missing' }
  Check "$What is SYSTEM's or administrators'" ($owner -in 'NT AUTHORITY\SYSTEM', 'BUILTIN\Administrators') $owner
  $found = @(if ($acl) { Get-Rules $Path })
  Check "$What has exactly its own access rules" ($acl -and $acl.AreAccessRulesProtected -and (($found -join '|') -eq (($Rules | Sort-Object) -join '|'))) ($found -join '; ')
}
$DataRules = 'NT AUTHORITY\SYSTEM Allow FullControl ContainerInherit, ObjectInherit', 'BUILTIN\Administrators Allow FullControl ContainerInherit, ObjectInherit',
  'BUILTIN\Users Allow ReadAndExecute, Synchronize ContainerInherit, ObjectInherit'
$ApplyRules = 'NT AUTHORITY\SYSTEM Allow FullControl ContainerInherit, ObjectInherit', 'BUILTIN\Administrators Allow FullControl ContainerInherit, ObjectInherit'

# The record's base and channel, as one line of JSON; nothing where there is none.
function Get-Record {
  $record = Join-Path $Data 'install.json'
  if (Test-Path $record) { Get-Content -Raw $record | ConvertFrom-Json | ConvertTo-Json -Compress } else { '' }
}

$vmp = Get-Vmp
Check 'Virtual Machine Platform is on before the install, as on the runner' ($vmp -eq 'Enabled') $vmp
Check-Gone 'before the install'

# Surogate's folders of ProgramData made first by a standard user, with files of theirs: a library in apply, a file
# beside the record, and a record of theirs.
# Of 14 characters, past which net user asks whether to go on.
$password = "$([Guid]::NewGuid().ToString('N').Substring(0, 10))aA1!"
net user $Squatter $password /add | Out-Null
Check 'a standard user is made to squat the folders' ($LASTEXITCODE -eq 0) "net user exited $LASTEXITCODE"
Squat $Data @('planted.txt', 'install.json')
Squat (Join-Path $Data 'apply') @('wintrust.dll')
Check 'the squatter owns both folders before the install' ((Get-Acl $Data).Owner -like "*\$Squatter" -and (Get-Acl (Join-Path $Data 'apply')).Owner -like "*\$Squatter") "$((Get-Acl $Data).Owner)"

$install = Msiexec 'install' "/i `"$Msi`""
Check 'it installs, and asks for no restart' ($install.Code -eq 0) "msiexec exited $($install.Code)"
Check 'its custom action left Virtual Machine Platform on' ($install.Log -match "Surogate: Windows's Virtual Machine Platform is on" -and (Get-Vmp) -eq 'Enabled') (Get-Vmp)

# The app, every file of its layout as it is there, in %ProgramFiles%\Surogate, which only administrators write.
$theirs = @(Get-Files $Layout)
$ours = @(if (Test-Path $Installed) { Get-Files $Installed })
$unlike = @(Get-Unlike $theirs $ours)
Check 'every file of the layout is installed, and nothing else' ($theirs.Count -gt 0 -and $unlike.Count -eq 0) "$($unlike.Count): $(($unlike | Select-Object -First 5) -join '; ')"
$differ = @($theirs | Where-Object { (Test-Path (Join-Path $Installed $_)) -and (Get-FileHash (Join-Path $Layout $_)).Hash -ne (Get-FileHash (Join-Path $Installed $_)).Hash })
Check 'each installed file is the layout''s' ($differ.Count -eq 0) "$(($differ | Select-Object -First 5) -join '; ')"
Check 'the app is the version installed' ((Get-AppVersion) -eq $Version) (Get-AppVersion)
foreach ($folder in $Installed, (Join-Path $Installed 'service'), (Join-Path $Installed 'bin'), (Join-Path $Installed 'resources\app\bin')) {
  $writers = @(Get-Writers $folder)
  Check "only administrators write $folder" ($writers.Count -eq 0) ($writers -join '; ')
}

# The agent disk, a fixed VHD where the service lets a VM read it, with no access rule of its own: each VM reads it as
# Users do, through the install folder's.
$disk = Join-Path $Installed 'resources\vm\agent.vhd'
$cookie = if (Test-Path $disk) { $bytes = [IO.File]::ReadAllBytes($disk); [Text.Encoding]::ASCII.GetString($bytes, $bytes.Length - 512, 8) } else { '' }
Check 'the agent disk is a fixed VHD at resources\vm\agent.vhd' ($cookie -eq 'conectix') $cookie
$diskAcl = if (Test-Path $disk) { Get-Acl $disk } else { $null }
$own = @(if ($diskAcl) { $diskAcl.Access | Where-Object { -not $_.IsInherited } | ForEach-Object { "$($_.IdentityReference) $($_.FileSystemRights)" } } else { 'missing' })
Check 'the agent disk has no access rule of its own' ($own.Count -eq 0) ($own -join '; ')
$readers = @(if ($diskAcl) { $diskAcl.Access | Where-Object { $_.AccessControlType -eq 'Allow' -and $_.IdentityReference.Value -eq 'BUILTIN\Users' -and ($_.FileSystemRights -band [Security.AccessControl.FileSystemRights]::ReadData) } })
Check 'Users read the agent disk, as each VM does' ($readers.Count -gt 0) "$(if ($diskAcl) { $diskAcl.Access | Out-String })"

# The service: LocalSystem, demand start, its program the installed one; interactive users may start it and do no more;
# started again once if it fails; not running.
$config = sc.exe qc SurogateVM | Out-String
Check 'the service runs as LocalSystem' ($config -match 'SERVICE_START_NAME\s*:\s*LocalSystem') $config
Check 'the service starts on demand' ($config -match 'START_TYPE\s*:\s*3\s+DEMAND_START') $config
Check 'the service is the installed program' ($config -match [regex]::Escape("`"$Installed\service\surogate-service.exe`"")) $config
$sddl = (sc.exe sdshow SurogateVM | Out-String).Trim()
$interactive = @(Get-Granted $sddl 'IU')
Check 'interactive users may start the service, and query it' ($interactive -contains 'RP' -and $interactive -contains 'LC') $sddl
Check 'interactive users may not stop, change or remove the service' (-not ($interactive | Where-Object { $_ -in 'WP', 'DC', 'SD', 'WD', 'WO', 'DT' })) $sddl
foreach ($sid in 'WD', 'AU', 'BU', 'S-1-5-83-0') { Check "the service gives $sid no right" (@(Get-Granted $sddl $sid).Count -eq 0) $sddl }
$failure = sc.exe qfailure SurogateVM | Out-String
Check 'the service is started again once after it fails' ($failure -match 'RESTART -- Delay = 5000' -and $failure -match 'RESET_PERIOD \(in seconds\)\s*:\s*86400') $failure
Check 'the service is not running' ((Get-ServiceState) -eq 'Stopped') (Get-ServiceState)

# The surogate:// scheme, machine-wide, starting the installed app with the link after the end of its switches.
$key = Get-ItemProperty -Path $Scheme -ErrorAction SilentlyContinue
$open = Get-ItemProperty -Path "$Scheme\shell\open\command" -ErrorAction SilentlyContinue
$command = if ($open) { $open.'(default)' } else { '' }
Check 'surogate:// is a URL scheme' ($key -and $key.'(default)' -eq 'URL:Surogate' -and $key.PSObject.Properties['URL Protocol'] -and $key.'URL Protocol' -eq '')
Check 'surogate:// opens the installed app with the link' ($command -eq "`"$Installed\Surogate.exe`" -- `"%1`"") $command

# The Start menu's shortcut, to the installed app, not advertised, with the app's AppUserModelID and toast activator.
$facts = Get-ShortcutFacts
Check 'the Start menu shortcut starts the installed app, and is not advertised' ($facts.Target -eq "$Installed\Surogate.exe") $facts.Target
Check 'the shortcut carries the app''s AppUserModelID' ($facts.Id -eq 'ai.invergent.surogate') $facts.Id
Check 'the shortcut carries the app''s toast activator' ($facts.Activator.Trim('{}') -eq '6E271D8E-0539-4AF2-8B3D-57150B0C7113') $facts.Activator

# The two outbound rules that block the file tools' programs.
foreach ($pair in @(@($Rules[0], 'resources\app\bin\node.exe'), @($Rules[1], 'resources\app\bin\rg.exe'))) {
  $rule = Get-Rule $pair[0]
  $program = if ($rule) { ($rule | Get-NetFirewallApplicationFilter).Program } else { '' }
  Check "the firewall rule $($pair[0]) blocks every connection out of $($pair[1])" ($rule -and $rule.Direction -eq 'Outbound' -and $rule.Action -eq 'Block' -and $rule.Enabled -eq 'True' -and $program -eq (Join-Path $Installed $pair[1])) "$($rule | Format-List DisplayName, Direction, Action, Enabled, Profile | Out-String) $program"
}

# The install record, of the public base and channel where the command line gave none, where only administrators and
# SYSTEM write; and apply\, which only they reach. Both folders the squatter made are the install's, and what they put
# in them is gone, but the record, which is SYSTEM's and the install's own.
$record = Join-Path $Data 'install.json'
Check 'with no base or channel given, the record holds the public ones' ((Get-Record) -eq '{"base":"https://surogate.ai","channel":"stable"}') (Get-Record)
Check-Taken '%ProgramData%\Surogate, which the squatter made,' $Data $DataRules
Check-Taken '%ProgramData%\Surogate\apply, which the squatter made,' (Join-Path $Data 'apply') $ApplyRules
Check 'what the squatter put in either folder is gone' (-not (Test-Path (Join-Path $Data 'planted.txt')) -and -not (Test-Path (Join-Path $Data 'apply\wintrust.dll'))) "$(Get-ChildItem -Force -Recurse $Data -ErrorAction SilentlyContinue | ForEach-Object FullName)"
$recordOwner = if (Test-Path $record) { (Get-Acl $record).Owner } else { 'missing' }
Check 'the record is SYSTEM''s or administrators'', with no access rule of its own' ($recordOwner -in 'NT AUTHORITY\SYSTEM', 'BUILTIN\Administrators' -and @(Get-Rules $record | Where-Object { $_ -ne 'inherited' }).Count -eq 0) "$recordOwner; $((Get-Rules $record) -join '; ')"
$dataAcl = if (Test-Path $Data) { Get-Acl $Data } else { $null }
Check '%ProgramData%\Surogate inherits nothing, and only administrators and SYSTEM write it' ($dataAcl -and $dataAcl.AreAccessRulesProtected -and @(Get-Writers $Data).Count -eq 0 -and @(Get-Writers $record).Count -eq 0) "$(if ($dataAcl) { $dataAcl.Access | Out-String } else { 'missing' })"
$apply = Join-Path $Data 'apply'
$applyAcl = if (Test-Path $apply) { Get-Acl $apply } else { $null }
$others = @(if ($applyAcl) { $applyAcl.Access | Where-Object { 'NT AUTHORITY\SYSTEM', 'BUILTIN\Administrators' -notcontains $_.IdentityReference.Value } } else { 'missing' })
Check '%ProgramData%\Surogate\apply grants administrators and SYSTEM alone' ($applyAcl -and $applyAcl.AreAccessRulesProtected -and $others.Count -eq 0) ($others | Out-String)
$product = @(Get-Product)
Check 'Installed apps lists Surogate of this version, by Surogate' ($product.Count -eq 1 -and $product[0].DisplayVersion -eq $Version -and $product[0].Publisher -eq 'Surogate') "$($product | Format-List DisplayName, DisplayVersion, Publisher | Out-String)"

# Started, as an interactive user's surogate-vm.exe starts it; the uninstall stops it, then removes everything.
$started = try { if ((Get-ServiceState) -ne 'missing') { Start-Service SurogateVM }; '' } catch { "$_" }
Check 'the service starts' ((Get-ServiceState) -eq 'Running') "$(Get-ServiceState) $started"
$remove = Msiexec 'remove' "/x `"$Msi`""
Check 'it uninstalls, the service running, and asks for no restart' ($remove.Code -eq 0) "msiexec exited $($remove.Code)"
Check 'its custom action does not run at the uninstall' ($remove.Log -notmatch "Surogate: Windows's Virtual Machine Platform")
Check-Gone 'after the uninstall'
Check 'Virtual Machine Platform is still on after the uninstall' ((Get-Vmp) -eq 'Enabled') (Get-Vmp)

# Another folder for the app, by the command line, is refused, and nothing is installed anywhere.
foreach ($given in "INSTALLFOLDER=`"$Elsewhere\`"", "TARGETDIR=`"$Elsewhere\`"") {
  $elsewhere = Msiexec "elsewhere-$($given.Split('=')[0])" "/i `"$Msi`" $given"
  Check "an install given $($given.Split('=')[0]) is refused, in words" ($elsewhere.Code -ne 0 -and $elsewhere.Log -match 'Surogate installs into its own folder of Program Files alone') "msiexec exited $($elsewhere.Code)"
  Check "an install given $($given.Split('=')[0]) installs nothing" (-not (Test-Path $Elsewhere) -and -not (Test-Path $Installed) -and @(Get-Product).Count -eq 0)
}

# A link in the data folder's place, to a folder of the squatter's: removed and not followed, and the folder the install's
# own. With SUROGATE_VMP=0, as a policy that turns the feature on itself would give, and a base and channel of its own:
# each remembered, and kept by every later install that gives none.
New-Item -ItemType Directory -Force $Victim | Out-Null
icacls $Victim /setowner $Squatter | Out-Null
$victimSddl = (Get-Acl $Victim).Sddl
New-Item -ItemType Junction -Path $Data -Target $Victim | Out-Null
$again = Msiexec 'install-given' "/i `"$Msi`" SUROGATE_VMP=0 SUROGATE_BASE=https://mirror.example.test SUROGATE_CHANNEL=beta"
Check 'it installs with SUROGATE_VMP=0 and a base and channel of its own' ($again.Code -eq 0) "msiexec exited $($again.Code)"
Check 'with SUROGATE_VMP=0 the custom action does not run' ($again.Log -match 'Skipping action: EnableVirtualMachinePlatform \(condition is false\)' -and $again.Log -notmatch "Surogate: Windows's Virtual Machine Platform") ''
Check 'the install record holds the base and channel given' ((Get-Record) -eq '{"base":"https://mirror.example.test","channel":"beta"}') (Get-Record)
$made = Get-Item $Data -Force -ErrorAction SilentlyContinue
Check 'a link in the data folder''s place is removed, and the folder is one' ($made -and $made.PSIsContainer -and -not ($made.Attributes -band [IO.FileAttributes]::ReparsePoint)) "$(if ($made) { $made.Attributes } else { 'missing' })"
Check 'the folder the link named is as it was, and holds nothing of the install''s' ((Get-Acl $Victim).Sddl -eq $victimSddl -and @(Get-ChildItem -Force $Victim).Count -eq 0) "$(Get-ChildItem -Force $Victim | ForEach-Object Name)"
Check-Taken '%ProgramData%\Surogate, made where the link was,' $Data $DataRules
$kept = Get-ItemProperty $Remembered -ErrorAction SilentlyContinue
Check 'the base, the channel and SUROGATE_VMP are remembered' ($kept -and $kept.Base -eq 'https://mirror.example.test' -and $kept.Channel -eq 'beta' -and $kept.VirtualMachinePlatform -eq '0') "$($kept | Format-List Base, Channel, VirtualMachinePlatform | Out-String)"

if ($Newer) {
  # Each with no property: what the install before it was given is kept, the feature's policy too.
  foreach ($step in @(@('upgrade', $Newer, $NewerVersion, 'a newer MSI upgrades it in place'), @('downgrade', $Msi, $Version, 'an older MSI replaces the newer'))) {
    $run = Msiexec $step[0] "/i `"$($step[1])`""
    $product = @(Get-Product)
    # The two are made of one layout: Installed apps, the service and the app's program say which is installed.
    Check $step[3] ($run.Code -eq 0 -and $product.Count -eq 1 -and $product[0].DisplayVersion -eq $step[2] -and (Get-Service SurogateVM -ErrorAction SilentlyContinue) -and (Test-Path (Join-Path $Installed 'Surogate.exe'))) "msiexec exited $($run.Code); $(@($product | ForEach-Object DisplayVersion) -join ', ')"
    Check "the $($step[0]) keeps the base and channel given before" ((Get-Record) -eq '{"base":"https://mirror.example.test","channel":"beta"}') (Get-Record)
    Check "the $($step[0]) keeps SUROGATE_VMP=0, and runs no DISM" ($run.Log -match 'Skipping action: EnableVirtualMachinePlatform \(condition is false\)' -and $run.Log -notmatch "Surogate: Windows's Virtual Machine Platform") ''
  }
}
$last = Msiexec 'remove-again' "/x `"$Msi`""
Check 'it uninstalls again' ($last.Code -eq 0) "msiexec exited $($last.Code)"
Check-Gone 'after the last uninstall'

net user $Squatter /delete | Out-Null
Remove-Item -Recurse -Force $Victim -ErrorAction SilentlyContinue
"msiexec's logs: $Logs"
if ($failed.Count) { throw "msi.test.ps1: $($failed.Count) checks failed: $($failed -join '; ')" }
