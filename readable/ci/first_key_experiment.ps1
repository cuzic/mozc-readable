# Experiment: does the first IME-on key of a fresh process get lost, and does it depend on how long the thread
# lived before the key?  Run for the official (unmodified) Mozc and for mozc-readable, then compare.
# Not a pass/fail check: it prints a table.  Observation = the text in an EDIT (readable/ci/first_key_probe.py).
param([Parameter(Mandatory)][string]$Msi,
      [Parameter(Mandatory)][ValidateSet('official', 'readable')][string]$Product,
      [string]$Delays = '0.5,2,5,10',
      [int]$Repeats = 3)
$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
if ($Product -eq 'official') {
  $Clsid = '{10A67BC8-22FA-4A59-90DC-2546652C56BF}'; $Profile = '{186F700C-71CF-43FE-A00E-AACB1D9E6D3D}'
} else {
  $Clsid = '{AFC94560-63DA-488E-9227-B04F3EF34B5B}'; $Profile = '{7D7B9A38-A0A4-4390-9706-509E98A8C791}'
}

Write-Host "== install ($Product)"
$p = Start-Process msiexec.exe -ArgumentList "/i `"$Msi`" /qn /norestart /l*v install.log" -Wait -PassThru
Write-Host "msiexec exit code: $($p.ExitCode)"
if ($p.ExitCode -notin 0, 3010) { Get-Content install.log -Tail 60; exit 1 }
$l = New-WinUserLanguageList ja-JP
$l[0].InputMethodTips.Clear()
$l[0].InputMethodTips.Add("0411:$Clsid$Profile")
Set-WinUserLanguageList $l -Force
Write-Host ((Get-WinUserLanguageList | ForEach-Object { "$($_.LanguageTag): $($_.InputMethodTips -join ',')" }) -join "`n")

function Run-Probe($delay, [switch]$WithConsoleNeighbour) {
  $server = [bool](Get-Process -Name mozc_server -ErrorAction SilentlyContinue)
  $out = Join-Path $env:RUNNER_TEMP "probe_$([guid]::NewGuid().ToString('N')).out"
  # NOT `-Wait`: it also waits for descendant processes, and the TIP loaded into this python process starts
  # mozc_server.exe, which never exits (run 39102127100 hung in the warm-up for 30 minutes).
  # The neighbour imitates verify.ps1: a console python process (the shared-memory reader) started right after the
  # window process.  It is started FIRST here so that it is up when the probe's key is sent only if the probe
  # waits long enough; with a short delay its console window appears around the moment of the key.
  $nb = $null
  if ($WithConsoleNeighbour) {
    $nbOut = "$out.neighbour"
    $nb = Start-Process python -ArgumentList '-c "import time; time.sleep(6)"' -PassThru -RedirectStandardOutput $nbOut -RedirectStandardError "$nbOut.err"
  }
  $pr = Start-Process python -ArgumentList "`"$here\first_key_probe.py`" $delay" -PassThru -RedirectStandardOutput $out -RedirectStandardError "$out.err"
  if (-not $pr.WaitForExit(60000)) { Write-Host "probe (delay $delay) did not exit in 60 s; killing it"; $pr.Kill() }
  if ($nb -and -not $nb.HasExited) { $nb.Kill() }
  $line = (Get-Content $out -ErrorAction SilentlyContinue | Where-Object { $_ -like 'RESULT*' } | Select-Object -First 1)
  if (-not $line) { $line = "RESULT delay=$delay text=<no result, exit $($pr.ExitCode)> ok=False"; Get-Content "$out.err" -ErrorAction SilentlyContinue | Out-Host }
  return [pscustomobject]@{ Delay = $delay; Server = $server; Line = $line; Ok = ($line -match 'ok=True') }
}

Write-Host '== warm-up (not counted): the first activation after installing starts mozc_server'
$w = Run-Probe 10
Write-Host "warm-up: $($w.Line) (mozc_server was running before it: $($w.Server))"

Write-Host "== measurement: product=$Product"
$rows = @()
foreach ($variant in @('alone', 'with-console-neighbour')) {
  Write-Host "-- variant: $variant"
  foreach ($d in $Delays.Split(',')) {
    for ($i = 1; $i -le $Repeats; $i++) {
      $r = if ($variant -eq 'alone') { Run-Probe $d } else { Run-Probe $d -WithConsoleNeighbour }
      $r | Add-Member -NotePropertyName Variant -NotePropertyValue $variant
      Write-Host ("delay={0,-4} run={1} server_was_running={2} -> {3}" -f $d, $i, $r.Server, $r.Line)
      $rows += $r
    }
  }
}
Write-Host ''
Write-Host "== summary ($Product): first key took effect / runs, per variant and delay before the key"
foreach ($g in ($rows | Group-Object Variant, Delay)) {
  $ok = ($g.Group | Where-Object { $_.Ok }).Count
  $self = ($g.Group | Where-Object { $_.Line -match 'fg_self=True' }).Count
  Write-Host ("  {0,-34} : {1}/{2} took effect; window was in the foreground at the key: {3}/{2}" -f $g.Name, $ok, $g.Count, $self)
}
foreach ($v in @('alone', 'with-console-neighbour')) {
  $sub = $rows | Where-Object { $_.Variant -eq $v }
  Write-Host ("  total {0,-24} : {1}/{2}" -f $v, ($sub | Where-Object { $_.Ok }).Count, $sub.Count)
}
