# mozc-readable CI: uninstall in its own step (a previous run lost the whole runner at msiexec /x).
param([Parameter(Mandatory)][string]$Msi)
$ErrorActionPreference = 'Continue'
$Clsid = '{AFC94560-63DA-488E-9227-B04F3EF34B5B}'
Get-Service -Name MozcReadableCacheService -ErrorAction SilentlyContinue | Stop-Service -Force -ErrorAction SilentlyContinue
foreach ($n in 'mozc_server', 'mozc_renderer', 'mozc_broker', 'mozc_cache_service', 'mozc_tool') {
  Get-Process -Name $n -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
}
$p = Start-Process msiexec.exe -ArgumentList "/x `"$Msi`" /qn /norestart REBOOT=ReallySuppress MSIRESTARTMANAGERCONTROL=Disable /l*v uninstall.log" -PassThru
if (-not $p.WaitForExit(300000)) { Write-Host '::error::msiexec /x did not finish in 300s'; $p.Kill(); exit 1 }
Write-Host "msiexec /x exit code: $($p.ExitCode)"
$gone = -not (Test-Path "Registry::HKEY_LOCAL_MACHINE\SOFTWARE\Classes\CLSID\$Clsid")
Write-Host "CLSID removed: $gone"
if ($p.ExitCode -notin 0, 3010 -or -not $gone) { exit 1 }
