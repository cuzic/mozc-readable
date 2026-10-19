# Installs an older mozc-readable release MSI, then the MSI under test with a plain `msiexec /i` (what a user
# does), and checks that the files were replaced. Background: v3.34.6239-readable.2 over .1 left the old DLLs
# because the file version did not rise (Windows Installer ignores the 4th ProductVersion field and does not
# replace a file of an equal version). Run on its own runner (the install is global to the session).
param([Parameter(Mandatory)][string]$NewMsi, [Parameter(Mandatory)][string]$OldTag)
$ErrorActionPreference = 'Stop'
$fail = 0
function Check($name, $ok, $detail = '') {
  if ($ok) { Write-Host "PASS  $name $detail" } else { Write-Host "FAIL  $name $detail"; $script:fail++ }
}
$old = Join-Path $PWD 'old-msi'
New-Item -ItemType Directory -Force $old | Out-Null
gh release download $OldTag -R cuzic/mozc-readable -p '*.msi' -D $old
$oldMsi = (Get-ChildItem $old -Filter *.msi | Select-Object -First 1).FullName
Write-Host "old MSI: $oldMsi"

$dll = "${env:ProgramFiles}\Mozc Readable\mozc_tip64.dll"
$server = "${env:ProgramFiles(x86)}\Mozc Readable\mozc_server.exe"
function Install($msi, $log) {
  $p = Start-Process msiexec.exe -ArgumentList "/i `"$msi`" /qn /norestart /l*v $log" -Wait -PassThru
  Check "msiexec /i $(Split-Path $msi -Leaf) exit code" ($p.ExitCode -in 0, 3010) "($($p.ExitCode))"
}
function Ver($path) { if (Test-Path $path) { (Get-Item $path).VersionInfo.FileVersion } else { '(missing)' } }

Install $oldMsi 'upgrade-old.log'
$dllDirs = @("${env:ProgramFiles}\Mozc Readable", "${env:ProgramFiles(x86)}\Mozc Readable") | Where-Object { Test-Path $_ }
$dll = ($dllDirs | ForEach-Object { Join-Path $_ 'mozc_tip64.dll' } | Where-Object { Test-Path $_ } | Select-Object -First 1)
$srv = ($dllDirs | ForEach-Object { Join-Path $_ 'mozc_server.exe' } | Where-Object { Test-Path $_ } | Select-Object -First 1)
$vOldDll = Ver $dll; $vOldSrv = Ver $srv
Write-Host "after the old MSI: mozc_tip64.dll=$vOldDll mozc_server.exe=$vOldSrv"

Install $NewMsi 'upgrade-new.log'
$vNewDll = Ver $dll; $vNewSrv = Ver $srv
Write-Host "after the new MSI: mozc_tip64.dll=$vNewDll mozc_server.exe=$vNewSrv"
Check 'mozc_tip64.dll was replaced by a plain /i (file version rose)' ([version]$vNewDll -gt [version]$vOldDll) "($vOldDll -> $vNewDll)"
Check 'mozc_server.exe was replaced by a plain /i (file version rose)' ([version]$vNewSrv -gt [version]$vOldSrv) "($vOldSrv -> $vNewSrv)"
Get-ChildItem "${env:ProgramFiles}\Mozc Readable", "${env:ProgramFiles(x86)}\Mozc Readable" -ErrorAction SilentlyContinue | ForEach-Object { Write-Host "  $($_.FullName) $($_.LastWriteTime)" }
$prof = Get-ItemProperty "Registry::HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\CTF\TIP\{AFC94560-63DA-488E-9227-B04F3EF34B5B}\LanguageProfile\0x00000411\{7D7B9A38-A0A4-4390-9706-509E98A8C791}" -ErrorAction SilentlyContinue
Check 'TSF profile is still registered after the upgrade' ($null -ne $prof)
if ($fail -gt 0) { Write-Host "$fail check(s) failed"; exit 1 }
