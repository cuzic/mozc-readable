# mozc-readable CI: install the MSI, verify it, publish/read the shared state (uninstall.ps1 follows).
# Stages A-D are hard checks (exit 1). Stage E (activate the TIP and read published state)
# is reported with ::warning:: while the activation procedure is still being established.
# (CI timing note: measured on run 39099740462, the four sections ran in 59-136 s in parallel versus about 307 s in series.)
# -Section splits the scenarios so CI can run them in parallel on separate runners (A-D always run):
#   state = E + E2, apps = G (Chrome, Notepad), appcontainer = the AppContainer check,
#   config = I (writes config1.db into the user profile and restarts mozc_server: it changes the profile and the
#   running server, so it has a runner of its own),
#   deactivation = H (switches the keyboard layout, which is global to the session, so it needs a runner
#   of its own),
#   composition = J (the version 3 fields, ADR-267: composition_probe.py; it starts cold on purpose (no warm-up) to
#   observe server_ready, and ends with a layout switch, so it too has a runner of its own).
#   'all' runs everything in this order, as before.
# Where a new scenario goes: one that only needs the TIP loaded goes in the section that matches what it
# looks at (state: shared-memory values and latency; apps: ordinary foreground apps; appcontainer: inside an
# AppContainer). One that changes the state of the whole session (switching the input method or layout,
# changing the language list or IME settings, starting or stopping a service) gets a section of its own,
# like deactivation, so it runs on a separate runner. When you add a section, change the ValidateSet below
# and `matrix.section` in .github/workflows/readable-build.yaml together.
param([Parameter(Mandatory)][string]$Msi,
      [ValidateSet('all', 'state', 'apps', 'appcontainer', 'config', 'deactivation', 'composition')][string]$Section = 'all')
$ErrorActionPreference = 'Stop'
$Clsid   = '{AFC94560-63DA-488E-9227-B04F3EF34B5B}'
$Profile = '{7D7B9A38-A0A4-4390-9706-509E98A8C791}'
$OfficialClsid = '{10A67BC8-22FA-4A59-90DC-2546652C56BF}'
$fail = 0
function Check($name, $ok, $detail = '') {
  if ($ok) { Write-Host "PASS  $name $detail" } else { Write-Host "FAIL  $name $detail"; $script:fail++ }
}

function Want($name) { return ($Section -eq 'all' -or $Section -eq $name) }

