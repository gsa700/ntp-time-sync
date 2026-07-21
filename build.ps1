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

# Also emit the release-named exe fresh every build. PyInstaller names the file
# "NTP Time Sync.exe"; the release asset is "NTP-Time-Sync.exe". Producing it here
# (rather than a manual copy at release time) means it's never a stale leftover.
$release = Join-Path $root "dist\NTP-Time-Sync.exe"
Copy-Item $built $release -Force

# Portable edition: exe + marker + example config + README, zipped. The marker
# file makes the app run in place -- config beside the exe, no install, no Run
# key, no Add/Remove entry -- so it runs from a USB stick and leaves no trace.
Write-Host "==> Packaging portable zip" -ForegroundColor Cyan
$portDir = Join-Path $root "dist\portable"
Remove-Item $portDir -Recurse -Force -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $portDir | Out-Null
Copy-Item $built (Join-Path $portDir "NTP-Time-Sync.exe") -Force
$marker = @"
This file marks NTP Time Sync as portable.

While portable.txt sits next to NTP-Time-Sync.exe, the app keeps its settings in
this folder (config.json), never installs itself, and adds nothing to Windows --
no Start-at-logon entry, no Add/Remove Programs listing. Run it from anywhere,
including a USB stick, and it leaves no trace.

Delete this file if you want the exe to install itself normally instead.
"@
Set-Content -Path (Join-Path $portDir "portable.txt") -Value $marker -Encoding utf8
Copy-Item (Join-Path $root "config.example.json") $portDir -Force
Copy-Item (Join-Path $root "README.md") $portDir -Force
$zip = Join-Path $root "dist\NTP-Time-Sync-portable.zip"
Remove-Item $zip -Force -ErrorAction SilentlyContinue
Compress-Archive -Path (Join-Path $portDir '*') -DestinationPath $zip -Force
Write-Host "==> Built $zip" -ForegroundColor Green

if ($NoDeploy) { Write-Host "==> -NoDeploy set; skipping install." ; return }

# Deploy over the copy that is actually installed, not a hardcoded guess: the
# exe may sit anywhere the user put it, and the Start-at-logon entry points at
# that path. Writing to the recommended folder regardless would leave a second
# install behind and a logon entry still aimed at the old one.
$dest = $null
$run = Get-ItemProperty "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" `
    -Name NtpTimeSync -ErrorAction SilentlyContinue
if ($run) {
    $candidate = $run.NtpTimeSync.Trim('"')
    # Ignore a dev-checkout entry (pythonw + script); only follow a real exe.
    if ($candidate -like "*.exe" -and $candidate -notlike "*pythonw*") { $dest = $candidate }
}
if ($dest) {
    Write-Host "==> Existing install: $dest" -ForegroundColor DarkGray
} else {
    $dest = Join-Path $env:LOCALAPPDATA "Programs\NTP Time Sync\NTP-Time-Sync.exe"
    Write-Host "==> No install found; using default: $dest" -ForegroundColor DarkGray
}

Write-Host "==> Stopping running instances" -ForegroundColor Cyan
Get-CimInstance Win32_Process | Where-Object { $_.Name -match "NTP.?Time.?Sync" } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2

New-Item -ItemType Directory -Force -Path (Split-Path $dest) | Out-Null
Copy-Item $built $dest -Force
Start-Process -FilePath $dest
Write-Host "==> Updated and relaunched: $dest" -ForegroundColor Green
Write-Host "    (settings in %APPDATA% preserved)"
