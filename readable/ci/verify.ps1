# mozc-readable CI: install the MSI, verify it, publish/read the shared state, uninstall.
# Stages A-D are hard checks (exit 1). Stage E (activate the TIP and read published state)
# is reported with ::warning:: while the activation procedure is still being established.
param([Parameter(Mandatory)][string]$Msi)
$ErrorActionPreference = 'Stop'
$Clsid   = '{AFC94560-63DA-488E-9227-B04F3EF34B5B}'
$Profile = '{7D7B9A38-A0A4-4390-9706-509E98A8C791}'
$OfficialClsid = '{10A67BC8-22FA-4A59-90DC-2546652C56BF}'
$fail = 0
function Check($name, $ok, $detail = '') {
  if ($ok) { Write-Host "PASS  $name $detail" } else { Write-Host "FAIL  $name $detail"; $script:fail++ }
}

Write-Host '== A. install'
$p = Start-Process msiexec.exe -ArgumentList "/i `"$Msi`" /qn /norestart /l*v install.log" -Wait -PassThru
Check 'msiexec install exit code' ($p.ExitCode -in 0, 3010) "($($p.ExitCode))"
if ($p.ExitCode -notin 0, 3010) { Get-Content install.log -Tail 80 }

Write-Host '== B. files'
$dirs = @("${env:ProgramFiles}\Mozc Readable", "${env:ProgramFiles(x86)}\Mozc Readable") | Where-Object { Test-Path $_ }
Write-Host "install dirs: $($dirs -join ', ')"
Check 'install dir "Mozc Readable" exists' ($dirs.Count -gt 0)
foreach ($d in $dirs) { Get-ChildItem $d | ForEach-Object { Write-Host "  $($_.Name)" } }
Check 'mozc_tip64.dll present' (($dirs | ForEach-Object { Test-Path "$_\mozc_tip64.dll" }) -contains $true)
Check 'mozc_server.exe present' (($dirs | ForEach-Object { Test-Path "$_\mozc_server.exe" }) -contains $true)
Check 'official "Mozc" dir not touched' (-not (Test-Path "${env:ProgramFiles(x86)}\Mozc"))

Write-Host '== C. registry (COM + TSF)'
$inproc = Get-ItemProperty "Registry::HKEY_LOCAL_MACHINE\SOFTWARE\Classes\CLSID\$Clsid\InprocServer32" -ErrorAction SilentlyContinue
Check 'CLSID InprocServer32 registered' ($null -ne $inproc) "($($inproc.'(default)'))"
Check 'official Mozc CLSID not registered' (-not (Test-Path "Registry::HKEY_LOCAL_MACHINE\SOFTWARE\Classes\CLSID\$OfficialClsid"))
Check 'TSF TIP key present' (Test-Path "Registry::HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\CTF\TIP\$Clsid")
Check 'TSF ja-JP profile present' (Test-Path "Registry::HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\CTF\TIP\$Clsid\LanguageProfile\0x00000411\$Profile")

Write-Host '== D. add the input method for ja-JP'
try {
  $l = New-WinUserLanguageList ja-JP
  $l[0].InputMethodTips.Clear()
  $l[0].InputMethodTips.Add("0411:$Clsid$Profile")
  Set-WinUserLanguageList $l -Force
  Check 'Set-WinUserLanguageList' $true
  Write-Host ((Get-WinUserLanguageList | ForEach-Object { "$($_.LanguageTag): $($_.InputMethodTips -join ',')" }) -join "`n")
} catch { Check 'Set-WinUserLanguageList' $false $_.Exception.Message }

Write-Host '== E. activate and read the published state from another process'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$tidFile = Join-Path $env:RUNNER_TEMP 'readable_tid.txt'
Remove-Item $tidFile -ErrorAction SilentlyContinue
# 0x19 = VK_KANJI (Hankaku/Zenkaku), 0xF3 = VK_OEM_AUTO; pressed twice each: ON, OFF, ON, OFF.
$host_ = Start-Process python -ArgumentList "`"$here\ui_host.py`" `"$tidFile`" 19,19,F3,F3" -PassThru -NoNewWindow
$r = Start-Process python -ArgumentList "`"$here\reader.py`" `"$tidFile`" 22" -Wait -PassThru -NoNewWindow
if (-not $host_.HasExited) { $host_.WaitForExit(15000) | Out-Null }
if ($r.ExitCode -eq 0) { Write-Host 'PASS  reader opened the shared state mapping from another process' }
else { Write-Host "::warning::reader could not open the mapping (exit $($r.ExitCode)); TIP probably not activated in the CI session" }

Write-Host '== F. uninstall'
$u = Start-Process msiexec.exe -ArgumentList "/x `"$Msi`" /qn /norestart /l*v uninstall.log" -Wait -PassThru
Check 'msiexec uninstall exit code' ($u.ExitCode -in 0, 3010) "($($u.ExitCode))"
Check 'CLSID removed' (-not (Test-Path "Registry::HKEY_LOCAL_MACHINE\SOFTWARE\Classes\CLSID\$Clsid"))

if ($fail -gt 0) { Write-Host "$fail check(s) failed"; exit 1 }
