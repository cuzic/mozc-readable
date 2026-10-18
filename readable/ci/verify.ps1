# mozc-readable CI: install the MSI, verify it, publish/read the shared state (uninstall.ps1 follows).
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
$tmp = $env:RUNNER_TEMP
# Keys (mozc ms-ime keymap): 0x19 Kanji = IME on/off toggle, 0xF1 Katakana, 0xF0 Eisu, 0xF2 Hiragana.
# Sequence: ON -> full katakana -> half alnum -> hiragana -> OFF.
$keys = '19,F1,F0,F2,19'

function Run-Scenario($name, $readerMode) {
  $tidFile = Join-Path $tmp "tid_$name.txt"
  $uiOut = Join-Path $tmp "ui_$name.out"; $rdOut = Join-Path $tmp "rd_$name.out"
  Remove-Item $tidFile -ErrorAction SilentlyContinue
  $ui = Start-Process python -ArgumentList "`"$here\ui_host.py`" `"$tidFile`" $keys" -PassThru -RedirectStandardOutput $uiOut -RedirectStandardError "$uiOut.err"
  $rdArgs = "`"$here\reader.py`" `"$tidFile`" 30 $readerMode"
  $rd = Start-Process python -ArgumentList $rdArgs -PassThru -RedirectStandardOutput $rdOut -RedirectStandardError "$rdOut.err"
  $rd.WaitForExit(60000) | Out-Null
  if (-not $ui.HasExited) { $ui.WaitForExit(20000) | Out-Null }
  Write-Host "--- $name ui_host:"; Get-Content $uiOut, "$uiOut.err" -ErrorAction SilentlyContinue
  Write-Host "--- $name reader:"; Get-Content $rdOut, "$rdOut.err" -ErrorAction SilentlyContinue
  return @{ Ui = $uiOut; Rd = $rdOut; RdExit = $rd.ExitCode }
}

$poll = Run-Scenario 'poll' ''
$t = Get-Content $poll.Rd -Raw
Check 'reader opened the mapping from another process' ($poll.RdExit -eq 0)
Check 'open: IME turned on is visible'  ($t -match 'eff_open=1 eff_mode=1')
Check 'mode: full katakana (2) is visible' ($t -match 'eff_open=1 eff_mode=2')
Check 'mode: half alnum (3) is visible'    ($t -match 'eff_open=1 eff_mode=3')
Check 'open: IME turned off is visible (last state)' ($t -match 'eff_open=0 eff_mode=\d[^\n]*\r?\n[^\n]*distinct' -or $t -match '(?s)eff_open=1.*eff_open=0')

Write-Host '== E2. event-driven reader and key->reader latency'
$ev = Run-Scenario 'event' 'event'
$e = Get-Content $ev.Rd -Raw
Check 'reader opened the change event' ($e -match 'opened change event')
Check 'event woke the reader at least once' ($e -match 'wakes=[1-9]')
python "$here\summarize.py" $ev.Ui $ev.Rd
Check 'every key produced a state change within 1000 ms' ($LASTEXITCODE -eq 0)

if ($fail -gt 0) { Write-Host "$fail check(s) failed"; exit 1 }
