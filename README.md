# NTP Time Sync

A tiny Windows system-tray light for **Windows Time (w32time) sync health**.
Green means your PC clock is accurate; red means it isn't. No dialog to open,
no numbers to read — just a dot by the clock.

![NTP Time Sync status lights](docs/lights.png)

> **Why it exists:** FT8/FT4 digital modes (WSJT-X and friends) need the PC
> clock within about **1 second of UTC** or *nothing decodes*, even with strong
> signals in the waterfall. "Signals but no decodes" is almost always a clock
> problem. This app makes that failure visible at a glance — but it's useful to
> anyone who depends on an accurate Windows clock.

## Download (recommended)

A single self-contained executable — **no Python, no installer, no dependencies.**

**⬇ [Download the latest `NTP-Time-Sync.exe`](https://github.com/gsa700/ntp-time-sync/releases/latest)**

1. Download the `.exe` from the latest release and drop it anywhere (Desktop, a tools folder…).
2. Double-click it — a colored dot appears in the system tray.
3. **Windows SmartScreen** may say *"Windows protected your PC"* because the app isn't
   code-signed. Click **More info → Run anyway**. It's open source; every line is in this repo.
4. On **Windows 11**, do the one-time step in [Make the icon visible](#make-the-icon-visible-windows-11) below.
5. Left-click the dot to open the panel, then tick **Start at logon** to launch it automatically at boot.

First run creates its settings at `%APPDATA%\NTP Time Sync\config.json`.

## Make the icon visible (Windows 11)

Windows 11 **hides new tray icons by default** — the app is running, but the dot won't
show on the taskbar until you allow it once:

**Settings → Personalization → Taskbar → Other system tray icons →** turn the
**NTP Time Sync** entry **On**.

One-time and per-app; it sticks afterward. (Windows 10 shows the icon automatically.)

## The light

| Color  | Meaning |
|--------|---------|
| 🟢 Green  | Synced to the configured server and \|offset\| < 1 s |
| 🟡 Yellow | Synced but drifting (1–2 s) or last sync is stale (> 40 min) |
| 🔴 Red    | Wrong source, not synced, offset > 2 s, or server unreachable |
| ⚪ Gray   | Starting up / probe error |

Hover the icon for a one-line summary; **left-click** to open the panel.

## The panel

**Left-click** the tray dot to open the status panel — a solid status-colored
header over the live readout, with every action one click away:

<img src="docs/panel.png" alt="NTP Time Sync status panel" width="300">

- **Colored header** — green / yellow / red with the reason, readable at a glance
- **Readout** — offset, server, source, last sync (updates live while open)
- **Refresh** — re-probe immediately
- **Resync (admin)** — `w32tm /resync /force`; opens an elevated PowerShell (UAC)
- **Configure… (admin)** — change the NTP server, then applies it elevated (UAC)
- **Open time.is** — browser sanity check
- **Start at logon** — per-user autostart toggle (HKCU `Run`, no admin)
- **Quit**

Right-click gives a minimal fallback menu (Open / Quit). Polling is read-only and
runs **non-elevated**; only Resync and Configure raise a UAC prompt on demand.

## Run from source (developers)

Requires **Python 3.8+** on Windows.

```
pip install -r requirements.txt
pythonw ntp_time_sync.pyw
```

Double-clicking `ntp_time_sync.pyw` also works (runs windowless via `pythonw`).

### Build the executable

```
pip install pyinstaller
python make_ico.py
python -m PyInstaller --onefile --windowed --name "NTP Time Sync" ^
  --icon app.ico --hidden-import pystray._win32 ntp_time_sync.pyw
```

The bundled `NTP Time Sync.exe` lands in `dist/`.

## Configure

Settings live in `config.json` — in `%APPDATA%\NTP Time Sync\` for the packaged
`.exe`, or next to the script when run from source. Created on first run with these
defaults:

```json
{
  "server": "pool.ntp.org",
  "poll_seconds": 45,
  "green_max_offset": 1.0,
  "yellow_max_offset": 2.0,
  "stale_minutes": 40
}
```

- **server** — any NTP host or IP. Public pool by default; point it at a LAN
  time server if you run one (e.g. `192.0.2.10` or a GPS-disciplined NTP box).
- **green_max_offset / yellow_max_offset** — thresholds in seconds.
- **poll_seconds** — how often to probe.
- **stale_minutes** — if the last successful sync is older than this, don't show green.

Edit and restart, or use **Configure server…** to change the server from the UI.

## How it works

Read-only polling shells out to the built-in Windows tools:

- `w32tm /query /status` — current source and last successful sync time
- `w32tm /stripchart /computer:<server> /samples:1` — live offset vs. the server

No third-party time daemon required; it reports on whatever Windows Time is
already doing. The admin actions just wrap `w32tm /config` and `w32tm /resync`.

## Requirements

- **The `.exe`:** Windows 10/11 — nothing else; Python and all libraries are bundled.
- **From source:** Python 3.8+ with `pystray` and `Pillow` (see `requirements.txt`).

Uses the built-in `w32tm` and the Win32 notification area.

## Author

David Erickson (AB0R). Contributions and issues welcome.

## License

GPLv3 — see [LICENSE](LICENSE). Copyright (C) 2026 David Erickson (AB0R).