function Soft($name, $ok, $detail = '') {
  if ($ok) { Write-Host "PASS  $name $detail" } else { Write-Host "::warning::NO    $name $detail" }
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

# Every `Start-Process python` below uses -NoNewWindow. Without it each console python process gets its own console
# window (a Windows Terminal window on this runner) which takes the foreground, and the key the window process is
# sending at that moment goes to that console instead: the 'first key lost, resend after 8 s' seen in this CI was
# exactly that, in the unmodified Mozc as well (readable/ci/first_key_experiment.ps1, run 39104699356: every
# failed probe had fg_class=CASCADIA_HOSTING_WINDOW_CLASS).
function Run-Scenario($name, $readerMode, $keys) {
  $tidFile = Join-Path $tmp "tid_$name.txt"
  $uiOut = Join-Path $tmp "ui_$name.out"; $rdOut = Join-Path $tmp "rd_$name.out"
  Remove-Item $tidFile -ErrorAction SilentlyContinue
  $ui = Start-Process python -ArgumentList "`"$here\ui_host.py`" `"$tidFile`" $keys" -PassThru -NoNewWindow -RedirectStandardOutput $uiOut -RedirectStandardError "$uiOut.err"
  $rdArgs = "`"$here\reader.py`" `"$tidFile`" 30 $readerMode"
  $rd = Start-Process python -ArgumentList $rdArgs -PassThru -NoNewWindow -RedirectStandardOutput $rdOut -RedirectStandardError "$rdOut.err"
  $rd.WaitForExit(60000) | Out-Null
  if (-not $ui.HasExited) { $ui.WaitForExit(20000) | Out-Null }
  Write-Host "--- $name ui_host:"; Get-Content $uiOut, "$uiOut.err" -ErrorAction SilentlyContinue | Out-Host
  Write-Host "--- $name reader:"; Get-Content $rdOut, "$rdOut.err" -ErrorAction SilentlyContinue | Out-Host
  return @{ Ui = $uiOut; Rd = $rdOut; RdExit = $rd.ExitCode }
}

# Warm-up: the first activation starts mozc_server (a previous run saw ~3 s for the first key and a
# lost key in the cold polling run). Not asserted; only there so the measured runs are steady state.
# Every section that drives the IME needs it on its own runner: without it the AppContainer check's first
# key (IME on) was lost on all 3 tries (runs 39099740462 and 39100514498, 'all steps reached' was NO).
# 'deactivation' has its own scenario and does not need it.
# 'composition' starts cold on purpose (server_ready, ADR-267 D10).
if ($Section -notin 'deactivation', 'composition') { $null = Run-Scenario 'warmup' '' 'F2=1:1,19=0:*' }

if (Want 'state') {
$poll = Run-Scenario 'poll' '' $keys
$t = Get-Content $poll.Rd -Raw
Check 'reader opened the mapping from another process' ($poll.RdExit -eq 0)
Check 'block is version 3 and active=1 while the TIP is live (read from another process)' ($t -match 'active=1 version=3')
Check 'the version 3 fields are in the reader output' ($t -match 'phase=\d cat=\d plen=\d pcur=\d key_seq=\d+ sfm=\d srv=\d cks=\d+')
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
}

if (Want 'apps') {
Write-Host '== G. other applications: is the state published for the foreground thread of a TSF-native app?'
# Soft checks: this is the question the project exists for, not a regression gate (yet).
function Run-App($label, $exe, $exeArgs, $class, $procName) {
  Write-Host "--- $label"
  $sp = @{ FilePath = $exe; PassThru = $true }
  if ($exeArgs) { $sp.ArgumentList = $exeArgs }   # Start-Process rejects an empty -ArgumentList
  $p = Start-Process @sp
  Start-Sleep -Seconds 6
  $out = Join-Path $tmp "app_$label.out"
  $d = Start-Process python -ArgumentList "`"$here\drive_foreground.py`" $class F2=1:1,F1=1:2,F0=1:3,F2=1:1,19=0:* 30" -PassThru -NoNewWindow -Wait -RedirectStandardOutput $out -RedirectStandardError "$out.err"
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
Set-Content $page '<!doctype html><title>t</title><style>html,body{margin:0;height:100%}</style><input id=a autofocus style="position:fixed;left:0;top:0;width:100%;height:100%;box-sizing:border-box;font-size:40px">' -Encoding UTF8
$chrome = @("${env:ProgramFiles}\Google\Chrome\Application\chrome.exe", "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe") | Where-Object { Test-Path $_ } | Select-Object -First 1
if ($chrome) {
  $udd = Join-Path $tmp 'chrome-profile'
  Run-App 'chrome' $chrome "--user-data-dir=`"$udd`" --no-first-run --no-default-browser-check --new-window `"file:///$($page -replace '\\','/')`"" 'Chrome_WidgetWin_1' 'chrome'
} else { Write-Host '::warning::chrome.exe not found on the runner' }
Run-App 'notepad' 'notepad.exe' '' 'Notepad' 'notepad'
}

if (Want 'appcontainer') {
Write-Host '--- appcontainer (the sandbox UWP apps run in): ui_host.py started inside an AppContainer'
$acDir = Join-Path $tmp 'ac'
$acLaunchOut = Join-Path $tmp 'ac_launch.out'
# 60 s idle is only an upper bound now: the child is stopped as soon as the drive finishes (below). The drive
# needs about 25 s after the 12 s head start (measured: run 39097062197), and each lost first key adds 8 s.
$acLaunch = Start-Process python -ArgumentList "`"$here\ac_launch.py`" `"$acDir`" 60" -PassThru -NoNewWindow -RedirectStandardOutput $acLaunchOut -RedirectStandardError "$acLaunchOut.err"
Start-Sleep -Seconds 12
Get-Content $acLaunchOut, "$acLaunchOut.err" -ErrorAction SilentlyContinue | Out-Host
Write-Host '--- Application event log (crash records, last 5 minutes):'
Get-WinEvent -FilterHashtable @{ LogName = 'Application'; Id = 1000, 1001; StartTime = (Get-Date).AddMinutes(-5) } -ErrorAction SilentlyContinue |
  Select-Object -First 4 | ForEach-Object { "[$($_.Id)] " + (($_.Message -split "`n" | Select-Object -First 12) -join ' | ') } | Out-Host
Get-Content (Join-Path $acDir 'ac_ui_host.out') -ErrorAction SilentlyContinue | Out-Host
$acOut = Join-Path $tmp 'app_appcontainer.out'
$d = Start-Process python -ArgumentList "`"$here\drive_foreground.py`" ReadableCiHost F2=1:1,F1=1:2,F0=1:3,F2=1:1,19=0:* 30" -PassThru -NoNewWindow -Wait -RedirectStandardOutput $acOut -RedirectStandardError "$acOut.err"
Get-Content $acOut, "$acOut.err" -ErrorAction SilentlyContinue | Out-Host
Write-Host '--- ac_launch (monitor) output after the drive:'
# The drive is done. Stop the AppContainer child now instead of waiting out its idle time (the child lives
# only while its parent ac_launch.py lives, so stopping the parent ends it). Print the exit code when
# ac_launch ended by itself (a crash of the child before the drive finished).
if ($acLaunch.HasExited) { Write-Host "ac_launch: exited by itself, exit code $($acLaunch.ExitCode)" }
else { Stop-Process -Id $acLaunch.Id -Force -ErrorAction SilentlyContinue; Write-Host 'ac_launch: stopped after the drive' }
Start-Sleep -Milliseconds 500
Get-Content $acLaunchOut, "$acLaunchOut.err" -ErrorAction SilentlyContinue | Out-Host
Write-Host '--- appcontainer ui_host output after the drive:'
Get-Content (Join-Path $acDir 'ac_ui_host.out') -ErrorAction SilentlyContinue | Out-Host
Write-Host ("python processes still alive: " + ((Get-Process -Name python -ErrorAction SilentlyContinue | ForEach-Object { $_.Id }) -join ','))
Soft 'appcontainer: window became foreground' ($d.ExitCode -ne 2)
Soft 'appcontainer: shared block readable from outside the AppContainer' ($d.ExitCode -notin 2, 3)
Soft 'appcontainer: all steps reached' ($d.ExitCode -eq 0) "(exit $($d.ExitCode))"
Get-Process -Name python -ErrorAction SilentlyContinue | Where-Object { $_.Id -ne $PID } | Stop-Process -Force -ErrorAction SilentlyContinue
}

if (Want 'config') {
Write-Host '== I. config1.db: a Mozc Readable keymap file at the documented path is read by the running server'
# awase reads Mozc-family config1.db files (awase-gji-config). The path comes from Mozc's GetUserProfileDirectory:
# <LocalLow>\<product name>\config1.db, here "Mozc Readable". The default keymap (MS-IME) binds nothing to F8 while
# the IME is closed (DirectInput) or open (Precomposition); readable/ci/config_make.py writes a CUSTOM keymap in which
# F8 turns the IME on and off. So a state change on F8 proves the server read this file, from this path.
$profileDir = Join-Path $env:USERPROFILE 'AppData\LocalLow\Mozc Readable'
$cfgPath = Join-Path $profileDir 'config1.db'
Write-Host "config1.db before the test: exists=$(Test-Path $cfgPath) ($cfgPath)"
Soft 'no config1.db exists yet on a fresh runner (the control below relies on the default keymap)' (-not (Test-Path $cfgPath))

$ctl = Run-Scenario 'cfg-control' '' '77=same'
$ctlT = Get-Content $ctl.Ui -Raw
Check 'control: without config1.db, F8 changes nothing while the IME is closed' ($ctlT -match 'vk=0x77 expected=same .* ok=True')

python "$here\config_make.py" $cfgPath | Out-Host
Check 'config1.db was written' ((Test-Path $cfgPath) -and ((Get-Item $cfgPath).Length -eq 71))
# The server reads config1.db at startup only: stop it, the TIP starts a new one on the next key.
Get-Process -Name mozc_server -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2

$cus = Run-Scenario 'cfg-custom' '' '77=1:*,77=0:*'
$cusT = Get-Content $cus.Ui -Raw
Check 'custom keymap: F8 turns the IME on (the server read config1.db)' ($cusT -match 'vk=0x77 try=\d -> state=\(1, \d\) expected=\(1,None\) ok=True')
Check 'custom keymap: F8 turns the IME off' ($cusT -match 'vk=0x77 try=\d -> state=\(0, \d\) expected=\(0,None\) ok=True')
Write-Host ('mozc_server processes now: ' + ((Get-Process -Name mozc_server -ErrorAction SilentlyContinue | ForEach-Object { $_.Id }) -join ','))
}

if (Want 'deactivation') {
Write-Host '== H. deactivation (LAST: switching the layout is global, so nothing may run after this that needs the TIP)'
$env:MOZC_CI_DEACTIVATE = '1'
$de = Run-Scenario 'deactivate' '' 'F2=1:1'
Remove-Item Env:\MOZC_CI_DEACTIVATE
$deT = Get-Content $de.Ui -Raw
Soft 'active=1 before the layout switch' ($deT -match 'before deactivate: active=1')
Soft 'active becomes 0 after the thread switches to a layout without the TIP' ($deT -match 'deactivate: active=0')
}

if (Want 'composition') {
Write-Host '== J. version 3 fields (ADR-267 stage 1): composition_probe.py, in-process reads; the layout switch is last'
$env:MOZC_CI_DEACTIVATE = '1'
$tidFile = Join-Path $tmp 'tid_composition.txt'
$cpOut = Join-Path $tmp 'composition.out'
# Not `-Wait`: mozc_server is started by the TIP inside the probe, so it is a child of the probe holding the redirected
# output handles, and -Wait would wait for it too (the first run of this section hung for 15 minutes on that).
$cp = Start-Process python -ArgumentList "`"$here\composition_probe.py`" `"$tidFile`"" -PassThru -NoNewWindow -RedirectStandardOutput $cpOut -RedirectStandardError "$cpOut.err"
# Limit 150 s: the whole section took 44 s on run 39121303058 (section start 10:58:25 to probe end 10:59:09 including the cold start).
if (-not $cp.WaitForExit(150000)) { Write-Host '::warning::composition_probe.py did not finish in 150 s; killing it'; $cp.Kill() }
Remove-Item Env:\MOZC_CI_DEACTIVATE
Get-Content $cpOut, "$cpOut.err" -ErrorAction SilentlyContinue | Out-Host
Check 'composition_probe.py ran to the end' ($cp.HasExited -and $cp.ExitCode -eq 0) "(exit $($cp.ExitCode))"
foreach ($line in (Get-Content $cpOut -ErrorAction SilentlyContinue)) {
  if ($line -match '^probe: (HARD|SOFT) (.+) ok=(True|False)') {
    if ($Matches[1] -eq 'HARD') { Check $Matches[2] ($Matches[3] -eq 'True') } else { Soft $Matches[2] ($Matches[3] -eq 'True') }
  }
}
}

if ($fail -gt 0) { Write-Host "$fail check(s) failed"; exit 1 }
