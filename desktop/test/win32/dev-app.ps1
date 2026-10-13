# A development build of the app as a Windows app of its own, for the checks a person makes on a PC by hand: a shortcut
# in the user's Start menu, "Surogate Dev", to this package's Electron on this checkout's dist\shell\main.js, with the
# development build's AppUserModelID and toast activator, without which Windows shows none of its toasts. Its code is
# this checkout's dist\, as `npm run build` built it. The app takes its surogate:// links for this user itself, at its
# start, and its login item is its own, as Settings sets it.
#
#   test\win32\dev-app.ps1           make the shortcut, and start the app
#   test\win32\dev-app.ps1 -Remove   quit the app, take out its login item and its links, and remove the shortcut
param([switch]$Remove)
$ErrorActionPreference = "Stop"

$desktop = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$electron = Join-Path $desktop "node_modules\electron\dist\electron.exe"
$main = Join-Path $desktop "dist\shell\main.js"
# As src/shell/app-identity-win32.ts names a development build.
$id = "ai.invergent.surogate.development"
$activator = "{73E63950-1345-41A7-A15E-AE7A7F851999}"
$shortcut = Join-Path ([Environment]::GetFolderPath("Programs")) "Surogate Dev.lnk"
$run = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
$approved = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run"
$links = "HKCU:\Software\Classes\surogate"

# This checkout's app, running: its Electron's processes on its main.
function Get-DevApp {
  Get-CimInstance Win32_Process -Filter "Name = 'electron.exe'" |
    Where-Object { $_.ExecutablePath -eq $electron -and $_.CommandLine -like "*$main*" }
}

if ($Remove) {
  if (Test-Path $electron) {
    # Asked as its own Quit asks, through its questions: a launch with --quit hands that to the running app, and quits
    # at once where none runs.
    Start-Process -FilePath $electron -ArgumentList "`"$main`" --quit" -Wait
    $until = (Get-Date).AddSeconds(60)
    while ((Get-DevApp) -and (Get-Date) -lt $until) { Start-Sleep -Milliseconds 250 }
    if (Get-DevApp) {
      Write-Error "Surogate Dev is still running: answer its question, or quit it from its icon in the notification area, then run this again."
    }
  }
  # Its login item: Electron's value in the user's Run key, named by its AppUserModelID, and its Startup apps switch.
  foreach ($key in @($run, $approved)) { Remove-ItemProperty -Path $key -Name $id -ErrorAction SilentlyContinue }
  # Its links, where they still start this checkout's Electron.
  $command = (Get-ItemProperty -Path "$links\shell\open\command" -ErrorAction SilentlyContinue).'(default)'
  if ($command -and $command.StartsWith("`"$electron`"")) { Remove-Item -Path $links -Recurse }
  Remove-Item -Path $shortcut -ErrorAction SilentlyContinue
  "Removed $shortcut"
  exit 0
}

if (-not (Test-Path $main)) { Write-Error "Build it first: npm run build" }
if (-not (Test-Path $electron)) { Write-Error "Install Electron first: npm ci, then npm run electron:install" }
$made = Start-Process -FilePath $electron -ArgumentList "`"$PSScriptRoot\dev-shortcut.cjs`" `"$shortcut`" `"$main`" $id $activator" -Wait -PassThru
if ($made.ExitCode -ne 0) { Write-Error "Windows did not make $shortcut" }
"Made $shortcut"
Start-Process -FilePath $electron -ArgumentList "`"$main`""
