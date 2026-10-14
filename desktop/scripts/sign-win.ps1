# The Windows app's signatures (Security: every program the app ships carries Surogate's, but node.exe, which keeps
# the OpenJS Foundation's). The release signs with Azure Artifact Signing's own action, by the list this script makes;
# a run that has no signing account, and the Windows workflow, sign instead with a certificate made in the run, issued by
# a root made in the run that this computer trusts until the run says otherwise, so that every check here runs as it runs
# on the release's signatures. What such a run signs is published nowhere. PowerShell 7, on Windows, with the Windows
# SDK's signtool, as the runner has them.
#
#   sign-win.ps1 list <folder> <catalog>   every file under <folder> the release signs, a line each, by its path from
#                                          <catalog>'s own folder, as the signing action reads a catalog: every program
#                                          and library but the app's node.exe, and every MSI
#   sign-win.ps1 trial <catalog>           each file of <catalog> signed with this run's certificate, a SHA-256 digest
#                                          and a SHA-256 timestamp; says the subject of the root this computer now trusts
#   sign-win.ps1 verify <folder> [<root>]  each file the release signs, checked: its signature, by Windows's own trust
#                                          (and chained to the root of that subject, where one is named, as a trial's
#                                          are), with a SHA-256 digest and a SHA-256 timestamp, all by one signer; and
#                                          node.exe, where it is, the OpenJS Foundation's
#   sign-win.ps1 untrust <root>            the root of that subject trusted no more
param(
  [Parameter(Mandatory, Position = 0)][ValidateSet('list', 'trial', 'verify', 'untrust')][string] $Command,
  [Parameter(Position = 1)][string] $First,
  [Parameter(Position = 2)][string] $Second
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$Sha256 = '2.16.840.1.101.3.4.2.1'
# A trial's timestamps: a public RFC 3161 service, as the release's are Artifact Signing's own.
$TrialTimestamps = 'http://timestamp.digicert.com'
# The app's node, by the end of its path in the layout (scripts/package-win.sh), wherever the layout is.
$Node = '\resources\app\bin\node.exe'

# The Windows SDK's signtool for x64, its newest.
function Signtool {
  $found = Get-ChildItem -Path "${env:ProgramFiles(x86)}\Windows Kits\10\bin\*\x64\signtool.exe" -ErrorAction SilentlyContinue |
    Where-Object { $_.Directory.Parent.Name -match '^\d+(\.\d+)+$' } | Sort-Object { [version]$_.Directory.Parent.Name } | Select-Object -Last 1
  if (-not $found) { throw 'sign-win.ps1: no signtool in the Windows SDK' }
  $found.FullName
}

# Whether *path* is a Windows program or library, by its header: MZ, and PE\0\0 where the header says.
function Test-Portable([string] $Path) {
  $stream = [IO.File]::OpenRead($Path)
  try {
    $head = [byte[]]::new(4096)
    $read = $stream.Read($head, 0, $head.Length)
    if ($read -lt 64 -or $head[0] -ne 0x4D -or $head[1] -ne 0x5A) { return $false }
    $at = [BitConverter]::ToUInt32($head, 0x3C)
    return $at + 4 -le $read -and $head[$at] -eq 0x50 -and $head[$at + 1] -eq 0x45 -and $head[$at + 2] -eq 0 -and $head[$at + 3] -eq 0
  } finally {
    $stream.Dispose()
  }
}

# Every file under *folder* the release signs, by its whole path.
function Get-Signed([string] $Folder) {
  Get-ChildItem -Path $Folder -Recurse -File | Where-Object {
    -not $_.FullName.Replace('/', '\').EndsWith($Node, [StringComparison]::OrdinalIgnoreCase) -and ($_.Extension -eq '.msi' -or (Test-Portable $_.FullName))
  } | ForEach-Object FullName | Sort-Object
}

# A program's or library's Authenticode signature, as PKCS #7: its certificate table's first entry (Microsoft, "PE
# Format", the attribute certificate table, WIN_CERT_TYPE_PKCS_SIGNED_DATA); null where it has none.
function Get-PeSignature([string] $Path) {
  $bytes = [IO.File]::ReadAllBytes($Path)
  $at = [BitConverter]::ToInt32($bytes, 0x3C)
  $optional = $at + 24
  $directories = $optional + $(if ([BitConverter]::ToUInt16($bytes, $optional) -eq 0x20B) { 112 } else { 96 })
  $offset = [BitConverter]::ToInt32($bytes, $directories + 4 * 8)
  $size = [BitConverter]::ToInt32($bytes, $directories + 4 * 8 + 4)
  if ($offset -eq 0 -or $size -lt 8 -or [BitConverter]::ToUInt16($bytes, $offset + 6) -ne 2) { return $null }
  $length = [BitConverter]::ToInt32($bytes, $offset)
  $signed = [byte[]]::new($length - 8)
  [Array]::Copy($bytes, $offset + 8, $signed, 0, $length - 8)
  $cms = [Security.Cryptography.Pkcs.SignedCms]::new()
  $cms.Decode($signed)
  $cms
}

# The digest algorithms of a program's signature, *cms*, and of its RFC 3161 timestamp, or why it has none.
function Get-PeDigests($Cms) {
  if (-not $Cms) { return 'no signature' }
  $signer = $Cms.SignerInfos[0]
  $stamp = $signer.UnsignedAttributes | Where-Object { $_.Oid.Value -eq '1.3.6.1.4.1.311.3.3.1' } | Select-Object -First 1
  if (-not $stamp) { return "$($signer.DigestAlgorithm.Value), no RFC 3161 timestamp" }
  $token = $null
  $read = 0
  if (-not [Security.Cryptography.Pkcs.Rfc3161TimestampToken]::TryDecode($stamp.Values[0].RawData, [ref]$token, [ref]$read)) { return "$($signer.DigestAlgorithm.Value), a timestamp that does not decode" }
  "$($signer.DigestAlgorithm.Value), timestamped $($token.TokenInfo.HashAlgorithmId.Value)"
}

switch ($Command) {
  'list' {
    if (-not $First -or -not $Second) { throw 'usage: sign-win.ps1 list <folder> <catalog>' }
    $from = Split-Path -Parent ([IO.Path]::GetFullPath($Second))
    $lines = Get-Signed $First | ForEach-Object { [IO.Path]::GetRelativePath($from, $_) }
    if (-not $lines) { throw "sign-win.ps1: $First holds nothing to sign" }
    [IO.File]::WriteAllLines([IO.Path]::GetFullPath($Second), [string[]]$lines)
    $lines
  }

  'trial' {
    if (-not $First) { throw 'usage: sign-win.ps1 trial <catalog>' }
    $X509 = [Security.Cryptography.X509Certificates.X509Certificate2]
    $now = [DateTimeOffset]::UtcNow
    $root = "CN=Surogate trial root $([Guid]::NewGuid().ToString('N'))"
    $rootKey = [Security.Cryptography.RSA]::Create(3072)
    $asked = [Security.Cryptography.X509Certificates.CertificateRequest]::new($root, $rootKey, 'SHA256', [Security.Cryptography.RSASignaturePadding]::Pkcs1)
    $asked.CertificateExtensions.Add([Security.Cryptography.X509Certificates.X509BasicConstraintsExtension]::new($true, $true, 0, $true))
    $asked.CertificateExtensions.Add([Security.Cryptography.X509Certificates.X509KeyUsageExtension]::new('KeyCertSign, CrlSign', $true))
    $rootCertificate = $asked.CreateSelfSigned($now.AddDays(-1), $now.AddDays(2))
    $leafKey = [Security.Cryptography.RSA]::Create(3072)
    $asked = [Security.Cryptography.X509Certificates.CertificateRequest]::new('CN=Surogate trial signer', $leafKey, 'SHA256', [Security.Cryptography.RSASignaturePadding]::Pkcs1)
    $asked.CertificateExtensions.Add([Security.Cryptography.X509Certificates.X509BasicConstraintsExtension]::new($false, $false, 0, $true))
    $asked.CertificateExtensions.Add([Security.Cryptography.X509Certificates.X509KeyUsageExtension]::new('DigitalSignature', $true))
    $codeSigning = [Security.Cryptography.OidCollection]::new()
    $codeSigning.Add([Security.Cryptography.Oid]::new('1.3.6.1.5.5.7.3.3')) | Out-Null
    $asked.CertificateExtensions.Add([Security.Cryptography.X509Certificates.X509EnhancedKeyUsageExtension]::new($codeSigning, $false))
    $serial = [byte[]]::new(16)
    [Security.Cryptography.RandomNumberGenerator]::Fill($serial)
    $leaf = [Security.Cryptography.X509Certificates.RSACertificateExtensions]::CopyWithPrivateKey($asked.Create($rootCertificate, $now.AddHours(-1), $now.AddDays(1), $serial), $leafKey)

    # The root, trusted by this computer, where signtool and WinVerifyTrust look; the signer, in a file of this run's.
    $store = [Security.Cryptography.X509Certificates.X509Store]::new('Root', 'LocalMachine')
    $store.Open('ReadWrite')
    $store.Add($X509::new($rootCertificate.Export('Cert')))
    $store.Close()
    $password = [Convert]::ToBase64String([Security.Cryptography.RandomNumberGenerator]::GetBytes(24))
    $pfx = Join-Path ([IO.Path]::GetTempPath()) "surogate-trial-$([Guid]::NewGuid().ToString('N')).pfx"
    [IO.File]::WriteAllBytes($pfx, $leaf.Export('Pfx', $password))
    try {
      $from = Split-Path -Parent ([IO.Path]::GetFullPath($First))
      $files = Get-Content $First | ForEach-Object { Join-Path $from $_ }
      & (Signtool) sign /q /fd SHA256 /tr $TrialTimestamps /td SHA256 /f $pfx /p $password @files | Out-Host
      if ($LASTEXITCODE -ne 0) { throw "sign-win.ps1: signtool sign exited $LASTEXITCODE" }
    } finally {
      Remove-Item -Force $pfx
    }
    $root
  }

  'verify' {
    if (-not $First) { throw 'usage: sign-win.ps1 verify <folder> [<root>]' }
    $signtool = Signtool
    $chained = if ($Second) { @('/r', ($Second -replace '^CN=', '')) } else { @() }
    $signers = @{}
    $signed = @(Get-Signed $First)
    if (-not $signed) { throw "sign-win.ps1: $First holds nothing the release signs" }
    $wrong = foreach ($file in $signed) {
      # The signature the file carries, which signtool checks: never one of Windows's catalogs, which a library of
      # Windows's own is in too, whatever signature it carries (Get-AuthenticodeSignature answers the catalog's).
      $said = & $signtool verify /pa /all /tw @chained $file 2>&1 | Out-String
      if ($LASTEXITCODE -ne 0) { "$file`: signtool verify exited $LASTEXITCODE`n$said"; continue }
      if ([IO.Path]::GetExtension($file) -eq '.msi') {
        $signature = Get-AuthenticodeSignature -LiteralPath $file
        if ($signature.Status -ne 'Valid' -or -not $signature.TimeStamperCertificate) { "$file`: $($signature.Status), $($signature.StatusMessage)"; continue }
        $signers[$signature.SignerCertificate.Subject] = $true
        if ($said -notmatch 'Hash of file \(sha256\)') { "$file`: no SHA-256 digest" }
      } else {
        $cms = Get-PeSignature $file
        if ($cms) { $signers[$cms.SignerInfos[0].Certificate.Subject] = $true }
        $digests = Get-PeDigests $cms
        if ($digests -ne "$Sha256, timestamped $Sha256") { "$file`: $digests" }
      }
    }
    if ($signers.Count -gt 1) { $wrong = @($wrong) + "signed by more than one signer: $($signers.Keys -join '; ')" }
    $nodes = @(Get-ChildItem -Path $First -Recurse -File | Where-Object { $_.FullName.Replace('/', '\').EndsWith($Node, [StringComparison]::OrdinalIgnoreCase) } | ForEach-Object FullName)
    foreach ($node in $nodes) {
      $said = & $signtool verify /pa /all $node 2>&1 | Out-String
      $code = $LASTEXITCODE
      $cms = Get-PeSignature $node
      $subject = if ($cms) { $cms.SignerInfos[0].Certificate.Subject } else { 'no signer' }
      if ($code -ne 0 -or $subject -notmatch '\bO=OpenJS Foundation\b') { $wrong = @($wrong) + "$node`: not the OpenJS Foundation's: $subject`n$said" }
    }
    if ($wrong) { throw "sign-win.ps1: these are not signed as the release signs:`n$($wrong -join "`n")" }
    "verified, by $($signers.Keys -join '; ')$(if ($nodes) { "; node.exe by the OpenJS Foundation" })"
  }

  'untrust' {
    if (-not $First) { throw 'usage: sign-win.ps1 untrust <root>' }
    $store = [Security.Cryptography.X509Certificates.X509Store]::new('Root', 'LocalMachine')
    $store.Open('ReadWrite')
    foreach ($certificate in @($store.Certificates | Where-Object Subject -eq $First)) { $store.Remove($certificate) }
    $store.Close()
  }
}
