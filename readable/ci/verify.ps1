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
# Sequence: ON (Hiragana key is idempotent) -> full katakana -> half alnum -> hiragana -> OFF.
# Each step waits for the expected published state and re-sends the key if it was lost.
$keys = 'F2=1:1,F1=1:2,F0=1:3,F2=1:1,19=0:*'

function Run-Scenario($name, $readerMode, $keys) {
  $tidFile = Join-Path $tmp "tid_$name.txt"
  $uiOut = Join-Path $tmp "ui_$name.out"; $rdOut = Join-Path $tmp "rd_$name.out"
  Remove-Item $tidFile -ErrorAction SilentlyContinue
  $ui = Start-Process python -ArgumentList "`"$here\ui_host.py`" `"$tidFile`" $keys" -PassThru -RedirectStandardOutput $uiOut -RedirectStandardError "$uiOut.err"
  $rdArgs = "`"$here\reader.py`" `"$tidFile`" 30 $readerMode"
  $rd = Start-Process python -ArgumentList $rdArgs -PassThru -RedirectStandardOutput $rdOut -RedirectStandardError "$rdOut.err"
  $rd.WaitForExit(60000) | Out-Null
  if (-not $ui.HasExited) { $ui.WaitForExit(20000) | Out-Null }
  Write-Host "--- $name ui_host:"; Get-Content $uiOut, "$uiOut.err" -ErrorAction SilentlyContinue | Out-Host
  Write-Host "--- $name reader:"; Get-Content $rdOut, "$rdOut.err" -ErrorAction SilentlyContinue | Out-Host
  return @{ Ui = $uiOut; Rd = $rdOut; RdExit = $rd.ExitCode }
}

# Warm-up: the first activation starts mozc_server (a previous run saw ~3 s for the first key and a
# lost key in the cold polling run). Not asserted; only there so the measured runs are steady state.
$null = Run-Scenario 'warmup' '' 'F2=1:1,19=0:*'
$poll = Run-Scenario 'poll' '' $keys
$t = Get-Content $poll.Rd -Raw
Check 'reader opened the mapping from another process' ($poll.RdExit -eq 0)
Check 'open: IME turned on is visible'  ($t -match 'eff_open=1 eff_mode=1')
Check 'mode: full katakana (2) is visible' ($t -match 'eff_open=1 eff_mode=2')
Check 'mode: half alnum (3) is visible'    ($t -match 'eff_open=1 eff_mode=3')
Check 'open: IME turned off is visible (last state)' ($t -match 'eff_open=0 eff_mode=\d[^\n]*\r?\n[^\n]*distinct' -or $t -match '(?s)eff_open=1.*eff_open=0')

Write-Host '== E2. event-driven reader and key->reader latency'
$ev = Run-Scenario 'event' 'event' $keys
$e = Get-Content $ev.Rd -Raw
Check 'reader opened the change event' ($e -match 'opened change event')
Check 'event woke the reader at least once' ($e -match 'wakes=[1-9]')
python "$here\summarize.py" $ev.Ui $ev.Rd
Check 'every key produced a state change within 1000 ms' ($LASTEXITCODE -eq 0)

Write-Host '== G. other applications: is the state published for the foreground thread of a TSF-native app?'
# Soft checks: this is the question the project exists for, not a regression gate (yet).
function Soft($name, $ok, $detail = '') {
  if ($ok) { Write-Host "PASS  $name $detail" } else { Write-Host "::warning::NO    $name $detail" }
}
function Run-App($label, $exe, $exeArgs, $class, $procName) {
  Write-Host "--- $label"
  $sp = @{ FilePath = $exe; PassThru = $true }
  if ($exeArgs) { $sp.ArgumentList = $exeArgs }   # Start-Process rejects an empty -ArgumentList
  $p = Start-Process @sp
  Start-Sleep -Seconds 6
  $out = Join-Path $tmp "app_$label.out"
  $d = Start-Process python -ArgumentList "`"$here\drive_foreground.py`" $class F2=1:1,F1=1:2,F0=1:3,F2=1:1,19=0:* 30" -PassThru -Wait -RedirectStandardOutput $out -RedirectStandardError "$out.err"
  Get-Content $out, "$out.err" -ErrorAction SilentlyContinue | Out-Host
  Write-Host "modules mozc_tip* loaded in ${procName}:"
  Get-Process -Name $procName -ErrorAction SilentlyContinue | ForEach-Object {
    try { $m = $_.Modules | Where-Object { $_.ModuleName -like 'mozc_tip*' }; if ($m) { "  pid $($_.Id): $($m.ModuleName -join ', ')" } } catch {}
  } | Out-Host
  Soft "${label}: target window became foreground" ($d.ExitCode -ne 2)
  Soft "${label}: shared block exists for the foreground thread" ($d.ExitCode -notin 2, 3)
  Soft "${label}: all steps (on / katakana / alnum / hiragana / off) reached" ($d.ExitCode -eq 0) "(exit $($d.ExitCode))"
  Get-Process -Name $procName -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
}
$page = Join-Path $tmp 'input.html'
Set-Content $page '<!doctype html><title>t</title><input id=a autofocus style="width:90%;font-size:30px">' -Encoding UTF8
$chrome = @("${env:ProgramFiles}\Google\Chrome\Application\chrome.exe", "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($chrome) {
  $udd = Join-Path $tmp 'chrome-profile'
  Run-App 'chrome' $chrome "--user-data-dir=`"$udd`" --no-first-run --no-default-browser-check --new-window `"file:///$($page -replace '\\','/')`"" 'Chrome_WidgetWin_1' 'chrome'
} else { Write-Host '::warning::chrome.exe not found on the runner' }
Run-App 'notepad' 'notepad.exe' '' 'Notepad' 'notepad'

Write-Host '--- appcontainer (the sandbox UWP apps run in): ui_host.py started inside an AppContainer'
$acDir = Join-Path $tmp 'ac'
python "$here\ac_launch.py" $acDir 70 | Out-Host
Start-Sleep -Seconds 8
Get-Content (Join-Path $acDir 'ac_ui_host.out') -ErrorAction SilentlyContinue | Out-Host
$acOut = Join-Path $tmp 'app_appcontainer.out'
$d = Start-Process python -ArgumentList "`"$here\drive_foreground.py`" ReadableCiHost F2=1:1,F1=1:2,F0=1:3,F2=1:1,19=0:* 30" -PassThru -Wait -RedirectStandardOutput $acOut -RedirectStandardError "$acOut.err"
Get-Content $acOut, "$acOut.err" -ErrorAction SilentlyContinue | Out-Host
Write-Host '--- appcontainer ui_host output after the drive:'
Get-Content (Join-Path $acDir 'ac_ui_host.out') -ErrorAction SilentlyContinue | Out-Host
Write-Host ("python processes still alive: " + ((Get-Process -Name python -ErrorAction SilentlyContinue | ForEach-Object { $_.Id }) -join ','))
Soft 'appcontainer: window became foreground' ($d.ExitCode -ne 2)
Soft 'appcontainer: shared block readable from outside the AppContainer' ($d.ExitCode -notin 2, 3)
Soft 'appcontainer: all steps reached' ($d.ExitCode -eq 0) "(exit $($d.ExitCode))"
Get-Process -Name python -ErrorAction SilentlyContinue | Where-Object { $_.Id -ne $PID } | Stop-Process -Force -ErrorAction SilentlyContinue

if ($fail -gt 0) { Write-Host "$fail check(s) failed"; exit 1 }
