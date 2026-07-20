<#
.SYNOPSIS
  Rebuild NTP Time Sync into a standalone .exe and update the installed copy.

.DESCRIPTION
  Compile-checks the source, builds a one-file windowed exe with PyInstaller,
  then (unless -NoDeploy) stops the running instance, overwrites the installed
  exe in %LOCALAPPDATA%\Programs\NTP Time Sync\, and relaunches it.

  Settings live in %APPDATA%\NTP Time Sync\config.json and are never touched,
  so your server/thresholds survive updates. The install path stays the same,
  so the Windows 11 tray-visibility toggle also sticks.

.EXAMPLE
  .\build.ps1            # build + update your install
  .\build.ps1 -NoDeploy  # just build into dist\ (e.g. before cutting a release)
#>
param([switch]$NoDeploy)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

# Prefer the repo-local venv; fall back to whatever python is on PATH.
$venvPy = Join-Path $root ".venv\Scripts\python.exe"
if (Test-Path $venvPy) {
    $py = $venvPy
    Write-Host "==> Using venv: $venvPy" -ForegroundColor DarkGray
} else {
    $py = "python"
    Write-Host "==> No .venv found; using python from PATH" -ForegroundColor DarkGray
}

Write-Host "==> Compile check" -ForegroundColor Cyan
& $py -m py_compile ntp_time_sync.pyw

Write-Host "==> Building exe (PyInstaller)" -ForegroundColor Cyan
& $py -m PyInstaller --noconfirm --clean --onefile --windowed `
    --name "NTP Time Sync" --icon "$root\app.ico" `
    --hidden-import pystray._win32 `
    --distpath dist --workpath build --specpath build ntp_time_sync.pyw | Out-Null

$built = Join-Path $root "dist\NTP Time Sync.exe"
if (-not (Test-Path $built)) { throw "Build failed: $built not found" }
$mb = [math]::Round((Get-Item $built).Length / 1MB, 1)
Write-Host "==> Built $built ($mb MB)" -ForegroundColor Green

if ($NoDeploy) { Write-Host "==> -NoDeploy set; skipping install." ; return }

$dest = Join-Path $env:LOCALAPPDATA "Programs\NTP Time Sync\NTP-Time-Sync.exe"
Write-Host "==> Stopping running instances" -ForegroundColor Cyan
Get-CimInstance Win32_Process | Where-Object { $_.Name -match "NTP.?Time.?Sync" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2

New-Item -ItemType Directory -Force -Path (Split-Path $dest) | Out-Null
Copy-Item $built $dest -Force
Start-Process -FilePath $dest
Write-Host "==> Updated and relaunched: $dest" -ForegroundColor Green
Write-Host "    (settings in %APPDATA% preserved)"
